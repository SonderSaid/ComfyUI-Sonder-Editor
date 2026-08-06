"""Immutable regression contract for prompt composition.

Every assertion here describes composition with NO channel template set — the
default three-channel `sonder` behavior that shipped before the channel-template
work. These strings are hardcoded golden values, not recomputations: if a later
phase changes them, an existing project's served prompt changed and the change
is a regression, not a test update.

Do not edit the expected strings in this file. Add new expectations to the
phase-specific test files instead.

Frame ranges are half-open [start, end) throughout.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import prompt_payload as pp
from server.timeline_state import PromptSection


# --- fixture ------------------------------------------------------------------

# One deliberately mixed scenario: every channel is populated somewhere, every
# channel is empty somewhere, and the section lengths (120f each) make the
# boundary-threshold arithmetic easy to read (5f of 120f is ~4.2%).
def _scenario():
    return [
        PromptSection(0, 120, channels={
            "visual": "a dog walks", "speech": "hello there", "sounds": "rain"}),
        PromptSection(120, 240, channels={
            "visual": "it barks", "speech": "", "sounds": "thunder"}),
        PromptSection(240, 360, channels={
            "visual": "the dog runs", "speech": "goodbye", "sounds": ""}),
    ]


# (case label, global text, window start, window end, labels_on, delimiter, threshold)
_CASES = [
    ("full/labels/dot", "cinematic", 0, 360, True, ".", 0.0),
    ("full/labels/comma", "cinematic", 0, 360, True, ",", 0.0),
    ("full/labels/empty-delim", "cinematic", 0, 360, True, "", 0.0),
    ("full/plain/dot", "cinematic", 0, 360, False, ".", 0.0),
    ("full/plain/comma", "cinematic", 0, 360, False, ",", 0.0),
    ("full/plain/empty-delim", "cinematic", 0, 360, False, "", 0.0),
    # A full window has no clipped boundary section, so the threshold is inert.
    ("full/labels/thr10", "cinematic", 0, 360, True, ".", 10.0),
    ("full/plain/thr10", "cinematic", 0, 360, False, ".", 10.0),
    # Clipped window: section 3 covers 5f of its authored 120f (~4.2%).
    ("clipped/labels/thr0", "", 0, 245, True, ".", 0.0),
    ("clipped/labels/thr10", "", 0, 245, True, ".", 10.0),
    ("clipped/plain/thr0", "", 0, 245, False, ".", 0.0),
    ("clipped/plain/thr10", "", 0, 245, False, ".", 10.0),
    # A window sitting inside one section is never emptied by the threshold.
    ("inside-one/labels/thr50", "g", 130, 140, True, ".", 50.0),
    ("no-global/labels/dot", "", 0, 360, True, ".", 0.0),
]

_EXPECTED = [
    ("full/labels/dot",
     "cinematic [VISUAL]: a dog walks. it barks. the dog runs"
     " [SPEECH]: hello there. goodbye [SOUNDS]: rain. thunder"),
    ("full/labels/comma",
     "cinematic [VISUAL]: a dog walks, it barks, the dog runs"
     " [SPEECH]: hello there, goodbye [SOUNDS]: rain, thunder"),
    ("full/labels/empty-delim",
     "cinematic [VISUAL]: a dog walks it barks the dog runs"
     " [SPEECH]: hello there goodbye [SOUNDS]: rain thunder"),
    ("full/plain/dot",
     "cinematic a dog walks hello there rain. it barks thunder."
     " the dog runs goodbye"),
    ("full/plain/comma",
     "cinematic a dog walks hello there rain, it barks thunder,"
     " the dog runs goodbye"),
    ("full/plain/empty-delim",
     "cinematic a dog walks hello there rain it barks thunder"
     " the dog runs goodbye"),
    ("full/labels/thr10",
     "cinematic [VISUAL]: a dog walks. it barks. the dog runs"
     " [SPEECH]: hello there. goodbye [SOUNDS]: rain. thunder"),
    ("full/plain/thr10",
     "cinematic a dog walks hello there rain. it barks thunder."
     " the dog runs goodbye"),
    ("clipped/labels/thr0",
     "[VISUAL]: a dog walks. it barks. the dog runs"
     " [SPEECH]: hello there. goodbye [SOUNDS]: rain. thunder"),
    ("clipped/labels/thr10",
     "[VISUAL]: a dog walks. it barks [SPEECH]: hello there"
     " [SOUNDS]: rain. thunder"),
    ("clipped/plain/thr0",
     "a dog walks hello there rain. it barks thunder. the dog runs goodbye"),
    ("clipped/plain/thr10",
     "a dog walks hello there rain. it barks thunder"),
    ("inside-one/labels/thr50",
     "g [VISUAL]: it barks [SOUNDS]: thunder"),
    ("no-global/labels/dot",
     "[VISUAL]: a dog walks. it barks. the dog runs"
     " [SPEECH]: hello there. goodbye [SOUNDS]: rain. thunder"),
]


# --- composition grid ---------------------------------------------------------

def test_compose_range_prompt_grid_is_byte_identical():
    sections = _scenario()
    produced = [
        (label, pp.compose_range_prompt(global_text, sections, start, end,
                                        labels_on=labels_on, delimiter=delimiter,
                                        boundary_threshold_pct=threshold))
        for (label, global_text, start, end,
             labels_on, delimiter, threshold) in _CASES
    ]
    assert produced == _EXPECTED


def test_grid_covers_both_label_modes_and_is_not_vacuous():
    # Anti-vacuity: the golden table must actually distinguish the knobs it
    # claims to cover, otherwise a composer that ignored them would still pass.
    by_label = dict(_EXPECTED)
    assert len(by_label) == len(_EXPECTED) == len(_CASES)
    assert by_label["full/labels/dot"] != by_label["full/labels/comma"]
    assert by_label["full/labels/dot"] != by_label["full/plain/dot"]
    assert by_label["clipped/labels/thr0"] != by_label["clipped/labels/thr10"]
    assert all(text for _, text in _EXPECTED)


def test_default_delimiter_and_labels_defaults_unchanged():
    # The composer's own defaults are part of the contract: callers that pass
    # nothing must keep getting labels-on with the "." seam.
    sections = _scenario()
    assert pp.DEFAULT_SECTION_DELIMITER == "."
    assert pp.compose_range_prompt("cinematic", sections, 0, 360) == (
        dict(_EXPECTED)["full/labels/dot"]
    )


def test_channel_order_and_labels_unchanged():
    assert pp.CHANNEL_ORDER == ("visual", "speech", "sounds")
    assert pp.CHANNEL_LABELS == {
        "visual": "[VISUAL]:",
        "speech": "[SPEECH]:",
        "sounds": "[SOUNDS]:",
    }


# --- section round-trip -------------------------------------------------------

def test_section_channels_survive_round_trip_without_key_loss():
    # Whatever key set a constructed section ends up holding must survive
    # to_dict/from_dict intact. This is the truncation guard: it stays true
    # for three channels today and for n channels after the template work,
    # so a serializer that coerces to a fixed key set fails here.
    section = PromptSection(0, 120, channels={
        "visual": "a dog walks", "speech": "hello there", "sounds": "rain"})
    assert section.channels  # anti-vacuity — an empty dict would pass trivially

    restored = PromptSection.from_dict(section.to_dict())
    assert set(restored.channels) == set(section.channels)
    assert restored.channels == section.channels
    assert restored == section
    assert restored.prompt == section.prompt


def test_section_round_trip_preserves_identity_fields():
    section = PromptSection(30, 90, channels={"visual": "v", "speech": "s", "sounds": ""},
                            muted=True)
    restored = PromptSection.from_dict(section.to_dict())
    assert restored.prompt_id == section.prompt_id
    assert restored.start_frame == 30
    assert restored.end_frame == 90
    assert restored.muted is True


def test_derived_prompt_mirror_is_label_free():
    section = PromptSection(0, 10, channels={
        "visual": "a dog walks", "speech": "hello there", "sounds": "rain"})
    assert section.prompt == "a dog walks hello there rain"
    assert "[" not in section.prompt


def test_identity_validation_accepts_own_snapshot():
    # `_validate_prompt_identity` must accept a section's own to_dict() output.
    # It reads the derived `.prompt` mirror, so any truncation in that mirror
    # turns ordinary edits into spurious identity_mismatch 409s.
    import server.routes as routes

    section = PromptSection(0, 120, channels={
        "visual": "a dog walks", "speech": "hello there", "sounds": "rain"})
    routes._validate_prompt_identity(section, section.to_dict())  # must not raise
