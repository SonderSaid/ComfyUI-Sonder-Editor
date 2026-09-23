"""Management panels act on what they drew, not on whatever now sits at its index.

The Prompt panel, Guide popup and Reference Lane panel are not repainted when
Undo or a refetch replaces the scene beneath them, and each addressed its rows
by list index or frame. Panel Delete removed the section Undo had just
restored; the guide popup's strength landed on another guide — both with a 200
and no message. These drive the real host methods with the panel's identity.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_project_mutation_queue import _run_gesture_node

ROOT = Path(__file__).resolve().parents[1]

_PROMPT_HOST = """
        const w = makeWidget(), sent = [];
        let undos = 0, refreshes = 0;
        w._pushUndo = () => { undos += 1; return { label: 'x' }; };
        w._isPromptTrackLocked = () => false;
        w._hidePromptEditor = () => {};
        w._promptPanelHandle = { refresh: () => { refreshes += 1; } };
        w._channelTemplate = () => ({ channels: [] });
        w._takePromptIdentityCreateIntents = () => [];
        w._adoptPromptIdentitiesFromMutation = () => {};
        w._finalizePromptIdentityCreationHistory = () => {};
        w._refreshPromptContextDependencyConsumers = () => {};
        w._runSceneMutation = async (operations, options) => {
            sent.push({ operations: structuredClone(operations), key: options.key });
            return { payload: {} };
        };
        const section = (id, start, end, extra = {}) => ({ prompt_id: id,
            start_frame: start, end_frame: end, prompt: '', muted: false,
            channels: {}, channel_docs: {}, attachments: [],
            global_channel_exceptions: [], ...extra });
"""


def test_panel_delete_resolves_the_drawn_section_by_id_and_guards_on_the_id_only():
    _run_gesture_node(_PROMPT_HOST + """
        // The panel drew B at index 1; Undo has since removed A, so B is at 0
        // and index 1 now holds C.
        w.activeScene = { scene_id: 'scene',
            prompt_sections: [section('b', 10, 20), section('c', 20, 30)] };
        assert.equal(await w._deletePromptSection(1, { promptId: 'b' }), true);
        assert.equal(undos, 1);
        assert.deepEqual(sent[0].operations,
            [{ type: 'delete_prompt_section', index: 0, expected: { prompt_id: 'b' } }]);
        assert.ok(sent[0].key.includes(':b:delete'));
        assert.deepEqual(w.activeScene.prompt_sections.map(s => s.prompt_id), ['c']);
    """)


def test_panel_delete_of_a_vanished_section_refuses_without_undo_or_write():
    _run_gesture_node(_PROMPT_HOST + """
        w.activeScene = { scene_id: 'scene', prompt_sections: [section('c', 20, 30)] };
        assert.equal(await w._deletePromptSection(0, { promptId: 'gone' }), false);
        assert.equal(undos, 0);
        assert.equal(sent.length, 0);
        assert.equal(refreshes, 1);
        assert.deepEqual(w.activeScene.prompt_sections.map(s => s.prompt_id), ['c']);
    """)


def test_timeline_delete_keeps_its_index_and_full_current_row_guard():
    _run_gesture_node(_PROMPT_HOST + """
        w.activeScene = { scene_id: 'scene', prompt_sections: [section('a', 0, 10)] };
        await w._deletePromptSection(0);
        assert.deepEqual(sent[0].operations[0].expected,
            { prompt_id: 'a', start_frame: 0, end_frame: 10 });
    """)


def test_back_to_back_panel_range_commits_do_not_refuse_each_other():
    _run_gesture_node(_PROMPT_HOST + """
        w.activeScene = { scene_id: 'scene', prompt_sections: [section('a', 0, 10)] };
        // Start, then End, both from a panel that has not repainted between.
        await w._updatePromptSection(0, { start_frame: 2, end_frame: 10 }, { promptId: 'a' });
        await w._updatePromptSection(0, { start_frame: 2, end_frame: 14 }, { promptId: 'a' });
        const [first, second] = sent.map(value => value.operations.at(-1));
        assert.equal(first.expected.start_frame, 0);
        // The second describes the optimistic row the first painted, which is
        // what the server holds once the first write lands ahead of it.
        assert.equal(second.expected.start_frame, 2);
        assert.equal(second.expected.end_frame, 10);
        assert.equal(second.expected.prompt_id, 'a');
    """)


def test_panel_range_commit_targets_the_drawn_section_after_a_reorder():
    _run_gesture_node(_PROMPT_HOST + """
        w.activeScene = { scene_id: 'scene',
            prompt_sections: [section('x', 0, 5), section('a', 10, 20)] };
        await w._updatePromptSection(0, { start_frame: 11, end_frame: 20 }, { promptId: 'a' });
        const operation = sent[0].operations.at(-1);
        assert.equal(operation.index, 1);
        assert.equal(operation.expected.prompt_id, 'a');
        assert.equal(w.activeScene.prompt_sections[0].start_frame, 0);
    """)


def test_panel_update_of_a_vanished_section_warns_and_repaints():
    _run_gesture_node(_PROMPT_HOST + """
        w.activeScene = { scene_id: 'scene', prompt_sections: [section('x', 0, 5)] };
        const result = await w._updatePromptSection(0, { start_frame: 1, end_frame: 5 },
            { promptId: 'gone' });
        assert.equal(result, false);
        assert.equal(sent.length, 0);
        assert.equal(undos, 0);
        assert.equal(refreshes, 1);
    """)


def test_global_inherit_is_computed_from_the_id_resolved_row():
    _run_gesture_node(_PROMPT_HOST + """
        w.activeScene = { scene_id: 'scene', prompt_sections: [
            section('x', 0, 5),
            section('y', 10, 20, { global_channel_exceptions: ['visual'] })] };
        // The panel drew Y at index 0. Y already opts out of `visual`: a no-op,
        // where index 0 (X) would have written an opt-out onto X.
        await w._setSectionGlobalInherit(0, 'visual', false, { promptId: 'y' });
        assert.equal(sent.length, 0);
        await w._setSectionGlobalInherit(0, 'visual', true, { promptId: 'y' });
        const operation = sent[0].operations.at(-1);
        assert.equal(operation.index, 1);
        assert.deepEqual(operation.fields.global_channel_exceptions, []);
        assert.deepEqual(operation.expected.global_channel_exceptions, ['visual']);
    """)


def test_add_after_anchors_on_the_drawn_section():
    _run_gesture_node(_PROMPT_HOST + """
        const created = [];
        w._saveNewPromptSection = async (start, end) => { created.push([start, end]); };
        w.totalFrames = 100;
        w.activeScene = { scene_id: 'scene', duration_frames: 100, prompt_sections: [
            section('x', 0, 5), section('a', 10, 20), section('z', 40, 50)] };
        assert.equal(await w._addPromptSectionAfter(0, { promptId: 'a' }), true);
        assert.deepEqual(created, [[20, 40]]);
    """)


_GUIDE_HOST = """
        const w = makeWidget(), sent = [];
        w._runSceneMutation = async (operations, options) => {
            sent.push({ operations: structuredClone(operations), key: options.key });
            return { payload: {} };
        };
        w._isGuideTrackLocked = () => false;
        w._clearSelection = () => {}; w._hideItemEditor = () => {};
        w._remapSelectedItem = () => {}; w._pruneLocalLinkedGroups = () => {};
        w._shouldApplyLinked = () => true;
        w._expandItemsWithLinked = (items) => items;
        w._isItemLocked = () => false;
        const guide = (id, frame, extra = {}) => ({ guide_id: id, frame_index: frame,
            asset_id: `asset-${id}`, strength: 1, muted: false, ...extra });
"""


def test_a_guide_write_carries_the_callers_snapshot_not_the_frames_occupant():
    _run_gesture_node(_GUIDE_HOST + """
        // The popup drew g1 at frame 10; g2 now sits there.
        const shown = guide('g1', 10);
        const occupant = guide('g2', 10);
        w.activeScene = { scene_id: 'scene', guide_frames: [occupant] };
        w._findSceneItemBySelection = () => ({ type: 'guide', id: 10, data: occupant });
        assert.equal(w._guideIdentityForAction(shown), null);
        await w._updateItemProperty('guide', 10, { muted: true },
            { refresh: false, expected: w._guideSnapshotIdentity(shown) });
        assert.deepEqual(sent[0].operations[0].expected,
            { guide_id: 'g1', frame_index: 10, asset_id: 'asset-g1' });
        // Nothing painted onto the guide the popup never showed.
        assert.equal(occupant.muted, false);
        assert.equal(sent[0].operations[0].apply_linked, false);
    """)


def test_a_matching_guide_snapshot_still_paints_its_linked_mute():
    _run_gesture_node(_GUIDE_HOST + """
        const occupant = guide('g1', 10);
        w.activeScene = { scene_id: 'scene', guide_frames: [occupant] };
        w._findSceneItemBySelection = () => ({ type: 'guide', id: 10, data: occupant });
        const identity = w._guideIdentityForAction(occupant);
        assert.deepEqual(identity, { guide_id: 'g1', frame_index: 10, asset_id: 'asset-g1' });
        await w._updateItemProperty('guide', 10, { muted: true },
            { refresh: false, expected: identity });
        assert.equal(occupant.muted, true);
        assert.equal(sent[0].operations[0].apply_linked, true);
    """)


def test_an_id_less_legacy_guide_omits_the_empty_keys():
    _run_gesture_node(_GUIDE_HOST + """
        assert.deepEqual(w._guideSnapshotIdentity({ guide_id: '', frame_index: 4, asset_id: '' }),
            { frame_index: 4 });
    """)


def test_local_guide_delete_and_move_skip_a_guide_the_caller_did_not_show():
    _run_gesture_node(_GUIDE_HOST + """
        const occupant = guide('g2', 10);
        w.activeScene = { scene_id: 'scene', guide_frames: [occupant] };
        w._applyLocalBulkDeleteItems([{ type: 'guide', id: 10,
            expected: { guide_id: 'g1', frame_index: 10 } }]);
        assert.deepEqual(w.activeScene.guide_frames.map(g => g.guide_id), ['g2']);
        await w._moveGuideToFrame(guide('g1', 10), 30);
        assert.deepEqual(w.activeScene.guide_frames.map(g => [g.guide_id, g.frame_index]),
            [['g2', 10]]);
        assert.equal(sent[0].operations[0].expected.guide_id, 'g1');
        // The guide it did show is still deleted.
        w._applyLocalBulkDeleteItems([{ type: 'guide', id: 10,
            expected: { guide_id: 'g2', frame_index: 10 } }]);
        assert.deepEqual(w.activeScene.guide_frames, []);
    """)


def test_a_reference_recipe_save_guards_on_the_lane_the_panel_drew():
    _run_gesture_node("""
        const w = makeWidget(), sent = [];
        w._runSceneMutation = async (operations) => { sent.push(structuredClone(operations)); };
        w._defaultReferenceLaneRecipe = () => ({ lane_id: '', recipe: {} });
        w._defaultLaneConfig = () => ({});
        w.activeScene = { scene_id: 'scene', reference_lane_count: 2,
            reference_lane_configs: [{}, {}],
            reference_lane_recipes: [{ lane_id: 'L2', recipe: { name: 'two' } },
                                     { lane_id: 'L1', recipe: { name: 'one' } }] };
        const entry = { type: 'reference', laneIndex: 0, customName: 'Ref',
            referenceRecipe: { lane_id: 'L1', recipe: { name: 'edited' } } };
        await w._saveLaneConfig([entry], { expectedLaneId: 'L1' });
        assert.deepEqual(sent[0][0].expected, { lane_id: 'L1' });
        // Lane L2 took index 0: nothing painted onto it.
        assert.deepEqual(w.activeScene.reference_lane_recipes[0],
            { lane_id: 'L2', recipe: { name: 'two' } });
        await w._saveLaneConfig([{ ...entry }], { expectedLaneId: 'L2' });
        assert.deepEqual(sent[1][0].expected, { lane_id: 'L2' });
        assert.equal(w.activeScene.reference_lane_recipes[0].recipe.name, 'edited');
    """)


def test_only_undo_and_redo_reach_the_timeline_under_a_management_modal():
    _run_gesture_node("""
        window.addEventListener = () => {}; window.removeEventListener = () => {};
        globalThis.Element = class {};
        globalThis.document = { activeElement: null,
            addEventListener() {}, removeEventListener() {} };
        const w = makeWidget();
        w.isFullscreen = true; w.selectedItems = [{ type: 'clip', id: 'c' }];
        let deletes = 0, undos = 0, plays = 0;
        w._deleteSelectedItems = () => { deletes += 1; };
        w._undo = async () => { undos += 1; };
        w._togglePlayback = () => { plays += 1; };
        w._keyboardConsumerId = (name) => `test-${name}`;
        w._setupKeyboardEvents();
        const key = (k, extra = {}) => w._editorKeyConsumer({ key: k, ctrlKey: false,
            metaKey: false, shiftKey: false, target: null, ...extra });
        let open = true;
        w._promptPanelHandle = { isMounted: () => open };
        const underModal = [key('Delete'), key(' '), key('z', { ctrlKey: true })];
        assert.equal(deletes, 0);
        assert.equal(plays, 0);
        assert.equal(undos, 1);
        assert.equal(typeof underModal[0], 'symbol');
        assert.equal(underModal[2], true);
        open = false;
        assert.equal(key('Delete'), true);
        assert.equal(deletes, 1);
    """)


def test_reference_panel_refresh_follows_its_lane_or_closes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for mounted Reference panel coverage")
    module_url = (ROOT / "web" / "js" / "editor_reference_panel.js").as_uri()
    script = """
class N {
  constructor(tag) { this.tagName=String(tag).toUpperCase(); this.children=[];
    this.options=[]; this.style={cssText:""}; this.dataset={}; this.attributes={};
    this.value=""; this.textContent=""; this.title=""; this.disabled=false;
    this.checked=false; this.open=false; this._handlers={}; }
  appendChild(c) { if(!c?.tagName) return c; this.children.push(c); c.parentElement=this;
    if(c.tagName==="OPTION") this.options.push(c); return c; }
  append(...cs) { cs.forEach((c)=>this.appendChild(c)); }
  addEventListener(t,h) { (this._handlers[t] ||= []).push(h); }
  removeEventListener() {}
  setAttribute(k,v) { this.attributes[k]=String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  focus() {}
  remove() { if(this.parentElement) this.parentElement.children=
    this.parentElement.children.filter((c)=>c!==this); }
}
globalThis.document={createElement:(t)=>new N(t),body:new N("body"),activeElement:null};
globalThis.window={addEventListener(){},removeEventListener(){},localStorage:null};
globalThis.localStorage={getItem(){return null;},setItem(){}};
const mod=await import(__MODULE_URL__);
const recipe=(id)=>({lane_id:id,recipe_id:"",media_kind:"image",recipe:{}});
const host={
  projectId:"project",activeSceneId:"scene",totalFrames:20,playhead:0,
  _trackLayout:[{type:"reference",laneIndex:0},{type:"reference",laneIndex:1}],
  activeScene:{reference_lane_count:2,duration_frames:20,
    reference_lane_configs:[{},{}],reference_lane_recipes:[recipe("A"),recipe("B")],
    reference_items:[]},
  _referenceRecipePresets:[],_customReferenceRecipes:[],_referenceRecipeFieldSchema:[],
  _promptContextProfiles:[],_promptContextCatalog:{},
  _defaultReferenceLaneRecipe:()=>recipe(""),
  _referenceMemberForRef:()=>null,_findAssetById:()=>null,
  _referenceAssetPreviewUrl:()=>null,_referenceLaneAdvisories:()=>[],
  _isLaneLocked:()=>false,_channelTemplate:()=>({}),
};
const handle=mod.mountReferenceLanePanel(host,{laneIndex:1});
const drawn=handle.laneIndex;
// An Undo swaps the lanes: B is now at index 0.
host.activeScene.reference_lane_recipes=[recipe("B"),recipe("A")];
handle.refresh();
const followed=handle.laneIndex;
// A later refetch drops lane B entirely.
host.activeScene.reference_lane_recipes=[recipe("A")];
host.activeScene.reference_lane_count=1;
host._trackLayout=[{type:"reference",laneIndex:0}];
const stillOpen=handle.refresh();
console.log(JSON.stringify({drawn,followed,stillOpen,
  handleCleared:host._referencePanelHandle===null}));
""".replace("__MODULE_URL__", json.dumps(module_url))
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {"drawn": 1, "followed": 0, "stillOpen": False,
                      "handleCleared": True}


# -- The panels' own wiring. The tests above hand the identity to the host
# directly; these prove each surface actually passes it, so reverting a panel
# call site fails here rather than nowhere.

def test_every_prompt_panel_section_action_passes_the_drawn_prompt_id():
    source = (ROOT / "web/js/editor_prompt_panel.js").read_text(encoding="utf-8")
    calls = {
        "range commit": "host._updatePromptSection(idx, { start_frame: start, end_frame: end },\n"
                        "                    { promptId: sectionBase.prompt_id || \"\" })",
        "add after": "host._addPromptSectionAfter(idx,\n"
                     "                    { promptId: sectionBase.prompt_id || \"\" })",
        "delete": "host._deletePromptSection(idx, { promptId: sectionBase.prompt_id || \"\" })",
        "global inherit": "host._setSectionGlobalInherit(idx, key, box.checked,\n"
                          "                            { promptId: sectionBase.prompt_id || \"\" })",
        "convert link copy": "}, { promptId: sectionBase.prompt_id || \"\" });",
    }
    normalized = source.replace("\r\n", "\n")
    missing = [name for name, call in calls.items() if call not in normalized]
    assert not missing, f"panel section actions no longer pass the drawn id: {missing}"
    # No section action may fall back to a bare index.
    for bare in ("host._deletePromptSection(idx);", "host._addPromptSectionAfter(idx)",
                 "host._setSectionGlobalInherit(idx, key, box.checked)\n"):
        assert bare not in normalized, bare


_POPUP_DOM = """
        class N {
          constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = [];
            this.style = { cssText: "" }; this.dataset = {}; this.value = "";
            this.textContent = ""; this.title = ""; this.disabled = false;
            this._handlers = {}; this.options = []; this.isConnected = true; }
          appendChild(c) { this.children.push(c); c.parentElement = this;
            if (c.tagName === "OPTION") this.options.push(c); return c; }
          append(...cs) { cs.forEach((c) => this.appendChild(c)); }
          addEventListener(t, h) { (this._handlers[t] ||= []).push(h); }
          removeEventListener() {}
          setAttribute() {} getAttribute() { return null; }
          remove() { this.isConnected = false; }
          set innerHTML(v) {} get innerHTML() { return ""; }
          fire(t, e = {}) { for (const h of this._handlers[t] || [])
            h({ stopPropagation() {}, preventDefault() {}, target: this, ...e }); }
        }
        const all = (n, out = []) => { out.push(n); n.children.forEach((c) => all(c, out)); return out; };
        globalThis.document = { createElement: (t) => new N(t), body: new N("body"),
            activeElement: null, listeners: {},
            addEventListener(t, h) { (this.listeners[t] ||= []).push(h); },
            removeEventListener() {} };
        globalThis.Element = N;
        window.addEventListener = () => {}; window.removeEventListener = () => {};
"""


def test_the_guide_popup_refuses_a_stale_strength_before_undo_or_write():
    _run_gesture_node(_POPUP_DOM + """
        const w = makeWidget(), sent = [];
        let undos = 0, fetches = 0;
        w._pushUndo = () => { undos += 1; };
        w._runSceneMutation = async (operations) => { sent.push(operations); };
        w._fetchScenes = async () => { fetches += 1; };
        w._isGuideTrackLocked = () => false; w._isGuideTrackHidden = () => false;
        w._getGuideAsset = () => null; w.totalFrames = 100; w._timecodeMode = "frames";
        w.activeScene = { scene_id: 'scene',
            guide_frames: [{ guide_id: 'g1', frame_index: 10, asset_id: 'a1', strength: 1 }] };
        w._showGuideManagementPopup(0, 0);
        const strengthOf = () => all(w._guideManagerEl).find((n) => n.title === 'Guide strength');
        const staleInput = strengthOf();
        // Undo puts another guide on frame 10; the popup is not repainted.
        w.activeScene.guide_frames = [{ guide_id: 'g2', frame_index: 10, asset_id: 'a2', strength: 1 }];
        staleInput.value = '0.5';
        staleInput.fire('change');
        // The popup repaints through the gated scheduler (next frame, or 50 ms).
        await new Promise((resolve) => setTimeout(resolve, 80));
        assert.equal(undos, 0);
        assert.equal(sent.length, 0);
        assert.equal(fetches, 1);
        // Redrawn from the scene, the same edit goes through with g2's identity.
        const fresh = strengthOf();
        fresh.value = '0.5';
        fresh.fire('change');
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(undos, 1);
        assert.deepEqual(sent[0][0].expected, { guide_id: 'g2', frame_index: 10, asset_id: 'a2' });
    """)


def test_the_guide_popup_keeps_one_escape_registration_across_rebuilds():
    _run_gesture_node(_POPUP_DOM + """
        const w = makeWidget();
        w._isGuideTrackLocked = () => false; w._isGuideTrackHidden = () => false;
        w._getGuideAsset = () => null; w.totalFrames = 100;
        w.activeScene = { scene_id: 'scene', guide_frames: [] };
        w._showGuideManagementPopup(0, 0);
        const first = w._guideManagerReleaseEscape;
        assert.equal(typeof first, 'function');
        w._showGuideManagementPopup(0, 0);
        assert.equal(w._guideManagerReleaseEscape, first);
        assert.equal(w._managementModalMounted(), true);
        w._hideGuideManagementPopup();
        assert.equal(w._guideManagerReleaseEscape, null);
        assert.equal(w._managementModalMounted(), false);
    """)


def test_a_ctrl_chord_under_a_modal_is_consumed_but_clipboard_keys_are_not():
    _run_gesture_node("""
        window.addEventListener = () => {}; window.removeEventListener = () => {};
        globalThis.Element = class {};
        globalThis.document = { activeElement: null,
            addEventListener() {}, removeEventListener() {} };
        const w = makeWidget();
        w.isFullscreen = true;
        w._keyboardConsumerId = (name) => `test-${name}`;
        w._setupKeyboardEvents();
        w._promptPanelHandle = { isMounted: () => true };
        const key = (k, extra = {}) => w._editorKeyConsumer({ key: k, ctrlKey: false,
            metaKey: false, shiftKey: false, target: null, ...extra });
        assert.equal(key('s', { ctrlKey: true }), true);
        assert.equal(key('o', { metaKey: true }), true);
        assert.equal(typeof key('c', { ctrlKey: true }), 'symbol');
        assert.equal(typeof key('v', { ctrlKey: true }), 'symbol');
        assert.equal(typeof key('s'), 'symbol');
    """)


def test_reference_panel_recipe_write_follows_its_lane_and_blank_lanes_close():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for mounted Reference panel coverage")
    module_url = (ROOT / "web" / "js" / "editor_reference_panel.js").as_uri()
    script = """
class N {
  constructor(tag) { this.tagName=String(tag).toUpperCase(); this.children=[];
    this.options=[]; this.style={cssText:""}; this.dataset={}; this.attributes={};
    this.value=""; this.textContent=""; this.title=""; this.disabled=false;
    this.checked=false; this.open=false; this._handlers={}; }
  appendChild(c) { if(!c?.tagName) return c; this.children.push(c); c.parentElement=this;
    if(c.tagName==="OPTION") this.options.push(c); return c; }
  append(...cs) { cs.forEach((c)=>this.appendChild(c)); }
  addEventListener(t,h) { (this._handlers[t] ||= []).push(h); }
  removeEventListener() {}
  setAttribute(k,v) { this.attributes[k]=String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  focus() {}
  remove() { if(this.parentElement) this.parentElement.children=
    this.parentElement.children.filter((c)=>c!==this); }
}
globalThis.document={createElement:(t)=>new N(t),body:new N("body"),activeElement:null};
globalThis.window={addEventListener(){},removeEventListener(){},localStorage:null};
globalThis.localStorage={getItem(){return null;},setItem(){}};
const mod=await import(__MODULE_URL__);
const walk=(n,out=[])=>{out.push(n);n.children.forEach((c)=>walk(c,out));return out;};
const recipe=(id)=>({lane_id:id,recipe_id:"",media_kind:"image",recipe:{}});
const makeHost=(ids)=>({
  projectId:"project",activeSceneId:"scene",totalFrames:20,playhead:0,
  _trackLayout:ids.map((_,i)=>({type:"reference",laneIndex:i})),
  activeScene:{reference_lane_count:ids.length,duration_frames:20,
    reference_lane_configs:ids.map(()=>({})),reference_lane_recipes:ids.map(recipe),
    reference_items:[]},
  _referenceRecipePresets:[],_customReferenceRecipes:[],_referenceRecipeFieldSchema:[],
  _promptContextProfiles:[],_promptContextCatalog:{},
  _defaultReferenceLaneRecipe:()=>recipe(""),
  _referenceMemberForRef:()=>null,_findAssetById:()=>null,
  _referenceAssetPreviewUrl:()=>null,_referenceLaneAdvisories:()=>[],
  _isLaneLocked:()=>false,_channelTemplate:()=>({}),
  saves:[],_saveLaneConfig:async function(entries,options){this.saves.push({
    laneIndex:entries[0].laneIndex,expectedLaneId:options?.expectedLaneId,
    undoLabel:options?.undoLabel});},
});
const templateSelect=()=>walk(document.body.children.at(-1)).find((n)=>n.tagName==="SELECT");
const change=async(select)=>{for(const h of select._handlers.change||[])h({});
  await new Promise((r)=>setTimeout(r,0));};

const host=makeHost(["A","B"]);
const handle=mod.mountReferenceLanePanel(host,{laneIndex:1});
const staleSelect=templateSelect();
host.activeScene.reference_lane_recipes=[recipe("B"),recipe("A")];
await change(staleSelect);
const staleSaves=host.saves.length, followed=handle.laneIndex;
await change(templateSelect());
const fresh=host.saves[0];
handle.close();

// A lane with no durable id: an Undo restoring a lane before it shifts it.
const blank=makeHost(["A",""]);
const blankHandle=mod.mountReferenceLanePanel(blank,{laneIndex:1});
blank.activeScene.reference_lane_recipes=[recipe("A"),recipe("R"),recipe("")];
blank.activeScene.reference_lane_count=3;
blank._trackLayout=[0,1,2].map((i)=>({type:"reference",laneIndex:i}));
await change(templateSelect());
console.log(JSON.stringify({staleSaves,followed,fresh,
  blankSaves:blank.saves.length,blankClosed:blank._referencePanelHandle===null}));
""".replace("__MODULE_URL__", json.dumps(module_url))
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {"staleSaves": 0, "followed": 0,
                      "fresh": {"laneIndex": 0, "expectedLaneId": "B",
                                "undoLabel": "change lane recipe"},
                      "blankSaves": 0, "blankClosed": True}


# -- L3B: panels repaint after the scene is replaced, gated by what they show
# and by whether the author is working in them.

_REPAINT_HOST = """
        const node = (tag, type = '') => ({ tagName: tag, type, isContentEditable: false });
        const root = { members: new Set(), contains(n) { return this.members.has(n); } };
        globalThis.document = { activeElement: null, listeners: {},
            addEventListener(t, h) { (this.listeners[t] ||= []).push(h); },
            removeEventListener() {} };
        const w = makeWidget();
        let refreshes = 0;
        w._channelTemplate = () => ({ id: 'standard' });
        w._isPromptTrackLocked = () => false; w._isGlobalPromptTrackLocked = () => false;
        w._promptPanelHandle = { isMounted: () => true, element: root,
            refresh: () => { refreshes += 1; w._stampManagementPanel('prompt'); } };
        w.activeScene = { scene_id: 'scene', prompt_sections: [
            { prompt_id: 'a', start_frame: 0, end_frame: 10, channels: { visual: 'x' } }] };
        w._stampManagementPanel('prompt');
"""


def test_a_panel_repaints_only_when_what_it_projects_changed():
    _run_gesture_node(_REPAINT_HOST + """
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 0);
        // The panel's own channel save: text is not what the panel stamps, so
        // the mounted editors survive it.
        w.activeScene.prompt_sections[0].channels.visual = 'saved';
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 0);
        // An Undo that restores a section is a structural change.
        w.activeScene.prompt_sections.push({ prompt_id: 'b', start_frame: 10, end_frame: 20 });
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
    """)


def test_a_repaint_waits_for_typed_input_and_a_pressed_pointer_then_replays():
    _run_gesture_node(_REPAINT_HOST + """
        const field = node('INPUT', 'text');
        root.members.add(field);
        document.activeElement = field;
        w.activeScene.prompt_sections.push({ prompt_id: 'b', start_frame: 10, end_frame: 20 });
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 0);
        assert.equal(w._managementPanelsPending, true);
        // Focus leaves for a button inside the panel, but the pointer that
        // moved it is still down on that button: still deferred.
        const button = node('BUTTON');
        root.members.add(button);
        document.activeElement = button;
        w._managementPointerTarget = button;
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 0);
        // Pointer up; a focused button holds no input.
        w._managementPointerTarget = null;
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
        assert.equal(w._managementPanelsPending, false);
    """)


def test_leaving_the_panel_replays_a_deferred_repaint():
    _run_gesture_node(_REPAINT_HOST + """
        let frames = [];
        globalThis.requestAnimationFrame = (callback) => { frames.push(callback); return 1; };
        const field = node('TEXTAREA');
        root.members.add(field);
        document.activeElement = field;
        w.activeScene.prompt_sections[0].end_frame = 12;
        w._refreshOpenManagementPanels('scene');
        frames.shift()();
        assert.equal(refreshes, 0);
        // The author tabs out of the panel: the focus event schedules the
        // replay after its own task, then the next frame repaints.
        document.activeElement = null;
        for (const handler of document.listeners.focusout || []) handler({});
        await new Promise((resolve) => setTimeout(resolve, 0));
        frames.shift()();
        assert.equal(refreshes, 1);
    """)


def test_a_deferred_panel_render_replays_even_without_a_scene_change():
    _run_gesture_node(_REPAINT_HOST + """
        w._deferManagementPanelRender('prompt');
        assert.equal(w._managementPanelsPending, true);
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
    """)


def test_a_reference_lane_swap_repaints_the_reference_panel():
    _run_gesture_node("""
        globalThis.document = { activeElement: null, addEventListener() {}, removeEventListener() {} };
        const w = makeWidget();
        let refreshes = 0;
        w.activeScene = { scene_id: 'scene',
            reference_lane_recipes: [{ lane_id: 'A' }, { lane_id: 'B' }],
            reference_lane_configs: [{}, {}], reference_items: [] };
        w._referencePanelHandle = { laneIndex: 1, element: { contains: () => false },
            refresh: () => { refreshes += 1; } };
        w._stampManagementPanel('reference', { laneIndex: 1 });
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 0);
        w.activeScene.reference_lane_recipes.reverse();
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
    """)


def test_scene_replacement_and_local_paint_both_reach_the_repaint_hook():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    for method, marker in (("_setActiveScene(scene,", '_refreshOpenManagementPanels("scene")'),
                           ("_renderSceneAfterLocalMutation({",
                            '_refreshOpenManagementPanels("local-mutation")')):
        start = source.index(f"    {method}")
        end = source.index("\n    }\n", start) if "\n    }\n" in source[start:] else \
            source.index("\n    }\r\n", start)
        assert marker in source[start:end], method
    panel = (ROOT / "web/js/editor_prompt_panel.js").read_text(encoding="utf-8")
    assert 'host._stampManagementPanel?.("prompt")' in panel
    assert 'host._deferManagementPanelRender?.("prompt")' in panel
    reference = (ROOT / "web/js/editor_reference_panel.js").read_text(encoding="utf-8")
    assert 'host._stampManagementPanel?.("reference", { laneIndex: state.laneIndex })' in reference


def test_a_press_in_the_popup_is_tracked_from_the_moment_it_opens():
    # The tracker used to be installed by the first scene change, so on a fresh
    # page the first press in a panel went unseen and a repaint scheduled by
    # that press's own blur-commit rebuilt the row before its click landed.
    _run_gesture_node(_POPUP_DOM + """
        const w = makeWidget();
        w._isGuideTrackLocked = () => false; w._isGuideTrackHidden = () => false;
        w._getGuideAsset = () => null; w.totalFrames = 100;
        w.activeScene = { scene_id: 'scene',
            guide_frames: [{ guide_id: 'g1', frame_index: 10, asset_id: 'a1', strength: 1 }] };
        w._showGuideManagementPopup(0, 0);
        const popup = w._guideManagerEl;
        const button = all(popup).find((n) => n.tagName === 'BUTTON' && n.textContent === 'Hide');
        popup.contains = (n) => all(popup).includes(n);
        for (const h of document.listeners.pointerdown || []) h({ target: button, button: 0 });
        w.activeScene.guide_frames[0].strength = 0.5;
        w._runManagementPanelRefresh();
        assert.equal(w._guideManagerEl, popup);
        assert.equal(w._managementPanelsPending, true);
        // A right-button press does not hold it.
        for (const h of document.listeners.pointerup || []) h({});
        for (const h of document.listeners.pointerdown || []) h({ target: button, button: 2 });
        w._runManagementPanelRefresh();
        assert.notEqual(w._guideManagerEl, popup);
    """)


def test_an_undone_text_edit_repaints_through_the_panels_own_staleness_check():
    _run_gesture_node(_REPAINT_HOST + """
        let textStale = false;
        w._promptPanelHandle.isTextStale = () => textStale;
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 0);
        textStale = true;
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
    """)


def test_an_open_attachment_dialog_holds_the_prompt_panel():
    _run_gesture_node(_REPAINT_HOST + """
        let dialog = true;
        // Editors keep their menus mounted and hidden; only a visible one holds.
        document.querySelectorAll = () => [{ getClientRects: () => [] },
            { getClientRects: () => (dialog ? [{}] : []) }];
        w.activeScene.prompt_sections.push({ prompt_id: 'b', start_frame: 10, end_frame: 20 });
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 0);
        assert.equal(w._managementPanelsPending, true);
        dialog = false;
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
    """)


def test_a_deferral_raised_inside_a_run_survives_it():
    _run_gesture_node(_REPAINT_HOST + """
        w._promptPanelHandle.refresh = () => { refreshes += 1; w._deferManagementPanelRender('prompt'); };
        w.activeScene.prompt_sections.push({ prompt_id: 'b', start_frame: 10, end_frame: 20 });
        w._runManagementPanelRefresh();
        assert.equal(refreshes, 1);
        assert.equal(w._managementPanelsPending, true);
    """)


def test_the_prompt_panel_reports_text_staleness_against_its_baseline_and_view():
    source = (ROOT / "web/js/editor_prompt_panel.js").read_text(encoding="utf-8")
    assert "return !sameChannelText(now, base, channelKeys)\n                    && !sameChannelText(now, shown, channelKeys);" in source
    assert "isTextStale: () => mounted && textStaleChecks.some(" in source
    assert "host._managementPanelBusy?.(body, { selects: false })" in source
