"""The Sonder Template Helper: Model Template values read from a model's VAE (today, the Audio grid).

In-process tests cover the refusal layers with attribute stubs. The crop check, the V3
node and real weights run in a subprocess from the ComfyUI root (``SONDER_COMFYUI_ROOT``,
or the install whose venv runs this test): ComfyUI's ``server`` and ``nodes`` collide
with this repository's packages. Real weights are looked up in ``SONDER_VAE_DIR``, the
install's ``models/vae``, or a Stability Matrix ``Models/VAE``; each missing file skips.
"""

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _rules():
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.template_rules")


def _audio_stub(rate=44100, block=4096, **attrs):
    fields = dict(first_stage_model=types.SimpleNamespace(encode=lambda x: x), downscale_ratio=block,
                  output_channels=2, audio_sample_rate=rate, crop_input=True,
                  memory_used_encode=lambda shape, dtype: 1)
    fields.update(attrs)
    return types.SimpleNamespace(**fields)


def _decoder_only(shape, dtype):
    raise RuntimeError("MiniMax Music3 DAV cannot encode audio")


@pytest.mark.parametrize("vae,status,words", [
    (None, "no_vae", "not a loaded VAE"),
    (types.SimpleNamespace(first_stage_model=None), "no_vae", "not a loaded VAE"),
    # Image VAE: the core default (8, three output channels).
    (_audio_stub(block=8, output_channels=3), "not_audio", "not a stereo audio VAE"),
    # Video VAE: a per-axis formula, not a block.
    (_audio_stub(block=(lambda a: a, 8, 8), output_channels=3), "not_audio", "not a stereo audio VAE"),
    (_audio_stub(block=(4096,)), "not_audio", "not a stereo audio VAE"),
    (_audio_stub(block=True), "not_audio", "not a stereo audio VAE"),
    (_audio_stub(first_stage_model=types.SimpleNamespace()), "no_encoder", "cannot encode (it has no encoder)"),
    (_audio_stub(block=512, memory_used_encode=_decoder_only), "no_encoder", "cannot encode audio"),
    # MMAudio: no crop (and a fractional block).
    (_audio_stub(block=512 * 44100 / 16000, crop_input=False), "no_crop", "does not crop"),
    (_audio_stub(rate=44099), "unsupported", "cannot be entered"),
    (_audio_stub(block=1411.2), "unsupported", "cannot be entered"),
    (_audio_stub(rate=8000, block=8001), "unsupported", "cannot be entered"),
])
def test_each_refusal_reports_off_with_its_reason(vae, status, words):
    result = _rules().audio_grid_from_vae(vae)
    assert result["status"] == status
    assert (result["rate"], result["block"]) == (0, 0)
    assert "no audio grid (outputs 0, 0)" in result["lines"][0]
    assert words in "\n".join(result["lines"])


def test_a_vae_like_object_without_a_crop_method_reports_off_instead_of_failing():
    pytest.importorskip("torch")
    result = _rules().audio_grid_from_vae(_audio_stub())  # no vae_encode_crop_pixels
    assert result["status"] == "crop_unexpected" and (result["rate"], result["block"]) == (0, 0)
    assert "could not be checked" in "\n".join(result["lines"])


def test_the_shared_facts_read_layout_rate_block_and_crop():
    facts = _rules().audio_encoder_facts
    assert facts(_audio_stub(block=4096.0)) == {"audio": True, "rate": 44100, "block": 4096, "crops": True}
    assert facts(_audio_stub(block=8, output_channels=3))["audio"] is False
    assert facts(types.SimpleNamespace()) == {"audio": False, "rate": None, "block": None, "crops": None}


def test_an_integral_float_block_is_a_whole_block():
    rules = _rules()
    assert rules._whole(4096.0) == 4096 and type(rules._whole(4096.0)) is int
    assert rules._whole(1411.2) == 1411.2 and rules._whole(True) is True


# ── Against the installed ComfyUI: its crop, the V3 node, real weights ──────────

_SCRIPT = r"""
import importlib, json, os, sys, types
repo, comfy_root, vae_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy_root)
import torch
import comfy.sd, comfy.utils, comfy.model_management

pkg = types.ModuleType("sonder_helper_pkg"); pkg.__path__ = [repo]
sys.modules["sonder_helper_pkg"] = pkg
# Only audio_grid is needed from `server`; its __init__ would register routes.
server_pkg = types.ModuleType("sonder_helper_pkg.server"); server_pkg.__path__ = [os.path.join(repo, "server")]
sys.modules["sonder_helper_pkg.server"] = server_pkg
rules = importlib.import_module("sonder_helper_pkg.nodes.template_rules")
helper = importlib.import_module("sonder_helper_pkg.nodes.template_helper")
out = {"families": {}, "real": {}}


class Stub:
    # The real core methods, bound to an object carrying only the load-time attributes.
    vae_encode_crop_pixels = comfy.sd.VAE.vae_encode_crop_pixels
    spacial_compression_encode = comfy.sd.VAE.spacial_compression_encode

    def __init__(self, rate, block, crop=True):
        self.first_stage_model = types.SimpleNamespace(encode=lambda x: x)
        self.downscale_ratio = self.upscale_ratio = block
        self.output_channels, self.audio_sample_rate, self.crop_input = 2, rate, crop
        self.pad_channel_value = "replicate"
        self.memory_used_encode = lambda shape, dtype: 1


# Every core audio VAE family's load-time values (comfy/sd.py).
for name, rate, block in (("ltx", 44100, 4096), ("h3", 32000, 800), ("oobleck", 44100, 2048),
                          ("oobleck_48k", 48000, 1920), ("ace", 44100, 4096), ("sa3", 44100, 4096)):
    out["families"][name] = rules.audio_grid_from_vae(Stub(rate, block))


class KeepsAll(Stub):
    def vae_encode_crop_pixels(self, pixels):
        return pixels


class CutsWholeBlocks(Stub):
    def vae_encode_crop_pixels(self, pixels):
        return pixels[:, 1:]


class TailCrop(Stub):  # keeps the whole blocks from the front: still a usable grid
    def vae_encode_crop_pixels(self, pixels):
        return pixels[:, : (pixels.shape[1] // self.downscale_ratio) * self.downscale_ratio]


class ToTwoBlocks(Stub):  # always keeps two blocks, whatever the length
    def vae_encode_crop_pixels(self, pixels):
        return pixels[:, : 2 * self.downscale_ratio]


class Reversed(Stub):  # the right length, the wrong content
    def vae_encode_crop_pixels(self, pixels):
        kept = comfy.sd.VAE.vae_encode_crop_pixels(self, pixels)
        return kept if kept.shape[1] == pixels.shape[1] else kept.flip(1)


out["keeps_all"] = rules.audio_grid_from_vae(KeepsAll(44100, 4096))
out["tail_crop"] = rules.audio_grid_from_vae(TailCrop(44100, 4096))
out["to_two_blocks"] = rules.audio_grid_from_vae(ToTwoBlocks(44100, 4096))
out["reversed"] = rules.audio_grid_from_vae(Reversed(44100, 4096))
out["block_2"] = rules.audio_grid_from_vae(Stub(8000, 2))
out["cuts_whole"] = rules.audio_grid_from_vae(CutsWholeBlocks(44100, 4096))

# The V3 node.
schema = helper.SonderTemplateHelper.GET_SCHEMA()
node = helper.SonderTemplateHelper.execute(vae=Stub(32000, 800))
off = helper.SonderTemplateHelper.execute(vae=types.SimpleNamespace(first_stage_model=None))
out["node"] = {
    "id": schema.node_id, "display": schema.display_name, "output_node": bool(schema.is_output_node),
    "inputs": [(i.id, i.get_io_type()) for i in schema.inputs],
    "outputs": [(o.display_name, o.get_io_type()) for o in schema.outputs],
    "aliases": list(getattr(schema, "search_aliases", None) or []),
    "values": list(node.args[:2]), "report_is_str": isinstance(node.args[2], str),
    "ui": node.ui.as_dict(), "report": node.args[2], "off": list(off.args[:2]),
    "return_names": list(helper.SonderTemplateHelper.RETURN_NAMES),
}

# Real weights, CPU: loaded with ComfyUI's own loader, the model never run.
for key, rel in (("ltx_2_5_audio", "LTX/ltx-2.5-audio-vae-bf16.safetensors"),
                 ("ltx_2_3_audio", "LTX23/LTX23_audio_vae_bf16.safetensors"),
                 ("h3_audio", "H3/minimax_h3_audio_vae_fp32.safetensors"),
                 ("ace_1_5", "AceSTEP/ace_1.5_vae.safetensors"),
                 ("wan_2_1_video", "Wan/wan_2.1_vae.safetensors"),
                 ("sdxl_image", "fixFP16ErrorsSDXLLowerMemoryUse_v10.safetensors")):
    path = os.path.join(vae_dir, rel)
    if not os.path.isfile(path):
        continue
    sd, metadata = comfy.utils.load_torch_file(path, return_metadata=True)
    vae = comfy.sd.VAE(sd=sd, metadata=metadata)
    # From here on, loading the model onto a device or running it is a failure.
    calls = []

    def refuse(name):
        def call(*args, **kwargs):
            calls.append(name)
            raise AssertionError(name)
        return call

    real_load = comfy.model_management.load_models_gpu
    comfy.model_management.load_models_gpu = refuse("load_models_gpu")
    vae.encode, vae.decode = refuse("vae.encode"), refuse("vae.decode")
    vae.first_stage_model.encode = refuse("model.encode")
    vae.first_stage_model.decode = refuse("model.decode")
    try:
        out["real"][key] = dict(rules.audio_grid_from_vae(vae), calls=calls)
    finally:
        comfy.model_management.load_models_gpu = real_load
    del vae, sd
print("HELPER_RESULT " + json.dumps(out))
"""


def _comfy_root():
    candidates = []
    if os.environ.get("SONDER_COMFYUI_ROOT"):
        candidates.append(Path(os.environ["SONDER_COMFYUI_ROOT"]))
    candidates.extend(Path(sys.executable).resolve().parents[:3])
    for root in candidates:
        if (root / "comfy" / "sd.py").is_file() and (root / "comfy_api").is_dir():
            return root
    return None


def _vae_dir(comfy_root):
    candidates = [Path(os.environ["SONDER_VAE_DIR"])] if os.environ.get("SONDER_VAE_DIR") else []
    candidates += [comfy_root / "models" / "vae", comfy_root.parents[1] / "Models" / "VAE"]
    return next((path for path in candidates if (path / "LTX").is_dir() or (path / "H3").is_dir()), candidates[0])


@pytest.fixture(scope="module")
def installed():
    comfy_root = _comfy_root()
    if comfy_root is None:
        pytest.skip("needs a ComfyUI install (SONDER_COMFYUI_ROOT or the test install's venv)")
    completed = subprocess.run([sys.executable, "-c", _SCRIPT, str(ROOT), str(comfy_root), str(_vae_dir(comfy_root))],
                               cwd=str(comfy_root), capture_output=True, text=True, timeout=900,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    line = next((item for item in completed.stdout.splitlines() if item.startswith("HELPER_RESULT ")), None)
    assert line is not None, completed.stderr[-4000:]
    return json.loads(line[len("HELPER_RESULT "):])


def test_every_core_audio_family_gives_its_grid_through_the_real_crop(installed):
    expected = {"ltx": (44100, 4096), "h3": (32000, 800), "oobleck": (44100, 2048),
                "oobleck_48k": (48000, 1920), "ace": (44100, 4096), "sa3": (44100, 4096)}
    for name, (rate, block) in expected.items():
        result = installed["families"][name]
        assert result["status"] == "grid", (name, result)
        assert (result["rate"], result["block"]) == (rate, block)
        report = "\n".join(result["lines"])
        assert f"{rate} Hz, block {block} samples" in report and "whole blocks pass intact" in report
        assert "Not checked: timing inside the encoder" in report  # says what it does not measure
    # The centre crop drops half the excess from the front: block - 1 extra -> (block - 1) // 2.
    assert "up to 399 (12.5 ms) of them from the start" in "\n".join(installed["families"]["h3"]["lines"])


def test_only_a_crop_that_keeps_exactly_the_whole_blocks_gives_a_grid(installed):
    assert installed["tail_crop"]["status"] == "grid"  # whole blocks kept, from the front
    for name in ("to_two_blocks", "reversed"):
        result = installed[name]
        assert result["status"] == "crop_unexpected" and (result["rate"], result["block"]) == (0, 0), name
    two = "\n".join(installed["block_2"]["lines"])
    assert "block 2 samples" in two and "lose up to 1 sample," in two


def test_a_crop_that_keeps_everything_or_cuts_whole_blocks_is_off(installed):
    assert installed["keeps_all"]["status"] == "no_crop"
    assert (installed["keeps_all"]["rate"], installed["keeps_all"]["block"]) == (0, 0)
    assert installed["cuts_whole"]["status"] == "crop_unexpected"
    assert (installed["cuts_whole"]["rate"], installed["cuts_whole"]["block"]) == (0, 0)


def test_the_node_is_a_v3_output_node_showing_its_report(installed):
    node = installed["node"]
    assert (node["id"], node["display"], node["output_node"]) == ("SonderTemplateHelper", "Sonder Template Helper", True)
    assert node["inputs"] == [["vae", "VAE"]]
    # Appended outputs are allowed later (Phase 4); these three keep their order.
    assert node["outputs"] == [["audio_rate", "INT"], ["audio_block", "INT"], ["report", "STRING"]]
    assert node["return_names"] == ["audio_rate", "audio_block", "report"]
    assert node["values"] == [32000, 800] and node["report_is_str"]
    assert node["ui"] == {"text": [node["report"]]} or node["ui"] == {"text": (node["report"],)}
    assert node["off"] == [0, 0]


@pytest.mark.parametrize("key,status,values", [
    ("ltx_2_5_audio", "grid", (44100, 4096)),
    ("ltx_2_3_audio", "grid", (44100, 4096)),
    ("h3_audio", "grid", (32000, 800)),
    ("ace_1_5", "grid", (48000, 1920)),  # no built-in grid: the case the helper exists for
    ("wan_2_1_video", "not_audio", (0, 0)),
    ("sdxl_image", "not_audio", (0, 0)),
])
def test_real_weights_give_their_grid_without_loading_or_running_the_model(installed, key, status, values):
    real = installed["real"].get(key)
    if real is None:
        pytest.skip(f"{key}: weights not found (SONDER_VAE_DIR, models/vae or a Stability Matrix Models/VAE)")
    assert real["status"] == status and (real["rate"], real["block"]) == values, real
    assert real["calls"] == []  # nothing loaded onto a device, encoded or decoded
    if status == "grid":
        # The LTX encoder's own offset is named only for LTX.
        assert ("22 ms" in "\n".join(real["lines"])) == key.startswith("ltx"), real
