"""Literal model COMBO requirements shared by Launcher and Generate.

Finalized node schemas own the options. Links and custom validators retain
ComfyUI/Controls validation; this check never guesses their values.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from . import control_descriptors as cd
from .prompt_graph import upstream_reaches

logger = logging.getLogger(__name__)
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft"}
INPUT_FOLDERS = {"ckpt_name": "checkpoints", "lora_name": "loras", "vae_name": "vae",
                 "unet_name": "diffusion_models", "clip_name": "text_encoders", "text_encoder": "text_encoders"}


def _normal(value):
    return str(value).replace("\\", "/")


@lru_cache(maxsize=512)
def _absent(value: str, options: tuple[str, ...]) -> bool:
    # Options are the fingerprint: same-size filename replacements and dynamic
    # schema changes invalidate too. No stale time-based model cache.
    return value not in options


def _folder(name: str, class_type: str, options: tuple[str, ...]) -> str:
    if name in INPUT_FOLDERS:
        return INPUT_FOLDERS[name]
    if name in {"upscale_model", "model_name"} and "upscale" in class_type.lower():
        return "upscale_models"
    if not options:
        return ""
    try:
        import folder_paths
        matches = [key for key in folder_paths.folder_names_and_paths
                   if set(map(_normal, folder_paths.get_filename_list(key))) == set(options)]
        return matches[0] if len(matches) == 1 else ""
    except (ImportError, AttributeError, OSError):
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
                      if isinstance(value, str) and os.path.splitext(value)[1].lower() in MODEL_EXTENSIONS]
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
                    missing.append({"file": value, "folder": _folder(name, class_type, options),
                                    "node": str(node.get("_meta", {}).get("title") or class_type),
                                    "node_id": str(key), "input": name})
        except Exception:
            logger.debug("Cannot inspect model inputs for %s", class_type, exc_info=True)
    return missing


def missing_model_message(item: dict) -> str:
    where = f" from models/{item['folder']}" if item.get("folder") else ""
    return f"{item['file']} is missing{where} ({item['node']})"
