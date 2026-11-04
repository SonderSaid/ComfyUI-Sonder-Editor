"""The Sonder App starters' subgraph names (0.8 hardening, Phase 6B).

A starter is an API export, so its Relay manifest must carry the
``subgraph_names`` the canvas export would add (``exportSubgraphNames`` in
``web/js/control_bindings.js``). The starters hold deliberate App-only edits
that a canvas re-export would undo, so the names are inserted by this script
from each starter's matching canvas example, and ``tests/test_launcher_starters.py``
checks them with the same walker.

    python tools/starter_subgraph_names.py          # rewrite the starters
    python tools/starter_subgraph_names.py --check  # exit 1 when one differs
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import control_manifest as cm  # noqa: E402

# Each starter and the canvas example it was exported from.
STARTERS = {
    "sonder_ltx_2_5_edit_vfx": "sonder_ltx_2_5_edit_vfx",
    "sonder_ltx_2_5_refine_upscale": "sonder_ltx_2_5_refine_upscale",
    "sonder_minimax_h3_references_swaps": "sonder_minimax_h3_references_swaps",
}


def subgraph_names(workflow: dict) -> dict:
    """``{host execution path: name}`` for every subgraph instance of a
    UI-format workflow, at every depth and whatever its mode: the instance's
    title, else its definition's name. The export walker's rule."""
    definitions = {d.get("id"): d for d in (workflow.get("definitions") or {}).get("subgraphs") or []}
    names: dict = {}

    def visit(nodes, prefix, depth):
        if depth >= cm.MAX_PATH_SEGMENTS:
            return
        for node in nodes or []:
            definition = definitions.get(node.get("type"))
            if definition is None:
                continue
            path = f"{prefix}:{node['id']}" if prefix else str(node["id"])
            name = cm.clean_subgraph_name(node.get("title")) or cm.clean_subgraph_name(definition.get("name"))
            if name and cm.clean_path(path) and len(names) < cm.MAX_SUBGRAPH_NAMES:
                names[path] = name
            visit(definition.get("nodes"), path, depth + 1)

    visit(workflow.get("nodes"), "", 0)
    return names


def relay_manifest(starter: dict) -> tuple[str, dict]:
    key = next(k for k, n in starter.items() if n.get("class_type") == cm.RELAY_CLASS_TYPE)
    return key, json.loads(starter[key]["inputs"]["manifest"])


def updated_text(slug: str) -> tuple[str, str]:
    """The starter file's text now, and with its manifest's names set."""
    path = ROOT / "sdk_starters" / f"{slug}.json"
    text = path.read_bytes().decode("utf-8")
    starter = json.loads(text)
    key, manifest = relay_manifest(starter)
    example = json.loads((ROOT / "example_workflows" / f"{STARTERS[slug]}.json").read_text(encoding="utf-8"))
    names = subgraph_names(example)
    saved = starter[key]["inputs"]["manifest"]
    updated = dict(manifest)
    updated.pop("subgraph_names", None)
    if names:
        updated["subgraph_names"] = names
    # Rewritten in the style it was saved in (a canvas export's compact JSON,
    # or Python's spaced form from an earlier script), so nothing else moves.
    styles = [{"ensure_ascii": ascii_only, "separators": separators}
              for separators in ((",", ":"), (", ", ": ")) for ascii_only in (False, True)]
    style = next((s for s in styles if json.dumps(manifest, **s) == saved), None)
    if style is None:
        raise SystemExit(f"{slug}: the manifest text is in an unknown JSON style; refusing to rewrite it.")
    rewritten = json.dumps(updated, **style)
    found = [o for o in (json.dumps(saved, ensure_ascii=False), json.dumps(saved)) if text.count(o) == 1]
    if not found:
        raise SystemExit(f"{slug}: the manifest text was not found once in the file.")
    old = found[0]
    # The file escapes non-ASCII text only when its saved form did.
    new = json.dumps(rewritten, ensure_ascii=old != json.dumps(saved, ensure_ascii=False))
    return text, text.replace(old, new)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for slug in STARTERS:
        text, updated = updated_text(slug)
        if text == updated:
            continue
        stale.append(slug)
        if not check:
            (ROOT / "sdk_starters" / f"{slug}.json").write_bytes(updated.encode("utf-8"))
    for slug in stale:
        print(("differs: " if check else "updated: ") + slug)
    return 1 if check and stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
