// Manage's Modes section: categories, their choices and warnings, and the
// category and choice editors (plans/controls-modes-phase-b.md §6). DOM only:
// the validating host captures values and stores every edit.
//
// The list is keyed and updated in place, and a queued write never disables a
// button: a click whose mousedown blurred a field (whose save re-renders this)
// must still land on the button it pressed.
import { register, PRIORITY, PRESERVE_DEFAULT } from "./keyboard_ownership.js";
import { EDITOR_COLORS as COLORS, RADIUS, SPACE, TYPE, chromeInputCss, setButtonVariant } from "./editor_theme.js";
import { blankName, mintBindingId } from "./control_bindings.js";
import { deriveModes } from "./controls_modes_model.js";
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

export function mountModesManage(host, { track, notify, flushPending = async () => true, changed = () => {} }) {
    const el = make("section"), heading = make("div", "Modes"), list = make("div"), loose = make("div"),
        looseTitle = make("div", "Not in a category"), looseList = make("div"), addCategory = button("+ Category"),
        editor = make("div"), editorTitle = make("div"), name = make("input"), description = make("textarea"), counter = make("div"),
        categoryLabel = make("label"), categorySelect = make("select"), missing = make("div"),
        actions = make("div"), save = button("Create"), remove = button("Delete"), reset = button("Reset to workflow's version");
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
    for (const input of [name, description]) { input.style.cssText = chromeInputCss({minWidth: "0"}) + "width:100%;box-sizing:border-box;"; input.dataset.sonderTextUndo = "true"; }
    description.dataset.modesDescription = "true";
    counter.style.cssText = `${smallText}align-self:flex-end;`;
    categoryLabel.style.cssText = `display:flex;align-items:center;gap:${SPACE.s1}px;${smallText}`;
    categorySelect.style.cssText = chromeInputCss({minWidth: "0"}) + "flex:1;";
    categorySelect.setAttribute("aria-label", "Category");
    categoryLabel.append(make("span", "Category"), categorySelect);
    actions.style.cssText = `display:flex;flex-wrap:wrap;gap:${SPACE.s1}px;`;
    actions.append(save, remove, reset);
    editor.append(editorTitle, name, description, counter, categoryLabel, missing, actions);
    el.append(heading, list, loose, addCategory, editor);

    // `sel`: what the editor holds. {kind: "category"|"choice", id, draft, category?}
    // (a new choice remembers the category it goes into). `members` holds
    // setting ids, `switches` node-switch ids; a choice may hold both.
    let view = {}, sel = null, members = new Set(), switches = new Set(), membershipDirty = false, destroyed = false;
    // `staleView`: the view a write left behind when no read after it was
    // adopted; a `choices` list built from it would undo that write.
    let tail = Promise.resolve(), queued = 0, creating = null, staleView = null;
    const pendingText = new Map(), blocks = new Map(), looseRows = new Map();
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
    const busy = () => queued > 0;
    // Asks only about a draft: a queued write is no reason to refuse a click
    // (its blur-save is often what queued it). The panel waits for the queue
    // before closing Manage, and refuses a workflow switch while it runs.
    const canLeave = () => !dirty() || globalThis.confirm(isChoice() ? "Discard this unsaved choice?" : "Discard this unsaved category?");

    function select(next) {
        if (!canLeave()) return false;
        sel = next; membershipDirty = false; creating = null;
        const row = next?.kind === "choice" ? choiceRow(next.id) : next ? categoryRow(next.id) : null;
        name.value = next?.draft ? "" : row?.name || "";
        description.value = next?.draft ? "" : row?.description || "";
        members = new Set(isChoice() && !next.draft ? (row?.members || []).map(m => m.binding_id) : []);
        switches = new Set(isChoice() && !next.draft ? (row?.execution || []).map(m => m.execution_id) : []);
        render(view); changed(); return true;
    }

    // Every edit queues behind the last one and is built when its turn comes,
    // from the view the previous edit produced (it waits for a fresh one), so
    // two quick edits of one list never build on the same stale list.
    function enqueue(what, build, { flush = false, text = null, after = null } = {}) {
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
                if (host.freshView && !destroyed) staleView = (await host.freshView()).fresh ? null : host.getView();
                return true;
            } catch (error) { notify(`The ${what} could not be saved.`, error?.message); return false; }
            finally {
                if (text && pendingText.get(text.target) === text.value) pendingText.delete(text.target);
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

    function saveText(field, input) {
        if (!sel || sel.draft) return;
        const row = isChoice() ? choiceRow(sel.id) : categoryRow(sel.id);
        if (!row || input.value === (row[field] || "")) return;
        if (field === "name" && blankName(input.value)) { notify("A name can't be empty."); input.value = row.name; return; }
        const target = `${isChoice() ? "preset" : "preset_category"}:${sel.id}:${field}`, value = input.value;
        void enqueue(isChoice() ? "choice" : "category", () => ({[target]: value}), {text: {target, value}});
    }
    name.addEventListener("blur", () => saveText("name", name));
    description.addEventListener("blur", () => saveText("description", description));
    description.addEventListener("input", () => paintCounter());
    save.addEventListener("mousedown", e => e.preventDefault());
    save.addEventListener("click", () => {
        if (!sel) return;
        if (sel.draft) {
            if (creating === sel.id) return; // a second click on the same draft
            if (blankName(name.value)) { notify(isChoice() ? "Name the choice first." : "Name the category first."); name.focus(); return; }
            creating = sel.id;
        }
        const id = sel.id, draft = !!sel.draft, into = sel.category || null;
        if (!draft) { saveText("name", name); saveText("description", description); }
        const done = () => { if (sel?.id === id) { sel = {...sel, draft: false}; delete sel.category; membershipDirty = false; creating = null; } };
        if (!isChoice()) {
            if (!draft) return;
            const fields = {name: name.value, description: description.value};
            void enqueue("category", () => ({[`preset_category:${id}:create`]: fields}), {after: done})
                .then(ok => { if (!ok && creating === id) creating = null; });
            return;
        }
        const capture = {members: [...members], ...(switches.size ? {execution: [...switches]} : {})};
        const fields = draft ? {...capture, name: name.value, description: description.value} : capture;
        void enqueue("choice", current => {
            const changes = {[`preset:${id}:${draft ? "create" : "capture"}`]: fields};
            if (!draft || !into) return changes;
            const listed = choicesChange(current, into, choices => [...choices, id]);
            return listed && {...changes, ...listed};
        }, {flush: true, after: done}).then(ok => { if (!ok && creating === id) creating = null; });
    });
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
        // An edited entry gone from the view (deleted elsewhere, Undo) leaves the editor.
        if (sel && !sel.draft && !(isChoice() ? choiceRow(sel.id) : categoryRow(sel.id))) { sel = null; membershipDirty = false; }
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

        const ids = new Set((view.bindings || []).map(b => b.binding_id));
        const absent = choice ? (current?.members || []).filter(m => !ids.has(m.binding_id)) : [];
        const switchIds = new Set((view.execution || []).map(d => d.execution_id));
        const gone = choice ? (current?.execution || []).filter(m => !switchIds.has(m.execution_id)) : [];
        missing.replaceChildren();
        if (absent.length || gone.length) missing.append(make("div", "Settings without a Control"));
        for (const member of absent) missing.append(memberControl({binding_id: member.binding_id, status: "missing", absent: true}));
        for (const member of gone) missing.append(switchControl({execution_id: member.execution_id, status: "missing", absent: true}));
    }

    // A node switch member: its captured modes against the current ones.
    function switchControl(spec) {
        const eid = spec.execution_id, stored = (choiceRow(sel.id)?.execution || []).find(m => m.execution_id === eid);
        const label = make("label"), checkbox = make("input"), text = make("span");
        checkbox.type = "checkbox"; checkbox.checked = switches.has(eid);
        checkbox.disabled = (!stored && !checkbox.checked) && (spec.status !== "ok" || spec.read_only);
        checkbox.setAttribute("aria-label", `Include ${spec.label || eid} in this choice`);
        text.textContent = spec.absent ? `${eid} · In this choice` : "In this choice";
        if (stored && stored.status !== "ok") text.textContent += " · Changed since captured: update the choice";
        else if (stored) text.textContent += ` · Choice: ${stored.preview}`;
        label.style.cssText = `display:flex;gap:${SPACE.s1}px;align-items:center;${smallText}`;
        checkbox.addEventListener("change", () => { checkbox.checked ? switches.add(eid) : switches.delete(eid); membershipDirty = true; });
        label.append(checkbox, text);
        return label;
    }
    function memberControl(spec) {
        if (!isChoice()) return null;
        if (spec.execution_id) return switchControl(spec);
        if (!spec.binding_id) return null;
        const bid = spec.binding_id, stored = (choiceRow(sel.id)?.members || []).find(m => m.binding_id === bid);
        const label = make("label"), checkbox = make("input"), text = make("span");
        checkbox.type = "checkbox"; checkbox.checked = members.has(bid);
        checkbox.disabled = (!stored && !checkbox.checked) && (spec.status !== "ok" || spec.hidden);
        checkbox.setAttribute("aria-label", `Include ${spec.label || bid} in this choice`);
        // An absent member has no descriptor to label it, so the line names its id.
        text.textContent = spec.absent ? `${bid} · In this choice` : "In this choice";
        // A row list previews as its summary, which two lists can share, so the
        // server compares the rows themselves (control_presets.preset_states).
        const differs = spec.kind === "ROWS" ? stored?.differs === true : stored?.preview !== String(spec.value);
        if (stored && (differs || (stored.seed_mode && stored.seed_mode !== spec.seed_mode)))
            text.textContent += ` · Choice: ${stored.preview}${stored.seed_mode ? ` (${stored.seed_mode})` : ""}`;
        if (stored?.seed_mode && spec.seed_mode == null) text.textContent += " · Seed mode is skipped on this host";
        label.style.cssText = `display:flex;gap:${SPACE.s1}px;align-items:center;${smallText}`;
        checkbox.addEventListener("change", () => { checkbox.checked ? members.add(bid) : members.delete(bid); membershipDirty = true; });
        label.append(checkbox, text);
        return label;
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
        // Settles when every queued write has (the queue's tail never rejects).
        whenIdle: () => tail,
        get editing() { return dirty() || [name, description].includes(document.activeElement); },
        // The panel's "N choices aren't in a category · Manage" lands here.
        revealModes() {
            el.scrollIntoView?.({block: "start"});
            heading.focus?.();
        },
        destroy() { destroyed = true; unregister(); el.remove(); },
    };
}
