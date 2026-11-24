// Real-browser readback and position checks for the shared Context-chip editor.
//
//   node tests/browser/prompt_chip_readback/run.mjs [--browser=firefox|chromium|all]
//        [--headed] [--no-active-window] [--label=<name>]
//
// FIREFOX_BIN picks the Firefox (159 reproduced the readback loss; see README).
// SONDER_JS_DIR points the page at another web/js, e.g. 0.9.2 extracted with
// `git archive v0.9.2 web/js`, to see which cases failed there.
// Exit 0 = every case passed, 1 = a case failed, 77 = SKIPPED (no browser or
// driver). A skip is never a pass. Results go to results-<label>.json
// (ignored by git).
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { chromiumDriver, firefoxDriver } from "../prompt_chip_selection/drivers.mjs";
import { JS, serveFixture } from "./fixture.mjs";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const SKIP = 77;
const args = process.argv.slice(2);
const which = args.find((a) => a.startsWith("--browser="))?.split("=")[1] || "all";
const headed = args.includes("--headed");
const activeWindow = !args.includes("--no-active-window");
const label = args.find((a) => a.startsWith("--label="))?.split("=")[1] || "";

// The reporter's prompts, as the Firefox 159 audit built them.
const OPENING = "@Test is @Test. ";
const LAST = "this line has a total of 817 characters.";
const REPEAT = "This is some text that follows 'Test'.. ";
const LONG = OPENING + REPEAT.repeat(20).slice(0, 817 - OPENING.length - LAST.length - 1) + " " + LAST;
const PROMPTS = {
    long: LONG,
    terminal: "@Test is @Test and now we are adding @Test. " + "This is a bunch more text. . ".repeat(13)
        + "@Test @Test @Test @Test @Test",
    multiline: LONG.replaceAll("'Test'.. This", "'Test'..\nThis"),
    short: "@Test is @Test. A short prompt for @Test",
};
const SENTENCE = "keep @Test plus this whole suffix";
const SUFFIX = " plus this whole suffix";

async function type(driver, text) {
    for (const character of text) await driver.key(character);
}

async function run(driver) {
    const go = (expression) => driver.evaluate(expression);
    const cases = [];
    const record = async (group, name, expected, actual, extra = {}) => {
        const oracle = await go("oracle()");
        const failures = [];
        const tail = (value) => JSON.stringify(String(value).slice(-40));
        if (actual !== expected) failures.push(`expected ${String(expected).length} chars ending ${tail(expected)},`
            + ` got ${String(actual).length} ending ${tail(actual)}`);
        if (extra.precondition) failures.push(extra.precondition);
        cases.push({ group, name, pass: !failures.length, failures, expected, actual, oracle, ...extra });
    };

    // 1. Readback: a keystroke at the end of a wrapped, scrolled field. The
    //    caret is placed by script, and for two cases by a real click at the
    //    end of the text (the reporter's gesture).
    const READBACK = [];
    for (const [variant, text] of Object.entries(PROMPTS)) {
        for (const width of [211, 241]) {
            for (const typed of ["X", " This line ends with @Test"]) {
                if (variant === "short" && width === 211) continue;
                READBACK.push({ variant, text, width, typed, placement: "script" });
            }
        }
    }
    for (const variant of ["long", "terminal"]) {
        READBACK.push({ variant, text: PROMPTS[variant], width: 241, typed: "X", placement: "click" });
    }
    for (const { variant, text, width, typed, placement } of READBACK) {
                await go(`mount(${JSON.stringify({ text, width })})`);
                let before;
                if (placement === "click") {
                    await driver.click(await go("endPoint()"));
                    await delay(120);
                    before = await go("snapshot()");
                } else before = await go("caretEnd()");
                // The case only counts with the caret where it says.
                const precondition = before.caret?.[0] === text.length && before.caret?.[1] === text.length
                    ? null : `caret at ${JSON.stringify(before.caret)} before typing, not ${text.length}`;
                await type(driver, typed);
                await delay(120);
                const after = await go("snapshot()");
                await record("readback", `${variant} ${width}px ${placement} +${JSON.stringify(typed)}`,
                    text + typed, after.model,
                    { precondition, dom: after.dom === text + typed ? "whole" : after.dom.length,
                        modelLength: after.model.length, expectedLength: (text + typed).length });
    }

    // A named point must reach the editor in the shape it names; a browser
    // that normalized it to a text point would no longer test the mapping.
    const SHAPES = {
        "mark:0": { type: "mark", offset: 0 }, "mark:1": { type: "mark", offset: 1 },
        "span:2": { type: "text", offset: 2, children: 3 },
        "markText:5": { type: "text", in: "mark", offset: 5 },
    };
    const shapeCheck = (shape, name) => {
        const want = SHAPES[name];
        return Object.entries(want).every(([key, value]) => shape?.[key] === value)
            ? null : `selection end reached the editor as ${JSON.stringify(shape)}, not ${name}`;
    };

    // 2. A selection ending on the mention, in each DOM shape, then a key.
    const OPS = {
        Delete: { key: ["Delete"], expected: "keep " + SUFFIX },
        Backspace: { key: ["Backspace"], expected: "keep " + SUFFIX },
        Enter: { key: ["Enter"], expected: "keep \n" + SUFFIX },
        // Typing over a range is native: a control.
        Type: { key: ["Z"], expected: "keep Z" + SUFFIX },
        Cut: { key: ["x", { ctrl: true }], expected: "keep " + SUFFIX },
    };
    for (const end of ["mark:1", "span:2", "markText:5"]) {
        for (const [op, { key, expected }] of Object.entries(OPS)) {
            await go(`mount(${JSON.stringify({ nodes: [{ type: "text", node_id: "t", text: SENTENCE }],
                width: 540, compact: false })})`);
            const selected = await go(`select("text:5", ${JSON.stringify(end)})`);
            await driver.key(...key);
            await delay(120);
            const after = await go("snapshot()");
            await record("mention-range", `end ${end}, ${op}`, expected, after.model,
                { liveShape: selected.shape, selectedCaret: selected.caret,
                    focusAfter: after.active, precondition: shapeCheck(selected.shape?.[1], end) });
        }
    }

    // 3. A collapsed caret on the mention element.
    const CARETS = [
        { name: "Enter at (mark, 1)", nodes: [{ type: "text", node_id: "t", text: SENTENCE }],
            at: "mark:1", key: ["Enter"], expected: "keep @Test\n" + SUFFIX },
        { name: "Backspace at (mark, 0) after a chip",
            nodes: [{ type: "attachment", node_id: "c", attachment_id: "ref" },
                { type: "text", node_id: "t", text: "see @Test" }],
            at: "mark:0", key: ["Backspace"], expected: "[ref]see@Test" },
        { name: "Delete at (mark, 1) before a chip",
            nodes: [{ type: "text", node_id: "t", text: "@Test see" },
                { type: "attachment", node_id: "c", attachment_id: "ref" }],
            at: "mark:1", key: ["Delete"], expected: "@Testsee[ref]" },
    ];
    for (const c of CARETS) {
        await go(`mount(${JSON.stringify({ nodes: c.nodes, width: 540, compact: false })})`);
        const selected = await go(`select(${JSON.stringify(c.at)})`);
        await driver.key(...c.key);
        await delay(120);
        const after = await go("snapshot()");
        await record("mention-caret", c.name, c.expected, after.order, { liveShape: selected.shape,
            precondition: shapeCheck(selected.shape?.[0], c.at) || shapeCheck(selected.shape?.[1], c.at) });
    }

    // 4. Native edits that change the DOM shape, judged by the model and
    //    reported through the oracle.
    await go(`mount(${JSON.stringify({ text: "ab", width: 241 })})`);
    await go("caretEnd()");
    await driver.key("Backspace"); await driver.key("Backspace"); await delay(120);
    let after = await go("snapshot()");
    await record("native-shape", "Backspace the field empty", "", after.model, { children: after.children });

    await go(`mount(${JSON.stringify({ text: "x @Test", width: 241 })})`);
    await go("caretEnd()");
    for (let i = 0; i < 5; i += 1) await driver.key("Backspace");
    await delay(120);
    after = await go("snapshot()");
    await record("native-shape", "Backspace through a mention", "x ", after.model, { children: after.children });

    await go(`mount(${JSON.stringify({ text: LONG, width: 241 })})`);
    await go("caretEnd()");
    await type(driver, "  a");
    await delay(120);
    after = await go("snapshot()");
    const spaces = [...after.model.slice(-3)].map((c) => c.codePointAt(0).toString(16));
    // Typed spaces may arrive as U+00A0; the model stores what was typed either way.
    await record("native-shape", "two spaces at a line end", LONG + "  a",
        after.model.replaceAll("\u00a0", " "), { codepoints: spaces });

    // A rich-markup drop. A driver may not produce a native drop at all; the
    // case then records that instead of failing.
    await go(`mount(${JSON.stringify({ text: "start ", width: 241 })})`);
    const points = await go("richDrag()");
    await driver.drag(points.from, points.to);
    await delay(250);
    after = await go("snapshot()");
    const dropped = after.model !== "start ";
    cases.push({ group: "native-shape", name: "rich-markup drop", pass: !dropped || (
        after.model.includes("bold") && !after.model.includes("dropped-style") && !after.model.includes("HIDDEN")
        && after.model === after.dom),
    produced: dropped, actual: after.model, dom: after.dom, oracle: await go("oracle()"), children: after.children });
    return cases;
}

const server = await serveFixture();
const url = `http://127.0.0.1:${server.address().port}/`;
const results = [];
let anySkip = false;
for (const name of which === "all" ? ["firefox", "chromium"] : [which]) {
    const driver = name === "firefox" ? await firefoxDriver(url, { headed, activeWindow })
        : await chromiumDriver(url, { headed });
    if (driver.skip) { console.log(`${name}: SKIPPED (${driver.skip})`); anySkip = true; continue; }
    try {
        const cases = await run(driver);
        const failed = cases.filter((c) => !c.pass);
        const seen = cases.reduce((sum, c) => sum + (c.oracle?.captured || 0), 0);
        const reshaped = cases.reduce((sum, c) => sum + (c.oracle?.reshaped || 0), 0);
        const diffs = cases.filter((c) => c.oracle?.diffs?.length);
        console.log(`${driver.version} [${JS}]: ${cases.length - failed.length}/${cases.length} passed;`
            + ` oracle (${cases[0]?.oracle?.reader}): ${seen} span shapes captured before the editor's`
            + ` input handler, ${reshaped} reshaped, differences from innerText in ${diffs.length} case(s)`);
        for (const c of diffs) console.log(`  ORACLE ${c.group}: ${c.name} - ${JSON.stringify(c.oracle.diffs).slice(0, 300)}`);
        for (const c of failed) console.log(`  FAIL ${c.group}: ${c.name} - ${c.failures?.join("; ") || "see results"}`);
        results.push({ browser: driver.version, js: JS, activeWindow: name === "firefox" ? activeWindow : null, cases });
    } finally {
        await driver.close();
    }
}
server.close();
const file = join(HERE, `results-${label || which}.json`);
writeFileSync(file, JSON.stringify(results, null, 2));
console.log(`results: ${file}`);
const failed = results.some((r) => r.cases.some((c) => !c.pass));
// A skipped browser is never a pass, even beside one that passed.
process.exit(failed ? 1 : anySkip ? SKIP : 0);
