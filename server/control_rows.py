"""Row-group Controls: one Control for a node's whole dynamic row group.

ComfyUI's V3 ``_DynamicGroup`` input (io type ``COMFY_DYNAMICGROUP_V3``, first
consumer the 0.40 LoRA stack loaders ``LoadLoraModel`` / ``LoadLoraTextEncoder``)
repeats a template of widget fields as rows. The API prompt carries one flat
key per row field, ``<group>.<index>.<field>``; the node's base schema still
names the group itself, with its ``template``, ``min``, ``max`` and
``group_name``. Finalization (``get_finalized_class_inputs``) replaces the
group with the flat keys of the rows present and raises outside min..max.

A ``ROWS`` Control targets the group id (``{path, input: "loras"}``) and its
value is the ordered list of rows, ``[{<field>: value}]``. Binding a single row
field is refused: the frontend renames later rows when one is removed, so a
name-keyed binding would silently drive another row.

This module is the one authority for the row facts every host needs: the
spec, reading rows from a node's inputs, coercion and validation, the
canonical form used for every comparison, the flat-key write and the summary
text. ``web/js/control_rows.js`` mirrors only ``canonical`` and ``summary``.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

ROWS_KIND = "ROWS"
GROUP_TYPE = "COMFY_DYNAMICGROUP_V3"
MAX_ROWS = 20  # _DynamicGroup._MaxRows; a template cannot declare more
FIELD_KINDS = ("STRING", "INT", "FLOAT", "BOOLEAN", "COMBO")
_KEY_RE = re.compile(r"^(0|[1-9][0-9]*)\.([^.]+)$")
_SAFE_INTEGER = 2 ** 53


def _cd():
    # Lazy: control_descriptors imports this module at load time.
    from . import control_descriptors

    return control_descriptors


def group_input(class_inputs: Any, input_name: str) -> dict | None:
    """The group's extra dict when ``input_name`` is a dynamic row group."""
    info = _cd()._input_info(class_inputs, input_name)
    if info is None or info[0] != GROUP_TYPE:
        return None
    return info[1] if isinstance(info[1], dict) else {}


def group_ids(class_inputs: Any) -> list[str]:
    """Every dynamic row group a base schema declares, in schema order."""
    found = []
    for category in ("required", "optional"):
        entries = class_inputs.get(category) if isinstance(class_inputs, dict) else None
        for name, info in (entries.items() if isinstance(entries, dict) else []):
            if isinstance(info, (list, tuple)) and info and info[0] == GROUP_TYPE:
                found.append(name)
    return found


def row_field_group(class_inputs: Any, input_name: str) -> str | None:
    """The group a flat row-field name belongs to (``loras.0.strength`` -> ``loras``)."""
    for group in group_ids(class_inputs):
        if input_name.startswith(group + "."):
            return group
    return None


def build_spec(extra: dict, input_name: str) -> tuple[dict | None, str | None]:
    """``(spec, None)`` for a template every field of which is a plain value,
    else ``(None, reason)``.

    A seed field becomes an INT: a row has no seed control of its own.
    """
    cd = _cd()
    template = extra.get("template") if isinstance(extra.get("template"), dict) else {}
    required = template.get("required") if isinstance(template.get("required"), dict) else {}
    if template.get("optional"):
        return None, "Its rows have optional settings, which a Control can't set yet."
    fields = []
    for field_id, info in required.items():
        if not isinstance(info, (list, tuple)) or not info:
            return None, "Its rows can't be read."
        field_extra = info[1] if len(info) > 1 and isinstance(info[1], dict) else {}
        spec = cd.spec_from_input_info(info[0], field_extra, field_id)
        if spec.get("kind") == "SEED":
            spec["kind"] = "INT"
        if spec.get("kind") not in FIELD_KINDS:
            return None, "Its rows take a setting that can't be a Control."
        label = field_extra.get("display_name")
        fields.append({"id": field_id, "label": label if isinstance(label, str) and label else field_id, **spec})
    if not fields:
        return None, "Its rows can't be read."
    minimum, maximum = extra.get("min", 0), extra.get("max", MAX_ROWS)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or isinstance(maximum, bool) or not isinstance(maximum, int):
        return None, "Its row limits can't be read."
    maximum = max(1, min(maximum, MAX_ROWS))
    minimum = max(0, min(minimum, maximum))
    group_name = extra.get("group_name")
    return {
        "kind": ROWS_KIND,
        "group_name": group_name if isinstance(group_name, str) and group_name else input_name,
        "min": minimum,
        "max": maximum,
        "fields": fields,
        "new_row": {field["id"]: default_value(field) for field in fields},
    }, None


def default_value(field: dict) -> Any:
    """A new row's value for one field, as the frontend's Add creates it."""
    kind, default = field.get("kind"), field.get("default")
    if kind == "COMBO":
        options = field.get("options") or []
        return default if default in options else (options[0] if options else "")
    if kind == "BOOLEAN":
        return default if isinstance(default, bool) else False
    if kind in ("INT", "FLOAT"):
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            return default
        minimum = field.get("min")
        return minimum if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and minimum > 0 else 0
    return default if isinstance(default, str) else ""


def rows_from_inputs(inputs: dict, group: str, field_ids: list[str]) -> tuple[list[dict], bool]:
    """``(rows, linked)`` from a node's prompt inputs, in index order.

    Gaps are dropped (the frontend packs sparse rows on import); a missing field
    of a present row reads None. ``linked`` when any row field is a link.
    """
    cd = _cd()
    prefix = group + "."
    by_index: dict[int, dict] = {}
    linked = False
    for key, value in (inputs or {}).items():
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        match = _KEY_RE.match(key[len(prefix):])
        if not match or match.group(2) not in field_ids:
            continue
        if cd._is_link(value):
            linked = True
        by_index.setdefault(int(match.group(1)), {})[match.group(2)] = value
    rows = [{field: by_index[index].get(field) for field in field_ids} for index in sorted(by_index)]
    return rows, linked


def _check_bounds(value: Any, field: dict) -> None:
    cd = _cd()
    bounded = dict(field)
    for name in ("min", "max"):
        if isinstance(bounded.get(name), str):
            bounded[name] = cd.coerce_control_value(field["kind"], bounded[name])
    if bounded.get("options_truncated"):
        bounded.pop("options", None)
    cd.check_value_for_target(value, bounded)


def coerce_rows(raw: Any, spec: dict) -> list[dict]:
    """The row list in its fields' kinds, or ``ControlValueError``."""
    cd = _cd()
    if not isinstance(raw, list):
        raise cd.ControlValueError("Send the list of rows.")
    noun = spec.get("group_name") or "row"
    minimum, maximum = spec.get("min", 0), spec.get("max", MAX_ROWS)
    if len(raw) < minimum:
        raise cd.ControlValueError(f"Add at least {minimum} {_plural(noun, minimum)}.")
    if len(raw) > maximum:
        raise cd.ControlValueError(f"At most {maximum} {_plural(noun, maximum)} fit.")
    fields = spec.get("fields") or []
    ids = [field["id"] for field in fields]
    rows = []
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict) or set(row) != set(ids):
            raise cd.ControlValueError(f"{noun} {index} must set {', '.join(ids)}.")
        cleaned = {}
        for field in fields:
            value = row[field["id"]]
            try:
                if field["kind"] == "BOOLEAN":
                    if not isinstance(value, bool):
                        raise cd.ControlValueError("Choose on or off.")
                else:
                    value = cd.coerce_control_value(field["kind"], value)
                _check_bounds(value, field)
            except (cd.ControlValueError, OverflowError) as exc:
                raise cd.ControlValueError(f"{noun} {index} {field.get('label') or field['id']}: {exc}") from None
            cleaned[field["id"]] = value
        rows.append(cleaned)
    return rows


def coerce_change(raw: Any, descriptor: dict, *, defer_base: bool = False) -> list[dict] | dict:
    """A panel write: a row list, or ``{rows, base}`` where ``base`` is the
    list the edit started from. A base that no longer matches the current rows
    is refused, so two edits of one list never silently undo each other.

    ``defer_base`` (the mounted relay's pre-check, whose view may trail edits
    still queued for the canvas) checks only the shape and returns
    ``{rows, base}``, so the canvas writer checks the base against the live
    graph when the edit lands.
    """
    cd = _cd()
    if isinstance(raw, dict):
        if set(raw) != {"rows", "base"}:
            raise cd.ControlValueError("Send the rows and the list they were edited from.")
        if defer_base:
            if not value_shape_ok(raw["base"]):
                raise cd.ControlValueError("Send the rows and the list they were edited from.")
            return {"rows": coerce_rows(raw["rows"], descriptor), "base": raw["base"]}
        if canonical(raw["base"]) != canonical(descriptor.get("value")):
            raise cd.ControlValueError(
                f"The {_plural(descriptor.get('group_name') or 'row', 2)} changed. Your edit was not applied; try again.")
        raw = raw["rows"]
    return coerce_rows(raw, descriptor)


def _normal(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if float(value).is_integer() and abs(value) < _SAFE_INTEGER:
            return int(value)
        return float(value) if isinstance(value, float) else value
    if isinstance(value, dict):
        return {str(key): _normal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    return str(value)


def canonical(rows: Any) -> str:
    """The comparison form of a row list: sorted keys, 1.0 == 1.

    ``web/js/control_rows.js`` ``canonical`` mirrors it byte for byte.
    """
    return json.dumps(_normal(rows), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def apply_rows(inputs: dict, group: str, rows: list[dict]) -> None:
    """Replace a node's flat row keys with ``rows`` at indices 0..n-1, in place."""
    prefix = group + "."
    for key in [key for key in inputs if isinstance(key, str) and key.startswith(prefix)]:
        del inputs[key]
    for index, row in enumerate(rows):
        for field, value in row.items():
            inputs[f"{group}.{index}.{field}"] = value


def _plural(noun: str, count: int) -> str:
    return noun if count == 1 else plural_name(noun)


def plural_name(noun: str) -> str:
    """``LoRA`` -> ``LoRAs``: a group's rows in a label. JS ``pluralName`` mirrors it."""
    return noun + "s"


def summary(rows: Any, spec: dict) -> str:
    """``3 LoRAs · 2 on``. ``web/js/control_rows.js`` ``summary`` mirrors it."""
    rows = rows if isinstance(rows, list) else []
    noun = spec.get("group_name") or "row"
    text = f"{len(rows)} {_plural(noun, len(rows))}"
    if any(field.get("id") == "enabled" and field.get("kind") == "BOOLEAN" for field in spec.get("fields") or []):
        text += f" · {sum(1 for row in rows if isinstance(row, dict) and row.get('enabled') is True)} on"
    return text


def detail(rows: Any, spec: dict, limit: int, *, marker: str = "…") -> str:
    """The summary followed by each row's values, for provenance text."""
    fields = [field["id"] for field in spec.get("fields") or []]
    parts = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            parts.append(", ".join(_text(row.get(field)) for field in fields))
    text = summary(rows, spec) + (": " + "; ".join(parts) if parts else "")
    return text if len(text) <= limit else text[:limit - len(marker)] + marker


def _text(value: Any) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    return "" if value is None else str(_normal(value))


def check_publication(descriptor: dict) -> None:
    """Shape of a published ROWS descriptor (mounted relay), or ``ValueError``.

    The pre-check then validates changes against these field specs, so a
    malformed one must be refused here rather than fail mid-validation.
    """
    cd = _cd()
    fields = descriptor.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 32:
        raise ValueError("A row list needs its fields.")
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("id"), str) or field.get("kind") not in FIELD_KINDS:
            raise ValueError("A row field is invalid.")
        if "options" in field and not isinstance(field["options"], (list, tuple)):
            raise ValueError("Row field options must be a list.")
        if "options_truncated" in field and not isinstance(field["options_truncated"], bool):
            raise ValueError("The options preview flag must be boolean.")
        for bound in ("min", "max"):
            if field.get(bound) is not None:
                cd.coerce_control_value("INT" if field["kind"] == "INT" else "FLOAT", field[bound])
    for bound in ("min", "max"):
        if isinstance(descriptor.get(bound), bool) or not isinstance(descriptor.get(bound), int):
            raise ValueError("A row list needs its row limits.")
    if not isinstance(descriptor.get("value"), list):
        raise ValueError("A row list's value must be a list.")


def value_shape_ok(value: Any) -> bool:
    """A stored row list (a preset member): at most 20 flat rows of scalars."""
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        return False
    for row in value:
        if not isinstance(row, dict) or not row or len(row) > 32:
            return False
        for key, item in row.items():
            if not isinstance(key, str) or not 0 < len(key) <= 64:
                return False
            if not (item is None or isinstance(item, (str, bool, int, float))):
                return False
            if isinstance(item, float) and not math.isfinite(item):
                return False
            if isinstance(item, str) and len(item) > 4096:
                return False
    return True
