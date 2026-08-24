"""Phase 1 contracts for visible compile failures and cooperative prompt bars."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _run_node(script: str):
    """Execute an exported predicate under node and return its JSON output."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for this test")
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def _method(source: str, name: str, next_name: str) -> str:
    def declaration(method_name: str, offset: int = 0) -> int:
        candidates = [source.find(prefix, offset) for prefix in (
            f"    {method_name}(", f"    async {method_name}(")]
        found = [value for value in candidates if value >= 0]
        if not found:
            raise AssertionError(f"method {method_name} not found")
        return min(found)

    start = declaration(name)
    return source[start:declaration(next_name, start + 1)]


def test_queue_refusals_surface_the_server_detail_for_single_and_batch_jobs():
    widget = _source("web/js/editor_widget.js")
    single = _method(widget, "_addToRenderQueue", "_addBatchToRenderQueue")
    batch = _method(widget, "_addBatchToRenderQueue", "_fetchRenderQueue")
    for method in (single, batch):
        assert "failureDetail: (error) => [error?.payload?.code || error?.code, error?.message]" in method
        assert '.filter(Boolean).join(": ")' in method


def test_inline_attachment_transactions_keep_every_prompt_bar_open():
    widget = _source("web/js/editor_widget.js")
    builder = _method(widget, "_buildChannelInputs", "_showPromptCreator")
    assert builder.count("onEnter?.({ close: false })") == 9
    global_bar = _method(widget, "_showGlobalPromptEditor", "_updateScenePrompt")
    assert "({ close = true } = {})" in global_bar
    assert "if (close) this._hidePromptEditor();" in global_bar


def test_time_is_template_gated_and_identity_sections_replace_generated_subjects():
    panel = _source("web/js/editor_prompt_panel.js")
    identity = _source("web/js/prompt_identity_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert panel.count('template.shot_marker_channel\n                        ? ["shot", "timestamp"') >= 1
    assert 'mountPromptIdentityPanel(card' in panel
    assert 'referenceBody.dataset.referencePrompting = "1"' in identity
    assert 'identityBody.dataset.identityPrompting = "1"' in identity
    assert 'Create prompt identity' in identity
    assert 'Reference-generated' not in identity
    assert 'Managed by Reference' not in identity
    assert 'template.shot_marker_channel\n                        ? ["shot", "timestamp"' in widget


def test_prompting_rows_share_six_named_cells_and_compact_accessible_actions():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt row DOM coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.value=""; this.textContent=""; this.disabled=false; this._handlers={{}}; }}
  appendChild(c) {{ this.children.push(c); c.parentElement=this; return c; }}
  append(...cs) {{ cs.forEach((c) => c?.tagName && this.appendChild(c)); }}
  addEventListener(t,h) {{ (this._handlers[t] ||= []).push(h); }}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
  querySelector() {{ return null; }}
}}
globalThis.document={{createElement:(t)=>new N(t),body:new N("body"),activeElement:null}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
globalThis.CSS={{escape:(v)=>String(v)}};
const mod=await import({json.dumps(module_url)});
const root=new N("div");
mod.mountPromptIdentityPanel(root, {{
  profile: {{
    physical_populations:[{{key:"pictures",label:"Picture",source_key:"picture_ids",label_template:"<Picture {{n}}>"}}],
    identity_kinds:[{{key:"subject",label:"Subject"}}],
  }},
  candidate: {{setup_manifest:{{pictures:[{{member_id:"member-1",asset_id:"asset-1",role:"identity",slot_number:1}}]}}}},
  references:[{{reference_id:"reference-1",name:"Woman",members:[{{member_id:"member-1",name:"Portrait",asset_id:"asset-1"}}]}}],
  semanticUnits:[{{semantic_unit_id:"unit-1",handle:"Woman",name:"Woman",kind:"subject",sources:[{{member_id:"member-1"}}]}}],
}});
const rows=[];
const walk=(n)=>{{ if(n.dataset?.promptingRow) rows.push(n); n.children.forEach(walk); }};
walk(root);
console.log(JSON.stringify(rows.map((row)=>({{
  kind:row.dataset.promptingRow,
  columns:Number(row.dataset.promptingColumnCount || 0),
  children:row.children.length,
  cells:row.children.map((child)=>child.dataset.promptingCell || ""),
  action:row.children.at(-1)?.textContent,
  aria:row.children.at(-1)?.attributes?.["aria-label"] || "",
  css:row.style.cssText,
}}))));
"""
    rows = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert [row["kind"] for row in rows] == [
        "physical", "identity", "identity-create"]
    assert all(row["columns"] == row["children"] == 6 for row in rows)
    assert all(row["cells"] == [
        "thumbnail", "name", "status", "attach", "edit", "action"]
        for row in rows)
    # The physical row's action is LABELLED. It is the step that turns staged
    # media into something a prompt can name, and a bare "+" made the single
    # most important control in the tool the least discoverable one. The
    # identity rows keep their glyphs: delete is destructive and conventional,
    # and the create-identity row is already introduced by its own prose.
    assert [(row["action"], row["aria"]) for row in rows] == [
        ("+ Identity", "Create prompt identity from physical Reference"),
        ("×", "Delete prompt identity"),
        ("+", "Create prompt identity"),
    ]
    # The action column must be able to grow. A fixed width sized for the glyph
    # rows is what wrapped the labelled button onto two lines while every
    # assertion here still passed. This checks only that the column CAN grow —
    # whether the label actually fits at the real panel width is a question no
    # fake DOM can answer, and it belongs to the visual pass.
    assert all("minmax(28px,auto)" in row["css"] for row in rows)
    assert not any(row["css"].endswith("28px;") for row in rows)


def test_identity_editor_preserves_unknown_format_owned_values_visibly():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt identity preservation coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.options=[]; this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.value=""; this.textContent=""; this.disabled=false; this._handlers={{}}; }}
  appendChild(c) {{ this.children.push(c); c.parentElement=this;
    if(c.tagName==="OPTION") this.options.push(c); return c; }}
  append(...cs) {{ cs.forEach((c)=>c?.tagName && this.appendChild(c)); }}
  addEventListener(t,h) {{ (this._handlers[t] ||= []).push(h); }}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
  querySelector() {{ return null; }}
  remove() {{ if(this.parentElement) this.parentElement.children =
    this.parentElement.children.filter((c)=>c!==this); }}
}}
globalThis.document={{createElement:(t)=>new N(t),body:new N("body"),activeElement:null}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
globalThis.CSS={{escape:(v)=>String(v)}};
const mod=await import({json.dumps(module_url)});
const root=new N("div"); let saved=null;
mod.mountPromptIdentityPanel(root, {{
  profile: {{
    identity_kinds:[{{key:"subject",label:"Subject"}}],
    physical_populations:[{{key:"pictures",media_kinds:["image"]}}],
    contribution_catalog:{{pictures:[{{value:"known",label:"Known"}}]}},
    capabilities:{{reference:{{derived:{{retention:{{fields:{{
      visual_intent:{{values:[{{value:"known_visual",label:"Known visual"}}]}},
      audio_intent:{{values:[{{value:"known_audio",label:"Known audio"}}]}},
    }}}}}}}}}},
  }},
  references:[{{reference_id:"ref",name:"Ref",members:[{{
    member_id:"member",asset_id:"asset",name:"Member"}}]}}],
  assets:[{{asset_id:"asset",asset_type:"image"}}],
  semanticUnits:[{{semantic_unit_id:"unit",kind:"subject",name:"Lead",handle:"Lead",
    visual_intent:"future_visual",audio_intent:"future_audio",
    sources:[{{entity_id:"ref",member_id:"member",contribution:"future_contribution"}}],
    voice:{{member_id:"missing_voice"}}}}],
  saveSemanticUnitChange: async (change) => {{ saved=change.value; }},
}});
const walk=(n,out=[])=>{{ out.push(n); n.children.forEach((c)=>walk(c,out)); return out; }};
const edit=walk(root).find((n)=>n.tagName==="BUTTON" && n.textContent==="Edit");
edit._handlers.click[0]();
const modal=globalThis.document.body.children.at(-1);
const nodes=walk(modal);
const unsupported=nodes.filter((n)=>n.tagName==="OPTION"
  && n.textContent.startsWith("Unsupported saved value:"))
  .map((n)=>n.value).sort();
const save=nodes.find((n)=>n.tagName==="BUTTON" && n.textContent==="Save identity");
await save._handlers.click[0]();
console.log(JSON.stringify({{unsupported,saved}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["unsupported"] == [
        "future_audio", "future_contribution", "future_visual", "missing_voice"]
    assert result["saved"]["visual_intent"] == "future_visual"
    assert result["saved"]["audio_intent"] == "future_audio"
    assert result["saved"]["sources"][0]["contribution"] == "future_contribution"
    assert result["saved"]["voice"]["member_id"] == "missing_voice"


def test_identity_editor_progressive_disclosure_search_feedback_and_routing():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt identity editor DOM coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    profile = {
        "name": "MiniMax H3 Full Reference",
        "identity_kinds": [{
            "key": "subject", "label": "Subject",
            "description": "A visible person or character addressed as a Subject.",
        }],
        "physical_populations": [{"key": "pictures", "media_kinds": ["image"]}],
        "contribution_catalog": {
            "pictures": [{"value": "appearance", "label": "Appearance"}]},
        "capabilities": {"reference": {"derived": {
            "definitions": {
                "order": 1, "label": "Definition",
                "channel_key": "subject_definitions", "placement": "section_prefix",
                "description": "Defines the identity for this model.",
                "help": "Use stable physical traits.",
                "example": "<Subject 1> is a weathered sailor.",
            },
            "summary": {
                "order": 2, "label": "Summary", "channel_key": "summary",
                "placement": "section_prefix", "fields": {"task_types": {
                    "type": "enum_multi", "label": "Summary task types",
                    "help": "Choose model operations.",
                    "example": "reference generation + video editing", "values": [
                        {"value": "reference generation", "label": "Reference generation"},
                        {"value": "video editing", "label": "Video editing"},
                    ],
                }},
            },
            "retention": {
                "order": 3, "label": "Retention",
                "channel_key": "retention_analysis", "placement": "section_prefix",
                "fields": {
                    "visual_intent": {"values": [
                        {"value": "preserve", "label": "Fully preserve"}]},
                    "audio_intent": {"values": [{
                        "value": "reference_characteristics",
                        "label": "Reference audio"}]},
                },
            },
        }}},
    }
    script = r"""
class N {
  constructor(tag) {
    this.tagName=String(tag).toUpperCase(); this.children=[]; this.options=[];
    this.style={cssText:""}; this.dataset={}; this.attributes={}; this._handlers={};
    this.value=""; this.textContent=""; this.disabled=false; this.checked=false;
    this.open=false; this.multiple=false; this.selected=false;
  }
  appendChild(c) {
    if (typeof c === "string") { const text=new N("#text"); text.textContent=c; c=text; }
    this.children.push(c); c.parentElement=this;
    if (c.tagName === "OPTION") this.options.push(c);
    return c;
  }
  append(...cs) { cs.forEach((c) => c != null && this.appendChild(c)); }
  addEventListener(t,h) { (this._handlers[t] ||= []).push(h); }
  setAttribute(k,v) { this.attributes[k]=String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  querySelector() { return null; }
  remove() {
    if (this.parentElement) this.parentElement.children =
      this.parentElement.children.filter((c) => c !== this);
  }
  focus() { globalThis.document.activeElement=this; }
  get selectedOptions() { return this.options.filter((option) => option.selected); }
}
globalThis.document={
  createElement:(t)=>new N(t), createTextNode:(v)=>{const n=new N("#text");n.textContent=String(v);return n;},
  body:new N("body"), activeElement:null,
};
const windowHandlers={};
globalThis.window={
  addEventListener(t,h){(windowHandlers[t] ||= []).push(h);},
  removeEventListener(t,h){windowHandlers[t]=(windowHandlers[t] || []).filter((v)=>v!==h);},
};
globalThis.localStorage={getItem(){return null;},setItem(){}};
globalThis.CSS={escape:(v)=>String(v)};
const mod=await import(__MODULE__);
const root=new N("div"); let saves=0; let savedUnits=[];
mod.mountPromptIdentityPanel(root, {
  projectKey:"phase-d",
  profile:__PROFILE__,
  references:[
    {reference_id:"r1",name:"First Ref",members:[{member_id:"m1",name:"Portrait One",asset_id:"a1"}]},
    {reference_id:"r2",name:"Second Ref",members:[{member_id:"m2",name:"Portrait Two",asset_id:"a2"}]},
  ],
  assets:[{asset_id:"a1",asset_type:"image"},{asset_id:"a2",asset_type:"image"}],
  semanticUnits:[], candidate:{setup_manifest:{}},
  saveSemanticUnitChange:async(change)=>{saves += 1;savedUnits=[change.value];},
});
const walk=(n,out=[])=>{out.push(n);n.children.forEach((c)=>walk(c,out));return out;};
const text=(n)=>[n.textContent,...n.children.map(text)].join(" ");
const create=walk(root).find((n)=>n.tagName==="BUTTON"
  && n.attributes["aria-label"]==="Create prompt identity");
create._handlers.click[0]();
const modal=globalThis.document.body.children.at(-1);
let nodes=walk(modal);
const details=nodes.filter((n)=>n.tagName==="DETAILS").map((n)=>({
  title:n.children[0]?.textContent || "", open:n.open,
}));
const kindHelp=nodes.find((n)=>n.textContent.includes("visible person or character"))?.textContent || "";
const search=nodes.find((n)=>n.tagName==="INPUT" && n.placeholder==="Search physical References…");
const sourceRows=nodes.filter((n)=>n.tagName==="DIV"
  && n.children.some((c)=>String(c.textContent || "").startsWith("Portrait")));
const before=sourceRows.map((row)=>row.style.display || "");
search.value="second"; search._handlers.input[0]();
const after=sourceRows.map((row)=>row.style.display || "");
const routing=nodes.filter((n)=>n.dataset.sonderIdentityRoutingKind).map((n)=>({
  kind:n.dataset.sonderIdentityRoutingKind, text:text(n).replace(/\s+/g," ").trim(),
}));
const taskSelect=nodes.find((n)=>n.tagName==="SELECT" && n.multiple);
const taskLabels=taskSelect.options.map((option)=>option.textContent);
const routingTitles=nodes.filter((n)=>n.dataset.sonderIdentityRoutingKind)
  .map((n)=>n.title || "");
const definitionControl=nodes.find((n)=>n.tagName==="TEXTAREA"
  && n.placeholder==="Describe this identity for prompt use...");
const save=nodes.find((n)=>n.tagName==="BUTTON" && n.textContent==="Save identity");
await save._handlers.click[0]();
const required=nodes.find((n)=>n.dataset.sonderIdentityRequired);
const requiredFailure={display:required.style.display,text:required.textContent};
const name=nodes.find((n)=>n.tagName==="INPUT" && n.placeholder==="Identity name");
name.value="Edited subject";
taskSelect.options.find((option)=>option.value==="video editing").selected=true;
await save._handlers.click[0]();
const baseRoot=new N("div"); let baseUnits=[];
mod.mountPromptIdentityPanel(baseRoot, {
  projectKey:"phase-d-base",
  profile:{name:"MiniMax H3 Base",
    identity_kinds:[{key:"subject",label:"Subject",description:"A subject."}]},
  references:[],assets:[],semanticUnits:[],candidate:{setup_manifest:{}},
  saveSemanticUnitChange:async(change)=>{baseUnits=[change.value];},
});
const baseCreate=walk(baseRoot).find((n)=>n.tagName==="BUTTON"
  && n.attributes["aria-label"]==="Create prompt identity");
baseCreate._handlers.click[0]();
const baseModal=globalThis.document.body.children.at(-1);
const baseNodes=walk(baseModal);
const baseLabels=baseNodes.filter((n)=>n.tagName==="LABEL")
  .map((n)=>text(n).replace(/\s+/g," ").trim());
baseNodes.find((n)=>n.tagName==="INPUT" && n.placeholder==="Identity name").value="Base subject";
await baseNodes.find((n)=>n.tagName==="BUTTON"
  && n.textContent==="Save identity")._handlers.click[0]();
const baseIdentity=baseUnits[0] || {};
baseCreate._handlers.click[0]();
const escapeModal=globalThis.document.body.children.at(-1);
const escapeDialog=walk(escapeModal).find((n)=>n.attributes.role==="dialog");
const dialogAttrs={role:escapeDialog?.attributes.role || "",
  modal:escapeDialog?.attributes["aria-modal"] || "",
  labelledBy:escapeDialog?.attributes["aria-labelledby"] || ""};
const escapeEvent={key:"Escape",isComposing:false,keyCode:27,
  stopImmediatePropagation(){},preventDefault(){}};
windowHandlers.keydown[0](escapeEvent);
console.log(JSON.stringify({details,kindHelp,before,after,routing,taskLabels,saves,
  routingTitles,definitionTitle:definitionControl.title || "",
  taskTitle:taskSelect.title || "",
  requiredFailure,savedTaskTypes:savedUnits[0]?.attachment_defaults?.task_types || [],
  baseLabels,baseDefaults:baseIdentity.attachment_defaults || {},
  baseHasVisual:Object.hasOwn(baseIdentity,"visual_intent"),
  baseHasAudio:Object.hasOwn(baseIdentity,"audio_intent"),
  dialogAttrs,escapeClosed:!globalThis.document.body.children.includes(escapeModal),
  focused:globalThis.document.activeElement?.placeholder || ""}));
""".replace("__MODULE__", json.dumps(module_url)).replace(
        "__PROFILE__", json.dumps(profile))
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["details"] == [
        {"title": "Physical sources — optional", "open": False},
        {"title": "Voice — optional", "open": False},
        # Names the rung and, when the project has any, how many attachments
        # follow it. "Advanced" said nothing about which direction inheritance
        # ran, which is why this group and the chip's own fieldset read as two
        # copies of one panel.
        {"title": "Defaults for all attachments", "open": False},
        {"title": "Format defaults — derived output routing", "open": False},
    ]
    assert "visible person or character" in result["kindHelp"]
    assert result["before"] == ["none", "none"]
    assert result["after"] == ["none", "grid"]
    assert result["routing"] == [
        {"kind": "definitions",
         "text": "Definition → subject_definitions · section prefix"},
        {"kind": "summary",
         "text": "Summary → summary · section prefix"},
        {"kind": "retention",
         "text": "Retention → retention_analysis · section prefix"},
    ]
    assert result["taskLabels"] == ["Reference generation", "Video editing"]
    assert "Example: reference generation + video editing" in result["taskTitle"]
    assert "Example: <Subject 1> is a weathered sailor." in result["definitionTitle"]
    assert "Example: <Subject 1> is a weathered sailor." in result["routingTitles"][0]
    assert result["saves"] == 1
    assert result["requiredFailure"] == {
        "display": "block", "text": "Complete the required fields: Name."}
    assert result["savedTaskTypes"] == ["video editing"]
    assert not any(label.startswith((
        "Visual", "Audio default", "Summary default", "Retention default",
        "Audio definition", "Summary task types"))
        for label in result["baseLabels"])
    assert result["baseDefaults"] == {}
    assert result["baseHasVisual"] is False
    assert result["baseHasAudio"] is False
    assert result["dialogAttrs"]["role"] == "dialog"
    assert result["dialogAttrs"]["modal"] == "true"
    assert result["dialogAttrs"]["labelledBy"].startswith(
        "sonder-prompt-identity-title-")
    assert result["escapeClosed"] is True
    assert result["focused"] == "Identity name"


def test_disclosure_memory_remains_optional_when_browser_storage_fails():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for disclosure-memory coverage")
    module_url = (ROOT / "web/js/disclosure_memory.js").as_uri()
    script = f"""
const mod=await import({json.dumps(module_url)});
const writes=[];
const memory=mod.createDisclosureMemory("identity", {{storage:{{
  getItem(){{throw new Error("blocked");}},
  setItem(key,value){{writes.push([key,value]);throw new Error("blocked");}},
}}}});
const before=memory.isOpen("advanced",true);
memory.remember("advanced",false);
console.log(JSON.stringify({{before,after:memory.isOpen("advanced",true),writes:writes.length}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {"before": True, "after": False, "writes": 1}


def test_identity_modal_coalesces_background_refresh_until_draft_closes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for identity refresh-gate coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
const mod=await import({json.dumps(module_url)});
let runs=0;const scheduled=[];
const gate=mod.createModalRefreshGate(()=>{{runs += 1;}},{{
  schedule:(callback)=>scheduled.push(callback),
}});
gate.setOpen(true);
const first=gate.request();const second=gate.request();
const whileOpen={{runs,scheduled:scheduled.length}};
gate.setOpen(false);
const afterClose={{runs,scheduled:scheduled.length}};
scheduled.shift()();
const direct=gate.request();
gate.setOpen(true);gate.request();gate.clear();gate.setOpen(false);
const opening={{semantic_unit_id:"a",name:"Opening",order:0}};
const newer=[opening,{{semantic_unit_id:"b",name:"Added elsewhere",order:1}}];
const merged=mod.applyPromptIdentityChange(newer,{{type:"upsert",
  value:{{...opening,name:"Edited locally"}},expected:opening}});
let conflict="";
try {{
  mod.applyPromptIdentityChange([
    {{...opening,name:"Edited elsewhere"}},newer[1]],{{type:"upsert",
      value:{{...opening,name:"Edited locally"}},expected:opening}});
}} catch (error) {{ conflict=error.message; }}
console.log(JSON.stringify({{first,second,whileOpen,afterClose,runs,direct,
  remaining:scheduled.length,merged,conflict}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "first": False, "second": False,
        "whileOpen": {"runs": 0, "scheduled": 0},
        "afterClose": {"runs": 0, "scheduled": 1},
        "runs": 2, "direct": True, "remaining": 0,
        "merged": [
            {"semantic_unit_id": "a", "name": "Edited locally", "order": 0},
            {"semantic_unit_id": "b", "name": "Added elsewhere", "order": 1},
        ],
        "conflict": "Prompt identity changed elsewhere. Reopen it before saving.",
    }
    panel = _source("web/js/editor_prompt_panel.js")
    assert "createModalRefreshGate(() => renderNow())" in panel
    assert "onModalStateChange: identityRefreshGate.setOpen" in panel


def test_prompt_section_plus_and_delete_are_one_atomic_accessible_grid_cell():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt section DOM coverage")
    module_url = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.textContent=""; this.title=""; }}
  appendChild(c) {{ this.children.push(c); c.parentElement=this; return c; }}
  append(...cs) {{ cs.forEach((c)=>c?.tagName && this.appendChild(c)); }}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
}}
globalThis.document={{createElement:(t)=>new N(t),body:new N("body")}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
const mod=await import({json.dumps(module_url)});
const cells=Array.from({{length:6}},(_,i)=>new N(`cell-${{i}}`));
const add=new N("button"); add.textContent="+"; add.setAttribute("aria-label","Add prompt section after this section");
const remove=new N("button"); remove.textContent="✕"; remove.setAttribute("aria-label","Delete prompt section");
const row=mod.buildPromptSectionControlRow(cells,[add,remove]);
const group=row.children.at(-1);
console.log(JSON.stringify({{
  columns:Number(row.dataset.promptSectionColumnCount || 0),
  children:row.children.length,
  group:group.dataset.promptSectionActions,
  groupChildren:group.children.length,
  groupStyle:group.style.cssText,
  aria:group.children.map((child)=>child.attributes["aria-label"] || ""),
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["columns"] == result["children"] == 7
    assert result["group"] == "1" and result["groupChildren"] == 2
    assert "display:inline-flex" in result["groupStyle"]
    assert "white-space:nowrap" in result["groupStyle"]
    assert "flex:0 0 auto" in result["groupStyle"]
    assert result["aria"] == [
        "Add prompt section after this section", "Delete prompt section"]


def test_every_prompt_panel_glyph_control_has_an_accessible_name():
    source = _source("web/js/editor_prompt_panel.js")
    for expected in (
        'makeBtn("⋯", "Prompt format actions", "subtle",',
        'makeBtn("+", "Insert a new section after this one (fills the gap to the next section)",',
        'makeBtn("✕", "Delete this section", "danger",',
        'makeBtn("✕", "Delete this template", "danger",',
    ):
        assert expected in source
    assert '"Prompt format actions")' in source
    assert '"Add prompt section after this section")' in source
    assert '"Delete prompt section")' in source
    assert '"Delete prompt template")' in source


def test_reference_override_normalization_and_identity_defaults_in_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt Context helper coverage")
    module_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const attachment = mod.normalizePromptAttachment({{
  kind: "reference", config: {{summary: "old", audio_speaker_subject_id: "lead"}}
}});
const profile={{name:"Format A",capabilities:{{reference:{{
  defaults:{{summary:"format summary"}},
  capability_defaults:{{summary:{{task_types:["reference generation"]}}}},
  derived:{{summary:{{fields:{{task_types:{{label:"Task categories",
    default_source:"roles"}}}}}}}},
}}}}}};
const defaults = mod.referencePromptDefaults("lead", {{semanticUnits: [{{
  semantic_unit_id: "lead", handle: "Lead", definition: "Person",
  visual_intent: "partial", audio_intent: "copy_partial",
  attachment_defaults: {{summary: "identity summary"}}
}}],profile,capabilityKind:"summary"}});
const inherited=mod.referenceCapabilityInputProjection("summary",{{
  selected:"lead",profile,semanticUnits:[{{semantic_unit_id:"lead",handle:"Lead",
    attachment_defaults:{{summary:"identity summary"}}}}],overrides:{{}}}});
const authoredEmpty=mod.referenceCapabilityInputProjection("summary",{{
  selected:"lead",profile,semanticUnits:[{{semantic_unit_id:"lead",handle:"Lead",
    attachment_defaults:{{summary:"identity summary"}}}}],overrides:{{summary:""}}}});
const taskEmpty=mod.referenceCapabilityInputProjection("summary",{{
  selected:"lead",profile,semanticUnits:[{{semantic_unit_id:"lead",handle:"Lead",
    attachment_defaults:{{summary:"identity summary"}}}}],overrides:{{task_types:[]}}}});
const capabilityWins=mod.referenceCapabilityInputProjection("summary",{{
  selected:"lead",profile,semanticUnits:[{{semantic_unit_id:"lead",handle:"Lead",
    attachment_defaults:{{summary:"identity summary"}}}}],
  overrides:{{summary:"chip summary"}},
  capabilityConfig:{{summary:"capability summary"}}
}});
const stagedDefinition=mod.referenceCapabilityInputProjection("definitions",{{
  selected:"lead",profile:{{name:"Format A",capabilities:{{reference:{{
    derived:{{definitions:{{fields:{{}}}}}}}}}}}},
  semanticUnits:[{{semantic_unit_id:"lead",handle:"Lead",
    sources:[{{member_id:"member"}}]}}],
  setupManifest:{{presentation:[{{member_id:"member",member_prompt:"Staged prose"}}]}}
}});
const physical=mod.referencePromptDefaults("physical:picture:member",{{
  references:[{{reference_id:"ref",members:[{{member_id:"member",handle:"Portrait",
    prompt:"Library prose",visual_intent:"transfer_attributes"}}]}}],
  profile:{{name:"Format A",capabilities:{{reference:{{
    defaults:{{definition:"Format definition"}}}}}}}},
  setupManifest:{{pictures:[{{member_id:"member",visual_intent:"partial",
    audio_intent:"copy_partial"}}]}}
}});
const physicalFallback=mod.referencePromptDefaults("physical:picture:member",{{
  references:[{{members:[{{member_id:"member",handle:"Portrait",
    prompt:"Library prose"}}]}}],profile:{{name:"Format A"}}
}});
const physicalEntity=mod.referencePromptDefaults("physical:picture:member",{{
  references:[{{reference_id:"ref",name:"Korean Woman",
    visual_intent:"transfer_attributes",members:[{{member_id:"member",
    handle:"Portrait"}}]}}],profile:{{name:"Format A"}}
}});
const physicalBag=mod.referencePromptDefaults("physical:picture:member",{{
  references:[{{reference_id:"ref",name:"Korean Woman",members:[{{
    member_id:"member",handle:"Portrait",
    attachment_defaults:{{retention_detail:"Keeps her jacket."}}}}]}}],
  profile:{{name:"Format A"}},capabilityKind:"retention"
}});
console.log(JSON.stringify({{config: attachment.config, defaults,inherited,
  authoredEmpty,taskEmpty,capabilityWins,stagedDefinition,physical,physicalFallback,
  physicalEntity,physicalBag}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["config"] == {
        "audio_speaker_subject_id": "lead", "overrides": {"summary": "old"}}
    assert result["defaults"]["values"] == {
        "summary": "identity summary", "task_types": ["reference generation"],
        "definition": "Person", "visual_intent": "partial",
        "audio_intent": "copy_partial"}
    assert result["defaults"]["source"] == "Shared identity default · @Lead"
    # Every source carries a `rank` on the documented precedence chain as well as
    # a `tier`. `tier` has only three values and collapses entity, member, staged
    # and identity into one of them, so it cannot say which of two inherited
    # sources is the more specific — which is what a collapsed summary needs in
    # order to NAME the source in play instead of counting them.
    assert result["defaults"]["fieldSources"]["summary"] == {
        "label": "Shared identity default · @Lead", "tier": "shared", "rank": 5}
    assert result["defaults"]["fieldSources"]["task_types"] == {
        "label": "Prompt Format default · Format A", "tier": "format", "rank": 1}
    assert result["inherited"][:2] == [
        {"field": "summary", "label": "Summary", "value": "identity summary",
         "source": "Shared identity default · @Lead", "tier": "shared",
         "authored_empty": False},
        {"field": "task_types", "label": "Task categories",
         "value": ["reference generation"],
         "source": "Prompt Format default · Format A", "tier": "format",
         "authored_empty": False},
    ]
    assert result["authoredEmpty"][0] == {
        "field": "summary", "label": "Summary", "value": "",
        "source": "Chip override", "tier": "chip", "authored_empty": True,
        "stored_empty": True}
    assert result["taskEmpty"][1] == {
        "field": "task_types", "label": "Task categories",
        "value": [], "source": "Chip override", "tier": "chip",
        "authored_empty": True, "stored_empty": True}
    assert result["capabilityWins"][0] == {
        "field": "summary", "label": "Summary", "value": "capability summary",
        "source": "Chip override", "tier": "chip", "authored_empty": False}
    assert result["stagedDefinition"][0] == {
        "field": "definition", "label": "Definition", "value": "Staged prose",
        "source": "Staged Reference default · @Lead", "tier": "shared",
        "authored_empty": False}
    assert result["physical"]["values"] == {
        "definition": "Format definition", "visual_intent": "partial",
        "audio_intent": "copy_partial"}
    assert result["physical"]["fieldSources"] == {
        "definition": {"label": "Prompt Format default · Format A",
                       "tier": "format", "rank": 1},
        "visual_intent": {"label": "Staged Reference default · @Portrait",
                          "tier": "shared", "rank": 4},
        "audio_intent": {"label": "Staged Reference default · @Portrait",
                         "tier": "shared", "rank": 4},
    }
    # Authored Library prose is the definition of last resort, so a blank chip
    # FOLLOWS the member. This used to be absent entirely, which is why the chip
    # claimed no inherited definition existed. It stays BELOW a format default
    # (see `physical` above), matching the compiler's own fallback order.
    assert result["physicalFallback"]["values"] == {
        "definition": "Library prose",
        "visual_intent": "preserve",
        "audio_intent": "reference_characteristics"}
    assert result["physicalFallback"]["fieldSources"] == {
        "definition": {"label": "Physical Reference default · @Portrait",
                       "tier": "shared", "rank": 3},
        "visual_intent": {"label": "Renderer fallback · Format A",
                          "tier": "format", "rank": 1},
        "audio_intent": {"label": "Renderer fallback · Format A",
                         "tier": "format", "rank": 1},
    }
    # The Reference entity's authored intent is consulted before the renderer's
    # literal, mirroring the server setup manifest's own staged/member/entity
    # order. This tier existed in the data and in `minimax_h3.py` but the chip
    # skipped it, attributing an authored value to a fallback that never ran.
    assert result["physicalEntity"]["values"]["visual_intent"] == "transfer_attributes"
    assert result["physicalEntity"]["fieldSources"]["visual_intent"] == {
        "label": "Reference default · Korean Woman", "tier": "shared", "rank": 2}
    # The member's own defaults bag outranks a format default.
    assert result["physicalBag"]["values"]["retention_detail"] == "Keeps her jacket."
    assert result["physicalBag"]["fieldSources"]["retention_detail"] == {
        "label": "Physical Reference default · @Portrait",
        "tier": "shared", "rank": 3}
    # Rank orders the chain the durable rule documents, and the entity tier sits
    # BELOW the member tier — a distinction `tier` cannot express, since it calls
    # both "shared".
    assert (result["physicalEntity"]["fieldSources"]["visual_intent"]["rank"]
            < result["physicalBag"]["fieldSources"]["retention_detail"]["rank"]
            < result["physical"]["fieldSources"]["visual_intent"]["rank"]
            < result["defaults"]["fieldSources"]["summary"]["rank"])
    source = _source("web/js/prompt_context_chips.js")
    assert 'state.textContent = isOverride ? "Chip override"' in source
    assert 'rendered.textContent = value.authored_empty' in source
    assert "Delete this chip override and follow the current default." in source


def test_prompt_reference_attach_factory_and_first_use_history_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt identity helper coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    profiles = [{"capabilities": {"reference": {"derived": {
        "definitions": {"order": 1, "channel_key": "subjects",
                        "placement": "section_prefix", "label": "Definition"},
        "mentions": {"order": 2, "channel_key": "visual",
                     "placement": "inline", "label": "Mention"},
    }}}}, {"capabilities": {"reference": {"derived": {
        "summary": {"order": 1, "channel_key": "overview",
                    "placement": "channel_suffix", "label": "Overview"},
    }}}}]
    script = f"""
const mod = await import({json.dumps(module_url)});
const profiles = {json.dumps(profiles)};
const physical = mod.promptReferenceAttachment({{
  type: "physical", memberId: "portrait", declaration: {{source_key: "picture_ids"}}
}}, profiles[0]);
const identity = mod.promptReferenceAttachment({{
  type: "identity", identityId: "lead"
}}, profiles[1]);
console.log(JSON.stringify({{physical, identity}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["physical"]["source"] == {"picture_ids": ["portrait"]}
    assert result["identity"]["source"] == {"semantic_unit_ids": ["lead"]}
    # Capability IDENTITY and order come from the declaration; ROUTING does not.
    # Copying the declared channel and placement into the record froze them, so
    # a later format change moved the routing panel while compiled output stayed
    # put — and the frozen copy was indistinguishable from a real override.
    # Blank inherits, and only a genuine deviation is ever stored.
    # `enabled` is omitted for the same reason as the routing beside it: absent
    # inherits the Reference/identity default. Seeding `True` made every new
    # chip read as an explicit override, pinning the capability on however the
    # Reference was later configured.
    assert result["physical"]["capabilities"] == [
        {"capability_id": "definitions", "kind": "definitions",
         "channel_key": "", "placement": "", "config": {}},
        {"capability_id": "mentions", "kind": "mentions",
         "channel_key": "", "placement": "", "config": {}},
    ]
    assert result["identity"]["capabilities"] == [{
        "capability_id": "summary", "kind": "summary",
        "channel_key": "", "placement": "", "config": {},
    }]
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    lifecycle = panel.index(
        'host._beginSceneHistoryLifecycle?.("attach prompt Reference")')
    materialize = panel.index("_materializeReferenceMemberHandle", lifecycle)
    scene_commit = panel.index(
        "const committed = await commit(operations, label, history, lifecycleToken)",
        materialize)
    lifecycle_end = panel.index(
        "host._endSceneHistoryLifecycle?.(lifecycleToken)", scene_commit)
    assert lifecycle < materialize < scene_commit < lifecycle_end
    assert "referenceOperations" in panel and "inverseReferenceOperations" in panel
    assert "physicalMaterialization?.ownsHandle" in panel
    assert "referenceOperations: structuredClone" in widget
    assert "await this._applyReferenceHistoryOperations(entry.referenceOperations" in widget


def test_prompt_declaration_projection_drives_recipe_union_and_role_choices():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Format declaration coverage")
    module_url = (ROOT / "web/js/prompt_profile_declarations.js").as_uri()
    catalog = {"profiles": [{
        "key": "format_a@1", "resolved": {
            "profile_id": "format_a", "version": "1",
            "capabilities": {"reference": {"derived": {
                "definitions": {"order": 2, "label": "Definitions"},
            }}},
            "role_catalogs": {"pictures": [
                {"value": "identity", "label": "Character identity"},
            ]},
        },
    }, {
        "key": "format_b@1", "resolved": {
            "profile_id": "format_b", "version": "1",
            "capabilities": {"reference": {"derived": {
                "audio_relationship": {"order": 1, "label": "Audio relationship"},
                "definitions": {"order": 3, "label": "Duplicate label"},
            }}},
        },
    }]}
    default_catalog = {"profiles": [{
        "key": "generic@1", "resolved": {"capabilities": {"reference": {
            "derived": {"derived_prompt": {
                "order": 1, "label": "Reference prompt"}},
        }}},
    }]}
    script = f"""
const mod = await import({json.dumps(module_url)});
const catalog = {json.dumps(catalog)};
const defaultCatalog = {json.dumps(default_catalog)};
const profile = mod.resolvedPromptProfile({{
  profileId:"format_a@1", catalog,
  candidate:{{profile:{{profile_id:"format_a",version:"1",
    capabilities:{{reference:{{derived:{{}}}}}}}}}},
}});
console.log(JSON.stringify({{
  capabilities: mod.referenceCapabilityChoicesForProfiles(
    catalog, ["format_a@1", "format_b@1"]),
  defaultCapabilities: mod.referenceCapabilityChoicesForProfiles(defaultCatalog, []),
  roles: mod.referenceRoleChoices(profile, "pictures"),
  catalogWinsCandidate: profile.role_catalogs?.pictures?.[0]?.label || "",
  missingRoles: mod.referenceRoleChoices(profile, "audio"),
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "capabilities": [
            {"value": "definitions", "label": "Definitions"},
            {"value": "audio_relationship", "label": "Audio relationship"},
        ],
        "defaultCapabilities": [
            {"value": "derived_prompt", "label": "Reference prompt"}],
        "roles": [{"value": "identity", "label": "Character identity"}],
        "catalogWinsCandidate": "Character identity",
        "missingRoles": [],
    }

    consumers = "\n".join(_source(path) for path in (
        "web/js/editor_reference_panel.js",
        "web/js/prompt_identity_panel.js",
        "web/js/prompt_context_chips.js",
        "web/js/editor_prompt_panel.js",
        "web/js/editor_widget.js",
        "web/js/prompt_profile_declarations.js",
    ))
    for retired_global in (
        "catalog.capabilities", "catalog.role_catalogs",
        "catalog.minimax_task_types", "minimax_task_types",
    ):
        assert retired_global not in consumers
    for provider_value in (
        '"reference generation"', '"video editing"',
        '"video continuation"', '"audio reuse"', '"audio reference"',
        '"fully_preserved"', '"copy_all"',
        '"subject_definitions"', '"retention_analysis"',
    ):
        assert provider_value not in consumers


def test_writing_projection_displays_handles_over_stable_attachment_ids():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Writing projection helper coverage")
    module_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const identity = {{kind:"reference",source:{{semantic_unit_ids:["lead"]}}}};
const physical = {{kind:"reference",source:{{picture_ids:["portrait"]}}}};
const context = {{
  references:[{{reference_id:"woman",name:"Woman",members:[{{
    member_id:"portrait",name:"Character sheet",handle:"KoreanWoman"
  }}]}}],
  semanticUnits:[{{semantic_unit_id:"lead",name:"Lead",handle:"KWoman"}}],
}};
console.log(JSON.stringify([
  mod.resolveReferenceAttachmentIdentity(identity, context),
  mod.resolveReferenceAttachmentIdentity(physical, context),
]));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == ["@KWoman", "@KoreanWoman"]
    panel = _source("web/js/editor_prompt_panel.js")
    assert 'compiled.dataset.sonderWritingCompiled = "1"' in panel
    assert "writingCompiledPayload(payload)" in panel
    assert "prompt.textContent = compiled.prompt" in panel
    assert '["prompt_link", "prompt_link_scope"].includes(attachment.kind)' in panel


def test_linked_edits_use_exact_identity_snapshots_in_one_noncoalesced_batch():
    widget = _source("web/js/editor_widget.js")
    method = _method(widget, "_updateLinkedPromptAttachment", "_deletePromptSection")
    for identity_field in (
        "prompt_id", "start_frame", "end_frame", "prompt", "muted",
        "channels", "channel_docs", "attachments",
        "global_channel_exceptions",
    ):
        assert identity_field in method
    assert 'type: "update_prompt_section"' in method
    assert "coalesce: false" in method
    assert "retryOnConflict: false" in method


def test_prompt_diagnostics_use_stable_scrollable_geometry():
    panel = _source("web/js/editor_prompt_panel.js")
    assert "height:72px;overflow-y:auto;box-sizing:border-box" in panel


def test_prompt_tool_rebuilds_scope_rows_after_attachment_transactions():
    panel = _source("web/js/editor_prompt_panel.js")
    assert "renderGlobalScope();\n                            commitGlobal()" in panel
    assert "renderScope();\n                                commitChannels()" in panel


def test_context_actions_are_named_by_inline_vs_scope_semantics():
    chips = _source("web/js/prompt_context_chips.js")
    assert 'label: "Insert at cursor"' in chips
    assert 'label: "Writing aid"' in chips
    assert "export function installPromptContextMenu" in chips
    assert "createContextPicker" not in chips
    # One control where four were: the kind select, Attach, the reuse select and
    # Reuse collapsed into a single `+ Attach` whose menu carries the kinds and
    # a Reuse submenu. The old label named the two SCOPES it could attach to,
    # which the row's own caption now says once instead of on every button.
    assert 'add.textContent = "+ Attach";' in chips
    scope = chips[chips.index("export function createScopeChipRow"):]
    # No selects at all, and no per-chip trailing controls: a chip is label-only
    # so its holder's width is its width, which is what "uniform" means here.
    assert 'createElement("select")' not in scope
    assert 'textContent = "Unlink"' not in scope
    assert 'textContent = "Convert to copy"' not in scope
    # The two actions that both read "Unlink" are now distinguishable, because a
    # menu row can carry a hint and a button in a crowded row could not.
    assert '"Unlink this section"' in scope
    assert '"Unlink from the other sections"' in scope
    # And the filter that keeps Timeline, Structured and Writing agreeing about
    # what may be attached still runs once, here.
    assert "promptContextGatedKinds(" in scope


def test_writing_grip_and_prompt_panel_use_the_advertised_space():
    chips = _source("web/js/prompt_context_chips.js")
    panel = _source("web/js/editor_prompt_panel.js")
    assert 'max-height:${compact ? "100px" : "none"}' in chips
    assert "flex:0 0 auto;" in chips
    assert "function promptBox" not in panel
    for key in ("panelDraftBoxHeight", "panelGlobalBoxHeight", "panelChannelBoxHeight"):
        assert f'applyBoxHeight(host, ' in panel
        assert f'"{key}"' in panel
    assert 'width: "min(1320px, 96vw)"' in panel
    assert 'maxWidth: "1320px"' in panel


def _call_text(source, start):
    """One call, from its opening paren to its matching close.

    A fixed-width slice is a magic number that decides what the guard can see:
    the call this exists to police is 2034 characters long, so a 2000-character
    window missed its last argument and the probe that should have failed came
    back green.
    """
    depth = 0
    for index in range(source.index("(", start), len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("unbalanced call")


def _method_body(source, name, marker=None):
    """The text of one method: its parameter list, then its brace-matched body.

    Balances the PARENTHESES first. A destructured parameter list carries its
    own braces, so matching braces from the signature stops at the end of the
    parameters and returns a body of nothing -- which then satisfies every
    "this identifier is absent" assertion made about it.
    """
    start = source.index(name + "(")
    depth = 0
    cursor = start + len(name)
    for index in range(cursor, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                cursor = source.index("{", index)
                break
    else:
        raise AssertionError(f"unbalanced parentheses after {name}")
    depth = 0
    for index in range(cursor, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                body = source[start:index + 1]
                # Truncation is the dangerous failure, not total failure: a
                # short body satisfies every "this identifier is absent"
                # assertion vacuously. Brace matching cannot see braces inside
                # strings or template literals, so the caller states a marker it
                # knows the real body ends with.
                assert len(body) > len(name) + 40, body
                if marker is not None:
                    assert marker in body, (name, body[-300:])
                return body
    raise AssertionError(f"unbalanced braces after {name}")


def test_copy_plan_discloses_fallbacks_and_is_offered_only_when_supported():
    """The browser cannot offer Copy on a contribution the builder cannot serve."""
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert '"definitions", "retention", "mentions", "summary", "audio_relationship"' in panel
    assert "line.emitting && COPY_CAPABILITY_KINDS.has(line.capability)" in panel
    assert "plan.frozen_static" in panel and "plan.frozen_ordinal" in panel
    assert 'text += String(part.rendered || "")' in panel
    assert "frozenOrdinal.push(part.rendered)" in panel

    request = _method_body(
        widget, "_promptCopyPlan", marker="return payload?.copy_plan || null")
    assert "copy_plan_for:" in request
    assert "convert_plan_for" not in request
    assert "sceneWide" in request
    assert "selectionStart: 0, selectionEnd: duration" in request

    # A dormant row is projected from the scene-wide compile, so its Copy
    # action must request that same scope instead of silently recompiling the
    # current narrow selection and returning an empty plan.
    assert "{ sceneWide: Boolean(dormancy) }" in panel


def test_copy_plan_executes_the_scope_used_by_its_projection_row():
    """Dormant Copy behavior, not merely its source spelling, stays scene-wide."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Copy scope coverage")
    widget = _source("web/js/editor_widget.js")
    method = _method_body(
        widget, "_promptCopyPlan", marker="return payload?.copy_plan || null")
    script = """
const api = { apiURL: (value) => value };
const requests = [];
globalThis.fetch = async (_url, options) => {
  requests.push(JSON.parse(options.body));
  return { ok: true, json: async () => ({ copy_plan: { lines: [] } }) };
};
class Subject {
  async """ + method + """
  constructor() {
    this.activeSceneId = "scene";
    this.activeScene = { duration_frames: 100 };
    this.totalFrames = 100;
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return { contextStart: 10, contextEnd: 20 }; }
  _promptCompileRequestBody({ windowStart, windowEnd, selection = null }) {
    return {
      window_start: windowStart,
      window_end: windowEnd,
      selection_start: selection ? selection.selectionStart : windowStart,
      selection_end: selection ? selection.selectionEnd : windowEnd,
    };
  }
}
const subject = new Subject();
await subject._promptCopyPlan("attachment", "capability", null,
  { sceneWide: true });
await subject._promptCopyPlan("attachment", "capability");
console.log(JSON.stringify(requests));
"""
    requests = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert [requests[0][key] for key in (
        "window_start", "window_end", "selection_start", "selection_end"
    )] == [0, 100, 0, 100]
    assert [requests[1][key] for key in (
        "window_start", "window_end", "selection_start", "selection_end"
    )] == [10, 20, 10, 20]


def test_prompt_panel_consumes_only_windowed_candidate_diagnostics():
    """Prompt-tool blockers come only from the WINDOWED candidate compile.

    This used to assert `"_promptPayloadCache" not in panel` -- a NAME, so the
    moment a second full-scene payload arrived under any other name the guard
    passed while the invariant it was written for was gone. It now asserts the
    property: the only full-scene payload that exists is projection-shaped, the
    panel reaches it exclusively through two host helpers, and nothing on its
    arrival path can reach diagnostics.
    """
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert "refreshDiagnostics: renderDiagnostics" in panel
    assert "_candidate_scene_id: sceneId" in widget
    assert "const candidateSelection = resolvePromptCandidateSelection(" in widget
    # The windowed selection still reaches the compile, now through the one
    # request body both the preview and the Convert plan share. Two copies of
    # that body is how a surface ends up asking about a different candidate
    # than the author is looking at.
    assert "selection: candidateSelection," in widget
    assert "selection_start: selection ? selection.selectionStart" in widget
    assert "_promptCompileRequestBody({" in widget
    assert "this._promptPayloadCache = payload" not in widget

    # The scene-wide payload is kept behind two named helpers. The panel may not
    # touch the cache itself, or a surface could read a field the subset drops
    # and silently get `undefined` instead of the windowed answer.
    assert "_promptContextScenePayloadCache" not in panel
    assert "host._promptCandidateForSection?.(" in panel
    assert "host._promptSectionDormancy?.(" in panel

    # What the subset keeps, exactly. Anything not on this list never leaves the
    # fetch, which is the only form of the rule that survives a later change.
    subset = _method_body(widget, "_promptProjectionSubset", marker="return out;")
    keep = set(re.findall(r'"([a-z_]+)"', subset))
    assert keep == {
        "attachment_capability_projections", "attachment_channel_previews",
        "attachment_channel_routes", "emissions", "attachment_previews",
        "section_window_states"}, keep

    # And nothing on the scene payload's arrival path can reach diagnostics,
    # the queue gate, or Reference Prompting -- which derives from a
    # `setup_manifest` this payload deliberately does not carry.
    arrival = _method_body(widget, "_previewPromptContextScenePayload",
                           marker="refreshProjections?.();")
    # Comments stripped: the body explains at length what it must not do, and
    # matching that prose would make the guard pass or fail on the wording.
    code = " ".join(line.split("//")[0] for line in arrival.splitlines())
    for forbidden in ("refreshDiagnostics", "applyCandidate", "setup_manifest",
                      "ordinal_manifest", "errors", "warnings"):
        assert forbidden not in code, forbidden


def test_the_scene_wide_compile_asks_for_the_whole_scene():
    """A full-scene SELECTION, not a full-scene window. They are not the same.

    `window_start`/`window_end` in the compile body are only defaults for the
    selection; the server resolves the real window from `selection +/- context`
    (`resolve_execution_window`). Sending window fields alone therefore compiles
    the NARROW window again -- a scene-wide payload that is a copy of the one it
    exists to supplement, with nothing to show it went wrong.
    """
    widget = _source("web/js/editor_widget.js")
    arrival = _method_body(widget, "_previewPromptContextScenePayload",
                           marker="refreshProjections?.();")
    assert "selection: { selectionStart: 0, selectionEnd: duration }" in arrival
    assert "windowStart: 0, windowEnd: duration" in arrival
    # ...and it does not fire at all when the window already is the scene.
    assert "if (windowStart <= 0 && windowEnd >= duration)" in arrival


def test_prompt_projection_repaints_are_signature_gated_at_every_consumer():
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert panel.count("attachmentChannelProjectionSignature({") == 2
    assert widget.count("attachmentChannelProjectionSignature({") == 1
    # The first call must build hosts before callers dereference them.
    assert panel.count(
        "if (projectionHosts && signature === projectionSignature) return;") == 2
    assert "if (signature === projectionSignatures[key]) continue;" in widget
    assert "lastDiagnosticsSignature" in panel
    assert "lastWritingDecorationSignature" in panel
    assert "lastDormancySignature" in panel


def test_writing_decoration_signature_ignores_prose_but_tracks_layout_changes():
    module = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    result = _run_node(f"""
const mod = await import({json.dumps(module)});
const sig = (document) => mod.writingDecorationDocumentSignature(
  document, ["summary", "detailed_description"],
  {{defaultKey: "detailed_description"}});
const base = {{nodes:[
  {{type:"text",node_id:"a",text:"detailed_description:\\nA shot"}},
  {{type:"attachment",node_id:"b",attachment_id:"ref-1"}},
]}};
const prose = {{nodes:[
  {{type:"text",node_id:"a",text:"detailed_description:\\nA changed shot"}},
  {{type:"attachment",node_id:"b",attachment_id:"ref-1"}},
]}};
const heading = {{nodes:[
  {{type:"text",node_id:"a",text:"summary:\\nA changed shot"}},
  {{type:"attachment",node_id:"b",attachment_id:"ref-1"}},
]}};
const movedBlock = {{nodes:[
  {{type:"text",node_id:"a",text:"detailed_description:\\nA shot\\n---\\nNext"}},
  {{type:"attachment",node_id:"b",attachment_id:"ref-1"}},
]}};
console.log(JSON.stringify({{
  proseIsInert: sig(base) === sig(prose),
  headingRepaints: sig(base) !== sig(heading),
  blockMembershipRepaints: sig(base) !== sig(movedBlock),
}}));
""")
    assert result == {
        "proseIsInert": True,
        "headingRepaints": True,
        "blockMembershipRepaints": True,
    }


def test_writing_compiled_view_reads_candidate_and_relay_payload_shapes():
    module = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    result = _run_node(f"""
const mod = await import({json.dumps(module)});
console.log(JSON.stringify({{
  candidate: mod.writingCompiledPayload({{
    prompt:"candidate prompt", channels:{{summary:"candidate summary"}},
    compiled_prompt:"stale relay prompt",
  }}),
  relay: mod.writingCompiledPayload({{
    compiled_prompt:"relay prompt", compiled_channels:{{summary:"relay summary"}},
  }}),
  invalid: mod.writingCompiledPayload(null),
}}));
""")
    assert result == {
        "candidate": {
            "prompt": "candidate prompt",
            "channels": {"summary": "candidate summary"},
        },
        "relay": {
            "prompt": "relay prompt",
            "channels": {"summary": "relay summary"},
        },
        "invalid": {"prompt": "", "channels": {}},
    }


def test_stale_cache_marking_is_immediate_but_its_visual_repaint_waits():
    widget = _source("web/js/editor_widget.js")
    definition = widget[widget.index("\n    _previewPromptContextCandidate("):]
    preview = _method_body(definition, "_previewPromptContextCandidate",
                           marker="Math.max(0, Number(delay) || 0)")
    assert "PROMPT_STALE_VISUAL_DELAY_MS = 300" in widget
    assert "_promptContextCandidateCache = {" in preview
    assert "_promptContextScenePayloadCache = {" in preview
    stale_timer = preview.index("_promptContextStaleVisualTimer = setTimeout")
    compile_timer = preview.index("_promptContextPreviewTimer = setTimeout")
    assert stale_timer < compile_timer
    immediate = preview[preview.index("if (this._promptScenePayload())"):stale_timer]
    assert "refreshDiagnostics" not in immediate
    assert "_refreshInlinePromptProjections" not in immediate
    # Every terminal window branch settles against both parallel caches.
    assert preview.count("_clearPromptStaleVisualTimerIfSettled(sceneId)") >= 2


def test_stale_visual_timer_and_scene_invalidation_execute_at_edit_time():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for stale-paint lifecycle coverage")
    widget = _source("web/js/editor_widget.js")
    definition = widget[widget.index("\n    _previewPromptContextCandidate("):]
    method = _method_body(definition, "_previewPromptContextCandidate",
                          marker="Math.max(0, Number(delay) || 0)")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const api = { apiURL: (value) => value };
const resolvePromptCandidateSelection = (start, end, duration) =>
  ({ selectionStart: start || 0, selectionEnd: end || duration });
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = { fn, ms, cancelled: false };
  timers.push(timer);
  return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
class Subject {
""" + method + """
  constructor() {
    this.activeSceneId = "scene";
    this.activeScene = { duration_frames: 100 };
    this.totalFrames = 100;
    this.selectionStart = 0;
    this.selectionEnd = 100;
    this._promptContextPreviewToken = 0;
    this._promptContextScenePayloadToken = 7;
    this._promptContextCandidateCache = {
      _candidate_scene_id: "scene", _stale: false };
    this._promptContextScenePayloadCache = {
      _candidate_scene_id: "scene", _stale: false };
    this.counts = { diagnostics: 0, projections: 0, inline: 0 };
    this._promptPanelHandle = {
      // Production `renderDiagnostics` owns the panel projection fan-out.
      refreshDiagnostics: () => {
        this.counts.diagnostics++; this.counts.projections++;
      },
      refreshProjections: () => this.counts.projections++,
    };
    this._refreshInlinePromptProjections = () => this.counts.inline++;
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return { contextStart: 0, contextEnd: 100 }; }
  _promptScenePayload() { return this._promptContextScenePayloadCache; }
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 1000);
const immediate = {
  windowStale: subject._promptContextCandidateCache._stale,
  windowVisual: subject._promptContextCandidateCache._stale_visual,
  sceneStale: subject._promptContextScenePayloadCache._stale,
  sceneVisual: subject._promptContextScenePayloadCache._stale_visual,
  sceneToken: subject._promptContextScenePayloadToken,
  counts: { ...subject.counts },
};
const staleTimer = timers.find((timer) =>
  timer.ms === PROMPT_STALE_VISUAL_DELAY_MS && !timer.cancelled);
await staleTimer.fn();
const painted = {
  windowVisual: subject._promptContextCandidateCache._stale_visual,
  sceneVisual: subject._promptContextScenePayloadCache._stale_visual,
  counts: { ...subject.counts },
};
subject._previewPromptContextCandidate({}, 1000);
console.log(JSON.stringify({ immediate, painted,
  secondSceneToken: subject._promptContextScenePayloadToken }));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "immediate": {
            "windowStale": True, "windowVisual": False,
            "sceneStale": True, "sceneVisual": False,
            "sceneToken": 8,
            "counts": {"diagnostics": 0, "projections": 0, "inline": 0},
        },
        "painted": {
            "windowVisual": True, "sceneVisual": True,
            "counts": {"diagnostics": 1, "projections": 1, "inline": 1},
        },
        "secondSceneToken": 9,
    }


def test_window_settlement_keeps_timer_while_scene_cache_is_stale():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for parallel-cache settlement coverage")
    widget = _source("web/js/editor_widget.js")
    start = widget.index("\n    _clearPromptStaleVisualTimerIfSettled(")
    settle = _method_body(widget[start:], "_clearPromptStaleVisualTimerIfSettled",
                          marker="return true;")
    script = """
const cancelled = [];
globalThis.clearTimeout = (value) => cancelled.push(value);
class Subject {
""" + settle + """
}
const subject = new Subject();
subject.activeSceneId = "scene";
subject._promptContextStaleVisualTimer = "timer";
subject._promptContextCandidateCache = {
  _candidate_scene_id: "scene", _stale: false };
subject._promptContextScenePayloadCache = {
  _candidate_scene_id: "scene", _stale: true, _stale_visual: false };
const whileScenePending = subject._clearPromptStaleVisualTimerIfSettled("scene");
subject._promptContextScenePayloadCache = null;
const afterSceneSettles = subject._clearPromptStaleVisualTimerIfSettled("scene");
console.log(JSON.stringify({ whileScenePending, afterSceneSettles,
  cancelled, timer: subject._promptContextStaleVisualTimer }));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "whileScenePending": False,
        "afterSceneSettles": True,
        "cancelled": ["timer"],
        "timer": None,
    }


def test_invalid_window_payload_publishes_failure_and_settles_timer():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for invalid-payload lifecycle coverage")
    widget = _source("web/js/editor_widget.js")
    preview_start = widget.index("\n    _previewPromptContextCandidate(")
    preview = _method_body(widget[preview_start:], "_previewPromptContextCandidate",
                           marker="Math.max(0, Number(delay) || 0)")
    settle_start = widget.index("\n    _clearPromptStaleVisualTimerIfSettled(")
    settle = _method_body(widget[settle_start:],
                          "_clearPromptStaleVisualTimerIfSettled",
                          marker="return true;")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const api = { apiURL: (value) => value };
const resolvePromptCandidateSelection = (_start, _end, duration) =>
  ({ selectionStart: 0, selectionEnd: duration });
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = { fn, ms, cancelled: false };
  timers.push(timer); return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
globalThis.fetch = async () => ({ ok: true, status: 200,
  json: async () => null });
class Subject {
""" + settle + """
""" + preview + """
  constructor() {
    this.activeSceneId = "scene";
    this.activeScene = { duration_frames: 100 };
    this.totalFrames = 100;
    this._promptContextCandidateCache = {
      _candidate_scene_id: "scene", _stale: false };
    this._promptContextScenePayloadCache = null;
    this.counts = { diagnostics: 0, inline: 0, apply: 0 };
    this._promptPanelHandle = {
      refreshDiagnostics: () => this.counts.diagnostics++,
      applyCandidate: () => this.counts.apply++,
    };
    this._refreshInlinePromptProjections = () => this.counts.inline++;
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return { contextStart: 0, contextEnd: 100 }; }
  _promptScenePayload() { return null; }
  _previewPromptContextScenePayload() {}
  _promptCompileRequestBody() { return {}; }
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 0);
const compile = timers.find((timer) => timer.ms === 0 && !timer.cancelled);
await compile.fn();
const staleTimer = timers.find((timer) => timer.ms === 300);
console.log(JSON.stringify({
  code: subject._promptContextCandidateCache.errors[0].code,
  stale: subject._promptContextCandidateCache._stale,
  staleTimerCancelled: staleTimer.cancelled,
  counts: subject.counts,
}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "code": "preview_invalid_response", "stale": False,
        "staleTimerCancelled": True,
        "counts": {"diagnostics": 1, "inline": 1, "apply": 1},
    }


def test_invalid_scene_payload_clears_obsolete_dormant_projections():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for scene failure settlement coverage")
    widget = _source("web/js/editor_widget.js")
    scene_start = widget.index("\n    _previewPromptContextScenePayload(")
    scene_method = _method_body(widget[scene_start:],
                                "_previewPromptContextScenePayload",
                                marker="refreshProjections?.();")
    settle_start = widget.index("\n    _clearPromptStaleVisualTimerIfSettled(")
    settle = _method_body(widget[settle_start:],
                          "_clearPromptStaleVisualTimerIfSettled",
                          marker="return true;")
    script = """
const api = { apiURL: (value) => value };
globalThis.fetch = async () => ({ ok: true, status: 200,
  json: async () => null });
class Subject {
""" + settle + """
""" + scene_method + """
  constructor() {
    this.activeSceneId = "scene";
    this.totalFrames = 100;
    this._promptContextScenePayloadToken = 0;
    this._promptContextCandidateCache = {
      _candidate_scene_id: "scene", _stale: false };
    this._promptContextScenePayloadCache = {
      _candidate_scene_id: "scene", _stale: true, _stale_visual: false };
    this._promptContextStaleVisualTimer = setTimeout(() => {}, 10000);
    this.counts = { inline: 0, projections: 0 };
    this._refreshInlinePromptProjections = () => this.counts.inline++;
    this._promptPanelHandle = {
      refreshProjections: () => this.counts.projections++,
    };
  }
  _promptCompileRequestBody() { return {}; }
  _promptProjectionSubset(value) { return value; }
}
const subject = new Subject();
subject._previewPromptContextScenePayload({ dirName: "project", sceneId: "scene",
  candidate: { duration_frames: 100 }, windowStart: 10, windowEnd: 20 });
await new Promise((resolve) => setTimeout(resolve, 0));
console.log(JSON.stringify({ cache: subject._promptContextScenePayloadCache,
  timer: subject._promptContextStaleVisualTimer, counts: subject.counts }));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "cache": None, "timer": None,
        "counts": {"inline": 1, "projections": 1},
    }


def test_an_editing_surface_never_reads_the_scene_wide_payload():
    """The attach dialog is an editing surface, not a projection surface.

    `configurePromptAttachment` renders each capability's live routing state
    from `attachment_capability_projections` and a standalone Time's resolved
    value from `attachment_channel_previews` -- both of which the scene payload
    KEEPS. So "we dropped the dangerous fields" is not by itself an argument:
    handed the scene payload it would show a compile state the render will not
    produce, with none of the disclosure a projection row carries. It also reads
    `setup_manifest`, which the subset drops, so it would silently degrade.
    """
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    chips = _source("web/js/prompt_context_chips.js")
    # The fields that make this a live question rather than a theoretical one.
    assert "candidate?.attachment_capability_projections" in chips
    assert "candidate?.attachment_channel_previews" in chips
    # EVERY configure call, in BOTH files, and every one of them -- not the
    # first. The inline timeline bar lives in `editor_widget.js`, which an
    # earlier version of this guard did not read at all, and it was handing the
    # dialog its own per-section `candidate` closure.
    windowed = ("candidate: currentCandidatePayload()",
                "candidate: this._windowedPromptCandidate()")
    checked = 0
    for source in (panel, widget):
        start = 0
        while True:
            start = source.find("configurePromptAttachment(", start)
            if start < 0:
                break
            call = _call_text(source, start)
            start += 1
            # SHORTHAND counts. `candidate,` passes a local named
            # `candidate`, which in the inline bar is the per-section
            # payload. An earlier version of this guard skipped any call
            # with no literal `candidate:`, so the one real violation in
            # the tree went unseen and the probe that should have caught
            # it came back green.
            for line in call.split(chr(10)):
                assert line.strip() not in ("candidate,", "candidate"), call[:600]
            if "candidate:" not in call:
                continue
            assert any(value in call for value in windowed), call[:600]
            checked += 1
    # The guard is worthless if it matched nothing; there are several.
    assert checked >= 4, checked
    # And the projection surfaces are the ones that switch.
    assert "payload = candidateForSection(section.prompt_id)) => {" in panel


def test_refused_prompt_writes_reconcile_every_mounted_consumer():
    """A refused write must not leave an open panel showing rejected text.

    Each of these paths rolls the scene back correctly and then reached only the
    timeline, so Prompt Management — a separate overlay owning its own channel
    editors — kept displaying what the server had just refused.

    Source-level by necessity: `editor_widget.js` throws on `window` at its own
    line 6 and cannot be imported under node, so the class methods are
    unreachable. The gated seam itself is behaviour, asserted below.
    """
    widget = _source("web/js/editor_widget.js")
    for name, following in (
        ("_updateSceneGlobalContext", "_setSectionGlobalInherit"),
        ("_updatePromptSection", "_updateLinkedPromptAttachment"),
        ("_placeReferencePayload", "_handleAssetDrop"),
    ):
        method = _method(widget, name, following)
        catch = method[method.index("} catch"):]
        assert "this._refreshPromptContextDependencyConsumers();" in catch, (
            f"{name}'s refusal path does not reconcile the Prompt panel")

    # The seam must stay GATED. Calling `_promptPanelHandle.refresh()` directly
    # would rebuild the panel from `activeScene` even mid-edit, and because
    # these writes coalesce a refusal can land after the user has started
    # typing in another channel box — discarding that text.
    seam = _method(widget, "_refreshPromptContextDependencyConsumers",
                   "_fetchReferences")
    assert "this._hasPendingProjectMutations()" in seam
    assert "this.isDragging || this._timelineMutationDepth > 0" in seam
    assert "this._deferProjectBackedRefresh(" in seam
    # Deferral must return before touching any consumer.
    assert seam.index("_deferProjectBackedRefresh") < seam.index(
        "this._promptPanelHandle?.refresh?.()")


def test_mounted_prompt_consumers_request_and_reconcile_dependencies():
    widget = _source("web/js/editor_widget.js")
    assert widget.count("if (this._promptContextConsumersMounted())") == 5
    show_panel = _method(widget, "_showPromptManagementPanel", "_channelTemplateSwitchImpact")
    assert "if (this._promptPanelHandle?.isMounted?.())" in show_panel
    builder = _method(widget, "_buildChannelInputs", "_showPromptCreator")
    assert "scheduleCandidatePreview();\n        return { wrap" in builder
    assert 'inputs[key] ? inputs[key].value.trim() : (channels[key] || "")' in builder
    assert "const snapshot = draftSceneSnapshot();" in builder
    reference_apply = _method(widget, "_applyReferencePayload", "_promptContextConsumersMounted")
    assert "this._refreshPromptContextDependencyConsumers();" in reference_apply
    replay = _method(widget, "_replayDeferredProjectBackedRefresh", "_schedulePostMutationSceneRefresh")
    assert "if (this.isDragging || this._timelineMutationDepth > 0)" in replay
    assert "this._pendingProjectRefreshDrain = false;" in replay
    drag_replay = _method(widget, "_flushDeferredDragState", "_shouldDeferSceneRefresh")
    assert "this._replayDeferredProjectBackedRefresh();" in drag_replay
    timeline_commit = _method(widget, "_withTimelineMutationCommit", "_buildDOM")
    assert "if (this._timelineMutationDepth === 0)" in timeline_commit
    project_update = _method(widget, "updateProject", "refresh")
    assert 'this._fetchReferences({ reason: "load_project", force: true })' in project_update


def test_landed_candidate_rebuilds_reference_prompting_but_not_on_every_keystroke():
    """Reference Prompting reads `candidate.setup_manifest`, not `_references`.

    Confirmed live 2026-08-16 against the real editor: four seconds after the
    panel opened, the candidate cache held a scene-matched, non-stale payload
    with three `setup_manifest.pictures` rows while the DOM still showed three
    "no references resolve in this window" messages. A bare `refresh()` — same
    cached candidate, no refetch — rendered all three `<Picture N>` rows. The
    compile's success path simply never told that section anything.

    The signature is what keeps the fix from becoming PR-11 again: typing
    rewrites the compiled text on every debounce but leaves the setup and
    ordinal manifests alone, so only a genuinely new manifest rebuilds.
    """
    module = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    base = {"_candidate_scene_id": "s1",
            "setup_manifest": {"pictures": [{"slot": 1}]},
            "ordinal_manifest": {"pictures": {"m1": 1}}}
    typed = {**base, "channels": {"visual": "text the user just typed"},
             "emissions": ["changed"], "_stale": False}
    restaged = {**base, "setup_manifest": {"pictures": [{"slot": 1}, {"slot": 2}]}}
    result = _run_node("\n".join([
        f"const mod = await import({json.dumps(module)});",
        "const sig = mod.identityCandidateSignature;",
        "console.log(JSON.stringify({",
        f"  typingIsInert: sig({json.dumps(base)}) === sig({json.dumps(typed)}),",
        f"  restageRebuilds: sig({json.dumps(base)}) !== sig({json.dumps(restaged)}),",
        f"  arrivalRebuilds: sig(null) !== sig({json.dumps(base)}),",
        "  emptyStable: sig(null) === sig(undefined),",
        "}));",
    ]))
    # The whole point: a landed manifest rebuilds, a keystroke does not.
    assert result["arrivalRebuilds"] is True
    assert result["restageRebuilds"] is True
    assert result["typingIsInert"] is True
    assert result["emptyStable"] is True

    widget = _source("web/js/editor_widget.js")
    panel = _source("web/js/editor_prompt_panel.js")
    # The compile success path must reach the seam, not only diagnostics.
    preview = _method(widget, "_previewPromptContextCandidate",
                      "_refreshPromptUsageHighlight")
    assert "this._promptPanelHandle?.applyCandidate?.(" in preview
    assert preview.index("refreshDiagnostics") < preview.index("applyCandidate")
    # It must go through `render`, which is gated by `identityRefreshGate`;
    # touching the identity section directly discards an open modal draft.
    seam = panel.split("applyCandidate: (payload) => {", 1)[1].split("},", 1)[0]
    assert "render();" in seam
    assert "identityCandidateSignature(payload)" in seam
    assert "if (next === lastIdentityCandidateSignature) return false;" in seam
    assert "mountPromptIdentityPanel(" not in seam


def test_deferred_references_replay_keeps_the_requesting_consumer():
    """A deferred request replays with the intent it was DEFERRED with.

    The replay used to re-derive eligibility from the sidebar, so every
    references request whose consumer was NOT the sidebar was silently dropped:
    the Prompt tool asks with `force` on mount, and if that fetch defers behind
    a pending project mutation the replay discarded it.

    This was found while chasing "Reference Prompting renders empty on a fresh
    fullscreen open". It is a real defect on that path, but fixing it did NOT
    change that symptom, which remains open in the bug tracker. This test owns
    the deferral contract only — do not read it as coverage of that bug.
    """
    widget = _source("web/js/editor_widget.js")
    fetch = _method(widget, "_fetchReferences", "_mutateReferences")
    # The intent has to be recorded at the moment of deferral; nothing
    # downstream can reconstruct "a non-sidebar consumer asked".
    assert "if (force) this._deferredReferencesForce = true;" in fetch
    assert fetch.index("this._deferredReferencesForce = true;") < fetch.index(
        "this._deferProjectBackedRefresh([\"references\"], reason);")

    replay = _method(widget, "_replayDeferredProjectBackedRefresh",
                     "_schedulePostMutationSceneRefresh")
    assert "const forced = this._deferredReferencesForce === true;" in replay
    # Consumed exactly once, or a single forced request replays forever.
    assert "this._deferredReferencesForce = false;" in replay
    assert replay.index("const forced =") < replay.index(
        "this._deferredReferencesForce = false;")
    # The sidebar check survives as one DISJUNCT, not as the whole gate.
    assert "if (forced || (this.isFullscreen" in replay
    assert 'fullscreenSidebarContent === "references")) {' in replay
    # And the drop path must still exist for the genuinely unwanted case.
    assert "this._referencesDirty = true;" in replay

    # The Prompt tool is the consumer that regressed, so pin that it asks.
    show_panel = _method(widget, "_showPromptManagementPanel",
                         "_channelTemplateSwitchImpact")
    assert 'this._fetchReferences({ force: true, reason: "prompt_context" })' in show_panel


def test_fullscreen_background_paste_preserves_gallery_image_path():
    widget = _source("web/js/editor_widget.js")
    gallery = _source("web/js/shared_asset_gallery.js")
    keyboard_registration = widget[widget.index("this._editorKeyOff = registerKeyboardConsumer"):
        widget.index("// Track editor focus")]
    assert 'root.dataset.sonderAssetGallery = "1"' in gallery
    assert 'candidate?.closest?.("[data-sonder-asset-gallery=\'1\']")' in keyboard_registration
    assert 'String(item?.type || "").startsWith("image/")' in keyboard_registration
    assert "if (galleryImagePaste) return false;" in keyboard_registration
    assert "if (editable) return false;" in keyboard_registration
    assert "return true;" in keyboard_registration
    assert "notifyWarning" not in keyboard_registration
    assert 'source: "fullscreen-background-paste"' not in keyboard_registration
    assert "Paste needs a text field" not in keyboard_registration
    assert "fullscreen background paste is ignored" in widget


def test_prompt_panel_writing_and_global_views_use_bound_shared_overlays():
    panel = _source("web/js/editor_prompt_panel.js")
    writing = panel[panel.index("const renderWritingView"):panel.index("const renderContextSettings")]
    assert "attachmentContext: { scene, template: host._channelTemplate() }" in writing
    assert "attachmentContext: { scene, template }," not in writing
    assert "const draftGlobalSceneSnapshot = () => sceneWithDraftGlobal(scene" in panel
    assert "const snapshot = draftGlobalSceneSnapshot();" in panel


def test_candidate_diagnostic_projection_keys_chip_errors_and_labels_window():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Context diagnostics coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_diagnostics.js").as_uri()
    payload = {
        "execution_window": {
            "render_start": 8,
            "render_end": 56,
            "generation_start": 16,
            "generation_end": 48,
            "actual_pre": 8,
            "actual_post": 8,
        },
        "errors": [
            {"code": "unresolved_audio_speaker_binding", "message": "Choose a managed speaker.",
             "attachment_id": "chip-a"},
            {"code": "profile_error", "message": "Profile is invalid."},
        ],
        "warnings": [
            {"code": "quiet", "message": "Check this chip.", "attachment_id": "chip-a"},
        ],
    }
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"console.log(JSON.stringify(mod.buildPromptContextDiagnostics({json.dumps(payload)})));\n"
    )
    projected = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert projected["windowLabel"] == (
        "Effective render window: frames 8–56 (selection 16–48; pre 8f, post 8f).")
    assert [row["code"] for row in projected["byAttachment"]["chip-a"]] == [
        "unresolved_audio_speaker_binding", "quiet"]
    assert [row["code"] for row in projected["general"]] == ["profile_error"]
    assert projected["errorCount"] == 2
    assert projected["warningCount"] == 1


def test_candidate_selection_matches_full_scene_queue_semantics():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Context selection coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_diagnostics.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        "console.log(JSON.stringify(["
        "mod.resolvePromptCandidateSelection(0,0,96),"
        "mod.resolvePromptCandidateSelection(12,48,96),"
        "mod.resolvePromptCandidateSelection(120,-4,96)]));\n"
    )
    values = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert values == [
        {"selectionStart": 0, "selectionEnd": 96},
        {"selectionStart": 12, "selectionEnd": 48},
        {"selectionStart": 0, "selectionEnd": 96},
    ]


def test_prompt_format_lifecycle_seeds_and_versions_follow_immutable_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Format lifecycle coverage")
    module_url = (ROOT / "web" / "js" / "editor_prompt_panel.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        "const fresh = mod.freshPromptFormatDefinition({id:'house',channels:["
        "{key:'vision'},{key:'sound'}]});\n"
        "const universal = mod.promptFormatTemplateBinding("
        "{compatible_templates:['*']},{template_id:'wrong'},'*');\n"
        "const multi = mod.promptFormatTemplateBinding("
        "{compatible_templates:['one','two']},{template_id:'wrong'},'*');\n"
        "console.log(JSON.stringify({fresh,universal,multi,versions:["
        "mod.nextPromptFormatVersion('2'),mod.nextPromptFormatVersion('beta')]}));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True,
    ).stdout)
    assert result["fresh"]["template_id"] == "house"
    assert result["fresh"]["capabilities"]["custom"] == {
        "channel_key": "vision", "placement": "inline", "formatter": "{text}"}
    assert result["fresh"]["validators"] == []
    assert result["fresh"]["physical_populations"] == []
    assert result["fresh"]["identity_kinds"] == []
    assert result["universal"] == {"template_id": "standard"}
    assert result["multi"] == {
        "template_id": "one", "compatible_templates": ["one", "two"]}
    assert result["versions"] == ["3", "beta.1"]


def test_live_prompt_draft_overlays_preserve_section_identity_and_global_scope():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt draft overlay coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    scene = {
        "prompt_sections": [{
            "prompt_id": "one", "start_frame": 0, "end_frame": 20,
            "muted": True, "global_channel_exceptions": ["speech"],
            "attachments": [{"attachment_id": "old", "kind": "custom"}],
        }],
        "global_attachments": [{"attachment_id": "global-old", "kind": "custom"}],
    }
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"const scene = {json.dumps(scene)};\n"
        "const section = mod.sceneWithDraftSection(scene, {index:0, promptId:'one', "
        "startFrame:4, endFrame:24, attachments:[{attachment_id:'new',kind:'custom'}], "
        "channels:{visual:'draft'}, channelDocs:{visual:{nodes:[]}}});\n"
        "const fallback = mod.sceneWithDraftSection(scene, {promptId:'one', "
        "startFrame:99, endFrame:100, attachments:[]});\n"
        "const identityOnly = mod.sceneWithDraftSection(scene, {promptId:'one', attachments:[]});\n"
        "const global = mod.sceneWithDraftGlobal(scene, {"
        "attachments:[{attachment_id:'global-new',kind:'custom'}]});\n"
        "console.log(JSON.stringify({section, fallback, identityOnly, global}));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True,
    ).stdout)
    overlaid = result["section"]["prompt_sections"]
    assert len(overlaid) == 1
    assert overlaid[0]["muted"] is True
    assert overlaid[0]["global_channel_exceptions"] == ["speech"]
    assert overlaid[0]["start_frame"] == 4
    assert overlaid[0]["attachments"][0]["attachment_id"] == "new"
    assert len(result["fallback"]["prompt_sections"]) == 1
    assert result["identityOnly"]["prompt_sections"][0]["start_frame"] == 0
    assert result["identityOnly"]["prompt_sections"][0]["end_frame"] == 20
    assert len(result["global"]["prompt_sections"]) == 1
    assert result["global"]["global_attachments"][0]["attachment_id"] == "global-new"


def test_new_section_creator_defers_chip_transactions_until_explicit_commit():
    widget = _source("web/js/editor_widget.js")
    creator = _method(widget, "_showPromptCreator", "_saveNewPromptSection")
    assert "const commit = ({ close = true } = {}) =>" in creator
    assert "if (!close) return;" in creator
    assert creator.index("if (!close) return;") < creator.index("this._saveNewPromptSection(")


def test_reference_chip_identity_inheritance_and_vocal_target_window_are_dynamic():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Reference chip authoring coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    references = [{
        "reference_id": "woman", "name": "Korean Woman", "members": [{
            "member_id": "portrait", "name": "Portrait",
            "prompt": "a poised woman in a blue coat",
        }, {
            "member_id": "inactive", "name": "Unused portrait",
            "prompt": "prose from a non-winning member",
        }],
    }]
    scene = {
        "duration_frames": 20,
        "reference_lane_count": 1,
        "reference_lane_configs": [{}],
        "reference_lane_recipes": [{"lane_id": "pictures", "recipe": {
            "soft": {"physical_population": "pictures"}}}],
        "reference_items": [{"reference_item_id": "item", "members": [{
            "entity_id": "woman", "member_id": "portrait"}]}],
        "prompt_sections": [
            {"start_frame": 0, "end_frame": 10, "attachments": [{
                "kind": "vocal_event", "source": {"subject_ids": ["subject"]}}]},
            {"start_frame": 10, "end_frame": 20, "attachments": [{
                "kind": "vocal_event", "source": {"subject_ids": ["later"]}}]},
            {"start_frame": 0, "end_frame": 10, "muted": True,
             "attachments": [{"kind": "vocal_event",
                              "source": {"subject_ids": ["muted"]}}]},
        ],
    }
    units = [{
        "semantic_unit_id": "subject", "name": "Korean Woman", "definition": "",
        "sources": [
            {"entity_id": "woman", "member_id": "portrait"},
            {"entity_id": "woman", "member_id": "inactive"},
        ],
    }]
    attachment = {"kind": "reference", "source": {
        "semantic_unit_ids": ["subject"]}, "config": {"label": "stale label"}}
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"const refs = {json.dumps(references)};\n"
        f"const scene = {json.dumps(scene)};\n"
        f"const units = {json.dumps(units)};\n"
        f"const attachment = {json.dumps(attachment)};\n"
        "const options = {scene, references: refs, semanticUnits: units};\n"
        "const identity = mod.resolveReferenceAttachmentIdentity(attachment, options);\n"
        "const inherited = mod.resolveReferenceSelectionInheritance('subject', options);\n"
        "const physicalInherited = mod.resolveReferenceSelectionInheritance("
        "'physical:picture:portrait', options);\n"
        "const label = mod.attachmentLabel(attachment, '<Subject 1> preview', identity);\n"
        "console.log(JSON.stringify({identity, inherited, physicalInherited, label}));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert result == {
        "identity": "Korean Woman",
        "inherited": {
            "value": "a poised woman in a blue coat",
            "source": "Library member Korean Woman · Portrait",
        },
        # A physical selection resolves its own member's prose. This used to be
        # {"value": "", "source": ""} because the lookup only matched semantic
        # unit ids, so the chip claimed no inherited definition existed while
        # the member's Defaults panel plainly held one.
        "physicalInherited": {
            "value": "a poised woman in a blue coat",
            "source": "Library member Korean Woman · Portrait",
        },
        "label": "Korean Woman — <Subject 1> preview",
    }


def test_every_reference_chip_surface_uses_runtime_identity_and_authoring_controls():
    chips = _source("web/js/prompt_context_chips.js")
    widget = _source("web/js/editor_widget.js")
    panel = _source("web/js/editor_prompt_panel.js")
    label_function = chips[chips.index(
        "export function attachmentLabel"):chips.index("function chipCss")]
    reference_case = label_function[:label_function.index(
        'if (["prompt_link", "prompt_link_scope"].includes(attachment?.kind))')]
    assert "attachment?.config?.label" not in reference_case
    # The render-signature call mirrors the label resolver beside each
    # projection renderer, so it adds one occurrence in Widget and two in the
    # panel without adding a new user surface.
    assert widget.count("attachmentLabelFor,") == 4
    # Ten: the Writing contribution resolver is a chip surface too, and it is
    # called twice — once for channels the author has written in and once for
    # the channels a chip feeds but they have not. Omitting identity there is
    # how every Shot and every section-scoped chip came to read "Reference".
    # 9, not 10: the Writing decoration resolved its rows twice — once for
    # written channels and once for fed-but-unwritten ones — and now does
    # it through one `rowsFor` helper. A surface was merged, not lost.
    assert panel.count("attachmentLabelFor,") == 11
    assert "Shared identity default · @" in chips
    assert "Prompt Format default ·" in chips
    assert "no managed Vocal Event in this window" in chips
    assert "managedVocalEventSubjectIds" not in chips
    assert widget.count("managedSpeakerSubjectIds:") == 3
    assert panel.count("managedSpeakerSubjectIds:") == 7
    # The chip fieldset's field list, labels, and help now come from the format
    # declaration, so there is no call shape to grep for. Behavioural coverage
    # lives in test_prompt_context_corrections.py::
    # test_reference_fieldset_renders_only_declared_fields_with_declared_copy.
    assert "const routeKey = String(current.kind || capabilityId);" in chips
    assert "referenceDerived?.[routeKey]?.channel_key" in chips
    assert "else delete capability.channel_key;" in chips
    assert "Creative definition (never generated)" not in panel


def test_reference_backed_subject_dependency_audit_and_delete_guard_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Subject dependency audit coverage")
    identity_source = _source("web/js/prompt_identity_panel.js")
    module_url = (ROOT / "web" / "js" / "prompt_identity_panel.js").as_uri()
    scenes = [{
        "global_attachments": [{
            "kind": "reference",
            "source": {"semantic_unit_ids": ["unit:authored"]},
            "config": {"audio_speaker_subject_id": "unit:authored"},
        }],
        "prompt_sections": [{"attachments": [{
            "kind": "vocal_event",
            "source": {"subject_ids": ["unit:authored"]},
        }]}],
    }]
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"console.log(JSON.stringify(mod.promptIdentityDependents("
        f"'unit:authored', {json.dumps(scenes)}, {{sources:[{{}},{{}}]}})));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "reference_chips": 1, "vocal_events": 1,
        "audio_speaker_bindings": 1,
        "source_relationships": 2,
    }
    assert "Bound chips and Vocal Events remain visible as broken links" in identity_source
    assert "source_relationships" in identity_source
    assert "save.disabled = !hasMembers" not in identity_source
    assert ".generated" not in identity_source


def test_prompt_context_pure_helpers_pin_eligibility_reuse_projection_and_suppression():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt Context helper coverage")
    module_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const base = {{
  attachment_id: "a-late", emission_group_id: "shared", kind: "custom",
  enabled: true, config: {{text: "soft blue edge light"}},
  capabilities: [{{capability_id: "prefix", kind: "custom", placement: "section_prefix", enabled: true}}]
}};
const early = {{...structuredClone(base), attachment_id: "a-early"}};
const scene = {{prompt_sections: [
  {{prompt_id: "later", start_frame: 20, attachments: [base]}},
  {{prompt_id: "early", start_frame: 0, attachments: [early]}}
]}};
const deduped = mod.dedupeReusableAttachments([base, early], {{scene}});
const labels = [
  mod.attachmentReuseLabel({{kind: "reference"}}),
  mod.attachmentReuseLabel({{kind: "shot", config: {{timestamp: true}}}}),
  mod.attachmentReuseLabel({{kind: "timestamp"}}),
  mod.attachmentReuseLabel(base),
  mod.attachmentReuseLabel({{kind: "prompt_link", source: {{prompt_id: "early", channel_key: "visual"}}}}, {{scene, template: {{channels: [{{key: "visual", label: "Visual"}}]}}}}),
  mod.attachmentReuseLabel({{kind: "prompt_link"}}),
  mod.attachmentReuseLabel({{kind: "guide", config: {{text: "first frame"}}}}),
  mod.attachmentReuseLabel({{kind: "vocal_event", config: {{event_type: "speech"}}}})
];
const configured = {{...structuredClone(base), capabilities: [{{...base.capabilities[0], enabled: true}}]}};
const target = {{...structuredClone(base), attachment_id: "target", capabilities: [{{...base.capabilities[0], enabled: false}}]}};
const propagated = mod.propagateLinkedPromptAttachment(configured, target, "a-late");
const source = mod.propagateLinkedPromptAttachment(configured, base, "a-late");
const suppressed = mod.setPromptAttachmentCapabilityEnabled(base, {{capability_id: "prefix"}}, false);
const eligibility = mod.subjectSourceEligibility({{sources: [{{member_id: "not-staged"}}]}});
const assetlessEligibility = mod.subjectSourceEligibility({{sources: []}});
const split = mod.splitCapabilityProjectionsByRegion([
  {{capability_id: "suffix", region: "after", order: 2}},
  {{capability_id: "prefix", region: "before", order: 1}}
]);
console.log(JSON.stringify({{
  deduped: deduped.map((row) => [row.attachment.attachment_id, row.count]),
  labels,
  propagatedEnabled: propagated.capabilities[0].enabled,
  sourceEnabled: source.capabilities[0].enabled,
  suppressedEnabled: suppressed.capabilities[0].enabled,
  eligibility,
  assetlessEligibility,
  split: {{before: split.before.map((row) => row.capability_id), after: split.after.map((row) => row.capability_id)}}
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)

    assert result["deduped"] == [["a-early", 2]]
    assert len(result["labels"]) == 8
    assert result["labels"][5] == "Linked prompt"
    assert all(label and "a-early" not in label and "a-late" not in label
               for label in result["labels"])
    assert result["propagatedEnabled"] is False
    assert result["sourceEnabled"] is True
    assert result["suppressedEnabled"] is False
    assert result["eligibility"] == {
        "eligible": False,
        "appliesNow": False,
        "reason": "not staged in this scene",
        "suffix": " - not staged in this scene",
    }
    assert result["assetlessEligibility"] == {
        "eligible": True, "appliesNow": True, "reason": "", "suffix": "",
    }
    assert result["split"] == {"before": ["prefix"], "after": ["suffix"]}


def test_attach_dialog_authors_overrides_and_rides_the_same_batch():
    """Configure before attaching, not attach-then-hunt-for-the-chip.

    The dialog used to ask only WHERE — the least interesting question — at the
    one moment the author had full context, then went silent while every
    remaining decision hid behind an unadvertised click on the chip it had just
    created.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for attach dialog coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.childNodes=this.children; this.style={{cssText:"",setProperty(){{}}}};
    this.dataset={{}}; this.attributes={{}}; this.options=[]; this.value="";
    this.textContent=""; this.title=""; this.disabled=false; this.multiple=false;
    this._handlers={{}}; }}
  appendChild(c) {{ this.children.push(c); c.parentElement=this;
    if (c.tagName === "OPTION") this.options.push(c); return c; }}
  append(...cs) {{ cs.forEach((c) => c?.tagName && this.appendChild(c)); }}
  addEventListener(t,h) {{ (this._handlers[t] ||= []).push(h); }}
  removeEventListener() {{}}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
  getAttribute(k) {{ return this.attributes[k] ?? null; }}
  insertAdjacentElement(_pos, el) {{ this.parentElement?.appendChild(el); return el; }}
  querySelector() {{ return null; }}
  querySelectorAll() {{ return []; }}
  get selectedOptions() {{ return this.options.filter((o) =>
    o.selected === true || (!this.multiple && String(o.value) === String(this.value))); }}
  focus() {{}}
  remove() {{ const p=this.parentElement; if(!p) return;
    p.children.splice(p.children.indexOf(this),1); }}
}}
globalThis.document={{createElement:(t)=>new N(t),
  createTextNode:(t)=>({{nodeType:3,nodeValue:t}}),
  body:new N("body"),activeElement:null}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
globalThis.CSS={{escape:(v)=>String(v)}};
globalThis.Node={{TEXT_NODE:3}};
const mod=await import({json.dumps(module_url)});
const calls=[];
const root=new N("div");
mod.mountPromptIdentityPanel(root, {{
  profile: {{
    physical_populations:[{{key:"pictures",label:"Picture",
      source_key:"picture_ids",label_template:"<Picture {{n}}>"}}],
    identity_kinds:[{{key:"subject",label:"Subject"}}],
    capabilities:{{reference:{{derived:{{
      definitions:{{order:1,channel_key:"defs",placement:"section_prefix",
        label:"Portrayal",help:"Declared guidance.",fields:{{}}}},
    }}}}}},
  }},
  candidate: {{setup_manifest:{{pictures:[{{member_id:"member-1",
    asset_id:"asset-1",role:"identity",slot_number:1}}]}}}},
  references:[{{reference_id:"reference-1",name:"Woman",members:[
    {{member_id:"member-1",name:"Portrait",asset_id:"asset-1",
      handle:"CharacterSheet",prompt:"library prose"}}]}}],
  semanticUnits:[],
  scene:{{prompt_sections:[{{start_frame:0,end_frame:100}}]}},
  attachReference: async (owner,target,overrides) => {{
    calls.push({{ownerType:owner?.type,scope:target?.scope,overrides}}); return true; }},
}});
const find=(pred)=>{{ const out=[]; const walk=(n)=>{{ if(pred(n)) out.push(n);
  n.children.forEach(walk); }}; walk(root); walk(document.body); return out; }};
const rowAttach=find((n)=>n.tagName==="BUTTON"&&n.textContent==="Attach...")[0];
rowAttach._handlers.click.forEach((h)=>h());
const dialog=document.body.children.find((n)=>n.dataset?.promptAttachmentTarget);
const fieldsetHost=find((n)=>n.dataset?.sonderAttachFieldset)[0];
const textareas=find((n)=>["TEXTAREA","INPUT"].includes(n.tagName)
  && n.parentElement && n.type!=="checkbox");
const definition=textareas.find((n)=>n.tagName==="TEXTAREA");
definition.value="authored at attach time";
(definition._handlers.input||[]).forEach((h)=>h());
const dialogAttach=find((n)=>n.tagName==="BUTTON"&&n.textContent==="Attach")[0];
await dialogAttach._handlers.click[0]();
console.log(JSON.stringify({{
  dialogOpened: Boolean(dialog),
  fieldsetPresent: Boolean(fieldsetHost),
  guarded: typeof dialog?._handlers?.click?.[0] === "function",
  calls,
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["dialogOpened"] is True
    assert result["fieldsetPresent"] is True, (
        "The Attach dialog must carry the declared override fieldset")
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["ownerType"] == "physical"
    assert call["scope"] == "global"
    # Authored in the dialog, forwarded to the same scene batch as the
    # attachment so one Undo removes both.
    assert call["overrides"] == {"definition": "authored at attach time"}


def test_handle_suggestions_are_typeable_and_sanitize_live():
    """A handle is what an author TYPES while writing a prompt.

    Camel-casing every filename token produced `ChatGPTImageAug112026120009PM`
    — technically valid and unusable at the exact moment it matters.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for handle coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        "const derive = mod.derivePromptHandleSuggestion;\n"
        "console.log(JSON.stringify({\n"
        "  generatedFilename: derive('ChatGPT Image Aug 11 2026 12 00 09 PM'),\n"
        "  ordinaryName: derive('Korean Woman'),\n"
        "  longPhrase: derive('a poised woman in a long blue winter coat'),\n"
        "  digitsOnly: derive('20260811120009'),\n"
        "  empty: derive('', 'Identity'),\n"
        "  sanitizeSpaces: mod.sanitizePromptHandle('Korean Woman 2'),\n"
        "  sanitizeLeadingDigit: mod.sanitizePromptHandle('2Fast'),\n"
        "  sanitizePunctuation: mod.sanitizePromptHandle('a-b_c!d'),\n"
        "  rule: mod.PROMPT_HANDLE_RULE,\n"
        "}));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    # Date/time/AM-PM debris is dropped and the result is capped at three words.
    assert result["generatedFilename"] == "ChatGPTImage"
    assert result["ordinaryName"] == "KoreanWoman"
    assert result["longPhrase"] == "APoisedWoman"
    # Nothing meaningful survives, so the fallback keeps it valid.
    assert result["digitsOnly"] == "Ref20260811120009"
    assert result["empty"] == "Identity"
    # Live sanitization encodes the same rule the server refusal enforces.
    assert result["sanitizeSpaces"] == "KoreanWoman2"
    assert result["sanitizeLeadingDigit"] == "Ref2Fast"
    assert result["sanitizePunctuation"] == "abcd"
    assert "starting with a letter" in result["rule"]


def test_unstored_handles_render_as_suggestions_without_mutating_project_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for handle-state DOM coverage")
    identity_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.value=""; this.placeholder=""; this.textContent=""; this._handlers={{}};
    this.classNames=[]; this.classList={{add:(value)=>this.classNames.push(value)}}; }}
  appendChild(c) {{ this.children.push(c); c.parentElement=this; return c; }}
  append(...cs) {{ cs.forEach((c)=>c?.tagName && this.appendChild(c)); }}
  addEventListener(t,h) {{ (this._handlers[t] ||= []).push(h); }}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
  querySelector() {{ return null; }}
}}
globalThis.document={{createElement:(t)=>new N(t),body:new N("body"),activeElement:null}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
globalThis.localStorage={{getItem(){{return null;}},setItem(){{}}}};
globalThis.CSS={{escape:(v)=>String(v)}};
const mod=await import({json.dumps(identity_url)});
const root=new N("div"); let referenceMutations=0; let identitySaves=0;
mod.mountPromptIdentityPanel(root, {{
  profile: {{
    physical_populations:[{{key:"pictures",label:"Picture",source_key:"picture_ids",label_template:"<Picture {{n}}>"}}],
    identity_kinds:[{{key:"subject",label:"Subject"}}],
  }},
  candidate: {{setup_manifest:{{pictures:[{{member_id:"member-1",asset_id:"asset-1",slot_number:1}}]}}}},
  references:[{{reference_id:"reference-1",name:"Cast",members:[
    {{member_id:"member-1",name:"Portrait One",asset_id:"asset-1"}}]}}],
  assets:[{{asset_id:"asset-1",asset_type:"image"}}],
  semanticUnits:[
    {{semantic_unit_id:"unstored",name:"Korean Woman",kind:"subject",sources:[]}},
    {{semantic_unit_id:"stored",name:"Sailor",handle:"Captain",kind:"subject",sources:[]}},
  ],
  mutateReferences:async()=>{{referenceMutations += 1;}},
  saveSemanticUnitChange:async()=>{{identitySaves += 1;}},
}});
const walk=(n,out=[])=>{{out.push(n);n.children.forEach((c)=>walk(c,out));return out;}};
const text=(n)=>[n.textContent,...n.children.map(text)].join("").trim();
const nodes=walk(root);
const physical=nodes.find((n)=>n.tagName==="INPUT"
  && n.attributes["aria-label"]==="Physical Reference handle");
const identityRows=nodes.filter((n)=>n.dataset?.promptingRow==="identity")
  .map((row)=>text(row.children[1]));
console.log(JSON.stringify({{
  physical:{{value:physical.value,placeholder:physical.placeholder,
    classes:physical.classNames}},
  identityRows,referenceMutations,identitySaves,
  unstoredPicker:mod.promptReferencePickerLabel({{name:"Cast"}},{{name:"Portrait One"}}),
  storedPicker:mod.promptReferencePickerLabel({{name:"Cast"}},{{name:"Portrait One",handle:"Portrait"}}),
  owner:mod.promptIdentityAttachmentOwner({{semantic_unit_id:"unstored",name:"Korean Woman"}}),
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["physical"]["value"] == ""
    assert result["physical"]["placeholder"] == "PortraitOne"
    assert result["physical"]["classes"] == ["sonder-chrome-dim-placeholder"]
    assert result["identityRows"] == [
        "Korean Woman· no handle", "@Captain — Sailor"]
    assert result["unstoredPicker"] == "Portrait One"
    assert result["storedPicker"] == "@Portrait — Portrait One"
    assert result["owner"] == {
        "type": "identity", "identityId": "unstored",
        "handle": "KoreanWoman", "storedHandle": "",
        "displayName": "Korean Woman"}
    # Projection/render is read-only. Suggestions do not become project state
    # until an explicit edit or Attach action invokes a mutation seam.
    assert result["referenceMutations"] == 0
    assert result["identitySaves"] == 0


def test_identity_attach_materializes_the_suggested_handle_through_save_seam():
    panel_url = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    result = _run_node(f"""
const mod=await import({json.dumps(panel_url)});
const units=[{{semantic_unit_id:"unit",name:"Korean Woman",kind:"subject",sources:[]}}];
const calls=[];
const result=await mod.materializePromptIdentityOwner({{
  type:"identity",identityId:"unit",handle:"KoreanWoman",storedHandle:"",
}}, units, async(change,label,options)=>{{calls.push({{change,label,options}});return [change.value];}},
{{recordUndo:false}});
const adoptedCalls=[];
const adopted=await mod.materializePromptIdentityOwner({{
  type:"identity",identityId:"unit",handle:"StaleSuggestion",storedHandle:"",
}}, [{{...units[0],handle:"StoredNow"}}], async(...args)=>adoptedCalls.push(args));
let changedError="";
try {{
  await mod.materializePromptIdentityOwner({{
    type:"identity",identityId:"unit",handle:"StoredBefore",storedHandle:"StoredBefore",
  }}, [{{...units[0],handle:"StoredNow"}}], async()=>true);
}} catch(error) {{ changedError=error.message; }}
console.log(JSON.stringify({{result,calls,units,adopted,adoptedCalls,changedError}}));
""")
    assert result["calls"][0]["label"] == "materialize prompt identity handle"
    assert result["calls"][0]["options"] == {"recordUndo": False}
    change = result["calls"][0]["change"]
    assert change["type"] == "upsert"
    assert "handle" not in change["expected"]
    assert change["value"]["handle"] == "KoreanWoman"
    assert result["result"]["owner"]["storedHandle"] == "KoreanWoman"
    assert result["adopted"]["owner"]["storedHandle"] == "StoredNow"
    assert result["adoptedCalls"] == []
    assert "changed elsewhere" in result["changedError"]
    # The renderer/helper does not mutate its input snapshot in place.
    assert "handle" not in result["units"][0]


def test_failed_identity_attach_uses_targeted_rollback_or_keeps_recovery_undo():
    panel_url = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    result = _run_node(f"""
const mod=await import({json.dumps(panel_url)});
const history={{
  promptIdentityChange:{{type:"upsert",value:{{semantic_unit_id:"u"}},
    expected:{{semantic_unit_id:"u",handle:"Person"}}}},
  inversePromptIdentityChange:{{type:"upsert",
    value:{{semantic_unit_id:"u",handle:"Person"}},
    expected:{{semantic_unit_id:"u"}}}},
}};
const successCalls=[];
const success={{_applyPromptIdentityChange:async(...args)=>successCalls.push(args),
  _pushPromptIdentityUndo:()=>{{throw new Error("unexpected recovery");}}}};
await mod.rollbackPromptIdentityAttachment(success,history);
const recovery=[];
const failed={{_applyPromptIdentityChange:async()=>{{throw new Error("offline");}},
  _pushPromptIdentityUndo:(...args)=>recovery.push(args)}};
let failedError="";
try {{ await mod.rollbackPromptIdentityAttachment(failed,history); }}
catch(error) {{ failedError=error.message; }}
const changedRecovery=[];
const changed={{_applyPromptIdentityChange:async()=>{{const error=new Error("changed");
  error.code="prompt_identity_changed_elsewhere"; throw error;}},
  _pushPromptIdentityUndo:(...args)=>changedRecovery.push(args)}};
let changedError="";
try {{ await mod.rollbackPromptIdentityAttachment(changed,history); }}
catch(error) {{ changedError=error.message; }}
const lostResponseRecovery=[];
const lostResponse={{_promptSemanticUnits:[history.promptIdentityChange.value],
  _applyPromptIdentityChange:async()=>{{throw new Error("lost response");}},
  _fetchReferences:async()=>({{ok:true}}),
  _pushPromptIdentityUndo:(...args)=>lostResponseRecovery.push(args)}};
const lostResponseResult=await mod.rollbackPromptIdentityAttachment(lostResponse,history);
console.log(JSON.stringify({{successCalls,recovery,failedError,changedRecovery,changedError,
  lostResponseRecovery,lostResponseResult}}));
""")
    assert result["successCalls"][0][1:] == [
        "rollback refused prompt identity attachment", {"recordUndo": False}]
    assert result["recovery"][0] == [
        "materialize prompt identity handle",
        {
            "type": "upsert",
            "value": {"semantic_unit_id": "u"},
            "expected": {"semantic_unit_id": "u", "handle": "Person"},
        },
        {
            "type": "upsert",
            "value": {"semantic_unit_id": "u", "handle": "Person"},
            "expected": {"semantic_unit_id": "u"},
        },
    ]
    assert "remains undoable" in result["failedError"]
    assert "offline" in result["failedError"]
    assert result["changedRecovery"] == []
    assert "changed elsewhere and was left untouched" in result["changedError"]
    assert "undoable" not in result["changedError"]
    assert result["lostResponseRecovery"] == []
    assert result["lostResponseResult"] is True


def test_failed_physical_attach_uses_targeted_rollback_or_keeps_recovery_undo():
    panel_url = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    result = _run_node(f"""
const mod=await import({json.dumps(panel_url)});
const history={{referenceOperations:[{{type:"update_member",member_id:"m",
  fields:{{handle:""}},expected:{{handle:"Portrait"}}}}],
  inverseReferenceOperations:[{{type:"update_member",member_id:"m",
  fields:{{handle:"Portrait"}},expected:{{handle:""}}}}]}};
const calls=[];
await mod.rollbackPromptPhysicalAttachment(
  {{_mutateReferences:async(...args)=>calls.push(args)}},history);
const recovery=[];
let failedError="";
try {{ await mod.rollbackPromptPhysicalAttachment({{
  _mutateReferences:async()=>{{throw new Error("offline");}},
  _pushReferenceUndo:(...args)=>recovery.push(args),
}},history); }} catch(error) {{ failedError=error.message; }}
const changedRecovery=[]; let changedError="";
try {{ await mod.rollbackPromptPhysicalAttachment({{
  _mutateReferences:async()=>{{const error=new Error("changed");
    error.code="identity_mismatch"; throw error;}},
  _pushReferenceUndo:(...args)=>changedRecovery.push(args),
}},history); }} catch(error) {{ changedError=error.message; }}
const lostResponseRecovery=[];
const lostResponse={{_references:[{{members:[{{member_id:"m",handle:""}}]}}],
  _mutateReferences:async()=>{{throw new Error("lost response");}},
  _fetchReferences:async()=>({{ok:true}}),
  _pushReferenceUndo:(...args)=>lostResponseRecovery.push(args)}};
const lostResponseResult=await mod.rollbackPromptPhysicalAttachment(lostResponse,history);
console.log(JSON.stringify({{calls,recovery,failedError,changedRecovery,changedError,
  lostResponseRecovery,lostResponseResult}}));
""")
    assert result["calls"][0][1] == (
        "rollback refused prompt Reference attachment")
    assert result["recovery"][0][0] == "materialize prompt Reference handle"
    assert result["recovery"][0][1:] == [
        [{"type": "update_member", "member_id": "m",
          "fields": {"handle": ""}, "expected": {"handle": "Portrait"}}],
        [{"type": "update_member", "member_id": "m",
          "fields": {"handle": "Portrait"}, "expected": {"handle": ""}}],
    ]
    assert "remains undoable" in result["failedError"]
    assert result["changedRecovery"] == []
    assert "changed elsewhere and was left untouched" in result["changedError"]
    assert result["lostResponseRecovery"] == []
    assert result["lostResponseResult"] is True


def test_composite_scene_undo_restores_identity_dependencies_in_one_step():
    widget = _source("web/js/editor_widget.js")
    push = _method(widget, "_pushUndo", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyWarning=()=>{{}};
class Harness {{
{push}
{undo}
{redo}
  constructor() {{
    this.activeSceneId="scene"; this.activeScene={{attachments:[]}};
    this.currentDependencies={{prompt_semantic_units:[{{semantic_unit_id:"u"}}]}};
    this._undoStack=[]; this._redoStack=[]; this._maxUndoSteps=20;
    this._editorFocused=false; this.events=[];
  }}
  _keyboardDebug() {{}}
  async _applyPromptIdentityChange(change) {{
    this.events.push(["identity",change.value?.handle || ""]);
    this.currentDependencies={{prompt_semantic_units:[structuredClone(change.value)]}};
  }}
  async _restoreScene(_sceneId,value) {{
    this.events.push(["scene",value.attachments.length]);
    this.activeScene=structuredClone(value);
  }}
  async _mutateReferences() {{}}
}}
const h=new Harness();
const before={{semantic_unit_id:"u"}};
const after={{semantic_unit_id:"u",handle:"KoreanWoman"}};
h._pushUndo("attach prompt Reference",{{
  promptIdentityChange:{{type:"upsert",value:before,expected:after}},
  inversePromptIdentityChange:{{type:"upsert",value:after,expected:before}},
}});
h.activeScene={{attachments:[{{attachment_id:"chip"}}]}};
h.currentDependencies={{prompt_semantic_units:[structuredClone(after)]}};
const depthAfterAttach=h._undoStack.length;
await h._undo();
const afterUndo={{scene:h.activeScene,deps:h.currentDependencies,
  undo:h._undoStack.length,redo:h._redoStack.length,events:[...h.events]}};
h.events=[];
await h._redo();
console.log(JSON.stringify({{depthAfterAttach,afterUndo,afterRedo:{{
  scene:h.activeScene,deps:h.currentDependencies,
  undo:h._undoStack.length,redo:h._redoStack.length,events:h.events,
}}}}));
""")
    assert result["depthAfterAttach"] == 1
    assert result["afterUndo"]["scene"]["attachments"] == []
    assert "handle" not in result["afterUndo"]["deps"]["prompt_semantic_units"][0]
    assert result["afterUndo"]["undo"] == 0
    assert result["afterUndo"]["redo"] == 1
    assert result["afterUndo"]["events"] == [
        ["identity", ""], ["scene", 0]]
    assert result["afterRedo"]["scene"]["attachments"] == [
        {"attachment_id": "chip"}]
    assert result["afterRedo"]["deps"]["prompt_semantic_units"][0]["handle"] == (
        "KoreanWoman")
    assert result["afterRedo"]["undo"] == 1
    assert result["afterRedo"]["redo"] == 0
    assert result["afterRedo"]["events"] == [
        ["identity", "KoreanWoman"], ["scene", 1]]


def test_semantic_unit_save_can_defer_undo_to_composite_scene_history():
    widget = _source("web/js/editor_widget.js")
    save = _method(widget, "_savePromptSemanticUnits", "_applyReferencePayload")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};
globalThis.fetchProjectJson=async()=>({{payload:{{prompt_semantic_units:[{{handle:"Stored"}}]}}}});
class Harness {{
{save}
  constructor() {{ this.projectDir="project"; this.undo=[]; this.fetches=0; }}
  _captureProjectDependencies() {{ return {{prompt_semantic_units:[]}}; }}
  _projectDirName() {{ return "project"; }}
  _pushProjectDependencyUndo(...args) {{ this.undo.push(args); }}
  async _fetchReferences() {{ this.fetches += 1; }}
}}
const deferred=new Harness();
await deferred._savePromptSemanticUnits([{{handle:"Stored"}}],"materialize",{{recordUndo:false}});
const normal=new Harness();
await normal._savePromptSemanticUnits([{{handle:"Stored"}}],"materialize");
console.log(JSON.stringify({{deferredUndo:deferred.undo.length,normalUndo:normal.undo.length,
  deferredFetches:deferred.fetches,normalFetches:normal.fetches}}));
""")
    assert result == {
        "deferredUndo": 0, "normalUndo": 1,
        "deferredFetches": 1, "normalFetches": 1,
    }


def test_failed_scene_commit_can_discard_its_exact_interleaved_undo_entry():
    widget = _source("web/js/editor_widget.js")
    push = _method(widget, "_pushUndo", "_captureProjectDependencies")
    discard = _method(widget, "_discardUndoEntry", "_trimLocalLaneConfigs")
    result = _run_node(f"""
class Harness {{
{push}
{discard}
  constructor() {{ this.activeSceneId="scene"; this.activeScene={{}};
    this._undoStack=[]; this._redoStack=[]; this._maxUndoSteps=20; }}
}}
const h=new Harness();
const failed=h._pushUndo("attach prompt Reference");
const concurrent=h._pushUndo("concurrent edit");
const removed=h._discardUndoEntry(failed);
console.log(JSON.stringify({{removed,labels:h._undoStack.map((entry)=>entry.label),
  concurrentRetained:h._undoStack[0]===concurrent}}));
""")
    assert result == {
        "removed": True,
        "labels": ["concurrent edit"],
        "concurrentRetained": True,
    }


def test_post_commit_refresh_failure_cannot_trigger_durable_compensation():
    panel = _source("web/js/editor_prompt_panel.js")
    start = panel.index(
        "const commit = async (operations, label, history = {}, lifecycleToken = null)")
    end = panel.index('profile.addEventListener("change"', start)
    commit = panel[start:end]
    reserved = commit.index("host._beginSceneHistoryLifecycle")
    mutation = commit.index("await host._runSceneMutation")
    refused = commit.index("prompt-context-refused")
    committed = commit.index("host._commitUndoEntry")
    refresh = commit.index("await host._fetchScenes")
    refresh_token = commit.index("sceneHistoryLifecycleToken: token", refresh)
    refresh_failed = commit.index("prompt-context-refresh-failed")
    released = commit.index("host._endSceneHistoryLifecycle")
    assert (reserved < mutation < refused < committed < refresh < refresh_token
            < refresh_failed < released)
    assert commit.count("return false;") == 2
    assert commit.rindex("return true;") > refresh_failed


def test_prompt_commit_lifecycle_reservation_releases_only_owned_tokens():
    panel = _source("web/js/editor_prompt_panel.js")
    start = panel.index(
        "const commit = async (operations, label, history = {}, lifecycleToken = null)")
    end = panel.index('profile.addEventListener("change"', start)
    commit = panel[start:end]
    result = _run_node(f"""
const warnings=[]; globalThis.notifyWarning=(message)=>warnings.push(message);
const render=()=>{{host.renders += 1;}};
const host={{owner:null,begins:0,ends:0,renders:0,mutations:0,commits:0,discards:0,
  failMutation:false,failRefresh:false,
  _beginSceneHistoryLifecycle(label){{if(this.owner)return null;
    this.begins += 1; this.owner={{label}}; return this.owner;}},
  _endSceneHistoryLifecycle(token){{if(this.owner!==token)return false;
    this.ends += 1; this.owner=null; return true;}},
  _pushUndo(){{return {{pending:true}};}},
  _commitUndoEntry(entry){{entry.pending=false;this.commits += 1;}},
  _discardUndoEntry(){{this.discards += 1;}},
  async _runSceneMutation(){{this.mutations += 1;
    if(this.failMutation)throw new Error("refused");
    if(!this.owner)throw new Error("mutation ran without reservation");}},
  async _fetchScenes(options){{if(options.sceneHistoryLifecycleToken!==this.owner)
    throw new Error("refresh ran without its exact reservation");
    if(this.failRefresh)throw new Error("refresh refused");return true;}},
  activeSceneId:"A",
}};
{commit}
const success=await commit([],"save");
const outer=host._beginSceneHistoryLifecycle("attach");
const borrowed=await commit([],"attach",{{}},outer);
const retained=host.owner===outer;
host._endSceneHistoryLifecycle(outer);
host.failMutation=true;
const refused=await commit([],"refused");
host.failMutation=false;host.failRefresh=true;
const refreshFailed=await commit([],"refresh failed");
console.log(JSON.stringify({{success,borrowed,retained,refused,refreshFailed,owner:host.owner,
  begins:host.begins,ends:host.ends,mutations:host.mutations,commits:host.commits,
  discards:host.discards,renders:host.renders,warnings}}));
""")
    assert result == {
        "success": True, "borrowed": True, "retained": True, "refused": False,
        "refreshFailed": True, "owner": None, "begins": 4, "ends": 4,
        "mutations": 4, "commits": 3, "discards": 1, "renders": 2,
        "warnings": ["refused", "refresh refused"],
    }


def test_pending_scene_history_cannot_be_consumed_by_undo():
    widget = _source("web/js/editor_widget.js")
    pending_helper = _method(
        widget, "_hasPendingHistoryCommit", "_historyIsBusy")
    push = _method(widget, "_pushUndo", "_commitUndoEntry")
    commit_entry = _method(widget, "_commitUndoEntry", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const notices=[]; globalThis.notifyInfo=(message)=>notices.push(message);
globalThis.notifyWarning=()=>{{}};
class Harness {{
{pending_helper}
{push}
{commit_entry}
{undo}
{redo}
  constructor() {{ this.activeSceneId="scene"; this.activeScene={{value:"after"}};
    this._undoStack=[]; this._redoStack=[{{label:"older redo"}}]; this._maxUndoSteps=20;
    this._editorFocused=false; this.restores=0; }}
  _keyboardDebug() {{}}
  async _restoreScene(_id,snapshot) {{ this.restores += 1; this.activeScene=snapshot; }}
  async _mutateReferences() {{}}
  async _applyPromptIdentityChange() {{}}
}}
const h=new Harness();
const entry=h._pushUndo("attach",{{pending:true}});
await h._undo();
await h._redo();
const whilePending={{undo:h._undoStack.length,redo:h._redoStack.length,
  restores:h.restores,pending:entry.pending}};
const interleaved=new Harness();
const buried=interleaved._pushUndo("attach",{{pending:true}});
interleaved._pushUndo("later edit");
interleaved._redoStack=[{{label:"later redo"}}];
await interleaved._undo(); await interleaved._redo();
const whileBuried={{undo:interleaved._undoStack.map((value)=>value.label),
  redo:interleaved._redoStack.map((value)=>value.label),
  restores:interleaved.restores,pending:buried.pending}};
h._commitUndoEntry(entry);
await h._undo();
const orphaned=new Harness(); orphaned.activeSceneId="scene-b";
const orphan={{sceneId:"scene-a",snapshot:{{}},label:"attach",pending:true}};
const orphanCommitted=orphaned._commitUndoEntry(orphan);
console.log(JSON.stringify({{whilePending,whileBuried,afterCommit:{{undo:h._undoStack.length,
  redo:h._redoStack.length,restores:h.restores,pending:entry.pending}},notices,
  orphan:{{committed:orphanCommitted,undo:orphaned._undoStack.length,
  pending:orphan.pending}}}}));
""")
    assert result["whilePending"] == {
        "undo": 1, "redo": 1, "restores": 0, "pending": True}
    assert result["whileBuried"] == {
        "undo": ["attach", "later edit"],
        "redo": ["later redo"], "restores": 0, "pending": True,
    }
    assert result["afterCommit"] == {
        "undo": 0, "redo": 1, "restores": 1, "pending": False}
    assert result["notices"] == [
        "That change is still saving. Try Undo again when it finishes.",
        "That change is still saving. Try Redo again when it finishes.",
        "That change is still saving. Try Undo again when it finishes.",
        "That change is still saving. Try Redo again when it finishes.",
    ]
    assert result["orphan"] == {
        "committed": False, "undo": 0, "pending": True}


def test_refused_pending_transaction_preserves_unrelated_redo_history():
    widget = _source("web/js/editor_widget.js")
    push = _method(widget, "_pushUndo", "_commitUndoEntry")
    trim = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    discard = _method(widget, "_discardUndoEntry", "_trimLocalLaneConfigs")
    result = _run_node(f"""
class Harness {{
{push}
{trim}
{discard}
  constructor() {{ this.activeSceneId="scene"; this.activeScene={{}};
    this._undoStack=[{{label:"oldest"}},{{label:"newest"}}];
    this._redoStack=[{{label:"prior redo"}}]; this._maxUndoSteps=2; }}
}}
const h=new Harness();
const entry=h._pushUndo("attach",{{pending:true}});
const during=h._redoStack.map((value)=>value.label);
h._discardUndoEntry(entry);
console.log(JSON.stringify({{during,after:h._redoStack.map((value)=>value.label),
  undo:h._undoStack.map((value)=>value.label)}}));
""")
    assert result == {
        "during": ["prior redo"], "after": ["prior redo"],
        "undo": ["oldest", "newest"],
    }


def test_prompt_identity_history_keeps_polarity_across_cleanup_and_refresh_failures():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    transactions_url = (ROOT / "web/js/prompt_identity_transactions.js").as_uri()
    result = _run_node(f"""
const {{promptIdentityCleanupPlan,promptIdentityRedoPlan}}=
  await import({json.dumps(transactions_url)});
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const notices=[]; globalThis.notifyInfo=(message)=>notices.push(message);
globalThis.notifyWarning=(message)=>notices.push(message);
class Harness {{
{undo}
{redo}
  constructor() {{
    this.activeSceneId="scene"; this.activeScene={{scene_id:"scene",value:"applied"}};
    this._promptSemanticUnits=[{{semantic_unit_id:"one",handle:"One",name:"One",
      kind:"subject",definition:"",sources:[],voice:{{member_id:null}}}}];
    this._undoStack=[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:"old"}},
      label:"apply prompt setup",promptIdentityCreateIntents:[{{
        type:"create_prompt_semantic_unit",handle_suggestion:"One",
        unit:{{semantic_unit_id:"one",name:"One",kind:"subject",definition:""}},
        cleanup_expected:this._promptSemanticUnits[0]}}]}}];
    this._redoStack=[]; this._editorFocused=false; this._sceneHistoryLifecycleOwner=null;
    this._historyOperationInFlight=false; this.refreshWorks=false;
    this.restores=[]; this.cleanupAttempts=0; this.createAttempts=0;
  }}
  _hasPendingHistoryCommit() {{ return false; }}
  _finishHistoryOperation() {{ this._historyOperationInFlight=false; }}
  _keyboardDebug() {{}}
  async _applyReferenceHistoryOperations() {{}}
  async _applyPromptIdentityChange() {{}}
  async _restoreScene(_id,snapshot) {{
    this.restores.push(snapshot.value); this.activeScene=structuredClone(snapshot);
  }}
  async _runSceneMutation(operations) {{
    if(operations[0]?.type==="delete_prompt_semantic_unit_if_unreferenced") {{
      this.cleanupAttempts += 1; throw new Error("cleanup response lost");
    }}
    this.createAttempts += 1;
    return {{payload:{{results:[],prompt_semantic_units:this._promptSemanticUnits}}}};
  }}
  async _fetchReferences() {{ return this.refreshWorks ? {{ok:true}} : null; }}
  _adoptPromptIdentitiesFromMutation() {{}}
  _finalizePromptIdentityCreationHistory() {{}}
}}
const h=new Harness();
await h._undo();
const afterUndo={{undo:h._undoStack.length,redo:h._redoStack.length,
  redoSnapshot:h._redoStack[0].snapshot.value,
  refreshRequired:h._redoStack[0].promptIdentityRefreshRequired===true}};
await h._redo();
const afterFailedRedo={{undo:h._undoStack.length,redo:h._redoStack.length,
  retrySnapshot:h._redoStack[0].retryOpposite.snapshot.value,
  restores:[...h.restores],creates:h.createAttempts}};
h.refreshWorks=true;
await h._redo();
console.log(JSON.stringify({{afterUndo,afterFailedRedo,afterSuccess:{{
  undo:h._undoStack.length,redo:h._redoStack.length,
  undoSnapshot:h._undoStack[0].snapshot.value,
  restores:h.restores,creates:h.createAttempts}},notices}}));
""")
    assert result["afterUndo"] == {
        "undo": 0, "redo": 1, "redoSnapshot": "applied", "refreshRequired": True}
    assert result["afterFailedRedo"] == {
        "undo": 0, "redo": 1, "retrySnapshot": "old",
        "restores": ["old"], "creates": 0}
    assert result["afterSuccess"] == {
        "undo": 1, "redo": 0, "undoSnapshot": "old",
        "restores": ["old", "applied"], "creates": 0}
    assert any("cleanup could not be confirmed" in notice for notice in result["notices"])


def test_unknown_prompt_apply_keeps_pending_history_until_authoritative_reconciliation():
    widget = _source("web/js/editor_widget.js")
    methods_start = widget.index("    _reconcilePromptSetupIdentityCreates(")
    methods_end = widget.index("    async _applyPromptSetup(", methods_start)
    methods = widget[methods_start:methods_end]
    transactions_url = (ROOT / "web/js/prompt_identity_transactions.js").as_uri()
    result = _run_node(f"""
const {{promptIdentityCleanupPlan,reconcilePromptIdentityCreateOutcome}}=
  await import({json.dumps(transactions_url)});
class Harness {{
{methods}
  constructor() {{
    this.refreshWorks=false; this.commits=0;
    this._promptSemanticUnits=[{{semantic_unit_id:"one",handle:"One",name:"One",
      kind:"subject",definition:"",order:0,sources:[],voice:{{member_id:null}},
      attachment_defaults:{{}},disabled_capabilities:[]}}];
    this.activeScene={{prompt_sections:[{{prompt_id:"p",start_frame:0,end_frame:24,
      channels:{{visual:"hello"}},channel_docs:{{visual:{{nodes:[
        {{type:"text",node_id:"t",text:"hello"}}]}}}},attachments:[],muted:false,
      global_channel_exceptions:[]}}]}};
  }}
  async _fetchScenes() {{ return this.refreshWorks; }}
  async _fetchReferences() {{ return this.refreshWorks ? {{ok:true}} : null; }}
  _commitUndoEntry(entry) {{ entry.pending=false; this.commits += 1; return true; }}
}}
const h=new Harness();
const intent={{type:"create_prompt_semantic_unit",handle_suggestion:"One",
  unit:{{semantic_unit_id:"one",name:"One",kind:"subject",definition:""}}}};
const expected=structuredClone(h.activeScene.prompt_sections);
const entry={{pending:true,promptIdentityCreateIntents:[intent],
  promptIdentityExpectedSections:expected}};
const unknown=await h._refreshAndReconcilePromptSetupIdentityCreates(
  entry,[intent],expected);
h.refreshWorks=true;
const applied=await h._refreshAndReconcilePromptSetupIdentityCreates(
  entry,[intent],expected);
const cleanup=promptIdentityCleanupPlan(entry.promptIdentityCreateIntents);
const edited=reconcilePromptIdentityCreateOutcome({{
  units:[{{...h._promptSemanticUnits[0],attachment_defaults:{{summary:"changed"}}}}],
  intents:[intent],expectedSections:expected,actualSections:expected}});
console.log(JSON.stringify({{unknown,applied,pending:entry.pending,commits:h.commits,
  cleanupExpected:Boolean(entry.promptIdentityCreateIntents[0].cleanup_expected),
  cleanupUnproven:entry.promptIdentityCreateIntents[0].cleanup_unproven,
  reconciledExpected:entry.promptIdentityCreateIntents[0].reconciled_expected,
  cleanupOperations:cleanup.operations.length,
  retainedUnprovenIds:cleanup.retainedUnprovenIds,edited}}));
""")
    assert result["unknown"]["outcome_unknown"] is True
    assert result["unknown"]["applied"] is False
    assert result["applied"]["applied"] is True
    assert result["pending"] is False
    assert result["commits"] == 1
    assert result["cleanupExpected"] is False
    assert result["cleanupUnproven"] is True
    assert result["reconciledExpected"]["semantic_unit_id"] == "one"
    assert result["cleanupOperations"] == 0
    assert result["retainedUnprovenIds"] == ["one"]
    assert result["edited"]["applied"] is False
    assert result["edited"]["conflicting_prompt_semantic_unit_ids"] == ["one"]

    apply_start = widget.index("    async _applyPromptSetup(")
    apply_end = widget.index("    _getPromptTemplates()", apply_start)
    apply_method = widget[apply_start:apply_end]
    preserve = apply_method.index("if (reconciled.outcome_unknown && !knownRefusal)")
    discard = apply_method.index("this._discardUndoEntry(undoEntry)", preserve)
    assert preserve < discard


def test_prompt_identity_history_authorizes_cleanup_only_for_created_true_result():
    widget = _source("web/js/editor_widget.js")
    start = widget.index("    _finalizePromptIdentityCreationHistory(")
    end = widget.index("    _mergeQueueMutationIntents(", start)
    method = widget[start:end]
    result = _run_node(f"""
class Harness {{
{method}
}}
const intent={{unit:{{semantic_unit_id:"one",name:"One",kind:"subject",definition:""}},
  cleanup_expected:{{semantic_unit_id:"stale"}}}};
const entry={{promptIdentityCreateIntents:[intent]}};
const h=new Harness();
const replay=h._finalizePromptIdentityCreationHistory(entry,{{payload:{{results:[{{
  type:"create_prompt_semantic_unit",created:false,unit:{{semantic_unit_id:"one",
  handle:"One",name:"One",kind:"subject",definition:"",order:0,sources:[],
  voice:{{member_id:null}},attachment_defaults:{{summary:"edited"}},
  disabled_capabilities:[]}}}}]}}}});
const afterReplay={{cleanupExpected:Boolean(intent.cleanup_expected),
  cleanupUnproven:intent.cleanup_unproven,
  reconciledExpected:intent.reconciled_expected,created:[...replay]}};
const created=h._finalizePromptIdentityCreationHistory(entry,{{payload:{{results:[{{
  type:"create_prompt_semantic_unit",created:true,unit:{{semantic_unit_id:"one",
  handle:"One",name:"One",kind:"subject",definition:"",order:0,sources:[],
  voice:{{member_id:null}},attachment_defaults:{{}},disabled_capabilities:[]}}}}]}}}});
console.log(JSON.stringify({{afterReplay,afterCreated:{{
  cleanupExpected:intent.cleanup_expected,cleanupUnproven:Boolean(intent.cleanup_unproven),
  created:[...created]}}}}));
""")
    assert result["afterReplay"] == {
        "cleanupExpected": False,
        "cleanupUnproven": True,
        "reconciledExpected": {
            "semantic_unit_id": "one",
            "handle": "One",
            "name": "One",
            "kind": "subject",
            "definition": "",
            "order": 0,
            "sources": [],
            "voice": {"member_id": None},
            "attachment_defaults": {"summary": "edited"},
            "disabled_capabilities": [],
        },
        "created": [],
    }
    assert result["afterCreated"]["cleanupExpected"]["semantic_unit_id"] == "one"
    assert result["afterCreated"]["cleanupUnproven"] is False
    assert result["afterCreated"]["created"] == ["one"]


def test_undo_and_redo_share_a_non_reentrant_history_gate():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const notices=[]; globalThis.notifyInfo=(message)=>notices.push(message);
globalThis.notifyWarning=()=>{{}};
class Harness {{
{undo}
{redo}
  constructor() {{ this.activeSceneId="scene"; this.activeScene={{value:2}};
    this._undoStack=[
      {{sceneId:"scene",snapshot:{{value:0}},label:"first"}},
      {{sceneId:"scene",snapshot:{{value:1}},label:"second"}},
    ]; this._redoStack=[]; this._editorFocused=false; this.restores=0; }}
  _keyboardDebug() {{}}
  async _restoreScene() {{ this.restores += 1;
    await new Promise((resolve)=>{{this.release=resolve;}}); }}
  async _applyPromptIdentityChange() {{}}
  async _applyReferenceHistoryOperations() {{}}
}}
const h=new Harness();
const first=h._undo(); await Promise.resolve();
await h._undo();
await h._redo();
const whileRunning={{undo:h._undoStack.length,redo:h._redoStack.length,
  restores:h.restores,inFlight:h._historyOperationInFlight}};
h.release(); await first;
console.log(JSON.stringify({{whileRunning,after:{{undo:h._undoStack.length,
  redo:h._redoStack.length,restores:h.restores,inFlight:h._historyOperationInFlight}},
  notices}}));
""")
    assert result["whileRunning"] == {
        "undo": 1, "redo": 0, "restores": 1, "inFlight": True}
    assert result["after"] == {
        "undo": 1, "redo": 1, "restores": 1, "inFlight": False}
    assert result["notices"] == [
        "Undo or Redo is still finishing. Try again in a moment.",
        "Undo or Redo is still finishing. Try again in a moment.",
    ]


def test_scene_navigation_cannot_clear_history_while_undo_or_redo_is_in_flight():
    widget = _source("web/js/editor_widget.js")
    history_busy = _method(widget, "_hasPendingHistoryCommit", "applyWidgetState")
    create_scene = _method(widget, "_createScene", "_setActiveScene")
    set_scene = _method(widget, "_setActiveScene", "_refreshDurationInput")
    assert set_scene.index('_setWidgetValue("scene_id"') < set_scene.index(
        '_setWidgetValue("selection_start"')
    cycle = _method(widget, "_cycleScene", "_renameScene")
    delete_scene = _method(widget, "_deleteScene", "_duplicateScene")
    duplicate_scene = _method(widget, "_duplicateScene", "_allProjectAssetsForGallery")
    result = _run_node(f"""
const notices=[]; globalThis.notifyInfo=(message)=>notices.push(message);
class Harness {{
{history_busy}
{create_scene}
{set_scene}
{cycle}
{delete_scene}
{duplicate_scene}
  constructor(inFlight,pending) {{ this._historyOperationInFlight=inFlight; this.activeSceneId="A";
    this.activeScene={{scene_id:"A"}}; this.scenes=[this.activeScene,{{scene_id:"B"}}];
    this._undoStack=[{{label:"older"}},...(pending?[{{label:"attach",pending:true}}]:[])];
    this._redoStack=[{{label:"redo"}}]; this.projectDir="project"; }}
}}
    const activeOp=new Harness(true,false);
    const activeDirect=activeOp._setActiveScene(activeOp.scenes[1]); activeOp._cycleScene(1);
    activeOp._sceneHistoryLifecycleOwner={{allowSceneSwitch:true}};
    const borrowedPermission=activeOp._setActiveScene(activeOp.scenes[1]);
    const pendingCommit=new Harness(false,true);
const pendingDirect=pendingCommit._setActiveScene(pendingCommit.scenes[1]);
pendingCommit._cycleScene(1);
await pendingCommit._createScene();
await pendingCommit._deleteScene(pendingCommit.activeScene);
await pendingCommit._duplicateScene(pendingCommit.activeScene);
    console.log(JSON.stringify({{activeDirect,borrowedPermission,pendingDirect,
  active:activeOp.activeSceneId,pendingActive:pendingCommit.activeSceneId,
  activeUndo:activeOp._undoStack.map((entry)=>entry.label),
  pendingUndo:pendingCommit._undoStack.map((entry)=>entry.label),
  activeRedo:activeOp._redoStack.map((entry)=>entry.label),
  pendingRedo:pendingCommit._redoStack.map((entry)=>entry.label),notices}}));
""")
    assert result == {
        "activeDirect": False,
        "borrowedPermission": False,
        "pendingDirect": False,
        "active": "A",
        "pendingActive": "A",
        "activeUndo": ["older"],
        "pendingUndo": ["older", "attach"],
        "activeRedo": ["redo"],
        "pendingRedo": ["redo"],
        "notices": [
            "Finish saving, Undo, or Redo before switching scenes.",
            "Finish saving, Undo, or Redo before switching scenes.",
            "Finish saving, Undo, or Redo before switching scenes.",
            "Finish saving, Undo, or Redo before switching scenes.",
            "Finish saving, Undo, or Redo before switching scenes.",
            "Another scene change is still finishing. Try again in a moment.",
            "Another scene change is still finishing. Try again in a moment.",
            "Another scene change is still finishing. Try again in a moment.",
        ],
    }


def test_active_scene_delete_holds_lifecycle_through_delayed_refresh():
    widget = _source("web/js/editor_widget.js")
    history_lifecycle = _method(widget, "_hasPendingHistoryCommit", "applyWidgetState")
    delete_scene = _method(widget, "_deleteScene", "_duplicateScene")
    result = _run_node(f"""
const notices=[]; globalThis.notifyInfo=(message)=>notices.push(message);
globalThis.confirm=()=>true; globalThis.api={{apiURL:(value)=>value}};
let releaseDelete;
globalThis.fetch=()=>new Promise((resolve)=>{{releaseDelete=()=>resolve({{ok:true}});}});
class Harness {{
{history_lifecycle}
{delete_scene}
  constructor(){{this._historyOperationInFlight=false;this._undoStack=[];
    this._redoStack=[];this._sceneHistoryLifecycleOwner=null;
    this.activeSceneId="A";this.activeScene={{scene_id:"A",name:"A"}};
    this.scenes=[this.activeScene,{{scene_id:"B",name:"B"}}];this.projectDir="project";
    this.fetchOwner=null;this.fetchTokenMatched=false;this.switched=false;}}
  async _fetchScenes(options){{this.fetchOwner=this._sceneHistoryLifecycleOwner;
    this.fetchTokenMatched=options.sceneHistoryLifecycleToken===this.fetchOwner;
    this.switched=!!(this.fetchOwner?.allowSceneSwitch && this.fetchTokenMatched);
    if(this.switched)this.activeSceneId="B";}}
}}
const h=new Harness();
const deletion=h._deleteScene(h.activeScene); await Promise.resolve();
const ownerDuringDelete=h._sceneHistoryLifecycleOwner;
const blockedAttach=h._beginSceneHistoryLifecycle("attach prompt Reference");
releaseDelete(); await deletion;
const ownerAfterDelete=h._sceneHistoryLifecycleOwner;
const attachAfter=h._beginSceneHistoryLifecycle("attach prompt Reference");
const attachAccepted=!!attachAfter; h._endSceneHistoryLifecycle(attachAfter);
console.log(JSON.stringify({{ownerDuringDelete:!!ownerDuringDelete,blockedAttach,
  fetchOwned:h.fetchOwner===ownerDuringDelete,fetchTokenMatched:h.fetchTokenMatched,
  switched:h.switched,active:h.activeSceneId,ownerAfterDelete,attachAccepted,
  finalOwner:h._sceneHistoryLifecycleOwner,notices}}));
""")
    assert result == {
        "ownerDuringDelete": True,
        "blockedAttach": None,
        "fetchOwned": True,
        "fetchTokenMatched": True,
        "switched": True,
        "active": "B",
        "ownerAfterDelete": None,
        "attachAccepted": True,
        "finalOwner": None,
        "notices": ["Another scene change is still finishing. Try again in a moment."],
    }


def test_scene_refresh_cannot_supersede_a_foreign_lifecycle_owner():
    widget = _source("web/js/editor_widget.js")
    fetch_scenes = _method(widget, "_fetchScenes", "_createScene")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};
globalThis.getProjectVersion=()=>""; globalThis.sessionDiagRecord=()=>{{}};
let fetchCount=0; let releaseFetch;
globalThis.fetch=()=>{{fetchCount += 1;return new Promise((resolve)=>{{
  releaseFetch=()=>resolve({{ok:true,headers:{{get:()=>""}},
    json:async()=>({{scenes:[{{scene_id:"B"}}]}})}});
}});}};
class Harness {{
{fetch_scenes}
  constructor(){{this.projectDir="project";this.activeSceneId="A";
    this.scenes=[{{scene_id:"A"}}];this._sceneFetchSeq=0;
    this._sceneMutationInvalidationSeq=0;this._sceneHistoryLifecycleOwner=null;
    this._pendingScenesRefresh=false;this.deferred=[];}}
  _shouldDeferSceneRefresh(){{return false;}}
  _deferSceneRefresh(reason,details){{this._pendingScenesRefresh=true;
    this.deferred.push([reason,details.stage]);}}
  _clearProjectNotFound(){{}}
  _markStaleReplayApplied(){{}}
  _getWidgetValue(){{return "";}}
  _setActiveScene(scene){{this.activeSceneId=scene.scene_id;}}
  _governStaleVersionReplay(){{return true;}}
}}
const h=new Harness();
h._sceneHistoryLifecycleOwner={{label:"delete"}};
const startResult=await h._fetchScenes({{reason:"foreign-start"}});
const blockedAtStart={{fetchCount,pending:h._pendingScenesRefresh,
  scenes:h.scenes.map((scene)=>scene.scene_id),result:startResult}};
h._pendingScenesRefresh=false;h._sceneHistoryLifecycleOwner=null;
const inFlight=h._fetchScenes({{reason:"foreign-apply"}});await Promise.resolve();
h._sceneHistoryLifecycleOwner={{label:"delete"}};releaseFetch();const applyResult=await inFlight;
console.log(JSON.stringify({{blockedAtStart,afterApply:{{fetchCount,
  pending:h._pendingScenesRefresh,scenes:h.scenes.map((scene)=>scene.scene_id),
  active:h.activeSceneId,result:applyResult,deferred:h.deferred}}}}));
""")
    assert result == {
        "blockedAtStart": {
            "fetchCount": 0, "pending": True, "scenes": ["A"], "result": False},
        "afterApply": {
            "fetchCount": 1,
            "pending": True,
            "scenes": ["A"],
            "active": "A",
            "result": False,
            "deferred": [
                ["foreign-start", "scene_history_lifecycle_start"],
                ["foreign-apply", "scene_history_lifecycle_apply"],
            ],
        },
    }


def test_scene_fetch_reports_network_and_http_failure_without_applying():
    widget = _source("web/js/editor_widget.js")
    fetch_scenes = _method(widget, "_fetchScenes", "_createScene")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};globalThis.getProjectVersion=()=>"";
globalThis.sessionDiagRecord=()=>{{}};globalThis.console.warn=()=>{{}};
class Harness {{
{fetch_scenes}
  constructor(){{this.projectDir="project";this.activeSceneId="A";
    this.scenes=[{{scene_id:"A"}}];this._sceneFetchSeq=0;
    this._sceneMutationInvalidationSeq=0;this._sceneHistoryLifecycleOwner={{label:"prompt"}};
    this._pendingScenesRefresh=false;}}
  _shouldDeferSceneRefresh(){{return false;}}
  _deferSceneRefresh(){{this._pendingScenesRefresh=true;}}
  _clearProjectNotFound(){{}}
  _showProjectNotFound(){{}}
  _markStaleReplayApplied(){{}}
  _getWidgetValue(){{return "";}}
  _setActiveScene(scene){{this.activeSceneId=scene.scene_id;}}
  _governStaleVersionReplay(){{return true;}}
}}
const h=new Harness();const token=h._sceneHistoryLifecycleOwner;
globalThis.fetch=async()=>{{throw new Error("offline");}};
const network=await h._fetchScenes({{sceneHistoryLifecycleToken:token}});
globalThis.fetch=async()=>({{ok:false,status:500,headers:{{get:()=>""}}}});
const http=await h._fetchScenes({{sceneHistoryLifecycleToken:token}});
globalThis.fetch=async()=>({{ok:true,status:200,headers:{{get:()=>""}},
  json:async()=>({{scenes:[{{scene_id:"A",fresh:true}}]}})}});
const success=await h._fetchScenes({{sceneHistoryLifecycleToken:token}});
console.log(JSON.stringify({{network,http,success,fresh:h.scenes[0].fresh===true,
  ownerRetained:h._sceneHistoryLifecycleOwner===token}}));
""")
    assert result == {
        "network": False,
        "http": False,
        "success": True,
        "fresh": True,
        "ownerRetained": True,
    }


def test_remote_scene_state_defers_before_host_write_during_history():
    widget = _source("web/js/editor_widget.js")
    history_busy = _method(widget, "_hasPendingHistoryCommit", "applyWidgetState")
    apply_state = _method(widget, "applyWidgetState", "_flushDeferredDragState")
    flush_drag = _method(
        widget, "_flushDeferredDragState", "_shouldDeferSceneRefresh")
    commit_entry = _method(widget, "_commitUndoEntry", "_captureProjectDependencies")
    finish_history = _method(widget, "_finishHistoryOperation", "_runUndo")
    result = _run_node(f"""
globalThis.sessionDiagRecord=()=>{{}};
globalThis.coerceBoolean=(value)=>Boolean(value);
globalThis.notifyWarning=()=>{{}};
class Harness {{
{history_busy}
{apply_state}
{flush_drag}
{commit_entry}
{finish_history}
  constructor(scenes) {{ this._historyOperationInFlight=true; this.activeSceneId="A";
    this.activeScene={{scene_id:"A"}}; this.scenes=scenes; this.hostWrites=[];
    this.selectionStart=0; this.selectionEnd=10; this.playhead=0; this.totalFrames=10;
    this._undoStack=[]; this._redoStack=[]; this._maxUndoSteps=20; }}
  _setHostValueLocal(name,value) {{ this.hostWrites.push([name,value]); }}
  _setActiveScene(scene) {{ this.activeSceneId=scene.scene_id; this.activeScene=scene; }}
  _refreshContextInputs() {{}} _refreshSelectionInputs() {{}} _updateToolbar() {{}}
  _renderTimeline() {{}} _renderQueuePanel() {{}}
  _replayDeferredProjectBackedRefresh() {{}}
  _promptContextConsumersMounted() {{ return false; }}
}}
const known=new Harness([{{scene_id:"A"}},{{scene_id:"B"}}]);
known.applyWidgetState({{scene_id:"B",selection_start:4}});
const knownDeferred={{active:known.activeSceneId,writes:[...known.hostWrites],
  pending:structuredClone(known._pendingHistoryWidgetState)}};
known._finishHistoryOperation();
const unknown=new Harness([{{scene_id:"A"}}]);
unknown._undoStack=[{{label:"scene-a"}}]; unknown._redoStack=[{{label:"redo-a"}}];
unknown.applyWidgetState({{scene_id:"C",selection_start:6}});
const unknownDeferred={{active:unknown.activeSceneId,writes:[...unknown.hostWrites]}};
unknown._finishHistoryOperation();
const ordered=new Harness([{{scene_id:"A"}},{{scene_id:"B"}}]);
ordered.applyWidgetState({{scene_id:"B",selection_start:4}});
ordered.applyWidgetState({{selection_start:7}});
ordered.applyWidgetState({{scene_id:"A",selection_start:8}});
const orderedDeferred={{active:ordered.activeSceneId,writes:[...ordered.hostWrites],
  pending:structuredClone(ordered._pendingHistoryWidgetState)}};
ordered._finishHistoryOperation();
const dragOrdered=new Harness([{{scene_id:"A"}},{{scene_id:"B"}}]);
dragOrdered.isDragging=true;
dragOrdered.applyWidgetState({{selection_start:7}});
dragOrdered.applyWidgetState({{scene_id:"B",selection_start:4}});
dragOrdered.isDragging=false;
dragOrdered._flushDeferredDragState();
const dragDeferred={{active:dragOrdered.activeSceneId,writes:[...dragOrdered.hostWrites],
  drag:dragOrdered._pendingApplyWidgetState || null,
  pending:structuredClone(dragOrdered._pendingHistoryWidgetState)}};
dragOrdered._finishHistoryOperation();
const sceneSuperseded=new Harness([{{scene_id:"A"}},{{scene_id:"B"}}]);
sceneSuperseded.applyWidgetState({{scene_id:"B",selection_start:4}});
sceneSuperseded.applyWidgetState({{scene_id:"A"}});
const supersededPending=structuredClone(sceneSuperseded._pendingHistoryWidgetState);
sceneSuperseded._finishHistoryOperation();
const postDragGap=new Harness([{{scene_id:"A"}}]);
postDragGap._historyOperationInFlight=false;
postDragGap.isDragging=true;
postDragGap.applyWidgetState({{selection_start:7}});
postDragGap.isDragging=false;
postDragGap.applyWidgetState({{selection_start:4}});
postDragGap._flushDeferredDragState();
const pendingCommit=new Harness([{{scene_id:"A"}},{{scene_id:"B"}}]);
pendingCommit._historyOperationInFlight=false;
const pendingEntry={{pending:true}}; pendingCommit._undoStack.push(pendingEntry);
pendingCommit.applyWidgetState({{scene_id:"B",selection_start:4}});
const pendingCommitDeferred={{active:pendingCommit.activeSceneId,
  writes:[...pendingCommit.hostWrites],
  pending:structuredClone(pendingCommit._pendingHistoryWidgetState)}};
pendingCommit._commitUndoEntry(pendingEntry);
const selectionFirst=new Harness([{{scene_id:"A"}},{{scene_id:"B"}}]);
selectionFirst._historyOperationInFlight=false;
const selectionFirstEntry={{pending:true}};
selectionFirst._undoStack.push(selectionFirstEntry);
selectionFirst.applyWidgetState({{selection_start:4}});
const selectionOnlyDeferred={{active:selectionFirst.activeSceneId,
  writes:[...selectionFirst.hostWrites],
  pending:structuredClone(selectionFirst._pendingHistoryWidgetState)}};
selectionFirst.applyWidgetState({{scene_id:"B"}});
const identifiedPending=structuredClone(selectionFirst._pendingHistoryWidgetState);
selectionFirst._commitUndoEntry(selectionFirstEntry);
console.log(JSON.stringify({{knownDeferred,knownAfter:{{active:known.activeSceneId,
  writes:known.hostWrites,selection:known.selectionStart}},unknownDeferred,
  unknownAfter:{{active:unknown.activeSceneId,writes:unknown.hostWrites,
  selection:unknown.selectionStart,undo:unknown._undoStack.length,
  redo:unknown._redoStack.length}},orderedDeferred,
  orderedAfter:{{active:ordered.activeSceneId,writes:ordered.hostWrites,
  selection:ordered.selectionStart}},dragDeferred,
  dragAfter:{{active:dragOrdered.activeSceneId,writes:dragOrdered.hostWrites,
  selection:dragOrdered.selectionStart}},supersededPending,
  supersededAfter:{{active:sceneSuperseded.activeSceneId,
  writes:sceneSuperseded.hostWrites,selection:sceneSuperseded.selectionStart}},
  postDragGap:{{writes:postDragGap.hostWrites,selection:postDragGap.selectionStart,
  pending:postDragGap._pendingApplyWidgetState || null}},pendingCommitDeferred,
  pendingCommitAfter:{{active:pendingCommit.activeSceneId,
  writes:pendingCommit.hostWrites,selection:pendingCommit.selectionStart}},
  selectionOnlyDeferred,identifiedPending,
  selectionFirstAfter:{{active:selectionFirst.activeSceneId,
  writes:selectionFirst.hostWrites,selection:selectionFirst.selectionStart}}}}));
""")
    assert result["knownDeferred"] == {
        "active": "A", "writes": [],
        "pending": {"scene_id": "B", "selection_start": 4},
    }
    assert result["knownAfter"] == {
        "active": "B",
        "writes": [["scene_id", "B"], ["selection_start", 4]],
        "selection": 4,
    }
    assert result["unknownDeferred"] == {"active": "A", "writes": []}
    assert result["unknownAfter"] == {
        "active": "C",
        "writes": [["scene_id", "C"], ["selection_start", 6]],
        "selection": 6,
        "undo": 0,
        "redo": 0,
    }
    assert result["orderedDeferred"] == {
        "active": "A", "writes": [],
        "pending": {"scene_id": "A", "selection_start": 8},
    }
    assert result["orderedAfter"] == {
        "active": "A",
        "writes": [["scene_id", "A"], ["selection_start", 8]],
        "selection": 8,
    }
    assert result["dragDeferred"] == {
        "active": "A", "writes": [], "drag": None,
        "pending": {"selection_start": 4, "scene_id": "B"},
    }
    assert result["dragAfter"] == {
        "active": "B",
        "writes": [["scene_id", "B"], ["selection_start", 4]],
        "selection": 4,
    }
    assert result["supersededPending"] == {"scene_id": "A"}
    assert result["supersededAfter"] == {
        "active": "A", "writes": [["scene_id", "A"]], "selection": 0,
    }
    assert result["postDragGap"] == {
        "writes": [["selection_start", 4]], "selection": 4, "pending": None,
    }
    assert result["pendingCommitDeferred"] == {
        "active": "A", "writes": [],
        "pending": {"scene_id": "B", "selection_start": 4},
    }
    assert result["pendingCommitAfter"] == {
        "active": "B",
        "writes": [["scene_id", "B"], ["selection_start", 4]],
        "selection": 4,
    }
    assert result["selectionOnlyDeferred"] == {
        "active": "A", "writes": [], "pending": {"selection_start": 4},
    }
    assert result["identifiedPending"] == {
        "selection_start": 4, "scene_id": "B"}
    assert result["selectionFirstAfter"] == {
        "active": "B",
        "writes": [["scene_id", "B"], ["selection_start", 4]],
        "selection": 4,
    }


def test_controller_delegates_remote_widget_state_before_shared_widget_mutation():
    controller = _source("web/js/editor_node_controller.js")
    apply_remote = _method(
        controller, "_applyRemoteWidgetState", "_onFullscreenWidgetStateApplied")
    finalize = _method(
        controller, "_onFullscreenWidgetStateApplied", "_startWidgetStateFallbackPolling")
    result = _run_node(f"""
globalThis.EDITOR_WIDGET_FIELDS=["scene_id","selection_start","selection_end"];
class Harness {{
{apply_remote}
{finalize}
  constructor(editor) {{
    this.values={{scene_id:"A",selection_start:0,selection_end:10}};
    this.widgetWrites=[]; this.stateCalls=[]; this.previewCalls=[];
    this.fullscreenSession=editor ? {{editor}} : null;
    this.node={{setDirtyCanvas:()=>{{}}}};
  }}
  _getWidgetValue(name) {{ return this.values[name]; }}
  _setWidgetValue(name,value) {{ this.widgetWrites.push([name,value]); this.values[name]=value; }}
  _recordDiagEvent() {{}}
  onEditorWidgetValueChange(name,value) {{ this.stateCalls.push([name,value]); }}
  _previewInvalidationKeysForWidget(name) {{ return name === "scene_id" ? ["scene"] : ["preview"]; }}
  _schedulePreviewStateRefresh(keys) {{ this.previewCalls.push(keys); }}
  refreshSummary() {{ return Promise.resolve(); }}
  render() {{}}
}}
const editor={{pending:true,received:[],hasDeferredWidgetState(){{return this.pending;}},
  applyWidgetState(values){{this.received.push(structuredClone(values));}}}};
const mounted=new Harness(editor);
const accepted=mounted._applyRemoteWidgetState({{selection_start:0,scene_id:"A"}},"ws","peer");
const deferred={{accepted,writes:[...mounted.widgetWrites],received:[...editor.received]}};
editor.pending=false;
mounted._applyRemoteWidgetState({{selection_start:4,scene_id:"B"}},"ws","peer");
const immediate={{writes:[...mounted.widgetWrites],received:[...editor.received]}};
mounted.values.scene_id="B"; mounted.values.selection_start=4;
mounted._onFullscreenWidgetStateApplied({{scene_id:"B",selection_start:4}});
const finalized={{stateCalls:mounted.stateCalls,previewCalls:mounted.previewCalls}};
const dormant=new Harness(null);
dormant._applyRemoteWidgetState({{selection_start:4,scene_id:"B"}},"poll","peer");
console.log(JSON.stringify({{deferred,immediate,finalized,
  dormant:{{writes:dormant.widgetWrites,stateCalls:dormant.stateCalls}}}}));
""")
    assert result["deferred"] == {
        "accepted": True,
        "writes": [],
        "received": [{"scene_id": "A", "selection_start": 0}],
    }
    assert result["immediate"] == {
        "writes": [],
        "received": [
            {"scene_id": "A", "selection_start": 0},
            {"scene_id": "B", "selection_start": 4},
        ],
    }
    assert result["finalized"] == {
        "stateCalls": [["scene_id", "B"], ["selection_start", 4]],
        "previewCalls": [["scene", "preview"]],
    }
    assert result["dormant"] == {
        "writes": [["scene_id", "B"], ["selection_start", 4]],
        "stateCalls": [["scene_id", "B"], ["selection_start", 4]],
    }


def test_standalone_reference_recovery_history_undoes_and_redoes_target_change():
    widget = _source("web/js/editor_widget.js")
    push_reference = _method(
        widget, "_pushReferenceUndo", "_restoreProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}}; globalThis.notifyWarning=()=>{{}};
class Harness {{
{push_reference}
{undo}
{redo}
  constructor() {{ this.activeSceneId="scene"; this.activeScene={{}};
    this._undoStack=[]; this._redoStack=[]; this._maxUndoSteps=20;
    this._editorFocused=false; this.calls=[]; }}
  _keyboardDebug() {{}}
  _trimUndoStack() {{}}
  async _applyReferenceHistoryOperations(operations) {{
    this.calls.push(operations[0].fields.handle);
  }}
  async _restoreScene() {{}}
  async _applyPromptIdentityChange() {{}}
}}
const h=new Harness();
const rollback=[{{type:"update_member",fields:{{handle:""}},expected:{{handle:"Portrait"}}}}];
const forward=[{{type:"update_member",fields:{{handle:"Portrait"}},expected:{{handle:""}}}}];
h._pushReferenceUndo("materialize",rollback,forward);
await h._undo(); await h._redo();
console.log(JSON.stringify({{calls:h.calls,undo:h._undoStack.length,redo:h._redoStack.length}}));
""")
    assert result == {"calls": ["", "Portrait"], "undo": 1, "redo": 0}


def test_project_dependency_history_restore_failure_keeps_source_entry():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const warnings=[]; globalThis.notifyWarning=(message)=>warnings.push(message);
class Harness {{
{undo}
{redo}
  constructor() {{
    this.activeSceneId="scene"; this.activeScene={{}}; this._editorFocused=false;
    this._undoStack=[]; this._redoStack=[];
  }}
  _keyboardDebug() {{}}
  _captureProjectDependencies() {{ return {{value:"current"}}; }}
  async _restoreProjectDependencies() {{ throw new Error("dependency restore refused"); }}
  async _restoreScene() {{}}
  async _mutateReferences() {{}}
}}
const undoHarness=new Harness();
undoHarness._undoStack.push({{kind:"project_dependencies",sceneId:"scene",
  snapshot:{{value:"before"}},label:"identity edit"}});
await undoHarness._undo();
const undoState={{undo:undoHarness._undoStack.length,redo:undoHarness._redoStack.length}};
const redoHarness=new Harness();
redoHarness._redoStack.push({{kind:"project_dependencies",sceneId:"scene",
  snapshot:{{value:"after"}},label:"identity edit"}});
await redoHarness._redo();
console.log(JSON.stringify({{undoState,redoState:{{undo:redoHarness._undoStack.length,
  redo:redoHarness._redoStack.length}},warnings}}));
""")
    assert result["undoState"] == {"undo": 1, "redo": 0}
    assert result["redoState"] == {"undo": 0, "redo": 1}
    assert result["warnings"] == [
        "dependency restore refused", "dependency restore refused"]


def test_composite_history_compensation_failure_keeps_retryable_entry():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const warnings=[]; globalThis.notifyWarning=(message)=>warnings.push(message);
class Harness {{
{undo}
{redo}
  constructor(mode) {{
    this.mode=mode; this.activeSceneId="scene"; this.activeScene={{value:"current"}};
    this._editorFocused=false; this._undoStack=[]; this._redoStack=[]; this.identityCalls=0;
  }}
  _keyboardDebug() {{}}
  async _applyPromptIdentityChange() {{
    this.identityCalls += 1;
    if (this.identityCalls > 1) throw new Error(`${{this.mode}} compensation refused`);
  }}
  async _restoreScene() {{ throw new Error(`${{this.mode}} scene refused`); }}
  async _mutateReferences() {{}}
}}
const entry={{sceneId:"scene",snapshot:{{value:"other"}},label:"attach",
  promptIdentityChange:{{type:"upsert",value:{{handle:""}}}},
  inversePromptIdentityChange:{{type:"upsert",value:{{handle:"Stored"}}}}}};
const undoHarness=new Harness("undo"); undoHarness._undoStack.push(structuredClone(entry));
await undoHarness._undo();
const undoState={{undo:undoHarness._undoStack.length,redo:undoHarness._redoStack.length}};
const redoHarness=new Harness("redo"); redoHarness._redoStack.push(structuredClone(entry));
await redoHarness._redo();
console.log(JSON.stringify({{undoState,redoState:{{undo:redoHarness._undoStack.length,
  redo:redoHarness._redoStack.length}},warnings}}));
""")
    assert result["undoState"] == {"undo": 1, "redo": 0}
    assert result["redoState"] == {"undo": 0, "redo": 1}
    assert "undo scene refused Recovery also failed: undo compensation refused" in result["warnings"]
    assert "redo scene refused Recovery also failed: redo compensation refused" in result["warnings"]


def test_prompt_identity_history_rebases_without_erasing_unrelated_units():
    widget = _source("web/js/editor_widget.js")
    apply_change = _method(
        widget, "_applyPromptIdentityChange", "_applyReferencePayload")
    identity_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    result = _run_node(f"""
const identity=await import({json.dumps(identity_url)});
globalThis.applyPromptIdentityChange=identity.applyPromptIdentityChange;
globalThis.sameIdentitySnapshot=identity.sameIdentitySnapshot;
class Harness {{
{apply_change}
  constructor(units) {{ this.projectDir="project"; this._promptSemanticUnits=units;
    this.serverUnits=structuredClone(units); this.saved=[]; this.refreshes=0;
    this.conflictProject=null; this.loseNext=false; }}
  async _fetchReferences() {{ this.refreshes += 1;
    this._promptSemanticUnits=structuredClone(this.serverUnits); return {{ok:true}}; }}
  async _savePromptSemanticUnits(units,_label,options) {{
    if (this.conflictProject) {{
      const project=this.conflictProject; this.conflictProject=null;
      const error=new Error("conflict"); error.code="project_version_conflict";
      error.project=project; throw error;
    }}
    if (this.loseNext) {{ this.loseNext=false;
      this.serverUnits=structuredClone(units); throw new Error("lost response"); }}
    this.saved.push(structuredClone(units)); this.options=options;
    this.serverUnits=structuredClone(units);
    this._promptSemanticUnits=structuredClone(units); return units;
  }}
}}
const previous={{semantic_unit_id:"u",name:"Person"}};
const next={{...previous,handle:"Person"}};
const unrelated={{semantic_unit_id:"other",name:"Concurrent"}};
const safe=new Harness([next,unrelated]);
await safe._applyPromptIdentityChange({{type:"upsert",value:previous,expected:next}},
  "rollback",{{recordUndo:false}});
const changed=new Harness([{{...next,name:"Changed elsewhere"}},unrelated]);
let changedError="";
try {{
  await changed._applyPromptIdentityChange(
    {{type:"upsert",value:previous,expected:next}},"rollback",{{recordUndo:false}});
}} catch(error) {{ changedError=error.message; }}
const rebased=new Harness([next]);
rebased.conflictProject={{prompt_semantic_units:[next,unrelated],
  prompt_context_profiles:[{{profile_id:"concurrent"}}]}};
rebased.serverUnits=[next,unrelated];
await rebased._applyPromptIdentityChange(
  {{type:"upsert",value:previous,expected:next}},"rollback",{{recordUndo:false}});
const lost=new Harness([next,unrelated]); lost.loseNext=true;
const lostResult=await lost._applyPromptIdentityChange(
  {{type:"upsert",value:previous,expected:next}},"undo",{{recordUndo:false}});
const rebasedLost=new Harness([next]);
rebasedLost.serverUnits=[next,unrelated];
rebasedLost.conflictProject={{prompt_semantic_units:[next,unrelated],
  prompt_context_profiles:[{{profile_id:"concurrent"}}]}};
rebasedLost.loseNext=true;
const rebasedLostResult=await rebasedLost._applyPromptIdentityChange(
  {{type:"upsert",value:previous,expected:next}},"undo",{{recordUndo:false}});
console.log(JSON.stringify({{safe:safe.saved[0],options:safe.options,
  refreshes:safe.refreshes,changedSaves:changed.saved.length,changedError,
  rebased:rebased.saved[0],profiles:rebased._promptContextProfiles,
  lost:lostResult,lostRefreshes:lost.refreshes,
  rebasedLost:rebasedLostResult,rebasedLostRefreshes:rebasedLost.refreshes}}));
""")
    assert result["safe"] == [
        {"semantic_unit_id": "u", "name": "Person"},
        {"semantic_unit_id": "other", "name": "Concurrent"},
    ]
    assert result["options"] == {"recordUndo": False}
    assert result["refreshes"] == 1
    assert result["changedSaves"] == 0
    assert "changed elsewhere" in result["changedError"]
    assert result["rebased"] == [
        {"semantic_unit_id": "u", "name": "Person"},
        {"semantic_unit_id": "other", "name": "Concurrent"},
    ]
    assert result["profiles"] == [{"profile_id": "concurrent"}]
    assert result["lost"] == [
        {"semantic_unit_id": "u", "name": "Person"},
        {"semantic_unit_id": "other", "name": "Concurrent"},
    ]
    assert result["lostRefreshes"] == 2
    assert result["rebasedLost"] == [
        {"semantic_unit_id": "u", "name": "Person"},
        {"semantic_unit_id": "other", "name": "Concurrent"},
    ]
    assert result["rebasedLostRefreshes"] == 2


def test_reference_history_reconciles_an_apply_then_lost_response():
    widget = _source("web/js/editor_widget.js")
    apply_reference = _method(
        widget, "_applyReferenceHistoryOperations", "_applyReferencePayload")
    result = _run_node(f"""
class Harness {{
{apply_reference}
  constructor() {{ this.serverHandle="Portrait"; this._references=[];
    this.refreshes=0; this.mutations=0; }}
  async _fetchReferences() {{ this.refreshes += 1;
    this._references=[{{members:[{{member_id:"m",handle:this.serverHandle}}]}}];
    return {{ok:true}}; }}
  async _mutateReferences(operations) {{ this.mutations += 1;
    this.serverHandle=operations[0].fields.handle; throw new Error("lost response"); }}
}}
const h=new Harness();
const result=await h._applyReferenceHistoryOperations([{{
  type:"update_member",member_id:"m",fields:{{handle:""}},
  expected:{{handle:"Portrait"}},
}}],"undo materialize");
console.log(JSON.stringify({{result,handle:h._references[0].members[0].handle,
  refreshes:h.refreshes,mutations:h.mutations}}));
""")
    assert result == {
        "result": True, "handle": "", "refreshes": 2, "mutations": 1}


def test_physical_handle_materialization_reconciles_unknown_outcome_without_claiming_undo():
    widget = _source("web/js/editor_widget.js")
    materialize = _method(
        widget, "_materializeReferenceMemberHandle",
        "_reconcileReferencesAfterAssetDeletion")
    result = _run_node(f"""
class Harness {{
{materialize}
  constructor(mode) {{ this.mode=mode; this.serverHandle="";
    this._references=[{{reference_id:"other",members:[{{member_id:"m",handle:"Wrong"}}]}},
      {{reference_id:"r",members:[{{member_id:"m",handle:""}}]}}]; }}
  async _mutateReferences() {{
    if (this.mode === "success") {{
      this.serverHandle="Portrait";
      this._references[1].members[0].handle=this.serverHandle;
      return {{payload:{{results:[{{type:"materialize_member_handle",
        member_id:"m",handle:this.serverHandle}}]}}}};
    }}
    if (this.mode === "lost_applied") this.serverHandle="Portrait";
    if (this.mode === "concurrent") this.serverHandle="OtherAuthor";
    throw new Error("offline");
  }}
  async _fetchReferences() {{
    this._references[1].members[0].handle=this.serverHandle;
    return {{ok:true}};
  }}
}}
const invoke=(h)=>h._materializeReferenceMemberHandle({{
  referenceId:"r",memberId:"m",suggestion:"Portrait",expectedHandle:""}});
const success=await invoke(new Harness("success"));
const lost=await invoke(new Harness("lost_applied"));
const concurrent=await invoke(new Harness("concurrent"));
let missing=""; try {{ await invoke(new Harness("lost_unapplied")); }}
catch(error) {{ missing=error.message; }}
console.log(JSON.stringify({{success,lost,concurrent,missing}}));
""")
    assert result == {
        "success": {"handle": "Portrait", "ownsHandle": True},
        "lost": {"handle": "Portrait", "ownsHandle": False},
        "concurrent": {"handle": "OtherAuthor", "ownsHandle": False},
        "missing": "offline",
    }


def test_production_scene_restore_rejects_http_and_network_failures():
    widget = _source("web/js/editor_widget.js")
    restore = _method(widget, "_restoreScene", "_setWidgetValue")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.api={{apiURL:(value)=>value}};
class Harness {{
{restore}
  constructor() {{ this.projectDir="project"; this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",attachments:[{{id:"chip"}}]}}; this.scenes=[]; }}
  _keyboardDebug() {{}}
  _setActiveScene(scene) {{ this.activeSceneId=scene.scene_id; this.activeScene=scene; }}
  _renderTimeline() {{}}
  _renderViewportFrame() {{}}
}}
const h=new Harness();
globalThis.fetch=async()=>({{ok:false,status:500}});
let http=""; try {{ await h._restoreScene("scene",{{}}); }}
catch(error) {{ http=error.message; }}
globalThis.fetch=async()=>{{throw new Error("offline")}};
let network=""; try {{ await h._restoreScene("scene",{{}}); }}
catch(error) {{ network=error.message; }}
const target={{scene_id:"scene",attachments:[]}}; let lostCalls=0;
globalThis.fetch=async()=>{{ lostCalls += 1;
  if (lostCalls===1) throw new Error("lost response");
  return {{ok:true,status:200,json:async()=>({{scenes:[target]}})}};
}};
let lost=""; try {{ await h._restoreScene("scene",target); }}
catch(error) {{ lost=error.message; }}
console.log(JSON.stringify({{http,network,lost,lostCalls,active:h.activeScene}}));
""")
    assert result == {
        "http": "Scene restore failed (500).",
        "network": "offline",
        "lost": "",
        "lostCalls": 2,
        "active": {"scene_id": "scene", "attachments": []},
    }


def test_chrome_placeholder_style_is_theme_owned_and_idempotent():
    theme_url = (ROOT / "web/js/editor_theme.js").as_uri()
    result = _run_node(f"""
const mod=await import({json.dumps(theme_url)});
const children=[];
const doc={{
  head:{{appendChild:(node)=>children.push(node)}},
  createElement:()=>({{id:"",textContent:""}}),
  getElementById:(id)=>children.find((node)=>node.id===id) || null,
}};
mod.installChromePlaceholderStyles(doc);
mod.installChromePlaceholderStyles(doc);
console.log(JSON.stringify({{count:children.length,css:children[0]?.textContent || "",
  color:mod.THEME.fgPlaceholder,className:mod.CHROME_DIM_PLACEHOLDER_CLASS}}));
""")
    assert result["count"] == 1
    assert f'.{result["className"]}::placeholder' in result["css"]
    assert result["color"] in result["css"]


def test_physical_row_status_uses_declared_role_labels():
    """Roles are declared as {value, label}; the row printed the raw value.

    That gave "first_frame" instead of "First frame", and for a format whose
    role vocabulary contains `identity` it produced the self-contradicting
    "identity · <Picture 2> · no identity".
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt row DOM coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.value=""; this.textContent=""; this.disabled=false; this._handlers={{}}; }}
  appendChild(c) {{ this.children.push(c); c.parentElement=this; return c; }}
  append(...cs) {{ cs.forEach((c) => c?.tagName && this.appendChild(c)); }}
  addEventListener(t,h) {{ (this._handlers[t] ||= []).push(h); }}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
  querySelector() {{ return null; }}
}}
globalThis.document={{createElement:(t)=>new N(t),body:new N("body"),activeElement:null}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
globalThis.CSS={{escape:(v)=>String(v)}};
const mod=await import({json.dumps(module_url)});
const root=new N("div");
mod.mountPromptIdentityPanel(root, {{
  profile: {{
    physical_populations:[{{key:"pictures",label:"Picture",
      source_key:"picture_ids",label_template:"<Picture {{n}}>"}}],
    identity_kinds:[{{key:"subject",label:"Subject"}}],
    role_catalogs:{{pictures:[
      {{value:"first_frame",label:"First frame"}},
      {{value:"identity",label:"Identity"}},
    ]}},
  }},
  candidate: {{setup_manifest:{{pictures:[
    {{member_id:"m1",asset_id:"a1",role:"first_frame",slot_number:1}},
    {{member_id:"m2",asset_id:"a2",role:"identity",slot_number:2}},
  ]}}}},
  references:[{{reference_id:"r",name:"Woman",members:[
    {{member_id:"m1",name:"One",asset_id:"a1"}},
    {{member_id:"m2",name:"Two",asset_id:"a2"}}]}}],
  semanticUnits:[],
}});
const rows=[];
const walk=(n)=>{{ if(n.dataset?.promptingRow==="physical") rows.push(n); n.children.forEach(walk); }};
walk(root);
console.log(JSON.stringify(rows.map((row)=>
  row.children.find((c)=>c.dataset.promptingCell==="status")?.textContent)));
"""
    statuses = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    # `Role:` is spelled out so the middle segment — the provider's resolved
    # slot label — can never be read as the role, and so a declared role
    # literally NAMED `identity` cannot collide with the identity count.
    assert statuses[0].startswith("Role: First frame · ")
    assert statuses[1].startswith("Role: Identity · ")
    # The count always says "prompt identity", which is the other sense of the
    # word. No action hint: the button beside it is labelled "+ Identity".
    assert all(status.endswith(" · No prompt identity") for status in statuses)
    assert not any("Create identity" in status for status in statuses)


def _run_panel_script(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt panel DOM coverage")
    module_url = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.textContent=""; this.title=""; }}
  appendChild(c) {{ this.children.push(c); c.parentElement=this; return c; }}
  append(...cs) {{ cs.forEach((c)=>c?.tagName && this.appendChild(c)); }}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
}}
globalThis.document={{createElement:(t)=>new N(t),body:new N("body")}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
const mod=await import({json.dumps(module_url)});
{body}
"""
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def test_writing_aid_choice_round_trip_preserves_declared_value_labels():
    """The flat form has one column, so it must carry the other one forward."""
    result = _run_panel_script("""
const declared = {
  motion: { type: "enum", label: "Motion type", optional: false,
    values: [{ value: "zooms in", label: "Zoom In" },
             { value: "pushes in", label: "Push In" }] },
};
const flat = mod.formatWritingAidChoices(declared);
// Re-parsing an unchanged edit must not flatten the labels away.
const unchanged = mod.parseWritingAidChoices(flat, "The camera {motion}.", declared);
// Dropping a value keeps the metadata of the values that survive.
const narrowed = mod.parseWritingAidChoices(
  "motion=zooms in", "The camera {motion}.", declared);
// A brand-new value with no prior metadata stays a bare string.
const widened = mod.parseWritingAidChoices(
  "motion=zooms in|arcs around", "The camera {motion}.", declared);
console.log(JSON.stringify({ flat, unchanged, narrowed, widened }));
""")
    assert result["flat"] == "motion=zooms in|pushes in"
    # Labels and the field's own metadata survive an untouched round trip.
    assert result["unchanged"]["motion"]["values"] == [
        {"value": "zooms in", "label": "Zoom In"},
        {"value": "pushes in", "label": "Push In"},
    ]
    assert result["unchanged"]["motion"]["label"] == "Motion type"
    assert result["narrowed"]["motion"]["values"] == [
        {"value": "zooms in", "label": "Zoom In"}]
    assert result["widened"]["motion"]["values"] == [
        {"value": "zooms in", "label": "Zoom In"}, "arcs around"]


def test_writing_aid_parse_only_declares_placeholders_the_text_uses():
    result = _run_panel_script("""
console.log(JSON.stringify({
  unused: mod.parseWritingAidChoices("ghost=a|b", "no placeholders here"),
  textIsNotAField: Object.keys(
    mod.parseWritingAidChoices("", "<d>[{language}] {text}</d>",
      { language: { type: "enum", values: ["English"] } })),
  emptyStaysEmpty: mod.parseWritingAidChoices("", "The camera {motion}."),
}));
""")
    # A declaration the text never substitutes is not a question worth asking.
    assert result["unused"] == {}
    assert result["textIsNotAField"] == ["language"]
    # An undeclared placeholder yields an empty vocabulary, which the New-aid
    # row now refuses by name instead of saving an unsavable format.
    assert result["emptyStaysEmpty"]["motion"]["values"] == []


# --- handle capability qualifier and the draft channel (writing-parity Phase 3) --

_H3_REF_PROFILE = {"capabilities": {"reference": {"derived": {
    "definitions": {"order": 1, "channel_key": "subject_definitions",
                    "placement": "section_prefix", "label": "Definition"},
    "summary": {"order": 2, "channel_key": "summary",
                "placement": "section_prefix", "label": "Summary"},
    "retention": {"order": 3, "channel_key": "retention_analysis",
                  "placement": "section_prefix", "label": "Retention"},
    "mentions": {"order": 4, "channel_key": "detailed_description",
                 "placement": "inline", "label": "Scene mention"},
    "audio_relationship": {"order": 5, "channel_key": "summary",
                           "placement": "section_prefix", "label": "Audio"},
}}}}
_GENERIC_PROFILE = {"capabilities": {"reference": {"derived": {
    "derived_prompt": {"order": 1, "channel_key": "visual",
                       "placement": "inline", "label": "Reference prompt"},
}}}}


def _run_chips_script(body):
    module_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    return _run_node(
        f"const mod = await import({json.dumps(module_url)});\n"
        f"const h3 = {json.dumps(_H3_REF_PROFILE)};\n"
        f"const generic = {json.dumps(_GENERIC_PROFILE)};\n"
        f"{body}\n")


def test_handle_mention_splits_its_capability_qualifier_at_the_first_dot():
    result = _run_chips_script("""
console.log(JSON.stringify({
  bare: mod.parseHandleMention("@KWoman"),
  dotted: mod.parseHandleMention("@KWoman.speaker"),
  noSigil: mod.parseHandleMention("KWoman.speaker"),
  // A handle cannot contain a dot (PROMPT_HANDLE_RE), so everything after the
  // first one is qualifier — never a second handle segment.
  extraDots: mod.parseHandleMention("@KWoman.a.b"),
  empty: mod.parseHandleMention(""),
}));
""")
    assert result["bare"] == {"handle": "KWoman", "qualifier": ""}
    assert result["dotted"] == {"handle": "KWoman", "qualifier": "speaker"}
    assert result["noSigil"] == {"handle": "KWoman", "qualifier": "speaker"}
    assert result["extraDots"] == {"handle": "KWoman", "qualifier": "a.b"}
    assert result["empty"] == {"handle": "", "qualifier": ""}


def test_handle_capability_is_inferred_from_its_channel_with_a_dotted_override():
    result = _run_chips_script("""
const kind = (profile, channelKey, qualifier) =>
  mod.handleAttachCapabilityKind(profile, { channelKey, qualifier });
console.log(JSON.stringify({
  definitions: kind(h3, "subject_definitions", ""),
  retention: kind(h3, "retention_analysis", ""),
  body: kind(h3, "detailed_description", ""),
  tie: kind(h3, "summary", ""),
  unclaimed: kind(h3, "overall_soundscape", ""),
  noChannel: kind(h3, "", ""),
  override: kind(h3, "subject_definitions", "mentions"),
  undeclaredOverride: kind(h3, "subject_definitions", "not_a_capability"),
  genericVisual: kind(generic, "visual", ""),
  genericOther: kind(generic, "speech", ""),
}));
""")
    # Placing a chip while writing in a channel seeds the capability that
    # channel routes to, so the chip emits where it was placed. Attaching in
    # `subject_definitions` used to seed `mentions`, whose declared route is
    # `detailed_description` — the chip emitted into a different channel.
    assert result["definitions"] == "definitions"
    assert result["retention"] == "retention"
    assert result["body"] == "mentions"
    # Two capabilities declare `summary`; the lower `order` wins the tie.
    assert result["tie"] == "summary"
    # A channel no capability claims falls back to the prose default, as does
    # one box projecting every channel — the Writing draft has no single channel.
    assert result["unclaimed"] == "mentions"
    assert result["noChannel"] == "mentions"
    # The dotted qualifier is the explicit override; an undeclared one falls
    # through rather than seeding a kind nothing can compile.
    assert result["override"] == "mentions"
    assert result["undeclaredOverride"] == "definitions"
    assert result["genericVisual"] == "derived_prompt"
    # `generic@1` declares no `mentions`, and "" is the correct answer: it means
    # seed nothing and take the format default.
    assert result["genericOther"] == ""


def test_handle_attach_stores_a_capability_only_when_it_deviates_from_the_default():
    result = _run_chips_script("""
const record = (profile, channelKey, qualifier) =>
  mod.handleAttachCapabilityRecord(profile, { channelKey, qualifier });
console.log(JSON.stringify({
  deviating: record(h3, "detailed_description", ""),
  matchesDefault: record(h3, "subject_definitions", ""),
  genericVisual: record(generic, "visual", ""),
  genericOther: record(generic, "speech", ""),
}));
""")
    # Sparse like the routing beside it: the compiler already falls back to the
    # lowest-`order` capability, so storing that same kind writes an authored
    # deviation where the author deviated from nothing. `capability_id`/`kind`
    # only — a stored `channel_key`/`placement` would freeze this chip's routing
    # at attach time, and a stored `enabled` would resolve the tri-state out of
    # inheriting its Reference or identity default.
    assert result["deviating"] == [
        {"capability_id": "mentions", "kind": "mentions"}]
    assert result["matchesDefault"] == []
    assert result["genericVisual"] == []
    assert result["genericOther"] == []


def test_handle_attach_default_matches_the_servers_undeclared_order_rule():
    from server import prompt_context

    # A capability with no `order` is LAST to the server
    # (`_default_capability` reads it as MAX_CAPABILITIES) and FIRST to the
    # browser's display ordering (`orderedReferenceDerived` reads it as 0).
    # Sparsity has to follow the server, or a chip omits a record the compiler
    # then resolves to a capability the author never chose.
    profile = {"capabilities": {"reference": {"derived": {
        "ordered": {"order": 2, "channel_key": "body", "placement": "inline",
                    "label": "Ordered"},
        "unordered": {"channel_key": "aside", "placement": "inline",
                      "label": "Unordered"},
    }}}}
    server_default = prompt_context._default_capability(
        {"kind": "reference"}, profile)["kind"]
    assert server_default == "ordered"
    result = _run_node(
        f"const mod = await import("
        f"{json.dumps((ROOT / 'web/js/prompt_context_chips.js').as_uri())});\n"
        f"const p = {json.dumps(profile)};\n"
        "const record = (channelKey) =>"
        " mod.handleAttachCapabilityRecord(p, { channelKey });\n"
        "console.log(JSON.stringify({"
        " displayFirst: mod.handleAttachCapabilityKind(p, {}),"
        " serverDefault: record('body'),"
        " deviating: record('aside') }));\n")
    # The server's default kind gets no record; the other one does — the reverse
    # of what the display ordering alone would have produced.
    assert result["serverDefault"] == []
    assert result["deviating"] == [
        {"capability_id": "unordered", "kind": "unordered"}]


def test_unheadered_draft_text_lands_in_the_declared_default_draft_channel():
    result = _run_chips_script("""
const keys = ["subject_definitions", "summary", "detailed_description"];
const doc = { nodes: [{ type: "text", node_id: "t1",
  text: "A woman crosses the market.\\nsummary: [reference generation]" }] };
const text = (split, key) => mod.promptDocumentText(split[key]);
const declared = mod.splitPromptDocumentChannels(doc, keys,
  { defaultKey: "detailed_description" });
const undeclared = mod.splitPromptDocumentChannels(doc, keys);
const unknown = mod.splitPromptDocumentChannels(doc, keys,
  { defaultKey: "not_a_channel" });
console.log(JSON.stringify({
  declared: keys.map((k) => [k, text(declared, k)]),
  undeclared: keys.map((k) => [k, text(undeclared, k)]),
  unknown: keys.map((k) => [k, text(unknown, k)]),
}));
""")
    # The leading unheadered line follows the declared channel; a later `key:`
    # header still wins for its own text.
    assert result["declared"] == [
        ["subject_definitions", ""],
        ["summary", "[reference generation]"],
        ["detailed_description", "A woman crosses the market."],
    ]
    # Omitted and unresolvable both keep channel 1 — the behavior every caller
    # had before the parameter existed. The template-retargeting collapse relies
    # on that default staying byte-identical.
    assert result["undeclared"] == [
        ["subject_definitions", "A woman crosses the market."],
        ["summary", "[reference generation]"],
        ["detailed_description", ""],
    ]
    assert result["unknown"] == result["undeclared"]


def test_writing_draft_stamps_its_draft_channel_rather_than_resolving_at_apply():
    panel = _source("web/js/editor_prompt_panel.js")
    settings = _source("web/js/editor_settings.js")
    # The stamp is written when the draft is BUILT and read back verbatim. A
    # draft authored before its template declared a draft channel carries no
    # stamp, and that absence is what keeps its unheadered text in channel 1
    # instead of silently relocating on the first Apply after an update.
    assert "writingState.defaultDraftChannel = defaultDraftChannel(template)" in panel
    assert ('writingState.defaultDraftChannel = String(saved.defaultDraftChannel || "")'
            in panel)
    assert "defaultDraftChannel: writingState.defaultDraftChannel" in panel
    # One accessor feeds the hint and EVERY split call site, so what the panel
    # promises, what the contribution decorations are placed against, and what
    # Apply writes cannot disagree. Four call sites: the Apply projection, the
    # compile-candidate patch, the decoration region walk, and the decoration
    # layout signature that decides whether that walk must repaint. A new
    # splitter that resolves the channel itself instead of taking the stamp is
    # the regression this counts.
    assert panel.count("defaultKey: writingDefaultChannelKey()") == 4
    assert "Unlabelled text goes to ${writingDefaultChannelKey()" in panel
    # Persisted only when set, exactly like `stash`, so an older record keeps
    # the shape it was written with.
    assert "if (value.defaultDraftChannel) {" in settings


def test_document_anchor_fields_survive_the_browser_normalizer():
    """Parity with `normalize_prompt_document`: same fields kept, same drops.

    A field the server keeps and the browser drops makes an echoed document
    hash differently from the server copy, which spuriously 409s identity
    validation. `record_key` used to travel this same carrier and was retired
    with the materializer, so the parity that remains is `capability_id` — plus
    the drop itself, asserted so a revival has to be deliberate on both sides
    rather than leaking back in on one.
    """
    result = _run_chips_script("""
const doc = { schema: "prompt_document@1", nodes: [
  { type: "attachment", node_id: "n1", attachment_id: "a1",
    capability_id: "definitions", record_key: "subject_definition:u1" },
  { type: "text", node_id: "n2", text: " is a woman" },
  { type: "attachment", node_id: "n3", attachment_id: "a2" },
]};
console.log(JSON.stringify({ nodes: mod.normalizePromptDocument(doc).nodes }));
""")
    nodes = result["nodes"]
    assert nodes[0]["capability_id"] == "definitions"
    assert "record_key" not in nodes[0]
    # An anchor that carries no capability must not invent one.
    assert "capability_id" not in nodes[2]

    from server import prompt_context
    server_nodes = prompt_context.normalize_prompt_document({"nodes": [
        {"type": "attachment", "node_id": "n1", "attachment_id": "a1",
         "capability_id": "definitions", "record_key": "subject_definition:u1"},
        {"type": "text", "node_id": "n2", "text": " is a woman"},
        {"type": "attachment", "node_id": "n3", "attachment_id": "a2"},
    ]})["nodes"]
    # The whole-node comparison is what makes this a parity test rather than two
    # independent ones: it fails if either side keeps a field the other drops.
    assert server_nodes == nodes


def test_anchor_fields_survive_a_project_save_and_load():
    """The persistence half of the anchor contract.

    `capability_id` says which part of a Reference a chip emits. It is useless
    if a project save drops it, and the loss would be silent — the chip would
    simply start compiling as the format default. Round-trips through real JSON
    so a serializer that stringifies unknown keys cannot pass by accident.
    """
    from server.timeline_state import Scene, PromptSection
    document = {"schema": "prompt_document_v1", "nodes": [
        {"type": "attachment", "node_id": "n1", "attachment_id": "a1",
         "capability_id": "definitions"},
        {"type": "text", "node_id": "n2", "text": " is a woman"},
    ]}
    section = PromptSection(
        start_frame=0, end_frame=10,
        channel_docs={"detailed_description": document},
        attachments=[{"attachment_id": "a1", "kind": "reference"}])
    scene = Scene(scene_id="s1", prompt_sections=[section])

    reloaded = Scene.from_dict(json.loads(json.dumps(scene.to_dict())))
    anchor = reloaded.prompt_sections[0].channel_docs[
        "detailed_description"]["nodes"][0]
    assert anchor["capability_id"] == "definitions"


def _run_panel_script(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt panel coverage")
    module_url = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    return _run_node(
        f"const mod = await import({json.dumps(module_url)});" + chr(10)
        + body + chr(10))


H3_CHANNELS = ["subject_definitions", "summary", "retention_analysis",
               "detailed_description", "overall_soundscape", "non_diegetic_music"]


def test_split_carries_the_field_the_caret_is_writing_in():
    """A split under a heading must not move the tail to another field.

    `---` starts a new block and a new block opens at its DEFAULT channel, so
    a bare break inserted while writing under `detailed_description` silently
    relocated everything after it into the default field.
    """
    result = _run_panel_script("const keys = "
        + json.dumps(H3_CHANNELS) + ";" + chr(10) + """
const draft = "subject_definitions:" + String.fromCharCode(10)
  + "a woman" + String.fromCharCode(10)
  + "detailed_description:" + String.fromCharCode(10)
  + "she walks in";
const at = (offset) => mod.writingChannelAtCaret(draft, offset, keys);
const brk = String.fromCharCode(10) + "---" + String.fromCharCode(10);
console.log(JSON.stringify({
  inFirst: at(draft.indexOf("a woman") + 3),
  inBody: at(draft.length),
  beforeAnyHeader: at(0),
  // After a break the channel resets, matching how the draft is re-read.
  afterBreak: mod.writingChannelAtCaret(
    "detailed_description:" + String.fromCharCode(10) + "x" + brk + "y",
    999, keys),
  carriesWhenDifferent: mod.writingSplitText("detailed_description", "subject_definitions"),
  silentWhenSame: mod.writingSplitText("detailed_description", "detailed_description"),
  silentWhenUnheaded: mod.writingSplitText("", "detailed_description"),
}));
""")
    assert result["inFirst"] == "subject_definitions"
    assert result["inBody"] == "detailed_description"
    assert result["beforeAnyHeader"] == ""
    assert result["afterBreak"] == ""
    # The heading is re-emitted only when the tail would otherwise move.
    nl = chr(10)
    assert result["carriesWhenDifferent"] == f"{nl}---{nl}detailed_description:{nl}"
    assert result["silentWhenSame"] == f"{nl}---{nl}"
    assert result["silentWhenUnheaded"] == f"{nl}---{nl}"


def test_a_handle_renders_without_pill_chrome():
    """Writing mode shows a sentence, so a handle must not be a capsule.

    Only the chrome goes: the element stays atomic and keyboard-reachable.
    """
    result = _run_chips_script("""
console.log(JSON.stringify({
  handle: mod.isHandleLabel("@KWoman"),
  qualified: mod.isHandleLabel("@KWoman.speaker"),
  described: mod.isHandleLabel("Reference — Korean Woman"),
  bareSigil: mod.isHandleLabel("@"),
  empty: mod.isHandleLabel(""),
}));
""")
    assert result["handle"] is True
    assert result["qualified"] is True
    assert result["described"] is False
    # A lone sigil is not a handle; it is someone mid-keystroke.
    assert result["bareSigil"] is False
    assert result["empty"] is False


def test_text_insertion_never_routes_through_exec_command():
    """Regression guard: `execCommand("insertText")` drops newlines here.

    That is not cosmetic — it is why "Split here" inserted an inline `---`
    that `splitWritingDraft` never recognized, so the button silently did
    nothing. The defect was invisible to every existing test because the
    string passed in was correct; only the DOM path mangled it. This pins the
    path rather than the string, since a future edit reaching for
    `execCommand` again would reintroduce exactly this failure.

    Source-level by necessity: the failure needs a real contenteditable and a
    live Selection, which the Node harness cannot provide. Behavior was
    verified in the running editor on 2026-08-18.
    """
    chips = _source("web/js/prompt_context_chips.js")
    panel = _source("web/js/editor_prompt_panel.js")

    insert = chips[chips.index("editor.insertText = "):]
    insert = insert[:insert.index("editor.removeAttachment")]
    # The CALL form, not the word: both files legitimately name execCommand
    # in comments explaining why they no longer use it.
    assert "execCommand(" not in insert and "execCommand?.(" not in insert, insert
    assert "model.nodes" in insert

    split = panel[panel.index(chr(39) + chr(39) + chr(39)) if False else
                  panel.index("makeBtn(" + chr(34) + "Split here" + chr(34)):]
    split = split[:split.index("Equalize")]
    assert "insertText(" in split, split
    assert "execCommand(" not in split and "execCommand?.(" not in split, split


def test_the_section_break_is_multi_line_so_the_splitter_can_see_it():
    """`splitWritingDraft` only breaks on a line whose trim equals `---`.

    So whatever the split inserts must put the marker alone on its own line,
    in every branch — including the one that carries a heading after it.
    """
    result = _run_panel_script('const t = (a, d) => mod.writingSplitText(a, d);\nconsole.log(JSON.stringify({\n  carried: mod.splitWritingDraft("a" + t("retention_analysis", "detailed_description") + "b"),\n  bare: mod.splitWritingDraft("a" + t("summary", "summary") + "b"),\n}));\n')
    assert len(result["carried"]) == 2
    assert result["carried"][1].startswith("retention_analysis:")
    assert len(result["bare"]) == 2
    assert result["bare"][1] == "b"


def test_at_autocomplete_only_opens_on_a_real_mention():
    """A sigil in prose must not open a menu.

    The query is bounded by the same grammar the handle uses, so an email
    address or an `@` that has already been followed by a space is text, not
    an in-progress mention.
    """
    result = _run_chips_script('const q = (t, o) => mod.writingMentionQuery(t, o);\nconst opts = ["KWoman", "KWomanTwo", "Bagger", "Locations"];\nconsole.log(JSON.stringify({\n  midWord: q("she meets @KWo", 14),\n  justSigil: q("she meets @", 11),\n  qualifier: q("@KWoman.spea", 12),\n  notAMention: q("mail me at bob@example", 22),\n  noSigil: q("plain prose", 11),\n  afterSpace: q("@KWoman walks in", 16),\n  afterPeriod: q("outside.@K", 10),\n  afterQuote: q(String.fromCharCode(34) + "@K", 3),\n  afterDash: q("cut-@K", 6),\n  prefixFirst: mod.handleMentionCandidates("KWo", opts).map(c => c.handle),\n  interiorAllowed: mod.handleMentionCandidates("oman", opts).map(c => c.handle),\n  emptyKeepsHostOrder: mod.handleMentionCandidates("", opts).map(c => c.handle),\n  noMatch: mod.handleMentionCandidates("zzz", opts),\n}));\n')
    assert result["midWord"]["handle"] == "KWo"
    assert result["midWord"]["completingQualifier"] is False
    # A bare sigil is a real mention with an empty query: it offers everything.
    assert result["justSigil"]["handle"] == ""
    # Past the dot the menu should offer capability kinds, not handles.
    assert result["qualifier"]["handle"] == "KWoman"
    assert result["qualifier"]["qualifier"] == "spea"
    assert result["qualifier"]["completingQualifier"] is True
    assert result["notAMention"] is None
    # Prose reaches `@` after punctuation far more often than after a bare
    # space. An opener allowlist refused all of these, which is what made the
    # menu look broken in the real panel while passing in a clean fixture.
    assert result["afterPeriod"]["handle"] == "K"
    assert result["afterQuote"]["handle"] == "K"
    assert result["afterDash"]["handle"] == "K"
    assert result["noSigil"] is None
    # The mention ended at the space; the caret is in ordinary prose again.
    assert result["afterSpace"] is None

    assert result["prefixFirst"] == ["KWoman", "KWomanTwo"]
    assert result["interiorAllowed"] == ["KWoman", "KWomanTwo"]
    # No query offers everything, in the order the host supplied.
    assert result["emptyKeepsHostOrder"] == ["KWoman", "KWomanTwo", "Bagger", "Locations"]
    assert result["noMatch"] == []


def test_a_split_discloses_the_channels_it_would_strand():
    """A split continues one channel; the rest stay with the block above.

    Silence there is the failure mode — a new section that quietly drops its
    summary and soundscape compiles to something the author never wrote. Only
    channels that actually hold text count, and the channel being continued is
    never among them.
    """
    result = _run_panel_script('const keys = ["subject_definitions","summary","retention_analysis","detailed_description","overall_soundscape","non_diegetic_music"];\nconst L = (t, a) => mod.writingSplitLinkage(t, a, keys);\nconst nl = String.fromCharCode(10);\nconst populated = "summary:" + nl + "a fashion video" + nl + "detailed_description:" + nl + "she walks in";\nconst onlyActive = "detailed_description:" + nl + "she walks in";\nconst blankOther = "summary:" + nl + nl + "detailed_description:" + nl + "she walks";\nconsole.log(JSON.stringify({\n  stranded: L(populated, "detailed_description"),\n  none: L(onlyActive, "detailed_description"),\n  blankIsNotPopulated: L(blankOther, "detailed_description"),\n  headerInlineText: L("summary: a line" + nl + "detailed_description:" + nl + "x", "detailed_description"),\n  activeItselfNeverListed: L(populated, "summary"),\n}));\n')
    assert result["stranded"] == ["summary"]
    assert result["none"] == []
    # A header with no body strands nothing; there is nothing to inherit.
    assert result["blankIsNotPopulated"] == []
    # Text on the header line itself counts as content.
    assert result["headerInlineText"] == ["summary"]
    # Continuing `summary` strands the body field instead, never itself.
    assert result["activeItselfNeverListed"] == ["detailed_description"]


def test_split_disclosure_measures_only_the_block_it_splits():
    """The notice must not name channels from a different section.

    `writingSplitLinkage` was handed the entire draft, so splitting a block
    whose own other channels were empty still announced channels populated
    somewhere else entirely. A false positive on the only signal the author
    gets is worse than silence.

    `splitWritingDraft` cannot supply the block: it trims each one and filters
    empties, which destroys the offsets needed to find the caret.
    `writingBlockHeadAt` keeps them and returns the head of the impending
    split — the tail travels with the author and is never stranded.
    """
    result = _run_panel_script('const keys = ["subject_definitions","summary","retention_analysis","detailed_description","overall_soundscape","non_diegetic_music"];\nconst nl = String.fromCharCode(10);\nconst head = (t, o) => mod.writingBlockHeadAt(t, o);\nconst draft = "detailed_description:" + nl + "she walks in" + nl + "---" + nl + "summary:" + nl + "a fashion video";\nconst caretInSecondBlock = draft.length;\nconst caretInFirstBlock = draft.indexOf("walks") + 3;\nconsole.log(JSON.stringify({\n  secondBlockHead: head(draft, caretInSecondBlock),\n  firstBlockHead: head(draft, caretInFirstBlock),\n  noBreakYet: head("summary:" + nl + "text", 14),\n  // The whole-draft bug: measuring everything reported summary as stranded\n  // while splitting the FIRST block, where summary is not even present.\n  strandedFromHead: mod.writingSplitLinkage(\n    head(draft, caretInFirstBlock), "detailed_description", keys),\n  strandedFromWholeDraft: mod.writingSplitLinkage(\n    draft, "detailed_description", keys),\n}));\n')
    nl = chr(10)
    assert result["secondBlockHead"] == "summary:" + nl + "a fashion video"
    assert result["firstBlockHead"] == "detailed_description:" + nl + "she wal"
    assert result["noBreakYet"] == "summary:" + nl + "text"
    # Splitting the first block strands nothing — summary lives past the break.
    assert result["strandedFromHead"] == []
    # The defect this fixes: the whole draft reports it anyway.
    assert result["strandedFromWholeDraft"] == ["summary"]


def test_split_disclosure_composition_uses_the_block_not_the_draft():
    """Covers the whole decision, not its parts.

    An earlier version of this coverage tested `writingBlockHeadAt` and
    `writingSplitLinkage` separately and passed with the call site still
    handing over the entire draft — the defect was in the composition, which
    no test could reach while it lived inline in a click handler. Breaking
    the composition must fail here.
    """
    result = _run_panel_script('const keys = ["subject_definitions","summary","retention_analysis","detailed_description","overall_soundscape","non_diegetic_music"];\nconst nl = String.fromCharCode(10);\nconst draft = "detailed_description:" + nl + "she walks in" + nl + "---" + nl + "summary:" + nl + "a fashion video";\nconsole.log(JSON.stringify({\n  splittingFirstBlock: mod.writingSplitDisclosure(draft, draft.indexOf("walks") + 3, keys),\n  splittingSecondBlock: mod.writingSplitDisclosure(draft, draft.length, keys),\n  sameBlockBothChannels: mod.writingSplitDisclosure(\n    "summary:" + nl + "a video" + nl + "detailed_description:" + nl + "she walks",\n    ("summary:" + nl + "a video" + nl + "detailed_description:" + nl + "she walks").length,\n    keys),\n}));\n')
    # `summary` is past the break, so splitting the first block strands nothing.
    assert result["splittingFirstBlock"] == []
    # Splitting the second strands nothing either — its own block has one field.
    assert result["splittingSecondBlock"] == []
    # Both fields in ONE block: splitting the body field does strand summary.
    assert result["sameBlockBothChannels"] == ["summary"]


def test_apply_is_never_gated_on_a_stale_draft():
    """All THREE enforcement points, because removing one leaves the others.

    A test that only asserted the button was enabled would pass while
    `_applyPromptSetup` still threw, and while the mutation still carried a
    version stamped when the draft was built. `modified_at` is a single
    whole-project timestamp bumped by ~50 save sites, so gating on it put
    every draft on a countdown and left authored work with no path in.
    """
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")

    # 1. The button no longer has a staleness term.
    assert "applyBtn.disabled = applyBlocked || !blocks.length;" in panel
    assert "Draft is stale" not in panel

    # 2. The second, independent throw is gone.
    assert "Writing draft is stale" not in widget

    # 3. The mutation no longer pins a version captured when the draft was
    #    built; the fetch layer stamps the freshest one at send time.
    apply_call = widget[widget.index("label: \"apply prompt setup\""):]
    apply_call = apply_call[:apply_call.index("});")]
    assert "expectedModifiedAt" not in apply_call, apply_call


def test_apply_clears_the_draft_only_when_the_write_landed():
    """The failure the staleness gate was hiding.

    `_applyPromptSetup` catches its own async refusal and does not rethrow, so
    the panel could only ever catch a SYNCHRONOUS one. Without an explicit
    outcome check, a 409 or an over-cap attachment set resolved normally, the
    draft and its Restore stash were wiped, and the author was told the apply
    succeeded. Removing the gate makes that the primary failure mode.
    """
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")

    # The outcome is reported rather than thrown, because two other callers
    # simply await this and re-render.
    assert "return true;" in widget[widget.index("async _applyPromptSetup"):]
    apply_fn = widget[widget.index("async _applyPromptSetup"):]
    apply_fn = apply_fn[:apply_fn.index("Browser-local prompt template library")]
    assert "return false;" in apply_fn

    # The panel gates the destructive half on that outcome.
    gated = panel[panel.index("let applied = false;"):]
    gated = gated[:gated.index("clearWritingState(")]
    assert 'typeof applied === "object"' in gated, gated
    assert "if (!appliedSuccessfully)" in gated, gated

    # And Apply now leaves the applied draft restorable rather than dropping
    # the stash with it — it is the only irreversible act on this surface.
    assert "keepAsStash: writingAppliedRestoreSnapshot(writingDraftSnapshot())" in panel


def test_carriage_returns_never_reach_a_document_or_its_channels():
    """CRLF text must parse its headers exactly as LF text does.

    Splitting on the newline leaves a trailing CR on every line, and in
    JavaScript regex `.` excludes CR — so the channel header pattern could
    not match, every header stayed literal text, and all content fell into
    one channel. Python's `.` DOES match CR, so the server parsed the same
    input correctly while keeping a stray CR in the stored text: the two
    splitters disagreed on identical input.

    Normalizing at the document normalizer rather than the paste handler is
    what heals drafts that already contain CRs.
    """
    result = _run_chips_script('const CR = String.fromCharCode(13), LF = String.fromCharCode(10);\nconst keys = ["subject_definitions","summary","retention_analysis","detailed_description","overall_soundscape","non_diegetic_music"];\nconst lf = "detailed_description:" + LF + "body" + LF + "overall_soundscape:" + LF + "ambient";\nconst crlf = lf.split(LF).join(CR + LF);\nconst doc = (t) => ({ schema: "prompt_document_v1", nodes: [{ type: "text", node_id: "t0", text: t }] });\nconst split = (t) => Object.fromEntries(Object.entries(\n  mod.splitPromptDocumentChannels(doc(t), keys, { defaultKey: "detailed_description" }))\n  .map(([k, v]) => [k, mod.promptDocumentText(v)]).filter(([, v]) => v));\nconsole.log(JSON.stringify({\n  normalized: mod.promptDocumentText(mod.normalizePromptDocument(doc(crlf))),\n  fromLF: split(lf),\n  fromCRLF: split(crlf),\n}));\n')
    cr = chr(13)
    assert cr not in result["normalized"]
    # Identical input, identical routing — that is the whole point.
    assert result["fromCRLF"] == result["fromLF"]
    assert result["fromCRLF"]["overall_soundscape"] == "ambient"
    assert "detailed_description:" not in result["fromCRLF"]["detailed_description"]


def test_the_two_document_normalizers_agree_on_carriage_returns():
    """Parity pair: the browser and server normalizers must strip identically."""
    from server import prompt_context as pc
    cr, lf = chr(13), chr(10)
    raw = "a" + cr + lf + "b" + cr + "c"
    server_text = pc.prompt_document_text(pc.normalize_prompt_document(
        {"nodes": [{"type": "text", "node_id": "t", "text": raw}]}))
    browser_text = _run_chips_script(
        "const CR = String.fromCharCode(13), LF = String.fromCharCode(10);" + chr(10)
        + "console.log(JSON.stringify({ t: mod.promptDocumentText("
        "mod.normalizePromptDocument({ nodes: [{ type: \"text\", node_id: \"t\","
        " text: \"a\" + CR + LF + \"b\" + CR + \"c\" }] })) }));" + chr(10))["t"]
    assert server_text == browser_text == "a" + lf + "b" + lf + "c"


def test_mention_ranking_preserves_the_source_a_row_needs_to_attach():
    """Ranking must not narrow the row to `{handle, label}`.

    Accepting a mention builds a Reference attachment from the row's `value`
    — the semantic unit id, or `physical:<population>:<member_id>`. When the
    ranker returned only the handle and the label, that id was silently lost
    and the accept produced a chip whose source was empty, referencing
    nothing. `eligible` matters for the same reason: an ineligible row is
    listed to explain itself but must not be attachable.
    """
    result = _run_chips_script('const opts = [\n  { handle: "KWoman", label: "Subject: KoreanWoman", value: "unit-1", eligible: true },\n  { handle: "Street", label: "Picture source: Locations", value: "physical:pictures:m-9", eligible: false },\n];\nconsole.log(JSON.stringify({\n  ranked: mod.handleMentionCandidates("", opts),\n  filtered: mod.handleMentionCandidates("Str", opts),\n}));\n')
    assert [r["handle"] for r in result["ranked"]] == ["KWoman", "Street"]
    assert result["ranked"][0]["value"] == "unit-1"
    assert result["ranked"][1]["value"] == "physical:pictures:m-9"
    assert result["ranked"][1]["eligible"] is False
    # Filtering must carry the same fields through.
    assert result["filtered"][0]["value"] == "physical:pictures:m-9"


def test_mention_candidates_join_handles_onto_attachable_sources():
    """`promptReferenceSourceOptions` rows carry no handle; this is the join.

    Its values are `physical:<pop>:<member_id>` or a semantic unit id, while
    handles live on the member and the unit. A source with no handle has no
    spelling to complete, so it is dropped rather than shown — a typeahead
    cannot offer something untypeable.
    """
    result = _run_chips_script("""
const options = {
  unitOptions: [["unit-1", "Subject: KoreanWoman", true]],
  physicalOptions: [
    ["physical:pictures:m-9", "Picture source: Locations", true],
    ["physical:pictures:m-nohandle", "Picture source: Unnamed", true],
  ],
};
const references = [{ members: [
  { member_id: "m-9", handle: "Street" },
  { member_id: "m-nohandle", handle: "" },
]}];
const semanticUnits = [{ semantic_unit_id: "unit-1", handle: "KWoman" }];
console.log(JSON.stringify(mod.promptMentionCandidates(
  { options, references, semanticUnits })));
""")
    assert [r["handle"] for r in result] == ["KWoman", "Street"]
    assert result[0]["value"] == "unit-1"
    assert result[1]["value"] == "physical:pictures:m-9"
    # The handleless member is offered by the caret menu but not by a typeahead.
    assert all("nohandle" not in r["value"] for r in result)


def test_reset_then_apply_reproduces_the_same_sections():
    """The parity claim the Writing pivot rests on, over the real projection.

    Chips are the record and Writing renders them; Structured edits them. That
    is only "one record, two views" if a draft rebuilt from sections and applied
    unchanged writes those sections back. The document functions round-trip on
    their own, so a test over them alone stays green against a build that
    relocates every section — the projection itself is what has to be pinned,
    which is why it was extracted out of the Apply closure to be reachable.

    Also pins the two documented NON-identities, so neither can regress into a
    silent surprise: bounds recompact from 0, and reused chips coalesce.
    """
    result = _run_panel_script(r"""
        const chips = await import(CHIPS_URL);
        const keys = ["summary", "detailed_description"];
        const chip = { attachment_id: "ref-1", kind: "reference",
            source: { reference_item_id: "item" }, config: {} };
        const sections = [
            { prompt_id: "p1", start_frame: 0, end_frame: 60,
              channels: { summary: "alpha", detailed_description: "says one" },
              channel_docs: {
                  summary: { nodes: [{ type: "text", node_id: "s1", text: "alpha" }] },
                  detailed_description: { nodes: [
                      { type: "text", node_id: "d1", text: "says one " },
                      { type: "attachment", node_id: "a1", attachment_id: "ref-1" }] },
              }, attachments: [chip], muted: false, global_channel_exceptions: [] },
            { prompt_id: "p2", start_frame: 60, end_frame: 130,
              channels: { summary: "beta", detailed_description: "says two" },
              channel_docs: {
                  summary: { nodes: [{ type: "text", node_id: "s2", text: "beta" }] },
                  detailed_description: { nodes: [{ type: "text", node_id: "d2", text: "says two" }] },
              }, attachments: [], muted: true, global_channel_exceptions: ["summary"] },
        ];
        // Exactly what `reconstructDraftFromSections` builds, then what Apply
        // projects back — the whole Reset -> Apply path in miniature.
        const roundTrip = (input) => {
            const normalized = input.map((section) => ({ ...section,
                channel_docs: Object.fromEntries(keys.map((key) => [key,
                    chips.healSeparatorPadding(chips.normalizePromptDocument(
                        section.channel_docs?.[key], section.channels?.[key] || ""))])) }));
            const document = chips.joinWritingSectionDocuments(normalized, keys);
            const blocks = chips.splitWritingPromptDocument(document,
                { keepEmpty: input.length > 0 });
            return mod.writingSectionsFromDraft({
                blocks,
                attachments: input.flatMap((section) => section.attachments || []),
                blockMeta: input.map((section) => ({
                    source_prompt_id: section.prompt_id,
                    merged_source_prompt_ids: [section.prompt_id],
                    muted: section.muted === true,
                    global_channel_exceptions: section.global_channel_exceptions || [],
                    attachments: (section.attachments || []).filter((value) =>
                        !Object.values(section.channel_docs || {}).some((doc) =>
                            (doc.nodes || []).some((node) =>
                                node.attachment_id === value.attachment_id))),
                })),
                allocations: input.map((section) => ({
                    length: section.end_frame - section.start_frame })),
                channelKeys: keys, defaultKey: "detailed_description", minLen: 1,
                newId: (index) => `fresh:${index}`,
            }).sections;
        };
        const compare = (rows) => rows.map((section) => ({
            prompt_id: section.prompt_id,
            bounds: [section.start_frame, section.end_frame],
            channels: section.channels,
            muted: section.muted,
            exceptions: section.global_channel_exceptions,
            attachments: section.attachments.map((value) => value.attachment_id),
            anchors: keys.flatMap((key) => (section.channel_docs[key].nodes || [])
                .filter((node) => node.type === "attachment")
                .map((node) => node.attachment_id)),
        }));
        const once = roundTrip(sections);
        const twice = roundTrip(once);

        // Documented non-identity 1: a lane with a gap recompacts from 0.
        const gapped = roundTrip(sections.map((section, index) => index === 0
            ? section : { ...section, start_frame: 90, end_frame: 160 }));

        // Documented non-identity 2: `reusePromptAttachment` keeps the emission
        // group and mints a new object id, and `scopedAttachmentIdentity` drops
        // only the object id — so a reused chip is identity-equal to its source
        // and the scope copy coalesces onto the anchored one. An INDEPENDENT
        // chip does not, because `emission_group_id` defaults to its own id.
        const reused = chips.reusePromptAttachment(chip);
        const independent = { ...structuredClone(chip), attachment_id: "ref-3",
            emission_group_id: "ref-3" };
        const withReuse = structuredClone(sections);
        withReuse[0].attachments = [chip, reused];
        const withIndependent = structuredClone(sections);
        withIndependent[0].attachments = [chip, independent];

        // A MERGED block absorbs its neighbour's prompt id, so a Prompt Link
        // pointing at the absorbed id has to follow the survivor or it dangles.
        // Apply always rebound; the compile preview carried its own copy of
        // this projection and did NOT, so preview could show a link Apply would
        // repoint. One projection now serves both, and this is the case that
        // tells them apart — without it the remap is an identity no-op in every
        // fixture and the shared code path is executed but never discriminated.
        const linked = structuredClone(sections);
        linked[0].attachments = [{ attachment_id: "link-1", kind: "prompt_link",
            source: { prompt_id: "p2" }, config: {} }];
        const merged = mod.writingSectionsFromDraft({
            blocks: [chips.joinWritingSectionDocuments([linked[0]], keys)],
            attachments: linked[0].attachments,
            blockMeta: [{ source_prompt_id: "p1",
                merged_source_prompt_ids: ["p1", "p2"],
                attachments: linked[0].attachments }],
            allocations: [{ length: 130 }], channelKeys: keys,
            defaultKey: "detailed_description", minLen: 1,
            newId: (index) => `fresh:${index}`,
        }).sections;

        console.log(JSON.stringify({
            first: compare(once), second: compare(twice),
            mergedLinkTarget: merged[0].attachments
                .find((v) => v.kind === "prompt_link")?.source?.prompt_id,
            mergedPromptId: merged[0].prompt_id,
            gappedBounds: gapped.map((s) => [s.start_frame, s.end_frame]),
            reusedCount: roundTrip(withReuse)[0].attachments.length,
            independentIds: roundTrip(withIndependent)[0].attachments
                .map((v) => v.attachment_id).sort(),
        }));
    """.replace("CHIPS_URL", json.dumps(
        (ROOT / "web/js/prompt_context_chips.js").as_uri())))

    first, second = result["first"], result["second"]
    # Identity, and identity that HOLDS — a projection that converged only on
    # the second pass would still have rewritten the author's lane on the first.
    assert first == second
    assert [row["prompt_id"] for row in first] == ["p1", "p2"]
    assert [row["bounds"] for row in first] == [[0, 60], [60, 130]]
    assert [row["channels"]["summary"] for row in first] == ["alpha", "beta"]
    assert [row["channels"]["detailed_description"] for row in first] == [
        "says one", "says two"]
    # The inline anchor survives as an anchor, and its record travels with it.
    assert first[0]["anchors"] == ["ref-1"]
    assert first[0]["attachments"] == ["ref-1"]
    # Per-block flags are carried, not recomputed from the lane.
    assert [row["muted"] for row in first] == [False, True]
    assert [row["exceptions"] for row in first] == [[], ["summary"]]
    # Recompaction from 0 is by construction: the draft owns lengths, not
    # positions, so a gapped lane does NOT come back unchanged.
    assert result["gappedBounds"] == [[0, 60], [60, 130]]
    # A link into an absorbed block follows the survivor rather than dangling.
    # This is the ONLY case that separates the shared projection from the
    # preview copy it replaced, which skipped the rebinding.
    assert result["mergedPromptId"] == "p1"
    assert result["mergedLinkTarget"] == "p1"
    # A reused chip coalesces onto its twin rather than emitting twice...
    assert result["reusedCount"] == 1
    # ...while an independent chip carrying the same config does NOT, because
    # its emission group is its own. Coalescing those would silently delete a
    # deliberate second attachment, so the pair has to be tested together.
    assert result["independentIds"] == ["ref-1", "ref-3"]


def test_an_empty_draft_is_not_mistaken_for_authored_work():
    """The predicate that decides whether a Writing draft holds anything.

    Its predecessor tested whether a document OBJECT existed rather than whether
    it held anything, and an emptied draft stores a document of one empty text
    node — truthy. Three consequences, all observed in a real project: Reset
    asked permission to discard nothing, `loadWritingState` restored the
    emptiness instead of rebuilding from sections so the panel opened blank, and
    an empty stash sat behind a "Restore draft" button that restored nothing.

    A blank panel plus one Apply writes ZERO sections, and Apply is deliberately
    unconfirmed, so getting this wrong costs the whole prompt lane.

    The opposite error is just as bad and is why this is not a text check: chips
    live in three places, and a draft whose only content is a chip attached with
    "Attach to this section/scene" has no text AND no entry in the editor's
    attachment registry. Treating that as empty would discard exactly the work
    this predicate exists to protect.
    """
    result = _run_panel_script(r"""
        const doc = (nodes) => ({ nodes });
        const empty = [{ type: "text", node_id: "a", text: "" }];
        const cases = {
            trulyEmpty: { draft: "", document: doc(empty) },
            whitespaceOnly: { draft: "   ", document: doc([{ type: "text", node_id: "a", text: "   " }]) },
            nothingAtAll: {},
            prose: { draft: "hello", document: doc([{ type: "text", node_id: "a", text: "hello" }]) },
            inlineChipOnly: { draft: "", document: doc([
                { type: "attachment", node_id: "a", attachment_id: "ref" }]) },
            registryChipOnly: { draft: "", document: doc(empty),
                attachments: [{ attachment_id: "ref", kind: "reference" }] },
            sectionScopedOnly: { draft: "", document: doc(empty),
                blockMeta: [{ attachments: [{ attachment_id: "ref", kind: "reference" }] }] },
        };
        console.log(JSON.stringify(Object.fromEntries(
            Object.entries(cases).map(([name, value]) =>
                [name, mod.writingDraftHasContent(value)]))));
    """)
    # Nothing worth keeping.
    assert result["trulyEmpty"] is False
    assert result["whitespaceOnly"] is False
    assert result["nothingAtAll"] is False
    # Prose, obviously.
    assert result["prose"] is True
    # ...and a chip in each of the three places one can live. The last is the
    # one a text-only predicate would throw away.
    assert result["inlineChipOnly"] is True
    assert result["registryChipOnly"] is True
    assert result["sectionScopedOnly"] is True


def test_writing_pending_identity_create_is_reachable_only_while_draft_references_it():
    result = _run_panel_script(r"""
        const create = (id, name) => ({type:"create_prompt_semantic_unit",
            handle_suggestion:name, unit:{semantic_unit_id:id,name,kind:"subject"}});
        const vocal = (attachmentId, ids) => ({attachment_id:attachmentId,
            emission_group_id:attachmentId,kind:"vocal_event",
            source:{subject_ids:ids},config:{},capabilities:[]});
        const creates = [create("one","One"),create("two","Two")];
        const direct = mod.reachableWritingSemanticUnitCreates(creates,
            [vocal("a",["one"])], []);
        const scoped = mod.reachableWritingSemanticUnitCreates(creates, [],
            [{attachments:[vocal("b",["two"])]}]);
        const removed = mod.reachableWritingSemanticUnitCreates(creates, [], []);
        console.log(JSON.stringify({
            direct:direct.map((value)=>value.unit.semantic_unit_id),
            scoped:scoped.map((value)=>value.unit.semantic_unit_id),
            removed,
            content:mod.writingDraftHasContent({pendingSemanticUnitCreates:creates}),
        }));
    """)
    assert result == {
        "direct": ["one"], "scoped": ["two"], "removed": [],
        "content": True,
    }


def test_writing_apply_restore_survives_empty_current_reload_without_pending_creates():
    result = _run_panel_script(r"""
        const create = {type:"create_prompt_semantic_unit",
            unit:{semantic_unit_id:"pending-1",name:"Narrator",kind:"subject"}};
        const authored = {draft:"hello",document:{nodes:[{type:"text",node_id:"t",text:"hello"}]},
            pendingSemanticUnitCreates:[create]};
        const appliedStash = mod.writingAppliedRestoreSnapshot(authored);
        const load = mod.writingDraftLoadState({draft:"",document:null,stash:appliedStash});
        const full = Array.from({length:64},(_,index)=>({type:"create_prompt_semantic_unit",
            unit:{semantic_unit_id:`pending-${index}`,name:`Speaker ${index}`,kind:"subject"}}));
        const refused = mod.stageWritingSemanticUnitCreate(full,{type:"create_prompt_semantic_unit",
            unit:{semantic_unit_id:"pending-65",name:"Speaker 65",kind:"subject"}});
        const replacement = mod.stageWritingSemanticUnitCreate(full,{type:"create_prompt_semantic_unit",
            unit:{semantic_unit_id:"pending-5",name:"Renamed",kind:"subject"}});
        console.log(JSON.stringify({load,appliedStash,
            refused:{accepted:refused.accepted,length:refused.creates.length},
            replacement:{accepted:replacement.accepted,length:replacement.creates.length,
                name:replacement.creates.at(-1).unit.name}}));
    """)
    assert result["load"]["useSavedDraft"] is False
    assert result["load"]["stash"]["draft"] == "hello"
    assert result["appliedStash"]["pendingSemanticUnitCreates"] == []
    assert result["refused"] == {"accepted": False, "length": 64}
    assert result["replacement"] == {
        "accepted": True, "length": 64, "name": "Renamed"}


def test_writing_pending_identity_overlay_uses_shared_preview_and_atomic_apply_paths():
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    builder_start = widget.index("    _promptCompileRequestBody({")
    builder_end = widget.index("    _promptProjectionSubset(", builder_start)
    scene_start = widget.index("    _previewPromptContextScenePayload({")
    window_start = widget.index("    _previewPromptContextCandidate(", scene_start)
    window_end = widget.index("    _refreshPromptUsageHighlight(", window_start)
    builder = widget[builder_start:builder_end]
    scene_preview = widget[scene_start:window_start]
    window_preview = widget[window_start:window_end]
    apply = widget[widget.index("async _applyPromptSetup("):
                   widget.index("/** Browser-local prompt template library")]

    assert "prompt_semantic_unit_creates:" in builder
    assert "promptSemanticUnitCreates" in scene_preview
    assert "promptSemanticUnitCreates" in window_preview
    assert "pruneWritingSemanticUnitCreates())" in panel
    assert "prompt_semantic_unit_creates: promptSemanticUnitCreates" in panel
    create_at = apply.index('type: "create_prompt_semantic_unit"')
    replace_at = apply.index('type: "replace_prompt_sections"')
    assert create_at < replace_at
    assert "pending: identityCreateIntents.length > 0" in apply
    assert "_refreshAndReconcilePromptSetupIdentityCreates" in apply


def test_cross_project_prompt_template_exports_vocal_identity_dependency_closure():
    widget = _source("web/js/editor_widget.js")
    save = _method(widget, "_savePromptTemplate", "_deletePromptTemplate")
    chips_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    result = _run_node(f"""
const {{semanticIdentityDependencyIds}}=await import({json.dumps(chips_url)});
const normalizeChannels=(value)=>value||{{}};
const normalizeChannelExceptions=(value)=>value||[];
const templateFreezeValue=(value)=>value;
globalThis.notifySuccess=()=>{{}};
class Harness {{
{save}
  constructor() {{
    this.activeScene={{prompt:"",global_channels:{{}},global_channel_docs:{{}},
      global_attachments:[],prompt_context_profile_id:"",prompt_sections:[{{
        prompt_id:"p",start_frame:0,end_frame:24,channels:{{visual:"hello"}},
        attachments:[{{kind:"vocal_event",source:{{subject_ids:["speaker"],
          voice_id:"provider-voice"}},config:{{audio_speaker_subject_id:"narrator"}}}}]
      }}]}};
    this._promptContextProfiles=[]; this._effectiveFps=24;
    this._promptSemanticUnits=["speaker","narrator","provider-voice"].map((id)=>({{
      semantic_unit_id:id,name:id,kind:"subject"}}));
    this.saved=null;
  }}
  _channelTemplate() {{ return {{id:"standard",channels:[{{key:"visual"}}]}}; }}
  _getPromptTemplates() {{ return []; }}
  _updateSettings(value) {{ this.saved=value.promptTemplates[0]; }}
}}
const h=new Harness(); h._savePromptTemplate("Vocal");
console.log(JSON.stringify(h.saved.prompt_semantic_units.map((value)=>value.semantic_unit_id)));
""")
    assert result == ["speaker", "narrator"]
