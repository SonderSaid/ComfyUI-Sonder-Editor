// Waveform peaks of an audio blob, decoded in the browser and never played. One
// decoder, two readers: an output pane's bars (output_pane_media.js) and the
// inspect overlay's waveform for a View's audio (view_inspect_items.js). A
// project asset's waveform comes from the server instead.

// Channel 0 of the audio at `url`, or null when it can't be decoded or was aborted.
export async function decodeChannel(url, { signal = null, fetchImpl = (...args) => globalThis.fetch(...args) } = {}) {
    try {
        const buffer = await (await fetchImpl(url, signal ? { signal } : undefined)).arrayBuffer();
        const Offline = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
        if (!Offline || signal?.aborted) return null;
        const audio = await new Offline(1, 1, 44100).decodeAudioData(buffer);
        return signal?.aborted ? null : audio.getChannelData(0);
    } catch (_error) {
        // A waveform is decoration; the transport still plays.
        return null;
    }
}

// `count` [min, max] pairs of `data`. Bucket i covers the samples from
// floor(i * step) up to floor((i + 1) * step), none past the end ([0, 0]). The
// default step spreads every sample over the buckets.
export function minMaxPeaks(data, count, step = data.length / Math.max(1, count)) {
    const peaks = [];
    for (let i = 0; i < count; i++) {
        const from = Math.floor(i * step), to = Math.min(data.length, Math.floor((i + 1) * step));
        if (from >= to) { peaks.push([0, 0]); continue; }
        let min = Infinity, max = -Infinity;
        for (let j = from; j < to; j++) {
            const value = data[j];
            if (value < min) min = value;
            if (value > max) max = value;
        }
        peaks.push([min, max]);
    }
    return peaks;
}
