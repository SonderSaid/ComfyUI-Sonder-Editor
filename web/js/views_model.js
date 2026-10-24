// Output Views: the pure model behind the viewport's panes. No DOM, app or
// network: the pane layout record (browser-local, per workflow), the pane
// picker's sections, the run outcome normalizer and the status table.
//
// A pane shows one source: the editor's Composite, its Guides, a Driver lane,
// or a named output View (the Relay's `views` section). Sources and layout
// are inspection only; nothing here reaches a prompt or the project.

export const LAYOUTS = Object.freeze({ "1": 1, "2c": 2, "3c": 3, "2x2": 4 });
export const LAYOUT_ORDER = Object.freeze(["1", "2c", "3c", "2x2"]);
export const LAYOUT_LABELS = Object.freeze({ "1": "1", "2c": "2", "3c": "3", "2x2": "2×2" });
export const LAYOUT_TITLES = Object.freeze({
    "1": "One pane", "2c": "Two panes side by side", "3c": "Three panes side by side", "2x2": "Four panes, two by two",
});
export const MAX_PANES = 4;
export const MAX_LAYOUT_RECORDS = 64;
export const FITS = Object.freeze(["contain", "cover"]);
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_ZOOM = 16;

export const paneCount = layout => LAYOUTS[layout] || 1;
// The first pane starts on the Composite, the others on the Guides.
export const defaultPane = (index = 0) => ({ source: { kind: index === 0 ? "composite" : "guides" }, item: 0, fit: "contain", zoom: null });

export function normalizeSource(raw) {
    if (!raw || typeof raw !== "object") return null;
    if (raw.kind === "composite" || raw.kind === "guides") return { kind: raw.kind };
    if (raw.kind === "driver" && Number.isInteger(raw.index) && raw.index >= 0 && raw.index <= 999) return { kind: "driver", index: raw.index };
    if (raw.kind === "view" && typeof raw.viewId === "string" && ID_RE.test(raw.viewId)) return { kind: "view", viewId: raw.viewId };
    return null;
}

export const sameSource = (a, b) => !!a && !!b && a.kind === b.kind && a.index === b.index && a.viewId === b.viewId;

function normalizeZoom(raw) {
    if (!raw || typeof raw !== "object") return null;
    const scale = Number(raw.scale), x = Number(raw.x), y = Number(raw.y);
    if (![scale, x, y].every(Number.isFinite) || scale < 1 || scale > MAX_ZOOM || Math.abs(x) > 1e5 || Math.abs(y) > 1e5) return null;
    return scale === 1 && x === 0 && y === 0 ? null : { scale, x, y };
}

export function normalizePane(raw, index = 0) {
    const pane = defaultPane(index);
    if (!raw || typeof raw !== "object") return pane;
    pane.source = normalizeSource(raw.source) || pane.source;
    pane.item = Number.isInteger(raw.item) && raw.item >= 0 && raw.item < 256 ? raw.item : 0;
    pane.fit = FITS.includes(raw.fit) ? raw.fit : "contain";
    pane.zoom = normalizeZoom(raw.zoom);
    return pane;
}

// A layout record: always four pane slots (the hidden ones keep their choice
// for when the layout grows again), Composite in at most one of them.
export function normalizeLayout(raw) {
    const record = { ts: 0, layout: "1", panes: Array.from({ length: MAX_PANES }, (_, i) => defaultPane(i)) };
    if (!raw || typeof raw !== "object") return record;
    record.ts = Number.isFinite(Number(raw.ts)) && Number(raw.ts) > 0 ? Number(raw.ts) : 0;
    record.layout = Object.hasOwn(LAYOUTS, raw.layout) ? raw.layout : "1";
    const panes = Array.isArray(raw.panes) ? raw.panes : [];
    record.panes = Array.from({ length: MAX_PANES }, (_, i) => normalizePane(panes[i], i));
    let composite = false;
    for (const pane of record.panes) {
        if (pane.source.kind !== "composite") continue;
        if (composite) pane.source = { kind: "guides" };
        composite = true;
    }
    return record;
}

// One Composite pane and nothing else chosen: the default, never stored.
export function isDefaultLayout(record) {
    const { ts: _ts, ...value } = normalizeLayout(record);
    const { ts: _fresh, ...fresh } = normalizeLayout(null);
    return JSON.stringify(value) === JSON.stringify(fresh);
}

// The browser-local map {workflowKey: record}: invalid and default records
// dropped, the newest 64 kept (Writing drafts' LRU pattern).
export function normalizeLayoutMap(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const entries = [];
    for (const [key, value] of Object.entries(raw)) {
        if (typeof key !== "string" || !key || key.length > 200 || !value || typeof value !== "object") continue;
        const record = normalizeLayout(value);
        if (!record.ts || isDefaultLayout(record)) continue;
        entries.push([key, record]);
    }
    entries.sort((a, b) => a[1].ts - b[1].ts);
    return Object.fromEntries(entries.slice(-MAX_LAYOUT_RECORDS));
}

export const visiblePanes = record => normalizeLayout(record).panes.slice(0, paneCount(record?.layout));

// Show `source` in pane `index`. Composite sits in one pane at most: picking
// it where another pane has it swaps the two panes' sources.
export function assignSource(record, index, source) {
    const next = normalizeLayout(record);
    const clean = normalizeSource(source);
    if (!clean || index < 0 || index >= MAX_PANES) return next;
    if (clean.kind === "composite") {
        const other = next.panes.findIndex((pane, i) => i !== index && pane.source.kind === "composite");
        if (other !== -1) next.panes[other] = { ...next.panes[other], source: next.panes[index].source, item: 0, zoom: null };
    }
    next.panes[index] = { ...next.panes[index], source: clean, item: 0, zoom: null };
    return next;
}

export const compositePane = record => visiblePanes(record).findIndex(pane => pane.source.kind === "composite");

// ── Picker ───────────────────────────────────────────────────────────────────

// The pane picker's sections. `views` and `candidates` come from the Controls
// view (the server's one list of eligible outputs); `drivers` from the scene.
export function pickerSections({ views = [], candidates = [], drivers = [], viewsError = null, viewsReady = true } = {}) {
    const editor = [
        { kind: "composite", label: "Composite", source: { kind: "composite" } },
        { kind: "guides", label: "Guides", source: { kind: "guides" } },
        ...drivers.map(lane => ({ kind: "driver", label: lane.label || `Driver ${lane.index + 1}`, source: { kind: "driver", index: lane.index } })),
    ];
    const viewItems = views.map(view => ({
        kind: "view", label: view.label || view.default_label || "Output", source: { kind: "view", viewId: view.view_id },
        status: view.status, detail: view.status !== "ok" ? "Unavailable" : view.participation === "inactive" ? "Switched off" : "",
        reason: view.reason || "",
    }));
    const taken = new Set(views.map(view => view.path).filter(Boolean));
    const add = candidates.filter(candidate => !taken.has(candidate.path)).map(candidate => ({
        kind: "candidate", label: candidate.title || candidate.class_type, candidate,
        detail: candidate.inactive ? inactiveText(candidate.inactive) : candidate.path.includes(":") ? "Inside a subgraph" : "",
    }));
    return { editor, views: viewItems, add, viewsError, viewsReady };
}

export function inactiveText(reason) {
    if (!reason) return "";
    const what = reason.mode === "bypassed" ? "bypassed" : "muted";
    return reason.by === "parent" ? `Inside ${reason.label || "a subgraph"}, which is ${what}` : `This node is ${what}`;
}

// ── Run outcomes ─────────────────────────────────────────────────────────────

// One shape for every host's branch-run answer:
// {state: "queued", promptId?, runId?} | {state: "refused"|"unsupported", code}
// | {state: "unconfirmed"|"busy"|"pending"}.
export function normalizeOutcome(raw, mode) {
    if (!raw || typeof raw !== "object") return { state: "refused", code: "no_answer" };
    if (mode === "sdk") {
        const runId = raw.payload?.run_ids?.[0];
        if (raw.ok && runId) return { state: "queued", runId: String(runId) };
        if (raw.payload?.code === "branch_unconfirmed" || (Array.isArray(raw.payload?.run_ids) && raw.payload.run_ids.length))
            return { state: "unconfirmed", ...(raw.payload.run_ids?.[0] ? { runId: String(raw.payload.run_ids[0]) } : {}) };
        return { state: "refused", code: String(raw.payload?.code || "refused"), ...(raw.payload?.error ? { message: String(raw.payload.error) } : {}) };
    }
    const state = raw.state;
    if (state === "queued") return raw.prompt_id ? { state, promptId: String(raw.prompt_id) } : { state: "unconfirmed" };
    if (state === "refused" || state === "unsupported") return { state, code: String(raw.code || state) };
    if (state === "busy") return { state: "busy" };
    // Mounted: the canvas has not answered yet, or went away with the request.
    if (state === "pending" || state === "claimed") return { state: "pending" };
    if (state === "unknown" || state === "unconfirmed") return { state: "unconfirmed" };
    return { state: "refused", code: String(raw.code || "refused") };
}

const REFUSALS = {
    branch_not_output: "This output is switched off or not an output node.",
    branch_not_in_prompt: "This output is switched off, so it can't run.",
    prompt_no_outputs: "ComfyUI found no output to run.",
    controls_unsaved: "A Controls edit was not saved, so the output did not run.",
    controls_not_saved: "A Controls edit was not saved, so the output did not run.",
    generate_refused: "This output can't run yet.",
    missing_nodes: "Some nodes of this workflow are not installed.",
    workflow_switching: "Wait for the workflow switch to finish.",
    workflow_changed: "The workflow changed. Try again.",
    too_many_runs: "Too many runs are already waiting.",
    not_owner: "This tab no longer owns the editor.",
    canvas_host_disconnected: "The canvas is not connected.",
    queue_full: "Too many requests are waiting. Try again in a moment.",
};
export const refusalText = outcome => outcome?.message || REFUSALS[outcome?.code] || "The output did not run.";

// ── Status table ─────────────────────────────────────────────────────────────

const VIEW_PROBLEMS = new Set(["missing", "duplicate", "kind", "unsupported"]);
const UNVIEWABLE_IMAGE = /\.(tif|tiff|exr|hdr|psd|dds)$/i;

// What kind of player an entry needs, or why it has none.
export function entryPlayer(entry) {
    if (!entry?.file) return "unsupported";
    if (entry.kind === "image") return UNVIEWABLE_IMAGE.test(String(entry.file.filename || "")) ? "unsupported" : "image";
    if (entry.kind === "animated_image") return "animated";
    if (entry.kind === "video") return "video";
    if (entry.kind === "audio") return "audio";
    return "unsupported";
}

const outcomeWord = run => (run?.outcome === "interrupted" ? "Last run was canceled" : "Last run failed");

// The pane for a View source. `read` is the server's answer for this source
// ({state, result, latest_run}); `run` the pane's own run outcome ({state,
// canceled?, removed?}); `hold` the pane's hold ({state, entry, run}) when it
// holds. Returns {message, chip, result (the run result to show, or null),
// unavailable (show a Release for a held pane)}.
export function viewPaneStatus({ view = null, read = null, run = null, hold = null, held = false,
    sessionLost = false, viewsError = null } = {}) {
    if (sessionLost) return { message: "Disconnected", chip: null, result: null };
    // A held result stands on its own: a Views problem does not hide it.
    if (held) {
        if (!hold) return { message: "Holding…", chip: "Held", result: null };
        if (hold.state !== "ok") return { message: "Held result unavailable (file changed or removed)", chip: "Unavailable", result: null, unavailable: true };
        return { message: null, chip: "Held", result: { entries: [hold.entry], run: hold.run, fixedIndex: true } };
    }
    if (viewsError) return { message: viewsError, chip: "Unavailable", result: null };
    if (!view) return { message: "This View is no longer in the workflow. Pick another source.", chip: "Unavailable", result: null };
    if (VIEW_PROBLEMS.has(view.status)) return { message: `${view.reason || "This View can't be shown."} Fix in Manage Controls.`, chip: "Unavailable", result: null };
    const previous = read?.result || null;
    if (view.participation === "inactive") {
        return { message: previous ? null : "No result yet", chip: "Switched off", result: previous };
    }
    if (run?.removed) return { message: previous ? null : "Canceled before it ran", chip: "Canceled", result: previous };
    const state = read?.state || "none";
    switch (state) {
        case "none":
        case "not_in_run":
            return { message: "No result yet", chip: null, result: null };
        case "pending":
            return previous ? { message: null, chip: "Previous result · Running", result: previous }
                : { message: "Waiting for run", chip: "Running", result: null };
        case "ok":
            return { message: null, chip: previous?.fresh === false ? "From cache" : null, result: previous };
        case "not_executed":
            return { message: previous ? null : "Last run didn't produce this", chip: "Last run didn't produce this", result: previous };
        case "failed":
            return { message: previous ? null : outcomeWord(read.latest_run), chip: outcomeWord(read.latest_run), result: previous };
        case "missing_file":
            return { message: "The output file is missing", chip: "Unavailable", result: null };
        case "unsupported":
            return { message: "This output type can't be shown here", chip: "Unsupported", result: null };
        default:
            return { message: "No result yet", chip: null, result: null };
    }
}

// A media-level condition overrides the read: the file was gone, too large, or
// the browser could not play it. `takeSaved` adds the Assets hint.
export function mediaStatus(condition, { takeSaved = false } = {}) {
    switch (condition) {
        case "missing_file": return { message: "The output file is missing", chip: "Unavailable" };
        case "too_large": return { message: "Too large to load here", chip: "Unavailable" };
        case "unsupported": return { message: "This output type can't be shown here", chip: "Unsupported" };
        case "decode": return { message: takeSaved ? "Can't play this here. Open it from Assets to play its proxy." : "Can't play this here",
            chip: "Can't play" };
        case "failed": return { message: "The output could not be loaded", chip: "Unavailable" };
        default: return null;
    }
}

// Whether the pane's Run button shows, and whether it is enabled (with why not).
export function runAvailability({ view = null, capability = null, outcome = null, busy = false, sessionLost = false } = {}) {
    if (!capability?.ok) return { show: false, reason: "This ComfyUI can't run one output on its own" };
    if (!view || view.status !== "ok") return { show: true, enabled: false, reason: "This View is unavailable" };
    if (view.participation === "inactive") return { show: true, enabled: false, reason: view.reason || "Its node is switched off" };
    if (sessionLost) return { show: true, enabled: false, reason: "Disconnected" };
    if (outcome && !outcome.settled) return { show: true, enabled: false, reason: "This output is already running" };
    if (busy) return { show: true, enabled: false, reason: "Another output is waiting for ComfyUI" };
    return { show: true, enabled: true, reason: "Run this output and what it needs" };
}

// Cancel shows for the pane's own unsettled run; it is disabled until ComfyUI
// has given the run an id.
export function cancelAvailability(outcome) {
    if (!outcome || outcome.settled) return { show: false };
    if (!outcome.promptId && !outcome.runId) return { show: true, enabled: false, reason: "Waiting for ComfyUI to accept the run" };
    return { show: true, enabled: true, reason: "Cancel this run" };
}
