"""Proxy media: browser-playable copies of video Chromium cannot decode.

Chromium cannot decode ProRes, FFV1, DNxHD, CineForm and others, two of which Sonder
writes itself. The server builds a proxy, an H.264 MP4 under the project's chosen Save
Video preset with a lossless FLAC copy of the audio, and every display surface shows it.
The original stays the source for render, export, bridges, guides and frame extraction.

Ownership:
- ``proxy_preset`` and ``proxy_build_timing`` are project durable, in
  ``project.metadata``; a missing or unknown value resolves to the default at read time
  and is never rewritten at rest.
- Proxy files and their sidecars are a derived cache in ``<project>/cache/proxies/``:
  ``{asset_id}.json`` and ``{asset_id}_{rev}.mp4``. Nothing here writes ``project.json``,
  and deleting the folder only costs a rebuild.
- The queue, the job in flight, visible demand and priority are this process's memory.
  Reconcile recovers after a restart.

This module is the one authority on whether an asset needs a proxy (``decision``). The
frontend never hands such an original to a ``<video>``.

Every build gets a new ``rev`` and so a new filename: the served URL is immutable, and a
rebuild never overwrites a file a browser is streaming.
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from types import SimpleNamespace
from typing import Callable, Iterable

from . import comfy_idle

logger = logging.getLogger("sonder_editor")

# ── Settings (project durable) ────────────────────────────────────────────────

PROXY_IDLE_GRACE_SECONDS = 30
PROXY_PRESETS = ("Compatible MP4", "High Quality MP4", "Editing Master MP4")
DEFAULT_PROXY_PRESET = "High Quality MP4"
TIMING_IDLE, TIMING_NOW, TIMING_MANUAL = "idle", "now", "manual"
BUILD_TIMINGS = (TIMING_IDLE, TIMING_NOW, TIMING_MANUAL)
DEFAULT_BUILD_TIMING = TIMING_IDLE
METADATA_PRESET_KEY = "proxy_preset"
METADATA_TIMING_KEY = "proxy_build_timing"

# Bump when ``media_helpers.encode_proxy``'s arguments change: it enters every rev.
ENCODER_VERSION = 1
SIDECAR_VERSION = 1
CACHE_LEAF = "proxies"
DEMAND_TTL_SECONDS = 120.0
ORPHAN_GRACE_SECONDS = 600
EVENT_INTERVAL_SECONDS = 0.5
BUSY_POLL_SECONDS = 1.0
MAX_JOBS = 5000

# What the server decided about an asset's original.
DECISION_PENDING = "pending"          # not yet decided (no probe yet)
DECISION_DECODABLE = "decodable"      # the browser plays the original
DECISION_UNDECODABLE = "undecodable"  # needs a proxy
DECISION_MANUAL = "manual"            # decodable, but the user asked for a proxy
DECISION_RUNTIME = "runtime"          # allowed here, but a browser failed to decode it
PROXY_DECISIONS = frozenset({DECISION_UNDECODABLE, DECISION_MANUAL, DECISION_RUNTIME})

# Proxy state of an asset.
STATE_NONE = "none"                   # no proxy and none wanted (or not decided yet)
STATE_MISSING = "missing"             # wanted, none ready, nothing queued; reconcile queues it
STATE_QUEUED = "queued"
STATE_WAITING_IDLE = "waiting_idle"
STATE_BUILDING = "building"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_MANUAL_ONLY = "manual_only"     # wanted, but builds only when asked

# Why a job exists.
REASON_AUTO = "auto"   # a commit, reconcile, demand or a runtime failure
REASON_USER = "user"   # Create, Rebuild or Regenerate: follows the timing setting
REASON_NOW = "now"     # Build now: no gate, preempts an automatic job in flight

_SIDECAR_RE = re.compile(r"^(?P<id>.+)\.json$")
_PROXY_FILE_RE = re.compile(r"^(?P<id>.+)_(?P<rev>[0-9a-f]{16})\.mp4$")
_TEMP_FILE_RE = re.compile(r"^(?P<id>.+)\.(?P<token>[0-9a-f]{32})\.tmp\.mp4$")
_SIDECAR_TEMP_RE = re.compile(r"^(?P<id>.+)\.json\.(?P<token>[0-9a-f]{32})\.tmp$")


def resolve_settings(metadata) -> dict:
    """``{"preset", "timing"}`` from ``project.metadata``; unknown values read as the default."""
    metadata = metadata if isinstance(metadata, dict) else {}
    preset = metadata.get(METADATA_PRESET_KEY)
    timing = metadata.get(METADATA_TIMING_KEY)
    return {
        "preset": preset if preset in PROXY_PRESETS else DEFAULT_PROXY_PRESET,
        "timing": timing if timing in BUILD_TIMINGS else DEFAULT_BUILD_TIMING,
    }


def validate_settings_update(metadata: dict) -> None:
    """Raise ``ValueError`` for a proxy setting outside its enum. Other keys pass."""
    if METADATA_PRESET_KEY in metadata and metadata[METADATA_PRESET_KEY] not in PROXY_PRESETS:
        raise ValueError(f"{METADATA_PRESET_KEY} must be one of {', '.join(PROXY_PRESETS)}")
    if METADATA_TIMING_KEY in metadata and metadata[METADATA_TIMING_KEY] not in BUILD_TIMINGS:
        raise ValueError(f"{METADATA_TIMING_KEY} must be one of {', '.join(BUILD_TIMINGS)}")


# ── Decision ──────────────────────────────────────────────────────────────────

# EXPIRY: this allow list is what current Chromium decodes, from the step 1 probe of the
# proxy plan (Chrome 152, 2026-10-21): H.264 in 4:2:0, 4:2:2 and 4:4:4 at 8 bit and in
# 4:2:0 and 4:2:2 at 10 bit, VP8, VP9 and AV1, in MP4/MOV or Matroska/WebM. HEVC plays
# only with hardware support, so it is proxied. Revisit when Chromium's software decoders
# change, or when a runtime-undecodable report shows an allowed format failing.
_ALLOWED_CONTAINER_TOKENS = frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm"})
_ALLOWED_H264_PIX_FMTS = frozenset({
    "yuv420p", "yuvj420p", "yuv422p", "yuvj422p", "yuv444p", "yuvj444p",
    "yuv420p10le", "yuv422p10le",
})
_ALLOWED_OTHER_CODECS = frozenset({"vp8", "vp9", "av1"})
_ENCODER_CODEC_NAMES = {"libx264": "h264", "libx265": "hevc", "prores_ks": "prores", "ffv1": "ffv1"}
_EXTENSION_CONTAINERS = {
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska", ".webm": "webm",
}


def decision_for_stream(format_name: str, codec_name: str, pix_fmt: str) -> str:
    """Decodable or undecodable, from a container and its first video stream."""
    tokens = {token.strip() for token in str(format_name or "").lower().split(",") if token.strip()}
    codec = _ENCODER_CODEC_NAMES.get(str(codec_name or "").lower(), str(codec_name or "").lower())
    pix_fmt = str(pix_fmt or "").lower()
    if not tokens & _ALLOWED_CONTAINER_TOKENS:
        return DECISION_UNDECODABLE
    if codec == "h264":
        return DECISION_DECODABLE if pix_fmt in _ALLOWED_H264_PIX_FMTS else DECISION_UNDECODABLE
    return DECISION_DECODABLE if codec in _ALLOWED_OTHER_CODECS else DECISION_UNDECODABLE


def decision_for_save_params(inline_params, extension: str) -> str | None:
    """A Save Video output is decided from its recorded preset or codec, with no probe.
    None when the asset carries neither (an import)."""
    from .media_helpers import CUSTOM_SAVE_VIDEO_PRESET, SAVE_VIDEO_PRESETS

    params = inline_params if isinstance(inline_params, dict) else {}
    preset = str(params.get("save_preset") or "")
    codec = str(params.get("codec") or "")
    pix_fmt = str(params.get("pix_fmt") or "")
    spec = SAVE_VIDEO_PRESETS.get(preset) if preset and preset != CUSTOM_SAVE_VIDEO_PRESET else None
    if spec:
        codec = codec or str(spec.get("codec") or "")
        pix_fmt = pix_fmt or str(spec.get("pix_fmt") or "")
    if not codec:
        return None
    container = _EXTENSION_CONTAINERS.get(str(extension or "").lower(), "")
    return decision_for_stream(container, codec, pix_fmt)


def wants_proxy(decision: str) -> bool:
    return decision in PROXY_DECISIONS


# ── Cache files ───────────────────────────────────────────────────────────────


def proxies_root(project_dir: str, *, create: bool = False) -> str:
    """``<project>/cache/proxies``, or "" when ``cache/`` is missing or the path is unsafe.
    Creating it only ever makes the last folder, under a ``cache/`` that already exists."""
    from .path_security import project_cache_root, resolve_project_path
    from .render_cache import RenderCacheError, _validate_cache_root_path

    if not project_dir or not project_cache_root(project_dir, must_exist=True):
        return ""
    root = resolve_project_path(project_dir, os.path.join("cache", CACHE_LEAF), purpose="proxy cache root")
    if not root:
        return ""
    try:
        _validate_cache_root_path(root, leaf=CACHE_LEAF)
        if create and not os.path.isdir(root):
            try:
                os.mkdir(root)
            except FileExistsError:
                pass
            _validate_cache_root_path(root, leaf=CACHE_LEAF)
    except (RenderCacheError, OSError):
        logger.warning("Proxy cache root rejected for a project", exc_info=True)
        return ""
    return root


def proxy_file_name(asset_id: str, rev: str) -> str:
    return f"{asset_id}_{rev}.mp4"


def matches_asset_proxy_file(asset_id: str, name: str) -> bool:
    """Exact match, never a glob: ``abc`` must never match ``abc_def``'s files."""
    return re.fullmatch(rf"{re.escape(asset_id)}_[0-9a-f]{{16}}\.mp4", name) is not None


def compute_rev(source_rev: str, preset: str, build: int) -> str:
    """A fresh rev per build. The nonce keeps a rev unique even when a sidecar is lost
    and its build counter restarts, so a name is never reused for different bytes."""
    raw = f"{source_rev}|{preset}|{ENCODER_VERSION}|{int(build)}|{uuid.uuid4().hex}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def source_signature(path: str) -> str:
    """``size:mtime_ns``, the same signature asset probes use; "" when unreadable."""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _safe_asset_id(asset_id) -> str:
    value = str(asset_id or "")
    if not value or len(value) > 128 or value in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return ""
    return value


class _Listing:
    """One ``scandir`` of a project's proxy folder, reused while the folder's mtime holds.
    On Windows a directory entry carries its size and mtime, so no per-file stat."""

    __slots__ = ("mtime_ns", "sidecars", "files", "temps")

    def __init__(self, mtime_ns: int):
        self.mtime_ns = mtime_ns
        self.sidecars: dict[str, tuple[int, int]] = {}   # asset_id -> (mtime_ns, size)
        self.files: dict[str, int] = {}                   # proxy file name -> bytes
        self.temps: list[str] = []


_listing_cache: "OrderedDict[str, _Listing]" = OrderedDict()
_sidecar_cache: "OrderedDict[str, tuple[tuple[int, int], dict]]" = OrderedDict()
_cache_lock = threading.Lock()
_LISTING_CACHE_MAX = 64
_SIDECAR_CACHE_MAX = 8192


def _listing(root: str) -> _Listing | None:
    try:
        mtime_ns = os.stat(root).st_mtime_ns
    except OSError:
        return None
    key = os.path.normcase(root)
    with _cache_lock:
        cached = _listing_cache.get(key)
        if cached is not None and cached.mtime_ns == mtime_ns:
            _listing_cache.move_to_end(key)
            return cached
    listing = _Listing(mtime_ns)
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                name = entry.name
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _TEMP_FILE_RE.match(name) or _SIDECAR_TEMP_RE.match(name):
                    listing.temps.append(name)
                    continue
                match = _PROXY_FILE_RE.match(name)
                if match:
                    listing.files[name] = int(stat.st_size)
                    continue
                match = _SIDECAR_RE.match(name)
                if match:
                    listing.sidecars[match.group("id")] = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return None
    with _cache_lock:
        _listing_cache[key] = listing
        _listing_cache.move_to_end(key)
        while len(_listing_cache) > _LISTING_CACHE_MAX:
            _listing_cache.popitem(last=False)
    return listing


def _forget_listing(root: str) -> None:
    with _cache_lock:
        _listing_cache.pop(os.path.normcase(root), None)


def read_sidecar(root: str, asset_id: str, stamp: tuple[int, int] | None = None) -> dict:
    """The parsed sidecar, or {} when absent or unreadable. Cached by (mtime, size)."""
    path = os.path.join(root, f"{asset_id}.json")
    if stamp is None:
        try:
            stat = os.stat(path)
        except OSError:
            return {}
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
    key = os.path.normcase(path)
    with _cache_lock:
        cached = _sidecar_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return copy.deepcopy(cached[1])
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    with _cache_lock:
        _sidecar_cache[key] = (stamp, data)
        _sidecar_cache.move_to_end(key)
        while len(_sidecar_cache) > _SIDECAR_CACHE_MAX:
            _sidecar_cache.popitem(last=False)
    return copy.deepcopy(data)


def write_sidecar(root: str, asset_id: str, data: dict) -> None:
    from .atomic_io import atomic_replace

    path = os.path.join(root, f"{asset_id}.json")
    temp = os.path.join(root, f"{asset_id}.json.{uuid.uuid4().hex}.tmp")
    payload = {**data, "v": SIDECAR_VERSION}
    try:
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, sort_keys=True)
        atomic_replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    _forget_listing(root)


def _remove_quietly(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# ── Project reads ─────────────────────────────────────────────────────────────

_settings_cache: "OrderedDict[str, tuple[tuple[int, int], dict, list[str]]]" = OrderedDict()


def _project_file_stamp(project_dir: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(os.path.join(project_dir, "project.json"))
    except OSError:
        return None
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _read_document(project_dir: str) -> dict:
    from .project_manager import read_project_document

    return read_project_document(project_dir)


def project_settings(project_dir: str) -> dict:
    """Settings from ``project.json``, re-read only when the file changed."""
    stamp = _project_file_stamp(project_dir)
    key = os.path.normcase(project_dir)
    with _cache_lock:
        cached = _settings_cache.get(key)
        if cached is not None and stamp is not None and cached[0] == stamp:
            return dict(cached[1])
    try:
        data = _read_document(project_dir)
    except Exception:
        return resolve_settings({})
    _remember_document(project_dir, data, stamp)
    return resolve_settings(data.get("metadata"))


def _remember_document(project_dir: str, data: dict, stamp=None) -> None:
    stamp = stamp or _project_file_stamp(project_dir)
    if stamp is None:
        return
    settings = resolve_settings(data.get("metadata"))
    aliases = _aliases_for(project_dir, str(data.get("project_id") or ""))
    key = os.path.normcase(project_dir)
    with _cache_lock:
        _settings_cache[key] = (stamp, settings, aliases)
        _settings_cache.move_to_end(key)
        while len(_settings_cache) > _LISTING_CACHE_MAX:
            _settings_cache.popitem(last=False)


def _aliases_for(project_dir: str, canonical: str) -> list[str]:
    folder = os.path.basename(os.path.normpath(project_dir)) if project_dir else ""
    return [alias for alias in dict.fromkeys((canonical, folder)) if alias]


def project_aliases(project_dir: str) -> list[str]:
    """The ids a project's events go to: its canonical id and its folder name."""
    key = os.path.normcase(project_dir)
    with _cache_lock:
        cached = _settings_cache.get(key)
    if cached is None:
        project_settings(project_dir)
        with _cache_lock:
            cached = _settings_cache.get(key)
    if cached is not None:
        return list(cached[2])
    return _aliases_for(project_dir, "")


def _raw_asset(data: dict, asset_id: str) -> dict | None:
    for raw in data.get("assets") or []:
        if isinstance(raw, dict) and str(raw.get("asset_id") or "") == asset_id:
            return raw
    return None


def _inline_params(raw: dict) -> dict:
    from .project_storage import INLINE_GENERATION_PARAM_KEYS

    params = raw.get("generation_params") if isinstance(raw.get("generation_params"), dict) else {}
    return {key: value for key, value in params.items() if key in INLINE_GENERATION_PARAM_KEYS}


def _asset_source_path(project_dir: str, raw: dict) -> str:
    from .path_security import project_media_path

    rel = str(raw.get("path") or "").replace("\\", "/").strip("/")
    if not rel.startswith("media/"):
        return ""
    return project_media_path(project_dir, rel, purpose=f"proxy source {raw.get('asset_id') or ''}")


def _color_subject(raw: dict) -> SimpleNamespace:
    """What ``resolve_source_color_interpretation`` reads from an asset, from its raw record."""
    inline = _inline_params(raw)
    return SimpleNamespace(
        width=int(raw.get("width") or 0), height=int(raw.get("height") or 0),
        color_space=str(raw.get("color_space") or ""), color_range=str(raw.get("color_range") or ""),
        color_probed=bool(raw.get("color_probed")), inline_generation_params=inline,
    )


# ── Jobs and cancellation ─────────────────────────────────────────────────────


class _Job:
    __slots__ = ("project_dir", "pkey", "asset_id", "reason", "rebuild", "force", "seq",
                 "waiting", "waiting_since", "resumed", "user_started")

    def __init__(self, project_dir: str, pkey: str, asset_id: str, reason: str, seq: int, *,
                 rebuild: bool = False, force: bool = False):
        self.project_dir = project_dir
        self.pkey = pkey
        self.asset_id = asset_id
        self.reason = reason
        self.rebuild = rebuild
        # Create proxy / Build now: a proxy even for an original the browser plays.
        self.force = force
        self.seq = seq
        # Waiting for ComfyUI to be idle now; ``waiting_since`` survives a kill and requeue.
        self.waiting = False
        self.waiting_since = 0.0
        self.resumed = False
        self.user_started = reason in (REASON_USER, REASON_NOW)


class _CancelSignal:
    """``is_set`` for ``run_ffmpeg_command``. Trips on stop, preemption, a user cancel
    or, for a job gated on idle, ComfyUI turning busy (sampled, and pushed by the
    prompt-queue hooks through ``comfy_idle``)."""

    def __init__(self, *, gated: bool, queue_busy: Callable[[], bool]):
        self.gated = gated
        self.queue_busy = queue_busy
        self.reason = ""
        self._event = threading.Event()
        self._checked_at = 0.0

    def trip(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        if self.gated:
            now = time.monotonic()
            if now - self._checked_at >= 0.5:
                self._checked_at = now
                if self.queue_busy():
                    comfy_idle.note_busy()
                    self.trip("busy")
        return self._event.is_set()

    def discards_result(self) -> bool:
        """After ffmpeg finished: only a cancel or a shutdown throws the encode away; a
        prompt starting or a Build now arriving does not undo finished work."""
        return self._event.is_set() and self.reason in ("cancel", "stop")


class _InlineSignal:
    """The take's encode stops on a ComfyUI interrupt or a worker shutdown."""

    def __init__(self, owner, interrupted: Callable[[], bool]):
        self.owner = owner
        self.interrupted = interrupted
        self.reason = ""

    def is_set(self) -> bool:
        if self.owner.stopped:
            self.reason = "stop"
            return True
        try:
            if self.interrupted():
                self.reason = "interrupt"
                return True
        except Exception:
            return False
        return False

    def discards_result(self) -> bool:
        return self.owner.stopped


class _Requeue(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Drop(Exception):
    """The job no longer applies (asset gone, project unresolvable, cancelled)."""


class _HandOff(Exception):
    """The take cannot build now; the worker takes it."""


class BuildFailed(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ── Worker ────────────────────────────────────────────────────────────────────


class ProxyWorker:
    """One daemon thread for every project's builds, one for decisions, and batched
    ``proxy_status`` events. Jobs carry the project directory and are re-resolved through
    ``resolve`` (the project routes' resolver) before running and again before
    publishing, so an unlinked or untrusted project is dropped instead of written to.

    Lock order: ``encode_lock`` → asset cache lock → project write lock; ``condition``
    is only ever taken last and held briefly, never across I/O."""

    def __init__(self, *, resolve: Callable[[str], str | None] | None = None,
                 queue_busy: Callable[[], bool] = comfy_idle.comfy_queue_busy,
                 idle_seconds: Callable[[], float] | None = None,
                 encode: Callable | None = None,
                 emit: Callable[[str, dict], None] | None = None,
                 grace_seconds: float = PROXY_IDLE_GRACE_SECONDS,
                 event_interval: float = EVENT_INTERVAL_SECONDS,
                 poll_seconds: float = BUSY_POLL_SECONDS):
        self.resolve = resolve
        self.queue_busy = queue_busy
        self._idle_seconds = idle_seconds
        self._encode = encode
        self._emit_event = emit
        self.grace_seconds = float(grace_seconds)
        self.event_interval = float(event_interval)
        self.poll_seconds = float(poll_seconds)
        self.condition = threading.Condition()
        self.jobs: "OrderedDict[tuple[str, str], _Job]" = OrderedDict()   # pending builds
        self.undecided: "OrderedDict[tuple[str, str], _Job]" = OrderedDict()
        self.current: _Job | None = None
        self.current_signal: _CancelSignal | None = None
        self.inline: set[tuple[str, str]] = set()   # takes building inside Save Video
        self.demand: dict[str, dict[str, float]] = {}
        self.deferred_deletes: set[str] = set()     # superseded proxy files only, never sidecars
        self.encode_lock = threading.Lock()
        self.stopped = False
        self._seq = 0
        self._epoch = 0
        self._threads: list[threading.Thread] = []
        self._pending_events: dict[str, set[str]] = {}
        self._event_timers: dict[str, threading.Timer] = {}
        self._last_flush: dict[str, float] = {}
        self._dirs: dict[str, str] = {}    # project key -> project_dir
        # The epoch of the last event sent per asset. Every read reports at least it, so
        # a read made after an event is never older than that event, and a read made
        # before one is (the frontend merges by epoch).
        self._asset_epochs: "OrderedDict[tuple[str, str], int]" = OrderedDict()
        # Packet counts read the whole source: kept per (path, signature) across requeues.
        self._packet_counts: "OrderedDict[tuple[str, str], int]" = OrderedDict()
        comfy_idle.add_busy_listener(self._on_busy)

    # Identity -------------------------------------------------------------

    @staticmethod
    def project_key(project_dir: str) -> str:
        return os.path.normcase(os.path.realpath(project_dir))

    def next_epoch(self) -> int:
        with self.condition:
            self._epoch = max(self._epoch + 1, time.time_ns() // 1000)
            return self._epoch

    def _resolved(self, project_dir: str) -> bool:
        if not project_dir or not os.path.isfile(os.path.join(project_dir, "project.json")):
            return False
        if self.resolve is None:
            return True
        resolved = self.resolve(os.path.basename(os.path.normpath(project_dir)))
        return bool(resolved) and self.project_key(resolved) == self.project_key(project_dir)

    # Requests -------------------------------------------------------------

    def enqueue(self, project_dir: str, asset_ids: Iterable[str], *, reason: str = REASON_AUTO,
                rebuild: bool = False, force: bool = False) -> int:
        """Queue decisions (and builds where needed). Cheap: no probe, no project read.
        ``rebuild`` replaces a ready proxy; ``force`` builds one even for a decodable
        original (the user's Create proxy)."""
        if not project_dir:
            return 0
        pkey = self.project_key(project_dir)
        queued = 0
        preempt = False
        with self.condition:
            if self.stopped:
                return 0
            self._dirs[pkey] = project_dir
            for raw_id in asset_ids:
                asset_id = _safe_asset_id(raw_id)
                if not asset_id:
                    continue
                key = (pkey, asset_id)
                existing = self.jobs.get(key) or self.undecided.get(key)
                if existing is not None:
                    if _reason_rank(reason) < _reason_rank(existing.reason):
                        existing.reason = reason
                        existing.user_started = existing.user_started or reason in (REASON_USER, REASON_NOW)
                    existing.rebuild = existing.rebuild or rebuild
                    existing.force = existing.force or force
                    # Preemption waits until a Build now job is decided and buildable.
                    if reason == REASON_NOW and key in self.jobs:
                        preempt = True
                    continue
                # An asset already building is queued again all the same: a replace may
                # have changed its source, and the build skips a proxy that is current.
                if len(self.jobs) + len(self.undecided) >= MAX_JOBS:
                    break
                self._seq += 1
                self.undecided[key] = _Job(project_dir, pkey, asset_id, reason, self._seq,
                                           rebuild=rebuild, force=force)
                queued += 1
            if preempt:
                self._preempt_locked()
            self.condition.notify_all()
        self.start()
        return queued

    def _preempt_locked(self) -> None:
        """Caller holds the condition. A Build now or a take is waiting: stop an
        automatic or queued build in flight, which goes back in line ahead of its peers."""
        if self.current is not None and self.current.reason != REASON_NOW and self.current_signal is not None:
            self.current_signal.trip("preempt")

    def note_demand(self, project_dir: str, asset_ids: Iterable[str]) -> None:
        pkey = self.project_key(project_dir)
        now = time.monotonic()
        ids = [asset_id for asset_id in (_safe_asset_id(value) for value in asset_ids) if asset_id]
        with self.condition:
            bucket = self.demand.setdefault(pkey, {})
            for asset_id in ids[:500]:
                bucket[asset_id] = now
            for asset_id, at in list(bucket.items()):
                if now - at > DEMAND_TTL_SECONDS:
                    bucket.pop(asset_id, None)
            self.condition.notify_all()
        self.enqueue(project_dir, ids, reason=REASON_AUTO)

    def cancel(self, project_dir: str, asset_ids: Iterable[str] | None = None, *,
               user_only: bool = False) -> int:
        """Drop pending jobs (and stop the one in flight) of a project, or of some of its
        assets; ``user_only`` keeps automatic ones. Returns how many were cancelled. No
        I/O: safe on the event loop and under an asset's cache lock."""
        pkey = self.project_key(project_dir)
        wanted = None if asset_ids is None else {str(value) for value in asset_ids}
        cancelled = []
        with self.condition:
            for table in (self.undecided, self.jobs):
                for key, job in list(table.items()):
                    if key[0] != pkey or (wanted is not None and job.asset_id not in wanted):
                        continue
                    if user_only and not job.user_started:
                        continue
                    table.pop(key, None)
                    cancelled.append(job)
            current = self.current
            if current is not None and current.pkey == pkey \
                    and (wanted is None or current.asset_id in wanted) \
                    and (not user_only or current.user_started) and self.current_signal is not None:
                self.current_signal.trip("cancel")
                cancelled.append(current)
            self.condition.notify_all()
        for job in cancelled:
            self._emit_state(job.project_dir, job.asset_id)
        return len(cancelled)

    def settings_changed(self, project_dir: str) -> None:
        """After a settings save: *Only when I ask* drops queued automatic jobs; leaving it
        queues every asset that wants a proxy and has none (trashed ones are dropped when
        decided)."""
        settings = project_settings(project_dir)
        if settings["timing"] == TIMING_MANUAL:
            pkey = self.project_key(project_dir)
            dropped = []
            with self.condition:
                for key, job in list(self.jobs.items()):
                    if key[0] == pkey and job.reason == REASON_AUTO:
                        self.jobs.pop(key, None)
                        dropped.append(job)
                current = self.current
                if current is not None and current.pkey == pkey \
                        and current.reason == REASON_AUTO and self.current_signal is not None:
                    self.current_signal.trip("cancel")
                    dropped.append(current)
            for job in dropped:
                self._emit_state(project_dir, job.asset_id)
            return
        root = proxies_root(project_dir)
        listing = _listing(root) if root else None
        if listing is None:
            return
        wanted = []
        for asset_id, stamp in listing.sidecars.items():
            sidecar = read_sidecar(root, asset_id, stamp)
            if _wants_build(sidecar, listing):
                wanted.append(asset_id)
        if wanted:
            self.enqueue(project_dir, wanted, reason=REASON_AUTO)

    # State ----------------------------------------------------------------

    def _overlay(self, pkey: str, asset_id: str) -> tuple[str, float] | None:
        """Caller holds the condition. The queue's view of one asset, if it has one."""
        key = (pkey, asset_id)
        current = self.current
        if (current is not None and current.pkey == pkey and current.asset_id == asset_id) or key in self.inline:
            return STATE_BUILDING, 0.0
        job = self.jobs.get(key) or self.undecided.get(key)
        if job is None:
            return None
        if job.waiting:
            return STATE_WAITING_IDLE, job.waiting_since
        return STATE_QUEUED, 0.0

    def states(self, project_dir: str, assets: Iterable[tuple[str, dict, str]], *,
               settings: dict | None = None) -> dict[str, dict]:
        """``{asset_id: {decision, state, rev, epoch, ...}}`` for video assets given as
        ``(asset_id, inline_generation_params, extension)``. One folder listing (cached by
        its mtime), no per-asset stat. Routes pass ``settings`` from the project they
        loaded, so this never re-reads ``project.json``."""
        assets = [entry for entry in assets if entry[0]]
        root = proxies_root(project_dir)
        listing = _listing(root) if root else None
        pkey = self.project_key(project_dir)
        result: dict[str, dict] = {}
        with self.condition:
            overlays = {asset_id: self._overlay(pkey, asset_id) for asset_id, _inline, _ext in assets}
            emitted = {asset_id: self._asset_epochs.get((pkey, asset_id), 0) for asset_id, _i, _e in assets}
        for asset_id, inline, extension in assets:
            sidecar = read_sidecar(root, asset_id, listing.sidecars[asset_id]) \
                if listing is not None and asset_id in listing.sidecars else {}
            decision = str(sidecar.get("decision") or "")
            if not decision:
                decision = decision_for_save_params(inline, extension) or DECISION_PENDING
            item = {
                "decision": decision,
                "state": STATE_NONE,
                "rev": "",
                "epoch": max(int(sidecar.get("epoch") or 0), emitted.get(asset_id, 0)),
            }
            ready = _ready_file(sidecar, listing)
            if ready:
                item["state"] = STATE_READY
                item["rev"] = str(sidecar.get("rev") or "")
                item["bytes"] = listing.files.get(ready, 0)
                item["preset"] = str(sidecar.get("preset") or "")
            overlay = overlays.get(asset_id)
            if ready:
                if overlay is not None:
                    item["rebuilding"] = overlay[0]
            elif overlay is not None:
                item["state"] = overlay[0]
                if overlay[1]:
                    item["waiting_since"] = overlay[1]
            elif sidecar.get("state") == STATE_FAILED and wants_proxy(decision):
                item["state"] = STATE_FAILED
                item["error_code"] = str(sidecar.get("error_code") or "")
            elif wants_proxy(decision):
                if settings is None:
                    settings = project_settings(project_dir)
                manual = settings["timing"] == TIMING_MANUAL or bool(sidecar.get("held"))
                item["state"] = STATE_MANUAL_ONLY if manual else STATE_MISSING
            result[asset_id] = item
        return result

    def queue_summary(self, project_dir: str) -> dict:
        pkey = self.project_key(project_dir)
        with self.condition:
            pending = [job for key, job in list(self.jobs.items()) + list(self.undecided.items()) if key[0] == pkey]
            current = self.current if (self.current is not None and self.current.pkey == pkey) else None
            inline = sorted(asset_id for key_pkey, asset_id in self.inline if key_pkey == pkey)
        waiting = [job.waiting_since for job in pending if job.waiting]
        return {
            "pending": len(pending),
            "waiting_idle": len(waiting),
            "waiting_since": min(waiting) if waiting else 0,
            "building": current.asset_id if current is not None else (inline[0] if inline else ""),
            "user_pending": sum(1 for job in pending if job.user_started)
                            + (1 if current is not None and current.user_started else 0),
        }

    # Events ---------------------------------------------------------------

    def _emit_state(self, project_dir: str, asset_id: str) -> None:
        """Note one asset's change. No I/O here: the batch is read and sent from a timer
        thread, at most one ``proxy_status`` per project per interval."""
        pkey = self.project_key(project_dir)
        with self.condition:
            self._dirs.setdefault(pkey, project_dir)
            self._pending_events.setdefault(pkey, set()).add(asset_id)
            if pkey in self._event_timers or self.stopped:
                return
            delay = max(0.0, self._last_flush.get(pkey, 0.0) + self.event_interval - time.monotonic())
            timer = threading.Timer(delay, self._flush_events, args=(pkey,))
            timer.daemon = True
            self._event_timers[pkey] = timer
        timer.start()

    def _flush_events(self, pkey: str) -> None:
        with self.condition:
            self._event_timers.pop(pkey, None)
            ids = sorted(self._pending_events.pop(pkey, set()))
            self._last_flush[pkey] = time.monotonic()
            project_dir = self._dirs.get(pkey, "")
        if not ids or not project_dir:
            return
        try:
            computed = self.states(project_dir, [(asset_id, {}, "") for asset_id in ids])
        except Exception:
            logger.debug("proxy_status state read failed", exc_info=True)
            return
        epoch = self.next_epoch()
        items = []
        with self.condition:
            for asset_id in ids:
                state = computed.get(asset_id) or {}
                item = {"asset_id": asset_id, "decision": state.get("decision", DECISION_PENDING),
                        "state": state.get("state", STATE_NONE), "rev": state.get("rev", ""), "epoch": epoch}
                for key in ("waiting_since", "error_code", "rebuilding"):
                    if state.get(key):
                        item[key] = state[key]
                items.append(item)
                self._asset_epochs[(pkey, asset_id)] = epoch
                self._asset_epochs.move_to_end((pkey, asset_id))
            while len(self._asset_epochs) > 20000:
                self._asset_epochs.popitem(last=False)
        event = {"type": "proxy_status", "epoch": epoch, "items": items,
                 "queue": self.queue_summary(project_dir)}
        emit = self._emit_event or _schedule_event
        for alias in project_aliases(project_dir):
            try:
                emit(alias, {**event, "project_id": alias})
            except Exception:
                logger.debug("proxy_status emit failed", exc_info=True)

    def flush_all_events(self) -> None:
        """Tests and shutdown: send everything batched now."""
        with self.condition:
            timers = dict(self._event_timers)
        for pkey, timer in timers.items():
            timer.cancel()
            self._flush_events(pkey)

    # Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        with self.condition:
            if self._threads or self.stopped:
                return
            for name, target in (("sonder-proxy-decide", self._decide_loop),
                                 ("sonder-proxy-build", self._build_loop)):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._threads.append(thread)
                thread.start()

    def stop(self) -> None:
        with self.condition:
            self.stopped = True
            self.jobs.clear()
            self.undecided.clear()
            if self.current_signal is not None:
                self.current_signal.trip("stop")
            timers = list(self._event_timers.values())
            self._event_timers.clear()
            self.condition.notify_all()
        for timer in timers:
            timer.cancel()
        comfy_idle.remove_busy_listener(self._on_busy)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5)

    def _on_busy(self) -> None:
        with self.condition:
            signal = self.current_signal
        if signal is not None and signal.gated:
            signal.trip("busy")

    def idle_seconds(self) -> float:
        if self._idle_seconds is not None:
            return float(self._idle_seconds())
        return comfy_idle.seconds_since_busy(queue_busy=self.queue_busy)

    # Decisions ------------------------------------------------------------

    def _decide_loop(self) -> None:
        while True:
            with self.condition:
                while not self.undecided and not self.stopped:
                    self.condition.wait()
                if self.stopped:
                    return
                # Newest first, Build now ahead of everything.
                key, job = min(self.undecided.items(), key=lambda item: (_reason_rank(item[1].reason), -item[1].seq))
                self.undecided.pop(key, None)
            try:
                build = self._decide(job)
            except _Drop:
                build = False
            except Exception:
                logger.exception("Proxy decision failed for an asset")
                build = False
            if build:
                with self.condition:
                    if not self.stopped:
                        existing = self.jobs.get(key)
                        if existing is None:
                            self.jobs[key] = job
                        else:
                            existing.rebuild = existing.rebuild or job.rebuild
                            existing.force = existing.force or job.force
                            if _reason_rank(job.reason) < _reason_rank(existing.reason):
                                existing.reason = job.reason
                                existing.user_started = True
                        if job.reason == REASON_NOW:
                            self._preempt_locked()
                        self.condition.notify_all()
            self._emit_state(job.project_dir, job.asset_id)

    def _decide(self, job: _Job) -> bool:
        """Settle the asset's decision and say whether a build should be queued.

        A stale or missing sidecar is always decided by probing the file. Save settings
        decide only the listing's fallback and a fresh take, because a Replace keeps
        them while changing the bytes."""
        if not self._resolved(job.project_dir):
            raise _Drop()
        data = _read_document(job.project_dir)
        _remember_document(job.project_dir, data)
        raw = _raw_asset(data, job.asset_id)
        if raw is None or str(raw.get("asset_type") or "") != "video":
            raise _Drop()
        if raw.get("trashed_at") and not job.user_started:
            raise _Drop()
        source = _asset_source_path(job.project_dir, raw)
        signature = source_signature(source) if source else ""
        if not signature:
            raise _Drop()
        root = proxies_root(job.project_dir, create=True)
        if not root:
            raise _Drop()
        with self._asset_lock(job.project_dir, job.asset_id):
            sidecar = read_sidecar(root, job.asset_id)
        changed = False
        if sidecar.get("source_rev") != signature or not sidecar.get("decision"):
            from .media_helpers import probe_video_stream_info

            previous = sidecar
            info = probe_video_stream_info(source)
            if not info.get("probed"):
                with self._asset_lock(job.project_dir, job.asset_id):
                    write_sidecar(root, job.asset_id, {
                        "source_rev": signature, "decision": DECISION_PENDING, "state": STATE_FAILED,
                        "error_code": "probe_failed", "build": int(previous.get("build") or 0),
                        "epoch": self.next_epoch()})
                return False
            decision = decision_for_stream(info.get("format_name", ""), info.get("codec_name", ""),
                                           info.get("pix_fmt", ""))
            sidecar = {
                "source_rev": signature,
                "decision": decision,
                "codec": info.get("codec_name", ""),
                "pix_fmt": info.get("pix_fmt", ""),
                "format": info.get("format_name", ""),
                "state": STATE_NONE,
                "build": int(previous.get("build") or 0),
                "rev": "", "file": "", "preset": "",
            }
            # A user's manual proxy survives a source change; a runtime report does not
            # outlive the bytes it was made against.
            if previous.get("decision") == DECISION_MANUAL and decision == DECISION_DECODABLE:
                sidecar.update({"decision": DECISION_MANUAL, "manual_from": decision})
            changed = True
        if job.force and sidecar.get("decision") in (DECISION_DECODABLE, DECISION_PENDING):
            sidecar["manual_from"] = sidecar.get("decision")
            sidecar["decision"] = DECISION_MANUAL
            changed = True
        if job.user_started and sidecar.get("held"):
            sidecar.pop("held", None)
            changed = True
        if changed:
            sidecar["epoch"] = self.next_epoch()
            with self._asset_lock(job.project_dir, job.asset_id):
                current = read_sidecar(root, job.asset_id)
                # Keep a proxy published meanwhile (a take built inline, say).
                if current.get("source_rev") == signature and current.get("state") == STATE_READY:
                    for key in ("state", "rev", "file", "preset", "bytes", "built_at", "build"):
                        if key in current:
                            sidecar[key] = current[key]
                write_sidecar(root, job.asset_id, sidecar)
        if not wants_proxy(sidecar.get("decision", "")):
            return False
        if _ready_file(sidecar, _listing(root)) and not job.rebuild:
            return False
        if not job.user_started:
            settings = project_settings(job.project_dir)
            # A proxy the user deleted is rebuilt only when someone asks for it.
            if settings["timing"] == TIMING_MANUAL or sidecar.get("held"):
                return False
            if sidecar.get("state") == STATE_FAILED and not changed:
                # A failure is retried when someone asks, not on every reconcile.
                return False
        return True

    # Builds ---------------------------------------------------------------

    def _rank(self, job: _Job, now: float) -> tuple:
        """Build now first; then a resumed job or one the editor shows; then newest."""
        if job.reason == REASON_NOW:
            return (0, -job.seq)
        demanded = now - self.demand.get(job.pkey, {}).get(job.asset_id, -1e9) <= DEMAND_TTL_SECONDS
        return (1 if (job.resumed or demanded) else 2, -job.seq)

    @staticmethod
    def _gated(job: _Job, settings: dict) -> bool:
        return job.reason != REASON_NOW and settings["timing"] != TIMING_NOW

    def _next_runnable(self) -> tuple[tuple[str, str], _Job, dict, bool] | None:
        """The best job that may run now, or None. Every gated job's waiting flag is
        brought up to date on the way (waiting for idle, or merely queued behind another
        build), and a gated job never starves an ungated one."""
        now = time.monotonic()
        with self.condition:
            ordered = sorted(self.jobs.items(), key=lambda item: self._rank(item[1], now))
        settings_by_project: dict[str, dict] = {}
        idle = None
        dropped: list[_Job] = []
        gated_jobs: list[_Job] = []
        chosen = None
        for key, job in ordered:
            settings = settings_by_project.get(job.pkey)
            if settings is None:
                settings = settings_by_project[job.pkey] = project_settings(job.project_dir)
            if job.reason == REASON_AUTO and settings["timing"] == TIMING_MANUAL:
                dropped.append(job)
                continue
            gated = self._gated(job, settings)
            if gated:
                gated_jobs.append(job)
                if idle is None:
                    idle = self.idle_seconds() >= self.grace_seconds
                if not idle:
                    continue
            if chosen is None:
                chosen = (key, job, settings, gated)
        stamp = time.time()
        changed: list[_Job] = []
        with self.condition:
            for job in dropped:
                if self.jobs.get((job.pkey, job.asset_id)) is job:
                    self.jobs.pop((job.pkey, job.asset_id), None)
            for job in gated_jobs:
                waiting = not idle
                if job.waiting != waiting:
                    job.waiting = waiting
                    if waiting and not job.waiting_since:
                        job.waiting_since = stamp
                    changed.append(job)
        for job in dropped + changed:
            self._emit_state(job.project_dir, job.asset_id)
        return chosen

    def _build_loop(self) -> None:
        self._retry_deferred_deletes()
        while True:
            with self.condition:
                if not self.jobs and not self.stopped:
                    self.condition.wait(timeout=60)
                if self.stopped:
                    return
                has_jobs = bool(self.jobs)
            if not has_jobs:
                self._retry_deferred_deletes()
                continue
            chosen = self._next_runnable()
            if chosen is None:
                with self.condition:
                    if not self.stopped:
                        self.condition.wait(timeout=self.poll_seconds)
                continue
            key, job, settings, gated = chosen
            signal = _CancelSignal(gated=gated, queue_busy=self.queue_busy)
            with self.condition:
                if self.jobs.get(key) is not job or self.stopped:
                    continue
                self.jobs.pop(key, None)
                job.waiting = False
                self.current = job
                self.current_signal = signal
            self._emit_state(job.project_dir, job.asset_id)
            outcome = "done"
            try:
                with self.encode_lock:
                    if signal.is_set():
                        raise _Requeue(signal.reason or "preempt")
                    self._build(job, settings, signal)
            except _Requeue as exc:
                outcome = exc.reason
            except _Drop:
                outcome = "drop"
            except Exception:
                logger.exception("Proxy build crashed for an asset")
                outcome = "drop"
            finally:
                with self.condition:
                    self.current = None
                    self.current_signal = None
                    if outcome in ("busy", "preempt") and not self.stopped and key not in self.jobs:
                        # Back in line ahead of its peers, keeping how long it has waited.
                        job.resumed = True
                        if outcome == "busy":
                            job.waiting = True
                            job.waiting_since = job.waiting_since or time.time()
                        self.jobs[key] = job
                    self.condition.notify_all()
            self._emit_state(job.project_dir, job.asset_id)

    def _source_packets(self, source: str, signature: str, signal) -> int:
        from .media_helpers import count_video_packets

        key = (os.path.normcase(source), signature)
        with self.condition:
            cached = self._packet_counts.get(key)
        if cached:
            return cached
        packets = count_video_packets(source, cancel_event=signal, low_priority=True)
        if packets > 0:
            with self.condition:
                self._packet_counts[key] = packets
                while len(self._packet_counts) > 256:
                    self._packet_counts.popitem(last=False)
        return packets

    def _check_frames(self, source: str, temp: str, packets: int, signal) -> None:
        """Every frame the source decodes to is in the proxy. Packets usually equal
        frames; an edit list (a stream-copy trim) drops some, so on a mismatch the source
        is decoded once and compared again."""
        from .media_helpers import count_decoded_frames, count_video_packets

        produced = count_video_packets(temp)
        if produced == packets:
            return
        decoded = count_decoded_frames(source, cancel_event=signal, low_priority=True)
        if decoded <= 0 or produced != decoded:
            raise BuildFailed("frame_mismatch", f"proxy has {produced} frames, source {decoded or packets}")

    def _encode_to(self, raw: dict, source: str, signature: str, temp: str, preset: str, signal, *,
                   low_priority: bool, progress=None) -> None:
        from .media_helpers import encode_proxy, probe_video_size, proxy_encode_timeout_seconds, \
            resolve_source_color_interpretation

        packets = self._source_packets(source, signature, signal)
        if packets <= 0:
            raise BuildFailed("probe_failed", "the source's video packets could not be counted")
        width = int(raw.get("width") or 0)
        height = int(raw.get("height") or 0)
        if width <= 0 or height <= 0:
            width, height = probe_video_size(source)
        kwargs = {}
        if progress is not None:
            kwargs["progress_callback"] = lambda done: progress(min(done, packets), packets)
        encode = self._encode or encode_proxy
        encode(
            source, temp,
            preset_id=preset, width=width, height=height, frame_count=packets,
            input_color=resolve_source_color_interpretation(_color_subject(raw), source),
            timeout=proxy_encode_timeout_seconds(preset, packets, width, height),
            cancel_event=signal, low_priority=low_priority, **kwargs,
        )
        self._check_frames(source, temp, packets, signal)

    def _build(self, job: _Job, settings: dict, signal: _CancelSignal) -> None:
        from .media_helpers import MediaOperationCancelled

        if not self._resolved(job.project_dir):
            raise _Drop()
        data = _read_document(job.project_dir)
        _remember_document(job.project_dir, data)
        raw = _raw_asset(data, job.asset_id)
        if raw is None or str(raw.get("asset_type") or "") != "video":
            raise _Drop()
        source = _asset_source_path(job.project_dir, raw)
        signature = source_signature(source) if source else ""
        root = proxies_root(job.project_dir, create=True)
        if not signature or not root:
            raise _Drop()
        sidecar = read_sidecar(root, job.asset_id)
        if sidecar.get("source_rev") != signature:
            # The source changed after the decision: decide again first.
            self.enqueue(job.project_dir, [job.asset_id], reason=job.reason, rebuild=job.rebuild,
                         force=job.force)
            raise _Drop()
        if not wants_proxy(sidecar.get("decision", "")):
            raise _Drop()
        if not job.rebuild and _ready_file(sidecar, _listing(root)):
            raise _Drop()  # queued twice; the first build is current
        preset = settings["preset"]
        temp = os.path.join(root, f"{job.asset_id}.{uuid.uuid4().hex}.tmp.mp4")
        try:
            try:
                self._encode_to(raw, source, signature, temp, preset, signal, low_priority=True)
            except MediaOperationCancelled:
                reason = signal.reason or "cancel"
                if reason in ("busy", "preempt"):
                    raise _Requeue(reason)
                raise _Drop()
            except BuildFailed as exc:
                self._record_failure(job, root, signature, exc.code)
                return
            except Exception as exc:
                code = "timeout" if exc.__class__.__name__ == "TimeoutExpired" else "encode_failed"
                logger.warning("Proxy encode failed for asset %s: %s", job.asset_id, str(exc)[:300])
                self._record_failure(job, root, signature, code)
                return
            self.publish(job.project_dir, job.asset_id, temp, signature, preset, signal=signal)
        finally:
            _remove_quietly(temp)

    def _record_failure(self, job: _Job, root: str, signature: str, code: str) -> None:
        with self._asset_lock(job.project_dir, job.asset_id):
            sidecar = read_sidecar(root, job.asset_id)
            if sidecar.get("source_rev") != signature:
                return
            sidecar.update({"state": STATE_FAILED, "error_code": code, "epoch": self.next_epoch()})
            write_sidecar(root, job.asset_id, sidecar)

    def publish(self, project_dir: str, asset_id: str, temp: str, signature: str, preset: str, *,
                signal=None) -> str:
        """Under the asset's cache lock: re-check the project and the source, move the
        encode into place under a fresh rev, write the sidecar, and retire the previous
        file. Returns the new rev; raises ``_Drop`` when the result is stale."""
        from .atomic_io import atomic_replace

        root = proxies_root(project_dir, create=True)
        if not root:
            raise _Drop()
        with self._asset_lock(project_dir, asset_id):
            if signal is not None and signal.discards_result():
                raise _Drop()
            if not self._resolved(project_dir):
                raise _Drop()
            data = _read_document(project_dir)
            raw = _raw_asset(data, asset_id)
            if raw is None:
                raise _Drop()
            source = _asset_source_path(project_dir, raw)
            if not source or source_signature(source) != signature:
                raise _Drop()
            sidecar = read_sidecar(root, asset_id)
            if sidecar.get("source_rev") not in (None, "", signature):
                raise _Drop()
            build = int(sidecar.get("build") or 0) + 1
            rev = compute_rev(signature, preset, build)
            name = proxy_file_name(asset_id, rev)
            final = os.path.join(root, name)
            atomic_replace(temp, final)
            previous = str(sidecar.get("file") or "")
            decision = sidecar.get("decision") or DECISION_UNDECODABLE
            sidecar.update({
                "source_rev": signature, "decision": decision, "state": STATE_READY,
                "error_code": "", "build": build, "rev": rev, "file": name, "preset": preset,
                "bytes": os.path.getsize(final), "built_at": datetime.now().isoformat(timespec="seconds"),
                "epoch": self.next_epoch(),
            })
            sidecar.pop("held", None)
            write_sidecar(root, asset_id, sidecar)
            if previous and previous != name and matches_asset_proxy_file(asset_id, previous):
                self._remove_or_defer(os.path.join(root, previous))
        return rev

    def _remove_or_defer(self, path: str) -> bool:
        """Remove a superseded proxy file, or retry it later (a browser may hold it)."""
        if _remove_quietly(path):
            return True
        with self.condition:
            self.deferred_deletes.add(path)
        return False

    def _retry_deferred_deletes(self) -> None:
        """Retry superseded files, each under its asset's lock, and never the file a
        sidecar names now."""
        with self.condition:
            paths = list(self.deferred_deletes)
        for path in paths:
            name = os.path.basename(path)
            root = os.path.dirname(path)
            match = _PROXY_FILE_RE.match(name)
            project_dir = os.path.dirname(os.path.dirname(root))
            if not match:
                with self.condition:
                    self.deferred_deletes.discard(path)
                continue
            with self._asset_lock(project_dir, match.group("id")):
                current = read_sidecar(root, match.group("id")).get("file") == name
                done = current or _remove_quietly(path)
            if done:
                with self.condition:
                    self.deferred_deletes.discard(path)

    # Removal and reconcile ------------------------------------------------

    def remove_asset_files(self, project_dir: str, asset_id: str) -> None:
        """Delete an asset's sidecar and proxy files. Takes no lock: callers may already
        hold the asset's cache lock. A sidecar that cannot be removed is blanked instead
        (never deferred: the same name is reused); a proxy file is retried later."""
        asset_id = _safe_asset_id(asset_id)
        root = proxies_root(project_dir)
        if not asset_id or not root:
            return
        self.cancel(project_dir, [asset_id])
        for name in _names(root):
            path = os.path.join(root, name)
            if name == f"{asset_id}.json":
                if not _remove_quietly(path):
                    try:
                        write_sidecar(root, asset_id, {})
                    except OSError:
                        logger.warning("A proxy sidecar could not be cleared")
            elif matches_asset_proxy_file(asset_id, name):
                self._remove_or_defer(path)
        _forget_listing(root)

    def delete_proxies(self, project_dir: str, asset_ids: Iterable[str]) -> dict:
        """The user's Delete: stop their jobs, then remove each proxy under its lock. An
        asset that needs a proxy is held (nothing automatic rebuilds it until someone
        asks); a manual proxy of a playable original goes back to playing the original."""
        ids = [asset_id for asset_id in (_safe_asset_id(value) for value in asset_ids) if asset_id]
        self.cancel(project_dir, ids)
        root = proxies_root(project_dir)
        removed = {"count": 0, "bytes": 0}
        if not root:
            return removed
        for asset_id in ids:
            with self._asset_lock(project_dir, asset_id):
                sidecar = read_sidecar(root, asset_id)
                count = 0
                for name in [name for name in _names(root) if matches_asset_proxy_file(asset_id, name)]:
                    path = os.path.join(root, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    self._remove_or_defer(path)
                    count += 1
                    removed["bytes"] += size
                removed["count"] += count
                if sidecar and count:
                    sidecar.update({"state": STATE_NONE, "rev": "", "file": "", "bytes": 0,
                                    "error_code": "", "epoch": self.next_epoch()})
                    if sidecar.get("decision") == DECISION_MANUAL:
                        sidecar["decision"] = sidecar.pop("manual_from", "") or DECISION_DECODABLE
                        sidecar.pop("held", None)
                    else:
                        sidecar["held"] = True
                    write_sidecar(root, asset_id, sidecar)
            self._emit_state(project_dir, asset_id)
        _forget_listing(root)
        return removed

    def mark_runtime_undecodable(self, project_dir: str, asset_id: str, *, variant: str = "original",
                                 rev: str = "") -> bool:
        """A browser failed to decode what the server allowed (or a proxy). The original
        gets ``decision: runtime`` and a build; a failing proxy is marked failed."""
        asset_id = _safe_asset_id(asset_id)
        root = proxies_root(project_dir, create=True)
        if not asset_id or not root:
            return False
        with self._asset_lock(project_dir, asset_id):
            sidecar = read_sidecar(root, asset_id)
            if variant == "proxy":
                if not rev or sidecar.get("rev") != rev:
                    return False
                sidecar.update({"state": STATE_FAILED, "error_code": "proxy_undecodable",
                                "epoch": self.next_epoch()})
                write_sidecar(root, asset_id, sidecar)
            else:
                if wants_proxy(sidecar.get("decision", "")):
                    return False
                try:
                    data = _read_document(project_dir)
                except Exception:
                    return False
                raw = _raw_asset(data, asset_id)
                source = _asset_source_path(project_dir, raw) if raw else ""
                signature = source_signature(source) if source else ""
                if not signature or str(raw.get("asset_type") or "") != "video":
                    return False
                sidecar.update({"source_rev": signature, "decision": DECISION_RUNTIME, "state": STATE_NONE,
                                "build": int(sidecar.get("build") or 0), "epoch": self.next_epoch()})
                sidecar.pop("held", None)
                write_sidecar(root, asset_id, sidecar)
        if variant != "proxy":
            self.enqueue(project_dir, [asset_id], reason=REASON_AUTO)
        self._emit_state(project_dir, asset_id)
        return True

    def reconcile(self, project_dir: str, video_assets: Iterable[tuple[str, str]],
                  known_asset_ids: Iterable[str]) -> dict:
        """Queue non-trashed video assets (``(asset_id, source_path)``) whose sidecar is
        missing or stale, sweep orphaned and superseded files, retry deferred deletes."""
        root = proxies_root(project_dir, create=True)
        if not root:
            return {"queued": 0, "swept": 0}
        pkey = self.project_key(project_dir)
        with self.condition:
            busy = {asset_id for key_pkey, asset_id in self.inline if key_pkey == pkey}
        listing = _listing(root)
        stale = []
        for asset_id, source in video_assets:
            if asset_id in busy:
                continue
            stamp = listing.sidecars.get(asset_id) if listing is not None else None
            sidecar = read_sidecar(root, asset_id, stamp) if stamp else {}
            signature = source_signature(source) if source else ""
            if not signature:
                continue
            if sidecar.get("source_rev") != signature or not sidecar.get("decision") \
                    or _wants_build(sidecar, listing):
                stale.append(asset_id)
        queued = self.enqueue(project_dir, stale, reason=REASON_AUTO) if stale else 0
        swept = self.sweep(project_dir, set(known_asset_ids))
        self._retry_deferred_deletes()
        return {"queued": queued, "swept": swept}

    def sweep(self, project_dir: str, known_asset_ids: set[str]) -> int:
        """Remove files of assets the project no longer has, superseded proxy files, and
        temporaries no job in flight owns. Files newer than ``ORPHAN_GRACE_SECONDS`` are
        left alone: their asset may have been committed after the caller read the list."""
        root = proxies_root(project_dir)
        if not root:
            return 0
        pkey = self.project_key(project_dir)
        with self.condition:
            busy_ids = {asset_id for key_pkey, asset_id in self.inline if key_pkey == pkey}
            if self.current is not None and self.current.pkey == pkey:
                busy_ids.add(self.current.asset_id)
        swept = 0
        for name in _names(root):
            path = os.path.join(root, name)
            temp = _TEMP_FILE_RE.match(name) or _SIDECAR_TEMP_RE.match(name)
            if temp:
                if temp.group("id") not in busy_ids and _older_than(path, 3600):
                    swept += int(_remove_quietly(path))
                continue
            match = _PROXY_FILE_RE.match(name) or _SIDECAR_RE.match(name)
            if not match:
                continue
            asset_id = match.group("id")
            if asset_id not in known_asset_ids:
                if asset_id in busy_ids or not _older_than(path, ORPHAN_GRACE_SECONDS):
                    continue
                if name.endswith(".json"):
                    swept += int(_remove_quietly(path))
                else:
                    swept += int(self._remove_or_defer(path))
                continue
            if _PROXY_FILE_RE.match(name):
                with self._asset_lock(project_dir, asset_id):
                    if read_sidecar(root, asset_id).get("file") != name:
                        swept += int(self._remove_or_defer(path))
        if swept:
            _forget_listing(root)
        return swept

    # Save Video inline ----------------------------------------------------

    def build_inline(self, project_dir: str, asset_id: str, *, interrupted: Callable[[], bool],
                     progress: Callable[[int, int], None] | None = None) -> str:
        """Build a fresh take's proxy before its node returns. Preempts a worker build in
        flight and never waits for idle (ComfyUI runs one prompt at a time, so nothing can
        compete). On an interrupt, a failure, or a Build now in flight it hands the job to
        the worker: a take is never failed or held up by its proxy. Returns the rev or ""."""
        from .media_helpers import MediaOperationCancelled

        try:
            data = _read_document(project_dir)
            _remember_document(project_dir, data)
            raw = _raw_asset(data, asset_id)
            if raw is None or str(raw.get("asset_type") or "") != "video":
                return ""
            extension = os.path.splitext(str(raw.get("path") or ""))[1]
            decision = decision_for_save_params(_inline_params(raw), extension)
            if decision is None:
                self.enqueue(project_dir, [asset_id])
                return ""
            source = _asset_source_path(project_dir, raw)
            signature = source_signature(source) if source else ""
            root = proxies_root(project_dir, create=True)
            if not signature or not root:
                return ""
            # A take's decision comes from the settings it was just encoded with.
            with self._asset_lock(project_dir, asset_id):
                previous = read_sidecar(root, asset_id)
                sidecar = {**previous, "source_rev": signature, "decision": decision, "state": STATE_NONE,
                           "build": int(previous.get("build") or 0), "epoch": self.next_epoch()}
                if previous.get("source_rev") != signature:
                    sidecar.update({"rev": "", "preset": "", "error_code": ""})
                write_sidecar(root, asset_id, sidecar)
            settings = resolve_settings(data.get("metadata"))
            if not wants_proxy(decision) or settings["timing"] == TIMING_MANUAL:
                self._emit_state(project_dir, asset_id)
                return ""
        except Exception:
            logger.warning("Take proxy could not start; handing it to the worker", exc_info=True)
            self.enqueue(project_dir, [asset_id])
            return ""

        key = (self.project_key(project_dir), asset_id)
        signal = _InlineSignal(self, interrupted)
        temp = os.path.join(root, f"{asset_id}.{uuid.uuid4().hex}.tmp.mp4")
        rev = ""
        handoff = True
        locked = False
        with self.condition:
            self.inline.add(key)
        self._emit_state(project_dir, asset_id)
        try:
            # Wait for the worker to let go, but never behind a Build now and never past
            # an interrupt.
            while True:
                if signal.is_set():
                    raise MediaOperationCancelled("interrupted")
                with self.condition:
                    if self.current is not None and self.current.reason == REASON_NOW:
                        raise _HandOff()
                    self._preempt_locked()
                if self.encode_lock.acquire(timeout=0.2):
                    locked = True
                    break
            self._encode_to(raw, source, signature, temp, settings["preset"], signal,
                            low_priority=False, progress=progress)
            rev = self.publish(project_dir, asset_id, temp, signature, settings["preset"], signal=signal)
            handoff = False
        except (MediaOperationCancelled, _HandOff):
            logger.info("Take proxy for asset %s goes to the background worker", asset_id)
        except _Drop:
            handoff = False
        except Exception:
            logger.warning("Take proxy failed for asset %s; the worker will retry", asset_id, exc_info=True)
        finally:
            if locked:
                self.encode_lock.release()
            _remove_quietly(temp)
            with self.condition:
                self.inline.discard(key)
        if handoff:
            self.enqueue(project_dir, [asset_id])
        self._emit_state(project_dir, asset_id)
        return rev

    @staticmethod
    def _asset_lock(project_dir: str, asset_id: str):
        from .thumbnail_service import asset_cache_lock

        return asset_cache_lock(project_dir, asset_id)


def _wants_build(sidecar: dict, listing: "_Listing | None") -> bool:
    """Wants a proxy, has none, and nothing stops an automatic build (failure, a hold)."""
    return (wants_proxy(sidecar.get("decision", "")) and not _ready_file(sidecar, listing)
            and sidecar.get("state") != STATE_FAILED and not sidecar.get("held"))


def _reason_rank(reason: str) -> int:
    return {REASON_NOW: 0, REASON_USER: 1}.get(reason, 2)


def _ready_file(sidecar: dict, listing: "_Listing | None") -> str:
    name = str(sidecar.get("file") or "")
    if sidecar.get("state") != STATE_READY or not name or listing is None or name not in listing.files:
        return ""
    return name


def _names(root: str) -> list[str]:
    try:
        return os.listdir(root)
    except OSError:
        return []


def _older_than(path: str, seconds: float) -> bool:
    try:
        return time.time() - os.stat(path).st_mtime > seconds
    except OSError:
        return False


def _schedule_event(alias: str, event: dict) -> None:
    from .session_registry import schedule_project_event

    schedule_project_event(alias, event)


# ── Process worker ────────────────────────────────────────────────────────────

_worker: ProxyWorker | None = None
_worker_lock = threading.Lock()


def worker() -> ProxyWorker:
    """The process's worker; ``configure`` gives it the project resolver."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ProxyWorker()
            # A daemon thread dies mid-encode at exit; stop it first so ffmpeg closes.
            # Leftover temporaries are swept by the next reconcile.
            atexit.register(_worker.stop)
        return _worker


def configure(resolve: Callable[[str], str | None]) -> None:
    worker().resolve = resolve


def enqueue_after_commit(project_dir: str, asset_ids: Iterable[str]) -> None:
    """Every path that commits video assets calls this once after a successful save. It
    never probes and never raises."""
    try:
        worker().enqueue(project_dir, list(asset_ids), reason=REASON_AUTO)
    except Exception:
        logger.debug("Proxy enqueue failed", exc_info=True)


def remove_asset_files(project_dir: str, asset_id: str) -> None:
    try:
        worker().remove_asset_files(project_dir, asset_id)
    except Exception:
        logger.debug("Proxy removal failed", exc_info=True)


def proxy_file_path(project_dir: str, asset_id: str, rev: str) -> str:
    """The current ready proxy file of an asset at ``rev``, or "" (stale, absent, unsafe)."""
    asset_id = _safe_asset_id(asset_id)
    if not asset_id or not re.fullmatch(r"[0-9a-f]{16}", str(rev or "")):
        return ""
    root = proxies_root(project_dir)
    if not root:
        return ""
    sidecar = read_sidecar(root, asset_id)
    name = proxy_file_name(asset_id, rev)
    if sidecar.get("state") != STATE_READY or sidecar.get("rev") != rev or sidecar.get("file") != name:
        return ""
    path = os.path.join(root, name)
    return path if os.path.isfile(path) else ""
