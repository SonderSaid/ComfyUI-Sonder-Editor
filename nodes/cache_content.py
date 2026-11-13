"""Content identity for the Editor's media outputs (cache pipeline, ``CONTENT`` policy).

The Editor's ``rendered_frames``, ``guide_images`` and ``audio`` carry its NaN
fingerprint, so every node fed by them misses ComfyUI's cache. Once the Editor
has run early (``server/cache_pipeline.py``), its media exists before cache keys
do. ``digest`` hashes a materialized value; ``token`` turns that digest into the
string an ephemeral carrier (``nodes/media_carrier.py``) is keyed by, so a
consumer of unchanged media hits ComfyUI's cache.

The digest covers the socket type, the dtype, the shape and every byte, in
order. Bytes, not values: ``0.0`` and ``-0.0``, or two NaN payloads, differ,
which can only cause a miss. **A miss is acceptable; a false hit never is.**

``digest`` is pure and never raises for a value it does not support: it returns
None, which means "no carrier for this slot". Frames are hashed from the
tensor's own memory, one frame at a time, on a pool created for the call, with
the caller's interrupt check between batches; a non-contiguous frame is copied
one frame at a time and the whole tensor never is.
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

TOKEN_PREFIX = "sonder-media:v1:"
CONTENT_TYPES = ("IMAGE", "AUDIO")
MAX_WORKERS = 8
BATCH = 16  # frames hashed between two interrupt checks
# Per process: a token cannot be guessed, and an external cache provider can
# never match a token from another run of ComfyUI.
_SALT = os.urandom(32)


def is_token(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(TOKEN_PREFIX)


def token(digest_hex: str) -> str:
    """The carrier token for a content digest: salted per process."""
    return TOKEN_PREFIX + hashlib.sha256(_SALT + bytes.fromhex(digest_hex)).hexdigest()


def _torch():
    try:
        import torch
    except Exception:  # pragma: no cover - ComfyUI always ships torch
        return None
    return torch


def _plain_tensor(torch: Any, value: Any) -> bool:
    """A dense CPU tensor whose bytes can be read in place."""
    return (isinstance(value, torch.Tensor) and value.device.type == "cpu" and value.layout == torch.strided
            and not value.is_quantized and value.ndim >= 1)


def _frame_bytes(torch: Any, frame: Any) -> memoryview:
    """``frame``'s bytes in logical order. A non-contiguous frame is copied (one frame)."""
    flat = frame.detach().reshape(-1)
    if not flat.is_contiguous():
        flat = flat.contiguous()
    if flat.numel() == 0:
        return memoryview(b"")
    return memoryview(flat.view(torch.uint8).numpy()).cast("B")


def _tensor_digest(torch: Any, tensor: Any, check: Callable[[], None] | None) -> bytes:
    """sha256 over each frame along dim 0, in order; independent of worker count and batch size."""
    count = int(tensor.shape[0])

    def one(index: int) -> bytes:
        return hashlib.sha256(_frame_bytes(torch, tensor[index])).digest()

    parts: list[bytes] = []
    if count <= 1:
        if check is not None:
            check()
        parts = [one(index) for index in range(count)]
    else:
        workers = max(1, min(MAX_WORKERS, os.cpu_count() or 1, count))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sonder-media-digest") as pool:
            for start in range(0, count, BATCH):
                if check is not None:
                    check()  # on the calling thread: an interrupt raised here propagates
                parts.extend(pool.map(one, range(start, min(count, start + BATCH))))
    combined = hashlib.sha256()
    for part in parts:
        combined.update(part)
    return combined.digest()


def _header(*fields: Any) -> bytes:
    return "\x1f".join(str(field) for field in fields).encode("utf-8")


def digest(socket_type: str, value: Any, check: Callable[[], None] | None = None) -> str | None:
    """Hex sha256 of ``value``'s type, dtype, shape and bytes, or None when unsupported.

    ``IMAGE``: a dense CPU tensor. ``AUDIO``: a dict with exactly ``waveform`` (a
    dense CPU tensor) and ``sample_rate`` (exactly ``int``). ``check`` is called
    between batches; whatever it raises propagates. Nothing else raises.
    """
    torch = _torch()
    if torch is None:
        return None
    if socket_type == "IMAGE":
        tensor = value
        header = _header("IMAGE")
    elif socket_type == "AUDIO":
        if not isinstance(value, dict) or set(value) != {"waveform", "sample_rate"}:
            return None
        rate = value["sample_rate"]
        if type(rate) is not int:
            return None
        tensor = value["waveform"]
        header = _header("AUDIO", rate)
    else:
        return None
    if not _plain_tensor(torch, tensor):
        return None
    header = header + b"\x1e" + _header(str(tensor.dtype), tuple(int(size) for size in tensor.shape))
    try:
        body = _tensor_digest(torch, tensor, check)
    except (TypeError, ValueError, RuntimeError):
        # A dtype numpy cannot view (it should not happen through view(uint8)) or a
        # tensor torch refuses to expose: no identity, never a guess.
        return None
    return hashlib.sha256(header + b"\x1d" + body).hexdigest()


def payload_bytes(socket_type: str, value: Any) -> int:
    """How many bytes ``digest`` reads for ``value`` (for the preparation record)."""
    torch = _torch()
    tensor = value.get("waveform") if socket_type == "AUDIO" and isinstance(value, dict) else value
    if torch is None or not isinstance(tensor, torch.Tensor):
        return 0
    return int(tensor.numel()) * int(tensor.element_size())
