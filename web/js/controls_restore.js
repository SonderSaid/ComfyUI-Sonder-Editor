// Restore notice DOM only. The panel owns pending work; the canvas host owns
// fresh validation, anchor writes and native Undo. No project state is added.
import { EDITOR_COLORS as COLORS, SPACE, TYPE, setButtonDisabled, setButtonVariant } from "./editor_theme.js";

const REASONS = {
    several_settings: "This Control points to several settings.", not_found: "Its setting was not found.",
    ambiguous: "More than one setting could match.", taken: "Another Control already uses this setting.",
    unavailable: "Its setting is not editable.",
};
const make = (tag, text) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; };

export function restoreSummary(count, unmatched = []) {
    const labels = unmatched.slice(0, 5).map(row => row.label || row.binding_id).join(", ");
    return `Restored ${count} Controls` + (unmatched.length
        ? `. ${unmatched.length} need Rebind in Manage: ${labels}${unmatched.length > 5 ? "…" : ""}` : "");
}

export function mountRestore(host, {track = work => work, flushPending = async () => true,
    notify = () => {}, rebind = () => {}} = {}) {
    const el = make("section"), message = make("div"), button = make("button", "Restore Controls"), unmatched = make("div");
    el.dataset.controlsRestore = "true";
    el.style.cssText = `display:none;flex-direction:column;gap:${SPACE.s2}px;color:${COLORS.textSecondary};font-size:${TYPE.t11}px;overflow-wrap:anywhere;`;
    button.type = "button";
    setButtonVariant(button, "accentSoft", {padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px`, alignSelf: "flex-start"});
    el.append(message, button, unmatched);
    let destroyed = false, busy = false, renderedKey = null;
    const ready = view => view?.state === "ready" && !view.error && view.relay?.status === "ok";
    function render(view = host.getView()) {
        if (destroyed) return;
        const plan = view.restore, count = plan?.restorable?.length || 0, misses = plan?.unmatched || [];
        el.style.display = count || misses.length ? "flex" : "none";
        message.textContent = `${count + misses.length} Controls came from an API file and aren't connected to their settings yet.`;
        button.style.display = count ? "" : "none";
        setButtonDisabled(button, busy || !ready(view));
        const key = JSON.stringify([misses, busy, ready(view)]);
        if (key !== renderedKey) {
            renderedKey = key;
            unmatched.replaceChildren();
            for (const row of misses) {
                const line = make("div"), action = make("button", "Rebind");
                line.style.cssText = `display:flex;align-items:center;gap:${SPACE.s2}px;`;
                action.type = "button";
                setButtonVariant(action, "secondary", {padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px`});
                setButtonDisabled(action, busy || !ready(view));
                action.addEventListener("click", () => rebind(row));
                line.append(make("span", `${row.label || row.binding_id}: ${REASONS[row.reason] || row.reason}`), action);
                unmatched.append(line);
            }
        }
    }
    button.addEventListener("click", () => {
        if (busy || !ready(host.getView())) return;
        const plan = host.getView().restore;
        const changes = {"workflow:restore": {bindings: plan.restorable.map(row => row.binding_id)}};
        busy = true; render();
        const work = (async () => {
            try {
                if (!await flushPending()) throw new Error("Finish the pending Control edit before restoring.");
                if (destroyed) return false;
                const result = await host.applyChanges(changes);
                if (!result?.ok) throw new Error(result?.rejected?.[0]?.reason || "The Controls could not be restored.");
                if (!destroyed) notify(restoreSummary(plan.restorable.length, plan.unmatched));
                return true;
            } catch (error) { if (!destroyed) notify(error.message); return false; }
            finally { busy = false; if (!destroyed) render(); }
        })();
        track(work, changes);
    });
    return {el, render, get busy() { return busy; }, destroy() { destroyed = true; el.remove(); }};
}
