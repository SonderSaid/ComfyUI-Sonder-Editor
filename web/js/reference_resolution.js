// Bridge output names in tuple order, plus the "slots" pseudo-name covering the
// whole r01..r16 block. Mirrored in server/reference_resolution.py.
export const REFERENCE_OUTPUT_NAMES = [
    "reference_frames",
    "reference_idx",
    "reference_strength",
    "reference_audio",
    "reference_prompt",
    "reference_names",
    "context",
    "slots",
];

// A recipe without a `live_outputs` declaration (detached/custom) keeps every
// output live; anything else is dead and emits its documented fallback.
export function referenceLiveOutputs(hard) {
    const declared = hard?.live_outputs;
    if (!Array.isArray(declared)) return new Set(REFERENCE_OUTPUT_NAMES);
    return new Set(declared.filter((name) => REFERENCE_OUTPUT_NAMES.includes(name)));
}

/**
 * The prompt a staged item contributes, mirroring `_assemble_prompt` in
 * `nodes/reference_core.py`. The panel shows this so the user can read and copy
 * what actually reaches the model before deciding to override it.
 *
 * `members` are pre-resolved `{name, prompt}` pairs in staged slot order.
 */
export function deriveReferencePrompt({ promptOverride = "", members = [], soft = {} } = {}) {
    const override = String(promptOverride || "").trim();
    if (override) return override;
    const tokenPattern = String(soft?.prompt_tokens || "");
    const values = [];
    (Array.isArray(members) ? members : []).forEach((entry, index) => {
        const memberPrompt = String(entry?.prompt || "").trim();
        const name = String(entry?.name || "").trim();
        let label = memberPrompt || name;
        if (tokenPattern) {
            // Python's str.replace substitutes every occurrence; JS String.replace
            // with a string literal would only substitute the first.
            const token = tokenPattern.split("{index}").join(String(index));
            label = label ? `${token}: ${label}` : token;
        }
        if (label) values.push(label);
    });
    const prefix = String(soft?.prompt_prefix || "").trim();
    const joined = values.join(", ");
    return [prefix, joined].filter(Boolean).join(" ");
}

function integer(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.trunc(number) : fallback;
}

export function resolveEffectiveReferences({
    referenceItems = [],
    laneCount = 1,
    sceneDuration = 0,
    windowStart = 0,
    windowEnd = sceneDuration,
    laneConfigs = [],
} = {}) {
    const count = Math.max(1, integer(laneCount, 1));
    const duration = Math.max(0, integer(sceneDuration, 0));
    const start = Math.min(duration, Math.max(0, integer(windowStart, 0)));
    const end = Math.min(duration, Math.max(start, integer(windowEnd, duration)));
    const items = Array.isArray(referenceItems) ? referenceItems : [];
    const configs = Array.isArray(laneConfigs) ? laneConfigs : [];
    const winners = Array(count).fill(null);
    const scores = Array(count).fill(null);

    items.forEach((item, itemIndex) => {
        const laneIndex = integer(item?.lane_index, 0);
        if (laneIndex < 0 || laneIndex >= count || item?.muted || configs[laneIndex]?.hidden) return;
        const itemStart = Math.min(
            Math.max(0, integer(item?.start_frame, 0)),
            Math.max(0, duration - 1),
        );
        const rawEnd = integer(item?.end_frame, -1);
        const itemEnd = rawEnd < 0 ? duration : Math.min(duration, rawEnd);
        if (itemEnd <= itemStart) return;
        const overlap = Math.max(0, Math.min(itemEnd, end) - Math.max(itemStart, start));
        if (overlap <= 0) return;
        const score = [overlap / (itemEnd - itemStart), itemStart, itemIndex];
        const previous = scores[laneIndex];
        const wins = !previous
            || score[0] > previous[0]
            || (score[0] === previous[0] && score[1] > previous[1])
            || (score[0] === previous[0] && score[1] === previous[1] && score[2] > previous[2]);
        if (wins) {
            scores[laneIndex] = score;
            winners[laneIndex] = { laneIndex, item };
        }
    });
    return winners;
}
