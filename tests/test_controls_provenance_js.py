"""Gallery side of Controls at run: renderer, supplied lookup, Copy and search."""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COPY_MODULE = ROOT / "web" / "js" / "gallery_copy_values.js"
RENDERERS = ROOT / "web" / "js" / "tracked_metadata_renderers.js"
GALLERY = ROOT / "web" / "js" / "shared_asset_gallery.js"

FAKE_DOM = r"""
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this.textContent = ''; this.title = ''; }
  get firstChild() { return this.children[0] || null; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  get text() { return [this.textContent, ...this.children.map((c) => c.text)].join(' ').trim(); }
}
globalThis.document = { createElement: (tag) => new El(tag) };
const style = (el) => el;
const CHROME = { textDim: '#888' };
"""

SECTION = {
    "label": "Controls at run", "display_type": "controls",
    "fields": {"Seed": "18446744073709551615", "Steps": "20", "Prompt": "x" * 512 + "… [truncated, full value in the source prompt]",
               "Frame rate": 24},
    "controls": [
        {"binding_id": "s1", "field": "Seed", "kind": "SEED", "seed_mode": "increment",
         "targets": [{"path": "3", "input": "seed"}]},
        {"binding_id": "st", "field": "Steps", "kind": "INT", "origin": "local", "group": "Sampling", "advanced": True,
         "targets": [{"path": "3", "input": "steps"}]},
        {"binding_id": "pr", "field": "Prompt", "kind": "STRING", "targets": [{"path": "2", "input": "text"}]},
        {"binding_id": "fr", "field": "Frame rate", "kind": "FLOAT", "section": "editor",
         "targets": [{"path": "1", "input": "fps"}]},
    ],
    "supplied": [{"section_index": 0, "field": "steps", "binding_id": "st"},
                 {"section_index": 0, "field": "cfg", "binding_id": "missing"},
                 {"section_index": 5, "field": "steps", "binding_id": "st"}],
}
COLLECTOR = {"label": "Sampler", "source_node_id": "3", "display_type": None, "fields": {"steps": 20, "cfg": 7}}


def _run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for gallery renderer tests")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def _imports():
    return (f"import {{ CONTROLS_TRUNCATED_MARKER, resolveTrackedFieldCopy, trackedFieldDisplayText }} "
            f"from {json.dumps(COPY_MODULE.as_uri())};\n"
            f"import {{ renderTrackedSectionBody, controlsSuppliedLookup }} "
            f"from {json.dumps(RENDERERS.as_uri())};\n")


def test_server_and_gallery_share_the_controls_marker():
    module = ast.parse((ROOT / "server" / "controls_snapshot.py").read_text(encoding="utf-8"))
    python_marker = next(ast.literal_eval(node.value) for node in module.body
                         if isinstance(node, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == "TRUNCATED_MARKER" for t in node.targets))
    assert _run(_imports() + "console.log(JSON.stringify(CONTROLS_TRUNCATED_MARKER));") == python_marker


def test_copy_takes_complete_values_and_refuses_a_capped_one():
    result = _run(_imports() + f"""
const entry = {json.dumps(SECTION)};
console.log(JSON.stringify({{
  seed: resolveTrackedFieldCopy(entry, 'Seed'),
  rate: resolveTrackedFieldCopy(entry, 'Frame rate'),
  missing: resolveTrackedFieldCopy({{ ...entry, fields: {{ ...entry.fields, Seed: null }} }}, 'Seed'),
  prompt: resolveTrackedFieldCopy(entry, 'Prompt'),
}}));
""")
    assert result["seed"] == {"kind": "value", "label": "Copy value", "text": "18446744073709551615"}
    assert result["rate"]["text"] == "24"
    assert result["missing"]["kind"] == "unavailable"
    assert result["prompt"]["kind"] == "unavailable" and result["prompt"]["text"] == ""


def test_supplied_lookup_validates_every_reference():
    result = _run(_imports() + f"""
const collector = {json.dumps(COLLECTOR)};
const controls = {json.dumps(SECTION)};
const lookup = controlsSuppliedLookup([collector, controls]);
console.log(JSON.stringify({{
  steps: lookup.supplied(collector, 'steps'),
  cfg: lookup.supplied(collector, 'cfg'),
  none: controlsSuppliedLookup([{{...collector, display_type: 'subgraph'}}, controls]).supplied(collector, 'steps'),
}}));
""")
    assert result["steps"] == {"binding_id": "st", "label": "Steps"}
    assert result["cfg"] is None  # its binding is not recorded
    assert result["none"] is None


NATIVE_STACK = {"label": "Load LoRA (Model)", "source_node_id": "2", "display_type": "native_lora_stack",
                "source_group": "loras", "fields": {"power_loras": [
                    {"slot": 1, "name": "sonder_test_a.safetensors", "enabled": True, "strength": 1},
                    {"slot": 2, "name": "sonder_test_b.safetensors", "enabled": True, "strength": 0, "skipped": True},
                    {"slot": 3, "name": "sonder_test_c.safetensors", "enabled": False, "strength": 1, "skipped": True}]}}
STACK_CONTROLS = {"label": "Controls at run", "display_type": "controls", "fields": {"Stack": "3 LoRAs · 2 on"},
                  "controls": [{"binding_id": "stack", "field": "Stack", "kind": "ROWS",
                                "targets": [{"path": "2", "input": "loras"}]}],
                  "supplied": [{"section_index": 0, "field": "power_loras", "binding_id": "stack"}]}


def test_a_native_lora_stack_is_marked_through_its_row_group():
    # Mirrors server controls_snapshot._supplied: a structured section matches
    # only through source_group; rgthree's power_loras keeps no input name.
    result = _run(_imports() + f"""
const stack = {json.dumps(NATIVE_STACK)};
const controls = {json.dumps(STACK_CONTROLS)};
const rgthree = {{...stack, display_type: 'power_loras', source_group: undefined}};
console.log(JSON.stringify({{
  native: controlsSuppliedLookup([stack, controls]).supplied(stack, 'power_loras'),
  rgthree: controlsSuppliedLookup([rgthree, controls]).supplied(rgthree, 'power_loras'),
  other: controlsSuppliedLookup([{{...stack, source_group: 'other'}}, controls]).supplied(stack, 'power_loras'),
}}));
""")
    assert result["native"] == {"binding_id": "stack", "label": "Stack"}
    assert result["rgthree"] is None and result["other"] is None


def test_a_native_lora_stack_renders_its_heading_chip_and_skipped_rows():
    result = _run(FAKE_DOM + _imports() + f"""
Object.assign(El.prototype, {{ addEventListener() {{}}, setAttribute() {{}}, contains() {{ return false; }} }});
const stack = {json.dumps(NATIVE_STACK)};
const lookup = controlsSuppliedLookup([stack, {json.dumps(STACK_CONTROLS)}]);
const out = renderTrackedSectionBody(stack, {{ style, CHROME, controlSupplied: lookup.supplied,
  formatGenerationValue: String, fieldSearchToken: () => '' }});
const statuses = out.dom.children.slice(1).map((wrap) => wrap.children[0].children[1].textContent);
console.log(JSON.stringify({{ heading: out.dom.children[0].text, statuses }}));
""")
    assert result["heading"].startswith("LoRAs") and "Control" in result["heading"]
    assert result["statuses"] == ["On", "Skipped", "Off"]


def test_renderer_groups_fields_and_decorates_through_gallery_cells():
    result = _run(FAKE_DOM + _imports() + f"""
const entry = {json.dumps(SECTION)};
const grids = [];
const ctx = {{ style, CHROME,
  renderFieldGrid: (e, keys, decorate) => {{
    const grid = document.createElement('div');
    for (const key of keys) {{
      const cell = document.createElement('div');
      if (key === 'Prompt') cell.title = 'Pinned to top';
      cell.appendChild(document.createElement('div'));
      cell.firstChild.textContent = key;
      decorate(cell, key);
      grid.appendChild(cell);
    }}
    grids.push({{ keys, titles: grid.children.map((c) => c.title), heads: grid.children.map((c) => c.firstChild.text) }});
    return grid;
  }},
}};
const tagged = renderTrackedSectionBody(entry, ctx);
const tagGrids = grids.splice(0);
console.log(JSON.stringify({{ consumed: tagged.consumedFields, generic: tagged.pinnedFieldsGeneric,
  text: tagged.dom.text, tagGrids }}));
""")
    assert sorted(result["consumed"]) == ["Frame rate", "Prompt", "Seed", "Steps"]
    assert result["generic"] is True
    assert "switched off" in result["text"]
    assert [grid["keys"] for grid in result["tagGrids"]] == [["Seed", "Prompt"], ["Steps"], ["Frame rate"]]
    assert "SAMPLING" not in result["text"] and "Sampling" in result["text"] and "Editor" in result["text"]
    seed_grid = result["tagGrids"][0]
    assert seed_grid["titles"][0] == "Control s1 · seed increment"
    assert seed_grid["heads"][0] == "Seed · increment"
    # An Advanced Control carries the panel's Adv tag (Phase 4B).
    assert result["tagGrids"][1]["titles"][0] == "Control st · App only · Advanced"
    assert "Adv" in result["tagGrids"][1]["heads"][0] and "Adv" not in seed_grid["heads"][0]
    assert result["tagGrids"][0]["titles"][1] == "Pinned to top\nControl pr"


def test_renderer_without_a_grid_host_falls_back_to_the_generic_grid():
    result = _run(FAKE_DOM + _imports() + f"""
const out = renderTrackedSectionBody({json.dumps(SECTION)}, {{ style, CHROME }});
console.log(JSON.stringify({{ consumed: out.consumedFields }}));
""")
    assert result["consumed"] == []


def test_gallery_wires_the_controls_lookup_and_grid():
    source = GALLERY.read_text(encoding="utf-8")
    assert "renderFieldGrid: (entry, keys, decorate) => renderGenericTrackedFields(" in source
    assert source.count("withControlsLookup(trackedRenderContext(surface), entries)") == 2
    assert "bodyResult.pinnedFieldsGeneric" in source
    # Collector fields a Control set keep their cell and gain a Control chip (tag display).
    assert 'chip.textContent = "Control";' in source and "controlsAlsoIn" not in source



def test_native_control_rows_keep_source_identity_for_events_copy_and_field_rendering():
    imports = f"""
import {{ renderTrackedFieldBody, trackedFieldMatchForEntry }} from {json.dumps(RENDERERS.as_uri())};
import {{ resolvePowerLoraRowCopies }} from {json.dumps(COPY_MODULE.as_uri())};
"""
    result = _run(FAKE_DOM + _imports() + imports + r"""
Object.assign(El.prototype, {
 addEventListener(type, fn) { (this.events ||= {})[type] = fn; }, setAttribute() {}, contains() { return false; }
});
const filename = 'folder/模型 % choice.safetensors';
const rows = [{slot: 1, name: filename, enabled: true, strength: 0.8},
 {slot: 2, name: filename, enabled: false, strength: 0, skipped: true}];
const entry = {display_type: 'controls', fields: {First: rows, Second: [{...rows[0], name: 'other'}], Scalar: 20},
 controls: [{field: 'First', kind: 'ROWS', binding_id: 'a', display_type: 'native_lora_stack', advanced: true},
 {field: 'Second', kind: 'ROWS', binding_id: 'b', display_type: 'native_lora_stack'},
 {field: 'Scalar', kind: 'INT', binding_id: 'c'}]};
const events = [], copies = [];
const ctx = {style, CHROME, formatGenerationValue: String, fieldSearchToken: (k,v) => 'field:'+encodeURIComponent(k)+'='+encodeURIComponent(v),
 tokenActiveA: () => true, tokenActiveB: () => false,
 onFieldClick: (e, info) => events.push(info), onFieldContextMenu: (e,info) => events.push(info),
 rowCopy: (e,i,k) => resolvePowerLoraRowCopies(e,i,k).row, onFieldCopy: copy => copies.push(copy),
 renderFieldGrid: () => document.createElement('div')};
const dom = renderTrackedFieldBody(entry, 'First', ctx);
const row = dom.children[1], button = row.children[0], copy = row.children[1];
const event = {preventDefault() {}, stopPropagation() {}};
button.events.click(event); button.events.contextmenu(event); copy.events.click(event);
const needle = events[0].value.toLowerCase();
const collector = {display_type:'power_loras', fields:{power_loras:[{name:filename, enabled:true, strength:'0.80'}]}};
const wrong = structuredClone(entry); wrong.fields.First[0].strength = 0.9;
const collision = {...entry, fields:{...entry.fields,power_loras:'a literal value'}};
console.log(JSON.stringify({
 heading: dom.children[0].text, ownField: events.every(i=>i.entry===entry && i.fieldKey==='First' && i.filterFieldKey==='power_loras' && i.rowIndex===0),
 copy: copies[0].text, name:resolvePowerLoraRowCopies(entry,0,'First').name.text,
 match: trackedFieldMatchForEntry(entry,'power_loras',needle), cross:trackedFieldMatchForEntry(collector,'power_loras',needle),
 wrong:trackedFieldMatchForEntry(wrong,'power_loras',needle), plain:trackedFieldMatchForEntry(entry,'power_loras','模型 %'),
 collision:trackedFieldMatchForEntry(collision,'power_loras','literal'), unrelated:trackedFieldMatchForEntry(entry,'scalar','20'),
 noOther:!dom.text.includes('other'), scalarHook:renderTrackedFieldBody(entry,'Scalar',ctx), events:events.length
}));
""")
    assert result['ownField'] and result['match'] and result['cross'] and result['plain'] and result['collision']
    assert result['wrong'] is False and result['unrelated'] is None
    assert result['noOther'] and result['scalarHook'] is None and result['events'] == 2
    assert 'Adv' in result['heading'] and '2 LoRAs' in result['heading']
    assert json.loads(result['copy'])['name'] == result['name'] == 'folder/模型 % choice.safetensors'


def test_controls_mixed_scalar_stack_order_and_unknown_tags_fall_back():
    imports = f"import {{ renderTrackedFieldBody }} from {json.dumps(RENDERERS.as_uri())};\n"
    result = _run(FAKE_DOM + _imports() + imports + r"""
Object.assign(El.prototype,{addEventListener(){},setAttribute(){},contains(){return false;}});
const entry = {display_type:'controls',fields:{Before:1,Stack:[],After:2},controls:[
 {field:'Before',binding_id:'a',kind:'INT'},
 {field:'Stack',binding_id:'b',kind:'ROWS',display_type:'native_lora_stack'},
 {field:'After',binding_id:'c',kind:'INT'}]};
const ctx={style,CHROME,formatGenerationValue:String,fieldSearchToken:()=>'',renderFieldGrid:(e,keys)=>{
 const grid=document.createElement('div');grid.textContent=keys.join(',');return grid;
}};
const body=renderTrackedSectionBody(entry,ctx).dom;
const unknown=structuredClone(entry);unknown.controls[1].display_type='future';
const malformed=structuredClone(entry);malformed.fields.Stack=[{name:'x'}];
console.log(JSON.stringify({order:body.children.slice(1).map(e=>e.text),
 unknown:renderTrackedFieldBody(unknown,'Stack',ctx),bad:renderTrackedFieldBody(malformed,'Stack',ctx)}));
""")
    assert result['order'] == ['Before', 'Stack · 0 LoRAs · 0 on', 'After']
    assert result['unknown'] is None and result['bad'] is None


def test_omitted_stack_copy_is_a_summary_and_clipped_native_fallback_is_unavailable():
    result = _run(_imports() + r"""
const entry={display_type:'controls',fields:{Stack:'20 LoRAs · 10 on',Bad:'prefix'+CONTROLS_TRUNCATED_MARKER},
 controls:[{field:'Stack',rows_omitted:true}]};
console.log(JSON.stringify({summary:resolveTrackedFieldCopy(entry,'Stack'),bad:resolveTrackedFieldCopy(entry,'Bad')}));
""")
    assert result['summary']['label'] == 'Copy summary'
    assert result['bad']['kind'] == 'unavailable'



def test_actual_gallery_click_context_and_search_use_virtual_filter_key():
    source = GALLERY.read_text(encoding='utf-8')
    def between(start, end):
        return source[source.index(start):source.index(end, source.index(start))]
    code = between('    function handleTrackedFieldClick(', '    function handleTrackedSectionContextMenu(')
    code += between('function parseAssetSearchQuery(', '// The one authority for which')
    code += between('function trackedFieldMatches(', 'function canOpenWorkflowFor(')
    code += between('function formatGenerationValue(', 'function assetExtension(')
    code += between('    function fieldSearchToken(', '    function activeSearchTokens(')
    imports = f"import {{ trackedFieldMatchForEntry }} from {json.dumps(RENDERERS.as_uri())};\n"
    result = _run(imports + code + r"""
const toggles=[];
const compareModeActive=()=>true;
const toggleCompareSearchToken=(side,token)=>toggles.push([side,token]);
const row={slot:1,name:'folder/模型 % X.safetensors',strength:0.8,enabled:true};
const entry={display_type:'controls',fields:{Stack:[row]},controls:[{kind:'ROWS',field:'Stack',display_type:'native_lora_stack'}]};
const info={entry,fieldKey:'Stack',filterFieldKey:'power_loras',rowIndex:0,value:JSON.stringify({n:row.name,e:1,s:'0.8'})};
const event={stopPropagation(){},preventDefault(){}};
handleTrackedFieldClick(event,info,'fullscreen');handleTrackedFieldContextMenu(event,info,'fullscreen');
const term=parseAssetSearchQuery(toggles[0][1]).fieldTerms[0];
console.log(JSON.stringify({sides:toggles.map(t=>t[0]),key:term.name,
 match:trackedFieldMatches([entry],term.name,term.value),
 wrong:trackedFieldMatches([{...entry,fields:{Stack:[{...row,strength:0.5}]}}],term.name,term.value),
 normal:trackedFieldMatches([entry],'stack','模型 %')}));
""")
    assert result == {'sides':['A','B'],'key':'power_loras','match':True,'wrong':False,'normal':True}


def test_actual_pinned_panel_lifts_only_one_native_stack_and_keeps_scalar_pin():
    source = GALLERY.read_text(encoding='utf-8')
    code = source[source.index('    function renderPinnedFieldsTopGroup('):source.index('    function renderTrackedSectionContainer(')]
    imports = f"import {{ renderTrackedFieldBody }} from {json.dumps(RENDERERS.as_uri())};\n"
    result = _run(FAKE_DOM + _imports() + imports + code + r"""
Object.assign(El.prototype,{addEventListener(){},setAttribute(){},contains(){return false;},prepend(node){this.children.unshift(node);}});
const rows=name=>[{slot:1,name,enabled:true,strength:0.8}];
const entry={label:'Controls at run',display_type:'controls',fields:{One:rows('chosen.safetensors'),Two:rows('UNPINNED.safetensors'),Scalar:42},
 controls:[{field:'One',kind:'ROWS',display_type:'native_lora_stack'},
 {field:'Two',kind:'ROWS',display_type:'native_lora_stack'},{field:'Scalar',kind:'INT'}]};
const pins=new Set(['One','Scalar']);
const pinSetsForSurface=()=>({fields:pins});
const trackedFieldPinKey=(e,key)=>key;
const ctx={style,CHROME,formatGenerationValue:String,fieldSearchToken:()=>'',renderFieldGrid:(e,keys)=>{
 const dom=document.createElement('div');dom.textContent=keys.join(',');return dom;
}};
const trackedRenderContext=()=>ctx,withControlsLookup=c=>c;
const formatGenerationValue=v=>JSON.stringify(v),fieldSearchToken=()=>'';
const makeMetaCell=(key,value)=>{const c=document.createElement('div');c.appendChild(document.createElement('div'));c.firstChild.textContent=key;return c;};
const attachCopyToElement=()=>{},applyFieldCellActiveStyle=()=>{},handleTrackedFieldContextMenu=()=>{},handleTrackedFieldClick=()=>{};
const result=renderPinnedFieldsTopGroup([entry],'fullscreen');
console.log(JSON.stringify({text:result.text,unrelated:result.text.includes('UNPINNED'),
 chosen:result.text.split('chosen.safetensors').length-1,scalar:result.text.includes('Scalar')}));
""")
    assert result['chosen'] == 1 and result['scalar'] and not result['unrelated']
    assert 'Pinned Fields (2)' in result['text']



def test_native_control_heading_preserves_duplicate_label_suffix():
    imports = f"import {{ renderTrackedFieldBody }} from {json.dumps(RENDERERS.as_uri())};\n"
    result = _run(FAKE_DOM + _imports() + imports + r"""
Object.assign(El.prototype,{addEventListener(){},setAttribute(){},contains(){return false;}});
const row={slot:1,name:'probe.safetensors',enabled:true,strength:1};
const entry={display_type:'controls',fields:{Stack:[row],'Stack [second]':[row]},controls:[
 {field:'Stack',label:'Stack',kind:'ROWS',display_type:'native_lora_stack'},
 {field:'Stack [second]',label:'Stack',kind:'ROWS',display_type:'native_lora_stack'}]};
const ctx={style,CHROME,formatGenerationValue:String,fieldSearchToken:()=>''};
console.log(JSON.stringify(entry.controls.map(c=>renderTrackedFieldBody(entry,c.field,ctx).children[0].text)));
""")
    assert result == ['Stack · 1 LoRA · 1 on', 'Stack [second] · 1 LoRA · 1 on']
