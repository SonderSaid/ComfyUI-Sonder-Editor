// Runs the Output Views linked-playback fixture in headless Chrome and judges it.
//
//   node tests/browser/views_barcode/run.mjs
//
// Needs the media (make_media.py), installed Google Chrome (H.264), and a
// playwright-core module: PLAYWRIGHT_CORE=<path to its package> or the npx
// cache (%LOCALAPPDATA%/npm-cache/_npx/*/node_modules/playwright-core).
// Exit 0 = PASS, 1 = FAIL, 77 = SKIPPED (no browser, no playwright, no media):
// a skip is never a pass.
import { createServer } from "node:http";
import { createReadStream, existsSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { extname, join, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const ROOT = resolve(HERE, "..", "..", "..");
const SKIP = 77;
const skip = reason => { console.log(`SKIP: ${reason}`); process.exit(SKIP); };

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

const TYPES = { ".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html", ".mp4": "video/mp4", ".wav": "audio/wav", ".css": "text/css" };

// A static server over the repository with byte ranges (media seeks need them).
function serve() {
    const server = createServer((request, response) => {
        const path = resolve(ROOT, "." + decodeURIComponent(new URL(request.url, "http://x").pathname));
        if (!path.startsWith(ROOT + sep) || !existsSync(path) || statSync(path).isDirectory()) {
            response.writeHead(404).end();
            return;
        }
        const size = statSync(path).size;
        const type = TYPES[extname(path)] || "application/octet-stream";
        const range = /bytes=(\d*)-(\d*)/.exec(request.headers.range || "");
        if (range) {
            const start = range[1] ? Number(range[1]) : 0;
            const end = range[2] ? Math.min(Number(range[2]), size - 1) : size - 1;
            response.writeHead(206, { "Content-Type": type, "Content-Length": end - start + 1, "Content-Range": `bytes ${start}-${end}/${size}`,
                "Accept-Ranges": "bytes" });
            createReadStream(path, { start, end }).pipe(response);
            return;
        }
        response.writeHead(200, { "Content-Type": type, "Content-Length": size, "Accept-Ranges": "bytes" });
        createReadStream(path).pipe(response);
    });
    return new Promise(resolveServer => server.listen(0, "127.0.0.1", () => resolveServer(server)));
}

const media = join(HERE, "media");
if (!["composite.mp4", "out12.mp4", "out25.mp4", "out30.mp4", "timeline.wav"].every(name => existsSync(join(media, name)))) {
    skip("media missing: run make_media.py first");
}
const playwrightPath = findPlaywright();
if (!playwrightPath) skip("playwright-core not found (set PLAYWRIGHT_CORE)");
const { chromium } = await import(pathToFileURL(join(playwrightPath, "index.mjs")).href).catch(() => import(pathToFileURL(join(playwrightPath, "index.js")).href));
let browser;
try {
    browser = await chromium.launch({ channel: "chrome", headless: true,
        args: ["--autoplay-policy=no-user-gesture-required", "--disable-gpu", "--disable-accelerated-video-decode", "--mute-audio"] });
} catch (error) {
    skip(`Chrome could not start: ${error.message.split("\n")[0]}`);
}
const server = await serve();
const port = server.address().port;
let result = null;
let pageErrors = [];
try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    page.on("pageerror", error => pageErrors.push(String(error)));
    // A failed load is judged by its URL (the console line has none); the
    // browser's own favicon probe is not the page's.
    page.on("console", message => {
        if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) pageErrors.push(message.text());
    });
    page.on("response", response => {
        if (response.status() >= 400 && !response.url().endsWith("/favicon.ico")) pageErrors.push(`${response.status()} ${response.url()}`);
    });
    await page.goto(`http://127.0.0.1:${port}/tests/browser/views_barcode/page.html`);
    await page.waitForFunction(() => window.__fixtureDone === true, null, { timeout: 180000 });
    result = await page.evaluate(() => window.__fixtureResult);
} catch (error) {
    pageErrors.push(String(error));
} finally {
    await browser.close();
    server.close();
}

const checks = [];
const check = (name, ok, detail) => checks.push({ name, ok: !!ok, detail });
if (result) {
    for (const [key, misses] of Object.entries(result.paused.mismatches)) check(`paused frames exact (${key})`, misses.length === 0, misses);
    for (const stall of result.playing.stalls) check(`stall ${stall.kind} recovers within 1 s`, stall.recovery_ms <= 1000, stall.recovery_ms);
    check("the main clock never rebuffers because of a pane", result.playing.rebuffer_holds === 0, result.playing.rebuffer_holds);
    const calibrated = result.audio.calibration.claimed.heard;
    check("audio calibration: the owner is heard, muted elements are not", calibrated.length === 1 && calibrated[0] === "pane:25", calibrated);
    for (const [want, got] of result.audio.steps) check(`exactly one audible: ${want}`, got.heard.length === 1 && got.heard[0] === want, got.heard);
}
const passed = !!result && !pageErrors.length && checks.every(c => c.ok);
const report = { verdict: passed ? "PASS" : "FAIL", at: new Date().toISOString(), checks, pageErrors, result };
writeFileSync(join(HERE, "results.json"), JSON.stringify(report, null, 2));
for (const c of checks) console.log(`${c.ok ? "ok  " : "FAIL"} ${c.name}${c.ok ? "" : ` ${JSON.stringify(c.detail)}`}`);
if (pageErrors.length) console.log("page errors:", pageErrors.slice(0, 5));
if (result) {
    console.log("steady p95 error (s):", JSON.stringify(result.playing.steady), "bound:", result.bound_s.toFixed(3));
    console.log("D2 presented fps:", JSON.stringify(result.d2));
    console.log("catching-up reports:", JSON.stringify(result.playing.catching_up_reports), "at start:", JSON.stringify(result.playing.start_catching_up));
}
console.log(report.verdict);
process.exit(passed ? 0 : 1);
