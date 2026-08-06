"""Durable per-section shot and subject fields (plan P2).

`starts_new_shot` and `subject_ids` follow `muted` through the model, the
composition carry-through, the mutation and identity sites, and the two
whitelist round-trips that would otherwise wipe them on restore.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server import prompt_payload as pp
import server.routes as routes
from server.timeline_state import (
    LaneConfig, PromptSection, ReferenceEntity, Scene, TimelineProject,
)


def _section(start, end, text="a dog walks", **kwargs):
    return PromptSection(start, end, channels={"visual": text}, **kwargs)


# --- model ---------------------------------------------------------------------

def test_round_trip_preserves_both_fields():
    section = _section(0, 120, starts_new_shot=True,
                       subject_ids=[{"entity_id": "ent_a", "retention": "weak_reference"}])
    restored = PromptSection.from_dict(section.to_dict())
    assert restored.starts_new_shot is True
    assert restored.subject_ids == [{"entity_id": "ent_a", "retention": "weak_reference"}]
    assert restored == section


def test_pre_upgrade_dict_lacking_both_keys_defaults_off():
    restored = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 120,
        "channels": {"visual": "old", "speech": "", "sounds": ""},
    })
    assert restored.starts_new_shot is False
    assert restored.subject_ids == []


def test_tolerant_coercion_of_hand_edited_values():
    section = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 120,
        "starts_new_shot": "yes",
        # Bare strings, a duplicate, a blank, and an unknown retention marker.
        "subject_ids": ["ent_a", {"entity_id": "ent_a"}, {"entity_id": ""},
                        {"entity_id": "ent_b", "retention": "made_up"}],
    })
    assert section.starts_new_shot is True
    assert section.subject_ids == [
        {"entity_id": "ent_a", "retention": "fully_preserved"},
        {"entity_id": "ent_b", "retention": "fully_preserved"},
    ]


def test_shot_timestamp_defaults_off_and_round_trips():
    # Defaults OFF: the flag is no longer gated behind starts_new_shot, so an
    # on-by-default would stamp a cut time onto every section.
    assert _section(0, 120).shot_timestamp is False
    assert PromptSection.from_dict({"start_frame": 0, "end_frame": 120}).shot_timestamp is False

    section = _section(0, 120, starts_new_shot=True, shot_timestamp=True)
    restored = PromptSection.from_dict(section.to_dict())
    assert restored.shot_timestamp is True
    assert restored == section


def test_stored_timestamp_on_a_non_shot_section_is_migrated_off():
    # The flag used to be gated behind starts_new_shot and defaulted True, so a
    # section that opened no shot could carry a meaningless True. Ungated, that
    # value would suddenly stamp. Under the old rule it emitted nothing, so
    # dropping it cannot change any existing project's output.
    migrated = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 120,
        "starts_new_shot": False, "shot_timestamp": True,
    })
    assert migrated.shot_timestamp is False
    # A section that DID open a shot keeps its stamp.
    kept = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 120,
        "starts_new_shot": True, "shot_timestamp": True,
    })
    assert kept.shot_timestamp is True


def test_equality_is_sensitive_to_the_timestamp_flag():
    assert _section(0, 120) != _section(0, 120, shot_timestamp=True)


def test_equality_is_sensitive_to_both_fields():
    base = _section(0, 120)
    assert base == _section(0, 120)
    assert base != _section(0, 120, starts_new_shot=True)
    assert base != _section(0, 120, subject_ids=[{"entity_id": "ent_a"}])
    # Retention is part of the binding, so a changed marker is a changed section.
    assert (_section(0, 120, subject_ids=[{"entity_id": "a", "retention": "weak_reference"}])
            != _section(0, 120, subject_ids=[{"entity_id": "a"}]))


# --- composition carry-through ---------------------------------------------------

def test_segments_carry_both_fields_from_objects_and_from_raw_dicts():
    objects = [_section(0, 120, starts_new_shot=True, subject_ids=["ent_a"])]
    raw = [{"start_frame": 0, "end_frame": 120, "channels": {"visual": "a dog walks"},
            "starts_new_shot": True, "subject_ids": ["ent_a"]}]
    for sections in (objects, raw):
        segments = pp.resolve_segments(sections, 0, 120, labels_on=False)
        assert [s["starts_new_shot"] for s in segments] == [True]
        assert [s["subject_ids"] for s in segments] == [
            [{"entity_id": "ent_a", "retention": "fully_preserved"}]]


# --- split ------------------------------------------------------------------------

def _scene_with(section):
    scene = Scene(scene_id="scene-1", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = [section]
    return scene


_SHOT_KEY = "integrated_multimodal_description"


def _compose_two(first, second):
    """Compose two 5s sections under MiniMax base. Each arg is (shot, stamp)."""
    from server import prompt_channel_templates as pct

    sections = [
        PromptSection(0, 120, channels={_SHOT_KEY: "a dog walks"},
                      starts_new_shot=first[0], shot_timestamp=first[1]),
        PromptSection(120, 240, channels={_SHOT_KEY: "it barks"},
                      starts_new_shot=second[0], shot_timestamp=second[1]),
    ]
    return pp.compose_range_prompt(
        "", sections, 0, 240, delimiter=".",
        template=pct.get_channel_template("minimax_h3_base"), fps=24.0)


def test_all_four_shot_and_timestamp_combinations_are_distinct():
    # Opening a shot and stamping a cut time are independent choices; a section
    # may do either, both or neither.
    opened = (True, False)
    composed = {
        "neither": _compose_two(opened, (False, False)),
        "shot": _compose_two(opened, (True, False)),
        "time": _compose_two(opened, (False, True)),
        "both": _compose_two(opened, (True, True)),
    }
    assert "walks. it barks" in composed["neither"]
    assert "[Shot 2] it barks" in composed["shot"]
    assert "At 00:05.000, it barks" in composed["time"]
    assert "[Shot 2] it barks" not in composed["time"]
    assert "[Shot 2] At 00:05.000, it barks" in composed["both"]
    assert len(set(composed.values())) == 4


def test_a_cut_time_without_a_shot_does_not_consume_a_shot_number():
    # Numbering counts shots, not stamps: a timed beat inside shot 1 must not
    # push the next real shot to [Shot 3].
    composed = _compose_two((True, False), (False, True))
    assert "[Shot 2]" not in composed


def test_the_first_section_is_not_forced_to_open_a_shot():
    # Position implies nothing. An unflagged first section emits no marker, and
    # the first section that DOES open one is [Shot 1].
    composed = _compose_two((False, False), (True, False))
    assert composed.count("[Shot ") == 1
    assert "[Shot 1] it barks" in composed


def test_no_section_opening_a_shot_emits_no_markers_at_all():
    composed = _compose_two((False, False), (False, False))
    assert "[Shot " not in composed
    assert "At 0" not in composed


def test_first_section_may_stamp_zero_when_it_asks():
    # Opt-in only: the MiniMax base guide says not to stamp the first shot, so
    # this is never a default — but "at 0:00 the subject appears" is a real
    # thing to write, and the flag must not be silently ignored.
    from server import prompt_channel_templates as pct

    template = pct.get_channel_template("minimax_h3_base")
    sections = [PromptSection(0, 120, channels={_SHOT_KEY: "a dog walks"},
                              starts_new_shot=True, shot_timestamp=True)]
    composed = pp.compose_range_prompt("", sections, 0, 120, template=template, fps=24.0)
    assert "[Shot 1] At 00:00.000, a dog walks" in composed

    # ...and stays absent when it does not ask.
    sections[0].shot_timestamp = False
    bare = pp.compose_range_prompt("", sections, 0, 120, template=template, fps=24.0)
    assert "[Shot 1] a dog walks" in bare
    assert "At " not in bare


def test_split_keeps_the_flag_left_and_copies_subjects_to_both():
    left = _section(0, 240, starts_new_shot=True, subject_ids=["ent_a", "ent_b"])
    scene = _scene_with(left)
    right = routes._split_prompt_object(scene, left, 120)

    assert left.starts_new_shot is True
    # A split is a range operation: inheriting would open two shots from one
    # and shift every later [Shot N].
    assert right.starts_new_shot is False
    assert right.subject_ids == left.subject_ids
    # Copied, never aliased — editing one half must not edit the other.
    assert right.subject_ids is not left.subject_ids
    right.subject_ids[0]["retention"] = "weak_reference"
    assert left.subject_ids[0]["retention"] == "fully_preserved"


# --- identity validation ------------------------------------------------------------

def test_identity_409_on_mismatch_of_either_field():
    section = _section(0, 120, starts_new_shot=True, subject_ids=["ent_a"])
    routes._validate_prompt_identity(section, section.to_dict())  # must not raise

    with pytest.raises(routes.ProjectMutationRequestError):
        routes._validate_prompt_identity(section, {"starts_new_shot": False})
    with pytest.raises(routes.ProjectMutationRequestError):
        # The section does not stamp, so a client claiming it does disagrees.
        routes._validate_prompt_identity(section, {"shot_timestamp": True})
    with pytest.raises(routes.ProjectMutationRequestError):
        routes._validate_prompt_identity(section, {"subject_ids": [{"entity_id": "ent_b"}]})


def test_no_409_when_the_client_omits_the_new_keys():
    # An older client sends the pre-upgrade expected shape; that is agreement
    # about the fields it knows, not a conflict.
    section = _section(0, 120, starts_new_shot=True, subject_ids=["ent_a"])
    routes._validate_prompt_identity(section, {
        "start_frame": 0, "end_frame": 120, "channels": dict(section.channels)})


def test_no_409_when_the_client_merely_reordered_subject_bindings():
    section = _section(0, 120, subject_ids=["ent_a", "ent_b"])
    routes._validate_prompt_identity(section, {
        "subject_ids": [{"entity_id": "ent_b"}, {"entity_id": "ent_a"},
                        {"entity_id": "ent_b"}]})


# --- mutation helpers -----------------------------------------------------------------

def test_create_and_update_carry_both_fields():
    scene = Scene(scene_id="scene-1", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = []
    created = routes._apply_create_prompt_section(scene, {
        "start_frame": 0, "end_frame": 120, "channels": {"visual": "x"},
        "starts_new_shot": True, "shot_timestamp": False, "subject_ids": ["ent_a"],
    })
    assert created.starts_new_shot is True
    assert created.shot_timestamp is False
    assert created.subject_ids == [{"entity_id": "ent_a", "retention": "fully_preserved"}]

    routes._apply_update_prompt_section(scene, 0, {"starts_new_shot": False})
    assert scene.prompt_sections[0].starts_new_shot is False
    # An update that mentions neither key leaves both alone.
    routes._apply_update_prompt_section(scene, 0, {"channels": {"visual": "y"}})
    assert scene.prompt_sections[0].subject_ids == [
        {"entity_id": "ent_a", "retention": "fully_preserved"}]


def test_channel_update_merges_rather_than_replacing():
    # An edit under one template must not delete another template's channels.
    scene = Scene(scene_id="scene-1", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = []
    routes._apply_create_prompt_section(scene, {
        "start_frame": 0, "end_frame": 120,
        "channels": {"visual": "keep me", "detailed_description": "keep me too"},
    })
    routes._apply_update_prompt_section(scene, 0, {"channels": {"visual": "changed"}})
    channels = scene.prompt_sections[0].channels
    assert channels["visual"] == "changed"
    assert channels["detailed_description"] == "keep me too"


# --- history whitelist round-trip -----------------------------------------------------

def test_prompt_history_entry_preserves_both_fields():
    project = TimelineProject(name="Project")
    project.metadata = {}

    class _Job:
        scene_prompt = "global"
        prompt_sections = [{
            "start_frame": 0, "end_frame": 120,
            "channels": {"visual": "a dog walks", "speech": "", "sounds": ""},
            "starts_new_shot": True,
            "shot_timestamp": False,
            "subject_ids": [{"entity_id": "ent_a", "retention": "attribute_transfer"}],
        }]

    routes._record_prompt_history(project, [_Job()])
    entry = project.metadata["prompt_history"][0]["sections"][0]
    # The panel's Apply rewrites the scene from exactly this projection, so a
    # missing key here is silent data loss on restore.
    assert entry["starts_new_shot"] is True
    assert entry["shot_timestamp"] is False
    assert entry["subject_ids"] == [
        {"entity_id": "ent_a", "retention": "attribute_transfer"}]


# --- orphan pruning ---------------------------------------------------------------------

def test_deleting_an_entity_prunes_its_bindings_across_every_scene():
    project = TimelineProject(name="Project")
    entity = ReferenceEntity(name="Chloe")
    project.references = [entity]
    for index in range(2):
        scene = Scene(scene_id=f"scene-{index}", duration_frames=360)
        scene.prompt_track_config = LaneConfig()
        scene.prompt_sections = [
            _section(0, 120, subject_ids=[entity.reference_id, "ent_keep"]),
            _section(120, 240, subject_ids=["ent_keep"]),
        ]
        project.scenes.append(scene)

    affected = routes._prune_prompt_subject_ids(project, {entity.reference_id})
    assert sorted(affected) == ["scene-0", "scene-1"]
    for scene in project.scenes:
        assert [b["entity_id"] for b in scene.prompt_sections[0].subject_ids] == ["ent_keep"]
        assert [b["entity_id"] for b in scene.prompt_sections[1].subject_ids] == ["ent_keep"]


def test_pruning_nothing_touches_nothing():
    project = TimelineProject(name="Project")
    scene = Scene(scene_id="scene-0", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = [_section(0, 120, subject_ids=["ent_keep"])]
    project.scenes.append(scene)
    assert routes._prune_prompt_subject_ids(project, set()) == []
    assert routes._prune_prompt_subject_ids(project, {"ent_other"}) == []
    assert scene.prompt_sections[0].subject_ids == [
        {"entity_id": "ent_keep", "retention": "fully_preserved"}]
