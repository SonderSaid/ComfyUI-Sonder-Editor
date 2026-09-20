/**
 * What a coalesced scene mutation loses, and what may safely be collapsed.
 *
 * The queue coalesces by **replacing** the pending intent
 * (`project_mutation_queue.js`: `existing.intent = intent`) unless the gesture
 * supplies a `merge`, and it settles every collapsed waiter from the survivor's
 * single result — so a losing gesture is told it succeeded whether or not its
 * intent survived. `tests/test_scene_mutation_registration.py` is the tripwire
 * that makes that a decision rather than a default; this module is what a
 * gesture passes once it has made the decision.
 *
 * ## The policy, in two parts
 *
 * **1. Preserve every operation.** The default merge concatenates, older first.
 * `_apply_scene_mutations_sync` applies a batch in order inside one request, so
 * two gestures' operations sent together generally produce the state two
 * separate requests would have produced — including each operation's own
 * `expected`, which is compared against the state its author actually saw
 * because the earlier operation in the same batch has just established it.
 *
 * **Two batch-level rules break that generality, and both are stated here
 * rather than assumed away.** `_apply_scene_mutations_sync` refuses a batch
 * holding more than one media-I/O create (`_media_io_operation_count` →
 * `400 too_many_media_io_operations`), so two concatenated `create_clip` /
 * `create_audio_track` drops are refused where two requests both succeed. And
 * it validates EVERY `update_prompt_section` `expected` in the batch **before
 * applying the first operation**, against pre-batch state — so a later
 * operation's guard, read after an earlier gesture's local apply, is compared
 * against a document the batch has not reached yet.
 *
 * The second of those **is** reachable: the lane-header visibility gesture
 * coalesces on a repeating key and reaches `update_prompt_section` through
 * `_buildLinkedMuteOperations`. It is safe because that operation's `expected`
 * is identity-only and a mute changes none of the fields it constrains — a
 * traced reason, recorded and pinned in
 * `tests/test_scene_mutation_coalescing_policy.py`, not a property of the rule.
 * That pin resolves each site's operations through the helpers it calls,
 * because attributing operations by enclosing scope sees only what a gesture
 * builds inline and clears it on that fraction.
 *
 * Coalescing does **not** have to mean collapsing payloads. It means collapsing
 * *writes*, and the write is where the cost is: `sonder_editor_bugs.md` measures
 * six lane-header clicks at 24.2 s because each pays the ~1,966 ms route floor,
 * not because six small operations are expensive to apply.
 *
 * **2. Collapse only where collapsing is traced to be lossless.** Concatenation
 * alone would let a long burst accumulate one operation per click — the 460 s
 * entry is 61 of them — so a same-row update collapses into one operation when
 * this table says the server cannot tell the difference. Collapsing is an
 * optimisation on top of a conservative base, never the base itself. An
 * operation this table does not classify as collapsible is kept, not dropped.
 *
 * ## The collapse rule, and why it is narrow
 *
 * Two operations collapse only when all of these hold:
 *
 *   - the table gives the type a `rowKey`, and both operations produce the same
 *     non-null key — so they name the same row and the same variant of it;
 *   - neither names a field this module records as sub-keyed, because the server
 *     applies such a value key by key and a field union would drop the keys the
 *     newer intent does not name;
 *   - their `expected` bags have the **same key set**, or neither has one.
 *
 * The last clause is the one that is easy to get wrong. `expected` states what
 * the author saw, and the older author saw the pre-burst state while the newer
 * author saw the state the older gesture's optimistic local apply produced. When
 * both operations are kept, each guard is compared against the state that
 * operation's author really saw. When they are collapsed, only one guard
 * survives — so the **oldest** must win (`durable_rules.md`; the shipped pattern
 * is `_updateItemPropertyWithinGesture`), and that is only equivalent when the
 * two bags constrain the same keys. Unequal key sets would let a newer-only key
 * survive describing state the server has not reached, turning a valid burst
 * into a 409. Refusing to collapse there costs one extra operation in a payload
 * and nothing else.
 *
 * `fields` is the mirror image: the newest value wins, because each named
 * field's value is replaced outright by `_apply_scene_fields` and its siblings.
 *
 * ## What preservation costs, stated because it is the other half of the trade
 *
 * Keeping both operations re-orders guard evaluation against a document the
 * earlier operation has already changed. That is exactly right when the newer
 * author read `expected` AFTER the older gesture's optimistic local apply — the
 * server reaches the same state the author saw. It is wrong when the gesture has
 * **no** local apply, because then both authors read the same pre-burst values
 * and the second guard is stale by the time it is compared.
 *
 * So the two halves of the policy protect opposite things, and neither is
 * universally safer: a fold is what makes a before-value guard survive, and
 * preservation is what makes a consumed-once intent survive. That is why a
 * gesture may only start coalescing through the tripwire in
 * `tests/test_scene_mutation_registration.py`, which is where an author is made
 * to say which of the two their payload needs.
 *
 * ## What this module deliberately does not own
 *
 * **The key.** Whether two gestures can coalesce at all is decided by the key
 * they enqueue under, and a key is a property of the *gesture* — which lanes it
 * spans, which item it addresses, whether the author would expect two of them to
 * be one edit. Declaring it per operation type would move that decision away
 * from the call site that owns it. The keys are traced and pinned in
 * `tests/test_scene_mutation_registration.py` instead, including the ones whose
 * interpolations make coalescing impossible by construction.
 *
 * **Whether a gesture coalesces.** `_runSceneMutation` keeps ownership. This
 * module answers "what would a collapse lose", the same way
 * `scene_mutation_addressing.js` answers "may this be replayed" without taking
 * the retry decision. `architecture.md#scene-mutation-authoring` records why
 * those decisions remain with their existing owners rather than a new registry.
 *
 * **Rollback.** A gesture that restores local state on failure needs
 * `onSupersededByCoalescing`, because the queue settles every collapsed waiter
 * from the survivor's result and each loser's own `catch` would otherwise
 * restore an earlier sibling's optimistic state. That is a `durable_rules.md`
 * invariant and lives at the call site.
 *
 * ## Consumers
 *
 * Three, all from umbrella Phase C stage 1:
 * `_applyHeaderVisibilityBulkWithinGesture` (L2),
 * `_convertClipRoleWithinGesture` and `_toggleSelectedMuteWithinGesture` (L3).
 * `_updateSceneFpsWithinGesture` and `_updateSceneGlobalContextWithinGesture`
 * were examined in L3 and deliberately left uncoalesced; their reasons are in
 * `COALESCE_OPT_OUT_REVIEWED`.
 *
 * Two gestures still define their own merge inline —
 * `_saveLaneConfigWithinGesture`'s per-lane merge and
 * `_updateItemPropertyWithinGesture`'s single-operation merge — and this module
 * was written to reproduce both. They are intentional mirrors and
 * `tests/test_scene_mutation_coalescing_policy.py` holds them to parity, which
 * is what `agent_workflow.md` requires of a mirror. Retiring them is stage 1
 * L3's, not this landing's: each carries its own rollback and undo behaviour
 * that has to be traced before its merge changes shape. Expiry for the parity
 * test: it goes when those two call sites pass `coalesceSceneMutationIntents`
 * and the inline copies are deleted.
 */

/** The row and variant two operations must share before they may collapse. */
const rowKey = (fn) => fn;

/** A collapsible operation type: same row, field union is lossless. */
const collapsible = (key, evidence) => Object.freeze({
    collapsible: true, rowKey: rowKey(key), evidence,
});

/** An operation that is always kept as its own entry in a merged batch. */
const preserved = (reason) => Object.freeze({
    collapsible: false, rowKey: null, evidence: reason,
});

const text = (value) => (value === undefined || value === null ? "" : String(value));
const flag = (value) => (value ? "1" : "0");

/**
 * Which timeline-bounds fields an operation names, as part of its row key.
 *
 * `_apply_update_clip` reads `timeline_start_frame` DIFFERENTLY depending on
 * whether `timeline_end_frame` is in the same `fields` dict: alone it preserves
 * duration (`clip.timeline_end_frame = new_start + duration`), together it is an
 * absolute pair. A field union would therefore silence the duration-preserving
 * branch and the clip would keep the older operation's end instead of shifting
 * to it. Putting the signature in the row key means two operations naming
 * different bounds never fold — measured at 32 divergent pairs out of 225
 * before this existed, every one of that exact shape.
 * Expiry: delete when `_apply_update_clip` and `_apply_update_audio_track` read
 * `timeline_start_frame` the same way whatever accompanies it.
 */
const BOUNDS_FIELDS = Object.freeze(["timeline_start_frame", "timeline_end_frame"]);

function boundsSignature(operation) {
    const fields = operation?.fields;
    if (!fields || typeof fields !== "object") return "-";
    const named = BOUNDS_FIELDS.filter((name) => Object.hasOwn(fields, name));
    return named.length ? named.join("+") : "-";
}

/**
 * A linked update that reaches the group-delta logic can never fold.
 *
 * `_apply_linked_bounds_update` derives the delta from the ANCHOR's stored
 * bounds and then moves partners by it — every member when the move is pure
 * (`delta_start == delta_end`), only the members aligned with the anchor when it
 * is not. Folding a pure move into a trim therefore changes WHICH partners
 * moved: measured, a partner that ends at 45..105 when the two are applied in
 * turn stays at 30..90 when they are folded. Alignment can also change between
 * two trims, so matching bounds signatures is not sufficient either.
 *
 * The one exception is the early return at the top of that function: a
 * `fields` of exactly `{ muted }` writes `muted` to every ref and returns before
 * any bounds arithmetic, which is the shape `_updateItemPropertyWithinGesture`
 * and the lane-header unmute both send.
 */
function foldsWhileLinked(operation) {
    if (!operation?.apply_linked) return true;
    const names = Object.keys(operation?.fields || {});
    return names.length === 1 && names[0] === "muted";
}

/**
 * Field names whose VALUE the server applies key by key.
 *
 * A field union is not enough for these: the union keeps the newest whole value
 * for the field, and the setter then writes only the inner keys that value
 * names, so an inner key the newer intent omits is never restored from the
 * older one. Intentional mirror of `SUBKEYED_PAYLOAD_VALUES` in
 * `tests/test_scene_mutation_registration.py`, which carries the traced reason
 * for each and is the authority; the parity test in
 * `tests/test_scene_mutation_coalescing_policy.py` keeps the two identical.
 *
 * Expiry: an entry leaves when its setter stops applying the value key by key.
 */
export const SUBKEYED_FIELDS = Object.freeze([
    "global_channels",
    "global_channel_docs",
    "channels",
    "channel_docs",
    "prompt_edit",
]);

const SUBKEYED = new Set(SUBKEYED_FIELDS);

/**
 * The `update_scene_fields` fields two operations may fold across.
 *
 * An ALLOW-list, not a deny-list, because `_apply_scene_fields` applies its
 * fields in a fixed internal order and the dangerous ones are dangerous for
 * different reasons — a deny-list would have to be re-audited every time that
 * function grows a branch, and the failure of missing one is silent. Everything
 * here is a plain assignment that touches nothing another field writes:
 * `scene.name`, `scene.width`, `scene.height`, `scene.generation_params`,
 * `scene.prompt_context_profile_id`, `scene.prompt_context_profile_config`.
 *
 * Deliberately absent, each traced: `duration_frames` (clamps Reference items),
 * `fps` (retimes all geometry, and is applied AFTER `duration_frames` whatever
 * order the client authored), the four lane-count fields (`_set_scene_lane_count`
 * pops configs, so shrink-then-grow destroys named lanes that a fold would
 * keep), `prompt` (`Scene.set_global_prompt` clears every channel but the
 * first), `global_attachments` (validated against the scene other fields
 * change), and the sub-keyed values above.
 * Expiry: an entry leaves when its branch stops being a plain assignment.
 */
const FOLDABLE_SCENE_FIELDS = Object.freeze(new Set([
    "name", "width", "height", "generation_params",
    "prompt_context_profile_id", "prompt_context_profile_config",
]));

/**
 * Per dispatcher operation type: may two of these collapse into one, and why.
 *
 * Every entry carries one traced line naming the code that decides it.
 * `tests/test_scene_mutation_coalescing_policy.py` checks the set against the
 * dispatcher and each citation against `server/routes.py`, so an operation type
 * added to the dispatcher without a decision here fails the suite.
 */
export const SCENE_MUTATION_COALESCING = Object.freeze({

    // --- collapsible: a named row, and fields replaced outright ---------------

    update_clip: collapsible(
        (op) => (foldsWhileLinked(op)
            ? `clip:${text(op.clip_id)}:${flag(op.apply_linked)}`
                + `:${flag(op.validate_lane_collision)}:${boundsSignature(op)}`
            : null),
        "`_apply_update_clip` writes only the keys `fields` names and replaces "
        + "each value outright -- EXCEPT `timeline_start_frame`, whose meaning "
        + "depends on whether `timeline_end_frame` accompanies it, which is why "
        + "the bounds signature is in the row key. `_apply_linked_bounds_update` "
        + "does not compose at all beyond its muted-only early return, so a "
        + "linked bounds write folds with nothing."),

    update_audio_track: collapsible(
        (op) => (foldsWhileLinked(op)
            ? `audio:${text(op.track_id)}:${flag(op.apply_linked)}`
                + `:${flag(op.validate_lane_collision)}:${boundsSignature(op)}`
            : null),
        "`_apply_update_audio_track` is the audio twin of `_apply_update_clip`, "
        + "with the same duration-preserving `timeline_start_frame` branch and "
        + "the same `_apply_linked_bounds_update` under `apply_linked`."),

    update_reference_item: collapsible(
        (op) => `reference:${text(op.reference_item_id)}`
            + `:${Object.keys(op?.fields || {}).sort().join(",")}`,
        "`_apply_update_reference_item` resolves a durable `reference_item_id`, "
        + "reads `start_frame` / `end_frame` absolutely, and writes the named "
        + "fields. Its guard is the one place the server derives the REQUIRED "
        + "keys from the payload -- `_reference_item_expected(item, expected, "
        + "fields.keys())` reaches `_require_expected` -- so a fold whose "
        + "`fields` union is wider than the older `expected` bag would build a "
        + "request the server refuses outright. The field names are therefore "
        + "in the row key, which forces the two payloads to name the same set "
        + "rather than relying on every emitter happening to build `expected` "
        + "from `Object.keys(props)`."),

    update_guide: collapsible(
        (op) => (foldsWhileLinked(op)
            ? `guide:${text(op.frame_index)}:${flag(op.apply_linked)}` : null),
        "`_apply_update_guide` accepts strength, muted, asset_id, source, "
        + "fit_mode and crop_position and NOT `frame_index`, so an unlinked "
        + "update can never move the row the next operation addresses -- moving "
        + "a guide is `move_guide`, which is preserved. Under `apply_linked` the "
        + "branch does NOT reach `_apply_update_guide` at all: it hands `fields` "
        + "to `_apply_linked_bounds_update`, which reads "
        + "`fields.get(\"frame_index\", ...)` and moves the guide and its whole "
        + "closure. That path folds only on its muted-only early return, the "
        + "same rule `update_clip` takes."),

    update_lane_config: collapsible(
        (op) => `lane:${text(op.lane_type)}:` + (text(op.expected?.lane_id)
            ? `id:${text(op.expected.lane_id)}` : `index:${text(op.lane_index || 0)}`),
        "`_apply_lane_config` writes one lane's config from the fields named. "
        + "Keyed by durable `lane_id` when the author knew one and by index "
        + "otherwise, mirroring the merge `_saveLaneConfigWithinGesture` already "
        + "ships: distinct lanes that occupy the same index across a reorder "
        + "must not erase each other's edits."),

    update_scene_fields: collapsible(
        (op) => (Object.keys(op?.fields || {})
            .every((name) => FOLDABLE_SCENE_FIELDS.has(name))
            ? "scene:fields" : null),
        "`_apply_scene_fields` writes only the keys `fields` names -- but it "
        + "writes them in a FIXED internal order, and several carry effects on "
        + "state another field also writes: `duration_frames` runs "
        + "`_clamp_reference_items_to_scene`, `fps` runs `retime_scene_geometry`, "
        + "each lane-count field runs `_set_scene_lane_count`, which POPS "
        + "configs, and `prompt` reaches `Scene.set_global_prompt`, whose own "
        + "docstring calls it destructive. Folding two operations reorders their "
        + "fields against each other, so only fields traced as plain assignment "
        + "fold. `_merge_prompt_edit_fields` puts `prompt_edit` out on the same "
        + "argument, one record at a time behind its own `expected`."),

    // --- preserved: the effect is not a field assignment on a named row ------

    update_lane_configs: preserved(
        "`_apply_lane_configs` replaces whole config LISTS from `fields`, so two "
        + "of them name no row to collapse on. No live gesture emits it."),

    update_prompt_section: preserved(
        "addressed by list INDEX. `_split_prompt_object` re-sorts "
        + "`prompt_sections` and `swap_prompt_sections` reorders them, so two "
        + "operations authored at different moments may not name the same "
        + "section; and its `fields` can carry `channels` / `channel_docs`, "
        + "which are sub-keyed."),

    set_lane_count: preserved(
        "`_set_scene_lane_count` reaches an ABSOLUTE count the author computed "
        + "from its own copy, and `_apply_scene_mutations_sync` applies a batch "
        + "in order, so keeping both reproduces exactly what two requests would "
        + "have done. A collapse would have to recompute the count from live "
        + "state, which is the gesture's job, not this table's."),

    consolidate_items: preserved(
        "`_consolidate_media_items` returns `final_target_lane` because it "
        + "compacts the lanes it empties, so a later operation's `target_lane` "
        + "is only meaningful after the earlier one has been applied."),

    remove_lane: preserved("removes a lane by index and shifts every lane above it."),
    move_lane: preserved("`_move_media_lane` reorders by from/to index."),

    create_clip: preserved("creates a row; a replacement would lose one clip."),
    create_audio_track: preserved("creates a row."),
    create_guide: preserved("creates a row."),
    create_reference_item: preserved("creates a row."),
    create_prompt_section: preserved("creates a row."),
    create_prompt_semantic_unit: preserved(
        "creates a durable identity other operations in the same intent refer "
        + "to. `_updateSceneGlobalContextWithinGesture` PREPENDS these, and they "
        + "are consumed once -- a replacement loses them outright."),
    create_link_group: preserved(
        "`_add_link_group` runs `_unlink_refs` first, so two of them are not two "
        + "assignments to one row."),

    delete_clip: preserved("removes a row."),
    delete_audio_track: preserved("removes a row."),
    delete_guide: preserved("removes a row."),
    delete_reference_item: preserved("removes a row."),
    delete_prompt_section: preserved("removes a row, by list index."),
    delete_link_group: preserved("removes a group by id."),
    delete_prompt_semantic_unit_if_unreferenced: preserved(
        "conditional on the reference count at the moment it runs."),
    bulk_delete_items: preserved("removes the rows `items` names."),

    unlink_items: preserved(
        "`_unlink_refs` then `_prune_linked_item_groups`; the second operation's "
        + "refs are only meaningful against the groups the first one left."),

    split_clip: preserved(
        "`_apply_split_linked` cuts at a frame inside the item's CURRENT bounds, "
        + "which the previous cut changed."),
    split_audio_track: preserved("the audio twin of `split_clip`."),
    split_prompt_section: preserved("addressed by list index, then split by frame."),
    split_reference_item: preserved("splits at a frame inside current bounds."),

    replace_clip_source: preserved(
        "`_apply_replace_clip_source` re-derives bounds from the new asset's "
        + "length, so two replacements are not two assignments."),
    replace_audio_source: preserved("the audio twin of `replace_clip_source`."),
    replace_prompt_sections: preserved(
        "replaces the whole collection behind `_validate_prompt_replacement_identity`, "
        + "whose `expected` describes the collection the author replaced."),

    move_guide: preserved(
        "addressed by `from_frame_index`, which the previous move changed."),
    swap_prompt_sections: preserved("reorders two sections by list index."),
    import_prompt_context_dependencies: preserved(
        "`_apply_prompt_context_dependencies` imports a dependency set into the "
        + "project; it names no scene row."),
});

/**
 * What a collapse of this operation would cost, and the row it would collapse on.
 *
 * Returns `{ type, collapsible, rowKey, reason }`. `rowKey` is null when this
 * operation must be kept as its own entry — either because the type is never
 * collapsible, or because this particular payload disqualifies it.
 */
export function sceneMutationCoalescingEvidence(operation) {
    const type = String(operation?.type || "");
    const entry = SCENE_MUTATION_COALESCING[type];
    if (!entry) {
        return { type, collapsible: false, rowKey: null,
            reason: "operation type is not classified" };
    }
    if (!entry.collapsible) {
        return { type, collapsible: false, rowKey: null, reason: entry.evidence };
    }
    const key = entry.rowKey(operation || {});
    if (!key) {
        return { type, collapsible: false, rowKey: null,
            reason: `this payload disqualifies a collapse the type allows: ${entry.evidence}` };
    }
    return { type, collapsible: true, rowKey: `${type}|${key}`, reason: entry.evidence };
}

/** `expected` bags constrain the same keys, or neither operation has one. */
function guardsAreComparable(older, newer) {
    const a = older?.expected;
    const b = newer?.expected;
    const aHas = !!a && typeof a === "object";
    const bHas = !!b && typeof b === "object";
    if (!aHas && !bHas) return true;
    if (!aHas || !bHas) return false;
    const aKeys = Object.keys(a).sort();
    const bKeys = Object.keys(b).sort();
    return aKeys.length === bKeys.length
        && aKeys.every((key, index) => key === bKeys[index]);
}

/**
 * One operation carrying both intents: newest values, oldest guard.
 *
 * `expected` spreads the newer first and the older OVER it, so every key the
 * guard constrains carries the value the earliest author read. `guardsAreComparable`
 * has already established that the two bags constrain the same keys, so nothing
 * of the newer guard survives by accident.
 */
function collapseOperations(older, newer) {
    const merged = { ...older, ...newer };
    if (older?.expected || newer?.expected) {
        merged.expected = { ...(newer?.expected || {}), ...(older?.expected || {}) };
    }
    if (older?.fields || newer?.fields) {
        merged.fields = { ...(older?.fields || {}), ...(newer?.fields || {}) };
    }
    return merged;
}

/**
 * An operation whose acceptance and effect depend on no other row.
 *
 * Deliberately far narrower than "collapsible". An earlier version of the fold
 * guard reasoned that *"two field assignments on different rows commute"*, and
 * that is false: `_apply_update_reference_item` calls
 * `_require_no_reference_overlap`, which compares the row it is writing against
 * its SIBLINGS on the lane, so two reference moves that succeed in one order
 * can refuse in the other. `validate_lane_collision` reaches
 * `_require_media_target_bounds_fit` the same way, and a lane config naming
 * `locked` gates every later operation on that lane.
 *
 * A mute is the one payload with none of that: `_apply_ref_muted` and the
 * `if "muted" in fields` branches write a single boolean on a single row and
 * consult nothing. So a fold may jump mutes and nothing else.
 * Expiry: widen only per type, with the sibling-validation path traced.
 */
function isRowLocal(operation) {
    const names = Object.keys(operation?.fields || {});
    return names.length === 1 && names[0] === "muted";
}

/**
 * May a newer operation be folded back to `at`, or would that reorder the batch?
 *
 * Folding moves an operation EARLIER, past everything the older gesture emitted
 * after the fold target, so it is equivalent to applying both in turn only when
 * the jumped operations neither constrain nor are constrained by the one being
 * folded. `isRowLocal` is that test, and it is deliberately strict.
 *
 * Reachable rather than theoretical: `_convertClipRoleWithinGesture` emits
 * `set_lane_count` followed by `update_clip` under one coalescing key, so
 * without this a second conversion's clip write would be folded in front of the
 * first conversion's lane-count change.
 *
 * **Known residual, stated rather than proved away.** The operation being
 * FOLDED is not itself required to be row-local, so a lane config that changes
 * `locked` could in principle be folded in front of a mute on that lane and
 * turn an accepted burst into a lock refusal. No live gesture emits both: the
 * lane-header gesture re-sends `locked` unchanged from live state and never
 * varies it, and `_saveLaneConfigWithinGesture` emits no item operations at all.
 * Expiry: close this by testing the folding operation too, on the day a gesture
 * emits a lock change and a lock-gated operation under one key.
 */
function foldWouldNotReorder(merged, at, folding) {
    for (let index = at + 1; index < merged.length; index += 1) {
        const jumped = merged[index];
        // A mute writes one boolean on one row and consults nothing, so it
        // interacts with no operation in either direction.
        if (isRowLocal(jumped)) continue;
        // Two lane configs never interact with each other: `_apply_lane_config`
        // writes one lane's config and reads no sibling lane. Their one
        // cross-row effect is `locked` gating ITEM operations, which this
        // clause does not cover -- a lane config may not jump an item write.
        // Without this pair a bulk hide over N lanes would stop folding
        // entirely, which is the measured case rather than an edge one.
        if (jumped.type === "update_lane_config"
                && folding?.type === "update_lane_config") continue;
        return false;
    }
    return true;
}

/**
 * The operations of two coalesced gestures, older first.
 *
 * Every operation of both gestures survives. A newer operation that names a row
 * an older one already names, and that this module's table says the server
 * cannot tell apart, is folded into the older one's position rather than
 * appended — so a burst of clicks on one control stays one operation long while
 * a burst across different rows stays exact.
 */
export function coalesceSceneMutationOperations(olderOperations, newerOperations) {
    const older = Array.isArray(olderOperations) ? olderOperations : [];
    const newer = Array.isArray(newerOperations) ? newerOperations : [];
    const merged = [...older];
    const positions = new Map();
    older.forEach((operation, index) => {
        const { rowKey: key } = sceneMutationCoalescingEvidence(operation);
        // Last position wins: a later operation on the same row is the one a
        // newer gesture composes with, and folding into an earlier one would
        // reorder the batch.
        if (key) positions.set(key, index);
    });
    for (const operation of newer) {
        const { rowKey: key } = sceneMutationCoalescingEvidence(operation);
        const at = key ? positions.get(key) : undefined;
        if (at !== undefined && guardsAreComparable(merged[at], operation)
                && foldWouldNotReorder(merged, at, operation)) {
            merged[at] = collapseOperations(merged[at], operation);
            continue;
        }
        if (key) positions.set(key, merged.length);
        merged.push(operation);
    }
    return merged;
}

/**
 * The `merge` a coalescing gesture passes to `_runSceneMutation`.
 *
 * Reads its FIRST argument, which is what makes it an effective merge by the
 * tripwire's definition and by the queue's: `merge(existing.intent, intent)`.
 * Everything outside `operations` is taken from the newer intent, because the
 * surviving request is the newer gesture's — it carries that gesture's scene and
 * project identity, and the queue has already adopted its history entry.
 */
export function coalesceSceneMutationIntents(olderIntent, newerIntent) {
    if (!olderIntent?.operations) return newerIntent;
    if (!newerIntent) return olderIntent;
    return {
        ...newerIntent,
        operations: coalesceSceneMutationOperations(
            olderIntent.operations, newerIntent.operations),
    };
}
