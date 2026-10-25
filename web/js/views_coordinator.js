// Output Views state for one editor page: the pane layout (browser-local, per
// workflow), what each pane shows, the reads of observed run outputs, holds,
// each pane's own run, and session loss. No DOM: EditorWidget owns this object
// and its lifetime, view_panes.js draws it.
//
// Reads: one readOutputs for every visible View pane (at most four sources),
// on a pane change, an outputs_changed hint (coalesced per animation frame),
// the page becoming visible, and a pane's own run. A 5 s poll runs only while
// a pane waits for a run; a 10-minute keepalive read covers idle holds. Reads
// are latest-wins and pause while the page is hidden or no View pane shows.
//
// Holds pin a result for comparison. A read names this page's holds
// (`keep_holds`), so a closed page's holds age out on the server; pagehide
// releases them at once. A hold ends on Release, a source, workflow or project
// change, its pane closing or the layout shrinking past it, fullscreen exit and
// teardown.
//
// Linked playback (Phase 3B): a video or audio result whose frames sit on the
// current scene (a take's placement, or the View's authored timing against its
// Editor's executed window) follows the editor clock; `paneModel` resolves the
// link (view_timing.js) and whether the scene changed in its range since
// ("Previous context", compared against the run's recorded digest).
import {
    MAX_PANES, assignSource, cancelAvailability, isDefaultLayout, mediaStatus, normalizeLayout, paneCount, refusalText,
    runAvailability, sameSource, viewPaneStatus,
} from "./views_model.js";
import { TIMELINE } from "./audio_ownership.js";
import { mintId } from "./controls_manage.js";
import { contentStatus, isLinkable, resolveLink } from "./view_timing.js";
import { fileKey } from "./output_media_cache.js";

export const POLL_MS = 5000;
export const KEEPALIVE_MS = 10 * 60 * 1000;
// A run ComfyUI never confirmed stops waiting after this.
export const UNCONFIRMED_MS = 60000;
// While the editor plays, Previous context is checked at most this often.
export const PREVIOUS_CONTEXT_PLAYING_MS = 500;
const HOLD_LIMIT_REASON = "Up to 8 results can be held at once, shared with this workflow's other editor tab. Release one first.";

// `scene` is the editor's own scene for linking: {projectId(), sceneId(), fps(),
// size() -> {width, height}, scene(), playing(), assetForSourcePath(path), assetsLoaded()}.
export function createViewsCoordinator({ host, controls, layoutStore, audio, cache, scene = null, onChange = () => {}, notify = () => {},
    now = () => Date.now(), setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = id => clearTimeout(id),
    raf = fn => requestAnimationFrame(fn), documentRef = globalThis.document } = {}) {
    let destroyed = false;
    let visible = false;
    let key = "";
    let layout = normalizeLayout(null);
    const panes = Array.from({ length: MAX_PANES }, () => ({ held: null, outcome: null, condition: null, followerState: null, previous: null, released: false, creating: false,
        duration: null }));
    let answer = null; // the latest readOutputs answer
    let readToken = 0;
    let reading = null;
    let readAgain = false;
    let pollTimer = null;
    let keepaliveTimer = null;
    let hintFrame = null;
    let pickerOpen = false;
    let demand = null;
    let sessionLost = false;
    let capability = null;
    let unsubscribeHint = null;
    let hintClient = null;
    const cleanups = [];
    const changed = () => { if (!destroyed) onChange(); };

    const current = () => host();
    const client = () => current()?.runOutputs?.() || null;
    const viewList = () => controls.getView()?.views || [];
    const viewFor = id => viewList().find(view => view.view_id === id) || null;
    const visibleCount = () => paneCount(layout.layout);
    const viewPanes = () => layout.panes.slice(0, visibleCount())
        .map((pane, index) => ({ pane, index })).filter(({ pane }) => pane.source.kind === "view");

    // ── Layout ───────────────────────────────────────────────────────────────

    function persist() {
        if (!key) return; // no workflow id: nothing is remembered
        layout = { ...layout, ts: now() };
        try { layoutStore.write(key, isDefaultLayout(layout) ? null : layout); } catch (error) { console.warn("[Sonder] Views layout save failed:", error); }
    }
    function adoptKey() {
        const next = current()?.workflowKey?.() || "";
        if (next === key) return false;
        releaseAll();
        key = next;
        layout = normalizeLayout(key ? layoutStore.read(key) : null);
        answer = null;
        for (const pane of panes) Object.assign(pane, { held: null, outcome: null, condition: null, followerState: null, previous: null, released: false, creating: false });
        return true;
    }

    // ── Demand and reads ─────────────────────────────────────────────────────

    function syncDemand() {
        const want = visible && !sessionLost && (pickerOpen || viewPanes().length > 0);
        if (want === demand) return;
        demand = want;
        current()?.setViewsDemand?.(want);
    }
    function sources() {
        const paths = new Set();
        // A held pane's source is read too: on Release the newest result is
        // already here, and a file still shown keeps playing (no remount).
        for (const { pane } of viewPanes()) {
            const view = viewFor(pane.source.viewId);
            if (view?.path && !["missing", "duplicate", "kind", "unsupported"].includes(view.status)) paths.add(view.path);
        }
        return [...paths].sort();
    }
    const heldIds = () => panes.map(pane => pane.held?.holdId).filter(Boolean);
    // A hidden page keeps its holds alive (the keepalive read), nothing else.
    const canKeepAlive = () => visible && !destroyed && !sessionLost && !!client();
    const canRead = () => canKeepAlive() && !documentRef?.hidden;
    function scheduleRead({ keepalive = false } = {}) {
        if (!(keepalive ? canKeepAlive() : canRead()) || (!viewPanes().length && !heldIds().length)) { schedulePolls(); return; }
        if (reading) { readAgain = true; return; }
        reading = read().finally(() => {
            reading = null;
            if (readAgain && !destroyed) { readAgain = false; scheduleRead(); }
        });
    }
    async function read() {
        const reader = client();
        const wanted = sources();
        const token = ++readToken;
        const forKey = key;
        if (!reader || (!wanted.length && !heldIds().length)) { schedulePolls(); return; }
        // This read refreshes the holds it names: the next keepalive counts from now.
        clearTimer(keepaliveTimer);
        keepaliveTimer = null;
        let result;
        try {
            result = await reader.readOutputs(wanted, { keepHolds: heldIds() });
        } catch (_error) {
            result = null;
        }
        if (destroyed || token !== readToken || forKey !== key) return;
        if (result?.ok) {
            answer = result;
            settleOutcomes();
            reconcileHolds(token);
            await settleUnlisted(reader, forKey);
            if (destroyed || forKey !== key) return;
        }
        schedulePolls();
        changed();
    }
    // A pane's run past the newest runs a read lists (a long queue behind it):
    // asked about on its own, so it never reads Running forever.
    async function settleUnlisted(reader, forKey) {
        const listed = new Set((answer?.runs || []).map(run => run.prompt_id));
        await Promise.all(panes.map(async pane => {
            const outcome = pane.outcome;
            if (!outcome || outcome.settled || !outcome.promptId || listed.has(outcome.promptId) || !reader.readRun) return;
            let run = null;
            try { run = await reader.readRun(outcome.promptId); } catch (_error) { return; }
            if (destroyed || forKey !== key || pane.outcome !== outcome) return;
            if (run?.ok && run.finished) pane.outcome = { ...outcome, settled: true, removed: run.outcome === "removed" };
            else if (run?.ok === false && now() - outcome.at > UNCONFIRMED_MS) pane.outcome = { ...outcome, settled: true };
        }));
    }
    // A hold the server no longer has (restart, aged out) reads Released. Only
    // a read sent after the hold was made can say so.
    function reconcileHolds(token) {
        const live = new Map((answer?.holds || []).map(hold => [hold.hold_id, hold]));
        panes.forEach(pane => {
            if (!pane.held?.holdId || pane.held.since >= token) return;
            const hold = live.get(pane.held.holdId);
            if (!hold) {
                pane.held = null;
                pane.released = true;
            } else if (hold.state !== "ok") {
                cache?.forget?.(hold.entry);
            }
        });
    }
    function waiting() {
        if (panes.some(pane => pane.outcome && !pane.outcome.settled)) return true;
        return Object.values(answer?.sources || {}).some(item => item.state === "pending");
    }
    function schedulePolls() {
        clearTimer(pollTimer);
        pollTimer = null;
        if (canRead() && waiting()) pollTimer = setTimer(() => { pollTimer = null; scheduleRead(); }, POLL_MS);
        // Armed once and kept: a hint or poll that does not read (a hidden
        // page) must not push it back, or holds would age out on the server.
        if (!canKeepAlive() || !heldIds().length) {
            clearTimer(keepaliveTimer);
            keepaliveTimer = null;
        } else if (keepaliveTimer === null) {
            keepaliveTimer = setTimer(() => { keepaliveTimer = null; scheduleRead({ keepalive: true }); }, KEEPALIVE_MS);
        }
    }
    function watchHints() {
        const reader = client();
        if (reader === hintClient) return;
        unsubscribeHint?.();
        hintClient = reader;
        unsubscribeHint = reader?.onOutputsChanged?.(() => {
            if (hintFrame !== null) return;
            hintFrame = true; // set first: a frame callback may run at once
            raf(() => { hintFrame = null; scheduleRead(); });
        }) || null;
    }

    // ── Runs ─────────────────────────────────────────────────────────────────

    function settleOutcomes() {
        const runs = new Map((answer?.runs || []).map(run => [run.prompt_id, run]));
        panes.forEach(pane => {
            let outcome = pane.outcome;
            if (!outcome || outcome.settled) return;
            outcome = current()?.resolve?.(outcome) || outcome;
            const run = outcome.promptId ? runs.get(outcome.promptId) : null;
            if (run?.finished) {
                pane.outcome = { ...outcome, settled: true, removed: run.outcome === "removed" };
            } else if (outcome.finalState) {
                // A Sonder App run that ended before it was observed (canceled while queued).
                pane.outcome = { ...outcome, settled: true, removed: !run && outcome.finalState === "canceled" };
            } else if (!outcome.promptId && !outcome.runId && now() - outcome.at > UNCONFIRMED_MS) {
                pane.outcome = { ...outcome, settled: true };
            } else {
                pane.outcome = outcome;
            }
        });
    }

    async function run(index) {
        const pane = layout.panes[index];
        const view = pane?.source.kind === "view" ? viewFor(pane.source.viewId) : null;
        const adapter = current();
        if (!view?.path || !adapter) return;
        const at = now();
        panes[index].outcome = { state: "starting", settled: false, at };
        changed();
        const outcome = await adapter.runBranch(view.path);
        if (destroyed || panes[index].outcome?.at !== at) return;
        if (outcome.state === "queued" || outcome.state === "unconfirmed" || outcome.state === "pending") {
            panes[index].outcome = { ...outcome, settled: false, at };
        } else {
            panes[index].outcome = { ...outcome, settled: true, at,
                message: outcome.state === "busy" ? "Another output is waiting for ComfyUI" : refusalText(outcome) };
        }
        changed();
        scheduleRead();
    }

    async function cancel(index) {
        const outcome = panes[index].outcome;
        const adapter = current();
        if (!outcome || outcome.settled || !adapter) return;
        panes[index].outcome = { ...outcome, canceling: true };
        changed();
        const resolved = adapter.resolve?.(outcome) || outcome;
        const result = await adapter.cancel(resolved);
        if (destroyed) return;
        if (!result.ok && panes[index].outcome?.at === outcome.at) {
            panes[index].outcome = { ...panes[index].outcome, canceling: false };
            notify("The run could not be canceled.");
        }
        changed();
        scheduleRead();
    }

    // ── Holds ────────────────────────────────────────────────────────────────

    async function hold(index) {
        const model = paneModel(index);
        const reader = client();
        const result = model.result;
        if (!reader || !result || model.held || !model.entry) return;
        const view = model.view;
        const promptId = result.prompt_id, entryIndex = model.entry.index ?? model.itemIndex;
        const pending = { pending: true };
        panes[index].held = pending;
        changed();
        let answerHold;
        try {
            answerHold = await reader.hold({ promptId, source: view.path, index: entryIndex });
        } catch (_error) {
            answerHold = { ok: false, code: "unreachable" };
        }
        if (destroyed || panes[index].held !== pending) {
            // The pane moved on (released, another source, closed) before the
            // server answered: the hold it made is nobody's, so it goes now.
            if (answerHold?.ok && answerHold.hold_id) void reader.release(answerHold.hold_id)?.catch?.(() => {});
            return;
        }
        if (answerHold.ok && answerHold.hold_id) {
            panes[index].held = { holdId: answerHold.hold_id, promptId, source: view.path, index: entryIndex, since: readToken };
            panes[index].released = false;
        } else {
            panes[index].held = null;
            notify(answerHold.code === "too_many" ? HOLD_LIMIT_REASON : "This result can't be held. It may have been removed.");
        }
        changed();
        scheduleRead();
    }
    function releasePane(index, { quiet = false } = {}) {
        const held = panes[index].held;
        panes[index].held = null;
        if (held?.holdId) void client()?.release(held.holdId).catch?.(() => {});
        if (!quiet) { changed(); scheduleRead(); }
    }
    function releaseAll({ keepalive = false } = {}) {
        const ids = heldIds();
        for (const pane of panes) pane.held = null;
        if (ids.length) void client()?.releaseAll?.(ids, { keepalive })?.catch?.(() => {});
    }

    // ── Pane model ───────────────────────────────────────────────────────────

    function heldRecord(index) {
        const id = panes[index].held?.holdId;
        return id ? (answer?.holds || []).find(hold => hold.hold_id === id) || null : null;
    }

    function paneModel(index) {
        const pane = layout.panes[index];
        const state = panes[index];
        const model = { index, pane, source: pane.source, held: !!state.held, holdPending: !!state.held?.pending,
            outcome: state.outcome, released: state.released };
        if (pane.source.kind !== "view") return model;
        const view = viewFor(pane.source.viewId);
        if (!view && state.creating) return { ...model, view: null, message: "Adding this output…", chip: null, entries: [], entry: null,
            runnable: { show: false }, cancellable: { show: false }, canHold: false };
        const item = view?.path ? answer?.sources?.[view.path] || null : null;
        const status = viewPaneStatus({ view, read: item, run: state.outcome, hold: heldRecord(index), held: !!state.held && !state.held.pending,
            // A Relay error leaves no Views: the pane says why, not "no longer in the workflow".
            sessionLost, viewsError: controls.getView()?.views_error || controls.getView()?.error || null });
        const entries = status.result?.entries || [];
        const itemIndex = Math.min(pane.item, Math.max(0, entries.length - 1));
        const entry = entries[status.result?.fixedIndex ? 0 : itemIndex] || null;
        const media = state.condition ? mediaStatus(state.condition, { takeSaved: !!status.result?.run?.saved_to_project }) : null;
        let chip = media?.chip || status.chip;
        if (state.outcome?.canceling && !state.outcome.settled) chip = "Canceling…";
        else if (state.outcome?.settled && state.outcome.message) chip = `Didn't run: ${state.outcome.message}`;
        else if (state.outcome && !state.outcome.settled && !chip) chip = "Running";
        if (state.released && !state.held) chip = chip || "Released";
        const adapter = current();
        const busy = adapter?.mode === "canvas" && panes.some((other, i) => i !== index && other.outcome?.state === "starting");
        const link = linkFor(index, view, status.result, entry);
        const linked = !!link?.linked;
        const catching = linked && state.followerState === "catching_up";
        const outside = linked && state.followerState === "outside";
        let linkChip = null;
        if (linked && link.previous === "changed") linkChip = "Previous context";
        else if (link && !linked && link.reason !== "off" && link.text) linkChip = `Unlinked: ${link.text}`;
        if (!media?.chip) {
            if (catching) chip = "Catching up";
            else if (linkChip) chip = chip ? `${linkChip} · ${chip}` : linkChip;
        }
        return {
            ...model, view, read: item, status, entries, itemIndex, entry, chip,
            linkable: !!entry && isLinkable(entry), link, linked, outside,
            message: media?.message || status.message || (outside ? "Outside generated range" : linked && link.pending ? "Linking…" : null),
            unavailable: !!status.unavailable,
            result: status.result, runnable: runAvailability({ view, capability, outcome: state.outcome, busy, sessionLost }),
            cancellable: cancelAvailability(state.outcome && !state.outcome.canceling ? state.outcome : null),
            canHold: !state.held && !!entry && !!status.result?.prompt_id && !sessionLost,
            audible: audio?.owner() === paneId(index),
        };
    }

    const paneId = index => `pane:${index}`;

    // The pane's link: {linked: true, map, audioMap, Fm, sig, previous, ...}
    // | {linked: false, reason, text} | null (not linkable, or no editor scene).
    function linkFor(index, view, result, entry) {
        if (!entry || !isLinkable(entry) || !scene) return null;
        if (!layout.panes[index]?.link) return { linked: false, reason: "off", text: "" };
        const state = panes[index];
        const key = fileKey(entry);
        const known = state.duration?.key === key;
        const resolved = resolveLink({
            entry, timing: view?.timing || null, contexts: result?.editor_context || null, fresh: result?.fresh,
            editor: { projectId: scene.projectId(), sceneId: scene.sceneId(), fps: scene.fps(), editorKey: current()?.editorKey?.() || "" },
            durationSec: known ? state.duration.sec : null,
        });
        if (!resolved.linked) return resolved;
        // Until the file's metadata arrives its length is unchecked: "Linking…".
        // A known non-finite duration links without the check.
        resolved.pending = !known;
        resolved.sig = [key, resolved.kind, resolved.S0, resolved.S1, resolved.Fm, resolved.required,
            entry.scene_map?.source_offset ?? view?.timing?.source_offset ?? 0, view?.timing?.anchor || ""].join("|");
        resolved.previous = previousFor(index, resolved.context);
        return resolved;
    }

    // "same" | "changed" | "unknown", recomputed on every paused render and at
    // most every PREVIOUS_CONTEXT_PLAYING_MS while playing (single slot per pane).
    function previousFor(index, context) {
        const content = context?.content;
        if (!content) return "unknown";
        const sceneObj = scene.scene?.() || null;
        const cached = panes[index].previous;
        const t = now();
        if (cached && cached.digest === content.digest && cached.scene === sceneObj && scene.playing?.()
            && t - cached.at < PREVIOUS_CONTEXT_PLAYING_MS) return cached.value;
        const size = scene.size?.() || {};
        let value = "unknown";
        try {
            value = contentStatus(sceneObj, content, {
                fps: scene.fps(), width: size.width, height: size.height,
                assetForSourcePath: path => scene.assetForSourcePath?.(path) || null, assetsLoaded: !!scene.assetsLoaded?.(),
            });
        } catch (error) {
            console.warn("[Sonder] Previous context check failed:", error);
        }
        panes[index].previous = { digest: content.digest, scene: sceneObj, at: t, value };
        return value;
    }

    // Whether this ComfyUI runs one output alone. A failed ask (ComfyUI
    // restarting) is asked again on session restore and on the page showing.
    let asking = false;
    function askCapability() {
        if (asking || capability?.ok || destroyed) return;
        asking = true;
        void Promise.resolve(current()?.capability?.())
            .then(result => { capability = result || { ok: false }; }, () => { capability = { ok: false }; })
            .finally(() => { asking = false; changed(); });
    }

    // ── Public ───────────────────────────────────────────────────────────────

    function setLayout(next) {
        const before = visibleCount();
        layout = normalizeLayout({ ...layout, layout: next });
        // Panes past the new layout close: their holds and sound end.
        for (let i = visibleCount(); i < Math.max(before, MAX_PANES); i++) {
            if (panes[i].held) releasePane(i, { quiet: true });
            audio?.release(paneId(i));
            panes[i].condition = null;
            panes[i].followerState = null;
        }
        persist();
        syncDemand();
        changed();
        scheduleRead();
    }

    function assign(index, source) {
        const before = layout.panes.map(pane => pane.source);
        layout = assignSource(layout, index, source);
        layout.panes.forEach((pane, i) => {
            if (sameSource(pane.source, before[i])) return;
            if (panes[i].held) releasePane(i, { quiet: true });
            audio?.release(paneId(i));
            Object.assign(panes[i], { outcome: null, condition: null, followerState: null, previous: null, released: false });
        });
        persist();
        syncDemand();
        changed();
        scheduleRead();
    }

    function setPaneOption(index, patch) {
        const pane = layout.panes[index];
        if (!pane) return;
        const next = normalizeLayout({ ...layout, panes: layout.panes.map((p, i) => (i === index ? { ...pane, ...patch } : p)) });
        // Unchanged (a media mount reporting its zoom, say): no settings write.
        if (JSON.stringify(next.panes[index]) === JSON.stringify(pane)) return;
        layout = next;
        if ("item" in patch) Object.assign(panes[index], { condition: null, followerState: null, previous: null });
        if ("link" in patch) panes[index].followerState = null;
        persist();
        changed();
    }

    // Pick an output node as a new View in pane `index`: one Controls write
    // (one native Undo step on the canvas), then the pane shows it.
    async function createView(index, candidate) {
        const id = mintId();
        const write = controls.applyChanges?.({ [`view:${id}:create`]: { candidate: { path: candidate.path } } });
        if (!write) return false;
        // A refused write puts back what the pane showed (the Composite, say).
        const previous = layout.panes[index]?.source || null;
        assign(index, { kind: "view", viewId: id });
        panes[index].creating = true;
        changed();
        let result;
        try { result = await write; } catch (error) { result = { ok: false, rejected: [{ reason: error?.message }] }; }
        if (destroyed) return false;
        panes[index].creating = false;
        if (!result?.ok) {
            notify(result?.rejected?.[0]?.reason || "The output could not be added as a View.");
            if (layout.panes[index]?.source.viewId === id) assign(index, previous || { kind: "guides" });
            return false;
        }
        void controls.refresh?.(true);
        changed();
        return true;
    }

    return {
        layout: () => layout,
        createView,
        workflowKey: () => key,
        paneModel,
        paneId,
        visibleCount,
        views: viewList,
        controlsView: () => controls.getView(),
        capability: () => capability,
        sessionLost: () => sessionLost,
        setLayout,
        assign,
        setPaneOption,
        setMediaCondition(index, condition) {
            if (panes[index].condition === condition) return;
            panes[index].condition = condition;
            changed();
        },
        // The shown file's duration (the link's length check needs it).
        setMediaDuration(index, key, sec) {
            const value = Number.isFinite(sec) && sec > 0 ? sec : null;
            const previous = panes[index].duration;
            if (previous?.key === key && previous.sec === value) return;
            panes[index].duration = { key, sec: value };
            changed();
        },
        // A linked pane's follower: catching_up | outside | following... or null.
        setFollowerState(index, state) {
            const value = state || null;
            if (panes[index].followerState === value) return;
            panes[index].followerState = value;
            changed();
        },
        run,
        cancel,
        hold,
        release: index => releasePane(index),
        setPickerOpen(open) {
            pickerOpen = !!open;
            syncDemand();
        },
        // Fullscreen opened or closed (the panes mount only while it shows).
        setVisible(next) {
            visible = !!next;
            if (!visible) {
                releaseAll();
                for (let i = 0; i < MAX_PANES; i++) audio?.release(paneId(i));
            }
            syncDemand();
            if (visible) { watchHints(); scheduleRead(); } else schedulePolls();
            changed();
        },
        // The host changed under the panes (a project switch, a Sonder App workflow switch).
        hostChanged() {
            if (adoptKey()) for (let i = 0; i < MAX_PANES; i++) audio?.release(paneId(i));
            demand = null; // a new Controls host has not heard it yet
            watchHints();
            syncDemand();
            changed();
            scheduleRead();
        },
        // The other editor tab on this workflow saved a newer layout (browser
        // storage sync): the last write wins, as for other layout settings.
        storedLayoutChanged() {
            if (!key) return;
            const stored = layoutStore.read(key);
            const next = normalizeLayout(stored);
            if (!stored || !(next.ts > layout.ts)) return;
            const before = layout.panes.map(pane => pane.source);
            const count = visibleCount();
            layout = next;
            layout.panes.forEach((pane, i) => {
                if (sameSource(pane.source, before[i]) && i < visibleCount()) return;
                if (panes[i].held) releasePane(i, { quiet: true });
                audio?.release(paneId(i));
                Object.assign(panes[i], { outcome: null, condition: null, followerState: null, previous: null, released: false, creating: false });
            });
            if (count !== visibleCount() || layout.panes.some((pane, i) => !sameSource(pane.source, before[i]))) {
                syncDemand();
                changed();
                scheduleRead();
            } else changed();
        },
        // Another project: its holds end and its results go; the layout stays.
        projectChanged() {
            releaseAll();
            answer = null;
            for (const pane of panes) Object.assign(pane, { outcome: null, condition: null, followerState: null, previous: null, released: false, creating: false });
            for (let i = 0; i < MAX_PANES; i++) audio?.release(paneId(i));
            watchHints();
            changed();
            scheduleRead();
        },
        refresh: scheduleRead,
        start() {
            adoptKey();
            const adapter = current();
            askCapability();
            cleanups.push(adapter?.onSessionLost?.(() => { sessionLost = true; syncDemand(); schedulePolls(); changed(); }) || (() => {}));
            cleanups.push(adapter?.onSessionRestored?.(() => { sessionLost = false; askCapability(); syncDemand(); scheduleRead(); changed(); }) || (() => {}));
            cleanups.push(controls.subscribe?.(() => { syncDemand(); changed(); scheduleRead(); }) || (() => {}));
            const onVisibility = () => { if (!documentRef.hidden) { askCapability(); scheduleRead(); } };
            documentRef?.addEventListener?.("visibilitychange", onVisibility);
            cleanups.push(() => documentRef?.removeEventListener?.("visibilitychange", onVisibility));
            const onPageHide = () => releaseAll({ keepalive: true });
            globalThis.window?.addEventListener?.("pagehide", onPageHide);
            cleanups.push(() => globalThis.window?.removeEventListener?.("pagehide", onPageHide));
            watchHints();
        },
        audibleOwner: () => audio?.owner() || TIMELINE,
        destroy() {
            if (destroyed) return;
            releaseAll();
            destroyed = true;
            for (const fn of cleanups.splice(0)) { try { fn(); } catch (_error) { /* torn down */ } }
            unsubscribeHint?.();
            clearTimer(pollTimer);
            clearTimer(keepaliveTimer);
            current()?.setViewsDemand?.(false);
        },
    };
}
