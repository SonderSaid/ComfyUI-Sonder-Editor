// Guards an authored modal draft against an accidental dismissal.
//
// A backdrop click or Escape is easy to hit by accident, and these editors hold
// substantial authored state — kind, handle, name, definition, sources, voice
// and attachment defaults for an identity; whole declaration groups for a
// prompt format. Discarding all of it silently on a stray click is the single
// most-reported friction in this tool.
//
// Two rules this module exists to keep:
//
//   1. **Cancel is never guarded.** It is explicit intent, and prompting there
//      would make the confirm meaningless by making it routine.
//   2. **The guard must not live inside the modal's `close()`.** That function
//      is also returned as a programmatic cleanup handle and invoked during
//      teardown — when another editor opens, or when the whole panel unmounts.
//      A blocking confirm fired from teardown would stall an unrelated flow.
//      Attach it to the dismissal gestures only.

/** Snapshot the authored value of every control the caller names.
 *
 *  Controls are read positionally, so callers pass the same list they built the
 *  modal from; a control that appears later still compares correctly because a
 *  missing entry reads as an empty string rather than throwing.
 */
export function readDraftSnapshot(controls) {
    return (Array.isArray(controls) ? controls : []).map((control) => {
        if (!control) return "";
        if (control.type === "checkbox") return control.checked ? "1" : "0";
        if (Array.isArray(control.selectedOptions)) {
            return control.selectedOptions.map((option) => option.value).join("\u0000");
        }
        if (control.selectedOptions) {
            return [...control.selectedOptions].map((option) => option.value)
                .join("\u0000");
        }
        return String(control.value ?? "");
    }).join("");
}

/**
 * Create a dismissal guard for one modal.
 *
 * `controls` is read on every check rather than captured once, so a modal that
 * rebuilds rows (physical sources, declaration groups) still compares against
 * whatever is currently mounted.
 *
 * Returns `{ isDirty, confirmDismiss }`. `confirmDismiss()` returns true when
 * the modal may close — either nothing changed, or the user accepted the loss.
 */
export function createModalDraftGuard({ controls = () => [], confirm = null,
    message = "Discard your unsaved changes?" } = {}) {
    const initial = readDraftSnapshot(controls());
    const ask = confirm || ((text) => {
        try {
            return globalThis.confirm ? globalThis.confirm(text) : true;
        } catch (_error) {
            // A context without `confirm` must not trap the user in the modal.
            return true;
        }
    });
    const isDirty = () => readDraftSnapshot(controls()) !== initial;
    return {
        isDirty,
        confirmDismiss: () => !isDirty() || !!ask(message),
    };
}
