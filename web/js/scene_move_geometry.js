// Bounds arithmetic for an optimistic timeline move, mirroring the server.
//
// Leaf module: DOM-free, host-free, no editor imports — the same shape and the
// same reason as `scene_split_geometry.js`. `editor_widget.js` cannot be
// imported under Node, so a mirror living only there could not carry the parity
// test `agent_workflow.md` requires of an intentional mirror.
//
// INTENTIONAL MIRROR of the `timeline_start_frame`-without-`timeline_end_frame`
// branch of `_apply_update_clip` / `_apply_update_audio_track`, and of the
// `pure_move` arm of `_apply_linked_bounds_update` (`server/routes.py`).
// `tests/test_move_geometry_parity.py` drives both sides over one table.
//
// Scope, stated because the branch is easy to mistake for the whole handler:
// this owns ONLY the case where a caller sends a new start and no new end. The
// drag-commit path sends both bounds explicitly and never reaches it; the item
// editor's Apply and Enter send only the start, which is the whole point —
// the server preserves the duration, and a client that recomputed the end from
// the input box would be a second answer to the same question.
//
// Two kinds of server check, and they are treated differently on purpose.
//
// A check that can REFUSE THE WHOLE MOVE is mirrored, by `linkedMoveRefusal`
// below, because the honest optimistic answer to a move the server will reject
// is to paint nothing. `_require_lane_unlocked` is the exception: the lock is
// already read before the gesture starts.
//
// A check the operation never asks for is NOT mirrored, and naming it here
// would be worse than silence. `_require_media_target_bounds_fit` runs only
// under `validate_lane_collision`, and the only emitters of that flag are the
// two trim-commit operations — a move sent from the item editor does not carry
// it, so the server accepts a move that lands on top of another item and the
// paint agrees with it. That asymmetry is pre-existing and deliberate here:
// making the paint stricter than the server would refuse a move the project
// would have accepted, which is the opposite failure.

/** The duration-preserving start shift.
 *
 *  `Math.max(0, Math.trunc(...))` is `max(0, int(x))`. `Math.trunc` is used
 *  because it is what `int()` MEANS, not because anything can currently tell
 *  the two apart: `Math.floor` differs only on a negative input, and a negative
 *  is clamped to zero either way. Injecting `Math.floor` here was tried against
 *  `tests/test_move_geometry_parity.py` and passed, so the parity test does not
 *  protect this choice and no fixture can be written that would. Stated rather
 *  than left implied, because the clamp is what makes them equivalent -- a
 *  future change that removes or widens it re-opens the difference, and this is
 *  the note that should stop it being reintroduced by accident.
 */
export function movedMediaBounds(startFrame, endFrame, requestedStart) {
    const from = Number(startFrame) || 0;
    const to = Number(endFrame) || 0;
    const nextStart = Math.max(0, Math.trunc(Number(requestedStart) || 0));
    return { start: nextStart, end: nextStart + (to - from) };
}

/** One linked member's bounds under a pure move.
 *
 *  Pure, in the server's sense: the anchor's start and end moved by the same
 *  amount, which is what sending a start with no end guarantees, so every
 *  member takes the same delta and NOTHING re-derives a source window.
 *  `_apply_ref_bounds` adjusts `source_in_frame` / `source_out_frame` only when
 *  the move is *not* pure — a trim — so a mirror that touched them here would
 *  quietly re-trim every linked member of an ordinary move.
 */
export function movedMemberBounds(oldStart, oldEnd, delta) {
    return { start: (Number(oldStart) || 0) + delta, end: (Number(oldEnd) || 0) + delta };
}

/** Why the server would refuse this whole linked move, or "" if it would not.
 *
 *  A pure move is refused as a WHOLE -- `_apply_linked_bounds_update` validates
 *  every target before writing any, and `_apply_ref_bounds` raises on the first
 *  member it cannot place. So the honest optimistic answer to any of these is to
 *  paint nothing and let the refusal speak, rather than to draw a row the
 *  project will never hold.
 *
 *  That matters more than a flicker. A painted row that the server rejects is
 *  still what a later gesture's `_pushUndo` snapshots, and Undo sends that
 *  snapshot back as its merge target. Worse for two of these: the scene-window
 *  checks are deliberately ABSENT from `_scene_history_content_violations`
 *  (ordinary writers may produce such states, so differential validation must
 *  not resurrect them), which means an out-of-scene guide or prompt would be
 *  written to disk rather than refused; while a prompt OVERLAP is present
 *  there, so painting one produces an undo entry the server then refuses to
 *  restore. Silent corruption in one direction, an unreachable Undo in the
 *  other.
 *
 *  Mirrors, in order: `_prompt_range_violations`, `_prompt_target_bounds_violations`,
 *  `_prompt_target_overlap_violations`, and the guide arm of `_apply_ref_bounds`.
 *  `sceneDuration` is `scene.duration_frames`; a zero or absent duration turns
 *  the upper bounds off exactly as `duration > 0` does on the server.
 */
export function linkedMoveRefusal(moves, { sceneDuration = 0, otherPromptRanges = [] } = {}) {
    const duration = Number(sceneDuration) || 0;
    for (const move of moves) {
        if (move.start < 0) return "before the start of the scene";
        if (move.type === "guide") {
            // A guide is one frame, and the server compares that frame -- not a
            // derived end -- against the duration.
            if (duration > 0 && move.start >= duration) return "past the end of the scene";
            continue;
        }
        if (move.type === "prompt") {
            if (move.end <= move.start) return "an empty prompt section";
            if (duration > 0 && move.end > duration) return "past the end of the scene";
        }
    }
    // `left_start < right_end && left_end > right_start`, the server's own
    // half-open test, against sections the move does NOT carry.
    for (const move of moves) {
        if (move.type !== "prompt") continue;
        for (const other of otherPromptRanges) {
            if (move.start < other[1] && move.end > other[0]) {
                return "onto another prompt section";
            }
        }
    }
    return "";
}


/** Where a linked member's bounds live, per type.
 *
 *  Mirrors `_item_bounds` / `_apply_ref_bounds`. A guide is a single frame that
 *  the server reads as `[idx, idx + 1]` and writes back as `frame_index` alone,
 *  so its end is derived rather than stored — writing an `end_frame` onto a
 *  guide would invent a field the model does not have.
 */
export const MEMBER_BOUND_FIELDS = Object.freeze({
    clip: { start: "timeline_start_frame", end: "timeline_end_frame" },
    audio: { start: "timeline_start_frame", end: "timeline_end_frame" },
    prompt: { start: "start_frame", end: "end_frame" },
    guide: { start: "frame_index", end: null },
});

/** Read one member's bounds the way `_item_bounds` does. */
export function memberBounds(type, data) {
    const fields = MEMBER_BOUND_FIELDS[type];
    if (!fields || !data) return null;
    const start = Number(data[fields.start]) || 0;
    return { start, end: fields.end ? (Number(data[fields.end]) || 0) : start + 1 };
}

/** Write one member's bounds the way `_apply_ref_bounds` does for a pure move. */
export function writeMemberBounds(type, data, start, end) {
    const fields = MEMBER_BOUND_FIELDS[type];
    if (!fields || !data) return false;
    data[fields.start] = start;
    if (fields.end) data[fields.end] = end;
    return true;
}
