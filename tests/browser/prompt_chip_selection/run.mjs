// Real-browser selection checks for the shared Context-chip editor.
//
//   node tests/browser/prompt_chip_selection/run.mjs [--browser=firefox|chromium|all] [--headed]
//
// Firefox runs over WebDriver BiDi (installed Firefox). Chromium runs installed
// Google Chrome through a playwright-core module: PLAYWRIGHT_CORE=<path> or the
// npx cache (%LOCALAPPDATA%/npm-cache/_npx/*/node_modules/playwright-core).
// Exit 0 = PASS, 1 = FAIL, 77 = SKIPPED (no browser, no Node driver). A skip is
// never a pass. Results go to results-<browser>.json (ignored by git).
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { chromiumDriver, firefoxDriver } from "./drivers.mjs";
import { serveFixture } from "./fixture.mjs";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const SKIP = 77;
const args = process.argv.slice(2);
const which = (args.find((a) => a.startsWith("--browser="))?.split("=")[1]) || "all";
const headed = args.includes("--headed");

// --- Cases -----------------------------------------------------------------

// Controls: ordinary drags whose deletion is already right. They guard the
// Phase 3 change against regressions.
const CONTROLS = [
    { name: "one chip, prose to prose", variant: "one", from: ["L", 10], to: ["M", 5], ops: ["Delete", "Cut"],
        expected: "KEEP-LEFT  KEEP-RIGHT" },
    { name: "two chips across lines", variant: "pills", from: ["L", 10], to: ["R", 6], ops: ["Delete", "Backspace", "Cut"],
        expected: "KEEP-LEFT  KEEP-RIGHT" },
    { name: "plain text", variant: "plain", from: ["L", 10], to: ["L", 33], ops: ["Delete"],
        expected: "KEEP-LEFT  KEEP-RIGHT" },
];
// The bug: an older selection covers both chips, then a drag starts on chip C.
// Phase 3 decision 3: a press on a chip collapses the older selection, so the
// key must not delete prose or chip D beyond the new drag, focus stays in the
// editor, and one Ctrl+Z returns the document.
const STALE = { name: "older selection, then a drag from chip C", variant: "pills",
    old: [["L", 10], ["R", 6]], from: ["C", 3], to: ["M", 5], ops: ["Delete", "Backspace", "Cut"] };

async function run(driver) {
    const samples = [];
    const go = async (expression) => driver.evaluate(expression);
    const drag = async (from, to) => driver.drag(await go(`point(${JSON.stringify(from)})`),
        await go(`point(${JSON.stringify(to)})`));
    const press = (op) => op === "Cut" ? driver.key("x", { ctrl: true }) : driver.key(op);
    const undo = async () => { await driver.key("z", { ctrl: true }); await delay(60); return go("state()"); };
    for (const c of CONTROLS) {
        for (const op of c.ops) {
            const before = await go(`setup(${JSON.stringify({ variant: c.variant })})`);
            await drag(c.from, c.to);
            await press(op); await delay(80);
            const after = await go("state()");
            const undone = await undo();
            const failures = [];
            if (after.doc !== c.expected) failures.push(`text ${JSON.stringify(after.doc)}`);
            if (after.selection.active !== "$editor") failures.push(`focus ${after.selection.active}`);
            if (JSON.stringify(undone.data) !== JSON.stringify(before.data)) failures.push("Undo did not restore");
            samples.push({ kind: "control", name: c.name, op, pass: !failures.length, failures, after: after.doc,
                attachments: after.data[1], focus: after.selection.active });
        }
    }
    for (const op of STALE.ops) {
        const before = await go(`setup(${JSON.stringify({ variant: STALE.variant })})`);
        await drag(...STALE.old);
        const oldSelection = await go("state()");
        // The caret settles one task after the release.
        await drag(STALE.from, STALE.to); await delay(60);
        const selected = await go("state()");
        await press(op); await delay(80);
        const after = await go("state()");
        const undone = await undo();
        const failures = [];
        // Decision 3 itself: the chip press collapses the older selection.
        if (selected.selection.text && selected.selection.text === oldSelection.selection.text)
            failures.push("older selection still live after the chip press");
        // Chip Focus plan (A2): the drag ends in a collapsed caret, so the key
        // takes at most one character there and neither chip.
        if (JSON.stringify(after.data[1]) !== JSON.stringify(["C", "D"])) failures.push("a chip was deleted");
        if (!after.doc.includes("omega KEEP-RIGHT")) failures.push("prose beyond the new drag deleted");
        if (after.selection.active !== "$editor") failures.push(`focus ${after.selection.active}`);
        if (JSON.stringify(undone.data) !== JSON.stringify(before.data)) failures.push("one Ctrl+Z did not restore");
        samples.push({ kind: "stale", name: STALE.name, op, pass: !failures.length, failures,
            oldSelection: oldSelection.selection.text, selectedAfterChipDrag: selected.selection,
            after: after.doc, attachments: after.data[1], focus: after.selection.active });
    }
    // Phase 3 audit M2: an older range, then Tab moves focus to chip C and
    // the author types. The range must not be replaced. Since the Chip Focus
    // plan (Part B) the letter continues after chip C instead of going nowhere.
    {
        const before = await go(`setup(${JSON.stringify({ variant: "pills" })})`);
        await drag(...STALE.old);
        await driver.key("Tab"); await delay(60);
        await driver.key("x"); await delay(80);
        const after = await go("state()");
        const failures = [];
        const order = await go("order()");
        if (order !== "KEEP-LEFT alpha [C]x beta ONE\nline TWO [D] omega KEEP-RIGHT")
            failures.push(`text ${JSON.stringify(order)} (older range intact, x after C)`);
        if (JSON.stringify(after.data[1]) !== JSON.stringify(before.data[1])) failures.push("chips changed");
        if (after.selection.active !== "$editor") failures.push(`focus ${after.selection.active}`);
        samples.push({ kind: "stale", name: "older selection, Tab to a chip, then typing", op: "x",
            pass: !failures.length, failures, after: after.doc, attachments: after.data[1], focus: after.selection.active });
    }
    // The 2026-11 user report ("more text than I highlighted was deleted ...
    // Ctrl+Z didn't save me"): the older-selection class, widened. An older
    // selection made several ways, then a click or drag starting on a chip,
    // then a destructive key. Nothing outside the new highlight may go, focus
    // stays in the editor, one Ctrl+Z restores. 0.9.0 failed 48 of these 60.
    const olds = {
        "drag across both chips": async () => drag(["L", 10], ["R", 6]),
        "drag L to M": async () => drag(["L", 4], ["M", 8]),
        "shift+arrows across C": async () => { const p = await go(`point(["L", 6])`); await driver.drag(p, p);
            for (let i = 0; i < 18; i++) await driver.key("ArrowRight", { shift: true }); },
        "cut then undo": async () => { await drag(["L", 10], ["R", 6]); await press("Cut"); await delay(60); await undo(); },
    };
    // Each press names what it may remove: prose outside its span (the
    // prefix and suffix) and the other chip must survive, whatever is live.
    const L = "KEEP-LEFT alpha ", M = " beta ONE\nline TWO ", R = " omega KEEP-RIGHT";
    const click = (id) => async () => { const p = await go(`point(["${id}", 3])`); await driver.drag(p, p); };
    // A drag that starts on a chip ends with a collapsed text caret where it
    // was released (Chip Focus plan, A2): the key acts there, exactly one
    // character, and both chips stay. Before, the chip kept focus and the key
    // removed the pressed chip.
    const caretAt = (text, at) => ({ Delete: text.slice(0, at) + text.slice(at + 1),
        Backspace: text.slice(0, at - 1) + text.slice(at), Cut: text });
    const presses = {
        "drag C to M": { run: async () => drag(["C", 3], ["M", 5]), prefix: L, suffix: M.slice(5) + R, stays: "D",
            exact: { both: true, ...Object.fromEntries(Object.entries(caretAt(M, 5)).map(([k, m]) => [k, L + m + R])) } },
        "drag D to R": { run: async () => drag(["D", 3], ["R", 8]), prefix: L + M, suffix: R.slice(8), stays: "C",
            exact: { both: true, ...Object.fromEntries(Object.entries(caretAt(R, 8)).map(([k, r]) => [k, L + M + r])) } },
        "drag C back to L": { run: async () => drag(["C", 3], ["L", 3]), prefix: L.slice(0, 3), suffix: M + R, stays: "D",
            exact: { both: true, ...Object.fromEntries(Object.entries(caretAt(L, 3)).map(([k, l]) => [k, l + M + R])) } },
        "click C": { run: click("C"), prefix: L, suffix: M + R, stays: "D" },
        "click D": { run: click("D"), prefix: L + M, suffix: R, stays: "C" },
    };
    for (const [oldName, makeOld] of Object.entries(olds)) for (const [pressName, press1] of Object.entries(presses))
        for (const op of ["Delete", "Backspace", "Cut"]) {
            const before = await go(`setup({ variant: "pills" })`);
            await makeOld(); await press1.run(); await delay(60);
            const hl = await go("highlighted()");
            await press(op); await delay(80);
            const after = await go("state()");
            const undone = await undo();
            const failures = [];
            // A drag press is judged exactly below; the span check is for clicks.
            if (!press1.exact && (!after.doc.startsWith(press1.prefix) || !after.doc.endsWith(press1.suffix)
                    || after.doc.length < press1.prefix.length + press1.suffix.length))
                failures.push(`removed prose outside the press: ${JSON.stringify(after.doc)}`);
            if (!after.data[1].includes(press1.stays)) failures.push(`chip ${press1.stays} removed (outside the press)`);
            if (press1.exact) {
                if (after.doc !== press1.exact[op])
                    failures.push(`expected ${JSON.stringify(press1.exact[op])} (one character at the release caret)`);
                if (JSON.stringify(after.data[1]) !== JSON.stringify(["C", "D"])) failures.push("a chip was removed");
                if (after.selection.active !== "$editor") failures.push(`focus ${after.selection.active}`);
            }
            if (after.selection.active === "BODY") failures.push("focus fell to the page");
            if (JSON.stringify(undone.data) !== JSON.stringify(before.data)) failures.push("one Ctrl+Z did not restore");
            samples.push({ kind: "report", name: `${oldName}, then ${pressName}`, op, pass: !failures.length, failures,
                highlight: hl, after: after.doc, attachments: after.data[1], focus: after.selection.active });
        }
    // A partial highlight across chips removes exactly what is highlighted, and
    // the field shows what the model (and so the save) holds.
    const spots = [["L", 3], ["M", 0], ["M", 13], ["R", 0], ["R", 12]];
    for (let i = 0; i < spots.length; i++) for (let j = i + 1; j < spots.length; j++) {
        if (spots[i][0] === spots[j][0]) continue;
        for (const [from, to, op] of [[spots[i], spots[j], "Delete"], [spots[j], spots[i], "Cut"]]) {
            const before = await go(`setup({ variant: "pills" })`);
            await drag(from, to);
            const hl = await go("highlighted()");
            await press(op); await delay(80);
            const after = await go("state()");
            const screen = await go("screenProse()");
            const undone = await undo();
            const failures = [];
            const at = before.doc.indexOf(hl);
            const exact = hl && [...before.doc.matchAll(new RegExp(hl.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g"))]
                .some((m) => before.doc.slice(0, m.index) + before.doc.slice(m.index + hl.length) === after.doc);
            if (at < 0 || !exact) failures.push(`removed other than ${JSON.stringify(hl)}: ${JSON.stringify(after.doc)}`);
            if (screen !== after.doc) failures.push(`screen ${JSON.stringify(screen)} differs from the model`);
            const order = { L: 0, C: 1, M: 2, D: 3, R: 4 };
            const [lo, hi] = [order[from[0]], order[to[0]]].sort((x, y) => x - y);
            const expectChips = ["C", "D"].filter((id) => !(lo < order[id] && order[id] < hi));
            if (JSON.stringify(after.data[1]) !== JSON.stringify(expectChips))
                failures.push(`chips ${JSON.stringify(after.data[1])}, expected ${JSON.stringify(expectChips)}`);
            if (JSON.stringify(undone.data) !== JSON.stringify(before.data)) failures.push("Ctrl+Z did not restore");
            samples.push({ kind: "report", name: `partial ${from} to ${to}`, op, pass: !failures.length, failures,
                highlight: hl, after: after.doc });
        }
    }
    // Chip Focus plan (2026-11-23): no gesture leaves the author without a text
    // caret beside a chip. Each case types `x` (or pastes) and reads where it
    // landed in the model, after the gesture plus a frame.
    const frame = () => delay(60);
    const focusCase = async (name, { setup, gesture, typed = "x", expect, active = "$editor" }) => {
        await go(`setup(${JSON.stringify(setup)})`);
        await gesture(); await frame();
        if (typed) { for (const ch of typed) await driver.key(ch); await frame(); }
        const after = await go("state()");
        const order = await go("order()");
        const failures = [];
        if (order !== expect) failures.push(`order ${JSON.stringify(order)}, expected ${JSON.stringify(expect)}`);
        if (active && after.selection.active !== active) failures.push(`focus ${after.selection.active}`);
        samples.push({ kind: "focus", name, op: typed || "-", pass: !failures.length, failures, order,
            focus: after.selection.active });
    };
    const clickAt = async (where) => { const p = await go(where); await driver.drag(p, p); };
    const dragTo = async (from, where) => driver.drag(await go(`point(${JSON.stringify(from)})`), await go(where));
    const T = "KEEP-LEFT alpha ";
    for (const handle of [false, true]) {
        const tag = handle ? " (handle chip)" : "";
        await focusCase(`click right of a trailing chip${tag}`, { setup: { variant: "trailing", handle },
            gesture: () => clickAt(`beside("C", 8)`), expect: `${T}[C]x` });
        await focusCase(`click right of a line-ending chip${tag}`, { setup: { variant: "lineEnd", handle },
            gesture: () => clickAt(`beside("C", 8)`), expect: `${T}[C]x\nsecond line KEEP-RIGHT` });
        await focusCase(`drag from a trailing chip to the blank beside it${tag}`, { setup: { variant: "trailing", handle },
            gesture: () => dragTo(["C", 3], `beside("C", 35)`), expect: `${T}[C]x` });
        await focusCase(`drag from a trailing chip to outside the field${tag}`, { setup: { variant: "trailing", handle },
            gesture: () => dragTo(["C", 3], "outside()"), expect: `${T}[C]x` });
    }
    await focusCase("Tab to a chip, ArrowRight, type", { setup: { variant: "one" },
        gesture: async () => { await driver.key("Tab"); await frame(); await driver.key("ArrowRight"); },
        expect: `${T}[C]x beta KEEP-RIGHT` });
    await focusCase("Tab to a chip, ArrowLeft, type", { setup: { variant: "one" },
        gesture: async () => { await driver.key("Tab"); await frame(); await driver.key("ArrowLeft"); },
        expect: `${T}x[C] beta KEEP-RIGHT` });
    await focusCase("Tab to a trailing chip, type @ and a letter", { setup: { variant: "trailing" },
        gesture: () => driver.key("Tab"), typed: "@a", expect: `${T}[C]@a` });
    // A run of chips, a chip alone on its line, one that soft-wraps to a new
    // line, and a handle whose label wraps: right of the last line is after it.
    const SOFT = "word ".repeat(16) + "last ", WRAP = "word ".repeat(14);
    for (const [variant, last, expect] of [["run", "D", `${T}[C][D]x`], ["alone", "C", "first line\n[C]x\nthird line"],
        ["softwrap", "C", `${SOFT}[C]x`]]) {
        await focusCase(`click right of the last chip (${variant})`, { setup: { variant },
            gesture: () => clickAt(`beside("${last}", 25)`), expect });
    }
    await focusCase("click right of a wrapped handle's last line", { setup: { variant: "wrapped", handle: true },
        gesture: () => clickAt(`beside("C", 25)`), expect: `${WRAP}[the quick brown fox jumps over it]x` });
    await focusCase("click in the padding above, right of a trailing chip", { setup: { variant: "trailing" },
        gesture: () => clickAt(`beside("C", 25, -13)`), expect: `${T}[C]x` });
    // The line above a soft-wrapped chip keeps its own clicks.
    await focusCase("click the line above a soft-wrapped chip", { setup: { variant: "softwrap" },
        gesture: () => clickAt(`beside("C", 25, -14)`), typed: "", expect: `${SOFT}[C]` });
    {
        const caret = (await go("state()")).selection.ranges[0]?.[0];
        samples.push({ kind: "focus", name: "click the line above a soft-wrapped chip, caret", op: "-",
            pass: caret?.[0] === "L", failures: caret?.[0] === "L" ? [] : [`caret ${JSON.stringify(caret)}`] });
    }
    await focusCase("drag from a trailing chip to the blank below it", { setup: { variant: "trailing" },
        gesture: () => dragTo(["C", 3], `beside("C", 20, 20)`), expect: `${T}[C]x` });
    // The editor's copy handler fills the browser's clipboard, which on a real
    // desktop is the system one.
    const copyKeep = async () => { await go(`selectRange(["L", 0], ["L", 4])`); await driver.key("c", { ctrl: true }); await frame(); };
    await focusCase("Tab to a chip, Ctrl+V", { setup: { variant: "one" },
        gesture: async () => { await copyKeep(); await go("focusEditor()"); await driver.key("Tab"); await frame();
            await driver.key("v", { ctrl: true }); }, typed: "", expect: `${T}[C]KEEP beta KEEP-RIGHT` });
    await focusCase("older range, Tab to a chip, Ctrl+V", { setup: { variant: "pills" },
        gesture: async () => { await copyKeep(); await drag(...STALE.old); await driver.key("Tab"); await frame();
            await driver.key("v", { ctrl: true }); }, typed: "",
        expect: "KEEP-LEFT alpha [C]KEEP beta ONE\nline TWO [D] omega KEEP-RIGHT" });
    await focusCase("chip settings Cancel, then type", { setup: { variant: "one", dialog: true },
        gesture: async () => { await clickAt(`point(["C", 3])`); await frame(); await clickAt("cancelPoint()"); },
        expect: `${T}[C]x beta KEEP-RIGHT` });
    await focusCase("chip settings Escape, then type", { setup: { variant: "one", dialog: true },
        gesture: async () => { await clickAt(`point(["C", 3])`); await frame(); await driver.key("Escape"); },
        expect: `${T}[C]x beta KEEP-RIGHT` });
    // The dialog takes no focus, so the chip under it still holds it: nothing
    // typed, pasted or pressed there may reach the prompt behind the modal.
    for (const [name, act] of [["type", () => driver.key("x")], ["Ctrl+V", () => driver.key("v", { ctrl: true })],
        ["Delete", () => driver.key("Delete")],
        ["ArrowRight, type", async () => { await driver.key("ArrowRight"); await driver.key("x"); }],
        ["Enter", () => driver.key("Enter")], ["Ctrl+X", () => driver.key("x", { ctrl: true })]]) {
        await focusCase(`chip settings open, ${name}`, { setup: { variant: "one", dialog: true },
            gesture: async () => { await copyKeep(); await clickAt(`point(["C", 3])`); await frame(); await act(); },
            typed: "", expect: `${T}[C] beta KEEP-RIGHT`, active: "chip:C" });
        const opened = await go("dialogs");
        if (opened !== 1) Object.assign(samples.at(-1), { pass: false,
            failures: [...samples.at(-1).failures, `dialogs ${opened}`] });
        await driver.key("Escape");
    }
    // An older range live under the dialog must not be typed over either.
    await focusCase("older range, chip settings opened by Space, type", { setup: { variant: "pills", dialog: true },
        gesture: async () => { await drag(...STALE.old); await driver.key("Tab"); await frame();
            await driver.key(" "); await frame(); },
        expect: "KEEP-LEFT alpha [C] beta ONE\nline TWO [D] omega KEEP-RIGHT", active: "chip:C" });
    await driver.key("Escape");
    {
        // Space and Enter keep the button convention: they open the chip.
        await go(`setup(${JSON.stringify({ variant: "one", dialog: true })})`);
        await driver.key("Tab"); await frame(); await driver.key(" "); await frame();
        const opened = await go("dialogs");
        const order = await go("order()");
        const failures = [];
        if (opened !== 1) failures.push(`dialogs ${opened}`);
        if (order !== `${T}[C] beta KEEP-RIGHT`) failures.push(`order ${JSON.stringify(order)}`);
        samples.push({ kind: "focus", name: "Tab to a chip, Space opens it", op: "Space", pass: !failures.length,
            failures, order });
    }
    return samples;
}

const server = await serveFixture();
const url = `http://127.0.0.1:${server.address().port}/`;
const wanted = which === "all" ? ["firefox", "chromium"] : [which];
const outcomes = {};
try {
    for (const name of wanted) {
        const driver = await (name === "firefox" ? firefoxDriver : chromiumDriver)(url, { headed });
        if (driver.skip) { console.log(`SKIP ${name}: ${driver.skip}`); outcomes[name] = "skip"; continue; }
        try {
            const samples = await run(driver);
            writeFileSync(join(HERE, `results-${name}.json`), JSON.stringify({ browser: driver.version, samples }, null, 2));
            for (const s of samples)
                console.log(`${s.pass ? "PASS" : "FAIL"} ${name} ${s.kind} | ${s.name} | ${s.op}${s.failures.length ? " — " + s.failures.join("; ") : ""}`);
            outcomes[name] = samples.every((s) => s.pass) ? "pass" : "fail";
        } finally { await driver.close(); }
    }
} finally { server.close(); }
const values = Object.values(outcomes);
console.log(JSON.stringify(outcomes));
process.exit(values.includes("fail") ? 1 : values.every((v) => v === "skip") ? SKIP : values.includes("skip") ? SKIP : 0);
