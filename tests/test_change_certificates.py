"""A change certificate says which reads an edit can affect, and it must be right.

`server/change_certificates.py` lets the browser skip the Reference bridge read and
the prompt compile after an edit it certifies as irrelevant to them. A wrong
"unchanged" leaves a stale preview or wrong bridge sockets on screen with nothing
to heal them, so this suite proves the certificate against the READS themselves:

1. **Coverage.** Every serialized Scene field is classified, and every certified
   operation is a real dispatcher operation.
2. **Scope.** Every certified operation -- including its linked branches --
   writes nothing outside the scene it addresses. That is what lets a consumer
   on another scene, or on project-level state, keep its result.
3. **Truth.** For every certified edit, a flag that says "unchanged" is checked
   by running the real candidate compile (two windows) and the real bridge route
   before and after: the outputs must be identical.
4. **Transport.** One certificate per committed transition, identical on the
   response header and on both `project_updated` alias events, bound to the
   committing attempt's base version, and absent for refused, no-op and
   uncertified batches.
"""

import asyncio
import copy
import importlib
import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import change_certificates, prompt_context, routes
from server.project_manager import create_project, load_project, save_project
from server.timeline_state import (
    Asset,
    AudioTrack,
    ClipReference,
    GuideFrame,
    LaneConfig,
    PromptSection,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    Scene,
    TimelineProject,
)

from test_scene_mutation_registration import _dispatcher_op_types

TEMPLATE = "minimax_h3_ref"
PROFILE = "minimax_h3_ref@1"
PICTURE_RECIPE = {"soft": {
    "compatible_profiles": [PROFILE], "physical_population": "pictures",
    "exposed_capabilities": ["definitions", "retention", "mentions"],
    "role_fields": ["role", "visual_intent"]}}
AUDIO_RECIPE = {"soft": {
    "compatible_profiles": [PROFILE], "physical_population": "standalone_audios",
    "exposed_capabilities": ["definitions", "retention", "mentions",
                             "audio_relationship"],
    "role_fields": ["role", "audio_intent"]}}


# ---------------------------------------------------------------------------
# Fixture: one scene every certified operation can meaningfully touch
# ---------------------------------------------------------------------------

def _section(prompt_id, start, end, text, attachments=()):
    section = PromptSection(start, end, channels={"detailed_description": text},
                            attachments=list(attachments))
    section.prompt_id = prompt_id
    return section


def _scene(scene_id="scene"):
    chip = prompt_context.normalize_attachment({
        "attachment_id": "chip", "emission_group_id": "chip", "kind": "reference",
        "provider_id": "minimax_h3_ref", "provider_version": "1",
        "source": {"semantic_unit_ids": ["hero"]}, "config": {},
        "capabilities": [{"capability_id": value, "kind": value,
                          "placement": "section_prefix"}
                         for value in ("definitions", "retention")]})
    scene = Scene(
        scene_id=scene_id, name="Saloon", duration_frames=200, fps=24.0,
        width=640, height=360,
        prompt_context_profile_id=PROFILE,
        video_lane_count=2,
        video_lane_configs=[LaneConfig(name="A"), LaneConfig(name="B")],
        audio_lane_count=1, audio_lane_configs=[LaneConfig(name="Dialogue")],
        reference_lane_count=2,
        reference_lane_configs=[LaneConfig(name="Pictures"), LaneConfig(name="Voice")],
        reference_lane_recipes=[
            ReferenceLaneRecipe(lane_id="L0", media_kind="image", recipe=copy.deepcopy(PICTURE_RECIPE)),
            ReferenceLaneRecipe(lane_id="L1", media_kind="audio", recipe=copy.deepcopy(AUDIO_RECIPE)),
        ],
        reference_items=[
            ReferenceItem(reference_item_id="r0", lane_index=0, start_frame=0, end_frame=-1,
                          members=[{"entity_id": "cast", "member_id": "m_img",
                                    "role": "identity", "visual_intent": "preserve"}]),
            ReferenceItem(reference_item_id="r1", lane_index=1, start_frame=50, end_frame=150,
                          members=[{"entity_id": "cast", "member_id": "m_aud"}]),
        ],
    )
    scene.guide_frames = [GuideFrame(guide_id="g1", frame_index=10, asset_id="img_a"),
                          GuideFrame(guide_id="g2", frame_index=80, asset_id="img_b")]
    scene.clips = [
        ClipReference(clip_id="c1", source_path="media/a.mp4", track_index=0,
                      timeline_start_frame=0, timeline_end_frame=50,
                      source_out_frame=50, total_source_frames=300),
        ClipReference(clip_id="c2", source_path="media/b.mp4", track_index=1,
                      timeline_start_frame=60, timeline_end_frame=100,
                      source_out_frame=40, total_source_frames=300),
    ]
    scene.audio_tracks = [AudioTrack(track_id="t1", source_path="media/b.wav",
                                     timeline_start_frame=60, timeline_end_frame=100,
                                     total_source_frames=300)]
    scene.prompt_sections = [
        _section("p1", 0, 60, "A saloon at dusk.",
                 [prompt_context.shot_attachment(), chip]),
        _section("p2", 60, 100, "The stranger walks in."),
        _section("p3", 120, 200, "The bartender looks up."),
    ]
    scene.linked_item_groups = [{"group_id": "grp", "items": [
        {"type": "clip", "id": "c2"}, {"type": "audio", "id": "t1"},
        {"type": "prompt", "id": "p2"}, {"type": "guide", "id": "g2"}]}]
    return scene


def _project(tmp_path):
    members = [ReferenceMember(member_id="m_img", asset_id="img_a", prompt="a tall stranger"),
               ReferenceMember(member_id="m_aud", asset_id="aud_a", prompt="a low voice")]
    return TimelineProject(
        project_dir=str(tmp_path), project_id="proj", name="Project", fps=24.0,
        scenes=[_scene("scene"), _scene("other")],
        assets=[Asset(asset_id="img_a", asset_type="image"),
                Asset(asset_id="img_b", asset_type="image"),
                Asset(asset_id="aud_a", asset_type="audio", duration_sec=6.0)],
        references=[ReferenceEntity(reference_id="cast", name="Cast", members=members)],
        prompt_semantic_units=[{
            "semantic_unit_id": "hero", "name": "Hero", "kind": "subject",
            "definition": "the stranger",
            "sources": [{"entity_id": "cast", "member_id": "m_img"}]}],
    )


# ---------------------------------------------------------------------------
# The certified-operation battery: (label, operations, expected flags)
# ---------------------------------------------------------------------------

def _clip_expected(clip_id, start, lane):
    return {"clip_id": clip_id, "timeline_start_frame": start,
            "track_index": lane, "role": "render"}


BATTERY = [
    ("unlinked clip move",
     [{"type": "update_clip", "clip_id": "c1", "fields": {"timeline_start_frame": 5}}],
     (False, False)),
    ("unlinked clip mute",
     [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}],
     (False, False)),
    ("linked move reaches a prompt section and a guide",
     [{"type": "update_clip", "clip_id": "c2", "apply_linked": True,
       "fields": {"timeline_start_frame": 70, "timeline_end_frame": 110}}],
     (True, True)),
    ("audio volume",
     [{"type": "update_audio_track", "track_id": "t1", "fields": {"volume": 0.5}}],
     (False, False)),
    ("linked audio move",
     [{"type": "update_audio_track", "track_id": "t1", "apply_linked": True,
       "fields": {"timeline_start_frame": 70, "timeline_end_frame": 110}}],
     (True, True)),
    ("media-lane rename",
     [{"type": "update_lane_config", "lane_type": "video", "lane_index": 1,
       "fields": {"name": "Renamed"}}],
     (False, False)),
    ("media-lane lock",
     [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
       "fields": {"locked": True}}],
     (False, False)),
    ("media-lane colour",
     [{"type": "update_lane_config", "lane_type": "audio", "lane_index": 0,
       "fields": {"color": "#aa0000"}}],
     (False, False)),
    ("Reference-lane rename",
     [{"type": "update_lane_config", "lane_type": "reference", "lane_index": 0,
       "fields": {"name": "Faces"}, "expected": {"lane_id": "L0"}}],
     (False, True)),
    ("Reference-lane lock",
     [{"type": "update_lane_config", "lane_type": "reference", "lane_index": 1,
       "fields": {"locked": True}, "expected": {"lane_id": "L1"}}],
     (False, False)),
    ("Reference-lane hide",
     [{"type": "update_lane_config", "lane_type": "reference", "lane_index": 1,
       "fields": {"hidden": True}, "expected": {"lane_id": "L1"}}],
     (True, True)),
    ("Reference recipe",
     [{"type": "update_lane_config", "lane_type": "reference", "lane_index": 0,
       "expected": {"lane_id": "L0"},
       "fields": {"reference_recipe": {"lane_id": "L0", "media_kind": "image",
                                       "recipe": {"soft": {"physical_population": "pictures",
                                                           "compatible_profiles": [PROFILE]}}}}}],
     (True, True)),
    ("prompt-lane hide",
     [{"type": "update_lane_config", "lane_type": "prompt", "fields": {"hidden": True}}],
     (True, False)),
    ("prompt-lane lock",
     [{"type": "update_lane_config", "lane_type": "prompt", "fields": {"locked": True}}],
     (False, False)),
    ("guide-lane lock",
     [{"type": "update_lane_config", "lane_type": "guide", "fields": {"locked": True}}],
     (False, False)),
    ("legacy whole-array media configs",
     [{"type": "update_lane_configs", "fields": {"video_lane_configs": [
         {"name": "X", "locked": True}, {"name": "Y"}]}}],
     (False, False)),
    ("scene rename",
     [{"type": "update_scene_fields", "fields": {"name": "Cantina"}}],
     (False, True)),
    ("scene width",
     [{"type": "update_scene_fields", "fields": {"width": 1280}}],
     (False, False)),
    ("scene duration",
     [{"type": "update_scene_fields", "fields": {"duration_frames": 240}}],
     (True, True)),
    ("scene fps retime",
     [{"type": "update_scene_fields", "fields": {"fps": 30.0}}],
     (True, True)),
    ("unlinked cut",
     [{"type": "split_clip", "clip_id": "c1", "frame": 20,
       "expected": _clip_expected("c1", 0, 0)}],
     (False, False)),
    ("linked cut divides a prompt section",
     [{"type": "split_clip", "clip_id": "c2", "frame": 90, "apply_linked": True,
       "expected": _clip_expected("c2", 60, 1)}],
     (True, False)),
    ("unlinked audio cut",
     [{"type": "update_audio_track", "track_id": "t1", "fields": {"muted": True}},
      {"type": "split_audio_track", "track_id": "t1", "frame": 80,
       "expected": {"track_id": "t1", "timeline_start_frame": 60, "lane_index": 0}}],
     (False, False)),
    ("prompt mute",
     [{"type": "update_prompt_section", "index": 1, "expected": {"prompt_id": "p2"},
       "fields": {"muted": True}}],
     (True, False)),
    ("linked prompt bounds move",
     [{"type": "update_prompt_section", "index": 1, "apply_linked": True,
       "expected": {"prompt_id": "p2"},
       "fields": {"start_frame": 70, "end_frame": 110}}],
     (True, True)),
    ("prompt create",
     [{"type": "create_prompt_section",
       "fields": {"start_frame": 100, "end_frame": 120, "prompt": "Silence."}}],
     (True, False)),
    ("prompt delete",
     [{"type": "delete_prompt_section", "index": 2, "expected": {"prompt_id": "p3"}}],
     (True, False)),
    ("linked prompt delete takes its guide",
     [{"type": "delete_prompt_section", "index": 1, "apply_linked": True,
       "expected": {"prompt_id": "p2"}}],
     (True, True)),
    ("prompt split",
     [{"type": "split_prompt_section", "index": 2, "frame": 150,
       "expected": {"prompt_id": "p3"}}],
     (True, False)),
    ("prompt swap",
     [{"type": "swap_prompt_sections", "index_a": 1, "index_b": 2,
       "expected_a": {"prompt_id": "p2"}, "expected_b": {"prompt_id": "p3"},
       "fields_a": {"start_frame": 160, "end_frame": 200},
       "fields_b": {"start_frame": 60, "end_frame": 140}}],
     (True, False)),
]


def _replace_battery_row():
    sections = [section.to_dict() for section in _scene().prompt_sections]
    replacement = copy.deepcopy(sections)
    replacement[2]["channels"] = {"detailed_description": "Rewritten."}
    return ("prompt replace",
            [{"type": "replace_prompt_sections", "sections": replacement,
              "expected": {"sections": sections}}],
            (True, False))


def _prompt_text_battery_row():
    """A direct field write owes the exact prior value of every field it writes."""
    section = _scene().prompt_sections[2]
    channels = dict(section.channels)
    channels["detailed_description"] = "Nobody looks up."
    return ("prompt text",
            [{"type": "update_prompt_section", "index": 2,
              "expected": {"prompt_id": "p3", "channels": copy.deepcopy(section.channels)},
              "fields": {"channels": channels}}],
            (True, False))


BATTERY.append(_replace_battery_row())
BATTERY.append(_prompt_text_battery_row())


# ---------------------------------------------------------------------------
# The two reads, run for real
# ---------------------------------------------------------------------------

@pytest.fixture
def route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


class DummyRequest(dict):
    def __init__(self, *, match_info=None, body=None, method="GET", headers=None, query=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = query or {}
        self.headers = headers or {}
        self._body = body
        self.method = method
        self.path = "/sonder-editor/project/p"
        self.rel_url = self.path

    async def json(self):
        return self._body


# The prompt read is checked under a generic, a MiniMax Base and a MiniMax H3
# format, labels off and on, with and without the candidate overlay.
FORMATS = [("sonder", "generic@1"), ("minimax_h3_base", "minimax_h3_base@1"),
           (TEMPLATE, PROFILE)]


def _compile(route_module, project, scene_id, window, *, template=TEMPLATE,
             labels_on=False, overlay=True):
    scene = project.get_scene(scene_id)
    start, end = window
    body = {"channel_template": template, "labels_on": labels_on,
            "window_start": start, "window_end": end,
            "selection_start": start, "selection_end": end}
    if overlay:
        body["scene"] = scene.to_dict()
    status, payload = route_module._compile_prompt_context_candidate_sync(
        project, scene_id, body)
    assert status == 200, payload
    payload = dict(payload)
    payload.pop("candidate_base_modified_at", None)
    return json.loads(json.dumps(payload, sort_keys=True, default=str))


def _bridge(route_module, monkeypatch, project, scene_id, window):
    handler = next(route.handler for route in route_module.routes
                   if route.method == "GET" and route.path.endswith("/bridge-references"))
    monkeypatch.setattr(route_module, "_load_scene_for_bridge",
                        lambda _request, sid: (project, project.get_scene(sid), None))
    start, end = window
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": scene_id},
        query={"selection_start": str(start), "selection_end": str(end)})))
    assert response.status == 200
    return json.loads(response.body.decode("utf-8"))


WINDOWS = [(0, 200), (40, 100)]


def _prompt_reads(route_module, project, scene_id="scene"):
    scene = project.get_scene(scene_id)
    authored = scene.prompt_context_profile_id
    reads = []
    try:
        for template, profile in FORMATS:
            scene.prompt_context_profile_id = profile
            for window in WINDOWS:
                for labels_on in (False, True):
                    for overlay in (False, True):
                        reads.append(_compile(route_module, project, scene_id, window,
                                              template=template, labels_on=labels_on,
                                              overlay=overlay))
    finally:
        scene.prompt_context_profile_id = authored
    return reads


def _reads(route_module, monkeypatch, project, scene_id="scene"):
    return {
        "prompt": _prompt_reads(route_module, project, scene_id),
        "bridge": [_bridge(route_module, monkeypatch, project, scene_id, window)
                   for window in WINDOWS],
    }


def _apply(route_module, project, operations):
    evidence = {}
    changed, _payload = route_module._apply_scene_mutation_batch(
        project, "scene", copy.deepcopy(operations), evidence=evidence)
    return changed, evidence.get("flags")


def _outside_the_scene(project):
    data = project.to_dict(include_internal=True)
    data.pop("modified_at", None)
    scenes = data.pop("scenes")
    return data, [scene for scene in scenes if scene.get("scene_id") != "scene"]


# ---------------------------------------------------------------------------
# 1. Coverage
# ---------------------------------------------------------------------------

def test_every_serialized_scene_field_is_classified():
    assert set(change_certificates.SCENE_FIELD_DEPENDENCIES) == set(Scene().to_dict()), (
        "classify the new Scene field in SCENE_FIELD_DEPENDENCIES: which projection "
        "reads it, or a traced reason that neither does")


def test_each_projection_covers_exactly_the_fields_classified_to_it():
    """Perturbing a field classified to a projection must move that projection."""
    for field, readers in change_certificates.SCENE_FIELD_DEPENDENCIES.items():
        for reader, project in (("prompt", change_certificates.prompt_dependency_projection),
                                ("bridge", change_certificates.bridge_dependency_projection)):
            if reader not in readers:
                continue
            scene = _scene()
            before = project(scene)
            _perturb(scene, field, reader)
            assert project(scene) != before, f"{reader} projection ignores {field}"


def _perturb(scene, field, reader):
    if field in ("prompt_track_config", "global_prompt_track_config", "guide_track_config"):
        getattr(scene, field).hidden = True
    elif field == "reference_lane_configs":
        if reader == "bridge":
            scene.reference_lane_configs[0].name = "changed"
        else:
            scene.reference_lane_configs[0].hidden = True
    elif field in ("prompt_sections", "guide_frames", "reference_items",
                   "reference_lane_recipes", "global_attachments"):
        getattr(scene, field).pop() if getattr(scene, field) else getattr(scene, field).append(
            prompt_context.shot_attachment())
    elif field in ("global_channels", "global_channel_docs", "prompt_context_profile_config"):
        setattr(scene, field, {"changed": "x"})
    elif field in ("duration_frames", "reference_lane_count"):
        setattr(scene, field, getattr(scene, field) + 1)
    elif field == "fps":
        scene.fps = 30.0
    else:
        setattr(scene, field, "changed")


# How to move each piece of scene state a projection deliberately leaves out.
# Keyed by `SCENE_FIELD_DEPENDENCIES` field, plus the lane sub-fields the
# projections read only in part. Added after the Phase 2 audit, which proved the
# classification right today but found nothing keeping it right tomorrow.
def _moves():
    return {
        "scene_id": None,   # identity: the certificate names it
        "name": lambda s: setattr(s, "name", "Other"),
        "order": lambda s: setattr(s, "order", 7),
        "width": lambda s: setattr(s, "width", 999),
        "height": lambda s: setattr(s, "height", 999),
        "prompt": None,     # a derived mirror of the global channels, no setter of its own
        "clips": lambda s: (setattr(s.clips[0], "timeline_start_frame", 3),
                            setattr(s.clips[0], "muted", True)),
        "audio_tracks": lambda s: (setattr(s.audio_tracks[0], "volume", 0.1),
                                   setattr(s.audio_tracks[0], "muted", True)),
        "linked_item_groups": lambda s: setattr(s, "linked_item_groups", []),
        "asset_ids": lambda s: setattr(s, "asset_ids", ["img_a", "zzz"]),
        "video_lane_count": lambda s: setattr(s, "video_lane_count", 5),
        "motion_driver_lane_count": lambda s: setattr(s, "motion_driver_lane_count", 3),
        "audio_lane_count": lambda s: setattr(s, "audio_lane_count", 4),
        "video_lane_configs": lambda s: (setattr(s.video_lane_configs[0], "hidden", True),
                                         setattr(s.video_lane_configs[0], "name", "q")),
        "audio_lane_configs": lambda s: setattr(s.audio_lane_configs[0], "hidden", True),
        "motion_driver_lane_configs": lambda s: setattr(
            s, "motion_driver_lane_configs", [LaneConfig(name="d", hidden=True)]),
        "generation_params": lambda s: setattr(s, "generation_params",
                                               {"seed": 5, "frame_count": 33}),
        "batch_config": lambda s: setattr(s.batch_config, "max_frames", 17),
        "is_bridge": lambda s: setattr(s, "is_bridge", True),
        "saved_selections": lambda s: setattr(s, "saved_selections",
                                              [{"name": "s", "start": 1, "end": 5}]),
        "duration_frames": lambda s: setattr(s, "duration_frames", 240),
        "fps": lambda s: setattr(s, "fps", 30.0),
        "global_channels": lambda s: s.set_global_channels({"detailed_description": "Global!"}),
        "global_channel_docs": lambda s: s.set_global_channels({"detailed_description": "Docs!"}),
        "global_attachments": lambda s: s.global_attachments.append(
            prompt_context.shot_attachment()),
        "prompt_sections": lambda s: setattr(s.prompt_sections[1], "muted", True),
        "prompt_context_profile_id": lambda s: setattr(s, "prompt_context_profile_id", "generic@1"),
        "prompt_context_profile_config": lambda s: setattr(s, "prompt_context_profile_config", {"x": 1}),
        "prompt_track_config": lambda s: setattr(s.prompt_track_config, "hidden", True),
        "global_prompt_track_config": lambda s: setattr(s.global_prompt_track_config, "hidden", True),
        "guide_track_config": lambda s: setattr(s.guide_track_config, "hidden", True),
        "guide_frames": lambda s: setattr(s.guide_frames[0], "frame_index", 30),
        "reference_items": lambda s: setattr(s.reference_items[1], "start_frame", 60),
        "reference_lane_count": lambda s: setattr(s, "reference_lane_count", 3),
        "reference_lane_configs": lambda s: setattr(s.reference_lane_configs[1], "hidden", True),
        "reference_lane_recipes": lambda s: setattr(s.reference_lane_recipes[1], "media_kind", "image"),
        # Sub-field state a projection reads only in part.
        "reference_lane_configs.name": lambda s: setattr(s.reference_lane_configs[0], "name", "Zed"),
        "lane presentation": lambda s: (
            setattr(s.reference_lane_configs[1], "locked", True),
            setattr(s.reference_lane_configs[1], "color", "#fff"),
            setattr(s.prompt_track_config, "locked", True),
            setattr(s.global_prompt_track_config, "locked", True),
            setattr(s.guide_track_config, "color", "#123")),
    }


def _unread_by(reader):
    """Every piece of state the named read is classified NOT to consume."""
    moves = _moves()
    unread = [field for field, readers in change_certificates.SCENE_FIELD_DEPENDENCIES.items()
              if reader not in readers and moves.get(field) is not None]
    unread.append("lane presentation")
    if reader == "prompt":
        unread.append("reference_lane_configs.name")
    return unread


def test_every_scene_field_has_a_way_to_move_it():
    missing = set(change_certificates.SCENE_FIELD_DEPENDENCIES) - set(_moves())
    assert not missing, f"give each new Scene field a move here: {sorted(missing)}"


@pytest.mark.parametrize("field", _unread_by("prompt"))
def test_state_the_prompt_projection_omits_never_moves_the_compile(route_module, tmp_path, field):
    project = _project(tmp_path)
    scene = project.get_scene("scene")
    projection = change_certificates.prompt_dependency_projection(scene)
    before = _prompt_reads(route_module, project)
    _moves()[field](scene)
    assert change_certificates.prompt_dependency_projection(scene) == projection, field
    assert _prompt_reads(route_module, project) == before, (
        f"the compile reads {field}; classify it as a prompt dependency")


@pytest.mark.parametrize("field", _unread_by("bridge"))
def test_state_the_bridge_projection_omits_never_moves_the_bridge(
        route_module, monkeypatch, tmp_path, field):
    project = _project(tmp_path)
    scene = project.get_scene("scene")
    projection = change_certificates.bridge_dependency_projection(scene)
    before = [_bridge(route_module, monkeypatch, project, "scene", w) for w in WINDOWS]
    _moves()[field](scene)
    assert change_certificates.bridge_dependency_projection(scene) == projection, field
    assert [_bridge(route_module, monkeypatch, project, "scene", w) for w in WINDOWS] == before, (
        f"bridge-references reads {field}; classify it as a bridge dependency")


def test_a_scene_whose_ids_were_minted_at_load_is_not_certified(route_module, tmp_path):
    data = _project(tmp_path).to_dict(include_internal=True)
    stored = next(scene for scene in data["scenes"] if scene["scene_id"] == "scene")
    stored["prompt_sections"][2]["prompt_id"] = stored["prompt_sections"][1]["prompt_id"]
    project = TimelineProject.from_dict(copy.deepcopy(data))
    project.project_dir = str(tmp_path)
    changed, flags = _apply(route_module, project, [
        {"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
         "fields": {"locked": True}}])
    assert changed and flags is None, (
        "two loads of this version mint different ids, so nothing may be certified")
    clean = TimelineProject.from_dict(copy.deepcopy(
        _project(tmp_path).to_dict(include_internal=True)))
    assert change_certificates.scene_ids_minted_at_load(clean.get_scene("scene")) is False


def test_every_certified_operation_is_a_dispatcher_operation():
    assert change_certificates.CERTIFIED_OPERATIONS <= set(_dispatcher_op_types())


def test_the_battery_exercises_every_certified_operation():
    exercised = {op["type"] for _label, operations, _flags in BATTERY for op in operations}
    assert change_certificates.CERTIFIED_OPERATIONS <= exercised


# ---------------------------------------------------------------------------
# 2-3. Scope and truth, per certified edit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,operations,expected", BATTERY,
                         ids=[row[0] for row in BATTERY])
def test_a_certified_edit_stays_in_its_scene_and_its_flags_are_true(
        route_module, monkeypatch, tmp_path, label, operations, expected):
    project = _project(tmp_path)
    outside_before = _outside_the_scene(project)
    reads_before = _reads(route_module, monkeypatch, project)

    changed, flags = _apply(route_module, project, operations)

    assert changed, f"{label}: the battery row must actually change the document"
    assert flags is not None, f"{label}: a certified batch must produce evidence"
    assert _outside_the_scene(project) == outside_before, (
        f"{label}: a certified operation wrote outside its scene")
    assert (flags["prompt"], flags["bridge"]) == expected, label
    reads_after = _reads(route_module, monkeypatch, project)
    if not flags["prompt"]:
        assert reads_after["prompt"] == reads_before["prompt"], (
            f"{label}: certified prompt-irrelevant, but the compile changed")
    if not flags["bridge"]:
        assert reads_after["bridge"] == reads_before["bridge"], (
            f"{label}: certified bridge-irrelevant, but the bridge payload changed")


def test_an_uncertified_operation_produces_no_evidence(route_module, tmp_path):
    project = _project(tmp_path)
    _changed, flags = _apply(route_module, project, [
        {"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}},
        {"type": "set_lane_count", "lane_type": "video", "count": 3}])
    assert flags is None


def test_the_projections_ignore_presentation_only_lane_state():
    scene = _scene()
    prompt = change_certificates.prompt_dependency_projection(scene)
    bridge = change_certificates.bridge_dependency_projection(scene)
    for config in scene.reference_lane_configs:
        config.locked = True
        config.color = "#123456"
    scene.prompt_track_config.locked = True
    scene.guide_track_config.color = "#654321"
    assert change_certificates.prompt_dependency_projection(scene) == prompt
    assert change_certificates.bridge_dependency_projection(scene) == bridge


# ---------------------------------------------------------------------------
# 4. Transport, through the real route and a real file
# ---------------------------------------------------------------------------

@pytest.fixture
def live(monkeypatch, tmp_path, route_module):
    project = create_project("Certified", base_dir=str(tmp_path))
    template = _project(tmp_path)
    project.scenes = template.scenes
    project.assets = template.assets
    project.references = template.references
    project.prompt_semantic_units = template.prompt_semantic_units
    save_project(project, notify=False)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    events = []
    monkeypatch.setattr(route_module, "schedule_project_event",
                        lambda alias, event: events.append((alias, copy.deepcopy(event))))
    handler = next(route.handler for route in route_module.routes
                   if route.method == "POST" and route.path.endswith("/scenes/{scene_id}/mutations"))
    folder_id = os.path.basename(project.project_dir)

    def post(operations, **headers):
        request = DummyRequest(method="POST", headers=headers,
                               match_info={"project_id": folder_id, "scene_id": "scene"},
                               body={"operations": operations})
        response = asyncio.run(handler(request))
        return response

    return SimpleNamespace(project=project, events=events, post=post,
                           project_dir=project.project_dir, folder_id=folder_id,
                           route_module=route_module)


def _header(response):
    raw = response.headers.get(change_certificates.HEADER)
    return json.loads(raw) if raw else None


def test_a_committed_certified_edit_publishes_one_identical_certificate(live):
    base = load_project(live.project_dir).modified_at
    response = live.post([{"type": "update_lane_config", "lane_type": "video",
                           "lane_index": 0, "fields": {"locked": True}}])
    assert response.status == 200
    stored = load_project(live.project_dir)
    certificate = _header(response)
    assert certificate == {
        "schema": 1, "project_id": stored.project_id, "scene_id": "scene",
        "base_modified_at": base, "modified_at": stored.modified_at,
        "prompt": False, "bridge": False}
    # Every `importlib.reload(routes)` in a test session registers another copy of
    # the saved hook, so emissions are compared as a set of distinct events.
    updated = {json.dumps([alias, event], sort_keys=True) for alias, event in live.events
               if event.get("type") == "project_updated"}
    updated = [json.loads(one)[1] for one in updated]
    assert len(updated) == 2, "one emission per alias"
    assert {alias for alias, _event in live.events} == {stored.project_id, live.folder_id}
    for event in updated:
        assert event[change_certificates.EVENT_KEY] == certificate


def test_the_certificate_is_never_persisted(live):
    live.post([{"type": "update_lane_config", "lane_type": "video",
                "lane_index": 0, "fields": {"locked": True}}])
    with open(os.path.join(live.project_dir, "project.json"), encoding="utf-8") as handle:
        stored = json.load(handle)
    assert change_certificates.EVENT_KEY not in stored
    assert change_certificates.HEADER not in json.dumps(stored)
    assert not any(isinstance(value, dict) and {"prompt", "bridge", "schema"} <= set(value)
                   for value in _walk(stored))


def test_a_no_op_publishes_nothing(live):
    response = live.post([{"type": "update_scene_fields", "fields": {"width": 640}}])
    assert response.status == 200
    assert json.loads(response.body)["committed"] is False
    assert _header(response) is None
    assert live.events == []


def test_an_uncertified_batch_publishes_an_event_without_a_certificate(live):
    response = live.post([{"type": "set_lane_count", "lane_type": "video", "count": 3}])
    assert response.status == 200
    assert _header(response) is None
    updated = [event for _alias, event in live.events if event.get("type") == "project_updated"]
    assert updated and all(change_certificates.EVENT_KEY not in event for event in updated)


def test_a_refused_attempt_publishes_nothing(live):
    response = live.post([{"type": "update_prompt_section", "index": 0,
                           "expected": {"prompt_id": "not-p1"}, "fields": {"muted": True}}])
    assert response.status == 409
    assert _header(response) is None
    assert live.events == []


def test_a_re_applied_edit_certifies_the_transition_that_committed(live, monkeypatch):
    """After a lost race the base is the RELOADED version, not the first load's."""
    route_module = live.route_module
    original = route_module._apply_scene_mutation_batch
    attempts = []

    def hooked(project, scene_id, operations, *, evidence=None):
        attempts.append(project.modified_at)
        if len(attempts) == 1:
            other = load_project(live.project_dir)
            other.scenes[1].name = "Elsewhere"
            save_project(other, notify=False)
        return original(project, scene_id, operations, evidence=evidence)

    monkeypatch.setattr(route_module, "_apply_scene_mutation_batch", hooked)
    response = live.post([{"type": "update_clip", "clip_id": "c1",
                           "fields": {"muted": True}}])
    assert response.status == 200
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    certificate = _header(response)
    assert certificate["base_modified_at"] == attempts[1]
    assert certificate["modified_at"] == load_project(live.project_dir).modified_at
    certified = [event[change_certificates.EVENT_KEY] for _alias, event in live.events
                 if change_certificates.EVENT_KEY in event]
    assert certified and all(one == certificate for one in certified)


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


@pytest.mark.parametrize("operations,headers", [
    ([{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
       "fields": {"locked": True}}], {}),
    ([{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}],
     {"X-Sonder-Mutation-Replay": "declined"}),
], ids=["positional", "declined-identity"])
def test_an_armed_attempt_that_loses_the_race_publishes_nothing(live, monkeypatch,
                                                                operations, headers):
    route_module = live.route_module
    original = route_module._apply_scene_mutation_batch

    def racing(project, scene_id, ops, **kwargs):
        other = load_project(live.project_dir)
        other.scenes[1].name = "Elsewhere"
        save_project(other, notify=False)
        return original(project, scene_id, ops, **kwargs)

    monkeypatch.setattr(route_module, "_apply_scene_mutation_batch", racing)
    with pytest.raises(route_module.ProjectVersionConflict):
        live.post(operations, **headers)
    assert live.events == []
    assert change_certificates._pending.get() is None, "the armed record must be released"


def test_a_saved_hook_that_fails_leaves_the_response_uncertified(live, monkeypatch):
    def broken(_project):
        raise RuntimeError("hook failed")

    monkeypatch.setattr(change_certificates, "certificate_for_saved", broken)
    response = live.post([{"type": "update_lane_config", "lane_type": "video",
                           "lane_index": 0, "fields": {"locked": True}}])
    assert response.status == 200
    assert _header(response) is None, "the header never certifies what no event did"


def test_a_client_supplied_certificate_is_ignored(live):
    response = live.post(
        [{"type": "set_lane_count", "lane_type": "video", "count": 3}],
        **{change_certificates.HEADER: json.dumps({"prompt": False, "bridge": False})})
    assert response.status == 200
    assert _header(response) is None


def test_a_save_the_certificate_was_not_armed_for_never_borrows_it(tmp_path):
    project = _project(tmp_path)
    project.modified_at = "2026-09-27T10:00:00"
    with change_certificates.pending_certificate(
            project_dir=str(tmp_path / "elsewhere"), project_id="proj", scene_id="scene",
            base_modified_at="2026-09-27T09:00:00", prompt=False, bridge=False):
        assert change_certificates.certificate_for_saved(project) is None
    assert change_certificates.certificate_for_saved(project) is None
