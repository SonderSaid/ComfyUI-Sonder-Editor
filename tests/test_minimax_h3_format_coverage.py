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
    assert set(minimax_h3.SETUP_LANE_POPULATIONS.values()) == {
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


@pytest.mark.xfail(strict=True, reason=(
    "PR-16: compound speaker tokens follow the chip's subject selection order, "
    "so a group whose members were numbered out of selection order renders "
    "(S2,S1). base guide 4.4 writes compound ids ascending."))
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
    assert any(value["code"] == "pictures_slot_cap"
               for value in resolved["errors"])


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


def test_round_trip_gate_materialize_then_compile_is_unchanged(monkeypatch):
    """The Phase-4 gate: materializing a line must not change what it compiles to.

    For every reference line every scene fixture produces, materialize it into
    document nodes and read it back. The result must equal the line the
    compiler emits today, byte for byte. This is the check that the authored
    prose stored in the document plus the derived parts recomputed on read
    reconstruct exactly the assembled sentence.
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

    checked = 0
    for attachment, capability, context in calls:
        expected = prompt_context.reference_capability_lines(
            attachment, capability, context)
        if not expected:
            continue
        document = prompt_context.materialize(attachment, capability, context)
        actual = prompt_context.materialized_lines(
            document, attachment, capability, context)
        assert [prompt_context.record_key(owner) for owner, _ in expected] == [
            key for key, _ in actual]
        for (_owner, line), (_key, rebuilt) in zip(expected, actual):
            assert rebuilt == line
            checked += 1
    assert checked, "no lines were round-tripped"


def test_materialized_document_stores_no_derived_element(monkeypatch):
    """What lands in the document is prose only.

    The stored text must not contain a label, a shot citation or a marker —
    those are recomputed on read. If any leaked into a text node the document
    would freeze a fact staging owns, which is the failure the whole phase is
    built to prevent.
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

    # Labels and shot citations are checked by pattern because both have a
    # shape ordinary prose cannot accidentally take. Markers are NOT checked by
    # substring: `reference` is an audio marker value and also a normal English
    # word, and real prose says "the whole-video temporal-structure reference".
    # Marker leakage is pinned instead by
    # `test_authored_prose_never_absorbs_a_derived_element`, which compares a
    # whole stripped segment rather than searching inside one.
    inspected = 0
    for attachment, capability, context in calls:
        lines = prompt_context.reference_capability_lines(
            attachment, capability, context)
        if not lines:
            continue
        document = prompt_context.materialize(attachment, capability, context)
        stored = prompt_context.prompt_document_text(document)
        assert not re.search(r"<[A-Za-z ]+\d+>", stored), stored
        assert "[Shot " not in stored, stored
        inspected += 1
    assert inspected, "no lines were materialized"


def test_a_deleted_prose_hole_does_not_resurrect_the_chip_config(monkeypatch):
    """An emptied hole contributes nothing rather than falling back to config.

    Re-seeding from `config` would restore prose the author deliberately
    deleted, which is the one behavior a detached record must never have.
    The derived parts must still be there — deleting prose removes the
    sentence, not the label it hangs off.
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

    checked = 0
    for attachment, capability, context in calls:
        document = prompt_context.materialize(attachment, capability, context)
        prose = [node for node in document["nodes"]
                 if node["type"] == "text" and node["text"] != chr(10)]
        if not prose:
            continue
        emptied = {"schema": document["schema"],
                   "nodes": [node for node in document["nodes"]
                             if node not in prose]}
        for _key, rebuilt in prompt_context.materialized_lines(
                emptied, attachment, capability, context):
            for run in (node["text"] for node in prose):
                assert run.strip() not in rebuilt or not run.strip(), rebuilt
        checked += 1
    assert checked, "no materialized prose to strip"


def test_detached_prose_survives_a_reseed_and_bound_prose_follows_it(monkeypatch):
    """The Bound/Detached split, which is the whole point of the flag.

    A Bound record still follows its chip config, so a changed default
    reaches it. A Detached record is the author's text and must not be
    overwritten by re-seeding.
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

    checked = 0
    for attachment, capability, context in calls:
        document = prompt_context.materialize(attachment, capability, context)
        keys = [node["record_key"] for node in document["nodes"]
                if node["type"] == "attachment"]
        if not keys:
            continue
        # The author rewrites the first record and detaches it.
        edited = copy.deepcopy(document)
        replaced = False
        for node in edited["nodes"]:
            if node["type"] == "text" and node["text"] != chr(10):
                node["text"] = " is MINE"
                replaced = True
                break
        if not replaced:
            continue
        target = copy.deepcopy(attachment)
        prompt_context.mark_record_detached(target, keys[0])
        assert prompt_context.detached_record_keys(target) == {keys[0]}

        reseeded = prompt_context.rematerialize(
            edited, target, capability, context)
        assert "MINE" in prompt_context.prompt_document_text(reseeded)

        # Clearing the flag lets the seed win again.
        prompt_context.mark_record_detached(target, keys[0], detached=False)
        assert prompt_context.detached_record_keys(target) == set()
        bound = prompt_context.rematerialize(
            edited, target, capability, context)
        assert "MINE" not in prompt_context.prompt_document_text(bound)
        assert (prompt_context.prompt_document_text(bound)
                == prompt_context.prompt_document_text(document))
        checked += 1
    assert checked, "no records were exercised"


def test_a_detached_record_still_renumbers_its_derived_parts(monkeypatch):
    """Detachment owns the prose, never the label.

    Detaching must not freeze the ordinal: the label, shot citation and marker
    stay live for a detached record exactly as for a bound one. Otherwise
    editing one word would silently pin a Subject number.
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

    checked = 0
    for attachment, capability, context in calls:
        document = prompt_context.materialize(attachment, capability, context)
        keys = [node["record_key"] for node in document["nodes"]
                if node["type"] == "attachment"]
        if not keys:
            continue
        target = copy.deepcopy(attachment)
        prompt_context.mark_record_detached(target, keys[0])
        rebuilt = prompt_context.materialized_lines(
            document, target, capability, context)
        expected = prompt_context.reference_capability_lines(
            target, capability, context)
        assert [line for _k, line in rebuilt] == [line for _o, line in expected]
        checked += 1
    assert checked, "no records were exercised"


# --------------------------------------------------------------------------
# Compatibility mirrors (Writing-Mode Parity, Phase 5)
# --------------------------------------------------------------------------

def _materialized_samples(monkeypatch):
    """Every reference line the fixtures produce, as a materialized document."""
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
        document = prompt_context.materialize(attachment, capability, context)
        if any(node["type"] == "attachment" for node in document["nodes"]):
            samples.append((document, attachment, capability, context))
    assert samples, "no materialized documents"
    return samples


H3_KEYS = ["subject_definitions", "summary", "retention_analysis",
           "detailed_description", "overall_soundscape", "non_diegetic_music"]
LEGACY_KEYS = ["visual", "speech", "sounds"]


def test_the_channels_mirror_carries_prose_but_never_an_ordinal(monkeypatch):
    """`channels` is PERSISTED in project.json, so it must stay unrendered.

    Materialized prose newly reaches the mirror — it used to sit in
    `config.definition`, which the mirror never saw. That is the intended
    growth. What must NOT reach it is any derived element: rendering the line
    into the mirror would persist `<Subject 1>` and `[Shot 1]` into the
    project file, which is the ordinal invariant this phase is built around.
    """
    for document, _attachment, _capability, _context in _materialized_samples(
            monkeypatch):
        mirror = prompt_context.channel_document_mirrors(
            {"detailed_description": document})["detailed_description"]
        assert not re.search(r"<[A-Za-z ]+\d+>", mirror), mirror
        assert "[Shot " not in mirror, mirror


def test_materialized_records_survive_a_template_retarget(monkeypatch):
    """Switching channel template must not strip an anchor or its record key.

    The collapse is deliberately lossy for TEXT, but it is document-aware:
    losing anchors here would orphan every materialized record, silently
    turning Bound prose into Free text on an ordinary template switch.
    """
    for document, _attachment, _capability, _context in _materialized_samples(
            monkeypatch):
        before = [(node.get("attachment_id"), node.get("record_key"))
                  for node in document["nodes"]
                  if node["type"] == "attachment"]
        documents = {key: prompt_context.text_document("") for key in H3_KEYS}
        documents["subject_definitions"] = document
        narrowed = prompt_context.retarget_channel_documents(
            documents, H3_KEYS, LEGACY_KEYS)
        widened = prompt_context.retarget_channel_documents(
            narrowed, LEGACY_KEYS, H3_KEYS)
        after = [(node.get("attachment_id"), node.get("record_key"))
                 for value in widened.values()
                 for node in prompt_context.normalize_prompt_document(
                     value)["nodes"]
                 if node["type"] == "attachment"]
        assert after == before


def test_a_materialized_document_refuses_a_flat_text_overwrite(monkeypatch):
    """A materialized record must not be silently replaced by plain text.

    `replace_document_text` already refuses when a document holds anchors;
    this pins that the protection covers materialized documents too, since
    they are exactly the documents whose anchors carry authored prose.
    """
    for document, _attachment, _capability, _context in _materialized_samples(
            monkeypatch):
        with pytest.raises(ValueError):
            prompt_context.replace_document_text(document, "flattened")
