"""Hardening Phase 10: complete Controls provenance.

I2: the full text of long STRING Controls, kept on the asset beside the section
(never in the section, its clip copies, search or the file's metadata), and the
UTF-8-safe writers that keep a lone surrogate from failing a save.
I4: Modes at run, read from the run's own record.
"""

import importlib
import json
import threading
import time
from pathlib import Path

import pytest

from server import control_rows
from server import controls_snapshot as cs
from server import json_text
from server import project_storage as ps
from server import run_feedback, sdk_runs

from test_controls_snapshot import CLASSES, _binding, _prompt

GOLDEN = Path(__file__).with_name("fixtures") / "controls_snapshot_golden.json"


def capture(prompt, consumers=("9",)):
    return cs.controls_capture(prompt, list(consumers), class_lookup=CLASSES.get)


def text_prompt(value, **bindings):
    prompt = _prompt(bindings=bindings or None)
    prompt["2"]["inputs"]["text"] = value
    return prompt


def field_of(section, binding_id):
    return next(c["field"] for c in section["controls"] if c["binding_id"] == binding_id)


CORPUS = {
    "crlf_lyrics": ("[Verse 1]\r\nThree in the morning, rain on the pavement\r\n" * 60),
    "korean": ("차가운 밤이 날 삼켜\n어둠 속에 길을 잃어\n" * 60),
    "combining": ("नमस्ते क्षत्रिय मَرْحَبًا é ä " * 40),
    "astral_zwj": ("👩‍👩‍👧‍👦 🏳️‍🌈 𝄞 🇲🇽 " * 80),
    "controls": ("cue\t\x01\x07\x1b\f" * 120),
}


# ── Golden: the section is byte-identical to the commit before Phase 10 ──────

def test_the_section_matches_the_pre_phase_golden_byte_for_byte():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert golden["cases"]
    for name, case in golden["cases"].items():
        section, _full = cs.controls_capture(case["prompt"], ["9"], class_lookup=CLASSES.get, tracked=case.get("tracked"))
        assert json.dumps(section, ensure_ascii=False, separators=(",", ":")) == case["section_json"], name
        assert cs.controls_snapshot(case["prompt"], ["9"], class_lookup=CLASSES.get, tracked=case.get("tracked")) == section


# ── Full text ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(CORPUS))
def test_full_text_is_the_exact_executed_string(name):
    text = CORPUS[name]
    assert len(text) > cs.TEXT_VALUE_LIMIT
    section, full = capture(text_prompt(text))
    preview = section["fields"][field_of(section, "text")]
    assert preview == text[:cs.TEXT_VALUE_LIMIT] + cs.TRUNCATED_MARKER
    assert full == {"v": 1, "values": {"text": text}}
    assert cs.controls_full_text({cs.FULL_TEXT_KEY: full}, "text") == text
    # The record never enters the section.
    assert "controls_text" not in json.dumps(section)


def test_short_and_exactly_capped_values_keep_no_full_text():
    assert capture(text_prompt("a red fox"))[1] is None
    assert capture(text_prompt("x" * cs.TEXT_VALUE_LIMIT))[1] is None
    assert capture(text_prompt("x" * (cs.TEXT_VALUE_LIMIT + 1)))[1]["values"]["text"] == "x" * 513


def _encoded(text):
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def two_texts(first, second):
    prompt = _prompt(bindings={
        "a": _binding("2", "text", label="A", order=1),
        "b": _binding("10", "text", label="B", order=2),
    })
    prompt["2"]["inputs"]["text"] = first
    prompt["10"] = {"class_type": "CLIPTextEncode", "inputs": {"text": second, "clip": ["6", 0]}}
    prompt["3"]["inputs"]["negative"] = ["10", 0]
    return prompt


def test_one_budget_per_asset_counts_encoded_bytes_first_fit_in_control_order():
    budget = cs.FULL_TEXT_BUDGET
    # Exactly at the budget: kept.
    at = "y" * (budget - 2)  # +2 quotes in the encoded size
    assert _encoded(at) == budget
    assert capture(text_prompt(at))[1]["values"] == {"text": at}
    # One byte over: not kept, counted.
    over = "y" * (budget - 1)
    assert capture(text_prompt(over))[1] == {"v": 1, "values": {}, "omitted": 1}
    # Escapes count: control characters encode 6x, so this short text is over.
    escaped = "\x01" * (budget // 6 + 1)
    assert len(escaped) < budget and capture(text_prompt(escaped))[1]["omitted"] == 1
    # First fit: A uses most of the budget, B no longer fits; a smaller B would.
    big, small = "a" * (budget - 600), "b" * 700
    record = capture(two_texts(big, small))[1]
    assert record["values"] == {"a": big} and record["omitted"] == 1
    record = capture(two_texts("c" * 900, "d" * (budget - 1500)))[1]
    assert set(record["values"]) == {"a", "b"} and "omitted" not in record


def test_a_lone_surrogate_keeps_the_section_and_is_never_kept_in_full():
    bad = "before \ud800 after " + "z" * 600
    section, full = capture(text_prompt(bad))
    preview = section["fields"][field_of(section, "text")]
    assert "�" in preview and "\ud800" not in preview
    assert full == {"v": 1, "values": {}, "omitted": 1}
    # The section encodes as UTF-8 (it used to raise inside the size check).
    json.dumps(section, ensure_ascii=False).encode("utf-8")
    # A short value and a label with one are scrubbed too.
    section, full = capture(text_prompt("x\udfffy", text=_binding("2", "text", label="Pro\ud800mpt")))
    assert section["fields"] == {"Pro�mpt": "x�y"} and full is None


def test_full_text_only_for_controls_the_section_kept():
    entries = [{"binding_id": "kept", "full": "k" * 600}]
    assert cs.full_text_record(entries) == {"v": 1, "values": {"kept": "k" * 600}}
    # `controls_capture` hands only the kept list to it (pruned Controls have no preview).
    prompt = text_prompt("t" * 700)
    section, full = capture(prompt)
    assert set(full["values"]) <= {c["binding_id"] for c in section["controls"]}


def test_a_cut_row_list_is_marked_capped():
    spec = {"kind": "ROWS", "fields": [{"id": "name", "kind": "STRING"}]}
    rows = [{"name": "n" * 80} for _ in range(10)]
    descriptor = {**spec, "value": rows}
    detail = control_rows.detail(rows, descriptor, cs.TEXT_VALUE_LIMIT)
    assert detail.endswith("…") and cs._row_text_capped(descriptor, detail)
    short = [{"name": "n"}]
    assert not cs._row_text_capped({**spec, "value": short}, control_rows.detail(short, {**spec, "value": short}, 512))
    assert not cs._row_text_capped({"kind": "STRING"}, "x" * 600)
    assert cs._record({"binding_id": "r", "kind": "ROWS", "targets": [], "label": "R", "group": "",
                       "section": "workflow", "origin": "authored", "advanced": False, "capped": True}, "R")["capped"] is True


# ── UTF-8-safe writers ───────────────────────────────────────────────────────

def test_dumps_utf8_keeps_todays_bytes_and_escapes_only_the_surrogate():
    value = {"t": "夜明け 👩‍👩‍👧 \r\n", "n": 1}
    assert json_text.dumps_utf8(value, separators=(",", ":")) == json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    bad = {"t": "a\ud800b 夜明け", "k\udfff": 1}
    text = json_text.dumps_utf8(bad, separators=(",", ":"))
    text.encode("utf-8")
    assert json.loads(text) == bad
    # Only the surrogates are escaped: the CJK stays raw, and the rest is byte-identical.
    assert text == json.dumps(bad, ensure_ascii=False, separators=(",", ":")).replace("\ud800", "\\ud800").replace("\udfff", "\\udfff")
    assert "夜明け" in text
    assert json_text.scrub({"k\ud800": ["x\udfff", 1]}) == {"k\ufffd": ["x\ufffd", 1]}
    plain = {"a": ["b"]}
    assert json_text.scrub(plain) is plain


def test_project_writers_stay_strict_so_durable_data_never_holds_a_surrogate(tmp_path):
    (tmp_path / "state").mkdir()
    with pytest.raises(UnicodeEncodeError):
        ps.publish_component(str(tmp_path), "provenance_" + "0" * 64, {"v": "a\ud800b"}, format_version=3)
    # An ordinary component's bytes are unchanged.
    normal = ps.publish_component(str(tmp_path), "provenance_" + "1" * 64, {"v": "夜"}, format_version=3)
    assert normal["bytes"] == len(json.dumps({"v": "夜"}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    # A revision hash never raises, and it moves with the full text.
    assert ps.provenance_revision({"v": "a\ud800b"}, {})
    plain = {"editor_export": {}}
    with_text = {**plain, cs.FULL_TEXT_KEY: {"v": 1, "values": {"b": "x" * 600}}}
    assert ps.provenance_revision(plain, {}) != ps.provenance_revision(with_text, {})


def test_the_ffmetadata_writer_scrubs_a_lone_surrogate(tmp_path):
    from server import media_helpers
    path = media_helpers._write_ffmetadata_file({"comment": "a\ud800b"})
    try:
        assert Path(path).read_text(encoding="utf-8").endswith("comment=a�b\n")
    finally:
        Path(path).unlink()


# ── Clip copies never carry asset-only provenance ────────────────────────────

def _params_with_both():
    modes = {"label": cs.MODES_SECTION_LABEL, "display_type": cs.MODES_DISPLAY_TYPE, "fields": {"Task": "Edit"},
             "modes": [{"id": "t", "field": "Task", "status": "selected"}]}
    controls = {"label": cs.SECTION_LABEL, "display_type": "controls", "fields": {}}
    return {"seed": 1, cs.FULL_TEXT_KEY: {"v": 1, "values": {"x": "y" * 600}},
            "editor_export": {"tracked_metadata": [controls, modes], "fps": 24}}


def test_asset_only_keys_mirror_the_capture_constants():
    assert ps.ASSET_ONLY_PARAM_KEYS == {cs.FULL_TEXT_KEY}
    assert ps.ASSET_ONLY_SECTION_TYPES == {cs.MODES_DISPLAY_TYPE}


def test_take_copy_params_strips_without_mutating_and_shares_nothing_changed():
    params = _params_with_both()
    before = json.dumps(params, sort_keys=True)
    out = ps.take_copy_params(params)
    assert json.dumps(params, sort_keys=True) == before  # the asset keeps both
    assert cs.FULL_TEXT_KEY not in out
    assert [s["display_type"] for s in out["editor_export"]["tracked_metadata"]] == ["controls"]
    assert out["editor_export"] is not params["editor_export"]
    assert out["editor_export"]["tracked_metadata"] is not params["editor_export"]["tracked_metadata"]
    plain = {"seed": 1, "editor_export": {"tracked_metadata": []}}
    assert ps.take_copy_params(plain) is plain


def test_every_clip_construction_drops_asset_only_provenance():
    from server.timeline_state import ClipReference
    params = _params_with_both()
    clip = ClipReference(generation_params=params, take_metadata=dict(params))
    for copy_ in (clip.generation_params, clip.take_metadata, clip.to_dict()["generation_params"]):
        assert cs.FULL_TEXT_KEY not in copy_
        assert all(s.get("display_type") != "modes" for s in copy_["editor_export"]["tracked_metadata"])
    assert cs.FULL_TEXT_KEY in params and len(params["editor_export"]["tracked_metadata"]) == 2
    restored = ClipReference.from_dict({**clip.to_dict(), "generation_params": params})
    assert cs.FULL_TEXT_KEY not in restored.generation_params


def test_asset_id_remap_never_rewrites_the_full_text(monkeypatch):
    from server import project_commit
    assert project_commit._remap_asset_id_values({"x": "old"}, {"old": "new"}) == {"x": "new"}
    params = {"ref": "old", "list": ["old"], cs.FULL_TEXT_KEY: {"v": 1, "values": {"b": "old"}}}
    remapped = project_commit._remap_generation_params(params, {"old": "new"})
    assert remapped == {"ref": "new", "list": ["new"], cs.FULL_TEXT_KEY: {"v": 1, "values": {"b": "old"}}}
    assert params["ref"] == "old"  # input untouched


# ── Modes at run ─────────────────────────────────────────────────────────────

ROWS = [
    {"id": "t", "name": "Task", "status": "selected", "choice": "Edit With Words", "matches": ["Edit With Words"]},
    {"id": "m", "name": "Mask", "status": "custom", "choice": "", "matches": []},
    {"id": "p", "name": "Passes", "status": "ambiguous", "choice": "", "matches": ["Both", "Refine"]},
    {"id": "u", "name": "Task", "status": "unavailable", "choice": "", "matches": []},
]


def relay_prompt(categories):
    manifest = {"v": 1, "bindings": {}, "presets": {"a": {"name": "A", "order": 0, "members": {}}},
                "preset_categories": categories}
    return {"R": {"class_type": "SonderControlRelay", "inputs": {"manifest": json.dumps(manifest)}}}


def test_modes_section_words_each_status_and_disambiguates_names():
    section = cs.modes_section({}, ROWS)
    assert section["label"] == "Modes at run" and section["display_type"] == "modes"
    assert section["fields"] == {"Task": "Edit With Words", "Mask": "Custom", "Passes": "Matches Both, Refine",
                                 "Task [u]": "Unavailable"}
    assert [m["status"] for m in section["modes"]] == ["selected", "custom", "ambiguous", "unavailable"]
    assert cs.modes_section({}, []) is None
    assert cs.modes_section({}, [{"id": "x", "status": "bogus"}]) is None


def test_modes_not_recorded_only_when_the_manifest_has_real_categories():
    declared = relay_prompt({"c": {"name": "Variant", "order": 0, "choices": ["a"]}})
    assert cs.modes_section(declared, None) == {"label": "Modes at run", "display_type": "modes", "fields": {},
                                                "modes": [], "status": "not_recorded"}
    assert cs.modes_section(relay_prompt({}), None) is None  # every category deleted
    assert cs.modes_section({}, None) is None
    assert cs.modes_section(None, None, declared=True)["status"] == "not_recorded"


def test_modes_section_stays_under_its_cap():
    rows = [{"id": f"c{i}", "name": f"Category {i}", "status": "ambiguous", "choice": "",
             "matches": ["m" * 120] * 8} for i in range(16)]
    section = cs.modes_section({}, rows)
    assert cs._encoded_size(section) <= cs.MAX_MODES_SECTION_BYTES
    assert section["truncated"] == 16 - len(section["modes"]) and section["modes"]


@pytest.fixture
def feedback(monkeypatch):
    run_feedback.reset_for_tests()
    state = {"owners": [], "pid": "p1"}
    monkeypatch.setattr(run_feedback, "_prompt_id_for", lambda prompt: state["pid"] if state["pid"] in run_feedback._DATA else "")
    monkeypatch.setattr(sdk_runs, "bound_run", lambda pid: {"owners": list(state["owners"])})
    monkeypatch.setattr(run_feedback, "_publish", lambda *a, **k: None)
    with run_feedback._LOCK:
        run_feedback._DATA["p1"] = run_feedback._new("p1")
    yield state
    run_feedback.reset_for_tests()


def test_modes_for_save_returns_the_record(feedback):
    feedback["owners"] = [("host", "x", "h")]
    run_feedback.note_modes(ROWS[:1], prompt_id="p1")
    assert run_feedback.modes_for_save({}, timeout=0) == ROWS[:1]
    assert run_feedback.modes_of("p1") == ROWS[:1]


def test_modes_for_save_waits_only_a_short_grace_once_registered_with_nothing_in_flight(feedback):
    feedback["owners"] = [("host", "x", "h")]
    started = time.monotonic()
    assert run_feedback.modes_for_save({}, timeout=5) is None
    elapsed = time.monotonic() - started
    assert run_feedback._MODES_SAVE_IDLE_GRACE_SEC <= elapsed < 1.0
    started = time.monotonic()
    assert run_feedback.modes_for_save({}, timeout=0) is None  # timeout 0 never waits
    assert time.monotonic() - started < 0.1


def test_modes_for_save_catches_a_capture_marked_just_after_registration(feedback):
    """The canvas route registers the receipt, then marks its capture."""
    feedback["owners"] = [("host", "x", "h")]

    def mark_late():
        time.sleep(0.05)
        run_feedback.capture_started("p1")
        time.sleep(0.2)
        run_feedback.note_modes(ROWS[:1], prompt_id="p1")
        run_feedback.capture_settled("p1")
    thread = threading.Thread(target=mark_late)
    thread.start()
    try:
        assert run_feedback.modes_for_save({}, timeout=3) == ROWS[:1]
    finally:
        thread.join()


def test_modes_for_save_waits_for_a_late_registration_and_its_capture(feedback):
    def register_then_capture():
        time.sleep(0.1)
        feedback["owners"] = [("host", "x", "h")]
        run_feedback.capture_started("p1")
        time.sleep(0.1)
        run_feedback.note_modes(ROWS[:2], prompt_id="p1")
        run_feedback.capture_settled("p1")
    thread = threading.Thread(target=register_then_capture)
    thread.start()
    try:
        assert run_feedback.modes_for_save({}, timeout=3) == ROWS[:2]
    finally:
        thread.join()


def test_modes_for_save_gives_up_at_its_timeout_and_after_a_settled_capture(feedback):
    started = time.monotonic()
    assert run_feedback.modes_for_save({}, timeout=0.2) is None  # never registered
    assert 0.15 < time.monotonic() - started < 1.5
    feedback["owners"] = [("host", "x", "h")]
    run_feedback.capture_started("p1")
    run_feedback.capture_settled("p1")  # dropped or failed
    started = time.monotonic()
    assert run_feedback.modes_for_save({}, timeout=5) is None
    assert time.monotonic() - started < 1.0  # the short grace only
    with run_feedback._LOCK:
        assert run_feedback._DATA["p1"]["modes_capturing"] == 0


def test_modes_for_save_never_holds_the_lock_while_waiting(feedback):
    result = []
    thread = threading.Thread(target=lambda: result.append(run_feedback.modes_for_save({}, timeout=1.0)))
    thread.start()
    time.sleep(0.1)
    acquired = run_feedback._LOCK.acquire(timeout=0.2)
    assert acquired
    run_feedback._LOCK.release()
    thread.join()
    assert result == [None]


def test_a_capture_cancelled_before_it_starts_still_settles(feedback, monkeypatch):
    import asyncio
    from server import run_output_routes
    monkeypatch.setattr(run_output_routes, "_MODES_TASKS", set())

    async def scenario():
        run_output_routes._capture_modes({"prompt": {}}, "p1", "")
        with run_feedback._LOCK:
            assert run_feedback._DATA["p1"]["modes_capturing"] == 1
        for task in list(run_output_routes._MODES_TASKS):
            task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    with run_feedback._LOCK:
        assert run_feedback._DATA["p1"]["modes_capturing"] == 0


def test_an_unobserved_run_has_no_modes(monkeypatch):
    run_feedback.reset_for_tests()
    assert run_feedback.modes_for_save({"x": {}}, timeout=2) is None
    assert run_feedback.modes_of("") is None
