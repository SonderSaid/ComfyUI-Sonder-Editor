"""Tests for project manager — create, save, load, list."""

import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.project_manager import create_project, load_project, save_project, list_projects
from server.timeline_state import ClipReference, Scene


def test_create_project():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Test Video", fps=30.0, width=1920, height=1080, base_dir=base_dir)

        assert project.name == "Test Video"
        assert project.fps == 30.0
        assert project.resolution == (1920, 1080)
        assert os.path.isdir(project.project_dir)
        assert os.path.isfile(os.path.join(project.project_dir, "project.json"))
        assert os.path.isdir(os.path.join(project.project_dir, "media"))
        assert os.path.isdir(os.path.join(project.project_dir, "renders"))
        assert os.path.isdir(os.path.join(project.project_dir, "exports"))
        assert os.path.isdir(os.path.join(project.project_dir, "cache", "thumbnails"))
        assert os.path.isdir(os.path.join(project.project_dir, "cache", "waveforms"))


def test_save_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Roundtrip Test", base_dir=base_dir)

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
        assert len(loaded.clips) == 1
        assert loaded.clips[0].clip_id == "test_clip"
        assert loaded.clips[0].timeline_end_frame == 48


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


def test_load_project_missing():
    import pytest
    with tempfile.TemporaryDirectory() as base_dir:
        with pytest.raises(FileNotFoundError):
            load_project(os.path.join(base_dir, "no_such_project"))


def test_safe_dirname_special_chars():
    from server.project_manager import _safe_dirname

    assert _safe_dirname("My Video!@#$") == "My-Video____"
    assert _safe_dirname("  spaces  ") == "spaces"
    assert _safe_dirname("") == "untitled"
    assert _safe_dirname("normal-name") == "normal-name"
