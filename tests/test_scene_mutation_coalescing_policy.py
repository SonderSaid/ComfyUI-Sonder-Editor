"""What a coalesced scene mutation may collapse, and what it must keep.

The queue coalesces by REPLACING the pending intent and settles every collapsed
waiter from the survivor's single result, so a losing gesture is told it
succeeded whether or not its intent survived.
`tests/test_scene_mutation_registration.py` makes that a decision rather than a
default; `web/js/scene_mutation_coalescing.js` is what a gesture passes once it
has decided, and this module is what keeps that table honest.

What these tests protect, in the order the failures matter:

1. **Nothing is silently dropped.** The merge concatenates by default, so the
   table is an allow-list of collapses and an unclassified type is preserved.
   `test_an_unclassified_operation_is_kept_rather_than_folded` is the one that
   proves the default is the safe direction rather than the convenient one.
2. **A collapse is equivalent to applying both.** The claim the whole module
   rests on is that the server cannot tell a collapsed pair from the two
   operations applied in turn. `test_collapsing_is_equivalent_to_applying_both`
   proves it against the real dispatcher rather than against a description of
   it -- and `test_the_equivalence_probe_can_fail` proves that test can fail,
   because an equivalence assertion that always passes is the false-confidence
   shape this suite exists to prevent.
3. **The oldest guard survives.** `expected` states what an author saw, and the
   newer author saw state the older gesture painted optimistically and the
   server has not reached. A collapse that kept the newer guard would turn a
   valid burst into a 409.
4. **Coverage.** Every dispatcher operation type carries a traced decision.

These import `test_scene_mutation_registration` rather than re-scanning, for the
reason `mutation-surface-correctness.md` §4 gives: its predicate was wrong twice
before it was right, and a second copy would be a third chance.
"""

import copy
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.routes as routes
from server.timeline_state import (
    AudioTrack, ClipReference, GuideFrame, LaneConfig, ReferenceItem,
    ReferenceLaneRecipe, Scene, TimelineProject)

import test_scene_mutation_registration as registration
from test_scene_mutation_registration import (
    SUBKEYED_PAYLOAD_VALUES,
    _code_mask,
    _dispatcher_op_types,
    _match_delimiter,
    _routes_identifiers,
)

ROOT = Path(__file__).resolve().parents[1]
COALESCING_JS = ROOT / "web/js/scene_mutation_coalescing.js"

_ENTRY_RE = re.compile(r"\n    (?P<name>[a-z_]+): (?P<kind>collapsible|preserved)\(")
_CITATION_RE = re.compile(r"`([A-Za-z_][\w.]*)`")


# ---------------------------------------------------------------------------
# Reading the shipped table out of its own source.
#
# Deliberately NOT by running node: the coverage ratchet must fail on a machine
# with no node, and a skipped ratchet is the same as no ratchet. The behaviour
# tests below DO run node, because behaviour is what the browser executes.
# ---------------------------------------------------------------------------

def _table_source() -> str:
    source = COALESCING_JS.read_text(encoding="utf-8")
    mask = _code_mask(source)
    anchor = source.index("export const SCENE_MUTATION_COALESCING")
    brace = source.index("{", source.index("freeze(", anchor))
    end = _match_delimiter(source, brace, "{", "}", mask)
    assert end > 0, "the coalescing table literal is unterminated"
    return source[brace:end + 1]


def _entries() -> dict[str, dict]:
    """`{op_type: {collapsible, body}}` read from the module's own source."""
    table = _table_source()
    mask = _code_mask(table)
    entries = {}
    for match in _ENTRY_RE.finditer(table):
        open_paren = table.index("(", match.start("kind"))
        end = _match_delimiter(table, open_paren, "(", ")", mask)
        assert end > 0, f"entry {match.group('name')} is unterminated"
        entries[match.group("name")] = {
            "collapsible": match.group("kind") == "collapsible",
            "body": table[open_paren:end + 1],
        }
    return entries


def _run_node(script: str) -> str:
    """Run an ES module under node, via a temp file rather than `-e`.

    The differential below batches hundreds of operation pairs into one script,
    and Windows refuses a command line that long ("The filename or extension is
    too long"). Every import inside the script is a file URI, so the module
    resolves the same from a temp directory as it would from the repo.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as directory:
        entry = Path(directory) / "probe.mjs"
        entry.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, str(entry)],
            cwd=ROOT, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def _coalesced_many(pairs: list) -> list:
    """Every `(older, newer)` merged, in ONE node process.

    The differential below drives hundreds of pairs, and a process per pair cost
    about a minute of suite time. Batching keeps the merge itself honest -- it is
    still the shipped module doing the work -- while making the test affordable
    enough to keep.
    """
    url = COALESCING_JS.as_uri()
    out = _run_node(f"""
        import {{ coalesceSceneMutationOperations }} from {url!r};
        for (const [older, newer] of {json.dumps(pairs)}) {{
            console.log(JSON.stringify(
                coalesceSceneMutationOperations(older, newer)));
        }}
    """)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _coalesced(older: list, newer: list) -> list:
    """`coalesceSceneMutationOperations` as the browser would run it."""
    return _coalesced_many([[older, newer]])[0]


def _evidence_for(operations: list) -> list:
    url = COALESCING_JS.as_uri()
    out = _run_node(f"""
        import {{ sceneMutationCoalescingEvidence }} from {url!r};
        for (const one of {json.dumps(operations)}) {{
            console.log(JSON.stringify(sceneMutationCoalescingEvidence(one)));
        }}
    """)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def test_every_dispatcher_operation_carries_a_decision():
    """An unclassified type is preserved at runtime, so this is about noticing.

    Preservation is the safe direction -- the operation still reaches the server
    -- but a type nobody classified is a type nobody thought about, and the next
    burst on it stays one operation per click forever.
    """
    classified = set(_entries())
    dispatched = set(_dispatcher_op_types())
    assert not dispatched - classified, (
        "scene_mutation_coalescing.js carries no decision for: "
        f"{sorted(dispatched - classified)}. Add `collapsible(...)` with a row "
        "key, or `preserved(...)` with the traced reason a collapse would lose "
        "something. Do not leave it to the unclassified default.")
    assert not classified - dispatched, (
        "scene_mutation_coalescing.js classifies operations the dispatcher no "
        f"longer accepts: {sorted(classified - dispatched)}")


def test_every_decision_cites_code_that_exists():
    """A reason is a claim about code and has to be traced like one.

    Phase A's audit found four exemption reasons that were inventions. The
    cheapest repair for the coverage ratchet above is a new entry, and without
    this the cheapest entry is one that asserts nothing.
    """
    routes_names = _routes_identifiers()
    helper = re.compile(r"^_[a-z][a-z0-9_]*$")
    uncited = []
    for name, entry in _entries().items():
        cited = [value for value in _CITATION_RE.findall(entry["body"])
                 if helper.match(value)]
        missing = [value for value in cited if value not in routes_names]
        assert not missing, (
            f"{name} cites server helpers that do not exist in routes.py: {missing}")
        if not cited:
            uncited.append(name)
    # A one-line "creates a row" needs no citation -- the claim is about the
    # operation's own shape, not about a helper. Pin the set so a collapsible
    # entry can never join it.
    for name in uncited:
        assert not _entries()[name]["collapsible"], (
            f"{name} claims a collapse is lossless and cites no server code. "
            "Name the helper that writes the row, so the claim can be re-traced.")


def test_the_subkeyed_field_mirror_matches_its_python_authority():
    """An intentional mirror, so it owes a parity test.

    `SUBKEYED_PAYLOAD_VALUES` in the registration tripwire carries the traced
    reason for each key and is the authority. The JS list is what stops a
    collapse from unioning a value the server applies key by key, and a key
    present in one and not the other is a hole in whichever lacks it.
    """
    source = COALESCING_JS.read_text(encoding="utf-8")
    anchor = source.index("export const SUBKEYED_FIELDS")
    body = source[anchor:source.index("]", anchor)]
    mirrored = set(re.findall(r"\"([a-z_]+)\"", body))
    assert mirrored == set(SUBKEYED_PAYLOAD_VALUES), (
        "scene_mutation_coalescing.js `SUBKEYED_FIELDS` and the registration "
        "tripwire's `SUBKEYED_PAYLOAD_VALUES` have drifted: "
        f"only in JS {sorted(mirrored - set(SUBKEYED_PAYLOAD_VALUES))}, "
        f"only in Python {sorted(set(SUBKEYED_PAYLOAD_VALUES) - mirrored)}")


# ---------------------------------------------------------------------------
# Behaviour, in the module the browser loads
# ---------------------------------------------------------------------------

def test_nothing_is_dropped_when_nothing_may_collapse():
    """The base case, and the reason the default is concatenation.

    Two creates are two creates. Replacement -- which is what the queue does
    with no merge at all -- would keep one and tell both authors it worked.
    """
    merged = _coalesced(
        [{"type": "create_guide", "fields": {"frame_index": 1}}],
        [{"type": "create_guide", "fields": {"frame_index": 2}}])
    assert [op["fields"]["frame_index"] for op in merged] == [1, 2]


def test_an_unclassified_operation_is_kept_rather_than_folded():
    """Fail closed. An unknown type has no traced collapse, so it gets none."""
    merged = _coalesced(
        [{"type": "not_a_real_operation", "fields": {"a": 1}}],
        [{"type": "not_a_real_operation", "fields": {"a": 2}}])
    assert len(merged) == 2
    assert _evidence_for([{"type": "not_a_real_operation"}])[0]["collapsible"] is False


def test_a_same_row_update_collapses_with_the_oldest_guard():
    """Rule 2: fields newest-wins, `expected` OLDEST-wins.

    The older author read `muted: true` off the stored record. The newer author
    read `muted: false` off the state the older gesture painted locally, which
    the server has not reached. Keeping the newer guard would compare a claim
    the server must refuse.
    """
    merged = _coalesced(
        [{"type": "update_reference_item", "reference_item_id": "r",
          "expected": {"muted": True}, "fields": {"muted": False}}],
        [{"type": "update_reference_item", "reference_item_id": "r",
          "expected": {"muted": False}, "fields": {"muted": True}}])
    assert len(merged) == 1
    assert merged[0]["expected"] == {"muted": True}
    assert merged[0]["fields"] == {"muted": True}


def test_a_field_the_newer_operation_does_not_name_survives_the_collapse():
    """The losing intent has to survive, or the merge proves nothing.

    A merge that only shows the newer value landed is satisfied by wholesale
    replacement, which is the behaviour this module exists to replace.
    """
    merged = _coalesced(
        [{"type": "update_clip", "clip_id": "c", "fields": {"muted": True, "strength": 2}}],
        [{"type": "update_clip", "clip_id": "c", "fields": {"muted": False}}])
    assert len(merged) == 1
    assert merged[0]["fields"] == {"muted": False, "strength": 2}


def test_guards_that_constrain_different_keys_refuse_to_collapse():
    """Unequal `expected` key sets would let a newer-only claim survive.

    Oldest-wins is only equivalent to keeping both when both bags constrain the
    same keys. Refusing costs one extra operation in a payload; getting it wrong
    costs a 409 on a burst that was valid.
    """
    merged = _coalesced(
        [{"type": "update_clip", "clip_id": "c", "fields": {"muted": True}}],
        [{"type": "update_clip", "clip_id": "c",
          "expected": {"timeline_start_frame": 10}, "fields": {"muted": False}}])
    assert len(merged) == 2


def test_a_subkeyed_field_refuses_to_collapse():
    """`prompt_edit` is applied one document and one attachment at a time.

    A field union keeps the newest whole value, and `_merge_prompt_edit_fields`
    then writes only the records that value names -- so the older gesture's
    document edit is lost while its author is told it succeeded.
    """
    merged = _coalesced(
        [{"type": "update_scene_fields", "fields": {"prompt_edit": {"documents": {"a": {}}}}}],
        [{"type": "update_scene_fields", "fields": {"prompt_edit": {"documents": {"b": {}}}}}])
    assert len(merged) == 2
    merged_plain = _coalesced(
        [{"type": "update_scene_fields", "fields": {"name": "one"}}],
        [{"type": "update_scene_fields", "fields": {"name": "two"}}])
    assert len(merged_plain) == 1, (
        "the plain case must still collapse, or the sub-keyed refusal above is "
        "passing because nothing collapses at all")


def test_a_different_variant_of_the_same_row_refuses_to_collapse():
    """`apply_linked` changes what the operation means, not just its scope.

    `_apply_linked_bounds_update` moves a whole closure by the anchor's delta;
    the unlinked branch writes one row. Folding one into the other would move
    partners the author never addressed.
    """
    merged = _coalesced(
        [{"type": "update_clip", "clip_id": "c", "apply_linked": True,
          "fields": {"timeline_start_frame": 10}}],
        [{"type": "update_clip", "clip_id": "c",
          "fields": {"timeline_start_frame": 20}}])
    assert len(merged) == 2


def test_a_lane_config_collapses_by_durable_lane_and_not_by_index():
    """Distinct lanes at one index across a reorder must not erase each other.

    The shipped merge in `_saveLaneConfigWithinGesture` already keys this way;
    the table reproduces it so every lane-config gesture inherits it.
    """
    at_index = lambda lane_id, name: {
        "type": "update_lane_config", "lane_type": "video", "lane_index": 0,
        **({"expected": {"lane_id": lane_id}} if lane_id else {}),
        "fields": {"name": name}}
    same = _coalesced([at_index("a", "A")], [at_index("a", "A latest")])
    assert len(same) == 1 and same[0]["fields"]["name"] == "A latest"
    moved = _coalesced([at_index("a", "A")], [at_index("b", "B")])
    assert len(moved) == 2
    bootstrap = _coalesced([at_index("", "one")], [at_index("", "two")])
    assert len(bootstrap) == 1, "a lane with no durable id still collapses by index"


def test_a_collapse_folds_in_place_rather_than_appending():
    """Order is the batch's semantics; a fold must not reorder it.

    In place when nothing non-commuting is jumped -- here a second row's field
    assignment, which commutes -- and appended when something is. The second
    half is the interesting one: folding past `create_guide` would move the clip
    write in front of a row creation it originally followed.
    """
    merged = _coalesced(
        [{"type": "update_clip", "clip_id": "c", "fields": {"muted": True}},
         {"type": "update_audio_track", "track_id": "t", "fields": {"muted": True}}],
        [{"type": "update_clip", "clip_id": "c", "fields": {"muted": False}}])
    assert [op["type"] for op in merged] == ["update_clip", "update_audio_track"]
    assert merged[0]["fields"] == {"muted": False}

    past_a_create = _coalesced(
        [{"type": "update_clip", "clip_id": "c", "fields": {"muted": True}},
         {"type": "create_guide", "fields": {"frame_index": 5}}],
        [{"type": "update_clip", "clip_id": "c", "fields": {"muted": False}}])
    assert [op["type"] for op in past_a_create] == [
        "update_clip", "create_guide", "update_clip"], (
        "a fold must not jump a row creation")


# ---------------------------------------------------------------------------
# The equivalence the whole module rests on
# ---------------------------------------------------------------------------

def _scene_builder():
    """A factory handing out identical copies of one scene.

    Built once and deep-copied rather than rebuilt: `Scene` mints fresh
    `lane_id`s and prompt-document `node_id`s on construction, so two freshly
    built scenes differ in ways no operation caused. Comparing those would make
    the equivalence assertion fail for a reason that has nothing to do with
    coalescing -- and, worse, would make its negative twin below pass for one.
    """
    scene = Scene(scene_id="scene", duration_frames=240)
    scene.clips = [ClipReference(
        source_path="a.mp4", timeline_start_frame=0, timeline_end_frame=48,
        total_source_frames=48, track_index=0, clip_id="c1")]
    scene.audio_tracks = [AudioTrack(
        source_path="a.wav", timeline_start_frame=0, timeline_end_frame=48,
        total_source_frames=48, lane_index=0, track_id="t1")]
    scene.video_lane_configs = [LaneConfig()]
    scene.audio_lane_configs = [LaneConfig()]
    # Seeded rather than left to the server's lazy bootstrap, which mints a
    # fresh `lane_id` on first write. Two applications of the same operations
    # would then differ in an id no operation asked for.
    scene.reference_lane_configs = [LaneConfig()]
    scene.reference_lane_recipes = [ReferenceLaneRecipe(lane_id="ref-lane-0")]
    scene.guide_frames = [GuideFrame(frame_index=5, guide_id="g1", asset_id="")]
    scene.reference_items = [ReferenceItem(
        reference_item_id="ri1", lane_index=0, start_frame=10, end_frame=60)]
    project = TimelineProject(project_dir=".", project_id="proj", scenes=[scene])

    def build():
        copied = copy.deepcopy(project)
        return copied, copied.scenes[0]
    return build


def _apply(build, operations: list) -> dict:
    """The whole document the batch produces, not just its scene.

    `scene.to_dict()` alone would miss a divergence outside the scene --
    `_apply_update_reference_item` canonicalizes members against
    `project.references`, and `_apply_create_clip` registers assets -- so a fold
    that changed project state while leaving the scene identical would read as
    equivalent. `modified_at` is dropped because it is a timestamp, not an
    outcome.
    """
    project, scene = build()
    for operation in operations:
        routes._apply_scene_mutation_operation(project, scene, operation)
    document = project.to_dict()
    document.pop("modified_at", None)
    return {"scene": scene.to_dict(), "project": document}


EQUIVALENCE_CASES = [
    pytest.param(
        [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}],
        [{"type": "update_clip", "clip_id": "c1", "fields": {"opacity": 0.5}}],
        id="update_clip-different-fields"),
    pytest.param(
        [{"type": "update_clip", "clip_id": "c1",
          "fields": {"timeline_start_frame": 12}}],
        [{"type": "update_clip", "clip_id": "c1",
          "fields": {"timeline_start_frame": 30}}],
        id="update_clip-same-field-twice"),
    pytest.param(
        [{"type": "update_audio_track", "track_id": "t1", "fields": {"muted": True}}],
        [{"type": "update_audio_track", "track_id": "t1", "fields": {"volume": 0.4}}],
        id="update_audio_track"),
    pytest.param(
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "fields": {"name": "A", "color": "", "locked": False, "hidden": True}}],
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "fields": {"name": "A", "color": "", "locked": False, "hidden": False}}],
        id="update_lane_config"),
    pytest.param(
        [{"type": "update_scene_fields", "fields": {"name": "one"}}],
        [{"type": "update_scene_fields", "fields": {"width": 1280}}],
        id="update_scene_fields"),
]


@pytest.mark.parametrize("older,newer", EQUIVALENCE_CASES)
def test_collapsing_is_equivalent_to_applying_both(older, newer):
    """The claim: the server cannot tell a collapsed pair from two operations.

    Proven against the real dispatcher, on a scene built here, rather than
    against a description of what the dispatcher does. Every case must actually
    collapse -- a case that quietly stops collapsing would pass this test while
    proving nothing, which is why the length is asserted first.
    """
    build = _scene_builder()
    merged = _coalesced(older, newer)
    assert len(merged) == 1, (
        f"this case no longer collapses ({len(merged)} operations), so the "
        "equivalence it claims to prove is not being exercised")
    assert _apply(build, merged) == _apply(build, older + newer)


def test_the_equivalence_probe_can_fail():
    """An equivalence assertion that cannot fail proves nothing.

    Wholesale replacement is what the queue does with no merge, and it is not
    equivalent -- the older operation's field edit is gone. If this ever starts
    passing, `_apply` is comparing something that does not depend on the
    operations.
    """
    build = _scene_builder()
    older = [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}]
    newer = [{"type": "update_clip", "clip_id": "c1",
              "fields": {"timeline_start_frame": 24}}]
    assert _apply(build, newer) != _apply(build, older + newer)


# ---------------------------------------------------------------------------
# The property test that found the defect this table was first written with
# ---------------------------------------------------------------------------
# The hand-written cases above prove the cases someone thought of. This one
# enumerates the cross product of field subsets for every collapsible type and
# checks the SAME equivalence over all of them, which is how the original
# `update_clip` entry was caught: it collapsed a duration-preserving start
# shift into an absolute one.
#
# `_apply_update_clip` reads `timeline_start_frame` differently depending on
# whether `timeline_end_frame` is in the SAME fields dict -- alone it preserves
# duration, together it is absolute. A field union therefore changes what the
# newer operation meant, and the table now puts the bounds-field signature in
# the row key so two operations naming different bounds never collapse.

_CLIP_FIELD_SPACE = {
    "timeline_start_frame": (20, 40),
    "timeline_end_frame": (80, 100),
    "muted": (True, False),
    "opacity": (0.5, 1.0),
}
_REFERENCE_FIELD_SPACE = {
    "start_frame": (20, 40),
    "end_frame": (80, 100),
    "muted": (True, False),
}
_GUIDE_FIELD_SPACE = {
    "strength": (0.2, 0.9),
    "muted": (True, False),
    "fit_mode": ("cover", "contain"),
}
_LANE_CONFIG_FIELD_SPACE = {
    "name": ("A", "B"),
    "color": ("#111111", "#222222"),
    "locked": (False, False),
    "hidden": (True, False),
}
# Chosen to FIRE the side effects, not to avoid them. `duration_frames` 40 is
# below the seeded Reference item's end, so `_clamp_reference_items_to_scene`
# actually clamps; `fps` retimes and is applied after `duration_frames` whatever
# order the client authored; the lane counts shrink before they grow, which is
# the shape that destroys a named config. A space that fires nothing would let
# this test pass over a table that folds all of them.
_SCENE_FIELD_SPACE = {
    "name": ("one", "two"),
    "width": (640, 1280),
    "duration_frames": (40, 300),
    "fps": (24.0, 12.0),
    "video_lane_count": (1, 3),
}


def _field_subsets(space):
    names = list(space)
    for size in range(1, len(names) + 1):
        yield from itertools.combinations(names, size)


def _differential(builder, make_operation, space):
    """(collapsed pairs checked, pairs whose collapse changed the outcome)."""
    checked, divergent = 0, []
    cases = [((older_names, newer_names),
              [make_operation({name: space[name][0] for name in older_names})],
              [make_operation({name: space[name][1] for name in newer_names})])
             for older_names in _field_subsets(space)
             for newer_names in _field_subsets(space)]
    merges = _coalesced_many([[older, newer] for _, older, newer in cases])
    for ((older_names, newer_names), older, newer), merged in zip(cases, merges):
            if len(merged) != 1:
                continue
            checked += 1
            sequential = _outcome(builder, older + newer)
            collapsed = _outcome(builder, merged)
            if sequential != collapsed:
                divergent.append((older_names, newer_names,
                                  _describe(sequential), _describe(collapsed)))
    return checked, divergent


def _outcome(builder, operations):
    """The scene the batch produces, or the refusal it raises.

    Deliberately NOT a bare `except: continue`. "Both paths refused" and "the
    sequential pair was refused and the fold succeeded" are different facts, and
    the second is the worst outcome a fold can have — it converts a refusal the
    server would have raised into a silent write. Swallowing both made exactly
    that case invisible: a linked `update_guide` carrying `fields.frame_index`
    404s on the second operation and succeeds when folded.
    """
    try:
        return ("applied", _apply(builder, operations))
    except Exception as error:  # the server's own refusal is the outcome
        return ("refused", f"{type(error).__name__}: {error}")


def _describe(outcome):
    kind, value = outcome
    return value if kind == "refused" else "applied"


def _reference_operation(fields):
    # `_reference_item_expected` requires a snapshot covering every field named,
    # so the guard is built from the seeded item's own values.
    current = {"start_frame": 10, "end_frame": 60, "muted": False}
    return {"type": "update_reference_item", "reference_item_id": "ri1",
            "fields": fields,
            "expected": {name: current[name] for name in fields}}


DIFFERENTIAL_CASES = [
    pytest.param(lambda fields: {"type": "update_clip", "clip_id": "c1", "fields": fields},
                 _CLIP_FIELD_SPACE, id="update_clip"),
    pytest.param(lambda fields: {"type": "update_audio_track", "track_id": "t1",
                                 "fields": fields},
                 _CLIP_FIELD_SPACE, id="update_audio_track"),
    pytest.param(_reference_operation, _REFERENCE_FIELD_SPACE, id="update_reference_item"),
    pytest.param(lambda fields: {"type": "update_guide", "frame_index": 5,
                                 "expected": {"frame_index": 5, "asset_id": "",
                                              "guide_id": "g1"},
                                 "fields": fields},
                 _GUIDE_FIELD_SPACE, id="update_guide"),
    pytest.param(lambda fields: {"type": "update_lane_config", "lane_type": "video",
                                 "lane_index": 0, "fields": fields},
                 _LANE_CONFIG_FIELD_SPACE, id="update_lane_config"),
    pytest.param(lambda fields: {"type": "update_scene_fields", "fields": fields},
                 _SCENE_FIELD_SPACE, id="update_scene_fields"),
]


@pytest.mark.parametrize("make_operation,space", DIFFERENTIAL_CASES)
def test_no_field_combination_collapses_into_a_different_outcome(make_operation, space):
    """Every collapse the table allows, over the cross product of field subsets."""
    build = _scene_builder()
    checked, divergent = _differential(build, make_operation, space)
    assert checked > 0, (
        "no pair collapsed at all, so this case proves nothing about collapsing")
    assert not divergent, (
        "collapsing changed the outcome for these (older fields, newer fields, "
        f"sequential, collapsed) cases: {divergent}. Either the operation's "
        "meaning depends on which fields accompany it -- in which case the row "
        "key must separate them -- or the type is not collapsible at all. A "
        "case where the sequential pair was REFUSED and the fold applied is the "
        "worst of these: the fold silently wrote what the server would have "
        "rejected.")


def test_the_differential_would_catch_the_defect_it_was_written_for():
    """The original `update_clip` entry collapsed a duration-preserving shift.

    Proven directly rather than by trusting that the table is now right: an
    older operation setting `timeline_end_frame` and a newer one setting
    `timeline_start_frame` ALONE must not collapse, because the union would
    silence `_apply_update_clip`'s `if "timeline_end_frame" not in fields`
    branch and the clip would keep the older end instead of shifting to it.
    """
    build = _scene_builder()
    older = [{"type": "update_clip", "clip_id": "c1",
              "fields": {"timeline_end_frame": 100}}]
    newer = [{"type": "update_clip", "clip_id": "c1",
              "fields": {"timeline_start_frame": 20}}]
    merged = _coalesced(older, newer)
    assert len(merged) == 2, (
        "a duration-preserving start shift must not be folded into an "
        "operation that names an end frame")
    # And the outcome the refusal protects: folding them would lose 10 frames
    # of duration.
    folded = [{"type": "update_clip", "clip_id": "c1",
               "fields": {"timeline_start_frame": 20, "timeline_end_frame": 100}}]
    assert _apply(build, folded) != _apply(build, older + newer)


def test_a_linked_bounds_update_is_never_folded():
    """`_apply_linked_bounds_update` moves partners by a delta from STORED bounds.

    Composition does not hold in general: a pure move shifts every member of
    the closure, while a trim moves only the members aligned with the anchor,
    so folding one into the other changes which partners moved. A muted-only
    linked update is the exception -- it takes the early return that writes
    `muted` to every ref and never reaches the delta logic.
    """
    move = {"type": "update_clip", "clip_id": "c1", "apply_linked": True,
            "fields": {"timeline_start_frame": 25}}
    trim = {"type": "update_clip", "clip_id": "c1", "apply_linked": True,
            "fields": {"timeline_start_frame": 25, "timeline_end_frame": 120}}
    assert len(_coalesced([move], [trim])) == 2
    assert len(_coalesced([trim], [dict(trim, fields={"timeline_start_frame": 25,
                                                      "timeline_end_frame": 140})])) == 2
    muted = {"type": "update_clip", "clip_id": "c1", "apply_linked": True,
             "fields": {"muted": True}}
    assert len(_coalesced([muted], [dict(muted, fields={"muted": False})])) == 1, (
        "a muted-only linked update takes `_apply_linked_bounds_update`'s early "
        "return and is the one linked shape that folds")
    assert len(_coalesced([muted], [{"type": "update_clip", "clip_id": "c1",
                                     "fields": {"muted": False}}])) == 2, (
        "a linked mute and an unlinked mute address different sets of rows")


# ---------------------------------------------------------------------------
# The two batch-level rules concatenation does not satisfy
# ---------------------------------------------------------------------------

# The two operation shapes a concatenated batch cannot simply carry. Keyed
# `module:scope`, each with the traced reason the site is safe anyway.
#
# Seeded with one entry, and that entry is the correction of a claim this
# module's header made and the first version of this pin could not check: the
# pin read `site["operands"]`, which resolves by ENCLOSING SCOPE, so it saw
# only the operations a gesture builds inline. The lane-header gesture builds
# `update_lane_config` inline and reaches five more types through
# `_buildLinkedMuteOperations`, so it cleared a check that had never looked at
# what it sends.
#
# Expiry: an entry leaves when its site stops coalescing, stops reaching the
# operation, or acquires a key that cannot repeat.
BATCH_RULE_REVIEWED = {
    "editor_widget.js:_applyHeaderVisibilityBulkWithinGesture": (
        "reaches `update_prompt_section` through `_buildLinkedMuteOperations` "
        "-> `_muteOperationForItem`, and coalesces on a repeating key, so two "
        "of them CAN share a batch and meet `_apply_scene_mutations_sync`'s "
        "pre-batch validation loop. Safe because that operation's `expected` is "
        "identity-only -- `start_frame`, `end_frame`, `prompt_id` -- and this "
        "gesture only ever writes `muted`, which changes none of them, so a "
        "guard read after an earlier click's optimistic apply still describes "
        "pre-batch state. `test_the_prompt_mute_guard_stays_identity_only` is "
        "what stops that reason silently expiring."),
    "editor_widget.js:_toggleSelectedMuteWithinGesture": (
        "emits `update_prompt_section` inline for a selected prompt section, "
        "and coalesces on `scene:<id>:selected-mute`, so two toggles CAN put "
        "two of them in one batch. Safe for the same traced reason as the "
        "lane-header gesture: the guard is `start_frame` / `end_frame` / "
        "`prompt_id`, read from the section record, and this gesture writes only "
        "`muted` -- so both guards still describe pre-batch state when the "
        "pre-batch validation loop compares them. Unlike the lane-header "
        "gesture, two of these really can reach one batch: nothing here clears "
        "the seeds between clicks, the second toggle simply sends the opposite "
        "value."),
}


def _reachable_operation_types(site, module_source: str) -> set:
    """Every operation type this site can send, including via helpers."""
    sources = ((site["module"], module_source),)
    emitters, bodies = registration._operation_emitting_scopes(sources)[site["module"]]
    owners = {site["scope"]} | (bodies.get(site["scope"], set()) & emitters)
    seen, frontier = set(owners), list(owners)
    while frontier:
        scope = frontier.pop()
        for called in bodies.get(scope, set()) & emitters:
            if called not in seen:
                seen.add(called)
                frontier.append(called)
    return {literal["op_type"]
            for literal in registration._scan(module_source, site["module"])
            if literal["scope"] in seen}


def test_the_batch_rules_concatenation_cannot_satisfy_are_accounted_for():
    """Preservation is not equivalent to two requests for these two shapes.

    `_apply_scene_mutations_sync` refuses a batch holding more than one
    media-I/O create, and it validates every `update_prompt_section` guard
    BEFORE applying the first operation -- against pre-batch state, which is the
    opposite of the justification concatenation rests on. A gesture that
    coalesces on a repeating key while reaching one of those operations owes a
    traced reason; it is not automatically wrong, but it is never automatically
    right either.
    """
    reaching = {"create_clip", "create_audio_track", "update_prompt_section"}
    offenders = {}
    for module in registration.EMITTING_MODULES:
        module_source = (registration.JS_DIR / module).read_text(encoding="utf-8")
        for site in registration._enqueue_call_sites():
            if site["module"] != module:
                continue
            if site["scope"] in registration._FORWARDING_SCOPES:
                continue
            if not site["coalesces"]:
                continue
            if not registration._key_repeats(site["key_interpolations"]):
                continue
            hit = _reachable_operation_types(site, module_source) & reaching
            if hit:
                offenders[f"{module}:{site['scope']}"] = sorted(hit)
    unreviewed = {key: why for key, why in offenders.items()
                  if key not in BATCH_RULE_REVIEWED}
    assert not unreviewed, (
        "these gestures coalesce on a key that can repeat while reaching an "
        f"operation the batch rules make non-concatenable: {unreviewed}. Either "
        "keep the key unrepeatable, or record why a merged batch is safe "
        "anyway -- a merged batch is refused outright for two media-I/O "
        "creates, and compares a guard against a document it has not reached "
        "for `update_prompt_section`.")
    dead = sorted(set(BATCH_RULE_REVIEWED) - set(offenders))
    assert not dead, f"these entries no longer describe a live site: {dead}"


def test_the_prompt_mute_guard_stays_identity_only():
    """The reason the lane-header entry above is safe, pinned against drift.

    Its `expected` constrains `start_frame`, `end_frame` and `prompt_id`, none
    of which a mute changes -- so a guard read after an earlier click's
    optimistic apply still describes pre-batch state. If a field the gesture
    WRITES ever joins that bag, the entry's reason stops being true and the
    pre-batch validation loop starts refusing valid bursts.
    """
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    # BOTH emitters: the shared helper the lane-header gesture reaches, and the
    # copy `_toggleSelectedMuteWithinGesture` builds inline. Each has its own
    # entry in BATCH_RULE_REVIEWED and each entry rests on this shape.
    regions = {
        "_muteOperationForItem": (
            "_muteOperationForItem(item, muted, applyLinked = false)",
            "_buildLinkedMuteOperations(items, muted)"),
        "_toggleSelectedMuteWithinGesture": (
            "async _toggleSelectedMuteWithinGesture()",
            "async _createLinkGroupFromSelection("),
    }
    for scope, (begin_anchor, end_anchor) in regions.items():
        begin = source.index(begin_anchor)
        end = source.index(end_anchor, begin)
        prompt = source[source.index('type: "update_prompt_section"', begin):end]
        guard = prompt[prompt.index("expected:"):prompt.index("fields:")]
        assert sorted(re.findall(r"(\w+):", guard)) == [
            "end_frame", "expected", "prompt_id", "start_frame"], (
            f"{scope}'s prompt mute guard changed shape: {guard}")
        assert "muted" not in guard, (
            f"{scope}'s mute guard now constrains the field the gesture writes, "
            "so two coalesced mutes can no longer both validate against "
            "pre-batch state")


def test_a_second_media_io_create_really_is_refused():
    """The premise of the pin above, proven against the server, not assumed."""
    build = _scene_builder()
    drops = [{"type": "create_audio_track", "fields": {"asset_id": "a"}},
             {"type": "create_audio_track", "fields": {"asset_id": "b"}}]
    assert len(_coalesced(drops[:1], drops[1:])) == 2, (
        "media creates are preserved, so a coalesced pair would be sent as two")
    project, _ = build()
    # `_media_io_operation_count` only counts drops whose asset needs ffmpeg, so
    # the count is what is asserted rather than a full request round trip.
    assert routes._media_io_operation_count(project, drops) == 0, (
        "with no assets registered neither drop is media-I/O; this test pins "
        "the counter's existence and shape, and the pin above is what keeps the "
        "reachable case out")


# ---------------------------------------------------------------------------
# The two inline merges this module supersedes
# ---------------------------------------------------------------------------

_INLINE_MERGE_SOURCES = {
    "_saveLaneConfigWithinGesture": ("async _saveLaneConfigWithinGesture", "async _addLane"),
    "_updateItemPropertyWithinGesture": ("async _updateItemPropertyWithinGesture",
                                         "async _createLinkGroupFromSelection"),
}


def _inline_merge(name: str) -> str:
    """The `const merge = ...` a live gesture still defines for itself."""
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    begin_anchor, end_anchor = _INLINE_MERGE_SOURCES[name]
    begin = source.index(begin_anchor)
    end = source.index(end_anchor, begin)
    body = source[begin:end]
    at = body.index("const merge = ")
    mask = _code_mask(body)
    brace = body.index("{", body.index("=>", at))
    close = _match_delimiter(body, brace, "{", "}", mask[brace:] and mask)
    assert close > 0, f"{name}'s merge body is unterminated"
    # A helper the merge shares with the gesture's coalescing key travels with
    # it, so the merge is compared as it runs rather than without its row key.
    helpers = ""
    helper_at = body.find("const laneConfigKey = ")
    if helper_at >= 0:
        stop = next(index for index in range(helper_at, len(body))
                    if mask[index] and body[index] == ";")
        helpers = body[helper_at:stop + 1] + "\n"
    return helpers + body[at:close + 1] + ";"


@pytest.mark.parametrize("gesture,intents", [
    ("_saveLaneConfigWithinGesture", [
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "expected": {"lane_id": "a"},
          "fields": {"name": "A", "color": "", "locked": False, "hidden": False}}],
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "expected": {"lane_id": "a"},
          "fields": {"name": "A latest", "color": "", "locked": False, "hidden": True}}],
    ]),
    ("_saveLaneConfigWithinGesture", [
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "expected": {"lane_id": "a"}, "fields": {"name": "A"}}],
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "expected": {"lane_id": "b"}, "fields": {"name": "B"}}],
    ]),
    ("_updateItemPropertyWithinGesture", [
        [{"type": "update_reference_item", "reference_item_id": "r",
          "expected": {"muted": True}, "fields": {"muted": False}}],
        [{"type": "update_reference_item", "reference_item_id": "r",
          "expected": {"muted": False}, "fields": {"muted": True}}],
    ]),
])
def test_the_shared_merge_agrees_with_the_inline_merge_it_supersedes(gesture, intents):
    """An intentional mirror, so it owes a parity test (`agent_workflow.md`).

    Two gestures still define their own merge inline, and this module was written
    to reproduce both. Nothing compared them: the module has no importer yet, so
    drift would be silent in BOTH directions until stage 1 L2 wires it up.

    They are not textually equivalent and the test does not pretend otherwise —
    the inline lane merge replaces a matched operation wholesale while the shared
    one unions `fields` and keeps the OLDEST `expected`. They agree only because
    `_saveLaneConfigWithinGesture` re-sends every config field and `lane_id` is
    in the row key. That is a property of today's payloads, and this is where it
    is stated and checked.

    Expiry: delete when both call sites pass `coalesceSceneMutationIntents` and
    the inline copies are gone — umbrella Phase C stage 1 L2/L3.
    """
    older, newer = intents
    url = COALESCING_JS.as_uri()
    out = _run_node(f"""
        import {{ coalesceSceneMutationIntents }} from {url!r};
        {_inline_merge(gesture)}
        const older = {{ operations: {json.dumps(older)} }};
        const newer = {{ operations: {json.dumps(newer)} }};
        console.log(JSON.stringify(merge(structuredClone(older), structuredClone(newer))));
        console.log(JSON.stringify(coalesceSceneMutationIntents(
            structuredClone(older), structuredClone(newer))));
    """)
    inline, shared = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert inline["operations"] == shared["operations"], (
        f"{gesture}'s inline merge and the shared merge disagree.\n"
        f"  inline: {inline['operations']}\n  shared: {shared['operations']}")


# ---------------------------------------------------------------------------
# `SUBKEYED_FIELDS` as a claim about the server, not about another list
# ---------------------------------------------------------------------------

def test_each_subkeyed_field_is_applied_key_by_key_by_the_server():
    """The mirror test above compares two hand lists; this one asks the server.

    A key is in this set because its VALUE is applied key by key, which is the
    reason a field union is not enough for it. Proven by writing one inner key
    and asserting a sibling the write did not name survives — if it does not,
    the value was replaced outright and the entry is wrong. The converse guard
    is `name`, which must NOT survive that treatment.
    """
    build = _scene_builder()

    def write(fields, expected=None):
        """Seed two channels, then write one of them and return the scene.

        `expected` is built FROM the seeded scene rather than supplied literally:
        `_validate_global_prompt_expectations` compares it, and a hand-written
        bag would be testing the guard instead of the setter.
        """
        project, scene = build()
        scene.set_global_channels({"visual": "keep me", "sounds": "and me"})
        operation = {"type": "update_scene_fields", "fields": fields}
        if expected is not None:
            operation["expected"] = {
                name: copy.deepcopy(getattr(scene, name)) for name in expected}
        routes._apply_scene_mutation_operation(project, scene, operation)
        return scene

    # `_require_prompt_mutation_contract` demands compare state for a direct
    # prompt write, and `_validate_global_prompt_expectations` compares it --
    # which is itself why this field is sub-keyed rather than replaced.
    subkeyed = write({"global_channels": {"visual": "changed"}},
                     expected=["global_channels"])
    assert subkeyed.global_channels.get("sounds") == "and me", (
        "`global_channels` is listed as sub-keyed, but an omitted channel did "
        "not survive the write -- the entry is wrong, and folding two of these "
        "would be safe where the table says it is not")
    assert subkeyed.global_channels.get("visual") == "changed"

    # The document twin: `set_global_channel_documents` normalises over the
    # union of stored and incoming keys, so an omitted document is not merely
    # kept -- `prompt_context.normalize_channel_documents` rebuilds it from the
    # flat mirror.
    docs = write(
        {"global_channel_docs": {"visual": {"nodes": [
            {"type": "text", "node_id": "n1", "text": "changed"}]}}},
        expected=["global_channel_docs"])
    assert "sounds" in docs.global_channel_docs, (
        "`global_channel_docs` is listed as sub-keyed, but an omitted channel "
        "document did not survive the write")

    replaced = write({"name": "renamed"})
    assert replaced.name == "renamed", (
        "the control case: a plain field is replaced outright, which is what "
        "makes it foldable")

    # The remaining three entries -- `channels`, `channel_docs`, `prompt_edit` --
    # are section-scoped or delta-shaped and reach different setters. They are
    # covered by the traced reason each carries in `SUBKEYED_PAYLOAD_VALUES`
    # and by the parity test above; stated here so the gap is deliberate rather
    # than assumed closed by the two cases that are exercised.


def test_a_fold_never_jumps_an_operation_that_does_not_commute():
    """Folding moves an operation EARLIER; that has to be equivalent.

    Two field assignments on different rows commute, so a lane-header burst
    still folds past its mute operations. Anything this module preserves does
    not commute -- preserved is exactly the class whose effect is not a field
    assignment -- so a fold that would jump one is refused and the operation is
    appended instead, reproducing the sequential order exactly.

    Reachable rather than theoretical: `_convertClipRoleWithinGesture` emits
    `set_lane_count` then `update_clip` under one coalescing key.
    """
    role = lambda count, index, kind: [
        {"type": "set_lane_count", "lane_type": "video", "count": count},
        {"type": "update_clip", "clip_id": "c1",
         "fields": {"role": kind, "track_index": index}}]
    merged = _coalesced(role(2, 0, "motion_driver"), role(3, 2, "render"))
    assert [op["type"] for op in merged] == [
        "set_lane_count", "update_clip", "set_lane_count", "update_clip"], (
        "the second conversion's clip write was folded in front of the first "
        f"conversion's lane-count change: {[op['type'] for op in merged]}")

    # The collapsible-only case must still fold, or the guard has cost the
    # measured win rather than protecting it.
    header = lambda hidden, muted: [
        {"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
         "fields": {"hidden": hidden}},
        {"type": "update_reference_item", "reference_item_id": "r",
         "expected": {"muted": not muted}, "fields": {"muted": muted}}]
    folded = _coalesced(header(True, False), header(False, True))
    assert len(folded) == 2, (
        "a burst of pure field assignments must still fold past each other")
    assert folded[0]["fields"]["hidden"] is False
    assert folded[1]["expected"]["muted"] is True, "and keep the oldest guard"


def test_the_reorder_guard_is_what_keeps_the_clip_role_batch_equivalent():
    """Proven against the dispatcher, not against the operation list.

    The list assertion above says the order is preserved; this says the order
    is what the outcome depends on, so the assertion is protecting something.
    """
    build = _scene_builder()
    older = [{"type": "set_lane_count", "lane_type": "video", "count": 3},
             {"type": "update_clip", "clip_id": "c1", "fields": {"track_index": 2}}]
    newer = [{"type": "set_lane_count", "lane_type": "video", "count": 1},
             {"type": "update_clip", "clip_id": "c1", "fields": {"track_index": 0}}]
    merged = _coalesced(older, newer)
    assert _apply(build, merged) == _apply(build, older + newer)

    # And the shape the guard refuses really would have differed: folding by
    # hand puts the clip write before the lane-count shrink.
    reordered = [older[0], {"type": "update_clip", "clip_id": "c1",
                            "fields": {"track_index": 0}}, newer[0]]
    assert len(merged) == 4, "this case must not fold"
    assert [op["type"] for op in reordered] != [op["type"] for op in merged]


# ---------------------------------------------------------------------------
# Equivalence when a gesture carries MORE THAN ONE operation
# ---------------------------------------------------------------------------
# Every dispatcher-backed assertion above uses one operation per gesture, and
# the differential skips any pair that does not fold to exactly one. That made
# the whole class `foldWouldNotReorder` governs invisible: a fold moves an
# operation earlier past the rest of the older gesture, and whether that is
# equivalent depends on what it jumped -- which a single-operation case can
# never exercise.

MULTI_OPERATION_CASES = [
    pytest.param(
        [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}},
         {"type": "update_audio_track", "track_id": "t1", "fields": {"muted": True}}],
        [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": False}}],
        2, id="fold-jumps-a-mute"),
    pytest.param(
        [{"type": "update_clip", "clip_id": "c1",
          "fields": {"timeline_start_frame": 12}},
         {"type": "update_audio_track", "track_id": "t1",
          "fields": {"timeline_start_frame": 6}}],
        [{"type": "update_clip", "clip_id": "c1",
          "fields": {"timeline_start_frame": 30}}],
        3, id="fold-refused-past-a-geometry-write"),
    pytest.param(
        [{"type": "set_lane_count", "lane_type": "video", "count": 3},
         {"type": "update_clip", "clip_id": "c1", "fields": {"track_index": 2}}],
        [{"type": "set_lane_count", "lane_type": "video", "count": 2},
         {"type": "update_clip", "clip_id": "c1", "fields": {"track_index": 1}}],
        4, id="fold-refused-past-a-lane-count"),
    pytest.param(
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "fields": {"name": "A", "color": "", "locked": False, "hidden": True}},
         {"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}],
        [{"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
          "fields": {"name": "A", "color": "", "locked": False, "hidden": False}},
         {"type": "update_clip", "clip_id": "c1", "fields": {"muted": False}}],
        2, id="lane-header-burst-shape"),
]


@pytest.mark.parametrize("older,newer,operations", MULTI_OPERATION_CASES)
def test_a_multi_operation_merge_matches_applying_both(older, newer, operations):
    """The case the fold guard exists for, against the real dispatcher.

    `operations` is asserted first so a case that silently stops folding -- or
    silently starts -- fails here rather than passing an equivalence it is no
    longer exercising.
    """
    build = _scene_builder()
    merged = _coalesced(older, newer)
    assert len(merged) == operations, (
        f"expected {operations} operations, got {len(merged)}: "
        f"{[op['type'] for op in merged]}")
    assert _apply(build, merged) == _apply(build, older + newer)


def test_a_fold_does_not_jump_an_operation_validated_against_its_siblings():
    """The counterexample that disproved "different rows commute".

    `_apply_update_reference_item` calls `_require_no_reference_overlap`, which
    compares the row being written against its siblings on the lane -- so two
    reference moves that succeed in one order can refuse in the other. An
    earlier version of the guard permitted a fold to jump any *collapsible*
    operation and folded exactly this, turning an accepted pair into a 400.
    """
    move = lambda rid, expected, value: {
        "type": "update_reference_item", "reference_item_id": rid,
        "expected": {"start_frame": expected[0], "end_frame": expected[1]},
        "fields": {"start_frame": value[0], "end_frame": value[1]}}
    merged = _coalesced(
        [move("r1", (0, 10), (40, 50)), move("r2", (20, 30), (0, 10))],
        [move("r1", (40, 50), (20, 30))])
    assert len(merged) == 3, (
        "the newer r1 move was folded in front of the r2 move it was authored "
        f"after: {[op['reference_item_id'] for op in merged]}")


def test_what_a_fold_may_jump_is_a_short_explicit_list():
    """Deliberately strict, and pairwise rather than a single predicate.

    A mute writes one boolean on one row and consults nothing, so anything may
    jump it. A lane config may jump another lane config, because
    `_apply_lane_config` reads no sibling lane -- without that pair a bulk hide
    over several lanes would stop folding, which is the measured case. It may
    NOT jump an item write, because `locked` gates those. Everything else
    blocks, rather than being reasoned about case by case.
    """
    fold_over = lambda blocker: len(_coalesced(
        [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": True}}, blocker],
        [{"type": "update_clip", "clip_id": "c1", "fields": {"muted": False}}]))
    assert fold_over({"type": "update_audio_track", "track_id": "t1",
                      "fields": {"muted": True}}) == 2
    assert fold_over({"type": "update_audio_track", "track_id": "t1",
                      "fields": {"muted": True, "volume": 0.5}}) == 3, (
        "a payload that is not muted-only must block the jump")
    assert fold_over({"type": "update_lane_config", "lane_type": "video",
                      "lane_index": 0, "fields": {"locked": True}}) == 3, (
        "an item write may not jump a lane config, which gates it")
    # But a lane config may jump another lane config.
    config = lambda index, hidden: {
        "type": "update_lane_config", "lane_type": "video", "lane_index": index,
        "fields": {"name": "", "color": "", "locked": False, "hidden": hidden}}
    assert len(_coalesced([config(1, True), config(0, True)],
                          [config(0, False), config(1, False)])) == 2, (
        "a bulk hide over two lanes must still fold to one operation per lane")
    assert fold_over({"type": "create_guide", "fields": {"frame_index": 5}}) == 3
