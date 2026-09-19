// Link-group normalization for an optimistically edited scene.
//
// Leaf module: DOM-free, host-free, no editor imports — the same shape and the
// same reason as `scene_split_geometry.js` and `scene_move_geometry.js`.
// `editor_widget.js` cannot be imported under Node, so a mirror living only
// there could not carry the parity test `agent_workflow.md` requires of an
// intentional mirror.
//
// INTENTIONAL MIRROR of `_prune_linked_item_groups` (`server/routes.py`), which
// the mutation pipeline runs after every operation that can invalidate a link
// ref. `tests/test_link_group_parity.py` drives both sides over one table.
//
// WHICH Python copy this mirrors, because there are FOUR and they are not
// identical. `_prune_linked_item_groups` (routes) is the one mirrored here,
// because the question a local apply answers is what the scene looks like after
// the mutation pipeline touches it, not after a reload. The others:
// `Scene._normalize_linked_item_groups` (`timeline_state.py`) applies the same
// rule on load and additionally requires a truthy item id;
// `_merge_scene_link_groups`' inline filter (`project_commit.py`) applies the
// membership and dedupe rules when merging a generated scene's groups but SKIPS
// a blank or colliding `group_id` where prune re-mints it; and
// `_validate_scene_history_link_groups` (routes) validates the same shape
// rather than normalizing it. None of the four is reconciled with the others.
// Adding a rule means finding all four; this comment is the only place that
// says how many there are.
//
// WHAT "EXACT MIRROR" MEANS HERE, since it cannot mean every input.
// Identical for every input the client can actually receive, which is a real
// bound rather than a hedge: `Scene._normalize_linked_item_groups` coerces
// `group_id` and both ref fields to `str` at load, so no non-string reaches
// this code through a scene payload — but it does NOT touch the ROWS, and
// `ClipReference.from_dict` and friends store `clip_id` uncoerced. That is why
// `existingLinkIds` below mirrors the row side precisely and the group/ref side
// is compared as strings: the asymmetry is in the durable data, not in the
// taste of whoever wrote this.
//
// Two deliberate asymmetries where the server RAISES. A `null` entry in
// `scene.clips`, or an `items` that is not a list, throws in Python and is
// tolerated here. A local apply that throws aborts a gesture midway and leaves
// the timeline holding half an edit, which is worse than disagreeing about
// input no supported document contains.
//
// WHAT THIS DELIBERATELY DOES NOT DO, since the method it replaced did.
// `_pruneLocalLinkedGroups` used to resolve each ref through
// `_findSceneItemForLinkRef`, which accepts a guide addressed by FRAME INDEX
// and a prompt section addressed by LIST INDEX, and then rewrote the ref to the
// row's durable id. The server does no such repair — `_prune_linked_item_groups`
// drops a ref it cannot match by durable id outright — so the client was
// silently healing refs the server would have discarded. It is unreachable
// either way: `Scene.from_dict` normalizes `linked_item_groups` before a scene
// is ever serialized to the client, so every ref the editor receives is already
// a durable id of an existing row, and the only refs the editor mints itself
// come from `_linkRefForItem`, which is durable-id shaped by construction.
// Matching the server exactly is what makes the parity test mean something;
// keeping a repair neither side can reach is what makes a mirror rot.

/** The four types a link ref may name — `LINK_ITEM_TYPES` in `server/routes.py`.
 *
 *  Exported because `editor_widget.js` kept a second copy of this list, and two
 *  lists disagree the day a fifth type is added. NOT `Object.freeze`d: freeze
 *  covers a Set's own properties, not its contents, so `.add` still works and
 *  the spelling would promise a guarantee it does not give.
 *
 *  Adding a type here means adding it to `existingLinkIds` in the same commit.
 *  The two are coupled asymmetrically with the server: in Python the allow-list
 *  is decorative, because `existing.get(type, set())` is empty for an unknown
 *  type — an injection removing it is not detectable. Here `existing[itemType]`
 *  is a plain object lookup, so a type in this set with no entry there calls
 *  `.has` on `undefined` and throws inside a local apply.
 */
export const LINK_ITEM_TYPES = new Set(["clip", "audio", "guide", "prompt"]);

/** The durable ids a link ref can name, by type — the mirror of
 *  `_scene_existing_link_ids`.
 *
 *  Two coercion rules, and they differ on the two sides of the comparison on
 *  purpose, because the server's do:
 *
 *  A REF's id is read as `String(x || "")`, because `_link_ref_key` spells it
 *  `str(ref.get("id", "") or "")` — a falsy id becomes the empty string, which
 *  `?? ""` would leave as `"0"`.
 *
 *  A ROW's id is read RAW. `_scene_existing_link_ids` builds `{clip.clip_id …}`
 *  with no `str()` at all, and `ClipReference.from_dict` does not coerce either,
 *  so a project whose JSON stored a numeric id keeps a number here and the
 *  server's `"5" in {5}` is False. Stringifying this side would make the mirror
 *  match a ref the server discards. Nothing in the editor writes a numeric id;
 *  the point is that a mirror is only worth having if it is wrong in the same
 *  places, and this one is reachable through durable data the loader tolerates.
 */
function existingLinkIds(scene) {
    return {
        clip: new Set((scene?.clips || []).map((clip) => clip?.clip_id)),
        audio: new Set((scene?.audio_tracks || []).map((track) => track?.track_id)),
        guide: new Set((scene?.guide_frames || []).map((guide) => guide?.guide_id)),
        prompt: new Set((scene?.prompt_sections || []).map((section) => section?.prompt_id)),
    };
}

function isPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
}

/** `linked_item_groups` as the server would leave them for this scene.
 *
 *  Returns a NEW array; the caller assigns it. Four rules, in the server's
 *  order, and the order matters for the third:
 *
 *  1. A ref whose row is not in the scene is dropped.
 *  2. A ref repeated inside one group is dropped.
 *  3. A `group_id` that is blank, or that a group KEPT EARLIER already used, is
 *     re-minted. Only kept groups reserve an id — a group that dissolves under
 *     rule 4 never held its id, so a later group may still use it.
 *  4. A group left with fewer than two refs is dropped.
 *
 *  `mintGroupId` is injected rather than imported so the parity test can make
 *  both languages mint the same deterministic sequence. Production passes the
 *  widget's `_newLocalItemId("link")`, whose format differs from the server's
 *  `uuid4().hex[:8]` and is not required to match: what the two sides must agree
 *  on is WHETHER an id is re-minted, never what it is re-minted to. The
 *  canonical response replaces a locally minted id in either case.
 */
export function pruneLinkedItemGroups(scene, mintGroupId) {
    const groups = scene?.linked_item_groups;
    if (!Array.isArray(groups) || !groups.length) return [];
    const existing = existingLinkIds(scene);
    const normalized = [];
    const seenGroupIds = new Set();
    for (const group of groups) {
        if (!isPlainObject(group)) continue;
        let groupId = String(group.group_id || "");
        if (!groupId || seenGroupIds.has(groupId)) groupId = String(mintGroupId());
        const items = [];
        const seenItems = new Set();
        for (const ref of Array.isArray(group.items) ? group.items : []) {
            if (!isPlainObject(ref)) continue;
            const itemType = String(ref.type || "");
            const itemId = String(ref.id || "");
            const key = `${itemType}:${itemId}`;
            if (!LINK_ITEM_TYPES.has(itemType)) continue;
            if (!existing[itemType].has(itemId) || seenItems.has(key)) continue;
            items.push({ type: itemType, id: itemId });
            seenItems.add(key);
        }
        if (items.length >= 2) {
            normalized.push({ group_id: groupId, items });
            seenGroupIds.add(groupId);
        }
    }
    return normalized;
}
