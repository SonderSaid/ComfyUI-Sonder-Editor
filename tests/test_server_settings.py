"""Share Projects Between ComfyUI Servers: the settings store, the drive check and the route.

The lock it switches is covered with real processes in ``test_project_cross_process.py``.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import external_links, project_file_lock as file_lock, project_manager as pm, server_settings
from test_external_links import _DummyRequest, _load_route_module, _response_json, _route_handler


@pytest.fixture(autouse=True)
def _fresh_cache():
    server_settings.invalidate_cache()
    yield
    server_settings.invalidate_cache()


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    root = tmp_path / "user"
    output = tmp_path / "output"
    monkeypatch.setitem(sys.modules, "folder_paths", SimpleNamespace(
        get_user_directory=lambda: str(root), get_output_directory=lambda: str(output)))
    return root


def settings_file(user_dir):
    return user_dir / "sonder-editor" / "settings.json"


def write_settings(user_dir, text):
    path = settings_file(user_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    server_settings.invalidate_cache()
    return path


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_no_file_and_no_user_directory_give_the_defaults(user_dir, monkeypatch):
    assert server_settings.current() == {"allow_external_project_links": False,
                                         "share_projects_between_servers": False}
    monkeypatch.setitem(sys.modules, "folder_paths", SimpleNamespace(get_user_directory=lambda: ""))
    server_settings.invalidate_cache()
    assert server_settings.sharing_enabled() is False


def test_writing_one_setting_keeps_the_other_and_unknown_keys(user_dir):
    write_settings(user_dir, json.dumps({"version": 1, "allow_external_project_links": True, "later": [1]}))
    server_settings.update({server_settings.SHARING_KEY: True})
    assert json.loads(settings_file(user_dir).read_text(encoding="utf-8")) == {
        "version": 1, "allow_external_project_links": True, "later": [1],
        "share_projects_between_servers": True}
    server_settings.update({server_settings.LINKS_KEY: False})
    assert server_settings.current() == {"allow_external_project_links": False,
                                         "share_projects_between_servers": True}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"', ""])
def test_an_unreadable_file_keeps_trust_closed_and_protection_on(user_dir, content, caplog):
    write_settings(user_dir, content)
    assert server_settings.links_enabled() is False
    assert server_settings.sharing_enabled() is True
    assert external_links.is_enabled() is False


def test_a_cold_start_on_an_unreadable_file_protects_too(user_dir, monkeypatch):
    path = write_settings(user_dir, json.dumps({"share_projects_between_servers": False}))
    real_open = open

    def refuse(file, *args, **kwargs):
        if os.fspath(file) == str(path):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse)
    server_settings.invalidate_cache()
    assert server_settings.sharing_enabled() is True
    assert server_settings.links_enabled() is False


def test_a_write_never_replaces_a_file_it_cannot_read(user_dir):
    path = write_settings(user_dir, "{broken")
    with pytest.raises(server_settings.SettingsUnreadable):
        server_settings.update({server_settings.LINKS_KEY: True})
    assert path.read_text(encoding="utf-8") == "{broken"


def test_a_deleted_file_gives_the_defaults_and_a_fixed_one_is_read_again(user_dir):
    path = write_settings(user_dir, json.dumps({"share_projects_between_servers": True}))
    assert server_settings.sharing_enabled() is True
    path.unlink()
    server_settings.invalidate_cache()
    assert server_settings.sharing_enabled() is False
    write_settings(user_dir, "{broken")
    assert server_settings.sharing_enabled() is True
    write_settings(user_dir, json.dumps({"share_projects_between_servers": False}))
    assert server_settings.sharing_enabled() is False


def test_another_writers_change_is_seen_once_the_cache_expires(user_dir, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(server_settings.time, "monotonic", lambda: clock["now"])
    write_settings(user_dir, json.dumps({"share_projects_between_servers": False}))
    assert server_settings.sharing_enabled() is False
    settings_file(user_dir).write_text(json.dumps({"share_projects_between_servers": True}), encoding="utf-8")
    assert server_settings.sharing_enabled() is False  # cached
    clock["now"] += server_settings.CACHE_TTL_SEC + 0.01
    assert server_settings.sharing_enabled() is True


def test_a_read_that_started_before_a_write_cannot_publish_its_older_values(user_dir, monkeypatch):
    write_settings(user_dir, json.dumps({"share_projects_between_servers": False}))
    real_read = server_settings._read_document
    read_started, release_read = threading.Event(), threading.Event()

    def slow_read():
        result = real_read()
        if threading.current_thread().name == "old-reader":
            read_started.set()
            release_read.wait(5)
        return result

    monkeypatch.setattr(server_settings, "_read_document", slow_read)
    seen = []
    reader = threading.Thread(name="old-reader", target=lambda: seen.append(server_settings.sharing_enabled()))
    reader.start()
    assert read_started.wait(5)
    writer = threading.Thread(target=lambda: server_settings.update({server_settings.SHARING_KEY: True}))
    writer.start()
    writer.join(0.2)
    assert writer.is_alive()  # a write waits for the read it would race
    release_read.set()
    reader.join(5)
    writer.join(5)
    assert seen == [False] and server_settings.sharing_enabled() is True


@pytest.mark.parametrize("value, links, sharing", [
    ("false", False, True), (1, False, True), (None, False, True), (True, True, True), (False, False, False)])
def test_only_a_literal_true_trusts_and_only_a_boolean_lowers_protection(user_dir, value, links, sharing):
    write_settings(user_dir, json.dumps({"allow_external_project_links": value,
                                         "share_projects_between_servers": value}))
    assert server_settings.current() == {"allow_external_project_links": links,
                                         "share_projects_between_servers": sharing}


def test_a_miss_does_not_wait_for_a_write_in_progress(user_dir):
    write_settings(user_dir, json.dumps({"share_projects_between_servers": False}))
    assert server_settings.sharing_enabled() is False
    server_settings._publish(server_settings.current())
    server_settings._snapshot["expires"] = 0.0  # expired, as on the event loop a second later
    with server_settings._lock:  # a write is replacing the file
        started = time.monotonic()
        assert server_settings.sharing_enabled() is False
        assert time.monotonic() - started < 0.1
        assert server_settings._snapshot["expires"] == 0.0  # answered, not published


def test_unknown_settings_are_refused(user_dir):
    with pytest.raises(ValueError):
        server_settings.update({"something_else": True})
    assert not settings_file(user_dir).exists()


# ---------------------------------------------------------------------------
# The real setting switches the real lock (every lock test elsewhere patches the getter)
# ---------------------------------------------------------------------------

def test_the_written_setting_is_what_the_save_obeys(user_dir, tmp_path):
    project = pm.create_project("Real", base_dir=str(tmp_path / "projects"))
    lock_file = Path(project.project_dir) / "project.lock"
    pm.save_project(project, notify=False)
    assert not lock_file.exists()
    server_settings.update({server_settings.SHARING_KEY: True})
    pm.save_project(project, notify=False)
    assert lock_file.exists()
    holder = file_lock.ProjectFileLock(project.project_dir)
    assert holder.try_acquire() == file_lock.HELD
    try:
        with pytest.raises(file_lock.ProjectBusyError):
            pm.save_project(project, notify=False, lock_wait=0.2)
        assert pm.project_held_elsewhere(project.project_dir) is True
        server_settings.update({server_settings.SHARING_KEY: False})
        pm.save_project(project, notify=False, lock_wait=0.2)  # off: the holder is not waited for
        assert pm.project_held_elsewhere(project.project_dir) is False
    finally:
        holder.release()


def test_off_never_waits_behind_this_process_s_own_save(user_dir, tmp_path):
    project = pm.create_project("Busy", base_dir=str(tmp_path / "projects"))
    with pm._project_write_lock(project.project_dir):  # a save of this process is running
        started = time.monotonic()
        assert pm.project_held_elsewhere(project.project_dir) is False
        assert time.monotonic() - started < 0.1


# ---------------------------------------------------------------------------
# The drive check
# ---------------------------------------------------------------------------

def test_the_drive_check_passes_on_a_real_folder_and_leaves_nothing(tmp_path):
    (tmp_path / f"{file_lock.PROBE_PREFIX}unrelated").write_text("keep", encoding="utf-8")
    assert file_lock.probe_locks(str(tmp_path)) == (file_lock.PROBE_OK, "")
    assert [p.name for p in tmp_path.iterdir()] == [f"{file_lock.PROBE_PREFIX}unrelated"]


def _scripted(monkeypatch, answers):
    """``_raw_lock`` answers in order; ``None`` means the lock was taken."""
    answers = list(answers)
    monkeypatch.setattr(file_lock, "_raw_lock", lambda fd: answers.pop(0))
    monkeypatch.setattr(file_lock, "_unlock", lambda fd: None)


NO_LOCKS = 50 if os.name == "nt" else errno.ENOSYS
CONFLICT = 33 if os.name == "nt" else errno.EWOULDBLOCK
UNCLEAR = 5 if os.name == "nt" else errno.ENOLCK


@pytest.mark.parametrize("answers, outcome", [
    ([None, CONFLICT, None], file_lock.PROBE_OK),
    ([NO_LOCKS], file_lock.PROBE_UNSUPPORTED),
    ([None, None], file_lock.PROBE_UNSUPPORTED),          # the lock is not enforced
    ([UNCLEAR], file_lock.PROBE_FAILED),
    ([None, UNCLEAR], file_lock.PROBE_FAILED),            # an unclear error is not a conflict
    ([None, CONFLICT, CONFLICT], file_lock.PROBE_FAILED), # not free again after release
])
def test_only_a_real_conflict_and_a_clean_retake_pass(tmp_path, monkeypatch, answers, outcome):
    _scripted(monkeypatch, answers)
    assert file_lock.probe_locks(str(tmp_path))[0] == outcome
    assert list(tmp_path.iterdir()) == []


def test_a_folder_the_check_cannot_write_fails_the_check(tmp_path):
    outcome, detail = file_lock.probe_locks(str(tmp_path / "missing"))
    assert outcome == file_lock.PROBE_FAILED and "missing" in detail


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@pytest.fixture
def route(user_dir, tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    projects = tmp_path / "projects"
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(projects))
    checked = []
    real = file_lock.probe_locks

    def probe(directory):
        checked.append(directory)
        return real(directory)

    monkeypatch.setattr(file_lock, "probe_locks", probe)
    return SimpleNamespace(
        get=_route_handler(module, "GET", "/sonder-editor/server-settings"),
        put=_route_handler(module, "PUT", "/sonder-editor/server-settings"),
        projects=projects, checked=checked)


def put(route, body, remote="127.0.0.1"):
    response = asyncio.run(route.put(_DummyRequest(body=body, remote=remote)))
    return response.status, _response_json(response)


def test_turning_sharing_on_checks_the_projects_folder_and_get_matches_put(route, user_dir):
    status, body = put(route, {"share_projects_between_servers": True})
    assert status == 200
    assert body == {"allow_external_project_links": False, "share_projects_between_servers": True,
                    "can_manage": True}
    assert route.checked == [os.path.realpath(route.projects)] and route.projects.is_dir()
    assert _response_json(asyncio.run(route.get(_DummyRequest()))) == body
    put(route, {"share_projects_between_servers": True})  # already on: not checked again
    assert len(route.checked) == 1
    assert put(route, {"share_projects_between_servers": False})[0] == 200
    assert len(route.checked) == 1  # turning it off never checks


@pytest.mark.parametrize("body", [{}, {"share_projects_between_servers": "yes"}, [], {"other": True},
                                  {"allow_external_project_links": True, "share_projects_between_servers": 1}])
def test_a_body_without_a_boolean_setting_is_refused(route, user_dir, body):
    assert put(route, body)[0] == 400
    assert not settings_file(user_dir).exists()


@pytest.mark.parametrize("outcome, status, code", [
    (file_lock.PROBE_UNSUPPORTED, 409, "locks_unsupported"),
    (file_lock.PROBE_FAILED, 500, "lock_check_failed"),
])
def test_a_failed_check_leaves_everything_off_and_names_no_path(route, user_dir, monkeypatch, outcome, status, code):
    monkeypatch.setattr(file_lock, "probe_locks", lambda directory: (outcome, f"{directory}: secret detail"))
    got, body = put(route, {"allow_external_project_links": True, "share_projects_between_servers": True})
    assert (got, body["code"]) == (status, code)
    assert str(route.projects) not in body["error"] and "secret" not in body["error"]
    assert not settings_file(user_dir).exists()
    assert server_settings.current() == {"allow_external_project_links": False,
                                         "share_projects_between_servers": False}


def test_a_remote_client_may_share_but_not_trust_and_a_mixed_refusal_writes_nothing(route, user_dir):
    lan = "192.168.1.20"
    status, body = put(route, {"allow_external_project_links": True, "share_projects_between_servers": True}, lan)
    assert (status, body["code"]) == (403, "not_local")
    assert route.checked == [] and not settings_file(user_dir).exists()
    status, body = put(route, {"share_projects_between_servers": True}, lan)
    assert status == 200 and body["share_projects_between_servers"] is True and body["can_manage"] is False
    assert route.checked == [os.path.realpath(route.projects)]  # only the projects folder


def test_a_failed_settings_write_names_no_path(route, user_dir, monkeypatch):
    def fail(*_args, **_kwargs):
        raise PermissionError(13, "denied", str(settings_file(user_dir)))

    monkeypatch.setattr(server_settings, "atomic_replace", fail)
    status, body = put(route, {"allow_external_project_links": False})
    assert status == 500 and str(user_dir) not in body["error"] and "settings.json" not in body["error"]


def test_an_unreadable_settings_file_is_not_replaced(route, user_dir):
    path = write_settings(user_dir, "{broken")
    status, body = put(route, {"allow_external_project_links": False})
    assert (status, body["code"]) == (500, "settings_unreadable")
    assert path.read_text(encoding="utf-8") == "{broken"
    get = _response_json(asyncio.run(route.get(_DummyRequest())))
    assert (get["allow_external_project_links"], get["share_projects_between_servers"]) == (False, True)
