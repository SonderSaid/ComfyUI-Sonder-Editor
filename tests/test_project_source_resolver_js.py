"""Frontend project-source resolution fixtures and consumer migration contracts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "web" / "js" / "project_source_resolver.js"
BRIDGES = ROOT / "web" / "js" / "bridge_nodes.js"
EXTENSION = ROOT / "web" / "js" / "extension.js"


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_resolver_physical_reroute_map_ids_cycles_and_ambiguity():
    module_url = RESOLVER.as_uri()
    payload = _run_node(
        f"""
const {{ resolveProjectSource }} = await import({json.dumps(module_url)});

function graph(useMap = false) {{
    const g = {{
        _nodes: [],
        links: useMap ? new Map() : {{}},
        getNodeById(id) {{ return this._nodes.find((node) => String(node.id) === String(id)); }},
        getLink(id) {{
            if (this.links instanceof Map) return this.links.get(id);
            return this.links[id];
        }},
    }};
    return g;
}}
function add(g, node) {{ node.graph = g; g._nodes.push(node); return node; }}
function editor(g, id) {{ return add(g, {{ id, type: "SonderEditor", outputs: [{{ type: "SONDER_PROJECT" }}] }}); }}
function target(g, id, link) {{
    return add(g, {{ id, type: "Consumer", inputs: [{{ name: "project", type: "SONDER_PROJECT", link }}] }});
}}
function link(g, id, origin, targetNode, targetSlot = 0, type = "SONDER_PROJECT") {{
    const value = {{ id, origin_id: origin.id, origin_slot: 0, target_id: targetNode.id, target_slot: targetSlot, type }};
    if (g.links instanceof Map) g.links.set(Number(id), value);
    else g.links[id] = value;
    return value;
}}

const directGraph = graph();
const directEditor = editor(directGraph, 1);
const directTarget = target(directGraph, 2, 10);
link(directGraph, 10, directEditor, directTarget);
const direct = resolveProjectSource(directTarget);
const wrongOutputEditor = add(directGraph, {{
    id: "wrong-output-editor",
    type: "SonderEditor",
    outputs: [{{ type: "IMAGE" }}],
}});
const wrongOutputTarget = target(directGraph, "wrong-output-target", 14);
link(directGraph, 14, wrongOutputEditor, wrongOutputTarget, 0, "*");
const wrongOutput = resolveProjectSource(wrongOutputTarget);
const disconnected = resolveProjectSource(add(directGraph, {{
    id: "disconnected",
    inputs: [{{ name: "project", type: "SONDER_PROJECT", link: null }}],
}}));

const rerouteGraph = graph(true);
const rerouteEditor = editor(rerouteGraph, "editor");
const reroute1 = add(rerouteGraph, {{
    id: "r1",
    type: "Reroute",
    inputs: [{{ type: "*", link: "11" }}],
    outputs: [{{ type: "SONDER_PROJECT" }}],
}});
const reroute2 = add(rerouteGraph, {{
    id: "r2",
    type: "Reroute",
    inputs: [{{ type: "SONDER_PROJECT", link: 12 }}],
    outputs: [{{ type: "*" }}],
}});
const rerouteTarget = target(rerouteGraph, "target", "13");
link(rerouteGraph, 11, rerouteEditor, reroute1);
link(rerouteGraph, 12, reroute1, reroute2);
link(rerouteGraph, 13, reroute2, rerouteTarget);
const reroute = resolveProjectSource(rerouteTarget);
const bounded = resolveProjectSource(rerouteTarget, {{ maxHops: 1 }});

const cycleGraph = graph();
const cycle1 = add(cycleGraph, {{
    id: "c1", type: "Reroute",
    inputs: [{{ type: "SONDER_PROJECT", link: 21 }}],
    outputs: [{{ type: "SONDER_PROJECT" }}],
}});
const cycle2 = add(cycleGraph, {{
    id: "c2", type: "Reroute",
    inputs: [{{ type: "SONDER_PROJECT", link: 22 }}],
    outputs: [{{ type: "SONDER_PROJECT" }}],
}});
const cycleTarget = target(cycleGraph, "ct", 23);
link(cycleGraph, 21, cycle2, cycle1);
link(cycleGraph, 22, cycle1, cycle2);
link(cycleGraph, 23, cycle1, cycleTarget);
const cycle = resolveProjectSource(cycleTarget);

const ambiguousGraph = graph();
const a1 = editor(ambiguousGraph, "a1");
const a2 = editor(ambiguousGraph, "a2");
const fork = add(ambiguousGraph, {{
    id: "fork", type: "Pass",
    inputs: [
        {{ type: "SONDER_PROJECT", link: 31 }},
        {{ type: "SONDER_PROJECT", link: 32 }},
    ],
    outputs: [{{ type: "SONDER_PROJECT" }}],
}});
const ambiguousTarget = target(ambiguousGraph, "at", 33);
link(ambiguousGraph, 31, a1, fork, 0);
link(ambiguousGraph, 32, a2, fork, 1);
link(ambiguousGraph, 33, fork, ambiguousTarget);
const ambiguous = resolveProjectSource(ambiguousTarget);

console.log(JSON.stringify({{
    direct: {{ status: direct.status, id: direct.editor?.id, route: direct.route }},
    wrongOutput: {{ status: wrongOutput.status, reason: wrongOutput.reason }},
    disconnected: {{ status: disconnected.status, reason: disconnected.reason }},
    reroute: {{ status: reroute.status, id: reroute.editor?.id, route: reroute.route }},
    bounded: {{ status: bounded.status, reason: bounded.reason }},
    cycle: {{ status: cycle.status, reason: cycle.reason }},
    ambiguous: {{ status: ambiguous.status, reason: ambiguous.reason }},
}}));
"""
    )

    assert payload["direct"]["status"] == "resolved"
    assert payload["direct"]["id"] == 1
    assert "physical" in payload["direct"]["route"]
    assert payload["wrongOutput"]["status"] == "unsupported"
    assert "not a SONDER_PROJECT output" in payload["wrongOutput"]["reason"]
    assert payload["disconnected"]["status"] == "none"
    assert "disconnected" in payload["disconnected"]["reason"]
    assert payload["reroute"]["status"] == "resolved"
    assert payload["reroute"]["id"] == "editor"
    assert payload["reroute"]["route"].count("pass-through") == 2
    assert payload["bounded"]["status"] == "error"
    assert "exceeded 1 hops" in payload["bounded"]["reason"]
    assert payload["cycle"]["status"] == "error"
    assert "Cycle" in payload["cycle"]["reason"]
    assert payload["ambiguous"]["status"] == "ambiguous"
    assert "multiple" in payload["ambiguous"]["reason"]


def test_resolver_kj_host_fallback_scopes_and_physical_precedence():
    module_url = RESOLVER.as_uri()
    payload = _run_node(
        f"""
const {{ resolveProjectSource }} = await import({json.dumps(module_url)});

function graph(parentGraph = null) {{
    return {{
        parentGraph,
        _nodes: [],
        links: {{}},
        getNodeById(id) {{ return this._nodes.find((node) => String(node.id) === String(id)); }},
        getLink(id) {{ return this.links[id]; }},
    }};
}}
function add(g, node) {{ node.graph = g; g._nodes.push(node); return node; }}
function editor(g, id) {{ return add(g, {{ id, type: "SonderEditor", outputs: [{{ type: "SONDER_PROJECT" }}] }}); }}
function wire(g, id, origin, target, targetSlot = 0) {{
    g.links[id] = {{
        id, origin_id: origin.id, origin_slot: 0,
        target_id: target.id, target_slot: targetSlot, type: "SONDER_PROJECT",
    }};
}}
function setter(g, id, name, source, linkId) {{
    const node = add(g, {{
        id, type: "SetNode",
        widgets: [{{ name: "Constant", value: name }}],
        inputs: [{{ type: "SONDER_PROJECT", link: linkId }}],
        outputs: [{{ type: "SONDER_PROJECT" }}],
    }});
    wire(g, linkId, source, node);
    return node;
}}
function getter(g, id, name) {{
    return add(g, {{
        id, type: "GetNode", isVirtualNode: true,
        widgets: [{{ name: "Constant", value: name }}],
        outputs: [{{ type: "SONDER_PROJECT" }}],
    }});
}}
function target(g, id, source, linkId) {{
    const node = add(g, {{
        id, type: "Consumer",
        inputs: [{{ name: "project", type: "SONDER_PROJECT", link: linkId }}],
    }});
    wire(g, linkId, source, node);
    return node;
}}

const sameGraph = graph();
const sameEditor = editor(sameGraph, "same-editor");
setter(sameGraph, "same-set", "project-a", sameEditor, 1);
const same = resolveProjectSource(target(sameGraph, "same-target", getter(sameGraph, "same-get", "project-a"), 2));

const duplicateGraph = graph();
const duplicateEditor = editor(duplicateGraph, "duplicate-editor");
setter(duplicateGraph, "dup-set-1", "dup", duplicateEditor, 11);
setter(duplicateGraph, "dup-set-2", "dup", duplicateEditor, 12);
const duplicate = resolveProjectSource(target(duplicateGraph, "dup-target", getter(duplicateGraph, "dup-get", "dup"), 13));

const missingGraph = graph();
const missing = resolveProjectSource(target(missingGraph, "missing-target", getter(missingGraph, "missing-get", "missing"), 21));

const root = graph();
const ancestorEditor = editor(root, "ancestor-editor");
setter(root, "ancestor-set", "ancestor", ancestorEditor, 31);
const child = graph(root);
const ancestor = resolveProjectSource(target(child, "ancestor-target", getter(child, "ancestor-get", "ancestor"), 32));

const hostGraph = graph();
const hostEditor = editor(hostGraph, "host-editor");
setter(hostGraph, "host-set", "host-name", hostEditor, 40);
const hostGet = getter(hostGraph, "host-get", "host-name");
hostGet.resolveInput = () => ({{ node: hostEditor, slot: 0 }});
const host = resolveProjectSource(target(hostGraph, "host-target", hostGet, 41));

const physicalGraph = graph();
const physicalEditor = editor(physicalGraph, "physical-editor");
const conflictingEditor = editor(physicalGraph, "host-conflict");
const reroute = add(physicalGraph, {{
    id: "physical-reroute", type: "Reroute",
    inputs: [{{ type: "SONDER_PROJECT", link: 51 }}],
    outputs: [{{ type: "SONDER_PROJECT" }}],
    resolveInput: () => ({{ node: conflictingEditor, slot: 0 }}),
}});
wire(physicalGraph, 51, physicalEditor, reroute);
const physical = resolveProjectSource(target(physicalGraph, "physical-target", reroute, 52));

const multiGraph = graph();
const multiA = editor(multiGraph, "multi-a");
const multiB = editor(multiGraph, "multi-b");
const virtual = add(multiGraph, {{
    id: "virtual", type: "VirtualProject",
    outputs: [{{ type: "*" }}],
    resolveInput: () => [{{ node: multiA }}, {{ node: multiB }}],
}});
const multiple = resolveProjectSource(target(multiGraph, "multi-target", virtual, 61));

console.log(JSON.stringify({{
    same: {{ status: same.status, id: same.editor?.id, route: same.route }},
    duplicate: {{ status: duplicate.status, reason: duplicate.reason }},
    missing: {{ status: missing.status, reason: missing.reason }},
    ancestor: {{ status: ancestor.status, id: ancestor.editor?.id }},
    host: {{ status: host.status, id: host.editor?.id, route: host.route }},
    physical: {{ status: physical.status, id: physical.editor?.id }},
    multiple: {{ status: multiple.status, candidates: multiple.candidates.length }},
}}));
"""
    )

    assert payload["same"]["status"] == "resolved"
    assert payload["same"]["id"] == "same-editor"
    assert "kj:project-a" in payload["same"]["route"]
    assert payload["duplicate"]["status"] == "ambiguous"
    assert "multiple setters" in payload["duplicate"]["reason"]
    assert payload["missing"]["status"] == "ambiguous"
    assert "no setter" in payload["missing"]["reason"]
    assert payload["ancestor"] == {"status": "resolved", "id": "ancestor-editor"}
    assert payload["host"]["status"] == "resolved"
    assert payload["host"]["id"] == "host-editor"
    assert "kj-host" in payload["host"]["route"]
    assert payload["physical"] == {"status": "resolved", "id": "physical-editor"}
    assert payload["multiple"] == {"status": "ambiguous", "candidates": 2}


def test_named_consumers_use_shared_resolver_without_upstream_scanning():
    bridge_source = BRIDGES.read_text(encoding="utf-8")
    extension_source = EXTENSION.read_text(encoding="utf-8")

    assert 'from "./project_source_resolver.js"' in bridge_source
    assert bridge_source.count("resolveProjectSource(node)") >= 4
    assert "projectResolutionStatusText(resolution)" in bridge_source
    assert "linkedNodeFromInput" not in bridge_source

    assert 'from "./project_source_resolver.js"' in extension_source
    assert "function resolvedEditorNodes" in extension_source
    assert 'resolvedEditorNodes(saveNode, "Save Video editor refresh"' in extension_source
    assert '"Save Bridge folder lookup"' in extension_source
    assert '"Save Bridge execution tracking"' in extension_source
    assert "logProjectResolutionDiagnostic(resolution" in extension_source
    assert "collectUpstreamEditorNodes" not in extension_source


def test_background_diagnostic_is_logged_once_per_resolution_context():
    module_url = RESOLVER.as_uri()
    payload = _run_node(
        f"""
const {{ logProjectResolutionDiagnostic }} = await import({json.dumps(module_url)});
const messages = [];
console.warn = (message) => messages.push(message);
const resolution = {{ status: "ambiguous", reason: "duplicate setter" }};
const node = {{ id: 7 }};
logProjectResolutionDiagnostic(resolution, {{ context: "refresh", node }});
logProjectResolutionDiagnostic(resolution, {{ context: "refresh", node }});
logProjectResolutionDiagnostic(resolution, {{ context: "folder", node }});
logProjectResolutionDiagnostic({{ status: "resolved" }}, {{ context: "refresh", node }});
console.log(JSON.stringify(messages));
"""
    )
    assert len(payload) == 2
    assert "ambiguous" in payload[0]
    assert "duplicate setter" in payload[0]
