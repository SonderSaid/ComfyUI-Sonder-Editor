// Render Queue wording shared by the queue panel and run notifications.
//
// DOM-free and import-free, so the run notice model (run_feedback_notice.js)
// words a queued job exactly as the queue panel (shared_render_queue.js) does.

export function formatQueueTime(frame, fps, mode = "frames") {
    const safeFrame = Math.max(0, parseInt(frame, 10) || 0);
    if (mode !== "timecode") return String(safeFrame);
    const safeFps = Math.max(1, Number(fps) || 24);
    const totalSeconds = safeFrame / safeFps;
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = Math.floor(totalSeconds % 60);
    const f = Math.floor(safeFrame % safeFps);
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
}

export function formatQueueSelectionSummary(job, options = {}) {
    const start = Math.max(0, parseInt(job?.selection_start, 10) || 0);
    const end = Math.max(start, parseInt(job?.selection_end, 10) || 0);
    const duration = end - start;
    const preContext = Math.max(0, parseInt(job?.pre_context_frames, 10) || 0);
    const postContext = Math.max(0, parseInt(job?.post_context_frames, 10) || 0);
    const maskPre = Math.max(0, parseInt(job?.mask_pre_offset, 10) || 0);
    const maskPost = Math.max(0, parseInt(job?.mask_post_offset, 10) || 0);
    const fps = Math.max(1, Number(job?.scene_fps) || Number(options.fps) || 24);
    const mode = options.mode === "timecode" ? "timecode" : "frames";
    return `In: ${formatQueueTime(start, fps, mode)} Out: ${formatQueueTime(end, fps, mode)} (${formatQueueTime(duration, fps, mode)}) | Ctx: -${preContext}/+${postContext} | Mask Offset: -${maskPre}/+${maskPost}`;
}

// "Chunk k of N" for a batch job (batch_index is 0-based): the queue panel's
// batch row titles and run receipts both word it here.
export function queueChunkLabel(batchIndex, batchTotal, fallbackIndex = 0) {
    const index = Math.max(1, (parseInt(batchIndex, 10) || fallbackIndex) + 1);
    return `Chunk ${index} of ${batchTotal}`;
}
