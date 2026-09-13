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


def test_every_video_preview_surface_consults_the_predicate():
    """Drift guard, not a behaviour test.

    Five surfaces render project video. If one is added or rewritten without the
    predicate it silently regains the black-rectangle bug, and no unit test would
    notice because the failure mode produces no error.
    """
    gallery = (ROOT / "web" / "js" / "shared_asset_gallery.js").read_text(encoding="utf-8")
    assert 'from "./media_preview_support.js"' in gallery
    # The single load funnel must gate before spending the transfer.
    funnel = gallery.split("function loadGalleryMediaAsBlob", 1)[1].split("function loadGalleryMediaViaCache", 1)[0]
    assert "shouldSkipVideoLoad" in funnel
    # Inspector, detail panel and compare each present the unavailable state.
    assert gallery.count("shouldSkipVideoLoad") >= 4, "a gallery video surface is not gated"
    # Imports have no recorded codec, so the runtime net must be attached too.
    assert "watchVideoDecodeFailure" in gallery
    assert gallery.count("watchVideoDecodeFailure(") >= 3

    editor = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    assert 'from "./media_preview_support.js"' in editor
    assert "previewUnavailable: shouldSkipVideoLoad(asset)" in editor

    ref_editor = (ROOT / "web" / "js" / "reference_media_editor.js").read_text(encoding="utf-8")
    assert "previewUnavailable" in ref_editor and "posterUrl" in ref_editor
    # The crop box must keep working over the still - refusing to open would make a
    # ProRes reference uncroppable rather than merely unscrubbable.
    assert "posterBehindMedia" in ref_editor


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
