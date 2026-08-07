"""Executable spec for MiniMax H3 channel templates, shot markers, and timestamps.

Written red at P0 of the Prompt Channel Templates plan and green since P4. Every
template symbol is still reached from inside a test body rather than a
module-level import, so a future removal fails as an individual test rather than
a collection error.

Format authority is the MiniMax H3 prompt-writing guides:
  docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md  (T2VA / I2VA / FL2VA / L2VA)
  docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md   (full-reference mode)
in MiniMaxAI/MiniMax-H3 on Hugging Face. Where this file and those guides
disagree, the guides win and this file is wrong.

Scenario throughout: 15s at 24fps (360 frames), three sections [0,120),
[120,240), [240,360). Section 1 opens shot 1 and section 2 continues it; section
3 opens a second shot at frame 240, which is 10.000s, and stamps its cut time.
Opening a shot and stamping a cut time are independent flags — a section may do
either, both or neither, and nothing is forced on the first section. The
scene-global prompt is deliberately EMPTY so these expectations do not move when
P5 channelizes global text.

Frame ranges are half-open [start, end) throughout.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import prompt_payload as pp
from server.timeline_state import PromptSection


FPS = 24.0


def _templates():
    """Import the template catalog from inside a test body."""
    import importlib

    return importlib.import_module("server.prompt_channel_templates")


def _template(template_id):
    module = _templates()
    getter = getattr(module, "get_channel_template")
    return getter(template_id)


def _compose(sections, window_start, window_end, template, **kwargs):
    return pp.compose_range_prompt(
        "", sections, window_start, window_end,
        labels_on=False,          # the template owns label policy, not this flag
        delimiter=".",
        boundary_threshold_pct=0.0,
        template=template,
        fps=FPS,
        **kwargs,
    )


# --- scenario -----------------------------------------------------------------

# Authored text carries no trailing period: the project section delimiter (".")
# supplies the seam, exactly as it does for the three-channel default. Section 3
# also opens lowercase, because a new-shot seam reads "[Shot 2] At 00:10.000,
# the shot cuts to ..." and the composer never re-cases authored text.
def _scenario():
    sections = [
        PromptSection(0, 120, channels={
            "subject_definitions":
                "<Subject 1> is the young woman in <Picture 1>, with long dark hair",
            "summary":
                "[reference generation] The target video shows <Subject 1> reading"
                " a letter on a train",
            "retention_analysis":
                "<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved -"
                " her hair and cardigan are retained",
            "detailed_description":
                "<Subject 1> sits beside the rain-covered train window holding a"
                " folded letter",
            "overall_soundscape":
                "Steady rain ticks against the window over a low ventilation hum",
            "non_diegetic_music": "Sustained cello notes at a slow tempo",
        }),
        PromptSection(120, 240, channels={
            "detailed_description":
                "She lifts her gaze from the folded letter toward the passing"
                " city lights",
        }),
        PromptSection(240, 360, channels={
            "detailed_description":
                "the shot cuts to a close-up of her hands folding the letter"
                " along its crease",
        }),
    ]
    # Section 1 opens shot 1 and section 2 continues it; section 3 opens shot 2
    # and asks for its cut time. Neither flag is implied by position: a section
    # that opens no shot emits no marker, wherever it sits.
    for section, starts, stamps in zip(sections, (True, False, True),
                                       (False, False, True)):
        setattr(section, "starts_new_shot", starts)
        setattr(section, "shot_timestamp", stamps)
    return sections


FULL_WINDOW_EXPECTED = (
    "subject_definitions:\n"
    "<Subject 1> is the young woman in <Picture 1>, with long dark hair"
    "\n\n"
    "summary:\n"
    "[reference generation] The target video shows <Subject 1> reading a letter"
    " on a train"
    "\n\n"
    "retention_analysis:\n"
    "<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - her hair and"
    " cardigan are retained"
    "\n\n"
    "detailed_description:\n"
    "[Shot 1] <Subject 1> sits beside the rain-covered train window holding a"
    " folded letter."
    " She lifts her gaze from the folded letter toward the passing city lights."
    " [Shot 2] At 00:10.000, the shot cuts to a close-up of her hands folding"
    " the letter along its crease"
    "\n\n"
    "overall_soundscape:\n"
    "Steady rain ticks against the window over a low ventilation hum"
    "\n\n"
    "non_diegetic_music:\n"
    "Sustained cello notes at a slow tempo"
)

# Window [240,360) resolves to section 3 alone: sections 1-2 hold only up to
# frame 240, so nothing of theirs survives clipping. Section 3 is then the first
# emitted segment, so its shot renumbers to [Shot 1] — numbering is dense over
# the EFFECTIVE set. It still stamps a cut time, because its own flag says so,
# and a first emitted segment's window-local start is always 0: `At 00:00.000,`.
# That shape is opt-in and never a default (the base guide advises against
# stamping the first shot), but it is requestable — "at 0:00 the subject
# appears" is a real thing to write.
# Its empty channels are omitted (plan decision 3) — including the
# subject_definitions authored on section 1, which this window never reaches.
SECTION_THREE_EXPECTED = (
    "detailed_description:\n"
    "[Shot 1] At 00:00.000, the shot cuts to a close-up of her hands folding the"
    " letter along its crease"
)


# --- template catalog ---------------------------------------------------------

def test_minimax_ref_template_channel_order():
    # Full-reference mode, guide section 1: six sections in a fixed order.
    template = _template("minimax_h3_ref")
    assert [channel["key"] for channel in template["channels"]] == [
        "subject_definitions",
        "summary",
        "retention_analysis",
        "detailed_description",
        "overall_soundscape",
        "non_diegetic_music",
    ]


def test_minimax_base_template_channel_order():
    # Base mode, guide section 2.2: three core fields.
    template = _template("minimax_h3_base")
    assert [channel["key"] for channel in template["channels"]] == [
        "integrated_multimodal_description",
        "overall_soundscape",
        "non_diegetic_music",
    ]


def test_minimax_labels_are_bare_field_names():
    # MiniMax field names are the labels — no [VISUAL]-style bracket decoration.
    for template_id in ("minimax_h3_base", "minimax_h3_ref"):
        template = _template(template_id)
        for channel in template["channels"]:
            assert channel["label"] == channel["key"]
            assert "[" not in channel["label"]


def test_minimax_templates_force_labels_on():
    # A named-field format must own its label policy: with the project's
    # prompt_channel_labels toggle off, deferring to it would emit prose soup.
    for template_id in ("minimax_h3_base", "minimax_h3_ref"):
        template = _template(template_id)
        assert template["labels"] == "always"


def test_minimax_field_separator_is_a_blank_line():
    for template_id in ("minimax_h3_base", "minimax_h3_ref"):
        assert _template(template_id)["field_separator"] == "\n\n"


def test_minimax_shot_marker_channel_matches_each_guide_main_field():
    # Base mode carries shots in integrated_multimodal_description; full
    # reference mode renames that field to detailed_description (ref guide 5.2).
    assert _template("minimax_h3_base")["shot_marker_channel"] == (
        "integrated_multimodal_description")
    assert _template("minimax_h3_ref")["shot_marker_channel"] == (
        "detailed_description")


def test_default_template_reproduces_today_three_channels():
    # The `sonder` preset is the default and must still describe exactly the
    # channel set the firewall file pins.
    template = _template("sonder")
    assert [channel["key"] for channel in template["channels"]] == list(pp.CHANNEL_ORDER)
    assert [channel["label"] for channel in template["channels"]] == [
        pp.CHANNEL_LABELS[key] for key in pp.CHANNEL_ORDER
    ]
    assert template["field_separator"] == " "


def test_standard_template_is_a_single_channel():
    template = _template("standard")
    assert len(template["channels"]) == 1


def test_every_channel_declares_authoring_guidance():
    module = _templates()
    presets = getattr(module, "PROMPT_CHANNEL_TEMPLATE_PRESETS")
    assert set(presets) >= {"standard", "sonder", "minimax_h3_base", "minimax_h3_ref"}
    labels_never = getattr(module, "LABELS_NEVER")
    for template_id, template in presets.items():
        assert template["channels"], template_id
        for channel in template["channels"]:
            assert channel["key"], template_id
            # A template that never emits labels may leave them blank; any
            # template that can emit them must name every channel.
            if template["labels"] != labels_never:
                assert channel["label"], (template_id, channel["key"])
            assert channel["description"].strip(), (template_id, channel["key"])


# --- composition --------------------------------------------------------------

def test_full_window_composes_the_six_named_fields():
    template = _template("minimax_h3_ref")
    assert _compose(_scenario(), 0, 360, template) == FULL_WINDOW_EXPECTED


def test_section_three_only_window_drops_earlier_sections_and_empty_fields():
    template = _template("minimax_h3_ref")
    assert _compose(_scenario(), 240, 360, template) == SECTION_THREE_EXPECTED


def test_full_window_and_section_window_differ():
    # Anti-vacuity: a composer that ignored the window would satisfy neither.
    assert FULL_WINDOW_EXPECTED != SECTION_THREE_EXPECTED
    assert "[Shot 2]" in FULL_WINDOW_EXPECTED
    assert "[Shot 2]" not in SECTION_THREE_EXPECTED
    assert "summary:" not in SECTION_THREE_EXPECTED
    assert "subject_definitions:" not in SECTION_THREE_EXPECTED


def _body(composed):
    """The detailed_description field alone — the shot-marker channel.

    Marker assertions must not read the other fields: retention_analysis
    legitimately mentions `[Shot 1]` as authored prose.
    """
    field = "detailed_description:\n"
    return composed.split(field, 1)[1].split("\n\n", 1)[0] if field in composed else ""


def test_first_shot_carries_no_timestamp_unless_its_section_asks():
    # The base guide says not to stamp the first shot, so an unflagged section
    # never does. Section 1 leads this window and does not ask.
    template = _template("minimax_h3_ref")
    assert "[Shot 1] At " not in _compose(_scenario(), 0, 360, template)


def test_first_shot_may_stamp_zero_when_its_section_asks():
    # Section 3 sets shot_timestamp, so leading the window stamps 00:00.000
    # rather than silently dropping the request.
    template = _template("minimax_h3_ref")
    composed = _compose(_scenario(), 240, 360, template)
    assert "[Shot 1] At 00:00.000," in composed


def test_a_section_may_stamp_a_cut_time_without_opening_a_shot():
    # Prompt 1: "[Shot 1] the man approaches" / Prompt 2: "At 00:05.000,
    # suddenly he takes out a gun" — one shot, two timed beats.
    template = _template("minimax_h3_ref")
    sections = _scenario()
    setattr(sections[1], "shot_timestamp", True)
    composed = _compose(sections, 0, 360, template)
    assert "At 00:05.000, she lifts her gaze" in composed.replace("She lifts", "she lifts")
    assert "[Shot 2] At 00:05.000," not in composed


def test_no_section_opening_a_shot_emits_no_markers():
    template = _template("minimax_h3_ref")
    sections = _scenario()
    for section in sections:
        setattr(section, "starts_new_shot", False)
        setattr(section, "shot_timestamp", False)
    body = _body(_compose(sections, 0, 360, template))
    assert body
    assert "[Shot " not in body
    assert "At 0" not in body


def test_cut_timestamp_is_window_relative_not_scene_relative():
    # Window [120,360): section 3 still opens a shot, but 120 frames later than
    # the scene start — 120 window-local frames at 24fps is 00:05.000, NOT the
    # 00:10.000 its scene position would give.
    #
    # It renumbers to [Shot 1] because section 1's shot is outside the window:
    # numbering is dense over the EFFECTIVE set, which is what stops a chunked
    # render from telling the model about a [Shot 7] it cannot see.
    template = _template("minimax_h3_ref")
    composed = _compose(_scenario(), 120, 360, template)
    assert "[Shot 1] At 00:05.000," in composed
    assert "00:10.000" not in composed


def test_timecode_format_is_mm_ss_mmm_with_unwrapped_minutes():
    module = _templates()
    fmt = getattr(module, "format_shot_timecode")
    assert fmt(0, 24.0) == "00:00.000"
    assert fmt(240, 24.0) == "00:10.000"
    assert fmt(84, 24.0) == "00:03.500"
    # Minutes are never wrapped into hours.
    assert fmt(24 * 60 * 75, 24.0) == "75:00.000"


def test_labels_off_project_toggle_cannot_strip_minimax_field_names():
    # The project's prompt_channel_labels toggle defaults to False at every read
    # site. A named-field template must ignore it.
    template = _template("minimax_h3_ref")
    with_labels = _compose(_scenario(), 0, 360, template)
    assert with_labels == FULL_WINDOW_EXPECTED
    assert "detailed_description:" in with_labels


def test_empty_channels_are_omitted_not_written_as_na():
    # Plan decision 3, recorded as a known deviation: MiniMax documents `N/A`
    # for an absent overall_soundscape / non_diegetic_music, but v1 omits empty
    # channels to match compose_section_text. Revisit after real generations.
    template = _template("minimax_h3_ref")
    composed = _compose(_scenario(), 240, 360, template)
    assert "N/A" not in composed
    assert "overall_soundscape" not in composed


# --- section model ------------------------------------------------------------

def test_six_channel_section_round_trips_without_truncation():
    # PromptSection has no project metadata to consult, so it normalizes
    # against the legacy key set and PRESERVES everything else. A six-channel
    # section therefore carries the six authored keys plus three empty legacy
    # ones — superset, never truncation. Those empty keys are inert in
    # composition and make switching a template away and back lossless.
    keys = [channel["key"] for channel in _template("minimax_h3_ref")["channels"]]
    authored = {key: f"text for {key}" for key in keys}
    section = PromptSection(0, 120, channels=authored)
    assert set(section.channels) >= set(keys)
    assert {key: section.channels[key] for key in keys} == authored

    restored = PromptSection.from_dict(section.to_dict())
    assert restored.channels == section.channels
    assert restored == section


def test_starts_new_shot_and_subject_ids_round_trip():
    section = PromptSection(0, 120, channels={"detailed_description": "x"})
    setattr(section, "starts_new_shot", True)
    setattr(section, "subject_ids", [{"entity_id": "abc123", "retention": "fully_preserved"}])

    data = section.to_dict()
    assert data["starts_new_shot"] is True
    assert data["subject_ids"] == [{"entity_id": "abc123", "retention": "fully_preserved"}]

    restored = PromptSection.from_dict(data)
    assert getattr(restored, "starts_new_shot") is True
    assert getattr(restored, "subject_ids") == [
        {"entity_id": "abc123", "retention": "fully_preserved"}]
    assert restored == section


def test_pre_upgrade_section_dict_defaults_both_new_fields():
    restored = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 120,
        "channels": {"visual": "old", "speech": "", "sounds": ""},
    })
    assert getattr(restored, "starts_new_shot") is False
    assert getattr(restored, "subject_ids") == []


def test_subject_ids_normalizer_dedupes_and_keeps_authored_order():
    # One normalizer shared by the model, the routes and the identity check.
    # Authored order survives because it is the tie-break when one section
    # binds several subjects; duplicates collapse to the first occurrence.
    normalize = getattr(pp, "normalize_subject_ids")
    assert normalize(None) == []
    assert [entry["entity_id"] for entry in
            normalize([{"entity_id": "b"}, {"entity_id": "a"}, {"entity_id": "b"}])] == ["b", "a"]
    # A binding is an OBJECT. There is no bare-string form to accept: retention
    # shipped in the same commit as subject_ids, so accepting one would only
    # revive junk such as the "[object Object]" left in dev browser storage.
    assert normalize(["a", "[object Object]"]) == []
    assert normalize([{"entity_id": "a", "retention": "bogus"}])[0]["retention"] == (
        getattr(pp, "DEFAULT_SUBJECT_RETENTION"))
    # Malformed containers reach this straight from request bodies; never raise.
    for malformed in ({}, {"entity_id": "a"}, 5, "abc", True):
        assert normalize(malformed) == []


def test_subject_ids_identity_ignores_order_and_duplicates():
    # Identity validation compares an order-insensitive projection, so a client
    # that merely reordered bindings cannot provoke a spurious 409.
    identity = getattr(pp, "subject_ids_identity")
    assert identity([{"entity_id": "b"}, {"entity_id": "a"}, {"entity_id": "b"}]) == (
        identity([{"entity_id": "a"}, {"entity_id": "b"}])
    )
