import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.media_helpers as media_helpers
from server.timeline_renderer import iter_scene_frames
from server.timeline_state import Asset, ClipReference, LaneConfig, Scene, TimelineProject, apply_color_metadata


def _asset(**overrides):
    fields = dict(
        color_space="", color_transfer="", color_primaries="", color_range="",
        color_probed=True, generation_params={}, width=1920, height=1080,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ---------------------------------------------------------------------------
# Correction affine math
# ---------------------------------------------------------------------------

def test_correction_affine_inverts_mismatched_decode_exactly():
    encode_709, offset_709 = media_helpers._yuv_encode_matrix("bt709", "tv")
    encode_601, offset_601 = media_helpers._yuv_encode_matrix("bt601", "tv")
    decode_601 = np.linalg.inv(encode_601)
    affine = media_helpers.rgb_color_correction_affine("bt709", "tv", "bt601", "tv")
    assert affine is not None and affine.shape == (3, 4)
    for rgb in ([0, 1, 0], [1, 0, 1], [1, 0, 0], [0.5, 0.5, 0.5], [1, 1, 1], [0, 0, 0]):
        rgb = np.array(rgb, dtype=np.float64)
        yuv = encode_709 @ rgb + offset_709          # what a tagged-709 file contains
        wrong = decode_601 @ (yuv - offset_601) * 255.0  # what cv2 (fixed 601) yields
        fixed = affine[:, :3] @ wrong + affine[:, 3]
        assert np.abs(fixed - rgb * 255.0).max() < 1e-9
    # equal-range matrix correction carries no offset term
    assert np.allclose(affine[:, 3], 0.0)


def test_correction_affine_identity_and_range_offset():
    assert media_helpers.rgb_color_correction_affine("bt601", "tv", "bt601", "tv") is None
    assert media_helpers.rgb_color_correction_affine("bt709", "tv", "bt709", "tv") is None
    range_affine = media_helpers.rgb_color_correction_affine("bt709", "pc", "bt709", "tv")
    assert range_affine is not None
    assert not np.allclose(range_affine[:, 3], 0.0)
    assert media_helpers.rgb_color_correction_affine("exotic", "tv", "bt601", "tv") is None


def test_color_correction_for_interpretation_uses_source_range():
    # cv2's range handling follows the frame properties (Phase-0 pinned), so the
    # correction must compare matrices at the SOURCE's own range — a pc-range
    # source still gets a pure matrix correction, no offset.
    affine = media_helpers.color_correction_for_interpretation(("bt709", "pc"))
    assert affine is not None
    assert np.allclose(affine[:, 3], 0.0)
    assert media_helpers.color_correction_for_interpretation(("bt601", "tv")) is None
    assert media_helpers.color_correction_for_interpretation(None) is None


def test_apply_rgb_color_correction_saturates_uint8():
    affine = media_helpers.color_correction_for_interpretation(("bt709", "tv"))
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[:] = (255, 255, 255)
    out = media_helpers.apply_rgb_color_correction(frame, affine)
    assert out.dtype == np.uint8
    assert media_helpers.apply_rgb_color_correction(frame, None) is frame


# ---------------------------------------------------------------------------
# Interpretation decision matrix
# ---------------------------------------------------------------------------

def test_resolve_interpretation_tagged_assets():
    assert media_helpers.resolve_source_color_interpretation(
        _asset(color_space="bt709", color_range="tv"), allow_probe=False) == ("bt709", "tv")
    assert media_helpers.resolve_source_color_interpretation(
        _asset(color_space="smpte170m", color_range="pc"), allow_probe=False) == ("bt601", "pc")
    assert media_helpers.resolve_source_color_interpretation(
        _asset(color_space="bt2020nc", color_range=""), allow_probe=False) == ("bt2020", "tv")
    # manual override wins even when unprobed (mis-tagged-file escape hatch)
    assert media_helpers.resolve_source_color_interpretation(
        _asset(color_space="bt601", color_probed=False), allow_probe=False) == ("bt601", "tv")


def test_resolve_interpretation_rgb_content_is_identity():
    assert media_helpers.resolve_source_color_interpretation(
        _asset(color_space="rgb", color_range="pc"), allow_probe=False) is None
    assert media_helpers.resolve_source_color_interpretation(
        _asset(generation_params={"save_preset": "Lossless FFV1 (RGB)", "codec": "ffv1", "pix_fmt": "gbrp"}),
        allow_probe=False) is None


def test_resolve_interpretation_legacy_self_encode_is_bt601():
    legacy = _asset(generation_params={"save_preset": "Compatible MP4", "codec": "libx264", "pix_fmt": "yuv420p"})
    assert media_helpers.resolve_source_color_interpretation(legacy, allow_probe=False) == ("bt601", "tv")


def test_resolve_interpretation_color_managed_self_encode():
    managed = _asset(
        color_probed=False, width=640, height=480,
        generation_params={"save_preset": "Compatible MP4", "codec": "libx264", "pix_fmt": "yuv420p",
                           "color_managed": True, "color_space": "bt709", "color_range": "tv"},
    )
    assert media_helpers.resolve_source_color_interpretation(managed, allow_probe=False) == ("bt709", "tv")


def test_resolve_interpretation_untagged_resolution_heuristic():
    assert media_helpers.resolve_source_color_interpretation(
        _asset(width=1280, height=720), allow_probe=False) == ("bt709", "tv")
    assert media_helpers.resolve_source_color_interpretation(
        _asset(width=640, height=480), allow_probe=False) == ("bt601", "tv")
    # asset missing entirely, probing disallowed -> SD default
    assert media_helpers.resolve_source_color_interpretation(None, "", allow_probe=False) == ("bt601", "tv")


def test_resolve_interpretation_lazy_probe_and_failure_degradation(monkeypatch):
    calls = []

    def fake_probe(path):
        calls.append(path)
        return {"color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
                "color_range": "tv", "color_probed": True}

    monkeypatch.setattr(media_helpers, "_cached_color_probe", fake_probe)
    unprobed = _asset(color_probed=False)
    assert media_helpers.resolve_source_color_interpretation(unprobed, "X:/fake.mp4") == ("bt709", "tv")
    assert calls == ["X:/fake.mp4"]

    monkeypatch.setattr(media_helpers, "_cached_color_probe", lambda path: {})
    degraded = _asset(color_probed=False, width=1920, height=1080)
    assert media_helpers.resolve_source_color_interpretation(degraded, "X:/fake.mp4") == ("bt709", "tv")


# ---------------------------------------------------------------------------
# Compositor correction wiring
# ---------------------------------------------------------------------------

class _FakeCapture:
    def __init__(self, frame_bgr):
        self._frame = frame_bgr
        self.released = False

    def isOpened(self):
        return True

    def set(self, *_args):
        return True

    def read(self):
        return True, self._frame.copy()

    def release(self):
        self.released = True


def _single_clip_project(tmp_path, asset):
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "clip.mp4").write_bytes(b"video")
    project = TimelineProject(project_dir=str(project_dir), resolution=(4, 4))
    project.assets.append(asset)
    scene = Scene(scene_id="scene-1", duration_frames=1, video_lane_configs=[LaneConfig()])
    scene.clips = [
        ClipReference(source_path=os.path.join("media", "clip.mp4"), timeline_start_frame=0, timeline_end_frame=1)
    ]
    return project, scene


def test_iter_scene_frames_applies_correction_for_tagged_709(tmp_path):
    asset = Asset(asset_type="video", path="media/clip.mp4", width=4, height=4,
                  color_space="bt709", color_range="tv", color_probed=True)
    project, scene = _single_clip_project(tmp_path, asset)
    frame_bgr = np.zeros((4, 4, 3), dtype=np.uint8)
    frame_bgr[:] = (30, 200, 60)  # BGR

    frames = list(iter_scene_frames(project, scene, 0, 1,
                                    video_capture_factory=lambda path: _FakeCapture(frame_bgr)))
    expected_rgb = media_helpers.apply_rgb_color_correction(
        np.ascontiguousarray(frame_bgr[:, :, ::-1]),
        media_helpers.color_correction_for_interpretation(("bt709", "tv")),
    )
    assert len(frames) == 1
    assert np.array_equal(frames[0], expected_rgb)
    assert not np.array_equal(frames[0], frame_bgr[:, :, ::-1])  # correction actually did something


def test_iter_scene_frames_passthrough_for_legacy_self_encode(tmp_path):
    asset = Asset(asset_type="video", path="media/clip.mp4", width=4, height=4, color_probed=True,
                  generation_params={"save_preset": "Compatible MP4", "codec": "libx264", "pix_fmt": "yuv420p"})
    project, scene = _single_clip_project(tmp_path, asset)
    frame_bgr = np.zeros((4, 4, 3), dtype=np.uint8)
    frame_bgr[:] = (30, 200, 60)

    frames = list(iter_scene_frames(project, scene, 0, 1,
                                    video_capture_factory=lambda path: _FakeCapture(frame_bgr)))
    assert np.array_equal(frames[0], frame_bgr[:, :, ::-1])


# ---------------------------------------------------------------------------
# ffmpeg extraction color forcing
# ---------------------------------------------------------------------------

def _capture_decode_cmd(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = [str(part) for part in cmd]
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(media_helpers.subprocess, "run", fake_run)
    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    return captured


def _vf_of(cmd):
    return cmd[cmd.index("-vf") + 1]


def test_decode_video_range_explicit_interpretation(monkeypatch):
    captured = _capture_decode_cmd(monkeypatch)
    list(media_helpers.decode_video_range("clip.mp4", 0, 2, target_w=4, target_h=4,
                                          color_interpretation=("bt709", "tv")))
    vf = _vf_of(captured["cmd"])
    assert "scale=4:4:in_color_matrix=bt709:in_range=tv" in vf
    assert vf.count("scale=") == 1

    monkeypatch.setattr(media_helpers, "probe_video_size", lambda path: (4, 4))
    list(media_helpers.decode_video_range("clip.mp4", 0, 2, color_interpretation=("bt601", "tv")))
    assert "scale=in_color_matrix=bt601:in_range=tv" in _vf_of(captured["cmd"])


def test_decode_video_range_none_keeps_legacy_command(monkeypatch):
    captured = _capture_decode_cmd(monkeypatch)
    monkeypatch.setattr(media_helpers, "probe_video_size", lambda path: (4, 4))
    list(media_helpers.decode_video_range("clip.mp4", 0, 2, color_interpretation=None))
    assert "scale" not in _vf_of(captured["cmd"])


def test_decode_video_range_auto_resolves(monkeypatch):
    captured = _capture_decode_cmd(monkeypatch)
    monkeypatch.setattr(media_helpers, "probe_video_size", lambda path: (4, 4))
    monkeypatch.setattr(media_helpers, "resolve_source_color_interpretation",
                        lambda asset, path, **kwargs: ("bt709", "tv"))
    list(media_helpers.decode_video_range("clip.mp4", 0, 2))
    assert "in_color_matrix=bt709" in _vf_of(captured["cmd"])


def test_decode_video_frame_passes_interpretation(monkeypatch):
    seen = {}

    def fake_range(path, start, end, *, color_interpretation="auto", **kwargs):
        seen["interpretation"] = color_interpretation
        return iter(())

    monkeypatch.setattr(media_helpers, "decode_video_range", fake_range)
    media_helpers.decode_video_frame("clip.mp4", 0, color_interpretation=("bt601", "tv"))
    assert seen["interpretation"] == ("bt601", "tv")


# ---------------------------------------------------------------------------
# Probe + Asset persistence
# ---------------------------------------------------------------------------

def test_probe_color_ffmpeg_banner_fallback(monkeypatch):
    monkeypatch.setattr(media_helpers, "_ffprobe_json", lambda *args, **kwargs: {})
    banners = {
        "tagged": "  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 1280x720, 36 kb/s, 24 fps",
        "triple": "  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709/unknown/unknown, progressive), 1280x720, 24 fps",
        "untagged": "  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1280x720, 33 kb/s, 24 fps",
        "prores": "  Stream #0:0[0x1]: Video: prores (HQ) (apch / 0x68637061), yuv422p10le(bt709, progressive), 1280x720, 5043 kb/s",
        "rgb": "  Stream #0:0: Video: ffv1 (FFV1 / 0x31564646), gbrp(pc), 1280x720, 24 fps",
    }
    results = {}
    for label, line in banners.items():
        monkeypatch.setattr(media_helpers, "_ffmpeg_input_text", lambda path, line=line: f"Input #0\n{line}\n")
        results[label] = media_helpers.probe_video_color_metadata("clip.mp4")

    assert results["tagged"] == {"color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
                                 "color_range": "tv", "color_probed": True}
    assert results["triple"]["color_space"] == "bt709"
    assert results["triple"]["color_primaries"] == ""
    assert results["untagged"] == {"color_space": "", "color_transfer": "", "color_primaries": "",
                                   "color_range": "", "color_probed": True}
    assert results["prores"]["color_space"] == "bt709"
    assert results["prores"]["color_range"] == ""
    assert results["rgb"]["color_space"] == "rgb"

    # neither tool produced stream info -> not probed
    monkeypatch.setattr(media_helpers, "_ffmpeg_input_text", lambda path: "")
    result = media_helpers.probe_video_color_metadata("clip.mp4")
    assert result["color_probed"] is False


def test_asset_color_fields_roundtrip():
    asset = Asset(asset_type="video", path="media/a.mp4",
                  color_space="bt709", color_transfer="bt709", color_primaries="bt709",
                  color_range="tv", color_probed=True)
    data = asset.to_dict()
    restored = Asset.from_dict(data)
    assert restored.color_space == "bt709"
    assert restored.color_range == "tv"
    assert restored.color_probed is True

    legacy = Asset.from_dict({"asset_id": "x", "path": "media/old.mp4", "asset_type": "video"})
    assert legacy.color_space == ""
    assert legacy.color_probed is False


def test_apply_color_metadata_updates_and_reports_changes():
    asset = Asset(asset_type="video", path="media/a.mp4")
    metadata = {"color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
                "color_range": "tv", "color_probed": True}
    assert apply_color_metadata(asset, metadata) is True
    assert asset.color_space == "bt709" and asset.color_probed is True
    assert apply_color_metadata(asset, metadata) is False
    assert apply_color_metadata(asset, {}) is True
    assert asset.color_space == "" and asset.color_probed is False


def test_project_asset_for_source_path_normalizes():
    project = TimelineProject(project_dir="X:/nowhere", name="p")
    asset = Asset(asset_type="video", path="media/Clip A.mp4")
    project.assets.append(asset)
    assert project.asset_for_source_path("media/Clip A.mp4") is asset
    assert project.asset_for_source_path("media\\Clip A.mp4") is asset
    assert project.asset_for_source_path("./media/clip a.MP4") is asset
    assert project.asset_for_source_path("media/other.mp4") is None
    assert project.asset_for_source_path("") is None
