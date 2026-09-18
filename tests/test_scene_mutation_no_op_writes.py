"""A scene mutation that changes nothing does not write, and does not lie.

The wasted bytes were never the point. `save_project` bumps `modified_at`, so a
no-op write makes a second writer holding the version it legitimately read fail
its own precondition, heal, and retry against a document that never changed --
and it fans out a `project_updated` broadcast to every open editor. Probed at
`8ddd3e9`: an identical `width`/`height`, an identical `duration_frames` and an
identical lane config all returned 200, all rewrote `project.json`, and all moved
the version.

**The failure mode is worse than the waste, which is what shapes these tests.**
The response returns the *in-memory* scene, which the client adopts through
`_reconcileActiveSceneFromMutation`. A write skipped in error would hand every
client a canonical scene the disk never received -- silent divergence, on the
editor's most-used route. So the central test here is not "did it skip the
write" but `test_the_response_scene_always_matches_what_the_next_reader_loads`,
which holds for both outcomes and would fail on any false "unchanged" whatever
produced it.

It compares against a fresh **load** rather than against the file's bytes, and
that is deliberate. Deserialization is not a byte-inverse of serialization: an
older document with short lane arrays is padded at load, so the model a reader
gets has legitimately never been written in that form. Every GET already returns
such a scene. The invariant that matters is not "the client holds the bytes on
disk" but "the client holds what any other reader will get", and the looser
reading is the only one that is true of the system as it already behaves.
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
from server import routes
from server.project_manager import (ProjectVersionConflict, create_project,
                                    load_project, save_project)
from server.timeline_state import LaneConfig, PromptSection, Scene

MUTATIONS_PATH = "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations"


class DummyRequest(dict):
    def __init__(self, *, match_info=None, body=None, method="POST", headers=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = {}
        self.headers = headers or {}
        self._body = body
        self.method = method
        self.path = "/sonder-editor/project/p"

    async def json(self):
        return self._body


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _handler(route_module):
    for route in route_module.routes:
        if route.method == "POST" and route.path == MUTATIONS_PATH:
            return route.handler
    raise AssertionError("scene mutations route not found")


@pytest.fixture
def project_fixture(monkeypatch, tmp_path):
    """A saved one-scene project, plus a `post(operations)` helper."""
    route_module = _load_route_module(monkeypatch)
    project = create_project("No-op writes", base_dir=str(tmp_path))
    scene = Scene(scene_id="s1", name="Scene", duration_frames=100,
                  width=640, height=480)
    scene.prompt_sections = [PromptSection(0, 50, channels={"visual": "a"})]
    project.scenes = [scene]
    save_project(project, notify=False)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    handler = _handler(route_module)
    project_id = os.path.basename(project.project_dir)
    project_file = os.path.join(project.project_dir, "project.json")

    def post(operations, if_match=""):
        response = asyncio.run(handler(DummyRequest(
            match_info={"project_id": project_id, "scene_id": "s1"},
            headers={"If-Match": if_match} if if_match else {},
            body={"operations": operations})))
        return response.status, json.loads(response.body.decode("utf-8"))

    def stored():
        with open(project_file, encoding="utf-8") as handle:
            return json.load(handle)

    return SimpleNamespace(
        routes=route_module, project_dir=project.project_dir,
        project_file=project_file, post=post, stored=stored)


def _version_and_mtime(fixture):
    return (fixture.stored()["modified_at"], os.stat(fixture.project_file).st_mtime_ns)


# ---------------------------------------------------------------------------
# The guarantee
# ---------------------------------------------------------------------------

def test_an_identical_scene_field_write_does_not_touch_the_document(project_fixture):
    before = _version_and_mtime(project_fixture)
    status, payload = project_fixture.post([
        {"type": "update_scene_fields", "fields": {"width": 640, "height": 480}}])
    assert status == 200
    assert payload["committed"] is False
    assert _version_and_mtime(project_fixture) == before, (
        "an identical write must not move `modified_at` -- that bump is what "
        "makes a concurrent writer fail its own precondition")


def test_an_identical_duration_write_does_not_touch_the_document(project_fixture):
    before = _version_and_mtime(project_fixture)
    status, payload = project_fixture.post([
        {"type": "update_scene_fields", "fields": {"duration_frames": 100}}])
    assert status == 200
    assert payload["committed"] is False
    assert _version_and_mtime(project_fixture) == before


def test_an_identical_lane_config_write_does_not_touch_the_document(project_fixture):
    """The second adopter, and the one that pays off most.

    `sonder_editor_bugs.md` measures a lane-hide burst at six clicks in 296 ms
    costing 24.2 s of serialized writes, and a mute/lock burst at 61 writes over
    460 s. A redundant toggle in such a burst now costs nothing.
    """
    before = _version_and_mtime(project_fixture)
    status, payload = project_fixture.post([
        {"type": "update_lane_config", "lane_type": "guide",
         "fields": {"hidden": False, "locked": False}}])
    assert status == 200
    assert payload["committed"] is False
    assert _version_and_mtime(project_fixture) == before


def test_a_real_change_still_commits(project_fixture):
    before = _version_and_mtime(project_fixture)
    status, payload = project_fixture.post([
        {"type": "update_scene_fields", "fields": {"width": 1280}}])
    assert status == 200
    assert payload["committed"] is True
    assert _version_and_mtime(project_fixture) != before
    assert project_fixture.stored()["scenes"][0]["width"] == 1280


def test_an_empty_batch_writes_nothing(project_fixture):
    """It used to rewrite the whole document to do nothing at all."""
    before = _version_and_mtime(project_fixture)
    status, payload = project_fixture.post([])
    assert status == 200
    assert payload["committed"] is False
    assert _version_and_mtime(project_fixture) == before


# ---------------------------------------------------------------------------
# The divergence guard -- the test that matters most
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operations,expect_committed", [
    ([{"type": "update_scene_fields", "fields": {"width": 640}}], False),
    ([{"type": "update_scene_fields", "fields": {"width": 800}}], True),
    ([{"type": "update_lane_config", "lane_type": "guide",
       "fields": {"hidden": False}}], False),
    ([{"type": "update_lane_config", "lane_type": "guide",
       "fields": {"hidden": True}}], True),
    ([{"type": "update_scene_fields", "fields": {"width": 640}},
      {"type": "update_lane_config", "lane_type": "prompt",
       "fields": {"locked": False}}], False),
    ([{"type": "update_scene_fields", "fields": {"name": "Renamed"}},
      {"type": "update_lane_config", "lane_type": "prompt",
       "fields": {"locked": False}}], True),
])
def test_the_response_scene_always_matches_what_the_next_reader_loads(
        project_fixture, operations, expect_committed):
    """A skipped write must never leave the client ahead of the document.

    The response carries the in-memory scene and the client adopts it as
    canonical, so this holds the whole mechanism honest: whatever decides
    `committed`, the scene the client is handed has to be the scene the next
    reader loads. A write wrongly skipped fails here, because the reload still
    holds the old value while the response holds the new one.
    """
    status, payload = project_fixture.post(operations)
    assert status == 200
    assert payload["committed"] is expect_committed
    reloaded = load_project(project_fixture.project_dir).scenes[0].to_dict()
    assert payload["scene"] == reloaded, (
        "the response scene and a fresh load disagree; a client adopting this "
        "response would be holding state no other reader will ever see")


def test_a_legacy_short_lane_array_is_not_rewritten_at_rest(project_fixture):
    """The hazard the plan named does not exist in the shape it described.

    The plan's worry was that `_apply_scene_fields`'s unconditional tail,
    `_ensure_scene_lane_config_lengths`, legitimately pads an older document, so
    a per-field "did the named setters write the same values" claim would report
    no change while the branch had in fact repaired something.

    Traced, and the padding happens at LOAD, not in the handler: a freshly loaded
    scene is already padded, and the tail is a no-op on it
    (`test_the_unconditional_tail_is_a_no_op_on_a_freshly_loaded_scene`). So an
    identical write really does change nothing, and the legacy encoding stays on
    disk untouched -- which is what `agent_workflow.md` asks for anyway: *project
    compat is tolerance ... never rewrite at rest*. The repair is deterministic
    and re-runs on every load, so nothing is lost and no reader sees a different
    scene. The next real write persists the padded form as it always did.
    """
    project = load_project(project_fixture.project_dir)
    scene = project.scenes[0]
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig()]
    save_project(project, notify=False)
    assert len(project_fixture.stored()["scenes"][0]["video_lane_configs"]) == 1
    before = _version_and_mtime(project_fixture)

    status, payload = project_fixture.post([
        {"type": "update_scene_fields", "fields": {"width": 640}}])
    assert status == 200
    assert payload["committed"] is False
    assert _version_and_mtime(project_fixture) == before
    assert len(project_fixture.stored()["scenes"][0]["video_lane_configs"]) == 1, (
        "the legacy shape must be left alone, not rewritten at rest")
    assert len(payload["scene"]["video_lane_configs"]) == 3
    assert payload["scene"] == load_project(
        project_fixture.project_dir).scenes[0].to_dict(), (
        "the client must hold exactly what the next reader will load")

    status, payload = project_fixture.post([
        {"type": "update_scene_fields", "fields": {"width": 1280}}])
    assert payload["committed"] is True
    assert len(project_fixture.stored()["scenes"][0]["video_lane_configs"]) == 3, (
        "the next real write persists the padded form as it always did")


@pytest.mark.parametrize("mutate,expect_no_op", [
    (lambda scene: None, True),
    # `Scene.from_dict` pads, so load already covers the short direction.
    (lambda scene: (setattr(scene, "video_lane_count", 4),
                    setattr(scene, "video_lane_configs", [LaneConfig()])), True),
    # It does NOT clamp a non-positive count, and it does NOT trim an over-long
    # array. Both are the handler's tail, on a scene that was just loaded.
    (lambda scene: setattr(scene, "video_lane_count", 0), False),
    (lambda scene: setattr(scene, "audio_lane_count", -2), False),
    (lambda scene: setattr(scene, "reference_lane_count", 0), False),
    (lambda scene: setattr(scene, "motion_driver_lane_count", 0), False),
    (lambda scene: (setattr(scene, "video_lane_count", 1),
                    setattr(scene, "video_lane_configs",
                            [LaneConfig(), LaneConfig(), LaneConfig()])), False),
])
def test_where_the_unconditional_tail_still_changes_a_freshly_loaded_scene(
        project_fixture, mutate, expect_no_op):
    """The plan's named hazard, measured rather than dismissed.

    An earlier pass of this landing recorded that `_ensure_scene_lane_config_lengths`
    is simply a no-op after load, because `Scene.from_dict` pads. That is true of
    the short direction only. The tail ALSO clamps a non-positive count through
    `max(1, ...)` and TRIMS an array longer than its count, and load does neither
    -- so on those documents the tail really is a change the handler makes.

    No data is at risk, and that is the point worth keeping: the comparison sees
    every one of these, so the batch commits. Under the per-field flag the plan
    specified, these are exactly the shapes that would have reported "unchanged"
    while the handler had repaired something -- the silent divergence the plan
    warned about, in the branch it named.
    """
    project = load_project(project_fixture.project_dir)
    mutate(project.scenes[0])
    save_project(project, notify=False)

    loaded = load_project(project_fixture.project_dir).scenes[0]
    before = loaded.to_dict()
    routes._ensure_scene_lane_config_lengths(loaded)
    assert (loaded.to_dict() == before) is expect_no_op


def test_a_document_the_tail_repairs_is_committed_not_skipped(project_fixture):
    """The safety net under the test above: a repair always reaches disk."""
    project = load_project(project_fixture.project_dir)
    project.scenes[0].video_lane_count = 0
    save_project(project, notify=False)
    before = _version_and_mtime(project_fixture)

    status, payload = project_fixture.post([
        {"type": "update_scene_fields", "fields": {"width": 640}}])
    assert status == 200
    assert payload["committed"] is True, (
        "the tail clamped a non-positive lane count, so this is not a no-op")
    assert _version_and_mtime(project_fixture) != before
    assert payload["scene"] == load_project(
        project_fixture.project_dir).scenes[0].to_dict()


# ---------------------------------------------------------------------------
# The allow-list, proved rather than asserted
# ---------------------------------------------------------------------------

def test_the_allow_list_only_holds_operations_the_dispatcher_accepts(project_fixture):
    import test_scene_mutation_registration as registration
    unknown = routes._SCENE_ONLY_MUTATIONS - registration._dispatcher_op_types()
    assert not unknown, f"_SCENE_ONLY_MUTATIONS names non-operations: {sorted(unknown)}"


# One payload per operation proved almost nothing: it reached `update_scene_fields`
# through four plain scalars and `update_lane_config` through its fixed-config
# branch only, while the design comment claims `fps`, `prompt_edit`, attachments,
# the lane-count attrs, the variable-lane branch and the recipe branch were all
# traced. These are those branches.
SCENE_ONLY_PROBES = {
    "update_scene_fields": [
        {"type": "update_scene_fields",
         "fields": {"width": 1024, "height": 768, "duration_frames": 250,
                    "name": "Probed"}},
        {"type": "update_scene_fields", "fields": {"fps": 30.0}},
        {"type": "update_scene_fields", "fields": {"video_lane_count": 3,
                                                   "reference_lane_count": 2}},
        {"type": "update_scene_fields",
         "fields": {"prompt_context_profile_id": "",
                    "prompt_context_profile_config": {"a": 1},
                    "generation_params": {"steps": 4}}},
        {"type": "update_scene_fields",
         "fields": {"global_channels": {"visual": "hello"}},
         "expected": {"global_channels": {}}},
        {"type": "update_scene_fields",
         "fields": {"global_attachments": []},
         "expected": {"global_attachments": []}},
    ],
    "update_lane_config": [
        {"type": "update_lane_config", "lane_type": "guide",
         "fields": {"name": "G", "color": "#fff", "hidden": True, "locked": True}},
        {"type": "update_lane_config", "lane_type": "prompt_global",
         "fields": {"hidden": True}},
        {"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
         "fields": {"name": "V1", "locked": True}},
        {"type": "update_lane_config", "lane_type": "reference", "lane_index": 0,
         "fields": {"reference_recipe": {"media_kind": "image"}}},
    ],
}


def test_a_scene_only_operation_leaves_the_rest_of_the_project_alone(project_fixture):
    """The runtime proof the allow-list rests on.

    An AST check cannot establish this -- `_apply_scene_fields` legitimately
    receives `project` and reads it. So drive each allow-listed operation with a
    payload that really changes something, and assert the project outside its
    scenes is untouched. If one ever grows a project-level write, the scene
    comparison would stop being an exact answer, and this fails rather than the
    write silently going missing.
    """
    assert set(SCENE_ONLY_PROBES) == set(routes._SCENE_ONLY_MUTATIONS), (
        "every allow-listed operation needs probes that exercise it")
    for name, operations in SCENE_ONLY_PROBES.items():
        for operation in operations:
            project = load_project(project_fixture.project_dir)
            scene = project.scenes[0]
            before = copy.deepcopy(project.to_dict(include_internal=True))
            routes._apply_scene_mutation_operation(project, scene, operation)
            after = project.to_dict(include_internal=True)
            for key in ("scenes", "modified_at"):
                before.pop(key, None)
                after.pop(key, None)
            assert before == after, (
                f"{name} wrote outside its scene on {sorted(operation)}, so a "
                "scene comparison can no longer answer for it")


def test_a_batch_that_reaches_past_the_scene_still_commits(project_fixture):
    """One unlisted operation takes the whole batch off the fast path.

    `delete_link_group` is the sharpest probe available: it is not allow-listed,
    and with an id that matches nothing it changes precisely nothing -- so the
    commit that follows can only be the allow-list gate doing its job, and not
    the scene having moved. An unlisted operation is assumed to change something,
    which is the safe direction and what keeps every un-traced branch exactly as
    it was.
    """
    before = _version_and_mtime(project_fixture)
    status, payload = project_fixture.post([
        {"type": "update_scene_fields", "fields": {"width": 640}},
        {"type": "delete_link_group", "group_id": "no-such-group"},
    ])
    assert status == 200
    assert payload["committed"] is True
    assert _version_and_mtime(project_fixture) != before
    assert payload["scene"] == load_project(
        project_fixture.project_dir).scenes[0].to_dict()


# ---------------------------------------------------------------------------
# The premise the mechanism rests on
# ---------------------------------------------------------------------------

def test_scene_to_dict_is_deterministic_and_is_what_gets_persisted():
    """Two claims, both load-bearing.

    Deterministic, or an unchanged scene would compare unequal and the skip
    would never fire -- and worse, a normalizer that minted ids would make the
    comparison meaningless rather than merely ineffective. And identical to what
    the project persists, or the comparison would answer a different question
    than "would the saved document differ".
    """
    scene = Scene(scene_id="s1", name="S", duration_frames=100, width=64, height=64)
    scene.set_global_channels({"visual": "hello", "speech": "hi"})
    scene.prompt_sections = [PromptSection(0, 50, channels={"visual": "a"}),
                             PromptSection(50, 100)]
    assert scene.to_dict() == scene.to_dict()

    from server.timeline_state import TimelineProject
    project = TimelineProject(project_id="p", name="P", project_dir="")
    project.scenes = [scene]
    assert project.to_dict(include_internal=True)["scenes"][0] == scene.to_dict()
    assert project.to_dict()["scenes"][0] == scene.to_dict(), (
        "`include_internal` must not give scenes a second serialization, or the "
        "comparison would be blind to whatever the internal form adds")


# ---------------------------------------------------------------------------
# Not writing is not the same as having nothing to check
# ---------------------------------------------------------------------------

def _write_from_another_process(project_dir, name):
    """A competing writer, exactly where a second tab or the worker lands."""
    other = load_project(project_dir)
    other.scenes[0].name = name
    save_project(other, notify=False)


@pytest.mark.parametrize("operations,label", [
    ([{"type": "update_scene_fields", "fields": {"width": 640}}],
     "the skip path"),
    ([{"type": "update_scene_fields", "fields": {"width": 640}},
      {"type": "delete_link_group", "group_id": "none"}],
     "the save path"),
])
def test_a_concurrent_write_inside_the_window_is_refused_either_way(
        project_fixture, monkeypatch, operations, label):
    """The regression this landing introduced before it was caught, pinned.

    `_load_project_from_request` releases the project write lock before the
    handler body runs, so `save_project`'s in-lock version re-check is the only
    compare-and-swap this route has. Dropping the write to save a no-op also
    dropped that check, and the probe was unambiguous: 200, `committed: false`,
    and a response scene still named `Scene` while the document said `Writer B`.
    `_reconcileActiveSceneFromMutation` adopts that with no version gate of its
    own and clears any deferred refresh, so nothing would have healed it.

    Both paths are parametrised deliberately. The save path has always refused
    here; the point of the test is that the skip path is indistinguishable from
    it, now and after whatever changes next.
    """
    version = project_fixture.stored()["modified_at"]
    original = project_fixture.routes._batch_changes_only_the_scene
    fired = []

    def hooked(ops):
        if not fired:
            fired.append(True)
            _write_from_another_process(project_fixture.project_dir, "Writer B")
        return original(ops)

    monkeypatch.setattr(project_fixture.routes,
                        "_batch_changes_only_the_scene", hooked)

    with pytest.raises(ProjectVersionConflict) as raised:
        project_fixture.post(operations, if_match=version)
    assert raised.value.expected_modified_at == version
    assert raised.value.actual_modified_at != version, label
    assert load_project(
        project_fixture.project_dir).scenes[0].name == "Writer B", (
        "the competing write must survive")


def test_an_unguarded_no_op_no_longer_clobbers_a_concurrent_write(
        project_fixture, monkeypatch):
    """The half of the same window that L6 genuinely fixes.

    Without an `If-Match` there is no precondition to keep, so the skip is
    unguarded either way -- but before L6 the no-op still rewrote the whole
    document from a stale in-memory model and destroyed the competing write.
    """
    original = project_fixture.routes._batch_changes_only_the_scene
    fired = []

    def hooked(ops):
        if not fired:
            fired.append(True)
            _write_from_another_process(project_fixture.project_dir, "Writer B")
        return original(ops)

    monkeypatch.setattr(project_fixture.routes,
                        "_batch_changes_only_the_scene", hooked)

    status, payload = project_fixture.post(
        [{"type": "update_scene_fields", "fields": {"width": 640}}])
    assert status == 200
    assert payload["committed"] is False
    assert load_project(
        project_fixture.project_dir).scenes[0].name == "Writer B"
