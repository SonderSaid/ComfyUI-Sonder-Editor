const MEDIA_TYPES = new Set(["image", "audio", "video"]);

export function isDirectInspectAssetUsable(asset) {
    return !!asset
        && MEDIA_TYPES.has(String(asset.asset_type || ""))
        && !asset.trashed
        && !asset.trashed_at
        && !asset.missing
        && !!asset.path;
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
