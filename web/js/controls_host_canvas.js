// Canvas graph authority for the shared Controls panel. No app/DOM at import.
import {
    MAX_MANIFEST_BYTES, editManifest, exportManifest, nodesOf, parseManifest, relayCensus, resolveBindings,
} from "./control_bindings.js";

const DESCRIBE_URL = "/sonder-editor/controls/describe";
const DEBOUNCE_MS = 400;
const refused = (changes, reason) => ({ok: false,
    rejected: Object.keys(changes || {}).map(target => ({target, reason}))});

async function promptHash(text) {
    if (globalThis.crypto?.subtle) {
        const bytes = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
        return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join("");
    }
    // HTTP LAN origins lack Web Crypto. This bounded fingerprint only detects
    // stale views; registry ownership and fresh graph comparison gate writes.
    let a = 0x811c9dc5, b = 0x9e3779b9;
    for (let i = 0; i < text.length; i++) {
        const c = text.charCodeAt(i);
        a = Math.imul(a ^ c, 0x01000193);
        b = Math.imul(b ^ c, 0x85ebca6b);
    }
    return `local:${(a >>> 0).toString(16).padStart(8, "0")}${(b >>> 0).toString(16).padStart(8, "0")}`;
}

export function createCanvasControlsHost({app, api, getEditorNode, setEditorSetting = () => {},
    getChangeTracker = () => app?.extensionManager?.workflow?.activeWorkflow?.changeTracker,
    debounceMs = DEBOUNCE_MS} = {}) {
    if (!app || typeof app.graphToPrompt !== "function" || typeof getEditorNode !== "function") {
        throw new TypeError("Canvas Controls needs an injected app and editor node getter.");
    }
    let view = {state: "idle", bindings: [], notes: [], error: null};
    let visible = false, mountedVisible = false, destroyed = false, listening = false;
    let epoch = 0, timer = null, active = null, trailing = false, lastHash = null;
    let writeTail = Promise.resolve();
    const subscribers = new Set();
    const rootGraph = () => app.rootGraph || app.graph;
    const needed = () => visible || mountedVisible;
    const notify = () => { for (const cb of subscribers) cb(view); };
    const identity = () => ({root: rootGraph(), editor: getEditorNode()});
    const sameGraph = id => !destroyed && id.root === rootGraph() && id.editor === getEditorNode() && !app.configuringGraph;
    // Visibility and graph events retire passive reads (epoch) but never a write
    // in flight: its fresh-export comparisons already refuse a changed workflow,
    // and hiding the panel right after an edit must not discard it.
    const isCurrent = (id, stamp) => epoch === stamp && sameGraph(id);
    const editorKey = (id, prompt) => nodesOf(id.root).includes(id.editor)
        && prompt[String(id.editor?.id)]?.class_type === "SonderEditor" ? String(id.editor.id) : null;
    const snapshot = async id => {
        const {output} = await app.graphToPrompt();
        if (!output || typeof output !== "object") throw new Error("The workflow could not be exported.");
        const key = editorKey(id, output);
        return {prompt: output, editor_key: key, hash: JSON.stringify([key, output])};
    };
    const describe = async (shot, changes) => {
        const body = {prompt: shot.prompt, relay_census: relayCensus(rootGraph()).census};
        if (changes !== undefined) body.changes = changes;
        const response = await api.fetchApi(DESCRIBE_URL, {method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        if (!response.ok) throw new Error("The workflow controls could not be read.");
        return response.json();
    };
    const adopt = raw => {
        // Canvas seeds follow the native mode; the export mirrored it.
        const bindings = (raw.bindings || []).map(d => d.kind === "SEED" ? {...d, seed_mode: d.seed_mode || "fixed"} : d);
        view = {...raw, bindings, state: "ready", error: raw.error || null};
        notify();
    };

    async function refresh(force = false) {
        if (destroyed || (!needed() && !force)) return view;
        if (active) {
            if (!force) { trailing = true; return active; }
            await active;
            return refresh(true);
        }
        const id = identity(), stamp = epoch;
        active = (async () => {
            try {
                const shot = await snapshot(id);
                if (!isCurrent(id, stamp) || (!needed() && !force)) return view;
                if (!force && shot.hash === lastHash) return view;
                const raw = await describe(shot);
                if (!isCurrent(id, stamp) || (!needed() && !force)) return view;
                // Graph edits that miss the event still cannot land an old view.
                if ((await snapshot(id)).hash !== shot.hash || !isCurrent(id, stamp)) return view;
                lastHash = shot.hash;
                adopt(raw);
            } catch (error) {
                if (isCurrent(id, stamp)) { view = {...view, state: "error", error: error.message}; notify(); }
            }
            return view;
        })();
        try { return await active; }
        finally {
            active = null;
            if (trailing && !destroyed && needed()) { trailing = false; schedule(); }
        }
    }
    function schedule() {
        if (destroyed || !needed()) return;
        clearTimeout(timer);
        timer = setTimeout(() => { timer = null; void refresh(); }, debounceMs);
    }
    const graphChanged = () => { epoch++; schedule(); };
    function syncListeners() {
        const next = needed() && !destroyed;
        if (next === listening) return;
        listening = next;
        for (const type of ["graphChanged", "promptQueued"]) {
            if (next) api.addEventListener?.(type, graphChanged);
            else api.removeEventListener?.(type, graphChanged);
        }
    }
    function visibility(which, value) {
        const was = needed();
        if (which === "local") visible = !!value; else mountedVisible = !!value;
        syncListeners();
        if (!needed()) { epoch++; clearTimeout(timer); timer = null; trailing = false; }
        else if (!was) { lastHash = null; void refresh(true); }
    }

    // Native widget writes for value targets, and one merged manifest write for
    // every presentation target in the batch (one widget, one history step).
    function operations(raw, changes, root) {
        const {active} = relayCensus(root);
        if (active.length !== 1) throw new Error("Keep exactly one active Sonder Control Relay in the workflow.");
        const relay = active[0];
        const manifestWidget = (relay.widgets || []).find(w => w?.name === "manifest");
        if (!manifestWidget) throw new Error("The Sonder Control Relay has no manifest.");
        const saved = typeof manifestWidget.value === "string" ? manifestWidget.value : "";
        const {bindings} = resolveBindings(root, parseManifest(saved));
        const byWidget = new Map(), presentation = [];
        const add = (node, widget, value, target) => {
            const existing = byWidget.get(widget);
            if (existing && existing.value !== value) throw new Error("Two changes address the same widget differently.");
            byWidget.set(widget, {node, widget, value, target});
        };
        for (const [target, value] of Object.entries(changes)) {
            const match = /^(binding|note):([A-Za-z0-9_-]{1,64}):(value|seed_mode|label|note|text|order|group)$/.exec(target);
            if (!match) throw new Error("This target is unavailable.");
            const [, source, id, field] = match;
            if (source === "note" || !["value", "seed_mode"].includes(field)) {
                presentation.push({source, id, field, value});
                continue;
            }
            const d = (raw.bindings || []).find(b => b.binding_id === id);
            const resolved = bindings[id];
            if (!d || !resolved || resolved.status !== "ok" || !resolved.widget) {
                throw new Error("This setting is unavailable. Refresh the Controls panel.");
            }
            // The graph must still route this binding to the inputs the server
            // validated; an anchor that moved since the export is refused.
            const same = (a, b) => JSON.stringify((a || []).map(t => [String(t.path), t.input]).sort())
                === JSON.stringify((b || []).map(t => [String(t.path), t.input]).sort());
            if (!same(resolved.targets, d.targets)) throw new Error("This setting moved. Refresh the Controls panel.");
            if (field === "seed_mode") {
                if (!resolved.modeWidget) throw new Error("This seed has no native mode widget.");
                add(resolved.node, resolved.modeWidget, value, target);
                continue;
            }
            let next = value;
            if (d.kind === "INT" || d.kind === "SEED") {
                const number = Number(value);
                // A canvas widget holds a JS number: refuse, never round, a
                // whole number beyond 2^53 (the Sonder App keeps it exact).
                if (!Number.isSafeInteger(number) || String(number) !== String(value).trim()) {
                    throw new Error(`${value} is too large for the canvas widget to hold exactly. Use at most 9007199254740991.`);
                }
                next = number;
            } else if (d.kind === "FLOAT") next = Number(value);
            else if (d.kind !== "BOOLEAN") next = String(value);
            add(resolved.node, resolved.widget, next, target);
        }
        if (presentation.length) {
            const text = editManifest(saved, manifest => {
                for (const {source, id, field, value} of presentation) {
                    const table = source === "binding" ? manifest.bindings : manifest.notes;
                    if (!table[id] || typeof table[id] !== "object") throw new Error("This Control is no longer in the workflow.");
                    table[id][field === "text" ? "text" : field] = value;
                }
            });
            if (new TextEncoder().encode(exportManifest(text, root)).length > MAX_MANIFEST_BYTES) {
                throw new Error("The Controls would exceed 128 KiB. Shorten a note first.");
            }
            add(relay, manifestWidget, text, "manifest");
        }
        return [...byWidget.values()];
    }

    async function write(changes, { expectedPromptHash = null, canApply = () => true } = {}) {
        if (destroyed) return refused(changes, "The Controls panel has closed.");
        const id = identity();
        epoch++; // a passive read in flight must not land a pre-write view
        try {
            const shot = await snapshot(id);
            if (!sameGraph(id)) return refused(changes, "The workflow changed. Try again.");
            if (!await canApply() || !sameGraph(id) || (expectedPromptHash && await promptHash(shot.hash) !== expectedPromptHash)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            const raw = await describe(shot, changes);
            if (!sameGraph(id) || (await snapshot(id)).hash !== shot.hash || !sameGraph(id)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            if (!raw.ok) return {ok: false, rejected: raw.rejected || []};
            if (!await canApply()) return refused(changes, "This tab no longer owns the editor.");
            // An asynchronous permission check can overlap a native graph edit.
            if (!sameGraph(id) || (await snapshot(id)).hash !== shot.hash || !sameGraph(id)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            const writes = operations(raw, raw.changes, id.root);
            const tracker = getChangeTracker();
            if (typeof tracker?.captureCanvasState !== "function") throw new Error("Workflow history is unavailable.");
            // Native history remains the owner; establish the pre-write state.
            tracker.captureCanvasState();
            const before = writes.map(({widget}) => widget.value);
            try {
                for (const {node, widget, value} of writes) {
                    widget.value = value;
                    widget.callback?.(value, app.canvas, node);
                    if (widget.value !== value && String(widget.value) !== String(value)) {
                        throw new Error(`The canvas widget changed ${value} to ${widget.value} (its step or precision), so nothing was saved.`);
                    }
                }
                id.root.setDirtyCanvas?.(true, true);
                tracker.captureCanvasState();
            } catch (error) {
                // Restore values and replay callbacks, so state a callback
                // derives (a Selector's options, its rewritten choice) follows.
                writes.forEach(({widget}, i) => { widget.value = before[i]; });
                writes.forEach(({node, widget}, i) => {
                    try { widget.callback?.(before[i], app.canvas, node); } catch { /* restored value stands */ }
                    widget.value = before[i];
                });
                id.root.setDirtyCanvas?.(true, true);
                throw error;
            }
            epoch++; lastHash = null;
            schedule();
            return {ok: true, rejected: []};
        } catch (error) { return refused(changes, error.message || "The control could not be changed."); }
    }

    return {
        getView: () => view,
        subscribe(cb) { subscribers.add(cb); return () => subscribers.delete(cb); },
        setVisible: value => visibility("local", value),
        setMountedVisible: value => visibility("mounted", value),
        refresh,
        getPromptHash: async () => promptHash((await snapshot(identity())).hash),
        applyChanges(changes, options) {
            const owned = { ...changes };
            const run = writeTail.then(() => write(owned, options));
            writeTail = run.catch(() => {});
            return run;
        },
        setEditorSetting,
        destroy() { destroyed = true; epoch++; clearTimeout(timer); syncListeners(); subscribers.clear(); },
    };
}
