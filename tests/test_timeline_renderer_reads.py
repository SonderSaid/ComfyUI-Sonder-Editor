"""How the timeline renderer moves its decoders: in-order reads, grabs, seeks,
cached repeats, reader pooling and release, failures, progress and cancellation."""

import os
import threading

import numpy as np
import pytest

import server.timeline_renderer as timeline_renderer
from server.timeline_renderer import (
    READER_IDLE_RELEASE_FRAMES,
    SEQUENTIAL_GRAB_LIMIT,
    TimelineRenderCancelled,
    iter_scene_frames,
    render_scene_frames,
)
from server.timeline_state import Asset, ClipReference, LaneConfig, Scene, TimelineProject


class DecoderLog:
    """Shared record of every capture call, plus scripted failures."""

    def __init__(self, *, seek_fail=(), read_fail=(), grab_fail=(), open_fail=(), fail_open_from=None,
                 seek_offset=0, persistent=False):
        self.ops = []
        self.opened = []
        self.released = []
        # Seek, read and grab failures are one-shot unless persistent: a retry of
        # the same frame succeeds, like a decoder hiccup.
        self.seek_fail = set(seek_fail)
        self.read_fail = set(read_fail)
        self.grab_fail = set(grab_fail)
        self.persistent = persistent
        self.open_fail = set(open_fail)
        self.fail_open_from = fail_open_from
        self.seek_offset = seek_offset

    def fails(self, kind, frame):
        failures = getattr(self, f"{kind}_fail")
        if frame not in failures:
            return False
        if not self.persistent:
            failures.discard(frame)
        return True

    def factory(self, path):
        return DecoderCapture(self, path)


class DecoderCapture:
    """Models a real decoder: read() returns the frame at the position and advances."""

    def __init__(self, log, path):
        self.log = log
        self.name = os.path.basename(path)
        self.index = len(log.opened)
        self.position = 0
        log.opened.append(self.name)

    def get(self, _prop):
        return float(self.position)  # CAP_PROP_POS_FRAMES, the only property the renderer reads

    def isOpened(self):
        if self.log.fail_open_from is not None and self.index >= self.log.fail_open_from:
            return False
        return self.name not in self.log.open_fail

    def set(self, _prop, value):
        target = int(value)
        self.log.ops.append((self.index, "set", target))
        self.position = target + self.log.seek_offset
        return not self.log.fails("seek", target)

    def grab(self):
        frame = self.position
        self.position += 1
        self.log.ops.append((self.index, "grab", frame))
        return not self.log.fails("grab", frame)

    def read(self):
        frame = self.position
        self.position += 1
        self.log.ops.append((self.index, "read", frame))
        if self.log.fails("read", frame):
            return False, None
        return True, np.full((2, 2, 3), frame % 256, dtype=np.uint8)

    def release(self):
        self.log.released.append(self.index)


def _project(tmp_path, sources, *, scene_fps=24.0, duration=48):
    project_dir = tmp_path / "project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    assets = []
    for name, fps, count in sources:
        (media_dir / name).write_bytes(b"video")
        assets.append(Asset(
            asset_id=name, name=name, asset_type="video", path=os.path.join("media", name),
            fps=fps, frame_count=count, color_space="rgb", color_range="pc", color_probed=True,
        ))
    project = TimelineProject(project_dir=str(project_dir), fps=scene_fps, resolution=(2, 2), assets=assets)
    scene = Scene(scene_id="scene", fps=scene_fps, duration_frames=duration,
                  video_lane_configs=[LaneConfig(), LaneConfig()])
    project.scenes = [scene]
    return project, scene


def _clip(name, start, end, source_in=0, track=0, opacity=1.0):
    return ClipReference(source_path=os.path.join("media", name), timeline_start_frame=start,
                         timeline_end_frame=end, source_in_frame=source_in, track_index=track, opacity=opacity)


@pytest.fixture
def used_frames(monkeypatch):
    """The decoded value each clip handed to the compositor, in call order."""
    seen = []
    original = timeline_renderer.fit_frame_to_canvas

    def recording(frame_rgb, *args, **kwargs):
        seen.append(int(frame_rgb[0, 0, 0]))
        return original(frame_rgb, *args, **kwargs)

    monkeypatch.setattr(timeline_renderer, "fit_frame_to_canvas", recording)
    return seen


def _render(project, scene, log, start=0, end=None, status=None):
    end = scene.duration_frames if end is None else end
    return list(iter_scene_frames(project, scene, start, end, video_capture_factory=log.factory,
                                  render_status=status))


def _kinds(log):
    return [kind for _index, kind, _frame in log.ops]


def test_a_fresh_reader_starts_at_frame_zero_and_reads_on_without_seeking(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=12)
    scene.clips = [_clip("a.mp4", 0, 12, source_in=5)]
    log = DecoderLog()
    _render(project, scene, log)
    assert _kinds(log) == ["grab"] * 5 + ["read"] * 12
    assert used_frames == list(range(5, 17))


def test_a_fresh_capture_that_does_not_report_frame_zero_seeks(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=4)
    scene.clips = [_clip("a.mp4", 0, 4, source_in=2)]
    log = DecoderLog()

    class Elsewhere(DecoderCapture):
        def get(self, _prop):
            return -1.0

    list(iter_scene_frames(project, scene, 0, 4, video_capture_factory=lambda path: Elsewhere(log, path)))
    assert log.ops[0] == (0, "set", 2)
    assert used_frames == [2, 3, 4, 5]


def test_a_fresh_reader_seeks_to_a_start_past_the_grab_limit(tmp_path, used_frames):
    start = SEQUENTIAL_GRAB_LIMIT + 4
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 100)], duration=6)
    scene.clips = [_clip("a.mp4", 0, 6, source_in=start)]
    log = DecoderLog()
    _render(project, scene, log)
    assert log.ops[0] == (0, "set", start)
    assert _kinds(log)[1:] == ["read"] * 6
    assert used_frames == list(range(start, start + 6))


def test_repeated_index_from_rate_up_conversion_reuses_the_decoded_frame(tmp_path, used_frames):
    # 24 fps media in a 30 fps scene repeats one source frame in every five.
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], scene_fps=30.0, duration=20)
    scene.clips = [_clip("a.mp4", 0, 20)]
    log = DecoderLog()
    _render(project, scene, log)
    expected = [int(np.floor((u + 0.5) * 0.8)) for u in range(20)]
    assert used_frames == expected
    assert _kinds(log).count("read") == len(set(expected))
    assert _kinds(log).count("set") == 0


def test_small_forward_gap_grabs_instead_of_seeking(tmp_path, used_frames):
    # 60 fps media in a 24 fps scene skips one or two frames each step.
    project, scene = _project(tmp_path, [("a.mp4", 60.0, 200)], duration=20)
    scene.clips = [_clip("a.mp4", 0, 20)]
    log = DecoderLog()
    _render(project, scene, log)
    assert used_frames == [int(np.floor((u + 0.5) * 2.5)) for u in range(20)]
    assert _kinds(log).count("set") == 0
    assert "grab" in _kinds(log)


def test_large_forward_jump_and_backward_jump_seek(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 200)], duration=12)
    jump = SEQUENTIAL_GRAB_LIMIT + 10
    scene.clips = [
        _clip("a.mp4", 0, 4, source_in=0),
        _clip("a.mp4", 4, 8, source_in=4 + jump),     # forward past the grab limit
        _clip("a.mp4", 8, 12, source_in=2),           # backward
    ]
    log = DecoderLog()
    _render(project, scene, log)
    assert used_frames == [0, 1, 2, 3, 4 + jump, 5 + jump, 6 + jump, 7 + jump, 2, 3, 4, 5]
    assert [frame for _i, kind, frame in log.ops if kind == "set"] == [4 + jump, 2]
    assert len(log.opened) == 1


def test_contiguous_cut_of_one_source_continues_without_a_seek(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=10)
    scene.clips = [_clip("a.mp4", 0, 5, source_in=0), _clip("a.mp4", 5, 10, source_in=5)]
    log = DecoderLog()
    _render(project, scene, log)
    assert used_frames == list(range(10))
    assert _kinds(log).count("set") == 0
    assert len(log.opened) == 1


def test_two_layers_on_the_same_frame_decode_it_once(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=8)
    scene.clips = [_clip("a.mp4", 0, 8, source_in=3, track=0), _clip("a.mp4", 0, 8, source_in=3, track=1, opacity=0.5)]
    log = DecoderLog()
    _render(project, scene, log)
    assert used_frames == [value for frame in range(3, 11) for value in (frame, frame)]
    assert _kinds(log).count("read") == 8
    assert len(log.opened) == 1


def test_two_layers_at_different_positions_get_a_reader_each(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=8)
    scene.clips = [_clip("a.mp4", 0, 8, source_in=0, track=0), _clip("a.mp4", 0, 8, source_in=20, track=1, opacity=0.5)]
    log = DecoderLog()
    _render(project, scene, log)
    assert used_frames == [value for frame in range(8) for value in (frame, frame + 20)]
    assert len(log.opened) == 2
    assert [(index, frame) for index, kind, frame in log.ops if kind == "set"] == [(1, 20)]


def test_failed_seek_is_marked_and_the_next_request_seeks_again(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 100)], duration=4)
    scene.clips = [_clip("a.mp4", 0, 4, source_in=30)]
    log = DecoderLog(seek_fail={30})
    status = {}
    _render(project, scene, log, status=status)
    assert [reason for _block, _path, reason in status["failures"]] == ["seek_failed"]
    # The frame read after the failed seek is still used, as before, but not trusted.
    assert [frame for _i, kind, frame in log.ops if kind == "set"] == [30, 31]
    assert used_frames == [30, 31, 32, 33]


@pytest.mark.parametrize("failure", ["read", "grab"])
def test_failed_in_order_read_or_grab_is_marked_and_retried_with_a_seek(tmp_path, used_frames, failure):
    if failure == "read":
        project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=6)
        log = DecoderLog(read_fail={2})
        retried, expected = 2, list(range(6))
    else:
        # 60 -> 24 reads 1, 3, 6, 8, ... and grabs the frames between; fail the grab of 4.
        project, scene = _project(tmp_path, [("a.mp4", 60.0, 200)], duration=6)
        log = DecoderLog(grab_fail={4})
        retried, expected = 6, [int(np.floor((u + 0.5) * 2.5)) for u in range(6)]
    scene.clips = [_clip("a.mp4", 0, 6)]
    status = {}
    _render(project, scene, log, status=status)
    # The block stays out of the cache, but the frame is recovered as the old
    # seek-every-frame path would have recovered it.
    assert [reason for _block, _path, reason in status["failures"]] == ["read_failed"]
    assert [frame for _i, kind, frame in log.ops if kind == "set"] == [retried]
    assert used_frames == expected


def test_a_read_that_fails_again_after_the_seek_drops_the_layer(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=4)
    scene.clips = [_clip("a.mp4", 0, 4)]
    log = DecoderLog(read_fail={2}, persistent=True)
    status = {}
    _render(project, scene, log, status=status)
    assert [reason for _block, _path, reason in status["failures"]] == ["read_failed", "read_failed"]
    assert used_frames == [0, 1, 3]


def test_a_seek_landing_off_by_k_shifts_frames_as_before(tmp_path, used_frames):
    # Decode order follows wherever the seek landed, as the old path did on each frame.
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 100)], duration=8)
    scene.clips = [_clip("a.mp4", 0, 8, source_in=30)]
    log = DecoderLog(seek_offset=1)
    _render(project, scene, log)
    assert used_frames == list(range(31, 39))


def test_an_open_failure_is_tried_once_per_source(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=10)
    scene.clips = [_clip("a.mp4", 0, 5), _clip("a.mp4", 5, 10, source_in=30)]
    log = DecoderLog(open_fail={"a.mp4"})
    status = {}
    _render(project, scene, log, status=status)
    assert log.opened == ["a.mp4"]
    assert len(status["failures"]) == 10
    assert used_frames == []


def test_a_second_reader_that_fails_to_open_shares_the_first(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 100)], duration=6)
    scene.clips = [_clip("a.mp4", 0, 6, source_in=0, track=0),
                   _clip("a.mp4", 0, 6, source_in=40, track=1, opacity=0.5)]
    log = DecoderLog(fail_open_from=1)
    status = {}
    _render(project, scene, log, status=status)
    assert used_frames == [value for frame in range(6) for value in (frame, frame + 40)]
    assert log.opened == ["a.mp4", "a.mp4"]  # one failed extra open, never retried
    assert status["failures"] == []


def test_layers_of_one_source_open_at_most_the_reader_cap(tmp_path, used_frames):
    layers = timeline_renderer.MAX_READERS_PER_SOURCE + 2
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 400)], duration=4)
    scene.video_lane_configs = [LaneConfig() for _ in range(layers)]
    scene.clips = [_clip("a.mp4", 0, 4, source_in=50 * layer, track=layer, opacity=0.5) for layer in range(layers)]
    log = DecoderLog()
    _render(project, scene, log)
    assert len(log.opened) == timeline_renderer.MAX_READERS_PER_SOURCE
    assert used_frames == [50 * layer + frame for frame in range(4) for layer in range(layers)]


def test_a_cut_on_one_layer_does_not_take_the_reader_another_layer_continues_on(tmp_path, used_frames):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 400)], duration=8)
    scene.clips = [
        _clip("a.mp4", 0, 4, source_in=0, track=0),
        _clip("a.mp4", 4, 8, source_in=200, track=0),               # the lower layer cuts away
        _clip("a.mp4", 0, 8, source_in=100, track=1, opacity=0.5),  # the upper layer runs on
    ]
    log = DecoderLog()
    _render(project, scene, log)
    assert [frame for _i, kind, frame in log.ops if kind == "set"] == [100, 200]
    upper = next(index for index, kind, frame in log.ops if kind == "set" and frame == 100)
    assert [frame for index, kind, frame in log.ops if index == upper and kind == "read"] == list(range(100, 108))


def test_an_idle_reader_is_released_and_reopened_later(tmp_path, used_frames):
    idle = READER_IDLE_RELEASE_FRAMES + 2
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 400), ("b.mp4", 24.0, 400)], duration=idle + 8)
    scene.clips = [
        _clip("a.mp4", 0, 4),
        _clip("b.mp4", 4, 4 + idle),
        _clip("a.mp4", 4 + idle, 8 + idle, source_in=4),
    ]
    log = DecoderLog()
    _render(project, scene, log)
    assert log.opened == ["a.mp4", "b.mp4", "a.mp4"]
    assert 0 in log.released[:-2]  # the first a.mp4 reader went before the render ended
    assert sorted(log.released) == [0, 1, 2]


def test_captures_are_released_when_the_render_ends(tmp_path):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48), ("b.mp4", 24.0, 48)], duration=6)
    scene.clips = [_clip("a.mp4", 0, 6, track=0), _clip("b.mp4", 0, 6, track=1, opacity=0.5)]
    log = DecoderLog()
    _render(project, scene, log)
    assert sorted(log.released) == [0, 1]


# --- progress ---------------------------------------------------------------

def test_progress_counts_requested_frames_with_the_cache_off(tmp_path):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=10)
    scene.clips = [_clip("a.mp4", 0, 10)]
    calls = []
    render_scene_frames(project, scene, 2, 9, use_cache=False, video_capture_factory=DecoderLog().factory,
                        progress=lambda done, total: calls.append((done, total)))
    assert calls == [(n, 7) for n in range(0, 8)]


def _assert_progress_run(calls, total):
    assert calls[0] == (0, total)
    values = [done for done, _total in calls]
    assert values == sorted(values)
    assert values[-1] == total
    assert all(seen_total == total for _done, seen_total in calls)


def test_progress_counts_empty_blocks(tmp_path):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 200)], duration=96)
    scene.clips = [_clip("a.mp4", 0, 20)]  # frames 20-95 have no clip
    calls = []
    render_scene_frames(project, scene, 0, 96, video_capture_factory=DecoderLog().factory,
                        progress=lambda done, total: calls.append((done, total)))
    _assert_progress_run(calls, 96)


def test_progress_never_passes_the_request_when_blocks_straddle_it(tmp_path):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 200)], duration=96)
    scene.clips = [_clip("a.mp4", 0, 96)]
    cold, warm, mixed = [], [], []
    render_scene_frames(project, scene, 5, 40, video_capture_factory=DecoderLog().factory,
                        progress=lambda done, total: cold.append((done, total)))
    _assert_progress_run(cold, 35)
    render_scene_frames(project, scene, 5, 40, video_capture_factory=DecoderLog().factory,
                        progress=lambda done, total: warm.append((done, total)))
    _assert_progress_run(warm, 35)
    render_scene_frames(project, scene, 20, 90, video_capture_factory=DecoderLog().factory,
                        progress=lambda done, total: mixed.append((done, total)))
    _assert_progress_run(mixed, 70)


# --- cancellation ------------------------------------------------------------

def test_cache_hit_loading_stops_within_one_block(tmp_path, monkeypatch):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 200)], duration=96)
    scene.clips = [_clip("a.mp4", 0, 96)]
    render_scene_frames(project, scene, 0, 96, video_capture_factory=DecoderLog().factory)
    cancel = threading.Event()
    loads = []
    original = timeline_renderer.load_block

    def loading(*args, **kwargs):
        loads.append(kwargs.get("block_index"))
        result = original(*args, **kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(timeline_renderer, "load_block", loading)
    with pytest.raises(TimelineRenderCancelled):
        render_scene_frames(project, scene, 0, 96, cancel_event=cancel, video_capture_factory=DecoderLog().factory)
    assert len(loads) == 1


def test_an_interrupt_from_progress_in_a_cached_render_cleans_up(tmp_path):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 200)], duration=96)
    scene.clips = [_clip("a.mp4", 0, 96)]
    log = DecoderLog()

    def progress(done, _total):
        if done == 40:
            raise KeyboardInterrupt  # a BaseException, like ComfyUI's interrupt

    with pytest.raises(KeyboardInterrupt):
        render_scene_frames(project, scene, 0, 96, video_capture_factory=log.factory, progress=progress)
    assert log.opened and sorted(log.released) == list(range(len(log.opened)))
    root = tmp_path / "project" / "cache" / "renders"
    assert not root.exists() or list(root.iterdir()) == []


def test_cache_off_render_releases_captures_before_an_error_leaves(tmp_path):
    project, scene = _project(tmp_path, [("a.mp4", 24.0, 48)], duration=10)
    scene.clips = [_clip("a.mp4", 0, 10)]
    log = DecoderLog()
    released_at_raise = []

    def progress(done, _total):
        if done == 3:
            released_at_raise.append(list(log.released))
            raise KeyboardInterrupt  # stands in for an interrupt raised by a progress hook

    with pytest.raises(KeyboardInterrupt):
        render_scene_frames(project, scene, 0, 10, use_cache=False, video_capture_factory=log.factory,
                            progress=progress)
    assert released_at_raise == [[]]
    assert log.released == [0]


# --- real media --------------------------------------------------------------

def _barcode_frame(index, width=128, height=64):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[height // 4:] = ((np.arange(width) + index * 3) % 256).astype(np.uint8)[None, :, None]
    stripe = width // 16
    for bit in range(16):
        if (index >> (15 - bit)) & 1:
            frame[:height // 4, bit * stripe:(bit + 1) * stripe] = 255
    return frame


def _read_barcode(rgb):
    height, width = rgb.shape[:2]
    stripe = width // 16
    value = 0
    for bit in range(16):
        value = (value << 1) | int(rgb[height // 8, bit * stripe + stripe // 2].mean() >= 128)
    return value


def _encode_with_pyav(path, codec, rate, frames, pix_fmt, options):
    av = pytest.importorskip("av")
    container = av.open(str(path), "w")
    try:
        stream = container.add_stream(codec, rate=rate)
        stream.width, stream.height, stream.pix_fmt = 128, 64, pix_fmt
        if options:
            stream.options = options
        try:
            first = stream.encode(av.VideoFrame.from_ndarray(_barcode_frame(0), format="rgb24"))
        except av.error.FFmpegError as exc:  # only an encoder this build lacks is a skip
            pytest.skip(f"{codec} encoder unavailable: {exc}")
        for packet in first:
            container.mux(packet)
        for index in range(1, frames):
            for packet in stream.encode(av.VideoFrame.from_ndarray(_barcode_frame(index), format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


def _encode_ffv1(path, rate, frames):
    # PyAV's ffv1 encoder does not open on this stack; the pack's own ffmpeg does,
    # with the Lossless FFV1 (RGB) preset's arguments.
    import subprocess

    from server.media_helpers import get_ffmpeg_path

    try:
        ffmpeg = get_ffmpeg_path()
    except Exception as exc:
        pytest.skip(f"ffmpeg unavailable: {exc}")
    raw = b"".join(_barcode_frame(index).tobytes() for index in range(frames))
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "128x64",
                    "-r", str(rate), "-i", "-", "-c:v", "ffv1", "-level", "3", "-coder", "1", "-context", "1",
                    "-g", "1", "-slices", "4", "-slicecrc", "1", "-pix_fmt", "gbrp", str(path)],
                   input=raw, check=True)


@pytest.mark.parametrize("codec", ["libx264", "prores_ks", "ffv1"])
def test_real_media_uses_decode_order_across_cuts_jumps_and_layers(tmp_path, monkeypatch, codec):
    cv2 = pytest.importorskip("cv2")
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    name = {"libx264": "barcode.mp4", "prores_ks": "barcode.mov", "ffv1": "barcode.mkv"}[codec]
    path = project_dir / "media" / name
    if codec == "ffv1":
        _encode_ffv1(path, 30, 120)
    elif codec == "libx264":
        # Short GOPs with B-frames, so grabs and seeks cross keyframes.
        _encode_with_pyav(path, codec, 30, 120, "yuv420p", {"g": "8", "bf": "3", "crf": "12"})
    else:
        _encode_with_pyav(path, codec, 30, 120, "yuv422p10le", None)
    asset = Asset(asset_id="a", name=name, asset_type="video", path=os.path.join("media", name), fps=30.0,
                  frame_count=120, color_space="rgb", color_range="pc", color_probed=True)
    project = TimelineProject(project_dir=str(project_dir), fps=24.0, resolution=(128, 64), assets=[asset])
    scene = Scene(scene_id="real", fps=24.0, duration_frames=48, video_lane_configs=[LaneConfig(), LaneConfig()])
    scene.clips = [
        _clip(name, 0, 10, source_in=0),
        _clip(name, 10, 18, source_in=20),                         # a forward cut of 10 grabs
        _clip(name, 18, 28, source_in=60),                         # a forward jump past the grab limit
        _clip(name, 28, 48, source_in=3),                          # backward
        _clip(name, 34, 48, source_in=10, track=1, opacity=0.5),   # a layered repeat
    ]
    project.scenes = [scene]
    seen, sets = [], []
    original = timeline_renderer.fit_frame_to_canvas

    def recording(frame_rgb, *args, **kwargs):
        seen.append(_read_barcode(frame_rgb))
        return original(frame_rgb, *args, **kwargs)

    class CountingCapture:
        def __init__(self, media_path):
            self.cap = cv2.VideoCapture(media_path, cv2.CAP_FFMPEG)

        def set(self, prop, value):
            sets.append(int(value))
            return self.cap.set(prop, value)

        def __getattr__(self, attr):
            return getattr(self.cap, attr)

    monkeypatch.setattr(timeline_renderer, "fit_frame_to_canvas", recording)
    render_scene_frames(project, scene, 0, 48, use_cache=False, video_capture_factory=CountingCapture)

    expected = []
    for frame in range(48):
        for clip in sorted((c for c in scene.clips if c.timeline_start_frame <= frame < c.timeline_end_frame),
                           key=lambda c: c.track_index):
            units = clip.source_in_frame + frame - clip.timeline_start_frame
            expected.append(min(119, int(np.floor((units + 0.5) * 30.0 / 24.0))))
    assert seen == expected
    # Seeks only for the far jump and the backward cut: the opening clip and the
    # layered repeat's new reader start near frame 0, and the 10-frame cut grabs.
    assert sorted(sets) == [4, 75]
