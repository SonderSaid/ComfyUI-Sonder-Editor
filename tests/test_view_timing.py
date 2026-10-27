"""Output Views Phase 3B: authored timing and the run content record.

- ``clean_timing`` (the reader) and its use in the manifest, descriptors, the
  Sonder App sidecar and Controls validation;
- the content record the Editor stores at execution, built from the inputs it
  consumed (a queued snapshot run included), and what moves it;
- holds keep their run's Editor context, through the hold route.

``parity_cases()`` is shared with ``test_view_timing_js.py``, which checks the
browser's ``contentStatus`` against the records built here.
"""

import copy
import json

import pytest

from server import control_descriptors as cd, control_manifest as cm, control_views as cv, view_timing as vt
from server.timeline_state import Scene

FPS, W, H = 24.0, 640, 360


# ── Timing ───────────────────────────────────────────────────────────────────

TIMING_CASES = [
    ({"mode": "editor_window", "anchor": "render_start"}, {"mode": "editor_window", "anchor": "render_start", "source_offset": 0, "fps": "editor"}),
    ({"mode": "editor_window", "anchor": "generation_start", "source_offset": 3.0, "fps": 12.0},
     {"mode": "editor_window", "anchor": "generation_start", "source_offset": 3, "fps": 12}),
    ({"mode": "editor_window", "anchor": "render_start", "fps": 23.976}, {"mode": "editor_window", "anchor": "render_start", "source_offset": 0, "fps": 23.976}),
    ({"mode": "editor_window", "anchor": "render_start", "fps": 240}, {"mode": "editor_window", "anchor": "render_start", "source_offset": 0, "fps": 240}),
    (None, None), ([], None), ("x", None),
    ({"mode": "other", "anchor": "render_start"}, None),
    ({"mode": "editor_window", "anchor": "end"}, None),
    ({"mode": "editor_window", "anchor": "render_start", "source_offset": -1}, None),
    ({"mode": "editor_window", "anchor": "render_start", "source_offset": 1.5}, None),
    ({"mode": "editor_window", "anchor": "render_start", "source_offset": True}, None),
    ({"mode": "editor_window", "anchor": "render_start", "source_offset": None}, None),
    ({"mode": "editor_window", "anchor": "render_start", "source_offset": 1000001}, None),
    ({"mode": "editor_window", "anchor": "render_start", "fps": 0}, None),
    ({"mode": "editor_window", "anchor": "render_start", "fps": -1}, None),
    ({"mode": "editor_window", "anchor": "render_start", "fps": 241}, None),
    ({"mode": "editor_window", "anchor": "render_start", "fps": "24"}, None),
    ({"mode": "editor_window", "anchor": "render_start", "fps": True}, None),
    ({"mode": "editor_window", "anchor": "render_start", "fps": None}, None),
]


@pytest.mark.parametrize("raw, expected", TIMING_CASES)
def test_clean_timing(raw, expected):
    assert vt.clean_timing(raw) == expected


def _relay_prompt(views):
    manifest = {"v": 1, "bindings": {}, "views": {"v": 1, "items": views}}
    return {
        "1": {"class_type": cm.RELAY_CLASS_TYPE, "inputs": {"manifest": json.dumps(manifest)}},
        "9": {"class_type": "PreviewImage", "inputs": {}},
    }


def test_manifest_reads_timing_tolerantly_and_descriptors_carry_it():
    timing = {"mode": "editor_window", "anchor": "render_start", "source_offset": 2, "fps": "editor"}
    views, error = cm.read_views({"v": 1, "items": {
        "a": {"label": "A", "order": 0, "timing": timing},
        "b": {"label": "B", "order": 1, "timing": {"mode": "nope"}},
    }})
    assert error is None and views["a"]["timing"] == timing and "timing" not in views["b"]
    row = cv.describe_one({"9": {"class_type": "PreviewImage"}}, "a",
                          {**views["a"], "status": "ok", "path": "9", "class_type": "PreviewImage"},
                          class_lookup=lambda c: type("N", (), {"OUTPUT_NODE": True}))
    assert row["timing"] == timing
    assert cv.describe_one({}, "b", views["b"])["timing"] is None


def test_timing_is_a_presentation_field_with_null_to_remove():
    descriptor = {"source": "view", "view_id": "a", "origin": "authored"}
    good = {"mode": "editor_window", "anchor": "generation_start", "source_offset": 0, "fps": 30}
    assert cv.coerce_presentation(descriptor, "timing", good, "canvas") == good
    assert cv.coerce_presentation(descriptor, "timing", None, "canvas") is None
    with pytest.raises(cd.ControlValueError):
        cv.coerce_presentation(descriptor, "timing", {"mode": "editor_window", "anchor": "render_start", "fps": 999}, "canvas")
    assert "view:a:timing" in cv.table_entries({"views": [descriptor]})


def test_sonder_app_copy_keeps_timing_on_rename_and_null_removes_it():
    timing = {"mode": "editor_window", "anchor": "render_start", "source_offset": 1, "fps": "editor"}
    current = {"view_id": "a", "label": "Out", "default_label": "Preview Image", "order": 0, "path": "9",
               "class_type": "PreviewImage", "origin": "authored", "timing": timing}
    data = {}
    cv.store_sdk_change(data, {"views": [current]}, "a", "label", "Renamed")
    assert data["views"]["a"]["timing"] == timing and data["views"]["a"]["label"] == "Renamed"
    assert cv.stored_views(data)["a"]["timing"] == timing
    cv.store_sdk_change(data, {"views": [current]}, "a", "timing", None)
    assert "timing" not in data["views"]["a"]
    # The export materializes a View's timing.
    curated = cv.curated_views({"views": [{**current, "status": "ok", "label": "Out"}]})
    assert curated["items"]["a"]["timing"] == timing
    # An invalid stored timing is ignored, the copy kept.
    data["views"]["a"]["timing"] = {"mode": "x"}
    assert "timing" not in cv.stored_views(data)["a"]


# ── Content record ───────────────────────────────────────────────────────────

WINDOW = (10, 40)


def base_scene() -> dict:
    """A scene as the browser holds it, sparse (fields left at their defaults)."""
    return {
        "scene_id": "s1", "duration_frames": 96, "fps": 24.0,
        "clips": [
            {"clip_id": "c1", "source_path": "Media/plate.mp4", "timeline_start_frame": 0, "timeline_end_frame": 60, "source_in_frame": 4,
             "track_index": 0},
            {"clip_id": "c2", "source_path": "Media/far.mp4", "timeline_start_frame": 70, "timeline_end_frame": 90, "track_index": 1},
            {"clip_id": "take0", "source_path": "Takes/old.mp4", "timeline_start_frame": 12, "timeline_end_frame": 30, "track_index": 2,
             "is_generated": True},
            {"clip_id": "d1", "source_path": "Media/pose.mp4", "timeline_start_frame": 5, "timeline_end_frame": 45, "track_index": 0,
             "role": "motion_driver", "strength": 0.75},
        ],
        "audio_tracks": [
            {"track_id": "a1", "source_path": "Media/music.wav", "timeline_start_frame": 0, "timeline_end_frame": 96, "lane_index": 0},
        ],
        "guide_frames": [
            {"guide_id": "g1", "frame_index": 20, "asset_id": "img1", "strength": 0.5, "fit_mode": "cover"},
            {"guide_id": "g2", "frame_index": -1, "asset_id": "img2"},
            {"guide_id": "g3", "frame_index": 38, "asset_id": "img3"},
        ],
        "guide_track_config": {"hidden": False},
        "video_lane_configs": [{"hidden": False}, {"hidden": False}, {"hidden": False}],
        "motion_driver_lane_configs": [{"hidden": False}],
        "audio_lane_configs": [{"hidden": False}],
        "linked_item_groups": [],
    }


def live_record(scene_dict: dict, window=WINDOW) -> dict:
    """The record the Editor stores for a live (not queued) run of this scene."""
    scene = Scene.from_dict(copy.deepcopy(scene_dict))
    return vt.scene_content_record(scene, fps=FPS, width=W, height=H, render_start=window[0], render_end=window[1],
                                   driver_source=scene.clips, guide_frames=scene.guide_frames,
                                   guide_track_hidden=bool(getattr(scene.guide_track_config, "hidden", False)),
                                   snapshot_window=None)


def snapshot_record(scene_dict: dict, *, selection=(14, 34), pre=2, post=2, window=WINDOW, executed: dict | None = None,
                    with_fit: bool = False) -> dict:
    """A queued run: guides, Drivers and Driver lane visibility frozen as
    editor_widget._buildQueueSnapshot does; `executed` is the live scene at
    execution when it changed after enqueue."""
    scene = Scene.from_dict(copy.deepcopy(executed or scene_dict))
    duration = scene.duration_frames
    start, end = max(0, selection[0] - pre), min(duration, selection[1] + post)
    hidden = bool(scene_dict.get("guide_track_config", {}).get("hidden"))
    guides = []
    for guide in scene_dict["guide_frames"]:
        index = int(guide.get("frame_index", 0))
        if index == -1:
            index = max(0, duration - 1)
        if start <= index < end:
            guides.append({"guide_id": guide.get("guide_id", ""), "frame_index": index, "asset_id": guide.get("asset_id"),
                           "source": guide.get("source") or "asset", "strength": guide.get("strength", 1.0),
                           "muted": hidden or bool(guide.get("muted")),
                           # Phase 3: newer jobs record fit and crop (with_fit); older ones did not.
                           **({"fit_mode": guide.get("fit_mode", "pad_edge"),
                               "crop_position": guide.get("crop_position", "center")} if with_fit else {})})
    from server.timeline_state import ClipReference, GuideFrame
    drivers = [ClipReference.from_dict(dict(c)) for c in scene_dict["clips"]
               if c.get("role") == "motion_driver" and c.get("timeline_start_frame", 0) < end and c.get("timeline_end_frame", 0) > start]
    return vt.scene_content_record(scene, fps=FPS, width=W, height=H, render_start=window[0], render_end=window[1],
                                   driver_source=drivers, guide_frames=[GuideFrame.from_dict(g) for g in guides],
                                   guide_track_hidden=False, snapshot_window=(start, end),
                                   driver_configs=[dict(c) for c in scene_dict["motion_driver_lane_configs"]],
                                   snapshot_guide_fit=with_fit and bool(guides))


def _with(scene, fn):
    scene = copy.deepcopy(scene)
    fn(scene)
    return scene


def _clip(scene, clip_id):
    return next(c for c in scene["clips"] if c["clip_id"] == clip_id)


def parity_cases():
    """[{name, scene (the browser's, after any edit), content, assets, assets_loaded, expect}]."""
    base = base_scene()
    record = live_record(base)
    full = Scene.from_dict(copy.deepcopy(base)).to_dict()
    own_take = {"clip_id": "t9", "source_path": "Takes/new.mp4", "timeline_start_frame": 14, "timeline_end_frame": 34, "track_index": 3,
                "is_generated": True}
    take_audio = {"track_id": "ta9", "source_path": "Takes/new.wav", "timeline_start_frame": 14, "timeline_end_frame": 34, "lane_index": 1}

    def add_take(linked):
        def edit(scene):
            scene["clips"].append(dict(own_take))
            scene["audio_tracks"].append(dict(take_audio))
            scene["video_lane_configs"].append({"hidden": False})
            scene["audio_lane_configs"].append({"hidden": False})
            if linked:
                scene["linked_item_groups"].append({"group_id": "lg", "items": [{"type": "clip", "id": "t9"}, {"type": "audio", "id": "ta9"}]})
        return edit

    cases = [
        ("unchanged, sparse", base, record, "same"),
        ("unchanged, to_dict shape", full, record, "same"),
        ("own take and linked audio land", _with(base, add_take(True)), record, "same"),
        ("own take and unlinked audio land (provenance)", _with(base, add_take(False)), record, "same"),
        ("a user audio track added in the range", _with(base, lambda s: s["audio_tracks"].append(
            {"track_id": "u1", "source_path": "Media/vo.wav", "timeline_start_frame": 0, "timeline_end_frame": 20})), record, "changed"),
        ("a clip in the range edited", _with(base, lambda s: _clip(s, "c1").update(opacity=0.5)), record, "changed"),
        ("a clip trimmed outside the range", _with(base, lambda s: _clip(s, "c1").update(timeline_end_frame=50)), record, "same"),
        ("a clip outside the range edited", _with(base, lambda s: _clip(s, "c2").update(opacity=0.2)), record, "same"),
        ("a pre-existing take deleted", _with(base, lambda s: s["clips"].remove(_clip(s, "take0"))), record, "changed"),
        ("a Driver's fit changed", _with(base, lambda s: _clip(s, "d1").update(fit_mode="cover")), record, "changed"),
        ("a guide muted", _with(base, lambda s: s["guide_frames"][0].update(muted=True)), record, "changed"),
        ("the guide track hidden", _with(base, lambda s: s["guide_track_config"].update(hidden=True)), record, "changed"),
        ("a video lane hidden", _with(base, lambda s: s["video_lane_configs"][0].update(hidden=True)), record, "changed"),
        ("a path written with backslashes", _with(base, lambda s: _clip(s, "c1").update(source_path="Media\\plate.mp4")), record, "same"),
        ("an asset renamed (path kept)", base, record, "same"),
        ("a clip split outside the range", _with(base, lambda s: (_clip(s, "c2").update(timeline_end_frame=80), s["clips"].append(
            {"clip_id": "c2b", "source_path": "Media/far.mp4", "timeline_start_frame": 80, "timeline_end_frame": 90, "source_in_frame": 10,
             "track_index": 1}))), record, "same"),
    ]
    # A last-frame guide inside the range: a duration change moves it out.
    edge = _with(base, lambda s: s.update(duration_frames=40))
    cases.append(("a last-frame guide moves with the duration", _with(edge, lambda s: s.update(duration_frames=60)), live_record(edge), "changed"))
    # A queued run: guides and Drivers from the job's raw window; a hidden
    # track, non-default guide fit and a guide in the snapped extension.
    queued = _with(base, lambda s: (s["guide_track_config"].update(hidden=True), s["guide_frames"].append(
        {"guide_id": "g4", "frame_index": 11, "asset_id": "img4", "fit_mode": "stretch"})))
    cases.append(("queued run, hidden guide track", queued, snapshot_record(queued), "same"))
    shown = _with(base, lambda s: s["guide_frames"].append({"guide_id": "g4", "frame_index": 11, "asset_id": "img4", "fit_mode": "stretch"}))
    cases.append(("queued run, guide fit and snap extension", shown, snapshot_record(shown), "same"))
    cases.append(("queued run, a guide edited after", _with(shown, lambda s: s["guide_frames"][0].update(strength=1.0)), snapshot_record(shown), "changed"))
    # A job that recorded its guides' fit and crop compares them too.
    cases.append(("queued run with recorded fit, unchanged", shown, snapshot_record(shown, with_fit=True), "same"))
    cases.append(("queued run with recorded fit, a guide's fit changed after",
                  _with(shown, lambda s: s["guide_frames"][0].update(fit_mode="stretch")), snapshot_record(shown, with_fit=True), "changed"))
    cases.append(("queued run without recorded fit, a guide's fit changed after",
                  _with(shown, lambda s: s["guide_frames"][0].update(fit_mode="stretch")), snapshot_record(shown), "same"))
    # Unicode in paths hashes identically.
    odd = _with(base, lambda s: _clip(s, "c1").update(source_path="Médias/ΑΣ 🎬  \x7f\"q\".mp4"))
    cases.append(("unicode, astral and control characters", odd, live_record(odd), "same"))
    rows = [{"name": name, "scene": scene, "content": content, "expect": expect,
             "assets": [{"path": "Takes/new.wav", "has_provenance": True}, {"path": "Media/vo.wav", "has_provenance": False}],
             "assets_loaded": True} for name, scene, content, expect in cases]
    # A Driver lane hidden after enqueue: the run used the job's visibility.
    hidden_after = _with(base, lambda s: s["motion_driver_lane_configs"][0].update(hidden=True))
    rows.append({"name": "queued run, a Driver lane hidden after enqueue", "scene": hidden_after,
                 "content": snapshot_record(base, executed=hidden_after), "expect": "changed", "assets": [], "assets_loaded": True})
    rows.append({"name": "queued run, a Driver lane hidden after enqueue and shown again", "scene": base,
                 "content": snapshot_record(base, executed=hidden_after), "expect": "same", "assets": [], "assets_loaded": True})
    # A new track whose asset the list does not hold yet: unknown, never changed.
    rows.append({"name": "a new unlinked track not in the asset list yet", "scene": _with(base, add_take(False)), "content": record,
                 "expect": "unknown", "assets": [{"path": "Media/vo.wav", "has_provenance": False}], "assets_loaded": True})
    # An id-less generated clip never hides the other id-less clips.
    idless = _with(base, lambda s: (_clip(s, "c1").update(clip_id=""), s["clips"].append(
        {"clip_id": "", "source_path": "Takes/x.mp4", "timeline_start_frame": 80, "timeline_end_frame": 90, "track_index": 2,
         "is_generated": True})))
    rows.append({"name": "an id-less generated clip beside an id-less clip", "scene": idless, "content": record,
                 "expect": "same", "assets": [], "assets_loaded": True})
    rows.append({"name": "a new unlinked track before assets load", "scene": _with(base, add_take(False)), "content": record,
                 "expect": "unknown", "assets": [], "assets_loaded": False})
    return rows


def test_records_have_their_window_present_set_and_snapshot_flags():
    record = live_record(base_scene())
    assert record["v"] == 1 and record["window"] == [10, 40] and record["input_window"] == [10, 40] and record["guide_fit"] is True
    assert record["present"] == sorted(["c1", "take0", "d1", "a1"])
    queued = snapshot_record(base_scene())
    assert queued["input_window"] == [12, 36] and queued["guide_fit"] is False
    assert snapshot_record(base_scene(), with_fit=True)["guide_fit"] is True


def test_digest_moves_with_inputs_in_the_window_only():
    base = base_scene()
    digest = live_record(base)["digest"]
    assert live_record(_with(base, lambda s: _clip(s, "c2").update(opacity=0.1)))["digest"] == digest
    assert live_record(_with(base, lambda s: _clip(s, "c1").update(muted=True)))["digest"] != digest
    assert live_record(_with(base, lambda s: s["audio_tracks"][0].update(volume=0.5)))["digest"] != digest


def test_present_cap_omits_the_record():
    many = _with(base_scene(), lambda s: s["clips"].extend(
        {"clip_id": f"x{i}", "source_path": "m.mp4", "timeline_start_frame": 10, "timeline_end_frame": 20, "track_index": 0}
        for i in range(vt.MAX_PRESENT)))
    assert live_record(many) is None


def test_hash_matches_the_known_promptHash_vectors():
    # Values from the JS two-lane hash (controls_host_canvas promptHash before its extraction).
    assert vt.two_lane_hash("") == "811c9dc59e3779b9"
    assert len(vt.two_lane_hash("🎬\ud83d")) == 16
    assert vt.canonical(["a", 1, True, ["é"]]) == "[1:a,1,1,[1:é]]"
    assert vt.canonical("🎬") == "2:🎬"
    assert vt.micro(0.1) == 100000 and vt.micro(float("nan")) == -1 and vt.micro(None) == -1 and vt.micro(True) == 1000000


def test_note_editor_context_records_content_only_while_observing(monkeypatch):
    from server import run_outputs, sdk_runs
    run_outputs.reset_for_tests()
    calls = []
    monkeypatch.setattr(sdk_runs, "trusted_key_for_prompt", lambda prompt: "k")
    monkeypatch.setattr(sdk_runs, "record_snapshot", lambda key: {"prompt_id": "p1"})
    monkeypatch.setattr(sdk_runs, "is_observing", lambda pid: False)
    run_outputs.note_editor_context({}, "5", {"scene_id": "s1"}, content=lambda: calls.append(1) or {"v": 1})
    assert calls == []
    monkeypatch.setattr(sdk_runs, "is_observing", lambda pid: True)
    run_outputs.note_editor_context({}, "5", {"scene_id": "s1"}, content=lambda: calls.append(1) or {"v": 1, "digest": "x"})
    assert calls == [1] and run_outputs._DATA["p1"]["editor_context"]["5"]["content"] == {"v": 1, "digest": "x"}
    # A failing builder records the context without content.
    run_outputs.note_editor_context({}, "6", {"scene_id": "s1"}, content=lambda: 1 / 0)
    assert "content" not in run_outputs._DATA["p1"]["editor_context"]["6"]
    run_outputs.reset_for_tests()
