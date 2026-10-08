import { mountComboPicker, wantsComboPicker } from "./controls_combo_picker.js";
import { mountManageSection, smallButton } from "./controls_manage.js";
import { mountPanelArrangement } from "./controls_panel_arrangement.js";
import { comparePanelRows, canArrange } from "./controls_panel_layout.js";
import { renderPanelMarkdown } from "./panel_markdown.js";
import { createDisclosureMemory } from "./disclosure_memory.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE, setButtonVariant } from "./editor_theme.js";

let nextPanel = 0;
const SEED_MODES = ["fixed", "increment", "decrement", "randomize"];
const editable = d => d.status === "ok";
const identified = d => !!(d.binding_id || d.note_id);
const baseOf = d => (d.binding_id ? "binding:" + d.binding_id : d.note_id ? "note:" + d.note_id : "");
const inEditor = d => d.source === "binding" && d.section === "editor";
// Values, modes and exposure change what a run submits; labels, notes and
// arrangement do not, so only run-affecting saves can refuse a Generate.
const changesRun = target => /^binding:.+:(value|seed_mode|expose|remove|rebind|hide|unhide)$/.test(target);
const CANVAS_EMPTY = "No Controls yet. Choose Manage to expose this workflow's settings here.";
const same = (a, b) => String(a ?? "") === String(b ?? "");
const make = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
};

export function mountControlsPanel(container, host) {
    const panelId = ++nextPanel,
        rows = new Map(),
        groups = new Map(),
        pending = new Set(),
        runPending = new Set();
    const disclosure = createDisclosureMemory("sonder-controls-disclosure-v1");
    let destroyed = false,
        lastView = null,
        noteEditor = null,
        manageOn = false,
        manage = null;
    const capabilities = () => host.capabilities || { hide: false, removeAuthored: true };
    // Target -> reason of its latest refused or unconfirmed save, shown on the row
    // until a newer save of it succeeds or an authoritative view arrives.
    const failed = new Map();
    let validTargets = null,
        layoutSaving = false;
    container.dataset.sonderControlsPanel = "true";
    container.style.cssText = `display:flex;flex-direction:column;flex:1;min-height:0;overflow:auto;padding:${SPACE.s4}px;gap:${SPACE.s3}px;font:${TYPE.t12}px ${FONT.sans};color:${COLORS.text};`;
    // A discreet Manage toggle: normal view keeps values and rendered notes;
    // exposing, renaming, notes and arranging happen in Manage.
    const header = make("div");
    header.style.cssText = `display:flex;justify-content:flex-end;align-items:center;gap:${SPACE.s2}px;`;
    const manageButton = smallButton("Manage");
    manageButton.dataset.controlsManageToggle = "true";
    manageButton.setAttribute("aria-pressed", "false");
    manageButton.title = "Expose, rename, annotate and arrange Controls";
    header.append(manageButton);
    const message = make("div");
    message.style.cssText = `color:${COLORS.textSecondary};line-height:1.5;`;
    const editorSection = make("details");
    editorSection.open = true;
    editorSection.append(make("summary", "Editor"));
    const editorBody = make("div");
    editorBody.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;padding-top:${SPACE.s2}px;`;
    editorSection.append(editorBody);
    const settingsRows = [];
    for (const [key, label] of [
        ["takePlacementMuted", "Takes start muted"],
        ["linkedTakePlacement", "Link take video + audio"],
    ]) {
        const row = make("label"),
            input = make("input");
        input.type = "checkbox";
        row.style.cssText = `display:flex;gap:${SPACE.s2}px;align-items:center;`;
        row.append(input, make("span", label));
        input.addEventListener("change", () => host.setEditorSetting("render." + key, input.checked));
        editorBody.append(row);
        settingsRows.push({ key, input });
    }
    const fieldsBody = make("div"),
        workflowBody = make("div");
    fieldsBody.style.cssText = workflowBody.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;`;
    workflowBody.dataset.panelDropGroup = "";
    const mainDrop = make("div", "Main section");
    mainDrop.dataset.panelDropGroup = "";
    mainDrop.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};padding:${SPACE.s2}px 0;`;
    editorBody.append(fieldsBody);
    container.append(header, editorSection, message, workflowBody);
    const warning = reason => host.notify?.(reason || "The change could not be saved.");
    const inputCss = `width:100%;min-width:0;box-sizing:border-box;border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;background:${COLORS.panelMuted};color:${COLORS.text};font:${TYPE.t12}px ${FONT.sans};padding:${SPACE.s2}px;`;
    const settingValues = () => host.getEditorSettings?.()?.render || {};

    function readInput(row) {
        return row.input.type === "checkbox" ? row.input.checked : row.input.value;
    }
    function setInput(row, value) {
        if (row.input.type === "checkbox") {
            row.input.checked = !!value;
            row.input.indeterminate = !!row.spec.mixed;
        } else {
            if (row.picker) {
                row.picker.setValue(value);
                return;
            }
            if (row.input.tagName === "SELECT" && ![...row.input.options].some(o => o.value === String(value ?? ""))) {
                row.input.append(new Option(String(value ?? ""), String(value ?? "")));
            }
            row.input.value = String(value ?? "");
        }
    }
    function track(work, blocksRun = false) {
        pending.add(work);
        if (blocksRun) runPending.add(work);
        work.finally(() => {
            pending.delete(work);
            runPending.delete(work);
        });
        return work;
    }
    function layoutEntries() {
        const view = host.getView();
        // Editor-section settings stay fixed at the bottom; only Workflow rows arrange.
        return [...(view.bindings || []).map(d => ({ ...d, source: "binding" })), ...(view.notes || [])]
            .filter(d => !inEditor(d))
            .map(d => ({ ...d, base: baseOf(d) }));
    }
    async function saveLayout(changes) {
        layoutSaving = true;
        let timer;
        const actual = Promise.resolve().then(() => host.applyChanges(changes));
        const work = (async () => {
            let result;
            try {
                result = await Promise.race([
                    actual,
                    new Promise(resolve => {
                        timer = setTimeout(
                            () =>
                                resolve({
                                    ok: false,
                                    rejected: [
                                        { reason: "The arrangement was not confirmed. Try again once connected." },
                                    ],
                                }),
                            5000,
                        );
                    }),
                ]);
            } catch (error) {
                result = { ok: false, rejected: [{ reason: error.message }] };
            }
            clearTimeout(timer);
            if (destroyed) return false;
            layoutSaving = false;
            // A refused arrangement leaves the panel as it was; it never blocks a run.
            if (!result.ok) warning(result.rejected?.[0]?.reason);
            render();
            return !!result.ok;
        })();
        actual.catch(() => {});
        return track(work);
    }
    const arrangement = mountPanelArrangement(container, {
        getEntries: layoutEntries,
        isBusy: () => layoutSaving || pending.size > 0 || !!noteEditor || !!manage?.editing || [...rows.values()].some(r => r.editing),
        apply: saveLayout,
        notify: warning,
    });
    function commit(row, target, value, before) {
        const token = (row.mutations.get(target)?.token || 0) + 1;
        row.mutations.set(target, { token, pending: true });
        failed.delete(target);
        const optimisticKey =
            target === row.target
                ? "optimistic"
                : target.endsWith(":seed_mode")
                  ? "modeOptimistic"
                  : target.endsWith(":label")
                    ? "labelOptimistic"
                    : "noteOptimistic";
        row[optimisticKey] = value;
        let timer;
        const actual = Promise.resolve().then(() => host.applyChanges({ [target]: value }));
        const work = (async () => {
            let result;
            try {
                result = await Promise.race([
                    actual,
                    new Promise(resolve => {
                        timer = setTimeout(
                            () =>
                                resolve({
                                    ok: false,
                                    rejected: [
                                        { target, reason: "The change was not confirmed. Try again once connected." },
                                    ],
                                }),
                            5000,
                        );
                    }),
                ]);
            } catch (error) {
                result = {
                    ok: false,
                    rejected: [{ target, reason: error?.message || "The change could not be saved." }],
                };
            }
            clearTimeout(timer);
            if (
                destroyed ||
                (validTargets && !validTargets.has(target)) ||
                token !== row.mutations.get(target)?.token
            ) {
                return true;
            }
            row.mutations.get(target).pending = false;
            if (!result.ok) {
                failed.set(target, result.rejected?.[0]?.reason || "The change could not be saved.");
                row[optimisticKey] = undefined;
                if (row.input && target === row.target && (!row.editing || same(readInput(row), value))) {
                    setInput(row, row.spec.value ?? before);
                    row.editStart = readInput(row);
                }
                if (row.mode && target.endsWith(":seed_mode") && same(row.mode.value, value)) {
                    row.mode.value = row.spec.seed_mode ?? before;
                    row.modeEditStart = row.mode.value;
                }
                warning(result.rejected?.[0]?.reason);
            }
            render();
            return !!result.ok;
        })();
        // A save confirmed after its timeout is saved; retire the stale warning.
        actual.then(
            late => {
                if (!late?.ok || destroyed || token !== row.mutations.get(target)?.token) return;
                if (failed.delete(target)) render();
            },
            () => {},
        );
        return track(work, changesRun(target));
    }
    function commitValue(row) {
        if (row.suppress || !row.input || row.input.disabled) return Promise.resolve(true);
        const value = readInput(row),
            before = row.editStart;
        row.editing = document.activeElement === row.input && row.input.type !== "checkbox";
        if (same(value, before)) return Promise.resolve(true);
        row.editStart = value;
        return commit(row, row.target, value, before);
    }
    // Structural Manage actions (remove, hide, unhide) on one row.
    function structural(changes, button) {
        if (button) button.disabled = true;
        const work = Promise.resolve()
            .then(() => host.applyChanges(changes))
            .catch(error => ({ ok: false, rejected: [{ reason: error?.message }] }))
            .then(result => {
                if (destroyed) return false;
                if (button) button.disabled = false;
                if (!result?.ok) warning(result?.rejected?.[0]?.reason);
                render();
                return !!result?.ok;
            });
        return track(work, Object.keys(changes).some(changesRun));
    }
    function closeRename(row, save) {
        const input = row.renameInput;
        if (!input) return;
        row.renameInput = null;
        row.editing = false;
        const next = input.value.trim(),
            before = row.labelOptimistic ?? row.spec.label ?? "";
        input.replaceWith(row.label);
        if (save && next && next !== before) void commit(row, `binding:${row.spec.binding_id}:label`, next, before);
        render();
    }
    function openRename(row) {
        if (!manageOn || row.renameInput || destroyed) return;
        const input = make("input");
        input.type = "text";
        input.maxLength = 120;
        input.value = row.labelOptimistic ?? row.spec.label ?? "";
        input.setAttribute("aria-label", "Control name");
        input.dataset.sonderTextUndo = "true";
        input.style.cssText = inputCss;
        row.renameInput = input;
        row.editing = true;
        row.label.replaceWith(input);
        input.addEventListener("blur", () => closeRename(row, true));
        input.focus();
        input.select?.();
    }
    function rowActions(row) {
        const spec = row.spec,
            box = row.actions;
        const local = spec.origin === "local";
        box.style.display = manageOn ? "flex" : "none";
        // Rebuilt only when what it offers changes: a refresh between a
        // press and its click must not swap the button out from under it.
        const caps = capabilities();
        const key = JSON.stringify([manageOn, local, !!spec.hidden, spec.status, spec.source, caps]);
        if (row.actionsKey === key) return;
        row.actionsKey = key;
        const items = [];
        if (local) items.push(marker("App only", "Added in the Sonder App; not part of the workflow file."));
        if (spec.hidden) items.push(marker("Hidden", "Not shown or applied in this app."));
        if (spec.source === "binding") {
            const rename = smallButton("Rename");
            rename.addEventListener("click", () => openRename(row));
            items.push(rename);
            if (spec.status !== "ok" && spec.status !== "shadowed" && (caps.removeAuthored || local)) {
                const rebind = smallButton("Rebind");
                rebind.title = "Bind this Control to another setting, keeping its id";
                rebind.addEventListener("click", () => manage?.setRebind(spec));
                items.push(rebind);
            }
        }
        const base = baseOf(spec);
        if (caps.removeAuthored || local) {
            const remove = smallButton("Remove");
            remove.title = caps.removeAuthored ? "Remove from Controls (Undo restores it)" : "Remove this app-only item";
            remove.addEventListener("click", () => void structural({ [`${base}:remove`]: true }, remove));
            items.push(remove);
        } else if (caps.hide) {
            const toggle = smallButton(spec.hidden ? "Unhide" : "Hide");
            toggle.title = spec.hidden ? "Show and apply it again" : "Hide it here; the workflow's value runs";
            toggle.addEventListener("click", () => void structural({ [`${base}:${spec.hidden ? "unhide" : "hide"}`]: true }, toggle));
            items.push(toggle);
        }
        box.replaceChildren(...items);
    }
    function marker(text, title) {
        const tag = make("span", text);
        tag.title = title;
        tag.style.cssText = `font-size:${TYPE.t10}px;color:${COLORS.textSecondary};border:1px solid ${COLORS.border};border-radius:${RADIUS.r1}px;padding:0 ${SPACE.s1}px;`;
        return tag;
    }
    function closeNote(save) {
        const edit = noteEditor;
        if (!edit) return Promise.resolve(true);
        if (edit.composing) return Promise.resolve(false);
        noteEditor = null;
        edit.unregister();
        edit.row.editing = false;
        const next = edit.area.value;
        edit.box.replaceChildren();
        if (!save || next === edit.before) {
            render();
            return Promise.resolve(true);
        }
        edit.row.noteOptimistic = next;
        render();
        return commit(edit.row, edit.target, next, edit.before).then(ok => {
            render();
            return ok;
        });
    }
    function openNote(row, box, target, text, canEdit) {
        if (!canEdit || destroyed) return;
        if (noteEditor) void closeNote(true);
        const area = make("textarea"),
            done = make("button", "Done");
        area.value = String(text ?? "");
        area.maxLength = 4000;
        area.dataset.sonderTextUndo = "true";
        area.style.cssText = inputCss + "min-height:100px;resize:vertical;";
        done.type = "button";
        done.style.cssText = `border:1px solid ${COLORS.border};background:${COLORS.accentSoft};color:${COLORS.text};border-radius:${RADIUS.r2}px;padding:${SPACE.s1}px ${SPACE.s2}px;align-self:flex-end;cursor:pointer;`;
        box.replaceChildren(area, done);
        row.editing = true;
        const edit = { row, box, target, area, before: String(text ?? ""), composing: false, unregister: null };
        noteEditor = edit;
        area.addEventListener("compositionstart", () => (edit.composing = true));
        area.addEventListener("compositionend", () => (edit.composing = false));
        edit.unregister = register({
            id: `controls-note:${panelId}`,
            priority: PRIORITY.OVERLAY,
            keydown: e => {
                if (noteEditor !== edit || !box.contains(document.activeElement)) return false;
                if (edit.composing || e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
                if (e.key === "Escape") {
                    void closeNote(false);
                    return true;
                }
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                    void closeNote(true);
                    return true;
                }
                return PRESERVE_DEFAULT;
            },
        });
        area.addEventListener("blur", () => {
            if (noteEditor === edit && !edit.composing) void closeNote(true);
        });
        done.addEventListener("mousedown", e => e.preventDefault());
        done.addEventListener("click", () => void closeNote(true));
        area.focus();
    }
    function noteBox(row, target, text, canEdit) {
        const box = make("div");
        box.dataset.noteTarget = target;
        box.style.cssText = `line-height:1.5;color:${COLORS.textSecondary};overflow-wrap:anywhere;cursor:${canEdit ? "text" : "default"};`;
        // Notes are authored in Manage; normal view shows them rendered.
        box.addEventListener("dblclick", () =>
            openNote(row, box, target, row.noteText, manageOn && identified(row.spec) && !row.spec.note_truncated),
        );
        return box;
    }
    function build(spec, target) {
        const row = {
            spec,
            target,
            kind: spec.kind,
            mutations: new Map(),
            editing: false,
            suppress: false,
            optimistic: undefined,
            noteOptimistic: undefined,
            labelOptimistic: undefined,
        };
        row.el = make("div");
        row.el.dataset.controlTarget = target;
        row.el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;padding:${SPACE.s2}px 0;border-bottom:1px solid ${COLORS.border};min-width:0;`;
        if (!inEditor(spec)) {
            row.grip = make("span", "⋮⋮");
            row.grip.dataset.panelDragHandle = "true";
            row.grip.draggable = true;
            row.grip.title = "Drag to reorder or move to another section";
            row.grip.setAttribute("aria-label", "Drag to arrange");
            row.el.style.position = "relative";
            row.el.style.paddingRight = `${SPACE.s5}px`;
            row.grip.style.cssText = `position:absolute;right:0;top:${SPACE.s2}px;cursor:grab;color:${COLORS.textSecondary};user-select:none;font-size:${TYPE.t14}px;`;
            row.el.append(row.grip);
        }
        if (spec.source === "note") {
            row.note = noteBox(row, target, spec.text, spec.status === "ok" && !spec.note_truncated);
            row.el.append(row.note);
        } else {
            row.label = make("label", spec.label);
            row.label.style.fontWeight = String(TYPE.fwBold);
            row.el.append(row.label);
            if (spec.kind === "COMBO" && wantsComboPicker(spec)) {
                row.picker = mountComboPicker({
                    inputCss,
                    onLeave: () => {
                        row.editing = false;
                        render();
                    },
                    choose: () => void commitValue(row),
                    cancel: () => {
                        row.suppress = true;
                        setInput(row, row.editStart);
                        row.input.blur();
                        row.suppress = false;
                    },
                });
                row.input = row.picker.input;
            } else {
                row.input = make(
                    spec.kind === "COMBO" ? "select" : spec.kind === "STRING" && spec.multiline ? "textarea" : "input",
                );
            }
            if (row.input.tagName === "INPUT" && !row.picker) {
                row.input.type =
                    spec.kind === "BOOLEAN" ? "checkbox" : ["INT", "FLOAT"].includes(spec.kind) ? "number" : "text";
            }
            if (spec.kind === "SEED") row.input.inputMode = "numeric";
            row.input.style.cssText = spec.kind === "BOOLEAN" ? "align-self:flex-start;" : inputCss;
            row.input.dataset.sonderTextUndo = "true";
            row.input.setAttribute("aria-label", spec.label || "Control value");
            row.input.id = `controls-${panelId}-${rows.size}`;
            row.label.htmlFor = row.input.id;
            if (spec.kind === "COMBO" && !row.picker) {
                for (const option of spec.options || []) row.input.append(new Option(String(option), String(option)));
            }
            if (spec.min != null) row.input.min = String(spec.min);
            if (spec.max != null) row.input.max = String(spec.max);
            if (spec.kind === "INT") row.input.step = "1";
            if (spec.kind === "FLOAT") row.input.step = String(spec.step || "any");
            row.el.append(row.picker?.box || row.input);
            row.input.addEventListener("focus", () => {
                if (!row.editing) row.editStart = readInput(row);
                row.editing = true;
            });
            row.input.addEventListener("change", () => {
                if (!row.picker) void commitValue(row);
            });
            row.input.addEventListener("blur", () => {
                if (row.picker) return;
                if (row.input.tagName === "TEXTAREA") void commitValue(row);
                row.editing = false;
                row.picker?.close();
                render();
            });
            row.input.addEventListener("compositionstart", () => (row.composing = true));
            row.input.addEventListener("compositionend", () => (row.composing = false));
            if (spec.kind === "SEED") {
                row.mode = make("select");
                row.mode.setAttribute("aria-label", (spec.label || "Seed") + " mode");
                row.mode.style.cssText = inputCss;
                for (const mode of SEED_MODES) row.mode.append(new Option(mode[0].toUpperCase() + mode.slice(1), mode));
                row.mode.addEventListener("focus", () => {
                    row.editing = true;
                    row.modeEditStart = row.mode.value;
                });
                row.mode.addEventListener("blur", () => {
                    row.editing = false;
                    render();
                });
                row.mode.addEventListener("change", () => {
                    row.modeEditStart = row.mode.value;
                    void commit(row, `binding:${spec.binding_id}:seed_mode`, row.mode.value, row.spec.seed_mode);
                });
                row.el.append(row.mode);
            }
            if (spec.source === "binding") {
                row.note = noteBox(
                    row,
                    `binding:${spec.binding_id}:note`,
                    spec.note,
                    identified(spec) && !spec.note_truncated,
                );
                row.el.append(row.note);
            }
        }
        row.status = make("div");
        row.status.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};line-height:1.4;`;
        row.actions = make("div");
        row.actions.dataset.controlsRowActions = "true";
        row.actions.style.cssText = `display:none;gap:${SPACE.s1}px;flex-wrap:wrap;align-items:center;`;
        row.el.append(row.status, row.actions);
        return row;
    }
    function update(row, spec, fresh) {
        row.spec = spec;
        if (row.grip) {
            const base = baseOf(spec);
            const allowed = manageOn && canArrange({ ...spec, base });
            row.grip.draggable = allowed;
            row.grip.hidden = !allowed;
            if (allowed) row.el.dataset.panelBase = base;
            else delete row.el.dataset.panelBase;
        }
        if (row.label) {
            const label = row.labelOptimistic ?? spec.label ?? "";
            row.label.textContent = label;
            row.input.setAttribute("aria-label", label || "Control value");
        }
        if (row.mode) row.mode.setAttribute("aria-label", (spec.label || "Seed") + " mode");
        // The status line is not an input, so it stays current while the row is edited.
        const noteTarget = spec.source === "note" ? row.target : `binding:${spec.binding_id}:note`;
        const modeTarget = `binding:${spec.binding_id}:seed_mode`;
        const labelTarget = `binding:${spec.binding_id}:label`;
        const failure = [row.target, modeTarget, noteTarget, labelTarget]
            .map(target => failed.get(target))
            .find(Boolean);
        row.status.textContent = failure
            ? `Not saved: ${failure}`
            : spec.note_truncated
              ? "Edit this longer note on the canvas."
              : spec.reason ||
                spec.error ||
                (spec.checked_at_run ? "Checked by the node when the run starts." : "");
        row.status.style.color = failure ? COLORS.warningText : COLORS.textDim;
        rowActions(row);
        if (row.editing) return;
        if (fresh) {
            if (!row.mutations.get(row.target)?.pending) row.optimistic = undefined;
            if (!row.mutations.get(modeTarget)?.pending) row.modeOptimistic = undefined;
            if (!row.mutations.get(noteTarget)?.pending) row.noteOptimistic = undefined;
            if (!row.mutations.get(labelTarget)?.pending) row.labelOptimistic = undefined;
        }
        if (row.input) {
            row.input.disabled = !editable(spec);
            if (row.picker) row.picker.update(spec);
            if (spec.kind === "COMBO" && !row.picker && row.optionsKey !== JSON.stringify(spec.options || [])) {
                row.optionsKey = JSON.stringify(spec.options || []);
                row.input.replaceChildren(...(spec.options || []).map(o => new Option(String(o), String(o))));
            }
            row.input.min = spec.min == null ? "" : String(spec.min);
            row.input.max = spec.max == null ? "" : String(spec.max);
            setInput(row, row.optimistic ?? spec.value);
        }
        if (row.mode) {
            row.mode.disabled = !editable(spec);
            row.mode.value = row.modeOptimistic ?? spec.seed_mode ?? "fixed";
        }
        if (row.note) {
            row.noteText = row.noteOptimistic ?? (spec.source === "note" ? spec.text : spec.note) ?? "";
            row.note.replaceChildren(renderPanelMarkdown(row.noteText));
            row.note.style.cursor = manageOn ? "text" : "default";
            row.note.title = manageOn ? "Double-click to edit" : "";
            if (!row.noteText) {
                // The Add note prompt belongs to Manage (SR-004).
                row.note.textContent = manageOn ? (spec.source === "note" ? "Double-click to write this note." : "Double-click to add a note.") : "";
                row.note.style.fontStyle = "italic";
                row.note.style.display = manageOn || spec.source === "note" ? "" : "none";
            } else {
                row.note.style.fontStyle = "normal";
                row.note.style.display = "";
            }
        }
    }
    function render(view = host.getView()) {
        if (destroyed) return;
        const fresh = view !== lastView;
        lastView = view;
        for (const { key, input } of settingsRows) input.checked = !!settingValues()[key];
        const empty = ![...(view.bindings || []), ...(view.notes || [])].some(d => manageOn || !d.hidden);
        message.textContent =
            view.error ||
            view.relay?.error ||
            (!empty ? "" : view.state === "ready" ? host.emptyMessage || CANVAS_EMPTY : "Loading controls…");
        manage?.render(view);
        const entries = [];
        for (const spec of [...(view.bindings || []), ...(view.notes || [])]) {
            const source = spec.source || ("note_id" in spec ? "note" : "binding");
            const id = source === "note" ? spec.note_id : spec.binding_id;
            if (!id) continue;
            // Hidden in the Sonder App: not exposed, so only Manage lists it.
            if (spec.hidden && !manageOn) continue;
            const target = source === "note" ? `note:${id}:text` : `binding:${id}:value`;
            entries.push({ target, spec: { ...spec, source }, group: spec.group || "" });
        }
        if (view.state === "ready") {
            validTargets = new Set(
                entries.flatMap(({ target, spec }) => {
                    const base = baseOf(spec);
                    return [
                        target,
                        `${base}:order`,
                        `${base}:group`,
                        ...(spec.source === "binding"
                            ? [`${base}:note`, `${base}:label`, ...(spec.kind === "SEED" ? [`${base}:seed_mode`] : [])]
                            : []),
                    ];
                }),
            );
            // An authoritative view shows the stored values the row now holds,
            // so it retires every failure no save is still working on.
            if (fresh) {
                for (const target of [...failed.keys()]) {
                    const owner = [...rows.values()].find(row => row.mutations.get(target)?.pending);
                    if (!owner) failed.delete(target);
                }
            }
        }
        entries.sort((a, b) => comparePanelRows({ ...a.spec, base: a.target }, { ...b.spec, base: b.target }));
        workflowBody.prepend(mainDrop);
        // The Main drop zone belongs to arranging, which is Manage only.
        mainDrop.style.display = manageOn ? "" : "none";
        const alive = new Set(),
            groupOrder = [];
        for (const { target, spec, group } of entries) {
            alive.add(target);
            let row = rows.get(target);
            if (
                row &&
                (row.kind !== spec.kind || (spec.kind === "COMBO" && !!row.picker !== wantsComboPicker(spec))) &&
                !row.editing
            ) {
                row.picker?.destroy();
                row.el.remove();
                rows.delete(target);
                row = null;
            }
            if (!row) {
                row = build(spec, target);
                rows.set(target, row);
            }
            update(row, spec, fresh);
            let body = inEditor(spec) ? fieldsBody : workflowBody;
            if (group && !inEditor(spec)) {
                if (!groups.has(group)) {
                    const detail = make("details"),
                        summary = make("summary", group),
                        content = make("div");
                    summary.dataset.panelDropGroup = group;
                    content.dataset.panelDropGroup = group;
                    detail.open = disclosure.isOpen(group, group.toLowerCase() !== "advanced");
                    content.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;padding-top:${SPACE.s1}px;`;
                    detail.append(summary, content);
                    detail.addEventListener("toggle", () => disclosure.remember(group, detail.open));
                    groups.set(group, { detail, content });
                }
                body = groups.get(group).content;
                if (!groupOrder.includes(group)) groupOrder.push(group);
            }
            if (!row.editing) body.append(row.el);
        }
        for (const [target, row] of rows)
            if (!alive.has(target) && !row.editing) {
                row.picker?.destroy();
                row.el.remove();
                rows.delete(target);
            }
        for (const group of groupOrder) workflowBody.append(groups.get(group).detail);
        for (const [group, obj] of groups)
            if (!groupOrder.includes(group) && ![...rows.values()].some(r => r.editing && obj.detail.contains(r.el))) {
                obj.detail.remove();
                groups.delete(group);
            }
    }
    function setManage(on) {
        if (destroyed || on === manageOn) return;
        if (!on) {
            for (const row of rows.values()) if (row.renameInput) closeRename(row, true);
            if (noteEditor) void closeNote(true);
        }
        manageOn = on;
        manageButton.textContent = on ? "Done" : "Manage";
        manageButton.setAttribute("aria-pressed", String(on));
        setButtonVariant(manageButton, on ? "accentSoft" : "secondary", { padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
        if (on) {
            manage = mountManageSection(host, { notify: warning,
                track: (work, changes) => track(work, Object.keys(changes).some(changesRun)) });
            header.after(manage.el);
        } else {
            manage?.destroy();
            manage = null;
        }
        host.setManage?.(on, "");
        render();
    }
    manageButton.addEventListener("click", () => setManage(!manageOn));
    const unregister = register({
        id: `controls-values:${panelId}`,
        priority: PRIORITY.TEXT_EDITOR,
        keydown: e => {
            const renaming = [...rows.values()].find(row => row.renameInput && row.renameInput === document.activeElement);
            if (renaming) {
                if (e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
                if (e.key === "Escape") { closeRename(renaming, false); return true; }
                if (e.key === "Enter") { closeRename(renaming, true); return true; }
                return PRESERVE_DEFAULT;
            }
            const row = [...rows.values()].find(
                row =>
                    row.input === document.activeElement ||
                    row.mode === document.activeElement ||
                    row.picker?.contains(document.activeElement),
            );
            if (!row) return false;
            if (row.mode === document.activeElement) return PRESERVE_DEFAULT;
            if (row.composing || e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
            if (row.picker && row.picker.keydown(e)) return true;
            if (e.key === "Escape") {
                row.suppress = true;
                setInput(row, row.editStart);
                row.input.blur();
                row.suppress = false;
                return true;
            }
            if (e.key === "Enter" && (row.input.tagName !== "TEXTAREA" || e.ctrlKey || e.metaKey)) {
                void commitValue(row);
                row.input.blur();
                return true;
            }
            return PRESERVE_DEFAULT;
        },
    });
    const unsubscribe = host.subscribe(view => render(view));
    render();
    return {
        render,
        // Saves the focused field or note, then waits for every save. Only a
        // run-value save still in flight now can refuse the caller's run: an
        // older refusal already reverted its row to the stored value.
        async flushPending() {
            // Snapshot before awaiting anything: a run save that settles while
            // the note saves was still in flight when the caller asked.
            const blocking = new Set(runPending);
            const closing = noteEditor ? closeNote(true) : null;
            const row = [...rows.values()].find(row => row.input === document.activeElement);
            if (row && !row.picker) {
                const saving = commitValue(row);
                if (changesRun(row.target)) blocking.add(saving);
            }
            await closing;
            await Promise.all([...pending]);
            return (await Promise.all(blocking)).every(Boolean);
        },
        destroy() {
            if (noteEditor) void closeNote(false);
            if (manageOn) host.setManage?.(false, "");
            manage?.destroy();
            destroyed = true;
            arrangement.destroy();
            for (const row of rows.values()) row.picker?.destroy();
            unsubscribe?.();
            unregister();
            rows.clear();
            groups.clear();
            container.replaceChildren();
        },
    };
}
