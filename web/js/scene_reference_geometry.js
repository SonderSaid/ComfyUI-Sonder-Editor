// @server-mirror server/routes.py::_reference_item_bounds
// @server-mirror server/routes.py::_canonical_reference_member_refs
// @server-mirror server/routes.py::_apply_create_reference_item
// @server-mirror server/routes.py::_reference_overlapping_items
// @server-mirror server/routes.py::_apply_update_reference_item
// @server-mirror server/routes.py::_reconcile_staged_reference_members
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
// Bounds and member arithmetic for an optimistic Reference staging paint, the
// lane-overlap decision, the planned row of a staged-item update, and what a
// Library member or Reference delete does to staged items.
//
// Leaf module: DOM-free, host-free, no editor imports — the same shape and the
// same reason as `scene_move_geometry.js` and `scene_split_geometry.js`. Its one
// import, `reference_lane_identity.js`, is a leaf of the same kind and already
// holds the population rule under its own parity test.
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
//   * NOT painting is what loses a drop. An append reads its `expected` guard
//     from the live row before its own paint, so without a paint a second drop
//     during the in-flight window guards against state the server has already
//     moved past and is refused `identity_mismatch`. The route floor is ~2 s at
//     6 MB and ~7.8 s at 28 MB, so that window is the ordinary case, not a race.
//     Both halves are pinned by tests, but by DIFFERENT ones: the route half by
//     `test_reference_geometry_parity.py`, the gesture half by
//     `test_an_append_reads_its_guard_before_the_paint` in
//     `test_project_mutation_queue.py`. Removing the paint leaves the route
//     tests green, so neither file holds the reversal on its own.
//
// For STAGING (`stagedReferenceItem`, `canonicalStagedMemberRefs`)
// applicability is deliberately NOT re-derived here, and the exact extent of
// that has to be stated rather than waved at, because an audit disproved the
// blanket version of this paragraph. The UPDATE planner at the end of this file
// takes the opposite stance for a stated reason; see
// `plannedReferenceItemUpdate`.
//
// For the members a staging gesture is ADDING, `resolveReferenceDropVerdict`
// (`reference_lane_identity.js`) already decides every refusal
// `_canonical_reference_member_refs` and `_require_no_reference_overlap` can
// raise — unresolvable member, missing asset, duplicate member, incompatible
// population, an occupied span, a collapsed or locked lane — before either
// gesture builds an operation, and `memberPopulationCompatible` already carries
// a JS↔Python parity test. A second copy of those rules here would be a second
// authority over the same decision, which is what §2 rule 5 forbids.
//
// An APPEND is an update naming `members`, and is planned as one
// (`plannedReferenceItemUpdate`): `_apply_update_reference_item`
// re-canonicalizes the WHOLE list, priors included, while the drop resolver
// iterates the dragged members alone. So a bar holding a member whose asset was
// trashed, whose lane population narrowed, or whose stored role the route would
// trim, is refused for a member the resolver never looked at -- and the append
// is sent unpainted, letting the response speak.
//
// One server refusal has no twin anywhere here, deliberately: the lane recipe's
// `hard.max_members` cap is allowed on the route's side as well -- the append
// permits the over-cap state and warns.
//
// Member canonicalization itself stays server-authoritative, exactly as §3
// required. What this file mirrors is the record SHAPE for refs the drop path
// already resolved; `entity_id` is re-derived from the owning reference the way
// the route re-derives it, rather than trusted from the payload, so a stale
// payload cannot paint a record the server would correct.

import {
    laneStagingPopulation,
    memberPopulationCompatible,
} from "./reference_lane_identity.js";

/** The stored bounds `_reference_item_bounds` would produce, or a refusal.
 *
 *  `Math.trunc` is `int()`, per `scene_move_geometry.js`'s note on the same
 *  choice. `-1` is the sentinel for "runs to the end of the scene" and is
 *  preserved rather than resolved, because the stored value is what a later
 *  guard compares and what a scene-duration change is supposed to follow.
 *
 *  The inputs are numbers as the request will carry them, and a non-finite
 *  number is `null` on the wire (`JSON.stringify`), which the route reads as
 *  the field's default: `0` for the start, `-1` for the end. An end of `0` is
 *  NOT the sentinel -- the route refuses it as an inverted range, and an
 *  earlier `|| -1` here painted it as "runs to scene end".
 *
 *  Returns `{ startFrame, endFrame, refusal }`. `refusal` is `"invalid_range"`
 *  when the server would answer 409 and `""` otherwise; a caller that paints
 *  through a refusal would be writing a row the project will not accept.
 */
export function referenceItemBounds(durationFrames, start, end) {
    const duration = Math.max(0, Math.trunc(Number(durationFrames) || 0));
    const requestedStart = Number(start);
    let startFrame = Math.max(0, Number.isFinite(requestedStart) ? Math.trunc(requestedStart) : 0);
    if (duration > 0) startFrame = Math.min(startFrame, duration - 1);
    const requestedEnd = Number(end ?? -1);
    let endFrame = Number.isFinite(requestedEnd) ? Math.trunc(requestedEnd) : -1;
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
 *  Refusing is free because nothing reaches THIS function carrying them: its
 *  callers pass Library drag payloads, which are `{entity_id, member_id}` and
 *  nothing else. Mirroring the validation would duplicate recipe and profile
 *  authority this file has no business holding; carrying the fields unvalidated
 *  would paint a lie.
 *
 *  A member the route already stored is a different question with a different
 *  answer -- see `updatedMemberRefs` below, which the update planner (and so an
 *  append) uses: a stored value `unchanged` against the item's own record is
 *  carried, because the route's `legacy_members` leniency returns it as is.
 */
export const PAINTABLE_MEMBER_FIELDS = Object.freeze(["entity_id", "member_id"]);

/** The fields `ReferenceItem.from_dict` stores beyond the two ids. */
const STORED_MEMBER_FIELDS = Object.freeze(
    ["visual_intent", "audio_intent", "role"]);

export function canonicalStagedMemberRefs(rawMembers, entityIdFor) {
    if (!Array.isArray(rawMembers) || !rawMembers.length) return null;
    const resolve = typeof entityIdFor === "function" ? entityIdFor : () => "";
    const out = [];
    const seen = new Set();
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
            if (!PAINTABLE_MEMBER_FIELDS.includes(key)) return null;
        }
        const entityId = String(resolve(memberId) || "");
        if (!entityId) return null;
        seen.add(memberId);
        out.push({ entity_id: entityId, member_id: memberId });
    }
    return out;
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

/** The first row on `laneIndex` a range would overlap, or null.
 *
 *  Mirrors `_reference_overlapping_items` with `_reference_effective_bounds`,
 *  including both clamps: a requested end at or before the start is widened to
 *  one frame, and a stored `-1` resolves to `max(start + 1, duration)`. The
 *  range is expected to have been through `referenceItemBounds` already, as it
 *  has on the route by the time the overlap check runs.
 *
 *  The route's `ignore=item` is an OBJECT identity, so pass the live row as
 *  `ignore` where there is one. `ignoreId` is the fallback for a caller holding
 *  a copy; the two differ only for rows sharing an id (or with an empty one),
 *  which load repair and the client-minted id contract both rule out.
 */
export function referenceItemOverlap(items, {
    laneIndex = 0,
    startFrame = 0,
    endFrame = -1,
    durationFrames = 0,
    ignore: ignoreRow = null,
    ignoreId = "",
} = {}) {
    const duration = Math.trunc(Number(durationFrames) || 0);
    const lane = Math.trunc(Number(laneIndex) || 0);
    const start = Math.trunc(Number(startFrame) || 0);
    const end = Math.trunc(Number(endFrame ?? -1));
    let resolvedEnd = end < 0 ? Math.max(0, duration) : end;
    if (resolvedEnd <= start) resolvedEnd = start + 1;
    const ignore = String(ignoreId || "");
    for (const other of Array.isArray(items) ? items : []) {
        if (!other || typeof other !== "object" || other === ignoreRow) continue;
        if (ignore && String(other.reference_item_id || "") === ignore) continue;
        if (Math.trunc(Number(other.lane_index) || 0) !== lane) continue;
        const otherStart = Math.trunc(Number(other.start_frame) || 0);
        const storedEnd = Math.trunc(Number(other.end_frame ?? -1));
        const otherEnd = storedEnd < 0 ? Math.max(otherStart + 1, duration) : storedEnd;
        if (start < otherEnd && resolvedEnd > otherStart) return other;
    }
    return null;
}

/** JSON with every object's keys sorted: the comparison Python's `==` makes.
 *
 *  A stored member record's key ORDER is whatever the stored file holds --
 *  `_overlay_unknown_record` keeps a record's own key order on load, while the
 *  route builds new records in field order -- and `==` on dicts ignores it. A
 *  comparison by plain `JSON.stringify` does not, and on a real project read
 *  every item carrying a stored role as changed.
 */
export function canonicalJson(value) {
    const sorted = (entry) => {
        if (Array.isArray(entry)) return entry.map(sorted);
        if (!entry || typeof entry !== "object") return entry;
        return Object.fromEntries(Object.keys(entry).sort().map((key) => [key, sorted(entry[key])]));
    };
    return JSON.stringify(sorted(value));
}

/** `_REFERENCE_ITEM_FIELDS`: what an update may name. Exported so the parity
 *  suite can hold it equal to the route's set; a field the route gains and this
 *  lacks would be refused locally, and the edit lost. */
export const REFERENCE_ITEM_FIELDS = Object.freeze([
    "lane_index", "start_frame", "end_frame", "members", "prompt_override",
    "strength", "sequence_frames", "muted",
]);

const REQUEST_NUMBER = (value) => typeof value === "number" || typeof value === "boolean";

/** The members `_canonical_reference_member_refs` would store for an update.
 *
 *  The route passes the item's CURRENT members as `legacy_members`, and every
 *  intent and role check is skipped for a value `unchanged` against that
 *  record -- so a stored value is carried and an authored one is validated
 *  against recipe and prompt-profile authority this file does not hold. The
 *  answer is therefore one of three:
 *
 *   * `{ refusal }` -- the route refuses whatever the client's Library says:
 *     no members, a member that is not an object, or one named twice. The
 *     route resolves each member before its duplicate check, so for an
 *     unresolvable member named twice it answers `item_not_found` rather than
 *     this code; it refuses either way, which is what a local refusal needs;
 *   * `{ members }` -- every member round-trips: it resolves to its owning
 *     reference (the route re-derives `entity_id`), its asset exists, the lane
 *     population accepts it, and every intent or role it carries is unchanged;
 *   * `{ paintable: false }` -- anything else. That includes an authored role
 *     or intent change, a stored role with surrounding whitespace (the route
 *     compares the TRIMMED value with the stored one, so it is not "unchanged"),
 *     and a member the client's Library cannot resolve. The write is still
 *     sent; the response says what the route made of it.
 *
 *  Keys the route never reads -- `order` from `moveMember`, say -- are ignored
 *  here as they are there.
 */
function updatedMemberRefs(rawMembers, legacyMembers, { entityIdFor, laneRecipe, assetFor }) {
    if (!Array.isArray(rawMembers) || !rawMembers.length) {
        return { refusal: "invalid_reference_item" };
    }
    const legacyById = new Map();
    for (const legacy of Array.isArray(legacyMembers) ? legacyMembers : []) {
        if (legacy && typeof legacy === "object") {
            legacyById.set(String(legacy.member_id || ""), legacy);
        }
    }
    const resolveEntity = typeof entityIdFor === "function" ? entityIdFor : () => "";
    const resolveAsset = typeof assetFor === "function" ? assetFor : () => null;
    // `ReferenceLaneRecipe()`'s default, which the route pads a missing recipe
    // slot with -- an empty kind here would read as an audio lane.
    const mediaKind = String(laneRecipe?.media_kind || "image");
    const population = laneStagingPopulation(laneRecipe);
    const members = [];
    const seen = new Set();
    let paintable = true;
    for (const raw of rawMembers) {
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
            return { refusal: "invalid_reference_item" };
        }
        const memberId = String(raw.member_id || "");
        if (seen.has(memberId)) return { refusal: "invalid_reference_item" };
        seen.add(memberId);
        const entityId = String(resolveEntity(memberId) || "");
        const asset = entityId ? resolveAsset(memberId) : null;
        if (!entityId || !asset || !memberPopulationCompatible(
            population, mediaKind, String(asset.asset_type || ""),
            { hasAudio: !!asset.has_audio })) {
            paintable = false;
            continue;
        }
        const legacy = legacyById.get(memberId) || {};
        const record = { entity_id: entityId, member_id: memberId };
        for (const field of STORED_MEMBER_FIELDS) {
            const value = field === "role"
                ? String(raw.role || "").trim() : String(raw[field] || "");
            if (!value) continue;
            if (String(legacy[field] || "") !== value) paintable = false;
            record[field] = value;
        }
        members.push(record);
    }
    return paintable ? { members } : { paintable: false };
}

/** The row `_apply_update_reference_item` would leave, a refusal, or neither.
 *
 *  Returns `{ painted, refusal, paintable }`:
 *
 *   * `refusal` names the route's error code when the route refuses whatever
 *     the client's Library says -- a caller refuses locally and sends nothing;
 *   * `paintable: true` with `painted`, the whole row as `ReferenceItem.to_dict`
 *     would store it -- a caller paints it;
 *   * `paintable: false` with no refusal -- the route's answer depends on
 *     authority this file does not mirror, so the caller sends the write
 *     unpainted and lets the response speak.
 *
 *  **Every update re-canonicalizes every member**, a scalar one included,
 *  because the route calls `_canonical_reference_member_refs` with the stored
 *  list whenever `members` is absent. So a strength change on an item whose
 *  members no longer round-trip is unpaintable too: the route would rewrite or
 *  refuse those members, and a paint that kept them would be a row the project
 *  never holds (`durable_rules.md`: an optimistic apply reproduces the server's
 *  applicability test).
 *
 *  **Why this planner re-derives applicability when staging does not.** The
 *  staging path above leaves member applicability to the drop resolver (or
 *  the panel stage's own population check), which has already decided it for
 *  the members being added. No resolver runs before a panel field edit, or
 *  before an append's priors, and the route re-validates the
 *  whole stored list on every update, so the lookups the route runs regardless
 *  of `legacy_members` -- member resolution, asset presence, the lane
 *  population -- are repeated here. The population rule is imported from its
 *  one JS home, not re-stated. Recipe and prompt-profile validation of an
 *  AUTHORED role or intent is still not mirrored; such a write declines.
 *
 *  **A paint changes only the fields the write names.** A rollback restores
 *  the fields it wrote, one chain per field, so a paint that also moved an
 *  unnamed field -- a stale `entity_id` the route re-derives on a strength
 *  write, a stored start past the scene end the route clamps -- would leave
 *  that change behind on a refusal, and the next edit's guard would describe a
 *  row the server does not hold. The route's answer is still exact in those
 *  cases, so the write declines and its response brings the rewrite in.
 *
 *  The `expected` guard and the lane lock are the caller's. The guard is read
 *  from the same live row and matches it by construction; the lock is a lane
 *  property the caller refuses on before it gets here. `laneCount` lets the
 *  planner decline an item stranded on a lane the route no longer counts
 *  (the route answers `item_not_found`); without it that check is the caller's.
 *
 *  Inputs are values as a JSON request carries them. A non-finite number is
 *  `null` on the wire, which the route refuses for `strength` and
 *  `sequence_frames` and reads as the default for a bound. A string where a
 *  number belongs, or a non-string prompt override, is not something the panel
 *  sends; this declines to paint it rather than guessing how Python's `int()`,
 *  `float()` or `str()` would read it. `lane_index` is declined the same way:
 *  moving an item between lanes re-derives the recipe and both locks.
 */
export function plannedReferenceItemUpdate(item, fields, {
    durationFrames = 0,
    laneItems = [],
    entityIdFor = null,
    laneRecipe = null,
    assetFor = null,
    laneCount = null,
} = {}) {
    const refuse = (refusal) => ({ painted: null, refusal, paintable: false });
    const decline = () => ({ painted: null, refusal: "", paintable: false });
    if (!item || typeof item !== "object") return refuse("item_not_found");
    if (!fields || typeof fields !== "object" || Array.isArray(fields)) {
        return refuse("invalid_project_mutation");
    }
    // An `undefined` value is dropped by `JSON.stringify`, so the route never
    // sees that key: judge the fields the request will actually carry.
    const keys = Object.keys(fields).filter((key) => fields[key] !== undefined);
    if (!keys.length || keys.some((key) => !REFERENCE_ITEM_FIELDS.includes(key))) {
        return refuse("invalid_project_mutation");
    }
    const has = (key) => keys.includes(key);
    if (has("lane_index")) return decline();
    if (laneCount !== null && Math.trunc(Number(item.lane_index) || 0) >= Number(laneCount)) {
        return decline();
    }
    for (const key of ["start_frame", "end_frame", "strength", "sequence_frames"]) {
        if (has(key) && fields[key] !== null && !REQUEST_NUMBER(fields[key])) return decline();
    }
    if (has("prompt_override") && fields.prompt_override !== null
            && typeof fields.prompt_override !== "string") return decline();
    if (has("muted") && fields.muted !== null && typeof fields.muted === "object") return decline();

    const bounds = referenceItemBounds(
        durationFrames,
        has("start_frame") ? fields.start_frame : item.start_frame,
        has("end_frame") ? fields.end_frame : item.end_frame);
    if (bounds.refusal) return refuse(bounds.refusal);

    const members = updatedMemberRefs(
        has("members") ? fields.members : item.members, item.members,
        { entityIdFor, laneRecipe, assetFor });
    if (members.refusal) return refuse(members.refusal);

    if (referenceItemOverlap(laneItems, {
        laneIndex: item.lane_index,
        startFrame: bounds.startFrame,
        endFrame: bounds.endFrame,
        durationFrames,
        ignore: item,
        ignoreId: item.reference_item_id,
    })) return refuse("lane_collision");

    const painted = {
        reference_item_id: String(item.reference_item_id || ""),
        lane_index: Math.trunc(Number(item.lane_index) || 0),
        start_frame: bounds.startFrame,
        end_frame: bounds.endFrame,
        members: members.members || [],
        prompt_override: String(item.prompt_override || ""),
        strength: Number(item.strength ?? 1.0),
        sequence_frames: Math.trunc(Number(item.sequence_frames) || 0),
        muted: !!item.muted,
    };
    if (has("prompt_override")) painted.prompt_override = String(fields.prompt_override || "");
    if (has("strength")) {
        const strength = Number(fields.strength);
        if (fields.strength === null || !Number.isFinite(strength)) {
            return refuse("invalid_project_mutation");
        }
        painted.strength = Math.min(1, Math.max(0, strength));
    }
    if (has("sequence_frames")) {
        const frames = Number(fields.sequence_frames);
        if (fields.sequence_frames === null || !Number.isFinite(frames)) {
            return refuse("invalid_project_mutation");
        }
        painted.sequence_frames = Math.max(0, Math.min(4096, Math.trunc(frames)));
    }
    if (has("muted")) painted.muted = !!fields.muted;
    if (!members.members) return decline();
    const same = (left, right) => canonicalJson(left) === canonicalJson(right);
    if (Object.keys(painted).some((key) => !has(key) && !same(painted[key], item[key]))) {
        return decline();
    }
    return { painted, refusal: "", paintable: true };
}

// -- a Library delete's staged-item cascade ---------------------------------------

/** The route's `{str(value) for value in removed_member_ids}`. */
function removedMemberIdSet(removedMemberIds) {
    return new Set([...(removedMemberIds || [])].map((value) => String(value)));
}

/** What a Library member or Reference delete does to one staged row.
 *
 *  `_reconcile_staged_reference_members` drops every staged member whose
 *  `member_id` the delete removed, deletes a row left with none, and keeps the
 *  survivors' records exactly as stored and in order. Nothing else on the row
 *  changes. A member staged twice leaves twice.
 *
 *  The comparison is on the raw stored value, as Python's
 *  `member.get("member_id") not in removed_member_ids` makes it: removed ids
 *  are strings, so a missing or non-string stored id never matches.
 *
 *  Returns `null` when the row is untouched, `{ removed: true }` when it is
 *  deleted, and `{ removed: false, members }` -- a NEW array -- when it is
 *  thinned. A caller paints `members` as a replacement array, because an
 *  acknowledged-value chain keeps `row.members` by reference and tells its own
 *  paint from another writer's by identity.
 */
export function referenceRowAfterMemberRemoval(row, removedMemberIds) {
    const removed = removedMemberIdSet(removedMemberIds);
    const members = Array.isArray(row?.members) ? row.members : [];
    const kept = members.filter((member) => !removed.has(member?.member_id));
    if (kept.length === members.length) return null;
    return kept.length ? { removed: false, members: kept } : { removed: true };
}

/** The staged-item half of a Library delete on one scene, as the route
 *  decides it: `{ removed: [itemId], thinned: [{ itemId, members }] }`, in
 *  row order. Staged items only -- prompt-identity source pruning is
 *  project-level and not the timeline's.
 *
 *  Built only from `referenceRowAfterMemberRemoval`, which is what the editor
 *  applies, row by row, because it splices rows in place and replaces
 *  `members` arrays. This scene-level form is what the parity table compares
 *  with the route, so parity holds per row; the editor's loop adds nothing
 *  but that iteration. */
export function plannedReferenceMemberCascade(scene, removedMemberIds) {
    const removedIds = removedMemberIdSet(removedMemberIds);
    const plan = { removed: [], thinned: [] };
    for (const row of Array.isArray(scene?.reference_items) ? scene.reference_items : []) {
        const after = referenceRowAfterMemberRemoval(row, removedIds);
        if (!after) continue;
        if (after.removed) plan.removed.push(row?.reference_item_id);
        else plan.thinned.push({ itemId: row?.reference_item_id, members: after.members });
    }
    return plan;
}
