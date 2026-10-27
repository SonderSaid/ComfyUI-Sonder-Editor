// The one apply path for a Modes choice: the Modes table's click and Manage's
// load both run it (plans/live-workflow-phase-4-controls-modes.md D8).
//
// The caller binds what the apply means for a run (`track`), what a fresh view
// decides (`decide`), and whether the apply is still wanted (`isCurrent`); this
// module owns only the order: flush, read, check, decide, write.

// Outcomes that leave the Controls short of what the caller asked for.
export const CHOICE_FAILURES = new Set(["refused", "not_confirmed", "flush_failed"]);

// Resolves to {status, reason?, detail?, fresh}; never rejects. `status` is one
// of applied, already, refused, not_confirmed, flush_failed, gone, superseded.
// `decide(view, fresh)` returns true to write, or the outcome to stop with.
export function applyChoice(host, presetId, { flushPending, track, decide = () => true, isCurrent = () => true }) {
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
            const decision = decide(read.view, fresh);
            if (decision !== true) return { ...decision, fresh };
            const result = await host.applyChanges({ [`preset:${presetId}:apply`]: true });
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
