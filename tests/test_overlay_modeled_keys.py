"""A modeled optional key that a mutation clears stays cleared through a save.

``Scene.to_dict`` and ``TimelineProject.to_dict`` overlay the raw document they
were loaded from (``_overlay_unknown_record``), so a key a future build writes
survives a save by this one. Until backlog step 1 the overlay copied back EVERY
raw key the canonical record lacked, and a record kind whose normalizer emits a
key only when it is set therefore could not express a clear: the stored value
came back on the next save. The cleared chip Summary (`enabled`), a staged
member's role or retention, an identity's legacy `voice` repair and a profile's
`contribution_catalog` were all affected.

The rule now: each record kind with optional keys declares its MODELED keys
beside its normalizer, and the overlay never restores a modeled key. Canonical
state is the only owner of a modeled key, so it must preserve every stored
modeled value -- including an out-of-vocabulary staged intent, which consumers
filter by vocabulary instead (``minimax_h3.staged_member_intent``).

``test_optional_key_census`` is the tripwire: a record that starts emitting a
key only when it is set must be modeled, or a clear of it silently stops saving
again.
"""

import ast
import copy
import inspect
import json
import textwrap

import pytest

from server import minimax_h3, prompt_context, prompt_live_context, routes, timeline_state
from server.timeline_state import (
    Asset,
    AudioTrack,
    BatchConfig,
    ClipReference,
    GenerationJob,
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


def _rt(project):
    return TimelineProject.from_dict(json.loads(json.dumps(project.to_dict())))


def _saved(project):
    return json.loads(json.dumps(project.to_dict()))


# --------------------------------------------------------------------------
# Capability `enabled` (the chip Summary re-enable), through the real route
# --------------------------------------------------------------------------

def _chip_project(enabled):
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "reference", "emission_group_id": "grp",
        "capabilities": [{"capability_id": "summary", "kind": "summary",
                          "enabled": enabled}],
    })
    section = PromptSection(0, 24, channels={"visual": "scene"}, attachments=[attachment])
    scene = Scene(scene_id="s", duration_frames=24, prompt_sections=[section])
    data = json.loads(json.dumps(TimelineProject(project_dir="", scenes=[scene]).to_dict()))
    # A key a future build wrote; it must keep surviving.
    data["scenes"][0]["prompt_sections"][0]["attachments"][0]["capabilities"][0][
        "future_cap"] = "kept"
    return TimelineProject.from_dict(data)


def _linked_attachment_op(project, enabled):
    """The op `_updateLinkedPromptAttachmentWithinGesture` sends.

    `sparseCapabilityRecord` deletes `enabled` when the chip returns to the
    inherited default, so a re-check sends the capability WITHOUT the key.
    """
    section = project.get_scene("s").to_dict()["prompt_sections"][0]
    attachments = copy.deepcopy(section["attachments"])
    capability = attachments[0]["capabilities"][0]
    if enabled is None:
        capability.pop("enabled", None)
    else:
        capability["enabled"] = enabled
    return {
        "type": "update_prompt_section", "index": 0,
        "expected": {key: copy.deepcopy(section.get(key)) for key in (
            "prompt_id", "start_frame", "end_frame", "prompt")}
        | {"muted": bool(section.get("muted")),
           "channels": copy.deepcopy(section.get("channels") or {}),
           "channel_docs": copy.deepcopy(section.get("channel_docs") or {}),
           "attachments": copy.deepcopy(section.get("attachments") or []),
           "global_channel_exceptions": copy.deepcopy(
               section.get("global_channel_exceptions") or [])},
        "fields": {"attachments": attachments},
    }


def _capability(saved):
    scene = next(value for value in saved["scenes"] if value["scene_id"] == "s")
    return scene["prompt_sections"][0]["attachments"][0]["capabilities"][0]


@pytest.mark.parametrize("stored", [False, True])
def test_clearing_a_capability_enabled_is_saved(stored):
    # The JSON round trip is the precondition: an in-memory row never meets
    # the overlay, which is why the defect hid from route-level tests.
    project = _rt(_chip_project(stored))
    _scene, payload = routes._apply_scene_mutation_batch(
        project, "s", [_linked_attachment_op(project, None)])

    assert payload["committed"] is True
    response = payload["scene"]["prompt_sections"][0]["attachments"][0]["capabilities"][0]
    assert "enabled" not in response
    saved = _saved(project)
    assert "enabled" not in _capability(saved)
    assert "enabled" not in _capability(_saved(_rt(project)))
    # The overlay's job is unchanged for a key nothing models.
    assert _capability(saved)["future_cap"] == "kept"


def test_an_explicit_capability_enabled_still_persists():
    project = _rt(_chip_project(False))
    routes._apply_scene_mutation_batch(project, "s", [_linked_attachment_op(project, True)])
    assert _capability(_saved(_rt(project)))["enabled"] is True


# --------------------------------------------------------------------------
# The rest of the scope Phase 0 found, cleared in memory as a handler leaves it
# --------------------------------------------------------------------------

def _scope_project():
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "reference",
        "capabilities": [{"capability_id": "summary", "kind": "summary"}]})
    document = {"schema": prompt_context.DOCUMENT_SCHEMA, "nodes": [
        {"type": "attachment", "node_id": "n1", "attachment_id": "chip",
         "capability_id": "summary"}]}
    section = PromptSection(0, 24, channels={"visual": ""},
                            channel_docs={"visual": document}, attachments=[attachment])
    scene = Scene(scene_id="s", duration_frames=24, prompt_sections=[section],
                  global_channel_docs={"visual": copy.deepcopy(document)})
    unit = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "u", "handle": "hero", "voice": {"asset_id": "x"},
        "visual_intent": "appearance", "audio_intent": "voice"})
    profile = copy.deepcopy(prompt_context.BUILTIN_PROFILES["generic@1"])
    profile.update(profile_id="custom", builtin=False,
                   compatible_templates=["standard"],
                   contribution_catalog={"kinds": []})
    project = TimelineProject(project_dir="", scenes=[scene],
                              prompt_semantic_units=[unit],
                              prompt_context_profiles=[prompt_context.normalize_profile(profile)])
    data = json.loads(json.dumps(project.to_dict()))
    # Unknown keys a future build wrote: these must keep surviving.
    data["prompt_semantic_units"][0]["future_unit"] = "kept"
    data["prompt_context_profiles"][0]["future_profile"] = "kept"
    scene_data = data["scenes"][0]
    scene_data["prompt_sections"][0]["channel_docs"]["visual"]["nodes"][0]["future_node"] = "kept"
    scene_data["global_channel_docs"]["visual"]["nodes"][0]["future_node"] = "kept"
    return TimelineProject.from_dict(data)


def test_clearing_a_document_node_capability_id_is_saved():
    project = _scope_project()
    scene = project.get_scene("s")
    scene.prompt_sections[0].channel_docs["visual"]["nodes"][0].pop("capability_id")
    scene.global_channel_docs["visual"]["nodes"][0].pop("capability_id")

    saved = next(value for value in _saved(project)["scenes"] if value["scene_id"] == "s")
    section_node = saved["prompt_sections"][0]["channel_docs"]["visual"]["nodes"][0]
    global_node = saved["global_channel_docs"]["visual"]["nodes"][0]
    for node in (section_node, global_node):
        assert "capability_id" not in node
        assert node["future_node"] == "kept"


@pytest.mark.parametrize("field", ["voice", "visual_intent", "audio_intent"])
def test_clearing_an_identity_optional_field_is_saved(field):
    # `voice` is the identity panel's legacy repair (`delete next.voice`).
    project = _scope_project()
    project.prompt_semantic_units[0].pop(field)

    unit = _saved(project)["prompt_semantic_units"][0]
    assert field not in unit
    assert unit["future_unit"] == "kept"
    assert field not in _saved(_rt(project))["prompt_semantic_units"][0]


@pytest.mark.parametrize("field", ["contribution_catalog", "compatible_templates"])
def test_clearing_a_profile_optional_field_is_saved(field):
    project = _scope_project()
    profile = copy.deepcopy(project.prompt_context_profiles[0])
    profile.pop(field)
    project.prompt_context_profiles[0] = prompt_context.normalize_profile(profile)

    saved = _saved(project)["prompt_context_profiles"][0]
    assert field not in saved
    assert saved["future_profile"] == "kept"


def test_a_staged_member_clear_commits_and_keeps_unknown_member_keys():
    member = {"entity_id": "e", "member_id": "m", "visual_intent": "preserve",
              "role": "identity", "future_member": "kept"}
    scene = Scene(scene_id="s", duration_frames=100, reference_lane_count=1,
                  reference_lane_recipes=[ReferenceLaneRecipe(
                      lane_id="lane", media_kind="image", recipe={"soft": {
                          "physical_population": "pictures",
                          "role_fields": ["role", "visual_intent"]}})],
                  reference_items=[ReferenceItem.from_dict({
                      "reference_item_id": "item", "lane_index": 0,
                      "start_frame": 0, "end_frame": 50, "members": [member]})])
    project = TimelineProject(
        project_dir="", scenes=[scene],
        assets=[Asset(asset_id="a", asset_type="image", path="media/a.png")],
        references=[ReferenceEntity(reference_id="e", name="E", members=[
            ReferenceMember(member_id="m", asset_id="a")])])
    data = json.loads(json.dumps(project.to_dict()))
    data["scenes"][0]["reference_items"][0]["members"][0]["future_member"] = "kept"
    project = TimelineProject.from_dict(data)
    # The canonical members, not the served row: a guard spelled with the
    # unknown key is refused until Phase 2's normalized compare (Bug Tracker
    # "...can never be bulk-deleted"), which this test is not about.
    stored = project.get_scene("s").reference_items[0].to_dict()["members"]
    cleared = [{key: value for key, value in stored[0].items()
                if key not in ("visual_intent", "role")}]

    _scene, payload = routes._apply_scene_mutation_batch(project, "s", [{
        "type": "update_reference_item", "reference_item_id": "item",
        "fields": {"members": cleared}, "expected": {"members": stored}}])

    assert payload["committed"] is True
    saved = _saved(_rt(project))["scenes"][0]["reference_items"][0]["members"][0]
    assert "visual_intent" not in saved and "role" not in saved
    assert saved["future_member"] == "kept"


# --------------------------------------------------------------------------
# Out-of-vocabulary staged intents: preserved at rest, inert in compile
# --------------------------------------------------------------------------

def test_an_unrecognized_staged_intent_is_preserved_in_canonical_state():
    item = ReferenceItem.from_dict({
        "reference_item_id": "item", "members": [{
            "entity_id": "e", "member_id": "m",
            "visual_intent": "future_intent", "audio_intent": 5, "role": " spaced "}]})
    member = item.to_dict()["members"][0]
    # Verbatim, type included: canonical is the only owner of a modeled key.
    assert member["visual_intent"] == "future_intent"
    assert member["audio_intent"] == 5
    assert member["role"] == " spaced "


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_staged_field_is_absent(blank):
    item = ReferenceItem.from_dict({"members": [{
        "member_id": "m", "visual_intent": blank, "audio_intent": blank, "role": blank}]})
    assert set(item.to_dict()["members"][0]) == {"entity_id", "member_id"}


PICTURE_RECIPE = {"soft": {
    "compatible_profiles": ["minimax_h3_ref@1"],
    "physical_population": "pictures",
    "exposed_capabilities": ["definitions", "retention", "mentions"],
    "role_fields": ["role", "visual_intent"]}}


def _h3_setup(staged_member, *, as_record):
    entity = ReferenceEntity(reference_id="e", name="Woman", members=[
        ReferenceMember(member_id="mp", asset_id="img", visual_intent="partial")])
    row = {"reference_item_id": "ip", "lane_index": 0, "start_frame": 0,
           "end_frame": 100, "members": [staged_member]}
    # A live scene serves a ReferenceItem; a frozen queue snapshot serves the
    # plain dict it froze. Both reach `_member_slots`.
    item = ReferenceItem.from_dict(row) if as_record else row
    return minimax_h3.resolve_setup(
        setup={"mode": "reference", "picture_lane_ids": ["lp"]},
        reference_items=[item],
        lane_recipes=[ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                          recipe=PICTURE_RECIPE)],
        lane_count=1, scene_duration=100, window_start=0, window_end=100,
        references=[entity],
        assets=[Asset(asset_id="img", asset_type="image")],
        semantic_units=[])


@pytest.mark.parametrize("as_record", [True, False])
@pytest.mark.parametrize("unrecognized_value", ["future_intent", 5])
def test_an_unrecognized_staged_intent_resolves_exactly_like_an_absent_one(
        as_record, unrecognized_value):
    absent = _h3_setup({"entity_id": "e", "member_id": "mp", "role": "identity"},
                       as_record=as_record)
    unrecognized = _h3_setup({"entity_id": "e", "member_id": "mp", "role": "identity",
                              "visual_intent": unrecognized_value}, as_record=as_record)

    # The Library member's own intent shows through, exactly as when absent.
    assert absent["setup_manifest"]["pictures"][0]["visual_intent"] == "partial"
    assert unrecognized["setup_manifest"] == absent["setup_manifest"]
    assert unrecognized["ordinal_manifest"] == absent["ordinal_manifest"]
    assert (prompt_context.content_hash(unrecognized["setup_manifest"])
            == prompt_context.content_hash(absent["setup_manifest"]))


_SHOT = prompt_context.shot_attachment()  # one id: a fresh call mints another


def _live_h3_compile(staged_member):
    """End to end: a project loaded from JSON, compiled as preview and enqueue do.

    Only the MiniMax H3 format reads staged intents (`_member_slots`); a
    generic profile never consults them, so it has nothing to compare.
    """
    chip = prompt_context.normalize_attachment({
        "attachment_id": "chip", "emission_group_id": "chip", "kind": "reference",
        "provider_id": "minimax_h3_ref", "provider_version": "1",
        "source": {"semantic_unit_ids": ["u"]}, "config": {},
        "capabilities": [{"capability_id": value, "kind": value, "placement": "section_prefix"}
                         for value in ("definitions", "retention")]})
    scene = Scene(
        scene_id="s", duration_frames=100, reference_lane_count=1,
        prompt_context_profile_id="minimax_h3_ref@1",
        reference_lane_recipes=[ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                                    recipe=PICTURE_RECIPE)],
        reference_items=[ReferenceItem.from_dict({
            "reference_item_id": "ip", "lane_index": 0, "start_frame": 0,
            "end_frame": 100, "members": [staged_member]})],
        prompt_sections=[PromptSection(0, 100, channels={"detailed_description": "She looks up."},
                                       attachments=[copy.deepcopy(_SHOT), chip])])
    project = TimelineProject(
        project_dir="", scenes=[scene],
        assets=[Asset(asset_id="img", asset_type="image", path="media/img.png")],
        references=[ReferenceEntity(reference_id="e", name="Woman", members=[
            ReferenceMember(member_id="mp", asset_id="img", prompt="the woman",
                            visual_intent="partial")])],
        prompt_semantic_units=[prompt_context.normalize_semantic_unit({
            "semantic_unit_id": "u", "name": "Woman",
            "sources": [{"entity_id": "e", "member_id": "mp"}]})])
    data = json.loads(json.dumps(project.to_dict()))
    data["scenes"][0]["prompt_sections"][0]["prompt_id"] = "section"
    project = TimelineProject.from_dict(data)
    return prompt_live_context.compile_live_scene_prompt_context(
        project, project.scenes[0], template="minimax_h3_ref", window_start=0,
        window_end=100, fps=24.0, labels_on=True)


def test_an_unrecognized_staged_intent_compiles_exactly_like_an_absent_one():
    absent = _live_h3_compile({"entity_id": "e", "member_id": "mp", "role": "identity"})
    unrecognized = _live_h3_compile({"entity_id": "e", "member_id": "mp", "role": "identity",
                                     "visual_intent": "future_intent"})

    assert not absent["errors"]
    # The retention line carries the Library member's "partial", as when absent.
    assert prompt_context.VISUAL_INTENTS["partial"] in absent["prompt"]
    for key in ("prompt", "content_hash", "setup_manifest"):
        assert unrecognized[key] == absent[key], key


# --------------------------------------------------------------------------
# Census tripwire
# --------------------------------------------------------------------------
#
# Each record kind the save overlay meets is run through its real normalizer:
# once as a minimal record, then with every key that normalizer names set to
# each value below in turn. A key emitted in some outputs and not others is
# OPTIONAL, and an optional key the kind does not model is a clear that would
# silently stop saving -- however the normalizer comes to emit it (a literal,
# `.update`, a loop, a comprehension).

_VALUES = ["x", " ", "", 1, 0, 2.5, True, False, None, ["x"], [], {"x": "y"}, {}]


def _keys_named_in(*functions):
    keys = set()
    for function in functions:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        keys |= {node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)
                 and node.value.isidentifier()}
    return keys


def _optional_keys(emit, base, functions, *, fixed=()):
    outputs = []

    def run(raw):
        try:
            out = emit(copy.deepcopy(raw))
        except Exception:  # a value this normalizer refuses tells us nothing
            return
        if isinstance(out, dict):
            outputs.append(set(out))

    run(base)
    for key in sorted(_keys_named_in(*functions) - set(fixed)):
        for value in _VALUES:
            run({**base, key: value})
    assert len(outputs) > 1, "the census exercised nothing"
    return set().union(*outputs) - set.intersection(*outputs)


def _canonical_scene(raw):
    scene = Scene.from_dict(raw)
    scene._raw_data = None  # the overlay is what is under test, not its input
    return scene.to_dict()


def _canonical_project(raw):
    project = TimelineProject.from_dict(raw)
    project._raw_data = None
    return project.to_dict()


_TWO_CLIPS = [{"clip_id": "c1", "track_index": 0},
              {"clip_id": "c2", "track_index": 0,
               "timeline_start_frame": 50, "timeline_end_frame": 60}]


def _link_group(raw):
    return _canonical_scene({"scene_id": "s", "clips": _TWO_CLIPS,
                             "linked_item_groups": [raw]})["linked_item_groups"][0]


def _member(cls, raw):
    return cls.from_dict(raw).to_dict()


def _document_node(raw):
    return prompt_context.normalize_prompt_document({"nodes": [raw]})["nodes"][0]


# (kind, emit, minimal record, functions whose literals name the keys, modeled)
_CENSUS = [
    ("staged member", timeline_state.normalize_staged_member, {"member_id": "m"},
     [timeline_state.normalize_staged_member], timeline_state.STAGED_MEMBER_FIELDS),
    ("capability", prompt_context.normalize_capability, {},
     [prompt_context.normalize_capability], prompt_context.CAPABILITY_FIELDS),
    ("text node", _document_node, {"type": "text", "node_id": "n"},
     [prompt_context.normalize_prompt_document], prompt_context.DOCUMENT_NODE_KEYS),
    ("attachment node", _document_node,
     {"type": "attachment", "node_id": "n", "attachment_id": "a"},
     [prompt_context.normalize_prompt_document], prompt_context.DOCUMENT_NODE_KEYS),
    ("identity", prompt_context.normalize_semantic_unit, {"semantic_unit_id": "u"},
     [prompt_context.normalize_semantic_unit], prompt_context.SEMANTIC_UNIT_FIELDS),
    ("identity source",
     lambda raw: prompt_context.normalize_semantic_unit({"sources": [raw]})["sources"][0],
     {"entity_id": "e", "member_id": "m"}, [prompt_context.normalize_semantic_unit], ()),
    ("profile", prompt_context.normalize_profile,
     copy.deepcopy(prompt_context.BUILTIN_PROFILES["generic@1"]),
     [prompt_context.normalize_profile], prompt_context.PROFILE_FIELDS),
    ("attachment", prompt_context.normalize_attachment,
     {"attachment_id": "a", "kind": "custom"}, [prompt_context.normalize_attachment], ()),
    ("asset", lambda raw: _member(Asset, raw), {"asset_id": "a"},
     [Asset.from_dict, Asset.to_dict], ()),
    ("library member", lambda raw: _member(ReferenceMember, raw),
     {"member_id": "m", "asset_id": "a"},
     [ReferenceMember.from_dict, ReferenceMember.to_dict], ()),
    ("library member crop",
     lambda raw: _member(ReferenceMember, {"member_id": "m", "asset_id": "a", "crop": raw})["crop"],
     {"x": 0, "y": 0, "w": 1, "h": 1}, [timeline_state.normalize_reference_crop], ()),
    ("reference", lambda raw: _member(ReferenceEntity, raw), {"reference_id": "r"},
     [ReferenceEntity.from_dict, ReferenceEntity.to_dict], ()),
    ("lane recipe", lambda raw: _member(ReferenceLaneRecipe, raw), {"lane_id": "l"},
     [ReferenceLaneRecipe.from_dict, ReferenceLaneRecipe.to_dict], ()),
    ("reference item", lambda raw: _member(ReferenceItem, raw), {"reference_item_id": "i"},
     [ReferenceItem.from_dict, ReferenceItem.to_dict], ()),
    ("guide", lambda raw: _member(GuideFrame, raw), {"guide_id": "g"},
     [GuideFrame.from_dict, GuideFrame.to_dict], ()),
    ("batch config", lambda raw: _member(BatchConfig, raw), {},
     [BatchConfig.from_dict, BatchConfig.to_dict], ()),
    ("lane config", lambda raw: _member(LaneConfig, raw), {},
     [LaneConfig.from_dict, LaneConfig.to_dict], ()),
    ("prompt section", lambda raw: _member(PromptSection, raw),
     {"prompt_id": "p", "start_frame": 0, "end_frame": 10},
     [PromptSection.from_dict, PromptSection.to_dict], ()),
    ("clip", lambda raw: _member(ClipReference, raw), {"clip_id": "c"},
     [ClipReference.from_dict, ClipReference.to_dict], ()),
    ("audio track", lambda raw: _member(AudioTrack, raw), {"track_id": "t"},
     [AudioTrack.from_dict, AudioTrack.to_dict], ()),
    # Hydrated only. An UNHYDRATED job's `to_dict` omits `FROZEN_JOB_FIELDS` by
    # design; their payload rides its content-addressed component, never this
    # overlay (`test_queue_roundtrip_and_no_resurrection_by_overlay` in
    # tests/test_project_storage.py), so the job kind needs no modeled set.
    ("generation job (hydrated)", lambda raw: _member(GenerationJob, raw), {"job_id": "j"},
     [GenerationJob.from_dict, GenerationJob.to_dict], ()),
    ("scene", _canonical_scene, {"scene_id": "s"}, [Scene.from_dict, Scene.to_dict], ()),
    ("saved selection",
     lambda raw: _canonical_scene({"scene_id": "s", "saved_selections": [raw]})["saved_selections"][0],
     {"name": "n"}, [Scene.from_dict, Scene.to_dict], ()),
    ("link group", _link_group,
     {"group_id": "g", "items": [{"type": "clip", "id": "c1"}, {"type": "clip", "id": "c2"}]},
     [Scene._normalize_linked_item_groups], ()),
    ("link group item",
     lambda raw: _link_group({"group_id": "g",
                              "items": [raw, {"type": "clip", "id": "c2"}]})["items"][0],
     {"type": "clip", "id": "c1"}, [Scene._normalize_linked_item_groups], ()),
    ("project", _canonical_project, {"project_id": "p"},
     [TimelineProject.from_dict, TimelineProject.to_dict], ()),
]


@pytest.mark.parametrize("kind, emit, base, functions, modeled", _CENSUS,
                         ids=[row[0] for row in _CENSUS])
def test_optional_key_census(kind, emit, base, functions, modeled):
    # A document node's `type` selects its shape, so it is not varied.
    unmodeled = _optional_keys(emit, base, functions, fixed=("type",)) - set(modeled)
    assert not unmodeled, (
        f"{kind} emits {sorted(unmodeled)} only when set. Model it beside the "
        "normalizer and pass the set to its overlay site, or a clear stops saving.")


def test_the_census_sees_a_key_added_any_way():
    """The census must not pass vacuously: one key per way of adding it."""
    def sparse(raw):
        out = {"id": "x"}
        if raw.get("by_update"):
            out.update({"by_update": raw["by_update"]})
        for name in ("by_loop",):
            if raw.get(name):
                out[name] = raw[name]
        out.update({key: raw[key] for key in ("by_comprehension",) if raw.get(key)})
        return out

    assert _optional_keys(sparse, {}, [sparse]) == {
        "by_update", "by_loop", "by_comprehension"}


def test_modeled_sets_name_exactly_what_each_normalizer_emits():
    staged = ReferenceItem.from_dict({"members": [{
        "entity_id": "e", "member_id": "m", "visual_intent": "preserve",
        "audio_intent": "copy_full", "role": "identity"}]}).to_dict()["members"][0]
    assert set(staged) == set(timeline_state.STAGED_MEMBER_FIELDS)

    capability = prompt_context.normalize_capability({"enabled": False})
    assert set(capability) == set(prompt_context.CAPABILITY_FIELDS)

    document = prompt_context.normalize_prompt_document({"nodes": [
        {"type": "text", "node_id": "t", "text": "x"},
        {"type": "attachment", "node_id": "a", "attachment_id": "c",
         "capability_id": "summary"}]})
    for node in document["nodes"]:
        assert set(node) == set(prompt_context.DOCUMENT_NODE_FIELDS[node["type"]])

    unit = prompt_context.normalize_semantic_unit({
        "voice": None, "visual_intent": "x", "audio_intent": "y"})
    assert set(unit) == set(prompt_context.SEMANTIC_UNIT_FIELDS)

    profile = copy.deepcopy(prompt_context.BUILTIN_PROFILES["generic@1"])
    profile.update(compatible_templates=["standard"], contribution_catalog={})
    assert set(prompt_context.normalize_profile(profile)) == set(prompt_context.PROFILE_FIELDS)
