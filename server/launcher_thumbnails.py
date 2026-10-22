"""The Launcher's project thumbnails: a short clip and a poster of each project's opening scene.

A derived cache, never project data. Files live in ``<project>/cache/launcher/`` as
``<key>.mp4`` plus ``<key>.jpg``, published atomically under the key of the state that
was actually rendered, so a project edited mid-render just shows as stale the next time
the Launcher asks. The key is ``sha1(GEN_VERSION | modified_at | scene_id | seconds | width)``:
every save moves ``modified_at``, which covers the clips, the scene length and its rate,
and ``scene_id`` covers the editor opening on another scene without a save.

One daemon worker renders one project at a time, newest request first. It waits while
ComfyUI's queue has work, so generation never competes with it, and gives a render up
(to run it again later) if generation starts mid-render. It never requeues a finished
job and never creates folders in a project: it loads with ``ensure_subdirs=False`` and
writes only under an existing ``cache/``. Like every load, its load pins the project's
storage, which schedules the usual collection of unreferenced components there.

Memory of failures and empty scenes is per process and per key, so a broken project is
retried only after it changes or ComfyUI restarts.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import re
import threading
import uuid
from collections import OrderedDict
from typing import Callable, Iterable

logger = logging.getLogger("sonder_editor")

GEN_VERSION = 1
WIDTH = 480
SECONDS = 4.0
CRF = 28
CACHE_DIR = "launcher"
KEY_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_QUEUE = 500
BUSY_POLL_SECONDS = 1.0
FRAMES_BETWEEN_BUSY_CHECKS = 8
RENDER_TIMEOUT_SECONDS = 300

# What a project's tile can be waiting for or showing.
QUEUED, RENDERING, READY, FAILED, NONE, STALE, MISSING = (
    "queued", "rendering", "ready", "failed", "none", "stale", "missing")


def thumbnail_key(modified_at, scene_id) -> str:
    raw = f"{GEN_VERSION}|{modified_at or ''}|{scene_id or ''}|{SECONDS}|{WIDTH}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def opening_scene(scenes: Iterable, editor_scene_id: str):
    """The scene the editor opens a project on: the SDK editor's last one if it still
    exists, else the first by order. Accepts scene dicts or ``Scene`` objects; ``None``
    for a project without scenes. The one authority for the Launcher's bar and tiles."""
    def field(scene, name, default):
        value = scene.get(name, default) if isinstance(scene, dict) else getattr(scene, name, default)
        return default if value is None else value

    scenes = [scene for scene in scenes or [] if str(field(scene, "scene_id", ""))]
    if not scenes:
        return None
    if editor_scene_id:
        for scene in scenes:
            if str(field(scene, "scene_id", "")) == editor_scene_id:
                return scene

    def order(scene):
        value = field(scene, "order", 0)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return min(scenes, key=order)


def editor_scene_id(folder: str) -> str:
    from . import sdk_templates

    try:
        return str(sdk_templates.read_editor_state(folder).get("scene_id") or "")
    except Exception:  # an unreadable position only loses the preselected scene
        logger.debug("SDK editor state for %s unreadable", folder, exc_info=True)
        return ""


def document_key(data: dict, folder: str) -> str:
    """The key of what a project's tile should show now, from its parsed document."""
    scene = opening_scene(data.get("scenes") if isinstance(data.get("scenes"), list) else [],
                          editor_scene_id(folder))
    scene_id = (scene.get("scene_id") if isinstance(scene, dict) else "") or ""
    return thumbnail_key(data.get("modified_at", ""), scene_id)


# ── Cache files ───────────────────────────────────────────────────────────────


def _cache_dir(project_dir: str, *, create: bool) -> str:
    """``<project>/cache/launcher``, or "" when ``cache/`` is missing or the path is unsafe.
    Only ever creates the last folder, under a ``cache/`` that already exists."""
    from .path_security import project_cache_path, project_cache_root

    if not project_cache_root(project_dir, must_exist=True):
        return ""
    path = project_cache_path(project_dir, CACHE_DIR, purpose="launcher thumbnail cache")
    if not path:
        return ""
    if create and not os.path.isdir(path):
        try:
            os.mkdir(path)
        except FileExistsError:
            pass
    return path


def published_keys(project_dir: str) -> list[str]:
    """Keys with both files present, newest first. Stats only; never creates anything."""
    folder = _cache_dir(project_dir, create=False)
    if not folder or not os.path.isdir(folder):
        return []
    try:
        names = set(os.listdir(folder))
    except OSError:
        return []
    keys = []
    for name in names:
        stem, _, ext = name.rpartition(".")
        if ext == "jpg" and KEY_RE.match(stem) and f"{stem}.mp4" in names:
            try:
                keys.append((os.stat(os.path.join(folder, name)).st_mtime_ns, stem))
            except OSError:
                continue
    return [key for _, key in sorted(keys, reverse=True)]


def media_path(project_dir: str, key: str, ext: str) -> str:
    """An existing published file, or "" (bad key, bad extension or not there)."""
    if not KEY_RE.match(str(key or "")) or ext not in ("jpg", "mp4"):
        return ""
    folder = _cache_dir(project_dir, create=False)
    path = os.path.join(folder, f"{key}.{ext}") if folder else ""
    return path if path and os.path.isfile(path) else ""


def _sweep_temporary(folder: str) -> None:
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.endswith(".tmp"):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def _remove_other_keys(folder: str, keep: str) -> None:
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        stem, _, ext = name.rpartition(".")
        if ext in ("jpg", "mp4") and KEY_RE.match(stem) and stem != keep:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


# ── Rendering ─────────────────────────────────────────────────────────────────


class _NothingToShow(Exception):
    """The opening scene is empty or has no visible clips."""


def _frame_range(project, scene) -> tuple[int, int]:
    from .timeline_renderer import _visible_render_clips
    from .timeline_state import effective_scene_fps

    clips = _visible_render_clips(scene)
    if not clips:
        raise _NothingToShow()
    start = max(0, min(int(clip.timeline_start_frame) for clip in clips))
    end = max(int(clip.timeline_end_frame) for clip in clips)
    duration = int(getattr(scene, "duration_frames", 0) or 0)
    if duration > 0:
        end = min(end, duration)
    fps = effective_scene_fps(project, scene)
    end = min(end, start + max(1, round(SECONDS * fps)))
    if end <= start:
        raise _NothingToShow()
    return start, end


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def render_thumbnail(project, scene, folder: str, key: str, *,
                     check_idle: Callable[[], None] = lambda: None) -> tuple[str, str]:
    """Render ``scene`` to temporary ``.mp4`` and ``.jpg`` files in ``folder`` and return
    their paths; the caller publishes them. Raises ``_NothingToShow`` for an empty scene.
    Removes its own temporary files when it fails."""
    import cv2

    from .media_helpers import _color_args_for_encode, _run_ffmpeg_streaming_frames, get_ffmpeg_path
    from .timeline_renderer import _scene_resolution, iter_scene_frames
    from .timeline_state import effective_scene_fps

    start, end = _frame_range(project, scene)
    src_w, src_h = _scene_resolution(project, scene)
    width = WIDTH
    height = _even(WIDTH * src_h / src_w)
    middle = start + (end - start) // 2
    poster = {}

    def frames():
        source = iter_scene_frames(project, scene, start, end)
        try:
            for index, frame in enumerate(source, start=start):
                if (index - start) % FRAMES_BETWEEN_BUSY_CHECKS == 0:
                    check_idle()
                small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
                if index == middle:
                    poster["frame"] = small
                yield small
        finally:
            source.close()

    video_tmp = os.path.join(folder, f"{key}.{uuid.uuid4().hex}.mp4.tmp")
    cmd = [
        get_ffmpeg_path(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(effective_scene_fps(project, scene)), "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-pix_fmt", "yuv420p",
        *_color_args_for_encode("libx264", "yuv420p", width, height, video_tmp),
        "-movflags", "+faststart", "-f", "mp4", video_tmp,
    ]
    poster_tmp = os.path.join(folder, f"{key}.{uuid.uuid4().hex}.jpg.tmp")
    try:
        _run_ffmpeg_streaming_frames(cmd, frames(), timeout=RENDER_TIMEOUT_SECONDS, frame_count=end - start)
        if "frame" not in poster:
            raise RuntimeError("the poster frame was not rendered")
        ok, encoded = cv2.imencode(".jpg", cv2.cvtColor(poster["frame"], cv2.COLOR_RGB2BGR),
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("the poster could not be encoded")
        with open(poster_tmp, "wb") as handle:
            handle.write(encoded.tobytes())
    except BaseException:
        for path in (video_tmp, poster_tmp):
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    return video_tmp, poster_tmp


# ── Worker ────────────────────────────────────────────────────────────────────


def _comfy_queue_busy() -> bool:
    """Whether ComfyUI has a prompt running or waiting (``comfy_idle``, shared with
    proxy media). False when it can't be asked."""
    from .comfy_idle import comfy_queue_busy

    return comfy_queue_busy()


class _Stopped(Exception):
    """The worker is shutting down."""


class _Busy(Exception):
    """ComfyUI started work mid-render: abandon it and render again when idle."""


class ThumbnailWorker:
    """Jobs are folder names, resolved again when they run through ``resolve`` (the
    project routes' resolver), so a folder that stopped resolving, a link removed or
    links turned off, is dropped instead of written to."""

    def __init__(self, *, resolve: Callable[[str], str | None] | None = None,
                 queue_busy: Callable[[], bool] = _comfy_queue_busy,
                 render: Callable = render_thumbnail, sleep: Callable[[float], None] | None = None):
        self.resolve = resolve
        self.queue_busy = queue_busy
        self.render = render
        self.condition = threading.Condition()
        self.pending: OrderedDict[str, None] = OrderedDict()  # folder names, next first
        self.status: dict[str, dict] = {}  # folder -> {"state", "key"}; at most MAX_QUEUE
        self.swept: set[str] = set()
        self.thread: threading.Thread | None = None
        self.stopped = False
        self._sleep = sleep or (lambda seconds: self._wait(seconds))

    # Requests and status --------------------------------------------------

    def request(self, folders: list[str]) -> None:
        """Queue folders ahead of earlier requests, in the order given (the page sends
        the tiles in view, newest first). One already rendering is not queued again."""
        with self.condition:
            if self.stopped:
                return
            for folder in reversed(folders[:MAX_QUEUE]):
                current = self.status.get(folder, {})
                if current.get("state") == RENDERING:
                    continue
                self.pending[folder] = None
                self.pending.move_to_end(folder, last=False)
                self.status[folder] = {"state": QUEUED, "key": current.get("key", "")}
            while len(self.pending) > MAX_QUEUE:  # the oldest requests give way
                dropped, _ = self.pending.popitem(last=True)
                self.status.pop(dropped, None)
            self._trim_status()
            self.condition.notify_all()
        self.start()

    def _trim_status(self) -> None:
        """Caller holds the condition. Keeps memory bounded: queued and rendering
        entries stay; the oldest settled results are forgotten first."""
        settled = [f for f, s in self.status.items() if s.get("state") not in (QUEUED, RENDERING)]
        for folder in settled[:max(0, len(self.status) - MAX_QUEUE)]:
            self.status.pop(folder, None)

    def describe(self, folder: str, project_dir: str, key: str = "") -> dict:
        """``{state, key, shown}`` for a tile. ``key`` is what it should show now when the
        caller knows it; ``shown`` is the newest published key, which the tile displays."""
        keys = published_keys(project_dir)
        shown = keys[0] if keys else ""
        with self.condition:
            memory = dict(self.status.get(folder, {}))
        if key and key in keys:
            return {"state": READY, "key": key, "shown": key}
        if memory.get("state") in (QUEUED, RENDERING):
            state = memory["state"]
        elif key and memory.get("key") == key and memory.get("state") in (FAILED, NONE):
            state = memory["state"]
        elif not key:
            state = memory.get("state") or (READY if shown else MISSING)
        else:
            state = STALE if shown else MISSING
        return {"state": state, "key": key or memory.get("key", ""), "shown": shown}

    # Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        with self.condition:
            if self.thread is not None or self.stopped:
                return
            self.thread = threading.Thread(target=self._run, name="sonder-launcher-thumbnails", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        with self.condition:
            self.stopped = True
            self.pending.clear()
            self.condition.notify_all()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _wait(self, seconds: float) -> None:
        with self.condition:
            if not self.stopped:
                self.condition.wait(seconds)

    def wait_idle(self) -> None:
        """Before a job: wait while ComfyUI has work. Nothing is open meanwhile."""
        while not self.stopped and self.queue_busy():
            self._sleep(BUSY_POLL_SECONDS)
        if self.stopped:
            raise _Stopped()

    def check_idle(self) -> None:
        """During a render: never wait with media and ffmpeg open (its timeout keeps
        running, and the sources stay locked); give the render up instead."""
        if self.stopped:
            raise _Stopped()
        if self.queue_busy():
            raise _Busy()

    def _run(self) -> None:
        while True:
            with self.condition:
                while not self.pending and not self.stopped:
                    self.condition.wait()
                if self.stopped:
                    return
                folder, _ = self.pending.popitem(last=False)
            try:
                self.wait_idle()
                self._process(folder)
            except _Stopped:
                return
            except _Busy:
                with self.condition:  # back to the front: it was next before ComfyUI started
                    if not self.stopped and folder not in self.pending:
                        self.pending[folder] = None
                        self.pending.move_to_end(folder, last=False)
                    self.status[folder] = {"state": QUEUED, "key": self.status.get(folder, {}).get("key", "")}
            except Exception:  # one project never stops the worker
                logger.exception("Launcher thumbnail worker failed for %s", folder)
                self._set(folder, FAILED, self.status.get(folder, {}).get("key", ""))

    def _set(self, folder: str, state: str, key: str) -> None:
        with self.condition:
            self.status[folder] = {"state": state, "key": key}

    def _forget(self, folder: str) -> None:
        with self.condition:
            self.status.pop(folder, None)

    # One job --------------------------------------------------------------

    def _process(self, folder: str) -> None:
        from .project_manager import load_project, read_project_document

        project_dir = self.resolve(folder) if self.resolve else None
        if not project_dir:  # no longer a project this server may read
            self._forget(folder)
            return
        try:
            key = document_key(read_project_document(project_dir), folder)
        except FileNotFoundError:
            self._forget(folder)
            return
        if key in published_keys(project_dir):
            self._set(folder, READY, key)
            return
        cache = _cache_dir(project_dir, create=True)
        if not cache:  # no cache/ folder: nothing may be created, so there is no tile
            self._set(folder, FAILED, key)
            return
        if project_dir not in self.swept:
            _sweep_temporary(cache)
            self.swept.add(project_dir)
        self._set(folder, RENDERING, key)
        project = load_project(project_dir, ensure_subdirs=False)
        # Publish under what was actually loaded, which may be newer than the request,
        # keyed from the loaded document itself so the listing computes the same key.
        raw = getattr(project, "_raw_data", None) or {}
        key = document_key(raw, folder)
        chosen = opening_scene(raw.get("scenes") if isinstance(raw.get("scenes"), list) else [], editor_scene_id(folder))
        scene_id = chosen.get("scene_id") if isinstance(chosen, dict) else ""
        scene = next((s for s in project.scenes if scene_id and s.scene_id == scene_id), None)
        if scene is None:
            scene = opening_scene(project.scenes, "")
        if scene is None:
            self._set(folder, NONE, key)
            return
        self._set(folder, RENDERING, key)
        temporary: list[str] = []
        try:
            try:
                # Real paths, taken before rendering, so this worker can still remove its
                # own temporary files after the folder's link is removed mid-render.
                real_cache = os.path.realpath(cache)
                temporary = [os.path.join(real_cache, os.path.basename(path)) for path in
                             self.render(project, scene, cache, key, check_idle=self.check_idle)]
            except _NothingToShow:
                self._set(folder, NONE, key)
                return
            if self.resolve(folder) != project_dir or not os.path.isfile(os.path.join(project_dir, "project.json")):
                self._forget(folder)  # deleted, unlinked or untrusted while rendering
                return
            from .atomic_io import atomic_replace

            video_tmp, poster_tmp = temporary
            atomic_replace(video_tmp, os.path.join(cache, f"{key}.mp4"))
            atomic_replace(poster_tmp, os.path.join(cache, f"{key}.jpg"))
            temporary = []
            _remove_other_keys(cache, key)
            self._set(folder, READY, key)
        except (_Stopped, _Busy):
            raise
        except Exception:
            logger.warning("Launcher thumbnail for %s failed", folder, exc_info=True)
            self._set(folder, FAILED, key)
        finally:
            for path in temporary:
                try:
                    os.remove(path)
                except OSError:
                    pass


_worker: ThumbnailWorker | None = None
_worker_lock = threading.Lock()


def worker() -> ThumbnailWorker:
    """The process's worker; ``configure`` gives it the project resolver."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ThumbnailWorker()
            # A daemon thread dies mid-render at exit; stop it first so ffmpeg and the
            # sources close. Leftover temporary files are swept on the next run.
            atexit.register(_worker.stop)
        return _worker


def configure(resolve: Callable[[str], str | None]) -> None:
    worker().resolve = resolve
