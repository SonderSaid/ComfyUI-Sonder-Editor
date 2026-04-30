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
    timeline_state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    thumbnail_service = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")

    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True, exist_ok=True)
    project = timeline_state.TimelineProject(project_dir=str(project_dir), name="Fidelity Diagnostics")
    frames_rgb = _make_diagnostic_rgb_frames(frame_count=4)
    frames = _rgb_to_tensor(frames_rgb)
    captured: list[list[str]] = []
    real_run = subprocess.run

    def capture_run(cmd, *args, **kwargs):
        captured.append([str(part) for part in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(thumbnail_service, "ensure_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(io_nodes, "_get_ffmpeg", lambda: ffmpeg)
    monkeypatch.setattr(io_nodes.subprocess, "run", capture_run)

    save_result = io_nodes.SonderSaveVideo().save_video(
        project,
        frames,
        filename_prefix="diagnostic",
        fps=24.0,
        mode="Video",
        mark_queue_complete=False,
        codec="libx264",
        quality=23,
    )
    preview_result = io_nodes.SonderPreviewVideo().preview(frames, fps=24.0)
    monkeypatch.setattr(io_nodes.subprocess, "run", real_run)

    save_path = Path(save_result["result"][0])
    preview_video = preview_result["ui"]["videos"][0]["filename"]
    preview_path = tmp_path / "temp" / preview_video
    assert save_path.is_file()
    assert preview_path.is_file()
    assert len(project.assets) == 1

    save_tail = captured[0][captured[0].index("-c:v") :]
    preview_tail = captured[1][captured[1].index("-c:v") :]
    assert save_tail[:6] == ["-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p"]
    assert preview_tail[:6] == ["-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p"]

    height, width = frames_rgb.shape[1:3]
    save_metrics = _metrics(frames_rgb, _decode_ffmpeg_rgb(ffmpeg, save_path, width, height))
    preview_metrics = _metrics(frames_rgb, _decode_ffmpeg_rgb(ffmpeg, preview_path, width, height))
    print("VIDEO_FIDELITY_NODE_PATHS=" + json.dumps({"save": save_metrics, "preview": preview_metrics}, sort_keys=True))
    assert save_metrics["max_delta"] > 0
    assert preview_metrics["mae"] == pytest.approx(save_metrics["mae"], abs=0.75)


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
    wildcard_route = "/sonder-editor/project/{project_id}/assets/{asset_id}"
    assert extract_route in route_paths
    assert wildcard_route in route_paths
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
