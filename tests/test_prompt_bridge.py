"""Tests for SonderPromptRelayBridge resolution + the prompt-payload route.

The bridge module has no torch/cv2 dependency — it imports through the fake
package so its relative `..server` import resolves.
"""

import asyncio
import importlib
import json
import os
import sys
import types
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"

sys.path.insert(0, str(ROOT))

from server.timeline_state import (  # noqa: E402
    GenerationJob,
    LaneConfig,
    PromptSection,
    Scene,
    TimelineProject,
)


def _import_prompt_bridge():
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.prompt_bridge")


def _project_with_scene(**scene_kwargs):
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=100, **scene_kwargs)
    return TimelineProject(project_dir="", name="Project", scenes=[scene]), scene


def test_bridge_live_resolution_with_ctx_window():
    prompt_bridge = _import_prompt_bridge()
    project, scene = _project_with_scene(
        prompt="global style",
        prompt_sections=[
            PromptSection(0, 30, prompt="walks"),
            PromptSection(30, 60, prompt="runs"),
        ],
    )
    # Editor executed: context-expanded window [20, 60)
    project._execution_context = {"scene_id": "scene-1", "context_start": 20, "context_end": 60}

    payload = prompt_bridge.build_window_relay_payload(project)
    assert payload["source"] == "live"
    assert payload["global_prompt"] == "global style"
    # Rebased real frames: walks covers [0,10), runs holds [10,40)
    assert payload["smart_prompt"] == "[VISUAL]: walks [0-10] | [VISUAL]: runs [10-40]"
    assert payload["local_prompts"] == "[VISUAL]: walks | [VISUAL]: runs"
    assert payload["segment_lengths"] == "10,30"


def test_bridge_window_fallback_without_editor_ctx():
    prompt_bridge = _import_prompt_bridge()
    project, scene = _project_with_scene(
        prompt="g", prompt_sections=[PromptSection(0, 100, prompt="all")],
    )
    payload = prompt_bridge.build_window_relay_payload(project)
    assert payload["window_start"] == 0
    assert payload["window_end"] == 100
    assert payload["segment_lengths"] == "100"


def test_bridge_snapshot_via_queue_job_ref_id_on_peek():
    """Peek runs (no consume) MUST resolve the frozen snapshot — the bridge
    keys off `queue_job_ref_id`, never the consume-only `queue_job_id`."""
    prompt_bridge = _import_prompt_bridge()
    project, scene = _project_with_scene(
        prompt="LIVE GLOBAL EDITED AFTER ENQUEUE",
        prompt_sections=[PromptSection(0, 100, prompt="LIVE EDITED")],
    )
    job = GenerationJob(
        job_id="job-1",
        scene_id="scene-1",
        scene_prompt="frozen global",
        prompt_sections=[
            {"start_frame": 0, "end_frame": 50,
             "channels": {"visual": "frozen A", "speech": "", "sounds": ""}},
            {"start_frame": 50, "end_frame": 100,
             "channels": {"visual": "frozen B", "speech": "", "sounds": ""}},
        ],
        params={"snapshot_version": 1, "prompt_channel_labels": True},
    )
    project.generation_queue = [job]
    project._execution_context = {
        "scene_id": "scene-1",
        "context_start": 0,
        "context_end": 100,
        "queue_job_id": "",            # peek: consume-only handle is EMPTY
        "queue_job_ref_id": "job-1",   # but the snapshot reference is set
    }

    payload = prompt_bridge.build_window_relay_payload(project)
    assert payload["source"] == "snapshot"
    assert payload["global_prompt"] == "frozen global"
    assert "frozen A" in payload["smart_prompt"] and "frozen B" in payload["smart_prompt"]
    assert "LIVE" not in payload["smart_prompt"]
    assert payload["segment_lengths"] == "50,50"


def test_bridge_legacy_v1_snapshot_flat_prompt_dicts():
    prompt_bridge = _import_prompt_bridge()
    project, _scene = _project_with_scene(prompt="live")
    job = GenerationJob(
        job_id="job-1",
        scene_id="scene-1",
        scene_prompt="",  # pre-upgrade jobs default to empty global
        prompt_sections=[{"start_frame": 0, "end_frame": 40, "prompt": "old flat text"}],
        params={"snapshot_version": 1},
    )
    project.generation_queue = [job]
    project._execution_context = {
        "scene_id": "scene-1", "context_start": 0, "context_end": 40,
        "queue_job_ref_id": "job-1",
    }
    payload = prompt_bridge.build_window_relay_payload(project)
    assert payload["smart_prompt"] == "[VISUAL]: old flat text [0-40]"
    assert payload["global_prompt"] == ""


def test_bridge_live_hidden_lanes_and_labels_off():
    prompt_bridge = _import_prompt_bridge()
    project, scene = _project_with_scene(
        prompt="global", prompt_sections=[PromptSection(0, 100, prompt="sec")],
    )
    project.metadata["prompt_channel_labels"] = False
    scene.global_prompt_track_config = LaneConfig(hidden=True)
    payload = prompt_bridge.build_window_relay_payload(project)
    assert payload["global_prompt"] == ""
    assert payload["smart_prompt"] == "sec [0-100]"  # labels off

    scene.prompt_track_config = LaneConfig(hidden=True)
    payload = prompt_bridge.build_window_relay_payload(project)
    assert payload["smart_prompt"] == ""
    assert payload["local_prompts"] == ""
    assert payload["segment_lengths"] == ""


def test_bridge_execute_sanitizes_and_records_provenance():
    prompt_bridge = _import_prompt_bridge()
    project, _scene = _project_with_scene(
        prompt="keep | global [3] verbatim",
        prompt_sections=[
            PromptSection(0, 100, channels={
                "visual": "a|b\nScene 2:\nwith [3] wolves",
                "speech": "", "sounds": "",
            }),
        ],
    )
    project._execution_context = {"scene_id": "scene-1", "context_start": 0, "context_end": 100}

    node = prompt_bridge.SonderPromptRelayBridge()
    global_prompt, smart_prompt, local_prompts, segment_lengths = node.execute(project)

    # Global passes through untouched; locals are sanitized
    assert global_prompt == "keep | global [3] verbatim"
    assert "|" not in local_prompts.replace(" | ", "")  # only our separator remains
    assert "\n" not in smart_prompt
    assert "[3]" not in smart_prompt
    assert smart_prompt.endswith("[0-100]")
    assert segment_lengths == "100"

    # Provenance flows via a public ctx key
    relay_ctx = project._execution_context.get("prompt_relay")
    assert relay_ctx is not None
    assert relay_ctx["global"] == global_prompt
    assert relay_ctx["segments"][0]["start"] == 0
    assert relay_ctx["segments"][0]["end"] == 100
    assert relay_ctx["source"] == "live"


def test_bridge_node_contract():
    prompt_bridge = _import_prompt_bridge()
    node_cls = prompt_bridge.SonderPromptRelayBridge
    assert node_cls.RETURN_TYPES == ("STRING", "STRING", "STRING", "STRING")
    assert node_cls.RETURN_NAMES == ("global_prompt", "smart_prompt", "local_prompts", "segment_lengths")
    assert "project" in node_cls.INPUT_TYPES()["required"]


# --- prompt-payload route ---------------------------------------------------

def _load_route_module(monkeypatch):
    import server
    import server.routes as routes

    fake_prompt_server = types.SimpleNamespace(
        instance=types.SimpleNamespace(routes=web.RouteTableDef(), app=web.Application())
    )
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


class DummyRequest(dict):
    def __init__(self, *, match_info=None, query=None, body=None, method="GET",
                 path="/sonder-editor/project/proj"):
        super().__init__()
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body
        self.method = method
        self.path = path

    async def json(self):
        return self._body


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def test_prompt_payload_route_live_and_snapshot(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=80,
                  prompt="global", prompt_sections=[PromptSection(10, 40, prompt="mid")])
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)

    handler = _route_handler(
        route_module, "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-payload",
    )
    request = DummyRequest(match_info={"project_id": "proj", "scene_id": "scene-1"})

    response = asyncio.run(handler(request))
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status == 200
    assert payload["source"] == "live"
    assert payload["window_end"] == 80
    # Full-scene window: gap-fill extends the only section over everything
    assert payload["relay"]["smart_prompt"] == "[VISUAL]: mid [0-80]"
    assert payload["global_prompt"] == "global"

    # A running snapshot job wins
    job = GenerationJob(
        job_id="job-1", scene_id="scene-1", status="running",
        scene_prompt="frozen", params={"snapshot_version": 1},
        prompt_sections=[{"start_frame": 0, "end_frame": 80, "prompt": "snap"}],
    )
    project.generation_queue = [job]
    response = asyncio.run(handler(request))
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["source"] == "snapshot"
    assert payload["global_prompt"] == "frozen"
    assert "snap" in payload["relay"]["smart_prompt"]
