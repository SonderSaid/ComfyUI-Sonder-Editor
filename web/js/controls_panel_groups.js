// Group authoring in Manage (SDK v2 Phase 4B): planners for group changes, the
// group header's menu and its in-place rename field. The panel
// (editor_controls_panel.js) owns rows, groups, pending saves and the host;
// this peer returns changes and cleanup, and never writes itself.
import { openContextMenu } from "./editor_context_menu.js";
import { groupNameError } from "./control_targets.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";

// Mirrors server/control_descriptors.py MAX_CONTROL_CHANGES.
export const MAX_GROUP_FLAGS = 64;
let nextRename = 0;

/** The change that moves `name` one place up (-1) or down (+1), or null at an end. */
export function groupStepChanges(order, name, step) {
    const index = order.indexOf(name);
    if (index < 0) throw new Error("This group changed. Refresh the Controls panel.");
    const position = index + step;
    if (position < 0 || position >= order.length) return null;
    const rest = order.filter(group => group !== name);
    return { [`group:${name}:move`]: { before: rest[position] ?? null } };
}

/** The change that puts `name` before `before` (null: last), or null when it is already there. */
export function groupDropChanges(order, name, before) {
    if (!order.includes(name) || name === before) return null;
    const following = order[order.indexOf(name) + 1] ?? null;
    if (following === before) return null;
    return { [`group:${name}:move`]: { before } };
}

/** Mark every Control of a group Advanced, or clear them all: one flag per Control. */
export function groupAdvancedChanges(members, on) {
    const changes = {};
    for (const spec of members) {
        if (spec.source === "binding" && spec.binding_id && (spec.advanced === true) !== on) {
            changes[`binding:${spec.binding_id}:advanced`] = on;
        }
    }
    if (Object.keys(changes).length > MAX_GROUP_FLAGS) {
        throw new Error(`This group has more than ${MAX_GROUP_FLAGS} Controls to change. Mark them one at a time.`);
    }
    return changes;
}

/** Menu items for a group header: rename, order, Advanced and Ungroup. */
export function groupMenuItems({ name, order, members, apply, rename, notify }) {
    const run = build => () => {
        try {
            const changes = build();
            if (changes && Object.keys(changes).length) apply(changes);
        } catch (error) {
            notify(error.message);
        }
    };
    const bindings = members.filter(spec => spec.source === "binding");
    const allAdvanced = bindings.length > 0 && bindings.every(spec => spec.advanced === true);
    const index = order.indexOf(name);
    return [
        { label: "Rename…", action: rename },
        { label: "Move up", disabled: index <= 0, action: run(() => groupStepChanges(order, name, -1)) },
        { label: "Move down", disabled: index < 0 || index >= order.length - 1, action: run(() => groupStepChanges(order, name, 1)) },
        { type: "separator" },
        allAdvanced
            ? { label: "Clear Advanced", hint: String(bindings.length), action: run(() => groupAdvancedChanges(bindings, false)) }
            : { label: "Mark all Advanced", disabled: !bindings.length, hint: bindings.length ? String(bindings.length) : "",
                action: run(() => groupAdvancedChanges(bindings, true)) },
        { type: "separator" },
        { label: "Ungroup", hint: members.length ? "Members stay, ungrouped" : "Deletes the empty group",
            action: run(() => ({ [`group:${name}:ungroup`]: true })) },
    ];
}

export function openGroupMenu(anchor, options) {
    const rect = anchor.getBoundingClientRect?.() || { left: 0, bottom: 0 };
    return openContextMenu({ x: rect.left, y: rect.bottom, items: groupMenuItems(options), focusFirst: true, closeOnScroll: true });
}

/**
 * "Move to group" items for one row. `planMove(group)` returns the row's
 * group and order changes (controls_panel_layout planPanelMove).
 */
export function moveToGroupItems({ order, current, planMove, apply, newGroup, notify }) {
    const run = group => () => {
        try {
            const changes = planMove(group);
            if (Object.keys(changes).length) apply(changes);
        } catch (error) {
            notify(error.message);
        }
    };
    const items = order.filter(group => group !== current).map(group => ({ label: group, action: run(group) }));
    if (current) items.push({ label: "Ungrouped", action: run("") });
    if (items.length) items.push({ type: "separator" });
    items.push({ label: "New group…", action: newGroup });
    return items;
}

/**
 * Replace `titleEl` with a name field. Enter or blur saves through
 * `save(name)`, Escape cancels; `close()` is idempotent and restores the title.
 */
export function startGroupRename(titleEl, { name, save, onClose = () => {}, notify = () => {}, css = "" }) {
    const id = ++nextRename;
    const input = document.createElement("input");
    input.type = "text";
    input.style.cssText = css;
    input.value = name;
    input.maxLength = 240; // the rule counts code points; the server refuses past 120
    input.setAttribute("aria-label", "Group name");
    input.dataset.sonderTextUndo = "true";
    input.dataset.groupRename = name;
    let closed = false, composing = false, unregister = () => {};
    const close = commit => {
        if (closed) return;
        closed = true;
        unregister();
        const next = input.value.trim();
        input.replaceWith(titleEl);
        onClose();
        if (!commit || next === name) return;
        const error = groupNameError(next);
        if (error) notify(error);
        else save(next);
    };
    input.addEventListener("compositionstart", () => (composing = true));
    input.addEventListener("compositionend", () => (composing = false));
    input.addEventListener("blur", () => close(true));
    // The header toggles its group on click; a click in the field must not.
    input.addEventListener("click", event => event.stopPropagation());
    unregister = register({
        id: `controls-group-rename:${id}`,
        priority: PRIORITY.OVERLAY,
        keydown: event => {
            if (closed || document.activeElement !== input) return false;
            if (composing || event.isComposing || event.keyCode === 229) return PRESERVE_DEFAULT;
            if (event.key === "Escape") { close(false); return true; }
            if (event.key === "Enter") { close(true); return true; }
            return PRESERVE_DEFAULT;
        },
    });
    titleEl.replaceWith(input);
    input.focus();
    input.select?.();
    return { input, close };
}
