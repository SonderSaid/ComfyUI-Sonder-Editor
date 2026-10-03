"""A scene may be at most 99,999 frames long (backlog step 1, Phase 3).

Adopts the approved plan "Scene Duration Upper Bound": the cap REFUSES rather
than clamps, binds every authored writer (the mutation route, the legacy scene
PUT and scene create), and never binds a write that lowers the duration, so a
project already holding an over-cap scene loads, accepts unrelated edits and
can be corrected downward. History restore, scene import and project load go
through `Scene.from_dict` and are deliberately not capped.

Also #29: the legacy PUT stored a negative or unreadable duration as given, and
scene create stored the request body's value with no coercion at all. All three
writers now parse with `_mutation_int` and floor at 0, as the mutation route
already did.
"""

import asyncio
import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import routes
from server.timeline_state import ClipReference, Scene, TimelineProject


ROOT = Path(__file__).resolve().parents[1]
CAP = 99999


def _project(duration=100, fps=24.0):
    scene = Scene(scene_id="scene-1", duration_frames=duration)
    return TimelineProject(project_id="p", fps=fps, scenes=[scene])


def _refusal(project, operations):
    with pytest.raises(routes.ProjectMutationRequestError) as caught:
        routes._apply_scene_mutation_batch(project, "scene-1", operations)
    return caught.value


def _fields(**fields):
    return [{"type": "update_scene_fields", "fields": fields}]


# -- the mutation route ------------------------------------------------------------

def test_the_constant_is_the_declared_cap():
    assert routes.MAX_SCENE_DURATION_FRAMES == CAP


def test_an_over_cap_duration_is_refused():
    error = _refusal(_project(), _fields(duration_frames=15351532))
    assert (error.status, error.code) == (400, "duration_limit")
    assert str(CAP) in str(error)


def test_a_duration_exactly_at_the_cap_is_accepted():
    project = _project()
    routes._apply_scene_mutation_batch(project, "scene-1", _fields(duration_frames=CAP))
    assert project.get_scene("scene-1").duration_frames == CAP


def test_an_over_cap_scene_can_be_lowered():
    project = _project(duration=15351532)
    routes._apply_scene_mutation_batch(project, "scene-1", _fields(duration_frames=500))
    assert project.get_scene("scene-1").duration_frames == 500


def test_an_over_cap_scene_can_be_lowered_to_a_value_still_over_the_cap():
    project = _project(duration=15351532)
    routes._apply_scene_mutation_batch(project, "scene-1", _fields(duration_frames=200000))
    assert project.get_scene("scene-1").duration_frames == 200000


def test_an_over_cap_scene_accepts_an_unrelated_edit():
    project = _project(duration=15351532)
    routes._apply_scene_mutation_batch(project, "scene-1", _fields(name="Renamed"))
    scene = project.get_scene("scene-1")
    assert (scene.name, scene.duration_frames) == ("Renamed", 15351532)


def test_an_over_cap_scene_may_not_be_raised():
    project = _project(duration=200000)
    error = _refusal(project, _fields(duration_frames=200001))
    assert error.code == "duration_limit"


def test_an_fps_change_that_retimes_past_the_cap_is_refused():
    """`retime_scene_geometry` writes the duration from the fps branch."""
    error = _refusal(_project(duration=CAP), _fields(fps=48.0))
    assert error.code == "duration_limit"


def test_an_fps_change_lowering_an_over_cap_scene_is_accepted():
    project = _project(duration=200000)
    routes._apply_scene_mutation_batch(project, "scene-1", _fields(fps=12.0))
    assert project.get_scene("scene-1").duration_frames == 100000


@pytest.mark.parametrize("value", ["abc", None, [1], float("inf")])
def test_an_unreadable_duration_is_refused_rather_than_raising(value):
    error = _refusal(_project(), _fields(duration_frames=value))
    assert error.status == 400


# -- the legacy scene PUT and scene create ------------------------------------------

def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


class _Request(dict):
    def __init__(self, *, match_info=None, body=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = {}
        self.headers = {}
        self._body = body

    async def json(self):
        return self._body


def _call(monkeypatch, project, method, path, body):
    route_module = _load_route_module(monkeypatch)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request, **_kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda saved, **_kwargs: saves.append(saved))
    # The mutations route commits through `_apply_project_versioned_sync`; this
    # project never touches disk, so the version check stands in for a commit
    # nobody raced (as `test_project_mutation_pipeline_backend.py` does).
    monkeypatch.setattr(route_module, "verify_project_version",
                        lambda _project, **_kwargs: None)
    handler = _route_handler(route_module, method, path)
    response = asyncio.run(handler(_Request(
        match_info={"project_id": "p", "scene_id": "scene-1"}, body=body)))
    return response, json.loads(response.body.decode("utf-8")), saves


SCENE_PUT = "/sonder-editor/project/{project_id}/scenes/{scene_id}"
SCENE_CREATE = "/sonder-editor/project/{project_id}/scenes"
SCENE_MUTATIONS = "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations"


def test_the_legacy_put_refuses_an_over_cap_duration(monkeypatch):
    project = _project()
    response, payload, saves = _call(monkeypatch, project, "PUT", SCENE_PUT,
                                     {"duration_frames": 15351532})
    assert (response.status, payload["code"], saves) == (400, "duration_limit", [])


def test_the_mutation_route_saves_nothing_when_it_refuses(monkeypatch):
    """The batch runs on a document loaded for this request; a refusal discards it."""
    project = _project()
    response, payload, saves = _call(monkeypatch, project, "POST", SCENE_MUTATIONS,
                                     {"operations": _fields(duration_frames=15351532)})
    assert (response.status, payload["code"], saves) == (400, "duration_limit", [])


def test_the_legacy_put_refuses_an_fps_retime_past_the_cap(monkeypatch):
    project = _project(duration=CAP)
    response, payload, saves = _call(monkeypatch, project, "PUT", SCENE_PUT, {"fps": 48.0})
    assert (response.status, payload["code"], saves) == (400, "duration_limit", [])


def test_the_legacy_put_lowers_an_over_cap_scene(monkeypatch):
    project = _project(duration=15351532)
    response, payload, saves = _call(monkeypatch, project, "PUT", SCENE_PUT,
                                     {"duration_frames": 300})
    assert (response.status, payload["duration_frames"], len(saves)) == (200, 300, 1)


def test_the_legacy_put_floors_a_negative_duration_at_zero(monkeypatch):
    """#29: it stored the value as given."""
    project = _project()
    response, payload, saves = _call(monkeypatch, project, "PUT", SCENE_PUT,
                                     {"duration_frames": -5})
    assert (response.status, payload["duration_frames"], len(saves)) == (200, 0, 1)


def test_the_legacy_put_refuses_an_unreadable_duration_as_json(monkeypatch):
    project = _project()
    response, payload, saves = _call(monkeypatch, project, "PUT", SCENE_PUT,
                                     {"duration_frames": "abc"})
    assert (response.status, saves) == (400, [])
    assert "duration_frames" in payload["error"]


def test_scene_create_refuses_an_over_cap_duration(monkeypatch):
    project = _project()
    response, payload, saves = _call(monkeypatch, project, "POST", SCENE_CREATE,
                                     {"name": "Huge", "duration_frames": 15351532})
    assert (response.status, payload["code"], saves) == (400, "duration_limit", [])
    assert [scene.scene_id for scene in project.scenes] == ["scene-1"]


def test_scene_create_accepts_the_cap(monkeypatch):
    project = _project()
    response, payload, saves = _call(monkeypatch, project, "POST", SCENE_CREATE,
                                     {"name": "Long", "duration_frames": CAP})
    assert (response.status, payload["duration_frames"], len(saves)) == (201, CAP, 1)


@pytest.mark.parametrize(("value", "stored"), [(-5, 0), ("120", 120), (12.9, 12)])
def test_scene_create_parses_and_floors_the_duration(monkeypatch, value, stored):
    """#29: create stored the body's value with no coercion at all."""
    project = _project()
    response, payload, _saves = _call(monkeypatch, project, "POST", SCENE_CREATE,
                                      {"duration_frames": value})
    assert (response.status, payload["duration_frames"]) == (201, stored)
    assert project.scenes[-1].duration_frames == stored


def test_scene_create_keeps_its_default_duration(monkeypatch):
    project = _project()
    response, payload, _saves = _call(monkeypatch, project, "POST", SCENE_CREATE, {})
    assert (response.status, payload["duration_frames"]) == (201, 200)


def test_scene_create_refuses_an_unreadable_duration(monkeypatch):
    project = _project()
    response, _payload, saves = _call(monkeypatch, project, "POST", SCENE_CREATE,
                                      {"duration_frames": "abc"})
    assert (response.status, saves) == (400, [])


# -- not capped: what is not an authored write -------------------------------------

def test_loading_an_over_cap_project_keeps_its_duration():
    data = _project(duration=15351532).to_dict()
    loaded = TimelineProject.from_dict(json.loads(json.dumps(data)))
    assert loaded.get_scene("scene-1").duration_frames == 15351532


# -- the browser's copy of the cap -------------------------------------------------

def test_the_browser_cap_is_the_server_cap():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the duration cap parity")
    module_url = (ROOT / "web" / "js" / "editor_settings.js").as_uri()
    script = f"""
globalThis.localStorage = {{ getItem() {{ return null; }}, setItem() {{}} }};
globalThis.window = {{ localStorage: globalThis.localStorage, addEventListener() {{}} }};
const mod = await import({json.dumps(module_url)});
console.log(JSON.stringify(mod.MAX_SCENE_DURATION_FRAMES));
"""
    result = subprocess.run([node, "--input-type=module"], input=script,
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == routes.MAX_SCENE_DURATION_FRAMES


def test_no_bare_cap_literal_remains_in_the_duration_inputs():
    """The three declarations now read the shared constant."""
    for name in ("editor_top_chrome.js", "editor_settings_panel.js"):
        source = (ROOT / "web" / "js" / name).read_text(encoding="utf-8")
        assert not re.search(r"\b99999\b", source), name
    settings = (ROOT / "web" / "js" / "editor_settings.js").read_text(encoding="utf-8")
    assert len(re.findall(r"\b99999\b", settings)) == 1
