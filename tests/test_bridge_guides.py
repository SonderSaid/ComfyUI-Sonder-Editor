"""Tests for the Sonder Guides Bridge — Start/End loop pair."""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _import_bridge_nodes(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")

    folder_paths = types.SimpleNamespace(get_output_directory=lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg

    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.bridge_nodes")


def _write_png(path: Path, color=(64, 128, 192), size=(8, 8)):
    pytest.importorskip("PIL")
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def _make_project_with_guides(tmp_path, monkeypatch, guide_frames):
    """Build a TimelineProject with the supplied guide frames pointing at real PNGs."""
    bridge = _import_bridge_nodes(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")

    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    project = timeline_state.TimelineProject(
        project_dir=str(project_dir),
        name="BridgeTest",
        fps=24.0,
        resolution=(64, 32),
    )

    for guide in guide_frames:
        asset_id = guide["asset_id"]
        rel_path = os.path.join("media", f"{asset_id}.png")
        _write_png(project_dir / rel_path)
        asset = timeline_state.Asset(
            asset_id=asset_id,
            name=f"{asset_id}.png",
            asset_type="image",
            path=rel_path,
        )
        project.assets.append(asset)

    positive_frames = [g["frame_index"] for g in guide_frames if g["frame_index"] >= 0]
    duration_frames = max(20, max(positive_frames) + 1) if positive_frames else 20
    scene = timeline_state.Scene(
        scene_id="scene-1",
        name="Scene 1",
        duration_frames=duration_frames,
    )
    scene.guide_frames = [
        timeline_state.GuideFrame(
            frame_index=g["frame_index"],
            asset_id=g["asset_id"],
            strength=g.get("strength", 1.0),
        )
        for g in guide_frames
    ]
    project.scenes.append(scene)
    return bridge, timeline_state, project, scene


def _set_render_window(project, start, end, scene_id="scene-1"):
    project._execution_context = {
        "scene_id": scene_id,
        "context_start": start,
        "context_end": end,
        "selection_start": start,
        "selection_end": end,
        "queue_job_id": "",
    }


# ── Start: single guide ───────────────────────────────────────────────
def test_start_emits_single_guide(tmp_path, monkeypatch):
    bridge, _ts, project, scene = _make_project_with_guides(
        tmp_path, monkeypatch,
        [{"frame_index": 5, "asset_id": "a", "strength": 0.7}],
    )
    _set_render_window(project, 0, scene.duration_frames)

    node = bridge.SonderGuidesBridgeStart()
    result = node.execute(project, iteration_index=0)

    flow, image, frame_idx, strength = result[:4]
    assert flow == bridge.FLOW_SENTINEL
    assert image.shape == (1, 32, 64, 3)
    assert frame_idx == 5
    assert abs(strength - 0.7) < 1e-6


# ── Start: iteration advances to next guide ───────────────────────────
def test_start_iteration_advances(tmp_path, monkeypatch):
    bridge, _ts, project, scene = _make_project_with_guides(
        tmp_path, monkeypatch,
        [
            {"frame_index": 3, "asset_id": "a", "strength": 0.5},
            {"frame_index": 9, "asset_id": "b", "strength": 0.9},
        ],
    )
    _set_render_window(project, 0, scene.duration_frames)

    node = bridge.SonderGuidesBridgeStart()
    result = node.execute(project, iteration_index=1)

    _flow, _image, frame_idx, strength = result[:4]
    assert frame_idx == 9
    assert abs(strength - 0.9) < 1e-6


# ── Start: render window filters guides and re-bases local index ──────
def test_filtered_guides_respects_render_window(tmp_path, monkeypatch):
    bridge, _ts, project, _scene = _make_project_with_guides(
        tmp_path, monkeypatch,
        [
            {"frame_index": 2, "asset_id": "a"},
            {"frame_index": 6, "asset_id": "b"},
            {"frame_index": 8, "asset_id": "c"},
            {"frame_index": 12, "asset_id": "d"},
        ],
    )
    _set_render_window(project, 5, 10)

    guides = bridge._filtered_guides(project)
    local_indices = [g["local_idx"] for g in guides]
    assert local_indices == [1, 3]


# ── Start: -1 frame_index resolves to last frame ──────────────────────
def test_negative_one_resolves_last_frame(tmp_path, monkeypatch):
    bridge, _ts, project, scene = _make_project_with_guides(
        tmp_path, monkeypatch,
        [{"frame_index": -1, "asset_id": "a"}],
    )
    scene.duration_frames = 20
    _set_render_window(project, 0, 20)

    guides = bridge._filtered_guides(project)
    assert len(guides) == 1
    assert guides[0]["local_idx"] == 19


# ── Start: missing asset is dropped without exception ─────────────────
def test_missing_asset_skipped_gracefully(tmp_path, monkeypatch):
    bridge, _ts, project, scene = _make_project_with_guides(
        tmp_path, monkeypatch,
        [{"frame_index": 5, "asset_id": "real"}],
    )
    # Add a guide referencing a non-existent asset id
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    scene.guide_frames.append(
        timeline_state.GuideFrame(frame_index=7, asset_id="ghost-id", strength=1.0)
    )
    _set_render_window(project, 0, scene.duration_frames)

    guides = bridge._filtered_guides(project)
    assert len(guides) == 1
    assert guides[0]["local_idx"] == 5


# ── End: empty guide list returns Start.value_i passthrough refs ──────
class _FakeDynPrompt:
    def __init__(self, nodes):
        self._nodes = nodes

    def get_node(self, node_id):
        return self._nodes.get(str(node_id))


def test_end_empty_list_returns_passthrough_refs(tmp_path, monkeypatch):
    pytest.importorskip("comfy_execution.graph_utils")
    bridge, _ts, project, _scene = _make_project_with_guides(tmp_path, monkeypatch, [])
    _set_render_window(project, 0, 10)

    dynprompt = _FakeDynPrompt({
        "start-1": {
            "class_type": "SonderGuidesBridgeStart",
            "inputs": {
                "project": ["project-src", 0],
                "iteration_index": 0,
                "value_0": "hello",
                "value_1": 42,
            },
        },
    })
    end = bridge.SonderGuidesBridgeEnd()
    out = end.execute(
        flow_control=["start-1", 0],
        project=project,
        dynprompt=dynprompt,
        unique_id="end-1",
    )

    assert isinstance(out, dict)
    assert "result" in out and "expand" in out
    result = out["result"]
    assert result[0] == "hello"
    assert result[1] == 42
    assert all(v is None for v in result[2:])
    assert out["expand"] == {}


# ── End: final iteration returns body kwargs ──────────────────────────
def test_end_final_iteration_returns_kwargs(tmp_path, monkeypatch):
    bridge, _ts, project, scene = _make_project_with_guides(
        tmp_path, monkeypatch,
        [
            {"frame_index": 2, "asset_id": "a"},
            {"frame_index": 8, "asset_id": "b"},
        ],
    )
    _set_render_window(project, 0, scene.duration_frames)

    dynprompt = _FakeDynPrompt({
        "start-1": {
            "class_type": "SonderGuidesBridgeStart",
            "inputs": {"project": ["src", 0], "iteration_index": 1},
        },
    })
    end = bridge.SonderGuidesBridgeEnd()
    out = end.execute(
        flow_control=["start-1", 0],
        project=project,
        dynprompt=dynprompt,
        unique_id="end-1",
        value_0="final-tensor",
        value_1="final-meta",
    )

    assert isinstance(out, tuple)
    assert out[0] == "final-tensor"
    assert out[1] == "final-meta"
    assert all(v is None for v in out[2:])


# ── End: mid-loop returns expansion graph with cloned Start ───────────
def test_end_mid_loop_expand_shape(tmp_path, monkeypatch):
    pytest.importorskip("comfy_execution.graph_utils")
    bridge, _ts, project, scene = _make_project_with_guides(
        tmp_path, monkeypatch,
        [
            {"frame_index": 2, "asset_id": "a"},
            {"frame_index": 6, "asset_id": "b"},
            {"frame_index": 9, "asset_id": "c"},
        ],
    )
    _set_render_window(project, 0, scene.duration_frames)

    # Body node consumes Start.image, end consumes body.value_0.
    dynprompt = _FakeDynPrompt({
        "start-1": {
            "class_type": "SonderGuidesBridgeStart",
            "inputs": {"project": ["src", 0], "iteration_index": 0},
        },
        "body-1": {
            "class_type": "SonderTestPassthrough",
            "inputs": {"image_in": ["start-1", 1]},
        },
        "end-1": {
            "class_type": "SonderGuidesBridgeEnd",
            "inputs": {
                "flow_control": ["start-1", 0],
                "value_0": ["body-1", 0],
            },
        },
    })

    end = bridge.SonderGuidesBridgeEnd()
    out = end.execute(
        flow_control=["start-1", 0],
        project=project,
        dynprompt=dynprompt,
        unique_id="end-1",
        value_0="body-output-iter0",
    )

    assert isinstance(out, dict)
    assert "expand" in out and "result" in out
    expand = out["expand"]
    # Find the cloned Start in the expansion graph.
    start_clones = [
        (nid, info) for nid, info in expand.items()
        if info.get("class_type") == "SonderGuidesBridgeStart"
    ]
    assert len(start_clones) == 1
    _new_start_id, new_start = start_clones[0]
    assert new_start["inputs"]["iteration_index"] == 1
    # Loop-carried value_0 propagates as the body's output from this iteration.
    assert new_start["inputs"]["value_0"] == "body-output-iter0"
    # Body and close clones exist too.
    body_clones = [info for info in expand.values() if info.get("class_type") == "SonderTestPassthrough"]
    end_clones = [info for info in expand.values() if info.get("class_type") == "SonderGuidesBridgeEnd"]
    assert len(body_clones) == 1
    assert len(end_clones) == 1
