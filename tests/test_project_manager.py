"""Tests for project manager — create, save, load, list."""

import sys
import os
import tempfile
import builtins
import copy
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.project_manager import create_project, load_project, save_project, list_projects
from server.timeline_state import ClipReference, Scene


def test_create_project():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Test Video", fps=30.0, width=1920, height=1080, template_id="ltx-2.3", base_dir=base_dir)

        assert project.name == "Test Video"
        assert project.fps == 30.0
        assert project.resolution == (1920, 1080)
        assert project.template_id == "ltx-2.3"
        assert os.path.isdir(project.project_dir)
        assert os.path.isfile(os.path.join(project.project_dir, "project.json"))
        assert os.path.isdir(os.path.join(project.project_dir, "media"))
        assert os.path.isdir(os.path.join(project.project_dir, "media", "Exports"))
        assert os.path.isdir(os.path.join(project.project_dir, "renders"))
        assert os.path.isdir(os.path.join(project.project_dir, "cache", "thumbnails"))
        assert os.path.isdir(os.path.join(project.project_dir, "cache", "waveforms"))
        assert os.path.isdir(os.path.join(project.project_dir, "cache", "bridge_out"))


def test_save_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Roundtrip Test", base_dir=base_dir)
        project.template_id = "ltx-2.3"

        clip = ClipReference(
            clip_id="test_clip",
            source_path="/fake/path.mp4",
            timeline_start_frame=0,
            timeline_end_frame=48,
        )
        project.add_clip(clip)
        save_project(project)

        loaded = load_project(project.project_dir)

        assert loaded.name == "Roundtrip Test"
        assert loaded.project_id == project.project_id
        assert loaded.template_id == "ltx-2.3"
        assert len(loaded.clips) == 1
        assert loaded.clips[0].clip_id == "test_clip"
        assert loaded.clips[0].timeline_end_frame == 48


def test_load_project_retries_only_transient_permission_errors(monkeypatch):
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Read Retry", base_dir=base_dir)
        project_file = os.path.abspath(os.path.join(project.project_dir, "project.json"))
        original_open = builtins.open
        attempts = {"count": 0}

        def flaky_open(file, mode="r", *args, **kwargs):
            if os.path.abspath(str(file)) == project_file and "r" in str(mode):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise PermissionError(13, "sharing violation", str(file))
            return original_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky_open)
        loaded = load_project(project.project_dir)
        assert loaded.project_id == project.project_id
        assert attempts["count"] == 3


def test_load_project_does_not_retry_malformed_json(monkeypatch):
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Bad Json", base_dir=base_dir)
        project_file = os.path.join(project.project_dir, "project.json")
        with open(project_file, "w", encoding="utf-8") as handle:
            handle.write("{")
        calls = {"count": 0}
        original_load = json.load

        def counted_load(handle):
            calls["count"] += 1
            return original_load(handle)

        monkeypatch.setattr(json, "load", counted_load)
        with pytest.raises(json.JSONDecodeError):
            load_project(project.project_dir)
        assert calls["count"] == 1


@pytest.mark.parametrize("corrupt", [
    '{{"modified_at": {version}, "name": "truncated"',
    '{{"modified_at": {version}, invalid-json}}',
])
def test_save_cas_rejects_malformed_matching_version_without_changing_bytes_or_shadow(
        corrupt, tmp_path):
    project = create_project("Fail Closed CAS", base_dir=str(tmp_path))
    project_file = os.path.join(project.project_dir, "project.json")
    expected = project.modified_at
    poisoned = corrupt.format(version=json.dumps(expected))
    with open(project_file, "w", encoding="utf-8") as handle:
        handle.write(poisoned)
    before_shadow = copy.deepcopy(project._raw_data)

    with pytest.raises(json.JSONDecodeError):
        save_project(project, expected_modified_at=expected, notify=False)

    assert open(project_file, encoding="utf-8").read() == poisoned
    assert project._raw_data == before_shadow


def test_save_cas_read_retries_transient_permission_errors(monkeypatch, tmp_path):
    project = create_project("CAS Read Retry", base_dir=str(tmp_path))
    project_file = os.path.abspath(os.path.join(project.project_dir, "project.json"))
    expected = project.modified_at
    original_open = builtins.open
    attempts = {"count": 0}

    def flaky_open(file, mode="r", *args, **kwargs):
        if os.path.abspath(str(file)) == project_file and "r" in str(mode):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise PermissionError(13, "sharing violation", str(file))
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    save_project(project, expected_modified_at=expected, notify=False)

    assert attempts["count"] == 3
    assert load_project(project.project_dir).modified_at == project.modified_at


def test_save_uses_one_exact_serialized_string_for_disk_and_shadow(
        monkeypatch, tmp_path):
    from server import project_manager

    project = create_project("Single Encode", base_dir=str(tmp_path))
    project._raw_data["future_project"] = {
        7: ("tuple", {"nested": (1, 2)}),
    }
    data = project.to_dict(include_internal=True)
    expected = json.dumps(data, indent=2, ensure_ascii=False)
    original_dumps = json.dumps
    original_replace = project_manager.atomic_replace
    calls = []
    replaced_text = []

    def counted_dumps(*args, **kwargs):
        calls.append((args, kwargs))
        return original_dumps(*args, **kwargs)

    def capture_replace(source, destination):
        replaced_text.append(open(source, "rb").read())
        return original_replace(source, destination)

    monkeypatch.setattr(project_manager.json, "dumps", counted_dumps)
    monkeypatch.setattr(project_manager, "atomic_replace", capture_replace)
    save_project(project, bump_modified_at=False, notify=False)

    project_file = os.path.join(project.project_dir, "project.json")
    persisted = open(project_file, "rb").read()
    assert len(calls) == 1
    assert replaced_text == [expected.encode("utf-8")]
    assert persisted == expected.encode("utf-8")
    assert not persisted.endswith(b"\n")
    assert project._raw_data == json.loads(expected)
    assert project._raw_data["future_project"]["7"] == [
        "tuple", {"nested": [1, 2]}]


@pytest.mark.parametrize("failure", ["encode", "write", "replace"])
def test_save_failure_does_not_advance_shadow_or_replace_durable_bytes(
        monkeypatch, tmp_path, failure):
    from server import project_manager

    project = create_project("Shadow Failure", base_dir=str(tmp_path))
    project_file = os.path.join(project.project_dir, "project.json")
    durable_before = open(project_file, "rb").read()
    shadow_before = copy.deepcopy(project._raw_data)
    original_open = builtins.open

    if failure == "encode":
        project.metadata["not_json"] = object()
    elif failure == "write":
        class PartialWriter:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def write(self, value):
                self.handle.write(value[:64])
                self.handle.flush()
                raise OSError("partial temp write refused")

        def reject_temp_write(file, mode="r", *args, **kwargs):
            if str(file).endswith(".tmp") and "w" in str(mode):
                return PartialWriter(original_open(file, mode, *args, **kwargs))
            return original_open(file, mode, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", reject_temp_write)
    else:
        monkeypatch.setattr(
            project_manager, "atomic_replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace refused")))

    with pytest.raises((TypeError, OSError)):
        save_project(project, bump_modified_at=False, notify=False)

    assert project._raw_data == shadow_before
    assert open(project_file, "rb").read() == durable_before
    assert not [name for name in os.listdir(project.project_dir)
                if name.startswith("project.json.") and name.endswith(".tmp")]


def test_legacy_template_id_roundtrips_unchanged():
    """LTX rename (#25) backward compat: the backend treats template_id as an
    opaque string with no registry validation, so a project saved with the
    legacy "ltxv-2.3" id must load and round-trip the string unchanged. The
    frontend (getTemplateById alias in editor_settings.js) canonicalizes it to
    "ltx-2.3" on resolution and the next save persists the new id; the backend
    deliberately does not rewrite it."""
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Legacy Template", base_dir=base_dir)
        project.template_id = "ltxv-2.3"
        save_project(project)

        loaded = load_project(project.project_dir)

        assert loaded.template_id == "ltxv-2.3"


def test_list_projects():
    with tempfile.TemporaryDirectory() as base_dir:
        create_project("Project A", base_dir=base_dir)
        create_project("Project B", fps=30.0, base_dir=base_dir)

        projects = list_projects(base_dir)

        assert len(projects) == 2
        names = {p["name"] for p in projects}
        assert "Project A" in names
        assert "Project B" in names

        for p in projects:
            assert "project_id" in p
            assert "path" in p
            assert "clip_count" in p
            assert "scene_count" in p
            assert "asset_count" in p


def test_list_projects_empty_dir():
    with tempfile.TemporaryDirectory() as base_dir:
        projects = list_projects(base_dir)
        assert projects == []


def test_list_projects_nonexistent_dir():
    projects = list_projects("/nonexistent/path/that/should/not/exist")
    assert projects == []


def test_route_project_lookup_reads_utf8_index_by_folder_and_project_id(monkeypatch):
    from server import routes

    class DummyRequest:
        def __init__(self, project_id):
            self.match_info = {"project_id": project_id}
            self.query = {}
            self.method = "GET"

    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Dance", base_dir=base_dir)
        project.name = "Second Pass 🎬"
        save_project(project)

        project_file = os.path.abspath(os.path.join(project.project_dir, "project.json"))
        original_open = builtins.open

        def checked_open(file, mode="r", *args, **kwargs):
            if os.path.abspath(str(file)) == project_file and "r" in str(mode):
                assert kwargs.get("encoding") == "utf-8"
            return original_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(routes, "_get_base_dir", lambda: base_dir)
        monkeypatch.setattr(builtins, "open", checked_open)

        by_folder = routes._load_project_from_request(DummyRequest("Dance"))
        by_project_id = routes._load_project_from_request(DummyRequest(project.project_id))

        assert by_folder.project_id == project.project_id
        assert by_folder.name == "Second Pass 🎬"
        assert by_project_id.project_dir == project.project_dir


def test_route_project_lookup_uses_direct_folder_without_scanning(monkeypatch):
    from server import routes

    class DummyRequest:
        def __init__(self, project_id):
            self.match_info = {"project_id": project_id}
            self.query = {}
            self.method = "GET"

    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Direct Folder", base_dir=base_dir)
        folder_id = os.path.basename(os.path.normpath(project.project_dir))

        monkeypatch.setattr(routes, "_get_base_dir", lambda: base_dir)
        monkeypatch.setattr(os, "listdir", lambda _path: (_ for _ in ()).throw(AssertionError("scan should not run")))

        by_folder = routes._load_project_from_request(DummyRequest(folder_id))

        assert by_folder.project_dir == project.project_dir


def test_project_saved_event_publishes_canonical_and_folder_aliases(monkeypatch):
    from server import routes

    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Alias Project", base_dir=base_dir)
        events = []

        monkeypatch.setattr(
            routes,
            "schedule_project_event",
            lambda project_id, event: events.append((project_id, event)),
        )

        routes._project_saved_event(project)

        folder_id = os.path.basename(os.path.normpath(project.project_dir))
        assert project.project_id != folder_id
        assert [project_id for project_id, _event in events] == [project.project_id, folder_id]
        assert [event["project_id"] for _project_id, event in events] == [project.project_id, folder_id]
        assert {event["canonical_project_id"] for _project_id, event in events} == {project.project_id}
        assert all(event["type"] == "project_updated" for _project_id, event in events)


def test_project_saved_event_dedupes_matching_alias(monkeypatch):
    from server import routes

    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Same Alias", base_dir=base_dir)
        folder_id = os.path.basename(os.path.normpath(project.project_dir))
        project.project_id = folder_id
        events = []

        monkeypatch.setattr(
            routes,
            "schedule_project_event",
            lambda project_id, event: events.append((project_id, event)),
        )

        routes._project_saved_event(project)

        assert [project_id for project_id, _event in events] == [folder_id]
        assert events[0][1]["project_id"] == folder_id


def test_save_project_diag_uses_folder_alias(monkeypatch):
    from server import session_registry

    events = []

    def capture(kind, project_id="", host_id="", **details):
        events.append((kind, project_id, host_id, details))

    monkeypatch.setattr(session_registry, "record_diag_event", capture)

    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Diag Alias", base_dir=base_dir)
        events.clear()

        save_project(project)

        folder_id = os.path.basename(os.path.normpath(project.project_dir))
        saved_events = [event for event in events if event[0] == "project_saved"]
        assert len(saved_events) == 1
        kind, project_id, host_id, details = saved_events[0]
        assert kind == "project_saved"
        assert project_id == folder_id
        assert project_id != project.project_id
        assert host_id == ""
        assert details["canonical_project_id"] == project.project_id
        assert details["modified_at"] == project.modified_at
        assert details["bumped"] is True
        assert details["caller"]


def test_load_project_missing():
    import pytest
    with tempfile.TemporaryDirectory() as base_dir:
        with pytest.raises(FileNotFoundError):
            load_project(os.path.join(base_dir, "no_such_project"))


def test_create_project_idempotent():
    """BUG-1 regression test: re-creating a project with the same name must NOT
    overwrite existing data — it should load the existing project."""
    with tempfile.TemporaryDirectory() as base_dir:
        # Create project and add a scene with clips
        project = create_project("Test", base_dir=base_dir)
        original_id = project.project_id
        scene = Scene(name="My Scene")
        clip = ClipReference(
            clip_id="important_clip",
            source_path="/fake/video.mp4",
            timeline_start_frame=0,
            timeline_end_frame=100,
        )
        scene.clips.append(clip)
        project.scenes.append(scene)
        save_project(project)

        # Re-create with same name — should load existing, not overwrite
        project2 = create_project("Test", base_dir=base_dir)

        assert project2.project_id == original_id
        assert len(project2.scenes) == 1
        assert len(project2.scenes[0].clips) == 1
        assert project2.scenes[0].clips[0].clip_id == "important_clip"


def test_create_project_can_report_created_without_guessing_from_loaded_state():
    with tempfile.TemporaryDirectory() as base_dir:
        first, first_created = create_project(
            "Created Flag", base_dir=base_dir, return_created=True)
        first.metadata["prompt_channel_template"] = "standard"
        save_project(first)

        second, second_created = create_project(
            "Created Flag", base_dir=base_dir, return_created=True)

        assert first_created is True
        assert second_created is False
        assert second.metadata["prompt_channel_template"] == "standard"


def test_save_project_retries_transient_permission_error(monkeypatch):
    """save_project survives a transient Windows-style PermissionError by retrying
    the atomic os.replace, and leaves no orphan temp file behind."""
    from server import atomic_io

    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Retry Test", base_dir=base_dir)
        project.name = "Renamed Before Save"

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "transient lock")
            return real_replace(src, dst)

        monkeypatch.setattr(atomic_io.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)

        save_project(project)

        assert calls["n"] == 3  # failed twice, succeeded on the third attempt
        assert load_project(project.project_dir).name == "Renamed Before Save"
        leftovers = [f for f in os.listdir(project.project_dir) if f.endswith(".tmp")]
        assert leftovers == []


def test_atomic_replace_exhausts_retries_and_cleans_temp(monkeypatch, tmp_path):
    """On a persistent lock atomic_replace re-raises PermissionError after the
    configured retries and removes the orphan temp; the destination is untouched."""
    import pytest
    from server import atomic_io

    src = tmp_path / "payload.tmp"
    dst = tmp_path / "payload.json"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    attempts = {"n": 0}

    def always_locked(_src, _dst):
        attempts["n"] += 1
        raise PermissionError(13, "stuck lock")

    monkeypatch.setattr(atomic_io.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(atomic_io.os, "replace", always_locked)

    with pytest.raises(PermissionError):
        atomic_io.atomic_replace(str(src), str(dst))

    assert attempts["n"] == atomic_io.ATOMIC_REPLACE_RETRIES
    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "old"


def test_safe_dirname_special_chars():
    from server.project_manager import _safe_dirname

    assert _safe_dirname("My Video!@#$") == "My-Video____"
    assert _safe_dirname("  spaces  ") == "spaces"
    assert _safe_dirname("") == "untitled"
    assert _safe_dirname("normal-name") == "normal-name"
