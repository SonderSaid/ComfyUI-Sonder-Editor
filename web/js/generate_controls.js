// Generate ×N and Cancel all for an editor whose host runs the workflow itself
// (Sonder App / SDK mode). The canvas modes keep ComfyUI's own Run; this is SDK
// mode's substitute, drawn with the existing chrome: a toolbar button and toasts.
// A run's notification and receipt are the shared ones every host shows
// (run_feedback_notice.js): the App's runs give their state and outcome, and the
// server's run records add the stage, count and receipt.
//
// Module contract: the host (`generationHost`) owns state and networking —
// `subscribe(fn)`, `activeRuns()`, `generate(count, opts)`, `cancelAll()`, and
// optionally `subscribeRunFeedback(fn)`, `refreshStatus()`, `details`.
// This module owns the DOM and listeners and returns `destroy()`.

import {
    notify,
    notifyError,
    notifyInfo,
    notifySuccess,
    notifyWarning,
} from "./editor_notifications.js";
import { EDITOR_COLORS as COLORS, FONT, TYPE, setButtonDisabled } from "./editor_theme.js";
import { createRunFeedbackDriver } from "./run_feedback_notice.js";

export const GENERATE_MAX_COUNT = 8;
export const GENERATE_MAX_UNFINISHED = 16;

const UNFINISHED_STATES = new Set(["submitting", "unconfirmed", "queued", "running", "finishing"]);

export function clampGenerateCount(value) {
    return Math.max(1, Math.min(GENERATE_MAX_COUNT, Math.round(Number(value) || 1)));
}

// Pure: which face the button shows for a status snapshot.
export function generateButtonState(runs, { submitting = false, count = 1 } = {}) {
    const active = (runs || []).filter((run) => UNFINISHED_STATES.has(String(run?.state || "")));
    return {
        generate: submitting ? "sending" : active.length + count > GENERATE_MAX_UNFINISHED ? "full" : "idle",
        cancel: !active.length ? "hidden" : active.every(run => run.canceling) ? "canceling" : "active",
    };
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
    if (code === "canceled_before_submit") return { tier: "info", message: "Canceled before it was sent." };
    if (code === "generate_refused") {
        const problems = Array.isArray(payload.problems) ? payload.problems : [];
        return { tier: "error", message: "This workflow can't generate yet.", detail: problems.join("\n") || null };
    }
    if (code === "missing_nodes") {
        return { tier: "error", message: "This workflow needs nodes that aren't installed.", detail: payload.error || null };
    }
    if (code === "sdk_not_installed" || code === "proxy_not_installed") {
        return {
            tier: "error",
            message: "The Sonder App packages are not installed.",
            detail: "Install them from the editor: Settings → Sonder App.",
        };
    }
    if (code === "sdk_uninstalling") {
        return { tier: "warning", message: "The Sonder App packages are being removed.", detail: "Generate is unavailable until they are installed again." };
    }
    // The Sonder App switched workflows between the click and the server.
    if (code === "workflow_changed" || code === "workflow_switching") {
        return { tier: "warning", message: "The workflow changed before this Generate was sent.", detail: "Nothing was generated. Generate again on the workflow now shown." };
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
    else if (notice.tier === "info") notifyInfo(notice.message, extra);
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
 * @param {Function} [args.timecodeMode] "frames" | "timecode", for run receipts
 */
export function createGenerateControls({
    host,
    makeButton,
    setVariant,
    makeStepButtons,
    buttonOptions,
    beforeGenerate = null,
    renderCacheMaxBytes = () => 0,
    timecodeMode = () => "frames",
}) {
    let count = 1;
    let submitting = false;
    let pendingGenerate = null;
    let destroyed = false;
    let latestRuns = [];
    let primed = false;
    // The App's runs drive the shared notice; a record joins its run by prompt
    // id, so any record with App run ids is kept (the driver shows only this
    // session's runs).
    const feedback = createRunFeedbackDriver({
        page: "app",
        app: true,
        notify,
        timecodeMode,
        ownerMatch: (record) => Array.isArray(record?.run_ids) && record.run_ids.length > 0,
        // A failed take's detail arrives only on an owner-gated read.
        failureDetail: async (run) => {
            await Promise.resolve(host.refreshStatus?.()).catch(() => {});
            return host.details?.get(run.run_id) || "";
        },
    });
    const unsubscribeFeedback = host.subscribeRunFeedback?.((message) => {
        if (destroyed) return;
        if (message?.record) feedback.applyRecord(message.record);
        else if (message?.read) feedback.applyRead(message.read);
    }) || (() => {});

    const element = document.createElement("span");
    element.dataset.sonderGenerate = "true";
    element.style.cssText = "display:inline-flex;align-items:center;gap:3px;white-space:nowrap;flex:0 0 auto;";

    const button = makeButton("Generate", "", buttonOptions.primary, "white-space:nowrap;");
    button.type = "button";
    setVariant(button, "primary");
    const cancelButton = makeButton("Cancel all", "Stop every take that is still queued or running", buttonOptions.secondary, "white-space:nowrap;");
    cancelButton.type = "button";
    setVariant(cancelButton, "secondary");

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
    element.append(button, countGroup, cancelButton);

    function render() {
        if (destroyed) return;
        countLabel.textContent = `×${count}`;
        const state = generateButtonState(latestRuns, { submitting, count });
        button.textContent = state.generate === "sending" ? "Sending…" : "Generate";
        // A Sonder App workflow switch in flight: the click would land on neither.
        setButtonDisabled(button, state.generate !== "idle" || !!host.switching);
        const takes = count === 1 ? "one take" : `${count} takes`;
        button.title = state.generate === "sending" ? "Sending the takes to ComfyUI"
            : state.generate === "full" ? `Only ${GENERATE_MAX_UNFINISHED} takes may be unfinished. Lower the count, wait, or cancel some.`
            : `Run this workflow for ${takes} of the current selection`;
        cancelButton.style.display = state.cancel === "hidden" ? "none" : "";
        cancelButton.textContent = state.cancel === "canceling" ? "Canceling…" : "Cancel all";
        cancelButton.title = state.cancel === "canceling" ? "Waiting for ComfyUI to stop the takes"
            : "Stop every take that is still queued or running";
    }

    const unsubscribe = host.subscribe((status) => {
        if (destroyed) return;
        latestRuns = Array.isArray(status?.runs) ? status.runs : [];
        // The first snapshot (a reload, a reopen) reports finished runs that
        // were already announced, or that ended while nobody was looking.
        feedback.applyAppRuns(latestRuns, { primed });
        if (status && Number(status.seq) >= 0) primed = true;
        render();
    });

    button.addEventListener("click", async () => {
        if (destroyed) return;
        const state = generateButtonState(latestRuns, { submitting, count });
        if (state.generate !== "idle") return;
        submitting = true;
        pendingGenerate = new AbortController();
        render();
        const requested = count;
        // The Sonder App's workflow is the one on screen at the click.
        const release = host.holdWorkflow?.();
        const templateId = host.templateId;
        try {
            await beforeGenerate?.();
            const result = await host.generate(requested, { renderCacheMaxBytes: renderCacheMaxBytes(), signal: pendingGenerate.signal, templateId });
            notifyTier(generateResultNotice(result, requested));
        } catch (err) {
            notifyError("Generate failed.", { detail: String(err?.message || err), source: "sdk-generate-request" });
        } finally {
            release?.();
            submitting = false;
            pendingGenerate = null;
            render();
        }
    });

    cancelButton.addEventListener("click", async () => {
        if (destroyed) return;
        // Invalidate the click even when beforeGenerate has not yet entered
        // host.generate. HTTP cancellation owns already dispatched requests.
        pendingGenerate?.abort();
        // Cancel remains live while a Generate request is still sending.
        try {
            const result = await host.cancelAll();
            const code = String(result?.payload?.code || "");
            if (!result?.ok && code !== "not_owner" && code !== "session_ended") {
                notifyError("Couldn't cancel the takes.", {
                    detail: result?.payload?.error || code || null,
                    source: "sdk-cancel",
                });
            }
        } catch (err) {
            notifyError("Couldn't cancel the takes.", { detail: String(err?.message || err), source: "sdk-cancel" });
        }
    });

    render();

    return {
        element,
        destroy() {
            destroyed = true;
            unsubscribe();
            unsubscribeFeedback();
            feedback.destroy();
        },
    };
}
