"""SDK templates (exported API workflows), their Control values, and the SDK editor position.

Install-level files under ``<comfy user dir>/sonder-editor`` (``sdk_paths``):

- ``templates/<id>.json`` — the exported API workflow, byte for byte as imported.
  Never rewritten: a re-import replaces it whole.
- ``templates/<id>.meta.json`` — name, times and the validation summary.
- ``templates/<id>.controls.json`` — Control values per workflow, shared across
  projects: ``{"version": 1, "controls": {<control_id>: {...}}, "fields":
  {"<class_type>/<field>": value}}``. Keyed by ``control_id`` so values survive
  re-import. Values of Controls a re-import removed are kept but ignored.
- ``sdk_state/<project folder>.json`` — the SDK editor position (the 13 relayed
  editor values) per project, shared across workflows; last writer wins.

Everything here blocks on disk or on describe: call it off the event loop.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Any, Callable

from . import control_descriptors, prompt_graph
from .external_links import WINDOWS_RESERVED_BASENAMES
from .editor_inputs import clean_editor_values
from .path_security import PathSecurityError, safe_route_token
from .sdk_paths import json_sidecar_lock, read_json_sidecar, sonder_user_dir, write_json_atomic

TEMPLATE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_TEMPLATE_BYTES = 8 * 1024 * 1024
MAX_TEMPLATES = 200
MAX_NAME_LENGTH = 120
CONTROLS_VERSION = 1

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
    return f"{base}.json", f"{base}.meta.json", f"{base}.controls.json"


def template_lock(template_id: str):
    """The lock every write to one template's files takes (Generate holds it too)."""
    return json_sidecar_lock(_template_paths(template_id)[0])


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
    describe = describe or (lambda p: control_descriptors.describe_controls(p, class_lookup=class_lookup))
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
        has_seed = False
        for control in described.get("controls", []):
            label = control.get("label") or control_descriptors.DEFAULT_CONTROL_LABEL
            controls_summary.append({
                "control_id": control.get("control_id", ""),
                "label": label,
                "kind": control.get("kind", ""),
                "status": control.get("status", ""),
                "group": control.get("group", ""),
            })
            if control.get("kind") == "SEED":
                has_seed = True
            problem = generate_refusal(control)
            if problem:
                blocking.append(problem)
            elif control.get("status") == control_descriptors.STATUS_UNLINKED:
                warnings.append(f"“{label}” is not connected to anything.")
        for field in described.get("fields", []):
            if field.get("status") != control_descriptors.STATUS_OK:
                warnings.append(f"“{field.get('label')}” can't be set from here: {field.get('reason')}")
        if not has_seed:
            warnings.append(
                "No Seed Control: every Generate of the same job uses the workflow's fixed seed. "
                "Add a Sonder Control on the seed and export again to get new takes."
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


_REFUSED_STATUSES = {
    control_descriptors.STATUS_STALE: "its type no longer matches what it is connected to",
    control_descriptors.STATUS_DUPLICATE: "two different Controls share its id",
    control_descriptors.STATUS_UNSUPPORTED: "it is connected to an input it can't set",
    control_descriptors.STATUS_MIXED: "it is connected to inputs of different types",
}


def generate_refusal(control: dict) -> str:
    """Why Generate refuses this Control descriptor, or "" when it may run.

    ``unlinked`` is not refused: an unconnected Control changes nothing.
    """
    label = control.get("label") or control_descriptors.DEFAULT_CONTROL_LABEL
    reason = _REFUSED_STATUSES.get(control.get("status"))
    if reason:
        return f"“{label}”: {reason}."
    if control.get("error"):
        return f"“{label}”: {control['error']}"
    if not control.get("control_id"):
        return f"“{label}” has no id. Open the workflow in ComfyUI, save it, and export again."
    return ""


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
    }


def list_templates() -> list[dict]:
    folder = templates_dir()
    if not os.path.isdir(folder):
        return []
    result = []
    for entry in sorted(os.listdir(folder)):
        if not entry.endswith(".meta.json"):
            continue
        template_id = entry[: -len(".meta.json")]
        if not TEMPLATE_ID_RE.match(template_id):
            continue
        json_path, meta_path, _ = _template_paths(template_id)
        if not os.path.isfile(json_path):
            continue
        with template_lock(template_id):
            meta = read_json_sidecar(meta_path)
        result.append(_public_meta(template_id, meta))
    result.sort(key=lambda item: (item["name"].lower(), item["template_id"]))
    return result


def import_template(raw: bytes, *, source_name: str = "", name: str = "", validate=validate_template) -> dict:
    prompt = parse_template_bytes(raw)
    validation = validate(prompt)
    if not validation["ok"]:
        raise TemplateError(" ".join(validation["errors"]), "invalid_template", 400, validation=validation)
    folder = templates_dir()
    if len(list_templates()) >= MAX_TEMPLATES:
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
    json_path, meta_path, _ = _template_paths(template_id)
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
        _write_bytes_atomic(json_path, raw)
        write_json_atomic(meta_path, meta)
    return _public_meta(template_id, meta)


def rename_template(template_id: str, name: Any) -> dict:
    json_path, meta_path, _ = _template_paths(template_id)
    with template_lock(template_id):
        if not os.path.isfile(json_path):
            raise TemplateError("Unknown workflow.", "unknown_template", 404)
        meta = read_json_sidecar(meta_path)
        meta["name"] = _clean_name(name, meta.get("name") or "Workflow")
        write_json_atomic(meta_path, meta)
    return _public_meta(template_id, meta)


def delete_template(template_id: str) -> None:
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
            prompt = parse_template_bytes(handle.read())
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

def _normalized_controls(data: dict) -> dict:
    data = dict(data)
    data["version"] = CONTROLS_VERSION
    if not isinstance(data.get("controls"), dict):
        data["controls"] = {}
    if not isinstance(data.get("fields"), dict):
        data["fields"] = {}
    return data


def read_control_values(template_id: str) -> dict:
    _, _, controls_path = _template_paths(template_id)
    with template_lock(template_id):
        return _normalized_controls(read_json_sidecar(controls_path))


def update_control_values(template_id: str, mutate: Callable[[dict], None]) -> dict:
    """Read, mutate in place and write the Control values, under the template lock.

    Refuses when the workflow was deleted meanwhile, so no orphan file appears.
    Unknown keys are preserved.
    """
    json_path, _, controls_path = _template_paths(template_id)
    with template_lock(template_id):
        if not os.path.isfile(json_path):
            raise TemplateError("Unknown workflow.", "unknown_template", 404)
        data = _normalized_controls(read_json_sidecar(controls_path))
        mutate(data)
        write_json_atomic(controls_path, data)
        return data


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


def write_editor_state(project_folder: str, values: Any) -> dict:
    """Merge valid values into the stored position (invalid ones are dropped)."""
    path = _state_path(project_folder)
    cleaned = clean_editor_values(values, EDITOR_STATE_FIELDS)
    with json_sidecar_lock(path):
        data = read_json_sidecar(path)
        merged = clean_editor_values(data.get("values"), EDITOR_STATE_FIELDS)
        merged.update(cleaned)
        data.update({"version": 1, "values": merged, "updated_at": time.time()})
        write_json_atomic(path, data)
    return merged
