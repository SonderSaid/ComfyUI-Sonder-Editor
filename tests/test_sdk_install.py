"""The Sonder App installer (server/sdk_install.py) and its routes, with a fake pip.

* The plan: no pip → the manual commands; a pinned package at another version
  refuses; a dry run that would change an installed package refuses; the report
  is read from a file (UTF-8 metadata), never stdout; only the reported set is
  installed, with ``--no-deps``; pip runs with ComfyUI's interpreter flags.
* The record: the intent file exists while pip runs and is gone after the
  record is written; only pinned packages Sonder added are removable, with the
  version installed; added dependencies are listed and left in place; a
  pre-existing package is kept. An unfinished or partial install is reconciled:
  what got in is recorded, and an intent with nothing missing is dropped.
* Uninstall removes only recorded packages still at Sonder's version and needed
  by nothing else, asks about a reused proxy before stopping, and deletes the
  shortcut and record once nothing recorded is left.
* One job at a time; a reservation stops new Generates; the "still running"
  flag past ten minutes.
* The shortcut page probes the cross-origin ping route, and the routes:
  loopback only (forwarding headers refuse), LAN status without commands,
  uninstall refused while a run is unfinished or a Generate is in flight.

The live install and uninstall on the test venv is a Phase 4c check (plan).
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from server import sdk_install
from server.sdk_install import InstallRefused


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_user_directory=lambda: str(tmp_path / "user"),
        get_output_directory=lambda: str(tmp_path / "output"),
    ))
    sdk_install.reset_jobs_for_tests()
    yield tmp_path
    sdk_install.reset_jobs_for_tests()


class FakePip:
    """pip with an in-memory environment. ``report`` is what a dry run would add."""

    def __init__(self, installed=None, report=None, fail=(), pip_ok=True):
        self.installed = dict(installed or {})
        self.report = report if report is not None else [("comfy-sdk", "0.4.0"), ("comfy-api-proxy", "0.1.6")]
        self.fail = set(fail)
        self.pip_ok = pip_ok
        self.calls = []
        self.on_install = None

    def version(self, name):
        return self.installed.get(sdk_install.canonical(name), "")

    def __call__(self, args):
        self.calls.append(list(args))
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0 if self.pip_ok else 1, "pip 26", "")
        if "--dry-run" in args:
            if "dry" in self.fail:
                return subprocess.CompletedProcess(args, 1, "", "resolver exploded")
            # pip's report carries each package's description: non-ASCII, as the
            # real comfy-sdk and comfy-api-proxy ones are (→, ↔, box drawing).
            report = {"version": "1", "install": [
                {"metadata": {"name": n, "version": v, "description": "Graph → prompt ↔ proxy ┌─┐"}} for n, v in self.report]}
            path = args[args.index("--report") + 1]
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "install":
            if self.on_install:
                self.on_install()
            specs = args[3:]
            if "install" in self.fail:
                return subprocess.CompletedProcess(args, 1, "", "network down")
            if "install_partial" in self.fail:
                specs, failed = specs[:1], True
            else:
                failed = False
            for spec in specs:
                name, version = spec.split("==")
                self.installed[sdk_install.canonical(name)] = version
            if failed:
                return subprocess.CompletedProcess(args, 1, "", "disk full")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "uninstall":
            if "uninstall" in self.fail:
                return subprocess.CompletedProcess(args, 1, "", "file in use")
            for name in args[2:]:
                self.installed.pop(sdk_install.canonical(name), None)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def record(user_dir):
    path = user_dir / "user" / "sonder-editor" / "sdk_install.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def intent_path(user_dir):
    return user_dir / "user" / "sonder-editor" / "sdk_install.intent.json"


# ── Plan ─────────────────────────────────────────────────────────────────────

def test_without_pip_the_commands_to_run_by_hand_are_given(user_dir):
    pip = FakePip(pip_ok=False)
    with pytest.raises(InstallRefused) as exc:
        sdk_install.plan_install(pip, pip.version)
    assert exc.value.code == "manual"
    commands = exc.value.details["commands"]
    assert "-m pip install comfy-sdk==0.4.0 comfy-api-proxy==0.1.6" in commands["pip"]
    assert commands["uv"].startswith("uv pip install --python ")
    assert pip.calls == [["--version"]]


def test_a_pinned_package_at_another_version_is_left_alone(user_dir):
    pip = FakePip(installed={"comfy-sdk": "0.3.2"})
    with pytest.raises(InstallRefused) as exc:
        sdk_install.plan_install(pip, pip.version)
    assert exc.value.code == "pin_conflict"
    assert "comfy-sdk 0.3.2" in str(exc.value)
    assert not any("--dry-run" in call for call in pip.calls)


def test_a_dry_run_that_would_change_an_installed_package_refuses(user_dir):
    pip = FakePip(installed={"httpx": "0.27.0"},
                  report=[("comfy-sdk", "0.4.0"), ("httpx", "0.28.1")])
    with pytest.raises(InstallRefused) as exc:
        sdk_install.plan_install(pip, pip.version)
    assert exc.value.code == "would_change"
    assert exc.value.details["changes"] == [{"package": "httpx", "installed": "0.27.0", "would_install": "0.28.1"}]


def test_a_failed_or_unreadable_dry_run_refuses(user_dir):
    with pytest.raises(InstallRefused) as exc:
        pip = FakePip(fail={"dry"})
        sdk_install.plan_install(pip, pip.version)
    assert exc.value.code == "dry_run_failed" and exc.value.details["detail"] == "resolver exploded"

    class Garbled(FakePip):
        def __call__(self, args):
            if "--dry-run" in args:
                # A warning on stdout and a broken file: neither is the plan.
                with open(args[args.index("--report") + 1], "w", encoding="utf-8") as handle:
                    handle.write("not json")
                return subprocess.CompletedProcess(args, 0, "WARNING: Ignoring invalid distribution ~ip", "")
            return super().__call__(args)

    garbled = Garbled()
    with pytest.raises(InstallRefused) as exc:
        sdk_install.plan_install(garbled, garbled.version)
    assert exc.value.code == "dry_run_unreadable"


def test_the_dry_run_names_the_exact_pins_and_reports_to_a_file_it_removes():
    pip = FakePip()
    assert sdk_install.plan_install(pip, pip.version) == [("comfy-sdk", "0.4.0"), ("comfy-api-proxy", "0.1.6")]
    call = pip.calls[1]
    report = call[call.index("--report") + 1]
    assert call == ["install", "--dry-run", "--report", report, "--quiet", "--no-input",
                    "comfy-sdk==0.4.0", "comfy-api-proxy==0.1.6"]
    assert report != "-" and not os.path.exists(report)


def test_pip_runs_with_comfyuis_interpreter_flags_and_utf8(monkeypatch):
    seen = {}
    monkeypatch.setattr(sdk_install.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, env=kw["env"]))
    monkeypatch.setattr(sdk_install.sys, "flags", types.SimpleNamespace(isolated=0, no_user_site=1, ignore_environment=0))
    sdk_install.default_runner(["--version"])
    assert seen["cmd"] == [sys.executable, "-s", "-m", "pip", "--version"]
    assert seen["env"]["PYTHONUTF8"] == "1" and seen["env"]["PYTHONIOENCODING"] == "utf-8"
    assert ' -s -m pip install comfy-sdk==0.4.0' in sdk_install.manual_commands()["pip"]
    monkeypatch.setattr(sdk_install.sys, "flags", types.SimpleNamespace(isolated=1, no_user_site=1, ignore_environment=1))
    sdk_install.default_runner(["--version"])
    assert seen["cmd"][1:3] == ["-I", "-m"]


# ── Install ──────────────────────────────────────────────────────────────────

def test_install_adds_only_the_reported_set_and_records_what_it_added(user_dir):
    pip = FakePip(installed={"comfy-sdk": "0.4.0"},
                  report=[("comfy-api-proxy", "0.1.6"), ("Some_Dep", "1.2")])
    seen_intent = []
    pip.on_install = lambda: seen_intent.append(json.loads(intent_path(user_dir).read_text(encoding="utf-8")))
    shortcut_calls = []
    result = sdk_install.install(runner=pip, installed=pip.version,
                                 shortcut=lambda: shortcut_calls.append(1) or "X:/Sonder Launcher.html")
    assert pip.calls[-1] == ["install", "--no-deps", "--no-input", "comfy-api-proxy==0.1.6", "some-dep==1.2"]
    assert seen_intent and seen_intent[0]["packages"] == ["comfy-api-proxy==0.1.6", "some-dep==1.2"]
    assert not intent_path(user_dir).exists()
    saved = record(user_dir)
    # The pre-existing comfy-sdk is not Sonder's to remove.
    assert saved["installed_by_sonder"] == ["comfy-api-proxy"]
    assert saved["installed_versions"] == {"comfy-api-proxy": "0.1.6"}
    assert saved["added_dependencies"] == [{"name": "some-dep", "version": "1.2"}]
    assert result["ok"] is True and result["added"] == ["comfy-api-proxy 0.1.6", "some-dep 1.2"]
    assert shortcut_calls == [1]
    assert result["notes"] == ["Saved the Launcher shortcut, Sonder Launcher.html, in the sonder-projects folder."]


def test_install_with_nothing_to_add_writes_no_record(user_dir):
    pip = FakePip(installed={"comfy-sdk": "0.4.0", "comfy-api-proxy": "0.1.6"}, report=[])
    result = sdk_install.install(runner=pip, installed=pip.version)
    assert result["message"] == "Both packages were already installed."
    assert not any(call[:2] == ["install", "--no-deps"] for call in pip.calls)
    assert record(user_dir) is None


def test_a_failed_pip_install_leaves_the_intent_for_the_status_to_report(user_dir, monkeypatch):
    pip = FakePip(fail={"install"})
    with pytest.raises(InstallRefused) as exc:
        sdk_install.install(runner=pip, installed=pip.version)
    assert exc.value.code == "pip_failed" and exc.value.details["detail"] == "network down"
    assert intent_path(user_dir).exists() and record(user_dir) is None
    monkeypatch.setattr(sdk_install, "_version", pip.version)
    status = sdk_install.installer_status()
    assert status["interrupted"]["packages"] == ["comfy-sdk==0.4.0", "comfy-api-proxy==0.1.6"]
    assert "pip install comfy-sdk==0.4.0" in status["interrupted"]["commands"]["pip"]


def test_a_partial_install_records_what_got_in(user_dir):
    pip = FakePip(fail={"install_partial"})
    with pytest.raises(InstallRefused):
        sdk_install.install(runner=pip, installed=pip.version)
    # comfy-sdk got in before pip failed: it is Sonder's, so Uninstall can remove it.
    assert record(user_dir)["installed_by_sonder"] == ["comfy-sdk"]
    assert json.loads(intent_path(user_dir).read_text(encoding="utf-8"))["packages"] == ["comfy-sdk==0.4.0", "comfy-api-proxy==0.1.6"]
    assert sdk_install.reconcile_intent(pip.version) == [("comfy-api-proxy", "0.1.6")]


def test_an_interrupted_install_that_got_everything_in_is_settled_by_the_status(user_dir, monkeypatch):
    """ComfyUI stopped after pip finished but before the record was written."""
    pip = FakePip(installed={"comfy-sdk": "0.4.0", "comfy-api-proxy": "0.1.6"})
    intent_path(user_dir).parent.mkdir(parents=True, exist_ok=True)
    intent_path(user_dir).write_text(json.dumps({"packages": ["comfy-api-proxy==0.1.6"]}), encoding="utf-8")
    monkeypatch.setattr(sdk_install, "_version", pip.version)
    status = sdk_install.installer_status()
    assert status["interrupted"] is None and status["installed_by_sonder"] == ["comfy-api-proxy"]
    assert not intent_path(user_dir).exists()
    assert record(user_dir)["installed_versions"] == {"comfy-api-proxy": "0.1.6"}


def test_a_status_read_survives_an_unreadable_record(user_dir, monkeypatch):
    monkeypatch.setattr(sdk_install, "read_record", lambda: (_ for _ in ()).throw(PermissionError("locked")))
    status = sdk_install.installer_status()
    assert status["installed_by_sonder"] == [] and status["job"]["state"] == "idle"


def test_versions_that_do_not_match_after_pip_fail_the_install(user_dir):
    class Liar(FakePip):
        def __call__(self, args):
            result = super().__call__(args)
            if args[:2] == ["install", "--no-deps"]:
                self.installed["comfy-sdk"] = "0.4.1"
            return result

    pip = Liar()
    with pytest.raises(InstallRefused) as exc:
        sdk_install.install(runner=pip, installed=pip.version)
    assert exc.value.code == "verify_failed" and "comfy-sdk (wanted 0.4.0, found 0.4.1)" in str(exc.value)


# ── Uninstall ────────────────────────────────────────────────────────────────

def test_uninstall_removes_only_what_sonder_added(user_dir):
    pip = FakePip(installed={"comfy-sdk": "0.4.0"}, report=[("comfy-api-proxy", "0.1.6"), ("some-dep", "1.2")])
    sdk_install.install(runner=pip, installed=pip.version)
    order = []
    result = sdk_install.uninstall(
        runner=lambda args: order.append(("pip", list(args))) or pip(args),
        installed=pip.version,
        stop_proxy=lambda: order.append(("stop_proxy",)),
        # Asked before the stop: stopping forgets a reused proxy.
        proxy_running_elsewhere=lambda: order.append(("elsewhere?",)) or True,
        shortcut_delete=lambda: order.append(("shortcut",)),
        required_by=lambda name, ignore: [],
    )
    assert order == [("elsewhere?",), ("stop_proxy",), ("pip", ["uninstall", "-y", "comfy-api-proxy"]), ("shortcut",)]
    assert pip.installed == {"comfy-sdk": "0.4.0", "some-dep": "1.2"}
    assert record(user_dir) is None
    assert result["removed"] == ["comfy-api-proxy"]
    assert any("didn't start is still running" in note for note in result["notes"])
    assert any("some-dep 1.2" in note for note in result["notes"])


def test_uninstall_with_nothing_recorded_touches_nothing(user_dir):
    pip = FakePip(installed={"comfy-sdk": "0.4.0", "comfy-api-proxy": "0.1.6"})
    stopped = []
    result = sdk_install.uninstall(runner=pip, installed=pip.version, stop_proxy=lambda: stopped.append(1))
    assert pip.calls == [] and stopped == []
    assert result["removed"] == [] and result["message"].startswith("Nothing to remove")


def _recorded(user_dir, pip, report):
    pip.report = report
    sdk_install.install(runner=pip, installed=pip.version)
    pip.calls.clear()


def test_uninstall_keeps_a_package_whose_version_changed_and_forgets_it(user_dir):
    pip = FakePip()
    _recorded(user_dir, pip, [("comfy-sdk", "0.4.0"), ("comfy-api-proxy", "0.1.6")])
    pip.installed["comfy-sdk"] = "0.4.2"  # the user upgraded it since
    result = sdk_install.uninstall(runner=pip, installed=pip.version, required_by=lambda name, ignore: [])
    assert pip.calls == [["uninstall", "-y", "comfy-api-proxy"]]
    assert pip.installed == {"comfy-sdk": "0.4.2"}
    assert "Kept comfy-sdk 0.4.2: Sonder installed 0.4.0" in result["notes"][0]
    assert record(user_dir) is None


def test_uninstall_keeps_a_package_another_distribution_needs(user_dir):
    pip = FakePip()
    _recorded(user_dir, pip, [("comfy-sdk", "0.4.0"), ("comfy-api-proxy", "0.1.6")])
    asked = []
    result = sdk_install.uninstall(
        runner=pip, installed=pip.version, shortcut_delete=lambda: asked.append("shortcut"),
        required_by=lambda name, ignore: asked.append((name, sorted(ignore))) or (["other-pack"] if name == "comfy-sdk" else []),
    )
    assert ("comfy-sdk", ["comfy-api-proxy", "comfy-sdk"]) in asked and "shortcut" not in asked
    assert pip.calls == [["uninstall", "-y", "comfy-api-proxy"]]
    assert result["notes"][0] == "Kept comfy-sdk: other-pack needs it."
    # Still Sonder's, so a later Uninstall can remove it.
    assert record(user_dir)["installed_by_sonder"] == ["comfy-sdk"]
    assert record(user_dir)["installed_versions"] == {"comfy-sdk": "0.4.0"}


def test_uninstall_after_a_manual_removal_clears_the_record_and_shortcut(user_dir):
    pip = FakePip()
    _recorded(user_dir, pip, [("comfy-api-proxy", "0.1.6")])
    pip.installed.clear()
    deleted = []
    result = sdk_install.uninstall(runner=pip, installed=pip.version, shortcut_delete=lambda: deleted.append(1))
    assert pip.calls == [] and deleted == [1] and record(user_dir) is None
    assert result["removed"] == []


def test_requirers_ignores_extras_and_the_packages_being_removed(monkeypatch):
    class Dist:
        def __init__(self, name, requires):
            self.metadata = {"Name": name}
            self.requires = requires

    dists = [Dist("Other_Pack", ["comfy-sdk>=0.4"]), Dist("dev-tool", ["comfy-sdk; extra == 'dev'"]),
             Dist("comfy-api-proxy", ["comfy-sdk"]), Dist("unrelated", ["comfy-sdk-extras"]), Dist("broken", None)]
    monkeypatch.setattr(sdk_install.importlib.metadata, "distributions", lambda: dists)
    assert sdk_install.requirers("comfy_sdk", ignore={"comfy-api-proxy"}) == ["other-pack"]


def test_a_failed_uninstall_keeps_the_record(user_dir):
    pip = FakePip(installed={"comfy-sdk": "0.4.0"}, report=[("comfy-api-proxy", "0.1.6")])
    sdk_install.install(runner=pip, installed=pip.version)
    pip.fail.add("uninstall")
    with pytest.raises(InstallRefused):
        sdk_install.uninstall(runner=pip, installed=pip.version)
    assert record(user_dir)["installed_by_sonder"] == ["comfy-api-proxy"]


# ── Jobs ─────────────────────────────────────────────────────────────────────

def test_one_job_at_a_time_and_its_result_is_reported(user_dir):
    release = threading.Event()
    pip = FakePip()
    pip.on_install = release.wait
    job = sdk_install.start_install(runner=pip, installed=pip.version)
    assert job["state"] == "running" and job["action"] == "install"
    with pytest.raises(InstallRefused) as exc:
        sdk_install.start_uninstall(runner=pip, installed=pip.version)
    assert exc.value.code == "busy"
    # While pip runs, an intent is not reported as an interrupted install.
    deadline = time.time() + 5
    while not intent_path(user_dir).exists() and time.time() < deadline:
        time.sleep(0.01)
    assert sdk_install.installer_status()["interrupted"] is None
    release.set()
    deadline = time.time() + 5
    while sdk_install.job_snapshot()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    done = sdk_install.job_snapshot()
    assert done["state"] == "done" and done["result"]["ok"] is True
    failing = FakePip(pip_ok=False)
    sdk_install.start_install(runner=failing, installed=failing.version)
    deadline = time.time() + 5
    while sdk_install.job_snapshot()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    failed = sdk_install.job_snapshot()
    assert failed["state"] == "failed" and failed["result"]["code"] == "manual"
    assert "commands" in failed["result"]


def test_a_reservation_marks_an_uninstall_and_releases_cleanly(user_dir):
    previous = sdk_install.reserve_job("uninstall")
    assert sdk_install.uninstall_running() is True
    with pytest.raises(InstallRefused):
        sdk_install.reserve_job("install")
    sdk_install.release_job(previous)
    assert sdk_install.uninstall_running() is False and sdk_install.job_snapshot()["state"] == "idle"
    sdk_install.reserve_job("install")  # the lock was given back
    assert sdk_install.uninstall_running() is False


def test_the_job_lock_is_released_even_when_the_work_raises_a_base_exception(user_dir):
    def work():
        raise KeyboardInterrupt

    sdk_install.reserve_job("install")
    sdk_install.launch_job("install", work)
    deadline = time.time() + 5
    while sdk_install.job_snapshot()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert sdk_install.job_snapshot()["state"] == "failed"
    assert not sdk_install._busy.locked()


def test_a_long_job_says_it_is_still_running(user_dir, monkeypatch):
    sdk_install._set_job(action="install", state="running", started_at=1000.0, result=None)
    assert sdk_install.job_snapshot(clock=lambda: 1000.0 + 599)["still_running"] is False
    later = sdk_install.job_snapshot(clock=lambda: 1000.0 + 601)
    assert later["still_running"] is True and later["elapsed_sec"] == 601.0


# ── Shortcut ─────────────────────────────────────────────────────────────────

def test_the_shortcut_probes_comfyui_then_opens_the_launcher(tmp_path):
    projects = tmp_path / "sonder-projects"
    path = sdk_install.write_shortcut(str(projects), "http://127.0.0.1:8888")
    html = (projects / "Sonder Launcher.html").read_text(encoding="utf-8")
    assert path.endswith("Sonder Launcher.html")
    assert 'const LAUNCHER = "http://127.0.0.1:8888/sonder-editor/app";' in html
    # The ping route, whose CORP allows a file:// page's no-cors fetch.
    assert 'const PING = "http://127.0.0.1:8888/sonder-editor/sdk/ping";' in html
    assert 'mode: "no-cors"' in html and "location.replace(LAUNCHER)" in html
    assert "Start ComfyUI first" in html
    assert sdk_install.delete_shortcut(str(projects)) is True
    assert not (projects / "Sonder Launcher.html").exists()
    assert sdk_install.delete_shortcut(str(projects)) is False


def test_the_shortcut_escapes_what_it_embeds():
    html = sdk_install.shortcut_html('http://h:1/</script><b>')
    assert "</script><b>" not in html.split("<script>", 1)[1].split("</script>", 1)[0]
    assert "&lt;/script&gt;&lt;b&gt;" in html


def test_startup_refreshes_the_shortcut_only_while_it_is_sonders(tmp_path):
    projects = str(tmp_path / "p")
    page = tmp_path / "p" / "Sonder Launcher.html"
    ours = lambda: {"installed_by_sonder": ["comfy-api-proxy"]}
    theirs = lambda: {"installed_by_sonder": []}
    ready, missing = (lambda: {"ready": True}), (lambda: {"ready": False})
    assert sdk_install.refresh_shortcut(projects, "http://127.0.0.1:9000", state=missing, record=ours) == ""
    # Packages the user installed alone get no file.
    assert sdk_install.refresh_shortcut(projects, "http://127.0.0.1:9000", state=ready, record=theirs) == ""
    assert not page.exists()
    sdk_install.refresh_shortcut(projects, "http://127.0.0.1:9000", state=ready, record=ours)
    assert "127.0.0.1:9000/sonder-editor/app" in page.read_text(encoding="utf-8")
    # An existing file follows the port even without a record.
    sdk_install.refresh_shortcut(projects, "http://127.0.0.1:9100", state=ready, record=theirs)
    assert "127.0.0.1:9100/sonder-editor/app" in page.read_text(encoding="utf-8")
    assert sdk_install.write_shortcut("", "http://x") == "" and sdk_install.write_shortcut(projects, "") == ""


def test_an_unchanged_shortcut_is_not_rewritten(tmp_path):
    projects = str(tmp_path / "p")
    path = sdk_install.write_shortcut(projects, "http://127.0.0.1:9000")
    os.utime(path, (1000, 1000))
    sdk_install.write_shortcut(projects, "http://127.0.0.1:9000")
    assert os.stat(path).st_mtime == 1000
    assert [p.name for p in (tmp_path / "p").iterdir()] == ["Sonder Launcher.html"]


# ── Routes ───────────────────────────────────────────────────────────────────

def _client(user_dir, monkeypatch, runner_has_runs=False):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from server import sdk_routes, sdk_runner

    started = _Started()
    monkeypatch.setattr(sdk_install, "start_install", lambda **kw: started.append(("install", kw)) or {"state": "running"})

    def launch(action, work):
        # Records the launch with the uninstall still reserved, then gives the
        # reservation back as the finished job thread would.
        started.append((action, sdk_install.uninstall_running()))
        sdk_install._set_job(state="done")
        sdk_install._busy.release()
        return {"state": "running"}

    monkeypatch.setattr(sdk_install, "launch_job", launch)
    monkeypatch.setattr(sdk_routes.sdk_proxy, "proxy_status", lambda: {"running": False, "managed": False})
    sdk_runner.reset_runner_for_tests()
    routes = web.RouteTableDef()
    sdk_routes.register_sdk_routes(routes, resolve_project=lambda p: None, remember_event_loop=lambda: None,
                                   projects_dir=lambda: str(user_dir / "output" / "sonder-projects"))
    runner = sdk_runner.get_runner()
    monkeypatch.setattr(runner, "has_unfinished_runs", lambda: runner_has_runs)
    closed = []

    async def aclose():
        closed.append(1)

    monkeypatch.setattr(runner, "aclose_clients", aclose)
    started.closed = closed
    app = web.Application()
    app.add_routes(routes)
    return TestClient(TestServer(app)), started


class _Started(list):
    pass


def test_install_and_uninstall_are_refused_from_another_host(user_dir, monkeypatch):
    async def run():
        client, started = _client(user_dir, monkeypatch)
        async with client:
            lan = await client.post("/sonder-editor/sdk/install", headers={"Host": "192.168.1.20:8888"})
            assert lan.status == 403 and (await lan.json())["code"] == "not_local"
            lan = await client.post("/sonder-editor/sdk/uninstall", headers={"Host": "192.168.1.20:8888"})
            assert lan.status == 403
            status = await client.get("/sonder-editor/sdk/status", headers={"Host": "192.168.1.20:8888"})
            assert (await status.json())["can_manage"] is False
            relayed = await client.post("/sonder-editor/sdk/install", headers={"Host": "127.0.0.1:8888", "X-Real-IP": "203.0.113.9"})
            assert relayed.status == 403
            local = await client.post("/sonder-editor/sdk/install", headers={"Host": "127.0.0.1:8888"})
            assert local.status == 202
        assert [name for name, _ in started] == ["install"]

    asyncio.run(run())


def test_uninstall_is_refused_while_takes_are_generating(user_dir, monkeypatch):
    async def run():
        client, started = _client(user_dir, monkeypatch, runner_has_runs=True)
        async with client:
            resp = await client.post("/sonder-editor/sdk/uninstall", headers={"Host": "127.0.0.1:8888"})
            assert resp.status == 409 and (await resp.json())["code"] == "runs_active"
        assert started == [] and started.closed == []
        # The reservation was given back: nothing is marked, and a job can start.
        assert sdk_install.uninstall_running() is False and sdk_install.job_snapshot()["state"] == "idle"
        assert not sdk_install._busy.locked()

    asyncio.run(run())


def test_uninstall_closes_the_clients_and_launches_while_reserved(user_dir, monkeypatch):
    async def run():
        client, started = _client(user_dir, monkeypatch)
        async with client:
            resp = await client.post("/sonder-editor/sdk/uninstall", headers={"Host": "127.0.0.1:8888"})
            assert resp.status == 202
        assert started == [("uninstall", True)] and started.closed == [1]

    asyncio.run(run())


def test_uninstall_waits_out_a_generate_in_flight_and_refuses_new_ones(user_dir, monkeypatch):
    from server import sdk_routes, sdk_runner

    async def run():
        client, started = _client(user_dir, monkeypatch)
        monkeypatch.setattr(sdk_routes, "UNINSTALL_DRAIN_SEC", 0.2)
        runner = sdk_runner.get_runner()
        async with client:
            runner._generating = 1  # a Generate between its gate and its submit
            resp = await client.post("/sonder-editor/sdk/uninstall", headers={"Host": "127.0.0.1:8888"})
            assert resp.status == 409 and (await resp.json())["code"] == "runs_active"
            runner._generating = 0
            assert started == [] and not sdk_install._busy.locked()
            sdk_install.reserve_job("uninstall")
            with pytest.raises(sdk_runner.SdkError) as exc:
                await runner.generate(None, count=1)
            assert exc.value.code == "sdk_uninstalling" and runner._generating == 0

    asyncio.run(run())


def test_lan_status_carries_no_commands_or_pip_output(user_dir, monkeypatch):
    async def run():
        client, _ = _client(user_dir, monkeypatch)
        sdk_install._set_job(state="failed", action="install",
                             result={"ok": False, "code": "manual", "message": "m", "commands": {"pip": "X"}, "detail": "C:/Users/x"})
        intent_path(user_dir).parent.mkdir(parents=True, exist_ok=True)
        intent_path(user_dir).write_text(json.dumps({"packages": ["comfy-sdk==0.4.0"]}), encoding="utf-8")
        monkeypatch.setattr(sdk_install, "_version", lambda name: "")
        async with client:
            lan = await (await client.get("/sonder-editor/sdk/status", headers={"Host": "192.168.1.20:8888"})).json()
            local = await (await client.get("/sonder-editor/sdk/status", headers={"Host": "127.0.0.1:8888"})).json()
        assert lan["installer"]["job"]["result"] == {"ok": False, "code": "manual", "message": "m"}
        assert "commands" not in lan["installer"]["interrupted"] and lan["installer"]["interrupted"]["packages"]
        assert local["installer"]["job"]["result"]["commands"] == {"pip": "X"}
        assert "commands" in local["installer"]["interrupted"]

    asyncio.run(run())


def test_the_ping_route_allows_a_cross_origin_probe(user_dir, monkeypatch):
    async def run():
        client, _ = _client(user_dir, monkeypatch)
        async with client:
            resp = await client.get("/sonder-editor/sdk/ping")
        assert resp.status == 204
        assert resp.headers["Cross-Origin-Resource-Policy"] == "cross-origin"

    asyncio.run(run())


def test_startup_refreshes_the_shortcut_through_the_app(user_dir, monkeypatch):
    from server import sdk_routes, sdk_runner

    calls = []
    monkeypatch.setattr(sdk_install, "refresh_shortcut", lambda projects, upstream: calls.append((projects, upstream)))
    monkeypatch.setattr(sdk_routes, "_upstream_or_empty", lambda: "http://127.0.0.1:8888")
    sdk_runner.reset_runner_for_tests()
    app = types.SimpleNamespace(on_startup=[])
    sdk_routes.register_sdk_routes(types.SimpleNamespace(get=lambda *a, **k: (lambda f: f), post=lambda *a, **k: (lambda f: f),
                                                         put=lambda *a, **k: (lambda f: f), patch=lambda *a, **k: (lambda f: f),
                                                         delete=lambda *a, **k: (lambda f: f)),
                                   resolve_project=lambda p: None, remember_event_loop=lambda: None,
                                   projects_dir=lambda: "P", app=app)
    assert len(app.on_startup) == 1
    asyncio.run(app.on_startup[0](app))
    assert calls == [("P", "http://127.0.0.1:8888")]


def test_status_reports_the_installer(user_dir, monkeypatch):
    async def run():
        client, _ = _client(user_dir, monkeypatch)
        async with client:
            payload = await (await client.get("/sonder-editor/sdk/status", headers={"Host": "localhost:8888"})).json()
        assert payload["can_manage"] is True
        assert payload["pinned"] == {"comfy-sdk": "0.4.0", "comfy-api-proxy": "0.1.6"}
        assert payload["installer"]["job"]["state"] == "idle"
        assert payload["installer"]["installed_by_sonder"] == [] and payload["installer"]["interrupted"] is None

    asyncio.run(run())


def test_the_shortcut_script_opens_the_launcher_or_asks_to_start_comfyui(tmp_path):
    """Runs the page's own script under Node with ComfyUI answering, then down."""
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the shortcut's script")
    html = sdk_install.shortcut_html("http://127.0.0.1:8888")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = """
const make = () => ({ textContent: "", hidden: true, listeners: [], addEventListener(t, f) { this.listeners.push(f); } });
async function run(up) {
  const els = { message: make(), retry: make() };
  const replaced = [];
  const fetched = [];
  globalThis.document = { getElementById: (id) => els[id] };
  globalThis.location = { replace: (u) => replaced.push(u) };
  globalThis.fetch = async (url, init) => { fetched.push([url, init.mode]); if (!up) throw new TypeError("Failed to fetch"); return {}; };
  new Function(SCRIPT)();
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  return { replaced, fetched, message: els.message.textContent, retryHidden: els.retry.hidden };
}
console.log(JSON.stringify({ up: await run(true), down: await run(false) }));
""".replace("SCRIPT", json.dumps(script))
    result = subprocess.run([node, "--input-type=module"], input=harness, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["up"]["fetched"] == [["http://127.0.0.1:8888/sonder-editor/sdk/ping", "no-cors"]]
    assert out["up"]["replaced"] == ["http://127.0.0.1:8888/sonder-editor/app"]
    assert out["down"]["replaced"] == []
    assert out["down"]["message"] == "Start ComfyUI first, then try again." and out["down"]["retryHidden"] is False
