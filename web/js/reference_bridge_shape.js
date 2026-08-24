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
//      project, an unwired selector, a lane index pointing at nothing and a
//      failed fetch are all "we don't know", not "this recipe drives nothing" —
//      a slow load must not look like a broken node.
//
// The backend always returns the full tuple. Each Image/Audio Bridge's
// workflow-embedded emission policy decides whether a dead slot carries its
// type-correct placeholder or no value.

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
        outputs: (lane?.live_outputs || []).map((name) => ({
            image_slots: "Image Bridge r01..r16",
            audio_slots: "Audio Bridge a01..a16",
            reference_prompt: "Prompt Bridge aggregate + p01..p16",
            reference_names: "Prompt Bridge reference_names",
        }[name] || name)),
        // The `sonder:` namespace is noise on a node this small; the tag name is
        // what identifies the reference.
        tags: (lane?.member_tags || []).map((tag) => String(tag).replace(/^sonder:/, "")),
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
        const slotLabels = Array.isArray(shape?.slotLabels) ? shape.slotLabels : [];
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
