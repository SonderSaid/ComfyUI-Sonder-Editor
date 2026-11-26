// The one apply path for a Modes choice: the Modes table's click and Manage's
// load both run it (plans/live-workflow-phase-4-controls-modes.md D8).
//
// The caller binds what the apply means for a run (`track`), what a fresh view
// decides (`decide`), and whether the apply is still wanted (`isCurrent`); this
// module owns only the order: flush, read, check, decide, write.

// Outcomes that leave the Controls short of what the caller asked for.
export const CHOICE_FAILURES = new Set(["refused", "not_confirmed", "flush_failed", "needs_install"]);

// Resolves to {status, reason?, detail?, fresh}; never rejects. `status` is one
// of applied, already, refused, not_confirmed, flush_failed, gone, superseded,
// needs_install. `decide(view, fresh)` returns (or resolves to) true to write, or
// the outcome to stop with.
// What the click saw (control_expectations.py): the choice's whole effect then,
// from the view the click was drawn on. Read before anything is awaited.
export function choiceExpectation(view, presetId) {
    const token = (view?.presets || []).find(p => p.id === presetId)?.token;
    if (typeof token !== "string" || !token) return null;
    return { workflow: typeof view.workflow_id === "string" ? view.workflow_id : "",
        targets: { [`preset:${presetId}:apply`]: { token } } };
}

const changedReason = (view, presetId) => {
    const name = (view?.presets || []).find(p => p.id === presetId)?.name || "This choice";
    return `“${name}” changed after you clicked it, so nothing was applied. Click it again to apply it as it is now.`;
};

export function applyChoice(host, presetId, { flushPending, track, decide = () => true, isCurrent = () => true,
    expect = choiceExpectation(host.getView?.(), presetId) }) {
    // Before tracking: the panel's flush waits for tracked work, so tracking
    // first would make this apply wait for itself.
    const flushed = flushPending();
    const work = (async () => {
        let fresh = false;
        try {
            if (!await flushed) return { status: "flush_failed", fresh };
            if (!isCurrent()) return { status: "superseded", fresh };
            const read = host.freshView ? await host.freshView() : { view: host.getView(), fresh: false };
            fresh = !!read.fresh;
            // A newer selection or a closed panel: nothing is written, so no
            // Undo step and no flip of the values.
            if (!isCurrent()) return { status: "superseded", fresh };
            // The same check as the write's, so "already selected" can't skip it:
            // a fresh view whose effect for this choice moved since the click.
            const now = expect && fresh ? (read.view?.presets || []).find(p => p.id === presetId)?.token : null;
            if (now && now !== expect.targets[`preset:${presetId}:apply`].token) {
                return { status: "refused", reason: changedReason(read.view, presetId), fresh };
            }
            let decision = decide(read.view, fresh);
            // Only a decide that had to read waits, so any other keeps its exact
            // order; while it read, the panel may have closed.
            if (typeof decision?.then === "function") {
                decision = await decision;
                if (decision === true && !isCurrent()) return { status: "superseded", fresh };
            }
            if (decision !== true) return { ...decision, fresh };
            const result = await host.applyChanges({ [`preset:${presetId}:apply`]: true }, expect ? { expect } : undefined);
            if (result?.ok) return { status: "applied", fresh };
            // A host that lost its answer (a mounted tab whose canvas reconnected)
            // can't say the apply failed, only that it isn't known.
            return { status: result?.unconfirmed ? "not_confirmed" : "refused", reason: result?.rejected?.[0]?.reason || "", fresh };
        } catch (error) {
            return { status: "refused", reason: "", detail: error?.message, fresh };
        }
    })();
    track(work);
    return work;
}
