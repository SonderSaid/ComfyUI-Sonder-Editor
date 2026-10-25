// The Output Views host adapter, one per editor mode, one shape:
//
//   {mode, workflowKey(), capability(), runOutputs(), runBranch(path) -> Outcome,
//    cancel(Outcome) -> {ok, code}, resolve(Outcome) -> Outcome, setViewsDemand(bool),
//    onSessionLost(cb), onSessionRestored(cb), sessionLost(), editorKey(), nodeState(path)}
//   Outcome = {state: "queued", promptId?, runId?} | {state: "refused"|"unsupported", code}
//           | {state: "unconfirmed"|"busy"|"pending"}
//
// The host owns state, networking and lifecycle (views_coordinator.js reads
// through this); `runOutputs()` is a getter, so an adapter never keeps a
// client a project switch disposed. Fullscreen runs and cancels through its
// canvas controller, the mounted tab through its canvas host's run lane and the
// owner-scoped cancel route, the Sonder App through its session.
//
// `editorKey()` is this editor's Sonder Editor as a prompt key, which picks its
// executed context when several Editors fed one output (view_timing.js
// pickContext): the canvas node's id when it is a root node (an Editor inside
// a subgraph has a path key the page does not derive; "" then), the Sonder
// App's sole Editor.
//
// `nodeState(path)` says whether a node can take a new View where the page
// can tell: the canvas reads its live graph ({missing} | {inactive: reason} |
// null); the mounted tab and the Sonder App hold no graph (null), and the
// server's refusal says why. Live sampling previews (Phase 4) need no method
// of their own: `runOutputs()` reads and fetches them, and each runs/live read
// carries the server's `sampling` capability, fresh after every finish hint.
import { normalizeOutcome } from "./views_model.js";

const CAPABILITY_PATH = "/sonder-editor/runs/capability";
let capability = null;

// Whether this ComfyUI can run one output on its own (one read per page).
export function branchCapability(apiURL, fetchImpl = (...args) => globalThis.fetch(...args)) {
    if (capability) return capability;
    capability = (async () => {
        try {
            const payload = await (await fetchImpl(apiURL(CAPABILITY_PATH))).json();
            return payload?.branch && typeof payload.branch === "object" ? payload.branch : { ok: false, reason: "unknown" };
        } catch (_error) {
            capability = null; // retried on the next ask
            return { ok: false, reason: "unreachable" };
        }
    })();
    return capability;
}

export function _resetCapabilityForTests() {
    capability = null;
}

// Session events for hosts that have them (mounted, Sonder App): the page
// calls lost()/restored(); listeners hear each change once.
function sessionEvents() {
    const lost = new Set(), restored = new Set();
    let isLost = false;
    const on = (set, cb) => { if (typeof cb !== "function") return () => {}; set.add(cb); return () => set.delete(cb); };
    return {
        onSessionLost: cb => on(lost, cb),
        onSessionRestored: cb => on(restored, cb),
        sessionLost: () => isLost,
        lost() { if (isLost) return; isLost = true; for (const cb of [...lost]) cb(); },
        restored() { if (!isLost) return; isLost = false; for (const cb of [...restored]) cb(); },
    };
}

const cancelBy = async (client, body) => {
    if (!client) return { ok: false, code: "unavailable" };
    try {
        const result = await client.cancel(body);
        return { ok: !!result?.ok, code: String(result?.code || "") };
    } catch (_error) {
        return { ok: false, code: "unreachable" };
    }
};

// Fullscreen on the canvas page: the controller's client, branch runs and
// Controls host. The workflow is the canvas graph's (`rootGraph.id`).
export function createCanvasViewsHost({ controller, apiURL, fetchImpl, controlsHost = () => controller.getControlsHost?.(), nodeState = () => null }) {
    const events = sessionEvents();
    const graphId = () => String(controller.node?.graph?.rootGraph?.id || controller.node?.graph?.id || "");
    return {
        mode: "canvas",
        editorKey() {
            const graph = controller.node?.graph;
            const root = graph?.rootGraph || graph;
            return graph && graph === root && controller.node?.id !== undefined ? String(controller.node.id) : "";
        },
        workflowKey: () => (graphId() ? `c:${graphId()}` : ""),
        capability: () => branchCapability(apiURL, fetchImpl),
        runOutputs: () => controller.runOutputs || null,
        async runBranch(path) {
            try { return normalizeOutcome(await controller.runBranch(path), "canvas"); }
            catch (_error) { return { state: "refused", code: "failed" }; }
        },
        cancel: outcome => (outcome?.promptId ? cancelBy(controller.runOutputs, { promptId: outcome.promptId })
            : Promise.resolve({ ok: false, code: "no_prompt" })),
        resolve: outcome => outcome,
        setViewsDemand: on => controlsHost()?.setViewsDemand?.(!!on),
        nodeState: path => nodeState(path),
        ...events,
    };
}

// The mounted tab: its canvas host's owner scope, filtered to the canvas
// workflow named in the handoff URL; branch runs through the run lane.
export function createMountedViewsHost({ workflowId = "", editorKey = "", runOutputs, runRequests, controlsHost, apiURL, fetchImpl }) {
    const events = sessionEvents();
    return {
        mode: "mounted",
        editorKey: () => String(editorKey || ""),
        // No workflow id means no layout record, never a shared key.
        workflowKey: () => (workflowId ? `c:${workflowId}` : ""),
        capability: () => branchCapability(apiURL, fetchImpl),
        runOutputs: () => runOutputs() || null,
        async runBranch(path) {
            try { return normalizeOutcome(await runRequests().runBranch(path), "mounted"); }
            catch (_error) { return { state: "refused", code: "failed" }; }
        },
        cancel: outcome => (outcome?.promptId ? cancelBy(runOutputs(), { promptId: outcome.promptId })
            : Promise.resolve({ ok: false, code: "no_prompt" })),
        resolve: outcome => outcome,
        setViewsDemand: on => controlsHost()?.setViewsDemand?.(!!on),
        nodeState: () => null,
        ...events,
    };
}

// The Sonder App: its session's runs (by run id), the template as workflow.
export function createSdkViewsHost({ host, runOutputs, apiURL, fetchImpl, controlsHost = () => host.getControlsHost?.() }) {
    const events = sessionEvents();
    const runOf = runId => (host.status?.runs || []).find(run => run.run_id === runId) || null;
    return {
        mode: "sdk",
        editorKey() {
            const editors = controlsHost()?.getView?.()?.editors;
            return Array.isArray(editors) && editors.length === 1 ? String(editors[0]) : "";
        },
        workflowKey: () => (host.templateId ? `a:${host.templateId}` : ""),
        capability: () => branchCapability(apiURL, fetchImpl),
        runOutputs: () => runOutputs() || null,
        async runBranch(path) {
            try { return normalizeOutcome(await host.runBranch(path), "sdk"); }
            catch (_error) { return { state: "refused", code: "failed" }; }
        },
        cancel: outcome => (outcome?.runId ? cancelBy(runOutputs(), { runId: outcome.runId })
            : Promise.resolve({ ok: false, code: "no_run" })),
        // The run's prompt id and end, from the session status.
        resolve(outcome) {
            const run = outcome?.runId ? runOf(outcome.runId) : null;
            if (!run) return outcome;
            const final = ["landed", "failed", "canceled", "ended", "completed"].includes(run.state);
            return { ...outcome, ...(run.prompt_id ? { promptId: String(run.prompt_id) } : {}),
                ...(final ? { finalState: run.state } : {}) };
        },
        setViewsDemand: on => controlsHost()?.setViewsDemand?.(!!on),
        nodeState: () => null,
        ...events,
    };
}
