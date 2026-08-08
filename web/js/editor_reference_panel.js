// Reference lane setup panel — centered overlay for the ☰ manage icon on a
// Reference lane header. It is the only surface that exposes what a Reference
// lane actually holds: the recipe values that decide render geometry, and the
// staged items with the ordered Library members they carry.
//
// Module-host contract (fullscreen seam pattern): the host owns state,
// networking, and durable writes; this module owns its DOM/listeners and
// returns a cleanup handle. Host surface used:
//   activeScene, activeSceneId, projectDir, totalFrames, playhead, _trackLayout,
//   _references, _referenceRecipePresets, _customReferenceRecipes,
//   _referenceRecipeFieldSchema, _referenceMemberForRef(ref),
//   _referenceLaneAdvisories(entry, definition), _defaultReferenceLaneRecipe(),
//   _findAssetById(id), _referenceAssetPreviewUrl(asset),
//   _isLaneLocked(type, laneIndex), _saveLaneConfig(entries),
//   _runSceneMutation(ops, opts), _mutateReferences(ops),
//   _fetchReferences(opts), _fetchScenes(opts), _buildTrackLayout(),
//   _renderTimeline(), _pushUndo(label), _discardLastUndo(label)
//
// Two invariants this module must not break:
//   1. A built-in recipe is never edited in place. It renders read-only and
//      forks through "Edit as custom", so a lane claiming a named template
//      always matches what the real node expects.
//   2. Every reference-item write carries exact prior values in `expected` and
//      runs non-coalesced — the backend requires the snapshot and treats a
//      mismatch as a terminal conflict, never a replay signal.

import {
    EDITOR_COLORS as COLORS,
    FONT,
    chromeButtonCss,
    chromeInputCss,
    chromeOverlayPanelCss,
    chromeScrollbarCss,
    chromeSelectCss,
} from "./editor_theme.js";
import {
    register as registerKeyboardConsumer,
    PRIORITY as KEY_PRIORITY,
} from "./keyboard_ownership.js";
import { TRACK_TYPE } from "./editor_timeline_constants.js";
import { moveMember } from "./reference_library_model.js";
import {
    REFERENCE_VERDICT,
    REFERENCE_VERDICT_LABEL,
    deriveReferencePrompt,
    resolveReferenceVerdicts,
} from "./reference_resolution.js";
import { notifySuccess, notifyWarning } from "./editor_notifications.js";

const DETACHED_LABEL = "Detached / Custom";
const NEW_ITEM = "__new_reference_item__";
// Value field -> the `*_source` field that decides whether it is pegged.
const PEG_SOURCE_FIELD = {
    size_multiple: "size_multiple_source",
    frame_step: "frame_grid_source",
    frame_offset: "frame_grid_source",
    loop_frames: "loop_frames_source",
};
// Why each verdict happened, and what to do about it — the two failure modes
// have different remedies, which is why they are separate states at all.
const VERDICT_EXPLANATION = {
    winner: "Most-specific-wins resolves this item for the current window; it is the one the model receives.",
    superseded: "Another item on this lane covers this window more tightly, so that one is sent instead. Restage or rescope to change which wins.",
    below_threshold: "The window covers too little of this item's own span for the Reference Threshold, so nothing is sent. Lower the threshold in Settings, or widen the item.",
    outside: "This item does not overlap the current generation window.",
    excluded: "Muted, or on a hidden lane, so it never participates.",
};
const GROUP_ORDER = ["Assembly", "Geometry", "Frame grid", "Members", "Bridge outputs", "Prompt", "Advisories"];

/**
 * Which schema fields apply to a recipe as currently authored.
 *
 * `applies_to` is the set of assembly modes a field means anything for, and
 * `requires` names a sibling gate — matched against `requires_value` when that
 * is set, and against truthiness when it is empty. Showing a field outside
 * those conditions would present a control that changes nothing.
 */
export function visibleRecipeFields(schema = [], hard = {}, soft = {}) {
    const assembly = String(hard?.assembly || "batch");
    return (Array.isArray(schema) ? schema : []).filter((field) => {
        const applies = Array.isArray(field?.applies_to) ? field.applies_to : [];
        if (applies.length && !applies.includes(assembly)) return false;
        if (field?.requires) {
            const source = field.section === "soft" ? soft : hard;
            const gate = source?.[field.requires];
            const expected = field.requires_value || "";
            const sibling = (schema || []).find((entry) => entry.key === field.requires);
            const effective = gate === undefined ? sibling?.default : gate;
            if (expected ? effective !== expected : !effective) return false;
        }
        return true;
    });
}

/** Group visible fields in declared order, dropping groups with no members. */
export function groupRecipeFields(fields = []) {
    const groups = new Map();
    for (const field of fields) {
        const name = field?.group || "Other";
        if (!groups.has(name)) groups.set(name, []);
        groups.get(name).push(field);
    }
    const ordered = [...GROUP_ORDER.filter((name) => groups.has(name)), ...[...groups.keys()].filter((name) => !GROUP_ORDER.includes(name))];
    return ordered.map((name) => ({ group: name, fields: groups.get(name) }));
}

const el = (tag, text = "", css = "") => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (css) node.style.cssText = css;
    return node;
};

const button = (label, title, variant = "secondary") => {
    const node = el("button", label, chromeButtonCss({ variant, padding: "4px 9px", fontSize: "11px" }));
    if (title) node.title = title;
    return node;
};

const sectionTitle = (text) => el("div", text, `
    font-size:10px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase;
    color:${COLORS.textMuted}; margin:2px 0 2px;
`);

export function mountReferenceLanePanel(host, { laneIndex = 0 } = {}) {
    const backdrop = el("div", "", `
        position: fixed; inset: 0; z-index: 10000;
        background: rgba(7,10,14,0.70);
        display: flex; align-items: center; justify-content: center;
    `);
    const panel = el("div", "", chromeOverlayPanelCss({
        width: "min(920px, 94vw)", maxWidth: "920px", maxHeight: "86vh", padding: "0",
    }) + `display:flex; flex-direction:column; overflow:hidden; font-family:${FONT.sans};`);
    backdrop.appendChild(panel);

    const state = {
        laneIndex: Math.max(0, parseInt(laneIndex, 10) || 0),
        pickerItemId: "",
        pickerQuery: "",
        busy: false,
    };
    let mounted = true;

    const close = () => {
        if (!mounted) return;
        mounted = false;
        unregisterKeyboard();
        backdrop.remove();
        if (host._referencePanelHandle === handle) host._referencePanelHandle = null;
    };

    const unregisterKeyboard = registerKeyboardConsumer({
        id: `sonder-reference-panel-${Date.now().toString(36)}`,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (event.key !== "Escape") return false;
            if (state.pickerItemId) {
                // First Esc closes an open member picker; a second closes the panel.
                state.pickerItemId = "";
                state.pickerQuery = "";
                render();
                return true;
            }
            close();
            return true;
        },
    });

    backdrop.addEventListener("mousedown", (event) => {
        if (event.target === backdrop) close();
    });

    // ── Lane + recipe accessors ────────────────────────────────────────────
    const laneEntries = () => (host._trackLayout || []).filter((entry) => entry.type === TRACK_TYPE.REFERENCE);
    const currentEntry = () => laneEntries().find((entry) => (entry.laneIndex || 0) === state.laneIndex) || null;
    const laneRecipe = () => {
        const stored = host.activeScene?.reference_lane_recipes?.[state.laneIndex];
        return stored ? JSON.parse(JSON.stringify(stored)) : host._defaultReferenceLaneRecipe();
    };
    const laneItems = () => (host.activeScene?.reference_items || [])
        .filter((item) => (item.lane_index || 0) === state.laneIndex)
        .slice()
        .sort((left, right) => (left.start_frame || 0) - (right.start_frame || 0));
    const definitions = () => [...(host._referenceRecipePresets || []), ...(host._customReferenceRecipes || [])];
    const definitionFor = (recipeId) => definitions().find((entry) => entry.id === recipeId) || null;
    const isBuiltIn = (recipeId) => !!definitionFor(recipeId)?.builtIn;
    const laneLocked = () => host._isLaneLocked?.(TRACK_TYPE.REFERENCE, state.laneIndex) || false;

    // ── Durable writes ─────────────────────────────────────────────────────
    const writeRecipe = async (recipe) => {
        const entry = currentEntry();
        if (!entry || state.busy) return;
        state.busy = true;
        entry.referenceRecipe = recipe;
        try {
            await host._saveLaneConfig([entry]);
        } finally {
            state.busy = false;
        }
        host._buildTrackLayout?.();
        host._renderTimeline?.();
        render();
    };

    const writeItem = async (item, fields, label) => {
        if (state.busy) return;
        const expected = {};
        for (const key of Object.keys(fields)) expected[key] = item[key] ?? (key === "end_frame" ? -1 : null);
        state.busy = true;
        host._pushUndo?.(label);
        try {
            const result = await host._runSceneMutation([{
                type: "update_reference_item",
                reference_item_id: item.reference_item_id,
                expected,
                fields,
            }], {
                key: `scene:${host.activeSceneId}:reference-panel:${item.reference_item_id}:${Date.now()}`,
                label,
                coalesce: false,
                refreshScenes: false,
            });
            host._reconcileActiveSceneFromMutation?.(result, { reason: "reference_panel", ignoreTimelineGate: true });
        } catch (error) {
            host._discardLastUndo?.(label);
            notifyWarning(error?.message || "Reference edit was refused.", { source: "reference-panel-refused" });
            await host._fetchScenes?.({ ignoreMutationGate: true, reason: "reference_panel_error" });
        } finally {
            state.busy = false;
        }
        host._buildTrackLayout?.();
        host._renderTimeline?.();
        render();
    };

    const runItemOperation = async (operation, label) => {
        if (state.busy) return;
        state.busy = true;
        host._pushUndo?.(label);
        try {
            const result = await host._runSceneMutation([operation], {
                key: `scene:${host.activeSceneId}:reference-panel-op:${Date.now()}`,
                label,
                coalesce: false,
                refreshScenes: false,
            });
            host._reconcileActiveSceneFromMutation?.(result, { reason: "reference_panel", ignoreTimelineGate: true });
        } catch (error) {
            host._discardLastUndo?.(label);
            notifyWarning(error?.message || "Reference edit was refused.", { source: "reference-panel-refused" });
            await host._fetchScenes?.({ ignoreMutationGate: true, reason: "reference_panel_error" });
        } finally {
            state.busy = false;
        }
        host._buildTrackLayout?.();
        host._renderTimeline?.();
        render();
    };

    // ── Recipe form ────────────────────────────────────────────────────────
    const readField = (recipe, field) => {
        const source = field.section === "soft" ? (recipe.recipe?.soft || {}) : (recipe.recipe?.hard || {});
        return Object.prototype.hasOwnProperty.call(source, field.key) ? source[field.key] : field.default;
    };

    const applyField = (field, value) => {
        const recipe = laneRecipe();
        const materialized = recipe.recipe && typeof recipe.recipe === "object" ? recipe.recipe : {};
        const section = field.section === "soft" ? "soft" : "hard";
        const next = { ...(materialized[section] || {}), [field.key]: value };
        // Typing a pegged value by hand un-pegs it: the number you just entered
        // is the intent, not the source it used to follow.
        const peg = PEG_SOURCE_FIELD[field.key];
        if (peg) next[peg] = "custom";
        materialized[section] = next;
        recipe.recipe = materialized;
        void writeRecipe(recipe);
    };

    /**
     * A pegged value is resolved at render time from a live source, so the
     * stored number is only a fallback. This reports what the peg resolves to
     * right now — it must never nag to "update" the stored value, because
     * following the source is the whole point of pegging.
     */
    const pegNote = (recipe, field) => {
        const peg = peggedFieldState(recipe, field);
        if (!peg) return null;
        const note = el("div", peg.resolved
            ? `from ${peg.label}`
            : `follows ${peg.label} — not set yet, so ${peg.fallback} is used`,
            `font-size:10px;color:${COLORS.textMuted};white-space:nowrap;`);
        note.title = `Pegged: resolved when the render runs, so it follows ${peg.label} instead of staying at a number typed once.`;
        return note;
    };

    /**
     * Peg state for a field, or null when it is authored directly.
     * `resolved` is 0 when the source has nothing to give, in which case the
     * authored `fallback` is what the assembler uses.
     */
    const peggedFieldState = (recipe, field) => {
        const source = PEG_SOURCE_FIELD[field.key];
        if (!source) return null;
        const hard = recipe.recipe?.hard || {};
        const mode = String(hard[source] || "custom");
        if (mode === "custom") return null;
        return {
            mode,
            resolved: resolvedPegValue(field.key, mode),
            fallback: hard[field.key] ?? field.default,
            label: mode === "window"
                ? "the render window"
                : (host._referenceTemplateName?.() || "the scene template"),
        };
    };

    /** What a peg resolves to right now, for display only. */
    const resolvedPegValue = (key, mode) => {
        if (mode === "template" && key === "size_multiple") {
            return Math.max(1, parseInt(host._referenceTemplateDimensionStep?.(), 10) || 0) || 0;
        }
        if (mode === "template" && (key === "frame_step" || key === "frame_offset")) {
            const constraint = host._referenceTemplateFrameConstraint?.() || null;
            const value = key === "frame_step" ? constraint?.step : constraint?.offset;
            return Number.isFinite(Number(value)) ? Number(value) : 0;
        }
        if (mode === "window" && key === "loop_frames") {
            const scene = host.activeScene;
            const hasSelection = Number.isFinite(host.selectionStart) && Number.isFinite(host.selectionEnd)
                && host.selectionEnd > host.selectionStart;
            return hasSelection
                ? host.selectionEnd - host.selectionStart
                : Math.max(0, parseInt(scene?.duration_frames, 10) || 0);
        }
        return 0;
    };

    /** Preset tag names, for Tab completion. */
    const tagCatalog = () => (host._referenceTagPresets || [])
        .map((preset) => String(preset?.tag || preset?.id || preset?.name || ""))
        .filter(Boolean);

    const tagChipEditor = (field, tags, locked) => {
        const wrap = el("div", "", "display:flex;flex-wrap:wrap;align-items:center;gap:4px;flex:1;");
        for (const tag of tags) {
            const chip = el("span", "", `
                display:inline-flex;align-items:center;gap:4px;font-size:10px;padding:2px 4px 2px 6px;
                border:1px solid ${COLORS.border};border-radius:10px;color:${COLORS.text};
            `);
            chip.appendChild(el("span", tag));
            if (!locked) {
                const drop = el("button", "×", chromeButtonCss({ variant: "tertiary", padding: "0 3px", fontSize: "10px", radius: "8px" }));
                drop.title = `Remove ${tag}`;
                drop.addEventListener("click", () => applyField(field, tags.filter((entry) => entry !== tag)));
                chip.appendChild(drop);
            }
            wrap.appendChild(chip);
        }
        if (locked) return wrap;

        const input = el("input", "", chromeInputCss({ padding: "3px 6px", fontSize: "11px" }) + "width:150px;");
        input.placeholder = "add tag — Tab completes";
        const completionFor = (typed) => {
            const needle = typed.trim().toLowerCase();
            if (!needle) return "";
            return tagCatalog().find((tag) => tag.toLowerCase().startsWith(needle) && !tags.includes(tag)) || "";
        };
        const commit = (value) => {
            const tag = String(value || "").trim();
            if (!tag || tags.includes(tag)) return;
            applyField(field, [...tags, tag]);
        };
        input.addEventListener("keydown", (event) => {
            event.stopPropagation();
            if (event.key === "Tab") {
                const completion = completionFor(input.value);
                // Nothing to complete: Tab stays a focus key. Trapping it here
                // would strand keyboard users inside the field.
                if (!completion) return;
                event.preventDefault();
                input.value = completion;
                return;
            }
            if (event.key === "Enter") {
                event.preventDefault();
                commit(input.value);
            }
        });
        input.addEventListener("change", () => commit(input.value));
        wrap.appendChild(input);
        const hint = el("span", "", `font-size:10px;color:${COLORS.textMuted};`);
        input.addEventListener("input", () => {
            const completion = completionFor(input.value);
            hint.textContent = completion && completion !== input.value ? `Tab → ${completion}` : "";
        });
        wrap.appendChild(hint);
        return wrap;
    };

    const fieldControl = (recipe, field, locked) => {
        const value = readField(recipe, field);
        const inputCss = chromeInputCss({ padding: "3px 6px", fontSize: "11px" });
        if (field.type === "bool") {
            const box = el("input");
            box.type = "checkbox";
            box.checked = !!value;
            box.disabled = locked;
            box.addEventListener("change", () => applyField(field, box.checked));
            return box;
        }
        if (field.type === "enum") {
            const wrap = el("div", "", "display:flex;flex-direction:column;gap:3px;flex:1;min-width:0;");
            const select = el("select", "", chromeSelectCss({ padding: "3px 6px", fontSize: "11px" }));
            for (const option of field.values || []) {
                const node = el("option", option);
                node.value = option;
                node.title = field.value_help?.[option] || "";
                select.appendChild(node);
            }
            select.value = String(value ?? field.default ?? "");
            select.disabled = locked;
            // The chosen option's meaning stays visible: which mode you picked is
            // the decision, and the field label alone never explains it.
            const chosenHelp = el("div", "", `font-size:10px;color:${COLORS.textMuted};line-height:1.3;`);
            const syncHelp = () => { chosenHelp.textContent = field.value_help?.[select.value] || ""; };
            syncHelp();
            select.addEventListener("change", () => {
                syncHelp();
                applyField(field, select.value);
            });
            wrap.append(select, chosenHelp);
            return wrap;
        }
        if (field.type === "output_list") {
            const wrap = el("div", "", "display:flex;flex-wrap:wrap;gap:4px 10px;");
            const declared = Array.isArray(value) ? value : null;
            for (const name of field.values || []) {
                const label = el("label", "", `display:flex;align-items:center;gap:4px;font-size:11px;color:${COLORS.text};`);
                const box = el("input");
                box.type = "checkbox";
                // No declaration at all means "every output live" — the
                // detached escape hatch — so an unauthored recipe shows all on.
                box.checked = declared ? declared.includes(name) : true;
                box.disabled = locked;
                // What this socket carries, and what it emits when unchecked.
                label.title = field.value_help?.[name] || "";
                box.addEventListener("change", () => {
                    const base = declared || [...(field.values || [])];
                    const next = box.checked
                        ? [...new Set([...base, name])]
                        : base.filter((entry) => entry !== name);
                    applyField(field, (field.values || []).filter((entry) => next.includes(entry)));
                });
                label.append(box, el("span", name === "slots" ? "r01..r16" : name));
                wrap.appendChild(label);
            }
            return wrap;
        }
        const input = el("input", "", inputCss + "width:170px;");
        input.disabled = locked;
        if (field.type === "int" || field.type === "number") {
            input.type = "number";
            // A pegged field shows what the render will USE, not the number it
            // replaced: displaying the authored fallback while the peg supplies
            // something else is what made this read as broken. The fallback stays
            // in the recipe for when the source is unset.
            const peg = peggedFieldState(recipe, field);
            input.value = String((peg?.resolved ?? value ?? field.default ?? 0));
            if (field.min !== undefined) input.min = String(field.min);
            if (field.max !== undefined) input.max = String(field.max);
            if (peg) {
                input.readOnly = true;
                input.style.opacity = "0.6";
                input.style.cursor = "not-allowed";
                input.title = `Pegged to ${peg.label}. Change "${field.label} taken from" to edit this directly.`;
                return input;
            }
            input.addEventListener("change", () => {
                const parsed = field.type === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
                applyField(field, Number.isFinite(parsed) ? parsed : (field.default ?? 0));
            });
            return input;
        }
        if (field.type === "int_list" || field.type === "int_pair") {
            input.value = (Array.isArray(value) ? value : []).join(", ");
            input.placeholder = field.type === "int_pair" ? "width, height" : "17, 25, 33";
            input.addEventListener("change", () => {
                const parsed = input.value.split(",").map((part) => parseInt(part.trim(), 10)).filter(Number.isFinite);
                applyField(field, field.type === "int_pair" && parsed.length && parsed.length !== 2 ? parsed.slice(0, 2) : parsed);
            });
            return input;
        }
        if (field.type === "string_list") {
            return field.key === "suggested_tags"
                ? tagChipEditor(field, Array.isArray(value) ? value : [], locked)
                : (() => {
                    input.value = (Array.isArray(value) ? value : []).join(", ");
                    input.placeholder = "tag, tag";
                    input.addEventListener("change", () => {
                        applyField(field, input.value.split(",").map((part) => part.trim()).filter(Boolean));
                    });
                    return input;
                })();
        }
        input.value = String(value ?? "");
        input.addEventListener("change", () => applyField(field, input.value));
        return input;
    };

    const renderRecipe = (body) => {
        const recipe = laneRecipe();
        const definition = definitionFor(recipe.recipe_id);
        const builtIn = isBuiltIn(recipe.recipe_id);
        const locked = builtIn || laneLocked() || state.busy;
        const occupied = laneItems().length > 0;

        body.appendChild(sectionTitle("Recipe"));

        const templateRow = el("div", "", "display:flex;align-items:center;gap:8px;flex-wrap:wrap;");
        templateRow.appendChild(el("span", "Template", `font-size:11px;color:${COLORS.textMuted};min-width:64px;`));
        const select = el("select", "", chromeSelectCss({ padding: "4px 7px", fontSize: "11px" }) + "min-width:240px;");
        const detached = el("option", DETACHED_LABEL);
        detached.value = "";
        select.appendChild(detached);
        for (const entry of definitions()) {
            const option = el("option", `${entry.builtIn ? "" : "★ "}${entry.name}`);
            option.value = entry.id;
            select.appendChild(option);
        }
        select.value = recipe.recipe_id || "";
        select.disabled = laneLocked() || state.busy;
        select.addEventListener("change", () => {
            const chosen = definitionFor(select.value);
            if (!chosen) {
                void writeRecipe({ ...host._defaultReferenceLaneRecipe({ media_kind: recipe.media_kind }) });
                return;
            }
            if (occupied && chosen.media_kind !== recipe.media_kind) {
                notifyWarning("Clear the Reference lane before switching between image and audio recipes.", { source: "reference-panel-refused" });
                render();
                return;
            }
            void writeRecipe({
                media_kind: chosen.media_kind === "audio" ? "audio" : "image",
                recipe_id: chosen.id,
                recipe: { name: chosen.name, hard: { ...(chosen.hard || {}) }, soft: { ...(chosen.soft || {}) } },
            });
        });
        templateRow.appendChild(select);

        const mediaBtn = button(
            `Media: ${recipe.media_kind === "audio" ? "Audio" : "Image"}`,
            occupied ? "Clear the lane before changing media kind" : "Switch this lane between image and audio references",
        );
        mediaBtn.disabled = occupied || laneLocked() || state.busy;
        if (mediaBtn.disabled) mediaBtn.style.opacity = "0.5";
        mediaBtn.addEventListener("click", () => {
            void writeRecipe(host._defaultReferenceLaneRecipe({
                media_kind: recipe.media_kind === "audio" ? "image" : "audio",
            }));
        });
        templateRow.appendChild(mediaBtn);

        if (builtIn) {
            const fork = button("Edit as custom", "Copy these values into a project recipe you can edit", "primary");
            fork.disabled = laneLocked() || state.busy;
            fork.addEventListener("click", () => void forkToCustom(recipe, definition));
            templateRow.appendChild(fork);
        }
        body.appendChild(templateRow);

        const note = el("div", builtIn
            ? "Built-in template — values are shown as the model expects them. Fork it to edit."
            : (recipe.recipe_id ? "Project recipe — edits apply to this lane." : "Detached — every value below is yours to set."),
            `font-size:10px;color:${COLORS.textMuted};margin:2px 0 4px;`);
        body.appendChild(note);

        const schema = host._referenceRecipeFieldSchema || [];
        const visible = visibleRecipeFields(schema, recipe.recipe?.hard || {}, recipe.recipe?.soft || {});
        for (const { group, fields } of groupRecipeFields(visible)) {
            const block = el("div", "", `
                border:1px solid ${COLORS.border}; border-radius:7px; padding:8px 10px;
                display:flex; flex-direction:column; gap:6px; background:${COLORS.panelMuted};
            `);
            block.appendChild(el("div", group, `font-size:11px;font-weight:700;color:${COLORS.text};`));
            for (const field of fields) {
                const row = el("div", "", "display:flex;align-items:center;gap:10px;");
                // Help is on hover, not permanently expanded: a visible line under
                // every one of ~28 controls is what made this form unreadable.
                const label = el("div", field.label, `font-size:11px;color:${COLORS.text};min-width:170px;flex-shrink:0;border-bottom:1px dotted ${COLORS.border};cursor:help;`);
                if (field.help) {
                    label.title = field.help;
                    row.title = field.help;
                }
                // A pegged value is not authored here any more, so its own
                // control is inert and the note says what it resolves to.
                const pegSource = PEG_SOURCE_FIELD[field.key];
                const pegged = pegSource
                    && String((recipe.recipe?.hard || {})[pegSource] || "custom") !== "custom";
                row.append(label, fieldControl(recipe, field, locked || !!pegged));
                const provenance = pegNote(recipe, field);
                if (provenance) row.appendChild(provenance);
                block.appendChild(row);
            }
            body.appendChild(block);
        }

        const actions = el("div", "", "display:flex;gap:6px;flex-wrap:wrap;margin-top:2px;");
        const saveAs = button("Save as custom…", "Create a project recipe from this lane's current values");
        saveAs.disabled = state.busy;
        saveAs.addEventListener("click", () => void saveAsCustom(recipe));
        actions.appendChild(saveAs);
        if (definition && !definition.builtIn) {
            const update = button(`Update “${definition.name}”`, "Push this lane's values back into the project recipe");
            update.disabled = state.busy;
            update.addEventListener("click", () => void updateCustom(recipe, definition));
            const rename = button("Rename", "Rename this project recipe");
            rename.addEventListener("click", () => void renameCustom(definition));
            const remove = button("Delete", "Delete this project recipe", "danger");
            remove.addEventListener("click", () => void deleteCustom(definition));
            actions.append(update, rename, remove);
        }
        body.appendChild(actions);

    };

    const renderAdvisories = (body) => {
        const recipe = laneRecipe();
        const laneAdvisories = host._referenceLaneAdvisories?.(currentEntry() || { laneIndex: state.laneIndex, type: TRACK_TYPE.REFERENCE }, {
            hard: recipe.recipe?.hard || {},
            soft: recipe.recipe?.soft || {},
        }) || [];
        if (!laneAdvisories.length) return;
        body.appendChild(sectionTitle("Advisories"));
        for (const advisory of laneAdvisories) {
            const silent = advisory.voice === "silent-loss";
            body.appendChild(el("div", `${silent ? "Silent loss" : "Suggestion"}: ${advisory.text}`, `
                font-size:11px; line-height:1.35; padding:5px 8px; border-radius:6px;
                color:${silent ? COLORS.warningText : COLORS.textMuted};
                border:1px solid ${silent ? COLORS.warningText : COLORS.border};
            `));
        }
    };

    // ── Custom recipe lifecycle ────────────────────────────────────────────
    const recipePayload = (recipe, name) => ({
        name,
        media_kind: recipe.media_kind === "audio" ? "audio" : "image",
        hard: { ...(recipe.recipe?.hard || {}) },
        soft: { ...(recipe.recipe?.soft || {}) },
    });

    // _mutateReferences resolves the versioned mutation result and the host has
    // already applied the canonical Library payload by then, so the panel only
    // needs to re-render — never a second fetch.
    const createdRecipeId = (result) => String(
        (result?.payload?.results || []).find((row) => row?.type === "create_recipe")?.recipe_id || "",
    );

    const forkToCustom = async (recipe, definition) => {
        const name = `${definition?.name || "Reference recipe"} (custom)`;
        const result = await host._mutateReferences?.([{ type: "create_recipe", fields: recipePayload(recipe, name) }]);
        const recipeId = createdRecipeId(result);
        if (!recipeId) {
            render();
            return;
        }
        await writeRecipe({ ...recipe, recipe_id: recipeId, recipe: { ...(recipe.recipe || {}), name } });
    };

    const saveAsCustom = async (recipe) => {
        const name = window.prompt("Custom recipe name:", recipe.recipe?.name || "Custom Reference Recipe");
        if (!name?.trim()) return;
        const result = await host._mutateReferences?.([{ type: "create_recipe", fields: recipePayload(recipe, name.trim()) }]);
        const recipeId = createdRecipeId(result);
        if (recipeId) {
            await writeRecipe({ ...recipe, recipe_id: recipeId, recipe: { ...(recipe.recipe || {}), name: name.trim() } });
            return;
        }
        render();
    };

    const expectedRecipe = (definition) => ({
        id: definition.id,
        name: definition.name,
        media_kind: definition.media_kind,
        hard: { ...(definition.hard || {}) },
        soft: { ...(definition.soft || {}) },
    });

    const updateCustom = async (recipe, definition) => {
        await host._mutateReferences?.([{
            type: "update_recipe",
            recipe_id: definition.id,
            expected: expectedRecipe(definition),
            fields: recipePayload(recipe, definition.name),
        }]);
        render();
    };

    const renameCustom = async (definition) => {
        const name = window.prompt("Recipe name:", definition.name || "");
        if (!name?.trim() || name.trim() === definition.name) return;
        await host._mutateReferences?.([{
            type: "update_recipe",
            recipe_id: definition.id,
            expected: expectedRecipe(definition),
            fields: { name: name.trim() },
        }]);
        render();
    };

    const deleteCustom = async (definition) => {
        await host._mutateReferences?.([{
            type: "delete_recipe",
            recipe_id: definition.id,
            expected: expectedRecipe(definition),
        }]);
        // The lane keeps its materialized values and falls back to Detached.
        const recipe = laneRecipe();
        if (recipe.recipe_id === definition.id) {
            await writeRecipe({ ...recipe, recipe_id: "" });
            return;
        }
        render();
    };

    // ── Staged items ───────────────────────────────────────────────────────
    const memberLabel = (memberRef) => {
        const resolved = host._referenceMemberForRef?.(memberRef);
        if (!resolved) return { title: "Missing member", detail: memberRef?.member_id || "", asset: null };
        const asset = host._findAssetById?.(resolved.member.asset_id) || null;
        const tags = (resolved.member.tags || []).join(", ");
        return {
            title: resolved.reference.name || "Reference",
            detail: [resolved.member.prompt || asset?.name || "", tags].filter(Boolean).join(" · "),
            asset,
        };
    };

    const renderMemberRow = (item, memberRef, index, total, locked) => {
        const { title, detail, asset } = memberLabel(memberRef);
        const row = el("div", "", `
            display:flex; align-items:center; gap:8px; padding:4px 6px;
            border:1px solid ${COLORS.border}; border-radius:6px;
        `);
        row.appendChild(el("div", String(index + 1), `font-size:10px;color:${COLORS.textMuted};width:14px;text-align:right;`));
        const thumbUrl = asset ? host._referenceAssetPreviewUrl?.(asset) : null;
        const thumb = el("div", "", `
            width:34px; height:26px; border-radius:4px; flex-shrink:0;
            background:${COLORS.bg} ${thumbUrl ? `url(${JSON.stringify(thumbUrl)}) center/cover no-repeat` : ""};
            border:1px solid ${COLORS.border};
        `);
        row.appendChild(thumb);
        const text = el("div", "", "flex:1;min-width:0;");
        text.appendChild(el("div", title, `font-size:11px;color:${COLORS.text};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`));
        if (detail) text.appendChild(el("div", detail, `font-size:10px;color:${COLORS.textMuted};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`));
        row.appendChild(text);

        const reorder = (direction) => {
            // Slot order is what the model receives, so it is authored here.
            // moveMember stamps a dense `order`; the item schema has no such
            // field, so it is dropped before the write.
            const next = moveMember(item.members || [], memberRef.member_id, direction)
                .map(({ order: _order, ...entry }) => entry);
            void writeItem(item, { members: next }, "reorder reference members");
        };
        const up = button("↑", "Move earlier");
        up.disabled = locked || index === 0;
        up.addEventListener("click", () => reorder(-1));
        const down = button("↓", "Move later");
        down.disabled = locked || index === total - 1;
        down.addEventListener("click", () => reorder(1));
        const remove = button("×", "Remove this member from the item", "danger");
        remove.disabled = locked || total <= 1;
        remove.addEventListener("click", () => {
            void writeItem(item, {
                members: (item.members || []).filter((entry) => entry.member_id !== memberRef.member_id),
            }, "remove reference member");
        });
        row.append(up, down, remove);
        return row;
    };

    /**
     * @param item     a staged item, or the NEW_ITEM sentinel for a fresh one
     * @param mediaKind the lane's hard media kind — wrong-kind members are not offered
     */
    const renderMemberPicker = (item, mediaKind) => {
        const creating = item.reference_item_id === NEW_ITEM;
        const wrap = el("div", "", `
            display:flex; flex-direction:column; gap:5px; padding:6px;
            border:1px dashed ${COLORS.border}; border-radius:6px;
        `);
        if (creating) {
            wrap.appendChild(el("div", "Pick the first member for the new item.", `font-size:11px;color:${COLORS.textMuted};`));
        }
        const search = el("input", "", chromeInputCss({ padding: "4px 7px", fontSize: "11px" }));
        search.placeholder = "Search the Library…";
        search.value = state.pickerQuery;
        search.addEventListener("input", () => {
            state.pickerQuery = search.value;
            renderList();
        });
        wrap.appendChild(search);
        const list = el("div", "", `display:flex;flex-direction:column;gap:3px;max-height:190px;overflow:auto;${chromeScrollbarCss()}`);
        wrap.appendChild(list);

        const staged = new Set((item.members || []).map((entry) => entry.member_id));
        const renderList = () => {
            list.textContent = "";
            const query = state.pickerQuery.trim().toLowerCase();
            let offered = 0;
            for (const reference of host._references || []) {
                for (const member of reference.members || []) {
                    if (staged.has(member.member_id)) continue;
                    const asset = host._findAssetById?.(member.asset_id) || null;
                    // A wrong-kind member is never offered: media_kind is a hard
                    // lane property and the backend refuses the write anyway.
                    const voice = (member.tags || []).includes("sonder:voice_identity");
                    const kind = asset?.asset_type === "audio" || (asset?.asset_type === "video" && voice) ? "audio" : "image";
                    if (!asset || kind !== mediaKind) continue;
                    const haystack = `${reference.name} ${member.prompt || ""} ${asset.name || ""} ${(member.tags || []).join(" ")}`.toLowerCase();
                    if (query && !haystack.includes(query)) continue;
                    offered += 1;
                    const row = button(`${reference.name} · ${member.prompt || asset.name || member.member_id}`, "Stage this member");
                    row.style.textAlign = "left";
                    row.addEventListener("click", () => {
                        state.pickerItemId = "";
                        state.pickerQuery = "";
                        const memberRef = { entity_id: reference.reference_id, member_id: member.member_id };
                        if (creating) {
                            void createItem([memberRef]);
                            return;
                        }
                        void writeItem(item, {
                            members: [...(item.members || []), memberRef],
                        }, "add reference member");
                    });
                    list.appendChild(row);
                }
            }
            if (!offered) {
                list.appendChild(el("div", `No unstaged ${mediaKind} members match.`, `font-size:11px;color:${COLORS.textMuted};padding:4px;`));
            }
        };
        renderList();
        return wrap;
    };

    /**
     * What happens to each staged item for the current window, keyed by
     * reference_item_id. Most-specific-wins is the resolver's whole job and is
     * otherwise invisible: two overlapping items look identical in this list.
     *
     * Same vocabulary and same window as the timeline marks, so one concept
     * reads the same in both surfaces — including no verdict at all when there
     * is no selection to resolve against.
     */
    const itemVerdicts = () => {
        const scene = host.activeScene;
        const range = host._selectionContextRange?.();
        const byId = new Map();
        if (!scene || !range) return byId;
        const { verdicts } = resolveReferenceVerdicts({
            referenceItems: scene.reference_items || [],
            laneCount: Math.max(1, parseInt(scene.reference_lane_count, 10) || 1),
            sceneDuration: Math.max(0, parseInt(scene.duration_frames, 10) || 0),
            windowStart: Math.max(0, Math.round(range.contextStart)),
            windowEnd: Math.round(range.contextEnd),
            laneConfigs: scene.reference_lane_configs || [],
            // Without this a chip would claim an item is in window that the
            // threshold actually drops at render time.
            frameThresholdPct: host._referenceFrameThreshold || 0,
        });
        for (const [index, verdict] of verdicts) {
            const item = (scene.reference_items || [])[index];
            if (item?.reference_item_id) byId.set(item.reference_item_id, verdict);
        }
        return byId;
    };

    const renderPromptRow = (item, locked) => {
        const soft = laneRecipe().recipe?.soft || {};
        const members = (item.members || []).map((memberRef) => {
            const resolved = host._referenceMemberForRef?.(memberRef);
            return { name: resolved?.reference?.name || "", prompt: resolved?.member?.prompt || "" };
        });
        const derived = deriveReferencePrompt({ promptOverride: "", members, soft });
        const overridden = !!item.prompt_override;
        const wrap = el("div", "", "display:flex;flex-direction:column;gap:4px;");
        const header = el("div", "", `display:flex;align-items:center;gap:6px;font-size:10px;color:${COLORS.textMuted};`);
        header.appendChild(el("span", overridden ? "Prompt — overridden" : "Prompt — derived from members"));
        wrap.appendChild(header);

        if (!overridden) {
            const box = el("textarea", "", chromeInputCss({ padding: "4px 6px", fontSize: "11px" }) + "width:100%;box-sizing:border-box;resize:vertical;min-height:34px;");
            box.value = derived;
            box.readOnly = true;
            box.title = "This is what reaches the model. Copy it, or take it over as an override.";
            wrap.appendChild(box);
            const copy = button("Copy", "Copy the derived prompt");
            copy.addEventListener("click", async () => {
                try {
                    await navigator.clipboard.writeText(derived);
                    notifySuccess("Derived prompt copied.", { source: "reference-panel-prompt" });
                } catch {
                    box.select();
                }
            });
            // Seeding the box from the derived text is explicit: a pre-filled
            // editable field would freeze the prompt on the first keystroke and
            // silently stop tracking member changes.
            const takeOver = button("Edit as override", "Start from this text and edit it freely");
            takeOver.disabled = locked || !derived;
            takeOver.addEventListener("click", () => {
                void writeItem(item, { prompt_override: derived }, "override reference prompt");
            });
            header.append(copy, takeOver);
            return wrap;
        }

        const box = el("textarea", "", chromeInputCss({ padding: "4px 6px", fontSize: "11px" }) + "width:100%;box-sizing:border-box;resize:vertical;min-height:34px;");
        box.value = item.prompt_override || "";
        box.disabled = locked;
        box.addEventListener("keydown", (event) => event.stopPropagation());
        box.addEventListener("change", () => {
            if (box.value !== (item.prompt_override || "")) {
                void writeItem(item, { prompt_override: box.value }, "edit reference prompt override");
            }
        });
        wrap.appendChild(box);
        const revert = button("Clear", "Go back to the prompt derived from the staged members");
        revert.disabled = locked;
        revert.addEventListener("click", () => void writeItem(item, { prompt_override: "" }, "clear reference prompt override"));
        const derivedNote = el("span", derived ? `derived: ${derived}` : "", `flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        derivedNote.title = derived;
        header.append(derivedNote, revert);
        return wrap;
    };

    const renderItems = (body) => {
        const items = laneItems();
        const recipe = laneRecipe();
        const locked = laneLocked() || state.busy;
        const verdicts = itemVerdicts();

        const header = el("div", "", "display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:6px;");
        header.appendChild(sectionTitle(`Staged items (${items.length})`));
        const add = button("+ Add item", "Stage a new item at the playhead");
        add.disabled = locked;
        add.addEventListener("click", () => {
            state.pickerItemId = NEW_ITEM;
            state.pickerQuery = "";
            render();
        });
        header.appendChild(add);
        body.appendChild(header);

        if (state.pickerItemId === NEW_ITEM) {
            body.appendChild(renderMemberPicker(
                { reference_item_id: NEW_ITEM, members: [] },
                recipe.media_kind === "audio" ? "audio" : "image",
            ));
        }

        if (!items.length) {
            body.appendChild(el("div", "Nothing staged on this lane yet. Drag an entity from the Library onto the lane, or add an item and pick its members here.", `
                font-size:11px;color:${COLORS.textMuted};padding:10px;text-align:center;
                border:1px dashed ${COLORS.border};border-radius:7px;
            `));
            return;
        }

        for (const item of items) {
            const verdict = verdicts.get(item.reference_item_id) || null;
            const effective = verdict === REFERENCE_VERDICT.WINNER;
            const card = el("div", "", `
                border:1px solid ${effective ? COLORS.accent : COLORS.border}; border-radius:7px; padding:8px 10px;
                display:flex; flex-direction:column; gap:6px;
            `);
            const top = el("div", "", "display:flex;align-items:center;gap:6px;flex-wrap:wrap;");
            // Same wording the timeline draws, and nothing at all without a
            // selection — there is no window to resolve against yet.
            if (verdict) {
                const marker = el("span", REFERENCE_VERDICT_LABEL[verdict],
                    `font-size:9px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;padding:2px 5px;border-radius:4px;`
                    + (effective
                        ? `color:${COLORS.bg};background:${COLORS.accent};`
                        : `color:${COLORS.textMuted};border:1px solid ${COLORS.border};`));
                marker.title = VERDICT_EXPLANATION[verdict] || "";
                top.appendChild(marker);
            }
            const startInput = el("input", "", chromeInputCss({ padding: "3px 6px", fontSize: "11px" }) + "width:74px;");
            startInput.type = "number";
            startInput.min = "0";
            startInput.value = String(item.start_frame || 0);
            startInput.disabled = locked;
            startInput.addEventListener("change", () => {
                const parsed = parseInt(startInput.value, 10);
                if (Number.isFinite(parsed)) void writeItem(item, { start_frame: Math.max(0, parsed) }, "move reference item");
            });
            const endInput = el("input", "", chromeInputCss({ padding: "3px 6px", fontSize: "11px" }) + "width:74px;");
            endInput.type = "number";
            endInput.min = "-1";
            endInput.value = String(item.end_frame ?? -1);
            endInput.title = "-1 runs to the end of the scene";
            endInput.disabled = locked;
            endInput.addEventListener("change", () => {
                const parsed = parseInt(endInput.value, 10);
                if (Number.isFinite(parsed)) void writeItem(item, { end_frame: parsed < 0 ? -1 : parsed }, "trim reference item");
            });
            top.append(
                el("span", "Frames", `font-size:11px;color:${COLORS.textMuted};`),
                startInput,
                el("span", "→", `font-size:11px;color:${COLORS.textMuted};`),
                endInput,
            );
            const strengthInput = el("input", "", chromeInputCss({ padding: "3px 6px", fontSize: "11px" }) + "width:64px;");
            strengthInput.type = "number";
            strengthInput.min = "0";
            strengthInput.max = "1";
            strengthInput.step = "0.05";
            strengthInput.value = Number(item.strength ?? 1).toFixed(2);
            strengthInput.title = "Conditioning strength for this staged item";
            strengthInput.disabled = locked;
            strengthInput.addEventListener("change", () => {
                const value = Math.max(0, Math.min(1, Number(strengthInput.value)));
                if (Number.isFinite(value) && value !== Number(item.strength ?? 1)) {
                    void writeItem(item, { strength: value }, "change reference strength");
                }
            });
            top.append(el("span", "Strength", `font-size:11px;color:${COLORS.textMuted};`), strengthInput);

            const hard = recipe.recipe?.hard || {};
            const allowedLengths = Array.isArray(hard.allowed_frame_counts)
                ? hard.allowed_frame_counts.filter((value) => Number.isFinite(Number(value)) && Number(value) > 0)
                : [];
            if (hard.assembly === "temporal" && allowedLengths.length) {
                const sequenceSelect = el("select", "", chromeInputCss({ padding: "3px 6px", fontSize: "11px" }) + "width:92px;");
                const auto = el("option", "Auto length");
                auto.value = "0";
                sequenceSelect.appendChild(auto);
                for (const length of allowedLengths) {
                    const option = el("option", `${Math.trunc(Number(length))} frames`);
                    option.value = String(Math.trunc(Number(length)));
                    sequenceSelect.appendChild(option);
                }
                sequenceSelect.value = String(Math.max(0, parseInt(item.sequence_frames, 10) || 0));
                sequenceSelect.disabled = locked;
                sequenceSelect.title = "Temporal Reference sequence length";
                sequenceSelect.addEventListener("change", () => {
                    const value = Math.max(0, parseInt(sequenceSelect.value, 10) || 0);
                    if (value !== Math.max(0, parseInt(item.sequence_frames, 10) || 0)) {
                        void writeItem(item, { sequence_frames: value }, "change reference sequence length");
                    }
                });
                top.append(sequenceSelect);
            }
            const mute = button(item.muted ? "Muted" : "Active", "Exclude this item from resolution without deleting it");
            mute.disabled = locked;
            mute.addEventListener("click", () => void writeItem(item, { muted: !item.muted }, "toggle reference mute"));
            const del = button("Delete item", "Remove this staged item", "danger");
            del.disabled = locked;
            del.addEventListener("click", () => void runItemOperation({
                type: "delete_reference_item",
                reference_item_id: item.reference_item_id,
                expected: { ...item },
            }, "delete reference item"));
            top.append(mute, del);
            card.appendChild(top);

            card.appendChild(renderPromptRow(item, locked));

            const members = item.members || [];
            for (const [index, memberRef] of members.entries()) {
                card.appendChild(renderMemberRow(item, memberRef, index, members.length, locked));
            }

            if (state.pickerItemId === item.reference_item_id) {
                card.appendChild(renderMemberPicker(item, recipe.media_kind === "audio" ? "audio" : "image"));
            } else {
                const addMember = button("+ Add member", "Stage another Library member on this item");
                addMember.disabled = locked;
                addMember.addEventListener("click", () => {
                    state.pickerItemId = item.reference_item_id;
                    state.pickerQuery = "";
                    render();
                });
                card.appendChild(addMember);
            }
            body.appendChild(card);
        }
    };

    // A reference item must carry at least one member — the backend refuses an
    // empty one — so creation runs from the picker, never from a bare button.
    const createItem = async (members) => {
        const items = laneItems();
        const duration = Math.max(1, parseInt(host.activeScene?.duration_frames, 10) || host.totalFrames || 1);
        const start = Math.min(duration - 1, Math.max(0, Math.round(Number(host.playhead) || 0)));
        if (items.some((item) => {
            const end = item.end_frame === -1 ? duration : item.end_frame;
            return (item.start_frame || 0) <= start && end > start;
        })) {
            notifyWarning("Another item already covers the playhead on this lane. Move the playhead and try again.", { source: "reference-panel-refused" });
            render();
            return;
        }
        const nextStart = items
            .map((item) => item.start_frame || 0)
            .filter((value) => value > start)
            .sort((left, right) => left - right)[0];
        await runItemOperation({
            type: "create_reference_item",
            fields: {
                lane_index: state.laneIndex,
                start_frame: start,
                end_frame: Number.isFinite(nextStart) ? nextStart : -1,
                members,
                prompt_override: "",
                strength: 1.0,
                sequence_frames: 0,
                muted: false,
            },
        }, "add reference item");
    };

    // ── Shell ──────────────────────────────────────────────────────────────
    const header = el("div", "", `
        display:flex; align-items:center; justify-content:space-between; gap:12px;
        padding:14px 18px 11px; background:${COLORS.panel};
        border-bottom:1px solid ${COLORS.border}; flex-shrink:0;
    `);
    const titleWrap = el("div");
    titleWrap.appendChild(el("div", "Reference Lanes", "font-size:15px;font-weight:700;color:#fff;"));
    const subtitle = el("div", "", `font-size:11px;color:${COLORS.textMuted};margin-top:3px;`);
    titleWrap.appendChild(subtitle);
    const closeBtn = el("button", "Close", chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" }));
    closeBtn.addEventListener("click", close);
    header.append(titleWrap, closeBtn);
    panel.appendChild(header);

    const tabs = el("div", "", `
        display:flex; gap:6px; flex-wrap:wrap; padding:9px 18px;
        border-bottom:1px solid ${COLORS.border}; flex-shrink:0;
    `);
    panel.appendChild(tabs);

    const body = el("div", "", `
        padding:12px 18px 18px; display:flex; flex-direction:column; gap:8px;
        overflow:auto; ${chromeScrollbarCss()}
    `);
    panel.appendChild(body);

    const render = () => {
        if (!mounted) return;
        const entries = laneEntries();
        if (entries.length && !entries.some((entry) => (entry.laneIndex || 0) === state.laneIndex)) {
            state.laneIndex = entries[0].laneIndex || 0;
        }
        subtitle.textContent = laneLocked()
            ? "Lane locked — unlock it on the timeline header to edit"
            : "Recipe values, staged items and their Library members";

        tabs.textContent = "";
        for (const entry of entries) {
            const index = entry.laneIndex || 0;
            const active = index === state.laneIndex;
            const tab = button(entry.customName || `Reference ${index + 1}`, "", active ? "primary" : "subtle");
            tab.addEventListener("click", () => {
                if (index === state.laneIndex) return;
                state.laneIndex = index;
                state.pickerItemId = "";
                state.pickerQuery = "";
                render();
            });
            tabs.appendChild(tab);
        }
        tabs.style.display = entries.length > 1 ? "flex" : "none";

        body.textContent = "";
        if (!entries.length) {
            body.appendChild(el("div", "This scene has no Reference lanes.", `font-size:11px;color:${COLORS.textMuted};padding:18px;text-align:center;`));
            return;
        }
        // Staged items first: this panel is opened to see what is on the lane.
        // The recipe is set once and read rarely.
        renderItems(body);
        renderAdvisories(body);
        renderRecipe(body);
    };

    render();
    document.body.appendChild(backdrop);

    const handle = {
        close,
        refresh: render,
        get laneIndex() { return state.laneIndex; },
    };
    host._referencePanelHandle = handle;
    return handle;
}
