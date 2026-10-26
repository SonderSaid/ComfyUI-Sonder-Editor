"""The timeline's take-restoring drop, run from the production methods.

`_handleAssetDropWithinGesture` and the restoration helpers beside it are lifted
from editor_widget.js into a harness whose queue runs a slot at once and whose
transport records the batch, so each case can inspect what was painted, what
was sent, and what the author was told.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

TAKE = {
    "take_placement_policy": "authored_v1", "placement_fps": 24, "scene_id": "scene",
    "selection_start": 20, "selection_end": 33, "actual_pre_context_frames": 17,
    "actual_post_context_frames": 0, "requested_mask_pre_offset": 0, "requested_mask_post_offset": 0,
    "frame_count_padding": 0, "take_placement_mode": "trimmed", "take_keep_padding": False,
}


def _method(source, name, next_name):
    def declaration(method_name, offset=0):
        found = [value for value in (source.find(f"    {method_name}(", offset),
                                     source.find(f"    async {method_name}(", offset)) if value >= 0]
        if not found:
            raise AssertionError(f"method {method_name} not found")
        return min(found)
    start = declaration(name)
    return source[start:declaration(next_name, start + 1)]


def _run(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    methods = _method(widget, "_handleAssetDropWithinGesture", "_firstAvailableLane")
    lanes = (ROOT / "web/js/lane_registry.js").as_uri()
    restoration = (ROOT / "web/js/take_restoration.js").as_uri()
    script = f"""
const {{TRACK_TYPE, descriptorFor, laneCountFor, laneAcceptsAssetType}} = await import({json.dumps(lanes)});
const {{restoreTakeGeometry, takeAudioCovers, takeRestorationSource, TAKE_RESTORATION_REASONS}} =
  await import({json.dumps(restoration)});
const notices = [];
globalThis.notifyInfo = (m) => notices.push(["info", m]);
globalThis.notifyError = (m) => notices.push(["error", m]);
globalThis.notifyWarning = (m) => notices.push(["warning", m]);
globalThis.deriveRetryOnConflict = () => false;
let detailCalls = 0;
let detailAnswer = null;
globalThis.requestProjectAssetDetails = (input) => {{
  detailCalls += 1;
  return {{ promise: Promise.resolve().then(() => detailAnswer(input)), cancel() {{}} }};
}};
class Harness {{
{methods}
  constructor(c) {{
    this.c = c; this.sent = []; this.painted = null; this.refreshes = [];
    this.projectDir = "projects/project"; this.activeSceneId = "scene"; this.fps = 24;
    this.activeScene = {{scene_id: "scene", fps: c.sceneFps || 24, video_lane_count: 1, audio_lane_count: 1,
      motion_driver_lane_count: 1, video_lane_configs: [{{name: "V"}}], audio_lane_configs: [{{name: "A"}}],
      motion_driver_lane_configs: [{{}}], clips: [], audio_tracks: []}};
    this._settings = {{timelineBehavior: {{restoreTakePlacement: c.setting !== false, linkedVideoAudioDrop: true}}}};
    this.assets = {{video: [], audio: []}};
    this._trackLayout = [{{type: c.lane === "audio" ? TRACK_TYPE.AUDIO : TRACK_TYPE.VIDEO, laneIndex: 0}}];
    this._undoStack = [];
  }}
  get _effectiveFps() {{ return this.activeScene?.fps > 0 ? this.activeScene.fps : (this.fps || 24); }}
  _snapshotProjectMutationContext() {{ return {{sceneId: "scene", projectId: "project"}}; }}
  _pushUndo(label) {{ const entry = {{label}}; this._undoStack.push(entry); return entry; }}
  _discardUnstampableUndoEntry() {{}}
  _withMutationDiagnosticHeaders(init) {{ return init; }}
  _timelineRulerHeight() {{ return 10; }} _layoutIndexFromRawY() {{ return 0; }}
  _isLaneLocked() {{ return !!this.c.locked; }} _driverClipInLane() {{ return false; }}
  _isRenderClip(clip) {{ return clip.role !== "motion_driver"; }}
  _findAssetById() {{ return null; }} _mediaTimelineFrames() {{ return 40; }}
  _defaultFitMode() {{ return "pad_edge"; }} _defaultCropPosition() {{ return "center"; }}
  _renderSceneAfterLocalMutation() {{}} _buildTrackLayout() {{}} _renderTimeline() {{}}
  _reconcileActiveSceneFromMutation() {{}}
  _deferProjectBackedRefresh(kinds, reason) {{ this.refreshes.push(reason); }}
  _showToast(message) {{ notices.push(["toast", message]); }}
  async _fetchScenes() {{ return true; }}
  async _queueProjectMutation(options) {{ return options.run(options.intent, null); }}
  async _runVersionedProjectMutation(path, init) {{
    this.painted = structuredClone({{clips: this.activeScene.clips, audio: this.activeScene.audio_tracks}});
    const body = JSON.parse(init.body);
    this.sent.push(body);
    const create = body.operations.find((op) => op.type.startsWith("create_"));
    return {{payload: {{scene: this.activeScene, results: [{{type: create.type,
      clip: {{clip_id: "created", ...create.fields}}, audio_track: null,
      take_restoration: this.c.warnings ? {{warnings: this.c.warnings,
        ...(this.c.removedLane ? {{removed_audio_lane: true}} : {{}})}} : undefined}}]}}}};
  }}
}}
const cases = {json.dumps(cases)};
const out = [];
for (const c of cases) {{
  notices.length = 0; detailCalls = 0;
  const h = new Harness(c);
  detailAnswer = (input) => {{
    if (c.changeLanesDuringRead) h.activeScene.video_lane_count = 2;
    return new Map([[input.assetIds[0], {{status: "ok", revision: c.detailRevision || "rev", value: c.params}}]]);
  }};
  const asset = {{asset_id: "v1", asset_type: c.assetType || "video", has_provenance: true,
    provenance_revision: "rev", path: "media/take.mp4", media_probe_signature: "1:1", frame_count: 40,
    duration_sec: 40 / 24, has_audio: c.hasAudio !== false, media_replaced_at: c.replaced || "",
    ...(c.payloadProvenance ? {{_provenance: {{revision: "rev", params: c.params}}}} : {{}})}};
  await h._handleAssetDropWithinGesture(asset, c.cursor ?? 100, c.rawY ?? 0, "seq", null);
  out.push({{sent: h.sent, painted: h.painted, notices: [...notices], detailCalls, refreshes: h.refreshes,
    audioLanes: h.activeScene.audio_lane_count}});
}}
console.log(JSON.stringify(out));
"""
    completed = subprocess.run([node, "--input-type=module"], input=script, encoding="utf-8",
                               capture_output=True, text=True, cwd=ROOT)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _create(result):
    return next(op for op in result["sent"][0]["operations"] if op["type"].startswith("create_"))


def test_a_ruler_drop_restores_paints_and_sends_the_restored_take():
    [result] = _run([{"params": TAKE, "payloadProvenance": True}])
    create = _create(result)
    assert result["detailCalls"] == 0  # the gallery's cached provenance rode the drag
    assert create["fields"]["restore_take"] is True and create["fields"]["timeline_start_frame"] == 20
    assert create["fields"]["dual_drop"] is True
    expected = create["expected"]
    assert (expected["source_in_frame"], expected["source_out_frame"], expected["total_source_frames"],
            expected["timeline_end_frame"]) == (17, 30, 40, 33)
    assert expected["lane_baseline"] == {"video_lane_count": 1, "audio_lane_count": 1,
                                         "video_lane_config": None, "audio_lane_config": None}
    clip = result["painted"]["clips"][0]
    audio = result["painted"]["audio"][0]
    assert (clip["timeline_start_frame"], clip["timeline_end_frame"], clip["source_in_frame"],
            clip["source_out_frame"], clip["total_source_frames"]) == (20, 33, 17, 30, 40)
    assert (audio["timeline_start_frame"], audio["timeline_end_frame"], audio["source_in_frame"],
            audio["total_source_frames"]) == (20, 33, 17, 40)
    assert result["notices"] == []


def test_another_scene_restores_the_trim_at_the_cursor_in_its_clock():
    [result] = _run([{"params": {**TAKE, "scene_id": "elsewhere"}, "payloadProvenance": True,
                      "sceneFps": 30, "cursor": 7}])
    create = _create(result)
    assert create["fields"]["timeline_start_frame"] == 7
    assert create["expected"]["scene_fps"] == 30
    assert (create["expected"]["source_in_frame"], create["expected"]["source_out_frame"],
            create["expected"]["timeline_end_frame"]) == (21, 38, 24)


def test_without_cached_provenance_the_drop_reads_it_once():
    [result] = _run([{"params": TAKE}])
    assert result["detailCalls"] == 1
    assert _create(result)["fields"]["restore_take"] is True


def test_a_legacy_take_drops_whole_at_the_cursor_and_says_why():
    legacy = {key: value for key, value in TAKE.items() if key not in ("take_placement_policy", "placement_fps")}
    [result] = _run([{"params": legacy, "payloadProvenance": True}])
    create = _create(result)
    assert "restore_take" not in create["fields"] and "expected" not in create
    assert create["fields"]["timeline_start_frame"] == 100
    assert result["painted"]["clips"][0]["source_in_frame"] == 0
    assert result["notices"][0][0] == "info"
    assert result["notices"][0][1].startswith("This take was saved before Sonder recorded take placement")


def test_replaced_media_drops_whole_and_says_why():
    [result] = _run([{"params": TAKE, "payloadProvenance": True, "replaced": "2026-10-25T00:00:00"}])
    assert "restore_take" not in _create(result)["fields"]
    assert "replaced" in result["notices"][0][1]


def test_setting_off_drops_whole_at_the_cursor_even_in_the_original_scene():
    [result] = _run([{"params": TAKE, "setting": False}])
    create = _create(result)
    assert result["detailCalls"] == 0 and result["notices"] == []
    assert create["fields"]["timeline_start_frame"] == 100 and "restore_take" not in create["fields"]


def test_a_record_that_changed_during_the_read_places_nothing():
    [result] = _run([{"params": TAKE, "detailRevision": "newer"}])
    assert result["sent"] == []
    assert result["notices"][0][0] == "error" and "changed while its record was being read" in result["notices"][0][1]
    assert result["refreshes"] == ["take_drop_stale"]


def test_lanes_that_changed_during_the_read_place_nothing():
    [result] = _run([{"params": TAKE, "changeLanesDuringRead": True}])
    assert result["sent"] == []
    assert "timeline changed while the asset's record was being read" in result["notices"][0][1]


def test_an_audio_lane_drop_of_the_take_audio_restores_its_trim():
    [result] = _run([{"params": {**TAKE, "encoded_frame_count": 40}, "payloadProvenance": True,
                      "assetType": "audio", "lane": "audio", "rawY": 20}])
    create = _create(result)
    assert create["type"] == "create_audio_track" and create["fields"]["restore_take"] is True
    assert create["fields"]["lane_index"] == 0 and create["fields"]["timeline_start_frame"] == 20
    assert create["expected"]["lane_baseline"]["audio_lane_config"] == {"name": "A"}
    assert result["sent"][0]["operations"] == [create]  # a lane drop adds no lane


def test_the_servers_video_only_warning_is_shown():
    [result] = _run([{"params": TAKE, "payloadProvenance": True,
                      "warnings": ["Placed the take's video, but its audio could not be restored."]}])
    assert ["warning", "Placed the take's video, but its audio could not be restored."] in result["notices"]


def test_a_refused_ordinary_drop_does_not_claim_it_was_placed():
    legacy = {key: value for key, value in TAKE.items() if key not in ("take_placement_policy", "placement_fps")}
    [result] = _run([{"params": legacy, "payloadProvenance": True, "rawY": 20, "locked": True}])
    assert result["sent"] == []
    assert not [notice for notice in result["notices"] if notice[0] == "info"]


def test_the_audio_lane_the_server_removed_leaves_the_painted_timeline_too():
    warning = "Placed the take's video, but its audio could not be restored."
    [kept, removed] = _run([
        {"params": TAKE, "payloadProvenance": True, "warnings": [warning]},
        {"params": TAKE, "payloadProvenance": True, "warnings": [warning], "removedLane": True},
    ])
    assert kept["audioLanes"] == 2  # the ruler drop added a lane the server kept
    assert removed["audioLanes"] == 1


def test_the_setting_defaults_on_and_keeps_a_stored_off():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    settings = (ROOT / "web/js/editor_settings.js").as_uri()
    script = f"""
import assert from "node:assert/strict";
globalThis.localStorage = {{ getItem() {{ return null; }}, setItem() {{}} }};
const s = await import({json.dumps(settings)});
assert.equal(s.DEFAULT_EDITOR_SETTINGS.timelineBehavior.restoreTakePlacement, true);
assert.equal(s.normalizeEditorSettings().timelineBehavior.restoreTakePlacement, true);
assert.equal(s.normalizeEditorSettings({{ timelineBehavior: {{}} }}).timelineBehavior.restoreTakePlacement, true);
assert.equal(s.normalizeEditorSettings({{ timelineBehavior: {{ restoreTakePlacement: false }} }})
  .timelineBehavior.restoreTakePlacement, false);
"""
    completed = subprocess.run([node, "--input-type=module"], input=script, encoding="utf-8",
                               capture_output=True, text=True, cwd=ROOT)
    assert completed.returncode == 0, completed.stderr
    panel = (ROOT / "web/js/editor_settings_panel.js").read_text(encoding="utf-8")
    reset = panel[panel.index('"Reset Timeline Section"'):]
    assert "timelineBehavior: DEFAULT_EDITOR_SETTINGS.timelineBehavior" in reset[:400]
