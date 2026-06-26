"""Concurrency tests for the per-project write lock added to `save_project`.

These guard the Fix A serialization gate: concurrent `save_project` calls from
multiple threads must not raise `PermissionError` (WinError 5/Errno 13 on the
single `project.json`) and must leave the file as valid JSON, and the lock must
canonicalize path spellings so symlinked/relative variants map to ONE lock.
"""

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.project_manager import _project_write_lock, load_project, save_project
from server.timeline_state import TimelineProject


def _make_project(project_dir):
    os.makedirs(project_dir, exist_ok=True)
    proj = TimelineProject(
        project_dir=str(project_dir),
        project_id="p1",
        name="Concurrency",
        resolution=(16, 16),
    )
    save_project(proj, notify=False)
    return proj


def test_concurrent_saves_no_permission_error(tmp_path):
    project_dir = tmp_path / "proj"
    proj = _make_project(project_dir)

    errors = []
    start = threading.Barrier(8)

    def worker():
        try:
            start.wait()
            for _ in range(25):
                # No expected_modified_at → no version check; pure write contention.
                save_project(proj, notify=False)
        except Exception as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent save_project raised: {errors!r}"

    # project.json must be intact and valid JSON — no half-written/locked clobber.
    with open(os.path.join(str(project_dir), "project.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["project_id"] == "p1"
    assert load_project(str(project_dir)).project_id == "p1"


def test_write_lock_canonicalizes_path_spellings(tmp_path):
    """Different spellings of the same project dir must return the same lock.

    A real directory symlink (the production output→Images case) needs OS
    privileges that CI may lack, so this uses the portable proxies `realpath`
    normalizes: a trailing `.` segment and (on case-insensitive filesystems) a
    case variant. The audit separately verified `realpath` collapses a Windows
    junction/symlink even for a not-yet-existent child.
    """
    project_dir = str(tmp_path / "proj")
    os.makedirs(project_dir, exist_ok=True)

    base = _project_write_lock(project_dir)
    assert base is _project_write_lock(os.path.join(project_dir, "."))

    if os.path.normcase("A") == os.path.normcase("a"):
        assert base is _project_write_lock(project_dir.upper())
