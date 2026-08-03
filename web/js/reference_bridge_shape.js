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
// So the fixed block is never removed. Outputs a recipe does not drive are
// RELABELLED as unused, which changes nothing about index, name or type. Only
// the r-block shrinks, because it is a contiguous TAIL: trimming r05..r16 moves
// no surviving slot.
//
// Two further rules:
//   1. A connected slot is never removed, whatever the recipe says. Removing it
//      would silently drop the user's link.
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
    return [...FIXED_OUTPUT_NAMES, ...Array.from({ length: MAX_REFERENCE_SLOTS }, (_, i) => slotName(i + 1))];
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
    };
}

const outputIndex = (node, name) => (node.outputs || []).findIndex((slot) => slot?.name === name);

function ensureOutput(node, name, metadata, order) {
    if (outputIndex(node, name) >= 0) return;
    const meta = metadata?.get(name) || { type: "IMAGE" };
    node.addOutput?.(name, meta.type || "IMAGE", { label: meta.label, tooltip: meta.tooltip });
}

export const UNUSED_SUFFIX = " (unused)";

/** Mark a dead output without touching its index, name or type. */
function markOutput(slot, live, metadata) {
    if (!slot) return false;
    const original = metadata?.get(slot.name)?.label ?? slot.name;
    const dead = live && !live.has(slot.name) && !outputConnected(slot);
    const label = dead ? `${original}${UNUSED_SUFFIX}` : original;
    if (slot.label === label) return false;
    slot.label = label;
    return true;
}

/**
 * Reshape a bridge node's displayed outputs.
 *
 * The fixed block is only ever relabelled, never removed — see the module
 * header. Only the r-block, a contiguous tail, actually shrinks.
 *
 * @param shape.slotCount   effective r-block size (0 when the recipe never uses it)
 * @param shape.liveOutputs fixed output names the recipe drives, or null for "show everything"
 * @param options.metadata  Map of name -> {type,label,tooltip} captured at node creation
 * @param options.order     canonical name order; defaults to the tuple order
 * @returns true when anything visible changed
 */
export function resolveBridgeOutputs(node, shape = {}, { metadata = null, order = null } = {}) {
    const { slotCount = 0, liveOutputs = null } = typeof shape === "number" ? { slotCount: shape } : (shape || {});
    const canonical = order || canonicalOutputOrder();
    const signature = () => (node.outputs || []).map((slot) => `${slot?.name}:${slot?.label ?? ""}`).join("|");
    const before = signature();
    const desired = Math.max(
        0,
        Math.min(MAX_REFERENCE_SLOTS, parseInt(slotCount, 10) || 0),
        connectedSlotCeiling(node),
    );
    const live = Array.isArray(liveOutputs) ? new Set(liveOutputs) : null;

    // Fixed outputs: always present, relabelled when the recipe does not drive
    // them. Removing one would shift every later slot into a position whose
    // declared type belongs to a different output.
    for (const name of FIXED_OUTPUT_NAMES) {
        ensureOutput(node, name, metadata, canonical);
        markOutput(node.outputs[outputIndex(node, name)], live, metadata);
    }
    // The r-block is a tail, so it can genuinely shrink. `slots` liveness is
    // already folded into slotCount by the caller.
    for (let index = 1; index <= desired; index++) {
        ensureOutput(node, slotName(index), metadata, canonical);
    }
    for (let index = (node.outputs || []).length - 1; index >= 0; index--) {
        const slot = node.outputs[index];
        if (slotNumber(slot) > desired && !outputConnected(slot)) node.removeOutput?.(index);
    }
    return signature() !== before;
}
