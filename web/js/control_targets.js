// Controls change targets and the group-name rule, shared by the canvas writer
// (control_bindings.js) and the panel peers, which the Sonder App page loads
// without the canvas graph helpers. Pure on import.
// server/control_descriptors.py parse_target and server/control_manifest.py
// group_name_error are the authority (tests pin the mirrors).

export const DESCRIPTION_MAX = 1000;

export const GROUP_NAME_MAX = 120;
const FORBIDDEN_NAMES = new Set(["__proto__", "constructor", "prototype"]);
// Checked before trimming, with U+FEFF, so JS trim() and Python's strip()
// (whose whitespace sets differ only in these) trim a valid name alike.
const GROUP_FORBIDDEN_CHARS = /[:\x00-\x1f\x7f-\x9f﻿]/;

// Stripped, 1-120 code points, not a prototype name, no ":" and no control characters.
export function groupNameError(name) {
    if (typeof name !== "string") return "A group name must be text.";
    if (GROUP_FORBIDDEN_CHARS.test(name)) return "A group name can't contain a colon or control characters.";
    const text = name.trim();
    if (!text) return "A group needs a name.";
    if ([...text].length > GROUP_NAME_MAX) return `A group name must be at most ${GROUP_NAME_MAX} characters.`;
    if (FORBIDDEN_NAMES.has(text)) return "This group name is reserved. Choose another.";
    return null;
}

// A stored group read tolerantly (server control_manifest.clean_group).
export const cleanGroup = value => (typeof value === "string" && !groupNameError(value) ? value.trim() : "");

// `binding:<id>:<field>`, `note:<id>:<field>`, `execution:<id>:<field>`,
// `view:<id>:<field>`, `group:<name>:<op>` and `workflow:description`.
const TARGET_RE = /^(?:(binding|note|preset|execution|view):([A-Za-z0-9_-]{1,64})|(group):([^:]+)):([a-z_]+)$/;
export const BINDING_STRUCTURAL_OPS = new Set(["expose", "remove", "rebind", "hide", "unhide", "create"]);
export const GROUP_OPS = new Set(["create", "rename", "ungroup", "move"]);

export function parseTarget(target) {
    if (typeof target !== "string") return null;
    if (["workflow:description", "workflow:cleanup", "workflow:restore"].includes(target)) return { source: "workflow", id: "", field: target.split(":")[1] };
    const match = TARGET_RE.exec(target);
    if (!match) return null;
    return match[1] ? { source: match[1], id: match[2], field: match[5] } : { source: "group", id: match[4], field: match[5] };
}

// What a View shows (Output Views Phase 4): an output's results (no `feed`, the
// default, never written) or a node's live sampling previews (`feed: "sampling"`).
// One View per node either way. Mirrors control_views.feed_of.
export const VIEW_FEED_OUTPUT = "output";
export const VIEW_FEED_SAMPLING = "sampling";
export const viewFeed = entry => (entry?.feed === VIEW_FEED_SAMPLING ? VIEW_FEED_SAMPLING : VIEW_FEED_OUTPUT);
// KJNodes Model Preview Override, the one third-party sampling-preview
// publisher (server/kj_preview_adapter.KJ_CLASS).
export const KJ_PREVIEW_CLASS = "ModelPreviewOverrideKJ";

export const STRUCTURAL_OPS = {
    binding: BINDING_STRUCTURAL_OPS, note: new Set(["create", "remove", "hide", "unhide"]),
    group: GROUP_OPS, preset: new Set(["create", "capture", "apply"]), workflow: new Set(["cleanup", "restore"]),
    execution: new Set(["expose", "remove", "rebind", "hide", "unhide"]),
    // An Output View: label and order are presentation. It never changes a run.
    view: new Set(["create", "remove"]),
};

export function isStructuralTarget(target) {
    const parsed = parseTarget(target);
    if (!parsed) return false;
    return STRUCTURAL_OPS[parsed.source]?.has(parsed.field) || false;
}

// Values, modes and exposure change what a run submits; labels, notes,
// Advanced, arrangement and groups do not, so only these saves can refuse a Generate.
const RUN_FIELDS = new Set(["value", "seed_mode", "expose", "remove", "rebind", "hide", "unhide"]);
export function changesRun(target) {
    const parsed = parseTarget(target);
    return (parsed?.source === "binding" && RUN_FIELDS.has(parsed.field)) || (parsed?.source === "preset" && parsed.field === "apply")
        // A node switch's mode decides what runs; exposing or removing one does not.
        || (parsed?.source === "execution" && (parsed.field === "mode" || parsed.field === "modes"));
}
