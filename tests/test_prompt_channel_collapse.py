"""Channel-header parser and template-switch collapse (batch step 5).

Switching channel templates used to preserve stranded text as keys no template
named — technically lossless, experientially invisible, and the cause of the
"saved a one-channel standard prompt" report. Collapse-then-re-split replaces
that: one rule in both directions, and the text stays visible.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import prompt_channel_templates as pct
from server import prompt_payload as pp
import server.routes as routes
from server.timeline_state import LaneConfig, PromptSection, Scene, TimelineProject


ROOT = Path(__file__).resolve().parents[1]
REF = pct.get_channel_template("minimax_h3_ref")
SONDER = pct.get_channel_template("sonder")
STANDARD = pct.get_channel_template("standard")


# --- parser -----------------------------------------------------------------

def test_header_parse_is_anchored_and_survives_inline_colons():
    # `says: <d>...</d>` is legitimate MiniMax body text. A "word followed by a
    # colon" rule would shred it into a bogus channel.
    body = "[Shot 1] the man (S1) says: <d>[English] hi</d>\nsays: still body text"
    parsed = pp.split_channel_headers(f"detailed_description:\n{body}", REF)
    assert parsed["detailed_description"] == body
    assert parsed["summary"] == ""


def test_unknown_header_stays_labelled_in_the_first_channel():
    # Widening: `visual:` is not a MiniMax key, so it must remain visible and
    # labelled rather than vanishing into an unreachable key.
    parsed = pp.split_channel_headers("visual:\na dog walks", REF)
    assert parsed["subject_definitions"] == "visual:\na dog walks"


def test_text_before_the_first_header_goes_to_the_first_channel():
    parsed = pp.split_channel_headers("loose opening\n\nsummary:\nthe summary", REF)
    assert parsed["subject_definitions"] == "loose opening"
    assert parsed["summary"] == "the summary"


def test_same_line_and_next_line_header_forms_both_parse():
    assert pp.split_channel_headers("summary: on the same line", REF)["summary"] == (
        "on the same line")
    assert pp.split_channel_headers("summary:\non the next line", REF)["summary"] == (
        "on the next line")


def test_join_carries_keys_from_a_third_template():
    # A -> B -> C must not drop B's text on the way through.
    joined = pp.join_channel_headers(
        {"summary": "b text", "visual": "a text"}, REF)
    assert "summary:\nb text" in joined
    assert "visual:\na text" in joined


def test_join_skips_empty_channels():
    assert pp.join_channel_headers({"summary": "", "detailed_description": "x"}, REF) == (
        "detailed_description:\nx")


# --- collapse ---------------------------------------------------------------

_SIX = {
    "subject_definitions": "S defs",
    "summary": "a summary",
    "detailed_description": "[Shot 1] he says: <d>[English] hi</d>",
}


def test_narrowing_then_widening_restores_every_channel():
    narrowed = pp.collapse_channels_for_template(_SIX, REF, STANDARD)
    # Everything is now visible in the one channel Standard has.
    assert narrowed["visual"].startswith("subject_definitions:\nS defs")
    assert "detailed_description:" in narrowed["visual"]

    widened = pp.collapse_channels_for_template(narrowed, STANDARD, REF)
    for key, value in _SIX.items():
        assert widened[key] == value, key


def test_widening_keeps_unmatched_text_visible():
    widened = pp.collapse_channels_for_template({"visual": "a dog walks"}, SONDER, REF)
    assert widened["subject_definitions"] == "visual:\na dog walks"
    # Never silently stranded in a key the new template does not name.
    assert widened.get("visual") == ""


def test_collapse_clears_outgoing_keys_explicitly():
    # Channel updates MERGE, so a key merely left out would keep its old value
    # and the collapsed copy would appear twice.
    patch = pp.collapse_channels_for_template(_SIX, REF, STANDARD)
    for key in ("summary", "detailed_description", "subject_definitions"):
        assert patch[key] == "", key


def test_collapsing_an_empty_section_stays_empty():
    patch = pp.collapse_channels_for_template({}, REF, STANDARD)
    assert not any(value for value in patch.values())


# --- pasting a model output --------------------------------------------------

_PASTED_MINIMAX = "\n".join([
    "integrated_multimodal_description:",
    "[Shot 1] Live-action, cinematic, a baker opens the shutters."
    " He says: <d>[English] First batch of the morning.</d>",
    "",
    "overall_soundscape:",
    "Wooden shutters scrape open over a quiet street.",
    "",
    "non_diegetic_music:",
    "A soft acoustic-guitar pattern at a moderate tempo.",
])


def test_a_pasted_minimax_output_arranges_itself_by_channel():
    # The headline reason the parser exists: a whole model output should drop
    # into the writing tool and land in the right fields, dialogue intact.
    base = pct.get_channel_template("minimax_h3_base")
    parsed = pp.split_channel_headers(_PASTED_MINIMAX, base)
    assert parsed["integrated_multimodal_description"].startswith("[Shot 1] Live-action")
    assert "<d>[English] First batch of the morning.</d>" in (
        parsed["integrated_multimodal_description"])
    assert parsed["overall_soundscape"] == (
        "Wooden shutters scrape open over a quiet street.")
    assert parsed["non_diegetic_music"] == (
        "A soft acoustic-guitar pattern at a moderate tempo.")
    # The dialogue's own colon must not have started a phantom channel.
    assert "He says" not in parsed["overall_soundscape"]


def test_pasted_output_round_trips_back_to_the_same_text():
    base = pct.get_channel_template("minimax_h3_base")
    parsed = pp.split_channel_headers(_PASTED_MINIMAX, base)
    assert pp.join_channel_headers(parsed, base) == _PASTED_MINIMAX


# --- the switch route -------------------------------------------------------

def _project_with_sections(template_id, channels_per_section):
    project = TimelineProject(name="Project")
    project.metadata = {pct.PROJECT_TEMPLATE_KEY: template_id}
    for index, channels in enumerate(channels_per_section):
        scene = Scene(scene_id=f"scene-{index}", duration_frames=360)
        scene.prompt_track_config = LaneConfig()
        scene.prompt_sections = [PromptSection(0, 120, channels=dict(channels))]
        project.scenes.append(scene)
    return project


def test_switch_rewrites_every_scene_not_just_the_active_one():
    # The template is project-level, so a switch that only converted the loaded
    # scene would leave the rest disagreeing with the pointer.
    project = _project_with_sections("minimax_h3_ref", [_SIX, _SIX])
    rewritten = routes._recollapse_prompt_channels(project, REF, STANDARD)
    assert rewritten == 2
    for scene in project.scenes:
        channels = scene.prompt_sections[0].channels
        assert "subject_definitions:" in channels["visual"]
        assert channels["summary"] == ""


def test_switch_is_reversible_through_the_same_helper():
    project = _project_with_sections("minimax_h3_ref", [_SIX])
    routes._recollapse_prompt_channels(project, REF, STANDARD)
    routes._recollapse_prompt_channels(project, STANDARD, REF)
    channels = project.scenes[0].prompt_sections[0].channels
    for key, value in _SIX.items():
        assert channels[key] == value, key


def test_switch_reports_nothing_rewritten_when_there_is_no_text():
    project = _project_with_sections("sonder", [{"visual": ""}])
    assert routes._recollapse_prompt_channels(project, SONDER, REF) == 0


# --- JS parity ---------------------------------------------------------------

_SPLIT_CASES = [
    ["detailed_description:\n[Shot 1] he says: <d>[English] hi</d>", "minimax_h3_ref"],
    ["visual:\na dog walks", "minimax_h3_ref"],
    ["loose opening\n\nsummary:\nthe summary", "minimax_h3_ref"],
    ["summary: on the same line", "minimax_h3_ref"],
    ["", "minimax_h3_ref"],
    ["subject_definitions:\nS defs\n\nsummary:\na summary", "standard"],
    ["no headers at all", "sonder"],
]

_COLLAPSE_CASES = [
    [_SIX, "minimax_h3_ref", "standard"],
    [{"visual": "a dog walks"}, "sonder", "minimax_h3_ref"],
    [{"visual": "a", "speech": "b", "sounds": "c"}, "sonder", "standard"],
    [{}, "minimax_h3_ref", "sonder"],
    [{"summary": "kept", "visual": "also kept"}, "minimax_h3_ref", "standard"],
]


def _node_json(expression):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the collapse parity tests")
    module = (ROOT / "web" / "js" / "prompt_composition.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module)});\n"
        f"console.log(JSON.stringify({expression}));\n"
    )
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)


def test_split_matches_between_python_and_javascript():
    expected = [pp.split_channel_headers(text, pct.get_channel_template(tid))
                for text, tid in _SPLIT_CASES]
    actual = _node_json(
        f"{json.dumps(_SPLIT_CASES)}.map(([t, id]) => mod.splitChannelHeaders(t, id))")
    assert actual == expected
    # Anti-vacuity: the fixtures must produce real, differing parses.
    assert expected[0]["detailed_description"].startswith("[Shot 1]")
    assert expected[1]["subject_definitions"] == "visual:\na dog walks"


def test_collapse_matches_between_python_and_javascript():
    expected = [pp.collapse_channels_for_template(
        channels, pct.get_channel_template(src), pct.get_channel_template(dst))
        for channels, src, dst in _COLLAPSE_CASES]
    actual = _node_json(
        f"{json.dumps(_COLLAPSE_CASES)}"
        ".map(([c, from_, to]) => mod.collapseChannelsForTemplate(c, from_, to))")
    assert actual == expected
    assert any(entry.get("visual") for entry in expected)


# Malformed shapes matter as much as well-formed ones: this normalizer is
# reached from `normalizeEditorSettings`, which evaluates at module scope over
# user-editable localStorage. The JS half used to throw on a non-array and
# iterate a string per character, while the Python half returned [].
_SUBJECT_ID_CASES = [
    [{"entity_id": "ref-a"}, {"entity_id": "ref-b"}],
    [{"entity_id": "ref-a", "retention": "attribute_transfer"}],
    [{"entity_id": "ref-a", "retention": "not-a-marker"}],
    [{"entity_id": "ref-a", "retention": 7}],
    [{"entity_id": "  ref-a  "}, {"entity_id": ""}],
    [{"entity_id": "ref-a", "retention": "weak_reference"},
     {"entity_id": "ref-a", "retention": "attribute_transfer"}],
    [],
    # Not bindings. A binding is an object, so bare strings — including the
    # "[object Object]" the pre-fix settings normalizer wrote — are dropped.
    ["ref-a", "ref-b"],
    ["[object Object]"],
    [None, 0, False, [], ["nested"]],
    [{"entity_id": 0}, {"entity_id": None}, {"entity_id": ["ref-a"]}],
    # Malformed containers. These reach the normalizer from request bodies and
    # from user-editable localStorage, so neither side may raise.
    {},
    {"entity_id": "ref-a"},
    5,
    "abc",
    None,
    True,
]


def test_subject_id_normalization_matches_between_python_and_javascript():
    expected = [pp.normalize_subject_ids(case) for case in _SUBJECT_ID_CASES]
    actual = _node_json(
        f"{json.dumps(_SUBJECT_ID_CASES)}.map((c) => mod.normalizeSubjectIds(c))")
    assert actual == expected
    # Anti-vacuity: the fixtures must exercise retention fallback and
    # first-wins dedupe, not just agree on a pile of empties.
    default = {"entity_id": "ref-a", "retention": "fully_preserved"}
    assert expected[0] == [default, {"entity_id": "ref-b",
                                     "retention": "fully_preserved"}]
    assert expected[1] == [{"entity_id": "ref-a", "retention": "attribute_transfer"}]
    assert expected[2] == [default]  # unknown marker falls back
    assert expected[3] == [default]  # non-string marker falls back
    assert expected[4] == [default]  # whitespace trimmed, blank dropped
    assert expected[5] == [{"entity_id": "ref-a", "retention": "weak_reference"}]
    # Everything from the first non-binding case on yields nothing.
    assert all(entry == [] for entry in expected[6:])
