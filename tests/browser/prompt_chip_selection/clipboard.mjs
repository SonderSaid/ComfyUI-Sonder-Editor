// Clipboard qualification for Phase 3's chip clipboard: does a custom type
// written in `copy`/`cut` come back in `paste`?
//
//   node tests/browser/prompt_chip_selection/clipboard.mjs [--browser=firefox|chromium|all]
//
// Headed on purpose: the system clipboard is the subject. It records and does
// not judge. Writes clipboard-results.json (ignored by git). Windows only for
// the "another application" step (PowerShell reads the clipboard). The text on
// the system clipboard before the run is put back afterwards; non-text content
// is not preserved: an empty or non-text clipboard is cleared afterwards.
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { chromiumDriver, firefoxDriver } from "./drivers.mjs";
import { FRAGMENT_TYPE, serveFixture } from "./fixture.mjs";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const which = (process.argv.slice(2).find((a) => a.startsWith("--browser="))?.split("=")[1]) || "all";
const ps = (command) => {
    try {
        return execFileSync("powershell.exe", ["-NoProfile", "-STA", "-Command", command],
            { encoding: "utf8" }).trim();
    } catch (error) { return `ERROR ${error.message.split("\n")[0]}`; }
};
const otherApp = () => ({
    text: ps("Get-Clipboard -Raw"),
    formats: ps("Add-Type -AssemblyName System.Windows.Forms; [Windows.Forms.Clipboard]::GetDataObject().GetFormats() -join '|'"),
});

// Saved as UTF-8 base64 so the console code page cannot alter it, untrimmed.
const savedB64 = ps("$t = Get-Clipboard -Raw; if ($null -eq $t) { '' } else { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t)) }");
const server = await serveFixture();
const url = `http://127.0.0.1:${server.address().port}/clip`;
const results = { fragmentType: FRAGMENT_TYPE, browsers: {} };
try {
    for (const name of which === "all" ? ["firefox", "chromium"] : [which]) {
        const driver = await (name === "firefox" ? firefoxDriver : chromiumDriver)(url, { headed: true });
        if (driver.skip) { results.browsers[name] = { skip: driver.skip }; console.log(`SKIP ${name}: ${driver.skip}`); continue; }
        const out = { version: driver.version, steps: {} };
        const lastPaste = async (tab) => (await tab.evaluate("window.clipLog")).filter((r) => r.type === "paste").at(-1) || null;
        const copyFrom = async (tab, op, nonce) => {
            await tab.evaluate(`(window.nonce = ${JSON.stringify(nonce)}, selectSource())`);
            await tab.key(op === "cut" ? "x" : "c", { ctrl: true });
            await delay(200);
            return (await tab.evaluate("window.clipLog")).filter((r) => r.type === op).at(-1) || null;
        };
        const pasteInto = async (tab, { shift = false } = {}) => {
            const before = (await tab.evaluate("window.clipLog")).length;
            await tab.evaluate("focusTarget()");
            await tab.key("v", { ctrl: true, shift });
            await delay(300);
            const log = await tab.evaluate("window.clipLog");
            return log.length > before ? await lastPaste(tab) : { fired: false };
        };
        try {
            await driver.bringToFront();
            out.steps.copyEvent = await copyFrom(driver, "copy", `${name}-copy`);
            out.steps.sameTab = await pasteInto(driver);
            out.steps.otherApplication = otherApp();
            const second = await driver.newTab(url);
            await second.bringToFront();
            out.steps.secondTab = await pasteInto(second);
            await driver.bringToFront();
            out.steps.plainPasteShortcut = await pasteInto(driver, { shift: true });
            out.steps.cutEvent = await copyFrom(driver, "cut", `${name}-cut`);
            out.steps.cutSameTab = await pasteInto(driver);
            // Another application copies after the browser: a stale fragment
            // must not ride along with its text.
            ps("Set-Clipboard -Value 'OTHER-APP-TEXT'");
            out.steps.afterOtherAppCopy = await pasteInto(driver);
        } catch (error) {
            out.error = String(error?.message || error);
        } finally { await driver.close(); }
        results.browsers[name] = out;
        console.log(name, JSON.stringify(out, null, 1));
    }
} finally {
    server.close();
    writeFileSync(join(HERE, "clipboard-results.json"), JSON.stringify(results, null, 2));
    // Put the saved text back, or clear the test text when there was none
    // (an empty read also covers a clipboard that held only non-text data,
    // which this script cannot restore).
    try {
        if (savedB64 && !savedB64.startsWith("ERROR")) {
            ps(`Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${savedB64}')))`);
        } else {
            ps("Add-Type -AssemblyName System.Windows.Forms; [Windows.Forms.Clipboard]::Clear()");
        }
    } catch (error) { console.log(`Clipboard not restored: ${error.message.split(/\r?\n/)[0]}`); }
}
