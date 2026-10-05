// Generate ×N and Cancel all for an editor whose host runs the workflow itself
// (Sonder App / SDK mode). The canvas modes keep ComfyUI's own Run and progress;
// this is SDK mode's substitute, drawn with the existing chrome: a toolbar
// button, the foreground progress notification and toasts.
//
// Module contract: the host (`generationHost`) owns state and networking —
// `subscribe(fn)`, `activeRuns()`, `generate(count, opts)`, `cancelAll()`.
// This module owns the DOM and listeners and returns `destroy()`.

import {
    notifyError,
    notifyProgress,
    notifySuccess,
    notifyWarning,
} from "./editor_notifications.js";
import { EDITOR_COLORS as COLORS, FONT, TYPE } from "./editor_theme.js";

export const GENERATE_MAX_COUNT = 8;

const FINAL_STATES = new Set(["landed", "failed", "canceled", "ended"]);
const UNFINISHED_STATES = new Set(["submitting", "unconfirmed", "queued", "running", "finishing"]);

export function clampGenerateCount(value) {
    return Math.max(1, Math.min(GENERATE_MAX_COUNT, Math.round(Number(value) || 1)));
}

// Pure: which face the button shows for a status snapshot.
export function generateButtonState(runs, { submitting = false } = {}) {
    const active = (runs || []).filter((run) => UNFINISHED_STATES.has(String(run?.state || "")));
    if (active.length && active.every((run) => run.canceling)) return "canceling";
    if (active.length) return "active";
    if (submitting) return "submitting";
    return "idle";
}

// Pure: the progress line for the runs of the current batch.
export function describeBatchProgress(batchRuns) {
    const runs = batchRuns || [];
    const total = runs.length;
    if (!total) return null;
    const done = runs.filter((run) => FINAL_STATES.has(String(run?.state || ""))).length;
    const current = runs.find((run) => run?.state === "running")
        || runs.find((run) => UNFINISHED_STATES.has(String(run?.state || "")));
    const index = Math.min(total, done + 1);
    let suffix = "";
    if (current?.state === "queued") suffix = " · waiting in ComfyUI's queue";
    else if (current?.state === "finishing") suffix = " · saving the take";
    else if (current?.state === "submitting" || current?.state === "unconfirmed") suffix = " · sending";
    const progress = current?.state === "running" && Number.isFinite(Number(current.progress)) && current.progress !== null
        ? Math.max(0, Math.min(1, Number(current.progress)))
        : null;
    return { message: `Generating ${index} of ${total}${suffix}`, progress };
}

// Pure: the toast for a run that just became final, or null. Messages carry no
// take index: same-source toasts coalesce into one with a count ("×3"), which
// would otherwise show only the last index.
export function outcomeToast(run) {
    const note = String(run?.note || "").trim();
    switch (run?.state) {
        case "landed":
            return { tier: "success", message: "Take landed on the timeline", source: "sdk-generate-landed" };
        case "failed":
            return { tier: "error", message: "Take failed", detail: note || null, source: "sdk-generate-failed" };
        case "canceled":
            return { tier: "warning", message: "Take canceled", detail: note || null, source: "sdk-generate-canceled" };
        case "ended":
            return { tier: "warning", message: "Take ended without landing", detail: note || null, source: "sdk-generate-ended" };
        default:
            return null;
    }
}

// Pure: the notification for a Generate request the server refused or only
// partly accepted, or null when nothing needs saying.
export function generateResultNotice(result, requested) {
    const payload = result?.payload || {};
    const code = String(payload.code || "");
    if (result?.ok) {
        const submitted = Array.isArray(payload.run_ids) ? payload.run_ids.length : 0;
        if (payload.error && submitted < requested) {
            return {
                tier: "warning",
                message: `Only ${submitted} of ${requested} takes were sent.`,
                detail: payload.error?.error || null,
            };
        }
        return null;
    }
    if (code === "not_owner" || code === "session_ended") return null; // the page blocker says it
    if (code === "generate_refused") {
        const problems = Array.isArray(payload.problems) ? payload.problems : [];
        return { tier: "error", message: "This workflow can't generate yet.", detail: problems.join("\n") || null };
    }
    if (code === "sdk_not_installed") {
        return {
            tier: "error",
            message: "The Sonder App packages are not installed.",
            detail: "Install them from the editor: Settings → Sonder App.",
        };
    }
    if (code === "too_many_runs") {
        return { tier: "warning", message: "Too many takes are already waiting.", detail: payload.error || null };
    }
    if (code === "network") {
        return { tier: "error", message: "Couldn't reach ComfyUI to generate.", detail: payload.error || null };
    }
    if (result?.status === 503) {
        return { tier: "error", message: "The Sonder App couldn't connect to ComfyUI.", detail: payload.error || null };
    }
    const nested = payload.error && typeof payload.error === "object" ? payload.error.error : payload.error;
    return { tier: "error", message: "ComfyUI didn't accept the run.", detail: nested || code || null };
}

function notifyTier(notice, opts = {}) {
    if (!notice) return;
    const extra = { detail: notice.detail ?? null, source: notice.source || opts.source || "sdk-generate-request" };
    if (notice.tier === "success") notifySuccess(notice.message, extra);
    else if (notice.tier === "warning") notifyWarning(notice.message, extra);
    else notifyError(notice.message, extra);
}

/**
 * @param {object} args
 * @param {object} args.host               generationHost
 * @param {Function} args.makeButton       top-chrome button factory (text, title, options, extraCss)
 * @param {Function} args.setVariant       (button, variant) restyles a button
 * @param {Function} args.makeStepButtons  top-chrome step factory (title, onStep(direction))
 * @param {object} args.buttonOptions      top-chrome BUTTON_OPTIONS
 * @param {Function} [args.beforeGenerate] awaited before each Generate (drains pending edits)
 * @param {Function} [args.renderCacheMaxBytes] current render-cache budget
 */
export function createGenerateControls({
    host,
    makeButton,
    setVariant,
    makeStepButtons,
    buttonOptions,
    beforeGenerate = null,
    renderCacheMaxBytes = () => 0,
}) {
    let count = 1;
    let submitting = false;
    let face = "";
    let destroyed = false;
    let progressHandle = null;
    let batchIds = [];
    let latestRuns = [];
    let primed = false;
    const announced = new Set();

    const element = document.createElement("span");
    element.dataset.sonderGenerate = "true";
    element.style.cssText = "display:inline-flex;align-items:center;gap:3px;white-space:nowrap;flex:0 0 auto;";

    const button = makeButton("Generate", "", buttonOptions.primary, "white-space:nowrap;");
    button.type = "button";

    const countLabel = document.createElement("span");
    countLabel.style.cssText = `
        color: ${COLORS.textSecondary};
        font-family: ${FONT.sans};
        font-size: ${TYPE.t11}px;
        min-width: 18px;
        text-align: right;
        font-variant-numeric: tabular-nums;
    `;
    const steps = makeStepButtons("Takes per Generate", (direction) => {
        count = clampGenerateCount(count + (direction === "up" ? 1 : -1));
        render();
    });
    const countGroup = document.createElement("span");
    countGroup.style.cssText = "display:inline-flex;align-items:center;gap:2px;flex:0 0 auto;";
    countGroup.title = `Takes per Generate (1–${GENERATE_MAX_COUNT})`;
    countGroup.append(countLabel, steps);
    countGroup.addEventListener("wheel", (event) => {
        event.preventDefault();
        event.stopPropagation();
        count = clampGenerateCount(count + (event.deltaY < 0 ? 1 : -1));
        render();
    }, { passive: false });
    element.append(button, countGroup);

    function render() {
        if (destroyed) return;
        countLabel.textContent = `×${count}`;
        const next = generateButtonState(latestRuns, { submitting });
        const takes = count === 1 ? "one take" : `${count} takes`;
        if (next !== face) {
            face = next;
            if (next === "active") {
                button.textContent = "Cancel all";
                setVariant(button, "secondary");
            } else if (next === "canceling") {
                button.textContent = "Canceling…";
                setVariant(button, "secondary");
            } else if (next === "submitting") {
                button.textContent = "Sending…";
                setVariant(button, "primary");
            } else {
                button.textContent = "Generate";
                setVariant(button, "primary");
            }
            countGroup.style.display = next === "idle" || next === "submitting" ? "inline-flex" : "none";
        }
        button.title = {
            active: "Stop every take that is still queued or running",
            canceling: "Waiting for ComfyUI to stop the takes",
            submitting: "Sending the takes to ComfyUI",
            idle: `Run this workflow for ${takes} of the current selection`,
        }[next];
    }

    function updateProgress(runs) {
        for (const run of runs) {
            if (UNFINISHED_STATES.has(String(run?.state || "")) && !batchIds.includes(run.run_id)) {
                batchIds.push(run.run_id);
            }
        }
        const byId = new Map(runs.map((run) => [run.run_id, run]));
        const batchRuns = batchIds.map((id) => byId.get(id)).filter(Boolean);
        const stillActive = batchRuns.some((run) => UNFINISHED_STATES.has(String(run.state || "")));
        if (!stillActive) {
            progressHandle?.dismiss();
            progressHandle = null;
            batchIds = [];
            return;
        }
        const line = describeBatchProgress(batchRuns);
        if (!progressHandle) {
            progressHandle = notifyProgress({
                source: "sdk-generate",
                foreground: true,
                message: line.message,
                progress: line.progress,
            });
        } else {
            progressHandle.update({ message: line.message, progress: line.progress });
        }
    }

    function announceOutcomes(runs) {
        for (const run of runs) {
            if (!FINAL_STATES.has(String(run?.state || "")) || announced.has(run.run_id)) continue;
            announced.add(run.run_id);
            // The first snapshot (a reload, a reopen) reports finished runs that
            // were already announced, or that ended while nobody was looking.
            if (primed) notifyTier(outcomeToast(run));
        }
    }

    const unsubscribe = host.subscribe((status) => {
        if (destroyed) return;
        latestRuns = Array.isArray(status?.runs) ? status.runs : [];
        announceOutcomes(latestRuns);
        if (status && Number(status.seq) >= 0) primed = true;
        updateProgress(latestRuns);
        render();
    });

    button.addEventListener("click", async () => {
        if (destroyed) return;
        const state = generateButtonState(latestRuns, { submitting });
        // Cancel all is live while a batch is still being sent: the server
        // stops submitting the rest.
        if (state === "active" || state === "canceling") {
            const result = await host.cancelAll();
            const code = String(result?.payload?.code || "");
            if (!result?.ok && code !== "not_owner" && code !== "session_ended") {
                notifyError("Couldn't cancel the takes.", {
                    detail: result?.payload?.error || code || null,
                    source: "sdk-cancel",
                });
            }
            return;
        }
        if (submitting) return;
        submitting = true;
        render();
        const requested = count;
        try {
            await beforeGenerate?.();
            const result = await host.generate(requested, { renderCacheMaxBytes: renderCacheMaxBytes() });
            notifyTier(generateResultNotice(result, requested));
        } catch (err) {
            notifyError("Generate failed.", { detail: String(err?.message || err), source: "sdk-generate-request" });
        } finally {
            submitting = false;
            render();
        }
    });

    render();

    return {
        element,
        destroy() {
            destroyed = true;
            unsubscribe();
            progressHandle?.dismiss();
            progressHandle = null;
        },
    };
}
