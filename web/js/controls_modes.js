// The Modes table: one row per category, its choices as exclusive buttons,
// above the Individual Controls (plans/controls-modes-phase-b.md).
//
// Module-host contract: the panel owns pending saves (flushPending, track) and
// notices; the host owns reads and writes (freshView, applyChanges); this
// module owns its DOM and listeners and returns cleanup. Which choice shows is
// derived from each view (controls_modes_model.js), never stored.
import { ambiguityText, deriveModes, followNotice, includesText, rowOf } from "./controls_modes_model.js";
import { CHOICE_FAILURES, applyChoice } from "./controls_choice_apply.js";
import { createHoverTip } from "./controls_hover_tip.js";
import { needsOf, needsText, runSummary } from "./controls_modes_availability.js";
import { SETUP_GUIDE_URL, asksForSetup } from "./docs_links.js";
import { VISUALLY_HIDDEN_CSS, describedBy, placeInOrder, setText } from "./controls_dom_order.js";
import { panelMarkdownPlainText, renderPanelMarkdown } from "./panel_markdown.js";
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, RADIUS, SPACE, TYPE, setButtonVariant } from "./editor_theme.js";
import * as S from "./controls_panel_style.js";

let serial = 0;
const make = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
};
const unusable = choice => choice.state === "unavailable" || choice.state === "refused";
// The setup guide, where a need is something to install (Sonder App only: only its
// host answers availability). A link keeps Enter: the keyboard owner lets it through.
const setupLink = () => {
    const link = make("a", "How to install ↗");
    link.href = SETUP_GUIDE_URL; link.target = "_blank"; link.rel = "noopener noreferrer";
    link.style.cssText = `color:inherit;text-decoration:underline;`;
    return link;
};
const UNAVAILABLE = "This choice is unavailable. Open Manage.";
// How long a click on a grey choice waits to learn whether it still is.
const AVAILABILITY_CLICK_TIMEOUT_MS = 3000;
const headingCss = `font-size:${TYPE.t11}px;font-weight:${TYPE.fwBold};color:${COLORS.textSecondary};letter-spacing:0.02em;`;
// Tooltip text by description: every render repaints every button.
const PLAIN_CACHE_MAX = 256;
const plainCache = new Map();
function plainText(description) {
    if (!description) return "";
    let text = plainCache.get(description);
    if (text === undefined) {
        text = panelMarkdownPlainText(description);
        if (plainCache.size >= PLAIN_CACHE_MAX) plainCache.delete(plainCache.keys().next().value);
        plainCache.set(description, text);
    }
    return text;
}
const asHeading = el => { el.setAttribute("role", "heading"); el.setAttribute("aria-level", "3"); return el; };
// Read by assistive technology, never drawn.
const hiddenCss = VISUALLY_HIDDEN_CSS;
// A key a button never uses: it goes on to the editor's shortcuts.
const shortcutKey = e => !!(e.ctrlKey || e.metaKey || e.altKey) || e.key === "Escape";

export function mountModes(host, { flushPending, track, notify, inform = () => {}, openManage = () => {} }) {
    const panel = ++serial;
    let nextId = 0;
    const domId = () => `cm${panel}.${++nextId}`;
    const el = make("section"), heading = make("div", "Modes"), table = make("div"), runLine = make("div"),
        runTitle = make("span"), runToggle = make("button"), runHelp = setupLink(), runList = make("div"),
        uncategorized = make("button"), individual = make("div", "Individual Controls");
    el.dataset.controlsModes = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    // What the current run lacks (Sonder App): a status, so warningText.
    runLine.dataset.controlsModesRun = "true";
    runLine.style.cssText = `color:${COLORS.warningText};font-size:${TYPE.t11}px;overflow-wrap:anywhere;display:none;`;
    runToggle.type = "button";
    runToggle.dataset.controlsModesRunToggle = "true";
    runToggle.style.cssText = `${S.quietLineCss}margin-left:${SPACE.s1}px;color:inherit;text-decoration:underline;`;
    runToggle.addEventListener("click", () => { runOpen = !runOpen; repaint(); });
    runList.dataset.controlsModesRunList = "true";
    runList.style.whiteSpace = "pre-line";
    runHelp.dataset.controlsModesRunHelp = "true";
    runHelp.style.marginLeft = `${SPACE.s1}px`;
    runLine.append(runTitle, runToggle, runHelp, runList);
    asHeading(heading);
    asHeading(individual);
    heading.style.cssText = headingCss;
    table.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    uncategorized.type = "button";
    uncategorized.dataset.controlsModesUncategorized = "true";
    uncategorized.style.cssText = S.quietLineCss;
    uncategorized.addEventListener("click", () => openManage());
    individual.dataset.controlsIndividualHeading = "true";
    individual.style.cssText = `${headingCss}margin-top:${SPACE.s1}px;`;
    el.append(heading, runLine, table, uncategorized, individual);

    const rows = new Map(); // category id -> row parts
    const open = new Set(); // category ids whose ? block is open
    let view = {}, options = {}, busy = false, destroyed = false, runOpen = false;
    // A choice's short description on hover and focus (controls_hover_tip.js).
    const tips = createHoverTip(el, { register, mark: "controlsModeTip" });
    // What a choice lacks on this install (Sonder App; no host answer elsewhere).
    const availability = host.availability || null;
    const needsTextOf = id => needsText(needsOf(availability?.get(), id), availability?.packs());
    const unsubscribeAvailability = availability?.subscribe(() => render(view, options)) || (() => {});

    function paintChoice(button, { selected, outlined, dim, grey }) {
        setButtonVariant(button, selected ? "active" : "secondary", { padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px` });
        // setButtonVariant rewrites the whole style; these follow it.
        Object.assign(button.style, {
            // Every button of a category fills its grid cell, so all share one
            // width and the tallest one's height (grid-auto-rows:1fr).
            whiteSpace: "normal", overflowWrap: "anywhere", width: "100%", height: "100%", minWidth: "0",
            outline: outlined ? `1px solid ${COLORS.accent}` : "", outlineOffset: outlined ? "1px" : "",
            opacity: dim || grey || busy ? "0.42" : "", cursor: busy ? "progress" : grey ? "not-allowed" : "pointer",
        });
        button.setAttribute("aria-pressed", String(!!selected));
        // Never `disabled`: disabling the focused button would blur it.
        if (busy || grey) button.setAttribute("aria-disabled", "true");
        else button.removeAttribute("aria-disabled");
    }

    function buildRow(id) {
        const row = { id, el: make("div"), lead: make("div"), top: make("div"), name: make("div"), description: make("div"),
            status: make("div"), more: make("button", "?"), choices: make("div"), described: make("div"), block: make("div"),
            buttons: new Map(), key: null, blockKey: null };
        row.el.dataset.controlsModeCategory = id;
        row.el.style.cssText = `position:relative;display:flex;flex-wrap:wrap;gap:${SPACE.s2}px;align-items:flex-start;`
            + `border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;padding:${SPACE.s2}px;min-width:0;`;
        // The name above its choices at every width: beside them, the name's
        // column left the choices less room than stacking does.
        row.lead.style.cssText = `flex:1 1 100%;min-width:0;display:flex;flex-direction:column;gap:2px;`;
        row.top.style.cssText = `display:flex;align-items:flex-start;gap:${SPACE.s1}px;`;
        row.name.id = domId();
        row.name.style.cssText = `flex:1;min-width:0;font-weight:${TYPE.fwBold};overflow-wrap:anywhere;`;
        row.more.type = "button";
        row.more.dataset.controlsModeMore = "true";
        row.more.style.cssText = `${S.quietLineCss}flex:0 0 auto;`;
        row.block.id = domId();
        row.more.setAttribute("aria-controls", row.block.id);
        row.more.addEventListener("click", () => {
            if (open.has(id)) open.delete(id);
            else open.add(id);
            render(view, options);
        });
        row.top.append(row.name, row.more);
        row.description.style.cssText = `color:${COLORS.textSecondary};font-size:${TYPE.t11}px;overflow-wrap:anywhere;`
            + "display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;";
        row.status.dataset.controlsModeStatus = "true";
        row.status.style.cssText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};overflow-wrap:anywhere;`;
        row.statusTag = make("span");
        row.statusTag.style.cssText = S.tagCss;
        row.statusText = make("span");
        row.status.append(row.statusTag, row.statusText);
        row.lead.append(row.top, row.description, row.status);
        row.choices.setAttribute("role", "group");
        row.choices.setAttribute("aria-labelledby", row.name.id);
        // Columns share the width and rows share the tallest button's height; a
        // lone last button keeps the column width.
        row.choices.style.cssText = `flex:1 1 100%;min-width:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));`
            + `grid-auto-rows:1fr;gap:${SPACE.s1}px;`;
        row.block.dataset.controlsModeDetails = "true";
        row.block.style.cssText = `flex:1 1 100%;min-width:0;display:none;flex-direction:column;gap:${SPACE.s1}px;`
            + `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};overflow-wrap:anywhere;`;
        // Descriptions the buttons point at (aria-describedby): outside them,
        // so they never join a button's name.
        row.described.style.cssText = hiddenCss;
        row.el.append(row.lead, row.choices, row.block, row.described);
        return row;
    }

    // The choice buttons are rebuilt only when the row's choice list changes.
    function buildChoices(row, choices) {
        for (const parts of row.buttons.values()) parts.detach();
        row.buttons.clear();
        row.choices.replaceChildren();
        row.described.replaceChildren();
        for (const choice of choices) {
            const button = make("button"), label = make("span"), description = make("span");
            button.type = "button";
            button.dataset.controlsModeChoice = choice.id;
            description.id = domId();
            button.append(label);
            row.described.append(description);
            const presetId = choice.id;
            button.addEventListener("click", () => void choose(presetId));
            // Inside the row, so the panel's zoom and sticky header can't misplace it.
            const detach = tips.attach(button, () => row.buttons.get(presetId)?.text || "", { anchor: row.el });
            row.choices.append(button);
            row.buttons.set(presetId, { button, label, description, text: "", detach });
        }
    }

    function paintRow(row, mode, modes) {
        setText(row.name, mode.name);
        setText(row.description, mode.description);
        row.description.style.display = mode.description ? "-webkit-box" : "none";
        // Custom and Unavailable are tags; the ambiguity sentence stays plain.
        const tag = mode.status === "custom" ? "Custom" : mode.status === "unavailable" ? "Unavailable" : "";
        setText(row.statusTag, tag);
        row.statusTag.style.display = tag ? "" : "none";
        setText(row.statusText, mode.status === "ambiguous" ? ambiguityText(mode) : "");
        row.status.style.display = row.status.textContent ? "" : "none";
        if (busy) row.choices.setAttribute("aria-busy", "true");
        else row.choices.removeAttribute("aria-busy");
        const detailed = !!mode.description || mode.choices.some(choice => choice.description);
        row.more.hidden = !detailed;
        row.more.style.display = detailed ? "" : "none";
        row.more.setAttribute("aria-label", `About ${mode.name}`);
        const isOpen = detailed && open.has(mode.id);
        row.more.setAttribute("aria-expanded", String(isOpen));
        const matching = new Set(mode.matches.map(choice => choice.id));
        for (const choice of mode.choices) {
            const parts = row.buttons.get(choice.id);
            setText(parts.label, choice.name);
            const needs = needsTextOf(choice.id);
            // What it lacks first. A choice that includes others says so on its own line.
            parts.text = [needs, ...(unusable(choice) ? [choice.reason || UNAVAILABLE]
                : [plainText(choice.description), includesText(modes, choice)])].filter(Boolean).join("\n");
            setText(parts.description, parts.text);
            describedBy(parts.button, parts.description.id, !!parts.text);
            paintChoice(parts.button, { selected: mode.selected?.id === choice.id,
                outlined: mode.status === "ambiguous" && matching.has(choice.id), dim: unusable(choice), grey: !!needs });
        }
        row.block.style.display = isOpen ? "flex" : "none";
        const blockKey = isOpen ? JSON.stringify([mode.description, mode.selected?.id || null,
            mode.choices.map(choice => [choice.id, choice.name, choice.description, choice.state, choice.reason, needsTextOf(choice.id)])]) : null;
        if (blockKey === row.blockKey) return;
        row.blockKey = blockKey;
        row.block.replaceChildren();
        if (!isOpen) return;
        if (mode.description) row.block.append(make("div", mode.description));
        for (const choice of mode.choices) {
            const item = make("div"), title = make("strong", choice.name);
            item.style.cssText = `display:flex;flex-direction:column;gap:2px;color:${COLORS.text};`;
            const suffix = mode.selected?.id === choice.id ? " (selected)" : "";
            item.append(title);
            if (suffix) title.append(document.createTextNode(suffix));
            const needs = needsTextOf(choice.id);
            if (needs) {
                const line = make("div", needs);
                line.style.whiteSpace = "pre-line";
                item.append(line);
                if (asksForSetup(needsOf(availability?.get(), choice.id))) {
                    const help = setupLink();
                    help.dataset.controlsModeSetupHelp = choice.id;
                    item.append(help);
                }
            }
            if (choice.description) item.append(renderPanelMarkdown(choice.description));
            if (unusable(choice)) item.append(make("div", choice.reason || UNAVAILABLE));
            row.block.append(item);
        }
    }

    function render(next = host.getView(), opts = options) {
        if (destroyed) return;
        view = next || {};
        options = opts || {};
        const hidden = !!options.manage || !!String(options.query || "").trim() || view.state !== "ready" || !!view.error
            || !Array.isArray(view.preset_categories);
        const modes = hidden ? { rows: [], uncategorized: [] } : deriveModes(view);
        const alive = new Set();
        for (const mode of modes.rows) {
            alive.add(mode.id);
            let row = rows.get(mode.id);
            if (!row) rows.set(mode.id, row = buildRow(mode.id));
            const key = mode.choices.map(choice => choice.id).join("\u0000");
            if (key !== row.key) { row.key = key; buildChoices(row, mode.choices); }
            paintRow(row, mode, modes);
        }
        for (const [id, row] of rows) {
            if (alive.has(id)) continue;
            for (const parts of row.buttons.values()) parts.detach();
            row.el.remove();
            rows.delete(id);
        }
        placeInOrder(table, modes.rows.map(mode => rows.get(mode.id).el));
        const count = modes.uncategorized.length;
        setText(uncategorized, count === 1 ? "1 choice isn't in a category · Manage" : `${count} choices aren't in a category · Manage`);
        uncategorized.title = "Open Manage to put these choices in a category";
        uncategorized.hidden = !count;
        uncategorized.style.display = count ? "" : "none";
        const drawn = modes.rows.length > 0;
        heading.style.display = table.style.display = individual.style.display = drawn ? "" : "none";
        if (drawn) table.style.display = "flex";
        // Shown with no Modes too: a workflow without them can still lack a file.
        const lacking = hidden ? null : runSummary(availability?.get(), availability?.packs(), { hasModes: drawn });
        const needsListed = !!lacking?.needs.length;
        setText(runTitle, lacking?.title || "");
        setText(runToggle, runOpen ? "Hide" : "Show");
        runToggle.setAttribute("aria-expanded", String(runOpen));
        runToggle.style.display = needsListed ? "" : "none";
        runHelp.style.display = lacking && asksForSetup(availability?.get()?.run) ? "" : "none";
        setText(runList, lacking ? [...(runOpen ? lacking.needs : []), ...lacking.problems].join("\n") : "");
        runList.style.display = runList.textContent ? "" : "none";
        runLine.style.display = lacking ? "" : "none";
        el.style.display = drawn || count || lacking ? "flex" : "none";
        if (!drawn && !count && !lacking) tips.hide();
        // Once per render: the open tip's text or place may have changed.
        else tips.refresh();
    }
    const repaint = () => render(view, options);

    const notifyUnusable = choice => notify(
        [needsTextOf(choice.id).replaceAll("\n", " "), choice.reason || UNAVAILABLE].filter(Boolean).join(" "));

    async function choose(presetId) {
        if (busy || destroyed) return;
        const clickRow = rowOf(deriveModes(host.getView()), presetId);
        const clicked = clickRow?.choices.find(choice => choice.id === presetId);
        if (!clicked) return;
        if (unusable(clicked)) { notifyUnusable(clicked); return; }
        const workflow = () => host.workflows?.current?.().template_id;
        const startedOn = workflow();
        busy = true;
        repaint();
        let before = null;
        const outcome = await applyChoice(host, presetId, {
            flushPending,
            // The click states what the run should use: a choice that didn't
            // apply refuses a run waiting on it.
            track: work => track(work.then(result => !CHOICE_FAILURES.has(result.status)), true),
            isCurrent: () => !destroyed,
            // A view that isn't fresh decides nothing: the click applies.
            decide: (read, fresh) => {
                const modes = deriveModes(fresh ? read : host.getView());
                const row = fresh ? rowOf(modes, presetId) : clickRow;
                if (!row) { notify("This choice is no longer in a category. Open Manage."); return { status: "gone" }; }
                const choice = row.choices.find(item => item.id === presetId) || clicked;
                if (fresh && unusable(choice)) { notifyUnusable(choice); return { status: "gone" }; }
                const proceed = () => {
                    // Already the row's only match: applying again would only
                    // rewrite an advancing seed's number.
                    if (fresh && row.status === "selected" && row.selected.id === presetId) return { status: "already" };
                    if (fresh && row.status === "ambiguous" && row.matches.some(item => item.id === presetId)) {
                        notify(ambiguityText(row));
                        return { status: "already" };
                    }
                    before = modes;
                    return true;
                };
                if (!needsTextOf(presetId)) return proceed();
                // A grey choice: read again, since the file may have just arrived.
                // After the flush, so an edit made just before this click counts.
                // Only an answer that still names it stops the click: Generate
                // remains the refusal when the read fails or runs out of time.
                return availability.refresh({ fresh: true, timeoutMs: AVAILABILITY_CLICK_TIMEOUT_MS }).then(adopted => {
                    const still = adopted ? needsTextOf(presetId) : "";
                    if (!still) return proceed();
                    notify(still.replaceAll("\n", " "));
                    return { status: "needs_install" };
                });
            },
        });
        busy = false;
        if (destroyed) return;
        if (outcome.status === "flush_failed") notify("The choice was not applied because a Control could not be saved.");
        else if (outcome.status === "refused" || outcome.status === "not_confirmed") {
            notify(outcome.reason || "The choice could not be applied.", outcome.detail);
        }
        repaint();
        // Not tracked: Generate never waits for the notice's read.
        if (outcome.status !== "applied" || !outcome.fresh || workflow() !== startedOn) return;
        const after = await host.freshView();
        if (destroyed || !after.fresh || workflow() !== startedOn) return;
        const message = followNotice(before, deriveModes(after.view), presetId, after.view);
        if (message) inform(message);
    }

    const unregister = register({ id: `controls-modes:${panel}`, priority: PRIORITY.TEXT_EDITOR, keydown: e => {
        const active = document.activeElement;
        const inside = el.contains(active);
        if (e.isComposing || e.keyCode === 229) return inside ? PRESERVE_DEFAULT : false;
        // An open tooltip's Escape is the tip's own (controls_hover_tip.js).
        if (!inside) return false;
        // Shortcuts (Ctrl+Z, Ctrl+S) and Escape keep working from a choice.
        if (shortcutKey(e)) return false;
        // Other keys stay with the choice (Enter and Space press it); none
        // reach the editor's or the canvas's single-key shortcuts.
        return PRESERVE_DEFAULT;
    } });

    return {
        el,
        render,
        get busy() { return busy; },
        destroy() {
            destroyed = true;
            unsubscribeAvailability();
            tips.destroy();
            unregister();
            rows.clear();
            el.remove();
        },
    };
}
