"""Gallery-side pieces of provenance preloading that the loader tests cannot see.

The tracked-metadata matchers now take entry lists (from the search projection or a held
detail) instead of reading `asset.generation_params`; these pin their behaviour to what
the gallery matched before, run against the gallery source's own functions. The three
Asset Gallery settings pin their defaults, clamping and `0` meanings.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GALLERY = ROOT / "web" / "js" / "shared_asset_gallery.js"
SETTINGS_URL = (ROOT / "web" / "js" / "editor_settings.js").as_uri()


def _run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run([node, "--input-type=module", "-e", script],
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def _matcher_source():
    source = GALLERY.read_text(encoding="utf-8")
    fmt = source.split("function formatGenerationValue(value) {", 1)[1]
    fmt = "function formatGenerationValue(value) {" + fmt.split("\nfunction ", 1)[0]
    body = source.split("const trackedEntriesMemo = new WeakMap();", 1)[1].split("function canOpenWorkflowFor", 1)[0]
    return fmt + "\nconst trackedEntriesMemo = new WeakMap();" + body


def test_matchers_keep_the_old_entry_filter_blob_and_field_rules():
    result = _run("""
// Registered renderers: "claims" matches only its own field, "refuses" is a definitive no,
// anything else defers to the generic field comparison.
const trackedFieldMatchForEntry = (entry, name, value) => {
    if (entry.display_type === "claims") return name === "lora" && value === "x";
    if (entry.display_type === "refuses") return false;
    return null;
};
""" + _matcher_source() + """
const raw = [
    { label: "LoRA Stack", display_type: "claims", fields: { lora: "ignored" } },
    { label: "Seed", fields: { seed: 42, name: "Alpha" } },
    { label: "Hidden", display_type: "refuses", fields: { seed: 42 } },
    ["array", "entry"],
    null,
    "text",
    { label: "No fields", raw_widget_text: "Widget Text" },
];
const entries = trackedEntriesFromValue(raw);
console.log(JSON.stringify({
    kept: entries.length,
    arrayKept: entries.some(Array.isArray),
    nonList: [trackedEntriesFromValue(null).length, trackedEntriesFromValue({ a: 1 }).length, trackedEntriesFromValue("x").length],
    fromParams: trackedEntriesFromParams({ editor_export: { tracked_metadata: raw } }).length,
    memoIdentity: trackedEntriesFromValue(raw) === entries,
    blobHasWidget: trackedMetadataBlob(entries).includes("widget text"),
    blobHasArray: trackedMetadataBlob(entries).includes('"label":""'),
    blobMemo: trackedMetadataBlob(entries) === trackedMetadataBlob(entries),
    claims: trackedFieldMatches(entries, "lora", "x"),
    generic: trackedFieldMatches(entries, "name", "alp"),
    refusedOnlyThere: trackedFieldMatches([entries[2]], "seed", "42"),
    genericSeed: trackedFieldMatches(entries, "seed", "42"),
    none: trackedFieldMatches(entries, "missing", "1"),
}));
""")
    # Same filter as the old `trackedMetadataEntries`: objects kept (arrays included).
    assert result["kept"] == 5 and result["arrayKept"] is True
    assert result["nonList"] == [0, 0, 0]
    assert result["fromParams"] == 5
    assert result["memoIdentity"] is True and result["blobMemo"] is True
    assert result["blobHasWidget"] is True and result["blobHasArray"] is True
    assert result["claims"] is True
    assert result["generic"] is True
    # A definitive "no" from a renderer skips that entry, never the others.
    assert result["refusedOnlyThere"] is False
    assert result["genericSeed"] is True
    assert result["none"] is False


def test_gallery_preload_settings_defaults_clamping_and_zero_meanings():
    result = _run(f"""
globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
const m = await import({json.dumps(SETTINGS_URL)});
const pick = (stored) => {{
    const g = m.normalizeEditorSettings(stored).gallery;
    return [g.preloadFollowingAssets, g.preloadNewAssets, g.maxCachedProvenanceDetails];
}};
console.log(JSON.stringify({{
    defaults: pick({{}}),
    declared: [m.DEFAULT_EDITOR_SETTINGS.gallery.preloadFollowingAssets, m.DEFAULT_EDITOR_SETTINGS.gallery.preloadNewAssets,
        m.DEFAULT_EDITOR_SETTINGS.gallery.maxCachedProvenanceDetails],
    zero: pick({{ gallery: {{ preloadFollowingAssets: 0, preloadNewAssets: false, maxCachedProvenanceDetails: 0 }} }}),
    high: pick({{ gallery: {{ preloadFollowingAssets: 500, maxCachedProvenanceDetails: 10000000 }} }}),
    bad: pick({{ gallery: {{ preloadFollowingAssets: "x", maxCachedProvenanceDetails: -4 }} }}),
    fractional: pick({{ gallery: {{ preloadFollowingAssets: 7.6, maxCachedProvenanceDetails: 2.4 }} }}),
    limits: [m.GALLERY_PRELOAD_FOLLOWING_MAX, m.GALLERY_PROVENANCE_CACHE_MAX],
}}));
""")
    assert result["defaults"] == [50, True, 0]
    assert result["declared"] == [50, True, 0]
    assert result["zero"] == [0, False, 0]
    assert result["high"] == [63, True, 100000]
    assert result["bad"] == [50, True, 0]
    assert result["fractional"] == [8, True, 2]
    assert result["limits"] == [63, 100000]
