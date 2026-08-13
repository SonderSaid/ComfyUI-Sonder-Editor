"""Channelized scene-global prompt (batch step 6, plan P5).

`Scene.global_channels` is the source of truth; `Scene.prompt` is a derived
label-free mirror kept for the relay socket and older readers. The mirror is
what makes this phase small — most `scene.prompt` readers never learn that
anything changed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server import prompt_channel_templates as pct
from server import prompt_payload as pp
import server.routes as routes
from server import prompt_context
from server.timeline_state import LaneConfig, PromptSection, Scene, TimelineProject


REF = pct.get_channel_template("minimax_h3_ref")
SONDER = pct.get_channel_template("sonder")


def _scene(**kwargs):
    scene = Scene(scene_id="scene-1", duration_frames=360, **kwargs)
    scene.prompt_track_config = LaneConfig()
    scene.global_prompt_track_config = LaneConfig()
    return scene


# --- model ------------------------------------------------------------------

def test_legacy_scene_seeds_channels_from_the_flat_prompt():
    scene = _scene(prompt="a cinematic look")
    assert scene.global_channels["visual"] == "a cinematic look"
    assert scene.prompt == "a cinematic look"


def test_mirror_is_derived_from_channels():
    scene = _scene()
    scene.set_global_channels({"visual": "a", "speech": "b"})
    assert scene.prompt == "a b"


def test_setting_the_flat_prompt_is_destructive():
    # Mirrors PromptSection.prompt's setter. `api_update_scene` writes
    # body["prompt"] directly, so without this an ordinary scene update would
    # silently wipe global channels 2..n.
    scene = _scene()
    scene.set_global_channels({"visual": "v", "speech": "s", "sounds": "n"})
    scene.set_global_prompt("replacement")
    assert scene.global_channels["visual"] == "replacement"
    assert scene.global_channels["speech"] == ""
    assert scene.global_channels["sounds"] == ""


def test_channel_updates_merge_rather_than_replace():
    scene = _scene()
    scene.set_global_channels({"visual": "keep", "detailed_description": "also keep"})
    scene.set_global_channels({"visual": "changed"})
    assert scene.global_channels["visual"] == "changed"
    assert scene.global_channels["detailed_description"] == "also keep"


def test_structured_global_documents_win_over_derived_flat_mirrors():
    scene = _scene()
    document = prompt_context.text_document("structured")
    routes._apply_scene_fields(None, scene, {
        "global_channels": {"visual": "stale mirror"},
        "global_channel_docs": {"visual": document},
    })
    assert scene.global_channels["visual"] == "structured"
    assert scene.global_channel_docs["visual"] == document


def test_flat_global_replacement_of_anchored_document_is_controlled_conflict():
    scene = _scene()
    attachment = prompt_context.normalize_attachment({"kind": "shot"})
    scene.global_attachments = [attachment]
    scene.set_global_channel_documents({"visual": {"nodes": [
        {"type": "text", "node_id": "text", "text": "hello"},
        {"type": "attachment", "node_id": "anchor",
         "attachment_id": attachment["attachment_id"]},
    ]}})
    with pytest.raises(routes.ProjectMutationRequestError) as conflict:
        routes._apply_scene_fields(None, scene, {
            "global_channels": {"visual": "flat replacement"},
        })
    assert (conflict.value.status, conflict.value.code) == (
        409, "structured_edit_conflict")


def test_flat_section_replacement_of_anchor_is_controlled_and_atomic():
    attachment = prompt_context.normalize_attachment({"kind": "guide"})
    section = PromptSection(
        0, 20, channels={"visual": "before"}, attachments=[attachment],
        channel_docs={"visual": {"nodes": [
            {"type": "text", "node_id": "text", "text": "before"},
            {"type": "attachment", "node_id": "anchor",
             "attachment_id": attachment["attachment_id"]},
        ]}},
    )
    scene = _scene()
    scene.prompt_sections = [section]
    before = section.to_dict()
    with pytest.raises(routes.ProjectMutationRequestError) as conflict:
        routes._apply_update_prompt_section(
            scene, 0, {"channels": {"visual": "flat replacement"}})
    assert (conflict.value.status, conflict.value.code) == (
        409, "structured_edit_conflict")
    assert section.to_dict() == before


def test_round_trip_preserves_channels_and_section_exceptions():
    scene = _scene()
    scene.set_global_channels({"detailed_description": "a style opening"})
    scene.prompt_sections = [PromptSection(
        0, 120, channels={"detailed_description": "x"},
        global_channel_exceptions=["detailed_description"])]
    restored = Scene.from_dict(scene.to_dict())
    assert restored.global_channels["detailed_description"] == "a style opening"
    assert restored.prompt_sections[0].global_channel_exceptions == ["detailed_description"]


def test_to_dict_derives_the_mirror_rather_than_trusting_the_field():
    # A stray direct assignment must not be persisted out of step with the
    # channels that actually compose.
    scene = _scene()
    scene.set_global_channels({"visual": "real text"})
    scene.prompt = "a lie"
    assert scene.to_dict()["prompt"] == "real text"


def test_inherit_defaults_on_and_only_named_channels_opt_out():
    # An EXCEPTIONS set, so a channel the section never mentions — including one
    # added to the template later — is inherited without a per-section write.
    section = PromptSection(0, 120, global_channel_exceptions=["speech"])
    assert pp.section_inherits_global(section, "visual") is True
    assert pp.section_inherits_global(section, "speech") is False
    assert pp.section_inherits_global(section, "added_later") is True
    # Order-insensitive and de-duplicated: this is a set, not a list.
    assert PromptSection(
        0, 120, global_channel_exceptions=["b", "a", "b", "", None]
    ).global_channel_exceptions == ["a", "b"]


# --- composition ------------------------------------------------------------

def _sections():
    # Opens a shot explicitly: nothing about being first implies one.
    return [PromptSection(0, 120, starts_new_shot=True, channels={
        "detailed_description": "he opens the shutters"})]


def test_global_channel_merges_into_its_own_field():
    # The MiniMax full-reference guide puts the style opening BEFORE [Shot 1]
    # inside detailed_description — not ahead of the first field name, which is
    # where the pre-P5 interim rule put it.
    composed = pp.compose_range_prompt(
        "", _sections(), 0, 120, delimiter=".", template=REF, fps=24.0,
        global_channels={"detailed_description": "The target video is cinematic"})
    assert composed == (
        "detailed_description:\n"
        "The target video is cinematic. [Shot 1] he opens the shutters")
    assert not composed.startswith("The target video")


def test_a_global_channel_with_no_section_text_still_composes():
    composed = pp.compose_range_prompt(
        "", [], 0, 120, template=REF, global_channels={"summary": "a summary"})
    assert composed == "summary:\na summary"


def test_uninherited_global_channel_is_absent():
    scene = _scene()
    scene.prompt_sections = _sections()
    scene.prompt_sections[0].global_channel_exceptions = ["detailed_description"]
    scene.set_global_channels({"detailed_description": "The target video is cinematic"})
    composed = scene.get_prompt_for_range(0, 120, delimiter=".", template=REF, fps=24.0)
    assert "cinematic" not in composed
    assert "he opens the shutters" in composed


def test_global_channel_is_emitted_once_when_any_section_inherits():
    # Emit-once-if-any: repeating it per inheriting section would print a style
    # opening several times over.
    scene = _scene()
    scene.prompt_sections = [
        PromptSection(0, 120, starts_new_shot=True,
                      channels={"detailed_description": "he opens the shutters"}),
        PromptSection(120, 240, channels={"detailed_description": "he steps back"}),
    ]
    scene.set_global_channels({"detailed_description": "The target video is cinematic"})
    composed = scene.get_prompt_for_range(0, 240, delimiter=".", template=REF, fps=24.0)
    assert composed.count("cinematic") == 1
    assert composed == ("detailed_description:\n"
                        "The target video is cinematic."
                        " [Shot 1] he opens the shutters. he steps back")


def test_one_opted_out_section_does_not_drop_the_global_for_the_others():
    scene = _scene()
    scene.prompt_sections = [
        PromptSection(0, 120, channels={"detailed_description": "he opens the shutters"},
                      global_channel_exceptions=["detailed_description"]),
        PromptSection(120, 240, channels={"detailed_description": "he steps back"}),
    ]
    scene.set_global_channels({"detailed_description": "The target video is cinematic"})
    # Whole scene: section 2 still inherits, so the global appears once.
    whole = scene.get_prompt_for_range(0, 240, delimiter=".", template=REF, fps=24.0)
    assert whole.count("cinematic") == 1
    # Section 1 alone: the only section in the window opted out, so it is gone.
    alone = scene.get_prompt_for_range(0, 120, delimiter=".", template=REF, fps=24.0)
    assert "cinematic" not in alone
    assert "he opens the shutters" in alone


def test_leading_merge_also_honors_the_section_opt_out():
    # Global channels OFF (or a `leading` template): one global text ahead of
    # everything, opted out under the template's first channel key.
    scene = _scene()
    scene.prompt_sections = [PromptSection(
        0, 120, channels={"visual": "a dog walks"},
        global_channel_exceptions=["visual"])]
    scene.set_global_channels({"visual": "cinematic"})
    composed = scene.get_prompt_for_range(0, 120, labels_on=True, template=SONDER)
    assert composed == "[VISUAL]: a dog walks"


def test_hidden_global_lane_still_zeroes_every_channel():
    scene = _scene()
    scene.prompt_sections = _sections()
    scene.set_global_channels({"detailed_description": "The target video is cinematic"})
    scene.global_prompt_track_config = LaneConfig(hidden=True)
    composed = scene.get_prompt_for_range(0, 120, template=REF)
    assert "cinematic" not in composed


def test_default_template_still_prepends_global_once():
    # `sonder` keeps GLOBAL_MERGE_LEADING, which is what the firewall pins.
    scene = _scene(prompt="cinematic")
    scene.prompt_sections = [PromptSection(0, 120, channels={"visual": "a dog walks"})]
    composed = scene.get_prompt_for_range(0, 120, labels_on=True, template=SONDER)
    assert composed == "cinematic [VISUAL]: a dog walks"


# --- frozen job -------------------------------------------------------------

def _job_with(global_channels):
    from server.timeline_state import GenerationJob

    job = GenerationJob()
    job.selection_start = 0
    job.selection_end = 120
    job.scene_prompt = "the flat mirror"
    job.prompt_sections = [{"start_frame": 0, "end_frame": 120,
                            "starts_new_shot": True,
                            "channels": {"detailed_description": "he opens the shutters"}}]
    job.params = {"snapshot_version": 1,
                  pct.PROJECT_TEMPLATE_KEY: "minimax_h3_ref",
                  "prompt_frame_threshold": 0.0,
                  "scene_global_channels": global_channels}
    return job


def test_frozen_job_composes_from_its_frozen_global_channels():
    project = TimelineProject(name="Project")
    project.metadata = {pct.PROJECT_TEMPLATE_KEY: "minimax_h3_ref",
                        "prompt_frame_threshold": 0.0}
    job = _job_with({"detailed_description": "The target video is cinematic"})
    routes._compose_frozen_job_prompt(project, job)
    assert "The target video is cinematic. [Shot 1] he opens the shutters" in job.prompt
    # `scene_prompt` keeps carrying the flat mirror, so the relay socket and
    # every existing reader are untouched.
    assert job.scene_prompt == "the flat mirror"


def test_frozen_job_without_global_channels_falls_back_to_the_flat_mirror():
    project = TimelineProject(name="Project")
    project.metadata = {pct.PROJECT_TEMPLATE_KEY: "minimax_h3_ref",
                        "prompt_frame_threshold": 0.0}
    job = _job_with(None)
    job.params.pop("scene_global_channels")
    routes._compose_frozen_job_prompt(project, job)
    # Pre-upgrade snapshot: still inside a field, never ahead of one.
    assert job.prompt.startswith("subject_definitions:\nthe flat mirror")


# --- route --------------------------------------------------------------------

def test_update_scene_fields_prompt_is_destructive_but_channels_merge():
    project = TimelineProject(name="Project")
    scene = _scene()
    scene.set_global_channels({"visual": "v", "speech": "s"})
    project.scenes.append(scene)

    routes._apply_scene_fields(project, scene, {"global_channels": {"visual": "changed"}})
    assert scene.global_channels["speech"] == "s"

    routes._apply_scene_fields(project, scene, {"prompt": "renamed"})
    assert scene.global_channels["speech"] == ""
    assert scene.global_channels["visual"] == "renamed"


def test_update_prompt_section_normalizes_global_exceptions():
    from server.timeline_state import LaneConfig

    project = TimelineProject(name="Project")
    scene = _scene()
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = [PromptSection(0, 120, channels={"visual": "x"})]
    project.scenes.append(scene)
    routes._apply_update_prompt_section(
        scene, 0, {"global_channel_exceptions": ["speech", "visual", "speech", ""]})
    assert scene.prompt_sections[0].global_channel_exceptions == ["speech", "visual"]


def test_section_global_exceptions_are_identity_checked():
    import pytest

    section = PromptSection(0, 120, channels={"visual": "x"},
                            global_channel_exceptions=["speech"])
    # Order-insensitive agreement is not a conflict.
    routes._validate_prompt_identity(section, {"global_channel_exceptions": ["speech"]})
    with pytest.raises(routes.ProjectMutationRequestError):
        routes._validate_prompt_identity(section, {"global_channel_exceptions": ["visual"]})


def test_writing_tool_apply_carries_global_channels_not_the_flat_mirror():
    """Apply rewrites SECTIONS; the global prompt must survive it untouched.

    Passing only `global` sent `Scene.prompt` — the label-free mirror over the
    LEGACY three channels, which reads empty on any other template — into
    `set_global_prompt`, and that setter is destructive by design: it routes
    text into channel 1 and clears 2..n. So Apply silently erased the whole
    global bag on MiniMax, and flattened visual+speech+sounds into visual on the
    legacy set. The fix is to hand back the channels themselves.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    panel = (root / "web" / "js" / "editor_prompt_panel.js").read_text(encoding="utf-8")
    widget = (root / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")

    apply_call = panel.split("await host._applyPromptSetup({", 1)[1].split("});", 1)[0]
    assert "global_channels: { ...(host.activeScene?.global_channels || {}) }" in apply_call
    # Tagged with the template it was authored under so no retarget is attempted.
    assert "source_channel_template_id: host._channelTemplate().id" in apply_call
    assert "source_channel_template: host._channelTemplate()" in apply_call

    # And a same-template apply must not round-trip through the collapse: that
    # path folds keys outside the template into channel 1 with a literal `key:`
    # label, which is right when retargeting and lossy when there is nothing to
    # retarget.
    assert "channelTemplateKeySetsEqual(" in widget
    same_template_branch = widget.split("if (!sameChannelTemplate)", 1)[1].split("};", 1)[0]
    assert "retargetChannelDocuments(" in same_template_branch
    assert "normalizePromptDocument(" in same_template_branch


def test_set_global_prompt_is_still_destructive_so_the_flat_path_stays_correct():
    """The flat field is not a bug — it is the right restore for pre-channel
    entries. The bug was a modern call site reaching for it."""
    scene = _scene()
    scene.global_channels = {"visual": "v", "speech": "s", "sounds": "x"}
    scene.set_global_prompt("only this")
    assert scene.global_channels["visual"] == "only this"
    assert scene.global_channels["speech"] == ""
    assert scene.global_channels["sounds"] == ""
