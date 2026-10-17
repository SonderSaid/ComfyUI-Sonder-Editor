// What the Controls panel's lane and folder pickers list, for the project and
// scene of the Editor hosting the panel (plans/bridge-selections-controls.md, C).
//
// Module-host contract: the EditorWidget creates one per panel and owns it; it
// supplies the context (project, active scene, selection and context frames, the
// same values the canvas graph panels read and the Sonder App run uses), the
// folder list it already holds (the gallery's) and visibility. The panel never
// fetches: it declares which picker kinds it shows (`setInterest`), reads `get`,
// and repaints on `subscribe`. Everything here is ephemeral.
//
// Reads go through the page's shared Reference and Driver coordinators, so in
// fullscreen a version wave joins the graph's read of the same URL. Replies carry
// the context token they were asked under and are dropped once it moved.
import {
    allocateBridgeReadGeneration,
    bridgePayloadVersion,
    requestBridgeDriverPayload,
    requestBridgeReferencePayload,
} from "./bridge_read_coordinator.js";
import { onProjectVersionChanged, versionStillAnswers } from "./api_client.js";
import { referenceBridgeUrl } from "./reference_bridge_shape.js";

export const PICKER_KINDS = Object.freeze(["reference_lanes", "driver_lane", "asset_folder"]);
// Window and scene changes come in bursts (a drag, a selection sweep).
const SETTLE_MS = 250;

const NETWORK = {
    reference_lanes: {
        url: (context, apiURL) => apiURL(referenceBridgeUrl(context.projectId, context.sceneId, context)),
        request: "requestReferences",
        // bridge-references is certified: hold the payload across versions the
        // `bridge` flag proves unchanged.
        dependency: "bridge",
        shape: payload => ({
            lanes: Array.isArray(payload?.references) ? payload.references : [],
            sceneName: String(payload?.scene_name || ""),
            source: String(payload?.source || "live"),
            tagPresets: Array.isArray(payload?.tag_presets) ? payload.tag_presets : [],
            tagFamilies: payload?.tag_families && typeof payload.tag_families === "object" ? payload.tag_families : {},
        }),
    },
    driver_lane: {
        url: (context, apiURL) => apiURL(`/sonder-editor/project/${encodeURIComponent(context.projectId)}`
            + `/scenes/${encodeURIComponent(context.sceneId)}/bridge-drivers`),
        request: "requestDrivers",
        // Clips and Driver lanes are outside both certificate projections, so a
        // version of this project always re-reads (architecture.md, certificates).
        dependency: "",
        shape: payload => ({
            drivers: (Array.isArray(payload?.drivers) ? payload.drivers : [])
                .map(row => ({ laneIndex: Number(row?.lane_index), name: String(row?.lane_name || "") }))
                .filter(row => Number.isSafeInteger(row.laneIndex) && row.laneIndex >= 0),
            sceneName: String(payload?.scene_name || ""),
            source: String(payload?.source || "live"),
        }),
    },
};

export function createControlsPickerSources({
    getContext,
    apiURL = path => path,
    getFolders = () => [],
    requestReferences = requestBridgeReferencePayload,
    requestDrivers = requestBridgeDriverPayload,
    subscribeVersions = onProjectVersionChanged,
    setTimer = (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimer = id => globalThis.clearTimeout(id),
} = {}) {
    if (typeof getContext !== "function") throw new TypeError("Picker sources need a context getter.");
    const requests = { requestReferences, requestDrivers };
    const subscribers = new Set();
    const entries = new Map(Object.keys(NETWORK).map(kind => [kind, {
        status: "idle", url: "", data: null, error: "", accepted: null, token: 0, timer: null, pending: null, owed: false,
    }]));
    let interest = new Set();
    let visible = false;
    let destroyed = false;
    let contextTimer = null;

    const notify = () => { for (const cb of [...subscribers]) cb(); };
    const context = () => {
        const raw = getContext() || {};
        return {
            projectId: String(raw.projectId || ""),
            sceneId: String(raw.sceneId || ""),
            selectionStart: raw.selectionStart,
            selectionEnd: raw.selectionEnd,
            preContextFrames: raw.preContextFrames,
            postContextFrames: raw.postContextFrames,
        };
    };
    const urlFor = (kind, ctx) => (ctx.projectId && ctx.sceneId ? NETWORK[kind].url(ctx, apiURL) : "");
    // Only Reference and Driver lanes read; folders come from the host's list.
    const active = kind => !!NETWORK[kind] && !destroyed && visible && interest.has(kind);

    // The project and scene a URL reads, without its window: lane names and
    // recipes do not depend on the window, so a window move keeps the list shown.
    const placeOf = url => String(url || "").split("?")[0];

    function read(kind, { generation = "", origin = "controls", ifMoved = false } = {}) {
        const entry = entries.get(kind);
        clearTimer(entry.timer);
        entry.timer = null;
        entry.pending = null;
        // Hidden or no longer shown: the read stays owed until it is shown again.
        if (!active(kind)) { entry.owed = true; return; }
        const ctx = context();
        const url = urlFor(kind, ctx);
        // A window sweep that came back to the held (or in-flight) window owes nothing.
        if (ifMoved && !entry.owed && url === entry.url && (entry.status === "ready" || entry.status === "loading")) return;
        entry.owed = false;
        const token = ++entry.token;
        entry.accepted = null; // revoked on dispatch, as the graph's held reads are
        if (!url) {
            Object.assign(entry, { url: "", status: "unavailable", data: null, error: "" });
            notify();
            return;
        }
        // Another scene's or project's lanes are not this one's: never list them
        // while the new read is in flight. The same scene keeps its list shown.
        if (placeOf(entry.url) !== placeOf(url)) entry.data = null;
        Object.assign(entry, { url, status: "loading", error: "" });
        notify();
        requests[NETWORK[kind].request]({
            url, origin, nodeId: "controls",
            generation: generation || allocateBridgeReadGeneration(origin),
        }).then(payload => {
            if (destroyed || token !== entry.token) return;
            entry.data = NETWORK[kind].shape(payload);
            entry.status = "ready";
            entry.accepted = { url, version: bridgePayloadVersion(payload) };
            notify();
        }, error => {
            if (destroyed || token !== entry.token) return;
            // The last good list stays shown beside the failure.
            Object.assign(entry, { status: "error", error: error?.message || "" });
            notify();
        });
    }

    // Re-arming keeps what the pending read already owes: a version read stays
    // unconditional and keeps its stable generation when a window move follows.
    function schedule(kind, options) {
        const entry = entries.get(kind);
        const pending = entry.pending;
        entry.pending = !pending ? { ...options } : {
            origin: pending.ifMoved ? options.origin : pending.origin,
            generation: pending.generation || options.generation || "",
            ifMoved: !!(pending.ifMoved && options.ifMoved),
        };
        clearTimer(entry.timer);
        entry.timer = setTimer(() => {
            entry.timer = null;
            const owed = entry.pending;
            entry.pending = null;
            read(kind, owed);
        }, SETTLE_MS);
    }

    // A read is owed when one was owed while hidden, none is held, or the held one
    // answers another URL (scene, window or project moved). A move re-arms the
    // settle timer, so a selection sweep reads once, after it stops; nothing that
    // leaves the URL alone re-arms, so a pending version read keeps its time and
    // still joins the graph's read. A failed read retries through the same timer.
    function reconcile(kind) {
        if (!active(kind)) return;
        const entry = entries.get(kind);
        if (entry.owed || entry.status === "idle") {
            if (!entry.timer) read(kind, { origin: entry.owed ? "controls_owed" : "controls_interest" });
            return;
        }
        if (entry.status === "error") { schedule(kind, { origin: "controls_retry" }); return; }
        if (entry.url === urlFor(kind, context())) return;
        schedule(kind, { origin: "controls_window", ifMoved: true });
    }

    const unsubscribeVersions = subscribeVersions((projectId, modifiedAt) => {
        const ctx = context();
        for (const kind of Object.keys(NETWORK)) {
            const entry = entries.get(kind);
            const url = urlFor(kind, ctx);
            // No scene: nothing to read, and nothing to repaint.
            if (!url && entry.status === "unavailable") continue;
            const { refresh, accepted } = versionStillAnswers(
                entry.accepted, { ...ctx, url }, { projectId, modifiedAt }, NETWORK[kind].dependency);
            entry.accepted = accepted;
            if (!refresh) continue;
            // Hidden or not shown: read when shown again instead of listing stale lanes.
            if (!active(kind)) { entry.owed = true; continue; }
            // The scheduler's stable generation, so the graph's read of the same
            // URL and version is shared rather than repeated.
            schedule(kind, { generation: `project-version:${String(modifiedAt || "")}`, origin: "controls_version" });
        }
    });

    return {
        /** The picker kinds the panel shows with a host source (any iterable). */
        setInterest(kinds) {
            const next = new Set([...(kinds || [])].filter(kind => entries.has(kind) || kind === "asset_folder"));
            const added = [...next].filter(kind => !interest.has(kind));
            interest = next;
            for (const kind of added) reconcile(kind);
        },
        setVisible(on) {
            const was = visible;
            visible = !!on;
            if (visible && !was) for (const kind of interest) reconcile(kind);
        },
        /**
         * The host's project, scene or window may have moved. Evaluated on the
         * next tick: a scene switch writes `scene_id` before it finishes updating
         * the rest of the editor's state.
         */
        contextChanged() {
            if (destroyed || contextTimer) return;
            contextTimer = setTimer(() => {
                contextTimer = null;
                for (const kind of entries.keys()) reconcile(kind);
            }, 0);
        },
        foldersChanged() {
            if (!destroyed && visible && interest.has("asset_folder")) notify();
        },
        /** Explicit retry from the panel (a failed picker's Retry). */
        refresh(kind) {
            if (!entries.has(kind)) return;
            entries.get(kind).owed = true;
            read(kind, { origin: "controls_manual" });
        },
        get(kind) {
            if (kind === "asset_folder") {
                return { status: "ready", folders: [...new Set((getFolders() || []).map(String).filter(Boolean))] };
            }
            const entry = entries.get(kind);
            if (!entry) return { status: "unavailable" };
            return { status: entry.status, error: entry.error, ...(entry.data || {}) };
        },
        subscribe(cb) {
            subscribers.add(cb);
            return () => subscribers.delete(cb);
        },
        destroy() {
            destroyed = true;
            clearTimer(contextTimer);
            for (const entry of entries.values()) { clearTimer(entry.timer); entry.token++; }
            unsubscribeVersions?.();
            subscribers.clear();
        },
    };
}
