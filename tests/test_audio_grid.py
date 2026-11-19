"""Audio grid, Phase 1: a Model Template's audio rate and crop block travel to the run.

The grid is declared by browser templates and reaches the backend as a resolved value,
like the frame rule: projected onto the project (`audio_constraint`), frozen into each
queued job, and resolved by the Editor. Phase 1 carries it end to end; nothing consumes
it yet. Plan: memory/plans/audio-grid-padding-2026-11.md.
"""

import asyncio
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import audio_grid  # noqa: E402
from server.timeline_state import GenerationJob, TimelineProject  # noqa: E402

LTX = {"rate": 44100, "block": 4096}
H3 = {"rate": 32000, "block": 800}


# ── The validator the backend uses ───────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (LTX, LTX),
    (H3, H3),
    ({"rate": 48000, "block": 1920, "newer_key": 1}, {"rate": 48000, "block": 1920}),  # a newer format still yields its grid
    ({"rate": 44100, "block": 44100}, {"rate": 44100, "block": 44100}),
    ({"rate": 44100, "block": 44101}, None),
    ({"rate": 44100, "block": 1}, None),
    ({"rate": 44100}, None),                    # a half pair is no grid
    ({"block": 4096}, None),
    ({"rate": 44100.0, "block": 4096}, None),   # exact ints only
    ({"rate": 44100, "block": 4096.0}, None),
    ({"rate": True, "block": 4096}, None),
    ({"rate": 44100, "block": True}, None),
    ({"rate": "44100", "block": 4096}, None),
    ({"rate": 44099, "block": 4096}, None),     # not a standard rate
    ({"rate": 10_000_000, "block": 4096}, None),
    ({"off": True}, None),
    (None, None),
    ([44100, 4096], None),
    ("44100/4096", None),
])
def test_valid_audio_grid(value, expected):
    assert audio_grid.valid_audio_grid(value) == expected


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    return node


def _run_js(body, stored_settings=None):
    script = """
const storedSettings = __SETTINGS__;
globalThis.window = {
    localStorage: {
        getItem(key) { return key === "sonder-editor-settings" && storedSettings ? JSON.stringify(storedSettings) : null; },
        setItem() {},
    },
    addEventListener() {},
};
globalThis.localStorage = window.localStorage;
const mod = await import(__MODULE_URL__);
""".replace("__SETTINGS__", json.dumps(stored_settings)).replace(
        "__MODULE_URL__", json.dumps((ROOT / "web" / "js" / "editor_settings.js").as_uri())) + body
    completed = subprocess.run([_node(), "--input-type=module", "-e", script],
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_js_and_python_share_one_rate_list():
    value = _run_js("console.log(JSON.stringify(mod.AUDIO_GRID_RATES));")
    assert tuple(value) == audio_grid.AUDIO_GRID_RATES


def test_js_normalize_matches_the_backend_validator():
    cases = [
        LTX, H3, {"rate": 44100, "block": 44101}, {"rate": 44100}, {"rate": 44100.5, "block": 4096},
        {"rate": True, "block": 4096}, {"rate": 44099, "block": 4096}, {"rate": 44100, "block": 1},
        None, [1, 2], "off",
    ]
    value = _run_js(f"""
const cases = {json.dumps(cases)};
console.log(JSON.stringify({{
    normalized: cases.map((c) => mod.normalizeAudioGrid(c)),
    absent: mod.normalizeAudioGrid(undefined) === undefined,
}}));""")
    assert value["absent"] is True
    for case, normalized in zip(cases, value["normalized"]):
        expected = audio_grid.valid_audio_grid(case)
        assert normalized == (expected if expected is not None else "off"), case


def test_builtins_declare_their_encoder_grid_and_others_none():
    value = _run_js("""
const settings = mod.getEditorSettings();
const ids = ["free", "ltx-2.3", "minimax-h3", "wan", "hunyuan-1.5"];
console.log(JSON.stringify(Object.fromEntries(ids.map((id) => [id, {
    project: mod.resolveAudioConstraintForTemplate(id, settings),
    job: mod.resolveAudioConstraintForJob(id, settings),
}]))));""")
    assert value["ltx-2.3"] == {"project": LTX, "job": LTX}
    assert value["minimax-h3"] == {"project": H3, "job": H3}
    for template_id in ("free", "wan", "hunyuan-1.5"):
        # The project stores none; a job freezes an explicit off so it never falls back.
        assert value[template_id] == {"project": None, "job": {"off": True}}


def test_overrides_inherit_unless_they_state_a_grid_and_normalize_is_idempotent():
    hard = {"dimensionStep": 32, "dimensionOffset": 0, "evenLatentDimensions": True,
            "frameStep": 8, "frameOffset": 1, "fps": [30]}
    stored = {"modelTemplates": {
        "builtinOverrides": {
            # Saved before the grid existed (or never touching it): inherits.
            "ltx-2.3": {"name": "LTX (mine)", "hard": dict(hard), "soft": {}},
            "minimax-h3": {"name": "H3 off", "hard": {**hard, "audioGrid": "off"}, "soft": {}},
            "wan": {"name": "Wan with a grid", "hard": {**hard, "audioGrid": {"rate": 16000, "block": 320}}, "soft": {}},
            "hunyuan-1.5": {"name": "Half pair", "hard": {**hard, "audioGrid": {"rate": 16000}}, "soft": {}},
        },
        "customTemplates": [
            {"id": "mine", "name": "Mine", "hard": {**hard, "audioGrid": {"rate": 48000, "block": 1920}}, "soft": {}},
            {"id": "plain", "name": "Plain", "hard": dict(hard), "soft": {}},
        ],
    }}
    value = _run_js("""
const settings = mod.getEditorSettings();
const again = mod.normalizeEditorSettings(JSON.parse(JSON.stringify(settings)));
const grids = (s) => Object.fromEntries(["ltx-2.3", "minimax-h3", "wan", "hunyuan-1.5", "mine", "plain"]
    .map((id) => [id, mod.resolveAudioConstraintForTemplate(id, s)]));
console.log(JSON.stringify({
    first: grids(settings),
    second: grids(again),
    storedLtx: again.modelTemplates.builtinOverrides["ltx-2.3"].hard.audioGrid === undefined,
    storedPlain: again.modelTemplates.customTemplates.find((t) => t.id === "plain").hard.audioGrid === undefined,
}));""", stored)
    expected = {"ltx-2.3": LTX, "minimax-h3": None, "wan": {"rate": 16000, "block": 320},
                "hunyuan-1.5": None, "mine": {"rate": 48000, "block": 1920}, "plain": None}
    assert value["first"] == expected
    assert value["second"] == expected
    # Absent stays absent through normalize, so inheritance survives every reload.
    assert value["storedLtx"] is True and value["storedPlain"] is True


def test_unknown_templates_are_never_resolved_to_none():
    value = _run_js("""
const settings = mod.getEditorSettings();
console.log(JSON.stringify({
    resolves: ["ltx-2.3", "free", "someone-elses-template", ""].map((id) => mod.templateIdResolves(id, settings)),
    job: mod.resolveAudioConstraintForJob("someone-elses-template", settings),
}));""")
    # "" normalizes to the free template, which this browser knows.
    assert value["resolves"] == [True, True, False, True]
    assert value["job"] is None  # the job defers to the project's stored grid


def test_audio_constraints_compare_by_effective_grid():
    value = _run_js("""
const eq = mod.audioConstraintsEqual;
console.log(JSON.stringify([
    eq({rate: 44100, block: 4096}, {rate: 44100, block: 4096}),
    eq({rate: 44100, block: 4096}, {rate: 44100, block: 4096, newer: 1}),
    eq({rate: 44100, block: 4096}, {rate: 32000, block: 800}),
    eq(null, undefined),
    eq(null, {off: true}),
    eq(null, {rate: 44100}),
    eq({rate: 44100, block: 4096}, null),
]));""")
    assert value == [True, True, False, True, True, True, False]


# ── Browser projection: create, switch, enqueue, heals ──────────────────────────

def _source(name):
    return (ROOT / "web" / "js" / name).read_text(encoding="utf-8")


def test_switch_and_enqueue_carry_the_grid():
    widget = _source("editor_widget.js")
    switch = widget.split("async _updateProjectTemplateIdWithinGesture", 1)[1].split("async _handleTemplateSelectionChange(", 1)[0]
    assert "resolveAudioConstraintForTemplate(templateId, this._settings)" in switch
    assert "audio_constraint: audioConstraint," in switch
    assert "audio_constraint_template_id" not in switch  # a switch sets template and grid together
    assert ("audio_constraint: resolveAudioConstraintForJob(this._projectTemplateIdRaw ?? this._templateId, this._settings)"
            in widget)
    assert "this._projectTemplateIdRaw = data.template_id ?? null;" in widget
    # Every successful template save moves the raw id (a pick, a deleted custom template, a settings change).
    assert switch.index("this._projectTemplateIdRaw = templateId;") > switch.index("_runVersionedProjectMutation")


def test_mounted_heal_skips_unknown_templates_and_names_its_template():
    widget = _source("editor_widget.js")
    assert "await this._maybeHealAudioConstraint(this.projectDir, dirName, data.template_id, data.audio_constraint);" in widget
    heal = widget.split("async _maybeHealAudioConstraint", 1)[1].split("_ensureViewportSurface", 1)[0]
    assert heal.index("templateIdResolves(templateId, this._settings)") < heal.index("_runVersionedProjectMutation")
    assert "audioConstraintsEqual(expected, persistedConstraint)" in heal
    assert "audio_constraint_template_id: templateId ?? null" in heal
    assert "_hasPendingProjectMutations()" in heal


def test_dormant_heal_reuses_the_frame_heal_read_and_skips_unknown_templates():
    controller = _source("editor_node_controller.js")
    heal = controller.split("async function healProjectAudioConstraint", 1)[1].split("\nasync function ", 1)[0]
    assert heal.index("templateIdResolves(templateId, settings)") < heal.index('method: "PUT"')
    assert "audio_constraint_template_id: templateId ?? null" in heal
    assert "healedProject = await healProjectFrameConstraint(projectDir);" in controller
    assert "await healProjectAudioConstraint(projectDir, healedProject);" in controller
    assert controller.count('this._audioConstraintHealedFor = "";') == 3


def test_settings_form_keeps_the_stored_grid_raw():
    panel = _source("editor_settings_panel.js")
    save = panel.split("const storedAudioGrid = ", 1)[1].split("this._templateFormState = ", 1)[0]
    # A built-in's form save carries its override's own value (absent = inherit), never the effective one.
    assert "builtinOverrides?.[editingTemplate.id]?.hard" in save
    assert "...(storedAudioGrid !== undefined ? { audioGrid: storedAudioGrid } : {})" in save


# ── Backend: stored raw, routes, frozen jobs ────────────────────────────────────

def test_project_and_job_round_trip_raw_values():
    newer = {"rate": 44100, "block": 4096, "newer_key": [1, 2]}
    project = TimelineProject.from_dict({"project_id": "p", "audio_constraint": newer})
    assert project.audio_constraint == newer and project.to_dict()["audio_constraint"] == newer
    assert TimelineProject.from_dict({"project_id": "p"}).audio_constraint is None
    for value in (LTX, {"off": True}, newer):
        job = GenerationJob.from_dict({"audio_constraint": value})
        assert GenerationJob.from_dict(job.to_dict()).audio_constraint == value
    assert GenerationJob.from_dict({}).audio_constraint is None


def test_marker_absent_jobs_refuse_a_grid_and_keep_the_v022_allowlist():
    from server import frozen_prompt
    frozen_prompt.validate_marker_absent_v022(GenerationJob())
    with pytest.raises(frozen_prompt.FrozenPromptEnvelopeError, match="audio_constraint"):
        frozen_prompt.validate_marker_absent_v022(GenerationJob(audio_constraint=LTX))
    assert "audio_constraint" not in frozen_prompt.V022_QUEUE_BODY_FIELDS
    with pytest.raises(frozen_prompt.FrozenPromptEnvelopeError, match="audio_constraint"):
        frozen_prompt.validate_marker_absent_queue_body({"scene_id": "s", "audio_constraint": LTX}, {})


class _Request:
    def __init__(self, body, project_id="p"):
        self.match_info = {"project_id": project_id}
        self.query = {}
        self._body = body
        self.content_length = len(json.dumps(body).encode("utf-8"))
        self.headers = {}

    async def json(self):
        return self._body


def _routes(monkeypatch, project):
    import server
    import server.routes as routes
    from aiohttp import web
    monkeypatch.setattr(server, "PromptServer",
                        SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef())), raising=False)
    module = importlib.reload(routes)
    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project, **kwargs: None)
    return module


def _put(module, body):
    response = asyncio.run(module.api_update_project(_Request(body)))
    assert response.status == 200, response.body
    return json.loads(response.body.decode("utf-8"))


def test_update_applies_a_heal_only_while_the_template_matches(tmp_path, monkeypatch):
    project = TimelineProject(project_id="p", project_dir=str(tmp_path), template_id="ltx-2.3")
    module = _routes(monkeypatch, project)

    _put(module, {"audio_constraint": LTX, "audio_constraint_template_id": "ltx-2.3"})
    assert project.audio_constraint == LTX
    # A heal computed for another template (a retry after a switch, another tab) is ignored.
    _put(module, {"audio_constraint": H3, "audio_constraint_template_id": "minimax-h3"})
    assert project.audio_constraint == LTX
    # A template switch sets the template and its grid together, without a guard.
    payload = _put(module, {"template_id": "minimax-h3", "audio_constraint": H3})
    assert project.template_id == "minimax-h3" and project.audio_constraint == H3
    assert payload["audio_constraint"] == H3
    _put(module, {"template_id": "wan", "audio_constraint": None})
    assert project.audio_constraint is None
    # An unset template is "free" on both sides of the guard.
    project.template_id = ""
    _put(module, {"audio_constraint": LTX, "audio_constraint_template_id": None})
    assert project.audio_constraint == LTX
    # A PUT that does not mention the grid leaves it alone.
    _put(module, {"name": "Renamed"})
    assert project.audio_constraint == LTX


def test_queue_route_freezes_the_job_grid(tmp_path, monkeypatch):
    from server.timeline_state import Scene
    project = TimelineProject(project_id="p", project_dir=str(tmp_path), template_id="ltx-2.3")
    project.scenes = [Scene(scene_id="scene-1", name="S", duration_frames=96)]
    module = _routes(monkeypatch, project)
    base = {"scene_id": "scene-1", "selection_start": 0, "selection_end": 48, "template_id": "ltx-2.3",
            "params": {"prompt_context_format": "prompt_context_v1"}}
    for value in (LTX, {"off": True}, None):
        response = asyncio.run(module.api_add_queue_job(_Request({**base, "audio_constraint": value})))
        assert response.status == 200, response.body
        assert project.generation_queue[-1].audio_constraint == value
    response = asyncio.run(module.api_add_queue_job(_Request({**base, "audio_constraint": "garbage"})))
    assert response.status == 200 and project.generation_queue[-1].audio_constraint is None


# ── The Editor's resolver ───────────────────────────────────────────────────────

def _editor_node(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    package = "sonder_audio_grid_test_pkg"
    monkeypatch.setitem(sys.modules, "folder_paths",
                        types.SimpleNamespace(get_output_directory=lambda: str(tmp_path)))
    if package not in sys.modules:
        pkg = types.ModuleType(package)
        pkg.__path__ = [str(ROOT)]
        sys.modules[package] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{package}.nodes.editor_node")


@pytest.mark.parametrize("job_value,project_value,expected", [
    (LTX, H3, LTX),                       # the job's own grid wins
    ({"off": True}, LTX, None),           # a job made for an off template stays off
    (None, LTX, LTX),                     # a job without one uses the project's
    ({}, H3, H3),
    ({"rate": 44100}, LTX, None),         # an invalid job value is no grid, not a fallback
    (None, {"rate": 44100.0, "block": 4096}, None),
    (None, None, None),
])
def test_execution_audio_constraint_precedence(tmp_path, monkeypatch, job_value, project_value, expected):
    editor_node = _editor_node(tmp_path, monkeypatch)
    resolve = editor_node.SonderEditor._execution_audio_constraint
    project = SimpleNamespace(audio_constraint=project_value)
    job = SimpleNamespace(audio_constraint=job_value)
    assert resolve(project, job) == expected
    assert resolve(project, None) == audio_grid.valid_audio_grid(project_value)


def test_create_projects_the_grid_only_onto_a_project_it_created(tmp_path, monkeypatch):
    module = _routes(monkeypatch, None)
    monkeypatch.setattr(module, "save_project", importlib.import_module("server.project_manager").save_project)
    monkeypatch.setattr(module, "_configured_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(tmp_path), raising=False)

    def create(audio_constraint):
        body = {"name": "Grid", "template_id": "ltx-2.3", "audio_constraint": audio_constraint}
        response = asyncio.run(module.api_create_project(_Request(body, project_id="")))
        assert response.status == 201, response.body
        return json.loads(response.body.decode("utf-8"))

    assert create(LTX)["audio_constraint"] == LTX
    # "Create" of an existing name opens it: the stored grid is kept, not replaced.
    assert create(H3)["audio_constraint"] == LTX


# ── Behaviour of the heals and the settings, with a mocked transport ────────────

_HEAL_BEHAVIOUR = r"""
import { readFileSync } from "node:fs";
let stored = null;
globalThis.window = {
    localStorage: { getItem: (k) => (k === "sonder-editor-settings" && stored ? JSON.stringify(stored) : null), setItem() {} },
    addEventListener() {},
};
globalThis.localStorage = window.localStorage;
const mod = await import(__SETTINGS_URL__);
const out = {};

// Dormant: the two module functions, run against a mocked fetch.
const ctl = readFileSync(__CONTROLLER__, "utf8");
const src = ctl.slice(ctl.indexOf("async function fetchProjectForHeal"), ctl.indexOf("\nasync function fetchJson"));
const calls = [];
let projectState;
const factory = (fetchImpl) => new Function("fetch", "buildProjectUrl", "getEditorSettings", "resolveFrameConstraintForTemplate",
    "frameConstraintsEqual", "templateIdResolves", "resolveAudioConstraintForTemplate", "audioConstraintsEqual",
    `${src}; return { healProjectFrameConstraint, healProjectAudioConstraint };`)(fetchImpl, (d) => `/p/${d}`,
    mod.getEditorSettings, mod.resolveFrameConstraintForTemplate, mod.frameConstraintsEqual, mod.templateIdResolves,
    mod.resolveAudioConstraintForTemplate, mod.audioConstraintsEqual);
const dormant = factory(async (url, init = {}) => {
    calls.push([init.method || "GET", init.body ? JSON.parse(init.body) : null]);
    if ((init.method || "GET") === "GET") return { ok: true, json: async () => structuredClone(projectState) };
    return { ok: true, status: 200, json: async () => ({}) };
});
async function openDormant(state) {
    projectState = state; calls.length = 0;
    await dormant.healProjectAudioConstraint("x", await dormant.healProjectFrameConstraint("x"));
    return structuredClone(calls);
}
out.clean = await openDormant({ template_id: "minimax-h3", frame_constraint: { step: 17, offset: 5 }, audio_constraint: { rate: 32000, block: 800 } });
out.stale = await openDormant({ template_id: "minimax-h3", frame_constraint: { step: 17, offset: 5 } });
out.unknown = await openDormant({ template_id: "someone-else", audio_constraint: { rate: 48000, block: 1920 } });
out.offStale = await openDormant({ template_id: "wan", frame_constraint: { step: 4, offset: 1 }, audio_constraint: { rate: 44100, block: 4096 } });
const conflict = factory(async (u, init = {}) => (init.method === "PUT"
    ? { ok: false, status: 409 } : { ok: true, json: async () => ({ template_id: "ltx-2.3" }) }));
try { await conflict.healProjectAudioConstraint("x", null); out.conflict = "no throw"; } catch (e) { out.conflict = String(e.message); }

// Mounted: the method, on a host that records writes and deferrals.
const w = readFileSync(__WIDGET__, "utf8");
const start = w.indexOf("    async _maybeHealAudioConstraint(");
const heal = new Function("templateIdResolves", "resolveAudioConstraintForTemplate", "audioConstraintsEqual",
    `return ({${w.slice(start, w.indexOf("\n    _ensureViewportSurface(", start))}})._maybeHealAudioConstraint;`)(
    mod.templateIdResolves, mod.resolveAudioConstraintForTemplate, mod.audioConstraintsEqual);
const host = {
    _settings: mod.getEditorSettings(), _audioConstraintHealedFor: "", writes: [], deferred: 0, pending: true,
    _hasPendingProjectMutations() { return this.pending; },
    _deferProjectBackedRefresh() { this.deferred += 1; },
    async _runVersionedProjectMutation(path, init) { this.writes.push(JSON.parse(init.body)); },
};
await heal.call(host, "/d/x", "x", "ltx-2.3", null);
out.mountedDeferred = { writes: host.writes.length, deferred: host.deferred, healed: host._audioConstraintHealedFor };
host.pending = false;
await heal.call(host, "/d/x", "x", "ltx-2.3", null);
await heal.call(host, "/d/x", "x", "ltx-2.3", null);
out.mountedWrites = host.writes;
const equal = { ...host, writes: [], _audioConstraintHealedFor: "" };
await heal.call(equal, "/d/y", "y", "ltx-2.3", { rate: 44100, block: 4096 });
out.mountedEqualWrites = equal.writes.length;
const unknown = { ...host, writes: [], _audioConstraintHealedFor: "" };
await heal.call(unknown, "/d/z", "z", "someone-else", { rate: 48000, block: 1920 });
out.mountedUnknownWrites = unknown.writes.length;

// Settings: reset brings the built-in back; an inheriting override follows a changed built-in default.
stored = { modelTemplates: { builtinOverrides: { "minimax-h3": { name: "H3", hard: { frameStep: 17, frameOffset: 5, fps: [24], audioGrid: "off" }, soft: {} } } } };
const mod2 = await import(__SETTINGS_URL__ + "?second");
let s = mod2.getEditorSettings();
out.overrideOff = mod2.resolveAudioConstraintForTemplate("minimax-h3", s);
s = mod2.updateEditorSettings({ modelTemplates: { builtinOverrides: { "minimax-h3": null } } });
out.afterReset = mod2.resolveAudioConstraintForTemplate("minimax-h3", s);
s = mod2.updateEditorSettings({ modelTemplates: { builtinOverrides: { "ltx-2.3": { name: "LTX", hard: { frameStep: 8, frameOffset: 1, fps: [24] }, soft: {} } } } });
mod2.BUILTIN_MODEL_TEMPLATES.find((t) => t.id === "ltx-2.3").hard.audioGrid = { rate: 48000, block: 1920 };
out.inheritsChangedDefault = mod2.resolveAudioConstraintForTemplate("ltx-2.3", s);
console.log(JSON.stringify(out));
"""


def test_heal_and_settings_behaviour_with_a_mocked_transport():
    script = (_HEAL_BEHAVIOUR
              .replace("__SETTINGS_URL__", json.dumps((ROOT / "web" / "js" / "editor_settings.js").as_uri()))
              .replace("__CONTROLLER__", json.dumps(str(ROOT / "web" / "js" / "editor_node_controller.js")))
              .replace("__WIDGET__", json.dumps(str(ROOT / "web" / "js" / "editor_widget.js"))))
    completed = subprocess.run([_node(), "--input-type=module", "-e", script],
                               capture_output=True, text=True, check=True)
    out = json.loads(completed.stdout)
    guarded = {"audio_constraint": H3, "audio_constraint_template_id": "minimax-h3"}
    # Dormant: one shared read; a write only when the stored grid differs, naming its template.
    assert out["clean"] == [["GET", None]]
    assert out["stale"] == [["GET", None], ["PUT", guarded]]
    assert out["unknown"] == [["GET", None]]  # never writes from a template it does not know
    assert out["offStale"] == [["GET", None], ["PUT", {"audio_constraint": None, "audio_constraint_template_id": "wan"}]]
    assert "409" in out["conflict"]  # thrown, so the open retries next time
    # Mounted: a deferral writes nothing and stays unhealed; the next pass writes once, then stops.
    assert out["mountedDeferred"] == {"writes": 0, "deferred": 1, "healed": ""}
    assert out["mountedWrites"] == [{"audio_constraint": LTX, "audio_constraint_template_id": "ltx-2.3"}]
    assert out["mountedEqualWrites"] == 0 and out["mountedUnknownWrites"] == 0
    # Settings.
    assert out["overrideOff"] is None
    assert out["afterReset"] == H3
    assert out["inheritsChangedDefault"] == {"rate": 48000, "block": 1920}


def test_an_ignored_heal_is_not_saved(tmp_path, monkeypatch):
    project = TimelineProject(project_id="p", project_dir=str(tmp_path), template_id="minimax-h3", audio_constraint=H3)
    module = _routes(monkeypatch, project)
    saves = []
    monkeypatch.setattr(module, "save_project", lambda project, **kwargs: saves.append(1))
    _put(module, {"audio_constraint": LTX, "audio_constraint_template_id": "ltx-2.3"})
    assert project.audio_constraint == H3 and saves == []
    # Any other field in the same PUT still saves, without the ignored grid.
    _put(module, {"name": "Renamed", "audio_constraint": LTX, "audio_constraint_template_id": "ltx-2.3"})
    assert project.audio_constraint == H3 and project.name == "Renamed" and saves == [1]


def test_the_resolver_names_an_unusable_stored_grid(tmp_path, monkeypatch, caplog):
    editor_node = _editor_node(tmp_path, monkeypatch)
    resolve = editor_node.SonderEditor._execution_audio_constraint
    with caplog.at_level("WARNING", logger="sonder_editor"):
        assert resolve(SimpleNamespace(audio_constraint={"rate": 44100}), None) is None
        assert resolve(SimpleNamespace(audio_constraint=None), SimpleNamespace(audio_constraint={"off": True})) is None
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and "project's audio grid" in messages[0]
