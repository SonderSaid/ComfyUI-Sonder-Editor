"""Literal model COMBO requirements shared by Launcher and Generate.

Finalized node schemas own the options. Links and custom validators retain
ComfyUI/Controls validation; this check never guesses their values.
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

from . import control_descriptors as cd
from .lora_metadata import NATIVE_LORA_STACKS
from .prompt_graph import upstream_reaches

logger = logging.getLogger(__name__)
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft"}
INPUT_FOLDERS = {"ckpt_name": "checkpoints", "lora_name": "loras", "vae_name": "vae",
                 "unet_name": "diffusion_models", "clip_name": "text_encoders", "text_encoder": "text_encoders"}
# Loaders whose input name says nothing about the folder, or says the wrong one
# (CLIP Vision's ``clip_name``, the latent upscaler's ``model_name``). A fresh
# install lists no files, so the option-list match below can't infer them either.
CLASS_INPUT_FOLDERS = {
    ("LoadMoGeModel", "model_name"): "geometry_estimation",
    ("CLIPVisionLoader", "clip_name"): "clip_vision",
    ("LatentUpscaleModelLoader", "model_name"): "latent_upscale_models",
    # KJNodes' sampling preview reads models/vae_approx (preview_override_node.py).
    ("ModelPreviewOverrideKJ", "tiny_vae"): "vae_approx",
}


# A native LoRA stack row's file field is a file whatever it says: a starter's
# switched-off "None" row has no extension, and when a run still carries it (a
# row turned on, or a preparation that fell back) it must refuse by name.
_NATIVE_ROW_FILE = re.compile(r"^loras\.(0|[1-9][0-9]*)\.lora_name$")


def _is_model_value(class_type: str, name: str, value) -> bool:
    if not isinstance(value, str):
        return False
    if os.path.splitext(value)[1].lower() in MODEL_EXTENSIONS:
        return True
    return class_type in NATIVE_LORA_STACKS and bool(value) and bool(_NATIVE_ROW_FILE.match(name))


def _normal(value):
    return str(value).replace("\\", "/")


@lru_cache(maxsize=512)
def _absent(value: str, options: tuple[str, ...]) -> bool:
    # Options are the fingerprint: same-size filename replacements and dynamic
    # schema changes invalidate too. No stale time-based model cache.
    return value not in options


def _folder(name: str, class_type: str, options: tuple[str, ...]) -> str:
    if (class_type, name) in CLASS_INPUT_FOLDERS:
        return CLASS_INPUT_FOLDERS[(class_type, name)]
    # The folder whose file list is exactly the options beats a name guess.
    if options:
        try:
            import folder_paths
            matches = [key for key in folder_paths.folder_names_and_paths
                       if set(map(_normal, folder_paths.get_filename_list(key))) == set(options)]
            if len(matches) == 1:
                return matches[0]
        except (ImportError, AttributeError, OSError):
            pass
    if name in INPUT_FOLDERS:
        return INPUT_FOLDERS[name]
    # A dynamic row field (``loras.0.lora_name``) is named by its last segment.
    leaf = name.rsplit(".", 1)[-1]
    if leaf != name and leaf in INPUT_FOLDERS:
        return INPUT_FOLDERS[leaf]
    if name in {"upscale_model", "model_name"} and "upscale" in class_type.lower():
        return "upscale_models"
    return ""


def missing_models(prompt: dict, class_lookup=None, *, input_cache=None, executable_only=False) -> list[dict]:
    lookup = class_lookup or cd.comfy_class_lookup
    cache = input_cache if input_cache is not None else {}
    outputs = []
    if executable_only:
        for key, node in prompt.items():
            try:
                if getattr(lookup(node.get("class_type", "")), "OUTPUT_NODE", False):
                    outputs.append(key)
            except (ImportError, AttributeError):
                pass
    missing = []
    for key, node in prompt.items():
        inputs = node.get("inputs", {})
        candidates = [(name, value) for name, value in inputs.items()
                      if _is_model_value(node.get("class_type", ""), name, value)]
        if not candidates:
            continue
        if executable_only and not any(upstream_reaches(prompt, output, key) for output in outputs):
            continue
        class_type = node.get("class_type", "")
        try:
            cls = lookup(class_type)
            if cls is None:
                continue
            schema = cd._class_inputs(cls, inputs, cache)
            for name, value in candidates:
                info = cd._input_info(schema, name)
                if info is None or cd.validator_covers(cls, name):
                    continue
                spec = cd.spec_from_input_info(*info, name)
                if spec.get("kind") != "COMBO":
                    continue
                options = tuple(map(_normal, spec["options"]))
                if _absent(_normal(value), options):
                    folder = _folder(name, class_type, options) or None
                    missing.append({"file": value, "folder": folder,
                                    # Where it goes, the value's own subfolders included:
                                    # the workflow names the file by that path.
                                    "relative": f"models/{folder}/{_normal(value)}" if folder else None,
                                    "node": str(node.get("_meta", {}).get("title") or class_type),
                                    "node_id": str(key), "input": name})
        except Exception:
            logger.debug("Cannot inspect model inputs for %s", class_type, exc_info=True)
    return missing


def names_an_off_row(prompt: dict, item: dict) -> bool:
    """Whether a ``missing_models`` item is a native LoRA row that's off, so no
    run ever loads it: a starter's switched-off "None" row (``control_rows.row_is_noop``)."""
    node = prompt.get(item.get("node_id")) or {}
    match = _NATIVE_ROW_FILE.match(str(item.get("input") or ""))
    if node.get("class_type") not in NATIVE_LORA_STACKS or not match:
        return False
    from . import control_rows
    prefix = f"loras.{match.group(1)}."
    inputs = node.get("inputs") or {}
    return control_rows.row_is_noop({key[len(prefix):]: value for key, value in inputs.items() if key.startswith(prefix)})


def missing_model_message(item: dict) -> str:
    name = os.path.basename(_normal(item["file"]))
    if os.path.splitext(name)[1].lower() not in MODEL_EXTENSIONS:
        return (f"A LoRA row in {item['node']} reads “{name}”, not an installed file. "
                "Choose a LoRA file for it in Controls, or switch the row off.")
    if item.get("relative"):
        return f"{name} is missing: put it at {item['relative']} ({item['node']})"
    return f"{name} is missing (folder unknown: {item['node']} · {item.get('input', '')})"


def target_path(item: dict) -> str | None:
    """The absolute file a missing model belongs at: the category's own folder
    (``models/diffusion_models`` before the legacy ``models/unet``), else its
    first one. For a browser on the ComfyUI machine only (the route decides)."""
    folder = item.get("folder")
    if not folder:
        return None
    try:
        import folder_paths
        paths = list(folder_paths.get_folder_paths(folder) or [])
    except (ImportError, AttributeError, KeyError, OSError):
        return None
    if not paths:
        return None
    base = next((p for p in paths if os.path.basename(os.path.normpath(p)) == folder), paths[0])
    return os.path.normpath(os.path.join(base, *_normal(item["file"]).split("/")))
