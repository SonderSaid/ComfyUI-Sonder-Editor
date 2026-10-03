"""A Reference item route judges only what the write names (backlog step 1, Phase 2).

Three Bug Tracker entries share one cause: the routes re-judged parts of a
staged item that the write did not touch.

* **The delete guard** (#81, "...can never be bulk-deleted") compared the
  client's SERVED member rows -- which carry the save overlay's unknown keys --
  with the canonical `to_dict()`, so an item holding a member key this build
  does not model could never be deleted. It now compares both sides after the
  route's own member normalization (`timeline_state.normalize_staged_members`)
  and stays strict on every modeled field.
* **A scalar update** (#66) re-validated every stored member, so a strength or
  mute edit was refused for a member whose asset went to Trash or whose lane
  population narrowed. Members are re-validated only when the write names them
  or moves the item to another lane.
* **A range write** (D8) re-clamped both bounds, so a Start edit on an item
  straddling the scene end silently cut its end, and a scalar write on two
  legacy items squashed onto one frame was refused as an overlap. A write now
  changes only the bounds it names, and skips the overlap check when lane and
  range are unchanged, since it creates no new overlap.

Every test goes through `_apply_scene_mutation_batch` on a project loaded from
JSON, so the served rows carry the overlay exactly as the editor receives them.
"""

import json

import pytest

from server import routes
from server.timeline_state import (
    Asset,
    LaneConfig,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    Scene,
    TimelineProject,
)


def _project(rows, *, duration=100, member_extras=None, lanes=None):
    """A JSON-loaded project; `member_extras` is written into the raw file."""
    assets = [
        Asset(asset_id="asset-a", name="a", asset_type="image", path="media/a.png"),
        Asset(asset_id="asset-b", name="b", asset_type="image", path="media/b.png"),
        Asset(asset_id="asset-s", name="s", asset_type="audio", path="media/s.wav"),
    ]
    entity = ReferenceEntity(reference_id="entity-1", name="Subject", members=[
        ReferenceMember(member_id=f"member-{key}", asset_id=f"asset-{key}")
        for key in ("a", "b", "s")])
    recipes = lanes or [
        ReferenceLaneRecipe(lane_id="lane-image", media_kind="image", recipe={
            "soft": {"physical_population": "pictures",
                     "role_fields": ["role", "visual_intent"]}}),
        ReferenceLaneRecipe(lane_id="lane-audio", media_kind="audio", recipe={
            "soft": {"role_fields": ["audio_intent"]}}),
    ]
    scene = Scene(
        scene_id="scene-1", duration_frames=duration,
        reference_lane_count=len(recipes),
        reference_lane_configs=[LaneConfig() for _ in recipes],
        reference_lane_recipes=recipes,
        reference_items=[ReferenceItem.from_dict(row) for row in rows])
    project = TimelineProject(project_id="p", assets=assets, references=[entity],
                              scenes=[scene])
    data = json.loads(json.dumps(project.to_dict()))
    for (item_index, member_index), extra in (member_extras or {}).items():
        data["scenes"][0]["reference_items"][item_index]["members"][member_index].update(extra)
    return TimelineProject.from_dict(data)


def _member(key, **extra):
    return {"entity_id": "entity-1", "member_id": f"member-{key}", **extra}


def _row(item_id, start, end, members, lane=0, **extra):
    return {"reference_item_id": item_id, "lane_index": lane, "start_frame": start,
            "end_frame": end, "members": members, **extra}


def _served(project, item_id):
    """The row as the editor holds it: the scene payload, overlay included."""
    scene = project.get_scene("scene-1").to_dict()
    return next(row for row in scene["reference_items"]
                if row["reference_item_id"] == item_id)


def _batch(project, operations):
    return routes._apply_scene_mutation_batch(project, "scene-1", operations)


def _refusal(project, operations):
    with pytest.raises(routes.ProjectMutationRequestError) as caught:
        _batch(project, operations)
    return caught.value.code


def _item(project, item_id):
    return next((item for item in project.get_scene("scene-1").reference_items
                 if item.reference_item_id == item_id), None)


def _saved(project):
    return TimelineProject.from_dict(json.loads(json.dumps(project.to_dict())))


# -- #81: the delete guard ----------------------------------------------------------

STORED_SHAPES = {
    "an unmodeled key": {"future": {"kept": [1]}},
    "a blank role": {"role": ""},
    "an unrecognized retention": {"visual_intent": "bogus"},
    "a whitespace role": {"role": "   "},
    "a non-string role": {"role": 5},
}


def _delete_ops(served):
    expected = {key: value for key, value in served.items()}
    return {
        "bulk": [{"type": "bulk_delete_items", "items": [
            {"type": "reference", "id": served["reference_item_id"],
             "expected": expected}]}],
        "single": [{"type": "delete_reference_item",
                    "reference_item_id": served["reference_item_id"],
                    "expected": expected}],
    }


@pytest.mark.parametrize("route", ["bulk", "single"])
@pytest.mark.parametrize("shape", sorted(STORED_SHAPES))
def test_a_served_row_deletes_whatever_its_members_carry(route, shape):
    project = _project([_row("item-1", 10, 40, [_member("a"), _member("b")])],
                       member_extras={(0, 0): STORED_SHAPES[shape]})
    _batch(project, _delete_ops(_served(project, "item-1"))[route])
    assert _item(project, "item-1") is None


@pytest.mark.parametrize("route", ["bulk", "single"])
def test_the_delete_guard_stays_strict_on_every_modeled_member_field(route):
    project = _project([_row("item-1", 10, 40, [
        _member("a", role="subject", visual_intent="preserve"), _member("b")])])
    served = _served(project, "item-1")
    stale = {
        "a changed role": [_member("a", role="other", visual_intent="preserve"),
                           _member("b")],
        "a dropped retention": [_member("a", role="subject"), _member("b")],
        "an added retention": [_member("a", role="subject", visual_intent="preserve"),
                               _member("b", visual_intent="partial")],
        "another entity": [{**_member("a", role="subject", visual_intent="preserve"),
                            "entity_id": "entity-2"}, _member("b")],
        "a missing member": [_member("a", role="subject", visual_intent="preserve")],
        "an extra member": [_member("a", role="subject", visual_intent="preserve"),
                            _member("b"), _member("s")],
        "another order": [_member("b"),
                          _member("a", role="subject", visual_intent="preserve")],
        "not a list": {"member_id": "member-a"},
    }
    for name, members in stale.items():
        operations = _delete_ops({**served, "members": members})[route]
        assert _refusal(project, operations) == "identity_mismatch", name
    assert _item(project, "item-1") is not None


# -- #66: a scalar write does not re-judge stored members ------------------------------

def _trash_b(project):
    project.assets = [asset for asset in project.assets if asset.asset_id != "asset-b"]


def _narrow_to_videos(project):
    recipe = project.get_scene("scene-1").reference_lane_recipes[0]
    recipe.recipe["soft"]["physical_population"] = "videos"


def _untrimmed_role(project):
    _item(project, "item-1").members[0]["role"] = " identity "


@pytest.mark.parametrize("fixture", [_trash_b, _narrow_to_videos, _untrimmed_role])
@pytest.mark.parametrize("fields", [{"strength": 0.5}, {"muted": True},
                                    {"prompt_override": "x"}, {"sequence_frames": 9}])
def test_a_scalar_write_saves_beside_a_member_the_route_would_now_refuse(fixture, fields):
    project = _project([_row("item-1", 10, 40, [_member("a", visual_intent="preserve"),
                                                _member("b")])])
    fixture(project)
    stored = _item(project, "item-1").to_dict()
    key, value = next(iter(fields.items()))
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "item-1",
                      "fields": fields, "expected": {key: stored[key]}}])
    after = _item(_saved(project), "item-1").to_dict()
    assert after[key] == value
    assert after["members"] == stored["members"]


def test_a_member_write_still_revalidates_every_member():
    project = _project([_row("item-1", 10, 40, [_member("a"), _member("b")])])
    _trash_b(project)
    stored = _item(project, "item-1").to_dict()["members"]
    reordered = [stored[1], stored[0]]
    assert _refusal(project, [{
        "type": "update_reference_item", "reference_item_id": "item-1",
        "fields": {"members": reordered}, "expected": {"members": stored}}]) == "asset_not_found"


def test_a_lane_move_still_validates_population_and_overlap():
    project = _project([
        _row("item-1", 10, 40, [_member("a")]),
        _row("item-2", 0, 30, [_member("s")], lane=1),
        _row("item-3", 50, 60, [_member("s")], lane=1),
    ])
    # The audio lane's population refuses the image member.
    assert _refusal(project, [{
        "type": "update_reference_item", "reference_item_id": "item-1",
        "fields": {"lane_index": 1}, "expected": {"lane_index": 0}}]) == (
        "reference_media_kind_mismatch")
    # Moving an audio item onto a lane already holding one at that span.
    _item(project, "item-3").start_frame = 20
    _item(project, "item-3").end_frame = 30
    project.get_scene("scene-1").reference_lane_recipes[0] = ReferenceLaneRecipe(
        lane_id="lane-image", media_kind="audio", recipe={})
    project.get_scene("scene-1").reference_items.append(
        ReferenceItem.from_dict(_row("item-4", 20, 30, [_member("s")], lane=0)))
    assert _refusal(project, [{
        "type": "update_reference_item", "reference_item_id": "item-3",
        "fields": {"lane_index": 0}, "expected": {"lane_index": 1}}]) == "lane_collision"


# -- D8: a write changes only the bounds it names ------------------------------------

def test_a_start_write_on_a_straddling_item_keeps_its_stored_end():
    project = _project([_row("item-1", 80, 150, [_member("a")])])
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "item-1",
                      "fields": {"start_frame": 85}, "expected": {"start_frame": 80}}])
    after = _item(_saved(project), "item-1")
    assert (after.start_frame, after.end_frame) == (85, 150)


def test_an_end_write_keeps_the_stored_start_and_still_clamps_the_named_end():
    project = _project([_row("item-1", 80, 150, [_member("a")])])
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "item-1",
                      "fields": {"end_frame": 400}, "expected": {"end_frame": 150}}])
    after = _item(project, "item-1")
    assert (after.start_frame, after.end_frame) == (80, 100)


def test_a_start_write_past_a_kept_end_is_refused():
    project = _project([_row("item-1", 10, 40, [_member("a")])])
    assert _refusal(project, [{
        "type": "update_reference_item", "reference_item_id": "item-1",
        "fields": {"start_frame": 40}, "expected": {"start_frame": 10}}]) == "invalid_range"


def test_a_scalar_write_on_two_squashed_items_saves():
    # Two items a pre-Phase-3 duration shrink squashed onto the last frame.
    project = _project([_row("item-1", 99, 100, [_member("a")]),
                        _row("item-2", 99, 100, [_member("b")])])
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "item-1",
                      "fields": {"strength": 0.25}, "expected": {"strength": 1.0}}])
    assert _item(_saved(project), "item-1").strength == 0.25


def test_a_scalar_write_keeps_a_stored_start_past_the_scene():
    project = _project([_row("item-1", 150, -1, [_member("a")])])
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "item-1",
                      "fields": {"muted": True}, "expected": {"muted": False}}])
    after = _item(_saved(project), "item-1")
    assert (after.start_frame, after.end_frame, after.muted) == (150, -1, True)


def test_a_range_write_into_another_item_is_still_refused():
    project = _project([_row("item-1", 10, 40, [_member("a")]),
                        _row("item-2", 50, 70, [_member("b")])])
    assert _refusal(project, [{
        "type": "update_reference_item", "reference_item_id": "item-1",
        "fields": {"end_frame": 60}, "expected": {"end_frame": 40}}]) == "lane_collision"


def test_a_range_write_on_a_squashed_item_still_checks_overlap():
    # A bound that changes re-derives the range and checks overlap; only a
    # write that leaves lane and range as stored skips the check.
    project = _project([_row("item-1", 99, 100, [_member("a")]),
                        _row("item-2", 99, 100, [_member("b")])])
    assert _refusal(project, [{
        "type": "update_reference_item", "reference_item_id": "item-1",
        "fields": {"start_frame": 98}, "expected": {"start_frame": 99}}]) == "lane_collision"


def test_a_left_trim_sending_both_bounds_keeps_the_end_it_did_not_change():
    # The trim and drag commits send both bounds; one sent at its stored value
    # counts as unnamed, so a straddling item's end is not cut to the scene.
    project = _project([_row("item-1", 80, 150, [_member("a")])])
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "item-1",
                      "fields": {"start_frame": 85, "end_frame": 150},
                      "expected": {"start_frame": 80, "end_frame": 150}}])
    after = _item(_saved(project), "item-1")
    assert (after.start_frame, after.end_frame) == (85, 150)


# -- Phase 6 (D5): an unpainted write and the bounds ---------------------------------

@pytest.mark.parametrize("start,end", [(80, 120), (110, 130), (60, -1), (10, 40)])
def test_a_member_write_keeps_both_stored_bounds(start, end):
    """What the editor sends unpainted -- an authored member role or retention --
    names neither bound, so it leaves both as stored, even on an item straddling
    the scene end, lying past it, or running to it (`-1`). This is why the
    timeline's move, trim and split need not wait on such a write for their
    range: the guards cover only a write that names one."""
    project = _project([_row("item-1", start, end, [_member("a"), _member("b")])])
    stored = _item(project, "item-1").to_dict()["members"]
    authored = [{**stored[0], "visual_intent": "partial"}, stored[1]]
    _batch(project, [{"type": "update_reference_item", "reference_item_id": "item-1",
                      "fields": {"members": authored}, "expected": {"members": stored}}])
    row = _item(project, "item-1")
    assert (row.start_frame, row.end_frame) == (start, end)
    assert row.members[0]["visual_intent"] == "partial"
