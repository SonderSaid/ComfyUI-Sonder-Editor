// Chromium 4:4:4 newly-decoded video: 0.500x = 16 ms, 0.508x = 1 ms.
// Retire after that probe is fast across supported browsers. DPR > 1 remains
// untested; the margin covers integer destination rounding. Zero disables it.
export const VIDEO_SCRATCH_SCALE_THRESHOLD = 0.55;

export function videoDestDownscale(mode, srcW, srcH, canvasW, canvasH) {
    if (![srcW, srcH, canvasW, canvasH].every((v) => Number.isFinite(v) && v > 0)) return Infinity;
    const sx = canvasW / srcW, sy = canvasH / srcH;
    return mode === "cover" ? Math.max(sx, sy) : Math.min(sx, sy);
}

export function createVideoScratchCache({
    createCanvas = () => typeof document === "undefined" ? null : document.createElement("canvas"),
    // Four dimension families bound alternating HD/UHD/aspect formats. 32 MP
    // holds three UHD buffers plus HD (~108 MB RGBA), capped at 128 MB/surface.
    maxEntries = 4, maxTotalPixels = 32e6,
} = {}) {
    const entries = new Map();
    let totalPixels = 0, sequence = 0;
    function release(key, entry) {
        entry.canvas.width = 0;
        entry.canvas.height = 0;
        totalPixels -= entry.pixels;
        entries.delete(key);
    }
    return {
        acquire(w, h, passSeq) {
            const pixels = w * h;
            if (!Number.isInteger(w) || !Number.isInteger(h) || w <= 0 || h <= 0
                || pixels > maxTotalPixels || maxEntries < 1) return null;
            const key = `${w}x${h}`;
            let entry = entries.get(key);
            if (!entry) {
                while (entries.size >= maxEntries || totalPixels + pixels > maxTotalPixels) {
                    let candidate = null;
                    for (const pair of entries) {
                        if (pair[1].lastPassSeq !== passSeq && (!candidate || pair[1].lastUsed < candidate[1].lastUsed)) candidate = pair;
                    }
                    if (!candidate) return null;
                    release(...candidate);
                }
                let canvas, ctx;
                try {
                    canvas = createCanvas();
                    if (!canvas) return null;
                    canvas.width = w;
                    canvas.height = h;
                    ctx = canvas.getContext("2d");
                } catch {
                    if (canvas) { canvas.width = 0; canvas.height = 0; }
                    return null;
                }
                if (!ctx) { canvas.width = 0; canvas.height = 0; return null; }
                entry = { canvas, ctx, pixels, lastPassSeq: passSeq, lastUsed: 0 };
                entries.set(key, entry);
                totalPixels += pixels;
            }
            entry.lastPassSeq = passSeq;
            entry.lastUsed = ++sequence;
            return entry;
        },
        clear() { for (const [key, entry] of entries) release(key, entry); },
        stats() { return { entries: entries.size, totalPixels }; },
    };
}
