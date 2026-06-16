"""Diagnostic coverage for Sonder video/image fidelity paths.

These tests intentionally measure encode/decode behavior instead of changing
production defaults. Assertions keep the diagnostic harness honest and skip
optional codecs when the local ffmpeg build does not support them.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TEST_PACKAGE = "video_editor_testpkg"


def _ensure_test_package() -> None:
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg


class _FakeStreamingStdin:
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    def write(self, data):
        if self.proc.write_error is not None:
            raise self.proc.write_error
        payload = bytes(data)
        self.proc.captured["writes"].append(payload)
        return len(payload)

    def close(self):
        self.closed = True


class _FakeStreamingProcess:
    def __init__(self, cmd, captured, *, kwargs, returncode=0, stderr=b"", write_error=None, wait_error=None):
        self.cmd = cmd
        self.captured = captured
        self.returncode = returncode
        self.write_error = write_error
        self.wait_error = wait_error
        self.killed = False
        self.terminated = False
        self.stdin = _FakeStreamingStdin(self)
        captured["cmds"].append([str(part) for part in cmd])
        captured["kwargs"].append(kwargs)
        captured["processes"].append(self)
        if stderr:
            kwargs["stderr"].write(stderr)
            kwargs["stderr"].flush()

    def wait(self, timeout=None):
        self.captured["wait_timeouts"].append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def _install_fake_streaming_popen(media_helpers, monkeypatch, **process_kwargs):
    captured = {"cmds": [], "kwargs": [], "processes": [], "wait_timeouts": [], "writes": []}

    def fake_popen(cmd, **kwargs):
        return _FakeStreamingProcess(cmd, captured, kwargs=kwargs, **process_kwargs)

    monkeypatch.setattr(media_helpers.subprocess, "Popen", fake_popen)
    return captured


def _install_folder_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    input_dir = tmp_path / "input"
    output_dir.mkdir(exist_ok=True)
    temp_dir.mkdir(exist_ok=True)
    input_dir.mkdir(exist_ok=True)
    folder_paths = types.SimpleNamespace(
        get_output_directory=lambda: str(output_dir),
        get_temp_directory=lambda: str(temp_dir),
        get_input_directory=lambda: str(input_dir),
        filter_files_content_types=lambda files, content_types: files,
    )
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)
    return folder_paths


def _import_io_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    _install_folder_paths(tmp_path, monkeypatch)
    _ensure_test_package()
    sys.modules.pop(f"{TEST_PACKAGE}.nodes.io_nodes", None)
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.io_nodes")


def _import_editor_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    _install_folder_paths(tmp_path, monkeypatch)
    _ensure_test_package()
    sys.modules.pop(f"{TEST_PACKAGE}.nodes.editor_node", None)
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.editor_node")


def _import_bridge_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    _install_folder_paths(tmp_path, monkeypatch)
    _ensure_test_package()
    sys.modules.pop(f"{TEST_PACKAGE}.nodes.bridge_nodes", None)
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.bridge_nodes")


def _load_route_module(monkeypatch: pytest.MonkeyPatch):
    from aiohttp import web

    sys.path.insert(0, str(ROOT))
    import server

    fake_prompt_server = types.SimpleNamespace(instance=types.SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    import server.routes as routes
    return importlib.reload(routes)


def _route_handler(route_module, method: str, path: str):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


class _JsonRequest:
    def __init__(self, *, match_info=None, query=None, body=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body or {}

    async def json(self):
        return self._body


class _MultipartField:
    def __init__(self, name: str, payload):
        self.name = name
        self._payload = payload

    async def read(self, decode=False):
        return self._payload

    async def text(self):
        if isinstance(self._payload, bytes):
            return self._payload.decode("utf-8")
        return str(self._payload)


class _MultipartReader:
    def __init__(self, fields):
        self._fields = list(fields)
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._fields):
            raise StopAsyncIteration
        field = self._fields[self._idx]
        self._idx += 1
        return field


class _MultipartRequest:
    def __init__(self, fields, *, match_info=None, query=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self._reader = _MultipartReader(fields)

    async def multipart(self):
        return self._reader


def _require_ffmpeg(io_nodes) -> str:
    ffmpeg = io_nodes._get_ffmpeg()
    if ffmpeg == "ffmpeg" and shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg unavailable")
    if ffmpeg != "ffmpeg" and not Path(ffmpeg).is_file():
        pytest.skip(f"ffmpeg path is unavailable: {ffmpeg}")
    return ffmpeg


def _probe_ffprobe(ffmpeg: str, media_path: Path) -> dict:
    candidates: list[str] = []
    if ffmpeg != "ffmpeg":
        ffmpeg_path = Path(ffmpeg)
        suffix = ".exe" if os.name == "nt" else ""
        candidates.append(str(ffmpeg_path.with_name(f"ffprobe{suffix}")))
    path_probe = shutil.which("ffprobe")
    if path_probe:
        candidates.append(path_probe)

    ffprobe = next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)
    if not ffprobe:
        return {"available": False}

    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return {"available": True, "error": result.stderr.strip()[:240]}
    data = json.loads(result.stdout or "{}")
    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "available": True,
        "codec": video_stream.get("codec_name", ""),
        "pix_fmt": video_stream.get("pix_fmt", ""),
        "color_space": video_stream.get("color_space", ""),
        "color_range": video_stream.get("color_range", ""),
        "color_transfer": video_stream.get("color_transfer", ""),
    }


def _make_diagnostic_rgb_frames(frame_count: int = 8, width: int = 160, height: int = 96):
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")

    frames: list[object] = []
    colors = np.array(
        [
            [255, 0, 0],
            [0, 255, 0],
            [0, 0, 255],
            [255, 255, 0],
            [0, 255, 255],
            [255, 0, 255],
            [255, 255, 255],
            [0, 0, 0],
        ],
        dtype=np.uint8,
    )
    for frame_index in range(frame_count):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        band_w = max(1, width // len(colors))
        for idx, color in enumerate(colors):
            frame[:, idx * band_w : (idx + 1) * band_w] = color

        ramp = np.linspace(0, 255, width, dtype=np.uint8)
        frame[height // 4 : height // 2, :, :] = ramp.reshape(1, width, 1)

        x_grad = np.linspace(0, 255, width, dtype=np.uint8)
        lower_h = height - (height // 2)
        y_grad = np.linspace(0, 255, lower_h, dtype=np.uint8)
        frame[height // 2 :, :, 0] = x_grad.reshape(1, width)
        frame[height // 2 :, :, 1] = y_grad.reshape(lower_h, 1)
        frame[height // 2 :, :, 2] = (frame_index * 31) % 256

        edge = np.zeros((height // 4, width, 3), dtype=np.uint8)
        edge[:, 0::2] = [255, 0, 0]
        edge[:, 1::2] = [0, 0, 255]
        frame[: height // 4] = edge

        cv2.putText(
            frame,
            f"F{frame_index:02d}",
            (8, height - 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        frames.append(frame)
    return np.stack(frames, axis=0)


def _rgb_to_tensor(frames_rgb):
    torch = pytest.importorskip("torch")
    return torch.from_numpy(frames_rgb.astype("float32") / 255.0)


def _trunc_rgb_uint8(tensor):
    return (tensor.detach().cpu().numpy() * 255.0).clip(0, 255).astype("uint8")


def _rounded_rgb_uint8(tensor):
    np = pytest.importorskip("numpy")
    return np.rint((tensor.detach().cpu().numpy() * 255.0).clip(0, 255)).astype("uint8")


def _metrics(reference_rgb, decoded_rgb) -> dict:
    np = pytest.importorskip("numpy")

    frames = min(len(reference_rgb), len(decoded_rgb))
    if frames <= 0:
        return {"frames": 0, "available": False}
    ref = reference_rgb[:frames].astype(np.float32)
    got = decoded_rgb[:frames].astype(np.float32)
    diff = got - ref
    abs_diff = np.abs(diff)
    mse = float(np.mean(diff ** 2))
    psnr = float("inf") if mse == 0.0 else float(20.0 * np.log10(255.0 / np.sqrt(mse)))
    return {
        "available": True,
        "frames": int(frames),
        "mae": float(np.mean(abs_diff)),
        "max_delta": int(np.max(abs_diff)),
        "rmse": float(np.sqrt(mse)),
        "psnr": psnr,
        "channel_mae": [float(v) for v in np.mean(abs_diff, axis=(0, 1, 2))],
    }


def _encode_raw_bgr(ffmpeg: str, frames_rgb, output_path: Path, args: list[str]) -> tuple[bool, str, list[str]]:
    cv2 = pytest.importorskip("cv2")

    bgr_frames = [cv2.cvtColor(frame, cv2.COLOR_RGB2BGR) for frame in frames_rgb]
    height, width = bgr_frames[0].shape[:2]
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        "24",
        "-i",
        "pipe:0",
        *args,
        str(output_path),
    ]
    raw_bytes = b"".join(frame.tobytes() for frame in bgr_frames)
    result = subprocess.run(cmd, input=raw_bytes, capture_output=True, timeout=60)
    return result.returncode == 0 and output_path.is_file(), result.stderr.decode(errors="replace")[:300], cmd


def _decode_ffmpeg_rgb(ffmpeg: str, media_path: Path, width: int, height: int):
    np = pytest.importorskip("numpy")

    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:300])
    frame_size = width * height * 3
    frame_count = len(result.stdout) // frame_size
    return np.frombuffer(result.stdout[: frame_count * frame_size], dtype=np.uint8).reshape(
        frame_count,
        height,
        width,
        3,
    )


def _decode_opencv_rgb(media_path: Path):
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")

    cap = cv2.VideoCapture(str(media_path))
    frames = []
    try:
        if not cap.isOpened():
            return np.zeros((0, 1, 1, 3), dtype=np.uint8)
        while True:
            ok, frame_bgr = cap.read()
            if not ok:
                break
            frames.append(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    finally:
        cap.release()
    if not frames:
        return np.zeros((0, 1, 1, 3), dtype=np.uint8)
    return np.stack(frames, axis=0)


def _write_png_sequence(frames_rgb, directory: Path):
    pytest.importorskip("PIL")
    from PIL import Image
    import numpy as np

    directory.mkdir(parents=True, exist_ok=True)
    decoded = []
    for idx, frame in enumerate(frames_rgb):
        path = directory / f"frame_{idx:03d}.png"
        Image.fromarray(frame).save(path)
        decoded.append(np.asarray(Image.open(path).convert("RGB")))
    return np.stack(decoded, axis=0)


def _run_encode_matrix(tmp_path: Path, ffmpeg: str, frames_rgb) -> list[dict]:
    height, width = frames_rgb.shape[1:3]
    results = [
        {
            "name": "png_sequence_reference",
            "status": "ok",
            "ffmpeg": _metrics(frames_rgb, _write_png_sequence(frames_rgb, tmp_path / "png_sequence")),
            "opencv": {"available": False},
            "metadata": {"available": False, "container": "png_sequence"},
            "command_tail": ["PIL.Image.save"],
        }
    ]
    variants = [
        ("current_sonder_h264", ".mp4", ["-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p"]),
        ("sonder_preview_h264", ".mp4", ["-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p"]),
        ("vhs_like_h264", ".mp4", ["-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p"]),
        ("high_quality_h264_420", ".mp4", ["-c:v", "libx264", "-crf", "10", "-preset", "slow", "-pix_fmt", "yuv420p"]),
        ("high_quality_h264_444", ".mp4", ["-c:v", "libx264", "-crf", "10", "-preset", "slow", "-pix_fmt", "yuv444p"]),
        ("ffv1_reference", ".mkv", ["-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "rgb24"]),
        ("prores_reference", ".mov", ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuv444p10le"]),
    ]
    for name, suffix, args in variants:
        output_path = tmp_path / f"{name}{suffix}"
        ok, error, cmd = _encode_raw_bgr(ffmpeg, frames_rgb, output_path, args)
        if not ok:
            results.append({"name": name, "status": "skipped", "error": error, "command_tail": args})
            continue
        ffmpeg_decoded = _decode_ffmpeg_rgb(ffmpeg, output_path, width, height)
        opencv_decoded = _decode_opencv_rgb(output_path)
        results.append(
            {
                "name": name,
                "status": "ok",
                "ffmpeg": _metrics(frames_rgb, ffmpeg_decoded),
                "opencv": _metrics(frames_rgb, opencv_decoded),
                "metadata": _probe_ffprobe(ffmpeg, output_path),
                "command_tail": cmd[cmd.index("-i") + 2 :],
            }
        )
    return results


def _row(results: list[dict], name: str) -> dict:
    return next(row for row in results if row["name"] == name)


def test_tensor_conversion_truncation_vs_rounding_quantifies_lsb_bias(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)

    probe = torch.tensor(
        [[[[0.5 / 255.0, 1.5 / 255.0, 2.5 / 255.0], [127.5 / 255.0, 128.5 / 255.0, 254.5 / 255.0]]]],
        dtype=torch.float32,
    )
    current_rgb = _trunc_rgb_uint8(probe)
    rounded_rgb = _rounded_rgb_uint8(probe)
    current_bgr = io_nodes._tensor_to_frames(probe)[0]

    assert current_rgb.tolist() == [[[[0, 1, 2], [127, 128, 254]]]]
    assert rounded_rgb.tolist() == [[[[0, 2, 2], [128, 128, 254]]]]
    assert current_bgr[0, 0].tolist() == [2, 1, 0]
    assert int((rounded_rgb.astype("int16") - current_rgb.astype("int16")).max()) == 1


def test_ffmpeg_codec_matrix_records_video_fidelity_diagnostics(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    ffmpeg = _require_ffmpeg(io_nodes)
    frames_rgb = _make_diagnostic_rgb_frames()

    results = _run_encode_matrix(tmp_path, ffmpeg, frames_rgb)
    print("VIDEO_FIDELITY_CODEC_MATRIX=" + json.dumps(results, sort_keys=True))

    png = _row(results, "png_sequence_reference")
    assert png["ffmpeg"]["max_delta"] == 0
    assert png["ffmpeg"]["psnr"] == float("inf")

    current = _row(results, "current_sonder_h264")
    if current["status"] != "ok":
        pytest.skip(f"libx264 current Sonder encode unavailable: {current.get('error', '')}")
    assert current["ffmpeg"]["max_delta"] > 0

    preview = _row(results, "sonder_preview_h264")
    if preview["status"] == "ok":
        assert preview["command_tail"][:6] == current["command_tail"][:6]
        assert preview["ffmpeg"]["mae"] == pytest.approx(current["ffmpeg"]["mae"], abs=1e-6)

    hq444 = _row(results, "high_quality_h264_444")
    if hq444["status"] == "ok":
        assert hq444["ffmpeg"]["mae"] < current["ffmpeg"]["mae"]

    ffv1 = _row(results, "ffv1_reference")
    if ffv1["status"] == "ok":
        assert ffv1["ffmpeg"]["max_delta"] <= 1


def test_sonder_save_and_preview_real_ffmpeg_paths_match_diagnostic_flags(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    ffmpeg = _require_ffmpeg(io_nodes)
    torch = pytest.importorskip("torch")
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    project = timeline_state.TimelineProject(project_dir=str(project_dir), name="Fidelity Diagnostics")
    frames_rgb = _make_diagnostic_rgb_frames(frame_count=4)
    frames = _rgb_to_tensor(frames_rgb)
    captured_popen: list[list[str]] = []
    captured_run: list[list[str]] = []
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def capture_popen(cmd, *args, **kwargs):
        captured_popen.append([str(part) for part in cmd])
        return real_popen(cmd, *args, **kwargs)

    def capture_run(cmd, *args, **kwargs):
        captured_run.append([str(part) for part in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_get_ffmpeg", lambda: ffmpeg)
    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: ffmpeg)
    monkeypatch.setattr(media_helpers.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(media_helpers.subprocess, "run", capture_run)

    save_result = io_nodes.SonderSaveVideo().save_video(
        project,
        frames,
        filename_prefix="diagnostic",
        fps=24.0,
        mode="Video",
        mark_queue_complete=False,
        save_preset="Compatible MP4",
    )
    preview_result = io_nodes.SonderPreviewVideo().preview(frames, fps=24.0)
    monkeypatch.setattr(media_helpers.subprocess, "Popen", real_popen)
    monkeypatch.setattr(media_helpers.subprocess, "run", real_run)

    save_path = Path(save_result["result"][0])
    preview_video = preview_result["ui"]["videos"][0]["filename"]
    preview_path = tmp_path / "temp" / preview_video
    assert save_path.is_file()
    assert preview_path.is_file()
    assert len(project.assets) == 1

    save_cmd = next(cmd for cmd in captured_popen if "-preset" in cmd and "slow" in cmd)
    preview_cmd = captured_run[0]
    save_tail = save_cmd[save_cmd.index("-c:v") :]
    preview_tail = preview_cmd[preview_cmd.index("-c:v") :]
    assert save_tail[:10] == ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    assert preview_tail[:6] == ["-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p"]
    assert project.assets[0].generation_params["save_preset"] == "Compatible MP4"
    assert project.assets[0].generation_params["tensor_mode"] == "round"

    height, width = frames_rgb.shape[1:3]
    save_metrics = _metrics(frames_rgb, _decode_ffmpeg_rgb(ffmpeg, save_path, width, height))
    preview_metrics = _metrics(frames_rgb, _decode_ffmpeg_rgb(ffmpeg, preview_path, width, height))
    print("VIDEO_FIDELITY_NODE_PATHS=" + json.dumps({"save": save_metrics, "preview": preview_metrics}, sort_keys=True))
    assert save_metrics["max_delta"] > 0
    assert save_metrics["mae"] <= preview_metrics["mae"] + 0.75


def test_encode_video_preset_commands_and_tensor_modes(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    frames = np.zeros((1, 4, 4, 3), dtype=np.uint8)
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    captured = _install_fake_streaming_popen(media_helpers, monkeypatch)

    assert media_helpers.SAVE_VIDEO_PRESET_ORDER[0] == "Compatible MP4"
    assert media_helpers.SAVE_VIDEO_PRESET_ORDER[-1] == "Custom"
    assert "Legacy" not in media_helpers.SAVE_VIDEO_PRESET_ORDER
    assert media_helpers.normalize_save_preset("Legacy") == "Compatible MP4"

    cases = [
        ("Compatible MP4", ".mp4", "libx264", "yuv420p", "round", ["-c:a", "aac", "-b:a", "192k"]),
        ("High Quality MP4", ".mp4", "libx264", "yuv420p", "round", ["-c:a", "aac", "-b:a", "256k"]),
        ("Editing Master MP4", ".mp4", "libx264", "yuv444p", "round", ["-c:a", "aac", "-b:a", "256k"]),
        ("ProRes 422 HQ", ".mov", "prores_ks", "yuv422p10le", "round", ["-c:a", "pcm_s16le"]),
        ("Lossless FFV1 (RGB)", ".mkv", "ffv1", "gbrp", "round", ["-c:a", "flac"]),
    ]
    for preset, extension, codec, pix_fmt, tensor_mode, audio_args in cases:
        meta = media_helpers.encode_video(
            frames,
            preset_id=preset,
            output_path=str(tmp_path / f"out{extension}"),
            fps=24,
            audio_path=str(audio_path),
        )
        cmd = captured["cmds"][-1]
        assert meta["save_preset"] == preset
        assert meta["codec"] == codec
        assert meta["pix_fmt"] == pix_fmt
        assert meta["tensor_mode"] == tensor_mode
        assert cmd[cmd.index("-c:v") + 1] == codec
        assert cmd[cmd.index("-pix_fmt", cmd.index("-c:v")) + 1] == pix_fmt
        for idx, arg in enumerate(audio_args):
            assert cmd[cmd.index(audio_args[0]) + idx] == arg

    custom_options = {
        "custom_output_kind": "Video File",
        "custom_container": "mp4",
        "custom_video_codec": "libx265",
        "custom_pix_fmt": "yuv420p",
        "custom_crf": 21,
        "custom_encoder_preset": "medium",
        "custom_audio_codec": "aac",
        "custom_audio_bitrate_kbps": 320,
    }
    meta = media_helpers.encode_video(
        frames,
        preset_id="Custom",
        output_path=str(tmp_path / "custom.mp4"),
        fps=24,
        audio_path=str(audio_path),
        custom_options=custom_options,
    )
    cmd = captured["cmds"][-1]
    assert meta["save_preset"] == "Custom"
    assert meta["codec"] == "libx265"
    assert meta["pix_fmt"] == "yuv420p"
    assert meta["container"] == "mp4"
    assert meta["tensor_mode"] == "round"
    assert meta["custom_crf"] == 21
    assert meta["custom_audio_bitrate_kbps"] == 320
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert cmd[cmd.index("-crf") + 1] == "21"
    assert cmd[cmd.index("-preset") + 1] == "medium"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-b:a") + 1] == "320k"
    assert cmd.count("-map") == 2
    assert cmd[cmd.index("-map") + 1] == "0:v:0"
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "1:a:0"

    prores_meta = media_helpers.encode_video(
        frames,
        preset_id="Custom",
        output_path=str(tmp_path / "custom.mov"),
        fps=24,
        audio_path=str(audio_path),
        custom_options={
            **custom_options,
            "custom_container": "mov",
            "custom_video_codec": "prores_ks",
            "custom_pix_fmt": "yuv422p10le",
            "custom_audio_codec": "pcm_s16le",
        },
    )
    prores_cmd = captured["cmds"][-1]
    assert prores_meta["codec"] == "prores_ks"
    assert prores_meta["audio_mode"] == "pcm_s16le"
    assert prores_cmd[prores_cmd.index("-profile:v") + 1] == "3"
    assert prores_cmd[prores_cmd.index("-c:a") + 1] == "pcm_s16le"

    ffv1_meta = media_helpers.encode_video(
        frames,
        preset_id="Custom",
        output_path=str(tmp_path / "custom.mkv"),
        fps=24,
        audio_path=str(audio_path),
        custom_options={
            **custom_options,
            "custom_container": "mkv",
            "custom_video_codec": "ffv1",
            "custom_pix_fmt": "gbrp",
            "custom_audio_codec": "flac",
        },
    )
    ffv1_cmd = captured["cmds"][-1]
    assert ffv1_meta["codec"] == "ffv1"
    assert ffv1_meta["pix_fmt"] == "gbrp"
    assert ffv1_cmd[ffv1_cmd.index("-slicecrc") + 1] == "1"
    assert ffv1_cmd[ffv1_cmd.index("-c:a") + 1] == "flac"

    no_audio_meta = media_helpers.encode_video(
        frames,
        preset_id="Custom",
        output_path=str(tmp_path / "custom_no_audio.mp4"),
        fps=24,
        audio_path=str(audio_path),
        custom_options={
            **custom_options,
            "custom_audio_codec": "none",
        },
    )
    no_audio_cmd = captured["cmds"][-1]
    assert no_audio_meta["audio_mode"] == "none"
    assert no_audio_cmd.count("-i") == 1
    assert "-map" not in no_audio_cmd
    assert "-c:a" not in no_audio_cmd


def test_save_video_encode_timeout_extends_large_high_cost_presets():
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")

    tiny_timeout = media_helpers.save_video_encode_timeout_seconds(
        "Compatible MP4",
        frame_count=2,
        width=2,
        height=2,
    )
    large_master_timeout = media_helpers.save_video_encode_timeout_seconds(
        "Editing Master MP4",
        frame_count=240,
        width=1920,
        height=1088,
    )
    custom_veryslow_timeout = media_helpers.save_video_encode_timeout_seconds(
        "Custom",
        frame_count=240,
        width=1920,
        height=1088,
        custom_options={
            "custom_output_kind": "Video File",
            "custom_container": "mp4",
            "custom_video_codec": "libx264",
            "custom_pix_fmt": "yuv444p",
            "custom_encoder_preset": "veryslow",
        },
    )

    assert tiny_timeout == media_helpers.MIN_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS
    assert large_master_timeout > media_helpers.MIN_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS
    assert custom_veryslow_timeout > media_helpers.MIN_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS
    assert large_master_timeout <= media_helpers.MAX_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS


def test_encode_video_streams_frames_incrementally(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    frames = np.arange(2 * 2 * 2 * 3, dtype=np.uint8).reshape((2, 2, 2, 3))
    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    captured = _install_fake_streaming_popen(media_helpers, monkeypatch)

    media_helpers.encode_video(
        frames,
        preset_id="Compatible MP4",
        output_path=str(tmp_path / "streamed.mp4"),
        fps=24,
    )

    kwargs = captured["kwargs"][0]
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] not in {subprocess.PIPE, subprocess.DEVNULL}
    assert captured["writes"] == [frames[0].tobytes(), frames[1].tobytes()]


def test_encode_video_streams_generator_after_first_frame_inference(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    frames = np.arange(2 * 2 * 2 * 3, dtype=np.uint8).reshape((2, 2, 2, 3))
    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    captured = _install_fake_streaming_popen(media_helpers, monkeypatch)

    def frame_generator():
        yield frames[0]
        assert captured["cmds"], "ffmpeg should start after first-frame inference"
        yield frames[1]

    media_helpers.encode_video(
        frame_generator(),
        preset_id="Compatible MP4",
        output_path=str(tmp_path / "streamed_generator.mp4"),
        fps=24,
    )

    assert captured["writes"] == [frames[0].tobytes(), frames[1].tobytes()]


def test_encode_video_rejects_empty_and_ragged_iterables(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    with pytest.raises(ValueError, match="one or more RGB frames"):
        media_helpers.encode_video(
            iter(()),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "empty.mp4"),
            fps=24,
        )

    with pytest.raises(ValueError, match="one or more RGB frames"):
        media_helpers.encode_video(
            iter([np.zeros((2, 2, 4), dtype=np.uint8)]),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "rgba.mp4"),
            fps=24,
        )

    captured = _install_fake_streaming_popen(media_helpers, monkeypatch)

    def ragged_generator():
        yield np.zeros((2, 2, 3), dtype=np.uint8)
        yield np.zeros((3, 2, 3), dtype=np.uint8)

    with pytest.raises(ValueError, match="same-sized RGB frames"):
        media_helpers.encode_video(
            ragged_generator(),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "ragged.mp4"),
            fps=24,
        )

    assert captured["processes"][0].terminated is True
    assert captured["processes"][0].stdin.closed is True


def test_encode_video_normalizes_generator_frame_dtype_per_frame(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    captured = _install_fake_streaming_popen(media_helpers, monkeypatch)

    frames = [
        np.full((2, 2, 3), 300.0, dtype=np.float32),
        np.full((2, 2, 3), 1.5, dtype=np.float32),
    ]

    media_helpers.encode_video(
        iter(frames),
        preset_id="Compatible MP4",
        output_path=str(tmp_path / "dtype.mp4"),
        fps=24,
    )

    assert captured["writes"][0] == bytes([255]) * 12
    assert captured["writes"][1] == bytes([1]) * 12


def test_encode_video_generator_cancel_terminates_process_and_closes_producer(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    cancel_event = threading.Event()
    closed = False
    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    captured = _install_fake_streaming_popen(media_helpers, monkeypatch)

    def frame_generator():
        nonlocal closed
        try:
            yield np.zeros((2, 2, 3), dtype=np.uint8)
            cancel_event.set()
            yield np.ones((2, 2, 3), dtype=np.uint8)
        finally:
            closed = True

    with pytest.raises(media_helpers.MediaOperationCancelled):
        media_helpers.encode_video(
            frame_generator(),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "cancel.mp4"),
            fps=24,
            cancel_event=cancel_event,
        )

    assert captured["processes"][0].terminated is True
    assert captured["processes"][0].stdin.closed is True
    assert closed is True


def test_encode_video_write_failure_closes_generator_producer(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    closed = False
    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    _install_fake_streaming_popen(
        media_helpers,
        monkeypatch,
        returncode=1,
        stderr=b"pipe closed",
        write_error=BrokenPipeError(),
    )

    def frame_generator():
        nonlocal closed
        try:
            yield np.zeros((2, 2, 3), dtype=np.uint8)
            yield np.ones((2, 2, 3), dtype=np.uint8)
        finally:
            closed = True

    with pytest.raises(RuntimeError, match="pipe closed"):
        media_helpers.encode_video(
            frame_generator(),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "write_failure.mp4"),
            fps=24,
        )

    assert closed is True


def test_encode_video_failure_closes_closeable_source_iterable(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    class CloseableFrames:
        def __init__(self):
            self.closed = False
            self.frames = [
                np.zeros((2, 2, 3), dtype=np.uint8),
                np.ones((2, 2, 3), dtype=np.uint8),
            ]

        def __iter__(self):
            return iter(self.frames)

        def close(self):
            self.closed = True

    producer = CloseableFrames()
    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    _install_fake_streaming_popen(
        media_helpers,
        monkeypatch,
        returncode=1,
        stderr=b"pipe closed",
        write_error=BrokenPipeError(),
    )

    with pytest.raises(RuntimeError, match="pipe closed"):
        media_helpers.encode_video(
            producer,
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "source_close.mp4"),
            fps=24,
        )

    assert producer.closed is True


def test_encode_video_streaming_nonzero_exit_includes_stderr(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    _install_fake_streaming_popen(media_helpers, monkeypatch, returncode=1, stderr=b"bad codec")

    with pytest.raises(RuntimeError, match="bad codec"):
        media_helpers.encode_video(
            np.zeros((1, 2, 2, 3), dtype=np.uint8),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "failed.mp4"),
            fps=24,
        )


def test_encode_video_streaming_timeout_kills_process(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    captured = _install_fake_streaming_popen(
        media_helpers,
        monkeypatch,
        stderr=b"still encoding",
        wait_error=subprocess.TimeoutExpired(["ffmpeg"], 90),
    )

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        media_helpers.encode_video(
            np.zeros((1, 2, 2, 3), dtype=np.uint8),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "timeout.mp4"),
            fps=24,
            timeout=90,
        )

    assert captured["processes"][0].killed is True
    assert exc_info.value.stderr == b"still encoding"


def test_encode_video_streaming_timer_timeout_reaps_process(tmp_path, monkeypatch):
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    class ImmediateTimer:
        daemon = False

        def __init__(self, timeout, callback):
            self.timeout = timeout
            self.callback = callback
            self.cancelled = False

        def start(self):
            self.callback()

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(media_helpers.threading, "Timer", ImmediateTimer)
    captured = _install_fake_streaming_popen(media_helpers, monkeypatch, stderr=b"timer fired")

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        media_helpers.encode_video(
            np.zeros((2, 2, 2, 3), dtype=np.uint8),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "timer_timeout.mp4"),
            fps=24,
            timeout=90,
        )

    assert captured["processes"][0].killed is True
    assert captured["wait_timeouts"] == [5]
    assert exc_info.value.stderr == b"timer fired"


def test_encode_video_streaming_broken_pipe_with_failure_exit_reports_error(tmp_path, monkeypatch):
    # A broken pipe together with a NON-ZERO ffmpeg exit is a real failure.
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    _install_fake_streaming_popen(
        media_helpers,
        monkeypatch,
        returncode=1,
        stderr=b"pipe closed",
        write_error=BrokenPipeError(),
    )

    with pytest.raises(RuntimeError, match="pipe closed"):
        media_helpers.encode_video(
            np.zeros((1, 2, 2, 3), dtype=np.uint8),
            preset_id="Compatible MP4",
            output_path=str(tmp_path / "pipe.mp4"),
            fps=24,
        )


def test_encode_video_streaming_broken_pipe_with_clean_exit_is_success(tmp_path, monkeypatch):
    # A broken pipe with a ZERO exit code is a benign shutdown race (ffmpeg
    # closed stdin via -shortest or a fast black-frame drain before the writer
    # finished) — ffmpeg produced a valid file, so encode_video must NOT raise.
    # Regression for the empty-space export failure (sonder_editor_bugs.md).
    _ensure_test_package()
    media_helpers = importlib.import_module(f"{TEST_PACKAGE}.server.media_helpers")
    np = pytest.importorskip("numpy")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    _install_fake_streaming_popen(
        media_helpers,
        monkeypatch,
        returncode=0,
        stderr=b"",
        write_error=BrokenPipeError(),
    )

    media_helpers.encode_video(
        np.zeros((1, 2, 2, 3), dtype=np.uint8),
        preset_id="Compatible MP4",
        output_path=str(tmp_path / "pipe.mp4"),
        fps=24,
    )


def test_save_video_custom_png_sequence_registers_image_assets(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "save_project", lambda project: None)

    single_dir = tmp_path / "single_project"
    (single_dir / "media").mkdir(parents=True, exist_ok=True)
    single_project = timeline_state.TimelineProject(project_dir=str(single_dir), name="Single PNG")
    single_frames = _rgb_to_tensor(_make_diagnostic_rgb_frames(frame_count=1, width=16, height=12))

    single_result = io_nodes.SonderSaveVideo().save_video(
        single_project,
        single_frames,
        filename_prefix="still",
        fps=24,
        mode="Video",
        save_preset="Custom",
        custom_output_kind="PNG Sequence",
        custom_png_compression=0,
    )

    single_path = Path(single_result["result"][0])
    assert single_path.is_file()
    assert single_path.name == "still.png"
    assert len(single_project.assets) == 1
    single_asset = single_project.assets[0]
    assert single_asset.asset_type == "image"
    assert single_asset.folder == ""
    assert single_asset.generation_params["save_preset"] == "Custom"
    assert single_asset.generation_params["custom_output_kind"] == "PNG Sequence"
    assert single_asset.generation_params["tensor_mode"] == "round"
    assert single_asset.generation_params["image_sequence"] is False
    assert single_asset.generation_params["sequence_index"] == 1

    multi_dir = tmp_path / "multi_project"
    (multi_dir / "media" / "seq").mkdir(parents=True, exist_ok=True)
    multi_project = timeline_state.TimelineProject(project_dir=str(multi_dir), name="Multi PNG")
    multi_frames = _rgb_to_tensor(_make_diagnostic_rgb_frames(frame_count=3, width=16, height=12))

    multi_result = io_nodes.SonderSaveVideo().save_video(
        multi_project,
        multi_frames,
        filename_prefix="seq",
        fps=24,
        mode="Video",
        save_preset="Custom",
        custom_output_kind="PNG Sequence",
        custom_png_compression=3,
    )

    folder_path = Path(multi_result["result"][0])
    assert folder_path.is_dir()
    assert folder_path.name == "seq_1"
    assert sorted(path.name for path in folder_path.glob("*.png")) == ["seq_0001.png", "seq_0002.png", "seq_0003.png"]
    assert len(multi_project.assets) == 3
    assert multi_project.metadata["asset_folders"] == ["seq_1"]
    for index, asset in enumerate(multi_project.assets, start=1):
        assert asset.asset_type == "image"
        assert asset.folder == "seq_1"
        assert asset.name == f"seq_{index:04d}.png"
        assert asset.generation_params["image_sequence"] is True
        assert asset.generation_params["sequence_folder"] == "seq_1"
        assert asset.generation_params["sequence_total"] == 3
        assert asset.generation_params["sequence_index"] == index
        assert asset.generation_params["custom_png_compression"] == 3


def test_save_video_rejects_take_mode_png_sequence(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    project_dir = tmp_path / "project"
    project = timeline_state.TimelineProject(project_dir=str(project_dir), name="Reject PNG Take")
    frames = _rgb_to_tensor(_make_diagnostic_rgb_frames(frame_count=2, width=8, height=8))

    with pytest.raises(ValueError, match="PNG Sequence.*Take mode"):
        io_nodes.SonderSaveVideo().save_video(
            project,
            frames,
            filename_prefix="take_png",
            fps=24,
            mode="Take",
            save_preset="Custom",
            custom_output_kind="PNG Sequence",
        )


def test_guide_extraction_and_loader_paths_are_measured(tmp_path, monkeypatch):
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    ffmpeg = _require_ffmpeg(io_nodes)
    editor_node = _import_editor_node(tmp_path, monkeypatch)
    bridge_nodes = _import_bridge_nodes(tmp_path, monkeypatch)
    route_module = _load_route_module(monkeypatch)
    pytest.importorskip("PIL")
    from PIL import Image
    import numpy as np
    import server.timeline_state as route_state

    frames_rgb = _make_diagnostic_rgb_frames(frame_count=3, width=128, height=72)
    image_path = tmp_path / "guide_source.png"
    Image.fromarray(frames_rgb[0]).save(image_path)

    video_path = tmp_path / "guide_source.mkv"
    ok, error, _cmd = _encode_raw_bgr(
        ffmpeg,
        frames_rgb,
        video_path,
        ["-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "rgb24"],
    )
    if not ok:
        video_path = tmp_path / "guide_source.mp4"
        ok, error, _cmd = _encode_raw_bgr(
            ffmpeg,
            frames_rgb,
            video_path,
            ["-c:v", "libx264", "-crf", "10", "-preset", "slow", "-pix_fmt", "yuv444p"],
        )
    if not ok:
        pytest.skip(f"no usable video codec for guide diagnostics: {error}")

    extracted_path = tmp_path / "ffmpeg_extract.png"
    extracted_size = route_module._extract_video_frame_ffmpeg(str(video_path), 0, str(extracted_path))
    assert extracted_size == (128, 72)

    editor = editor_node.SonderEditor()
    editor_image = (editor._load_guide_image(str(image_path), "image", 128, 72).numpy() * 255.0).round().astype("uint8")
    editor_video = (editor._load_guide_image(str(video_path), "video", 128, 72).numpy() * 255.0).round().astype("uint8")
    bridge_image = (bridge_nodes._load_guide_image_bridge(str(image_path), "image", 128, 72).numpy() * 255.0).round().astype("uint8")
    bridge_video = (bridge_nodes._load_guide_image_bridge(str(video_path), "video", 128, 72).numpy() * 255.0).round().astype("uint8")
    extracted_rgb = np.asarray(Image.open(extracted_path).convert("RGB"))

    route_paths = [route.path for route in route_module.routes]
    extract_route = "/sonder-editor/project/{project_id}/assets/extract_frame"
    snapshot_route = "/sonder-editor/project/{project_id}/assets/viewport_snapshot"
    wildcard_route = "/sonder-editor/project/{project_id}/assets/{asset_id}"
    assert extract_route in route_paths
    assert snapshot_route in route_paths
    assert wildcard_route in route_paths
    assert route_paths.index(snapshot_route) < route_paths.index(wildcard_route)
    assert route_paths.index(extract_route) < route_paths.index(wildcard_route)

    project_dir = tmp_path / "route_project"
    media_dir = project_dir / "media"
    media_dir.mkdir(parents=True)
    route_video = media_dir / "route_source.mp4"
    route_video.write_bytes(video_path.read_bytes())
    project = route_state.TimelineProject(project_dir=str(project_dir), name="Route Diagnostics")
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    monkeypatch.setattr(route_module, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(route_module, "_get_ffmpeg", lambda: ffmpeg)

    handler = _route_handler(route_module, "POST", extract_route)
    response = asyncio.run(
        handler(_JsonRequest(match_info={"project_id": "project"}, body={"source_path": "media/route_source.mp4", "frame_index": 0}))
    )
    assert response.status == 201
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["asset_type"] == "image"
    assert Path(project_dir / payload["path"]).is_file()
    assert payload["generation_params"]["extraction_mode"] in {"ffmpeg", "opencv_fallback"}

    response_scaled = asyncio.run(
        handler(_JsonRequest(match_info={"project_id": "project"}, body={
            "source_path": "media/route_source.mp4",
            "frame_index": 0,
            "target_long_edge": 64,
        }))
    )
    assert response_scaled.status == 201
    scaled_payload = json.loads(response_scaled.body.decode("utf-8"))
    assert (scaled_payload["width"], scaled_payload["height"]) == (64, 36)
    assert scaled_payload["generation_params"]["target_long_edge"] == 64

    project.add_asset(route_state.Asset(
        name="route_source.mp4",
        asset_type="video",
        path="media/route_source.mp4",
        width=128,
        height=72,
        frame_count=3,
        fps=24,
    ))
    from io import BytesIO

    png_bytes = BytesIO()
    Image.fromarray(frames_rgb[0]).save(png_bytes, format="PNG")
    snapshot_handler = _route_handler(route_module, "POST", snapshot_route)
    snapshot_response = asyncio.run(
        snapshot_handler(_MultipartRequest(
            [
                _MultipartField("metadata", json.dumps({
                    "source_path": "media/route_source.mp4",
                    "source_frame_index": 0,
                    "timeline_frame_index": 12,
                    "extraction_mode": "viewport_snapshot",
                    "snapshot_long_edge": 128,
                    "snapshot_source_long_edge": 128,
                })),
                _MultipartField("file", png_bytes.getvalue()),
            ],
            match_info={"project_id": "project"},
        ))
    )
    assert snapshot_response.status == 201
    snapshot_payload = json.loads(snapshot_response.body.decode("utf-8"))
    assert snapshot_payload["asset_type"] == "image"
    assert snapshot_payload["generation_params"]["extraction_mode"] == "viewport_snapshot"
    assert snapshot_payload["generation_params"]["timeline_frame_index"] == 12
    assert Path(project_dir / snapshot_payload["path"]).is_file()

    summary = {
        "direct_editor_image": _metrics(frames_rgb[:1], editor_image[None, ...]),
        "direct_bridge_image": _metrics(frames_rgb[:1], bridge_image[None, ...]),
        "ffmpeg_extract_png": _metrics(frames_rgb[:1], extracted_rgb[None, ...]),
        "editor_video_opencv": _metrics(frames_rgb[:1], editor_video[None, ...]),
        "bridge_video_opencv": _metrics(frames_rgb[:1], bridge_video[None, ...]),
        "route_status": response.status,
    }
    print("VIDEO_FIDELITY_GUIDE_PATHS=" + json.dumps(summary, sort_keys=True))
    assert summary["direct_editor_image"]["max_delta"] == 0
    assert summary["direct_bridge_image"]["max_delta"] == 0
    assert summary["ffmpeg_extract_png"]["available"] is True
    assert summary["editor_video_opencv"]["available"] is True
    assert summary["bridge_video_opencv"]["available"] is True
