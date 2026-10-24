// Manage ▸ Views: the workflow's Output Views, renamed inline, moved up and
// down, deleted, and (a Sonder App copy) reset to the workflow's own. Views are
// created from a viewport pane's picker ("Add output…"). DOM only: the host
// validates and writes (one Controls change, one native Undo on the canvas).
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, SPACE, TYPE, chromeInputCss, setButtonDisabled, setButtonVariant } from "./editor_theme.js";

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
    const inputs = new Map();

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
        [d.view_id, d.label, d.default_label, d.order, d.origin, d.status, d.reason, d.participation, d.title, d.class_type])]);

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
        inputs.clear();
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
            row.append(top, detail);
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
            const id = [...inputs.entries()].find(([, el]) => el === input)?.[0];
            if (!id) return false;
            if (event.isComposing || event.keyCode === 229) return PRESERVE_DEFAULT;
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
