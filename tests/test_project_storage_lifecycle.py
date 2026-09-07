import copy
import gc
import threading
import time
from pathlib import Path

import pytest

from server import project_manager as pm, project_storage as ps
from server.project_storage_lifecycle import StorageMaintenanceWorker
from server.timeline_state import Asset, GenerationJob


def sample(tmp_path):
    p = pm.create_project("Leases", base_dir=str(tmp_path))
    p.assets = [Asset(asset_id="a", generation_params={"prompt": "original"})]
    p.generation_queue = [GenerationJob(job_id="j", prompt_sections=[{"prompt": "original"}])]
    ps.stage_prompt_history(p, [{"hash": "old", "future": "kept"}])
    pm.save_project(p, notify=False)
    return p


def test_detached_copied_readers_keep_original_bytes_until_released(tmp_path):
    p = sample(tmp_path)
    stale = pm.load_project(p.project_dir)
    asset = copy.deepcopy(stale.assets[0])
    job = copy.deepcopy(stale.generation_queue[0])
    asset_path = Path(p.project_dir) / asset._generation_descriptor["path"]
    job_path = Path(p.project_dir) / job._frozen_descriptor["path"]
    del stale
    p.assets[0].generation_params = {"prompt": "new"}
    p.generation_queue = []
    pm.save_project(p, notify=False)
    pm.save_project(p, notify=False)
    ps.collect_unreferenced_components(p.project_dir)
    assert asset_path.is_file() and job_path.is_file()
    assert asset.generation_params == {"prompt": "original"}
    assert ps.hydrate_job(p, job).prompt_sections == [{"prompt": "original"}]
    del asset, job
    gc.collect()
    removed = ps.collect_unreferenced_components(p.project_dir)
    assert asset_path.name in removed and job_path.name in removed


def test_stale_history_index_pins_transitive_entries(tmp_path):
    p = sample(tmp_path)
    stale = pm.load_project(p.project_dir)
    descriptor = stale._raw_data["storage"]["components"]["prompt_history"]
    index = ps.read_component(p.project_dir, "prompt_history", descriptor)
    child = Path(p.project_dir) / index["entries"][0]["path"]
    for i in range(3):
        ps.stage_prompt_history(p, [{"new": i}])
        pm.save_project(p, notify=False)
    ps.collect_unreferenced_components(p.project_dir)
    assert ps.read_prompt_history(stale) == [{"hash": "old", "future": "kept"}]
    assert child.is_file()
    del stale
    gc.collect()
    assert child.name in ps.collect_unreferenced_components(p.project_dir)


def test_failed_publication_does_not_retarget_reader_lease(tmp_path, monkeypatch):
    p = sample(tmp_path)
    stale = pm.load_project(p.project_dir)
    lease = stale.assets[0]._storage_lease
    stale.assets[0].generation_params = {"prompt": "unsaved"}
    monkeypatch.setattr(pm, "atomic_replace", lambda *_: (_ for _ in ()).throw(OSError("failed root")))
    with pytest.raises(OSError):
        pm.save_project(stale, notify=False)
    assert stale.assets[0]._storage_lease is lease
    assert pm.load_project(p.project_dir).assets[0].generation_params == {"prompt": "original"}


def test_maintenance_deduplicates_retries_and_stops():
    calls = []
    done = threading.Event()
    def collect(project):
        calls.append((project, time.monotonic()))
        if len(calls) == 1:
            raise OSError("transient failure")
        done.set()
    worker = StorageMaintenanceWorker(collect=collect, interval=0.03)
    for _ in range(20):
        worker.request("project")
    worker.start()
    try:
        assert done.wait(3)
    finally:
        worker.stop()
    assert len(calls) == 2
    assert calls[1][1] - calls[0][1] >= 0.025
    worker.request("ignored after shutdown")
    assert not worker.pending
    assert not worker.thread.is_alive()


def test_background_collection_is_eventual_without_inline_io(tmp_path):
    p = sample(tmp_path)
    orphan = ps.publish_component(p.project_dir, "history_entry", {"orphan": True})
    orphan_path = Path(p.project_dir) / orphan["path"]
    worker = StorageMaintenanceWorker(collect=ps.collect_unreferenced_components, interval=0.02)
    worker.request(p.project_dir)
    assert orphan_path.is_file()
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while orphan_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not orphan_path.exists()
    finally:
        worker.stop()


def test_process_exit_joins_active_maintenance(tmp_path):
    import subprocess
    import sys
    output = tmp_path / "joined.txt"
    script = '''
import pathlib, threading, time
from server import project_storage_lifecycle as lifecycle
entered = threading.Event()
def collect(_):
    entered.set()
    time.sleep(0.1)
    pathlib.Path(OUTPUT).write_text("finished")
lifecycle._worker = lifecycle.StorageMaintenanceWorker(collect=collect, interval=0)
lifecycle._worker.request("test")
lifecycle._worker.start()
assert entered.wait(2)
'''.replace("OUTPUT", repr(str(output)))
    subprocess.run([sys.executable, "-c", script], check=True, timeout=15)
    assert output.read_text() == "finished"
