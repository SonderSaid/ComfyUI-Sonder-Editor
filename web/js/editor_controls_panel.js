import { mountRestore } from "./controls_restore.js";
import { mountWorkflowHeader } from "./controls_workflow_header.js";
import { mountModes } from "./controls_modes.js";
import { VISUALLY_HIDDEN_CSS, describedBy, placeInOrder, setText } from "./controls_dom_order.js";
import { mountComboPicker, wantsComboPicker } from "./controls_combo_picker.js";
import { mountRowsEditor, NO_ROWS_NOTICE } from "./controls_rows_editor.js";
import { mountExecutionRow, switchScope } from "./controls_execution_row.js";
import { mountDriverLanePicker, mountReferenceLanePicker, normalizeAssetFolder } from "./controls_lane_pickers.js";
import { mountManageSection, smallButton } from "./controls_manage.js";
import { createHoverTip } from "./controls_hover_tip.js";
import { mountPanelArrangement } from "./controls_panel_arrangement.js";
import { EDITOR_PARTITION, comparePanelRows, canArrange, planPanelMove } from "./controls_panel_layout.js";
import { moveToGroupItems, openGroupMenu, startGroupRename } from "./controls_panel_groups.js";
import { changesRun } from "./control_targets.js";
import { openContextMenu } from "./editor_context_menu.js";
import { disclosureKey, entryVisible, groupPlan, hasAdvanced, hasNotes, hiddenAdvanced, hiddenNotes, matchEntry, noteDisclosureKey, noteShown } from "./controls_panel_view.js";
import * as S from "./controls_panel_style.js";
import { renderPanelMarkdown, splitPanelMarkdownTitle } from "./panel_markdown.js";
import { createDisclosureMemory, createValueMemory } from "./disclosure_memory.js";
import { CANVAS_INTEGER_CEILING, intStep, seedBounds, stepSeed } from "./controls_seed_step.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE, applyNativeControlTheme, setButtonDisabled, setButtonVariant } from "./editor_theme.js";

let nextPanel = 0;
let nextDescription = 0;
const SEED_MODES = ["fixed", "increment", "decrement", "randomize"];
const editable = d => d.status === "ok" && !d.read_only;
const identified = d => !!(d.binding_id || d.note_id || d.execution_id);
const baseOf = d => (d.binding_id ? "binding:" + d.binding_id : d.note_id ? "note:" + d.note_id
    : d.execution_id ? "execution:" + d.execution_id : "");
// Settings and node switches share their row actions: rename, note, Advanced, group.
const isControl = d => d.source === "binding" || d.source === "execution";
const sourceOf = d => d.source || ("note_id" in d ? "note" : "execution_id" in d ? "execution" : "binding");
// Why a setting does not run now: its node, or a subgraph holding it, is off.
// Its value is kept and still editable.
function participationNote(spec) {
    if (spec.source !== "binding" || !["inactive", "partial"].includes(spec.participation)) return "";
    const reason = spec.inactive_reason || {};
    const mode = reason.mode === "bypassed" ? "bypassed" : "muted";
    const who = reason.by === "parent" && reason.label ? `“${reason.label}” is ${mode}` : `its node is ${mode}`;
    return spec.participation === "partial" ? `Partly off: ${who}. Its value is kept.`
        : `Off: ${who}. Its value is kept and applies once it runs again.`;
}
const inEditor = d => d.source === "binding" && d.section === "editor";
const CANVAS_EMPTY = "No Controls yet. Choose Manage to expose this workflow's settings here.";
const CANVAS_DISCONNECTED = "The canvas is not connected. Your Controls come back when the ComfyUI tab with this workflow is open.";
const EMPTY_NOTE = "Double-click to write this note. Start it with a bold line to make it collapsible.";
const LONG_VALUE = 18; // a choice longer than this gets a wide card
// A burst of seed steps saves once, this long after the last step.
const SEED_STEP_IDLE_MS = 400;
// A remembered text box height is kept within these bounds (CSS px).
const TEXT_HEIGHT_MIN = 60;
const TEXT_HEIGHT_MAX = 1200;
const clampTextHeight = value => Math.min(TEXT_HEIGHT_MAX, Math.max(TEXT_HEIGHT_MIN, Math.round(value)));
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

export function mountControlsPanel(container, host) {
    const panelId = ++nextPanel,
        rows = new Map(),
        groups = new Map(),
        pending = new Set(),
        runPending = new Set();
    const disclosure = createDisclosureMemory("sonder-controls-disclosure-v1");
    // A resized text box's height, per Control, in this browser.
    const textHeights = createValueMemory("sonder-controls-text-height-v1");
    let destroyed = false,
        lastView = null,
        noteEditor = null,
        manageOn = false,
        manage = null,
        query = "",
        groupOrder = []; // every workflow group, in display order (Manage shows empty ones)
    const capabilities = () => host.capabilities || { hide: false, removeAuthored: true };
    // The host's lane and folder choices (controls_picker_sources.js); absent on
    // hosts without a project, where pickers fall back to plain fields.
    const pickers = host.pickers || null;
    // Which editor a Control's row uses. One predicate decides build, update and
    // the rebuild check, so a picker hint moving between host and other rebuilds.
    // A picker shows only for its own project (`source: "host"`); otherwise the
    // plain field for the kind, with a note saying why.
    function editorKind(spec) {
        if (spec.source === "note") return "note";
        if (spec.source === "execution") return "execution";
        if (spec.kind === "ROWS") return "rows";
        const picker = spec.picker;
        if (pickers && picker?.source === "host") {
            if (picker.kind === "reference_lanes" && spec.kind === "STRING") return "reference_lanes";
            if (picker.kind === "driver_lane" && spec.kind === "INT") return "driver_lane";
            if (picker.kind === "asset_folder" && spec.kind === "COMBO") return "asset_folder";
        }
        if (spec.kind === "COMBO" && wantsComboPicker(spec)) return "combo";
        // A combo its node checks at run, with no options to list, takes typed text.
        if (spec.kind === "COMBO" && spec.checked_at_run && !(spec.options || []).length) return "text";
        return "plain";
    }
    function pickerNote(spec) {
        if (!spec.picker || !["plain", "text"].includes(editorKind(spec))) return "";
        const what = spec.picker.kind === "asset_folder" ? "Folder suggestions" : "Lane names";
        return spec.picker.source === "other"
            ? `Wired to another Sonder Editor, so ${what.toLowerCase()} aren't listed here.`
            : `${what} are unavailable here.`;
    }
    const pickerChoices = kind => pickers?.get(kind) || null;
    // The sources read only for kinds this view shows. Declared after the render
    // that found them: a source's synchronous "loading" notice must not re-enter it.
    let declaredPickers = "";
    function declarePickerInterest(kinds) {
        const key = [...kinds].sort().join(",");
        if (!pickers || key === declaredPickers) return;
        declaredPickers = key;
        queueMicrotask(() => { if (!destroyed) pickers.setInterest(kinds); });
    }
    const unsubscribePickers = pickers?.subscribe?.(() => { if (!destroyed) render(); });
    const settings = () => host.getEditorSettings?.() || {};
    const cards = () => !manageOn && settings().controls?.view === "cards";
    const showAdvanced = () => settings().controls?.showAdvanced === true;
    const showNotes = () => settings().controls?.showNotes !== false;
    // Whether a displayed Control shows its note now (Show notes, search, Manage).
    const notesShown = () => noteShown({ query, showNotes: showNotes(), manage: manageOn });
    // Target -> reason of its latest refused or unconfirmed save, shown on the row
    // until a newer save of it succeeds or an authoritative view arrives.
    const failed = new Map();
    // Cards whose note needs its More/Less overflow check on the next frame.
    const noteChecks = new Set();
    let noteFrame = 0;
    let validTargets = null,
        layoutSaving = false;
    container.dataset.sonderControlsPanel = "true";
    // A node's own help on its setting: the label on hover, the value control on
    // keyboard focus (plans/live-workflow-phase-4-controls-modes.md D6).
    const tips = createHoverTip(container);
    // A stable gutter: Manage's longer list adding a scrollbar never shifts the header.
    container.style.cssText = `display:flex;flex-direction:column;flex:1;min-height:0;overflow:auto;scrollbar-gutter:stable;padding:${SPACE.s3}px;gap:${SPACE.s2}px;font:${TYPE.t12}px ${FONT.sans};color:${COLORS.text};`;
    applyNativeControlTheme(container);

    // Header: search, a discreet Manage toggle, and the Advanced and notes lines.
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
    // Kept spans written in place: a replaced text node under the pointer drops its click.
    const quietMain = make("span"), quietChanged = make("span");
    quietChanged.style.color = COLORS.warningBorder;
    quietLine.append(quietMain, quietChanged);
    const notesLine = make("button");
    notesLine.type = "button";
    notesLine.dataset.controlsNotesLine = "true";
    notesLine.style.cssText = S.quietLineCss;
    notesLine.addEventListener("click", () => writeSetting("controls.showNotes", !showNotes()));
    // Both lines share one row, so turning on Advanced adds no second line of chrome.
    const quietRow = make("div");
    quietRow.style.cssText = `flex-wrap:wrap;align-items:baseline;column-gap:${SPACE.s3}px;row-gap:${SPACE.s1}px;`;
    quietRow.append(quietLine, notesLine);
    const workflowHeader = mountWorkflowHeader(host, {flushPending: () => flushPending(), notify: reason => warning(reason),
        // Asked before the switch saves anything, and again after: a choice
        // still loading or a Manage save still running refuses the switch.
        busyReason: () => (modes.busy ? "Wait for the choice to finish loading, then switch workflows."
            : restore.busy || manage?.modesSaving ? "Wait for the Manage change to save, then switch workflows." : ""),
        // Switching workflows remounts the panel: Manage drafts would be lost.
        canSwitch: () => {
            return !manage || (manage.canLeave() && (!manage.hasDraft
                || globalThis.confirm("Discard your unsaved Manage edits and switch workflows?")));
        }});
    // Modes above the Individual Controls; a choice applies through the host.
    const modes = mountModes(host, {flushPending: () => flushPending(), track, notify: (reason, detail) => warning(reason, detail),
        inform: message => host.inform?.(message),
        openManage: () => { setManage(true); manage?.revealModes(); }});
    header.append(headerTop);
    container.append(header);
    header.after(workflowHeader.el);
    workflowHeader.el.after(modes.el);
    modes.el.after(quietRow);
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
    const warning = (reason, detail) => host.notify?.(reason || "The change could not be saved.", detail);

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
        setText(group.toggle, open ? "v" : ">");
        group.toggle.title = `${open ? "Collapse" : "Expand"} ${group.title.textContent}`;
        group.toggle.setAttribute("aria-expanded", String(open));
        group.content.style.cssText = S.groupContentCss(cards());
        setShown(group.content, open);
        setText(group.count, String(visibleCount));
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
        return [...(view.bindings || []).map(d => ({ ...d, source: "binding" })), ...(view.notes || []),
            ...(view.execution || []).map(d => ({ ...d, source: "execution" }))]
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
                // A row list keeps its own copy while a field has focus; the
                // next edit's base must be the stored rows, not the refused ones.
                if (row.editor && target === row.target) row.editor.reset(row.spec.value);
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
    // ── Seed steps ──────────────────────────────────────────────────────────
    // The seed's up and down buttons (and ArrowUp/ArrowDown in its field).
    // Each step shows at once in the field; a burst saves once, after a short
    // pause, and flushPending saves a waiting step before any run.
    // A canvas widget holds a JS number, so a canvas host steps no higher than
    // 2^53 - 1; a host that keeps big integers exact (the Sonder App) does.
    const seedCeiling = () => (capabilities().exactIntegers ? null : CANVAS_INTEGER_CEILING);
    function seedStepButton(row, direction) {
        const button = make("button", direction > 0 ? "▴" : "▾");
        button.type = "button";
        button.dataset.seedStep = direction > 0 ? "up" : "down";
        button.style.cssText = direction > 0 ? S.seedStepUpCss : S.seedStepDownCss;
        // A press keeps the field's focus, so typing carries on after a step.
        button.addEventListener("mousedown", e => e.preventDefault());
        button.addEventListener("click", () => stepRow(row, direction));
        return button;
    }
    function stepRow(row, direction) {
        if (!row.input || row.input.disabled || !editable(row.spec)) return false;
        const next = stepSeed(readInput(row), direction, seedBounds(row.spec, seedCeiling()));
        if (next === null) return false;
        // The burst saves against the value stored before its first step, not
        // `editStart`: a focus in the burst, or a seed changed elsewhere since
        // the field was last focused, would make the step look like no change.
        if (!row.seedStepTimer) row.seedStepBase = row.optimistic ?? row.spec.value;
        setInput(row, next);
        clearTimeout(row.seedStepTimer);
        row.seedStepTimer = setTimeout(() => void flushSeedStep(row), SEED_STEP_IDLE_MS);
        paintSeedSteps(row);
        return true;
    }
    function flushSeedStep(row) {
        if (!row.seedStepTimer) return null;
        clearTimeout(row.seedStepTimer);
        row.seedStepTimer = 0;
        if (row.suppress || !row.input || row.input.disabled) return null;
        const value = readInput(row), before = row.seedStepBase;
        if (same(value, before)) return null;
        row.editStart = value;
        return commit(row, row.target, value, before);
    }
    // Typing during a burst replaces the step: the typed text saves the usual
    // way (change, Enter, blur or a run), never half-typed from the timer.
    function cancelSeedStep(row) {
        if (!row.seedStepTimer) return;
        clearTimeout(row.seedStepTimer);
        row.seedStepTimer = 0;
        row.editStart = row.seedStepBase;
    }
    function paintSeedSteps(row) {
        const label = row.labelOptimistic || row.spec.label || "seed";
        const can = !!row.input && !row.input.disabled && editable(row.spec);
        const bounds = seedBounds(row.spec, seedCeiling()), text = can ? readInput(row) : "";
        for (const [button, direction, verb] of [[row.seedUp, 1, "Increase"], [row.seedDown, -1, "Decrease"]]) {
            setButtonDisabled(button, !can || stepSeed(text, direction, bounds) === null);
            button.setAttribute("aria-label", `${verb} ${label}`);
            button.title = `${verb} ${label}`;
        }
    }
    // ── Text box heights ────────────────────────────────────────────────────
    // A multiline text box keeps the height it was dragged to, per Control,
    // in this browser (presentation only). The browser's resize handle writes
    // the inline height; styleRow puts the remembered one back on every render
    // except while a drag is in progress.
    function watchTextHeight(row, bindingId) {
        row.heightKey = `text:${bindingId}`;
        row.input.addEventListener("pointerdown", () => {
            row.resizing = true;
            // A release anywhere ends the drag, also outside the box.
            (globalThis.window || globalThis).addEventListener?.("pointerup", () => {
                row.resizing = false;
                rememberTextHeight(row);
            }, { once: true });
        });
        if (globalThis.ResizeObserver) {
            row.resizeObserver = new ResizeObserver(() => rememberTextHeight(row));
            row.resizeObserver.observe(row.input);
        }
    }
    function rememberTextHeight(row) {
        if (destroyed || !row.heightKey || !row.input) return;
        const height = parseFloat(row.input.style.height);
        if (!Number.isFinite(height)) return;
        const next = clampTextHeight(height);
        if (next !== textHeights.value(row.heightKey)) textHeights.remember(row.heightKey, next);
    }
    function rememberedTextHeight(row) {
        const height = row.heightKey ? textHeights.value(row.heightKey) : null;
        return Number.isFinite(height) ? `${clampTextHeight(height)}px` : "";
    }
    // Structural Manage actions (remove, hide, unhide) on one row.
    function structural(changes, button) {
        const affected = (host.getView().presets || []).filter(p => p.members.some(m => `binding:${m.binding_id}:remove` in changes)
            || (p.execution || []).some(m => `execution:${m.execution_id}:remove` in changes));
        if (button) button.disabled = true;
        const work = Promise.resolve()
            .then(() => host.applyChanges(changes))
            .catch(error => ({ ok: false, rejected: [{ reason: error?.message }] }))
            .then(result => {
                if (destroyed) return false;
                if (button) button.disabled = false;
                if (!result?.ok) warning(result?.rejected?.[0]?.reason);
                else if (affected.length) warning(`Removed Control is still in these choices: ${affected.map(p => p.name).join(", ")}. Repair them in Manage.`);
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
        if (save && next !== before) void commit(row, `${baseOf(row.spec)}:label`, next, before);
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
        const advanced = isControl(spec) && effectiveAdvanced(row);
        const key = JSON.stringify([manageOn, local, !!spec.hidden, !!spec.read_only, spec.status, spec.source, spec.section, caps, advanced, !!row.noteText]);
        if (row.actionsKey === key) return;
        row.actionsKey = key;
        const items = [];
        if (spec.read_only) {
            // A switch this app can't route is switched and arranged on the
            // canvas; one hidden earlier can still be shown again.
            const unhide = spec.hidden && caps.hide ? smallButton("Unhide") : null;
            unhide?.addEventListener("click", () => void structural({ [`${baseOf(spec)}:unhide`]: true }, unhide));
            box.replaceChildren(marker("Canvas only", "Switch and arrange node switches on the canvas."), ...(unhide ? [unhide] : []));
            return;
        }
        if (local) items.push(marker("App only", "Added in the Sonder App; not part of the workflow file."));
        if (spec.hidden) items.push(marker("Hidden", "Not shown or applied in this app."));
        if (isControl(spec)) {
            const rename = smallButton("Rename");
            rename.addEventListener("click", () => openRename(row));
            items.push(rename);
            const note = smallButton(row.noteText ? "Edit note" : "Note");
            note.title = spec.source === "execution" ? "A Markdown note shown under this switch" : "A Markdown note shown under this setting";
            note.addEventListener("click", () =>
                openNote(row, row.note, `${baseOf(spec)}:note`, row.noteText, identified(spec) && !spec.note_truncated));
            items.push(note);
            const flag = smallButton(advanced ? "Advanced ✓" : "Advanced", advanced ? "accentSoft" : "secondary");
            flag.dataset.controlsAdvancedToggle = spec.binding_id || spec.execution_id;
            flag.setAttribute("aria-pressed", String(advanced));
            flag.title = advanced
                ? "Shown only when Show advanced is on; its value still applies"
                : "Hide it unless Show advanced is on; its value still applies";
            flag.addEventListener("click", () => {
                const next = !effectiveAdvanced(row);
                void commit(row, `${baseOf(spec)}:advanced`, next, !next);
                render();
            });
            items.push(flag);
            if (!inEditor(spec)) items.push(groupButton(row));
            if (spec.status !== "ok" && spec.status !== "shadowed" && (caps.removeAuthored || local)) {
                const rebind = smallButton("Rebind");
                rebind.title = spec.source === "execution" ? "Point this switch at another node or group, keeping its id"
                    : "Bind this Control to another setting, keeping its id";
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
        edit.row.noteRendered = null; // the editor took the box: render it again
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
        // A collapsed titled note opens whole: the title line is part of the text.
        box.hidden = false;
        box.style.display = "";
        if (row.noteHeader) row.noteHeader.style.display = "none";
        if (row.noteFrame) row.noteFrame.style.display = "flex";
        box.replaceChildren(area, done);
        row.noteRendered = null;
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
        box.style.cssText = standalone ? S.standaloneNoteBodyCss : S.noteCss;
        // Notes are authored in Manage; normal view shows them rendered.
        box.addEventListener("dblclick", () =>
            openNote(row, box, target, row.noteText, manageOn && identified(row.spec) && !row.spec.note_truncated),
        );
        return box;
    }
    // A standalone note's strip: a title header when the note starts with a
    // heading or a bold line (splitPanelMarkdownTitle), then its body. The
    // header sits outside the box openNote replaces with its editor.
    function buildNoteFrame(row, target) {
        row.noteFrame = make("div");
        row.noteFrame.style.cssText = S.standaloneNoteCss;
        row.noteHeader = make("div");
        row.noteHeader.dataset.noteHeader = "true";
        row.noteHeader.style.cssText = S.noteHeaderCss;
        row.noteToggle = make("button");
        row.noteToggle.type = "button";
        row.noteToggle.dataset.noteToggle = "true";
        row.noteToggle.style.cssText = S.groupToggleCss;
        row.noteTitle = make("span");
        row.noteTitle.style.cssText = S.noteTitleCss;
        row.noteHeader.append(row.noteToggle, row.noteTitle);
        row.noteFrame.append(row.noteHeader, row.note);
        row.noteOpen = disclosure.isOpen(noteDisclosureKey(row.spec.note_id), true);
        row.noteToggle.addEventListener("click", () => flipNote(row));
        // The whole header toggles outside Manage. A double-click's second
        // click is ignored, so it never flips twice; in Manage it edits.
        row.noteHeader.addEventListener("click", event => {
            if (event.target === row.noteToggle || manageOn || (event.detail ?? 1) > 1) return;
            flipNote(row);
        });
        row.noteTitle.addEventListener("dblclick", () =>
            openNote(row, row.note, target, row.noteText, manageOn && identified(row.spec) && !row.spec.note_truncated));
    }
    function flipNote(row) {
        if (row.editing) return;
        row.noteOpen = !row.noteOpen;
        disclosure.remember(noteDisclosureKey(row.spec.note_id), row.noteOpen);
        paintStandaloneNote(row);
    }
    function paintStandaloneNote(row) {
        const text = row.noteText;
        const split = text ? splitPanelMarkdownTitle(text) : null;
        const open = !split || row.noteOpen;
        row.note.hidden = false;
        row.note.style.cursor = manageOn ? "text" : "default";
        row.note.title = manageOn ? "Double-click to edit" : "";
        if (text) {
            // Rebuilt only when the text changes: a link or a double-click
            // mid-gesture loses its target when the box is replaced.
            const body = split ? split.body : text;
            if (row.noteRendered !== body) row.note.replaceChildren(renderPanelMarkdown(body));
            row.noteRendered = body;
            row.note.style.fontStyle = "normal";
        } else {
            // A standalone note is never empty in practice; Manage invites one.
            setText(row.note, manageOn ? EMPTY_NOTE : "");
            row.noteRendered = null;
            row.note.style.fontStyle = "italic";
        }
        row.note.style.display = open ? "" : "none";
        row.noteFrame.style.display = text || manageOn ? "flex" : "none";
        row.noteHeader.style.display = split ? "flex" : "none";
        if (!split) return;
        setText(row.noteTitle, split.title);
        row.noteTitle.title = manageOn ? "Double-click to edit" : "";
        row.noteTitle.style.cursor = manageOn ? "text" : "pointer";
        row.noteHeader.style.cursor = manageOn ? "default" : "pointer";
        setText(row.noteToggle, open ? "v" : ">");
        row.noteToggle.title = `${open ? "Collapse" : "Expand"} ${split.title}`;
        row.noteToggle.setAttribute("aria-expanded", String(open));
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
        row.editorKind = editorKind(spec);
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
            buildNoteFrame(row, target);
            row.el.append(row.noteFrame);
        } else {
            row.line = make("div");
            row.label = make("label");
            row.labelText = make("span", spec.label);
            row.labelText.style.cssText = S.labelTextCss;
            row.tags = make("span");
            row.tags.style.cssText = `display:inline-flex;align-items:center;gap:${SPACE.s1}px;flex:0 0 auto;`;
            // Kept and updated in place: the label is pressable, and a node
            // replaced under the pointer drops the click.
            row.tagAdv = make("span", "Adv");
            row.tagAdv.title = "Advanced: shown only when Show advanced is on; its value still applies";
            row.tagAdv.style.cssText = S.tagCss;
            row.tagOff = make("span");
            row.tagOff.style.cssText = S.tagCss;
            row.tagDot = make("span");
            row.tagDot.title = "Differs from the workflow's own value";
            row.tagDot.style.cssText = S.changedDotCss;
            row.tags.append(row.tagAdv, row.tagOff, row.tagDot);
            row.label.append(row.labelText, row.tags);
            // Why the setting is off, for screen readers; outside the label so
            // it never joins the setting's name.
            row.offReason = make("span");
            row.offReason.id = `controls-${panelId}-off-${++nextDescription}`;
            row.offReason.style.cssText = VISUALLY_HIDDEN_CSS;
            row.control = make("div");
            const onEditing = on => {
                row.editing = on;
                if (!on) render();
            };
            if (row.editorKind === "execution") {
                row.editor = mountExecutionRow({ commit: (next, before) => commit(row, row.target, next, before) });
            } else if (row.editorKind === "rows") {
                // A row list edits as a whole; each edit carries the list it was made from.
                row.editor = mountRowsEditor({
                    commit: (next, before) => commit(row, row.target, { rows: next, base: before }, before),
                    onEditing,
                    attachTip: (el, getText, options = {}) => tips.attach(el, getText, { ...options, anchor: row.el }),
                });
            } else if (row.editorKind === "reference_lanes") {
                row.editor = mountReferenceLanePicker({ commit: (next, before) => commit(row, row.target, next, before), onEditing });
            } else if (row.editorKind === "driver_lane") {
                row.editor = mountDriverLanePicker({ commit: (next, before) => commit(row, row.target, next, before), onEditing });
            } else if (row.editorKind === "combo" || row.editorKind === "asset_folder") {
                const folder = row.editorKind === "asset_folder";
                row.picker = mountComboPicker({
                    inputCss: S.inputCss,
                    // A folder is any label: what you type is the value.
                    freeText: folder,
                    normalize: folder ? normalizeAssetFolder : undefined,
                    labels: folder ? { "": "Root" } : {},
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
                if (folder) row.input.placeholder = "Root";
            } else {
                row.input = make(
                    row.editorKind === "text" ? "input"
                        : spec.kind === "COMBO" ? "select" : spec.kind === "STRING" && spec.multiline ? "textarea" : "input",
                );
            }
            if (row.editor) row.control.append(row.editor.el);
            // A dedicated editor has no single input: it owns its fields and listeners.
            if (!row.editor) {
                if (row.input.tagName === "INPUT" && !row.picker) {
                    row.input.type =
                        spec.kind === "BOOLEAN" ? "checkbox" : ["INT", "FLOAT"].includes(spec.kind) ? "number" : "text";
                }
                if (spec.kind === "SEED") row.input.inputMode = "numeric";
                row.input.dataset.sonderTextUndo = "true";
                row.input.setAttribute("aria-label", spec.label || "Control value");
                row.input.id = `controls-${panelId}-${rows.size}`;
                row.label.htmlFor = row.input.id;
                if (row.input.tagName === "SELECT") {
                    for (const option of spec.options || []) row.input.append(new Option(String(option), String(option)));
                }
                if (spec.min != null) row.input.min = String(spec.min);
                if (spec.max != null) row.input.max = String(spec.max);
                // The node's declared step: the input steps from `min`, as the
                // canvas widget rounds (frontend 1.57 `useIntWidget`).
                if (spec.kind === "INT") row.input.step = intStep(spec);
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
                if (row.input.tagName === "TEXTAREA" && spec.binding_id) watchTextHeight(row, spec.binding_id);
            }
            if (spec.kind === "SEED" && row.input && !row.picker) {
                row.seedSteps = make("span");
                row.seedSteps.style.cssText = S.seedStepsCss;
                row.seedUp = seedStepButton(row, 1);
                row.seedDown = seedStepButton(row, -1);
                row.seedSteps.append(row.seedUp, row.seedDown);
                row.control.append(row.seedSteps);
                row.input.addEventListener("input", () => {
                    cancelSeedStep(row);
                    paintSeedSteps(row);
                });
            }
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
            // The node's help, for screen readers, beside why it is off.
            row.help = make("span");
            row.help.id = `controls-${panelId}-help-${++nextDescription}`;
            row.help.style.cssText = VISUALLY_HIDDEN_CSS;
            row.el.append(row.line, row.offReason, row.help);
            // Hovering the label shows its help; focusing the value from the
            // keyboard shows it too, with why the setting is off. Never over
            // an open option list.
            row.tipDetach = [tips.attach(row.label, () => labelTip(row), { anchor: row.el })];
            for (const target of valueTargets(row)) {
                row.tipDetach.push(tips.attach(target, () => focusTip(row), { anchor: row.el, focusOnly: true,
                    suppressed: () => !!row.picker?.open || !!row.editor?.open }));
            }
            row.note = noteBox(row, `${baseOf(spec)}:note`, false);
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
        // A row list stacks under its label and takes a full-width card. So does
        // a node switch: its three choices then fit one equal row at most widths.
        const stacked = (spec.kind === "STRING" && !!spec.multiline) || spec.kind === "ROWS"
            || row.editorKind === "reference_lanes" || row.editorKind === "execution";
        // A Control's note no longer widens its card (#11): it clamps inside it.
        const wide = spec.source === "note" || stacked
            || (spec.kind === "COMBO" && String(value ?? "").length > LONG_VALUE);
        row.el.style.cssText = S.rowCss({ cards: isCards, wide });
        if (manageOn && row.grip) row.el.style.paddingRight = `${SPACE.s5}px`;
        if (!row.line) return;
        const inline = spec.kind === "BOOLEAN";
        // A seed's field, steps and mode don't fit beside a label in a narrow
        // sidebar (the number shrank to a sliver): its label goes above them.
        row.line.style.cssText = S.lineCss({ cards: isCards, stacked: stacked || spec.kind === "SEED", inline });
        row.label.style.cssText = S.labelCss(isCards);
        row.control.style.cssText = S.controlCss({ cards: isCards, alignEnd: true });
        if (!row.input) return;
        const draggedHeight = row.input.style.height;
        if (spec.kind === "BOOLEAN") row.input.style.cssText = "margin:0;cursor:pointer;";
        else if (["INT", "FLOAT"].includes(spec.kind)) row.input.style.cssText = S.numberInputCss(isCards);
        else if (spec.kind === "SEED") row.input.style.cssText = S.seedInputCss;
        else row.input.style.cssText = S.inputCss + (row.input.tagName === "SELECT" ? "cursor:pointer;" : "")
            + (stacked ? `min-height:${TEXT_HEIGHT_MIN}px;resize:vertical;` : "");
        // The cssText above drops the height a user dragged to; put back the
        // remembered one, or keep the live one while the drag is still going.
        if (row.heightKey) row.input.style.height = row.resizing ? draggedHeight : rememberedTextHeight(row);
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
        const advanced = effectiveAdvanced(row);
        setShown(row.tagAdv, advanced && (showAdvanced() || query || manageOn));
        const off = spec.source === "binding" && ["inactive", "partial"].includes(spec.participation);
        const mode = spec.inactive_reason?.mode === "bypassed" ? "Bypassed" : "Muted";
        const reason = off ? participationNote(spec) : "";
        setText(row.tagOff, off ? (spec.participation === "partial" ? "Partly off" : mode) : "");
        row.tagOff.title = reason;
        if (off) row.tagOff.dataset.controlsInactive = spec.participation;
        else delete row.tagOff.dataset.controlsInactive;
        setShown(row.tagOff, off);
        setShown(row.tagDot, advanced && !!spec.overridden);
        setText(row.offReason, reason);
        const tooltip = spec.source === "binding" ? spec.tooltip || "" : "";
        setText(row.help, tooltip);
        for (const target of valueTargets(row)) {
            describedBy(target, row.offReason.id, off);
            describedBy(target, row.help.id, !!tooltip);
        }
    }
    // Where a row's value is edited: its input (and a seed's mode), or a
    // dedicated editor's focusable parts.
    function valueTargets(row) {
        return (row.input ? [row.input, row.mode] : row.editor?.describeTargets || []).filter(Boolean);
    }
    function labelTip(row) {
        const tooltip = row.spec.source === "binding" ? row.spec.tooltip : "";
        return tooltip ? `${row.labelText.textContent} — ${tooltip}` : "";
    }
    function focusTip(row) {
        const tooltip = row.spec.source === "binding" ? row.spec.tooltip : "";
        return [tooltip, row.offReason?.textContent].filter(Boolean).join("\n");
    }
    function dropRow(row) {
        clearTimeout(row.seedStepTimer);
        row.seedStepTimer = 0;
        row.resizeObserver?.disconnect();
        for (const detach of row.tipDetach || []) detach();
        row.picker?.destroy();
        row.editor?.destroy();
        row.el.remove();
    }
    function update(row, spec, fresh) {
        row.spec = spec;
        if (!row.presetMember) { row.presetMember = make("div"); row.el.prepend(row.presetMember); }
        const membership = manage?.memberControl(spec, row.presetMember.children[0]) || null;
        if (row.presetMember.children[0] !== membership) row.presetMember.replaceChildren(...(membership ? [membership] : []));
        if (row.grip) {
            const base = baseOf(spec);
            const allowed = manageOn && !spec.read_only && canArrange({ ...spec, base });
            row.grip.draggable = allowed;
            row.grip.hidden = !allowed;
            if (allowed) row.el.dataset.panelBase = base;
            else delete row.el.dataset.panelBase;
        }
        // The status line is not an input, so it stays current while the row is edited.
        const base = baseOf(spec);
        const noteTarget = spec.source === "note" ? row.target : `${base}:note`;
        const modeTarget = `${base}:seed_mode`;
        const labelTarget = `${base}:label`;
        const advancedTarget = `${base}:advanced`;
        if (fresh && !row.mutations.get(advancedTarget)?.pending) row.advancedOptimistic = undefined;
        if (row.label) {
            const label = row.labelOptimistic || spec.label || "";
            setText(row.labelText, label);
            // The help tip names the label itself: one tooltip, not two.
            if (spec.tooltip) row.label.removeAttribute("title");
            else row.label.title = label;
            row.input?.setAttribute("aria-label", label || "Control value");
            paintTags(row, spec);
        }
        if (row.mode) row.mode.setAttribute("aria-label", (spec.label || "Seed") + " mode");
        // A canvas without row widgets shows its notice in place of the rows,
        // so the server's "add at least 1" error would only repeat it.
        const rowsNotice = spec.kind === "ROWS" && spec.rows_unavailable && !capabilities().serverRows;
        const failure = [row.target, modeTarget, noteTarget, labelTarget, advancedTarget]
            .map(target => failed.get(target))
            .find(Boolean);
        // Why a setting is off is on its pill (title and description), not repeated here.
        const status = failure
            ? `Not saved: ${failure}`
            : spec.note_truncated
              ? "Edit this longer note on the canvas."
              : spec.reason ||
                (rowsNotice ? "" : spec.error) ||
                (spec.source === "execution" ? switchScope(spec) : "") ||
                pickerNote(spec) ||
                // A folder is any label by design; the run-time check says nothing new.
                (spec.checked_at_run && !spec.picker ? "Checked by the node when the run starts." : "");
        setText(row.status, status);
        row.status.style.color = failure ? COLORS.warningText : COLORS.textDim;
        row.status.style.display = status ? "" : "none";
        if (row.editing) {
            // An authoritative view seen mid-edit still retires settled
            // optimistic values once the edit ends, so the next edit builds on it.
            if (fresh) row.freshWhileEditing = true;
            rowActions(row);
            return;
        }
        if (fresh || row.freshWhileEditing) {
            row.freshWhileEditing = false;
            if (!row.mutations.get(row.target)?.pending) row.optimistic = undefined;
            if (!row.mutations.get(modeTarget)?.pending) row.modeOptimistic = undefined;
            if (!row.mutations.get(noteTarget)?.pending) row.noteOptimistic = undefined;
            if (!row.mutations.get(labelTarget)?.pending) row.labelOptimistic = undefined;
        }
        if (row.editorKind === "rows") {
            // The canvas can't edit rows its frontend never built; the Sonder
            // App builds its prompt on the server, so it edits them anyway.
            row.editor.update(spec, row.optimistic?.rows ?? spec.value, {
                canEdit: editable(spec),
                unavailable: rowsNotice ? NO_ROWS_NOTICE : "",
            });
        } else if (row.editor) {
            // Lane pickers derive Add and Remove from the pending value, so two
            // quick edits build on each other rather than on the stored value.
            row.editor.update(spec, row.optimistic ?? spec.value, {
                canEdit: editable(spec),
                choices: pickerChoices(row.editorKind),
                retry: () => pickers?.refresh?.(row.editorKind),
            });
        }
        if (row.input) {
            row.input.disabled = !editable(spec);
            if (row.editorKind === "asset_folder") {
                row.picker.update({ ...spec, options: ["", ...(pickerChoices("asset_folder")?.folders || [])] });
            } else if (row.picker) row.picker.update(spec);
            if (row.input.tagName === "SELECT" && row.optionsKey !== JSON.stringify(spec.options || [])) {
                row.optionsKey = JSON.stringify(spec.options || []);
                row.input.replaceChildren(...(spec.options || []).map(o => new Option(String(o), String(o))));
            }
            row.input.min = spec.min == null ? "" : String(spec.min);
            row.input.max = spec.max == null ? "" : String(spec.max);
            if (spec.kind === "INT" && row.input.type === "number") row.input.step = intStep(spec);
            // A seed step still waiting to save keeps its text.
            if (!row.seedStepTimer) setInput(row, row.optimistic ?? spec.value);
        }
        if (row.seedSteps) paintSeedSteps(row);
        if (row.mode) {
            row.mode.disabled = !editable(spec) || spec.seed_mode == null;
            row.mode.title = spec.seed_mode == null ? "This seed has no mode on this host." : "";
            row.mode.value = row.modeOptimistic ?? spec.seed_mode ?? "fixed";
        }
        if (row.note) {
            row.noteText = row.noteOptimistic ?? (spec.source === "note" ? spec.text : spec.note) ?? "";
            if (spec.source === "note") paintStandaloneNote(row);
            else {
                if (row.noteRendered !== row.noteText) row.note.replaceChildren(renderPanelMarkdown(row.noteText));
                row.noteRendered = row.noteText;
                row.note.style.cursor = manageOn ? "text" : "default";
                row.note.title = manageOn ? "Double-click to edit" : "";
                // A binding's empty note takes no space (Manage adds one through its Note action).
                row.note.style.fontStyle = "normal";
                row.note.style.display = row.noteText && notesShown() ? "" : "none";
                clampNote(row);
            }
        }
        rowActions(row);
        styleRow(row, spec);
    }
    // In Cards, a Control's note shows two lines, with More/Less only when it
    // runs longer, and its plain text as the tooltip (#11). Rows and Manage show
    // it whole. The expanded state is ephemeral.
    function clampNote(row) {
        const note = row.note, style = note.style;
        const clamp = cards() && !!row.noteText && notesShown();
        if (clamp && !row.noteMore) {
            row.noteMore = make("button");
            row.noteMore.type = "button";
            row.noteMore.dataset.noteMore = "true";
            row.noteMore.style.cssText = S.quietLineCss;
            row.noteMore.hidden = true; // until measured, so a short note never flashes it
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
        setText(row.noteMore, row.noteExpanded ? "Less" : "More");
        row.noteMore.setAttribute("aria-expanded", String(!!row.noteExpanded));
        if (!clamp) {
            row.noteMore.hidden = true;
            return;
        }
        // Shown only when the note overflows its two lines (or is expanded),
        // measured once per frame for every note: all reads, then all writes.
        // Until then it keeps its visibility: a render from a field's blur
        // runs between a press on More and its release, and hiding it there
        // dropped that first click (G1-F01).
        noteChecks.add(row);
        noteFrame = noteFrame || requestAnimationFrame(checkNotes);
    }
    function checkNotes() {
        noteFrame = 0;
        const rowsToCheck = [...noteChecks];
        noteChecks.clear();
        // Rechecked here: a later render may have left Cards or cleared the note.
        const shown = rowsToCheck.map(row => !destroyed && cards() && !!row.noteText && notesShown() && !!row.noteMore
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
        const filter = { query, showAdvanced: showAdvanced(), showNotes: showNotes(), manage: manageOn };
        const allSpecs = [...(view.bindings || []).map(d => ({ ...d, source: "binding" })), ...(view.notes || []).map(d => ({ ...d, source: "note" })),
            ...(view.execution || []).map(d => ({ ...d, source: "execution" }))];
        const empty = !allSpecs.some(d => manageOn || !d.hidden);
        restore.render(view);
        manage?.render(view);
        workflowHeader.render(view, {manage: manageOn});
        modes.render(view, {manage: manageOn, query});
        const entries = [];
        for (const spec of [...(view.bindings || []), ...(view.notes || []), ...(view.execution || []).map(d => ({ ...d, source: "execution" }))]) {
            const source = sourceOf(spec);
            const id = source === "note" ? spec.note_id : source === "execution" ? spec.execution_id : spec.binding_id;
            if (!id) continue;
            const target = source === "note" ? `note:${id}:text` : source === "execution" ? `execution:${id}:mode` : `binding:${id}:value`;
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
                        ...(isControl(spec)
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
        setText(quietMain, filter.showAdvanced ? "Hide advanced settings" : hidden.count ? `${hidden.count} advanced hidden` : "");
        setText(quietChanged, !filter.showAdvanced && hidden.count && hidden.changed ? ` · ${hidden.changed} changed` : "");
        setShown(quietLine, !manageOn && anyAdvanced && (filter.showAdvanced || hidden.count > 0));
        quietLine.title = filter.showAdvanced
            ? "Hide the settings this workflow marks Advanced (their values still apply)"
            : "Show the settings this workflow marks Advanced";
        // No aria-pressed: the label already states what a click does, and
        // "pressed" beside "Hide …" would read as the opposite.
        const notesHidden = hiddenNotes(allSpecs, filter);
        setText(notesLine, filter.showNotes ? "Hide notes" : `${notesHidden} ${notesHidden === 1 ? "note" : "notes"} hidden`);
        setShown(notesLine, !manageOn && hasNotes(allSpecs, filter));
        notesLine.title = filter.showNotes
            ? "Hide the workflow's notes (Manage shows them; a search shows a matching setting's note)"
            : "Show the workflow's notes";
        // Not setShown: showing again would clear the row's flex display.
        quietRow.hidden = quietLine.hidden && notesLine.hidden;
        quietRow.style.display = quietRow.hidden ? "none" : "flex";

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
        const pickerKinds = new Set();
        for (const { target, spec, group } of entries) {
            alive.add(target);
            const kind = editorKind(spec);
            if (["reference_lanes", "driver_lane", "asset_folder"].includes(kind)) pickerKinds.add(kind);
            let row = rows.get(target);
            if (
                row &&
                (row.kind !== spec.kind || row.editorKind !== editorKind(spec)) &&
                !row.editing
            ) {
                dropRow(row);
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
                if (visible && isControl(spec)) counts.set(group, (counts.get(group) || 0) + 1);
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
                dropRow(row);
                rows.delete(target);
            }
        declarePickerInterest(pickerKinds);
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
        setText(message,
            view.error ||
            view.relay?.error ||
            (!empty
                ? query && !shownTotal && !editorVisible ? `No settings match “${query.trim()}”. Search looks at names, groups and choices.` : ""
                : view.state === "ready" ? host.emptyMessage || CANVAS_EMPTY
                    : view.state === "disconnected" ? CANVAS_DISCONNECTED : "Loading controls…"));
        tips.refresh();
    }
    let closing = null;
    function setManage(on) {
        if (destroyed || on === manageOn) return;
        if (restore.busy) return;
        // Done while a Modes write is queued (often by the blur this press
        // caused): close once the queue settles, instead of dropping the click.
        if (!on && manage?.modesQueued) {
            if (closing) return;
            const waiting = manage;
            closing = waiting.whenIdle().catch(() => {}).then(() => {
                closing = null;
                if (manage === waiting) setManage(false);
            });
            return;
        }
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
            manage = mountManageSection(host, { notify: warning, success: message => host.success?.(message), flushPending, changed: () => render(),
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
            const listRow = [...rows.values()].find(row => row.editor?.contains(document.activeElement));
            if (listRow) {
                if (e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
                return listRow.editor.keydown(e) || PRESERVE_DEFAULT;
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
            // A seed's field steps like a number input, exactly (BigInt).
            if (row.seedSteps && row.input === document.activeElement && (e.key === "ArrowUp" || e.key === "ArrowDown")
                    && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
                stepRow(row, e.key === "ArrowUp" ? 1 : -1);
                return true;
            }
            if (e.key === "Escape") {
                cancelSeedStep(row);
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
    // started after the call (a tracked choice apply) are not awaited.
    async function flushPending() {
        // A focused row-list field commits on blur, before the snapshot.
        for (const row of rows.values()) row.editor?.flush();
        // A focused folder field's typed text is its value: save it with the run.
        for (const row of rows.values()) {
            if (row.picker?.freeText && row.picker.contains(document.activeElement)) row.picker.commitTyped();
        }
        // Snapshot before awaiting anything: a run save that settles while
        // the note saves was still in flight when the caller asked.
        const blocking = new Set(runPending);
        // A seed step still waiting for its pause saves now: the run uses it.
        for (const row of rows.values()) {
            const saving = flushSeedStep(row);
            if (saving) blocking.add(saving);
        }
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
            // A waiting seed step is a value the user chose: it still saves.
            for (const row of rows.values()) void flushSeedStep(row);
            if (noteEditor) void closeNote(false);
            for (const group of groups.values()) group.renaming?.close(false);
            if (manageOn) host.setManage?.(false, "");
            manage?.destroy();
            destroyed = true;
            container.removeEventListener("mouseover", valueTitle);
            arrangement.destroy();
            workflowHeader.destroy();
            modes.destroy();
            restore.destroy();
            tips.destroy();
            for (const row of rows.values()) {
                row.picker?.destroy();
                row.editor?.destroy();
                row.resizeObserver?.disconnect();
            }
            unsubscribe?.();
            // A remounted panel (a workflow switch) declares its own interest.
            unsubscribePickers?.();
            pickers?.setInterest([]);
            unregister();
            rows.clear();
            groups.clear();
            container.replaceChildren();
        },
    };
}
