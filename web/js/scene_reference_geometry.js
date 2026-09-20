// @server-mirror server/routes.py::_reference_item_bounds
// @server-mirror server/routes.py::_canonical_reference_member_refs
// @server-mirror server/routes.py::_apply_create_reference_item
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
// Bounds and member arithmetic for an optimistic Reference staging paint.
//
// Leaf module: DOM-free, host-free, no editor imports — the same shape and the
// same reason as `scene_move_geometry.js` and `scene_split_geometry.js`.
// `editor_widget.js` cannot be imported under Node, so a mirror living only
// there could not carry the parity test `agent_workflow.md` requires of an
// intentional mirror. `tests/test_reference_geometry_parity.py` drives both
// sides over one table.
//
// INTENTIONAL MIRROR of `_reference_item_bounds` and of the record
// `_canonical_reference_member_refs` builds (`server/routes.py`).
//
// **Why the member half is mirrored at all**, since the obvious reading is that
// it should not be. Phase C §3 decided this paint must not touch `item.members`
// because lengthening the list would make the next append send an `expected` the
// server never stored, turning a silent success into a 409. Probed against the
// route, the opposite is true in both halves:
//
//   * The canonical record for a Library drop is `{entity_id, member_id}` — the
//     exact shape `dragPayload` already sends (`editor_reference_library.js`) —
//     and `_expected_matches` is `==`, so a painted list IS what the server
//     stores. `canonicalStagedMemberRefs` is what keeps that structural rather
//     than accidental: it builds the record the server would build, from the
//     same inputs, and drops anything else.
//   * NOT painting is what loses a drop. `_appendReferenceMembersWithinGesture`
//     reads `priorMembers` before its await, so during the in-flight window a
//     second drop guards against state the server has already moved past and is
//     refused `identity_mismatch`. The route floor is ~2 s at 6 MB and ~7.8 s at
//     28 MB, so that window is the ordinary case, not a race. Both halves are
//     pinned by tests, but by DIFFERENT ones: the route half by
//     `test_reference_geometry_parity.py`, the gesture half by
//     `test_an_append_reads_its_guard_before_the_paint` in
//     `test_project_mutation_queue.py`. Removing the paint leaves the route
//     tests green, so neither file holds the reversal on its own.
//
// Applicability is deliberately NOT re-derived here, and the exact extent of
// that has to be stated rather than waved at, because an audit disproved the
// blanket version of this paragraph.
//
// For the members a gesture is ADDING, `resolveReferenceDropVerdict`
// (`reference_lane_identity.js`) already decides every refusal
// `_canonical_reference_member_refs` and `_require_no_reference_overlap` can
// raise — unresolvable member, missing asset, duplicate member, incompatible
// population, an occupied span, a collapsed or locked lane — before either
// gesture builds an operation, and `memberPopulationCompatible` already carries
// a JS↔Python parity test. A second copy of those rules here would be a second
// authority over the same decision, which is what §2 rule 5 forbids.
//
// An APPEND is not only about what it adds. `_apply_update_reference_item`
// re-canonicalizes the WHOLE list, priors included, while the drop resolver
// iterates the dragged members alone — so a bar holding a member whose asset
// was trashed since it was staged is refused `asset_not_found` for a member the
// resolver never looked at. The caller therefore re-resolves its priors through
// `canonicalStagedMemberRefs` too, and paints nothing when they no longer
// resolve. That is not a second authority: it is the same lookup the resolver
// runs, applied to the rows the resolver skips.
//
// Two server refusals have no twin here and are deliberate. The lane recipe's
// `hard.max_members` cap is allowed on the route's side as well — the append
// permits the over-cap state and warns. And a recipe whose `physical_population`
// NARROWED after a member was staged refuses that prior member; the paint goes
// ahead and the response corrects it, because refusing locally would need this
// file to re-derive the recipe rules the panel owns.
//
// Member canonicalization itself stays server-authoritative, exactly as §3
// required. What this file mirrors is the record SHAPE for refs the drop path
// already resolved; `entity_id` is re-derived from the owning reference the way
// the route re-derives it, rather than trusted from the payload, so a stale
// payload cannot paint a record the server would correct.

/** The stored bounds `_reference_item_bounds` would produce, or a refusal.
 *
 *  `Math.trunc` is `int()`, per `scene_move_geometry.js`'s note on the same
 *  choice. `-1` is the sentinel for "runs to the end of the scene" and is
 *  preserved rather than resolved, because the stored value is what a later
 *  guard compares and what a scene-duration change is supposed to follow.
 *
 *  Returns `{ startFrame, endFrame, refusal }`. `refusal` is `"invalid_range"`
 *  when the server would answer 409 and `""` otherwise; a caller that paints
 *  through a refusal would be writing a row the project will not accept.
 */
export function referenceItemBounds(durationFrames, start, end) {
    const duration = Math.max(0, Math.trunc(Number(durationFrames) || 0));
    let startFrame = Math.max(0, Math.trunc(Number(start) || 0));
    if (duration > 0) startFrame = Math.min(startFrame, duration - 1);
    let endFrame = Math.trunc(Number(end ?? -1) || -1);
    if (endFrame < 0) {
        endFrame = -1;
    } else if (endFrame <= startFrame) {
        return { startFrame, endFrame, refusal: "invalid_range" };
    } else if (duration > 0) {
        endFrame = Math.min(endFrame, duration);
        if (endFrame <= startFrame) {
            return { startFrame, endFrame, refusal: "invalid_range" };
        }
    }
    return { startFrame, endFrame, refusal: "" };
}

/** The member records `_canonical_reference_member_refs` would store, or null.
 *
 *  `entityIdFor(member_id)` mirrors the route's `by_member_id` lookup: it
 *  returns the id of the reference that OWNS the member, and `""`/null for one
 *  the project does not hold. The route answers 404 in that case; here the
 *  answer is `null`, meaning "paint nothing", because a paint the server will
 *  refuse is worse than no paint at all.
 *
 *  **`visual_intent`, `audio_intent` and `role` make the whole list unpaintable
 *  rather than being carried through, and that is the interesting decision.**
 *  `ReferenceItem.from_dict` stores those three alongside the ids, so copying
 *  them looks faithful — it is not. The route gates each against the lane
 *  recipe's `role_fields`, against the media kind, against
 *  `prompt_context.VISUAL_INTENTS` / `AUDIO_INTENTS`, and runs `role` through
 *  `normalize_reference_role` against the project's prompt-context profiles,
 *  trimming as it goes. A first version of this function copied them verbatim
 *  and diverged from the route in six measured ways, every one of which paints a
 *  record the save would refuse or rewrite.
 *
 *  Refusing is free because nothing reaches THIS function carrying them: both
 *  callers pass Library drag payloads, which are `{entity_id, member_id}` and
 *  nothing else. Mirroring the validation would duplicate recipe and profile
 *  authority this file has no business holding; carrying the fields unvalidated
 *  would paint a lie.
 *
 *  A member the route already stored is a different question with a different
 *  answer — see `canonicalStoredMemberRefs` below, which an append uses for its
 *  priors. Splitting them is what stops a bar whose members carry roles from
 *  quietly losing its optimistic paint.
 */
export const PAINTABLE_MEMBER_FIELDS = Object.freeze(["entity_id", "member_id"]);

/** The fields `ReferenceItem.from_dict` stores beyond the two ids. */
const STORED_MEMBER_FIELDS = Object.freeze(
    ["visual_intent", "audio_intent", "role"]);

function canonicalMemberRefs(rawMembers, entityIdFor, carryStoredFields) {
    if (!Array.isArray(rawMembers) || !rawMembers.length) return null;
    const resolve = typeof entityIdFor === "function" ? entityIdFor : () => "";
    const out = [];
    const seen = new Set();
    const allowed = carryStoredFields
        ? [...PAINTABLE_MEMBER_FIELDS, ...STORED_MEMBER_FIELDS]
        : PAINTABLE_MEMBER_FIELDS;
    for (const raw of rawMembers) {
        if (!raw || typeof raw !== "object") return null;
        const memberId = String(raw.member_id || "");
        if (!memberId || seen.has(memberId)) return null;
        // Any key this file cannot reproduce faithfully makes the list
        // unpaintable, including one nobody has added yet: a member record that
        // grows a further stored field would otherwise be painted without it and
        // diverge silently, which is precisely the failure `_pushUndo` turns
        // durable.
        for (const key of Object.keys(raw)) {
            if (!allowed.includes(key)) return null;
        }
        const entityId = String(resolve(memberId) || "");
        if (!entityId) return null;
        seen.add(memberId);
        const record = { entity_id: entityId, member_id: memberId };
        if (carryStoredFields) {
            for (const field of STORED_MEMBER_FIELDS) {
                if (field in raw) record[field] = raw[field];
            }
        }
        out.push(record);
    }
    return out;
}

export function canonicalStagedMemberRefs(rawMembers, entityIdFor) {
    return canonicalMemberRefs(rawMembers, entityIdFor, false);
}

/** The same record for members this client READ BACK from the route.
 *
 *  Separated from the staged form rather than given a flag, because the two
 *  differ on a question that has one right answer per call site and no sensible
 *  default: may a stored `visual_intent` / `audio_intent` / `role` be carried?
 *
 *  For a record the route returned, yes, and measurably so.
 *  `_apply_update_reference_item` passes `legacy_members=item.members`, and
 *  `_canonical_reference_member_refs` suppresses every intent and role check
 *  whose value is `unchanged` against that legacy record. Probed against the
 *  route: a stored member round-trips **byte-identically** through an append,
 *  including under a recipe that has since stopped exposing the field. So
 *  copying it is not a guess — it is the route's own answer.
 *
 *  For a record the AUTHOR just produced there is no legacy entry to be
 *  unchanged against, every check runs, and this file cannot mirror them without
 *  duplicating recipe and profile authority. Hence the two names.
 *
 *  Two prior-member refusals are deliberately NOT caught here and arrive from
 *  the response instead: `member_population_compatible` and the asset lookup run
 *  for every member regardless of `legacy_members`, so a lane whose population
 *  narrowed after staging refuses a prior. The asset half IS caught, because an
 *  unresolvable member fails `entityIdFor`.
 */
export function canonicalStoredMemberRefs(rawMembers, entityIdFor) {
    return canonicalMemberRefs(rawMembers, entityIdFor, true);
}

/** The row `_apply_create_reference_item` would append, or null on a refusal.
 *
 *  The defaults are the route's defaults, not the caller's: a field the staging
 *  gesture does not send still has to be painted as the value the server will
 *  store, or the bar the author sees differs from the bar the project holds the
 *  moment anything reads it — including `_pushUndo`, which snapshots
 *  `activeScene` verbatim and can write that snapshot back to disk.
 */
export function stagedReferenceItem({
    referenceItemId = "",
    laneIndex = 0,
    startFrame = 0,
    endFrame = -1,
    durationFrames = 0,
    members = null,
} = {}) {
    if (!referenceItemId || !Array.isArray(members) || !members.length) return null;
    const bounds = referenceItemBounds(durationFrames, startFrame, endFrame);
    if (bounds.refusal) return null;
    return {
        reference_item_id: String(referenceItemId),
        lane_index: Math.max(0, Math.trunc(Number(laneIndex) || 0)),
        start_frame: bounds.startFrame,
        end_frame: bounds.endFrame,
        members: members.map((member) => ({ ...member })),
        prompt_override: "",
        strength: 1.0,
        sequence_frames: 0,
        muted: false,
    };
}
