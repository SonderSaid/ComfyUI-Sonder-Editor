"""Sonder Control Relay against a real ComfyUI install (opt-in, skipped when absent).

Checks the Relay's published schema, describes bindings against ComfyUI's real
node classes (seed detection, bounds, validator coverage, the Selector options
provider, the denylist), and runs ComfyUI's own ``/prompt`` validation on a
workflow carrying a Relay, which must never be validated or executed itself.
ComfyUI's ``nodes`` and ``execution`` modules collide with this repository's
packages, so the checks run in a subprocess started from the ComfyUI root: the
root is ``SONDER_COMFYUI_ROOT``, or the install whose venv runs this test.
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
relay_module = importlib.import_module("sonder_pkg.nodes.control_relay")
selector = importlib.import_module("sonder_pkg.nodes.selector")
lazy = importlib.import_module("sonder_pkg.nodes.lazy_switches")

out = {"errors": {}}
Relay = relay_module.SonderControlRelay
nodes.NODE_CLASS_MAPPINGS["SonderControlRelay"] = Relay
nodes.NODE_CLASS_MAPPINGS["SonderSelector"] = selector.SonderSelector
nodes.NODE_CLASS_MAPPINGS["SonderLazySwitch"] = lazy.SonderLazySwitch
try:
    bridges = importlib.import_module("sonder_pkg.nodes.bridge_nodes")
    nodes.NODE_CLASS_MAPPINGS["SonderGuidesBridgeStart"] = bridges.SonderGuidesBridgeStart
except Exception as exc:
    out["errors"]["bridge_nodes"] = repr(exc)


class StringSink:
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {}), "index": ("INT", {"min": 0, "max": 99})}}

    def run(self, text, index):
        return ()


nodes.NODE_CLASS_MAPPINGS["StringSink"] = StringSink

info = Relay.GET_NODE_INFO_V1()
out["schema"] = {
    "name": info["name"], "display_name": info["display_name"],
    "required": list(info["input"].get("required", {})),
    "optional": info["input"].get("optional", {}),
    "output": info["output"], "output_node": info.get("output_node", False),
}
out["execute_args"] = len(Relay.execute(manifest="{}").args)


def binding(path, name, **extra):
    return {"label": "", "status": "ok", "targets": [{"path": path, "input": name}], **extra}


manifest = {"v": 1, "bindings": {
    "seed": binding("6", "seed", seed_mode="increment"),
    "steps": binding("6", "steps"),
    "sampler": binding("6", "sampler_name"),
    "image": binding("2", "image"),
    "choice": binding("20", "choice"),
    "list": binding("20", "choice_list"),
    "select": binding("8", "select"),
    "text": binding("10", "text"),
}}
prompt = {
    "1": {"class_type": "SonderControlRelay", "inputs": {"manifest": json.dumps(manifest)}},
    "2": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
    "6": {"class_type": "KSampler", "inputs": {"model": ["99", 0], "seed": 2 ** 64 - 1, "steps": 30, "cfg": 7.0,
          "sampler_name": "euler", "scheduler": "normal", "positive": ["99", 0], "negative": ["99", 0],
          "latent_image": ["99", 0], "denoise": 1.0}},
    "8": {"class_type": "SonderLazySwitch", "inputs": {"select": 1, "branches.item0": ["99", 0], "branches.item1": ["99", 0]}},
    "20": {"class_type": "SonderSelector", "inputs": {"choice": "Decompression", "choice_list": "Base\nDecompression\nDetail"}},
    "10": {"class_type": "CLIPTextEncode", "inputs": {"text": "before", "clip": ["99", 1]}},
    "99": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "none.safetensors"}},
}
if "SonderGuidesBridgeStart" in nodes.NODE_CLASS_MAPPINGS:
    manifest["bindings"]["guides"] = binding("16", "iteration_index")
    prompt["16"] = {"class_type": "SonderGuidesBridgeStart", "inputs": {"project": ["99", 0], "iteration_index": 0}}
    prompt["1"]["inputs"]["manifest"] = json.dumps(manifest)

try:
    view = cd.describe_bindings(prompt)
    out["bindings"] = {b["binding_id"]: {name: b.get(name) for name in (
        "status", "kind", "value", "min", "max", "options", "checked_at_run", "provider", "seed_mode", "error")}
        for b in view["bindings"]}
    out["relay"] = view["relay"]
    cleaned, rejected = cd.validate_binding_changes(view, {"binding:list:value": "Base\nGlow", "binding:choice:value": "Glow"}, prompt=prompt)
    out["batch"] = {"cleaned": cleaned, "rejected": rejected}
    text_changes, text_rejected = cd.validate_binding_changes(view, {"binding:text:value": "after"}, prompt=prompt)
    out["text_write"] = {"changes": text_changes, "rejected": text_rejected}
    out["text_candidates"] = [c for c in cd.candidate_inventory(prompt, cd.sdk_candidates(prompt))
                              if c["path"] == "10" and c["input"] == "text"]
except Exception:
    out["errors"]["describe"] = traceback.format_exc()


# /prompt validation of a runnable graph that carries a Relay: the Relay is
# not an output and not an ancestor of one, so it is neither validated nor run.
runnable = {
    "1": {"class_type": "SonderControlRelay", "inputs": {"manifest": json.dumps(manifest)}},
    "20": {"class_type": "SonderSelector", "inputs": {"choice": "Decompression", "choice_list": "Base\nDecompression\nDetail"}},
    "30": {"class_type": "StringSink", "inputs": {"text": ["20", 0], "index": ["20", 1]}},
}
valid = asyncio.run(execution.validate_prompt("relay-runtime", runnable, None))
out["runnable"] = {"valid": valid[0], "outputs": valid[2], "errors": valid[3]}
broken = dict(runnable, **{"1": {"class_type": "SonderControlRelay", "inputs": {"manifest": "{not json"}}})
out["broken_manifest_valid"] = asyncio.run(execution.validate_prompt("relay-runtime-2", broken, None))[0]
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
    assert runtime["relay"]["status"] == "ok"


def test_schema_is_the_published_contract(runtime):
    schema = runtime["schema"]
    assert schema["name"] == "SonderControlRelay"
    assert schema["display_name"] == "Sonder Control Relay"
    assert schema["required"] == []
    assert list(schema["optional"]) == ["manifest"]
    assert schema["optional"]["manifest"][1]["socketless"] is True
    assert schema["output"] == [] and schema["output_node"] is False
    assert runtime["execute_args"] == 0


def test_bindings_read_real_node_classes(runtime):
    bindings = runtime["bindings"]
    seed = bindings["seed"]
    assert (seed["status"], seed["kind"], seed["value"], seed["max"], seed["seed_mode"]) == (
        "ok", "SEED", 2 ** 64 - 1, 2 ** 64 - 1, "increment")
    steps = bindings["steps"]
    assert (steps["kind"], steps["value"], steps["min"]) == ("INT", 30, 1) and not steps["checked_at_run"]
    assert "euler" in bindings["sampler"]["options"]
    # LoadImage validates `image` itself, so native skips the option check: so does the panel.
    assert bindings["image"]["checked_at_run"] is True and bindings["image"]["options"] is None
    assert bindings["image"]["error"] is None
    choice = bindings["choice"]
    assert choice["options"] == ["Base", "Decompression", "Detail"] and choice["provider"] is True
    assert choice["value"] == "Decompression" and choice["error"] is None
    assert bindings["list"]["kind"] == "STRING"
    assert bindings["text"]["kind"] == "STRING" and bindings["text"]["value"] == "before"
    assert runtime["text_write"] == {"changes": {"binding:text:value": "after"}, "rejected": []}
    assert runtime["text_candidates"][0]["kind"] == "STRING"
    assert runtime["text_candidates"][0]["status"] == "ok"
    assert bindings["select"]["status"] == "ok"
    if "guides" in bindings:
        assert bindings["guides"]["status"] == "denied"


def test_a_new_choice_list_and_a_choice_in_it_validate_together(runtime):
    assert runtime["batch"]["rejected"] == []
    assert runtime["batch"]["cleaned"] == {"binding:list:value": "Base\nGlow", "binding:choice:value": "Glow"}


def test_prompt_validation_ignores_the_relay_and_routes_the_selector_literal(runtime):
    runnable = runtime["runnable"]
    assert runnable["valid"] is True, runnable["errors"]
    assert runnable["outputs"] == ["30"]
    # Even an unreadable manifest cannot fail a native Run: only Sonder reads it.
    assert runtime["broken_manifest_valid"] is True
