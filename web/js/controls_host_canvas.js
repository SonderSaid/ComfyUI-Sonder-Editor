// Canvas graph authority for the shared Controls panel. No app/DOM at import.
import { getGraphLink, getGraphNode } from "./project_source_resolver.js";
import { findControlWidget, seedModeWidget } from "./control_widgets.js";

const DESCRIBE_URL = "/sonder-editor/controls/describe";
const DEBOUNCE_MS = 400;
const nodeType = node => String(node?.comfyClass || node?.type || "");
const nodesOf = graph => graph?._nodes || graph?.nodes || [];
const valuesOf = map => map?.values ? [...map.values()] : Object.values(map || {});
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

function graphsOf(root) {
    const seen = new Set(), pending = [root];
    while (pending.length) {
        const graph = pending.pop();
        if (!graph || seen.has(graph)) continue;
        seen.add(graph);
        pending.push(...valuesOf(graph.subgraphs));
        for (const node of nodesOf(graph)) if (node?.subgraph) pending.push(node.subgraph);
    }
    return [...seen];
}

// API keys identify instance paths. A shared definition node may be reached
// through several keys, with a separate native promoted widget on each path.
function resolvePath(root, key) {
    const parts = String(key).split(":"), parents = [];
    let graph = root, node = null;
    for (let i = 0; i < parts.length; i++) {
        node = getGraphNode(graph, parts[i]);
        if (!node) return null;
        if (i < parts.length - 1) {
            if (!node.subgraph) return null;
            parents.push(node);
            graph = node.subgraph;
        }
    }
    return {node, parents};
}

function nativeWidget(path, name) {
    let node = path.node, widget = findControlWidget(node, name);
    const parents = [...path.parents];
    const seen = new Set();
    // An input boundary is a native promoted override; walk to the outermost
    // wrapper. String "-10" is what frontend 1.53.10 puts on that boundary.
    while (node && !seen.has(node)) {
        seen.add(node);
        const input = (node.inputs || []).find(i => (i.widget?.name || i.name) === name);
        if (!input || input.link == null) break;
        const link = getGraphLink(node.graph, input.link);
        if (!link || Number(link.origin_id) !== -10) return null; // linked by another node
        const parent = parents.pop();
        const outer = parent?.inputs?.[Number(link.origin_slot)];
        if (!outer) return null;
        widget = parent.getWidgetFromSlot?.(outer) || findControlWidget(parent, outer.widget?.name || outer.name);
        node = parent;
        name = outer.widget?.name || outer.name;
    }
    return widget ? {node, widget} : null;
}

export function createCanvasControlsHost({app, api, getEditorNode, setEditorSetting = () => {},
    getChangeTracker = () => app?.extensionManager?.workflow?.activeWorkflow?.changeTracker,
    debounceMs = DEBOUNCE_MS} = {}) {
    if (!app || typeof app.graphToPrompt !== "function" || typeof getEditorNode !== "function") {
        throw new TypeError("Canvas Controls needs an injected app and editor node getter.");
    }
    let view = {state: "idle", controls: [], fields: [], notes: [], error: null};
    let visible = false, mountedVisible = false, destroyed = false, listening = false;
    let epoch = 0, timer = null, active = null, trailing = false, lastHash = null;
    let writeTail = Promise.resolve();
    const subscribers = new Set();
    const rootGraph = () => app.rootGraph || app.graph;
    const needed = () => visible || mountedVisible;
    const notify = () => { for (const cb of subscribers) cb(view); };
    const identity = () => ({root: rootGraph(), editor: getEditorNode()});
    const isCurrent = (id, stamp) => !destroyed && epoch === stamp
        && id.root === rootGraph() && id.editor === getEditorNode() && !app.configuringGraph;
    const editorKey = (id, prompt) => nodesOf(id.root).includes(id.editor)
        && prompt[String(id.editor?.id)]?.class_type === "SonderEditor" ? String(id.editor.id) : null;
    const snapshot = async id => {
        const {output} = await app.graphToPrompt();
        if (!output || typeof output !== "object") throw new Error("The workflow could not be exported.");
        const key = editorKey(id, output);
        return {prompt: output, editor_key: key, hash: JSON.stringify([key, output])};
    };
    const describe = async (shot, changes) => {
        const body = {prompt: shot.prompt, editor_key: shot.editor_key};
        if (changes !== undefined) body.changes = changes;
        const response = await api.fetchApi(DESCRIBE_URL, {method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        if (!response.ok) throw new Error("The workflow controls could not be read.");
        return response.json();
    };
    const adopt = raw => {
        const controls = (raw.controls || []).map(d => {
            if (d.kind !== "SEED") return d;
            const matches = graphsOf(rootGraph()).flatMap(g => nodesOf(g)).filter(n =>
                nodeType(n) === "SonderControl" && findControlWidget(n, "control_id")?.value === d.control_id);
            const mode = matches.length === 1 ? seedModeWidget(findControlWidget(matches[0], "seed"))?.value : null;
            return {...d, seed_mode: mode || d.seed_mode || "fixed"};
        });
        view = {...raw, controls, state: "ready", error: null};
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

    function operations(raw, changes, root) {
        const byWidget = new Map();
        const add = (path, name, value, target) => {
            const resolved = nativeWidget(path, name);
            if (!resolved) throw new Error("This widget is unavailable or linked by another node.");
            const existing = byWidget.get(resolved.widget);
            if (existing && existing.value !== value) throw new Error("Two changes address the same widget differently.");
            byWidget.set(resolved.widget, {...resolved, value, target});
        };
        for (const [target, value] of Object.entries(changes)) {
            if (target.startsWith("field:")) {
                const descriptors = raw.fields.filter(d => `field:${d.class_type}/${d.field}` === target);
                if (!descriptors.length) throw new Error("This editor field is unavailable.");
                for (const d of descriptors) {
                    const path = resolvePath(root, d.key);
                    if (!path || nodeType(path.node) !== d.class_type) throw new Error("The field node changed.");
                    add(path, d.field, value, target);
                }
                continue;
            }
            const match = /^(control|note):(.+):(value|seed|seed_mode|note|text|order|group)$/.exec(target);
            if (!match) throw new Error("This target is unavailable.");
            const [, source, id, field] = match;
            const d = (source === "control" ? raw.controls : raw.notes).find(d =>
                (source === "control" ? d.control_id : d.note_id) === id);
            if (!d) throw new Error("This control is unavailable.");
            const type = source === "control" ? "SonderControl" : "SonderPanelNote";
            const matches = graphsOf(root).flatMap(g => nodesOf(g)).filter(n => nodeType(n) === type
                && findControlWidget(n, source === "control" ? "control_id" : "note_id")?.value === id);
            if (matches.length !== 1) throw new Error("This identity no longer names one shared definition.");
            for (const key of d.keys || [d.key]) {
                const path = resolvePath(root, key);
                if (!path || path.node !== matches[0]) throw new Error("The control instance changed.");
                const name = {value: "value_text", seed: "seed", seed_mode: "seed", note: "panel_note", text: "text", order: "panel_order", group: "panel_group"}[field];
                if (field === "seed_mode") {
                    const native = nativeWidget(path, name);
                    const promotedMode = seedModeWidget(native?.widget);
                    // Native promotions expose a numeric value wrapper but keep
                    // the after-generate mode on the definition's seed widget.
                    const mode = promotedMode || seedModeWidget(findControlWidget(path.node, "seed"));
                    if (!mode) throw new Error("This seed has no native mode widget.");
                    byWidget.set(mode, {node: promotedMode ? native.node : path.node, widget: mode, value, target});
                } else {
                    let next = field === "value" ? String(value) : value;
                    if (field === "seed") {
                        const number = Number(value);
                        next = Number.isSafeInteger(number) ? number : String(value);
                    }
                    add(path, name, next, target);
                }
            }
        }
        return [...byWidget.values()];
    }

    async function write(changes, { expectedPromptHash = null, canApply = () => true } = {}) {
        if (destroyed) return refused(changes, "The Controls panel has closed.");
        const id = identity(), stamp = ++epoch;
        try {
            const shot = await snapshot(id);
            if (!isCurrent(id, stamp)) return refused(changes, "The workflow changed. Try again.");
            if (!await canApply() || !isCurrent(id, stamp) || (expectedPromptHash && await promptHash(shot.hash) !== expectedPromptHash)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            const raw = await describe(shot, changes);
            if (!isCurrent(id, stamp) || (await snapshot(id)).hash !== shot.hash || !isCurrent(id, stamp)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            if (!raw.ok) return {ok: false, rejected: raw.rejected || []};
            if (!await canApply()) return refused(changes, "This tab no longer owns the editor.");
            // An asynchronous permission check can overlap a native graph edit.
            if (!isCurrent(id, stamp) || (await snapshot(id)).hash !== shot.hash || !isCurrent(id, stamp)) {
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
                        throw new Error("The native widget could not preserve this value.");
                    }
                }
                id.root.setDirtyCanvas?.(true, true);
                tracker.captureCanvasState();
            } catch (error) {
                writes.forEach(({widget}, i) => { widget.value = before[i]; });
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
        reset() { epoch++; lastHash = null; view = {state: "idle", controls: [], fields: [], notes: [], error: null}; notify(); schedule(); },
        destroy() { destroyed = true; epoch++; clearTimeout(timer); syncListeners(); subscribers.clear(); },
    };
}
