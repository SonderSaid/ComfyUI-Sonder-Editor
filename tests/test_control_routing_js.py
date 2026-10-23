"""Phase 1B canvas side: the routing capsule an export carries for the Sonder App.

exportRouting (web/js/control_bindings.js) mirrors ExecutableNodeDTO resolution
on graph-shaped objects; its capsule is then compiled by the server's
control_routing, so these tests run the whole path from canvas to prompt. The
installed frontend is the live oracle (a probe and a roadmap row).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import control_routing as cr

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

GRAPH = r'''
import assert from 'node:assert/strict';
const B = await import(BINDINGS_URL);
let nextLink = 100;
class Graph {
    constructor(){this._nodes=[];this.links=new Map();this.groups=[];}
    getLink(id){return this.links.get(id)??null;}
    getNodeById(id){return this._nodes.find(n=>String(n.id)===String(id))??null;}
}
const widget = (name,value,extra={}) => ({name,value,options:{},...extra});
const add = (g,type,id,{inputs=[],outputs=[],widgets=[],mode=0,virtual=false}={}) => {
    const n={id,type,comfyClass:type,title:type,graph:g,mode,properties:{},widgets,isVirtualNode:virtual,
        inputs:inputs.map(([name,type])=>({name,type,link:null})),outputs:outputs.map(type=>({type,links:[]}))};
    g._nodes.push(n);return n;};
const link = (g,from,fs,to,ts,extra={}) => { const id=nextLink++; g.links.set(id,{id,origin_id:from.id??from,origin_slot:fs,target_id:to.id,target_slot:ts,...extra}); to.inputs[ts].link=id; return id; };
const reroute = (g,id,type='*') => { const r=add(g,'Reroute',id,{inputs:[['',type]],outputs:[type],virtual:true});
    r.getInputLink = slot => r.inputs[0].link==null ? null : {...g.getLink(r.inputs[0].link), target_slot:0}; return r; };
const host = (g,id,{inputs=[],outputs=[],mode=0}={}) => { const h=add(g,'uuid-sub',id,{inputs,outputs,mode,virtual:true});
    h.subgraph=new Graph(); h.subgraph.id='def-'+id; h.outputLinks={};
    h.resolveSubgraphOutputLink = slot => { const l=h.outputLinks[slot]; return l ? {outputNode:h.subgraph.getNodeById(l.node), link:{origin_slot:l.slot}} : undefined; };
    return h; };
const out = x => console.log(JSON.stringify(x));
'''


def run(script, tmp_path):
    text = GRAPH.replace("BINDINGS_URL", json.dumps((ROOT / "web/js/control_bindings.js").as_uri())) + script
    path = tmp_path / "case.mjs"
    path.write_text(text, encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


CHAIN = r'''
const root = new Graph();
const load = add(root,'Load',1,{outputs:['IMAGE']});
const inv = add(root,'Invert',2,{inputs:[['image','IMAGE']],outputs:['IMAGE']});
const blur = add(root,'Blur',4,{inputs:[['image','IMAGE']],outputs:['IMAGE'],widgets:[widget('radius',3)]});
const r = reroute(root,9);
const save = add(root,'Save',3,{inputs:[['images','IMAGE'],['strength','FLOAT']],widgets:[widget('strength',0.5)]});
link(root,load,0,inv,0); link(root,inv,0,blur,0); link(root,blur,0,r,0); link(root,r,0,save,0); link(root,inv,0,save,1);
'''


def test_capsule_records_ports_hops_edges_and_fallbacks(tmp_path):
    [capsule] = run(CHAIN + "out(B.exportRouting(root, ['2','4']));", tmp_path)
    assert capsule["nodes"] == {
        "2": {"i": [["image", "IMAGE", {"a": ["1", 0]}]], "o": [["IMAGE"]]},
        "4": {"i": [["image", "IMAGE", {"m": ["2", 0, "IMAGE"]}]], "o": [["IMAGE"]]},
    }
    # Through the reroute the hop keeps the consumer's type; Save's strength
    # keeps its widget value as the fallback.
    assert sorted(map(tuple, (tuple(e[:5]) + tuple(e[5:]) for e in capsule["edges"]))) == sorted([
        ("4", "image", "2", 0, "IMAGE"), ("3", "images", "4", 0, "IMAGE"), ("3", "strength", "2", 0, "FLOAT", 0.5)])
    routing, problem = cr.read_routing(json.dumps(capsule))
    assert problem is None
    prompt = {"1": {"class_type": "Load", "inputs": {}}, "2": {"class_type": "Invert", "inputs": {"image": ["1", 0]}},
              "4": {"class_type": "Blur", "inputs": {"image": ["2", 0], "radius": 3}},
              "3": {"class_type": "Save", "inputs": {"images": ["4", 0], "strength": ["2", 0]}}}
    assert cr.compile_modes(prompt, routing, {"4": "bypassed"})["3"]["inputs"] == {"images": ["2", 0], "strength": ["2", 0]}
    assert cr.compile_modes(prompt, routing, {"2": "muted"})["3"]["inputs"] == {"images": ["4", 0], "strength": 0.5}


def test_an_off_non_member_upstream_stays_off_and_nothing_else_is_an_edge(tmp_path):
    [capsule] = run(CHAIN + "load.mode = 2; out(B.exportRouting(root, ['4']));", tmp_path)
    # Invert is no member: Blur's input resolves straight to it.
    assert capsule["nodes"]["4"]["i"] == [["image", "IMAGE", {"a": ["2", 0]}]]
    assert [e[:3] for e in capsule["edges"]] == [["3", "images", "4"]]


def test_instances_route_through_their_ports_and_promoted_inputs(tmp_path):
    [capsule] = run(r'''
const root = new Graph();
const load = add(root,'Load',1,{outputs:['IMAGE']});
const h = host(root,10,{inputs:[['pixels','IMAGE'],['amount','FLOAT']],outputs:['IMAGE']});
const inner = add(h.subgraph,'Invert',5,{inputs:[['image','IMAGE'],['amount','FLOAT']],outputs:['IMAGE'],widgets:[widget('amount',1)]});
link(h.subgraph,-10,0,inner,0,{originIsIoNode:true}); link(h.subgraph,-10,1,inner,1,{originIsIoNode:true});
h.outputLinks[0] = {node:5, slot:0};
h.getWidgetFromSlot = input => input.name === 'amount' ? widget('amount', 0.25) : null;
const save = add(root,'Save',3,{inputs:[['images','IMAGE']]});
const g = host(root,20,{inputs:[['pixels','IMAGE']],outputs:[]});
const deep = add(g.subgraph,'Preview',7,{inputs:[['images','IMAGE']]});
link(g.subgraph,-10,0,deep,0,{originIsIoNode:true});
link(root,load,0,h,0); link(root,h,0,save,0); link(root,h,0,g,0);
out(B.exportRouting(root, ['10']));
''', tmp_path)
    assert capsule["nodes"]["10"] == {"i": [["pixels", "IMAGE", {"a": ["1", 0]}], ["amount", "FLOAT", None]],
                                      "o": [["IMAGE", [["IMAGE", {"a": ["10:5", 0]}]]]]}
    # A consumer inside another instance reaches the member through that
    # instance's input, with that input's type.
    edges = {(e[0], e[1]): e for e in capsule["edges"]}
    assert edges[("3", "images")][2:5] == ["10", 0, "IMAGE"] and edges[("20:7", "images")][2:5] == ["10", 0, "IMAGE"]
    # The member's own inner node reads its promoted value from the host, not an edge.
    assert ("10:5", "amount") not in edges


def test_kj_get_nodes_resolve_through_their_set_nodes_link(tmp_path):
    # KJNodes' GetNode answers getInputLink with its SetNode's input link;
    # native resolves on that link's target (the SetNode).
    [capsule] = run(r'''
const root = new Graph();
const src = add(root,'Load',1,{outputs:['IMAGE']});
const inv = add(root,'Invert',2,{inputs:[['image','IMAGE']],outputs:['IMAGE']});
const set = add(root,'SetNode',5,{inputs:[['IMAGE','*']],outputs:['*'],virtual:true});
const get = add(root,'GetNode',6,{outputs:['*'],virtual:true});
const save = add(root,'Save',3,{inputs:[['images','IMAGE']]});
link(root,src,0,inv,0); link(root,inv,0,set,0); link(root,get,0,save,0);
get.getInputLink = () => root.getLink(set.inputs[0].link);
out(B.exportRouting(root, ['2']));
''', tmp_path)
    assert capsule.get("error") is None and capsule["edges"] == [["3", "images", "2", 0, "IMAGE"]]


def test_host_outputs_answer_each_asking_type(tmp_path):
    # An inner bypassed node picks its input by the asking type; the capsule
    # answers per type, so an untouched instance never refuses a run.
    [capsule] = run(r'''
const root = new Graph();
const load = add(root,'Load',1,{outputs:['MODEL']});
const h = host(root,10,{inputs:[['model','MODEL']],outputs:['MODEL']});
const lora = add(h.subgraph,'Lora',5,{inputs:[['model','MODEL']],outputs:['MODEL'],mode:4});
link(h.subgraph,-10,0,lora,0,{originIsIoNode:true});
h.outputLinks[0] = {node:5, slot:0};
const ks = add(root,'KSampler',3,{inputs:[['model','MODEL']]});
link(root,load,0,h,0); link(root,h,0,ks,0);
h.outputs[0].links = [1];
const up = add(root,'Up',4,{outputs:['X']});
out(B.exportRouting(root, ['10','4']));
''', tmp_path)
    assert capsule["nodes"]["10"]["o"] == [["MODEL", [["MODEL", {"a": ["1", 0]}]]]]
    routing, problem = cr.read_routing(json.dumps(capsule))
    prompt = {"1": {"class_type": "Load", "inputs": {}}, "3": {"class_type": "KSampler", "inputs": {"model": ["1", 0]}},
              "4": {"class_type": "Up", "inputs": {}}}
    assert problem is None and cr.compile_modes(prompt, routing, {"4": "muted"})["3"]["inputs"] == {"model": ["1", 0]}


def test_unreplayable_graphs_refuse_and_large_ones_hit_the_budget(tmp_path):
    unsupported, budget = run(CHAIN + r'''
const loop = new Graph();
const a = add(loop,'A',1,{inputs:[['x','X']],outputs:['X'],mode:4});
const b = add(loop,'B',2,{inputs:[['x','X']],outputs:['X'],mode:4});
const c = add(loop,'C',3,{inputs:[['x','X']]});
link(loop,a,0,b,0); link(loop,b,0,a,0); link(loop,b,0,c,0);
const m = add(loop,'M',4,{outputs:['X']});
out(B.exportRouting(loop, ['4']));
const big = new Graph();
const src = add(big,'Src',1,{outputs:['X']});
for (let i = 0; i < B.MAX_ROUTING_EDGES + 1; i++) link(big,src,0,add(big,'C',i+10,{inputs:[['x','X']]}),0);
out(B.exportRouting(big, ['1']));
''', tmp_path)
    assert unsupported == {"v": 1, "error": "unsupported"} and budget == {"v": 1, "error": "budget"}


def test_native_rules_mirror_the_server(tmp_path):
    pairs = [["IMAGE", "IMAGE"], ["IMAGE", "image"], ["IMAGE,MASK", "MASK"], ["*", "X"], ["", "X"], [None, "X"],
             ["IMAGE", "LATENT"], ["IMAGE,", "LATENT"], ["A,B", "C,D"]]
    bypass = [[["IMAGE"], "IMAGE", 0, "*"], [["IMAGE"], "IMAGE", 3, ""], [["MASK", "IMAGE"], "IMAGE", 1, "IMAGE"],
              [["MASK", "IMAGE"], "IMAGE", 0, "IMAGE"], [["IMAGE,MASK", "LATENT"], "MASK", 1, "MASK"],
              [["LATENT"], "IMAGE", 0, "IMAGE"], [["*", "IMAGE"], "IMAGE", 0, "IMAGE"]]
    js, = run(f"""out([{json.dumps(pairs)}.map(([a,b]) => B.isValidConnection(a,b)),
                     {json.dumps(bypass)}.map(([i,o,s,t]) => B.bypassIndex(i,o,s,t))]);""", tmp_path)
    assert js == [[cr.is_valid_connection(a, b) for a, b in pairs], [cr.bypass_index(*row) for row in bypass]]


def test_export_carries_the_capsule_as_text_and_saving_drops_it(tmp_path):
    exported, saved = run(CHAIN + r'''
inv.properties.sonderExecution = 'pass';
const text = JSON.stringify({v:1, bindings:{}, notes:{}, execution:{v:1, controls:{pass:{label:'',target:{kind:'node'}}}}});
const exported = JSON.parse(B.exportManifest(text, root));
out(exported);
out(JSON.parse(B.editManifest(JSON.stringify(exported), () => {})));
''', tmp_path)
    routing, problem = cr.read_routing(exported["execution"]["routing"])
    assert problem is None and set(routing["nodes"]) == {"2"}
    assert "routing" not in saved["execution"]
