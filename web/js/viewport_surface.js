import { FONT, THEME } from "./editor_theme.js";

function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
}

const PLAYBACK_COMMIT_HOLD_MS = 400;
const PLAYBACK_TAIL_HOLD_MAX_MS = 2000;
const PLAYBACK_OPAQUE_OPACITY = 0.999;
const PLAYBACK_COVERAGE_EPSILON = 0.75;
const PLAYBACK_FIRST_COMMIT_HOLD_MS = 2500;
// Boundary-count prebuffer: warm the next N distinct clip boundaries ahead
// (not a millisecond window). prebufferLookaheadMs is only the scan horizon.
const PLAYBACK_PREBUFFER_BOUNDARY_DEPTH = 2;
// Hard cap on simultaneously warmed prebuffer video elements (RAM/VRAM budget).
const PLAYBACK_PREBUFFER_MAX_ENTRIES = 8;
const PLAYBACK_CURRENT_BOUNDARY_HOLD_FRAMES = 3;
const PLAYBACK_CONTINUATION_SAFETY_TARGET_BUDGET = 2;
const PLAYBACK_PREBUFFER_MISS_THROTTLE_MS = 500;
// Bound the ephemeral warm-status map so long sessions cannot grow without limit.
const PLAYBACK_WARM_MAX_ENTRIES = 6000;
// Adaptive rebuffer hysteresis: minimum gap after a rebuffer exit before another
// may start, so a marginally-slow scene cannot oscillate buffer/play every frame.
const PLAYBACK_REBUFFER_REENTRY_MS = 500;
const PLAYBACK_REBUFFER_NEXT_BOUNDARY_SOFT_MAX_MS = 500;
const PLAYBACK_REBUFFER_TELEMETRY_THROTTLE_MS = 500;
const PLAYBACK_REBUFFER_TOAST_DELAY_MS = 700;
const PLAYBACK_REBUFFER_TOAST_DECAY_MS = 1800;
const PLAYBACK_REBUFFER_TOAST_MIN_VISIBLE_MS = 1200;
const PLAYBACK_REBUFFER_TOAST_LINGER_MS = 6000;
const PLAYBACK_REBUFFER_HEAVY_PRESSURE_THRESHOLD = 2.5;
const PLAYBACK_REBUFFER_HEAVY_PRESSURE_DECAY_MS = 12000;
const PLAYBACK_REBUFFER_HEAVY_PRESSURE_PER_BUFFER = 1;
const PLAYBACK_REBUFFER_HEAVY_WARNING_COOLDOWN_MS = 6000;
const DECODE_PRIORITY_HIGH = "high";
const DECODE_PRIORITY_URGENT = "urgent";
const DECODE_PRIORITY_LOW = "low";
const DECODE_DEADLINE_CURRENT_FRAME = "current-frame";
const DECODE_DEADLINE_CURRENT_SAFETY = "current-safety";
const DECODE_DEADLINE_REBUFFER_NEXT_BOUNDARY = "rebuffer-next-boundary";
const DECODE_DEADLINE_URGENT_OTHER = "urgent-other";
const DECODE_DEADLINE_NONE = "none";

function decodePriorityRank(priority) {
    if (priority === DECODE_PRIORITY_HIGH) return 3;
    if (priority === DECODE_PRIORITY_URGENT) return 2;
    if (priority === DECODE_PRIORITY_LOW) return 1;
    return 0;
}

function decodeDeadlineRank(deadlineClass) {
    if (deadlineClass === DECODE_DEADLINE_CURRENT_FRAME) return 4;
    if (deadlineClass === DECODE_DEADLINE_CURRENT_SAFETY) return 3;
    if (deadlineClass === DECODE_DEADLINE_REBUFFER_NEXT_BOUNDARY) return 2;
    if (deadlineClass === DECODE_DEADLINE_URGENT_OTHER) return 1;
    return 0;
}

function normalizeDecodeDeadlineMeta(meta = {}) {
    const deadlineClass = [
        DECODE_DEADLINE_CURRENT_FRAME,
        DECODE_DEADLINE_CURRENT_SAFETY,
        DECODE_DEADLINE_REBUFFER_NEXT_BOUNDARY,
        DECODE_DEADLINE_URGENT_OTHER,
        DECODE_DEADLINE_NONE,
    ].includes(meta.deadlineClass) ? meta.deadlineClass : DECODE_DEADLINE_NONE;
    const deadlineFrame = Number(meta.deadlineFrame);
    const deadlineDistanceFrames = Number(meta.deadlineDistanceFrames);
    return {
        deadlineClass,
        deadlineFrame: Number.isFinite(deadlineFrame) ? Math.round(deadlineFrame) : null,
        deadlineDistanceFrames: Number.isFinite(deadlineDistanceFrames) ? Math.round(deadlineDistanceFrames) : null,
        scheduleOrigin: meta.scheduleOrigin || "",
        targetFrame: Number.isFinite(Number(meta.targetFrame)) ? Math.round(Number(meta.targetFrame)) : null,
        targetSourceFrame: Number.isFinite(Number(meta.targetSourceFrame)) ? Math.round(Number(meta.targetSourceFrame)) : null,
        sourceTargetKey: meta.sourceTargetKey || "",
        targetKey: meta.targetKey || meta.key || "",
        layerKey: meta.layerKey || "",
    };
}

function decodeDeadlineTelemetry(meta = {}) {
    const normalized = normalizeDecodeDeadlineMeta(meta);
    return {
        deadlineClass: normalized.deadlineClass,
        deadlineFrame: normalized.deadlineFrame,
        deadlineDistanceFrames: normalized.deadlineDistanceFrames,
        deadlineScheduleOrigin: normalized.scheduleOrigin,
        deadlineTargetFrame: normalized.targetFrame,
        deadlineTargetSourceFrame: normalized.targetSourceFrame,
        deadlineSourceTargetKey: normalized.sourceTargetKey,
    };
}

export function _createDecodeConcurrencyLimiter({
    getMaxConcurrent = () => 1,
    reserveHighSlotForLow = false,
    allowSingleSlotLow = () => true,
} = {}) {
    const highQueue = [];
    const urgentQueue = [];
    const lowQueue = [];
    let activeHigh = 0;
    let activeUrgent = 0;
    let activeLow = 0;
    let activeUrgentCurrentFrame = 0;
    let activeUrgentCurrentSafety = 0;
    let activeUrgentRebufferNextBoundary = 0;
    let activeUrgentOther = 0;
    let queueSeq = 0;
    let peakActive = 0;
    let maxQueueDepth = 0;
    let urgentStarted = 0;
    let lowStarted = 0;
    let lowSkippedNoSlot = 0;
    let lowStaleDropped = 0;
    let urgentStaleDropped = 0;
    let urgentDeadlineStaleDropped = 0;
    let urgentNonDeadlineStaleDropped = 0;
    let urgentQueuedCancelled = 0;
    let lowQueuedCancelled = 0;
    let highUrgentWaitedForLow = 0;
    let urgentCurrentSafetyAdmissionBlocked = 0;

    const maxSlots = () => {
        const numeric = Number(getMaxConcurrent());
        return clamp(Number.isFinite(numeric) ? Math.round(numeric) : 1, 1, 8);
    };
    const lowSlotLimit = () => {
        const slots = maxSlots();
        if (slots <= 1 && reserveHighSlotForLow) return allowSingleSlotLow() ? 1 : 0;
        return reserveHighSlotForLow ? Math.max(0, slots - 1) : slots;
    };
    const noteQueueDepth = () => {
        maxQueueDepth = Math.max(maxQueueDepth, highQueue.length + urgentQueue.length + lowQueue.length);
    };
    const noteStaleDrop = (jobOrPriority) => {
        const priority = typeof jobOrPriority === "string" ? jobOrPriority : jobOrPriority?.priority;
        if (priority === DECODE_PRIORITY_LOW) {
            lowStaleDropped += 1;
        } else if (priority === DECODE_PRIORITY_URGENT) {
            urgentStaleDropped += 1;
            const deadlineClass = typeof jobOrPriority === "string"
                ? DECODE_DEADLINE_NONE
                : jobOrPriority?.deadline?.deadlineClass;
            if (decodeDeadlineRank(deadlineClass) > decodeDeadlineRank(DECODE_DEADLINE_URGENT_OTHER)) {
                urgentDeadlineStaleDropped += 1;
            } else {
                urgentNonDeadlineStaleDropped += 1;
            }
        }
    };
    const highestActivePriority = () => {
        if (activeHigh > 0) return DECODE_PRIORITY_HIGH;
        if (activeUrgent > 0) return DECODE_PRIORITY_URGENT;
        if (activeLow > 0) return DECODE_PRIORITY_LOW;
        return "";
    };
    const highestQueuedPriority = () => {
        if (highQueue.length) return DECODE_PRIORITY_HIGH;
        if (urgentQueue.length) return DECODE_PRIORITY_URGENT;
        if (lowQueue.length) return DECODE_PRIORITY_LOW;
        return "";
    };
    const normalizedPriority = (priority) => (
        priority === DECODE_PRIORITY_LOW
            ? DECODE_PRIORITY_LOW
            : (priority === DECODE_PRIORITY_URGENT ? DECODE_PRIORITY_URGENT : DECODE_PRIORITY_HIGH)
    );
    const queueForPriority = (priority) => {
        if (priority === DECODE_PRIORITY_LOW) return lowQueue;
        if (priority === DECODE_PRIORITY_URGENT) return urgentQueue;
        return highQueue;
    };
    const refreshQueuedPositions = () => {
        const annotate = (queue, priority) => {
            for (let index = 0; index < queue.length; index += 1) {
                const job = queue[index];
                job.queueClass = priority;
                job.queuePosition = index;
                job.queueDepth = queue.length;
                try {
                    job.onState?.(job, job.state || "queued");
                } catch (error) {}
            }
        };
        annotate(highQueue, DECODE_PRIORITY_HIGH);
        annotate(urgentQueue, DECODE_PRIORITY_URGENT);
        annotate(lowQueue, DECODE_PRIORITY_LOW);
    };
    const urgentActiveNonCurrentFrame = () => Math.max(0, activeUrgent - activeUrgentCurrentFrame);
    const urgentNonCurrentSlotLimit = () => {
        const slots = maxSlots();
        return slots <= 1 ? 1 : Math.max(0, slots - 1);
    };
    const urgentCurrentSafetySlotLimit = () => {
        const slots = maxSlots();
        if (slots <= 1) return 1;
        return Math.max(1, Math.min(2, slots - 1));
    };
    const urgentRunBlockReason = (job) => {
        if (!job || job.priority !== DECODE_PRIORITY_URGENT) return "";
        if (maxSlots() <= 1) return "";
        const deadlineClass = job.deadline?.deadlineClass || DECODE_DEADLINE_NONE;
        if (deadlineClass === DECODE_DEADLINE_CURRENT_FRAME) return "";
        if (urgentActiveNonCurrentFrame() >= urgentNonCurrentSlotLimit()) return "non-current-reserve";
        if (
            deadlineClass === DECODE_DEADLINE_CURRENT_SAFETY
            && activeUrgentCurrentSafety >= urgentCurrentSafetySlotLimit()
        ) {
            return "current-safety-cap";
        }
        return "";
    };
    const canRunUrgentNow = (job) => !urgentRunBlockReason(job);
    const currentSafetyAdmissionBlockedQueued = () => {
        if (activeUrgentCurrentSafety < urgentCurrentSafetySlotLimit()) return 0;
        return urgentQueue.filter((job) => job?.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_SAFETY).length;
    };
    const activeDeadlineCounts = () => ({
        decodeUrgentCurrentFrameActive: activeUrgentCurrentFrame,
        decodeUrgentCurrentSafetyActive: activeUrgentCurrentSafety,
        decodeUrgentCurrentSafetyActiveLimit: urgentCurrentSafetySlotLimit(),
        decodeUrgentRebufferNextBoundaryActive: activeUrgentRebufferNextBoundary,
        decodeUrgentOtherActive: activeUrgentOther,
        decodeUrgentNonCurrentActive: urgentActiveNonCurrentFrame(),
        decodeUrgentCurrentSafetyAdmissionBlockedQueued: currentSafetyAdmissionBlockedQueued(),
    });
    const queuedDeadlineCounts = () => {
        const counts = {
            decodeUrgentCurrentFrameQueued: 0,
            decodeUrgentCurrentSafetyQueued: 0,
            decodeUrgentRebufferNextBoundaryQueued: 0,
            decodeUrgentOtherQueued: 0,
        };
        for (const job of urgentQueue) {
            if (job?.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME) {
                counts.decodeUrgentCurrentFrameQueued += 1;
            } else if (job?.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_SAFETY) {
                counts.decodeUrgentCurrentSafetyQueued += 1;
            } else if (job?.deadline?.deadlineClass === DECODE_DEADLINE_REBUFFER_NEXT_BOUNDARY) {
                counts.decodeUrgentRebufferNextBoundaryQueued += 1;
            } else {
                counts.decodeUrgentOtherQueued += 1;
            }
        }
        return counts;
    };
    const currentFrameBlockedByActiveNonDeadline = () => (
        urgentQueue.some((job) => job?.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME)
        && activeUrgent >= Math.max(0, maxSlots() - activeHigh - activeLow)
        && urgentActiveNonCurrentFrame() > 0
    );
    const normalizeJobDeadline = (deadline = {}) => normalizeDecodeDeadlineMeta(deadline);
    const urgentDeadlineCompare = (a, b) => {
        const ar = decodeDeadlineRank(a?.deadline?.deadlineClass);
        const br = decodeDeadlineRank(b?.deadline?.deadlineClass);
        if (ar !== br) return br - ar;
        const ad = Number.isFinite(Number(a?.deadline?.deadlineDistanceFrames)) ? Number(a.deadline.deadlineDistanceFrames) : Infinity;
        const bd = Number.isFinite(Number(b?.deadline?.deadlineDistanceFrames)) ? Number(b.deadline.deadlineDistanceFrames) : Infinity;
        if (ad !== bd) return ad - bd;
        const af = Number.isFinite(Number(a?.deadline?.deadlineFrame)) ? Number(a.deadline.deadlineFrame) : Infinity;
        const bf = Number.isFinite(Number(b?.deadline?.deadlineFrame)) ? Number(b.deadline.deadlineFrame) : Infinity;
        if (af !== bf) return af - bf;
        return (a?.queueOrder || 0) - (b?.queueOrder || 0);
    };
    const insertQueuedJob = (job, { front = false } = {}) => {
        const queue = queueForPriority(job.priority);
        if (job.priority !== DECODE_PRIORITY_URGENT) {
            if (front) {
                queue.unshift(job);
            } else {
                queue.push(job);
            }
            refreshQueuedPositions();
            return;
        }
        if (front) job.queueOrder = -Math.abs(job.queueOrder || ++queueSeq);
        queue.push(job);
        queue.sort(urgentDeadlineCompare);
        refreshQueuedPositions();
    };
    const removeQueuedJob = (job) => {
        const queue = queueForPriority(job?.priority);
        const index = queue.indexOf(job);
        if (index < 0) return -1;
        queue.splice(index, 1);
        refreshQueuedPositions();
        return index;
    };
    const canRunLowNow = () => {
        const slots = maxSlots();
        if (slots <= 1 && reserveHighSlotForLow && !allowSingleSlotLow()) return false;
        if (highQueue.length || urgentQueue.length || activeHigh > 0 || activeUrgent > 0) return false;
        return activeLow < lowSlotLimit();
    };
    const shouldJobRun = (job, priority) => {
        if (typeof job?.shouldRun !== "function") return true;
        try {
            return !!job.shouldRun();
        } catch (error) {
            return false;
        }
    };
    const nextRunnableJob = (queue, priority) => {
        while (queue.length) {
            const job = queue.shift();
            refreshQueuedPositions();
            if (!shouldJobRun(job, priority)) {
                noteStaleDrop(job);
                job.state = "settled";
                job.onState?.(job, "settled");
                job.resolve(null);
                continue;
            }
            return job;
        }
        return null;
    };
    const nextRunnableUrgentJob = () => {
        urgentQueue.sort(urgentDeadlineCompare);
        while (urgentQueue.length) {
            let blockedByReservation = false;
            for (let index = 0; index < urgentQueue.length; index += 1) {
                const job = urgentQueue[index];
                if (!shouldJobRun(job, DECODE_PRIORITY_URGENT)) {
                    urgentQueue.splice(index, 1);
                    refreshQueuedPositions();
                    noteStaleDrop(job);
                    job.state = "settled";
                    job.onState?.(job, "settled");
                    job.resolve(null);
                    index -= 1;
                    continue;
                }
                const blockReason = urgentRunBlockReason(job);
                if (!blockReason) {
                    urgentQueue.splice(index, 1);
                    refreshQueuedPositions();
                    return job;
                }
                if (blockReason === "current-safety-cap") {
                    urgentCurrentSafetyAdmissionBlocked += 1;
                    return null;
                }
                blockedByReservation = true;
            }
            if (blockedByReservation) return null;
            return null;
        }
        return null;
    };
    const incrementActiveDeadline = (job) => {
        if (job?.priority !== DECODE_PRIORITY_URGENT) return;
        if (job.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME) {
            activeUrgentCurrentFrame += 1;
        } else if (job.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_SAFETY) {
            activeUrgentCurrentSafety += 1;
        } else if (job.deadline?.deadlineClass === DECODE_DEADLINE_REBUFFER_NEXT_BOUNDARY) {
            activeUrgentRebufferNextBoundary += 1;
        } else {
            activeUrgentOther += 1;
        }
    };
    const decrementActiveDeadline = (job) => {
        if (job?.priority !== DECODE_PRIORITY_URGENT) return;
        if (job.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME) {
            activeUrgentCurrentFrame = Math.max(0, activeUrgentCurrentFrame - 1);
        } else if (job.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_SAFETY) {
            activeUrgentCurrentSafety = Math.max(0, activeUrgentCurrentSafety - 1);
        } else if (job.deadline?.deadlineClass === DECODE_DEADLINE_REBUFFER_NEXT_BOUNDARY) {
            activeUrgentRebufferNextBoundary = Math.max(0, activeUrgentRebufferNextBoundary - 1);
        } else {
            activeUrgentOther = Math.max(0, activeUrgentOther - 1);
        }
    };

    function dispatch(job) {
        if (!shouldJobRun(job, job.priority)) {
            noteStaleDrop(job);
            job.state = "settled";
            job.onState?.(job, "settled");
            job.resolve(null);
            return;
        }
        job.state = "active";
        job.queueClass = job.priority;
        job.queuePosition = null;
        job.queueDepth = null;
        job.onState?.(job, "active");
        if (job.priority === DECODE_PRIORITY_LOW) {
            activeLow += 1;
            lowStarted += 1;
        } else if (job.priority === DECODE_PRIORITY_URGENT) {
            activeUrgent += 1;
            urgentStarted += 1;
            incrementActiveDeadline(job);
        } else {
            activeHigh += 1;
        }
        peakActive = Math.max(peakActive, activeHigh + activeUrgent + activeLow);
        Promise.resolve()
            .then(() => {
                if (!shouldJobRun(job, job.priority)) return null;
                return job.task();
            })
            .then((result) => (shouldJobRun(job, job.priority) ? result : null))
            .then(
                (result) => {
                    job.state = "settled";
                    job.onState?.(job, "settled");
                    release(job);
                    job.resolve(result);
                },
                (error) => {
                    job.state = "settled";
                    job.onState?.(job, "settled");
                    release(job);
                    job.reject(error);
                }
            );
    }

    function release(jobOrPriority) {
        const priority = typeof jobOrPriority === "string" ? jobOrPriority : jobOrPriority?.priority;
        if (priority === DECODE_PRIORITY_LOW) {
            activeLow = Math.max(0, activeLow - 1);
        } else if (priority === DECODE_PRIORITY_URGENT) {
            activeUrgent = Math.max(0, activeUrgent - 1);
            decrementActiveDeadline(jobOrPriority);
        } else {
            activeHigh = Math.max(0, activeHigh - 1);
        }
        pump();
    }

    function dropBlockedLowQueue() {
        if (lowSlotLimit() > 0) return false;
        while (lowQueue.length) {
            const job = lowQueue.shift();
            lowSkippedNoSlot += 1;
            job.state = "settled";
            job.onState?.(job, "settled");
            job.resolve(null);
        }
        return true;
    }

    function pump() {
        const slots = maxSlots();
        while ((activeHigh + activeUrgent + activeLow) < slots && highQueue.length) {
            const job = nextRunnableJob(highQueue, DECODE_PRIORITY_HIGH);
            if (!job) break;
            dispatch(job);
        }
        while ((activeHigh + activeUrgent + activeLow) < slots && urgentQueue.length) {
            const job = nextRunnableUrgentJob();
            if (!job) break;
            dispatch(job);
        }
        if (dropBlockedLowQueue()) return;
        while (
            (activeHigh + activeUrgent + activeLow) < slots
            && canRunLowNow()
            && lowQueue.length
        ) {
            const job = nextRunnableJob(lowQueue, DECODE_PRIORITY_LOW);
            if (!job) break;
            dispatch(job);
        }
    }

    function promote(job, priority, { front = false, deadline = null } = {}) {
        const requestedPriority = normalizedPriority(priority);
        if (!job || job.state !== "queued") return "not-queued";
        const previousPriority = job.priority;
        const previousQueue = queueForPriority(job.priority);
        const previousIndex = previousQueue.indexOf(job);
        if (previousIndex < 0) return "not-found";
        const previousDeadlineSig = JSON.stringify(job.deadline || {});
        removeQueuedJob(job);
        const priorityRankChanged = decodePriorityRank(requestedPriority) > decodePriorityRank(previousPriority);
        if (deadline) job.deadline = normalizeJobDeadline(deadline);
        const deadlineChanged = JSON.stringify(job.deadline || {}) !== previousDeadlineSig;
        const wasAlreadyFront = previousIndex === 0;
        if (!priorityRankChanged && !front && !deadlineChanged) {
            insertQueuedJob(job, { front: false });
            return "not-needed";
        }
        job.priority = requestedPriority;
        insertQueuedJob(job, { front });
        job.onState?.(job, "queued");
        noteQueueDepth();
        const newIndex = queueForPriority(job.priority).indexOf(job);
        pump();
        if (front && requestedPriority === previousPriority && newIndex >= 0 && newIndex < previousIndex) {
            return "moved-front";
        }
        if (front && requestedPriority === previousPriority && wasAlreadyFront && !deadlineChanged) {
            return "already-front";
        }
        if (deadlineChanged && requestedPriority === previousPriority) return "reprioritized-queued";
        return priorityRankChanged ? "promoted" : "moved-front";
    }

    function cancelQueued(job, reason = "cancelled") {
        if (!job || job.state !== "queued") return false;
        const index = removeQueuedJob(job);
        if (index < 0) return false;
        if (job.priority === DECODE_PRIORITY_LOW) {
            lowQueuedCancelled += 1;
        } else if (job.priority === DECODE_PRIORITY_URGENT) {
            urgentQueuedCancelled += 1;
        }
        job.state = "cancelled";
        job.cancelReason = reason;
        job.onState?.(job, "cancelled");
        job.resolve(null);
        pump();
        return true;
    }

    function run(priority, task, { shouldRun, onQueued, onState, front = false, deadline = null } = {}) {
        const normalizedPriorityValue = normalizedPriority(priority);
        if (normalizedPriorityValue === DECODE_PRIORITY_LOW && lowSlotLimit() <= 0) {
            lowSkippedNoSlot += 1;
            return Promise.resolve(null);
        }
        if (normalizedPriorityValue !== DECODE_PRIORITY_LOW && maxSlots() <= 1 && activeLow > 0) {
            highUrgentWaitedForLow += 1;
        }
        return new Promise((resolve, reject) => {
            const job = {
                priority: normalizedPriorityValue,
                state: "queued",
                task,
                shouldRun,
                onState,
                deadline: normalizeJobDeadline(deadline || {}),
                queueOrder: ++queueSeq,
                resolve,
                reject,
            };
            insertQueuedJob(job, { front });
            try { onQueued?.(job); } catch (error) {}
            try { onState?.(job, "queued"); } catch (error) {}
            noteQueueDepth();
            pump();
        });
    }

    function flushStats() {
        const stats = {
            peakConcurrentDecodes: peakActive,
            maxDecodeQueueDepth: maxQueueDepth,
            decodeHighActive: activeHigh,
            decodeUrgentActive: activeUrgent,
            decodeLowActive: activeLow,
            decodeHighQueued: highQueue.length,
            decodeUrgentQueued: urgentQueue.length,
            decodeLowQueued: lowQueue.length,
            decodeUrgentStarted: urgentStarted,
            decodeLowStarted: lowStarted,
            decodeLowSkipped: lowSkippedNoSlot,
            decodeLowSkippedNoSlot: lowSkippedNoSlot,
            decodeLowStaleDropped: lowStaleDropped,
            decodeUrgentStaleDropped: urgentStaleDropped,
            decodeUrgentDeadlineStaleDropped: urgentDeadlineStaleDropped,
            decodeUrgentNonDeadlineStaleDropped: urgentNonDeadlineStaleDropped,
            decodeLowQueuedCancelled: lowQueuedCancelled,
            decodeUrgentQueuedCancelled: urgentQueuedCancelled,
            decodeHighUrgentWaitedForLow: highUrgentWaitedForLow,
            decodeUrgentCurrentSafetyAdmissionBlocked: urgentCurrentSafetyAdmissionBlocked,
            decodeHighestActivePriority: highestActivePriority(),
            decodeHighestQueuedPriority: highestQueuedPriority(),
            decodeCurrentFrameBlockedByActiveNonDeadline: currentFrameBlockedByActiveNonDeadline() ? 1 : 0,
            ...activeDeadlineCounts(),
            ...queuedDeadlineCounts(),
        };
        peakActive = activeHigh + activeUrgent + activeLow;
        maxQueueDepth = highQueue.length + urgentQueue.length + lowQueue.length;
        urgentStarted = 0;
        lowStarted = 0;
        lowSkippedNoSlot = 0;
        lowStaleDropped = 0;
        urgentStaleDropped = 0;
        urgentDeadlineStaleDropped = 0;
        urgentNonDeadlineStaleDropped = 0;
        lowQueuedCancelled = 0;
        urgentQueuedCancelled = 0;
        highUrgentWaitedForLow = 0;
        urgentCurrentSafetyAdmissionBlocked = 0;
        return stats;
    }

    function snapshotStats() {
        return {
            decodeHighActive: activeHigh,
            decodeUrgentActive: activeUrgent,
            decodeLowActive: activeLow,
            decodeHighQueued: highQueue.length,
            decodeUrgentQueued: urgentQueue.length,
            decodeLowQueued: lowQueue.length,
            decodeUrgentCurrentSafetyAdmissionBlocked: urgentCurrentSafetyAdmissionBlocked,
            decodeHighestActivePriority: highestActivePriority(),
            decodeHighestQueuedPriority: highestQueuedPriority(),
            decodeCurrentFrameBlockedByActiveNonDeadline: currentFrameBlockedByActiveNonDeadline() ? 1 : 0,
            ...activeDeadlineCounts(),
            ...queuedDeadlineCounts(),
        };
    }

    function resetStats() {
        peakActive = activeHigh + activeUrgent + activeLow;
        maxQueueDepth = highQueue.length + urgentQueue.length + lowQueue.length;
        urgentStarted = 0;
        lowStarted = 0;
        lowSkippedNoSlot = 0;
        lowStaleDropped = 0;
        urgentStaleDropped = 0;
        urgentDeadlineStaleDropped = 0;
        urgentNonDeadlineStaleDropped = 0;
        lowQueuedCancelled = 0;
        urgentQueuedCancelled = 0;
        highUrgentWaitedForLow = 0;
        urgentCurrentSafetyAdmissionBlocked = 0;
    }

    return { run, promote, cancelQueued, flushStats, snapshotStats, resetStats };
}

function playbackLayerKey(layer) {
    return layer?.key || "";
}

function uniquePlaybackLayers(layers = []) {
    const seen = new Set();
    const result = [];
    for (const layer of layers || []) {
        const key = playbackLayerKey(layer);
        if (!key || seen.has(key)) continue;
        seen.add(key);
        result.push(layer);
    }
    return result;
}

// Internal executable scheduling seam. Callers provide already-resolved required
// renderable video layers, so visibility/coverage ownership remains in the
// viewport surface while transition novelty stays deterministic and testable.
export function _planEffectivePlaybackBoundaryGroups({
    candidateFrames = [],
    requiredLayersAtFrame = () => [],
    loopRange = null,
} = {}) {
    const loopStart = loopRange
        ? Math.max(0, Math.round(Number(loopRange.start) || 0))
        : null;
    const loopEnd = loopRange
        ? Math.max(loopStart + 1, Math.round(Number(loopRange.end) || loopStart + 1))
        : null;
    const groups = [];
    const resolvedLayers = new Map();
    const layersAtFrame = (frame) => {
        if (!resolvedLayers.has(frame)) {
            resolvedLayers.set(frame, uniquePlaybackLayers(requiredLayersAtFrame(frame)));
        }
        return resolvedLayers.get(frame);
    };
    for (const value of candidateFrames || []) {
        const frame = Math.max(0, Math.round(Number(value) || 0));
        if (loopRange && (frame < loopStart || frame >= loopEnd)) continue;
        const afterLayers = layersAtFrame(frame);
        if (!afterLayers.length) continue;
        const loopWrap = !!loopRange && frame === loopStart;
        let layers = afterLayers;
        if (!loopWrap) {
            if (frame <= 0) continue;
            const beforeKeys = new Set(
                layersAtFrame(frame - 1).map(playbackLayerKey),
            );
            layers = afterLayers.filter((layer) => !beforeKeys.has(playbackLayerKey(layer)));
        }
        if (layers.length) groups.push({ frame, layers, loopWrap });
    }
    return groups;
}

export function _classifyRebufferPrebufferEntry({
    desired = false,
    claimed = false,
    waiting = false,
    ready = false,
    valid = true,
} = {}) {
    if (claimed) return "drop-reference";
    if (!valid) return "cancel";
    if (desired || waiting) return "preserve";
    if (ready) return "preserve-ready";
    return "cancel";
}

function emptyPrebufferScheduleStats(reason = "") {
    return {
        rawPlayableVideoCount: 0,
        requiredVideoCount: 0,
        culledCoveredVideoCount: 0,
        currentSafetyTargetCount: 0,
        currentFrameRecoveryTargetCount: 0,
        currentFrameRecoveryCandidateCount: 0,
        currentFrameRecoveryPendingExistingCount: 0,
        currentFrameRecoveryMovedFrontCount: 0,
        currentFrameRecoveryReclassifiedCount: 0,
        currentFrameRecoveryQueuedPromotedCount: 0,
        currentFrameRecoveryActiveReclassifiedCount: 0,
        continuationSafetyTargetCount: 0,
        continuationSafetyAdmitted: 0,
        continuationSafetySuppressed: 0,
        currentSafetyAdmissionBlocked: 0,
        upcomingTargetCount: 0,
        rebufferNextBoundaryTargetCount: 0,
        deferredNextBoundaryTargetCount: 0,
        deferredNextBoundaryRetainedCount: 0,
        deferredNextBoundaryDroppedCount: 0,
        prebufferTargetCount: 0,
        handoffPruneReason: "",
        rawLayerSignature: "",
        requiredLayerSignature: "",
        targetSignature: "",
        rebufferLimited: false,
        handoffActive: false,
        reason,
    };
}

// Session-diagnostic helper: writes to `window.__SONDER_CANVAS_DIAG` populated
// by editor_widget.js when `window.SONDER_DEBUG_SESSION === true`. Zero-cost
// when disabled.
function viewportDiagRecord(kind, payload) {
    if (typeof window === "undefined" || window.SONDER_DEBUG_SESSION !== true) return;
    const surface = window.__SONDER_CANVAS_DIAG;
    if (!surface || typeof surface.record !== "function") return;
    surface.record(kind, payload || {});
}

function fitRect(sourceWidth, sourceHeight, targetWidth, targetHeight) {
    const safeSourceWidth = Math.max(1, Number(sourceWidth) || 1);
    const safeSourceHeight = Math.max(1, Number(sourceHeight) || 1);
    const safeTargetWidth = Math.max(1, Number(targetWidth) || 1);
    const safeTargetHeight = Math.max(1, Number(targetHeight) || 1);
    const sourceAspect = safeSourceWidth / safeSourceHeight;
    const targetAspect = safeTargetWidth / safeTargetHeight;
    if (sourceAspect > targetAspect) {
        const width = safeTargetWidth;
        const height = width / sourceAspect;
        return { x: 0, y: (safeTargetHeight - height) / 2, width, height };
    }
    const height = safeTargetHeight;
    const width = height * sourceAspect;
    return { x: (safeTargetWidth - width) / 2, y: 0, width, height };
}

// Per-item fit modes mirror server/media_helpers.py. Preview must show the chosen
// framing or it lies about what will render. Canvas smoothing isn't AREA/Lanczos,
// so this is framing-accurate, not pixel-accurate.
const VIEWPORT_FIT_MODES = new Set(["fit", "pad_edge", "cover", "stretch"]);

function fitOptionsFor(item) {
    const mode = item?.fit_mode;
    return {
        fitMode: VIEWPORT_FIT_MODES.has(mode) ? mode : "pad_edge",
        cropPosition: item?.crop_position || "center",
    };
}

// pad_edge: stretch the 1px source edge across each letterbox/pillarbox bar so the
// framing matches `fit` but without a hard black edge (mirrors cv2 BORDER_REPLICATE).
function drawEdgePadBars(ctx, element, rect, canvasW, canvasH, srcW, srcH) {
    const left = rect.x;
    const top = rect.y;
    const right = canvasW - (rect.x + rect.width);
    const bottom = canvasH - (rect.y + rect.height);
    try {
        if (left > 0.5) {
            ctx.drawImage(element, 0, 0, 1, srcH, 0, rect.y, left, rect.height);
        }
        if (right > 0.5) {
            ctx.drawImage(element, srcW - 1, 0, 1, srcH, rect.x + rect.width, rect.y, right, rect.height);
        }
        if (top > 0.5) {
            ctx.drawImage(element, 0, 0, srcW, 1, rect.x, 0, rect.width, top);
        }
        if (bottom > 0.5) {
            ctx.drawImage(element, 0, srcH - 1, srcW, 1, rect.x, rect.y + rect.height, rect.width, bottom);
        }
    } catch (error) {}
}

function removeMediaSource(mediaEl) {
    if (!mediaEl) return;
    try {
        mediaEl.pause?.();
    } catch (error) {}
    if (typeof mediaEl._sonderReleaseSourceRef === "function") {
        try {
            mediaEl._sonderReleaseSourceRef(mediaEl);
        } catch (error) {}
    } else {
        mediaEl._sonderSourceUrl = "";
        mediaEl._sonderSourceCacheKey = "";
    }
    mediaEl._sonderSourceRequestToken = (Number(mediaEl._sonderSourceRequestToken) || 0) + 1;
    if (typeof mediaEl.removeAttribute === "function") {
        mediaEl.removeAttribute("src");
    }
    try {
        mediaEl.load?.();
    } catch (error) {}
}

function clearCacheObject(cache) {
    if (!cache || typeof cache !== "object") return;
    for (const key of Object.keys(cache)) {
        delete cache[key];
    }
}

function waitForMediaReady(mediaEl, minReadyState = 1, timeoutMs = 800, { signal = null } = {}) {
    if (!mediaEl) return Promise.resolve(null);
    if (signal?.aborted) return Promise.resolve(null);
    if (mediaEl.error) return Promise.resolve(null);
    if ((mediaEl.readyState || 0) >= minReadyState) {
        return Promise.resolve(mediaEl);
    }
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(mediaEl && !mediaEl.error ? mediaEl : null);
        };
        const fail = () => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(null);
        };
        const cleanup = () => {
            window.clearTimeout(timer);
            for (const eventName of ["loadedmetadata", "loadeddata", "canplay", "canplaythrough"]) {
                mediaEl.removeEventListener(eventName, finish);
            }
            mediaEl.removeEventListener("error", fail);
            signal?.removeEventListener?.("abort", fail);
        };
        const timer = window.setTimeout(finish, timeoutMs);
        for (const eventName of ["loadedmetadata", "loadeddata", "canplay", "canplaythrough"]) {
            mediaEl.addEventListener(eventName, finish, { once: true });
        }
        mediaEl.addEventListener("error", fail, { once: true });
        signal?.addEventListener?.("abort", fail, { once: true });
    });
}

export { waitForMediaReady as _waitForMediaReady };

function clampMediaTargetTime(mediaEl, targetTime) {
    const duration = Number(mediaEl?.duration);
    const numericTarget = Number(targetTime);
    const safeTarget = Number.isFinite(numericTarget) ? numericTarget : 0;
    return Number.isFinite(duration) && duration > 0
        ? clamp(safeTarget, 0, Math.max(0, duration - 0.001))
        : Math.max(0, safeTarget);
}

function isMediaAtTarget(mediaEl, targetTime, tolerance = 0.02) {
    if (!mediaEl) return false;
    const current = Number(mediaEl.currentTime);
    const target = Number(targetTime);
    const tol = Number(tolerance);
    if (!Number.isFinite(current) || !Number.isFinite(target) || !Number.isFinite(tol) || tol < 0) return false;
    return Math.abs(current - target) <= tol;
}

function videoFrameCallbackMetadataTelemetry(metadata) {
    return {
        media_time: Number.isFinite(Number(metadata?.mediaTime)) ? Number(metadata.mediaTime) : null,
        presented_frames: Number.isFinite(Number(metadata?.presentedFrames)) ? Number(metadata.presentedFrames) : null,
        expected_display_delta_ms: Number.isFinite(Number(metadata?.expectedDisplayTime))
            ? Number(metadata.expectedDisplayTime) - performance.now()
            : null,
        processing_duration_ms: Number.isFinite(Number(metadata?.processingDuration))
            ? Number(metadata.processingDuration) * 1000
            : null,
    };
}

function waitForDecodedVideoFrame(mediaEl, timeoutMs = 120, { signal = null } = {}) {
    if (!mediaEl || typeof mediaEl.requestVideoFrameCallback !== "function") {
        return Promise.resolve(signal?.aborted ? null : mediaEl);
    }
    if (signal?.aborted) return Promise.resolve(null);
    const startTs = performance.now();
    return new Promise((resolve) => {
        let settled = false;
        let callbackId = null;
        let viaCallback = false;
        let lastMetadata = null;
        const finish = (result = mediaEl) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(timer);
            signal?.removeEventListener?.("abort", onAbort);
            if (callbackId !== null && typeof mediaEl.cancelVideoFrameCallback === "function") {
                try {
                    mediaEl.cancelVideoFrameCallback(callbackId);
                } catch (error) {}
            }
            viewportDiagRecord("wait_decoded_frame", {
                duration_ms: performance.now() - startTs,
                timeout_ms: timeoutMs,
                via_callback: viaCallback,
                ready_state: mediaEl.readyState,
                ...videoFrameCallbackMetadataTelemetry(lastMetadata),
            });
            resolve(result);
        };
        const onAbort = () => finish(null);
        const timer = window.setTimeout(() => finish(mediaEl), timeoutMs);
        signal?.addEventListener?.("abort", onAbort, { once: true });
        try {
            callbackId = mediaEl.requestVideoFrameCallback((_now, metadata = {}) => {
                viaCallback = true;
                lastMetadata = metadata;
                finish(mediaEl);
            });
        } catch (error) {
            finish(mediaEl);
        }
    });
}

function waitForDecodedVideoFrameAtTarget(mediaEl, targetTime, tolerance = 0.02, timeoutMs = 200) {
    if (mediaEl && !mediaEl.seeking && (mediaEl.readyState || 0) >= 2 && isMediaAtTarget(mediaEl, targetTime, tolerance)) {
        return Promise.resolve(true);
    }
    if (!mediaEl || typeof mediaEl.requestVideoFrameCallback !== "function") {
        return Promise.resolve(isMediaAtTarget(mediaEl, targetTime, tolerance));
    }
    const startTs = performance.now();
    return new Promise((resolve) => {
        let settled = false;
        let callbackId = null;
        let viaCallback = false;
        let lastMetadata = null;
        const finish = (ok) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(timer);
            if (callbackId !== null && typeof mediaEl.cancelVideoFrameCallback === "function") {
                try {
                    mediaEl.cancelVideoFrameCallback(callbackId);
                } catch (error) {}
            }
            viewportDiagRecord("wait_decoded_frame_at_target", {
                duration_ms: performance.now() - startTs,
                target_time: targetTime,
                timeout_ms: timeoutMs,
                via_callback: viaCallback,
                ready_state: mediaEl.readyState,
                ok: !!ok,
                ...videoFrameCallbackMetadataTelemetry(lastMetadata),
            });
            resolve(!!ok);
        };
        const timer = window.setTimeout(() => {
            finish(isMediaAtTarget(mediaEl, targetTime, tolerance));
        }, timeoutMs);
        try {
            callbackId = mediaEl.requestVideoFrameCallback((_now, metadata = {}) => {
                viaCallback = true;
                lastMetadata = metadata;
                const mediaTime = Number(metadata.mediaTime);
                const decodedAtTarget = Number.isFinite(mediaTime)
                    ? Math.abs(mediaTime - targetTime) <= tolerance
                    : isMediaAtTarget(mediaEl, targetTime, tolerance);
                finish(decodedAtTarget);
            });
        } catch (error) {
            finish(isMediaAtTarget(mediaEl, targetTime, tolerance));
        }
    });
}

function seekMedia(mediaEl, targetTime, {
    tolerance = 0.02,
    timeoutMs = 250,
    requireTarget = false,
    waitForFrame = false,
    signal = null,
} = {}) {
    if (!mediaEl) return Promise.resolve(null);
    if (signal?.aborted) return Promise.resolve(null);
    return waitForMediaReady(mediaEl, 1, 800, { signal }).then((element) => {
        if (!element) return null;
        const safeTarget = clampMediaTargetTime(element, targetTime);
        const finishWithFrame = () => {
            if (signal?.aborted) return Promise.resolve(null);
            const candidate = !requireTarget || isMediaAtTarget(element, safeTarget, tolerance)
                ? element
                : null;
            if (!candidate || !waitForFrame) return Promise.resolve(candidate);
            if ((candidate.readyState || 0) >= 2 && !candidate.seeking && isMediaAtTarget(candidate, safeTarget, tolerance)) {
                return Promise.resolve(candidate);
            }
            return waitForDecodedVideoFrame(candidate, Math.min(200, Math.max(80, timeoutMs)), { signal })
                .then((decoded) => decoded && (!requireTarget || isMediaAtTarget(candidate, safeTarget, tolerance)) ? candidate : null);
        };
        if ((element.readyState || 0) >= 2 && isMediaAtTarget(element, safeTarget, tolerance)) {
            return finishWithFrame();
        }
        return new Promise((resolve) => {
            let settled = false;
            let sawSeeked = false;
            const onSeeked = () => {
                sawSeeked = true;
                finish(false);
            };
            const onError = () => finish(true, true);
            const onAbort = () => finish(true, true);
            const finish = (force = false, failed = false) => {
                if (settled) return;
                const ready = (element.readyState || 0) >= 2;
                const atTarget = isMediaAtTarget(element, safeTarget, tolerance);
                if (!force && (!sawSeeked || !ready || !atTarget)) return;
                settled = true;
                cleanup();
                if (failed || element.error || (requireTarget && !atTarget)) {
                    resolve(null);
                    return;
                }
                if (!waitForFrame) {
                    resolve(element);
                    return;
                }
                waitForDecodedVideoFrame(element, Math.min(200, Math.max(80, timeoutMs)), { signal })
                    .then((decoded) => {
                        resolve(decoded && (!requireTarget || isMediaAtTarget(element, safeTarget, tolerance)) ? element : null);
                    });
            };
            const cleanup = () => {
                window.clearTimeout(timer);
                element.removeEventListener("seeked", onSeeked);
                element.removeEventListener("error", onError);
                signal?.removeEventListener?.("abort", onAbort);
            };
            const timer = window.setTimeout(() => finish(true), timeoutMs);
            element.addEventListener("seeked", onSeeked, { once: true });
            element.addEventListener("error", onError, { once: true });
            signal?.addEventListener?.("abort", onAbort, { once: true });
            try {
                if (signal?.aborted) {
                    finish(true, true);
                    return;
                }
                element.currentTime = safeTarget;
            } catch (error) {
                finish(true);
            }
        });
    });
}

export function createViewportSurface(options = {}) {
    const state = {
        canvas: options.canvas || null,
        ctx: options.canvas?.getContext?.("2d") || null,
        destroyed: false,
        liveMediaEnabled: !!options.initialLiveMediaEnabled,
        isPlaying: false,
        playbackRAF: null,
        playbackStartTime: 0,
        playbackStartFrame: 0,
        playbackSessionStartFrame: 0,
        playbackLoopRange: null,
        playbackSessionId: 0,
        playbackPrepareToken: 0,
        playbackCompositeCommitted: false,
        playbackBlockedSinceMs: null,
        playbackBlockedSignature: "",
        playbackCanvasWidth: 0,
        playbackCanvasHeight: 0,
        playbackLastCommittedFrame: null,
        playbackLastCommittedSignature: "",
        playbackLastCommittedSessionId: 0,
        playbackFirstCommitStartedAt: null,
        playbackFirstCommitFrame: null,
        playbackFirstCommitHoldExpired: false,
        playbackDecisionLogKeys: new Set(),
        audioReleasedThisSession: false,
        audioFreezeLogged: false,
        audioReleaseLogged: false,
        renderToken: 0,
        sourceUrlCache: new Map(),
        activePlaybackVideos: new Map(),
        activePlaybackAudios: new Map(),
        prebufferCache: new Map(),
        prebufferMissTelemetryKeys: new Map(),
        prebufferMissTelemetryEmitted: 0,
        prebufferMissTelemetrySuppressed: 0,
        prebufferPendingHoldCount: 0,
        expiredPendingPrebufferSkips: new Map(),
        lastRebufferLimitedScheduleSig: "",
        lastPrebufferScheduleStats: emptyPrebufferScheduleStats("init"),
        playbackWarmEntries: new Map(),
        playbackWarmGeneration: 0,
        playbackWarmNotifyRAF: null,
        playbackWarmEntrySeq: 0,
        playbackWarmContentToken: 0,
        // Outgoing media elements awaiting release after the next successful
        // commit (so we never tear down an element still feeding the canvas).
        pendingRelease: new Set(),
        videoCache: options.videoCache || {},
        lastBoundaryCoverageSig: "",
        audioCache: options.audioCache || {},
        imageCache: options.imageCache || {},
        // Phase 2 adaptive rebuffer: when a transient in-flight prepare keeps the
        // composite blocked, freeze the clock + audio at the buffering frame so they
        // stop running away from frozen video, then resume in sync on the next commit.
        playbackRebuffering: false,
        playbackRebufferFrame: 0,
        playbackRebufferSinceMs: null,
        playbackRebufferCapped: false,
        playbackRebufferLastExitMs: 0,
        playbackRebufferBlockTargetKey: "",
        playbackRebufferLastExitTargetKey: "",
        playbackRebufferEntryDecisionSig: "",
        playbackRebufferSafetyTargets: [],
        playbackRebufferSafetySig: "",
        playbackRebufferSafetyInitialized: false,
        playbackRebufferSafetyRetainedCount: 0,
        playbackRebufferSafetyDiscardedCount: 0,
        playbackRebufferLastSafetyStatuses: [],
        playbackRebufferLastSafetyReason: "",
        playbackDeferredNextBoundaryTargets: [],
        playbackDeferredNextBoundaryRetainedCount: 0,
        playbackDeferredNextBoundaryDroppedCount: 0,
        playbackDeferredNextBoundaryScheduledCount: 0,
        prebufferPriorityUpgradeOutcomes: {
            preservedRunning: 0,
            promotedQueued: 0,
            movedFront: 0,
            reprioritizedQueued: 0,
            recreatedQueued: 0,
            alreadyFront: 0,
        },
        playbackHandoffCurrentTargetCount: 0,
        playbackHandoffContinuationSuppressed: 0,
        playbackHandoffUpcomingSuppressed: 0,
        playbackHandoffNextBoundaryDelayed: 0,
        playbackHandoffQueuedLowPruned: 0,
        playbackHandoffQueuedUrgentPruned: 0,
        playbackCurrentFrameRecoveryCandidateCount: 0,
        playbackCurrentFrameRecoveryPendingExistingCount: 0,
        playbackCurrentFrameRecoveryMovedFrontCount: 0,
        playbackCurrentFrameRecoveryReclassifiedCount: 0,
        playbackCurrentFrameRecoveryQueuedPromotedCount: 0,
        playbackCurrentFrameRecoveryActiveReclassifiedCount: 0,
        playbackContinuationSafetyAdmitted: 0,
        playbackContinuationSafetySuppressed: 0,
        playbackDeadlineQueuedUrgentStalePruned: 0,
        playbackNonDeadlineQueuedUrgentStalePruned: 0,
        playbackLastHandoffPruneReason: "",
        playbackRebufferResumeDeferredSuppressed: 0,
        playbackRebufferLimitedSuppressed: 0,
        playbackRebufferResumeDeferredSig: "",
        playbackRebufferResumeDeferredAtMs: 0,
        playbackRebufferToastHandle: null,
        playbackRebufferToastTimer: null,
        playbackRebufferToastDismissTimer: null,
        playbackRebufferToastLevel: "",
        playbackRebufferToastShownAtMs: 0,
        playbackRebufferToastPressureMs: 0,
        playbackRebufferToastPressureAtMs: 0,
        playbackRebufferHeavyPressure: 0,
        playbackRebufferHeavyPressureAtMs: 0,
        playbackRebufferHeavyWarningAtMs: 0,
        playbackWarmTelemetrySig: "",
        playbackWarmTelemetryAtMs: 0,
        playbackWarmTelemetrySuppressed: 0,
        // Phase 1 playback telemetry accumulators (dev-only; only touched when
        // SONDER_DEBUG_SESSION or SONDER_DEBUG_PLAYBACK_BOUNDARY is on).
        playbackTelemetry: {
            lastFramesBehindMs: 0,
            maxFramesBehind: 0,
            lastQualitySampleMs: 0,
            qualityByKey: new Map(),
            blockReasons: new Map(),
            lastBlockFlushMs: 0,
        },
    };

    const noop = () => {};
    const getScene = options.getScene || (() => null);
    const getFrame = options.getFrame || (() => 0);
    const setFrame = options.setFrame || noop;
    const getTotalFrames = options.getTotalFrames || (() => 0);
    const getFps = options.getFps || (() => 24);
    const getLoopRange = options.getLoopRange || (() => null);
    const shouldReturnToPlaybackStart = options.shouldReturnToPlaybackStart || (() => false);
    const onFrameChange = options.onFrameChange || noop;
    const onTransportUpdate = options.onTransportUpdate || noop;
    const onPlaybackStateChange = options.onPlaybackStateChange || noop;
    const onPlaybackWarmStateChange = options.onPlaybackWarmStateChange || noop;
    const getAssetForSourcePath = options.getAssetForSourcePath || (() => null);
    const getGuideAsset = options.getGuideAsset || (() => null);
    const includeMotionDrivers = options.includeMotionDrivers || (() => false);
    const isVideoLaneHidden = options.isVideoLaneHidden || (() => false);
    const isMotionDriverLaneHidden = options.isMotionDriverLaneHidden || (() => false);
    const isAudioLaneHidden = options.isAudioLaneHidden || (() => false);
    const isGuideTrackHidden = options.isGuideTrackHidden || (() => false);
    const buildViewUrl = options.buildViewUrl || (() => null);
    const buildThumbnailUrl = options.buildThumbnailUrl || (() => null);
    const isPrebufferEnabled = options.isPrebufferEnabled || (() => true);
    const getPrebufferLookaheadMs = options.getPrebufferLookaheadMs || (() => 1000);
    const getStreamingMode = options.getStreamingMode || (() => "auto");
    const isSceneOutlineEnabled = options.isSceneOutlineEnabled || (() => false);
    const isAdaptiveRebufferEnabled = options.isAdaptiveRebufferEnabled || (() => true);
    const getRebufferEnterMs = options.getRebufferEnterMs || (() => 250);
    const getRebufferMaxMs = options.getRebufferMaxMs || (() => 4000);
    // Warm-ahead tuning knobs (browser-local playback settings; host-injected getters).
    // Fallbacks preserve current behavior for callers that do not wire them.
    const getPrebufferBoundaryDepth = options.getPrebufferBoundaryDepth || (() => PLAYBACK_PREBUFFER_BOUNDARY_DEPTH);
    const getPrebufferMaxEntries = options.getPrebufferMaxEntries || (() => PLAYBACK_PREBUFFER_MAX_ENTRIES);
    const getDecodeConcurrency = options.getDecodeConcurrency || (() => 2);
    // Injected notification emitter (Core `notifyInfo`-shaped: returns a handle with
    // update/resolve/dismiss, or null). No-op fallback keeps the surface decoupled.
    const notifyInfo = options.notifyInfo || (() => null);
    const notifyWarning = options.notifyWarning || (() => null);

    function currentFrame() {
        return clamp(Math.round(Number(getFrame()) || 0), 0, totalFrames());
    }

    function totalFrames() {
        return Math.max(0, Math.round(Number(getTotalFrames()) || 0));
    }

    function fps() {
        return Math.max(1, Number(getFps()) || 24);
    }

    function firstDrawTolerance() {
        return Math.max(0.04, 1 / fps());
    }

    function playbackDriftTolerance() {
        return Math.max(0.08, 2 / fps());
    }

    function debugPlaybackBoundary(eventName, details = {}) {
        if (typeof window === "undefined" || !window.SONDER_DEBUG_PLAYBACK_BOUNDARY) return;
        console.debug("[Sonder Playback Boundary]", eventName, details);
    }

    function playbackDebugEvent(eventName, details = {}) {
        if (typeof window === "undefined" || !window.SONDER_DEBUG_PLAYBACK_BOUNDARY) return;
        console.log("[Sonder Playback Boundary]", eventName, details);
    }

    function clearPlaybackDecisionLogs() {
        state.playbackDecisionLogKeys.clear();
    }

    function playbackDecisionDebugEvent(eventName, details = {}, keyParts = []) {
        if (typeof window === "undefined" || !window.SONDER_DEBUG_PLAYBACK_BOUNDARY) return;
        const logKey = [
            eventName,
            state.playbackSessionId,
            ...keyParts.map((part) => String(part ?? "")),
        ].join("|");
        if (state.playbackDecisionLogKeys.has(logKey)) return;
        state.playbackDecisionLogKeys.add(logKey);
        playbackDebugEvent(eventName, details);
    }

    // --- Phase 1 playback telemetry (dev-only) -------------------------------
    // All sampling is gated behind this check so it is zero-cost in normal use.
    // Records land in the SONDER_DEBUG_SESSION ring (__SONDER_CANVAS_DIAG via
    // viewportDiagRecord) and, when SONDER_DEBUG_PLAYBACK_BOUNDARY is on, also
    // print to the console. Surfaced through the existing Ctrl+Alt+Shift+D bundle.
    function playbackTelemetryActive() {
        return typeof window !== "undefined"
            && (window.SONDER_DEBUG_SESSION === true || !!window.SONDER_DEBUG_PLAYBACK_BOUNDARY);
    }

    function recordPlaybackTelemetry(kind, payload) {
        viewportDiagRecord(kind, payload);
        debugPlaybackBoundary(`telemetry:${kind}`, payload);
    }

    function roundTelemetryMs(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? Math.round(numeric * 10) / 10 : 0;
    }

    function videoElementDimensions(video) {
        return {
            videoWidth: Math.round(Number(video?.videoWidth) || 0),
            videoHeight: Math.round(Number(video?.videoHeight) || 0),
        };
    }

    function resetPlaybackTelemetry() {
        const t = state.playbackTelemetry;
        t.lastFramesBehindMs = 0;
        t.maxFramesBehind = 0;
        t.lastQualitySampleMs = 0;
        t.qualityByKey.clear();
        t.blockReasons.clear();
        t.lastBlockFlushMs = 0;
        state.lastBoundaryCoverageSig = "";
        state.lastPrebufferScheduleStats = emptyPrebufferScheduleStats("telemetry-reset");
        state.prebufferMissTelemetryKeys.clear();
        state.prebufferMissTelemetryEmitted = 0;
        state.prebufferMissTelemetrySuppressed = 0;
        state.prebufferPendingHoldCount = 0;
        state.expiredPendingPrebufferSkips.clear();
        state.lastRebufferLimitedScheduleSig = "";
        state.playbackWarmTelemetrySig = "";
        state.playbackWarmTelemetryAtMs = 0;
        state.playbackWarmTelemetrySuppressed = 0;
        state.prebufferPriorityUpgradeOutcomes.preservedRunning = 0;
        state.prebufferPriorityUpgradeOutcomes.promotedQueued = 0;
        state.prebufferPriorityUpgradeOutcomes.movedFront = 0;
        state.prebufferPriorityUpgradeOutcomes.reprioritizedQueued = 0;
        state.prebufferPriorityUpgradeOutcomes.recreatedQueued = 0;
        state.prebufferPriorityUpgradeOutcomes.alreadyFront = 0;
        resetPlaybackHandoffCounters();
        clearRebufferSafetyState();
        clearDeferredNextBoundaryTargets("telemetry-reset");
        playbackDecodeLimiter.resetStats();
    }

    function resetPlaybackHandoffCounters() {
        state.playbackHandoffCurrentTargetCount = 0;
        state.playbackHandoffContinuationSuppressed = 0;
        state.playbackHandoffUpcomingSuppressed = 0;
        state.playbackHandoffNextBoundaryDelayed = 0;
        state.playbackHandoffQueuedLowPruned = 0;
        state.playbackHandoffQueuedUrgentPruned = 0;
        state.playbackCurrentFrameRecoveryCandidateCount = 0;
        state.playbackCurrentFrameRecoveryPendingExistingCount = 0;
        state.playbackCurrentFrameRecoveryMovedFrontCount = 0;
        state.playbackCurrentFrameRecoveryReclassifiedCount = 0;
        state.playbackCurrentFrameRecoveryQueuedPromotedCount = 0;
        state.playbackCurrentFrameRecoveryActiveReclassifiedCount = 0;
        state.playbackContinuationSafetyAdmitted = 0;
        state.playbackContinuationSafetySuppressed = 0;
        state.playbackDeadlineQueuedUrgentStalePruned = 0;
        state.playbackNonDeadlineQueuedUrgentStalePruned = 0;
        state.playbackLastHandoffPruneReason = "";
    }

    // (1) Frames-behind-wall-clock: how far the free-running clock has advanced
    // past the last committed composite frame — i.e. the A/V desync, quantified.
    function recordFramesBehindTelemetry(timestamp, nextFrame, endFrame) {
        if (!playbackTelemetryActive()) return;
        const committed = state.playbackLastCommittedFrame;
        if (committed === null || !Number.isFinite(committed)) return;
        const behind = nextFrame - committed;
        if (behind < 2) return;
        const t = state.playbackTelemetry;
        if (behind > t.maxFramesBehind) t.maxFramesBehind = behind;
        if (timestamp - t.lastFramesBehindMs < 250) return;
        t.lastFramesBehindMs = timestamp;
        recordPlaybackTelemetry("playback_frames_behind", {
            behind,
            maxBehind: t.maxFramesBehind,
            committedFrame: committed,
            wallClockFrame: nextFrame,
            endFrame,
            fps: fps(),
            playbackSessionId: state.playbackSessionId,
        });
    }

    // (2) Decoder health: dropped vs total decoded frames per active video.
    // Separates "decoder fell behind" from "we never delivered the frame".
    function samplePlaybackQualityTelemetry() {
        if (!playbackTelemetryActive()) return;
        const now = performance.now();
        const t = state.playbackTelemetry;
        if (now - t.lastQualitySampleMs < 500) return;
        t.lastQualitySampleMs = now;
        for (const [key, active] of state.activePlaybackVideos.entries()) {
            const video = active?.video;
            if (typeof video?.getVideoPlaybackQuality !== "function") continue;
            const q = video.getVideoPlaybackQuality();
            const dropped = Number(q.droppedVideoFrames) || 0;
            const total = Number(q.totalVideoFrames) || 0;
            const prev = t.qualityByKey.get(key);
            if (!prev) {
                t.qualityByKey.set(key, { dropped, total });
                continue;
            }
            const deltaDropped = Math.max(0, dropped - prev.dropped);
            const deltaTotal = Math.max(0, total - prev.total);
            t.qualityByKey.set(key, { dropped, total });
            if (deltaTotal <= 0 && deltaDropped <= 0) continue;
            recordPlaybackTelemetry("playback_video_quality", {
                layerKey: key,
                sourcePath: active?.sourcePath || "",
                droppedTotal: dropped,
                totalTotal: total,
                droppedDelta: deltaDropped,
                totalDelta: deltaTotal,
                playbackSessionId: state.playbackSessionId,
            });
        }
    }

    // (4) Block-reason histogram: why drawPlaybackComposite couldn't commit, split
    // by whether a prepare was in flight (transient) vs not (likely permanent).
    function noteBlockReasonTelemetry(reason, inflight, timestamp) {
        if (!playbackTelemetryActive()) return;
        const t = state.playbackTelemetry;
        const bucket = `${reason || "unknown"}|${inflight ? "inflight" : "stalled"}`;
        t.blockReasons.set(bucket, (t.blockReasons.get(bucket) || 0) + 1);
        if (timestamp - t.lastBlockFlushMs >= 1000) flushBlockReasonTelemetry(timestamp);
    }

    function flushBlockReasonTelemetry(timestamp) {
        const t = state.playbackTelemetry;
        if (!t.blockReasons.size) return;
        t.lastBlockFlushMs = Number.isFinite(timestamp) ? timestamp : performance.now();
        recordPlaybackTelemetry("playback_block_reasons", {
            buckets: Object.fromEntries(t.blockReasons),
            playbackSessionId: state.playbackSessionId,
        });
        t.blockReasons.clear();
    }

    function schedulePlaybackWarmStateNotify(reason = "update") {
        if (state.playbackWarmNotifyRAF !== null) return;
        state.playbackWarmNotifyRAF = requestAnimationFrame(() => {
            state.playbackWarmNotifyRAF = null;
            const entries = Array.from(state.playbackWarmEntries.values());
            const stateCounts = entries.reduce((acc, entry) => {
                const key = entry?.state || "unknown";
                acc[key] = (acc[key] || 0) + 1;
                return acc;
            }, {});
            const payload = {
                generation: state.playbackWarmGeneration,
                entries,
                reason,
                playbackSessionId: state.playbackSessionId,
            };
            const now = performance.now();
            const telemetrySig = [
                reason,
                entries.length,
                JSON.stringify(stateCounts),
                state.playbackRebuffering ? state.playbackRebufferSafetySig : "",
            ].join("|");
            const suppressTelemetry = !!(
                state.playbackRebuffering
                && telemetrySig === state.playbackWarmTelemetrySig
                && now - state.playbackWarmTelemetryAtMs < PLAYBACK_REBUFFER_TELEMETRY_THROTTLE_MS
            );
            if (suppressTelemetry) {
                state.playbackWarmTelemetrySuppressed += 1;
            } else {
                const suppressedSinceLast = state.playbackWarmTelemetrySuppressed;
                state.playbackWarmTelemetrySuppressed = 0;
                state.playbackWarmTelemetrySig = telemetrySig;
                state.playbackWarmTelemetryAtMs = now;
                recordPlaybackTelemetry("playback_warm_state_update", {
                    generation: payload.generation,
                    entryCount: entries.length,
                    stateCounts,
                    reason,
                    suppressedSinceLast,
                    playbackSessionId: state.playbackSessionId,
                });
            }
            try {
                onPlaybackWarmStateChange(payload);
            } catch (e) {
                // The viewport owns playback; a host render bug must not break transport.
            }
        });
    }

    function cancelPlaybackWarmStateNotify() {
        if (state.playbackWarmNotifyRAF === null) return;
        cancelAnimationFrame(state.playbackWarmNotifyRAF);
        state.playbackWarmNotifyRAF = null;
    }

    function clearPlaybackWarmState(reason = "clear") {
        const hadEntries = state.playbackWarmEntries.size > 0;
        state.playbackWarmContentToken += 1;
        state.playbackWarmEntries.clear();
        state.prebufferMissTelemetryKeys.clear();
        state.prebufferMissTelemetryEmitted = 0;
        state.prebufferMissTelemetrySuppressed = 0;
        state.prebufferPendingHoldCount = 0;
        state.expiredPendingPrebufferSkips.clear();
        state.lastRebufferLimitedScheduleSig = "";
        state.playbackWarmTelemetrySig = "";
        state.playbackWarmTelemetryAtMs = 0;
        state.playbackWarmTelemetrySuppressed = 0;
        state.prebufferPriorityUpgradeOutcomes.preservedRunning = 0;
        state.prebufferPriorityUpgradeOutcomes.promotedQueued = 0;
        state.prebufferPriorityUpgradeOutcomes.movedFront = 0;
        state.prebufferPriorityUpgradeOutcomes.reprioritizedQueued = 0;
        state.prebufferPriorityUpgradeOutcomes.recreatedQueued = 0;
        state.prebufferPriorityUpgradeOutcomes.alreadyFront = 0;
        resetPlaybackHandoffCounters();
        clearRebufferSafetyState();
        clearDeferredNextBoundaryTargets("warm-state-clear");
        state.playbackWarmGeneration += 1;
        cancelPlaybackWarmStateNotify();
        recordPlaybackTelemetry("playback_warm_cache_clear", {
            generation: state.playbackWarmGeneration,
            reason,
            hadEntries,
            playbackSessionId: state.playbackSessionId,
        });
        try {
            onPlaybackWarmStateChange({
                generation: state.playbackWarmGeneration,
                entries: [],
                reason,
                playbackSessionId: state.playbackSessionId,
            });
        } catch (e) {
            // Keep cache teardown independent from host UI.
        }
    }

    function playbackWarmFrameRange(frame) {
        const startFrame = clamp(Math.round(Number(frame) || 0), 0, totalFrames());
        const endFrame = Math.min(totalFrames(), startFrame + 1);
        return endFrame > startFrame ? { startFrame, endFrame } : null;
    }

    function trimPlaybackWarmEntries() {
        while (state.playbackWarmEntries.size > PLAYBACK_WARM_MAX_ENTRIES) {
            const firstKey = state.playbackWarmEntries.keys().next().value;
            if (firstKey === undefined) break;
            state.playbackWarmEntries.delete(firstKey);
        }
    }

    function playbackWarmIdentityForLayer(layer, warmState, options = {}) {
        const owner = options.owner || "active";
        const ownerKey = options.ownerKey || layer?.key || "";
        return {
            layerKey: layer?.key || "",
            sourcePath: layer?.clip?.source_path || "",
            laneIndex: Math.max(0, Math.round(Number(layer?.clip?.track_index) || 0)),
            trackType: layer?.clip?.role === "motion_driver" ? "motion_driver" : "video",
            state: warmState,
            owner,
            ownerKey,
        };
    }

    function playbackWarmEntriesOverlap(entry, range) {
        return !!entry && !!range && entry.startFrame < range.endFrame && entry.endFrame > range.startFrame;
    }

    function playbackWarmEntriesTouch(entry, range) {
        return !!entry && !!range && entry.startFrame <= range.endFrame && entry.endFrame >= range.startFrame;
    }

    function playbackWarmCanMerge(entry, identity) {
        return !!entry
            && entry.layerKey === identity.layerKey
            && entry.sourcePath === identity.sourcePath
            && entry.laneIndex === identity.laneIndex
            && entry.trackType === identity.trackType
            && entry.state === identity.state
            && entry.owner === identity.owner
            && entry.ownerKey === identity.ownerKey;
    }

    function removePlaybackWarmEntriesByOwner(owner, ownerKey, reason = "owner-clear") {
        if (!owner || !ownerKey) return;
        let removed = false;
        for (const [key, entry] of Array.from(state.playbackWarmEntries.entries())) {
            if (entry.owner !== owner || entry.ownerKey !== ownerKey) continue;
            state.playbackWarmEntries.delete(key);
            removed = true;
        }
        if (!removed) return;
        state.playbackWarmGeneration += 1;
        schedulePlaybackWarmStateNotify(reason);
    }

    function pruneTransientPlaybackWarmEntries(reason = "transient-clear") {
        let removed = false;
        for (const [key, entry] of Array.from(state.playbackWarmEntries.entries())) {
            if (entry?.state === "warm") continue;
            state.playbackWarmEntries.delete(key);
            removed = true;
        }
        if (!removed) return;
        state.playbackWarmGeneration += 1;
        schedulePlaybackWarmStateNotify(reason);
    }

    function notePlaybackWarmLayer(layer, frame, warmState, reason, options = {}) {
        const token = options.token ?? state.playbackWarmContentToken;
        if (token !== state.playbackWarmContentToken) return;
        const range = playbackWarmFrameRange(frame);
        if (!range || !layer?.clip || !layer?.key) return;
        const identity = playbackWarmIdentityForLayer(layer, warmState, options);
        const sameSourceEntries = Array.from(state.playbackWarmEntries.entries())
            .filter(([, entry]) => entry.layerKey === identity.layerKey && entry.sourcePath === identity.sourcePath);
        const overlapping = sameSourceEntries.filter(([, entry]) => playbackWarmEntriesOverlap(entry, range));
        const exactCover = overlapping.find(([, entry]) => (
            playbackWarmCanMerge(entry, identity)
            && entry.startFrame <= range.startFrame
            && entry.endFrame >= range.endFrame
        ));
        if (exactCover) return;

        const mayReplaceWarm = warmState === "warm" || options.replaceWarm || reason === "missing-layer";
        if (!mayReplaceWarm && overlapping.some(([, entry]) => entry.state === "warm")) return;
        for (const [key] of overlapping) {
            state.playbackWarmEntries.delete(key);
        }

        const entry = {
            key: "",
            ...identity,
            startFrame: range.startFrame,
            endFrame: range.endFrame,
            reason,
            updatedAtMs: Math.round(performance.now()),
        };

        let merged = true;
        while (merged) {
            merged = false;
            for (const [key, candidate] of Array.from(state.playbackWarmEntries.entries())) {
                if (!playbackWarmCanMerge(candidate, identity)) continue;
                if (!playbackWarmEntriesTouch(candidate, entry)) continue;
                entry.startFrame = Math.min(entry.startFrame, candidate.startFrame);
                entry.endFrame = Math.max(entry.endFrame, candidate.endFrame);
                state.playbackWarmEntries.delete(key);
                merged = true;
            }
        }

        entry.key = `warm:${++state.playbackWarmEntrySeq}`;
        state.playbackWarmEntries.set(entry.key, entry);
        state.playbackWarmGeneration += 1;
        trimPlaybackWarmEntries();
        schedulePlaybackWarmStateNotify(reason);
    }

    function notePlaybackWarmMissingLayers(snapshot, reason = "missing") {
        for (const layer of snapshot?.missingClipLayers || []) {
            notePlaybackWarmLayer(layer, snapshot.frame, "blocked", reason);
        }
    }

    function invalidateAsyncPreviewRenders() {
        state.renderToken += 1;
    }

    function resetPlaybackCompositeState() {
        state.playbackCompositeCommitted = false;
        state.playbackBlockedSinceMs = null;
        state.playbackBlockedSignature = "";
        state.playbackCanvasWidth = 0;
        state.playbackCanvasHeight = 0;
        state.playbackLastCommittedFrame = null;
        state.playbackLastCommittedSignature = "";
        state.playbackLastCommittedSessionId = 0;
        state.lastBoundaryCoverageSig = "";
        state.lastPrebufferScheduleStats = emptyPrebufferScheduleStats("composite-reset");
    }

    function beginFirstCommitHold(timestamp, frame) {
        state.playbackFirstCommitStartedAt = Number.isFinite(timestamp) ? timestamp : performance.now();
        state.playbackFirstCommitFrame = clamp(Math.round(Number(frame) || 0), 0, totalFrames());
        state.playbackFirstCommitHoldExpired = false;
    }

    function clearFirstCommitHold() {
        state.playbackFirstCommitStartedAt = null;
        state.playbackFirstCommitFrame = null;
        state.playbackFirstCommitHoldExpired = false;
    }

    function resetAudioReleaseLatch() {
        state.audioReleasedThisSession = false;
        state.audioFreezeLogged = false;
        state.audioReleaseLogged = false;
    }

    function releaseAudioForSession(reason, details = {}) {
        if (!state.audioReleasedThisSession) {
            state.audioReleasedThisSession = true;
            if (!state.audioReleaseLogged) {
                playbackDebugEvent("audio-released", {
                    reason,
                    playbackSessionId: state.playbackSessionId,
                    ...details,
                });
                state.audioReleaseLogged = true;
            }
        }
    }

    function playbackCanvasStillValid() {
        return !!(
            state.playbackCompositeCommitted
            && state.canvas
            && state.playbackCanvasWidth === state.canvas.width
            && state.playbackCanvasHeight === state.canvas.height
        );
    }

    function notifyTransport() {
        onTransportUpdate({
            frame: currentFrame(),
            totalFrames: totalFrames(),
            isPlaying: state.isPlaying,
            liveMediaEnabled: state.liveMediaEnabled,
        });
    }

    function updatePlaybackState(nextValue) {
        if (state.isPlaying === nextValue) {
            notifyTransport();
            return;
        }
        state.isPlaying = nextValue;
        onPlaybackStateChange(nextValue);
        notifyTransport();
    }

    function applyFrame(nextFrame, meta = {}) {
        const clampedFrame = clamp(Math.round(Number(nextFrame) || 0), 0, totalFrames());
        setFrame(clampedFrame, meta);
        onFrameChange(clampedFrame, meta);
        notifyTransport();
        return clampedFrame;
    }

    function getGuideAtFrame(frame) {
        const scene = getScene();
        if (!scene?.guide_frames?.length) return null;
        if (isGuideTrackHidden()) return null;
        const lastFrame = Math.max(0, totalFrames() - 1);
        let best = null;
        let bestFrame = -1;
        for (const guide of scene.guide_frames) {
            if (guide.muted) continue;
            const frameIndex = guide.frame_index === -1 ? lastFrame : Math.max(0, parseInt(guide.frame_index, 10) || 0);
            if (frameIndex <= frame && frameIndex >= bestFrame) {
                best = guide;
                bestFrame = frameIndex;
            }
        }
        return best;
    }

    function getVisibleClipLayers(frame) {
        const scene = getScene();
        if (!scene?.clips?.length) return [];
        return scene.clips
            .filter((clip) => frame >= clip.timeline_start_frame && frame < clip.timeline_end_frame)
            .filter((clip) => !clip.muted)
            .filter((clip) => {
                if (!clip.role || clip.role === "render") return true;
                return clip.role === "motion_driver" && includeMotionDrivers();
            })
            .filter((clip) => {
                if (clip.role === "motion_driver") {
                    return !isMotionDriverLaneHidden(clip.track_index || 0);
                }
                return !isVideoLaneHidden(clip.track_index || 0);
            })
            .sort((a, b) => (a.track_index || 0) - (b.track_index || 0))
            .map((clip) => ({
                clip,
                asset: getAssetForSourcePath(clip.source_path) || null,
                key: clip.clip_id || `${clip.source_path}:${clip.timeline_start_frame}:${clip.track_index || 0}`,
                opacity: clamp(Number(clip.opacity ?? 1), 0, 1),
            }));
    }

    function getVisibleAudioLayers(frame) {
        const scene = getScene();
        if (!scene?.audio_tracks?.length) return [];
        return scene.audio_tracks
            .filter((track) => frame >= track.timeline_start_frame && frame < track.timeline_end_frame)
            .filter((track) => !isAudioLaneHidden(track.lane_index || 0))
            .filter((track) => !track.muted)
            .map((track) => ({
                track,
                asset: getAssetForSourcePath(track.source_path) || null,
                key: track.track_id || `${track.source_path}:${track.timeline_start_frame}:${track.lane_index || 0}`,
            }))
            .filter((layer) => layer.asset && !layer.asset.missing);
    }

    function buildFrameSnapshot(frame) {
        const guide = getGuideAtFrame(frame);
        const guideAsset = guide ? getGuideAsset(guide) : null;
        const clipLayers = getVisibleClipLayers(frame);
        const playableClipLayers = clipLayers.filter((layer) => layer.asset && !layer.asset.missing);
        const missingClipLayers = clipLayers.filter((layer) => !layer.asset || !!layer.asset.missing);
        return {
            frame,
            guide,
            guideAsset,
            clipLayers,
            playableClipLayers,
            missingClipLayers,
            audioLayers: getVisibleAudioLayers(frame),
        };
    }

    function getCanvasContext() {
        if (!state.canvas) return null;
        if (!state.ctx) {
            state.ctx = state.canvas.getContext("2d");
        }
        return state.ctx;
    }

    function drawBlack() {
        const ctx = getCanvasContext();
        if (!ctx || !state.canvas) return null;
        ctx.fillStyle = THEME.bg0;
        ctx.fillRect(0, 0, state.canvas.width, state.canvas.height);
        return ctx;
    }

    function drawViewportText(title, subtitle = "", palette = {}) {
        const ctx = drawBlack();
        if (!ctx || !state.canvas) return;
        const width = state.canvas.width;
        const height = state.canvas.height;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = palette.titleColor || THEME.fg3;
        ctx.font = `400 ${Math.max(16, height / 12)}px ${FONT.mono}`;
        ctx.fillText(title, width / 2, height / 2 - (subtitle ? 12 : 0));
        if (subtitle) {
            ctx.fillStyle = palette.subtitleColor || THEME.fg2;
            ctx.font = `400 ${Math.max(11, height / 24)}px ${FONT.sans}`;
            ctx.fillText(subtitle, width / 2, height / 2 + 16);
        }
    }

    function drawImageLike(element, { opacity = 1, fitMode = "pad_edge", cropPosition = "center" } = {}) {
        const ctx = getCanvasContext();
        if (!ctx || !state.canvas || !element) return false;
        const canvasW = state.canvas.width;
        const canvasH = state.canvas.height;
        const width = element.videoWidth || element.naturalWidth || element.width || canvasW;
        const height = element.videoHeight || element.naturalHeight || element.height || canvasH;
        const mode = VIEWPORT_FIT_MODES.has(fitMode) ? fitMode : "pad_edge";
        const previousAlpha = ctx.globalAlpha;
        ctx.globalAlpha = clamp(Number(opacity) || 0, 0, 1);
        try {
            if (mode === "stretch") {
                ctx.drawImage(element, 0, 0, canvasW, canvasH);
            } else if (mode === "cover") {
                // Source sub-rect that fills the canvas, anchored by cropPosition.
                // Math mirrors the backend cover crop so preview and render agree.
                const scale = Math.max(canvasW / width, canvasH / height);
                const srcW = Math.min(width, canvasW / scale);
                const srcH = Math.min(height, canvasH / scale);
                const xExtra = width - srcW;
                const yExtra = height - srcH;
                let sx = xExtra / 2;
                if (cropPosition === "left") sx = 0;
                else if (cropPosition === "right") sx = xExtra;
                let sy = yExtra / 2;
                if (cropPosition === "top") sy = 0;
                else if (cropPosition === "bottom") sy = yExtra;
                ctx.drawImage(element, sx, sy, srcW, srcH, 0, 0, canvasW, canvasH);
            } else {
                // fit / pad_edge: contain, centered.
                const rect = fitRect(width, height, canvasW, canvasH);
                ctx.drawImage(element, rect.x, rect.y, rect.width, rect.height);
                if (mode === "pad_edge") {
                    drawEdgePadBars(ctx, element, rect, canvasW, canvasH, width, height);
                }
            }
            return true;
        } catch (error) {
            return false;
        } finally {
            ctx.globalAlpha = previousAlpha;
        }
    }

    // Scene workspace outline. The canvas buffer is sized to the scene aspect
    // exactly, so the scene frame == the canvas edge; inset ~1px so the half-pixel
    // stroke isn't clipped. Drawn at the tail of ALL composite paths (playback,
    // live, static) so it stays visible while paused/scrubbing — exactly when
    // reading pad_edge / cover boundaries matters most.
    function drawSceneOutline() {
        if (!isSceneOutlineEnabled()) return;
        const ctx = getCanvasContext();
        if (!ctx || !state.canvas) return;
        const w = state.canvas.width;
        const h = state.canvas.height;
        if (w < 3 || h < 3) return;
        const previousAlpha = ctx.globalAlpha;
        ctx.save();
        ctx.globalAlpha = 1;
        ctx.lineWidth = 1;
        ctx.strokeStyle = THEME.fg3;
        ctx.strokeRect(0.5, 0.5, w - 1, h - 1);
        ctx.restore();
        ctx.globalAlpha = previousAlpha;
    }

    function imageLikeDimensions(element) {
        const width = Number(element?.videoWidth || element?.naturalWidth || element?.width);
        const height = Number(element?.videoHeight || element?.naturalHeight || element?.height);
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;
        return { width, height };
    }

    function imageLikeDrawRect(element) {
        if (!state.canvas || !element) return null;
        const dimensions = imageLikeDimensions(element);
        if (!dimensions) return null;
        return fitRect(dimensions.width, dimensions.height, state.canvas.width, state.canvas.height);
    }

    function imageLikeCoversCanvas(element, opacity = 1, fitOptions = {}) {
        if (!state.canvas || clamp(Number(opacity) || 0, 0, 1) < PLAYBACK_OPAQUE_OPACITY) return false;
        const dimensions = imageLikeDimensions(element);
        if (!dimensions) return false;
        const mode = VIEWPORT_FIT_MODES.has(fitOptions?.fitMode) ? fitOptions.fitMode : "pad_edge";
        if (mode === "stretch" || mode === "cover" || mode === "pad_edge") return true;
        const rect = imageLikeDrawRect(element);
        if (!rect) return false;
        return (
            rect.x <= PLAYBACK_COVERAGE_EPSILON
            && rect.y <= PLAYBACK_COVERAGE_EPSILON
            && rect.x + rect.width >= state.canvas.width - PLAYBACK_COVERAGE_EPSILON
            && rect.y + rect.height >= state.canvas.height - PLAYBACK_COVERAGE_EPSILON
        );
    }

    function resolvePreviewImageUrl(layer) {
        if (!layer?.asset) return null;
        if (layer.asset.asset_type === "image") {
            return buildViewUrl(layer.asset.path || layer.clip?.source_path || "");
        }
        if (layer.asset.asset_id) {
            return buildThumbnailUrl(layer.asset.asset_id);
        }
        return null;
    }

    function loadImage(cacheKey, src) {
        if (!cacheKey || !src) return Promise.resolve(null);
        const existing = state.imageCache[cacheKey];
        if (existing?.src === src && existing.img) {
            return Promise.resolve(existing.img);
        }
        if (existing?.src === src && existing.promise) {
            return existing.promise;
        }
        const img = new Image();
        img.crossOrigin = "anonymous";
        const promise = new Promise((resolve) => {
            img.onload = () => {
                state.imageCache[cacheKey] = { src, img, promise: null };
                resolve(img);
            };
            img.onerror = () => {
                state.imageCache[cacheKey] = { src, img: null, promise: null };
                resolve(null);
            };
        });
        state.imageCache[cacheKey] = { src, img: null, promise };
        img.src = src;
        return promise;
    }

    function getReadyImage(cacheKey, src, { rerenderOnLoad = false } = {}) {
        if (!cacheKey || !src) return null;
        const existing = state.imageCache[cacheKey];
        if (existing?.src === src && existing.img) {
            return existing.img;
        }
        if (!(existing?.src === src && existing.promise)) {
            loadImage(cacheKey, src).then(() => {
                if (rerenderOnLoad && !state.destroyed) {
                    renderFrame();
                }
            });
        }
        return null;
    }

    function effectiveSurfaceStreamingMode(forceBlob = false) {
        if (forceBlob) return "blob";
        return getStreamingMode() === "direct" ? "direct" : "blob";
    }

    function mediaSourceCacheKey(sourcePath, { forceBlob = false } = {}) {
        if (!sourcePath) return "";
        return `${effectiveSurfaceStreamingMode(forceBlob)}:${sourcePath}`;
    }

    function maybeReleaseSourceCacheEntry(cacheKey) {
        if (!cacheKey) return;
        const entry = state.sourceUrlCache.get(cacheKey);
        if (!entry || (entry.holders && entry.holders.size > 0)) return;
        state.sourceUrlCache.delete(cacheKey);
        if (entry.usesObjectUrl && entry.objectUrl) {
            try {
                URL.revokeObjectURL(entry.objectUrl);
            } catch (error) {}
        }
        entry.objectUrl = null;
    }

    function releaseMediaElementSource(mediaEl) {
        if (!mediaEl) return;
        const cacheKey = mediaEl._sonderSourceCacheKey || "";
        if (cacheKey) {
            const entry = state.sourceUrlCache.get(cacheKey);
            if (entry?.holders) entry.holders.delete(mediaEl);
            maybeReleaseSourceCacheEntry(cacheKey);
        }
        mediaEl._sonderSourceUrl = "";
        mediaEl._sonderSourceCacheKey = "";
    }

    function acquireMediaElementSource(mediaEl, cacheKey, sourceUrl) {
        if (!mediaEl || !cacheKey || !sourceUrl) return false;
        if (mediaEl._sonderSourceCacheKey === cacheKey && mediaEl._sonderSourceUrl === sourceUrl) {
            return true;
        }
        releaseMediaElementSource(mediaEl);
        const entry = state.sourceUrlCache.get(cacheKey);
        if (!entry) return false;
        if (!entry.holders) entry.holders = new Set();
        entry.holders.add(mediaEl);
        mediaEl._sonderSourceCacheKey = cacheKey;
        mediaEl._sonderSourceUrl = sourceUrl;
        mediaEl._sonderReleaseSourceRef = releaseMediaElementSource;
        mediaEl.src = sourceUrl;
        return true;
    }

    // forceBlob pins a caller to whole-file blob loading regardless of the
    // streaming mode (guide-snapshot capture is a frame-accuracy correctness
    // path). Surface auto/blob loads and forced-blob loads share `blob:...`;
    // explicit direct opt-in gets its own `direct:...` entry.
    function resolveMediaSourceUrl(sourcePath, { forceBlob = false } = {}) {
        if (!sourcePath) return Promise.resolve(null);
        const mode = effectiveSurfaceStreamingMode(forceBlob);
        const cacheKey = mediaSourceCacheKey(sourcePath, { forceBlob });
        const cached = state.sourceUrlCache.get(cacheKey);
        if (cached?.promise) {
            return cached.promise;
        }
        const directUrl = buildViewUrl(sourcePath);
        if (!directUrl) return Promise.resolve(null);
        const entry = {
            key: cacheKey,
            sourcePath,
            mode,
            holders: new Set(),
            objectUrl: null,
            usesObjectUrl: false,
            promise: null,
        };
        const loadAsBlob = () => {
            const startedAt = performance.now();
            return fetch(directUrl)
                .then((response) => {
                    if (!response.ok) {
                        throw new Error(`Failed to fetch media: ${response.status}`);
                    }
                    return response.blob();
                })
                .then((blob) => {
                    entry.objectUrl = URL.createObjectURL(blob);
                    entry.usesObjectUrl = true;
                    if (state.sourceUrlCache.get(cacheKey) !== entry || state.destroyed) {
                        URL.revokeObjectURL(entry.objectUrl);
                        entry.objectUrl = null;
                        return null;
                    }
                    viewportDiagRecord("resolve_media_source", {
                        source_path: sourcePath,
                        mode: "blob",
                        forced: forceBlob,
                        duration_ms: Math.round(performance.now() - startedAt),
                        blob_size: blob.size,
                    });
                    return entry.objectUrl;
                })
                .catch((error) => {
                    if (state.sourceUrlCache.get(cacheKey) !== entry || state.destroyed) {
                        return null;
                    }
                    console.warn("[Sonder] Failed to load media as blob, falling back to direct URL:", error);
                    playbackDebugEvent("resolve-media-source-fallback", {
                        sourcePath,
                        requestedMode: mode,
                        forced: forceBlob,
                        error: String(error?.message || error || ""),
                    });
                    entry.objectUrl = directUrl;
                    entry.usesObjectUrl = false;
                    return directUrl;
                });
        };
        entry.promise = Promise.resolve().then(() => {
            if (state.sourceUrlCache.get(cacheKey) !== entry || state.destroyed) {
                return null;
            }
            playbackDebugEvent("resolve-media-source", { sourcePath, mode, forced: forceBlob });
            if (mode === "direct") {
                entry.objectUrl = directUrl;
                entry.usesObjectUrl = false;
                viewportDiagRecord("resolve_media_source", { source_path: sourcePath, mode: "direct" });
                return directUrl;
            }
            return loadAsBlob();
        });
        state.sourceUrlCache.set(cacheKey, entry);
        return entry.promise;
    }

    function getOrCreateVideo(layer) {
        if (!layer?.key) return null;
        if (state.videoCache[layer.key]?.error) {
            removeMediaSource(state.videoCache[layer.key]);
            delete state.videoCache[layer.key];
        }
        if (!state.videoCache[layer.key]) {
            const video = document.createElement("video");
            video.preload = "metadata";
            video.muted = true;
            video.playsInline = true;
            state.videoCache[layer.key] = video;
        }
        return state.videoCache[layer.key];
    }

    function getOrCreateAudio(layer) {
        if (!layer?.key) return null;
        if (state.audioCache[layer.key]?.error) {
            removeMediaSource(state.audioCache[layer.key]);
            delete state.audioCache[layer.key];
        }
        if (!state.audioCache[layer.key]) {
            const audio = document.createElement("audio");
            audio.preload = "auto";
            state.audioCache[layer.key] = audio;
        }
        return state.audioCache[layer.key];
    }

    function createMutedVideoElement() {
        const video = document.createElement("video");
        video.preload = "metadata";
        video.muted = true;
        video.playsInline = true;
        return video;
    }

    function isRenderableVideoLayer(layer) {
        return !!(
            layer?.key
            && layer?.clip?.source_path
            && layer.asset
            && !layer.asset.missing
            && layer.asset.asset_type !== "image"
        );
    }

    function snapshotHasPlayableVideo(snapshot) {
        return (snapshot?.playableClipLayers || []).some(isRenderableVideoLayer);
    }

    function audioPlaybackAllowed(snapshot) {
        // While rebuffering, audio is frozen with the clock — never let the
        // syncPlaybackMedia resume-play branch un-pause it mid-hold.
        if (state.playbackRebuffering) return false;
        const allowed = state.audioReleasedThisSession || !snapshotHasPlayableVideo(snapshot);
        if (!allowed && !state.audioFreezeLogged) {
            playbackDebugEvent("audio-frozen", {
                frame: snapshot?.frame,
                playbackSessionId: state.playbackSessionId,
            });
            state.audioFreezeLogged = true;
        }
        return allowed;
    }

    function prebufferTargetTimeTolerance() {
        return Math.max(0.012, Math.min(0.025, 0.45 / fps()));
    }

    function sourceFrameTime(sourceFrame) {
        return Math.max(0, (Math.max(0, Math.round(Number(sourceFrame) || 0)) + 0.5) / fps());
    }

    function prebufferSourceTargetKey(layer, frame) {
        if (!isRenderableVideoLayer(layer)) return "";
        const sourcePath = layer.clip.source_path || "";
        return `${sourcePath}::source-frame:${clipSourceFrame(layer, frame)}`;
    }

    function prebufferKeyForLayer(layer, frame = currentFrame()) {
        if (!isRenderableVideoLayer(layer)) return "";
        const sourceTargetKey = prebufferSourceTargetKey(layer, frame);
        return sourceTargetKey ? `${layer.key}::${sourceTargetKey}` : "";
    }

    function prebufferTargetForLayer(layer, frame, options = {}) {
        const key = prebufferKeyForLayer(layer, frame);
        if (!key) return null;
        const targetSourceFrame = clipSourceFrame(layer, frame);
        return {
            key,
            layer,
            layerKey: layer.key,
            sourcePath: layer.clip.source_path,
            targetFrame: Math.round(Number(frame) || 0),
            targetSourceFrame,
            sourceTargetKey: prebufferSourceTargetKey(layer, frame),
            targetTime: sourceFrameTime(targetSourceFrame),
            intent: options.intent || "warm-ahead",
            decodePriority: options.decodePriority || DECODE_PRIORITY_LOW,
            scheduleOrigin: options.scheduleOrigin || "",
        };
    }

    function normalizedPrebufferLookaheadMs() {
        const numeric = Number(getPrebufferLookaheadMs());
        return clamp(Number.isFinite(numeric) ? Math.round(numeric) : 1000, 100, 5000);
    }

    function normalizedPrebufferBoundaryDepth() {
        const numeric = Number(getPrebufferBoundaryDepth());
        return clamp(Number.isFinite(numeric) ? Math.round(numeric) : PLAYBACK_PREBUFFER_BOUNDARY_DEPTH, 1, 12);
    }

    function normalizedPrebufferMaxEntries() {
        const numeric = Number(getPrebufferMaxEntries());
        return clamp(Number.isFinite(numeric) ? Math.round(numeric) : PLAYBACK_PREBUFFER_MAX_ENTRIES, 1, 64);
    }

    function normalizedDecodeConcurrency() {
        const numeric = Number(getDecodeConcurrency());
        return clamp(Number.isFinite(numeric) ? Math.round(numeric) : 2, 1, 8);
    }

    function currentSceneClipKeySet() {
        const scene = getScene();
        const keys = new Set();
        for (const clip of scene?.clips || []) {
            if (clip?.clip_id) keys.add(clip.clip_id);
        }
        return keys;
    }

    function playbackFrameDistance(fromFrame, targetFrame, endFrame) {
        const from = Math.max(0, Math.round(Number(fromFrame) || 0));
        const target = Math.max(0, Math.round(Number(targetFrame) || 0));
        const loopRange = state.playbackLoopRange;
        if (!loopRange) return target - from;
        const loopStart = Math.max(0, Math.round(Number(loopRange.start) || 0));
        const loopEnd = Math.max(loopStart + 1, Math.round(Number(endFrame) || Number(loopRange.end) || loopStart + 1));
        if (target >= from) return target - from;
        return Math.max(0, loopEnd - from) + Math.max(0, target - loopStart);
    }

    function deadlineClassForScheduleOrigin(scheduleOrigin, decodePriority = DECODE_PRIORITY_LOW) {
        if (scheduleOrigin === "current-frame-recovery") return DECODE_DEADLINE_CURRENT_FRAME;
        if (scheduleOrigin === "current-safety") return DECODE_DEADLINE_CURRENT_SAFETY;
        if (scheduleOrigin === "rebuffer-next-boundary") return DECODE_DEADLINE_REBUFFER_NEXT_BOUNDARY;
        if (decodePriority === DECODE_PRIORITY_URGENT) return DECODE_DEADLINE_URGENT_OTHER;
        return DECODE_DEADLINE_NONE;
    }

    function applyPrebufferDeadlineMetadata(target, fromFrame = currentFrame(), endFrame = null) {
        if (!target) return target;
        const scheduleOrigin = target.scheduleOrigin || "";
        const deadlineClass = deadlineClassForScheduleOrigin(scheduleOrigin, target.decodePriority || DECODE_PRIORITY_LOW);
        const playbackEndFrame = endFrame ?? (state.playbackLoopRange ? state.playbackLoopRange.end : totalFrames());
        const deadlineFrame = Math.max(0, Math.round(Number(target.targetFrame) || 0));
        return {
            ...target,
            deadlineClass,
            deadlineFrame,
            deadlineDistanceFrames: playbackFrameDistance(fromFrame, deadlineFrame, playbackEndFrame),
        };
    }

    function prebufferDeadlineForEntry(entry) {
        return normalizeDecodeDeadlineMeta({
            deadlineClass: entry?.deadlineClass || deadlineClassForScheduleOrigin(entry?.scheduleOrigin || "", entry?.decodePriority || DECODE_PRIORITY_LOW),
            deadlineFrame: entry?.deadlineFrame ?? entry?.targetFrame,
            deadlineDistanceFrames: entry?.deadlineDistanceFrames,
            scheduleOrigin: entry?.scheduleOrigin || "",
            targetFrame: entry?.targetFrame,
            targetSourceFrame: entry?.targetSourceFrame,
            sourceTargetKey: entry?.sourceTargetKey || "",
            targetKey: entry?.key || "",
            layerKey: entry?.layerKey || "",
        });
    }

    function prebufferDeadlineForTarget(target) {
        return normalizeDecodeDeadlineMeta({
            deadlineClass: target?.deadlineClass || deadlineClassForScheduleOrigin(target?.scheduleOrigin || "", target?.decodePriority || DECODE_PRIORITY_LOW),
            deadlineFrame: target?.deadlineFrame ?? target?.targetFrame,
            deadlineDistanceFrames: target?.deadlineDistanceFrames,
            scheduleOrigin: target?.scheduleOrigin || "",
            targetFrame: target?.targetFrame,
            targetSourceFrame: target?.targetSourceFrame,
            sourceTargetKey: target?.sourceTargetKey || "",
            targetKey: target?.key || "",
            layerKey: target?.layerKey || "",
        });
    }

    function prebufferDeadlineTelemetryFromEntry(entry) {
        return decodeDeadlineTelemetry(prebufferDeadlineForEntry(entry));
    }

    function prebufferDeadlineTelemetryFromTarget(target) {
        return decodeDeadlineTelemetry(prebufferDeadlineForTarget(target));
    }

    function prebufferEntryEffectiveTargetFrame(entry) {
        const recoveryFrame = Number(entry?.currentFrameRecoveryTargetFrame);
        if (
            Number.isFinite(recoveryFrame)
            && (entry?.scheduleOrigin === "current-frame-recovery" || entry?.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME)
        ) {
            return Math.max(0, Math.round(recoveryFrame));
        }
        return Math.max(0, Math.round(Number(entry?.targetFrame) || 0));
    }

    function prebufferEntryDecodeStillRelevant(entry) {
        if (!entry || entry.cancelled || entry.consumed) return false;
        if (entry.warmToken !== state.playbackWarmContentToken) return false;
        if (entry.ready || playbackWaitingForPrebufferEntry(entry)) return true;
        const targetFrame = prebufferEntryEffectiveTargetFrame(entry);
        const current = currentFrame();
        if (entry.scheduleOrigin === "current-frame-recovery" || entry.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME) {
            if (targetFrame === current) return true;
            if (
                state.playbackRebuffering
                && targetFrame === Math.max(0, Math.round(Number(state.playbackRebufferFrame) || 0))
            ) {
                return true;
            }
        }
        if (targetFrame >= current) return true;
        if (state.playbackRebuffering && targetFrame === Math.max(0, Math.round(Number(state.playbackRebufferFrame) || 0))) {
            return true;
        }
        const loopRange = state.playbackLoopRange;
        if (loopRange) {
            const loopStart = Math.max(0, Math.round(Number(loopRange.start) || 0));
            const loopEnd = Math.max(loopStart + 1, Math.round(Number(loopRange.end) || loopStart + 1));
            if (targetFrame < loopStart || targetFrame >= loopEnd) return false;
            const wrapDistance = playbackFrameDistance(current, targetFrame, loopEnd);
            return wrapDistance <= Math.max(
                PLAYBACK_CURRENT_BOUNDARY_HOLD_FRAMES,
                normalizedRebufferResumeSafetyFrames(),
            );
        }
        return false;
    }

    function isDeadlineUrgentEntry(entry) {
        const priority = entry?.decodeJobPriority || entry?.decodePriority || DECODE_PRIORITY_LOW;
        return priority === DECODE_PRIORITY_URGENT && decodeDeadlineRank(entry?.deadlineClass) > decodeDeadlineRank(DECODE_DEADLINE_URGENT_OTHER);
    }

    function dropStaleQueuedUrgentPrebuffers(reason = "stale-urgent-deadline") {
        let deadline = 0;
        let nonDeadline = 0;
        for (const [key, entry] of Array.from(state.prebufferCache.entries())) {
            if (!entry || entry.ready || entry.claimedByActive || playbackWaitingForPrebufferEntry(entry)) continue;
            if (entry.decodeJobState !== "queued" || !entry.decodeJob) continue;
            const priority = entry.decodeJobPriority || entry.decodePriority || DECODE_PRIORITY_LOW;
            if (priority !== DECODE_PRIORITY_URGENT) continue;
            if (prebufferEntryDecodeStillRelevant(entry)) continue;
            if (!playbackDecodeLimiter.cancelQueued(entry.decodeJob, reason)) continue;
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
            if (isDeadlineUrgentEntry(entry)) {
                deadline += 1;
            } else {
                nonDeadline += 1;
            }
        }
        state.playbackDeadlineQueuedUrgentStalePruned += deadline;
        state.playbackNonDeadlineQueuedUrgentStalePruned += nonDeadline;
        return { deadline, nonDeadline };
    }

    function singleSlotLowAllowed() {
        if (!state.isPlaying || state.playbackRebuffering) return false;
        if (normalizedDecodeConcurrency() > 1) return true;
        const playbackEndFrame = state.playbackLoopRange
            ? state.playbackLoopRange.end
            : totalFrames();
        const frame = currentFrame();
        for (let offset = 0; offset <= PLAYBACK_CURRENT_BOUNDARY_HOLD_FRAMES; offset += 1) {
            const targetFrame = playbackSearchFrame(frame, offset, playbackEndFrame);
            if (targetFrame === null) continue;
            const targetSnapshot = buildFrameSnapshot(targetFrame);
            for (const layer of currentSafetyLayersAtFrame(targetSnapshot, targetFrame, offset)) {
                const active = offset === 0 ? state.activePlaybackVideos.get(layer.key) : null;
                if (offset === 0 && isActiveVideoDrawable(active, layer, targetFrame)) continue;
                const candidates = prebufferCandidatesForLayerFrame(layer, targetFrame, { snapshot: targetSnapshot });
                if (candidates.some((candidate) => candidate.readyClaimable)) continue;
                return false;
            }
        }
        return true;
    }

    const playbackDecodeLimiter = _createDecodeConcurrencyLimiter({
        getMaxConcurrent: normalizedDecodeConcurrency,
        reserveHighSlotForLow: true,
        allowSingleSlotLow: singleSlotLowAllowed,
    });
    const guideDecodeLimiter = _createDecodeConcurrencyLimiter({
        getMaxConcurrent: () => 1,
        reserveHighSlotForLow: false,
    });

    function discardPrebufferEntry(entry) {
        if (!entry) return;
        if (entry.claimedByActive) return;
        removePlaybackWarmEntriesByOwner("prebuffer", entry.key, "prebuffer-discarded");
        entry.cancelled = true;
        try {
            entry.abortController?.abort?.();
        } catch (error) {}
        removeMediaSource(entry.video);
    }

    function cancelQueuedPrebufferEntry(entry, reason) {
        if (entry?.decodeJobState !== "queued" || !entry?.decodeJob) return false;
        return playbackDecodeLimiter.cancelQueued(entry.decodeJob, reason);
    }

    function prebufferEntryMatchesTargetSource(entry, target) {
        return !!(
            entry
            && target
            && entry.sourcePath === target.sourcePath
            && entry.targetSourceFrame === target.targetSourceFrame
            && entry.sourceTargetKey === target.sourceTargetKey
            && entry.warmToken === state.playbackWarmContentToken
        );
    }

    function prebufferEntryHasRunningWork(entry) {
        return !!(
            entry
            && (
                entry.decodeJobState === "source-pending"
                || entry.decodeJobState === "active"
                || (
                    entry.decodeJob
                    && entry.promise
                    && entry.decodeJobState !== "queued"
                    && entry.decodeJobState !== "settled"
                    && entry.decodeJobState !== "cancelled"
                )
            )
        );
    }

    function shouldRetainNonDesiredPrebufferDuringHandoff(entry, key) {
        if (!entry || entry.cancelled || entry.consumed) return false;
        if (entry.warmToken !== state.playbackWarmContentToken) return false;
        if (playbackWaitingForPrebufferEntry(entry)) return true;
        if (entry.ready) return true;
        if (entry.scheduleOrigin === "current-frame-recovery") return true;
        if (entry.scheduleOrigin === "rebuffer-next-boundary") return true;
        if ((state.playbackRebufferSafetyTargets || []).some((target) => target?.key === key || target?.key === entry.key)) {
            return true;
        }
        if (prebufferEntryHasRunningWork(entry)) return true;
            return !!(entry.decodeJobState === "queued" && entry.decodeJob);
    }

    // Release outgoing elements parked by claimPrebufferedVideo. On a normal
    // drain (post-commit) skip anything still referenced by an active video;
    // force=true (teardown) releases everything.
    function drainPendingReleases(force = false) {
        if (!state.pendingRelease.size) return;
        const inUse = new Set();
        if (!force) {
            for (const active of state.activePlaybackVideos.values()) inUse.add(active.video);
        }
        for (const el of Array.from(state.pendingRelease)) {
            if (!force && inUse.has(el)) continue;
            state.pendingRelease.delete(el);
            removeMediaSource(el);
        }
    }

    function clearPrebufferCache() {
        for (const [key, entry] of Array.from(state.prebufferCache.entries())) {
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
        }
    }

    async function ensureMediaElementSource(mediaEl, sourcePath, { forceBlob = false } = {}) {
        if (!mediaEl || !sourcePath) return null;
        const requestToken = (Number(mediaEl._sonderSourceRequestToken) || 0) + 1;
        mediaEl._sonderSourceRequestToken = requestToken;
        const cacheKey = mediaSourceCacheKey(sourcePath, { forceBlob });
        const resolvedUrl = await resolveMediaSourceUrl(sourcePath, { forceBlob });
        if (!resolvedUrl || state.destroyed || mediaEl._sonderSourceRequestToken !== requestToken) {
            maybeReleaseSourceCacheEntry(cacheKey);
            return null;
        }
        if (!acquireMediaElementSource(mediaEl, cacheKey, resolvedUrl)) return null;
        return await waitForMediaReady(mediaEl, 1);
    }

    async function captureSourceFrame(sourcePath, sourceFrame, targetLongEdge) {
        if (!sourcePath || state.destroyed) return null;
        if (typeof OffscreenCanvas !== "function") {
            return null;
        }
        const frameIndex = Math.max(0, Math.round(Number(sourceFrame) || 0));
        const captureFps = fps();
        // Stay strictly below half a frame so frame 0 at currentTime=0 cannot
        // satisfy the frame-center target without an actual seek/decode.
        const seekTolerance = 0.25 / captureFps;
        // requestVideoFrameCallback reports the frame presentation timestamp
        // (normally the frame start), so allow half a frame when validating
        // that the decoded frame belongs to our frame-center seek target.
        const decodedTolerance = 0.5 / captureFps;
        const targetTime = (frameIndex + 0.5) / captureFps;
        const cacheKey = `snapshot:${sourcePath}`;
        let video = state.videoCache[cacheKey];
        if (!video) {
            video = createMutedVideoElement();
            video.draggable = false;
            state.videoCache[cacheKey] = video;
        }
        const prepared = await guideDecodeLimiter.run(DECODE_PRIORITY_HIGH, async () => {
            const loaded = await ensureMediaElementSource(video, sourcePath, { forceBlob: true });
            if (!loaded || state.destroyed) return null;
            await waitForMediaReady(video, 2, 1500);
            const sought = await seekMedia(video, targetTime, {
                tolerance: seekTolerance,
                timeoutMs: 900,
                requireTarget: true,
                waitForFrame: true,
            });
            if (!sought || (video.readyState || 0) < 2) return null;
            const decodedAtTarget = await waitForDecodedVideoFrameAtTarget(video, targetTime, decodedTolerance, 240);
            return decodedAtTarget ? video : null;
        }, {
            shouldRun: () => !state.destroyed,
        });
        if (!prepared) return null;

        const sourceWidth = Math.round(Number(video.videoWidth) || 0);
        const sourceHeight = Math.round(Number(video.videoHeight) || 0);
        if (sourceWidth <= 0 || sourceHeight <= 0) return null;
        const sourceLongEdge = Math.max(sourceWidth, sourceHeight);
        const requestedLong = Math.max(1, Math.round(Number(targetLongEdge) || sourceLongEdge));
        const scale = requestedLong / sourceLongEdge;
        const width = Math.max(1, Math.round(sourceWidth * scale));
        const height = Math.max(1, Math.round(sourceHeight * scale));
        const canvas = new OffscreenCanvas(width, height);
        const ctx = canvas.getContext("2d");
        if (!ctx) return null;
        ctx.drawImage(video, 0, 0, width, height);
        const blob = await canvas.convertToBlob({ type: "image/png" });
        if (!blob) return null;
        return {
            blob,
            width,
            height,
            sourceWidth,
            sourceHeight,
            sourceLongEdge,
            targetLongEdge: requestedLong,
            mediaTime: Number(video.currentTime) || targetTime,
        };
    }

    async function loadPrebufferEntry(entry) {
        if (!entry?.video || !entry.sourcePath) return null;
        const sourcePath = entry.sourcePath;
        const layer = entry.layer;
        const targetFrame = entry.targetFrame;
        const targetSourceFrame = entry.targetSourceFrame;
        const sourceTargetKey = entry.sourceTargetKey;
        const warmToken = entry.warmToken;
        const signal = entry.abortController?.signal || null;
        const sourceCacheKey = mediaSourceCacheKey(sourcePath);
        const stillCurrent = () => (
            !state.destroyed
            && !entry.cancelled
            && entry.sourcePath === sourcePath
            && entry.targetFrame === targetFrame
            && entry.targetSourceFrame === targetSourceFrame
            && entry.sourceTargetKey === sourceTargetKey
            && entry.warmToken === warmToken
            && entry.warmToken === state.playbackWarmContentToken
            && state.prebufferCache.get(entry.key) === entry
        );
        const stillRelevant = () => stillCurrent() && prebufferEntryDecodeStillRelevant(entry);
        const resolvedUrl = await resolveMediaSourceUrl(sourcePath);
        if (!resolvedUrl || !stillCurrent()) {
            maybeReleaseSourceCacheEntry(sourceCacheKey);
            return null;
        }
        const video = entry.video;
        entry.decodeJob = null;
        entry.decodeJobState = "queued";
        entry.decodeJobPriority = entry.decodePriority || DECODE_PRIORITY_LOW;
        if (!acquireMediaElementSource(video, sourceCacheKey, resolvedUrl)) {
            maybeReleaseSourceCacheEntry(sourceCacheKey);
            return null;
        }
        const prepared = await playbackDecodeLimiter.run(entry.decodePriority || DECODE_PRIORITY_LOW, async () => {
            if (!stillRelevant()) return null;
            await waitForMediaReady(video, 2, 1500, { signal });
            if (!stillRelevant()) return null;
            const targetTime = clampMediaTargetTime(video, sourceFrameTime(targetSourceFrame));
            entry.targetTime = targetTime;
            const sought = await seekMedia(video, targetTime, {
                tolerance: prebufferTargetTimeTolerance(),
                timeoutMs: 700,
                requireTarget: true,
                waitForFrame: false,
                signal,
            });
            if (!sought || !stillRelevant()) return null;
            if ((video.readyState || 0) >= 2 && !video.seeking && isMediaAtTarget(video, targetTime, prebufferTargetTimeTolerance())) {
                publishPrebufferEntryReady(entry, layer, targetFrame, "seek-complete");
            }
            await waitForMediaReady(video, 2, 500, { signal });
            return stillRelevant() ? video : null;
        }, {
            front: entry.scheduleOrigin === "current-frame-recovery",
            deadline: prebufferDeadlineForEntry(entry),
            shouldRun: stillRelevant,
            onQueued: (job) => {
                if (!stillCurrent()) return;
                entry.decodeJob = job;
                entry.decodeJobState = job?.state || "queued";
                entry.decodeJobPriority = job?.priority || entry.decodePriority || DECODE_PRIORITY_LOW;
                entry.decodeJobDeadlineClass = job?.deadline?.deadlineClass || entry.deadlineClass || "";
                entry.decodeJobQueueClass = job?.queueClass || job?.priority || "";
                entry.decodeJobQueuePosition = Number.isFinite(Number(job?.queuePosition)) ? Number(job.queuePosition) : null;
                entry.decodeJobQueueDepth = Number.isFinite(Number(job?.queueDepth)) ? Number(job.queueDepth) : null;
            },
            onState: (job, jobState) => {
                if (entry.decodeJob && entry.decodeJob !== job) return;
                entry.decodeJob = job || entry.decodeJob;
                entry.decodeJobState = jobState || job?.state || "";
                entry.decodeJobPriority = job?.priority || entry.decodeJobPriority || entry.decodePriority || DECODE_PRIORITY_LOW;
                entry.decodeJobDeadlineClass = job?.deadline?.deadlineClass || entry.decodeJobDeadlineClass || entry.deadlineClass || "";
                entry.decodeJobQueueClass = job?.queueClass || entry.decodeJobQueueClass || job?.priority || "";
                entry.decodeJobQueuePosition = Number.isFinite(Number(job?.queuePosition)) ? Number(job.queuePosition) : null;
                entry.decodeJobQueueDepth = Number.isFinite(Number(job?.queueDepth)) ? Number(job.queueDepth) : null;
            },
        });
        if (!prepared) return null;
        if (!stillCurrent()) return null;
        const targetTime = clampMediaTargetTime(video, entry.targetTime ?? sourceFrameTime(targetSourceFrame));
        entry.ready = (video.readyState || 0) >= 2 && isMediaAtTarget(video, targetTime, prebufferTargetTimeTolerance());
        if (entry.ready) {
            publishPrebufferEntryReady(entry, layer, targetFrame, entry.readyPublishedFrom || "load-complete");
        }
        if ((entry.consumed || playbackWaitingForPrebufferEntry(entry)) && state.isPlaying) {
            renderFrame();
        }
        return entry.ready ? video : null;
    }

    function prebufferEntryMediaAtTarget(entry, targetTime) {
        if (!entry?.video || entry.video.error) return false;
        if (entry.video.seeking || (entry.video.readyState || 0) < 2) return false;
        return isMediaAtTarget(
            entry.video,
            clampMediaTargetTime(entry.video, targetTime),
            prebufferTargetTimeTolerance(),
        );
    }

    function publishPrebufferEntryReady(entry, layer, frame, source = "media-state") {
        if (!entry || entry.cancelled || entry.consumed || entry.claimedByActive) return false;
        const firstReadyPublication = !entry.ready || !entry.readyPublishedAtMs;
        entry.ready = true;
        entry.readyPublishedFrom = entry.readyPublishedFrom || source || "media-state";
        entry.readyPublishedAtMs = entry.readyPublishedAtMs || performance.now();
        if (firstReadyPublication) {
            notePlaybackWarmLayer(layer || entry.layer, frame ?? entry.targetFrame, "warm", "prebuffer-ready", {
                owner: "prebuffer",
                ownerKey: entry.key,
                token: entry.warmToken,
            });
        }
        return true;
    }

    function prebufferEntryReadyAtTarget(entry, layer, frame, target = null) {
        const resolvedTarget = target || prebufferTargetForLayer(layer, frame);
        if (!resolvedTarget || !entry?.video) return false;
        if (entry.cancelled || entry.consumed || entry.claimedByActive) return false;
        if (entry.warmToken !== state.playbackWarmContentToken) return false;
        if (entry.sourcePath !== resolvedTarget.sourcePath) return false;
        if (entry.targetSourceFrame !== resolvedTarget.targetSourceFrame) return false;
        if (entry.sourceTargetKey !== resolvedTarget.sourceTargetKey) return false;
        if (!prebufferEntryMediaAtTarget(entry, resolvedTarget.targetTime)) return false;
        if (!entry.ready) {
            publishPrebufferEntryReady(entry, layer, frame, "media-state");
        }
        return true;
    }

    function findVisibleLayerByKey(snapshot, key) {
        if (!snapshot || !key) return null;
        return (snapshot.playableClipLayers || []).find((layer) => layer.key === key) || null;
    }

    function prebufferEntryOwnerAvailableForClaim(entry, layer, frame, snapshot) {
        if (!entry || entry.layerKey === layer?.key) return true;
        const ownerLayer = findVisibleLayerByKey(snapshot, entry.layerKey);
        if (!ownerLayer) return true;
        const ownerTarget = prebufferTargetForLayer(ownerLayer, frame);
        if (!ownerTarget || ownerTarget.sourceTargetKey !== entry.sourceTargetKey) return true;
        const ownerActive = state.activePlaybackVideos.get(ownerLayer.key);
        if (isActiveVideoDrawable(ownerActive, ownerLayer, frame)) return true;
        const ownerExactKey = prebufferKeyForLayer(ownerLayer, frame);
        const ownerExactEntry = ownerExactKey ? state.prebufferCache.get(ownerExactKey) : null;
        return !!(
            ownerExactEntry
            && ownerExactEntry !== entry
            && prebufferEntryReadyAtTarget(ownerExactEntry, ownerLayer, frame)
        );
    }

    function pendingPrebufferSkipKey(layer, frame, entry) {
        if (!layer || !entry) return "";
        return [
            state.playbackSessionId,
            layer.key || "",
            Math.round(Number(frame) || 0),
            entry.sourceTargetKey || "",
            entry.key || "",
        ].join("|");
    }

    function markPendingPrebufferSkipped(layer, frame, entry, reason = "") {
        const key = pendingPrebufferSkipKey(layer, frame, entry);
        if (!key) return;
        state.expiredPendingPrebufferSkips.set(key, performance.now());
        while (state.expiredPendingPrebufferSkips.size > 512) {
            const oldestKey = state.expiredPendingPrebufferSkips.keys().next().value;
            if (oldestKey === undefined) break;
            state.expiredPendingPrebufferSkips.delete(oldestKey);
        }
        recordPlaybackTelemetry("playback_prebuffer_pending_expired", {
            requestedLayerKey: layer.key || "",
            sourcePath: layer.clip?.source_path || entry.sourcePath || "",
            frame,
            targetFrame: entry.targetFrame ?? null,
            targetSourceFrame: entry.targetSourceFrame ?? null,
            sourceTargetKey: entry.sourceTargetKey || "",
            candidateKey: entry.key || "",
            entryLayerKey: entry.layerKey || "",
            reason,
            readyState: entry.video?.readyState || 0,
            seeking: !!entry.video?.seeking,
            playbackSessionId: state.playbackSessionId,
        });
    }

    function pendingPrebufferSkippedFor(layer, frame, entry) {
        const key = pendingPrebufferSkipKey(layer, frame, entry);
        return !!(key && state.expiredPendingPrebufferSkips.has(key));
    }

    function prebufferCandidatesForLayerFrame(layer, frame, { snapshot = null } = {}) {
        const target = prebufferTargetForLayer(layer, frame);
        if (!target) return [];
        const candidates = [];
        for (const [key, entry] of state.prebufferCache.entries()) {
            if (!entry || entry.sourcePath !== target.sourcePath) continue;
            if (entry.targetSourceFrame !== target.targetSourceFrame) continue;
            if (entry.sourceTargetKey !== target.sourceTargetKey) continue;
            const exactLayerMatch = key === target.key || entry.layerKey === layer.key;
            const tokenMatches = entry.warmToken === state.playbackWarmContentToken;
            const reusable = !!entry.video && !entry.cancelled && !entry.consumed && !entry.claimedByActive;
            const pendingSkipped = pendingPrebufferSkippedFor(layer, frame, entry);
            const ownerAvailable = reusable
                && tokenMatches
                && prebufferEntryOwnerAvailableForClaim(entry, layer, frame, snapshot);
            const ready = ownerAvailable && prebufferEntryReadyAtTarget(entry, layer, frame, target);
            const pending = reusable && tokenMatches && !entry.ready && !pendingSkipped;
            const readyClaimable = ready && ownerAvailable;
            const pendingClaimable = pending && ownerAvailable;
            const pendingExact = pendingClaimable && exactLayerMatch;
            const pendingSource = pendingClaimable && !exactLayerMatch;
            candidates.push({
                key,
                entry,
                exactLayerMatch,
                tokenMatches,
                reusable,
                ownerAvailable,
                ready,
                pending,
                pendingSkipped,
                readyClaimable,
                pendingClaimable,
                pendingExact,
                pendingSource,
            });
        }
        candidates.sort((a, b) => (
            Number(b.exactLayerMatch) - Number(a.exactLayerMatch)
            || Number(b.readyClaimable) - Number(a.readyClaimable)
            || Number(b.pendingClaimable) - Number(a.pendingClaimable)
            || Number(b.ownerAvailable) - Number(a.ownerAvailable)
            || Number(b.tokenMatches) - Number(a.tokenMatches)
        ));
        return candidates;
    }

    function pendingPrebufferCandidateFromCandidates(candidates) {
        if (!Array.isArray(candidates) || !candidates.length) return null;
        if (candidates.some((candidate) => candidate.readyClaimable)) return null;
        return candidates.find((candidate) => candidate.pendingExact)
            || candidates.find((candidate) => candidate.pendingSource)
            || null;
    }

    function pendingPrebufferForLayerFrame(layer, frame, snapshot = null) {
        const candidates = prebufferCandidatesForLayerFrame(layer, frame, { snapshot });
        return pendingPrebufferCandidateFromCandidates(candidates);
    }

    function prebufferCandidateTelemetry(candidate, target = null) {
        const entry = candidate?.entry || null;
        const targetTime = Number(target?.targetTime ?? entry?.targetTime);
        const currentTime = Number(entry?.video?.currentTime);
        const atTarget = Number.isFinite(targetTime)
            ? prebufferEntryMediaAtTarget(entry, targetTime)
            : false;
        return {
            key: candidate?.key || entry?.key || "",
            entryLayerKey: entry?.layerKey || "",
            exactLayerMatch: !!candidate?.exactLayerMatch,
            tokenMatches: !!candidate?.tokenMatches,
            reusable: !!candidate?.reusable,
            ownerAvailable: !!candidate?.ownerAvailable,
            ready: !!candidate?.ready,
            entryReady: !!entry?.ready,
            pending: !!candidate?.pending,
            pendingSkipped: !!candidate?.pendingSkipped,
            readyClaimable: !!candidate?.readyClaimable,
            pendingClaimable: !!candidate?.pendingClaimable,
            readyState: entry?.video?.readyState || 0,
            seeking: !!entry?.video?.seeking,
            mediaError: !!entry?.video?.error,
            currentTime: Number.isFinite(currentTime) ? roundTelemetryMs(currentTime) : null,
            targetTime: Number.isFinite(targetTime) ? roundTelemetryMs(targetTime) : null,
            atTarget,
            decodeJobState: entry?.decodeJobState || "",
            decodeJobPriority: entry?.decodeJobPriority || entry?.decodePriority || "",
            decodeJobDeadlineClass: entry?.decodeJobDeadlineClass || entry?.deadlineClass || "",
            decodeJobQueueClass: entry?.decodeJobQueueClass || "",
            decodeJobQueuePosition: entry?.decodeJobQueuePosition ?? null,
            decodeJobQueueDepth: entry?.decodeJobQueueDepth ?? null,
            scheduleOrigin: entry?.scheduleOrigin || "",
            intent: entry?.intent || "",
            entryTargetFrame: entry?.targetFrame ?? null,
            effectiveTargetFrame: entry ? prebufferEntryEffectiveTargetFrame(entry) : null,
            currentFrameRecoveryTargetFrame: entry?.currentFrameRecoveryTargetFrame ?? null,
            ...prebufferDeadlineTelemetryFromEntry(entry),
            originalScheduleOrigin: entry?.currentFrameReclassifiedFromScheduleOrigin || "",
            originalDeadlineClass: entry?.currentFrameReclassifiedFromDeadlineClass || "",
            originalDecodeJobDeadlineClass: entry?.currentFrameReclassifiedFromDecodeJobDeadlineClass || "",
            currentFrameReclassificationSource: entry?.currentFrameReclassificationSource || "",
            readyPublishedFrom: entry?.readyPublishedFrom || "",
            pendingAgeMs: Number.isFinite(Number(entry?.scheduledAtMs))
                ? roundTelemetryMs(performance.now() - Number(entry.scheduledAtMs))
                : null,
        };
    }

    function candidateWouldBeCurrentFrameRecovery(candidate, layer, frame, target = null) {
        if (!candidate?.pendingClaimable || candidate.readyClaimable) return false;
        const resolvedTarget = target || prebufferTargetForLayer(layer, frame);
        if (!resolvedTarget) return false;
        const entry = candidate.entry;
        return !!(
            entry
            && entry.sourcePath === resolvedTarget.sourcePath
            && entry.targetSourceFrame === resolvedTarget.targetSourceFrame
            && entry.sourceTargetKey === resolvedTarget.sourceTargetKey
        );
    }

    function reclassifyPendingCandidateForCurrentFrame(candidate, layer, frame, source = "pending-hold") {
        const baseTarget = prebufferTargetForLayer(layer, frame, {
            intent: "current-frame-recovery",
            decodePriority: DECODE_PRIORITY_URGENT,
        });
        if (!candidateWouldBeCurrentFrameRecovery(candidate, layer, frame, baseTarget)) {
            return { reclassified: false };
        }
        const playbackEndFrame = state.playbackLoopRange
            ? state.playbackLoopRange.end
            : totalFrames();
        const target = applyPrebufferDeadlineMetadata(
            withScheduleOrigin(baseTarget, "current-frame-recovery"),
            frame,
            playbackEndFrame,
        );
        const entry = candidate.entry;
        const original = {
            scheduleOrigin: entry.scheduleOrigin || "",
            deadlineClass: entry.deadlineClass || "",
            decodeJobDeadlineClass: entry.decodeJobDeadlineClass || "",
            decodeJobState: entry.decodeJobState || "",
            decodePriority: entry.decodePriority || "",
        };
        const alreadyCurrent = !!(
            entry.layerKey === layer.key
            && entry.intent === "current-frame-recovery"
            && entry.decodePriority === DECODE_PRIORITY_URGENT
            && entry.scheduleOrigin === "current-frame-recovery"
            && entry.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME
            && entry.deadlineFrame === target.deadlineFrame
        );
        const queuedJobAlreadyCurrent = !!(
            entry.decodeJobState === "queued"
            && entry.decodeJob
            && entry.decodeJob.priority === DECODE_PRIORITY_URGENT
            && entry.decodeJob.deadline?.deadlineClass === DECODE_DEADLINE_CURRENT_FRAME
            && (entry.decodeJobQueuePosition === 0 || entry.decodeJob.queuePosition === 0)
        );
        if (alreadyCurrent && (entry.decodeJobState !== "queued" || queuedJobAlreadyCurrent)) {
            return {
                reclassified: false,
                alreadyCurrent: true,
                promoteResult: "",
                jobDeadlineMutated: false,
                originalScheduleOrigin: original.scheduleOrigin,
                originalDeadlineClass: original.deadlineClass,
                originalDecodeJobDeadlineClass: original.decodeJobDeadlineClass,
                originalDecodePriority: original.decodePriority,
                source,
            };
        }
        const updateEntryMetadata = () => {
            entry.layer = layer;
            entry.layerKey = layer.key;
            entry.intent = "current-frame-recovery";
            entry.decodePriority = DECODE_PRIORITY_URGENT;
            entry.scheduleOrigin = "current-frame-recovery";
            entry.deadlineClass = DECODE_DEADLINE_CURRENT_FRAME;
            entry.deadlineFrame = target.deadlineFrame ?? target.targetFrame;
            entry.deadlineDistanceFrames = target.deadlineDistanceFrames ?? 0;
            entry.currentFrameRecoveryTargetFrame = target.targetFrame;
            entry.currentFrameRecoveryTargetSourceFrame = target.targetSourceFrame;
            entry.currentFrameRecoverySourceTargetKey = target.sourceTargetKey;
            entry.currentFrameRecoveryTargetTime = target.targetTime;
            entry.currentFrameReclassifiedAtMs = performance.now();
            entry.currentFrameReclassifiedFromScheduleOrigin = original.scheduleOrigin;
            entry.currentFrameReclassifiedFromDeadlineClass = original.deadlineClass;
            entry.currentFrameReclassifiedFromDecodeJobDeadlineClass = original.decodeJobDeadlineClass;
            entry.currentFrameReclassificationSource = source;
        };
        let promoteResult = "";
        let jobDeadlineMutated = false;
        if (entry.decodeJobState === "queued" && entry.decodeJob) {
            promoteResult = playbackDecodeLimiter.promote(entry.decodeJob, DECODE_PRIORITY_URGENT, {
                front: true,
                deadline: prebufferDeadlineForTarget(target),
            });
            if (
                promoteResult === "promoted"
                || promoteResult === "moved-front"
                || promoteResult === "reprioritized-queued"
                || promoteResult === "already-front"
                || promoteResult === "not-needed"
            ) {
                jobDeadlineMutated = true;
                state.playbackCurrentFrameRecoveryQueuedPromotedCount += 1;
                const outcome = promoteResult === "moved-front"
                    ? "moved-front"
                    : (promoteResult === "reprioritized-queued"
                        ? "reprioritized-queued"
                        : (promoteResult === "already-front" ? "already-front" : "promoted-queued"));
                recordPrebufferPriorityUpgrade(entry, target, layer, outcome, {
                    promoteResult,
                    reclassificationSource: source,
                    originalScheduleOrigin: original.scheduleOrigin,
                    originalDeadlineClass: original.deadlineClass,
                    originalDecodeJobDeadlineClass: original.decodeJobDeadlineClass,
                    jobDeadlineMutated,
                });
            }
        } else if (
            entry.decodeJobState === "active"
            || entry.decodeJobState === "source-pending"
            || prebufferEntryHasRunningWork(entry)
        ) {
            state.playbackCurrentFrameRecoveryActiveReclassifiedCount += alreadyCurrent ? 0 : 1;
        }
        updateEntryMetadata();
        if (!alreadyCurrent) {
            state.playbackCurrentFrameRecoveryReclassifiedCount += 1;
        }
        recordPlaybackTelemetry("playback_prebuffer_current_reclassify", {
            key: candidate.key || entry.key || "",
            requestedLayerKey: layer.key,
            entryLayerKey: entry.layerKey || "",
            sourcePath: target.sourcePath,
            frame,
            targetFrame: target.targetFrame,
            targetSourceFrame: target.targetSourceFrame,
            sourceTargetKey: target.sourceTargetKey,
            originalScheduleOrigin: original.scheduleOrigin,
            originalDeadlineClass: original.deadlineClass,
            originalDecodeJobDeadlineClass: original.decodeJobDeadlineClass,
            originalDecodePriority: original.decodePriority,
            decodeJobState: entry.decodeJobState || "",
            promoteResult,
            jobDeadlineMutated,
            alreadyCurrent,
            source,
            ...prebufferDeadlineTelemetryFromEntry(entry),
            playbackSessionId: state.playbackSessionId,
        });
        return {
            reclassified: !alreadyCurrent || !!promoteResult,
            alreadyCurrent,
            promoteResult,
            jobDeadlineMutated,
            originalScheduleOrigin: original.scheduleOrigin,
            originalDeadlineClass: original.deadlineClass,
            originalDecodeJobDeadlineClass: original.decodeJobDeadlineClass,
            originalDecodePriority: original.decodePriority,
            source,
        };
    }

    function pendingPrebufferBlockReason(entry, targetTime = null) {
        if (!entry) return "missing-entry";
        if (entry.cancelled) return "cancelled";
        if (entry.decodeJobState === "queued") return "decode-queued";
        if (entry.decodeJobState === "active" || entry.decodeJobState === "source-pending") return "decode-active";
        if (entry.video?.seeking) return "seek";
        if ((entry.video?.readyState || 0) < 2) return "media-readiness";
        if (Number.isFinite(Number(targetTime)) && !prebufferEntryMediaAtTarget(entry, Number(targetTime))) return "media-target";
        if (!entry.ready) return "ready-publication";
        return "unknown";
    }

    function prebufferMissReason(candidates) {
        if (!candidates.length) return "not-scheduled";
        if (candidates.some((candidate) => candidate.pendingExact)) return "exact-pending";
        if (candidates.some((candidate) => candidate.pendingSource)) return "source-pending";
        if (candidates.some((candidate) => candidate.pendingSkipped)) return "pending-expired";
        if (candidates.some((candidate) => candidate.tokenMatches && !candidate.ownerAvailable)) {
            return "source-match-owned-by-visible-layer";
        }
        if (candidates.some((candidate) => !candidate.tokenMatches)) return "stale-token";
        if (candidates.some((candidate) => candidate.reusable)) return "not-ready";
        return "not-usable";
    }

    function recordPrebufferMissTelemetry(layer, frame, candidates) {
        if (!playbackTelemetryActive() || !isPrebufferEnabled()) return;
        const target = prebufferTargetForLayer(layer, frame);
        if (!target) return;
        const reason = prebufferMissReason(candidates);
        const telemetryKey = [
            state.playbackSessionId,
            layer.key,
            target.targetFrame,
            target.sourceTargetKey,
            reason,
        ].join("|");
        const now = performance.now();
        const previous = state.prebufferMissTelemetryKeys.get(telemetryKey);
        if (previous !== undefined && now - previous < PLAYBACK_PREBUFFER_MISS_THROTTLE_MS) {
            state.prebufferMissTelemetrySuppressed += 1;
            return;
        }
        state.prebufferMissTelemetryKeys.set(telemetryKey, now);
        while (state.prebufferMissTelemetryKeys.size > 2048) {
            const oldestKey = state.prebufferMissTelemetryKeys.keys().next().value;
            if (oldestKey === undefined) break;
            state.prebufferMissTelemetryKeys.delete(oldestKey);
        }
        state.prebufferMissTelemetryEmitted += 1;
        recordPlaybackTelemetry("playback_prebuffer_miss", {
            requestedLayerKey: layer.key,
            sourcePath: target.sourcePath,
            frame,
            targetFrame: target.targetFrame,
            targetSourceFrame: target.targetSourceFrame,
            sourceTargetKey: target.sourceTargetKey,
            intent: candidates[0]?.entry?.intent || target.intent || "",
            decodePriority: candidates[0]?.entry?.decodePriority || target.decodePriority || "",
            ...prebufferDeadlineTelemetryFromTarget(target),
            candidateCount: candidates.length,
            readyCandidateCount: candidates.filter((candidate) => candidate.readyClaimable).length,
            pendingClaimableCount: candidates.filter((candidate) => candidate.pendingClaimable).length,
            reason,
            nearestCandidate: candidates[0] ? prebufferCandidateTelemetry(candidates[0], target) : null,
            playbackSessionId: state.playbackSessionId,
        });
    }

    function notePrebufferPriorityUpgradeOutcome(outcome) {
        if (outcome === "preserved-running") {
            state.prebufferPriorityUpgradeOutcomes.preservedRunning += 1;
        } else if (outcome === "promoted-queued") {
            state.prebufferPriorityUpgradeOutcomes.promotedQueued += 1;
        } else if (outcome === "moved-front") {
            state.prebufferPriorityUpgradeOutcomes.movedFront += 1;
        } else if (outcome === "reprioritized-queued") {
            state.prebufferPriorityUpgradeOutcomes.reprioritizedQueued += 1;
        } else if (outcome === "recreated-queued") {
            state.prebufferPriorityUpgradeOutcomes.recreatedQueued += 1;
        } else if (outcome === "already-front") {
            state.prebufferPriorityUpgradeOutcomes.alreadyFront += 1;
        }
    }

    function recordPrebufferPriorityUpgrade(existing, target, layer, outcome, extra = {}) {
        notePrebufferPriorityUpgradeOutcome(outcome);
        if (outcome === "moved-front" && target?.scheduleOrigin === "current-frame-recovery") {
            state.playbackCurrentFrameRecoveryMovedFrontCount += 1;
        }
        recordPlaybackTelemetry("playback_prebuffer_priority_upgrade", {
            key: target.key,
            entryKey: existing?.key || "",
            layerKey: layer.key,
            sourcePath: target.sourcePath,
            targetFrame: target.targetFrame,
            targetSourceFrame: target.targetSourceFrame,
            sourceTargetKey: target.sourceTargetKey,
            fromPriority: existing?.decodePriority || DECODE_PRIORITY_LOW,
            toPriority: target.decodePriority || DECODE_PRIORITY_LOW,
            fromIntent: existing?.intent || "",
            toIntent: target.intent || "",
            fromScheduleOrigin: existing?.scheduleOrigin || "",
            toScheduleOrigin: target.scheduleOrigin || "",
            outcome,
            decodeJobState: existing?.decodeJobState || "",
            decodeJobPriority: existing?.decodeJobPriority || "",
            fromDeadlineClass: existing?.deadlineClass || "",
            toDeadlineClass: target.deadlineClass || "",
            toDeadlineFrame: target.deadlineFrame ?? null,
            toDeadlineDistanceFrames: target.deadlineDistanceFrames ?? null,
            playbackSessionId: state.playbackSessionId,
            ...extra,
        });
    }

    function ensurePrebufferedLayer(layer, targetFrame, options = {}) {
        const target = options?.key ? options : prebufferTargetForLayer(layer, targetFrame, options);
        if (!target) return;
        const active = state.activePlaybackVideos.get(layer.key);
        if (playbackVideoAtFrame(active, layer, targetFrame, prebufferTargetTimeTolerance())) return;
        const key = target.key;
        let existing = state.prebufferCache.get(key);
        let existingCacheKey = key;
        if (!existing && target.currentFrameRecoveryPendingExistingKey) {
            const sourceExisting = state.prebufferCache.get(target.currentFrameRecoveryPendingExistingKey);
            if (prebufferEntryMatchesTargetSource(sourceExisting, target)) {
                existing = sourceExisting;
                existingCacheKey = target.currentFrameRecoveryPendingExistingKey;
            }
        }
        if (!existing && target.scheduleOrigin === "current-frame-recovery") {
            const sourceCandidate = prebufferCandidatesForLayerFrame(layer, targetFrame, { snapshot: target.snapshot || null })
                .find((candidate) => candidate.pendingSource && prebufferEntryMatchesTargetSource(candidate.entry, target));
            if (sourceCandidate?.entry) {
                existing = sourceCandidate.entry;
                existingCacheKey = sourceCandidate.key || existing.key;
            }
        }
        if (existing) {
            if (
                prebufferEntryMatchesTargetSource(existing, target)
            ) {
                const existingPriority = existing.decodePriority || DECODE_PRIORITY_LOW;
                const requestedPriority = target.decodePriority || DECODE_PRIORITY_LOW;
                const queueFront = target.scheduleOrigin === "current-frame-recovery";
                const metadataAlreadyCurrent = !!(
                    existing.layerKey === layer.key
                    && existing.intent === (target.intent || existing.intent || "warm-ahead")
                    && existing.decodePriority === requestedPriority
                    && existing.scheduleOrigin === (target.scheduleOrigin || existing.scheduleOrigin || "")
                    && existing.deadlineClass === (target.deadlineClass || existing.deadlineClass || DECODE_DEADLINE_NONE)
                    && existing.deadlineFrame === (target.deadlineFrame ?? existing.deadlineFrame ?? existing.targetFrame)
                    && existing.deadlineDistanceFrames === (target.deadlineDistanceFrames ?? existing.deadlineDistanceFrames ?? null)
                );
                if (
                    !existing.ready
                    && (
                        decodePriorityRank(requestedPriority) > decodePriorityRank(existingPriority)
                        || queueFront
                    )
                ) {
                    const updateExistingMetadata = () => {
                        existing.layer = layer;
                        existing.layerKey = layer.key;
                        existing.intent = target.intent || existing.intent || "warm-ahead";
                        existing.decodePriority = requestedPriority;
                        existing.scheduleOrigin = target.scheduleOrigin || existing.scheduleOrigin || "";
                        existing.deadlineClass = target.deadlineClass || existing.deadlineClass || DECODE_DEADLINE_NONE;
                        existing.deadlineFrame = target.deadlineFrame ?? existing.deadlineFrame ?? existing.targetFrame;
                        existing.deadlineDistanceFrames = target.deadlineDistanceFrames ?? existing.deadlineDistanceFrames ?? null;
                    };
                    if (existing.decodeJobState === "queued" && existing.decodeJob) {
                        const promoteResult = playbackDecodeLimiter.promote(existing.decodeJob, requestedPriority, {
                            front: queueFront,
                            deadline: prebufferDeadlineForTarget(target),
                        });
                        if (
                            promoteResult === "promoted"
                            || promoteResult === "not-needed"
                            || promoteResult === "moved-front"
                            || promoteResult === "reprioritized-queued"
                        ) {
                            const outcome = promoteResult === "moved-front"
                                ? "moved-front"
                                : (promoteResult === "reprioritized-queued" ? "reprioritized-queued" : "promoted-queued");
                            recordPrebufferPriorityUpgrade(existing, target, layer, outcome, { promoteResult });
                            updateExistingMetadata();
                            return;
                        }
                        if (promoteResult === "already-front") {
                            recordPrebufferPriorityUpgrade(existing, target, layer, "already-front", { promoteResult });
                            updateExistingMetadata();
                            return;
                        }
                        recordPrebufferPriorityUpgrade(existing, target, layer, "recreated-queued", { promoteResult });
                        state.prebufferCache.delete(existingCacheKey);
                        discardPrebufferEntry(existing);
                    } else if (
                        existing.decodeJobState === "source-pending"
                        || existing.decodeJobState === "active"
                        || (
                            existing.decodeJob
                            && existing.decodeJobState !== "queued"
                            && existing.promise
                            && existing.decodeJobState !== "settled"
                            && existing.decodeJobState !== "cancelled"
                        )
                    ) {
                        if (!metadataAlreadyCurrent) {
                            recordPrebufferPriorityUpgrade(existing, target, layer, "preserved-running");
                        }
                        updateExistingMetadata();
                        return;
                    } else {
                        recordPrebufferPriorityUpgrade(existing, target, layer, "recreated-queued", { promoteResult: "not-running" });
                        state.prebufferCache.delete(existingCacheKey);
                        discardPrebufferEntry(existing);
                    }
                } else {
                    existing.layer = layer;
                    if (decodePriorityRank(requestedPriority) >= decodePriorityRank(existingPriority)) {
                        existing.intent = target.intent || existing.intent || "warm-ahead";
                        existing.decodePriority = requestedPriority;
                        existing.scheduleOrigin = target.scheduleOrigin || existing.scheduleOrigin || "";
                        existing.deadlineClass = target.deadlineClass || existing.deadlineClass || DECODE_DEADLINE_NONE;
                        existing.deadlineFrame = target.deadlineFrame ?? existing.deadlineFrame ?? existing.targetFrame;
                        existing.deadlineDistanceFrames = target.deadlineDistanceFrames ?? existing.deadlineDistanceFrames ?? null;
                    }
                    return;
                }
            } else {
                state.prebufferCache.delete(existingCacheKey);
                discardPrebufferEntry(existing);
            }
        }
        const warmToken = state.playbackWarmContentToken;
        const entry = {
            key,
            layer,
            layerKey: layer.key,
            sourcePath: target.sourcePath,
            targetFrame: target.targetFrame,
            targetSourceFrame: target.targetSourceFrame,
            sourceTargetKey: target.sourceTargetKey,
            targetTime: target.targetTime,
            intent: target.intent || "warm-ahead",
            decodePriority: target.decodePriority || DECODE_PRIORITY_LOW,
            scheduleOrigin: target.scheduleOrigin || "",
            deadlineClass: target.deadlineClass || DECODE_DEADLINE_NONE,
            deadlineFrame: target.deadlineFrame ?? target.targetFrame,
            deadlineDistanceFrames: target.deadlineDistanceFrames ?? null,
            scheduledAtMs: performance.now(),
            video: createMutedVideoElement(),
            ready: false,
            readyPublishedFrom: "",
            readyPublishedAtMs: null,
            cancelled: false,
            consumed: false,
            claimedByActive: false,
            abortController: typeof AbortController === "function" ? new AbortController() : null,
            warmToken,
            decodeJob: null,
            decodeJobState: "source-pending",
            decodeJobPriority: target.decodePriority || DECODE_PRIORITY_LOW,
            promise: null,
        };
        playbackDebugEvent("prebuffer-warm-scheduled", {
            key,
            layerKey: layer.key,
            sourcePath: target.sourcePath,
            targetFrame: target.targetFrame,
            targetSourceFrame: target.targetSourceFrame,
            sourceTargetKey: target.sourceTargetKey,
            intent: entry.intent,
            decodePriority: entry.decodePriority,
            scheduleOrigin: entry.scheduleOrigin,
            deadlineClass: entry.deadlineClass,
            deadlineFrame: entry.deadlineFrame,
            deadlineDistanceFrames: entry.deadlineDistanceFrames,
        });
        notePlaybackWarmLayer(layer, target.targetFrame, "warming", "prebuffer-scheduled", {
            owner: "prebuffer",
            ownerKey: key,
            token: warmToken,
        });
        entry.promise = loadPrebufferEntry(entry)
            .catch(() => null)
            .then((element) => {
                if (!element && !entry.cancelled && state.prebufferCache.get(key) === entry) {
                    state.prebufferCache.delete(key);
                    discardPrebufferEntry(entry);
                }
                return element;
            });
        state.prebufferCache.set(key, entry);
    }

    function claimPrebufferedVideo(layer, frame, snapshot = null, active = null, { recordMiss = true } = {}) {
        const candidates = prebufferCandidatesForLayerFrame(layer, frame, { snapshot });
        const match = candidates.find((candidate) => candidate.readyClaimable);
        if (!match?.entry?.video) {
            if (recordMiss) recordPrebufferMissTelemetry(layer, frame, candidates);
            return null;
        }
        const { entry, key, exactLayerMatch } = match;
        const pendingAgeMs = Number.isFinite(Number(entry.scheduledAtMs))
            ? performance.now() - Number(entry.scheduledAtMs)
            : null;
        const claimReadySource = entry.readyPublishedFrom || (entry.ready ? "entry-ready" : "");
        const readyPublishedAtClaim = claimReadySource === "media-state";
        const waitedFromPending = !!(
            active
            && (
                active.pendingPrebufferKey === key
                || active.pendingPrebufferEntry === entry
                || (
                    active.pendingPrebufferSourceTargetKey
                    && active.pendingPrebufferSourceTargetKey === entry.sourceTargetKey
                )
            )
        );
        state.prebufferCache.delete(key);
        entry.claimedByActive = true;
        entry.consumed = true;
        entry.layer = layer;
        const outgoing = active?.video || state.videoCache[layer.key];
        if (active && outgoing && outgoing !== entry.video) {
            active.prepareToken = ++state.playbackPrepareToken;
            active.pendingPrepare = null;
            active.readyForDraw = false;
            active.firstDrawComplete = false;
        }
        if (outgoing && outgoing !== entry.video) {
            // Same-clip re-entry: the outgoing element may still be feeding the
            // committed canvas. Defer its teardown until after the next commit.
            try {
                outgoing.pause?.();
            } catch (error) {}
            state.pendingRelease.add(outgoing);
        }
        state.videoCache[layer.key] = entry.video;
        if (active) {
            active.layer = layer;
            active.video = entry.video;
            active.sourcePath = layer.clip.source_path;
            active.layerKey = layer.key;
            active.claimedPrebufferKey = key;
            active.claimedPrebufferEntry = entry;
            active.playbackSessionId = state.playbackSessionId;
            clearActivePendingPrebuffer(active);
        }
        playbackDebugEvent("claim-hit", {
            key,
            layerKey: layer.key,
            sourcePath: layer.clip.source_path,
            frame,
            targetFrame: entry.targetFrame,
            targetSourceFrame: entry.targetSourceFrame,
            sourceTargetKey: entry.sourceTargetKey,
            exactLayerMatch,
            waitedFromPending,
        });
        recordPlaybackTelemetry("playback_prebuffer_claim", {
            key,
            requestedLayerKey: layer.key,
            entryLayerKey: entry.layerKey || "",
            sourcePath: layer.clip.source_path,
            frame,
            targetFrame: entry.targetFrame,
            targetSourceFrame: entry.targetSourceFrame,
            sourceTargetKey: entry.sourceTargetKey,
            exactLayerMatch: !!exactLayerMatch,
            waitedFromPending,
            intent: entry.intent || "",
            decodePriority: entry.decodePriority || "",
            scheduleOrigin: entry.scheduleOrigin || "",
            readyState: entry.video?.readyState || 0,
            currentTime: roundTelemetryMs(entry.video?.currentTime || 0),
            targetTime: roundTelemetryMs(entry.targetTime ?? sourceFrameTime(entry.targetSourceFrame)),
            claimReadySource,
            readyPublishedAtClaim,
            pendingAgeMs: pendingAgeMs !== null ? roundTelemetryMs(pendingAgeMs) : null,
            originalScheduleOrigin: entry.currentFrameReclassifiedFromScheduleOrigin || "",
            originalDeadlineClass: entry.currentFrameReclassifiedFromDeadlineClass || "",
            originalDecodeJobDeadlineClass: entry.currentFrameReclassifiedFromDecodeJobDeadlineClass || "",
            currentFrameReclassificationSource: entry.currentFrameReclassificationSource || "",
            ...prebufferDeadlineTelemetryFromEntry(entry),
            playbackSessionId: state.playbackSessionId,
        });
        removePlaybackWarmEntriesByOwner("prebuffer", key, "prebuffer-claimed");
        notePlaybackWarmLayer(layer, frame, "warm", "prebuffer-claimed", {
            owner: "active",
            ownerKey: layer.key,
        });
        return { entry, video: entry.video, key, sourceTargetKey: entry.sourceTargetKey, exactLayerMatch };
    }

    function playbackSearchFrame(startFrame, offset, endFrame) {
        const loopRange = state.playbackLoopRange;
        const frame = startFrame + offset;
        if (!loopRange) {
            return frame < endFrame ? frame : null;
        }
        const loopStart = Math.max(0, Math.round(Number(loopRange.start) || 0));
        const loopEnd = Math.max(loopStart + 1, Math.round(Number(loopRange.end) || loopStart + 1));
        const loopLength = Math.max(1, loopEnd - loopStart);
        if (frame < loopEnd) return frame;
        return loopStart + ((frame - loopEnd) % loopLength);
    }

    // Candidate boundary frames to consider warming: upcoming clip starts (a
    // not-yet-in-window clip first appears) UNION currently/soon-visible clip
    // ends (an upper covering clip ending can expose a lower clip) UNION the
    // loop-wrap frame. The effective-boundary planner resolves the real
    // visibility filter stack on each candidate and its reachable predecessor,
    // so raw endpoints that do not change required visible layers create no work.
    function collectPrebufferCandidateFrames(currentFrame, endFrame, horizonFrames) {
        const scene = getScene();
        const frames = new Set();
        const loopRange = state.playbackLoopRange;
        const loopStart = loopRange ? Math.max(0, Math.round(Number(loopRange.start) || 0)) : 0;
        const loopEnd = loopRange
            ? Math.max(loopStart + 1, Math.round(Number(endFrame) || Number(loopRange.end) || loopStart + 1))
            : 0;
        const consider = (value) => {
            if (value === null || value === undefined) return;
            const f = Math.round(Number(value) || 0);
            // When looping, only frames actually played each cycle are reachable.
            // A clip whose start sits before loopStart re-enters mid-clip AT
            // loopStart (covered by the explicit loop-start candidate below), so
            // its literal start frame must not become a wrongly-targeted candidate
            // (warming it to the pre-loop frame fails the claim at the wrap).
            if (loopRange && (f < loopStart || f >= loopEnd)) return;
            const dist = playbackFrameDistance(currentFrame, f, endFrame);
            if (dist > 0 && dist <= horizonFrames) frames.add(f);
        };
        for (const clip of scene?.clips || []) {
            consider(clip?.timeline_start_frame);
            consider(clip?.timeline_end_frame);
        }
        if (loopRange) {
            consider(loopStart);
        }
        return Array.from(frames).sort(
            (a, b) => playbackFrameDistance(currentFrame, a, endFrame)
                - playbackFrameDistance(currentFrame, b, endFrame),
        );
    }

    function rawRenderableVideoLayers(snapshot) {
        return (snapshot?.playableClipLayers || []).filter(isRenderableVideoLayer);
    }

    function requiredRenderableVideoLayers(snapshot) {
        return requiredClipLayersAfterCoverage(snapshot).filter(isRenderableVideoLayer);
    }

    function effectivePlaybackBoundaryGroups(candidateFrames) {
        return _planEffectivePlaybackBoundaryGroups({
            candidateFrames,
            loopRange: state.playbackLoopRange,
            requiredLayersAtFrame: (frame) => requiredRenderableVideoLayers(buildFrameSnapshot(frame)),
        });
    }

    function effectivePlaybackBoundaryLayers(frame) {
        return effectivePlaybackBoundaryGroups([frame])[0]?.layers || [];
    }

    function currentRecoveryEligibleLayers(snapshot) {
        const frame = Math.max(0, Math.round(Number(snapshot?.frame) || 0));
        const boundaryKeys = new Set(effectivePlaybackBoundaryLayers(frame).map(playbackLayerKey));
        return requiredRenderableVideoLayers(snapshot).filter((layer) => {
            const clipStart = Math.max(0, Math.round(Number(layer?.clip?.timeline_start_frame) || 0));
            return boundaryKeys.has(playbackLayerKey(layer))
                || (frame >= clipStart && frame - clipStart <= PLAYBACK_CURRENT_BOUNDARY_HOLD_FRAMES);
        });
    }

    function currentSafetyLayersAtFrame(snapshot, targetFrame, offset) {
        return offset === 0
            ? currentRecoveryEligibleLayers(snapshot)
            : effectivePlaybackBoundaryLayers(targetFrame);
    }

    function playbackLayerKeySignature(layers) {
        return (layers || []).map((layer) => layer?.key || "").filter(Boolean).join("|");
    }

    function prebufferTargetSignature(targets) {
        return (targets || [])
            .map((target) => (
                `${target?.key || ""}@${target?.targetFrame ?? ""}:${target?.scheduleOrigin || ""}:${target?.decodePriority || ""}:${target?.deadlineClass || ""}:${target?.deadlineFrame ?? ""}`
            ))
            .filter((part) => !part.startsWith("@"))
            .join("|");
    }

    function withScheduleOrigin(target, scheduleOrigin) {
        return target ? { ...target, scheduleOrigin } : target;
    }

    function collectBoundaryPrebufferTargets(
        frame,
        currentKeys = new Set(),
        seenKeys = new Set(),
        targetOptions = {},
        boundaryLayers = null,
    ) {
        const boundaryTargets = [];
        const boundaryKeys = new Set();
        for (const layer of boundaryLayers || effectivePlaybackBoundaryLayers(frame)) {
            const target = prebufferTargetForLayer(layer, frame, targetOptions);
            const key = target?.key || "";
            if (!key || currentKeys.has(key) || seenKeys.has(key) || boundaryKeys.has(key)) continue;
            boundaryKeys.add(key);
            boundaryTargets.push(target);
        }
        return boundaryTargets;
    }

    // Returns up to the configured boundary depth's worth of novel video layers to
    // warm, each tagged with its own target frame. The budget is enforced per WHOLE
    // boundary (a partially-warmed boundary still cold-starts), capped by maxEntries.
    function findUpcomingPrebufferTargets(snapshot, endFrame) {
        const currentFrame = Math.max(0, Math.round(Number(snapshot?.frame) || 0));
        const currentKeys = new Set(
            requiredRenderableVideoLayers(snapshot)
                .map((layer) => prebufferKeyForLayer(layer, currentFrame))
        );
        const maxBoundaries = normalizedPrebufferBoundaryDepth();
        const maxEntries = normalizedPrebufferMaxEntries();
        const horizonFrames = Math.max(1, Math.round((normalizedPrebufferLookaheadMs() / 1000) * fps()));
        const candidateFrames = collectPrebufferCandidateFrames(currentFrame, endFrame, horizonFrames);
        const boundaryGroups = effectivePlaybackBoundaryGroups(candidateFrames);
        const targets = [];
        const seenKeys = new Set();
        let boundariesCovered = 0;
        for (const group of boundaryGroups) {
            if (boundariesCovered >= maxBoundaries) break;
            const boundaryTargets = collectBoundaryPrebufferTargets(
                group.frame,
                currentKeys,
                seenKeys,
                {},
                group.layers,
            );
            if (!boundaryTargets.length) continue;
            // Budget is whole-boundary: a partially-warmed boundary still cold-starts,
            // so stop before a boundary that would overflow the cap rather than
            // half-warming it. Always admit the first boundary so a single oversized
            // boundary still warms as far as the schedule's hard cap allows.
            if (targets.length > 0 && targets.length + boundaryTargets.length > maxEntries) break;
            for (const t of boundaryTargets) {
                seenKeys.add(t.key);
                targets.push(t);
            }
            boundariesCovered += 1;
            if (targets.length >= maxEntries) break;
        }
        return targets;
    }

    function findNextRebufferBoundaryTargets(snapshot, endFrame, currentKeys, seenKeys, maxDistanceFrames = Infinity) {
        const currentFrame = Math.max(0, Math.round(Number(snapshot?.frame) || 0));
        const horizonFrames = Math.max(1, Math.round((normalizedPrebufferLookaheadMs() / 1000) * fps()));
        const candidateFrames = collectPrebufferCandidateFrames(currentFrame, endFrame, horizonFrames);
        const boundaryGroups = effectivePlaybackBoundaryGroups(candidateFrames);
        for (const group of boundaryGroups) {
            const distance = playbackFrameDistance(currentFrame, group.frame, endFrame);
            if (Number.isFinite(maxDistanceFrames) && distance > maxDistanceFrames) break;
            const boundaryTargets = collectBoundaryPrebufferTargets(
                group.frame,
                currentKeys,
                seenKeys,
                {
                    intent: "rebuffer-next-boundary",
                    decodePriority: DECODE_PRIORITY_URGENT,
                },
                group.layers,
            );
            if (boundaryTargets.length) return boundaryTargets;
        }
        return [];
    }

    function findCurrentPrebufferSafetyTargets(snapshot, { suppressContinuations = false } = {}) {
        const frame = Math.max(0, Math.round(Number(snapshot?.frame) || 0));
        const playbackEndFrame = state.playbackLoopRange
            ? state.playbackLoopRange.end
            : totalFrames();
        const targets = [];
        const seenKeys = new Set();
        let continuationSafetyAdmitted = 0;
        for (let offset = 0; offset <= PLAYBACK_CURRENT_BOUNDARY_HOLD_FRAMES; offset += 1) {
            const targetFrame = playbackSearchFrame(frame, offset, playbackEndFrame);
            if (targetFrame === null) continue;
            const targetSnapshot = offset === 0 ? snapshot : buildFrameSnapshot(targetFrame);
            const layersByVisualPriority = currentSafetyLayersAtFrame(targetSnapshot, targetFrame, offset);
            for (const layer of layersByVisualPriority) {
                const active = offset === 0 ? state.activePlaybackVideos.get(layer.key) : null;
                if (offset === 0 && activeHasMatchingPrepare(active, layer, targetFrame)) continue;
                if (offset === 0 && isActiveVideoDrawable(active, layer, targetFrame)) continue;
                const candidates = prebufferCandidatesForLayerFrame(layer, targetFrame, { snapshot: targetSnapshot });
                if (candidates.some((candidate) => candidate.readyClaimable)) continue;
                const claimablePending = candidates.find((candidate) => candidate.pendingClaimable);
                if (
                    claimablePending
                    && offset > 0
                    && decodePriorityRank(claimablePending.entry?.decodePriority || DECODE_PRIORITY_LOW)
                        >= decodePriorityRank(DECODE_PRIORITY_URGENT)
                ) {
                    continue;
                }
                if (suppressContinuations && offset > 0) {
                    state.playbackHandoffContinuationSuppressed += 1;
                    state.playbackContinuationSafetySuppressed += 1;
                    continue;
                }
                if (
                    offset > 0
                    && continuationSafetyAdmitted >= PLAYBACK_CONTINUATION_SAFETY_TARGET_BUDGET
                ) {
                    state.playbackContinuationSafetySuppressed += 1;
                    continue;
                }
                const scheduleOrigin = offset === 0 ? "current-frame-recovery" : "current-safety";
                const target = prebufferTargetForLayer(layer, targetFrame, {
                    intent: offset === 0 ? "current-frame-recovery" : "urgent-boundary",
                    decodePriority: DECODE_PRIORITY_URGENT,
                });
                if (!target?.key || seenKeys.has(target.key)) continue;
                if (offset === 0) {
                    target.currentFrameRecoveryCandidate = true;
                    target.currentFrameRecoveryPendingExisting = !!claimablePending;
                    target.currentFrameRecoveryPendingExistingKey = claimablePending?.key || "";
                    target.currentFrameRecoveryPendingState = claimablePending?.entry?.decodeJobState || "";
                    target.currentFrameRecoveryPendingPriority = claimablePending?.entry?.decodeJobPriority || claimablePending?.entry?.decodePriority || "";
                } else {
                    continuationSafetyAdmitted += 1;
                    state.playbackContinuationSafetyAdmitted += 1;
                }
                seenKeys.add(target.key);
                targets.push(withScheduleOrigin(target, scheduleOrigin));
            }
        }
        return targets;
    }

    function findRebufferCurrentRecoveryTargets(snapshot) {
        const frame = Math.max(0, Math.round(Number(snapshot?.frame) || 0));
        const targets = [];
        const seenKeys = new Set();
        for (const layer of requiredClipLayersAfterCoverage(snapshot)) {
            if (!isRenderableVideoLayer(layer)) continue;
            const active = state.activePlaybackVideos.get(layer.key);
            if (isActiveVideoDrawable(active, layer, frame)) continue;
            if (activeHasMatchingPrepare(active, layer, frame)) continue;
            const candidates = prebufferCandidatesForLayerFrame(layer, frame, { snapshot });
            if (candidates.some((candidate) => candidate.readyClaimable)) continue;
            const claimablePending = candidates.find((candidate) => candidate.pendingClaimable);
            const target = prebufferTargetForLayer(layer, frame, {
                intent: "rebuffer-current-recovery",
                decodePriority: DECODE_PRIORITY_URGENT,
            });
            if (!target?.key || seenKeys.has(target.key)) continue;
            target.currentFrameRecoveryCandidate = true;
            target.currentFrameRecoveryPendingExisting = !!claimablePending;
            target.currentFrameRecoveryPendingExistingKey = claimablePending?.key || "";
            target.currentFrameRecoveryPendingState = claimablePending?.entry?.decodeJobState || "";
            target.currentFrameRecoveryPendingPriority = claimablePending?.entry?.decodeJobPriority || claimablePending?.entry?.decodePriority || "";
            seenKeys.add(target.key);
            targets.push(withScheduleOrigin(target, "current-frame-recovery"));
        }
        return targets;
    }

    function normalizedRebufferResumeSafetyFrames() {
        const enterMs = Math.max(
            PLAYBACK_REBUFFER_REENTRY_MS,
            Number(getRebufferEnterMs()) || 0,
        );
        return Math.max(
            PLAYBACK_CURRENT_BOUNDARY_HOLD_FRAMES + 1,
            Math.ceil((enterMs / 1000) * fps()),
        );
    }

    function hasHeldFrameRecoveryWork(snapshot, currentSafetyTargets = []) {
        const currentTargetKeys = new Set(
            (currentSafetyTargets || []).map((target) => target?.key).filter(Boolean)
        );
        const frame = Math.max(0, Math.round(Number(snapshot?.frame) || 0));
        for (const layer of requiredClipLayersAfterCoverage(snapshot)) {
            if (!isRenderableVideoLayer(layer)) continue;
            const active = state.activePlaybackVideos.get(layer.key);
            if (isActiveVideoDrawable(active, layer, frame)) continue;
            const currentKey = prebufferKeyForLayer(layer, frame);
            if (currentKey && currentTargetKeys.has(currentKey)) continue;
            if (activeHasMatchingPrepare(active, layer, frame)) continue;
            const candidates = prebufferCandidatesForLayerFrame(layer, frame, { snapshot });
            if (candidates.some((candidate) => candidate.readyClaimable)) continue;
            if (candidates.some((candidate) => (
                candidate.pendingClaimable
                && decodePriorityRank(candidate.entry?.decodePriority || DECODE_PRIORITY_LOW)
                    >= decodePriorityRank(DECODE_PRIORITY_URGENT)
            ))) {
                continue;
            }
            return false;
        }
        return true;
    }

    function currentFrameHandoffRecoveryState(snapshot) {
        const frame = Math.max(0, Math.round(Number(snapshot?.frame) || 0));
        const currentKeys = new Set();
        let recoverable = false;
        let blockedCount = 0;
        let queuedExactCurrentCount = 0;
        let activeExactCurrentCount = 0;
        let pendingExactCurrentCount = 0;
        for (const layer of requiredClipLayersAfterCoverage(snapshot)) {
            if (!isRenderableVideoLayer(layer)) continue;
            const currentKey = prebufferKeyForLayer(layer, frame);
            if (currentKey) currentKeys.add(currentKey);
            const active = state.activePlaybackVideos.get(layer.key);
            if (isActiveVideoDrawable(active, layer, frame)) continue;
            const candidates = prebufferCandidatesForLayerFrame(layer, frame, { snapshot });
            if (candidates.some((candidate) => candidate.readyClaimable)) continue;
            if (activeHasMatchingPrepare(active, layer, frame)) {
                recoverable = true;
                blockedCount += 1;
                activeExactCurrentCount += 1;
                continue;
            }
            const pending = candidates.find((candidate) => candidate.pendingClaimable);
            if (pending) {
                recoverable = true;
                blockedCount += 1;
                pendingExactCurrentCount += 1;
                if (pending.entry?.decodeJobState === "queued") {
                    queuedExactCurrentCount += 1;
                } else if (
                    pending.entry?.decodeJobState === "active"
                    || pending.entry?.decodeJobState === "source-pending"
                ) {
                    activeExactCurrentCount += 1;
                }
            }
        }
        return {
            active: recoverable,
            blockedCount,
            currentKeys,
            frame,
            queuedExactCurrentCount,
            activeExactCurrentCount,
            pendingExactCurrentCount,
            shouldSuppressFuture: queuedExactCurrentCount > 0,
            shouldPruneFuture: queuedExactCurrentCount > 0,
        };
    }

    function rebufferHeldFrameCommitted() {
        const holdFrame = Math.max(0, Math.round(Number(state.playbackRebufferFrame) || 0));
        return !!(
            state.playbackCompositeCommitted
            && state.playbackLastCommittedFrame === holdFrame
            && state.playbackLastCommittedSessionId === state.playbackSessionId
        );
    }

    function pruneQueuedPrebuffersForCurrentHandoff(handoffState = {}, currentTargets = []) {
        const exactCurrentTargets = (currentTargets || [])
            .filter((target) => target?.scheduleOrigin === "current-frame-recovery");
        const hasQueuedExactCurrent = !!(
            handoffState?.queuedExactCurrentCount > 0
            || exactCurrentTargets.some((target) => (
                target.currentFrameRecoveryPendingExisting
                && target.currentFrameRecoveryPendingState === "queued"
            ))
        );
        if (!handoffState?.active) {
            state.playbackLastHandoffPruneReason = "not-active";
            return { low: 0, urgent: 0, reason: state.playbackLastHandoffPruneReason };
        }
        if (!hasQueuedExactCurrent) {
            state.playbackLastHandoffPruneReason = "no-queued-exact-current";
            return { low: 0, urgent: 0, reason: state.playbackLastHandoffPruneReason };
        }
        const keepKeys = new Set(handoffState.currentKeys || []);
        for (const target of exactCurrentTargets) {
            if (target?.key) keepKeys.add(target.key);
        }
        for (const target of state.playbackRebufferSafetyTargets || []) {
            if (target?.key) keepKeys.add(target.key);
        }
        let low = 0;
        let urgent = 0;
        for (const [key, entry] of Array.from(state.prebufferCache.entries())) {
            if (!entry || keepKeys.has(key) || playbackWaitingForPrebufferEntry(entry)) continue;
            if (entry.ready) continue;
            if (entry.scheduleOrigin === "current-frame-recovery" || entry.scheduleOrigin === "rebuffer-next-boundary") continue;
            const priority = entry.decodeJobPriority || entry.decodePriority || DECODE_PRIORITY_LOW;
            if (priority !== DECODE_PRIORITY_LOW && priority !== DECODE_PRIORITY_URGENT) continue;
            if (entry.decodeJobState !== "queued" || !entry.decodeJob) continue;
            if (!playbackDecodeLimiter.cancelQueued(entry.decodeJob, "current-handoff-preempt")) continue;
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
            if (priority === DECODE_PRIORITY_LOW) {
                low += 1;
            } else {
                urgent += 1;
            }
        }
        state.playbackHandoffQueuedLowPruned += low;
        state.playbackHandoffQueuedUrgentPruned += urgent;
        state.playbackLastHandoffPruneReason = low || urgent
            ? "queued-exact-current-pruned"
            : "queued-exact-current-no-prune";
        return { low, urgent, reason: state.playbackLastHandoffPruneReason };
    }

    function clearRebufferSafetyState() {
        state.playbackRebufferSafetyTargets = [];
        state.playbackRebufferSafetySig = "";
        state.playbackRebufferSafetyInitialized = false;
        state.playbackRebufferSafetyRetainedCount = 0;
        state.playbackRebufferSafetyDiscardedCount = 0;
        state.playbackRebufferLastSafetyStatuses = [];
        state.playbackRebufferLastSafetyReason = "";
        state.playbackRebufferResumeDeferredSig = "";
        state.playbackRebufferResumeDeferredAtMs = 0;
        state.playbackRebufferResumeDeferredSuppressed = 0;
        state.playbackRebufferLimitedSuppressed = 0;
    }

    function clearDeferredNextBoundaryTargets(reason = "clear") {
        state.playbackDeferredNextBoundaryTargets = [];
        state.playbackDeferredNextBoundaryRetainedCount = 0;
        state.playbackDeferredNextBoundaryDroppedCount = 0;
        state.playbackDeferredNextBoundaryScheduledCount = 0;
        state.playbackDeferredNextBoundaryLastClearReason = reason;
    }

    function rebufferSafetyTargetAllowed(target) {
        return !!(
            target
            && (
                target.scheduleOrigin === "current-safety"
                || target.scheduleOrigin === "current-frame-recovery"
                || target.scheduleOrigin === "rebuffer-next-boundary"
            )
        );
    }

    function makeStoredRebufferSafetyTarget(target) {
        return {
            ...target,
            layer: null,
            playbackSessionId: state.playbackSessionId,
            warmToken: state.playbackWarmContentToken,
            storedAtMs: performance.now(),
        };
    }

    function resolveRebufferSafetyTarget(target) {
        if (!rebufferSafetyTargetAllowed(target)) {
            return { valid: false, status: "invalid-origin", target };
        }
        if (target.playbackSessionId !== state.playbackSessionId) {
            return { valid: false, status: "stale-session", target };
        }
        if (target.warmToken !== state.playbackWarmContentToken) {
            return { valid: false, status: "stale-token", target };
        }
        const targetFrame = Math.max(0, Math.round(Number(target.targetFrame) || 0));
        const targetSnapshot = buildFrameSnapshot(targetFrame);
        const layer = requiredClipLayersAfterCoverage(targetSnapshot)
            .find((candidate) => candidate?.key === target.layerKey && isRenderableVideoLayer(candidate));
        if (!layer) {
            return { valid: false, status: "not-required", target, snapshot: targetSnapshot };
        }
        const resolvedTarget = prebufferTargetForLayer(layer, targetFrame, {
            intent: target.intent || "warm-ahead",
            decodePriority: target.decodePriority || DECODE_PRIORITY_LOW,
        });
        const identityValid = !!(
            resolvedTarget
            && resolvedTarget.key === target.key
            && resolvedTarget.sourcePath === target.sourcePath
            && resolvedTarget.targetSourceFrame === target.targetSourceFrame
            && resolvedTarget.sourceTargetKey === target.sourceTargetKey
        );
        if (!identityValid) {
            return { valid: false, status: "identity-changed", target, snapshot: targetSnapshot };
        }
        return {
            valid: true,
            layer,
            snapshot: targetSnapshot,
            target: {
                ...target,
                ...resolvedTarget,
                layer,
                scheduleOrigin: target.scheduleOrigin || "",
                intent: target.intent || resolvedTarget.intent || "",
                decodePriority: target.decodePriority || resolvedTarget.decodePriority || DECODE_PRIORITY_LOW,
                playbackSessionId: target.playbackSessionId,
                warmToken: target.warmToken,
                storedAtMs: target.storedAtMs,
            },
        };
    }

    function retainValidRebufferSafetyTargets({ countStats = false } = {}) {
        const retained = [];
        let discarded = 0;
        for (const target of state.playbackRebufferSafetyTargets || []) {
            const resolved = resolveRebufferSafetyTarget(target);
            if (resolved.valid) {
                retained.push(resolved.target);
            } else {
                discarded += 1;
            }
        }
        state.playbackRebufferSafetyTargets = retained;
        state.playbackRebufferSafetySig = prebufferTargetSignature(retained);
        if (countStats) {
            state.playbackRebufferSafetyRetainedCount = retained.length;
            state.playbackRebufferSafetyDiscardedCount += discarded;
        }
        return retained;
    }

    function storeRebufferSafetyTargets(targets) {
        const retained = retainValidRebufferSafetyTargets({ countStats: true });
        const seenKeys = new Set(retained.map((target) => target.key).filter(Boolean));
        const hasStoredNextBoundary = retained.some((target) => target.scheduleOrigin === "rebuffer-next-boundary");
        const nextBoundaryAdditionsAllowed = !state.playbackRebufferSafetyInitialized || !hasStoredNextBoundary;
        const additions = [];
        for (const target of (targets || [])) {
            if (!rebufferSafetyTargetAllowed(target) || !target.key || seenKeys.has(target.key)) continue;
            if (
                state.playbackRebufferSafetyInitialized
                && (target.scheduleOrigin === "current-safety" || target.scheduleOrigin === "current-frame-recovery")
            ) continue;
            if (state.playbackRebufferSafetyInitialized && target.scheduleOrigin === "rebuffer-next-boundary" && !nextBoundaryAdditionsAllowed) continue;
            seenKeys.add(target.key);
            additions.push(makeStoredRebufferSafetyTarget(target));
        }
        state.playbackRebufferSafetyInitialized = true;
        state.playbackRebufferSafetyTargets = [...retained, ...additions];
        state.playbackRebufferSafetySig = prebufferTargetSignature(state.playbackRebufferSafetyTargets);
        return retainValidRebufferSafetyTargets();
    }

    function deferredNextBoundaryTargetValid(target, snapshot, endFrame) {
        if (!target || target.scheduleOrigin !== "rebuffer-next-boundary") {
            return { valid: false, status: "invalid-origin", target };
        }
        const resolved = resolveRebufferSafetyTarget(target);
        if (!resolved.valid) return resolved;
        const snapshotFrame = Number(snapshot?.frame);
        const current = Math.max(0, Math.round(Number.isFinite(snapshotFrame) ? snapshotFrame : currentFrame()));
        const targetFrame = Math.max(0, Math.round(Number(resolved.target?.targetFrame) || 0));
        const distance = playbackFrameDistance(current, targetFrame, endFrame);
        if (!Number.isFinite(distance) || distance <= 0) {
            return { valid: false, status: "past-or-current", target: resolved.target, snapshot: resolved.snapshot };
        }
        if (distance > normalizedRebufferResumeSafetyFrames()) {
            return { valid: false, status: "outside-resume-safety", target: resolved.target, snapshot: resolved.snapshot };
        }
        return {
            valid: true,
            status: "retained",
            layer: resolved.layer,
            snapshot: resolved.snapshot,
            target: applyPrebufferDeadlineMetadata({
                ...resolved.target,
                deferredNextBoundary: true,
                intent: resolved.target.intent || "rebuffer-next-boundary",
                decodePriority: DECODE_PRIORITY_URGENT,
                scheduleOrigin: "rebuffer-next-boundary",
            }, current, endFrame),
        };
    }

    function storedDeferredNextBoundaryTarget(target) {
        return {
            ...target,
            layer: null,
            playbackSessionId: state.playbackSessionId,
            warmToken: state.playbackWarmContentToken,
            storedAtMs: target?.storedAtMs || performance.now(),
            deferredNextBoundary: true,
        };
    }

    function resolveDeferredNextBoundaryTargets(snapshot, endFrame) {
        const resolvedTargets = [];
        const retained = [];
        const seenKeys = new Set();
        let dropped = 0;
        for (const target of state.playbackDeferredNextBoundaryTargets || []) {
            const resolved = deferredNextBoundaryTargetValid(target, snapshot, endFrame);
            if (!resolved.valid || !resolved.target?.key || seenKeys.has(resolved.target.key)) {
                dropped += 1;
                continue;
            }
            seenKeys.add(resolved.target.key);
            retained.push(storedDeferredNextBoundaryTarget(resolved.target));
            resolvedTargets.push(resolved.target);
        }
        state.playbackDeferredNextBoundaryTargets = retained;
        state.playbackDeferredNextBoundaryRetainedCount = retained.length;
        state.playbackDeferredNextBoundaryDroppedCount += dropped;
        state.playbackDeferredNextBoundaryScheduledCount = resolvedTargets.length;
        return resolvedTargets;
    }

    function preserveDeferredNextBoundaryTargetsFromRebuffer(snapshot, endFrame) {
        const retained = [];
        const seenKeys = new Set();
        let dropped = 0;
        for (const target of state.playbackRebufferSafetyTargets || []) {
            if (target?.scheduleOrigin !== "rebuffer-next-boundary") continue;
            const resolved = deferredNextBoundaryTargetValid(target, snapshot, endFrame);
            const entry = resolved.valid ? state.prebufferCache.get(resolved.target.key) : null;
            const usefulEntry = !!(
                entry
                && !entry.cancelled
                && !entry.consumed
                && !entry.claimedByActive
                && entry.warmToken === state.playbackWarmContentToken
                && (entry.ready || entry.decodeJobState === "queued" || entry.decodeJobState === "active" || entry.decodeJobState === "source-pending")
            );
            if (!resolved.valid || !usefulEntry || !resolved.target?.key || seenKeys.has(resolved.target.key)) {
                dropped += 1;
                continue;
            }
            seenKeys.add(resolved.target.key);
            retained.push(storedDeferredNextBoundaryTarget(resolved.target));
        }
        state.playbackDeferredNextBoundaryTargets = retained;
        state.playbackDeferredNextBoundaryRetainedCount = retained.length;
        state.playbackDeferredNextBoundaryDroppedCount += dropped;
        state.playbackDeferredNextBoundaryScheduledCount = 0;
        return retained.length;
    }

    function rebufferSafetyTargetStatus(target, now) {
        const resolved = resolveRebufferSafetyTarget(target);
        if (!resolved.valid) {
            return { satisfied: true, status: resolved.status, target };
        }
        const { layer, snapshot: targetSnapshot } = resolved;
        const resolvedTarget = resolved.target;
        const targetFrame = resolvedTarget.targetFrame;
        const active = state.activePlaybackVideos.get(layer.key);
        if (isActiveVideoDrawable(active, layer, targetFrame)) {
            return { satisfied: true, status: "active", target: resolvedTarget };
        }
        if (activeHasMatchingPrepare(active, layer, targetFrame)) {
            return { satisfied: false, status: "active-prepare", target: resolvedTarget, pendingAgeMs: null };
        }
        const candidates = prebufferCandidatesForLayerFrame(layer, targetFrame, { snapshot: targetSnapshot });
        if (candidates.some((candidate) => candidate.readyClaimable)) {
            return { satisfied: true, status: "ready-prebuffer", target: resolvedTarget };
        }
        const pending = candidates.find((candidate) => candidate.pendingClaimable);
        const ownerUnavailable = candidates.find((candidate) => (
            candidate.tokenMatches
            && candidate.reusable
            && !candidate.ownerAvailable
        ));
        const pendingStartedAt = Number(pending?.entry?.scheduledAtMs);
        const activeStartedAt = Number(active?.pendingPrebufferStartedAt);
        const ageSource = Number.isFinite(pendingStartedAt)
            ? pendingStartedAt
            : (Number.isFinite(activeStartedAt) ? activeStartedAt : null);
        return {
            satisfied: false,
            status: pending ? "pending" : (ownerUnavailable ? "owner-unavailable" : "missing"),
            target: resolvedTarget,
            pendingAgeMs: ageSource !== null ? Math.max(0, now - ageSource) : null,
        };
    }

    function rebufferNextBoundarySoftBudgetMs() {
        const numeric = Number(getRebufferEnterMs());
        return Math.min(
            PLAYBACK_REBUFFER_NEXT_BOUNDARY_SOFT_MAX_MS,
            Math.max(0, Number.isFinite(numeric) ? numeric : PLAYBACK_REBUFFER_NEXT_BOUNDARY_SOFT_MAX_MS),
        );
    }

    function rebufferSafetyStatusTelemetry(statuses, limit = 16) {
        return (statuses || []).slice(0, limit).map((entry) => ({
            status: entry.status || "",
            satisfied: !!entry.satisfied,
            scheduleOrigin: entry.target?.scheduleOrigin || "",
            key: entry.target?.key || "",
            layerKey: entry.target?.layerKey || "",
            targetFrame: entry.target?.targetFrame ?? null,
            targetSourceFrame: entry.target?.targetSourceFrame ?? null,
            sourceTargetKey: entry.target?.sourceTargetKey || "",
            deadlineClass: entry.target?.deadlineClass || "",
            deadlineFrame: entry.target?.deadlineFrame ?? null,
            deadlineDistanceFrames: entry.target?.deadlineDistanceFrames ?? null,
            pendingAgeMs: entry.pendingAgeMs !== null && entry.pendingAgeMs !== undefined
                ? roundTelemetryMs(entry.pendingAgeMs)
                : null,
        }));
    }

    function evaluateRebufferResumeSafety(snapshot, now) {
        const holdFrame = Math.max(0, Math.round(Number(state.playbackRebufferFrame) || 0));
        const heldFrameCommitted = !!(
            state.playbackCompositeCommitted
            && state.playbackLastCommittedFrame === holdFrame
            && state.playbackLastCommittedSessionId === state.playbackSessionId
        );
        const targets = state.playbackRebufferSafetyTargets || [];
        const statuses = targets.map((target) => rebufferSafetyTargetStatus(target, now));
        const blocked = statuses.filter((entry) => !entry.satisfied);
        const currentBlocked = blocked.filter((entry) => entry.target?.scheduleOrigin !== "rebuffer-next-boundary");
        const nextBoundaryBlocked = blocked.filter((entry) => entry.target?.scheduleOrigin === "rebuffer-next-boundary");
        const pending = blocked.filter((entry) => entry.status === "pending");
        const missing = blocked.filter((entry) => entry.status === "missing");
        const nextBoundarySoftBudgetMs = rebufferNextBoundarySoftBudgetMs();
        const ready = !!(
            heldFrameCommitted
            && currentBlocked.length === 0
        );
        const nextBoundaryTarget = targets.find((target) => target?.scheduleOrigin === "rebuffer-next-boundary");
        const pendingAges = pending
            .map((entry) => entry.pendingAgeMs)
            .filter((value) => Number.isFinite(value));
        const reason = ready
            ? (nextBoundaryBlocked.length > 0 ? "recovered-next-boundary-pending" : "recovered")
            : "deferred";
        state.playbackRebufferLastSafetyStatuses = statuses;
        state.playbackRebufferLastSafetyReason = reason;
        return {
            ready,
            reason,
            heldFrameCommitted,
            targetCount: targets.length,
            readyCount: statuses.length - blocked.length,
            pendingCount: pending.length,
            missingCount: missing.length,
            currentBlockedCount: currentBlocked.length,
            nextBoundaryBlockedCount: nextBoundaryBlocked.length,
            nextBoundaryTimedOut: false,
            nextBoundaryPendingNonBlocking: ready ? nextBoundaryBlocked.length : 0,
            nextBoundarySoftBudgetMs,
            nextBoundaryFrame: nextBoundaryTarget?.targetFrame ?? null,
            oldestPendingWaitMs: pendingAges.length ? Math.max(...pendingAges) : null,
            statuses,
        };
    }

    function recordRebufferResumeDeferred(snapshot, safety, heldMs, now) {
        if (!playbackTelemetryActive()) return;
        const sig = [
            state.playbackSessionId,
            state.playbackRebufferFrame,
            state.playbackRebufferSafetySig,
            safety.pendingCount,
            safety.missingCount,
            safety.currentBlockedCount,
            safety.nextBoundaryBlockedCount,
            state.playbackRebufferCapped ? "capped" : "active",
        ].join("|");
        if (
            sig === state.playbackRebufferResumeDeferredSig
            && now - state.playbackRebufferResumeDeferredAtMs < PLAYBACK_REBUFFER_TELEMETRY_THROTTLE_MS
        ) {
            state.playbackRebufferResumeDeferredSuppressed += 1;
            return;
        }
        const suppressedSinceLast = state.playbackRebufferResumeDeferredSuppressed;
        state.playbackRebufferResumeDeferredSuppressed = 0;
        state.playbackRebufferResumeDeferredSig = sig;
        state.playbackRebufferResumeDeferredAtMs = now;
        recordPlaybackTelemetry("playback_rebuffer_resume_deferred", {
            frame: state.playbackRebufferFrame,
            heldMs: roundTelemetryMs(heldMs),
            nextBoundaryFrame: safety.nextBoundaryFrame,
            targetCount: safety.targetCount,
            readyCount: safety.readyCount,
            pendingCount: safety.pendingCount,
            missingCount: safety.missingCount,
            currentBlockedCount: safety.currentBlockedCount,
            nextBoundaryBlockedCount: safety.nextBoundaryBlockedCount,
            nextBoundaryPendingNonBlocking: safety.nextBoundaryPendingNonBlocking || 0,
            nextBoundarySoftBudgetMs: roundTelemetryMs(safety.nextBoundarySoftBudgetMs),
            suppressedSinceLast,
            oldestPendingWaitMs: safety.oldestPendingWaitMs !== null
                ? roundTelemetryMs(safety.oldestPendingWaitMs)
                : null,
            blockTargetKey: state.playbackRebufferBlockTargetKey || "",
            safetyTargetSignature: state.playbackRebufferSafetySig || "",
            safetyStatuses: rebufferSafetyStatusTelemetry(safety.statuses),
            heldFrameCommitted: !!safety.heldFrameCommitted,
            capped: !!state.playbackRebufferCapped,
            ...playbackDecodeLimiter.snapshotStats(),
            playbackSessionId: state.playbackSessionId,
        });
    }

    function rebufferBlockTargetKey(details = {}) {
        const frame = details?.frame ?? details?.pendingPrebufferFrame ?? "";
        const sourceTargetKey = details?.sourceTargetKey || details?.pendingPrebufferSourceTargetKey || "";
        const targetSourceFrame = details?.targetSourceFrame ?? details?.pendingPrebufferTargetSourceFrame ?? "";
        return [
            state.playbackSessionId,
            details?.reason || "",
            details?.layerKey || "",
            details?.sourcePath || "",
            frame,
            targetSourceFrame,
            sourceTargetKey,
            details?.prebufferKey || details?.pendingPrebufferKey || "",
        ].join("|");
    }

    function recordRebufferEntryDecision(decision) {
        if (!playbackTelemetryActive()) return;
        const sig = [
            decision.decision,
            decision.blockTargetKey || "",
            decision.lastExitTargetKey || "",
            decision.sameTarget ? "same" : "new",
            decision.recoveryInFlight ? "recovery" : "idle",
        ].join("|");
        if (sig === state.playbackRebufferEntryDecisionSig) return;
        state.playbackRebufferEntryDecisionSig = sig;
        recordPlaybackTelemetry("playback_rebuffer_entry_decision", {
            ...decision,
            cooldownRemainingMs: roundTelemetryMs(decision.cooldownRemainingMs || 0),
            playbackSessionId: state.playbackSessionId,
        });
    }

    function rebufferEntryDecision(now, details, snapshot) {
        const blockTargetKey = rebufferBlockTargetKey(details);
        const lastExitTargetKey = state.playbackRebufferLastExitTargetKey || "";
        const elapsedSinceExit = now - (Number(state.playbackRebufferLastExitMs) || 0);
        const sameTarget = !!blockTargetKey && blockTargetKey === lastExitTargetKey;
        const recoveryInFlight = hasCurrentLayerPrepareInFlight(snapshot);
        if (!recoveryInFlight) {
            const decision = {
                allowed: false,
                decision: "suppressed-no-recovery",
                blockTargetKey,
                lastExitTargetKey,
                sameTarget,
                recoveryInFlight,
                cooldownRemainingMs: 0,
            };
            recordRebufferEntryDecision(decision);
            return decision;
        }
        if ((!blockTargetKey || sameTarget) && elapsedSinceExit < PLAYBACK_REBUFFER_REENTRY_MS) {
            const decision = {
                allowed: false,
                decision: "suppressed-same-target-cooldown",
                blockTargetKey,
                lastExitTargetKey,
                sameTarget: true,
                recoveryInFlight,
                cooldownRemainingMs: PLAYBACK_REBUFFER_REENTRY_MS - elapsedSinceExit,
            };
            recordRebufferEntryDecision(decision);
            return decision;
        }
        const decision = {
            allowed: true,
            decision: elapsedSinceExit < PLAYBACK_REBUFFER_REENTRY_MS
                ? "allowed-target-changed"
                : "allowed-cooldown-elapsed",
            blockTargetKey,
            lastExitTargetKey,
            sameTarget,
            recoveryInFlight,
            cooldownRemainingMs: 0,
        };
        if (decision.decision === "allowed-target-changed") {
            recordRebufferEntryDecision(decision);
        }
        return decision;
    }

    function updatePrebufferScheduleStats(snapshot, {
        requiredLayers = [],
        targets = [],
        rebufferLimited = false,
        reason = "",
        handoffActive = false,
    } = {}) {
        const rawVideoLayers = rawRenderableVideoLayers(snapshot);
        const requiredVideoLayers = (requiredLayers || []).filter(isRenderableVideoLayer);
        state.lastPrebufferScheduleStats = {
            rawPlayableVideoCount: rawVideoLayers.length,
            requiredVideoCount: requiredVideoLayers.length,
            culledCoveredVideoCount: Math.max(0, rawVideoLayers.length - requiredVideoLayers.length),
            currentSafetyTargetCount: targets.filter((target) => (
                target?.scheduleOrigin === "current-safety"
                || target?.scheduleOrigin === "current-frame-recovery"
            )).length,
            currentFrameRecoveryTargetCount: targets.filter((target) => target?.scheduleOrigin === "current-frame-recovery").length,
            currentFrameRecoveryCandidateCount: targets.filter((target) => target?.currentFrameRecoveryCandidate).length,
            currentFrameRecoveryPendingExistingCount: targets.filter((target) => target?.currentFrameRecoveryPendingExisting).length,
            currentFrameRecoveryMovedFrontCount: state.playbackCurrentFrameRecoveryMovedFrontCount,
            currentFrameRecoveryReclassifiedCount: state.playbackCurrentFrameRecoveryReclassifiedCount,
            currentFrameRecoveryQueuedPromotedCount: state.playbackCurrentFrameRecoveryQueuedPromotedCount,
            currentFrameRecoveryActiveReclassifiedCount: state.playbackCurrentFrameRecoveryActiveReclassifiedCount,
            continuationSafetyTargetCount: targets.filter((target) => target?.scheduleOrigin === "current-safety").length,
            continuationSafetyAdmitted: state.playbackContinuationSafetyAdmitted,
            continuationSafetySuppressed: state.playbackContinuationSafetySuppressed,
            currentSafetyAdmissionBlocked: playbackDecodeLimiter.snapshotStats().decodeUrgentCurrentSafetyAdmissionBlocked || 0,
            upcomingTargetCount: targets.filter((target) => target?.scheduleOrigin === "upcoming").length,
            rebufferNextBoundaryTargetCount: targets.filter((target) => target?.scheduleOrigin === "rebuffer-next-boundary").length,
            deferredNextBoundaryTargetCount: targets.filter((target) => target?.deferredNextBoundary).length,
            deferredNextBoundaryRetainedCount: state.playbackDeferredNextBoundaryRetainedCount,
            deferredNextBoundaryDroppedCount: state.playbackDeferredNextBoundaryDroppedCount,
            prebufferTargetCount: targets.length,
            handoffPruneReason: state.playbackLastHandoffPruneReason || "",
            rawLayerSignature: playbackLayerKeySignature(rawVideoLayers),
            requiredLayerSignature: playbackLayerKeySignature(requiredVideoLayers),
            targetSignature: prebufferTargetSignature(targets),
            rebufferLimited: !!rebufferLimited,
            handoffActive: !!handoffActive,
            reason,
        };
    }

    function schedulePlaybackPrebuffer(snapshot, { handoffState = null } = {}) {
        const requiredCurrentLayers = requiredClipLayersAfterCoverage(snapshot);
        const currentHandoff = handoffState || currentFrameHandoffRecoveryState(snapshot);
        if (!state.isPlaying || !isPrebufferEnabled()) {
            clearRebufferSafetyState();
            clearDeferredNextBoundaryTargets(!state.isPlaying ? "not-playing" : "prebuffer-disabled");
            updatePrebufferScheduleStats(snapshot, {
                requiredLayers: requiredCurrentLayers,
                handoffActive: currentHandoff.active,
                reason: !state.isPlaying ? "not-playing" : "prebuffer-disabled",
            });
            clearPrebufferCache();
            return;
        }
        const playbackEndFrame = state.playbackLoopRange
            ? state.playbackLoopRange.end
            : totalFrames();
        if (!state.playbackLoopRange && playbackEndFrame <= (snapshot?.frame || 0) + 1) {
            clearRebufferSafetyState();
            clearDeferredNextBoundaryTargets("at-playback-end");
            updatePrebufferScheduleStats(snapshot, {
                requiredLayers: requiredCurrentLayers,
                handoffActive: currentHandoff.active,
                reason: "at-playback-end",
            });
            clearPrebufferCache();
            return;
        }
        const orderedTargets = [];
        const orderedKeys = new Set();
        const rebufferLimited = !!state.playbackRebuffering;
        const maxEntries = normalizedPrebufferMaxEntries();
        const suppressFutureForQueuedCurrent = !!currentHandoff.shouldSuppressFuture;
        const currentSafetyTargets = (rebufferLimited
            ? findRebufferCurrentRecoveryTargets(snapshot)
            : findCurrentPrebufferSafetyTargets(snapshot, { suppressContinuations: suppressFutureForQueuedCurrent }))
            .map((target) => withScheduleOrigin(target, target.scheduleOrigin || "current-safety"));
        const currentFrameRecoveryTargets = currentSafetyTargets
            .filter((target) => target?.scheduleOrigin === "current-frame-recovery");
        const currentFrameRecoveryPendingExistingCount = currentFrameRecoveryTargets
            .filter((target) => target?.currentFrameRecoveryPendingExisting).length;
        state.playbackHandoffCurrentTargetCount += currentFrameRecoveryTargets.length;
        state.playbackCurrentFrameRecoveryCandidateCount += currentFrameRecoveryTargets.length;
        state.playbackCurrentFrameRecoveryPendingExistingCount += currentFrameRecoveryPendingExistingCount;
        const hasQueuedExactCurrentTarget = currentFrameRecoveryTargets.some((target) => (
            target?.currentFrameRecoveryPendingExisting
            && target.currentFrameRecoveryPendingState === "queued"
        ));
        const shouldSuppressFuture = suppressFutureForQueuedCurrent || hasQueuedExactCurrentTarget;
        const staleUrgentPruned = dropStaleQueuedUrgentPrebuffers("prebuffer-schedule-stale-urgent");
        pruneQueuedPrebuffersForCurrentHandoff(currentHandoff, currentSafetyTargets);
        const currentRequiredKeys = new Set(
            requiredCurrentLayers
                .filter(isRenderableVideoLayer)
                .map((layer) => prebufferKeyForLayer(layer, snapshot.frame))
                .filter(Boolean)
        );
        let upcomingTargets = [];
        let rebufferNextBoundaryTargets = [];
        let deferredNextBoundaryTargets = [];
        if (rebufferLimited) {
            const stableSafetyCount = state.playbackRebufferSafetyInitialized
                ? (state.playbackRebufferSafetyTargets || []).length
                : currentSafetyTargets.length;
            const seenKeys = new Set([
                ...(state.playbackRebufferSafetyTargets || []).map((target) => target?.key).filter(Boolean),
                ...currentSafetyTargets.map((target) => target?.key).filter(Boolean),
            ]);
            const heldFrameCommitted = rebufferHeldFrameCommitted();
            const nextBoundaryAllowed = heldFrameCommitted || !currentHandoff.active;
            if (!nextBoundaryAllowed) {
                state.playbackHandoffNextBoundaryDelayed += 1;
            }
            const nextBoundaryGroup = nextBoundaryAllowed && hasHeldFrameRecoveryWork(snapshot, currentSafetyTargets)
                ? findNextRebufferBoundaryTargets(
                    snapshot,
                    playbackEndFrame,
                    currentRequiredKeys,
                    seenKeys,
                    normalizedRebufferResumeSafetyFrames(),
                )
                : [];
            if (
                nextBoundaryGroup.length
                && stableSafetyCount + nextBoundaryGroup.length <= maxEntries
            ) {
                rebufferNextBoundaryTargets = nextBoundaryGroup
                    .map((target) => withScheduleOrigin(target, "rebuffer-next-boundary"));
            }
        } else {
            deferredNextBoundaryTargets = resolveDeferredNextBoundaryTargets(snapshot, playbackEndFrame)
                .map((target) => withScheduleOrigin(target, "rebuffer-next-boundary"));
            if (shouldSuppressFuture) {
                state.playbackHandoffUpcomingSuppressed += 1;
            } else {
                upcomingTargets = findUpcomingPrebufferTargets(snapshot, playbackEndFrame)
                    .map((target) => withScheduleOrigin(target, "upcoming"));
            }
        }
        for (const target of [
            ...currentSafetyTargets,
            ...(rebufferLimited ? rebufferNextBoundaryTargets : deferredNextBoundaryTargets),
            ...(!rebufferLimited ? upcomingTargets : []),
        ]) {
            if (!target?.key || orderedKeys.has(target.key)) continue;
            orderedKeys.add(target.key);
            orderedTargets.push(applyPrebufferDeadlineMetadata(target, snapshot.frame, playbackEndFrame));
        }
        let targets = orderedTargets;
        // Hard RAM/VRAM safety cap. findUpcomingPrebufferTargets already enforces a
        // whole-boundary budget, so this only bites when a single boundary needs more
        // elements than the entire cap (degenerate stacked scene) — warming as many as
        // fit still beats warming none. Claimed entries are no longer in prebufferCache,
        // so this bounds the warmed element count.
        if (targets.length > maxEntries) {
            targets = targets.slice(0, maxEntries);
        }
        if (rebufferLimited) {
            targets = storeRebufferSafetyTargets(targets);
        } else {
            clearRebufferSafetyState();
        }
        updatePrebufferScheduleStats(snapshot, {
            requiredLayers: requiredCurrentLayers,
            targets,
            rebufferLimited,
            handoffActive: currentHandoff.active,
            reason: rebufferLimited ? "rebuffer-limited" : "normal",
        });
        if (rebufferLimited) {
            const scheduleStats = state.lastPrebufferScheduleStats || emptyPrebufferScheduleStats("missing");
            const sig = [
                snapshot?.frame ?? "",
                targets.map((target) => target.key).join("|"),
                scheduleStats.rawLayerSignature || "",
                scheduleStats.requiredLayerSignature || "",
                scheduleStats.targetSignature || "",
            ].join("::");
            if (sig !== state.lastRebufferLimitedScheduleSig) {
                const suppressedSinceLast = state.playbackRebufferLimitedSuppressed;
                state.playbackRebufferLimitedSuppressed = 0;
                state.lastRebufferLimitedScheduleSig = sig;
                recordPlaybackTelemetry("playback_prebuffer_rebuffer_limited", {
                    frame: snapshot?.frame ?? null,
                    targetCount: targets.length,
                    targets: targets.map((target) => ({
                        key: target.key,
                        layerKey: target.layerKey,
                        targetFrame: target.targetFrame,
                        targetSourceFrame: target.targetSourceFrame,
                        intent: target.intent,
                        decodePriority: target.decodePriority,
                        scheduleOrigin: target.scheduleOrigin || "",
                        deadlineClass: target.deadlineClass || "",
                        deadlineFrame: target.deadlineFrame ?? null,
                        deadlineDistanceFrames: target.deadlineDistanceFrames ?? null,
                    })),
                    rawPlayableVideoCount: scheduleStats.rawPlayableVideoCount,
                    requiredVideoCount: scheduleStats.requiredVideoCount,
                    culledCoveredVideoCount: scheduleStats.culledCoveredVideoCount,
                    currentSafetyTargetCount: scheduleStats.currentSafetyTargetCount,
                    currentFrameRecoveryTargetCount: scheduleStats.currentFrameRecoveryTargetCount,
                    currentFrameRecoveryCandidateCount: scheduleStats.currentFrameRecoveryCandidateCount,
                    currentFrameRecoveryPendingExistingCount: scheduleStats.currentFrameRecoveryPendingExistingCount,
                    currentFrameRecoveryMovedFrontCount: scheduleStats.currentFrameRecoveryMovedFrontCount,
                    currentFrameRecoveryReclassifiedCount: scheduleStats.currentFrameRecoveryReclassifiedCount,
                    currentFrameRecoveryQueuedPromotedCount: scheduleStats.currentFrameRecoveryQueuedPromotedCount,
                    currentFrameRecoveryActiveReclassifiedCount: scheduleStats.currentFrameRecoveryActiveReclassifiedCount,
                    continuationSafetyTargetCount: scheduleStats.continuationSafetyTargetCount,
                    continuationSafetyAdmitted: scheduleStats.continuationSafetyAdmitted,
                    continuationSafetySuppressed: scheduleStats.continuationSafetySuppressed,
                    currentSafetyAdmissionBlocked: scheduleStats.currentSafetyAdmissionBlocked,
                    upcomingTargetCount: scheduleStats.upcomingTargetCount,
                    rebufferNextBoundaryTargetCount: scheduleStats.rebufferNextBoundaryTargetCount,
                    deferredNextBoundaryTargetCount: scheduleStats.deferredNextBoundaryTargetCount,
                    deferredNextBoundaryRetainedCount: scheduleStats.deferredNextBoundaryRetainedCount,
                    deferredNextBoundaryDroppedCount: scheduleStats.deferredNextBoundaryDroppedCount,
                    prebufferTargetCount: scheduleStats.prebufferTargetCount,
                    handoffActive: !!scheduleStats.handoffActive,
                    handoffPruneReason: scheduleStats.handoffPruneReason || "",
                    handoffContinuationSuppressed: state.playbackHandoffContinuationSuppressed,
                    handoffUpcomingSuppressed: state.playbackHandoffUpcomingSuppressed,
                    handoffNextBoundaryDelayed: state.playbackHandoffNextBoundaryDelayed,
                    handoffQueuedLowPruned: state.playbackHandoffQueuedLowPruned,
                    handoffQueuedUrgentPruned: state.playbackHandoffQueuedUrgentPruned,
                    deadlineQueuedUrgentStalePruned: state.playbackDeadlineQueuedUrgentStalePruned,
                    nonDeadlineQueuedUrgentStalePruned: state.playbackNonDeadlineQueuedUrgentStalePruned,
                    scheduleDeadlineQueuedUrgentStalePruned: staleUrgentPruned.deadline,
                    scheduleNonDeadlineQueuedUrgentStalePruned: staleUrgentPruned.nonDeadline,
                    rawLayerSignature: scheduleStats.rawLayerSignature,
                    requiredLayerSignature: scheduleStats.requiredLayerSignature,
                    prebufferTargetSignature: scheduleStats.targetSignature,
                    retainedSafetyEntryCount: state.playbackRebufferSafetyRetainedCount,
                    discardedSafetyEntryCount: state.playbackRebufferSafetyDiscardedCount,
                    suppressedSinceLast,
                    playbackSessionId: state.playbackSessionId,
                });
            } else {
                state.playbackRebufferLimitedSuppressed += 1;
            }
        }
        const desiredKeys = new Set([
            ...requiredCurrentLayers
                .map((layer) => prebufferKeyForLayer(layer, snapshot.frame))
                .filter(Boolean),
            ...targets.map(({ key }) => key).filter(Boolean),
            ...targets.map(({ currentFrameRecoveryPendingExistingKey }) => currentFrameRecoveryPendingExistingKey).filter(Boolean),
            ...deferredNextBoundaryTargets.map(({ key }) => key).filter(Boolean),
            ...(!rebufferLimited ? (state.playbackDeferredNextBoundaryTargets || []) : [])
                .map((target) => target?.key)
                .filter(Boolean),
        ]);
        const preservingSuppressedHandoffWork = !rebufferLimited && currentHandoff.active && shouldSuppressFuture;
        for (const [key, entry] of Array.from(state.prebufferCache.entries())) {
            if (rebufferLimited) {
                const action = _classifyRebufferPrebufferEntry({
                    desired: desiredKeys.has(key),
                    claimed: !!entry?.claimedByActive,
                    waiting: playbackWaitingForPrebufferEntry(entry),
                    ready: !!entry?.ready,
                    valid: !!entry
                        && !entry.cancelled
                        && !entry.consumed
                        && entry.warmToken === state.playbackWarmContentToken,
                });
                if (action === "preserve" || action === "preserve-ready") continue;
                state.prebufferCache.delete(key);
                if (action === "cancel") {
                    cancelQueuedPrebufferEntry(entry, "rebuffer-non-desired");
                    discardPrebufferEntry(entry);
                }
                continue;
            }
            if (desiredKeys.has(key)) continue;
            if (entry?.claimedByActive) {
                state.prebufferCache.delete(key);
                continue;
            }
            if (playbackWaitingForPrebufferEntry(entry)) continue;
            if (preservingSuppressedHandoffWork && shouldRetainNonDesiredPrebufferDuringHandoff(entry, key)) continue;
            state.prebufferCache.delete(key);
            discardPrebufferEntry(entry);
        }
        for (const target of targets) {
            ensurePrebufferedLayer(target.layer, target.targetFrame, { ...target, snapshot });
        }
    }

    async function loadGuideLayer(snapshot) {
        if (!snapshot?.guide || !snapshot.guideAsset || snapshot.guideAsset.missing) return null;
        const cacheKey = `guide:${snapshot.guide.asset_id}`;
        const src = buildViewUrl(snapshot.guideAsset.path);
        return await loadImage(cacheKey, src);
    }

    async function renderStaticComposite(snapshot, renderToken) {
        const sources = [];
        const guideImage = await loadGuideLayer(snapshot);
        if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
        if (guideImage) {
            sources.push({ element: guideImage, opacity: 1, ...fitOptionsFor(snapshot.guide) });
        }
        for (const layer of snapshot.playableClipLayers) {
            const src = resolvePreviewImageUrl(layer);
            if (!src) continue;
            const cacheKey = `preview:${layer.asset?.asset_id || layer.key}`;
            const img = await loadImage(cacheKey, src);
            if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
            if (img) {
                sources.push({ element: img, opacity: layer.opacity, ...fitOptionsFor(layer.clip) });
            }
        }
        drawBlack();
        let drewAny = false;
        for (const source of sources) {
            if (drawImageLike(source.element, { opacity: source.opacity, fitMode: source.fitMode, cropPosition: source.cropPosition })) {
                drewAny = true;
            }
        }
        if (!drewAny) {
            drawViewportText("Preview unavailable", "Click Load Preview for live media.");
        }
        drawSceneOutline();
    }

    function clipSourceTime(layer, frame) {
        return sourceFrameTime(clipSourceFrame(layer, frame));
    }

    function clipSourceFrame(layer, frame) {
        const clip = layer?.clip || {};
        return Math.max(0,
            (Math.round(Number(frame) || 0))
            - (Math.round(Number(clip.timeline_start_frame) || 0))
            + (Math.round(Number(clip.source_in_frame) || 0))
        );
    }

    function isPlaybackTailFrame(layer, frame) {
        const clip = layer?.clip;
        if (!clip) return false;
        const timelineEnd = Number(clip.timeline_end_frame);
        const sourceOut = Number(clip.source_out_frame);
        const sourceFrame = clipSourceFrame(layer, frame);
        return (
            (Number.isFinite(timelineEnd) && frame >= timelineEnd - 1)
            || (Number.isFinite(sourceOut) && sourceFrame >= sourceOut - 1)
        );
    }

    function nextPlaybackFrameAfter(frame) {
        const endFrame = state.playbackLoopRange ? state.playbackLoopRange.end : totalFrames();
        return playbackSearchFrame(Math.max(0, Math.round(Number(frame) || 0) + 1), 0, endFrame);
    }

    function readyPrebufferEntryForLayerFrame(layer, frame) {
        const match = prebufferCandidatesForLayerFrame(layer, frame)
            .find((candidate) => candidate.readyClaimable);
        return match?.entry || null;
    }

    function hasReadyNextPlaybackVideo(frame) {
        const nextFrame = nextPlaybackFrameAfter(frame);
        if (nextFrame === null) return false;
        const nextSnapshot = buildFrameSnapshot(nextFrame);
        for (const layer of requiredClipLayersAfterCoverage(nextSnapshot)) {
            if (!isRenderableVideoLayer(layer) || clamp(Number(layer.opacity ?? 1), 0, 1) <= 0) continue;
            const active = state.activePlaybackVideos.get(layer.key);
            if (isActiveVideoDrawable(active, layer, nextFrame)) return true;
            if (prebufferCandidatesForLayerFrame(layer, nextFrame, { snapshot: nextSnapshot })
                .some((candidate) => candidate.readyClaimable)) return true;
        }
        return false;
    }

    function readyPlaybackRenderableForLayer(layer, frame) {
        const opacity = clamp(Number(layer?.opacity ?? 1), 0, 1);
        if (!layer?.asset || opacity <= 0) return null;
        if (layer.asset.asset_type === "image") {
            const src = buildViewUrl(layer.asset.path || layer.clip?.source_path || "");
            const image = getReadyImage(`live-image:${layer.asset.asset_id || layer.key}`, src, { rerenderOnLoad: true });
            return image ? { type: "image", element: image, opacity, layer } : null;
        }
        const active = state.activePlaybackVideos.get(layer.key);
        if (!isActiveVideoDrawable(active, layer, frame)) return null;
        return { type: "video", element: active.video, opacity, layer, active };
    }

    function layerCoverageElement(layer, frame) {
        if (!layer?.asset) return null;
        if (layer.asset.asset_type === "image") {
            const src = buildViewUrl(layer.asset.path || layer.clip?.source_path || "");
            const image = getReadyImage(`live-image:${layer.asset.asset_id || layer.key}`, src, { rerenderOnLoad: true });
            if (image) return image;
        } else {
            const active = state.activePlaybackVideos.get(layer.key);
            if (active?.video && imageLikeDimensions(active.video)) return active.video;
            for (const candidate of prebufferCandidatesForLayerFrame(layer, frame)) {
                if (candidate?.entry?.video && imageLikeDimensions(candidate.entry.video)) return candidate.entry.video;
            }
        }
        const assetWidth = Number(layer.asset.width);
        const assetHeight = Number(layer.asset.height);
        if (Number.isFinite(assetWidth) && Number.isFinite(assetHeight) && assetWidth > 0 && assetHeight > 0) {
            return { width: assetWidth, height: assetHeight };
        }
        return null;
    }

    function layerCoversCanvasForPlayback(layer, frame) {
        const opacity = clamp(Number(layer?.opacity ?? 1), 0, 1);
        if (opacity <= 0) return false;
        return imageLikeCoversCanvas(layerCoverageElement(layer, frame), opacity, fitOptionsFor(layer?.clip));
    }

    function requiredClipLayersAfterCoverage(snapshot) {
        const requiredTopDown = [];
        let coveredByUpper = false;
        for (const layer of [...(snapshot?.playableClipLayers || [])].reverse()) {
            const opacity = clamp(Number(layer?.opacity ?? 1), 0, 1);
            if (opacity <= 0) continue;
            if (coveredByUpper) continue;
            requiredTopDown.push(layer);
            if (layerCoversCanvasForPlayback(layer, snapshot.frame)) {
                coveredByUpper = true;
            }
        }
        return requiredTopDown;
    }

    function playbackRenderableFitOptions(renderable) {
        if (renderable?.guide) return fitOptionsFor(renderable.guide);
        return fitOptionsFor(renderable?.layer?.clip);
    }

    function playbackRenderableCoversCanvas(renderable) {
        return imageLikeCoversCanvas(renderable?.element, renderable?.opacity, playbackRenderableFitOptions(renderable));
    }

    function playbackLayerDebug(layer, reason, extra = {}) {
        return {
            reason,
            layerKey: layer?.key || "",
            sourcePath: layer?.clip?.source_path || "",
            opacity: clamp(Number(layer?.opacity ?? 1), 0, 1),
            ...extra,
        };
    }

    function isLayerCoveredByDrawableUpperLayer(layer, snapshot) {
        const layers = snapshot?.playableClipLayers || [];
        const index = layers.indexOf(layer);
        if (index < 0) return false;
        for (let i = layers.length - 1; i > index; i -= 1) {
            const renderable = readyPlaybackRenderableForLayer(layers[i], snapshot.frame);
            if (renderable && playbackRenderableCoversCanvas(renderable)) return true;
        }
        return false;
    }

    function shouldDeferPlaybackTailPrepare(active, layer, snapshot) {
        if (!active?.firstDrawComplete || !isPlaybackTailFrame(layer, snapshot.frame) || !playbackCanvasStillValid()) {
            return false;
        }
        return isLayerCoveredByDrawableUpperLayer(layer, snapshot) || hasReadyNextPlaybackVideo(snapshot.frame);
    }

    function audioSourceTime(layer, frame) {
        const sourceFrame = frame - layer.track.timeline_start_frame + (layer.track.source_in_frame || 0);
        return Math.max(0, (sourceFrame + 0.5) / fps());
    }

    function playMediaElement(mediaEl, context = {}) {
        if (!mediaEl || typeof mediaEl.play !== "function") return;
        try {
            const promise = mediaEl.play();
            if (promise && typeof promise.catch === "function") {
                promise.catch((error) => {
                    if (error?.name === "AbortError") return;
                    recordPlaybackTelemetry("playback_media_play_rejected", {
                        mediaType: context.mediaType || "",
                        layerKey: context.layerKey || "",
                        sourcePath: context.sourcePath || "",
                        errorName: error?.name || "",
                        error: String(error?.message || error || ""),
                        playbackSessionId: state.playbackSessionId,
                    });
                });
            }
        } catch (error) {
            if (error?.name === "AbortError") return;
            recordPlaybackTelemetry("playback_media_play_rejected", {
                mediaType: context.mediaType || "",
                layerKey: context.layerKey || "",
                sourcePath: context.sourcePath || "",
                errorName: error?.name || "",
                error: String(error?.message || error || ""),
                playbackSessionId: state.playbackSessionId,
            });
        }
    }

    function createActivePlaybackVideoEntry(layer, video, prebufferClaim = null) {
        return {
            layer,
            video,
            sourcePath: layer.clip.source_path,
            layerKey: layer.key,
            prepareToken: 0,
            requestedFrame: null,
            readyForDraw: false,
            firstDrawComplete: false,
            claimedPrebufferKey: prebufferClaim?.key || "",
            claimedPrebufferEntry: prebufferClaim?.entry || null,
            pendingPrebufferKey: "",
            pendingPrebufferEntry: null,
            pendingPrebufferSourceTargetKey: "",
            pendingPrebufferTargetSourceFrame: null,
            pendingPrebufferFrame: null,
            pendingPrebufferWaitKey: "",
            pendingPrebufferStartedAt: null,
            pendingPrepare: null,
            playbackSessionId: state.playbackSessionId,
        };
    }

    function clearActivePendingPrebuffer(active) {
        if (!active) return;
        active.pendingPrebufferKey = "";
        active.pendingPrebufferEntry = null;
        active.pendingPrebufferSourceTargetKey = "";
        active.pendingPrebufferTargetSourceFrame = null;
        active.pendingPrebufferFrame = null;
        active.pendingPrebufferWaitKey = "";
        active.pendingPrebufferStartedAt = null;
    }

    function playbackWaitingForPrebufferEntry(entry) {
        if (!entry) return false;
        for (const active of state.activePlaybackVideos.values()) {
            if (active?.pendingPrebufferEntry === entry) return true;
            if (entry.key && active?.pendingPrebufferKey === entry.key) return true;
        }
        return false;
    }

    function pendingPrebufferDetails(candidate) {
        const entry = candidate?.entry;
        return {
            pendingPrebufferKey: candidate?.key || entry?.key || "",
            pendingPrebufferEntryLayerKey: entry?.layerKey || "",
            pendingPrebufferSourceTargetKey: entry?.sourceTargetKey || "",
            pendingPrebufferTargetFrame: entry?.targetFrame ?? null,
            pendingPrebufferEffectiveTargetFrame: entry ? prebufferEntryEffectiveTargetFrame(entry) : null,
            pendingPrebufferCurrentFrameRecoveryTargetFrame: entry?.currentFrameRecoveryTargetFrame ?? null,
            pendingPrebufferTargetSourceFrame: entry?.targetSourceFrame ?? null,
            pendingPrebufferExact: !!candidate?.pendingExact,
            pendingPrebufferSource: !!candidate?.pendingSource,
            pendingPrebufferReadyState: entry?.video?.readyState || 0,
            pendingPrebufferSeeking: !!entry?.video?.seeking,
        };
    }

    function markActiveWaitingForPrebuffer(active, layer, frame, candidate) {
        if (!active || !layer || !candidate?.entry) return false;
        const reclassification = reclassifyPendingCandidateForCurrentFrame(candidate, layer, frame, "pending-hold");
        const entry = candidate.entry;
        const sourcePath = layer.clip?.source_path || "";
        const sourceChanged = active.layerKey !== layer.key || active.sourcePath !== sourcePath;
        const hadPendingPrepare = !!active.pendingPrepare;
        if (hadPendingPrepare) {
            active.prepareToken = ++state.playbackPrepareToken;
            active.pendingPrepare = null;
        }
        active.layer = layer;
        active.layerKey = layer.key;
        active.sourcePath = sourcePath;
        active.requestedFrame = frame;
        active.readyForDraw = false;
        if (sourceChanged) active.firstDrawComplete = false;
        active.playbackSessionId = state.playbackSessionId;
        if (active.pendingPrebufferKey !== candidate.key) {
            active.pendingPrebufferStartedAt = performance.now();
        }
        active.pendingPrebufferKey = candidate.key;
        active.pendingPrebufferEntry = entry;
        active.pendingPrebufferSourceTargetKey = entry.sourceTargetKey || "";
        active.pendingPrebufferTargetSourceFrame = entry.targetSourceFrame ?? null;
        active.pendingPrebufferFrame = frame;

        const waitKey = [
            state.playbackSessionId,
            layer.key,
            Math.round(Number(frame) || 0),
            candidate.key,
        ].join("|");
        if (active.pendingPrebufferWaitKey !== waitKey) {
            active.pendingPrebufferWaitKey = waitKey;
            state.prebufferPendingHoldCount += 1;
            const target = prebufferTargetForLayer(layer, frame);
            const candidateTelemetry = prebufferCandidateTelemetry(candidate, target);
            const currentFrameRecoveryCandidate = candidateWouldBeCurrentFrameRecovery(candidate, layer, frame, target);
            recordPlaybackTelemetry("playback_prebuffer_pending_hold", {
                requestedLayerKey: layer.key,
                sourcePath,
                frame,
                targetFrame: entry.targetFrame,
                targetSourceFrame: entry.targetSourceFrame,
                sourceTargetKey: entry.sourceTargetKey || "",
                candidateKey: candidate.key,
                entryLayerKey: entry.layerKey || "",
                exactLayerMatch: !!candidate.exactLayerMatch,
                pendingExact: !!candidate.pendingExact,
                pendingSource: !!candidate.pendingSource,
                readyState: entry.video?.readyState || 0,
                seeking: !!entry.video?.seeking,
                entryReady: !!entry.ready,
                currentTime: candidateTelemetry.currentTime,
                targetTime: candidateTelemetry.targetTime,
                atTarget: candidateTelemetry.atTarget,
                decodeJobState: entry.decodeJobState || "",
                decodeJobPriority: entry.decodeJobPriority || entry.decodePriority || "",
                decodeJobDeadlineClass: entry.decodeJobDeadlineClass || entry.deadlineClass || "",
                decodeJobQueueClass: entry.decodeJobQueueClass || "",
                decodeJobQueuePosition: entry.decodeJobQueuePosition ?? null,
                decodeJobQueueDepth: entry.decodeJobQueueDepth ?? null,
                scheduleOrigin: entry.scheduleOrigin || "",
                intent: entry.intent || "",
                pendingAgeMs: candidateTelemetry.pendingAgeMs,
                currentFrameRecoveryCandidate,
                currentFrameReclassified: !!reclassification.reclassified,
                currentFrameReclassificationSource: reclassification.source || "",
                currentFrameReclassificationPromoteResult: reclassification.promoteResult || "",
                currentFrameReclassificationJobDeadlineMutated: !!reclassification.jobDeadlineMutated,
                originalScheduleOrigin: reclassification.originalScheduleOrigin || entry.currentFrameReclassifiedFromScheduleOrigin || "",
                originalDeadlineClass: reclassification.originalDeadlineClass || entry.currentFrameReclassifiedFromDeadlineClass || "",
                originalDecodeJobDeadlineClass: reclassification.originalDecodeJobDeadlineClass || entry.currentFrameReclassifiedFromDecodeJobDeadlineClass || "",
                pendingBlockReason: pendingPrebufferBlockReason(entry, target?.targetTime),
                readyPublishedFrom: entry.readyPublishedFrom || "",
                cancelled: !!entry.cancelled,
                hadPendingPrepare,
                ...prebufferDeadlineTelemetryFromEntry(entry),
                ...playbackDecodeLimiter.snapshotStats(),
                playbackSessionId: state.playbackSessionId,
            });
        }
        notePlaybackWarmLayer(layer, frame, "warming", "prebuffer-pending", {
            owner: "prebuffer",
            ownerKey: candidate.key,
            token: entry.warmToken,
        });
        return true;
    }

    function expireActivePendingPrebuffer(active, fallbackFrame, snapshot = null, reason = "pending-expired") {
        if (!active?.pendingPrebufferEntry || !active.layer) return false;
        const layer = active.layer;
        const frame = Math.round(Number(active.pendingPrebufferFrame ?? fallbackFrame ?? snapshot?.frame) || 0);
        const entry = active.pendingPrebufferEntry;
        const pendingKey = active.pendingPrebufferKey || entry.key || "";
        const candidates = prebufferCandidatesForLayerFrame(layer, frame, { snapshot });
        const readyCandidate = candidates.find((candidate) => (
            candidate.readyClaimable
            && (
                candidate.entry === entry
                || candidate.key === pendingKey
                || (
                    active.pendingPrebufferSourceTargetKey
                    && candidate.entry?.sourceTargetKey === active.pendingPrebufferSourceTargetKey
                )
            )
        )) || candidates.find((candidate) => candidate.readyClaimable);
        if (readyCandidate?.entry?.video) {
            claimPrebufferedVideo(layer, frame, snapshot, active, { recordMiss: false });
            prepareActivePlaybackVideo(active, layer, frame);
            return true;
        }

        markPendingPrebufferSkipped(layer, frame, entry, reason);
        clearActivePendingPrebuffer(active);
        if (
            state.prebufferCache.get(pendingKey) === entry
            && !playbackWaitingForPrebufferEntry(entry)
            && !prebufferEntryReadyAtTarget(entry, layer, frame)
        ) {
            state.prebufferCache.delete(pendingKey);
            discardPrebufferEntry(entry);
        }
        prepareActivePlaybackVideo(active, layer, frame, { force: true });
        return true;
    }

    function expireStalePendingPrebuffers(snapshot, now, reason = "pending-expired") {
        const maxWaitMs = Math.max(250, Number(getRebufferMaxMs()) || 0);
        let expired = 0;
        for (const active of state.activePlaybackVideos.values()) {
            if (!active?.pendingPrebufferEntry || active.pendingPrebufferStartedAt === null) continue;
            const waitedMs = now - active.pendingPrebufferStartedAt;
            if (waitedMs < maxWaitMs && !state.playbackRebufferCapped) continue;
            if (expireActivePendingPrebuffer(active, snapshot?.frame, snapshot, reason)) {
                expired += 1;
            }
        }
        return expired;
    }

    function activeHasMatchingPrepare(active, layer, frame) {
        if (!active?.pendingPrepare || !layer?.clip?.source_path) return false;
        return (
            active.requestedFrame === frame
            && active.layerKey === layer.key
            && active.sourcePath === layer.clip.source_path
            && active.playbackSessionId === state.playbackSessionId
        );
    }

    function playbackVideoAtFrame(active, layer, frame, tolerance) {
        if (!active?.video || !layer?.clip) return false;
        if (active.layerKey !== layer.key || active.sourcePath !== layer.clip.source_path) return false;
        if (active.video.seeking || (active.video.readyState || 0) < 2) return false;
        return isMediaAtTarget(
            active.video,
            clampMediaTargetTime(active.video, clipSourceTime(layer, frame)),
            tolerance,
        );
    }

    function isActiveVideoDrawable(active, layer, frame) {
        if (!active?.readyForDraw) return false;
        const tolerance = active.firstDrawComplete ? playbackDriftTolerance() : firstDrawTolerance();
        return playbackVideoAtFrame(active, layer, frame, tolerance);
    }

    function syncPreparedVideoPlayback(active, layer, frame) {
        if (!active?.video) return;
        active.video.muted = true;
        if (state.playbackRebuffering) {
            active.video.pause();
            return;
        }
        if (layer && isPlaybackTailFrame(layer, frame)) {
            active.video.pause();
            return;
        }
        if (active.video.paused) {
            playMediaElement(active.video, { mediaType: "video", layerKey: layer?.key || "", sourcePath: layer?.clip?.source_path || "" });
        }
    }

    function pauseTailPlaybackVideo(active, layer, frame) {
        if (!active?.video || !layer || !isPlaybackTailFrame(layer, frame)) return;
        active.video.pause();
    }

    function pauseActivePlaybackVideos() {
        for (const active of state.activePlaybackVideos.values()) {
            try {
                active?.video?.pause?.();
            } catch (error) {}
        }
    }

    function prepareActivePlaybackVideo(active, layer, frame, { force = false } = {}) {
        if (!active?.video || !layer?.clip?.source_path) return Promise.resolve(null);
        const sourcePath = layer.clip.source_path;
        const expectedTime = clipSourceTime(layer, frame);
        const targetTolerance = firstDrawTolerance();
        const sessionId = state.playbackSessionId;
        const warmToken = state.playbackWarmContentToken;
        const existingAtTarget = playbackVideoAtFrame(active, layer, frame, targetTolerance);
        if (!force && existingAtTarget) {
            if (active.pendingPrepare) {
                active.prepareToken = ++state.playbackPrepareToken;
                active.pendingPrepare = null;
            }
            active.layer = layer;
            active.layerKey = layer.key;
            active.sourcePath = sourcePath;
            active.requestedFrame = frame;
            active.readyForDraw = true;
            active.playbackSessionId = sessionId;
            clearActivePendingPrebuffer(active);
            notePlaybackWarmLayer(layer, frame, "warm", "prepare-reused", {
                owner: "active",
                ownerKey: layer.key,
                token: warmToken,
            });
            recordPlaybackTelemetry("playback_prepare_reused", {
                layerKey: layer.key,
                sourcePath,
                frame,
                targetSourceFrame: clipSourceFrame(layer, frame),
                sourceTargetKey: prebufferSourceTargetKey(layer, frame),
                expectedTime,
                claimedPrebufferKey: active.claimedPrebufferKey || "",
                claimedPrebufferSourceTargetKey: active.claimedPrebufferEntry?.sourceTargetKey || "",
                ...videoElementDimensions(active.video),
                readyState: active.video?.readyState || 0,
                playbackSessionId: sessionId,
            });
            return Promise.resolve(active.video);
        }
        if (
            active.pendingPrepare
            && active.requestedFrame === frame
            && active.layerKey === layer.key
            && active.sourcePath === sourcePath
            && active.playbackSessionId === state.playbackSessionId
        ) {
            return active.pendingPrepare;
        }
        const token = ++state.playbackPrepareToken;
        // Cold-start timing: end-to-end prepare (source-resolve + seek + decode)
        // until first drawable frame. The blob-fetch portion is attributable
        // separately via the resolve_media_source diag record.
        const prepareStartedAt = performance.now();
        const prepareWasWarm = !!active.firstDrawComplete;
        active.claimedPrebufferKey = "";
        active.claimedPrebufferEntry = null;
        clearActivePendingPrebuffer(active);
        active.layer = layer;
        active.layerKey = layer.key;
        active.sourcePath = sourcePath;
        active.prepareToken = token;
        active.requestedFrame = frame;
        active.readyForDraw = false;
        active.playbackSessionId = sessionId;
        notePlaybackWarmLayer(layer, frame, "warming", "prepare-start", {
            owner: "active",
            ownerKey: layer.key,
            token: warmToken,
        });

        playbackDecisionDebugEvent("prepare-start", {
            layerKey: layer.key,
            sourcePath,
            frame,
            expectedTime,
            firstDrawComplete: !!active.firstDrawComplete,
            playbackSessionId: sessionId,
        }, [layer.key, sourcePath, active.firstDrawComplete ? "warm" : "first"]);
        const prepareStillCurrent = () => (
            state.isPlaying
            && !state.destroyed
            && state.playbackSessionId === sessionId
            && state.activePlaybackVideos.get(layer.key) === active
            && active.prepareToken === token
            && active.layerKey === layer.key
            && active.sourcePath === sourcePath
        );
        active.pendingPrepare = playbackDecodeLimiter.run(DECODE_PRIORITY_HIGH, () => (
            ensureMediaElementSource(active.video, sourcePath)
                .then((element) => waitForMediaReady(element, 2))
                .then((element) => seekMedia(element, expectedTime, {
                    tolerance: targetTolerance,
                    timeoutMs: active.firstDrawComplete ? 300 : 700,
                    requireTarget: true,
                    waitForFrame: true,
                }))
        ), {
            shouldRun: prepareStillCurrent,
        })
            .then((element) => {
                const stillActive = prepareStillCurrent();
                if (!stillActive) {
                    if (state.activePlaybackVideos.get(layer.key) === active && active.prepareToken === token) {
                        active.pendingPrepare = null;
                    }
                    return null;
                }
                active.pendingPrepare = null;
                active.readyForDraw = !!element && playbackVideoAtFrame(active, layer, frame, targetTolerance);
                if (active.readyForDraw) {
                    playbackDecisionDebugEvent("prepare-ready", {
                        layerKey: layer.key,
                        sourcePath,
                        frame,
                        expectedTime,
                        currentTime: Number(active.video?.currentTime) || 0,
                        readyState: active.video?.readyState || 0,
                        playbackSessionId: sessionId,
                    }, [layer.key, sourcePath]);
                    if (playbackTelemetryActive()) {
                        recordPlaybackTelemetry("playback_clip_coldstart", {
                            layerKey: layer.key,
                            sourcePath,
                            frame,
                            targetSourceFrame: clipSourceFrame(layer, frame),
                            sourceTargetKey: prebufferSourceTargetKey(layer, frame),
                            expectedTime,
                            prepareMs: Math.round(performance.now() - prepareStartedAt),
                            warm: prepareWasWarm,
                            claimedPrebufferKey: active.claimedPrebufferKey || "",
                            claimedPrebufferSourceTargetKey: active.claimedPrebufferEntry?.sourceTargetKey || "",
                            ...videoElementDimensions(active.video),
                            readyState: active.video?.readyState || 0,
                            playbackSessionId: sessionId,
                        });
                    }
                    notePlaybackWarmLayer(layer, frame, "warm", "prepare-ready", {
                        owner: "active",
                        ownerKey: layer.key,
                        token: warmToken,
                    });
                    renderFrame();
                    return active.video;
                }
                playbackDecisionDebugEvent("prepare-timeout-null", {
                    layerKey: layer.key,
                    sourcePath,
                    frame,
                    expectedTime,
                    currentTime: Number(active.video?.currentTime) || 0,
                    readyState: active.video?.readyState || 0,
                    seeking: !!active.video?.seeking,
                    playbackSessionId: sessionId,
                }, [layer.key, sourcePath, active.firstDrawComplete ? "warm" : "first"]);
                notePlaybackWarmLayer(layer, frame, "blocked", "prepare-timeout", {
                    owner: "active",
                    ownerKey: layer.key,
                    token: warmToken,
                });
                return null;
            })
            .catch(() => {
                const stillCurrent = prepareStillCurrent();
                if (stillCurrent) {
                    active.pendingPrepare = null;
                    active.readyForDraw = false;
                    notePlaybackWarmLayer(layer, frame, "blocked", "prepare-error", {
                        owner: "active",
                        ownerKey: layer.key,
                        token: warmToken,
                    });
                }
                return null;
            });
        return active.pendingPrepare;
    }

    function playbackLayerSignature(snapshot) {
        const parts = [];
        if (snapshot?.guide && snapshot.guideAsset && !snapshot.guideAsset.missing) {
            parts.push([
                "guide",
                snapshot.guide.frame_index ?? "",
                snapshot.guide.asset_id || "",
                snapshot.guideAsset.asset_id || "",
                snapshot.guideAsset.path || "",
                snapshot.guide.fit_mode || "",
                snapshot.guide.crop_position || "",
            ].join(":"));
        } else {
            parts.push("guide:none");
        }
        for (const [index, layer] of (snapshot?.playableClipLayers || []).entries()) {
            if ((Number(layer.opacity ?? 1) || 0) <= 0) continue;
            const clip = layer.clip || {};
            const asset = layer.asset || {};
            parts.push([
                "layer",
                index,
                asset.asset_id || "",
                asset.asset_type || "unknown",
                layer.key || "",
                clip.source_path || "",
                clamp(Number(layer.opacity ?? 1), 0, 1),
                clip.timeline_start_frame ?? "",
                clip.timeline_end_frame ?? "",
                clip.source_in_frame ?? "",
                clip.source_out_frame ?? "",
                clip.role || "",
                clip.track_index ?? "",
                clip.fit_mode || "",
                clip.crop_position || "",
            ].join(":"));
        }
        return parts.join("|");
    }

    function shouldReuseCommittedPlaybackFrame(snapshot) {
        if (!state.isPlaying || !playbackCanvasStillValid()) return false;
        if (state.playbackRebuffering) return false;
        if (state.playbackLastCommittedFrame !== snapshot.frame) return false;
        if (state.playbackLastCommittedSessionId !== state.playbackSessionId) return false;
        const signature = playbackLayerSignature(snapshot);
        if (state.playbackLastCommittedSignature !== signature) return false;
        debugPlaybackBoundary("reuse-committed-playback-frame", {
            frame: snapshot.frame,
            signature,
            storedSignature: state.playbackLastCommittedSignature,
            playbackSessionId: state.playbackSessionId,
            canvasWidth: state.canvas?.width || 0,
            canvasHeight: state.canvas?.height || 0,
        });
        return true;
    }

    function playbackBlockDetails(reason, layer, snapshot, active = null, extra = {}) {
        const expectedTime = layer?.clip ? clipSourceTime(layer, snapshot.frame) : null;
        return {
            reason,
            frame: snapshot.frame,
            layerKey: layer?.key || "",
            sourcePath: layer?.clip?.source_path || "",
            expectedTime,
            currentTime: active?.video ? Number(active.video.currentTime) || 0 : null,
            readyState: active?.video ? active.video.readyState || 0 : null,
            seeking: !!active?.video?.seeking,
            readyForDraw: !!active?.readyForDraw,
            prebufferKey: layer ? prebufferKeyForLayer(layer, snapshot.frame) : "",
            sourceTargetKey: layer ? prebufferSourceTargetKey(layer, snapshot.frame) : "",
            targetSourceFrame: layer ? clipSourceFrame(layer, snapshot.frame) : null,
            claimedPrebufferKey: active?.claimedPrebufferKey || "",
            claimedPrebufferSourceTargetKey: active?.claimedPrebufferEntry?.sourceTargetKey || "",
            pendingPrebufferKey: active?.pendingPrebufferKey || "",
            pendingPrebufferSourceTargetKey: active?.pendingPrebufferSourceTargetKey || "",
            pendingPrebufferFrame: active?.pendingPrebufferFrame ?? null,
            prebufferReady: !!active?.claimedPrebufferEntry?.ready,
            ...extra,
        };
    }

    function playbackGuideRenderable(snapshot) {
        if (!snapshot?.guide || !snapshot.guideAsset || snapshot.guideAsset.missing) return { element: null };
        const cacheKey = `guide:${snapshot.guide.asset_id}`;
        const src = buildViewUrl(snapshot.guideAsset.path);
        const image = getReadyImage(cacheKey, src, { rerenderOnLoad: true });
        if (!image) {
            return { blocked: true, details: playbackBlockDetails("guide-image-loading", null, snapshot) };
        }
        return { element: image };
    }

    function preflightPlaybackComposite(snapshot) {
        const renderablesByKey = new Map();
        const skippedLayers = [];
        const topDownLayers = [...(snapshot.playableClipLayers || [])].reverse();
        let coveredByUpper = false;
        let coveringLayer = null;

        for (const layer of topDownLayers) {
            const opacity = clamp(Number(layer.opacity ?? 1), 0, 1);
            if (opacity <= 0) {
                skippedLayers.push(playbackLayerDebug(layer, "transparent"));
                continue;
            }
            if (coveredByUpper) {
                skippedLayers.push(playbackLayerDebug(layer, "covered-by-upper", {
                    coveringLayerKey: coveringLayer?.layer?.key || "",
                    coveringSourcePath: coveringLayer?.layer?.clip?.source_path || "",
                }));
                continue;
            }

            let renderable = null;
            if (layer.asset?.asset_type === "image") {
                const src = buildViewUrl(layer.asset.path || layer.clip.source_path || "");
                const image = getReadyImage(`live-image:${layer.asset.asset_id || layer.key}`, src, { rerenderOnLoad: true });
                if (!image) {
                    return {
                        blocked: true,
                        details: playbackBlockDetails("image-loading", layer, snapshot, null, { skippedLayers }),
                    };
                }
                renderable = { type: "image", element: image, opacity, layer };
            } else {
                const active = state.activePlaybackVideos.get(layer.key);
                if (!isActiveVideoDrawable(active, layer, snapshot.frame)) {
                    const pendingCandidate = pendingPrebufferForLayerFrame(layer, snapshot.frame, snapshot);
                    if (pendingCandidate && !activeHasMatchingPrepare(active, layer, snapshot.frame)) {
                        if (active) markActiveWaitingForPrebuffer(active, layer, snapshot.frame, pendingCandidate);
                        return {
                            blocked: true,
                            suppressFallback: true,
                            details: playbackBlockDetails("video-prebuffer-pending", layer, snapshot, active, {
                                skippedLayers,
                                ...pendingPrebufferDetails(pendingCandidate),
                            }),
                        };
                    }
                    if (
                        active?.firstDrawComplete
                        && isPlaybackTailFrame(layer, snapshot.frame)
                        && playbackCanvasStillValid()
                        && hasReadyNextPlaybackVideo(snapshot.frame)
                    ) {
                        return {
                            blocked: true,
                            suppressFallback: true,
                            details: playbackBlockDetails("tail-frame-hold", layer, snapshot, active, {
                                skippedLayers,
                                nextPlaybackFrame: nextPlaybackFrameAfter(snapshot.frame),
                                nextFramePrebufferReady: true,
                            }),
                        };
                    }
                    if (active) prepareActivePlaybackVideo(active, layer, snapshot.frame);
                    return {
                        blocked: true,
                        details: playbackBlockDetails("video-not-drawable", layer, snapshot, active, { skippedLayers }),
                    };
                }
                renderable = { type: "video", element: active.video, opacity, layer, active };
            }

            renderable.coversCanvas = playbackRenderableCoversCanvas(renderable);
            renderablesByKey.set(layer.key, renderable);
            if (renderable.coversCanvas) {
                coveredByUpper = true;
                coveringLayer = renderable;
            }
        }

        const renderables = [];
        if (!coveredByUpper) {
            const guide = playbackGuideRenderable(snapshot);
            if (guide.blocked) {
                return {
                    blocked: true,
                    details: { ...guide.details, skippedLayers },
                };
            }
            if (guide.element) {
                renderables.push({ type: "guide", element: guide.element, opacity: 1, guide: snapshot.guide });
            }
        } else if (snapshot?.guide && snapshot.guideAsset && !snapshot.guideAsset.missing) {
            skippedLayers.push({
                reason: "guide-covered-by-upper",
                coveringLayerKey: coveringLayer?.layer?.key || "",
                coveringSourcePath: coveringLayer?.layer?.clip?.source_path || "",
            });
        }

        for (const layer of snapshot.playableClipLayers || []) {
            const renderable = renderablesByKey.get(layer.key);
            if (renderable) renderables.push(renderable);
        }
        return { blocked: false, renderables, skippedLayers };
    }

    function hasCurrentLayerPrepareInFlight(snapshot) {
        const requiredLayers = requiredClipLayersAfterCoverage(snapshot);
        const requiredKeys = new Set(requiredLayers.map((layer) => layer.key));
        for (const active of state.activePlaybackVideos.values()) {
            if (active?.layerKey && !requiredKeys.has(active.layerKey)) continue;
            if (active?.pendingPrepare) return true;
        }
        for (const layer of requiredLayers) {
            if (!isRenderableVideoLayer(layer)) continue;
            if (prebufferCandidatesForLayerFrame(layer, snapshot.frame, { snapshot })
                .some((candidate) => candidate.pendingClaimable)) return true;
        }
        return false;
    }

    function commitPlaybackBlocked(snapshot, details, options = {}) {
        const now = performance.now();
        if (playbackTelemetryActive()) {
            noteBlockReasonTelemetry(details?.reason, hasCurrentLayerPrepareInFlight(snapshot), now);
        }
        notePlaybackWarmMissingLayers(snapshot, "missing-layer");
        const blockedLayer = [...(snapshot?.clipLayers || [])].find((layer) => layer.key === details?.layerKey);
        if (blockedLayer) {
            // D6: a layer blocked only because its own prepare/seek is still in flight
            // is recovering, not stalled — tag it `warming` (amber) so the strip does
            // not flicker red on every cold-start. The diag confirms the observed red is
            // `video-not-drawable|inflight`, i.e. this commit site while a prepare runs
            // (preflightPlaybackComposite kicks that prepare just before this commits).
            // Reserve `blocked` (red) for a genuine stall (no prepare in flight); missing
            // media is already tagged blocked above. The hold/rebuffer control flow below
            // is unchanged — this only affects the warm-strip color.
            const blockedActive = state.activePlaybackVideos.get(blockedLayer.key);
            const blockedPrepareInFlight = !!blockedActive?.pendingPrepare
                || prebufferCandidatesForLayerFrame(blockedLayer, snapshot.frame, { snapshot })
                    .some((candidate) => candidate.pendingClaimable);
            notePlaybackWarmLayer(
                blockedLayer,
                snapshot.frame,
                blockedPrepareInFlight ? "warming" : "blocked",
                details?.reason || "blocked",
            );
        }
        const signature = playbackLayerSignature(snapshot);
        const canHoldCanvas = playbackCanvasStillValid();
        const currentRecoveryInFlight = hasCurrentLayerPrepareInFlight(snapshot);
        if (state.playbackBlockedSignature !== signature) {
            state.playbackBlockedSignature = signature;
            state.playbackBlockedSinceMs = canHoldCanvas ? now : null;
        } else if (canHoldCanvas && state.playbackBlockedSinceMs === null) {
            state.playbackBlockedSinceMs = now;
        }

        const pendingPrebufferBlock = details?.reason === "video-prebuffer-pending";
        const immediateActivePrepareBlock = details?.reason === "video-not-drawable" && currentRecoveryInFlight;
        if (pendingPrebufferBlock) {
            expireStalePendingPrebuffers(snapshot, now, "pending-wait-expired");
        }
        if (
            (pendingPrebufferBlock || immediateActivePrepareBlock)
            && isAdaptiveRebufferEnabled()
            && !state.playbackRebuffering
            && canHoldCanvas
            && state.playbackCompositeCommitted
        ) {
            const decision = rebufferEntryDecision(now, details, snapshot);
            if (decision.allowed) enterRebuffer(now, details, decision);
        }

        if (options.suppressFallback && canHoldCanvas) {
            const blockedForMs = state.playbackBlockedSinceMs !== null ? now - state.playbackBlockedSinceMs : 0;
            // Tail-frame suppressFallback only fires when the next video is already
            // ready/covered, so it can hold longer than the generic failure ladder.
            if (blockedForMs < PLAYBACK_TAIL_HOLD_MAX_MS) {
                debugPlaybackBoundary("hold-previous-composite", { ...details, blockedForMs, suppressedFallback: true });
                return false;
            }
            playbackDecisionDebugEvent("tail-hold-expired", { ...details, blockedForMs }, [
                signature,
                details?.reason,
                details?.layerKey,
                details?.sourcePath,
                details?.prebufferKey,
            ]);
        }

        if (canHoldCanvas && state.playbackBlockedSinceMs !== null) {
            const blockedForMs = now - state.playbackBlockedSinceMs;
            if (blockedForMs < PLAYBACK_COMMIT_HOLD_MS) {
                debugPlaybackBoundary("hold-previous-composite", { ...details, blockedForMs });
                return false;
            }
        }

        // Adaptive rebuffer: a transient in-flight prepare has kept us blocked past
        // the enter threshold while a committed frame is on screen → arm the clock
        // freeze (the actual freeze happens at the top of playbackTick). The canvas
        // stays held by the hold-inflight-prepare path just below.
        if (
            isAdaptiveRebufferEnabled()
            && !state.playbackRebuffering
            && canHoldCanvas
            && state.playbackCompositeCommitted
            && state.playbackBlockedSinceMs !== null
            && (now - state.playbackBlockedSinceMs) >= getRebufferEnterMs()
            && currentRecoveryInFlight
        ) {
            const decision = rebufferEntryDecision(now, details, snapshot);
            if (decision.allowed) enterRebuffer(now, details, decision);
        }

        if (canHoldCanvas && currentRecoveryInFlight) {
            playbackDecisionDebugEvent("hold-inflight-prepare", { ...details }, [
                signature,
                details?.reason,
                details?.layerKey,
                details?.sourcePath,
                details?.prebufferKey,
            ]);
            return false;
        }

        // No prepare in flight and still blocked → this is a permanent failure
        // (missing/offline media), not a transient buffer. Stop freezing and let the
        // timeline play through (audio resumes) rather than holding indefinitely.
        if (state.playbackRebuffering) {
            finishRebuffer({ resume: true, reason: "permanent-failure" });
        }
        drawViewportText("Loading preview...", "");
        resetPlaybackCompositeState();
        playbackDecisionDebugEvent("commit-loading-fallback", details, [
            signature,
            details?.reason,
            details?.layerKey,
            details?.sourcePath,
            details?.prebufferKey,
        ]);
        return false;
    }

    async function resolveRenderableLayer(layer, frame) {
        if (!layer?.asset) return null;
        if (layer.asset.asset_type === "image") {
            const src = buildViewUrl(layer.asset.path || layer.clip?.source_path || "");
            if (!src) return null;
            const img = await loadImage(`live-image:${layer.asset.asset_id || layer.key}`, src);
            return img ? { type: "image", element: img, opacity: layer.opacity, clip: layer.clip } : null;
        }
        const video = getOrCreateVideo(layer);
        if (!video) return null;
        const element = await ensureMediaElementSource(video, layer.clip.source_path);
        if (!element) return null;
        await waitForMediaReady(element, 2);
        const sought = await seekMedia(element, clipSourceTime(layer, frame), {
            requireTarget: true,
            waitForFrame: true,
        });
        return sought ? { type: "video", element: sought, opacity: layer.opacity, clip: layer.clip } : null;
    }

    async function renderLiveComposite(snapshot, renderToken) {
        const guideImage = await loadGuideLayer(snapshot);
        if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
        const renderableLayers = await Promise.all(
            snapshot.playableClipLayers.map((layer) => resolveRenderableLayer(layer, snapshot.frame))
        );
        if (state.destroyed || state.isPlaying || renderToken !== state.renderToken) return;
        drawBlack();
        let drewAny = false;
        if (guideImage) {
            drewAny = drawImageLike(guideImage, { opacity: 1, ...fitOptionsFor(snapshot.guide) }) || drewAny;
        }
        for (const layer of renderableLayers) {
            if (!layer?.element) continue;
            if (drawImageLike(layer.element, { opacity: layer.opacity, ...fitOptionsFor(layer.clip) })) {
                drewAny = true;
            }
        }
        if (!drewAny) {
            drawViewportText("Loading preview...", "");
        }
        drawSceneOutline();
    }

    function syncPlaybackVideoMedia(snapshot) {
        const desiredVideoKeys = new Set();
        const playbackVideoLayers = [...requiredClipLayersAfterCoverage(snapshot)].reverse();
        for (const layer of playbackVideoLayers) {
            if (layer.asset?.asset_type === "image") continue;
            desiredVideoKeys.add(layer.key);
            if (!state.activePlaybackVideos.has(layer.key)) {
                const prebufferClaim = claimPrebufferedVideo(layer, snapshot.frame, snapshot, null, { recordMiss: false });
                const video = prebufferClaim?.video || getOrCreateVideo(layer);
                if (!video) continue;
                const active = createActivePlaybackVideoEntry(layer, video, prebufferClaim);
                state.activePlaybackVideos.set(layer.key, active);
                if (!prebufferClaim) {
                    const candidates = prebufferCandidatesForLayerFrame(layer, snapshot.frame, { snapshot });
                    const pendingCandidate = pendingPrebufferCandidateFromCandidates(candidates);
                    if (pendingCandidate) {
                        markActiveWaitingForPrebuffer(active, layer, snapshot.frame, pendingCandidate);
                        continue;
                    }
                    recordPrebufferMissTelemetry(layer, snapshot.frame, candidates);
                }
                prepareActivePlaybackVideo(active, layer, snapshot.frame);
                continue;
            }
            const active = state.activePlaybackVideos.get(layer.key);
            const sourceChanged = active.layerKey !== layer.key || active.sourcePath !== layer.clip.source_path;
            active.layer = layer;
            if (sourceChanged) {
                const prebufferClaim = claimPrebufferedVideo(layer, snapshot.frame, snapshot, active, { recordMiss: false });
                if (prebufferClaim) {
                    prepareActivePlaybackVideo(active, layer, snapshot.frame);
                } else {
                    const candidates = prebufferCandidatesForLayerFrame(layer, snapshot.frame, { snapshot });
                    const pendingCandidate = pendingPrebufferCandidateFromCandidates(candidates);
                    if (pendingCandidate) {
                        markActiveWaitingForPrebuffer(active, layer, snapshot.frame, pendingCandidate);
                        continue;
                    }
                    recordPrebufferMissTelemetry(layer, snapshot.frame, candidates);
                    active.layerKey = layer.key;
                    active.sourcePath = layer.clip.source_path;
                    active.readyForDraw = false;
                    active.firstDrawComplete = false;
                    prepareActivePlaybackVideo(active, layer, snapshot.frame, { force: true });
                }
            } else if (isActiveVideoDrawable(active, layer, snapshot.frame)) {
                active.readyForDraw = true;
                clearActivePendingPrebuffer(active);
            } else if (shouldDeferPlaybackTailPrepare(active, layer, snapshot)) {
                active.requestedFrame = snapshot.frame;
                active.readyForDraw = false;
                debugPlaybackBoundary("defer-tail-prepare", playbackBlockDetails("tail-prepare-deferred", layer, snapshot, active, {
                    coveredByUpper: isLayerCoveredByDrawableUpperLayer(layer, snapshot),
                    nextFramePrebufferReady: hasReadyNextPlaybackVideo(snapshot.frame),
                    nextPlaybackFrame: nextPlaybackFrameAfter(snapshot.frame),
                }));
            } else {
                const prebufferClaim = claimPrebufferedVideo(layer, snapshot.frame, snapshot, active, { recordMiss: false });
                if (prebufferClaim) {
                    prepareActivePlaybackVideo(active, layer, snapshot.frame);
                    continue;
                }
                const candidates = prebufferCandidatesForLayerFrame(layer, snapshot.frame, { snapshot });
                const pendingCandidate = pendingPrebufferCandidateFromCandidates(candidates);
                if (pendingCandidate) {
                    if (activeHasMatchingPrepare(active, layer, snapshot.frame)) {
                        prepareActivePlaybackVideo(active, layer, snapshot.frame);
                        continue;
                    }
                    markActiveWaitingForPrebuffer(active, layer, snapshot.frame, pendingCandidate);
                    continue;
                }
                recordPrebufferMissTelemetry(layer, snapshot.frame, candidates);
                prepareActivePlaybackVideo(active, layer, snapshot.frame);
            }
        }
        for (const [key, active] of Array.from(state.activePlaybackVideos.entries())) {
            if (desiredVideoKeys.has(key)) continue;
            active.video.pause();
            active.readyForDraw = false;
            active.pendingPrepare = null;
            state.activePlaybackVideos.delete(key);
        }
    }

    function syncPlaybackAudioMedia(snapshot) {
        const canPlayAudioNow = audioPlaybackAllowed(snapshot);
        const desiredAudioKeys = new Set();
        for (const layer of snapshot.audioLayers) {
            desiredAudioKeys.add(layer.key);
            const audioSessionId = state.playbackSessionId;
            const sourcePath = layer.track?.source_path || "";
            const previousAudio = state.activePlaybackAudios.get(layer.key);
            if (
                previousAudio
                && (previousAudio.sourcePath !== sourcePath || previousAudio.playbackSessionId !== audioSessionId)
            ) {
                previousAudio.audio.pause();
                removeMediaSource(previousAudio.audio);
                state.activePlaybackAudios.delete(layer.key);
            }
            if (!state.activePlaybackAudios.has(layer.key)) {
                const audio = getOrCreateAudio(layer);
                if (!audio) continue;
                const activeAudio = {
                    layer,
                    audio,
                    layerKey: layer.key,
                    sourcePath,
                    playbackSessionId: audioSessionId,
                };
                state.activePlaybackAudios.set(layer.key, activeAudio);
                // Audio stays blob-loaded regardless of streamingMode: audio
                // files are small, and whole-file blob avoids network under-buffer
                // stalls and drift that direct streaming introduces (matches the
                // gallery's video-only-direct policy).
                ensureMediaElementSource(audio, layer.track.source_path, { forceBlob: true })
                    .then((element) => waitForMediaReady(element, 1))
                    .then((element) => {
                        const current = state.activePlaybackAudios.get(layer.key);
                        if (
                            !element
                            || !state.isPlaying
                            || state.playbackSessionId !== audioSessionId
                            || current !== activeAudio
                            || current.layerKey !== layer.key
                            || current.sourcePath !== sourcePath
                        ) return;
                        element.currentTime = audioSourceTime(layer, snapshot.frame);
                        element.volume = clamp(Number(layer.track.volume ?? 1), 0, 1);
                        if (audioPlaybackAllowed(snapshot)) {
                            playMediaElement(element, { mediaType: "audio", layerKey: layer.key, sourcePath });
                        }
                    })
                    .catch(() => {});
                continue;
            }
            const active = state.activePlaybackAudios.get(layer.key);
            active.layerKey = layer.key;
            active.layer = layer;
            const expectedTime = audioSourceTime(layer, snapshot.frame);
            active.audio.volume = clamp(Number(layer.track.volume ?? 1), 0, 1);
            if (Math.abs((Number(active.audio.currentTime) || 0) - expectedTime) > 0.35) {
                active.audio.currentTime = expectedTime;
            }
            if (canPlayAudioNow && active.audio.paused) {
                playMediaElement(active.audio, { mediaType: "audio", layerKey: layer.key, sourcePath });
            }
        }
        for (const [key, active] of Array.from(state.activePlaybackAudios.entries())) {
            if (desiredAudioKeys.has(key)) continue;
            active.audio.pause();
            state.activePlaybackAudios.delete(key);
        }
        samplePlaybackQualityTelemetry();
    }

    function syncPlaybackMedia(snapshot) {
        syncPlaybackVideoMedia(snapshot);
        syncPlaybackAudioMedia(snapshot);
    }

    // E8/Phase 2.5: per-boundary coverage telemetry. Fires once per distinct
    // committed video layer set, recording live decoder count plus decode-limiter
    // stats so a re-captured diag can confirm seek storms are serialized.
    function playbackVideoDrawTelemetry(renderable, drawMs, didDraw, frame) {
        const video = renderable?.element || renderable?.active?.video;
        const layer = renderable?.layer;
        const targetSourceFrame = layer ? clipSourceFrame(layer, frame) : null;
        return {
            layerKey: layer?.key || "",
            sourcePath: layer?.clip?.source_path || "",
            targetSourceFrame,
            sourceTargetKey: layer ? prebufferSourceTargetKey(layer, frame) : "",
            fitMode: layer?.clip?.fit_mode || "",
            cropPosition: layer?.clip?.crop_position || "",
            coversCanvas: playbackRenderableCoversCanvas(renderable),
            ...videoElementDimensions(video),
            readyState: video?.readyState || 0,
            currentTime: roundTelemetryMs(video?.currentTime || 0),
            drawMs: roundTelemetryMs(drawMs),
            drew: !!didDraw,
        };
    }

    function prebufferTelemetryStats() {
        let readyCount = 0;
        let pendingCount = 0;
        let staleCount = 0;
        const readySourceTargets = new Set();
        const pendingSourceTargets = new Set();
        for (const entry of state.prebufferCache.values()) {
            if (!entry || entry.cancelled || entry.consumed || entry.claimedByActive) continue;
            if (entry.warmToken !== state.playbackWarmContentToken) {
                staleCount += 1;
                continue;
            }
            if (entry.ready) {
                readyCount += 1;
                if (entry.sourceTargetKey) readySourceTargets.add(entry.sourceTargetKey);
            } else {
                pendingCount += 1;
                if (entry.sourceTargetKey) pendingSourceTargets.add(entry.sourceTargetKey);
            }
        }
        return {
            prebufferReadyCount: readyCount,
            prebufferPendingCount: pendingCount,
            prebufferStaleCount: staleCount,
            prebufferReadySourceTargetCount: readySourceTargets.size,
            prebufferPendingSourceTargetCount: pendingSourceTargets.size,
            prebufferPriorityUpgradePreservedRunning: state.prebufferPriorityUpgradeOutcomes.preservedRunning,
            prebufferPriorityUpgradePromotedQueued: state.prebufferPriorityUpgradeOutcomes.promotedQueued,
            prebufferPriorityUpgradeMovedFront: state.prebufferPriorityUpgradeOutcomes.movedFront,
            prebufferPriorityUpgradeReprioritizedQueued: state.prebufferPriorityUpgradeOutcomes.reprioritizedQueued,
            prebufferPriorityUpgradeRecreatedQueued: state.prebufferPriorityUpgradeOutcomes.recreatedQueued,
            prebufferPriorityUpgradeAlreadyFront: state.prebufferPriorityUpgradeOutcomes.alreadyFront,
            rebufferSafetyRetainedCount: state.playbackRebufferSafetyRetainedCount,
            rebufferSafetyDiscardedCount: state.playbackRebufferSafetyDiscardedCount,
            deferredNextBoundaryRetainedCount: state.playbackDeferredNextBoundaryRetainedCount,
            deferredNextBoundaryDroppedCount: state.playbackDeferredNextBoundaryDroppedCount,
            deferredNextBoundaryScheduledCount: state.playbackDeferredNextBoundaryScheduledCount,
            handoffCurrentTargetCount: state.playbackHandoffCurrentTargetCount,
            handoffContinuationSuppressed: state.playbackHandoffContinuationSuppressed,
            handoffUpcomingSuppressed: state.playbackHandoffUpcomingSuppressed,
            handoffNextBoundaryDelayed: state.playbackHandoffNextBoundaryDelayed,
            handoffQueuedLowPruned: state.playbackHandoffQueuedLowPruned,
            handoffQueuedUrgentPruned: state.playbackHandoffQueuedUrgentPruned,
            currentFrameRecoveryCandidateTotal: state.playbackCurrentFrameRecoveryCandidateCount,
            currentFrameRecoveryPendingExistingTotal: state.playbackCurrentFrameRecoveryPendingExistingCount,
            currentFrameRecoveryMovedFrontTotal: state.playbackCurrentFrameRecoveryMovedFrontCount,
            currentFrameRecoveryReclassifiedTotal: state.playbackCurrentFrameRecoveryReclassifiedCount,
            currentFrameRecoveryQueuedPromotedTotal: state.playbackCurrentFrameRecoveryQueuedPromotedCount,
            currentFrameRecoveryActiveReclassifiedTotal: state.playbackCurrentFrameRecoveryActiveReclassifiedCount,
            continuationSafetyAdmitted: state.playbackContinuationSafetyAdmitted,
            continuationSafetySuppressed: state.playbackContinuationSafetySuppressed,
            deadlineQueuedUrgentStalePruned: state.playbackDeadlineQueuedUrgentStalePruned,
            nonDeadlineQueuedUrgentStalePruned: state.playbackNonDeadlineQueuedUrgentStalePruned,
            handoffPruneReason: state.playbackLastHandoffPruneReason || "",
        };
    }

    function recordBoundaryCoverageTelemetry(snapshot, committedVideoKeys, compositeTiming = null, videoDraws = null) {
        if (!playbackTelemetryActive()) return;
        const scheduleStats = state.lastPrebufferScheduleStats || emptyPrebufferScheduleStats("missing");
        const sig = [
            [...committedVideoKeys].sort().join("|"),
            scheduleStats.rawLayerSignature || "",
            scheduleStats.requiredLayerSignature || "",
            scheduleStats.targetSignature || "",
            state.prebufferCache.size,
        ].join("::");
        if (sig === state.lastBoundaryCoverageSig) return;
        state.lastBoundaryCoverageSig = sig;
        const prebufferMissTelemetrySuppressed = state.prebufferMissTelemetrySuppressed;
        const prebufferMissTelemetryEmitted = state.prebufferMissTelemetryEmitted;
        const prebufferPendingHoldCount = state.prebufferPendingHoldCount;
        state.prebufferMissTelemetryEmitted = 0;
        state.prebufferMissTelemetrySuppressed = 0;
        state.prebufferPendingHoldCount = 0;
        const sceneClipKeys = currentSceneClipKeySet();
        let videoCacheLiveCount = 0;
        for (const key of Object.keys(state.videoCache)) {
            if (sceneClipKeys.has(key)) videoCacheLiveCount += 1;
        }
        recordPlaybackTelemetry("playback_boundary_coverage", {
            frame: snapshot.frame,
            videoLayerCount: committedVideoKeys.length,
            videoCacheLiveCount,
            activeCount: state.activePlaybackVideos.size,
            prebufferCount: state.prebufferCache.size,
            prebufferEnabled: !!isPrebufferEnabled(),
            prebufferLookaheadMs: normalizedPrebufferLookaheadMs(),
            prebufferBoundaryDepth: normalizedPrebufferBoundaryDepth(),
            prebufferMaxEntries: normalizedPrebufferMaxEntries(),
            committedVideoTargets: [...committedVideoKeys],
            rawPlayableVideoCount: scheduleStats.rawPlayableVideoCount,
            requiredVideoCount: scheduleStats.requiredVideoCount,
            culledCoveredVideoCount: scheduleStats.culledCoveredVideoCount,
            currentSafetyTargetCount: scheduleStats.currentSafetyTargetCount,
            currentFrameRecoveryTargetCount: scheduleStats.currentFrameRecoveryTargetCount,
            currentFrameRecoveryCandidateCount: scheduleStats.currentFrameRecoveryCandidateCount,
            currentFrameRecoveryPendingExistingCount: scheduleStats.currentFrameRecoveryPendingExistingCount,
            currentFrameRecoveryMovedFrontCount: scheduleStats.currentFrameRecoveryMovedFrontCount,
            currentFrameRecoveryReclassifiedCount: scheduleStats.currentFrameRecoveryReclassifiedCount,
            currentFrameRecoveryQueuedPromotedCount: scheduleStats.currentFrameRecoveryQueuedPromotedCount,
            currentFrameRecoveryActiveReclassifiedCount: scheduleStats.currentFrameRecoveryActiveReclassifiedCount,
            continuationSafetyTargetCount: scheduleStats.continuationSafetyTargetCount,
            continuationSafetyAdmitted: scheduleStats.continuationSafetyAdmitted,
            continuationSafetySuppressed: scheduleStats.continuationSafetySuppressed,
            currentSafetyAdmissionBlocked: scheduleStats.currentSafetyAdmissionBlocked,
            upcomingTargetCount: scheduleStats.upcomingTargetCount,
            rebufferNextBoundaryTargetCount: scheduleStats.rebufferNextBoundaryTargetCount,
            deferredNextBoundaryTargetCount: scheduleStats.deferredNextBoundaryTargetCount,
            deferredNextBoundaryRetainedCount: scheduleStats.deferredNextBoundaryRetainedCount,
            deferredNextBoundaryDroppedCount: scheduleStats.deferredNextBoundaryDroppedCount,
            prebufferTargetCount: scheduleStats.prebufferTargetCount,
            handoffActive: !!scheduleStats.handoffActive,
            handoffPruneReason: scheduleStats.handoffPruneReason || "",
            rawLayerSignature: scheduleStats.rawLayerSignature,
            requiredLayerSignature: scheduleStats.requiredLayerSignature,
            prebufferTargetSignature: scheduleStats.targetSignature,
            prebufferScheduleReason: scheduleStats.reason,
            prebufferScheduleRebufferLimited: !!scheduleStats.rebufferLimited,
            ...prebufferTelemetryStats(),
            prebufferPendingHoldCount,
            prebufferMissTelemetryEmitted,
            prebufferMissTelemetrySuppressed,
            prebufferMissTelemetryKeyCount: state.prebufferMissTelemetryKeys.size,
            decodeConcurrency: normalizedDecodeConcurrency(),
            canvasWidth: state.canvas?.width || 0,
            canvasHeight: state.canvas?.height || 0,
            ...(compositeTiming ? { compositeTiming } : {}),
            ...(Array.isArray(videoDraws) ? { videoNativeSizes: videoDraws } : {}),
            ...playbackDecodeLimiter.flushStats(),
            playbackSessionId: state.playbackSessionId,
        });
    }

    function drawPlaybackComposite(snapshot) {
        const telemetryActive = playbackTelemetryActive();
        const compositeStartedAt = telemetryActive ? performance.now() : 0;
        const preflight = preflightPlaybackComposite(snapshot);
        if (preflight.blocked) {
            commitPlaybackBlocked(snapshot, preflight.details, { suppressFallback: !!preflight.suppressFallback });
            return false;
        }

        const preflightFinishedAt = telemetryActive ? performance.now() : 0;
        const drawStartedAt = telemetryActive ? performance.now() : 0;
        drawBlack();
        let drewAny = false;
        const committedVideoKeys = [];
        const videoDraws = telemetryActive ? [] : null;
        let videoDrawMs = 0;
        let imageDrawMs = 0;
        let guideDrawMs = 0;
        for (const renderable of preflight.renderables || []) {
            const fitItem = renderable.type === "guide" ? renderable.guide : renderable.layer?.clip;
            const layerDrawStartedAt = telemetryActive ? performance.now() : 0;
            const didDraw = drawImageLike(renderable.element, { opacity: renderable.opacity, ...fitOptionsFor(fitItem) });
            if (telemetryActive) {
                const layerDrawMs = performance.now() - layerDrawStartedAt;
                if (renderable.type === "video") {
                    videoDrawMs += layerDrawMs;
                    videoDraws.push(playbackVideoDrawTelemetry(renderable, layerDrawMs, didDraw, snapshot.frame));
                } else if (renderable.type === "image") {
                    imageDrawMs += layerDrawMs;
                } else if (renderable.type === "guide") {
                    guideDrawMs += layerDrawMs;
                }
            }
            if (renderable.type === "video" && renderable.active) {
                pauseTailPlaybackVideo(renderable.active, renderable.layer, snapshot.frame);
            }
            if (didDraw) {
                drewAny = true;
                if (renderable.type === "video" && renderable.active) {
                    renderable.active.firstDrawComplete = true;
                    renderable.active.readyForDraw = true;
                    syncPreparedVideoPlayback(renderable.active, renderable.layer, snapshot.frame);
                    notePlaybackWarmLayer(renderable.layer, snapshot.frame, "warm", "composite-commit");
                    if (renderable.layer?.key) {
                        committedVideoKeys.push(renderable.layer.key);
                    }
                } else if (renderable.type === "image" && renderable.layer) {
                    notePlaybackWarmLayer(renderable.layer, snapshot.frame, "warm", "composite-commit");
                }
            }
        }
        const outlineStartedAt = telemetryActive ? performance.now() : 0;
        drawSceneOutline();
        const drawFinishedAt = telemetryActive ? performance.now() : 0;
        const compositeTiming = telemetryActive ? {
            preflightMs: roundTelemetryMs(preflightFinishedAt - compositeStartedAt),
            drawMs: roundTelemetryMs(drawFinishedAt - drawStartedAt),
            totalMs: roundTelemetryMs(drawFinishedAt - compositeStartedAt),
            videoDrawMs: roundTelemetryMs(videoDrawMs),
            imageDrawMs: roundTelemetryMs(imageDrawMs),
            guideDrawMs: roundTelemetryMs(guideDrawMs),
            outlineMs: roundTelemetryMs(drawFinishedAt - outlineStartedAt),
            renderableCount: (preflight.renderables || []).length,
            committedVideoCount: committedVideoKeys.length,
        } : null;
        recordBoundaryCoverageTelemetry(snapshot, committedVideoKeys, compositeTiming, videoDraws);
        state.playbackCompositeCommitted = true;
        state.playbackBlockedSinceMs = null;
        state.playbackBlockedSignature = "";
        state.playbackCanvasWidth = state.canvas?.width || 0;
        state.playbackCanvasHeight = state.canvas?.height || 0;
        state.playbackLastCommittedFrame = snapshot.frame;
        state.playbackLastCommittedSignature = playbackLayerSignature(snapshot);
        state.playbackLastCommittedSessionId = state.playbackSessionId;
        clearPlaybackDecisionLogs();
        releaseAudioForSession("first-composite-commit", { frame: snapshot.frame });
        clearFirstCommitHold();
        // maybeHoldForRebuffer owns rebuffer exit. Future next-boundary work can
        // stay pending after the held/current frame is safe.
        if (state.playbackRebuffering) pauseActivePlaybackVideos();
        // The new frame is committed; outgoing elements parked at claim time are
        // no longer on screen and can be torn down.
        drainPendingReleases();
        debugPlaybackBoundary("commit-playback-composite", {
            frame: snapshot.frame,
            drewAny,
            renderables: (preflight.renderables || []).map((entry) => ({
                type: entry.type,
                layerKey: entry.layer?.key || "",
                sourcePath: entry.layer?.clip?.source_path || "",
                opacity: entry.opacity,
                coversCanvas: !!entry.coversCanvas,
            })),
            skippedLayers: preflight.skippedLayers || [],
        });
        return drewAny;
    }

    function renderPlaybackFrame(snapshot) {
        if (shouldReuseCommittedPlaybackFrame(snapshot)) return true;
        notePlaybackWarmMissingLayers(snapshot, "missing-layer");
        syncPlaybackVideoMedia(snapshot);
        const handoffState = currentFrameHandoffRecoveryState(snapshot);
        schedulePlaybackPrebuffer(snapshot, { handoffState });
        syncPlaybackAudioMedia(snapshot);
        return drawPlaybackComposite(snapshot);
    }

    function renderFrame() {
        if (state.destroyed || !state.canvas || !getCanvasContext()) return;
        notifyTransport();
        const width = state.canvas.width;
        const height = state.canvas.height;
        if (width <= 0 || height <= 0) return;
        const frame = currentFrame();
        const snapshot = buildFrameSnapshot(frame);
        const renderToken = ++state.renderToken;

        if (!snapshot.playableClipLayers.length && !snapshot.missingClipLayers.length && !snapshot.guide) {
            if (state.isPlaying) resetPlaybackCompositeState();
            drawViewportText(`Frame ${frame}`);
            drawSceneOutline();
            return;
        }
        if (!snapshot.playableClipLayers.length && snapshot.missingClipLayers.length) {
            if (state.isPlaying) resetPlaybackCompositeState();
            const missingLayer = snapshot.missingClipLayers[0];
            const missingSourceName = typeof missingLayer.clip?.source_path === "string"
                ? missingLayer.clip.source_path.split(/[/\\]/).pop()
                : "";
            const missingName = missingLayer.asset?.name
                || missingSourceName
                || "Missing clip";
            drawViewportText("Missing clip", missingName, {
                titleColor: "#dfb1b1",
                subtitleColor: THEME.fg2,
            });
            drawSceneOutline();
            return;
        }
        if (state.isPlaying) {
            renderPlaybackFrame(snapshot);
            return;
        }
        if (!state.liveMediaEnabled) {
            renderStaticComposite(snapshot, renderToken).catch((error) => {
                console.warn("[Sonder] Static viewport preview failed:", error);
            });
            return;
        }
        renderLiveComposite(snapshot, renderToken).catch((error) => {
            console.warn("[Sonder] Live viewport render failed:", error);
        });
    }

    // Tiny loops can outrun the first async seek; hold the clock briefly so
    // the current playback session can commit one drawable frame.
    function holdPlaybackClockForFirstCommit(timestamp, nextFrame, endFrame) {
        if (state.playbackCompositeCommitted || state.playbackFirstCommitHoldExpired) return false;
        const holdFrame = clamp(
            Math.round(Number(state.playbackFirstCommitFrame ?? state.playbackStartFrame) || 0),
            0,
            totalFrames()
        );
        if (nextFrame <= holdFrame) return false;
        const startedAt = Number.isFinite(state.playbackFirstCommitStartedAt)
            ? state.playbackFirstCommitStartedAt
            : state.playbackStartTime;
        const blockedForMs = timestamp - startedAt;
        if (blockedForMs > PLAYBACK_FIRST_COMMIT_HOLD_MS) {
            state.playbackFirstCommitHoldExpired = true;
            releaseAudioForSession("first-commit-hold-expired", {
                frame: holdFrame,
                nextFrame,
                endFrame,
                blockedForMs,
            });
            playbackDebugEvent("release-first-commit-clock", {
                frame: holdFrame,
                nextFrame,
                endFrame,
                blockedForMs,
                playbackSessionId: state.playbackSessionId,
            });
            return false;
        }
        if (currentFrame() !== holdFrame) {
            applyFrame(holdFrame, { reason: "playback-first-commit-hold" });
        }
        state.playbackStartTime = timestamp;
        state.playbackStartFrame = holdFrame;
        const snapshot = buildFrameSnapshot(holdFrame);
        invalidateAsyncPreviewRenders();
        renderPlaybackFrame(snapshot);
        debugPlaybackBoundary("hold-first-commit-clock", {
            frame: holdFrame,
            nextFrame,
            endFrame,
            blockedForMs,
            playbackSessionId: state.playbackSessionId,
        });
        state.playbackRAF = requestAnimationFrame(playbackTick);
        return true;
    }

    // --- Phase 2 adaptive rebuffer ------------------------------------------------
    // Full reset for session start / stop / dormant teardown.
    function clearRebufferToastTimer() {
        if (state.playbackRebufferToastTimer !== null) {
            window.clearTimeout(state.playbackRebufferToastTimer);
            state.playbackRebufferToastTimer = null;
        }
    }

    function clearRebufferToastDismissTimer() {
        if (state.playbackRebufferToastDismissTimer !== null) {
            window.clearTimeout(state.playbackRebufferToastDismissTimer);
            state.playbackRebufferToastDismissTimer = null;
        }
    }

    function setRebufferToastPressure(value, now = performance.now()) {
        const pressure = Math.max(
            0,
            Math.min(PLAYBACK_REBUFFER_TOAST_DELAY_MS, Number(value) || 0),
        );
        state.playbackRebufferToastPressureMs = pressure;
        state.playbackRebufferToastPressureAtMs = pressure > 0 ? now : 0;
    }

    function decayedRebufferToastPressure(now = performance.now()) {
        const pressure = Math.max(0, Number(state.playbackRebufferToastPressureMs) || 0);
        const at = Number(state.playbackRebufferToastPressureAtMs) || 0;
        if (!pressure || !at) return 0;
        const elapsed = Math.max(0, now - at);
        if (elapsed >= PLAYBACK_REBUFFER_TOAST_DECAY_MS) return 0;
        return pressure * (1 - (elapsed / PLAYBACK_REBUFFER_TOAST_DECAY_MS));
    }

    function captureRebufferToastPressure(now = performance.now()) {
        const pressure = decayedRebufferToastPressure(now);
        setRebufferToastPressure(pressure, now);
        return pressure;
    }

    function currentRebufferToastPressure(now = performance.now()) {
        const base = Math.max(0, Number(state.playbackRebufferToastPressureMs) || 0);
        const heldMs = state.playbackRebufferSinceMs !== null
            ? Math.max(0, now - state.playbackRebufferSinceMs)
            : 0;
        return Math.min(PLAYBACK_REBUFFER_TOAST_DELAY_MS, base + heldMs);
    }

    function setRebufferHeavyPressure(value, now = performance.now()) {
        const pressure = Math.max(
            0,
            Math.min(PLAYBACK_REBUFFER_HEAVY_PRESSURE_THRESHOLD, Number(value) || 0),
        );
        state.playbackRebufferHeavyPressure = pressure;
        state.playbackRebufferHeavyPressureAtMs = pressure > 0 ? now : 0;
    }

    function decayedRebufferHeavyPressure(now = performance.now()) {
        const pressure = Math.max(0, Number(state.playbackRebufferHeavyPressure) || 0);
        const at = Number(state.playbackRebufferHeavyPressureAtMs) || 0;
        if (!pressure || !at) return 0;
        const elapsed = Math.max(0, now - at);
        if (elapsed >= PLAYBACK_REBUFFER_HEAVY_PRESSURE_DECAY_MS) return 0;
        return pressure * (1 - (elapsed / PLAYBACK_REBUFFER_HEAVY_PRESSURE_DECAY_MS));
    }

    function captureRebufferHeavyPressure(now = performance.now()) {
        const pressure = decayedRebufferHeavyPressure(now);
        setRebufferHeavyPressure(pressure, now);
        return pressure;
    }

    function clearRebufferHeavyPressure() {
        setRebufferHeavyPressure(0, 0);
        state.playbackRebufferHeavyWarningAtMs = 0;
    }

    function addRebufferHeavyPressure(value = PLAYBACK_REBUFFER_HEAVY_PRESSURE_PER_BUFFER, now = performance.now()) {
        const pressure = Math.min(
            PLAYBACK_REBUFFER_HEAVY_PRESSURE_THRESHOLD,
            decayedRebufferHeavyPressure(now) + Math.max(0, Number(value) || 0),
        );
        setRebufferHeavyPressure(pressure, now);
        if (pressure < PLAYBACK_REBUFFER_HEAVY_PRESSURE_THRESHOLD) return false;
        if (state.playbackRebufferToastLevel === "warning") return false;
        const lastWarningAt = Number(state.playbackRebufferHeavyWarningAtMs) || 0;
        if (lastWarningAt && now - lastWarningAt < PLAYBACK_REBUFFER_HEAVY_WARNING_COOLDOWN_MS) {
            return false;
        }
        escalateRebufferToast(now);
        return true;
    }

    function dismissRebufferToastNow() {
        if (state.playbackRebufferToastHandle) {
            try { state.playbackRebufferToastHandle.dismiss(); } catch (e) { /* ignore */ }
            state.playbackRebufferToastHandle = null;
        }
        state.playbackRebufferToastLevel = "";
        state.playbackRebufferToastShownAtMs = 0;
    }

    function clearRebufferToast() {
        clearRebufferToastTimer();
        clearRebufferToastDismissTimer();
        dismissRebufferToastNow();
        setRebufferToastPressure(0, 0);
        clearRebufferHeavyPressure();
    }

    function dismissRebufferToastAfterMinimum(now = performance.now()) {
        clearRebufferToastDismissTimer();
        if (!state.playbackRebufferToastHandle) {
            state.playbackRebufferToastShownAtMs = 0;
            state.playbackRebufferToastLevel = "";
            return;
        }
        const shownAt = Number(state.playbackRebufferToastShownAtMs) || now;
        const remainingMs = PLAYBACK_REBUFFER_TOAST_MIN_VISIBLE_MS - Math.max(0, now - shownAt);
        if (remainingMs <= 0) {
            dismissRebufferToastNow();
            return;
        }
        state.playbackRebufferToastDismissTimer = window.setTimeout(() => {
            state.playbackRebufferToastDismissTimer = null;
            if (state.playbackRebuffering) return;
            dismissRebufferToastNow();
        }, remainingMs);
    }

    function rebufferToastDurationMs() {
        const maxMs = Number(getRebufferMaxMs());
        return Math.max(
            PLAYBACK_REBUFFER_TOAST_LINGER_MS,
            (Number.isFinite(maxMs) ? maxMs : 0) + 1000,
        );
    }

    function showRebufferToast(now = performance.now()) {
        if (!state.playbackRebuffering || state.playbackRebufferCapped) return;
        clearRebufferToastDismissTimer();
        if (state.playbackRebufferToastLevel === "warning") {
            addRebufferHeavyPressure(PLAYBACK_REBUFFER_HEAVY_PRESSURE_PER_BUFFER, now);
            escalateRebufferToast(now);
            return;
        }
        const durationMs = rebufferToastDurationMs();
        if (state.playbackRebufferToastHandle) {
            state.playbackRebufferToastHandle.update({
                tier: "info",
                message: "Buffering...",
                durationMs,
            });
        } else {
            state.playbackRebufferToastHandle = notifyInfo("Buffering...", {
                source: "playback-rebuffer",
                durationMs,
            });
        }
        state.playbackRebufferToastShownAtMs = now;
        state.playbackRebufferToastLevel = "buffering";
        // Once the toast is visible, keep cooldown pressure decaying from this
        // display time so follow-up stalls are judged in recent playback context.
        setRebufferToastPressure(PLAYBACK_REBUFFER_TOAST_DELAY_MS, now);
        addRebufferHeavyPressure(PLAYBACK_REBUFFER_HEAVY_PRESSURE_PER_BUFFER, now);
    }

    function scheduleRebufferToast(now = performance.now()) {
        clearRebufferToastDismissTimer();
        if (state.playbackRebufferToastHandle) {
            showRebufferToast(now);
            return;
        }
        if (state.playbackRebufferToastTimer !== null) return;
        const remainingMs = PLAYBACK_REBUFFER_TOAST_DELAY_MS - currentRebufferToastPressure(now);
        if (remainingMs <= 0) {
            showRebufferToast(now);
            return;
        }
        state.playbackRebufferToastTimer = window.setTimeout(() => {
            state.playbackRebufferToastTimer = null;
            showRebufferToast(performance.now());
        }, remainingMs);
    }

    function settleRebufferToastOnRecovery(heldMs, now = performance.now()) {
        clearRebufferToastTimer();
        const totalPressure = Math.max(
            0,
            (Number(state.playbackRebufferToastPressureMs) || 0) + Math.max(0, Number(heldMs) || 0),
        );
        if (!state.playbackRebufferToastHandle && totalPressure >= PLAYBACK_REBUFFER_TOAST_DELAY_MS) {
            showRebufferToast(now);
        }
        if (state.playbackRebufferToastHandle) {
            dismissRebufferToastAfterMinimum(now);
        } else {
            setRebufferToastPressure(totalPressure, now);
        }
    }

    function resetRebufferState() {
        state.playbackRebuffering = false;
        state.playbackRebufferFrame = 0;
        state.playbackRebufferSinceMs = null;
        state.playbackRebufferCapped = false;
        state.playbackRebufferLastExitMs = 0;
        state.playbackRebufferBlockTargetKey = "";
        state.playbackRebufferLastExitTargetKey = "";
        state.playbackRebufferEntryDecisionSig = "";
        clearRebufferSafetyState();
        clearDeferredNextBoundaryTargets("rebuffer-reset");
        clearRebufferToast();
    }

    function enterRebuffer(now, details = {}, decision = {}) {
        const blockReason = typeof details === "string" ? details : (details?.reason || "");
        const blockTargetKey = decision.blockTargetKey || rebufferBlockTargetKey(details);
        captureRebufferToastPressure(now);
        captureRebufferHeavyPressure(now);
        state.playbackRebuffering = true;
        // Hold at the current (runaway) frame: audio has already played to ~here, so
        // catching the frozen video up to this frame keeps audio continuous (no
        // rewind) and resyncs A/V where the listener already is.
        state.playbackRebufferFrame = currentFrame();
        state.playbackRebufferSinceMs = now;
        state.playbackRebufferCapped = false;
        state.playbackRebufferBlockTargetKey = blockTargetKey;
        clearRebufferSafetyState();
        // Deferred ownership belongs to the previous rebuffer cycle. A new hold
        // computes its own effective next boundary and must not protect stale work.
        clearDeferredNextBoundaryTargets("rebuffer-reentry");
        for (const active of state.activePlaybackVideos.values()) {
            active.video.pause();
        }
        for (const active of state.activePlaybackAudios.values()) {
            active.audio.pause();
        }
        scheduleRebufferToast(now);
        recordPlaybackTelemetry("playback_rebuffer_enter", {
            frame: state.playbackRebufferFrame,
            committedFrame: state.playbackLastCommittedFrame,
            blockReason,
            blockTargetKey,
            lastExitTargetKey: state.playbackRebufferLastExitTargetKey || "",
            reentryCooldownState: decision.decision || "",
            currentSafetyTargetCount: state.lastPrebufferScheduleStats?.currentSafetyTargetCount || 0,
            playbackSessionId: state.playbackSessionId,
        });
    }

    // Exit a live rebuffer (a frame committed again, or a permanent failure made
    // holding pointless). resume:true re-pins the clock and re-seeks audio to the
    // buffering frame so A/V resume in sync.
    function finishRebuffer({ resume, reason } = {}) {
        if (!state.playbackRebuffering && !state.playbackRebufferToastHandle && state.playbackRebufferToastTimer === null) return;
        const now = performance.now();
        const wasBuffering = state.playbackRebuffering;
        const heldMs = state.playbackRebufferSinceMs !== null
            ? now - state.playbackRebufferSinceMs
            : 0;
        const exitTargetKey = state.playbackRebufferBlockTargetKey || "";
        const resumeSafetyTargetCount = state.playbackRebufferSafetyTargets?.length || 0;
        const safetyStatuses = rebufferSafetyStatusTelemetry(state.playbackRebufferLastSafetyStatuses || []);
        const safetyReason = state.playbackRebufferLastSafetyReason || "";
        const resumeDeferredSuppressed = state.playbackRebufferResumeDeferredSuppressed;
        const rebufferLimitedSuppressed = state.playbackRebufferLimitedSuppressed;
        const retainedSafetyEntryCount = state.playbackRebufferSafetyRetainedCount;
        const discardedSafetyEntryCount = state.playbackRebufferSafetyDiscardedCount;
        const nextBoundaryPendingNonBlocking = (state.playbackRebufferLastSafetyStatuses || [])
            .filter((entry) => !entry.satisfied && entry.target?.scheduleOrigin === "rebuffer-next-boundary")
            .length;
        const decodeSnapshot = playbackDecodeLimiter.snapshotStats();
        // Resume from the buffering frame (where audio already is) — NOT
        // lastCommittedFrame, which a permanent-failure exit resets to null.
        const resumeFrame = clamp(Math.round(Number(state.playbackRebufferFrame) || 0), 0, totalFrames());
        const playbackEndFrame = state.playbackLoopRange ? state.playbackLoopRange.end : totalFrames();
        const deferredNextBoundaryRetained = preserveDeferredNextBoundaryTargetsFromRebuffer(
            buildFrameSnapshot(resumeFrame),
            playbackEndFrame,
        );
        settleRebufferToastOnRecovery(heldMs, now);
        state.playbackRebuffering = false;
        state.playbackRebufferSinceMs = null;
        state.playbackRebufferCapped = false;
        state.playbackRebufferLastExitMs = now;
        state.playbackRebufferLastExitTargetKey = exitTargetKey;
        state.playbackRebufferBlockTargetKey = "";
        clearRebufferSafetyState();
        if (resume) {
            // Re-pin the clock and re-seek audio to the buffering frame so A/V resume
            // locked together (covers both the held-tick and async prepare-ready paths).
            state.playbackStartTime = now;
            state.playbackStartFrame = resumeFrame;
            if (currentFrame() !== resumeFrame) {
                applyFrame(resumeFrame, { reason: "playback-rebuffer-resume" });
            }
            for (const active of state.activePlaybackAudios.values()) {
                try { active.audio.currentTime = audioSourceTime(active.layer, resumeFrame); } catch (e) { /* ignore */ }
            }
            // Held-frame commits pause media while buffering; force one normal
            // playback pass so syncPlaybackMedia restarts drawable audio/video.
            state.playbackLastCommittedFrame = null;
            state.playbackLastCommittedSignature = "";
            state.playbackLastCommittedSessionId = 0;
        }
        if (wasBuffering) {
            recordPlaybackTelemetry("playback_rebuffer_exit", {
                reason: reason || "",
                frame: resumeFrame,
                heldMs: roundTelemetryMs(heldMs),
                blockTargetKey: exitTargetKey,
                lastExitTargetKey: state.playbackRebufferLastExitTargetKey || "",
                resumeSafetyTargetCount,
                safetyReason,
                safetyStatuses,
                resumeDeferredSuppressed,
                rebufferLimitedSuppressed,
                retainedSafetyEntryCount,
                discardedSafetyEntryCount,
                nextBoundaryPendingNonBlocking,
                rebufferExitNextBoundaryPendingNonBlocking: nextBoundaryPendingNonBlocking,
                deferredNextBoundaryRetained,
                deferredNextBoundaryDroppedCount: state.playbackDeferredNextBoundaryDroppedCount,
                ...decodeSnapshot,
                playbackSessionId: state.playbackSessionId,
            });
        }
    }

    function escalateRebufferToast(now = performance.now()) {
        clearRebufferToastTimer();
        clearRebufferToastDismissTimer();
        const patch = {
            tier: "warning",
            message: "Scene too heavy for smooth live playback",
            durationMs: PLAYBACK_REBUFFER_TOAST_LINGER_MS,
        };
        if (state.playbackRebufferToastHandle) {
            state.playbackRebufferToastHandle.update(patch);
        } else {
            state.playbackRebufferToastHandle = notifyWarning(patch.message, {
                source: "playback-rebuffer",
                durationMs: patch.durationMs,
            });
        }
        state.playbackRebufferToastShownAtMs = now;
        state.playbackRebufferToastLevel = "warning";
        setRebufferToastPressure(PLAYBACK_REBUFFER_TOAST_DELAY_MS, now);
        setRebufferHeavyPressure(PLAYBACK_REBUFFER_HEAVY_PRESSURE_THRESHOLD, now);
        state.playbackRebufferHeavyWarningAtMs = now;
    }

    // Freeze the wall clock at the buffering frame so the prepare target stops
    // moving and the in-flight seek can land. Returns true while held (consumes the
    // tick). At the cap, prefer an honest stall (last frame held, audio paused) over
    // resuming the runaway.
    function maybeHoldForRebuffer(timestamp) {
        if (!state.playbackRebuffering) return false;
        const holdFrame = clamp(Math.round(Number(state.playbackRebufferFrame) || 0), 0, totalFrames());
        state.playbackStartTime = timestamp;
        state.playbackStartFrame = holdFrame;
        if (currentFrame() !== holdFrame) {
            applyFrame(holdFrame, { reason: "playback-rebuffer-hold" });
        }
        const snapshot = buildFrameSnapshot(holdFrame);
        invalidateAsyncPreviewRenders();
        renderPlaybackFrame(snapshot);
        if (!state.playbackRebuffering) {
            // Recovered by a permanent-failure path inside renderPlaybackFrame.
        } else {
            const heldMs = state.playbackRebufferSinceMs !== null ? timestamp - state.playbackRebufferSinceMs : 0;
            const safety = evaluateRebufferResumeSafety(snapshot, timestamp);
            if (safety.ready) {
                finishRebuffer({ resume: true, reason: safety.reason || "recovered" });
            } else {
                pauseActivePlaybackVideos();
                recordRebufferResumeDeferred(snapshot, safety, heldMs, timestamp);
                if (heldMs >= getRebufferMaxMs() && !state.playbackRebufferCapped) {
                    state.playbackRebufferCapped = true;
                    escalateRebufferToast();
                    const expiredPendingCount = expireStalePendingPrebuffers(snapshot, timestamp, "rebuffer-cap");
                    recordPlaybackTelemetry("playback_rebuffer_cap", {
                        frame: holdFrame,
                        heldMs,
                        expiredPendingCount,
                        blockTargetKey: state.playbackRebufferBlockTargetKey || "",
                        resumeSafetyTargetCount: state.playbackRebufferSafetyTargets?.length || 0,
                        safetyReason: safety.reason || "",
                        currentBlockedCount: safety.currentBlockedCount,
                        nextBoundaryBlockedCount: safety.nextBoundaryBlockedCount,
                        nextBoundaryPendingNonBlocking: safety.nextBoundaryPendingNonBlocking || 0,
                        nextBoundarySoftBudgetMs: roundTelemetryMs(safety.nextBoundarySoftBudgetMs),
                        safetyStatuses: rebufferSafetyStatusTelemetry(safety.statuses),
                        resumeDeferredSuppressed: state.playbackRebufferResumeDeferredSuppressed,
                        rebufferLimitedSuppressed: state.playbackRebufferLimitedSuppressed,
                        retainedSafetyEntryCount: state.playbackRebufferSafetyRetainedCount,
                        discardedSafetyEntryCount: state.playbackRebufferSafetyDiscardedCount,
                        ...playbackDecodeLimiter.snapshotStats(),
                        playbackSessionId: state.playbackSessionId,
                    });
                }
                if (state.playbackRebufferCapped && safety.ready) {
                    finishRebuffer({ resume: true, reason: "resume-safety-cap" });
                }
            }
        }
        state.playbackRAF = requestAnimationFrame(playbackTick);
        return true;
    }

    function restartPlaybackLoop(timestamp) {
        if (!state.playbackLoopRange) return;
        const hadCommittedFrame = state.playbackCompositeCommitted;
        state.playbackSessionId += 1;
        clearPlaybackDecisionLogs();
        clearDeferredNextBoundaryTargets("loop-restart");
        resetPlaybackCompositeState();
        const nextFrame = applyFrame(state.playbackLoopRange.start, { reason: "playback-loop" });
        state.playbackStartTime = timestamp;
        state.playbackStartFrame = nextFrame;
        state.playbackFirstCommitFrame = nextFrame;
        if (hadCommittedFrame || state.playbackFirstCommitStartedAt === null) {
            beginFirstCommitHold(timestamp, nextFrame);
        }
        const snapshot = buildFrameSnapshot(nextFrame);
        invalidateAsyncPreviewRenders();
        renderPlaybackFrame(snapshot);
        state.playbackRAF = requestAnimationFrame(playbackTick);
    }

    function playbackTick(timestamp) {
        if (state.destroyed || !state.isPlaying) return;
        // Freeze the clock before any advance while rebuffering (mirrors the
        // first-commit hold) so the playhead never overshoots the buffering frame.
        if (maybeHoldForRebuffer(timestamp)) {
            return;
        }
        // RAF timestamps can lag a clock reset that happened during async rebuffer recovery.
        const elapsedSeconds = Math.max(0, (timestamp - state.playbackStartTime) / 1000);
        const nextFrame = state.playbackStartFrame + Math.floor(elapsedSeconds * fps());
        const loopRange = state.playbackLoopRange;
        const endFrame = loopRange ? loopRange.end : totalFrames();
        if (holdPlaybackClockForFirstCommit(timestamp, nextFrame, endFrame)) {
            return;
        }
        if (nextFrame >= endFrame) {
            if (loopRange) {
                restartPlaybackLoop(timestamp);
                return;
            }
            applyFrame(totalFrames(), { reason: "playback-end" });
            stopPlayback();
            return;
        }
        applyFrame(nextFrame, { reason: "playback" });
        recordFramesBehindTelemetry(timestamp, nextFrame, endFrame);
        const snapshot = buildFrameSnapshot(nextFrame);
        invalidateAsyncPreviewRenders();
        renderPlaybackFrame(snapshot);
        state.playbackRAF = requestAnimationFrame(playbackTick);
    }

    function clearActivePlaybackMedia() {
        for (const active of state.activePlaybackVideos.values()) {
            active.video.pause();
            active.readyForDraw = false;
            active.pendingPrepare = null;
        }
        state.activePlaybackVideos.clear();
        for (const active of state.activePlaybackAudios.values()) {
            active.audio.pause();
        }
        state.activePlaybackAudios.clear();
        drainPendingReleases(true);
    }

    function stopPlayback({ preservePlayhead = false } = {}) {
        if (state.playbackRAF) {
            cancelAnimationFrame(state.playbackRAF);
            state.playbackRAF = null;
        }
        if (!state.isPlaying) {
            notifyTransport();
            return;
        }
        updatePlaybackState(false);
        flushBlockReasonTelemetry();
        resetRebufferState();
        clearActivePlaybackMedia();
        clearPrebufferCache();
        state.prebufferMissTelemetryKeys.clear();
        state.prebufferMissTelemetryEmitted = 0;
        state.prebufferMissTelemetrySuppressed = 0;
        state.prebufferPendingHoldCount = 0;
        state.expiredPendingPrebufferSkips.clear();
        state.lastRebufferLimitedScheduleSig = "";
        state.lastPrebufferScheduleStats = emptyPrebufferScheduleStats("playback-stop");
        resetPlaybackHandoffCounters();
        pruneTransientPlaybackWarmEntries("playback-stop");
        resetPlaybackCompositeState();
        clearFirstCommitHold();
        if (!preservePlayhead && shouldReturnToPlaybackStart()) {
            applyFrame(state.playbackSessionStartFrame, { reason: "playback-stop-return" });
        }
        state.playbackLoopRange = null;
        renderFrame();
    }

    function startPlayback() {
        if (state.destroyed || state.isPlaying) return;
        if (!state.liveMediaEnabled) {
            state.liveMediaEnabled = true;
        }
        const loopRange = getLoopRange();
        if (loopRange && (currentFrame() < loopRange.start || currentFrame() >= loopRange.end)) {
            applyFrame(loopRange.start, { reason: "playback-start-align" });
        }
        const startFrame = currentFrame();
        state.playbackStartTime = performance.now();
        state.playbackStartFrame = startFrame;
        state.playbackSessionStartFrame = startFrame;
        state.playbackLoopRange = loopRange;
        state.playbackSessionId += 1;
        invalidateAsyncPreviewRenders();
        clearPlaybackDecisionLogs();
        resetPlaybackCompositeState();
        resetAudioReleaseLatch();
        resetPlaybackTelemetry();
        resetRebufferState();
        beginFirstCommitHold(state.playbackStartTime, startFrame);
        updatePlaybackState(true);
        const snapshot = buildFrameSnapshot(startFrame);
        renderPlaybackFrame(snapshot);
        state.playbackRAF = requestAnimationFrame(playbackTick);
    }

    function togglePlayback() {
        if (state.isPlaying) {
            stopPlayback();
        } else {
            startPlayback();
        }
    }

    function clearMediaCache() {
        clearActivePlaybackMedia();
        clearPrebufferCache();
        resetPlaybackCompositeState();
        clearFirstCommitHold();
        resetRebufferState();
        clearPlaybackWarmState("media-cache-clear");
        for (const mediaEl of Object.values(state.videoCache)) {
            removeMediaSource(mediaEl);
        }
        for (const mediaEl of Object.values(state.audioCache)) {
            removeMediaSource(mediaEl);
        }
        for (const entry of state.sourceUrlCache.values()) {
            if (entry?.usesObjectUrl && entry.objectUrl) {
                URL.revokeObjectURL(entry.objectUrl);
            }
        }
        state.sourceUrlCache.clear();
        clearCacheObject(state.videoCache);
        clearCacheObject(state.audioCache);
        clearCacheObject(state.imageCache);
        state.lastBoundaryCoverageSig = "";
        state.prebufferMissTelemetryKeys.clear();
        state.prebufferMissTelemetryEmitted = 0;
        state.prebufferMissTelemetrySuppressed = 0;
        state.prebufferPendingHoldCount = 0;
        state.expiredPendingPrebufferSkips.clear();
        state.lastRebufferLimitedScheduleSig = "";
        state.lastPrebufferScheduleStats = emptyPrebufferScheduleStats("media-cache-clear");
        resetPlaybackHandoffCounters();
    }

    function invalidatePlaybackComposite() {
        resetPlaybackCompositeState();
    }

    function setLiveMediaEnabled(nextValue) {
        const enabled = !!nextValue;
        if (state.liveMediaEnabled === enabled) {
            notifyTransport();
            return;
        }
        if (!enabled && state.isPlaying) {
            stopPlayback({ preservePlayhead: true });
        }
        state.liveMediaEnabled = enabled;
        notifyTransport();
        renderFrame();
    }

    function destroy() {
        if (state.destroyed) return;
        state.destroyed = true;
        if (state.playbackRAF) {
            cancelAnimationFrame(state.playbackRAF);
            state.playbackRAF = null;
        }
        unregisterDiagClearHook();
        updatePlaybackState(false);
        clearMediaCache();
    }

    // Reset hook for window.SonderClearDiag() — wipes this surface's telemetry
    // counters and the decision-log dedup keys (the latter is what otherwise
    // forces a reload to re-see suppressed playback logs). Mirrors the reset
    // that startPlayback() already performs, so it is safe to invoke anytime.
    function diagClearHook() {
        clearPlaybackDecisionLogs();
        resetPlaybackTelemetry();
    }
    function registerDiagClearHook() {
        if (typeof window === "undefined") return;
        if (!(window.__SONDER_DIAG_CLEARERS instanceof Set)) {
            window.__SONDER_DIAG_CLEARERS = new Set();
        }
        window.__SONDER_DIAG_CLEARERS.add(diagClearHook);
    }
    function unregisterDiagClearHook() {
        if (typeof window !== "undefined" && window.__SONDER_DIAG_CLEARERS instanceof Set) {
            window.__SONDER_DIAG_CLEARERS.delete(diagClearHook);
        }
    }
    registerDiagClearHook();

    notifyTransport();

    return {
        renderFrame,
        togglePlayback,
        startPlayback,
        stopPlayback,
        captureSourceFrame,
        clearMediaCache,
        clearPlaybackWarmState,
        invalidatePlaybackComposite,
        destroy,
        setLiveMediaEnabled,
        isLiveMediaEnabled: () => state.liveMediaEnabled,
        isPlaying: () => state.isPlaying,
    };
}
