"""Phase 1B: the routing compiler replays ComfyUI's serializer for node modes.

Native rules (frontend 1.57.1 ``ExecutableNodeDTO``, ``executionUtil``) are
pinned here on hand-made capsules; the live oracle (graphToPrompt under real
modes against compile_modes on the all-enabled export) is a roadmap row and a
probe, since it needs the installed frontend.
"""

import copy

import pytest

from server import control_routing as cr


def test_is_valid_connection_follows_litegraph():
    assert cr.is_valid_connection("IMAGE", "IMAGE")
    assert cr.is_valid_connection("IMAGE", "image")
    assert cr.is_valid_connection("IMAGE,MASK", "MASK")
    assert cr.is_valid_connection("*", "LATENT") and cr.is_valid_connection("", "LATENT") and cr.is_valid_connection(None, "X")
    assert not cr.is_valid_connection("IMAGE", "LATENT")
    assert not cr.is_valid_connection("IMAGE,MASK", "LATENT,MODEL")
    # An empty piece of a list is a wildcard, as in the native split.
    assert cr.is_valid_connection("IMAGE,", "LATENT")


@pytest.mark.parametrize("inputs,output,slot,target,expected", [
    (["IMAGE"], "IMAGE", 0, "*", 0),                    # wildcard: the same slot
    (["IMAGE"], "IMAGE", 3, "", 0),                     # wildcard past the inputs: the first
    (["MASK", "IMAGE"], "IMAGE", 1, "IMAGE", 1),        # same index wins when valid
    (["MASK", "IMAGE"], "IMAGE", 0, "IMAGE", 1),        # else the exact type
    (["IMAGE,MASK", "LATENT"], "MASK", 1, "MASK", 0),   # else the first valid both ways
    (["LATENT"], "IMAGE", 0, "IMAGE", -1),              # none: bypass not possible
    (["*", "IMAGE"], "IMAGE", 0, "IMAGE", 0),           # a wildcard input is valid
])
def test_bypass_index(inputs, output, slot, target, expected):
    assert cr.bypass_index(inputs, output, slot, target) == expected


def capsule(nodes, edges):
    routing, problem = cr.read_routing({"v": 1, "nodes": nodes, "edges": edges})
    assert problem is None, problem
    return routing


# Load(1) -> Invert(2) -> Blur(4) -> Save(3); Save also has a widget "strength".
PROMPT = {
    "1": {"class_type": "Load", "inputs": {"name": "a.png"}},
    "2": {"class_type": "Invert", "inputs": {"image": ["1", 0]}},
    "4": {"class_type": "Blur", "inputs": {"image": ["2", 0], "radius": 3}},
    "3": {"class_type": "Save", "inputs": {"images": ["4", 0], "strength": ["2", 0]}},
}
CHAIN = capsule(
    {"2": {"i": [["image", "IMAGE", {"a": ["1", 0]}]], "o": [["IMAGE"]]},
     "4": {"i": [["image", "IMAGE", {"m": ["2", 0, "IMAGE"]}], ["radius", "INT", None]], "o": [["IMAGE"]]}},
    [["4", "image", "2", 0, "IMAGE"], ["3", "images", "4", 0, "IMAGE"], ["3", "strength", "2", 0, "FLOAT", 0.5]])


def test_no_modes_is_the_export_itself():
    out = cr.compile_modes(PROMPT, CHAIN, {})
    assert out == PROMPT and out is not PROMPT


def test_bypass_passes_the_matching_input_through_a_chain():
    out = cr.compile_modes(PROMPT, CHAIN, {"2": "bypassed", "4": "bypassed"})
    assert set(out) == {"1", "3"}
    # No IMAGE input can feed a FLOAT target, so that input keeps its widget value.
    assert out["3"]["inputs"] == {"images": ["1", 0], "strength": 0.5}


def test_a_muted_origin_leaves_the_widget_value_or_nothing():
    out = cr.compile_modes(PROMPT, CHAIN, {"2": "muted"})
    assert "2" not in out
    # Blur's linked image has no widget, so it is left out; Save's strength
    # input falls back to its widget value.
    assert out["4"]["inputs"] == {"radius": 3}
    assert out["3"]["inputs"] == {"images": ["4", 0], "strength": 0.5}
    # Muted upstream of a bypass: nothing reaches Save.
    assert "images" not in cr.compile_modes(PROMPT, CHAIN, {"2": "muted", "4": "bypassed"})["3"]["inputs"]


def test_type_decides_which_input_a_bypass_passes():
    prompt = {"1": {"class_type": "M", "inputs": {}}, "5": {"class_type": "I", "inputs": {}},
              "2": {"class_type": "Comp", "inputs": {"mask": ["1", 0], "image": ["5", 0]}},
              "3": {"class_type": "Save", "inputs": {"images": ["2", 0]}}}
    routing = capsule({"2": {"i": [["mask", "MASK", {"a": ["1", 0]}], ["image", "IMAGE", {"a": ["5", 0]}]],
                             "o": [["IMAGE"]]}},
                      [["3", "images", "2", 0, "IMAGE"]])
    assert cr.compile_modes(prompt, routing, {"2": "bypassed"})["3"]["inputs"]["images"] == ["5", 0]
    # A wildcard target takes the same index: the mask.
    wild = capsule({"2": {"i": [["mask", "MASK", {"a": ["1", 0]}], ["image", "IMAGE", {"a": ["5", 0]}]], "o": [["IMAGE"]]}},
                   [["3", "images", "2", 0, "*"]])
    assert cr.compile_modes(prompt, wild, {"2": "bypassed"})["3"]["inputs"]["images"] == ["1", 0]


def test_an_instance_drops_its_inner_nodes_and_routes_through_its_ports():
    prompt = {"1": {"class_type": "Load", "inputs": {}},
              "10:5": {"class_type": "Invert", "inputs": {"image": ["1", 0]}},
              "10:6": {"class_type": "Preview", "inputs": {"images": ["10:5", 0]}},
              "3": {"class_type": "Save", "inputs": {"images": ["10:5", 0]}}}
    routing = capsule({"10": {"i": [["pixels", "IMAGE", {"a": ["1", 0]}]], "o": [["IMAGE", [["IMAGE", {"a": ["10:5", 0]}]]]]}},
                      [["3", "images", "10", 0, "IMAGE"]])
    bypassed = cr.compile_modes(prompt, routing, {"10": "bypassed"})
    assert set(bypassed) == {"1", "3"} and bypassed["3"]["inputs"]["images"] == ["1", 0]
    muted = cr.compile_modes(prompt, routing, {"10": "muted"})
    assert set(muted) == {"1", "3"} and muted["3"]["inputs"] == {}
    # Enabled through a chain: the host's own inner origin.
    chained = capsule({"10": {"i": [["pixels", "IMAGE", {"a": ["1", 0]}]], "o": [["IMAGE", [["IMAGE", {"a": ["10:5", 0]}]]]]},
                       "4": {"i": [["image", "IMAGE", {"m": ["10", 0, "IMAGE"]}]], "o": [["IMAGE"]]}},
                      [["3", "images", "4", 0, "IMAGE"]])
    prompt["4"] = {"class_type": "Blur", "inputs": {"image": ["10:5", 0]}}
    prompt["3"]["inputs"]["images"] = ["4", 0]
    assert cr.compile_modes(prompt, chained, {"4": "bypassed"})["3"]["inputs"]["images"] == ["10:5", 0]


def test_a_promoted_widget_value_is_boxed_like_the_native_serializer():
    prompt = {"2": {"class_type": "X", "inputs": {}}, "3": {"class_type": "Y", "inputs": {"v": ["2", 0]}}}
    routing = capsule({"2": {"i": [["v", "*", {"w": [1, 2]}]], "o": [["*"]]}}, [["3", "v", "2", 0, "*"]])
    assert cr.compile_modes(prompt, routing, {"2": "bypassed"})["3"]["inputs"]["v"] == {"__value__": [1, 2]}


def test_dangling_links_go_and_consumers_inside_off_instances_go_with_them():
    prompt = copy.deepcopy(PROMPT)
    prompt["7"] = {"class_type": "Other", "inputs": {"x": ["2", 0]}}  # not in the capsule's edges
    out = cr.compile_modes(prompt, CHAIN, {"2": "muted"})
    assert out["7"]["inputs"] == {}


def test_loops_and_unreplayable_routes_refuse():
    loop = capsule({"2": {"i": [["a", "X", {"m": ["4", 0, "X"]}]], "o": [["X"]]},
                    "4": {"i": [["a", "X", {"m": ["2", 0, "X"]}]], "o": [["X"]]}},
                   [["3", "a", "2", 0, "X"]])
    prompt = {"3": {"class_type": "Y", "inputs": {"a": ["2", 0]}}, "2": {"class_type": "Z", "inputs": {}},
              "4": {"class_type": "Z", "inputs": {}}}
    with pytest.raises(cr.RoutingError):
        cr.compile_modes(prompt, loop, {"2": "bypassed", "4": "bypassed"})
    opaque = capsule({"10": {"i": [], "o": [["IMAGE", [["IMAGE", {"x": 1}]]]]},
                      "4": {"i": [["a", "IMAGE", {"m": ["10", 0, "IMAGE"]}]], "o": [["IMAGE"]]}},
                     [["3", "a", "4", 0, "IMAGE"]])
    with pytest.raises(cr.RoutingError):
        cr.compile_modes(prompt, opaque, {"4": "bypassed"})
    # Only a route a run reaches refuses: another switch muted, node 4 still
    # enabled, and nothing asks the host.
    elsewhere = capsule({"10": {"i": [], "o": [["IMAGE", [["IMAGE", {"x": 1}]]]]},
                         "4": {"i": [["a", "IMAGE", {"m": ["10", 0, "IMAGE"]}]], "o": [["IMAGE"]]},
                         "5": {"i": [], "o": [["X"]]}},
                        [["3", "a", "4", 0, "IMAGE"]])
    assert cr.compile_modes({**prompt, "3": {"class_type": "Y", "inputs": {"a": ["4", 0]}}}, elsewhere,
                            {"5": "muted"})["3"]["inputs"] == {"a": ["4", 0]}
    # A type the capsule never answered for refuses rather than guess.
    unanswered = capsule({"10": {"i": [], "o": [["IMAGE", [["MASK", {"a": ["10:5", 0]}]]]]},
                          "4": {"i": [["a", "IMAGE", {"m": ["10", 0, "IMAGE"]}]], "o": [["IMAGE"]]}},
                         [["3", "a", "4", 0, "IMAGE"]])
    with pytest.raises(cr.RoutingError):
        cr.compile_modes(prompt, unanswered, {"4": "bypassed"})
    with pytest.raises(cr.RoutingError):  # a switched node the capsule doesn't know
        cr.compile_modes(prompt, CHAIN, {"9": "muted"})


@pytest.mark.parametrize("raw,problem", [
    (None, "missing"),
    ({"v": 1, "error": "budget"}, "budget"),
    ({"v": 1, "error": "unsupported"}, "unsupported"),
    ({"v": 2, "nodes": {}, "edges": []}, "unsupported"),
    ({"v": True, "nodes": {}, "edges": []}, "unsupported"),
    ({"v": 1, "nodes": {"2": {"i": [["a", "X", {"q": 1}]], "o": []}}, "edges": []}, "unsupported"),
    ({"v": 1, "nodes": {}, "edges": [["3", "a", "2", 0, "X"]]}, "unsupported"),     # an edge to no member
    ({"v": 1, "nodes": {"__proto__": {"i": [], "o": []}}, "edges": []}, "unsupported"),
    ({"v": 1, "nodes": {f"n{i}": {"i": [], "o": []} for i in range(cr.MAX_ROUTING_NODES + 1)}, "edges": []}, "unsupported"),
])
def test_read_routing_is_tolerant_and_capped(raw, problem):
    assert cr.read_routing(raw) == (None, problem)


def test_an_imported_capsule_only_reroutes_links():
    # A capsule edge naming a literal (the Editor's project) changes nothing.
    prompt = {"1": {"class_type": "SonderEditor", "inputs": {"project": "Mine"}},
              "2": {"class_type": "X", "inputs": {}}, "3": {"class_type": "Y", "inputs": {"a": ["2", 0]}}}
    routing = capsule({"2": {"i": [], "o": [["X"]]}}, [["1", "project", "2", 0, "X", "Theirs"], ["3", "a", "2", 0, "X"]])
    out = cr.compile_modes(prompt, routing, {"2": "muted"})
    assert out["1"]["inputs"]["project"] == "Mine" and out["3"]["inputs"] == {}


def test_a_route_that_resolved_to_nothing_can_resolve_once_switched():
    # An instance's output came from an inner node muted in its definition:
    # the export left the consumer's input out (or its widget value). Bypassing
    # the instance routes its matching input there, as native does.
    prompt = {"1": {"class_type": "E", "inputs": {}},
              "3": {"class_type": "PV", "inputs": {}},
              "4": {"class_type": "W", "inputs": {"strength": 0.5}}}
    routing = capsule({"10": {"i": [["image", "IMAGE", {"a": ["1", 0]}]], "o": [["IMAGE", [["IMAGE", None]]]]}},
                      [["3", "images", "10", 0, "IMAGE"], ["4", "strength", "10", 0, "IMAGE", 0.5]])
    out = cr.compile_modes(prompt, routing, {"10": "bypassed"})
    assert out["3"]["inputs"] == {"images": ["1", 0]} and out["4"]["inputs"] == {"strength": ["1", 0]}
    # A literal other than what the export left is never the capsule's.
    prompt["4"]["inputs"]["strength"] = 0.9
    assert cr.compile_modes(prompt, routing, {"10": "bypassed"})["4"]["inputs"] == {"strength": 0.9}


def test_a_chain_too_deep_to_follow_refuses_cleanly():
    count = 3000
    nodes = {f"n{i}": {"i": [["a", "X", {"m": [f"n{i + 1}", 0, "X"]} if i + 1 < count else {"a": ["src", 0]}]], "o": [["X"]]}
             for i in range(count)}
    routing, problem = cr.read_routing({"v": 1, "nodes": nodes, "edges": [["c", "a", "n0", 0, "X"]]})
    if problem:  # past the node cap: the reader refuses it first
        assert problem == "unsupported"
        routing = {"nodes": {k: {"inputs": [tuple(p[:2]) + (p[2],) for p in v["i"]], "outputs": [("X", None, False)]}
                             for k, v in nodes.items()},
                   "edges": [{"consumer": "c", "input": "a", "member": "n0", "slot": 0, "type": "X", "fallback": None,
                              "has_fallback": False}]}
    prompt = {"c": {"class_type": "C", "inputs": {"a": ["n0", 0]}}}
    with pytest.raises(cr.RoutingError):
        cr.compile_modes(prompt, routing, {f"n{i}": "bypassed" for i in range(count)})


def test_the_capsule_travels_as_text():
    import json
    text = json.dumps({"v": 1, "nodes": {"2": {"i": [], "o": [["IMAGE"]]}}, "edges": []})
    assert cr.read_routing(text)[1] is None
    assert cr.read_routing("[" * 5000)[1] == "unsupported"
    assert cr.read_routing("x" * (cr.MAX_ROUTING_BYTES + 1))[1] == "budget"


def test_covers_names_every_member():
    assert cr.covers(CHAIN, ["2", "4"]) and not cr.covers(CHAIN, ["2", "9"]) and not cr.covers(None, [])
