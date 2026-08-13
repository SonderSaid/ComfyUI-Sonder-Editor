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


def _section(start=0, end=120, text="a dog walks", *, shot=False,
             timestamp=False, attachments=None, **kwargs):
    authored = list(attachments or [])
    if shot:
        authored.append(pp_context.shot_attachment(timestamp=timestamp))
    elif timestamp:
        authored.append(pp_context.timestamp_attachment())
    return PromptSection(start, end, channels={SHOT_KEY: text},
                         attachments=authored, **kwargs)


def _compose(first, second):
    sections = [
        _section(0, 120, shot=first[0], timestamp=first[1]),
        _section(120, 240, text="it barks", shot=second[0],
                 timestamp=second[1]),
    ]
    compiled = pp_context.compile_prompt_context(
        sections=sections, window_start=0, window_end=240, delimiter=".",
        template=pct.get_channel_template("minimax_h3_base"), fps=24.0)
    assert compiled["errors"] == []
    return compiled["prompt"]


def test_model_round_trip_keeps_canonical_shot_attachment_only():
    section = _section(shot=True, timestamp=True)
    restored = PromptSection.from_dict(section.to_dict())
    assert not hasattr(restored, "starts_new_shot")
    assert not hasattr(restored, "shot_timestamp")
    assert [item["kind"] for item in restored.attachments] == ["shot"]
    assert restored.attachments[0]["config"]["timestamp"] is True
    assert restored == section


def test_shot_and_standalone_time_are_explicit_canonical_attachments():
    shot = pp_context.shot_attachment(timestamp=False)
    timed_shot = pp_context.shot_attachment(timestamp=True)
    standalone = pp_context.timestamp_attachment()
    assert shot["kind"] == "shot" and shot["config"]["timestamp"] is False
    assert timed_shot["kind"] == "shot" and timed_shot["config"]["timestamp"] is True
    assert standalone["kind"] == "timestamp"
    assert standalone["config"]["standalone"] is True


def test_inline_standalone_time_anchor_is_preserved_without_marker_migration():
    section = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 24,
        "channels": {SHOT_KEY: "before after"},
        "channel_docs": {SHOT_KEY: {"nodes": [
            {"type": "text", "node_id": "before", "text": "before "},
            {"type": "attachment", "node_id": "stamp",
             "attachment_id": "legacy-time"},
            {"type": "text", "node_id": "after", "text": "after"},
        ]}},
        "attachments": [{"attachment_id": "legacy-time", "kind": "timestamp",
                         "config": {"standalone": True}}],
    })
    assert [value["kind"] for value in section.attachments] == ["timestamp"]
    assert any(node.get("attachment_id") == "legacy-time"
               for node in section.channel_docs[SHOT_KEY]["nodes"])
    assert section.channels[SHOT_KEY] == "before after"


def test_legacy_subject_ids_and_marker_booleans_are_ignored_without_conversion():
    restored = PromptSection.from_dict({
        "start_frame": 0,
        "end_frame": 120,
        "channels": {"visual": "old"},
        "subject_ids": [{"entity_id": "retired"}],
        "starts_new_shot": True,
        "shot_timestamp": True,
    })
    assert not hasattr(restored, "subject_ids")
    assert not hasattr(restored, "starts_new_shot")
    saved = restored.to_dict()
    assert "subject_ids" not in saved
    assert "starts_new_shot" not in saved and "shot_timestamp" not in saved
    assert saved["attachments"] == []


def test_shot_and_time_attachment_combinations_compile_independently():
    composed = {
        "neither": _compose((True, False), (False, False)),
        "shot": _compose((True, False), (True, False)),
        "time": _compose((True, False), (False, True)),
        "both": _compose((True, False), (True, True)),
    }
    assert "walks. it barks" in composed["neither"]
    assert "[Shot 2] it barks" in composed["shot"]
    assert "At 00:05.000, it barks" in composed["time"]
    assert "[Shot 2] At 00:05.000, it barks" in composed["both"]
    assert len(set(composed.values())) == 4


def test_first_section_can_stamp_zero_and_is_not_forced_to_open_shot():
    template = pct.get_channel_template("minimax_h3_base")
    stamped = pp_context.compile_prompt_context(
        sections=[_section(shot=True, timestamp=True)], window_start=0,
        window_end=120, template=template, fps=24.0)["prompt"]
    assert "[Shot 1] At 00:00.000, a dog walks" in stamped
    unmarked = pp_context.compile_prompt_context(
        sections=[_section()], window_start=0, window_end=120,
        template=template, fps=24.0)["prompt"]
    assert "[Shot " not in unmarked


def test_split_keeps_markers_left_and_clones_context():
    attachment = {
        "attachment_id": "a1",
        "emission_group_id": "g1",
        "kind": "custom",
        "source": {"text": "context"},
    }
    left = _section(0, 240, shot=True, timestamp=True,
                    attachments=[attachment])
    scene = Scene(scene_id="scene-1", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = [left]
    right = routes._split_prompt_object(scene, left, 120)
    assert [item["kind"] for item in left.attachments] == ["custom", "shot"]
    cloned = [item for item in right.attachments if item["kind"] == "custom"]
    assert len(cloned) == 1
    assert cloned[0]["attachment_id"] != "a1"
    assert cloned[0]["emission_group_id"] == "g1"


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
