import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Reference coordinator coverage")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


def test_reference_coordinator_joins_one_generation_and_keeps_only_latest_trailing():
    module_url = (ROOT / "web/js/bridge_reference_coordinator.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const gates = [];
const calls = [];
const gate = () => {{
  let resolve, reject;
  const promise = new Promise((yes, no) => {{ resolve = yes; reject = no; }});
  return {{ promise, resolve, reject }};
}};
const coordinator = mod.createBridgeReferenceCoordinator({{
  request(meta) {{
    calls.push(meta);
    const pending = gate();
    gates.push(pending);
    return pending.promise;
  }},
}});
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const same = Array.from({{ length: 6 }}, () => coordinator.request({{
  url: "/same?selection=1", generation: "g1", origin: "startup",
}}));
await tick();
const later2 = coordinator.request({{ url: "/same?selection=1", generation: "g2", origin: "version" }});
const later3 = coordinator.request({{ url: "/same?selection=1", generation: "g3", origin: "version" }});
const later4 = coordinator.request({{ url: "/same?selection=1", generation: "g4", origin: "version" }});
await tick();
const callsBeforeActiveSettled = calls.length;
gates[0].resolve({{ served: "g1" }});
await tick();
const secondGeneration = calls[1].generation;
gates[1].resolve({{ served: "g4" }});
const values = await Promise.all([...same, later2, later3, later4]);
await tick();
console.log(JSON.stringify({{
  callsBeforeActiveSettled,
  callGenerations: calls.map((call) => call.generation),
  values: values.map((value) => value.served),
  size: coordinator._debugSize(),
}}));
"""
    assert _run_node(script) == {
        "callsBeforeActiveSettled": 1,
        "callGenerations": ["g1", "g4"],
        "values": ["g1"] * 6 + ["g4"] * 3,
        "size": 0,
    }


def test_reference_coordinator_rejection_is_generation_scoped_and_urls_do_not_join():
    module_url = (ROOT / "web/js/bridge_reference_coordinator.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const gates = [];
const calls = [];
const gate = () => {{
  let resolve, reject;
  const promise = new Promise((yes, no) => {{ resolve = yes; reject = no; }});
  return {{ promise, resolve, reject }};
}};
const coordinator = mod.createBridgeReferenceCoordinator({{
  request(meta) {{
    calls.push(meta);
    const pending = gate();
    gates.push(pending);
    return pending.promise;
  }},
}});
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const failed = coordinator.request({{ url: "/one", generation: "g1", origin: "startup" }});
const trailing = coordinator.request({{ url: "/one", generation: "g2", origin: "version" }});
const independent = coordinator.request({{ url: "/two", generation: "g1", origin: "startup" }});
const settlement = Promise.allSettled([failed, trailing, independent]);
await tick();
gates[0].reject(new Error("first failed"));
gates[1].resolve({{ served: "two" }});
await tick();
gates[2].resolve({{ served: "trailing" }});
const settled = await settlement;
await tick();
console.log(JSON.stringify({{
  calls: calls.map((call) => [call.url, call.generation]),
  settled: settled.map((value) => value.status === "fulfilled"
    ? value.value.served : value.reason.message),
  size: coordinator._debugSize(),
}}));
"""
    assert _run_node(script) == {
        "calls": [["/one", "g1"], ["/two", "g1"], ["/one", "g2"]],
        "settled": ["first failed", "trailing", "two"],
        "size": 0,
    }


def test_reference_coordinator_default_dispatch_has_diagnostics_and_never_aborts_shared_work():
    module_url = (ROOT / "web/js/bridge_reference_coordinator.js").as_uri()
    script = f"""
const calls = [];
globalThis.fetch = async (url, options = {{}}) => {{
  calls.push({{ url, options }});
  return {{ ok: true, status: 200, json: async () => ({{ references: [] }}) }};
}};
const mod = await import({json.dumps(module_url)});
const coordinator = mod.createBridgeReferenceCoordinator();
await Promise.all([
  coordinator.request({{ url: "/shape?a=1", generation: "same", origin: "startup" }}),
  coordinator.request({{ url: "/shape?a=1", generation: "same", origin: "startup" }}),
]);
console.log(JSON.stringify({{
  callCount: calls.length,
  headers: calls[0].options.headers,
  hasSignal: Object.prototype.hasOwnProperty.call(calls[0].options, "signal"),
  size: coordinator._debugSize(),
}}));
"""
    result = _run_node(script)
    assert result["callCount"] == 1
    assert result["headers"]["X-Sonder-Reference-Generation"] == "same"
    assert result["headers"]["X-Sonder-Reference-Origin"] == "startup"
    assert result["headers"]["X-Sonder-Reference-Request-Id"].startswith("reference-")
    assert result["hasSignal"] is False
    assert result["size"] == 0


def test_reference_coordinator_evicts_success_rejection_and_many_distinct_urls():
    module_url = (ROOT / "web/js/bridge_reference_coordinator.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
let calls = 0;
const coordinator = mod.createBridgeReferenceCoordinator({{
  request(meta) {{
    calls += 1;
    if (meta.url === "/reject") return Promise.reject(new Error("no"));
    return Promise.resolve({{ url: meta.url }});
  }},
}});
await Promise.all(Array.from({{ length: 200 }}, (_, index) => coordinator.request({{
  url: `/resource/${{index}}`, generation: `g${{index}}`, origin: "cycle",
}})));
const afterSuccess = coordinator._debugSize();
await Promise.allSettled([
  coordinator.request({{ url: "/reject", generation: "bad", origin: "cycle" }}),
]);
console.log(JSON.stringify({{
  calls,
  afterSuccess,
  afterRejection: coordinator._debugSize(),
}}));
"""
    assert _run_node(script) == {
        "calls": 201,
        "afterSuccess": 0,
        "afterRejection": 0,
    }


def test_reference_extension_routes_all_six_node_refresh_origins_through_real_scheduler(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Reference extension integration coverage")

    bridge_source = (ROOT / "web/js/reference_bridge.js").read_text(encoding="utf-8")
    modules = {
        "app.mjs": """
export const app = {
  graph: null,
  registerExtension(extension) { globalThis.__extension = extension; },
};
""",
        "api.mjs": "export const api = { apiURL: (value) => value };\n",
        "events.mjs": """
export function onEditorRenderWindowChanged(callback) {
  globalThis.__renderWindowChanged = callback;
  return () => {};
}
""",
        "keyboard.mjs": """
export const PRIORITY = { OVERLAY: 100 };
export const register = () => () => {};
""",
        "resolver.mjs": """
const keyed = (value, key) => value?.[key] ?? value?.[String(key)] ?? null;
export const getGraphLink = (graph, id) => graph?.getLink?.(id) ?? keyed(graph?.links, id);
export const getGraphNode = (graph, id) => graph?.getNodeById?.(id)
  ?? keyed(graph?._nodes_by_id, id)
  ?? null;
export const resolveProjectSource = () => ({
  status: "resolved",
  editor: { _sonderController: globalThis.__controller },
});
""",
    }
    for name, source in modules.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    client_source = (ROOT / "web/js/api_client.js").read_text(encoding="utf-8")
    client_source = client_source.replace(
        '"/scripts/api.js"', json.dumps((tmp_path / "api.mjs").as_uri()))
    (tmp_path / "client.mjs").write_text(client_source, encoding="utf-8")
    replacements = {
        "/scripts/app.js": (tmp_path / "app.mjs").as_uri(),
        "/scripts/api.js": (tmp_path / "api.mjs").as_uri(),
        "./api_client.js": (tmp_path / "client.mjs").as_uri(),
        "./editor_render_window_events.js": (tmp_path / "events.mjs").as_uri(),
        "./keyboard_ownership.js": (tmp_path / "keyboard.mjs").as_uri(),
        "./project_source_resolver.js": (tmp_path / "resolver.mjs").as_uri(),
        "./reference_bridge_shape.js": (ROOT / "web/js/reference_bridge_shape.js").as_uri(),
        "./bridge_reference_coordinator.js": (
            ROOT / "web/js/bridge_reference_coordinator.js"
        ).as_uri(),
    }
    for old, new in replacements.items():
        bridge_source = bridge_source.replace(f'"{old}"', json.dumps(new))
    bridge_path = tmp_path / "reference_bridge.mjs"
    bridge_path.write_text(bridge_source, encoding="utf-8")

    script = f"""
class Element {{
  constructor(tag) {{
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parentElement = null;
    this.style = {{ cssText: "" }}; this.attributes = {{}}; this._handlers = {{}};
    this.textContent = ""; this.title = ""; this.disabled = false; this.type = "";
  }}
  appendChild(child) {{ this.children.push(child); child.parentElement = this; return child; }}
  append(...children) {{ children.forEach((child) => this.appendChild(child)); }}
  replaceChildren(...children) {{ this.children = []; this.append(...children); }}
  addEventListener(type, handler) {{ (this._handlers[type] ||= []).push(handler); }}
  setAttribute(name, value) {{ this.attributes[name] = String(value); }}
  contains(target) {{
    for (let value = target; value; value = value.parentElement) if (value === this) return true;
    return false;
  }}
  click() {{ for (const handler of this._handlers.click || []) handler({{ target: this }}); }}
}}
globalThis.document = {{ createElement: (tag) => new Element(tag) }};
globalThis.window = {{
  setTimeout,
  clearTimeout,
  addEventListener() {{}},
  removeEventListener() {{}},
}};
const readyCallbacks = [];
globalThis.__controller = {{
  state: {{
    projectDir: "",
    sceneId: "scene-1",
    selectionStart: 0,
    selectionEnd: 20,
    preContextFrames: 0,
    postContextFrames: 0,
  }},
  whenProjectReady(callback) {{ readyCallbacks.push(callback); }},
}};
const fetches = [];
let responseLabel = "One";
let responseSlotCount = 1;
let holdNextFetch = false;
let releaseHeldFetch = null;
const makeResponse = (label, slotCount) => ({{
  ok: true,
  status: 200,
  json: async () => ({{
    scene_name: "Nightmare (copy)",
    source: "live",
    references: [
      {{ lane_index: 0, lane_name: label, recipe_name: "Slots",
         image_slot_count: slotCount, audio_slot_count: 0,
         prompt_slot_count: 0, live_outputs: ["image_slots"],
         slot_labels: Array.from({{ length: slotCount }}, (_, index) => `slot-${{index}}`),
         image_slot_labels: Array.from({{ length: slotCount }}, (_, index) => `slot-${{index}}`),
         reserved_member_span: slotCount, member_tags: [] }},
      {{ lane_index: 1, lane_name: "Two", recipe_name: "Slots",
         image_slot_count: 2, audio_slot_count: 0,
         prompt_slot_count: 0, live_outputs: ["image_slots"], slot_labels: ["two-a", "two-b"],
         image_slot_labels: ["two-a", "two-b"], reserved_member_span: 2, member_tags: [] }},
    ],
  }}),
}});
globalThis.fetch = (url, options = {{}}) => {{
  fetches.push({{ url, headers: options.headers || {{}} }});
  const response = makeResponse(responseLabel, responseSlotCount);
  if (!holdNextFetch) return Promise.resolve(response);
  holdNextFetch = false;
  return new Promise((resolve) => {{
    releaseHeldFetch = () => {{
      releaseHeldFetch = null;
      resolve(response);
    }};
  }});
}};
const {{ app }} = await import({json.dumps((tmp_path / "app.mjs").as_uri())});
const client = await import({json.dumps((tmp_path / "client.mjs").as_uri())});
await import({json.dumps(bridge_path.as_uri())});
const extension = globalThis.__extension;
extension.setup();

const selectors = [0, 1, 2].map((index) => {{
  const laneWidget = {{ name: "reference_lanes", value: ["0", "1", "0, 1"][index], callback() {{}} }};
  return {{
    id: index + 1,
    type: "SonderReferenceSelector",
    comfyClass: "SonderReferenceSelector",
    widgets: [laneWidget],
    inputs: [],
    outputs: [{{ name: "reference_set", links: [100 + index] }}],
    addDOMWidget(name, type, element, options) {{
      this.panel = element;
      return {{ computeSize() {{}}, ...options }};
    }},
    computeSize() {{ return [280, 120]; }},
    setSize() {{}},
  }};
}});
const bridges = [0, 1, 2].map((index) => {{
  const unused = {{ name: "unused_slots", value: "placeholder", callback() {{}} }};
  return {{
    id: index + 10,
    type: "SonderReferenceImageBridge",
    comfyClass: "SonderReferenceImageBridge",
    widgets: [unused],
    inputs: [{{ name: "reference_set", link: 100 + index }}],
    outputs: Array.from({{ length: 16 }}, (_, slot) => ({{
      name: `r${{String(slot + 1).padStart(2, "0")}}`, type: "IMAGE", links: [],
    }})),
    addOutput(name, type, options) {{ this.outputs.push({{ name, type, links: [], ...options }}); }},
    removeOutput(indexToRemove) {{ this.outputs.splice(indexToRemove, 1); }},
    computeSize() {{ return [280, 120]; }},
    setSize() {{}},
  }};
}});
const nodes = [...selectors, ...bridges];
const byId = Object.fromEntries(nodes.map((item) => [item.id, item]));
const links = Object.fromEntries(bridges.map((bridge, index) => [
  100 + index,
  {{ origin_id: selectors[index].id, target_id: bridge.id, target_slot: 0 }},
]));
const graph = {{
  _nodes: nodes,
  _nodes_by_id: byId,
  links,
  getLink(id) {{ return links[id] || null; }},
  getNodeById(id) {{ return byId[id] || null; }},
  setDirtyCanvas() {{}},
}};
app.graph = graph;
for (const node of nodes) {{
  node.graph = graph;
  extension.loadedGraphNode(node);
}}
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
await wait(20);
const beforeReady = fetches.length;
globalThis.__controller.state.projectDir = "C:/projects/Project-Minimax-References-Sample";
for (const callback of readyCallbacks) callback();
await wait(20);
const afterReady = fetches.length;
const bridgeCounts = bridges.map((bridge) =>
  bridge.outputs.filter((slot) => /^r\\d{{2}}$/.test(slot.name)).length);

// One real healing payload emits canonical UUID and folder-alias notifications.
// They are one logical version wave and must still produce one physical URL.
client.rememberProjectVersionFromPayload({{
  project: {{ project_id: "canonical-project-uuid", modified_at: "2026-08-30T12:00:00" }},
}}, "Project-Minimax-References-Sample");
await wait(280);
const afterVersion = fetches.length;
globalThis.__renderWindowChanged({{ field: "selection_start" }});
await wait(280);
const afterWindow = fetches.length;
for (const node of nodes) node.onConnectionsChange?.();
await wait(20);
const afterConnections = fetches.length;
for (const selector of selectors) selector.widgets[0].callback("changed");
for (const bridge of bridges) bridge.widgets[0].callback("changed");
await wait(20);
const afterWidgets = fetches.length;

const descendants = (root) => [root, ...root.children.flatMap(descendants)];
const refreshButton = descendants(selectors[0].panel)
  .find((element) => element.tagName === "BUTTON" && element.textContent === "Refresh");

responseLabel = "Selector pre-click";
holdNextFetch = true;
selectors[0].onConnectionsChange();
await wait(20);
const selectorActive = fetches.length;
responseLabel = "Selector post-click";
refreshButton.click();
await wait(20);
const selectorWhileHeld = fetches.length;
const selectorAppliedPreClick = descendants(selectors[0].panel)
  .some((element) => String(element.textContent).startsWith("Selector pre-click"));
releaseHeldFetch();
await wait(20);
const afterSelectorRefresh = fetches.length;
const selectorAppliedPostClick = descendants(selectors[0].panel)
  .some((element) => String(element.textContent).startsWith("Selector post-click"));

const options = [];
bridges[0].getExtraMenuOptions?.(null, options);
responseSlotCount = 1;
holdNextFetch = true;
bridges[0].onConnectionsChange();
await wait(20);
const bridgeActive = fetches.length;
responseSlotCount = 4;
options.find((option) => option.content === "Refresh reference slots").callback();
await wait(20);
const bridgeWhileHeld = fetches.length;
releaseHeldFetch();
await wait(20);
const afterBridgeRefresh = fetches.length;
const bridgePostClickCount = bridges[0].outputs
  .filter((slot) => /^r\\d{{2}}$/.test(slot.name)).length;

console.log(JSON.stringify({{
  readyCallbacks: readyCallbacks.length,
  counts: {{
    beforeReady, afterReady, afterVersion, afterWindow, afterConnections,
    afterWidgets, selectorActive, selectorWhileHeld, afterSelectorRefresh,
    bridgeActive, bridgeWhileHeld, afterBridgeRefresh,
  }},
  bridgeCounts,
  selectorAppliedPreClick,
  selectorAppliedPostClick,
  bridgePostClickCount,
  diagnosticHeaders: fetches.every((entry) =>
    entry.headers["X-Sonder-Reference-Request-Id"]
    && entry.headers["X-Sonder-Reference-Generation"]
    && entry.headers["X-Sonder-Reference-Origin"]),
  urls: [...new Set(fetches.map((entry) => entry.url))],
}}));
"""
    result = _run_node(script)
    assert result["readyCallbacks"] == 6
    assert result["counts"] == {
        "beforeReady": 0,
        "afterReady": 1,
        "afterVersion": 2,
        "afterWindow": 3,
        "afterConnections": 4,
        "afterWidgets": 5,
        "selectorActive": 6,
        "selectorWhileHeld": 6,
        "afterSelectorRefresh": 7,
        "bridgeActive": 8,
        "bridgeWhileHeld": 8,
        "afterBridgeRefresh": 9,
    }
    assert result["bridgeCounts"] == [1, 2, 3]
    assert result["selectorAppliedPreClick"] is False
    assert result["selectorAppliedPostClick"] is True
    assert result["bridgePostClickCount"] == 4
    assert result["diagnosticHeaders"] is True
    assert result["urls"] == [
        "/sonder-editor/project/Project-Minimax-References-Sample/scenes/scene-1/"
        "bridge-references?selection_start=0&selection_end=20"
        "&pre_context_frames=0&post_context_frames=0"
    ]
