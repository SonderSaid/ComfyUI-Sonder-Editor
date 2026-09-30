"""The Reference Lane Setup panel's staged-item writes, on the real host paths.

Harness contract (plan: Paint First in Reference Lane Setup, "Tests"):

* The widget is a real ``EditorWidget``. ``_runSceneMutation``, the project
  mutation queue, the undo stack, ``_reconcileActiveSceneFromMutation``,
  ``_renderSceneAfterLocalMutation`` and the management-panel refresh gate are
  the shipped ones, and ``_referenceMemberForRef`` / ``_findAssetById`` read a
  fixture Library and asset list.
* ``_runVersionedProjectMutation``, ``_fetchScenes`` and ``_setActiveScene``
  are the shipped ones too, so a refusal reaches the gesture in the client's
  real error shape (the route's code on ``error.payload.code``; a dead network
  is a ``TypeError`` with no status), a scenes GET is gated and sequenced as it
  is live, and a scene replacement bumps what it bumps. Only ``fetch`` itself,
  the DOM, and the timeline/viewport painters and widget plumbing
  ``_setActiveScene`` calls are stood in for, each named where it is.
* **There is no route copy in JavaScript.** ``fetch`` forwards every mutation
  batch to a Python child process that applies it with the real
  ``routes._apply_scene_mutation_batch`` to the fixture project, round-trips
  the committed project through ``to_dict``/``from_dict`` as a save and reload
  would, and answers with the canonical payload or the route's error. Scene
  GETs read the same project. A test may instead script the next answer as a
  refusal (a code) or a dead network, and may hold a write until it releases
  it.

Route fidelity of the optimistic mirror itself lives in
``test_reference_geometry_parity.py``; this file tests what the host does with
it.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server.timeline_state import (
    Asset,
    LaneConfig,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    Scene,
    TimelineProject,
)


ROOT = Path(__file__).resolve().parents[1]

# The route, in a child process. One request per line in, one answer per line
# out, prefixed so an import-time print cannot be mistaken for an answer. A
# refused batch leaves the project as it was, as the route's reload does.
_ROUTE_CHILD = r"""
import copy, json, sys
sys.path.insert(0, sys.argv[1])
from server import routes
from server.timeline_state import TimelineProject
with open(sys.argv[2], encoding="utf-8") as handle:
    project = TimelineProject.from_dict(json.load(handle))
print("@@ready", flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get("read"):
        scene = project.get_scene(request["sceneId"])
        print("@@" + json.dumps({"ok": True, "scene": scene.to_dict()}), flush=True)
        continue
    candidate = copy.deepcopy(project)
    try:
        _committed, payload = routes._apply_scene_mutation_batch(
            candidate, request["sceneId"], request["operations"])
    except routes.ProjectMutationRequestError as exc:
        print("@@" + json.dumps({"ok": False, "status": exc.status, "code": exc.code,
                                  "message": exc.message}), flush=True)
        continue
    except Exception as exc:  # the route would answer 500; keep the child alive
        print("@@" + json.dumps({"ok": False, "status": 500, "code": "server_error",
                                  "message": repr(exc)}), flush=True)
        continue
    # A save and the next load, so what a later batch sees is what disk holds.
    project = TimelineProject.from_dict(json.loads(json.dumps(candidate.to_dict(), default=str)))
    print("@@" + json.dumps({"ok": True, "payload": payload}, default=str), flush=True)
"""


def fixture_project():
    """Two Reference lanes: an image lane holding two items, an audio lane one."""
    assets = [
        Asset(asset_id="asset-a", name="a.png", asset_type="image", path="media/a.png"),
        Asset(asset_id="asset-b", name="b.png", asset_type="image", path="media/b.png"),
        Asset(asset_id="asset-c", name="c.png", asset_type="image", path="media/c.png"),
        Asset(asset_id="asset-s", name="s.wav", asset_type="audio", path="media/s.wav"),
        Asset(asset_id="asset-va", name="va.mp4", asset_type="video", path="media/va.mp4",
              has_audio=True),
    ]
    subject = ReferenceEntity(reference_id="entity-1", name="Subject", members=[
        ReferenceMember(member_id=f"member-{key}", asset_id=f"asset-{key}", name=key.upper())
        for key in ("a", "b", "c", "s", "va")])

    def member(key, **extra):
        return {"entity_id": "entity-1", "member_id": f"member-{key}", **extra}

    scene = Scene(
        scene_id="scene", duration_frames=100, reference_lane_count=2,
        reference_lane_configs=[LaneConfig(), LaneConfig()],
        reference_lane_recipes=[
            ReferenceLaneRecipe(lane_id="lane-image", media_kind="image", recipe={
                "hard": {"assembly": "batch"},
                "soft": {"physical_population": "pictures"}}),
            ReferenceLaneRecipe(lane_id="lane-audio", media_kind="audio", recipe={
                "hard": {"assembly": "audio"}, "soft": {}}),
        ],
        reference_items=[ReferenceItem.from_dict(row) for row in (
            {"reference_item_id": "item-1", "lane_index": 0, "start_frame": 10,
             "end_frame": 40, "members": [member("a"), member("b")]},
            {"reference_item_id": "item-2", "lane_index": 0, "start_frame": 50,
             "end_frame": 70, "members": [member("c")]},
            {"reference_item_id": "item-4", "lane_index": 1, "start_frame": 0,
             "end_frame": 30, "members": [member("s")]},
        )],
    )
    return TimelineProject(project_id="project", assets=assets,
                           references=[subject], scenes=[scene])


_HARNESS = r"""
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

// -- the route child ----------------------------------------------------------
const child = spawn(__PYTHON__, ['-c', __CHILD__, __ROOT__, __PROJECT_FILE__],
  { cwd: __ROOT__, stdio: ['pipe', 'pipe', 'inherit'],
    env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' } });
const answers = [];
const waiting = [];
createInterface({ input: child.stdout }).on('line', (line) => {
  if (!line.startsWith('@@')) return;
  const answer = line === '@@ready' ? { ready: true } : JSON.parse(line.slice(2));
  const next = waiting.shift();
  if (next) next(answer); else answers.push(answer);
});
const nextAnswer = () => new Promise((resolve) => {
  if (answers.length) resolve(answers.shift()); else waiting.push(resolve);
});
const ask = async (request) => { child.stdin.write(JSON.stringify(request) + '\n'); return nextAnswer(); };
assert.ok((await nextAnswer()).ready, 'route child did not start');

// -- a minimal DOM --------------------------------------------------------------
const docListeners = {};
class N {
  constructor(tag) { this.tagName=String(tag).toUpperCase(); this.children=[];
    this.options=[]; this.style={cssText:""}; this.dataset={}; this.attributes={};
    this.value=""; this._text=""; this.title=""; this.disabled=false; this.readOnly=false;
    this.checked=false; this.open=false; this.type=""; this._handlers={}; this.parentElement=null; }
  get selectedOptions() { return this.options.filter((o) => o.value === this.value); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v ?? ""); for (const c of this.children) c.parentElement = null;
    this.children = []; this.options = []; }
  get isConnected() { let n = this; while (n.parentElement) n = n.parentElement; return n === document.body; }
  appendChild(c) { if(!c?.tagName) return c; c.parentElement?.children && c.remove();
    this.children.push(c); c.parentElement=this; if(c.tagName==="OPTION") this.options.push(c); return c; }
  append(...cs) { cs.forEach((c)=>this.appendChild(c)); }
  insertBefore(c, ref) { this.appendChild(c); this.children.pop();
    const at = this.children.indexOf(ref); this.children.splice(at < 0 ? this.children.length : at, 0, c); return c; }
  addEventListener(t,h) { (this._handlers[t] ||= []).push(h); }
  removeEventListener() {}
  dispatch(t, extra = {}) { (this._handlers[t] || []).forEach((h) => h({ target: this,
    preventDefault() {}, stopPropagation() {}, ...extra })); }
  setAttribute(k,v) { this.attributes[k]=String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  querySelectorAll(sel) { const tags=String(sel).split(",").map((v)=>v.trim().toUpperCase());
    const out=[]; const walk=(n)=>n.children.forEach((c)=>{ if(tags.includes(c.tagName)) out.push(c); walk(c); });
    walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  contains(node) { for (let n = node; n; n = n.parentElement) if (n === this) return true; return false; }
  getClientRects() { return this.isConnected ? [{}] : []; }
  focus() { document.activeElement = this; fireDoc('focusin', this); }
  blur() { if (document.activeElement === this) document.activeElement = document.body; fireDoc('focusout', this); }
  remove() { if(this.parentElement) { this.parentElement.children =
    this.parentElement.children.filter((c)=>c!==this); this.parentElement = null; } }
}
const fireDoc = (type, target) => (docListeners[type] || []).forEach((h) => h({ target, button: 0 }));
globalThis.document = { createElement:(t)=>new N(t), body:new N("body"), activeElement:null,
  addEventListener:(t,h)=>{ (docListeners[t] ||= []).push(h); },
  removeEventListener:(t,h)=>{ docListeners[t] = (docListeners[t] || []).filter((x) => x !== h); },
  querySelectorAll:()=>[] };
document.activeElement = document.body;
globalThis.window = { addEventListener(){}, removeEventListener(){}, localStorage:null,
  comfyAPI:{api:{api:{apiURL:(path)=>path}}}, SONDER_DEBUG_SESSION:true, prompt:()=>null };
globalThis.localStorage = { getItem(){return null;}, setItem(){} };
globalThis.location = { href:'http://test/' };
// Frames run only when a test flushes them, so "repaints on the next frame"
// is observable. The gated refresh's 50 ms fallback timer is real.
const frames = [];
globalThis.requestAnimationFrame = (cb) => { frames.push(cb); return frames.length; };
// One frame: the callbacks queued before it. A callback that schedules the next
// frame (the session-diagnostics gap monitor does) waits for the next flush.
const flushFrames = () => { const now = performance.now(); for (const cb of frames.splice(0)) cb(now); };

// -- the widget -----------------------------------------------------------------
const { EditorWidget } = await import(__WIDGET__);
const { ProjectMutationQueue } = await import(__QUEUE__);
const panelModule = await import(__PANEL__);
// Every toast the page raises, as {tier, message, detail, source}.
const notes = await import(__NOTES__);
const toasts = [];
const seenToasts = new Set();
notes.subscribe((list) => { for (const n of list) {
  if (seenToasts.has(n.id)) continue; seenToasts.add(n.id);
  toasts.push({ tier: n.tier, message: n.message, detail: n.detail || null, source: n.source || '' }); } });
const fixture = __FIXTURE__;
const w = Object.create(EditorWidget.prototype);
const sent = [];
const gets = [];
const scripted = [];
const held = [];
let holdNext = 0;
const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 6) => { for (let i = 0; i < n; i += 1) await tick(); };
Object.assign(w, {
  projectDir:'project', projectId:'project', activeSceneId:'scene',
  activeScene: null, scenes: structuredClone(fixture.scenes),
  assets: fixture.assets, _references: fixture.references, _referencesLoaded: true,
  _referenceRecipePresets:[], _customReferenceRecipes:[], _referenceRecipeFieldSchema:[],
  _promptContextProfiles:[], _promptContextCatalog:{},
  _activeMutationGesture:null, _timelineMutationDepth:0, _sceneMutationInvalidationSeq:0,
  _queueFetchSeq:0, isDragging:false,
  _projectMutationQueue: new ProjectMutationQueue({ onIdle: () => w._replayDeferredProjectBackedRefresh() }),
  _undoStack:[], _redoStack:[], _maxUndoSteps:50, _historyStackRevision:0,
  totalFrames: 100, playhead: 0,
  _trimUndoStack(){}, _clearRedoForNewEdit(){}, _replayDeferredHistoryWidgetStateIfIdle(){},
  _renderTimeline(){}, _renderViewportFrame(){}, _clearPlaybackWarmOverlay(){}, _reconcileSelection(){},
  _refreshPromptContextDependencyConsumers(){},
  _defaultReferenceLaneRecipe:()=>({lane_id:'', recipe_id:'', media_kind:'image', recipe:{}}),
  _referenceAssetPreviewUrl:()=>null, _referenceLaneAdvisories:()=>[], _channelTemplate:()=>({}),
  _openReferenceMediaEditor(){},
  // Stand-in: the layout the real builder derives for Reference lanes.
  _buildTrackLayout() {
    const configs = w.activeScene?.reference_lane_configs || [];
    w._trackLayout = configs.map((config, laneIndex) => ({ type:'reference', laneIndex,
      customName: config.name || '', color: config.color || '', locked: !!config.locked,
      hidden: !!config.hidden }));
  },
  // Stand-ins: the DOM painters and widget plumbing the REAL `_setActiveScene`
  // and `_fetchScenes` call. Their logic -- gates, sequencing, replacement, the
  // authoritative-scene bump, the panel refresh -- is the shipped code.
  selectedItems: [], _sceneFetchSeq: 0, _sceneHistoryLifecycleOwner: null,
  _persistActiveTimelineSelection(){}, _readStoredTimelineSelection(){ return null; },
  _setWidgetValue(){}, _getWidgetValue(_name, fallback){ return fallback; },
  _refreshDurationInput(){}, _updateSceneIdentity(){}, _syncSceneResolutionControls(){},
  _syncSceneFpsControl(){}, _updateViewportHeader(){}, _resizeViewportCanvas(){}, _updateToolbar(){},
  _promptContextConsumersMounted(){ return false; }, _stopPlayback(){}, _clearSelection(){},
  _hideItemEditor(){}, _hidePromptEditor(){}, _fitToView(){},
  _clearProjectNotFound(){}, _showProjectNotFound(){}, _markStaleReplayApplied(){},
  _governStaleVersionReplay(){ return true; },
});
w.activeScene = w.scenes[0];
w._buildTrackLayout();
// Stand-in: `fetch`. Mutation POSTs go to the route child unless the test
// scripted the answer; scene GETs read the child's project. Everything above
// it -- `postProjectJsonWithReconcile`, the version map, the error shape -- is
// the shipped client.
const sceneReads = [];
const json = (status, body) => new Response(JSON.stringify(body), { status,
  headers: { 'Content-Type': 'application/json' } });
globalThis.fetch = async (url, init = {}) => {
  const path = String(url).split('?')[0];
  const method = String(init.method || 'GET').toUpperCase();
  const mutation = /\/scenes\/([^/]+)\/mutations$/.exec(path);
  if (method === 'POST' && mutation) {
    const operations = JSON.parse(init.body).operations;
    sent.push(structuredClone(operations));
    if (holdNext > 0) { holdNext -= 1; await new Promise((resolve, reject) => held.push({resolve, reject})); }
    const script = scripted[0]?.kind === 'offline-get' ? null : scripted.shift();
    if (script?.kind === 'offline') throw new TypeError('Failed to fetch');
    if (script?.kind === 'lost') {
      // The route commits; the answer never reaches the client.
      await ask({ sceneId: decodeURIComponent(mutation[1]), operations });
      throw new TypeError('Failed to fetch');
    }
    if (script?.kind === 'refuse') return json(script.status, { error: script.code, code: script.code });
    const answer = await ask({ sceneId: decodeURIComponent(mutation[1]), operations });
    return answer.ok ? json(200, answer.payload)
      : json(answer.status, { error: answer.message, code: answer.code });
  }
  if (method === 'GET' && /\/scenes$/.test(path)) {
    gets.push(path);
    if (scripted[0]?.kind === 'offline-get') { scripted.shift(); throw new TypeError('Failed to fetch'); }
    const scenes = [];
    for (const id of fixture.sceneIds) scenes.push((await ask({ read: true, sceneId: id })).scene);
    return json(200, { scenes });
  }
  const single = /\/scenes\/([^/]+)$/.exec(path);
  assert.ok(method === 'GET' && single, `unexpected fetch ${method} ${path}`);
  sceneReads.push(single[1]);
  return json(200, (await ask({ read: true, sceneId: decodeURIComponent(single[1]) })).scene);
};
const hold = (count = 1) => { holdNext += count; };
const refuseNext = (code, status = 409) => scripted.push({ kind: 'refuse', code, status });
const offlineNext = () => scripted.push({ kind: 'offline' });
const offlineGetNext = () => scripted.push({ kind: 'offline-get' });
const lostNext = () => scripted.push({ kind: 'lost' });
const nextHeld = async () => { for (let i = 0; i < 40 && !held.length; i += 1) await tick();
  assert.ok(held.length, 'no write is held'); return held.shift(); };
const release = async () => { (await nextHeld()).resolve(); await settle(); };
const serverScene = async () => (await ask({ read: true, sceneId: 'scene' })).scene;
const item = (id, scene = w.activeScene) => (scene.reference_items || []).find((row) => row.reference_item_id === id);
const walk = (n, out=[]) => { out.push(n); n.children.forEach((c) => walk(c, out)); return out; };
let handle = null;
const mount = (laneIndex = 0) => { handle = panelModule.mountReferenceLanePanel(w, { laneIndex }); return handle; };
const nodes = () => walk(handle.element);
const cards = () => nodes().filter((n) => n.tagName === 'BUTTON' && n.textContent === 'Delete item')
  .map((b) => b.parentElement.parentElement);
const cardAt = (start) => cards().find((c) => c.querySelectorAll('input')[0]?.value === String(start));
const inputsOf = (card) => card.querySelectorAll('input');
const diag = (kind) => (window.__SONDER_CANVAS_DIAG?.events || []).filter((e) => e.kind === kind);
let result;
try {
  result = await (async () => {
__BODY__
  })();
} finally {
  child.stdin.end();
}
console.log(JSON.stringify(result ?? null));
"""


def run_item_panel(body, tmp_path, project=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Reference panel item harness")
    project = project or fixture_project()
    project_file = tmp_path / "project.json"
    project_file.write_text(json.dumps(project.to_dict()), encoding="utf-8")
    scenes = [scene.to_dict() for scene in project.scenes]
    assets = {}
    for asset in project.assets:
        assets.setdefault(asset.asset_type, []).append(asset.to_dict())
    fixture = {"scenes": scenes, "sceneIds": [scene["scene_id"] for scene in scenes],
               "assets": assets,
               "references": [reference.to_dict() for reference in project.references]}
    script = (_HARNESS
              .replace("__PYTHON__", json.dumps(sys.executable))
              .replace("__CHILD__", json.dumps(_ROUTE_CHILD))
              .replace("__ROOT__", json.dumps(str(ROOT)))
              .replace("__PROJECT_FILE__", json.dumps(str(project_file)))
              .replace("__WIDGET__", json.dumps((ROOT / "web/js/editor_widget.js").as_uri()))
              .replace("__QUEUE__", json.dumps((ROOT / "web/js/project_mutation_queue.js").as_uri()))
              .replace("__PANEL__", json.dumps((ROOT / "web/js/editor_reference_panel.js").as_uri()))
              .replace("__NOTES__", json.dumps((ROOT / "web/js/editor_notifications.js").as_uri()))
              .replace("__FIXTURE__", json.dumps(fixture))
              .replace("__BODY__", body))
    completed = subprocess.run([node, "--input-type=module"], input=script,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=120)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


# -- the harness proves itself on today's paths ----------------------------------

def test_the_transport_is_the_real_route_and_its_scene_is_adopted(tmp_path):
    """An accepted write stores through the route and the canonical scene replaces
    the active one through the real reconcile."""
    result = run_item_panel("""
    mount();
    const before = w.activeScene;
    const strength = inputsOf(cardAt(10))[2];
    strength.value = '0.5'; strength.dispatch('change');
    await settle();
    return { sent: sent.map((ops) => ops.map((op) => op.type)), stored: item('item-1', await serverScene()).strength,
      shown: item('item-1').strength, replaced: w.activeScene !== before, gets: gets.length };
    """, tmp_path)
    assert result == {"sent": [["update_reference_item"]], "stored": 0.5, "shown": 0.5,
                      "replaced": True, "gets": 0}


def test_a_refusal_reaches_the_gesture_in_the_clients_real_error_shape(tmp_path):
    """The route's code is on `error.payload.code`, never `error.code` (the client
    lifts only `project_version_conflict`), and a dead network has no status."""
    result = run_item_panel("""
    const shape = (error) => ({ code: error.code ?? null, payloadCode: error.payload?.code ?? null,
      status: Number.isInteger(error.status) ? error.status : null, type: error.constructor.name });
    const attempt = (fields, expected, key) => w._runSceneMutation([{ type: 'update_reference_item',
      reference_item_id: 'item-1', fields, expected }], { key, coalesce: false, refreshScenes: false })
      .then(() => null, shape);
    const routeRefusal = await attempt({ end_frame: 60 }, { end_frame: 40 }, 'k1');
    refuseNext('identity_mismatch');
    const scripted = await attempt({ strength: 0.2 }, { strength: 1 }, 'k2');
    offlineNext();
    const offline = await attempt({ strength: 0.2 }, { strength: 1 }, 'k3');
    return { routeRefusal, scripted, offline, stored: item('item-1', await serverScene()).strength };
    """, tmp_path)
    assert result["routeRefusal"] == {"code": None, "payloadCode": "lane_collision",
                                      "status": 409, "type": "Error"}
    assert result["scripted"] == {"code": None, "payloadCode": "identity_mismatch",
                                  "status": 409, "type": "Error"}
    assert result["offline"] == {"code": None, "payloadCode": None, "status": None,
                                 "type": "TypeError"}
    assert result["stored"] == 1.0


def test_the_gated_refresh_waits_for_a_focused_field_and_replays_on_leaving_it(tmp_path):
    """The panel is rebuilt only through the host gate: never under a focused input."""
    result = run_item_panel("""
    mount();
    const strength = inputsOf(cardAt(10))[2];
    strength.focus();
    item('item-2').strength = 0.3;
    w._renderSceneAfterLocalMutation({ viewport: false });
    flushFrames();
    const keptWhileFocused = inputsOf(cardAt(10))[2] === strength;
    strength.blur();
    await settle();
    flushFrames();
    return { keptWhileFocused, rebuiltAfter: inputsOf(cardAt(10))[2] !== strength,
      shows: inputsOf(cardAt(50))[2].value };
    """, tmp_path)
    assert result == {"keptWhileFocused": True, "rebuiltAfter": True, "shows": "0.30"}


# -- host plumbing ---------------------------------------------------------------

def test_a_pending_write_that_can_change_the_item_field_is_found_and_no_other(tmp_path):
    result = run_item_panel("""
    const q = w._projectMutationQueue;
    const park = (key, payload) => q.enqueue({ key, coalesce: false,
      intent: payload === null ? null : { payload }, run: () => new Promise(() => {}) });
    const pending = (id, field, options) => w._referenceItemFieldWritePending('scene', id, field, options);
    const snapshot = (id, fields) => Object.fromEntries(fields.map((f) => [f, pending(id, f)]));
    const empty = pending('item-1', 'strength');
    park('a', { sceneId: 'scene', operations: [{ type: 'update_reference_item', reference_item_id: 'item-1',
      fields: { start_frame: 12, end_frame: 42 } }] });
    park('b', { sceneId: 'other', operations: [{ type: 'delete_reference_item', reference_item_id: 'item-2' }] });
    park('references:1', [{ type: 'update_reference', reference_id: 'entity-1', fields: { name: 'x' } }]);
    await tick();
    const painted = snapshot('item-1', ['start_frame', 'strength', 'members']);
    const excluded = pending('item-1', 'start_frame', { excludeKeys: new Set(['a']) });
    const others = { otherItem: pending('item-4', 'strength'), otherScene: pending('item-2', 'strength') };
    w._referenceItemWriteState().unpaintedKeys.add('u');
    park('u', { sceneId: 'scene', operations: [{ type: 'update_reference_item', reference_item_id: 'item-4',
      fields: { strength: 0.5 } }] });
    const unpainted = snapshot('item-4', ['members', 'muted']);
    park('d', { sceneId: 'scene', operations: [{ type: 'bulk_delete_items',
      items: [{ type: 'clip', id: 'x' }, { type: 'reference', id: 'item-2' }] }] });
    park('e', { sceneId: 'scene', operations: [{ type: 'split_reference_item', reference_item_id: 'item-1', frame: 20 }] });
    const deletes = { bulk: pending('item-2', 'muted'), splitEnd: pending('item-1', 'end_frame'),
      splitStrength: pending('item-1', 'strength') };
    return { empty, painted, excluded, others, unpainted, deletes };
    """, tmp_path)
    assert result == {
        "empty": False,
        # A painted scalar write round-trips its members, so it does not name them.
        "painted": {"start_frame": True, "strength": False, "members": False},
        "excluded": False,
        "others": {"otherItem": False, "otherScene": False},
        # An unpainted one may have them rewritten by the route.
        "unpainted": {"members": True, "muted": False},
        "deletes": {"bulk": True, "splitEnd": True, "splitStrength": False},
    }


def test_writers_without_item_operations_are_counted_by_what_they_can_change(tmp_path):
    result = run_item_panel("""
    const q = w._projectMutationQueue;
    const probe = async (key, payload, fields = ['start_frame', 'strength', 'members']) => {
      const parked = new ProjectMutationQueue();
      w._projectMutationQueue = parked;
      parked.enqueue({ key, coalesce: false, intent: payload === null ? null : { payload },
        run: () => new Promise(() => {}) });
      await tick();
      const out = Object.fromEntries(fields.map((f) => [f,
        w._referenceItemFieldWritePending('scene', 'item-1', f)]));
      w._projectMutationQueue = q;
      return out;
    };
    return {
      history: await probe('history:undo:1', null),
      libraryDelete: await probe('references:2', [{ type: 'delete_member', member_id: 'member-b' }]),
      libraryEdit: await probe('references:3', [{ type: 'update_member', member_id: 'member-b' }]),
      removeLane: await probe('scene:scene:remove-lane', { sceneId: 'scene',
        operations: [{ type: 'remove_lane', lane_type: 'reference', lane_index: 1 }] }),
      removeVideoLane: await probe('scene:scene:remove-video-lane', { sceneId: 'scene',
        operations: [{ type: 'remove_lane', lane_type: 'video', lane_index: 1 }] }),
      duration: await probe('scene:scene:duration', { sceneId: 'scene',
        operations: [{ type: 'update_scene_fields', fields: { duration_frames: 50 } }] }),
      fps: await probe('scene:scene:fps', { sceneId: 'scene',
        operations: [{ type: 'update_scene_fields', fields: { fps: 30 } }] }),
      rename: await probe('scene:scene:name', { sceneId: 'scene',
        operations: [{ type: 'update_scene_fields', fields: { name: 'x' } }] }),
    };
    """, tmp_path)
    every = {"start_frame": True, "strength": True, "members": True}
    none = {"start_frame": False, "strength": False, "members": False}
    bounds = {"start_frame": True, "strength": False, "members": False}
    assert result == {"history": every, "libraryDelete": every, "libraryEdit": none,
                      "removeLane": every, "removeVideoLane": none,
                      "duration": bounds, "fps": bounds, "rename": none}


def test_a_foreign_write_queued_between_two_chain_writes_makes_the_baseline_unknown(tmp_path):
    """Audit finding: a trim painted and queued between two panel writes.

    w1 (40 -> 50) is accepted, the foreign trim (60) is accepted, w2 (70)
    fails. The server holds 60; restoring w1's 50 would show a value it does
    not hold, so the rollback defers.
    """
    result = run_item_panel("""
    const chains = new Map();
    const row = { end_frame: 40 };
    let foreignPending = false;
    const access = { isLive: () => true, read: (f) => row[f], write: (f, v) => { row[f] = v; } };
    const unknown = () => foreignPending;
    const w1 = w._openFieldWriteChains(chains, 'r', row, { end_frame: 50 }, { baselineUnknown: unknown });
    row.end_frame = 50;
    foreignPending = true; row.end_frame = 60;
    const w2 = w._openFieldWriteChains(chains, 'r', row, { end_frame: 70 }, { baselineUnknown: unknown });
    row.end_frame = 70;
    w._settleFieldWriteChains(chains, w1, 'ok', access);
    foreignPending = false;
    const failed = w._settleFieldWriteChains(chains, w2, 'failed', access);
    return { failed, shown: row.end_frame };
    """, tmp_path)
    assert result == {"failed": {"restored": False, "deferred": ["end_frame"]}, "shown": 70}


def test_a_chain_opened_over_another_writers_paint_defers_instead_of_restoring(tmp_path):
    """Unknown baseline: the row shows a paint the server may never have held."""
    result = run_item_panel("""
    const chains = new Map();
    const row = { strength: 0.7 };
    const access = { isLive: () => true, read: (f) => row[f], write: (f, v) => { row[f] = v; } };
    const unknown = w._openFieldWriteChains(chains, 'r', row, { strength: 0.2 }, { baselineUnknown: () => true });
    row.strength = 0.2;
    const failedUnknown = w._settleFieldWriteChains(chains, unknown, 'failed', access);
    const afterUnknown = row.strength;
    const known = w._openFieldWriteChains(chains, 'r', row, { strength: 0.4 });
    row.strength = 0.4;
    const failedKnown = w._settleFieldWriteChains(chains, known, 'failed', access);
    const first = w._openFieldWriteChains(chains, 'r', row, { strength: 0.9 }, { baselineUnknown: () => true });
    row.strength = 0.9;
    const second = w._openFieldWriteChains(chains, 'r', row, { strength: 0.1 });
    row.strength = 0.1;
    w._settleFieldWriteChains(chains, first, 'ok', access);
    const acceptedThenFailed = w._settleFieldWriteChains(chains, second, 'failed', access);
    return { failedUnknown, afterUnknown, failedKnown, afterKnown: 0.2, now: row.strength,
      acceptedThenFailed, open: chains.size };
    """, tmp_path)
    assert result["failedUnknown"] == {"restored": False, "deferred": ["strength"]}
    assert result["afterUnknown"] == 0.2
    assert result["failedKnown"] == {"restored": True, "deferred": []}
    # An accepted write makes an unknown baseline known: the later failure
    # restores the accepted value, not the other writer's paint.
    assert result["acceptedThenFailed"] == {"restored": True, "deferred": []}
    assert result["now"] == 0.9
    assert result["open"] == 0


def test_an_ordered_history_baseline_is_opted_into_not_inferred(tmp_path):
    """The explicit option starts the queue-order baseline; nothing else does."""
    result = run_item_panel("""
    const contextOf = async (from, to, options) => {
      w._latestHistoryOrderContext = null;
      const run = w._runSceneMutation([{ type: 'update_reference_item', reference_item_id: 'item-1',
        fields: { strength: to }, expected: { strength: from } }],
        { key: 'k' + to, coalesce: false, refreshScenes: false, ...options });
      const context = w._latestHistoryOrderContext;
      await run;
      return context ? { rebaseIntents: context.rebaseIntents, scene: context.scenes.has('scene') } : null;
    };
    const plain = await contextOf(1, 0.3, {});
    const opted = await contextOf(0.3, 0.4, { orderedHistoryBaseline: true });
    // An explicit context wins, as it did over the old inline trigger.
    w._latestHistoryOrderContext = null;
    const explicit = { operation: 'caller', scenes: new Map(), parent: null };
    await w._runSceneMutation([{ type: 'update_reference_item', reference_item_id: 'item-1',
      fields: { strength: 0.5 }, expected: { strength: 0.4 } }], { key: 'k-explicit', coalesce: false,
      refreshScenes: false, orderedHistoryBaseline: true, historyOrderContext: explicit });
    const explicitKept = w._latestHistoryOrderContext === null;
    // A write with no scene in its intent has no scene to order.
    w._latestHistoryOrderContext = null;
    await w._queueProjectMutation({ key: 'k-sceneless', coalesce: false, refreshScenes: false,
      orderedHistoryBaseline: true, intent: { projectId: 'project', operations: [] },
      run: async () => ({ payload: {} }) });
    return { plain, opted, explicitKept, sceneless: w._latestHistoryOrderContext };
    """, tmp_path)
    assert result == {"plain": None, "opted": {"rebaseIntents": False, "scene": True},
                      "explicitKept": True, "sceneless": None}


def test_the_item_write_state_is_replaced_on_project_change_and_destroy():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    update = widget.split("    updateProject(projectDir) {", 1)[1].split("\n    }\n", 1)[0]
    destroy = widget.split("    destroy() {", 1)[1].split("\n    }\n", 1)[0]
    assert "this._resetReferenceItemWriteState();" in update
    assert "this._resetReferenceItemWriteState();" in destroy


# -- Phase 3: Add item and Delete item --------------------------------------------
#
# Shared helpers for the panel's picker and cards.
_PANEL = """
const addItem = (memberName) => {
  nodes().find((n) => n.tagName === 'BUTTON' && n.textContent === '+ Add item').dispatch('click');
  const row = nodes().find((n) => n.tagName === 'BUTTON' && n.title === 'Stage this member'
    && n.textContent === `Subject · ${memberName}`);
  assert.ok(row, `the picker does not offer ${memberName}`);
  row.dispatch('click');
};
const deleteAt = (start) => {
  const card = cardAt(start);
  assert.ok(card, `no card at ${start}`);
  card.querySelectorAll('button').find((b) => b.textContent === 'Delete item').dispatch('click');
};
const ids = (scene = w.activeScene) => (scene.reference_items || []).map((row) => row.reference_item_id);
const creates = () => sent.flat().filter((op) => op.type === 'create_reference_item');
"""


def test_add_item_paints_the_minted_bar_before_the_write_and_an_edit_behind_it_addresses_it(tmp_path):
    result = run_item_panel(_PANEL + """
    mount();
    w.playhead = 80;
    hold();
    addItem('C');
    const minted = ids().find((id) => /^ref-/.test(id));
    const paintedBeforeAck = !!item(minted) && !!cardAt(80);
    // The new card is drawn, so its strength can be edited before the stage lands.
    const strength = inputsOf(cardAt(80))[2];
    strength.value = '0.4'; strength.dispatch('change');
    await release();
    await settle();
    const server = await serverScene();
    return { minted: creates()[0]?.fields?.reference_item_id === minted, paintedBeforeAck,
      editAddressed: sent[1]?.[0]?.reference_item_id === minted,
      stored: item(minted, server) && { start: item(minted, server).start_frame,
        end: item(minted, server).end_frame, strength: item(minted, server).strength },
      sentOps: sent.map((ops) => ops.map((op) => op.type)), gets: gets.length,
      undo: w._undoStack.map((entry) => entry.label) };
    """, tmp_path)
    assert result["minted"] and result["paintedBeforeAck"] and result["editAddressed"]
    assert result["stored"] == {"start": 80, "end": -1, "strength": 0.4}
    # Panel staging never writes the lane: one create, no lane-count or recipe op.
    assert result["sentOps"] == [["create_reference_item"], ["update_reference_item"]]
    assert result["undo"] == ["add reference item", "change reference strength"]


def test_a_refused_add_item_removes_its_bar_locally_with_one_message(tmp_path):
    result = run_item_panel(_PANEL + """
    mount();
    w.playhead = 80;
    refuseNext('invalid_reference_item', 400);
    addItem('C');
    const painted = ids().length;
    await settle();
    return { painted, after: ids(), gets: gets.length, undo: w._undoStack.length,
      toasts: toasts.filter((t) => t.tier !== 'info').map((t) => [t.message, t.detail]) };
    """, tmp_path)
    assert result["painted"] == 4
    assert result["after"] == ["item-1", "item-2", "item-4"]
    assert result["gets"] == 0
    assert result["undo"] == 0
    assert result["toasts"] == [["The Reference item could not be staged.", "invalid_reference_item"]]


def test_add_item_on_an_audio_lane_accepts_a_video_that_has_audio(tmp_path):
    result = run_item_panel(_PANEL + """
    mount(1);
    w.playhead = 50;
    addItem('VA');
    await settle();
    const staged = (await serverScene()).reference_items.filter((row) => row.lane_index === 1
      && row.start_frame === 50);
    return { staged: staged.map((row) => row.members.map((m) => m.member_id)), toasts: toasts.length };
    """, tmp_path)
    assert result == {"staged": [["member-va"]], "toasts": 0}


def _unconfigured_lane_project():
    project = fixture_project()
    scene = project.scenes[0]
    scene.reference_lane_count = 3
    scene.reference_lane_configs.append(LaneConfig())
    scene.reference_lane_recipes.append(ReferenceLaneRecipe(lane_id="lane-blank"))
    project.assets.append(Asset(asset_id="asset-voice", name="voice.mp4", asset_type="video",
                                path="media/voice.mp4", has_audio=True))
    project.references[0].members.append(ReferenceMember(
        member_id="member-voice", asset_id="asset-voice", name="VOICE",
        tags=["sonder:voice_identity"]))
    return project


def test_add_item_on_a_never_configured_lane_does_not_retype_it(tmp_path):
    """The drop resolver would retype this lane to audio for a voice-tagged video;
    the panel addresses the lane it shows and keeps its recipe."""
    result = run_item_panel(_PANEL + """
    mount(2);
    w.playhead = 10;
    addItem('VOICE');
    await settle();
    const server = await serverScene();
    return { ops: sent.map((ops) => ops.map((op) => op.type)),
      recipe: server.reference_lane_recipes[2], staged: server.reference_items
        .filter((row) => row.lane_index === 2).map((row) => row.members[0].member_id) };
    """, tmp_path, project=_unconfigured_lane_project())
    assert result["ops"] == [["create_reference_item"]]
    assert result["recipe"]["lane_id"] == "lane-blank"
    assert result["recipe"]["media_kind"] == "image" and result["recipe"]["recipe_id"] == ""
    assert result["staged"] == ["member-voice"]


def test_local_add_and_delete_refusals_send_nothing_and_keep_redo(tmp_path):
    result = run_item_panel(_PANEL + """
    mount();
    w._redoStack = [{ label: 'something undone', snapshot: {} }];
    w.playhead = 20;                                   // inside item-1
    addItem('C');
    const overlap = { sent: sent.length, undo: w._undoStack.length, redo: w._redoStack.length };
    const missing = await w._deleteReferenceItemFromPanel('no-such-item');
    w.activeScene.reference_lane_configs[0].locked = true;
    w._buildTrackLayout();
    w.playhead = 80;
    const lockedStage = await w._stageReferenceItemOnLane({ members: [{ entity_id: 'entity-1',
      member_id: 'member-c' }] }, 0, 80);
    const lockedDelete = await w._deleteReferenceItemFromPanel('item-1');
    return { overlap, missing, lockedStage, lockedDelete, sent: sent.length,
      undo: w._undoStack.length, redo: w._redoStack.length,
      rows: ids(), warnings: toasts.map((t) => t.source) };
    """, tmp_path)
    assert result["overlap"] == {"sent": 0, "undo": 0, "redo": 1}
    assert result["missing"] == "refused"
    assert result["lockedStage"] == "refused" and result["lockedDelete"] == "refused"
    assert (result["sent"], result["undo"], result["redo"]) == (0, 0, 1)
    assert result["rows"] == ["item-1", "item-2", "item-4"]
    # One source, so the notifier coalesces them into one toast with a count.
    assert result["warnings"] and set(result["warnings"]) == {"reference-panel-refused"}


def test_delete_item_paints_at_once_and_a_failed_delete_puts_the_row_back_at_its_index(tmp_path):
    result = run_item_panel(_PANEL + """
    mount();
    hold();
    offlineNext();
    offlineGetNext();                                  // the heal finds the server down too
    deleteAt(10);
    const rowsAtOnce = ids();
    // A button action repaints through the host's gated refresh, on the next frame.
    flushFrames();
    const painted = { rows: rowsAtOnce, card: !!cardAt(10) };
    await release();
    await settle();
    flushFrames();
    return { painted, after: ids(), card: !!cardAt(10), undo: w._undoStack.length,
      gets: gets.length, toasts: toasts.map((t) => t.message) };
    """, tmp_path)
    assert result["painted"] == {"rows": ["item-2", "item-4"], "card": False}
    assert result["after"] == ["item-1", "item-2", "item-4"]
    assert result["card"] is True
    # No answer means the delete may have committed, so a gated refresh is
    # armed -- and fails here, which is exactly when the local rollback matters.
    assert result["undo"] == 0 and result["gets"] == 1
    assert result["toasts"] == ["The Reference item could not be deleted."]


def test_an_accepted_delete_stays_deleted_and_owns_its_undo_step(tmp_path):
    result = run_item_panel(_PANEL + """
    mount();
    deleteAt(50);
    await settle();
    return { local: ids(), server: ids(await serverScene()),
      undo: w._undoStack.map((entry) => [entry.label, !!entry.postSnapshot]) };
    """, tmp_path)
    assert result == {"local": ["item-1", "item-4"], "server": ["item-1", "item-4"],
                      "undo": [["delete reference item", True]]}


def test_with_the_server_stopped_add_then_delete_leaves_no_phantom_row(tmp_path):
    """The create fails first and voids the delete's hold; the delete's failure
    must not reinsert a row the project never accepted."""
    result = run_item_panel(_PANEL + """
    mount();
    w.playhead = 80;
    offlineNext(); offlineNext(); offlineGetNext(); offlineGetNext();
    addItem('C');
    const minted = ids().find((id) => /^ref-/.test(id));
    deleteAt(80);
    await settle(12);
    return { rows: ids(), minted: ids().includes(minted), undo: w._undoStack.length,
      server: ids(await serverScene()), deferred: diag('reference_delete_rollback_deferred').length };
    """, tmp_path)
    assert result["rows"] == ["item-1", "item-2", "item-4"]
    assert result["minted"] is False
    assert result["undo"] == 0
    assert result["server"] == ["item-1", "item-2", "item-4"]
    assert result["deferred"] == 1


def test_a_refused_delete_keeps_the_previous_accepted_undo_step(tmp_path):
    """Bug Tracker 0.4a, delete half: the panel used to pop the top entry by label
    after the queue had already removed this write's own."""
    result = run_item_panel(_PANEL + """
    mount();
    const strength = inputsOf(cardAt(50))[2];
    strength.value = '0.6'; strength.dispatch('change');
    await settle();
    refuseNext('identity_mismatch');
    deleteAt(10);
    await settle();
    return { undo: w._undoStack.map((entry) => [entry.label, !!entry.postSnapshot]), rows: ids() };
    """, tmp_path)
    assert result["undo"] == [["change reference strength", True]]
    assert result["rows"] == ["item-1", "item-2", "item-4"]


def test_a_refused_delete_does_not_enter_the_next_edits_undo_snapshot(tmp_path):
    """Plan test 12. The panel's delete opts into the queue-order history
    baseline, so an edit queued behind it takes its before-snapshot from the
    canonical scene at its queue position -- which still holds the row the
    refused delete had painted away. Undoing that edit therefore cannot delete
    the row."""
    result = run_item_panel(_PANEL + """
    mount();
    hold();
    refuseNext('identity_mismatch');
    deleteAt(10);
    const strength = inputsOf(cardAt(50))[2];
    strength.value = '0.6'; strength.dispatch('change');
    const snapshotAtPush = ids(w._undoStack.at(-1).snapshot);
    await release();
    await settle(12);
    const entry = w._undoStack.at(-1);
    return { snapshotAtPush, snapshotNow: ids(entry.snapshot), label: entry.label,
      rows: ids(), sceneReads: sceneReads.length };
    """, tmp_path)
    assert result["snapshotAtPush"] == ["item-2", "item-4"], "the paint was in the push-time snapshot"
    assert result["snapshotNow"] == ["item-1", "item-2", "item-4"]
    assert result["label"] == "change reference strength"
    assert result["rows"] == ["item-1", "item-2", "item-4"]
    assert result["sceneReads"] >= 1


def test_a_project_switch_releases_an_in_flight_delete_hold(tmp_path):
    """Plan test 13, delete half: the old project's hold neither survives the
    switch nor reinserts its row anywhere when its write fails afterwards."""
    result = run_item_panel(_PANEL + """
    mount();
    Object.assign(w, { _clearStaleReplayState(){}, _clearVideoCache(){}, _sweepRenderCache(){},
      _fetchProjectSettings(){}, _renderQueuePanel(){}, _updateProjectIdentity(){},
      _clearUnconfirmedReferenceRetry(){}, _fetchReferences: async () => {},
      _fetchAssets: () => new Promise(() => {}), _renderCacheSweepGeneration: 0 });
    hold();
    offlineNext();
    deleteAt(10);
    const oldScene = w.activeScene;
    const holdsBefore = w._referenceItemWriteState().deleteHolds.size;
    w.updateProject('other-project');
    const holdsAfter = w._referenceItemWriteState().deleteHolds.size;
    await release();
    await settle();
    return { holdsBefore, holdsAfter, oldRows: ids(oldScene), active: w.activeScene,
      deferred: diag('reference_delete_rollback_deferred').map((e) => e.reason ?? e.payload?.reason ?? e.data?.reason) };
    """, tmp_path)
    assert result["holdsBefore"] == 1 and result["holdsAfter"] == 0
    assert result["oldRows"] == ["item-2", "item-4"]
    assert result["active"] is None
    assert result["deferred"] == ["scene_replaced"]


def test_the_stage_tail_keeps_its_diagnostics():
    """`_reportStagedReferenceIdShortfall` still hears the minted id, and the
    lane/recipe heal still records a deferral."""
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    tail = widget.split("    async _commitReferenceStageWithinGesture({", 1)[1]
    tail = tail.split("\n    async _handleAssetDrop(", 1)[0]
    assert 'this._reportStagedReferenceIdShortfall(result, painted ? referenceItemId : "");' in tail
    assert 'sessionDiagRecord("reference_stage_rollback_deferred", {});' in tail


def _two_scene_project():
    project = fixture_project()
    other = Scene(scene_id="scene-b", duration_frames=100, reference_lane_count=1,
                  reference_lane_configs=[LaneConfig()],
                  reference_lane_recipes=[ReferenceLaneRecipe(lane_id="lane-b", media_kind="image")])
    project.scenes.append(other)
    return project


def test_a_write_that_fails_after_a_scene_switch_still_rolls_back_the_scene_it_painted(tmp_path):
    """A switch reuses the list's scene objects, so the painted scene is still the
    one the author returns to; only a server replacement makes it stale."""
    result = run_item_panel(_PANEL + """
    mount();
    const sceneA = w.activeScene;
    w.playhead = 80;
    hold(2);
    refuseNext('invalid_reference_item', 400);
    refuseNext('identity_mismatch');
    addItem('C');
    const minted = ids().find((id) => /^ref-/.test(id));
    deleteAt(10);
    const painted = ids(sceneA);
    w._setActiveScene(w.scenes.find((scene) => scene.scene_id === 'scene-b'));
    await release();
    await release();
    await settle();
    return { painted, rowsA: ids(sceneA), phantom: ids(sceneA).includes(minted),
      active: w.activeScene.scene_id, listed: w.scenes.includes(sceneA) };
    """, tmp_path, project=_two_scene_project())
    assert result["painted"] == ["item-2", "item-4", result["painted"][2]]
    assert result["phantom"] is False
    assert result["rowsA"] == ["item-1", "item-2", "item-4"]
    assert result["active"] == "scene-b" and result["listed"] is True


def test_a_stage_whose_answer_was_lost_converges_on_what_the_server_committed(tmp_path):
    """No answer: the bar is removed locally, and a gated refresh brings back the
    row the server did commit."""
    result = run_item_panel(_PANEL + """
    mount();
    w.playhead = 80;
    lostNext();
    addItem('C');
    await settle(16);
    const server = await serverScene();
    const staged = (scene) => scene.reference_items.filter((row) => row.start_frame === 80).length;
    return { local: staged(w.activeScene), server: staged(server), gets: gets.length };
    """, tmp_path)
    assert result == {"local": 1, "server": 1, "gets": 1}


def test_a_delete_the_route_answers_item_not_found_is_not_put_back(tmp_path):
    """The route's code is on `error.payload.code`; `item_not_found` means the row
    is gone there too, so reinserting it would show a row nobody holds."""
    result = run_item_panel(_PANEL + """
    mount();
    refuseNext('item_not_found', 404);
    deleteAt(10);
    await settle();
    return { rows: ids(), reasons: diag('reference_delete_rollback_deferred')
      .map((e) => e.reason ?? e.payload?.reason ?? e.data?.reason) };
    """, tmp_path)
    assert result == {"rows": ["item-2", "item-4"], "reasons": ["item_not_found"]}


def test_an_accepted_delete_releases_its_hold(tmp_path):
    result = run_item_panel(_PANEL + """
    mount();
    deleteAt(50);
    const during = w._referenceItemWriteState().deleteHolds.size;
    await settle();
    return { during, after: w._referenceItemWriteState().deleteHolds.size };
    """, tmp_path)
    assert result == {"during": 1, "after": 0}


def test_a_member_the_lane_cannot_take_is_refused_before_anything_is_sent(tmp_path):
    result = run_item_panel(_PANEL + """
    const audio = await w._stageReferenceItemOnLane({ members: [{ entity_id: 'entity-1',
      member_id: 'member-s' }] }, 0, 80);
    const unknown = await w._stageReferenceItemOnLane({ members: [{ entity_id: 'entity-1',
      member_id: 'member-nobody' }] }, 0, 80);
    return { audio, unknown, sent: sent.length, undo: w._undoStack.length, rows: ids() };
    """, tmp_path)
    assert result == {"audio": "refused", "unknown": "refused", "sent": 0, "undo": 0,
                      "rows": ["item-1", "item-2", "item-4"]}


def test_a_refused_add_item_does_not_enter_the_next_edits_undo_snapshot(tmp_path):
    """Plan test 12, Add half: panel staging opts into the ordered baseline too."""
    result = run_item_panel(_PANEL + """
    mount();
    w.playhead = 80;
    hold();
    refuseNext('invalid_reference_item', 400);
    addItem('C');
    const minted = ids().find((id) => /^ref-/.test(id));
    const strength = inputsOf(cardAt(50))[2];
    strength.value = '0.6'; strength.dispatch('change');
    const atPush = ids(w._undoStack.at(-1).snapshot).includes(minted);
    await release();
    await settle(12);
    return { atPush, now: ids(w._undoStack.at(-1).snapshot).includes(minted),
      label: w._undoStack.at(-1).label };
    """, tmp_path)
    assert result == {"atPush": True, "now": False, "label": "change reference strength"}


def test_a_row_is_live_on_its_scene_object_or_in_a_live_delete_hold(tmp_path):
    result = run_item_panel("""
    const state = w._referenceItemWriteState();
    const sceneA = w.activeScene;
    const row = item('item-1');
    const live = () => w._referenceItemRowLive(state, sceneA, row);
    const onScene = live();
    sceneA.reference_items.splice(sceneA.reference_items.indexOf(row), 1);
    const removed = live();
    const hold = { scene: sceneA, row, index: 0, voided: false };
    state.deleteHolds.set('item-1', hold);
    const held = live();
    hold.voided = true;
    const voided = live();
    hold.voided = false;
    w._setActiveScene(w.scenes.find((scene) => scene.scene_id === 'scene-b'));
    const switchedAway = live();
    w.scenes = w.scenes.filter((scene) => scene !== sceneA);
    const replaced = live();
    return { onScene, removed, held, voided, switchedAway, replaced };
    """, tmp_path, project=_two_scene_project())
    assert result == {"onScene": True, "removed": False, "held": True, "voided": False,
                      "switchedAway": True, "replaced": False}
