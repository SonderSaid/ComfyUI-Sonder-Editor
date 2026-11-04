"""Regenerate ``controls_snapshot_golden.json`` from a git revision's own
``server/controls_snapshot.py`` (Hardening Phase 10: the section must stay byte
identical for prompts with no new behavior).

    python tests/fixtures/characterize_controls_snapshot_head.py [REV]

REV defaults to ``dbe1b05``, the commit before Phase 10. The revision's module
is loaded beside the current ``server`` package, so only that one file is old.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_controls_snapshot import CLASSES, _binding, _node, _prompt, _relay  # noqa: E402

GOLDEN = Path(__file__).with_name("controls_snapshot_golden.json")


def cases():
    """Prompts whose sections Phase 10 must not change (no capped row list,
    no lone surrogate). Long text is included: its preview is unchanged."""
    out = {"default": _prompt()}
    long_text = _prompt()
    long_text["2"]["inputs"]["text"] = ("a quiet harbor at dawn, " * 40) + "\r\nlast line"
    out["long_text"] = long_text
    cjk = _prompt()
    cjk["2"]["inputs"]["text"] = "夜明けの川に光が揺れて静かな窓の向こうで風が歌う" * 30
    out["long_cjk"] = cjk
    exact = _prompt()
    exact["2"]["inputs"]["text"] = "x" * 512
    out["exactly_512"] = exact
    grouped = _prompt(bindings={
        "text": _binding("2", "text", label="Prompt", group="Words", order=1),
        "seed": _binding("3", "seed", label="Seed", seed_mode="randomize"),
    })
    grouped["2"]["inputs"]["text"] = "y" * 900
    out["grouped_long"] = grouped
    out["duplicate_labels"] = _prompt(bindings={
        "a": _binding("3", "steps", label="Steps"),
        "b": _binding("3", "cfg", label="Steps"),
    })
    # The 16 KiB cap: whole Controls pruned from the end, with a count.
    pruned = {"1": _node("SonderEditor", {"project": "P"}), "9": _node("SonderSaveVideo", {"project": ["1", 0]})}
    bindings = {}
    for index in range(200):
        key = str(100 + index)
        pruned[key] = _node("CLIPTextEncode", {"text": "t" * 700})
        pruned["9"]["inputs"][f"in{index}"] = [key, 0]
        bindings[f"b{index}"] = _binding(key, "text", label=f"Label {index:03d} " + "L" * 100)
    pruned["20"] = _relay(bindings)
    out["pruned"] = pruned
    return out


# Cases recorded with a collector chain (``supplied``).
TRACKED = {"supplied": [
    {"label": "Sampler", "source_node_id": "3", "display_type": None, "fields": {"steps": 20, "cfg": 7.5, "denoise": 1.0}},
    {"label": "LoRAs", "source_node_id": "3", "display_type": "power_loras", "fields": {"steps": 1}},
]}


def load_module(rev):
    source = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:server/controls_snapshot.py"],
                            check=True, capture_output=True, text=True, encoding="utf-8").stdout
    spec = importlib.util.spec_from_loader("server._controls_snapshot_golden", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "server"
    exec(compile(source, f"{rev}:server/controls_snapshot.py", "exec"), module.__dict__)
    return module


def main(rev="dbe1b05"):
    module = load_module(rev)
    golden = {"rev": rev, "cases": {}}
    all_cases = cases()
    all_cases["supplied"] = _prompt()
    for name, prompt in all_cases.items():
        tracked = TRACKED.get(name)
        section = module.controls_snapshot(prompt, ["9"], class_lookup=CLASSES.get, tracked=tracked)
        golden["cases"][name] = {"prompt": prompt, "tracked": tracked, "section_json": json.dumps(
            section, ensure_ascii=False, separators=(",", ":"))}
    GOLDEN.write_text(json.dumps(golden, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN} from {rev}: {len(golden['cases'])} cases")


if __name__ == "__main__":
    main(*sys.argv[1:])
