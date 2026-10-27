"""Controls Modes derivation: server/control_preset_categories.derive_modes is the
Python mirror of web/js/controls_modes_model.js::deriveModes (run feedback's
receipt derives Modes server-side; the panel derives them in the browser)."""

import json

from server import control_preset_categories as pc
from test_view_timing_js import module, node_run


def P(pid, state="differs"):
    return {"id": pid, "name": pid.upper(), "description": "", "order": 0, "state": state, "origin": "workflow",
            "members": [], "execution": [], "unavailable": [], "reason": ""}


def C(cid, choices, name=None):
    return {"id": cid, "name": cid.title() if name is None else name, "description": "", "order": 0,
            "origin": "workflow", "choices": choices, "warnings": []}


CASES = {
    "selected": {"presets": [P("a", "match"), P("b")], "preset_categories": [C("task", ["a", "b"])]},
    "ambiguous": {"presets": [P("a", "match"), P("b", "match"), P("c")], "preset_categories": [C("task", ["a", "b", "c"])]},
    "custom": {"presets": [P("a"), P("b", "refused")], "preset_categories": [C("task", ["a", "b"])]},
    "unavailable": {"presets": [P("a", "unavailable"), P("b", "refused")], "preset_categories": [C("task", ["a", "b"])]},
    "first_category_claims": {"presets": [P("a", "match"), P("b"), P("c", "match")],
                              "preset_categories": [C("one", ["a", "b"]), C("two", ["b", "c"]), C("three", ["a"])]},
    "empty_and_missing": {"presets": [P("a")], "preset_categories": [C("gone", ["x", "y"]), C("task", ["a", "x"], name="")]},
    "duplicate_within": {"presets": [P("a", "match"), P("b")], "preset_categories": [C("task", ["a", "a", "b"])]},
    "no_view_parts": {},
    "uncategorized": {"presets": [P("a"), P("b", "match"), P("z")], "preset_categories": [C("task", ["a"])]},
}


def shape(result):
    return {"rows": [{"id": row["id"], "name": row["name"], "status": row["status"],
                      "choices": [c["id"] for c in row["choices"]],
                      "selected": row["selected"]["id"] if row["selected"] else None,
                      "matches": [m["id"] for m in row["matches"]]} for row in result["rows"]],
            "uncategorized": [row["id"] for row in result["uncategorized"]]}


def test_derive_modes_matches_the_browser_model(tmp_path):
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(CASES), encoding="utf-8")
    out = node_run(f"""
const M = await import({module("controls_modes_model.js")});
const {{ readFileSync }} = await import('node:fs');
const cases = JSON.parse(readFileSync({json.dumps(str(data))}, 'utf-8'));
const shape = r => ({{rows: r.rows.map(row => ({{id: row.id, name: row.name, status: row.status,
    choices: row.choices.map(c => c.id), selected: row.selected ? row.selected.id : null,
    matches: row.matches.map(m => m.id)}})), uncategorized: r.uncategorized.map(p => p.id)}});
console.log('RESULT=' + JSON.stringify(Object.fromEntries(Object.entries(cases).map(([k, v]) => [k, shape(M.deriveModes(v))]))));
""")
    assert out == {name: shape(pc.derive_modes(view)) for name, view in CASES.items()}
    assert out["ambiguous"]["rows"][0]["status"] == "ambiguous" and out["unavailable"]["rows"][0]["status"] == "unavailable"


def test_modes_receipt_names_the_choice_or_the_ambiguity():
    assert pc.modes_receipt(CASES["selected"]) == [
        {"id": "task", "name": "Task", "status": "selected", "choice": "A", "matches": ["A"]}]
    assert pc.modes_receipt(CASES["ambiguous"])[0] == {"id": "task", "name": "Task", "status": "ambiguous",
                                                      "choice": "", "matches": ["A", "B"]}
    assert pc.modes_receipt({}) == []
