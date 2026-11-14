"""The Editor's render and audio mix as recipes, so their identity exists before they run (Phase 4b).

The Editor's ``rendered_frames`` and ``audio`` are ``DESCRIPTOR`` outputs it
defers (``SONDER_CACHE_DEFERRED``, ``nodes/cache_policy.py``). ``_execute_once``
splits each into two halves:

- **resolve** (``frames_plan``, ``audio_plan``): reads the live project and
  scene once and builds a **recipe**: every value the renderer or mixer reads
  that is not file content (raster, rate, window, padding, each visible clip's
  geometry, opacity and fit, each source's resolved path, asset rate, length
  and colour interpretation; each audible track's path, volume and placement,
  the ffmpeg and ffprobe paths) plus ``sources``, the files it reads. A value
  that is not exactly the type the renderer expects (a fractional frame, a
  ``None``, a mistyped field) gives the slot **no recipe**: it renders as
  today, without an identity. Conversions are exact-equivalent only (an
  integral float frame is the same frame; an integer opacity is the same
  opacity), never truncation.
- **materialize** (``PendingMedia.value``): renders from the canonical recipe
  JSON and the files it names, through the same renderer and mixer as ever
  (``RenderRequest``, the mixer's ``request``). The live project and scene are
  never read again, so a later change to them cannot reach a late render.

The identity (``PendingMedia.identify``) hashes the canonical recipe with the
full sha256 of every source, and ``cache_content.token`` salts it per process
over a domain-separated digest, so it can never equal a content token. A
materialization with an identity runs inside ``cache_identity.reading``: a
source that moved since its hash fails the run (``SourceChanged``) and caches
nothing. **A miss is acceptable; a false hit never is.**

Without deferral (``--cache-none``, a failed gate, no hook, the stock path)
``_execute_once`` materializes every demanded slot where it always rendered,
in the same order. With deferral (only the cache pipeline's early run asks, via
``deferring``) each slot with a recipe is left as ``DEFERRED`` in the Editor's
memo and ``PendingMedia`` is kept on the run record; the pipeline settles it
before any node runs, and two safety nets (``settle_scheduled`` at the
scheduled Editor, ``settle_carrier`` at an executing carrier) settle whatever
is left, so no node ever receives the marker.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cache_content, cache_identity
from .prompt_once import call_key as _call_key
from ..server import sdk_runs

logger = logging.getLogger(__name__)

RECIPE_VERSION = 1
DOMAIN = b"sonder-editor-media:v1\0"
MAX_WORKERS = 8
# Every prompt hashes every source a slot names, even on a hit (step 0, kit
# phase4b-2026-11-13: 217 MB/s sustained cold on the HDD test disk). Past these
# the cold hash would cost more than half of what a hit saves (a render from a
# warm render cache, 1.4 s; uncached, 7.2 s; a mix, 0.9 s), so the slot keeps
# Phase 4a's path: rendered, then carried by a digest of its content.
FRAMES_MAX_BYTES = 160 * 1024 * 1024
FRAMES_MAX_BYTES_UNCACHED = 512 * 1024 * 1024
AUDIO_MAX_BYTES = 96 * 1024 * 1024


class _Deferred:
    """The marker a deferred slot holds in the Editor's memo until it is settled."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<deferred Sonder Editor media>"


DEFERRED = _Deferred()


def is_deferred(value: Any) -> bool:
    return value is DEFERRED


# --- Deferral: only the cache pipeline's early run asks for it ------------------------

_DEFERRING: contextvars.ContextVar[str] = contextvars.ContextVar("sonder_editor_media_deferring", default="")


@contextlib.contextmanager
def deferring(call_key: str):
    """Within this block, the Editor call ``call_key`` defers the slots it can identify."""
    token = _DEFERRING.set(str(call_key or ""))
    try:
        yield
    finally:
        _DEFERRING.reset(token)


def deferral_requested(unique_id: Any) -> bool:
    wanted = _DEFERRING.get()
    return bool(wanted) and _call_key(unique_id) == wanted


# --- Exact values -------------------------------------------------------------------


class _Inexact(Exception):
    """A value the renderer reads raw is not exactly the type a recipe can hold."""


def _exact_int(value: Any) -> int:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)  # the same frame: every comparison and sum gives the same result
    raise _Inexact(repr(value))


def _exact_float(value: Any) -> float:
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)  # the renderer only compares it and blends with float(value)
    raise _Inexact(repr(value))


def _exact_str(value: Any) -> str:
    if type(value) is str:
        return value
    raise _Inexact(repr(value))


# --- Plans --------------------------------------------------------------------------


@dataclass
class MediaPlan:
    """One deferred-able slot: its recipe (or why none), the stock request, and its materializer.

    ``materialize(request)`` is the Editor's render or mix of ``request``.
    ``request`` is the detached snapshot used when there is no recipe; with a
    recipe, the request is always rebuilt from the recipe's canonical JSON.
    """

    kind: str
    request: Any
    recipe: dict | None
    reason: str
    materialize: Callable[[Any], Any]
    expect: Callable[[Any], bool]
    max_bytes: int
    recipe_json: str = ""

    def render_request(self) -> Any:
        if self.recipe is None:
            return self.request
        return (_frames_request_from_recipe if self.kind == "frames" else _audio_request_from_recipe)(
            json.loads(self.recipe_json))


def _finish(plan: MediaPlan) -> MediaPlan:
    if plan.recipe is not None:
        try:
            plan.recipe_json = cache_identity.canonical(plan.recipe)
        except cache_identity.RecipeNotCanonical:
            plan.recipe, plan.reason = None, "recipe_not_json"
    return plan


def frames_plan(project: Any, scene: Any, start: int, end: int, *, use_cache: bool, padding_target: int,
                materialize: Callable[[Any], Any]) -> MediaPlan:
    """The render of ``[start, end)``: its request, its recipe when every value is exact, its materializer."""
    from ..server import render_cache, timeline_renderer as renderer
    from ..server.timeline_state import effective_scene_fps

    try:
        width, height = renderer._scene_resolution(project, scene)
        scene_fps = float(effective_scene_fps(project, scene))
        duration = max(0, int(getattr(scene, "duration_frames", 0) or 0))
        scene_id = str(getattr(scene, "scene_id", "") or "")
        visible = renderer._visible_render_clips(scene)
        store = str(getattr(project, "project_dir", "") or "") if use_cache else ""
    except Exception as exc:  # a scene the renderer would read some other way: render it as today
        logger.debug("Editor render has no request: %s", type(exc).__name__)
        return MediaPlan("frames", None, None, "request_unavailable", materialize, lambda _value: False, 0)
    render_start = max(0, int(start or 0))
    render_end = max(render_start, int(end or render_start))
    if duration > 0:
        render_start = min(render_start, duration)
        render_end = min(max(render_start, render_end), duration)
    count = render_end - render_start
    coverage = (render_start, render_end)
    block = 0
    if use_cache and count > 0:
        block = render_cache.block_frame_count(width, height)
        scene_duration = max(render_end, duration)
        coverage = ((render_start // block) * block, min(scene_duration, ((render_end - 1) // block + 1) * block))

    records, reason = [], ""
    for clip in visible:
        try:
            records.append(renderer.RenderClip(
                timeline_start_frame=_exact_int(clip.timeline_start_frame),
                timeline_end_frame=_exact_int(clip.timeline_end_frame),
                source_in_frame=_exact_int(clip.source_in_frame),
                track_index=_exact_int(clip.track_index),
                opacity=_exact_float(clip.opacity),
                fit_mode=_exact_str(clip.fit_mode),
                crop_position=_exact_str(clip.crop_position),
                source_path=_exact_str(clip.source_path)))
        except (_Inexact, AttributeError):
            reason = "recipe_inexact"
            break
    if reason:
        # Raw values, every visible clip: the render is exactly today's, with no identity.
        clips = tuple(renderer.RenderClip(
            timeline_start_frame=getattr(clip, "timeline_start_frame", 0),
            timeline_end_frame=getattr(clip, "timeline_end_frame", 0),
            source_in_frame=getattr(clip, "source_in_frame", 0),
            track_index=getattr(clip, "track_index", 0),
            opacity=getattr(clip, "opacity", 1.0),
            fit_mode=getattr(clip, "fit_mode", "pad_edge"),
            crop_position=getattr(clip, "crop_position", "center"),
            source_path=str(getattr(clip, "source_path", "") or "")) for clip in visible)
    else:
        clips = tuple(record for record in records
                      if record.timeline_start_frame < coverage[1] and record.timeline_end_frame > coverage[0])
    try:
        prepared = renderer._prepare_sources(project, scene, list(clips))
    except Exception as exc:
        logger.debug("Editor render sources unavailable: %s", type(exc).__name__)
        return MediaPlan("frames", None, None, "request_unavailable", materialize, lambda _value: False, 0)
    sources = {key: renderer.RenderSource(
        abs_path=source.abs_path, source_fps=float(source.source_fps),
        native_frame_count=int(source.native_frame_count),
        interpretation=tuple(source.interpretation) if source.interpretation is not None else None)
        for key, source in prepared.items()}
    request = renderer.RenderRequest(width=width, height=height, scene_fps=scene_fps, duration=duration,
                                     scene_id=scene_id, clips=clips, sources=sources, store=store,
                                     start=render_start, end=render_end, use_cache=bool(use_cache),
                                     padding_target=int(padding_target or 0))
    expected_count = max(count, int(padding_target or 0)) if count > 0 else max(1, int(padding_target or 0))

    def expect(value: Any) -> bool:
        return _plain_frames(value, (expected_count, height, width, 3))

    recipe = None
    if not reason:
        if not all(type(s.interpretation) is tuple and all(type(part) is str for part in s.interpretation)
                   for s in sources.values() if s.interpretation is not None):
            reason = "recipe_inexact"
        else:
            recipe = {
                "kind": "frames", "version": RECIPE_VERSION, "pipeline": renderer.CACHE_PIPELINE_VERSION,
                "width": width, "height": height, "fps": scene_fps, "start": render_start, "end": render_end,
                "duration": duration, "scene_id": scene_id, "padding_target": int(padding_target or 0),
                "render_cache": bool(use_cache), "block_frames": block, "store": store,
                "clips": [[c.timeline_start_frame, c.timeline_end_frame, c.source_in_frame, c.track_index,
                           c.opacity, c.fit_mode, c.crop_position, c.source_path] for c in clips],
                "media": {key: [s.abs_path, s.source_fps, s.native_frame_count,
                                list(s.interpretation) if s.interpretation is not None else None]
                          for key, s in sorted(sources.items())},
                "sources": sorted({s.abs_path for s in sources.values() if s.abs_path}),
            }
    cap = FRAMES_MAX_BYTES if use_cache else FRAMES_MAX_BYTES_UNCACHED
    return _finish(MediaPlan("frames", request, recipe, reason, materialize, expect, cap))


def _frames_request_from_recipe(recipe: dict) -> Any:
    from ..server import timeline_renderer as renderer

    clips = tuple(renderer.RenderClip(timeline_start_frame=c[0], timeline_end_frame=c[1], source_in_frame=c[2],
                                      track_index=c[3], opacity=c[4], fit_mode=c[5], crop_position=c[6],
                                      source_path=c[7]) for c in recipe["clips"])
    sources = {key: renderer.RenderSource(abs_path=item[0], source_fps=item[1], native_frame_count=item[2],
                                          interpretation=tuple(item[3]) if item[3] is not None else None)
               for key, item in recipe["media"].items()}
    return renderer.RenderRequest(width=recipe["width"], height=recipe["height"], scene_fps=recipe["fps"],
                                  duration=recipe["duration"], scene_id=recipe["scene_id"], clips=clips,
                                  sources=sources, store=recipe["store"], start=recipe["start"], end=recipe["end"],
                                  use_cache=recipe["render_cache"], padding_target=recipe["padding_target"])


@dataclass(frozen=True)
class AudioRecord:
    """An audible track as the mixer reads it, detached from the live scene; ``volume`` is already effective."""

    timeline_start_frame: int
    timeline_end_frame: int
    source_in_frame: int
    volume: float
    source_path: str = ""


def audio_plan(project: Any, scene: Any, start: int, end: int, *, padding_target: int, pad_fps: float,
               materialize: Callable[[Any], Any]) -> MediaPlan:
    """The mix of ``[start, end)``: its request, its recipe when every value is exact, its materializer."""
    from ..server import audio_pipeline, media_helpers
    from ..server.timeline_state import effective_scene_fps

    try:
        sources, window, vote = audio_pipeline.audio_identity_sources(project, scene, start, end)
        fps = effective_scene_fps(project, scene)
    except Exception as exc:
        logger.debug("Editor audio has no request: %s", type(exc).__name__)
        return MediaPlan("audio", None, None, "request_unavailable", materialize, lambda _value: False, 0)
    records, reason = [], ""
    for track, path in sources:
        volume = audio_pipeline.effective_volume(track)
        try:
            records.append((AudioRecord(_exact_int(track.timeline_start_frame), _exact_int(track.timeline_end_frame),
                                        _exact_int(track.source_in_frame), float(volume),
                                        str(getattr(track, "source_path", "") or "")), path))
        except (_Inexact, AttributeError, TypeError, ValueError):
            reason = "recipe_inexact"
            break
    placement = {"start": int(start), "end": int(end), "padding_target": int(padding_target or 0), "pad_fps": pad_fps}
    if reason:
        request = {"sources": list(sources), "fps": fps, **placement}
    else:
        request = {"sources": records, "fps": fps, **placement}

    def expect(value: Any) -> bool:
        return _plain_audio(value)

    recipe = None
    if not reason:
        try:
            ffmpeg = media_helpers.get_ffmpeg_path()
            ffprobe = media_helpers.get_ffprobe_path() or ""
        except Exception:
            ffmpeg, reason = "", "ffmpeg_unavailable"
        if not reason and (type(fps) not in (int, float) or not math.isfinite(fps)
                           or type(pad_fps) not in (int, float) or not math.isfinite(pad_fps)):
            reason = "recipe_inexact"
        if not reason:
            recipe = {
                "kind": "audio", "version": RECIPE_VERSION, "start": int(start), "end": int(end),
                "fps": float(fps), "padding_target": int(padding_target or 0), "pad_fps": float(pad_fps),
                "ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe),
                "tracks": [[path, record.volume, record.timeline_start_frame, record.timeline_end_frame,
                            record.source_in_frame] for record, path in records],
                "sources": sorted(set(window) | set(vote)),
            }
    return _finish(MediaPlan("audio", request, recipe, reason, materialize, expect, AUDIO_MAX_BYTES))


def _audio_request_from_recipe(recipe: dict) -> dict:
    return {"sources": [(AudioRecord(track[2], track[3], track[4], track[1]), track[0])
                        for track in recipe["tracks"]],
            "fps": recipe["fps"], "start": recipe["start"], "end": recipe["end"],
            "padding_target": recipe["padding_target"], "pad_fps": recipe["pad_fps"]}


def _plain_frames(value: Any, shape: tuple) -> bool:
    try:
        import torch
    except Exception:  # pragma: no cover
        return False
    return (isinstance(value, torch.Tensor) and value.dtype == torch.float32 and value.device.type == "cpu"
            and value.layout == torch.strided and tuple(int(size) for size in value.shape) == tuple(shape))


def _plain_audio(value: Any) -> bool:
    try:
        import torch
    except Exception:  # pragma: no cover
        return False
    if not isinstance(value, dict) or set(value) != {"waveform", "sample_rate"}:
        return False
    waveform, rate = value["waveform"], value["sample_rate"]
    return (type(rate) is int and rate > 0 and isinstance(waveform, torch.Tensor)
            and waveform.dtype == torch.float32 and waveform.device.type == "cpu"
            and waveform.layout == torch.strided and waveform.ndim == 3 and int(waveform.shape[0]) == 1)


# --- One Editor call's media ------------------------------------------------------------


@dataclass
class Identity:
    token: str
    snapshots: dict
    total_bytes: int


@dataclass
class PendingMedia:
    """The media of one Editor call: its plans, their identities, the values settled so far.

    ``value(slot)`` materializes a slot at most once per prompt, under a lock,
    and (when ``deferred``) fills the Editor's memo, so a second request never
    renders again. ``accept(slot, value)`` takes a value ComfyUI's cache kept.
    ``on_accept(slot, value)`` runs once per settled slot (the frames build the
    colour-drift reference); ``on_failure(exc)`` applies the failure contract
    of a materialization outside ``_execute_once`` (the Editor marks a consumed
    job failed only while this run still leases it; an interrupt and a
    ``SourceChanged`` never mark it).
    """

    key: str = ""
    call_key: str = ""
    deferred: bool = False
    on_accept: Callable[[int, Any], None] | None = None
    on_failure: Callable[[BaseException], None] | None = None
    plans: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    identities: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)
    digests: dict = field(default_factory=dict)  # path -> (sha256, stat snapshot), shared by both slots
    failure_id: int = 0  # id() of the last materialization's exception: never the object (no cycle)
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def add(self, slot: int, plan: MediaPlan) -> None:
        self.plans[slot] = plan

    def defers(self, slot: int) -> bool:
        return slot in self.plans and slot not in self.values

    def can_defer(self, slot: int) -> bool:
        plan = self.plans.get(slot)
        return plan is not None and plan.recipe is not None

    # Identity ---------------------------------------------------------------------

    def identify(self, slot: int, check: Callable[[], None] | None = None) -> tuple[str, str]:
        """``(token, "")``, or ``("", reason)`` when the slot has no identity this prompt.

        Hashes the canonical recipe and every source in full; files go to a
        per-call pool of up to 8 threads, and ``check`` (the executor's
        interrupt check) runs on this thread only, between files: its raise
        propagates.
        """
        with self._lock:
            if slot in self.identities:
                return self.identities[slot].token, ""
            if slot in self.reasons:
                return "", self.reasons[slot]
            plan = self.plans.get(slot)
            if plan is None:
                return "", "no_plan"
            if plan.recipe is None:
                self.reasons[slot] = plan.reason or "recipe_inexact"
                return "", self.reasons[slot]
            try:
                identity = self._identify(plan, check)
            except cache_identity.SourceTooLarge:
                self.reasons[slot] = "source_too_large"
            except cache_identity.SourceUnstable:
                self.reasons[slot] = "source_unstable"
            except (cache_identity.SourceMissing, OSError):
                self.reasons[slot] = "source_missing"
            else:
                self.identities[slot] = identity
                return identity.token, ""
            return "", self.reasons[slot]

    def _identify(self, plan: MediaPlan, check: Callable[[], None] | None) -> Identity:
        sources = plan.recipe["sources"]
        total = 0
        for path in sources:
            if not path or not os.path.isfile(path):
                raise cache_identity.SourceMissing(path)
            total += os.path.getsize(path)
        if total > plan.max_bytes:
            raise cache_identity.SourceTooLarge(total)
        missing = [path for path in sources if path not in self.digests]
        if check is not None:
            check()
        if missing:
            workers = max(1, min(MAX_WORKERS, os.cpu_count() or 1, len(missing)))
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sonder-editor-identity")
            try:
                futures = {pool.submit(cache_identity.digest_file, path, None): path for path in missing}
                for future in as_completed(futures):
                    if check is not None:
                        check()  # on the calling thread: an interrupt raised here propagates
                    self.digests[futures[future]] = future.result()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        hasher = hashlib.sha256(DOMAIN)
        hasher.update(plan.kind.encode("ascii") + b"\0" + plan.recipe_json.encode("utf-8") + b"\0")
        snapshots = {}
        for path in sources:
            digest, snapshot = self.digests[path]
            snapshots[path] = snapshot
            hasher.update(path.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\0")
        return Identity(cache_content.token(hasher.hexdigest()), snapshots, total)

    def identity_bytes(self) -> int:
        return sum(identity.total_bytes for identity in self.identities.values())

    # Settling ---------------------------------------------------------------------

    def value(self, slot: int) -> Any:
        """The slot's value, materialized now if nothing settled it yet (at most once)."""
        with self._lock:
            if slot in self.values:
                return self.values[slot]
            plan = self.plans[slot]
            identity = self.identities.get(slot)
            try:
                with cache_identity.reading(identity.snapshots if identity is not None else None):
                    value = plan.materialize(plan.render_request())
            except BaseException as exc:
                self.failure_id = id(exc)
                raise
            self._accept(slot, value)
            return value

    def matches(self, slot: int, value: Any) -> bool:
        plan = self.plans.get(slot)
        try:
            return plan is not None and bool(plan.expect(value))
        except Exception:
            return False

    def accept(self, slot: int, value: Any) -> bool:
        """Take ``value`` (from ComfyUI's cache) for a slot not settled yet; False when it does not fit."""
        with self._lock:
            if slot in self.values:
                return True
            if not self.matches(slot, value):
                return False
            self._accept(slot, value)
            return True

    def _accept(self, slot: int, value: Any) -> None:
        self.values[slot] = value
        if self.deferred:
            sdk_runs.fill_result(self.key, self.call_key, slot, value, DEFERRED)
        if self.on_accept is not None:
            self.on_accept(slot, value)

    def failed_with(self, exc: BaseException) -> bool:
        """Whether ``exc`` came out of a materialization (the Editor's failure, not pipeline code)."""
        return self.failure_id == id(exc) and self.failure_id != 0

    def fail(self, exc: BaseException) -> None:
        if self.on_failure is not None:
            self.on_failure(exc)


# --- Safety nets --------------------------------------------------------------------------


def _settle(pending: PendingMedia, slots) -> None:
    for slot in slots:
        try:
            pending.value(slot)
        except BaseException as exc:
            if pending.failed_with(exc):
                pending.fail(exc)
            raise


def settle_scheduled(prompt: Any, unique_id: Any, result: Any) -> Any:
    """The scheduled Editor's net: every slot still deferred is materialized before it returns."""
    if not isinstance(result, tuple) or not any(is_deferred(value) for value in result):
        return result
    key = sdk_runs.trusted_key_for_prompt(prompt)
    call_key = _call_key(unique_id) if key else ""
    pending = sdk_runs.pending_for(key, call_key) if call_key else None
    if pending is None:
        raise RuntimeError("The Sonder Editor's media for this run was lost before it was rendered. Run again.")
    _settle(pending, [slot for slot, value in enumerate(result) if is_deferred(value)])
    found, filled = sdk_runs.first_result(key, call_key)
    if not found or not isinstance(filled, tuple) or any(is_deferred(value) for value in filled):
        raise RuntimeError("The Sonder Editor's media for this run was lost before it was rendered. Run again.")
    return filled


def settle_carrier(prompt_id: Any, token: str, value: Any) -> Any:
    """An executing carrier's net: a slot still deferred is materialized now."""
    if not is_deferred(value):
        return value
    pending = sdk_runs.pending_for_prompt(prompt_id, token)
    slot = next((slot for slot, identity in (pending.identities.items() if pending else ())
                 if identity.token == token), None)
    if pending is None or slot is None:
        raise RuntimeError("This media carrier's Editor media was lost before it was rendered. Run again.")
    _settle(pending, [slot])
    return pending.values[slot]
