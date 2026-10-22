// What a user does to proxies: Create, Rebuild, Remove, Build now, Regenerate and
// Delete by category, Cancel. Shared by every host (the editor in all three modes,
// the dormant card's gallery), so the same gesture reads the same everywhere.
//
// User-started builds show one progress toast per project, driven by the queue
// summary the store keeps (`proxy_status` events, `GET /proxies`); it says when the
// builds wait for ComfyUI to be idle, offers Cancel, and always ends, saying what
// became of the videos it started. Automatic and take builds show none: badges and
// pills carry their state.

import { notifyError, notifyInfo, notifyProgress } from "./editor_notifications.js";
import { proxyStore } from "./media_display_source.js";
import { postProxyRequest, refreshProxyStates } from "./proxy_client.js";

// While builds run, re-read the queue now and then: a page with no project socket
// (or one that missed events) still sees them end.
const PROGRESS_POLL_MS = 5_000;
// Reads that fail this many times in a row end the toast (the project is gone, say).
const PROGRESS_POLL_FAILURES = 6;

const trackers = new Map();   // projectId -> tracker

function plural(count, one, many = `${one}s`) {
    return `${count} ${count === 1 ? one : many}`;
}

function failure(message, result) {
    // The written sentence is the message; the server's code goes in detail.
    notifyError(message, { detail: result?.error || null });
    const error = new Error(message);
    error.code = result?.error || "";
    return error;
}

function progressView(tracker, queue) {
    const remaining = Math.max(0, Number(queue?.user_pending) || 0);
    tracker.total = Math.max(tracker.total, remaining + tracker.done, 1);
    tracker.done = Math.max(tracker.done, tracker.total - remaining);
    const waiting = (Number(queue?.waiting_idle) || 0) > 0 && !queue?.building;
    return {
        remaining,
        message: waiting
            ? "Waiting for ComfyUI to be idle"
            : `${tracker.done} of ${plural(tracker.total, "proxy", "proxies")} built`,
    };
}

/** The rev each asset had when its build was asked for, so a rebuild is told apart. */
function noteStartRevs(tracker, projectId, assetIds) {
    for (const id of assetIds || []) {
        if (!tracker.startRevs.has(id)) tracker.startRevs.set(id, String(proxyStore.get(projectId, id)?.rev || ""));
    }
}

/** What became of the videos this toast started: `{ tier, message }`. */
export function proxyBuildOutcome(projectId, startRevs) {
    let built = 0;
    let failed = 0;
    for (const [id, startRev] of startRevs) {
        const proxy = proxyStore.get(projectId, id);
        if (proxy?.state === "ready" && proxy.rev && proxy.rev !== startRev) built += 1;
        else if (proxy?.state === "failed") failed += 1;
    }
    const total = startRevs.size;
    if (failed) {
        return { tier: "warning", message: `${built} of ${plural(total, "proxy", "proxies")} built, ${failed} failed` };
    }
    if (built && built === total) return { tier: "success", message: `${plural(built, "proxy", "proxies")} built` };
    if (built) return { tier: "success", message: `${built} of ${plural(total, "proxy", "proxies")} built; the others needed none` };
    return { tier: "info", message: "No proxies built: they were current already, or the builds were stopped" };
}

function finish(projectId, tracker, patch) {
    if (trackers.get(projectId) !== tracker) return;
    trackers.delete(projectId);
    tracker.unsubscribe();
    clearInterval(tracker.poll);
    // A finished toast keeps no Cancel: it would cancel builds started later.
    tracker.handle.resolve({ ...patch, actions: null });
}

function update(projectId, tracker) {
    if (trackers.get(projectId) !== tracker) return;
    const queue = proxyStore.queue(projectId);
    if (!queue || (Number(queue.epoch) || 0) < tracker.since) return;
    const view = progressView(tracker, queue);
    if (!view.remaining) {
        if (tracker.ending) return;
        tracker.ending = true;
        const token = ++tracker.endToken;
        // An event reads states before its queue summary, so the last asset's ready
        // state can trail the "nothing pending" it ends with. Its job is done by now:
        // a fresh read sees the result.
        void refreshProxyStates(projectId, { force: true }).finally(() => {
            // A build started meanwhile extended this toast instead.
            if (tracker.ending && tracker.endToken === token) {
                finish(projectId, tracker, proxyBuildOutcome(projectId, tracker.startRevs));
            }
        });
        return;
    }
    tracker.handle.update({ message: view.message, progress: { current: tracker.done, total: tracker.total, unit: "" } });
}

function poll(projectId, tracker) {
    void refreshProxyStates(projectId).then((payload) => {
        if (trackers.get(projectId) !== tracker) return;
        if (payload) {
            tracker.failedPolls = 0;
            // A read that matches what the store had notifies nobody: check here.
            update(projectId, tracker);
            return;
        }
        tracker.failedPolls += 1;
        if (tracker.failedPolls >= PROGRESS_POLL_FAILURES) {
            finish(projectId, tracker, { tier: "info", message: "Proxy progress is unavailable: the project could not be read" });
        }
    });
}

/**
 * Show (or extend) the project's progress toast for user-started builds of
 * `assetIds`, from the queue summary their request returned. Nothing queued:
 * nothing to show.
 */
function trackUserBuilds(projectId, assetIds, queue) {
    const pending = Math.max(0, Number(queue?.user_pending) || 0);
    // The request's own summary goes into the store, so the "nothing pending" that
    // ends these builds is a change even when the store last read nothing pending.
    proxyStore.ingestQueue(projectId, queue, queue?.epoch);
    if (!pending) return null;
    let tracker = trackers.get(projectId);
    if (tracker) {
        tracker.ending = false;
        tracker.endToken += 1;
        noteStartRevs(tracker, projectId, assetIds);
        tracker.since = Math.max(tracker.since, Number(queue?.epoch) || 0);
        tracker.total = Math.max(tracker.total, tracker.done + pending);
        update(projectId, tracker);
        return tracker;
    }
    tracker = {
        total: pending, done: 0, since: Number(queue?.epoch) || 0, handle: null,
        unsubscribe: () => {}, poll: 0, failedPolls: 0, startRevs: new Map(), ending: false, endToken: 0,
    };
    noteStartRevs(tracker, projectId, assetIds);
    const view = progressView(tracker, queue);
    tracker.handle = notifyProgress({
        verb: "Building proxies",
        message: view.message,
        progress: { current: 0, total: tracker.total, unit: "" },
        source: `proxy-builds:${projectId}`,
        actions: [{
            label: "Cancel",
            variant: "warning",
            dismiss: false,
            fn: () => { void cancelProxyBuilds(projectId); },
        }],
    });
    trackers.set(projectId, tracker);
    tracker.unsubscribe = proxyStore.subscribe((changes) => {
        if (changes.has(String(projectId))) update(projectId, tracker);
    });
    tracker.poll = setInterval(() => poll(projectId, tracker), PROGRESS_POLL_MS);
    return tracker;
}

/** Cancel this project's user-started builds (automatic ones carry on). */
export async function cancelProxyBuilds(projectId) {
    const result = await postProxyRequest(projectId, "/proxies/cancel", {});
    const tracker = trackers.get(projectId);
    if (!result.ok) {
        if (tracker) tracker.handle.update({ message: "Could not cancel. Try again." });
        notifyError("Proxy builds could not be cancelled.", { detail: result.error || null });
        return false;
    }
    if (tracker) finish(projectId, tracker, { tier: "info", message: "Proxy builds cancelled" });
    return true;
}

/**
 * Create (or, with `rebuild`, replace) the proxies of these video assets. `now`
 * builds at once, ahead of everything else, whatever the timing setting says;
 * otherwise the build follows it.
 */
export async function buildProxies(projectId, assetIds, { now = false, rebuild = false } = {}) {
    const ids = [...new Set((assetIds || []).map(String).filter(Boolean))];
    if (!projectId || !ids.length) return null;
    const result = await postProxyRequest(projectId, "/proxies/build", {
        asset_ids: ids, mode: now ? "now" : "queue", rebuild,
    });
    if (!result.ok) {
        throw failure(ids.length === 1 ? "The proxy could not be queued." : "The proxies could not be queued.", result);
    }
    trackUserBuilds(projectId, ids, result.payload?.queue);
    return result.payload;
}

/** Remove these assets' proxies. A video that cannot play waits for a user build. */
export async function removeProxies(projectId, assetIds) {
    const ids = [...new Set((assetIds || []).map(String).filter(Boolean))];
    if (!projectId || !ids.length) return null;
    const result = await postProxyRequest(projectId, "/proxies/delete", { asset_ids: ids });
    if (!result.ok) throw failure(ids.length === 1 ? "The proxy could not be removed." : "The proxies could not be removed.", result);
    return result.payload;
}

/** Settings → Regenerate Proxies…: rebuild what exists, build what is missing. */
export async function regenerateProxies(projectId, categories) {
    const result = await postProxyRequest(projectId, "/proxies/regenerate", { categories });
    if (!result.ok) throw failure("Proxies could not be regenerated.", result);
    if (!trackUserBuilds(projectId, result.payload?.asset_ids || [], result.payload?.queue)) {
        notifyInfo("No proxies to regenerate in those categories.");
    }
    return result.payload;
}

/**
 * Settings → Delete Proxies…: by category; trashed assets' proxies always go. The
 * Settings line shows the new count, so success needs no toast.
 */
export async function deleteProxiesByCategory(projectId, categories) {
    const result = await postProxyRequest(projectId, "/proxies/delete", { categories });
    if (!result.ok) throw failure("Proxies could not be deleted.", result);
    return result.payload;
}

/**
 * The gallery's proxy gestures, by name, for the host to hand it as `onProxyAction`:
 * "create" (follows the timing setting), "rebuild", "now" (Build now) and "remove".
 * Failures are already shown, so this never rejects: it resolves to the response,
 * or to null when the request failed.
 */
export async function runProxyAction(projectId, action, assetIds) {
    try {
        if (action === "remove") return await removeProxies(projectId, assetIds);
        if (action === "now") return await buildProxies(projectId, assetIds, { now: true });
        if (action === "rebuild") return await buildProxies(projectId, assetIds, { rebuild: true });
        if (action === "create") return await buildProxies(projectId, assetIds);
    } catch (_error) {
        // Reported by the action itself.
    }
    return null;
}

/** Tests only. */
export function _resetProxyActions() {
    for (const tracker of trackers.values()) {
        tracker.unsubscribe();
        clearInterval(tracker.poll);
    }
    trackers.clear();
}
