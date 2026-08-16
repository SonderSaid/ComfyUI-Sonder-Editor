"""Phase 1 contracts for visible compile failures and cooperative prompt bars."""

import json
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
    assert [(row["action"], row["aria"]) for row in rows] == [
        ("+", "Create prompt identity from physical Reference"),
        ("×", "Delete prompt identity"),
        ("+", "Create prompt identity"),
    ]


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
  && n.children.some((c)=>String(c.textContent || "").startsWith("@Portrait")));
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
        {"title": "Advanced — attachment defaults", "open": False},
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
console.log(JSON.stringify({{config: attachment.config, defaults,inherited,
  authoredEmpty,taskEmpty,capabilityWins,stagedDefinition,physical,physicalFallback}}));
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
    assert result["defaults"]["fieldSources"]["summary"] == {
        "label": "Shared identity default · @Lead", "tier": "shared"}
    assert result["defaults"]["fieldSources"]["task_types"] == {
        "label": "Prompt Format default · Format A", "tier": "format"}
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
        "definition": {"label": "Prompt Format default · Format A", "tier": "format"},
        "visual_intent": {"label": "Staged Reference default · @Portrait", "tier": "shared"},
        "audio_intent": {"label": "Staged Reference default · @Portrait", "tier": "shared"},
    }
    assert result["physicalFallback"]["values"] == {
        "visual_intent": "preserve",
        "audio_intent": "reference_characteristics"}
    assert result["physicalFallback"]["fieldSources"] == {
        "visual_intent": {"label": "Renderer fallback · Format A", "tier": "format"},
        "audio_intent": {"label": "Renderer fallback · Format A", "tier": "format"},
    }
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
    assert result["physical"]["capabilities"] == [
        {"capability_id": "definitions", "kind": "definitions",
         "channel_key": "", "placement": "", "enabled": True, "config": {}},
        {"capability_id": "mentions", "kind": "mentions",
         "channel_key": "", "placement": "", "enabled": True, "config": {}},
    ]
    assert result["identity"]["capabilities"] == [{
        "capability_id": "summary", "kind": "summary",
        "channel_key": "", "placement": "", "enabled": True, "config": {},
    }]
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert panel.index("_materializeReferenceMemberHandle") < panel.index(
        "const committed = await commit(operations, label, history)")
    assert "referenceOperations" in panel and "inverseReferenceOperations" in panel
    assert "referenceOperations: structuredClone" in widget
    assert "await this._mutateReferences(entry.referenceOperations" in widget


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
    assert 'payload.compiled_prompt' in panel
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
    assert 'add.textContent = "Attach to this section/scene";' in chips
    scope = chips[chips.index("export function createScopeChipRow"):]
    assert "add.title" not in scope


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


def test_prompt_panel_consumes_only_windowed_candidate_diagnostics():
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert "_promptPayloadCache" not in panel
    assert "currentCandidatePayload()?.attachment_previews" in panel
    assert "refreshDiagnostics: renderDiagnostics" in panel
    assert "_candidate_scene_id: sceneId" in widget
    assert "const candidateSelection = resolvePromptCandidateSelection(" in widget
    assert "selection_start: candidateSelection.selectionStart" in widget
    assert "selection_end: candidateSelection.selectionEnd" in widget
    assert "this._promptPayloadCache = payload" not in widget


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
        "reference_lane_recipes": [{"lane_id": "pictures"}],
        "active_minimax_h3_setup_id": "setup",
        "minimax_h3_conditioning_setups": [{
            "setup_id": "setup", "mode": "reference",
            "picture_lane_ids": ["pictures"],
        }],
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
        "physicalInherited": {"value": "", "source": ""},
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
    assert widget.count("attachmentLabelFor,") == 3
    assert panel.count("attachmentLabelFor,") == 8
    assert "Shared identity default · @" in chips
    assert "Prompt Format default ·" in chips
    assert "no managed Vocal Event in this window" in chips
    assert "managedVocalEventSubjectIds" not in chips
    assert widget.count("managedSpeakerSubjectIds:") == 3
    assert panel.count("managedSpeakerSubjectIds:") == 7
    assert 'overridableFieldRow("Summary task types", "task_types"' in chips
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
