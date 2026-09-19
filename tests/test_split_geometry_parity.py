"""Intentional Python/JavaScript split half-geometry parity.

`web/js/scene_split_geometry.js` exists so the optimistic split can paint a half
that is byte-for-byte what the server will send back. That makes it an
intentional mirror of `_split_clip_object` / `_split_audio_object`, and
`agent_workflow.md` requires an intentional mirror to be protected by a parity
test rather than by care.

The failure this prevents is specific and would not show up as an exception: a
mirror that drifts paints a half with the wrong source window, the author trims
or renders against it during the write, and the canonical response then replaces
it with different numbers. The drift is visible only as a flicker, or not at all.

Two things are deliberately compared rather than asserted about:

* `total_source_frames` carries two conflicting meanings after a split -- the
  server writes this piece's own length, not the source's -- and the mirror
  reproduces that rather than repairing it. A mirror that "fixed" it would make
  the optimistic half trim differently from the half the response brings back,
  which is a worse defect than the one it fixed. Repairing it is a server change
  with its own migration; see the bug tracker.
* Python's `or` treats a stored **0** as absent, so
  `clip.source_out_frame or (end - start)` falls back to the duration on a clip
  placed with no explicit out point -- and real clips store 0 there. The JS uses
  `||` for the same reason. The zero-valued fixtures below are what keep that
  true; `??` would pass every other case and fail exactly those.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server.routes import _split_audio_object, _split_clip_object
from server.timeline_state import AudioTrack, ClipReference, Scene


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "scene_split_geometry.js").as_uri()


# Each case is (clip fields, split frame). Chosen to cross every branch the two
# implementations can disagree on rather than to look realistic.
CLIP_CASES = [
    # Untrimmed, zero source_in AND zero source_out -- the Python-truthiness
    # case. `source_out_frame or (end - start)` falls back to 100 here.
    ({"timeline_start_frame": 0, "timeline_end_frame": 100,
      "source_in_frame": 0, "source_out_frame": 0}, 40),
    # Ordinary trimmed clip.
    ({"timeline_start_frame": 10, "timeline_end_frame": 110,
      "source_in_frame": 25, "source_out_frame": 125,
      "total_source_frames": 400, "source_origin_frame": 25}, 60),
    # Odd frame, odd offsets: integer arithmetic, no rounding anywhere.
    ({"timeline_start_frame": 7, "timeline_end_frame": 93,
      "source_in_frame": 13, "source_out_frame": 99,
      "total_source_frames": 201}, 51),
    # A cut one frame in, and one frame from the end.
    ({"timeline_start_frame": 0, "timeline_end_frame": 50,
      "source_in_frame": 5, "source_out_frame": 55}, 1),
    ({"timeline_start_frame": 0, "timeline_end_frame": 50,
      "source_in_frame": 5, "source_out_frame": 55}, 49),
    # A generated clip carrying takes: the fields a partial mirror forgets, and
    # the ones that make the right half render as the wrong picture.
    ({"timeline_start_frame": 0, "timeline_end_frame": 80,
      "source_in_frame": 0, "source_out_frame": 80,
      "source_path": "media/gen.mp4", "opacity": 0.5, "track_index": 3,
      "role": "render", "strength": 0.75, "muted": True,
      "fit_mode": "cover", "crop_position": "top", "prompt": "a prompt",
      "is_generated": True, "generation_params": {"seed": 7, "model": "x"},
      "takes": [{"take": 1}, {"take": 2}], "active_take": 1,
      "take_metadata": {"scene_id": "s", "selection_start": 4}}, 33),
]

AUDIO_CASES = [
    ({"timeline_start_frame": 0, "timeline_end_frame": 100,
      "source_in_frame": 0}, 40),
    ({"timeline_start_frame": 10, "timeline_end_frame": 110,
      "source_in_frame": 25, "total_source_frames": 400,
      "source_origin_frame": 25}, 60),
    ({"timeline_start_frame": 7, "timeline_end_frame": 93,
      "source_in_frame": 13, "total_source_frames": 201}, 51),
    ({"timeline_start_frame": 0, "timeline_end_frame": 60,
      "source_path": "media/a.wav", "source_in_frame": 9,
      "volume": 0.25, "muted": True, "lane_index": 2}, 59),
]


def _javascript_halves(kind, cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for split geometry parity")
    fn = "splitClipGeometry" if kind == "clip" else "splitAudioGeometry"
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(([item, frame]) => {{
  const copy = JSON.parse(JSON.stringify(item));
  const halves = mod.{fn}(copy, frame, "RIGHT");
  Object.assign(copy, halves.left);
  return {{left: copy, right: halves.right}};
}})));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _python_clip_halves(cases):
    """Both halves, plus the COMPLETE input record the JS half is given.

    The client never sees a partial clip -- it reads `ClipReference.to_dict()`
    out of a scene response -- so the mirror must be driven from the same full
    record. Feeding JS the sparse fixture instead would compare a dict missing
    every defaulted key against one that has them all, and fail for a reason
    that has nothing to do with the split.
    """
    out, inputs = [], []
    for fields, frame in cases:
        scene = Scene(scene_id="s")
        clip = ClipReference(clip_id="LEFT", **fields)
        scene.clips = [clip]
        inputs.append([clip.to_dict(), frame])
        right = _split_clip_object(scene, clip, frame, "RIGHT")
        out.append({"left": clip.to_dict(), "right": right.to_dict()})
    return out, inputs


def _python_audio_halves(cases):
    out, inputs = [], []
    for fields, frame in cases:
        scene = Scene(scene_id="s")
        track = AudioTrack(track_id="LEFT", **fields)
        scene.audio_tracks = [track]
        inputs.append([track.to_dict(), frame])
        right = _split_audio_object(scene, track, frame, "RIGHT")
        out.append({"left": track.to_dict(), "right": right.to_dict()})
    return out, inputs


def test_clip_split_halves_match_field_for_field():
    """Every field of both halves, not just the geometry.

    `takes`, `active_take` and `take_metadata` are in the fixtures because a
    generated clip DRAWS its take: a right half missing them renders as the
    wrong picture for the whole in-flight window, while every frame number in
    the comparison still agrees.
    """
    python, inputs = _python_clip_halves(CLIP_CASES)
    assert _javascript_halves("clip", inputs) == python


def test_audio_split_halves_match_field_for_field():
    python, inputs = _python_audio_halves(AUDIO_CASES)
    assert _javascript_halves("audio", inputs) == python


def test_a_zero_source_out_frame_falls_back_to_the_duration_on_both_sides():
    """The Python-truthiness case, pinned on its own.

    A clip placed with no explicit out point stores `source_out_frame = 0`, and
    `_split_clip_object` reads `clip.source_out_frame or (end - start)`. The JS
    mirror uses `||` to match. `??` would be the natural JavaScript spelling and
    would pass every other fixture in this file while silently giving the right
    half a source window of zero on exactly the clips that are most common.
    """
    fields = {"timeline_start_frame": 0, "timeline_end_frame": 100,
              "source_in_frame": 0, "source_out_frame": 0}
    _python, inputs = _python_clip_halves([(fields, 40)])
    [case] = _javascript_halves("clip", inputs)
    assert case["right"]["source_out_frame"] == 100
    assert case["right"]["total_source_frames"] == 60
    assert case["left"]["source_out_frame"] == 40


def test_the_split_mirror_reproduces_the_two_meanings_of_total_source_frames():
    """Documented drift that must NOT be repaired in the mirror.

    After a split the server writes each half's OWN length into
    `total_source_frames`, not the source's length, so the trim ceiling
    (`total_source_frames - source_out_frame`) computes a negative tail and
    clamps to zero on both halves. That is a tracked server defect. Repairing it
    here would make the optimistic half trim differently from the half the
    canonical response brings back -- a worse failure, and a silent one. This
    test exists so a well-meaning fix to one side fails loudly instead.
    """
    fields = {"timeline_start_frame": 0, "timeline_end_frame": 100,
              "source_in_frame": 20, "source_out_frame": 120,
              "total_source_frames": 500}
    pythons, inputs = _python_clip_halves([(fields, 50)])
    [case] = _javascript_halves("clip", inputs)
    [python] = pythons
    assert case["left"]["total_source_frames"] == python["left"]["total_source_frames"] == 50
    assert case["right"]["total_source_frames"] == python["right"]["total_source_frames"] == 50
    # Both halves therefore report no trimmable tail, which is the defect.
    assert case["left"]["total_source_frames"] - case["left"]["source_out_frame"] < 0
    assert case["right"]["total_source_frames"] - case["right"]["source_out_frame"] < 0
