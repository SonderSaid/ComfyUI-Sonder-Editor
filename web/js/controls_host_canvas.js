// Canvas graph authority for the shared Controls panel. No app/DOM at import.
import {
    MAX_MANIFEST_BYTES, RELAY_CLASS, anchorSnapshot, applyCleanupPlan, definitionAnchorIds, dropAllAnchors, graphsOf, applyGroupPlan, applyPresetEdit, dropAnchors, editManifest, exportManifest,
    isStructuralTarget, listCandidates, nodesOf, parseManifest, parseTarget, readAnchors, relayCensus, resolveBindings,
    resolveCandidate, restoreAnchors, restoreDynamicPrompts, setAnchor,
} from "./control_bindings.js";
import { syncRelayRows } from "./control_relay_rows.js";

const DESCRIBE_URL = "/sonder-editor/controls/describe";
const DEBOUNCE_MS = 400;
// Fields this writer lands; hide/unhide are app-only targets.
const FIELDS = new Set(["value", "seed_mode", "label", "note", "text", "order", "group", "advanced",
    "expose", "remove", "rebind", "create", "rename", "ungroup", "move", "description", "cleanup"]);
const sameTargets = (a, b) => JSON.stringify((a || []).map(t => [String(t.path), t.input]).sort())
    === JSON.stringify((b || []).map(t => [String(t.path), t.input]).sort());
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

// The first expose in a workflow without a Relay adds one at the root, beside
// the editor node, in the same history step.
function defaultCreateRelay(app, root, editor) {
    const node = globalThis.LiteGraph?.createNode?.(RELAY_CLASS);
    if (!node) throw new Error("The Sonder Control Relay node is not available. Update the Sonder nodes.");
    const [x, y] = editor?.pos || [0, 0];
    node.pos = [x + (editor?.size?.[0] || 0) + 40, y];
    root.add(node);
    return node;
}

export function createCanvasControlsHost({app, api, getEditorNode,
    getChangeTracker = () => app?.extensionManager?.workflow?.activeWorkflow?.changeTracker,
    createRelay = defaultCreateRelay, debounceMs = DEBOUNCE_MS} = {}) {
    if (!app || typeof app.graphToPrompt !== "function" || typeof getEditorNode !== "function") {
        throw new TypeError("Canvas Controls needs an injected app and editor node getter.");
    }
    let view = {state: "idle", bindings: [], notes: [], error: null};
    let visible = false, mountedVisible = false, destroyed = false, listening = false;
    let epoch = 0, timer = null, active = null, trailing = false, lastHash = null;
    let writeTail = Promise.resolve();
    // Manage is open in this page's panel (local) or a mounted tab (mounted);
    // candidates are listed only then, filtered before they are sent.
    const manage = {local: {on: false, filter: ""}, mounted: {on: false, filter: ""}};
    const activeManage = () => (mountedVisible && manage.mounted.on ? manage.mounted
        : visible && manage.local.on ? manage.local : null);
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
        restoreDynamicPrompts(id.root, output);
        const key = editorKey(id, output);
        const listing = activeManage();
        // Graph identity is independent of the ephemeral Manage query. Anchors
        // and census still guard writes, including Restore after Relay deletion.
        const anchors = graphsOf(id.root).map(graph => nodesOf(graph).map(node => [node?.id, readAnchors(node)]));
        const graphHash = JSON.stringify([key, output, anchors, relayCensus(id.root).census]);
        return {prompt: output, editor_key: key, graphHash, hash: JSON.stringify([graphHash, listing])};
    };
    const describe = async (shot, changes, extra = {}) => {
        const body = {prompt: shot.prompt, relay_census: relayCensus(rootGraph()).census, definition_anchors: definitionAnchorIds(rootGraph()), ...extra};
        if (!body.relay_census.active) body.orphan_manifest = exportManifest("", rootGraph());
        const listing = activeManage();
        if (listing && changes === undefined) {
            const {candidates, truncated} = listCandidates(rootGraph(), {filter: listing.filter, labels: view.candidate_labels || []});
            Object.assign(body, {manage: true, candidates, candidates_truncated: truncated});
        }
        if (changes !== undefined) body.changes = changes;
        const response = await api.fetchApi(DESCRIBE_URL, {method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        if (!response.ok) throw new Error("The workflow controls could not be read.");
        return response.json();
    };
    const adopt = raw => {
        // Canvas seeds follow the native mode; the export mirrored it.
        const bindings = raw.bindings || [];
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
    function setManage(which, on, filter = "") {
        const next = {on: !!on, filter: String(filter || "").slice(0, 120)};
        if (manage[which].on === next.on && manage[which].filter === next.filter) return;
        manage[which] = next;
        if (needed()) { epoch++; lastHash = null; schedule(); }
    }

    // The candidates a batch names, resolved on the live graph, so the server
    // validates exactly what the writer will anchor.
    function batchCandidates(changes, root) {
        const found = new Map();
        for (const [target, value] of Object.entries(changes || {})) {
            const parsed = parseTarget(target);
            if (parsed?.source === "group" || !["expose", "rebind"].includes(parsed?.field) || !value?.candidate) continue;
            const live = resolveCandidate(root, value.candidate);
            if (live) found.set(`${live.node.id}/${live.name}`, {path: String(live.node.id), input: live.name,
                targets: live.targets, node_title: live.node_title});
        }
        return [...found.values()];
    }

    // Native widget writes for value targets, and one merged manifest write for
    // every presentation target in the batch (one widget, one history step).
    function operations(raw, changes, root) {
        const {active, census} = relayCensus(root);
        const structural = Object.entries(changes).filter(([target, value]) => isStructuralTarget(target) && !value?.noop);
        // A first expose, note or group may add the Relay; everything else needs the one Relay.
        const addsRelay = !active.length && !census.muted && !census.bypassed && !census.nested
            && (structural.length > 0 || "workflow:description" in changes)
            && structural.every(([target]) => { const p = parseTarget(target); return ["binding", "note", "group"].includes(p.source) && ["expose", "create"].includes(p.field); });
        if (!structural.length && Object.values(changes).every(value => value?.noop)) return {writes: [], anchors: []};
        if (active.length !== 1 && !addsRelay) throw new Error("Keep exactly one active Sonder Control Relay in the workflow.");
        const relay = active[0] || null;
        const manifestWidget = relay ? (relay.widgets || []).find(w => w?.name === "manifest") : null;
        if (relay && !manifestWidget) throw new Error("The Sonder Control Relay has no manifest.");
        const saved = typeof manifestWidget?.value === "string" ? manifestWidget.value : "";
        const {bindings, suggestions} = resolveBindings(root, parseManifest(saved));
        const byWidget = new Map(), anchors = [], manifestOps = [];
        const add = (node, widget, value, target) => {
            const existing = byWidget.get(widget);
            if (existing && existing.value !== value) throw new Error("Two changes address the same widget differently.");
            byWidget.set(widget, {node, widget, value, target});
        };
        for (const [target, value] of Object.entries(changes)) {
            const parsed = parseTarget(target);
            if (!parsed || (parsed.source !== "preset" && !FIELDS.has(parsed.field))) throw new Error("This target is unavailable.");
            const { source, id, field } = parsed;
            if (source === "preset") {
                manifestOps.push(m => applyPresetEdit(m, id, field, value));
                continue;
            }
            if (source === "workflow") {
                if (field === "cleanup") {
                    // Re-resolve immediately before touching graph state. The
                    // server already rechecked the exported descriptor view.
                    for (const bid of value.bindings) if (bindings[bid]?.status !== "missing")
                        throw new Error("A Control is no longer missing. Reopen the cleanup review.");
                    const definitions = new Set(definitionAnchorIds(root));
                    for (const bid of value.suggestions) if (bindings[bid] || !suggestions.some(s => s.binding_id === bid))
                        throw new Error("An anchor changed. Reopen the cleanup review.");
                    for (const bid of value.definition_anchors) if (bindings[bid] || !definitions.has(bid))
                        throw new Error("A definition anchor changed. Reopen the cleanup review.");
                    for (const bid of new Set([...value.bindings, ...value.suggestions, ...value.definition_anchors]))
                        anchors.push(() => dropAllAnchors(root, bid));
                    manifestOps.push(m => applyCleanupPlan(m, value));
                } else manifestOps.push(m => { m.description = value; });
                continue;
            }
            if (source === "group") {
                // One unexpanded plan, expanded against this saved manifest.
                if (!value?.noop) manifestOps.push(m => applyGroupPlan(m, id, value, bindings));
                continue;
            }
            if (isStructuralTarget(target)) {
                if (!value?.noop) structuralOp(root, {source, id, field, value}, {bindings, suggestions, anchors, manifestOps});
                continue;
            }
            if (source === "note" || !["value", "seed_mode"].includes(field)) {
                // Preserve batch order: a subsequent group rename must see
                // this member move, just as the SDK sidecar writer does.
                manifestOps.push(manifest => {
                    const table = source === "binding" ? manifest.bindings : manifest.notes;
                    if (!table[id] || typeof table[id] !== "object") throw new Error("This Control is no longer in the workflow.");
                    table[id][field] = value;
                });
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
        if (manifestOps.length) {
            const text = editManifest(saved, manifest => {
                for (const edit of manifestOps) edit(manifest);
            });
            // The writer never saves text its own parser refuses.
            parseManifest(text);
            if (new TextEncoder().encode(exportManifest(text, root)).length > MAX_MANIFEST_BYTES) {
                throw new Error("The Controls would exceed 128 KiB. Shorten a note first.");
            }
            if (relay) add(relay, manifestWidget, text, "manifest");
            else byWidget.set("relay", {createRelay: true, value: text, target: "manifest"});
        }
        return {writes: [...byWidget.values()], anchors};
    }

    // Anchor and manifest edits for one structural target. The server already
    // validated it against this graph's export; the live graph must agree.
    function structuralOp(root, {source, id, field, value}, {bindings, suggestions, anchors, manifestOps}) {
        if (source === "note") {
            if (field === "create") manifestOps.push(m => { m.notes[id] = {text: value.text, group: value.group, order: value.order}; });
            else manifestOps.push(m => { delete m.notes[id]; });
            return;
        }
        if (field === "remove") {
            manifestOps.push(m => { delete m.bindings[id]; });
            anchors.push(() => dropAnchors(root, id));
            return;
        }
        let live = null;
        if (value.candidate) {
            live = resolveCandidate(root, value.candidate);
            if (!live || !sameTargets(live.targets, value.targets)) throw new Error("This setting moved. Refresh the Controls panel.");
            const owner = readAnchors(live.node)[live.name];
            if (owner && owner !== id) throw new Error("This setting is already a Control.");
        } else if (!suggestions.some(s => s.binding_id === id)) {
            throw new Error("This setting is no longer in the workflow. Refresh the Controls panel.");
        }
        if (field === "rebind") {
            if (!bindings[id] || bindings[id].status === "ok") throw new Error("Only a setting that is off can be rebound.");
            anchors.push(() => dropAnchors(root, id));
            anchors.push(() => { setAnchor(live.node, live.name, id); return [live.node]; });
            return;
        }
        if (bindings[id]) throw new Error("This Control id is already in use.");
        manifestOps.push(m => {
            // No section: the server derives it from the target nodes' classes.
            m.bindings[id] = {label: value.label, group: "", order: value.order, advanced: false, note: ""};
        });
        if (live) anchors.push(() => { setAnchor(live.node, live.name, id); return [live.node]; });
    }

    async function write(changes, { expectedPromptHash = null, canApply = () => true } = {}) {
        if (destroyed) return refused(changes, "The Controls panel has closed.");
        const id = identity();
        epoch++; // a passive read in flight must not land a pre-write view
        try {
            const shot = await snapshot(id);
            if (!sameGraph(id)) return refused(changes, "The workflow changed. Try again.");
            if (!await canApply() || !sameGraph(id) || (expectedPromptHash && await promptHash(shot.graphHash) !== expectedPromptHash)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            const raw = await describe(shot, changes, {candidates: batchCandidates(changes, id.root)});
            if (!sameGraph(id) || (await snapshot(id)).graphHash !== shot.graphHash || !sameGraph(id)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            if (!raw.ok) return {ok: false, rejected: raw.rejected || []};
            if (!await canApply()) return refused(changes, "This tab no longer owns the editor.");
            // An asynchronous permission check can overlap a native graph edit.
            if (!sameGraph(id) || (await snapshot(id)).graphHash !== shot.graphHash || !sameGraph(id)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            const {writes, anchors} = operations(raw, raw.changes, id.root);
            const tracker = getChangeTracker();
            if (typeof tracker?.captureCanvasState !== "function") throw new Error("Workflow history is unavailable.");
            // Native history remains the owner; establish the pre-write state.
            tracker.captureCanvasState();
            const before = writes.map(({widget}) => widget?.value);
            const touched = new Map();
            let created = null;
            try {
                // Anchors first, so the export a created Relay makes sees them.
                if (anchors.length) for (const graph of graphsOf(id.root)) for (const node of nodesOf(graph))
                    touched.set(node, anchorSnapshot(node));
                for (const apply of anchors) apply();
                for (const write of writes) {
                    if (write.createRelay) {
                        created = createRelay(app, id.root, id.editor);
                        write.node = created;
                        write.widget = (created?.widgets || []).find(w => w?.name === "manifest");
                        if (!write.widget) throw new Error("The new Sonder Control Relay has no manifest.");
                    }
                    const {node, widget, value} = write;
                    widget.value = value;
                    widget.callback?.(value, app.canvas, node);
                    if (widget.value !== value && String(widget.value) !== String(value)) {
                        throw new Error(`The canvas widget changed ${value} to ${widget.value} (its step or precision), so nothing was saved.`);
                    }
                }
                // The Relay's rows (and the size they need) follow now, inside
                // this history step rather than as one of their own.
                for (const relay of relayCensus(id.root).active) {
                    try { syncRelayRows(relay, {root: id.root, canvas: app.canvas, resize: true}); } catch { /* rows refresh on their own tick */ }
                }
                id.root.setDirtyCanvas?.(true, true);
                tracker.captureCanvasState();
            } catch (error) {
                // Restore values and replay callbacks, so state a callback
                // derives (a Selector's options, its rewritten choice) follows.
                const restorable = writes.map((write, i) => [write, before[i]]).filter(([write]) => !write.createRelay);
                for (const [{widget}, value] of restorable) widget.value = value;
                for (const [{node, widget}, value] of restorable) {
                    try { widget.callback?.(value, app.canvas, node); } catch { /* restored value stands */ }
                    widget.value = value;
                }
                for (const [node, snap] of touched) restoreAnchors(node, snap);
                if (created) id.root.remove?.(created);
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
        setManage: (on, filter) => setManage("local", on, filter),
        setMountedManage: (on, filter) => setManage("mounted", on, filter),
        // The workflow owns its Controls here: remove, never hide.
        capabilities: {hide: false, removeAuthored: true},
        refresh,
        getPromptHash: async () => promptHash((await snapshot(identity())).graphHash),
        applyChanges(changes, options) {
            const owned = { ...changes };
            const run = writeTail.then(() => write(owned, options));
            writeTail = run.catch(() => {});
            return run;
        },
        destroy() { destroyed = true; epoch++; clearTimeout(timer); syncListeners(); subscribers.clear(); },
    };
}
