"""The Editor's deferred media: every invalidation case its ``DEFERS`` declaration registers.

``nodes/editor_media.py`` gives the Editor's render and audio mix an identity
before they run: a recipe of every value they read plus the full content of
every file they name. A false hit would hand a consumer stale frames, so each
``DEFERRED_INVALIDATION`` case is checked here on real media
(``tests/helpers/editor_media_fixtures.py``), for both slots:

- ``inputs_reach_identity``: every field of the clip, audio track, scene, asset
  and lane records the render or mix can read is perturbed (integer, fractional,
  wrong-type and ``None`` values), on a project loaded from disk; whenever the
  stock render or mix (the live reader, not the recipe) changes or raises, the
  recipe changes or there is none. A negative control (a recipe that drops clip
  opacity) must fail the same check.
- ``same_path_content`` and ``unchanged_content``: a different file at the same
  path with the same size and a copied mtime changes the token; identical bytes
  rewritten keep it.
- ``recipe_only``: the materializer runs with every project, scene, path and
  colour-probe reader patched to raise, and a project changed after resolve
  changes nothing. A negative control (a materializer that reads the live
  scene) must fail.
- Source coverage: every project file the materializer opens through OpenCV,
  ``open``, mutagen or an ffmpeg/ffprobe subprocess is in the recipe's sources.
- ``PendingMedia``: once per prompt; the failure contract (an interrupt, a
  changed source, a raise) and the lease condition on marking a job failed.

The cases are written for the Editor's two plans; the registry test pins the
canonical list of ``DEFERS`` outputs, so a second deferring class must add its
own cases before it can pass there.
"""

import builtins
import copy
import dataclasses
import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "editor_media_cases_testpkg"
sys.path.insert(0, str(ROOT / "tests" / "helpers"))


def load(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_output_directory=lambda: str(tmp_path), get_user_directory=lambda: str(tmp_path / "user"),
        get_temp_directory=lambda: str(tmp_path / "temp")))
    pkg = types.ModuleType(TEST_PACKAGE)
    pkg.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, TEST_PACKAGE, pkg)
    for name in list(sys.modules):
        if name.startswith(TEST_PACKAGE + "."):
            monkeypatch.delitem(sys.modules, name)
    importlib.invalidate_caches()
    get = lambda name: importlib.import_module(f"{TEST_PACKAGE}.{name}")  # noqa: E731
    m = types.SimpleNamespace(
        timeline_state=get("server.timeline_state"), project_manager=get("server.project_manager"),
        media_helpers=get("server.media_helpers"), editor_node=get("nodes.editor_node"),
        editor_media=get("nodes.editor_media"), renderer=get("server.timeline_renderer"),
        audio=get("server.audio_pipeline"), cache_identity=get("nodes.cache_identity"),
        cache_policy=get("nodes.cache_policy"), sdk_runs=get("server.sdk_runs"),
        render_cache=get("server.render_cache"))
    monkeypatch.setattr(get("server.external_links"), "is_enabled", lambda: False)
    try:
        ffmpeg = m.media_helpers.get_ffmpeg_path()
    except RuntimeError:
        pytest.skip("ffmpeg is required for the Editor media fixtures")
    return m, ffmpeg


WINDOW = (6, 30)


def project_of(m, tmp_path, ffmpeg, name="cases"):
    import editor_media_fixtures as fixtures

    folder = fixtures.build_project(m, tmp_path, ffmpeg, name=name)
    return str(tmp_path / "sonder-projects" / folder)


def frames_plan(m, project, scene, use_cache=False, window=WINDOW):
    start, end = window
    return m.editor_media.frames_plan(
        project, scene, start, end, use_cache=use_cache, padding_target=0,
        materialize=lambda request: m.renderer.render_scene_frames(
            project, scene, start, end, use_cache=use_cache, request=request))


def audio_plan(m, project, scene, window=WINDOW):
    start, end = window
    return m.editor_media.audio_plan(
        project, scene, start, end, padding_target=0, pad_fps=24.0,
        materialize=lambda request: m.editor_node.SonderEditor()._load_scene_audio(
            project, scene, start, end, request=request))


def stock(m, kind, project, scene, window=WINDOW):
    """The live reader, exactly as before Phase 4b: what the recipe must account for."""
    start, end = window
    try:
        if kind == "frames":
            value = m.renderer.render_scene_frames(project, scene, start, end, use_cache=False)
            return ("ok", value.contiguous().numpy().tobytes())
        value = m.editor_node.SonderEditor()._load_scene_audio(project, scene, start, end)
        return ("ok", value["sample_rate"], value["waveform"].contiguous().numpy().tobytes())
    except Exception as exc:  # noqa: BLE001 - a raise is an output too
        return ("raised", type(exc).__name__)


def identity_of(plan):
    return plan.recipe_json if plan.recipe is not None else None


# --- inputs_reach_identity -------------------------------------------------------------------


def _values_for(value):
    """Perturbations of a field: a changed value of its type, a fractional and a wrong-type one, None."""
    out = []
    if isinstance(value, bool):
        out += [not value]
    elif isinstance(value, int):
        out += [value + 1, value + 0.5, float(value), str(value), None]
    elif isinstance(value, float):
        out += [value * 0.5 + 0.25, value + 1.0, int(value), str(value), None]
    elif isinstance(value, str):
        out += [value + "x", "", None]
    return out


def perturbations(m, project_dir):
    """``(label, change(project, scene))`` for every scalar field the render or the mix can read."""
    ts = m.timeline_state
    project = m.project_manager.load_project(project_dir)
    scene = project.scenes[0]
    changes = []

    def scalar_fields(obj):
        return [f.name for f in dataclasses.fields(obj)
                if isinstance(getattr(obj, f.name), (bool, int, float, str))]

    for index, clip in enumerate(scene.clips):
        for name in scalar_fields(clip):
            for value in _values_for(getattr(clip, name)):
                changes.append((f"clip[{index}].{name}={value!r}",
                                lambda p, s, i=index, n=name, v=value: setattr(s.clips[i], n, v)))
    for index, track in enumerate(scene.audio_tracks):
        for name in scalar_fields(track):
            for value in _values_for(getattr(track, name)):
                changes.append((f"audio[{index}].{name}={value!r}",
                                lambda p, s, i=index, n=name, v=value: setattr(s.audio_tracks[i], n, v)))
    for name in ("duration_frames", "fps", "width", "height"):
        for value in _values_for(getattr(scene, name)):
            changes.append((f"scene.{name}={value!r}", lambda p, s, n=name, v=value: setattr(s, n, v)))
    for configs in ("video_lane_configs", "audio_lane_configs"):
        for index in range(len(getattr(scene, configs))):
            changes.append((f"scene.{configs}[{index}].hidden flipped",
                            lambda p, s, c=configs, i=index: setattr(getattr(s, c)[i], "hidden",
                                                                     not getattr(s, c)[i].hidden)))
    for index, asset in enumerate(project.assets):
        for name in ("fps", "frame_count", "path", "color_space", "color_range", "color_primaries"):
            if not hasattr(asset, name):
                continue
            for value in _values_for(getattr(asset, name)):
                changes.append((f"asset[{index}].{name}={value!r}",
                                lambda p, s, i=index, n=name, v=value: setattr(p.assets[i], n, v)))
    for name, value in (("fps", 30.0), ("fps", 24), ("resolution", (20, 14))):
        changes.append((f"project.{name}={value!r}", lambda p, s, n=name, v=value: setattr(p, n, v)))
    return changes


def violations(m, project_dir, kinds=("frames", "audio"), recipe_hook=None):
    """Perturbations whose stock output changed while the recipe stayed the same."""
    base_project = m.project_manager.load_project(project_dir)
    base_scene = base_project.scenes[0]
    found = []
    for kind in kinds:
        build = frames_plan if kind == "frames" else audio_plan
        baseline_plan = build(m, base_project, base_scene)
        if recipe_hook:
            recipe_hook(baseline_plan)
        baseline_recipe = identity_of(baseline_plan)
        assert baseline_recipe is not None, f"the fixture must give {kind} a recipe"
        baseline = stock(m, kind, base_project, base_scene)
        assert baseline[0] == "ok"
        for label, change in perturbations(m, project_dir):
            project = m.project_manager.load_project(project_dir)
            scene = project.scenes[0]
            try:
                change(project, scene)
            except Exception:  # noqa: BLE001 - a value the record refuses is not a reachable input
                continue
            try:
                plan = build(m, project, scene)
                if recipe_hook:
                    recipe_hook(plan)
                recipe = identity_of(plan)
            except Exception as exc:  # noqa: BLE001 - resolve itself raising stops the run: no false hit
                recipe = f"resolve raised {type(exc).__name__}"
            output = stock(m, kind, project, scene)
            if output != baseline and recipe == baseline_recipe:
                found.append(f"{kind}: {label} changed the output ({output[0]}) but not the recipe")
    return found


def test_inputs_reach_identity_for_every_field_on_a_project_from_disk(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project_dir = project_of(m, tmp_path, ffmpeg)
    assert violations(m, project_dir) == []


def test_inputs_reach_identity_negative_control_a_recipe_without_opacity_fails(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project_dir = project_of(m, tmp_path, ffmpeg)

    def drop_opacity(plan):
        if plan.recipe is None:
            return
        for clip in plan.recipe["clips"]:
            clip[4] = 1.0
        plan.recipe_json = m.cache_identity.canonical(plan.recipe)

    found = violations(m, project_dir, kinds=("frames",), recipe_hook=drop_opacity)
    assert any(".opacity=" in item for item in found), found


def test_fractional_and_mistyped_frames_give_no_identity_and_render_as_today(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project = m.project_manager.load_project(project_of(m, tmp_path, ffmpeg))
    scene = project.scenes[0]
    scene.clips[1].timeline_start_frame = 6.5
    scene.audio_tracks[0].source_in_frame = None
    frames, audio = frames_plan(m, project, scene), audio_plan(m, project, scene)
    assert (frames.recipe, frames.reason) == (None, "recipe_inexact")
    assert (audio.recipe, audio.reason) == (None, "recipe_inexact")
    pending = m.editor_media.PendingMedia()
    pending.add(1, frames)
    assert pending.identify(1) == ("", "recipe_inexact")
    assert pending.value(1).contiguous().numpy().tobytes() == stock(m, "frames", project, scene)[1]


def test_an_integral_float_frame_and_an_integer_opacity_are_the_same_recipe(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project = m.project_manager.load_project(project_of(m, tmp_path, ffmpeg))
    scene = project.scenes[0]
    assert type(scene.clips[0].opacity) is int  # loaded from JSON as written: 1, not 1.0
    before = frames_plan(m, project, scene).recipe_json
    scene.clips[0].opacity = 1.0
    scene.clips[0].timeline_end_frame = float(scene.clips[0].timeline_end_frame)
    assert frames_plan(m, project, scene).recipe_json == before


# --- same_path_content, unchanged_content -------------------------------------------------------


def _token(m, project_dir, kind):
    project = m.project_manager.load_project(project_dir)
    scene = project.scenes[0]
    pending = m.editor_media.PendingMedia()
    pending.add(0, (frames_plan if kind == "frames" else audio_plan)(m, project, scene))
    token, reason = pending.identify(0)
    assert token, reason
    return token


@pytest.mark.parametrize("kind, rel", [("frames", "media/b.mkv"), ("audio", "media/music.wav")])
def test_same_path_content_changes_the_token_and_unchanged_content_keeps_it(tmp_path, monkeypatch, kind, rel):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project_dir = project_of(m, tmp_path, ffmpeg)
    path = Path(project_dir) / rel
    first = _token(m, project_dir, kind)
    data = path.read_bytes()
    stat = os.stat(path)
    path.write_bytes(data)  # identical bytes, a new mtime
    assert _token(m, project_dir, kind) == first
    changed = bytearray(data)
    changed[len(changed) // 2] ^= 0x01  # same size, one bit
    path.write_bytes(bytes(changed))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # a copied mtime
    assert _token(m, project_dir, kind) != first


def test_tokens_are_domain_separated_and_salted_like_content_tokens(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project_dir = project_of(m, tmp_path, ffmpeg)
    frames, audio = _token(m, project_dir, "frames"), _token(m, project_dir, "audio")
    content = importlib.import_module(f"{TEST_PACKAGE}.nodes.cache_content")
    assert content.is_token(frames) and content.is_token(audio) and frames != audio


def test_sources_over_the_cap_get_no_identity(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project = m.project_manager.load_project(project_of(m, tmp_path, ffmpeg))
    plan = frames_plan(m, project, project.scenes[0])
    plan.max_bytes = 10
    pending = m.editor_media.PendingMedia()
    pending.add(0, plan)
    assert pending.identify(0) == ("", "source_too_large")


# --- recipe_only -----------------------------------------------------------------------------------


READERS = [
    ("server.timeline_renderer", ["_scene_resolution", "_visible_render_clips", "_prepare_sources",
                                  "effective_scene_fps", "resolve_source_color_interpretation",
                                  "resolve_existing_project_path", "hidden_lane_indexes"]),
    ("server.audio_pipeline", ["audible_sources", "effective_scene_fps", "resolve_existing_project_path",
                               "hidden_lane_indexes"]),
    ("server.media_helpers", ["resolve_source_color_interpretation"]),
    ("server.project_manager", ["load_project"]),
]


def _patch_readers(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a materializer read the project, the scene, a path or a probe")

    for module, names in READERS:
        loaded = importlib.import_module(f"{TEST_PACKAGE}.{module}")
        for name in names:
            if hasattr(loaded, name):
                monkeypatch.setattr(loaded, name, refuse)


@pytest.mark.parametrize("kind", ["frames", "audio"])
@pytest.mark.parametrize("use_cache", [False, True])
def test_recipe_only_materialization_reads_nothing_live(tmp_path, monkeypatch, kind, use_cache):
    if kind == "audio" and use_cache:
        pytest.skip("the mix has no render cache")
    m, ffmpeg = load(tmp_path, monkeypatch)
    project = m.project_manager.load_project(project_of(m, tmp_path, ffmpeg))
    scene = project.scenes[0]
    baseline = stock(m, kind, project, scene)
    plan = frames_plan(m, project, scene, use_cache=use_cache) if kind == "frames" else audio_plan(m, project, scene)
    pending = m.editor_media.PendingMedia()
    pending.add(0, plan)
    token, reason = pending.identify(0)
    assert token, reason
    # Resolve is over: the live project and scene change, every live reader refuses.
    for clip in scene.clips:
        clip.opacity, clip.source_in_frame = 0.25, 0
    for track in scene.audio_tracks:
        track.volume = 0.1
    scene.duration_frames = 3
    project.assets = []
    _patch_readers(monkeypatch)
    value = pending.value(0)
    produced = (("ok", value.contiguous().numpy().tobytes()) if kind == "frames"
                else ("ok", value["sample_rate"], value["waveform"].contiguous().numpy().tobytes()))
    assert produced == baseline


def test_recipe_only_negative_control_a_materializer_reading_the_live_scene_fails(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project = m.project_manager.load_project(project_of(m, tmp_path, ffmpeg))
    scene = project.scenes[0]
    start, end = WINDOW
    plan = m.editor_media.frames_plan(
        project, scene, start, end, use_cache=False, padding_target=0,
        materialize=lambda _request: m.renderer.render_scene_frames(project, scene, start, end, use_cache=False))
    pending = m.editor_media.PendingMedia()
    pending.add(0, plan)
    _patch_readers(monkeypatch)
    with pytest.raises(AssertionError, match="a materializer read"):
        pending.value(0)


# --- Source coverage ------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["frames", "audio"])
def test_every_file_a_materialization_opens_is_in_its_recipe(tmp_path, monkeypatch, kind):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project_dir = project_of(m, tmp_path, ffmpeg)
    project = m.project_manager.load_project(project_dir)
    scene = project.scenes[0]
    plan = frames_plan(m, project, scene, use_cache=True) if kind == "frames" else audio_plan(m, project, scene)
    sources = {os.path.normcase(os.path.realpath(path)) for path in plan.recipe["sources"]}
    opened = []
    project_root = os.path.normcase(os.path.realpath(project_dir))

    def note(path):
        if isinstance(path, (str, os.PathLike)):
            real = os.path.normcase(os.path.realpath(os.fspath(path)))
            if real.startswith(project_root) and os.sep + "cache" + os.sep not in real:
                opened.append(real)

    import cv2

    real_capture, real_open, real_run, real_popen = cv2.VideoCapture, builtins.open, subprocess.run, subprocess.Popen
    monkeypatch.setattr(cv2, "VideoCapture", lambda path, *a: (note(path), real_capture(path, *a))[1])
    monkeypatch.setattr(builtins, "open", lambda path, *a, **k: (note(path), real_open(path, *a, **k))[1])

    def watch_args(args):
        for item in (args if isinstance(args, (list, tuple)) else [args]):
            note(item)

    monkeypatch.setattr(subprocess, "run", lambda args, *a, **k: (watch_args(args), real_run(args, *a, **k))[1])
    monkeypatch.setattr(subprocess, "Popen", type("WatchedPopen", (real_popen,), {
        "__init__": lambda self, args, *a, **k: (watch_args(args), real_popen.__init__(self, args, *a, **k))[1]}))
    try:
        import mutagen
    except ImportError:  # the probe falls back to ffprobe, whose arguments are watched above
        mutagen = None
    if mutagen is not None:
        real_file = mutagen.File
        monkeypatch.setattr(mutagen, "File", lambda path, *a, **k: (note(path), real_file(path, *a, **k))[1])
    pending = m.editor_media.PendingMedia()
    pending.add(0, plan)
    pending.value(0)
    assert opened, "the materialization opened no project file"
    assert set(opened) <= sources, sorted(set(opened) - sources)


# --- PendingMedia ---------------------------------------------------------------------------------------


def test_pending_media_materializes_once_and_fills_only_a_marker(tmp_path, monkeypatch):
    m, _ffmpeg = load(tmp_path, monkeypatch)
    calls = []
    plan = m.editor_media.MediaPlan("frames", object(), None, "recipe_inexact",
                                    lambda request: calls.append(request) or "value", lambda value: True, 0)
    key = "k-once"
    prompt = {"1": {"class_type": "SonderEditor", "inputs": {}}}
    m.sdk_runs.open_release(key, "p-once", prompt)
    try:
        marker = m.editor_media.DEFERRED
        m.sdk_runs.remember_result(key, "1#0", ("p", marker, "g", marker))
        accepted = []
        pending = m.editor_media.PendingMedia(key=key, call_key="1#0", deferred=True,
                                              on_accept=lambda slot, value: accepted.append(slot))
        pending.add(1, plan)
        pending.add(3, plan)
        assert pending.value(1) == "value" and pending.value(1) == "value" and len(calls) == 1
        assert pending.accept(3, "cached") and accepted == [1, 3]
        assert m.sdk_runs.first_result(key, "1#0") == (True, ("p", "value", "g", "cached"))
        # A filled slot is never replaced.
        assert not m.sdk_runs.fill_result(key, "1#0", 1, "other", marker)
        m.sdk_runs.remember_pending(key, "1#0", pending)
        assert m.sdk_runs.pending_for(key, "1#0") is pending
        record = m.sdk_runs.close_release(key)
        assert "pending" not in record
    finally:
        m.sdk_runs.close_release(key)


def test_pending_media_tells_its_own_failure_from_pipeline_code(tmp_path, monkeypatch):
    m, _ffmpeg = load(tmp_path, monkeypatch)

    def broken(_request):
        raise RuntimeError("render failed")

    pending = m.editor_media.PendingMedia()
    pending.add(1, m.editor_media.MediaPlan("frames", None, None, "x", broken, lambda value: True, 0))
    with pytest.raises(RuntimeError) as raised:
        pending.value(1)
    assert pending.failed_with(raised.value) and not pending.failed_with(RuntimeError("elsewhere"))
    assert pending.defers(1)


class _Job:
    def __init__(self):
        self.job_id, self.status, self.error = "job-1", "running", ""


def test_a_late_failure_marks_a_job_failed_only_while_this_run_leases_it(tmp_path, monkeypatch):
    m, _ffmpeg = load(tmp_path, monkeypatch)
    editor = m.editor_node.SonderEditor()
    marked = []
    monkeypatch.setattr(editor, "_mark_queue_job_failed", lambda proj, job, message: marked.append(message))
    proj = types.SimpleNamespace(project_dir=str(tmp_path / "p"))
    job = _Job()
    key = "k-lease"
    m.sdk_runs.open_release(key, "p-lease", {})
    try:
        # Not leased (a save already settled it, or another run holds it): untouched.
        editor._materialization_failed(proj, job, True, key, RuntimeError("late"))
        m.sdk_runs.open_release("k-other", "p-other", {})
        m.sdk_runs.take_lease("k-other", proj.project_dir, job.job_id)
        editor._materialization_failed(proj, job, True, key, RuntimeError("late"))
        assert marked == []
        m.sdk_runs.take_lease(key, proj.project_dir, job.job_id)
        # An interrupt and a changed source never mark it.
        editor._materialization_failed(proj, job, True, key, KeyboardInterrupt())
        editor._materialization_failed(proj, job, True, key, m.cache_identity.SourceChanged("changed"))
        editor._materialization_failed(proj, job, False, key, RuntimeError("not consumed"))
        assert marked == []
        editor._materialization_failed(proj, job, True, key, RuntimeError("late render failure"))
        assert marked == ["late render failure"]
    finally:
        m.sdk_runs.close_release(key)
        m.sdk_runs.close_release("k-other")


def test_a_source_changed_between_hash_and_render_fails_and_caches_nothing(tmp_path, monkeypatch):
    m, ffmpeg = load(tmp_path, monkeypatch)
    project_dir = project_of(m, tmp_path, ffmpeg)
    project = m.project_manager.load_project(project_dir)
    pending = m.editor_media.PendingMedia()
    pending.add(0, frames_plan(m, project, project.scenes[0]))
    assert pending.identify(0)[0]
    with open(Path(project_dir) / "media" / "b.mkv", "ab") as handle:
        handle.write(b"\0" * 8)
    with pytest.raises(m.cache_identity.SourceChanged, match="changed while this run was reading it"):
        pending.value(0)
    assert pending.defers(0) and pending.failure_id != 0


def test_the_deferred_declarations_are_the_registered_cases(tmp_path, monkeypatch):
    m, _ffmpeg = load(tmp_path, monkeypatch)
    editor = m.editor_node.SonderEditor
    assert m.cache_policy.deferred_slots(editor) == (editor.OUTPUT_SLOTS["rendered_frames"],
                                                     editor.OUTPUT_SLOTS["audio"])
    for name in ("rendered_frames", "audio"):
        assert editor.SONDER_CACHE_INVALIDATION[name] == m.cache_policy.DEFERRED_INVALIDATION


def _deferred_editor_run(m, tmp_path, ffmpeg, monkeypatch, **execute_kwargs):
    """The real Editor's early run with deferral on: its PendingMedia and its result."""
    import editor_media_fixtures as fixtures

    name = fixtures.build_project(m, tmp_path, ffmpeg, name="recipe-window")
    kept = []
    monkeypatch.setattr(m.editor_node.editor_media, "deferral_requested", lambda unique_id: True)
    monkeypatch.setattr(m.editor_node.sdk_runs, "remember_pending",
                        lambda key, call_key, pending: kept.append(pending) or True)
    result = m.editor_node.SonderEditor().execute(
        project=name, project_name="", fps=24.0, width=16, height=12, scene_id=fixtures.SCENE,
        render_queue_active=False, **execute_kwargs)
    assert kept, "the Editor kept no deferred media"
    return kept[0], result


def test_the_editors_materializer_renders_the_recipes_window_and_padding(tmp_path, monkeypatch):
    """The Editor's own closures take the window, cache flag and padding from the recipe, never their locals."""
    m, ffmpeg = load(tmp_path, monkeypatch)
    pending, result = _deferred_editor_run(m, tmp_path, ffmpeg, monkeypatch, selection_start=8, selection_end=20,
                                           render_cache_max_bytes=0)
    slots = m.editor_node.SonderEditor.OUTPUT_SLOTS
    frames_slot, audio_slot = slots["rendered_frames"], slots["audio"]
    assert m.editor_media.is_deferred(result[frames_slot]) and m.editor_media.is_deferred(result[audio_slot])
    project = m.project_manager.load_project(str(tmp_path / "sonder-projects" / "recipe-window"))
    scene = project.scenes[0]
    for slot in (frames_slot, audio_slot):
        plan = pending.plans[slot]
        plan.recipe.update(start=2, end=9, padding_target=11)
        if plan.kind == "audio":
            plan.recipe["pad_fps"] = 24.0
        plan.recipe_json = m.cache_identity.canonical(plan.recipe)
    frames = pending.value(frames_slot)
    expected = m.renderer.render_scene_frames(project, scene, 2, 9, use_cache=False)
    expected = m.editor_node.SonderEditor._pad_image_batch_to_frame_count(expected, 11)
    assert frames.contiguous().numpy().tobytes() == expected.contiguous().numpy().tobytes()
    audio = pending.value(audio_slot)
    stock = m.editor_node.SonderEditor()._load_scene_audio(project, scene, 2, 9)
    stock = m.editor_node.SonderEditor._pad_audio_to_frame_count(stock, 11, 24.0)
    assert audio["sample_rate"] == stock["sample_rate"]
    assert audio["waveform"].contiguous().numpy().tobytes() == stock["waveform"].contiguous().numpy().tobytes()

