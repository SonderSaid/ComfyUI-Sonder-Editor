import { mountRestore } from "./controls_restore.js";
import { mountPresets } from "./controls_presets.js";
import { mountComboPicker, wantsComboPicker } from "./controls_combo_picker.js";
import { mountManageSection, smallButton } from "./controls_manage.js";
import { mountPanelArrangement } from "./controls_panel_arrangement.js";
import { EDITOR_PARTITION, comparePanelRows, canArrange, planPanelMove } from "./controls_panel_layout.js";
import { moveToGroupItems, openGroupMenu, startGroupRename } from "./controls_panel_groups.js";
import { changesRun } from "./control_targets.js";
import { openContextMenu } from "./editor_context_menu.js";
import { disclosureKey, entryVisible, groupPlan, hasAdvanced, hiddenAdvanced, matchEntry } from "./controls_panel_view.js";
import * as S from "./controls_panel_style.js";
import { renderPanelMarkdown } from "./panel_markdown.js";
import { createDisclosureMemory } from "./disclosure_memory.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE, applyNativeControlTheme, setButtonVariant } from "./editor_theme.js";

let nextPanel = 0;
const SEED_MODES = ["fixed", "increment", "decrement", "randomize"];
const editable = d => d.status === "ok";
const identified = d => !!(d.binding_id || d.note_id);
const baseOf = d => (d.binding_id ? "binding:" + d.binding_id : d.note_id ? "note:" + d.note_id : "");
const inEditor = d => d.source === "binding" && d.section === "editor";
const CANVAS_EMPTY = "No Controls yet. Choose Manage to expose this workflow's settings here.";
const LONG_VALUE = 18; // a choice longer than this gets a wide card
// Your own editor settings, shown first in the Editor group in every mode. They
// write through the host's typed setter (editor_settings.js controlsPanelSettingPatch).
const MIRRORS = [
    { key: "render.takePlacementMuted", label: "Takes start muted", kind: "BOOLEAN" },
    { key: "render.linkedTakePlacement", label: "Link take video + audio", kind: "BOOLEAN" },
    { key: "batchRender.maxFramesPerChunk", label: "Max frames per batch", kind: "INT",
        title: "Total frames per + Batch job, context included; 0 uses the built-in size. Snaps up to the model's frame rule." },
];
const same = (a, b) => String(a ?? "") === String(b ?? "");
const make = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
};
const settingAt = (settings, key) => key.split(".").reduce((value, name) => value?.[name], settings);
// Rows and groups carry an inline `display`, which outranks the `hidden`
// attribute, so showing and hiding sets both. Call it after any cssText write.
const setShown = (el, shown) => {
    el.hidden = !shown;
    if (!shown) el.style.display = "none";
    else if (el.style.display === "none") el.style.display = "";
};

// Puts `wanted` in order inside `body` without moving the element that holds
// focus: moving it blurs it, which ends a rename or discards a draft. Its
// siblings move around it instead.
function placeInOrder(body, wanted) {
    const active = document.activeElement;
    const anchor = wanted.find(el => el.parentElement === body && el.contains(active));
    if (!anchor) {
        for (const el of wanted) body.append(el);
        return;
    }
    const at = wanted.indexOf(anchor);
    for (const el of wanted.slice(0, at)) anchor.before(el);
    let previous = anchor;
    for (const el of wanted.slice(at + 1)) {
        previous.after(el);
        previous = el;
    }
}

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
        manage = null,
        query = "",
        groupOrder = []; // every workflow group, in display order (Manage shows empty ones)
    const capabilities = () => host.capabilities || { hide: false, removeAuthored: true };
    const settings = () => host.getEditorSettings?.() || {};
    const cards = () => !manageOn && settings().controls?.view === "cards";
    const showAdvanced = () => settings().controls?.showAdvanced === true;
    // Target -> reason of its latest refused or unconfirmed save, shown on the row
    // until a newer save of it succeeds or an authoritative view arrives.
    const failed = new Map();
    // Cards whose note needs its More/Less overflow check on the next frame.
    const noteChecks = new Set();
    let noteFrame = 0;
    let validTargets = null,
        layoutSaving = false;
    container.dataset.sonderControlsPanel = "true";
    // A stable gutter: Manage's longer list adding a scrollbar never shifts the header.
    container.style.cssText = `display:flex;flex-direction:column;flex:1;min-height:0;overflow:auto;scrollbar-gutter:stable;padding:${SPACE.s3}px;gap:${SPACE.s2}px;font:${TYPE.t12}px ${FONT.sans};color:${COLORS.text};`;
    applyNativeControlTheme(container);

    // Header: search, a discreet Manage toggle, and the hidden-Advanced line.
    const header = make("div");
    // Sticky, so search and Manage stay in reach in a long list.
    header.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;position:sticky;top:-${SPACE.s3}px;z-index:1;`
        + `margin-top:-${SPACE.s3}px;padding-top:${SPACE.s3}px;padding-bottom:${SPACE.s1}px;background:${COLORS.panel};`;
    const headerTop = make("div");
    headerTop.style.cssText = `display:flex;align-items:center;gap:${SPACE.s2}px;`;
    const search = make("input");
    search.type = "search";
    search.placeholder = "Search settings";
    search.setAttribute("aria-label", "Search settings");
    search.dataset.controlsSearch = "true";
    search.dataset.sonderTextUndo = "true";
    search.style.cssText = S.searchCss;
    const manageButton = smallButton("Manage");
    manageButton.dataset.controlsManageToggle = "true";
    manageButton.setAttribute("aria-pressed", "false");
    manageButton.title = "Expose, rename, annotate and arrange Controls";
    // Manage and Done keep one place and size: the slot sits at the right end,
    // and a hidden "Manage" sizer holds its width (setButtonVariant rewrites the
    // button's own style on every toggle, so the size lives in the slot).
    const manageSlot = make("span");
    manageSlot.style.cssText = "display:inline-flex;flex-direction:column;flex:0 0 auto;margin-left:auto;";
    const manageSizer = smallButton("Manage");
    manageSizer.tabIndex = -1;
    manageSizer.setAttribute("aria-hidden", "true");
    manageSizer.style.cssText += "height:0;padding-top:0;padding-bottom:0;border-top-width:0;border-bottom-width:0;"
        + "visibility:hidden;overflow:hidden;";
    manageSlot.append(manageButton, manageSizer);
    headerTop.append(search, manageSlot);
    const quietLine = make("button");
    quietLine.type = "button";
    quietLine.dataset.controlsAdvancedLine = "true";
    quietLine.style.cssText = S.quietLineCss;
    quietLine.addEventListener("click", () => writeSetting("controls.showAdvanced", !showAdvanced()));
    const presets = mountPresets(host, {flushPending: () => flushPending(), track, notify: reason => warning(reason),
        // Switching workflows remounts the panel: Manage drafts would be lost.
        canSwitch: () => {
            if (restore.busy || manage?.presetSaving) {
                warning("Wait for the Manage change to save, then switch workflows.");
                return false;
            }
            return !manage || (manage.canLeave() && (!manage.hasDraft
                || globalThis.confirm("Discard your unsaved Manage edits and switch workflows?")));
        }});
    header.append(headerTop);
    container.append(header);
    header.after(presets.el);
    presets.el.after(quietLine);
    const restore = mountRestore(host, {flushPending: () => flushPending(), track: work => track(work),
        notify: reason => warning(reason), rebind: row => { setManage(true); manage?.setRebind(row); }});
    header.after(restore.el);
    const message = make("div");
    message.style.cssText = `color:${COLORS.textSecondary};line-height:1.5;`;
    const editorGroup = makeGroup(null, "Editor");
    const fieldsBody = make("div");
    const workflowBody = make("div");
    workflowBody.dataset.panelDropGroup = "";
    const mainDrop = make("div", "Main section");
    mainDrop.dataset.panelDropGroup = "";
    mainDrop.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};padding:${SPACE.s1}px 0;grid-column:1 / -1;`;
    container.append(message, editorGroup.el, workflowBody);
    const warning = reason => host.notify?.(reason || "The change could not be saved.");

    function writeSetting(key, value) {
        try {
            host.setEditorSetting?.(key, value);
            return true;
        } catch (error) {
            warning(error?.message);
            render();
            return false;
        }
    }

    // ── Groups ──────────────────────────────────────────────────────────────
    // `name` null is the Editor section. The header is clickable; its toggle is
    // a real button, so the keyboard opens and closes it natively.
    function makeGroup(name, title) {
        const key = disclosureKey(name);
        const group = { name, key, el: make("section"), header: make("div"), toggle: make("button"), title: make("span"),
            count: make("span"), actions: make("span"), content: make("div"), open: disclosure.isOpen(key, true) };
        group.el.dataset.controlsGroup = name === null ? "editor" : name;
        group.el.dataset.controlsSection = name === null ? "editor" : "workflow";
        group.el.style.cssText = S.groupCss;
        group.header.style.cssText = S.groupHeaderCss;
        group.toggle.type = "button";
        group.toggle.style.cssText = S.groupToggleCss;
        group.title.textContent = title;
        group.title.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:0.02em;";
        if (name !== null) {
            // Manage drags a group by this grip (controls_panel_arrangement.js).
            group.grip = make("span", "⋮⋮");
            group.grip.dataset.panelGroupHandle = name;
            group.grip.dataset.groupAction = "drag";
            group.grip.title = "Drag to reorder groups";
            group.grip.setAttribute("aria-hidden", "true");
            group.grip.style.cssText = `cursor:grab;color:${COLORS.textMuted};user-select:none;font-size:${TYPE.t12}px;letter-spacing:-2px;`;
        }
        group.count.style.cssText = S.countCss;
        group.actions.style.cssText = `display:flex;align-items:center;gap:${SPACE.s2}px;`;
        const left = make("span");
        left.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1 + 2}px;min-width:0;`;
        left.append(...(group.grip ? [group.grip] : []), group.toggle, group.title);
        const right = make("span");
        right.style.cssText = `display:flex;align-items:center;gap:${SPACE.s2}px;flex:0 0 auto;`;
        right.append(group.actions, group.count);
        group.header.append(left, right);
        if (name !== null) {
            group.header.dataset.panelDropGroup = name;
            group.content.dataset.panelDropGroup = name;
        }
        group.el.append(group.header, group.content);
        const flip = event => {
            if (event.target.closest?.("[data-group-action]") || group.renaming) return;
            event.preventDefault?.();
            if (query) return; // a search keeps every group open
            group.open = !group.open;
            disclosure.remember(key, group.open);
            render();
        };
        group.header.addEventListener("click", flip);
        return group;
    }
    // Manage adds a note to a group from its header, and opens its menu:
    // rename, move up or down, Mark all / Clear Advanced, Ungroup.
    function paintGroupActions(group) {
        if (group.grip) {
            group.grip.draggable = manageOn && !group.renaming;
            setShown(group.grip, manageOn);
        }
        const key = String(manageOn);
        if (group.actionsKey === key) return;
        group.actionsKey = key;
        if (!manageOn) {
            group.actions.replaceChildren();
            return;
        }
        const note = make("button", "+ Note");
        note.type = "button";
        note.dataset.groupAction = "note";
        note.title = `Add a Markdown note to ${group.name}`;
        note.style.cssText = S.linkCss;
        note.addEventListener("click", event => {
            event.stopPropagation?.();
            manage?.addNote(group.name);
        });
        const menu = make("button", "⋯");
        menu.type = "button";
        menu.dataset.groupAction = "menu";
        menu.title = `Rename, reorder or ungroup ${group.name}`;
        menu.setAttribute("aria-label", `Group ${group.name} actions`);
        menu.setAttribute("aria-haspopup", "menu");
        menu.style.cssText = S.groupMenuCss;
        menu.addEventListener("click", event => {
            event.stopPropagation?.();
            openGroupMenu(menu, {
                name: group.name,
                order: groupOrder,
                members: groupMembers(group.name),
                apply: changes => void structural(changes),
                rename: () => renameGroup(group),
                notify: warning,
            });
        });
        group.actions.replaceChildren(note, menu);
    }
    // A group's rows as the panel shows them, with any unsaved Advanced flag.
    function groupMembers(name) {
        return [...rows.values()]
            .filter(row => !inEditor(row.spec) && (row.spec.group || "") === name)
            .map(row => ({ ...row.spec, advanced: effectiveAdvanced(row) }));
    }
    function renameGroup(group) {
        if (!manageOn || group.renaming || destroyed) return;
        const name = group.name;
        group.renaming = startGroupRename(group.title, {
            name,
            css: S.inputCss + "min-width:0;",
            notify: warning,
            save: next => {
                // The renamed group keeps its open or closed state.
                disclosure.remember(disclosureKey(next), group.open);
                void structural({ [`group:${name}:rename`]: next });
            },
            onClose: () => {
                group.renaming = null;
                render();
            },
        });
        paintGroupActions(group);
    }
    // "Move to group" for one Workflow row: its group and order in one save.
    function openMoveMenu(row, anchor) {
        const base = baseOf(row.spec);
        const current = row.spec.group || "";
        const planMove = group => planPanelMove(layoutEntries(), base, group, null);
        const rect = anchor.getBoundingClientRect?.() || { left: 0, bottom: 0 };
        openContextMenu({
            x: rect.left,
            y: rect.bottom,
            focusFirst: true,
            closeOnScroll: true,
            items: moveToGroupItems({
                order: groupOrder,
                current,
                planMove,
                notify: warning,
                apply: changes => void structural(changes),
                newGroup: () => manage?.addGroup(name => {
                    try {
                        return planPanelMove(layoutEntries(), base, name, null);
                    } catch (error) {
                        warning(error.message);
                        return {};
                    }
                }),
            }),
        });
    }
    function paintGroup(group, visibleCount) {
        const open = !!query || group.open;
        group.toggle.textContent = open ? "v" : ">";
        group.toggle.title = `${open ? "Collapse" : "Expand"} ${group.title.textContent}`;
        group.toggle.setAttribute("aria-expanded", String(open));
        group.content.style.cssText = S.groupContentCss(cards());
        setShown(group.content, open);
        group.count.textContent = String(visibleCount);
    }

    // ── Your editor settings (the Editor group's first rows) ────────────────
    const mirrors = MIRRORS.map(spec => {
        const row = { spec: { ...spec, source: "mirror" }, el: make("div"), line: make("div"), label: make("label"),
            labelText: make("span"), control: make("div"), input: make("input") };
        row.el.dataset.controlsMirror = spec.key;
        row.input.type = spec.kind === "BOOLEAN" ? "checkbox" : "number";
        row.input.id = `controls-${panelId}-mirror-${spec.key.replace(/\W/g, "-")}`;
        row.label.htmlFor = row.input.id;
        row.labelText.textContent = spec.label;
        row.labelText.style.cssText = S.labelTextCss;
        row.label.append(row.labelText);
        if (spec.title) row.el.title = spec.title;
        row.input.setAttribute("aria-label", spec.label);
        if (spec.kind === "INT") {
            row.input.min = "0";
            row.input.max = "10000";
            row.input.step = "1";
            row.input.dataset.sonderTextUndo = "true";
        }
        row.control.append(row.input);
        row.line.append(row.label, row.control);
        row.el.append(row.line);
        row.input.addEventListener("change", () => {
            const value = spec.kind === "BOOLEAN" ? row.input.checked : row.input.value;
            if (!writeSetting(spec.key, value)) paintMirror(row);
        });
        row.input.addEventListener("focus", () => (row.editing = true));
        row.input.addEventListener("blur", () => {
            row.editing = false;
            paintMirror(row);
        });
        return row;
    });
    // These children have a fixed order. Moving them on refresh blurs an
    // active input and discards its unfinished draft in a real browser.
    editorGroup.content.append(...mirrors.map(row => row.el), fieldsBody);
    function paintMirror(row) {
        const value = settingAt(settings(), row.spec.key);
        if (row.spec.kind === "BOOLEAN") row.input.checked = !!value;
        else if (!row.editing) row.input.value = String(value ?? "");
        row.spec.value = value;
    }

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
        // Exposed Editor settings reorder within their own partition (the
        // built-in mirrors above them are not rows and stay put).
        return [...(view.bindings || []).map(d => ({ ...d, source: "binding" })), ...(view.notes || [])]
            .map(d => ({ ...d, ...(inEditor(d) ? { group: EDITOR_PARTITION } : {}), base: baseOf(d) }));
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
    // A value too long for its field shows in full on hover (#9): set as the
    // field's title only while clipped, and only on fields that set none.
    // Text areas are left out: a long prompt would become a screen-sized tooltip,
    // and the field already shows its text and resizes.
    let measure = null;
    const titledBy = new WeakMap(); // field -> the exact title this handler set
    function textClipped(field, text) {
        if (field.tagName !== "SELECT") return field.scrollWidth > field.clientWidth;
        // A select reports no scroll width: measure its chosen text, leaving room for the arrow.
        measure = measure || globalThis.document?.createElement?.("canvas")?.getContext?.("2d");
        if (!measure) return false;
        measure.font = globalThis.getComputedStyle?.(field)?.font || "";
        return measure.measureText(text).width > field.clientWidth - 24;
    }
    function valueTitle(e) {
        const field = e.target?.closest?.("input, select");
        if (!field || !field.closest?.("[data-control-target]") || field.type === "checkbox") return;
        // A list (the long-option picker's choices) is not one value: a title on it
        // would show over every option it lists.
        if (field.tagName === "SELECT" && (field.size > 1 || field.multiple)) return;
        const ours = titledBy.get(field);
        if (field.title && field.title !== ours) return; // a tooltip the row set wins
        const text = field.tagName === "SELECT" ? (field.selectedOptions?.[0]?.textContent || "") : String(field.value || "");
        if (text && textClipped(field, text)) {
            field.title = text;
            titledBy.set(field, text);
        } else if (ours !== undefined) {
            if (field.title === ours) field.removeAttribute("title");
            titledBy.delete(field);
        }
    }
    container.addEventListener("mouseover", valueTitle);
    const arrangement = mountPanelArrangement(container, {
        getEntries: layoutEntries,
        getGroups: () => groupOrder,
        isBusy: () => layoutSaving || pending.size > 0 || !!noteEditor || !!manage?.editing || [...rows.values()].some(r => r.editing)
            || [...groups.values()].some(group => group.renaming),
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
                    : target.endsWith(":advanced")
                      ? "advancedOptimistic"
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
        const affected = (host.getView().presets || []).filter(p => p.members.some(m => `binding:${m.binding_id}:remove` in changes));
        if (button) button.disabled = true;
        const work = Promise.resolve()
            .then(() => host.applyChanges(changes))
            .catch(error => ({ ok: false, rejected: [{ reason: error?.message }] }))
            .then(result => {
                if (destroyed) return false;
                if (button) button.disabled = false;
                if (!result?.ok) warning(result?.rejected?.[0]?.reason);
                else if (affected.length) warning(`Removed Control is still a member of: ${affected.map(p => p.name).join(", ")}. Repair in Manage.`);
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
        // An empty name goes back to the default one (node title and setting).
        if (save && next !== before) void commit(row, `binding:${row.spec.binding_id}:label`, next, before);
        render();
    }
    function openRename(row) {
        if (!manageOn || row.renameInput || destroyed) return;
        const input = make("input");
        input.type = "text";
        input.maxLength = 120;
        input.value = row.labelOptimistic ?? row.spec.label ?? "";
        input.placeholder = "Default name";
        input.setAttribute("aria-label", "Control name");
        input.dataset.sonderTextUndo = "true";
        input.style.cssText = S.inputCss;
        row.renameInput = input;
        row.editing = true;
        row.label.replaceWith(input);
        input.addEventListener("blur", () => closeRename(row, true));
        input.focus();
        input.select?.();
    }
    const effectiveAdvanced = row => (row.advancedOptimistic ?? row.spec.advanced) === true;
    function rowActions(row) {
        const spec = row.spec,
            box = row.actions;
        const local = spec.origin === "local";
        box.style.display = manageOn ? "flex" : "none";
        // Rebuilt only when what it offers changes: a refresh between a
        // press and its click must not swap the button out from under it.
        const caps = capabilities();
        const advanced = spec.source === "binding" && effectiveAdvanced(row);
        const key = JSON.stringify([manageOn, local, !!spec.hidden, spec.status, spec.source, spec.section, caps, advanced, !!row.noteText]);
        if (row.actionsKey === key) return;
        row.actionsKey = key;
        const items = [];
        if (local) items.push(marker("App only", "Added in the Sonder App; not part of the workflow file."));
        if (spec.hidden) items.push(marker("Hidden", "Not shown or applied in this app."));
        if (spec.source === "binding") {
            const rename = smallButton("Rename");
            rename.addEventListener("click", () => openRename(row));
            items.push(rename);
            const note = smallButton(row.noteText ? "Edit note" : "Note");
            note.title = "A Markdown note shown under this setting";
            note.addEventListener("click", () =>
                openNote(row, row.note, `binding:${spec.binding_id}:note`, row.noteText, identified(spec) && !spec.note_truncated));
            items.push(note);
            const flag = smallButton(advanced ? "Advanced ✓" : "Advanced", advanced ? "accentSoft" : "secondary");
            flag.dataset.controlsAdvancedToggle = spec.binding_id;
            flag.setAttribute("aria-pressed", String(advanced));
            flag.title = advanced
                ? "Shown only when Show advanced is on; its value still applies"
                : "Hide it unless Show advanced is on; its value still applies";
            flag.addEventListener("click", () => {
                const next = !effectiveAdvanced(row);
                void commit(row, `binding:${spec.binding_id}:advanced`, next, !next);
                render();
            });
            items.push(flag);
            if (!inEditor(spec)) items.push(groupButton(row));
            if (spec.status !== "ok" && spec.status !== "shadowed" && (caps.removeAuthored || local)) {
                const rebind = smallButton("Rebind");
                rebind.title = "Bind this Control to another setting, keeping its id";
                rebind.addEventListener("click", () => manage?.setRebind(spec));
                items.push(rebind);
            }
        }
        if (spec.source === "note") items.push(groupButton(row));
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
    function groupButton(row) {
        const button = smallButton("Group ▸");
        button.dataset.controlsMoveGroup = baseOf(row.spec);
        button.title = "Move to another group";
        button.setAttribute("aria-haspopup", "menu");
        button.addEventListener("click", () => openMoveMenu(row, button));
        return button;
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
        if (!canEdit || destroyed || !box) return;
        if (noteEditor) void closeNote(true);
        const area = make("textarea"),
            done = make("button", "Done");
        area.value = String(text ?? "");
        area.maxLength = 4000;
        area.dataset.sonderTextUndo = "true";
        area.style.cssText = S.inputCss + "min-height:100px;resize:vertical;";
        done.type = "button";
        done.style.cssText = `border:1px solid ${COLORS.border};background:${COLORS.accentSoft};color:${COLORS.text};border-radius:${RADIUS.r2}px;padding:${SPACE.s1}px ${SPACE.s2}px;align-self:flex-end;cursor:pointer;`;
        box.style.display = "";
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
    function noteBox(row, target, standalone) {
        const box = make("div");
        box.dataset.noteTarget = target;
        box.style.cssText = standalone ? S.standaloneNoteCss : S.noteCss;
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
            advancedOptimistic: undefined,
        };
        row.el = make("div");
        row.el.dataset.controlTarget = target;
        {
            row.grip = make("span", "⋮⋮");
            row.grip.dataset.panelDragHandle = "true";
            row.grip.draggable = true;
            row.grip.title = inEditor(spec) ? "Drag to reorder within Editor" : "Drag to reorder or move to another group";
            row.grip.setAttribute("aria-label", "Drag to arrange");
            row.grip.style.cssText = `position:absolute;right:${SPACE.s1}px;top:${SPACE.s1}px;cursor:grab;color:${COLORS.textMuted};user-select:none;font-size:${TYPE.t12}px;letter-spacing:-2px;`;
            row.el.append(row.grip);
        }
        if (spec.source === "note") {
            row.note = noteBox(row, target, true);
            row.el.append(row.note);
        } else {
            row.line = make("div");
            row.label = make("label");
            row.labelText = make("span", spec.label);
            row.labelText.style.cssText = S.labelTextCss;
            row.tags = make("span");
            row.tags.style.cssText = `display:inline-flex;align-items:center;gap:${SPACE.s1}px;flex:0 0 auto;`;
            row.label.append(row.labelText, row.tags);
            row.control = make("div");
            if (spec.kind === "COMBO" && wantsComboPicker(spec)) {
                row.picker = mountComboPicker({
                    inputCss: S.inputCss,
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
            row.control.append(row.picker?.box || row.input);
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
                row.mode.style.cssText = S.modeSelectCss;
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
                row.control.append(row.mode);
            }
            row.line.append(row.label, row.control);
            row.el.append(row.line);
            row.note = noteBox(row, `binding:${spec.binding_id}:note`, false);
            row.el.append(row.note);
        }
        row.status = make("div");
        row.status.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};line-height:1.4;`;
        row.actions = make("div");
        row.actions.dataset.controlsRowActions = "true";
        row.actions.style.cssText = `display:none;gap:${SPACE.s1}px;flex-wrap:wrap;align-items:center;`;
        row.el.append(row.status, row.actions);
        return row;
    }
    // Rows or Cards, applied on every render so a settings change restyles in place.
    function styleRow(row, spec) {
        const isCards = cards();
        const value = row.optimistic ?? spec.value;
        const stacked = spec.kind === "STRING" && !!spec.multiline;
        // A Control's note no longer widens its card (#11): it clamps inside it.
        const wide = spec.source === "note" || stacked
            || (spec.kind === "COMBO" && String(value ?? "").length > LONG_VALUE);
        row.el.style.cssText = S.rowCss({ cards: isCards, wide });
        if (manageOn && row.grip) row.el.style.paddingRight = `${SPACE.s5}px`;
        if (!row.line) return;
        const inline = spec.kind === "BOOLEAN";
        row.line.style.cssText = S.lineCss({ cards: isCards, stacked, inline });
        row.label.style.cssText = S.labelCss(isCards);
        row.control.style.cssText = S.controlCss({ cards: isCards, alignEnd: true });
        if (spec.kind === "BOOLEAN") row.input.style.cssText = "margin:0;cursor:pointer;";
        else if (["INT", "FLOAT"].includes(spec.kind)) row.input.style.cssText = S.numberInputCss(isCards);
        else if (spec.kind === "SEED") row.input.style.cssText = S.seedInputCss;
        else row.input.style.cssText = S.inputCss + (row.input.tagName === "SELECT" ? "cursor:pointer;" : "")
            + (stacked ? "min-height:60px;resize:vertical;" : "");
    }
    function styleMirror(row) {
        const isCards = cards();
        row.el.style.cssText = S.rowCss({ cards: isCards, wide: false });
        row.line.style.cssText = S.lineCss({ cards: isCards, stacked: false, inline: row.spec.kind === "BOOLEAN" });
        row.label.style.cssText = S.labelCss(isCards);
        row.control.style.cssText = S.controlCss({ cards: isCards, alignEnd: true });
        row.input.style.cssText = row.spec.kind === "BOOLEAN" ? "margin:0;cursor:pointer;" : S.numberInputCss(isCards);
    }
    function paintTags(row, spec) {
        const tags = [];
        const advanced = effectiveAdvanced(row);
        if (advanced && (showAdvanced() || query || manageOn)) {
            const tag = make("span", "Adv");
            tag.title = "Advanced: shown only when Show advanced is on; its value still applies";
            tag.style.cssText = S.tagCss;
            tags.push(tag);
        }
        if (advanced && spec.overridden) {
            const dot = make("span");
            dot.title = "Differs from the workflow's own value";
            dot.style.cssText = S.changedDotCss;
            tags.push(dot);
        }
        row.tags.replaceChildren(...tags);
    }
    function update(row, spec, fresh) {
        row.spec = spec;
        if (!row.presetMember) { row.presetMember = make("div"); row.el.prepend(row.presetMember); }
        const membership = manage?.memberControl(spec);
        row.presetMember.replaceChildren(...(membership ? [membership] : []));
        if (row.grip) {
            const base = baseOf(spec);
            const allowed = manageOn && canArrange({ ...spec, base });
            row.grip.draggable = allowed;
            row.grip.hidden = !allowed;
            if (allowed) row.el.dataset.panelBase = base;
            else delete row.el.dataset.panelBase;
        }
        // The status line is not an input, so it stays current while the row is edited.
        const noteTarget = spec.source === "note" ? row.target : `binding:${spec.binding_id}:note`;
        const modeTarget = `binding:${spec.binding_id}:seed_mode`;
        const labelTarget = `binding:${spec.binding_id}:label`;
        const advancedTarget = `binding:${spec.binding_id}:advanced`;
        if (fresh && !row.mutations.get(advancedTarget)?.pending) row.advancedOptimistic = undefined;
        if (row.label) {
            const label = row.labelOptimistic || spec.label || "";
            row.labelText.textContent = label;
            row.label.title = label;
            row.input.setAttribute("aria-label", label || "Control value");
            paintTags(row, spec);
        }
        if (row.mode) row.mode.setAttribute("aria-label", (spec.label || "Seed") + " mode");
        const failure = [row.target, modeTarget, noteTarget, labelTarget, advancedTarget]
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
        row.status.style.display = row.status.textContent ? "" : "none";
        if (row.editing) {
            rowActions(row);
            return;
        }
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
            row.mode.disabled = !editable(spec) || spec.seed_mode == null;
            row.mode.title = spec.seed_mode == null ? "This seed has no mode on this host." : "";
            row.mode.value = row.modeOptimistic ?? spec.seed_mode ?? "fixed";
        }
        if (row.note) {
            row.noteText = row.noteOptimistic ?? (spec.source === "note" ? spec.text : spec.note) ?? "";
            row.note.replaceChildren(renderPanelMarkdown(row.noteText));
            row.note.style.cursor = manageOn ? "text" : "default";
            row.note.title = manageOn ? "Double-click to edit" : "";
            if (!row.noteText) {
                // A standalone note is never empty in practice; a binding's empty
                // note takes no space (Manage adds one through its Note action).
                row.note.textContent = manageOn && spec.source === "note" ? "Double-click to write this note." : "";
                row.note.style.fontStyle = "italic";
                row.note.style.display = manageOn && spec.source === "note" ? "" : "none";
            } else {
                row.note.style.fontStyle = "normal";
                row.note.style.display = "";
            }
            if (spec.source !== "note") clampNote(row);
        }
        rowActions(row);
        styleRow(row, spec);
    }
    // In Cards, a Control's note shows two lines, with More/Less only when it
    // runs longer, and its plain text as the tooltip (#11). Rows and Manage show
    // it whole. The expanded state is ephemeral.
    function clampNote(row) {
        const note = row.note, style = note.style;
        const clamp = cards() && !!row.noteText;
        if (clamp && !row.noteMore) {
            row.noteMore = make("button");
            row.noteMore.type = "button";
            row.noteMore.dataset.noteMore = "true";
            row.noteMore.style.cssText = S.quietLineCss;
            row.noteMore.addEventListener("click", () => { row.noteExpanded = !row.noteExpanded; clampNote(row); });
            note.after(row.noteMore);
        }
        const folded = clamp && !row.noteExpanded;
        if (folded) {
            style.display = "-webkit-box";
            style.webkitLineClamp = "2";
            style.webkitBoxOrient = "vertical";
            style.overflow = "hidden";
        } else {
            if (style.display === "-webkit-box") style.display = "";
            style.webkitLineClamp = style.webkitBoxOrient = style.overflow = "";
        }
        if (clamp && !manageOn) note.title = note.textContent || "";
        if (!row.noteMore) return;
        row.noteMore.textContent = row.noteExpanded ? "Less" : "More";
        row.noteMore.setAttribute("aria-expanded", String(!!row.noteExpanded));
        row.noteMore.hidden = true;
        if (!clamp) return;
        // Shown only when the note overflows its two lines (or is expanded),
        // measured once per frame for every note: all reads, then all writes.
        noteChecks.add(row);
        noteFrame = noteFrame || requestAnimationFrame(checkNotes);
    }
    function checkNotes() {
        noteFrame = 0;
        const rowsToCheck = [...noteChecks];
        noteChecks.clear();
        // Rechecked here: a later render may have left Cards or cleared the note.
        const shown = rowsToCheck.map(row => !destroyed && cards() && !!row.noteText && !!row.noteMore
            && (row.noteExpanded || row.note.scrollHeight > row.note.clientHeight + 1));
        rowsToCheck.forEach((row, i) => { if (row.noteMore) row.noteMore.hidden = !shown[i]; });
    }
    function render(view = host.getView()) {
        if (destroyed) return;
        const fresh = view !== lastView;
        lastView = view;
        const current = settings();
        const scale = Number(current.layout?.scaleControls) || 1;
        container.style.zoom = scale !== 1 ? String(scale) : "";
        const filter = { query, showAdvanced: showAdvanced(), manage: manageOn };
        const allSpecs = [...(view.bindings || []).map(d => ({ ...d, source: "binding" })), ...(view.notes || []).map(d => ({ ...d, source: "note" }))];
        const empty = !allSpecs.some(d => manageOn || !d.hidden);
        restore.render(view);
        manage?.render(view);
        presets.render(view, manageOn);
        const entries = [];
        for (const spec of [...(view.bindings || []), ...(view.notes || [])]) {
            const source = spec.source || ("note_id" in spec ? "note" : "binding");
            const id = source === "note" ? spec.note_id : spec.binding_id;
            if (!id) continue;
            const target = source === "note" ? `note:${id}:text` : `binding:${id}:value`;
            entries.push({ target, spec: { ...spec, source }, group: spec.group || "" });
        }
        if (view.state === "ready") {
            // Every row of the view, displayed or not: a save on a row a search
            // or Advanced hides still reports its outcome.
            validTargets = new Set(
                entries.flatMap(({ target, spec }) => {
                    const base = baseOf(spec);
                    return [
                        target,
                        `${base}:order`,
                        `${base}:group`,
                        ...(spec.source === "binding"
                            ? [`${base}:note`, `${base}:label`, `${base}:advanced`,
                                ...(spec.kind === "SEED" ? [`${base}:seed_mode`] : [])]
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

        // Header: search (normal view only), the hidden-Advanced line.
        // Hidden, not removed: the header row keeps its height in Manage.
        search.style.visibility = manageOn ? "hidden" : "";
        const hidden = hiddenAdvanced(allSpecs, filter);
        const anyAdvanced = hasAdvanced(allSpecs);
        quietLine.textContent = "";
        if (filter.showAdvanced) quietLine.textContent = "Hide advanced settings";
        else if (hidden.count) {
            quietLine.append(make("span", `${hidden.count} advanced hidden`));
            if (hidden.changed) {
                const changed = make("span", ` · ${hidden.changed} changed`);
                changed.style.color = COLORS.warningBorder;
                quietLine.append(changed);
            }
        }
        setShown(quietLine, !manageOn && anyAdvanced && (filter.showAdvanced || hidden.count > 0));
        quietLine.title = filter.showAdvanced
            ? "Hide the settings this workflow marks Advanced (their values still apply)"
            : "Show the settings this workflow marks Advanced";
        quietLine.setAttribute("aria-pressed", String(filter.showAdvanced));

        // The Editor group: your settings first, then the workflow's Editor settings.
        const editorBody = editorGroup.content;
        let editorVisible = 0;
        for (const mirror of mirrors) {
            paintMirror(mirror);
            styleMirror(mirror);
            const shown = manageOn || matchEntry(mirror.spec, query);
            setShown(mirror.el, shown);
            if (shown) editorVisible++;
        }
        fieldsBody.style.cssText = cards() ? "display:contents;" : "display:flex;flex-direction:column;";

        workflowBody.style.cssText = S.bodyCss(cards());
        // The Main drop zone belongs to arranging, which is Manage only.
        mainDrop.style.display = manageOn ? "" : "none";
        const alive = new Set(),
            counts = new Map(),
            used = new Set(),
            placements = new Map(); // body -> its rows, in display order
        let shownTotal = 0;
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
            const visible = entryVisible({ ...spec, advanced: effectiveAdvanced(row) }, filter);
            setShown(row.el, visible || row.editing);
            if (visible) shownTotal++;
            let body = workflowBody;
            if (inEditor(spec)) {
                body = fieldsBody;
                if (visible) editorVisible++;
            } else if (group) {
                if (!groups.has(group)) groups.set(group, makeGroup(group, group));
                body = groups.get(group).content;
                used.add(group);
                if (visible && spec.source === "binding") counts.set(group, (counts.get(group) || 0) + 1);
                if (visible) counts.set(`${group}\u0000any`, true);
            }
            // A row being edited keeps its parent until the edit ends.
            if (!row.editing || row.el.parentElement === body) {
                if (!placements.has(body)) placements.set(body, []);
                placements.get(body).push(row.el);
            }
        }
        for (const [target, row] of rows)
            if (!alive.has(target) && !row.editing) {
                row.picker?.destroy();
                row.el.remove();
                rows.delete(target);
            }
        paintGroup(editorGroup, editorVisible);
        setShown(editorGroup.el, manageOn || editorVisible > 0);
        groupOrder = groupPlan(view, entries.map(e => e.spec));
        // Empty groups exist only to be filled, so they show only in Manage.
        const displayed = new Set(groupOrder.filter(name => manageOn || used.has(name)));
        for (const name of groupOrder) {
            if (!displayed.has(name)) continue;
            if (!groups.has(name)) groups.set(name, makeGroup(name, name));
            const group = groups.get(name);
            paintGroup(group, counts.get(name) || 0);
            paintGroupActions(group);
            setShown(group.el, manageOn || !!counts.get(`${name}\u0000any`));
        }
        for (const [body, els] of placements) if (body !== workflowBody) placeInOrder(body, els);
        placeInOrder(workflowBody, [mainDrop, ...(placements.get(workflowBody) || []),
            ...groupOrder.filter(name => displayed.has(name)).map(name => groups.get(name).el)]);
        for (const [name, group] of groups)
            if (!displayed.has(name) && !group.renaming && ![...rows.values()].some(r => r.editing && group.el.contains(r.el))) {
                group.el.remove();
                groups.delete(name);
            }
        message.textContent =
            view.error ||
            view.relay?.error ||
            (!empty
                ? query && !shownTotal && !editorVisible ? `No settings match “${query.trim()}”. Search looks at names, groups and choices.` : ""
                : view.state === "ready" ? host.emptyMessage || CANVAS_EMPTY : "Loading controls…");
    }
    function setManage(on) {
        if (destroyed || on === manageOn) return;
        if (restore.busy) return;
        if (!on && manage?.canLeave && !manage.canLeave()) return;
        if (!on) {
            for (const row of rows.values()) if (row.renameInput) closeRename(row, true);
            if (noteEditor) void closeNote(true);
        }
        manageOn = on;
        manageButton.textContent = on ? "Done" : "Manage";
        manageButton.setAttribute("aria-pressed", String(on));
        setButtonVariant(manageButton, on ? "accentSoft" : "secondary", { padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
        if (on) {
            manage = mountManageSection(host, { notify: warning, flushPending, changed: () => render(),
                track: (work, changes) => track(work, Object.keys(changes).some(changesRun)) });
            restore.el.after(manage.el);
        } else {
            manage?.destroy();
            manage = null;
        }
        host.setManage?.(on, "");
        render();
    }
    manageButton.addEventListener("click", () => setManage(!manageOn));
    search.addEventListener("input", () => {
        query = search.value;
        render();
    });
    const unregister = register({
        id: `controls-values:${panelId}`,
        priority: PRIORITY.TEXT_EDITOR,
        keydown: e => {
            if (document.activeElement === search) {
                if (e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
                if (e.key === "Escape") {
                    if (search.value) {
                        search.value = "";
                        query = "";
                        render();
                    } else search.blur();
                    return true;
                }
                return PRESERVE_DEFAULT;
            }
            const mirror = mirrors.find(m => m.input === document.activeElement && m.input.type !== "checkbox");
            if (mirror) {
                if (e.key === "Escape") {
                    mirror.editing = false;
                    paintMirror(mirror);
                    mirror.input.blur();
                    return true;
                }
                if (e.key === "Enter") {
                    mirror.input.blur();
                    return true;
                }
                return PRESERVE_DEFAULT;
            }
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
    // Saves the focused field or note, then waits for every save. Only a
    // run-value save still in flight now can refuse the caller's run: an
    // older refusal already reverted its row to the stored value. Saves
    // started after the call (a tracked preset apply) are not awaited.
    async function flushPending() {
        // Snapshot before awaiting anything: a run save that settles while
        // the note saves was still in flight when the caller asked.
        const blocking = new Set(runPending);
        const closing = noteEditor ? closeNote(true) : null;
        const row = [...rows.values()].find(row => row.input === document.activeElement);
        if (row && !row.picker) {
            const saving = commitValue(row);
            if (changesRun(row.target)) blocking.add(saving);
        }
        const mirror = mirrors.find(m => m.input === document.activeElement);
        if (mirror) mirror.input.blur();
        const savingNow = [...pending];
        await closing;
        await Promise.all(savingNow);
        return (await Promise.all(blocking)).every(Boolean);
    }
    const unsubscribe = host.subscribe(view => render(view));
    render();
    return {
        render,
        flushPending,
        destroy() {
            if (noteEditor) void closeNote(false);
            for (const group of groups.values()) group.renaming?.close(false);
            if (manageOn) host.setManage?.(false, "");
            manage?.destroy();
            destroyed = true;
            container.removeEventListener("mouseover", valueTitle);
            arrangement.destroy();
            presets.destroy();
            restore.destroy();
            for (const row of rows.values()) row.picker?.destroy();
            unsubscribe?.();
            unregister();
            rows.clear();
            groups.clear();
            container.replaceChildren();
        },
    };
}
