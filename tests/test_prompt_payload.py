"""Unit tests for server/prompt_payload.py — composition, window resolution,
sanitization, and PromptRelay payload building.

Frame ranges are half-open [start, end) throughout.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import prompt_payload as pp
from server.timeline_state import PromptSection


# --- compose -----------------------------------------------------------------

def test_compose_section_text_labels_on():
    channels = {"visual": "a shot", "speech": "hello", "sounds": "rain"}
    assert pp.compose_section_text(channels, labels_on=True) == (
        "[VISUAL]: a shot [SPEECH]: hello [SOUNDS]: rain"
    )


def test_compose_section_text_labels_off_and_empty_channels():
    channels = {"visual": "a shot", "speech": "", "sounds": "rain"}
    assert pp.compose_section_text(channels, labels_on=False) == "a shot rain"
    assert pp.compose_section_text(channels, labels_on=True) == "[VISUAL]: a shot [SOUNDS]: rain"
    assert pp.compose_section_text({"visual": "", "speech": "", "sounds": ""}, True) == ""
    assert pp.compose_section_text(None, True) == ""


def test_compose_window_prompt():
    channels = {"visual": "action", "speech": "", "sounds": ""}
    assert pp.compose_window_prompt("global", channels, labels_on=True) == "global [VISUAL]: action"
    assert pp.compose_window_prompt("global", channels, labels_on=False) == "global action"
    assert pp.compose_window_prompt("", channels, labels_on=False) == "action"
    assert pp.compose_window_prompt("global", None) == "global"
    assert pp.compose_window_prompt("", None) == ""


def test_normalize_channels():
    assert pp.normalize_channels({"visual": "v"}) == {"visual": "v", "speech": "", "sounds": ""}
    assert pp.normalize_channels(None, legacy_prompt="flat") == {
        "visual": "flat", "speech": "", "sounds": ""
    }
    assert pp.normalize_channels({"visual": None, "speech": "s"}) == {
        "visual": "", "speech": "s", "sounds": ""
    }


# --- resolve_segments ----------------------------------------------------------

def _sections(*ranges):
    return [PromptSection(start_frame=s, end_frame=e, prompt=text) for s, e, text in ranges]


def test_resolve_zero_sections():
    assert pp.resolve_segments([], 0, 100) == []
    assert pp.resolve_segments(None, 0, 100) == []


def test_resolve_single_section_fills_whole_window():
    segments = pp.resolve_segments(_sections((40, 50, "A")), 0, 100, labels_on=False)
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [("A", 0, 100)]


def test_resolve_gaps_leading_middle_trailing():
    sections = _sections((10, 20, "A"), (40, 50, "B"))
    segments = pp.resolve_segments(sections, 0, 100, labels_on=False)
    # Leading gap → first section extends back; middle gap → A holds until B
    # starts; trailing gap → B holds to window end.
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [
        ("A", 0, 40),
        ("B", 40, 100),
    ]


def test_resolve_section_entirely_before_window_holds_tail():
    # Hold-until-next applies at scene level: a section before the window
    # still owns the window when nothing follows it.
    segments = pp.resolve_segments(_sections((0, 10, "A")), 40, 50, labels_on=False)
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [("A", 0, 10)]


def test_resolve_section_after_window_extends_back():
    segments = pp.resolve_segments(_sections((80, 90, "B")), 0, 20, labels_on=False)
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [("B", 0, 20)]


def test_resolve_overlap_first_wins():
    # A[0,50) B[30,80): the EARLIER section keeps the overlap zone, matching
    # the legacy first-match selector. B starts where A ends.
    sections = _sections((0, 50, "A"), (30, 80, "B"))
    segments = pp.resolve_segments(sections, 0, 80, labels_on=False)
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [
        ("A", 0, 50),
        ("B", 50, 80),
    ]


def test_resolve_fully_shadowed_section_drops():
    # B lives entirely inside A → shadowed, never emitted (zero-span drop)
    sections = _sections((0, 50, "A"), (10, 30, "B"), (60, 90, "C"))
    segments = pp.resolve_segments(sections, 0, 100, labels_on=False)
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [
        ("A", 0, 60),
        ("C", 60, 100),
    ]


def test_resolve_equal_starts_first_wins():
    sections = _sections((10, 30, "A"), (10, 50, "B"))
    segments = pp.resolve_segments(sections, 0, 60, labels_on=False)
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [
        ("A", 0, 30),
        ("B", 30, 60),
    ]


def test_resolve_empty_composed_section_drops():
    sections = [
        PromptSection(0, 20, channels={"visual": "", "speech": "", "sounds": ""}),
        PromptSection(30, 60, prompt="real"),
    ]
    segments = pp.resolve_segments(sections, 0, 80, labels_on=False)
    assert [(s["text"], s["start"], s["end"]) for s in segments] == [("real", 0, 80)]


def test_resolve_rebases_to_window_local():
    segments = pp.resolve_segments(_sections((250, 280, "A"), (280, 300, "B")), 250, 300,
                                   labels_on=False)
    assert [(s["start"], s["end"]) for s in segments] == [(0, 30), (30, 50)]


def test_resolve_raw_v1_snapshot_dicts():
    # Pre-upgrade frozen job sections carry only the flat prompt — the
    # legacy→visual fallback applies to raw dicts too.
    raw = [
        {"start_frame": 0, "end_frame": 10, "prompt": "old flat"},
        {"start_frame": 10, "end_frame": 20,
         "channels": {"visual": "new", "speech": "talk", "sounds": ""}},
    ]
    segments = pp.resolve_segments(raw, 0, 20, labels_on=True)
    assert [s["text"] for s in segments] == [
        "[VISUAL]: old flat",
        "[VISUAL]: new [SPEECH]: talk",
    ]


def test_resolve_invalid_window():
    assert pp.resolve_segments(_sections((0, 10, "A")), 50, 50) == []
    assert pp.resolve_segments(_sections((0, 10, "A")), 60, 50) == []


# --- join + compose_range_prompt -------------------------------------------------

def test_join_segment_texts():
    assert pp.join_segment_texts(["a", "b", "c"], ".") == "a. b. c"
    assert pp.join_segment_texts(["a", "b"], ",") == "a, b"
    assert pp.join_segment_texts(["a", "b"], "") == "a b"
    assert pp.join_segment_texts(["a", "b"], "  ") == "a b"
    assert pp.join_segment_texts(["only"], ".") == "only"
    assert pp.join_segment_texts([], ".") == ""
    assert pp.join_segment_texts(["a", "", "b"], ".") == "a. b"


def test_compose_range_prompt_multi_segment_temporal_order():
    sections = _sections((0, 30, "walks"), (30, 60, "runs"), (60, 90, "jumps"))
    out = pp.compose_range_prompt("global", sections, 0, 90, labels_on=False)
    assert out == "global walks. runs. jumps"
    # Labels ON groups by channel: ONE label, segment texts joined in order
    out = pp.compose_range_prompt("global", sections, 0, 90, labels_on=True, delimiter=",")
    assert out == "global [VISUAL]: walks, runs, jumps"


def test_compose_range_prompt_groups_channels_across_segments():
    sections = [
        PromptSection(0, 30, channels={"visual": "a dog", "speech": "woof", "sounds": ""}),
        PromptSection(30, 60, channels={"visual": "it barks", "speech": "", "sounds": "rain"}),
    ]
    out = pp.compose_range_prompt("g", sections, 0, 60, labels_on=True)
    # One label per channel; texts in temporal order; empty channels omitted
    assert out == "g [VISUAL]: a dog. it barks [SPEECH]: woof [SOUNDS]: rain"
    # Labels OFF keeps plain temporal per-segment concatenation
    out = pp.compose_range_prompt("g", sections, 0, 60, labels_on=False)
    assert out == "g a dog woof. it barks rain"


def test_compose_range_prompt_before_window_hold_included():
    # A section entirely before the window still holds (hold-until-next)
    out = pp.compose_range_prompt("g", _sections((0, 10, "early")), 40, 50, labels_on=False)
    assert out == "g early"


def test_compose_range_prompt_zero_sections_and_empty_global():
    assert pp.compose_range_prompt("only global", [], 0, 50) == "only global"
    assert pp.compose_range_prompt("", _sections((0, 50, "sec")), 0, 50, labels_on=False) == "sec"
    assert pp.compose_range_prompt("", [], 0, 50) == ""


# --- sanitize ------------------------------------------------------------------

def test_sanitize_pipes():
    assert pp.sanitize_segment_text("a cyberpunk street | neon signs") == (
        "a cyberpunk street , neon signs"
    )


def test_sanitize_newlines_kill_block_mode_flip():
    # A line ending `<words> <number>:` would flip the Smart parser into
    # block mode for the whole payload — flattening newlines prevents the
    # header from ever sitting on its own line.
    text = "wide shot\nScene 2:\nclose up"
    assert "\n" not in pp.sanitize_segment_text(text)
    assert pp.sanitize_segment_text(text) == "wide shot Scene 2: close up"


def test_sanitize_numeric_tags_stripped_to_inner():
    assert pp.sanitize_segment_text("a pack of [3] wolves") == "a pack of 3 wolves"
    assert pp.sanitize_segment_text("x [1.5] y") == "x 1.5 y"
    assert pp.sanitize_segment_text("x [0-50] y") == "x 0-50 y"
    assert pp.sanitize_segment_text("x [0:50] y") == "x 0:50 y"


def test_sanitize_keeps_channel_labels():
    # Non-numeric brackets are NOT weight tags and must survive
    assert pp.sanitize_segment_text("[VISUAL]: a shot [SPEECH]: hi") == (
        "[VISUAL]: a shot [SPEECH]: hi"
    )


def test_sanitize_collapses_whitespace():
    assert pp.sanitize_segment_text("  a   b\r\n c  ") == "a b c"
    assert pp.sanitize_segment_text(None) == ""


# --- build_relay_payload ---------------------------------------------------------

def test_build_relay_payload_formats():
    segments = [
        {"text": "A", "start": 0, "end": 33},
        {"text": "B", "start": 33, "end": 97},
    ]
    payload = pp.build_relay_payload("global style", segments)
    assert payload["global_prompt"] == "global style"
    assert payload["smart_prompt"] == "A [0-33] | B [33-97]"
    assert payload["local_prompts"] == "A | B"
    assert payload["segment_lengths"] == "33,64"


def test_build_relay_payload_empty():
    payload = pp.build_relay_payload("global", [])
    assert payload["smart_prompt"] == ""
    assert payload["local_prompts"] == ""
    assert payload["segment_lengths"] == ""
    assert payload["global_prompt"] == "global"


def test_build_relay_payload_sanitizes_each_segment():
    segments = [{"text": "a|b\nScene 2:\nc [3]", "start": 0, "end": 10}]
    payload = pp.build_relay_payload("g", segments)
    assert payload["smart_prompt"] == "a,b Scene 2: c 3 [0-10]"


def test_build_relay_payload_drops_sanitize_to_empty_and_rebuilds_lengths():
    # A middle segment whose text sanitizes to empty must be dropped and its
    # span absorbed by the PREVIOUS neighbor — PromptRelay drops empty locals
    # after pipe-split WITHOUT re-aligning segment_lengths.
    segments = [
        {"text": "A", "start": 0, "end": 20},
        {"text": " \r\n  ", "start": 20, "end": 50},  # whitespace-only → sanitizes to ""
        {"text": "C", "start": 50, "end": 80},
    ]
    payload = pp.build_relay_payload("g", segments)
    assert payload["local_prompts"] == "A | C"
    assert payload["smart_prompt"] == "A [0-50] | C [50-80]"
    assert payload["segment_lengths"] == "50,30"


def test_build_relay_payload_dropped_first_segment_span_goes_to_next():
    segments = [
        {"text": "   ", "start": 0, "end": 30},
        {"text": "B", "start": 30, "end": 60},
    ]
    payload = pp.build_relay_payload("g", segments)
    assert payload["smart_prompt"] == "B [0-60]"
    assert payload["segment_lengths"] == "60"


def test_build_relay_payload_global_passthrough_untouched():
    # Global is never pipe-split or tag-parsed by the relay nodes — only
    # trimmed, never rewritten.
    payload = pp.build_relay_payload("  keep | pipes [3] and\nnewlines  ", [])
    assert payload["global_prompt"] == "keep | pipes [3] and\nnewlines"
