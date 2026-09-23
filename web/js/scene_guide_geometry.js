// @server-mirror server/routes.py::_apply_swap_guides
// @server-mirror server/routes.py::_validate_guide_identity
// Scope/parity: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES.
// The host owns undo, queuing, reconciliation and popup lifecycle. This leaf
// owns only the in-place optimistic exchange and its applicability. Raw frame
// order is canonical: the -1 sentinel sorts first, unlike popup display order.

/**
 * `_validate_guide_identity`'s decision: every key the caller's snapshot
 * carries must match. Exported so a host local apply acts only on the guide
 * its caller saw, with the same answer the server's compare will give.
 */
export function guideIdentityMatches(guide, expected) {
    if (!expected || typeof expected !== "object" || Array.isArray(expected)) return true;
    // Preserve explicit nulls: Python getattr defaults only missing fields.
    const values = { guide_id: guide.guide_id === undefined ? "" : guide.guide_id,
        frame_index: guide.frame_index === undefined ? 0 : guide.frame_index,
        asset_id: guide.asset_id === undefined ? "" : guide.asset_id,
        source: guide.source === undefined ? "" : guide.source,
        strength: guide.strength === undefined ? 1 : guide.strength, muted: !!guide.muted };
    return Object.entries(values).every(([key, value]) => {
        if (!(key in expected)) return true;
        const prior = expected[key];
        if (typeof value === "number" && typeof prior === "number") return Math.abs(value - prior) <= 1e-9;
        return value === prior;
    });
}

export function applyGuideSwap(scene, operation) {
    if (!scene || scene.guide_track_config?.locked) return false;
    const a = operation.frame_index_a, b = operation.frame_index_b;
    // The host emits integers. Refuse malformed external input conservatively.
    if (!Number.isInteger(a) || !Number.isInteger(b) || a === b) return false;
    const guides = scene.guide_frames || [];
    const first = guides.find((guide) => guide.frame_index === a);
    const second = guides.find((guide) => guide.frame_index === b);
    if (!first || !second || !guideIdentityMatches(first, operation.expected_a)
            || !guideIdentityMatches(second, operation.expected_b)) return false;
    if (!first.guide_id || !second.guide_id || first.guide_id === second.guide_id) return false;
    first.frame_index = b;
    second.frame_index = a;
    guides.sort((left, right) => left.frame_index - right.frame_index);
    return true;
}
