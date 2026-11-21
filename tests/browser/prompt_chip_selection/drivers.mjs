// Browser drivers shared by run.mjs and clipboard.mjs. Each driver opens a
// page and exposes: evaluate(expression) -> JSON value, key(name, {ctrl, shift}),
// drag(fromPoint, toPoint), newTab(url) -> a tab with the same evaluate/key and
// bringToFront(), and close(). `{ skip }` means the browser or driver is missing.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

export const FIREFOX = process.env.FIREFOX_BIN || "C:/Program Files/Mozilla Firefox/firefox.exe";

function findPlaywright() {
    if (process.env.PLAYWRIGHT_CORE) return process.env.PLAYWRIGHT_CORE;
    const cache = join(process.env.LOCALAPPDATA || "", "npm-cache", "_npx");
    if (!existsSync(cache)) return null;
    for (const dir of readdirSync(cache)) {
        const candidate = join(cache, dir, "node_modules", "playwright-core");
        if (existsSync(join(candidate, "package.json"))) return candidate;
    }
    return null;
}

const BIDI_KEYS = { Delete: "\ue017", Backspace: "\ue003", Control: "\ue009", Shift: "\ue008" };

export async function firefoxDriver(url, { headed = false } = {}) {
    if (!existsSync(FIREFOX)) return { skip: `Firefox not found at ${FIREFOX} (set FIREFOX_BIN)` };
    if (typeof WebSocket !== "function") return { skip: "this Node has no global WebSocket (Node 22+)" };
    const profile = mkdtempSync(join(tmpdir(), "sonder-chip-ff-"));
    const port = 9300 + Math.floor(Math.random() * 500);
    const firefox = spawn(FIREFOX, [...(headed ? [] : ["-headless"]), "-no-remote", "-profile", profile,
        "--remote-debugging-port", String(port), "about:blank"], { windowsHide: !headed });
    let socket;
    for (let i = 0; i < 80 && !socket; i += 1) {
        if (firefox.exitCode !== null) return { skip: `Firefox exited ${firefox.exitCode}` };
        try {
            socket = await new Promise((resolve, reject) => {
                const s = new WebSocket(`ws://127.0.0.1:${port}/session`);
                s.addEventListener("open", () => resolve(s), { once: true });
                s.addEventListener("error", reject, { once: true });
            });
        } catch { await delay(250); }
    }
    if (!socket) { firefox.kill(); return { skip: "Firefox BiDi did not open" }; }
    let id = 0;
    const pending = new Map();
    socket.addEventListener("message", (event) => {
        const message = JSON.parse(event.data);
        pending.get(message.id)?.(message);
        pending.delete(message.id);
    });
    const send = async (method, params = {}) => {
        const message = await new Promise((resolve, reject) => {
            const number = ++id;
            const alarm = setTimeout(() => reject(new Error("BiDi timeout " + method)), 10000);
            pending.set(number, (m) => { clearTimeout(alarm); resolve(m); });
            socket.send(JSON.stringify({ id: number, method, params }));
        });
        if (message.error) throw new Error(JSON.stringify(message));
        return message.result;
    };
    const { capabilities } = await send("session.new", { capabilities: { alwaysMatch: {} } });
    const tab = (context) => {
        const actions = (type, inputId, list) => send("input.performActions", { context, actions: [{ type, id: inputId,
            ...(type === "pointer" ? { parameters: { pointerType: "mouse" } } : {}), actions: list }] });
        return {
            evaluate: async (expression) => {
                const result = await send("script.evaluate", { expression: `(async()=>JSON.stringify(await (${expression})))()`,
                    awaitPromise: true, target: { context } });
                if (result.type !== "success") throw new Error(JSON.stringify(result));
                return result.result.value === undefined ? undefined : JSON.parse(result.result.value);
            },
            key: async (name, { ctrl = false, shift = false } = {}) => {
                const value = BIDI_KEYS[name] || name;
                const mods = [...(ctrl ? [BIDI_KEYS.Control] : []), ...(shift ? [BIDI_KEYS.Shift] : [])];
                await actions("key", "keyboard", [...mods.map((m) => ({ type: "keyDown", value: m })),
                    { type: "keyDown", value }, { type: "keyUp", value },
                    ...mods.reverse().map((m) => ({ type: "keyUp", value: m }))]);
            },
            drag: async (a, b) => {
                const move = (x, y, duration) => ({ type: "pointerMove", x, y, duration });
                await actions("pointer", "mouse", [move(a.x, a.y, 0), { type: "pointerDown", button: 0 },
                    ...[0.25, 0.5, 0.75, 1].map((k) => move(Math.round(a.x + (b.x - a.x) * k),
                        Math.round(a.y + (b.y - a.y) * k), 65)), { type: "pointerUp", button: 0 }]);
            },
            bringToFront: () => send("browsingContext.activate", { context }),
        };
    };
    const { contexts: [{ context }] } = await send("browsingContext.getTree");
    await send("browsingContext.setViewport", { context, viewport: { width: 900, height: 700 } });
    await send("browsingContext.navigate", { context, url, wait: "complete" });
    return {
        name: "firefox", version: `${capabilities.browserName} ${capabilities.browserVersion}`,
        ...tab(context),
        newTab: async (tabUrl) => {
            const { context: created } = await send("browsingContext.create", { type: "tab" });
            await send("browsingContext.navigate", { context: created, url: tabUrl, wait: "complete" });
            return tab(created);
        },
        close: async () => { socket.close(); firefox.kill(); await delay(300);
            try { rmSync(profile, { recursive: true, force: true }); } catch {} },
    };
}

export async function chromiumDriver(url, { headed = false } = {}) {
    const path = findPlaywright();
    if (!path) return { skip: "playwright-core not found (set PLAYWRIGHT_CORE)" };
    const { chromium } = await import(pathToFileURL(join(path, "index.mjs")).href)
        .catch(() => import(pathToFileURL(join(path, "index.js")).href));
    let browser;
    try { browser = await chromium.launch({ channel: "chrome", headless: !headed }); }
    catch (error) { return { skip: `Chrome could not start: ${error.message.split("\n")[0]}` }; }
    const context = await browser.newContext({ viewport: { width: 900, height: 700 } });
    const tab = (page) => ({
        evaluate: (expression) => page.evaluate(`(async()=>await (${expression}))()`),
        key: (name, { ctrl = false, shift = false } = {}) =>
            page.keyboard.press(`${ctrl ? "Control+" : ""}${shift ? "Shift+" : ""}${name}`),
        drag: async (a, b) => {
            await page.mouse.move(a.x, a.y); await page.mouse.down();
            for (const k of [0.25, 0.5, 0.75, 1])
                await page.mouse.move(Math.round(a.x + (b.x - a.x) * k), Math.round(a.y + (b.y - a.y) * k), { steps: 3 });
            await page.mouse.up();
        },
        bringToFront: () => page.bringToFront(),
    });
    const page = await context.newPage();
    await page.goto(url);
    return {
        name: "chromium", version: `chrome ${browser.version()}`,
        ...tab(page),
        newTab: async (tabUrl) => { const created = await context.newPage(); await created.goto(tabUrl); return tab(created); },
        close: () => browser.close(),
    };
}
