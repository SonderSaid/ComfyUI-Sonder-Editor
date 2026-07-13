"""Sonder Guides Bridge — paired loop nodes for variable-length guide injection.

Tail-recursive loop pair using ComfyUI Node Expansion. Start emits one
(image, frame_index, strength) tuple per iteration plus dynamic value_i
passthrough sockets; End decides whether to recurse via GraphBuilder.

Reference: BadCafeCode/execution-inversion-demo-comfyui (`flow_control.py`).
"""

from __future__ import annotations

import logging
import json
import os

import cv2
import numpy as np
import torch

try:
    from comfy_execution.graph_utils import GraphBuilder, is_link
except ImportError:  # pragma: no cover - tests stub these via importorskip
    GraphBuilder = None  # type: ignore[assignment]

    def is_link(value):  # type: ignore[no-redef]
        if not isinstance(value, list) or len(value) != 2:
            return False
        return isinstance(value[0], str) and isinstance(value[1], (int, float))

from ..server.media_helpers import (
    apply_rgb_color_correction,
    color_correction_for_interpretation,
    decode_video_frame,
    fit_frame_to_canvas,
    resolve_source_color_interpretation,
)
from ..server.path_security import resolve_existing_project_path
from ..server.timeline_state import GuideFrame

logger = logging.getLogger(__name__)


def _resolve_max_passthrough() -> int:
    raw = os.environ.get("SONDER_BRIDGE_MAX_PASSTHROUGH", "8") or "8"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 8
    return max(1, min(32, value))


MAX_PASSTHROUGH = _resolve_max_passthrough()
FLOW_SENTINEL = "SONDER_BRIDGE_FLOW"


# ── Wildcard type ─────────────────────────────────────────────────────
class _AnyType(str):
    def __ne__(self, other):  # noqa: D401 — make any type comparison succeed
        return False


_ANY = _AnyType("*")


# ── Guide image loading (mirrors editor_node.py:793) ──────────────────
def _load_guide_image_bridge(path: str, asset_type: str, target_w: int, target_h: int,
                             fit_mode: str = "pad_edge", crop_position: str = "center"):
    try:
        if asset_type == "video":
            interpretation = resolve_source_color_interpretation(None, path)
            frame_rgb = decode_video_frame(path, 0, color_interpretation=interpretation)
            if frame_rgb is None:
                cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
                try:
                    if not cap.isOpened():
                        return None
                    ok, frame_bgr = cap.read()
                    if not ok:
                        return None
                    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                    frame_rgb = apply_rgb_color_correction(
                        frame_rgb, color_correction_for_interpretation(interpretation)
                    )
                finally:
                    cap.release()
        else:
            frame_bgr = cv2.imread(path, cv2.IMREAD_COLOR)
            if frame_bgr is None:
                return None
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        rgb, _bounds = fit_frame_to_canvas(frame_rgb, target_w, target_h,
                                           mode=fit_mode, crop_position=crop_position)
        return torch.from_numpy(rgb.astype(np.float32) / 255.0)
    except Exception as e:
        logger.warning("Sonder bridge: failed to load guide %s: %s", path, e)
        return None


# ── Guide filtering (mirrors editor_node.py:454-492) ──────────────────
def _resolve_active_scene(project):
    ctx = getattr(project, "_execution_context", None) or {}
    scene_id = ctx.get("scene_id", "")
    if scene_id:
        scene = project.get_scene(scene_id)
        if scene is not None:
            return scene
    scenes = getattr(project, "scenes", None) or []
    return scenes[0] if scenes else None


def _resolve_render_window(project, scene):
    proj_w, proj_h = project.resolution
    if scene is not None:
        if getattr(scene, "width", 0) and scene.width > 0:
            proj_w = scene.width
        if getattr(scene, "height", 0) and scene.height > 0:
            proj_h = scene.height
    ctx = getattr(project, "_execution_context", None) or {}
    render_start = ctx.get("context_start")
    render_end = ctx.get("context_end")
    if render_start is None or render_end is None:
        render_start = 0
        render_end = scene.duration_frames if scene is not None else 0
    return int(render_start), int(render_end), int(proj_w), int(proj_h)


def _queue_snapshot_version(queue_job) -> int:
    params = getattr(queue_job, "params", {}) or {}
    if not isinstance(params, dict):
        return 0
    try:
        return max(0, int(params.get("snapshot_version", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _bridge_guide_key(asset_id: str, frame_index: int) -> str:
    return f"{asset_id}:{frame_index}"


def _parse_bridge_overrides(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        return {}
    guides = data.get("guides")
    return guides if isinstance(guides, dict) else data


def _override_muted_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and "muted" in value:
        return bool(value.get("muted"))
    return None


def _apply_bridge_overrides(guides, overrides_json=""):
    overrides = _parse_bridge_overrides(overrides_json)
    out = []
    for guide in guides or []:
        if not isinstance(guide, dict):
            continue
        muted = bool(guide.get("editor_muted", False))
        override_value = _override_muted_value(overrides.get(guide.get("guide_key", "")))
        if override_value is not None:
            muted = override_value
        if muted:
            continue
        next_guide = dict(guide)
        next_guide["effective_muted"] = False
        out.append(next_guide)
    return out


def _filtered_guides(project):
    """List guides in render window, preserving editor mute state for overrides."""
    if project is None:
        return []
    scene = _resolve_active_scene(project)
    if scene is None:
        return []

    render_start, render_end, _proj_w, _proj_h = _resolve_render_window(project, scene)
    if render_end <= render_start:
        return []

    ctx = getattr(project, "_execution_context", None) or {}
    queue_job = None
    # Snapshot readers use the peek+consume reference. queue_job_id is the
    # consume-only completion handle and is deliberately empty on peek runs.
    queue_job_id = ctx.get("queue_job_ref_id", "")
    if queue_job_id:
        for job in getattr(project, "generation_queue", []) or []:
            if getattr(job, "job_id", "") == queue_job_id:
                queue_job = job
                break

    snapshot_version = _queue_snapshot_version(queue_job) if queue_job else 0
    if queue_job and snapshot_version > 0:
        guides_src = []
        for guide_index, guide in enumerate(getattr(queue_job, "guide_frame_snapshots", []) or []):
            if not isinstance(guide, dict):
                continue
            guide_data = dict(guide)
            guide_data["guide_id"] = str(guide_data.get("guide_id") or f"legacy-guide-{guide_index}")
            guides_src.append(GuideFrame.from_dict(guide_data))
        live_guide_track_hidden = False
    else:
        guides_src = list(getattr(scene, "guide_frames", []) or [])
        live_guide_track_hidden = bool(getattr(getattr(scene, "guide_track_config", None), "hidden", False))

    out = []
    for guide in guides_src:
        idx = int(getattr(guide, "frame_index", 0))
        if idx == -1:
            idx = scene.duration_frames - 1
        if not (render_start <= idx < render_end):
            continue
        asset = project.get_asset(getattr(guide, "asset_id", ""))
        if asset is None:
            continue
        asset_path = resolve_existing_project_path(
            project,
            asset.path,
            purpose=f"bridge guide asset {getattr(asset, 'asset_id', '') or '(unknown)'}",
        )
        if not os.path.isfile(asset_path):
            continue
        item = {
            "guide_id": str(getattr(guide, "guide_id", "") or ""),
            "guide_key": _bridge_guide_key(getattr(guide, "asset_id", ""), idx),
            "local_idx": idx - render_start,
            "frame_index": idx,
            "asset_id": getattr(guide, "asset_id", ""),
            "asset_path": asset_path,
            "asset_type": asset.asset_type,
            "strength": float(getattr(guide, "strength", 1.0)),
            "editor_muted": live_guide_track_hidden or bool(getattr(guide, "muted", False)),
            "fit_mode": getattr(guide, "fit_mode", "pad_edge"),
            "crop_position": getattr(guide, "crop_position", "center"),
        }
        manifest = ctx.get("guide_injection") if isinstance(ctx, dict) else None
        if isinstance(manifest, dict) and manifest.get("auto_offset_enabled") is True:
            guide_id = item["guide_id"]
            entry = next(
                (
                    candidate for candidate in (manifest.get("entries") or [])
                    if isinstance(candidate, dict) and str(candidate.get("guide_id") or "") == guide_id
                ),
                None,
            )
            if entry is not None:
                try:
                    item["local_idx"] = int(entry.get("effective_local_idx", item["local_idx"]))
                except (TypeError, ValueError):
                    pass
        out.append(item)
    return out


# ── Loop body collection (BadCafeCode pattern) ────────────────────────
def _explore_dependencies(node_id, dynprompt, upstream):
    node_info = dynprompt.get_node(node_id)
    if not node_info or "inputs" not in node_info:
        return
    for _k, value in node_info["inputs"].items():
        if is_link(value):
            parent_id = value[0]
            if parent_id not in upstream:
                upstream[parent_id] = []
                _explore_dependencies(parent_id, dynprompt, upstream)
            upstream[parent_id].append(node_id)


def _collect_contained(node_id, upstream, contained):
    if node_id not in upstream:
        return
    for child_id in upstream[node_id]:
        if child_id not in contained:
            contained[child_id] = True
            _collect_contained(child_id, upstream, contained)


def _empty_image(proj_w: int, proj_h: int) -> torch.Tensor:
    return torch.zeros(1, max(1, proj_h), max(1, proj_w), 3, dtype=torch.float32)


# ── SonderGuidesBridgeStart ───────────────────────────────────────────
class SonderGuidesBridgeStart:
    """Loop-open node — emits one guide per iteration plus value_i passthrough."""

    CATEGORY = "Sonder"
    FUNCTION = "execute"
    DESCRIPTION = (
        "Loop-open node for variable-length guide injection. Emits one "
        "(image, frame_index, strength) tuple per iteration plus dynamic "
        "value_i passthrough sockets. Pair with Sonder Guides Bridge End."
    )

    RETURN_TYPES = (
        ("FLOW_CONTROL", "IMAGE", "INT", "FLOAT")
        + tuple(_ANY for _ in range(MAX_PASSTHROUGH))
    )
    RETURN_NAMES = (
        ("flow_control", "image", "frame_index", "strength")
        + tuple(f"value_{i}" for i in range(MAX_PASSTHROUGH))
    )

    @classmethod
    def INPUT_TYPES(cls):
        optional = {
            "bridge_overrides_json": ("STRING", {"default": "{}"}),
            **{f"value_{i}": (_ANY,) for i in range(MAX_PASSTHROUGH)},
        }
        return {
            "required": {
                "project": ("SONDER_PROJECT",),
                "iteration_index": ("INT", {"default": 0, "min": 0, "max": 9999}),
            },
            "optional": optional,
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **_kwargs):
        return True

    def execute(self, project, iteration_index=0, **kwargs):
        guides = _apply_bridge_overrides(
            _filtered_guides(project),
            kwargs.get("bridge_overrides_json", "{}"),
        )
        scene = _resolve_active_scene(project)
        _rs, _re, proj_w, proj_h = _resolve_render_window(project, scene)
        passthrough = tuple(kwargs.get(f"value_{i}") for i in range(MAX_PASSTHROUGH))

        try:
            iteration_index = int(iteration_index)
        except (TypeError, ValueError):
            iteration_index = 0

        if not guides or iteration_index < 0 or iteration_index >= len(guides):
            # Empty/out-of-range placeholder: black image with strength 0.0 so downstream
            # guide conditioning treats it as inert. Corrupt-image branch below preserves
            # the configured guide strength (different semantic — real guide exists).
            return (FLOW_SENTINEL, _empty_image(proj_w, proj_h), 0, 0.0) + passthrough

        guide = guides[iteration_index]
        img = _load_guide_image_bridge(
            guide["asset_path"], guide["asset_type"], proj_w, proj_h,
            fit_mode=guide.get("fit_mode", "pad_edge"),
            crop_position=guide.get("crop_position", "center"),
        )
        if img is None:
            return (
                FLOW_SENTINEL,
                _empty_image(proj_w, proj_h),
                int(guide["local_idx"]),
                float(guide["strength"]),
            ) + passthrough

        return (
            FLOW_SENTINEL,
            img.unsqueeze(0),
            int(guide["local_idx"]),
            float(guide["strength"]),
        ) + passthrough


# ── SonderGuidesBridgeEnd ─────────────────────────────────────────────
class SonderGuidesBridgeEnd:
    """Loop-close node — recurses via GraphBuilder or returns final value_i."""

    CATEGORY = "Sonder"
    FUNCTION = "execute"
    DESCRIPTION = (
        "Loop-close node. Reads guide list from the paired Start, recurses via "
        "Node Expansion when more guides remain, or returns the loop-carried "
        "value_i passthrough on the final iteration."
    )

    RETURN_TYPES = tuple(_ANY for _ in range(MAX_PASSTHROUGH))
    RETURN_NAMES = tuple(f"value_{i}" for i in range(MAX_PASSTHROUGH))

    @classmethod
    def INPUT_TYPES(cls):
        optional = {f"value_{i}": (_ANY,) for i in range(MAX_PASSTHROUGH)}
        return {
            "required": {
                "project": ("SONDER_PROJECT",),
                "flow_control": ("FLOW_CONTROL", {"rawLink": True}),
            },
            "optional": optional,
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **_kwargs):
        return True

    def _kwargs_passthrough(self, kwargs):
        return tuple(kwargs.get(f"value_{i}") for i in range(MAX_PASSTHROUGH))

    def _start_input_passthrough(self, start_inputs):
        return tuple(start_inputs.get(f"value_{i}") for i in range(MAX_PASSTHROUGH))

    def execute(self, flow_control, project=None, dynprompt=None, unique_id=None, **kwargs):
        # Defensive: malformed flow_control or missing dynprompt → pass body kwargs.
        if not isinstance(flow_control, list) or len(flow_control) < 2 or dynprompt is None:
            return self._kwargs_passthrough(kwargs)

        start_node_id = flow_control[0]
        start_node = dynprompt.get_node(start_node_id)
        if not start_node or start_node.get("class_type") != "SonderGuidesBridgeStart":
            return self._kwargs_passthrough(kwargs)
        start_inputs = start_node.get("inputs", {}) or {}

        guides = _apply_bridge_overrides(
            _filtered_guides(project) if project is not None else [],
            start_inputs.get("bridge_overrides_json", "{}"),
        )

        if not guides:
            result = self._start_input_passthrough(start_inputs)
            if GraphBuilder is None:
                return result
            return {"result": result, "expand": GraphBuilder().finalize()}

        try:
            current_iter = int(start_inputs.get("iteration_index", 0))
        except (TypeError, ValueError):
            current_iter = 0

        if current_iter + 1 >= len(guides):
            return self._kwargs_passthrough(kwargs)

        if GraphBuilder is None:
            return self._kwargs_passthrough(kwargs)

        # Mid-loop: clone {start, body, close} via GraphBuilder.
        upstream: dict = {}
        _explore_dependencies(unique_id, dynprompt, upstream)
        contained: dict = {}
        _collect_contained(start_node_id, upstream, contained)
        contained[unique_id] = True
        contained[start_node_id] = True

        graph = GraphBuilder()
        for nid in list(contained.keys()):
            original = dynprompt.get_node(nid)
            if not original:
                continue
            new_id = "Recurse" if nid == unique_id else nid
            node = graph.node(original["class_type"], new_id)
            node.set_override_display_id(nid)

        for nid in list(contained.keys()):
            original = dynprompt.get_node(nid)
            if not original:
                continue
            new_id = "Recurse" if nid == unique_id else nid
            node = graph.lookup_node(new_id)
            for k, v in (original.get("inputs", {}) or {}).items():
                if is_link(v) and v[0] in contained:
                    parent_id = v[0]
                    parent_clone_id = "Recurse" if parent_id == unique_id else parent_id
                    parent_clone = graph.lookup_node(parent_clone_id)
                    if parent_clone is not None:
                        node.set_input(k, parent_clone.out(v[1]))
                        continue
                node.set_input(k, v)

        new_open = graph.lookup_node(start_node_id)
        if new_open is not None:
            new_open.set_input("iteration_index", current_iter + 1)
            if "bridge_overrides_json" in start_inputs:
                new_open.set_input("bridge_overrides_json", start_inputs.get("bridge_overrides_json"))
            for i in range(MAX_PASSTHROUGH):
                key = f"value_{i}"
                if key in kwargs:
                    new_open.set_input(key, kwargs.get(key))

        new_close = graph.lookup_node("Recurse")
        result = tuple(
            new_close.out(i) if new_close is not None else kwargs.get(f"value_{i}")
            for i in range(MAX_PASSTHROUGH)
        )
        return {"result": result, "expand": graph.finalize()}
