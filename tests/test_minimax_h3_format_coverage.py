"""Structural coverage of the MiniMax H3 full-reference format.

One row per item of the reference-combination checklist
(`memory/Research Entries/research_artifacts/minimax-h3-2026-08/
reference_combination_checklist.md`), compiled through the real setup resolver
and the real prompt compiler.

Two disciplines keep this file alive:

* **Pin structure, not prose.** Assertions cover label ordinals, marker
  vocabulary, task-prefix ordering, channel routing and `(Sx)` placement.
  Authored sentences are supplied by the fixtures and never asserted whole, so
  a formatter reword cannot fail a row.
* **Reachability is not tested here.** Whether an author can get to a shape
  without provider knowledge is a UX property; it lives in the coverage matrix
  and the practice-run findings, not in an assertion.

Three rows are `xfail(strict=True)`: they assert the guide's shape for a
relationship Sonder currently emits differently. They fail loudly the day the
gap is closed, which is the signal to promote them to ordinary rows.
"""

import copy
import re
import time

import pytest

from server import minimax_h3, prompt_context
from server.timeline_state import (
    Asset,
    PromptSection,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
)


# --------------------------------------------------------------------------
# Recipe declarations, mirroring the shipped H3 presets' `soft` block.
# --------------------------------------------------------------------------

PICTURE_RECIPE = {"soft": {
    "compatible_profiles": ["minimax_h3_ref@1"],
    "physical_population": "pictures",
    "exposed_capabilities": ["definitions", "retention", "mentions"],
    "role_fields": ["role", "visual_intent"]}}
VIDEO_RECIPE = {"soft": {
    "compatible_profiles": ["minimax_h3_ref@1"],
    "physical_population": "videos",
    "exposed_capabilities": ["definitions", "retention", "mentions",
                             "audio_relationship"],
    "role_fields": ["role", "visual_intent", "audio_intent"]}}
AUDIO_RECIPE = {"soft": {
    "compatible_profiles": ["minimax_h3_ref@1"],
    "physical_population": "standalone_audios",
    "exposed_capabilities": ["definitions", "retention", "mentions",
                             "audio_relationship"],
    "role_fields": ["role", "audio_intent"]}}

WINDOW_END = 100
H3_PROFILE = prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]


def _assets():
    return [
        Asset(asset_id="img_a", asset_type="image"),
        Asset(asset_id="img_b", asset_type="image"),
        Asset(asset_id="img_c", asset_type="image"),
        Asset(asset_id="vid_a", asset_type="video", duration_sec=10.0,
              has_audio=True, has_audio_checked=True),
        Asset(asset_id="aud_a", asset_type="audio", duration_sec=6.0),
        Asset(asset_id="aud_b", asset_type="audio", duration_sec=6.0),
    ]


def _reference_chip(attachment_id, source, config, *,
                    capabilities=("definitions", "retention"),
                    placement="section_prefix", group=None):
    return prompt_context.normalize_attachment({
        "attachment_id": attachment_id,
        "emission_group_id": group or attachment_id,
        "kind": "reference",
        "provider_id": "minimax_h3_ref",
        "provider_version": "1",
        "source": source,
        "config": config,
        "capabilities": [{"capability_id": value, "kind": value,
                          "placement": placement} for value in capabilities],
    })


def _vocal_event(attachment_id, *, subject_ids=(), voice_id="",
                 event_type="dialogue", phrase="", text="Line."):
    return prompt_context.normalize_attachment({
        "attachment_id": attachment_id,
        "kind": "vocal_event",
        "source": {"subject_ids": list(subject_ids), "voice_id": voice_id},
        "config": {"event_type": event_type, "language": "English",
                   "subject_phrase": phrase, "text": text},
    })


def _resolve(*, setup, entities, items, recipes, units=()):
    return minimax_h3.resolve_setup(
        setup=setup, reference_items=list(items), lane_recipes=list(recipes),
        lane_count=len(recipes), scene_duration=WINDOW_END, window_start=0,
        window_end=WINDOW_END, references=list(entities), assets=_assets(),
        semantic_units=list(units))


def _compile(resolved, units, sections):
    return prompt_context.compile_prompt_context(
        sections=list(sections), window_start=0, window_end=WINDOW_END,
        fps=24.0, template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_picture_ordinals": resolved.get("unit_picture_ordinals", {}),
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": list(units),
        }, labels_on=True)


# --------------------------------------------------------------------------
# Base scene fixtures.  Each is reused by several checklist rows.
# --------------------------------------------------------------------------

def scene_single_member_subject():
    """One Subject defined by one Picture (A1, A6, A7, F1/F2, G1)."""
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a",
                        prompt="the young woman with long dark hair")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes,
                        units=units)
    sections = [PromptSection(0, WINDOW_END, channels={
        "detailed_description": "She looks up."}, attachments=[
            prompt_context.shot_attachment(),
            _reference_chip("c", {"semantic_unit_ids": ["u"]}, {
                "retention_detail": "face and hairstyle"}),
            _reference_chip("s", {"semantic_unit_ids": ["u"]},
                            {"summary": "The target video follows her."},
                            capabilities=("summary",))])]
    return _compile(resolved, units, sections)


def test_global_h3_summary_owns_the_scene_wide_summary_key():
    """H3 summaries dedupe scene-wide even when their reuse groups differ."""
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a",
                        prompt="the young woman with long dark hair")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(
        setup={"mode": "reference", "picture_lane_ids": ["lp"]},
        entities=[entity], items=items, recipes=recipes, units=units)
    global_summary = _reference_chip(
        "global-summary", {"semantic_unit_ids": ["u"]},
        {"summary": "GLOBAL SUMMARY"}, capabilities=("summary",),
        group="global-summary-group")
    section_summary = _reference_chip(
        "section-summary", {"semantic_unit_ids": ["u"]},
        {"summary": "SECTION SUMMARY"}, capabilities=("summary",),
        group="section-summary-group")
    context = {
        "setup_manifest": resolved["setup_manifest"],
        "ordinal_manifest": resolved["ordinal_manifest"],
        "unit_picture_ordinals": resolved.get("unit_picture_ordinals", {}),
        "unit_source_labels": resolved.get("unit_source_labels", {}),
        "unit_source_members": resolved.get("unit_source_members", {}),
        "semantic_units": units,
    }

    compiled = prompt_context.compile_prompt_context(
        global_channels={"summary": "GLOBAL AUTHORED"},
        global_attachments=[global_summary],
        sections=[PromptSection(0, WINDOW_END, channels={
            "summary": "SECTION AUTHORED"}, attachments=[section_summary])],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context=context, labels_on=True)

    assert "GLOBAL SUMMARY" in compiled["prompt"]
    assert "SECTION SUMMARY" not in compiled["prompt"]
    assert [row["attachment_id"] for row in compiled["emissions"]
            if row["capability_id"] == "summary"] == ["global-summary"]
    assert [row["attachment_id"] for row in compiled["errors"]
            if row["code"] == "conflicting_emission"] == ["section-summary"]


def scene_composite_subject():
    """One Subject spanning a Picture and a Video (A2, E1, A5, C3, E4, G3)."""
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a"),
        ReferenceMember(member_id="mv", asset_id="vid_a")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE),
               ReferenceLaneRecipe(lane_id="lv", media_kind="image",
                                   recipe=VIDEO_RECIPE)]
    items = [
        ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "mp",
                           "role": "identity", "visual_intent": "preserve"}]),
        ReferenceItem(reference_item_id="iv", lane_index=1, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "mv",
                           "role": "temporal_structure",
                           "visual_intent": "reference_loosely"}]),
    ]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "definition": "the woman whose appearance comes from the still "
                            "and whose walking motion comes from the clip",
              "sources": [{"entity_id": "e", "member_id": "mp"},
                                 {"entity_id": "e", "member_id": "mv"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"],
                               "video_lane_ids": ["lv"]},
                        entities=[entity], items=items, recipes=recipes,
                        units=units)
    sections = [PromptSection(0, WINDOW_END, channels={
        "detailed_description": "She walks."}, attachments=[
            prompt_context.shot_attachment(),
            # Both chips must carry an explicit `visual_intent`: the Subject's
            # two staged members deliberately disagree (identity is preserved,
            # structure is only loosely referenced), and a Subject resolves to
            # exactly one retention marker.  See PR-18.
            _reference_chip("c", {"semantic_unit_ids": ["u"]},
                            {"retention_detail": "identity and gait",
                             "visual_intent": "preserve"}),
            _reference_chip("v", {"video_ids": ["mv"]}, {
                "definition": "the whole-video temporal-structure reference",
                "retention_detail": "cut and pacing only",
                "visual_intent": "reference_loosely"}),
            _reference_chip("s", {"semantic_unit_ids": ["u"]},
                            {"summary": "She crosses the room.",
                             "visual_intent": "preserve"},
                            capabilities=("summary",))])]
    return _compile(resolved, units, sections)


def scene_multi_picture_subject():
    """One Subject with three Pictures of the same aspect (A3)."""
    entity = ReferenceEntity(reference_id="e", name="Dog", members=[
        ReferenceMember(member_id=f"m{n}", asset_id=asset)
        for n, asset in enumerate(("img_a", "img_b", "img_c"), 1)])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": f"m{n}",
                                "role": "identity"} for n in (1, 2, 3)])]
    units = [{"semantic_unit_id": "u", "name": "Dog", "order": 0,
              "definition": "the fluffy white Samoyed",
              "sources": [{"entity_id": "e", "member_id": f"m{n}"}
                                 for n in (1, 2, 3)]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes,
                        units=units)
    sections = [PromptSection(0, WINDOW_END, channels={
        "detailed_description": "It runs."}, attachments=[
            prompt_context.shot_attachment(),
            _reference_chip("c", {"semantic_unit_ids": ["u"]},
                            {"retention_detail": "coat and build"})])]
    return _compile(resolved, units, sections)


def scene_one_asset_many_subjects():
    """One Picture yielding several Subjects, one line each (A4)."""
    entity = ReferenceEntity(reference_id="e", name="Interior", members=[
        ReferenceMember(member_id="mp", asset_id="img_a")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "environment"}])]
    units = [
        {"semantic_unit_id": "room", "name": "Room", "order": 0,
         "definition": "the room", "sources": [
             {"entity_id": "e", "member_id": "mp"}]},
        {"semantic_unit_id": "sofa", "name": "Sofa", "order": 1,
         "definition": "the sofa", "sources": [
             {"entity_id": "e", "member_id": "mp"}]},
        {"semantic_unit_id": "art", "name": "Wall art", "order": 2,
         "definition": "the wall art", "sources": [
             {"entity_id": "e", "member_id": "mp"}]},
    ]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes,
                        units=units)
    sections = [PromptSection(0, WINDOW_END, channels={
        "detailed_description": "The camera pans."}, attachments=[
            prompt_context.shot_attachment(),
            *(_reference_chip(f"c_{unit['semantic_unit_id']}",
                              {"semantic_unit_ids": [unit["semantic_unit_id"]]},
                              {"retention_detail": "layout"})
              for unit in units)])]
    return _compile(resolved, units, sections)


def scene_physical_pictures():
    """Standalone `<Picture N>` roles and visual retention markers (B, G2, G7)."""
    entity = ReferenceEntity(reference_id="e", name="Frames", members=[
        ReferenceMember(member_id="first", asset_id="img_a"),
        ReferenceMember(member_id="last", asset_id="img_b"),
        ReferenceMember(member_id="board", asset_id="img_c")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "first",
                                "role": "first_frame", "visual_intent": "preserve"},
                               {"entity_id": "e", "member_id": "last",
                                "role": "last_frame",
                                "visual_intent": "partial"},
                               {"entity_id": "e", "member_id": "board",
                                "role": "storyboard",
                                "visual_intent": "transfer_attributes"}])]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes)
    sections = [
        PromptSection(0, 50, channels={"detailed_description": "Open."},
                      attachments=[
                          prompt_context.shot_attachment(),
                          _reference_chip("p1", {"picture_ids": ["first"]}, {
                              "definition": "the first frame of the opening shot",
                              "retention_detail": "framing held"}),
                          _reference_chip("p2", {"picture_ids": ["last"]}, {
                              "definition": "the last frame of the closing shot",
                              "retention_detail": "pose held, lighting changed"}),
                          _reference_chip("p3", {"picture_ids": ["board"]}, {
                              "definition": "a storyboard reference defining "
                                            "viewpoint and shot order",
                              "retention_detail": "viewpoint transferred"})]),
        PromptSection(50, WINDOW_END, channels={"detailed_description": "Close."},
                      attachments=[prompt_context.shot_attachment()]),
    ]
    return _compile(resolved, [], sections)


def scene_video_edit_with_audio():
    """One source asset staged as a Video and as a standalone Audio (C1, E3, F8, F11)."""
    entity = ReferenceEntity(reference_id="e", name="Source", members=[
        ReferenceMember(member_id="sv", asset_id="vid_a"),
        ReferenceMember(member_id="sa", asset_id="vid_a")])
    recipes = [ReferenceLaneRecipe(lane_id="lv", media_kind="image",
                                   recipe=VIDEO_RECIPE),
               ReferenceLaneRecipe(lane_id="la", media_kind="audio",
                                   recipe=AUDIO_RECIPE)]
    items = [
        ReferenceItem(reference_item_id="iv", lane_index=0, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "sv",
                           "role": "video_editing", "visual_intent": "preserve"}]),
        ReferenceItem(reference_item_id="ia", lane_index=1, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "sa",
                           "role": "audio_reuse", "audio_intent": "copy_full"}]),
    ]
    resolved = _resolve(setup={"mode": "reference", "video_lane_ids": ["lv"],
                               "audio_lane_ids": ["la"]},
                        entities=[entity], items=items, recipes=recipes)
    sections = [PromptSection(0, WINDOW_END, channels={
        "detailed_description": "The edit plays."}, attachments=[
            prompt_context.shot_attachment(),
            _reference_chip("v", {"video_ids": ["sv"]}, {
                "definition": "the source video for the target video edit",
                "retention_detail": "shot order kept"}),
            _reference_chip("a", {"audio_ids": ["sa"]}, {
                "audio_definition": "the synchronized track of the source video",
                "retention_detail": "reused 1:1 as the final audio track"}),
            _reference_chip("s", {"video_ids": ["sv"]}, {
                "summary": "The target video is an edited version of "
                           "@video(sv)."}, capabilities=("summary",))])]
    return _compile(resolved, [], sections)


def scene_video_continuation():
    """A continuation source plus a keyframe image (C2, F4, F7)."""
    entity = ReferenceEntity(reference_id="e", name="Source", members=[
        ReferenceMember(member_id="sv", asset_id="vid_a"),
        ReferenceMember(member_id="kf", asset_id="img_a")])
    recipes = [ReferenceLaneRecipe(lane_id="lv", media_kind="image",
                                   recipe=VIDEO_RECIPE),
               ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [
        ReferenceItem(reference_item_id="iv", lane_index=0, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "sv",
                           "role": "video_continuation"}]),
        ReferenceItem(reference_item_id="ip", lane_index=1, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "kf",
                           "role": "last_frame"}]),
    ]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"],
                               "video_lane_ids": ["lv"]},
                        entities=[entity], items=items, recipes=recipes)
    sections = [PromptSection(0, WINDOW_END, channels={
        "detailed_description": "It resumes."}, attachments=[
            prompt_context.shot_attachment(),
            _reference_chip("s", {"video_ids": ["sv"]},
                            {"summary": "The clip continues and lands on the "
                                        "still."}, capabilities=("summary",))])]
    return _compile(resolved, [], sections)


def scene_audio_and_speakers():
    """Picture + Audio Subject, two managed speakers, group speech (D, E2, E5-E8, G4-G6, H5)."""
    entity = ReferenceEntity(reference_id="e", name="Cast", members=[
        ReferenceMember(member_id="face_a", asset_id="img_a"),
        ReferenceMember(member_id="face_b", asset_id="img_b"),
        ReferenceMember(member_id="voice_a", asset_id="aud_a"),
        ReferenceMember(member_id="bgm", asset_id="aud_b")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE),
               ReferenceLaneRecipe(lane_id="la", media_kind="audio",
                                   recipe=AUDIO_RECIPE)]
    items = [
        ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "face_a",
                           "role": "identity"},
                          {"entity_id": "e", "member_id": "face_b",
                           "role": "identity"}]),
        ReferenceItem(reference_item_id="ia", lane_index=1, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "voice_a",
                           "role": "timbre",
                           "audio_intent": "reference_characteristics"},
                          {"entity_id": "e", "member_id": "bgm",
                           "role": "audio_reuse",
                           "audio_intent": "copy_partial"}]),
    ]
    units = [
        {"semantic_unit_id": "ua", "name": "A", "order": 0,
         "definition": "the first speaker", "sources": [
             {"entity_id": "e", "member_id": "face_a"},
             {"entity_id": "e", "member_id": "voice_a"}]},
        {"semantic_unit_id": "ub", "name": "B", "order": 1,
         "definition": "the second speaker", "sources": [
             {"entity_id": "e", "member_id": "face_b"}]},
    ]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"],
                               "audio_lane_ids": ["la"]},
                        entities=[entity], items=items, recipes=recipes,
                        units=units)
    document = {"nodes": [
        {"type": "text", "text": "B turns. "},
        {"type": "attachment", "attachment_id": "vb"},
        {"type": "text", "text": " Then A answers. "},
        {"type": "attachment", "attachment_id": "va"},
        {"type": "text", "text": " Finally "},
        {"type": "attachment", "attachment_id": "vg"},
    ]}
    sections = [PromptSection(0, WINDOW_END,
                              channel_docs={"detailed_description": document},
                              attachments=[
        prompt_context.shot_attachment(),
        _vocal_event("vb", subject_ids=["ub"], phrase="the second speaker"),
        _vocal_event("va", subject_ids=["ua"], phrase="the first speaker"),
        _vocal_event("vg", subject_ids=["ua", "ub"], event_type="group_speech",
                     phrase="they"),
        _reference_chip("ca", {"semantic_unit_ids": ["ua"]}, {
            "audio_definition": "the voice-timbre reference",
            "audio_speaker_subject_id": "ua",
            "retention_details": {"<Subject 1>": "face preserved",
                                  "<Audio 1>": "timbre only"}}),
        _reference_chip("cb", {"semantic_unit_ids": ["ub"]},
                        {"retention_detail": "face preserved"}),
        _reference_chip("bg", {"audio_ids": ["bgm"]}, {
            "audio_definition": "the reused background music; its vocal is part "
                                "of the track and is not a target speaker",
            "retention_detail": "first half copied"}),
    ])]
    return _compile(resolved, units, sections)


def scene_dense_setup():
    """Over-cap pictures plus every population, for caps and ordering (H1, H3)."""
    members = [ReferenceMember(member_id=f"p{n}", asset_id="img_a")
               for n in range(12)]
    members += [ReferenceMember(member_id="v1", asset_id="vid_a"),
                ReferenceMember(member_id="a1", asset_id="aud_a")]
    entity = ReferenceEntity(reference_id="e", name="Dense", members=members)
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE),
               ReferenceLaneRecipe(lane_id="lv", media_kind="image",
                                   recipe=VIDEO_RECIPE),
               ReferenceLaneRecipe(lane_id="la", media_kind="audio",
                                   recipe=AUDIO_RECIPE)]
    items = [
        ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                      end_frame=WINDOW_END,
                      members=[{"entity_id": "e", "member_id": f"p{n}"}
                               for n in range(12)]),
        ReferenceItem(reference_item_id="iv", lane_index=1, start_frame=0,
                      end_frame=WINDOW_END,
                      members=[{"entity_id": "e", "member_id": "v1"}]),
        ReferenceItem(reference_item_id="ia", lane_index=2, start_frame=0,
                      end_frame=WINDOW_END,
                      members=[{"entity_id": "e", "member_id": "a1"}]),
    ]
    return _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"],
                           "video_lane_ids": ["lv"], "audio_lane_ids": ["la"]},
                    entities=[entity], items=items, recipes=recipes)


SCENES = {
    "single_member_subject": scene_single_member_subject,
    "composite_subject": scene_composite_subject,
    "multi_picture_subject": scene_multi_picture_subject,
    "one_asset_many_subjects": scene_one_asset_many_subjects,
    "physical_pictures": scene_physical_pictures,
    "video_edit_with_audio": scene_video_edit_with_audio,
    "video_continuation": scene_video_continuation,
    "audio_and_speakers": scene_audio_and_speakers,
}


@pytest.fixture(scope="module")
def compiled():
    return {name: build() for name, build in SCENES.items()}


def _channel(compiled, scene, key):
    return compiled[scene]["channels"].get(key, "")


# --------------------------------------------------------------------------
# A — subject/asset cardinality (ref 2.1)
# --------------------------------------------------------------------------

CARDINALITY_ROWS = [
    # item, scene, channel, required fragments
    ("A1", "single_member_subject", "subject_definitions",
     ["<Subject 1> is ", "from <Picture 1>"]),
    ("A3", "multi_picture_subject", "subject_definitions",
     ["<Subject 1> is ", "from <Picture 1>, <Picture 2>, and <Picture 3>"]),
    ("A5", "composite_subject", "subject_definitions", ["<Video 1>"]),
    ("A7", "one_asset_many_subjects", "subject_definitions",
     ["<Subject 1> is ", "<Subject 2> is ", "<Subject 3> is "]),
]


@pytest.mark.parametrize("item,scene,channel,fragments", CARDINALITY_ROWS,
                         ids=[row[0] for row in CARDINALITY_ROWS])
def test_subject_asset_cardinality(compiled, item, scene, channel, fragments):
    value = _channel(compiled, scene, channel)
    for fragment in fragments:
        assert fragment in value, f"{item}: {fragment!r} missing from {channel}"


def test_a2_composite_subject_cites_both_source_labels(compiled):
    """A2/E1 — one Subject, two assets, both provenance labels present."""
    value = _channel(compiled, "composite_subject", "subject_definitions")
    assert "<Subject 1> is " in value
    assert "<Picture 1>" in value and "<Video 1>" in value


def test_a4_one_asset_defines_several_subjects_with_the_same_ordinal(compiled):
    """A4 — three Subjects, one shared Picture ordinal, one entry each."""
    value = _channel(compiled, "one_asset_many_subjects", "subject_definitions")
    assert value.count("from <Picture 1>") == 3
    assert re.findall(r"<Subject (\d+)> is", value) == ["1", "2", "3"]
    assert "<Picture 2>" not in value


def test_a2_split_role_staging_needs_an_explicit_chip_retention(compiled):
    """A2 — a Subject resolves to one retention marker, so members that stage
    different visual intents must be reconciled on the chip.

    Every chip naming that Subject is validated, including one that carries no
    retention capability at all, which is why the composite fixture sets
    `visual_intent` on its summary-only chip too.  Recorded as PR-18.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a"),
        ReferenceMember(member_id="mv", asset_id="vid_a")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE),
               ReferenceLaneRecipe(lane_id="lv", media_kind="image",
                                   recipe=VIDEO_RECIPE)]
    items = [
        ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "mp",
                           "visual_intent": "preserve"}]),
        ReferenceItem(reference_item_id="iv", lane_index=1, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "mv",
                           "visual_intent": "reference_loosely"}]),
    ]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "definition": "the woman", "sources": [
                  {"entity_id": "e", "member_id": "mp"},
                  {"entity_id": "e", "member_id": "mv"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"],
                               "video_lane_ids": ["lv"]},
                        entities=[entity], items=items, recipes=recipes,
                        units=units)

    def compile_with(config):
        return _compile(resolved, units, [PromptSection(
            0, WINDOW_END, channels={"detailed_description": "She walks."},
            attachments=[prompt_context.shot_attachment(),
                         _reference_chip("c", {"semantic_unit_ids": ["u"]},
                                         config)])])

    refused = compile_with({"retention_detail": "identity"})
    assert [value["code"] for value in refused["errors"]] == [
        "conflicting_reference_intent"]
    accepted = compile_with({"retention_detail": "identity",
                             "visual_intent": "preserve"})
    assert accepted["errors"] == []
    assert "fully_preserved" in accepted["channels"]["retention_analysis"]

    summary_only = _compile(resolved, units, [PromptSection(
        0, WINDOW_END, channels={"detailed_description": "She walks."},
        attachments=[prompt_context.shot_attachment(),
                     _reference_chip("s", {"semantic_unit_ids": ["u"]},
                                     {"summary": "She crosses."},
                                     capabilities=("summary",))])])
    assert [value["code"] for value in summary_only["errors"]] == [
        "conflicting_reference_intent"]


def test_a6_definition_only_picture_still_receives_its_ordinal(compiled):
    """A6 — no standalone `<Picture N>` line, but the ordinal is still assigned."""
    result = compiled["single_member_subject"]
    assert result["ordinal_manifest"]["pictures"]["mp"] == 1
    definitions = result["channels"]["subject_definitions"]
    assert not re.search(r"(?m)^<Picture 1> is", definitions)
    assert "from <Picture 1>" in definitions


# --------------------------------------------------------------------------
# B — standalone `<Picture N>` roles (ref 2.2)
# --------------------------------------------------------------------------

PICTURE_ROLE_VALUES = {value["value"] for value in
                       prompt_context.MINIMAX_H3_ROLE_CATALOGS["pictures"]}


@pytest.mark.parametrize("item,role", [
    ("B1", "first_frame"),
    ("B2", "keyframe"),
    ("B4", "last_frame"),
    ("B5", "composition_anchor"),
    ("B6", "storyboard"),
])
def test_picture_roles_exist_in_the_authoring_catalog(item, role):
    assert role in PICTURE_ROLE_VALUES, f"{item}: no authorable {role} role"


def test_b3_edited_keyframe_has_no_role_value():
    """B3 — deliberately recorded: the relationship is prose-only today."""
    assert "edited_keyframe" not in PICTURE_ROLE_VALUES
    # The task-type deriver still understands the value, so adding it to the
    # catalog is the only change a fix needs.
    assert prompt_context._minimax_task_types(
        {"setup_manifest": {"pictures": [{"role": "edited_keyframe"}]}},
        profile=H3_PROFILE) == [
            "keyframe completion"]


def test_standalone_picture_lines_use_their_own_ordinals(compiled):
    """B1/B4/B6 — each staged Picture gets its own definition line."""
    value = _channel(compiled, "physical_pictures", "subject_definitions")
    assert "<Picture 1> is " in value
    assert "<Picture 2> is " in value
    assert "<Picture 3> is " in value


# --------------------------------------------------------------------------
# C — `<Video N>` whole-video relationships (ref 2.3)
# --------------------------------------------------------------------------

VIDEO_ROLE_VALUES = {value["value"] for value in
                     prompt_context.MINIMAX_H3_ROLE_CATALOGS["videos"]}


@pytest.mark.parametrize("item,role", [
    ("C1", "video_editing"),
    ("C2", "video_continuation"),
    ("C3", "temporal_structure"),
])
def test_video_roles_exist_in_the_authoring_catalog(item, role):
    assert role in VIDEO_ROLE_VALUES


def test_c1_edit_source_emits_a_video_definition(compiled):
    value = _channel(compiled, "video_edit_with_audio", "subject_definitions")
    assert "<Video 1> is " in value


def test_c3_structure_only_video_never_derives_editing_or_continuation(compiled):
    """C3 — a structure-only video is `reference generation`, never editing."""
    summary = _channel(compiled, "composite_subject", "summary")
    assert summary.startswith("[reference generation]")
    assert "video editing" not in summary and "video continuation" not in summary


# --------------------------------------------------------------------------
# D — `<Audio N>` roles (ref 2.4).  D8 is a deliberate gap (paired audio).
# --------------------------------------------------------------------------

AUDIO_ROLE_VALUES = {value["value"] for value in
                     prompt_context.MINIMAX_H3_ROLE_CATALOGS["standalone_audios"]}


@pytest.mark.parametrize("item,role", [
    ("D1", "audio_reuse"),
    ("D2", "audio_reference"),
    ("D3", "timbre"),
    ("D6", "rhythm"),
    ("D5", "sound_texture"),
])
def test_audio_roles_exist_in_the_authoring_catalog(item, role):
    assert role in AUDIO_ROLE_VALUES


def test_d3_voice_timbre_reuses_the_target_speaker_id(compiled):
    """D3/E2 — `<Audio N> is ... for <Subject N> (Sx)`, never a new number."""
    value = _channel(compiled, "audio_and_speakers", "subject_definitions")
    match = re.search(r"<Audio 1> is .*? for <Subject (\d+)> \(S(\d+)\)", value)
    assert match, value
    subject_number, speaker_number = match.groups()
    detailed = _channel(compiled, "audio_and_speakers", "detailed_description")
    # The (Sx) in the audio definition must be the one the target video's own
    # vocal order assigned to that Subject, not a fresh audio-side number.
    assert f"(S{speaker_number})" in detailed
    assert subject_number == "1"


def test_d7_one_audio_carries_several_roles_in_one_line(compiled):
    """D7 — one natural sentence, no extra subsections."""
    value = _channel(compiled, "audio_and_speakers", "subject_definitions")
    audio_lines = [line for line in value.splitlines()
                   if line.strip().startswith("<Audio 2>")]
    assert len(audio_lines) <= 1


def test_d8_paired_audio_remains_absent():
    """D8 — recorded as a deliberate gap, not a defect."""
    assert not hasattr(minimax_h3, "MAX_VIDEO_PAIRED_AUDIO")
    assert {minimax_h3.PICTURES_POPULATION, minimax_h3.VIDEOS_POPULATION,
            minimax_h3.STANDALONE_AUDIOS_POPULATION} == {
        "pictures", "videos", "standalone_audios"}


# --------------------------------------------------------------------------
# E — cross-label combinations
# --------------------------------------------------------------------------

def test_e3_one_asset_is_numbered_independently_per_population(compiled):
    """E3 — `<Video 1>` and `<Audio 1>` from one file; indices never pair."""
    result = compiled["video_edit_with_audio"]
    assert result["ordinal_manifest"]["videos"]["sv"] == 1
    assert result["ordinal_manifest"]["audios"]["sa"] == 1
    definitions = result["channels"]["subject_definitions"]
    assert "<Video 1> is " in definitions and "<Audio 1> is " in definitions


def test_e4_one_video_serves_structure_and_supplies_a_subject(compiled):
    """E4 — two labels over one asset: `<Video 1>` and a `<Subject N>`."""
    value = _channel(compiled, "composite_subject", "subject_definitions")
    assert "<Subject 1> is " in value
    assert re.search(r"(?m)^<Video 1> is", value) or "\n<Video 1> is" in value \
        or "<Video 1> is " in value


def test_e5_speaking_subject_carries_a_speaker_id(compiled):
    """E5 — every managed vocal event renders `(Sx)` and a bounded `<d>` block."""
    value = _channel(compiled, "audio_and_speakers", "detailed_description")
    for number in (1, 2):
        assert f"(S{number})" in value
    assert value.count("<d>[English] ") == 3


def test_e7_group_speech_uses_one_compound_speaker_token(compiled):
    """E7 — already-numbered speakers speaking together share one `(...)`."""
    value = _channel(compiled, "audio_and_speakers", "detailed_description")
    compound = re.search(r"\((S\d+,S\d+)\)", value)
    assert compound, value
    assert set(compound.group(1).split(",")) == {"S1", "S2"}


def test_e7_compound_speaker_token_is_ascending(compiled):
    value = _channel(compiled, "audio_and_speakers", "detailed_description")
    assert "(S1,S2)" in value


def test_e8_a_voice_inside_reused_bgm_creates_no_speaker(compiled):
    """E8 — the audible source is cited; no `(Sx)` is invented for it."""
    result = compiled["audio_and_speakers"]
    definitions = result["channels"]["subject_definitions"]
    # Read to the next reference label rather than to a newline: definition
    # lines from separate chips are still space-joined (PR-15).
    bgm = re.search(r"<Audio 2> is (.*?)(?=<(?:Subject|Picture|Video|Audio) \d+>|$)",
                    definitions, re.S)
    assert bgm, definitions
    assert "(S" not in bgm.group(1)
    # Only the two Subjects with managed Vocal Events own speaker numbers.
    assert result["managed_speaker_subject_ids"] == ["ub", "ua"]


# --------------------------------------------------------------------------
# F — `summary` task-type prefixes (ref 3)
# --------------------------------------------------------------------------

ROLE_TASK_TYPES = [
    ("F1", "pictures", "identity", ["reference generation"]),
    ("F2", "pictures", "first_frame", ["keyframe completion"]),
    ("F3", "videos", "video_editing", ["video editing"]),
    ("F4", "videos", "video_continuation", ["video continuation"]),
    ("F5", "standalone_audios", "audio_reuse", ["audio reuse"]),
    ("F6", "standalone_audios", "audio_reference", ["audio reference"]),
]


@pytest.mark.parametrize("item,population,role,expected", ROLE_TASK_TYPES,
                         ids=[row[0] for row in ROLE_TASK_TYPES])
def test_task_type_is_derived_from_the_staged_role(item, population, role,
                                                   expected):
    assert prompt_context._minimax_task_types(
        {"setup_manifest": {population: [{"role": role}]}},
        profile=H3_PROFILE) == expected


def test_media_presence_alone_creates_no_task_type():
    """ref 3 — the mere presence of an asset never implies a task type."""
    assert prompt_context._minimax_task_types({"setup_manifest": {
        "pictures": [{"role": ""}], "videos": [{"role": ""}],
        "standalone_audios": [{"role": ""}]}}, profile=H3_PROFILE) == []


def test_identity_task_type_default_overrides_role_derivation_in_compiled_summary():
    entity = ReferenceEntity(reference_id="task-entity", name="Subject", members=[
        ReferenceMember(member_id="task-picture", asset_id="img_a")])
    recipe = ReferenceLaneRecipe(lane_id="task-lane", media_kind="image",
                                 recipe=PICTURE_RECIPE)
    item = ReferenceItem(reference_item_id="task-item", lane_index=0,
                         start_frame=0, end_frame=WINDOW_END, members=[{
                             "entity_id": "task-entity",
                             "member_id": "task-picture",
                             "role": "identity",
                             "visual_intent": "preserve",
                         }])
    units = [{
        "semantic_unit_id": "task-subject", "name": "Subject", "order": 0,
        "sources": [{"entity_id": "task-entity", "member_id": "task-picture"}],
        "attachment_defaults": {"task_types": ["audio reuse"]},
    }]
    resolved = _resolve(
        setup={"mode": "reference", "picture_lane_ids": ["task-lane"]},
        entities=[entity], items=[item], recipes=[recipe], units=units)
    summary = _reference_chip(
        "task-summary", {"semantic_unit_ids": ["task-subject"]},
        {"summary": "The subject crosses the room."}, capabilities=("summary",))
    compiled = _compile(resolved, units, [PromptSection(
        0, WINDOW_END, channels={"detailed_description": "Move."},
        attachments=[summary])])

    value = compiled["channels"]["summary"]
    assert value.startswith("[audio reuse] The subject crosses the room.")
    assert "[reference generation]" not in value


COMBINED_TASK_TYPES = [
    ("F7", ["video continuation", "keyframe completion"]),
    ("F8", ["video editing", "audio reuse"]),
    ("F9", ["reference generation", "audio reference"]),
    ("F10", ["video continuation", "audio reference"]),
]


@pytest.mark.parametrize("item,selection", COMBINED_TASK_TYPES,
                         ids=[row[0] for row in COMBINED_TASK_TYPES])
def test_combined_task_types_emit_once_each_in_canonical_order(item, selection):
    emitted = prompt_context._minimax_task_types({}, selection, H3_PROFILE)
    assert set(emitted) == set(selection)
    assert len(emitted) == len(set(emitted))
    canonical = list(prompt_context.MINIMAX_TASK_TYPES)
    assert emitted == sorted(emitted, key=canonical.index)


def test_f7_and_f8_reach_the_compiled_summary(compiled):
    assert _channel(compiled, "video_edit_with_audio", "summary").startswith(
        "[video editing + audio reuse]")
    assert _channel(compiled, "video_continuation", "summary").startswith(
        "[keyframe completion + video continuation]")


def test_f11_video_editing_opener_resolves_its_video_token(compiled):
    """F11 — the opener's `<Video 1>` comes from a late-bound stable id."""
    summary = _channel(compiled, "video_edit_with_audio", "summary")
    body = summary.split("] ", 1)[1]
    assert body.startswith("The target video is an edited version of <Video 1>.")
    assert "@video(" not in summary


# --------------------------------------------------------------------------
# G — `retention_analysis` pairings (ref 4)
# --------------------------------------------------------------------------

def test_g1_subject_retention_lists_its_shots(compiled):
    value = _channel(compiled, "single_member_subject", "retention_analysis")
    assert re.search(r"<Subject 1> \(appears in \[Shot 1\]\): fully_preserved - ",
                     value)


def test_g1_appearance_list_accumulates_every_shot_reached(compiled):
    value = _channel(compiled, "one_asset_many_subjects", "retention_analysis")
    assert "(appears in [Shot 1])" in value


VISUAL_MARKERS = [
    ("G2", "physical_pictures", r"<Picture 1>[^:]*: fully_preserved - "),
    ("G7", "physical_pictures", r"<Picture 3>[^:]*: attribute_transfer - "),
    ("G3", "composite_subject", r"<Video 1>[^:]*: weak_reference - "),
]


@pytest.mark.parametrize("item,scene,pattern", VISUAL_MARKERS,
                         ids=[row[0] for row in VISUAL_MARKERS])
def test_visual_retention_markers(compiled, item, scene, pattern):
    value = _channel(compiled, scene, "retention_analysis")
    assert re.search(pattern, value), f"{item}: {pattern} not in {value!r}"


def test_g_partially_preserved_marker_reaches_output(compiled):
    value = _channel(compiled, "physical_pictures", "retention_analysis")
    assert re.search(r"<Picture 2>[^:]*: partially_preserved - ", value)


AUDIO_MARKERS = [
    ("G4", "video_edit_with_audio", r"<Audio 1>[^:]*: fully_copy - "),
    ("G5", "audio_and_speakers", r"<Audio 2>[^:]*: partially_copy - "),
    ("G6", "audio_and_speakers", r"<Audio 1>[^:]*: reference - "),
]


@pytest.mark.parametrize("item,scene,pattern", AUDIO_MARKERS,
                         ids=[row[0] for row in AUDIO_MARKERS])
def test_audio_retention_markers(compiled, item, scene, pattern):
    value = _channel(compiled, scene, "retention_analysis")
    assert re.search(pattern, value), f"{item}: {pattern} not in {value!r}"


def test_g8_weak_reference_is_the_only_shared_marker():
    shared = set(prompt_context.VISUAL_INTENTS.values()) & set(
        prompt_context.AUDIO_INTENTS.values())
    assert shared == {"weak_reference"}


def test_g7_attribute_transfer_has_no_audio_counterpart():
    assert "attribute_transfer" not in prompt_context.AUDIO_INTENTS.values()


def test_speaker_ids_never_appear_in_retention_analysis(compiled):
    """ref 4 — `(Sx)` is a description-side identity only."""
    for name in SCENES:
        value = _channel(compiled, name, "retention_analysis")
        assert not re.search(r"\(S\d+", value), name


@pytest.mark.xfail(strict=True, reason=(
    "PR-17: physical retention lines omit the guide's role parenthetical, so "
    "`<Picture 2> ([Shot 1] first frame): ...` compiles as `<Picture 2>: ...`."))
def test_physical_retention_carries_its_role_parenthetical(compiled):
    value = _channel(compiled, "physical_pictures", "retention_analysis")
    assert re.search(r"<Picture 1> \([^)]+\): fully_preserved", value)


# --------------------------------------------------------------------------
# H — ceilings, ordering and numbering (node contract)
# --------------------------------------------------------------------------

def test_h1_per_group_caps():
    assert (minimax_h3.MAX_PICTURES, minimax_h3.MAX_VIDEOS,
            minimax_h3.MAX_STANDALONE_AUDIO) == (9, 3, 3)
    resolved = scene_dense_setup()
    assert len(resolved["setup_manifest"]["pictures"]) == 9
    assert not any(value["code"] == "pictures_slot_cap"
                   for value in resolved["errors"])
    assert any(value["code"] == "pictures_slot_cap"
               for value in resolved["warnings"])


def test_h3_presentation_order_is_pictures_then_videos_then_audio():
    resolved = scene_dense_setup()
    kinds = [row["kind"] for row in resolved["setup_manifest"]["presentation"]]
    assert kinds == ["picture"] * 9 + ["video", "standalone_audio"]


def test_h4_ordinals_are_positional_and_one_based_per_type():
    resolved = scene_dense_setup()
    manifest = resolved["setup_manifest"]
    assert [row["picture_ordinal"] for row in manifest["pictures"]] == list(
        range(1, 10))
    assert [row["video_ordinal"] for row in manifest["videos"]] == [1]
    assert [row["audio_ordinal"] for row in manifest["standalone_audios"]] == [1]


def test_h4_authoring_a_definition_never_renumbers_a_slot(compiled):
    """The manifest is resolved before any chip renders."""
    result = compiled["physical_pictures"]
    assert result["ordinal_manifest"]["pictures"] == {
        **result["ordinal_manifest"]["pictures"],
        "first": 1, "last": 2, "board": 3,
    }


def test_h5_speaker_order_follows_the_target_video_not_the_audio_staging(compiled):
    """H5 — `(Sx)` comes from vocal-event order, not audio member order."""
    result = compiled["audio_and_speakers"]
    # `voice_a` is the first staged audio member, and its Subject `ua` speaks
    # second in the target video, so it must not own (S1).
    assert result["ordinal_manifest"]["audios"]["voice_a"] == 1
    assert result["managed_speaker_subject_ids"].index("ua") == 1


# --------------------------------------------------------------------------
# Cross-cutting: every reference label the guide defines stays on its own line.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scene,channel", [
    ("one_asset_many_subjects", "subject_definitions"),
    ("physical_pictures", "retention_analysis"),
])
def test_each_reference_label_owns_its_own_line(compiled, scene, channel):
    """ref 2 "Give each item its own line"; ref 4 "one line for each label".

    Counts labels in RECORD-OPENING position, not every label on the line. A
    definition legitimately cites its sources — `<Subject 1> is the room from
    <Picture 1>` is the guide's own shape — so a naive count of labels per line
    reports correct output as broken. The three opening shapes are `<L> is …`
    (definition), `<L> (…): …` (retention with a qualifier) and `<L>: …`
    (retention without one); PR-15's space-join put two of those on one line.
    """
    value = _channel(compiled, scene, channel)
    lines = [line for line in value.splitlines() if line.strip()]
    assert lines, value
    for line in lines:
        openings = re.findall(
            r"<(?:Subject|Picture|Video|Audio) \d+>(?= is | \(|:)", line)
        assert len(openings) == 1, line


def test_no_scene_fixture_produces_a_blocking_diagnostic(compiled):
    """Every in-scope combination must compile without an integrity error."""
    for name, result in compiled.items():
        assert [value["code"] for value in result["errors"]] == [], name


# --------------------------------------------------------------------------
# Materialization boundary (Writing-Mode Parity, Phase 4)
# --------------------------------------------------------------------------

def test_authored_prose_never_absorbs_a_derived_element(monkeypatch):
    """The materialization boundary, checked against every scene fixture.

    Only `authored` segments may ever be stored as Writing-mode text. So an
    authored segment must not contain an entity label, a shot citation or a
    retention marker: those renumber with staging and the render window, and
    freezing one into stored prose is the ordinal-invariant violation this
    whole phase exists to avoid. A future edit that folds a derived element
    back into the prose body fails here rather than in a user's project.
    """
    captured = []
    original = prompt_context._reference_capability_parts

    def _capture(attachment, capability, context):
        result = original(attachment, capability, context)
        captured.extend(segments for _owner, segments in result)
        return result

    monkeypatch.setattr(prompt_context, "_reference_capability_parts", _capture)
    for build in SCENES.values():
        build()
    assert captured, "no reference lines were assembled"

    markers = set(prompt_context.VISUAL_INTENTS.values()) | set(
        prompt_context.AUDIO_INTENTS.values())
    for segments in captured:
        for segment in segments:
            if not segment.get("authored"):
                continue
            text = segment["text"]
            assert not re.search(r"<[A-Za-z ]+\d+>", text), text
            assert "[Shot " not in text, text
            assert text.strip() not in markers, text


def test_every_line_is_exactly_its_segments(monkeypatch):
    """`segments_text` reproduces the assembled line byte for byte.

    This is the round-trip gate at the assembler seam: the compiler's string
    path is a join over the same segments the materializer consumes, so a
    materialized document cannot drift from what the compiler would have
    emitted. Guards against a future branch that builds a string directly and
    leaves the segment list behind. Inputs are captured during a real compile
    and replayed afterwards, so the two public entry points are compared
    without either one re-entering the capture.
    """
    calls = []
    original = prompt_context._reference_capability_parts

    def _capture(attachment, capability, context):
        calls.append((attachment, capability, context))
        return original(attachment, capability, context)

    monkeypatch.setattr(prompt_context, "_reference_capability_parts", _capture)
    for build in SCENES.values():
        build()
    monkeypatch.undo()
    assert calls, "no reference lines were assembled"

    compared = 0
    for attachment, capability, context in calls:
        parts = prompt_context.reference_capability_segments(
            attachment, capability, context)
        rendered = prompt_context.reference_capability_lines(
            attachment, capability, context)
        assert [owner for owner, _ in parts] == [owner for owner, _ in rendered]
        for (_owner, segments), (_key, line) in zip(parts, rendered):
            assert prompt_context.segments_text(segments) == line
            assert all(segment.get("kind") for segment in segments), segments
            compared += 1
    assert compared, "no lines were compared"


def _anchored_samples(monkeypatch):
    """Every Reference chip the fixtures stage, as an authored channel document.

    Chips are the record and a channel document holds the author's prose plus an
    inline anchor per staged chip. These contracts were previously exercised
    through materialized documents, whose shape was a superset of this one, so
    the FIXTURE moved and the assertions did not: what they pin — anchors
    surviving a template retarget, the persisted mirror staying free of
    ordinals, and a flat overwrite being refused — is unchanged by the pivot.
    """
    calls = []
    original = prompt_context._reference_capability_parts

    def _capture(attachment, capability, context):
        calls.append((attachment, capability, context))
        return original(attachment, capability, context)

    monkeypatch.setattr(prompt_context, "_reference_capability_parts", _capture)
    for build in SCENES.values():
        build()
    monkeypatch.undo()
    samples = []
    for attachment, capability, context in calls:
        lines = prompt_context.reference_capability_lines(
            attachment, capability, context)
        rendered = chr(10).join(text for _owner, text in lines)
        if not rendered.strip():
            continue
        # Two anchors separated by a newline-bearing text node, because that is
        # the shape a channel collapse is most likely to mishandle and the one
        # the old fixture produced by accident rather than by design.
        document = prompt_context.normalize_prompt_document({"nodes": [
            {"type": "text", "node_id": "lead", "text": "She wears "},
            {"type": "attachment", "node_id": "anchor",
             "attachment_id": attachment["attachment_id"]},
            {"type": "text", "node_id": "mid", "text": " in the opening shot."
             + chr(10) + "Later, "},
            {"type": "attachment", "node_id": "anchor2",
             "attachment_id": attachment["attachment_id"]},
            {"type": "text", "node_id": "tail", "text": " returns."},
        ]})
        samples.append((document, rendered, attachment, capability, context))
    assert samples, "no anchored documents"
    return samples


H3_KEYS = ["subject_definitions", "summary", "retention_analysis",
           "detailed_description", "overall_soundscape", "non_diegetic_music"]
LEGACY_KEYS = ["visual", "speech", "sounds"]


def test_the_channels_mirror_carries_prose_but_never_an_ordinal(monkeypatch):
    """`channels` is PERSISTED in project.json, so it must stay unrendered.

    The mirror is `prompt_document_text` over the channel documents, so it can
    only ever hold what the author typed. The failure it guards against is the
    mirror learning to RENDER its anchors: every one of these documents holds a
    chip whose compiled line carries an ordinal, so if the mirror ever composed
    instead of transcribing, `<Subject 1>` and `[Shot 1]` would land on disk.

    Convert to prose is the other half of the ordinal-at-rest guard and has its
    own test; this one pins that the mirror never invents an ordinal the document
    does not contain.
    """
    ordinal = re.compile(r"<[A-Za-z ]+\d+>")
    leakable = 0
    for document, rendered, _attachment, _capability, _context in _anchored_samples(
            monkeypatch):
        mirror = prompt_context.channel_document_mirrors(
            {"detailed_description": document})["detailed_description"]
        # The author's prose is there...
        assert "She wears " in mirror and "opening shot" in mirror, mirror
        # ...and nothing the compiler derives is.
        assert not ordinal.search(mirror), mirror
        assert "[Shot " not in mirror, mirror
        if ordinal.search(rendered) or "[Shot " in rendered:
            leakable += 1
    # Without this the suite could pass on documents whose lines never carried
    # an ordinal at all, which would prove nothing about the invariant.
    assert leakable, "no sample could have leaked an ordinal"


def test_anchors_survive_a_template_retarget(monkeypatch):
    """Switching channel template must not strip an inline anchor.

    The collapse is deliberately lossy for TEXT, but it is document-aware:
    losing anchors here would silently drop the author's staged chips on an
    ordinary template switch, leaving prose that refers to a Reference the
    prompt no longer conditions on.
    """
    for document, _rendered, _attachment, _capability, _context in _anchored_samples(
            monkeypatch):
        before = [node.get("attachment_id") for node in document["nodes"]
                  if node["type"] == "attachment"]
        assert before
        documents = {key: prompt_context.text_document("") for key in H3_KEYS}
        documents["subject_definitions"] = document
        narrowed = prompt_context.retarget_channel_documents(
            documents, H3_KEYS, LEGACY_KEYS)
        widened = prompt_context.retarget_channel_documents(
            narrowed, LEGACY_KEYS, H3_KEYS)
        after = [node.get("attachment_id")
                 for value in widened.values()
                 for node in prompt_context.normalize_prompt_document(
                     value)["nodes"]
                 if node["type"] == "attachment"]
        assert after == before


def test_an_anchored_document_refuses_a_flat_text_overwrite(monkeypatch):
    """A document holding chips must not be silently replaced by plain text.

    `replace_document_text` refuses when a document holds anchors; flattening
    one would delete the author's staged References while appearing to be an
    ordinary text edit.
    """
    for document, _rendered, _attachment, _capability, _context in _anchored_samples(
            monkeypatch):
        with pytest.raises(ValueError):
            prompt_context.replace_document_text(document, "flattened")


def test_copy_classifies_ordinals_instead_of_refusing(monkeypatch):
    """Every offered Reference contribution copies, with truthful disclosure.

    Copy itself does not write project data, so an ordinal no longer refuses the
    whole action. It must be classified as `frozen_ordinal`, while markers and
    task prefixes that merely stop tracking intent are `frozen_static`.
    """
    ordinal = re.compile(r"<[A-Za-z ]+\d+>|\[Shot \d+\]|\(S\d+\)")
    copied = 0
    ordinal_parts = 0
    static_parts = 0
    for _document, _rendered, attachment, capability, context in _anchored_samples(
            monkeypatch):
        plan = prompt_context.copy_capability_plan(
            attachment, capability, context)
        assert not plan["refused"], plan
        copied += bool(plan["lines"])
        for line in plan["lines"]:
            for part in line["parts"]:
                freeze_class = part.get("freeze_class")
                if not freeze_class:
                    continue
                if freeze_class == "ordinal":
                    ordinal_parts += 1
                    assert ordinal.search(part["text"]), part
                    assert part["text"].strip() in plan["frozen_ordinal"]
                else:
                    static_parts += 1
                    assert freeze_class == "static", part
                    assert not ordinal.search(part["text"]), part
                    assert part["text"].strip() in plan["frozen_static"]
    # Every arm is exercised, so classifying everything into one bucket cannot
    # satisfy this test.
    assert copied, "nothing copied"
    assert ordinal_parts, "no ordinal disclosure was exercised"
    assert static_parts, "no static disclosure was exercised"


def test_copy_keeps_the_entity_and_its_sources_live(monkeypatch):
    """A copied definition prefers live handles for identity and sources.

    `label` and `sources` are the two derived kinds with a live spelling, so
    they leave as handle references carrying stable ids rather than as the text
    `<Subject 1>` / `from <Picture 1>`. Emitting ids is also what keeps handle
    resolution in one place instead of duplicating it server-side.
    """
    seen_handle = 0
    seen_sources = 0
    empty_sources = 0
    for _document, _rendered, attachment, capability, context in _anchored_samples(
            monkeypatch):
        plan = prompt_context.copy_capability_plan(
            attachment, capability, context)
        if plan["refused"]:
            continue
        for line in plan["lines"]:
            for part in line["parts"]:
                if part["kind"] == "handle":
                    # A handle with no id would resolve to nothing and the
                    # copy would silently drop the entity.
                    assert part["source"] == "unit"
                    assert part["id"], part
                    seen_handle += 1
                if part["kind"] == "sources":
                    seen_sources += 1
                    # `all([])` is TRUE, so asserting only this let an EMPTY
                    # sources part through — and the browser join then wrote
                    # " from  and @undefined" into the author's prompt. Count
                    # the empty ones instead of assuming they cannot happen.
                    if not part["member_ids"]:
                        empty_sources += 1
                        # The precondition the browser's zero-length branch
                        # rests on: a sources part with no members also renders
                        # to nothing, so skipping it can never drop text the
                        # author would have seen. Joining it instead is what
                        # produced " from  and @undefined".
                        assert not part["rendered"].strip(), part
                        continue
                    # Index-parallel with the labels it stands for, and every
                    # member named rather than a rendered string.
                    assert all(part["member_ids"]), part
    assert seen_handle, "no definition carried an entity handle"
    assert seen_sources, "no definition carried its sources"
    # An empty sources part is REACHABLE outside these fixtures — a Subject with
    # no visual source — and the assertion above pins what makes skipping it
    # safe. The checklist never stages a voice-only Subject, so the count here
    # is not asserted; the browser guard carries a manual row instead.


def test_copy_classifies_numbers_with_no_live_spelling(monkeypatch):
    """Numbers without a live spelling are ordinal, never static.

    A retention line cites `[Shot 2]` and a speech line carries `(S1)`. Both are
    ordinals; neither has a handle spelling reachable from the segment. Copy
    carries the rendered number, while naming the stronger consequence so the
    author knows a paste can go stale.
    """
    ordinal_parts = []
    static_parts = []
    for _document, _rendered, attachment, capability, context in _anchored_samples(
            monkeypatch):
        plan = prompt_context.copy_capability_plan(
            attachment, capability, context)
        assert not plan["refused"], plan
        ordinal_parts.extend(plan["frozen_ordinal"])
        static_parts.extend(plan["frozen_static"])
    assert any("[Shot " in value for value in ordinal_parts), ordinal_parts
    assert any("(S" in value for value in ordinal_parts), ordinal_parts
    assert any(re.search(r"<(?:Picture|Video|Audio) \d+>", value)
               for value in ordinal_parts), ordinal_parts
    assert static_parts, "the fixtures must exercise staged-intent disclosure"
    assert not any("[Shot " in value or "(S" in value
                   or re.search(r"<[A-Za-z ]+\d+>", value)
                   for value in static_parts), static_parts


def test_the_source_member_sidecar_stays_aligned_with_its_labels():
    """`unit_source_labels` dedupes on the LABEL, so the pair can drift.

    One rendered label can be reached by more than one member. If only the label
    list skips the duplicate, every index after it names the wrong member, and a
    converted source resolves to a Reference the author never staged. The two
    lists are built together for exactly this reason.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a"),
        ReferenceMember(member_id="mq", asset_id="img_b")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"},
                               {"entity_id": "e", "member_id": "mq",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "sources": [{"entity_id": "e", "member_id": "mp"},
                          {"entity_id": "e", "member_id": "mq"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes,
                        units=units)
    labels = resolved["unit_source_labels"]["u"]
    members = resolved["unit_source_members"]["u"]
    assert labels, "fixture produced no source labels"
    # One entry per label, in the same order, naming a real member.
    assert len(members) == len(labels)
    assert all(member for member in members)
    assert set(members) <= {"mp", "mq"}


def test_copy_plan_through_the_real_compile_keeps_the_entity_and_sources():
    """Convert, through the wiring rather than the pure function.

    The pure `copy_capability_plan` was already covered — and it passed the
    whole time the feature was broken. The defect lived in the WIRING: the plan
    was built from a context assembled by hand, and `_reference_capability_parts`
    reads `semantic_units_by_id` and `profile`, which `compile_prompt_context`
    injects during compilation. A hand-built context has neither, so
    `declared_label` returned "", the definition branch was never taken, and
    every Convert returned an empty plan that the browser treated as success:
    the capability was disabled and no prose was written.

    Asserting merely "non-empty" would NOT have caught it either. With a chip
    definition override the broken path still produced one text part, having
    silently dropped the entity handle and the source list — a live-to-static
    degradation on a one-way action. So this asserts the SHAPE: handle, prose,
    sources.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a",
                        prompt="the young woman with long dark hair")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    chip = _reference_chip("chip-1", {"semantic_unit_ids": ["u"]},
                           {"definition": "a woman in a red coat"},
                           capabilities=("definitions",))
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    sections = [PromptSection(0, WINDOW_END,
                              channels={"detailed_description": "She looks up."},
                              attachments=[chip])]

    compiled = prompt_context.compile_prompt_context(
        sections=sections, window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_picture_ordinals": resolved.get("unit_picture_ordinals", {}),
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": units,
        }, labels_on=True,
        copy_plan_for={"attachment_id": "chip-1", "capability_id": "definitions"})

    plan = compiled["copy_plan"]
    assert not plan["refused"], plan
    assert plan["lines"], "the wiring produced no lines; this is the shipped defect"
    parts = plan["lines"][0]["parts"]
    kinds = [part["kind"] for part in parts]
    # The entity leaves as a HANDLE keyed by a stable id, not as `<Subject 1>`.
    assert "handle" in kinds, parts
    handle = next(part for part in parts if part["kind"] == "handle")
    assert handle["source"] == "unit" and handle["id"] == "u", handle
    # The author's prose is carried through...
    assert any(part["kind"] == "text" and "red coat" in part["text"]
               for part in parts), parts
    # ...and the sources come with it, naming members rather than labels. The
    # broken path produced the prose alone, which is why "non-empty" is not
    # a sufficient assertion here.
    assert "sources" in kinds, parts
    sources = next(part for part in parts if part["kind"] == "sources")
    assert sources["member_ids"] == ["mp"], sources


def test_copy_plan_refuses_a_capability_that_is_not_staged():
    """A Convert request naming something absent must decline, not crash.

    The lookup deliberately searches the compiled state rather than trusting the
    request, so a stale panel cannot convert a capability the author can no
    longer see.
    """
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channels={"detailed_description": "She looks up."})],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        copy_plan_for={"attachment_id": "nope", "capability_id": "definitions"})
    plan = compiled["copy_plan"]
    assert plan["refused"]
    assert not plan["lines"] and not plan["frozen_static"]
    assert not plan["frozen_ordinal"]


def test_copy_locator_uses_compiler_capability_authorities():
    """Sparse, preserved, and synthesized capabilities remain locatable."""
    profile = H3_PROFILE
    sparse = prompt_context.normalize_attachment({
        "attachment_id": "sparse", "kind": "reference", "capabilities": [],
    })
    located = prompt_context._locate_capability(
        {"attachment_id": "sparse", "capability_id": "definitions"},
        [sparse], [], profile, H3_KEYS)
    assert located and located[1]["kind"] == "definitions", located

    preserved = prompt_context.normalize_attachment({
        "attachment_id": "preserved", "kind": "reference",
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "placement": "future_phase"}],
    })
    assert prompt_context._capabilities(preserved, profile) == []
    located = prompt_context._locate_capability(
        {"attachment_id": "preserved", "capability_id": "definitions"},
        [preserved], [], profile, H3_KEYS)
    assert located and located[1]["placement"] == "future_phase", located

    scope_link = prompt_context.normalize_attachment({
        "attachment_id": "scope", "kind": "prompt_link_scope",
        "source": {"prompt_id": "earlier", "channel_keys": ["summary"]},
        "capabilities": [],
    })
    located = prompt_context._locate_capability(
        {"attachment_id": "scope",
         "capability_id": "prompt_link_scope:summary"},
        [], [PromptSection(0, WINDOW_END, attachments=[scope_link])], profile,
        H3_KEYS)
    assert located, "the synthesized per-channel capability was invisible"
    assert located[1]["capability_id"] == "prompt_link_scope:summary", located
    assert prompt_context._locate_capability(
        {"attachment_id": "scope",
         "capability_id": "prompt_link_scope:detailed_description"},
        [], [PromptSection(0, WINDOW_END, attachments=[scope_link])], profile,
        H3_KEYS
    ) is None
    assert prompt_context._locate_capability(
        {"attachment_id": "scope",
         "capability_id": "prompt_link_scope:not_a_template_channel"},
        [], [PromptSection(0, WINDOW_END, attachments=[scope_link])], profile,
        H3_KEYS
    ) is None

    compatible = copy.deepcopy(profile)
    compatible["template_id"] = "standard"
    compatible["compatible_templates"] = ["standard", "minimax_h3_ref"]
    located = prompt_context._locate_capability(
        {"attachment_id": "scope",
         "capability_id": "prompt_link_scope:summary"},
        [], [PromptSection(0, WINDOW_END, attachments=[scope_link])], compatible,
        H3_KEYS)
    assert located, "the active compatible template, not the primary, owns channels"


def test_a_converted_mention_renders_where_the_author_put_it():
    """What Convert's minted chips must compile to, end to end.

    Convert writes prose into the channel the contribution came from and
    replaces each named entity with a real Reference chip. Those chips have to
    render AT THEIR ANCHOR, inside that sentence. Two things are required and
    the shipped build had neither, so the compiled prompt read
    `" is the korean woman ... from "` with both handle positions empty:

    1. the capability must be the one the format declares `inline` — a
       section-prefix capability emits its own line and nothing at the caret; and
    2. its route must EQUAL the channel it is anchored in. `_route_for` answers
       with the capability's DECLARED channel, and `mentions` declares
       `detailed_description`, so seeding the kind alone routes the token out of
       `subject_definitions` into the prose channel — a different empty result
       reached the same way.

    Asserting only that `<Subject 1>` appears SOMEWHERE would pass on that
    routed-away case, which is why this pins the sentence.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a",
                        prompt="the young woman with long dark hair")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    # Exactly what `convertContributionToProse` mints: the inline kind, pinned
    # to the channel the author converted in.
    chip = prompt_context.normalize_attachment({
        "attachment_id": "minted", "kind": "reference",
        "provider_id": "minimax_h3_ref", "provider_version": "1",
        "source": {"semantic_unit_ids": ["u"]}, "config": {},
        "capabilities": [{"capability_id": "mentions", "kind": "mentions",
                          "channel_key": "subject_definitions",
                          "placement": "inline"}],
    })
    document = {"nodes": [
        {"type": "attachment", "attachment_id": "minted"},
        {"type": "text", "text": " is the korean woman."},
    ]}
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channel_docs={"subject_definitions": document},
                                attachments=[chip])],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_picture_ordinals": resolved.get("unit_picture_ordinals", {}),
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": units,
        }, labels_on=True)

    definitions = compiled["channels"]["subject_definitions"]
    # The token stands where the chip was anchored, inside the author's sentence.
    assert "<Subject 1> is the korean woman." in definitions, definitions
    # ...and not merely present somewhere while the sentence keeps its hole.
    assert " is the korean woman." not in definitions.replace(
        "<Subject 1> is the korean woman.", ""), definitions
    # An inline capability emits ONLY at its anchor, so nothing was also added
    # as a separate line above or below it.
    assert definitions.strip() == "<Subject 1> is the korean woman.", definitions
    # And the token did not leak into the channel `mentions` declares.
    assert "<Subject 1>" not in compiled["channels"].get(
        "detailed_description", ""), compiled["channels"]


def test_a_converted_mention_that_only_seeds_its_kind_routes_away():
    """The discrimination half of the guard above, kept as documentation.

    Same chip with the `channel_key`/`placement` pins removed — the record the
    first fix attempt would have written. `mentions` then routes to its declared
    `detailed_description`, so `subject_definitions` keeps the hole and the
    token surfaces in a channel the author never wrote in. Pinned so that
    "sparse records are always better" cannot be re-adopted here.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a",
                        prompt="the young woman with long dark hair")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "order": 0,
              "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    chip = prompt_context.normalize_attachment({
        "attachment_id": "minted", "kind": "reference",
        "provider_id": "minimax_h3_ref", "provider_version": "1",
        "source": {"semantic_unit_ids": ["u"]}, "config": {},
        "capabilities": [{"capability_id": "mentions", "kind": "mentions"}],
    })
    document = {"nodes": [
        {"type": "attachment", "attachment_id": "minted"},
        {"type": "text", "text": " is the korean woman."},
    ]}
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channel_docs={"subject_definitions": document},
                                attachments=[chip])],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_picture_ordinals": resolved.get("unit_picture_ordinals", {}),
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": units,
        }, labels_on=True)
    assert "<Subject 1>" not in compiled["channels"]["subject_definitions"]


def _handle_prose_scene(text, *, unit_handle="KWoman", member_handle="Sheet",
                        attachments=()):
    """One staged Subject and one staged Picture, with prose instead of chips."""
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a", handle=member_handle,
                        prompt="the young woman with long dark hair")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "handle": unit_handle,
              "order": 0, "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    document = {"nodes": [{"type": "text", "text": text}]}
    return prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channel_docs={"detailed_description": document},
                                attachments=list(attachments))],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_picture_ordinals": resolved.get("unit_picture_ordinals", {}),
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": units,
            "references": [entity.to_dict() if hasattr(entity, "to_dict") else entity],
        }, labels_on=True)


def test_a_handle_written_in_prose_resolves_where_it_sits():
    """The whole point: a mention is text now, and text renders in place.

    A handle carries no routing of its own, so it cannot be misrouted the way a
    chip could — it emits at the position the author typed it, in the channel
    they typed it in. That is exactly the `mentions` semantics the compiler
    already describes as "authored placements", which is why prose is allowed to
    express it and nothing else.
    """
    compiled = _handle_prose_scene("a @KWoman walks past the window")
    described = compiled["channels"]["detailed_description"]
    assert "<Subject 1> walks past the window" in described, described
    # The handle spelling is GONE, replaced in place — not appended, not
    # duplicated, and not left beside its own resolution.
    assert "@KWoman" not in described, described


def test_a_possessive_handle_keeps_its_suffix():
    """`@KWoman's jacket` is a mention plus prose, not a handle named KWomans."""
    described = _handle_prose_scene(
        "@KWoman's jacket is red")["channels"]["detailed_description"]
    assert described.startswith("<Subject 1>'s jacket is red"), described


def test_case_does_not_change_what_a_handle_names():
    """Handle uniqueness is enforced case-insensitively when one is created.

    So two spellings cannot name two different things, and prose must not be the
    one surface that pretends they can.
    """
    described = _handle_prose_scene(
        "@kwoman and @KWOMAN")["channels"]["detailed_description"]
    assert described == "<Subject 1> and <Subject 1>", described


def test_an_unmatched_handle_is_left_exactly_as_written():
    """Three cases that must not be told apart, and must never half-resolve.

    An `@` that was never a handle; a handle for a Reference that does not exist
    yet — late binding, which is what lets prose be drafted before the
    References are staged and wire itself up when they are; and a handle whose
    entity is not staged in this window. Rendering a partial label in any of
    them puts a number in the prompt that staging does not agree with.
    """
    described = _handle_prose_scene(
        "write to bob@example about @Nobody at @mail.com"
    )["channels"]["detailed_description"]
    assert described == "write to bob@example about @Nobody at @mail.com", described


def test_a_physical_member_handle_resolves_to_its_population_label():
    """Both namespaces work in prose, not only semantic identities."""
    described = _handle_prose_scene(
        "framed like @Sheet")["channels"]["detailed_description"]
    assert "<Picture 1>" in described, described
    assert "@Sheet" not in described, described


def test_a_handle_resolves_in_every_physical_population_not_only_the_first():
    """Which population a member belongs to is the manifest's answer.

    `_handle_sources` walked every member under each declaration in turn and
    kept the first claim, so with H3 declaring pictures, videos and standalone
    audio in that order, EVERY member handle was filed as a picture. A video or
    audio handle then resolved against `ordinal_manifest["pictures"]`, missed,
    produced no label, and shipped to the model as literal `@text`.

    That failure is invisible by construction: it is indistinguishable from the
    deliberate silence for a handle that is not staged or does not exist. So
    this stages one member of each kind and asserts all three, rather than
    asserting "a physical handle works" from a picture alone.
    """
    entity = ReferenceEntity(reference_id="e", name="Cast", members=[
        ReferenceMember(member_id="mp", asset_id="img_a", handle="Sheet",
                        prompt="the young woman"),
        ReferenceMember(member_id="mv", asset_id="vid_a", handle="Clip",
                        prompt="the walk cycle"),
        ReferenceMember(member_id="ma", asset_id="aud_a", handle="Track",
                        prompt="the room tone")])
    recipes = [
        ReferenceLaneRecipe(lane_id="lp", media_kind="image", recipe=PICTURE_RECIPE),
        ReferenceLaneRecipe(lane_id="lv", media_kind="video", recipe=VIDEO_RECIPE),
        ReferenceLaneRecipe(lane_id="la", media_kind="audio", recipe=AUDIO_RECIPE)]
    items = [
        ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "mp",
                           "role": "identity", "visual_intent": "preserve"}]),
        ReferenceItem(reference_item_id="iv", lane_index=1, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "mv",
                           "role": "identity", "visual_intent": "preserve"}]),
        ReferenceItem(reference_item_id="ia", lane_index=2, start_frame=0,
                      end_frame=WINDOW_END, members=[
                          {"entity_id": "e", "member_id": "ma",
                           "role": "identity", "audio_intent": "reference"}])]
    resolved = _resolve(setup={"mode": "reference"}, entities=[entity],
                        items=items, recipes=recipes, units=[])
    document = {"nodes": [{"type": "text",
                           "text": "pic @Sheet vid @Clip aud @Track"}]}
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channel_docs={"detailed_description": document})],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_picture_ordinals": resolved.get("unit_picture_ordinals", {}),
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": [],
            # Dicts, not dataclasses: `_members_by_id` calls `.get`.
            "references": [entity.to_dict()],
        }, labels_on=True)
    described = compiled["channels"]["detailed_description"]
    # Every population, not just the one declared first.
    assert "<Picture 1>" in described, described
    assert "<Video 1>" in described, described
    assert "<Audio 1>" in described, described
    # And nothing was left as literal text.
    assert "@" not in described, described


MENTION_PARITY_CASES = [
    # (capability kind, chip config, what the rendered line should contain)
    ("mentions", {}, "<Subject 1>"),
    ("mentions", {"text": "the woman in red"}, "the woman in red"),
    ("summary", {"summary": "A quiet corridor."}, "A quiet corridor."),
    ("summary", {"summary": ""}, ""),
    ("audio_relationship",
     {"audio_relationship": "<Audio 1> is the voice for <Subject 1>."},
     "voice for"),
]


def test_every_copyable_capability_assembles_to_its_rendered_line():
    """Segments must rebuild the rendered line byte for byte.

    `definitions` and `retention` were always segment-assembled, and the
    existing coverage compares their segments against
    `reference_capability_lines` — which is built from the same parts, so it can
    only prove self-consistency. `mentions`, `summary` and `audio_relationship`
    are different: `_render_reference` produces them as flat strings by a
    separate path, and the new builders have to agree with THAT.

    A one-character disagreement is not cosmetic. Copy puts the segment
    assembly on the clipboard while the prompt carries the rendered line, so a
    drift means pasting something subtly different from what the chip emits —
    silently, and only visible by comparing two prompts.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a", handle="Sheet",
                        prompt="the young woman with long dark hair")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "handle": "KWoman",
              "order": 0, "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    context = {
        "setup_manifest": resolved["setup_manifest"],
        "ordinal_manifest": resolved["ordinal_manifest"],
        "unit_source_labels": resolved.get("unit_source_labels", {}),
        "unit_source_members": resolved.get("unit_source_members", {}),
        "semantic_units_by_id": {u["semantic_unit_id"]: u for u in units},
        "references": [entity.to_dict()],
        "profile": prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"],
    }
    compared = 0
    for capability_kind, config, expected in MENTION_PARITY_CASES:
        chip = _reference_chip("chip-1", {"semantic_unit_ids": ["u"]}, dict(config),
                               capabilities=(capability_kind,))
        capability = next(value for value in chip["capabilities"]
                          if value["capability_id"] == capability_kind)
        rendered = prompt_context._render_reference_capability(
            chip, capability, context)
        parts = prompt_context.reference_capability_segments(chip, capability, context)
        assembled = "".join(
            prompt_context.segments_text(segments) for _owner, segments in parts)
        assert assembled == rendered, (capability_kind, config, assembled, rendered)
        if expected:
            assert expected in rendered, (capability_kind, rendered)
        compared += 1
    assert compared == len(MENTION_PARITY_CASES)


def test_a_mention_carries_the_id_that_spells_it_live():
    """Copy must be able to write a handle, not a number.

    A mention's labels are `<Subject 1>` / `<Picture 1>` — ordinals, which the
    invariant forbids putting into stored text. Each therefore has to arrive as
    a LABEL segment carrying the id behind it, so the browser can resolve the
    handle. A segment with the right TEXT and no id is the silent failure: Copy
    would fall back to freezing the number.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a", handle="Sheet",
                        prompt="the young woman")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "handle": "KWoman",
              "order": 0, "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    context = {
        "setup_manifest": resolved["setup_manifest"],
        "ordinal_manifest": resolved["ordinal_manifest"],
        "unit_source_labels": resolved.get("unit_source_labels", {}),
        "unit_source_members": resolved.get("unit_source_members", {}),
        "semantic_units_by_id": {u["semantic_unit_id"]: u for u in units},
        "references": [entity.to_dict()],
        "profile": prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"],
    }
    # A chip naming BOTH namespaces at once.
    chip = _reference_chip(
        "chip-1", {"semantic_unit_ids": ["u"], "picture_ids": ["mp"]}, {},
        capabilities=("mentions",))
    capability = chip["capabilities"][0]
    parts = prompt_context.reference_capability_segments(chip, capability, context)
    segments = parts[0][1]
    labels = [seg for seg in segments
              if seg["kind"] == prompt_context.SEGMENT_LABEL]
    assert [seg["text"] for seg in labels] == ["<Subject 1>", "<Picture 1>"], segments
    # The semantic identity carries its unit id, the physical member its member
    # id — the two spellings a handle can have.
    assert labels[0].get("unit_id") == "u", labels[0]
    assert labels[1].get("member_id") == "mp", labels[1]
    # Neither is a bare text segment, which is what would let a number freeze.
    assert all(seg.get("unit_id") or seg.get("member_id") for seg in labels), labels
    # And the assembly still rebuilds the rendered line exactly — the separator
    # between two labels is part of that line, so dropping it is a silent drift
    # that a single-label case cannot see.
    rendered = prompt_context._render_reference_capability(chip, capability, context)
    assert prompt_context.segments_text(segments) == rendered, (segments, rendered)
    assert rendered == "<Subject 1> <Picture 1>", rendered


def test_a_copy_plan_spells_a_mention_as_handles_in_both_namespaces():
    """Copy must never put an ordinal on the clipboard.

    A mention renders `<Subject 1> <Picture 1>` — two numbers that staging can
    change. Both have a live spelling: the identity through its unit, the
    physical member through its own handle. The plan therefore returns two
    HANDLE parts with the ids behind them, and the browser resolves the
    spellings, which is the same division of labour the definition path already
    uses.

    A physical DEFINITION label still blocks, and that is not inconsistent: its
    owner is keyed by the rendered label itself, so there is no id to hand back.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a", handle="Sheet",
                        prompt="the young woman")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "handle": "KWoman",
              "order": 0, "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    chip = _reference_chip(
        "chip-1", {"semantic_unit_ids": ["u"], "picture_ids": ["mp"]}, {},
        capabilities=("mentions",), placement="inline")
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channels={"detailed_description": "She looks up."},
                                attachments=[chip])],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": units,
            "references": [entity.to_dict()],
        }, labels_on=True,
        copy_plan_for={"attachment_id": "chip-1", "capability_id": "mentions"})
    plan = compiled["copy_plan"]
    assert not plan["refused"], plan
    parts = plan["lines"][0]["parts"]
    handles = [part for part in parts if part["kind"] == "handle"]
    assert [(h["source"], h["id"]) for h in handles] == [
        ("unit", "u"), ("member", "mp")], parts
    # The rendered ordinals ride along for the refusal message only; nothing in
    # the plan asks the browser to write them.
    assert [h["rendered"] for h in handles] == ["<Subject 1>", "<Picture 1>"], parts
    assert not any(part["kind"] == "text" and "<" in part["text"]
                   for part in parts), parts


def test_an_unresolved_handle_is_reported_without_blocking():
    """Silence was a typo's only signal.

    Prose may name a handle before its Reference exists — that is the late
    binding an LLM-drafted scene depends on — so this cannot be an error. But a
    typo (`@KWomn`) is indistinguishable from that by construction, and without
    a diagnostic it reached the model as literal text with nothing anywhere
    saying so. A handle carries no `attachment_id`, so the advisory is
    channel-level, like the one the token grammar beside it already uses.
    """
    compiled = _handle_prose_scene("a @KWoman meets @Nobody at bob@example")
    codes = [w.get("code") for w in compiled["warnings"]]
    assert "unresolved_handle_mention" in codes, compiled["warnings"]
    row = next(w for w in compiled["warnings"]
               if w["code"] == "unresolved_handle_mention")
    # Only the unresolved one is named. `@KWoman` resolved, and `bob@example`
    # was never a mention.
    assert "@Nobody" in row["message"], row
    assert "@KWoman" not in row["message"], row
    assert "bob" not in row["message"], row
    assert row["channel_key"] == "detailed_description", row
    # Non-blocking: nothing here may stop a render.
    assert not compiled["errors"], compiled["errors"]


def test_prose_whose_handles_all_resolve_says_nothing():
    """An advisory that always fires is noise, and noise is ignored."""
    compiled = _handle_prose_scene("a @KWoman walks past")
    assert not [w for w in compiled["warnings"]
                if w.get("code") == "unresolved_handle_mention"], compiled["warnings"]


def test_authored_ordinals_report_resolved_and_unresolved_numbers():
    """The paste advisory compares every spelling with the live manifest."""
    compiled = _handle_prose_scene(
        ("<Picture 1> and <Picture 2>; [Shot 1] then [Shot 2]; "
         "(S1), (S1,S2), not (S1,S3)"),
        attachments=[prompt_context.shot_attachment(),
                     _vocal_event("voice-1", voice_id="one"),
                     _vocal_event("voice-2", voice_id="two")])
    rows = [value for value in compiled["warnings"]
            if value.get("code") == "authored_ordinal_literal"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["channel_key"] == "detailed_description", row
    for spelling in ("<Picture 1>", "[Shot 1]", "(S1)", "(S1,S2)"):
        assert f"{spelling} currently resolves to staged input" in row["message"], row
    for spelling in ("<Picture 2>", "[Shot 2]", "(S1,S3)"):
        assert (f"{spelling} does not resolve to anything currently staged"
                in row["message"]), row


def test_authored_ordinal_patterns_include_assetless_and_escape_templates():
    """Identity populations stay distinct; regex syntax and bare labels are safe."""
    profile = copy.deepcopy(H3_PROFILE)
    identity = profile["identity_kinds"][0]
    identity["referenced_label_template"] = "<Subject {n}>"
    identity["assetless_label_template"] = "[Hero {n}]"
    found = prompt_context.authored_ordinal_literals(
        "<Subject 1> <Subject 2> [Hero 1] [Hero 2]", profile,
        {"ordinal_manifest": {"subjects": {"referenced": 1, "assetless": 2}},
         "semantic_units_by_id": {
             "referenced": {"sources": [{"member_id": "m"}]},
             "assetless": {"sources": []},
         }})
    assert found == {
        "<Subject 1>": True, "<Subject 2>": False,
        "[Hero 1]": False, "[Hero 2]": True,
    }

    # Repeating the same declaration placeholder is valid and denotes one
    # ordinal. The scanner must use a backreference, not duplicate a named
    # regex group and crash the entire compile.
    profile["physical_populations"][0]["label_template"] = "<Picture {n}/{n}>"
    repeated = prompt_context.authored_ordinal_literals(
        "<Picture 3/3> <Picture 3/4>", profile,
        {"ordinal_manifest": {"pictures": {"member": 3}},
         "semantic_units_by_id": {}})
    assert repeated == {"<Picture 3/3>": True}

    profile["speaker_policy"]["token_template"] = "(S{n}/{n})"
    compound = prompt_context.authored_ordinal_literals(
        "(S1/1,S2/2) (S1/2,S2/2)", profile,
        {"speaker_order": {"voice:two": 2}, "ordinal_manifest": {},
         "semantic_units_by_id": {}})
    assert compound == {"(S1/1,S2/2)": False}

    profile["speaker_policy"].update({
        "token_template": "(S/{n})", "compound_join": "/"})
    joiner_collision = prompt_context.authored_ordinal_literals(
        "(S/1/S/2)", profile,
        {"speaker_order": {"voice:one": 1, "voice:two": 2},
         "ordinal_manifest": {}, "semantic_units_by_id": {}})
    assert joiner_collision == {"(S/1/S/2)": True}

    profile["speaker_policy"].update({
        "token_template": "(S{n})", "compound_join": ","})
    many_speakers = "(" + ",".join(["S1"] * 1200) + ")"
    assert prompt_context.authored_ordinal_literals(
        many_speakers, profile,
        {"speaker_order": {"voice:one": 1}, "ordinal_manifest": {},
         "semantic_units_by_id": {}}) == {many_speakers: True}

    # An unwrapped token makes the compound and simple patterns both match.
    # Re-scanning every prior compound span for every simple token was O(n²)
    # and blocked the synchronous candidate-compile endpoint for seconds.
    profile["speaker_policy"]["token_template"] = "S{n}"
    repeated_compounds = "S1,S2 " * 5000
    started = time.perf_counter()
    repeated = prompt_context.authored_ordinal_literals(
        repeated_compounds, profile,
        {"speaker_order": {"voice:one": 1, "voice:two": 2},
         "ordinal_manifest": {}, "semantic_units_by_id": {}})
    elapsed = time.perf_counter() - started
    assert repeated == {"S1,S2": True}
    assert elapsed < 1.0, f"repeated compound scan took {elapsed:.3f}s"


def test_copy_refuses_a_capability_whose_contribution_the_render_refuses():
    """The clipboard must not carry what the prompt does not.

    `_render_reference_capability` short-circuits on a capability the format
    does not declare and on one holding a value outside its declared
    vocabulary. The segment builders behind Copy had neither check, so a chip
    whose `summary` names an unknown task type contributed NOTHING to the
    prompt while Copy still offered `[reference generation] body` — text the
    render had refused, handed to the author as if it were live.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a", handle="Sheet",
                        prompt="the young woman")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [{"semantic_unit_id": "u", "name": "Woman", "handle": "KWoman",
              "order": 0, "sources": [{"entity_id": "e", "member_id": "mp"}]}]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    context = {
        "setup_manifest": resolved["setup_manifest"],
        "ordinal_manifest": resolved["ordinal_manifest"],
        "unit_source_labels": resolved.get("unit_source_labels", {}),
        "unit_source_members": resolved.get("unit_source_members", {}),
        "semantic_units_by_id": {u["semantic_unit_id"]: u for u in units},
        "references": [entity.to_dict()],
        "profile": prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"],
    }
    chip = _reference_chip("chip-1", {"semantic_unit_ids": ["u"]},
                           {"summary": "body", "task_types": ["not_a_role"]},
                           capabilities=("summary",))
    capability = chip["capabilities"][0]
    # The render refuses it outright...
    assert prompt_context.reference_capability_errors(
        chip, capability, context), "fixture no longer triggers the refusal"
    assert prompt_context._render_reference_capability(
        chip, capability, context) == ""
    # ...so the segments behind Copy must be empty too, or the two disagree
    # about what this chip contributes.
    assert prompt_context.reference_capability_segments(
        chip, capability, context) == []

    # A stored record naming a capability the ACTIVE format does not declare —
    # reachable whenever a scene changes format, since records are preserved at
    # rest rather than purged. No explicit guard handles this: it is measured
    # here because `effective_reference_config` resolves through the
    # declarations, so an undeclared capability arrives with no config and the
    # builders fall through on their own. Pinned so that stays true rather than
    # being re-guarded by someone who assumes it does not.
    generic = dict(context)
    generic["profile"] = prompt_context.BUILTIN_PROFILES["generic@1"]
    stale = _reference_chip("chip-2", {"semantic_unit_ids": ["u"]},
                            {"text": "left over"}, capabilities=("mentions",))
    stale_capability = stale["capabilities"][0]
    assert "mentions" not in prompt_context._reference_derived_view(
        generic["profile"]), "generic@1 declares mentions now; pick another kind"
    assert prompt_context._render_reference_capability(
        stale, stale_capability, generic) == ""
    assert prompt_context.reference_capability_segments(
        stale, stale_capability, generic) == []


def test_an_identity_handle_that_resolves_to_nothing_is_reported():
    """The one case that used to have no signal whatsoever.

    Physical member handles were scoped to the window by reading the ordinal
    manifest, so an unstaged one fell out of the known map and the advisory
    named it. Semantic units were filed regardless — so a real, correctly
    spelled identity handle with no ordinal in this window produced no label
    (the prose stayed literal) AND no warning, because the advisory saw it in
    the map. Silence was the only thing distinguishing it from a typo.
    """
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img_a", handle="Sheet",
                        prompt="the young woman")])
    recipes = [ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                   recipe=PICTURE_RECIPE)]
    items = [ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                           end_frame=WINDOW_END, members=[
                               {"entity_id": "e", "member_id": "mp",
                                "role": "identity", "visual_intent": "preserve"}])]
    units = [
        {"semantic_unit_id": "u", "name": "Woman", "handle": "KWoman",
         "order": 0, "sources": [{"entity_id": "e", "member_id": "mp"}]},
        # Declared, handled, and sourced from a member nothing stages — so it
        # has no ordinal here and therefore no label.
        {"semantic_unit_id": "u2", "name": "Ghost", "handle": "Ghost",
         "order": 1, "sources": [{"entity_id": "e", "member_id": "absent"}]},
    ]
    resolved = _resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                        entities=[entity], items=items, recipes=recipes, units=units)
    document = {"nodes": [{"type": "text", "text": "a @KWoman meets @Ghost"}]}
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channel_docs={"detailed_description": document})],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": resolved["setup_manifest"],
            "ordinal_manifest": resolved["ordinal_manifest"],
            "unit_source_labels": resolved.get("unit_source_labels", {}),
            "unit_source_members": resolved.get("unit_source_members", {}),
            "semantic_units": units,
            "references": [entity.to_dict()],
        }, labels_on=True)
    described = compiled["channels"]["detailed_description"]
    rows = [w for w in compiled["warnings"]
            if w.get("code") == "unresolved_handle_mention"]
    if "@Ghost" in described:
        # It did not resolve, so it must be reported — the whole point.
        assert rows and "@Ghost" in rows[0]["message"], (described, compiled["warnings"])
        # And the one that DID resolve is not named.
        assert "@KWoman" not in rows[0]["message"], rows[0]
    else:
        # If staging gives it a label after all the fixture is wrong, not the
        # rule — say so rather than passing quietly.
        raise AssertionError(f"fixture resolved @Ghost: {described}")


def test_a_compile_without_a_copy_request_carries_no_plan():
    """The field appears only when asked for; every other compile is unchanged."""
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(0, WINDOW_END,
                                channels={"detailed_description": "She looks up."})],
        window_start=0, window_end=WINDOW_END, fps=24.0,
        template="minimax_h3_ref", profile="minimax_h3_ref@1")
    assert "copy_plan" not in compiled
