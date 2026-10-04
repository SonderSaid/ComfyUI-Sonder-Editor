"""Sonder Control against a real ComfyUI install (opt-in, skipped when absent).

The descriptor's ``unsupported`` check must call target validators exactly as
``/prompt`` does, and the node must run through ComfyUI's own V3 machinery.
Both need ComfyUI's ``nodes`` and ``execution`` modules, whose names collide
with this repository's packages, so the checks run in a subprocess started from
the ComfyUI root. The root is ``SONDER_COMFYUI_ROOT``, or the install whose
venv runs this test.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _comfy_root() -> Path | None:
    candidates = []
    if os.environ.get("SONDER_COMFYUI_ROOT"):
        candidates.append(Path(os.environ["SONDER_COMFYUI_ROOT"]))
    candidates.extend(Path(sys.executable).resolve().parents[:3])
    for root in candidates:
        if (root / "execution.py").is_file() and (root / "comfy_api").is_dir():
            return root
    return None


SCRIPT = r'''
import asyncio, importlib, json, sys, types, traceback
repo, comfy_root = sys.argv[1], sys.argv[2]
sys.path.insert(0, comfy_root)

import nodes
import execution

for name, path in (("sonder_pkg", repo), ("sonder_pkg.server", repo + "/server"), ("sonder_pkg.nodes", repo + "/nodes")):
    module = types.ModuleType(name)
    module.__path__ = [path]
    sys.modules[name] = module

cd = importlib.import_module("sonder_pkg.server.control_descriptors")
control_module = importlib.import_module("sonder_pkg.nodes.sonder_control")
lazy = importlib.import_module("sonder_pkg.nodes.lazy_switches")
collector = importlib.import_module("sonder_pkg.nodes.metadata_collector_v3")
from comfy_api.latest import io

out = {"errors": {}}
SonderControl = control_module.SonderControl
nodes.NODE_CLASS_MAPPINGS["SonderControl"] = SonderControl
nodes.NODE_CLASS_MAPPINGS["SonderLazySwitch"] = lazy.SonderLazySwitch
nodes.NODE_CLASS_MAPPINGS["SonderMetadataCollectorV3"] = collector.SonderMetadataCollectorV3
try:
    bridges = importlib.import_module("sonder_pkg.nodes.bridge_nodes")
    nodes.NODE_CLASS_MAPPINGS["SonderGuidesBridgeStart"] = bridges.SonderGuidesBridgeStart
except Exception as exc:
    out["errors"]["bridge_nodes"] = repr(exc)


class TypesValidated:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"amount": ("FLOAT", {"min": 0.0, "max": 1.0})}}
    RETURN_TYPES = ()
    FUNCTION = "run"

    @classmethod
    def VALIDATE_INPUTS(cls, input_types, amount):
        return amount is None and input_types.get("amount") == "*"


class StrictV3(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="StrictV3", inputs=[io.Int.Input("count", min=0, max=9)], outputs=[])

    @classmethod
    def validate_inputs(cls, count):
        return count + 1 > 0  # TypeError for a linked None

    @classmethod
    def execute(cls, count):
        return io.NodeOutput()


class NodeWide:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mode": (["a", "b"],), "text": ("STRING", {})}}
    RETURN_TYPES = ()
    FUNCTION = "run"

    @classmethod
    def VALIDATE_INPUTS(cls, mode, text):
        return mode == "a"  # fails on its own literal, whatever feeds text


nodes.NODE_CLASS_MAPPINGS["TypesValidated"] = TypesValidated
nodes.NODE_CLASS_MAPPINGS["StrictV3"] = StrictV3
nodes.NODE_CLASS_MAPPINGS["NodeWide"] = NodeWide

info = SonderControl.GET_NODE_INFO_V1()
required = info["input"]["required"]
out["schema"] = {
    "name": info["name"],
    "display_name": info["display_name"],
    "inputs": list(required),
    "kind_options": required["kind"][0] if isinstance(required["kind"][0], list) else required["kind"][1].get("options"),
    "seed_control": required["seed"][1].get("control_after_generate"),
    "socketless": sorted(name for name, spec in required.items() if spec[1].get("socketless")),
    "output": info["output"],
    "output_name": info["output_name"],
    "hidden": sorted(info["input"].get("hidden", {})),
}


def control(value_text, kind, seed=0, cid="c1"):
    return {"class_type": "SonderControl", "_meta": {"title": "Ctl " + cid},
            "inputs": {"value_text": value_text, "seed": seed, "kind": kind, "panel_order": 0,
                       "panel_group": "", "control_id": cid}}

prompt = {
    "1": control("example.png", "COMBO", cid="loader"),
    "2": {"class_type": "LoadImage", "inputs": {"image": ["1", 0]}},
    "3": control("30", "INT", cid="steps"),
    "4": control("euler", "COMBO", cid="sampler"),
    "5": control("", "SEED", seed=42, cid="seed"),
    "6": {"class_type": "KSampler", "inputs": {"model": ["99", 0], "seed": ["5", 0], "steps": ["3", 0],
          "cfg": 7.0, "sampler_name": ["4", 0], "scheduler": "normal", "positive": ["99", 0],
          "negative": ["99", 0], "latent_image": ["99", 0], "denoise": 1.0}},
    "7": control("1", "INT", cid="select"),
    "8": {"class_type": "SonderLazySwitch", "inputs": {"select": ["7", 0], "branches.item0": ["99", 0], "branches.item1": ["99", 0]}},
    "9": control("x", "STRING", cid="collect"),
    "10": {"class_type": "SonderMetadataCollectorV3", "inputs": {"project": ["99", 0], "values.value_0": ["9", 0], "label_0": "x"}},
    "11": control("0.5", "FLOAT", cid="types"),
    "12": {"class_type": "TypesValidated", "inputs": {"amount": ["11", 0]}},
    "13": control("3", "INT", cid="strict"),
    "14": {"class_type": "StrictV3", "inputs": {"count": ["13", 0]}},
    "17": control("hello", "STRING", cid="nodewide"),
    "18": {"class_type": "NodeWide", "inputs": {"mode": "b", "text": ["17", 0]}},
    "99": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "none.safetensors"}},
}
if "SonderGuidesBridgeStart" in nodes.NODE_CLASS_MAPPINGS:
    prompt["15"] = control("0", "INT", cid="guides")
    prompt["16"] = {"class_type": "SonderGuidesBridgeStart", "inputs": {"project": ["99", 0], "iteration_index": ["15", 0]}}

try:
    described = cd.describe_controls(prompt)
    out["controls"] = {
        c["control_id"]: {"status": c["status"], "value": c["value"], "error": c["error"],
                          "targets": [(t["class_type"], t["input"], t["status"], t.get("detail")) for t in c["targets"]]}
        for c in described["controls"]
    }
    out["target_status"] = {t["key"]: t["status"] for c in described["controls"] for t in c["targets"]}
except Exception:
    out["errors"]["describe"] = traceback.format_exc()


# ComfyUI's own /prompt validation of every node a Control feeds: whether the
# node's validator refused (custom_validation_failed) or raised.
async def real_validation(key):
    try:
        _, errors, _ = await execution.validate_inputs("sonder-test", prompt, key, {})
    except Exception as exc:
        return "raised " + type(exc).__name__
    return "refused" if any(e.get("type") == "custom_validation_failed" for e in errors) else "accepted"

out["real_validation"] = {
    key: asyncio.run(real_validation(key))
    for key, node in prompt.items()
    if node["class_type"] != "SonderControl" and any(
        isinstance(v, list) and prompt.get(str(v[0]), {}).get("class_type") == "SonderControl"
        for v in node["inputs"].values()
    )
}


class FakeDynPrompt:
    def __init__(self, prompt):
        self.prompt = prompt

    def get_original_prompt(self):
        return self.prompt


async def run_control(key):
    data, _, v3_data = execution.get_input_data(prompt[key]["inputs"], SonderControl, key, dynprompt=FakeDynPrompt(prompt))
    results = await execution._async_map_node_over_list(None, key, SonderControl, data, "execute", v3_data=v3_data)
    results = await execution.resolve_map_node_over_list_results(results)
    return results[0].args[0]

executed = {}
for key in ("3", "4", "5", "7"):
    try:
        value = asyncio.run(run_control(key))
        executed[key] = [repr(value), type(value).__name__]
    except Exception as exc:
        executed[key] = ["raised", f"{type(exc).__name__}: {exc}"]
prompt["3"]["inputs"]["value_text"] = "20000"
try:
    asyncio.run(run_control("3"))
    executed["3-out-of-range"] = ["no error", ""]
except Exception as exc:
    executed["3-out-of-range"] = ["raised", f"{type(exc).__name__}: {exc}"]
prompt["3"]["inputs"]["value_text"] = "30"
prompt["3"]["inputs"]["kind"] = "FLOAT"
try:
    asyncio.run(run_control("3"))
    executed["3-stale-kind"] = ["no error", ""]
except Exception as exc:
    executed["3-stale-kind"] = ["raised", f"{type(exc).__name__}: {exc}"]
out["executed"] = executed
print("@@RESULT@@" + json.dumps(out, default=repr))
'''


@pytest.fixture(scope="module")
def runtime():
    root = _comfy_root()
    if root is None:
        pytest.skip("no ComfyUI install found (set SONDER_COMFYUI_ROOT)")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(REPO_ROOT), str(root)],
        cwd=str(root), env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    marker = [line for line in completed.stdout.splitlines() if line.startswith("@@RESULT@@")]
    assert marker, f"runtime script failed:\n{completed.stdout[-4000:]}\n{completed.stderr[-4000:]}"
    return json.loads(marker[-1][len("@@RESULT@@"):])


def test_runtime_imports_and_describe_ran(runtime):
    assert "describe" not in runtime["errors"], runtime["errors"].get("describe")


def test_schema_is_the_published_contract(runtime):
    schema = runtime["schema"]
    assert schema["name"] == "SonderControl"
    assert schema["display_name"] == "Sonder Control"
    assert schema["inputs"] == ["value_text", "seed", "kind", "panel_order", "panel_group", "control_id"]
    assert schema["kind_options"] == ["STRING", "INT", "FLOAT", "BOOLEAN", "COMBO", "SEED"]
    assert schema["seed_control"] == "fixed"
    assert schema["socketless"] == sorted(schema["inputs"])  # no linked inputs
    assert schema["output"] == ["*"] and schema["output_name"] == ["value"]
    assert schema["hidden"] == ["prompt", "unique_id"]


def test_validator_dry_run_matches_prompt_validation(runtime):
    controls = runtime["controls"]
    loader = controls["loader"]
    assert loader["status"] == "unsupported"
    assert loader["targets"][0][:3] == ["LoadImage", "image", "unsupported"]
    assert "AttributeError" in loader["targets"][0][3]
    strict = controls["strict"]
    assert strict["status"] == "unsupported" and "TypeError" in strict["targets"][0][3]
    # Validators that handle a linked None are not refused.
    for control_id in ("select", "collect", "types"):
        assert controls[control_id]["status"] == "ok", (control_id, controls[control_id])
    if "guides" in controls:
        assert controls["guides"]["status"] == "ok", controls["guides"]
    for control_id in ("steps", "sampler", "seed"):
        assert controls[control_id]["status"] == "ok", (control_id, controls[control_id])
    assert controls["steps"]["value"] == 30
    assert controls["seed"]["value"] == 42


def test_dry_run_refuses_exactly_what_comfy_validation_refuses(runtime):
    real, described = runtime["real_validation"], runtime["target_status"]
    # A node that fails on its own literal fails at /prompt either way; the
    # Control is not blamed (and its link is not removed).
    assert real.pop("18") == "refused" and described["18"] == "ok"
    assert {"2", "6", "8", "10", "12", "14"} <= set(real)
    for key, verdict in real.items():
        assert (described[key] == "unsupported") == (verdict != "accepted"), (key, verdict, described[key])


def test_node_executes_typed_values_through_comfy(runtime):
    executed = runtime["executed"]
    assert executed["3"] == ["30", "int"]
    assert executed["4"] == ["'euler'", "str"]
    assert executed["5"] == ["42", "int"]
    assert executed["7"] == ["1", "int"]
    assert executed["3-out-of-range"][0] == "raised"
    assert "above the maximum of 10000 for KSampler steps" in executed["3-out-of-range"][1]
    assert executed["3-stale-kind"][0] == "raised"
    assert "set up for FLOAT but feeds KSampler steps, which takes INT" in executed["3-stale-kind"][1]
