// Pure display/parity mirror of server/guide_collision.py. Backend queue
// prediction remains authoritative for user warnings.

function asInt(value, fallback = 0) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : fallback;
}

export function snapDriverStart(idx, step, offset) {
    idx = asInt(idx);
    step = Math.max(1, asInt(step, 1));
    offset = asInt(offset);
    if (idx <= 0) return 0;
    return Math.max(0, offset + Math.floor((idx - offset) / step) * step);
}

export function driverOccupiedCoords(localIdx, pixelLen, step, offset) {
    localIdx = asInt(localIdx);
    pixelLen = asInt(pixelLen);
    step = Math.max(1, asInt(step, 1));
    offset = asInt(offset);
    if (pixelLen <= 1 || step <= 1) return [localIdx];
    const start = snapDriverStart(localIdx, step, offset);
    if (start === 0) {
        const count = Math.max(0, Math.ceil((pixelLen - 1) / step));
        return [0, ...Array.from({ length: count }, (_, k) => offset + step * k)];
    }
    const count = Math.max(1, Math.ceil(pixelLen / step));
    return Array.from({ length: count }, (_, k) => start + step * k);
}

export function resolveGuideCollisions({
    guides = [], drivers = [], frame_count = 0, frame_constraint = null,
    auto_offset_enabled = true,
} = {}) {
    const step = Math.max(1, asInt(frame_constraint?.step, 1));
    const offset = asInt(frame_constraint?.offset, 0);
    frame_count = Math.max(0, asInt(frame_count));
    if (!frame_constraint || step <= 1 || frame_count <= 0) {
        return {
            driver_coords: [],
            entries: guides.map((guide, index) => ({
                guide_id: String(guide?.guide_id || `legacy-guide-${index}`),
                bridge_override_key: String(guide?.bridge_override_key || ""),
                original_local_idx: asInt(guide?.local_idx),
                effective_local_idx: asInt(guide?.local_idx),
                collided: false,
                collided_with: "",
            })),
            collision_count: 0,
            driver_driver_collision_count: 0,
            unresolved_collision_count: 0,
            predicted_unresolved: false,
            max_excess_latents: 0,
        };
    }

    const effectiveOccupied = new Map();
    const originalCounts = new Map();
    const driver_coords = [];
    let driverDriver = 0;
    [...drivers].sort((a, b) =>
        asInt(a?.local_idx) - asInt(b?.local_idx)
        || asInt(a?.lane_index) - asInt(b?.lane_index)
        || String(a?.clip_id || "").localeCompare(String(b?.clip_id || ""))
    ).forEach((driver, index) => {
        const clipId = String(driver?.clip_id || `driver-${index}`);
        const coords = driverOccupiedCoords(driver?.local_idx, driver?.pixel_len, step, offset);
        for (const coord of coords) {
            if ((originalCounts.get(coord) || 0) > 0) driverDriver += 1;
            originalCounts.set(coord, (originalCounts.get(coord) || 0) + 1);
            if (!effectiveOccupied.has(coord)) effectiveOccupied.set(coord, `driver:${clipId}`);
        }
        driver_coords.push({
            clip_id: clipId,
            lane_index: asInt(driver?.lane_index),
            local_idx: asInt(driver?.local_idx),
            snapped_start: snapDriverStart(driver?.local_idx, step, offset),
            pixel_len: asInt(driver?.pixel_len),
            coords: [...new Set(coords)].sort((a, b) => a - b),
        });
    });

    const seen = new Set();
    const normalized = guides.map((guide, index) => {
        let guideId = String(guide?.guide_id || `legacy-guide-${index}`);
        if (seen.has(guideId)) guideId = `${guideId}#${index}`;
        seen.add(guideId);
        return { ...guide, guide_id: guideId };
    }).sort((a, b) => asInt(a.local_idx) - asInt(b.local_idx) || a.guide_id.localeCompare(b.guide_id));

    let originalGuideCollisions = 0;
    const entries = [];
    for (const guide of normalized) {
        const original = asInt(guide.local_idx);
        if ((originalCounts.get(original) || 0) > 0) originalGuideCollisions += 1;
        originalCounts.set(original, (originalCounts.get(original) || 0) + 1);
        let effective = original;
        const collidedWith = effectiveOccupied.get(effective) || "";
        if (collidedWith) {
            let candidate = effective;
            while (effectiveOccupied.has(candidate) && candidate < frame_count) candidate += 1;
            if (candidate >= frame_count) {
                candidate = original - 1;
                while (effectiveOccupied.has(candidate) && candidate >= 0) candidate -= 1;
            }
            if (candidate < 0) {
                if (auto_offset_enabled) throw new Error("No free frame coordinate remains for guide auto-offset");
                candidate = original;
            }
            effective = candidate;
        }
        if (!effectiveOccupied.has(effective)) effectiveOccupied.set(effective, `guide:${guide.guide_id}`);
        entries.push({
            guide_id: guide.guide_id,
            bridge_override_key: String(guide.bridge_override_key || ""),
            original_local_idx: original,
            effective_local_idx: effective,
            collided: !!collidedWith,
            collided_with: collidedWith,
        });
    }
    const collisionCount = entries.filter((entry) => entry.collided).length;
    const unresolved = driverDriver + (auto_offset_enabled ? 0 : originalGuideCollisions);
    return {
        driver_coords,
        entries,
        collision_count: collisionCount,
        driver_driver_collision_count: driverDriver,
        unresolved_collision_count: unresolved,
        predicted_unresolved: unresolved > 0,
        max_excess_latents: unresolved,
    };
}
