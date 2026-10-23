"""Sonder App frontend (SDK v2 Phase 4b), driven under Node with the real modules.

* ``SdkWidgetHost``: the editor position save is debounced, carries the session
  and tab headers, waits while the tab is displaced, survives a dropped
  connection, and is folded into Generate; ``generation_status`` snapshots are
  ordered by ``seq``; a refusal naming another owner or an ended session is
  reported once.
* Session storage: the secret is keyed by session id, the Exit flag is read
  once, and a blocked store never throws.
* Generate controls: the button's faces, the progress line, the outcome
  toasts (which coalesce, so carry no take index), the refusal notices, and a
  fake-DOM pass through click → drain → generate and Cancel all.
* The queue row's error line, the gallery's Open Source Workflow gate, the
  ``generation_status`` dispatch, and the project creation shared with the
  canvas's "+ Create New".

The Launcher and tab pages themselves are checked live (Phase 4b manual rows).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"


def _url(name):
    return json.dumps((JS / name).as_uri())


SCRIPT = r"""
class FakeEl {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.style = { cssText: "", display: "" };
        this.dataset = {};
        this.children = [];
        this.listeners = {};
        this.textContent = "";
        this.title = "";
        this.attributes = {};
    }
    append(...nodes) { for (const n of nodes) this.children.push(n); }
    appendChild(n) { this.children.push(n); return n; }
    replaceChildren(...nodes) { this.children = nodes; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener() {}
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return this.attributes[k] ?? null; }
    click() { for (const fn of this.listeners.click || []) fn({ preventDefault() {}, stopPropagation() {} }); }
}
globalThis.document = {
    createElement: (tag) => new FakeEl(tag),
    createTextNode: (text) => ({ textContent: text }),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {}, body: new FakeEl("body"),
};
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.window = {
    comfyAPI: { api: { api: { apiURL: (path) => path } } },
    location: { href: "http://test/" }, localStorage: globalThis.localStorage,
    addEventListener() {}, removeEventListener() {},
};
globalThis.location = { href: "http://test/" };
const sockets = [];
globalThis.WebSocket = class { static OPEN = 1; constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); } send() {} close() {} };

const host = await import(__HOST__);
const gc = await import(__GC__);
const notes = await import(__NOTES__);
const queue = await import(__QUEUE__);
const gallery = await import(__GALLERY__);
const sync = await import(__SYNC__);
const pc = await import(__PC__);

const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };
const out = {};

// ── SdkWidgetHost ─────────────────────────────────────────────────────────
function harness(responder = () => ({ status: 200, body: { ok: true } })) {
    const timers = new Map();
    let nextTimer = 1;
    const calls = [];
    const h = new host.SdkWidgetHost({
        sessionId: "sdk-1", secret: "s3cret", projectId: "P", tabSessionId: "tab-1",
        defaults: { scene_id: "", selection_start: 0, selection_end: 0 },
        initialValues: { selection_end: 5 },
        fetchImpl: async (url, init = {}) => {
            const call = { url, method: init.method || "GET", headers: init.headers || {},
                body: init.body ? JSON.parse(init.body) : null, keepalive: !!init.keepalive };
            calls.push(call);
            const resp = await responder(call);
            if (resp instanceof Error) throw resp;
            return { ok: resp.status < 400, status: resp.status, json: async () => resp.body };
        },
        setTimeoutImpl: (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id; },
        clearTimeoutImpl: (id) => timers.delete(id),
        debounceMs: 400,
    });
    const fire = async () => { const pending = [...timers.values()]; timers.clear(); pending.forEach((t) => t.fn()); await settle(); };
    return { h, timers, calls, fire };
}

{
    const { h, timers, calls, fire } = harness();
    out.initial = { ...h.values };
    h.setValue("selection_start", 1);
    h.setValue("selection_start", 2);
    h.setValue("selection_end", 9);
    out.debounce_timers = timers.size;
    out.debounce_ms = [...timers.values()][0].ms;
    await fire();
    out.debounce_calls = calls.map((c) => ({ url: c.url, method: c.method, headers: c.headers, body: c.body }));
    h.setValue("selection_end", 9);
    out.noop_timers = timers.size;
    out.node_id = h.getNodeId();
}
{
    const { h, timers, calls, fire } = harness();
    h.setOwnerState("displaced");
    h.setValue("selection_start", 4);
    out.displaced_timers = timers.size;
    h.setOwnerState("ended");
    out.ended_can_write = h.canWrite();
    h.setOwnerState("active");
    out.resumed_timers = timers.size;
    await fire();
    out.resumed_calls = calls.length;
    out.resumed_value = calls[0]?.body?.values?.selection_start;
}
{
    const lost = [];
    const { h, fire } = harness(() => ({ status: 409, body: { ok: false, code: "not_owner" } }));
    h.onOwnershipLost = (code) => lost.push(code);
    h.setValue("selection_start", 3);
    await fire();
    out.not_owner_lost = lost;
}
{
    let fail = true;
    const { h, calls, fire } = harness(() => (fail ? new Error("offline") : { status: 200, body: { ok: true } }));
    const originalWarn = console.warn; console.warn = () => {};
    h.setValue("selection_start", 7);
    await fire();
    console.warn = originalWarn;
    fail = false;
    const ok = await h.flushValues({ keepalive: true });
    out.offline_retry = { attempts: calls.length, ok, keepalive: calls[1]?.keepalive, value: calls[1]?.body?.values?.selection_start };
}
{
    const { h, timers, calls } = harness(() => ({ status: 202, body: { ok: true, run_ids: ["r1"] } }));
    h.setValue("selection_start", 6);
    const result = await h.generate(12, { renderCacheMaxBytes: -1 });
    out.generate = {
        timers_after: timers.size, ok: result.ok, url: calls[0].url, method: calls[0].method,
        count: calls[0].body.count, cache: calls[0].body.render_cache_max_bytes,
        value: calls[0].body.editor_values.selection_start, headers: calls[0].headers,
    };
    await h.generate(0);
    out.generate_min = calls[1].body.count;
    await h.cancelAll();
    out.cancel = { url: calls[2].url, method: calls[2].method };
}
{
    const ended = [];
    const { h } = harness(() => ({ status: 404, body: { ok: false, code: "session_ended" } }));
    h.onOwnershipLost = (code) => ended.push(code);
    const result = await h.generate(1);
    out.generate_ended = { ok: result.ok, code: result.payload.code, lost: ended };
}
{
    const { h } = harness(() => new Error("down"));
    const result = await h.generate(1);
    out.generate_network = result.payload.code;
}
{
    const { h } = harness();
    const seen = [];
    h.subscribe((status) => seen.push(status.seq));
    const r = [];
    r.push(h.applyStatus({ session_id: "sdk-1", seq: 3, runs: [{ run_id: "a", state: "queued" }] }));
    r.push(h.applyStatus({ session_id: "sdk-1", seq: 3, runs: [] }));
    r.push(h.applyStatus({ session_id: "sdk-1", seq: 3, runs: [{ run_id: "a", state: "running" }] }, { fetched: true }));
    r.push(h.applyStatus({ session_id: "sdk-1", seq: 2, runs: [] }, { fetched: true }));
    r.push(h.applyStatus({ session_id: "sdk-other", seq: 9, runs: [] }));
    r.push(h.applyStatus({ session_id: "sdk-1", seq: "x", runs: [] }));
    out.status_order = { applied: r, seen, active: h.activeRuns().map((run) => run.state) };
}

// ── Edits a refused request did not save are kept ─────────────────────────
async function refusedGenerate(response) {
    const { h, timers, calls } = harness(() => response);
    h.setValue("selection_start", 11);
    const lost = [];
    h.onOwnershipLost = (code) => lost.push(code);
    const result = await h.generate(1);
    return { ok: result.ok, unsaved: h.hasUnsavedValues(), timers: timers.size, lost, posts: calls.length };
}
out.generate_restore = {
    not_installed: await refusedGenerate({ status: 409, body: { ok: false, code: "sdk_not_installed" } }),
    not_owner: await refusedGenerate({ status: 409, body: { ok: false, code: "not_owner" } }),
    busy: await refusedGenerate({ status: 429, body: { ok: false, code: "too_many_runs" } }),
    network: await refusedGenerate(new Error("down")),
    refused_workflow: await refusedGenerate({ status: 409, body: { ok: false, code: "generate_refused", problems: [] } }),
    unconfirmed: await refusedGenerate({ status: 502, body: { ok: false, run_ids: [], error: { code: "submit_failed" } } }),
};
{
    const { h, timers, calls, fire } = harness(() => ({ status: 409, body: { ok: false, code: "not_owner" } }));
    h.setValue("selection_start", 12);
    await fire();
    const afterRefusal = { unsaved: h.hasUnsavedValues(), timers: timers.size, puts: calls.length };
    const { h: h2, timers: t2, fire: fire2 } = harness(() => ({ status: 400, body: { ok: false, code: "invalid_values" } }));
    h2.setValue("selection_start", -5);
    await fire2();
    out.flush_refusals = { not_owner: afterRefusal, invalid_dropped: h2.hasUnsavedValues(), invalid_timers: t2.size };
}
{
    // A save still in flight is answered before Generate is sent.
    let releasePut;
    const order = [];
    const { h, fire } = harness((call) => {
        order.push(`send ${call.method}`);
        if (call.method === "PUT") return new Promise((resolve) => { releasePut = () => { order.push("put answered"); resolve({ status: 200, body: { ok: true } }); }; });
        return { status: 202, body: { ok: true, run_ids: ["r"] } };
    });
    // harness's fetch awaits the responder's value; a promise is awaited too.
    h.setValue("selection_start", 13);
    void fire();
    await settle();
    const pending = h.generate(1);
    await settle();
    const beforeRelease = [...order];
    releasePut();
    await pending;
    out.inflight_order = { beforeRelease, after: order };
}
{
    // An ended session publishes a terminal status and ignores later snapshots.
    const { h } = harness();
    const seen = [];
    h.subscribe((status) => seen.push({ seq: status.seq, runs: status.runs.length, ended: !!status.ended }));
    h.applyStatus({ session_id: "sdk-1", seq: 1, runs: [{ run_id: "a", state: "running" }] });
    h.setOwnerState("ended");
    h.setOwnerState("ended");
    const late = h.applyStatus({ session_id: "sdk-1", seq: 2, runs: [{ run_id: "a", state: "landed" }] });
    out.ended_status = { seen, late };
}

{
    const releases = [];
    const { h, calls } = harness(call => call.method === "PUT"
        ? new Promise(resolve => releases.push(resolve)) : { status: 202, body: { run_ids: ["r"] } });
    h.setValue("selection_start", 20); void h.flushValues();
    h.setValue("selection_start", 30); void h.flushValues();
    await settle();
    releases[1]({status: 200, body: {stale: false}}); await settle();
    h.setValue("selection_start", 40);
    const pending = h.generate(1); await settle();
    const before = calls.map(c => c.method);
    releases[0]({status: 200, body: {stale: true}}); await pending;
    h.setValue("selection_start", 50); void h.flushValues({keepalive: true}); await settle();
    const draining = h.drainSaves({timeoutMs: 10});
    let drained = false; draining.then(() => drained = true); await settle();
    const exitWaited = !drained;
    releases[2]({status: 200, body: {stale: true}}); await draining;
    out.all_saves = { before, exitWaited, sequences: calls.map(c => c.body.seq ?? c.body.editor_values_seq),
        keepalive: calls.at(-1).keepalive, unsaved: h.hasUnsavedValues() };
}
{
    const { h, timers } = harness(() => new Promise(() => {}));
    h.setValue("selection_start", 1); void h.flushValues();
    const draining = h.drainSaves({timeoutMs: 10});
    [...timers.values()].find(t => t.ms === 10).fn();
    out.drain_timeout = await draining;
    h.destroy();
}

// ── Session ownership state machine ───────────────────────────────────────
function ownershipHarness({ claimResults = [], heartbeatResults = [] } = {}) {
    const { h } = harness();
    const intervals = new Map();
    let nextInterval = 1;
    const log = [];
    let claims = 0;
    let beats = 0;
    const ownership = host.createSdkOwnership({
        host: h,
        tabSessionId: "tab-1",
        claim: async () => { claims += 1; const r = claimResults.shift(); if (r instanceof Error) throw r; return r; },
        heartbeat: async () => { beats += 1; const r = heartbeatResults.shift(); if (r instanceof Error) throw r; return r; },
        onChange: (state) => log.push(state),
        onReclaimed: () => log.push("reclaimed"),
        setIntervalImpl: (fn, ms) => { const id = nextInterval++; intervals.set(id, { fn, ms }); return id; },
        clearIntervalImpl: (id) => intervals.delete(id),
    });
    return { h, ownership, intervals, log, counts: () => ({ claims, beats }) };
}
const mine = (status = "active") => ({ ok: true, owner: { session_id: "tab-1", status } });
const theirs = { ok: false, code: "locked", owner: { session_id: "tab-2", status: "active" } };
const noOwner = { ok: false, code: "no_owner", owner: null };
const gone = { ok: false, code: "invalid_sdk_session", status: 409 };
{
    const o = ownershipHarness({ heartbeatResults: [mine("orphaned"), theirs], claimResults: [mine()] });
    o.ownership.start("active");
    const started = { state: o.h.ownerState, interval: [...o.intervals.values()][0]?.ms };
    await o.ownership.beat();
    const orphaned = o.h.ownerState;
    await o.ownership.beat();
    const displaced = { state: o.h.ownerState, heartbeat: o.ownership.heartbeatRunning };
    await o.ownership.beat();
    o.ownership.handleSessionChanged({ owner: null });
    await o.ownership.handleOwnershipLost("not_owner");
    await settle();
    const passive = { state: o.h.ownerState, counts: o.counts() };
    await o.ownership.takeOver();
    out.own_displace = { started, orphaned, displaced, passive,
        after_take_over: { state: o.h.ownerState, heartbeat: o.ownership.heartbeatRunning, counts: o.counts() }, log: o.log };
}
{
    // A failed Take over leaves the tab displaced, not apparently live.
    const o = ownershipHarness({ heartbeatResults: [theirs], claimResults: [new Error("offline"), { ok: true, owner: { session_id: "tab-1", status: "active" } }] });
    o.ownership.start("active");
    await o.ownership.beat();
    await o.ownership.takeOver();
    const afterFailure = { state: o.h.ownerState, heartbeat: o.ownership.heartbeatRunning };
    await o.ownership.takeOver();
    out.own_failed_take_over = { afterFailure, retried: o.h.ownerState };
}
{
    // `not_owner` with nobody owning the session: claimed back quietly.
    const o = ownershipHarness({ heartbeatResults: [noOwner], claimResults: [mine()] });
    o.ownership.start("active");
    await o.ownership.handleOwnershipLost("not_owner");
    await settle();
    const quiet = { state: o.h.ownerState, log: [...o.log], counts: o.counts() };
    // ...and with another tab owning it: displaced, without a claim.
    const o2 = ownershipHarness({ heartbeatResults: [theirs] });
    o2.ownership.start("active");
    await o2.ownership.handleOwnershipLost("not_owner");
    out.own_not_owner = { quiet, other: { state: o2.h.ownerState, counts: o2.counts() } };
}
{
    // A restarted ComfyUI no longer knows the session: ended, heartbeat stopped.
    const o = ownershipHarness({ heartbeatResults: [noOwner], claimResults: [gone] });
    o.ownership.start("active");
    await o.ownership.beat();
    await settle();
    const ended = { state: o.h.ownerState, heartbeat: o.ownership.heartbeatRunning };
    await o.ownership.takeOver();
    const o2 = ownershipHarness();
    o2.ownership.start("active");
    await o2.ownership.handleOwnershipLost("session_ended");
    out.own_ended = { ended, claims_after: o.counts().claims, by_refusal: o2.h.ownerState };
}
{
    // A network failure keeps the heartbeat going and recovers on the next beat.
    const o = ownershipHarness({ heartbeatResults: [new Error("offline"), mine()] });
    o.ownership.start("active");
    await o.ownership.beat();
    const down = { state: o.h.ownerState, heartbeat: o.ownership.heartbeatRunning, canWrite: o.h.canWrite() };
    await o.ownership.beat();
    out.own_network = { down, back: o.h.ownerState };
}
{
    // session_changed: mine, another tab's, released; concurrent reclaims claim once.
    const o = ownershipHarness({ claimResults: [mine()] });
    o.ownership.start("active");
    o.ownership.handleSessionChanged({ owner: { session_id: "tab-1", status: "orphaned" } });
    const orphaned = o.h.ownerState;
    o.ownership.handleSessionChanged({ owner: null });
    o.ownership.handleSessionChanged({ owner: null });
    await settle();
    const reclaimed = { state: o.h.ownerState, claims: o.counts().claims };
    o.ownership.handleSessionChanged({ owner: { session_id: "tab-2", status: "active" } });
    const displaced = o.h.ownerState;
    o.ownership.stop();
    o.ownership.handleSessionChanged({ owner: null });
    out.own_events = { orphaned, reclaimed, displaced, stopped_heartbeat: o.ownership.heartbeatRunning };
}

// ── Session storage ───────────────────────────────────────────────────────
{
    const map = new Map();
    const store = { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, String(v)), removeItem: (k) => map.delete(k) };
    const stored = host.storeSdkSecret("sdk-9", "abc", store);
    const read = host.readSdkSecret("sdk-9", store);
    const keys = [...map.keys()];
    host.forgetSdkSecret("sdk-9", store);
    host.writeSdkExitFlag({ session_id: "sdk-9", project: "P", template_id: "t" }, store);
    const flag = host.takeSdkExitFlag(store);
    const again = host.takeSdkExitFlag(store);
    host.writeSdkExitFlag({ project: "P" }, store);
    const noSession = host.takeSdkExitFlag(store);
    const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
    out.storage = {
        stored, read, keys, after_forget: host.readSdkSecret("sdk-9", store),
        flag: flag && { session_id: flag.session_id, project: flag.project, template_id: flag.template_id, has_at: typeof flag.at === "number" },
        again, noSession,
        broken: [host.storeSdkSecret("x", "y", broken), host.readSdkSecret("x", broken), host.takeSdkExitFlag(broken)],
        no_store: [host.storeSdkSecret("x", "y", null), host.readSdkSecret("x", null)],
    };
}

// ── Generate controls: pure parts ─────────────────────────────────────────
out.faces = {
    idle: gc.generateButtonState([]),
    finished: gc.generateButtonState([{ state: "landed" }, { state: "failed" }]),
    submitting: gc.generateButtonState([], { submitting: true }),
    active: gc.generateButtonState([{ state: "queued" }, { state: "landed" }]),
    active_while_sending: gc.generateButtonState([{ state: "submitting" }], { submitting: true }),
    canceling: gc.generateButtonState([{ state: "running", canceling: true }, { state: "queued", canceling: true }]),
    partly_canceling: gc.generateButtonState([{ state: "running", canceling: true }, { state: "queued" }]),
};
out.progress = {
    queued: gc.describeBatchProgress([{ state: "queued" }, { state: "submitting" }]),
    running: gc.describeBatchProgress([{ state: "landed" }, { state: "running", progress_node: "Sampler", progress_count: {value: 2, max: 8} }, { state: "queued" }]),
    finishing: gc.describeBatchProgress([{ state: "finishing" }]),
    null_progress: gc.describeBatchProgress([{ state: "running", progress: null }]),
    empty: gc.describeBatchProgress([]),
    named: gc.describeBatchProgress([{ state: "landed", template_name: "Upscale" }, { state: "running", template_name: "LTX Edit" }]),
};
out.branch_progress = {
    finishing: gc.describeBatchProgress([{ state: "finishing", purpose: "branch" }]),
    done: gc.describeBatchProgress([{ state: "completed", purpose: "branch" }, { state: "queued", purpose: "branch" }]),
};
out.branch_toasts = ["completed", "landed", "failed", "canceled", "ended"].map((state) =>
    gc.outcomeToast({ state, purpose: "branch", note: "Why." }));
out.toasts_pure = ["landed", "failed", "canceled", "ended", "running"].map((state) =>
    gc.outcomeToast({ state, index: 2, count: 3, note: state === "landed" ? "" : "Because." }));
out.notices = {
    ok: gc.generateResultNotice({ ok: true, status: 202, payload: { run_ids: ["a", "b"], error: null } }, 2),
    partial: gc.generateResultNotice({ ok: true, status: 202, payload: { run_ids: ["a"], error: { error: "ComfyUI did not confirm this run." } } }, 3),
    refused: gc.generateResultNotice({ ok: false, status: 409, payload: { code: "generate_refused", problems: ["“Seed”: bad.", "“Steps”: worse."] } }, 1),
    not_owner: gc.generateResultNotice({ ok: false, status: 409, payload: { code: "not_owner" } }, 1),
    ended: gc.generateResultNotice({ ok: false, status: 404, payload: { code: "session_ended" } }, 1),
    not_installed: gc.generateResultNotice({ ok: false, status: 409, payload: { code: "sdk_not_installed" } }, 1),
    proxy_not_installed: gc.generateResultNotice({ ok: false, status: 503, payload: { code: "proxy_not_installed", error: "The comfy-api-proxy package is not installed." } }, 1),
    uninstalling: gc.generateResultNotice({ ok: false, status: 409, payload: { code: "sdk_uninstalling", error: "The Sonder App packages are being removed." } }, 1),
    busy: gc.generateResultNotice({ ok: false, status: 429, payload: { code: "too_many_runs", error: "Too many takes are already waiting." } }, 1),
    proxy: gc.generateResultNotice({ ok: false, status: 503, payload: { code: "proxy_unavailable", error: "No free port." } }, 1),
    network: gc.generateResultNotice({ ok: false, status: 0, payload: { code: "network", error: "Failed to fetch" } }, 1),
    missing_nodes: gc.generateResultNotice({ ok: false, status: 409, payload: { code: "missing_nodes", error: "This workflow needs nodes that aren't installed: X." } }, 1),
    submit_failed: gc.generateResultNotice({ ok: false, status: 502, payload: { ok: false, run_ids: [], error: { code: "submit_failed", error: "Invalid prompt." } } }, 1),
};
out.clamp = [gc.clampGenerateCount(0), gc.clampGenerateCount(3.4), gc.clampGenerateCount(99), gc.clampGenerateCount("x")];

{
    const { h } = harness(() => ({status: 409, body: {code: "canceled_before_submit"}}));
    h.setValue("selection_start", 12);
    await h.generate(1);
    out.canceled_saved = !h.hasUnsavedValues();
    out.canceled_notice = gc.generateResultNotice({ok:false,payload:{code:"canceled_before_submit"}},1);
    h.applyStatus({session_id:"sdk-1",seq:9,runs:[{run_id:"f",state:"failed"}]});
    const older = h.applyStatus({session_id:"sdk-1",seq:8,runs:[{run_id:"f",state:"failed"}],details:{f:"ValueError: detail"}},{fetched:true});
    out.old_details = {older,seq:h.status.seq,detail:h.details.get("f")};
    h.applyStatus({session_id:"sdk-1",seq:10,runs:[{run_id:"f",state:"failed"}]});
    out.details_after_ws = h.details.get("f");
}

{
    let release;
    const { h, calls } = harness();
    h.flushControlsPanel = () => new Promise(resolve => release = resolve);
    h.setValue("selection_start", 42);
    const pending = h.generate(1);
    await settle();
    await h.cancelAll();
    release(true);
    const result = await pending;
    out.cancel_during_host_drain = {code:result.payload.code, methods:calls.map(c=>c.method),
        generated:calls.some(c=>c.url.endsWith("/generate")), saved:calls.find(c=>c.method==="PUT")?.body.values.selection_start};
}

// ── Generate controls: fake DOM ───────────────────────────────────────────
const captured = [];
const seenIds = new Set();
let live = [];
notes.subscribe((list) => {
    live = list;
    for (const n of list) {
        if (seenIds.has(n.id)) continue;
        seenIds.add(n.id);
        captured.push({ tier: n.tier, message: n.message, source: n.source || "", foreground: !!n.foreground });
    }
});
function fakeGenerationHost(initial = { seq: -1, runs: [] }) {
    return {
        listeners: new Set(), status: initial, generated: [], cancels: 0,
        subscribe(fn) { this.listeners.add(fn); fn(this.status); return () => this.listeners.delete(fn); },
        emit(status) { this.status = status; for (const fn of [...this.listeners]) fn(status); },
        async generate(count, opts) { this.generated.push([count, {renderCacheMaxBytes: opts.renderCacheMaxBytes}]); return { ok: true, status: 202, payload: { run_ids: ["r"], error: null } }; },
        async cancelAll() { this.cancels += 1; return { ok: true, status: 200, payload: { canceling: [] } }; },
    };
}
function mount(fake, order, before = null) {
    return gc.createGenerateControls({
        host: fake,
        makeButton: (text, title) => { const b = document.createElement("button"); b.textContent = text; b.title = title; return b; },
        setVariant: (b, variant) => { b.dataset.variant = variant; },
        makeStepButtons: (title, onStep) => { const w = document.createElement("span"); w.onStep = onStep; return w; },
        buttonOptions: { primary: {}, secondary: {} },
        beforeGenerate: before || (async () => { order.push("drain"); }),
        renderCacheMaxBytes: () => 123,
    });
}
{
    const fake = fakeGenerationHost();
    const order = [];
    const controls = mount(fake, order);
    const [button, countGroup, cancelButton] = controls.element.children;
    const [countLabel, steps] = countGroup.children;
    const faces = [];
    faces.push([button.textContent, button.dataset.variant, countLabel.textContent]);
    steps.onStep("up"); steps.onStep("up"); steps.onStep("down"); steps.onStep("up");
    faces.push(countLabel.textContent);
    for (let i = 0; i < 10; i++) steps.onStep("up");
    faces.push(countLabel.textContent);
    for (let i = 0; i < 10; i++) steps.onStep("down");
    steps.onStep("up"); steps.onStep("up");
    button.click();
    await settle();
    out.dom_generate = { order, generated: fake.generated };
    {
        // Sonder App: the click holds its workflow through beforeGenerate and
        // sends the one on screen at the click; a switch in flight disables it.
        const app = fakeGenerationHost();
        const events = [];
        Object.assign(app, {
            templateId: "A", switching: false,
            holdWorkflow() { events.push("hold"); return () => events.push("release"); },
            async generate(count, opts) { events.push(["generate", opts.templateId]); return { ok: true, status: 202, payload: { run_ids: ["r"], error: null } }; },
        });
        const appControls = mount(app, [], async () => { events.push("before"); app.templateId = "B"; });
        const [appButton] = appControls.element.children;
        appButton.click();
        await settle();
        out.dom_hold = events;
        app.switching = true;
        app.emit(app.status);
        out.dom_switching_disabled = appButton.disabled;
        appControls.destroy();
    }

    fake.emit({ seq: 0, runs: [{ run_id: "a", state: "queued", index: 1, count: 2 }, { run_id: "b", state: "submitting", index: 2, count: 2 }] });
    faces.push([button.textContent, cancelButton.textContent, countGroup.style.display]);
    const progress = live.find((n) => n.source === "sdk-generate");
    out.dom_progress = progress && { tier: progress.tier, foreground: progress.foreground, message: progress.message };
    cancelButton.click();
    await settle();
    out.dom_cancels = fake.cancels;
    fake.emit({ seq: 1, runs: [{ run_id: "a", state: "running", canceling: true, progress: 0.5 }, { run_id: "b", state: "queued", canceling: true }] });
    faces.push(cancelButton.textContent);
    fake.emit({ seq: 2, runs: [{ run_id: "a", state: "landed", index: 1, count: 2 }, { run_id: "b", state: "canceled", index: 2, count: 2, note: "Canceled before it started." }] });
    faces.push([button.textContent, countGroup.style.display]);
    out.dom_faces = faces;
    out.dom_progress_after = live.filter((n) => n.source === "sdk-generate").length;
    out.dom_toasts = captured.filter((n) => n.source.startsWith("sdk-generate-")).map((n) => [n.tier, n.message]);
    controls.destroy();
    out.dom_listeners_after_destroy = fake.listeners.size;
}
{
    const fake = fakeGenerationHost({seq:0,runs:[]});
    const controls = mount(fake, []);
    const [button, countGroup, cancelButton] = controls.element.children;
    let release;
    fake.generate = () => new Promise(resolve => release = resolve);
    fake.emit({seq:1,runs:[{run_id:"a",state:"running",progress_node:"Sampler"}]});
    button.click(); await settle();
    const sending = {disabled:button.disabled,label:button.textContent,cancel:cancelButton.textContent,count:countGroup.children[0].textContent};
    cancelButton.click(); await settle();
    release({ok:true,payload:{run_ids:["b"]}}); await settle();
    const sixteen = Array.from({length:16},(_,i)=>({run_id:`cap${i}`,state:"queued"}));
    fake.emit({seq:2,runs:sixteen});
    out.side_by_side = {sending,cancels:fake.cancels,capacityDisabled:button.disabled,reason:button.title};
    controls.destroy();
}
{
    const fake = fakeGenerationHost({seq:0,runs:[]});
    const controls = mount(fake, []);
    for(let i=0;i<50;i++) {
        const runs = Array.from({length:i+1},(_,n)=>({run_id:`long${n}`,state:n===i?"running":"landed"})).slice(-25);
        fake.emit({seq:i+1,runs});
    }
    out.trimmed_line = live.find(n=>n.source === "sdk-generate")?.message;
    controls.destroy();
}
{
    const fake = fakeGenerationHost({seq:0,runs:[]});
    let reads=0;
    fake.details=new Map();
    fake.refreshStatus=async()=>{
        reads++;
        fake.details.set("detail-run","ValueError: private detail");
        fake.emit(fake.status);
    };
    const controls=mount(fake,[]);
    fake.emit({seq:1,runs:[{run_id:"detail-run",state:"failed",note:"The run failed."}]});
    await settle();
    out.failure_detail={reads,detail:live.find(n=>n.source === "sdk-generate-failed")?.detail};
    controls.destroy();
}
{
    let release;
    const { h, calls } = harness();
    h.applyStatus({session_id:"sdk-1",seq:0,runs:[{run_id:"old",state:"running"}]});
    h.setValue("selection_start",43);
    const controls = mount(h, [], () => new Promise(resolve => release = resolve));
    const [button, count, cancel] = controls.element.children;
    button.click(); await settle();
    cancel.click(); await settle();
    release(); await settle();
    out.cancel_before_host = {methods:calls.map(c=>c.method),generated:calls.some(c=>c.url.endsWith("/generate")),saved:calls.find(c=>c.method==="PUT")?.body.values.selection_start};
    controls.destroy();
}
{
    // The real order: the host starts at seq -1, a fetched snapshot primes the
    // controls silently, then live snapshots announce new outcomes only.
    for (const n of [...live]) notes.dismiss(n.id);
    const before = captured.length;
    const { h } = harness();
    const controls = mount(h, []);
    h.applyStatus({ session_id: "sdk-1", seq: 7, runs: [{ run_id: "x", state: "landed" }, { run_id: "y", state: "failed", note: "boom" }] }, { fetched: true });
    h.applyStatus({ session_id: "sdk-1", seq: 8, runs: [{ run_id: "x", state: "landed" }, { run_id: "y", state: "failed", note: "boom" }, { run_id: "z", state: "failed", note: "new" }] });
    await settle();
    out.primed_toasts = captured.slice(before).map((n) => [n.tier, n.message]);
    // An ended session ends the progress line too.
    h.applyStatus({ session_id: "sdk-1", seq: 9, runs: [{ run_id: "w", state: "running", progress: 0.1 }] });
    const progressBefore = live.filter((n) => n.source === "sdk-generate").length;
    h.setOwnerState("ended");
    out.ended_progress = { before: progressBefore, after: live.filter((n) => n.source === "sdk-generate").length,
        face: controls.element.children[0].textContent };
    controls.destroy();
}

// ── The Launcher module is inert outside its page ─────────────────────────
{
    const touched = [];
    const realDocument = globalThis.document;
    const realFetch = globalThis.fetch;
    globalThis.fetch = (...args) => { touched.push(`fetch ${args[0]}`); return Promise.reject(new Error("no")); };
    globalThis.document = new Proxy({ body: { dataset: {} } }, {
        get(target, key) {
            if (key === "body") return target.body;
            touched.push(`document.${String(key)}`);
            return realDocument[key];
        },
    });
    const landing = await import(__LANDING__);
    globalThis.document = realDocument;
    globalThis.fetch = realFetch;
    out.landing_inert = {
        touched,
        marker: [landing.isLauncherPage({ body: { dataset: { sonderLauncher: "true" } } }), landing.isLauncherPage({ body: { dataset: {} } }), landing.isLauncherPage(null)],
    };
}

// ── Queue row error line ──────────────────────────────────────────────────
out.queue_error = [
    queue.queueJobErrorText({ status: "pending", error: "  Returned: the run was interrupted.  " }),
    queue.queueJobErrorText({ status: "failed", error: "Sampler error" }),
    queue.queueJobErrorText({ status: "running", error: "stale" }),
    queue.queueJobErrorText({ status: "completed", error: "old" }),
    queue.queueJobErrorText({ status: "pending" }),
];

// ── Open Source Workflow gate ─────────────────────────────────────────────
{
    const handler = { onOpenSourceWorkflow() {} };
    const mp4 = { path: "takes/a.mp4" };
    const webm = { path: "takes/a.webm" };
    const notEmbedded = { path: "takes/a.mp4", generation_summary: { has_embedded_workflow: false } };
    const embeddedWebm = { path: "takes/a.webm", generation_summary: { has_embedded_workflow: true } };
    out.gallery_gate = {
        canvas_mp4: gallery.sourceWorkflowMenuDisabled({ ...handler, canOpenSourceWorkflow: () => true }, mp4),
        no_flag_handler: gallery.sourceWorkflowMenuDisabled(handler, mp4),
        tab_mp4: gallery.sourceWorkflowMenuDisabled({ ...handler, canOpenSourceWorkflow: () => false }, mp4),
        no_handler: gallery.sourceWorkflowMenuDisabled({}, mp4),
        not_embedded: gallery.sourceWorkflowMenuDisabled({ ...handler, canOpenSourceWorkflow: () => true }, notEmbedded),
        webm_unknown: gallery.sourceWorkflowMenuDisabled({ ...handler, canOpenSourceWorkflow: () => true }, webm),
        webm_embedded: gallery.sourceWorkflowMenuDisabled({ ...handler, canOpenSourceWorkflow: () => true }, embeddedWebm),
    };
}

// ── generation_status dispatch ────────────────────────────────────────────
{
    const got = [];
    const connection = sync.connectProjectSync("P", { onGenerationStatus: (event) => got.push(event.seq), onRunFinished: () => got.push("rf") }, { clientId: "c" });
    const ws = sockets.at(-1);
    ws.onmessage({ data: JSON.stringify({ type: "generation_status", session_id: "s", seq: 4, runs: [] }) });
    ws.onmessage({ data: JSON.stringify({ type: "run_finished" }) });
    connection.close();
    out.dispatch = got;
}

// ── Project creation shared with "+ Create New" ───────────────────────────
async function createWith(projects) {
    const calls = [];
    const fetchImpl = async (url, init = {}) => {
        calls.push({ url, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null });
        if (url === "/sonder-editor/project") return { ok: true, status: 201, json: async () => ({ project_id: "pid", name: "Shot A" }) };
        if (url === "/sonder-editor/projects") return { ok: true, status: 200, json: async () => ({ projects }) };
        return { ok: true, status: 201, json: async () => ({}) };
    };
    const result = await pc.createSonderProject({ name: "  Shot A ", fps: 24, width: 960, height: 544, apiURL: (p) => p, fetchImpl });
    return { calls, folder: result.folder };
}
{
    const fresh = await createWith([{ project_id: "pid", name: "Shot A", path: "C:\\projects\\Shot-A", scene_count: 0 }]);
    const existing = await createWith([{ project_id: "pid", name: "Shot A", path: "/p/Shot-A", scene_count: 2 }]);
    let missingName = "";
    try { await pc.createSonderProject({ name: "  ", fetchImpl: async () => { throw new Error("must not fetch"); } }); }
    catch (err) { missingName = err.message; }
    let serverError = "";
    try {
        await pc.createSonderProject({ name: "x", apiURL: (p) => p,
            fetchImpl: async () => ({ ok: false, status: 400, json: async () => ({ error: "Invalid prompt channel template" }) }) });
    } catch (err) { serverError = err.message; }
    let sceneFailure = null;
    try {
        await pc.createSonderProject({ name: "y", apiURL: (p) => p, fetchImpl: async (url, init = {}) => {
            if (url === "/sonder-editor/project") return { ok: true, status: 201, json: async () => ({ project_id: "q", name: "y" }) };
            if (url === "/sonder-editor/projects") return { ok: true, status: 200, json: async () => ({ projects: [{ project_id: "q", name: "y", path: "/p/y", scene_count: 0 }] }) };
            return { ok: false, status: 500, json: async () => ({ error: "disk full" }) };
        } });
    } catch (err) { sceneFailure = { message: err.message, folder: err.folder, listed: (err.projects || []).length }; }
    out.create = {
        sceneFailure,
        post: fresh.calls[0], list: fresh.calls[1], scene: fresh.calls[2], folder: fresh.folder,
        existing_calls: existing.calls.map((c) => `${c.method} ${c.url}`), existing_folder: existing.folder,
        missingName, serverError,
        folder_name: [pc.projectFolderName({ path: "D:\\a\\b c" }), pc.projectFolderName({ path: "/x/y/" }), pc.projectFolderName(null)],
    };
}

console.log(JSON.stringify(out));
process.exit(0);
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Sonder App frontend tests")
    script = (SCRIPT
              .replace("__HOST__", _url("sdk_host.js"))
              .replace("__GC__", _url("generate_controls.js"))
              .replace("__NOTES__", _url("editor_notifications.js"))
              .replace("__QUEUE__", _url("shared_render_queue.js"))
              .replace("__GALLERY__", _url("shared_asset_gallery.js"))
              .replace("__SYNC__", _url("cross_tab_sync.js"))
              .replace("__PC__", _url("project_create.js"))
              .replace("__LANDING__", _url("sdk_landing.js")))
    result = subprocess.run([node, "--input-type=module"], input=script, capture_output=True,
                            text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_position_saves_are_debounced_and_carry_the_session_headers(results):
    assert results["initial"] == {"scene_id": "", "selection_start": 0, "selection_end": 5}
    assert results["debounce_timers"] == 1 and results["debounce_ms"] == 400
    assert results["debounce_calls"] == [{
        "url": "/sonder-editor/sdk/session/sdk-1/values",
        "method": "PUT",
        "headers": {"X-Sonder-SDK-Session": "s3cret", "X-Sonder-Tab-Session": "tab-1",
                    "Content-Type": "application/json"},
        "body": {"values": {"scene_id": "", "selection_start": 2, "selection_end": 9}, "seq": 1},
    }]
    assert results["noop_timers"] == 0
    assert results["node_id"] == "sdk-1"


def test_a_displaced_tab_holds_its_change_until_it_owns_the_session_again(results):
    assert results["displaced_timers"] == 0
    assert results["ended_can_write"] is False
    assert results["resumed_timers"] == 1
    assert results["resumed_calls"] == 1 and results["resumed_value"] == 4


def test_a_refusal_naming_another_owner_reports_the_loss(results):
    assert results["not_owner_lost"] == ["not_owner"]
    assert results["generate_ended"] == {"ok": False, "code": "session_ended", "lost": ["session_ended"]}


def test_an_offline_save_is_kept_and_sent_later_with_keepalive(results):
    assert results["offline_retry"] == {"attempts": 2, "ok": True, "keepalive": True, "value": 7}


def test_generate_folds_in_the_pending_save_and_clamps_the_count(results):
    gen = results["generate"]
    assert gen["timers_after"] == 0  # the debounced PUT is dropped: Generate saves these values
    assert gen["ok"] is True and gen["method"] == "POST"
    assert gen["url"] == "/sonder-editor/sdk/session/sdk-1/generate"
    assert gen["count"] == 8 and results["generate_min"] == 1
    assert gen["cache"] == -1  # unlimited survives (renderCacheMaxBytes returns -1)
    assert gen["value"] == 6
    assert gen["headers"]["X-Sonder-Tab-Session"] == "tab-1"
    assert results["cancel"] == {"url": "/sonder-editor/sdk/session/sdk-1/cancel", "method": "POST"}
    assert results["generate_network"] == "network"


def test_status_snapshots_are_ordered_by_seq(results):
    order = results["status_order"]
    # ws seq 3 applies; a repeat over ws is dropped; a fetched repeat applies;
    # an older one, another session's and a malformed one are dropped.
    assert order["applied"] == [True, False, True, False, False, False]
    assert order["seen"] == [-1, 3, 3]
    assert order["active"] == ["running"]


def test_session_storage_holds_the_secret_by_id_and_the_exit_flag_once(results):
    storage = results["storage"]
    assert storage["stored"] is True and storage["read"] == "abc"
    assert storage["keys"] == ["sonder-sdk-session:sdk-9"]
    assert storage["after_forget"] == ""
    assert storage["flag"] == {"session_id": "sdk-9", "project": "P", "template_id": "t", "has_at": True}
    assert storage["again"] is None and storage["noSession"] is None
    assert storage["broken"] == [False, "", None]
    assert storage["no_store"] == [False, ""]


def test_generate_button_faces(results):
    assert results["faces"] == {
        "idle": {"generate": "idle", "cancel": "hidden"}, "finished": {"generate": "idle", "cancel": "hidden"},
        "submitting": {"generate": "sending", "cancel": "hidden"},
        "active": {"generate": "idle", "cancel": "active"}, "active_while_sending": {"generate": "sending", "cancel": "active"},
        "canceling": {"generate": "idle", "cancel": "canceling"}, "partly_canceling": {"generate": "idle", "cancel": "active"},
    }
    assert results["clamp"] == [1, 3, 8, 1]


def test_progress_line(results):
    progress = results["progress"]
    assert progress["queued"] == {"message": "Generating 1 of 2 · waiting in ComfyUI's queue", "progress": None, "detail": None}
    assert progress["running"] == {"message": "Generating 2 of 3 · Sampler 2/8", "progress": 0.25, "detail": None}
    assert progress["finishing"] == {"message": "Generating 1 of 1 · saving the take", "progress": None, "detail": None}
    # The line names the workflow of the take it describes (#21).
    assert progress["named"]["detail"] == "LTX Edit"
    assert progress["null_progress"]["progress"] is None
    assert progress["empty"] is None


def test_a_missing_node_refusal_says_so(results):
    assert results["notices"]["missing_nodes"] == {"tier": "error", "message": "This workflow needs nodes that aren't installed.",
                                                  "detail": "This workflow needs nodes that aren't installed: X."}


def test_outcome_toasts_carry_no_take_index(results):
    landed, failed, canceled, ended, running = results["toasts_pure"]
    assert landed == {"tier": "success", "message": "Take landed on the timeline", "source": "sdk-generate-landed"}
    assert failed == {"tier": "error", "message": "Take failed", "detail": "Because.", "source": "sdk-generate-failed"}
    assert canceled["tier"] == "warning" and canceled["message"] == "Take canceled"
    assert ended["tier"] == "warning" and ended["message"] == "Take ended without landing"
    assert running is None
    for toast in (landed, failed, canceled, ended):
        assert not any(ch.isdigit() for ch in toast["message"])


def test_refusal_notices_put_the_sentence_in_message_and_the_reason_in_detail(results):
    notices = results["notices"]
    assert notices["ok"] is None
    assert notices["partial"] == {"tier": "warning", "message": "Only 1 of 3 takes were sent.",
                                  "detail": "ComfyUI did not confirm this run."}
    assert notices["refused"] == {"tier": "error", "message": "This workflow can't generate yet.",
                                  "detail": "“Seed”: bad.\n“Steps”: worse."}
    assert notices["not_owner"] is None and notices["ended"] is None
    assert notices["not_installed"]["message"] == "The Sonder App packages are not installed."
    assert "Settings → Sonder App" in notices["not_installed"]["detail"]
    assert notices["proxy_not_installed"] == notices["not_installed"]
    assert notices["uninstalling"]["tier"] == "warning"
    assert notices["uninstalling"]["message"] == "The Sonder App packages are being removed."
    assert notices["busy"]["tier"] == "warning"
    assert notices["proxy"] == {"tier": "error", "message": "The Sonder App couldn't connect to ComfyUI.", "detail": "No free port."}
    assert notices["network"]["message"] == "Couldn't reach ComfyUI to generate."
    assert notices["submit_failed"] == {"tier": "error", "message": "ComfyUI didn't accept the run.", "detail": "Invalid prompt."}


def test_generate_controls_drive_the_host(results):
    assert results["dom_generate"] == {"order": ["drain"], "generated": [[3, {"renderCacheMaxBytes": 123}]]}
    # The Sonder App's workflow is held from the click and sent as it was then.
    assert results["dom_hold"] == ["hold", "before", ["generate", "A"], "release"]
    assert results["dom_switching_disabled"] is True
    faces = results["dom_faces"]
    assert faces[0] == ["Generate", "primary", "×1"]
    assert faces[1] == "×3"
    assert faces[2] == "×8"
    assert faces[3] == ["Generate", "Cancel all", ""]  # count remains visible
    assert faces[4] == "Canceling…"
    assert faces[5] == ["Generate", ""]
    assert results["dom_cancels"] == 1
    assert results["dom_progress"] == {"tier": "progress", "foreground": True,
                                       "message": "Generating 1 of 2 · waiting in ComfyUI's queue"}
    assert results["dom_progress_after"] == 0
    assert results["dom_toasts"] == [["success", "Take landed on the timeline"], ["warning", "Take canceled"]]
    assert results["dom_listeners_after_destroy"] == 0


def test_a_reopened_session_does_not_repeat_old_outcomes(results):
    assert results["primed_toasts"] == [["error", "Take failed"]]


def test_an_ended_session_ends_the_progress_line(results):
    assert results["ended_progress"] == {"before": 1, "after": 0, "face": "Generate"}
    assert results["ended_status"] == {
        "seen": [{"seq": -1, "runs": 0, "ended": False}, {"seq": 1, "runs": 1, "ended": False},
                 {"seq": 1, "runs": 0, "ended": True}],
        "late": False,
    }


def test_generate_keeps_edits_it_did_not_save(results):
    restore = results["generate_restore"]
    # Refused before the server saved anything: kept, and saved again shortly
    # (not when another tab owns it or the network is down: no retry loop).
    assert restore["not_installed"] == {"ok": False, "unsaved": True, "timers": 1, "lost": [], "posts": 1}
    assert restore["busy"]["unsaved"] is True and restore["busy"]["timers"] == 1
    assert restore["not_owner"] == {"ok": False, "unsaved": True, "timers": 0, "lost": ["not_owner"], "posts": 1}
    assert restore["network"]["unsaved"] is True and restore["network"]["timers"] == 0
    # Saved by the server before it refused the workflow or tried to submit.
    assert restore["refused_workflow"]["unsaved"] is False
    assert restore["unconfirmed"]["unsaved"] is False


def test_a_refused_save_keeps_its_edits_unless_they_were_invalid(results):
    refusals = results["flush_refusals"]
    assert refusals["not_owner"] == {"unsaved": True, "timers": 0, "puts": 1}
    assert refusals["invalid_dropped"] is False and refusals["invalid_timers"] == 0


def test_an_older_save_lands_before_generate(results):
    order = results["inflight_order"]
    assert order["beforeRelease"] == ["send PUT"]
    assert order["after"] == ["send PUT", "put answered", "send POST"]


def test_ownership_displaced_tab_never_claims_on_its_own(results):
    o = results["own_displace"]
    assert o["started"] == {"state": "active", "interval": 10000}
    assert o["orphaned"] == "orphaned"
    assert o["displaced"] == {"state": "displaced", "heartbeat": False}
    # A beat, a release event and a refused write all leave it displaced, unclaimed.
    assert o["passive"] == {"state": "displaced", "counts": {"claims": 0, "beats": 2}}
    assert o["after_take_over"] == {"state": "active", "heartbeat": True, "counts": {"claims": 1, "beats": 2}}
    assert o["log"] == ["active", "orphaned", "displaced", "active", "reclaimed"]


def test_ownership_failed_take_over_stays_displaced(results):
    assert results["own_failed_take_over"] == {
        "afterFailure": {"state": "displaced", "heartbeat": False},
        "retried": "active",
    }


def test_ownership_not_owner_asks_who_owns_it(results):
    o = results["own_not_owner"]
    assert o["quiet"]["state"] == "active"
    assert "displaced" not in o["quiet"]["log"] and "reclaimed" in o["quiet"]["log"]
    assert o["quiet"]["counts"] == {"claims": 1, "beats": 1}
    assert o["other"] == {"state": "displaced", "counts": {"claims": 0, "beats": 1}}


def test_ownership_ends_when_the_server_forgets_the_session(results):
    o = results["own_ended"]
    assert o["ended"] == {"state": "ended", "heartbeat": False}
    assert o["claims_after"] == 2  # Take over may try again; it stays ended on refusal
    assert o["by_refusal"] == "ended"


def test_ownership_survives_a_network_blip(results):
    assert results["own_network"] == {
        "down": {"state": "reconnecting", "heartbeat": True, "canWrite": True},
        "back": "active",
    }


def test_ownership_follows_session_changed(results):
    assert results["own_events"] == {
        "orphaned": "orphaned",
        "reclaimed": {"state": "active", "claims": 1},
        "displaced": "displaced",
        "stopped_heartbeat": False,
    }


def test_the_launcher_module_does_nothing_outside_its_page(results):
    """ComfyUI imports every script under web/ on its canvas page."""
    assert results["landing_inert"] == {"touched": [], "marker": [True, False, False]}


def test_queue_rows_show_why_a_job_waits_or_failed(results):
    assert results["queue_error"] == ["Returned: the run was interrupted.", "Sampler error", "", "", ""]


def test_open_source_workflow_needs_a_handler_and_a_workflow(results):
    assert results["gallery_gate"] == {
        "canvas_mp4": False,
        "no_flag_handler": False,  # the dormant card passes no check: unchanged
        "tab_mp4": True,
        "no_handler": True,
        "not_embedded": True,
        "webm_unknown": True,
        "webm_embedded": False,
    }


def test_generation_status_reaches_its_handler(results):
    assert results["dispatch"] == [4, "rf"]


def test_project_creation_is_shared_with_create_new(results):
    create = results["create"]
    assert create["post"]["method"] == "POST" and create["post"]["url"] == "/sonder-editor/project"
    body = create["post"]["body"]
    assert body["name"] == "Shot A"
    assert (body["fps"], body["width"], body["height"]) == (24, 960, 544)
    assert body["template_id"] == "free"
    assert "prompt_channel_template" in body and "frame_constraint" in body
    assert create["list"] == {"url": "/sonder-editor/projects", "method": "GET", "body": None}
    assert create["scene"] == {"url": "/sonder-editor/project/Shot-A/scenes", "method": "POST",
                               "body": {"name": "Scene 1", "duration_frames": 241}}
    assert create["folder"] == "Shot-A"
    assert create["existing_calls"] == ["POST /sonder-editor/project", "GET /sonder-editor/projects"]
    assert create["existing_folder"] == "Shot-A"
    assert create["missingName"] == "Project name is required"
    assert create["serverError"] == "Invalid prompt channel template"
    assert create["folder_name"] == ["b c", "", ""]
    # The project exists when its first scene fails: the error carries the list.
    assert create["sceneFailure"] == {"message": "disk full", "folder": "y", "listed": 1}


def test_the_canvas_create_new_uses_the_shared_helper():
    """Pinned call site: the Launcher and the node card must create projects one way."""
    extension = (JS / "extension.js").read_text(encoding="utf-8")
    body = extension.split("async function createProjectFromNode(", 1)[1].split("\nfunction ", 1)[0]
    assert "createSonderProject(" in body
    assert 'fetch(api.apiURL("/sonder-editor/project")' not in body
    landing = (JS / "sdk_landing.js").read_text(encoding="utf-8")
    assert "createSonderProject(" in landing


def test_every_save_is_drained_and_sequence_protects_keepalive(results):
    assert results["all_saves"] == {"before": ["PUT", "PUT"], "exitWaited": True,
        "sequences": [1, 2, 3, 4], "keepalive": True, "unsaved": False}
    assert results["drain_timeout"] is False
    source = (ROOT / "web/js/tab_entry.js").read_text(encoding="utf-8")
    assert "await host.drainSaves()" in source


def test_failure_details_survive_newer_snapshots_and_fetch_once(results):
    assert results["old_details"] == {"older": False, "seq": 9, "detail": "ValueError: detail"}
    assert results["details_after_ws"] == "ValueError: detail"
    assert results["failure_detail"] == {"reads": 1, "detail": "The run failed.\n\nValueError: private detail"}


def test_generate_and_cancel_are_independent_with_capacity_feedback(results):
    assert results["side_by_side"]["sending"] == {"disabled": True, "label": "Sending…", "cancel": "Cancel all", "count": "×1"}
    assert results["side_by_side"]["cancels"] == 1
    assert results["side_by_side"]["capacityDisabled"] is True
    assert "16" in results["side_by_side"]["reason"]
    assert results["trimmed_line"] == "Generating 50 of 50"
    assert results["canceled_saved"] is True
    assert results["canceled_notice"] == {"tier": "info", "message": "Canceled before it was sent."}


def test_cancel_invalidates_generate_during_each_client_barrier(results):
    assert results["cancel_during_host_drain"] == {"code": "canceled_before_submit", "methods": ["POST", "PUT"], "generated": False, "saved": 42}
    assert results["cancel_before_host"] == {"methods": ["POST", "PUT"], "generated": False, "saved": 43}


def test_a_branch_run_reads_as_an_output_never_a_take(results):
    progress = results["branch_progress"]
    assert progress["finishing"]["message"] == "Running output 1 of 1 · finishing"
    # A completed branch counts as done.
    assert progress["done"]["message"] == "Running output 2 of 2 · waiting in ComfyUI's queue"
    completed, landed, failed, canceled, ended = results["branch_toasts"]
    assert completed == {"tier": "success", "message": "Output ready", "source": "sdk-branch-completed"}
    assert landed["message"] == "Take landed on the timeline"  # a branch that saved a take
    assert failed == {"tier": "error", "message": "Output failed", "detail": "Why.", "source": "sdk-branch-failed"}
    assert canceled["message"] == "Output canceled" and ended["message"] == "Output run ended"
