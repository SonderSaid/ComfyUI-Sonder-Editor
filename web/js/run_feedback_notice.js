// Run notifications: one live toast per page and a receipt per run, the same in
// fullscreen, mounted and the Sonder App (plans/live-workflow-phase-3-job-feedback.md, D5).
//
// Two parts. The model is DOM-free and pure: it words a run's stage, receipt and
// outcome from the server's ``run_progress`` record (server/run_feedback.py)
// and, in the Sonder App, from the App's own run (SdkRunner's ``generation_status``,
// which stays the authority for an App run's state and outcome). The driver is
// one per page: it keeps the records this page owns, follows the earliest
// unfinished run with a single live notification updated only through its handle,
// and turns each earlier run's outcome into a receipt toast. It never touches the
// DOM; the notification Core and its presenters do.

import { ambiguityText } from "./controls_modes_model.js";
import { formatQueueTime, queueChunkLabel } from "./queue_format.js";

export const UNCONFIRMED_END = "The run ended, but Sonder could not confirm how.";

const TERMINAL = new Set(["completed", "failed", "cancelled", "removed"]);
const APP_FINAL = new Set(["landed", "failed", "canceled", "ended", "completed"]);
const APP_UNFINISHED = new Set(["submitting", "unconfirmed", "queued", "running", "finishing"]);
// How many runs a coalesced receipt toast still names ("a merged toast must
// still name the gesture it last counted").
const RECEIPTS_LISTED = 3;
const MAX_RECORDS = 256;

const arr = (value) => (Array.isArray(value) ? value : []);
const num = (value) => (Number.isFinite(Number(value)) && value !== null && value !== "" ? Number(value) : null);

// ── Model ────────────────────────────────────────────────────────────────────────

function rangeText(start, end, fps, mode, padding = 0) {
    const a = num(start), b = num(end);
    if (a == null || b == null) return "";
    // Timecode needs the run's own fps; without it the frames read exactly.
    const shown = mode === "timecode" && num(fps) ? "timecode" : "frames";
    const text = `In ${formatQueueTime(a, fps, shown)} · Out ${formatQueueTime(b, fps, shown)} (${formatQueueTime(Math.max(0, b - a), fps, shown)})`;
    const pad = num(padding) || 0;
    return pad > 0 ? `${text} · +${pad} padding frame${pad === 1 ? "" : "s"}` : text;
}

function modeLine(row) {
    const name = String(row?.name || "Mode");
    switch (row?.status) {
        case "selected": return `${name}: ${row.choice}`;
        case "ambiguous": return `${name}: ${ambiguityText({ matches: arr(row.matches).map((match) => ({ name: match })) })}`;
        case "unavailable": return `${name}: unavailable`;
        default: return `${name}: Custom`;
    }
}

/**
 * The receipt, as lines: the range this run rendered (or asked for while it
 * waits), whether the Render Queue was off or which job ran, then the Modes in
 * effect when there are any. A run with no Controls has no Modes line.
 */
export function receiptLines(record, { timecodeMode = "frames" } = {}) {
    const receipt = record?.receipt || {};
    const windows = arr(receipt.windows), jobs = arr(receipt.jobs), requested = arr(receipt.requested);
    const window = windows[0] || null;
    const asked = requested.find((item) => item.editor === window?.editor) || requested[0] || null;
    const job = jobs.find((item) => item.editor === window?.editor) || jobs[0] || null;
    const lines = [];
    if (window) {
        lines.push(rangeText(window.generation_start, window.generation_end, window.fps, timecodeMode, window.frame_count_padding));
    } else if (asked?.linked) {
        lines.push("Range set by the graph");
    } else if (asked && num(asked.selection_end) > num(asked.selection_start)) {
        // An empty selection (the Sonder App's unset 0–0) is no range: the window
        // the Editor executes names it once the run starts.
        lines.push(rangeText(asked.selection_start, asked.selection_end, null, "frames"));
    }
    if (job) {
        // A chunk only for a job that is one of several.
        const chunk = (parseInt(job.batch_total, 10) || 0) > 1 ? queueChunkLabel(job.batch_index, job.batch_total) : "";
        const name = [job.scene_name || "", chunk].filter(Boolean).join(" · ") || "a queued job";
        lines.push(job.mode === "peek" ? `Render Queue job: ${name} — previewed, not consumed` : `Render Queue job: ${name}`);
    } else if (asked?.render_queue_active === false) {
        lines.push("Render Queue off");
    } else if (window) {
        lines.push(asked?.render_queue_active ? "Render Queue had no pending job — the live selection ran" : "Live selection");
    } else if (asked?.render_queue_active) {
        lines.push("Render Queue on — the job is picked when the run starts");
    }
    const editors = new Set([...windows, ...requested].map((item) => item.editor)).size;
    if (editors > 1) lines.push(`+${editors - 1} more Editor${editors > 2 ? "s" : ""} in this run`);
    for (const row of arr(receipt.modes)) lines.push(modeLine(row));
    return lines.filter(Boolean);
}

function countProgress(count) {
    const value = num(count?.value), max = num(count?.max);
    return value != null && max != null && max > 1 ? { current: value, total: max } : null;
}

/**
 * What the run is doing now: ``{ verb, note, progress }``. ``appRun`` (the
 * Sonder App's own run) supplies the states the server record cannot know.
 */
export function stageLine(record, appRun = null) {
    const branch = (appRun?.purpose || record?.purpose) === "branch";
    if (appRun?.canceling) return { verb: "Canceling", note: "Waiting for ComfyUI to stop it", progress: null };
    if (appRun?.state === "submitting" || appRun?.state === "unconfirmed") return { verb: "Sending", note: "", progress: null };
    // An App run's own state leads once the record has ended (the App still
    // lands the take), and before a record exists.
    const state = (appRun && (!record || TERMINAL.has(record.state)))
        ? (APP_UNFINISHED.has(appRun.state) ? appRun.state : "finishing")
        : record?.state || "queued";
    if (state === "queued") return { verb: "Queued", note: "Waiting in ComfyUI's queue", progress: null };
    if (state === "finishing") return { verb: "Finishing", note: "", progress: null };
    const count = countProgress(record?.count);
    switch (record?.stage) {
        case "rendering": return { verb: "Rendering frames", note: "", progress: count };
        case "preparing_inputs": return { verb: "Preparing graph inputs", note: "", progress: null };
        case "encoding": return { verb: "Encoding frames", note: "", progress: count };
        case "saving": return { verb: branch ? "Saving the output" : "Saving the take", note: "", progress: null };
        default: break;
    }
    const title = String(record?.node_title || "");
    return { verb: title ? `Running ${title}` : "Running", note: "", progress: title ? count : null };
}

/**
 * How a run ended: ``{ tier, message, kind, note }``. ``kind`` keys the receipt
 * toast, so started and unstarted cancels never merge. App runs read their
 * outcome from the App's own state; the record adds the failing node and
 * whether a job went back to pending.
 */
export function outcomeFor(record, appRun = null) {
    const terminal = record?.terminal || {};
    const branch = (appRun?.purpose || record?.purpose) === "branch";
    const started = (num(record?.elapsed_ms) || 0) > 0 || terminal.outcome === "interrupted" || terminal.outcome === "error"
        || ["running", "finishing"].includes(appRun?.lastLiveState);
    const returned = arr(terminal.returned_job_ids).length > 0;
    const failedIn = terminal.node_type ? `Failed in ${terminal.node_type}` : "";
    const note = String(appRun?.note || "").trim();
    const state = appRun ? appRun.state : ({ completed: "completed", failed: "failed", cancelled: "canceled", removed: "removed" })[record?.state];
    switch (state) {
        case "landed":
            return { tier: "success", message: "Take landed on the timeline", kind: "landed", note: "" };
        case "completed":
            if (branch) return { tier: "success", message: "Output ready", kind: "completed", note: "" };
            return terminal.take_saved || !record
                ? { tier: "success", message: "Take landed on the timeline", kind: "landed", note: "" }
                : { tier: "success", message: "Run completed", kind: "completed", note: "" };
        case "failed":
            return {
                tier: "error",
                message: failedIn || (branch ? "Output failed" : appRun ? "Take failed" : "Run failed"),
                kind: "failed",
                note: appRun ? note : "",
            };
        case "canceled":
            return started
                ? { tier: "warning", message: returned ? "Canceled — the job returned to pending" : "Canceled", kind: "cancelled:started", note: "" }
                : { tier: "warning", message: "Canceled before it started", kind: "cancelled:unstarted", note: "" };
        case "removed":
            return { tier: "warning", message: "Removed before it started", kind: "removed", note: "" };
        case "ended":
            return { tier: "warning", message: branch ? "Output run ended" : "Take ended without landing", kind: "ended", note };
        default:
            return { tier: "warning", message: UNCONFIRMED_END, kind: "unconfirmed", note: "" };
    }
}

/** The notification fields for a live run (``tier: progress``). */
export function noticeFor(record, appRun = null, { timecodeMode = "frames", waiting = 0, workflow = "" } = {}) {
    const stage = stageLine(record, appRun);
    const lines = receiptLines(record, { timecodeMode });
    const message = [stage.note, ...lines.slice(0, 2)].filter(Boolean).join(" · ") || "Working";
    const detail = [workflow, ...lines.slice(2), waiting > 0 ? `+${waiting} waiting` : ""].filter(Boolean).join("\n") || null;
    return { tier: "progress", verb: stage.verb, message, detail, progress: stage.progress };
}

// ── Driver ───────────────────────────────────────────────────────────────────────

// A canvas run whose Editor left this page (its workflow tab was switched or
// closed): nothing here can follow it any more, so its live line must not tick on.
const DETACHED = { tier: "info", message: "Still running in another workflow", kind: "detached", note: "" };
const READ_RETRIES_MS = [1000, 2000, 4000, 8000];
const FAILURE_DETAIL_TIMEOUT_MS = 3000;

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * @param {object} args
 * @param {string} args.page              this page's name, for receipt sources
 * @param {Function} args.ownerMatch      (record) => whether this page shows it
 * @param {Function} args.notify          editor_notifications.notify
 * @param {boolean} [args.app]            the Sonder App: its runs are the state
 * @param {Function} [args.timecodeMode]  () => "frames" | "timecode"
 * @param {Function} [args.failureDetail] async (appRun) => detail text, for App failures
 * @param {Function} [args.now]           clock (tests)
 * @param {number[]} [args.readRetriesMs] waits between failed reads (tests)
 */
export function createRunFeedbackDriver({
    page,
    ownerMatch,
    notify,
    app = false,
    timecodeMode = () => "frames",
    failureDetail = null,
    now = () => Date.now(),
    readRetriesMs = READ_RETRIES_MS,
}) {
    const records = new Map();    // prompt id -> { record, receipt, startedAt, queuedSince, firstSeen }
    const items = new Map();      // key -> item (host: prompt id; App: run id)
    const receipts = new Map();   // receipt source -> { handle, lines[] }
    const appMode = !!app;
    let epoch = "";
    let live = null;              // { handle, key }
    let seen = 0;
    let destroyed = false;

    const mode = () => (timecodeMode() === "timecode" ? "timecode" : "frames");

    function keep(record) {
        const pid = String(record?.prompt_id || "");
        if (!pid) return null;
        const previous = records.get(pid);
        if (previous && previous.record.epoch === record.epoch && Number(record.seq) <= Number(previous.record.seq)) return previous;
        const entry = previous || { firstSeen: ++seen, startedAt: null, queuedSince: null, receipt: null };
        // Receipt fields travel only when they change; keep the last one.
        if (record.receipt) entry.receipt = record.receipt;
        entry.record = { ...record, receipt: entry.receipt };
        if (!entry.startedAt && Number(record.elapsed_ms) > 0) entry.startedAt = now() - Number(record.elapsed_ms);
        if (!entry.queuedSince && record.state === "queued") entry.queuedSince = now() - (Number(record.waited_ms) || 0);
        records.set(pid, entry);
        // Bounded like the server's finished-run cap: ended runs go first.
        while (records.size > MAX_RECORDS) {
            const keys = [...records.keys()];
            const done = keys.find((key) => !appMode && (!items.has(key) || items.get(key).ended)) || keys[0];
            records.delete(done);
            if (!appMode && items.get(done)?.ended) items.delete(done);
        }
        return entry;
    }

    function hostItem(pid) {
        let item = items.get(pid);
        if (!item) {
            item = { key: pid, ended: false, shownLive: false, project: "", firstSeenAt: now() };
            items.set(pid, item);
        }
        return item;
    }

    const recordOf = (item) => (appMode ? records.get(String(item.app?.prompt_id || ""))?.record : records.get(item.key)?.record) || null;
    const entryOf = (item) => (appMode ? records.get(String(item.app?.prompt_id || "")) : records.get(item.key)) || null;
    const startedAtOf = (item) => entryOf(item)?.startedAt || item.appStartedAt || null;
    const queuedSinceOf = (item) => entryOf(item)?.queuedSince || item.firstSeenAt || null;
    const unfinished = (item) => !item.ended && (item.ending
        || (appMode ? APP_UNFINISHED.has(String(item.app?.state || "")) : !TERMINAL.has(recordOf(item)?.state)));
    const isRunning = (item) => {
        const state = appMode ? item.app?.state : recordOf(item)?.state;
        return state === "running" || state === "finishing";
    };
    const order = (item) => item.order ?? records.get(item.key)?.firstSeen ?? 0;
    // Host runs this page shows as live (the ones a read or a detach may end).
    const heldKeys = (scope = () => true) => [...items.values()]
        .filter((item) => !item.ended && item.shownLive && scope(recordOf(item), item)).map((item) => item.key);

    function receiptSummary(item) {
        const lines = receiptLines(recordOf(item), { timecodeMode: mode() });
        return lines.slice(0, 2).join(" · ");
    }

    function announce(item, outcome, extraDetail = "") {
        const source = `run-outcome:${page}:${outcome.kind}`;
        let slot = receipts.get(source);
        if (!slot || !slot.handle?.alive?.()) slot = { handle: null, lines: [] };
        const summary = receiptSummary(item);
        if (summary) slot.lines = [...slot.lines, summary].slice(-RECEIPTS_LISTED);
        const detail = [outcome.note, extraDetail, ...receiptsFor(item, slot)].filter(Boolean).join("\n") || null;
        slot.handle = notify({ tier: outcome.tier, message: outcome.message, detail, source });
        receipts.set(source, slot);
    }

    // A single run's receipt names its full lines; a coalesced toast lists the
    // last few runs it counted.
    function receiptsFor(item, slot) {
        if (slot.lines.length > 1) return slot.lines;
        const lines = receiptLines(recordOf(item), { timecodeMode: mode() });
        if (appMode && item.app?.template_name) lines.unshift(item.app.template_name);
        return lines;
    }

    async function end(item) {
        if (item.ended || item.ending) return;
        let extra = "";
        if (appMode && item.app?.state === "failed" && failureDetail) {
            // Still the run the live toast follows while its detail is fetched,
            // for a few seconds at most: a slow read must not freeze the live line.
            item.ending = true;
            try {
                extra = String((await Promise.race([failureDetail(item.app), wait(FAILURE_DETAIL_TIMEOUT_MS).then(() => "")])) || "");
            } catch (_) { extra = ""; }
            item.ending = false;
            if (destroyed) return;
        }
        item.ended = true;
        const outcome = item.forcedOutcome || outcomeFor(recordOf(item), appMode ? item.app : null);
        const others = [...items.values()].some((other) => other !== item && unfinished(other));
        if (live && live.key === item.key && !others && live.handle.alive()) {
            // A single run: the one live toast resolves in place to its outcome.
            const lines = receiptLines(recordOf(item), { timecodeMode: mode() });
            if (appMode && item.app?.template_name) lines.unshift(item.app.template_name);
            live.handle.resolve({
                tier: outcome.tier, verb: null, message: outcome.message, startedAt: null,
                detail: [outcome.note, extra, ...lines].filter(Boolean).join("\n") || null,
            });
            live = null;
        } else {
            announce(item, outcome, extra);
        }
        render();
    }

    function forceEnd(keys, outcome) {
        for (const key of keys) {
            const item = items.get(key);
            if (!item || item.ended) continue;
            item.forcedOutcome = outcome;
            end(item);
        }
    }

    function render() {
        if (destroyed) return;
        const open = [...items.values()].filter(unfinished);
        const running = open.filter(isRunning).sort((a, b) => (startedAtOf(a) || 0) - (startedAtOf(b) || 0) || order(a) - order(b));
        const waitingItems = open.filter((item) => !isRunning(item)).sort((a, b) => order(a) - order(b));
        const followed = running[0] || waitingItems[0] || null;
        if (!followed) {
            if (live?.handle?.alive()) live.handle.dismiss();
            live = null;
            return;
        }
        if (followed.ending) return; // its outcome is on the way: keep the last line
        const notice = noticeFor(recordOf(followed), appMode ? followed.app : null, {
            timecodeMode: mode(), waiting: open.length - 1, workflow: appMode ? followed.app?.template_name || "" : "",
        });
        // The clock counts this run's own time: since it started, or since it was queued.
        const fields = { ...notice, startedAt: (isRunning(followed) ? startedAtOf(followed) : queuedSinceOf(followed)) || null };
        if (live?.handle?.alive()) {
            // Handle-only updates: the live toast moves on to the next run in place.
            live.key = followed.key;
            live.handle.update(fields);
            return;
        }
        // Gone from the screen: the author dismissed it. Stay quiet for that run;
        // the next run to lead opens a new one.
        if (live && live.key === followed.key) return;
        live = { handle: notify({ ...fields, foreground: true, source: `run-live:${page}` }), key: followed.key };
    }

    // ── Host pages (canvas and mounted): records are the whole state ──

    function applyRecord(record, { quiet = false, project = "" } = {}) {
        if (destroyed || !record || typeof record !== "object") return;
        const pid = String(record.prompt_id || "");
        const known = items.get(pid);
        // A run this page already shows stays followed even after the Editor
        // host that owned it left the page (another one may still relay it).
        if (!ownerMatch(record) && !(known && (known.detached || !known.ended))) return;
        const entry = keep(record);
        if (!entry) return;
        if (record.epoch) {
            // A record from a new server process: runs held from the old one died with it.
            if (epoch && record.epoch !== epoch && !appMode) {
                forceEnd(heldKeys((held) => held?.epoch && held.epoch !== record.epoch), outcomeFor(null));
            }
            epoch = record.epoch;
        }
        if (appMode) {
            // The App joins a record to its own run by prompt id; state stays the App's.
            render();
            return;
        }
        const item = hostItem(pid);
        if (project) item.project = String(project);
        if (item.detached) {
            // Followed again (its Editor came back, or another host relays it).
            item.detached = false;
            item.ended = false;
            item.forcedOutcome = null;
        }
        if (item.ended) return;
        if (TERMINAL.has(entry.record.state)) {
            if (quiet && !item.shownLive) { item.ended = true; return; }
            end(item);
            return;
        }
        item.shownLive = true;
        render();
    }

    /**
     * An owner-gated read's records. A fresh page shows live runs only, plus
     * outcomes of runs it was already showing. ``held`` is what the read covers:
     * the runs this page showed (for the read's scope) when it was asked, so a
     * run bound since, or owned by another host, is never ended by it. A held run
     * the read no longer lists, or one from an older server process, ends as
     * unconfirmed.
     */
    function applyRead(payload, { held = null, project = "" } = {}) {
        if (destroyed || !payload || payload.ok === false) return;
        const present = new Set();
        for (const record of arr(payload.records)) {
            if (!ownerMatch(record)) continue;
            present.add(String(record.prompt_id));
            applyRecord(record, { quiet: true, project });
        }
        if (appMode) return;
        const stale = (key) => {
            const recordEpoch = records.get(key)?.record?.epoch;
            return !!(payload.epoch && recordEpoch && recordEpoch !== payload.epoch);
        };
        forceEnd((held ?? heldKeys()).filter((key) => !present.has(key) || stale(key)), outcomeFor(null));
    }

    /**
     * Read and apply, on subscribe and on reconnect. ``scope(record)`` names the
     * runs this read can speak for (one Editor host's). A read that keeps failing
     * (a restarted server whose hosts have not come back) ends what it covers as
     * unconfirmed rather than leaving a live line ticking on.
     */
    async function read(fetchRead, { scope = () => true, project = "" } = {}) {
        const held = appMode ? null : heldKeys(scope);
        for (let attempt = 0; ; attempt++) {
            let payload = null;
            try { payload = await fetchRead(); } catch (_) { payload = null; }
            if (destroyed || payload?.skip) return;
            if (payload && payload.ok !== false) {
                applyRead(payload, { held, project });
                return;
            }
            if (attempt >= readRetriesMs.length) break;
            await wait(readRetriesMs[attempt]);
            if (destroyed) return;
        }
        if (held) forceEnd(held, outcomeFor(null));
    }

    /**
     * A project socket on this page closed (a workflow tab switch, a removed
     * Editor, a project switch): end the live runs no socket here still relays
     * (``watched(project)``), with a neutral note, so no live line ticks on with
     * nothing to update it. Their records revive them.
     */
    function detach({ watched = () => false } = {}) {
        if (destroyed || appMode) return;
        for (const key of heldKeys()) {
            const item = items.get(key);
            const record = recordOf(item);
            if (item.project ? watched(item.project) : (record && ownerMatch(record))) continue;
            item.detached = true;
            item.forcedOutcome = DETACHED;
            end(item);
        }
    }

    // ── Sonder App: the App's runs are the state; records add stage and receipt ──

    function applyAppRuns(runs, { primed = true } = {}) {
        if (destroyed || !appMode) return;
        const now_ = now();
        // A run gone from the session's status (an ended session, a run another
        // session took over) is no longer this page's to report: end it quietly.
        const present = new Set(arr(runs).map((run) => String(run?.run_id || "")));
        for (const [key, item] of items) {
            if (!item.ending && !present.has(key)) items.delete(key);
        }
        for (const run of arr(runs)) {
            const key = String(run?.run_id || "");
            if (!key) continue;
            let item = items.get(key);
            if (!item) {
                item = { key, ended: false, order: ++seen, app: null, appStartedAt: null, firstSeenAt: now_ };
                items.set(key, item);
                // A reload's first snapshot reports runs that already ended: no replay.
                if (!primed && APP_FINAL.has(String(run.state || ""))) item.ended = true;
            }
            const lastLiveState = ["running", "finishing"].includes(run.state) ? run.state : item.app?.lastLiveState;
            item.app = { ...run, lastLiveState };
            if (run.state === "running" && !item.appStartedAt) item.appStartedAt = now_;
        }
        // Ends after the whole snapshot is read: a take that landed while the next
        // one runs is a receipt, not the end of the live toast.
        for (const item of items.values()) {
            if (APP_FINAL.has(String(item.app?.state || "")) && !item.ended) end(item);
        }
        render();
    }

    return {
        applyRecord,
        applyRead,
        read,
        detach,
        applyAppRuns,
        /** Whether this page has a record for the prompt (the canvas encode pill's guard). */
        knows: (promptId) => records.has(String(promptId || "")),
        destroy() {
            destroyed = true;
            if (live?.handle?.alive()) live.handle.dismiss();
            live = null;
        },
    };
}
