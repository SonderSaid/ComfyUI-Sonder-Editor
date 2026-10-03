"""Intentional Python/JavaScript parity for optimistic Reference item paints.

``web/js/scene_reference_geometry.js`` mirrors, for a staging paint:

* ``_reference_item_bounds``, which clamps a start into the scene, preserves the
  ``-1`` "runs to scene end" sentinel, and refuses a range that inverts; and
* the RECORD ``_canonical_reference_member_refs`` builds -- ``entity_id`` derived
  from the reference that owns the member, and ``member_id``. A member carrying
  any other stored field is refused rather than painted, for reasons the module
  states and ``test_a_member_carrying_a_stored_role_field_makes_the_list_unpaintable``
  pins; and
* the ROW ``_apply_create_reference_item`` appends, field for field, including
  the defaults a staging gesture never sends.

For the Reference Lane Setup panel's item edits it also mirrors the lane-overlap
decision (``_reference_overlapping_items``) and the row
``_apply_update_reference_item`` leaves -- or its refusal, or a decline where the
route's answer rests on authority the client does not hold. Those tables are at
the end of this file.

For a Library member or Reference delete it mirrors which staged rows
``_reconcile_staged_reference_members`` removes and thins, and the members a
thinned row keeps; that table is last.

**Why the member half is mirrored at all** is the part a later reader is most
likely to try to undo, so it is stated here as well as beside the code. Phase C
§3 decided the Reference local apply must be geometry-only, on the reasoning
that painting ``item.members`` would make the next append send an ``expected``
the server never stored. Probed against the route, that is wrong in both halves:
the canonical record for a Library drop is byte-identical to what ``dragPayload``
sends, and it is NOT painting that loses a drop -- an append reads its
``expected`` guard from the live row before its await, so a second drop inside
one in-flight window guards against state the route has already left. ``test_two_appends_in_one_window`` below holds the ROUTE half of
that. It does not hold the gesture half -- removing the paint from
``editor_widget.js`` leaves every test in this file green -- so the reversal is
pinned jointly with ``test_an_append_reads_its_guard_before_the_paint`` and
``test_an_append_whose_PRIOR_member_cannot_be_resolved_paints_nothing`` in
``test_project_mutation_queue.py``. Neither file is sufficient alone, and
believing otherwise is how the paint gets removed as dead weight.

The drift this file prevents is silent and durable. ``_pushUndo`` snapshots
``activeScene`` verbatim and a history restore PUTs that snapshot as the merge
target, so a painted member record that differs from the stored one reaches disk
as soon as a later gesture in the same window pushes an entry.

**What this file does not protect.** Applicability is not mirrored and is not
compared here -- unresolvable members, missing assets, duplicate members,
incompatible populations, occupied spans and locked or collapsed lanes are all
decided by ``resolveReferenceDropVerdict`` before either gesture builds an
operation, and ``member_population_compatible`` already carries its own parity
test in ``test_reference_timeline.py``. Intent and role VALUES are likewise
server-authoritative, and the mirror does not carry them at all -- a member
holding one makes the whole list unpaintable, because reproducing the route's
recipe- and profile-dependent validation here would duplicate authority this
file has no business holding, while copying the values unvalidated paints a
record the save would refuse or rewrite.

**An append is planned as an update.** ``_apply_update_reference_item``
re-canonicalizes the whole list including priors, so a prior whose asset was
trashed, whose lane population narrowed, or whose stored role the route would
trim is refused for a member the drop resolver never examined. The append
therefore goes through ``plannedReferenceItemUpdate`` like every other
staged-item update, and its shapes are rows of ``UPDATE_CASES``.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import routes
from server.timeline_state import (
    Asset,
    LaneConfig,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    STAGED_MEMBER_FIELDS,
    Scene,
    TimelineProject,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "scene_reference_geometry.js").as_uri()


def _project(member_ids=("member-a", "member-b", "member-c"), media_kind="image"):
    asset_type = "audio" if media_kind == "audio" else "image"
    assets, members = [], []
    for member_id in member_ids:
        asset_id = f"asset-{member_id}"
        assets.append(Asset(asset_id=asset_id, name=member_id,
                            asset_type=asset_type, path=f"media/{member_id}.png"))
        members.append(ReferenceMember(member_id=member_id, asset_id=asset_id))
    reference = ReferenceEntity(reference_id="entity-1", name="Subject",
                                members=members)
    scene = Scene(
        scene_id="scene-1", duration_frames=100, reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(media_kind=media_kind)],
    )
    project = TimelineProject(project_id="p", assets=assets,
                              references=[reference], scenes=[scene])
    return project, scene


def _drag(member_id):
    """The Library drag payload shape, verbatim from `editor_reference_library.js`."""
    return {"entity_id": "entity-1", "member_id": member_id}


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for reference geometry parity")
    # On stdin, not `-e`: the update table is longer than a Windows command line.
    result = subprocess.run([node, "--input-type=module"], input=script,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


# (scene duration, requested start, requested end). Chosen to cross every branch
# the two implementations can disagree on, not to look realistic.
BOUNDS_CASES = [
    (100, 0, -1),        # the sentinel, from the very start
    (100, 40, -1),       # the sentinel, mid-scene
    (100, 40, 80),       # an ordinary bounded bar
    (100, 40, 100),      # ends exactly at scene duration
    (100, 40, 400),      # past the end: clamped to duration on both sides
    (100, 400, -1),      # start past the end: clamped to duration - 1
    (100, -7, -1),       # negative start: max(0, ...) on both sides
    (100, 40.9, 80.9),   # fractional: truncated, not rounded
    (0, 40, 80),         # a scene with no duration: no clamping arm runs
    (100, 40, 40),       # inverted-by-equality: refused on both sides
    (100, 40, 10),       # inverted: refused on both sides
    # An end of 0 is a frame, not the sentinel. The mirror used to read it
    # through `|| -1` and paint "runs to scene end" for a range the route
    # refuses as inverted.
    (100, 40, 0),
    (100, 0, 0),
    # `null` is what a request carries for an absent or non-finite value, and
    # the route reads it as the field's default.
    (100, None, None),
    (100, 40, None),
]


def _javascript_bounds(cases):
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const cases = {json.dumps([list(case) for case in cases])};
console.log(JSON.stringify(cases.map(([duration, start, end]) =>
  mod.referenceItemBounds(duration, start, end))));
"""
    return _node(script)


def _python_bounds(cases):
    out = []
    for duration, start, end in cases:
        scene = Scene(scene_id="s", duration_frames=duration)
        try:
            start_frame, end_frame = routes._reference_item_bounds(scene, start, end)
        except routes.ProjectMutationRequestError as exc:
            # The mirror returns the bounds it had computed alongside the
            # refusal, so compare the DECISION and the partial arithmetic both.
            assert exc.code == "invalid_range", exc.code
            start_frame = max(0, int(start if start is not None else 0))
            if duration > 0:
                start_frame = min(start_frame, duration - 1)
            out.append({"startFrame": start_frame,
                        "endFrame": int(end if end is not None else -1),
                        "refusal": "invalid_range"})
            continue
        out.append({"startFrame": start_frame, "endFrame": end_frame, "refusal": ""})
    return out


def test_reference_bounds_agree_in_both_languages():
    assert _javascript_bounds(BOUNDS_CASES) == _python_bounds(BOUNDS_CASES)


def test_an_inverted_range_is_refused_on_both_sides_rather_than_repaired():
    """A refusal is a decision, and the mirror compares decisions.

    The Class A landing's audit found a mirror that reproduced the server's
    arithmetic and one of its four refusal conditions, so an out-of-scene row was
    painted in full. Painting a bar the route will reject is worse than painting
    nothing: the bar is on the author's timeline until the response lands, and
    `_pushUndo` can carry it to disk in between.
    """
    [refused] = _javascript_bounds([(100, 40, 10)])
    assert refused["refusal"] == "invalid_range"
    scene = Scene(scene_id="s", duration_frames=100)
    with pytest.raises(routes.ProjectMutationRequestError) as excinfo:
        routes._reference_item_bounds(scene, 40, 10)
    assert excinfo.value.code == "invalid_range"


def test_a_fractional_bound_truncates_rather_than_rounding():
    [fractional] = _javascript_bounds([(100, 40.9, 80.9)])
    assert fractional == {"startFrame": 40, "endFrame": 80, "refusal": ""}


def test_an_end_of_zero_is_refused_rather_than_read_as_the_sentinel():
    """`Number(0) || -1` is `-1`: the old mirror painted a refused range to scene end."""
    [zero] = _javascript_bounds([(100, 40, 0)])
    assert zero == {"startFrame": 40, "endFrame": 0, "refusal": "invalid_range"}
    with pytest.raises(routes.ProjectMutationRequestError) as excinfo:
        routes._reference_item_bounds(Scene(scene_id="s", duration_frames=100), 40, 0)
    assert excinfo.value.code == "invalid_range"


def test_a_non_finite_bound_reads_as_the_null_the_request_carries():
    """`JSON.stringify` writes NaN and Infinity as `null`; the route reads the default."""
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
console.log(JSON.stringify([
  mod.referenceItemBounds(100, NaN, Infinity),
  mod.referenceItemBounds(100, Infinity, NaN),
  mod.referenceItemBounds(100, null, null),
]));
"""
    non_finite_start, infinite_start, null_both = _node(script)
    scene = Scene(scene_id="s", duration_frames=100)
    assert routes._reference_item_bounds(scene, None, None) == (0, -1)
    expected = {"startFrame": 0, "endFrame": -1, "refusal": ""}
    assert non_finite_start == infinite_start == null_both == expected


MEMBER_CASES = [
    [_drag("member-a")],
    [_drag("member-a"), _drag("member-b")],
    # `entity_id` is RE-DERIVED on both sides, so a stale one in the payload is
    # corrected rather than stored. Without that, the divergence would surface
    # as the NEXT append's `identity_mismatch` instead of here.
    [{"entity_id": "stale-entity", "member_id": "member-b"}],
    # Order is preserved on both sides, and a three-member list is where a
    # mirror that rebuilt from a set or a dict would diverge.
    [_drag("member-c"), _drag("member-a"), _drag("member-b")],
]


def _javascript_members(cases):
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const cases = {json.dumps(cases)};
const owns = new Set(["member-a", "member-b", "member-c"]);
console.log(JSON.stringify(cases.map((members) =>
  mod.canonicalStagedMemberRefs(members, (id) => owns.has(id) ? "entity-1" : ""))));
"""
    return _node(script)


def _python_members(cases):
    project, _scene = _project()
    recipe = {"soft": {"role_fields": ["visual_intent", "audio_intent"]}}
    return [routes._canonical_reference_member_refs(
        project, members, "image", recipe, "") for members in cases]


def test_the_painted_member_record_is_the_record_the_route_stores():
    assert _javascript_members(MEMBER_CASES) == _python_members(MEMBER_CASES)


def test_a_member_the_project_does_not_hold_paints_nothing():
    """`null`, not a partial list: the route answers 404 for the whole operation.

    Painting the resolvable half would leave the bar showing members the save is
    about to refuse wholesale.
    """
    [absent] = _javascript_members([[_drag("member-missing")]])
    assert absent is None
    project, _scene = _project()
    with pytest.raises(routes.ProjectMutationRequestError) as excinfo:
        routes._canonical_reference_member_refs(
            project, [_drag("member-missing")], "image", {}, "")
    assert excinfo.value.code == "item_not_found"


def test_a_member_named_twice_paints_nothing():
    [duplicate] = _javascript_members([[_drag("member-a"), _drag("member-a")]])
    assert duplicate is None
    project, _scene = _project()
    with pytest.raises(routes.ProjectMutationRequestError) as excinfo:
        routes._canonical_reference_member_refs(
            project, [_drag("member-a"), _drag("member-a")], "image", {}, "")
    assert excinfo.value.code == "invalid_reference_item"


def _append(project, scene, item_id, prior, added):
    """One `_appendReferenceMembersWithinGesture` emission."""
    return routes._apply_update_reference_item(project, scene, {
        "reference_item_id": item_id,
        "fields": {"members": [*prior, *added]},
        "expected": {"members": prior},
    })


def test_two_appends_in_one_window_both_land_when_the_first_is_painted():
    """The reversal of Phase C §3, held in both directions.

    The gesture reads its guard from the live row before its await, so the second drop's
    guard describes whatever the local scene holds at that moment. With the
    paint that is the list the route is about to store; without it, it is the
    list the route has already replaced.
    """
    project, scene = _project()
    scene.reference_items = [ReferenceItem(
        reference_item_id="item-1", lane_index=0, start_frame=0, end_frame=50,
        members=[_drag("member-a")])]
    item = scene.reference_items[0]

    # Painted: gesture two sees [a, b] because gesture one wrote it locally.
    first_prior = [dict(member) for member in item.members]
    second_prior = [*first_prior, _drag("member-b")]
    _append(project, scene, "item-1", first_prior, [_drag("member-b")])
    _append(project, scene, "item-1", second_prior, [_drag("member-c")])
    assert [member["member_id"] for member in item.members] == [
        "member-a", "member-b", "member-c"]


def test_without_the_paint_the_second_append_in_one_window_is_lost():
    """The defect the paint removes, pinned so it cannot come back unnoticed.

    Both gestures read the same unpainted `[a]`, so the second one's `expected`
    describes a state the route left when it applied the first. The author's
    second drop is refused and nothing tells them to repeat it.
    """
    project, scene = _project()
    scene.reference_items = [ReferenceItem(
        reference_item_id="item-1", lane_index=0, start_frame=0, end_frame=50,
        members=[_drag("member-a")])]
    item = scene.reference_items[0]
    unpainted = [dict(member) for member in item.members]

    _append(project, scene, "item-1", [dict(m) for m in unpainted], [_drag("member-b")])
    with pytest.raises(routes.ProjectMutationRequestError) as excinfo:
        _append(project, scene, "item-1", [dict(m) for m in unpainted],
                [_drag("member-c")])
    assert excinfo.value.code == "identity_mismatch"
    assert [member["member_id"] for member in item.members] == [
        "member-a", "member-b"]
# -- the painted ROW, field for field ----------------------------------------
#
# `stagedReferenceItem` writes every field of the bar the author sees, and the
# first version of this file did not exercise it at all: the gesture tests assert
# `start_frame`, `end_frame`, `members` and the id, so drifting
# `prompt_override`, `strength`, `sequence_frames` or `muted` -- or deleting the
# refusal gate outright -- passed the whole suite. An audit proved that by
# mutation. Those are exactly the fields the module's own docstring says must be
# the ROUTE's defaults rather than the caller's, because `_pushUndo` snapshots
# the scene verbatim and a history restore can PUT that snapshot back.

def _javascript_row(spec):
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const spec = {json.dumps(spec)};
const owns = new Set(["member-a", "member-b", "member-c"]);
const members = mod.canonicalStagedMemberRefs(
    spec.members, (id) => owns.has(id) ? "entity-1" : "");
console.log(JSON.stringify(mod.stagedReferenceItem({{...spec, members}})));
"""
    return _node(script)


def _python_row(spec):
    project, scene = _project()
    scene.duration_frames = spec["durationFrames"]
    scene.reference_lane_count = 1
    try:
        item = routes._apply_create_reference_item(project, scene, {
            "reference_item_id": spec["referenceItemId"],
            "lane_index": spec["laneIndex"],
            "start_frame": spec["startFrame"],
            "end_frame": spec["endFrame"],
            "members": spec["members"],
        })
    except routes.ProjectMutationRequestError:
        return None
    return item.to_dict()


ROW_SPECS = [
    {"referenceItemId": "ref-aaaa1111", "laneIndex": 0, "startFrame": 40,
     "endFrame": -1, "durationFrames": 100, "members": [_drag("member-a")]},
    {"referenceItemId": "ref-bbbb2222", "laneIndex": 0, "startFrame": 0,
     "endFrame": 50, "durationFrames": 100,
     "members": [_drag("member-a"), _drag("member-b")]},
    {"referenceItemId": "ref-cccc3333", "laneIndex": 0, "startFrame": 40.9,
     "endFrame": 400, "durationFrames": 100, "members": [_drag("member-c")]},
    # Refused on both sides: the mirror paints nothing, the route raises.
    {"referenceItemId": "ref-dddd4444", "laneIndex": 0, "startFrame": 40,
     "endFrame": 10, "durationFrames": 100, "members": [_drag("member-a")]},
]


def test_the_painted_row_matches_the_stored_row_field_for_field():
    """Every field, not just the ones a gesture test happens to look at."""
    for spec in ROW_SPECS:
        painted = _javascript_row(spec)
        stored = _python_row(spec)
        assert painted == stored, spec["referenceItemId"]


def test_the_painted_row_carries_the_routes_defaults_not_the_callers():
    """Pinned by value, so a drifted default fails here and not in a live project."""
    painted = _javascript_row(ROW_SPECS[0])
    assert painted["prompt_override"] == ""
    assert painted["strength"] == 1.0
    assert painted["sequence_frames"] == 0
    assert painted["muted"] is False
    assert painted["lane_index"] == 0


def test_a_row_the_route_would_refuse_is_not_painted():
    assert _javascript_row(ROW_SPECS[3]) is None
    assert _python_row(ROW_SPECS[3]) is None


# -- fields the mirror refuses rather than reproduces -------------------------

INTENT_FIELDS = [
    ("visual_intent", "preserve"),
    ("visual_intent", "   "),
    ("visual_intent", "nope"),
    ("audio_intent", "copy_full"),
    ("role", "subject"),
    ("role", "  subject  "),
]


def test_a_member_carrying_a_stored_role_field_makes_the_list_unpaintable():
    """Fail closed, because the route's validation cannot be mirrored honestly.

    Each of these diverges from `_canonical_reference_member_refs` in a different
    way -- `role_fields` gating, media-kind gating, the `VISUAL_INTENTS` /
    `AUDIO_INTENTS` tables, `normalize_reference_role` against the project's
    prompt-context profiles, and whitespace trimming. An earlier version copied
    them through verbatim and diverged on all six; no caller supplies them, so
    refusing costs nothing and keeps the divergence impossible rather than
    merely absent.
    """
    cases = [[{"entity_id": "entity-1", "member_id": "member-a", field: value}]
             for field, value in INTENT_FIELDS]
    assert _javascript_members(cases) == [None] * len(cases)


def test_an_unknown_member_field_is_refused_too():
    """Including one nobody has added yet.

    A member record that grows a fourth stored field would otherwise be painted
    without it and diverge silently -- the failure `_pushUndo` turns durable.
    """
    [painted] = _javascript_members(
        [[{"entity_id": "entity-1", "member_id": "member-a", "future_field": "x"}]])
    assert painted is None
# -- priors: the members an append re-sends without the author touching them ---

def test_a_stored_member_round_trips_through_an_append_unchanged():
    """Why priors may carry role fields where a fresh drop may not.

    `_apply_update_reference_item` passes `legacy_members=item.members`, and
    `_canonical_reference_member_refs` suppresses every intent and role check
    whose value is `unchanged` against that legacy record. So the route returns a
    stored member byte-identically -- even under a recipe that has since stopped
    exposing the field, which is the case that decides this.
    """
    project, _scene = _project()
    stored = {"entity_id": "entity-1", "member_id": "member-a",
              "visual_intent": "preserve"}
    added = _drag("member-b")
    exposed = {"soft": {"role_fields": ["visual_intent", "audio_intent"]}}
    narrowed = {"soft": {}}

    under_original = routes._canonical_reference_member_refs(
        project, [stored, added], "image", exposed, "", [stored])
    under_narrowed = routes._canonical_reference_member_refs(
        project, [stored, added], "image", narrowed, "", [stored])
    assert under_original == [stored, added]
    assert under_narrowed == [stored, added], "legacy_members is what protects it"

    # Without a legacy entry the same value is refused, which is exactly why the
    # staged form fails closed and the stored form does not.
    with pytest.raises(routes.ProjectMutationRequestError) as excinfo:
        routes._canonical_reference_member_refs(
            project, [stored, added], "image", narrowed, "", None)
    assert excinfo.value.code == "unsupported_reference_member_field"
    # The mirror half is the UPDATE_CASES row "append after the recipe stopped
    # exposing retention", which the planner paints and the route stores.


# -- the overlap decision -------------------------------------------------------
#
# `referenceItemOverlap` mirrors `_reference_overlapping_items`, which every
# create and update runs after the bounds. Both clamps are where a mirror would
# drift: a requested range that collapsed to nothing is widened to one frame,
# and a stored `-1` resolves to `max(start + 1, duration)` rather than to the
# duration alone.

OVERLAP_ROWS = [
    # (id, lane, start, stored end)
    ("left", 0, 10, 40),
    ("right", 0, 50, 70),
    ("tail", 0, 80, -1),
    ("other-lane", 1, 0, 100),
    ("past-end", 2, 120, -1),   # start past the duration: -1 resolves to start + 1
]

# (lane, start, end, duration, ignore id, expected first overlapping id)
OVERLAP_CASES = [
    (0, 40, 50, 100, "", None),            # exactly between two rows: touching is legal
    (0, 39, 50, 100, "", "left"),          # one frame into the left row
    (0, 45, 51, 100, "", "right"),
    (0, 70, 80, 100, "", None),            # touches both neighbours
    (0, 75, -1, 100, "", "tail"),          # a requested sentinel runs to the duration
    (0, 99, -1, 100, "", "tail"),
    (0, 95, 95, 100, "", "tail"),          # inside "tail" with or without the widening
    (0, 50, 50, 100, "", "right"),         # collapsed range widened to one frame: decisive
    (0, 55, -1, 0, "", "right"),           # sentinel with no duration: widened, decisive
    (0, 20, 30, 100, "left", None),        # the row itself is ignored
    (0, 20, 60, 100, "left", "right"),
    (1, 40, 50, 100, "", "other-lane"),
    (2, 120, 121, 100, "", "past-end"),    # stored -1 resolves to start + 1, not 100
    (2, 121, 125, 100, "", None),
    # A requested -1 runs without limit: it overlaps a later row even one past
    # the current end, which it would overlap once the scene grew back.
    (2, 100, -1, 100, "", "past-end"),
    (0, 75, -1, 60, "", "tail"),           # "tail" lies past a 60-frame end
    (0, 85, -1, 60, "", None),             # nothing on lane 0 starts after 85
    (0, 40, 50, 0, "", None),              # no duration: -1 rows resolve to start + 1
    (0, 80, 81, 0, "", "tail"),
]


def _overlap_rows():
    return [{"reference_item_id": row_id, "lane_index": lane, "start_frame": start,
             "end_frame": end, "members": [_drag("member-a")]}
            for row_id, lane, start, end in OVERLAP_ROWS]


def _javascript_overlap(cases):
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const rows = {json.dumps(_overlap_rows())};
const cases = {json.dumps([list(case) for case in cases])};
console.log(JSON.stringify(cases.map(([laneIndex, startFrame, endFrame, durationFrames, ignoreId]) =>
  mod.referenceItemOverlap(rows, {{ laneIndex, startFrame, endFrame, durationFrames, ignoreId }})
    ?.reference_item_id ?? null)));
"""
    return _node(script)


def _python_overlap(cases):
    out = []
    for lane, start, end, duration, ignore_id, _expected in cases:
        scene = Scene(scene_id="s", duration_frames=duration, reference_items=[
            ReferenceItem.from_dict(row) for row in _overlap_rows()])
        ignore = next((item for item in scene.reference_items
                       if item.reference_item_id == ignore_id), None)
        first = next(routes._reference_overlapping_items(
            scene, lane, start, end, ignore=ignore), None)
        out.append(first.reference_item_id if first is not None else None)
    return out


def test_the_overlap_decision_agrees_in_both_languages():
    javascript = _javascript_overlap(OVERLAP_CASES)
    python = _python_overlap(OVERLAP_CASES)
    assert javascript == python
    assert python == [case[-1] for case in OVERLAP_CASES], "the table pins the decision"


# -- the planned row of an update -----------------------------------------------
#
# `plannedReferenceItemUpdate` answers one of three things, and each is compared
# with the real `_apply_update_reference_item` on identical data:
#
#   * a REFUSAL must be one the route makes -- a caller refuses locally and
#     sends nothing, so a refusal the route would not make is an edit silently
#     lost. Every case in the table has one cause, and there the code matches
#     too; with several causes the two sides may name different ones;
#   * a PAINT must be the row the route stores, field for field -- `_pushUndo`
#     snapshots the painted scene verbatim;
#   * a DECLINE sends the write unpainted. It is the only answer allowed where
#     the route's decision rests on authority the mirror does not hold (recipe
#     and profile validation of an authored role or intent), or on the client's
#     copy of the Library being current. What the route does instead is pinned
#     per case, so a decline cannot be mistaken for caution.

def _update_fixture():
    assets = [
        Asset(asset_id="asset-a", name="a", asset_type="image", path="media/a.png"),
        Asset(asset_id="asset-b", name="b", asset_type="image", path="media/b.png"),
        Asset(asset_id="asset-c", name="c", asset_type="image", path="media/c.png"),
        Asset(asset_id="asset-x", name="x", asset_type="image", path="media/x.png"),
        Asset(asset_id="asset-s", name="s", asset_type="audio", path="media/s.wav"),
        Asset(asset_id="asset-va", name="va", asset_type="video", path="media/va.mp4",
              has_audio=True),
    ]
    subject = ReferenceEntity(reference_id="entity-1", name="Subject", members=[
        ReferenceMember(member_id=f"member-{key}", asset_id=f"asset-{key}")
        for key in ("a", "b", "c", "s", "va")])
    other = ReferenceEntity(reference_id="entity-2", name="Other", members=[
        ReferenceMember(member_id="member-x", asset_id="asset-x")])

    def member(key, **extra):
        return {"entity_id": "entity-1", "member_id": f"member-{key}", **extra}

    scene = Scene(
        scene_id="scene-1", duration_frames=100, reference_lane_count=2,
        reference_lane_configs=[LaneConfig(), LaneConfig()],
        reference_lane_recipes=[
            ReferenceLaneRecipe(lane_id="lane-image", media_kind="image", recipe={
                "soft": {"physical_population": "pictures",
                         "role_fields": ["role", "visual_intent"]}}),
            ReferenceLaneRecipe(lane_id="lane-audio", media_kind="audio", recipe={
                "soft": {"role_fields": ["audio_intent"]}}),
        ],
        reference_items=[ReferenceItem.from_dict(row) for row in (
            {"reference_item_id": "item-1", "lane_index": 0, "start_frame": 10,
             "end_frame": 40, "members": [member("a", visual_intent="preserve"),
                                          member("b")]},
            {"reference_item_id": "item-2", "lane_index": 0, "start_frame": 50,
             "end_frame": 70, "members": [member("c")]},
            {"reference_item_id": "item-3", "lane_index": 0, "start_frame": 80,
             "end_frame": -1, "members": [member("a")], "strength": 0.4,
             "sequence_frames": 17, "prompt_override": "kept", "muted": True},
            {"reference_item_id": "item-4", "lane_index": 1, "start_frame": 0,
             "end_frame": 30, "members": [
                 member("s", audio_intent="reference_characteristics"),
                 member("va")]},
        )],
    )
    project = TimelineProject(project_id="p", assets=assets,
                              references=[subject, other], scenes=[scene])
    return project, scene


def _item(scene, item_id):
    return next(item for item in scene.reference_items
                if item.reference_item_id == item_id)


# Fixture edits applied to BOTH sides before a case runs: states a live project
# can hold that the mirror must notice.
def _stale_entity(project, scene):
    _item(scene, "item-1").members[1]["entity_id"] = "entity-2"


def _trashed_asset(project, scene):
    project.assets = [asset for asset in project.assets if asset.asset_id != "asset-b"]


def _narrowed_population(project, scene):
    scene.reference_lane_recipes[0].recipe["soft"]["physical_population"] = "videos"


def _untrimmed_role(project, scene):
    _item(scene, "item-1").members[0]["role"] = " identity "


def _stored_overlap(project, scene):
    _item(scene, "item-2").start_frame = 30


def _stored_inverted(project, scene):
    _item(scene, "item-2").end_frame = 50


def _stored_start_past_scene(project, scene):
    _item(scene, "item-3").start_frame = 150


def _squashed(project, scene):
    # What a pre-step-1 duration shrink did to rows lying past the new end.
    for item_id in ("item-2", "item-3"):
        _item(scene, item_id).start_frame = 99
        _item(scene, item_id).end_frame = 100


def _straddling(project, scene):
    _item(scene, "item-3").end_frame = 150


def _past_end(project, scene):
    _item(scene, "item-3").start_frame = 150
    _item(scene, "item-3").end_frame = 170


def _missing_recipe(project, scene):
    # The route pads a missing slot with `ReferenceLaneRecipe()`, an image lane.
    scene.reference_lane_recipes = scene.reference_lane_recipes[:1]


def _orphan_lane(project, scene):
    # `_set_scene_lane_count` does not check items, so a row can outlive its lane.
    scene.reference_lane_count = 1


def _retention_withdrawn(project, scene):
    # The recipe stopped exposing a field a stored member still carries.
    scene.reference_lane_recipes[0].recipe["soft"]["role_fields"] = ["role"]


def _members(*keys):
    return [{"entity_id": "entity-1", "member_id": f"member-{key}"} for key in keys]


PRESERVED_A = {"entity_id": "entity-1", "member_id": "member-a", "visual_intent": "preserve"}

# (name, item id, fields, fixture edit or None, expected: "paint" | "decline" |
# "refuse:<code>")
UPDATE_CASES = [
    ("strength", "item-1", {"strength": 0.5}, None, "paint"),
    ("strength clamped high", "item-1", {"strength": 1.7}, None, "paint"),
    ("strength clamped low", "item-1", {"strength": -3}, None, "paint"),
    ("strength null", "item-1", {"strength": None}, None,
     "refuse:invalid_project_mutation"),
    ("sequence truncated", "item-1", {"sequence_frames": 12.9}, None, "paint"),
    ("sequence clamped high", "item-1", {"sequence_frames": 9999}, None, "paint"),
    ("sequence clamped low", "item-1", {"sequence_frames": -5}, None, "paint"),
    ("sequence null", "item-1", {"sequence_frames": None}, None,
     "refuse:invalid_project_mutation"),
    ("override", "item-1", {"prompt_override": "hello"}, None, "paint"),
    ("override cleared by null", "item-3", {"prompt_override": None}, None, "paint"),
    ("mute", "item-1", {"muted": True}, None, "paint"),
    ("mute by one", "item-1", {"muted": 1}, None, "paint"),
    ("unmute by zero", "item-3", {"muted": 0}, None, "paint"),
    # A cleared number input is NaN, which the request carries as null: the
    # route reads the default, NOT the stored value.
    ("start null reads as 0", "item-2", {"start_frame": None}, None,
     "refuse:lane_collision"),
    ("end null reads as the sentinel", "item-1", {"end_frame": None}, None,
     "refuse:lane_collision"),
    ("keeps untouched scalars", "item-3", {"strength": 0.9}, None, "paint"),
    ("start", "item-1", {"start_frame": 20}, None, "paint"),
    ("start past the end", "item-1", {"start_frame": 45}, None, "refuse:invalid_range"),
    ("end of zero", "item-1", {"end_frame": 0}, None, "refuse:invalid_range"),
    ("end into the next row", "item-1", {"end_frame": 60}, None, "refuse:lane_collision"),
    ("end to scene end", "item-1", {"end_frame": -1}, None, "refuse:lane_collision"),
    ("end touching the next row", "item-1", {"end_frame": 50}, None, "paint"),
    ("start touching the previous row", "item-2", {"start_frame": 40}, None, "paint"),
    ("start clamped into the scene", "item-3", {"start_frame": 200}, None, "paint"),
    ("end clamped into the next row", "item-2", {"end_frame": 400}, None,
     "refuse:lane_collision"),
    ("reorder", "item-1", {"members": [_members("b")[0], PRESERVED_A]}, None, "paint"),
    ("reorder carrying moveMember's order key", "item-1",
     {"members": [{**_members("b")[0], "order": 0}, {**PRESERVED_A, "order": 1}]},
     None, "paint"),
    ("remove", "item-1", {"members": [PRESERVED_A]}, None, "paint"),
    ("remove the last member", "item-1", {"members": []}, None,
     "refuse:invalid_reference_item"),
    ("a member named twice", "item-1", {"members": [PRESERVED_A, PRESERVED_A]}, None,
     "refuse:invalid_reference_item"),
    ("audio lane scalar", "item-4", {"strength": 0.3}, None, "paint"),
    # D8: a write changes only the bounds it names, and an unchanged lane and
    # range create no new overlap, so stored range defects do not block it.
    ("scalar beside a stored overlap", "item-1", {"strength": 0.5}, _stored_overlap,
     "paint"),
    ("scalar on a stored inverted range", "item-2", {"strength": 0.5},
     _stored_inverted, "paint"),
    ("scalar on squashed items", "item-2", {"strength": 0.5}, _squashed, "paint"),
    ("start on a squashed item", "item-2", {"start_frame": 98}, _squashed,
     "refuse:lane_collision"),
    ("start on a straddling item keeps its end", "item-3", {"start_frame": 85},
     _straddling, "paint"),
    ("end on a straddling item is clamped", "item-3", {"end_frame": 400},
     _straddling, "paint"),
    ("start past a straddling item's kept end", "item-3", {"start_frame": 160},
     _straddling, "paint"),
    ("start onto a kept end", "item-1", {"start_frame": 40}, None, "refuse:invalid_range"),
    # The trim and drag commits send both bounds: one sent at its stored value
    # counts as unnamed, so it is neither clamped nor overlap-checked.
    ("left trim of a straddling item keeps its end", "item-3",
     {"start_frame": 85, "end_frame": 150}, _straddling, "paint"),
    ("both bounds sent unchanged on squashed items", "item-2",
     {"start_frame": 99, "end_frame": 100}, _squashed, "paint"),
    ("sentinel end sent unchanged", "item-3", {"start_frame": 85, "end_frame": -1},
     None, "paint"),
    # A named end is still clamped to the scene (D8), so a past-end item's
    # right edge cannot be trimmed short of the end of the scene. Kept by the
    # maintainer 2026-10-02: the timeline refuses that trim before it starts
    # (`_refusePastEndReferenceTrim`, tests/test_scene_duration_client_js.py).
    ("right trim of a past-end item", "item-3", {"start_frame": 150, "end_frame": 160},
     _past_end, "refuse:invalid_range"),
    # A sentinel beside a row a shrink left past the end would overlap it once
    # the scene grew back, so it is refused; the explicit end is accepted.
    ("sentinel beside a row past the end", "item-2", {"end_frame": -1}, _past_end,
     "refuse:lane_collision"),
    ("scene end beside a row past the end", "item-2", {"end_frame": 100}, _past_end,
     "paint"),
    ("unknown field", "item-1", {"bogus": 1}, None, "refuse:invalid_project_mutation"),
    ("no fields", "item-1", {}, None, "refuse:invalid_project_mutation"),
    # A clear is painted: the row the route stores simply lacks the field.
    ("clear a stored retention", "item-1",
     {"members": [_members("a")[0], _members("b")[0]]}, None, "paint"),
    ("clear a stored audio retention", "item-4",
     {"members": [{"entity_id": "entity-1", "member_id": "member-s"},
                  {"entity_id": "entity-1", "member_id": "member-va"}]}, None, "paint"),
    ("authored role", "item-1",
     {"members": [{**PRESERVED_A, "role": "identity"}, _members("b")[0]]}, None,
     "decline"),
    ("authored retention", "item-1",
     {"members": [{**PRESERVED_A, "visual_intent": "partial"}, _members("b")[0]]}, None,
     "decline"),
    # A write that names neither members nor a lane keeps every stored member
    # and both unnamed bounds as stored (#66, D8), so none of these blocks it.
    ("stale entity id", "item-1", {"strength": 0.5}, _stale_entity, "paint"),
    ("stored start past the scene", "item-3", {"strength": 0.5},
     _stored_start_past_scene, "paint"),
    ("missing lane recipe", "item-4", {"strength": 0.2}, _missing_recipe, "paint"),
    ("asset no longer in the project", "item-1", {"strength": 0.5}, _trashed_asset,
     "paint"),
    ("population narrowed", "item-1", {"strength": 0.5}, _narrowed_population, "paint"),
    ("untrimmed stored role", "item-1", {"strength": 0.5}, _untrimmed_role, "paint"),
    ("item on a lane no longer counted", "item-4", {"strength": 0.2}, _orphan_lane,
     "decline"),
    ("override as a boolean", "item-1", {"prompt_override": True}, None, "decline"),
    ("lane move", "item-1", {"lane_index": 1}, None, "decline"),
    ("strength as a string", "item-1", {"strength": "0.5"}, None, "decline"),
    # An append (`_appendReferenceMembersWithinGesture`): the stored priors as
    # they are, the dragged members after them.
    ("append", "item-1", {"members": [PRESERVED_A, *_members("b", "c")]}, None, "paint"),
    ("append after the recipe stopped exposing retention", "item-1",
     {"members": [PRESERVED_A, *_members("b", "c")]}, _retention_withdrawn, "paint"),
    ("append a drag with a stale entity id", "item-2",
     {"members": [*_members("c"), {"entity_id": "entity-2", "member_id": "member-a"}]},
     None, "paint"),
    ("append a member carrying an unknown key", "item-2",
     {"members": [*_members("c"), {**_members("a")[0], "future_field": "x"}]}, None, "paint"),
    ("append over a trashed prior", "item-1",
     {"members": [PRESERVED_A, *_members("b", "c")]}, _trashed_asset, "decline"),
    ("append over an untrimmed stored role", "item-1",
     {"members": [{**PRESERVED_A, "role": " identity "}, *_members("b", "c")]},
     _untrimmed_role, "decline"),
    ("append a member the lane cannot take", "item-1",
     {"members": [PRESERVED_A, *_members("b", "s")]}, None, "decline"),
    ("append a member already staged", "item-1",
     {"members": [PRESERVED_A, *_members("b", "b")]}, None, "refuse:invalid_reference_item"),
]


def _python_update(item_id, fields, fixture):
    project, scene = _update_fixture()
    if fixture:
        fixture(project, scene)
    item = _item(scene, item_id)
    current = item.to_dict()
    try:
        routes._apply_update_reference_item(project, scene, {
            "reference_item_id": item_id,
            "fields": dict(fields),
            "expected": {key: current.get(key) for key in fields},
        })
    except routes.ProjectMutationRequestError as exc:
        return {"refused": exc.code}
    return {"row": _item(scene, item_id).to_dict()}


def _update_context(item_id, fixture):
    """Everything the mirror is handed, read from the same fixture."""
    project, scene = _update_fixture()
    if fixture:
        fixture(project, scene)
    item = _item(scene, item_id)
    owners, assets = {}, {}
    for reference in project.references:
        for member in reference.members:
            owners[member.member_id] = reference.reference_id
            asset = project.get_asset(member.asset_id)
            assets[member.member_id] = None if asset is None else {
                "asset_type": asset.asset_type, "has_audio": bool(asset.has_audio)}
    recipes = scene.reference_lane_recipes
    return {
        "item": item.to_dict(),
        "laneItems": [row.to_dict() for row in scene.reference_items],
        "durationFrames": scene.duration_frames,
        "laneRecipe": (recipes[item.lane_index].to_dict()
                       if item.lane_index < len(recipes) else None),
        "laneCount": scene.reference_lane_count,
        "owners": owners,
        "assets": assets,
    }


def _javascript_updates(cases):
    payload = [{"context": _update_context(item_id, fixture), "fields": fields}
               for _name, item_id, fields, fixture, _expected in cases]
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const cases = {json.dumps(payload)};
console.log(JSON.stringify(cases.map(({{ context, fields }}) =>
  mod.plannedReferenceItemUpdate(context.item, fields, {{
    durationFrames: context.durationFrames,
    laneItems: context.laneItems,
    laneRecipe: context.laneRecipe,
    laneCount: context.laneCount,
    entityIdFor: (id) => context.owners[id] || "",
    assetFor: (id) => context.assets[id] || null,
  }}))));
"""
    return _node(script)


def test_the_planned_update_agrees_with_the_route_in_both_directions():
    planned = _javascript_updates(UPDATE_CASES)
    for (name, item_id, fields, fixture, expected), mirror in zip(UPDATE_CASES, planned):
        route = _python_update(item_id, fields, fixture)
        if mirror["refusal"]:
            outcome = f"refuse:{mirror['refusal']}"
            # A local refusal sends nothing, so it must be the route's own.
            assert route == {"refused": mirror["refusal"]}, name
        elif mirror["paintable"]:
            outcome = "paint"
            # A paint is the stored row, field for field.
            assert route == {"row": mirror["painted"]}, name
        else:
            outcome = "decline"
            assert mirror["painted"] is None, name
        assert outcome == expected, name
        # The converse: whatever the route refuses, the mirror never paints --
        # it refuses the same way or declines.
        if "refused" in route:
            assert not mirror["paintable"], name


def test_every_update_decline_stands_in_for_a_route_outcome_the_mirror_cannot_see():
    """What each decline would have painted wrongly.

    A decline is not free -- the edit waits for the server before it shows -- so
    each one must be a case where painting the obvious row would disagree with
    the route, or where the route's answer rests on authority this client does
    not mirror.
    """
    routed = {name: _python_update(item_id, fields, fixture)
              for name, item_id, fields, fixture, expected in UPDATE_CASES
              if expected == "decline"}
    assert routed["item on a lane no longer counted"] == {"refused": "item_not_found"}
    # Python's `str(True)` is "True"; JavaScript's is "true".
    assert routed["override as a boolean"]["row"]["prompt_override"] == "True"
    # Validated against recipe and intent vocabulary, then stored.
    assert routed["authored retention"]["row"]["members"][0]["visual_intent"] == "partial"
    # Decided by prompt-profile authority the mirror does not hold.
    assert routed["authored role"] == {"refused": "unsupported_reference_role"}
    # Moving lanes re-derives the destination's recipe and checks both locks;
    # here the destination is an audio lane, whose recipe refuses the images.
    assert routed["lane move"] == {"refused": "reference_media_kind_mismatch"}
    assert routed["strength as a string"]["row"]["strength"] == 0.5
    # An append's priors are re-validated by the route, not only its additions.
    assert routed["append over a trashed prior"] == {"refused": "asset_not_found"}
    assert routed["append over an untrimmed stored role"] == {
        "refused": "unsupported_reference_role"}
    assert routed["append a member the lane cannot take"] == {
        "refused": "reference_media_kind_mismatch"}


def test_a_scalar_update_keeps_what_it_does_not_name():
    """#66 and D8: these rows declined, or were refused, before backlog step 1,
    because the route re-judged stored members and re-clamped both bounds on
    every update. Now the route keeps them as stored, and the paint (pinned
    equal to the route by the table) does too."""
    routed = {name: _python_update(item_id, fields, fixture)["row"]
              for name, item_id, fields, fixture, _expected in UPDATE_CASES
              if name in {"stale entity id", "stored start past the scene",
                          "asset no longer in the project", "untrimmed stored role",
                          "start on a straddling item keeps its end",
                          "scalar on squashed items"}}
    assert routed["stale entity id"]["members"][1]["entity_id"] == "entity-2"
    assert routed["stored start past the scene"]["start_frame"] == 150
    assert [member["member_id"] for member in
            routed["asset no longer in the project"]["members"]] == ["member-a", "member-b"]
    assert routed["untrimmed stored role"]["members"][0]["role"] == " identity "
    assert (routed["start on a straddling item keeps its end"]["start_frame"],
            routed["start on a straddling item keeps its end"]["end_frame"]) == (85, 150)
    assert (routed["scalar on squashed items"]["start_frame"],
            routed["scalar on squashed items"]["strength"]) == (99, 0.5)


def test_clearing_a_stored_member_field_is_saved():
    """Through the batch and a save/load, as a project on disk goes -- the table
    above compares `to_dict()` of the in-memory row, which never meets the
    overlay, so it cannot see this. The save overlay never restores a modeled
    member key a write cleared (`test_overlay_modeled_keys.py`)."""
    project, _scene = _update_fixture()
    project = TimelineProject.from_dict(json.loads(json.dumps(project.to_dict())))
    scene = project.scenes[0]
    stored = _item(scene, "item-1").to_dict()["members"]
    cleared = [{key: value for key, value in stored[0].items() if key != "visual_intent"},
               stored[1]]
    routes._apply_scene_mutation_batch(project, scene.scene_id, [{
        "type": "update_reference_item", "reference_item_id": "item-1",
        "fields": {"members": cleared}, "expected": {"members": stored}}])
    saved = TimelineProject.from_dict(json.loads(json.dumps(project.to_dict())))
    assert "visual_intent" not in _item(saved.scenes[0], "item-1").members[0]


def test_the_stored_member_field_set_is_the_routes():
    """`STORED_MEMBER_FIELDS` is the optional half of `STAGED_MEMBER_FIELDS`."""
    source = (ROOT / "web" / "js" / "scene_reference_geometry.js").read_text(encoding="utf-8")
    match = re.search(r"const STORED_MEMBER_FIELDS = Object\.freeze\(\s*\[([^\]]*)\]", source)
    assert match, "STORED_MEMBER_FIELDS literal not found"
    mirrored = re.findall(r'"([a-z_]+)"', match.group(1))
    assert ["entity_id", "member_id", *mirrored] == list(STAGED_MEMBER_FIELDS)


def test_the_updatable_field_set_is_the_routes():
    """A field the route gains and the mirror lacks would be refused locally, and lost."""
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
console.log(JSON.stringify([...mod.REFERENCE_ITEM_FIELDS]));
"""
    assert set(_node(script)) == set(routes._REFERENCE_ITEM_FIELDS)


def test_an_undefined_field_is_judged_as_the_request_carries_it():
    """`JSON.stringify` drops an `undefined` value, so the route never sees the key."""
    context = _update_context("item-1", None)
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const context = {json.dumps(context)};
const plan = (fields) => mod.plannedReferenceItemUpdate(context.item, fields, {{
  durationFrames: context.durationFrames, laneItems: context.laneItems,
  laneRecipe: context.laneRecipe, laneCount: context.laneCount,
  entityIdFor: (id) => context.owners[id] || "",
  assetFor: (id) => context.assets[id] || null }});
console.log(JSON.stringify([plan({{ strength: 0.5, bogus: undefined }}),
  plan({{ strength: 0.5 }}), plan({{ strength: undefined }})]));
"""
    with_undefined, plain, only_undefined = _node(script)
    assert with_undefined == plain and plain["paintable"]
    assert only_undefined["refusal"] == "invalid_project_mutation"


def test_a_painted_scalar_update_stores_the_routes_member_records():
    """A scalar write keeps the stored member records; the paint does too."""
    [planned] = _javascript_updates([UPDATE_CASES[0]])
    route = _python_update("item-1", {"strength": 0.5}, None)
    assert planned["painted"]["members"] == route["row"]["members"] == [
        PRESERVED_A, _members("b")[0]]


def test_the_planned_update_does_not_mind_member_key_order():
    """Live regression: a stored member record keeps its own key order through
    the unknown-field overlay, which can differ from the order the route builds
    records in. Python's `==` ignores the order; a planner comparing by plain
    `JSON.stringify` declined every edit of an item holding a stored retention."""
    context = _update_context("item-1", None)
    for member in context["item"]["members"]:
        member_sorted = dict(sorted(member.items(), reverse=True))
        member.clear()
        member.update(member_sorted)
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const context = {json.dumps(context)};
console.log(JSON.stringify(mod.plannedReferenceItemUpdate(context.item, {{ strength: 0.5 }}, {{
  durationFrames: context.durationFrames, laneItems: context.laneItems,
  laneRecipe: context.laneRecipe, laneCount: context.laneCount,
  entityIdFor: (id) => context.owners[id] || "",
  assetFor: (id) => context.assets[id] || null }})));
"""
    planned = _node(script)
    assert list(context["item"]["members"][0]) == ["visual_intent", "member_id", "entity_id"]
    assert planned["paintable"] is True
    assert planned["painted"] == _python_update("item-1", {"strength": 0.5}, None)["row"]


# -- a Library delete's staged-item cascade ---------------------------------------
#
# `plannedReferenceMemberCascade` must name the rows `_reconcile_staged_reference_members`
# removes and thins, and leave each thinned row the members the route keeps, in
# order and record for record -- the editor paints that result into the scenes
# it holds and checks it against the route's per-scene report. Compared in both
# directions: a row the mirror keeps and the route deletes is a phantom bar, a
# row the mirror deletes and the route keeps is a bar that vanishes wrongly.

def _cascade_member(key, **extra):
    return {"entity_id": "entity-1", "member_id": f"member-{key}", **extra}


CASCADE_SCENES = [
    {"scene_id": "scene-1", "rows": [
        ("solo", [_cascade_member("a")]),
        ("pair", [_cascade_member("a", role="identity"), _cascade_member("b")]),
        ("other", [_cascade_member("c")]),
        ("twice", [_cascade_member("a"), _cascade_member("b"), _cascade_member("a")]),
        ("both", [_cascade_member("a"), _cascade_member("d")]),
    ]},
    {"scene_id": "scene-2", "rows": [
        ("far-solo", [_cascade_member("d")]),
        ("far-pair", [_cascade_member("b"), _cascade_member("d", visual_intent="preserve")]),
    ]},
    {"scene_id": "scene-3", "rows": [("untouched", [_cascade_member("c")])]},
]

# (removed member ids) -- one member, a Reference's several, none staged, and a
# numeric id the route's `str()` turns into a string that matches nothing.
CASCADE_CASES = [
    ["member-a"],
    ["member-d"],
    ["member-a", "member-d"],
    ["member-z"],
    [7],
]


def _cascade_scene_dicts():
    return [{"scene_id": spec["scene_id"], "duration_frames": 100, "reference_lane_count": 1,
             "reference_items": [{"reference_item_id": item_id, "lane_index": 0,
                                  "start_frame": index * 10, "end_frame": index * 10 + 5,
                                  "members": members}
                                 for index, (item_id, members) in enumerate(spec["rows"])]}
            for spec in CASCADE_SCENES]


def _javascript_cascade(cases):
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const scenes = {json.dumps(_cascade_scene_dicts())};
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map((removed) => Object.fromEntries(scenes.map((scene) => {{
  const plan = mod.plannedReferenceMemberCascade(scene, removed);
  return [scene.scene_id, {{ removed: plan.removed,
    thinned: plan.thinned.map((entry) => [entry.itemId, entry.members]) }}];
}})))));
"""
    return _node(script)


def _python_cascade(cases):
    out = []
    for removed in cases:
        project = TimelineProject(project_id="p", scenes=[
            Scene.from_dict(raw) for raw in _cascade_scene_dicts()])
        report = routes._reconcile_staged_reference_members(project, set(removed))
        per_scene = {}
        for scene in project.scenes:
            entry = report["scenes"].get(scene.scene_id, {
                "removed_reference_item_ids": [], "thinned_reference_item_ids": []})
            rows = {item.reference_item_id: item.members for item in scene.reference_items}
            per_scene[scene.scene_id] = {
                "removed": entry["removed_reference_item_ids"],
                "thinned": [[item_id, rows[item_id]]
                            for item_id in entry["thinned_reference_item_ids"]]}
        out.append(per_scene)
    return out


def test_the_delete_cascade_agrees_with_the_route_in_both_directions():
    javascript = _javascript_cascade(CASCADE_CASES)
    python = _python_cascade(CASCADE_CASES)
    assert javascript == python
    first = python[0]
    assert first["scene-1"] == {
        "removed": ["solo"],
        "thinned": [["pair", [_cascade_member("b")]],
                    ["twice", [_cascade_member("b")]],
                    ["both", [_cascade_member("d")]]]}, "the table pins the decision"
    assert python[2]["scene-1"]["removed"] == ["solo", "both"]
    assert python[2]["scene-2"] == {"removed": ["far-solo"],
                                    "thinned": [["far-pair", [_cascade_member("b")]]]}
    assert all(scene == {"removed": [], "thinned": []}
               for case in python[3:] for scene in case.values())


def test_a_thinned_row_is_painted_with_a_new_members_array():
    """A chain tells its own paint from another writer's by `row.members`
    identity, so the cascade must hand back a replacement, never the stored
    array edited in place."""
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const row = {{ reference_item_id: 'pair', members: [{{ member_id: 'member-a' }}, {{ member_id: 'member-b' }}] }};
const before = row.members;
const after = mod.referenceRowAfterMemberRemoval(row, ['member-a']);
console.log(JSON.stringify({{ fresh: after.members !== before, untouched: before.length,
  kept: after.members.map((m) => m.member_id),
  none: mod.referenceRowAfterMemberRemoval(row, ['member-z']) }}));
"""
    assert _node(script) == {"fresh": True, "untouched": 2, "kept": ["member-b"], "none": None}
