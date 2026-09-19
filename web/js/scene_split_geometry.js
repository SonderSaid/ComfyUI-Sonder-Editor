// Half-geometry for an optimistic split, mirroring the server exactly.
//
// Leaf module: DOM-free, host-free, no editor imports — the same shape as
// `scene_mutation_addressing.js` and `scene_mutation_coalescing.js`, and for the
// same reason. `editor_widget.js` cannot be imported under Node, so a mirror
// living only there could not be parity-tested, and `agent_workflow.md` requires
// an intentional mirror to carry one. The host owns the scene, the history entry
// and the network; this owns arithmetic.
//
// INTENTIONAL MIRROR of `_split_clip_object` / `_split_audio_object`
// (`server/routes.py`). `tests/test_split_geometry_parity.py` drives both over
// the same table and compares field for field, so a change here that is not
// made there fails the suite. Read that test before editing either side.
//
// Scope, deliberately narrow: this owns HALF-GEOMETRY ONLY. It does not
// reproduce the server's left/right link partition — overlap-majority, ties
// left, then drop any side with fewer than two members — because that would be
// a second authority over link-group ownership. The right half is not a
// link-group member until the canonical scene response lands, and the surviving
// group stays valid throughout.
//
// The three refusals `_split_clip_object` raises (Driver clip, frame outside the
// clip, frame outside the track) are NOT mirrored: `_planItemSplit` has already
// answered all three against the same state, and duplicating them here would put
// the same decision in two places with nothing keeping them equal. The parity
// test covers the arithmetic; the planner covers the refusals.

/** `data.get(key, default)` in JavaScript.
 *
 *  NOT `??`. `ClipReference.from_dict` uses `data.get(key, default)`, which
 *  substitutes only when the key is ABSENT and preserves a stored `null` --
 *  that is the "deserialize permissively, preserve unknown and over-cap values"
 *  rule in `agent_workflow.md`, and a hand-edited or third-party project file
 *  really can hold one. `??` would substitute the default and the optimistic
 *  half would differ from the half the response brings back. That used to be a
 *  flicker; it is now durable, because a later gesture's `_pushUndo` snapshots
 *  the painted scene and Undo writes that snapshot back as its merge target.
 */
function stored(item, key, fallback) {
    return key in item ? item[key] : fallback;
}

/** Which source frame the cut falls on, in the piece's own source coordinates. */
function sourceSplitFrame(item, splitFrame) {
    return (item.source_in_frame || 0) + (splitFrame - item.timeline_start_frame);
}

/** The right half of a split clip, and the fields the left half takes.
 *
 *  `|| 0` and `|| (end - start)` are not defensive spelling — they reproduce
 *  Python's truthiness exactly. `clip.source_out_frame or (end - start)` in
 *  `_split_clip_object` treats a stored **0** as absent and falls back to the
 *  duration, and a clip placed with no explicit out point really does store 0.
 *  Writing `??` here would diverge from the server on exactly those clips.
 */
export function splitClipGeometry(clip, splitFrame, rightId = "") {
    const sourceSplit = sourceSplitFrame(clip, splitFrame);
    const origSourceOut = clip.source_out_frame
        || (clip.timeline_end_frame - clip.timeline_start_frame);
    const leftSourceIn = clip.source_in_frame || 0;
    return {
        // Every field `ClipReference.to_dict()` emits. A partial record renders
        // wrong for the whole in-flight window -- `takes`, `active_take` and
        // `take_metadata` above all, because a generated clip draws its take.
        right: {
            clip_id: String(rightId || ""),
            source_path: stored(clip, "source_path", ""),
            timeline_start_frame: splitFrame,
            timeline_end_frame: clip.timeline_end_frame,
            source_in_frame: sourceSplit,
            source_out_frame: origSourceOut,
            total_source_frames: origSourceOut - sourceSplit,
            source_origin_frame: sourceSplit,
            opacity: stored(clip, "opacity", 1.0),
            track_index: stored(clip, "track_index", 0),
            role: stored(clip, "role", "render"),
            strength: stored(clip, "strength", 1.0),
            muted: stored(clip, "muted", false),
            fit_mode: stored(clip, "fit_mode", "pad_edge"),
            crop_position: stored(clip, "crop_position", "center"),
            prompt: stored(clip, "prompt", ""),
            is_generated: stored(clip, "is_generated", false),
            generation_params: { ...(clip.generation_params || {}) },
            takes: [...(clip.takes || [])],
            active_take: Math.trunc(Number(clip.active_take || 0)) || 0,
            take_metadata: { ...(clip.take_metadata || {}) },
        },
        // `total_source_frames` becomes this piece's own length, which is NOT
        // the source's length. That double meaning is a tracked defect and is
        // reproduced rather than repaired: a mirror that corrected it would make
        // the optimistic half trim differently from the half the response
        // brings back, which is a worse failure than the one it fixed. Fixing it
        // is a server change with its own migration.
        left: {
            timeline_end_frame: splitFrame,
            source_out_frame: sourceSplit,
            total_source_frames: sourceSplit - leftSourceIn,
            source_origin_frame: leftSourceIn,
        },
    };
}

/** The audio twin. Audio carries no `source_out_frame`, so the arithmetic is
 *  durations rather than source points, exactly as `_split_audio_object` has it.
 */
export function splitAudioGeometry(track, splitFrame, rightId = "") {
    const sourceSplit = sourceSplitFrame(track, splitFrame);
    const origEndFrame = track.timeline_end_frame;
    return {
        right: {
            track_id: String(rightId || ""),
            source_path: stored(track, "source_path", ""),
            timeline_start_frame: splitFrame,
            timeline_end_frame: origEndFrame,
            source_in_frame: sourceSplit,
            total_source_frames: origEndFrame - splitFrame,
            source_origin_frame: sourceSplit,
            volume: stored(track, "volume", 1.0),
            muted: stored(track, "muted", false),
            lane_index: stored(track, "lane_index", 0),
        },
        left: {
            timeline_end_frame: splitFrame,
            total_source_frames: splitFrame - track.timeline_start_frame,
            source_origin_frame: track.source_in_frame || 0,
        },
    };
}

// A client-minted durable id. Eight lowercase hex characters, the same shape
// `uuid4().hex[:8]` gives every server-minted clip and track id, so nothing
// downstream can tell them apart — a grep for a consumer assuming that length
// found none, and matching it keeps that true for free.
//
// Uniqueness is NOT guaranteed here and deliberately is not attempted: the
// client cannot see rows another editor added, so the server refuses a colliding
// id with 409 rather than silently re-minting it the way
// `_apply_create_reference_item` does. Re-minting would hand back an id the
// optimistic half does not carry, and the client would then hold a half the
// server has never heard of. At 32 bits against a scene's worth of clips the
// refusal is vanishingly rare, and a refusal is recoverable where a silent
// substitution is not.
export function mintSplitHalfId(randomHex = null) {
    if (typeof randomHex === "function") return randomHex();
    let out = "";
    // `Math.random()` may return exactly 0, and `(0).toString(16).slice(2)`
    // is the empty string -- an unguarded loop would then never grow and
    // would lock the tab. Bounded, with a deterministic tail.
    for (let attempt = 0; attempt < 8 && out.length < 8; attempt += 1) {
        out += Math.floor(Math.random() * 0xffffffff).toString(16);
    }
    return (out + "00000000").slice(0, 8);
}

/** The wire key a right-half id is filed under: the LEFT ref it comes from.
 *
 *  Keyed by the left ref rather than positionally because under `apply_linked`
 *  several rows split in one operation and each needs its own id, while a member
 *  the client does not mint for — a prompt section, whose right half the server
 *  builds from `clone_for_split` — simply has no entry and stays
 *  server-authoritative.
 */
export function splitHalfIdKey(type, id) {
    return `${type}:${String(id ?? "")}`;
}
