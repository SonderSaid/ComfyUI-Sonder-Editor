"""Proxy media (``server/proxy_media.py``) and its ffmpeg half (``media_helpers``).

* One server authority decides whether an original needs a proxy: Save Video outputs from
  their recorded preset or codec with no probe, everything else from one probe.
* Every build gets a new rev and so a new file; the previous one is retired, and a file
  that cannot be removed yet is retried. Removal matches names exactly.
* Publishing re-checks the source under the asset's cache lock, which is held only to
  publish: a replace is never blocked by an encode, and a stale encode never lands.
* *When ComfyUI is idle* waits for a grace period of an empty queue, kills an encode
  when ComfyUI turns busy and requeues it keeping how long it has waited. Build now
  preempts an automatic build. *Only when I ask* drops automatic jobs only.
* Nothing here writes ``project.json``.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from server import comfy_idle, media_helpers, proxy_media as pmx, project_manager as pm, session_registry
from server.media_helpers import MediaOperationCancelled
from server.timeline_state import Asset
from legacy_projects import create_legacy_project


# ── Fixtures ────────────────────────────────────────────────────────────────

def ffmpeg(*args):
    subprocess.run([media_helpers.get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


SOURCES = {
    "prores.mov": ["-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24:duration=0.5",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
                   "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s24le", "-shortest"],
    "ffv1.mkv": ["-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24:duration=0.5",
                 "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gbrp"],
    "odd.mov": ["-f", "lavfi", "-i", "testsrc2=size=95x53:rate=24:duration=0.5",
                "-c:v", "prores_ks", "-pix_fmt", "yuv422p10le"],
    "vfr.mkv": ["-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24:duration=1",
                "-vf", "select='not(mod(n\\,3))+lt(n\\,4)'", "-fps_mode", "vfr",
                "-c:v", "libx264", "-pix_fmt", "yuv420p"],
    "h264.mp4": ["-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24:duration=0.5",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p"],
    "mjpeg.avi": ["-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24:duration=0.5",
                  "-c:v", "mjpeg", "-pix_fmt", "yuvj420p"],
}


@pytest.fixture(scope="session")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("proxy_sources")
    for name, args in SOURCES.items():
        ffmpeg(*args, str(root / name))
    return root


@pytest.fixture(autouse=True)
def quiet_idle():
    comfy_idle._reset_for_tests(0.0)
    yield
    comfy_idle._reset_for_tests(0.0)


def make_project(tmp_path, sources, files=("prores.mov",), *, legacy=True, metadata=None, name="Film"):
    base = tmp_path / "projects"
    base.mkdir(exist_ok=True)
    project = create_legacy_project(name, base) if legacy else pm.create_project(name, base_dir=str(base))
    assets = []
    for index, file in enumerate(files):
        target = Path(project.project_dir) / "media" / file
        shutil.copy(sources / file, target)
        assets.append(Asset(asset_id=f"a{index}", name=file, asset_type="video", path=f"media/{file}",
                            width=96, height=54, frame_count=12, fps=24.0))
    project.assets = assets
    if metadata:
        project.metadata.update(metadata)
    pm.save_project(project, notify=False)
    return project.project_dir


def fake_encode(*, delay=0.0, calls=None, started=None):
    """Copies the source (same packets) after ``delay``, honouring cancellation."""

    def encode(source, temp, *, cancel_event=None, **kwargs):
        if calls is not None:
            calls.append({"source": source, **kwargs})
        if started is not None:
            started.set()
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise MediaOperationCancelled("cancelled")
            time.sleep(0.01)
        shutil.copy(source, temp)

    return encode


def make_worker(**kwargs):
    events = kwargs.pop("events", None)
    defaults = dict(idle_seconds=lambda: 1e9, encode=fake_encode(), queue_busy=lambda: False,
                    grace_seconds=30, event_interval=0.05, poll_seconds=0.02,
                    emit=(lambda alias, event: events.append((alias, event))) if events is not None else (lambda *a: None))
    defaults.update(kwargs)
    return pmx.ProxyWorker(**defaults)


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def state(worker, project_dir, asset_id="a0"):
    return worker.states(project_dir, [(asset_id, {}, "")])[asset_id]


def project_bytes(project_dir):
    return Path(project_dir, "project.json").read_bytes()


@pytest.fixture
def stopper():
    workers = []
    yield workers.append
    for worker in workers:
        worker.stop()


# ── Settings and decision ───────────────────────────────────────────────────

def test_settings_resolve_unknown_values_to_defaults_and_validate_writes():
    assert pmx.resolve_settings({}) == {"preset": "High Quality MP4", "timing": "idle"}
    assert pmx.resolve_settings({"proxy_preset": "ProRes 422 HQ", "proxy_build_timing": "soon"}) == \
        {"preset": "High Quality MP4", "timing": "idle"}
    assert pmx.resolve_settings({"proxy_preset": "Editing Master MP4", "proxy_build_timing": "manual"}) == \
        {"preset": "Editing Master MP4", "timing": "manual"}
    pmx.validate_settings_update({"proxy_preset": "Compatible MP4", "proxy_build_timing": "now", "other": 1})
    with pytest.raises(ValueError):
        pmx.validate_settings_update({"proxy_preset": "Lossless FFV1 (RGB)"})
    with pytest.raises(ValueError):
        pmx.validate_settings_update({"proxy_build_timing": "never"})


@pytest.mark.parametrize("fmt,codec,pix_fmt,expected", [
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "yuv420p", "decodable"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "yuv444p", "decodable"),      # Editing Master
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "yuv422p", "decodable"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "yuv422p10le", "decodable"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "yuv444p10le", "undecodable"),
    ("matroska,webm", "h264", "yuv420p", "decodable"),
    ("matroska,webm", "vp9", "yuv420p", "decodable"),
    ("matroska,webm", "av1", "yuv420p", "decodable"),
    ("avi", "h264", "yuv420p", "undecodable"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "prores", "yuv422p10le", "undecodable"),
    ("matroska,webm", "ffv1", "gbrp", "undecodable"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "hevc", "yuv420p", "undecodable"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "mpeg4", "yuv420p", "undecodable"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "dnxhd", "yuv422p", "undecodable"),
])
def test_decision_table(fmt, codec, pix_fmt, expected):
    assert pmx.decision_for_stream(fmt, codec, pix_fmt) == expected


def test_save_video_outputs_are_decided_from_their_preset_without_a_probe(monkeypatch):
    monkeypatch.setattr(media_helpers, "probe_video_stream_info", lambda *a: pytest.fail("probed"))
    assert pmx.decision_for_save_params({"save_preset": "ProRes 422 HQ"}, ".mov") == "undecodable"
    assert pmx.decision_for_save_params({"save_preset": "Lossless FFV1 (RGB)"}, ".mkv") == "undecodable"
    assert pmx.decision_for_save_params({"save_preset": "Editing Master MP4"}, ".mp4") == "decodable"
    assert pmx.decision_for_save_params({"save_preset": "Compatible MP4", "codec": "libx264",
                                         "pix_fmt": "yuv420p"}, ".mp4") == "decodable"
    assert pmx.decision_for_save_params({"save_preset": "Custom", "codec": "libx265",
                                         "pix_fmt": "yuv420p"}, ".mkv") == "undecodable"
    assert pmx.decision_for_save_params({}, ".mp4") is None


def test_the_probe_reads_the_ffmpeg_banner_when_there_is_no_ffprobe(sources, monkeypatch):
    monkeypatch.setattr(media_helpers, "_ffprobe_json", lambda *a, **k: {})
    info = media_helpers.probe_video_stream_info(str(sources / "prores.mov"))
    assert (info["format_name"], info["codec_name"], info["pix_fmt"], info["probed"]) == \
        ("mov,mp4,m4a,3gp,3g2,mj2", "prores", "yuv422p10le", True)
    info = media_helpers.probe_video_stream_info(str(sources / "mjpeg.avi"))
    assert (info["format_name"], info["codec_name"]) == ("avi", "mjpeg")
    assert media_helpers.count_video_packets(str(sources / "vfr.mkv")) > 0


# ── Files and naming ────────────────────────────────────────────────────────

def test_proxy_files_are_matched_by_exact_name_never_a_prefix():
    assert pmx.matches_asset_proxy_file("abc", "abc_0123456789abcdef.mp4")
    assert not pmx.matches_asset_proxy_file("abc", "abc_def_0123456789abcdef.mp4")
    assert not pmx.matches_asset_proxy_file("abc", "abcd_0123456789abcdef.mp4")
    assert not pmx.matches_asset_proxy_file("a.c", "abc_0123456789abcdef.mp4")
    assert not pmx.matches_asset_proxy_file("abc", "abc_0123456789abcdef.mp4.tmp")


def test_removal_of_one_asset_never_touches_a_prefixed_neighbour(tmp_path, sources):
    project_dir = make_project(tmp_path, sources)
    root = pmx.proxies_root(project_dir, create=True)
    for name in ("abc.json", "abc_0123456789abcdef.mp4", "abc_def.json", "abc_def_0123456789abcdef.mp4"):
        Path(root, name).write_text("x")
    pmx.ProxyWorker().remove_asset_files(project_dir, "abc")
    assert sorted(os.listdir(root)) == ["abc_def.json", "abc_def_0123456789abcdef.mp4"]


def test_the_cache_root_is_only_ever_made_under_an_existing_cache_folder(tmp_path, sources):
    project_dir = make_project(tmp_path, sources)
    shutil.rmtree(Path(project_dir, "cache"))
    assert pmx.proxies_root(project_dir, create=True) == ""
    assert not Path(project_dir, "cache").exists()


# ── Worker: decide, build, publish ──────────────────────────────────────────

def test_an_undecodable_import_is_probed_once_built_and_published_under_a_fresh_rev(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov", "h264.mp4"))
    before = project_bytes(project_dir)
    events = []
    worker = make_worker(events=events)
    stopper(worker)
    worker.enqueue(project_dir, ["a0", "a1"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    wait_until(lambda: state(worker, project_dir, "a1")["decision"] == "decodable")
    first = state(worker, project_dir)
    assert first["decision"] == "undecodable" and len(first["rev"]) == 16
    assert state(worker, project_dir, "a1")["state"] == "none"
    root = pmx.proxies_root(project_dir)
    assert pmx.proxy_file_path(project_dir, "a0", first["rev"])
    assert not [name for name in os.listdir(root) if name.startswith("a1_")]

    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER, rebuild=True)
    wait_until(lambda: state(worker, project_dir)["rev"] not in ("", first["rev"]))
    second = state(worker, project_dir)
    # A rebuild never overwrites a file a browser may be streaming: new rev, new file,
    # and the old URL stops resolving.
    assert pmx.proxy_file_path(project_dir, "a0", second["rev"])
    assert pmx.proxy_file_path(project_dir, "a0", first["rev"]) == ""
    assert sorted(n for n in os.listdir(root) if n.endswith(".mp4")) == [f"a0_{second['rev']}.mp4"]
    assert project_bytes(project_dir) == before
    worker.flush_all_events()
    assert events and all(event["type"] == "proxy_status" for _alias, event in events)


def test_save_video_takes_are_decided_from_their_settings_without_a_probe(tmp_path, sources, monkeypatch,
                                                                        stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov",), legacy=False)
    project = pm.load_project(project_dir)
    project.assets[0].generation_params = {"save_preset": "ProRes 422 HQ", "codec": "prores_ks",
                                           "pix_fmt": "yuv422p10le", "prompt_text": "x" * 50}
    pm.save_project(project, notify=False)
    monkeypatch.setattr(media_helpers, "probe_video_stream_info", lambda *a: pytest.fail("probed"))
    worker = make_worker()
    stopper(worker)
    # The lean listing already knows, before the worker ran, and so does the take path.
    inline = pm.load_project(project_dir).assets[0].inline_generation_params
    assert worker.states(project_dir, [("a0", inline, ".mov")])["a0"]["decision"] == "undecodable"
    assert worker.build_inline(project_dir, "a0", interrupted=lambda: False)
    assert state(worker, project_dir)["state"] == "ready"


def test_a_replaced_save_video_asset_is_probed_not_trusted(tmp_path, sources, stopper):
    """A Replace keeps the take's save settings while changing the bytes."""
    project_dir = make_project(tmp_path, sources, ("prores.mov",))
    project = pm.load_project(project_dir)
    project.assets[0].generation_params = {"save_preset": "High Quality MP4"}  # says decodable
    pm.save_project(project, notify=False)
    worker = make_worker()
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    assert state(worker, project_dir)["decision"] == "undecodable"


def test_a_source_changed_during_the_encode_is_never_published(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    source = Path(project_dir, "media", "prores.mov")

    def encode(src, temp, **kwargs):
        shutil.copy(src, temp)
        os.utime(source, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))  # a replace

    worker = make_worker(encode=encode)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    root = Path(project_dir, "cache", "proxies")
    wait_until(lambda: (root / "a0.json").exists() and len(list(root.glob("a0_*.mp4"))) == 0
               and worker.current is None and not worker.jobs and not worker.undecided)
    assert state(worker, project_dir)["state"] != "ready"
    assert not list(root.glob("*.tmp.mp4"))


def test_the_cache_lock_is_held_only_to_publish_so_a_replace_is_not_blocked(tmp_path, sources, stopper):
    from server.thumbnail_service import asset_cache_lock

    project_dir = make_project(tmp_path, sources)
    started = threading.Event()
    worker = make_worker(encode=fake_encode(delay=1.0, started=started))
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    assert started.wait(10)
    acquired = asset_cache_lock(project_dir, "a0").acquire(timeout=0.3)
    assert acquired, "a replace would have waited for the whole encode"
    asset_cache_lock(project_dir, "a0").release()
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")


def test_a_failed_encode_is_recorded_and_not_retried_by_reconcile(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    calls = []

    def encode(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("ffmpeg exploded")

    worker = make_worker(encode=encode)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "failed")
    assert state(worker, project_dir)["error_code"] == "encode_failed"
    source = str(Path(project_dir, "media", "prores.mov"))
    worker.reconcile(project_dir, [("a0", source)], ["a0"])
    time.sleep(0.3)
    assert calls == [1]
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER)
    wait_until(lambda: len(calls) == 2)


def test_an_unlinked_project_is_dropped_and_never_written_to(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    worker = make_worker(resolve=lambda folder: None)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: not worker.undecided and not worker.jobs)
    time.sleep(0.1)
    assert not Path(project_dir, "cache", "proxies").exists()


# ── Timing ──────────────────────────────────────────────────────────────────

def test_idle_timing_waits_for_the_grace_period(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    idle = {"seconds": 0.0}
    worker = make_worker(idle_seconds=lambda: idle["seconds"], grace_seconds=30)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "waiting_idle")
    assert "waiting_since" in state(worker, project_dir)
    idle["seconds"] = 29.0  # the queue emptied, but not long enough ago
    time.sleep(0.2)
    assert state(worker, project_dir)["state"] == "waiting_idle"
    idle["seconds"] = 31.0
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")


def test_comfy_idle_tracks_the_grace_period_across_samples_and_hooks():
    busy = {"value": True}
    assert not comfy_idle.idle_for(30, queue_busy=lambda: busy["value"], now=100.0)
    busy["value"] = False
    assert not comfy_idle.idle_for(30, queue_busy=lambda: busy["value"], now=120.0)
    assert comfy_idle.idle_for(30, queue_busy=lambda: busy["value"], now=131.0)
    comfy_idle.note_busy(now=140.0)  # a prompt started and ended between two samples
    assert not comfy_idle.idle_for(30, queue_busy=lambda: busy["value"], now=150.0)
    assert comfy_idle.idle_for(30, queue_busy=lambda: busy["value"], now=171.0)


def test_comfyui_turning_busy_kills_the_encode_and_requeues_it_keeping_its_wait(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    idle = {"seconds": 0.0}
    busy = {"value": False}
    started = threading.Event()
    calls = []
    worker = make_worker(idle_seconds=lambda: idle["seconds"], queue_busy=lambda: busy["value"],
                         encode=fake_encode(delay=3.0, started=started, calls=calls), grace_seconds=30)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "waiting_idle")
    waited_since = state(worker, project_dir)["waiting_since"]
    idle["seconds"] = 60.0
    assert started.wait(10)
    assert state(worker, project_dir)["state"] == "building"
    # A prompt arrives: the hook reports it and the slow encode is killed at once.
    started.clear()
    busy["value"] = True
    idle["seconds"] = 0.0
    killed_at = time.monotonic()
    comfy_idle.note_busy()
    wait_until(lambda: state(worker, project_dir)["state"] == "waiting_idle", timeout=5)
    assert time.monotonic() - killed_at < 2.0
    assert state(worker, project_dir)["waiting_since"] == waited_since
    root = Path(project_dir, "cache", "proxies")
    assert not list(root.glob("*.tmp.mp4"))
    busy["value"] = False
    idle["seconds"] = 60.0
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    assert len(calls) == 2


def test_build_now_skips_the_gate_and_preempts_an_automatic_build(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov", "ffv1.mkv"))
    started = threading.Event()
    order = []

    def encode(source, temp, **kwargs):
        order.append(os.path.basename(source))
        fake_encode(delay=2.0 if source.endswith("prores.mov") and len(order) == 1 else 0.0,
                    started=started)(source, temp, **kwargs)

    worker = make_worker(encode=encode, idle_seconds=lambda: 1e9)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    assert started.wait(10)
    worker.enqueue(project_dir, ["a1"], reason=pmx.REASON_NOW)
    wait_until(lambda: state(worker, project_dir, "a1")["state"] == "ready")
    wait_until(lambda: state(worker, project_dir, "a0")["state"] == "ready")
    assert order == ["prores.mov", "ffv1.mkv", "prores.mov"]


def test_build_now_runs_while_comfyui_is_busy(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    worker = make_worker(idle_seconds=lambda: 0.0, queue_busy=lambda: True)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_NOW)
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")


def test_only_when_i_ask_drops_automatic_jobs_and_keeps_manual_ones(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov", "ffv1.mkv"),
                               metadata={"proxy_build_timing": "manual"})
    calls = []
    worker = make_worker(encode=fake_encode(calls=calls), idle_seconds=lambda: 1e9)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["decision"] == "undecodable" and not worker.undecided)
    assert state(worker, project_dir)["state"] == "manual_only"
    worker.enqueue(project_dir, ["a1"], reason=pmx.REASON_USER)
    wait_until(lambda: state(worker, project_dir, "a1")["state"] == "ready")
    assert [os.path.basename(call["source"]) for call in calls] == ["ffv1.mkv"]


def test_switching_to_only_when_i_ask_cancels_queued_automatic_jobs_only(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov", "ffv1.mkv"))
    worker = make_worker(idle_seconds=lambda: 0.0)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    worker.enqueue(project_dir, ["a1"], reason=pmx.REASON_USER)
    wait_until(lambda: len(worker.jobs) == 2)
    project = pm.load_project(project_dir)
    project.metadata["proxy_build_timing"] = "manual"
    pm.save_project(project, notify=False)
    worker.settings_changed(project_dir)
    assert [job.asset_id for job in worker.jobs.values()] == ["a1"]
    assert state(worker, project_dir)["state"] == "manual_only"
    # Leaving it queues what wants a proxy again.
    project = pm.load_project(project_dir)
    project.metadata["proxy_build_timing"] = "now"
    pm.save_project(project, notify=False)
    worker.settings_changed(project_dir)
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")


def test_a_preset_change_keeps_existing_proxies_until_regenerated(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    calls = []
    worker = make_worker(encode=fake_encode(calls=calls))
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    rev = state(worker, project_dir)["rev"]
    project = pm.load_project(project_dir)
    project.metadata["proxy_preset"] = "Compatible MP4"
    pm.save_project(project, notify=False)
    worker.settings_changed(project_dir)
    worker.reconcile(project_dir, [("a0", str(Path(project_dir, "media", "prores.mov")))], ["a0"])
    time.sleep(0.3)
    assert state(worker, project_dir)["rev"] == rev and len(calls) == 1
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER, rebuild=True)
    wait_until(lambda: state(worker, project_dir)["rev"] != rev)
    assert calls[-1]["preset_id"] == "Compatible MP4"
    assert state(worker, project_dir)["preset"] == "Compatible MP4"


# ── Removal, reconcile, runtime reports ─────────────────────────────────────

def test_a_file_that_cannot_be_removed_is_retried_later(tmp_path, sources, monkeypatch, stopper):
    project_dir = make_project(tmp_path, sources)
    worker = make_worker()
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    old = pmx.proxy_file_path(project_dir, "a0", state(worker, project_dir)["rev"])
    real_remove = os.remove
    monkeypatch.setattr(pmx.os, "remove", lambda path: (_ for _ in ()).throw(PermissionError(path))
                        if path == old else real_remove(path))
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER, rebuild=True)
    wait_until(lambda: old in worker.deferred_deletes)
    assert os.path.exists(old)
    monkeypatch.setattr(pmx.os, "remove", real_remove)
    worker._retry_deferred_deletes()
    assert not os.path.exists(old) and not worker.deferred_deletes


def test_reconcile_queues_stale_assets_and_sweeps_orphans_and_superseded_files(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov", "h264.mp4"))
    worker = make_worker()
    stopper(worker)
    root = Path(pmx.proxies_root(project_dir, create=True))
    (root / "gone.json").write_text("{}")
    (root / "gone_0123456789abcdef.mp4").write_text("x")
    old = time.time() - pmx.ORPHAN_GRACE_SECONDS - 5
    for name in ("gone.json", "gone_0123456789abcdef.mp4"):
        os.utime(root / name, (old, old))
    (root / "a0_fedcba9876543210.mp4").write_text("x")  # superseded, no sidecar names it
    stale_temp = root / f"a0.{'0' * 32}.tmp.mp4"
    stale_temp.write_text("x")
    os.utime(stale_temp, (time.time() - 7200, time.time() - 7200))
    videos = [("a0", str(Path(project_dir, "media", "prores.mov"))),
              ("a1", str(Path(project_dir, "media", "h264.mp4")))]
    result = worker.reconcile(project_dir, videos, ["a0", "a1"])
    assert result["queued"] == 2
    assert not (root / "gone.json").exists() and not (root / "gone_0123456789abcdef.mp4").exists()
    assert not (root / "a0_fedcba9876543210.mp4").exists() and not stale_temp.exists()
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    wait_until(lambda: state(worker, project_dir, "a1")["decision"] == "decodable")
    # A second reconcile finds nothing to do.
    assert worker.reconcile(project_dir, videos, ["a0", "a1"])["queued"] == 0


def test_a_deleted_proxy_is_held_until_someone_asks(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    worker = make_worker()
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    removed = worker.delete_proxies(project_dir, ["a0"])
    assert removed["count"] == 1 and removed["bytes"] > 0
    assert state(worker, project_dir)["state"] == "manual_only"
    source = str(Path(project_dir, "media", "prores.mov"))
    assert worker.reconcile(project_dir, [("a0", source)], ["a0"])["queued"] == 0
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER)
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")


def test_a_runtime_decode_failure_turns_an_allowed_original_into_a_proxy(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("h264.mp4",))
    worker = make_worker()
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["decision"] == "decodable")
    assert worker.mark_runtime_undecodable(project_dir, "a0")
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    assert state(worker, project_dir)["decision"] == "runtime"
    rev = state(worker, project_dir)["rev"]
    assert not worker.mark_runtime_undecodable(project_dir, "a0", variant="proxy", rev="0" * 16)
    assert worker.mark_runtime_undecodable(project_dir, "a0", variant="proxy", rev=rev)
    assert pmx.read_sidecar(pmx.proxies_root(project_dir), "a0")["error_code"] == "proxy_undecodable"


def test_create_proxy_on_a_decodable_asset_is_a_manual_proxy_and_regenerate_is_not(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("h264.mp4", "vfr.mkv"), name="Two")
    calls = []
    worker = make_worker(encode=fake_encode(calls=calls))
    stopper(worker)
    # Regenerate's kind of request: user-started, not forced. The asset is decided and
    # left alone.
    worker.enqueue(project_dir, ["a1"], reason=pmx.REASON_USER, rebuild=True)
    wait_until(lambda: state(worker, project_dir, "a1")["decision"] == "decodable" and not worker.undecided)
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER, force=True)
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    assert state(worker, project_dir)["decision"] == "manual"
    assert state(worker, project_dir, "a1")["state"] == "none" and len(calls) == 1


# ── Events ──────────────────────────────────────────────────────────────────

def test_events_are_batched_per_project_and_left_out_of_the_diagnostic_ring(tmp_path, sources, stopper):
    assert "proxy_status" in session_registry._UNRECORDED_BROADCAST_TYPES
    project_dir = make_project(tmp_path, sources, ("prores.mov", "ffv1.mkv", "odd.mov"))
    events = []
    worker = make_worker(events=events, event_interval=0.5)
    stopper(worker)
    worker.enqueue(project_dir, ["a0", "a1", "a2"])
    wait_until(lambda: all(state(worker, project_dir, a)["state"] == "ready" for a in ("a0", "a1", "a2")))
    worker.flush_all_events()
    folder = os.path.basename(project_dir)
    per_alias = [event for alias, event in events if alias == folder]
    assert per_alias and all(event["project_id"] == folder for event in per_alias)
    stamps = sorted(event["epoch"] for event in per_alias)
    assert stamps == [event["epoch"] for event in per_alias]  # monotonic
    # Far fewer events than state changes: each carries every asset that moved.
    assert len(per_alias) < 3 * 4
    last = {}
    for event in per_alias:
        for item in event["items"]:
            last[item["asset_id"]] = item
    assert {asset: item["state"] for asset, item in last.items()} == {"a0": "ready", "a1": "ready", "a2": "ready"}
    # A read made after an event is never older than it, so a refetch after missed
    # events wins the epoch merge.
    for asset_id, item in last.items():
        assert state(worker, project_dir, asset_id)["epoch"] >= item["epoch"]
    canonical = json.loads(Path(project_dir, "project.json").read_text(encoding="utf-8"))["project_id"]
    if canonical != folder:
        assert any(alias == canonical for alias, _event in events)


# ── Save Video inline ───────────────────────────────────────────────────────

def _take_project(tmp_path, sources, metadata=None):
    project_dir = make_project(tmp_path, sources, ("prores.mov",), metadata=metadata)
    project = pm.load_project(project_dir)
    project.assets[0].generation_params = {"save_preset": "ProRes 422 HQ"}
    pm.save_project(project, notify=False)
    return project_dir


def test_a_take_proxy_is_published_before_the_node_returns(tmp_path, sources, stopper):
    project_dir = _take_project(tmp_path, sources)
    worker = make_worker(idle_seconds=lambda: 0.0, queue_busy=lambda: True)
    stopper(worker)
    progress = []
    rev = worker.build_inline(project_dir, "a0", interrupted=lambda: False,
                              progress=lambda done, total: progress.append((done, total)))
    assert rev and pmx.proxy_file_path(project_dir, "a0", rev)
    assert not worker.jobs and not worker.undecided


def test_an_interrupted_take_proxy_is_handed_to_the_worker(tmp_path, sources, stopper):
    project_dir = _take_project(tmp_path, sources)
    worker = make_worker(encode=fake_encode(delay=5.0), idle_seconds=lambda: 0.0)
    stopper(worker)
    flag = {"on": False}
    timer = threading.Timer(0.2, lambda: flag.__setitem__("on", True))
    timer.start()
    started = time.monotonic()
    assert worker.build_inline(project_dir, "a0", interrupted=lambda: flag["on"]) == ""
    assert time.monotonic() - started < 3.0
    wait_until(lambda: state(worker, project_dir)["state"] == "waiting_idle")
    assert not list(Path(project_dir, "cache", "proxies").glob("*.tmp.mp4"))


def test_a_failing_take_proxy_never_raises(tmp_path, sources, stopper):
    project_dir = _take_project(tmp_path, sources)
    worker = make_worker(encode=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
                         idle_seconds=lambda: 0.0)
    stopper(worker)
    assert worker.build_inline(project_dir, "a0", interrupted=lambda: False) == ""
    wait_until(lambda: state(worker, project_dir)["state"] == "waiting_idle")


def test_a_take_under_only_when_i_ask_records_its_decision_and_builds_nothing(tmp_path, sources, stopper):
    project_dir = _take_project(tmp_path, sources, metadata={"proxy_build_timing": "manual"})
    calls = []
    worker = make_worker(encode=fake_encode(calls=calls))
    stopper(worker)
    assert worker.build_inline(project_dir, "a0", interrupted=lambda: False) == ""
    assert calls == [] and state(worker, project_dir)["state"] == "manual_only"


def test_save_video_calls_the_take_proxy_after_its_commit():
    source = Path(__file__).resolve().parents[1].joinpath("nodes", "io_nodes.py").read_text(encoding="utf-8")
    commit = source.index("committed_project = _save_generated_project(project, str(execution_context")
    call = source.index("_build_take_proxy(committed_project if committed_project is not None else project")
    once = source.index('run_once_per_prompt(prompt, unique_id, "SonderSaveVideo"')
    assert commit < call and once < commit


# ── Real encodes ────────────────────────────────────────────────────────────

def _stream_lines(path):
    result = subprocess.run([media_helpers.get_ffmpeg_path(), "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True)
    return [line for line in (result.stderr or "").splitlines() if "Stream #" in line]


@pytest.mark.parametrize("name,audio,size", [("prores.mov", True, (96, 54)), ("ffv1.mkv", False, (96, 54)),
                                             ("odd.mov", False, (95, 53)), ("vfr.mkv", False, (96, 54)),
                                             ("mjpeg.avi", False, (96, 54))])
def test_real_encodes_keep_every_frame_tag_bt709_and_carry_flac(sources, tmp_path, name, audio, size):
    source = str(sources / name)
    packets = media_helpers.count_video_packets(source)
    out = tmp_path / "proxy.mp4"
    media_helpers.encode_proxy(
        source, str(out), preset_id="High Quality MP4", width=size[0], height=size[1], frame_count=packets,
        input_color=media_helpers.resolve_source_color_interpretation(None, source), timeout=120,
    )
    assert media_helpers.count_video_packets(str(out)) == packets
    lines = _stream_lines(out)
    video = next(line for line in lines if "Video:" in line)
    assert "h264" in video and "yuv420p" in video and "bt709" in video
    audio_lines = [line for line in lines if "Audio:" in line]
    assert bool(audio_lines) == audio
    if audio:
        assert "flac" in audio_lines[0]


def test_a_cancelled_encode_stops_promptly(sources, tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(MediaOperationCancelled):
        media_helpers.encode_proxy(
            str(sources / "prores.mov"), str(tmp_path / "x.mp4"), preset_id="Compatible MP4", width=96,
            height=54, frame_count=12, input_color=("bt709", "tv"), timeout=60, cancel_event=event,
        )


# ── Audit regressions (Phase 1) ─────────────────────────────────────────────

def test_an_edit_list_source_is_compared_with_what_it_decodes(tmp_path, sources, stopper):
    """A stream-copy trim keeps packets the decoder drops; the proxy holds the frames."""
    long_gop = tmp_path / "gop.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24:duration=4", "-c:v", "libx264", "-g", "48",
           "-bf", "0", "-pix_fmt", "yuv420p", str(long_gop))
    trimmed = sources / "trimmed_editlist.mov"
    ffmpeg("-ss", "1.3", "-i", str(long_gop), "-c", "copy", str(trimmed))
    packets = media_helpers.count_video_packets(str(trimmed))
    decoded = media_helpers.count_decoded_frames(str(trimmed))
    assert packets > decoded > 0  # the premise: an edit list hides packets
    project_dir = make_project(tmp_path, sources, ("trimmed_editlist.mov",))
    worker = make_worker(encode=media_helpers.encode_proxy)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER, force=True)
    wait_until(lambda: state(worker, project_dir)["state"] in ("ready", "failed"), timeout=60)
    assert state(worker, project_dir)["state"] == "ready"


def test_a_take_never_waits_behind_a_build_now(tmp_path, sources, stopper):
    project_dir = _take_project(tmp_path, sources)
    other = Path(project_dir, "media", "other.mov")
    shutil.copy(sources / "prores.mov", other)
    project = pm.load_project(project_dir)
    project.assets.append(Asset(asset_id="b0", name="other.mov", asset_type="video", path="media/other.mov",
                                width=96, height=54))
    pm.save_project(project, notify=False)
    started = threading.Event()
    worker = make_worker(encode=fake_encode(delay=4.0, started=started))
    stopper(worker)
    worker.enqueue(project_dir, ["b0"], reason=pmx.REASON_NOW)
    assert started.wait(10)
    began = time.monotonic()
    assert worker.build_inline(project_dir, "a0", interrupted=lambda: False) == ""
    assert time.monotonic() - began < 1.0
    # Handed to the worker, which builds it once the Build now is done.
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")


def test_a_take_waiting_for_the_lock_honours_an_interrupt(tmp_path, sources, stopper):
    project_dir = _take_project(tmp_path, sources)
    worker = make_worker()
    stopper(worker)
    worker.encode_lock.acquire()  # something holds the encoder and does not let go
    try:
        flag = {"on": False}
        threading.Timer(0.3, lambda: flag.__setitem__("on", True)).start()
        began = time.monotonic()
        assert worker.build_inline(project_dir, "a0", interrupted=lambda: flag["on"]) == ""
        assert time.monotonic() - began < 2.0
    finally:
        worker.encode_lock.release()


def test_a_building_take_reads_as_building_and_reconcile_leaves_it_alone(tmp_path, sources, stopper):
    project_dir = _take_project(tmp_path, sources)
    seen = {}

    def encode(source, temp, **kwargs):
        seen["state"] = state(worker, project_dir)["state"]
        seen["reconcile"] = worker.reconcile(project_dir, [("a0", source)], ["a0"])["queued"]
        shutil.copy(source, temp)

    worker = make_worker(encode=encode)
    stopper(worker)
    assert worker.build_inline(project_dir, "a0", interrupted=lambda: False)
    assert seen == {"state": "building", "reconcile": 0}


def test_a_finished_encode_survives_comfyui_turning_busy(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources)
    busy = {"value": False}
    calls = []

    def encode(source, temp, **kwargs):
        calls.append(1)
        shutil.copy(source, temp)
        busy["value"] = True  # a prompt starts just as ffmpeg exits

    worker = make_worker(encode=encode, queue_busy=lambda: busy["value"],
                         idle_seconds=lambda: 0.0 if busy["value"] else 1e9)
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    assert calls == [1]


def test_a_queued_job_behind_another_build_is_not_waiting_for_idle(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov", "ffv1.mkv"))
    idle = {"seconds": 0.0}
    started = threading.Event()
    worker = make_worker(idle_seconds=lambda: idle["seconds"], encode=fake_encode(delay=1.5, started=started))
    stopper(worker)
    worker.enqueue(project_dir, ["a0", "a1"])
    wait_until(lambda: worker.queue_summary(project_dir)["waiting_idle"] == 2)
    idle["seconds"] = 60.0
    assert started.wait(10)
    wait_until(lambda: worker.queue_summary(project_dir)["waiting_idle"] == 0)
    queued = [a for a in ("a0", "a1") if state(worker, project_dir, a)["state"] == "queued"]
    assert len(queued) == 1


def test_a_deferred_file_is_never_the_current_one_and_sidecars_are_never_deferred(tmp_path, sources,
                                                                                 monkeypatch, stopper):
    project_dir = make_project(tmp_path, sources)
    worker = make_worker()
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    root = pmx.proxies_root(project_dir)
    real_remove = os.remove
    monkeypatch.setattr(pmx.os, "remove", lambda path: (_ for _ in ()).throw(PermissionError(path)))
    worker.remove_asset_files(project_dir, "a0")  # a replace, with everything held open
    monkeypatch.setattr(pmx.os, "remove", real_remove)
    assert not any(path.endswith(".json") for path in worker.deferred_deletes)
    assert pmx.read_sidecar(root, "a0").get("decision", "") == ""  # blanked, not left stale
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    current = pmx.proxy_file_path(project_dir, "a0", state(worker, project_dir)["rev"])
    worker._retry_deferred_deletes()
    assert os.path.exists(current) and pmx.read_sidecar(root, "a0")["decision"] == "undecodable"
    assert not worker.deferred_deletes


def test_delete_holds_only_assets_that_lost_a_proxy(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("prores.mov", "ffv1.mkv"), metadata={"proxy_build_timing": "now"})
    worker = make_worker()
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    # a1 was decided but never built (say, a fresh import) when Delete ran.
    root = pmx.proxies_root(project_dir)
    pmx.write_sidecar(root, "a1", {"source_rev": pmx.source_signature(str(Path(project_dir, "media", "ffv1.mkv"))),
                                   "decision": "undecodable", "state": "none"})
    assert worker.delete_proxies(project_dir, ["a0", "a1"])["count"] == 1
    assert not pmx.read_sidecar(root, "a1").get("held")
    worker.note_demand(project_dir, ["a1"])
    wait_until(lambda: state(worker, project_dir, "a1")["state"] == "ready")
    assert state(worker, project_dir)["state"] == "manual_only"


def test_deleting_a_manual_proxy_goes_back_to_the_playable_original(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, ("h264.mp4",))
    worker = make_worker()
    stopper(worker)
    worker.enqueue(project_dir, ["a0"], reason=pmx.REASON_USER, force=True)
    wait_until(lambda: state(worker, project_dir)["state"] == "ready")
    assert state(worker, project_dir)["decision"] == "manual"
    worker.delete_proxies(project_dir, ["a0"])
    assert state(worker, project_dir) | {"epoch": 0} == {"decision": "decodable", "state": "none", "rev": "",
                                                         "epoch": 0}


def test_leaving_only_when_i_ask_never_builds_trashed_assets(tmp_path, sources, stopper):
    project_dir = make_project(tmp_path, sources, metadata={"proxy_build_timing": "manual"})
    calls = []
    worker = make_worker(encode=fake_encode(calls=calls))
    stopper(worker)
    worker.enqueue(project_dir, ["a0"])
    wait_until(lambda: state(worker, project_dir)["state"] == "manual_only")
    project = pm.load_project(project_dir)
    project.assets[0].trashed_at = "2026-10-21T00:00:00"
    project.metadata["proxy_build_timing"] = "now"
    pm.save_project(project, notify=False)
    worker.settings_changed(project_dir)
    wait_until(lambda: not worker.undecided and not worker.jobs)
    time.sleep(0.2)
    assert calls == []


def test_the_sweep_leaves_a_just_committed_asset_s_files_alone(tmp_path, sources):
    project_dir = make_project(tmp_path, sources)
    root = Path(pmx.proxies_root(project_dir, create=True))
    (root / "fresh.json").write_text("{}")
    (root / "fresh_0123456789abcdef.mp4").write_text("x")
    worker = pmx.ProxyWorker()
    assert worker.sweep(project_dir, {"a0"}) == 0
    assert (root / "fresh.json").exists()
    old = time.time() - pmx.ORPHAN_GRACE_SECONDS - 5
    for name in ("fresh.json", "fresh_0123456789abcdef.mp4"):
        os.utime(root / name, (old, old))
    assert worker.sweep(project_dir, {"a0"}) == 2


def test_events_never_read_the_disk_on_the_caller_s_thread(tmp_path, sources, monkeypatch, stopper):
    project_dir = make_project(tmp_path, sources)
    worker = make_worker()
    stopper(worker)
    calls = []
    monkeypatch.setattr(pmx, "_listing", lambda root: calls.append(threading.current_thread().name))
    worker.cancel(project_dir, ["a0"])
    worker._emit_state(project_dir, "a0")
    assert calls == []
