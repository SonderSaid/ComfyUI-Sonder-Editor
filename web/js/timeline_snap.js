// Timeline snap targets and the nearest-snap search. Leaf module: DOM-free and
// host-free, so the rules run under Node (editor_widget.js cannot be imported
// there). The editor owns when to snap and what to draw; this owns where.
import { referenceItemEffectiveBounds } from "./scene_reference_geometry.js";

/** A timeline item's snap identity, qualified by type: a prompt's id is its
 *  index and a guide's is its frame, so bare ids of two types can be equal. */
export const snapItemKey = (type, id) => `${type}:${id}`;

/** Every frame an edge can snap to, in priority order for equal distances:
 *  playhead, selection, clip, audio, guide, prompt, Reference, scene bounds.
 *  `exclude` is a Set of `snapItemKey`s whose own edges are left out. */
export function collectSnapCandidates(scene, {
    playhead = null, selectionStart = 0, selectionEnd = 0, totalFrames = 0, targets = {}, exclude = null,
} = {}) {
    const out = [];
    const skip = (type, id) => !!exclude?.has(snapItemKey(type, id));
    if (targets.playhead && Number.isFinite(playhead)) out.push(playhead);
    if (targets.selection && selectionStart < selectionEnd) out.push(selectionStart, selectionEnd);
    if (targets.clipEdges) {
        for (const clip of scene?.clips || []) {
            if (!skip("clip", clip.clip_id)) out.push(clip.timeline_start_frame, clip.timeline_end_frame);
        }
    }
    if (targets.audioEdges) {
        for (const track of scene?.audio_tracks || []) {
            if (!skip("audio", track.track_id)) out.push(track.timeline_start_frame, track.timeline_end_frame);
        }
    }
    if (targets.guides) {
        for (const guide of scene?.guide_frames || []) {
            if (skip("guide", guide.frame_index)) continue;
            out.push(guide.frame_index === -1 ? totalFrames - 1 : guide.frame_index);
        }
    }
    if (targets.promptSections) {
        (scene?.prompt_sections || []).forEach((section, index) => {
            if (!skip("prompt", index)) out.push(section.start_frame, section.end_frame);
        });
    }
    if (targets.referenceItems) {
        for (const item of scene?.reference_items || []) {
            if (skip("reference", item.reference_item_id)) continue;
            const { start, end } = referenceItemEffectiveBounds(item, totalFrames);
            out.push(start, end);
        }
    }
    if (targets.sceneBounds) out.push(0, totalFrames);
    return out.filter(Number.isFinite);
}

/** The closest pairing of a moving point and a candidate within `threshold`
 *  frames, or null. `delta` moves the point onto `frame`. Equal distances keep
 *  the earlier point, then the earlier candidate. A candidate in
 *  `excludeFrames` is never chosen (the snap-back guard, #35). */
export function nearestSnap(points, candidates, threshold, excludeFrames = null) {
    let best = null;
    for (const point of points) {
        if (!Number.isFinite(point)) continue;
        for (const frame of candidates) {
            if (excludeFrames?.has(frame)) continue;
            const distance = Math.abs(point - frame);
            if (distance > threshold || (best && distance >= best.distance)) continue;
            best = { distance, delta: frame - point, frame, point };
        }
    }
    return best;
}

const sideOf = (frame, { start, end }) => (frame <= start ? -1 : frame >= end ? 1 : 0);

/** Where a Razor cut at `frame` may snap: strictly inside the clicked item,
 *  and on the same side of each linked partner as the pointer. A split divides
 *  only the partners the frame falls inside and files the rest by side
 *  (`_planItemSplit`), so a snapped cut divides exactly what the pointer's
 *  would. */
export function razorSnapTargets(candidates, item, partners, frame) {
    return candidates.filter(c => c > item.start && c < item.end
        && partners.every(range => sideOf(c, range) === sideOf(frame, range)));
}
