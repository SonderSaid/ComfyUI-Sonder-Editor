// Which Reference Bridge outputs LiteGraph displays, and how dead ones read.
//
// THE SLOT INDEX IS THE TYPE CONTRACT. ComfyUI validates a connection against
// the node's static `/object_info` definition by slot index, not against the
// live `node.outputs[i].type`. Removing a MIDDLE output shifts every later slot
// into a position whose declared type belongs to something else — dragging from
// a slot labelled `r01` sitting at index 3 is read as `reference_audio`/AUDIO
// and refused. That is a tuple reshape in everything but name, which
// `durable_rules.md` forbids.
//
// So NO output is ever removed. Outputs a recipe does not drive are RELABELLED
// as unused, which changes nothing about index, name or type.
//
// The r-block used to shrink, because it was a contiguous TAIL and trimming
// r05..r16 moved no surviving slot. Appending p01..p16 after it ended that:
// removing an r-slot now slides every p-slot down into an r-block position, so
// a socket labelled p01 delivered an image tensor. Nothing is a tail any more,
// so nothing is removed.
//
// Two further rules:
//   1. A connected slot is never REMOVED, whatever the recipe says. Removing it
//      would silently drop the user's link. It is still MARKED: wiring says the
//      user connected something, not that the recipe drives it, and a wired
//      output the recipe ignores emits its fallback into a live link. That is
//      the case most worth naming, not the one to stay quiet about. Marking
//      touches only the label, so the link is untouched either way.
//   2. No liveness declaration means show everything unmarked. An unresolved
//      project, an unwired selector, a lane index pointing at nothing and a
//      failed fetch are all "we don't know", not "this recipe drives nothing" —
//      a slow load must not look like a broken node.
//
// The backend is untouched either way: it always returns the full tuple with
// its documented fallbacks, so AUDIO still never returns None.

import { REFERENCE_OUTPUT_NAMES } from "./reference_resolution.js";

export const MAX_REFERENCE_SLOTS = 16;
export const SLOT_NAME_RE = /^r(0[1-9]|1[0-6])$/;

// Fixed outputs in tuple order. "slots" is the r-block pseudo-name used by
// `live_outputs`, not a socket, so it is excluded and handled by slotCount.
export const FIXED_OUTPUT_NAMES = REFERENCE_OUTPUT_NAMES.filter((name) => name !== "slots");

export const slotName = (index) => `r${String(index).padStart(2, "0")}`;
export const promptSlotName = (index) => `p${String(index).padStart(2, "0")}`;

export function slotNumber(slot) {
    const match = SLOT_NAME_RE.exec(String(slot?.name || ""));
    return match ? parseInt(match[1], 10) : -1;
}

export function outputConnected(slot) {
    return Array.isArray(slot?.links) ? slot.links.length > 0 : slot?.link != null;
}

export function connectedSlotCeiling(node) {
    return Math.max(0, ...(node?.outputs || []).filter(outputConnected).map(slotNumber));
}

/** Canonical tuple order: the fixed block, then r01..r16. */
export function canonicalOutputOrder() {
    return [
        ...FIXED_OUTPUT_NAMES,
        ...Array.from({ length: MAX_REFERENCE_SLOTS }, (_, i) => slotName(i + 1)),
        ...Array.from({ length: MAX_REFERENCE_SLOTS }, (_, i) => promptSlotName(i + 1)),
    ];
}

/**
 * What the Reference Selector panel should display for a lane payload.
 *
 * Pure so the two cases that actually matter are testable: a selected lane the
 * scene no longer has (which must stay visible, or the dropdown would read as a
 * different lane than the INT holds), and the status line.
 */
export function selectorPanelView({ lanes = [], laneIndex = 0, status = "", sceneName = "", source = "live" } = {}) {
    const rows = Array.isArray(lanes) ? lanes : [];
    const selected = Math.max(0, parseInt(laneIndex, 10) || 0);
    const options = rows.map((lane) => ({
        value: lane.lane_index,
        label: `${lane.lane_name} — ${lane.recipe_name}`,
        orphan: false,
    }));
    if (rows.length && !rows.some((lane) => lane.lane_index === selected)) {
        options.push({
            value: selected,
            label: `Reference ${selected + 1} — no such lane in this scene`,
            orphan: true,
        });
    }
    const lane = rows.find((entry) => entry.lane_index === selected) || null;
    let statusText;
    if (status) {
        statusText = status;
    } else if (!lane) {
        statusText = `Lane ${selected} is not in ${sceneName || "this scene"}.`;
    } else {
        const parts = [
            String(lane.media_kind || "image"),
            `${lane.item_count} item${lane.item_count === 1 ? "" : "s"}`,
            `${lane.member_count} member${lane.member_count === 1 ? "" : "s"}`,
        ];
        if (lane.hidden) parts.push("lane hidden");
        // A running job froze this lane, so the panel is describing the snapshot
        // rather than what the timeline currently shows.
        if (source === "snapshot") parts.push("frozen job");
        statusText = parts.join(" · ");
    }
    return {
        options,
        selectedValue: selected,
        disabled: !rows.length,
        status: statusText,
        outputs: (lane?.live_outputs || []).map((name) => (name === "slots" ? "r01..r16" : name)),
        // The `sonder:` namespace is noise on a node this small; the tag name is
        // what identifies the reference.
        tags: (lane?.member_tags || []).map((tag) => String(tag).replace(/^sonder:/, "")),
    };
}

const outputIndex = (node, name) => (node.outputs || []).findIndex((slot) => slot?.name === name);

function ensureOutput(node, name, metadata, order) {
    if (outputIndex(node, name) >= 0) return;
    const meta = metadata?.get(name) || { type: "IMAGE" };
    node.addOutput?.(name, meta.type || "IMAGE", { label: meta.label, tooltip: meta.tooltip });
}

export const UNUSED_SUFFIX = " (unused)";

/**
 * Mark a dead output without touching its index, name or type.
 *
 * Writes BOTH `label` and `localized_name`: legacy LiteGraph draws `label`,
 * Nodes 2.0 reads `localized_name`, so setting only one leaves the other
 * renderer showing the bare name. Same pairing as `bridge_nodes.js` and
 * `autogrow_passthrough.js`.
 *
 * `ignoreConnection` decouples marking from wiring. Rule 1 in the module header
 * is about REMOVAL — a connected slot must survive — but it leaked into the
 * label too, and the two are not the same question. A wired output the recipe
 * does not drive is exactly the case worth announcing: it emits a type-correct
 * fallback (a black frame for `reference_frames`) into a live link under a
 * clean-looking name. `reference_frames` is wired in essentially every real
 * workflow, so the guard made the one output that most needed the mark the one
 * output that could never carry it.
 */
function markOutput(slot, live, metadata, { ignoreConnection = false } = {}) {
    if (!slot) return false;
    const meta = metadata?.get(slot.name);
    const captured = meta?.label ?? meta?.localized_name ?? slot.name;
    // Strip a suffix we previously wrote. `metadata` is captured from live
    // `node.outputs` at nodeCreated/loadedGraphNode, so a graph reloaded while a
    // slot was marked would otherwise re-mark an already-marked label and drift
    // to "name (unused) (unused)" on every load.
    const original = String(captured).endsWith(UNUSED_SUFFIX)
        ? String(captured).slice(0, -UNUSED_SUFFIX.length)
        : captured;
    const dead = live && !live.has(slot.name) && (ignoreConnection || !outputConnected(slot));
    const label = dead ? `${original}${UNUSED_SUFFIX}` : original;
    if (slot.label === label && slot.localized_name === label) return false;
    slot.label = label;
    slot.localized_name = label;
    return true;
}

/**
 * Reshape a bridge node's displayed outputs.
 *
 * Every output stays present at its declared index; unused ones are relabelled.
 * See the module header for why nothing may be removed.
 *
 * @param shape.slotCount        staged member count for the r-block — r slots above it read as unused
 * @param shape.promptSlotCount  staged member count for the p-block; absent means "we don't know"
 * @param shape.liveOutputs      fixed output names the recipe drives, or null for "show everything"
 * @param options.metadata       Map of name -> {type,label,tooltip} captured at node creation
 * @param options.order          canonical name order; defaults to the tuple order
 * @returns true when anything visible changed
 */
export function resolveBridgeOutputs(node, shape = {}, { metadata = null, order = null } = {}) {
    const {
        slotCount = 0, promptSlotCount = undefined, liveOutputs = null,
    } = typeof shape === "number" ? { slotCount: shape } : (shape || {});
    const canonical = order || canonicalOutputOrder();
    const signature = () => (node.outputs || [])
        .map((slot) => `${slot?.name}:${slot?.label ?? ""}:${slot?.localized_name ?? ""}`).join("|");
    const before = signature();
    const staged = Math.max(
        0,
        Math.min(MAX_REFERENCE_SLOTS, parseInt(slotCount, 10) || 0),
        connectedSlotCeiling(node),
    );
    // The p-block has its OWN count. Folding it into `staged` meant a recipe
    // driving reference_prompt without the r-block reported zero slots and
    // marked real per-member text as unused. Absent means "we don't know", so
    // the COUNT relaxes to the full block — but only the count: the
    // `reference_prompt` liveness check below still applies, and a null
    // `liveOutputs` still means show everything unmarked.
    const promptStaged = promptSlotCount === undefined || promptSlotCount === null
        ? MAX_REFERENCE_SLOTS
        : Math.max(
            0,
            Math.min(MAX_REFERENCE_SLOTS, parseInt(promptSlotCount, 10) || 0),
        );
    const live = Array.isArray(liveOutputs) ? new Set(liveOutputs) : null;

    for (const name of FIXED_OUTPUT_NAMES) {
        ensureOutput(node, name, metadata, canonical);
        // Fixed outputs mark on liveness alone. The numbered blocks below keep
        // the connection guard: they are what the socket-pair reorder will
        // redesign, so changing them is a separate decision. Note
        // `connectedSlotCeiling` reads SLOT_NAME_RE, which is r-only, so it
        // raises `staged` but never `promptStaged` — a wired p-slot is protected
        // by markOutput's own connection guard instead.
        markOutput(node.outputs[outputIndex(node, name)], live, metadata,
                   { ignoreConnection: true });
    }
    // Numbered slots past the staged member count carry a fallback rather than a
    // member, so they read as unused — but they keep their index, because the
    // p-block sits behind them and would otherwise slide into r-block positions.
    // Two passes, not one interleaved pass: ensureOutput appends, so creating
    // r01,p01,r02,p02… would build the wrong canonical order on a node that is
    // missing slots.
    for (let index = 1; index <= MAX_REFERENCE_SLOTS; index++) {
        ensureOutput(node, slotName(index), metadata, canonical);
    }
    for (let index = 1; index <= MAX_REFERENCE_SLOTS; index++) {
        ensureOutput(node, promptSlotName(index), metadata, canonical);
    }
    for (let index = 1; index <= MAX_REFERENCE_SLOTS; index++) {
        const imageLive = live ? (live.has("slots") && index <= staged) : true;
        const textLive = live ? (live.has("reference_prompt") && index <= promptStaged) : true;
        markOutput(node.outputs[outputIndex(node, slotName(index))], imageLive ? null : new Set(), metadata);
        markOutput(node.outputs[outputIndex(node, promptSlotName(index))], textLive ? null : new Set(), metadata);
    }
    return signature() !== before;
}
