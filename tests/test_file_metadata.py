import importlib
import json
import sys
import types
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"


def _import_media_helpers():
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")


def test_png_metadata_round_trip_uncompressed_text_chunk(tmp_path):
    media = _import_media_helpers()
    Image = importlib.import_module("PIL.Image")
    path = tmp_path / "meta.png"
    workflow = json.dumps({"nodes": [{"id": 1, "type": "Node"}]})

    media.write_png(str(path), np.zeros((1, 1, 3), dtype=np.uint8), metadata={"workflow": workflow})

    assert b"tEXtworkflow\x00" in path.read_bytes()
    with Image.open(path) as image:
        assert image.info["workflow"] == workflow


def test_extract_embedded_workflow_metadata_from_png(tmp_path):
    media = _import_media_helpers()
    path = tmp_path / "meta.png"
    workflow = {"nodes": [{"id": 1, "type": "Node"}]}

    media.write_png(str(path), np.zeros((1, 1, 3), dtype=np.uint8), metadata={"workflow": json.dumps(workflow)})

    assert media.extract_embedded_workflow_metadata(str(path)) == workflow


def test_encode_video_metadata_command_assembled(tmp_path, monkeypatch):
    media = _import_media_helpers()
    captured = {}

    def fake_run(cmd, frames, timeout=90, cancel_event=None):
        captured["cmd"] = list(cmd)
        Path(tmp_path / "out.mp4").write_bytes(b"video")

    monkeypatch.setattr(media, "_run_ffmpeg_streaming_frames", fake_run)
    monkeypatch.setattr(media, "get_ffmpeg_path", lambda: "ffmpeg")

    media.encode_video(
        np.zeros((1, 2, 2, 3), dtype=np.uint8),
        preset_id="Compatible MP4",
        output_path=str(tmp_path / "out.mp4"),
        fps=24,
        embed_metadata={"workflow": "{}"},
    )

    cmd = captured["cmd"]
    assert "-map_metadata" in cmd
    assert cmd[cmd.index("-map_metadata") + 1] == "1"
    assert "+faststart+use_metadata_tags" in cmd
    assert ["-map", "0:v:0"] == cmd[cmd.index("-map"):cmd.index("-map") + 2]


def test_encode_video_metadata_with_audio_map_intact(tmp_path, monkeypatch):
    media = _import_media_helpers()
    captured = {}
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"audio")

    def fake_run(cmd, frames, timeout=90, cancel_event=None):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(media, "_run_ffmpeg_streaming_frames", fake_run)
    monkeypatch.setattr(media, "get_ffmpeg_path", lambda: "ffmpeg")

    media.encode_video(
        np.zeros((1, 2, 2, 3), dtype=np.uint8),
        preset_id="Compatible MP4",
        output_path=str(tmp_path / "out.mp4"),
        fps=24,
        audio_path=str(audio_path),
        embed_metadata={"workflow": "{}"},
    )

    cmd = captured["cmd"]
    assert cmd[cmd.index("-map_metadata") + 1] == "2"
    assert "-map" in cmd
    video_map = cmd.index("-map")
    audio_map = cmd.index("-map", video_map + 1)
    assert cmd[video_map:video_map + 2] == ["-map", "0:v:0"]
    assert cmd[audio_map:audio_map + 2] == ["-map", "1:a:0"]


def test_encode_video_no_metadata_kwarg_keeps_old_command(tmp_path, monkeypatch):
    media = _import_media_helpers()
    captured = {}

    def fake_run(cmd, frames, timeout=90, cancel_event=None):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(media, "_run_ffmpeg_streaming_frames", fake_run)
    monkeypatch.setattr(media, "get_ffmpeg_path", lambda: "ffmpeg")

    media.encode_video(
        np.zeros((1, 2, 2, 3), dtype=np.uint8),
        preset_id="Compatible MP4",
        output_path=str(tmp_path / "out.mp4"),
        fps=24,
    )

    assert "-map_metadata" not in captured["cmd"]


def test_ffmeta_escaping():
    media = _import_media_helpers()

    assert media._escape_ffmetadata("\\=;#\n\r") == "\\\\\\=\\;\\#\\n\\r"
