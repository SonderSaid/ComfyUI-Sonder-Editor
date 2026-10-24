// The viewport's panes: a grid of up to four, each showing the Composite, the
// Guides, a Driver lane or an output View. Owns the grid, the layout control
// (1 / 2 / 3 / 2×2) in the viewport header and each pane's chrome: its source
// button and picker, status chip, Hold/Release, Run/Cancel, item picker, fit
// and sound. The coordinator (views_coordinator.js) owns state; the widget
// owns the Composite canvas and is told where it goes (`placeComposite`).
import { LAYOUTS, LAYOUT_LABELS, LAYOUT_ORDER, LAYOUT_TITLES, entryPlayer, pickerSections } from "./views_model.js";
import { openPanePicker } from "./view_pane_picker.js";
import { mountOutputMedia, messageEl } from "./output_pane_media.js";
import { mountDriverPainter, mountGuidesPainter } from "./editor_source_followers.js";
import { fileKey } from "./output_media_cache.js";
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE, THEME, TYPE, segmentCss, setButtonDisabled, setButtonVariant } from "./editor_theme.js";

const ZOOM_SAVE_MS = 300;
const make = (tag, text, css = "") => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (css) el.style.cssText = css;
    return el;
};

function button(text, title, variant = "secondary") {
    const el = make("button", text);
    el.type = "button";
    el.title = title;
    setButtonVariant(el, variant, { padding: `1px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
    el.style.whiteSpace = "nowrap";
    return el;
}

const GRIDS = {
    "1": "grid-template-columns:1fr;grid-template-rows:1fr;",
    "2c": "grid-template-columns:1fr 1fr;grid-template-rows:1fr;",
    "3c": "grid-template-columns:1fr 1fr 1fr;grid-template-rows:1fr;",
    "2x2": "grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;",
};

export function sourceLabel(model, { driverLabel = index => `Driver ${index + 1}` } = {}) {
    const source = model.source;
    if (source.kind === "composite") return "Composite";
    if (source.kind === "guides") return "Guides";
    if (source.kind === "driver") return driverLabel(source.index);
    return model.view?.label || model.view?.default_label || "Output";
}

// What a pane's content is: rebuilt only when this changes (chips and buttons
// are patched in place, so a read never restarts a playing output, and Hold or
// Release of the shown file keeps it playing).
function contentKey(model) {
    const source = model.source;
    if (source.kind !== "view") return `${source.kind}:${source.index ?? ""}`;
    return `view:${source.viewId}:${model.entry ? fileKey(model.entry) : ""}`;
}

export function mountViewPanes({ root, header, coordinator, cache, audio, sourceHost, placeComposite }) {
    let destroyed = false;
    let frame = null;
    let picker = null; // {index, close}
    const cells = [];
    const zoomTimers = new Map();
    const rootStyle = root.style.cssText;
    root.style.cssText = "flex:1;display:grid;gap:2px;min-height:0;position:relative;overflow:hidden;";

    // Header: the layout control, and in one-pane layouts the pane's own bar.
    const headerSlot = make("div", undefined, `display:flex;align-items:center;gap:${SPACE.s2}px;min-width:0;flex:1 1 auto;justify-content:flex-end;`);
    const layoutControl = make("div", undefined, "display:flex;gap:0;flex:0 0 auto;");
    layoutControl.setAttribute("role", "group");
    layoutControl.setAttribute("aria-label", "Viewport panes");
    const layoutButtons = new Map();
    LAYOUT_ORDER.forEach((layout, i) => {
        const el = make("button", LAYOUT_LABELS[layout]);
        el.type = "button";
        el.title = LAYOUT_TITLES[layout];
        el.dataset.sonderPaneLayout = layout;
        el.addEventListener("click", () => coordinator.setLayout(layout));
        layoutButtons.set(layout, el);
        layoutControl.append(el);
        el._radius = i === 0 ? `${RADIUS.r2}px 0 0 ${RADIUS.r2}px` : i === LAYOUT_ORDER.length - 1 ? `0 ${RADIUS.r2}px ${RADIUS.r2}px 0` : "0";
    });
    header.append(headerSlot, layoutControl);

    function cell(index) {
        if (cells[index]) return cells[index];
        const el = make("div", undefined, `display:flex;flex-direction:column;min-width:0;min-height:0;background:${COLORS.panelMuted};`);
        el.dataset.sonderPane = String(index);
        const bar = make("div", undefined, `display:flex;align-items:center;gap:${SPACE.s1}px;min-width:0;flex:0 0 auto;`
            + `padding:2px ${SPACE.s1}px;font:${TYPE.t11}px ${FONT.sans};color:${COLORS.textSecondary};`);
        const body = make("div", undefined, "position:relative;flex:1 1 auto;min-height:0;display:flex;flex-direction:column;"
            + "align-items:stretch;justify-content:center;overflow:hidden;background:#000;");
        const sourceButton = button("", "Choose what this pane shows");
        sourceButton.dataset.sonderPaneSource = String(index);
        sourceButton.setAttribute("aria-haspopup", "menu");
        // A fixed cap: a percentage of a shrink-to-fit bar collapses to nothing.
        sourceButton.style.maxWidth = "220px";
        sourceButton.style.minWidth = "0";
        sourceButton.style.flex = "0 1 auto";
        sourceButton.style.overflow = "hidden";
        sourceButton.style.textOverflow = "ellipsis";
        sourceButton.addEventListener("click", () => togglePicker(index, sourceButton));
        const chip = make("span", "", `min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${THEME.statusPending};`);
        chip.dataset.sonderPaneChip = String(index);
        const spacer = make("span", undefined, "flex:1 1 auto;");
        const actions = make("span", undefined, `display:flex;gap:${SPACE.s1}px;flex:0 0 auto;`);
        bar.append(sourceButton, chip, spacer, actions);
        el.append(bar, body);
        cells[index] = { el, bar, body, sourceButton, chip, actions, key: null, content: null, message: null, painterStatus: null };
        return cells[index];
    }

    function destroyContent(entry) {
        try { entry.content?.destroy(); } catch (error) { console.warn("[Sonder] pane teardown failed:", error); }
        entry.content = null;
        entry.painterStatus = null;
        for (const child of [...entry.body.children]) if (child.dataset.sonderComposite !== "true") child.remove();
        entry.message = null;
    }

    function mountContent(index, entry, model) {
        const source = model.source;
        const paneCanvas = () => {
            const canvas = make("canvas", undefined, "display:block;margin:auto;background:#000;");
            entry.body.append(canvas);
            return canvas;
        };
        const onStatus = text => { entry.painterStatus = text; showMessage(entry, text); };
        if (source.kind === "guides") entry.content = mountGuidesPainter({ canvas: paneCanvas(), cell: entry.body, host: sourceHost, onStatus });
        else if (source.kind === "driver") entry.content = mountDriverPainter({ canvas: paneCanvas(), cell: entry.body, host: sourceHost, index: source.index, onStatus });
        else if (source.kind === "view" && model.entry) {
            entry.content = mountOutputMedia({
                container: entry.body, entry: model.entry, cache, paneId: coordinator.paneId(index), audio,
                fit: model.pane.fit, zoom: model.pane.zoom, takeSaved: !!model.result?.run?.saved_to_project,
                onZoom: zoom => {
                    clearTimeout(zoomTimers.get(index));
                    zoomTimers.set(index, setTimeout(() => coordinator.setPaneOption(index, { zoom }), ZOOM_SAVE_MS));
                },
                onCondition: condition => coordinator.setMediaCondition(index, condition),
            });
        }
    }

    function showMessage(entry, text) {
        if ((entry.message?.textContent || null) === (text || null)) return;
        entry.message?.remove();
        entry.message = text ? messageEl(text) : null;
        if (entry.message) entry.body.append(entry.message);
    }

    function actionsFor(index, model) {
        const out = [];
        if (model.source.kind !== "view") return out;
        const entries = model.entries || [];
        if (entries.length > 1 && !model.held) {
            const prev = button("◀", "Previous item");
            const next = button("▶", "Next item");
            const count = make("span", `${model.itemIndex + 1}/${entries.length}`, `color:${COLORS.textDim};align-self:center;`);
            setButtonDisabled(prev, model.itemIndex <= 0);
            setButtonDisabled(next, model.itemIndex >= entries.length - 1);
            prev.addEventListener("click", () => coordinator.setPaneOption(index, { item: model.itemIndex - 1 }));
            next.addEventListener("click", () => coordinator.setPaneOption(index, { item: model.itemIndex + 1 }));
            out.push(prev, count, next);
        }
        const player = model.entry ? entryPlayer(model.entry) : null;
        if (player && player !== "audio" && player !== "unsupported") {
            const fit = button(model.pane.fit === "cover" ? "Fill" : "Fit", model.pane.fit === "cover"
                ? "The output fills the pane. Show all of it" : "The whole output shows. Fill the pane");
            fit.addEventListener("click", () => {
                const value = model.pane.fit === "cover" ? "contain" : "cover";
                coordinator.setPaneOption(index, { fit: value });
                cells[index]?.content?.setFit?.(value);
            });
            out.push(fit);
        }
        // The owner keeps its Mute whatever its media does now (missing, an
        // image, disconnected): it is the way the sound goes back to the timeline.
        if (player === "video" || player === "audio" || model.audible) {
            const sound = button(model.audible ? "Mute" : "Unmute", model.audible ? "Give the sound back to the timeline"
                : "Hear this output; the timeline and other panes go quiet");
            sound.setAttribute("aria-pressed", String(!!model.audible));
            sound.addEventListener("click", () => {
                if (model.audible) audio.release(coordinator.paneId(index));
                else audio.claim(coordinator.paneId(index));
            });
            out.push(sound);
        }
        if (model.held || model.unavailable) {
            const release = button("Release", "Stop holding this result; show the newest");
            setButtonDisabled(release, model.holdPending);
            release.addEventListener("click", () => coordinator.release(index));
            out.push(release);
        } else if (model.canHold) {
            const hold = button("Hold", "Keep this result here while newer runs land");
            hold.addEventListener("click", () => void coordinator.hold(index));
            out.push(hold);
        }
        if (model.cancellable?.show) {
            const cancel = button("Cancel", model.cancellable.reason);
            setButtonDisabled(cancel, !model.cancellable.enabled);
            cancel.addEventListener("click", () => void coordinator.cancel(index));
            out.push(cancel);
        } else if (model.runnable?.show) {
            const run = button("Run", model.runnable.reason, "accentSoft");
            run.dataset.sonderPaneRun = String(index);
            setButtonDisabled(run, !model.runnable.enabled);
            run.addEventListener("click", () => void coordinator.run(index));
            out.push(run);
        }
        return out;
    }

    function actionSignature(model) {
        return JSON.stringify([model.entries?.length || 0, model.itemIndex, model.pane.fit, model.audible, model.held, model.holdPending,
            model.unavailable, model.canHold, model.cancellable, model.runnable,
            model.entry ? entryPlayer(model.entry) : null]);
    }

    function renderPane(index, single) {
        const entry = cell(index);
        const model = coordinator.paneModel(index);
        const key = contentKey(model);
        if (entry.key !== key) {
            destroyContent(entry);
            entry.key = key;
            mountContent(index, entry, model);
        } else if (model.source.kind === "guides" || model.source.kind === "driver") {
            // The scene changed under a paused clock (a hidden track, a muted
            // clip, a new layout): the painter reads it again.
            entry.content?.update?.();
        }
        entry.sourceButton.textContent = `${sourceLabel(model, { driverLabel: sourceHost.driverLabel })} ▾`;
        entry.sourceButton.setAttribute("aria-expanded", String(picker?.index === index));
        entry.chip.textContent = model.chip || "";
        entry.chip.title = model.chip || "";
        const signature = actionSignature(model);
        if (entry.actionSignature !== signature) {
            entry.actionSignature = signature;
            entry.actions.replaceChildren(...actionsFor(index, model));
        }
        entry.bar.title = model.runnable && !model.runnable.show ? model.runnable.reason : "";
        if (model.source.kind === "view") showMessage(entry, model.message || null);
        else if (model.source.kind === "composite") showMessage(entry, null);
        // One pane: its bar sits in the viewport header, no extra row.
        const barHome = single ? headerSlot : entry.el;
        if (entry.bar.parentElement !== barHome) {
            if (single) headerSlot.replaceChildren(entry.bar);
            else entry.el.insertBefore(entry.bar, entry.body);
        }
        entry.bar.style.padding = single ? "0" : `2px ${SPACE.s1}px`;
        // In the header the bar takes the free width; in a cell it keeps its height.
        entry.bar.style.flex = single ? "1 1 auto" : "0 0 auto";
        return { entry, model };
    }

    function render() {
        frame = null;
        if (destroyed) return;
        const record = coordinator.layout();
        const count = LAYOUTS[record.layout] || 1;
        root.style.cssText = `flex:1;display:grid;gap:2px;min-height:0;position:relative;overflow:hidden;${GRIDS[record.layout] || GRIDS["1"]}`;
        for (const [layout, el] of layoutButtons) {
            el.style.cssText = segmentCss({ active: layout === record.layout }) + `border-radius:${el._radius};cursor:pointer;margin-left:-1px;`;
            el.setAttribute("aria-pressed", String(layout === record.layout));
        }
        for (let i = count; i < cells.length; i++) {
            if (!cells[i]) continue;
            destroyContent(cells[i]);
            cells[i].el.remove();
            cells[i].bar.remove();
            cells[i] = null;
        }
        cells.length = Math.min(cells.length, count);
        if (picker && picker.index >= count) closePicker();
        let compositeBody = null;
        for (let i = 0; i < count; i++) {
            const { entry, model } = renderPane(i, count === 1);
            if (entry.el.parentElement !== root || root.children[i] !== entry.el) root.insertBefore(entry.el, root.children[i] || null);
            if (model.source.kind === "composite") compositeBody = entry.body;
        }
        if (count !== 1 && headerSlot.childElementCount) headerSlot.replaceChildren();
        placeComposite(compositeBody);
        refreshPicker();
    }

    function schedule() {
        if (frame === null && !destroyed) frame = requestAnimationFrame(render);
    }

    function closePicker() {
        const open = picker;
        picker = null;
        open?.close();
    }
    function togglePicker(index, anchor) {
        if (picker?.index === index) { closePicker(); return; }
        closePicker();
        const sections = currentSections();
        coordinator.setPickerOpen(true);
        const handle = openPanePicker({
            anchor, sections, current: coordinator.layout().panes[index]?.source,
            onPick: value => {
                if (value.source) coordinator.assign(index, value.source);
                else if (value.candidate) void coordinator.createView(index, value.candidate);
            },
            onClose: () => {
                if (picker?.handle === handle) picker = null;
                coordinator.setPickerOpen(false);
                schedule();
            },
        });
        picker = { index, close: () => handle.close(), handle, signature: JSON.stringify(sections) };
        schedule();
    }
    function currentSections() {
        const view = coordinator.controlsView() || {};
        return pickerSections({
            views: view.views || [], candidates: view.view_candidates || [], drivers: sourceHost.driverLanes(),
            // A Relay error (two Relays, a muted one) leaves no Views and no candidates: say why.
            viewsError: view.views_error || view.error || null, viewsReady: view.state === "ready",
        });
    }
    // The Controls view moved on while the picker is open: refresh it in place.
    function refreshPicker() {
        if (!picker) return;
        const sections = currentSections();
        const signature = JSON.stringify(sections);
        if (signature === picker.signature) return;
        picker.signature = signature;
        picker.handle.update(sections);
    }

    // Who has the sound changed (a Mute/Unmute press, Inspect): the bars say so.
    const unsubscribeAudio = audio?.subscribe?.(() => schedule()) || (() => {});
    schedule();
    return {
        update: schedule,
        destroy() {
            if (destroyed) return;
            destroyed = true;
            unsubscribeAudio();
            if (frame !== null) cancelAnimationFrame(frame);
            closePicker();
            for (const timer of zoomTimers.values()) clearTimeout(timer);
            for (const entry of cells) if (entry) { destroyContent(entry); entry.el.remove(); entry.bar.remove(); }
            cells.length = 0;
            headerSlot.remove();
            layoutControl.remove();
            root.style.cssText = rootStyle;
            placeComposite(null, { restore: true });
        },
    };
}

