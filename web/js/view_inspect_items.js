// What the gallery's inspect overlay shows when a View pane opens it: one item
// per pane with something to inspect, and the media each item needs while the
// overlay is open. Inspection only: an item is never an asset, never project
// data, and nothing here reaches the server. Everything is let go by
// `release()`, which the overlay calls once, on every way it can close.
//
// An output item is a snapshot: its own holder on the pane cache, so the pane
// moving on never changes the overlay. A sampling item follows its pane: each
// `refresh(models)` (the panes' models after a render) may give it a newer
// preview, and it keeps the last one while there is none. Until `ready` the
// anchor (the pane whose button was pressed) is still loading; the other items
// join as theirs load. No DOM: the overlay owns elements, and reports what they
// loaded (`markShown`) or could not (`markFailed`).
import { fileKey } from "./output_media_cache.js";
import { previewKind } from "./sampling_pane_media.js";
import { entryPlayer, subgraphChain, viewLabel } from "./views_model.js";
import { decodeChannel, minMaxPeaks } from "./waveform_peaks.js";

export const MAX_PEAKS = 4096;
export const NOTE_NEWER = "A newer result is in the pane";
export const NOTE_OTHER_SOURCE = "This pane now shows another source";

const KIND_OF_PLAYER = { image: "image", animated: "image", video: "video", audio: "audio" };
const KIND_LABELS = { image: "Image", video: "Video", audio: "Audio" };
let serial = 0;

// What a pane's media is in the overlay ("image" | "video" | "audio"), whether or
// not it has loaded; null for media the overlay can't show and for editor sources.
export function inspectableKind(model) {
    if (model?.source?.kind !== "view") return null;
    if (model.sampling) return model.frame?.blob ? previewKind(model.frame.mime) : null;
    if (!model.entry || model.entry.available === false) return null;
    return KIND_OF_PLAYER[entryPlayer(model.entry)] || null;
}
// The same, or null while it is not loaded and shown. `model` is a pane model
// with view_panes' `loaded` folded in.
export function inspectKind(model) {
    if (model?.condition || (!model?.sampling && !model?.loaded)) return null;
    return inspectableKind(model);
}

// The file or preview a pane shows. A preview's key is `prompt#n` per node, so
// its node joins it: two samplers of one run can reach the same n.
const mediaKey = model => (model.sampling ? `sampling:${model.frame.source}:${model.frame.key}` : `file:${fileKey(model.entry)}`);

const positive = value => (Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : 0);
// The media's own rate, the View's Timing, a KJ preview's rate, then the scene's.
function itemFps(model, sceneFps) {
    return positive(model.entry?.fps) || positive(model.view?.timing?.fps)
        || (model.sampling ? positive(model.read?.live?.fps) : 0) || positive(sceneFps) || 24;
}

// What is said of the media itself. A followed preview says what its pane does.
// A snapshot leaves the pane's chip behind: a run or a link is the pane's, and
// would go stale here (Held is in the item's place).
const itemStatus = model => (model.sampling ? model.chip || "" : model.result?.fresh === false ? "From cache" : "");

function itemDetails(item, model, subgraphNames) {
    const view = model.view;
    const title = view?.title || view?.class_type || "";
    const chain = subgraphChain(view?.path, subgraphNames);
    const named = chain && title ? `${chain} › ${title}` : title;
    const rows = [["View", viewLabel(view)]];
    if (named) rows.push(["Node", view.class_type && view.class_type !== title ? `${named} (${view.class_type})` : named]);
    rows.push(["Kind", KIND_LABELS[item.kind]]);
    if (item.status) rows.push(["Status", item.status]);
    if (!model.sampling && !model.held && (model.entries?.length || 0) > 1) rows.push(["Item", `${model.itemIndex + 1}/${model.entries.length}`]);
    return rows;
}

// `models` are the visible panes' models; `fps` the scene's. `startTime` is
// where the anchor's own media is. `signal` abandons the open.
export function openInspectSession({ models = [], cache, anchorIndex, fps = 0, startTime = 0, subgraphNames = null, signal = null,
    createObjectURL = blob => URL.createObjectURL(blob), revokeObjectURL = url => URL.revokeObjectURL(url),
    decode = decodeChannel } = {}) {
    const name = `inspect:${++serial}`;
    const controller = new AbortController();
    const listeners = new Set();
    let released = false;
    const all = [];

    const emit = event => {
        for (const listener of [...listeners]) {
            try { listener(event); } catch (error) { console.warn("[Sonder] Inspect listener failed:", error); }
        }
    };

    // Panes showing the identical file or preview are one item: the lower pane,
    // or the anchor when it is one of them (its notes must be its own pane's).
    const taken = new Map();
    for (const model of models) {
        if (!inspectKind(model)) continue;
        const key = mediaKey(model);
        if (!taken.has(key) || model.index === anchorIndex) taken.set(key, model);
    }
    for (const model of [...taken.values()].sort((a, b) => a.index - b.index)) {
        const anchor = model.index === anchorIndex;
        const place = `Pane ${model.index + 1}${model.held ? " · Held" : ""}`;
        const item = {
            id: `pane:${model.index}`, index: model.index, viewId: model.source.viewId, kind: inspectKind(model),
            title: `${viewLabel(model.view)} · ${place}`, place,
            fps: itemFps(model, fps), startTime: anchor ? positive(startTime) : 0, live: !!model.sampling,
            note: "", status: itemStatus(model), details: [], ready: false, frozen: false,
            // media: the newest preview's, and every one given a URL still held ({url, kind, blob, fps})
            key: mediaKey(model), claim: null, blob: null, url: "", held: [], shown: null, peaks: null,
            model,
        };
        item.details = itemDetails(item, model, subgraphNames);
        if (item.live) {
            item.blob = model.frame.blob;
            item.ready = true;
        } else {
            item.claim = cache.acquire(model.entry, `${name}:${model.index}`);
        }
        all.push(item);
    }
    const anchor = all.find(item => item.index === anchorIndex) || null;
    const byId = id => all.find(item => item.id === id) || null;

    async function load(item) {
        if (!item.claim) return {};
        const result = await item.claim.ready;
        if (released) return { error: "aborted" };
        if (result.error) return { error: result.error };
        item.url = result.url;
        item.ready = true;
        return {};
    }
    function dropItem(item) {
        const at = all.indexOf(item);
        if (at >= 0) all.splice(at, 1);
        item.claim?.release();
    }

    const opening = anchor ? load(anchor) : Promise.resolve({ error: "nothing" });
    for (const item of [...all]) {
        if (item === anchor || item.ready) continue;
        void load(item).then(result => {
            if (released) return;
            if (result.error) dropItem(item);
            else emit({ type: "joined", id: item.id });
        });
    }
    // A session that opens nothing holds nothing.
    const ready = opening.then(result => {
        if (released) return { error: "aborted" };
        if (result.error) release();
        return result.error ? { error: result.error } : { ok: true };
    });

    // A sampling preview's URLs, made when one is asked for. One is revoked once
    // a newer one is on screen, so a URL an element may still be loading never is.
    function urlOf(id) {
        const item = byId(id);
        if (!item || released) return "";
        if (!item.live || item.url) return item.url;
        item.url = createObjectURL(item.blob);
        item.held.push({ url: item.url, kind: item.kind, blob: item.blob, fps: item.fps });
        return item.url;
    }
    function markShown(id, url) {
        const item = byId(id);
        const at = item ? item.held.findIndex(media => media.url === url) : -1;
        if (at < 0) return;
        item.shown = item.held[at];
        for (const old of item.held.splice(0, at)) revokeObjectURL(old.url);
    }
    // A preview that can't decode: the item goes back to the last one shown, so a
    // rebuilt stage draws that and not the broken one. Its key stays, so it is not
    // taken again.
    function markFailed(id, url) {
        const item = byId(id);
        if (!item?.live || !url || item.url !== url || !item.shown || item.shown.url === url) return;
        const before = item.kind;
        item.held = item.held.filter(media => media.url !== url);
        revokeObjectURL(url);
        const { kind, blob, fps: rate } = item.shown;
        Object.assign(item, { kind, blob, fps: rate, url: item.shown.url });
        emit({ type: kind === before ? "media" : "kind", id });
    }

    function setText(item, field, value) {
        if (item[field] === value) return false;
        item[field] = value;
        return true;
    }

    // The panes rendered again. `current` are the visible panes' models.
    function refresh(current = []) {
        if (released) return;
        const byIndex = new Map(current.map(model => [model.index, model]));
        const events = [];
        for (const item of all) {
            const model = byIndex.get(item.index);
            const same = model?.source?.kind === "view" && model.source.viewId === item.viewId;
            let changed = false;
            if (!item.live) {
                // A pane with nothing shown (disconnected, waiting) says nothing about its result.
                const note = !same ? NOTE_OTHER_SOURCE : model.entry && mediaKey(model) !== item.key ? NOTE_NEWER : "";
                changed = setText(item, "note", note);
            } else if (!item.frozen && !same) {
                item.frozen = true;
                changed = setText(item, "note", NOTE_OTHER_SOURCE);
            } else if (!item.frozen && (!model.view || !model.sampling)) {
                // Its View can't be read now (the canvas is away, a Relay error): only
                // the pane's word for that changes.
                if (setText(item, "status", model.chip || model.message || "")) changed = true;
                item.details = itemDetails(item, item.model, subgraphNames);
            } else if (!item.frozen) {
                item.model = model;
                const kind = inspectKind(model);
                const key = kind ? mediaKey(model) : item.key;
                // No preview yet (a new run) or one that can't show: the last picture stays.
                if (key !== item.key) {
                    const before = item.kind;
                    Object.assign(item, { key, kind, blob: model.frame.blob, url: "", fps: itemFps(model, fps) });
                    events.push({ type: kind === before ? "media" : "kind", id: item.id });
                }
                if (setText(item, "status", itemStatus(model))) changed = true;
                item.details = itemDetails(item, model, subgraphNames);
            }
            if (changed) events.push({ type: "status", id: item.id });
        }
        events.forEach(emit);
    }

    // {peaks} of an audio item, decoded once; null when it can't be.
    function peaksOf(id) {
        const item = byId(id);
        if (!item || released || item.kind !== "audio") return Promise.resolve(null);
        item.peaks ||= decode(urlOf(id), { signal: controller.signal })
            .then(data => (data?.length ? { peaks: minMaxPeaks(data, Math.min(MAX_PEAKS, data.length)) } : null));
        return item.peaks;
    }

    function release() {
        if (released) return;
        released = true;
        signal?.removeEventListener?.("abort", release);
        controller.abort();
        listeners.clear();
        for (const item of all.splice(0)) {
            item.claim?.release();
            for (const media of item.held.splice(0)) revokeObjectURL(media.url);
            item.url = "";
            item.shown = null;
        }
    }
    if (signal?.aborted) release();
    else signal?.addEventListener?.("abort", release, { once: true });

    return {
        get items() { return all.filter(item => item.ready); },
        get released() { return released; },
        anchorId: anchor?.id || "",
        ready,
        subscribe(listener) {
            if (released) return () => {};
            listeners.add(listener);
            return () => listeners.delete(listener);
        },
        refresh, markShown, markFailed, urlOf, peaksOf, release,
    };
}
