// Which Reference Bridge outputs LiteGraph displays, and how dead ones read.
//
// THE SLOT INDEX IS THE PAYLOAD CONTRACT. Each replacement bridge has one
// homogeneous numbered tuple, so removing its true tail cannot create a type
// mismatch. It can still create a semantic shift if a lower hole is removed,
// so the displayed block never shrinks below its highest connected slot.
// Removal runs high-to-low above max(staged count, connected-slot ceiling).
//
// Two further rules:
//   1. A connected-but-unstaged slot survives and is MARKED unused. Wiring says
//      the user connected something, not that the recipe currently drives it.
//   2. No liveness declaration means show everything unmarked. An unresolved
//      project, an unwired selector, a selection containing only orphan lanes and a
//      failed fetch are all "we don't know", not "this recipe drives nothing" —
//      a slow load must not look like a broken node.
//
// The backend always returns the full tuple. Each Image/Audio Bridge's
// workflow-embedded emission policy decides whether a dead slot carries its
// type-correct placeholder or no value.

import { formatReferenceTag } from "./reference_library_model.js";

export const MAX_REFERENCE_SLOTS = 16;
export const SLOT_NAME_RE = /^[rap](0[1-9]|1[0-6])$/;

export const BRIDGE_CONFIGS = Object.freeze({
    SonderReferenceImageBridge: Object.freeze({
        prefix: "r", fixed: [], liveName: "image_slots", countKey: "imageSlotCount", type: "IMAGE",
    }),
    SonderReferenceAudioBridge: Object.freeze({
        prefix: "a", fixed: [], liveName: "audio_slots", countKey: "audioSlotCount", type: "AUDIO",
    }),
    SonderReferencePromptBridge: Object.freeze({
        prefix: "p", fixed: ["reference_prompt", "reference_names"],
        liveName: "reference_prompt", countKey: "promptSlotCount", type: "STRING",
    }),
});

export const slotName = (index) => `r${String(index).padStart(2, "0")}`;
export const audioSlotName = (index) => `a${String(index).padStart(2, "0")}`;
export const promptSlotName = (index) => `p${String(index).padStart(2, "0")}`;
export const numberedSlotName = (prefix, index) => `${prefix}${String(index).padStart(2, "0")}`;

export function slotNumber(slot) {
    const match = SLOT_NAME_RE.exec(String(slot?.name || ""));
    return match ? parseInt(match[1], 10) : -1;
}

export function outputConnected(slot) {
    return Array.isArray(slot?.links) ? slot.links.length > 0 : slot?.link != null;
}

export function connectedSlotCeiling(node, prefix = null) {
    return Math.max(0, ...(node?.outputs || [])
        .filter((slot) => outputConnected(slot) && (!prefix || String(slot?.name || "").startsWith(prefix)))
        .map(slotNumber));
}

const namedEntries = (value) => (
    value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : []
);

/** Reduce one /object_info node definition to the input facts this module models. */
export function distillInputDefinition(nodeData = {}) {
    const input = nodeData?.input && typeof nodeData.input === "object" ? nodeData.input : {};
    const requiredEntries = namedEntries(input.required);
    const optionalEntries = namedEntries(input.optional);
    const autogrow = [];
    for (const [, value] of [...requiredEntries, ...optionalEntries]) {
        const options = Array.isArray(value) && value[1] && typeof value[1] === "object" ? value[1] : null;
        const template = options?.template;
        if (!template || typeof template !== "object") continue;

        let templateRequired = null;
        for (const [category, entries] of namedEntries(template.input)) {
            if (!entries || typeof entries !== "object" || !Object.keys(entries).length) continue;
            templateRequired = category === "required";
            break;
        }
        // An Autogrow shape with no readable template input is unknown. Failing
        // quiet is safer than labelling a valid graph as broken.
        if (templateRequired === null) continue;

        const names = Array.isArray(template.names)
            ? template.names.map((name) => String(name))
            : null;
        const prefix = typeof template.prefix === "string" ? template.prefix : null;
        if (!names && prefix === null) continue;
        const parsedMin = Number(template.min);
        const parsedMax = Number(template.max);
        autogrow.push({
            prefix,
            names,
            min: Number.isInteger(parsedMin) && parsedMin >= 0 ? parsedMin : 0,
            max: names
                ? names.length
                : (Number.isInteger(parsedMax) && parsedMax >= 1 ? parsedMax : 0),
            required: templateRequired,
        });
    }
    return {
        required: new Set(requiredEntries.map(([name]) => name)),
        optional: new Set(optionalEntries.map(([name]) => name)),
        autogrow,
    };
}

/** Return required/optional only when the captured input shape proves it. */
export function inputRequirement(definition, inputName) {
    const name = String(inputName || "");
    if (!definition || !name) return "unknown";
    if (definition.required instanceof Set && definition.required.has(name)) return "required";
    if (definition.optional instanceof Set && definition.optional.has(name)) return "optional";
    for (const template of Array.isArray(definition.autogrow) ? definition.autogrow : []) {
        let index = -1;
        if (Array.isArray(template?.names)) {
            index = template.names.indexOf(name);
        } else if (typeof template?.prefix === "string" && name.startsWith(template.prefix)) {
            const suffix = name.slice(template.prefix.length);
            if (/^\d+$/.test(suffix)) index = Number(suffix);
            if (!Number.isInteger(index) || index < 0 || index >= Number(template.max)) index = -1;
        }
        if (index < 0) continue;
        return template.required && index < Number(template.min) ? "required" : "optional";
    }
    return "unknown";
}

/** Canonical tuple order for one homogeneous bridge. */
export function canonicalOutputOrder(bridgeType = "SonderReferenceImageBridge") {
    const config = BRIDGE_CONFIGS[bridgeType] || BRIDGE_CONFIGS.SonderReferenceImageBridge;
    return [
        ...config.fixed,
        ...Array.from({ length: MAX_REFERENCE_SLOTS }, (_, i) => numberedSlotName(config.prefix, i + 1)),
    ];
}

export function parseLaneSelection(value) {
    const authored = String(value ?? "");
    const laneIndices = [];
    const invalidTokens = [];
    const seen = new Set();
    for (const token of authored.trim().split(/[, \t\r\n\f\v]+/).filter(Boolean)) {
        if (/^-[0-9]+$/.test(token)) continue;
        if (!/^[0-9]+$/.test(token)) {
            invalidTokens.push(token);
            continue;
        }
        const index = Number(token);
        if (!Number.isSafeInteger(index)) {
            invalidTokens.push(token);
            continue;
        }
        if (!seen.has(index)) {
            seen.add(index);
            laneIndices.push(index);
        }
    }
    laneIndices.sort((left, right) => left - right);
    return { authored, laneIndices, invalidTokens };
}

const stableJson = (value) => {
    if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
    if (value && typeof value === "object") {
        return `{${Object.keys(value).sort().map((key) => (
            `${JSON.stringify(key)}:${stableJson(value[key])}`
        )).join(",")}}`;
    }
    return JSON.stringify(value);
};

const laneReservedSpan = (lane) => Math.max(0, parseInt(lane?.reserved_member_span, 10) || 0);
const laneMemberCap = (lane) => Math.max(
    1,
    Math.min(MAX_REFERENCE_SLOTS, parseInt(lane?.recipe?.hard?.max_members, 10) || MAX_REFERENCE_SLOTS),
);

const selectedLaneRows = (lanes, laneIndices) => {
    const byIndex = new Map(lanes.map((lane) => [Number(lane?.lane_index), lane]));
    return laneIndices.map((laneIndex) => ({ laneIndex, lane: byIndex.get(laneIndex) || null }));
};

/**
 * What the Reference Selector panel should display for a lane payload.
 *
 * Pure so orphaned authored indices remain visible instead of making the panel
 * appear to select different lanes than the workflow string actually names.
 */
export function selectorPanelView({
    lanes = [], laneIndices = [], invalidTokens = [], status = "", sceneName = "", source = "live",
    tagPresets = [], tagFamilies = {},
} = {}) {
    const available = Array.isArray(lanes) ? lanes : [];
    const selected = [...new Set((Array.isArray(laneIndices) ? laneIndices : [])
        .map((value) => parseInt(value, 10)).filter((value) => Number.isInteger(value) && value >= 0))]
        .sort((left, right) => left - right);
    const selectedRows = selectedLaneRows(available, selected).map(({ laneIndex, lane }) => {
        if (!lane) {
            return {
                laneIndex, lane: null, orphan: true,
                label: `Reference ${laneIndex + 1} — no such lane in ${sceneName || "this scene"}`,
                status: `Lane ${laneIndex} is not in ${sceneName || "this scene"}.`,
            };
        }
        const reserved = laneReservedSpan(lane);
        const parts = [
            String(lane.media_kind || "image"),
            `${lane.item_count} item${lane.item_count === 1 ? "" : "s"}`,
            `${lane.member_count} member${lane.member_count === 1 ? "" : "s"}`,
            `${reserved} reserved`,
        ];
        if (lane.hidden) parts.push("lane hidden");
        if (source === "snapshot") parts.push("frozen job");
        return {
            laneIndex, lane, orphan: false,
            label: `${lane.lane_name} — ${lane.recipe_name}`,
            status: parts.join(" · "),
        };
    });
    const anchor = selectedRows.find((row) => row.lane)?.lane || null;
    const effectiveAnchor = selectedRows.find((row) => row.lane?.item_count)?.lane || null;
    const selectedTotal = selectedRows.reduce((total, row) => total + laneReservedSpan(row.lane), 0);
    const anchorRecipe = anchor?.recipe && typeof anchor.recipe === "object" ? anchor.recipe : {};
    const anchorDetached = Boolean(anchor) && (!String(anchor.recipe_id || "") || !Object.keys(anchorRecipe).length);
    const budget = anchor ? Math.min(MAX_REFERENCE_SLOTS, laneMemberCap(anchor)) : MAX_REFERENCE_SLOTS;
    const selectedSet = new Set(selected);
    const addable = available
        .filter((lane) => !selectedSet.has(Number(lane?.lane_index)))
        .map((lane) => {
            const reasons = [];
            const candidateRecipe = lane?.recipe && typeof lane.recipe === "object" ? lane.recipe : {};
            const candidateDetached = !String(lane?.recipe_id || "") || !Object.keys(candidateRecipe).length;
            if (anchorDetached) reasons.push("The selected blank or detached recipe must stay alone.");
            if (anchor && String(lane?.media_kind || "image") !== String(anchor.media_kind || "image")) {
                reasons.push("Media kind differs from the selected lanes.");
            }
            if (anchor && stableJson(candidateRecipe) !== stableJson(anchorRecipe)) {
                reasons.push("Materialized recipe differs from the selected lanes.");
            }
            if (anchor && candidateDetached) reasons.push("A blank or detached recipe can only be selected alone.");
            if (effectiveAnchor && lane?.item_count
                    && String(lane?.prompt_override || "") !== String(effectiveAnchor.prompt_override || "")) {
                reasons.push("Prompt override differs from the effective selected lanes.");
            }
            const nextTotal = selectedTotal + laneReservedSpan(lane);
            if (nextTotal > budget || laneReservedSpan(lane) > laneMemberCap(lane)) {
                reasons.push(`The reserved total would exceed ${budget} slots.`);
            }
            return {
                laneIndex: Number(lane?.lane_index), lane,
                label: `${lane?.lane_name || `Reference ${Number(lane?.lane_index) + 1}`} — ${lane?.recipe_name || "Detached / Custom"}`,
                disabled: reasons.length > 0,
                reason: reasons.join(" "),
            };
        });
    const disclosures = [];
    for (const row of selectedRows) {
        if (row.lane && !row.lane.item_count && laneReservedSpan(row.lane) > 0) {
            disclosures.push(`Lane ${row.laneIndex} is inactive in this window; its ${laneReservedSpan(row.lane)} slots remain reserved.`);
        }
    }
    const strengths = selectedRows
        .filter((row) => row.lane?.item_count)
        .map((row) => Number(row.lane?.strength || 0));
    if (new Set(strengths.map((value) => String(value))).size > 1) {
        disclosures.push("Effective selected lanes use different strengths; the first effective lane drives reference_strength.");
    }
    if (invalidTokens.length) {
        disclosures.push(`Ignored unparseable lane token${invalidTokens.length === 1 ? "" : "s"}: ${invalidTokens.join(", ")}.`);
    }
    const selectedLanes = selectedRows.map((row) => row.lane).filter(Boolean);
    return {
        laneIndices: selected,
        rows: selectedRows,
        addable,
        disabled: !available.length,
        status: status || (!selected.length ? "No Reference lanes selected." : ""),
        disclosures,
        outputs: [...new Set(selectedLanes.flatMap((lane) => lane?.live_outputs || []))].map((name) => ({
            image_slots: "Image Bridge r01..r16",
            audio_slots: "Audio Bridge a01..a16",
            reference_prompt: "Prompt Bridge aggregate + p01..p16",
            reference_names: "Prompt Bridge reference_names",
        }[name] || name)),
        tags: [...new Set(selectedLanes.flatMap((lane) => lane?.member_tags || []))]
            .map((tag) => formatReferenceTag(tag, {
                catalog: tagPresets,
                families: tagFamilies,
                density: "short",
            })),
    };
}

export function mergedBridgeShape({ lanes = [], laneIndices = [] } = {}) {
    const available = Array.isArray(lanes) ? lanes : [];
    const selected = [...new Set((Array.isArray(laneIndices) ? laneIndices : [])
        .map((value) => parseInt(value, 10)).filter((value) => Number.isInteger(value) && value >= 0))]
        .sort((left, right) => left - right);
    const resolved = selectedLaneRows(available, selected).map((row) => row.lane).filter(Boolean);
    const padLabels = (values, count) => Array.from(
        { length: Math.max(0, count) }, (_, index) => String(values?.[index] || "(unused)"),
    );
    return {
        imageSlotCount: resolved.reduce((total, lane) => total + Math.max(0, parseInt(lane?.image_slot_count, 10) || 0), 0),
        audioSlotCount: resolved.reduce((total, lane) => total + Math.max(0, parseInt(lane?.audio_slot_count, 10) || 0), 0),
        promptSlotCount: resolved.reduce((total, lane) => total + Math.max(0, parseInt(lane?.prompt_slot_count, 10) || 0), 0),
        liveOutputs: resolved.length
            ? [...new Set(resolved.flatMap((lane) => lane?.live_outputs || []))]
            : (selected.length ? null : []),
        slotLabels: resolved.flatMap((lane) => padLabels(lane?.slot_labels, laneReservedSpan(lane))),
        imageSlotLabels: resolved.flatMap((lane) => padLabels(
            lane?.image_slot_labels,
            Math.max(0, parseInt(lane?.image_slot_count, 10) || 0),
        )),
    };
}

const outputIndex = (node, name) => (node.outputs || []).findIndex((slot) => slot?.name === name);

function ensureOutput(node, name, metadata, order) {
    if (outputIndex(node, name) >= 0) return;
    const fallbackType = name.startsWith("a") ? "AUDIO"
        : name.startsWith("p") || name.startsWith("reference_") ? "STRING" : "IMAGE";
    const meta = metadata?.get(name) || { type: fallbackType };
    // All dynamic additions use LiteGraph's {label, tooltip} option keys.
    node.addOutput?.(name, meta.type || "IMAGE", { label: meta.label, tooltip: meta.tooltip });
    const rank = new Map((order || []).map((entry, index) => [entry, index]));
    node.outputs?.sort((left, right) => (
        (rank.get(left?.name) ?? Number.MAX_SAFE_INTEGER)
        - (rank.get(right?.name) ?? Number.MAX_SAFE_INTEGER)
    ));
}

export const UNUSED_SUFFIX = " (unused)";
export const UNUSED_REQUIRED_SUFFIX = " (unused · required input)";

/**
 * Mark a dead output without touching its index, name or type.
 *
 * Writes BOTH `label` and `localized_name`: legacy LiteGraph draws `label`,
 * Nodes 2.0 reads `localized_name`, so setting only one leaves the other
 * renderer showing the bare name. Same pairing as `bridge_nodes.js` and
 * `autogrow_passthrough.js`.
 *
 * `ignoreConnection` decouples marking from wiring. A connected slot must
 * survive, but a connected output the recipe does not drive is exactly the case
 * worth announcing because its selected fallback reaches a live link.
 */
function markOutput(
    slot,
    live,
    metadata,
    { ignoreConnection = false, authoredLabel = "", deadSuffix = UNUSED_SUFFIX } = {},
) {
    if (!slot) return false;
    const meta = metadata?.get(slot.name);
    const captured = meta?.label ?? meta?.localized_name ?? slot.name;
    // Strip a suffix we previously wrote. `metadata` is captured from live
    // `node.outputs` at nodeCreated/loadedGraphNode, so a graph reloaded while a
    // slot was marked would otherwise re-mark an already-marked label and drift
    // to "name (unused) (unused)" on every load.
    const original = authoredLabel || (String(captured).endsWith(UNUSED_SUFFIX)
        ? String(captured).slice(0, -UNUSED_SUFFIX.length)
        : captured);
    const dead = live && !live.has(slot.name) && (ignoreConnection || !outputConnected(slot));
    const label = dead ? `${original}${deadSuffix}` : original;
    if (slot.label === label && slot.localized_name === label) return false;
    slot.label = label;
    slot.localized_name = label;
    return true;
}

/**
 * Reshape a bridge node's displayed outputs.
 *
 * Each homogeneous numbered block shrinks only from its real tail. A connected
 * slot pins the ceiling so no surviving Reference can shift to another link.
 *
 * @param shape.imageSlotCount   staged member count for the r-block
 * @param shape.audioSlotCount   staged member count for the a-block
 * @param shape.promptSlotCount  staged member count for the p-block
 * @param shape.liveOutputs      fixed output names the recipe drives, or null for "show everything"
 * @param shape.unusedSlots      workflow policy: placeholder or nothing
 * @param shape.requiredConsumerSlots numbered outputs wired to proven-required inputs
 * @param options.metadata       Map of name -> {type,label,tooltip} captured at node creation
 * @param options.order          canonical name order; defaults to the tuple order
 * @returns true when anything visible changed
 */
export function resolveBridgeOutputs(node, shape = {}, { metadata = null, order = null } = {}) {
    {
        const bridgeType = String(node?.comfyClass || node?.type || "SonderReferenceImageBridge");
        const config = BRIDGE_CONFIGS[bridgeType] || BRIDGE_CONFIGS.SonderReferenceImageBridge;
        const canonical = order || canonicalOutputOrder(bridgeType);
        const signature = () => (node.outputs || [])
            .map((slot) => `${slot?.name}:${slot?.label ?? ""}:${slot?.localized_name ?? ""}`).join("|");
        const before = signature();
        const live = Array.isArray(shape?.liveOutputs) ? new Set(shape.liveOutputs) : null;
        const requested = Math.max(
            0,
            Math.min(MAX_REFERENCE_SLOTS, parseInt(shape?.[config.countKey], 10) || 0),
        );
        const connected = connectedSlotCeiling(node, config.prefix);
        const ceiling = live === null
            ? MAX_REFERENCE_SLOTS
            : Math.max(connected, live.has(config.liveName) ? requested : 0);
        const labelKey = config.prefix === "r" ? "imageSlotLabels" : "slotLabels";
        const slotLabels = Array.isArray(shape?.[labelKey])
            ? shape[labelKey]
            : Array.isArray(shape?.slotLabels) ? shape.slotLabels : [];
        const unusedSlots = String(shape?.unusedSlots || "placeholder");
        const requiredConsumerSlots = new Set(
            Array.isArray(shape?.requiredConsumerSlots) ? shape.requiredConsumerSlots : [],
        );

        for (const name of config.fixed) {
            ensureOutput(node, name, metadata, canonical);
            const fixedLive = live === null || live.has(name);
            markOutput(
                node.outputs[outputIndex(node, name)],
                fixedLive ? null : new Set(),
                metadata,
                { ignoreConnection: true },
            );
        }
        for (let index = 1; index <= ceiling; index++) {
            ensureOutput(node, numberedSlotName(config.prefix, index), metadata, canonical);
        }
        // Homogeneous tuples make tail removal index-safe. The ceiling, not a
        // per-slot connection check, prevents holes and silent reference shifts.
        for (let outputIndexValue = (node.outputs || []).length - 1; outputIndexValue >= 0; outputIndexValue--) {
            const slot = node.outputs[outputIndexValue];
            if (!String(slot?.name || "").startsWith(config.prefix)) continue;
            if (slotNumber(slot) > ceiling) node.removeOutput?.(outputIndexValue);
        }
        for (let index = 1; index <= ceiling; index++) {
            const name = numberedSlotName(config.prefix, index);
            const labelSuffix = String(slotLabels[index - 1] || "").trim();
            const authoredLabel = labelSuffix ? `${name} · ${labelSuffix}` : name;
            const numberedLive = live === null || (live.has(config.liveName) && index <= requested);
            const slot = node.outputs[outputIndex(node, name)];
            const warnsRequired = !numberedLive
                && unusedSlots === "nothing"
                && outputConnected(slot)
                && requiredConsumerSlots.has(name);
            markOutput(
                slot,
                numberedLive ? null : new Set(),
                metadata,
                {
                    ignoreConnection: true,
                    authoredLabel,
                    deadSuffix: warnsRequired ? UNUSED_REQUIRED_SUFFIX : UNUSED_SUFFIX,
                },
            );
        }
        return signature() !== before;
    }
}
