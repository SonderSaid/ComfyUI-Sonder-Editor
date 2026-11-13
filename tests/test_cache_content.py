"""Content digest of the Editor's media outputs (``nodes/cache_content.py``).

Equal content gives equal digests whatever the memory layout, worker count or
batch size; any change to a byte, the shape, the dtype or the sample rate
changes it; unsupported values give None; the interrupt check runs between
batches and its raise propagates; the token is salted per process.
"""

import importlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "sonder_cache_content_test_pkg"


def _module():
    if TEST_PACKAGE not in sys.modules:
        package = types.ModuleType(TEST_PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = package
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.cache_content")


content = _module()


def _frames(count=5, height=6, width=4, seed=1):
    generator = torch.Generator().manual_seed(seed)
    return torch.rand((count, height, width, 3), generator=generator)


def test_equal_content_gives_equal_digests_whatever_the_layout():
    frames = _frames()
    base = content.digest("IMAGE", frames)
    assert base is not None and len(base) == 64
    assert content.digest("IMAGE", frames.clone()) == base
    # A non-contiguous view with the same logical content.
    permuted = frames.permute(0, 3, 1, 2).contiguous().permute(0, 2, 3, 1)
    assert not permuted.is_contiguous()
    assert content.digest("IMAGE", permuted) == base
    # A slice view of a larger tensor.
    larger = torch.cat([frames, _frames(seed=2)])
    assert content.digest("IMAGE", larger[:5]) == base
    # Channels-last memory format.
    assert content.digest("IMAGE", frames.contiguous(memory_format=torch.channels_last)) == base


def test_the_digest_is_independent_of_workers_and_batch_size(monkeypatch):
    frames = _frames(count=41)
    base = content.digest("IMAGE", frames)
    for workers, batch in ((1, 1), (2, 3), (8, 16), (8, 64)):
        monkeypatch.setattr(content, "MAX_WORKERS", workers)
        monkeypatch.setattr(content, "BATCH", batch)
        assert content.digest("IMAGE", frames) == base


def test_any_change_changes_the_digest():
    frames = _frames()
    base = content.digest("IMAGE", frames)
    changed = frames.clone()
    changed[3, 2, 1, 0] += 1e-6
    assert content.digest("IMAGE", changed) != base
    # Same bytes, another shape.
    assert content.digest("IMAGE", frames.reshape(5, 4, 6, 3)) != base
    assert content.digest("IMAGE", frames.reshape(10, 3, 4, 3)) != base
    # Same values, another dtype.
    assert content.digest("IMAGE", frames.double()) != base
    # Bytes, not values: -0.0 differs from 0.0 (a miss, never a false hit).
    zeros = torch.zeros((2, 2, 2, 3))
    negative = zeros.clone()
    negative[0, 0, 0, 0] = -0.0
    assert content.digest("IMAGE", zeros) != content.digest("IMAGE", negative)
    # Frame order matters.
    assert content.digest("IMAGE", frames.flip(0)) != base


def test_audio_digest_covers_waveform_and_sample_rate():
    waveform = torch.rand((1, 2, 4800))
    base = content.digest("AUDIO", {"waveform": waveform, "sample_rate": 48000})
    assert base is not None
    assert content.digest("AUDIO", {"waveform": waveform.clone(), "sample_rate": 48000}) == base
    assert content.digest("AUDIO", {"waveform": waveform, "sample_rate": 44100}) != base
    louder = waveform * 1.0001
    assert content.digest("AUDIO", {"waveform": louder, "sample_rate": 48000}) != base
    # An IMAGE with the audio's bytes is another identity.
    assert content.digest("IMAGE", waveform) != base


@pytest.mark.parametrize("socket, value", [
    ("IMAGE", "not a tensor"),
    ("IMAGE", torch.tensor(1.0)),  # 0-dim
    ("IMAGE", torch.zeros((2, 2)).to_sparse()),
    ("AUDIO", {"waveform": torch.zeros((1, 2, 4)), "sample_rate": 48000.0}),
    ("AUDIO", {"waveform": torch.zeros((1, 2, 4)), "sample_rate": True}),
    ("AUDIO", {"waveform": torch.zeros((1, 2, 4)), "sample_rate": 48000, "extra": 1}),
    ("AUDIO", {"waveform": "x", "sample_rate": 48000}),
    ("AUDIO", torch.zeros((1, 2, 4))),
    ("LATENT", torch.zeros((1, 4, 8, 8))),
])
def test_unsupported_values_give_none(socket, value):
    assert content.digest(socket, value) is None


@pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a GPU tensor")
def test_a_gpu_tensor_gives_none():
    assert content.digest("IMAGE", torch.zeros((1, 2, 2, 3), device="cuda")) is None


def test_inference_mode_tensors_hash_from_pool_threads():
    with torch.inference_mode():
        frames = _frames(count=20)
    assert frames.is_inference()
    assert content.digest("IMAGE", frames) == content.digest("IMAGE", frames.clone())


def test_the_interrupt_check_runs_between_batches_and_its_raise_propagates(monkeypatch):
    monkeypatch.setattr(content, "BATCH", 4)
    calls = []
    frames = _frames(count=10)
    content.digest("IMAGE", frames, lambda: calls.append(1))
    assert len(calls) == 3  # batches of 4, 4, 2

    class Cancelled(BaseException):
        pass

    def cancel():
        raise Cancelled()

    with pytest.raises(Cancelled):
        content.digest("IMAGE", frames, cancel)


def test_hashing_reads_in_place(monkeypatch):
    # Every reshape or copy works on one frame at a time, never the whole tensor;
    # a contiguous frame is not copied at all.
    calls = []
    for name in ("contiguous", "reshape"):
        real = getattr(torch.Tensor, name)

        def counting(self, *args, _real=real, _name=name, **kwargs):
            out = _real(self, *args, **kwargs)
            calls.append((_name, self.numel(), out.data_ptr() != self.data_ptr()))
            return out

        monkeypatch.setattr(torch.Tensor, name, counting)
    frames = _frames(count=6)
    content.digest("IMAGE", frames)
    assert calls and all(numel <= frames[0].numel() and not copied for _name, numel, copied in calls)
    calls.clear()
    content.digest("IMAGE", frames.permute(0, 2, 1, 3))
    assert any(copied for *_rest, copied in calls)
    assert all(numel <= frames[0].numel() for _name, numel, _copied in calls)


def test_tokens_are_salted_per_process():
    digest = content.digest("IMAGE", _frames())
    token = content.token(digest)
    assert content.is_token(token) and token == content.token(digest)
    assert digest not in token
    script = (f"import sys, types, importlib; p = types.ModuleType({TEST_PACKAGE!r}); p.__path__ = [{str(ROOT)!r}]; "
              f"sys.modules[{TEST_PACKAGE!r}] = p; "
              f"print(importlib.import_module({TEST_PACKAGE + '.nodes.cache_content'!r}).token({digest!r}))")
    other = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert other.returncode == 0, other.stderr
    assert other.stdout.strip() != token and content.is_token(other.stdout.strip())
