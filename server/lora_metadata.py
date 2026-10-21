"""Pure native LoRA provenance shared by collectors and Controls snapshots."""

from __future__ import annotations

import math
import re
from typing import Any


NATIVE_LORA_STACKS = frozenset({"LoadLoraModel", "LoadLoraTextEncoder"})
NATIVE_LORA_DISPLAY = "native_lora_stack"
_NATIVE_LORA_KEY = re.compile(r"^loras\.(0|[1-9][0-9]*)\.(lora_name|strength|enabled)$")


def native_lora_rows(values: list[dict]) -> list[dict]:
    """Power LoRA-shaped rows, retaining the collector's permissive semantics."""
    rows = []
    for values_row in values:
        name = values_row.get("lora_name")
        if name is None or str(name).strip() in {"", "None", "none"}:
            continue
        row = {"slot": len(rows) + 1, "name": str(name), "enabled": values_row.get("enabled") is not False}
        if "strength" in values_row:
            row["strength"] = values_row["strength"]
        if not row["enabled"] or values_row.get("strength") == 0:
            row["skipped"] = True
        rows.append(row)
    return rows


def native_lora_fields(inputs: dict) -> dict | None:
    """Collector adapter for the executed prompt's flat dynamic-group inputs."""
    grouped: dict[int, dict[str, Any]] = {}
    for key, value in inputs.items():
        match = _NATIVE_LORA_KEY.match(str(key))
        if match:
            grouped.setdefault(int(match.group(1)), {})[match.group(2)] = value
    rows = native_lora_rows([grouped[index] for index in sorted(grouped)])
    if not rows:
        return None
    return {"power_loras": rows, "enabled_lora_count": sum(row["enabled"] for row in rows),
            "total_lora_count": len(rows)}


def captured_native_lora_rows(value: Any) -> list[dict] | None:
    """Faithful literals, independent of today's widget/options validation."""
    if not isinstance(value, list) or len(value) > 20:
        return None
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"lora_name", "strength", "enabled"}
                or not isinstance(row.get("lora_name"), str)):
            return None
        strength = row.get("strength")
        try:
            finite = math.isfinite(strength) if isinstance(strength, (int, float)) else False
        except OverflowError:
            finite = False
        if (isinstance(strength, bool) or not isinstance(strength, (int, float))
                or not finite or not isinstance(row.get("enabled"), bool)):
            return None
    return native_lora_rows(value)
