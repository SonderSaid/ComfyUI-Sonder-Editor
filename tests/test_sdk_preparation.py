"""Sonder App runs submit their execution copy, judged by one judge (Phase 3 of the 0.8 hardening).

A Task Mode sets a Sonder Switch's ``select``: Edit (branch 0) needs a checkpoint
this ComfyUI lacks and an uninstalled node, Clean (branch 1) needs neither. The
runner, the Launcher's check and the Mode attribution are exercised end to end
with the real describe over a fake class registry.
"""

import asyncio
import json
import sys
import types

import pytest

from server import control_descriptors as cd, sdk_runs, sdk_templates
from server.sdk_runner import SdkError
from test_control_descriptors import CLASSES as BASE_CLASSES, _binding, _node
from test_sdk_runner import World


class Loader:
    RETURN_TYPES = ("MODEL",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"ckpt_name": (["installed.ckpt"],)}}


class Switch:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"select": ("INT", {"default": 0, "min": 0, "max": 31})}}


class Save:
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": ("SONDER_PROJECT",)}, "optional": {"video": ("*",)}}


class Seeded:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"seed": ("INT", {"min": 0, "max": 100})}}


CLASSES = {**BASE_CLASSES, "Loader": Loader, "SonderLazySwitch": Switch, "SonderSaveVideo": Save, "Seeded": Seeded}


def describe(prompt, **kwargs):
    return cd.describe_bindings(prompt, class_lookup=CLASSES.get, **kwargs)


def workflow(select=0):
    manifest = {
        "v": 1,
        "bindings": {"task": _binding("sw", "select", label="Task"),
                     "seed": _binding("s", "seed", label="Seed", seed_mode="increment")},
        "notes": {},
        "presets": {"edit": {"name": "Edit", "order": 0, "members": {"task": {"value": 0}}},
                    "clean": {"name": "Clean", "order": 1, "members": {"task": {"value": 1}}}},
        "preset_categories": {"tasks": {"name": "Task", "order": 0, "choices": ["edit", "clean"]}},
    }
    return {
        "1": _node("SonderEditor", {"project": "P"}),
        "4": _node("Loader", {"ckpt_name": "missing.ckpt"}, title="Edit model"),
        "x": _node("NotInstalledNode", {"model": ["4", 0]}),
        "5": _node("Loader", {"ckpt_name": "installed.ckpt"}),
        "sw": _node("SonderLazySwitch", {"select": select, "branches.item0": ["x", 0], "branches.item1": ["5", 0]}),
        "s": _node("Seeded", {"seed": 5, "model": ["sw", 0]}),
        "2": _node("SonderSaveVideo", {"project": ["1", 0], "video": ["s", 0]}),
        "9": _node("SonderControlRelay", {"manifest": json.dumps(manifest)}),
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    monkeypatch.setattr(cd, "_comfy_class_lookup", CLASSES.get)
    monkeypatch.setattr(cd, "comfy_class_lookup", CLASSES.get)
    sdk_runs.reset_for_tests()
    result = World()
    result.runner._describe = describe
    result.runner._class_lookup = CLASSES.get
    result.missing_classes = {"NotInstalledNode"}
    sdk_templates.replace_template(result.template_id, json.dumps(workflow()).encode(), validate=lambda p: {"ok": True})
    yield result
    result.runner.close()
    sdk_runs.reset_for_tests()


def choose(world, preset):
    prompt, _ = sdk_templates.load_template(world.template_id)
    stored = sdk_templates.read_control_values(world.template_id)
    view = sdk_templates.apply_stored_values(prompt, describe(prompt), stored, describe=describe)
    cleaned, rejected = cd.validate_binding_changes(view, {f"preset:{preset}:apply": True}, prompt=prompt,
                                                    structural="sdk")
    assert not rejected, rejected
    sdk_templates.store_binding_changes(world.template_id, cleaned, view, prompt)


def check(world):
    return sdk_templates.check_template(world.template_id, class_lookup=CLASSES.get, describe=describe,
                                        node_lookup=lambda name: None if name in world.missing_classes else CLASSES.get(name, object))


def test_the_launcher_check_names_the_choice_that_needs_each_missing_item(world):
    result = check(world)
    assert result["missing_nodes"] == [{"class_type": "NotInstalledNode", "needed_by": ["Task → Edit"]}]
    assert [(m["file"], m["needed_by"]) for m in result["missing_models"]] == [("missing.ckpt", ["Task → Edit"])]
    choose(world, "clean")
    result = check(world)
    assert result["missing_nodes"] == [] and result["missing_models"] == [] and result["problems"] == []


def test_generate_refuses_naming_the_choice_then_runs_the_cut_prompt(world):
    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as refused:
            await world.runner.generate(session, tab_session_id="tab-1")
        choose(world, "clean")
        result = await world.runner.generate(session, tab_session_id="tab-1")
        return refused.value, result

    refused, result = asyncio.run(go())
    assert refused.code == "missing_nodes" and "NotInstalledNode is needed by Task → Edit." in str(refused)
    submitted = world.client.submitted[0]
    # The execution copy: the Edit branch and what only it needs are gone.
    assert set(submitted) == {"1", "5", "sw", "s", "2", "9"}
    assert set(submitted["sw"]["inputs"]) == {"select", "branches.item1"}
    assert result["run_ids"]


def test_a_missing_model_alone_refuses_with_its_choice(world):
    world.missing_classes = set()

    async def go():
        session = await world.open()
        with pytest.raises(SdkError) as refused:
            await world.runner.generate(session, tab_session_id="tab-1")
        return refused.value

    refused = asyncio.run(go())
    assert refused.code == "generate_refused"
    assert any("missing.ckpt" in p and "Needed by Task → Edit." in p for p in refused.details["problems"])


def test_a_seeded_select_is_never_cut(world, monkeypatch):
    # A seed rewrites its target per take after preparation, so a select it
    # drives must keep every branch.
    class SeededSwitch:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"select": ("INT", {"min": 0, "max": 31, "control_after_generate": True})}}

    monkeypatch.setitem(CLASSES, "SonderLazySwitch", SeededSwitch)
    prompt = workflow(select=1)
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["bindings"]["seed"] = _binding("sw", "select", label="Seed", seed_mode="increment")
    del manifest["bindings"]["task"]
    manifest["presets"], manifest["preset_categories"] = {}, {}
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    sdk_templates.replace_template(world.template_id, json.dumps(prompt).encode(), validate=lambda p: {"ok": True})
    stored = sdk_templates.read_control_values(world.template_id)
    view = sdk_templates.apply_stored_values(prompt, describe(prompt), stored, describe=describe)
    assert ("sw", "select") in sdk_templates.volatile_inputs(view)
    prepared = sdk_templates.prepare_compiled(prompt, view, class_lookup=CLASSES.get)
    assert "branches.item0" in prepared["prompt"]["sw"]["inputs"]


def test_branch_run_seeds_advance_as_before(world, monkeypatch):
    # The seed's node is outside a branch run of the Save: nothing writes it,
    # and the seed still advances, as native partial queueing does.
    choose(world, "clean")
    from server import run_control
    run_control.reset_for_tests()
    monkeypatch.setattr(run_control, "branch_capability", lambda: {"ok": True, "reason": ""})
    bodies = []

    async def post(body):
        bodies.append(body)
        return 200, {"prompt_id": body["prompt_id"], "number": 1, "node_errors": {}}

    world.runner._branch_submit = post

    async def go():
        session = await world.open()
        first = await world.runner.run_branch(session, tab_session_id="tab-1", target="2")
        second = await world.runner.run_branch(session, tab_session_id="tab-1", target="2")
        return first, second

    try:
        first, second = asyncio.run(go())
    finally:
        run_control.reset_for_tests()
    assert [r["seeds_used"][0]["seed"] for r in (first, second)] == ["5", "6"]
    assert bodies[0]["prompt"]["s"]["inputs"]["seed"] == 5 and bodies[1]["prompt"]["s"]["inputs"]["seed"] == 6
    assert "4" not in bodies[0]["prompt"] and "x" not in bodies[0]["prompt"]


def test_attribution_stops_at_its_cap(world, monkeypatch):
    monkeypatch.setattr(sdk_templates, "MAX_ATTRIBUTIONS", 0)
    result = check(world)
    assert result["missing_nodes"] == [{"class_type": "NotInstalledNode"}]


def test_attribution_skips_a_choice_that_cannot_apply(world):
    prompt = workflow()
    manifest = json.loads(prompt["9"]["inputs"]["manifest"])
    manifest["presets"]["clean"]["members"] = {"gone": {"value": 1}}  # a member no binding has
    prompt["9"]["inputs"]["manifest"] = json.dumps(manifest)
    sdk_templates.replace_template(world.template_id, json.dumps(prompt).encode(), validate=lambda p: {"ok": True})
    result = check(world)
    assert result["missing_nodes"] == [{"class_type": "NotInstalledNode"}]


def test_the_launcher_check_is_reused_while_unchanged_and_survives_a_failed_attribution(world, monkeypatch):
    calls = []
    real = sdk_templates._check_template
    monkeypatch.setattr(sdk_templates, "_check_template", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    lookup = lambda name: None if name in world.missing_classes else CLASSES.get(name, object)  # noqa: E731
    first = sdk_templates.check_template(world.template_id, class_lookup=CLASSES.get, describe=describe, node_lookup=lookup)
    again = sdk_templates.check_template(world.template_id, class_lookup=CLASSES.get, describe=describe, node_lookup=lookup)
    assert first == again and len(calls) == 1
    choose(world, "clean")  # new values: computed again
    sdk_templates.check_template(world.template_id, class_lookup=CLASSES.get, describe=describe, node_lookup=lookup)
    assert len(calls) == 2
    choose(world, "edit")
    monkeypatch.setattr(sdk_templates, "attribute_requirements", lambda *a, **kw: 1 / 0)
    result = sdk_templates.check_template(world.template_id, class_lookup=CLASSES.get, describe=describe, node_lookup=lookup)
    assert result["missing_nodes"] == [{"class_type": "NotInstalledNode"}] and result["missing_models"]
