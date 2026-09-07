"""Single-server reader leases and deferred immutable-component maintenance.

No model is retained by the registry. Publication and lease acquisition use the
project manager's lock; finalizers only enqueue work, never touch the filesystem.
"""
import copy
import atexit
import logging
import os
import threading
import time
import weakref

logger = logging.getLogger("sonder_editor")
_leases = weakref.WeakValueDictionary()
_leases_lock = threading.Lock()


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
    _worker.stop()


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


async def stop_storage_maintenance(app):
    import asyncio
    await asyncio.to_thread(_worker.stop)
