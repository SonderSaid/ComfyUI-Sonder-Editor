// Canvas graph authority for the shared Controls panel. No app/DOM at import.
import {
    EXECUTION_VERSION, MAX_MANIFEST_BYTES, NATIVE_MODES, RELAY_CLASS, anchorSnapshot, applyCleanupPlan, definitionAnchorIds, dropAllAnchors,
    dropExecutionAnchors, executionAnchorSnapshot, graphsOf, applyGroupPlan, applyPresetEdit, dropAnchors, editManifest, exportManifest,
    executionCandidatesFor, inactiveBodies, isStructuralTarget, listCandidates, listExecutionCandidates, nodesOf, parseManifest, parseTarget, readAnchors,
    readExecution, readExecutionAnchor, relayCensus, resolveBindings, resolveCandidate, resolveExecution, resolveExecutionCandidate,
    planRestore, restoreAnchors, restoreDynamicPrompts, restoreExecutionAnchor, setAnchor, setExecutionAnchor,
    VIEWS_VERSION, dropViewAnchors, inactiveOutputs, isOutputNodeOf, nodeType, readViewAnchors, readViews, resolveExecPath, resolveViews,
    restoreViewAnchors, setViewAnchor, viewAnchorSnapshot,
} from "./control_bindings.js";
import { syncRelayRows } from "./control_relay_rows.js";
import { canonical, readRows, writeRows } from "./control_rows.js";
import { twoLaneHash } from "./text_hash.js";

const DESCRIBE_URL = "/sonder-editor/controls/describe";
const DEBOUNCE_MS = 400;
// Fields this writer lands; hide/unhide are app-only targets.
const FIELDS = new Set(["value", "seed_mode", "label", "note", "text", "order", "group", "advanced",
    "expose", "remove", "rebind", "create", "rename", "ungroup", "move", "description", "cleanup", "restore", "mode", "modes", "timing"]);
const samePaths = (a, b) => JSON.stringify((a || []).map(m => String(m.path)).sort())
    === JSON.stringify((b || []).map(m => String(m.path)).sort());
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
    return `local:${twoLaneHash(text)}`;
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
    // viewsDemand: a View pane or the pane picker shows in this page, so the
    // describe runs for its Views even while the Controls panel is hidden.
    let visible = false, mountedVisible = false, viewsDemand = false, destroyed = false, listening = false;
    let epoch = 0, timer = null, active = null, trailing = false, lastHash = null;
    let writeTail = Promise.resolve();
    // Manage is open in this page's panel (local) or a mounted tab (mounted);
    // candidates are listed only then, filtered before they are sent.
    const manage = {local: {on: false, filter: ""}, mounted: {on: false, filter: ""}};
    const activeManage = () => (mountedVisible && manage.mounted.on ? manage.mounted
        : visible && manage.local.on ? manage.local : null);
    const subscribers = new Set();
    const rootGraph = () => app.rootGraph || app.graph;
    const needed = () => visible || mountedVisible || viewsDemand;
    const notify = () => { for (const cb of subscribers) cb(view); };
    const identity = () => ({root: rootGraph(), editor: getEditorNode()});
    const sameGraph = id => !destroyed && id.root === rootGraph() && id.editor === getEditorNode() && !app.configuringGraph;
    // Visibility and graph events retire passive reads (epoch) but never a write
    // in flight: its fresh-export comparisons already refuse a changed workflow,
    // and hiding the panel right after an edit must not discard it.
    const isCurrent = (id, stamp) => epoch === stamp && sameGraph(id);
    const editorKey = (id, prompt) => nodesOf(id.root).includes(id.editor)
        && prompt[String(id.editor?.id)]?.class_type === "SonderEditor" ? String(id.editor.id) : null;
    const savedManifest = root => {
        const relay = relayCensus(root).active[0];
        try { return parseManifest(relay?.widgets?.find(w => w.name === "manifest")?.value || ""); }
        catch { return null; } // the server reports why from the export
    };
    const snapshot = async id => {
        const {output} = await app.graphToPrompt();
        if (!output || typeof output !== "object") throw new Error("The workflow could not be exported.");
        restoreDynamicPrompts(id.root, output);
        const key = editorKey(id, output);
        const listing = activeManage();
        // Graph identity is independent of the ephemeral Manage query. Anchors
        // and census still guard writes, including Restore after Relay deletion.
        const anchors = graphsOf(id.root).map(graph => nodesOf(graph).map(node => [node?.id, readAnchors(node), readExecutionAnchor(node), readViewAnchors(node)]));
        // Muted and bypassed target nodes are not in the export, so their
        // bodies, and each switch's members and modes, join the fingerprint:
        // an edit to either between validation and write refuses the write.
        const manifest = savedManifest(id.root);
        // A View's own muted source first, then every other muted output: the
        // server lists those as Views and candidates that are off.
        const views = manifest ? resolveViews(id.root, manifest).views : {};
        const viewSources = Object.values(views).filter(v => v.inactiveReason && v.node)
            .map(v => ({ node: v.node, path: v.path, inactive: v.inactiveReason }));
        const inactive = inactiveBodies(manifest ? resolveBindings(id.root, manifest).bindings : {},
            { first: viewSources, last: inactiveOutputs(id.root) });
        const switches = manifest ? Object.entries(resolveExecution(id.root, manifest).controls)
            .map(([sid, r]) => [sid, r.status, r.members.map(m => [m.path, m.mode])]) : [];
        const viewStates = Object.entries(views).map(([vid, v]) => [vid, v.status, v.path || null, v.participation || null]);
        const graphHash = JSON.stringify([key, output, anchors, relayCensus(id.root).census, inactive, switches, viewStates]);
        return {prompt: output, inactive, editor_key: key, graphHash, hash: JSON.stringify([graphHash, listing])};
    };
    const describe = async (shot, changes, extra = {}) => {
        const body = {prompt: shot.prompt, inactive: shot.inactive, relay_census: relayCensus(rootGraph()).census,
            definition_anchors: definitionAnchorIds(rootGraph()), ...extra};
        // The hosting Editor's prompt key, for picker hints. Always sent: null when
        // the Editor is not in the prompt (muted, bypassed, inside a subgraph), so
        // the server never takes another Editor for it and pickers fall back.
        body.editor_key = shot.editor_key || null;
        if (!body.relay_census.active) body.orphan_manifest = exportManifest("", rootGraph());
        const listing = activeManage();
        if (listing && changes === undefined) {
            const {candidates, truncated} = listCandidates(rootGraph(), {filter: listing.filter, labels: view.candidate_labels || []});
            const known = new Set(Object.keys(readExecution(savedManifest(rootGraph())?.execution).controls));
            const switches = listExecutionCandidates(rootGraph(), {filter: listing.filter, known});
            Object.assign(body, {manage: true, candidates, candidates_truncated: truncated, filter: listing.filter,
                execution_candidates: switches.candidates});
        }
        const relay = relayCensus(rootGraph()).active[0];
        const manifest = parseManifest(relay?.widgets?.find(w => w.name === "manifest")?.value || "");
        const restore = planRestore(rootGraph(), manifest);
        body.restore_plan = restore;
        if (restore.restorable.length) {
            const candidates = new Map((body.candidates || []).map(c => [`${c.path}/${c.input}`, c]));
            for (const {path, input} of restore.restorable) {
                candidates.set(`${path}/${input}`, {path, input, targets: [{path, input}]});
            }
            body.candidates = [...candidates.values()];
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
        if (which === "local") visible = !!value;
        else if (which === "views") viewsDemand = !!value;
        else mountedVisible = !!value;
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
            if (["group", "execution"].includes(parsed?.source) || !["expose", "rebind"].includes(parsed?.field) || !value?.candidate) continue;
            const live = resolveCandidate(root, value.candidate);
            if (live) found.set(`${live.node.id}/${live.name}`, {path: String(live.node.id), input: live.name,
                targets: live.targets, node_title: live.node_title});
        }
        return [...found.values()];
    }
    // The node and group candidates a batch adds or rebinds, resolved on the
    // live graph: the server checks exactly what the writer will anchor.
    function batchExecutionCandidates(changes, root) {
        const locators = Object.entries(changes || {}).filter(([target, value]) => {
            const parsed = parseTarget(target);
            return parsed?.source === "execution" && ["expose", "rebind"].includes(parsed.field) && value?.candidate;
        }).map(([, value]) => value.candidate);
        if (!locators.length) return undefined;
        const known = new Set(Object.keys(readExecution(savedManifest(root)?.execution).controls));
        return executionCandidatesFor(root, locators, {known});
    }

    // Native widget writes for value targets, one merged manifest write for
    // every presentation target, and node mode writes for switches, all in the
    // batch's one history step.
    function operations(raw, changes, root) {
        const {active, census} = relayCensus(root);
        const structural = Object.entries(changes).filter(([target, value]) => isStructuralTarget(target) && !value?.noop);
        // A first expose, note, group or switch may add the Relay; everything else needs the one Relay.
        const addsRelay = !active.length && !census.muted && !census.bypassed && !census.nested
            && (structural.length > 0 || "workflow:description" in changes)
            && structural.every(([target]) => { const p = parseTarget(target); return ["binding", "note", "group", "execution", "view"].includes(p.source) && ["expose", "create"].includes(p.field); });
        if (!structural.length && Object.values(changes).every(value => value?.noop)) return {writes: [], anchors: [], modes: []};
        if (active.length !== 1 && !addsRelay) throw new Error("Keep exactly one active Sonder Control Relay in the workflow.");
        const relay = active[0] || null;
        const manifestWidget = relay ? (relay.widgets || []).find(w => w?.name === "manifest") : null;
        if (relay && !manifestWidget) throw new Error("The Sonder Control Relay has no manifest.");
        const saved = typeof manifestWidget?.value === "string" ? manifestWidget.value : "";
        const {bindings, suggestions} = resolveBindings(root, parseManifest(saved));
        const switches = resolveExecution(root, parseManifest(saved));
        const views = resolveViews(root, parseManifest(saved));
        const byWidget = new Map(), anchors = [], manifestOps = [], byNode = new Map();
        const add = (node, widget, value, target) => {
            const existing = byWidget.get(widget);
            if (existing && existing.value !== value) throw new Error("Two changes address the same widget differently.");
            byWidget.set(widget, {node, widget, value, target});
        };
        // A node one switch and a group both set (a preset's overlap) takes one mode.
        const setMode = (node, mode) => {
            if (byNode.has(node) && byNode.get(node) !== mode) throw new Error("Two changes switch the same node differently.");
            byNode.set(node, mode);
        };
        for (const [target, value] of Object.entries(changes)) {
            const parsed = parseTarget(target);
            if (!parsed || (parsed.source !== "preset" && !FIELDS.has(parsed.field))) throw new Error("This target is unavailable.");
            const { source, id, field } = parsed;
            if (source === "preset") {
                manifestOps.push(m => applyPresetEdit(m, id, field, value));
                continue;
            }
            if (source === "execution") {
                if (!value?.noop) executionOp(root, {id, field, value}, raw, {switches, anchors, manifestOps, setMode});
                continue;
            }
            if (source === "view") {
                if (!value?.noop) viewOp(root, {id, field, value}, {views, anchors, manifestOps});
                continue;
            }
            if (source === "workflow") {
                if (field === "restore") {
                    const livePlan = new Map(planRestore(root, parseManifest(saved)).restorable.map(r => [r.binding_id, r]));
                    for (const row of value.bindings) {
                        const live = livePlan.get(row.binding_id);
                        if (!live || !sameTargets([live], row.targets) || !sameTargets([live], [row.candidate]))
                            throw new Error("A Control moved. Refresh the Controls panel before restoring.");
                        const candidate = resolveCandidate(root, live);
                        if (!candidate) throw new Error("A setting is no longer available. Refresh the Controls panel.");
                        anchors.push(() => setAnchor(candidate.node, candidate.name, row.binding_id));
                        manifestOps.push(m => { delete m.bindings[row.binding_id].restore_targets; });
                    }
                } else if (field === "cleanup") {
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
                    for (const sid of value.execution || []) {
                        if (switches.controls[sid]?.status !== "missing")
                            throw new Error("A node switch is no longer missing. Reopen the cleanup review.");
                        anchors.push(() => dropExecutionAnchors(root, sid));
                    }
                    manifestOps.push(m => applyCleanupPlan(m, value));
                } else manifestOps.push(m => { m.description = value; });
                continue;
            }
            if (source === "group") {
                // One unexpanded plan, expanded against this saved manifest.
                if (!value?.noop) manifestOps.push(m => applyGroupPlan(m, id, value, bindings, switches.controls));
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
            if (!d || !resolved || resolved.status !== "ok" || (!resolved.widget && !resolved.rows)) {
                throw new Error("This setting is unavailable. Refresh the Controls panel.");
            }
            // The graph must still route this binding to the inputs the server
            // validated; an anchor that moved since the export is refused.
            const same = (a, b) => JSON.stringify((a || []).map(t => [String(t.path), t.input]).sort())
                === JSON.stringify((b || []).map(t => [String(t.path), t.input]).sort());
            if (!same(resolved.targets, d.targets)) throw new Error("This setting moved. Refresh the Controls panel.");
            if (d.kind === "ROWS") {
                if (field !== "value" || !resolved.rows || !Array.isArray(value)) throw new Error("This setting is unavailable. Refresh the Controls panel.");
                if (!resolved.rows.ready) throw new Error("This ComfyUI frontend can't show these rows. Update ComfyUI's frontend to 1.57 or newer.");
                // The server checked the edit's base against this export; one
                // write per group, keyed so a second change to it is refused.
                if (byWidget.has(`rows:${resolved.node.id}/${resolved.rows.group}`)) throw new Error("Two changes address the same rows.");
                byWidget.set(`rows:${resolved.node.id}/${resolved.rows.group}`, {node: resolved.node, rows: resolved.rows, value, target});
                continue;
            }
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
        return {writes: [...byWidget.values()], anchors, modes: [...byNode].map(([node, mode]) => ({node, mode}))};
    }

    // One switch target. The server validated it against this graph's export;
    // the live members must still be the ones it validated.
    function executionOp(root, {id, field, value}, raw, {switches, anchors, manifestOps, setMode}) {
        const live = switches.controls[id];
        if (switches.error) throw new Error(switches.error);
        // An absent or damaged section is replaced only by an explicit edit;
        // another version's section is never rewritten.
        const table = m => {
            if (!m.execution || typeof m.execution !== "object" || Array.isArray(m.execution)) m.execution = {v: EXECUTION_VERSION, controls: {}};
            const {error} = readExecution(m.execution);
            if (error) throw new Error(error);
            if (!m.execution.controls || typeof m.execution.controls !== "object" || Array.isArray(m.execution.controls)) m.execution.controls = {};
            return m.execution.controls;
        };
        if (field === "mode" || field === "modes") {
            const described = (raw.execution || []).find(d => d.execution_id === id);
            if (!live || live.status !== "ok" || !described || !samePaths(live.members, described.members)) {
                throw new Error("This switch changed. Refresh the Controls panel.");
            }
            if (field === "modes" && !samePaths(live.members, Object.keys(value).map(path => ({path})))) {
                throw new Error("The group's nodes changed. Update the preset.");
            }
            for (const member of live.members) setMode(member.node, field === "modes" ? value[member.path] : value);
            return;
        }
        if (field === "remove") {
            manifestOps.push(m => { delete table(m)[id]; });
            anchors.push(() => dropExecutionAnchors(root, id));
            return;
        }
        if (field === "expose" || field === "rebind") {
            const chosen = resolveExecutionCandidate(root, value.candidate);
            if (!chosen) throw new Error("This node or group moved. Refresh the Controls panel.");
            if (chosen.node) {
                // An anchor naming no switch (a deleted Relay's) is replaced.
                const owner = readExecutionAnchor(chosen.node);
                if (owner && owner !== id && switches.entries[owner]) throw new Error("This node is already a switch.");
            }
            const target = chosen.kind === "group" ? {kind: "group", group_id: chosen.group.id} : {kind: chosen.kind};
            if (field === "rebind") {
                if (!live || live.status === "ok") throw new Error("Only a switch that is off can be rebound.");
                manifestOps.push(m => {
                    if (!table(m)[id]) throw new Error("This switch is no longer in the workflow.");
                    table(m)[id].target = target;
                });
                anchors.push(() => dropExecutionAnchors(root, id));
            } else {
                if (switches.entries[id]) throw new Error("This Control id is already in use.");
                manifestOps.push(m => {
                    table(m)[id] = {label: value.label || "", group: "", order: value.order ?? 0, advanced: false, note: "", target};
                });
            }
            if (chosen.node) anchors.push(() => { setExecutionAnchor(chosen.node, id); return [chosen.node]; });
            return;
        }
        // Presentation, in batch order (a later group rename sees this move).
        manifestOps.push(m => {
            const entry = table(m)[id];
            if (!entry || typeof entry !== "object") throw new Error("This switch is no longer in the workflow.");
            entry[field] = value;
        });
    }

    // One View target. The server validated it against this graph's export; the
    // live graph must still hold that output where the anchor goes.
    function viewOp(root, {id, field, value}, {views, anchors, manifestOps}) {
        if (views.error) throw new Error(views.error);
        // An absent or damaged section is replaced only by an explicit edit;
        // another version's section is never rewritten.
        const table = m => {
            if (!m.views || typeof m.views !== "object" || Array.isArray(m.views)) m.views = {v: VIEWS_VERSION, items: {}};
            const {error} = readViews(m.views);
            if (error) throw new Error(error);
            if (!m.views.items || typeof m.views.items !== "object" || Array.isArray(m.views.items)) m.views.items = {};
            return m.views.items;
        };
        if (field === "remove") {
            manifestOps.push(m => { delete table(m)[id]; });
            anchors.push(() => dropViewAnchors(root, id));
            return;
        }
        if (field === "create") {
            if (views.items[id]) throw new Error("This View id is already in use.");
            const {path, class_type: classType, host, rel = ""} = value.candidate || {};
            const hostNode = nodesOf(root).find(n => String(n?.id) === String(host));
            const live = resolveExecPath(root, path);
            const anchoredOn = live ? (rel ? live.hosts[0] : live.node) : null;
            if (!hostNode || !live || anchoredOn !== hostNode || nodeType(live.node) !== classType || !isOutputNodeOf(live.node)) {
                throw new Error("This output moved. Refresh the Controls panel.");
            }
            const owner = readViewAnchors(hostNode)[rel];
            if (owner && owner !== id && views.items[owner]) throw new Error("This output already has a View.");
            manifestOps.push(m => { table(m)[id] = {label: value.label || "", order: value.order ?? 0}; });
            anchors.push(() => { setViewAnchor(hostNode, rel, id); return [hostNode]; });
            return;
        }
        if (field !== "label" && field !== "order" && field !== "timing") throw new Error("This View change is unavailable.");
        manifestOps.push(m => {
            const entry = table(m)[id];
            if (!entry || typeof entry !== "object") throw new Error("This View is no longer in the workflow.");
            // Timing null: the View follows nothing (unlinked) again.
            if (field === "timing" && value === null) delete entry.timing;
            else entry[field] = value;
        });
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
            manifestOps.push(m => { delete m.bindings[id].restore_targets; });
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
            const raw = await describe(shot, changes, {candidates: batchCandidates(changes, id.root),
                execution_candidates: batchExecutionCandidates(changes, id.root)});
            if (!sameGraph(id) || (await snapshot(id)).graphHash !== shot.graphHash || !sameGraph(id)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            if (!raw.ok) {
                // A refusal can mean the panel's view is behind the graph (a
                // row list changed natively): re-read it so a retry starts there.
                schedule();
                return {ok: false, rejected: raw.rejected || []};
            }
            if (!await canApply()) return refused(changes, "This tab no longer owns the editor.");
            // An asynchronous permission check can overlap a native graph edit.
            if (!sameGraph(id) || (await snapshot(id)).graphHash !== shot.graphHash || !sameGraph(id)) {
                return refused(changes, "The workflow changed. Try again.");
            }
            const {writes, anchors, modes} = operations(raw, raw.changes, id.root);
            const tracker = getChangeTracker();
            if (typeof tracker?.captureCanvasState !== "function") throw new Error("Workflow history is unavailable.");
            // Native history remains the owner; establish the pre-write state.
            tracker.captureCanvasState();
            const before = writes.map(({node, widget, rows}) => (rows ? readRows(node, rows.group, rows.fieldIds) : widget?.value));
            const modesBefore = modes.map(({node}) => node.mode);
            const touched = new Map();
            let created = null;
            try {
                // Anchors first, so the export a created Relay makes sees them.
                if (anchors.length) for (const graph of graphsOf(id.root)) for (const node of nodesOf(graph))
                    touched.set(node, [anchorSnapshot(node), executionAnchorSnapshot(node), viewAnchorSnapshot(node)]);
                for (const apply of anchors) apply();
                for (const write of writes) {
                    if (write.createRelay) {
                        created = createRelay(app, id.root, id.editor);
                        write.node = created;
                        write.widget = (created?.widgets || []).find(w => w?.name === "manifest");
                        if (!write.widget) throw new Error("The new Sonder Control Relay has no manifest.");
                    }
                    const {node, widget, value} = write;
                    if (write.rows) {
                        // Rows change through the frontend's own count path, then
                        // each field like a native edit; all in this history step.
                        writeRows(node, write.rows.group, write.rows.fieldIds, value,
                            (field, next) => field.callback?.(next, app.canvas, node));
                        if (canonical(readRows(node, write.rows.group, write.rows.fieldIds)) !== canonical(value)) {
                            throw new Error("The canvas changed a row value (its step or options), so nothing was saved.");
                        }
                        continue;
                    }
                    widget.value = value;
                    widget.callback?.(value, app.canvas, node);
                    if (widget.value !== value && String(widget.value) !== String(value)) {
                        throw new Error(`The canvas widget changed ${value} to ${widget.value} (its step or precision), so nothing was saved.`);
                    }
                }
                // Modes as the native group menu sets them (node.mode, never
                // changeMode, which refuses bypass). Enabled leaves a node that
                // already runs (mode 1 or 3) as it is.
                for (const {node, mode} of modes) {
                    const next = mode === "enabled" && ![2, 4].includes(node.mode) ? node.mode : NATIVE_MODES[mode];
                    node.mode = next;
                    if (node.mode !== next) throw new Error("The canvas refused a node's mode, so nothing was saved.");
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
                for (const [{node, rows}, value] of restorable.filter(([write]) => write.rows)) {
                    try { writeRows(node, rows.group, rows.fieldIds, value, (field, next) => {
                        try { field.callback?.(next, app.canvas, node); } catch { /* restored value stands */ }
                        field.value = next;
                    }); } catch { /* the graph hash check refuses the next write */ }
                }
                const scalars = restorable.filter(([write]) => !write.rows);
                for (const [{widget}, value] of scalars) widget.value = value;
                for (const [{node, widget}, value] of scalars) {
                    try { widget.callback?.(value, app.canvas, node); } catch { /* restored value stands */ }
                    widget.value = value;
                }
                modes.forEach(({node}, i) => { node.mode = modesBefore[i]; });
                for (const [node, [snap, executionSnap, viewSnap]] of touched) {
                    restoreAnchors(node, snap);
                    restoreExecutionAnchor(node, executionSnap);
                    restoreViewAnchors(node, viewSnap);
                }
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
        // A View pane or the pane picker shows: keep its Views current.
        setViewsDemand: value => visibility("views", value),
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
