"""The optional SDK packages: whether they are installed, installing and removing them.

comfy-sdk and comfy-api-proxy are not pack dependencies; a user opts in from the
editor's Settings → Sonder App. Installed state is read from the Python
environment on every call, never stored.

**Install** pins exact versions and never changes a package ComfyUI already has:
a ``pip --dry-run --report <file>`` lists what pip would add or change, any
change to an installed package refuses, and only that reported set is installed
with ``--no-deps``. ``<user>/sonder-editor/sdk_install.json`` records what Sonder
added: the pinned packages it installed, with their versions
(``installed_by_sonder`` / ``installed_versions``, which Uninstall removes) and
any dependencies the dry run added (``added_dependencies``, listed and left in
place). A pinned package that was already installed is never recorded, so
Uninstall keeps it. An intent file written before pip runs and removed after the
record is written shows an install that did not finish; whatever it names that
is now installed at its version is merged into the record (``reconcile_intent``).

**One job at a time**, in a thread. pip has no hard kill: past ten minutes the
status says it is still running and the lock stays held. pip runs with ComfyUI's
interpreter flags (``-s``/``-I``/``-E``) and UTF-8 I/O, and the report is read
from a file, never stdout (a cp1252 stdout cannot carry the packages' metadata).

**Uninstall** removes only recorded packages still at the version Sonder
installed and needed by no other installed distribution. The route reserves the
job first, which stops new Generates (``uninstall_running``), and refuses while
any SDK run is unfinished; the job stops the proxy Sonder started.

**The Launcher shortcut** (``<sonder-projects>/Sonder Launcher.url``, Windows
only) exists only while Sonder's install does: written on install, refreshed at
startup when both packages are present and Sonder installed one or the file
exists, deleted when Uninstall leaves nothing recorded. It is a Windows Internet
Shortcut, which the shell opens as a direct navigation: ComfyUI's origin
middleware refuses the cross-site navigation a ``file://`` page would make.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable

from .atomic_io import atomic_replace
from .sdk_paths import json_sidecar_lock, read_json_sidecar, sonder_user_dir, write_json_atomic

logger = logging.getLogger("sonder_editor")

PINNED = {"comfy-sdk": "0.4.0", "comfy-api-proxy": "0.1.6"}
RECORD_NAME = "sdk_install.json"
INTENT_NAME = "sdk_install.intent.json"
SHORTCUT_NAME = "Sonder Launcher.url"
# The unreleased HTML shortcut it replaced; deleted wherever it is found. Remove this
# name and its cleanup once a release that ships the .url has been published.
LEGACY_SHORTCUT_NAME = "Sonder Launcher.html"
SHORTCUT_ICON = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web", "assets", "sonder.ico"))
STILL_RUNNING_AFTER_SEC = 600.0
# Top-level modules of each package: loaded ones stay in memory after an
# uninstall until ComfyUI restarts. comfy_api_proxy only ever runs as a subprocess.
PACKAGE_MODULES = {"comfy-sdk": ("comfy_sdk", "comfy_low"), "comfy-api-proxy": ("comfy_api_proxy",)}

Runner = Callable[[list], "subprocess.CompletedProcess"]


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


_PINNED = {canonical(name): version for name, version in PINNED.items()}


def _version(distribution: str) -> str:
    try:
        # None while pip is mid-write and METADATA is missing.
        return importlib.metadata.version(distribution) or ""
    except importlib.metadata.PackageNotFoundError:
        return ""


def install_state() -> dict:
    """``{"sdk": {"installed", "version"}, "proxy": {...}, "ready"}`` from the environment."""
    importlib.invalidate_caches()
    sdk_version = _version("comfy-sdk")
    proxy_version = _version("comfy-api-proxy")
    sdk_ok = sdk_version.startswith("0.4.")
    proxy_ok = proxy_version.startswith("0.1.")
    return {
        "sdk": {"installed": bool(sdk_version), "version": sdk_version, "supported": sdk_ok},
        "proxy": {"installed": bool(proxy_version), "version": proxy_version, "supported": proxy_ok},
        "ready": sdk_ok and proxy_ok,
    }


class InstallRefused(Exception):
    def __init__(self, message: str, code: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


# ── pip ──────────────────────────────────────────────────────────────────────

def _pin_args() -> list[str]:
    return [f"{name}=={version}" for name, version in PINNED.items()]


def _interpreter_flags() -> list[str]:
    """ComfyUI's own isolation flags, so pip resolves against what ComfyUI can import.

    The portable build runs ``python -s``: without it here, a package only in
    user site-packages would satisfy pip's resolver yet be invisible to ComfyUI.
    """
    if sys.flags.isolated:
        return ["-I"]
    flags = []
    if sys.flags.no_user_site:
        flags.append("-s")
    if sys.flags.ignore_environment:
        flags.append("-E")
    return flags


def manual_commands() -> dict:
    pins = " ".join(_pin_args())
    flags = "".join(f" {flag}" for flag in _interpreter_flags())
    return {
        "pip": f'"{sys.executable}"{flags} -m pip install {pins}',
        "uv": f'uv pip install --python "{sys.executable}" {pins}',
    }


def default_runner(args: list) -> subprocess.CompletedProcess:
    """``python -m pip <args>`` in ComfyUI's own environment, no console window."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    return subprocess.run(
        [sys.executable, *_interpreter_flags(), "-m", "pip", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=flags, env=env,
    )


def _tail(text: str, limit: int = 600) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _read_report(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def plan_install(runner: Runner, installed: Callable[[str], str] = _version) -> list[tuple[str, str]]:
    """What install would add, as ``[(name, version)]``; raises ``InstallRefused``."""
    try:
        check = runner(["--version"])
        pip_ok = check.returncode == 0
    except OSError:
        pip_ok = False
    if not pip_ok:
        raise InstallRefused(
            "pip isn't available in ComfyUI's Python. Install the packages by hand with one of these commands.",
            "manual", commands=manual_commands(),
        )
    for name, version in PINNED.items():
        current = installed(name)
        if current and current != version:
            raise InstallRefused(
                f"{name} {current} is already installed, and the Sonder App needs {version}. "
                "Sonder won't change it; update it yourself if nothing else needs that version.",
                "pin_conflict", package=name, installed=current, needed=version,
            )
    # pip writes a report file as UTF-8; on stdout it uses the console encoding,
    # which cannot carry the packages' descriptions on a cp1252 Windows Python.
    fd, report_path = tempfile.mkstemp(prefix="sonder-pip-report-", suffix=".json")
    os.close(fd)
    try:
        result = runner(["install", "--dry-run", "--report", report_path, "--quiet", "--no-input", *_pin_args()])
        if result.returncode != 0:
            raise InstallRefused("pip couldn't plan the install.", "dry_run_failed", detail=_tail(result.stderr))
        try:
            report = _read_report(report_path)
            items = [(canonical(item["metadata"]["name"]), str(item["metadata"]["version"])) for item in report["install"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise InstallRefused("pip's install plan couldn't be read.", "dry_run_unreadable", detail=str(exc)) from exc
    finally:
        try:
            os.remove(report_path)
        except OSError:
            pass
    changes = [(name, installed(name), version) for name, version in items if installed(name)]
    if changes:
        listed = ", ".join(f"{name} {old} → {new}" for name, old, new in changes)
        raise InstallRefused(
            f"Installing would change packages ComfyUI already has ({listed}). Sonder only adds packages, so it stopped.",
            "would_change", changes=[{"package": n, "installed": o, "would_install": v} for n, o, v in changes],
        )
    return items


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement or "")
    return canonical(match.group(1)) if match else ""


def requirers(name: str, *, ignore: set[str] = frozenset()) -> list[str]:
    """Installed distributions, outside ``ignore``, that require ``name`` (extras excluded)."""
    target = canonical(name)
    found = set()
    for dist in importlib.metadata.distributions():
        dist_name = canonical(dist.metadata.get("Name", "") if dist.metadata else "")
        if not dist_name or dist_name == target or dist_name in ignore:
            continue
        for requirement in dist.requires or ():
            if "extra" in requirement.partition(";")[2]:
                continue
            if _requirement_name(requirement) == target:
                found.add(dist_name)
                break
    return sorted(found)


# ── Record and intent ────────────────────────────────────────────────────────

def _state_path(name: str) -> str:
    base = sonder_user_dir()
    if not base:
        raise InstallRefused("ComfyUI's user folder is unavailable.", "no_user_dir")
    return os.path.join(base, name)


def read_record() -> dict:
    try:
        path = _state_path(RECORD_NAME)
    except InstallRefused:
        return {"installed_by_sonder": [], "installed_versions": {}, "added_dependencies": []}
    with json_sidecar_lock(path):
        record = read_json_sidecar(path)
    by_sonder = record.get("installed_by_sonder")
    versions = record.get("installed_versions")
    deps = record.get("added_dependencies")
    return {
        **record,
        "installed_by_sonder": [canonical(n) for n in by_sonder if isinstance(n, str)] if isinstance(by_sonder, list) else [],
        "installed_versions": ({canonical(k): str(v) for k, v in versions.items() if isinstance(k, str)}
                               if isinstance(versions, dict) else {}),
        "added_dependencies": [d for d in deps if isinstance(d, dict) and d.get("name")] if isinstance(deps, list) else [],
    }


def _write_record(record: dict) -> None:
    path = _state_path(RECORD_NAME)
    with json_sidecar_lock(path):
        write_json_atomic(path, record)


def _merge_into_record(items: list[tuple[str, str]]) -> None:
    """Record ``items`` as Sonder's: pinned packages are removable, others are listed."""
    record = read_record()
    versions = dict(record["installed_versions"])
    by_sonder = set(record["installed_by_sonder"])
    for name, version in items:
        if name in _PINNED:
            by_sonder.add(name)
            versions[name] = version
    deps = {d["name"]: d for d in record["added_dependencies"]}
    deps.update({n: {"name": n, "version": v} for n, v in items if n not in _PINNED})
    _write_record({
        **{k: v for k, v in record.items() if k not in ("installed_by_sonder", "installed_versions", "added_dependencies")},
        "version": 1,
        "installed_at": time.time(),
        "installed_by_sonder": sorted(by_sonder),
        "installed_versions": {n: versions[n] for n in sorted(by_sonder) if n in versions},
        "added_dependencies": sorted(deps.values(), key=lambda d: d["name"]),
    })


def _remove_state_file(name: str) -> None:
    try:
        path = _state_path(name)
    except InstallRefused:
        return
    with json_sidecar_lock(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _read_intent() -> dict:
    try:
        path = _state_path(INTENT_NAME)
    except InstallRefused:
        return {}
    with json_sidecar_lock(path):
        return read_json_sidecar(path)


def _intent_exists() -> bool:
    try:
        return os.path.isfile(_state_path(INTENT_NAME))
    except InstallRefused:
        return False


def _intent_items(intent: dict) -> list[tuple[str, str]]:
    items = []
    for spec in intent.get("packages") or []:
        name, sep, version = str(spec).partition("==")
        if sep and name and version:
            items.append((canonical(name), version))
    return items


def reconcile_intent(installed: Callable[[str], str] | None = None) -> list[tuple[str, str]]:
    """Settle an unfinished install: record what it got in, drop the intent when it got everything.

    Returns what is still missing (empty when there is no intent). The caller
    holds the job lock, so no pip of ours is running.
    """
    installed = installed or _version
    items = _intent_items(_read_intent())
    if not items:
        _remove_state_file(INTENT_NAME)
        return []
    present = [(n, v) for n, v in items if installed(n) == v]
    if present:
        _merge_into_record(present)
    missing = [(n, v) for n, v in items if (n, v) not in present]
    if not missing:
        _remove_state_file(INTENT_NAME)
    return missing


# ── Launcher shortcut ────────────────────────────────────────────────────────

def shortcuts_supported() -> bool:
    """Internet Shortcut files are a Windows format; other systems get a bookmark hint instead."""
    return os.name == "nt"


def shortcut_bytes(upstream: str, icon_path: str = SHORTCUT_ICON) -> bytes:
    """The ``.url`` file: an INI the Windows shell reads in the ANSI code page.

    Only ASCII is written, so it reads the same in every code page. An icon path
    that is not ASCII is left out and the shell shows the browser's icon instead.
    """
    base = str(upstream or "").rstrip("/")
    lines = ["[InternetShortcut]", f"URL={base}/sonder-editor/app"]
    if icon_path and icon_path.isascii() and os.path.isfile(icon_path):
        lines += [f"IconFile={icon_path}", "IconIndex=0"]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def shortcut_path(projects_dir: str) -> str:
    return os.path.join(projects_dir, SHORTCUT_NAME) if projects_dir else ""


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def remove_legacy_shortcut(projects_dir: str) -> bool:
    """Delete the unreleased HTML shortcut. It never worked (ComfyUI refuses its cross-site
    navigation) and nothing but Sonder wrote that name, so finding it is enough.

    Only a regular file is removed, and a failure is logged rather than raised, so a
    locked or odd entry never stops the install or uninstall that called this.
    """
    path = os.path.join(projects_dir, LEGACY_SHORTCUT_NAME) if projects_dir else ""
    if not path or not os.path.isfile(path) or os.path.islink(path):
        return False
    try:
        return _remove(path)
    except OSError as exc:
        logger.warning("Couldn't delete the old Launcher shortcut %s: %s", LEGACY_SHORTCUT_NAME, exc)
        return False


def write_shortcut(projects_dir: str, upstream: str, *, icon_path: str = SHORTCUT_ICON,
                   supported: Callable[[], bool] = shortcuts_supported) -> str:
    """Write the shortcut, unless it already says exactly this. "" where none is written."""
    path = shortcut_path(projects_dir)
    if path:
        remove_legacy_shortcut(projects_dir)
    if not path or not upstream or not supported():
        return ""
    data = shortcut_bytes(upstream, icon_path)
    try:
        with open(path, "rb") as handle:
            if handle.read() == data:
                return path
    except OSError:
        pass
    os.makedirs(projects_dir, exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        atomic_replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def delete_shortcut(projects_dir: str) -> bool:
    if not projects_dir:
        return False
    legacy = remove_legacy_shortcut(projects_dir)
    return _remove(shortcut_path(projects_dir)) or legacy


def refresh_shortcut(projects_dir: str, upstream: str, *, state: Callable[[], dict] = install_state,
                     record: Callable[[], dict] = read_record, icon_path: str = SHORTCUT_ICON,
                     supported: Callable[[], bool] = shortcuts_supported) -> str:
    """At startup: rewrite the shortcut (the port or icon path may have changed) while it is Sonder's.

    That is while both packages are present and either Sonder installed one, the
    file already exists, or the legacy HTML shortcut did; packages a user
    installed alone get no file. The legacy file is deleted in every case.
    """
    had_legacy = remove_legacy_shortcut(projects_dir)
    if not state().get("ready"):
        return ""
    path = shortcut_path(projects_dir)
    if not path or not (os.path.isfile(path) or had_legacy or record()["installed_by_sonder"]):
        return ""
    return write_shortcut(projects_dir, upstream, icon_path=icon_path, supported=supported)


# ── Jobs ─────────────────────────────────────────────────────────────────────

_busy = threading.Lock()
_job_guard = threading.Lock()
_IDLE_JOB = {"action": "", "state": "idle", "started_at": 0.0, "finished_at": 0.0, "result": None}
_job: dict[str, Any] = dict(_IDLE_JOB)


def _set_job(**fields: Any) -> None:
    with _job_guard:
        _job.update(fields)


def job_snapshot(clock: Callable[[], float] = time.time) -> dict:
    with _job_guard:
        job = dict(_job)
    running = job["state"] == "running"
    elapsed = (clock() - job["started_at"]) if running and job["started_at"] else 0.0
    return {**job, "elapsed_sec": round(elapsed, 1), "still_running": running and elapsed > STILL_RUNNING_AFTER_SEC}


def uninstall_running() -> bool:
    """True from the moment an uninstall is reserved until it ends: new SDK work waits."""
    with _job_guard:
        return _job["action"] == "uninstall" and _job["state"] == "running"


def installer_status() -> dict:
    """What the Settings section shows: the job, the record, an unfinished install."""
    job = job_snapshot()
    interrupted = None
    record = {"installed_by_sonder": [], "added_dependencies": []}
    try:
        # Not while a job holds the lock: its own intent is not an interruption.
        if job["state"] != "running" and _intent_exists() and _busy.acquire(blocking=False):
            try:
                missing = reconcile_intent()
            finally:
                _busy.release()
            if missing:
                interrupted = {
                    "packages": [f"{n}=={v}" for n, v in missing],
                    "message": "An install didn't finish. Run Install again, or install by hand with the command below.",
                    "commands": manual_commands(),
                }
        record = read_record()
    except OSError as exc:  # a locked or unreadable state file must not fail the status route
        logger.warning("Reading the Sonder App install record failed: %s", exc)
    return {
        "job": job,
        "installed_by_sonder": record["installed_by_sonder"],
        "added_dependencies": record["added_dependencies"],
        "interrupted": interrupted,
    }


def reserve_job(action: str) -> dict:
    """Take the one job slot now, before any await; returns what ``release_job`` restores.

    The route reserves an uninstall first so ``uninstall_running`` stops new
    Generates while it waits for in-flight ones, then launches or releases.
    """
    if not _busy.acquire(blocking=False):
        raise InstallRefused("An install or uninstall is already running.", "busy")
    with _job_guard:
        previous = dict(_job)
        _job.update(action=action, state="running", started_at=time.time(), finished_at=0.0, result=None)
    return previous


def release_job(previous: dict) -> None:
    """Give back a reservation that never launched."""
    with _job_guard:
        _job.clear()
        _job.update(previous)
    _busy.release()


def launch_job(action: str, work: Callable[[], dict]) -> dict:
    """Run ``work`` in a thread under a reservation; the thread releases it."""

    def target() -> None:
        result: dict = {"ok": False, "code": "failed", "message": f"The {action} failed."}
        try:
            result = work()
        except InstallRefused as exc:
            result = {"ok": False, "code": exc.code, "message": str(exc), **exc.details}
        except BaseException as exc:  # reported, never raised into a thread
            logger.exception("Sonder App %s failed", action)
            result = {"ok": False, "code": "failed", "message": f"The {action} failed.", "detail": str(exc)}
        finally:
            _set_job(state="done" if result.get("ok") else "failed", finished_at=time.time(), result=result)
            _busy.release()

    threading.Thread(target=target, name=f"sonder-sdk-{action}", daemon=True).start()
    return job_snapshot()


def _run_job(action: str, work: Callable[[], dict]) -> dict:
    reserve_job(action)
    try:
        return launch_job(action, work)
    except BaseException:
        _set_job(state="failed", finished_at=time.time(), result={"ok": False, "code": "failed", "message": f"The {action} failed."})
        _busy.release()
        raise


def install(*, runner: Runner = default_runner, installed: Callable[[str], str] = _version,
            shortcut: Callable[[], str] = lambda: "") -> dict:
    """The install work (synchronous; ``start_install`` runs it as the job)."""
    reconcile_intent(installed)
    items = plan_install(runner, installed)
    if items:
        intent_path = _state_path(INTENT_NAME)
        with json_sidecar_lock(intent_path):
            write_json_atomic(intent_path, {"started_at": time.time(), "packages": [f"{n}=={v}" for n, v in items]})
        try:
            result = runner(["install", "--no-deps", "--no-input", *[f"{n}=={v}" for n, v in items]])
            if result.returncode != 0:
                raise InstallRefused("pip couldn't install the packages.", "pip_failed", detail=_tail(result.stderr))
            importlib.invalidate_caches()
            wrong = [f"{n} (wanted {v}, found {installed(n) or 'nothing'})" for n, v in items if installed(n) != v]
            if wrong:
                raise InstallRefused("pip finished, but the installed versions don't match: " + ", ".join(wrong) + ".",
                                     "verify_failed")
        finally:
            # Whatever pip got in is Sonder's, even when it stopped partway; the
            # failure itself is the job's result. What is still missing keeps the
            # intent, and a failed record write is retried by the next status read.
            importlib.invalidate_caches()
            try:
                reconcile_intent(installed)
            except Exception:
                logger.exception("Recording the Sonder App install failed")
    notes = []
    try:
        path = shortcut()
        if path:
            # Named, not the full path: the status route is readable without auth.
            notes.append(f"Saved the Launcher shortcut, {SHORTCUT_NAME}, in the sonder-projects folder.")
    except Exception as exc:
        logger.warning("Writing the Sonder Launcher shortcut failed: %s", exc)
        notes.append("The Launcher shortcut couldn't be written.")
    added = [f"{n} {v}" for n, v in items]
    return {
        "ok": True,
        "message": ("Installed " + " and ".join(added) + ".") if added else "Both packages were already installed.",
        "added": added,
        "notes": notes,
        "restart_needed": False,
    }


def uninstall(*, runner: Runner = default_runner, installed: Callable[[str], str] = _version,
              stop_proxy: Callable[[], Any] = lambda: None, proxy_running_elsewhere: Callable[[], bool] = lambda: False,
              shortcut_delete: Callable[[], Any] = lambda: None,
              required_by: Callable[..., list] = requirers) -> dict:
    """The uninstall work. The caller has reserved the job and refused if any SDK run is unfinished."""
    reconcile_intent(installed)
    record = read_record()
    recorded = record["installed_by_sonder"]
    removable, kept, notes = [], [], []
    for name in recorded:
        current = installed(name)
        if not current:
            continue  # already gone: drop it from the record
        wanted = record["installed_versions"].get(name, "")
        if current != wanted:
            # Changed since Sonder installed it, so it is the user's now.
            notes.append(f"Kept {name} {current}: Sonder installed {wanted or 'another version'}, so it isn't Sonder's to remove.")
            continue
        needed_by = required_by(name, ignore=set(recorded))
        if needed_by:
            kept.append(name)
            notes.append(f"Kept {name}: {', '.join(needed_by)} needs it.")
            continue
        removable.append(name)
    elsewhere = False
    if removable:
        elsewhere = proxy_running_elsewhere()  # before stopping: a stop forgets a reused proxy
        stop_proxy()
        result = runner(["uninstall", "-y", *removable])
        if result.returncode != 0:
            raise InstallRefused("pip couldn't remove the packages.", "pip_failed", detail=_tail(result.stderr))
        importlib.invalidate_caches()
    if kept:
        _write_record({**record, "installed_by_sonder": kept,
                       "installed_versions": {n: record["installed_versions"][n] for n in kept}})
    else:
        try:
            shortcut_delete()
        except Exception as exc:
            logger.warning("Deleting the Sonder Launcher shortcut failed: %s", exc)
        _remove_state_file(RECORD_NAME)
    _remove_state_file(INTENT_NAME)
    if elsewhere and "comfy-api-proxy" in removable:
        notes.append("A comfy-api-proxy that Sonder didn't start is still running; stop it yourself if you no longer need it.")
    loaded = [name for name in removable
              if any(module.split(".", 1)[0] in PACKAGE_MODULES.get(name, ()) for module in list(sys.modules))]
    if loaded:
        notes.append(" and ".join(loaded) + " stays loaded until ComfyUI restarts.")
    if removable and record["added_dependencies"]:
        notes.append("Packages added alongside it were left in place: "
                     + ", ".join(f"{d['name']} {d.get('version', '')}".strip() for d in record["added_dependencies"]) + ".")
    if not removable:
        return {"ok": True, "message": "Nothing to remove: Sonder didn't install these packages, or they changed since.",
                "removed": [], "notes": notes}
    return {"ok": True, "message": "Removed " + " and ".join(removable) + ".", "removed": removable, "notes": notes}


def start_install(**kwargs: Any) -> dict:
    return _run_job("install", lambda: install(**kwargs))


def start_uninstall(**kwargs: Any) -> dict:
    return _run_job("uninstall", lambda: uninstall(**kwargs))


def reset_jobs_for_tests() -> None:
    _set_job(**_IDLE_JOB)
    if _busy.locked():
        _busy.release()
