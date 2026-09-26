const MEDIA_TYPES = new Set(["image", "audio", "video"]);

export function isDirectInspectAssetUsable(asset) {
    return !!asset
        && MEDIA_TYPES.has(String(asset.asset_type || ""))
        && !asset.trashed
        && !asset.trashed_at
        && !asset.missing
        && !!asset.path;
}

/**
 * What each compare side shows, as ids. `candidateIds` are the same-type
 * assets compare may show, in order; `anchorId` is the overlay's own asset,
 * always one of them. A slot keeps its asset while it is a candidate. A
 * missing A falls back to the anchor, and B never repeats A: a missing or
 * equal B takes the first other candidate. Only with a single candidate do
 * both sides name the same asset.
 */
export function resolveCompareSlots(candidateIds = [], anchorId = "", leftId = "", rightId = "") {
    const ids = (candidateIds || []).filter(Boolean);
    const has = (id) => !!id && ids.includes(id);
    const left = has(leftId) ? leftId : (has(anchorId) ? anchorId : (ids[0] || ""));
    const right = has(rightId) && rightId !== left
        ? rightId
        : (ids.find((id) => id !== left) || left);
    return { leftId: left, rightId: right };
}

export function resolveInspectOverlayScope({
    origin = "gallery",
    requestedAssetId = "",
    visibleAssets = [],
    sortedProjectAssets = [],
} = {}) {
    const direct = origin === "direct";
    const source = direct
        ? sortedProjectAssets.filter(isDirectInspectAssetUsable)
        : visibleAssets;
    const seen = new Set();
    const assets = [];
    for (const asset of source) {
        const id = String(asset?.asset_id || "");
        if (!id || seen.has(id)) continue;
        seen.add(id);
        assets.push(asset);
    }
    const asset = assets.find((entry) => entry.asset_id === requestedAssetId) || null;
    return {
        origin: direct ? "direct" : "gallery",
        assets,
        asset,
    };
}
