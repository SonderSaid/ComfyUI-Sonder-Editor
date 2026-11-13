"""The ``PREPARES`` contract, enforced: what a preparable bridge's ``execute`` may do.

The cache pipeline runs each ``PREPARES`` bridge's own ``execute`` before
ComfyUI builds cache keys, and a bridge that stays scheduled runs it again
(``nodes/cache_policy.py``). That is only safe while ``execute``:

- publishes JSON-safe values under its declared ``publishes`` context keys only;
- writes nothing: no file, no project save, no run-record change;
- returns the same values and leaves the same context when called twice on the
  same project (the second, scheduled call).

Each class runs here on live and frozen-job fixtures with every write path
patched to fail. The V3 Reference nodes import through a stand-in ``comfy_api``
so the test runs on any Python; the real V3 call is covered by the executor
harness.
"""

import ast
import builtins
import copy
import importlib
import io as io_module
import json
import os
import shutil
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "bridge_prepares_testpkg"

sys.path.insert(0, str(ROOT))

from server.timeline_state import (  # noqa: E402
    Asset,
    ClipReference,
    GenerationJob,
    LaneConfig,
    PromptSection,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    REFERENCE_RECIPE_PRESETS,
    Scene,
    TimelineProject,
)


# --- A stand-in V3 io, enough for the Reference nodes' schemas --------------------------


class _Value:
    def __init__(self, id=None, **kwargs):
        self.id = id
        for key, value in kwargs.items():
            setattr(self, key, value)


class _NodeOutput:
    def __init__(self, *args):
        self.args = args


def _install_io(monkeypatch):
    def typed(name):
        return types.SimpleNamespace(Input=lambda id=None, **kw: _Value(id=id, socket_type=name, **kw),
                                     Output=lambda id=None, **kw: _Value(id=id, socket_type=name, **kw))

    io = types.SimpleNamespace(
        ComfyNode=type("ComfyNode", (), {}), Schema=_Value, Custom=typed, Int=typed("INT"),
        Float=typed("FLOAT"), String=typed("STRING"), Combo=typed("COMBO"), Image=typed("IMAGE"),
        Audio=typed("AUDIO"), NumberDisplay=types.SimpleNamespace(number="number"), NodeOutput=_NodeOutput)
    comfy = types.ModuleType("comfy_api")
    version = types.ModuleType("comfy_api.v0_0_2")
    version.io = io
    comfy.v0_0_2 = version
    monkeypatch.setitem(sys.modules, "comfy_api", comfy)
    monkeypatch.setitem(sys.modules, "comfy_api.v0_0_2", version)


@pytest.fixture
def modules(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    monkeypatch.setitem(sys.modules, "folder_paths",
                        types.SimpleNamespace(get_output_directory=lambda: str(tmp_path)))
    _install_io(monkeypatch)
    package = types.ModuleType(TEST_PACKAGE)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, TEST_PACKAGE, package)
    for name in list(sys.modules):
        if name.startswith(TEST_PACKAGE + "."):
            monkeypatch.delitem(sys.modules, name)
    importlib.invalidate_caches()
    load = lambda name: importlib.import_module(f"{TEST_PACKAGE}.{name}")  # noqa: E731
    reference = load("nodes.reference_bridge_v3")
    # The stand-in io builds no schema, so give the V3 nodes their real return types
    # (as ComfyUI's classproperties would): normalisation and the type checks need them.
    monkeypatch.setattr(reference.SonderReferenceSelector, "RETURN_TYPES",
                        ("SONDER_REFERENCE_SET", "INT", "FLOAT"), raising=False)
    monkeypatch.setattr(reference.SonderReferenceSelector, "RETURN_NAMES",
                        ("reference_set", "has_reference", "reference_strength"), raising=False)
    prompt_names = ("reference_prompt", "reference_names", *(f"p{i:02d}" for i in range(1, 17)))
    monkeypatch.setattr(reference.SonderReferencePromptBridge, "RETURN_TYPES", ("STRING",) * 18, raising=False)
    monkeypatch.setattr(reference.SonderReferencePromptBridge, "RETURN_NAMES", prompt_names, raising=False)
    return types.SimpleNamespace(
        policy=load("nodes.cache_policy"), relay=load("nodes.prompt_bridge"), masks=load("nodes.masks_bridge"),
        driver=load("nodes.driver_bridge"), reference=load("nodes.reference_bridge_v3"),
        sdk_runs=load("server.sdk_runs"), project_manager=load("server.project_manager"),
        atomic_io=load("server.atomic_io"))


# --- Writes fail loudly while a bridge runs ---------------------------------------------------


class WriteAttempted(AssertionError):
    pass


WRITING = ("save", "create", "write", "delete", "remove", "rename", "commit", "move", "trash")


def _state_of(module):
    """Every module-level container, as text: run-record state must not change."""
    return {name: repr(value) for name, value in vars(module).items()
            if not name.startswith("__") and isinstance(value, (dict, list, set))}


@pytest.fixture
def no_writes(modules, monkeypatch):
    """Arm a guard where every write path raises. ``sdk_runs`` is checked by state, not by name."""
    real_open = io_module.open
    real_os_open = os.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in "wax+"):
            raise WriteAttempted(f"open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            raise WriteAttempted(f"os.open({path!r})")
        return real_os_open(path, flags, *args, **kwargs)

    def refuse(name):
        def refused(*args, **kwargs):
            raise WriteAttempted(name)
        return refused

    def arm():
        # pathlib writes through io.open, tempfile through os.open.
        monkeypatch.setattr(builtins, "open", guarded_open)
        monkeypatch.setattr(io_module, "open", guarded_open)
        monkeypatch.setattr(os, "open", guarded_os_open)
        # Python 3.10's pathlib holds its own reference to io.open: guard Path directly.
        real_path_open = Path.open

        def guarded_path_open(self, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in "wax+"):
                raise WriteAttempted(f"Path.open({str(self)!r}, {mode!r})")
            return real_path_open(self, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_path_open)
        for name in ("write_text", "write_bytes", "touch", "mkdir", "unlink", "rename", "replace", "rmdir"):
            monkeypatch.setattr(Path, name, refuse(f"Path.{name}"))
        for name in ("replace", "rename", "remove", "unlink", "makedirs", "mkdir", "rmdir"):
            monkeypatch.setattr(os, name, refuse(f"os.{name}"))
        monkeypatch.setattr(shutil, "rmtree", refuse("shutil.rmtree"))
        monkeypatch.setattr(shutil, "move", refuse("shutil.move"))
        monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
        monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
        monkeypatch.setattr(modules.atomic_io, "atomic_replace", refuse("atomic_replace"))
        for name in dir(modules.project_manager):
            # Functions that start with a writing verb (the write lock itself is in memory).
            if name.lstrip("_").lower().startswith(WRITING) and callable(getattr(modules.project_manager, name)):
                monkeypatch.setattr(modules.project_manager, name, refuse(f"project_manager.{name}"))
        return _state_of(modules.sdk_runs)

    return arm


# --- Fixtures -----------------------------------------------------------------------------------


def _relay_live():
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=100, prompt="global style",
                  prompt_sections=[PromptSection(0, 30, prompt="walks"), PromptSection(30, 60, prompt="runs")])
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project._execution_context = {"scene_id": "scene-1", "context_start": 20, "context_end": 60,
                                  "queue_job_ref_id": ""}
    return project


def _relay_frozen():
    project = _relay_live()
    project.generation_queue = [GenerationJob(
        job_id="job-1", scene_id="scene-1", scene_prompt="frozen global",
        prompt_sections=[{"start_frame": 0, "end_frame": 40, "prompt": "frozen text"}],
        params={"snapshot_version": 1, "prompt_channel_labels": False})]
    project._execution_context["queue_job_ref_id"] = "job-1"
    return project


def _relay_frozen_on_disk(modules, tmp_path):
    """A frozen job saved and loaded back, so its snapshot hydrates from disk on first read."""
    project = _relay_frozen()
    project.project_dir = str(tmp_path / "sonder-projects" / "frozen")
    os.makedirs(project.project_dir, exist_ok=True)
    modules.project_manager.save_project(project)
    loaded = modules.project_manager.load_project(project.project_dir)
    assert getattr(loaded.generation_queue[0], "_frozen_unhydrated", False), "the fixture must hydrate on read"
    loaded._execution_context = dict(project._execution_context)
    return loaded


def _masks(frozen=False):
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=100)
    project = TimelineProject(project_dir="", name="Project", fps=24.0, scenes=[scene])
    project._execution_context = {"scene_id": "scene-1", "mask_start_frame": 12, "mask_end_frame": 36,
                                  "frame_count": 49, "frame_count_padding": 0, "queue_job_ref_id": ""}
    if frozen:
        project.generation_queue = [GenerationJob(job_id="job-1", scene_id="scene-1", scene_fps=30.0,
                                                  params={"snapshot_version": 1})]
        project._execution_context["queue_job_ref_id"] = "job-1"
    return project


def _driver(tmp_path, *, muted=False, lane_count=2):
    project_dir = tmp_path / "sonder-projects" / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    (project_dir / "media" / "driver.mp4").write_bytes(b"driver")
    scene = Scene(scene_id="scene-1", name="Scene 1", duration_frames=24, motion_driver_lane_count=lane_count)
    scene.motion_driver_lane_configs = [LaneConfig() for _ in range(lane_count)]
    scene.clips = [ClipReference(clip_id="driver-1", source_path=os.path.join("media", "driver.mp4"),
                                 timeline_start_frame=3, timeline_end_frame=8, source_in_frame=10,
                                 track_index=0, role="motion_driver", strength=0.42, muted=muted)]
    project = TimelineProject(project_dir=str(project_dir), name="Drivers", fps=24.0, resolution=(6, 4),
                              scenes=[scene])
    project._execution_context = {"scene_id": "scene-1", "context_start": 5, "context_end": 10,
                                  "frame_count": 5, "queue_job_id": "", "queue_job_ref_id": ""}
    return project


def _reference(tmp_path, *, override=""):
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "subject.png").write_bytes(b"png")
    definition = next(row for row in REFERENCE_RECIPE_PRESETS if row["media_kind"] == "image")
    recipe = ReferenceLaneRecipe(media_kind="image", recipe_id=definition["id"],
                                 recipe={"name": definition["name"], "hard": dict(definition["hard"]),
                                         "soft": dict(definition["soft"])})
    asset = Asset(asset_id="asset", name="Red", asset_type="image", path="media/subject.png", width=40, height=24)
    member = ReferenceMember(member_id="member", asset_id="asset", prompt="red subject")
    item = ReferenceItem(reference_item_id="item", lane_index=0, start_frame=10, end_frame=-1,
                         members=[{"entity_id": "entity", "member_id": "member"}], prompt_override=override)
    scene = Scene(scene_id="scene", duration_frames=60, width=64, height=48, reference_lane_count=1,
                  reference_lane_configs=[LaneConfig()], reference_lane_recipes=[recipe], reference_items=[item])
    project = TimelineProject(project_dir=str(tmp_path), project_id="project", resolution=(64, 48), scenes=[scene],
                              assets=[asset], references=[ReferenceEntity(reference_id="entity", name="Hero",
                                                                          members=[member])])
    project._execution_context = {"scene_id": "scene", "context_start": 12, "context_end": 30}
    return project


def _cases(modules, tmp_path):
    """(label, class, project, call) for every PREPARES class, live and frozen where it applies."""
    relay, masks, driver, reference = modules.relay, modules.masks, modules.driver, modules.reference

    def v1(cls, **kwargs):
        return lambda project: getattr(cls(), cls.FUNCTION)(project=project, **kwargs)

    def v3_selector(project):
        return tuple(reference.SonderReferenceSelector.execute(project=project, reference_lanes="0").args)

    def v3_prompt(project):
        ref = reference.SonderReferenceSelector.execute(project=project, reference_lanes="0").args[0]
        return tuple(reference.SonderReferencePromptBridge.execute(reference_set=ref).args)

    return [
        ("relay live", relay.SonderPromptRelayBridge, _relay_live(), v1(relay.SonderPromptRelayBridge)),
        ("relay frozen", relay.SonderPromptRelayBridge, _relay_frozen(), v1(relay.SonderPromptRelayBridge)),
        ("relay frozen from disk", relay.SonderPromptRelayBridge, _relay_frozen_on_disk(modules, tmp_path / "g"),
         v1(relay.SonderPromptRelayBridge)),
        ("masks edit", masks.SonderMasksBridge, _masks(),
         v1(masks.SonderMasksBridge, edit_video=True, edit_audio=True, unique_id="7")),
        ("masks freeze frozen", masks.SonderMasksBridge, _masks(frozen=True),
         v1(masks.SonderMasksBridge, edit_video=True, edit_audio=False, unique_id="7")),
        ("driver present", driver.SonderDriverSelector, _driver(tmp_path / "a"), v1(driver.SonderDriverSelector)),
        ("driver muted", driver.SonderDriverSelector, _driver(tmp_path / "b", muted=True),
         v1(driver.SonderDriverSelector)),
        ("driver absent lane", driver.SonderDriverSelector, _driver(tmp_path / "c"),
         v1(driver.SonderDriverSelector, driver_lane_index=1)),
        ("reference selector", reference.SonderReferenceSelector, _reference(tmp_path / "d"), v3_selector),
        ("reference prompt", reference.SonderReferencePromptBridge, _reference(tmp_path / "e"), v3_prompt),
        ("reference prompt override", reference.SonderReferencePromptBridge,
         _reference(tmp_path / "f", override="custom words"), v3_prompt),
    ]


def _declared_in_source():
    """Every class under ``nodes/`` that assigns SONDER_CACHE_PREPARE, by reading the source."""
    found = set()
    for path in (ROOT / "nodes").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and any(
                    isinstance(item, ast.Assign) and any(getattr(t, "id", "") == "SONDER_CACHE_PREPARE"
                                                         for t in item.targets) for item in node.body):
                found.add(node.name)
    return found


def test_every_prepares_class_is_covered_here(modules, tmp_path):
    covered = {cls.__name__ for _label, cls, _project, _call in _cases(modules, tmp_path)}
    assert covered == _declared_in_source()
    assert all(len(cls.RETURN_TYPES) > 0 for _label, cls, _project, _call in _cases(modules, tmp_path))


def _comparable(value):
    """Values compared across two calls: tensors by content, everything else as is."""
    torch = sys.modules.get("torch")
    if torch is not None and isinstance(value, torch.Tensor):
        return ("tensor", tuple(value.shape), value.flatten().tolist())
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items() if key != "project"}
    if isinstance(value, (list, tuple)):
        return [_comparable(item) for item in value]
    return value


def test_execute_publishes_only_declared_json_safe_keys_writes_nothing_and_repeats_exactly(
        modules, tmp_path, no_writes):
    cases = _cases(modules, tmp_path)
    state = no_writes()
    for label, cls, project, call in cases:
        publishes = set(modules.policy.prepare_policy(cls).publishes)
        before = copy.deepcopy(project._execution_context)
        first = call(project)
        after_first = copy.deepcopy(project._execution_context)
        changed = {key for key in set(before) | set(after_first) if before.get(key) != after_first.get(key)}
        assert changed <= publishes, (label, changed - publishes)
        for key in changed:
            json.dumps(after_first[key])  # JSON-safe: it reaches the take's generation params
        # The second, scheduled call of a bridge that stays scheduled.
        second = call(project)
        assert _comparable(second) == _comparable(first), label
        assert project._execution_context == after_first, label
        # Exact canonical types on every EXACT output.
        for slot in modules.policy.exact_slots(cls):
            assert type(first[slot]) is modules.policy.LITERAL_TYPES[cls.RETURN_TYPES[slot]], (label, slot)
        # The fixture exercised the path it is named for, not an empty one.
        assert EXPECTED[label](first, after_first), (label, first[:4], after_first)
    # No run-record state changed: no result, failure, lease, token, receipt or release.
    assert _state_of(modules.sdk_runs) == state


EXPECTED = {
    "relay live": lambda out, ctx: out[0] == "global style" and ctx["prompt_relay"]["source"] == "live",
    "relay frozen": lambda out, ctx: out[0] == "frozen global" and ctx["prompt_relay"]["source"] == "snapshot",
    "relay frozen from disk": lambda out, ctx: out[0] == "frozen global" and ctx["prompt_relay"]["source"] == "snapshot",
    "masks edit": lambda out, ctx: out[:4] == (0.5, 1.5, 0.5, 1.5) and "7" in ctx["masks_bridge_by_node"],
    "masks freeze frozen": lambda out, ctx: out[:4] == (0.4, 1.2, 0.4, 0.4) and ctx["masks_bridge"]["edit_audio"] is False,
    "driver present": lambda out, ctx: out[1] == 1 and ctx["driver_selector"]["has_driver"] == 1,
    "driver muted": lambda out, ctx: out[1] == 0 and ctx["driver_selector"]["has_driver"] == 0,
    "driver absent lane": lambda out, ctx: out[1] == 0 and ctx["driver_selector"]["lane_index"] == 1,
    "reference selector": lambda out, ctx: type(out[1]) is int and out[1] == 1 and type(out[2]) is float and out[2] == 1.0,
    "reference prompt": lambda out, ctx: "red subject" in out[0] and out[1] == "Hero",
    "reference prompt override": lambda out, ctx: "custom words" in out[0],
}


def test_the_write_guard_catches_a_write(modules, tmp_path, no_writes):
    # The guard itself must be able to fail, or the test above proves nothing.
    import tempfile
    state = no_writes()
    with pytest.raises(WriteAttempted):
        open(tmp_path / "x.txt", "w")
    with pytest.raises(WriteAttempted):
        (tmp_path / "y.txt").write_text("y")
    with pytest.raises(WriteAttempted):
        tempfile.mkstemp(dir=str(tmp_path))
    with pytest.raises(WriteAttempted):
        modules.project_manager.create_project("x", base_dir=str(tmp_path))
    modules.sdk_runs.forget_token("never-registered")  # a no-op: the state check is what catches a change
    modules.sdk_runs.request_cancel("p-x")
    assert _state_of(modules.sdk_runs) != state
