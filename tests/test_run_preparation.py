"""Sonder App execution preparation (server/run_preparation.py, contract v1).

Graphs are synthetic API prompts. Whether ComfyUI accepts the prepared prompt
is checked through its real validator and executor in
``test_run_preparation_executor.py``.
"""

import copy
import importlib.util
from pathlib import Path

import pytest

from server import control_rows, run_preparation as rp

ROOT = Path(__file__).resolve().parents[1]


class _Out:
    OUTPUT_NODE = True


class _Plain:
    pass


KNOWN = {"Save": _Out, "Preview": _Out, "Src": _Plain, "Loader": _Plain, "SonderLazySwitch": _Plain,
         "SonderLazyCluster": _Plain, "SonderGate": _Plain, "SonderSelector": _Plain, "PrimitiveInt": _Plain,
         "PrimitiveBoolean": _Plain, "LoadLoraModel": _Plain, "LoadLoraTextEncoder": _Plain,
         "SonderControlRelay": _Plain, "SonderEditor": _Plain, "Use": _Plain}


def lookup(class_type):
    return KNOWN.get(class_type)


def n(class_type, **inputs):
    return {"class_type": class_type, "inputs": inputs}


def prep(prompt, **kwargs):
    kwargs.setdefault("class_lookup", lookup)
    return rp.prepare(prompt, **kwargs)


def switch_graph(select=1, select_input=None):
    return {
        "a": n("Src", value=0),
        "b": n("Loader", ckpt_name="missing.safetensors"),
        "x": n("NotInstalled", value=["b", 0]),
        "sw": n("SonderLazySwitch", select=select if select_input is None else select_input,
                **{"branches.item0": ["x", 0], "branches.item1": ["a", 0]}),
        "save": n("Save", value=["sw", 0]),
        "relay": n("SonderControlRelay", manifest="{}"),
    }


def test_switch_drops_unselected_branches_and_what_only_they_need():
    graph = switch_graph()
    before = copy.deepcopy(graph)
    result = prep(graph)
    assert graph == before  # never mutated
    assert result["fallback"] is None
    assert set(result["prompt"]) == {"a", "sw", "save", "relay"}
    assert result["prompt"]["sw"]["inputs"] == {"select": 1, "branches.item1": ["a", 0]}
    assert result["removed"] == ["b", "x"]
    assert result["cut"] == {"sw": ["branches.item0"]}
    # Idempotent: preparing the copy again changes nothing.
    again = prep(result["prompt"])
    assert again["prompt"] == result["prompt"] and again["removed"] == []


@pytest.mark.parametrize("select", [5, -1, "1", True, None])
def test_switch_with_an_unusable_select_keeps_every_branch(select):
    result = prep(switch_graph(select=select))
    assert set(result["prompt"]) == set(switch_graph())


def test_selector_and_primitive_resolve_a_select_but_other_nodes_do_not():
    graph = switch_graph(select_input=["sel", 1])
    graph["sel"] = n("SonderSelector", choice="Two", choice_list="One\nTwo\n")
    assert prep(graph)["prompt"]["sw"]["inputs"].get("branches.item0") is None
    graph["sel"] = n("SonderSelector", choice="Two", choice_list="One\nTwo\nTwo")  # duplicate: refused at run
    assert "branches.item0" in prep(graph)["prompt"]["sw"]["inputs"]
    graph = switch_graph(select_input=["p", 0])
    graph["p"] = n("PrimitiveInt", value=1)
    assert "branches.item0" not in prep(graph)["prompt"]["sw"]["inputs"]
    graph = switch_graph(select_input=["s", 0])
    graph["s"] = n("Src", value=1)  # an arbitrary node's value exists only at run time
    assert "branches.item0" in prep(graph)["prompt"]["sw"]["inputs"]


def test_a_seeded_primitive_or_volatile_select_is_unresolved():
    graph = switch_graph(select_input=["p", 0])
    graph["p"] = n("PrimitiveInt", value=1)
    result = prep(graph, volatile=[("p", "value")])
    assert "branches.item0" in result["prompt"]["sw"]["inputs"]
    result = prep(switch_graph(), volatile=[("sw", "select")])
    assert "branches.item0" in result["prompt"]["sw"]["inputs"]


def test_a_missing_selected_branch_is_left_to_native_validation():
    graph = switch_graph(select=2)
    assert prep(graph)["prompt"] == graph


def test_cluster_keeps_only_the_selected_branch_lanes():
    graph = {f"s{i}": n("Src", value=i) for i in range(4)}
    graph["cl"] = n("SonderLazyCluster", select=1, branches=2, lanes=2,
                    b0_l0=["s0", 0], b0_l1=["s1", 0], b1_l0=["s2", 0], b1_l1=["s3", 0])
    graph["save"] = n("Save", a=["cl", 0], b=["cl", 1])
    result = prep(graph)
    assert set(result["prompt"]["cl"]["inputs"]) == {"select", "branches", "lanes", "b1_l0", "b1_l1"}
    assert result["removed"] == ["s0", "s1"]
    graph["cl"]["inputs"].pop("b1_l1")  # selected branch incomplete: native refuses, cut nothing
    assert prep(graph)["prompt"]["cl"] == graph["cl"]
    graph["cl"]["inputs"].update(b1_l1=["s3", 0], select=2)  # outside the branch count
    assert prep(graph)["prompt"] == graph


@pytest.mark.parametrize("condition,closed", [
    (None, True), (False, True), (0, True), (0.0, True), (True, False), (1, False), ("", False), ("x", False),
])
def test_gate_lane_closure_follows_gate_open(condition, closed):
    graph = {"v": n("Loader", ckpt_name="missing.safetensors"),
             "g": n("SonderGate", value_A=["v", 0], when_A=condition), "save": n("Save", a=["g", 0])}
    result = prep(graph)
    assert ("value_A" not in result["prompt"]["g"]["inputs"]) is closed
    assert ("v" in result["prompt"]) is not closed


def test_gate_unwired_condition_closes_and_a_linked_one_is_kept():
    graph = {"v": n("Src", value=1), "c": n("Src", value=0),
             "g": n("SonderGate", value_A=["v", 0], value_B=["v", 0], when_B=["c", 0]),
             "save": n("Save", a=["g", 0], b=["g", 1])}
    result = prep(graph)
    assert set(result["prompt"]["g"]["inputs"]) == {"value_B", "when_B"}


def stack(rows, model=("m", 0)):
    inputs = {"model": list(model)}
    for index, row in enumerate(rows):
        inputs.update({f"loras.{index}.{field}": value for field, value in row.items()})
    return n("LoadLoraModel", **inputs)


def row(name="a.safetensors", strength=1.0, enabled=True):
    return {"lora_name": name, "strength": strength, "enabled": enabled}


def test_stack_drops_off_rows_and_packs_the_rest():
    graph = {"m": n("Src", value=0), "st": stack([row("off", enabled=False), row("b"), row("z", 0)]),
             "save": n("Save", model=["st", 0])}
    result = prep(graph)
    inputs = result["prompt"]["st"]["inputs"]
    assert {k: v for k, v in inputs.items() if k.startswith("loras.")} == {
        "loras.0.lora_name": "b", "loras.0.strength": 1.0, "loras.0.enabled": True}
    assert result["packed_rows"] == {"st": {"rows": [row("off", enabled=False), row("b"), row("z", 0)],
                                            "bypassed": False}}


def test_an_all_off_stack_is_bypassed_including_chains():
    graph = {"m": n("Src", value=0), "s1": stack([row("x", enabled=False)]),
             "s2": stack([row("y", 0)], model=("s1", 0)), "use": n("Use", model=["s2", 0]),
             "save": n("Save", model=["use", 0])}
    result = prep(graph)
    assert result["prompt"]["use"]["inputs"]["model"] == ["m", 0]
    # Kept, unconsumed and unchanged: ComfyUI never validates them, provenance reads them.
    assert result["prompt"]["s1"] == graph["s1"] and result["prompt"]["s2"] == graph["s2"]
    assert result["removed"] == []
    assert set(result["packed_rows"]) == {"s1", "s2"} and all(e["bypassed"] for e in result["packed_rows"].values())
    graph["use"]["inputs"]["model"] = ["m", 0]  # a stack nothing used is not kept for provenance
    graph["s3"] = stack([row("q", enabled=False)])
    graph["other"] = n("Use", model=["s3", 0])
    graph["sw"] = n("SonderLazySwitch", select=0, **{"branches.item0": ["use", 0], "branches.item1": ["other", 0]})
    graph["save"]["inputs"]["model"] = ["sw", 0]
    assert "s3" not in prep(graph)["prompt"]


def test_a_stack_that_cannot_be_bypassed_is_left_alone():
    dangling = {"m": n("Src", value=0), "st": stack([row(enabled=False)])}  # nothing consumes it
    assert prep(dangling, node_lookup=lambda c: None)["prompt"] == dangling  # unknown: a root, untouched
    literal = {"st": n("LoadLoraModel", model=None, **{"loras.0.lora_name": "x", "loras.0.strength": 1,
                                                       "loras.0.enabled": False}),
               "save": n("Save", model=["st", 0])}
    assert prep(literal)["prompt"] == literal
    linked = {"m": n("Src", value=0), "flag": n("Src", value=0),
              "st": stack([{"lora_name": "x", "strength": 1.0, "enabled": ["flag", 0]}]),
              "save": n("Save", model=["st", 0])}
    assert prep(linked)["prompt"] == linked


def test_a_stack_linked_to_itself_falls_back():
    graph = {"st": stack([row(enabled=False)], model=("st", 0)), "save": n("Save", model=["st", 0])}
    result = prep(graph)
    assert result["prompt"] == graph


@pytest.mark.parametrize("row_value,noop", [
    ({"enabled": False}, True), ({"enabled": "false"}, False), ({"enabled": 0}, True), ({"enabled": ""}, True),
    ({"strength": 0}, True), ({"strength": 0.0}, True), ({"strength": "0"}, True), ({"strength": "x"}, False),
    ({"strength": -0.0}, True), ({"strength": 0.001}, False), ({"lora_name": ""}, True), ({"lora_name": "None"}, False),
    ({"enabled": ["n", 0]}, False), ({"strength": ["n", 0]}, False), ({"enabled": {"__value__": False}}, True),
])
def test_row_is_noop_matches_the_native_skip_after_conversion(row_value, noop):
    full = {"lora_name": "a", "strength": 1.0, "enabled": True, **row_value}
    assert control_rows.row_is_noop(full) is noop
    # The native rule on the converted values, where the row has only literals.
    if not any(isinstance(v, list) for v in full.values()):
        def convert(value, kind):
            value = value["__value__"] if isinstance(value, dict) else value
            return bool(value) if kind == "b" else float(value)
        try:
            native = (not full["lora_name"] or not convert(full["enabled"], "b")
                      or convert(full["strength"], "f") == 0)
        except ValueError:
            return  # unconvertible: native validation refuses the row
        assert native is noop


def test_leaves_are_roots_so_relay_unknown_and_dangling_nodes_stay():
    graph = switch_graph()
    graph["dangling"] = n("NotInstalled", value=["b", 0])
    result = prep(graph)
    assert {"relay", "dangling", "b"} <= set(result["prompt"])


def test_a_known_output_inside_a_cut_branch_is_kept():
    graph = switch_graph()
    graph["preview"] = n("Preview", value=["x", 0])
    graph["sw"]["inputs"]["branches.item0"] = ["preview", 0]
    result = prep(graph)
    assert {"preview", "x", "b"} <= set(result["prompt"])


def test_unknown_classes_reached_only_through_a_cut_are_dropped():
    result = prep(switch_graph(), node_lookup=lookup)
    assert "x" in result["removed"]


def test_branch_run_roots_are_the_target_and_the_relay():
    graph = switch_graph()
    graph["preview"] = n("Preview", value=["a", 0])
    result = prep(graph, targets=["preview"])
    assert set(result["prompt"]) == {"preview", "a", "relay"}


def test_editor_dropped_falls_back():
    graph = {"ed": n("SonderEditor", project="p"), "sw": n("SonderLazySwitch", select=1,
             **{"branches.item0": ["ed", 0], "branches.item1": ["a", 0]}), "a": n("Src", value=1),
             "save": n("Save", value=["sw", 0])}
    result = prep(graph)
    assert result["prompt"] == graph and result["fallback"] == "editor dropped"


def test_an_exception_falls_back(monkeypatch):
    monkeypatch.setattr(rp, "_cut_switch", lambda *a: 1 / 0)
    result = prep(switch_graph())
    assert result["prompt"] == switch_graph() and result["fallback"].startswith("error")


def test_selector_parser_mirrors_the_node():
    spec = importlib.util.spec_from_file_location("sonder_selector_parity", ROOT / "nodes" / "selector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for text in ("", "a", " a \n\n b \r\n", "x\ny\nx", None, 3):
        assert rp.parse_choice_list(text) == module._parse_choice_list(text)


def test_an_installed_non_output_leaf_is_not_a_root_but_an_unknown_one_is():
    # A switched-off Mode's chain often ends in such a leaf (H3's MaskToImage
    # after Mask Off mutes its preview): ComfyUI never runs it.
    graph = {"v": n("Loader", ckpt_name="missing.safetensors"), "use": n("Use", model=["v", 0]),
             "save": n("Save", a=["m", 0]), "m": n("Src", value=1)}
    result = prep(graph)
    assert set(result["prompt"]) == {"save", "m"} and result["removed"] == ["use", "v"]
    graph["use"]["class_type"] = "NotInstalledLeaf"
    assert set(prep(graph, node_lookup=lookup)["prompt"]) == {"save", "m", "use", "v"}
    # Without class information every leaf stays.
    assert set(rp.prepare(graph)["prompt"]) == set(graph)


def test_a_branch_run_keeps_the_editor_even_when_its_output_does_not_read_it():
    # The run's token lives on the Editor (``_meta.sonder_run``).
    graph = {"ed": n("SonderEditor", project="p"), "save": n("Save", project=["ed", 0]),
             "img": n("Src", value=1), "pv": n("Preview", value=["img", 0]), "relay": n("SonderControlRelay")}
    result = prep(graph, targets=["pv"])
    assert set(result["prompt"]) == {"ed", "img", "pv", "relay"} and result["fallback"] is None


def test_a_kept_bypassed_stack_keeps_what_its_linked_fields_read():
    graph = {"m": n("Src", value=0), "p": n("PrimitiveInt", value=1),
             "st": stack([{"lora_name": "x", "strength": ["p", 0], "enabled": False}]),
             "save": n("Save", model=["st", 0])}
    result = prep(graph)
    assert result["fallback"] is None and result["prompt"]["save"]["inputs"]["model"] == ["m", 0]
    assert {"st", "p"} <= set(result["prompt"])


@pytest.mark.parametrize("primitive,value,cut", [
    ("PrimitiveBoolean", False, True), ("PrimitiveInt", 0, True), ("PrimitiveFloat", 0.0, True),
    ("PrimitiveString", False, False),  # runs as the text "False": the lane opens
    ("PrimitiveInt", "x", False),        # unconvertible: refused at run time, cut nothing
])
def test_primitive_literals_are_read_as_comfyui_converts_them(primitive, value, cut):
    KNOWN.setdefault("PrimitiveFloat", _Plain)
    KNOWN.setdefault("PrimitiveString", _Plain)
    graph = {"v": n("Loader", ckpt_name="missing.safetensors"), "c": n(primitive, value=value),
             "g": n("SonderGate", value_A=["v", 0], when_A=["c", 0]), "save": n("Save", a=["g", 0])}
    assert ("value_A" not in prep(graph)["prompt"]["g"]["inputs"]) is cut
