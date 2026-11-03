"""The Controls in effect for one run, frozen into a generated asset's provenance.

``controls_snapshot(prompt, consumer_keys)`` reads the *executed* API prompt a
save node received and returns one tracked-metadata section
(``display_type: "controls"``), or ``None`` when the run had no Controls. It is
appended to ``editor_export.tracked_metadata`` by ``io_nodes._compact_editor_export``,
so Video, PNG sequence, every Take copy and Save Bridge registrations carry it.

Presentation comes from the Relay's exported manifest. A Sonder App run adds
what its sidecar changes (contract amendment A1) as a small delta on the
editor node's ``_meta.sonder_controls``, stamped by ``sdk_runner``: label,
group, order, seed-mode and Advanced overrides, app-only bindings, and hidden
ids to drop. Values never come from either: each binding's value is its first target's literal in the prompt,
which already carries every stored value and the seed this run used.

Only bindings ``describe_bindings`` reports ``ok`` are recorded, and only those
with a target upstream of the consumer, so a branch that cannot reach the
saved output is left out. A statically connected branch that a switch turned
off at run time is still listed on a canvas run; the gallery says so. A Sonder
App run's prompt has no such branch (``run_preparation`` cut it), so its
Controls there are left out. A LoRA stack that run packed or bypassed is
recorded with its authored rows, from the delta. Notes are never recorded.

The section keeps the collector's searchable shape (``label`` and ``fields``,
keyed by Control label) so ``tracked:``/``field:`` search, pins and Copy work,
and adds ``controls`` (per-binding detail and optional native-stack display
annotation, without the value, which lives only
in ``fields``) and ``supplied`` (generic collector fields a binding set). It
has no ``raw_widget_text``: a Take stores the section three times inline, and
a third copy of the values would only repeat ``fields``. Native LoRA values
are complete normalized row arrays; other ROWS values remain text. It is capped at
``MAX_SECTION_BYTES``.

Pure apart from the injectable ``describe``/``class_lookup``; it never raises
on a hostile prompt (callers still guard it, because a save must not fail on
provenance).
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from . import control_descriptors, control_manifest, control_rows, prompt_graph
from .lora_metadata import NATIVE_LORA_STACKS, NATIVE_LORA_DISPLAY, captured_native_lora_rows

SECTION_LABEL = "Controls at run"
DISPLAY_TYPE = "controls"
SDK_META_KEY = "sonder_controls"
SDK_DELTA_VERSION = 1

MAX_SECTION_BYTES = 16 * 1024
MAX_BINDINGS = 128
MAX_TARGETS = 8
# App-only bindings read from an untrusted ``_meta`` delta: each is described
# (``INPUT_TYPES`` per target) inside the save, so the delta is kept small.
MAX_LOCAL_BINDINGS = 64
TEXT_VALUE_LIMIT = 512
TRUNCATED_MARKER = "… [truncated, full value in the source prompt]"


# ── Sonder App delta (written by sdk_runner, read here) ──────────────────────

def prepared_rows(view: dict, packed_rows: dict) -> dict:
    """``{binding_id: {"rows", "bypassed"}}`` for each native stack binding whose
    stack the run's preparation packed or bypassed (``run_preparation``)."""
    found = {}
    for descriptor in view.get("bindings", []) if isinstance(view, dict) and packed_rows else []:
        if descriptor.get("kind") != control_rows.ROWS_KIND or descriptor.get("status") != control_descriptors.STATUS_OK:
            continue
        targets = descriptor.get("targets") or []
        entry = packed_rows.get(str(targets[0].get("path"))) if targets and targets[0].get("input") == "loras" else None
        if isinstance(entry, dict) and isinstance(descriptor.get("binding_id"), str):
            found[descriptor["binding_id"]] = {"rows": entry["rows"], "bypassed": entry["bypassed"] is True}
    return found


def sdk_overlay_delta(view: dict, prompt: dict, rows: dict | None = None) -> dict | None:
    """What a Sonder App view changes about the workflow's own Controls.

    ``view`` is the effective view Generate submits with (``apply_stored_values``)
    and ``prompt`` the compiled prompt before preparation cut it.
    Returns ``None`` when nothing differs, so a plain template run stamps
    nothing. Values are not part of it: they are already in the prompt, except
    ``rows`` (``prepared_rows``): the authored rows of a stack the run packed
    or bypassed, which the executed prompt no longer holds. Seed modes are: the
    app's own mode is not in the prompt, only the export's.
    """
    manifest = control_manifest.find_relay(prompt).get("manifest") or control_manifest.empty_manifest()
    authored = manifest.get("bindings") or {}
    labels, groups, orders, seed_modes, advanced, local, hidden = {}, {}, {}, {}, {}, {}, []
    for descriptor in view.get("bindings", []) if isinstance(view, dict) else []:
        binding_id = descriptor.get("binding_id")
        if not isinstance(binding_id, str):
            continue
        if descriptor.get("hidden"):
            hidden.append(binding_id)
            continue
        if descriptor.get("status") != control_descriptors.STATUS_OK:
            continue
        mode = descriptor.get("seed_mode") if descriptor.get("kind") == "SEED" else None
        if descriptor.get("origin") == "local":
            if mode in control_manifest.SEED_MODES:
                seed_modes[binding_id] = mode
            local[binding_id] = {
                "targets": [{"path": t.get("path"), "input": t.get("input"), "class_type": t.get("class_type")}
                            for t in descriptor.get("targets") or []],
                "section": descriptor.get("section", "workflow"),
                "label": descriptor.get("label", ""),
                "group": descriptor.get("group", ""),
                "order": descriptor.get("order", 0),
            }
            if descriptor.get("advanced") is True:
                local[binding_id]["advanced"] = True
            continue
        entry = authored.get(binding_id)
        if not isinstance(entry, dict):
            continue
        base_label = entry.get("label") or control_descriptors._default_label(prompt, entry.get("targets") or [])
        if descriptor.get("label") and descriptor["label"] != base_label:
            labels[binding_id] = descriptor["label"]
        if descriptor.get("group", "") != entry.get("group", ""):
            groups[binding_id] = descriptor.get("group", "")
        if descriptor.get("order", 0) != entry.get("order", 0):
            orders[binding_id] = descriptor.get("order", 0)
        if (descriptor.get("advanced") is True) != (entry.get("advanced") is True):
            advanced[binding_id] = descriptor.get("advanced") is True
        if mode in control_manifest.SEED_MODES and mode != entry.get("seed_mode"):
            seed_modes[binding_id] = mode
    rows = {key: value for key, value in (rows or {}).items() if key not in hidden}
    if not (labels or groups or orders or seed_modes or advanced or local or hidden or rows):
        return None
    delta: dict[str, Any] = {"v": SDK_DELTA_VERSION}
    for name, value in (("labels", labels), ("groups", groups), ("orders", orders), ("seed_modes", seed_modes),
                        ("advanced", advanced), ("local", local), ("hidden", hidden), ("rows", rows)):
        if value:
            delta[name] = value
    return delta


def _clean_id(value: Any) -> str | None:
    if (isinstance(value, str) and control_manifest.ID_RE.match(value)
            and value not in control_manifest.FORBIDDEN_KEYS):
        return value
    return None


def _clean_texts(raw: Any, limit: int) -> dict:
    texts = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        if _clean_id(key) and isinstance(value, str):
            texts[key] = value.strip()[:limit]
        if len(texts) >= control_manifest.MAX_BINDINGS:
            break
    return texts


def _clean_orders(raw: Any) -> dict:
    orders = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        if _clean_id(key) and isinstance(value, int) and not isinstance(value, bool):
            orders[key] = max(-control_manifest.ORDER_LIMIT, min(control_manifest.ORDER_LIMIT, value))
        if len(orders) >= control_manifest.MAX_BINDINGS:
            break
    return orders


def _clean_modes(raw: Any) -> dict:
    modes = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        if _clean_id(key) and value in control_manifest.SEED_MODES:
            modes[key] = value
        if len(modes) >= control_manifest.MAX_BINDINGS:
            break
    return modes


def _clean_flags(raw: Any) -> dict:
    flags = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        if _clean_id(key) and isinstance(value, bool):
            flags[key] = value
        if len(flags) >= control_manifest.MAX_BINDINGS:
            break
    return flags


def _small_local(raw: Any) -> dict | None:
    """At most ``MAX_LOCAL_BINDINGS`` app-only bindings of at most ``MAX_TARGETS``
    targets each; ``describe_bindings`` checks the rest."""
    if not isinstance(raw, dict):
        return None
    local = {}
    for key, entry in raw.items():
        targets = entry.get("targets") if isinstance(entry, dict) else None
        if _clean_id(key) and isinstance(targets, list) and 0 < len(targets) <= MAX_TARGETS:
            local[key] = entry
        if len(local) >= MAX_LOCAL_BINDINGS:
            break
    return local or None


def _clean_rows(raw: Any) -> dict:
    """Authored rows of a stack preparation packed or bypassed: at most
    ``MAX_BINDINGS`` row lists of the shape a stored row list has."""
    rows = {}
    for key, entry in (raw.items() if isinstance(raw, dict) else []):
        value = entry.get("rows") if isinstance(entry, dict) else None
        if _clean_id(key) and control_rows.value_shape_ok(value) and value:
            rows[key] = {"rows": value, "bypassed": entry.get("bypassed") is True}
        if len(rows) >= control_manifest.MAX_BINDINGS:
            break
    return rows


def read_sdk_delta(prompt: dict) -> dict:
    """The hygiene-checked delta of the first editor that carries one, else empty.

    The prompt is untrusted: any client may submit ``_meta``. A bad delta only
    changes how this asset's own provenance is labelled.
    """
    for key in prompt_graph.editor_keys(prompt):
        meta = prompt[key].get("_meta")
        raw = meta.get(SDK_META_KEY) if isinstance(meta, dict) else None
        if not isinstance(raw, dict) or raw.get("v") != SDK_DELTA_VERSION:
            continue
        hidden = raw.get("hidden") if isinstance(raw.get("hidden"), list) else []
        return {
            "labels": _clean_texts(raw.get("labels"), control_manifest.LABEL_MAX),
            "groups": _clean_texts(raw.get("groups"), control_manifest.LABEL_MAX),
            "orders": _clean_orders(raw.get("orders")),
            "seed_modes": _clean_modes(raw.get("seed_modes")),
            "advanced": _clean_flags(raw.get("advanced")),
            "local": _small_local(raw.get("local")),
            "hidden": {item for item in hidden[:control_manifest.MAX_BINDINGS] if _clean_id(item)},
            "rows": _clean_rows(raw.get("rows")),
        }
    return {"labels": {}, "groups": {}, "orders": {}, "seed_modes": {}, "advanced": {}, "local": None, "hidden": set(),
            "rows": {}}


# ── Snapshot ─────────────────────────────────────────────────────────────────

def _native_stack(descriptor: dict, prompt: dict) -> bool:
    targets = descriptor.get("targets") or []
    first = targets[0] if targets else {}
    node = prompt.get(str(first.get("path")))
    return (descriptor.get("kind") == control_rows.ROWS_KIND and first.get("input") == "loras"
            and isinstance(node, dict) and node.get("class_type") in NATIVE_LORA_STACKS)


def _authored_rows(descriptor: dict, entry: Any) -> Any:
    """The delta's authored rows of a stack preparation packed or bypassed, when
    they agree with what ran (a bypassed stack ran none, unchanged; a packed one
    ran exactly the rows that are not off). Anything else is ignored."""
    if not isinstance(entry, dict):
        return None
    authored = entry["rows"]
    executed = descriptor.get("value")
    expected = authored if entry["bypassed"] else [row for row in authored if not control_rows.row_is_noop(row)]
    if entry["bypassed"] and not all(control_rows.row_is_noop(row) for row in authored):
        return None
    return authored if control_rows.canonical(executed) == control_rows.canonical(expected) else None


def _recorded_value(descriptor: dict, prompt: dict, prepared: Any = None) -> Any:
    """The first target's literal, as the run received it; INT/SEED as decimal text.

    ``prepared`` is the delta's entry for a stack the run's preparation packed
    or bypassed: its authored rows are recorded, off rows included.
    """
    targets = descriptor.get("targets") or []
    first = targets[0] if targets else {}
    kind = descriptor.get("kind")
    if kind == control_rows.ROWS_KIND:
        if _native_stack(descriptor, prompt):
            authored = _authored_rows(descriptor, prepared)
            rows = captured_native_lora_rows(authored if authored is not None else descriptor.get("value"))
            if rows is not None:
                return rows
            return control_rows.detail(descriptor.get("value"), descriptor, TEXT_VALUE_LIMIT, marker=TRUNCATED_MARKER)
        return control_rows.detail(descriptor.get("value"), descriptor, TEXT_VALUE_LIMIT)
    value = prompt_graph.node_inputs(prompt, first.get("path")).get(first.get("input"))
    if kind in ("INT", "SEED") and isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, int):
            return str(value)
        return str(int(value)) if math.isfinite(value) and value.is_integer() else str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str) and len(value) > TEXT_VALUE_LIMIT:
        return value[:TEXT_VALUE_LIMIT] + TRUNCATED_MARKER
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None  # describe never reports a container as ``ok``


def _sort_key(entry: dict, positions: dict) -> tuple:
    group = entry["group"]
    return (entry["section"] != "workflow", group != "", positions.get(group, len(positions)),
            entry["order"], entry["label"].lower(), entry["binding_id"])


def _field_name(label: str, binding_id: str, used: set) -> str:
    name = label or binding_id
    if name in used:
        name = f"{name} [{binding_id}]"
    used.add(name)
    return name


def _supplied(tracked: Any, controls: list[dict]) -> list[dict]:
    """Collector fields that a recorded binding set.

    Generic sections (no ``display_type``) match field by field. A structured
    section matches only through its ``source_group``: a native LoRA stack's
    rows (``power_loras``) are the row list a ROWS Control set. Other
    structured rows (rgthree's ``power_loras``, ``subgraph`` interface fields)
    do not keep the input they came from. ``tracked_metadata_renderers.js``
    ``controlsSuppliedLookup`` mirrors it.
    """
    by_target = {}
    for control in controls:
        for target in control["targets"]:
            by_target.setdefault((target["path"], target["input"]), control["binding_id"])
    supplied = []
    for index, section in enumerate(tracked if isinstance(tracked, list) else []):
        if not isinstance(section, dict):
            continue
        fields = section.get("fields")
        source = section.get("source_node_id")
        if not isinstance(fields, dict) or not isinstance(source, str):
            continue
        if section.get("display_type"):
            group = section.get("source_group")
            binding_id = by_target.get((source, group)) if isinstance(group, str) else None
            if binding_id is not None and "power_loras" in fields:
                supplied.append({"section_index": index, "field": "power_loras", "binding_id": binding_id})
            continue
        for field in fields:
            binding_id = by_target.get((source, field))
            if binding_id is not None:
                supplied.append({"section_index": index, "field": field, "binding_id": binding_id})
    return supplied


def _encoded_size(section: dict) -> int:
    return len(json.dumps(section, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _record(entry: dict, field: str) -> dict:
    """One binding's detail. Defaults are omitted and the value lives only in
    ``fields``: a Take stores this section three times inline."""
    record = {"binding_id": entry["binding_id"], "field": field, "kind": entry["kind"], "targets": entry["targets"]}
    if entry["label"] != field:
        record["label"] = entry["label"]
    if entry["group"]:
        record["group"] = entry["group"]
    if entry["section"] != "workflow":
        record["section"] = entry["section"]
    if entry["origin"] != "authored":
        record["origin"] = entry["origin"]
    if entry["advanced"]:
        record["advanced"] = True
    for name in ("seed_mode", "targets_truncated", "display_type", "rows_omitted"):
        if name in entry:
            record[name] = entry[name]
    return record


def _assemble(entries: list[dict], tracked: Any, truncated: int) -> dict:
    used: set = set()
    fields, controls = {}, []
    for entry in entries:
        field = _field_name(entry["label"], entry["binding_id"], used)
        fields[field] = entry["value"]
        controls.append(_record(entry, field))
    section = {
        "label": SECTION_LABEL,
        "display_type": DISPLAY_TYPE,
        "fields": fields,
        "controls": controls,
    }
    supplied = _supplied(tracked, controls)
    if supplied:
        section["supplied"] = supplied
    if truncated:
        section["truncated"] = truncated
    return section


def controls_snapshot(
    prompt: Any,
    consumer_keys: Any,
    *,
    tracked: Any = None,
    describe: Callable[..., dict] | None = None,
    class_lookup: Callable[[str], Any] | None = None,
) -> dict | None:
    """The Controls section for an asset saved by ``consumer_keys``, or ``None``.

    ``consumer_keys`` are the prompt keys whose upstream decides relevance: the
    Save Video node, or a Save Bridge plus the nodes consuming its outputs.
    ``tracked`` is the collector chain the section joins, for ``supplied``.
    """
    if not isinstance(prompt, dict):
        return None
    delta = read_sdk_delta(prompt)
    relay = control_manifest.find_relay(prompt)
    if relay["status"] == "error" or (relay["status"] == "none" and not delta["local"]):
        return None
    reachable = prompt_graph.upstream_set(prompt, consumer_keys)
    if not reachable:
        return None
    describe = describe or control_descriptors.describe_bindings
    kwargs = {"local": delta["local"]}
    if class_lookup is not None:
        kwargs["class_lookup"] = class_lookup
    view = describe(prompt, **kwargs)
    if (view.get("relay") or {}).get("status") == "error":
        return None
    entries = []
    for descriptor in view.get("bindings", []):
        binding_id = descriptor.get("binding_id")
        if descriptor.get("status") != control_descriptors.STATUS_OK or binding_id in delta["hidden"]:
            continue
        targets = [{"path": t.get("path"), "input": t.get("input")} for t in descriptor.get("targets") or []]
        prepared = delta["rows"].get(binding_id)
        # A stack the run bypassed is kept with nothing consuming it: listed
        # from the delta, which names only stacks whose consumers ran.
        if not any(t["path"] in reachable for t in targets) and not (prepared and prepared["bypassed"]):
            continue
        origin = descriptor.get("origin", "authored")
        label = descriptor.get("label") or control_descriptors.DEFAULT_LABEL
        group = descriptor.get("group", "")
        order = descriptor.get("order", 0)
        advanced = descriptor.get("advanced") is True
        if origin != "local":
            label = delta["labels"].get(binding_id) or label
            group = control_manifest.clean_group(delta["groups"].get(binding_id, group))
            order = delta["orders"].get(binding_id, order)
            advanced = delta["advanced"].get(binding_id, advanced)
        entry = {
            "binding_id": binding_id,
            "origin": origin,
            "label": label,
            "group": group,
            "section": descriptor.get("section", "workflow"),
            "order": order,
            "advanced": advanced,
            "kind": descriptor.get("kind"),
            "value": _recorded_value(descriptor, prompt, prepared),
            "targets": targets[:MAX_TARGETS],
        }
        if _native_stack(descriptor, prompt) and isinstance(entry["value"], list):
            entry["display_type"] = NATIVE_LORA_DISPLAY
        if len(targets) > MAX_TARGETS:
            entry["targets_truncated"] = len(targets) - MAX_TARGETS
        mode = delta["seed_modes"].get(binding_id, descriptor.get("seed_mode"))
        if descriptor.get("kind") == "SEED" and mode in control_manifest.SEED_MODES:
            entry["seed_mode"] = mode
        entries.append(entry)
    if not entries:
        return None
    # The panel's group order (one authority), over the groups this run used.
    # The order itself is not recorded: it is presentation, not the run.
    order = control_descriptors.group_order({"groups": view.get("groups"), "bindings": entries, "notes": []})
    positions = {name: index for index, name in enumerate(order)}
    entries.sort(key=lambda entry: _sort_key(entry, positions))
    truncated = max(0, len(entries) - MAX_BINDINGS)
    kept = entries[:MAX_BINDINGS]
    # Shrink an individually oversized stack before it can evict later Controls.
    # Reserve the omission count if this becomes the only retained Control.
    for entry in kept:
        if (entry.get("display_type") == NATIVE_LORA_DISPLAY
                and _encoded_size(_assemble([entry], tracked, max(0, len(entries) - 1))) > MAX_SECTION_BYTES):
            rows = entry["value"]
            entry["value"] = f"{len(rows)} LoRA{'s' if len(rows) != 1 else ''} · {sum(row['enabled'] for row in rows)} on"
            del entry["display_type"]
            entry["rows_omitted"] = True
    section = _assemble(kept, tracked, truncated)
    while _encoded_size(section) > MAX_SECTION_BYTES and len(kept) > 1:
        # Drop from the end (Editor section and late groups first) until it fits.
        cut = max(1, len(kept) // 8)
        kept = kept[:-cut]
        truncated += cut
        section = _assemble(kept, tracked, truncated)
    return section if _encoded_size(section) <= MAX_SECTION_BYTES else None
