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

/**
 * Where compare goes when `removedId` leaves its candidates (an unfavorite in
 * the Favorites view). Every list is ids as they stood BEFORE the removal:
 * `sideLists.A` / `.B` are each side's filtered list, or null while a
 * metadata search is not ready; `sameTypeIds` are all same-type candidates.
 *
 * A vacated slot (the removed id, or any id no longer a candidate) takes the
 * next id after the removed one in its side's list,
 * wrapping, as ↓ would, skipping the removed id and the other slot's id; A
 * resolves first and B is checked against the new A. A side list that is
 * empty, not ready, lacks the removed id or has nothing else to offer falls
 * back to `sameTypeIds`. Fewer than two remaining candidates end compare,
 * naming the survivor (empty when none).
 */
export function planCompareRemoval({ sideLists = {}, sameTypeIds = [], leftId = "", rightId = "", removedId = "" } = {}) {
    const remaining = (sameTypeIds || []).filter((id) => id && id !== removedId);
    if (remaining.length < 2) return { compare: false, survivorId: remaining[0] || "" };
    const nextAfterRemoved = (list, excludeId) => {
        const start = list.indexOf(removedId);
        if (start < 0) return "";
        for (let step = 1; step <= list.length; step += 1) {
            const id = list[(start + step) % list.length];
            if (id !== excludeId && remaining.includes(id)) return id;
        }
        return "";
    };
    const vacate = (side, excludeId) => {
        const list = Array.isArray(sideLists?.[side]) ? sideLists[side] : [];
        return nextAfterRemoved(list, excludeId)
            || nextAfterRemoved(sameTypeIds, excludeId)
            || remaining.find((id) => id !== excludeId)
            || "";
    };
    // A slot is vacated by the removal, or already was: an asset that left
    // earlier (a list from elsewhere) is no more a contender than this one.
    const left = remaining.includes(leftId)
        ? leftId
        : vacate("A", remaining.includes(rightId) ? rightId : "");
    const right = remaining.includes(rightId) && rightId !== left
        ? rightId
        : vacate("B", left);
    return { compare: true, leftId: left, rightId: right };
}

/**
 * An external item (a View pane's output or sampling preview,
 * `view_inspect_items.js`) in the shape the overlay reads. It names no file
 * and no project record, so nothing asset-keyed can resolve it.
 */
export function externalOverlayAsset(item) {
    return {
        asset_id: item.id,
        asset_type: item.kind,
        name: item.title,
        external: true,
        path: "",
        fps: item.fps,
        duration_sec: null,
        has_thumbnail: false,
        has_audio: false,
        place: item.place || "",
        note: item.note || "",
        status: item.status || "",
        details: item.details || [],
    };
}

/**
 * What the overlay offers for its source. Project assets get everything; an
 * external item gets nothing that reads or writes the project: no favorite or
 * Trash, no search, sort or metadata filtering, no provenance demand, proxies
 * or server media.
 */
export function inspectOverlayPolicy(external = false) {
    const project = !external;
    return {
        favorites: project,
        trash: project,
        metadataFilters: project,
        detailDemand: project,
        projectMedia: project,
        chooserSearch: project,
        // External candidates keep pane order.
        chooserSort: project,
        chooserTitle: (side) => (project ? `Gallery ${side}` : `Pane ${side}`),
        chooserHint: (side) => (project
            ? `Left click assigns A. Right click assigns B. Search filters Gallery ${side}.`
            : "Left click assigns A. Right click assigns B."),
        compareTitle: (possible) => {
            if (project) return possible ? "Compare (C)" : "Compare needs ≥ 2 same-type visible assets";
            return possible ? "Compare with another pane showing the same kind of media (C)"
                : "Compare needs another pane showing the same kind of media";
        },
        cycleTitle: `Up/Down arrows cycle this side's ${project ? "asset" : "pane"} in compare mode (click to switch side)`,
        metadataTitle: project ? "Show or hide asset metadata in this inspector" : "Show or hide what this View is",
    };
}

// Shortcuts that act on a project asset, so an external overlay has neither.
export const INSPECT_ASSET_ONLY_KEYS = Object.freeze(new Set(["S", "Delete"]));
const EXTERNAL_SHORTCUT_TEXT = {
    "ArrowLeft / ArrowRight": "Video: step 1 frame back / forward · Image/Audio: previous / next pane",
    "ArrowUp / ArrowDown": "Previous / next pane (compare: cycle the active A/B side)",
};

// The overlay's own help rows: `[key, description]` pairs.
export function inspectHelpRows(rows, external = false) {
    if (!external) return rows;
    return rows.filter(([key]) => !INSPECT_ASSET_ONLY_KEYS.has(key))
        .map(([key, text]) => [key, EXTERNAL_SHORTCUT_TEXT[key] || text]);
}

export function resolveInspectOverlayScope({
    origin = "gallery",
    requestedAssetId = "",
    visibleAssets = [],
    sortedProjectAssets = [],
    externalItems = [],
} = {}) {
    const direct = origin === "direct";
    const external = origin === "external";
    const source = external
        ? externalItems.map(externalOverlayAsset)
        : direct
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
        origin: external ? "external" : direct ? "direct" : "gallery",
        assets,
        asset,
    };
}
