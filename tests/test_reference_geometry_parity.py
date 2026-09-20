"""Intentional Python/JavaScript parity for an optimistic Reference staging paint.

``web/js/scene_reference_geometry.js`` mirrors two things and only two:

* ``_reference_item_bounds``, which clamps a start into the scene, preserves the
  ``-1`` "runs to scene end" sentinel, and refuses a range that inverts; and
* the RECORD ``_canonical_reference_member_refs`` builds -- ``entity_id`` derived
  from the reference that owns the member, and ``member_id``. A member carrying
  any other stored field is refused rather than painted, for reasons the module
  states and ``test_a_member_carrying_a_stored_role_field_makes_the_list_unpaintable``
  pins; and
* the ROW ``_apply_create_reference_item`` appends, field for field, including
  the defaults a staging gesture never sends.

**Why the member half is mirrored at all** is the part a later reader is most
likely to try to undo, so it is stated here as well as beside the code. Phase C
§3 decided the Reference local apply must be geometry-only, on the reasoning
that painting ``item.members`` would make the next append send an ``expected``
the server never stored. Probed against the route, that is wrong in both halves:
the canonical record for a Library drop is byte-identical to what ``dragPayload``
sends, and it is NOT painting that loses a drop --
``_appendReferenceMembersWithinGesture`` reads ``priorMembers`` before its await,
so a second drop inside one in-flight window guards against state the route has
already left. ``test_two_appends_in_one_window`` below holds the ROUTE half of
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

**One refusal the append paints through, deliberately.** ``_apply_update_reference_item``
re-canonicalizes the whole list including priors, so a lane recipe whose
``physical_population`` narrowed after a member was staged refuses that prior
member. The caller re-resolves its priors -- which catches an unresolvable
member or a trashed asset -- but does not re-derive recipe rules, so that one
arrives as a refusal from the response.
"""

import json
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
    result = subprocess.run([node, "--input-type=module", "-e", script],
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
            start_frame = max(0, int(start))
            if duration > 0:
                start_frame = min(start_frame, duration - 1)
            out.append({"startFrame": start_frame, "endFrame": int(end),
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

    The gesture reads `priorMembers` before its await, so the second drop's
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

def _javascript_stored(cases):
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const cases = {json.dumps(cases)};
const owns = new Set(["member-a", "member-b", "member-c"]);
console.log(JSON.stringify(cases.map((members) =>
  mod.canonicalStoredMemberRefs(members, (id) => owns.has(id) ? "entity-1" : ""))));
"""
    return _node(script)


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

    [painted] = _javascript_stored([[stored, added]])
    assert painted == under_narrowed


def test_the_stored_form_still_refuses_what_it_cannot_reproduce():
    """It is not a way around the fail-closed rule -- only a wider allow-list."""
    assert _javascript_stored([[{"entity_id": "entity-1", "member_id": "member-a",
                                 "future_field": "x"}]]) == [None]
    assert _javascript_stored([[_drag("member-a"), _drag("member-a")]]) == [None]
    assert _javascript_stored([[_drag("member-gone")]]) == [None]
