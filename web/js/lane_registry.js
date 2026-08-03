import { TRACK_TYPE } from "./editor_timeline_constants.js";

export { TRACK_TYPE } from "./editor_timeline_constants.js";

const freeze = (value) => Object.freeze(value);

export const LANE_DESCRIPTORS = freeze([
    freeze({
        trackType: TRACK_TYPE.VIDEO,
        laneType: "video",
        variable: true,
        headerControllable: true,
        layoutOrder: 10,
        laneOrder: "desc",
        countField: "video_lane_count",
        configsField: "video_lane_configs",
        recipeAttr: "",
        fixedConfigField: "",
        labelPrefix: "V",
        labelSingular: "Video",
        labelFixed: "",
        menuLabel: "Video",
        logLabel: "video",
        color: freeze({ mode: "palette", key: "" }),
        accentColorKey: "laneVideo",
        itemsSource: freeze({
            listField: "clips",
            indexField: "track_index",
            idField: "clip_id",
            drawPredicate: "not_motion_driver",
            mutationPredicate: "render",
        }),
        visibilityIcons: freeze({ visible: "👁", hidden: "🚫" }),
        visibilityMode: "items",
        hasManageIcon: false,
        manageAction: "",
        dropAccepts: freeze({ video: "accept" }),
        maxItemsPerLane: null,
        supportsMultiLaneDelete: true,
        supportsCompaction: true,
        laneRemovable: true,
    }),
    freeze({
        trackType: TRACK_TYPE.AUDIO,
        laneType: "audio",
        variable: true,
        headerControllable: true,
        layoutOrder: 20,
        laneOrder: "asc",
        countField: "audio_lane_count",
        configsField: "audio_lane_configs",
        recipeAttr: "",
        fixedConfigField: "",
        labelPrefix: "A",
        labelSingular: "Audio",
        labelFixed: "",
        menuLabel: "Audio",
        logLabel: "audio",
        color: freeze({ mode: "palette", key: "" }),
        accentColorKey: "laneAudio",
        itemsSource: freeze({
            listField: "audio_tracks",
            indexField: "lane_index",
            idField: "track_id",
            drawPredicate: "all",
            mutationPredicate: "all",
        }),
        visibilityIcons: freeze({ visible: "🔊", hidden: "🔇" }),
        visibilityMode: "items",
        hasManageIcon: false,
        manageAction: "",
        dropAccepts: freeze({ audio: "accept", video: "accept_as_audio" }),
        maxItemsPerLane: null,
        supportsMultiLaneDelete: true,
        supportsCompaction: true,
        laneRemovable: true,
    }),
    freeze({
        trackType: TRACK_TYPE.MOTION_DRIVER,
        laneType: "motion_driver",
        variable: true,
        headerControllable: true,
        layoutOrder: 30,
        laneOrder: "asc",
        countField: "motion_driver_lane_count",
        configsField: "motion_driver_lane_configs",
        recipeAttr: "",
        fixedConfigField: "",
        labelPrefix: "Driver ",
        labelSingular: "Driver",
        labelFixed: "",
        menuLabel: "Driver",
        logLabel: "driver",
        color: freeze({ mode: "fixed", key: "laneDriver" }),
        accentColorKey: "laneDriver",
        itemsSource: freeze({
            listField: "clips",
            indexField: "track_index",
            idField: "clip_id",
            drawPredicate: "motion_driver",
            mutationPredicate: "motion_driver",
        }),
        visibilityIcons: freeze({ visible: "👁", hidden: "🚫" }),
        visibilityMode: "items",
        hasManageIcon: false,
        manageAction: "",
        dropAccepts: freeze({ video: "accept" }),
        maxItemsPerLane: 1,
        supportsMultiLaneDelete: false,
        supportsCompaction: false,
        laneRemovable: true,
    }),
    freeze({
        trackType: TRACK_TYPE.REFERENCE,
        laneType: "reference",
        variable: true,
        headerControllable: true,
        layoutOrder: 40,
        laneOrder: "asc",
        countField: "reference_lane_count",
        configsField: "reference_lane_configs",
        recipeAttr: "reference_lane_recipes",
        fixedConfigField: "",
        labelPrefix: "R",
        labelSingular: "Reference",
        labelFixed: "",
        menuLabel: "Reference",
        logLabel: "reference",
        color: freeze({ mode: "fixed", key: "laneReference" }),
        accentColorKey: "laneReference",
        itemsSource: freeze({
            listField: "reference_items",
            indexField: "lane_index",
            idField: "reference_item_id",
            drawPredicate: "all",
            mutationPredicate: "all",
        }),
        visibilityIcons: freeze({ visible: "👁", hidden: "🚫" }),
        visibilityMode: "items",
        hasManageIcon: true,
        manageAction: "references",
        dropAccepts: freeze({}),
        maxItemsPerLane: null,
        supportsMultiLaneDelete: false,
        supportsCompaction: false,
        laneRemovable: true,
    }),
    freeze({
        trackType: TRACK_TYPE.GUIDES,
        laneType: "guide",
        variable: false,
        headerControllable: true,
        layoutOrder: 50,
        laneOrder: "single",
        countField: "",
        configsField: "",
        recipeAttr: "",
        fixedConfigField: "guide_track_config",
        labelPrefix: "",
        labelSingular: "",
        labelFixed: "Guides",
        menuLabel: "Guides",
        logLabel: "guides",
        color: freeze({ mode: "none", key: "" }),
        accentColorKey: "laneGuide",
        itemsSource: freeze({
            listField: "guide_frames",
            indexField: "frame_index",
            idField: "guide_id",
            drawPredicate: "all",
            mutationPredicate: "all",
        }),
        visibilityIcons: freeze({ visible: "👁", hidden: "🚫" }),
        visibilityMode: "items",
        hasManageIcon: true,
        manageAction: "guides",
        dropAccepts: freeze({ image: "accept" }),
        maxItemsPerLane: null,
        supportsMultiLaneDelete: false,
        supportsCompaction: false,
        laneRemovable: false,
    }),
    freeze({
        trackType: TRACK_TYPE.PROMPT_GLOBAL,
        laneType: "prompt_global",
        variable: false,
        headerControllable: true,
        layoutOrder: 60,
        laneOrder: "single",
        countField: "",
        configsField: "",
        recipeAttr: "",
        fixedConfigField: "global_prompt_track_config",
        labelPrefix: "",
        labelSingular: "",
        labelFixed: "Global",
        menuLabel: "Global Prompt",
        logLabel: "global prompt",
        color: freeze({ mode: "none", key: "" }),
        // Preserve the current accent fallthrough to laneVideo.
        accentColorKey: "laneVideo",
        itemsSource: null,
        visibilityIcons: freeze({ visible: "🔊", hidden: "🔇" }),
        visibilityMode: "config_only",
        hasManageIcon: true,
        manageAction: "prompts",
        dropAccepts: freeze({}),
        maxItemsPerLane: null,
        supportsMultiLaneDelete: false,
        supportsCompaction: false,
        laneRemovable: false,
    }),
    freeze({
        trackType: TRACK_TYPE.PROMPT,
        laneType: "prompt",
        variable: false,
        headerControllable: true,
        layoutOrder: 70,
        laneOrder: "single",
        countField: "",
        configsField: "",
        recipeAttr: "",
        fixedConfigField: "prompt_track_config",
        labelPrefix: "",
        labelSingular: "",
        labelFixed: "Prompt",
        menuLabel: "Prompt",
        logLabel: "prompt",
        color: freeze({ mode: "none", key: "" }),
        accentColorKey: "lanePrompt",
        itemsSource: freeze({
            listField: "prompt_sections",
            indexField: "start_frame",
            idField: "prompt_id",
            drawPredicate: "all",
            mutationPredicate: "all",
        }),
        visibilityIcons: freeze({ visible: "🔊", hidden: "🔇" }),
        visibilityMode: "config_only",
        hasManageIcon: true,
        manageAction: "prompts",
        dropAccepts: freeze({}),
        maxItemsPerLane: null,
        supportsMultiLaneDelete: false,
        supportsCompaction: false,
        laneRemovable: false,
    }),
]);

export const RESERVED_REFERENCE_ORDER = 40;

const BY_TRACK_TYPE = new Map(LANE_DESCRIPTORS.map((descriptor) => [descriptor.trackType, descriptor]));
const BY_LANE_TYPE = new Map(LANE_DESCRIPTORS.map((descriptor) => [descriptor.laneType, descriptor]));

export const LAYOUT_ORDER = freeze(
    [...LANE_DESCRIPTORS]
        .sort((left, right) => left.layoutOrder - right.layoutOrder)
        .map((descriptor) => descriptor.trackType)
);
export const VARIABLE_TRACK_TYPES = freeze(
    LANE_DESCRIPTORS.filter((descriptor) => descriptor.variable).map((descriptor) => descriptor.trackType)
);

export function descriptorFor(trackType) {
    return BY_TRACK_TYPE.get(trackType) || null;
}

export function descriptorForLaneType(laneType) {
    return BY_LANE_TYPE.get(laneType) || null;
}

export function isVariableLane(trackType) {
    return descriptorFor(trackType)?.variable === true;
}

export function isHeaderControllable(trackType) {
    return descriptorFor(trackType)?.headerControllable === true;
}

export function laneTypeFor(trackType) {
    return descriptorFor(trackType)?.laneType || "";
}

export function variableLaneTypeFor(trackType) {
    const descriptor = descriptorFor(trackType);
    return descriptor?.variable ? descriptor.laneType : "";
}

export function laneLogLabel(trackType) {
    return descriptorFor(trackType)?.logLabel || "lane";
}

export function laneCountFor(scene, trackType) {
    const descriptor = descriptorFor(trackType);
    return descriptor?.variable ? (scene?.[descriptor.countField] || 1) : 1;
}

export function laneHiddenInScene(scene, trackType, laneIndex = 0) {
    const descriptor = descriptorFor(trackType);
    if (!descriptor) return false;
    const config = descriptor.variable
        ? scene?.[descriptor.configsField]?.[laneIndex]
        : scene?.[descriptor.fixedConfigField];
    return !!config?.hidden;
}

export function laneLabel(scene, trackType, laneIndex, customName = "") {
    if (customName) return customName;
    const descriptor = descriptorFor(trackType);
    if (!descriptor) return "";
    if (!descriptor.variable) return descriptor.labelFixed;
    const count = laneCountFor(scene, trackType);
    return count > 1 ? `${descriptor.labelPrefix}${laneIndex + 1}` : descriptor.labelSingular;
}

export function isRenderClip(clip) {
    return !clip?.role || clip.role === "render";
}

export function isMotionDriverClip(clip) {
    return clip?.role === "motion_driver";
}

export function trackTypeForClip(clip) {
    // Preserve the draw-path fallback: unknown roles land on video here.
    return isMotionDriverClip(clip) ? TRACK_TYPE.MOTION_DRIVER : TRACK_TYPE.VIDEO;
}

function predicateMatches(item, predicate) {
    if (predicate === "not_motion_driver") return !isMotionDriverClip(item);
    if (predicate === "render") return isRenderClip(item);
    if (predicate === "motion_driver") return isMotionDriverClip(item);
    return predicate === "all";
}

function itemsFor(scene, descriptor, laneIndex, predicateKey) {
    const source = descriptor?.itemsSource;
    if (!scene || !source) return [];
    const items = scene[source.listField] || [];
    const predicate = source[predicateKey];
    if (!descriptor.variable || laneIndex === null) {
        return items.filter((item) => predicateMatches(item, predicate));
    }
    return items.filter((item) => predicateMatches(item, predicate)
        && (item?.[source.indexField] || 0) === laneIndex);
}

export function laneItemsForType(scene, trackType, laneIndex) {
    return itemsFor(scene, descriptorFor(trackType), laneIndex, "mutationPredicate");
}

export function trackItemsForEntry(scene, entry) {
    return itemsFor(scene, descriptorFor(entry?.type), entry?.laneIndex || 0, "drawPredicate");
}

function fallbackColor(descriptor, laneIndex, theme) {
    if (descriptor.color.mode === "palette") {
        const palette = theme?.palette || [];
        return palette.length ? palette[laneIndex % palette.length] : "";
    }
    if (descriptor.color.mode === "fixed") return theme?.fixed?.[descriptor.color.key] || "";
    return "";
}

export function buildTrackLayout({ scene, collapsedKeys = null, theme = {} } = {}) {
    const layout = [];
    for (const descriptor of [...LANE_DESCRIPTORS].sort((left, right) => left.layoutOrder - right.layoutOrder)) {
        if (descriptor.variable) {
            const laneCount = scene?.[descriptor.countField] || 1;
            const configs = scene?.[descriptor.configsField] || [];
            const start = descriptor.laneOrder === "desc" ? laneCount - 1 : 0;
            const step = descriptor.laneOrder === "desc" ? -1 : 1;
            const inRange = descriptor.laneOrder === "desc"
                ? (laneIndex) => laneIndex >= 0
                : (laneIndex) => laneIndex < laneCount;
            for (let laneIndex = start; inRange(laneIndex); laneIndex += step) {
                const config = configs[laneIndex] || {};
                layout.push({
                    type: descriptor.trackType,
                    label: config.name || laneLabel(scene, descriptor.trackType, laneIndex),
                    customName: config.name || "",
                    laneIndex,
                    collapsed: collapsedKeys?.has(`${descriptor.trackType}:${laneIndex}`) || false,
                    color: config.color || fallbackColor(descriptor, laneIndex, theme),
                    locked: config.locked || false,
                    hidden: config.hidden || false,
                    referenceRecipe: descriptor.recipeAttr
                        ? (scene?.[descriptor.recipeAttr]?.[laneIndex] || { media_kind: "image", recipe_id: "", recipe: {} })
                        : null,
                });
            }
            continue;
        }

        const config = scene?.[descriptor.fixedConfigField] || {};
        layout.push({
            type: descriptor.trackType,
            label: descriptor.labelFixed,
            customName: "",
            laneIndex: 0,
            collapsed: collapsedKeys?.has(`${descriptor.trackType}:0`) || false,
            color: "",
            locked: !!config.locked,
            hidden: !!config.hidden,
            referenceRecipe: null,
        });
    }
    return layout;
}

export function laneLayoutIndex(layout, trackType, laneIndex, { animaticMode = false } = {}) {
    // Null is the explicit ephemeral animatic-video suppression sentinel.
    // Every registered track resolves through its own descriptor identity.
    if (animaticMode && trackType === TRACK_TYPE.VIDEO) return null;
    const resolvedType = descriptorFor(trackType)?.trackType || trackType;
    return (layout || []).findIndex(
        (entry) => entry.type === resolvedType && entry.laneIndex === laneIndex
    );
}

export function laneAcceptsAssetType(trackType, assetType, { hasAudio = false } = {}) {
    if (!assetType) {
        const accepts = descriptorFor(trackType)?.dropAccepts || {};
        return isVariableLane(trackType) && Object.keys(accepts).length ? "accept" : "reject";
    }
    const result = descriptorFor(trackType)?.dropAccepts?.[assetType] || "reject";
    return result === "accept_as_audio" && !hasAudio ? "reject" : result;
}
