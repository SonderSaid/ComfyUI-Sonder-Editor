"""Mode availability on the shipped starters with real ComfyUI parts (CPU).

Real: ``folder_paths`` over temporary model folders, core loaders, ``LoadLoraModel``,
the Sonder selector and lazy switches, and every Sonder server path. The other
classes are stubs whose inputs are read from the starters, so a file input is a
real COMBO of what its folder holds. No model is ever loaded and nothing runs.
"""
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types

comfy, repo = sys.argv[1:]
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy)

import folder_paths
import nodes

root = Path(tempfile.mkdtemp(prefix="sonder-avail-"))
models = root / "models"
FOLDERS = ("diffusion_models", "text_encoders", "vae", "loras", "checkpoints", "geometry_estimation", "vae_approx",
           "latent_upscale_models", "clip")
for name in FOLDERS:
    (models / name).mkdir(parents=True)
    folder_paths.folder_names_and_paths[name] = ([str(models / name)], {".safetensors"})
folder_paths.filename_list_cache.clear()
(root / "user").mkdir()
folder_paths.set_user_directory(str(root / "user"))

pkg = types.ModuleType("sonder_avail_testpkg")
pkg.__path__ = [str(Path(repo))]
sys.modules[pkg.__name__] = pkg
lazy = importlib.import_module(f"{pkg.__name__}.nodes.lazy_switches")
selector = importlib.import_module(f"{pkg.__name__}.nodes.selector")
st = importlib.import_module(f"{pkg.__name__}.server.sdk_templates")
cd = importlib.import_module(f"{pkg.__name__}.server.control_descriptors")
stack_module = importlib.import_module("comfy_extras.nodes_lora_stack")

STARTERS = {path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((Path(repo) / "sdk_starters").glob("*.json")) if not path.name.endswith(".meta.json")}
OUTPUTS = {"SonderSaveVideo", "SonderPreviewVideo", "SaveVideo", "PreviewAny", "PreviewImage"}
FILE_FOLDERS = {("VAELoaderKJ", "vae_name"): "vae", ("LoadMoGeModel", "model_name"): "geometry_estimation",
                ("ModelPreviewOverrideKJ", "tiny_vae"): "vae_approx", ("LatentUpscaleModelLoader", "model_name"): "latent_upscale_models"}
LIMIT = 2 ** 53


def stub(class_type, examples):
    class Stub:
        OUTPUT_NODE = class_type in OUTPUTS
        RETURN_TYPES = ("*",)
        CATEGORY = "stub"
        FUNCTION = "run"

        @classmethod
        def INPUT_TYPES(cls):
            required = {}
            for name, value in examples.items():
                if isinstance(value, bool):
                    required[name] = ("BOOLEAN", {})
                elif isinstance(value, int):
                    required[name] = ("INT", {"min": -LIMIT, "max": LIMIT})
                elif isinstance(value, float):
                    required[name] = ("FLOAT", {"min": -1e9, "max": 1e9, "step": 0.001})
                elif isinstance(value, str) and (class_type, name) in FILE_FOLDERS:
                    required[name] = (list(folder_paths.get_filename_list(FILE_FOLDERS[(class_type, name)])),)
                elif isinstance(value, str):
                    required[name] = ("STRING", {})
                else:
                    required[name] = ("*",)
            return {"required": required}

    Stub.__name__ = class_type
    return Stub


examples, needed = {}, set()
for graph in STARTERS.values():
    for node in graph.values():
        needed.add(node["class_type"])
        examples.setdefault(node["class_type"], {}).update(node.get("inputs") or {})
nodes.NODE_CLASS_MAPPINGS.update(lazy.LAZY_NODE_CLASS_MAPPINGS)
nodes.NODE_CLASS_MAPPINGS.update({"SonderSelector": selector.SonderSelector, "LoadLoraModel": stack_module.LoadLoraModel})
stubbed = {name: stub(name, examples[name]) for name in needed if name not in nodes.NODE_CLASS_MAPPINGS}
nodes.NODE_CLASS_MAPPINGS.update(stubbed)
RELAY_NODE = "PromptRelaySmartEncode"
relay_class = nodes.NODE_CLASS_MAPPINGS.pop(RELAY_NODE)  # absent unless a case puts it back

SIMULATIONS = []
real_simulate = st._simulate_choice
st._simulate_choice = lambda *a, **kw: SIMULATIONS.append(a[4]) or real_simulate(*a, **kw)

ids = {stem: next(identity for identity, path in st.starter_sources().items() if Path(path).stem == stem) for stem in STARTERS}
FILE_INPUTS = {"unet_name": "diffusion_models", "clip_name": "text_encoders", "vae_name": "vae", "lora_name": "loras",
               "ckpt_name": "checkpoints", "tiny_vae": "vae_approx"}


def saved_files(stem):
    found = set()
    for node in STARTERS[stem].values():
        for name, value in (node.get("inputs") or {}).items():
            if not isinstance(value, str) or not value.endswith(".safetensors"):
                continue
            folder = FILE_FOLDERS.get((node["class_type"], name)) or FILE_INPUTS.get(name)
            found.add((folder, value.replace("\\", "/")))
    return found


def place(folder, relative):
    target = models / folder / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    # A directory's mtime is what folder_paths notices; make the change visible.
    for parent in (target.parent, models / folder):
        os.utime(parent, (time.time() + 2, time.time() + 2))
    folder_paths.filename_list_cache.pop(folder, None)


def clear_models():
    for name in FOLDERS:
        for path in sorted((models / name).rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        folder_paths.filename_list_cache.pop(name, None)


def forget():
    st._availability_cache.clear()
    st._starter_validation_cache.clear()
    for identity in ids.values():
        path = st._template_paths(identity)[2]
        if os.path.exists(path):
            os.remove(path)


def judge(stem):
    return st.check_availability(ids[stem], fresh=True)


def choice_names(stem):
    prompt, _ = st.load_template(ids[stem])
    stored = st.read_control_values(ids[stem])
    view = st.apply_stored_values(prompt, st.describe_stored(prompt, stored), stored)
    return {row["id"]: row["name"] for row in view["presets"]}, view, prompt


def greyed(stem):
    names, _, _ = choice_names(stem)
    return {names[identity] for identity in judge(stem)["choices"]}


def file_names(needs):
    return {item["file"].replace("\\", "/").rsplit("/", 1)[-1] for item in needs["missing_models"]}


cases = []


def case(name, passed, detail=None):
    cases.append({"name": name, "passed": bool(passed), "detail": detail})


# ── Complete install: the shortcut fires despite each starter's off "None" row ──
clear_models()
nodes.NODE_CLASS_MAPPINGS[RELAY_NODE] = relay_class
for stem in STARTERS:
    for folder, relative in saved_files(stem):
        place(folder, relative)
for stem in STARTERS:
    forget()
    SIMULATIONS.clear()
    result = judge(stem)
    case(f"complete install, no simulations: {stem}", not SIMULATIONS and not result["choices"]
         and not result["run"]["missing_models"] and not result["run"]["missing_nodes"] and not result["run"]["problems"],
         {"simulated": len(SIMULATIONS), "run": result["run"], "choices": list(result["choices"])})

# ── Prompt Relay absent, everything else installed ─────────────────────────────
del nodes.NODE_CLASS_MAPPINGS[RELAY_NODE]
forget()
edit = "sonder_ltx_2_5_edit_vfx"
result, names = judge(edit), choice_names(edit)[0]
case("no Prompt Relay: only Timed Prompts is greyed, for the node",
     {names[i] for i in result["choices"]} == {"Timed Prompts (Prompt Relay)"}
     and next(iter(result["choices"].values()))["missing_nodes"] == [RELAY_NODE]
     and not result["run"]["missing_nodes"], {"run": result["run"], "choices": result["choices"]})

# ── One file installed while the page is open ──────────────────────────────────
clean_plate = next(item for item in saved_files(edit) if "clean-plate" in item[1])
target = models / clean_plate[0] / clean_plate[1]
target.unlink()
os.utime(target.parent, (time.time() + 4, time.time() + 4))
folder_paths.filename_list_cache.pop(clean_plate[0], None)
forget()
before = greyed(edit)
# The directory's own mtime from here on, as a real install: back to the present,
# then a real create. Nothing touches the cache or the clock.
now = time.time()
os.utime(target.parent, (now - 10, now - 10))
greyed(edit)
target.write_bytes(b"")
after = greyed(edit)
case("a file added clears its choice on a fresh read, by the folder's real mtime",
     "Remove People" in before and "Remove People" not in after, {"before": sorted(before), "after": sorted(after)})

# ── Bare install: each starter's pattern ───────────────────────────────────────
clear_models()
EXPECTED = {
    edit: {"Edit With Words", "Edit With a Picture", "Follow Scene Layout", "Follow Body Movement", "Follow Outlines",
           "Remove People", "Make a Subject Mask", "Timed Prompts (Prompt Relay)"},
    "sonder_ltx_2_5_refine_upscale": {"Refine and Upscale Pass", "Refining Pass Only", "Upscale Pass Only"},
    "sonder_minimax_h3_references_swaps": {"Character Swap", "Head Swap", "Mask On"},
}
forget()
for stem, expected in EXPECTED.items():
    got = greyed(stem)
    case(f"bare install pattern: {stem}", got == expected, {"got": sorted(got), "expected": sorted(expected)})

# ── A simulated choice equals a real apply through the store ───────────────────
for stem in EXPECTED:
    forget()
    names, _, _ = choice_names(stem)
    for identity, label in names.items():
        forget()
        prompt, _ = st.load_template(ids[stem])
        stored = st.read_control_values(ids[stem])
        view = st.apply_stored_values(prompt, st.describe_stored(prompt, stored), stored)
        simulated = real_simulate(copy.deepcopy(st.load_template(ids[stem])[0]), stored, view, prompt, identity, own_only=False)
        cleaned, rejected = cd.validate_binding_changes(view, {f"preset:{identity}:apply": True}, prompt=prompt, structural="sdk")
        assert not rejected, rejected
        # What runner.change_controls stores; the runner's own path is in test_mode_availability.py.
        st.store_binding_changes(ids[stem], cleaned, view, prompt)
        st._availability_cache.clear()
        applied = judge(stem)["run"]
        sim_files = file_names({"missing_models": simulated["missing_models"]})
        case(f"simulation equals apply: {stem} / {label}",
             sim_files == file_names(applied) and set(simulated["missing_nodes"]) == set(applied["missing_nodes"]),
             {"simulated": sorted(sim_files), "applied": sorted(file_names(applied))})

print("SONDER_AVAIL_REPORT=" + json.dumps({"cases": cases, "stubbed": sorted(stubbed)}, default=str))
sys.exit(0 if all(c["passed"] for c in cases) else 1)
