"""SDK templates (exported API workflows), their Control values, and the SDK editor position.

Install-level files under ``<comfy user dir>/sonder-editor`` (``sdk_paths``):

- ``templates/<id>.json`` — the exported API workflow, byte for byte as imported.
  Never rewritten: a re-import replaces it whole.
- ``templates/<id>.meta.json`` — name, times and the validation summary.
- ``templates/<id>.controls.json`` — Control values per workflow, shared across
  projects: ``{"version": 1, "bindings": {<binding_id>: {"value", "seed_mode",
  "sig"}}, "labels", "notes", "layout", "seq", "presentation_hash"}``. Values
  are keyed by the Relay's ``binding_id`` so they survive re-import; ``sig``
  (target class, input and kind) resets a value whose binding now points at a
  different setting. ``labels``/``notes``/``layout`` are view-only overlays
  keyed ``binding:<id>`` or ``note:<id>``; they reset on re-import, bound to
  the export's SHA-256 ``presentation_hash`` for crash recovery. ``seq`` orders
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

    controls_summary: list[dict] = []
    if not errors:
        described = describe(prompt)
        blocking.extend(generate_refusals(described))
        has_seed = False
        for binding in described.get("bindings", []):
            label = binding.get("label") or control_descriptors.DEFAULT_LABEL
            controls_summary.append({
                "binding_id": binding.get("binding_id", ""),
                "label": label,
                "kind": binding.get("kind") or "",
                "status": binding.get("status", ""),
                "section": binding.get("section", "workflow"),
                "group": binding.get("group", ""),
            })
            if binding.get("kind") == "SEED" and binding.get("status") == control_descriptors.STATUS_OK:
                has_seed = True
            if binding.get("status") != control_descriptors.STATUS_OK:
                warnings.append(f"“{label}” is off: {binding.get('reason') or 'it cannot be set.'}")
        if not has_seed:
            warnings.append(
                "No seed exposed: every Generate of the same job uses the workflow's fixed seed. "
                "Expose the seed through the workflow's Sonder Control Relay and export again to get new takes."
            )
    return {
        "ok": not errors,
        "errors": errors,
        "blocking": blocking,
        "warnings": warnings,
        "editor_key": editor_key,
        "editor_defaults": editor_defaults,
        "controls": controls_summary,
    }


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
    if requirements:
        input_cache = {}
        for meta in result:
            try:
                with template_lock(meta["template_id"]):
                    prompt, _ = load_template(meta["template_id"])
                    stored = read_control_values(meta["template_id"])
                    # A stored binding value (a checkpoint, say) changes which
                    # model files the run needs.
                    if stored.get("bindings"):
                        apply_stored_values(prompt, control_descriptors.describe_bindings(prompt), stored)
                    meta["missing_models"] = model_requirements.missing_models(prompt, input_cache=input_cache)
            except Exception:  # one workflow's problem must not hide the whole list
                logger.warning("Model requirements failed for workflow %s", meta.get("template_id"), exc_info=True)
                meta["requirements_error"] = "Model requirements could not be checked. Check again."
    result.sort(key=lambda item: (item["name"].lower(), item["template_id"]))
    return result


def template_requirements(template_id: str) -> dict:
    """Selected-row fallback: avoid inspecting every schema in a large library."""
    with template_lock(template_id):
        prompt, _ = load_template(template_id)
        stored = read_control_values(template_id)
        if stored.get("bindings"):
            apply_stored_values(prompt, control_descriptors.describe_bindings(prompt), stored)
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


def replace_template(template_id: str, raw: bytes, *, source_name: str = "", validate=validate_template) -> dict:
    """Re-import over an existing workflow, keeping its id and Control values."""
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
        notes_reset = len(data["notes"])
        layout_reset = len(data["layout"]) + len(data["labels"])
        data.update(notes={}, layout={}, labels={}, presentation_hash=hashlib.sha256(raw).hexdigest(), seq=data["seq"] + 1)
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
    return {**_public_meta(template_id, meta), "notes_reset": notes_reset, "layout_reset": layout_reset}


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
    for name in ("bindings", "labels", "notes", "layout"):
        if not isinstance(data.get(name), dict):
            data[name] = {}
    if data.get("presentation_hash") != presentation_hash:
        data["labels"] = {}
        data["notes"] = {}
        data["layout"] = {}
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


def apply_stored_values(prompt: dict, described: dict, stored: dict, *, describe=None) -> dict:
    """Apply stored values to the prompt and presentation overlays to a view only.

    Generate, Controls GET and the Launcher's model check share this authority.
    A value applies only to an ``ok`` binding whose target still matches the
    signature it was saved for; anything else is skipped and the binding shows
    ``reset``. Re-describing the resulting prompt rechecks bounds and options.
    Labels, notes and layout never alter a prompt input or its cache identity.
    """
    cd = control_descriptors
    stored_bindings = stored.get("bindings") if isinstance(stored.get("bindings"), dict) else {}
    by_id = {d.get("binding_id"): d for d in described.get("bindings", [])}
    batch, resets = {}, set()
    for binding_id, descriptor in by_id.items():
        entry = stored_bindings.get(binding_id)
        if descriptor.get("status") != cd.STATUS_OK or not isinstance(entry, dict) or "value" not in entry:
            continue
        if entry.get("sig") != cd.binding_signature(descriptor):
            resets.add(binding_id)
            continue
        batch[f"binding:{binding_id}:value"] = entry["value"]
    # One batch (uncapped: up to 256 stored values) so a stored choice list and
    # choice agree. Validation reports every refusal, so reject only those.
    cleaned, rejected = cd.validate_binding_changes(described, batch, prompt=prompt, max_changes=None) if batch else ({}, [])
    if rejected:
        refused = {row.get("target") for row in rejected}
        resets.update(target.split(":")[1] for target in batch if target in refused)
        cleaned = {target: value for target, value in batch.items() if target not in refused}
        cleaned, _ = cd.validate_binding_changes(described, cleaned, prompt=prompt, max_changes=None) if cleaned else ({}, [])
    for target, value in cleaned.items():
        for item in by_id[target.split(":")[1]].get("targets", []):
            prompt[item["path"]]["inputs"][item["input"]] = value
    view = copy.deepcopy((describe or cd.describe_bindings)(prompt) if cleaned else described)
    layouts = stored.get("layout") if isinstance(stored.get("layout"), dict) else {}
    notes = stored.get("notes") if isinstance(stored.get("notes"), dict) else {}
    labels = stored.get("labels") if isinstance(stored.get("labels"), dict) else {}
    for descriptor in view.get("bindings", []):
        binding_id = descriptor.get("binding_id")
        entry = stored_bindings.get(binding_id)
        entry = entry if isinstance(entry, dict) else {}
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
            layout = layouts.get(base)
            if isinstance(layout, dict):
                # Preserve unknown/over-cap storage, consume only modeled values.
                for field in ("order", "group"):
                    if field in layout:
                        try:
                            descriptor[field] = cd.coerce_panel_layout(field, layout[field])
                        except cd.ControlValueError:
                            pass
            text = notes.get(base)
            if isinstance(text, str):
                descriptor[text_key] = text[:cd.PANEL_NOTE_MAX_CHARS]
            label = labels.get(base)
            if prefix == "binding" and isinstance(label, str) and label.strip():
                descriptor["label"] = label.strip()[:cd.PANEL_LABEL_MAX_CHARS]
    return view


def store_binding_changes(template_id: str, changes: dict, view: dict) -> dict:
    """Persist already validated targets through the canonical sidecar mutator.

    ``view`` is the view the changes were validated against: a value is saved
    with its binding's target signature.
    """
    signatures = {d.get("binding_id"): control_descriptors.binding_signature(d) for d in view.get("bindings", [])}

    def mutate(data):
        for target, value in changes.items():
            source, identity, field = target.split(":", 2)
            base = f"{source}:{identity}"
            if field in ("note", "text"):
                data["notes"][base] = value
            elif field == "label":
                data["labels"][base] = value
            elif field in ("order", "group"):
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
    return update_control_values(template_id, mutate)


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
