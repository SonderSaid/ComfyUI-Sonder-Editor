import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = (ROOT / "web" / "js" / "media_preview_support.js").as_uri()


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _hints(assets: dict) -> dict:
    script = f"""
const {{ videoPreviewHint }} = await import({json.dumps(MODULE)});
const assets = {json.dumps(assets)};
const out = {{}};
for (const [name, asset] of Object.entries(assets)) out[name] = videoPreviewHint(asset);
console.log(JSON.stringify(out));
"""
    return _run_node(script)


def test_static_hint_suppresses_only_on_recorded_undecodable_codecs():
    """ProRes and FFV1 are the only codecs Chromium cannot decode at all."""
    result = _hints({
        "prores": {"generation_summary": {"codec": "prores_ks", "save_preset": "ProRes 422 HQ"}},
        "ffv1": {"generation_summary": {"codec": "ffv1", "save_preset": "Lossless FFV1 (RGB)"}},
        "prores_bare": {"generation_summary": {"codec": "PRORES"}},
        "preset_only": {"generation_summary": {"save_preset": "ProRes 422 HQ"}},
    })
    assert result == {
        "prores": "undecodable",
        "ffv1": "undecodable",
        "prores_bare": "undecodable",
        "preset_only": "undecodable",
    }


def test_editing_master_is_never_suppressed():
    """The false-positive guard, and the most important assertion in this file.

    Editing Master MP4 is yuv444p H.264. It carries `browser_preview_compatible:
    false`, yet it decodes perfectly and - since the all-intra change - is the
    best-scrubbing format the editor writes. Gating on that flag instead of the
    codec would blank a preview that works.
    """
    result = _hints({
        "editing_master": {"generation_summary": {
            "codec": "libx264", "pix_fmt": "yuv444p", "save_preset": "Editing Master MP4",
        }},
        "compatible": {"generation_summary": {
            "codec": "libx264", "pix_fmt": "yuv420p", "save_preset": "Compatible MP4",
        }},
    })
    assert result == {"editing_master": "unknown", "compatible": "unknown"}


def test_imports_and_unknown_shapes_fall_through_to_runtime():
    """Extension is deliberately not an input.

    An imported .mov holding H.264, or .mkv holding VP9, decodes fine and has no
    recorded codec. Suppressing on extension would be terminal - it skips the
    load, so the runtime check could never correct it.
    """
    result = _hints({
        "imported_mov": {"path": "media/clip.mov", "extension": ".mov"},
        "imported_mkv": {"path": "media/clip.mkv", "extension": ".mkv",
                         "generation_summary": {}},
        "no_summary": {"path": "media/clip.mp4"},
        "null_asset": None,
    })
    assert set(result.values()) == {"unknown"}


def test_runtime_check_is_authoritative_and_waits_for_metadata():
    script = f"""
const {{ videoTrackFailedToDecode }} = await import({json.dumps(MODULE)});
const cases = {{
  undecodable_ready:   {{ readyState: 4, videoWidth: 0, videoHeight: 0 }},
  undecodable_metadata:{{ readyState: 1, videoWidth: 0, videoHeight: 0 }},
  metadata_not_in_yet: {{ readyState: 0, videoWidth: 0, videoHeight: 0 }},
  decoding_fine:       {{ readyState: 1, videoWidth: 1920, videoHeight: 1088 }},
  height_only_zero:    {{ readyState: 4, videoWidth: 1920, videoHeight: 0 }},
}};
const out = {{}};
for (const [name, state] of Object.entries(cases)) out[name] = videoTrackFailedToDecode(state);
console.log(JSON.stringify(out));
"""
    result = _run_node(script)
    assert result == {
        "undecodable_ready": True,
        "undecodable_metadata": True,
        # readyState 0 means metadata has not arrived; every video reports width 0
        # there, so firing would flash "unsupported" at a file that decodes.
        "metadata_not_in_yet": False,
        "decoding_fine": False,
        "height_only_zero": True,
    }


def test_every_video_preview_surface_consults_the_one_display_authority():
    """Drift guard, not a behaviour test.

    Every surface that shows project video asks `media_display_source.js` which
    picture to show (proxy, original, or a notice). If one is added or rewritten
    without it, it silently regains the black-rectangle bug, and no unit test would
    notice because the failure mode produces no error. The static codec hint is
    only that module's fallback for a payload with no server decision.
    """
    js = ROOT / "web" / "js"
    gallery = (js / "shared_asset_gallery.js").read_text(encoding="utf-8")
    assert 'from "./media_display_source.js"' in gallery
    # The single load funnel must gate before spending the transfer.
    funnel = gallery.split("function loadGalleryMediaAsBlob", 1)[1].split("function loadGalleryMediaViaCache", 1)[0]
    assert "galleryVideoSource(asset)" in funnel and '"blocked"' in funnel
    # Inspector, detail panel (pill and block) and compare each present the notice.
    assert gallery.count("videoBlocked(") >= 5, "a gallery video surface is not gated"
    # An original nothing has judged yet still loads, so the runtime net stays.
    assert gallery.count("watchVideoDecodeFailure(") >= 3

    editor = (js / "editor_widget.js").read_text(encoding="utf-8")
    assert 'from "./media_display_source.js"' in editor
    assert "this._displayVideoSource(asset)" in editor  # Crop, Trim & Preview
    assert "previewUnavailable: blocked," in editor

    viewport = (js / "viewport_surface.js").read_text(encoding="utf-8")
    assert "displayVideoSource(projectId, asset)" in viewport and "blockedClipLayers" in viewport

    dormant = (js / "editor_node_controller.js").read_text(encoding="utf-8")
    assert dormant.count("dormantVideoSource(") >= 3

    ref_editor = (js / "reference_media_editor.js").read_text(encoding="utf-8")
    assert "previewUnavailable" in ref_editor and "posterUrl" in ref_editor
    # The crop box must keep working over the still - refusing to open would make a
    # ProRes reference uncroppable rather than merely unscrubbable.
    assert "posterBehindMedia" in ref_editor

    # Nothing but the display authority reads the static hint any more.
    for path in js.glob("*.js"):
        if path.name in ("media_preview_support.js", "media_display_source.js"):
            continue
        assert "shouldSkipVideoLoad" not in path.read_text(encoding="utf-8"), path.name


def test_media_failed_to_decode_catches_both_current_failure_forms():
    """Chromium 152: loud (error 4, no audio track) and silent (readyState 4, 0x0)."""
    script = f"""
const {{ mediaFailedToDecode }} = await import({json.dumps(MODULE)});
console.log(JSON.stringify({{
  loud: mediaFailedToDecode({{ error: {{ code: 4 }}, readyState: 0, videoWidth: 0, videoHeight: 0 }}),
  silent: mediaFailedToDecode({{ error: null, readyState: 4, videoWidth: 0, videoHeight: 0 }}),
  loading: mediaFailedToDecode({{ error: null, readyState: 0, videoWidth: 0, videoHeight: 0 }}),
  network: mediaFailedToDecode({{ error: {{ code: 2 }}, readyState: 0, videoWidth: 0, videoHeight: 0 }}),
  fine: mediaFailedToDecode({{ error: null, readyState: 4, videoWidth: 320, videoHeight: 180 }}),
  nothing: mediaFailedToDecode(null),
}}));
"""
    assert _run_node(script) == {"loud": True, "silent": True, "loading": False, "network": False,
                                 "fine": False, "nothing": False}


def test_only_a_failure_that_proves_it_counts_as_a_codec_verdict():
    """A 404, 5xx or refused connection gives the same loud `error.code 4` as a
    codec the browser lacks: alone it is "unconfirmed", never "decode"."""
    script = f"""
const {{ classifyMediaFailure: c }} = await import({json.dumps(MODULE)});
console.log(JSON.stringify({{
  loudOverHttp: c({{ error: {{ code: 4, message: "MEDIA_ELEMENT_ERROR: Format error" }}, readyState: 0,
                    src: "http://x/view?f=a.mov" }}),
  loudDemuxer: c({{ error: {{ code: 4, message: "DEMUXER_ERROR_NO_SUPPORTED_STREAMS: FFmpegDemuxer: no supported streams" }},
                   readyState: 0, src: "http://x/view?f=a.mov" }}),
  loudBlob: c({{ error: {{ code: 4, message: "" }}, readyState: 0, src: "blob:http://x/1" }}),
  silent: c({{ error: null, readyState: 4, videoWidth: 0, videoHeight: 0 }}),
  network: c({{ error: {{ code: 2 }}, readyState: 0, videoWidth: 0, videoHeight: 0 }}),
  midStream: c({{ error: {{ code: 3 }}, readyState: 4, videoWidth: 0, videoHeight: 0 }}),
  loading: c({{ error: null, readyState: 0, videoWidth: 0, videoHeight: 0 }}),
  fine: c({{ error: null, readyState: 4, videoWidth: 320, videoHeight: 180 }}),
}}));
"""
    assert _run_node(script) == {"loudOverHttp": "unconfirmed", "loudDemuxer": "decode", "loudBlob": "decode",
                                 "silent": "decode", "network": "", "midStream": "", "loading": "", "fine": ""}


def test_browser_preview_compatible_is_not_used_as_the_gate():
    """It is false for Editing Master, which decodes fine, and absent from /assets.

    Guarding this explicitly because it is the obvious-looking field and reaching
    for it would reintroduce a suppressed-but-working preview.
    """
    for name in ("media_preview_support.js", "shared_asset_gallery.js", "reference_media_editor.js"):
        source = (ROOT / "web" / "js" / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.strip().startswith("//") and not line.strip().startswith("*")
        )
        assert "browser_preview_compatible" not in code, name


def test_skip_load_matches_the_static_hint():
    script = f"""
const {{ shouldSkipVideoLoad }} = await import({json.dumps(MODULE)});
console.log(JSON.stringify({{
  prores: shouldSkipVideoLoad({{ generation_summary: {{ codec: "prores_ks" }} }}),
  master: shouldSkipVideoLoad({{ generation_summary: {{ codec: "libx264" }} }}),
  unknown: shouldSkipVideoLoad({{}}),
}}));
"""
    assert _run_node(script) == {"prores": True, "master": False, "unknown": False}
