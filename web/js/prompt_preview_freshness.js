// @server-mirror server/routes.py::PROMPT_CANDIDATE_OVERLAY_FIELDS
// @server-mirror server/routes.py::PROMPT_CANDIDATE_OVERLAY_ALIASES
// @server-mirror server/change_certificates.py::prompt_dependency_projection
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
// When a prompt preview result still answers, without asking the server again.
//
// Leaf module: DOM-free and host-free, so its decisions can be tested under Node.
// A preview compile's output is a function of three things:
//
//   1. The candidate fields the server takes FROM THE REQUEST. That is the
//      overlay allow-list mirrored below; every other candidate field is read
//      from the stored scene instead.
//   2. The request's other inputs: template, window, selection, context,
//      frame constraint, FPS, labels and pending identity creates.
//   3. The stored project at the request's base version.
//
// `promptCompileSemanticKey` covers the first two exactly. The third moves with
// the project version, and a result read at an older version still answers only
// when server change certificates prove, transition by transition, that the
// prompt read of this scene is unchanged (`api_client.certifiedUnchanged`). A
// failed or stale result never answers, whatever its key.

export const PROMPT_CANDIDATE_OVERLAY_ALIASES = Object.freeze({
    sections: "prompt_sections",
    global_documents: "global_channel_docs",
    global_attachments: "global_attachments",
});

export const PROMPT_CANDIDATE_OVERLAY_FIELDS = Object.freeze([
    "prompt_sections", "global_channels", "global_channel_docs",
    "global_attachments", "prompt_context_profile_id",
    "prompt_context_profile_config",
    "guide_frames", "reference_items",
    "reference_lane_recipes", "reference_lane_configs",
    "reference_lane_count", "duration_frames", "fps",
]);

const OVERLAY_FIELD_SET = new Set(PROMPT_CANDIDATE_OVERLAY_FIELDS);

// The compile reads only `hidden` from a Reference lane config (staging drops a
// hidden lane's items); name, lock and colour are presentation. This is the
// same narrowing as the server's `prompt_dependency_projection`, and
// `tests/test_change_certificates.py` proves the compile ignores the rest.
const OVERLAY_PROJECTIONS = Object.freeze({
    reference_lane_configs: (configs) => (Array.isArray(configs)
        ? configs.map((config) => ({ hidden: !!(config && typeof config === "object" && config.hidden) }))
        : configs),
});

/** Canonical JSON: object keys sorted at every depth, so equal values key equally. */
export function stableStringify(value) {
    if (value === undefined) return "null";
    if (value === null || typeof value !== "object") return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
    const keys = Object.keys(value).filter((key) => value[key] !== undefined).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
}

/**
 * The identity of a compile request by everything that can change its output
 * except the stored project. `base_modified_at` is a precondition, not an input:
 * version validity is decided separately, by certificates.
 */
export function promptCompileSemanticKey(body) {
    const request = body && typeof body === "object" ? body : {};
    const scene = request.scene && typeof request.scene === "object" ? request.scene : {};
    const overlay = {};
    for (const [rawKey, value] of Object.entries(scene)) {
        const key = PROMPT_CANDIDATE_OVERLAY_ALIASES[rawKey] || rawKey;
        if (!OVERLAY_FIELD_SET.has(key)) continue;
        overlay[key] = OVERLAY_PROJECTIONS[key] ? OVERLAY_PROJECTIONS[key](value) : value;
    }
    const rest = {};
    for (const [key, value] of Object.entries(request)) {
        if (key !== "scene" && key !== "base_modified_at") rest[key] = value;
    }
    return stableStringify({ overlay, request: rest });
}

/**
 * Whether a cached preview result answers a request with `key` at `version`.
 *
 * `certifiedUnchanged` is `api_client.certifiedUnchanged`, passed in so this
 * module stays a leaf.
 */
export function promptResultAnswers({ cache, sceneId, key, projectId, version,
    certifiedUnchanged } = {}) {
    if (!cache || cache._candidate_scene_id !== sceneId) return false;
    if (cache._stale === true || cache._failed === true) return false;
    if (!key || cache._semantic_key !== key) return false;
    const from = String(cache._version || "");
    if (!from || typeof certifiedUnchanged !== "function") return false;
    return certifiedUnchanged({ projectId, sceneId, fromVersion: from,
        toVersion: String(version || ""), dependency: "prompt" }) === true;
}

/**
 * Whether pending work for a branch already answers a request with `key`.
 *
 * Scheduled work builds its body when it fires, at whatever version then holds,
 * so it answers any request with the same key. Work already in flight answers
 * only while the project is still at the version it was sent at. Once any
 * transition lands, the request typically meets a version conflict on the
 * server, and if its bounded retry is exhausted, the branch would be left
 * failed with every later intent joined to it and nothing asking again. A
 * certified irrelevant edit therefore re-sends in-flight work, as before this
 * module existed, and saves requests only against held results.
 */
export function pendingPromptWorkAnswers({ pending, sceneId, key, version } = {}) {
    if (!pending || !key || pending.key !== key || pending.sceneId !== sceneId) return false;
    if (pending.state === "scheduled") return true;
    if (pending.state !== "inflight") return false;
    const from = String(pending.version || "");
    return !!from && from === String(version || "");
}
