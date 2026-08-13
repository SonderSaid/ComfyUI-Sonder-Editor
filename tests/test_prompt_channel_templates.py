"""Channel-template catalog, resolution, and job-freeze coverage (plan P1).

The byte-for-byte proof that existing projects are unaffected lives in
tests/test_prompt_composition_firewall.py; this file covers what the template
layer ADDS.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import prompt_channel_templates as pct
from server import prompt_context
from server import prompt_payload as pp
from server.timeline_state import GenerationJob, PromptSection, Scene, TimelineProject


# --- catalog ------------------------------------------------------------------

def test_unknown_template_id_falls_back_to_default():
    # A project saved against a custom template that was later deleted must
    # still compose rather than raising mid-render.
    assert pct.get_channel_template("no-such-template")["id"] == pct.DEFAULT_CHANNEL_TEMPLATE_ID
    assert pct.get_channel_template("")["id"] == pct.DEFAULT_CHANNEL_TEMPLATE_ID
    assert pct.get_channel_template(None)["id"] == pct.DEFAULT_CHANNEL_TEMPLATE_ID


def test_channel_keys_are_unique_within_every_preset():
    for template_id, template in pct.PROMPT_CHANNEL_TEMPLATE_PRESETS.items():
        keys = [channel["key"] for channel in template["channels"]]
        assert len(keys) == len(set(keys)), template_id


def test_shot_marker_channel_names_a_real_channel():
    for template_id, template in pct.PROMPT_CHANNEL_TEMPLATE_PRESETS.items():
        marker = template["shot_marker_channel"]
        if marker:
            assert marker in pct.template_channel_keys(template), template_id


def test_label_policy_resolution():
    minimax = pct.get_channel_template("minimax_h3_ref")
    assert pct.template_labels_on(minimax, False) is True
    assert pct.template_labels_on(minimax, True) is True

    standard = pct.get_channel_template("standard")
    assert pct.template_labels_on(standard, True) is False

    sonder = pct.get_channel_template("sonder")
    assert pct.template_labels_on(sonder, True) is True
    assert pct.template_labels_on(sonder, False) is True


# --- custom (project-owned) templates -------------------------------------------

def test_normalize_custom_template_drops_blank_and_duplicate_keys():
    template = pct.normalize_channel_template({
        "id": "custom:mine", "name": "Mine",
        "channels": [
            {"key": "a", "label": "A"},
            {"key": "", "label": "ignored"},
            {"key": "a", "label": "duplicate"},
            "not a dict",
            {"key": "b", "label": "B"},
        ],
        "field_separator": "\n",
    })
    assert pct.template_channel_keys(template) == ("a", "b")
    assert template["builtin"] is False


def test_normalize_custom_template_with_no_usable_channels_falls_back():
    fallback = pct.normalize_channel_template({"id": "x", "channels": []})
    assert fallback["id"] == pct.DEFAULT_CHANNEL_TEMPLATE_ID


def test_normalize_custom_template_rejects_dangling_marker_and_policies():
    template = pct.normalize_channel_template({
        "id": "custom:mine",
        "channels": [{"key": "a", "label": "A"}],
        "shot_marker_channel": "not_a_channel",
        "labels": "sometimes",
        "global_merge": "somehow",
    })
    assert template["shot_marker_channel"] == ""
    assert template["labels"] == pct.LABELS_PROJECT
    assert template["global_merge"] == pct.GLOBAL_MERGE_LEADING


# --- resolution and freeze ------------------------------------------------------

def test_frozen_params_win_over_live_project_metadata():
    metadata = {pct.PROJECT_TEMPLATE_KEY: "minimax_h3_base"}
    params = {pct.PROJECT_TEMPLATE_KEY: "minimax_h3_ref"}
    assert pct.resolve_channel_template(metadata, params)["id"] == "minimax_h3_ref"
    assert pct.resolve_channel_template(metadata, {})["id"] == "minimax_h3_base"
    assert pct.resolve_channel_template({}, {})["id"] == pct.DEFAULT_CHANNEL_TEMPLATE_ID


def test_project_and_job_projections_are_deliberately_different():
    assert pct.project_template_value("minimax_h3_ref") == "minimax_h3_ref"
    builtin_frozen = pct.template_freeze_value("minimax_h3_ref")
    assert isinstance(builtin_frozen, dict)
    assert builtin_frozen["id"] == "minimax_h3_ref"
    fork = pct.normalize_channel_template({
        "id": "custom:mine", "name": "Mine",
        "channels": [{"key": "a", "label": "A", "description": "d"}],
    })
    frozen = pct.template_freeze_value(fork)
    # A fork must freeze whole: the project could edit or delete it before the
    # job ever runs, and an id alone would then resolve to something else.
    assert isinstance(frozen, dict)
    assert pct.template_channel_keys(pct.get_channel_template(frozen)) == ("a",)
    assert pct.project_template_value(fork) == frozen


def _job_with_sections(**params):
    job = GenerationJob(scene_id="scene-1")
    job.selection_start = 0
    job.selection_end = 360
    job.scene_prompt = ""
    job.prompt_sections = [
        {"start_frame": 0, "end_frame": 180,
         "channels": {"integrated_multimodal_description": "a dog walks",
                      "overall_soundscape": "rain"}},
        {"start_frame": 180, "end_frame": 360,
         "channels": {"integrated_multimodal_description": "it barks"}},
    ]
    job.params = {"snapshot_version": 1,
                  "prompt_context_format": "prompt_context_v1", **params}
    return job


def _project_with_job_sections(template="minimax_h3_base"):
    scene = Scene(scene_id="scene-1", duration_frames=360, prompt_sections=[
        PromptSection(0, 180, channels={
            "integrated_multimodal_description": "a dog walks",
            "overall_soundscape": "rain"}),
        PromptSection(180, 360, channels={
            "integrated_multimodal_description": "it barks"}),
    ])
    project = TimelineProject(name="Project", scenes=[scene])
    project.metadata = {pct.PROJECT_TEMPLATE_KEY: template,
                        "prompt_frame_threshold": 0.0}
    return project


def test_queued_job_recomposes_identically_after_the_preset_changes(monkeypatch):
    import server.routes as routes

    project = _project_with_job_sections()

    job = _job_with_sections()
    routes._freeze_new_job_channel_template(project, job)
    routes._compose_frozen_job_prompt(project, job)
    frozen_prompt = job.prompt
    # Anti-vacuity: the template must actually be doing something here.
    assert "integrated_multimodal_description:" in frozen_prompt
    assert job.params[pct.PROJECT_TEMPLATE_KEY]["id"] == "minimax_h3_base"

    # Both the project and shipped preset change AFTER the job was queued.
    project.metadata[pct.PROJECT_TEMPLATE_KEY] = "sonder"
    changed = dict(pct.PROMPT_CHANNEL_TEMPLATE_PRESETS["minimax_h3_base"])
    changed["labels"] = pct.LABELS_NEVER
    monkeypatch.setitem(pct.PROMPT_CHANNEL_TEMPLATE_PRESETS, "minimax_h3_base", changed)
    requeued = _job_with_sections(**{
        pct.PROJECT_TEMPLATE_KEY: job.params[pct.PROJECT_TEMPLATE_KEY],
    })
    routes._compose_frozen_job_prompt(project, requeued)
    assert requeued.prompt == frozen_prompt


def test_new_job_freeze_carries_policy_in_the_whole_template():
    import server.routes as routes

    project = _project_with_job_sections("standard")
    project.metadata = {"prompt_channel_labels": True, "prompt_frame_threshold": 0.0}
    job = _job_with_sections(prompt_channel_labels=True)
    routes._freeze_new_job_channel_template(project, job)
    routes._compose_frozen_job_prompt(project, job)
    assert job.params["prompt_channel_labels"] is True
    assert job.params[pct.PROJECT_TEMPLATE_KEY]["id"] == pct.DEFAULT_CHANNEL_TEMPLATE_ID
    assert job.params[pct.PROJECT_TEMPLATE_KEY]["labels"] == pct.LABELS_ALWAYS


def test_legacy_bare_preset_job_keeps_its_frozen_label_toggle_without_mutation():
    preset_before = pct.get_channel_template("sonder")
    params = {
        pct.PROJECT_TEMPLATE_KEY: "sonder",
        "prompt_channel_labels": False,
    }
    resolved = pct.resolve_channel_template({}, params)
    assert resolved["labels"] == pct.LABELS_PROJECT
    assert pct.template_labels_on(resolved, params["prompt_channel_labels"]) is False
    assert pct.get_channel_template("sonder") == preset_before


def test_strict_normalizer_rejects_instead_of_adopting_default():
    assert pct.strict_normalize_channel_template(None) is None
    assert pct.strict_normalize_channel_template({"id": "broken", "channels": []}) is None
    assert pct.strict_normalize_channel_template({
        "id": "broken",
        "channels": [{"key": "a"}, {"key": "a"}],
    }) is None
    assert pct.strict_normalize_channel_template({
        "id": "broken", "channels": [{"key": "a"}], "labels": "sometimes",
    }) is None


def test_normalizer_fallback_is_not_the_shared_preset_object():
    fallback = pct.normalize_channel_template(None)
    fallback["labels"] = pct.LABELS_NEVER
    fallback["channels"][0]["label"] = "changed"
    fresh = pct.get_channel_template("sonder")
    assert fresh["labels"] == pct.LABELS_ALWAYS
    assert fresh["channels"][0]["label"] == "[VISUAL]:"


# --- composition under a template -----------------------------------------------

def test_section_text_living_only_in_a_late_channel_still_resolves():
    # The truncation trap: with a six-channel template, a section whose text is
    # entirely in channel 4 composes to "" under the legacy three-key set and
    # would be dropped before coverage, silently losing the whole section.
    template = pct.get_channel_template("minimax_h3_ref")
    sections = [PromptSection(0, 120, channels={"detailed_description": "a dog walks"})]
    segments = pp.resolve_segments(sections, 0, 120, True, 0.0, template)
    assert [segment["text"] for segment in segments] == [
        "detailed_description:\na dog walks"]


def test_same_sections_compose_differently_under_each_template():
    sections = [PromptSection(0, 120, attachments=[
        prompt_context.shot_attachment()], channels={
        "visual": "a dog walks",
        "integrated_multimodal_description": "a dog walks",
    })]
    sonder = pp.compose_range_prompt("", sections, 0, 120, labels_on=True,
                                     template=pct.get_channel_template("sonder"))
    minimax = pp.compose_range_prompt("", sections, 0, 120, labels_on=False,
                                      template=pct.get_channel_template("minimax_h3_base"))
    assert sonder == "[VISUAL]: a dog walks"
    # labels_on=False, yet the named-field template still emits its field name,
    # and its shot-marker channel opens the first shot.
    assert minimax == "integrated_multimodal_description: a dog walks"


def test_global_text_merges_into_the_first_emitted_field_not_ahead_of_it():
    # Interim P1 rule. Left alone the global text would print before the first
    # field name and produce a malformed named-field payload.
    template = pct.get_channel_template("minimax_h3_base")
    sections = [PromptSection(0, 120, attachments=[
        prompt_context.shot_attachment()], channels={
        "integrated_multimodal_description": "a dog walks"})]
    composed = pp.compose_range_prompt("cinematic", sections, 0, 120,
                                       delimiter=".", template=template)
    # The global text lands ahead of [Shot 1] inside the field, which is where
    # the MiniMax full-reference guide puts the style opening.
    assert composed == (
        "integrated_multimodal_description: cinematic. a dog walks")
    assert not composed.startswith("cinematic")


def test_global_text_alone_still_lands_inside_a_field():
    # With no sections at all the global text is the whole payload — but under
    # a named-field template it still belongs INSIDE the first field. Bare text
    # ahead of every field name is exactly the malformed shape being avoided.
    template = pct.get_channel_template("minimax_h3_base")
    assert pp.compose_range_prompt("cinematic", [], 0, 120, template=template) == (
        "integrated_multimodal_description: cinematic")


def test_unknown_channels_survive_a_template_round_trip():
    # Switching template away and back must not be data loss: keys the active
    # template does not name are preserved, not dropped.
    authored = {"detailed_description": "kept", "summary": "also kept"}
    section = PromptSection(0, 120, channels=authored)
    under_sonder = pp.compose_range_prompt("", [section], 0, 120, labels_on=True,
                                           template=pct.get_channel_template("sonder"))
    assert under_sonder == ""  # nothing the three-channel template can show
    under_minimax = pp.compose_range_prompt("", [section], 0, 120,
                                            template=pct.get_channel_template("minimax_h3_ref"))
    assert "kept" in under_minimax and "also kept" in under_minimax


# --- timecode -------------------------------------------------------------------

def test_timecode_rejects_unusable_fps():
    assert pct.format_shot_timecode(24, 0) == ""
    assert pct.format_shot_timecode(24, -1) == ""
    assert pct.format_shot_timecode(-1, 24) == ""
    assert pct.format_shot_timecode("x", 24) == ""


def test_timecode_rounds_rather_than_truncates():
    # 1 frame at 23.976fps is 41.708...ms — truncation would give .041.
    assert pct.format_shot_timecode(1, 23.976) == "00:00.042"
