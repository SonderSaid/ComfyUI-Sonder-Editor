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
* Every scene reaches the client with its keys SORTED. The server does not
  sort -- a stored record keeps its own key order through the unknown-field
  overlay -- but a stored order that differs from the route's field order is
  what a real project delivers (the QA project stores members as
  `entity_id, member_id, role, visual_intent`), and sorting is one such order.
  A comparison that minds key order fails here as it did live.
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
        print("@@" + json.dumps({"ok": True, "scene": scene.to_dict()}, sort_keys=True), flush=True)
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
    print("@@" + json.dumps({"ok": True, "payload": payload}, default=str, sort_keys=True), flush=True)
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
            # A stored role and retention: a member record the route carries
            # unchanged, and whose key ORDER differs on the wire (sorted) from
            # the order the route builds it in.
            {"reference_item_id": "item-1", "lane_index": 0, "start_frame": 10,
             "end_frame": 40, "members": [member("a", role="identity", visual_intent="preserve"),
                                          member("b")]},
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
  // Tag lists, and one `[data-x='v']` attribute selector (the panel's autofocus).
  querySelectorAll(sel) { const attr=/^\[data-([\w-]+)='([^']*)'\]$/.exec(String(sel).trim());
    const key=attr && attr[1].replace(/-(\w)/g, (_m, c) => c.toUpperCase());
    const tags=String(sel).split(",").map((v)=>v.trim().toUpperCase());
    const hit=(c)=>attr ? c.dataset[key] === attr[2] : tags.includes(c.tagName);
    const out=[]; const walk=(n)=>n.children.forEach((c)=>{ if(hit(c)) out.push(c); walk(c); });
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
  // The shipped `onIdle`, so an ordered history context is dropped when the
  // queue drains and the next wave pays its baseline read again, as it does live.
  _projectMutationQueue: new ProjectMutationQueue({ onIdle: () => {
    if (!w._historyOrderContextNeedsRetention?.(w._latestHistoryOrderContext)) {
      w._latestHistoryOrderContext = null;
    }
    w._replayDeferredProjectBackedRefresh();
  } }),
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
              # Sorted, as a project loaded from storage reaches the client.
              .replace("__FIXTURE__", json.dumps(fixture, sort_keys=True))
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
    """The same-label Undo discard (formerly a Bug Tracker entry), delete half: a
    refused delete leaves an earlier accepted step alone. The labels differ here,
    so this also passed on the old label-matched code; what holds the delete half
    is the pin that the panel no longer calls `_discardLastUndo`
    (`test_reference_panel_js.py`)."""
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
      _fetchAssets: () => new Promise(() => {}), _renderCacheSweepGeneration: 0,
      _referenceFramingIntents: new Map() });
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


# -- Phase 4: field edits -----------------------------------------------------------

_FIELDS = _PANEL + """
const strengthOf = (start) => inputsOf(cardAt(start))[2];
const setStrength = (start, value) => { const input = strengthOf(start);
  input.value = String(value); input.dispatch('change'); return input; };
const muteButton = (start) => cardAt(start).querySelectorAll('button')
  .find((b) => b.textContent === 'Active' || b.textContent === 'Muted');
const updates = () => sent.flat().filter((op) => op.type === 'update_reference_item');
"""


def test_a_field_edit_paints_before_the_write_and_a_second_one_is_sent_behind_it(tmp_path):
    """No drop: the second edit made while the first is saving reads the first
    one's paint as its guard, is sent in order, and both land."""
    result = run_item_panel(_FIELDS + """
    mount();
    hold(2);
    setStrength(10, 0.5);
    const paintedFirst = item('item-1').strength;
    setStrength(10, 0.7);
    const paintedSecond = item('item-1').strength;
    await release();
    await release();
    await settle();
    return { paintedFirst, paintedSecond,
      sent: updates().map((op) => [op.fields.strength, op.expected.strength]),
      local: item('item-1').strength, server: item('item-1', await serverScene()).strength,
      undo: w._undoStack.map((entry) => [entry.label, !!entry.postSnapshot]) };
    """, tmp_path)
    assert result["paintedFirst"] == 0.5 and result["paintedSecond"] == 0.7
    assert result["sent"] == [[0.5, 1.0], [0.7, 0.5]]
    assert result["local"] == 0.7 and result["server"] == 0.7
    assert result["undo"] == [["change reference strength", True]] * 2


def test_mute_clicked_twice_before_a_repaint_ends_active(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    hold(2);
    const button = muteButton(10);
    button.dispatch('click');
    const afterFirst = item('item-1').muted;
    button.dispatch('click');                 // the card has not repainted yet
    await release(); await release(); await settle();
    return { afterFirst, local: item('item-1').muted, server: item('item-1', await serverScene()).muted,
      sent: updates().map((op) => op.fields.muted) };
    """, tmp_path)
    assert result == {"afterFirst": True, "local": False, "server": False, "sent": [True, False]}


def test_two_refused_edits_return_the_field_to_its_saved_value_without_the_network(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    hold(2);
    setStrength(10, 0.5);
    setStrength(10, 0.7);
    refuseNext('identity_mismatch'); refuseNext('identity_mismatch');
    await release(); await release(); await settle();
    return { local: item('item-1').strength, gets: gets.length, undo: w._undoStack.length,
      toasts: toasts.filter((t) => t.source === 'project-mutation-failed').length };
    """, tmp_path)
    assert result == {"local": 1.0, "gets": 0, "undo": 0, "toasts": 1}


def test_a_refused_edit_behind_an_accepted_one_keeps_the_accepted_value(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    hold(2);
    setStrength(10, 0.5);
    setStrength(10, 0.7);
    await release();                          // the first lands
    refuseNext('identity_mismatch');
    await release();                          // the second is refused
    await settle();
    return { local: item('item-1').strength, server: item('item-1', await serverScene()).strength,
      undo: w._undoStack.map((entry) => entry.label) };
    """, tmp_path)
    # The same-label Undo discard (formerly a Bug Tracker entry): the accepted
    # step stays; only the refused one leaves.
    assert result == {"local": 0.5, "server": 0.5, "undo": ["change reference strength"]}


def test_a_rollback_defers_when_another_writers_edit_of_the_field_is_pending(tmp_path):
    """A timeline move queued on the item: the row shows its paint, so the panel
    chain has no known baseline and a failure must not restore a guess."""
    result = run_item_panel(_FIELDS + """
    mount();
    hold(2);
    // The timeline's move: painted, then queued.
    item('item-1').start_frame = 12;
    const moving = w._runSceneMutation([{ type: 'update_reference_item', reference_item_id: 'item-1',
      fields: { start_frame: 12 }, expected: { start_frame: 10 } }],
      { key: 'timeline-move', coalesce: false, refreshScenes: false }).catch(() => {});
    const start = inputsOf(cardAt(10))[0];
    start.value = '14'; start.dispatch('change');
    const painted = item('item-1').start_frame;
    await release();                          // the move lands
    refuseNext('identity_mismatch');
    await release();                          // the panel edit is refused
    await settle();
    await moving;
    return { painted, shownAfter: item('item-1').start_frame,
      deferred: diag('reference_item_rollback_deferred').map((e) => e.fields ?? e.payload?.fields ?? e.data?.fields),
      gets: gets.length, server: item('item-1', await serverScene()).start_frame };
    """, tmp_path)
    assert result["painted"] == 14
    assert result["deferred"] == [["start_frame"]]
    # The re-armed heal read the scene: the row shows what the server holds.
    assert result["gets"] == 1
    assert result["server"] == 12 and result["shownAfter"] == 12


def test_local_field_refusals_send_nothing_push_nothing_and_put_the_field_back(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    w._redoStack = [{ label: 'undone', snapshot: {} }];
    const end = inputsOf(cardAt(10))[1];
    end.value = '60'; end.dispatch('change');         // into item-2 at 50
    await settle(2);
    const overlapShown = end.value;
    end.value = '0'; end.dispatch('change');          // an end of 0 is not "to scene end"
    await settle(2);
    const zeroShown = end.value;
    const unchanged = await w._writeReferenceItemFromPanel('item-1', { strength: 1 }, 'noop');
    return { overlapShown, zeroShown, unchanged, sent: sent.length, undo: w._undoStack.length,
      redo: w._redoStack.length, end: item('item-1').end_frame,
      messages: toasts.map((t) => t.message) };
    """, tmp_path)
    assert result["overlapShown"] == "40" and result["zeroShown"] == "40"
    assert result["unchanged"] == "unchanged"
    assert (result["sent"], result["undo"], result["redo"], result["end"]) == (0, 0, 1, 40)
    assert "That range overlaps another item on this lane." in result["messages"]


def test_an_edit_from_an_undrawn_card_writes_the_live_row(tmp_path):
    """The panel was not rebuilt (its field has focus) after the scene object was
    replaced; the edit still addresses and guards the live row."""
    result = run_item_panel(_FIELDS + """
    mount();
    const input = strengthOf(50);
    input.focus();
    await w._writeReferenceItemFromPanel('item-2', { muted: true }, 'toggle reference mute');
    await settle();
    const replaced = w.activeScene;
    const stillDrawn = strengthOf(50) === input;
    input.value = '0.3'; input.dispatch('change');
    await settle();
    const last = updates().at(-1);
    return { stillDrawn, expected: last.expected, liveMuted: item('item-2').muted,
      live: item('item-2').strength, sameScene: w.activeScene === replaced,
      server: item('item-2', await serverScene()).strength };
    """, tmp_path)
    assert result["stillDrawn"] is True
    assert result["expected"] == {"strength": 1.0}
    assert result["liveMuted"] is True and result["live"] == 0.3 and result["server"] == 0.3


def test_a_focused_field_is_not_rebuilt_and_a_button_repaints_on_the_next_frame(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    hold(2);
    const input = strengthOf(10);
    input.focus();
    input.value = '0.4'; input.dispatch('change');
    flushFrames();
    const kept = strengthOf(10) === input;
    input.blur();
    muteButton(50).dispatch('click');
    const beforeFrame = muteButton(50).textContent;
    await settle(2);
    flushFrames();
    const afterFrame = muteButton(50).textContent;
    await release(); await release(); await settle();
    return { kept, beforeFrame, afterFrame };
    """, tmp_path)
    assert result == {"kept": True, "beforeFrame": "Active", "afterFrame": "Muted"}


def test_an_edit_that_fails_behind_a_delete_rolls_back_onto_the_held_row(tmp_path):
    """The failed delete then reinserts the corrected row, not the edit's paint."""
    result = run_item_panel(_FIELDS + """
    mount();
    hold(2);
    setStrength(10, 0.5);
    deleteAt(10);
    refuseNext('identity_mismatch');
    await release();                          // the edit is refused
    offlineNext(); offlineGetNext();
    await release();                          // the delete fails
    await settle(8);
    return { rows: ids(), strength: item('item-1')?.strength };
    """, tmp_path)
    assert result == {"rows": ["item-1", "item-2", "item-4"], "strength": 1.0}


# The unpainted write the tests below build on: an authored retention, which
# the route validates against recipe authority the mirror does not hold, so
# the mirror declines to paint it and the route accepts it (`_retention_project`).
# Until backlog step 1 a strength edit beside a stale member record, or beside
# a member whose asset the client could not see, declined as well; the route
# no longer re-judges members a scalar write does not name, so those paint.
_UNPAINTED = _FIELDS + """
const retain = (id, member, value) => w._editReferenceItemMembersFromPanel(id,
  { kind: 'patch', memberId: member, patch: { visual_intent: value } }, 'change visual retention');
const retentionOf = (id, member, scene = w.activeScene) =>
  item(id, scene).members.find((m) => m.member_id === member)?.visual_intent || '';
"""


def test_an_edit_the_mirror_cannot_paint_is_sent_unpainted_behind_a_barrier_and_adopted(tmp_path):
    """Nothing is painted, member-guarded gestures wait on the item, and the
    answer is adopted."""
    result = run_item_panel(_UNPAINTED + """
    mount();
    hold();
    const pending = retain('item-2', 'member-c', 'partial');
    const duringWrite = { retention: retentionOf('item-2', 'member-c'),
      barrier: w._referenceItemWriteState().barriers.has('item-2'),
      unpainted: w._referenceItemWriteState().unpaintedKeys.size };
    await release();
    await pending;
    await settle();
    return { duringWrite, after: { retention: retentionOf('item-2', 'member-c'),
      barrier: w._referenceItemWriteState().barriers.has('item-2'),
      unpainted: w._referenceItemWriteState().unpaintedKeys.size } };
    """, tmp_path, project=_retention_project())
    assert result["duringWrite"] == {"retention": "", "barrier": True, "unpainted": 1}
    assert result["after"] == {"retention": "partial", "barrier": False, "unpainted": 0}


def test_an_unpainted_edit_adopts_its_answer_when_the_reconcile_defers(tmp_path):
    """Another write queued behind it defers the scene reconcile, so the host
    brings the canonical row's fields in itself -- and only onto the same,
    untouched row."""
    result = run_item_panel(_UNPAINTED + """
    mount();
    hold(2);
    retain('item-2', 'member-c', 'partial');  // unpainted
    setStrength(10, 0.8);                     // queued behind: the reconcile will defer
    const before = w.activeScene;
    await release();
    const adopted = { retention: retentionOf('item-2', 'member-c'),
      sameScene: w.activeScene === before };
    await release();
    await settle();
    return adopted;
    """, tmp_path, project=_retention_project())
    assert result == {"retention": "partial", "sameScene": True}


def test_a_project_switch_releases_the_field_chains(tmp_path):
    """Plan test 13, field half."""
    result = run_item_panel(_FIELDS + """
    mount();
    Object.assign(w, { _clearStaleReplayState(){}, _clearVideoCache(){}, _sweepRenderCache(){},
      _fetchProjectSettings(){}, _renderQueuePanel(){}, _updateProjectIdentity(){},
      _clearUnconfirmedReferenceRetry(){}, _fetchReferences: async () => {},
      _fetchAssets: () => new Promise(() => {}), _renderCacheSweepGeneration: 0,
      _referenceFramingIntents: new Map() });
    hold();
    setStrength(10, 0.5);
    const oldScene = w.activeScene;
    const chainsBefore = w._referenceItemWriteState().chains.size;
    w.updateProject('other-project');
    const chainsAfter = w._referenceItemWriteState().chains.size;
    refuseNext('identity_mismatch');
    await release();
    await settle();
    return { chainsBefore, chainsAfter, oldStrength: item('item-1', oldScene).strength };
    """, tmp_path)
    # The old scene object is no longer held by the editor, so its paint is left
    # alone rather than restored into a project nobody shows.
    assert result == {"chainsBefore": 1, "chainsAfter": 0, "oldStrength": 0.5}


def test_an_edit_behind_an_unpainted_one_waits_and_guards_against_the_answer(tmp_path):
    """The second edit waits on the item's barrier and reads its guard from the
    adopted answer. It names the field the unpainted write changed, because
    only then does waiting change the result: without the wait its guard would
    state the drawn retention, and the route would refuse it."""
    result = run_item_panel(_UNPAINTED + """
    mount();
    hold();
    retain('item-2', 'member-c', 'partial');  // unpainted
    retain('item-2', 'member-c', 'preserve'); // waits on the barrier
    await release();
    await settle(10);
    const retention = (members) => members[0].visual_intent || '';
    return { guards: updates().map((op) => retention(op.expected.members)),
      local: retentionOf('item-2', 'member-c'),
      server: retentionOf('item-2', 'member-c', await serverScene()), toasts: toasts.length };
    """, tmp_path, project=_retention_project())
    assert result["guards"] == ["", "partial"]
    assert result["local"] == result["server"] == "preserve"
    assert result["toasts"] == 0


def test_an_item_with_stored_roles_paints_its_edit(tmp_path):
    """Live regression: stored members arrive with sorted keys, and a
    key-order-sensitive comparison declined every such edit to paint."""
    result = run_item_panel(_FIELDS + """
    mount();
    hold();
    setStrength(10, 0.5);
    const painted = item('item-1').strength;
    const keys = Object.keys(item('item-1').members[0]);
    await release(); await settle();
    return { painted, keys, unpainted: w._referenceItemWriteState().unpaintedKeys.size };
    """, tmp_path)
    assert result["keys"] == ["entity_id", "member_id", "role", "visual_intent"]
    assert result["painted"] == 0.5 and result["unpainted"] == 0


# -- Phase 4 audit: fixes and the gaps it found ------------------------------------

def test_three_quick_edits_behind_an_unpainted_one_all_land(tmp_path):
    """Audit finding 1: the second waiting edit installs a barrier of its own, so
    the third must wait again rather than read a row the second never painted."""
    result = run_item_panel(_UNPAINTED + """
    // Each authored retention is unpaintable, so every edit raises a barrier.
    mount();
    hold();
    retain('item-2', 'member-c', 'partial');
    retain('item-2', 'member-c', 'preserve');
    retain('item-2', 'member-c', 'partial');
    await release();
    await settle(20);
    const retention = (members) => members[0].visual_intent || '';
    return { sent: updates().map((op) => [retention(op.fields.members),
                                          retention(op.expected.members)]),
      local: retentionOf('item-2', 'member-c'),
      server: retentionOf('item-2', 'member-c', await serverScene()), toasts: toasts.length };
    """, tmp_path, project=_retention_project())
    assert result["sent"] == [["partial", ""], ["preserve", "partial"], ["partial", "preserve"]]
    assert result["local"] == result["server"] == "partial"
    assert result["toasts"] == 0


def test_a_value_set_back_behind_an_unpainted_edit_is_sent(tmp_path):
    """Audit finding 4: the panel no longer decides "unchanged" from a row an
    unpainted write has not reached. Setting the retention back to what is
    drawn looks like no change until the answer is adopted; resolved after the
    wait, it is one, and it is sent.

    The toggle-direction half of this finding (mute twice) needs an unpainted
    write on `muted` itself. Since backlog step 1 a mute always paints, so that
    case has no realistic form and is no longer tested here."""
    result = run_item_panel(_UNPAINTED + """
    mount();
    hold();
    retain('item-2', 'member-c', 'partial');  // unpainted
    retain('item-2', 'member-c', '');         // waits; '' is what is drawn
    await release();
    await settle(16);
    const retention = (members) => members[0].visual_intent || '';
    return { sent: updates().map((op) => retention(op.fields.members)),
      local: retentionOf('item-2', 'member-c'),
      server: retentionOf('item-2', 'member-c', await serverScene()) };
    """, tmp_path, project=_retention_project())
    assert result["sent"] == ["partial", ""]
    assert result["local"] == result["server"] == ""


def test_an_edit_waiting_on_a_barrier_is_dropped_by_a_project_switch_not_sent_there(tmp_path):
    """Audit finding 2: the waiting edit must not post to, nor reconcile into, the
    project the editor switched to."""
    result = run_item_panel(_UNPAINTED + """
    mount();
    Object.assign(w, { _clearStaleReplayState(){}, _clearVideoCache(){}, _sweepRenderCache(){},
      _fetchProjectSettings(){}, _renderQueuePanel(){}, _updateProjectIdentity(){},
      _clearUnconfirmedReferenceRetry(){}, _fetchReferences: async () => {},
      _fetchAssets: () => new Promise(() => {}), _renderCacheSweepGeneration: 0,
      _referenceFramingIntents: new Map() });
    hold();
    const first = retain('item-2', 'member-c', 'partial');
    const second = w._writeReferenceItemFromPanel('item-2', { strength: 0.6 }, 'change reference strength');
    await tick();
    w.updateProject('copy-project');
    w.activeScene = structuredClone(fixture.scenes[0]);     // same ids, another project
    w.activeSceneId = 'scene';
    const other = w.activeScene;
    await release();
    const outcomes = [await first, await second];
    await settle();
    return { outcomes, posted: sent.length, otherUntouched: w.activeScene === other
      && item('item-2', other).strength === 1 && retentionOf('item-2', 'member-c', other) === '' };
    """, tmp_path, project=_retention_project())
    assert result == {"outcomes": ["ok", "refused"], "posted": 1, "otherUntouched": True}


def test_an_unpainted_answer_reaches_a_scene_the_author_switched_away_from(tmp_path):
    """Audit finding 3: the reconcile only replaces the ACTIVE scene; the scene
    object in the list gets the answer adopted onto it, so switching back shows
    it and the next edit guards against it."""
    project = _retention_project()
    project.scenes.append(Scene(scene_id="scene-b", duration_frames=100, reference_lane_count=1,
                                reference_lane_configs=[LaneConfig()],
                                reference_lane_recipes=[ReferenceLaneRecipe(lane_id="lane-b")]))
    result = run_item_panel(_UNPAINTED + """
    mount();
    const sceneA = w.activeScene;
    hold();
    const pending = retain('item-2', 'member-c', 'partial');
    await tick();
    w._setActiveScene(w.scenes.find((scene) => scene.scene_id === 'scene-b'));
    await release();
    await pending;
    await settle();
    const adopted = retentionOf('item-2', 'member-c', sceneA);
    w._setActiveScene(sceneA);
    const next = await retain('item-2', 'member-c', 'preserve');
    await settle();
    return { adopted, next, expected: updates().at(-1).expected.members[0].visual_intent || '',
      server: retentionOf('item-2', 'member-c', await serverScene()) };
    """, tmp_path, project=project)
    assert result == {"adopted": "partial", "next": "ok", "expected": "partial",
                      "server": "preserve"}


def test_a_failed_edit_in_a_focused_field_shows_the_saved_value_again(tmp_path):
    """Audit finding 5, and plan Verification 3 in the harness: strength with the
    server down reverts with one message, and the focused field follows."""
    result = run_item_panel(_FIELDS + """
    mount();
    const input = strengthOf(10);
    input.focus();
    offlineNext(); offlineGetNext();
    input.value = '0.40'; input.dispatch('change');
    const painted = item('item-1').strength;
    await settle(12);
    return { painted, row: item('item-1').strength, shown: input.value,
      toasts: toasts.map((t) => t.message), undo: w._undoStack.length, gets: gets.length };
    """, tmp_path)
    # No answer means it may have saved: the gated scenes read is scheduled
    # (and fails here, the server being down), which is why the local rollback
    # is what the author sees.
    assert result == {"painted": 0.4, "row": 1.0, "shown": "1.00",
                      "toasts": ["The Reference item change could not be saved."], "undo": 0,
                      "gets": 1}


def test_a_refused_field_paint_does_not_enter_the_next_edits_undo_snapshot(tmp_path):
    """Plan test 12, field half: the writer opts into the ordered baseline."""
    result = run_item_panel(_FIELDS + """
    mount();
    hold();
    refuseNext('identity_mismatch');
    setStrength(10, 0.5);
    setStrength(50, 0.6);
    const atPush = item('item-1', w._undoStack.at(-1).snapshot).strength;
    await release();
    await settle(12);
    return { atPush, now: item('item-1', w._undoStack.at(-1).snapshot).strength,
      label: w._undoStack.at(-1).label, entries: w._undoStack.length };
    """, tmp_path)
    assert result == {"atPush": 0.5, "now": 1.0, "label": "change reference strength", "entries": 1}


def test_a_write_with_no_project_context_counts_as_refused(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    const snapshot = w._snapshotProjectMutationContext;
    w._snapshotProjectMutationContext = () => null;
    const outcome = await w._writeReferenceItemFromPanel('item-1', { strength: 0.5 }, 'change reference strength');
    w._snapshotProjectMutationContext = snapshot;
    return { outcome, row: item('item-1').strength, undo: w._undoStack.length, sent: sent.length };
    """, tmp_path)
    assert result == {"outcome": "refused", "row": 1.0, "undo": 0, "sent": 0}


def test_a_field_edit_on_a_locked_lane_is_refused_before_anything_else(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    w._redoStack = [{ label: 'undone', snapshot: {} }];
    w.activeScene.reference_lane_configs[0].locked = true;
    w._buildTrackLayout();
    const outcome = await w._writeReferenceItemFromPanel('item-1', { strength: 0.5 }, 'change reference strength');
    return { outcome, row: item('item-1').strength, sent: sent.length, undo: w._undoStack.length,
      redo: w._redoStack.length, sources: toasts.map((t) => t.source) };
    """, tmp_path)
    assert result == {"outcome": "refused", "row": 1.0, "sent": 0, "undo": 0, "redo": 1,
                      "sources": ["reference-panel-refused"]}


def test_an_unpainted_write_takes_the_baseline_from_an_open_chain(tmp_path):
    """A painted edit is in flight when an unpainted one is sent behind it. The
    host cannot know which fields the unpainted answer rewrites until it
    arrives, so the first one's failure must not restore the value from before
    both; the re-armed refresh shows what the server holds instead."""
    result = run_item_panel(_UNPAINTED + """
    mount();
    hold(2);
    setStrength(50, 0.5);                                   // painted, chain open
    const second = retain('item-2', 'member-c', 'partial'); // unpainted
    refuseNext('identity_mismatch');
    await release();                                        // the painted one is refused
    const afterFirst = item('item-2').strength;
    await release();
    await second;
    await settle();
    return { afterFirst, deferred: diag('reference_item_rollback_deferred').length,
      final: item('item-2').strength, server: item('item-2', await serverScene()).strength,
      retention: retentionOf('item-2', 'member-c', await serverScene()) };
    """, tmp_path, project=_retention_project())
    assert result["afterFirst"] == 0.5, "not restored to 1.0 under an unpainted write"
    assert result["deferred"] >= 1
    assert result["server"] == 1.0 and result["final"] == 1.0
    assert result["retention"] == "partial"


def test_edit_as_override_derives_from_the_live_members(tmp_path):
    result = run_item_panel(_FIELDS + """
    const { deriveReferencePrompt } = await import(__RESOLUTION__);
    mount();
    const card = cardAt(10);
    card.querySelectorAll('details').forEach((d) => { d.open = true; });
    const take = walk(card).find((n) => n.tagName === 'BUTTON' && n.textContent === 'Edit as override');
    // A reconcile replaced the scene since the card was drawn (the panel has
    // not repainted), and the live row stages only member b now.
    w.activeScene = structuredClone(w.activeScene);
    item('item-1').members = [item('item-1').members[1]];
    const soft = w.activeScene.reference_lane_recipes[0].recipe?.soft || {};
    const expectedText = deriveReferencePrompt({ promptOverride: '', soft, members:
      item('item-1').members.map((ref) => { const r = w._referenceMemberForRef(ref);
        return { entity_name: r?.reference?.name || '', member_name: r?.member?.name || '',
          prompt: r?.member?.prompt || '' }; }) });
    take.dispatch('click');
    await settle(4);
    return { sent: updates().at(-1)?.fields?.prompt_override, expectedText };
    """.replace("__RESOLUTION__", json.dumps((ROOT / "web/js/reference_resolution.js").as_uri())), tmp_path)
    assert result["sent"] == result["expectedText"] and result["sent"]


def test_each_settled_field_edit_refreshes_the_prompt_consumers_once(tmp_path):
    result = run_item_panel(_FIELDS + """
    mount();
    let refreshed = 0;
    w._refreshPromptContextDependencyConsumers = () => { refreshed += 1; };
    await w._writeReferenceItemFromPanel('item-1', { strength: 0.5 }, 'change reference strength');
    const afterOk = refreshed;
    await w._writeReferenceItemFromPanel('item-1', { end_frame: 60 }, 'trim reference item');
    return { afterOk, afterLocalRefusal: refreshed };
    """, tmp_path)
    assert result == {"afterOk": 1, "afterLocalRefusal": 1}


# -- Phase 5: member edits ------------------------------------------------------------

_MEMBERS = _FIELDS + """
const order = (id = 'item-1', scene = w.activeScene) => item(id, scene).members.map((m) => m.member_id);
const retentions = (id = 'item-1', scene = w.activeScene) =>
  item(id, scene).members.map((m) => m.visual_intent || '');
const memberButtons = (start, text) => cardAt(start).querySelectorAll('button')
  .filter((b) => b.textContent === text);
const drag = (key) => ({ entity_id: 'entity-1', member_id: `member-${key}` });
"""


def _retention_project():
    """Lane 0 exposes per-member retention: a retention change is an authored
    member field the mirror declines to paint, and the route accepts."""
    project = fixture_project()
    project.scenes[0].reference_lane_recipes[0].recipe["soft"]["role_fields"] = ["visual_intent"]
    return project


def _three_member_project():
    """item-1 holds [a, b, d], so a member can move twice."""
    project = fixture_project()
    project.assets.append(Asset(asset_id="asset-d", name="d.png", asset_type="image",
                                path="media/d.png"))
    project.references[0].members.append(
        ReferenceMember(member_id="member-d", asset_id="asset-d", name="D"))
    project.scenes[0].reference_items[0].members.append(
        {"entity_id": "entity-1", "member_id": "member-d"})
    return project


def test_a_member_moved_up_twice_quickly_moves_twice_and_both_persist(tmp_path):
    """Plan manual row "move a member ↑ twice quickly": the second click on the
    same, unrepainted button moves the member again from where the first put it."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold(2);
    const up = memberButtons(10, '↑')[2];            // member d
    up.dispatch('click');
    const afterFirst = order();
    up.dispatch('click');
    const afterSecond = order();
    await release(); await release(); await settle();
    return { afterFirst, afterSecond, local: order(), server: order('item-1', await serverScene()),
      sent: updates().map((op) => op.fields.members.map((m) => m.member_id)),
      undo: w._undoStack.map((entry) => entry.label) };
    """, tmp_path, project=_three_member_project())
    assert result["afterFirst"] == ["member-a", "member-d", "member-b"]
    assert result["afterSecond"] == ["member-d", "member-a", "member-b"]
    assert result["sent"] == [result["afterFirst"], result["afterSecond"]]
    assert result["local"] == result["server"] == result["afterSecond"]
    assert result["undo"] == ["reorder reference members"] * 2


def test_a_stale_move_past_the_top_of_the_list_is_a_silent_no_op(tmp_path):
    result = run_item_panel(_MEMBERS + """
    mount();
    const outcome = await w._editReferenceItemMembersFromPanel('item-1',
      { kind: 'move', memberId: 'member-a', direction: -1 }, 'reorder reference members');
    return { outcome, sent: sent.length, undo: w._undoStack.length, toasts: toasts.length };
    """, tmp_path)
    assert result == {"outcome": "unchanged", "sent": 0, "undo": 0, "toasts": 0}


def test_removing_a_member_paints_and_removing_the_last_one_is_refused_locally(tmp_path):
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    memberButtons(10, '×')[1].dispatch('click');     // member b
    const painted = order();
    await release(); await settle();
    w._redoStack = [{ label: 'undone', snapshot: {} }];
    memberButtons(50, '×')[0].dispatch('click');     // item-2's only member
    await settle(4);
    return { painted, server: order('item-1', await serverScene()), sent: sent.length,
      redo: w._redoStack.length, lone: order('item-2'), messages: toasts.map((t) => t.message) };
    """, tmp_path)
    assert result["painted"] == ["member-a"] and result["server"] == ["member-a"]
    assert (result["sent"], result["redo"], result["lone"]) == (1, 1, ["member-c"])
    assert "An item must keep at least one Library member." in result["messages"]


def test_a_member_edit_behind_an_unpainted_one_waits_and_both_persist(tmp_path):
    """Plan test 9, first row: an authored member field is sent unpainted behind
    the item's barrier. A second member edit made while it saves -- with the
    first select still focused -- waits, reads the adopted members as its guard,
    and both persist."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    const [first, second] = cardAt(10).querySelectorAll('select');
    first.focus();
    first.value = 'partial'; first.dispatch('change');
    const unpainted = retentions();
    await settle(2);
    second.value = 'preserve'; second.dispatch('change');
    await settle(4);
    const whileHeld = sent.length;
    await release();
    await settle(10);
    return { unpainted, whileHeld,
      guard: updates()[1]?.expected.members.map((m) => m.visual_intent || ''),
      local: retentions(), server: retentions('item-1', await serverScene()),
      shown: [first.value, second.value], stillFocused: document.activeElement === first,
      toasts: toasts.length };
    """, tmp_path, project=_retention_project())
    assert result["unpainted"] == ["preserve", ""]
    assert result["whileHeld"] == 1
    assert result["guard"] == ["partial", ""]
    assert result["local"] == result["server"] == ["partial", "preserve"]
    assert result["shown"] == ["partial", "preserve"] and result["stillFocused"] is True
    assert result["toasts"] == 0


def test_a_refused_retention_change_puts_the_select_back(tmp_path):
    result = run_item_panel(_MEMBERS + """
    mount();
    const [select] = cardAt(10).querySelectorAll('select');
    select.focus();
    refuseNext('unsupported_reference_member_field');
    select.value = 'partial'; select.dispatch('change');
    await settle(8);
    return { shown: select.value, local: retentions()[0], undo: w._undoStack.length,
      toasts: toasts.length, barrier: w._referenceItemWriteState().barriers.size };
    """, tmp_path, project=_retention_project())
    assert result == {"shown": "preserve", "local": "preserve", "undo": 0, "toasts": 1,
                      "barrier": 0}


def test_delete_item_behind_an_unpainted_member_edit_waits_and_succeeds(tmp_path):
    """Plan test 9, second row."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    const [select] = cardAt(10).querySelectorAll('select');
    select.value = 'partial'; select.dispatch('change');
    await settle(2);
    deleteAt(10);
    await settle(4);
    const whileHeld = sent.length;
    await release();
    await settle(10);
    return { whileHeld, rows: ids(), server: ids(await serverScene()),
      messages: toasts.map((t) => t.message) };
    """, tmp_path, project=_retention_project())
    assert result == {"whileHeld": 1, "rows": ["item-2", "item-4"],
                      "server": ["item-2", "item-4"], "messages": []}


def test_a_timeline_append_behind_an_unpainted_member_edit_waits_and_succeeds(tmp_path):
    """Plan test 9, third row."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    const [select] = cardAt(10).querySelectorAll('select');
    select.value = 'partial'; select.dispatch('change');
    await settle(2);
    const appending = w._appendReferenceMembers('item-1', { members: [drag('c')] });
    await settle(4);
    const whileHeld = sent.length;
    await release();
    const outcome = await appending;
    await settle(8);
    const both = (scene) => item('item-1', scene).members.map((m) => [m.member_id, m.visual_intent || '']);
    return { whileHeld, outcome, local: both(), server: both(await serverScene()),
      toasts: toasts.length };
    """, tmp_path, project=_retention_project())
    assert result["whileHeld"] == 1 and result["outcome"] == "ok"
    assert result["local"] == result["server"] == [
        ["member-a", "partial"], ["member-b", ""], ["member-c", ""]]
    assert result["toasts"] == 0


def test_the_timeline_delete_key_waits_for_an_unpainted_member_edit(tmp_path):
    """Its per-item guard names the members, which the answer is about to change."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    const [select] = cardAt(10).querySelectorAll('select');
    select.value = 'partial'; select.dispatch('change');
    await settle(2);
    w.selectedItems = [{ type: 'reference', id: 'item-1', data: item('item-1') }];
    const deleting = w._deleteSelectedItems();
    await settle(4);
    const whileHeld = sent.length;
    await release();
    await deleting;
    await settle(8);
    const bulk = sent.flat().find((op) => op.type === 'bulk_delete_items');
    return { whileHeld, guard: bulk?.items[0].expected.members.map((m) => m.visual_intent || ''),
      rows: ids(), server: ids(await serverScene()) };
    """, tmp_path, project=_retention_project())
    assert result == {"whileHeld": 1, "guard": ["partial", ""],
                      "rows": ["item-2", "item-4"], "server": ["item-2", "item-4"]}


def test_adoption_is_skipped_when_a_newer_paint_changed_the_members(tmp_path):
    """Plan test 9, fourth row: the answer is not written over a row something
    newer has painted; the scenes refresh converges instead."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold(2);
    const [select] = cardAt(10).querySelectorAll('select');
    select.value = 'partial'; select.dispatch('change');   // unpainted
    setStrength(50, 0.5);                                   // queued: the reconcile defers
    const row = item('item-1');
    row.members = [row.members[1], row.members[0]];          // a newer paint
    await release();
    const atSettle = { order: order(), retentions: retentions() };
    await release();
    await settle(12);
    return { atSettle, local: order(), server: order('item-1', await serverScene()),
      localRetention: retentions(), serverRetention: retentions('item-1', await serverScene()) };
    """, tmp_path, project=_retention_project())
    assert result["atSettle"] == {"order": ["member-b", "member-a"], "retentions": ["", "preserve"]}
    assert result["local"] == result["server"] == ["member-a", "member-b"]
    assert result["localRetention"] == result["serverRetention"] == ["partial", ""]


def test_the_invalid_role_autofocus_runs_on_a_lanes_first_draw_only(tmp_path):
    """Plan test 10: a redraw after an edit must not take the author's focus."""
    project = fixture_project()
    project.scenes[0].reference_lane_recipes[0].recipe["soft"]["role_fields"] = ["role"]
    result = run_item_panel(_MEMBERS + """
    w._promptContextCatalog = { schema_version: 1 };
    mount();
    await tick();
    const first = document.activeElement;
    const focusedInvalid = first?.dataset?.sonderInvalid === '1';
    first.blur();
    await w._writeReferenceItemFromPanel('item-2', { strength: 0.5 }, 'change reference strength');
    await settle(2); flushFrames(); await settle(2);
    return { focusedInvalid, redrawn: strengthOf(50).value,
      focusAfterRedraw: document.activeElement === document.body };
    """, tmp_path, project=project)
    assert result == {"focusedInvalid": True, "redrawn": "0.50", "focusAfterRedraw": True}


def test_the_picker_adds_a_member_to_an_existing_item_through_the_append(tmp_path):
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    memberButtons(50, '+ Add member')[0].dispatch('click');
    const pick = nodes().find((n) => n.tagName === 'BUTTON' && n.title === 'Stage this member'
      && n.textContent === 'Subject · A');
    pick.dispatch('click');
    const painted = order('item-2');
    const pickerClosed = !nodes().some((n) => n.title === 'Stage this member');
    await release(); await settle();
    return { painted, pickerClosed, server: order('item-2', await serverScene()),
      undo: w._undoStack.map((entry) => entry.label) };
    """, tmp_path)
    assert result == {"painted": ["member-c", "member-a"], "pickerClosed": True,
                      "server": ["member-c", "member-a"], "undo": ["add reference member"]}


def test_a_member_picked_from_a_stale_picker_is_refused_as_already_staged(tmp_path):
    """The picker's duplicate filter is draw-time; the append checks again when it
    acts, so a member staged on the item since the list was drawn is refused,
    not silently folded into a no-op."""
    result = run_item_panel(_MEMBERS + """
    mount();
    memberButtons(50, '+ Add member')[0].dispatch('click');
    const pick = nodes().find((n) => n.tagName === 'BUTTON' && n.title === 'Stage this member'
      && n.textContent === 'Subject · A');
    await w._appendReferenceMembers('item-2', { members: [drag('a')] });   // e.g. a timeline drop
    await settle();
    const sentBefore = sent.length;
    pick.dispatch('click');
    await settle(4);
    return { members: order('item-2'), extraSends: sent.length - sentBefore,
      messages: toasts.map((t) => t.message) };
    """, tmp_path)
    assert result["members"] == ["member-c", "member-a"] and result["extraSends"] == 0
    assert "That member is already staged on this Reference item." in result["messages"]


def test_an_append_refuses_a_duplicate_and_a_locked_lane_when_it_acts(tmp_path):
    """The drop resolver and the picker judged both when they were drawn."""
    result = run_item_panel(_MEMBERS + """
    mount();
    // One source folds into one notification, so read what it says after each.
    let shown = null;
    notes.subscribe((list) => {
      shown = list.find((n) => n.source === 'reference-stage-refused')?.message ?? shown; });
    w._redoStack = [{ label: 'undone', snapshot: {} }];
    const said = [];
    said.push([await w._appendReferenceMembers('item-1', { members: [drag('b')] }), shown]);
    said.push([await w._appendReferenceMembers('item-2', { members: [drag('a'), drag('a')] }), shown]);
    w.activeScene.reference_lane_configs[0].locked = true;
    w._buildTrackLayout();
    said.push([await w._appendReferenceMembers('item-1', { members: [drag('c')] }), shown]);
    return { said, sent: sent.length, undo: w._undoStack.length, redo: w._redoStack.length };
    """, tmp_path)
    duplicate = ["refused", "That member is already staged on this Reference item."]
    assert result["said"] == [duplicate, duplicate, [
        "refused", "This Reference lane is locked. Unlock it on the timeline header to edit."]]
    assert (result["sent"], result["undo"], result["redo"]) == (0, 0, 1)


def test_an_append_that_fails_keeps_a_held_panel_writes_paint(tmp_path):
    """Plan test 11: the rollback is local, per the members chain -- no refetch
    replaces the panel's strength paint queued behind the append."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold(2);
    const appending = w._appendReferenceMembers('item-1', { members: [drag('c')] });
    const painted = order();
    setStrength(10, 0.5);
    refuseNext('identity_mismatch');
    await release();                          // the append is refused
    const afterRefusal = { members: order(), strength: item('item-1').strength, gets: gets.length };
    await release();
    const outcome = await appending;
    await settle(8);
    return { painted, afterRefusal, outcome, strength: item('item-1').strength,
      server: { members: order('item-1', await serverScene()),
        strength: item('item-1', await serverScene()).strength },
      messages: toasts.map((t) => t.message) };
    """, tmp_path)
    assert result["painted"] == ["member-a", "member-b", "member-c"]
    assert result["afterRefusal"] == {"members": ["member-a", "member-b"], "strength": 0.5, "gets": 0}
    assert result["outcome"] == "failed" and result["strength"] == 0.5
    assert result["server"] == {"members": ["member-a", "member-b"], "strength": 0.5}
    assert result["messages"] == ["The Reference member could not be added."]


def test_an_append_refused_under_a_queued_reorder_returns_both_to_the_saved_list(tmp_path):
    """Plan test 3 for members: the reorder guards on the append's paint, so it is
    refused too, and the chain's last write puts the saved list back."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold(2);
    const appending = w._appendReferenceMembers('item-1', { members: [drag('c')] });
    const moving = w._editReferenceItemMembersFromPanel('item-1',
      { kind: 'move', memberId: 'member-c', direction: -1 }, 'reorder reference members');
    const painted = order();
    refuseNext('identity_mismatch');
    await release();
    await release();
    const outcomes = [await appending, await moving];
    await settle(10);
    return { painted, outcomes, local: order(), server: order('item-1', await serverScene()),
      undo: w._undoStack.length };
    """, tmp_path)
    assert result["painted"] == ["member-a", "member-c", "member-b"]
    assert result["outcomes"] == ["failed", "failed"]
    assert result["local"] == result["server"] == ["member-a", "member-b"]
    assert result["undo"] == 0


def test_an_unpainted_append_holds_a_member_edit_behind_it(tmp_path):
    """A prior this client cannot resolve (its asset list lags the project's)
    makes the append unpaintable: it installs the item's barrier, and a reorder
    made meanwhile waits and moves the member the answer brought in. (A stale
    `entity_id` alone does not: the append names `members`, and the paint
    re-derives it as the route does.)"""
    result = run_item_panel(_MEMBERS + """
    mount();
    const find = w._findAssetById.bind(w);
    w._findAssetById = (assetId) => assetId === 'asset-c' ? null : find(assetId);
    hold();
    const appending = w._appendReferenceMembers('item-2', { members: [drag('a')] });
    const duringWrite = { members: order('item-2'),
      barrier: w._referenceItemWriteState().barriers.has('item-2') };
    const moving = w._editReferenceItemMembersFromPanel('item-2',
      { kind: 'move', memberId: 'member-a', direction: -1 }, 'reorder reference members');
    await settle(4);
    const whileHeld = sent.length;
    await release();
    const outcomes = [await appending, await moving];
    await settle(8);
    return { duringWrite, whileHeld, outcomes, local: order('item-2'),
      server: order('item-2', await serverScene()) };
    """, tmp_path)
    assert result["duringWrite"] == {"members": ["member-c"], "barrier": True}
    assert result["whileHeld"] == 1 and result["outcomes"] == ["ok", "ok"]
    assert result["local"] == result["server"] == ["member-a", "member-c"]


def test_an_append_over_the_lane_cap_is_kept_and_said_out_loud(tmp_path):
    project = fixture_project()
    project.scenes[0].reference_lane_recipes[0].recipe["hard"]["max_members"] = 2
    result = run_item_panel(_MEMBERS + """
    mount();
    const outcome = await w._appendReferenceMembers('item-1', { members: [drag('c')] });
    await settle();
    return { outcome, server: order('item-1', await serverScene()),
      sources: toasts.map((t) => t.source) };
    """, tmp_path, project=project)
    assert result == {"outcome": "ok", "server": ["member-a", "member-b", "member-c"],
                      "sources": ["reference-stage-over-cap"]}


# -- Phase 5 audit ---------------------------------------------------------------------

def test_the_delete_key_deletes_what_was_selected_when_pressed_not_after_the_wait(tmp_path):
    """Audit finding 1: the author presses Delete on item-1, then selects item-2
    while item-1's unpainted member write settles."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    const [select] = cardAt(10).querySelectorAll('select');
    select.value = 'partial'; select.dispatch('change');
    await settle(2);
    w.selectedItems = [{ type: 'reference', id: 'item-1', data: item('item-1') }];
    const deleting = w._deleteSelectedItems();
    await settle(2);
    w.selectedItems = [{ type: 'reference', id: 'item-2', data: item('item-2') }];
    await release();
    await deleting;
    await settle(8);
    return { rows: ids(), server: ids(await serverScene()),
      selected: w.selectedItems.map((entry) => entry.id) };
    """, tmp_path, project=_retention_project())
    assert result == {"rows": ["item-2", "item-4"], "server": ["item-2", "item-4"],
                      "selected": ["item-2"]}


def test_an_append_pays_no_ordered_baseline_read_and_a_panel_edit_does(tmp_path):
    """Audit finding 2: the append keeps the timeline's exposure rather than a
    scene GET per drop; the panel's writers opt in (plan: three panel writers)."""
    result = run_item_panel(_MEMBERS + """
    mount();
    await w._appendReferenceMembers('item-2', { members: [drag('a')] });
    await settle(4);
    const afterAppend = sceneReads.length;
    await w._writeReferenceItemFromPanel('item-1', { strength: 0.5 }, 'change reference strength');
    await settle(4);
    return { afterAppend, afterPanelEdit: sceneReads.length };
    """, tmp_path)
    assert result == {"afterAppend": 0, "afterPanelEdit": 1}


def test_clearing_a_saved_retention_is_saved(tmp_path):
    """Audit finding 3 of the paint-first plan, closed by backlog step 1: the
    save no longer restores a member field a write cleared, so the panel sends
    the clear and the server keeps it (`test_overlay_modeled_keys.py`)."""
    result = run_item_panel(_MEMBERS + """
    mount();
    const [select] = cardAt(10).querySelectorAll('select');
    select.focus();
    select.value = ''; select.dispatch('change');
    await settle(8);
    const member = (row) => row.members.find((value) => value.member_id === 'member-a');
    return { shown: select.value, sent: sent.length, undo: w._undoStack.length,
      painted: 'visual_intent' in member(item('item-1')),
      stored: 'visual_intent' in member(item('item-1', await serverScene())),
      messages: toasts.map((t) => t.message) };
    """, tmp_path, project=_retention_project())
    assert result == {"shown": "", "sent": 1, "undo": 1, "painted": False,
                      "stored": False, "messages": []}


def test_an_edit_of_a_member_no_longer_on_the_item_is_refused_with_a_message(tmp_path):
    """Audit finding 8."""
    result = run_item_panel(_MEMBERS + """
    mount();
    const outcome = await w._editReferenceItemMembersFromPanel('item-2',
      { kind: 'remove', memberId: 'member-a' }, 'remove reference member');
    return { outcome, sent: sent.length, messages: toasts.map((t) => t.message) };
    """, tmp_path)
    assert result == {"outcome": "refused", "sent": 0,
                      "messages": ["That member is no longer on this Reference item."]}


def test_a_barrier_is_released_even_when_the_settle_throws(tmp_path):
    """Audit finding 4: a later member edit on the item must not wait forever."""
    result = run_item_panel(_MEMBERS + """
    mount();
    const reconcile = w._reconcileActiveSceneFromMutation.bind(w);
    let throwOnce = true;
    w._reconcileActiveSceneFromMutation = (...args) => {
      if (throwOnce) { throwOnce = false; throw new Error('reconcile failed'); }
      return reconcile(...args);
    };
    const first = w._editReferenceItemMembersFromPanel('item-1',
      { kind: 'patch', memberId: 'member-a', patch: { visual_intent: 'partial' } }, 'change visual retention')
      .then(() => 'settled', () => 'threw');
    const second = w._editReferenceItemMembersFromPanel('item-1',
      { kind: 'move', memberId: 'member-b', direction: -1 }, 'reorder reference members');
    const firstOutcome = await first;
    const secondOutcome = await Promise.race([second, new Promise((r) => setTimeout(() => r('stuck'), 3000))]);
    return { firstOutcome, secondOutcome, barriers: w._referenceItemWriteState().barriers.size };
    """, tmp_path, project=_retention_project())
    # The second is released and settles; its guard read a row the thrown settle
    # never adopted the answer onto, so the route refuses it -- which is the
    # point: an answer, not a wait that never ends.
    assert result == {"firstOutcome": "threw", "secondOutcome": "failed", "barriers": 0}


def test_the_invalid_role_autofocus_waits_for_a_draw_that_can_judge_a_role(tmp_path):
    """Audit finding 5: a lane first drawn while the Prompt Format catalog loads
    still gets its one autofocus when the catalog arrives -- and only one."""
    project = fixture_project()
    project.scenes[0].reference_lane_recipes[0].recipe["soft"]["role_fields"] = ["role"]
    result = run_item_panel(_MEMBERS + """
    mount();
    await tick();
    const whileLoading = document.activeElement === document.body;
    w._promptContextCatalog = { schema_version: 1 };
    handle.refresh();
    await tick();
    const focused = document.activeElement?.dataset?.sonderInvalid === '1';
    document.activeElement.blur();
    handle.refresh();
    await tick();
    return { whileLoading, focused, again: document.activeElement !== document.body };
    """, tmp_path, project=project)
    assert result == {"whileLoading": True, "focused": True, "again": False}


def test_a_newer_change_of_the_same_select_decides_what_it_shows(tmp_path):
    """Audit finding 6: the first change is refused while a second change of the
    same select waits behind it; the refusal must not put back a value over the
    second one, which then lands."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    const [select] = cardAt(10).querySelectorAll('select');
    select.focus();
    select.value = 'partial'; select.dispatch('change');
    refuseNext('unsupported_reference_member_field');
    await settle(2);
    select.value = 'transfer_attributes'; select.dispatch('change');
    await release();
    await settle(10);
    return { shown: select.value, local: retentions()[0], server: retentions('item-1', await serverScene())[0] };
    """, tmp_path, project=_retention_project())
    assert result == {"shown": "transfer_attributes", "local": "transfer_attributes",
                      "server": "transfer_attributes"}


# -- Timeline Lock Menus Phase 4: Delete Items in Lane on a Reference lane --------
# Plan: memory/plans/timeline-lock-menus.md. Every non-clip row used to go out
# as "audio", so the route answered "Audio track not found" for every
# Reference lane.

def test_delete_items_in_a_reference_lane_deletes_through_the_route(tmp_path):
    """The lane's rows go out as Reference rows carrying the identity the route
    requires, and only that lane is emptied."""
    result = run_item_panel(_FIELDS + """
    globalThis.confirm = () => true;
    await w._deleteItemsInLaneWithinGesture('reference', 0);
    await settle(8);
    const bulk = sent.flat().find((op) => op.type === 'bulk_delete_items');
    return { types: bulk.items.map((row) => row.type), ids: bulk.items.map((row) => row.id),
      guarded: bulk.items.every((row) => row.expected?.reference_item_id === row.id && row.preserve_lane),
      rows: ids(), server: ids(await serverScene()), undo: w._undoStack.length };
    """, tmp_path)
    assert result == {"types": ["reference", "reference"], "ids": ["item-1", "item-2"],
                      "guarded": True, "rows": ["item-4"], "server": ["item-4"], "undo": 1}


def test_delete_items_in_a_reference_lane_waits_for_an_unpainted_member_edit(tmp_path):
    """Its per-item guard names the members, which the held answer is about to
    change: it waits, then reads the rows after it -- as the Delete key does."""
    result = run_item_panel(_MEMBERS + """
    globalThis.confirm = () => true;
    mount();
    hold();
    const [select] = cardAt(10).querySelectorAll('select');
    select.value = 'partial'; select.dispatch('change');
    await settle(2);
    const deleting = w._deleteItemsInLaneWithinGesture('reference', 0);
    await settle(4);
    const whileHeld = sent.length;
    await release();
    await deleting;
    await settle(8);
    const bulk = sent.flat().find((op) => op.type === 'bulk_delete_items');
    const first = bulk?.items.find((row) => row.id === 'item-1');
    return { whileHeld, guard: first?.expected.members.map((m) => m.visual_intent || ''),
      rows: ids(), server: ids(await serverScene()) };
    """, tmp_path, project=_retention_project())
    assert result == {"whileHeld": 1, "guard": ["partial", ""],
                      "rows": ["item-4"], "server": ["item-4"]}


# Phase 4 audit: what the wait must re-read. Each holds item-1's unpainted member
# edit, starts the lane delete behind it, changes the world, then releases.
_LANE_DELETE_BEHIND_A_HELD_EDIT = _MEMBERS + """
globalThis.confirm = () => true;
mount();
hold();
const [select] = cardAt(10).querySelectorAll('select');
select.value = 'partial'; select.dispatch('change');
await settle(2);
const undoBefore = w._undoStack.length;
const deleting = w._deleteItemsInLaneWithinGesture('reference', 0);
await settle(4);
"""


def _three_lane_retention_project():
    """`_retention_project` plus a second image lane (index 2) to move into."""
    project = _retention_project()
    scene = project.scenes[0]
    scene.reference_lane_count = 3
    scene.reference_lane_configs.append(LaneConfig())
    scene.reference_lane_recipes.append(ReferenceLaneRecipe(
        lane_id="lane-image-2", media_kind="image",
        recipe={"hard": {"assembly": "batch"}, "soft": {"physical_population": "pictures"}}))
    return project


def test_a_lane_delete_skips_a_row_moved_off_the_lane_during_the_wait(tmp_path):
    result = run_item_panel(_LANE_DELETE_BEHIND_A_HELD_EDIT + """
    // Another writer moves item-2 to lane 2 while item-1's answer is held.
    const moved = await ask({ sceneId: 'scene', operations: [{ type: 'update_reference_item',
      reference_item_id: 'item-2', fields: { lane_index: 2 }, expected: { lane_index: 0 } }] });
    await release();
    await deleting;
    await settle(8);
    const bulk = sent.flat().find((op) => op.type === 'bulk_delete_items');
    return { moved: moved.ok, deleted: bulk?.items.map((row) => row.id),
      server: ids(await serverScene()) };
    """, tmp_path, project=_three_lane_retention_project())
    assert result == {"moved": True, "deleted": ["item-1"], "server": ["item-2", "item-4"]}


def test_a_lane_locked_during_the_wait_refuses_before_the_paint(tmp_path):
    result = run_item_panel(_LANE_DELETE_BEHIND_A_HELD_EDIT + """
    // The author locks the lane while item-1's answer is held, through the
    // header's own lock path (its write queues behind the held one).
    w._pushUndo('toggle track lock');
    w._trackLayout[0].locked = true;
    w._saveLaneConfig([w._trackLayout[0]]);
    await release();
    await deleting;
    await settle(8);
    return { sent: sent.flat().some((op) => op.type === 'bulk_delete_items'),
      undo: w._undoStack.map((entry) => entry.label).slice(undoBefore), rows: ids(),
      locked: (await serverScene()).reference_lane_configs[0].locked,
      toast: toasts.some((t) => t.message === 'Lane is locked.') };
    """, tmp_path, project=_retention_project())
    # Only the lock's own Undo step: the delete added none and sent nothing.
    assert result == {"sent": False, "undo": ["toggle track lock"], "locked": True,
                      "rows": ["item-1", "item-2", "item-4"], "toast": True}


def test_a_project_or_scene_switch_during_the_wait_sends_nothing(tmp_path):
    for switch in ("w.projectDir = 'elsewhere';", "w.activeSceneId = 'another-scene';"):
        result = run_item_panel(_LANE_DELETE_BEHIND_A_HELD_EDIT + switch + """
        await release();
        await deleting;
        await settle(8);
        return { sent: sent.flat().some((op) => op.type === 'bulk_delete_items'),
          undo: w._undoStack.length - undoBefore };
        """, tmp_path, project=_retention_project())
        assert result == {"sent": False, "undo": 0}, switch


# --- Backlog step 1, Phase 6: timeline gestures and an unpainted write (D5) ----

# A retention edit on item-1 the mirror cannot paint, held: its barrier is up.
_HELD_UNPAINTED_EDIT = _MEMBERS + """
mount();
hold();
const [retentionSelect] = cardAt(10).querySelectorAll('select');
retentionSelect.value = 'partial'; retentionSelect.dispatch('change');
await settle(2);
const lane0Rows = (scene) => (scene.reference_items || []).filter((row) => row.lane_index === 0)
  .sort((left, right) => left.start_frame - right.start_frame)
  .map((row) => [row.start_frame, row.end_frame, row.members.map((m) => m.visual_intent || '')]);
"""


def test_a_split_waits_for_an_unpainted_member_edit_and_cuts_the_answered_row(tmp_path):
    """D5: a split waits for the item's unsettled member writes, including one
    parked behind the first, so it is sent after every edit made while the item
    was whole and both halves carry them (audit #1: without the wait the parked
    edit went after the split and reached only the left half)."""
    result = run_item_panel(_HELD_UNPAINTED_EDIT + """
    const parked = w._editReferenceItemMembersFromPanel('item-1',
      { kind: 'patch', memberId: 'member-b', patch: { visual_intent: 'partial' } }, 'change visual retention');
    await settle(2);
    const splitting = w._splitItemsAtFrame([{ type: 'reference', id: 'item-1', data: item('item-1') }], 25);
    await settle(4);
    const whileHeld = lane0Rows(w.activeScene);
    await release();
    await parked; await splitting;
    await settle(10);
    return { whileHeld, local: lane0Rows(w.activeScene), server: lane0Rows(await serverScene()),
      toasts: toasts.map((t) => t.message) };
    """, tmp_path, project=_retention_project())
    assert result["whileHeld"] == [[10, 40, ["preserve", ""]], [50, 70, [""]]]
    assert result["local"] == result["server"] == [
        [10, 25, ["partial", "partial"]], [25, 40, ["partial", "partial"]], [50, 70, [""]]]
    assert result["toasts"] == []


def test_a_split_after_an_unpainted_append_snapshots_the_appended_member(tmp_path):
    """Audit #2: the split's Undo entry is pushed after the wait, so its
    before-state holds the member the append added, and Undo of the split does
    not take it away."""
    result = run_item_panel(_MEMBERS + """
    mount();
    hold();
    // An authored retention: the mirror cannot paint it, so the append is sent
    // unpainted behind a barrier.
    const appending = w._appendReferenceMembers('item-1', { members: [{ ...drag('c'), visual_intent: 'partial' }] });
    const barrier = w._referenceItemWriteState().barriers.has('item-1');
    await settle(2);
    const splitting = w._splitItemsAtFrame([{ type: 'reference', id: 'item-1', data: item('item-1') }], 25);
    await settle(4);
    await release();
    await appending; await splitting;
    await settle(10);
    const entry = w._undoStack.find((candidate) => /split/.test(candidate.label));
    const snapshotRow = entry?.snapshot?.reference_items?.find((row) => row.reference_item_id === 'item-1');
    return { barrier, snapshot: snapshotRow?.members?.map((m) => m.member_id),
      server: item('item-1', await serverScene()).members.map((m) => m.member_id) };
    """, tmp_path, project=_retention_project())
    assert result["barrier"] is True
    assert result["server"] == ["member-a", "member-b", "member-c"]
    assert result["snapshot"] == result["server"]


def test_the_item_editor_move_waits_for_an_unpainted_member_edit(tmp_path):
    """D5: the Start field's move reads its guard from the row after the answer."""
    result = run_item_panel(_HELD_UNPAINTED_EDIT + """
    const moving = w._moveItemToFrame('reference', 'item-1', item('item-1'), 12);
    await settle(4);
    const whileHeld = lane0Rows(w.activeScene)[0];
    await release();
    await moving;
    await settle(10);
    return { whileHeld, local: lane0Rows(w.activeScene)[0], server: lane0Rows(await serverScene())[0],
      toasts: toasts.map((t) => t.message) };
    """, tmp_path, project=_retention_project())
    assert result["whileHeld"] == [10, 40, ["preserve", ""]]
    assert result["local"] == result["server"] == [12, 42, ["partial", ""]]
    assert result["toasts"] == []


def test_a_move_after_the_wait_paints_the_row_the_scene_now_holds(tmp_path):
    """A scenes read may replace the scene object while the move waits; the
    move then acts on the live row, not the one the item editor was built on."""
    result = run_item_panel(_HELD_UNPAINTED_EDIT + """
    hold();  // the move's own write, after the wait
    const moving = w._moveItemToFrame('reference', 'item-1', item('item-1'), 12);
    await settle(2);
    const replaced = structuredClone(w.activeScene);
    w.scenes = (w.scenes || []).map((scene) => (scene === w.activeScene ? replaced : scene));
    w.activeScene = replaced;
    await release();
    await settle(4);
    const paintedWhileSaving = lane0Rows(w.activeScene)[0].slice(0, 2);
    await release();
    await moving;
    await settle(8);
    return { paintedWhileSaving, server: lane0Rows(await serverScene())[0].slice(0, 2) };
    """, tmp_path, project=_retention_project())
    assert result == {"paintedWhileSaving": [12, 42], "server": [12, 42]}


def test_a_move_after_a_wait_closes_the_item_editor_built_on_the_old_row(tmp_path):
    """Audit #3: the reconcile of the answer may replace the row the open item
    editor was built on; a second Start from it would guard a stale range."""
    result = run_item_panel(_HELD_UNPAINTED_EDIT + """
    hold();  // the move's own write
    let hidden = 0;
    const realHide = w._hideItemEditor.bind(w);
    w._hideItemEditor = () => { hidden += 1; return realHide(); };
    const moving = w._moveItemToFrame('reference', 'item-1', item('item-1'), 12);
    await settle(2);
    const beforeAnswer = hidden;
    await release();
    await settle(4);
    const whileMoveSaves = hidden;
    await release();
    await moving;
    return { beforeAnswer, whileMoveSaves };
    """, tmp_path, project=_retention_project())
    assert result["beforeAnswer"] == 0 and result["whileMoveSaves"] >= 1


def test_a_move_abandoned_by_a_scene_switch_during_the_wait_sends_nothing(tmp_path):
    for gesture in ("w._moveItemToFrame('reference', 'item-1', item('item-1'), 12)",):
        result = run_item_panel(_HELD_UNPAINTED_EDIT + f"""
        const pending = {gesture};
        await settle(2);
        w.activeSceneId = 'another-scene';
        await release();
        await pending;
        await settle(8);
        return {{ sent: sent.flat().filter((op) => Object.hasOwn(op.fields || {{}}, 'start_frame')).length,
          undo: w._undoStack.filter((entry) => /split|move item/.test(entry.label)).length }};
        """, tmp_path, project=_retention_project())
        assert result == {"sent": 0, "undo": 0}, gesture


def test_a_drag_or_trim_of_an_item_still_saving_does_not_start(tmp_path):
    """D5: they name only the range, but the answer may still replace the row
    they would drag; they say why instead of waiting under the pointer."""
    result = run_item_panel(_HELD_UNPAINTED_EDIT + """
    const saving = w._referenceItemSavingRefusal([{ type: 'reference', id: 'item-1' }]);
    const other = w._referenceItemSavingRefusal([{ type: 'reference', id: 'item-2' }]);
    const notReference = w._referenceItemSavingRefusal([{ type: 'clip', id: 'item-1' }]);
    const trimRefused = w._refuseReferenceItemSaving([{ type: 'reference', id: 'item-1' }]);
    await release();
    await settle(8);
    return { saving, other, notReference, trimRefused,
      after: w._referenceItemSavingRefusal([{ type: 'reference', id: 'item-1' }]),
      toasts: toasts.map((t) => [t.message, t.source]) };
    """, tmp_path, project=_retention_project())
    message = "This Reference item is still saving. Try again in a moment."
    assert result["saving"] == message
    assert result["other"] == "" and result["notReference"] == "" and result["after"] == ""
    assert result["trimRefused"] is True
    assert [message, "reference-item-saving"] in result["toasts"]


def test_the_timeline_pointer_handlers_ask_whether_the_item_is_saving():
    """The mousedown handler is inline in `_setupTimelineEvents`; pinned lexically."""
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    start = source.index("    _setupTimelineEvents(")
    body = source[start:source.index("\n    }\n", start)]
    trim = body.index("this._refusePastEndReferenceTrim(edgeHit)")
    assert body.index("this._refuseReferenceItemSaving([edgeHit])", trim) < body.index(
        'this._pushUndo("trim")', trim), "a trim of a saving item must refuse before its Undo step"
    assert "this._referenceItemSavingRefusal(this.selectedItems)" in body
    assert "this._dragRefusal.source" in body
