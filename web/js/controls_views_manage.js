// Manage ▸ Views: the workflow's Output Views, renamed inline, moved up and
// down, deleted, and (a Sonder App copy) reset to the workflow's own. Views are
// created from a viewport pane's picker ("Add output…"). Each View's Timing
// says how its output follows the timeline (Phase 3B): from the Editor's render
// or generation start, skipping its first N output frames, at the Editor's
// frame rate or another. DOM only: the host validates and writes (one Controls
// change, one native Undo on the canvas).
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, SPACE, TYPE, chromeInputCss, chromeSelectCss, setButtonDisabled, setButtonVariant } from "./editor_theme.js";
import { normalizeTiming } from "./view_timing.js";

let serial = 0;
const make = (tag, text) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; };
const button = (text, title) => {
    const el = make("button", text);
    el.type = "button";
    el.title = title;
    setButtonVariant(el, "secondary", { padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
    return el;
};

const ORIGINS = { app: "App only", app_copy: "App copy" };
// "" is no authored timing. A take a Sonder Save Video places on the timeline
// follows its placement whatever this says (its scene_map wins).
const ANCHORS = [["", "No timing set"], ["render_start", "Follows from the render start"],
    ["generation_start", "Follows from the generation start"]];
const timingKey = timing => JSON.stringify(normalizeTiming(timing));
const labelled = (text, control) => {
    const label = make("label");
    label.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1}px;font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
    label.append(make("span", text), control);
    return label;
};

export function mountViewsManage(host, { track = work => work, notify = () => {} } = {}) {
    const el = make("section"), heading = make("div", "Views"), hint = make("div"), list = make("div");
    el.dataset.controlsViewsManage = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    heading.style.cssText = `font-weight:${TYPE.fwBold};font-size:${TYPE.t12}px;color:${COLORS.text};`;
    hint.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
    hint.textContent = "Add a View from a viewport pane: its source button, then Add output.";
    list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    el.append(heading, list, hint);
    let view = {}, pending = 0, destroyed = false, renderedKey = null;
    let queue = Promise.resolve();
    const inputs = new Map(); // view id (its name), or "<id>#offset" / "<id>#fps"
    const controlsById = new Map();

    // Writes run one after another, never refused: a rename committed by the
    // blur of the press that clicks Delete is followed by that Delete.
    // `changesFor()` reads the Views as they are when its turn comes.
    function write(changesFor) {
        if (destroyed) return Promise.resolve(false);
        pending += 1;
        const run = queue.then(async () => {
            const changes = destroyed ? null : changesFor();
            if (!changes || !Object.keys(changes).length) return false;
            let result;
            try {
                result = await track(host.applyChanges(changes), changes);
            } catch (error) {
                result = { ok: false, rejected: [{ reason: error?.message }] };
            }
            if (destroyed) return false;
            if (!result?.ok) notify(result?.rejected?.[0]?.reason || "The View could not be saved.");
            render(host.getView());
            return !!result?.ok;
        }).finally(() => { pending -= 1; });
        queue = run.catch(() => false);
        return run;
    }

    // The row's Timing controls as one value: null (not linked) or a timing.
    // An invalid entry says why and puts the stored timing back.
    function commitTiming(descriptor, controls) {
        if (controls.anchor._sonderDiscarded) return;
        const anchor = controls.anchor.value;
        let next = null;
        if (anchor) {
            const offsetText = controls.offset.value.trim();
            const fpsText = controls.fps.value.trim();
            next = normalizeTiming({ mode: "editor_window", anchor, source_offset: offsetText === "" ? 0 : Number(offsetText),
                fps: fpsText === "" || /^editor/i.test(fpsText) ? "editor" : Number(fpsText) });
            if (!next) {
                notify("Timing needs a whole number of frames to skip from 0, and a frame rate above 0 and up to 240 (or empty for the Editor's).");
                renderedKey = null;
                render(host.getView());
                return;
            }
        }
        if (timingKey(next) === timingKey(descriptor.timing)) return;
        void write(() => ({ [`view:${descriptor.view_id}:timing`]: next }));
    }

    function timingRow(descriptor) {
        const timing = normalizeTiming(descriptor.timing);
        const row = make("div");
        row.dataset.viewTiming = descriptor.view_id;
        row.style.cssText = `display:flex;flex-wrap:wrap;gap:${SPACE.s1}px ${SPACE.s2}px;align-items:center;`;
        const anchor = make("select");
        anchor.style.cssText = chromeSelectCss();
        anchor.setAttribute("aria-label", `How View ${descriptor.label || descriptor.default_label || ""} follows the timeline`);
        for (const [value, text] of ANCHORS) {
            const option = make("option", text);
            option.value = value;
            anchor.append(option);
        }
        anchor.value = timing?.anchor || "";
        const offset = make("input");
        offset.type = "number";
        offset.min = "0";
        offset.step = "1";
        offset.value = timing ? String(timing.source_offset) : "0";
        offset.style.cssText = chromeInputCss({ minWidth: "0" }) + "width:64px;";
        offset.dataset.sonderTextUndo = "true";
        const fps = make("input");
        fps.type = "text";
        fps.inputMode = "decimal";
        fps.placeholder = "Editor's";
        fps.value = timing && timing.fps !== "editor" ? String(timing.fps) : "";
        fps.style.cssText = chromeInputCss({ minWidth: "0" }) + "width:72px;";
        fps.dataset.sonderTextUndo = "true";
        const controls = { anchor, offset, fps };
        const unit = text => {
            const el = make("span", text);
            el.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};`;
            return el;
        };
        const sync = () => { for (const el of [offset, fps]) el.disabled = !anchor.value; };
        sync();
        anchor.addEventListener("change", () => { sync(); commitTiming(descriptor, controls); });
        for (const el of [offset, fps]) el.addEventListener("blur", () => commitTiming(descriptor, controls));
        inputs.set(`${descriptor.view_id}#offset`, offset);
        inputs.set(`${descriptor.view_id}#fps`, fps);
        controlsById.set(descriptor.view_id, controls);
        row.append(anchor, labelled("Skip first", offset), unit("output frames,"), labelled("at", fps), unit("fps"));
        if (descriptor.class_type === "SonderSaveVideo") {
            const note = unit("A take placed on the timeline follows its placement; this timing applies otherwise.");
            note.style.flexBasis = "100%";
            row.append(note);
        }
        return row;
    }

    function rename(id, input, current) {
        if (input._sonderDiscarded) return; // replaced by a rebuild that kept its draft
        const value = input.value.trim();
        if (value === current) return;
        void write(() => ({ [`view:${id}:label`]: value }));
    }

    // Orders renumbered 0..n-1 in the new sequence: at most 32 targets, one write.
    function move(id, delta) {
        void write(() => {
            const views = host.getView()?.views || [];
            const order = views.map(d => d.view_id);
            const from = order.indexOf(id), to = from + delta;
            if (from < 0 || to < 0 || to >= order.length) return null;
            order.splice(from, 1);
            order.splice(to, 0, id);
            const changes = {};
            order.forEach((vid, index) => {
                if (views.find(d => d.view_id === vid)?.order !== index) changes[`view:${vid}:order`] = index;
            });
            return changes;
        });
    }

    // What the list shows: rebuilt only when this changes. The panel renders
    // on every focus change, and a rebuild between a press and its click loses it.
    const listKey = value => JSON.stringify([value.views_error || null, (value.views || []).map(d =>
        [d.view_id, d.label, d.default_label, d.order, d.origin, d.status, d.reason, d.participation, d.title, d.class_type,
            timingKey(d.timing)])]);

    function render(next = host.getView()) {
        view = next || {};
        const views = view.views || [];
        el.style.display = views.length || view.views_error ? "flex" : "none";
        const key = listKey(view);
        if (key === renderedKey) return;
        renderedKey = key;
        // A name being typed survives the rebuild: same View, same draft.
        const focusedEntry = [...inputs.entries()].find(([, input]) => input === document.activeElement);
        const draft = focusedEntry ? { id: focusedEntry[0], value: focusedEntry[1].value,
            start: focusedEntry[1].selectionStart, end: focusedEntry[1].selectionEnd } : null;
        for (const input of inputs.values()) input._sonderDiscarded = true;
        for (const controls of controlsById.values()) controls.anchor._sonderDiscarded = true;
        inputs.clear();
        controlsById.clear();
        if (view.views_error) {
            list.replaceChildren(make("div", view.views_error));
            return;
        }
        list.replaceChildren(...views.map((descriptor, index) => {
            const row = make("div");
            row.dataset.viewId = descriptor.view_id;
            row.style.cssText = `display:flex;flex-direction:column;gap:2px;padding:2px 0;`;
            const top = make("div");
            top.style.cssText = `display:flex;gap:${SPACE.s1}px;align-items:center;`;
            const input = make("input");
            input.type = "text";
            input.value = descriptor.label || "";
            input.placeholder = descriptor.default_label || "Output";
            input.setAttribute("aria-label", `Name of View ${descriptor.label || descriptor.default_label || ""}`);
            input.dataset.sonderTextUndo = "true";
            input.style.cssText = chromeInputCss({ minWidth: "0" }) + "flex:1 1 auto;";
            const current = descriptor.label === descriptor.default_label ? "" : descriptor.label || "";
            if (!current) input.value = "";
            input.addEventListener("blur", () => rename(descriptor.view_id, input, current));
            inputs.set(descriptor.view_id, input);
            const up = button("↑", "Move View up"), down = button("↓", "Move View down");
            setButtonDisabled(up, index === 0);
            setButtonDisabled(down, index === views.length - 1);
            up.addEventListener("click", () => move(descriptor.view_id, -1));
            down.addEventListener("click", () => move(descriptor.view_id, 1));
            const remove = button("Delete", "Delete this View; its output node stays in the workflow");
            remove.addEventListener("click", () => {
                if (host.capabilities?.hide && !globalThis.confirm(`Delete View “${descriptor.label || descriptor.default_label}”?`)) return;
                void write(() => ({ [`view:${descriptor.view_id}:remove`]: true }));
            });
            top.append(input, up, down, remove);
            if (descriptor.origin === "app_copy") {
                const reset = button("Reset", "Back to the workflow's own View");
                reset.addEventListener("click", () => void write(() => ({ [`view:${descriptor.view_id}:reset`]: true })));
                top.append(reset);
            }
            const detail = make("div", [descriptor.title || descriptor.class_type, ORIGINS[descriptor.origin] || "",
                descriptor.status !== "ok" ? descriptor.reason : descriptor.participation === "inactive" ? "Switched off" : ""]
                .filter(Boolean).join(" · "));
            detail.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textDim};overflow-wrap:anywhere;`;
            row.append(top, detail, timingRow(descriptor));
            return row;
        }));
        const kept = draft && inputs.get(draft.id);
        if (kept) {
            kept.value = draft.value;
            kept.focus();
            try { kept.setSelectionRange(draft.start ?? draft.value.length, draft.end ?? draft.value.length); } catch (_error) { /* not a text input */ }
        }
    }

    const unregister = register({
        id: `controls-views-manage:${++serial}`, priority: PRIORITY.TEXT_EDITOR, keydown: event => {
            const input = document.activeElement;
            const key = [...inputs.entries()].find(([, el]) => el === input)?.[0];
            if (!key) return false;
            if (event.isComposing || event.keyCode === 229) return PRESERVE_DEFAULT;
            const [id, part] = key.split("#");
            if (part) {
                // A Timing field: Enter commits, Escape puts the stored value back.
                if (event.key === "Escape") {
                    // No commit on the blur, and no draft kept by the rebuild.
                    const controls = controlsById.get(id);
                    if (controls) controls.anchor._sonderDiscarded = true;
                    input.blur();
                    renderedKey = null;
                    render(host.getView());
                    return true;
                }
                if (event.key === "Enter") { input.blur(); return true; }
                return PRESERVE_DEFAULT;
            }
            if (event.key === "Escape") {
                const descriptor = (view.views || []).find(d => d.view_id === id);
                input.value = descriptor && descriptor.label !== descriptor.default_label ? descriptor.label : "";
                input.blur();
                return true;
            }
            if (event.key === "Enter") { input.blur(); return true; }
            return PRESERVE_DEFAULT;
        },
    });
    render();
    return {
        el, render,
        get busy() { return pending > 0; },
        get editing() { return [...inputs.values()].includes(document.activeElement); },
        destroy() { destroyed = true; unregister(); el.remove(); },
    };
}
