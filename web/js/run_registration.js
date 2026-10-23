// Register the runs this canvas page submits, so Sonder can observe their outputs.
//
// ComfyUI's own Run (and Queue selected output nodes) goes through
// `api.queuePrompt(number, {output, workflow}, {partialExecutionTargets})`. For a
// prompt holding Sonder Editors, the wrapper stamps a fresh receipt on shallow
// copies of those Editor nodes (`_meta.sonder_receipt`), calls the original at
// once and registers the receipt once ComfyUI accepts the prompt. The queue's
// `put` binds the prompt first and the registration adopts it, so Run is never
// delayed and a failed registration only means the run goes unobserved. `app.queuePrompt` is not wrapped: widget beforeQueued /
// afterQueued callbacks, native seed control and the frontend's job store are
// untouched, and the original `output` object (which the job store keeps, and
// other wrappers such as rgthree's may prune) is never mutated.
//
// Never take a prompt id from `app.queuePrompt`: it returns a boolean, and while
// another queue is processing it defers the request and returns false.

const EDITOR_CLASS = "SonderEditor";
const REGISTER_PATH = "/sonder-editor/runs/register";
const REGISTER_TIMEOUT_MS = 5000;

const nodesOf = graph => graph?._nodes || graph?.nodes || [];

function mintReceipt() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// A prompt key is the node id, or `host:inner` for a node inside subgraph
// instances. Every key of one shared definition resolves to the same node object.
export function nodeForPromptKey(rootGraph, key) {
    let graph = rootGraph, node = null;
    for (const part of String(key).split(":")) {
        if (!graph) return null;
        node = nodesOf(graph).find(item => String(item?.id) === part) || null;
        if (!node) return null;
        graph = node.subgraph || null;
    }
    return node;
}

function hostIdentity(controller) {
    if (!controller || controller._destroyed) return null;
    const projectId = controller._projectId?.() || "";
    const hostId = controller._hostId?.() || "";
    const sessionId = controller._editorSessionId || "";
    if (!projectId || !hostId || !sessionId) return null;
    return { projectId, hostId, sessionId };
}


// Pure: the stamped request data and the registrations it needs. A prompt with no
// Editor, or none this page hosts, comes back unchanged. `receiptFor(identity)`
// may supply a receipt already registered server-side (a mounted run request);
// that group is stamped with it and needs no registration from here.
export function stampReceipts(data, rootGraph, { partial = false, mint = mintReceipt, receiptFor = () => "" } = {}) {
    const output = data?.output;
    if (!output || typeof output !== "object") return { data, registrations: [] };
    const groups = new Map();
    for (const [key, node] of Object.entries(output)) {
        if (node?.class_type !== EDITOR_CLASS) continue;
        const identity = hostIdentity(nodeForPromptKey(rootGraph, key)?._sonderController);
        if (!identity) continue;
        const groupKey = `${identity.projectId}\u0000${identity.hostId}`;
        if (!groups.has(groupKey)) {
            const given = receiptFor(identity) || "";
            groups.set(groupKey, { ...identity, editors: [], receipt: given || mint(), preregistered: !!given });
        }
        groups.get(groupKey).editors.push(key);
    }
    if (!groups.size) return { data, registrations: [] };
    const stamped = { ...output };
    const registrations = [];
    for (const group of groups.values()) {
        for (const key of group.editors) {
            stamped[key] = { ...stamped[key], _meta: { ...(stamped[key]._meta || {}), sonder_receipt: group.receipt } };
        }
        if (group.preregistered) continue;
        registrations.push({
            receipt: group.receipt,
            project_id: group.projectId,
            host_id: group.hostId,
            session_id: group.sessionId,
            editors: group.editors,
            purpose: partial ? "branch" : "run",
        });
    }
    return { data: { ...data, output: stamped }, registrations };
}

// ── Branch runs ─────────────────────────────────────────────────────────────
//
// `runBranch(target)` runs one output node and what it needs, through ComfyUI's
// own Queue selected output nodes path (`app.queuePrompt` with that node), so
// widget callbacks, native seed control and the job store behave as a native
// partial run. `app.queuePrompt` never says which prompt it queued, so one
// pending *intent* is matched in the `api.queuePrompt` wrapper by its targets and
// resolved from ComfyUI's reply. While another queue is processing, the request
// waits its turn; past BRANCH_WAIT_MS it answers `unconfirmed`, and a later submit
// is still observed (it registers normally) but answers no caller.

const RELAY_CLASS = "SonderControlRelay";
const CAPABILITY_PATH = "/sonder-editor/runs/capability";
export const BRANCH_WAIT_MS = 30000;

let installed = null;
let pendingIntent = null;
let capability = null;

function sameTargets(targets, intent) {
    return Array.isArray(targets) && targets.length === 1 && String(targets[0]) === intent.target;
}

function refusalOf(error) {
    const response = error?.response || {};
    return {
        state: "refused",
        code: String(response.error?.type || error?.code || "rejected"),
        ...(response.node_errors && typeof response.node_errors === "object" ? { node_errors: response.node_errors } : {}),
    };
}

async function branchCapability() {
    if (capability?.ok) return capability;
    try {
        const resp = await installed.fetchImpl(installed.api.apiURL(CAPABILITY_PATH));
        const payload = await resp.json();
        capability = payload?.branch && typeof payload.branch === "object" ? payload.branch : { ok: false, reason: "unknown" };
    } catch (_error) {
        return { ok: false, reason: "unreachable" };
    }
    return capability;
}

function defaultIsOutputNode(classType) {
    const type = globalThis.LiteGraph?.registered_node_types?.[classType];
    return !!(type?.nodeData?.output_node ?? type?.prototype?.constructor?.nodeData?.output_node);
}

// Resolves to {state: "queued", prompt_id} | {state: "refused", code, node_errors?} |
// {state: "unconfirmed"} | {state: "busy"} | {state: "unsupported", code}.
export async function runBranch(target, { receipt = "", hostId = "" } = {}) {
    if (!installed) return { state: "unsupported", code: "not_installed" };
    const allowed = await branchCapability();
    if (!allowed.ok) return { state: "unsupported", code: allowed.reason || "unsupported" };
    if (pendingIntent) return { state: "busy" };
    target = String(target ?? "");
    const { app, isOutputNode } = installed;
    // Eligibility against what would be queued now: a muted, bypassed or
    // switched-off node is not in the serialized prompt.
    let output;
    try {
        ({ output } = await app.graphToPrompt());
    } catch (_error) {
        return { state: "refused", code: "prompt_build_failed" };
    }
    const node = output?.[target];
    if (!node) return { state: "refused", code: "branch_not_in_prompt" };
    if (node.class_type === RELAY_CLASS || !isOutputNode(node.class_type)) return { state: "refused", code: "branch_not_output" };
    if (pendingIntent) return { state: "busy" };
    return new Promise((resolve) => {
        const intent = {
            target, receipt: String(receipt || ""), hostId: String(hostId || ""), seen: false, settled: false, timer: null,
            settle(value) {
                if (intent.settled) return;
                intent.settled = true;
                clearTimeout(intent.timer);
                if (pendingIntent === intent) pendingIntent = null;
                resolve(value);
            },
        };
        pendingIntent = intent;
        Promise.resolve()
            .then(() => app.queuePrompt(0, 1, [target]))
            .then(() => {
                // Deferred behind another queue (false), or done without our
                // wrapper ever seeing it (another wrapper failed): wait, then stop.
                if (!intent.settled) intent.timer = setTimeout(() => intent.settle({ state: "unconfirmed" }), BRANCH_WAIT_MS);
            }, () => intent.settle({ state: "refused", code: "queue_failed" }));
    });
}

export function installRunRegistration({
    api, app, fetchImpl = globalThis.fetch?.bind(globalThis), warn = console.warn, isOutputNode = defaultIsOutputNode,
} = {}) {
    if (!api || typeof api.queuePrompt !== "function" || api.__sonderRunRegistration) return false;
    installed = { api, app, fetchImpl, isOutputNode };
    const original = api.queuePrompt;
    const register = (body) => {
        const controller = typeof AbortController === "function" ? new AbortController() : null;
        const timer = controller ? setTimeout(() => controller.abort(), REGISTER_TIMEOUT_MS) : null;
        return Promise.resolve()
            .then(() => fetchImpl(api.apiURL(REGISTER_PATH), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
                signal: controller?.signal,
            }))
            .then((resp) => { if (!resp?.ok) warn("Sonder: run registration was refused; its outputs are not observed.", resp?.status); })
            .catch(() => warn("Sonder: run registration failed; its outputs are not observed."))
            .finally(() => { if (timer) clearTimeout(timer); });
    };
    api.queuePrompt = function sonderQueuePrompt(number, data, options, ...rest) {
        let request = data, registrations = [];
        const targets = options?.partialExecutionTargets;
        const intent = pendingIntent && !pendingIntent.seen && !pendingIntent.settled && sameTargets(targets, pendingIntent)
            ? pendingIntent : null;
        if (intent) intent.seen = true;
        try {
            const partial = Array.isArray(targets) && targets.length > 0;
            const receiptFor = (identity) => (intent?.receipt && identity.hostId === intent.hostId ? intent.receipt : "");
            ({ data: request, registrations } = stampReceipts(data, app?.graph || app?.rootGraph, { partial, receiptFor }));
        } catch (_error) {
            request = data;
            registrations = [];
        }
        // `api` as the receiver: another wrapper may call through with `app`.
        const result = original.call(api, number, request, options, ...rest);
        // Register only what ComfyUI accepted: a refused prompt never reaches the
        // queue, and its receipt would only hold an unbound slot. The server
        // pairs a registration that arrives after the run started (or finished).
        Promise.resolve(result).then((response) => {
            if (response?.prompt_id) for (const body of registrations) void register(body);
            intent?.settle(response?.prompt_id ? { state: "queued", prompt_id: String(response.prompt_id) }
                : { state: "refused", code: "no_prompt_id" });
        }, (error) => intent?.settle(refusalOf(error)));
        return result;
    };
    api.__sonderRunRegistration = true;
    return true;
}

export function resetBranchRunsForTests() {
    pendingIntent = null;
    capability = null;
}
