"""Live sampling previews for Output Views (Phase 4): attribution and normalization.

Two publishers, both seen on the one ``send_sync`` wrap (``sdk_progress``):

* ComfyUI's native sampler preview. ``WebUIProgressHandler`` sends event 4
  (``PREVIEW_IMAGE_WITH_METADATA``) with the prompt and node in its metadata
  when the prompt's client advertised ``supports_preview_metadata``; otherwise
  the progress hook sends event 2 (``UNENCODED_PREVIEW_IMAGE``) with the image
  alone. Exactly one of the two per preview, on the sampler's thread, carrying
  an unencoded PIL image.
* KJNodes Model Preview Override's ``kj_preview_override`` text event. It names
  the Override node (a MODEL wrapper, not the sampler) and carries no prompt id;
  per-step payloads are sent from KJ's encoder thread, which
  ``kj_preview_adapter`` makes run in the submitting sampler's context.

Attribution never guesses: a preview belongs to a prompt only through event 4's
metadata or ComfyUI's per-node ``ExecutionContext``, and a KJ source must sit on
the sampler's model chain. ComfyUI leaves a V3 node's id out of its cache
signature, so a cached Override wrapper can carry another, identical node's id.
The global client id, ``last_node_id`` and ``last_prompt_id`` are never used.

Everything here runs on the sending thread and stays O(1) per event: payloads
are kept by reference (the PIL image, the base64 text) and encoded or decoded
only when a pane asks (``encode_payload``), off the event loop. The one walk, a
sampler's upstream Override nodes, runs once per (prompt, sampler).
"""
from __future__ import annotations

import base64
import io
import logging
import threading
import time
from typing import Any

from . import kj_preview_adapter, run_outputs, sdk_progress, sdk_runs
from .prompt_graph import linked_node_id

logger = logging.getLogger(__name__)

KJ_EVENT = "kj_preview_override"
KJ_CLASS = kj_preview_adapter.KJ_CLASS
KJ_MIMES = frozenset({"image/jpeg", "image/webp", "video/mp4"})
NATIVE_MIMES = {"JPEG": "image/jpeg", "PNG": "image/png"}
# Every MIME a payload can carry; the bytes route serves only these.
SERVED_MIMES = KJ_MIMES | frozenset(NATIVE_MIMES.values())
# The inputs a model chain runs through: a sampler's model (or guider) and, upstream,
# each patch node's model. A latent or image link is another chain (a second pass).
MODEL_TYPES = frozenset({"MODEL", "GUIDER"})
# A passthrough reached through a model link carries the model on: a switch's
# "*" inputs, a V3 MatchType or an input that takes several types including MODEL.
GENERIC_TYPES = frozenset({"*", "COMFY_MATCHTYPE_V3"})
MAX_INPUT_TYPE_CLASSES = 2048
# Base64 characters kept per payload; a larger one records ``too_large`` only.
MAX_PAYLOAD_CHARS = 16 * 1024 * 1024
# KJ's encoder drains for up to 5 s after sampling returns; its late payloads
# still belong to their run. Expiry: drop the grace when KJ's shutdown joins
# its worker before returning.
FINISH_GRACE_SEC = 10.0

_LOCK = threading.Lock()
_INSTALLED = False
_EVENT_WITH_METADATA = 4
_EVENT_UNENCODED = 2
# Diagnostics only: previews dropped because their run could not be named.
COUNTERS = {"unattributed_native": 0, "unattributed_kj": 0, "kj_ambiguous": 0, "foreign_source": 0,
            "context_mismatch": 0}
_INPUT_TYPES: dict[str, dict | None] = {}


def _class_lookup(class_type: str):
    # The descriptors' lookup (ComfyUI's NODE_CLASS_MAPPINGS): one authority.
    from .control_descriptors import comfy_class_lookup
    return comfy_class_lookup(class_type)


def install() -> bool:
    """Register on the ``send_sync`` wrap. Idempotent; never raises."""
    global _INSTALLED, _EVENT_WITH_METADATA, _EVENT_UNENCODED
    try:
        try:
            from protocol import BinaryEventTypes
            _EVENT_WITH_METADATA = int(BinaryEventTypes.PREVIEW_IMAGE_WITH_METADATA)
            _EVENT_UNENCODED = int(BinaryEventTypes.UNENCODED_PREVIEW_IMAGE)
            events = {_EVENT_WITH_METADATA, _EVENT_UNENCODED, KJ_EVENT}
        except Exception:
            # Without ComfyUI's protocol module only the KJ text event is known.
            events = {KJ_EVENT}
        sdk_progress.add_preview_observer(observe_preview, events)
        sdk_progress.add_observer(observe_event)
        _INSTALLED = True
        return True
    except Exception:
        logger.warning("Sampling preview observation unavailable", exc_info=True)
        return False


def capability() -> dict[str, Any]:
    """What a pane can expect from this ComfyUI. Expiry: ``kj`` goes with the adapter."""
    return {"native": _INSTALLED and sdk_progress.is_installed(), "kj": kj_preview_adapter.status()}


def _bump(name: str) -> None:
    with _LOCK:
        COUNTERS[name] = COUNTERS.get(name, 0) + 1


def _context():
    try:
        from comfy_execution.utils import get_executing_context
        return get_executing_context()
    except Exception:
        return None


def _registry_for(prompt_id: str):
    """ComfyUI's progress registry, only while it is this prompt's."""
    try:
        from comfy_execution.progress import get_progress_state
        registry = get_progress_state()
    except Exception:
        return None
    return registry if str(getattr(registry, "prompt_id", "") or "") == prompt_id else None


def _display_id(prompt_id: str, registry, node_id: str) -> str:
    """The prompt key a runtime id stands for (expansion children map to their parent).

    Read from ComfyUI's registry while it is this prompt's, and remembered for the
    run, so a late KJ payload (the next prompt owns the registry by then) maps the
    same way. Expiry: needed while event 2 and KJ's event carry no display id.
    """
    if not node_id:
        return node_id
    if registry is None:
        return run_outputs.live_display_id(prompt_id, node_id) or node_id
    try:
        display = registry.dynprompt.get_display_node_id(node_id)
    except Exception:
        return node_id
    display = str(display) if display not in (None, "") else node_id
    if display != node_id:
        run_outputs.note_live_display_id(prompt_id, node_id, display)
    return display


def _progress_of(registry, node_id: str) -> tuple[int | None, int | None]:
    """The step ComfyUI's registry recorded for the node just before this preview."""
    try:
        entry = registry.nodes.get(node_id) if registry is not None else None
        if entry is None:
            return None, None
        return int(entry["value"]), int(entry["max"])
    except Exception:
        return None, None


def _window_open(prompt_id: str) -> dict | None:
    """The bound run, while it runs or within the finish grace; None otherwise."""
    bound = sdk_runs.bound_run(prompt_id)
    if bound is None or bound["outcome"] == sdk_runs.OUTCOME_REMOVED:
        return None
    if bound["finished"] and time.time() - float(bound.get("finished_at") or 0.0) > FINISH_GRACE_SEC:
        return None
    return bound


def _source_info(prompt_id: str, source: str) -> dict | None:
    """``{class_type, title}`` of a source, captured on its first record (the prompt
    is gone once the run finishes). None when the source is no key of the prompt."""
    known = run_outputs.live_source_info(prompt_id, source)
    if known is not None:
        return known or None
    prompt = sdk_runs.bound_prompt(prompt_id)
    if not isinstance(prompt, dict):
        return None
    node = prompt.get(source)
    if not isinstance(node, dict):
        run_outputs.note_live_source_info(prompt_id, source, {})
        return None
    meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    title = meta.get("title") if isinstance(meta.get("title"), str) else ""
    info = {"class_type": str(node.get("class_type") or "")[:256], "title": (title or "")[:128]}
    run_outputs.note_live_source_info(prompt_id, source, info)
    return info


def _only_override(prompt_id: str) -> str | None:
    """The prompt's one ``ModelPreviewOverrideKJ`` key, or None (none, several, or no prompt)."""
    prompt = sdk_runs.bound_prompt(prompt_id)
    if not isinstance(prompt, dict):
        return None
    keys = [key for key, node in prompt.items() if isinstance(node, dict) and node.get("class_type") == KJ_CLASS]
    return keys[0] if len(keys) == 1 else None


def _kj_chain(prompt_id: str, via: str) -> frozenset | None:
    """The Override nodes upstream of a sampler, one walk per (prompt, sampler)."""
    known = run_outputs.live_kj_chain(prompt_id, via)
    if known is not None:
        return known
    prompt = sdk_runs.bound_prompt(prompt_id)
    if not isinstance(prompt, dict):
        return None
    upstream = _model_upstream(prompt, via)
    chain = frozenset(key for key in upstream
                      if isinstance(prompt.get(key), dict) and prompt[key].get("class_type") == KJ_CLASS)
    run_outputs.note_live_kj_chain(prompt_id, via, chain)
    return chain


def _input_types(class_type: Any) -> dict | None:
    """``{input name: declared type}`` of a node class, once per class; None when unknown."""
    if not isinstance(class_type, str):
        return None
    if class_type in _INPUT_TYPES:
        return _INPUT_TYPES[class_type]
    types: dict | None = None
    try:
        node_class = _class_lookup(class_type)
        spec = node_class.INPUT_TYPES() if node_class is not None else None
        if isinstance(spec, dict):
            types = {}
            for section in ("required", "optional"):
                for name, value in (spec.get(section) or {}).items():
                    declared = value[0] if isinstance(value, (list, tuple)) and value else value
                    types[str(name)] = declared if isinstance(declared, str) else None
    except Exception:
        types = None
    with _LOCK:
        if len(_INPUT_TYPES) >= MAX_INPUT_TYPE_CLASSES:
            _INPUT_TYPES.clear()
        _INPUT_TYPES[class_type] = types
    return types


def _carries_model(declared: Any) -> bool:
    """Whether a linked input of this declared type can be the model chain."""
    if declared is None:
        return True  # not a plain type name: unknown, followed
    if declared in MODEL_TYPES or declared in GENERIC_TYPES:
        return True
    return any(part.strip() in MODEL_TYPES for part in declared.split(","))


def _model_upstream(prompt: dict, via: str) -> set[str]:
    """Prompt keys on the sampler's model chain: only MODEL/GUIDER links are followed.

    A second-pass sampler takes its latent from the first pass, so following every
    link would put the first pass's Override node on its chain too. A class whose
    inputs can't be read is followed through every link (never narrower than the
    graph). A node with no links is never asked for its inputs (loaders list files).
    """
    seen: set[str] = set()
    stack = [via] if via in prompt else []
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        node = prompt.get(current)
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        links = [(name, linked_node_id(value)) for name, value in inputs.items()]
        links = [(name, upstream) for name, upstream in links if upstream]
        if not links:
            continue
        types = _input_types(node.get("class_type"))
        for name, upstream in links:
            if types is not None and not _carries_model(types.get(str(name))):
                continue
            if upstream not in seen:
                stack.append(upstream)
    return seen


# --- Observation (sending thread) ----------------------------------------------------

def observe_preview(event: Any, data: Any) -> None:
    if event == KJ_EVENT:
        _observe_kj(data)
    elif event == _EVENT_WITH_METADATA or event == _EVENT_UNENCODED:
        _observe_native(event, data)


def _image_of(value: Any):
    """``(format, PIL.Image, max_size)`` as ComfyUI's previewers build it, or None."""
    if not (isinstance(value, tuple) and len(value) == 3 and value[0] in NATIVE_MIMES):
        return None
    image = value[1]
    if not hasattr(image, "size") or not hasattr(image, "getbands"):
        return None
    return value


def _contained(width: int, height: int, max_size: Any) -> tuple[int, int]:
    """The size ``ImageOps.contain(image, (max_size, max_size))`` gives, as PIL computes it.

    It scales up as well as down, so a small preview is served at ``max_size``.
    """
    if not isinstance(max_size, int) or max_size <= 0 or width <= 0 or height <= 0:
        return width, height
    if width > height:
        return max_size, round(height / width * max_size)
    if width < height:
        return round(width / height * max_size), max_size
    return max_size, max_size


def _observe_native(event: Any, data: Any) -> None:
    if event == _EVENT_WITH_METADATA:
        if not (isinstance(data, tuple) and len(data) == 2 and isinstance(data[1], dict)):
            return
        image_tuple, meta = _image_of(data[0]), data[1]
        prompt_id = str(meta.get("prompt_id") or "")
        node = str(meta.get("node_id") or "")
        context = _context()
        if context is not None and str(context.prompt_id or "") != prompt_id:
            # ComfyUI fills the metadata from the registry: a thread of another prompt
            # would land on the current one. Event 2 is dropped the same way.
            _bump("context_mismatch")
            return
        registry = _registry_for(prompt_id)
        source = str(meta.get("display_node_id") or "") or _display_id(prompt_id, registry, node)
    else:
        image_tuple = _image_of(data)
        context = _context()
        if context is None:
            _bump("unattributed_native")
            return
        prompt_id, node = str(context.prompt_id or ""), str(context.node_id or "")
        registry = _registry_for(prompt_id)
        source = _display_id(prompt_id, registry, node)
    if image_tuple is None or not prompt_id or not source:
        return
    bound = _window_open(prompt_id)
    if bound is None:
        return
    info = _source_info(prompt_id, source)
    if info is None:
        _bump("foreign_source")
        return
    fmt, image, max_size = image_tuple
    width, height = _contained(int(image.size[0]), int(image.size[1]), max_size)
    step, total = _progress_of(registry, node)
    record = {"publisher": "native", "via": source, "step": step, "total": total,
              "mime": NATIVE_MIMES[fmt], "w": width, "h": height, "fps": None, **info}
    size = int(image.size[0]) * int(image.size[1]) * max(1, len(image.getbands()))
    run_outputs.record_live(bound, source, record, ("pil", image_tuple), size)


def _observe_kj(data: Any) -> None:
    if not isinstance(data, dict):
        return
    context = _context()
    if context is None:
        _bump("unattributed_kj")
        # Only while a run Sonder observes executes: a stray event from another
        # thread or an unbound client says nothing about the runs panes show.
        if sdk_runs.any_bound_started():
            kj_preview_adapter.note_unattributed()
        return
    kj_preview_adapter.note_attributed()
    prompt_id = str(context.prompt_id or "")
    named = data.get("prompt_id")
    if isinstance(named, str) and named and named != prompt_id:
        # Expiry hook: should KJ add a prompt id, it decides, and the adapter can go.
        prompt_id = named
    bound = _window_open(prompt_id)
    if bound is None:
        return
    registry = _registry_for(prompt_id)
    via = _display_id(prompt_id, registry, str(context.node_id or ""))
    raw = _display_id(prompt_id, registry, str(data.get("node_id") or ""))
    chain = _kj_chain(prompt_id, via)
    reattributed = False
    if chain is not None and raw in chain:
        source = raw
    elif chain is not None and len(chain) == 1:
        # A cached wrapper named another, identical Override node: the sampler's
        # own model chain holds exactly one, so the preview is that one's.
        source, reattributed = next(iter(chain)), True
    elif chain is None and run_outputs.live_source_info(prompt_id, raw):
        source = raw  # finished run in its grace: the verdict was taken on its first record
    elif chain is not None and not chain and _only_override(prompt_id) == raw:
        # The walk found none (a passthrough it can't read), but the prompt holds
        # exactly one Override node: no other node's wrapper can be reused.
        source = raw
    else:
        _bump("kj_ambiguous")
        return
    info = _source_info(prompt_id, source)
    if info is None:
        _bump("foreign_source")
        return
    image = data.get("image")
    mime = data.get("mime") or ("image/jpeg" if image else None)
    state, payload, size = "ok", None, 0
    if isinstance(image, str) and image:
        if mime not in KJ_MIMES:
            state = "unsupported"
        elif len(image) > MAX_PAYLOAD_CHARS:
            state = "too_large"
        else:
            payload, size = ("b64", image), len(image)
    elif image is None and int(_number(data.get("step")) or 0) == 0:
        state = "starting"  # the boundary-0 message without a noise still
    else:
        state = "unsupported"
    record = {"publisher": "kj", "via": via, "reattributed": reattributed,
              "step": _number(data.get("step")), "total": _number(data.get("total")),
              "mime": mime if isinstance(mime, str) else None, "w": _number(data.get("w")), "h": _number(data.get("h")),
              "fps": _number(data.get("fps")), "state": state, **info}
    run_outputs.record_live(bound, source, record, payload, size)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def observe_event(event: str, data: Any) -> None:
    """``execution_start``: the preview method ComfyUI just set for this prompt.

    Expiry: drop it when ComfyUI reports a prompt's preview method in its own events.
    """
    if event != "execution_start" or not isinstance(data, dict):
        return
    prompt_id = str(data.get("prompt_id") or "")
    if not prompt_id or not sdk_runs.is_observing(prompt_id):
        return
    try:
        from comfy.cli_args import args
        method = getattr(args.preview_method, "value", args.preview_method)
    except Exception:
        return
    run_outputs.note_preview_method(prompt_id, str(method or ""))


# --- Bytes (off the event loop) --------------------------------------------------------

def encode_payload(payload: tuple) -> bytes | None:
    """The bytes a pane shows: a native image encoded as ComfyUI would, or KJ's decoded base64."""
    kind, value = payload
    if kind == "bytes":
        return value
    if kind == "b64":
        try:
            return base64.b64decode(value, validate=True)
        except Exception:
            return None
    if kind == "pil":
        fmt, image, max_size = value
        try:
            from PIL import Image, ImageOps
            if isinstance(max_size, int) and max_size > 0:
                resampling = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.LANCZOS
                image = ImageOps.contain(image, (max_size, max_size), resampling)
            buffer = io.BytesIO()
            image.save(buffer, format=fmt, quality=95, compress_level=1)
            return buffer.getvalue()
        except Exception:
            logger.warning("Encoding a sampling preview failed", exc_info=True)
            return None
    return None


def reset_for_tests() -> None:
    global _INSTALLED
    with _LOCK:
        for name in COUNTERS:
            COUNTERS[name] = 0
        _INPUT_TYPES.clear()
    _INSTALLED = False
