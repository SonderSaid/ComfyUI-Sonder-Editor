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
        await drag(STALE.from, STALE.to);
        const selected = await go("state()");
        await press(op); await delay(80);
        const after = await go("state()");
        const undone = await undo();
        const failures = [];
        // Decision 3 itself: the chip press collapses the older selection.
        if (selected.selection.text && selected.selection.text === oldSelection.selection.text)
            failures.push("older selection still live after the chip press");
        if (!after.data[1].includes("D")) failures.push("chip D deleted (outside the new drag)");
        if (!after.doc.includes("omega KEEP-RIGHT")) failures.push("prose beyond the new drag deleted");
        if (after.selection.active !== "$editor") failures.push(`focus ${after.selection.active}`);
        if (JSON.stringify(undone.data) !== JSON.stringify(before.data)) failures.push("one Ctrl+Z did not restore");
        samples.push({ kind: "stale", name: STALE.name, op, pass: !failures.length, failures,
            oldSelection: oldSelection.selection.text, selectedAfterChipDrag: selected.selection,
            after: after.doc, attachments: after.data[1], focus: after.selection.active });
    }
    // Phase 3 audit M2: an older range, then Tab moves focus to chip C and
    // the author types. The range must not be replaced.
    {
        const before = await go(`setup(${JSON.stringify({ variant: "pills" })})`);
        await drag(...STALE.old);
        await driver.key("Tab"); await delay(60);
        await driver.key("x"); await delay(80);
        const after = await go("state()");
        const failures = [];
        if (after.doc !== before.doc) failures.push(`text ${JSON.stringify(after.doc)}`);
        if (JSON.stringify(after.data[1]) !== JSON.stringify(before.data[1])) failures.push("chips changed");
        samples.push({ kind: "stale", name: "older selection, Tab to a chip, then typing", op: "x",
            pass: !failures.length, failures, after: after.doc, attachments: after.data[1], focus: after.selection.active });
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
