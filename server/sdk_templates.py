"""SDK templates (exported API workflows), their Control values, and the SDK editor position.

Install-level files under ``<comfy user dir>/sonder-editor`` (``sdk_paths``):

- ``templates/<id>.json`` — the exported API workflow, byte for byte as imported.
  Never rewritten: a re-import replaces it whole.
- ``templates/<id>.meta.json`` — name, times and the validation summary.
- ``templates/<id>.controls.json`` — Control values per workflow, shared across
  projects: ``{"version": 1, "bindings": {<binding_id>: {"value", "seed_mode",
  "sig"}}, "local", "local_notes", "hidden", "labels", "notes", "layout",
  "seq", "presentation_hash"}``. Values are keyed by ``binding_id`` so they
  survive re-import; ``sig`` (target class, input and kind) resets a value
  whose binding now points at a different setting. ``labels``/``notes``/
  ``layout`` are view-only overlays on the workflow's own bindings and notes,
  keyed ``binding:<id>`` or ``note:<id>``; they reset on re-import, bound to
  the export's SHA-256 ``presentation_hash`` for crash recovery.
  App-only curation (contract amendment A1) survives re-import: ``local``
  holds app-only bindings ``{<id>: {targets: [{path, input, class_type}],
  section, label, group, order, note}}``, ``local_notes`` app-only notes
  ``{<id>: {text, group, order}}``, and ``hidden`` the workflow's own bindings
  and notes hidden here (``{"binding:<id>"|"note:<id>": true}``). ``seq`` orders
  changes and seed advances. Unknown keys (the retired ``controls``/``fields``
  sections included) are preserved and ignored.
- ``sdk_state/<project folder>.json`` — the SDK editor position (the 13 relayed
  editor values) per project, shared across workflows; last writer wins.

Everything here blocks on disk or on describe: call it off the event loop.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Callable

from . import control_descriptors, prompt_graph, model_requirements
from .external_links import WINDOWS_RESERVED_BASENAMES
from .editor_inputs import clean_editor_values
from .path_security import PathSecurityError, safe_route_token
from .sdk_paths import json_sidecar_lock, read_json_sidecar, sonder_user_dir, write_json_atomic

TEMPLATE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_TEMPLATE_BYTES = 8 * 1024 * 1024
MAX_TEMPLATES = 200
MAX_NAME_LENGTH = 120
CONTROLS_VERSION = 1
logger = logging.getLogger(__name__)
STARTERS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sdk_starters")
_starter_validation_cache = {}


from . import control_presets


def starter_sources() -> dict[str, str]:
    if not os.path.isdir(STARTERS_DIR):
        return {}
    return {hashlib.sha256(("sonder-starter:" + name[:-5]).encode()).hexdigest()[:16]:
            os.path.join(STARTERS_DIR, name)
            for name in sorted(os.listdir(STARTERS_DIR))
            if name.endswith(".json") and not name.endswith(".meta.json")
            and os.path.isfile(os.path.join(STARTERS_DIR, name))}


def template_source_path(template_id: str) -> str:
    identity = check_template_id(template_id)
    return starter_sources().get(identity) or os.path.join(templates_dir(), identity + ".json")


def _require_writable(template_id: str) -> None:
    if check_template_id(template_id) in starter_sources():
        raise TemplateError("Bundled workflows are read-only.", "starter_read_only", 409)

# The 13 editor values a mounted host relays (``session_registry.WIDGET_FIELD_NAMES``),
# which are also what the SDK host stores. Kept as a literal list here so this module
# does not import the registry; ``tests/test_sdk_templates.py`` pins the two equal.
EDITOR_STATE_FIELDS = (
    "scene_id", "selection_start", "selection_end",
    "pre_context_frames", "post_context_frames",
    "mask_pre_offset", "mask_post_offset",
    "take_placement_mode", "take_placement_linked", "take_placement_muted",
    "take_fit_mode", "take_crop_position", "render_queue_active",
)


class TemplateError(ValueError):
    """A template request refused with a code and an HTTP status."""

    def __init__(self, message: str, code: str = "invalid_template", status: int = 400, **details: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


# ── Paths ────────────────────────────────────────────────────────────────────

def _root(name: str) -> str:
    base = sonder_user_dir()
    if not base:
        raise TemplateError("ComfyUI's user directory is unavailable.", "storage_unavailable", 500)
    return os.path.join(base, name)


def templates_dir() -> str:
    return _root("templates")


def check_template_id(template_id: Any) -> str:
    template_id = str(template_id or "")
    if not TEMPLATE_ID_RE.match(template_id):
        raise TemplateError("Unknown workflow.", "unknown_template", 404)
    return template_id


def _template_paths(template_id: str) -> tuple[str, str, str]:
    base = os.path.join(templates_dir(), check_template_id(template_id))
    return template_source_path(template_id), f"{base}.meta.json", f"{base}.controls.json"


def template_lock(template_id: str):
    """The lock every write to one template's files takes (Generate holds it too)."""
    # Stable install-level lock, regardless of whether the source is bundled.
    return json_sidecar_lock(os.path.join(templates_dir(), check_template_id(template_id) + ".json"))


# ── Parsing and validation ───────────────────────────────────────────────────

def parse_template_bytes(raw: bytes) -> dict:
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise TemplateError("This workflow file is too large.", "too_large", 413)
    try:
        prompt = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise TemplateError("This file is not a JSON workflow.", "not_json") from None
    if not isinstance(prompt, dict) or not prompt:
        raise TemplateError("This file is not an exported API workflow.", "not_api_format")
    if "nodes" in prompt and "links" in prompt:
        raise TemplateError(
            "This is a UI workflow. In ComfyUI, use Workflow → Export (API) and import that file.",
            "ui_format",
        )
    for key, node in prompt.items():
        if not (isinstance(node, dict) and isinstance(node.get("class_type"), str)
                and isinstance(node.get("inputs"), dict)):
            raise TemplateError("This file is not an exported API workflow.", "not_api_format", node=str(key))
    return prompt


def _literal_number(value: Any) -> Any:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def validate_template(
    prompt: dict,
    *,
    class_lookup: Callable[[str], Any] | None = None,
    describe: Callable[[dict], dict] | None = None,
) -> dict:
    """Check an API workflow can drive the editor, and summarize its Controls.

    ``errors`` refuse the import. ``blocking`` lists what Generate will refuse
    until the workflow is fixed; ``warnings`` are informational.
    """
    class_lookup = class_lookup or control_descriptors.comfy_class_lookup
    describe = describe or (lambda p: control_descriptors.describe_bindings(p, class_lookup=class_lookup))
    errors: list[str] = []
    blocking: list[str] = []
    warnings: list[str] = []

    missing = sorted({node["class_type"] for node in prompt.values() if class_lookup(node["class_type"]) is None})
    if missing:
        errors.append("These nodes are not installed: " + ", ".join(missing) + ".")
    editors = prompt_graph.editor_keys(prompt)
    editor_key = editors[0] if len(editors) == 1 else ""
    if not editors:
        errors.append("The workflow has no Sonder Editor.")
    elif len(editors) > 1:
        errors.append("The workflow has more than one Sonder Editor.")
    elif not prompt_graph.nodes_reaching(prompt, editor_key, prompt_graph.SONDER_SAVE_CLASS_TYPES):
        errors.append("No Sonder Save Video or Save Bridge takes its project from the editor, so takes would not land.")

    editor_inputs = prompt_graph.node_inputs(prompt, editor_key) if editor_key else {}
    editor_defaults = {
        name: _literal_number(editor_inputs.get(name)) for name in ("fps", "width", "height")
    }

    description = control_descriptors.control_manifest.find_relay(prompt)["manifest"].get("description", "")
    controls_summary: list[dict] = []
    if not errors:
        described = describe(prompt)
        blocking.extend(generate_refusals(described))
        controls_summary, control_warnings = controls_summary_of(described)
        warnings.extend(control_warnings)
    return {
        "ok": not errors,
        "errors": errors,
        "blocking": blocking,
        "warnings": warnings,
        "editor_key": editor_key,
        "editor_defaults": editor_defaults,
        "controls": controls_summary,
        "description": description,
        "preset_count": len(control_manifest_of(prompt).get("presets", {})),
    }


def controls_summary_of(view: dict) -> tuple[list[dict], list[str]]:
    """The Launcher's Controls line and warnings for one effective view.

    Hidden bindings are not exposed, so they are left out; app-only ones count.
    These are every warning ``validate_template`` gives, so the Launcher may
    replace them wholesale when it recomputes them on read.
    """
    summary, warnings, has_seed = [], [], False
    for binding in view.get("bindings", []):
        if binding.get("hidden"):
            continue
        label = binding.get("label") or control_descriptors.DEFAULT_LABEL
        summary.append({
            "binding_id": binding.get("binding_id", ""),
            "label": label,
            "kind": binding.get("kind") or "",
            "status": binding.get("status", ""),
            "section": binding.get("section", "workflow"),
            "group": binding.get("group", ""),
            "origin": binding.get("origin", "authored"),
            "advanced": binding.get("advanced") is True,
        })
        if binding.get("kind") == "SEED" and binding.get("status") == control_descriptors.STATUS_OK:
            has_seed = True
        if binding.get("status") != control_descriptors.STATUS_OK:
            warnings.append(f"“{label}” is off: {binding.get('reason') or 'it cannot be set.'}")
    if not has_seed:
        warnings.append(
            "No seed exposed: every Generate of the same job uses the workflow's fixed seed. "
            "Expose the seed in Controls → Manage, or through the workflow's Sonder Control Relay, to get new takes."
        )
    return summary, warnings


def generate_refusals(view: dict) -> list[str]:
    """Why Generate refuses this Controls view; empty when it may run.

    Only a Relay that cannot be read refuses. A binding that is missing, linked
    or otherwise off skips its override and the workflow's own value runs, as a
    native Run would: a settled problem never blocks a run.
    """
    relay = view.get("relay") or {}
    if relay.get("status") == "error":
        return [f"Controls: {relay.get('error') or 'the Sonder Control Relay cannot be read.'}"]
    return []


# ── Store ────────────────────────────────────────────────────────────────────

def _clean_name(name: Any, fallback: str = "Workflow") -> str:
    text = " ".join(str(name or "").split())[:MAX_NAME_LENGTH]
    return text or fallback


def _name_from_source(source_name: str) -> str:
    base = os.path.basename(str(source_name or ""))
    stem = base[:-5] if base.lower().endswith(".json") else base
    return _clean_name(stem)


def _public_meta(template_id: str, meta: dict) -> dict:
    return {
        "template_id": template_id,
        "name": meta.get("name", ""),
        "description": (meta.get("validation") or {}).get("description", ""),
        "source_name": meta.get("source_name", ""),
        "imported_at": meta.get("imported_at", 0),
        "replaced_at": meta.get("replaced_at", 0),
        "validation": meta.get("validation", {}),
        "starter": bool(meta.get("starter")),
        "model_family": meta.get("model_family", ""),
        "order": meta.get("order", 0),
    }


def list_templates(*, requirements: bool = False) -> list[dict]:
    folder = templates_dir()
    result = []
    starters = starter_sources()
    for entry in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not entry.endswith(".meta.json"):
            continue
        template_id = entry[: -len(".meta.json")]
        if not TEMPLATE_ID_RE.match(template_id):
            continue
        if template_id in starters:
            continue
        json_path, meta_path, _ = _template_paths(template_id)
        if not os.path.isfile(json_path):
            continue
        with template_lock(template_id):
            meta = read_json_sidecar(meta_path)
        result.append(_public_meta(template_id, meta))
    for template_id in starters:
        try:
            _, meta = load_template(template_id)
            result.append(meta)
        except (TemplateError, OSError):
            logger.warning("Cannot read bundled workflow %s", template_id, exc_info=True)
    for meta in result:
        _overlay_summary(meta)
    if requirements:
        input_cache = {}
        for meta in result:
            try:
                with template_lock(meta["template_id"]):
                    prompt, _ = load_template(meta["template_id"])
                    stored = read_control_values(meta["template_id"])
                    # A stored binding value (a checkpoint, say) changes which
                    # model files the run needs.
                    if _has_curation(stored):
                        apply_stored_values(prompt, describe_stored(prompt, stored), stored)
                    meta["missing_models"] = model_requirements.missing_models(prompt, input_cache=input_cache)
            except Exception:  # one workflow's problem must not hide the whole list
                logger.warning("Model requirements failed for workflow %s", meta.get("template_id"), exc_info=True)
                meta["requirements_error"] = "Model requirements could not be checked. Check again."
    result.sort(key=lambda item: (item["name"].lower(), item["template_id"]))
    return result


# (template_id) -> (sidecar seq, presentation hash, summary, warnings)
_summary_cache: dict[str, tuple] = {}


def _has_curation(stored: dict) -> bool:
    # Notes and layout count too: the summary carries groups and Advanced.
    return any(stored.get(name) for name in ("bindings", "local", "local_notes", "hidden", "labels",
                                             "notes", "layout", "presets")) or "description" in stored


def _overlay_summary(meta: dict) -> None:
    """Recompute the Controls summary and its warnings from the sidecar, on read.

    The import-time summary cannot see app-only bindings, hidden ones or
    renames. Untouched workflows keep it; others are recomputed once per
    sidecar change. A failure keeps the stored summary.
    """
    template_id = meta.get("template_id")
    validation = meta.get("validation")
    if not isinstance(validation, dict) or not validation.get("ok", True) or validation.get("errors"):
        return
    try:
        with template_lock(template_id):
            stored = read_control_values(template_id)
            if not _has_curation(stored):
                return
            key = (stored["seq"], stored["presentation_hash"])
            cached = _summary_cache.get(template_id)
            if not cached or cached[0] != key:
                prompt, _ = load_template(template_id)
                view = apply_stored_values(prompt, describe_stored(prompt, stored), stored)
                cached = (key, *controls_summary_of(view), view.get("description", ""), len(view.get("presets", [])))
                _summary_cache[template_id] = cached
    except Exception:
        logger.warning("Controls summary failed for workflow %s", template_id, exc_info=True)
        return
    meta["description"] = cached[3]
    meta["validation"] = {**validation, "controls": copy.deepcopy(cached[1]), "warnings": list(cached[2]), "description": cached[3], "preset_count": cached[4]}


def template_requirements(template_id: str) -> dict:
    """Selected-row fallback: avoid inspecting every schema in a large library."""
    with template_lock(template_id):
        prompt, _ = load_template(template_id)
        stored = read_control_values(template_id)
        if _has_curation(stored):
            apply_stored_values(prompt, describe_stored(prompt, stored), stored)
        return {"ok": True, "template_id": template_id, "missing_models": model_requirements.missing_models(prompt)}


def import_template(raw: bytes, *, source_name: str = "", name: str = "", validate=validate_template) -> dict:
    prompt = parse_template_bytes(raw)
    validation = validate(prompt)
    if not validation["ok"]:
        raise TemplateError(" ".join(validation["errors"]), "invalid_template", 400, validation=validation)
    folder = templates_dir()
    if sum(not t.get("starter") for t in list_templates()) >= MAX_TEMPLATES:
        raise TemplateError("Too many workflows. Delete one first.", "too_many_templates", 409)
    template_id = secrets.token_hex(8)
    json_path, meta_path, _ = _template_paths(template_id)
    os.makedirs(folder, exist_ok=True)
    now = time.time()
    meta = {
        "name": _clean_name(name, _name_from_source(source_name)),
        "source_name": os.path.basename(str(source_name or ""))[:MAX_NAME_LENGTH],
        "imported_at": now,
        "replaced_at": 0,
        "validation": validation,
    }
    with template_lock(template_id):
        _write_bytes_atomic(json_path, raw)
        write_json_atomic(meta_path, meta)
    return _public_meta(template_id, meta)


def replace_template(template_id: str, raw: bytes, *, source_name: str = "", validate=validate_template,
                     describe=None) -> dict:
    """Re-import over an existing workflow, keeping its id, Control values and
    app-only curation; authored overlays (labels, notes, layout) reset."""
    _require_writable(template_id)
    json_path, meta_path, controls_path = _template_paths(template_id)
    prompt = parse_template_bytes(raw)
    validation = validate(prompt)
    if not validation["ok"]:
        raise TemplateError(" ".join(validation["errors"]), "invalid_template", 400, validation=validation)
    with template_lock(template_id):
        if not os.path.isfile(json_path):
            raise TemplateError("Unknown workflow.", "unknown_template", 404)
        meta = read_json_sidecar(meta_path)
        meta.update({
            "source_name": os.path.basename(str(source_name or ""))[:MAX_NAME_LENGTH] or meta.get("source_name", ""),
            "replaced_at": time.time(),
            "validation": validation,
        })
        meta.setdefault("name", _name_from_source(source_name))
        data = read_control_values(template_id)
        old_presets = control_manifest_of(parse_template_bytes(_read_bytes(json_path)))['presets']
        new_presets = control_manifest_of(prompt)['presets']
        presets_kept = sum(1 for identity, entry in data.get("presets", {}).items()
                           if isinstance(entry, dict) and identity in new_presets
                           and old_presets.get(identity) != new_presets[identity])
        description_reset = "description" in data
        data.pop("description", None)
        notes_reset = len(data["notes"])
        layout_reset = len(data["layout"]) + len(data["labels"])
        data.update(notes={}, layout={}, labels={}, presentation_hash=hashlib.sha256(raw).hexdigest(), seq=data["seq"] + 1)
        # Preserved app copies and newly authored presets must remain portable.
        _check_preset_projection(prompt, data, describe)
        # The file lock makes the three writes one transaction for live readers.
        # Restore exact bytes on a failed write. The hash binding below also
        # prevents old notes appearing over a new export after a process crash.
        paths = (json_path, controls_path, meta_path)
        before = {}
        for path in paths:
            before[path] = _read_bytes(path)
        written = []
        try:
            _write_bytes_atomic(json_path, raw)
            written.append(json_path)
            write_json_atomic(controls_path, data)
            written.append(controls_path)
            write_json_atomic(meta_path, meta)
        except BaseException:
            for path in reversed(written):
                try:
                    if before[path] is None:
                        os.remove(path)
                    else:
                        _write_bytes_atomic(path, before[path])
                except OSError:
                    logger.exception("Could not restore workflow file %s", path)
            raise
    local_missing = local_shadowed = 0
    if data.get("local"):
        try:
            view = describe_stored(prompt, data, describe)
            for binding in view.get("bindings", []):
                if binding.get("origin") == "local":
                    local_missing += binding.get("status") == control_descriptors.STATUS_MISSING
                    local_shadowed += binding.get("status") == control_descriptors.STATUS_SHADOWED
        except Exception:
            logger.warning("Could not check app-only Controls of workflow %s", template_id, exc_info=True)
    return {**_public_meta(template_id, meta), "notes_reset": notes_reset, "layout_reset": layout_reset,
            "local_missing": local_missing, "local_shadowed": local_shadowed, "description_reset": description_reset, "presets_kept": presets_kept}


def export_curated(template_id: str) -> bytes:
    """A portable snapshot of the app's menu and values, without changing storage.

    The file lock joins source and sidecar reads. No runner lock or full node
    validation: exporting work must remain possible with uninstalled nodes.
    """
    cm = control_descriptors.control_manifest
    with template_lock(template_id):
        # Read the source directly: load_template validates bundled starters.
        raw = _read_bytes(template_source_path(template_id))
        if raw is None:
            raise TemplateError("Unknown workflow.", "unknown_template", 404)
        prompt = parse_template_bytes(raw)
        relay = cm.find_relay(prompt)
        if relay["status"] == "error":
            raise TemplateError(relay["error"], "invalid_controls")
        stored = read_control_values(template_id)
        view = apply_stored_values(prompt, describe_stored(prompt, stored), stored)
        if generate_refusals(view):
            raise TemplateError(" ".join(generate_refusals(view)), "invalid_controls")
        if len(view.get("_presets", {})) > cm.MAX_PRESETS:
            raise TemplateError("At most 32 presets fit in one workflow. Remove an app preset before exporting.", "invalid_controls")
        manifest = _curated_manifest(prompt, relay, stored, view)
        try:
            text = json.dumps(manifest, ensure_ascii=False)
            cm.parse_manifest(text)  # counts and UTF-8 cap, including unknown metadata
        except (cm.ManifestError, UnicodeError) as exc:
            raise TemplateError(str(exc), "invalid_controls") from None
        if relay["key"] or manifest["bindings"] or manifest["notes"] or manifest["groups"] or manifest["description"] or manifest["presets"]:
            key = relay["key"]
            if key is None:
                roots = [int(k) for k in prompt if k.isascii() and k.isdecimal()]
                key = str(max(roots, default=0) + 1)
                prompt[key] = {"class_type": cm.RELAY_CLASS_TYPE, "inputs": {}}
            prompt[key]["inputs"]["manifest"] = text
        for node in prompt.values():
            if isinstance(node.get("_meta"), dict):
                node["_meta"].pop("sonder_controls", None)
        exported = json.dumps(prompt, ensure_ascii=False).encode("utf-8")
        parse_template_bytes(exported)
        return exported



def _curated_manifest(prompt, relay, stored, view):
    """One materialization authority for export and preset-write caps."""
    cm = control_descriptors.control_manifest
    # Keep unknown author metadata, but materialize only effective menu rows.
    saved_text = prompt[relay["key"]]["inputs"].get("manifest") if relay["key"] else ""
    saved = json.loads(saved_text) if saved_text and saved_text.strip() else {}
    if len(view.get("group_order", [])) > cm.MAX_GROUPS:
        raise TemplateError("The exported menu has too many groups.", "invalid_controls")
    manifest = {**saved, "v": 1, "bindings": {}, "notes": {},
                "description": view.get("description", ""), "presets": view.get("_presets", {}),
                "groups": {name: {"order": i} for i, name in enumerate(view.get("group_order", []))}}
    local_bindings = control_descriptors.clean_local_bindings(stored.get("local"))
    labels = stored.get("labels") or {}
    for descriptor in view.get("bindings", []):
        if descriptor.get("hidden") or (descriptor.get("origin") == "local" and descriptor.get("status") != "ok"):
            continue
        identity = descriptor["binding_id"]
        entry = copy.deepcopy((saved.get("bindings") or {}).get(identity, {}))
        entry.update({key: descriptor.get(key, default) for key, default in
                      (("label", ""), ("note", ""), ("order", 0), ("group", ""), ("advanced", False))})
        # A derived label can exceed the authored-label cap. Keep the
        # empty authored label so the receiving app derives it identically.
        if descriptor.get("origin") == "local":
            label = local_bindings.get(identity, {}).get("label", "")
        else:
            authored_label = (saved.get("bindings") or {}).get(identity, {}).get("label", "")
            label = labels.get(f"binding:{identity}", authored_label)
        entry["label"] = label.strip()[:cm.LABEL_MAX] if isinstance(label, str) else ""
        if descriptor.get("origin") == "local":
            entry["targets"] = [{"path": t["path"], "input": t["input"]} for t in descriptor.get("targets", [])]
        mode = descriptor.get("seed_mode")
        if mode in cm.SEED_MODES:
            entry["seed_mode"] = mode
        for key in ("status", "suggestions", "error"):
            entry.pop(key, None)
        # Approved 4C amendment: off authored bindings retain their status.
        # It is semantic, not a cache: stripping it revives no-widget
        # bindings and duplicates whose other owner was hidden on export.
        if descriptor.get("origin") != "local" and descriptor.get("status") != "ok":
            entry["status"] = descriptor["status"]
        manifest["bindings"][identity] = entry
    for descriptor in view.get("notes", []):
        if descriptor.get("hidden"):
            continue
        identity = descriptor["note_id"]
        entry = copy.deepcopy((saved.get("notes") or {}).get(identity, {}))
        entry.update({key: descriptor.get(key, default) for key, default in (("text", ""), ("group", ""), ("order", 0))})
        manifest["notes"][identity] = entry
    for key in ("status", "suggestions", "error"):
        manifest.pop(key, None)
    return manifest

def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def rename_template(template_id: str, name: Any) -> dict:
    _require_writable(template_id)
    json_path, meta_path, _ = _template_paths(template_id)
    with template_lock(template_id):
        if not os.path.isfile(json_path):
            raise TemplateError("Unknown workflow.", "unknown_template", 404)
        meta = read_json_sidecar(meta_path)
        meta["name"] = _clean_name(name, meta.get("name") or "Workflow")
        write_json_atomic(meta_path, meta)
    return _public_meta(template_id, meta)


def delete_template(template_id: str) -> None:
    _require_writable(template_id)
    paths = _template_paths(template_id)
    with template_lock(template_id):
        if not os.path.isfile(paths[0]):
            raise TemplateError("Unknown workflow.", "unknown_template", 404)
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def load_template(template_id: str) -> tuple[dict, dict]:
    """The stored workflow and its public meta."""
    json_path, meta_path, _ = _template_paths(template_id)
    if not os.path.isfile(json_path):
        raise TemplateError("Unknown workflow.", "unknown_template", 404)
    with template_lock(template_id):
        with open(json_path, "rb") as handle:
            raw = handle.read()
            prompt = parse_template_bytes(raw)
        if template_id in starter_sources():
            digest = hashlib.sha256(raw).hexdigest()
            cached = _starter_validation_cache.get(template_id)
            if not cached or cached[0] != digest:
                cached = (digest, validate_template(prompt))
                _starter_validation_cache[template_id] = cached
            shipped = read_json_sidecar(json_path[:-5] + ".meta.json")
            order = shipped.get("order", 0)
            meta = {"name": _clean_name(shipped.get("name"), _name_from_source(os.path.basename(json_path))),
                    "source_name": os.path.basename(json_path), "starter": True,
                    "model_family": str(shipped.get("model_family") or ""),
                    "order": order if isinstance(order, (int, float)) else 0,
                    "validation": copy.deepcopy(cached[1])}
        else:
            meta = read_json_sidecar(meta_path)
    return prompt, _public_meta(template_id, meta)


def _write_bytes_atomic(path: str, data: bytes) -> None:
    from .atomic_io import atomic_replace

    tmp = f"{path}.{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        atomic_replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── Control values ───────────────────────────────────────────────────────────

def _normalized_controls(data: dict, presentation_hash: str) -> dict:
    data = dict(data)
    data["version"] = CONTROLS_VERSION
    for name in ("bindings", "labels", "notes", "layout", "local", "local_notes", "hidden", "presets"):
        if not isinstance(data.get(name), dict):
            data[name] = {}
    if data.get("presentation_hash") != presentation_hash:
        data["labels"] = {}
        data["notes"] = {}
        data["layout"] = {}
        data.pop("description", None)
    data["presentation_hash"] = presentation_hash
    seq = data.get("seq")
    data["seq"] = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else 0
    return data


def _presentation_hash(template_id: str) -> str:
    json_path, _, _ = _template_paths(template_id)
    with open(json_path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def read_control_values(template_id: str) -> dict:
    _, _, controls_path = _template_paths(template_id)
    with template_lock(template_id):
        return _normalized_controls(read_json_sidecar(controls_path), _presentation_hash(template_id))


def update_control_values(template_id: str, mutate: Callable[[dict], None]) -> dict:
    """Read, mutate in place and write the Control values, under the template lock.

    Refuses when the workflow was deleted meanwhile, so no orphan file appears.
    Unknown keys are preserved.
    """
    json_path, _, controls_path = _template_paths(template_id)
    with template_lock(template_id):
        if not os.path.isfile(json_path):
            raise TemplateError("Unknown workflow.", "unknown_template", 404)
        data = _normalized_controls(read_json_sidecar(controls_path), _presentation_hash(template_id))
        mutate(data)
        data["seq"] += 1
        write_json_atomic(controls_path, data)
        return data


def describe_stored(prompt: dict, stored: dict, describe=None) -> dict:
    """Describe a template with its sidecar's app-only bindings and notes."""
    describe = describe or control_descriptors.describe_bindings
    return describe(prompt, local=stored.get("local"), local_notes=stored.get("local_notes"))


def hidden_keys(stored: dict) -> set:
    hidden = stored.get("hidden") if isinstance(stored.get("hidden"), dict) else {}
    return {key for key, value in hidden.items() if value is True and isinstance(key, str)}


def apply_stored_values(prompt: dict, described: dict, stored: dict, *, describe=None) -> dict:
    """Apply stored values to the prompt and presentation overlays to a view only.

    Generate, Controls GET and the Launcher's model check share this authority.
    ``described`` comes from ``describe_stored``. A value applies only to an
    ``ok`` binding whose target still matches the signature it was saved for;
    anything else is skipped and the binding shows ``reset``. A hidden binding
    is not exposed: its stored value is kept but skipped, so the workflow's own
    value runs. Re-describing the resulting prompt rechecks bounds and options.
    Labels, notes and layout never alter a prompt input or its cache identity.
    """
    cd = control_descriptors
    stored_bindings = stored.get("bindings") if isinstance(stored.get("bindings"), dict) else {}
    hidden = hidden_keys(stored)
    by_id = {d.get("binding_id"): d for d in described.get("bindings", [])}
    batch, resets = {}, set()
    for binding_id, descriptor in by_id.items():
        entry = stored_bindings.get(binding_id)
        if descriptor.get("status") != cd.STATUS_OK or not isinstance(entry, dict) or "value" not in entry:
            continue
        if f"binding:{binding_id}" in hidden:
            continue
        if entry.get("sig") != cd.binding_signature(descriptor):
            resets.add(binding_id)
            continue
        batch[f"binding:{binding_id}:value"] = entry["value"]
    # One batch (uncapped: up to 256 stored values) so a stored choice list and
    # choice agree. Validation reports every refusal, so reject only those.
    cleaned, rejected = cd.validate_binding_changes(described, batch, prompt=prompt, max_changes=None) if batch else ({}, [])
    binding_of = lambda target: cd.parse_target(target)[1]  # noqa: E731 - every key here is binding:<id>:value
    if rejected:
        refused = {row.get("target") for row in rejected}
        resets.update(binding_of(target) for target in batch if target in refused)
        cleaned = {target: value for target, value in batch.items() if target not in refused}
        cleaned, _ = cd.validate_binding_changes(described, cleaned, prompt=prompt, max_changes=None) if cleaned else ({}, [])
    for target, value in cleaned.items():
        for item in by_id[binding_of(target)].get("targets", []):
            prompt[item["path"]]["inputs"][item["input"]] = value
    # A stored value that differs from the template's own literal: the panel
    # marks it where an Advanced row hides it ("N changed").
    overridden = {binding_of(target) for target, value in cleaned.items()
                  if str(value) != str(by_id[binding_of(target)].get("value"))}
    view = copy.deepcopy(describe_stored(prompt, stored, describe) if cleaned else described)
    layouts = stored.get("layout") if isinstance(stored.get("layout"), dict) else {}
    notes = stored.get("notes") if isinstance(stored.get("notes"), dict) else {}
    labels = stored.get("labels") if isinstance(stored.get("labels"), dict) else {}
    for descriptor in view.get("bindings", []):
        binding_id = descriptor.get("binding_id")
        entry = stored_bindings.get(binding_id)
        entry = entry if isinstance(entry, dict) else {}
        if binding_id in overridden:
            descriptor["overridden"] = True
        if binding_id in resets:
            descriptor["reset"] = True
            descriptor["reason"] = "Reset to the workflow's value: this setting changed since its value was saved."
        if descriptor.get("kind") == "SEED":
            mode = entry.get("seed_mode")
            exported = descriptor.get("seed_mode")
            descriptor["seed_mode"] = mode if mode in cd.SEED_MODES else (
                exported if exported in cd.SEED_MODES else "randomize")
    for descriptors, prefix, identity_key, text_key in (
        (view.get("bindings", []), "binding", "binding_id", "note"),
        (view.get("notes", []), "note", "note_id", "text"),
    ):
        for descriptor in descriptors:
            base = f"{prefix}:{descriptor.get(identity_key)}"
            if base in hidden and descriptor.get("origin") != "local":
                descriptor["hidden"] = True
            if descriptor.get("origin") == "local":
                continue  # app-only presentation lives in its own entry
            layout = layouts.get(base)
            if isinstance(layout, dict):
                # Preserve unknown/over-cap storage, consume only modeled values.
                if "order" in layout:
                    try:
                        descriptor["order"] = cd.coerce_panel_layout("order", layout["order"])
                    except cd.ControlValueError:
                        pass
                if isinstance(layout.get("group"), str):
                    descriptor["group"] = cd.control_manifest.clean_group(layout["group"])
                if prefix == "binding" and isinstance(layout.get("advanced"), bool):
                    descriptor["advanced"] = layout["advanced"]
            text = notes.get(base)
            if isinstance(text, str):
                descriptor[text_key] = text[:cd.PANEL_NOTE_MAX_CHARS]
            label = labels.get(base)
            if prefix == "binding" and isinstance(label, str):
                descriptor["label"] = (label.strip()[:cd.PANEL_LABEL_MAX_CHARS]
                                       or cd._default_label(prompt, descriptor.get("targets") or []))
    description = stored.get("description")
    if isinstance(description, str) and len(description) <= cd.control_manifest.DESCRIPTION_MAX:
        view["description"] = description
    if "relay" in view and not view.get("error"):
        view["groups"] = _overlay_groups(view.get("groups"), layouts)
        view["group_order"] = cd.group_order(view)  # the overlays may regroup
    effective, origins = control_presets.effective_presets(control_manifest_of(prompt).get("presets"), stored.get("presets"))
    view["_presets"] = effective
    view["presets"] = cd.preset_states(view, effective, origins=origins, prompt=prompt)
    from .control_cleanup import inventory
    view["cleanup"] = inventory(view, stored=stored)
    return view


GROUP_LAYOUT_PREFIX = "group:"


def _layout_groups(layouts: dict):
    """``(name, entry)`` for each valid ``layout["group:<name>"]`` overlay."""
    for key, entry in layouts.items():
        if not (isinstance(key, str) and key.startswith(GROUP_LAYOUT_PREFIX) and isinstance(entry, dict)):
            continue
        name = key[len(GROUP_LAYOUT_PREFIX):]
        if name == name.strip() and not control_descriptors.control_manifest.group_name_error(name):
            yield name, entry


def _overlay_groups(authored: Any, layouts: dict) -> dict:
    """The groups map the app shows: the workflow's own, with the app's group
    order applied, app-only groups added and groups it renamed away suppressed."""
    groups = {name: dict(entry) for name, entry in (authored or {}).items() if isinstance(entry, dict)}
    for name, entry in _layout_groups(layouts):
        if entry.get("suppressed") is True:
            groups.pop(name, None)
            continue
        order = entry.get("order")
        if isinstance(order, int) and not isinstance(order, bool) and abs(order) <= control_descriptors.PANEL_ORDER_LIMIT:
            groups[name] = {**groups.get(name, {}), "order": order}
    return groups


def _class_of(prompt: dict | None, path: str) -> str | None:
    node = (prompt or {}).get(path)
    return node.get("class_type") if isinstance(node, dict) else None


def store_binding_changes(template_id: str, changes: dict, view: dict, prompt: dict | None = None) -> dict:
    """Persist already validated targets through the canonical sidecar mutator.

    ``view`` is the view the changes were validated against: a value is saved
    with its binding's target signature, and an app-only binding's or note's
    presentation goes to its own ``local`` entry rather than the overlays.
    ``prompt`` names the node classes an app-only binding remembers.
    """
    signatures = {d.get("binding_id"): control_descriptors.binding_signature(d) for d in view.get("bindings", [])}
    origins = {("binding", d.get("binding_id")): d.get("origin") for d in view.get("bindings", [])}
    origins.update({("note", d.get("note_id")): d.get("origin") for d in view.get("notes", [])})

    def locator(target: dict) -> dict:
        return {"path": target["path"], "input": target["input"],
                "class_type": target.get("class_type") or _class_of(prompt, target["path"])}

    authored_groups = set(control_manifest_of(prompt).get("groups") or {})

    def mutate(data):
        # Ephemeral batch projection: later group expansion sees earlier member
        # moves/creation and group edits, while disk still receives one write.
        current = copy.deepcopy(view)
        for target, value in changes.items():
            source, identity, field = control_descriptors.parse_target(target)
            base = f"{source}:{identity}"
            if isinstance(value, dict) and value.get("noop"):
                continue
            if source == "preset":
                authored = control_manifest_of(prompt).get("presets", {})
                effective, _ = control_presets.effective_presets(authored, data["presets"])
                if field == "reset":
                    data["presets"].pop(identity, None)
                elif field == "remove":
                    data["presets"][identity] = {"removed": True}
                else:
                    before = copy.deepcopy(effective)
                    control_presets.edit_definitions(effective, identity, field, value)
                    for key, definition in effective.items():
                        if key == identity or definition != before.get(key):
                            data["presets"][key] = definition
                continue
            if source == "workflow" and field == "restore":
                raise TemplateError("Restore applies to canvas workflows.", "invalid_controls")
            if source == "workflow" and field == "cleanup":
                # Re-read under the file lock: even a direct sidecar writer may
                # have changed eligibility since the runner's snapshot.
                from . import control_cleanup
                fresh_prompt, _ = load_template(template_id)
                fresh = apply_stored_values(fresh_prompt, describe_stored(fresh_prompt, data), data)
                try:
                    plan = control_cleanup.validate(fresh, value, mode="sdk")
                except control_descriptors.ControlValueError as exc:
                    raise TemplateError(str(exc), "invalid_controls") from None
                for bid in plan["bindings"]:
                    _store_structural(data, "binding", bid, "remove", {}, locator)
                for bid in plan["values"]:
                    data["bindings"].pop(bid, None)
                for key in plan["hidden"]:
                    data["hidden"].pop(key, None)
                # Edit raw definitions, not the tolerant read projection: future
                # metadata and unreadable, unselected members remain recoverable.
                relay = control_descriptors.control_manifest.find_relay(fresh_prompt)
                raw_manifest = fresh_prompt[relay["key"]]["inputs"].get("manifest") or {}
                if isinstance(raw_manifest, str):
                    raw_manifest = json.loads(raw_manifest)
                authored = raw_manifest.get("presets", {})
                effective = {}
                for row in plan["preset_members"]:
                    pid = row["preset_id"]
                    overlay = data["presets"].get(pid)
                    selected = overlay if pid in control_descriptors.control_manifest.read_presets({pid: overlay}) else authored.get(pid)
                    effective[pid] = copy.deepcopy(selected)
                changed = control_cleanup.remove_members(effective, plan["preset_members"])
                for pid in changed:
                    data["presets"][pid] = effective.get(pid, {"removed": True})
                if changed:
                    _check_preset_projection(fresh_prompt, data)
                continue
            if source == "workflow" and field == "description":
                data["description"] = value
                continue
            if source == "group":
                _store_group(data, current, authored_groups, identity, value)
                continue
            if field in ("expose", "rebind", "create", "remove", "hide", "unhide"):
                _store_structural(data, source, identity, field, value, locator)
                if field == "remove" or (source == "note" and field == "create"):
                    rows_key, id_key = ("bindings", "binding_id") if source == "binding" else ("notes", "note_id")
                    rows = current.setdefault(rows_key, [])
                    rows[:] = [d for d in rows if d.get(id_key) != identity]
                    if field == "create":
                        rows.append({"note_id": identity, "origin": "local", **data["local_notes"][identity]})
                continue
            if field in ("group", "order"):
                rows, key = ("bindings", "binding_id") if source == "binding" else ("notes", "note_id")
                for descriptor in current.get(rows, []):
                    if descriptor.get(key) == identity:
                        descriptor[field] = value
            if origins.get((source, identity)) == "local":
                table = data["local"] if source == "binding" else data["local_notes"]
                entry = table.get(identity)
                if isinstance(entry, dict) and field in ("label", "note", "text", "order", "group", "advanced"):
                    entry[field] = value
                    continue
            if field in ("note", "text"):
                data["notes"][base] = value
            elif field == "label":
                data["labels"][base] = value
            elif field in ("order", "group", "advanced"):
                entry = data["layout"].get(base)
                if not isinstance(entry, dict):
                    entry = data["layout"][base] = {}
                entry[field] = value
            else:
                entry = data["bindings"].get(identity)
                if not isinstance(entry, dict):
                    entry = data["bindings"][identity] = {}
                if field == "value":
                    entry["value"] = value
                    entry["sig"] = signatures.get(identity)
                else:
                    entry[field] = value
        if any((control_descriptors.parse_target(target) or (None,))[0] == "preset" for target in changes):
            _check_preset_projection(prompt, data)
    return update_control_values(template_id, mutate)


def _check_preset_projection(prompt, data, describe=None):
    cm = control_descriptors.control_manifest
    effective, _ = control_presets.effective_presets(control_manifest_of(prompt).get("presets"), data.get("presets"))
    if not effective:
        return
    if len(effective) > cm.MAX_PRESETS:
        raise TemplateError("At most 32 presets fit in one workflow. Remove an app preset before replacing this workflow.", "invalid_controls")
    projected_prompt = copy.deepcopy(prompt or {})
    projected_view = apply_stored_values(projected_prompt, describe_stored(projected_prompt, data, describe), data)
    projected = _curated_manifest(projected_prompt, cm.find_relay(projected_prompt), data, projected_view)
    try:
        cm.parse_manifest(json.dumps(projected, ensure_ascii=False))
    except cm.ManifestError as exc:
        raise TemplateError(str(exc), "invalid_controls") from None


def control_manifest_of(prompt: dict | None) -> dict:
    """The template's own saved manifest (empty without a usable Relay)."""
    return control_descriptors.control_manifest.find_relay(prompt or {})["manifest"]


def _store_group(data: dict, view: dict, authored_groups: set, name: str, plan: dict) -> None:
    """Expand one validated group plan against the app's current view, in the
    same sidecar mutation (one ``seq``). Members move through their own overlay:
    ``layout`` for the workflow's items, ``local``/``local_notes`` for app-only
    ones. Group order is written for every group, so implied groups keep their
    place; a workflow group renamed or ungrouped away is suppressed."""
    layout = data["layout"]
    order = control_descriptors.group_order(view)

    def regroup(old: str, new: str) -> None:
        for kind, rows, identity_key, local_table in (
                ("binding", view.get("bindings", []), "binding_id", "local"),
                ("note", view.get("notes", []), "note_id", "local_notes")):
            for descriptor in rows:
                if descriptor.get("group") != old:
                    continue
                descriptor["group"] = new
                identity = descriptor.get(identity_key)
                if descriptor.get("origin") == "local":
                    entry = data[local_table].get(identity)
                    if isinstance(entry, dict):
                        entry["group"] = new
                    continue
                entry = layout.get(f"{kind}:{identity}")
                if not isinstance(entry, dict):
                    entry = layout[f"{kind}:{identity}"] = {}
                entry["group"] = new

    def place(names: list[str]) -> None:
        view["groups"] = {group: {"order": i + 1} for i, group in enumerate(names)}
        for index, group in enumerate(names):
            entry = layout.get(GROUP_LAYOUT_PREFIX + group)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry.pop("suppressed", None)
            entry["order"] = index + 1
            layout[GROUP_LAYOUT_PREFIX + group] = entry

    def retire(group: str) -> None:
        view.get("groups", {}).pop(group, None)
        if group in authored_groups:
            layout[GROUP_LAYOUT_PREFIX + group] = {"suppressed": True}
        else:
            layout.pop(GROUP_LAYOUT_PREFIX + group, None)

    if plan.get("create"):
        place([g for g in order if g != name] + [name])
    elif plan.get("rename"):
        new = plan["rename"]
        regroup(name, new)
        place([new if g == name else g for g in order])
        retire(name)
    elif plan.get("ungroup"):
        regroup(name, "")
        retire(name)
    elif plan.get("move"):
        rest = [g for g in order if g != name]
        before = plan.get("before")
        index = rest.index(before) if before in rest else len(rest)
        place(rest[:index] + [name] + rest[index:])


def _store_structural(data: dict, source: str, identity: str, op: str, value: dict, locator) -> None:
    base = f"{source}:{identity}"
    if op == "hide":
        data["hidden"][base] = True
    elif op == "unhide":
        data["hidden"].pop(base, None)
    elif source == "note" and op == "create":
        data["local_notes"][identity] = {"text": value["text"], "group": value["group"], "order": value["order"]}
    elif source == "note" and op == "remove":
        data["local_notes"].pop(identity, None)
    elif op == "expose":
        data["local"][identity] = {"targets": [locator(t) for t in value["targets"]], "label": value["label"],
                                   "group": "", "order": value["order"], "advanced": False, "note": ""}
    elif op == "rebind":
        entry = data["local"].get(identity)
        if isinstance(entry, dict):
            entry["targets"] = [locator(t) for t in value["targets"]]
    elif op == "remove":
        data["local"].pop(identity, None)
        data["bindings"].pop(identity, None)
        for table in ("labels", "notes", "layout"):
            data[table].pop(base, None)


# ── SDK editor position ──────────────────────────────────────────────────────

def _state_path(project_folder: str) -> str:
    try:
        folder = safe_route_token(project_folder, "project")
    except PathSecurityError as exc:
        raise TemplateError(str(exc), "invalid_project", 400) from None
    if folder.split(".")[0].upper() in WINDOWS_RESERVED_BASENAMES:
        raise TemplateError("Unknown project.", "invalid_project", 400)
    return os.path.join(_root("sdk_state"), f"{folder}.json")


def read_editor_state(project_folder: str) -> dict:
    path = _state_path(project_folder)
    with json_sidecar_lock(path):
        data = read_json_sidecar(path)
    return clean_editor_values(data.get("values"), EDITOR_STATE_FIELDS)


def clean_editor_writer(writer: Any) -> dict:
    """Validate the per-page freshness stamp; a runtime write must supply one."""
    if not isinstance(writer, dict):
        raise TemplateError("Editor position needs a page and sequence.", "invalid_writer", 400)
    tab, seq = writer.get("tab"), writer.get("seq")
    if (not isinstance(tab, str) or not tab or len(tab) > 128
            or isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 9007199254740991):
        raise TemplateError("Editor position needs a valid page and sequence.", "invalid_writer", 400)
    return {"tab": tab, "seq": seq}


def write_editor_state(project_folder: str, values: Any, *, writer=None) -> dict:
    """Merge valid values, refusing older writes from the same page.

    Runtime callers must pass ``writer={tab, seq}``. The no-writer form exists
    only for test fixture setup and returns the same {values, stale} envelope.
    """
    writer = clean_editor_writer(writer) if writer is not None else None
    path = _state_path(project_folder)
    cleaned = clean_editor_values(values, EDITOR_STATE_FIELDS)
    with json_sidecar_lock(path):
        data = read_json_sidecar(path)
        merged = clean_editor_values(data.get("values"), EDITOR_STATE_FIELDS)
        if writer is not None:
            writers = data.get("writers")
            if not isinstance(writers, dict) or len(writers) > 16:
                writers = {}
            try:
                for tab, seq in writers.items():
                    clean_editor_writer({"tab": tab, "seq": seq})
            except TemplateError:
                writers = {}
            if writer["seq"] <= writers.get(writer["tab"], 0):
                return {"values": merged, "stale": True}
            writers.pop(writer["tab"], None)
            writers[writer["tab"]] = writer["seq"]
            while len(writers) > 16:
                writers.pop(next(iter(writers)))
            data["writers"] = writers
        merged.update(cleaned)
        data.update({"version": 1, "values": merged, "updated_at": time.time()})
        write_json_atomic(path, data)
    return {"values": merged, "stale": False}
