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
