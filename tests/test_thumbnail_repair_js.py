import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


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


def _browser_bootstrap():
    return """
class FakeApi extends EventTarget {
    apiURL(path) { return path; }
    emit(type, detail) {
        const event = new Event(type);
        Object.defineProperty(event, "detail", { value: detail });
        this.dispatchEvent(event);
    }
}
const api = new FakeApi();
globalThis.window = new EventTarget();
window.comfyAPI = { api: { api } };
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
globalThis.document = new EventTarget();
document.visibilityState = "visible";
"""


def test_automatic_thumbnail_repair_waits_for_comfy_idle_and_suppresses_failed_signature():
    module_url = (ROOT / "web" / "js" / "thumbnail_repair_manager.js").as_uri()
    script = _browser_bootstrap() + f"""
let nextTimer = 1;
const timers = new Map();
globalThis.setTimeout = window.setTimeout = (callback, delay) => {{
    const id = nextTimer++;
    timers.set(id, {{ callback, delay }});
    return id;
}};
globalThis.clearTimeout = window.clearTimeout = (id) => timers.delete(id);
const runDelay = (delay) => {{
    const match = Array.from(timers.entries()).find(([, timer]) => timer.delay === delay);
    if (!match) return false;
    timers.delete(match[0]);
    match[1].callback();
    return true;
}};
const flush = async () => {{ for (let i = 0; i < 12; i += 1) await Promise.resolve(); }};

let fetchCount = 0;
let fail = false;
globalThis.fetch = async () => {{
    fetchCount += 1;
    return {{ ok: !fail, status: fail ? 500 : 200, arrayBuffer: async () => new ArrayBuffer(0) }};
}};
const manager = await import({json.dumps(module_url)});
const first = {{ asset_id: "asset-1", asset_type: "image", path: "media/a.png", media_probe_signature: "sig-1", has_thumbnail: false, missing: false, trashed_at: "" }};
api.emit("executing", {{ node: "node-1" }});
const enqueued = manager.enqueueAutomaticThumbnailRepair({{ ownerId: "gallery:a", projectDir: "C:/projects/demo", asset: first }});
const ranQuietWhileBusy = runDelay(manager.AUTOMATIC_THUMBNAIL_QUIET_MS);
await flush();
const busyFetches = fetchCount;
api.emit("executing", {{ node: null }});
const ranIdleYield = runDelay(manager.AUTOMATIC_THUMBNAIL_YIELD_MS);
await flush();
const repairedFetches = fetchCount;

fail = true;
const failed = {{ asset_id: "asset-2", asset_type: "image", path: "media/b.png", media_probe_signature: "sig-2", has_thumbnail: false, missing: false, trashed_at: "" }};
const failureEnqueued = manager.enqueueAutomaticThumbnailRepair({{ ownerId: "gallery:b", projectDir: "C:/projects/demo", asset: failed }});
runDelay(manager.AUTOMATIC_THUMBNAIL_QUIET_MS);
await flush();
const retryAccepted = manager.enqueueAutomaticThumbnailRepair({{ ownerId: "gallery:b", projectDir: "C:/projects/demo", asset: failed }});
fail = false;
const explicitRetry = await manager.startBulkThumbnailRepair({{ ownerId: "editor", projectDir: "C:/projects/demo", assets: [failed] }});

console.log(JSON.stringify({{
    enqueued,
    ranQuietWhileBusy,
    busyFetches,
    ranIdleYield,
    repairedFetches,
    repaired: first.has_thumbnail,
    failureEnqueued,
    retryAccepted,
    explicitRetry,
}}));
"""
    result = _run_node(script)
    assert result == {
        "enqueued": True,
        "ranQuietWhileBusy": True,
        "busyFetches": 0,
        "ranIdleYield": True,
        "repairedFetches": 1,
        "repaired": True,
        "failureEnqueued": True,
        "retryAccepted": False,
        "explicitRetry": {
            "projectId": "demo",
            "total": 1,
            "completed": 1,
            "repaired": 1,
            "failed": 0,
            "cancelled": False,
        },
    }


def test_bulk_preflight_filters_to_present_active_media_without_thumbnails():
    module_url = (ROOT / "web" / "js" / "thumbnail_repair_manager.js").as_uri()
    script = _browser_bootstrap() + f"""
const assets = [
    {{ asset_id: "repair", asset_type: "video", path: "media/a.mp4", has_thumbnail: false, missing: false, trashed_at: "" }},
    {{ asset_id: "cached", asset_type: "image", path: "media/b.png", has_thumbnail: true, missing: false, trashed_at: "" }},
    {{ asset_id: "trash", asset_type: "audio", path: "media/c.wav", has_thumbnail: false, missing: false, trashed_at: "2026-01-01" }},
    {{ asset_id: "missing", asset_type: "image", path: "media/d.png", has_thumbnail: false, missing: true, trashed_at: "" }},
    {{ asset_id: "artifact", asset_type: "artifact", path: "media/e.json", has_thumbnail: false, missing: false, trashed_at: "" }},
];
let requested = "";
globalThis.fetch = async (url) => {{
    requested = String(url);
    return {{ ok: true, status: 200, json: async () => ({{ assets }}) }};
}};
const manager = await import({json.dumps(module_url)});
const candidates = await manager.fetchMissingThumbnailAssets("C:/projects/demo");
console.log(JSON.stringify({{ requested, ids: candidates.map((asset) => asset.asset_id) }}));
"""
    result = _run_node(script)
    assert result["requested"].endswith("/sonder-editor/project/demo/assets?include_trashed=true")
    assert result["ids"] == ["repair"]


def test_bulk_thumbnail_repair_uses_two_workers_and_continues_through_failures():
    module_url = (ROOT / "web" / "js" / "thumbnail_repair_manager.js").as_uri()
    script = _browser_bootstrap() + f"""
let active = 0;
let maximum = 0;
let call = 0;
globalThis.fetch = async () => {{
    call += 1;
    const thisCall = call;
    active += 1;
    maximum = Math.max(maximum, active);
    await new Promise((resolve) => setTimeout(resolve, 4));
    active -= 1;
    return {{ ok: thisCall !== 3, status: thisCall === 3 ? 500 : 200, arrayBuffer: async () => new ArrayBuffer(0) }};
}};
const manager = await import({json.dumps(module_url)});
const assets = Array.from({{ length: 5 }}, (_, index) => ({{
    asset_id: `asset-${{index}}`,
    asset_type: index === 4 ? "audio" : "image",
    path: `media/${{index}}.png`,
    media_probe_signature: `sig-${{index}}`,
    has_thumbnail: false,
    missing: false,
    trashed_at: "",
}}));
const result = await manager.startBulkThumbnailRepair({{ ownerId: "editor", projectDir: "C:/projects/demo", assets }});
console.log(JSON.stringify({{ maximum, call, result }}));
"""
    result = _run_node(script)
    assert result["maximum"] == 2
    assert result["call"] == 5
    assert result["result"] == {
        "projectId": "demo",
        "total": 5,
        "completed": 5,
        "repaired": 4,
        "failed": 1,
        "cancelled": False,
    }


def test_bulk_cancellation_stops_queued_work_after_two_active_requests():
    module_url = (ROOT / "web" / "js" / "thumbnail_repair_manager.js").as_uri()
    notifications_url = (ROOT / "web" / "js" / "editor_notifications.js").as_uri()
    script = _browser_bootstrap() + f"""
const pending = [];
let fetchCount = 0;
globalThis.fetch = () => {{
    fetchCount += 1;
    return new Promise((resolve) => pending.push(() => resolve({{ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(0) }})));
}};
const notifications = await import({json.dumps(notifications_url)});
const manager = await import({json.dumps(module_url)});
let latest = [];
notifications.subscribe((items) => {{ latest = items; }});
const assets = Array.from({{ length: 6 }}, (_, index) => ({{
    asset_id: `asset-${{index}}`, asset_type: "video", path: `media/${{index}}.mp4`,
    media_probe_signature: `sig-${{index}}`, has_thumbnail: false, missing: false, trashed_at: "",
}}));
const promise = manager.startBulkThumbnailRepair({{ ownerId: "editor", projectDir: "C:/projects/demo", assets }});
for (let index = 0; index < 8; index += 1) await Promise.resolve();
const initialFetches = fetchCount;
const cancelAccepted = manager.cancelBulkThumbnailRepair({{ ownerId: "editor" }});
for (const resolve of pending.splice(0)) resolve();
const result = await promise;
const terminal = latest[latest.length - 1];
console.log(JSON.stringify({{
    initialFetches,
    finalFetches: fetchCount,
    cancelAccepted,
    result,
    terminal: terminal ? {{ tier: terminal.tier, progress: terminal.progress, message: terminal.message }} : null,
}}));
"""
    result = _run_node(script)
    assert result["initialFetches"] == 2
    assert result["finalFetches"] == 2
    assert result["cancelAccepted"] is True
    assert result["result"]["completed"] == 2
    assert result["result"]["cancelled"] is True
    assert result["terminal"]["tier"] == "warning"
    assert result["terminal"]["progress"] is None


def test_gallery_settings_and_toast_presenter_use_thumbnail_repair_contracts():
    gallery = (ROOT / "web" / "js" / "shared_asset_gallery.js").read_text(encoding="utf-8")
    settings = (ROOT / "web" / "js" / "editor_settings_panel.js").read_text(encoding="utf-8")
    toast = (ROOT / "web" / "js" / "editor_toast_stack.js").read_text(encoding="utf-8")

    assert "new IntersectionObserver" in gallery
    assert "enqueueAutomaticThumbnailRepair" in gallery
    assert "Regenerate Missing Thumbnails" in settings
    assert "Current Project Thumbnails" in settings
    assert "a.dismiss !== false" in toast
