// @server-mirror server/scene_history_delta.py::delta_between
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
// Rebuilds the scene a write was applied to from the scene it produced and the
// `history_before` delta the server sent with it.
//
// Leaf module: DOM-free, host-free. The server decides what changed
// (`delta_between` and `delta_from_capture` write one shape); this only reads it back.

const VERSION = 1;

function isRecord(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
}

function idKeyOf(members) {
    const keys = {
        clips: "clip_id",
        audio_tracks: "track_id",
        guide_frames: "guide_id",
        reference_items: "reference_item_id",
        prompt_sections: "prompt_id",
        linked_item_groups: "group_id",
        global_attachments: "attachment_id",
    };
    return Object.hasOwn(keys, members) ? keys[members] : "";
}

function membersBefore(name, after, change) {
    const key = idKeyOf(name);
    if (!key || !Array.isArray(after) || !isRecord(change) || !Array.isArray(change.order)
            || !Array.isArray(change.removed) || !isRecord(change.fields)
            || !isRecord(change.absent)) return null;
    const byId = new Map();
    for (const member of [...after, ...change.removed]) {
        const id = member?.[key];
        if (typeof id !== "string" || !id) return null;
        byId.set(id, member);
    }
    const before = [];
    for (const id of change.order) {
        const member = byId.get(id);
        if (!member) return null;
        const fields = Object.hasOwn(change.fields, id) ? change.fields[id] : null;
        const absent = Object.hasOwn(change.absent, id) ? change.absent[id] : null;
        if (!fields && !absent) {
            before.push(member);
            continue;
        }
        if ((fields && !isRecord(fields)) || (absent && !Array.isArray(absent))) return null;
        const restored = { ...member, ...(fields || {}) };
        for (const field of absent || []) delete restored[field];
        before.push(restored);
    }
    return before;
}

/** The scene before the write, or null for a delta this cannot read.
 *
 *  Unchanged values are SHARED with `scene`, not cloned: a scene is megabytes and
 *  this runs on every write. The caller must own `scene` and treat both as
 *  read-only, as history entries do (paint and restore clone what they use). */
export function sceneBeforeFromDelta(scene, delta) {
    if (!isRecord(scene) || !isRecord(delta) || delta.v !== VERSION
            || !isRecord(delta.fields) || !isRecord(delta.members)
            || !Array.isArray(delta.absent)) return null;
    const before = { ...scene, ...delta.fields };
    for (const name of delta.absent) delete before[name];
    for (const [name, change] of Object.entries(delta.members)) {
        const members = membersBefore(name, scene[name], change);
        if (!members) return null;
        before[name] = members;
    }
    return before;
}

/** Whether the write changed nothing the delta can express. */
export function isEmptySceneDelta(delta) {
    return isRecord(delta) && isRecord(delta.fields) && isRecord(delta.members)
        && Array.isArray(delta.absent) && !delta.absent.length
        && !Object.keys(delta.fields).length && !Object.keys(delta.members).length;
}
