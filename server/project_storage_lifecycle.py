"""Single-server reader leases and deferred immutable-component maintenance.

No model is retained by the registry. Publication and lease acquisition use the
project manager's lock; finalizers only enqueue work, never touch the filesystem.
"""
import copy
import atexit
import concurrent.futures
import contextvars
import functools
import logging
import os
import sys
import threading
import time
import weakref

logger = logging.getLogger("sonder_editor")
_leases = weakref.WeakValueDictionary()
_leases_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Project I/O executor
# ---------------------------------------------------------------------------
# `asyncio.to_thread` uses the event loop's DEFAULT executor, which ffmpeg
# thumbnailing, ffprobe, media streaming and per-chunk upload disk checks all share.
# A migrating save holds the per-project write lock for seconds, and every project
# route waiting on it parks a default-pool thread doing nothing: measured, an
# unrelated `to_thread` waited 0.578 s behind 24 concurrent project GETs while
# loop-tick latency stayed at zero. The loop was fine; the pool was not.
#
# This pool is owned HERE rather than in `routes`, which the tests reload ~251 times
# across 19 files — a module-level pool there would be re-created on every reload and
# never shut down, which is the mistake `TimelineExportManager` already makes.
_PROJECT_EXECUTOR: "concurrent.futures.ThreadPoolExecutor | None" = None
_PROJECT_EXECUTOR_LOCK = threading.Lock()
_PROJECT_EXECUTOR_STOPPED = False
PROJECT_EXECUTOR_THREAD_PREFIX = "sonder-project"


class ProjectExecutorStopped(RuntimeError):
    """Raised when project I/O is requested after the pool has been shut down."""


def _project_executor_size() -> int:
    """Deliberately the same formula asyncio uses for its default executor.

    The point of this pool is ISOLATION, not a different degree of parallelism.
    Project work parks on the per-project write lock, and threads parked on project
    A's lock still block project B's reads — so a smaller bound would make
    cross-project starvation worse than sharing the default pool does. It is also
    not the whole picture: `save_project` is entered from the ComfyUI prompt worker
    and the `sonder-bridge-*` daemon too, which hold the same lock without consuming
    this pool, so the pool can be fully parked behind a writer it cannot see.

    The workload is not purely lock-bound, and the next person sizing this should
    know it: the scene mutations batch reaches `_prepare_video_audio_asset`, and the
    streamed import/replace commits ffprobe, so a few pool threads can be doing real
    encode work rather than waiting. `durable_rules` bounds that to one media-I/O
    create per batch, which is why it does not dominate.

    Expiry: revisit if a measurement shows project I/O is CPU-bound rather than
    lock-bound, at which point a smaller, cheaper pool becomes the right answer.
    """
    return min(32, (os.cpu_count() or 1) + 4)


def project_executor() -> "concurrent.futures.ThreadPoolExecutor":
    """The pool that owns project loads, saves and creation.

    Callers hold `_PROJECT_EXECUTOR_LOCK`; see `_submit_project_io` for why.
    """
    global _PROJECT_EXECUTOR
    if _PROJECT_EXECUTOR_STOPPED:
        raise ProjectExecutorStopped(
            "the project I/O pool has been shut down; the server is stopping")
    if _PROJECT_EXECUTOR is None:
        _PROJECT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=_project_executor_size(),
            thread_name_prefix=PROJECT_EXECUTOR_THREAD_PREFIX,
        )
    return _PROJECT_EXECUTOR


def on_project_executor_thread() -> bool:
    return threading.current_thread().name.startswith(PROJECT_EXECUTOR_THREAD_PREFIX)


def start_project_executor() -> None:
    """Re-arm the pool after a shutdown, the way the maintenance worker is re-created.

    Separate from `project_executor()` so resurrection is always something a caller
    asked for. The pool itself is still built lazily on first use.
    """
    global _PROJECT_EXECUTOR_STOPPED
    with _PROJECT_EXECUTOR_LOCK:
        _PROJECT_EXECUTOR_STOPPED = False


# The frame that submitted the work a pool thread is currently running, as
# `(co_filename, f_lineno, co_name)`. `save_project` reads it so the `project_saved`
# diagnostic can name a route: where `save_project` is itself the submitted callable, its
# own immediate frame is the pool's runner in `concurrent/futures/thread.py`, which is
# true and useless.
#
# A ContextVar rather than a `threading.local()`: the pool reuses threads, and a value
# left set would attribute the NEXT task's save to this task's route — a confident wrong
# answer, worse than no answer. `copy_context().run()` restores the thread's own context
# when the task returns, so leakage is structurally impossible rather than guarded by a
# `finally` a later edit can drop.
_PROJECT_IO_CALLER = contextvars.ContextVar("sonder_project_io_caller", default=None)

# Frames that drive a coroutine but cannot name the code that wanted the work. In
# practice this is asyncio: `concurrent/futures` is listed for symmetry and is not
# reachable, since a pool thread never drives a coroutine's first `send()` and the
# re-entry guard in `run_project_io` refuses it before this is reached anyway.
_UNATTRIBUTABLE_FRAME_DIRS = (
    os.path.join("asyncio", ""),
    os.path.join("concurrent", "futures"),
)


def project_io_caller():
    """The frame that submitted this pool task, or None when it is not attributable.

    Returns the raw tuple. Formatting is the caller's, because the one consumer builds a
    string only when it is actually going to emit one.
    """
    return _PROJECT_IO_CALLER.get()


def _frame_identity(frame):
    """Reduce a frame to a tuple, or None when it cannot honestly name a submitter.

    Holding the frame itself would pin its locals alive, and `f_lineno` moves as the
    caller runs on, so this is captured eagerly and by value.

    A directly-awaited coroutine has its first `send()` driven by the awaiting frame,
    which is the route handler we want. Under `create_task`/`gather` it is driven by
    asyncio internals instead, and the route handler is not on this stack at all — so
    recording frame 1 there would name a frame inside `asyncio/events.py`, which is worse
    than reporting nothing because it reads like a plausible caller. Walking further up
    does not help either: every frame above it belongs to the loop, not to the request.
    """
    if frame is None:
        return None
    filename = frame.f_code.co_filename
    if any(marker in filename for marker in _UNATTRIBUTABLE_FRAME_DIRS):
        return None
    return (filename, frame.f_lineno, frame.f_code.co_name)


def _submit_project_io(func, args, kwargs, frame_info=None):
    """Resolve the pool and submit while holding the lock, as one step.

    Two shutdown races live in the gap between "which pool" and "submit". Resolving
    first and submitting after lets a request either build a SECOND pool that
    `on_cleanup` has already walked past and will never join, or hand work to a pool
    mid-shutdown and get an unhandled `cannot schedule new futures` as a 500. Doing
    both under the lock leaves exactly one outcome: a deliberate, catchable refusal.
    """
    # Each task carries its own context copy, so the submitter cannot outlive it.
    # `ThreadPoolExecutor.submit` propagates no context at all, unlike `asyncio.to_thread`,
    # which runs its target inside `copy_context()` — moving project I/O from one to the
    # other dropped that silently, and copying here restores it for every ContextVar, not
    # just this one. The copy is O(1): contexts share an immutable mapping.
    context = contextvars.copy_context()
    context.run(_PROJECT_IO_CALLER.set, frame_info)
    with _PROJECT_EXECUTOR_LOCK:
        return project_executor().submit(
            context.run, functools.partial(func, *args, **kwargs))


async def run_project_io(func, /, *args, **kwargs):
    """Run project filesystem work on the project pool, off the event loop.

    Bounded pools deadlock when work running inside them waits on work submitted to
    the same pool, so direct re-entry is refused rather than left to be discovered
    under load. Note the guard catches only that direct shape: work that blocks on a
    result another pool task must produce would still deadlock, and nothing here can
    see it. No such path exists today.
    """
    import asyncio

    if on_project_executor_thread():
        raise RuntimeError(
            "project-pool work must not resubmit to the project pool; a bounded pool "
            "waiting on itself deadlocks. Call the function directly — you are "
            "already off the event loop.")
    # Frame 1 is this coroutine's awaiter. Every call site is a direct `await` in a route
    # handler body, so that is the handler — see `_frame_identity` for the case where it
    # is not, and why that one records nothing instead. Guarded because this is a
    # diagnostic on the production save path: no reachable case has too shallow a stack,
    # but the analogous capture in `save_project` is inside a `try` and a real save must
    # not 500 over a field nobody asked for.
    try:
        submitter = _frame_identity(sys._getframe(1))
    except ValueError:
        submitter = None
    return await asyncio.wrap_future(
        _submit_project_io(func, args, kwargs, submitter))


def _shutdown_project_executor() -> None:
    """Idempotent, and refuses further work rather than silently rebuilding.

    `StorageMaintenanceWorker` in this module takes the same stance: once stopped it
    stays stopped, and `start_storage_maintenance` re-creates it deliberately and
    visibly. Silent resurrection would mean `on_cleanup` returning while a live,
    unowned pool it never joined is still running.
    """
    global _PROJECT_EXECUTOR, _PROJECT_EXECUTOR_STOPPED
    with _PROJECT_EXECUTOR_LOCK:
        _PROJECT_EXECUTOR_STOPPED = True
        executor, _PROJECT_EXECUTOR = _PROJECT_EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=True)


class StorageMaintenanceWorker:
    def __init__(self, collect=None, interval=30.0):
        self.collect = collect
        self.interval = interval
        self.condition = threading.Condition()
        self.pending = {}
        self.last_attempt = {}
        self.thread = None
        self.stopped = False

    def request(self, project_dir):
        with self.condition:
            if self.stopped:
                return
            now = time.monotonic()
            self.pending.setdefault(project_dir, max(now + self.interval, self.last_attempt.get(project_dir, 0) + self.interval))
            self.condition.notify_all()

    def start(self):
        with self.condition:
            if self.thread is not None or self.stopped:
                return
            self.thread = threading.Thread(target=self._run, name="sonder-storage-maintenance", daemon=True)
            self.thread.start()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.pending.clear()
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()

    def _run(self):
        while True:
            with self.condition:
                while not self.stopped:
                    if not self.pending:
                        self.condition.wait()
                        continue
                    project_dir, due = min(self.pending.items(), key=lambda pair: pair[1])
                    delay = due - time.monotonic()
                    if delay > 0:
                        self.condition.wait(delay)
                        continue
                    del self.pending[project_dir]
                    self.last_attempt[project_dir] = time.monotonic()
                    break
                else:
                    return
            try:
                if self.collect is None:
                    from .project_storage import collect_unreferenced_components
                    collect_unreferenced_components(project_dir)
                else:
                    self.collect(project_dir)
            except Exception:
                logger.exception("Frozen-component maintenance failed for %s; will retry", project_dir)
                self.request(project_dir)


_worker = StorageMaintenanceWorker()


def _stop_on_exit():
    # Standalone ComfyUI does not consistently call AppRunner.cleanup on Ctrl-C.
    # Retain aiohttp cleanup for embedding and join on normal process exit too.
    #
    # The worker is the load-bearing half: it is a daemon thread and nothing else
    # joins it. The pool is belt-and-braces only — by the time atexit runs, CPython
    # has already executed `threading._shutdown()`, which runs
    # `concurrent.futures.thread._python_exit` and joins every pool thread. Kept so
    # the two lifecycles are stopped from one place rather than relying on that
    # interpreter detail. Expiry: drop the pool call if the pool ever becomes daemon
    # threads, at which point this really would be the only thing joining it.
    _worker.stop()
    _shutdown_project_executor()


atexit.register(_stop_on_exit)


def _released(project_dir):
    _worker.request(project_dir)


class _DescriptorLease:
    def __init__(self, project_dir, name, descriptor):
        self.project_dir = project_dir
        self.name = name
        self.descriptor = copy.deepcopy(descriptor)
        weakref.finalize(self, _released, project_dir)

    def __deepcopy__(self, memo):
        return self

    def __copy__(self):
        return self


def pin_project(project):
    """Caller holds the project lock, before exposing a loaded/published model."""
    from .project_storage import storage_of, asset_component_name, job_component_name
    storage = storage_of(project._raw_data)
    if not storage:
        return
    root = os.path.normcase(os.path.realpath(project.project_dir))

    def acquire(name, descriptor):
        key = (root, name, descriptor["path"], descriptor["sha256"], descriptor["bytes"])
        with _leases_lock:
            lease = _leases.get(key)
            if lease is None:
                lease = _DescriptorLease(root, name, descriptor)
                _leases[key] = lease
            return lease

    project._storage_leases = tuple(acquire(name, descriptor) for name, descriptor in storage["components"].items())
    for asset in project.assets:
        descriptor = getattr(asset, "_generation_descriptor", None)
        asset._storage_lease = acquire(asset_component_name(asset.asset_id), descriptor) if descriptor else None
    for job in project.generation_queue:
        descriptor = getattr(job, "_frozen_descriptor", None)
        job._storage_lease = acquire(job_component_name(job.job_id), descriptor) if descriptor else None
    _worker.request(root)


def live_descriptors(project_dir):
    """Caller holds the project lock. Returned leases stay alive for the sweep."""
    root = os.path.normcase(os.path.realpath(project_dir))
    with _leases_lock:
        return [lease for lease in list(_leases.values()) if lease.project_dir == root]


async def start_storage_maintenance(app):
    global _worker
    if _worker.stopped:
        _worker = StorageMaintenanceWorker()
    _worker.start()
    # The embedded-restart case: a previous app shut the pool down and this startup
    # re-arms it. Explicit, mirroring the worker re-creation above, rather than the
    # pool quietly resurrecting itself under a late request.
    start_project_executor()


async def stop_storage_maintenance(app):
    import asyncio
    await asyncio.to_thread(_worker.stop)
    # Off the loop deliberately: `shutdown(wait=True)` joins whatever is in flight,
    # and an in-flight migrating save runs for seconds. Blocking the loop on it here
    # would reintroduce the exact stall this pool exists to bound.
    await asyncio.to_thread(_shutdown_project_executor)
