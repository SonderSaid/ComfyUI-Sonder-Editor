// Output Views: the pure model behind the viewport's panes. No DOM, app or
// network: the pane layout record (browser-local, per workflow), the pane
// picker's sections, the run outcome normalizer and the status table.
//
// A pane shows one source: the editor's Composite, its Guides, a Driver lane,
// or a named output View (the Relay's `views` section). Sources and layout
// are inspection only; nothing here reaches a prompt or the project.

import { KJ_PREVIEW_CLASS, VIEW_FEED_SAMPLING, viewFeed } from "./control_targets.js";

export const MAX_PANES = 4;
export const MAX_LAYOUT_RECORDS = 64;
export const FITS = Object.freeze(["contain", "cover"]);
// How many panes show, and how they sit. Grid is two by two, or with three
// panes one large pane on the left and two stacked; with two panes it is the
// same as Row, so the bar offers Row and Column only.
export const PANE_COUNT_TITLES = Object.freeze(["One pane", "Two panes", "Three panes", "Four panes"]);
export const ARRANGEMENTS = Object.freeze(["grid", "row", "column"]);
export const ARRANGEMENT_TITLES = Object.freeze({
    grid: "Grid: two by two, or one large pane and two stacked",
    row: "Row: panes side by side",
    column: "Column: panes stacked top to bottom",
});
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_ZOOM = 16;

const normalizeCount = raw => (Number.isInteger(raw) && raw >= 1 && raw <= MAX_PANES ? raw : 1);
export const paneCount = record => normalizeCount(record?.count);
// What the panes show as: "single" with one pane; Grid with two panes is Row.
export function effectiveArrangement(record) {
    const count = paneCount(record);
    if (count === 1) return "single";
    const arrangement = ARRANGEMENTS.includes(record?.arrangement) ? record.arrangement : "grid";
    return count === 2 && arrangement === "grid" ? "row" : arrangement;
}

// The panes' CSS grid: columns, rows, and each cell's `gridRow` ("" for none).
// Cells keep their order whatever the arrangement, so a pane is the same
// pane (and keeps its content) when the arrangement changes.
export function paneGrid(count, arrangement) {
    const n = normalizeCount(count);
    const effective = effectiveArrangement({ count: n, arrangement });
    const spans = Array(n).fill("");
    const repeat = `repeat(${n}, 1fr)`;
    if (effective === "single") return { gridTemplateColumns: "1fr", gridTemplateRows: "1fr", spans };
    if (effective === "row") return { gridTemplateColumns: repeat, gridTemplateRows: "1fr", spans };
    if (effective === "column") return { gridTemplateColumns: "1fr", gridTemplateRows: repeat, spans };
    if (n === 3) {
        spans[0] = "1 / 3";
        return { gridTemplateColumns: "2fr 1fr", gridTemplateRows: "1fr 1fr", spans };
    }
    return { gridTemplateColumns: "1fr 1fr", gridTemplateRows: "1fr 1fr", spans };
}
// The first pane starts on the Composite, the others on the Guides.
// `link`: an output follows the editor's scene clock when it can (Phase 3B).
export const defaultPane = (index = 0) => ({ source: { kind: index === 0 ? "composite" : "guides" }, item: 0, fit: "contain", zoom: null, link: true });

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
    pane.link = raw.link !== false;
    return pane;
}

// A layout record: a pane count, an arrangement, and always four pane slots
// (the hidden ones keep their choice for when the count grows again),
// Composite in at most one of them.
export function normalizeLayout(raw) {
    const record = { ts: 0, count: 1, arrangement: "grid", panes: Array.from({ length: MAX_PANES }, (_, i) => defaultPane(i)) };
    if (!raw || typeof raw !== "object") return record;
    record.ts = Number.isFinite(Number(raw.ts)) && Number(raw.ts) > 0 ? Number(raw.ts) : 0;
    record.count = normalizeCount(raw.count);
    record.arrangement = ARRANGEMENTS.includes(raw.arrangement) ? raw.arrangement : "grid";
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

// One Composite pane and nothing else chosen: the default. It is stored
// dated like any layout, so another tab follows a return to it.
export function isDefaultLayout(record) {
    const { ts: _ts, ...value } = normalizeLayout(record);
    const { ts: _fresh, ...fresh } = normalizeLayout(null);
    return JSON.stringify(value) === JSON.stringify(fresh);
}

// The browser-local map {workflowKey: record}: invalid and undated records
// dropped, the newest 64 kept (Writing drafts' LRU pattern). A dated default
// stays: it is how another tab learns the layout went back to one pane.
export function normalizeLayoutMap(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const entries = [];
    for (const [key, value] of Object.entries(raw)) {
        if (typeof key !== "string" || !key || key.length > 200 || !value || typeof value !== "object") continue;
        const record = normalizeLayout(value);
        if (!record.ts) continue;
        entries.push([key, record]);
    }
    entries.sort((a, b) => a[1].ts - b[1].ts);
    return Object.fromEntries(entries.slice(-MAX_LAYOUT_RECORDS));
}

export const visiblePanes = record => normalizeLayout(record).panes.slice(0, paneCount(record));

// Show `source` in pane `index`. Composite sits in one pane at most: picking
// it where another pane has it swaps the two panes' sources.
export function assignSource(record, index, source) {
    const next = normalizeLayout(record);
    const clean = normalizeSource(source);
    if (!clean || index < 0 || index >= MAX_PANES) return next;
    if (clean.kind === "composite") {
        const other = next.panes.findIndex((pane, i) => i !== index && pane.source.kind === "composite");
        if (other !== -1) next.panes[other] = { ...next.panes[other], source: next.panes[index].source, item: 0, zoom: null, link: true };
    }
    next.panes[index] = { ...next.panes[index], source: clean, item: 0, zoom: null, link: true };
    return next;
}

export const compositePane = record => visiblePanes(record).findIndex(pane => pane.source.kind === "composite");

// ── Picker ───────────────────────────────────────────────────────────────────

// The pane picker's sections. `views`, `candidates` and `samplingCandidates`
// come from the Controls view (the server's lists of eligible outputs and core
// samplers); `seen` from a runs/live read (nodes that sent previews in a recent
// run); `drivers` from the scene. `nodeState(path)` says, where the page can
// tell (the canvas), that a seen node is switched off or gone: {inactive} |
// {missing} | null.
export function pickerSections({ views = [], candidates = [], samplingCandidates = [], seen = [], drivers = [], viewsError = null,
    viewsReady = true, nodeState = () => null } = {}) {
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
    // One View per node, whatever it shows: a path any View has is taken.
    const taken = new Set(views.map(view => view.path).filter(Boolean));
    const where = candidate => (candidate.inactive ? inactiveText(candidate.inactive) : candidate.path.includes(":") ? "Inside a subgraph" : "");
    const add = candidates.filter(candidate => !taken.has(candidate.path)).map(candidate => ({
        kind: "candidate", label: candidate.title || candidate.class_type, candidate, detail: where(candidate),
    }));
    const sampling = samplingCandidates.filter(candidate => !taken.has(candidate.path)).map(candidate => ({
        kind: "sampling", label: candidate.title || candidate.class_type, candidate, detail: where(candidate),
    }));
    const listed = new Set([...taken, ...sampling.map(item => item.candidate.path)]);
    for (const entry of seen) {
        const path = typeof entry?.source === "string" ? entry.source : "";
        if (!path || listed.has(path)) continue;
        listed.add(path);
        const candidate = { path, class_type: String(entry.class_type || ""), title: String(entry.title || "") };
        const state = nodeState(path);
        const off = state?.missing ? "This node is no longer in the workflow"
            : state?.inactive ? `${inactiveText(state.inactive)}. Switch it on to add it` : "";
        sampling.push({ kind: "sampling", label: candidate.title || candidate.class_type || path, candidate, seen: true,
            detail: off || "Sent previews in a recent run", disabled: !!off });
    }
    return { editor, views: viewItems, add, sampling, viewsError, viewsReady };
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
        return { message: null, chip: "Held", result: { entries: [hold.entry], run: hold.run, fixedIndex: true,
            // A held result links as it did when held (its run's data may be gone).
            editor_context: hold.editor_context || null, fresh: hold.fresh } };
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

// The pane for a sampling-preview View (Phase 4). `read` is the runs/live
// answer for its node ({state, live, run, latest_run}); `sampling` the
// server's capability ({native, kj: {state}}), from the same read. Returns
// {message, chip, chipTitle, show (show the newest fetched preview)}.
export const KJ_UNMATCHED = "Model Preview Override previews can't be matched to a run with this KJNodes version";
export function samplingPaneStatus({ view = null, read = null, sampling = null, sessionLost = false, viewsError = null } = {}) {
    const nothing = (message, chip) => ({ message, chip, show: false, chipTitle: "" });
    if (sessionLost) return nothing("Disconnected", null);
    if (viewsError) return nothing(viewsError, "Unavailable");
    if (!view) return nothing("This View is no longer in the workflow. Pick another source.", "Unavailable");
    if (VIEW_PROBLEMS.has(view.status)) return nothing(`${view.reason || "This View can't be shown."} Fix in Manage Controls.`, "Unavailable");
    // Without the KJ adapter its step previews carry no run: none can be shown truthfully.
    if (view.class_type === KJ_PREVIEW_CLASS && ["unsupported", "degraded"].includes(sampling?.kj?.state)) return nothing(KJ_UNMATCHED, "Unavailable");
    const live = read?.live || null;
    const steps = Number.isFinite(live?.step) ? (Number.isFinite(live?.total) && live.total > 0 ? ` · step ${live.step}/${live.total}` : ` · step ${live.step}`) : "";
    const chipTitle = live?.reattributed ? "Matched through the model chain" : "";
    // The owner's newest run left this node out (another output ran on its own).
    const leftOut = read?.latest_run && read.latest_run.in_scope === false;
    const outNote = leftOut ? " · Not in the newest run" : "";
    let status;
    switch (read?.state || "none") {
        case "live": status = { message: null, chip: `Sampling${steps}`, show: true }; break;
        case "last": status = { message: null, chip: `Last preview${steps}${outNote}`, show: true }; break;
        case "cached": status = { message: null, chip: `From cache${outNote}`, show: true }; break;
        case "previous": status = { message: null, chip: "Previous run · Running", show: true }; break;
        // A Model Preview Override on the sampler's model takes its previews while it runs too.
        case "waiting": status = { message: read.run?.suppressed_by ? "This sampler's previews go to Model Preview Override"
            : "Waiting for previews", chip: "Running", show: false }; break;
        case "no_previews": {
            // A run that was canceled or failed may have ended before the node ran.
            if (read.run?.outcome && read.run.outcome !== "success") {
                const word = outcomeWord(read.run);
                status = { message: word, chip: word, show: false };
                break;
            }
            const native = live?.publisher !== "kj" && view.class_type !== KJ_PREVIEW_CLASS;
            // A node ComfyUI served from its cache did not run, so it sent nothing. (An
            // Override node can be cached while its sampler runs: not said for KJ.)
            const message = read.run?.suppressed_by ? "This sampler's previews went to Model Preview Override"
                : native && read.run?.source_ran === false ? "This node came from ComfyUI's cache in the last run, so it sent no previews"
                : native && read.run?.preview_method === "none"
                    ? "Previews were off for this run (ComfyUI's Live preview method setting, or --preview-method)"
                    : "No previews arrived from this node in the last run";
            status = { message, chip: "No previews", show: false };
            break;
        }
        case "evicted": status = { message: "This preview is no longer kept", chip: "Unavailable", show: false }; break;
        case "too_large": status = { message: "Too large to show here", chip: "Unavailable", show: false }; break;
        case "unsupported": status = { message: "This preview type can't be shown here", chip: "Unsupported", show: false }; break;
        default: status = { message: leftOut ? "The newest run didn't include this node" : "No result yet", chip: null, show: false };
    }
    // A switched-off sampler keeps showing its last preview.
    if (view.participation === "inactive") status = { ...status, chip: "Switched off", message: status.show ? null : status.message };
    return { ...status, chipTitle };
}

// A client-side condition of a shown preview overrides the read.
export function samplingMediaStatus(condition) {
    if (condition === "too_large") return { message: "Too large to show here", chip: "Unavailable" };
    if (condition === "decode" || condition === "unsupported") return { message: "This preview type can't be shown here", chip: "Unsupported" };
    return null;
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
    // A sampling preview never runs on its own (inspection only).
    if (viewFeed(view) === VIEW_FEED_SAMPLING) return { show: false, reason: view.run_reason || "A sampling preview shows while another output runs" };
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
