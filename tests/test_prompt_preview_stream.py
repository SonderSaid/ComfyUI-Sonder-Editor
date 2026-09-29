"""The streamed paired prompt preview (`plans/cut-read-fanout.md` §3).

One compile request answers both prompt views: the project is loaded and
version-checked once, the candidate is prepared once, and the live window's
result is delivered before the scene-wide projection compiles. These tests pin:

1. **Parity.** Each streamed record equals the separate compile the client used
   to send for it, across formats, windows, labels, overlays, drafts, dormant
   and overlapping sections, hidden lanes and pending identities.
2. **Streaming.** The first record is readable while the second still compiles;
   one load and one preparation per pair; headers survive streaming.
3. **Failure ownership.** Refusals before the stream are ordinary JSON; a branch
   failure after it is a record; a disconnect skips work not yet started.
4. **The reader.** Fragmented, malformed and truncated streams, in the browser's
   own module.
"""

import asyncio
import copy
import importlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import server
from server import prompt_context, routes
from server.project_storage import ProjectStorageError
from server.timeline_state import LaneConfig, PromptSection

from test_change_certificates import FORMATS, _project, _section

ROOT = Path(__file__).resolve().parents[1]
STREAM_JS = (ROOT / "web/js/prompt_preview_stream.js").as_uri()
COMPILE = "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile"
URL = "/sonder-editor/project/proj/scenes/scene/prompt-context/compile"
VERSION = "2026-09-28T10:00:00.000000"
CUSTOM = prompt_context.normalize_profile({
    "profile_id": "custom", "version": "1", "template_id": "standard",
    "capabilities": {}, "writing_aids": [],
    "validators": [{"kind": "max_final_chars", "limit": 40, "severity": "warning"}],
})


@pytest.fixture
def route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt preview stream coverage")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def _normal(payload):
    return json.loads(json.dumps(payload, sort_keys=True, default=str))


def _stream_project(tmp_path):
    project = _project(tmp_path)
    project.modified_at = VERSION
    project.prompt_context_profiles = [CUSTOM]
    scene = project.get_scene("scene")
    # A dormant section overlapped by an earlier one, and a muted one.
    overlapped = _section("p4", 130, 170, "Overlapped and never compiled.")
    muted = _section("p5", 100, 120, "Muted.")
    muted.muted = True
    scene.prompt_sections += [overlapped, muted]
    return project


def _separate_body(body, *, scene_wide, duration):
    """What the client sent for each view before the streamed pair existed."""
    if not scene_wide:
        return body
    return {**body, "window_start": 0, "window_end": duration,
            "selection_start": 0, "selection_end": duration}


def _streamed(route_module, project, body, projections=("windowed", "scene")):
    prepared = route_module._prepare_prompt_preview_stream_sync(
        project, "scene", body, list(projections))
    if isinstance(prepared[0], int):
        return {"refused": list(prepared)}
    prepared, windows = prepared
    records, windowed = {}, None
    for name in projections:
        reuse = None
        if (name == "scene" and windowed is not None
                and windows["scene"]["render_start"] == windows["windowed"]["render_start"]
                and windows["scene"]["render_end"] == windows["windowed"]["render_end"]):
            reuse = windowed
        line, compiled = route_module._prompt_preview_record_sync(
            prepared, name, windows[name], reuse)
        if name == "windowed":
            windowed = compiled
        assert line.endswith(b"\n") and line.count(b"\n") == 1
        records[name] = json.loads(line)
    return records


# ---------------------------------------------------------------------------
# 1. Parity
# ---------------------------------------------------------------------------

def test_the_stream_constants_match_the_server(route_module):
    js = _node(f"""
const mod = await import({json.dumps(STREAM_JS)});
console.log(JSON.stringify({{ stream: mod.PROMPT_PREVIEW_STREAM,
  projections: [...mod.PROMPT_PREVIEW_PROJECTIONS],
  contentType: mod.PROMPT_PREVIEW_STREAM_CONTENT_TYPE }}));
""")
    assert js == {"stream": route_module.PROMPT_PREVIEW_STREAM,
                  "projections": list(route_module.PROMPT_PREVIEW_PROJECTIONS),
                  "contentType": route_module.PROMPT_PREVIEW_STREAM_CONTENT_TYPE}


def _variants(project):
    """(label, mutate project, body overrides) -- each a distinct compile input."""
    scene = project.get_scene("scene")
    draft = [section.to_dict() for section in scene.prompt_sections]
    draft[1]["channels"] = {"detailed_description": "An unsaved draft line."}
    return [
        ("stored scene, no overlay", None, {"scene": None}),
        ("overlay", None, {}),
        ("draft overlay", None, {"scene": {**scene.to_dict(), "prompt_sections": draft}}),
        ("hidden Reference lane", lambda: setattr(
            scene, "reference_lane_configs", [LaneConfig(name="Pictures"),
                                              LaneConfig(name="Voice", hidden=True)]), {}),
        ("hidden prompt lane", lambda: setattr(
            scene, "prompt_track_config", LaneConfig(hidden=True)), {}),
        ("context and frame constraint", None, {
            "pre_context_frames": 7, "post_context_frames": 9,
            "frame_constraint": {"step": 8, "offset": 1, "min": 9}}),
        ("pending identity", None, {"prompt_semantic_unit_creates": [{
            "type": "create_prompt_semantic_unit", "handle_suggestion": "Barkeep",
            "unit": {"semantic_unit_id": "pending-1", "name": "Barkeep",
                     "kind": "subject", "definition": "a tired barkeep"}}]}),
    ]


@pytest.mark.parametrize("window", [(40, 100), (0, 200), (130, 150)])
def test_a_streamed_pair_equals_the_two_separate_compiles(route_module, tmp_path, window):
    checked = 0
    refused = set()
    for template, profile in FORMATS + [("standard", "custom@1")]:
        for labels_on in (False, True):
            project = _stream_project(tmp_path)
            for label, mutate, overrides in _variants(project):
                if mutate:
                    mutate()
                scene = project.get_scene("scene")
                scene.prompt_context_profile_id = profile
                start, end = window
                body = {"base_modified_at": VERSION, "channel_template": template,
                        "labels_on": labels_on, "window_start": start, "window_end": end,
                        "selection_start": start, "selection_end": end,
                        "scene": scene.to_dict(), **overrides}
                if body["scene"] is None:
                    del body["scene"]
                # Each compile gets its own copy of the loaded project, as each
                # request would from its own load: a compile that mutated the
                # project could not then leak into the one it is compared with.
                snapshot = copy.deepcopy(project)
                records = _streamed(route_module, copy.deepcopy(snapshot), body)
                if "refused" in records:
                    # Refused before the stream opens, exactly as each separate
                    # request is (a speaking identity the format cannot voice).
                    for scene_wide in (False, True):
                        separate = _separate_body(body, scene_wide=scene_wide,
                                                  duration=scene.duration_frames)
                        assert list(route_module._compile_prompt_context_candidate_sync(
                            copy.deepcopy(snapshot), "scene", copy.deepcopy(separate))
                        ) == records["refused"]
                    refused.add((label, profile))
                    checked += 2
                    continue
                assert list(records) == ["windowed", "scene"]
                for name, record in records.items():
                    separate = _separate_body(body, scene_wide=name == "scene",
                                              duration=scene.duration_frames)
                    status, payload = route_module._compile_prompt_context_candidate_sync(
                        copy.deepcopy(snapshot), "scene", copy.deepcopy(separate))
                    context = (label, template, profile, labels_on, window, name)
                    assert record["status"] == status == 200, (context, payload)
                    assert _normal(record["payload"]) == _normal(payload), context
                    assert record["projection"] == name
                    assert record["candidate_base_modified_at"] == VERSION
                    checked += 1
    assert checked == 4 * 2 * len(_variants(_stream_project(tmp_path))) * 2
    # The pending identity compiles where the format lets a subject speak.
    assert ("pending identity", "minimax_h3_ref@1") not in refused
    assert refused <= {("pending identity", profile) for _template, profile in FORMATS
                       + [("standard", "custom@1")]}


def test_a_streamed_scene_record_matches_the_separate_scene_request_the_client_built(
        route_module, tmp_path):
    """The server's derivation is the body `_promptSceneRequestBody` builds."""
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    start = widget.index("\n    _promptSceneRequestBody(")
    method = widget[start:widget.index("\n    }\n", start) + 7]
    body = {"base_modified_at": VERSION, "window_start": 40, "window_end": 100,
            "selection_start": 50, "selection_end": 90, "pre_context_frames": 10,
            "post_context_frames": 10, "labels_on": True, "fps": 24}
    client = _node(f"""
class Host {{ {method} }}
console.log(JSON.stringify(new Host()._promptSceneRequestBody({json.dumps(body)}, 200)));
""")
    project = _stream_project(tmp_path)
    prepared = route_module._prepare_prompt_context_candidate_sync(project, "scene", body)
    assert route_module._scene_projection_body(prepared, body) == client


# ---------------------------------------------------------------------------
# 2-3. The route, over real HTTP
# ---------------------------------------------------------------------------

class _Harness:
    def __init__(self, route_module, monkeypatch, project):
        self.route_module = route_module
        self.project = project
        self.loads = 0
        self.prepares = 0
        self.events = []
        self.compiles = []
        self.scene_gate = None
        self.windowed_gate = None
        self.prepare_gate = None
        self.fail_scene = None

        def load(request, **_kwargs):
            self.loads += 1
            route_module._remember_request_project(request, project)
            return project

        prepare = route_module._prepare_prompt_context_candidate_sync

        def counted_prepare(*args, **kwargs):
            self.prepares += 1
            if self.prepare_gate is not None:
                self.prepare_gate["started"].set()
                assert self.prepare_gate["release"].wait(5.0)
            return prepare(*args, **kwargs)

        compile_prepared = route_module._compile_prepared_prompt_candidate

        def gated_compile(prepared, window, copy_plan_for=None):
            scene_wide = (window["generation_start"], window["generation_end"]) == (
                0, prepared.candidate.duration_frames)
            name = "scene" if scene_wide else "windowed"
            self.compiles.append(name)
            gate = self.scene_gate if scene_wide else self.windowed_gate
            if gate is not None:
                gate["started"].set()
                assert gate["release"].wait(5.0)
            if scene_wide and self.fail_scene is not None:
                raise self.fail_scene
            return compile_prepared(prepared, window, copy_plan_for)

        monkeypatch.setattr(route_module, "_load_project_from_request", load)
        monkeypatch.setattr(route_module, "_prepare_prompt_context_candidate_sync",
                            counted_prepare)
        monkeypatch.setattr(route_module, "_compile_prepared_prompt_candidate", gated_compile)
        monkeypatch.setattr(route_module, "record_diag_event",
                            lambda event, **payload: self.events.append((event, payload)))

    def app(self):
        module = self.route_module
        app = web.Application(middlewares=[
            module._project_error_middleware,
            module._project_version_header_middleware,
            module._sonder_security_middleware,
        ])
        handler = next(route.handler for route in module.routes
                       if route.method == "POST" and route.path == COMPILE)
        app.router.add_post(COMPILE, handler)
        return app

    def run(self, exercise):
        async def main():
            async with TestClient(TestServer(self.app())) as client:
                return await exercise(client)
        return asyncio.run(main())


def _body(project, **overrides):
    scene = project.get_scene("scene")
    return {"base_modified_at": VERSION, "channel_template": "minimax_h3_ref",
            "scene": scene.to_dict(), "window_start": 40, "window_end": 100,
            "selection_start": 40, "selection_end": 100,
            "preview_response": "stream-v1", "projections": ["windowed", "scene"],
            **overrides}


def _gate():
    return {"started": threading.Event(), "release": threading.Event()}


def test_the_live_window_is_readable_while_the_scene_projection_still_compiles(
        route_module, monkeypatch, tmp_path):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))
    harness.scene_gate = _gate()

    async def exercise(client):
        response = await client.post(URL, json=_body(harness.project))
        first = json.loads(await response.content.readline())
        # The scene compile has started and is still blocked.
        blocked = await asyncio.to_thread(harness.scene_gate["started"].wait, 5.0)
        released_before_first = harness.scene_gate["release"].is_set()
        harness.scene_gate["release"].set()
        second = json.loads(await response.content.readline())
        tail = await response.content.read()
        return response, first, blocked, released_before_first, second, tail

    response, first, blocked, released_before_first, second, tail = harness.run(exercise)
    assert (first["projection"], first["status"]) == ("windowed", 200)
    assert blocked and not released_before_first
    assert (second["projection"], second["status"]) == ("scene", 200)
    assert tail == b""
    assert harness.loads == 1 and harness.prepares == 1
    assert harness.compiles == ["windowed", "scene"]
    headers = response.headers
    assert headers["Content-Type"].startswith("application/x-ndjson")
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors" in headers["Content-Security-Policy"]
    assert headers["X-Sonder-Project-Id"] == "proj"
    assert headers["X-Sonder-Project-Modified-At"] == VERSION


def test_a_window_covering_the_scene_is_compiled_once(route_module, monkeypatch, tmp_path):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))

    async def exercise(client):
        response = await client.post(URL, json=_body(
            harness.project, window_start=0, window_end=200,
            selection_start=0, selection_end=200))
        return [json.loads(line) for line in (await response.read()).splitlines()]

    windowed, scene = harness.run(exercise)
    assert len(harness.compiles) == 1
    assert windowed["payload"] == scene["payload"]


def test_only_the_requested_projection_is_compiled(route_module, monkeypatch, tmp_path):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))

    async def exercise(client):
        response = await client.post(URL, json=_body(harness.project, projections=["scene"]))
        return [json.loads(line) for line in (await response.read()).splitlines()]

    records = harness.run(exercise)
    assert [record["projection"] for record in records] == ["scene"]
    assert harness.compiles == ["scene"]


@pytest.mark.parametrize("failure, code, message", [
    (RuntimeError("C:\\secret\\path exploded"), "prompt_preview_projection_failed",
     "Prompt Context preview failed"),
    (ProjectStorageError("C:\\secret\\project.json unreadable"), "project_storage_unreadable",
     routes.PROJECT_STORAGE_UNREADABLE_MESSAGE),
])
def test_a_branch_failure_is_a_record_and_the_other_branch_still_lands(
        route_module, monkeypatch, tmp_path, failure, code, message):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))
    harness.fail_scene = failure

    async def exercise(client):
        response = await client.post(URL, json=_body(harness.project))
        return response.status, [json.loads(line) for line in (await response.read()).splitlines()]

    status, records = harness.run(exercise)
    assert status == 200
    assert [(record["projection"], record["status"]) for record in records] == [
        ("windowed", 200), ("scene", 500)]
    assert records[1]["payload"] == {"error": message, "code": code}
    assert "secret" not in json.dumps(records)


@pytest.mark.parametrize("overrides, status, code", [
    ({"base_modified_at": "2026-09-28T09:00:00.000000"}, 409, "project_version_conflict"),
    ({"preview_response": "stream-v9"}, 400, "invalid_prompt_preview_response"),
    ({"projections": []}, 400, "invalid_prompt_preview_projections"),
    ({"projections": ["windowed", "windowed"]}, 400, "invalid_prompt_preview_projections"),
    ({"projections": ["prompt"]}, 400, "invalid_prompt_preview_projections"),
    ({"copy_plan_for": {"attachment_id": "chip"}}, 400, "invalid_prompt_preview_response"),
    ({"pre_context_frames": "many"}, 400, None),
    ({"prompt_semantic_unit_creates": "no"}, 400, "invalid_prompt_semantic_unit_overlay"),
])
def test_refusals_before_the_stream_are_ordinary_json(
        route_module, monkeypatch, tmp_path, overrides, status, code):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))

    async def exercise(client):
        response = await client.post(URL, json=_body(harness.project, **overrides))
        return response.status, response.headers, await response.json()

    got, headers, payload = harness.run(exercise)
    assert got == status
    assert headers["Content-Type"].startswith("application/json")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert payload.get("code") == code
    assert harness.compiles == []


def test_an_unknown_scene_is_an_ordinary_404(route_module, monkeypatch, tmp_path):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))

    async def exercise(client):
        response = await client.post(
            "/sonder-editor/project/proj/scenes/missing/prompt-context/compile",
            json=_body(harness.project))
        return response.status, await response.json()

    assert harness.run(exercise) == (404, {"error": "Scene not found"})


def test_the_ordinary_response_is_unchanged_for_callers_that_do_not_opt_in(
        route_module, monkeypatch, tmp_path):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))
    body = _body(harness.project)
    del body["preview_response"], body["projections"]

    async def exercise(client):
        response = await client.post(URL, json=body)
        return response.status, response.headers["Content-Type"], await response.json()

    status, content_type, payload = harness.run(exercise)
    assert status == 200 and content_type.startswith("application/json")
    status, separate = route_module._compile_prompt_context_candidate_sync(
        harness.project, "scene", body)
    assert _normal(payload) == _normal(separate)


def test_a_disconnect_skips_the_scene_compile_it_has_not_started(
        route_module, monkeypatch, tmp_path):
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))
    harness.windowed_gate = _gate()

    async def exercise(client):
        response = await client.post(URL, json=_body(harness.project))
        assert await asyncio.to_thread(harness.windowed_gate["started"].wait, 5.0)
        response.close()                       # the browser aborted the request
        await asyncio.sleep(0.2)               # let the server see the closed socket
        harness.windowed_gate["release"].set()
        for _ in range(100):
            if any(event == "prompt_preview_stream_abandoned" for event, _ in harness.events):
                break
            await asyncio.sleep(0.02)

    harness.run(exercise)
    assert harness.compiles == ["windowed"]
    abandoned = [payload for event, payload in harness.events
                 if event == "prompt_preview_stream_abandoned"]
    assert abandoned == [{"project_id": "proj", "scene_id": "scene", "skipped": ["scene"]}]


def test_a_disconnect_before_the_stream_opens_compiles_nothing_and_raises_nothing(
        route_module, monkeypatch, tmp_path, caplog):
    """Audit #3: the browser leaving during load or preparation is not an error."""
    harness = _Harness(route_module, monkeypatch, _stream_project(tmp_path))
    harness.prepare_gate = _gate()

    async def exercise(client):
        pending = asyncio.ensure_future(client.post(URL, json=_body(harness.project)))
        assert await asyncio.to_thread(harness.prepare_gate["started"].wait, 5.0)
        pending.cancel()                        # the browser aborted before headers
        await asyncio.sleep(0.2)
        harness.prepare_gate["release"].set()
        for _ in range(100):
            if any(event == "prompt_preview_stream_abandoned" for event, _ in harness.events):
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)

    with caplog.at_level("DEBUG"):
        harness.run(exercise)
    assert harness.compiles == []
    abandoned = [payload for event, payload in harness.events
                 if event == "prompt_preview_stream_abandoned"]
    assert abandoned == [{"project_id": "proj", "scene_id": "scene",
                          "skipped": ["windowed", "scene"]}]
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


# ---------------------------------------------------------------------------
# 4. The reader, in the browser's own module
# ---------------------------------------------------------------------------

def _read(chunks_js: str, projections=("windowed", "scene"), content_type="application/x-ndjson"):
    return _node(f"""
const {{ readPromptPreviewRecords, isPromptPreviewStream }} = await import({json.dumps(STREAM_JS)});
const encoder = new TextEncoder();
const chunks = {chunks_js};
const body = new ReadableStream({{ start(controller) {{
  for (const chunk of chunks) controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
  controller.close(); }} }});
const response = new Response(body, {{ headers: {{ "Content-Type": {json.dumps(content_type)} }} }});
const records = [];
let error = null;
const stream = isPromptPreviewStream(response);
try {{
  await readPromptPreviewRecords(response, {json.dumps(list(projections))}, (record) => records.push(record));
}} catch (caught) {{ error = [caught.name, caught.message]; }}
console.log(JSON.stringify({{ stream, records, error }}));
""")


def test_records_split_anywhere_are_reassembled_including_inside_a_character():
    result = _read(r"""(() => {
  const text = JSON.stringify({ projection: "windowed", status: 200,
      payload: { prompt: "Café \u{1F37A} at dusk" } }) + "\n"
    + JSON.stringify({ projection: "scene", status: 200, payload: { a: 1 } }) + "\n";
  const bytes = encoder.encode(text);
  const cut = bytes.indexOf(0xF0) + 2;           // inside the four-byte emoji
  return [bytes.slice(0, 7), bytes.slice(7, cut), bytes.slice(cut, cut + 30),
          bytes.slice(cut + 30)];
})()""")
    assert result["stream"] is True
    assert result["error"] is None
    assert [record["projection"] for record in result["records"]] == ["windowed", "scene"]
    assert result["records"][0]["payload"]["prompt"] == "Café \U0001F37A at dusk"


@pytest.mark.parametrize("chunks, message", [
    ('["{\\"projection\\":\\"windowed\\",\\"status\\":200,\\"payload\\":{}}\\n", "not json\\n"]',
     "unreadable"),
    ('["{\\"projection\\":\\"prompt\\",\\"status\\":200,\\"payload\\":{}}\\n"]', "malformed"),
    ('["{\\"projection\\":\\"scene\\",\\"status\\":\\"200\\",\\"payload\\":{}}\\n"]', "malformed"),
    ('["{\\"projection\\":\\"scene\\",\\"status\\":200,\\"payload\\":null}\\n"]', "malformed"),
    ('["{\\"projection\\":\\"windowed\\",\\"status\\":200,\\"payload\\":{}}\\n{\\"proj"]',
     "mid-record"),
])
def test_a_malformed_or_truncated_stream_throws_after_delivering_what_preceded_it(chunks, message):
    result = _read(chunks)
    assert result["error"][0] == "PromptPreviewStreamError"
    assert message in result["error"][1]
    assert all(record["projection"] == "windowed" for record in result["records"])


def test_only_a_successful_ndjson_answer_is_a_stream():
    assert _read('[]', content_type="application/json; charset=utf-8")["stream"] is False
    assert _read('[]', content_type="application/x-ndjson; charset=utf-8")["stream"] is True
