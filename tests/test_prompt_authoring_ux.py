"""Phase 1 contracts for visible compile failures and cooperative prompt bars."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import prompt_context


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _run_node(script: str):
    """Execute an exported predicate under node and return its JSON output."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for this test")
    return json.loads(subprocess.run(
        [node, "--input-type=module"], input=script, capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def test_reference_override_fields_mirror_matches_the_server():
    chips_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    client = _run_node(f"""
const {{REFERENCE_OVERRIDE_FIELDS}}=await import({json.dumps(chips_url)});
console.log(JSON.stringify(REFERENCE_OVERRIDE_FIELDS));
""")
    assert set(client) == set(prompt_context.REFERENCE_OVERRIDE_FIELDS)


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
    attachment_defaults:{{audio_retention_detail:"Keep the exact voice"}},
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
const audioRetention=nodes.find((n)=>n.tagName==="TEXTAREA"
  && n.placeholder==="Default audio preservation detail (optional)");
const preserved=nodes.find((n)=>n.dataset.sonderPreservedIdentityDefaults==="1");
const save=nodes.find((n)=>n.tagName==="BUTTON" && n.textContent==="Save identity");
await save._handlers.click[0]();
console.log(JSON.stringify({{unsupported,saved,audioRetention:audioRetention?.value || "",
  preserved:preserved?.textContent || ""}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["unsupported"] == [
        "future_audio", "future_contribution", "future_visual"]
    assert result["saved"]["voice"] == {"member_id": "missing_voice"}
    assert result["audioRetention"] == "Keep the exact voice"
    assert result["preserved"] == ""
    assert result["saved"]["attachment_defaults"]["audio_retention_detail"] == (
        "Keep the exact voice")
    assert result["saved"]["visual_intent"] == "future_visual"
    assert result["saved"]["audio_intent"] == "future_audio"
    assert result["saved"]["sources"][0]["contribution"] == "future_contribution"
    assert result["saved"]["voice"]["member_id"] == "missing_voice"


def test_legacy_voice_repair_is_explicit_idempotent_and_one_save():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for legacy voice repair coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
class N {{
  constructor(tag) {{ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.options=[]; this.style={{cssText:""}}; this.dataset={{}}; this.attributes={{}};
    this.value=""; this.textContent=""; this.disabled=false; this.checked=false;
    this.open=false; this.multiple=false; this.selected=false; this._handlers={{}}; }}
  appendChild(c) {{ if(typeof c==="string"){{const t=new N("#text");t.textContent=c;c=t;}}
    this.children.push(c); c.parentElement=this;
    if(c.tagName==="OPTION") this.options.push(c); return c; }}
  append(...cs) {{ cs.forEach((c)=>c!=null && this.appendChild(c)); }}
  prepend(...cs) {{ [...cs].reverse().forEach((c)=>{{ if(typeof c==="string"){{const t=new N("#text");t.textContent=c;c=t;}}
    this.children.unshift(c); c.parentElement=this; }}); }}
  addEventListener(t,h) {{ (this._handlers[t] ||= []).push(h); }}
  setAttribute(k,v) {{ this.attributes[k]=String(v); }}
  removeAttribute(k) {{ delete this.attributes[k]; }}
  querySelector() {{ return null; }}
  remove() {{ if(this.parentElement) this.parentElement.children =
    this.parentElement.children.filter((c)=>c!==this); }}
  focus() {{ globalThis.document.activeElement=this; }}
  get selectedOptions() {{ return this.options.filter((option)=>option.selected); }}
}}
globalThis.document={{createElement:(t)=>new N(t),createTextNode:(v)=>{{const n=new N("#text");n.textContent=String(v);return n;}},body:new N("body"),activeElement:null}};
globalThis.window={{addEventListener(){{}},removeEventListener(){{}}}};
globalThis.localStorage={{getItem(){{return null;}},setItem(){{}}}};
globalThis.CSS={{escape:(v)=>String(v)}};
const mod=await import({json.dumps(module_url)});
const legacy={{semantic_unit_id:"unit",kind:"subject",name:"Lead",handle:"Lead",
  definition:"Lead",sources:[],voice:{{member_id:"voice-member"}}}};
const references=[{{reference_id:"ref",name:"Voice Ref",members:[{{
  member_id:"voice-member",asset_id:"asset",name:"Voice Clip"}}]}}];
const pure=mod.legacyVoiceBindingRepair(legacy,references);
const repeated=mod.legacyVoiceBindingRepair(pure.identity,references);
const already=mod.legacyVoiceBindingRepair({{...legacy,sources:[{{
  entity_id:"ref",member_id:"voice-member",contribution:""}}]}},references);
const root=new N("div"); let saves=[];
mod.mountPromptIdentityPanel(root, {{
  profile:{{identity_kinds:[{{key:"subject",label:"Subject"}}],
    physical_populations:[{{key:"audios",media_kinds:["audio"]}}]}},
  references,assets:[{{asset_id:"asset",asset_type:"audio"}}],
  semanticUnits:[legacy],candidate:{{setup_manifest:{{}}}},
  saveSemanticUnitChange:async(change)=>{{saves.push(change.value);}},
}});
const walk=(n,out=[])=>{{out.push(n);n.children.forEach((c)=>walk(c,out));return out;}};
walk(root).find((n)=>n.tagName==="BUTTON" && n.textContent==="Edit")._handlers.click[0]();
const modal=globalThis.document.body.children.at(-1);
const nodes=walk(modal);
const repair=nodes.find((n)=>n.dataset.sonderLegacyVoiceRepair==="1");
const repairButton=nodes.find((n)=>n.tagName==="BUTTON" && n.textContent==="Add physical source");
const repairDetails=nodes.find((n)=>n.tagName==="DETAILS"
  && walk(n,[]).includes(repair));
nodes.find((n)=>n.tagName==="INPUT" && n.placeholder==="Identity name").value="Edited Lead";
await repairButton._handlers.click[0]();
console.log(JSON.stringify({{pure,repeated,already,repair:!!repair,
  repairOpen:repairDetails?.open===true,saves}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["pure"]["identity"]["sources"] == [{
        "entity_id": "ref", "member_id": "voice-member", "contribution": ""}]
    assert "voice" not in result["pure"]["identity"]
    assert result["repeated"] is None
    assert result["already"] is None
    assert result["repair"] is True
    assert result["repairOpen"] is True
    assert len(result["saves"]) == 1
    assert result["saves"][0]["name"] == "Edited Lead"
    assert result["saves"][0]["sources"] == result["pure"]["identity"]["sources"]
    assert "voice" not in result["saves"][0]


def test_identity_source_groups_follow_asset_kind_with_video_audio_exception():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for identity source grouping coverage")
    module_url = (ROOT / "web/js/prompt_identity_panel.js").as_uri()
    script = f"""
const mod=await import({json.dumps(module_url)});
console.log(JSON.stringify({{
  image:mod.promptIdentitySourceGroup("image",{{}},{{}}),
  audio:mod.promptIdentitySourceGroup("audio",{{}},{{}}),
  videoDefault:mod.promptIdentitySourceGroup("video",
    {{audio_intent:"reference_characteristics"}},{{}}),
  videoEntityAudio:mod.promptIdentitySourceGroup("video",
    {{audio_intent:"copy_full"}},{{}}),
  videoMemberAudio:mod.promptIdentitySourceGroup("video",
    {{audio_intent:"reference_characteristics"}},
    {{audio_intent:"reference_loosely"}}),
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "image": "appearance", "audio": "audio",
        "videoDefault": "appearance", "videoEntityAudio": "audio",
        "videoMemberAudio": "audio",
    }
    source = _source("web/js/prompt_identity_panel.js")
    assert "Audio sources declare this character's voice." in source


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
    assert 'height:72px;flex:0 0 72px' in panel
    assert "height:100%;overflow-y:auto;box-sizing:border-box" in panel


def test_prompt_tool_rebuilds_scope_rows_after_attachment_transactions():
    panel = _source("web/js/editor_prompt_panel.js")
    assert "renderGlobalScope();\n                            commitGlobal()" in panel
    assert "renderScope();\n                                commitChannels()" in panel


def test_context_actions_are_named_by_inline_vs_scope_semantics():
    chips = _source("web/js/prompt_context_chips.js")
    assert 'label: "Mention"' in chips
    assert 'label: "Attach"' in chips
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


def _prompt_compile_test_support(widget):
    """Run the actual shared request/cache helpers in isolated host harnesses."""
    methods = _method(widget, "_requestPromptContextCompile", "_promptCompileRequestBody")
    api_url = (ROOT / "web/js/api_client.js").as_uri()
    return f"""
const {{ getProjectVersion, rememberProjectVersion, resetProjectVersion,
    rememberProjectVersionFromPayload, rememberProjectVersionFromResponse }} =
    await import({json.dumps(api_url)});
class CompileSupport {{
  _projectDirName() {{ return "project"; }}
  {methods}
}}
"""


def test_copy_plan_executes_the_scope_used_by_its_projection_row():
    """Dormant Copy behavior, not merely its source spelling, stays scene-wide."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Copy scope coverage")
    widget = _source("web/js/editor_widget.js")
    method = _method_body(
        widget, "_promptCopyPlan", marker="return payload?.copy_plan || null")
    script = _prompt_compile_test_support(widget) + """
const api = { apiURL: (value) => value };
const requests = [];
globalThis.fetch = async (_url, options) => {
  requests.push(JSON.parse(options.body));
  return { ok: true, json: async () => ({ copy_plan: { lines: [] } }) };
};
class Subject extends CompileSupport {
  async """ + method + """
  constructor() {
    super();
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
        "section_channel_previews", "section_window_states"}, keep

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


def test_timeline_prompt_labels_gate_compiles_by_collapse_and_debounce_hot_refreshes():
    widget = _source("web/js/editor_widget.js")
    timeline = _method_body(widget, "_promptContextTimelineConsumerMounted")
    assert "_promptLayoutIdx()" in timeline
    assert "_globalPromptLayoutIdx()" in timeline
    assert "collapsed !== true" in timeline
    code = " ".join(line.split("//")[0] for line in timeline.splitlines())
    assert "hidden" not in code

    delay = _method_body(widget, "    _promptContextPreviewDelay")
    assert "_promptContextEditingConsumersMounted()" in delay
    assert "PROMPT_TIMELINE_COMPILE_DELAY_MS" in delay
    # Widget-state, scene-switch, context, selection, and dependency gates all
    # pass through the timeline-only debounce instead of hard-coding zero.
    assert widget.count("this._promptContextPreviewDelay(0)") >= 5

    scene = _method_body(widget, "_previewPromptContextScenePayload",
                         marker="this._renderTimeline();")
    windowed = _method_body(widget, "    _previewPromptContextCandidate",
                            marker="this._renderTimeline();")
    # HTTP failure and invalid JSON now share one repaint branch.
    assert scene.count("this._renderTimeline();") >= 2
    assert windowed.count("this._renderTimeline();") >= 3


def test_expanding_the_first_prompt_lane_schedules_its_initial_compile():
    widget = _source("web/js/editor_widget.js")
    transition = _method(
        widget, "_previewPromptContextForNewConsumer",
        "_refreshPromptContextDependencyConsumers")
    result = _run_node(f"""
const PROMPT_TIMELINE_COMPILE_DELAY_MS = 180;
class Subject {{
{transition}
  constructor(mounted) {{ this.mounted = mounted; this.calls = []; }}
  _promptContextConsumersMounted() {{ return this.mounted; }}
  _promptContextEditingConsumersMounted() {{ return false; }}
  _promptContextPreviewDelay(immediateDelay = 0) {{
    return this._promptContextEditingConsumersMounted()
      ? immediateDelay : PROMPT_TIMELINE_COMPILE_DELAY_MS;
  }}
  _previewPromptContextCandidate(options, delay) {{ this.calls.push([options, delay]); }}
}}
const dormant = new Subject(false);
const expanded = new Subject(true);
console.log(JSON.stringify({{
  dormant: [dormant._previewPromptContextForNewConsumer(false), dormant.calls],
  expanded: [expanded._previewPromptContextForNewConsumer(false), expanded.calls],
  alreadyMounted: [expanded._previewPromptContextForNewConsumer(true), expanded.calls],
}}));
""")
    assert result["dormant"] == [False, []]
    assert result["expanded"] == [True, [[{}, 180]]]
    assert result["alreadyMounted"] == [False, [[{}, 180]]]

    settings = _method(widget, "_handleSettingsChange", "_syncSettingsPanelControls")
    timeline_events = _method(widget, "_setupTimelineEvents", "_resolveDropHoverTarget")
    for path in (settings, timeline_events):
        assert "const hadPromptConsumer = this._promptContextConsumersMounted();" in path
        assert "this._previewPromptContextForNewConsumer(hadPromptConsumer);" in path


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


def test_stale_cache_marking_is_immediate_and_grace_starts_with_request():
    widget = _source("web/js/editor_widget.js")
    definition = widget[widget.index("\n    _previewPromptContextCandidate("):]
    preview = _method_body(definition, "_previewPromptContextCandidate",
                           marker="Math.max(0, Number(delay) || 0)")
    assert "PROMPT_STALE_VISUAL_DELAY_MS = 300" in widget
    assert "_promptContextCandidateCache = {" in preview
    assert "_promptContextScenePayloadCache = {" in preview
    compile_timer = preview.index("_promptContextPreviewTimer = setTimeout")
    stale_timer = preview.index("_promptContextStaleVisualTimer = setTimeout")
    assert compile_timer < stale_timer
    immediate = preview[preview.index("if (this._promptScenePayload())"):compile_timer]
    assert "_promptContextStaleVisualTimer = setTimeout" not in immediate
    assert "refreshDiagnostics" not in immediate
    assert "_refreshInlinePromptProjections" not in immediate
    # Every terminal window branch settles against both parallel caches.
    assert preview.count("_clearPromptStaleVisualTimerIfSettled(sceneId)") >= 2


def test_stale_visual_timer_arms_only_when_debounced_request_starts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for stale-paint lifecycle coverage")
    widget = _source("web/js/editor_widget.js")
    definition = widget[widget.index("\n    _previewPromptContextCandidate("):]
    method = _method_body(definition, "_previewPromptContextCandidate",
                          marker="Math.max(0, Number(delay) || 0)")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
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
    this.counts = { diagnostics: 0, projections: 0, inline: 0, timeline: 0 };
    this._promptPanelHandle = {
      // Production `renderDiagnostics` owns the panel projection fan-out.
      refreshDiagnostics: () => {
        this.counts.diagnostics++; this.counts.projections++;
      },
      refreshProjections: () => this.counts.projections++,
    };
    this._refreshInlinePromptProjections = () => this.counts.inline++;
  }
  _renderTimeline() { this.counts.timeline++; }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return { contextStart: 0, contextEnd: 100 }; }
  _promptScenePayload() { return this._promptContextScenePayloadCache; }
  _previewPromptContextScenePayload() {}
  _promptCompileRequestBody() { return {}; }
  _requestPromptContextCompile() { return new Promise(() => {}); }
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
const beforeRequest = {
  activeCompileTimers: timers.filter((timer) => timer.ms === 1000 && !timer.cancelled).length,
  activeStaleTimers: timers.filter((timer) =>
    timer.ms === PROMPT_STALE_VISUAL_DELAY_MS && !timer.cancelled).length,
};
const compileTimer = timers.find((timer) => timer.ms === 1000 && !timer.cancelled);
compileTimer.fn();
await Promise.resolve();
const staleTimer = timers.find((timer) =>
  timer.ms === PROMPT_STALE_VISUAL_DELAY_MS && !timer.cancelled);
const afterRequest = {
  activeStaleTimers: timers.filter((timer) =>
    timer.ms === PROMPT_STALE_VISUAL_DELAY_MS && !timer.cancelled).length,
};
staleTimer.fn();
const painted = {
  windowVisual: subject._promptContextCandidateCache._stale_visual,
  sceneVisual: subject._promptContextScenePayloadCache._stale_visual,
  counts: { ...subject.counts },
};
console.log(JSON.stringify({ immediate, beforeRequest, afterRequest, painted }));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "immediate": {
            "windowStale": True, "windowVisual": False,
            "sceneStale": True, "sceneVisual": False,
            "sceneToken": 8,
            "counts": {"diagnostics": 0, "projections": 0, "inline": 0,
                       "timeline": 0},
        },
        "beforeRequest": {"activeCompileTimers": 1, "activeStaleTimers": 0},
        "afterRequest": {"activeStaleTimers": 1},
        "painted": {
            "windowVisual": True, "sceneVisual": True,
            "counts": {"diagnostics": 1, "projections": 1, "inline": 1,
                       "timeline": 1},
        },
    }


def test_continuous_burst_arms_no_grace_and_token_bump_cancels_one_in_flight():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for stale-paint cancellation coverage")
    widget = _source("web/js/editor_widget.js")
    definition = widget[widget.index("\n    _previewPromptContextCandidate("):]
    method = _method_body(definition, "_previewPromptContextCandidate",
                          marker="Math.max(0, Number(delay) || 0)")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const resolvePromptCandidateSelection = () => ({selectionStart: 0, selectionEnd: 100});
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = {fn, ms, cancelled: false}; timers.push(timer); return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
class Subject {
""" + method + """
  constructor() {
    this.activeSceneId = "scene"; this.activeScene = {duration_frames: 100};
    this.totalFrames = 100; this._promptContextPreviewToken = 0;
    this._promptContextScenePayloadToken = 0;
    this._promptContextCandidateCache = {
      _candidate_scene_id: "scene", _stale: false};
    this._promptContextScenePayloadCache = null;
    this.paints = 0;
    this._promptPanelHandle = {refreshDiagnostics: () => this.paints++};
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return {contextStart: 0, contextEnd: 100}; }
  _promptScenePayload() { return null; }
  _previewPromptContextScenePayload() {}
  _promptCompileRequestBody() { return {}; }
  _requestPromptContextCompile() { return new Promise(() => {}); }
  _renderTimeline() {}
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 180);
subject._previewPromptContextCandidate({}, 180);
const burst = {
  activeCompile: timers.filter((timer) => timer.ms === 180 && !timer.cancelled).length,
  activeGrace: timers.filter((timer) => timer.ms === 300 && !timer.cancelled).length,
  paints: subject.paints,
};
const compile = timers.find((timer) => timer.ms === 180 && !timer.cancelled);
compile.fn();
await Promise.resolve();
const grace = timers.find((timer) => timer.ms === 300 && !timer.cancelled);
subject.activeSceneId = "other";
subject._previewPromptContextCandidate({}, 180);
grace.fn();
const destroyed = new Subject();
const destroyStart = timers.length;
destroyed._previewPromptContextCandidate({}, 180);
const destroyCompile = timers.slice(destroyStart).find(
  (timer) => timer.ms === 180 && !timer.cancelled);
destroyCompile.fn();
await Promise.resolve();
const destroyGrace = timers.slice(destroyStart).find(
  (timer) => timer.ms === 300 && !timer.cancelled);
destroyed._destroyed = true;
destroyGrace.fn();
console.log(JSON.stringify({burst, graceCancelled: grace.cancelled,
  token: subject._promptContextPreviewToken, paints: subject.paints,
  destroyedPaints: destroyed.paints}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "burst": {"activeCompile": 1, "activeGrace": 0, "paints": 0},
        "graceCancelled": True, "token": 3, "paints": 0,
        "destroyedPaints": 0,
    }


def test_destroy_cancels_prompt_preview_grace_and_ownership_tokens():
    widget = _source("web/js/editor_widget.js")
    start = widget.index("\n    destroy() {")
    setup = widget[start:widget.index("        this._stopPlayback();", start)]
    assert setup.count("_promptContextPreviewToken") == 2
    assert setup.count("_promptContextScenePayloadToken") == 2
    assert "clearTimeout(this._promptContextPreviewTimer)" in setup
    assert "clearTimeout(this._promptContextStaleVisualTimer)" in setup
    assert "this._promptContextPreviewTimer = null" in setup
    assert "this._promptContextStaleVisualTimer = null" in setup


def test_compile_settling_inside_request_grace_never_paints_stale():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for request-grace lifecycle coverage")
    widget = _source("web/js/editor_widget.js")
    definition = widget[widget.index("\n    _previewPromptContextCandidate("):]
    method = _method_body(definition, "_previewPromptContextCandidate",
                          marker="Math.max(0, Number(delay) || 0)")
    settle_start = widget.index("\n    _clearPromptStaleVisualTimerIfSettled(")
    settle = _method_body(widget[settle_start:],
                          "_clearPromptStaleVisualTimerIfSettled",
                          marker="return true;")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const resolvePromptCandidateSelection = (_start, _end, duration) =>
  ({ selectionStart: 0, selectionEnd: duration });
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = { fn, ms, cancelled: false };
  timers.push(timer); return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
class Subject {
""" + settle + """
""" + method + """
  constructor() {
    this.activeSceneId = "scene";
    this.activeScene = { duration_frames: 100 };
    this.totalFrames = 100;
    this._promptContextPreviewToken = 0;
    this._promptContextCandidateCache = {
      _candidate_scene_id: "scene", prompt: "old", _stale: false };
    this._promptContextScenePayloadCache = null;
    this.counts = { diagnostics: 0, inline: 0, apply: 0, timeline: 0 };
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
  async _requestPromptContextCompile() {
    return {response: {ok: true}, payload: {prompt: "new"}};
  }
  _renderTimeline() { this.counts.timeline++; }
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 180);
const beforeRequest = timers.filter((timer) => timer.ms === 300 && !timer.cancelled).length;
const compileTimer = timers.find((timer) => timer.ms === 180 && !timer.cancelled);
await compileTimer.fn();
const staleTimers = timers.filter((timer) => timer.ms === 300);
console.log(JSON.stringify({
  beforeRequest,
  staleTimerCount: staleTimers.length,
  staleTimerCancelled: staleTimers[0]?.cancelled,
  cache: subject._promptContextCandidateCache,
  counts: subject.counts,
}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "beforeRequest": 0,
        "staleTimerCount": 1,
        "staleTimerCancelled": True,
        "cache": {"prompt": "new", "_candidate_scene_id": "scene",
                  "_stale": False, "_stale_visual": False, "_failed": False},
        "counts": {"diagnostics": 1, "inline": 1, "apply": 1,
                   "timeline": 1},
    }


def test_failed_candidate_latch_survives_edit_and_success_clears_it():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for failed-candidate lifecycle coverage")
    widget = _source("web/js/editor_widget.js")
    preview_start = widget.index("\n    _previewPromptContextCandidate(")
    preview = _method_body(widget[preview_start:], "_previewPromptContextCandidate",
                           marker="Math.max(0, Number(delay) || 0)")
    failed_start = widget.index("\n    _failedPromptContextCandidate(")
    failed = _method_body(widget[failed_start:], "_failedPromptContextCandidate")
    settle_start = widget.index("\n    _clearPromptStaleVisualTimerIfSettled(")
    settle = _method_body(widget[settle_start:],
                          "_clearPromptStaleVisualTimerIfSettled",
                          marker="return true;")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const resolvePromptCandidateSelection = () => ({selectionStart: 0, selectionEnd: 100});
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = {fn, ms, cancelled: false}; timers.push(timer); return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
class Subject {
""" + failed + """
""" + settle + """
""" + preview + """
  constructor() {
    this.activeSceneId = "scene"; this.activeScene = {duration_frames: 100};
    this.totalFrames = 100; this._promptContextPreviewToken = 0;
    this._promptContextScenePayloadToken = 0;
    this._promptContextCandidateCache = {_candidate_scene_id: "scene",
      prompt: "last good", setup_manifest: {slots: [1]},
      attachment_capability_projections: {a: [{state: "emitted"}]},
      _stale: false, _stale_visual: false};
    this._promptContextScenePayloadCache = null;
    this.results = [
      {response: {ok: false, status: 409},
       payload: {code: "project_version_conflict", error: "project_version_conflict"}},
      {response: {ok: true, status: 200}, payload: {prompt: "fresh"}},
    ];
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return {contextStart: 0, contextEnd: 100}; }
  _promptScenePayload() { return null; }
  _previewPromptContextScenePayload() {}
  _promptCompileRequestBody() { return {}; }
  async _requestPromptContextCompile() { return this.results.shift(); }
  _renderTimeline() {}
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 0);
await timers.find((timer) => timer.ms === 0 && !timer.cancelled).fn();
const failedState = structuredClone(subject._promptContextCandidateCache);
subject._previewPromptContextCandidate({}, 1000);
const superseded = structuredClone(subject._promptContextCandidateCache);
await timers.find((timer) => timer.ms === 1000 && !timer.cancelled).fn();
const success = structuredClone(subject._promptContextCandidateCache);
console.log(JSON.stringify({failedState, superseded, success}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    for state in (result["failedState"], result["superseded"]):
        assert state["_failed"] is True
        assert state["_stale"] is True
        assert state["_stale_visual"] is True
        assert state["prompt"] == "last good"
        assert state["setup_manifest"] == {"slots": [1]}
        assert state["attachment_capability_projections"] == {
            "a": [{"state": "emitted"}]}
        assert state["errors"][0]["code"] == "project_version_conflict"
        assert "project changed while it was compiling" in state["errors"][0]["message"]
        assert "Any previous preview remains visible" in state["errors"][0]["message"]
        assert state["errors"][0]["message"] != "project_version_conflict"
    assert result["success"] == {
        "prompt": "fresh", "_candidate_scene_id": "scene",
        "_stale": False, "_stale_visual": False, "_failed": False,
    }


def test_first_load_preview_conflict_does_not_claim_retained_compile():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for first-load preview-conflict coverage")
    widget = _source("web/js/editor_widget.js")
    preview = _method_body(
        widget[widget.index("\n    _previewPromptContextCandidate("):],
        "_previewPromptContextCandidate", marker="Math.max(0, Number(delay) || 0)")
    failed = _method_body(
        widget[widget.index("\n    _failedPromptContextCandidate("):],
        "_failedPromptContextCandidate")
    settle = _method_body(
        widget[widget.index("\n    _clearPromptStaleVisualTimerIfSettled("):],
        "_clearPromptStaleVisualTimerIfSettled", marker="return true;")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const resolvePromptCandidateSelection = () => ({selectionStart: 0, selectionEnd: 100});
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = {fn, ms, cancelled: false}; timers.push(timer); return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
class Subject {
""" + failed + """
""" + settle + """
""" + preview + """
  constructor() {
    this.activeSceneId = "scene"; this.activeScene = {duration_frames: 100};
    this.totalFrames = 100; this._promptContextPreviewToken = 0;
    this._promptContextCandidateCache = null;
    this._promptContextScenePayloadCache = null;
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return {contextStart: 0, contextEnd: 100}; }
  _promptScenePayload() { return null; }
  _previewPromptContextScenePayload() {}
  _promptCompileRequestBody() { return {}; }
  async _requestPromptContextCompile() {
    return {response: {ok: false, status: 409},
      payload: {code: "project_version_conflict", error: "project_version_conflict"}};
  }
  _renderTimeline() {}
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 0);
await timers.find((timer) => timer.ms === 0 && !timer.cancelled).fn();
const first = structuredClone(subject._promptContextCandidateCache);
subject._previewPromptContextCandidate({}, 0);
await timers.filter((timer) => timer.ms === 0 && !timer.cancelled).at(-1).fn();
const second = structuredClone(subject._promptContextCandidateCache);
console.log(JSON.stringify({first, second}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    for state in (result["first"], result["second"]):
        assert state["_failed"] is True
        message = state["errors"][0]["message"]
        assert "preview could not refresh" in message
        assert "project changed while it was compiling" in message
        assert "Any previous preview remains visible" in message
        assert "showing the last successful compile" not in message


def test_stale_grace_refreshes_only_consumers_of_flags_it_flips():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for stale-paint fan-out coverage")
    widget = _source("web/js/editor_widget.js")
    preview = _method_body(
        widget[widget.index("\n    _previewPromptContextCandidate("):],
        "_previewPromptContextCandidate", marker="Math.max(0, Number(delay) || 0)")
    script = """
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const resolvePromptCandidateSelection = () => ({selectionStart: 0, selectionEnd: 100});
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = {fn, ms, cancelled: false}; timers.push(timer); return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
class Subject {
""" + preview + """
  constructor(windowed, scene) {
    this.activeSceneId = "scene"; this.activeScene = {duration_frames: 100};
    this.totalFrames = 100; this._promptContextPreviewToken = 0;
    this._promptContextScenePayloadToken = 0;
    this._promptContextCandidateCache = windowed;
    this._promptContextScenePayloadCache = scene;
    this.counts = {diagnostics: 0, projections: 0, inline: 0, timeline: 0};
    this._promptPanelHandle = {
      // Production renderDiagnostics performs the panel-wide projection sweep.
      refreshDiagnostics: () => {
        this.counts.diagnostics++; this.counts.projections++;
      },
      refreshProjections: () => this.counts.projections++,
    };
    this._refreshInlinePromptProjections = () => this.counts.inline++;
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return {contextStart: 0, contextEnd: 100}; }
  _promptScenePayload() { return this._promptContextScenePayloadCache; }
  _previewPromptContextScenePayload() {}
  _promptCompileRequestBody() { return {}; }
  _requestPromptContextCompile() { return new Promise(() => {}); }
  _renderTimeline() { this.counts.timeline++; }
}
const fresh = () => ({_candidate_scene_id: "scene", _stale: false,
  _stale_visual: false});
const failed = () => ({_candidate_scene_id: "scene", _stale: true,
  _stale_visual: true, _failed: true});
const rows = {};
for (const [name, windowed, scene] of [
  ["windowOnly", fresh(), null],
  ["sceneOnly", null, fresh()],
  ["both", fresh(), fresh()],
  ["sceneWithFailedWindow", failed(), fresh()],
  ["alreadyVisual", failed(), null],
]) {
  const start = timers.length;
  const subject = new Subject(windowed, scene);
  subject._previewPromptContextCandidate({}, 0);
  timers.slice(start).find((timer) => timer.ms === 0 && !timer.cancelled).fn();
  await Promise.resolve();
  const grace = timers.slice(start).find((timer) => timer.ms === 300 && !timer.cancelled);
  grace.fn();
  rows[name] = {counts: subject.counts,
    windowVisual: subject._promptContextCandidateCache?._stale_visual,
    sceneVisual: subject._promptContextScenePayloadCache?._stale_visual};
}
console.log(JSON.stringify(rows));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "windowOnly": {
            "counts": {"diagnostics": 1, "projections": 1,
                       "inline": 1, "timeline": 1},
            "windowVisual": True,
        },
        "sceneOnly": {
            "counts": {"diagnostics": 0, "projections": 1,
                       "inline": 1, "timeline": 1},
            "sceneVisual": True,
        },
        "both": {
            "counts": {"diagnostics": 1, "projections": 1,
                       "inline": 1, "timeline": 1},
            "windowVisual": True, "sceneVisual": True,
        },
        "sceneWithFailedWindow": {
            "counts": {"diagnostics": 0, "projections": 1,
                       "inline": 1, "timeline": 1},
            "windowVisual": True, "sceneVisual": True,
        },
        "alreadyVisual": {
            "counts": {"diagnostics": 0, "projections": 0,
                       "inline": 0, "timeline": 0},
            "windowVisual": True,
        },
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
    script = _prompt_compile_test_support(widget) + """
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
class Subject extends CompileSupport {
""" + settle + """
""" + preview + """
  constructor() {
    super();
    this.activeSceneId = "scene";
    this.activeScene = { duration_frames: 100 };
    this.totalFrames = 100;
    this._promptContextCandidateCache = {
      _candidate_scene_id: "scene", _stale: false };
    this._promptContextScenePayloadCache = null;
    this.counts = { diagnostics: 0, inline: 0, apply: 0, timeline: 0 };
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
  _renderTimeline() { this.counts.timeline++; }
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 0);
const compile = timers.find((timer) => timer.ms === 0 && !timer.cancelled);
await compile.fn();
const staleTimer = timers.find((timer) => timer.ms === 300);
console.log(JSON.stringify({
  code: subject._promptContextCandidateCache.errors[0].code,
  stale: subject._promptContextCandidateCache._stale,
  failed: subject._promptContextCandidateCache._failed,
  staleTimerCancelled: staleTimer.cancelled,
  counts: subject.counts,
}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "code": "preview_invalid_response", "stale": True, "failed": True,
        "staleTimerCancelled": True,
        "counts": {"diagnostics": 1, "inline": 1, "apply": 1, "timeline": 1},
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
    script = _prompt_compile_test_support(widget) + """
const api = { apiURL: (value) => value };
globalThis.fetch = async () => ({ ok: true, status: 200,
  json: async () => null });
class Subject extends CompileSupport {
""" + settle + """
""" + scene_method + """
  constructor() {
    super();
    this.activeSceneId = "scene";
    this.totalFrames = 100;
    this._promptContextScenePayloadToken = 0;
    this._promptContextCandidateCache = {
      _candidate_scene_id: "scene", _stale: false };
    this._promptContextScenePayloadCache = {
      _candidate_scene_id: "scene", _stale: true, _stale_visual: false };
    this._promptContextStaleVisualTimer = setTimeout(() => {}, 10000);
    this.counts = { inline: 0, projections: 0, timeline: 0 };
    this._refreshInlinePromptProjections = () => this.counts.inline++;
    this._promptPanelHandle = {
      refreshProjections: () => this.counts.projections++,
    };
  }
  _promptCompileRequestBody() { return {}; }
  _promptProjectionSubset(value) { return value; }
  _renderTimeline() { this.counts.timeline++; }
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
        "counts": {"inline": 1, "projections": 1, "timeline": 1},
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


def test_prompt_panel_reuses_one_draft_overlay_per_global_and_section_change():
    panel = _source("web/js/editor_prompt_panel.js")
    global_start = panel.index("onChange: ({ document: nextDocument, attachments, reason }) => {",
                               panel.index("const draftGlobalSceneSnapshot"))
    global_change = panel[global_start:panel.index("onActivateAttachment:", global_start)]
    assert global_change.count("draftGlobalSceneSnapshot()") == 1
    assert "const snapshot = draftGlobalSceneSnapshot();" in global_change
    assert "keepGlobalDraft(snapshot);" in global_change

    section_start = panel.index("onChange: ({ document: nextDocument, attachments, reason }) => {",
                                panel.index("const draftSceneSnapshot"))
    section_change = panel[section_start:panel.index("onActivateAttachment:", section_start)]
    assert section_change.count("draftSceneSnapshot()") == 1
    assert "const snapshot = draftSceneSnapshot();" in section_change
    assert "keepSectionDraft(snapshot);" in section_change
    assert "prompt_sections: snapshot.prompt_sections" in section_change


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
            {"code": "quiet", "message": "Check this chip.",
             "attachment_id": "chip-a", "origin": "global",
             "semantic_unit_id": "unit-a"},
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
    assert projected["byAttachment"]["chip-a"][1]["origin"] == "global"
    assert projected["byAttachment"]["chip-a"][1]["semantic_unit_id"] == "unit-a"
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


def test_draft_overlays_clone_only_overwritten_keys_and_keep_inputs_isolated():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt draft overlay clone coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    result = _run_node(f"""
const mod = await import({json.dumps(module_url)});
const scene = {{metadata: {{owner: "scene"}},
  global_attachments: [{{attachment_id: "global-old"}}],
  global_channel_docs: {{visual: {{nodes: []}}}},
  global_channels: {{visual: "old"}},
  prompt_sections: [
    {{prompt_id: "one", start_frame: 0, end_frame: 20,
      untouched: {{owner: "section"}}, attachments: [{{attachment_id: "old"}}],
      channel_docs: {{visual: {{nodes: []}}}}, channels: {{visual: "old"}}}},
    {{prompt_id: "two", start_frame: 20, end_frame: 40,
      untouched: {{owner: "other"}}}},
  ]}};
const sectionAttachments = [{{attachment_id: "new"}}];
const sectionDocs = {{visual: {{nodes: [{{type: "text", text: "draft"}}]}}}};
const sectionChannels = {{visual: "draft"}};
const section = mod.sceneWithDraftSection(scene, {{index: 0, promptId: "one",
  attachments: sectionAttachments, channelDocs: sectionDocs, channels: sectionChannels}});
const globalAttachments = [{{attachment_id: "global-new"}}];
const globalDocs = {{visual: {{nodes: [{{type: "text", text: "global"}}]}}}};
const globalChannels = {{visual: "global"}};
const global = mod.sceneWithDraftGlobal(scene, {{attachments: globalAttachments,
  channelDocs: globalDocs, channels: globalChannels}});
const identity = {{
  sectionMetadataShared: section.metadata === scene.metadata,
  sectionArrayCloned: section.prompt_sections !== scene.prompt_sections,
  selectedSectionCloned: section.prompt_sections[0] !== scene.prompt_sections[0],
  untouchedSectionShared: section.prompt_sections[1] === scene.prompt_sections[1],
  untouchedSelectedKeyShared:
    section.prompt_sections[0].untouched === scene.prompt_sections[0].untouched,
  sectionAttachmentsCloned: section.prompt_sections[0].attachments !== sectionAttachments,
  sectionDocsCloned: section.prompt_sections[0].channel_docs !== sectionDocs,
  sectionChannelsCloned: section.prompt_sections[0].channels !== sectionChannels,
  globalMetadataShared: global.metadata === scene.metadata,
  globalSectionsShared: global.prompt_sections === scene.prompt_sections,
  globalAttachmentsCloned: global.global_attachments !== globalAttachments,
  globalDocsCloned: global.global_channel_docs !== globalDocs,
  globalChannelsCloned: global.global_channels !== globalChannels,
}};
section.prompt_sections[0].attachments[0].attachment_id = "mutated";
section.prompt_sections[0].channel_docs.visual.nodes[0].text = "mutated";
section.prompt_sections[0].channels.visual = "mutated";
global.global_attachments[0].attachment_id = "mutated";
global.global_channel_docs.visual.nodes[0].text = "mutated";
global.global_channels.visual = "mutated";
console.log(JSON.stringify({{identity, inputs: {{sectionAttachments, sectionDocs,
  sectionChannels, globalAttachments, globalDocs, globalChannels}}, scene}}));
""")
    assert all(result["identity"].values())
    assert result["inputs"]["sectionAttachments"][0]["attachment_id"] == "new"
    assert result["inputs"]["sectionDocs"]["visual"]["nodes"][0]["text"] == "draft"
    assert result["inputs"]["sectionChannels"]["visual"] == "draft"
    assert result["inputs"]["globalAttachments"][0]["attachment_id"] == "global-new"
    assert result["inputs"]["globalDocs"]["visual"]["nodes"][0]["text"] == "global"
    assert result["inputs"]["globalChannels"]["visual"] == "global"
    assert result["scene"]["prompt_sections"][0]["attachments"][0]["attachment_id"] == "old"
    assert result["scene"]["global_attachments"][0]["attachment_id"] == "global-old"


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
    assert "Audio target speaker" not in chips
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
    return structuredClone(this.activeScene);
  }}
  async _mutateReferences() {{}}
}}
const h=new Harness();
const before={{semantic_unit_id:"u"}};
const after={{semantic_unit_id:"u",handle:"KoreanWoman"}};
const entry=h._pushUndo("attach prompt Reference",{{
  promptIdentityChange:{{type:"upsert",value:before,expected:after}},
  inversePromptIdentityChange:{{type:"upsert",value:after,expected:before}},
}});
h.activeScene={{attachments:[{{attachment_id:"chip"}}]}};
entry.postSnapshot=structuredClone(h.activeScene);
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


def test_scene_history_post_snapshot_is_stamped_only_by_claimed_mutation_payload():
    widget = _source("web/js/editor_widget.js")
    push = _method(widget, "_pushUndo", "_commitUndoEntry")
    result = _run_node(f"""
class Harness {{
{push}
  constructor() {{
    this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",value:"before"}};
    this._undoStack=[];this._redoStack=[];this._maxUndoSteps=20;
  }}
  _trimUndoStack(){{}}
}}
const h=new Harness();
const entry=h._pushUndo("edit");
h.activeScene.value="optimistic-local";
const beforeStamp={{hasPost:Object.hasOwn(entry,"postSnapshot"),
  snapshot:entry.snapshot.value}};
const claimed=h._claimHistoryPostSnapshotCapture("scene");
h._stampHistoryPostSnapshot(claimed,{{scene_id:"scene",value:"server-after"}});
const afterStamp={{post:entry.postSnapshot.value,active:h.activeScene.value}};
h._stampHistoryPostSnapshot(entry,{{scene_id:"scene",value:"later-server"}});
console.log(JSON.stringify({{beforeStamp,afterStamp,stable:entry.postSnapshot.value}}));
""")
    assert result == {
        "beforeStamp": {"hasPost": False, "snapshot": "before"},
        "afterStamp": {"post": "server-after", "active": "optimistic-local"},
        "stable": "server-after",
    }
    fetch_scenes = _method(widget, "_fetchScenes", "_createScene")
    reconcile = _method(widget, "_reconcileActiveSceneFromMutation", "_discardLastUndo")
    assert "_stampHistoryPostSnapshot" not in fetch_scenes
    assert "_stampHistoryPostSnapshot" not in reconcile


def test_fps_change_is_not_recorded_until_scene_history_is_timebase_aware():
    widget = _source("web/js/editor_widget.js")
    update_fps = _method(widget, "_updateSceneFps", "_cycleScene")
    assert "_pushUndo" not in update_fps
    assert "_discardLastUndo" not in update_fps


def test_coalesced_scene_mutation_stamps_only_its_latest_correlated_undo_entry():
    widget = _source("web/js/editor_widget.js")
    queue_method = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    history_methods = _method(widget, "_pushUndo", "_commitUndoEntry")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
class Harness {{
{queue_method}
{history_methods}
  constructor(){{this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",value:0}};
    this._undoStack=[];this._redoStack=[];this._maxUndoSteps=20;
    this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();}}
  _trimUndoStack(){{}}
  _discardUndoEntry(entry){{const index=this._undoStack.indexOf(entry);
    if(index<0)return false;this._undoStack.splice(index,1);return true;}}
}}
const h=new Harness();
const first=h._pushUndo("first");
const firstPromise=h._queueProjectMutation({{key:"scene:value",label:"first",
  refreshScenes:false,intent:{{sceneId:"scene",value:1}},
  run:async(intent)=>({{payload:{{scene:{{scene_id:"scene",value:intent.value}}}}}})}});
h.activeScene.value=1;
const second=h._pushUndo("second");
const secondPromise=h._queueProjectMutation({{key:"scene:value",label:"second",
  refreshScenes:false,intent:{{sceneId:"scene",value:2}},
  run:async(intent)=>({{payload:{{scene:{{scene_id:"scene",value:intent.value}}}}}})}});
await Promise.all([firstPromise,secondPromise]);
const orphan=h._pushUndo("dedicated route");
await Promise.resolve();
await h._queueProjectMutation({{key:"scene:later",label:"later",
  refreshScenes:false,intent:{{sceneId:"scene",value:3}},coalesce:false,
  run:async(intent)=>({{payload:{{scene:{{scene_id:"scene",value:intent.value}}}}}})}});
console.log(JSON.stringify({{
  firstClaimed:first._postSnapshotCaptureClaimed===true,
  firstStamped:Object.hasOwn(first,"postSnapshot"),
  firstPresent:h._undoStack.includes(first),
  secondPost:second.postSnapshot,
  orphanStamped:Object.hasOwn(orphan,"postSnapshot"),
}}));
""")
    assert result == {
        "firstClaimed": True,
        "firstStamped": False,
        "firstPresent": False,
        "secondPost": {"scene_id": "scene", "value": 2},
        "orphanStamped": False,
    }


def test_unstamped_scene_history_is_removed_before_network_access():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.sessionDiagRecord=()=>{{}};
let fetches=0;globalThis.fetch=async()=>{{fetches+=1;}};
const warnings=[];globalThis.notifyWarning=(message)=>warnings.push(message);
globalThis.notifyInfo=()=>{{}};
class Harness {{
{undo}
  constructor() {{this.activeSceneId="scene";this.activeScene={{value:"after"}};
    this._undoStack=[{{sceneId:"scene",snapshot:{{value:"before"}},label:"edit"}}];
    this._redoStack=[];this._editorFocused=false;}}
  _keyboardDebug(){{}}
}}
const h=new Harness();await h._undo();
console.log(JSON.stringify({{fetches,warnings,undo:h._undoStack.length,
  redo:h._redoStack.length}}));
""")
    assert result["fetches"] == 0
    assert result["undo"] == 0
    assert result["redo"] == 0
    assert "unusable entry was removed" in result["warnings"][0]


def test_redo_builds_inverse_from_restore_response_not_active_scene():
    widget = _source("web/js/editor_widget.js")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyWarning=()=>{{}};globalThis.notifyInfo=()=>{{}};
class Harness {{
{redo}
  constructor() {{
    this.activeSceneId="scene";this.activeScene={{scene_id:"scene",items:["stale"]}};
    this._undoStack=[];this._redoStack=[{{sceneId:"scene",label:"edit",
      snapshot:{{scene_id:"scene",items:["owned"]}},
      postSnapshot:{{scene_id:"scene",items:["concurrent"]}}}}];
    this._editorFocused=false;this._historyOperationInFlight=false;this.calls=[];
  }}
  _keyboardDebug(){{}}
  _finishHistoryOperation(){{this._historyOperationInFlight=false;}}
  async _restoreScene(sceneId,target,base){{
    this.calls.push({{sceneId,target,base}});
    this.activeScene={{scene_id:"scene",items:["owned","concurrent"]}};
    return structuredClone(this.activeScene);
  }}
  async _applyReferenceHistoryOperations(){{}}
  async _applyPromptIdentityChange(){{}}
}}
const h=new Harness();await h._redo();
console.log(JSON.stringify({{calls:h.calls,undo:h._undoStack,active:h.activeScene}}));
""")
    assert result["calls"][0]["target"]["items"] == ["owned"]
    assert result["calls"][0]["base"]["items"] == ["concurrent"]
    assert result["undo"][0]["snapshot"]["items"] == ["concurrent"]
    assert result["undo"][0]["postSnapshot"]["items"] == [
        "owned", "concurrent"]


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
globalThis.notifyWarning=(message)=>notices.push(message);
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{pending_helper}
{push}
{commit_entry}
{undo}
{redo}
  constructor() {{ this.activeSceneId="scene"; this.activeScene={{scene_id:"scene",value:"after"}};
    this._undoStack=[]; this._redoStack=[{{label:"older redo"}}]; this._maxUndoSteps=20;
    this._editorFocused=false; this.restores=0; }}
  _keyboardDebug() {{}}
  async _restoreScene(_id,snapshot) {{ this.restores += 1;
    this.activeScene=snapshot; return structuredClone(snapshot); }}
  async _mutateReferences() {{}}
  async _applyPromptIdentityChange() {{}}
}}
const h=new Harness();
const entry=h._pushUndo("attach",{{pending:true}});
entry.postSnapshot=structuredClone(h.activeScene);
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
        f"{operation} is waiting on an unresolved Prompt Apply. "
        "Reopen Apply and save or discard that draft first."
        for operation in ("Undo", "Redo", "Undo", "Redo")
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
      postSnapshot:{{scene_id:"scene",value:"applied"}},
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
    return structuredClone(snapshot);
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
  postSnapshot:h._redoStack[0].postSnapshot.value,
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
        "undo": 0, "redo": 1, "postSnapshot": "old",
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
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    discard = _method(widget, "_discardUndoEntry", "_trimLocalLaneConfigs")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{promptIdentityCleanupPlan,reconcilePromptIdentityCreateOutcome}}=
  await import({json.dumps(transactions_url)});
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
class Harness {{
{methods}
{queue_mutation}
{discard}
  constructor() {{
    this.refreshWorks=false; this.commits=0;
    this._projectMutationQueue=new ProjectMutationQueue();
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
  _deferProjectBackedRefresh(){{}}
  _commitUndoEntry(entry) {{ if(!this._undoStack.includes(entry))return false;
    entry.pending=false; this.commits += 1; return true; }}
}}
const h=new Harness();
const intent={{type:"create_prompt_semantic_unit",handle_suggestion:"One",
  unit:{{semantic_unit_id:"one",name:"One",kind:"subject",definition:""}}}};
const expected=structuredClone(h.activeScene.prompt_sections);
const entry={{pending:true,promptIdentityCreateIntents:[intent],
  promptIdentityExpectedSections:expected}};
h._undoStack=[entry];
try{{await h._queueProjectMutation({{key:"apply",historyEntry:entry,
  historyFailureOwnedByCaller:true,run:async()=>{{throw new Error("lost response");}}}});}}catch{{}}
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
    apply_setup = _method(widget, "_applyPromptSetup", "_getPromptTemplates")
    assert "historyFailureOwnedByCaller: identityCreateIntents.length > 0" in apply_setup
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


def test_three_rapid_undos_reserve_distinct_entries_and_run_serially():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const notices=[]; globalThis.notifyInfo=(message)=>notices.push(message);
globalThis.notifyWarning=(message)=>notices.push(message);
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{undo}
{redo}
{helpers}
  constructor() {{ this.activeSceneId="scene"; this.activeScene={{value:2}};
    this._undoStack=[
      {{sceneId:"scene",snapshot:{{value:0}},postSnapshot:{{value:1}},label:"first"}},
      {{sceneId:"scene",snapshot:{{value:1}},postSnapshot:{{value:2}},label:"second"}},
      {{sceneId:"scene",snapshot:{{value:2}},postSnapshot:{{value:3}},label:"third"}},
    ]; this._redoStack=[]; this._editorFocused=false; this.restores=[];this.releases=[];
    this._historyStackRevision=0;this._historyOperationSeq=0;
    this._queuedHistoryOperationCount=0;this._queuedHistoryNotification=null;
    this._sceneMutationInvalidationSeq=0;this._projectMutationQueue=new ProjectMutationQueue(); }}
  _keyboardDebug() {{}}
  async _restoreScene(_sceneId,target) {{ this.restores.push(target.value);
    await new Promise((resolve)=>this.releases.push(resolve)); return target; }}
  async _applyPromptIdentityChange() {{}}
  async _applyReferenceHistoryOperations() {{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();
const promises=[h._undo(),h._undo(),h._undo()];await Promise.resolve();await Promise.resolve();
const reserved=h._undoStack.map((entry)=>({{label:entry.label,claimed:Boolean(entry.claimedBy)}}));
const starts=[];for(let index=0;index<3;index+=1){{
  while(h.releases.length===0)await new Promise((resolve)=>setImmediate(resolve));
  starts.push([...h.restores]);h.releases.shift()();
}}
await Promise.all(promises);
console.log(JSON.stringify({{reserved,starts,after:{{undo:h._undoStack.length,
  redo:h._redoStack.length,restores:h.restores,inFlight:h._historyOperationInFlight}},notices}}));
""")
    assert result["reserved"] == [
        {"label": "first", "claimed": True},
        {"label": "second", "claimed": True},
        {"label": "third", "claimed": True},
    ]
    assert result["starts"] == [[2], [2, 1], [2, 1, 0]]
    assert result["after"] == {
        "undo": 0, "redo": 3, "restores": [2, 1, 0], "inFlight": False}
    assert result["notices"] == []


def test_queued_history_alternates_directions_without_dropping_or_misrouting_actions():
    widget = _source("web/js/editor_widget.js")
    reservations = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
const notices=[];globalThis.notifyInfo=(message)=>notices.push(message);
globalThis.notifyWarning=(message)=>notices.push(message);
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{reservations}
{undo}
{redo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:2}};
    this._undoStack=[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:1}},
      postSnapshot:{{scene_id:"scene",value:2}},label:"edit"}}];this._redoStack=[];
    this._maxUndoSteps=50;this._editorFocused=false;this._historyStackRevision=0;
    this._historyOperationSeq=0;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();this.restores=[];}}
  _keyboardDebug(){{}}
  async _restoreScene(_sceneId,target){{this.restores.push(target.value);
    this.activeScene=structuredClone(target);return structuredClone(target);}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();let release;
const blocker=h._projectMutationQueue.enqueue({{key:"busy",coalesce:false,
  run:async()=>await new Promise((resolve)=>release=resolve)}});
while(!release)await new Promise((resolve)=>setImmediate(resolve));
const actions=[h._undo(),h._redo(),h._undo()];await Promise.resolve();
const during={{undo:h._undoStack.map((entry)=>({{future:!!entry._historyFuture,
  claimed:!!entry.claimedBy}})),redo:h._redoStack.map((entry)=>({{
  future:!!entry._historyFuture,claimed:!!entry.claimedBy}})),
  queued:h._queuedHistoryOperationCount}};
release();await blocker;await Promise.all(actions);
const all=[...h._undoStack,...h._redoStack];
console.log(JSON.stringify({{during,restores:h.restores,undo:h._undoStack.length,
  redo:h._redoStack.length,futures:all.filter((entry)=>entry._historyFuture).length,
  claims:all.filter((entry)=>entry.claimedBy).length,notices}}));
""")
    assert result["during"] == {
        "undo": [{"future": False, "claimed": True},
                 {"future": True, "claimed": True}],
        "redo": [{"future": True, "claimed": True},
                 {"future": True, "claimed": False}],
        "queued": 3,
    }
    assert result["restores"] == [1, 2, 1]
    assert result["undo"] == 0
    assert result["redo"] == 1
    assert result["futures"] == 0
    assert result["claims"] == 0
    assert result["notices"] == []


def test_new_edit_during_undo_restore_prevents_invalid_redo_materialization():
    widget = _source("web/js/editor_widget.js")
    reservations = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    clear_redo = _method(widget, "_clearRedoForNewEdit", "_recordHistoryRefusal")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{reservations}
{undo}
{clear_redo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:2}};
    this._undoStack=[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:1}},
      postSnapshot:{{scene_id:"scene",value:2}},label:"edit"}}];this._redoStack=[];
    this._maxUndoSteps=50;this._editorFocused=false;this._historyStackRevision=0;
    this._historyOperationSeq=0;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();}}
  _keyboardDebug(){{}}
  async _restoreScene(_sceneId,target){{this.restoreStarted=true;
    await new Promise((resolve)=>this.releaseRestore=resolve);return target;}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();const call=h._undo();
while(!h.restoreStarted)await new Promise((resolve)=>setImmediate(resolve));
const reservedRedo=h._redoStack[0];h._historyStackRevision+=1;h._clearRedoForNewEdit();
const afterEdit={{redo:h._redoStack.length,reservationStillPresent:h._redoStack.includes(reservedRedo)}};
h.releaseRestore();await call;
console.log(JSON.stringify({{afterEdit,undo:h._undoStack.length,redo:h._redoStack.length,
  reservationFuture:!!reservedRedo._historyFuture}}));
""")
    assert result == {
        "afterEdit": {"redo": 0, "reservationStillPresent": False},
        "undo": 0,
        "redo": 0,
        "reservationFuture": True,
    }


def test_alternating_history_queue_does_not_discard_viable_actions_at_undo_depth_limit():
    widget = _source("web/js/editor_widget.js")
    reservations = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
const warnings=[];globalThis.notifyInfo=()=>{{}};
globalThis.notifyWarning=(message)=>warnings.push(message);
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{reservations}
{undo}
{redo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:2}};
    this._undoStack=[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:1}},
      postSnapshot:{{scene_id:"scene",value:2}},label:"edit"}}];this._redoStack=[];
    this._maxUndoSteps=4;this._editorFocused=false;this._historyStackRevision=0;
    this._historyOperationSeq=0;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();}}
  _keyboardDebug(){{}}
  _finishHistoryOperation(){{}}
  async _restoreScene(_sceneId,target){{return target;}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();let release;
const blocker=h._projectMutationQueue.enqueue({{key:"busy",coalesce:false,
  run:async()=>await new Promise((resolve)=>release=resolve)}});
while(!release)await new Promise((resolve)=>setImmediate(resolve));
    const actions=[];for(let index=0;index<55;index+=1){{
      actions.push(index%2===0?h._undo():h._redo());
}}
await Promise.resolve();const during={{count:h._queuedHistoryOperationCount,
  pending:h._projectMutationQueue._pending.length}};
release();await blocker;const outcomes=await Promise.all(actions);
console.log(JSON.stringify({{during,outcomes,warnings,undo:h._undoStack.length,
  redo:h._redoStack.length}}));
""")
    assert result["during"] == {"count": 55, "pending": 55}
    assert result["outcomes"] == [None] * 55
    assert result["warnings"] == []
    assert result["undo"] == 0
    assert result["redo"] == 1


def test_trim_preserves_claimed_history_entry_at_depth_limit():
    widget = _source("web/js/editor_widget.js")
    trim = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    result = _run_node(f"""
class Harness {{
{trim}
  constructor(){{this._maxUndoSteps=49;
    this._undoStack=Array.from({{length:51}},(_,id)=>({{id}}));
    this._undoStack[0].claimedBy={{operation:"undo",id:1}};}}
}}
const h=new Harness();const claimed=h._undoStack[0];h._trimUndoStack();
console.log(JSON.stringify({{depth:h._undoStack.length,claimedRetained:h._undoStack[0]===claimed,
  ids:h._undoStack.map((entry)=>entry.id)}}));
""")
    assert result["depth"] == 50
    assert result["claimedRetained"] is True
    assert result["ids"][0] == 0
    assert 1 not in result["ids"]


def test_redo_materialization_reapplies_undo_depth_limit_after_future_reservations():
    widget = _source("web/js/editor_widget.js")
    reservations = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{reservations}
{redo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:0}};
    this._undoStack=[];this._redoStack=[
      {{sceneId:"scene",snapshot:{{scene_id:"scene",value:1}},
        postSnapshot:{{scene_id:"scene",value:0}},label:"one"}},
      {{sceneId:"scene",snapshot:{{scene_id:"scene",value:2}},
        postSnapshot:{{scene_id:"scene",value:1}},label:"two"}}];
    this._maxUndoSteps=2;this._editorFocused=false;this._historyStackRevision=0;
    this._historyOperationSeq=0;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();}}
  _keyboardDebug(){{}}
  _finishHistoryOperation(){{}}
  async _restoreScene(_sceneId,target){{return target;}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();let release;
const blocker=h._projectMutationQueue.enqueue({{key:"busy",coalesce:false,
  run:async()=>await new Promise((resolve)=>release=resolve)}});
while(!release)await new Promise((resolve)=>setImmediate(resolve));
const actions=[h._redo(),h._redo()];await Promise.resolve();
h._undoStack.push({{label:"later-a"}},{{label:"later-b"}});h._trimUndoStack();
const during={{depth:h._undoStack.length,futures:h._undoStack.filter(
  (entry)=>entry._historyFuture).length}};
release();await blocker;await Promise.all(actions);
console.log(JSON.stringify({{during,depth:h._undoStack.length,
  futures:h._undoStack.filter((entry)=>entry._historyFuture).length,
  labels:h._undoStack.map((entry)=>entry.label)}}));
""")
    assert result["during"] == {"depth": 4, "futures": 2}
    assert result["depth"] == 2
    assert result["futures"] == 0
    assert result["labels"] == ["later-a", "later-b"]


def test_failed_downstream_undo_retrims_materialized_future_entry():
    widget = _source("web/js/editor_widget.js")
    reservations = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{reservations}
{undo}
{redo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:0}};
    this._undoStack=[];this._redoStack=[{{sceneId:"scene",
      snapshot:{{scene_id:"scene",value:1}},postSnapshot:{{scene_id:"scene",value:0}},
      label:"redo-source"}}];this._maxUndoSteps=2;this._editorFocused=false;
    this._historyStackRevision=0;this._historyOperationSeq=0;
    this._queuedHistoryOperationCount=0;this._queuedHistoryNotification=null;
    this._sceneMutationInvalidationSeq=0;this._projectMutationQueue=new ProjectMutationQueue();
    this.restoreCalls=0;}}
  _keyboardDebug(){{}}
  async _restoreScene(_sceneId,target){{this.restoreCalls+=1;
    if(this.restoreCalls===2)throw new Error("downstream restore refused");return target;}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();let release;
const blocker=h._projectMutationQueue.enqueue({{key:"busy",coalesce:false,
  run:async()=>await new Promise((resolve)=>release=resolve)}});
while(!release)await new Promise((resolve)=>setImmediate(resolve));
const redoCall=h._redo();const undoCall=h._undo();await Promise.resolve();
h._undoStack.push({{label:"later-a"}},{{label:"later-b"}});h._trimUndoStack();
const during={{depth:h._undoStack.length,claimed:!!h._undoStack[0].claimedBy}};
release();await blocker;await Promise.all([redoCall,undoCall]);
console.log(JSON.stringify({{during,depth:h._undoStack.length,
  labels:h._undoStack.map((entry)=>entry.label),futures:[...h._undoStack,...h._redoStack]
    .filter((entry)=>entry._historyFuture).length,restoreCalls:h.restoreCalls}}));
""")
    assert result["during"] == {"depth": 3, "claimed": True}
    assert result["depth"] == 2
    assert result["labels"] == ["later-a", "later-b"]
    assert result["futures"] == 0
    assert result["restoreCalls"] == 2


def test_failed_queued_history_clears_claim_and_preserves_stack_bytes():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{undo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:"after"}};
    this._undoStack=[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:"before"}},
      postSnapshot:{{scene_id:"scene",value:"after"}},label:"failing edit",
      metadata:{{nested:[1,2,3]}}}}];this._redoStack=[];this._editorFocused=false;
    this._historyStackRevision=0;this._historyOperationSeq=0;
    this._queuedHistoryOperationCount=0;this._queuedHistoryNotification=null;
    this._sceneMutationInvalidationSeq=0;this._projectMutationQueue=new ProjectMutationQueue();}}
  _keyboardDebug(){{}}
  async _restoreScene(){{throw new Error("restore refused");}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();const before=JSON.stringify(h._undoStack);await h._undo();
console.log(JSON.stringify({{before,after:JSON.stringify(h._undoStack),
  claim:Object.hasOwn(h._undoStack[0],"claimedBy"),redo:h._redoStack.length,
  idle:!h._projectMutationQueue.isBusy()}}));
""")
    assert result["after"] == result["before"]
    assert result["claim"] is False
    assert result["redo"] == 0
    assert result["idle"] is True


def test_history_catch_compensation_uses_owner_token_without_deadlock():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{undo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene"}};
    this._undoStack=[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:"before"}},
      postSnapshot:{{scene_id:"scene",value:"after"}},label:"composite",
      referenceOperations:[{{type:"forward"}}],
      inverseReferenceOperations:[{{type:"inverse"}}]}}];this._redoStack=[];
    this._editorFocused=false;this._historyStackRevision=0;this._historyOperationSeq=0;
    this._queuedHistoryOperationCount=0;this._queuedHistoryNotification=null;
    this._sceneMutationInvalidationSeq=0;this._projectMutationQueue=new ProjectMutationQueue();
    this.calls=[];this.nestedId=0;}}
  _keyboardDebug(){{}}
  async _applyReferenceHistoryOperations(operations,_label,_diagnostics,ownerToken){{
    const type=operations[0].type;
    return this._projectMutationQueue.enqueue({{key:`nested:${{++this.nestedId}}`,
      coalesce:false,ownerToken,run:async()=>this.calls.push(type)}});
  }}
  async _applyPromptIdentityChange(){{}}
  async _restoreScene(){{throw new Error("force compensation");}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();const completed=await Promise.race([
  h._undo().then(()=>true),
  new Promise((resolve)=>setTimeout(()=>resolve(false),150)),
]);
console.log(JSON.stringify({{completed,calls:h.calls,idle:!h._projectMutationQueue.isBusy(),
  undo:h._undoStack.length,claim:Object.hasOwn(h._undoStack[0],"claimedBy")}}));
""")
    assert result == {"completed": True, "calls": ["forward", "inverse"],
                      "idle": True, "undo": 1, "claim": False}


def test_undo_and_redo_wait_behind_busy_mutation_queue_and_then_apply():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const notices=[]; globalThis.notifyInfo=(message)=>notices.push(message);
globalThis.notifyWarning=(message)=>notices.push(message);
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{undo}
{redo}
{helpers}
  constructor(operation) {{ this.activeSceneId="scene"; this.activeScene={{scene_id:"scene",value:2}};
    this._undoStack=operation==="undo"
      ?[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:1}},postSnapshot:{{scene_id:"scene",value:2}},label:"edit"}}]:[];
    this._redoStack=operation==="redo"
      ?[{{sceneId:"scene",snapshot:{{scene_id:"scene",value:3}},postSnapshot:{{scene_id:"scene",value:2}},label:"edit"}}]:[];
    this._editorFocused=false;this.restores=[];this._historyStackRevision=0;
    this._historyOperationSeq=0;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();this.suppressions=[]; }}
  _keyboardDebug() {{}}
  _hasPendingProjectMutations() {{return this._projectMutationQueue.isBusy();}}
  async _restoreScene(_sceneId,target) {{this.restores.push(target.value);return target;}}
  async _applyPromptIdentityChange() {{}}
  async _applyReferenceHistoryOperations() {{}}
  _activateGraphUndoSuppression(reason){{this.suppressions.push(reason);}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const results=[];
for (const operation of ["undo","redo"]) {{
  const h=new Harness(operation);let release;
  const blocker=h._projectMutationQueue.enqueue({{key:"scene:scene:busy",coalesce:false,
    run:async()=>{{await new Promise((resolve)=>release=resolve);return "write";}}}});
  while(!release)await new Promise((resolve)=>setImmediate(resolve));
  const call=operation === "undo" ? h._undo() : h._redo();await Promise.resolve();
  const source=operation === "undo" ? h._undoStack : h._redoStack;
  const during={{claimed:Boolean(source[0]?.claimedBy),restores:[...h.restores],
    suppressions:[...h.suppressions],
    queued:h._projectMutationQueue.hasPending()}};
  release();await blocker;await call;
  results.push({{operation,during,restores:h.restores,suppressions:h.suppressions,
    undo:h._undoStack.length,redo:h._redoStack.length}});
}}
console.log(JSON.stringify({{results,notices}}));
""")
    assert result["results"] == [
        {"operation": "undo", "during": {"claimed": True, "restores": [],
         "suppressions": [], "queued": True}, "restores": [1],
         "suppressions": ["editor-undo-apply"], "undo": 0, "redo": 1},
        {"operation": "redo", "during": {"claimed": True, "restores": [],
         "suppressions": [], "queued": True}, "restores": [3],
         "suppressions": ["editor-redo-apply"], "undo": 1, "redo": 0},
    ]
    assert result["notices"] == []


def test_history_claim_and_apply_refusals_have_parity_and_runtime_diagnostics():
    widget = _source("web/js/editor_widget.js")
    reservations = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    clear_redo = _method(widget, "_clearRedoForNewEdit", "_recordHistoryRefusal")
    refusal = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
const records=[];globalThis.sessionDiagRecord=(kind,payload)=>{{
  if(kind==="history_operation_refused")records.push(payload);
}};
class Harness {{
{reservations}
{undo}
{redo}
{clear_redo}
{refusal}
  constructor(operation,gate) {{this.activeSceneId="scene-a";this.gate=gate;
    this.activeScene={{scene_id:"scene-a"}};this._project="project-a";
    const entry={{sceneId:"scene-a",snapshot:{{scene_id:"scene-a"}},
      postSnapshot:{{scene_id:"scene-a"}},label:`${{operation}}-${{gate}}`}};
    this._undoStack=operation==="undo"?[entry]:[];
    this._redoStack=operation==="redo"?[entry]:[];
    this._editorFocused=false;this._historyStackRevision=0;
    this._historyCommitRevisionByEntry=new WeakMap();this._historyOperationSeq=0;
    this._queuedHistoryOperationCount=0;this._queuedHistoryNotification=null;
    this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=gate==="queue_failed"
      ?{{enqueue:()=>Promise.reject(new Error("queue refused"))}}
      :{{enqueue:(spec)=>new Promise((resolve)=>{{this.release=async()=>resolve(
          await spec.run(spec.intent,spec.diagnostics,{{owner:true}}));}})}};}}
  _keyboardDebug(){{}}
  _projectDirName(){{return this._project;}}
  _hasPendingProjectMutations(){{return ["stalled_pending_entry",
    "redo_invalidated_by_edit"].includes(this.gate);}}
  async _restoreScene(_sceneId,target){{return target;}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const shared=["editor_destroyed","project_changed","claim_lost",
  "history_dependency_unresolved","stalled_pending_entry",
  "scene_history_lifecycle","target_scene_unavailable"];
let invalidatedRedoDepth=null;
for(const operation of ["undo","redo"]){{
  for(const gate of [...shared,...(operation==="redo"?["redo_invalidated_by_edit"]:[]),
      "queue_failed"]){{
    const h=new Harness(operation,gate);const entry=(operation==="undo"
      ?h._undoStack:h._redoStack)[0];
    if(gate==="stalled_pending_entry"){{
      h._priorPending={{sceneId:"scene-a",pending:true}};
      h._undoStack.unshift(h._priorPending);
    }}
    if(gate==="redo_invalidated_by_edit"){{
      h._priorPending={{sceneId:"scene-a",pending:true}};
      h._undoStack.push(h._priorPending);
    }}
    const call=operation==="undo"?h._undo():h._redo();
    if(gate==="queue_failed"){{await call;continue;}}
    if(gate==="editor_destroyed")h._destroyed=true;
    if(gate==="project_changed")h._project="project-b";
    if(gate==="claim_lost")delete entry.claimedBy;
    if(gate==="history_dependency_unresolved")entry._historyFuture=true;
    if(gate==="scene_history_lifecycle")h._sceneHistoryLifecycleOwner={{}};
    if(gate==="target_scene_unavailable"){{h.activeSceneId="scene-b";
      h.activeScene={{scene_id:"scene-b"}};}}
    if(gate==="redo_invalidated_by_edit"){{h._priorPending.pending=false;
      h._historyStackRevision=1;
      h._historyCommitRevisionByEntry.set(h._priorPending,1);
      h._clearRedoForNewEdit({{preserveFutureReservations:true}});}}
    await h.release();await call;
    if(gate==="redo_invalidated_by_edit")invalidatedRedoDepth=h._redoStack.length;
  }}
}}
for(const operation of ["undo","redo"]){{
  const empty=new Harness(operation,"empty");
  if(operation==="undo")empty._undoStack=[];else empty._redoStack=[];
  await (operation==="undo"?empty._undo():empty._redo());
  const stalled=new Harness(operation,"stalled");
  if(operation==="undo")stalled._undoStack.unshift({{sceneId:"scene-a",pending:true}});
  else stalled._undoStack=[{{sceneId:"scene-a",pending:true}},
    {{sceneId:"scene-a",pending:false}}];
  await (operation==="undo"?stalled._undo():stalled._redo());
}}
console.log(JSON.stringify({{records,invalidatedRedoDepth,sourceOrder:{{
  undoEmpty:{json.dumps(undo)}.indexOf('"empty_stack"'),
  undoPending:{json.dumps(undo)}.indexOf('"stalled_pending_entry"'),
  redoEmpty:{json.dumps(redo)}.indexOf('"empty_stack"'),
  redoPending:{json.dumps(redo)}.indexOf('"stalled_pending_entry"'),
}}}}));
""")
    shared = ["editor_destroyed", "project_changed", "claim_lost",
              "history_dependency_unresolved",
              "stalled_pending_entry", "scene_history_lifecycle",
              "target_scene_unavailable"]
    expected = ([*(('undo', gate) for gate in shared), ('undo', 'queue_failed'),
                 *(('redo', gate) for gate in shared),
                 ('redo', 'redo_invalidated_by_edit'), ('redo', 'queue_failed'),
                 ('undo', 'empty_stack'), ('undo', 'stalled_pending_entry'),
                 ('redo', 'empty_stack'), ('redo', 'stalled_pending_entry')])
    assert [(row["operation"], row["gate"]) for row in result["records"]] == expected
    assert all("scene_id" in row and "gesture_id" in row
               for row in result["records"])
    assert result["invalidatedRedoDepth"] == 0
    # Undo discovers an empty source stack before it can inspect an entry;
    # Redo deliberately checks a pending Undo transaction first.
    assert result["sourceOrder"]["undoEmpty"] < result["sourceOrder"]["undoPending"]
    assert result["sourceOrder"]["redoPending"] < result["sourceOrder"]["redoEmpty"]


def test_history_apply_refusals_emit_entry_and_gesture_diagnostics_at_runtime():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    refusal = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
const records=[];globalThis.sessionDiagRecord=(kind,payload)=>{{
  if(kind==="history_operation_refused")records.push(payload);
}};
class Harness {{
{undo}
{redo}
{refusal}
  constructor(mode,operation){{this.mode=mode;this.operation=operation;
    this.activeSceneId="active-scene";this.activeScene={{scene_id:"active-scene"}};
    this._undoStack=[];this._redoStack=[];this._editorFocused=false;}}
  _keyboardDebug(){{}}
  _captureProjectDependencies(){{return {{}};}}
  async _restoreProjectDependencies(){{throw new Error("project dependencies refused");}}
  async _applyPromptIdentityChange(){{throw new Error("prompt identity refused");}}
  async _applyReferenceHistoryOperations(){{throw new Error("reference refused");}}
  async _restoreScene(){{const error=new Error("scene refused");
    if(this.mode==="restore_ambiguous"){{error.restoreAmbiguous=true;error.restoreToken="token";}}
    throw error;}}
}}
const entryFor=(mode)=>{{
  const base={{sceneId:"entry-scene",label:mode,snapshot:{{scene_id:"entry-scene"}}}};
  if(mode==="project_dependencies_restore_failed")return{{...base,kind:"project_dependencies"}};
  if(mode==="prompt_identity_restore_failed")return{{...base,kind:"prompt_identity",
    change:{{}},inverseChange:{{}}}};
  if(mode==="reference_restore_failed")return{{...base,kind:"reference_change",
    operations:[{{type:"x"}}],inverseOperations:[]}};
  if(mode==="missing_post_snapshot")return base;
  return{{...base,postSnapshot:{{scene_id:"entry-scene"}}}};
}};
for(const operation of ["undo","redo"]){{
  for(const mode of ["project_dependencies_restore_failed","prompt_identity_restore_failed",
    "reference_restore_failed","missing_post_snapshot","restore_ambiguous","restore_failed"]){{
    const h=new Harness(mode,operation);const entry=entryFor(mode);
    (operation==="undo"?h._undoStack:h._redoStack).push(entry);
    await (operation==="undo"
      ?h._runUndoWithinGesture({{gestureId:`g-${{operation}}-${{mode}}`}})
      :h._runRedoWithinGesture({{gestureId:`g-${{operation}}-${{mode}}`}}));
  }}
}}
console.log(JSON.stringify(records));
""")
    gates = [
        "project_dependencies_restore_failed",
        "prompt_identity_restore_failed",
        "reference_restore_failed",
        "missing_post_snapshot",
        "restore_ambiguous",
        "restore_failed",
    ]
    assert len(result) == 12
    for operation_index, operation in enumerate(("undo", "redo")):
        rows = result[operation_index * 6:(operation_index + 1) * 6]
        assert [row["gate"] for row in rows] == gates
        assert all(row["operation"] == operation for row in rows)
        assert all(row["scene_id"] == "entry-scene" for row in rows)
        assert [row["gesture_id"] for row in rows] == [
            f"g-{operation}-{gate}" for gate in gates]


def test_missing_post_snapshot_is_consumed_once_for_direct_and_reserved_history():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_runUndoWithinGesture", "_redo")
    redo = _method(widget, "_runRedoWithinGesture", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
const warnings=[];const diagnostics=[];
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=(message)=>warnings.push(message);
globalThis.sessionDiagRecord=(kind,payload)=>diagnostics.push({{kind,payload}});
class Harness {{
{undo}
{redo}
  constructor(mode){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:"after"}};
    this.scenes=[this.activeScene];this._undoStack=[];this._redoStack=[];
    this.restores=[];this.refusals=[];this.mode=mode;}}
  _keyboardDebug(){{}}
  _recordHistoryRefusal(operation,gate,_diagnostics,detail){{
    this.refusals.push({{operation,gate,...detail}});}}
  _recordHistoryOrderedScene(){{}}
  _historyBaseForRestoredScene(scene){{return scene;}}
  async _restoreScene(_id,target){{this.restores.push(target.value);return structuredClone(target);}}
  _materializeHistoryOpposite(stack,reservation,opposite){{
    const index=stack.indexOf(reservation);if(index<0)return false;
    stack.splice(index,1,opposite);return true;}}
  async _restoreProjectDependencies(){{}}
  _captureProjectDependencies(){{return {{}};}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
}}
async function direct(mode){{
  const h=new Harness(mode);
  const valid={{sceneId:"scene",label:"valid",
    snapshot:{{scene_id:"scene",value:mode==="undo"?"before":"after"}},
    postSnapshot:{{scene_id:"scene",value:mode==="undo"?"after":"before"}}}};
  const poison={{sceneId:"scene",label:"failed",snapshot:{{scene_id:"scene",value:"bad"}}}};
  const source=mode==="undo"?h._undoStack:h._redoStack;
  source.push(valid,poison);
  await (mode==="undo"?h._runUndoWithinGesture():h._runRedoWithinGesture());
  const afterPoison={{source:source.map((entry)=>entry.label),
    opposite:(mode==="undo"?h._redoStack:h._undoStack).map((entry)=>entry.label),
    restores:[...h.restores]}};
  await (mode==="undo"?h._runUndoWithinGesture():h._runRedoWithinGesture());
  return {{afterPoison,afterValid:{{source:source.map((entry)=>entry.label),
    opposite:(mode==="undo"?h._redoStack:h._undoStack).map((entry)=>entry.label),
    restores:[...h.restores]}},refusals:h.refusals}};
}}
async function reserved(mode){{
  const h=new Harness(mode);const claim={{id:"claim"}};
  const poison={{sceneId:"scene",label:"failed",snapshot:{{scene_id:"scene"}},claimedBy:claim}};
  const source=mode==="undo"?h._undoStack:h._redoStack;
  const opposite=mode==="undo"?h._redoStack:h._undoStack;
  const future={{_historyFuture:true}};source.push(poison);opposite.push(future);
  await (mode==="undo"
    ? h._runUndoWithinGesture(null,poison,claim,null,future,null)
    : h._runRedoWithinGesture(null,poison,claim,null,future,null));
  return {{source:source.length,opposite:opposite.length,futureStill:opposite.includes(future),
    restores:h.restores,refusals:h.refusals}};
}}
const output={{undo:await direct("undo"),redo:await direct("redo"),
  reservedUndo:await reserved("undo"),reservedRedo:await reserved("redo"),
  warnings,diagnostics}};
console.log(JSON.stringify(output));
""")
    for operation in ("undo", "redo"):
        direct = result[operation]
        assert direct["afterPoison"] == {
            "source": ["valid"], "opposite": [], "restores": []}
        assert direct["afterValid"]["source"] == []
        assert direct["afterValid"]["opposite"] == ["valid"]
        assert direct["afterValid"]["restores"] == ["before" if operation == "undo" else "after"]
        assert direct["refusals"][0]["gate"] == "missing_post_snapshot"
        assert direct["refusals"][0]["entry_discarded"] is True
    for key in ("reservedUndo", "reservedRedo"):
        reserved = result[key]
        assert reserved["source"] == 0
        assert reserved["opposite"] == 1  # outer queued finalizer releases this reservation
        assert reserved["futureStill"] is True
        assert reserved["restores"] == []
        assert reserved["refusals"][0]["entry_discarded"] is True
    assert len(result["warnings"]) == 4
    assert all("Press Undo again" in message or "Press Redo again" in message
               for message in result["warnings"])
    recovered = [row for row in result["diagnostics"]
                 if row["kind"] == "undo_missing_post_snapshot"]
    assert len(recovered) == 4
    assert all(row["payload"]["entry_discarded"] is True for row in recovered)


def test_queued_missing_post_snapshot_releases_its_opposite_reservation():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    reservations = _method(widget, "_reserveHistoryOpposite", "_beginHistoryOrderContext")
    contexts = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};globalThis.performance={{now:()=>0}};
globalThis.api={{apiURL:value=>value}};
let reads=0;globalThis.fetchProjectJson=async()=>{{reads++;throw new Error("offline");}};
class Harness {{
{undo}
{redo}
{helpers}
{reservations}
{contexts}
  constructor(mode){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene"}};
    this.scenes=[this.activeScene];this._undoStack=[];this._redoStack=[];
    this._historyStackRevision=0;this._historyOperationSeq=0;
    this._sceneMutationInvalidationSeq=0;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._projectMutationQueue=new ProjectMutationQueue();
    this._latestHistoryOrderContext={{scenes:new Map([["scene",null]])}};
    (mode==="undo"?this._undoStack:this._redoStack).push({{
      sceneId:"scene",label:"failed",snapshot:{{scene_id:"scene"}}}});}}
  _projectDirName(){{return "project";}}
  _keyboardDebug(){{}}
  _activateGraphUndoSuppression(){{}}
  _schedulePostMutationSceneRefresh(){{}}
  _deferProjectBackedRefresh(){{}}
  _trimUndoStack(){{}}
  _hasPendingProjectMutations(){{return this._projectMutationQueue.isBusy();}}
}}
async function run(mode){{const h=new Harness(mode);
  let release;const gate=new Promise(resolve=>{{release=resolve;}});
  const blocker=h._projectMutationQueue.enqueue({{key:"blocker",run:()=>gate}});
  const pending=mode==="undo"?h._undo():h._redo();
  const futureCount=(mode==="undo"?h._redoStack:h._undoStack).length;
  release();await blocker;await pending;
  return {{undo:h._undoStack.length,redo:h._redoStack.length,
    inFlight:Boolean(h._historyOperationInFlight),futureCount,reads}};}}
console.log(JSON.stringify({{undo:await run("undo"),redo:await run("redo")}}));
""")
    assert result == {
        "undo": {"undo": 0, "redo": 0, "inFlight": False, "futureCount": 1, "reads": 0},
        "redo": {"undo": 0, "redo": 0, "inFlight": False, "futureCount": 1, "reads": 0},
    }


def test_failure_recovery_preserves_stamped_and_restore_token_entries():
    widget = _source("web/js/editor_widget.js")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    undo = _method(widget, "_runUndoWithinGesture", "_redo")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.sessionDiagRecord=()=>{{}};globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const stamped={{sceneId:"scene",label:"stamped",snapshot:{{scene_id:"scene"}},
  postSnapshot:{{scene_id:"scene"}}}};
class QueueHarness {{
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();
    this._pendingHistoryEntryByMutationKey=new Map();this._undoStack=[stamped];}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _historyOrderedSceneForContext(){{return null;}}
  _recordHistoryOrderedScene(){{}} _stampHistoryPostSnapshot(){{}}
  _schedulePostMutationSceneRefresh(){{}} _deferProjectBackedRefresh(){{}}
  _discardUndoEntry(entry){{const index=this._undoStack.indexOf(entry);
    if(index<0)return false;this._undoStack.splice(index,1);return true;}}
}}
const q=new QueueHarness();
try {{await q._queueProjectMutation({{key:"failed",coalesce:false,refreshScenes:false,
  historyEntry:stamped,intent:{{sceneId:"scene",operations:[]}},
  run:async()=>{{throw new Error("lost");}}}});}} catch(_error){{}}
const tokenEntry={{sceneId:"scene",label:"token",snapshot:{{scene_id:"scene"}},
  restoreToken:"receipt"}};
class UndoHarness {{
{undo}
  constructor(){{this._undoStack=[tokenEntry];this._redoStack=[];
    this.activeSceneId="scene";this.activeScene={{scene_id:"scene"}};}}
  _keyboardDebug(){{}} _recordHistoryRefusal(){{}}
}}
const u=new UndoHarness();await u._runUndoWithinGesture();
console.log(JSON.stringify({{stampedKept:q._undoStack.includes(stamped),
  tokenKept:u._undoStack.includes(tokenEntry),token:tokenEntry.restoreToken}}));
""")
    assert result == {
        "stampedKept": True,
        "tokenKept": True,
        "token": "receipt",
    }


def test_queued_history_notification_is_single_and_tracks_depth():
    widget = _source("web/js/editor_widget.js")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    result = _run_node(f"""
const events=[];
globalThis.notifyProgress=(options)=>{{events.push(["create",options]);return{{
  update:(patch)=>events.push(["update",patch]),
  dismiss:()=>events.push(["dismiss"]),
}};}};
class Harness {{
{helpers}
  constructor(){{this._queuedHistoryOperationCount=0;this._queuedHistoryNotification=null;}}
}}
const h=new Harness();const first=h._beginQueuedHistoryWait("undo");
const second=h._beginQueuedHistoryWait("redo");
h._endQueuedHistoryWait(first);h._endQueuedHistoryWait(second);
h._endQueuedHistoryWait(second);
console.log(JSON.stringify({{events,count:h._queuedHistoryOperationCount,
  handle:h._queuedHistoryNotification}}));
""")
    assert result["count"] == 0
    assert result["handle"] is None
    assert [event[0] for event in result["events"]] == [
        "create", "update", "update", "dismiss"]
    assert result["events"][0][1]["source"] == "history-operation-queued"
    assert "1 history action" in result["events"][0][1]["message"]
    assert "2 history actions" in result["events"][1][1]["message"]
    assert "1 history action" in result["events"][2][1]["message"]


def test_busy_undo_keeps_reserved_entry_stampable_until_queued_apply():
    widget = _source("web/js/editor_widget.js")
    push_and_stamp = _method(widget, "_pushUndo", "_commitUndoEntry")
    commit_and_trim = _method(
        widget, "_commitUndoEntry", "_captureProjectDependencies")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    undo = _method(widget, "_undo", "_redo")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
const warnings=[];globalThis.notifyInfo=()=>{{}};
globalThis.notifyWarning=(message)=>warnings.push(message);globalThis.notifyError=()=>{{}};
const events=[];globalThis.notifyProgress=(options)=>{{events.push(["create",options]);return{{
  update:(patch)=>events.push(["update",patch]),dismiss:()=>events.push(["dismiss"]),
}};}};globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{push_and_stamp}
{commit_and_trim}
{queue_mutation}
{undo}
{helpers}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene",value:"before"}};
    this._undoStack=[];this._redoStack=[];this._maxUndoSteps=50;this._editorFocused=false;
    this._historyStackRevision=0;this._historyOperationSeq=0;
    this._queuedHistoryOperationCount=0;this._queuedHistoryNotification=null;
    this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();this.restores=[];}}
  _keyboardDebug(){{}}
  _hasPendingProjectMutations(){{return this._projectMutationQueue.isBusy();}}
  async _restoreScene(_sceneId,target,expectedPost){{
    this.restores.push({{target:target.value,expectedPost:expectedPost?.value || null}});return target;}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();const entry=h._pushUndo("edit",{{pending:true}});let release;
const write=h._queueProjectMutation({{key:"scene:scene:edit",label:"edit",coalesce:false,
  refreshScenes:false,intent:{{sceneId:"scene"}},run:async()=>{{
    await new Promise((resolve)=>release=resolve);
    return {{payload:{{scene:{{scene_id:"scene",value:"after"}}}}}};
  }}}});
const committed=write.then(()=>h._commitUndoEntry(entry));
while(!release)await new Promise((resolve)=>setImmediate(resolve));
const pending=h._undo();await Promise.resolve();
const during={{claimed:Boolean(entry.claimedBy),stamped:Boolean(entry.postSnapshot),
  retained:h._undoStack.includes(entry),entryPending:entry.pending,
  revision:h._historyStackRevision,count:h._queuedHistoryOperationCount,events:[...events]}};
release();await write;const committedResult=await committed;await pending;
console.log(JSON.stringify({{during,after:{{count:h._queuedHistoryOperationCount,
  events,handle:h._queuedHistoryNotification,restores:h.restores,
  undo:h._undoStack.length,redo:h._redoStack.length,committedResult,
  revision:h._historyStackRevision,warnings}}}}));
""")
    assert result["during"]["claimed"] is True
    assert result["during"]["stamped"] is False
    assert result["during"]["retained"] is True
    assert result["during"]["entryPending"] is True
    assert result["during"]["revision"] == 0
    assert result["during"]["count"] == 1
    assert [event[0] for event in result["during"]["events"]] == ["create"]
    assert result["after"]["count"] == 0
    assert result["after"]["handle"] is None
    assert [event[0] for event in result["after"]["events"]] == ["create", "dismiss"]
    assert result["after"]["restores"] == [
        {"target": "before", "expectedPost": "after"}]
    assert result["after"]["undo"] == 0
    assert result["after"]["redo"] == 1
    assert result["after"]["committedResult"] is True
    assert result["after"]["revision"] == 1
    assert result["after"]["warnings"] == []


def test_edits_after_queued_history_use_physical_order_for_snapshots_and_validity():
    widget = _source("web/js/editor_widget.js")
    push_and_stamp = _method(widget, "_pushUndo", "_commitUndoEntry")
    commit_and_helpers = _method(
        widget, "_commitUndoEntry", "_captureProjectDependencies")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    clear_redo = _method(widget, "_clearRedoForNewEdit", "_recordHistoryRefusal")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
const warnings=[];globalThis.notifyInfo=()=>{{}};
globalThis.notifyWarning=(message)=>warnings.push(message);globalThis.notifyError=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
let receiptScene=null;globalThis.api={{apiURL:(path)=>path}};
globalThis.fetch=async()=>({{ok:true,json:async()=>({{
  status:"committed",scene:structuredClone(receiptScene)}})}});
globalThis.fetchProjectJson=async()=>({{payload:structuredClone(receiptScene)}});
class Harness {{
{push_and_stamp}
{commit_and_helpers}
{queue_mutation}
{undo}
{redo}
{clear_redo}
{helpers}
  constructor(mode){{this.mode=mode;this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",a:mode==="redo"?1:2,b:0}};
    const history={{sceneId:"scene",snapshot:{{scene_id:"scene",a:mode==="redo"?2:1,b:0}},
      postSnapshot:{{scene_id:"scene",a:mode==="redo"?1:2,b:0}},label:"first"}};
    this._undoStack=mode==="redo"?[]:[history];this._redoStack=mode==="redo"?[history]:[];
    this._maxUndoSteps=50;this._editorFocused=false;this._historyStackRevision=0;
    this._historyCommitRevisionByEntry=new WeakMap();this._historyOperationSeq=0;
    this._latestHistoryOrderContext=null;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._sceneMutationInvalidationSeq=0;
    this._queueFetchSeq=0;this._projectMutationQueue=new ProjectMutationQueue();this.restores=[];}}
  _keyboardDebug(){{}}
  _projectDirName(){{return "project";}}
  _hasPendingProjectMutations(){{return this._projectMutationQueue.isBusy();}}
  async _restoreScene(_sceneId,target){{this.restores.push(structuredClone(target));
    if(this.ambiguousRestore){{receiptScene=structuredClone(target);
      const error=new Error("lost response");error.restoreAmbiguous=true;
      error.restoreToken="restore-token";throw error;}}
    this.activeScene=structuredClone(target);return structuredClone(target);}}
  async _applyPromptIdentityChange(){{}}
  async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
async function runLaterEdit(mode,pending){{
  const h=new Harness(mode);let release;
  const blocker=h._projectMutationQueue.enqueue({{key:"busy",coalesce:false,
    run:async()=>await new Promise((resolve)=>release=resolve)}});
  while(!release)await new Promise((resolve)=>setImmediate(resolve));
  const historyCall=mode==="redo"?h._redo():h._undo();
  const later=h._pushUndo("later",{{pending}});h.activeScene.b=1;
  const write=h._queueProjectMutation({{key:`later-${{mode}}-${{pending}}`,label:"later",
    coalesce:false,refreshScenes:false,historyEntry:later,intent:{{sceneId:"scene"}},
    run:async()=>({{payload:{{scene:{{scene_id:"scene",a:mode==="redo"?2:1,b:1}}}}}})}});
  const committed=pending?write.then(()=>h._commitUndoEntry(later)):write;
  release();await blocker;await Promise.all([historyCall,committed]);
  return {{restores:h.restores.map((scene)=>scene.a),before:later.snapshot,
    post:later.postSnapshot,undo:h._undoStack.length,redo:h._redoStack.length}};
}}
async function runEarlierPendingRedo(){{
  const h=new Harness("redo");let release;
  const blocker=h._projectMutationQueue.enqueue({{key:"busy",coalesce:false,
    run:async()=>await new Promise((resolve)=>release=resolve)}});
  while(!release)await new Promise((resolve)=>setImmediate(resolve));
  const earlier=h._pushUndo("earlier",{{pending:true}});h.activeScene.b=1;
  const write=h._queueProjectMutation({{key:"earlier",label:"earlier",coalesce:false,
    refreshScenes:false,historyEntry:earlier,intent:{{sceneId:"scene"}},
    run:async()=>({{payload:{{scene:{{scene_id:"scene",a:1,b:1}}}}}})}});
  const committed=write.then(()=>h._commitUndoEntry(earlier));
  const redoCall=h._redo();release();await blocker;await Promise.all([committed,redoCall]);
  return {{restores:h.restores.length,undo:h._undoStack.length,redo:h._redoStack.length}};
}}
async function runTrailingWave(){{
  const h=new Harness("undo");h.activeScene.c=0;
  h._undoStack[0].snapshot.c=0;h._undoStack[0].postSnapshot.c=0;
  let releaseBlocker;const blocker=h._projectMutationQueue.enqueue({{key:"busy-wave",
    coalesce:false,run:async()=>await new Promise((resolve)=>releaseBlocker=resolve)}});
  while(!releaseBlocker)await new Promise((resolve)=>setImmediate(resolve));
  const historyCall=h._undo();const editB=h._pushUndo("B");h.activeScene.b=1;
  let releaseB;const writeB=h._queueProjectMutation({{key:"B",label:"B",coalesce:false,
    refreshScenes:false,historyEntry:editB,intent:{{sceneId:"scene"}},run:async()=>{{
      await new Promise((resolve)=>releaseB=resolve);
      return {{payload:{{scene:{{scene_id:"scene",a:1,b:1,c:0}}}}}};
    }}}});
  releaseBlocker();await blocker;
  while(!releaseB)await new Promise((resolve)=>setImmediate(resolve));
  const editC=h._pushUndo("C");h.activeScene.c=1;
  const writeC=h._queueProjectMutation({{key:"C",label:"C",coalesce:false,
    refreshScenes:false,historyEntry:editC,intent:{{sceneId:"scene"}},
    run:async()=>({{payload:{{scene:{{scene_id:"scene",a:1,b:1,c:1}}}}}})}});
  releaseB();await Promise.all([historyCall,writeB,writeC]);
  return {{bBefore:editB.snapshot,bPost:editB.postSnapshot,
    cBefore:editC.snapshot,cPost:editC.postSnapshot}};
}}
async function runAmbiguousHistory(){{
  const h=new Harness("undo");h.ambiguousRestore=true;let releaseBlocker;
  const blocker=h._projectMutationQueue.enqueue({{key:"busy-ambiguous",coalesce:false,
    run:async()=>await new Promise((resolve)=>releaseBlocker=resolve)}});
  while(!releaseBlocker)await new Promise((resolve)=>setImmediate(resolve));
  const historyCall=h._undo();const later=h._pushUndo("after ambiguous");h.activeScene.b=1;
  const write=h._queueProjectMutation({{key:"after-ambiguous",label:"after ambiguous",
    coalesce:false,refreshScenes:false,historyEntry:later,intent:{{sceneId:"scene"}},
    run:async()=>({{payload:{{scene:{{scene_id:"scene",a:1,b:1}}}}}})}});
  releaseBlocker();await blocker;await Promise.all([historyCall,write]);
  return {{before:later.snapshot,post:later.postSnapshot}};
}}
const laterPendingUndo=await runLaterEdit("undo",true);
const laterOrdinaryRedo=await runLaterEdit("redo",false);
const earlierPendingRedo=await runEarlierPendingRedo();
const trailingWave=await runTrailingWave();
const ambiguousHistory=await runAmbiguousHistory();
console.log(JSON.stringify({{laterPendingUndo,laterOrdinaryRedo,earlierPendingRedo,
  trailingWave,ambiguousHistory,warnings}}));
""")
    assert result["laterPendingUndo"] == {
        "restores": [1],
        "before": {"scene_id": "scene", "a": 1, "b": 0},
        "post": {"scene_id": "scene", "a": 1, "b": 1},
        "undo": 1,
        "redo": 0,
    }
    assert result["laterOrdinaryRedo"] == {
        "restores": [2],
        "before": {"scene_id": "scene", "a": 2, "b": 0},
        "post": {"scene_id": "scene", "a": 2, "b": 1},
        "undo": 2,
        "redo": 0,
    }
    assert result["earlierPendingRedo"] == {
        "restores": 0, "undo": 1, "redo": 0}
    assert result["trailingWave"] == {
        "bBefore": {"scene_id": "scene", "a": 1, "b": 0, "c": 0},
        "bPost": {"scene_id": "scene", "a": 1, "b": 1, "c": 0},
        "cBefore": {"scene_id": "scene", "a": 1, "b": 1, "c": 0},
        "cPost": {"scene_id": "scene", "a": 1, "b": 1, "c": 1},
    }
    assert result["ambiguousHistory"] == {
        "before": {"scene_id": "scene", "a": 1, "b": 0},
        "post": {"scene_id": "scene", "a": 1, "b": 1},
    }
    assert len(result["warnings"]) == 2
    assert any("newer edit changed its history" in warning
               for warning in result["warnings"])
    assert any("still being confirmed" in warning
               for warning in result["warnings"])


def test_later_scene_mutation_rebases_compare_state_and_stable_targets_after_history():
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
class Harness {{
{rebase}
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();this._pendingHistoryEntryByMutationKey=new Map();}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _stampHistoryPostSnapshot(){{}}
  _historyOrderedSceneForContext(context,sceneId){{return context?.scenes?.get(sceneId)||null;}}
  _recordHistoryOrderedScene(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const ordered={{scene_id:"scene",prompt:"after undo",global_channel_docs:{{visual:{{text:"u"}}}},
  prompt_sections:[{{prompt_id:"p",start_frame:4,end_frame:8,channel_docs:{{visual:{{text:"u"}}}},
    attachments:[{{attachment_id:"a",value:"u"}}]}}],
  guide_frames:[{{guide_id:"g",frame_index:7,asset_id:"new"}}],
  reference_items:[{{reference_item_id:"r",start_frame:3,end_frame:9,muted:false}}]}};
const context={{scenes:new Map([["scene",ordered]])}};
const intent={{sceneId:"scene",operations:[
  {{type:"update_scene_fields",fields:{{prompt:"later"}},expected:{{prompt:"before undo",global_channel_docs:{{}}}}}},
  {{type:"update_prompt_section",index:1,expected:{{prompt_id:"p",start_frame:20}},
    fields:{{prompt_edit:{{documents:{{visual:{{expected:{{text:"old"}},value:{{text:"later"}}}}}},
      attachments:{{a:{{expected:{{attachment_id:"a",value:"old"}},value:null}}}}}}}}}},
  {{type:"move_guide",from_frame_index:20,to_frame_index:21,
    expected:{{guide_id:"g",frame_index:20,asset_id:"old"}}}},
  {{type:"update_reference_item",reference_item_id:"r",fields:{{muted:true}},expected:{{muted:true}}}},
  {{type:"delete_prompt_section",index:1,expected:{{start_frame:20,end_frame:30}}}}
]}};
let sent=null;
const h=new Harness();h._latestHistoryOrderContext=context;
await h._queueProjectMutation({{key:"later",label:"later",coalesce:false,
  refreshScenes:false,intent,run:async(value)=>{{sent=value;
    if(value.operations[0].expected.prompt!==ordered.prompt)throw new Error("stale prompt compare");
    return {{payload:{{scene:ordered}}}};}}}});
console.log(JSON.stringify({{sent,original:intent}}));
""")
    sent = result["sent"]["operations"]
    assert sent[0]["expected"] == {
        "prompt": "after undo", "global_channel_docs": {"visual": {"text": "u"}}}
    assert sent[1]["index"] == 0
    assert sent[1]["expected"] == {"prompt_id": "p", "start_frame": 4}
    assert sent[1]["fields"]["prompt_edit"]["documents"]["visual"]["expected"] == {
        "text": "u"}
    assert sent[1]["fields"]["prompt_edit"]["attachments"]["a"]["expected"] == {
        "attachment_id": "a", "value": "u"}
    assert sent[2]["from_frame_index"] == 7
    assert sent[2]["expected"] == {
        "guide_id": "g", "frame_index": 7, "asset_id": "new"}
    assert sent[3]["expected"] == {"muted": False}
    assert sent[4] == {
        "type": "delete_prompt_section", "index": 1,
        "expected": {"start_frame": 20, "end_frame": 30},
    }
    assert result["original"]["operations"][0]["expected"]["prompt"] == "before undo"
    delete_method = _method(widget, "_deletePromptSection", "_showItemEditor")
    assert 'prompt_id: section.prompt_id || ""' in delete_method


def test_later_lane_mutation_rebases_identity_and_count_through_the_queue():
    """Drive the production wiring, not the helper in isolation.

    `_queueProjectMutation` overwrites `historyEntry.snapshot` with the ordered
    scene before the rebase reads it, so a test that calls
    `_rebaseSceneMutationIntentForHistory` directly with a distinct authored
    scene passes against wiring that can never supply one. Assert on what was
    actually sent.
    """
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
class Harness {{
{rebase}
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();
    this._pendingHistoryEntryByMutationKey=new Map();}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _stampHistoryPostSnapshot(){{}}
  _historyOrderedSceneForContext(context,sceneId){{return context?.scenes?.get(sceneId)||null;}}
  _recordHistoryOrderedScene(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const authored={{scene_id:"scene",video_lane_count:1,
  video_lane_configs:[{{name:"B",locked:false}}],clips:[]}};
const ordered={{scene_id:"scene",video_lane_count:2,
  video_lane_configs:[{{name:"A",locked:false}},{{name:"B",locked:false}}],clips:[]}};
const historyEntry={{sceneId:"scene",label:"undo",snapshot:authored}};
const context={{scenes:new Map([["scene",ordered]])}};
const intent={{sceneId:"scene",operations:[
  {{type:"update_lane_config",lane_type:"video",lane_index:0,fields:{{locked:true}}}},
  {{type:"set_lane_count",lane_type:"video",count:2}}
]}};
let sent=null;
const h=new Harness();h._latestHistoryOrderContext=context;
await h._queueProjectMutation({{key:"lane",label:"lane",coalesce:false,
  refreshScenes:false,intent,historyEntry,
  run:async(value)=>{{sent=value;return {{payload:{{scene:ordered}}}};}}}});
console.log(JSON.stringify({{sent,
  entrySnapshotLaneCount:historyEntry.snapshot?.video_lane_count}}));
""")
    operations = result["sent"]["operations"]
    # Lane B sat at index 0 when the edit was authored; the queued undo restored
    # lane A ahead of it, so the write must land on index 1.
    assert operations[0]["lane_index"] == 1
    # Authored delta was +1 onto a 1-lane scene; the ordered scene has 2.
    assert operations[1]["count"] == 3
    # The snapshot is still rebased to the queue position - that invariant feeds
    # the restore target at the undo/redo apply sites and must not regress.
    assert result["entrySnapshotLaneCount"] == 2


def test_equal_count_lane_content_move_never_retargets_later_destination():
    """Lane membership is content, not topology or destination identity.

    This is the manual failure from the queued-Undo run driven through the
    production queue wrapper: FirstScene occupied Target when the later drop
    was authored, then Undo moved it back to C before that drop executed.  The
    three lane slots still mean B/C/Target, so the destination must remain 2.
    """
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
class Harness {{
{rebase}
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();
    this._pendingHistoryEntryByMutationKey=new Map();}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _stampHistoryPostSnapshot(){{}}
  _historyOrderedSceneForContext(context,sceneId){{return context?.scenes?.get(sceneId)||null;}}
  _recordHistoryOrderedScene(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const laneConfigs=[{{name:"B"}},{{name:"C"}},{{name:"Target"}}];
const authored={{scene_id:"scene",video_lane_count:3,video_lane_configs:laneConfigs,
  clips:[{{clip_id:"FirstScene",track_index:2}}]}};
const ordered={{scene_id:"scene",video_lane_count:3,video_lane_configs:laneConfigs,
  clips:[{{clip_id:"FirstScene",track_index:1}}]}};
const historyEntry={{sceneId:"scene",label:"drop",snapshot:authored}};
const context={{scenes:new Map([["scene",ordered]])}};
const intent={{sceneId:"scene",operations:[
  {{type:"create_clip",fields:{{asset_id:"waking",track_index:2,dual_drop:false}}}},
  {{type:"update_clip",clip_id:"waking",fields:{{track_index:2}}}}
]}};
let sent=null;
const h=new Harness();h._latestHistoryOrderContext=context;
await h._queueProjectMutation({{key:"drop",label:"drop",coalesce:false,
  refreshScenes:false,intent,historyEntry,
  run:async(value)=>{{sent=value;return {{payload:{{scene:ordered}}}};}}}});
console.log(JSON.stringify({{sent}}));
""")
    assert result["sent"]["operations"][0]["fields"]["track_index"] == 2
    assert result["sent"]["operations"][1]["fields"]["track_index"] == 2


def test_production_media_drops_rebase_behind_history_and_stamp_batch_scene():
    widget = _source("web/js/editor_widget.js")
    queue_setup = widget[widget.index("this._projectMutationQueue = new ProjectMutationQueue({"):
                         widget.index("        this._projectMutationCloseInProgress = null;")]
    methods = "\n".join([
        _method(widget, "_handleAssetDropWithinGesture", "_firstAvailableLane"),
        _method(widget, "_historyExpectedProjection", "_queueProjectMutation"),
        _method(widget, "_queueProjectMutation", "_runSceneMutation"),
        _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies"),
        _method(widget, "_discardUndoEntry", "_trimLocalLaneConfigs"),
    ])
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    lanes_url = (ROOT / "web/js/lane_registry.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
const {{TRACK_TYPE,descriptorFor,laneCountFor,laneAcceptsAssetType}}=await import({json.dumps(lanes_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};globalThis.notifyInfo=()=>{{}};
globalThis.api={{apiURL:value=>value}};
const configs=[{{name:"B"}},{{name:"C"}},{{name:"Target"}}];
const base={{scene_id:"scene",video_lane_count:3,audio_lane_count:3,motion_driver_lane_count:3,
  video_lane_configs:configs,audio_lane_configs:configs,motion_driver_lane_configs:configs,
  clips:[{{clip_id:"FirstScene",track_index:2,timeline_start_frame:0,timeline_end_frame:24}}],audio_tracks:[]}};
class Harness {{
{methods}
  constructor(mode,structural,fail){{this.mode=mode;this.fail=fail;this.events=[];this.sent=[];
    this.projectDir="project";this.activeSceneId="scene";this._effectiveFps=24;
    this.activeScene=structuredClone(base);this.server=structuredClone(base);
    this.server.clips[0].track_index=1;
    if(structural)for(const type of ["video","audio","motion_driver"]){{
      this.server[type+"_lane_count"]=2;this.server[type+"_lane_configs"]=configs.slice(1);}}
    this.asset={{asset_id:"asset",asset_type:mode==="audio"?"audio":"video",has_audio:true}};
    this.assets={{[this.asset.asset_type]:[this.asset]}};
    this._trackLayout=[{{type:mode==="driver"?TRACK_TYPE.MOTION_DRIVER:
      ["audio","extracted"].includes(mode)?TRACK_TYPE.AUDIO:TRACK_TYPE.VIDEO,laneIndex:2}}];
    this._undoStack=[];
    {queue_setup}
    this._latestHistoryOrderContext={{scenes:new Map()}};
  }}
  _snapshotProjectMutationContext(){{return {{sceneId:"scene",projectId:"project"}};}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _pushUndo(label){{const entry={{sceneId:"scene",label,snapshot:structuredClone(this.activeScene)}};
    this._undoStack.push(entry);this.dropEntry=entry;return entry;}}
  _stampHistoryPostSnapshot(entry,scene){{if(entry&&scene)entry.postSnapshot=structuredClone(scene);}}
  _withMutationDiagnosticHeaders(init){{return init;}}
  _timelineRulerHeight(){{return 10;}} _layoutIndexFromRawY(){{return 0;}}
  _isLaneLocked(){{return false;}} _driverClipInLane(){{return false;}}
  _isRenderClip(clip){{return clip.role!=="motion_driver";}}
  _findAssetById(){{return this.asset;}} _mediaTimelineFrames(){{return 24;}}
  _defaultMotionDriverStrength(){{return 1;}} _defaultFitMode(){{return "contain";}}
  _defaultCropPosition(){{return "center";}}
  _renderSceneAfterLocalMutation(){{}} _buildTrackLayout(){{}} _renderTimeline(){{}}
  _deferProjectBackedRefresh(){{}} _schedulePostMutationSceneRefresh(){{}}
  _reconcileActiveSceneFromMutation(result){{
    this.activeScene=structuredClone(result.payload.scene);return true;}}
  _replayDeferredProjectBackedRefresh(){{}} // Model the refresh not yet completed.
  _showToast(message){{throw new Error(message);}}
  async _fetchScenes(){{this.activeScene=structuredClone(this.server);return true;}}
  async _runVersionedProjectMutation(path,init){{const body=JSON.parse(init.body);
    this.events.push("batch");this.sent.push(body);
    if(this.fail)throw new Error("batch failure");
    const results=[];
    for(const op of body.operations){{
      if(op.type==="set_lane_count"){{
        this.server[op.lane_type+"_lane_count"]=op.count;
        results.push({{type:op.type}});continue;
      }}
      if(op.type==="create_clip"){{
        const clip={{clip_id:"created",...op.fields}};this.server.clips.push(clip);
        let audio_track=null;
        if(op.fields.dual_drop){{audio_track={{track_id:"paired",lane_index:op.fields.audio_lane_index}};
          this.server.audio_tracks.push(audio_track);}}
        results.push({{type:op.type,clip,audio_track}});continue;
      }}
      const audio_track={{track_id:"created",...op.fields}};
      this.server.audio_tracks.push(audio_track);
      results.push({{type:op.type,audio_track}});
    }}
    return {{payload:{{results,scene:structuredClone(this.server)}}}};
  }}
}}
const outcomes=[];
for(const structural of [false,true])for(const mode of ["video","dual","audio","extracted","driver"]){{
  const h=new Harness(mode,structural,false);let release;
  const gate=new Promise(resolve=>{{release=resolve;}});
  const history=h._projectMutationQueue.enqueue({{key:"history",coalesce:false,run:async()=>{{
    await gate;h.events.push("undo");h.activeScene=structuredClone(h.server);
    h._recordHistoryOrderedScene(h._latestHistoryOrderContext,h.server);}}}});
  const drop=h._handleAssetDropWithinGesture(h.asset,100,mode==="dual"?0:20,1,null);
  const beforeRelease=h.sent.length;release();await history;await drop;
  let reads=0;
  globalThis.fetchProjectJson=async()=>{{reads++;return {{payload:structuredClone(h.server)}};}};
  const next={{sceneId:"scene",snapshot:structuredClone(h.activeScene)}};
  await h._queueProjectMutation({{key:"next",historyEntry:next,refreshScenes:false,
    intent:{{sceneId:"scene",projectId:"project",operations:[]}},
    run:async()=>({{payload:{{scene:structuredClone(h.server)}}}})}});
  outcomes.push({{mode,structural,beforeRelease,events:h.events,body:h.sent[0],reads,
    stamped:!!h.dropEntry.postSnapshot,
    baselineHasCreated:[...next.snapshot.clips,...next.snapshot.audio_tracks]
      .some(item=>(item.clip_id||item.track_id)==="created")}});
}}
// Internally caught failures run the complete production batch cleanup twice;
// neither pass may consume a same-labelled neighbor.
for(const mode of ["video","dual","audio","extracted","driver"]){{
  const h=new Harness(mode,false,true);const prior={{label:mode==="driver"?"add driver":"add asset",postSnapshot:{{}}}};
  h._undoStack.push(prior);
  await h._handleAssetDropWithinGesture(h.asset,100,mode==="dual"?0:20,1,null);
  outcomes.push({{mode,failure:true,prior:h._undoStack.includes(prior),failed:h._undoStack.includes(h.dropEntry)}});
}}
console.log(JSON.stringify(outcomes));
""")
    for row in result[:10]:
        assert row["beforeRelease"] == 0
        assert row["events"] == ["undo", "batch"]
        index = (2 if row["structural"] else 3) if row["mode"] == "dual" else (1 if row["structural"] else 2)
        field = "lane_index" if row["mode"] in ("audio", "extracted") else "track_index"
        create = next(op for op in row["body"]["operations"]
                      if op["type"].startswith("create_"))
        assert create["fields"][field] == index
        if row["mode"] == "dual":
            assert create["fields"]["audio_lane_index"] == index
            assert [op["type"] for op in row["body"]["operations"]] == [
                "set_lane_count", "set_lane_count", "create_clip"]
        else:
            assert len(row["body"]["operations"]) == 1
        assert row["reads"] == 0
        assert row["baselineHasCreated"] is True
        assert row["stamped"] is True
    for row in result[10:]:
        assert row["prior"] is True
        assert row["failed"] is False


def test_drop_batch_is_built_from_execution_time_lane_intent():
    """The queued semantic target, not an enqueue-time body closure, is sent."""
    widget = _source("web/js/editor_widget.js")
    drop = _method(widget, "_handleAssetDropWithinGesture", "_firstAvailableLane")
    helper = drop[drop.index("const queueDropMutation ="):
                  drop.index("// Zone-model drop targeting")]
    result = _run_node(f"""
let sentBody=null;let queuedIntent=null;
class Harness {{
  constructor(fail=false){{this.fail=fail;this.discarded=false;}}
  _snapshotProjectMutationContext(){{return {{projectId:"project",sceneId:"scene"}};}}
  _withMutationDiagnosticHeaders(init){{return init;}}
      async _runVersionedProjectMutation(_path,init){{if(this.fail)throw new Error("404");
        sentBody=JSON.parse(init.body);
        return {{payload:{{results:[{{type:"create_clip",clip:{{clip_id:"created"}}}}],
          scene:{{scene_id:"scene"}}}}}};}}
  _discardUnstampableUndoEntry(entry){{this.discarded=entry?.label==="drop";}}
  _queueProjectMutation(options){{queuedIntent=options.intent;
    const ordered=structuredClone(options.intent);
    ordered.operations[0].fields.track_index=7;
    ordered.operations[0].fields.audio_lane_index=5;
    return options.run(ordered,null);}}
  async run(){{
    const dropSeq=3;const diagnostics=null;
    const dropContext=this._snapshotProjectMutationContext();
{helper}
        return queueDropMutation({{
          keySuffix:"clip",label:"drop clip",
          historyEntry:{{label:"drop"}},
          operation:{{type:"create_clip",fields:{{track_index:2,audio_lane_index:1,
            dual_drop:true,asset_id:"asset"}}}},
        }});
  }}
}}
const h=new Harness();const outcome=await h.run();
const failing=new Harness(true);const failedOutcome=await failing.run();
console.log(JSON.stringify({{sentBody,queuedIntent,outcome,
  failed:failedOutcome.ok===false,discarded:failing.discarded}}));
""")
    assert result["queuedIntent"]["operations"][0]["type"] == "create_clip"
    fields = result["sentBody"]["operations"][0]["fields"]
    assert fields["track_index"] == 7
    assert fields["audio_lane_index"] == 5
    assert result["outcome"]["ok"] is True
    assert result["failed"] is True
    assert result["discarded"] is True
    driver_branch = drop[drop.index("if (targetMotionDriverLane >= 0)"):
                         drop.index("const _findAsset")]
    assert "queueDropMutation({" in driver_branch
    assert "await fetch(" not in driver_branch
    assert 'type: "create_audio_track"' in drop
    assert 'type: "set_lane_count"' in drop


def test_ruler_drop_sends_one_batch_and_stamps_its_composite_scene():
    """Lane and entity creation stay in one slot and stamp one exact scene."""
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    drop = _method(widget, "_handleAssetDropWithinGesture", "_firstAvailableLane")
    helper = drop[drop.index("const queueDropMutation ="):
                  drop.index("// Zone-model drop targeting")]
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
const TRACK_TYPE={{VIDEO:"video",AUDIO:"audio"}};
const laneCountFor=(scene,type)=>type==="video"
  ? scene.video_lane_count : scene.audio_lane_count;
let releaseBatch;const batchGate=new Promise((resolve)=>{{releaseBatch=resolve;}});
const events=[];let sentBody=null;
class Harness {{
{rebase}
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();
    this._pendingHistoryEntryByMutationKey=new Map();this.stamps=[];
    this._latestHistoryOrderContext={{scenes:new Map([["scene",before]])}};}}
  _snapshotProjectMutationContext(){{return {{projectId:"project",sceneId:"scene"}};}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _stampHistoryPostSnapshot(entry,scene){{if(scene)this.stamps.push(scene.video_lane_count);}}
  _historyOrderedSceneForContext(context,sceneId){{return context?.scenes?.get(sceneId)||null;}}
  _recordHistoryOrderedScene(context,scene){{if(scene)context?.scenes?.set(scene.scene_id,scene);}}
  _schedulePostMutationSceneRefresh(){{}}
  _withMutationDiagnosticHeaders(init){{return init;}}
  async _runVersionedProjectMutation(path,init){{
    events.push("batch");sentBody=JSON.parse(init.body);await batchGate;
    return {{payload:{{scene:afterBatch,results:[
      {{type:"set_lane_count",lane_type:"video"}},
      {{type:"create_clip",clip:afterBatch.clips[0]}}
    ]}}}};
  }}
  runDrop(){{
    const dropSeq=1;const diagnostics=null;
    const dropContext=this._snapshotProjectMutationContext();
{helper}
    return queueDropMutation({{keySuffix:"clip",label:"drop clip",
      historyEntry:entry,
      laneCountOperations:[{{type:"set_lane_count",lane_type:"video",count:2}}],
      operation:{{type:"create_clip",fields:{{asset_id:"asset",track_index:1}}}},
    }});
  }}
}}
const before={{scene_id:"scene",video_lane_count:1,
  video_lane_configs:[{{name:"B"}}],clips:[]}};
const afterBatch={{scene_id:"scene",video_lane_count:2,
  video_lane_configs:[{{name:"B"}},{{name:"New"}}],
  clips:[{{clip_id:"created",track_index:1}}]}};
const entry={{sceneId:"scene",label:"drop",snapshot:before}};
const h=new Harness();const dropPromise=h.runDrop();
await new Promise((resolve)=>setTimeout(resolve,0));
const interloper=h._projectMutationQueue.enqueue({{key:"other",coalesce:false,
  run:async()=>{{events.push("interloper");}}}});
releaseBatch();await dropPromise;await interloper;
console.log(JSON.stringify({{sentBody,stamps:h.stamps,events}}));
""")
    assert [op["type"] for op in result["sentBody"]["operations"]] == [
        "set_lane_count", "create_clip"]
    assert result["sentBody"]["operations"][1]["fields"]["track_index"] == 1
    assert result["stamps"] == [2]
    assert result["events"] == ["batch", "interloper"]
    assert "laneCountOperations" in drop


def test_lane_rebase_covers_all_drop_lane_fields_without_membership_identity():
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    result = _run_node(f"""
class Harness {{
{rebase}
}}
const configs=[{{name:"B"}},{{name:"C"}},{{name:"Target"}}];
const counts={{video_lane_count:3,motion_driver_lane_count:3,
  audio_lane_count:3,reference_lane_count:3}};
const configLists={{video_lane_configs:configs,motion_driver_lane_configs:configs,
  audio_lane_configs:configs,reference_lane_configs:configs}};
const recipes=[{{lane_id:"B"}},{{lane_id:"C"}},{{lane_id:"Target"}}];
const authored={{scene_id:"scene",...counts,...configLists,
  reference_lane_recipes:recipes,
  clips:[{{clip_id:"video-member",track_index:2}},
    {{clip_id:"driver-member",track_index:2,role:"motion_driver"}}],
  audio_tracks:[{{track_id:"audio-member",lane_index:2}}],
  reference_items:[{{reference_item_id:"reference-member",lane_index:2}}]}};
const ordered={{scene_id:"scene",...counts,...configLists,
  reference_lane_recipes:recipes,
  clips:[{{clip_id:"video-member",track_index:1}},
    {{clip_id:"driver-member",track_index:1,role:"motion_driver"}}],
  audio_tracks:[{{track_id:"audio-member",lane_index:1}}],
  reference_items:[{{reference_item_id:"reference-member",lane_index:1}}]}};
const operations=[
  {{type:"create_clip",fields:{{track_index:2,dual_drop:true,audio_lane_index:2}}}},
  {{type:"create_clip",fields:{{track_index:2,role:"motion_driver",dual_drop:false}}}},
  {{type:"create_audio_track",fields:{{lane_index:2}}}},
  {{type:"create_reference_item",fields:{{lane_index:2}}}},
];
const equal=new Harness()._rebaseSceneMutationIntentForHistory(
  {{sceneId:"scene",operations}},ordered,authored).operations;
const structuralAuthored={{scene_id:"scene",video_lane_count:1,
  video_lane_configs:[{{name:"B",locked:false}}],clips:[]}};
const structuralOrdered={{scene_id:"scene",video_lane_count:2,
  video_lane_configs:[{{name:"A",locked:false}},{{locked:false,name:"B"}}],clips:[]}};
const structural=new Harness()._rebaseSceneMutationIntentForHistory(
  {{sceneId:"scene",operations:[{{type:"create_clip",fields:{{track_index:0}}}}]}},
  structuralOrdered,structuralAuthored).operations[0];
console.log(JSON.stringify({{equal,structural}}));
""")
    equal = result["equal"]
    assert equal[0]["fields"]["track_index"] == 2
    assert equal[0]["fields"]["audio_lane_index"] == 2
    assert equal[1]["fields"]["track_index"] == 2
    assert equal[2]["fields"]["lane_index"] == 2
    assert equal[3]["fields"]["lane_index"] == 2
    assert result["structural"]["fields"]["track_index"] == 1


def test_history_order_context_keeps_live_publishers_and_receipt_owners():
    widget = _source("web/js/editor_widget.js")
    contexts = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    cleanup = _method(widget, "_discardAmbiguousHistoryReservation", "_materializeHistoryOpposite")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
class Harness {{
{contexts}
{cleanup}
}}
const h=new Harness();h._projectMutationQueue=new ProjectMutationQueue();
let release;const gate=new Promise(resolve=>{{release=resolve;}});
const producer=h._beginHistoryOrderContext("undo",0);
const blocked=h._projectMutationQueue.enqueue({{key:"history",run:async()=>{{
  await gate;h._recordHistoryOrderedScene(producer,{{scene_id:"scene",value:"late"}});}}}});
const receiptEntry={{}};
h._markHistoryOrderContextAmbiguous(producer,"ambiguous","token",{{entry:receiptEntry}});
for(let i=1;i<20;i++)h._beginHistoryOrderContext("undo",i);
const head=h._latestHistoryOrderContext;
const reachable=()=>{{for(let current=head;current;current=current.parent)if(current===producer)return true;return false;}};
const liveReachable=reachable();release();await blocked;
const late=h._historyOrderedSceneForContext(head,"scene");
h._compactHistoryOrderContextChain(head);
const idleReachable=reachable();
h._discardAmbiguousHistoryReservation(receiptEntry);
const stillAmbiguous=h._historyOrderContextNeedsResolution(head,"ambiguous");
let depth=0;for(let current=head;current;current=current.parent)depth++;
console.log(JSON.stringify({{liveReachable,idleReachable,late,stillAmbiguous,depth}}));
""")
    assert result == {
        "liveReachable": True, "idleReachable": True,
        "late": {"scene_id": "scene", "value": "late"},
        "stillAmbiguous": False, "depth": 2,
    }


def test_history_order_context_chain_prunes_empty_settled_links_without_losing_lookups():
    """Sustained ambiguity must not grow one full scene clone per action.

    Queue idle normally truncates the chain, but an unresolved receipt
    deliberately preserves it. Pruning settled links stays lookup-preserving:
    a scene recorded deep in the chain is still found, and an ambiguity marked
    deeper still blocks resolution.
    """
    widget = _source("web/js/editor_widget.js")
    contexts = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    result = _run_node(f"""
class Harness {{
{contexts}
  constructor(){{this._latestHistoryOrderContext=null;}}
  _keyboardDebug(){{}}
}}
const h=new Harness();
for(let i=0;i<40;i+=1){{
  const context=h._beginHistoryOrderContext("undo",i);
  if(i===0)h._markHistoryOrderContextAmbiguous(context,"blocked","token");
  if(i===1)h._recordHistoryOrderedScene(context,{{scene_id:"deep",value:"kept"}});
}}
let depth=0;
for(let node=h._latestHistoryOrderContext;node;node=node.parent)depth+=1;
const head=h._latestHistoryOrderContext;
console.log(JSON.stringify({{depth,
  deepScene:h._historyOrderedSceneForContext(head,"deep"),
  blocked:h._historyOrderContextNeedsResolution(head,"blocked"),
  hasAmbiguity:h._historyOrderContextHasAmbiguity(head)}}));
""")
    assert result["depth"] <= 3, "empty settled links must not grow per action"
    # Pruning retains the original scene and receipt owners.
    assert result["deepScene"] == {"scene_id": "deep", "value": "kept"}
    assert result["blocked"] is True
    assert result["hasAmbiguity"] is True


def test_permanently_missing_receipt_clears_the_ambiguity_instead_of_wedging():
    """A token the server has never heard of must not block the scene forever.

    If the restore PUT is lost *and* never committed, and the server then
    restarts, the receipt lookup 404s for good. The poll exhausts, the mutation
    is refused, and because nothing clears an unresolvable ambiguity the same
    ~24s block repeats for every later edit on that scene. Only pressing Ctrl+Z
    again escapes it, which the failure message never suggests.

    A 404 is only treated as terminal after the whole budget is spent - an early
    404 can simply mean the server has not registered the pending receipt yet.
    """
    widget = _source("web/js/editor_widget.js")
    resolve = _method(
        widget, "_resolveHistoryOrderContextScene", "_captureProjectDependencies")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};
globalThis.rememberProjectVersionFromResponse=()=>{{}};
globalThis.fetchProjectJson=async()=>({{payload:{{}}}});
// Collapse the retry backoff; the schedule itself is not under test.
const realTimeout=globalThis.setTimeout;
globalThis.setTimeout=(fn)=>realTimeout(fn,0);
let polls=0;
globalThis.fetch=async()=>{{polls+=1;
  return {{ok:false,status:404,json:async()=>null}};}};
let refreshed=null;
class Harness {{
{resolve}
  _recordHistoryOrderedScene(){{}}
  _discardHistoryFutureReservation(){{return true;}}
  _finalizeCommittedHistoryAmbiguity(){{return true;}}
  _projectDirName(){{return "project";}}
  async _fetchScenes(options){{refreshed=options?.reason||"";}}
}}
const entry={{sceneId:"scene",restoreToken:"gone",label:"undo"}};
const context={{ambiguousScenes:new Map([["scene",
  {{entry,projectId:"project",restoreToken:"gone",operation:"undo"}}]]),
  scenes:new Map()}};
const h=new Harness();
let code="";let message="";
try {{ await h._resolveHistoryOrderContextScene(context,"scene"); }}
catch(error) {{ code=error.code||"";message=error.message||""; }}
console.log(JSON.stringify({{code,message,polls,refreshed,
  stillAmbiguous:context.ambiguousScenes.has("scene"),
  tokenRetained:Object.hasOwn(entry,"restoreToken")}}));
""")
    assert result["polls"] > 1, "the budget must still be spent before giving up"
    assert result["stillAmbiguous"] is False, "an unresolvable ambiguity must be cleared"
    assert result["code"] == "scene_history_receipt_unrecoverable"
    assert result["refreshed"], "authoritative state must be re-read, not assumed"
    assert result["tokenRetained"] is False


def test_unresolvable_lane_refusal_surfaces_its_message_and_frees_the_undo_stack():
    """A genuine ambiguity must refuse loudly and leave history usable.

    Two failure-path defects meet here: the queue's rejection handler builds a
    generic message and discards `error.message`, and the entry never reaches
    `_stampHistoryPostSnapshot`, so an unstamped entry is left at the top of the
    undo stack where it refuses every later Ctrl+Z.
    """
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
const notified=[];
globalThis.notifyError=(m)=>notified.push(m);
globalThis.notifyWarning=(m)=>notified.push(m);
const authored={{scene_id:"scene",video_lane_count:1,
  video_lane_configs:[{{}}],clips:[]}};
const ordered={{scene_id:"scene",video_lane_count:2,
  video_lane_configs:[{{}},{{}}],clips:[]}};
const historyEntry={{sceneId:"scene",label:"lane",snapshot:authored}};
let discarded=false;
class Harness {{
{rebase}
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();
    this._pendingHistoryEntryByMutationKey=new Map();}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _stampHistoryPostSnapshot(){{}}
  _historyOrderedSceneForContext(context,sceneId){{return context?.scenes?.get(sceneId)||null;}}
  _recordHistoryOrderedScene(){{}}
  _schedulePostMutationSceneRefresh(){{}}
  _deferProjectBackedRefresh(){{}}
  _discardUndoEntry(entry){{discarded=entry===historyEntry;return true;}}
}}
const context={{scenes:new Map([["scene",ordered]])}};
const intent={{sceneId:"scene",operations:[
  {{type:"update_lane_config",lane_type:"video",lane_index:0,fields:{{locked:true}}}}
]}};
let ran=false;let failure="";
const h=new Harness();h._latestHistoryOrderContext=context;
try {{
  await h._queueProjectMutation({{key:"lane",label:"lane",coalesce:false,
    refreshScenes:false,intent,historyEntry,
    run:async()=>{{ran=true;return {{payload:{{scene:ordered}}}};}}}});
}} catch(error) {{ failure=error.message; }}
await new Promise((resolve)=>setTimeout(resolve,0));
console.log(JSON.stringify({{failure,ran,discarded,notified}}));
""")
    assert result["ran"] is False, "the ambiguous edit must never reach the server"
    assert "could no longer be identified" in result["failure"]
    # The specific wording must survive the queue's generic fallback.
    assert result["notified"] == [result["failure"]]
    assert result["discarded"] is True, "unstampable entry must leave the undo stack"


def test_any_terminal_queue_failure_discards_its_exact_unstampable_entry():
    widget = _source("web/js/editor_widget.js")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
const failed={{sceneId:"scene",label:"same",snapshot:{{scene_id:"scene"}}}};
const newer={{sceneId:"scene",label:"same",snapshot:{{scene_id:"scene"}}}};
class Harness {{
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();
    this._pendingHistoryEntryByMutationKey=new Map();this._undoStack=[failed,newer];}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _historyOrderedSceneForContext(){{return null;}}
  _recordHistoryOrderedScene(){{}}
  _stampHistoryPostSnapshot(){{}}
  _schedulePostMutationSceneRefresh(){{}}
  _deferProjectBackedRefresh(){{}}
  _discardUndoEntry(entry){{const index=this._undoStack.indexOf(entry);
    if(index<0)return false;this._undoStack.splice(index,1);return true;}}
}}
const h=new Harness();let failure="";
try {{await h._queueProjectMutation({{key:"failed",label:"failed",coalesce:false,
  refreshScenes:false,historyEntry:failed,intent:{{sceneId:"scene",operations:[]}},
  run:async()=>{{throw new Error("ordinary 404");}}}});}}
catch(error){{failure=error.message;}}
await new Promise((resolve)=>setTimeout(resolve,0));
console.log(JSON.stringify({{failure,failedStill:h._undoStack.includes(failed),
  newerStill:h._undoStack.includes(newer),depth:h._undoStack.length}}));
""")
    assert result == {
        "failure": "ordinary 404",
        "failedStill": False,
        "newerStill": True,
        "depth": 1,
    }


def test_failed_consolidation_preserves_same_label_neighbors():
    widget = _source("web/js/editor_widget.js")
    methods = "\n".join([
        _method(widget, "_consolidateSelectedItemsToLane", "_removeLaneDeletingItems"),
        _method(widget, "_queueProjectMutation", "_runSceneMutation"),
        _method(widget, "_discardLastUndo", "_trimLocalLaneConfigs"),
    ])
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
class Harness {{
{methods}
  constructor(){{this.activeScene={{scene_id:"scene"}};this.activeSceneId="scene";
    this.projectDir="project";this._undoStack=[];this.selectedItems=[];
    this._projectMutationQueue=new ProjectMutationQueue();}}
  _selectedConsolidationItems(){{return [];}}
  _consolidationRefusal(){{return "";}}
  _pushUndo(label){{const entry={{label,sceneId:"scene",snapshot:this.activeScene}};
    this._undoStack.push(entry);this.candidate=entry;return entry;}}
  _claimHistoryPostSnapshotCapture(){{return this.candidate;}}
  _deferProjectBackedRefresh(){{}}
  _runSceneMutation(operations,options){{return this._queueProjectMutation({{...options,
    intent:{{sceneId:"scene",operations}},run:async()=>{{
      if(this.newer)this._undoStack.push(this.newer);
      throw new Error("ordinary 404");}}}});}}
}}
const outcomes=[];
for(const withNewer of [false,true]){{
  const h=new Harness();const valid={{label:"consolidate items",postSnapshot:{{}}}};
  h._undoStack.push(valid);
  if(withNewer)h.newer={{label:"consolidate items"}};
  await h._consolidateSelectedItemsToLane({{type:"clip",data:{{track_index:2}}}});
  outcomes.push({{valid:h._undoStack.includes(valid),failed:h._undoStack.includes(h.candidate),
    newer:!withNewer||h._undoStack.includes(h.newer)}});
}}
console.log(JSON.stringify(outcomes));
""")
    assert result == [{"valid": True, "failed": False, "newer": True}] * 2


def test_history_context_resolution_failure_cleans_exact_entry_and_context():
    widget = _source("web/js/editor_widget.js")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
const outcomes=[];
for(const code of ["scene_history_outcome_pending","scene_history_receipt_unrecoverable"]){{
  const entry={{sceneId:"scene",snapshot:{{scene_id:"scene"}}}};
  let discarded=false,sent=false;
  class Harness {{
{queue_mutation}
    constructor(){{this._projectMutationQueue=new ProjectMutationQueue();}}
    _historyOrderContextNeedsResolution(){{return true;}}
    _resolveHistoryOrderContextScene(){{throw Object.assign(new Error(code),{{code}});}}
    _discardUndoEntry(target){{discarded=target===entry;}}
    _deferProjectBackedRefresh(){{}}
  }}
  const h=new Harness();
  try{{await h._queueProjectMutation({{key:code,label:code,historyEntry:entry,
    historyOrderContext:{{}},intent:{{sceneId:"scene"}},
    run:async()=>{{sent=true;}}}});}}catch{{}}
  outcomes.push({{discarded,sent,contextRetained:!!entry._historyOrderContext}});
}}
console.log(JSON.stringify(outcomes));
""")
    assert result == [{"discarded": True, "sent": False, "contextRetained": False}] * 2


def test_response_lost_write_cannot_be_absorbed_into_next_history_entry():
    widget = _source("web/js/editor_widget.js")
    methods = "\n".join([
        _method(widget, "_queueProjectMutation", "_runSceneMutation"),
        _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies"),
        _method(widget, "_discardUndoEntry", "_trimLocalLaneConfigs"),
    ])
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.api={{apiURL:value=>value}};
let server={{scene_id:"scene",x:0,y:0}},reads=0,failRead=true;
globalThis.fetchProjectJson=async()=>{{reads++;if(failRead)throw new Error("GET lost");
  return {{payload:structuredClone(server)}};}};
class Harness {{
{methods}
  constructor(){{this._projectMutationQueue=new ProjectMutationQueue();this._undoStack=[];
    this._latestHistoryOrderContext={{scenes:new Map([["scene",structuredClone(server)]])}};}}
  _stampHistoryPostSnapshot(entry,scene){{if(scene)entry.postSnapshot=structuredClone(scene);}}
  _deferProjectBackedRefresh(){{}}
}}
const h=new Harness();const failed={{sceneId:"scene",snapshot:structuredClone(server)}};
h._undoStack.push(failed);
const intent={{sceneId:"scene",projectId:"captured-project",operations:[]}};
try{{await h._queueProjectMutation({{key:"X",historyEntry:failed,intent,refreshScenes:false,
  run:async()=>{{server.x=1;throw new Error("response lost");}}}});}}catch{{}}
let sent=false;const refused={{sceneId:"scene",snapshot:{{...server}}}};h._undoStack.push(refused);
try{{await h._queueProjectMutation({{key:"read-fails",historyEntry:refused,intent,refreshScenes:false,
  run:async()=>{{sent=true;}}}});}}catch{{}}
const stillNeedsRead=h._historyOrderContextNeedsResolution(h._latestHistoryOrderContext,"scene");
failRead=false;
const next={{sceneId:"scene",snapshot:{{scene_id:"scene",x:0,y:0}}}};h._undoStack.push(next);
await h._queueProjectMutation({{key:"Y",historyEntry:next,intent,refreshScenes:false,
  run:async()=>{{server.y=1;return {{payload:{{scene:structuredClone(server)}}}};}}}});
console.log(JSON.stringify({{sent,stillNeedsRead,reads,failedKept:h._undoStack.includes(failed),
  refusedKept:h._undoStack.includes(refused),before:next.snapshot,after:next.postSnapshot,
  failedStamped:!!failed.postSnapshot}}));
""")
    assert result == {
        "sent": False, "stillNeedsRead": True, "reads": 2,
        "failedKept": False, "refusedKept": False, "failedStamped": False,
        "before": {"scene_id": "scene", "x": 1, "y": 0},
        "after": {"scene_id": "scene", "x": 1, "y": 1},
    }


def test_lane_rebase_matches_a_lane_an_earlier_history_action_emptied():
    """Dragging an item back into the lane a queued undo just emptied.

    The authored anchor carries the item ids the lane held; the ordered lane is
    now empty. Matching item ids and config in parallel makes this structurally
    unmatchable - no clause can fire - so the most likely post-undo gesture
    refuses. The config is the lane's only surviving handle and must be reached
    as a fallback.
    """
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    result = _run_node(f"""
class Harness {{
{rebase}
}}
const h=new Harness();
const authored={{scene_id:"scene",video_lane_count:1,
  video_lane_configs:[{{name:"L"}}],clips:[{{clip_id:"c1",track_index:0}}]}};
const ordered={{scene_id:"scene",video_lane_count:2,
  video_lane_configs:[{{name:"A"}},{{name:"L"}}],clips:[]}};
const intent={{sceneId:"scene",operations:[
  {{type:"update_lane_config",lane_type:"video",lane_index:0,fields:{{locked:true}}}}
]}};
const rebased=h._rebaseSceneMutationIntentForHistory(intent,ordered,authored);
console.log(JSON.stringify({{rebased}}));
""")
    assert result["rebased"]["operations"][0]["lane_index"] == 1


def test_lane_rebase_prefers_durable_reference_lane_id_over_moved_items():
    """A structural rebase uses durable `lane_id`, even with duplicate configs."""
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    result = _run_node(f"""
class Harness {{
{rebase}
}}
const h=new Harness();
const authored={{scene_id:"scene",reference_lane_count:1,
  reference_lane_configs:[{{}}],reference_lane_recipes:[{{lane_id:"R"}}],
  reference_items:[]}};
const ordered={{scene_id:"scene",reference_lane_count:2,
  reference_lane_configs:[{{}},{{}}],
  reference_lane_recipes:[{{lane_id:"S"}},{{lane_id:"R"}}],reference_items:[]}};
const intent={{sceneId:"scene",operations:[
  {{type:"update_lane_config",lane_type:"reference",lane_index:0,fields:{{locked:true}}}}
]}};
const rebased=h._rebaseSceneMutationIntentForHistory(intent,ordered,authored);
console.log(JSON.stringify({{rebased}}));
""")
    assert result["rebased"]["operations"][0]["lane_index"] == 1


def test_lane_rebase_helper_retargets_and_refuses_ambiguous_lanes():
    """Unit-level cover for the helper itself.

    Kept alongside the wired test above: this one pins the helper's contract,
    that one proves production actually reaches it.
    """
    widget = _source("web/js/editor_widget.js")
    rebase = _method(widget, "_historyExpectedProjection", "_queueProjectMutation")
    result = _run_node(f"""
class Harness {{
{rebase}
}}
const h=new Harness();
const authored={{scene_id:"scene",video_lane_count:1,
  video_lane_configs:[{{name:"B",locked:false}}],clips:[]}};
const ordered={{scene_id:"scene",video_lane_count:2,
  video_lane_configs:[{{name:"A",locked:false}},{{name:"B",locked:false}}],clips:[]}};
const intent={{sceneId:"scene",operations:[
  {{type:"update_lane_config",lane_type:"video",lane_index:0,fields:{{locked:true}}}},
  {{type:"remove_lane",lane_type:"video",lane_index:0,item_policy:"require_empty"}},
  {{type:"set_lane_count",lane_type:"video",count:2}}
]}};
const rebased=h._rebaseSceneMutationIntentForHistory(intent,ordered,authored);
let ambiguity="";
try {{
  h._rebaseSceneMutationIntentForHistory(intent,
    {{...ordered,video_lane_configs:[{{}},{{}}]}},
    {{...authored,video_lane_configs:[{{}}]}});
}} catch(error) {{ ambiguity=error.message; }}
console.log(JSON.stringify({{rebased,original:intent,ambiguity}}));
""")
    operations = result["rebased"]["operations"]
    assert operations[0]["lane_index"] == 1
    assert operations[1]["lane_index"] == 1
    assert operations[2]["count"] == 3
    assert result["original"]["operations"][0]["lane_index"] == 0
    assert "could no longer be identified" in result["ambiguity"]


def test_parent_history_ambiguity_dominates_descendant_scene_and_history_apply_waits():
    widget = _source("web/js/editor_widget.js")
    contexts = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    queue_undo = _method(widget, "_runUndo", "_runUndoWithinGesture")
    queue_redo = _method(widget, "_runRedo", "_runRedoWithinGesture")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.performance={{now:()=>0}};globalThis.sessionDiagRecord=()=>{{}};
globalThis.notifyWarning=()=>{{}};globalThis.notifyProgress=()=>({{dismiss:()=>{{}}}});
class ContextHarness {{
{contexts}
}}
const parent={{scenes:new Map(),ambiguousScenes:new Map([["scene",{{}}]])}};
const child={{parent,scenes:new Map([["scene",{{scene_id:"scene",value:"too-new"}}]])}};
const c=new ContextHarness();
const dominance={{scene:c._historyOrderedSceneForContext(child,"scene"),
  needs:c._historyOrderContextNeedsResolution(child,"scene")}};
let activeParent=parent;
class Harness {{
{contexts}
{queue_undo}
{queue_redo}
{helpers}
  constructor(mode){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene"}};
    this.scenes=[this.activeScene];this._undoStack=[];this._redoStack=[];
    const entry={{sceneId:"scene",snapshot:{{scene_id:"scene"}},
      postSnapshot:{{scene_id:"scene"}},label:"A"}};
    (mode==="undo"?this._undoStack:this._redoStack).push(entry);
    this._projectMutationQueue=new ProjectMutationQueue();this._historyOperationSeq=0;
    this._historyStackRevision=0;this._sceneMutationInvalidationSeq=0;
    this._queuedHistoryOperationCount=0;this.events=[];this.mode=mode;}}
  _projectDirName(){{return "project";}} _keyboardDebug(){{}}
  _beginHistoryOrderContext(){{return {{parent:activeParent,scenes:new Map()}};}}
  _reserveHistoryOpposite(){{return {{_historyFuture:true}};}}
  _discardAmbiguousHistoryReservation(){{}} _trimUndoStack(){{}}
  _hasPendingProjectMutations(){{return this._projectMutationQueue.isBusy();}}
  async _resolveHistoryOrderContextScene(){{this.events.push("resolve");return {{scene_id:"scene"}};}}
  _activateGraphUndoSuppression(){{this.events.push("suppress");}}
  async _runUndoWithinGesture(){{this.events.push("run");}}
  async _runRedoWithinGesture(){{this.events.push("run");}}
  _schedulePostMutationSceneRefresh(){{}} _finishHistoryOperation(){{}}
}}
async function run(mode){{const h=new Harness(mode);
  await (mode==="undo"?h._queueUndoWithinGesture():h._queueRedoWithinGesture());
  return h.events;}}
const undo=await run("undo");const redo=await run("redo");
// Same wiring, unambiguous ancestor: the real gate must now decline to resolve.
activeParent={{scenes:new Map([["scene",{{scene_id:"scene"}}]]),
  ambiguousScenes:new Map()}};
const undoClean=await run("undo");
console.log(JSON.stringify({{dominance,undo,redo,undoClean}}));
""")
    assert result == {
        "dominance": {"scene": None, "needs": True},
        "undo": ["resolve", "suppress", "run"],
        "redo": ["resolve", "suppress", "run"],
        # The gate is the production one now, so a clean ancestor skips the wait
        # instead of the test asserting that a stub it forced to true was called.
        "undoClean": ["suppress", "run"],
    }


def test_image_asset_drop_uses_the_project_mutation_queue():
    """Run the image drop branch, do not grep it.

    Three source substrings cannot tell whether the queued operation carries the
    right fields, whether the guide the server returns is found again by id, or
    whether a failure propagates so the caller can roll back. `assert "await
    fetch(" not in image_branch` in particular is satisfied by any rewrite at
    all.
    """
    widget = _source("web/js/editor_widget.js")
    drop = _method(widget, "_handleAssetDropWithinGesture", "_firstAvailableLane")
    image_branch = drop[drop.index('if (asset.asset_type === "image")'):
                        drop.index('} else if (asset.asset_type === "video"')] + "}"
    result = _run_node(f"""
const emit=console.log;console.log=()=>{{}};
class Harness {{
  constructor(){{this.activeSceneId="scene";this.applied=[];this.sent=[];
    this.response={{payload:{{scene:{{guide_frames:[
      {{guide_id:"guide-1",frame_index:20,asset_id:"a1"}}]}}}}}};}}
  _seedFitDefaults(fields){{return {{...fields,fit_mode:"contain"}};}}
  _newLocalItemId(kind){{return `${{kind}}-1`;}}
  _defaultGuideStrength(){{return 0.5;}}
  _applyLocalCreateGuide(fields){{this.applied.push(fields);}}
  _renderSceneAfterLocalMutation(){{}}
  async _runSceneMutation(operations,options){{
    this.sent.push({{operations,options}});
    if(this.fail)throw new Error("mutation refused");
    return this.response;
  }}
  async runDrop(asset,frame,dropSeq,diagnostics){{
{image_branch}
  }}
}}
const h=new Harness();
await h.runDrop({{asset_type:"image",asset_id:"a1"}},12,7,null);
let propagated=false;
const failing=new Harness();failing.fail=true;
try {{ await failing.runDrop({{asset_type:"image",asset_id:"a1"}},12,7,null); }}
catch(error) {{ propagated=error.message==="mutation refused"; }}
emit(JSON.stringify({{sent:h.sent,applied:h.applied,propagated}}));
""")
    assert len(result["sent"]) == 1, "the drop must issue exactly one scene mutation"
    call = result["sent"][0]
    assert call["operations"] == [{
        "type": "create_guide",
        "fields": {
            "guide_id": "guide-1", "frame_index": 12, "asset_id": "a1",
            "source": "asset", "strength": 0.5, "fit_mode": "contain",
        },
    }]
    # The drop defers its own scenes refresh, and history ordering depends on it
    # not coalescing with a neighbouring drop.
    assert call["options"]["coalesce"] is False
    assert call["options"]["refreshScenes"] is False
    assert "drop:7:guide" in call["options"]["key"]
    # The server's authoritative frame_index must be re-applied, which only works
    # if the returned scene is searched by the locally minted guide_id.
    assert [entry["frame_index"] for entry in result["applied"]] == [12, 20]
    # A refusal has to reach the caller's catch, which owns the rollback.
    assert result["propagated"] is True


def test_nested_owner_write_records_only_its_executing_history_context():
    widget = _source("web/js/editor_widget.js")
    queue_mutation = _method(widget, "_queueProjectMutation", "_runSceneMutation")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.notifyError=()=>{{}};globalThis.notifyWarning=()=>{{}};
class Harness {{
{queue_mutation}
  constructor(){{this._sceneMutationInvalidationSeq=0;this._queueFetchSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();this._pendingHistoryEntryByMutationKey=new Map();}}
  _claimHistoryPostSnapshotCapture(){{return null;}}
  _stampHistoryPostSnapshot(){{}}
  _historyOrderedSceneForContext(){{return null;}}
  _recordHistoryOrderedScene(context,scene){{context.scenes.set(scene.scene_id,structuredClone(scene));}}
  _schedulePostMutationSceneRefresh(){{}}
}}
const h=new Harness();const parent={{scenes:new Map()}},child={{parent,scenes:new Map()}};
await h._projectMutationQueue.enqueue({{key:"history",coalesce:false,run:async(_i,_d,ownerToken)=>{{
  h._latestHistoryOrderContext=child;
  return await h._queueProjectMutation({{key:"nested",label:"nested",coalesce:false,
    refreshScenes:false,ownerToken,historyOrderContext:parent,intent:{{sceneId:"scene"}},
    run:async()=>({{payload:{{scene:{{scene_id:"scene",value:"parent"}}}}}})}});
}}}});
console.log(JSON.stringify({{parent:parent.scenes.get("scene")||null,
  child:child.scenes.get("scene")||null}}));
""")
    assert result == {
        "parent": {"scene_id": "scene", "value": "parent"},
        "child": None,
    }


def test_committed_ambiguous_history_receipt_consumes_exact_source_entry():
    widget = _source("web/js/editor_widget.js")
    history_context = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};globalThis.notifyInfo=()=>{{}};
globalThis.rememberProjectVersionFromResponse=()=>{{}};
globalThis.fetch=async()=>({{ok:true,json:async()=>({{status:"committed",
  scene:{{scene_id:"scene",value:"restored"}}}})}});
class Harness {{
{history_context}
  constructor(){{this._undoStack=[];this._redoStack=[];}}
  _projectDirName(){{return "project";}}
  _trimUndoStack(){{}}
}}
const h=new Harness();const entry={{sceneId:"scene",label:"edit",restoreToken:"token"}};
const later={{sceneId:"scene",label:"later"}};h._undoStack.push(entry,later);
const context={{scenes:new Map()}};
h._markHistoryOrderContextAmbiguous(context,"scene","token",{{
  operation:"undo",entry,sourceStack:h._undoStack}});
const scene=await h._resolveHistoryOrderContextScene(context,"scene",{{}});
console.log(JSON.stringify({{scene,labels:h._undoStack.map((value)=>value.label),
  token:Object.hasOwn(entry,"restoreToken"),ambiguous:context.ambiguousScenes.has("scene")}}));
""")
    assert result == {
        "scene": {"scene_id": "scene", "value": "restored"},
        "labels": ["later"],
        "token": False,
        "ambiguous": False,
    }


def _on_idle_closure(widget: str) -> str:
    """Lift the real `onIdle` callback body out of the widget constructor.

    The callback is the behavior under test and cannot be reached through
    `_method`, which only extracts class-body methods.
    """
    start = widget.index("onIdle: () => {")
    body_start = widget.index("{", start)
    depth = 0
    for index in range(body_start, len(widget)):
        if widget[index] == "{":
            depth += 1
        elif widget[index] == "}":
            depth -= 1
            if depth == 0:
                return widget[body_start:index + 1]
    raise AssertionError("unbalanced onIdle callback")


def test_unresolved_history_context_survives_queue_idle_until_receipt_resolution():
    """Drive the real queue-idle callback, not the file's text.

    The previous form asserted two substrings against the whole 21k-line source.
    One of them, `this._latestHistoryOrderContext = null;`, also matches the
    constructor's own initialiser, so it could not fail. Splice the actual
    callback into a harness constructor and let a real `ProjectMutationQueue`
    reach idle.
    """
    widget = _source("web/js/editor_widget.js")
    history_context = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    on_idle = _on_idle_closure(widget)
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
class Harness {{
{history_context}
  constructor(){{
    this._latestHistoryOrderContext=null;
    this._projectMutationQueue=new ProjectMutationQueue({{onIdle: () => {on_idle}}});
  }}
  _replayDeferredProjectBackedRefresh(){{}}
  async drain(){{
    await this._projectMutationQueue.enqueue(
      {{key:"edit",coalesce:false,run:async()=>({{ok:true}})}});
    await new Promise((resolve)=>setTimeout(resolve,0));
  }}
}}
const ambiguous={{scenes:new Map(),
  ambiguousScenes:new Map([["scene",{{restoreToken:"token"}}]])}};
const resolved={{scenes:new Map(),ambiguousScenes:new Map()}};
const h=new Harness();
h._latestHistoryOrderContext=ambiguous;
await h.drain();
const survivedWhileAmbiguous=h._latestHistoryOrderContext===ambiguous;
h._latestHistoryOrderContext=resolved;
await h.drain();
const clearedWhenResolved=h._latestHistoryOrderContext===null;
console.log(JSON.stringify({{survivedWhileAmbiguous,clearedWhenResolved}}));
""")
    assert result == {
        "survivedWhileAmbiguous": True, "clearedWhenResolved": True}


def test_committed_ambiguous_redo_materializes_undo_before_later_edit():
    widget = _source("web/js/editor_widget.js")
    history_context = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    push = _method(widget, "_pushUndo", "_claimHistoryPostSnapshotCapture")
    clear_redo = _method(widget, "_clearRedoForNewEdit", "_recordHistoryRefusal")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};globalThis.notifyInfo=()=>{{}};
globalThis.rememberProjectVersionFromResponse=()=>{{}};
globalThis.fetch=async()=>({{ok:true,json:async()=>({{status:"committed",
  scene:{{scene_id:"scene",value:"redone"}}}})}});
class Harness {{
{history_context}
{push}
{clear_redo}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene"}};
    this._undoStack=[];this._redoStack=[];this._maxUndoSteps=50;this._historyStackRevision=0;}}
  _projectDirName(){{return "project";}}
  _trimUndoStack(){{}}
  _materializeHistoryOpposite(stack,reservation,opposite){{
    const index=stack.indexOf(reservation);if(index<0)return false;
    stack.splice(index,1,opposite);return true;
  }}
}}
const h=new Harness();
const source={{sceneId:"scene",label:"A",restoreToken:"token",
  snapshot:{{scene_id:"scene",value:"redone-base"}}}};
const reservation={{_historyFuture:true,label:"A"}};
const opposite={{sceneId:"scene",label:"A",snapshot:{{scene_id:"scene",value:"before"}}}};
h._redoStack.push(source);h._undoStack.push(reservation);
const context={{scenes:new Map()}};
h._markHistoryOrderContextAmbiguous(context,"scene","token",{{operation:"redo",
  entry:source,sourceStack:h._redoStack,opposite,oppositeReservation:reservation,
  oppositeStack:h._undoStack}});
h._pushUndo("B");
await h._resolveHistoryOrderContextScene(context,"scene",{{}});
console.log(JSON.stringify({{undo:h._undoStack,redo:h._redoStack}}));
""")
    assert [entry["label"] for entry in result["undo"]] == ["A", "B"]
    assert result["undo"][0]["postSnapshot"] == {
        "scene_id": "scene", "value": "redone-base"}
    assert result["redo"] == []


def test_receipt_reconciliation_uses_restore_target_as_reverse_merge_base():
    widget = _source("web/js/editor_widget.js")
    history_base = _method(
        widget, "_rememberReconciledHistoryBase", "_resolveHistoryOrderContextScene")
    restore = _method(widget, "_restoreScene", "_setWidgetValue")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
class Harness {{
{history_base}
{restore}
  constructor(){{this.projectDir="project";this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",value:"after"}};this.scenes=[];}}
  _keyboardDebug(){{}} _renderTimeline(){{}} _renderViewportFrame(){{}}
  _setActiveScene(scene){{this.activeScene=scene;}}
}}
let calls=0;globalThis.fetch=async()=>{{calls+=1;
  if(calls===1)return{{ok:true,status:200,json:async()=>({{restore_token:"token"}})}};
  if(calls===2)throw new Error("response lost");
  return{{ok:true,status:200,json:async()=>({{status:"committed",
    scene:{{scene_id:"scene",value:"concurrent"}}}})}};
}};
const h=new Harness();const target={{scene_id:"scene",value:"before"}};
const restored=await h._restoreScene("scene",target,{{scene_id:"scene",value:"after"}});
console.log(JSON.stringify({{restored,base:h._historyBaseForRestoredScene(restored)}}));
""")
    assert result == {
        "restored": {"scene_id": "scene", "value": "concurrent"},
        "base": {"scene_id": "scene", "value": "before"},
    }


def test_plain_restore_response_still_uses_restore_target_as_reverse_merge_base():
    """A 200 restore body is not proof of the exact committed bytes either.

    The server answers an already-receipted token with the *current* scene
    (`routes.py` durable and in-memory short-circuits), and a transparent
    transport retry reaches that branch with no client-visible token. The
    reverse entry must therefore pin the authored restore target on the
    ordinary success path too, not just on the reconciliation paths.
    """
    widget = _source("web/js/editor_widget.js")
    history_base = _method(
        widget, "_rememberReconciledHistoryBase", "_resolveHistoryOrderContextScene")
    restore = _method(widget, "_restoreScene", "_setWidgetValue")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
class Harness {{
{history_base}
{restore}
  constructor(){{this.projectDir="project";this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",value:"after"}};this.scenes=[];}}
  _keyboardDebug(){{}} _renderTimeline(){{}} _renderViewportFrame(){{}}
  _setActiveScene(scene){{this.activeScene=scene;}}
}}
let calls=0;globalThis.fetch=async()=>{{calls+=1;
  if(calls===1)return{{ok:true,status:200,json:async()=>({{restore_token:"token"}})}};
  return{{ok:true,status:200,json:async()=>({{
    scene:{{scene_id:"scene",value:"current-with-later-work"}}}})}};
}};
const h=new Harness();const target={{scene_id:"scene",value:"before"}};
const restored=await h._restoreScene("scene",target,{{scene_id:"scene",value:"after"}});
console.log(JSON.stringify({{calls,restored,base:h._historyBaseForRestoredScene(restored)}}));
""")
    assert result["calls"] == 2, "expected the token POST then a single restore PUT"
    assert result["restored"] == {
        "scene_id": "scene", "value": "current-with-later-work"}
    assert result["base"] == {"scene_id": "scene", "value": "before"}


def test_refused_ambiguous_receipt_compensates_auxiliary_state_before_later_edit():
    widget = _source("web/js/editor_widget.js")
    history_context = _method(widget, "_beginHistoryOrderContext", "_captureProjectDependencies")
    push = _method(widget, "_pushUndo", "_claimHistoryPostSnapshotCapture")
    clear_redo = _method(widget, "_clearRedoForNewEdit", "_recordHistoryRefusal")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};globalThis.notifyInfo=()=>{{}};
globalThis.rememberProjectVersionFromResponse=()=>{{}};
globalThis.fetch=async()=>({{ok:true,json:async()=>({{status:"refused"}})}});
globalThis.fetchProjectJson=async()=>({{payload:{{scene_id:"scene",value:"unchanged"}}}});
class Harness {{
{history_context}
{push}
{clear_redo}
  constructor(){{this.activeSceneId="scene";this.activeScene={{scene_id:"scene"}};
    this._undoStack=[];this._redoStack=[];this.calls=[];this._maxUndoSteps=50;
    this._historyStackRevision=0;}}
  _projectDirName(){{return "project";}}
  _trimUndoStack(){{}}
  async _applyPromptIdentityChange(change){{this.calls.push(["prompt",change.type]);}}
  async _applyReferenceHistoryOperations(operations){{this.calls.push(["references",operations[0].type]);}}
  _discardHistoryFutureReservation(stack,reservation){{
    const index=stack.indexOf(reservation);if(index>=0)stack.splice(index,1);
  }}
}}
const h=new Harness();const reservation={{_historyFuture:true}};
const entry={{sceneId:"scene",label:"A",restoreToken:"token",
  inversePromptIdentityChange:{{type:"prompt-inverse"}},
  inverseReferenceOperations:[{{type:"reference-inverse"}}],
  _ambiguousAuxiliaryState:{{promptIdentityApplied:true,referencesApplied:true}}}};
h._redoStack.push(entry);h._undoStack.push(reservation);
const context={{scenes:new Map()}};
h._markHistoryOrderContextAmbiguous(context,"scene","token",{{operation:"redo",
  entry,sourceStack:h._redoStack,oppositeReservation:reservation,
  oppositeStack:h._undoStack}});
h._pushUndo("B");
const scene=await h._resolveHistoryOrderContextScene(context,"scene",{{}});
console.log(JSON.stringify({{scene,calls:h.calls,undo:h._undoStack.length,
  redo:h._redoStack.length,token:Object.hasOwn(entry,"restoreToken"),
  auxiliary:Object.hasOwn(entry,"_ambiguousAuxiliaryState")}}));
""")
    assert result == {
        "scene": {"scene_id": "scene", "value": "unchanged"},
        "calls": [["prompt", "prompt-inverse"],
                  ["references", "reference-inverse"]],
        "undo": 1,
        "redo": 0,
        "token": False,
        "auxiliary": False,
    }


def test_direct_retry_replaces_retained_ambiguous_future_reservation():
    widget = _source("web/js/editor_widget.js")
    reservations = _method(widget, "_trimUndoStack", "_captureProjectDependencies")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    helpers = _method(widget, "_recordHistoryRefusal", "_setWidgetValue")
    queue_url = (ROOT / "web/js/project_mutation_queue.js").as_uri()
    result = _run_node(f"""
const {{ProjectMutationQueue}}=await import({json.dumps(queue_url)});
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyInfo=()=>{{}};globalThis.notifyWarning=()=>{{}};
globalThis.notifyProgress=()=>({{update:()=>{{}},dismiss:()=>{{}}}});
globalThis.sessionDiagRecord=()=>{{}};
class Harness {{
{reservations}
{undo}
{redo}
{helpers}
  constructor(mode){{this.mode=mode;this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",value:"base"}};
    const entry={{sceneId:"scene",snapshot:{{scene_id:"scene",value:"target"}},
      postSnapshot:{{scene_id:"scene",value:"base"}},label:"A"}};
    this._undoStack=mode==="undo"?[entry]:[];this._redoStack=mode==="redo"?[entry]:[];
    this._maxUndoSteps=50;this._editorFocused=false;this._historyStackRevision=0;
    this._historyOperationSeq=0;this._queuedHistoryOperationCount=0;
    this._queuedHistoryNotification=null;this._sceneMutationInvalidationSeq=0;
    this._projectMutationQueue=new ProjectMutationQueue();this.calls=0;}}
  _keyboardDebug(){{}} _projectDirName(){{return "project";}}
  _hasPendingProjectMutations(){{return this._projectMutationQueue.isBusy();}}
  async _restoreScene(_id,target){{this.calls+=1;if(this.calls===1){{
    const error=new Error("unknown");error.restoreAmbiguous=true;error.restoreToken="token";
    throw error;}}return structuredClone(target);}}
  async _applyPromptIdentityChange(){{}} async _applyReferenceHistoryOperations(){{}}
  _schedulePostMutationSceneRefresh(){{}}
}}
async function run(mode){{const h=new Harness(mode);
  await (mode==="undo"?h._undo():h._redo());
  const pending={{undo:h._undoStack.length,redo:h._redoStack.length,
    futures:[...h._undoStack,...h._redoStack].filter((value)=>value._historyFuture).length}};
  await (mode==="undo"?h._undo():h._redo());
  return {{pending,done:{{undo:h._undoStack.length,redo:h._redoStack.length,
    futures:[...h._undoStack,...h._redoStack].filter((value)=>value._historyFuture).length}}}};
}}
console.log(JSON.stringify({{undo:await run("undo"),redo:await run("redo")}}));
""")
    assert result == {
        "undo": {
            "pending": {"undo": 1, "redo": 1, "futures": 1},
            "done": {"undo": 0, "redo": 1, "futures": 0},
        },
        "redo": {
            "pending": {"undo": 1, "redo": 1, "futures": 1},
            "done": {"undo": 1, "redo": 0, "futures": 0},
        },
    }


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
  _stampLatestHistoryPostSnapshot(){{return false;}}
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
  _stampLatestHistoryPostSnapshot(){{return false;}}
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
const entry={{sceneId:"scene",snapshot:{{value:"other"}},
  postSnapshot:{{value:"current"}},label:"attach",
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


def test_ambiguous_composite_restore_keeps_auxiliary_state_and_reconciles_once():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
const warnings=[];globalThis.notifyWarning=(message)=>warnings.push(message);
globalThis.notifyInfo=()=>{{}};
class Harness {{
{undo}
{redo}
  constructor(mode){{this.mode=mode;this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",value:"current"}};this._editorFocused=false;
    this._undoStack=[];this._redoStack=[];this.referenceCalls=0;
    this.identityCalls=0;this.restoreCalls=[];}}
  _keyboardDebug(){{}}
  async _applyReferenceHistoryOperations(){{this.referenceCalls+=1;}}
  async _applyPromptIdentityChange(){{this.identityCalls+=1;}}
  async _restoreScene(_id,target,_base,token){{this.restoreCalls.push(token||"");
    if(this.restoreCalls.length===1){{const error=new Error("response unknown");
      error.restoreAmbiguous=true;error.restoreToken=`${{this.mode}}-token`;throw error;}}
    this.activeScene=structuredClone(target);return structuredClone(target);
  }}
}}
const entry={{sceneId:"scene",snapshot:{{scene_id:"scene",value:"target"}},
  postSnapshot:{{scene_id:"scene",value:"base"}},label:"composite",
  referenceOperations:[{{type:"ref"}}],inverseReferenceOperations:[{{type:"ref-back"}}],
  promptIdentityChange:{{type:"identity"}},
  inversePromptIdentityChange:{{type:"identity-back"}}}};
const undoHarness=new Harness("undo");undoHarness._undoStack.push(structuredClone(entry));
await undoHarness._undo();const undoPending={{refs:undoHarness.referenceCalls,
  ids:undoHarness.identityCalls,token:undoHarness._undoStack[0].restoreToken}};
await undoHarness._undo();
const redoHarness=new Harness("redo");redoHarness._redoStack.push(structuredClone(entry));
await redoHarness._redo();const redoPending={{refs:redoHarness.referenceCalls,
  ids:redoHarness.identityCalls,token:redoHarness._redoStack[0].restoreToken}};
await redoHarness._redo();
console.log(JSON.stringify({{undoPending,undoDone:{{refs:undoHarness.referenceCalls,
  ids:undoHarness.identityCalls,calls:undoHarness.restoreCalls,
  undo:undoHarness._undoStack.length,redo:undoHarness._redoStack.length}},
  redoPending,redoDone:{{refs:redoHarness.referenceCalls,ids:redoHarness.identityCalls,
  calls:redoHarness.restoreCalls,undo:redoHarness._undoStack.length,
  redo:redoHarness._redoStack.length}},warnings}}));
""")
    assert result["undoPending"] == {
        "refs": 1, "ids": 1, "token": "undo-token"}
    assert result["undoDone"] == {
        "refs": 1, "ids": 1, "calls": ["", "undo-token"],
        "undo": 0, "redo": 1,
    }
    assert result["redoPending"] == {
        "refs": 1, "ids": 1, "token": "redo-token"}
    assert result["redoDone"] == {
        "refs": 1, "ids": 1, "calls": ["", "redo-token"],
        "undo": 1, "redo": 0,
    }
    assert any("still being confirmed" in message for message in result["warnings"])


def test_ambiguous_composite_partial_compensation_tracks_each_applied_leg():
    widget = _source("web/js/editor_widget.js")
    undo = _method(widget, "_undo", "_redo")
    redo = _method(widget, "_redo", "_restoreScene")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.notifyWarning=()=>{{}};globalThis.notifyInfo=()=>{{}};
class Harness {{
{undo}
{redo}
  constructor(mode){{this.mode=mode;this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene",value:"current"}};this._editorFocused=false;
    this._undoStack=[];this._redoStack=[];this.referenceCalls=[];
    this.identityCalls=[];this.restoreCalls=[];this.failedReferenceCompensation=false;}}
  _keyboardDebug(){{}}
  async _applyReferenceHistoryOperations(operations){{
    const type=operations[0]?.type||"";this.referenceCalls.push(type);
    if(type==="ref-back"&&!this.failedReferenceCompensation){{
      this.failedReferenceCompensation=true;throw new Error("reference compensation refused");
    }}
  }}
  async _applyPromptIdentityChange(change){{this.identityCalls.push(change?.type||"");}}
  async _restoreScene(_id,target,_base,token){{this.restoreCalls.push(token||"");
    if(this.restoreCalls.length===1){{const error=new Error("response unknown");
      error.restoreAmbiguous=true;error.restoreToken=`${{this.mode}}-token`;throw error;}}
    if(this.restoreCalls.length===2)throw new Error("restore refused");
    this.activeScene=structuredClone(target);return structuredClone(target);
  }}
}}
const entry={{sceneId:"scene",snapshot:{{scene_id:"scene",value:"target"}},
  postSnapshot:{{scene_id:"scene",value:"base"}},label:"composite",
  referenceOperations:[{{type:"ref"}}],inverseReferenceOperations:[{{type:"ref-back"}}],
  promptIdentityChange:{{type:"identity"}},
  inversePromptIdentityChange:{{type:"identity-back"}}}};
const run=async(mode)=>{{const h=new Harness(mode);
  (mode==="undo"?h._undoStack:h._redoStack).push(structuredClone(entry));
  if(mode==="undo"){{await h._undo();await h._undo();}}
  else{{await h._redo();await h._redo();}}
  const pending=(mode==="undo"?h._undoStack:h._redoStack)[0];
  const afterRefusal=structuredClone(pending._ambiguousAuxiliaryState);
  if(mode==="undo")await h._undo();else await h._redo();
  return {{afterRefusal,referenceCalls:h.referenceCalls,identityCalls:h.identityCalls,
    restoreCalls:h.restoreCalls,undo:h._undoStack.length,redo:h._redoStack.length}};
}};
console.log(JSON.stringify({{undo:await run("undo"),redo:await run("redo")}}));
""")
    for mode, expected_stacks in (
            ("undo", {"undo": 0, "redo": 1}),
            ("redo", {"undo": 1, "redo": 0})):
        row = result[mode]
        assert row["afterRefusal"]["referencesApplied"] is True
        assert row["afterRefusal"]["promptIdentityApplied"] is False
        assert row["referenceCalls"] == ["ref", "ref-back"]
        assert row["identityCalls"] == ["identity", "identity-back", "identity"]
        assert row["restoreCalls"] == ["", f"{mode}-token", ""]
        assert {"undo": row["undo"], "redo": row["redo"]} == expected_stacks


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
    assert result["options"] == {
        "recordUndo": False,
        "diagnostics": None,
        "attempt": 1,
    }
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
const h=new Harness(); let httpCalls=0;
globalThis.fetch=async()=>{{httpCalls += 1;
  if(httpCalls===1)return {{ok:true,status:200,json:async()=>({{restore_token:"http"}})}};
  return {{ok:false,status:500,json:async()=>{{throw new Error("no body");}}}};
}};
const base={{scene_id:"scene",attachments:[{{id:"chip"}}]}};
let http=""; try {{ await h._restoreScene("scene",{{scene_id:"scene"}},base); }}
catch(error) {{ http=error.message; }}
let networkCalls=0;
globalThis.fetch=async()=>{{networkCalls += 1;
  if(networkCalls===1)return {{ok:true,status:200,json:async()=>({{restore_token:"network"}})}};
  if(networkCalls===2)throw new Error("offline");
  return {{ok:true,status:200,json:async()=>({{status:"pending"}})}};
}};
  let network=""; let networkToken="";
  try {{ await h._restoreScene("scene",{{scene_id:"scene"}},base); }}
  catch(error) {{ network=error.message; networkToken=error.restoreToken||""; }}
  const retryCalls=[];
  globalThis.fetch=async(url,init={{}})=>{{retryCalls.push({{url,init}});
    return {{ok:true,status:200,json:async()=>({{scene:{{scene_id:"scene",value:"retried"}}}})}};
  }};
  await h._restoreScene("scene",{{scene_id:"scene"}},base,networkToken);
  const expiredCalls=[];
  globalThis.fetch=async(url,init={{}})=>{{expiredCalls.push({{url,init}});
    if(expiredCalls.length===1)return {{ok:false,status:400,json:async()=>({{
      error:"expired",code:"scene_restore_token_expired"}})}};
    if(expiredCalls.length===2)return {{ok:true,status:200,
      json:async()=>({{restore_token:"fresh"}})}};
    return {{ok:true,status:200,
      json:async()=>({{scene:{{scene_id:"scene",value:"restart-reconciled"}}}})}};
  }};
  await h._restoreScene("scene",{{scene_id:"scene"}},base,"expired");
const target={{scene_id:"scene",attachments:[]}}; let lostCalls=0;
globalThis.fetch=async()=>{{ lostCalls += 1;
  if(lostCalls===1)return {{ok:true,status:200,json:async()=>({{restore_token:"lost"}})}};
  if(lostCalls===2)throw new Error("lost response");
  return {{ok:true,status:200,json:async()=>({{status:"committed",scene:target}})}};
}};
let lost=""; try {{ await h._restoreScene("scene",target,base); }}
catch(error) {{ lost=error.message; }}
let bodyCalls=0;
globalThis.fetch=async()=>{{ bodyCalls += 1;
  if(bodyCalls===1)return {{ok:true,status:200,json:async()=>({{restore_token:"body"}})}};
  if(bodyCalls===2)return {{ok:true,status:200,json:async()=>{{throw new Error("body lost");}}}};
  return {{ok:true,status:200,json:async()=>({{status:"committed",scene:target}})}};
}};
let body=""; try {{ await h._restoreScene("scene",target,base); }}
catch(error) {{ body=error.message; }}
  console.log(JSON.stringify({{http,httpCalls,network,networkToken,networkCalls,
    retryCalls:retryCalls.map((call)=>({{method:call.init.method,
      token:JSON.parse(call.init.body).restore_token}})),
    expiredCalls:expiredCalls.map((call)=>call.init.method||"GET"),
    lost,lostCalls,body,bodyCalls,active:h.activeScene}}));
""")
    assert result == {
        "http": "Scene restore failed (500).",
        "httpCalls": 7,
        "network": "offline",
        "networkToken": "network",
        "networkCalls": 7,
        "retryCalls": [{"method": "PUT", "token": "network"}],
        "expiredCalls": ["PUT", "POST", "PUT"],
        "lost": "",
        "lostCalls": 3,
        "body": "",
        "bodyCalls": 3,
        "active": {"scene_id": "scene", "attachments": []},
    }


def test_scene_restore_rearms_graph_suppression_at_direct_and_reconciled_adoption():
    widget = _source("web/js/editor_widget.js")
    restore = _method(widget, "_restoreScene", "_setWidgetValue")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
class Harness {{
{restore}
  constructor(){{this.projectDir="project";this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene"}};this.scenes=[];this.events=[];}}
  _keyboardDebug(){{}}
  _activateGraphUndoSuppression(reason){{this.events.push(["suppress",reason]);}}
  _setActiveScene(scene){{this.events.push(["set",scene.value]);this.activeScene=scene;}}
  _renderTimeline(){{}} _renderViewportFrame(){{}}
}}
const h=new Harness();let calls=0;
globalThis.fetch=async()=>{{calls+=1;
  if(calls===1)return{{ok:true,status:200,json:async()=>({{restore_token:"direct"}})}};
  return{{ok:true,status:200,json:async()=>({{scene:{{scene_id:"scene",value:"direct"}}}})}};
}};
await h._restoreScene("scene",{{scene_id:"scene",value:"direct"}},{{scene_id:"scene"}});
calls=0;globalThis.fetch=async()=>{{calls+=1;
  if(calls===1)return{{ok:true,status:200,json:async()=>({{restore_token:"receipt"}})}};
  if(calls===2)throw new Error("response lost");
  return{{ok:true,status:200,json:async()=>({{status:"committed",
    scene:{{scene_id:"scene",value:"reconciled"}}}})}};
}};
await h._restoreScene("scene",{{scene_id:"scene",value:"reconciled"}},
  {{scene_id:"scene",value:"direct"}});
console.log(JSON.stringify(h.events));
""")
    assert result == [
        ["suppress", "editor-history-adopt"], ["set", "direct"],
        ["suppress", "editor-history-adopt"], ["set", "reconciled"],
    ]


def test_scene_restore_adopts_project_versions_from_direct_and_receipt_responses():
    widget = _source("web/js/editor_widget.js")
    restore = _method(widget, "_restoreScene", "_setWidgetValue")
    result = _run_node(f"""
globalThis.api={{apiURL:(value)=>value}};
globalThis.document={{activeElement:null}};globalThis.describeKeyboardDebugElement=()=>({{}});
const versions=[];globalThis.rememberProjectVersionFromResponse=(response,projectId)=>
  versions.push([response.version||"",projectId]);
class Harness {{
{restore}
  constructor(){{this.projectDir="project";this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene"}};this.scenes=[];}}
  _keyboardDebug(){{}} _setActiveScene(scene){{this.activeScene=scene;}}
  _renderTimeline(){{}} _renderViewportFrame(){{}}
}}
const h=new Harness();let calls=0;
globalThis.fetch=async()=>{{calls+=1;
  if(calls===1)return{{ok:true,status:200,version:"token-direct",
    json:async()=>({{restore_token:"direct"}})}};
  return{{ok:true,status:200,version:"restore-direct",
    json:async()=>({{scene:{{scene_id:"scene",value:"direct"}}}})}};
}};
await h._restoreScene("scene",{{scene_id:"scene",value:"direct"}},{{scene_id:"scene"}});
calls=0;globalThis.fetch=async()=>{{calls+=1;
  if(calls===1)return{{ok:true,status:200,version:"token-receipt",
    json:async()=>({{restore_token:"receipt"}})}};
  if(calls===2)throw new Error("response lost");
  return{{ok:true,status:200,version:"receipt-committed",
    json:async()=>({{status:"committed",scene:{{scene_id:"scene",value:"receipt"}}}})}};
}};
await h._restoreScene("scene",{{scene_id:"scene",value:"receipt"}},
  {{scene_id:"scene",value:"direct"}});
console.log(JSON.stringify(versions));
""")
    assert result == [
        ["token-direct", "project"], ["restore-direct", "project"],
        ["token-receipt", "project"], ["receipt-committed", "project"],
    ]


def test_scene_restore_conflict_refreshes_without_receipt_reconciliation_get():
    widget = _source("web/js/editor_widget.js")
    restore = _method(widget, "_restoreScene", "_setWidgetValue")
    result = _run_node(f"""
globalThis.document={{activeElement:null}};
globalThis.describeKeyboardDebugElement=()=>({{}});
globalThis.api={{apiURL:(value)=>value}};
const calls=[];
globalThis.fetch=async(url,init={{}})=>{{
  calls.push({{url,method:init.method||"GET"}});
  if(calls.length===1)return {{ok:true,status:200,json:async()=>({{restore_token:"token"}})}};
  return {{ok:false,status:409,json:async()=>({{
    error:"Scene changed elsewhere at clips[c].muted.",
    code:"scene_merge_conflict",conflicts:[{{path:"clips[c].muted"}}]
  }})}};
}};
class Harness {{
{restore}
  constructor(){{this.projectDir="project";this.activeSceneId="scene";
    this.activeScene={{scene_id:"scene"}};this.scenes=[];this.refreshes=[];}}
  _keyboardDebug(){{}}
  async _fetchScenes(options){{this.refreshes.push(options);return true;}}
  _setActiveScene(scene){{this.activeScene=scene;}}
  _renderTimeline(){{}} _renderViewportFrame(){{}}
}}
const h=new Harness();let error=null;
try{{await h._restoreScene("scene",{{scene_id:"scene",value:0}},
  {{scene_id:"scene",value:1}});}}catch(value){{error={{message:value.message,
  code:value.code,status:value.status}};}}
console.log(JSON.stringify({{calls,refreshes:h.refreshes,error}}));
""")
    assert len(result["calls"]) == 2
    assert result["calls"][1]["method"] == "PUT"
    assert result["error"] == {
        "message": "Scene changed elsewhere at clips[c].muted.",
        "code": "scene_merge_conflict", "status": 409,
    }
    assert result["refreshes"][0]["reason"] == (
        "scene_history_scene_merge_conflict")


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


def test_a_handle_labelled_attachment_renders_without_pill_chrome():
    """An inline attachment handle stays compact beside authored prose.

    It remains an atomic, keyboard-reachable chip—not a plain-text mention.
    """
    result = _run_chips_script("""
console.log(JSON.stringify({
  handle: mod.isHandleLabel("@KWoman"),
  described: mod.isHandleLabel("Reference — Korean Woman"),
  bareSigil: mod.isHandleLabel("@"),
  empty: mod.isHandleLabel(""),
}));
""")
    assert result["handle"] is True
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


def test_mention_ranking_preserves_source_metadata_without_gating_text():
    """Ranking must not narrow the row to `{handle, label}`.

    `value` is the semantic unit id or `physical:<population>:<member_id>` used
    to join a ranked handle back to its discovery row. `eligible` is still
    carried as attachment metadata, but it deliberately does not gate mention
    insertion: an unstaged or out-of-window handle is valid prose and produces
    the compiler's non-blocking `unresolved_handle_mention` warning.
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
    assert result == ["speaker"]


def test_candidate_compile_retries_fixed_snapshot_and_keeps_version_policy():
    widget = _source("web/js/editor_widget.js")
    result = _run_node(_prompt_compile_test_support(widget) + """
const api = { apiURL: value => value };
const results = [];
for (const [sent, actual, during] of [["v1", "v2", ""], ["v9", "v2", ""],
                                     ["v1", "v2", "v3"]]) {
  resetProjectVersion("project", sent);
  const host = new CompileSupport(); host.activeSceneId = "scene";
  const body = {base_modified_at: sent, scene: {text: "draft"}, selection_start: 4,
    window_end: 12, frame_constraint: {step: 4}, copy_plan_for: {attachment_id: "a"}};
  const requests = [];
  globalThis.fetch = async (_url, init) => {
    requests.push(JSON.parse(init.body));
    if (requests.length === 1) {
      body.scene.text = "newer typing";
      body.window_end = 99;
      if (during) rememberProjectVersion("project", during);
      return new Response(JSON.stringify({code: "project_version_conflict",
        actual_modified_at: actual, project: {project_id:"project",modified_at:actual}}),
        {status:409});
    }
    return new Response(JSON.stringify({copy_plan:{lines:["ok"]}}));
  };
  const value = await host._requestPromptContextCompile("project","scene",body);
  results.push({requests, version:getProjectVersion("project"), ok:value.response.ok});
}
console.log(JSON.stringify(results));
""")
    for row, expected in zip(result, ("v2", "v2", "v3")):
        first, retry = row["requests"]
        assert retry == {**first, "base_modified_at": expected}
        assert retry["scene"]["text"] == "draft"
        assert row["version"] == expected
        assert row["ok"]


def test_candidate_compile_bounds_retry_and_cancels_superseded_work():
    widget = _source("web/js/editor_widget.js")
    result = _run_node(_prompt_compile_test_support(widget) + """
const api = { apiURL: value => value };
const rows = [];
for (const cancel of ["none", "edit", "scene", "project", "destroy"]) {
  resetProjectVersion("project", "v1");
  const host = new CompileSupport(); host.activeSceneId = "scene";
  let current = true, calls = 0;
  globalThis.fetch = async () => {
    calls++;
    if (cancel === "edit") current = false;
    if (cancel === "scene") host.activeSceneId = "other";
    if (cancel === "project") host._projectDirName = () => "other";
    if (cancel === "destroy") host._destroyed = true;
    return new Response(JSON.stringify({code:"project_version_conflict",
      actual_modified_at:"v2",project:{project_id:"project",modified_at:"v2"}}),{status:409});
  };
  const value = await host._requestPromptContextCompile("project","scene",
    {base_modified_at:"v1"}, () => current);
  rows.push({cancel,calls,discarded:value===null,version:getProjectVersion("project")});
}
console.log(JSON.stringify(rows));
""")
    assert result[0] == {"cancel": "none", "calls": 2, "discarded": False, "version": "v2"}
    for row in result[1:]:
        assert row["calls"] == 1
        assert row["discarded"] is True
        # Obsolete work has no retry/application authority, but its conflict
        # still heals the shared version map for the trailing owner.
        assert row["version"] == "v2"


def test_exhausted_preview_conflict_fails_window_and_settles_scene_sibling():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for exhausted preview-conflict coverage")
    widget = _source("web/js/editor_widget.js")
    preview = _method_body(
        widget[widget.index("\n    _previewPromptContextCandidate("):],
        "_previewPromptContextCandidate", marker="Math.max(0, Number(delay) || 0)")
    scene = _method_body(
        widget[widget.index("\n    _previewPromptContextScenePayload("):],
        "_previewPromptContextScenePayload", marker="refreshProjections?.();")
    settle = _method_body(
        widget[widget.index("\n    _clearPromptStaleVisualTimerIfSettled("):],
        "_clearPromptStaleVisualTimerIfSettled", marker="return true;")
    result = _run_node(_prompt_compile_test_support(widget) + """
const api = {apiURL: value => value};
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const resolvePromptCandidateSelection = () => ({selectionStart: 10, selectionEnd: 20});
const timers = [];
globalThis.setTimeout = (fn, ms) => {
  const timer = {fn, ms, cancelled: false}; timers.push(timer); return timer;
};
globalThis.clearTimeout = (timer) => { if (timer) timer.cancelled = true; };
resetProjectVersion("project", "v1");
let calls = 0;
globalThis.fetch = async () => {
  calls++;
  return new Response(JSON.stringify({code: "project_version_conflict",
    error: "project_version_conflict", actual_modified_at: "v2",
    project: {project_id: "project", modified_at: "v2"}}), {status: 409});
};
class Subject extends CompileSupport {
""" + settle + """
""" + scene + """
""" + preview + """
  constructor() {
    super(); this.activeSceneId = "scene";
    this.activeScene = {duration_frames: 100}; this.totalFrames = 100;
    this.selectionStart = 10; this.selectionEnd = 20;
    this._promptContextPreviewToken = 0; this._promptContextScenePayloadToken = 0;
    this._promptContextCandidateCache = {_candidate_scene_id: "scene",
      prompt: "last good", attachment_previews: {a: "kept"},
      setup_manifest: {slots: [1]}, _stale: false};
    this._promptContextScenePayloadCache = {_candidate_scene_id: "scene",
      attachment_previews: {dormant: "old"}, _stale: false};
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return {contextStart: 10, contextEnd: 20}; }
  _promptScenePayload() { return this._promptContextScenePayloadCache; }
  _promptCompileRequestBody() { return {base_modified_at: getProjectVersion("project")}; }
  _promptProjectionSubset(payload) { return payload; }
  _renderTimeline() {}
}
const subject = new Subject();
subject._previewPromptContextCandidate({}, 0);
await timers.find((timer) => timer.ms === 0 && !timer.cancelled).fn();
for (let index = 0; index < 40; index++) await Promise.resolve();
const grace = timers.find((timer) => timer.ms === 300);
console.log(JSON.stringify({calls, candidate: subject._promptContextCandidateCache,
  scene: subject._promptContextScenePayloadCache, graceCancelled: grace.cancelled}));
""")
    assert result["calls"] == 4
    assert result["scene"] is None
    assert result["graceCancelled"] is True
    assert result["candidate"]["_failed"] is True
    assert result["candidate"]["_stale"] is True
    assert result["candidate"]["attachment_previews"] == {"a": "kept"}
    assert result["candidate"]["setup_manifest"] == {"slots": [1]}
    error = result["candidate"]["errors"][0]
    assert error["code"] == "project_version_conflict"
    assert "project changed while it was compiling" in error["message"]
    assert error["message"] != "project_version_conflict"


def test_failed_candidate_retains_whole_same_scene_payload_only():
    widget = _source("web/js/editor_widget.js")
    result = _run_node(_prompt_compile_test_support(widget) + """
const host = new CompileSupport();
const previous = {_candidate_scene_id:"scene", prompt:"last good",
  attachment_capability_projections:{a:[{state:"emitted"}]},
  setup_manifest:{slots:[1]}, managed_speaker_subject_ids:["s"],
  execution_window:{render_start:0}, future_projection_field:{kept:true},
  errors:[],warnings:[]};
host._promptContextCandidateCache = previous;
const diagnostic = {code:"project_version_conflict"};
console.log(JSON.stringify({same:host._failedPromptContextCandidate("scene",diagnostic),
  other:host._failedPromptContextCandidate("other",diagnostic),previous}));
""")
    same = result["same"]
    assert same == {**result["previous"], "errors": [{"code": "project_version_conflict"}],
                    "warnings": [], "_stale": True, "_stale_visual": True, "_failed": True}
    assert result["previous"]["errors"] == []
    assert result["other"] == {
        "_candidate_scene_id": "other", "errors": [{"code": "project_version_conflict"}],
        "warnings": [], "_stale": True, "_stale_visual": True, "_failed": True}


def test_compile_callers_recheck_ownership_after_helper_return():
    widget = _source("web/js/editor_widget.js")
    methods = "\n".join([
        "async " + _method_body(widget[widget.index("\n    async _promptCopyPlan("):],
            "_promptCopyPlan"),
        _method_body(widget[widget.index("\n    _previewPromptContextScenePayload("):],
            "_previewPromptContextScenePayload"),
        _method_body(widget[widget.index("\n    _previewPromptContextCandidate("):],
            "_previewPromptContextCandidate"),
    ])
    result = _run_node(_prompt_compile_test_support(widget) + """
const api = {apiURL: value => value};
const PROMPT_STALE_VISUAL_DELAY_MS = 300;
const resolvePromptCandidateSelection = () => ({selectionStart:0,selectionEnd:100});
const timers = [];
globalThis.setTimeout = fn => {timers.push(fn);return timers.length;};
globalThis.clearTimeout = () => {};
globalThis.fetch = async () => new Response(JSON.stringify({
  prompt:"OLD",copy_plan:{lines:["OLD"]},attachment_capability_projections:[]}));
class Subject extends CompileSupport {
""" + methods + """
  constructor() {
    super(); this.activeSceneId="scene";this.activeScene={duration_frames:100};
    this._promptContextPreviewToken=0; this._promptContextScenePayloadToken=0;
  }
  _selectionContextRange() {return null;}
  _promptScenePayload() {return null;}
  _promptCompileRequestBody() {return {};}
  _clearPromptStaleVisualTimerIfSettled() {}
  _renderTimeline() {}
  _promptProjectionSubset(payload) {return payload;}
  async _requestPromptContextCompile(...args) {
    const result = await super._requestPromptContextCompile(...args);
    // An external microtask can run after the helper's final ownership check.
    queueMicrotask(() => {
      this._promptContextPreviewToken++;
      this._promptContextScenePayloadToken++;
      this._promptContextCandidateCache={prompt:"NEW",_stale:true};
      this._promptContextScenePayloadCache={prompt:"NEW",_stale:true};
    });
    return result;
  }
}
const rows=[];
for(const caller of ["copy","window","scene"]) {
  const host=new Subject();
    if(caller==="copy") {
      const value=await host._promptCopyPlan("a","c");
      rows.push({caller,refused:value?.refused||null});
  } else if(caller==="window") {
    host._previewPromptContextScenePayload=()=>{};
    host._previewPromptContextCandidate({},0);
    await timers.pop()();
    rows.push({caller,cache:host._promptContextCandidateCache});
  } else {
    host._previewPromptContextScenePayload({dirName:"project",sceneId:"scene",
      candidate:{duration_frames:100},windowStart:10,windowEnd:20});
    for(let i=0;i<30;i++) await Promise.resolve();
    rows.push({caller,cache:host._promptContextScenePayloadCache});
  }
}
console.log(JSON.stringify(rows));
""")
    assert result == [
        {"caller": "copy",
         "refused": "Project changed; request Copy Plan again."},
        {"caller": "window", "cache": {"prompt": "NEW", "_stale": True}},
        {"caller": "scene", "cache": {"prompt": "NEW", "_stale": True}},
    ]


def test_copy_plan_action_is_host_owned_across_panel_remount_and_has_request_ids():
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    method = _method_body(
        widget, "_promptCopyPlan", marker="this._promptCopyPlanInFlight = null")
    busy_method = _method_body(
        widget, "_isPromptCopyPlanBusy",
        marker="return !!this._promptCopyPlanInFlight;")
    result = _run_node("""
class Subject {
  async """ + method + """
  """ + busy_method + """
  constructor() {
    this.activeSceneId="scene"; this.activeScene={duration_frames:24};
    this.totalFrames=24; this._promptContextPreviewToken=1; this.pending=[];
    this.requests=[];
  }
  _projectDirName() { return "project"; }
  _selectionContextRange() { return {contextStart:0,contextEnd:24}; }
  _promptCompileRequestBody() { return {base_modified_at:"v1"}; }
  _requestPromptContextCompile(_project,_scene,body,_current,meta) {
    this.requests.push({body:structuredClone(body),meta});
    return new Promise(resolve=>this.pending.push(resolve));
  }
}
const host=new Subject();
const first=host._promptCopyPlan("a","c");
const second=await host._promptCopyPlan("a","c");
const busyWhileMounted=host._isPromptCopyPlanBusy();
// A remounted panel calls the same host method; it cannot reset host ownership.
const remountAttempt=await host._promptCopyPlan("a","c");
host.pending.shift()({response:{ok:true},payload:{copy_plan:{lines:[]}}});
await first;
const busyAfter=host._isPromptCopyPlanBusy();
const third=host._promptCopyPlan("a","c");
host.pending.shift()({response:{ok:true},payload:{copy_plan:{lines:[]}}});
await third;
console.log(JSON.stringify({requests:host.requests,second,remountAttempt,
  busyWhileMounted,busyAfter}));
""")
    assert [row["meta"]["requestId"] for row in result["requests"]] == [
        "copy-plan-1", "copy-plan-2"]
    assert all(row["meta"]["purpose"] == "copy-plan"
               for row in result["requests"])
    assert result["second"] == {
        "refused": "A Copy Plan request is already running."}
    assert result["remountAttempt"] == result["second"]
    assert result["busyWhileMounted"] is True
    assert result["busyAfter"] is False
    menu = panel[panel.index('label: "Copy with handles"'):]
    assert "disabled: host._isPromptCopyPlanBusy?.() === true" in menu
    assert "a Copy Plan request is already running" in menu


def test_coordinator_obsolete_409_heals_without_retry_then_trailing_owns_retry():
    widget = _source("web/js/editor_widget.js")
    coordinator_url = (ROOT / "web/js/prompt_compile_coordinator.js").as_uri()
    result = _run_node(_prompt_compile_test_support(widget) + f"""
const {{createPromptCompileCoordinator}}=await import({json.dumps(coordinator_url)});
const api={{apiURL:value=>value}}; resetProjectVersion("project","v1");
let releaseA; const requests=[];
globalThis.fetch=async (_url,init)=>{{
  const row={{body:JSON.parse(init.body),headers:init.headers}}; requests.push(row);
  if(requests.length===1) return await new Promise(resolve=>{{releaseA=resolve;}});
  if(requests.length===2) return new Response(JSON.stringify({{
    code:"project_version_conflict",actual_modified_at:"v3",
    project:{{project_id:"project",modified_at:"v3"}}}}),{{status:409}});
  return new Response(JSON.stringify({{prompt:"B"}}));
}};
class Subject extends CompileSupport {{
  constructor() {{
    super(); this.activeSceneId="scene";
    this._promptCompileCoordinator=createPromptCompileCoordinator(
      (request)=>this._requestPromptContextCompile(request.projectId,request.sceneId,
        request.body,request.isCurrent,{{purpose:request.purpose,
          requestId:request.requestId}}));
  }}
}}
const host=new Subject();
const a=host._queuePromptContextCompile("windowed-preview","project","scene",
  {{base_modified_at:"v1",scene:{{text:"A"}}}},()=>true);
await Promise.resolve(); await Promise.resolve();
const b=host._queuePromptContextCompile("windowed-preview","project","scene",
  {{base_modified_at:"v1",scene:{{text:"B"}}}},()=>true);
releaseA(new Response(JSON.stringify({{
  code:"project_version_conflict",actual_modified_at:"v2",
  project:{{project_id:"project",modified_at:"v2"}}}}),{{status:409}}));
const values=await Promise.all([a,b]);
console.log(JSON.stringify({{requests,version:getProjectVersion("project"),
  aDiscarded:values[0]===null,bPrompt:values[1]?.payload?.prompt}}));
""")
    assert result["aDiscarded"] is True
    assert result["bPrompt"] == "B"
    assert result["version"] == "v3"
    assert [row["body"]["scene"]["text"] for row in result["requests"]] == [
        "A", "B", "B"]
    assert [row["headers"]["X-Sonder-Prompt-Request-Id"]
            for row in result["requests"]] == ["prompt-1", "prompt-2", "prompt-2"]
    assert [row["headers"]["X-Sonder-Prompt-Attempt"]
            for row in result["requests"]] == ["1", "1", "2"]
    assert result["requests"][2]["body"]["base_modified_at"] == "v3"


def test_prompt_draft_revisions_and_targeted_intents_survive_rapid_edits():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{promptEditFields, promptDraftKey, updatePromptDraft, savePromptDraft, rebasePromptDraft}} = await import({json.dumps(url)});
const base = {{prompt_id:"section", channel_docs:{{visual:{{text:"old"}},speech:{{text:"old speech"}}}}, attachments:[]}};
const first = structuredClone(base); first.channel_docs.visual.text="A";
const second = structuredClone(first); second.channel_docs.visual.text="B";
const drafts=new Map(), key=promptDraftKey("p","s","section"), calls=[];
updatePromptDraft(drafts,key,base,first);
let finish;
const save = async (value,before) => {{ calls.push([value,before]); if(calls.length===1) return await new Promise(r=>finish=r); return true; }};
const pending=savePromptDraft(drafts,key,save);
updatePromptDraft(drafts,key,base,second);
const later=savePromptDraft(drafts,key,save);
finish(true); await pending; await later;
const clean=!drafts.has(key);
updatePromptDraft(drafts,key,second,first);
await savePromptDraft(drafts,key,async()=>false);
const current=structuredClone(second); current.channel_docs.speech.text="external";
const restored=rebasePromptDraft(drafts.get(key),current);
const intent=promptEditFields(drafts.get(key).base,{{channel_docs:restored.channel_docs,attachments:restored.attachments}});
console.log(JSON.stringify({{clean,calls,refused:drafts.get(key).error,restored,intent,
 distinct:key!==promptDraftKey("p2","s","section") && key!==promptDraftKey("p","s2","section")}}));
""")
    assert result["clean"] and result["distinct"]
    assert len(result["calls"]) == 2
    assert result["calls"][1][1]["channel_docs"]["visual"]["text"] == "A"
    assert result["calls"][1][0]["channel_docs"]["visual"]["text"] == "B"
    assert result["refused"]
    assert result["restored"]["channel_docs"]["visual"]["text"] == "A"
    assert result["restored"]["channel_docs"]["speech"]["text"] == "external"
    assert list(result["intent"]["prompt_edit"]["documents"]) == ["visual"]


def test_newer_prompt_draft_retries_even_when_the_pending_save_is_refused():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{updatePromptDraft,savePromptDraft}}=await import({json.dumps(url)});
const drafts=new Map(), key="draft", calls=[];
updatePromptDraft(drafts,key,{{text:"base"}},{{text:"A"}});
let finish;
const save=async(value,base)=>{{calls.push([value.text,base.text]);
  if(calls.length===1)return await new Promise(resolve=>finish=resolve);return true;}};
const first=savePromptDraft(drafts,key,save);
updatePromptDraft(drafts,key,{{text:"base"}},{{text:"B"}});
const second=savePromptDraft(drafts,key,save);
finish(false);
const outcomes=await Promise.all([first,second]);
console.log(JSON.stringify({{calls,outcomes,clean:!drafts.has(key)}}));
""")
    assert result == {"calls": [["A", "base"], ["B", "base"]],
                      "outcomes": [True, True], "clean": True}



def test_prompt_draft_adopts_authoritative_ack_without_losing_newer_text():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{updatePromptDraft,savePromptDraft}}=await import({json.dumps(url)});
const drafts=new Map(), key="draft";
const base={{channel_docs:{{visual:{{text:"old"}}}},attachments:[]}};
const submitted={{channel_docs:{{visual:{{text:"A"}}}},attachments:[{{attachment_id:"chip",capabilities:[{{capability_id:"summary",kind:"summary",enabled:false}}]}}]}};
const acknowledged={{channel_docs:{{visual:{{text:"A",nodes:[]}}}},attachments:[{{attachment_id:"chip",capabilities:[{{capability_id:"summary",kind:"summary",channel_key:"",placement:"",config:{{}},enabled:false}}]}}]}};
const newer=structuredClone(submitted); newer.channel_docs.visual.text="B";
updatePromptDraft(drafts,key,base,submitted);
let finish;
const pending=savePromptDraft(drafts,key,async()=>await new Promise(resolve=>finish=resolve));
updatePromptDraft(drafts,key,base,newer);
finish({{status:"acknowledged",value:acknowledged}});
await pending;
const row=drafts.get(key);
console.log(JSON.stringify({{base:row.base,value:row.value,error:row.error}}));
""")
    assert result["base"] == {
        "channel_docs": {"visual": {"text": "A", "nodes": []}},
        "attachments": [{"attachment_id": "chip", "capabilities": [{
            "capability_id": "summary", "kind": "summary", "channel_key": "",
            "placement": "", "config": {}, "enabled": False,
        }]}],
    }
    assert result["value"]["channel_docs"]["visual"]["text"] == "B"
    assert result["value"]["attachments"] == result["base"]["attachments"]
    assert result["error"] == ""



def test_prompt_ack_callback_identifies_the_submitted_snapshot_for_live_settlement():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{updatePromptDraft,savePromptDraft}}=await import({json.dumps(url)});
const drafts=new Map(),key="draft";
const base={{channel_docs:{{visual:{{text:"old"}}}},attachments:[]}};
const submitted={{channel_docs:{{visual:{{text:"A"}}}},attachments:[]}};
const acknowledged={{channel_docs:{{visual:{{text:"A",nodes:[]}}}},attachments:[]}};
updatePromptDraft(drafts,key,base,submitted);
let callback;
await savePromptDraft(drafts,key,async()=>({{status:"acknowledged",value:acknowledged}}),{{
  onAcknowledge:(value)=>callback=value,
}});
console.log(JSON.stringify(callback));
""")
    assert result["submitted"] == {
        "channel_docs": {"visual": {"text": "A"}}, "attachments": [],
    }
    assert result["baseline"] == result["value"] == {
        "channel_docs": {"visual": {"text": "A", "nodes": []}},
        "attachments": [],
    }

def test_prompt_draft_no_op_settles_an_a_to_b_to_a_revision():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{updatePromptDraft,savePromptDraft}}=await import({json.dumps(url)});
const drafts=new Map(),key="draft",base={{channel_docs:{{visual:{{text:"A"}}}},attachments:[]}};
updatePromptDraft(drafts,key,base,{{channel_docs:{{visual:{{text:"B"}}}},attachments:[]}});
updatePromptDraft(drafts,key,base,structuredClone(base));
const ok=await savePromptDraft(drafts,key,async()=>({{status:"no-op"}}));
console.log(JSON.stringify({{ok,clean:!drafts.has(key)}}));
""")
    assert result == {"ok": True, "clean": True}



def test_prompt_recovery_resolves_only_the_contested_record_and_can_dismiss():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{dismissPromptDraftError,resolvePromptDraftRecord,updatePromptDraft}}=await import({json.dumps(url)});
const drafts=new Map(),key="draft";
const base={{channel_docs:{{visual:{{text:"old"}},speech:{{text:"old speech"}}}},attachments:[{{attachment_id:"a",config:{{text:"old"}}}}]}};
const local={{channel_docs:{{visual:{{text:"mine"}},speech:{{text:"local speech"}}}},attachments:[{{attachment_id:"a",config:{{text:"mine"}}}},{{attachment_id:"b",config:{{text:"local chip"}}}}]}};
const row=updatePromptDraft(drafts,key,base,local);
row.error="conflict"; row.conflict={{record_kind:"attachment",record_key:"a",current:{{attachment_id:"a",config:{{text:"server"}}}}}};
const resolved=resolvePromptDraftRecord(drafts,key);
const afterResolve=structuredClone(drafts.get(key));
row.error="network"; row.conflict={{record_kind:"document",record_key:"visual",current:{{text:"server visual"}}}};
const dismissed=dismissPromptDraftError(drafts,key);
console.log(JSON.stringify({{resolved,dismissed,afterResolve,afterDismiss:row}}));
""")
    resolved = result["afterResolve"]
    assert result["resolved"] and result["dismissed"]
    assert resolved["base"]["attachments"][0]["config"]["text"] == "server"
    assert resolved["value"]["attachments"] == [
        {"attachment_id": "a", "config": {"text": "server"}},
        {"attachment_id": "b", "config": {"text": "local chip"}},
    ]
    assert resolved["value"]["channel_docs"]["visual"]["text"] == "mine"
    assert resolved["value"]["channel_docs"]["speech"]["text"] == "local speech"
    assert result["afterDismiss"]["error"] == ""
    assert result["afterDismiss"]["conflict"] is None


def test_prompt_recovery_retry_does_not_choose_a_conflict_winner():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{retryPromptDraft,savePromptDraft,updatePromptDraft}}=await import({json.dumps(url)});
const drafts=new Map(),key="draft",calls=[];
updatePromptDraft(drafts,key,{{channel_docs:{{visual:{{text:"old"}}}},attachments:[]}},
  {{channel_docs:{{visual:{{text:"mine"}}}},attachments:[]}});
const save=async()=>{{calls.push("save");return {{status:"refused",message:"conflict",conflict:{{record_kind:"document",record_key:"visual",current:{{text:"server"}}}}}};}};
await savePromptDraft(drafts,key,save);
const retried=await retryPromptDraft(drafts,key);
console.log(JSON.stringify({{retried,calls,row:drafts.get(key)}}));
""")
    assert result["retried"] is False
    assert result["calls"] == ["save", "save"]
    assert result["row"]["value"]["channel_docs"]["visual"]["text"] == "mine"
    assert result["row"]["conflict"]["current"]["text"] == "server"



def test_prompt_retry_keeps_failure_visible_until_the_attempt_settles():
    url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    result = _run_node(f"""
const {{retryPromptDraft,savePromptDraft,updatePromptDraft}}=await import({json.dumps(url)});
const drafts=new Map(),key="draft";
updatePromptDraft(drafts,key,{{channel_docs:{{visual:{{text:"old"}}}},attachments:[]}},
  {{channel_docs:{{visual:{{text:"mine"}}}},attachments:[]}});
let attempt=0,finish;
const save=async()=>{{attempt+=1;if(attempt===1)return {{status:"refused",message:"conflict",conflict:{{record_kind:"document",record_key:"visual",current:{{text:"server"}}}}}};return await new Promise(resolve=>finish=resolve);}};
await savePromptDraft(drafts,key,save);
const pending=retryPromptDraft(drafts,key);
await Promise.resolve();
const during={{error:drafts.get(key).error,conflict:drafts.get(key).conflict}};
finish({{status:"acknowledged",value:{{channel_docs:{{visual:{{text:"mine"}}}},attachments:[]}}}});
const ok=await pending;
console.log(JSON.stringify({{during,ok,clean:!drafts.has(key)}}));
""")
    assert result["during"]["error"] == "conflict"
    assert result["during"]["conflict"]["record_key"] == "visual"
    assert result["ok"] and result["clean"]

def test_prompt_panel_uses_zero_height_recovery_chrome_and_commits_user_close():
    panel = _source("web/js/editor_prompt_panel.js")
    assert "draftStatus" not in panel
    assert "refreshDraftStatus" not in panel
    header_at = panel.index("panel.appendChild(header);")
    region_at = panel.index("panel.appendChild(diagnosticsRegion);")
    body_at = panel.index("panel.appendChild(body);")
    assert header_at < region_at < body_at
    assert 'height:72px;flex:0 0 72px' in panel
    assert 'draftRecovery.style.cssText = `display:none;position:absolute;' in panel
    assert 'diagnosticsRegion.appendChild(draftRecovery);' in panel
    assert 'diagnosticsRegion.appendChild(diagnostics);' in panel
    assert 'draftRecovery.style.display = "none";' in panel
    assert 'draftRecovery.style.display = "flex";' in panel
    assert 'diagnostics.style.paddingTop = "40px";' in panel
    assert "let recoverySignature" in panel
    assert "renderDraftRecovery = () =>" in panel
    assert 'closeBtn.addEventListener("click", requestClose)' in panel
    assert "if (e.target === backdrop) requestClose();" in panel
    assert "guard.focusedBox.el.blur();" in panel
    assert "cleanup: () => close({ commitFocused: false })" in panel
    assert "focused.el.promptState = focused.revert" in panel
    assert "guard.suppressBlurCommit = true;\n                focused.el.blur();" in panel
    assert panel.count("guard.focusedBox.revert = sibling.promptState") == 2


def test_prompt_recovery_actions_preserve_focus_and_discard_one_key():
    panel = _source("web/js/editor_prompt_panel.js")
    recovery = panel[panel.index("const draftRecovery"):
                     panel.index("const diagnostics", panel.index("const draftRecovery"))]
    assert 'control.addEventListener("pointerdown", (event) => event.preventDefault())' in recovery
    assert 'control.addEventListener("mousedown", (event) => event.preventDefault())' in recovery
    for label in ("Retry", "Use server's version", "Dismiss", "Discard"):
        assert f'makeBtn("{label}"' in recovery
    assert "drafts.delete(key);" in recovery
    assert "for (const [key] of rows) drafts.delete(key)" not in recovery
    assert "row.error" not in panel[panel.index("const keepGlobalDraft"):]


def test_h3_summary_owner_notice_matches_compiler_identity_across_capability_ids():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for H3 Summary warning parity coverage")
    chips_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    result = _run_node(f"""
const {{referenceSummaryEmissionSignatures}} = await import({json.dumps(chips_url)});
const first = {{attachment_id: "first", emission_group_id: "shared",
  source: {{semantic_unit_ids: ["u"]}}, config: {{overrides: {{summary: "First."}}}},
  capabilities: [{{capability_id: "summary-a", kind: "summary",
    channel_key: "summary", enabled: true}}]}};
const second = {{attachment_id: "second", emission_group_id: "shared",
  source: {{semantic_unit_ids: ["u"]}}, config: {{overrides: {{summary: "Second."}}}},
  capabilities: [{{capability_id: "summary-b", kind: "summary",
    channel_key: "summary", enabled: true}}]}};
const h3 = {{validators: ["minimax_reference_setup"], capabilities: {{}}}};
const generic = {{validators: [], capabilities: {{}}}};
const signatures = (value, profile) => referenceSummaryEmissionSignatures(value, {{
  resolvedProfile: profile, channelKey: "summary", scene: {{_context_channel_keys: ["summary"]}},
}});
const intersects = (left, right) => [...left].some((value) => right.has(value));
console.log(JSON.stringify({{
  h3: {{first: [...signatures(first, h3)], second: [...signatures(second, h3)],
    warning: intersects(signatures(first, h3), signatures(second, h3))}},
  generic: {{first: [...signatures(first, generic)], second: [...signatures(second, generic)],
    warning: intersects(signatures(first, generic), signatures(second, generic))}},
  disabled: [...signatures({{...second, enabled: false}}, h3)],
}}));
""")
    assert result == {
        "h3": {"first": ["summary:summary"], "second": ["summary:summary"],
               "warning": True},
        "generic": {"first": ["summary-a:summary"],
                    "second": ["summary-b:summary"], "warning": False},
        "disabled": [],
    }

    chips = _source("web/js/prompt_context_chips.js")
    notice = chips[chips.index("const summaryGroupId ="):
                   chips.index("for (const field of", chips.index("const summaryGroupId ="))]
    assert "referenceSummaryEmissionSignatures(value" in notice
    assert 'String(value?.emission_group_id || "") === summaryGroupId' in notice
    assert "Linked chips share one Summary." in notice

    profile = _source("server/prompt_context.py")
    assert "Prose from each chip accumulates. Two unlinked chips with different summaries both reach the prompt." in profile
    assert "Task types are scene-wide. Every chip's selections combine, duplicates are removed, and the result prints once in MiniMax order as [a + b]." in profile


def test_capability_diagnostic_decoration_does_not_poison_siblings():
    source = _source("web/js/editor_prompt_panel.js")
    start = source.index('        for (const chip of panel.querySelectorAll("[data-attachment-id]"))')
    loop = source[start:source.index('        refreshWritingCompiled(payload);', start)]
    url = (ROOT / "web/js/prompt_context_diagnostics.js").as_uri()
    result = _run_node(f"""
const {{buildPromptContextDiagnostics,promptContextDiagnosticTitle,promptCapabilityDiagnostics}} = await import({json.dumps(url)});
const state=buildPromptContextDiagnostics({{errors:[{{attachment_id:"a",channel_key:"visual",capability_id:"bad",code:"conflicting_emission"}}]}});
const chips=[{{capabilityId:"bad",channelKey:"visual"}},{{capabilityId:"good",channelKey:"visual"}},{{capabilityId:"bad",channelKey:"speech"}},{{}}].map(scope=>({{
 dataset:{{attachmentId:"a",...scope}},style:{{}},title:"State: linked_elsewhere",invalid:false,
 closest:()=>null,removeAttribute(){{this.invalid=false;}},setAttribute(){{this.invalid=true;}}
}}));
const panel={{querySelectorAll:()=>chips}},COLORS={{dangerText:"red",warningText:"yellow"}};
{loop}
console.log(JSON.stringify(chips.map(chip=>({{invalid:chip.invalid,title:chip.title}}))));
""")
    assert [row["invalid"] for row in result] == [True, False, False, True]
    assert "conflicting_emission" not in result[1]["title"]


def test_capability_diagnostics_discriminate_attachment_origin():
    url = (ROOT / "web/js/prompt_context_diagnostics.js").as_uri()
    result = _run_node(f"""
const {{promptCapabilityDiagnostics}} = await import({json.dumps(url)});
const rows = [
  {{code:"global",origin:"global",channel_key:"speech"}},
  {{code:"section",origin:"section-a"}},
  {{code:"legacy"}},
];
console.log(JSON.stringify({{
  global: promptCapabilityDiagnostics(rows,"visual","","global").map(row=>row.code),
  section: promptCapabilityDiagnostics(rows,"","","section-a").map(row=>row.code),
}}));
""")
    assert result == {
        "global": ["global", "legacy"],
        "section": ["section", "legacy"],
    }


def test_capability_projections_discriminate_attachment_origin():
    chips_path = ROOT / "web/js/prompt_context_chips.js"
    result = _run_node(f"""
const {{channelContributionRows,attachmentChannelProjectionSignature}} = await import({json.dumps(chips_path.as_uri())});
const attachment = {{
  attachment_id:"same",emission_group_id:"same",kind:"custom",enabled:true,
  source:{{}},config:{{}},capabilities:[{{capability_id:"body",kind:"custom",placement:"section_prefix",enabled:true}}],
}};
const candidate = {{attachment_capability_projections:[
  {{attachment_id:"same",emission_group_id:"same",capability_id:"body",channel_key:"visual",origin:"global",state:"dormant",state_reason:"global dormant",region:"before",order:0}},
  {{attachment_id:"same",emission_group_id:"same",capability_id:"body",channel_key:"visual",origin:"section-a",state:"emitted",text:"section text",region:"before",order:0}},
  {{attachment_id:"same",emission_group_id:"same",capability_id:"legacy",channel_key:"visual",state:"empty",state_reason:"legacy",region:"after",order:1}},
]}};
const rows = (origin) => channelContributionRows({{
  channelKey:"visual",attachments:[attachment],candidate,origin,
}}).map(value => [value.row.origin || "", value.state]);
const signature = (origin) => JSON.parse(attachmentChannelProjectionSignature({{
  channelKey:"visual",attachments:[attachment],candidate,origin,
}})).projections.map(value => [value.origin || "", value.state]);
console.log(JSON.stringify({{
  globalRows:rows("global"),sectionRows:rows("section-a"),
  globalSignature:signature("global"),sectionSignature:signature("section-a"),
}}));
""")
    assert result == {
        "globalRows": [["global", "dormant"], ["", "empty"]],
        "sectionRows": [["section-a", "emitted"], ["", "empty"]],
        "globalSignature": [["global", "dormant"], ["", "empty"]],
        "sectionSignature": [["section-a", "emitted"], ["", "empty"]],
    }

    chips = _source("web/js/prompt_context_chips.js")
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert 'origin || (scope === "global" ? "global" : "")' in chips
    assert 'origin: String(section.prompt_id || "section")' in panel
    assert 'String(consumerSection.prompt_id || "section") : "global"' in widget


def test_not_inherited_projection_uses_generic_non_emitting_presentation():
    chips_path = ROOT / "web/js/prompt_context_chips.js"
    result = _run_node(f"""
const {{channelContributionRows}} = await import({json.dumps(chips_path.as_uri())});
const attachment = {{
  attachment_id:"global",emission_group_id:"global",kind:"reference",enabled:true,
  source:{{}},config:{{}},capabilities:[{{capability_id:"summary",kind:"summary",enabled:true}}],
}};
const reason = "No effective prompt section in this window inherits the global Summary channel.";
const candidate = {{attachment_capability_projections:[{{
  attachment_id:"global",emission_group_id:"global",capability_id:"summary",
  channel_key:"summary",origin:"global",state:"not_inherited",
  state_reason:reason,text:"",region:"before",order:0,
}}]}};
const row = channelContributionRows({{
  channelKey:"summary",attachments:[attachment],candidate,origin:"global",
}})[0];
console.log(JSON.stringify({{
  state:row.state,emitting:row.emitting,resolved:row.resolved,reason:row.reason,
}}));
""")
    assert result == {
        "state": "not_inherited",
        "emitting": False,
        "resolved": (
            "No effective prompt section in this window inherits the global "
            "Summary channel."),
        "reason": (
            "No effective prompt section in this window inherits the global "
            "Summary channel."),
    }


@pytest.mark.parametrize("global_scope", [False, True])
def test_real_prompt_save_preserves_remote_sibling_and_revision_owned_undo(global_scope):
    widget = _source("web/js/editor_widget.js")
    method = _method(widget, "_updateSceneGlobalContext", "_setSectionGlobalInherit") if global_scope else _method(widget, "_updatePromptSection", "_updateLinkedPromptAttachment")
    take = _method(widget, "_takePromptIdentityCreateIntents", "_adoptPromptIdentitiesFromMutation")
    intent_url = (ROOT / "web/js/prompt_edit_intent.js").as_uri()
    composition_url = (ROOT / "web/js/prompt_composition.js").as_uri()
    template_url = (ROOT / "web/js/prompt_channel_templates.js").as_uri()
    result = _run_node(f"""
const {{promptEditFields}}=await import({json.dumps(intent_url)});
const {{normalizeChannels,composeSectionText}}=await import({json.dumps(composition_url)});
const {{projectTemplateValue,getChannelTemplate}}=await import({json.dumps(template_url)});
const globalScope={json.dumps(global_scope)};
const docsKey=globalScope?"global_channel_docs":"channel_docs", channelsKey=globalScope?"global_channels":"channels", chipsKey=globalScope?"global_attachments":"attachments";
const baseline={{prompt_id:"stable",start_frame:0,end_frame:24,[docsKey]:{{visual:{{text:"old"}},speech:{{text:"S"}}}},[channelsKey]:{{visual:"old",speech:"S"}},[chipsKey]:[]}};
class Host {{
 {method}
 {take}
 constructor() {{this.projectDir="p";this.activeSceneId="s";this.row=structuredClone(baseline);this.row[docsKey].speech.text="REMOTE";this.row[channelsKey].speech="REMOTE";this.activeScene=globalScope?this.row:{{prompt_sections:[this.row]}};this.history=[];this.calls=[];}}
 _projectDirName(){{return "p";}} _channelTemplate(){{return getChannelTemplate("sonder");}}
 _isGlobalPromptTrackLocked(){{return false;}} _isPromptTrackLocked(){{return false;}}
 _pushUndo(label){{const row={{label}};this.history.push(row);return row;}}
 _discardUndoEntry(row){{this.history=this.history.filter(value=>value!==row);}}
 _renderSceneAfterLocalMutation(){{}} _refreshPromptContextDependencyConsumers(){{}}
 _adoptPromptIdentitiesFromMutation(){{}} _finalizePromptIdentityCreationHistory(){{}}
 _applyLocalPromptUpdate(index,fields){{Object.assign(this.row,fields);}}
 _fetchReferences(){{return Promise.resolve();}}
 _runSceneMutation(operations){{return new Promise((resolve,reject)=>this.calls.push({{operations,resolve,reject}}));}}
}}
const host=new Host();
const value=structuredClone(baseline);value[docsKey].visual.text="A";value[channelsKey].visual="A";
const submit=(row,base)=>globalScope?host._updateSceneGlobalContext(row[channelsKey],row[docsKey],row[chipsKey],{{baseline:base}}):host._updatePromptSection(0,{{channels:row.channels,channel_docs:row.channel_docs,attachments:row.attachments}},{{baseline:base}});
const one=submit(value,baseline), firstUndo=host.history[0];
const value2=structuredClone(value);value2[docsKey].visual.text="B";value2[channelsKey].visual="B";
const two=submit(value2,value), secondUndo=host.history[1];
host.calls[0].reject(new Error("refused")); await one;
const afterOldFailure=host.row[docsKey].visual.text, ownUndo=host.history.includes(secondUndo)&&!host.history.includes(firstUndo);
host.calls[1].resolve({{payload:{{}}}});await two;
const identity={{unit:{{semantic_unit_id:"u"}}}}, chip={{attachment_id:"chip"}};
host._pendingPromptIdentityCreateIntents=new Map([["u",{{attachmentId:"chip",intent:identity}}]]);
const third=structuredClone(value2);third[docsKey].visual.text="C";third[chipsKey]=[chip];
const fail=submit(third,value2);host.calls[2].reject(new Error("refused identity"));await fail;
const retry=submit(third,value2);host.calls[3].resolve({{payload:{{}}}});await retry;
console.log(JSON.stringify({{changed:Object.keys(host.calls[0].operations.at(-1).fields.prompt_edit.documents),afterOldFailure,ownUndo,
 creates:host.calls.slice(2).map(call=>call.operations.filter(op=>op.unit).length)}}));
""")
    assert result == {"changed": ["visual"], "afterOldFailure": "B", "ownUndo": True, "creates": [1, 1]}
