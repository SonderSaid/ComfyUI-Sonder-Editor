import { descriptorForLaneType } from "./lane_registry.js";

/**
 * How each scene mutation names its target, and therefore whether a
 * compare-and-swap conflict on it may be re-sent.
 *
 * `durable_rules.md`: *a mutation's re-application policy is decided by the
 * evidence in the request, and a weaker request can only get the weaker policy.*
 * Two forms of evidence qualify and one disqualifies:
 *
 *   1. **A durable id.** It means the same row in any version, so replaying the
 *      request against a document the caller never read still writes the row the
 *      caller named.
 *   2. **The value snapshot the caller read**, when the server actually compares
 *      it. A positional address plus a compared identity claim cannot silently
 *      hit the wrong row: the replay either resolves to the row the caller meant
 *      or is refused.
 *   3. **A list position, lane index, or a count read from the caller's own
 *      document**, with nothing compared. That means only what the caller's copy
 *      said, so a conflict is refused rather than replayed.
 *
 * A snapshot that constrains no *identifying* field constrains nothing, so it
 * does not buy clause 2 — `expected: { attachments }` names no row.
 *
 * **The server must be the one comparing it.** Umbrella Phase B's central
 * finding is that 23 of 38 dispatch branches ignore any `expected` a client
 * sends, so "the emission carries a guard" is not evidence of anything. Every
 * `promotedBy` entry below names the validator that reads the key, and
 * `tests/test_scene_mutation_retry_policy.py` checks each name against
 * `routes.py` and against `GUARD_CONTRACTS`, the traced record of what each
 * branch validates.
 *
 * What a lost retry costs, stated because the derivation must have a reason in
 * both directions: without retry, `postProjectJsonWithReconcile` rethrows
 * `project_version_conflict` to the gesture's `catch`, which rolls back and
 * toasts — the user's edit is discarded. The conflict is a CAS miss from *any*
 * concurrent writer, including the render worker's out-of-band job-status
 * commits. So "no retry" is the safe direction only where replaying would be
 * wrong; granting it by omission is what this table exists to stop.
 *
 * Deliberately NOT a mutation registry. The decision in
 * `architecture.md#scene-mutation-authoring` retains each policy's ownership;
 * this module declares one property of one policy. The server-side twin — teaching
 * `_apply_scene_mutations_sync` the `addressing="identity"|"positional"` shape
 * `_apply_project_versioned_sync` already has — is the roadmap execution-queue
 * item for bare project writes, and it consumes this table under a parity test
 * written at that landing.
 */

/** The row a request names is the same row in any version. */
export const DURABLE = "durable";
/** The row, or the destination, is named by a position in the caller's own copy. */
export const POSITIONAL = "positional";
/** A whole collection is replaced; no row is named at all. */
export const COLLECTION = "collection";
/** The addressing lives inside `items[]`, one decision per member. */
export const PER_ITEM = "per_item";
/** No row and no document-derived destination is named. */
export const UNADDRESSED = "unaddressed";

const freeze = (value) => Object.freeze(value);

/** `a.b` path read, so an id nested in a payload object can be named. */
function readPath(source, path) {
    let value = source;
    for (const part of String(path).split(".")) {
        if (!value || typeof value !== "object") return undefined;
        value = value[part];
    }
    return value;
}

const isBlank = (value) => value === undefined || value === null
    || (typeof value === "string" && value.trim() === "");

/**
 * One `expected*` bag the server compares, and what must be in it.
 *
 * `identifying` — at least one of these, present and non-blank. A blank id is
 * the "constrains no identifying field" case and degrades.
 * `required` — these keys must be present, whatever their value. Used where an
 * empty string is itself the positive claim (`replaces_guide_id: ""` means "the
 * frame held nothing", compared against every occupant).
 */
const guard = (bag, { identifying = [], required = [], validator = "" } = {}) =>
    freeze({ bag, identifying: freeze(identifying), required: freeze(required), validator });

// Lane-count fields `_apply_scene_fields` can write. `_set_scene_lane_count`
// POPS configs and recipes to reach an absolute count the caller computed from
// its own copy, so one of these in `fields` makes the write document-dependent
// in exactly the way `set_lane_count` is. No emitter sends one today — the
// client uses `set_lane_count` — and this degrade exists so that one starting to
// cannot acquire retry by omission.
// Expiry: delete when `_apply_scene_fields` no longer accepts count fields.
const LANE_COUNT_FIELDS = freeze([
    "video_lane_count", "audio_lane_count",
    "motion_driver_lane_count", "reference_lane_count",
]);

/**
 * A durable row id says WHAT moves. It does not say WHERE it lands.
 *
 * `update_clip`, `update_audio_track` and `update_reference_item` name their row
 * by a durable id and then write a **lane index** into it, which nothing on the
 * server compares — `clip.track_index = int(fields["track_index"])`,
 * `track.lane_index = int(fields["lane_index"])`. Replayed against a document a
 * lane was removed from, that index denotes a different lane and the row lands
 * on it, 200 OK and silent.
 *
 * The repo already knows this: `_rebaseSceneMutationIntentForHistory`
 * RETARGETS `fields.track_index` through `rebaseLaneIndex` rather than passing
 * it through, and the comment there says why — *"item lane_index fields still
 * have no lane identity guard, so a pass-through index could delete or rewrite
 * the OTHER lane."* An HTTP replay is a pass-through index, against a document
 * the client never read and so cannot rebase against.
 *
 * `update_reference_item` is no better guarded for this despite comparing the
 * written fields: `expected.lane_index` compares the item's CURRENT lane number
 * against the caller's snapshot, and both are unchanged when it is the lane
 * *layout* that moved.
 *
 * Reachable on the commonest gesture in the editor — the drag commit sends
 * `track_index` on every clip it touches, whether or not the lane changed. A
 * mute toggle or a trim that names no lane keeps its retry.
 * Expiry: delete when those branches compare a lane identity, which the
 * `update_clip`/`update_audio_track` guard retrofit owns.
 */
const LANE_DESTINATION_FIELD = freeze({
    update_clip: "track_index",
    update_audio_track: "lane_index",
    update_reference_item: "lane_index",
});

function laneDestinationRefine(operation) {
    const field = LANE_DESTINATION_FIELD[String(operation?.type || "")];
    const fields = operation?.fields;
    if (!field || !fields || typeof fields !== "object") return null;
    if (!Object.hasOwn(fields, field)) return null;
    return {
        addressing: POSITIONAL,
        retryable: false,
        reason: `fields names \`${field}\`, a lane position nothing compares; a `
            + "replay would write the row to whatever lane that index denotes in "
            + "the newer document",
    };
}

/**
 * Per dispatcher operation type: how it is addressed, what promotes a positional
 * address to a replayable one, and one traced line of evidence naming the code.
 *
 * `refine(operation)` exists only where the class genuinely depends on the
 * payload. Two operations need it and both are recorded below; everything else
 * is data so that it can be read without executing it.
 */
export const SCENE_MUTATION_ADDRESSING = freeze({

    // --- named by a durable id ------------------------------------------------
    update_clip: freeze({ addressing: DURABLE, ids: freeze(["clip_id"]),
        evidence: "`_apply_update_clip` and `_apply_linked_bounds_update` both "
            + "resolve the durable `clip_id`. The id is the whole of the evidence, "
            + "and under the rule it is enough: the row is the same row in any "
            + "version. **Residual, stated rather than implied:** neither path "
            + "reads `expected`, so the client-computed timeline geometry in "
            + "`fields` is re-applied to a document the caller never read, which "
            + "widens the staleness window behind the tracked entry about items "
            + "sitting past the end of their own media. Retry did not create that "
            + "window and removing retry would not close it -- the guard retrofit "
            + "is server-side first, and it is owned by a successor plan.",
        refine: laneDestinationRefine }),
    delete_clip: freeze({ addressing: DURABLE, ids: freeze(["clip_id"]),
        evidence: "`_delete_clip` / `_apply_delete_link_refs` by durable `clip_id`." }),
    replace_clip_source: freeze({ addressing: DURABLE, ids: freeze(["clip_id", "asset_id"]),
        evidence: "`_apply_replace_clip_source` resolves a durable clip and a "
            + "durable asset. Its `expected_type` parameter names an ASSET TYPE "
            + "and is not a row guard — the decoy a widened pattern certified twice." }),
    split_clip: freeze({ addressing: DURABLE, ids: freeze(["clip_id"]),
        evidence: "`_apply_split_linked` takes a durable `clip_id` and a frame. "
            + "The frame is a coordinate, not an address. The dispatch branch "
            + "additionally requires and compares an `expected` through "
            + "`_validate_clip_identity`, which is what makes the replay honest: "
            + "if the concurrent write that caused the conflict moved the clip's "
            + "bounds or lane, the replay refuses instead of cutting a different "
            + "shape at the same frame. `changed` reporting for this branch is "
            + "still owned by umbrella Phase C stage 2." }),
    update_audio_track: freeze({ addressing: DURABLE, ids: freeze(["track_id"]),
        evidence: "`_apply_update_audio_track` / `_apply_linked_bounds_update` by "
            + "durable `track_id`; `fields.lane_index` degrades it, see "
            + "`LANE_DESTINATION_FIELD`.",
        refine: laneDestinationRefine }),
    delete_audio_track: freeze({ addressing: DURABLE, ids: freeze(["track_id"]),
        evidence: "`_delete_audio_track` / `_apply_delete_link_refs` by durable `track_id`." }),
    replace_audio_source: freeze({ addressing: DURABLE, ids: freeze(["track_id", "asset_id"]),
        evidence: "`_apply_replace_audio_source`; the same `expected_type` decoy "
            + "as `replace_clip_source`." }),
    split_audio_track: freeze({ addressing: DURABLE, ids: freeze(["track_id"]),
        evidence: "`_apply_split_linked` by durable `track_id`, with the same "
            + "dispatch-branch `_validate_audio_identity` guard as `split_clip`." }),
    update_reference_item: freeze({ addressing: DURABLE, ids: freeze(["reference_item_id"]),
        evidence: "`_apply_update_reference_item` calls `_find_reference_item` on "
            + "the durable id; `_reference_item_expected` then compares the written "
            + "fields, which narrows the write but is not what addresses it. "
            + "`fields.lane_index` degrades it: that comparison reads the item's "
            + "current lane NUMBER, which is unchanged when it is the lane layout "
            + "that moved.",
        refine: laneDestinationRefine }),
    delete_reference_item: freeze({ addressing: DURABLE, ids: freeze(["reference_item_id"]),
        evidence: "`_apply_delete_reference_item` by durable id, with the whole "
            + "stored record required and compared." }),
    split_reference_item: freeze({ addressing: DURABLE, ids: freeze(["reference_item_id"]),
        evidence: "`_apply_split_reference_item` by durable id." }),
    delete_link_group: freeze({ addressing: DURABLE, ids: freeze(["group_id"]),
        evidence: "The only branch that calls no handler: "
            + "`_apply_scene_mutation_operation` filters `scene.linked_item_groups` "
            + "by the durable `group_id` inline." }),
    delete_prompt_semantic_unit_if_unreferenced: freeze({
        addressing: DURABLE, ids: freeze(["semantic_unit_id"]),
        evidence: "`_apply_delete_prompt_semantic_unit_if_unreferenced` resolves "
            + "the durable project-level id and compares the whole dict." }),
    create_prompt_semantic_unit: freeze({
        addressing: DURABLE, ids: freeze(["unit.semantic_unit_id"]),
        evidence: "A create, but the browser owns a client-stable id and "
            + "`_apply_create_prompt_semantic_unit` refuses it outright when it is "
            + "missing. The locked mutation owns the collision check and the final "
            + "handle, so a replay mints the same identity or is refused." }),

    // --- names no row and no document-derived destination ---------------------
    update_scene_fields: freeze({
        addressing: UNADDRESSED,
        evidence: "The scene is named by the durable `scene_id` in the route path, "
            + "and `_apply_scene_fields` writes whole values into the keys `fields` "
            + "carries. `_merge_prompt_edit_fields` additionally refuses a "
            + "`prompt_edit` document on a content-hash mismatch, and "
            + "`_validate_global_prompt_expectations` compares the four "
            + "`_DIRECT_GLOBAL_PROMPT_FIELDS`, so the guarded fields re-validate on "
            + "a replay rather than overwriting.",
        refine(operation) {
            const fields = operation?.fields;
            if (!fields || typeof fields !== "object") return null;
            const counts = LANE_COUNT_FIELDS.filter((name) => name in fields);
            if (!counts.length) return null;
            return {
                addressing: POSITIONAL,
                retryable: false,
                reason: `fields names ${counts.join(", ")}; \`_set_scene_lane_count\``
                    + " pops configs and recipes down to an absolute count read from"
                    + " the caller's own document, so a replay would delete a lane"
                    + " another writer added",
            };
        },
    }),
    import_prompt_context_dependencies: freeze({
        addressing: UNADDRESSED,
        evidence: "`_apply_prompt_context_dependencies` extends project-level lists "
            + "keyed by exact `profile_id`/`semantic_unit_id`, refusing a dependency "
            + "with no exact id and refusing an id collision with a different "
            + "definition rather than choosing a side. No scene row is addressed." }),

    // --- positional, promoted by a snapshot the server compares ----------------
    update_guide: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["guide_id"], validator: "_validate_guide_identity" })]),
        evidence: "Addressed by `frame_index`, and a frame can hold a different "
            + "guide in a later version — `_apply_create_guide` replaces at a frame. "
            + "`_validate_guide_identity` compares `guide_id` when the caller sends "
            + "it, on both the `apply_linked` path and `_apply_update_guide`, which "
            + "is what makes the positional address safe to re-resolve." }),
    delete_guide: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["guide_id"], validator: "_validate_guide_identity" })]),
        evidence: "`frame_index`, with `_validate_guide_identity` on both paths." }),
    move_guide: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["guide_id"], required: ["replaces_guide_id"],
            validator: "_validate_guide_destination" })]),
        evidence: "Two frames, two claims. `_validate_guide_identity` checks the "
            + "source guide and `_validate_guide_destination` checks what is about "
            + "to be replaced at the destination, requiring `replaces_guide_id` "
            + "outright. Both re-run on a replay." }),
    create_guide: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            required: ["replaces_guide_id"], validator: "_validate_guide_creation_identity" })]),
        evidence: "The destination frame is not an empty slot: `_apply_create_guide` "
            + "REPLACES every guide already there. `_validate_guide_creation_identity` "
            + "requires `replaces_guide_id` and compares it against every occupant, "
            + "and an empty string is the positive claim that the frame held nothing. "
            + "That is an identity claim about what is destroyed, so the replay is "
            + "verified rather than blind." }),
    update_prompt_section: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["prompt_id"], validator: "_validate_prompt_identity" })]),
        evidence: "Addressed by list `index`, which `_find_prompt_section` resolves "
            + "positionally and which re-sorts after every write. "
            + "`_validate_prompt_identity` compares `prompt_id` when sent — on the "
            + "`apply_linked` path, in `_apply_update_prompt_section`, and in the "
            + "`_apply_scene_mutations_sync` batch pre-pass." }),
    delete_prompt_section: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["prompt_id"], validator: "_validate_prompt_identity" })]),
        evidence: "List `index`, with `_validate_prompt_identity` on both paths." }),
    split_prompt_section: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["prompt_id"], validator: "_validate_prompt_identity" })]),
        evidence: "List `index`, validated in the branch before `_apply_split_linked`." }),
    swap_guides: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([
            guard("expected_a", { identifying: ["guide_id"], validator: "_validate_guide_identity" }),
            guard("expected_b", { identifying: ["guide_id"], validator: "_validate_guide_identity" }),
        ]),
        evidence: "Two frame indices; `_apply_swap_guides` validates BOTH identities. "
            + "Replaying without either claim could exchange the wrong pair." }),
    swap_prompt_sections: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([
            guard("expected_a", { identifying: ["prompt_id"], validator: "_validate_prompt_identity" }),
            guard("expected_b", { identifying: ["prompt_id"], validator: "_validate_prompt_identity" }),
        ]),
        evidence: "Two list indices, and `_apply_swap_prompt_sections` calls "
            + "`_validate_prompt_identity` twice. BOTH claims are required: a swap "
            + "that re-resolved one row and guessed the other would exchange the "
            + "wrong pair." }),
    remove_lane: freeze({ addressing: POSITIONAL,
        evidence: "Guarded, and deliberately NOT promoted by that guard. "
            + "`_validate_lane_removal_identity` requires `expected` and compares "
            + "the lane's normalized `config` plus a `lane_count` FLOOR, plus "
            + "`lane_id` for `reference`. The identity `durable_rules.md` names for "
            + "a family with no durable id is *one unique exact normalized-config "
            + "match*, and this guard does not check uniqueness — it compares the "
            + "config sitting AT the index. On a fresh project every lane's config "
            + "is the default, so the anchor distinguishes nothing, and a retry "
            + "fires only when the document HAS moved, which is exactly when it "
            + "cannot. Counterexample: three default video lanes, this caller "
            + "removes lane 1 while another writer removes lane 0 and appends one — "
            + "floor 3 >= 3 passes, default config equals default config, and the "
            + "replay deletes the lane that inherited the index along with the "
            + "clip on it. Refusing costs one lane delete during a render; "
            + "replaying costs work the user cannot see. The guard is still worth "
            + "having against the FIRST attempt, where the caller did read the "
            + "document it names. Promote this only when the guard resolves by a "
            + "unique match, or when the write carries a version precondition." }),
    move_lane: freeze({ addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["from_lane_id", "to_lane_id"],
            validator: "_move_media_lane" })]),
        evidence: "`from_index`/`to_index` are positional, and `_move_media_lane` "
            + "REQUIRES `expected`, refuses a family with no `recipe_attr` outright, "
            + "and refuses a blank stored id — so the swap is spelled in durable lane "
            + "ids on the only movable family." }),
    update_lane_config: freeze({
        addressing: POSITIONAL,
        promotedBy: freeze([guard("expected", {
            identifying: ["lane_id"], validator: "_apply_lane_config" })]),
        evidence: "Per-field partiality, exactly as §1c warns: `_apply_lane_config` "
            + "compares `expected.lane_id` ONLY when the lane descriptor has a "
            + "`recipe_attr`, which today is `reference` alone. A fixed-config lane "
            + "type never reads `lane_index` at all — it is addressed by `lane_type` "
            + "— and a variable family without a recipe has its `expected` ignored "
            + "outright, so sending one there buys nothing. Expected stays optional "
            + "by design for bootstrap callers (`durable_rules.md`), which is why a "
            + "blank id degrades rather than refuses.",
        refine(operation) {
            const laneType = String(operation?.lane_type || "");
            const descriptor = descriptorForLaneType(laneType);
            if (!descriptor) return null;   // unknown lane type: the branch 400s
            // Branch order mirrors `_apply_lane_config`, which tests
            // `fixed_config_attr` FIRST and without consulting `variable`. No
            // descriptor carries both today and `test_lane_registry_parity.py`
            // protects both fields, but mirroring the order rather than the
            // current data means a family that gained both could not read
            // differently on the two sides.
            if (descriptor.fixedConfigField) {
                return { addressing: DURABLE, retryable: true,
                    reason: `lane_type "${laneType}" is a fixed-config lane; `
                        + "`_apply_lane_config` writes "
                        + `\`${descriptor.fixedConfigField}\` and never reads \`lane_index\`` };
            }
            if (!descriptor.recipeAttr) {
                return { addressing: POSITIONAL, retryable: false,
                    reason: `lane_type "${laneType}" has no recipe_attr, so `
                        + "`_apply_lane_config` skips the identity comparison entirely "
                        + "and any `expected` sent is discarded" };
            }
            return null;    // reference: the declared `promotedBy` decides
        },
    }),

    // --- positional, and nothing in the request compares the row ---------------
    set_lane_count: freeze({ addressing: POSITIONAL,
        evidence: "An ABSOLUTE count the client computed from its own lane count "
            + "(`count: nextCount`, `count: laneIndex + 1`). "
            + "`_set_scene_lane_count` pops configs and recipes down to it, so "
            + "replaying against a document another writer grew deletes the lane it "
            + "added. Nothing in the request names which lanes the caller meant." }),
    update_lane_configs: freeze({ addressing: COLLECTION,
        evidence: "`_apply_lane_configs`, the legacy positional whole-array "
            + "replacement, compares nothing. No client emits it; the entry exists so "
            + "one that starts to cannot acquire retry by omission." }),
    consolidate_items: freeze({ addressing: POSITIONAL,
        evidence: "`item_ids` are durable, but `target_lane` is a lane index and "
            + "`_consolidate_media_items` removes the lanes it vacates — so a replay "
            + "consolidates into whatever lane that index means in the newer "
            + "document. The lane removal it performs internally passes "
            + "`LANE_INDEX_FROM_SERVER_STATE` precisely because it is derived from "
            + "state the server already holds, which a re-sent request is not." }),
    create_clip: freeze({ addressing: POSITIONAL,
        evidence: "`fields.track_index` is a lane position and nothing compares it. "
            + "`_apply_create_clip` reads fields only, so a replay lands the clip on "
            + "whatever lane that index means in the newer document." }),
    create_audio_track: freeze({ addressing: POSITIONAL,
        evidence: "`fields.track_index`, with `_apply_create_audio_track` reading "
            + "fields only." }),
    create_reference_item: freeze({ addressing: POSITIONAL,
        evidence: "`fields.lane_index` is a lane position that no guard covers. "
            + "`_validate_reference_creation_identity` compares `next_start_frame` — "
            + "a measurement of the neighbour the caller sized itself against, on the "
            + "lane it named — so it refuses a moved neighbour but says nothing about "
            + "whether the lane index still means the same lane." }),
    create_prompt_section: freeze({ addressing: POSITIONAL,
        evidence: "`fields.start_frame`/`end_frame` name a destination span and "
            + "`_apply_create_prompt_section` compares nothing about a prior row. "
            + "`_require_no_prompt_overlap` is a consistency check against the other "
            + "sections, not an identity claim — the same argument was made for "
            + "`create_reference_item`'s `_require_no_reference_overlap` and an audit "
            + "disproved it with a counterexample, so it is not accepted here either." }),

    // --- a whole collection, with a compared structure --------------------------
    replace_prompt_sections: freeze({ addressing: COLLECTION,
        promotedBy: freeze([guard("expected", {
            required: ["sections"], validator: "_validate_prompt_replacement_identity" })]),
        evidence: "`_apply_replace_prompt_sections` assigns `scene.prompt_sections` "
            + "outright, so nothing is addressed. `_validate_prompt_replacement_identity` "
            + "REQUIRES `expected.sections` and compares the ordered "
            + "`_prompt_section_structure` — prompt ids with their bounds — which "
            + "catches an insert, a delete, a reorder or a retime. Known residual, "
            + "stated in the validator: a concurrent edit to a section's CONTENT that "
            + "leaves its id and bounds alone is not covered, and a replay widens the "
            + "window on that. Its only live emitter opts out of retry explicitly for "
            + "a separate lifecycle reason." }),

    // --- one decision per member ------------------------------------------------
    bulk_delete_items: freeze({ addressing: PER_ITEM,
        evidence: "Each member resolves through `_item_ref_from_selection`, which "
            + "validates that member's own `expected` at the point it resolves the "
            + "row. `clip` and `audio` members carry a durable id; `guide` and "
            + "`prompt` members are resolved positionally by frame or list index, so "
            + "they need their own identity claim." }),
    create_link_group: freeze({ addressing: PER_ITEM,
        evidence: "`_add_link_group` resolves every member through "
            + "`_item_ref_from_selection`." }),
    unlink_items: freeze({ addressing: PER_ITEM,
        evidence: "`_item_ref_from_selection` per member; `_unlink_refs` then acts "
            + "on what it returned." }),
});

/**
 * Per-member identity for the `items[]` operations.
 *
 * `_mutationItemFromSelection` sets a guide member's `id` to its FRAME INDEX and
 * a prompt member's to its LIST INDEX, and `_item_ref_from_selection` resolves
 * both positionally, so neither is treated as durable here whatever the id looks
 * like. Demotion is the safe direction: the worst it costs is a refusal.
 *
 * `reference` is `_apply_bulk_delete_items`' **fifth** member type and the
 * strongest of them: `_find_reference_item` resolves a durable id and
 * `_reference_item_expected(item, expected, set(item.to_dict()))` requires and
 * compares the whole stored record. It satisfies both qualifying clauses of the
 * rule at once. Leaving it out is not a conservative default — because one weak
 * member denies the batch, it stripped retry from every selection that happened
 * to contain a Reference item, including a plain clip-plus-reference delete.
 * `_item_ref_from_selection`'s `LINK_ITEM_TYPES` has no `reference`, so a
 * `create_link_group` or `unlink_items` naming one is refused 400 before any
 * write and never reaches a conflict at all.
 */
const PER_ITEM_IDENTITY = freeze({
    clip: freeze({ durable: true, identifying: freeze([]) }),
    audio: freeze({ durable: true, identifying: freeze([]) }),
    reference: freeze({ durable: true, identifying: freeze([]) }),
    guide: freeze({ durable: false, identifying: freeze(["guide_id"]) }),
    prompt: freeze({ durable: false, identifying: freeze(["prompt_id"]) }),
});

function guardSatisfied(operation, requirement) {
    const bag = operation?.[requirement.bag];
    if (!bag || typeof bag !== "object") return false;
    for (const key of requirement.required) {
        if (!(key in bag)) return false;
    }
    if (!requirement.identifying.length) return true;
    return requirement.identifying.every((key) => !isBlank(bag[key]));
}

function perItemEvidence(operation) {
    const items = Array.isArray(operation?.items) ? operation.items : null;
    // An empty or malformed `items` is not a positive claim about anything.
    if (!items || !items.length) {
        return { addressing: PER_ITEM, retryable: false,
            reason: "no items to decide on" };
    }
    for (const item of items) {
        const identity = PER_ITEM_IDENTITY[String(item?.type || "")];
        if (!identity) {
            return { addressing: PER_ITEM, retryable: false,
                reason: `member type "${String(item?.type || "")}" is not classified` };
        }
        if (identity.durable) {
            if (isBlank(item?.id)) {
                return { addressing: PER_ITEM, retryable: false,
                    reason: `a ${item.type} member carries no durable id` };
            }
            continue;
        }
        const expected = item?.expected;
        const named = expected && typeof expected === "object"
            && identity.identifying.some((key) => !isBlank(expected[key]));
        if (!named) {
            return { addressing: PER_ITEM, retryable: false,
                reason: `a ${item.type} member is resolved positionally and names no `
                    + identity.identifying.join("/") };
        }
    }
    return { addressing: PER_ITEM, retryable: true,
        reason: "every member is named by a durable id or by an identity "
            + "`_item_ref_from_selection` compares where it resolves the row" };
}

/**
 * Whether one operation may be re-sent against a document the caller never read.
 *
 * Returns `{ type, addressing, retryable, reason }`. `reason` is for the retry
 * diagnostic and for the tests that pin these decisions — it is never shown to
 * the user, whose refusal message comes from the server.
 */
export function sceneMutationRetryEvidence(operation) {
    const type = String(operation?.type || "");
    const entry = SCENE_MUTATION_ADDRESSING[type];
    if (!entry) {
        return { type, addressing: "", retryable: false,
            reason: "operation type is not classified" };
    }

    const refined = typeof entry.refine === "function" ? entry.refine(operation) : null;
    if (refined) return { type, ...refined };

    if (entry.addressing === DURABLE) {
        const missing = entry.ids.filter((path) => isBlank(readPath(operation, path)));
        if (missing.length) {
            return { type, addressing: POSITIONAL, retryable: false,
                reason: `names no ${missing.join(", ")}` };
        }
        return { type, addressing: DURABLE, retryable: true,
            reason: `named by durable ${entry.ids.join(", ")}` };
    }

    if (entry.addressing === PER_ITEM) return { type, ...perItemEvidence(operation) };

    if (entry.addressing === UNADDRESSED) {
        return { type, addressing: UNADDRESSED, retryable: true,
            reason: "names no row and no document-derived destination" };
    }

    const requirements = entry.promotedBy || [];
    if (!requirements.length) {
        return { type, addressing: entry.addressing, retryable: false,
            reason: "positional, and nothing in the request names the row" };
    }
    const unmet = requirements.find((requirement) => !guardSatisfied(operation, requirement));
    if (unmet) {
        return { type, addressing: entry.addressing, retryable: false,
            reason: `${unmet.bag} names no identity \`${unmet.validator}\` can compare` };
    }
    return { type, addressing: entry.addressing, retryable: true,
        reason: "positional, with a snapshot the server compares where it resolves "
            + `the row (${requirements.map((one) => one.validator).join(", ")})` };
}

/**
 * The batch's retry policy: every operation must qualify.
 *
 * One weak operation makes the whole request weak, because the request is
 * re-sent whole — there is no way to replay part of it. An empty batch never
 * reaches the network; it is classified as not retryable so that "nothing to
 * decide on" can never read as permission.
 */
export function deriveRetryOnConflict(operations) {
    const list = Array.isArray(operations) ? operations : [];
    if (!list.length) return false;
    return list.every((operation) => sceneMutationRetryEvidence(operation).retryable);
}

// Deliberately no warn-once for a denied retry, though the shape is available
// (`_warnHistoryOptimisticSkip`). A denial fires on every non-qualifying gesture
// whether or not a conflict ever happens, so the line would usually say "could
// not retry" about a write that had nothing to retry -- noise on one of the
// commonest gestures in the editor, against a manual row that asks whether
// ordinary authoring stays quiet. `sceneMutationRetryEvidence(...).reason` is
// there for a debugging session that wants it.
