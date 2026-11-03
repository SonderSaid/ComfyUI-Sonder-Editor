// Manage's Modes section: categories, their choices and warnings, and the
// category and choice editors (plans/controls-modes-phase-b.md §6). DOM only:
// the validating host captures values and stores every edit.
//
// Selecting a saved choice loads it first, through the Modes table's own apply
// (controls_choice_apply.js), so its editor edits what the choice sets
// (plans/live-workflow-phase-4-controls-modes.md D8).
//
// The list is keyed and updated in place, and a queued write never disables a
// button: a click whose mousedown blurred a field (whose save re-renders this)
// must still land on the button it pressed.
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, RADIUS, SPACE, TYPE, chromeInputCss, setButtonVariant } from "./editor_theme.js";
import { blankName, mintBindingId } from "./control_bindings.js";
import { deriveModes } from "./controls_modes_model.js";
import { applyChoice } from "./controls_choice_apply.js";
import { placeInOrder, setText } from "./controls_dom_order.js";

let serial = 0;
const CATEGORY_DESCRIPTION_MAX = 300; // control_manifest.CATEGORY_DESCRIPTION_MAX
const make = (tag, text) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; };
const button = text => { const el = make("button", text); el.type = "button"; setButtonVariant(el, "secondary", {padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px`}); return el; };
// A move with nowhere to go: dimmed and inert, but never `disabled` (see above).
function setInert(el, inert) {
    if (inert) el.setAttribute("aria-disabled", "true");
    else el.removeAttribute("aria-disabled");
    el.style.opacity = inert ? "0.42" : "";
    el.style.cursor = inert ? "default" : "pointer";
}
const inert = el => el.attributes?.["aria-disabled"] === "true" || el.getAttribute?.("aria-disabled") === "true";
const smallText = `font-size:${TYPE.t11}px;color:${COLORS.textSecondary};overflow-wrap:anywhere;`;
const STATE_TEXT = { match: " · current values", unavailable: " · unavailable", refused: " · unavailable" };
const ORIGIN_TEXT = { app_copy: "App copy", app: "App only" };
// A reason as the middle of a sentence: no closing stop of its own.
const clause = text => String(text || "").trim().replace(/[.\s]+$/, "");

export function mountModesManage(host, { track, notify, success = () => {}, flushPending = async () => true, changed = () => {} }) {
    const el = make("section"), heading = make("div", "Modes"), list = make("div"), loose = make("div"),
        looseTitle = make("div", "Not in a category"), looseList = make("div"), addCategory = button("+ Category"),
        editor = make("div"), editorTitle = make("div"), loadLine = make("div"), name = make("input"), description = make("textarea"), counter = make("div"),
        categoryLabel = make("label"), categorySelect = make("select"), siblings = make("div"), siblingsTitle = make("div"), siblingsList = make("div"),
        missing = make("div"), actions = make("div"), save = button("Create"), remove = button("Delete"), reset = button("Reset to workflow's version");
    el.dataset.controlsModesManage = "true";
    el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;`;
    heading.tabIndex = -1;
    heading.style.cssText = `font-weight:${TYPE.fwBold};outline:none;`;
    list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s2}px;`;
    loose.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    looseTitle.style.cssText = `${smallText}font-weight:${TYPE.fwBold};`;
    looseList.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    loose.append(looseTitle, looseList);
    addCategory.dataset.controlsAddCategory = "true";
    addCategory.title = "Add a category of choices; one of them is chosen at a time";
    editor.dataset.controlsModesEditor = "true";
    editor.style.cssText = `display:none;flex-direction:column;gap:${SPACE.s1}px;padding:${SPACE.s2}px;`
        + `border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;`;
    editorTitle.style.cssText = `${smallText}font-weight:${TYPE.fwBold};`;
    loadLine.dataset.controlsModesLoad = "true";
    loadLine.setAttribute("role", "status");
    loadLine.style.cssText = smallText;
    for (const input of [name, description]) { input.style.cssText = chromeInputCss({minWidth: "0"}) + "width:100%;box-sizing:border-box;"; input.dataset.sonderTextUndo = "true"; }
    description.dataset.modesDescription = "true";
    counter.style.cssText = `${smallText}align-self:flex-end;`;
    categoryLabel.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1}px;${smallText}`;
    categorySelect.style.cssText = chromeInputCss({minWidth: "0"}) + "flex:1;";
    categorySelect.setAttribute("aria-label", "Category");
    categoryLabel.append(make("span", "Category"), categorySelect);
    // Choices from other categories this one applies with its own (Phase 6A).
    const also = make("div"), alsoTitle = make("div", "Also applies"), alsoNote = make("div"), alsoList = make("div");
    also.dataset.controlsModesIncludes = "true";
    also.style.cssText = `display:none;flex-direction:column;gap:2px;`;
    alsoTitle.style.cssText = `${smallText}font-weight:${TYPE.fwBold};`;
    alsoNote.style.cssText = smallText;
    alsoList.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    also.append(alsoTitle, alsoNote, alsoList);
    // The other choices of the edited one's category, to move between them.
    siblings.dataset.controlsModesSiblings = "true";
    siblings.style.cssText = `display:none;flex-direction:column;gap:2px;`;
    siblingsTitle.style.cssText = `${smallText}font-weight:${TYPE.fwBold};`;
    siblingsList.style.cssText = `display:flex;flex-wrap:wrap;gap:${SPACE.s1}px;`;
    siblings.append(siblingsTitle, siblingsList);
    actions.style.cssText = `display:flex;flex-wrap:wrap;gap:${SPACE.s1}px;`;
    actions.append(save, remove, reset);
    editor.append(editorTitle, loadLine, name, description, counter, categoryLabel, also, siblings, missing, actions);
    el.append(heading, list, loose, addCategory, editor);

    // `sel`: what the editor holds. {kind: "category"|"choice", id, draft, category?}
    // (a new choice remembers the category it goes into). `members` holds
    // setting ids, `switches` node-switch ids; a choice may hold both.
    let view = {}, sel = null, members = new Set(), switches = new Set(), membershipDirty = false, destroyed = false;
    // `staleView`: the view a write left behind when no read after it was
    // adopted; a `choices` list built from it would undo that write.
    let tail = Promise.resolve(), queued = 0, creating = null, staleView = null;
    // The edited choice's load: {id, status: loading|loaded|not_loaded|not_confirmed,
    // reason, view (the view its outcome came with), work}. Each selection takes
    // a new `token`, so a load it superseded writes nothing.
    let load = null, token = 0;
    // The choice last loaded and the view its load left: kept while a category
    // or a draft is edited, so tuning values and then loading another choice
    // still asks. Replaced by the next load.
    let lastLoaded = null;
    const loads = new Set(); // loads still running, superseded ones included
    const pendingText = new Map(), blocks = new Map(), looseRows = new Map(), siblingButtons = new Map();
    // Also applies: a queued select's value until its write lands, keyed by
    // choice and category (`pendingIncludes`), a draft's selections
    // (`draftIncludes`, by category), kept lines.
    const pendingIncludes = new Map(), draftIncludes = new Map(), alsoLines = new Map();
    const categories = () => Array.isArray(view.preset_categories) ? view.preset_categories : [];
    const presets = () => Array.isArray(view.presets) ? view.presets : [];
    const choiceRow = id => presets().find(p => p.id === id);
    const categoryRow = id => categories().find(c => c.id === id);
    // The category that shows a choice (the first that lists it).
    const homeOf = id => categories().find(c => (c.choices || []).includes(id)) || null;
    // A choice two categories list cannot move: moving it adds it to a third.
    const listedTwice = id => categories().some(c => (c.warnings || []).some(w => w.code === "duplicate" && w.other && w.choice === id));
    const isChoice = () => sel?.kind === "choice";
    // A draft whose Create is queued is no longer unsaved.
    const dirty = () => !!sel?.draft && creating !== sel.id
        && (!!name.value || !!description.value || (isChoice() && (members.size > 0 || switches.size > 0)));
    const busy = () => queued > 0 || loads.size > 0;
    // The last loaded choice no longer matches the live values: they were
    // tuned after it loaded (a view adopted since then says so).
    const diverged = () => !!lastLoaded && view !== lastLoaded.view && choiceRow(lastLoaded.id)?.state === "differs";
    // Asks about a draft, unsaved ticks of a saved choice, and, before loading
    // `next`, live values tuned since the last choice loaded (the Sonder App
    // has no Undo). A queued write is no reason to refuse a click (its
    // blur-save is often what queued it). The panel waits for the queue
    // before closing Manage, and refuses a workflow switch while it runs.
    function canLeave(next = null) {
        if (sel?.draft) return !dirty() || globalThis.confirm(isChoice() ? "Discard this unsaved choice?" : "Discard this unsaved category?");
        const current = isChoice() ? choiceRow(sel.id)?.name || name.value : "";
        if (isChoice() && membershipDirty && !globalThis.confirm(`Discard the unsaved setting changes to “${current}”?`)) return false;
        const loading = next?.kind === "choice" && !next.draft;
        if (loading && diverged() && !globalThis.confirm(`Your Controls changed since loading “${choiceRow(lastLoaded.id)?.name || lastLoaded.id}” `
            + `and aren't saved to it${ownRowsText(lastLoaded.id)}. Load “${choiceRow(next.id)?.name || next.id}” anyway?`)) return false;
        return true;
    }
    // Update never saves an included choice's Controls: they belong to its own row.
    function ownRowsText(id) {
        const names = (choiceRow(id)?.includes || []).filter(item => item.status !== "missing")
            .map(item => item.name || choiceRow(item.id)?.name).filter(Boolean);
        if (!names.length) return "";
        const text = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
        return ` (${text} ${names.length === 1 ? "is" : "are"} set in ${names.length === 1 ? "its" : "their"} own row${names.length === 1 ? "" : "s"})`;
    }

    function select(next) {
        if (!canLeave(next)) return false;
        sel = next; membershipDirty = false; creating = null; draftIncludes.clear();
        const row = next?.kind === "choice" ? choiceRow(next.id) : next ? categoryRow(next.id) : null;
        name.value = next?.draft ? "" : row?.name || "";
        description.value = next?.draft ? "" : row?.description || "";
        members = new Set(isChoice() && !next.draft ? (row?.members || []).map(m => m.binding_id) : []);
        switches = new Set(isChoice() && !next.draft ? (row?.execution || []).map(m => m.execution_id) : []);
        // A category or a draft edits only; a saved choice loads first.
        const mine = ++token;
        load = null;
        if (isChoice() && !next.draft) startLoad(next.id, mine);
        render(view); changed(); return true;
    }

    // Writes the choice's saved values and node switches into the Controls, as
    // its Modes button does: one native Undo step on the canvas. Never captures.
    function startLoad(id, mine) {
        const current = () => !destroyed && token === mine;
        lastLoaded = null;
        const work = applyChoice(host, id, {
            flushPending,
            // Runs wait for a load, but it never refuses one: a load that
            // fails leaves the Controls as they were, and says so. So it never
            // blocks the next selection's flush either.
            track: outcome => track(outcome.then(() => true), {[`preset:${id}:apply`]: true}),
            isCurrent: current,
            // Only a fresh view's match skips the write: a stale one might
            // show a match the Controls no longer hold. Skipping never
            // rewrites an advancing seed's number.
            decide: (read, fresh) => {
                // A view listing no presets says nothing about this one: the
                // host's own answer decides.
                if (!fresh || !Array.isArray(read.presets)) return true;
                const row = read.presets.find(p => p.id === id);
                if (!row) return {status: "gone", reason: "This choice no longer exists"};
                return row.state === "match" ? {status: "already"} : true;
            },
        });
        load = {id, status: "loading", reason: "", view: null, work};
        const mineLoad = load;
        loads.add(work);
        void work.then(outcome => {
            loads.delete(work);
            if (destroyed) return;
            if (current() && load === mineLoad && sel?.id === id) {
                const label = choiceRow(id)?.name || name.value || id;
                if (outcome.status === "applied" || outcome.status === "already") {
                    load = {...mineLoad, status: "loaded", view: host.getView()};
                    lastLoaded = {id, view: load.view};
                } else if (outcome.status === "not_confirmed") {
                    load = {...mineLoad, status: "not_confirmed"};
                    notify(`Couldn't confirm “${label}” loaded. Check the Controls before updating it.`);
                } else if (outcome.status !== "superseded") {
                    const reason = clause(outcome.status === "flush_failed" ? "A Control could not be saved"
                        : outcome.reason || "It could not be applied");
                    load = {...mineLoad, status: "not_loaded", reason};
                    notify(`“${label}” was not loaded: ${reason}.`, outcome.detail);
                }
            }
            render(host.getView()); changed();
        });
    }

    // Every edit queues behind the last one and is built when its turn comes,
    // from the view the previous edit produced (it waits for a fresh one), so
    // two quick edits of one list never build on the same stale list.
    // `done`: the success message (or a function giving it) for a write
    // accepted by the canvas or stored by the app, announced once.
    // `settle`: runs when the write is over, accepted or not, before the repaint.
    function enqueue(what, build, { flush = false, text = null, after = null, done = null, settle = null } = {}) {
        if (destroyed) return Promise.resolve(false);
        if (text) pendingText.set(text.target, text.value);
        // Snapshot saves before this edit joins the panel's gate.
        const flushed = flush ? flushPending() : Promise.resolve(true);
        queued++;
        const work = tail.then(async () => {
            try {
                if (!await flushed) { notify(`The ${what} was not saved because a Control could not be saved.`); return false; }
                if (destroyed) return false;
                // The last write's view is still the one shown: read once more.
                if (staleView && host.getView() === staleView && host.freshView) {
                    if ((await host.freshView()).fresh) staleView = null;
                    if (destroyed) return false;
                }
                const changes = build(host.getView());
                if (!changes) return false;
                const result = await host.applyChanges(changes);
                if (!result?.ok) { notify(result?.rejected?.[0]?.reason || `The ${what} could not be saved.`); return false; }
                after?.();
                const message = typeof done === "function" ? done() : done;
                if (message) success(message);
                if (host.freshView && !destroyed) staleView = (await host.freshView()).fresh ? null : host.getView();
                return true;
            } catch (error) { notify(`The ${what} could not be saved.`, error?.message); return false; }
            finally {
                if (text && pendingText.get(text.target) === text.value) pendingText.delete(text.target);
                settle?.();
                queued--;
                if (!destroyed) { render(host.getView()); changed(); }
            }
        });
        tail = work;
        // Modes edits never change the run (changesRun is false for them all).
        return track(work, {});
    }
    // The displayed choices of a category in `current`, edited by `edit`.
    // Writing the displayed list drops ids it doesn't show: missing choices and
    // ones an earlier category claims (the validator refuses unknown ids).
    function choicesChange(current, cid, edit) {
        if (staleView && current === staleView) { notify("Controls are still updating. Try again."); return null; }
        const row = (current.preset_categories || []).find(c => c.id === cid);
        if (!row) { notify("This category no longer exists. Open Manage again."); return null; }
        return {[`preset_category:${cid}:choices`]: edit([...(row.choices || [])])};
    }

    // A saved entry's name or description, queued. Its own toast unless the
    // caller announces it (`quiet`); null when nothing changed.
    function saveText(field, input, { quiet = false } = {}) {
        if (!sel || sel.draft) return null;
        const row = isChoice() ? choiceRow(sel.id) : categoryRow(sel.id);
        if (!row) return null;
        const prefix = `${isChoice() ? "preset" : "preset_category"}:${sel.id}`, target = `${prefix}:${field}`, value = input.value;
        // Compared with a save still queued, so a blur-save then Update never
        // sends (or announces) the same text twice.
        if (value === (pendingText.has(target) ? pendingText.get(target) : row[field] || "")) return null;
        if (field === "name" && blankName(value)) { notify("A name can't be empty."); input.value = row.name; return null; }
        const shown = pendingText.get(`${prefix}:name`) ?? row.name;
        const done = quiet ? null : field === "name" ? `Renamed to “${value}”.` : `Description of “${shown}” saved.`;
        return enqueue(isChoice() ? "choice" : "category", () => ({[target]: value}), {text: {target, value}, done});
    }
    name.addEventListener("blur", () => void saveText("name", name));
    description.addEventListener("blur", () => void saveText("description", description));
    description.addEventListener("input", () => paintCounter());
    save.addEventListener("mousedown", e => e.preventDefault());
    save.addEventListener("click", () => void saveEditor());
    async function saveEditor() {
        if (!sel) return;
        if (sel.draft) {
            if (creating === sel.id) return; // a second click on the same draft
            if (blankName(name.value)) { notify(isChoice() ? "Name the choice first." : "Name the category first."); name.focus(); return; }
            creating = sel.id;
        }
        const id = sel.id, draft = !!sel.draft, into = sel.category || null;
        if (!draft && isChoice()) {
            // Update captures what the Controls hold: wait for this choice's
            // load, and ask when it didn't load (a Loaded choice never asks).
            const pending = load?.id === id && load.status === "loading" ? load : null;
            if (pending) await pending.work.catch(() => {});
            if (destroyed) return;
            // Never captured into another choice: the click is dropped, and says so.
            if (sel?.id !== id || sel.draft) { notify("Update didn't run: another choice was selected while this one loaded."); return; }
            if (load?.id === id && ["not_loaded", "not_confirmed"].includes(load.status)
                && !globalThis.confirm(`The Controls may not hold this choice's settings. Update “${choiceRow(id)?.name || name.value}” from them anyway?`)) return;
        }
        // One click, one toast: the capture's message names a rename.
        let renamed = false;
        if (!draft) {
            saveText("name", name, {quiet: isChoice()})?.then(ok => { renamed = ok; });
            saveText("description", description, {quiet: isChoice()});
        }
        const done = () => { if (sel?.id === id) { sel = {...sel, draft: false}; delete sel.category; membershipDirty = false; creating = null; } };
        if (!isChoice()) {
            if (!draft) return;
            const fields = {name: name.value, description: description.value};
            void enqueue("category", () => ({[`preset_category:${id}:create`]: fields}), {after: done, done: `Category “${fields.name}” created.`})
                .then(ok => { if (!ok && creating === id) creating = null; });
            return;
        }
        const capture = {members: [...members], ...(switches.size ? {execution: [...switches]} : {})};
        // A draft's Also applies selections, except one in the category it goes into.
        const includes = [...draftIncludes].filter(([cid, pid]) => pid && cid !== into).map(([, pid]) => pid);
        const fields = draft ? {...capture, name: name.value, description: description.value, ...(includes.length ? {includes} : {})} : capture;
        const label = name.value;
        const message = () => {
            if (!draft) return `Choice “${label}” ${renamed ? "renamed and updated" : "updated"} from current values.`;
            const category = into && categoryRow(into)?.name;
            return category ? `Choice “${label}” created in “${category}”.` : `Choice “${label}” created.`;
        };
        void enqueue("choice", current => {
            const changes = {[`preset:${id}:${draft ? "create" : "capture"}`]: fields};
            if (!draft || !into) return changes;
            const listed = choicesChange(current, into, choices => [...choices, id]);
            return listed && {...changes, ...listed};
        }, {flush: true, after: done, done: message}).then(ok => { if (!ok && creating === id) creating = null; });
    }
    remove.addEventListener("click", () => {
        if (!sel || sel.draft) return;
        const id = sel.id;
        if (isChoice()) {
            if (host.capabilities?.hide && !globalThis.confirm(`Delete the choice “${choiceRow(id)?.name || name.value}”?`)) return;
            void enqueue("choice", () => ({[`preset:${id}:remove`]: true}), {after: () => { if (sel?.id === id) sel = null; }});
            return;
        }
        const row = categoryRow(id), count = (row?.choices || []).length;
        if (count && !globalThis.confirm(`Delete “${row.name}”? Its ${count === 1 ? "choice stays" : `${count} choices stay`}, under Not in a category.`)) return;
        void enqueue("category", () => ({[`preset_category:${id}:remove`]: true}), {after: () => { if (sel?.id === id) sel = null; }});
    });
    reset.addEventListener("click", () => {
        if (!sel || sel.draft) return;
        const id = sel.id, target = `${isChoice() ? "preset" : "preset_category"}:${id}:reset`;
        void enqueue(isChoice() ? "choice" : "category", () => ({[target]: true}), {after: () => { if (sel?.id === id) sel = null; }});
    });
    categorySelect.addEventListener("change", () => {
        if (!isChoice()) return;
        const to = categorySelect.value || null;
        if (sel.draft) { sel = {...sel, category: to}; return; }
        const id = sel.id;
        void enqueue("choice", current => {
            const from = (current.preset_categories || []).find(c => (c.choices || []).includes(id))?.id || null;
            if (from === to) return null;
            const changes = {};
            if (from) {
                const removed = choicesChange(current, from, choices => choices.filter(c => c !== id));
                if (!removed) return null;
                Object.assign(changes, removed);
            }
            if (to) {
                const added = choicesChange(current, to, choices => [...choices, id]);
                if (!added) return null;
                Object.assign(changes, added);
            }
            return changes;
        });
    });

    function moveCategory(cid, step) {
        void enqueue("category", current => {
            const order = (current.preset_categories || []).map(c => c.id), at = order.indexOf(cid);
            if (at < 0 || at + step < 0 || at + step >= order.length) return null;
            return {[`preset_category:${cid}:move`]: {before: step < 0 ? order[at - 1] : order[at + 2] ?? null}};
        });
    }
    function moveChoice(cid, pid, step) {
        void enqueue("category", current => choicesChange(current, cid, choices => {
            const at = choices.indexOf(pid), to = at + step;
            if (at < 0 || to < 0 || to >= choices.length) return choices;
            [choices[at], choices[to]] = [choices[to], choices[at]];
            return choices;
        }));
    }

    function choiceLine(pid, cid) {
        const row = {el: make("div"), pick: button(""), up: cid ? button("↑") : null, down: cid ? button("↓") : null};
        row.el.dataset.controlsModesChoice = pid;
        row.el.style.cssText = `display:flex;gap:${SPACE.s1}px;min-width:0;`;
        Object.assign(row.pick.style, {flex: "1", minWidth: "0", whiteSpace: "normal", overflowWrap: "anywhere", textAlign: "left"});
        row.pick.addEventListener("click", () => select({kind: "choice", id: pid, draft: false}));
        row.el.append(row.pick);
        if (cid) {
            row.up.title = "Move choice up"; row.down.title = "Move choice down";
            row.up.setAttribute("aria-label", "Move choice up"); row.down.setAttribute("aria-label", "Move choice down");
            row.up.addEventListener("click", () => { if (!inert(row.up)) moveChoice(cid, pid, -1); });
            row.down.addEventListener("click", () => { if (!inert(row.down)) moveChoice(cid, pid, 1); });
            row.el.append(row.up, row.down);
        }
        return row;
    }
    function paintChoiceLine(line, pid, first, last) {
        const preset = choiceRow(pid);
        setText(line.pick, `${preset?.name || pid}${STATE_TEXT[preset?.state] || ""}`);
        line.pick.title = preset?.reason || "";
        line.pick.setAttribute("aria-pressed", String(isChoice() && sel.id === pid));
        if (line.up) { setInert(line.up, first); setInert(line.down, last); }
    }

    function buildBlock(cid) {
        const block = {el: make("div"), header: make("div"), title: make("span"), meta: make("span"), up: button("↑"), down: button("↓"),
            edit: button("Edit"), warnings: make("div"), choices: make("div"), add: button("+ Choice"), rows: new Map(), warningsKey: null};
        block.el.dataset.controlsModesCategory = cid;
        block.el.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;padding:${SPACE.s2}px;`
            + `border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;min-width:0;`;
        block.header.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1}px;min-width:0;`;
        block.title.style.cssText = `flex:1;min-width:0;font-weight:${TYPE.fwBold};overflow-wrap:anywhere;`;
        block.meta.style.cssText = smallText;
        block.up.title = "Move category up"; block.down.title = "Move category down";
        block.up.setAttribute("aria-label", "Move category up"); block.down.setAttribute("aria-label", "Move category down");
        block.up.addEventListener("click", () => { if (!inert(block.up)) moveCategory(cid, -1); });
        block.down.addEventListener("click", () => { if (!inert(block.down)) moveCategory(cid, 1); });
        block.edit.addEventListener("click", () => select({kind: "category", id: cid, draft: false}));
        block.header.append(block.title, block.meta, block.up, block.down, block.edit);
        block.warnings.style.cssText = `display:flex;flex-direction:column;gap:2px;${smallText}color:${COLORS.warningText};`;
        block.choices.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
        block.add.title = "Add a choice to this category: tick In this choice on each Control it sets, then Create";
        block.add.style.alignSelf = "flex-start";
        block.add.addEventListener("click", () => {
            if (select({kind: "choice", id: mintBindingId(), draft: true, category: cid})) name.focus();
        });
        block.el.append(block.header, block.warnings, block.choices, block.add);
        return block;
    }
    function paintWarnings(block, row) {
        const warnings = row.warnings || [];
        const key = JSON.stringify(warnings);
        if (key === block.warningsKey) return;
        block.warningsKey = key;
        block.warnings.replaceChildren(...warnings.map(warning => {
            const line = make("div");
            line.style.cssText = `display:flex;flex-wrap:wrap;align-items:center;gap:${SPACE.s1}px;`;
            const text = warning.code === "missing" ? `${warning.message} Editing this category's choices removes them.`
                : warning.code === "duplicate" && warning.other ? `${warning.message} Editing this category's choices removes it here.`
                    : warning.message;
            line.append(make("span", text));
            const action = warning.code === "missing" ? "Remove missing"
                : warning.code === "duplicate" ? warning.other ? "Remove from this category" : "Remove the repeat" : "";
            if (action) {
                const fix = button(action);
                // Writing the displayed list is the fix: it holds neither.
                fix.addEventListener("click", () => void enqueue("category", current => choicesChange(current, row.id, choices => choices)));
                line.append(fix);
            }
            return line;
        }));
        block.warnings.style.display = warnings.length ? "flex" : "none";
    }

    function paintCounter() {
        setText(counter, `${[...description.value].length}/${CATEGORY_DESCRIPTION_MAX}`);
    }

    function render(next = host.getView()) {
        if (destroyed) return;
        view = next || {};
        // An edited entry gone from the view (deleted elsewhere, Undo) leaves the
        // editor. A view that lists none (loading, an error, a mounted tab's
        // canvas reconnecting) says nothing about it: the editor and its load stay.
        const listed = Array.isArray(isChoice() ? view.presets : view.preset_categories);
        if (sel && !sel.draft && listed && !(isChoice() ? choiceRow(sel.id) : categoryRow(sel.id))) { sel = null; membershipDirty = false; }
        const current = sel && !sel.draft ? (isChoice() ? choiceRow(sel.id) : categoryRow(sel.id)) : null;
        if (isChoice() && current && !membershipDirty) {
            members = new Set((current.members || []).map(m => m.binding_id));
            switches = new Set((current.execution || []).map(m => m.execution_id));
        }
        if (current) {
            const prefix = isChoice() ? "preset" : "preset_category";
            if (document.activeElement !== name && !pendingText.has(`${prefix}:${sel.id}:name`)) name.value = current.name || "";
            if (document.activeElement !== description && !pendingText.has(`${prefix}:${sel.id}:description`)) description.value = current.description || "";
        }

        const rows = categories(), alive = new Set();
        rows.forEach((row, i) => {
            alive.add(row.id);
            let block = blocks.get(row.id);
            if (!block) blocks.set(row.id, block = buildBlock(row.id));
            setText(block.title, row.name);
            const count = (row.choices || []).length;
            setText(block.meta, [`${count} ${count === 1 ? "choice" : "choices"}`, ORIGIN_TEXT[row.origin]].filter(Boolean).join(" · "));
            block.edit.setAttribute("aria-pressed", String(sel?.kind === "category" && sel.id === row.id));
            block.edit.setAttribute("aria-label", `Edit category ${row.name}`);
            setInert(block.up, i === 0);
            setInert(block.down, i === rows.length - 1);
            paintWarnings(block, row);
            const ids = row.choices || [], shown = new Set(ids);
            ids.forEach((pid, at) => {
                let line = block.rows.get(pid);
                if (!line) block.rows.set(pid, line = choiceLine(pid, row.id));
                paintChoiceLine(line, pid, at === 0, at === ids.length - 1);
            });
            for (const [pid, line] of block.rows) if (!shown.has(pid)) { line.el.remove(); block.rows.delete(pid); }
            placeInOrder(block.choices, ids.map(pid => block.rows.get(pid).el));
        });
        for (const [cid, block] of blocks) if (!alive.has(cid)) { block.el.remove(); blocks.delete(cid); }
        placeInOrder(list, rows.map(row => blocks.get(row.id).el));

        const uncategorized = deriveModes(view).uncategorized.map(p => p.id), looseIds = new Set(uncategorized);
        for (const pid of uncategorized) {
            let line = looseRows.get(pid);
            if (!line) looseRows.set(pid, line = choiceLine(pid, null));
            paintChoiceLine(line, pid, true, true);
        }
        for (const [pid, line] of looseRows) if (!looseIds.has(pid)) { line.el.remove(); looseRows.delete(pid); }
        placeInOrder(looseList, uncategorized.map(pid => looseRows.get(pid).el));
        loose.style.display = uncategorized.length ? "flex" : "none";

        renderEditor(current);
    }

    function renderEditor(current) {
        editor.style.display = sel ? "flex" : "none";
        if (!sel) return;
        const choice = isChoice();
        const into = sel.draft ? categoryRow(sel.category)?.name : null;
        setText(editorTitle, choice
            ? sel.draft ? (into ? `New choice in “${into}”` : "New choice") : "Choice"
            : sel.draft ? "New category" : "Category");
        name.placeholder = choice ? "Choice name" : "Category name";
        name.setAttribute("aria-label", choice ? "Choice name" : "Category name");
        description.placeholder = choice ? "Choice description (Markdown)" : "Category description";
        description.setAttribute("aria-label", choice ? "Choice description" : "Category description");
        description.maxLength = choice ? 1000 : CATEGORY_DESCRIPTION_MAX;
        counter.style.display = choice ? "none" : "";
        paintCounter();
        setText(save, sel.draft ? "Create" : "Update from current values");
        save.hidden = !sel.draft && !choice;
        save.style.display = save.hidden ? "none" : "";
        save.title = choice && !sel.draft ? "Capture the ticked Controls' current values into this choice" : "";
        remove.hidden = !!sel.draft;
        remove.style.display = remove.hidden ? "none" : "";
        reset.hidden = !!sel.draft || current?.origin !== "app_copy";
        reset.style.display = reset.hidden ? "none" : "";

        categoryLabel.style.display = choice ? "flex" : "none";
        if (choice) {
            const options = [["", "Not in a category"], ...categories().map(c => [c.id, c.name])];
            const key = JSON.stringify(options);
            if (categorySelect.dataset.key !== key) {
                categorySelect.dataset.key = key;
                categorySelect.replaceChildren(...options.map(([value, text]) => new Option(text, value)));
            }
            categorySelect.value = sel.draft ? sel.category || "" : homeOf(sel.id)?.id || "";
            const twice = !sel.draft && listedTwice(sel.id);
            categorySelect.disabled = twice;
            categorySelect.title = twice ? "Listed in two categories. Remove it from one first." : "";
        }
        paintAlso(choice);
        paintSiblings(choice ? categoryRow(sel.draft ? sel.category : homeOf(sel.id)?.id) : null);
        paintLoad(choice && !sel.draft);

        const ids = new Set((view.bindings || []).map(b => b.binding_id));
        const absent = choice ? (current?.members || []).filter(m => !ids.has(m.binding_id)) : [];
        const switchIds = new Set((view.execution || []).map(d => d.execution_id));
        const gone = choice ? (current?.execution || []).filter(m => !switchIds.has(m.execution_id)) : [];
        // Kept lines, updated in place, like the rows' own (a press keeps its click).
        const lines = absent.length || gone.length ? [missingTitle] : [];
        for (const member of absent) lines.push(memberControl({binding_id: member.binding_id, status: "missing", absent: true},
            missingLines.get(`binding:${member.binding_id}`)));
        for (const member of gone) lines.push(switchControl({execution_id: member.execution_id, status: "missing", absent: true},
            missingLines.get(`execution:${member.execution_id}`)));
        missingLines = new Map(lines.filter(line => line !== missingTitle).map(line => [line.dataset.controlsMember, line]));
        for (const child of [...missing.children]) if (!lines.includes(child)) child.remove();
        placeInOrder(missing, lines);
    }

    // Also applies: one select per other category (Nothing or one of its
    // choices) and a line with Remove for each inclusion no select shows (a
    // choice in no category, or one that no longer exists). The server owns
    // the rules; a select disables only what the rows show for certain.
    const includesOf = (current, id) => ((current.presets || []).find(p => p.id === id)?.includes || []).map(item => item.id);
    const homeIn = (current, pid) => (current.preset_categories || []).find(c => (c.choices || []).includes(pid))?.id || null;
    function writeIncludes(id, edit, options) {
        return enqueue("choice", current => {
            if (staleView && current === staleView) { notify("Controls are still updating. Try again."); return null; }
            if (!(current.presets || []).some(p => p.id === id)) { notify("This choice no longer exists. Open Manage again."); return null; }
            const before = includesOf(current, id), next = edit(before, current);
            return next.join("\u0000") === before.join("\u0000") ? null : {[`preset:${id}:includes`]: next};
        }, options);
    }
    function chooseIncluded(cid, pid) {
        if (!isChoice()) return;
        if (sel.draft) { draftIncludes.set(cid, pid); return; }
        const id = sel.id, owner = choiceRow(id)?.name || id, chosen = choiceRow(pid)?.name, key = `${id}\u0000${cid}`;
        pendingIncludes.set(key, pid);
        void writeIncludes(id, (before, current) => {
            // Only this category's entry changes, in its place; an earlier
            // write is already in `before`.
            const mine = item => homeIn(current, item) === cid;
            const rest = before.filter(item => !mine(item));
            if (!pid) return rest;
            const at = before.findIndex(mine);
            const index = at < 0 ? rest.length : before.slice(0, at).filter(item => !mine(item)).length;
            return [...rest.slice(0, index), pid, ...rest.slice(index)];
        }, {done: pid ? `“${owner}” now also applies “${chosen}”.` : `“${owner}” no longer applies a choice from “${categoryRow(cid)?.name || cid}”.`,
            settle: () => { if (pendingIncludes.get(key) === pid) pendingIncludes.delete(key); }});
    }
    function removeIncluded(pid) {
        if (!isChoice() || sel.draft) return;
        const id = sel.id, owner = choiceRow(id)?.name || id;
        void writeIncludes(id, before => before.filter(item => item !== pid), {done: `“${owner}” no longer includes that choice.`});
    }
    function alsoSelect(cid) {
        const label = make("label"), text = make("span"), select = make("select");
        label.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1}px;${smallText}`;
        select.style.cssText = chromeInputCss({minWidth: "0"}) + "flex:1;";
        select.addEventListener("change", () => chooseIncluded(cid, select.value || null));
        label.append(text, select);
        return {el: label, text, select};
    }
    function alsoRemoveLine(pid) {
        const line = make("div"), text = make("span"), fix = button("Remove");
        line.style.cssText = `display:flex;flex-wrap:wrap;align-items:center;gap:${SPACE.s1}px;${smallText}`;
        fix.addEventListener("click", () => removeIncluded(pid));
        line.append(text, fix);
        return {el: line, text, fix};
    }
    function paintAlso(choice) {
        const home = !choice ? null : sel.draft ? sel.category || null : homeOf(sel.id)?.id || null;
        const others = choice ? categories().filter(c => c.id !== home) : [];
        const stored = choice && !sel.draft ? choiceRow(sel.id)?.includes || [] : [];
        const parents = choice && !sel.draft ? presets().filter(p => (p.includes || []).some(item => item.id === sel.id)) : [];
        const lines = [], represented = new Set();
        if (!parents.length) {
            for (const category of others) {
                const key = `category:${category.id}`;
                let line = alsoLines.get(key);
                if (!line) alsoLines.set(key, line = alsoSelect(category.id));
                setText(line.text, `“${category.name}”:`);
                const options = [["", "Nothing", ""], ...(category.choices || []).filter(pid => choiceRow(pid)).map(pid => [pid, choiceRow(pid).name,
                    // A choice that includes others can't be included (one level).
                    (choiceRow(pid).includes || []).length ? "It includes other choices itself, so it can't be included." : ""])];
                const optionsKey = JSON.stringify(options);
                if (line.select.dataset.key !== optionsKey) {
                    line.select.dataset.key = optionsKey;
                    line.select.replaceChildren(...options.map(([value, label, why]) => {
                        const option = new Option(label, value);
                        option.disabled = !!why;
                        option.title = why;
                        return option;
                    }));
                }
                line.select.setAttribute("aria-label", `Also applies from ${category.name}`);
                const saved = stored.find(item => item.status !== "missing" && (category.choices || []).includes(item.id))?.id || "";
                if (saved) represented.add(saved);
                const pending = `${sel.id}\u0000${category.id}`;
                const value = sel.draft ? draftIncludes.get(category.id) || ""
                    : pendingIncludes.has(pending) ? pendingIncludes.get(pending) || "" : saved;
                if (line.select.value !== value) line.select.value = value;
                lines.push(line.el);
            }
        }
        // What no select shows: an included choice in no other category, a
        // second one from one category, or one that is gone.
        for (const item of stored) {
            if (represented.has(item.id)) continue;
            const key = `include:${item.id}`;
            let line = alsoLines.get(key);
            if (!line) alsoLines.set(key, line = alsoRemoveLine(item.id));
            const label = item.name || choiceRow(item.id)?.name || item.id;
            setText(line.text, item.status === "missing" ? "An included choice no longer exists"
                : homeIn(view, item.id) === home && home ? `“${label}” (in this choice's own category)`
                    : homeIn(view, item.id) ? `“${label}”` : `“${label}” (not in a category)`);
            line.fix.setAttribute("aria-label", item.status === "missing" ? "Remove the missing choice" : `Remove ${label}`);
            lines.push(line.el);
        }
        const live = new Set(lines);
        for (const [key, line] of alsoLines) if (!live.has(line.el)) { line.el.remove(); alsoLines.delete(key); }
        placeInOrder(alsoList, lines);
        const note = parents.length
            ? `${parents.map(p => `“${p.name}”`).join(" and ")} ${parents.length === 1 ? "includes" : "include"} this choice, so it can't include others.` : "";
        setText(alsoNote, note);
        alsoNote.style.display = note ? "" : "none";
        also.style.display = choice && (lines.length || note) ? "flex" : "none";
    }

    // Choices in “<category>” (N), in the category's own order (its `choices`
    // are the one membership authority). The edited one is marked, the others
    // select like the main list. Kept buttons, updated in place.
    function paintSiblings(category) {
        const ids = category ? (category.choices || []).filter(pid => choiceRow(pid)) : [];
        siblings.style.display = ids.length ? "flex" : "none";
        if (!ids.length) return;
        setText(siblingsTitle, `Choices in “${category.name}” (${ids.length})`);
        for (const pid of ids) {
            let pick = siblingButtons.get(pid);
            if (!pick) {
                siblingButtons.set(pid, pick = button(""));
                pick.dataset.controlsModesSibling = pid;
                Object.assign(pick.style, {whiteSpace: "normal", overflowWrap: "anywhere", textAlign: "left"});
                pick.addEventListener("click", () => {
                    if (sel?.kind === "choice" && sel.id === pid) return;
                    select({kind: "choice", id: pid, draft: false});
                });
            }
            const current = isChoice() && sel.id === pid;
            setText(pick, choiceRow(pid)?.name || pid);
            setButtonVariant(pick, current ? "active" : "secondary", {padding: `2px ${SPACE.s2}px`, fontSize: `${TYPE.t11}px`});
            Object.assign(pick.style, {whiteSpace: "normal", overflowWrap: "anywhere", textAlign: "left"});
            if (current) pick.setAttribute("aria-current", "true");
            else pick.removeAttribute("aria-current");
        }
        const shown = new Set(ids);
        for (const [pid, pick] of siblingButtons) if (!shown.has(pid)) { pick.remove(); siblingButtons.delete(pid); }
        placeInOrder(siblingsList, ids.map(pid => siblingButtons.get(pid)));
    }

    function paintLoad(saved) {
        const mine = saved && load?.id === sel.id ? load : null;
        const text = !mine ? ""
            : mine.status === "loading" ? "Loading this choice into the Controls…"
                : mine.status === "loaded" ? "Loaded: the Controls show this choice."
                    : mine.status === "not_loaded" ? `Not loaded: ${mine.reason}. Controls still show your current settings.`
                        : `Couldn't confirm “${choiceRow(sel.id)?.name || name.value}” loaded. Check the Controls before updating it.`;
        // Always in the tree, so a screen reader announces its first text.
        setText(loadLine, text);
        loadLine.style.color = mine && ["not_loaded", "not_confirmed"].includes(mine.status) ? COLORS.warningText : COLORS.textSecondary;
        if (mine?.status === "loading") editor.setAttribute("aria-busy", "true");
        else editor.removeAttribute("aria-busy");
    }

    const missingTitle = make("div", "Settings without a Control");
    let missingLines = new Map();
    // One "In this choice" line per member. `previous`, the line this row
    // showed last, is updated in place when it is for the same member: the
    // panel repaints rows on every view, and a checkbox replaced between
    // mousedown and mouseup gets no click.
    // Only a line this Manage made is reused: one left from an earlier Manage
    // (a row kept alive across Done and reopen) closes over that one's state.
    const madeLines = new WeakSet();
    function memberLine(key, previous, onChange) {
        if (previous && madeLines.has(previous) && previous.dataset.controlsMember === key) return previous;
        const label = make("label"), checkbox = make("input"), text = make("span");
        madeLines.add(label);
        checkbox.type = "checkbox";
        label.dataset.controlsMember = key;
        label.style.cssText = `display:flex;gap:${SPACE.s1}px;align-items:center;${smallText}`;
        checkbox.addEventListener("change", () => { onChange(checkbox.checked); membershipDirty = true; });
        label.append(checkbox, text);
        return label;
    }
    function paintMemberLine(label, {checked, disabled, name, text}) {
        const [checkbox, span] = label.children;
        checkbox.checked = checked;
        checkbox.disabled = disabled;
        checkbox.setAttribute("aria-label", `Include ${name} in this choice`);
        setText(span, text);
        return label;
    }
    // A node switch member: its captured modes against the current ones.
    function switchControl(spec, previous) {
        const eid = spec.execution_id, stored = (choiceRow(sel.id)?.execution || []).find(m => m.execution_id === eid);
        const checked = switches.has(eid);
        let text = spec.absent ? `${eid} · In this choice` : "In this choice";
        if (stored && stored.status !== "ok") text += " · Changed since captured: update the choice";
        else if (stored) text += ` · Choice: ${stored.preview}`;
        const line = memberLine(`execution:${eid}`, previous, on => { on ? switches.add(eid) : switches.delete(eid); });
        return paintMemberLine(line, {checked, text, name: spec.label || eid,
            disabled: (!stored && !checked) && (spec.status !== "ok" || spec.read_only)});
    }
    function memberControl(spec, previous = null) {
        if (!isChoice()) return null;
        if (spec.execution_id) return switchControl(spec, previous);
        if (!spec.binding_id) return null;
        const bid = spec.binding_id, stored = (choiceRow(sel.id)?.members || []).find(m => m.binding_id === bid);
        const checked = members.has(bid);
        // An absent member has no descriptor to label it, so the line names its id.
        let text = spec.absent ? `${bid} · In this choice` : "In this choice";
        // A row list previews as its summary, which two lists can share, so the
        // server compares the rows themselves (control_presets.preset_states).
        const differs = spec.kind === "ROWS" ? stored?.differs === true : stored?.preview !== String(spec.value);
        if (stored && (differs || (stored.seed_mode && stored.seed_mode !== spec.seed_mode)))
            text += ` · Choice: ${stored.preview}${stored.seed_mode ? ` (${stored.seed_mode})` : ""}`;
        if (stored?.seed_mode && spec.seed_mode == null) text += " · Seed mode is skipped on this host";
        const line = memberLine(`binding:${bid}`, previous, on => { on ? members.add(bid) : members.delete(bid); });
        return paintMemberLine(line, {checked, text, name: spec.label || bid,
            disabled: (!stored && !checked) && (spec.status !== "ok" || spec.hidden)});
    }

    addCategory.addEventListener("click", () => {
        if (select({kind: "category", id: mintBindingId(), draft: true})) name.focus();
    });
    const unregister = register({id: `controls-modes-manage:${++serial}`, priority: PRIORITY.TEXT_EDITOR, keydown: e => {
        const input = document.activeElement;
        if (input !== name && input !== description) return false;
        if (e.isComposing || e.keyCode === 229) return PRESERVE_DEFAULT;
        const field = input === name ? "name" : "description";
        if (e.key === "Escape") {
            // Esc cancels: a draft's field empties, a saved one shows what is stored.
            const row = sel && !sel.draft ? (isChoice() ? choiceRow(sel.id) : categoryRow(sel.id)) : null;
            input.value = row?.[field] || "";
            input.blur();
            return true;
        }
        if (e.key === "Enter" && (input === name || e.ctrlKey || e.metaKey)) { input.blur(); return true; }
        return PRESERVE_DEFAULT;
    }});
    return {
        el, render, memberControl, canLeave,
        get busy() { return busy(); },
        // Settles when every queued write and load has (neither ever rejects).
        whenIdle: () => Promise.all([tail, ...loads]).then(() => {}),
        get editing() { return dirty() || [name, description].includes(document.activeElement); },
        // The panel's "N choices aren't in a category · Manage" lands here.
        revealModes() {
            el.scrollIntoView?.({block: "start"});
            heading.focus?.();
        },
        destroy() { destroyed = true; unregister(); el.remove(); },
    };
}
