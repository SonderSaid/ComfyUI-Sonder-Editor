"""Reference Prompting's Library writes (Library paint-first Phase 4).

The real `mountPromptIdentityPanel` on a minimal DOM, with the host writer
stubbed: a handle edit paints through `writeReferenceMember` after a local
collision check, Save defaults sends only the keys the author changed, and a
refused Save hands its typed values back through the host-owned drafts map, so
the editor reopens with them after the panel remounts. The host side of these
writes -- the guard rule and the real route -- is covered in
`test_reference_library_writes_js.py`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_PANEL = r"""
class N {
  constructor(tag) { this.tagName=String(tag).toUpperCase(); this.children=[];
    this.style={cssText:""}; this.dataset={}; this.attributes={}; this.options=[];
    this.value=""; this.placeholder=""; this.textContent=""; this.title=""; this._handlers={};
    this.checked=false; this.classNames=[]; this.classList={add:(value)=>this.classNames.push(value)}; }
  appendChild(c) { this.children.push(c); c.parentElement=this; if (c.tagName==="OPTION") this.options.push(c); return c; }
  append(...cs) { cs.forEach((c)=>c?.tagName && this.appendChild(c)); }
  insertBefore(c, ref) { const i=ref ? this.children.indexOf(ref) : -1;
    if (i < 0) return this.appendChild(c); this.children.splice(i, 0, c); c.parentElement=this; return c; }
  insertAdjacentElement(_where, c) { const list=this.parentElement.children;
    list.splice(list.indexOf(this)+1, 0, c); c.parentElement=this.parentElement; return c; }
  remove() { if (this.parentElement) this.parentElement.children=this.parentElement.children.filter((x)=>x!==this); }
  addEventListener(t,h) { (this._handlers[t] ||= []).push(h); }
  dispatch(t) { return Promise.all((this._handlers[t] || []).map((h) => h({ target:this, key:"",
    preventDefault(){}, stopPropagation(){} }))); }
  setAttribute(k,v) { this.attributes[k]=String(v); }
  setSelectionRange() {}
  blur() {}
  querySelector(selector) {
    const match = /data-physical-defaults='([^']*)'/.exec(selector);
    if (!match) return null;
    const walk = (n) => { for (const c of n.children) { if (c.dataset?.physicalDefaults===match[1]) return c;
      const found = walk(c); if (found) return found; } return null; };
    return walk(this);
  }
}
globalThis.document={createElement:(t)=>new N(t),body:new N("body"),activeElement:null};
globalThis.window={addEventListener(){},removeEventListener(){}};
globalThis.localStorage={getItem(){return null;},setItem(){}};
globalThis.CSS={escape:(v)=>String(v)};
const mod=await import(__IDENTITY__);
const writes=[]; const errors=[]; let refreshes=0; let answer=async()=>null;
const drafts=new Map(); let viewPatch={};
const member={member_id:"member-1",name:"Portrait One",asset_id:"asset-1",handle:"",prompt:"",
  visual_intent:"",audio_intent:"",disabled_capabilities:[]};
const mount=()=>{ const root=new N("div"); mod.mountPromptIdentityPanel(root, {
  profile:{physical_populations:[{key:"pictures",label:"Picture",source_key:"picture_ids",
    label_template:"<Picture {n}>"}], identity_kinds:[{key:"subject",label:"Subject"}]},
  candidate:{setup_manifest:{pictures:[{member_id:"member-1",asset_id:"asset-1",slot_number:1}]}},
  references:[{reference_id:"reference-1",name:"Cast",members:[member]}],
  assets:[{asset_id:"asset-1",asset_type:"image"}], semanticUnits:[],
  writeReferenceMember:(write)=>{ writes.push(structuredClone({...write, onUnconfirmedResolved:undefined}));
    return answer(write); },
  // The view is a new object per paint, never the row the panel drew.
  displayedMember:()=>({...member, ...viewPatch}), storedHandleFor:()=>member.handle,
  handleCollision:(handle)=>handle.toLowerCase()==="taken" ? {kind:"prompt identity",id:"u"} : null,
  defaultsDrafts:drafts, onRefresh:()=>{ refreshes+=1; }, onError:(error)=>errors.push(error.message),
}); return root; };
const walk=(n,out=[])=>{out.push(n);n.children.forEach((c)=>walk(c,out));return out;};
const find=(root, test)=>walk(root).find(test);
const handleInput=(root)=>find(root,(n)=>n.attributes["aria-label"]==="Physical Reference handle");
const button=(root,text)=>find(root,(n)=>n.tagName==="BUTTON" && n.textContent===text);
const textarea=(root)=>find(root,(n)=>n.tagName==="TEXTAREA");
const result = await (async () => {
__BODY__
})();
console.log(JSON.stringify(result));
"""


def _run(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Reference Prompting DOM tests")
    script = (_PANEL.replace("__IDENTITY__", json.dumps((ROOT / "web/js/prompt_identity_panel.js").as_uri()))
              .replace("__BODY__", body))
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_a_handle_another_owner_holds_is_refused_before_anything_is_sent():
    result = _run("""
    const root = mount();
    const input = handleInput(root);
    input.value = "Taken";
    await input.dispatch("change");
    return { writes: writes.length, value: input.value, errors };
    """)
    assert result == {"writes": 0, "value": "",
                      "errors": ["@Taken is already used by another prompt identity."]}


def test_a_handle_edit_paints_through_the_host_writer_with_the_drawn_guard():
    result = _run("""
    const root = mount();
    const input = handleInput(root);
    input.value = "  NewName ";
    await input.dispatch("change");
    // Unchanged against the view: nothing is sent.
    const again = handleInput(mount());
    again.value = "";
    await again.dispatch("change");
    return { writes, refreshes, errors };
    """)
    assert result["writes"] == [{"referenceId": "reference-1", "memberId": "member-1",
                                 "drawn": {"handle": ""}, "fields": {"handle": "NewName"},
                                 "label": "edit physical Reference handle"}]
    assert result["refreshes"] >= 1 and result["errors"] == []


def test_save_defaults_sends_only_the_keys_the_author_changed():
    result = _run("""
    const root = mount();
    await button(root, "Defaults").dispatch("click");
    textarea(root).value = "hello";
    await button(root, "Save defaults").dispatch("click");
    return { writes, open: !!textarea(root) };
    """)
    assert result["writes"] == [{
        "referenceId": "reference-1", "memberId": "member-1",
        "drawn": {"prompt": "", "visual_intent": "", "audio_intent": "", "disabled_capabilities": []},
        "fields": {"prompt": "hello"}, "label": "edit physical Reference prompt defaults"}]
    assert result["open"] is False, "the editor closes on Save"


def test_a_refused_save_reopens_the_editor_with_what_was_typed():
    result = _run("""
    answer = async () => { throw Object.assign(new Error("refused"), { status: 409 }); };
    const root = mount();
    await button(root, "Defaults").dispatch("click");
    textarea(root).value = "hello";
    await button(root, "Save defaults").dispatch("click");
    const kept = drafts.get("member-1")?.values?.prompt;
    // The panel remounts on every render; the host-owned map reopens it.
    answer = async () => null;
    const again = mount();
    const reopened = textarea(again)?.value;
    await button(again, "Save defaults").dispatch("click");
    return { kept, reopened, second: writes[1]?.fields, draftsLeft: drafts.size };
    """)
    assert result == {"kept": "hello", "reopened": "hello", "second": {"prompt": "hello"},
                      "draftsLeft": 0}


def test_attach_waits_for_a_handle_edit_still_saving():
    """Source pins for the Attach half, which needs the whole Prompt panel: the
    owner's stored handle is the server's, and Attach materializes behind a
    pending edit, expecting the typed handle."""
    panel = (ROOT / "web/js/editor_prompt_panel.js").read_text(encoding="utf-8")
    attach = panel[panel.index("const attachReference = async (owner, target, overrides = null) => {"):]
    attach = attach[:attach.index("identityPanelCleanup = mountPromptIdentityPanel(")]
    # Read when the attach acts, never from the row as drawn.
    reread = attach.index("host._referenceStoredHandle(owner.memberId)")
    assert reread < attach.index("host._materializeReferenceMemberHandle?.(")
    assert "handlePending: pendingHandle !== storedHandle," in attach
    assert 'owner?.type === "physical" && (!owner.storedHandle || owner.handlePending)' in attach
    # The displayed handle, even a clear to "", is what the materialize expects.
    assert 'expectedHandle: owner.handlePending ? String(owner.pendingHandle || "") : "",' in attach


def test_closing_the_defaults_editor_dismisses_a_returned_draft():
    result = _run("""
    answer = async () => { throw Object.assign(new Error("refused"), { status: 409 }); };
    const root = mount();
    await button(root, "Defaults").dispatch("click");
    textarea(root).value = "hello";
    await button(root, "Save defaults").dispatch("click");
    const again = mount();
    const reopened = !!textarea(again);
    await button(again, "Defaults").dispatch("click");
    return { reopened, closed: !textarea(again), draftsLeft: drafts.size, reopensAfter: !!textarea(mount()) };
    """)
    assert result == {"reopened": True, "closed": True, "draftsLeft": 0, "reopensAfter": False}


def test_a_second_handle_edit_before_the_panel_redraws_is_guarded_by_the_first():
    """The render after a write can be deferred (a press in the panel); the
    panel's own accepted write advances its drawn row, so the next write's
    guard is what the author saw, not an older value."""
    result = _run("""
    const root = mount();
    const input = handleInput(root);
    input.value = "First";
    await input.dispatch("change");
    viewPatch = { handle: "First" };
    input.value = "Second";
    await input.dispatch("change");
    return writes.map((write) => [write.drawn.handle, write.fields.handle]);
    """)
    assert result == [["", "First"], ["First", "Second"]]


# -- Prompt-tool override rows (Reference Prompting Streamline, Phase 2) -----------

_OVERRIDE = r"""
const itemWrites=[]; const overrideDrafts=new Map(); const guards=[]; let suppressed=false;
let liveScene=null;
const scene={ reference_lane_count:1, duration_frames:120,
  reference_lane_recipes:[{ lane_id:"lane-a", recipe:{ name:"Cast", soft:{} } }],
  reference_items:[{ reference_item_id:"item-1", lane_index:0, start_frame:0, end_frame:120,
    members:[{entity_id:"reference-1",member_id:"member-1"},{entity_id:"reference-1",member_id:"member-2"}],
    prompt_override:"" }] };
const references=[{reference_id:"reference-1",name:"Cast",members:[
  {member_id:"member-1",name:"Hat",asset_id:"asset-1",prompt:"a wide straw hat"},
  {member_id:"member-2",name:"Boot",asset_id:"asset-2",prompt:"a scuffed leather boot"}]}];
const mountOverride=(profile={}, sceneValue=scene)=>{ const root=new N("div"); mod.mountPromptIdentityPanel(root, {
  profile, candidate:{window:{start_frame:0,end_frame:120}}, scene:sceneValue, references,
  assets:[], semanticUnits:[],
  // The host writer applies a function intent to the row it holds NOW.
  writeReferenceItem:(itemId, intent, label)=>{
    const live=(liveScene || sceneValue).reference_items.find((i)=>i.reference_item_id===itemId);
    const fields=typeof intent==="function" ? intent(live) : intent;
    itemWrites.push({itemId, fields, label}); return Promise.resolve("ok"); },
  overrideDrafts, registerBoxGuard:(box, stored)=>guards.push({box, stored}),
  isCommitSuppressed:()=>suppressed,
  onRefresh(){}, onError:(error)=>errors.push(error.message),
}); return root; };
const overrideBox=(root)=>find(root,(n)=>n.tagName==="TEXTAREA" && n.dataset.referenceItemId==="item-1");
const rowText=(root)=>walk(find(root,(n)=>n.dataset?.sonderPromptOverride==="item-1")?.parentElement || new N("x"))
  .map((n)=>n.textContent).filter(Boolean);
"""


def _run_override(body):
    return _run(_OVERRIDE + body)


def test_edit_as_override_derives_from_the_live_members():
    """The button computes its text when the host acts, from the live row: a
    reconcile that dropped a member since the panel drew is honoured."""
    result = _run_override("""
    const root = mountOverride();
    liveScene = structuredClone(scene);
    liveScene.reference_items[0].members = [scene.reference_items[0].members[1]];
    const { deriveReferencePrompt } = await import(__RESOLUTION__);
    const expected = deriveReferencePrompt({ promptOverride: "", soft: {},
      members: [{ entity_name: "Cast", member_name: "Boot", prompt: "a scuffed leather boot" }] });
    await button(root, "Edit as override").dispatch("click");
    return { writes: itemWrites, expected };
    """.replace("__RESOLUTION__", json.dumps((ROOT / "web/js/reference_resolution.js").as_uri())))
    assert result["expected"] and "straw hat" not in result["expected"]
    assert result["writes"] == [{"itemId": "item-1",
                                 "fields": {"prompt_override": result["expected"]},
                                 "label": "override reference prompt"}]


def test_an_override_draft_survives_a_remount_and_saves_on_blur():
    result = _run_override("""
    scene.reference_items[0].prompt_override = "stored text";
    const first = mountOverride();
    const box = overrideBox(first);
    box.value = "typed text";
    await box.dispatch("input");
    // The panel remounts on every render: the typed text comes back.
    const again = mountOverride();
    const reopened = overrideBox(again).value;
    await overrideBox(again).dispatch("blur");
    return { reopened, writes: itemWrites, draftsLeft: overrideDrafts.size,
      guarded: guards.length >= 2 && guards.at(-1).stored() };
    """)
    assert result == {"reopened": "typed text", "draftsLeft": 0, "guarded": "stored text",
                      "writes": [{"itemId": "item-1", "fields": {"prompt_override": "typed text"},
                                  "label": "edit reference prompt override"}]}


def test_an_escape_reverted_override_box_does_not_save_on_blur():
    result = _run_override("""
    scene.reference_items[0].prompt_override = "stored text";
    const box = overrideBox(mountOverride());
    box.value = "typed text";
    await box.dispatch("input");
    // The panel's first Escape puts the stored text back and suppresses the blur.
    box.value = guards.at(-1).stored();
    suppressed = true;
    await box.dispatch("blur");
    return { writes: itemWrites.length, draftsLeft: overrideDrafts.size };
    """)
    assert result == {"writes": 0, "draftsLeft": 0}


def test_clear_goes_back_to_the_derived_text():
    result = _run_override("""
    scene.reference_items[0].prompt_override = "stored text";
    const root = mountOverride();
    await button(root, "Clear").dispatch("click");
    return itemWrites;
    """)
    assert result == [{"itemId": "item-1", "fields": {"prompt_override": ""},
                       "label": "clear reference prompt override"}]


def test_h3_labels_the_override_as_prompt_bridge_only():
    result = _run_override("""
    const h3 = rowText(mountOverride({ physical_populations: [{ key: "pictures", label: "Picture",
      source_key: "picture_ids", label_template: "<Picture {n}>" }] }));
    const generic = rowText(mountOverride({}));
    const label = "Override: Prompt Bridge / Generic formats only";
    return { h3: h3.includes(label), generic: generic.includes(label),
      copy: !!button(mountOverride({}), "Copy") };
    """)
    assert result == {"h3": True, "generic": False, "copy": True}


def test_a_locked_lane_shows_its_override_controls_locked():
    result = _run_override("""
    scene.reference_lane_configs = [{ locked: true }];
    const fresh = mountOverride();
    scene.reference_items[0].prompt_override = "stored text";
    const overridden = mountOverride();
    const state = (b) => ({ disabled: !!b?.disabled || b?.attributes?.["aria-disabled"] === "true",
      title: b?.title });
    return { take: state(button(fresh, "Edit as override")), clear: state(button(overridden, "Clear")),
      box: overrideBox(overridden).disabled };
    """)
    assert result["take"]["disabled"] and result["clear"]["disabled"] and result["box"] is True
    assert "Unlock the lane" in result["take"]["title"]
