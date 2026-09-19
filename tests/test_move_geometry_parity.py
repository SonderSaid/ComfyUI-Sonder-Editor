"""Intentional Python/JavaScript parity for an optimistic timeline move.

`web/js/scene_move_geometry.js` mirrors two things and only two:

* the ``timeline_start_frame``-without-``timeline_end_frame`` branch of
  ``_apply_update_clip`` / ``_apply_update_audio_track``, which preserves the
  duration and clamps the start with ``max(0, int(x))``; and
* the ``pure_move`` arm of ``_apply_linked_bounds_update``, which adds the
  anchor's delta to every member and leaves every source window alone.

The drift this prevents is silent: a mirror that disagrees paints a position the
response then replaces with a different one, and since ``_pushUndo`` snapshots
the scene verbatim, a later Undo can write the wrong one back to disk.

**What this file does not protect, stated because it was measured rather than
assumed.** Four drifts were injected into the mirror and the file was re-run.
Two failed loudly -- a one-frame duration error, and giving a guide an
``end_frame`` it does not have. Two PASSED: swapping ``Math.trunc`` for
``Math.floor``, which nothing can distinguish while the ``max(0, ...)`` clamp
flattens every negative to zero; and clamping each linked member independently,
which is unreachable because ``_applyLocalItemMove`` now refuses to paint a
linked move at all when the delta would carry any member below zero -- the
server refuses that move, so painting it would draw a row the project will never
hold. Both are recorded beside the code they concern. A parity test that is
believed to cover more than it does is worse than one whose limits are written
down.

The server's validation is otherwise NOT mirrored and not compared here: lane
fit, lane locks, prompt ranges and the driver-lane preflight stay
server-authoritative, and the optimistic paint says where the item goes *if the
move is allowed*.

"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server.routes import (
    _apply_linked_bounds_update, _apply_update_audio_track, _apply_update_clip,
)
from server.timeline_state import (
    AudioTrack, ClipReference, GuideFrame, PromptSection, Scene, TimelineProject,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "scene_move_geometry.js").as_uri()


# (start, end, requested new start). Chosen to cross every branch the two
# implementations can disagree on, not to look realistic.
MOVE_CASES = [
    (0, 100, 250),        # ordinary forward move
    (40, 90, 0),          # to the very start
    (40, 90, -7),         # negative: max(0, ...) on both sides
    (40, 90, -0.5),       # negative AND fractional; the clamp flattens both
    (7, 93, 51.9),        # fractional forward: truncated, not rounded
    (0, 1, 0),            # a no-op move of a one-frame item
    (250, 300, 3),        # backwards past the start of another item
]


def _javascript_moves(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for move geometry parity")
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(([start, end, requested]) =>
  mod.movedMediaBounds(start, end, requested))));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _python_clip_moves(cases):
    out = []
    for start, end, requested in cases:
        scene = Scene(scene_id="s", duration_frames=4000)
        clip = ClipReference(clip_id="c", timeline_start_frame=start,
                             timeline_end_frame=end, track_index=0)
        scene.clips = [clip]
        project = TimelineProject(name="p", scenes=[scene])
        _apply_update_clip(project, scene, "c",
                           {"timeline_start_frame": requested})
        out.append({"start": clip.timeline_start_frame,
                    "end": clip.timeline_end_frame})
    return out


def _python_audio_moves(cases):
    out = []
    for start, end, requested in cases:
        scene = Scene(scene_id="s", duration_frames=4000)
        track = AudioTrack(track_id="t", timeline_start_frame=start,
                           timeline_end_frame=end, lane_index=0)
        scene.audio_tracks = [track]
        _apply_update_audio_track(scene, "t", {"timeline_start_frame": requested})
        out.append({"start": track.timeline_start_frame,
                    "end": track.timeline_end_frame})
    return out


def test_a_clip_move_preserves_its_duration_identically_in_both_languages():
    cases = [list(case) for case in MOVE_CASES]
    assert _javascript_moves(cases) == _python_clip_moves(MOVE_CASES)


def test_an_audio_move_preserves_its_duration_identically_in_both_languages():
    cases = [list(case) for case in MOVE_CASES]
    assert _javascript_moves(cases) == _python_audio_moves(MOVE_CASES)


def test_a_fractional_target_truncates_rather_than_rounding():
    """Truncation, which is what both `int()` and `Math.trunc` mean.

    Note what this does NOT prove. `Math.floor` was injected into the mirror and
    this whole file still passed, because floor and trunc differ only on a
    negative input and the `max(0, ...)` clamp flattens every negative to zero.
    No fixture can separate them while the clamp is there, and pretending
    otherwise is how a parity test starts certifying a choice it cannot see. The
    module says the same thing beside the code.

    What this does pin is that neither side ROUNDS: `51.9` must become `51`, and
    a mirror using `Math.round` would move the item a frame further than the
    server does on roughly half of all fractional inputs.
    """
    [negative] = _javascript_moves([[40, 90, -0.5]])
    assert negative == {"start": 0, "end": 50}
    [fractional] = _javascript_moves([[7, 93, 51.9]])
    assert fractional != {"start": 52, "end": 138}, "rounded, not truncated"
    assert fractional == {"start": 51, "end": 137}
    assert _python_clip_moves([(7, 93, 51.9)]) == [fractional]


def test_a_linked_move_adds_one_delta_to_every_member():
    """The anchor's delta, computed once -- not clamped per member.

    A group straddling frame zero is the case that separates the two: clamping
    each member independently piles them up at zero instead of moving them
    together, and every member except the anchor ends up in the wrong place.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for move geometry parity")

    scene = Scene(scene_id="s", duration_frames=4000)
    clip = ClipReference(clip_id="c", timeline_start_frame=100,
                         timeline_end_frame=200, source_in_frame=12,
                         source_out_frame=112, track_index=0)
    track = AudioTrack(track_id="t", timeline_start_frame=110,
                       timeline_end_frame=160, source_in_frame=5, lane_index=0)
    section = PromptSection(120, 170)
    section.prompt_id = "p"
    guide = GuideFrame(guide_id="g", frame_index=130)
    scene.clips = [clip]
    scene.audio_tracks = [track]
    scene.prompt_sections = [section]
    scene.guide_frames = [guide]
    scene.linked_item_groups = [{"group_id": "g1", "items": [
        {"type": "clip", "id": "c"}, {"type": "audio", "id": "t"},
        {"type": "prompt", "id": "p"}, {"type": "guide", "id": "g"}]}]
    project = TimelineProject(name="p", scenes=[scene])

    _apply_linked_bounds_update(
        project, scene, {"type": "clip", "id": "c"},
        {"timeline_start_frame": 40})

    delta = 40 - 100
    # Driven through `memberBounds` from the stored ROWS, not from literal
    # tuples: reading is half the mirror, and a guide's end is DERIVED
    # (`[idx, idx + 1]`, per `_item_bounds`) rather than stored. Handing the
    # JavaScript a pre-computed `131` would have tested nothing about that.
    rows = [
        ["clip", {"timeline_start_frame": 100, "timeline_end_frame": 200}],
        ["audio", {"timeline_start_frame": 110, "timeline_end_frame": 160}],
        ["prompt", {"start_frame": 120, "end_frame": 170}],
        ["guide", {"frame_index": 130}],
    ]
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const rows = {json.dumps(rows)};
console.log(JSON.stringify(rows.map(([type, data]) => {{
  const at = mod.memberBounds(type, data);
  const moved = mod.movedMemberBounds(at.start, at.end, {delta});
  const row = {{}};
  mod.writeMemberBounds(type, row, moved.start, moved.end);
  return row;
}})));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    javascript = json.loads(result.stdout)

    assert javascript[0] == {"timeline_start_frame": clip.timeline_start_frame,
                             "timeline_end_frame": clip.timeline_end_frame}
    assert javascript[1] == {"timeline_start_frame": track.timeline_start_frame,
                             "timeline_end_frame": track.timeline_end_frame}
    assert javascript[2] == {"start_frame": section.start_frame,
                             "end_frame": section.end_frame}
    # A guide stores one frame. The mirror must write `frame_index` alone or it
    # invents a field `GuideFrame` does not have.
    assert javascript[3] == {"frame_index": guide.frame_index}
    assert "end_frame" not in javascript[3]

    # And a pure move leaves every source window exactly where it was.
    assert (clip.source_in_frame, clip.source_out_frame) == (12, 112)
    assert track.source_in_frame == 5


def test_the_server_refuses_a_linked_move_that_would_carry_a_member_negative():
    """Why `_applyLocalItemMove` declines to paint that move at all.

    The client cannot clamp its way out of this: the delta is the anchor's, and
    clamping each member independently would pile the group up at zero instead
    of moving it together. The server's answer is to refuse the whole move, so
    the honest optimistic answer is to paint nothing and let the refusal speak.
    """
    scene = Scene(scene_id="s", duration_frames=4000)
    clip = ClipReference(clip_id="c", timeline_start_frame=100,
                         timeline_end_frame=200, track_index=0)
    # A partner that starts BEFORE the anchor: a delta big enough to put the
    # anchor at zero puts this one below it.
    track = AudioTrack(track_id="t", timeline_start_frame=50,
                       timeline_end_frame=150, lane_index=0)
    scene.clips = [clip]
    scene.audio_tracks = [track]
    scene.linked_item_groups = [{"group_id": "g1", "items": [
        {"type": "clip", "id": "c"}, {"type": "audio", "id": "t"}]}]
    project = TimelineProject(name="p", scenes=[scene])

    with pytest.raises(Exception) as refused:
        _apply_linked_bounds_update(
            project, scene, {"type": "clip", "id": "c"},
            {"timeline_start_frame": 0})
    assert "invalid_range" in str(refused.value) or "range" in str(refused.value).lower()
    # The anchor IS moved in memory before the partner raises -- `_apply_ref_bounds`
    # runs per ref and the loop is not transactional. That costs nothing on the
    # server, where the request is abandoned before `save_project` and the next
    # one reloads from disk. It is precisely what the CLIENT cannot copy: there,
    # the half-applied scene is what the author is looking at, and a later
    # `_pushUndo` would snapshot it. Planning every member before writing any is
    # the client's equivalent of the server's reload.
    assert clip.timeline_start_frame == 0
    assert track.timeline_start_frame == 50


# ---------------------------------------------------------------------------
# Refusal parity: does the client decline to paint exactly the moves the server
# declines to apply? This compares the DECISION, not the arithmetic, which is
# the half an adversarial audit of this landing found missing -- the mirror
# reproduced `_apply_ref_bounds`' maths and one of its four refusal conditions.
# ---------------------------------------------------------------------------

def _javascript_refusal(moves, duration, others):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for move geometry parity")
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
console.log(JSON.stringify(mod.linkedMoveRefusal({json.dumps(moves)}, {{
  sceneDuration: {json.dumps(duration)},
  otherPromptRanges: {json.dumps(others)},
}})));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _python_linked_move_refused(members, duration, anchor_new_start, others=()):
    """Run the real server path and report whether it refused."""
    scene = Scene(scene_id="s", duration_frames=duration)
    scene.clips, scene.audio_tracks, scene.prompt_sections, scene.guide_frames = [], [], [], []
    refs = []
    for kind, start, end in members:
        if kind == "clip":
            scene.clips.append(ClipReference(
                clip_id="c", timeline_start_frame=start, timeline_end_frame=end,
                track_index=0))
            refs.append({"type": "clip", "id": "c"})
        elif kind == "audio":
            scene.audio_tracks.append(AudioTrack(
                track_id="t", timeline_start_frame=start, timeline_end_frame=end,
                lane_index=0))
            refs.append({"type": "audio", "id": "t"})
        elif kind == "prompt":
            section = PromptSection(start, end)
            section.prompt_id = "p"
            scene.prompt_sections.append(section)
            refs.append({"type": "prompt", "id": "p"})
        elif kind == "guide":
            scene.guide_frames.append(GuideFrame(guide_id="g", frame_index=start))
            refs.append({"type": "guide", "id": "g"})
    for index, (start, end) in enumerate(others):
        other = PromptSection(start, end)
        other.prompt_id = f"other{index}"
        scene.prompt_sections.append(other)
    scene.linked_item_groups = [{"group_id": "g1", "items": refs}]
    project = TimelineProject(name="p", scenes=[scene])
    try:
        _apply_linked_bounds_update(
            project, scene, refs[0], {"timeline_start_frame": anchor_new_start})
    except Exception:
        return True
    return False


# (label, members as (type, start, end), scene duration, anchor's new start,
#  other prompt sections). The anchor is always the first member.
REFUSAL_CASES = [
    ("plain forward move", [("clip", 100, 200), ("audio", 110, 160)], 400, 150, ()),
    ("a member carried below zero", [("clip", 100, 200), ("audio", 50, 150)], 400, 0, ()),
    ("a guide pushed past the scene end",
     [("clip", 100, 200), ("guide", 250, 251)], 300, 160, ()),
    ("a guide landing exactly on the last frame",
     [("clip", 100, 200), ("guide", 250, 251)], 300, 149, ()),
    ("a prompt pushed past the scene end",
     [("clip", 100, 200), ("prompt", 200, 280)], 300, 150, ()),
    ("a prompt landing exactly on the scene end",
     [("clip", 100, 200), ("prompt", 200, 280)], 300, 120, ()),
    ("a prompt moved onto another section",
     [("clip", 100, 200), ("prompt", 0, 50)], 400, 150, ((60, 120),)),
    ("a prompt moved clear of another section",
     [("clip", 100, 200), ("prompt", 0, 50)], 400, 110, ((160, 220),)),
    # The two boundaries below were added because injecting an off-by-one into
    # each comparison left the rest of this table green. A parity test that
    # cannot see a `>` where a `>=` belongs is not testing the comparison.
    ("a guide landing exactly ON the duration",        # `frame >= duration`
     [("clip", 100, 200), ("guide", 250, 251)], 300, 150, ()),
    ("a prompt ending exactly where another begins",   # touching is not overlap
     [("clip", 100, 200), ("prompt", 0, 50)], 400, 210, ((160, 220),)),
]


@pytest.mark.parametrize("label,members,duration,new_start,others", REFUSAL_CASES,
                         ids=[case[0] for case in REFUSAL_CASES])
def test_the_client_declines_exactly_the_linked_moves_the_server_refuses(
        label, members, duration, new_start, others):
    """The decision, compared directly, in both directions.

    An asymmetry either way is a defect: declining a move the server would
    accept re-imposes the lockout this whole phase exists to remove, and
    painting one the server refuses draws a row the project will never hold --
    which a later `_pushUndo` can then write to disk, because the scene-window
    checks are deliberately absent from the history content validator.
    """
    anchor_start = members[0][1]
    delta = max(0, int(new_start)) - anchor_start
    moves = [{"type": kind, "start": start + delta, "end": end + delta}
             for kind, start, end in members]
    javascript = _javascript_refusal(moves, duration, [list(pair) for pair in others])
    python_refused = _python_linked_move_refused(members, duration, new_start, others)
    assert bool(javascript) == python_refused, (
        f"{label}: client said {javascript!r}, server "
        f"{'refused' if python_refused else 'accepted'}")
