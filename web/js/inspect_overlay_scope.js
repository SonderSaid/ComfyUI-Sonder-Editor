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
