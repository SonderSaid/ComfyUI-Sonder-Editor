function normalizePath(value) {
    return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").trim();
}

function buildAssetLookups(assets = []) {
    const byId = new Map();
    const byPath = new Map();
    for (const asset of assets || []) {
        const assetId = String(asset?.asset_id || "");
        if (assetId) byId.set(assetId, asset);
        const path = normalizePath(asset?.path);
        if (path) byPath.set(path, asset);
    }
    return { byId, byPath };
}

export function deriveCurrentSceneAssetIds(scene, assets = []) {
    const ids = new Set();
    const { byId, byPath } = buildAssetLookups(assets);
    const addId = (assetId) => {
        const id = String(assetId || "");
        if (id) ids.add(id);
    };
    const addPath = (sourcePath) => {
        const asset = byPath.get(normalizePath(sourcePath));
        if (asset?.asset_id) addId(asset.asset_id);
    };

    for (const clip of scene?.clips || []) addPath(clip?.source_path);
    for (const track of scene?.audio_tracks || []) addPath(track?.source_path);
    for (const guide of scene?.guide_frames || []) {
        const assetId = String(guide?.asset_id || "");
        if (!assetId) continue;
        addId(byId.has(assetId) ? byId.get(assetId).asset_id : assetId);
    }

    return Array.from(ids);
}
