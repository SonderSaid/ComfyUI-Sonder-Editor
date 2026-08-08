// Type-homogeneous Bridge output groups. Mirrored in
// server/reference_resolution.py.
export const REFERENCE_OUTPUT_NAMES = [
    "image_slots",
    "audio_slots",
    "reference_prompt",
    "reference_names",
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
/**
 * Expand one member's slice of the derived prompt. Mirrors
 * `member_prompt_fragment` in nodes/reference_core.py.
 *
 * A pattern may use `{n}` (1-based), `{index}` (0-based), `{prompt}` and
 * `{name}` as many times as it likes. With no `{prompt}`/`{name}` placeholder
 * the member text is appended after the expanded pattern, preserving the
 * original `token: label` behaviour.
 */
export const PROJECT_SCOPED_PROMPT_TOKENS = Object.freeze([
    "{subject_n}", "{picture_n}", "{audio_n}", "{speaker_n}",
]);

export function memberPromptFragment(pattern, index, prompt, name, registryNumbers = null) {
    const memberPrompt = String(prompt || "").trim();
    const entityName = String(name || "").trim();
    const label = memberPrompt || entityName;
    if (!pattern) return label;
    const numbers = registryNumbers && typeof registryNumbers === "object" ? registryNumbers : {};
    // split/join, not String.replace: a string-literal replace substitutes only
    // the first occurrence, while Python's str.replace substitutes every one.
    // `{n}` stays lane-local; the four project-scoped tokens come from the
    // cross-lane registry so one entity numbers the same on every lane.
    const expand = (promptValue) => String(pattern)
        .split("{subject_n}").join(String(numbers.subject_n || 0))
        .split("{picture_n}").join(String(numbers.picture_n || 0))
        .split("{audio_n}").join(String(numbers.audio_n || 0))
        .split("{speaker_n}").join(String(numbers.speaker_n || 0))
        .split("{n}").join(String(index + 1))
        .split("{index}").join(String(index))
        .split("{prompt}").join(promptValue)
        .split("{name}").join(entityName);
    if (pattern.includes("{prompt}") || pattern.includes("{name}")) {
        // An empty member prompt inside a `{prompt}` pattern would otherwise
        // leave a dangling clause; fall back to the entity name rather than
        // dropping the fragment, which would hide a member whose image still
        // reaches the model.
        const useName = !memberPrompt && entityName && pattern.includes("{prompt}");
        return expand(useName ? entityName : memberPrompt).trim();
    }
    return label ? `${expand(memberPrompt)}: ${label}` : expand(memberPrompt);
}

export function deriveReferencePrompt({ promptOverride = "", members = [], soft = {} } = {}) {
    const override = String(promptOverride || "").trim();
    if (override) return override;
    const tokenPattern = String(soft?.prompt_tokens || "");
    const values = [];
    (Array.isArray(members) ? members : []).forEach((entry, index) => {
        const fragment = memberPromptFragment(tokenPattern, index, entry?.prompt, entry?.name);
        if (fragment) values.push(fragment);
    });
    const prefix = String(soft?.prompt_prefix || "").trim();
    const joined = values.join(", ");
    return [prefix, joined].filter(Boolean).join(" ");
}

function integer(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.trunc(number) : fallback;
}

/** Why a staged item did or did not reach the model for a window. */
export const REFERENCE_VERDICT = Object.freeze({
    WINNER: "winner",
    SUPERSEDED: "superseded",
    BELOW_THRESHOLD: "below_threshold",
    OUTSIDE: "outside",
    EXCLUDED: "excluded",
});

/** Display labels, shared by the timeline marks and the lane panel chips. */
export const REFERENCE_VERDICT_LABEL = Object.freeze({
    [REFERENCE_VERDICT.WINNER]: "In window",
    [REFERENCE_VERDICT.SUPERSEDED]: "Superseded",
    [REFERENCE_VERDICT.BELOW_THRESHOLD]: "Below threshold",
    [REFERENCE_VERDICT.OUTSIDE]: "Outside window",
    [REFERENCE_VERDICT.EXCLUDED]: "Excluded",
});

/**
 * Score every staged item for a window and say what happened to each one.
 *
 * This is the scoring core. `resolveEffectiveReferences` derives its winners
 * from it, so the shared JS-vs-Python winner parity test also guards this: any
 * drift here changes the winners and fails that comparison.
 *
 * `frameThresholdPct` drops an item whose in-window overlap is under that
 * percentage of its OWN span, before the survivors are scored. Unlike the
 * prompt boundary threshold there is no never-empty guard: a lane may resolve
 * to nothing, which is what makes a reference stop applying outside its scope.
 *
 * @returns {{winners: Array, verdicts: Map<number, string>}} verdicts keyed by
 *   the item's index in `referenceItems`.
 */
export function resolveReferenceVerdicts({
    referenceItems = [],
    laneCount = 1,
    sceneDuration = 0,
    windowStart = 0,
    windowEnd = sceneDuration,
    laneConfigs = [],
    frameThresholdPct = 0,
} = {}) {
    const count = Math.max(1, integer(laneCount, 1));
    const rawThreshold = Number(frameThresholdPct);
    const threshold = (Number.isFinite(rawThreshold) ? Math.max(0, Math.min(100, rawThreshold)) : 0) / 100;
    const duration = Math.max(0, integer(sceneDuration, 0));
    const start = Math.min(duration, Math.max(0, integer(windowStart, 0)));
    const end = Math.min(duration, Math.max(start, integer(windowEnd, duration)));
    const items = Array.isArray(referenceItems) ? referenceItems : [];
    const configs = Array.isArray(laneConfigs) ? laneConfigs : [];
    const winners = Array(count).fill(null);
    const scores = Array(count).fill(null);
    const verdicts = new Map();

    items.forEach((item, itemIndex) => {
        const laneIndex = integer(item?.lane_index, 0);
        if (laneIndex < 0 || laneIndex >= count || item?.muted || configs[laneIndex]?.hidden) {
            verdicts.set(itemIndex, REFERENCE_VERDICT.EXCLUDED);
            return;
        }
        const itemStart = Math.min(
            Math.max(0, integer(item?.start_frame, 0)),
            Math.max(0, duration - 1),
        );
        const rawEnd = integer(item?.end_frame, -1);
        const itemEnd = rawEnd < 0 ? duration : Math.min(duration, rawEnd);
        if (itemEnd <= itemStart) {
            verdicts.set(itemIndex, REFERENCE_VERDICT.OUTSIDE);
            return;
        }
        const overlap = Math.max(0, Math.min(itemEnd, end) - Math.max(itemStart, start));
        if (overlap <= 0) {
            verdicts.set(itemIndex, REFERENCE_VERDICT.OUTSIDE);
            return;
        }
        const coverage = overlap / (itemEnd - itemStart);
        if (threshold > 0 && coverage < threshold) {
            verdicts.set(itemIndex, REFERENCE_VERDICT.BELOW_THRESHOLD);
            return;
        }
        // A candidate that clears the threshold is provisionally the winner and
        // demotes whoever held the lane; most-specific-wins is resolved by the
        // scan order, so the loser's verdict is only final once it completes.
        const score = [coverage, itemStart, itemIndex];
        const previous = scores[laneIndex];
        const wins = !previous
            || score[0] > previous[0]
            || (score[0] === previous[0] && score[1] > previous[1])
            || (score[0] === previous[0] && score[1] === previous[1] && score[2] > previous[2]);
        if (wins) {
            if (winners[laneIndex]) verdicts.set(winners[laneIndex].itemIndex, REFERENCE_VERDICT.SUPERSEDED);
            scores[laneIndex] = score;
            winners[laneIndex] = { laneIndex, item, itemIndex };
            verdicts.set(itemIndex, REFERENCE_VERDICT.WINNER);
        } else {
            verdicts.set(itemIndex, REFERENCE_VERDICT.SUPERSEDED);
        }
    });
    return { winners, verdicts };
}

/**
 * The lane an item belongs to, normalized exactly as the scorer does.
 *
 * Export rather than re-spell: `(item.lane_index || 0)` and a raw read both
 * disagree with the scorer for `undefined`, `"2"` and `1.7`, which the scorer
 * reads as lanes 0, 2 and 1. Any caller bucketing items by lane must agree with
 * the function that decided the verdicts, or a resolved lane reads unresolved.
 */
export function referenceLaneIndex(item) {
    return integer(item?.lane_index, 0);
}

/** Why a lane failed to resolve for one chunk, most actionable first. */
export const REFERENCE_LANE_CAUSE = Object.freeze({
    BELOW_THRESHOLD: "below_threshold",
    OUTSIDE: "outside",
    EXCLUDED: "excluded",
});

/**
 * Per-lane outcome across a batch's chunks, with the CAUSE of each failure.
 *
 * `resolveEffectiveReferences` answers "did this lane resolve" and throws the
 * reason away, so a caller reading it cannot tell a deliberately scoped item
 * (never overlapped this chunk) from one the frame threshold dropped. Both flip
 * `has_reference` 1->0 and both matter, but they need different words and a
 * different urgency, and only one of them is fixed by touching a setting.
 *
 * Cause precedence per chunk: BELOW_THRESHOLD dominates, because it is the only
 * one where the item DID overlap the chunk and a setting is what dropped it.
 * EXCLUDED (muted item / hidden lane) is window-independent, so it can only ever
 * apply to every chunk at once — it never produces a flip.
 *
 * @returns {Array<{laneIndex, staged, resolved, causeCounts, dominantCause}>}
 */
export function classifyReferenceChunks(chunks = [], options = {}) {
    const { referenceItems = [], laneCount = 1 } = options;
    const count = Math.max(1, integer(laneCount, 1));
    const items = Array.isArray(referenceItems) ? referenceItems : [];
    const windows = Array.isArray(chunks) ? chunks : [];
    const lanes = Array.from({ length: count }, (_, laneIndex) => ({
        laneIndex,
        staged: items.some((item) => referenceLaneIndex(item) === laneIndex),
        resolved: 0,
        causeCounts: {
            [REFERENCE_LANE_CAUSE.BELOW_THRESHOLD]: 0,
            [REFERENCE_LANE_CAUSE.OUTSIDE]: 0,
            [REFERENCE_LANE_CAUSE.EXCLUDED]: 0,
        },
        dominantCause: null,
    }));

    for (const chunk of windows) {
        const { winners, verdicts } = resolveReferenceVerdicts({
            ...options,
            windowStart: chunk?.start,
            windowEnd: chunk?.end,
        });
        const seen = lanes.map(() => new Set());
        verdicts.forEach((verdict, itemIndex) => {
            const laneIndex = referenceLaneIndex(items[itemIndex]);
            if (laneIndex < 0 || laneIndex >= count) return;
            seen[laneIndex].add(verdict);
        });
        for (const lane of lanes) {
            if (!lane.staged) continue;
            if (winners[lane.laneIndex]) { lane.resolved += 1; continue; }
            const reasons = seen[lane.laneIndex];
            const cause = reasons.has(REFERENCE_VERDICT.BELOW_THRESHOLD)
                ? REFERENCE_LANE_CAUSE.BELOW_THRESHOLD
                : reasons.has(REFERENCE_VERDICT.OUTSIDE)
                    ? REFERENCE_LANE_CAUSE.OUTSIDE
                    : REFERENCE_LANE_CAUSE.EXCLUDED;
            lane.causeCounts[cause] += 1;
        }
    }
    for (const lane of lanes) {
        const ranked = [
            REFERENCE_LANE_CAUSE.BELOW_THRESHOLD,
            REFERENCE_LANE_CAUSE.OUTSIDE,
            REFERENCE_LANE_CAUSE.EXCLUDED,
        ].filter((cause) => lane.causeCounts[cause] > 0);
        lane.dominantCause = ranked[0] || null;
    }
    return lanes;
}

/**
 * One most-specific overlapping item per Reference lane, or null.
 * Mirrored in server/reference_resolution.py.
 */
export function resolveEffectiveReferences(options = {}) {
    // The extra `itemIndex` the core carries is internal bookkeeping; the
    // published winner shape stays `{laneIndex, item}`.
    return resolveReferenceVerdicts(options).winners.map(
        (winner) => (winner ? { laneIndex: winner.laneIndex, item: winner.item } : null),
    );
}
