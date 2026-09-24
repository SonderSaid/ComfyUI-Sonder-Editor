// Checkbox control for a format-declared multi-choice Reference field — today
// the Summary task types — shared by the identity defaults editor, the Attach
// dialog and the chip editor.
//
// What it exists to get right is SPARSENESS. Automatic checks (inherited from a
// lower tier, or derived by the compiler from staged roles) are displayed but
// never written: opening a panel and saving must not turn an automatic choice
// into an authored one, because a stored list stops following the roles or the
// default it was copied from. Only Customize and Reset change what is stored,
// and an untouched control hands back exactly the value it was given — stored
// empty lists and unsupported saved values included.
//
// Module contract: the host supplies the local value, a reader for the tier
// below, and the compiler's last preview; this module owns its DOM and listeners
// and holds no global state, so there is nothing to clean up.

import { EDITOR_COLORS as COLORS, setButtonDisabled,
    setButtonVariant } from "./editor_theme.js";
import { declaredFieldChoices } from "./prompt_profile_declarations.js";
import { promptCandidateVisuallyStale } from "./prompt_context_diagnostics.js";

export const TASK_TYPE_AUTOMATIC_LABEL = "Automatic from staged roles";
export const TASK_TYPE_PREVIEW_LABEL = "Current-scene preview";

const normalizedList = (value) => (Array.isArray(value) ? value : [])
    .map((entry) => String(entry ?? "").trim()).filter(Boolean);

/**
 * The compiler's last H3 task-type preview, captured once when a modal opens.
 *
 * `h3_task_type_preview` is response-only and exists only for an H3 Full
 * Reference compile, so an absent key, a failed compile or a different format
 * all read as unavailable — the surface then says so instead of guessing which
 * roles are staged.
 */
export function captureTaskTypePreview(candidate) {
    const preview = candidate?.h3_task_type_preview;
    const window = candidate?.window || {};
    const start = Number(window.start_frame);
    const end = Number(window.end_frame);
    const windowLabel = Number.isFinite(start) && Number.isFinite(end) && end > start
        ? `frames ${start}–${end}` : "";
    // The panel's own stale rule, so these lines dim exactly when it does.
    const stale = promptCandidateVisuallyStale(candidate);
    if (!preview || typeof preview !== "object" || !Array.isArray(preview.role_derived)) {
        return { available: false, roleDerived: [], sceneEffective: [],
            sceneSource: "", windowLabel, stale };
    }
    return {
        available: true,
        roleDerived: normalizedList(preview.role_derived),
        sceneEffective: normalizedList(preview.scene_effective),
        sceneSource: String(preview.scene_source || ""),
        windowLabel,
        stale,
    };
}

/**
 * What the control displays, without any DOM.
 *
 * - `customized`: a local list is stored and says something. Under a
 *   roles-derived field an empty local list does NOT say "none" — the compiler
 *   reads it as "no explicit choice" and derives from staged roles, so it is
 *   shown as automatic and kept as stored.
 * - `following`: nothing local; the tier below states a list, or the field
 *   has no automatic source to fall back on.
 * - `automatic`: nothing effective is stated, so staged roles decide.
 */
export function resolveTaskTypeDisplay({ rolesDefault = false, local = null,
    inherited = null, preview = null, automaticLabel = TASK_TYPE_AUTOMATIC_LABEL,
    localLabel = "this attachment" } = {}) {
    const localPresent = Boolean(local?.present);
    const localValue = normalizedList(local?.value);
    const inheritedValue = normalizedList(inherited?.value);
    const inheritedLabel = String(inherited?.label || "the Prompt Format default");
    const automaticDisplay = (stored) => {
        const available = Boolean(preview?.available);
        const window = String(preview?.windowLabel || "");
        const details = [];
        if (stored) {
            details.push(`${localLabel.replace(/^./, (c) => c.toUpperCase())} stores an empty choice, so it ignores inherited task types.`);
        }
        details.push(available
            ? `Roles staged in ${window || "the last compiled window"}${
                preview?.stale ? " (last compile is out of date)" : ""}.`
            : "Role suggestion unavailable — no compiled preview of the staged roles yet.");
        return {
            mode: "automatic", stored,
            checks: available ? [...preview.roleDerived] : [],
            label: available ? automaticLabel : `${automaticLabel} · unavailable`,
            detail: details.join(" "),
        };
    };
    if (localPresent && (localValue.length || !rolesDefault)) {
        return { mode: "customized", stored: true, checks: localValue,
            label: "Customized", detail: "" };
    }
    if (localPresent) return automaticDisplay(true);
    if (inheritedValue.length || !rolesDefault) {
        return { mode: "following", stored: false, checks: inheritedValue,
            label: `Following ${inheritedLabel}`,
            detail: inheritedValue.length ? "" : "No task types." };
    }
    return automaticDisplay(false);
}

function actionButton(label, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.title = title;
    setButtonVariant(button, "secondary",
        { padding: "3px 7px", fontSize: "9px", lineHeight: "1.3" });
    return button;
}

/**
 * Build the checkbox control.
 *
 * `local` is `{present, value}` for the tier this surface writes. `inherited()`
 * returns `{value, label}` for the tier below and is re-read on `refresh()`,
 * because the chip editor re-resolves it when the source selection changes.
 * `scenePreview` (Attach and chip only) shows the last compiled scene-wide
 * selection; it is captured by the host and never follows unsaved edits.
 */
export function createTaskTypeChoiceControl({
    declaration = {}, local = null, inherited = () => null, preview = null,
    automaticLabel = TASK_TYPE_AUTOMATIC_LABEL, localLabel = "this attachment",
    showScenePreview = false, label = "Summary task types", onChange = null,
} = {}) {
    const rolesDefault = String(declaration?.default_source || "") === "roles";
    const choices = declaredFieldChoices(declaration);
    const declared = new Set(choices.map((choice) => choice.value));
    // The compiler matches a saved value trimmed and case-folded
    // (`_minimax_task_types`), so a saved "Video Editing" IS the declared
    // choice. Matching exactly here would call it unsupported and refuse a
    // selection the compiler accepts. Only display and validation fold; an
    // untouched control still hands back the raw stored value.
    const canonical = (value) => String(value ?? "").trim().toLowerCase();
    const declaredByCanonical = new Map(choices.map((choice) =>
        [canonical(choice.value), choice.value]));
    const asDeclared = (value) => declaredByCanonical.get(canonical(value))
        ?? String(value ?? "");
    const isSupported = (value) => declaredByCanonical.has(canonical(value));
    const original = {
        present: Boolean(local?.present),
        value: Array.isArray(local?.value) ? structuredClone(local.value) : [],
    };
    // `touched` is the whole sparseness contract: until the author presses
    // Customize or Reset, `value()` returns `original` verbatim.
    let touched = false;
    let localState = { present: original.present, value: [...original.value] };
    let display = null;

    const root = document.createElement("div");
    root.dataset.sonderTaskTypeChoices = "1";
    root.setAttribute("role", "group");
    root.setAttribute("aria-label", label);
    root.style.cssText = `box-sizing:border-box;width:100%;display:flex;flex-direction:column;gap:4px;padding:5px 7px;border:1px solid ${COLORS.border};border-radius:5px;background:${COLORS.panel};`;
    const state = document.createElement("span");
    state.dataset.sonderTaskTypeMode = "";
    state.style.cssText = "font:9px/1.3 system-ui;";
    const detail = document.createElement("span");
    detail.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.textDim};`;
    const list = document.createElement("div");
    list.style.cssText = "display:flex;flex-direction:column;gap:3px;";
    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;";
    const customize = actionButton("Customize",
        "Start from the checks shown and choose task types for " + localLabel + ".");
    const reset = actionButton("Reset to inherited",
        "Remove the choice stored on " + localLabel + ".");
    const resetTarget = document.createElement("span");
    resetTarget.style.cssText = `font:9px/1.3 system-ui;color:${COLORS.textDim};`;
    actions.append(customize, reset, resetTarget);
    const scene = document.createElement("span");
    scene.dataset.sonderTaskTypeScenePreview = "1";
    scene.style.cssText = `font:9px/1.35 system-ui;color:${COLORS.textSecondary};`;
    const error = document.createElement("span");
    error.dataset.sonderTaskTypeError = "1";
    error.style.cssText = `display:none;font:9px/1.35 system-ui;color:${COLORS.dangerText};`;
    root.append(state, detail, list, actions, scene, error);

    // Rows are built once per value and then only restyled, so Customize and
    // Reset never replace the checkbox the keyboard is sitting on.
    const boxes = new Map();
    const rowsByValue = new Map();
    const ensureRow = (value, text, description, unsupported) => {
        if (rowsByValue.has(value)) return;
        const row = document.createElement("label");
        row.dataset.sonderTaskTypeUnsupported = unsupported ? "1" : "";
        row.style.cssText = `display:flex;align-items:center;gap:6px;font:11px system-ui;color:${
            unsupported ? COLORS.dangerText : COLORS.text};`;
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = value;
        if (description) row.title = description;
        const name = document.createElement("span");
        name.textContent = text;
        row.append(box, name);
        box.addEventListener("change", () => {
            const next = [...boxes].filter(([, entry]) => entry.checked)
                .map(([key]) => key);
            // Declared order first, then unsupported values in the order they
            // were saved: the compiler prints canonical order anyway.
            localState = { present: true, value: [
                ...choices.map((choice) => choice.value)
                    .filter((key) => next.includes(key)),
                ...next.filter((key) => !declared.has(key)),
            ] };
            touched = true;
            error.style.display = "none";
            onChange?.();
        });
        list.appendChild(row);
        boxes.set(value, box);
        rowsByValue.set(value, row);
    };
    for (const choice of choices) {
        ensureRow(choice.value, choice.label, String(choice.description || ""), false);
    }
    const renderBoxes = (checks, editable) => {
        const checked = new Set(checks.map(asDeclared));
        // A saved value the format no longer declares stays visible and checked
        // until the author unchecks it; coercing it away would rewrite authored
        // intent the compiler still reports. Once Reset has removed the local
        // choice, the loaded value is gone and so is its row.
        const loaded = !touched || localState.present ? original.value : [];
        const unsupported = new Set([...loaded, ...checks, ...localState.value]
            .map(String).filter((value) => value && !isSupported(value)));
        for (const value of unsupported) {
            ensureRow(value, `Unsupported saved value: ${value}`, "", true);
        }
        for (const [value, box] of boxes) {
            const row = rowsByValue.get(value);
            box.checked = checked.has(value);
            box.disabled = !editable;
            row.style.display = declared.has(value) || unsupported.has(value)
                ? "flex" : "none";
            row.style.cursor = editable ? "pointer" : "default";
        }
    };
    const inheritedDisplay = () => resolveTaskTypeDisplay({
        rolesDefault, local: null, inherited: inherited?.() || null, preview,
        automaticLabel, localLabel,
    });
    const render = () => {
        display = resolveTaskTypeDisplay({
            rolesDefault, local: localState, inherited: inherited?.() || null,
            preview, automaticLabel, localLabel,
        });
        const customized = display.mode === "customized";
        state.textContent = display.label;
        state.dataset.sonderTaskTypeMode = display.mode;
        // The field-status ramp: an authored value is `text` at 600, anything
        // followed is `textSecondary` at 400. Authority is never a hue.
        state.style.color = customized ? COLORS.text : COLORS.textSecondary;
        state.style.fontWeight = customized ? "600" : "400";
        detail.textContent = display.detail;
        detail.style.display = display.detail ? "block" : "none";
        renderBoxes(display.checks, customized);
        customize.style.display = customized ? "none" : "";
        setButtonDisabled(reset, !localState.present);
        resetTarget.textContent = localState.present
            ? `Returns to: ${inheritedDisplay().label}` : "";
        if (showScenePreview) {
            const values = preview?.available ? preview.sceneEffective : [];
            scene.textContent = !preview?.available
                ? "Scene-wide task types: unavailable until the scene compiles."
                : `Scene-wide task types (last compile${
                    preview.windowLabel ? `, ${preview.windowLabel}` : ""}${
                    preview.stale ? ", out of date" : ""}): ${
                    values.length ? values.join(" + ") : "none"}${
                    { explicit: " · combined from customized chips",
                        roles: " · automatic from staged roles",
                        none: " · no Summary contributes in this window",
                    }[preview.sceneSource] || ""}`;
            scene.style.display = "block";
        } else {
            scene.style.display = "none";
        }
    };
    customize.addEventListener("click", () => {
        // Start from exactly what is displayed, so Customize alone changes
        // authority without changing a single check.
        localState = { present: true,
            value: [...new Set((display?.checks || []).map(asDeclared))] };
        touched = true;
        render();
        onChange?.();
        [...boxes.values()].find((box) => !box.disabled)?.focus?.();
    });
    reset.addEventListener("click", () => {
        localState = { present: false, value: [] };
        touched = true;
        error.style.display = "none";
        render();
        // Focus first: the host may hide this row in `onChange` (a collapsed
        // fieldset shows only overridden rows) and then moves focus itself.
        customize.focus?.();
        onChange?.({ reset: true });
    });
    render();

    const api = {
        element: root,
        rolesDefault,
        mode: () => display.mode,
        isTouched: () => touched,
        hasLocal: () => localState.present,
        /** The local value as it would be stored; the original when untouched. */
        value: () => (touched ? [...localState.value] : structuredClone(original.value)),
        /** Displayed checks, for an effective-value projection. */
        displayedValues: () => [...new Set(display.checks.map(asDeclared))],
        /** Label of the current mode, for a second readout of the same state. */
        modeLabel: () => display.label,
        /** One line for a collapsed fieldset. */
        compactSummary: () => `${label}: ${display.checks.length
            ? display.checks.join(" + ") : "none"} · ${display.label}`,
        /**
         * Refuse only what the author just made. Under a roles-derived field an
         * empty customized list would silently mean "automatic", and a list of
         * nothing but unsupported values emits nothing, so a Customize must
         * keep one declared choice. An untouched stored value is never refused.
         */
        validate: () => {
            if (!touched || !rolesDefault || !localState.present) return "";
            if (localState.value.some(isSupported)) return "";
            return "Choose at least one supported task type, or Reset to follow the automatic choice.";
        },
        showError: (message) => {
            error.textContent = message;
            error.style.display = message ? "block" : "none";
            if (message) {
                ([...boxes.values()].find((box) => !box.disabled) || customize)
                    .focus?.();
            }
        },
        refresh: () => render(),
        focus: () => ([...boxes.values()].find((box) => !box.disabled)
            || customize).focus?.(),
    };
    // Modal dirty detection reads this. Whether a local choice exists is part
    // of the signature, so Customize with identical checks still counts as a
    // change, while Customize-then-Reset returns to the opening state.
    root.draftSignature = () =>
        `${localState.present ? "local" : "inherit"}:${localState.value.join("\u0000")}`;
    root.focus = api.focus;
    return api;
}
