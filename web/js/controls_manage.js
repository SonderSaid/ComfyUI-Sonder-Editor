// Manage: the Controls panel's authoring surface (Phase 3B). Owns the "Add
// settings" DOM, its filter, Restore, Rebind picking and new notes, and returns
// cleanup. The host owns candidates, writes and validation; the panel owns rows.
//
// Candidates arrive in the view (`view.candidates`) only while Manage is open:
// the host lists them from the live graph (canvas, mounted) or the template
// (Sonder App), already filtered and never greyed out.
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { groupNameError } from "./control_targets.js";
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, TYPE, chromeInputCss, setButtonDisabled, setButtonVariant } from "./editor_theme.js";

const ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
const FILTER_DEBOUNCE_MS = 250;
// Mirrors server/control_descriptors.py MAX_CONTROL_CHANGES: one batch, one Undo step.
const MAX_BATCH = 64;
let nextManage = 0;

// Client-minted ids, as control_bindings.mintBindingId (kept local so the
// Sonder App page does not load the canvas graph helpers).
export function mintId(length = 12) {
    const bytes = new Uint8Array(length);
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, byte => ID_ALPHABET[byte % ID_ALPHABET.length]).join("");
}

const make = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
};

export function smallButton(text, variant = "secondary") {
    const button = make("button", text);
    button.type = "button";
    setButtonVariant(button, variant, { padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
    return button;
}

const KIND_NAMES = { INT: "number", FLOAT: "number", SEED: "seed", BOOLEAN: "on/off", COMBO: "choice", STRING: "text" };
export const kindName = kind => KIND_NAMES[kind] || "";

export function mountManageSection(host, { notify = () => {}, track = work => work } = {}) {
    const id = ++nextManage;
    let destroyed = false,
        rebind = null, // {binding_id, label} while choosing a new setting for a binding
        filter = "",
        filterTimer = null,
        busy = false,
        draft = null, // the new-note or new-group editor
        lastView = null;
    const el = make("section");
    el.dataset.controlsManage = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;padding:${SPACE.s2}px;border:1px solid ${COLORS.border};border-radius:${RADIUS.r3}px;background:${COLORS.panel};`;
    // "Add settings" folds into one bar until opened (ephemeral), so Manage
    // stays short; a Rebind opens it.
    let open = false;
    const heading = make("button");
    heading.type = "button";
    heading.dataset.controlsAddSettings = "true";
    heading.style.cssText = `display:flex;justify-content:space-between;align-items:center;gap:${SPACE.s2}px;width:100%;`
        + `background:none;border:0;padding:0;cursor:pointer;font:${TYPE.fwBold} ${TYPE.t12}px ${FONT.sans};color:${COLORS.text};text-align:left;`;
    const headingText = make("span", "Add settings");
    const headingCount = make("span");
    headingCount.style.cssText = `font-size:${TYPE.t11}px;font-weight:${TYPE.fwNormal};color:${COLORS.textDim};`;
    heading.append(headingText, headingCount);
    heading.addEventListener("click", () => {
        open = !open;
        renderedKey = null;
        render(lastView || host.getView());
        if (open) filterInput.focus();
    });
    const filterInput = make("input");
    filterInput.type = "search";
    filterInput.placeholder = "Filter by node or setting";
    filterInput.setAttribute("aria-label", "Filter settings to add");
    filterInput.dataset.sonderTextUndo = "true";
    filterInput.style.cssText = chromeInputCss({ minWidth: "0" }) + "width:100%;box-sizing:border-box;";
    const banner = make("div");
    banner.style.cssText = `display:none;gap:${SPACE.s2}px;align-items:center;justify-content:space-between;color:${COLORS.text};`;
    const bannerText = make("span");
    const cancelRebind = smallButton("Cancel");
    banner.append(bannerText, cancelRebind);
    const list = make("div");
    list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;max-height:320px;overflow:auto;`;
    const footer = make("div");
    footer.style.cssText = `display:flex;gap:${SPACE.s2}px;align-items:center;flex-wrap:wrap;`;
    const addNote = smallButton("+ Note");
    addNote.title = "Add a Markdown note to the ungrouped Controls";
    const addGroup = smallButton("+ Group");
    addGroup.dataset.controlsAddGroup = "true";
    addGroup.title = "Add an empty group; move Controls into it from their Group menu";
    const noteBox = make("div");
    noteBox.style.cssText = `display:none;flex-direction:column;gap:${SPACE.s1}px;`;
    footer.append(addNote, addGroup);
    el.append(heading, filterInput, banner, list, footer, noteBox);

    function apply(changes, button) {
        return track(applyNow(changes, button), changes);
    }
    async function applyNow(changes, button) {
        if (busy || destroyed) return false;
        busy = true;
        if (button) setButtonDisabled(button, true);
        let result;
        try {
            result = await host.applyChanges(changes);
        } catch (error) {
            result = { ok: false, rejected: [{ reason: error?.message }] };
        }
        busy = false;
        if (destroyed) return false;
        if (button) setButtonDisabled(button, false);
        if (!result?.ok) notify(result?.rejected?.[0]?.reason || "The change could not be saved.");
        render();
        return !!result?.ok;
    }

    const expose = (candidate, button) =>
        apply({ [`binding:${mintId()}:expose`]: { candidate: { path: candidate.path, input: candidate.input } } }, button);

    function candidateRow(candidate, action) {
        const row = make("div");
        row.dataset.candidate = `${candidate.path}/${candidate.input}`;
        row.style.cssText = `display:flex;gap:${SPACE.s2}px;align-items:center;justify-content:space-between;min-width:0;padding:2px 0;`;
        const text = make("div");
        text.style.cssText = "display:flex;flex-direction:column;min-width:0;";
        const name = make("span", candidate.recommended ? candidate.label : candidate.input);
        name.style.cssText = `overflow-wrap:anywhere;color:${COLORS.text};`;
        const detail = make("span", [candidate.recommended ? candidate.node_title : "", kindName(candidate.kind), candidate.hint || ""]
            .filter(Boolean).join(" · "));
        detail.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};overflow-wrap:anywhere;`;
        text.append(name, detail);
        const button = smallButton(action.label, "accentSoft");
        button.title = action.title;
        button.addEventListener("click", () => void action.run(candidate, button));
        row.append(text, button);
        return row;
    }

    function groupHeading(text, extra) {
        const row = make("div");
        row.style.cssText = `display:flex;gap:${SPACE.s2}px;align-items:center;justify-content:space-between;margin-top:${SPACE.s1}px;font-size:${TYPE.t11}px;font-weight:${TYPE.fwBold};color:${COLORS.textSecondary};`;
        row.append(make("span", text));
        if (extra) row.append(extra);
        return row;
    }

    let renderedKey = null;
    function render(view = host.getView()) {
        if (destroyed) return;
        lastView = view;
        // Rebuilt only when what it lists changes: the panel renders on every
        // focus change, and a rebuild between a press and its click loses it.
        const key = JSON.stringify([rebind, filter, open, view.candidates, view.suggestions, view.candidates_truncated]);
        if (key === renderedKey) return;
        renderedKey = key;
        const candidates = view.candidates || [];
        const showing = open || !!rebind;
        banner.style.display = rebind ? "flex" : "none";
        bannerText.textContent = rebind ? `Choose a new setting for “${rebind.label}”.` : "";
        headingText.textContent = `${showing ? "v" : ">"}  ${rebind ? "Rebind" : "Add settings"}`;
        heading.setAttribute("aria-expanded", String(showing));
        const recommendedCount = candidates.filter(c => c.recommended).length;
        headingCount.textContent = view.candidates === undefined ? ""
            : `${candidates.length}${view.candidates_truncated ? "+" : ""} available${recommendedCount ? ` · ${recommendedCount} recommended` : ""}`;
        filterInput.style.display = showing ? "" : "none";
        list.style.display = showing ? "flex" : "none";
        footer.style.display = rebind ? "none" : "flex";
        const nodes = [];
        const action = rebind
            ? { label: "Use", title: "Bind this Control to this setting", run: (c, b) => apply({ [`binding:${rebind.binding_id}:rebind`]: { candidate: { path: c.path, input: c.input } } }, b).then(ok => { if (ok) setRebind(null); }) }
            : { label: "Add", title: "Expose this setting as a Control", run: (c, b) => expose(c, b) };
        const recommended = rebind ? [] : candidates.filter(c => c.recommended);
        if (recommended.length) {
            const all = smallButton("Add all recommended", "accentSoft");
            all.addEventListener("click", () => {
                const changes = {};
                for (const c of recommended.slice(0, MAX_BATCH)) {
                    changes[`binding:${mintId()}:expose`] = { candidate: { path: c.path, input: c.input } };
                }
                void apply(changes, all);
            });
            nodes.push(groupHeading("Recommended", all), ...recommended.map(c => candidateRow(c, action)));
        }
        const suggestions = rebind ? [] : view.suggestions || [];
        if (suggestions.length) {
            nodes.push(groupHeading("Exposed before"));
            for (const s of suggestions) {
                nodes.push(candidateRow({ path: "", input: s.label || s.binding_id, kind: "", node_title: "" }, {
                    label: "Restore",
                    title: "Expose it again, keeping its saved values",
                    run: (_c, b) => apply({ [`binding:${s.binding_id}:expose`]: {} }, b),
                }));
            }
        }
        let title = null;
        for (const c of candidates) {
            if (c.recommended && !rebind) continue;
            if (c.node_title !== title) {
                title = c.node_title;
                nodes.push(groupHeading(title || c.class_type || "Node"));
            }
            nodes.push(candidateRow(c, action));
        }
        // Amendment A1: the Sonder App lists top-level settings only.
        if (host.capabilities?.hide && !rebind) {
            const subgraphs = make("div", "Settings inside subgraphs are exposed from ComfyUI.");
            subgraphs.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
            nodes.push(subgraphs);
        }
        if (view.candidates_truncated) {
            const more = make("div", "Narrow the filter to see more.");
            more.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
            nodes.push(more);
        }
        if (!nodes.length) {
            const empty = make("div", view.candidates === undefined ? "Loading settings…"
                : filter ? "No settings match the filter." : "No more settings to expose.");
            empty.style.cssText = `color:${COLORS.textDim};`;
            nodes.push(empty);
        }
        list.replaceChildren(...nodes);
    }

    function setRebind(binding) {
        rebind = binding ? { binding_id: binding.binding_id, label: binding.label || binding.binding_id } : null;
        render(lastView || host.getView());
        if (rebind) filterInput.focus();
    }
    cancelRebind.addEventListener("click", () => setRebind(null));

    filterInput.addEventListener("input", () => {
        clearTimeout(filterTimer);
        filterTimer = setTimeout(() => {
            filter = filterInput.value.trim();
            host.setManage?.(true, filter);
        }, FILTER_DEBOUNCE_MS);
    });

    function closeDraft(save) {
        const edit = draft;
        if (!edit) return;
        if (save && edit.kind === "group") {
            const name = edit.area.value.trim();
            const error = groupNameError(name);
            if (error) {
                notify(error);
                return; // keep the field open to fix the name
            }
            const exists = (lastView || host.getView()).group_order?.includes?.(name);
            if (exists && !edit.then) {
                notify(`A group named “${name}” already exists. Choose another name.`);
                return;
            }
            const changes = { ...(exists ? {} : { [`group:${name}:create`]: {} }), ...(edit.then?.(name) || {}) };
            finishDraft(edit);
            void apply(changes);
            return;
        }
        finishDraft(edit);
        if (save && edit.area.value.trim()) {
            void apply({ [`note:${mintId()}:create`]: { text: edit.area.value, group: edit.group } });
        }
    }
    function finishDraft(edit) {
        draft = null;
        edit.unregister();
        noteBox.style.display = "none";
        noteBox.replaceChildren();
        setButtonDisabled(addNote, false);
        setButtonDisabled(addGroup, false);
    }
    function openDraft(group = "") {
        if (draft || destroyed) return;
        const area = make("textarea");
        area.maxLength = 4000;
        area.placeholder = "Markdown note shown with the Controls";
        area.dataset.sonderTextUndo = "true";
        area.style.cssText = chromeInputCss() + `width:100%;box-sizing:border-box;min-height:80px;resize:vertical;font-size:${TYPE.t12}px;font-family:${FONT.sans};`;
        const actions = make("div");
        actions.style.cssText = `display:flex;gap:${SPACE.s2}px;justify-content:flex-end;`;
        const save = smallButton("Save note", "accentSoft"), cancel = smallButton("Cancel");
        actions.append(cancel, save);
        noteBox.replaceChildren(area, actions);
        noteBox.style.display = "flex";
        setButtonDisabled(addNote, true);
        setButtonDisabled(addGroup, true);
        const edit = { kind: "note", area, group, composing: false, unregister: () => {} };
        area.placeholder = group ? `Markdown note shown in ${group}` : "Markdown note shown with the Controls";
        area.addEventListener("compositionstart", () => (edit.composing = true));
        area.addEventListener("compositionend", () => (edit.composing = false));
        save.addEventListener("click", () => closeDraft(true));
        cancel.addEventListener("click", () => closeDraft(false));
        draft = edit;
        edit.unregister = register({
            id: `controls-manage-note:${id}`,
            priority: PRIORITY.OVERLAY,
            keydown: e => {
                if (draft !== edit || document.activeElement !== area) return false;
                if (edit.composing || e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
                if (e.key === "Escape") { closeDraft(false); return true; }
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { closeDraft(true); return true; }
                return PRESERVE_DEFAULT;
            },
        });
        area.focus();
        noteBox.scrollIntoView?.({ block: "nearest" });
    }
    addNote.addEventListener("click", () => openDraft(""));

    // A new group's name, in the panel (never prompt()). `then(name)` adds
    // changes to the same batch: "New group…" moves a Control into it.
    function openGroupDraft(then = null) {
        if (destroyed) return;
        if (draft) {
            // Never discard an open draft's text to make room.
            notify("Save or cancel the open note or group first.");
            draft.area.focus();
            return;
        }
        const area = make("input");
        area.type = "text";
        area.maxLength = 240; // the rule counts code points; the server refuses past 120
        area.placeholder = "Group name";
        area.setAttribute("aria-label", "New group name");
        area.dataset.controlsNewGroup = "true";
        area.dataset.sonderTextUndo = "true";
        area.style.cssText = chromeInputCss({ minWidth: "0" }) + "width:100%;box-sizing:border-box;";
        const actions = make("div");
        actions.style.cssText = `display:flex;gap:${SPACE.s2}px;justify-content:flex-end;`;
        const save = smallButton(then ? "Create and move" : "Create group", "accentSoft"), cancel = smallButton("Cancel");
        actions.append(cancel, save);
        noteBox.replaceChildren(area, actions);
        noteBox.style.display = "flex";
        setButtonDisabled(addNote, true);
        setButtonDisabled(addGroup, true);
        const edit = { kind: "group", area, then, composing: false, unregister: () => {} };
        area.addEventListener("compositionstart", () => (edit.composing = true));
        area.addEventListener("compositionend", () => (edit.composing = false));
        save.addEventListener("mousedown", event => event.preventDefault());
        save.addEventListener("click", () => closeDraft(true));
        cancel.addEventListener("click", () => closeDraft(false));
        draft = edit;
        edit.unregister = register({
            id: `controls-manage-group:${id}`,
            priority: PRIORITY.OVERLAY,
            keydown: e => {
                if (draft !== edit || document.activeElement !== area) return false;
                if (edit.composing || e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
                if (e.key === "Escape") { closeDraft(false); return true; }
                if (e.key === "Enter") { closeDraft(true); return true; }
                return PRESERVE_DEFAULT;
            },
        });
        area.focus();
        noteBox.scrollIntoView?.({ block: "nearest" });
    }
    addGroup.addEventListener("click", () => openGroupDraft());

    // The filter keeps its keys: Ctrl+Z edits the query, never the scene.
    const unregister = register({
        id: `controls-manage:${id}`,
        priority: PRIORITY.TEXT_EDITOR,
        keydown: e => {
            if (document.activeElement !== filterInput) return false;
            if (e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
            if (e.key === "Escape") {
                if (rebind) setRebind(null);
                else if (filterInput.value) {
                    filterInput.value = "";
                    filterInput.dispatchEvent?.(new Event("input"));
                } else filterInput.blur();
                return true;
            }
            return PRESERVE_DEFAULT;
        },
    });

    return {
        el,
        render,
        setRebind,
        addNote: group => openDraft(typeof group === "string" ? group : ""),
        addGroup: then => openGroupDraft(typeof then === "function" ? then : null),
        get rebinding() { return rebind?.binding_id || null; },
        get editing() { return !!draft || document.activeElement === filterInput; },
        destroy() {
            destroyed = true;
            clearTimeout(filterTimer);
            if (draft) closeDraft(false);
            unregister();
            el.remove();
        },
    };
}
