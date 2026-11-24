"""Two real processes on one project: no save or sweep may break it or lose a save.

Every ordering here is forced by flag files (``cross_process_worker.py``). A barrier
that is never met fails the test.
"""
import asyncio
import errno
import gc
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from server import project_commit, project_file_lock as file_lock, project_manager as pm, project_storage as ps
from server import project_storage_lifecycle as lifecycle
from server.project_commit import created_ids_since, snapshot_item_ids
from server.project_file_lock import PROJECT_BUSY_MESSAGE, ProjectBusyError
from server.timeline_state import Asset
from test_project_mutation_pipeline_backend import _load_route_module

WORKER = Path(__file__).with_name("cross_process_worker.py")
TIMEOUT = 400


class Peers:
    def __init__(self, flags):
        self.flags = flags
        self.flags.mkdir()
        self.running = []

    def start(self, role, *args):
        result = self.flags / f"result-{len(self.running)}"
        process = subprocess.Popen(
            [sys.executable, str(WORKER), str(self.flags), role, *map(str, args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "CROSS_PROCESS_RESULT": str(result)})
        process.result_file = result
        self.running.append(process)
        return process

    def finish(self, process):
        out, err = process.communicate(timeout=TIMEOUT)
        assert process.returncode == 0, out + err
        result = process.result_file
        return json.loads(result.read_text()) if result.exists() else None

    def raise_flag(self, name):
        (self.flags / name).touch()

    def wait(self, name, seconds=180):
        deadline = time.monotonic() + seconds
        while not (self.flags / name).exists():
            assert time.monotonic() < deadline, f"barrier {name} was never met"
            time.sleep(0.005)


@pytest.fixture
def peers(tmp_path):
    group = Peers(tmp_path / "flags")
    yield group
    for process in group.running:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            stream.close()


def make_project(tmp_path, assets=12):
    project = pm.create_project("Shared", base_dir=str(tmp_path))
    project.assets = [Asset(asset_id=f"a{i}", name=f"a{i}", path=f"media/a{i}.mp4",
                            generation_params={"prompt": f"a{i}", "workflow": {"n": i}})
                      for i in range(assets)]
    ps.stage_prompt_history(project, [{"hash": "first"}])
    for _ in range(3):
        pm.save_project(project, notify=False)
    project_dir = project.project_dir
    del project
    gc.collect()  # no lease left here, so what the setup superseded can go now
    ps.collect_unreferenced_components(project_dir, retention_seconds=0)
    return project_dir


def assert_whole(project_dir):
    """Loads, and every current and retained component is present and intact."""
    project = pm.load_project(project_dir)
    storage = project._raw_data["storage"]
    for group in ("components", "recovery_previous", "authoring_previous"):
        for name, descriptor in storage[group].items():
            if name == "prompt_history":
                ps._read_history_component(project_dir, descriptor)
            else:
                ps.read_component(project_dir, name, descriptor)
    return project


# ---------------------------------------------------------------------------
# The reproduced races. These three failed before the cross-process lock.
# ---------------------------------------------------------------------------

def test_save_between_the_collectors_read_and_its_scan(tmp_path, peers):
    project_dir = make_project(tmp_path)
    collector = peers.start("collect_paused_after_read", project_dir)
    saver = peers.start("save_after_collector_read", project_dir, "new")
    peers.finish(saver)
    peers.finish(collector)
    assert assert_whole(project_dir).assets[-1].generation_params["prompt"] == "take"


@pytest.mark.parametrize("reused", [False, True])
def test_collection_between_publication_and_root_replacement(tmp_path, peers, reused):
    project_dir = make_project(tmp_path)
    if reused:  # bytes an old orphan already holds: publication writes nothing new
        orphan = ps.publish_component(project_dir, ps.asset_component_name("take"),
                                      {"prompt": "take", "workflow": {"seed": "same"}}, format_version=3)
        old = time.time() - 7 * 86400
        os.utime(os.path.join(project_dir, orphan["path"]), (old, old))
    saver = peers.start("save_paused_before_replace", project_dir, "same")
    collector = peers.start("collect_after_publication", project_dir)
    peers.finish(collector)
    peers.finish(saver)
    assert assert_whole(project_dir).assets[-1].generation_params["prompt"] == "take"


def test_no_acknowledged_save_is_lost_between_processes(tmp_path, peers):
    project_dir = make_project(tmp_path, assets=40)
    writers = [peers.start("stress", project_dir, tag, 12) for tag in ("p", "q")]
    loader = peers.start("load_loop", project_dir)
    for name in ("ready_p", "ready_q", "ready_loader"):
        peers.wait(name)
    peers.raise_flag("go")
    results = [peers.finish(writer) for writer in writers]
    peers.raise_flag("stop")
    assert peers.finish(loader)["loads"] > 0
    acknowledged = {name for result in results for name in result["acknowledged"]}
    assert len(acknowledged) == 24
    assert acknowledged <= {asset.asset_id for asset in assert_whole(project_dir).assets}


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

@pytest.fixture
def held(tmp_path, peers):
    """A project another process holds the lock of."""
    project_dir = make_project(tmp_path)
    holder = peers.start("hold", project_dir)
    peers.wait("holding")
    return project_dir, holder


def test_a_held_project_refuses_the_save_and_publishes_nothing(held):
    project_dir, _ = held
    project = pm.load_project(project_dir)  # a load never waits for the holder
    project.assets.append(Asset(asset_id="take", generation_params={"prompt": "take"}))
    before = {path: path.read_bytes() for path in Path(project_dir).rglob("*")
              if path.is_file() and path.name != "project.lock"}
    # This process has never locked the project, so a first attempt must not read a
    # held lock as a filesystem without locks.
    started = time.monotonic()
    with pytest.raises(ProjectBusyError) as busy:
        pm.save_project(project, notify=False, lock_wait=0.3)
    assert 0.25 < time.monotonic() - started < 5
    assert isinstance(busy.value, PermissionError) and str(busy.value) == PROJECT_BUSY_MESSAGE
    assert {path: path.read_bytes() for path in before} == before
    assert {path for path in Path(project_dir).rglob("*") if path.is_file()} - before.keys() == {
        Path(project_dir) / "project.lock"}


def test_a_killed_holder_releases_the_project(held):
    project_dir, holder = held
    holder.kill()
    holder.wait()
    pm.save_project(pm.load_project(project_dir), notify=False, lock_wait=5)


def test_loads_here_do_not_wait_for_a_save_waiting_on_another_process(held, peers):
    project_dir, _ = held
    project = pm.load_project(project_dir)
    outcome = []

    def save():
        try:
            pm.save_project(project, notify=False, lock_wait=20)
            outcome.append("saved")
        except Exception as exc:  # noqa: BLE001 - reported by the assertion
            outcome.append(exc)

    saver = threading.Thread(target=save)
    saver.start()
    try:
        time.sleep(0.2)
        started = time.monotonic()
        for _ in range(3):
            pm.load_project(project_dir)
        pm.scan_projects(str(Path(project_dir).parent))
        assert time.monotonic() - started < 5
        assert saver.is_alive() and not outcome  # it was waiting the whole time
    finally:
        peers.raise_flag("release")
        saver.join(30)
    assert outcome == ["saved"]


def test_a_save_after_another_process_saved_conflicts(tmp_path, peers):
    project_dir = make_project(tmp_path)
    mine = pm.load_project(project_dir)
    base = mine.modified_at
    peers.finish(peers.start("save", project_dir, "seed"))
    mine.assets[0].name = "renamed here"
    with pytest.raises(pm.ProjectVersionConflict):
        pm.save_project(mine, expected_modified_at=base, notify=False)
    assert [asset.asset_id for asset in assert_whole(project_dir).assets][-1] == "other"


WINDOWS_CODES = [
    (1, file_lock.UNSUPPORTED),     # ERROR_INVALID_FUNCTION
    (50, file_lock.UNSUPPORTED),    # ERROR_NOT_SUPPORTED
    (33, file_lock.CONTENDED),      # ERROR_LOCK_VIOLATION
    (5, file_lock.CONTENDED),       # ERROR_ACCESS_DENIED
    (59, file_lock.CONTENDED),      # ERROR_UNEXP_NET_ERR: a passing condition while a peer holds it
    (87, file_lock.CONTENDED),      # ERROR_INVALID_PARAMETER
]
POSIX_CODES = [
    (errno.ENOTSUP, file_lock.UNSUPPORTED),
    (errno.EOPNOTSUPP, file_lock.UNSUPPORTED),
    (errno.ENOSYS, file_lock.UNSUPPORTED),
    (errno.EACCES, file_lock.CONTENDED),
    (errno.EAGAIN, file_lock.CONTENDED),
    (errno.ENOLCK, file_lock.CONTENDED),
    (errno.EINVAL, file_lock.CONTENDED),
]


@pytest.mark.parametrize("code, outcome", WINDOWS_CODES if os.name == "nt" else POSIX_CODES)
def test_only_a_filesystem_that_says_so_counts_as_having_no_locks(tmp_path, monkeypatch, code, outcome):
    if os.name == "nt":
        monkeypatch.setattr(file_lock, "_lock_file", lambda _fd: code)
    else:
        def refuse(*_):
            raise OSError(code, "injected")
        monkeypatch.setattr(file_lock.fcntl, "flock", refuse)
    lock = file_lock.ProjectFileLock(str(tmp_path))
    assert lock.try_acquire() == outcome
    lock.release()  # nothing held: a no-op
    monkeypatch.undo()
    again = file_lock.ProjectFileLock(str(tmp_path))
    assert again.try_acquire() == file_lock.HELD  # the refused attempt left nothing open
    again.release()


def test_a_filesystem_without_locks_saves_as_before_and_never_sweeps(tmp_path, monkeypatch, caplog):
    project_dir = make_project(tmp_path)
    orphan = ps.publish_component(project_dir, "history_entry", {"orphan": True}, format_version=3)
    monkeypatch.setattr(pm.ProjectFileLock, "try_acquire", lambda self: file_lock.UNSUPPORTED)
    with caplog.at_level(logging.WARNING, logger="sonder_editor"):
        for _ in range(2):
            pm.save_project(pm.load_project(project_dir), notify=False)
    assert sum("no file locks" in record.message for record in caplog.records) == 1
    assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == []
    assert (Path(project_dir) / orphan["path"]).is_file()


def test_the_lock_excludes_threads_and_a_second_instance(tmp_path):
    first, second = file_lock.ProjectFileLock(str(tmp_path)), file_lock.ProjectFileLock(str(tmp_path))
    assert first.try_acquire() == file_lock.HELD
    results = []
    thread = threading.Thread(target=lambda: results.append(second.try_acquire()))
    thread.start()
    thread.join()
    assert results == [file_lock.CONTENDED]
    first.release()
    assert second.try_acquire() == file_lock.HELD
    second.release()
    assert (tmp_path / "project.lock").read_bytes() == b""


def test_the_project_folder_can_be_copied_while_it_is_held(held, tmp_path):
    project_dir, _ = held
    shutil.copytree(project_dir, tmp_path / "copy")
    assert (tmp_path / "copy" / "project.lock").read_bytes() == b""


def test_a_lock_path_that_is_not_a_regular_file_is_refused(tmp_path, monkeypatch):
    (tmp_path / "project.lock").mkdir()
    opened = []
    monkeypatch.setattr(file_lock.os, "open", lambda *args, **_k: opened.append(args))
    with pytest.raises(OSError, match="not a regular file"):
        file_lock.ProjectFileLock(str(tmp_path)).try_acquire()
    assert not opened


def test_the_lock_file_is_never_followed_through_a_link(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("kept")
    project = tmp_path / "p"
    project.mkdir()
    try:
        (project / "project.lock").symlink_to(target)
    except OSError:
        pytest.skip("creating a symlink needs a privilege this account lacks")
    with pytest.raises(OSError):
        file_lock.ProjectFileLock(str(project)).try_acquire()
    assert target.read_text() == "kept"


def test_an_open_failure_is_not_reported_as_a_busy_project(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(file_lock.os, "open", denied)
    with pytest.raises(PermissionError) as raised:
        with pm._project_commit_lock(str(tmp_path)):
            pass
    assert not isinstance(raised.value, ProjectBusyError)
    assert str(tmp_path) not in str(raised.value)
    assert pm.project_held_elsewhere(str(tmp_path)) is False
    monkeypatch.undo()
    with pm._project_commit_lock(str(tmp_path)):  # the thread lock was let go
        pass


def test_a_lock_file_held_open_for_a_moment_is_retried(tmp_path, monkeypatch):
    real, attempts = os.open, []

    def briefly_denied(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(errno.EACCES, "antivirus")
        return real(*args, **kwargs)

    monkeypatch.setattr(file_lock.os, "open", briefly_denied)
    lock = file_lock.ProjectFileLock(str(tmp_path))
    assert lock.try_acquire() == file_lock.HELD
    lock.release()


def test_a_lock_file_this_account_cannot_write_still_locks(tmp_path, monkeypatch):
    (tmp_path / "project.lock").touch()
    real = os.open

    def read_only(path, flags, *args, **kwargs):
        if flags & os.O_RDWR:
            raise PermissionError(errno.EACCES, "another user's file")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(file_lock.os, "open", read_only)
    first, second = file_lock.ProjectFileLock(str(tmp_path)), file_lock.ProjectFileLock(str(tmp_path))
    assert first.try_acquire() == file_lock.HELD
    assert second.try_acquire() == file_lock.CONTENDED
    first.release()


def test_version_tokens_advance_when_the_clock_goes_back(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    project = pm.load_project(project_dir)
    stored = project.modified_at

    class Earlier(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(stored) - timedelta(hours=1)

    monkeypatch.setattr(pm, "datetime", Earlier)
    tokens = [stored]
    for _ in range(3):
        pm.save_project(project, expected_modified_at=tokens[-1], notify=False)
        tokens.append(project.modified_at)
    pm.save_project(project, notify=False)  # a bare save too
    tokens.append(project.modified_at)
    assert tokens == sorted(set(tokens))


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

DAY = ps.COMPONENT_RETENTION_SECONDS


class Clock:
    def __init__(self, monkeypatch):
        self.now = time.time()
        monkeypatch.setattr(ps.time, "time", lambda: self.now)

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def ledger(project_dir):
    path = Path(project_dir) / "state" / "retention.json"
    return json.loads(path.read_bytes())["unreferenced_since"] if path.exists() else {}


def orphan(project_dir, seed):
    descriptor = ps.publish_component(project_dir, "history_entry", {"orphan": seed}, format_version=3)
    return Path(project_dir) / descriptor["path"]


def test_collection_deletes_only_what_stayed_unused_for_the_whole_retention(tmp_path, clock):
    project_dir = make_project(tmp_path)
    expired = orphan(project_dir, 1)
    assert ps.collect_unreferenced_components(project_dir) == []
    assert expired.name in ledger(project_dir)
    clock.advance(DAY - 60)
    young = orphan(project_dir, 2)
    assert ps.collect_unreferenced_components(project_dir) == []
    clock.advance(61)
    assert ps.collect_unreferenced_components(project_dir) == [expired.name]
    assert not expired.exists() and young.exists()
    assert expired.name not in ledger(project_dir) and young.name in ledger(project_dir)
    clock.advance(DAY)
    assert ps.collect_unreferenced_components(project_dir) == [young.name]
    assert_whole(project_dir)


def test_an_expired_component_a_save_reuses_stays_with_its_history(tmp_path, clock):
    """The control orphan of the same age goes in the same sweep, so this is not
    collection being off."""
    project_dir = make_project(tmp_path)
    entries = [{"hash": "reused-a"}, {"hash": "reused-b"}]
    index = ps.publish_history(project_dir, entries, format_version=3)
    names = {Path(index["path"]).name} | {
        Path(child["path"]).name for child in ps.read_component(project_dir, "prompt_history", index)["entries"]}
    control = orphan(project_dir, "control")
    ps.collect_unreferenced_components(project_dir)
    assert names | {control.name} <= ledger(project_dir).keys()
    clock.advance(DAY + 1)

    project = pm.load_project(project_dir)
    ps.stage_prompt_history(project, entries)
    pm.save_project(project, notify=False)
    assert not names & ledger(project_dir).keys()
    for _ in range(2):  # dropped again, past both retained sets
        ps.stage_prompt_history(project, [{"hash": "later"}])
        pm.save_project(project, notify=False)
        pm.save_project(project, notify=False)
    del project
    gc.collect()
    assert ps.collect_unreferenced_components(project_dir) == [control.name]
    assert names <= ledger(project_dir).keys()
    clock.advance(DAY + 1)
    assert names <= set(ps.collect_unreferenced_components(project_dir))


def test_a_component_recreated_under_a_lingering_record_is_new(tmp_path, clock, monkeypatch):
    """A sweep that died between deleting a file and dropping its record."""
    project_dir = make_project(tmp_path)
    path = Path(project_dir) / ps.publish_component(
        project_dir, ps.asset_component_name("take"), {"prompt": "take", "workflow": {"k": 1}}, format_version=3)["path"]
    ps.collect_unreferenced_components(project_dir)
    clock.advance(DAY + 1)
    with monkeypatch.context() as patch:
        patch.setattr(ps, "_write_retention", lambda *_: (_ for _ in ()).throw(OSError("died here")))
        with pytest.raises(OSError, match="died here"):
            ps.collect_unreferenced_components(project_dir)
    assert path.name in ledger(project_dir) and not path.exists()

    project = pm.load_project(project_dir)
    project.assets.append(Asset(asset_id="take", generation_params={"prompt": "take", "workflow": {"k": 1}}))
    pm.save_project(project, notify=False)
    assert path.exists() and path.name not in ledger(project_dir)


def test_a_save_whose_root_replace_fails_only_restarts_the_delay(tmp_path, clock, monkeypatch):
    project_dir = make_project(tmp_path)
    params = {"prompt": "take", "workflow": {"k": 1}}
    path = Path(project_dir) / ps.publish_component(
        project_dir, ps.asset_component_name("take"), params, format_version=3)["path"]
    ps.collect_unreferenced_components(project_dir)
    clock.advance(DAY + 1)
    project = pm.load_project(project_dir)
    project.assets.append(Asset(asset_id="take", generation_params=dict(params)))
    with monkeypatch.context() as patch:
        patch.setattr(pm, "atomic_replace", lambda *_: (_ for _ in ()).throw(OSError("injected")))
        with pytest.raises(OSError, match="injected"):
            pm.save_project(project, notify=False)
    assert path.name not in ledger(project_dir)
    assert path.name not in ps.collect_unreferenced_components(project_dir)
    assert path.exists()


def test_a_save_that_cannot_clear_its_record_fails_before_the_root(tmp_path, clock, monkeypatch):
    project_dir = make_project(tmp_path)
    params = {"prompt": "take", "workflow": {"k": 1}}
    ps.publish_component(project_dir, ps.asset_component_name("take"), params, format_version=3)
    ps.collect_unreferenced_components(project_dir)
    root = (Path(project_dir) / "project.json").read_bytes()
    project = pm.load_project(project_dir)
    project.assets.append(Asset(asset_id="take", generation_params=dict(params)))
    monkeypatch.setattr(ps, "_write_retention", lambda *_: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError, match="injected"):
        pm.save_project(project, notify=False)
    assert (Path(project_dir) / "project.json").read_bytes() == root


def test_a_full_disk_can_still_be_freed(tmp_path, clock, monkeypatch, caplog):
    project_dir = make_project(tmp_path)
    expired = orphan(project_dir, 1)
    ps.collect_unreferenced_components(project_dir)
    clock.advance(DAY + 1)
    monkeypatch.setattr(ps, "_write_retention",
                        lambda *_: (_ for _ in ()).throw(OSError(errno.ENOSPC, "disk full")))
    with caplog.at_level(logging.WARNING, logger="sonder_editor"):
        assert ps.collect_unreferenced_components(project_dir) == [expired.name]
    assert not expired.exists() and not caplog.records


@pytest.mark.parametrize("group", ["recovery_previous", "authoring_previous"])
def test_each_retained_set_keeps_its_own_components(tmp_path, group):
    project_dir = make_project(tmp_path)
    path = Path(project_dir) / "project.json"
    data = json.loads(path.read_bytes())
    kept = ps.publish_history(project_dir, [{"hash": group}], format_version=3)
    data["storage"][group] = {**data["storage"][group], "prompt_history": kept}
    path.write_text(json.dumps(data), encoding="utf-8")
    control = orphan(project_dir, "control")
    assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == [control.name]
    assert (Path(project_dir) / kept["path"]).exists()
    for child in ps.read_component(project_dir, "prompt_history", kept)["entries"]:
        assert (Path(project_dir) / child["path"]).exists()


@pytest.mark.parametrize("damage", [
    b"not json", b"[]", b'{"v": 9, "unreferenced_since": {}}', b'{"v": 1, "unreferenced_since": []}',
    b'{"v": 1, "unreferenced_since": {"NAME": NaN}}'])
def test_a_damaged_retention_file_restarts_the_delay(tmp_path, clock, damage):
    project_dir = make_project(tmp_path)
    path = orphan(project_dir, 1)
    ps.collect_unreferenced_components(project_dir)
    clock.advance(DAY + 1)
    (Path(project_dir) / "state" / "retention.json").write_bytes(damage.replace(b"NAME", path.name.encode()))
    assert ps.collect_unreferenced_components(project_dir) == []
    assert path.exists() and ledger(project_dir)[path.name] == clock.now


def test_a_recorded_time_from_the_future_restarts(tmp_path, clock):
    project_dir = make_project(tmp_path)
    path = orphan(project_dir, 1)
    ps.collect_unreferenced_components(project_dir)
    clock.advance(-3600)  # the clock was set back
    ps.collect_unreferenced_components(project_dir)
    assert ledger(project_dir)[path.name] == clock.now


def test_a_component_in_use_again_loses_its_record(tmp_path, clock):
    project_dir = make_project(tmp_path)
    path = orphan(project_dir, 1)
    ps.collect_unreferenced_components(project_dir)
    path.unlink()
    ps.collect_unreferenced_components(project_dir)
    assert path.name not in ledger(project_dir)


def test_one_unreadable_history_entry_does_not_unprotect_the_others(tmp_path, clock):
    project_dir = make_project(tmp_path)
    project = pm.load_project(project_dir)
    ps.stage_prompt_history(project, [{"hash": "one"}, {"hash": "two"}])
    pm.save_project(project, notify=False)
    reader = pm.load_project(project_dir)  # leases the index of one and two
    descriptor = reader._raw_data["storage"]["components"]["prompt_history"]
    children = [Path(project_dir) / child["path"]
                for child in ps.read_component(project_dir, "prompt_history", descriptor)["entries"]]
    for hash_ in ("x", "y", "z"):
        ps.stage_prompt_history(project, [{"hash": hash_}])
        pm.save_project(project, notify=False)
    children[0].write_bytes(b"damaged")
    control = orphan(project_dir, "control")
    assert control.name in ps.collect_unreferenced_components(project_dir, retention_seconds=0)
    assert all(child.exists() for child in children)
    # The index itself: its children are unknown now, so nothing is deleted.
    (Path(project_dir) / descriptor["path"]).write_bytes(b"damaged")
    control = orphan(project_dir, "control")
    assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == []
    assert control.exists() and all(child.exists() for child in children)
    del reader


def test_a_reader_elsewhere_keeps_what_another_process_superseded(tmp_path, peers):
    project_dir = make_project(tmp_path)
    reader = pm.load_project(project_dir)
    assert peers.finish(peers.start("supersede", project_dir)) == []
    assert reader.assets[0].generation_params["prompt"] == "a0"
    with pytest.raises(pm.ProjectVersionConflict):  # and it cannot overwrite the newer one
        pm.save_project(reader, expected_modified_at=reader.modified_at, notify=False)


@pytest.mark.parametrize("change", ["supersede", "repack", "reuse"])
def test_a_reader_paused_after_the_root_gets_what_the_root_named(tmp_path, peers, change):
    if change == "repack":
        from test_component_migration import stored_project
        project, path, _history = stored_project(tmp_path)
        project_dir = project.project_dir
        expected = [asset.generation_params["prompt"] for asset in project.assets]
        del project
    else:
        project_dir = make_project(tmp_path)
        expected = [f"a{i}" for i in range(12)]
    gc.collect()
    reader = peers.start("reader_paused_before_children", project_dir)
    peers.wait("root_read")
    project = pm.load_project(project_dir)
    if change != "repack":
        project.assets[0].generation_params = {"prompt": "superseded"}
        ps.stage_prompt_history(project, [{"hash": "later"}])
    for _ in range(3):
        pm.save_project(project, notify=False)
    if change == "reuse":
        project.assets[0].generation_params = {"prompt": "a0", "workflow": {"n": 0}}
        pm.save_project(project, notify=False)
    del project
    gc.collect()
    ps.collect_unreferenced_components(project_dir)
    peers.raise_flag("replaced")
    result = peers.finish(reader)
    assert result["prompts"] == expected
    assert_whole(project_dir)


def test_a_vanishing_file_does_not_fail_a_load(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    state = Path(project_dir) / "state"
    (state / ("e" * 64 + ".json")).write_bytes(b"swept by another process")
    (state / "0123.tmp").write_bytes(b"another process's save")
    real = os.scandir
    asked = []

    class Vanishing:
        def __init__(self, entry):
            self.entry, self.name = entry, entry.name

        def stat(self, **kwargs):
            asked.append(self.name)
            if self.name.startswith("e" * 64) or self.name.endswith(".tmp"):
                raise FileNotFoundError(self.name)
            return self.entry.stat(**kwargs)

    class Listing:
        def __init__(self, path):
            self.entries = real(path)

        def __enter__(self):
            return (Vanishing(entry) for entry in self.entries)

        def __exit__(self, *_):
            self.entries.close()

    monkeypatch.setattr(ps.os, "scandir", Listing)
    assert len(pm.load_project(project_dir).assets) == 12
    assert "e" * 64 + ".json" in asked and "0123.tmp" not in asked
    # A referenced file that is gone still fails.
    monkeypatch.undo()
    descriptor = json.loads((Path(project_dir) / "project.json").read_bytes())["storage"]["components"][
        ps.asset_component_name("a0")]
    (Path(project_dir) / descriptor["path"]).unlink()
    with pytest.raises(ps.ProjectStorageError):
        pm.load_project(project_dir)


# ---------------------------------------------------------------------------
# What a read leaves behind
# ---------------------------------------------------------------------------

def run_maintenance_to_completion(monkeypatch, project_dir, action):
    """The real worker and sweep, with no interval."""
    swept = threading.Event()

    def collect(requested):
        try:
            ps.collect_unreferenced_components(requested)
        finally:
            swept.set()

    worker = lifecycle.StorageMaintenanceWorker(collect=collect, interval=0)
    monkeypatch.setattr(lifecycle, "_worker", worker)
    worker.start()
    try:
        action()
        assert swept.wait(20)
    finally:
        worker.stop()
    assert not worker.pending


@pytest.mark.parametrize("read", ["load", "scan", "thumbnail"])
def test_a_read_creates_no_file(tmp_path, monkeypatch, read):
    project_dir = make_project(tmp_path)
    for name in ("project.lock", os.path.join("state", "retention.json")):
        (Path(project_dir) / name).unlink(missing_ok=True)
    before = {path for path in Path(project_dir).rglob("*") if path.is_file()}

    def action():
        if read == "scan":
            pm.scan_projects(str(Path(project_dir).parent), detail=True)
            pm.load_project(project_dir)  # a scan schedules no sweep of its own
        else:
            pm.load_project(project_dir, ensure_subdirs=read == "load")

    run_maintenance_to_completion(monkeypatch, project_dir, action)
    assert {path for path in Path(project_dir).rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("with_orphan", [False, True])
def test_a_read_only_project_loads_and_sweeps_quietly(tmp_path, monkeypatch, caplog, with_orphan):
    project_dir = make_project(tmp_path)
    if with_orphan:
        orphan(project_dir, 1)
    real = os.open

    def read_only(path, flags, *args, **kwargs):
        if flags & (os.O_RDWR | os.O_WRONLY | os.O_CREAT) and str(path).startswith(project_dir):
            raise OSError(errno.EROFS, "read-only", str(path))
        return real(path, flags, *args, **kwargs)

    (Path(project_dir) / "project.lock").unlink()
    monkeypatch.setattr(file_lock.os, "open", read_only)
    with caplog.at_level(logging.WARNING, logger="sonder_editor"):
        run_maintenance_to_completion(monkeypatch, project_dir, lambda: pm.load_project(project_dir))
    assert not caplog.records


def test_a_busy_sweep_waits_for_a_later_one(held, caplog):
    project_dir, _ = held
    path = orphan(project_dir, 1)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sonder_editor"):
        assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == []
    assert time.monotonic() - started < pm.COMMIT_LOCK_WAIT_SECONDS / 2  # a sweep never waits
    assert path.exists() and not caplog.records


def test_a_file_open_elsewhere_is_skipped_and_the_rest_go(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    stuck, free = orphan(project_dir, 1), orphan(project_dir, 2)
    real = os.remove

    def remove(path):
        if os.path.basename(path) == stuck.name:
            raise PermissionError("open in another process")
        real(path)

    monkeypatch.setattr(ps.os, "remove", remove)
    assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == [free.name]
    assert stuck.name in ledger(project_dir)
    monkeypatch.undo()
    assert ps.collect_unreferenced_components(project_dir, retention_seconds=0) == [stuck.name]


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------

def test_a_finished_take_waits_for_the_other_process_and_keeps_everything(held, peers):
    project_dir, _ = held
    project = pm.load_project(project_dir)
    base, before = project.modified_at, snapshot_item_ids(project)
    project.assets.append(Asset(asset_id="take", path="media/take.mp4", generation_params={"prompt": "take"}))
    project.metadata["asset_folders"] = ["Takes"]  # the conflict merge does not copy this
    waits = []
    real = pm.save_project

    def save(*args, **kwargs):
        waits.append(1)
        if len(waits) == 2:
            peers.raise_flag("release")
        return real(*args, **kwargs, lock_wait=0.5)

    project_commit.save_project = save
    try:
        committed = project_commit.save_generated_project(
            project, base, created_ids=created_ids_since(before, project))
    finally:
        project_commit.save_project = real
    assert len(waits) >= 2 and committed is project
    stored = assert_whole(project_dir)
    assert stored.assets[-1].asset_id == "take" and stored.metadata["asset_folders"] == ["Takes"]


def test_a_finished_take_gives_up_after_one_deadline(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    project = pm.load_project(project_dir)
    clock, calls = [0.0], []
    monkeypatch.setattr(project_commit.time, "monotonic", lambda: clock[0])

    def busy(*_args, **_kwargs):
        calls.append(1)
        clock[0] += 10
        raise ProjectBusyError()

    monkeypatch.setattr(project_commit, "save_project", busy)
    with pytest.raises(ProjectBusyError):
        project_commit.save_generated_project(project, project.modified_at)
    assert clock[0] == project_commit.GENERATED_SAVE_BUSY_SECONDS == 120 and len(calls) == 12


def _busy_then_real(monkeypatch, times, *, conflict_first=False):
    """``project_commit.save_project``: optionally one conflict, then ``times`` busy, then real."""
    real, seen = pm.save_project, []

    def save(project, **kwargs):
        seen.append("save")
        if conflict_first and len(seen) == 1:
            other = pm.load_project(project.project_dir)
            other.name = "edited elsewhere"
            real(other, notify=False)
            raise pm.ProjectVersionConflict(project_dir=project.project_dir,
                                            expected_modified_at="", actual_modified_at="")
        if len(seen) <= times + conflict_first:
            raise ProjectBusyError()
        return real(project, notify=False, **kwargs)

    monkeypatch.setattr(project_commit, "save_project", save)
    return seen


def _with_take(project_dir):
    project = pm.load_project(project_dir)
    before = snapshot_item_ids(project)
    project.assets.append(Asset(asset_id="take", path="media/take.mp4", generation_params={"prompt": "take"}))
    return project, created_ids_since(before, project)


def test_a_take_without_a_base_waits_too(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    project, created = _with_take(project_dir)
    seen = _busy_then_real(monkeypatch, 2)
    project_commit.save_generated_project(project, "", created_ids=created)
    assert len(seen) == 3 and assert_whole(project_dir).assets[-1].asset_id == "take"


def test_a_take_merged_after_a_conflict_waits_too(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    project, created = _with_take(project_dir)
    seen = _busy_then_real(monkeypatch, 2, conflict_first=True)
    project_commit.save_generated_project(project, project.modified_at, created_ids=created)
    stored = assert_whole(project_dir)
    assert len(seen) == 4 and stored.name == "edited elsewhere" and stored.assets[-1].asset_id == "take"


def test_the_deadline_is_one_for_the_first_save_and_the_merge(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    project, created = _with_take(project_dir)
    clock, seen = [0.0], []
    monkeypatch.setattr(project_commit.time, "monotonic", lambda: clock[0])

    def save(*_args, **_kwargs):
        seen.append(1)
        clock[0] += 10
        if len(seen) == 10:  # a conflict once 100 of the 120 seconds are gone
            raise pm.ProjectVersionConflict(project_dir=project_dir, expected_modified_at="", actual_modified_at="")
        raise ProjectBusyError()

    monkeypatch.setattr(project_commit, "save_project", save)
    with pytest.raises(ProjectBusyError):
        project_commit.save_generated_project(project, project.modified_at, created_ids=created)
    assert clock[0] == 120


@pytest.mark.parametrize("busy_at", [1, 4])
def test_a_busy_sync_phase_returns_the_committed_project_on_any_attempt(monkeypatch, busy_at):
    """Attempt 4 is the last one, after three conflicts."""
    routes = _load_route_module(monkeypatch)
    committed, seen = object(), []

    def save(_project, **kwargs):
        seen.append(kwargs["lock_wait"])
        if len(seen) == busy_at:
            raise ProjectBusyError()
        raise pm.ProjectVersionConflict(project_dir="", expected_modified_at="", actual_modified_at="")

    monkeypatch.setattr(routes, "save_project", save)
    assert routes._save_versioned_sync_phase(object(), lambda _project: True, lambda: committed) is committed
    assert seen == [0] * busy_at


def test_the_purge_waits_in_full_once_it_has_deleted(tmp_path, monkeypatch):
    project_dir = make_project(tmp_path)
    routes = _load_route_module(monkeypatch)
    waits = []
    monkeypatch.setattr(routes, "save_project", lambda _project, **kwargs: waits.append(kwargs["lock_wait"]))
    monkeypatch.setattr(routes, "_sync_media_folder", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "_purge_expired_trashed_assets", lambda *_a, **_k: True)
    routes._sync_media_folder_versioned(pm.load_project(project_dir))
    assert waits == [0, pm.COMMIT_LOCK_WAIT_SECONDS]


def test_a_busy_project_answers_503_with_the_editor_headers(held, monkeypatch):
    routes = _load_route_module(monkeypatch)

    async def handler(_request):
        pm.save_project(pm.load_project(held[0]), notify=False, lock_wait=0)

    request = make_mocked_request("PUT", "/sonder-editor/project/Shared")
    response = asyncio.run(routes._project_error_middleware(request, handler))
    assert response.status == 503
    assert json.loads(response.body) == {"error": PROJECT_BUSY_MESSAGE, "code": "project_busy"}
    assert held[0] not in response.text
    assert "Content-Security-Policy" in response.headers


def test_a_busy_sync_returns_the_committed_project_and_purges_nothing(held, monkeypatch):
    project_dir, _ = held
    routes = _load_route_module(monkeypatch)
    media = Path(project_dir) / "media"
    media.mkdir(exist_ok=True)
    (media / "found.png").write_bytes(b"not an image")
    monkeypatch.setattr(routes, "_sync_media_folder",
                        lambda project, *_args, **_kwargs: bool(project.assets.append(Asset(asset_id="found")) or True))
    proxied, purged = [], []
    monkeypatch.setattr(routes.proxy_media, "enqueue_after_commit", lambda _dir, ids: proxied.append(ids))
    monkeypatch.setattr(routes, "_purge_expired_trashed_assets", lambda *_a, **_k: purged.append(1))

    def reload():
        project = pm.load_project(project_dir)
        project.assets[0].trashed_at = "2020-01-01T00:00:00"
        return project

    started = time.monotonic()
    synced = routes._sync_media_folder_versioned(reload(), reload_project=reload)
    assert time.monotonic() - started < 8  # opportunistic: no full wait
    assert "found" not in {asset.asset_id for asset in synced.assets}
    assert proxied == [[]] and purged == []
