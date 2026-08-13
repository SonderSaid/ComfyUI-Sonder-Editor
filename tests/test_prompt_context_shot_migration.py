"""Prompt-section Shot/Timestamp compatibility and structured identity tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server import prompt_channel_templates as pct
from server import prompt_context as pp_context
from server import prompt_payload as pp
import server.routes as routes
from server.timeline_state import LaneConfig, PromptSection, Scene


SHOT_KEY = "integrated_multimodal_description"


def _section(start=0, end=120, text="a dog walks", **kwargs):
    return PromptSection(start, end, channels={SHOT_KEY: text}, **kwargs)


def _compose(first, second):
    sections = [
        _section(0, 120, starts_new_shot=first[0], shot_timestamp=first[1]),
        _section(120, 240, text="it barks", starts_new_shot=second[0],
                 shot_timestamp=second[1]),
    ]
    return pp.compose_range_prompt(
        "", sections, 0, 240, delimiter=".",
        template=pct.get_channel_template("minimax_h3_base"), fps=24.0)


def test_model_round_trip_keeps_markers_as_attachments():
    section = _section(starts_new_shot=True, shot_timestamp=True)
    restored = PromptSection.from_dict(section.to_dict())
    assert restored.starts_new_shot is True
    assert restored.shot_timestamp is True
    assert [item["kind"] for item in restored.attachments] == ["shot"]
    assert restored.attachments[0]["config"]["timestamp"] is True
    assert restored == section


def test_marker_mutations_persist_one_shot_with_timestamp_option():
    attachments = pp_context.set_marker_attachment([], "timestamp", True)
    assert [value["kind"] for value in attachments] == ["shot"]
    assert attachments[0]["config"]["timestamp"] is True

    attachments = pp_context.set_marker_attachment(attachments, "timestamp", False)
    assert [value["kind"] for value in attachments] == ["shot"]
    assert attachments[0]["config"]["timestamp"] is False

    attachments = pp_context.set_marker_attachment(attachments, "shot", False)
    assert attachments == []


def test_legacy_inline_timestamp_anchor_is_removed_during_shot_migration():
    section = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 24,
        "channels": {SHOT_KEY: "before after"},
        "channel_docs": {SHOT_KEY: {"nodes": [
            {"type": "text", "node_id": "before", "text": "before "},
            {"type": "attachment", "node_id": "stamp",
             "attachment_id": "legacy-time"},
            {"type": "text", "node_id": "after", "text": "after"},
        ]}},
        "attachments": [{"attachment_id": "legacy-time", "kind": "timestamp"}],
    })
    assert section.starts_new_shot is True and section.shot_timestamp is True
    assert [value["kind"] for value in section.attachments] == ["shot"]
    assert all(node.get("attachment_id") != "legacy-time"
               for node in section.channel_docs[SHOT_KEY]["nodes"])
    assert section.channels[SHOT_KEY] == "before after"


def test_legacy_subject_ids_are_ignored_without_conversion():
    restored = PromptSection.from_dict({
        "start_frame": 0,
        "end_frame": 120,
        "channels": {"visual": "old"},
        "subject_ids": [{"entity_id": "retired"}],
    })
    assert not hasattr(restored, "subject_ids")
    assert "subject_ids" not in restored.to_dict()


def test_legacy_timestamp_only_combination_canonicalizes_to_timed_shot():
    composed = {
        "neither": _compose((True, False), (False, False)),
        "shot": _compose((True, False), (True, False)),
        "time": _compose((True, False), (False, True)),
        "both": _compose((True, False), (True, True)),
    }
    assert "walks. it barks" in composed["neither"]
    assert "[Shot 2] it barks" in composed["shot"]
    assert "[Shot 2] At 00:05.000, it barks" in composed["time"]
    assert "[Shot 2] At 00:05.000, it barks" in composed["both"]
    assert composed["time"] == composed["both"]
    assert len(set(composed.values())) == 3


def test_first_section_can_stamp_zero_and_is_not_forced_to_open_shot():
    template = pct.get_channel_template("minimax_h3_base")
    stamped = pp.compose_range_prompt(
        "", [_section(starts_new_shot=True, shot_timestamp=True)], 0, 120,
        template=template, fps=24.0)
    assert "[Shot 1] At 00:00.000, a dog walks" in stamped
    unmarked = pp.compose_range_prompt(
        "", [_section(starts_new_shot=False, shot_timestamp=False)], 0, 120,
        template=template, fps=24.0)
    assert "[Shot " not in unmarked


def test_split_does_not_inherit_markers_and_clones_context():
    attachment = {
        "attachment_id": "a1",
        "emission_group_id": "g1",
        "kind": "guide",
        "source": {"text": "guide"},
    }
    left = _section(0, 240, starts_new_shot=True, shot_timestamp=True,
                    attachments=[attachment])
    scene = Scene(scene_id="scene-1", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = [left]
    right = routes._split_prompt_object(scene, left, 120)
    assert left.starts_new_shot is True and left.shot_timestamp is True
    assert right.starts_new_shot is False and right.shot_timestamp is False
    guides = [item for item in right.attachments if item["kind"] == "guide"]
    assert len(guides) == 1
    assert guides[0]["attachment_id"] != "a1"
    assert guides[0]["emission_group_id"] == "g1"


def test_structured_identity_checks_documents_and_attachments():
    section = _section(channel_docs={
        SHOT_KEY: {
            "document_version": 1,
            "nodes": [{"node_id": "n1", "type": "text", "text": "a dog walks"}],
        }
    })
    routes._validate_prompt_identity(section, section.to_dict())
    changed_doc = section.to_dict()
    changed_doc["channel_docs"][SHOT_KEY]["nodes"][0]["text"] = "different"
    with pytest.raises(routes.ProjectMutationRequestError):
        routes._validate_prompt_identity(section, changed_doc)
    changed_attachment = section.to_dict()
    changed_attachment["attachments"] = [{"kind": "guide", "source": {"text": "x"}}]
    with pytest.raises(routes.ProjectMutationRequestError):
        routes._validate_prompt_identity(section, changed_attachment)
