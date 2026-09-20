"""Intentional Python/JavaScript parity for link-group normalization.

``web/js/scene_link_groups.js`` mirrors ``_prune_linked_item_groups``
(``server/routes.py``), which the mutation pipeline runs after every operation
that can invalidate a link ref. The client runs the same rule optimistically so
a badge painted before the write names the groups the project will actually
hold.

The mirror was unprotected until this file existed, and it had already drifted:
the JavaScript did not dedupe ``group_id`` across groups, so two groups sharing
an id both survived locally while the server re-minted the second. That could
not be reached from the editor -- nothing minted a group id client-side -- which
is precisely why it went unnoticed, and why it mattered to close before the
optimistic link apply started minting them.

**What is compared, and what is not.** The DECISION is compared: which groups
survive, which refs survive them, in what order, and whether each group's id was
kept or re-minted. The re-minted VALUE is not: the server mints
``uuid4().hex[:8]`` and the client mints ``_newLocalItemId("link")``, and they
are not required to agree, because a locally minted id is replaced by the
canonical response. Both sides take their mint function from the outside here so
one deterministic sequence drives both.

**What is and is not covered, measured by injection rather than assumed.**
Fourteen drifts were injected into the mirror and this file re-run. Thirteen
failed loudly, including both ``isPlainObject`` guards: removing the REF guard
makes node throw on the ``None`` ref already in the table, and removing the
GROUP guard is caught only because a malformed group advances the mint counter
before it is discarded, which is what the ``consumes no minted id`` case exists
for.

The one genuine blind spot is narrower than it first looks, and an earlier
version of this docstring named the wrong thing. WEAKENING the ref guard to
``typeof ref === "object"`` -- rather than removing it -- passes, because the
two differ only for an array, and an array has no ``type``, so the allow-list
drops it one line later. The guard stays in that stricter form because a mirror
faithful only where a test looks is not a mirror; but nothing here pins it.

**What this file deliberately does not cover.** ``Scene._normalize_linked_item_groups``
(``server/timeline_state.py``) is a third copy of nearly this rule, applied on
load. It differs by requiring a truthy item id where the routes copy accepts an
empty one. That divergence is pre-existing, reachable only through a row whose
durable id is the empty string, and reconciling two Python copies of one rule is
not this mirror's job -- but a reader who assumes this file pins every copy
would be wrong, so it is written down.
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import server.routes as routes
from server.routes import _prune_linked_item_groups
from server.timeline_state import (
    AudioTrack, ClipReference, GuideFrame, PromptSection, Scene,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "scene_link_groups.js").as_uri()


# Each case is (name, rows, groups). `rows` names the durable ids the scene
# holds; `groups` is the raw `linked_item_groups` value, deliberately including
# shapes no well-formed scene contains, because a mirror is at its most likely
# to diverge on input neither author was picturing.
CASES = [
    (
        "an ordinary pair survives untouched",
        {"clips": ["c1"], "audio": ["a1"]},
        [{"group_id": "keep01", "items": [{"type": "clip", "id": "c1"},
                                          {"type": "audio", "id": "a1"}]}],
    ),
    (
        "a ref to a row the scene no longer holds is dropped, and takes the group with it",
        {"clips": ["c1"]},
        [{"group_id": "keep01", "items": [{"type": "clip", "id": "c1"},
                                          {"type": "clip", "id": "gone"}]}],
    ),
    (
        "a ref repeated inside one group is deduped",
        {"clips": ["c1"], "audio": ["a1"]},
        [{"group_id": "keep01", "items": [{"type": "clip", "id": "c1"},
                                          {"type": "clip", "id": "c1"},
                                          {"type": "audio", "id": "a1"}]}],
    ),
    (
        "a duplicate ref that leaves one survivor dissolves the group",
        {"clips": ["c1"], "audio": ["a1"]},
        [{"group_id": "keep01", "items": [{"type": "clip", "id": "c1"},
                                          {"type": "clip", "id": "c1"}]}],
    ),
    (
        "the SECOND group to use an id is re-minted, not the first",
        {"clips": ["c1", "c2"], "audio": ["a1", "a2"]},
        [
            {"group_id": "same", "items": [{"type": "clip", "id": "c1"},
                                           {"type": "audio", "id": "a1"}]},
            {"group_id": "same", "items": [{"type": "clip", "id": "c2"},
                                           {"type": "audio", "id": "a2"}]},
        ],
    ),
    (
        "a blank id is minted",
        {"clips": ["c1"], "audio": ["a1"]},
        [{"group_id": "", "items": [{"type": "clip", "id": "c1"},
                                    {"type": "audio", "id": "a1"}]}],
    ),
    (
        "a missing id is minted",
        {"clips": ["c1"], "audio": ["a1"]},
        [{"items": [{"type": "clip", "id": "c1"}, {"type": "audio", "id": "a1"}]}],
    ),
    (
        "a falsy non-string id is minted rather than stringified",
        # `str(x or "")` on the server and `String(x || "")` in the mirror. Under
        # `?? ""` the client would keep the id `"0"` and the two would disagree
        # on the group's name for the rest of its life.
        {"clips": ["c1"], "audio": ["a1"]},
        [{"group_id": 0, "items": [{"type": "clip", "id": "c1"},
                                   {"type": "audio", "id": "a1"}]}],
    ),
    (
        "an id freed by a DISSOLVED group may be reused by a later one",
        # Only kept groups reserve an id. The first group here loses a ref and
        # dissolves, so the second keeps `same` rather than being re-minted --
        # the case that separates "seen" from "kept" and the one a plausible
        # reimplementation gets wrong.
        {"clips": ["c1", "c2"], "audio": ["a2"]},
        [
            {"group_id": "same", "items": [{"type": "clip", "id": "c1"},
                                           {"type": "clip", "id": "gone"}]},
            {"group_id": "same", "items": [{"type": "clip", "id": "c2"},
                                           {"type": "audio", "id": "a2"}]},
        ],
    ),
    (
        "an unsupported item type is dropped",
        {"clips": ["c1"], "audio": ["a1"]},
        [{"group_id": "keep01", "items": [{"type": "reference", "id": "r1"},
                                          {"type": "clip", "id": "c1"},
                                          {"type": "audio", "id": "a1"}]}],
    ),
    (
        "guides and prompts link by durable id",
        {"guides": [("g1", 10)], "prompts": ["p1"]},
        [{"group_id": "keep01", "items": [{"type": "guide", "id": "g1"},
                                          {"type": "prompt", "id": "p1"}]}],
    ),
    (
        "a guide addressed by FRAME INDEX is dropped by both, not repaired",
        # The reconciliation this file forced. `_pruneLocalLinkedGroups` used to
        # resolve a guide by frame index and rewrite the ref to its durable id;
        # the server drops it. Unreachable either way -- `Scene.from_dict`
        # normalizes before any scene reaches the client -- so matching the
        # server is free and keeping the repair was a silent second answer.
        {"guides": [("g1", 10)], "prompts": ["p1"]},
        [{"group_id": "keep01", "items": [{"type": "guide", "id": "10"},
                                          {"type": "prompt", "id": "p1"}]}],
    ),
    (
        "a prompt addressed by LIST INDEX is dropped by both, not repaired",
        {"prompts": ["p1", "p2"]},
        [{"group_id": "keep01", "items": [{"type": "prompt", "id": "0"},
                                          {"type": "prompt", "id": "p2"}]}],
    ),
    (
        "a falsy ref id is coerced to the empty string, which a row may actually carry",
        # The fixture that separates `String(x || "")` from `String(x ?? "")`.
        # It needs a row whose durable id IS the empty string, or both spellings
        # simply fail to find the ref and the difference is invisible: under
        # `||` this ref resolves to the empty-id clip and the group survives,
        # under `??` it looks for `"0"`, finds nothing, and the group dissolves.
        {"clips": [""], "audio": ["a1"]},
        [{"group_id": "keep01", "items": [{"type": "clip", "id": 0},
                                          {"type": "audio", "id": "a1"}]}],
    ),
    (
        "a row whose durable id is a NUMBER matches no ref on either side",
        # `_scene_existing_link_ids` applies no `str()`, and `ClipReference.from_dict`
        # does not coerce, so the server compares `"5" in {5}` and gets False.
        # A mirror that stringified the row side would resolve this ref and
        # paint a group the project does not hold. Durable JSON can carry it;
        # the editor never writes it.
        {"clips": [5], "audio": ["a1"]},
        [{"group_id": "keep01", "items": [{"type": "clip", "id": "5"},
                                          {"type": "audio", "id": "a1"}]}],
    ),
    (
        "entries that are not objects are skipped on both levels",
        {"clips": ["c1"], "audio": ["a1"]},
        [
            None,
            "not a group",
            [{"type": "clip", "id": "c1"}],
            {"group_id": "keep01", "items": [
                None, "not a ref", ["clip", "c1"],
                {"type": "clip", "id": "c1"}, {"type": "audio", "id": "a1"}]},
        ],
    ),
    (
        "a non-object group consumes no minted id",
        # The fixture that separates `isPlainObject` from `typeof x === "object"`
        # at the GROUP level. An array has no `items`, so under either spelling
        # it contributes no group -- but the looser one falls into the body and
        # calls the mint before discovering that, so the group AFTER it is named
        # one step further along the sequence than the server names it. Nothing
        # about the entry itself is observable; the mint counter is.
        {"clips": ["c1"], "audio": ["a1"]},
        [
            [{"type": "clip", "id": "c1"}],
            {"group_id": "", "items": [{"type": "clip", "id": "c1"},
                                       {"type": "audio", "id": "a1"}]},
        ],
    ),
    (
        "a group with no items list at all is dropped",
        {"clips": ["c1"], "audio": ["a1"]},
        [{"group_id": "keep01"}, {"group_id": "k2", "items": None}],
    ),
    (
        "group order is preserved",
        {"clips": ["c1", "c2"], "audio": ["a1", "a2"]},
        [
            {"group_id": "second", "items": [{"type": "clip", "id": "c2"},
                                             {"type": "audio", "id": "a2"}]},
            {"group_id": "first", "items": [{"type": "clip", "id": "c1"},
                                            {"type": "audio", "id": "a1"}]},
        ],
    ),
    (
        "ref order inside a group is preserved",
        {"clips": ["c1"], "audio": ["a1"], "guides": [("g1", 4)]},
        [{"group_id": "keep01", "items": [{"type": "guide", "id": "g1"},
                                          {"type": "audio", "id": "a1"},
                                          {"type": "clip", "id": "c1"}]}],
    ),
    (
        "linked_item_groups that is not a list becomes an empty list",
        {"clips": ["c1"]},
        "not a list",
    ),
]


def _scene_dict(rows, groups):
    return {
        "clips": [{"clip_id": value} for value in rows.get("clips", [])],
        "audio_tracks": [{"track_id": value} for value in rows.get("audio", [])],
        "guide_frames": [{"guide_id": gid, "frame_index": frame}
                         for gid, frame in rows.get("guides", [])],
        "prompt_sections": [{"prompt_id": value} for value in rows.get("prompts", [])],
        "linked_item_groups": groups,
    }


def _javascript_prunes(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for link group parity")
    scenes = [_scene_dict(rows, groups) for _, rows, groups in cases]
    script = f"""
const mod = await import({json.dumps(MODULE_URL)});
const scenes = {json.dumps(scenes)};
console.log(JSON.stringify(scenes.map((scene) => {{
  let n = 0;
  const mint = () => `mint${{String(++n).padStart(4, "0")}}`;
  return mod.pruneLinkedItemGroups(scene, mint);
}})));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


class _CountingUuid:
    """`uuid4().hex[:8]` yielding `mint0001`, `mint0002`, ... per scene.

    Substituted for the `uuid` module inside `server.routes` so both languages
    mint the same sequence. Only `_prune_linked_item_groups` runs under it.
    """

    def __init__(self):
        self.count = 0

    def uuid4(self):
        self.count += 1
        return SimpleNamespace(hex=f"mint{self.count:04d}" + "0" * 24)


def _python_prunes(cases, monkeypatch):
    out = []
    for _, rows, groups in cases:
        scene = Scene(scene_id="s", duration_frames=1000)
        scene.clips = [ClipReference(clip_id=value) for value in rows.get("clips", [])]
        scene.audio_tracks = [AudioTrack(track_id=value) for value in rows.get("audio", [])]
        scene.guide_frames = [GuideFrame(guide_id=gid, frame_index=frame)
                              for gid, frame in rows.get("guides", [])]
        sections = []
        for value in rows.get("prompts", []):
            section = PromptSection(start_frame=0, end_frame=1)
            section.prompt_id = value
            sections.append(section)
        scene.prompt_sections = sections
        scene.linked_item_groups = json.loads(json.dumps(groups)) if groups != "not a list" else groups
        monkeypatch.setattr(routes, "uuid", _CountingUuid())
        _prune_linked_item_groups(scene)
        out.append(scene.linked_item_groups)
    return out


def test_link_group_normalization_is_identical_in_both_languages(monkeypatch):
    javascript = _javascript_prunes(CASES)
    python = _python_prunes(CASES, monkeypatch)
    for (name, _, _), js_result, py_result in zip(CASES, javascript, python):
        assert js_result == py_result, name


def test_the_table_actually_exercises_dropping_minting_and_deduping(monkeypatch):
    """A parity table that agrees on nothing interesting agrees trivially.

    Both sides could return `[]` for every case and the comparison above would
    pass. These are the outcomes the table must contain for it to be evidence.
    """
    python = _python_prunes(CASES, monkeypatch)
    flat = [group for scene in python for group in scene]
    assert any(group["group_id"].startswith("mint") for group in flat), "nothing was re-minted"
    assert any(group["group_id"] == "keep01" for group in flat), "no id survived"
    assert any(len(scene) == 0 for scene in python), "no group was dissolved"
    assert any(len(group["items"]) == 3 for group in flat), "no group kept three refs"
    # The reused-id case: the second group keeps `same` because the first
    # dissolved rather than being kept.
    assert any(group["group_id"] == "same" for group in flat), "a freed id was not reused"


def test_a_group_id_collision_is_resolved_the_same_way_in_both_languages(monkeypatch):
    """The drift this file was written to catch, stated on its own.

    The JavaScript mirror kept two groups under one id. Pinning it inside the
    table above would leave a reader unable to tell which case was the live
    defect, so it also stands alone.
    """
    case = [(
        "collision",
        {"clips": ["c1", "c2"], "audio": ["a1", "a2"]},
        [
            {"group_id": "same", "items": [{"type": "clip", "id": "c1"},
                                           {"type": "audio", "id": "a1"}]},
            {"group_id": "same", "items": [{"type": "clip", "id": "c2"},
                                           {"type": "audio", "id": "a2"}]},
        ],
    )]
    [javascript] = _javascript_prunes(case)
    [python] = _python_prunes(case, monkeypatch)
    assert [group["group_id"] for group in javascript] == ["same", "mint0001"]
    assert javascript == python


@pytest.mark.parametrize("action", ["link", "unlink"])
def test_explicit_group_edit_matches_server(action):
    """Regroup survivors, whole-group unlink, durable guide/prompt refs, collision.

    Drive the actual server mutation functions, not a second expected-value
    implementation. Every case also checks that prediction leaves its input alone.
    """
    rows = {"clips": ["c1", "c2", "c3"], "audio": ["a1", "a2"],
            "guides": [("g1", 20)], "prompts": ["p1"]}
    groups = [
        {"group_id": "old", "items": [{"type": "clip", "id": "c1"},
         {"type": "audio", "id": "a1"}, {"type": "guide", "id": "g1"},
         {"type": "prompt", "id": "p1"}]},
        {"group_id": "keep", "items": [{"type": "clip", "id": "c2"}, {"type": "audio", "id": "a2"}]},
    ]
    selections = [
        [{"type": "clip", "id": "c1"}, {"type": "clip", "id": "c2"}],
        [{"type": "guide", "id": "g1"}, {"type": "prompt", "id": "p1"}],
        [{"type": "clip", "id": "c2"}, {"type": "audio", "id": "a2"}],
        [{"type": "clip", "id": "c3"}, {"type": "audio", "id": "a2"}],
    ]
    cases, expected = [], []
    for refs in selections:
        for group_id in ["fresh", "keep"]:
            data = _scene_dict(rows, groups)
            scene = Scene.from_dict({**data, "scene_id": "s", "duration_frames": 100})
            try:
                if action == "link":
                    routes._add_link_group(scene, refs, group_id)
                else:
                    expanded = routes._expand_linked_refs(scene, refs, True)
                    routes._unlink_refs(scene, expanded)
                    routes._prune_linked_item_groups(scene)
                expected.append(scene.linked_item_groups)
            except routes.ProjectMutationRequestError as exc:
                assert exc.code == "id_conflict"
                expected.append(None)
            cases.append({"scene": data, "refs": refs, "options":
                {"groupId": group_id} if action == "link" else {"entireGroup": True}})
    script = f"""
        import assert from 'node:assert/strict';
        import {{ editedLinkGroups }} from {json.dumps(MODULE_URL)};
        const cases = {json.dumps(cases)};
        const results = cases.map(c => {{
            const before = JSON.stringify(c.scene);
            const result = editedLinkGroups(c.scene, c.refs, c.options, () => 'mint0001');
            assert.equal(JSON.stringify(c.scene), before);
            return result;
        }});
        console.log(JSON.stringify(results));
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for link-group parity")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
