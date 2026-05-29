"""Tests for project manager — create, save, load, list."""

import sys
import os
import tempfile
import builtins

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


def test_safe_dirname_special_chars():
    from server.project_manager import _safe_dirname

    assert _safe_dirname("My Video!@#$") == "My-Video____"
    assert _safe_dirname("  spaces  ") == "spaces"
    assert _safe_dirname("") == "untitled"
    assert _safe_dirname("normal-name") == "normal-name"
