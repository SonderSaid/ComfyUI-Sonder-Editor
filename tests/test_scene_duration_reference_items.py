"""A scene duration change never moves or resizes a Reference item (backlog step 1, Phase 3).

Decisions D1, D2 and D9 of the plan "Backlog Cleanup Step 1":

* **D1.** A duration change, through any writer, leaves every Reference item
  stored as authored, locked lane or not, exactly as clips and audio are left.
  It used to squash an item lying wholly past the new end to `[d-1, d)` (#80),
  where two such items then collided, and the client never adopted the squash.
  Live resolution treats the part of an item past the scene end as absent.
* **D2.** An fps retime still scales Reference items, since it changes the
  timebase rather than resizing anything, but no longer clamps a start to
  `duration - 1`, which matches clips.
* **The canvas, the item editor and the gestures** resolve a stored `-1` end
  by the rule the route's overlap check already uses,
  `max(start + 1, duration)`, through one parity-tested helper.
"""

import asyncio
import importlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import routes
from server.reference_resolution import resolve_reference_verdicts
from server.timeline_state import (
    LaneConfig,
    ReferenceItem,
    ReferenceLaneRecipe,
    Scene,
    TimelineProject,
    retime_scene_geometry,
)


ROOT = Path(__file__).resolve().parents[1]

# Scene of 100 frames, shrunk to 60 by the tests. Lane 1 is locked.
ROWS = [
    ("inside", 0, 0, 30),
    ("straddle", 0, 40, 90),     # straddles the new end
    ("past", 0, 92, 98),          # wholly past the new end
    ("tail", 1, 80, -1),          # a sentinel row starting past the new end, locked lane
    ("locked-past", 1, 65, 75),   # wholly past the new end, locked lane
]


def _rows():
    return [{"reference_item_id": item_id, "lane_index": lane, "start_frame": start,
             "end_frame": end, "members": []}
            for item_id, lane, start, end in ROWS]


def _project(duration=100):
    scene = Scene(
        scene_id="scene-1", duration_frames=duration,
        reference_lane_count=2,
        reference_lane_configs=[LaneConfig(), LaneConfig(locked=True)],
        reference_lane_recipes=[ReferenceLaneRecipe(lane_id="lane-0"),
                                ReferenceLaneRecipe(lane_id="lane-1")],
        reference_items=[ReferenceItem.from_dict(row) for row in _rows()])
    return TimelineProject(project_id="p", fps=24.0, scenes=[scene])


def _stored(project):
    return {item.reference_item_id: (item.lane_index, item.start_frame, item.end_frame)
            for item in project.get_scene("scene-1").reference_items}


AUTHORED = {item_id: (lane, start, end) for item_id, lane, start, end in ROWS}


def _batch(project, operations):
    return routes._apply_scene_mutation_batch(project, "scene-1", operations)


# -- D1: the three duration writers ------------------------------------------------

def test_a_shrink_through_the_mutation_route_leaves_every_reference_item_as_authored():
    project = _project()
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 60}}])
    assert project.get_scene("scene-1").duration_frames == 60
    assert _stored(project) == AUTHORED


def test_growing_back_restores_the_original_coverage():
    project = _project()
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 60}}])
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 100}}])
    assert _stored(project) == AUTHORED


def test_a_scalar_write_on_a_row_past_the_end_keeps_its_range():
    """The squash moved such a row on every shrink, and a write then saw it moved."""
    project = _project()
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 20}}])
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "past",
                      "expected": {"start_frame": 92, "end_frame": 98, "strength": 1.0},
                      "fields": {"strength": 0.5}}])
    assert _stored(project) == AUTHORED


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


def _legacy_put(monkeypatch, project, body):
    route_module = _load_route_module(monkeypatch)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request, **_kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda saved, **_kwargs: saves.append(saved))
    handler = _route_handler(route_module, "PUT",
                             "/sonder-editor/project/{project_id}/scenes/{scene_id}")
    response = asyncio.run(handler(_Request(
        match_info={"project_id": "p", "scene_id": "scene-1"}, body=body)))
    return response, saves


def test_a_shrink_through_the_legacy_scene_put_leaves_every_reference_item_as_authored(monkeypatch):
    project = _project()
    response, saves = _legacy_put(monkeypatch, project, {"duration_frames": 60})
    assert response.status == 200
    assert len(saves) == 1
    assert project.get_scene("scene-1").duration_frames == 60
    assert _stored(project) == AUTHORED
    served = {row["reference_item_id"]: (row["lane_index"], row["start_frame"], row["end_frame"])
              for row in json.loads(response.body.decode("utf-8"))["reference_items"]}
    assert served == AUTHORED


# -- D2: an fps retime scales, and clamps nothing ---------------------------------

def test_a_retime_scales_reference_items_past_the_end_without_clamping_them():
    scene = _project(duration=60).get_scene("scene-1")
    retime_scene_geometry(scene, 24.0, 48.0)
    assert scene.duration_frames == 120
    stored = {item.reference_item_id: (item.start_frame, item.end_frame)
              for item in scene.reference_items}
    # Every bound doubles; the old clamp put "past" and "tail" at 119.
    assert stored == {"inside": (0, 60), "straddle": (80, 180), "past": (184, 196),
                      "tail": (160, -1), "locked-past": (130, 150)}


def test_a_retime_halving_the_rate_rounds_half_up_as_clips_do():
    scene = _project(duration=60).get_scene("scene-1")
    retime_scene_geometry(scene, 24.0, 12.0)
    stored = {item.reference_item_id: (item.start_frame, item.end_frame)
              for item in scene.reference_items}
    assert stored == {"inside": (0, 15), "straddle": (20, 45), "past": (46, 49),
                      "tail": (40, -1), "locked-past": (33, 38)}


def test_an_fps_change_through_the_mutation_route_scales_without_squashing():
    project = _project(duration=60)
    _batch(project, [{"type": "update_scene_fields", "fields": {"fps": 48.0}}])
    assert _stored(project)["past"] == (0, 184, 196)
    assert _stored(project)["tail"] == (1, 160, -1)


# -- live resolution: the part past the end is absent -----------------------------

RESOLVER_CASES = {
    # A row wholly past the end used to be squashed to [d-1, d) and then WON
    # its lane against an earlier row, being the more specific of the two.
    "a row past the end does not win": dict(
        items=[{"reference_item_id": "a", "lane_index": 0, "start_frame": 10, "end_frame": 20},
               {"reference_item_id": "b", "lane_index": 0, "start_frame": 25, "end_frame": 60}],
        duration=20, start=0, end=20,
        verdicts={0: "winner", 1: "outside"}),
    "a sentinel row starting past the end is outside": dict(
        items=[{"reference_item_id": "a", "lane_index": 0, "start_frame": 0, "end_frame": 10},
               {"reference_item_id": "t", "lane_index": 0, "start_frame": 30, "end_frame": -1}],
        duration=20, start=0, end=20,
        verdicts={0: "winner", 1: "outside"}),
    "a sentinel row starting exactly at the end is outside": dict(
        items=[{"reference_item_id": "t", "lane_index": 0, "start_frame": 20, "end_frame": -1}],
        duration=20, start=0, end=20,
        verdicts={0: "outside"}),
    "a straddling row is clipped to the scene": dict(
        # Clipped to [10, 20) it is exactly as specific as "a" for the window
        # [10, 20), and the later start wins the tie.
        items=[{"reference_item_id": "a", "lane_index": 0, "start_frame": 5, "end_frame": 20},
               {"reference_item_id": "s", "lane_index": 0, "start_frame": 10, "end_frame": 90}],
        duration=20, start=10, end=20,
        verdicts={0: "superseded", 1: "winner"}),
    "a scene without rows past the end is unchanged": dict(
        items=[{"reference_item_id": "broad", "lane_index": 0, "start_frame": 0, "end_frame": -1},
               {"reference_item_id": "narrow", "lane_index": 0, "start_frame": 20, "end_frame": 40},
               {"reference_item_id": "late", "lane_index": 1, "start_frame": 60, "end_frame": 100},
               {"reference_item_id": "muted", "lane_index": 1, "start_frame": 0, "end_frame": 50,
                "muted": True}],
        duration=100, start=25, end=35, count=2,
        verdicts={0: "superseded", 1: "winner", 2: "outside", 3: "excluded"}),
}


def _python_verdicts(case):
    resolved = resolve_reference_verdicts(
        reference_items=case["items"], lane_count=case.get("count", 1),
        scene_duration=case["duration"], window_start=case["start"],
        window_end=case["end"])
    return {int(index): verdict for index, verdict in resolved["verdicts"].items()}


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Reference resolution parity")
    result = subprocess.run([node, "--input-type=module"], input=script,
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _javascript_verdicts(cases):
    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map((c) => Object.fromEntries(mod.resolveReferenceVerdicts({{
  referenceItems: c.items, laneCount: c.count ?? 1, sceneDuration: c.duration,
  windowStart: c.start, windowEnd: c.end,
}}).verdicts))));
"""
    return [{int(index): verdict for index, verdict in row.items()} for row in _node(script)]


@pytest.mark.parametrize("name", sorted(RESOLVER_CASES))
def test_live_resolution_treats_the_part_past_the_end_as_absent(name):
    case = RESOLVER_CASES[name]
    assert _python_verdicts(case) == case["verdicts"]


def test_the_browser_resolver_agrees_with_the_server_past_the_end():
    names = sorted(RESOLVER_CASES)
    cases = [RESOLVER_CASES[name] for name in names]
    assert dict(zip(names, _javascript_verdicts(cases))) == {
        name: RESOLVER_CASES[name]["verdicts"] for name in names}


# -- the client's reading of a stored `-1` ----------------------------------------

EFFECTIVE_CASES = [
    # (start, stored end, duration)
    (0, -1, 100),
    (40, -1, 100),
    (99, -1, 100),
    (100, -1, 100),    # starts at the end: one frame, not an empty or inverted bar
    (130, -1, 100),
    (10, 60, 100),
    (92, 98, 60),      # an explicit end is never resolved against the duration
    (0, -1, 0),
    (5, -1, 0),
]


def _javascript_effective(cases):
    module_url = (ROOT / "web" / "js" / "scene_reference_geometry.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const cases = {json.dumps([list(case) for case in cases])};
console.log(JSON.stringify(cases.map(([start, end, duration]) => {{
  const bounds = mod.referenceItemEffectiveBounds(
    {{ start_frame: start, end_frame: end }}, duration);
  return [bounds.start, bounds.end];
}})));
"""
    return _node(script)


def test_the_client_resolves_a_stored_sentinel_as_the_route_does():
    python = [list(routes._reference_effective_bounds(
        duration, SimpleNamespace(start_frame=start, end_frame=end)))
        for start, end, duration in EFFECTIVE_CASES]
    assert _javascript_effective(EFFECTIVE_CASES) == python
    assert python[4] == [130, 131], "a sentinel row past the end keeps one frame"


def test_the_canvas_and_the_item_editor_read_a_sentinel_through_the_helper():
    """The draw, both hit tests and the item editor resolve `-1` one way.

    Source-level by necessity: the canvas functions take a host whose layout
    and geometry would all be stand-ins. The helper itself is parity-tested
    above; this pins that the Reference branches call it instead of reading
    `totalFrames` for a sentinel.
    """
    canvas = (ROOT / "web" / "js" / "editor_timeline_canvas.js").read_text(encoding="utf-8")
    assert canvas.count("referenceItemEffectiveBounds(") == 4, "draw, hit test, edge, trim ghost"
    # No Reference sentinel is resolved any other way in the canvas.
    assert "end_frame === -1" not in canvas and "end_frame == -1" not in canvas
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    editor = widget.split("    _showItemEditor(", 1)[1].split("\n    }\n", 1)[0]
    assert "referenceItemEffectiveBounds" in editor


# -- a sentinel written in a shrunk scene (audit, Phase 3 #2) ----------------------

def _lane_project():
    scene = Scene(
        scene_id="scene-1", duration_frames=1000, reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(lane_id="lane-0")],
        reference_items=[ReferenceItem.from_dict(row) for row in (
            {"reference_item_id": "a", "lane_index": 0, "start_frame": 100,
             "end_frame": 500, "members": []},
            {"reference_item_id": "b", "lane_index": 0, "start_frame": 600,
             "end_frame": 700, "members": []},
        )])
    return TimelineProject(project_id="p", fps=24.0, scenes=[scene])


def _trim_to(project, end):
    return _batch(project, [{"type": "update_reference_item", "reference_item_id": "a",
                             "expected": {"start_frame": 100, "end_frame": 500},
                             "fields": {"start_frame": 100, "end_frame": end}}])


def test_a_sentinel_beside_a_row_past_the_end_is_refused():
    """`-1` there would overlap "b" as soon as the scene grew back past 600."""
    project = _lane_project()
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 300}}])
    with pytest.raises(routes.ProjectMutationRequestError) as caught:
        _trim_to(project, -1)
    assert caught.value.code == "lane_collision"
    assert _stored(project)["a"] == (0, 100, 500)


def test_the_explicit_scene_end_is_accepted_and_survives_growing_back():
    project = _lane_project()
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 300}}])
    _trim_to(project, 300)
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 1000}}])
    scene = project.get_scene("scene-1")
    assert _stored(project) == {"a": (0, 100, 300), "b": (0, 600, 700)}
    assert not list(routes._scene_history_content_violations(scene))


def test_a_sentinel_with_no_later_row_is_still_written():
    project = _lane_project()
    project.get_scene("scene-1").reference_items.pop()
    _batch(project, [{"type": "update_scene_fields", "fields": {"duration_frames": 300}}])
    _trim_to(project, -1)
    assert _stored(project) == {"a": (0, 100, -1)}


def test_history_judges_a_stored_sentinel_as_stored():
    """Only a REQUESTED `-1` runs without limit. A stored one beside a row past
    the end is what a project may already hold, and history restore must not
    start refusing it: at rest it resolves to the scene end."""
    project = _lane_project()
    scene = project.get_scene("scene-1")
    scene.duration_frames = 300
    scene.reference_items[0].end_frame = -1
    assert not [violation for violation in routes._scene_history_content_violations(scene)
                if violation["code"] == "reference_overlap"]
