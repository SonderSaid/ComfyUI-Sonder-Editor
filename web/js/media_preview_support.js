// Leaf module (no DOM, no imports) answering one question: can this browser
// actually decode this asset's video track? Shared by the gallery inspector,
// compare view, detail panel and the Reference media editor.
//
// This is NOT the same question as `browser_preview_compatible`, and that flag
// must not be used here. It is derived as `mp4 + libx264 + yuv420p`
// (server/media_helpers.py), so Editing Master MP4 carries `false` despite
// decoding perfectly — gating on it would suppress a working preview, and after
// the all-intra change Editing Master is the *best*-scrubbing format we write.
// Its real meaning is "safe for someone else's browser", not "shows a picture
// in ours". It is also absent from the lean /assets payload.
//
// The failure takes two forms, and both are current (Chromium 152, measured for
// ProRes and FFV1 on 2026-10-21):
// - LOUD, a file with no audio track: `error.code === 4` at readyState 0.
// - SILENT, a file with an audio track: readyState 4 with `error === null`; the
//   audio decodes and `currentTime` advances while `videoWidth`/`videoHeight`
//   stay 0 and no frame is ever presented.
// `mediaFailedToDecode` catches both. Never treat the absence of an `error`
// event as proof a video is playing.
//
// Whether an asset needs a proxy is the server's decision (`asset.proxy`, read
// through `media_display_source.js`); the hint below is only the fallback for a
// payload that carries no decision yet.

// Codecs Chromium ships no video decoder for. Values match what the encoder
// records in `generation_summary.codec`.
// EXPIRY: remove an entry when Chromium ships a decoder for it; the runtime
// check below would then start disagreeing with the hint, and the hint loses.
export const UNDECODABLE_VIDEO_CODECS = Object.freeze(new Set(["prores", "prores_ks", "ffv1"]));

// Save presets whose output uses one of those codecs, for assets that recorded
// a preset but no explicit codec.
const UNDECODABLE_SAVE_PRESETS = Object.freeze(new Set(["ProRes 422 HQ", "Lossless FFV1 (RGB)"]));

/**
 * Static, provenance-only hint. Returns "undecodable" ONLY on positive evidence
 * from what the editor itself recorded at encode time; everything else is
 * "unknown", which means "load it and let the runtime check decide".
 *
 * Deliberately does NOT look at the file extension. A `.mov` can hold H.264 and
 * a `.mkv` can hold VP9 — both decode fine, and both arrive as imports with no
 * recorded codec. Suppressing on extension would be terminal: it skips the
 * load, so `loadeddata` never fires and the runtime check can never correct it.
 * There is no "decodable" return value for the same reason — absence of
 * evidence is not evidence.
 */
export function videoPreviewHint(asset) {
    const summary = asset && typeof asset === "object" ? asset.generation_summary : null;
    if (!summary || typeof summary !== "object") return "unknown";
    const codec = String(summary.codec || "").toLowerCase();
    if (codec && UNDECODABLE_VIDEO_CODECS.has(codec)) return "undecodable";
    if (!codec && UNDECODABLE_SAVE_PRESETS.has(String(summary.save_preset || ""))) {
        return "undecodable";
    }
    return "unknown";
}

/**
 * Runtime verdict, and the authoritative one. Takes a plain property bag so the
 * leaf stays DOM-free and testable under Node with an object literal.
 *
 * `readyState >= 1` (HAVE_METADATA) is required: before metadata arrives every
 * video reports videoWidth 0, so checking earlier would flash "unsupported" at
 * a file that decodes perfectly.
 */
export function videoTrackFailedToDecode(state) {
    if (!state || typeof state !== "object") return false;
    const readyState = Number(state.readyState) || 0;
    if (readyState < 1) return false;
    const width = Number(state.videoWidth) || 0;
    const height = Number(state.videoHeight) || 0;
    return width === 0 || height === 0;
}

/** True when this asset should never be handed to a `<video>` element at all. */
export function shouldSkipVideoLoad(asset) {
    return videoPreviewHint(asset) === "undecodable";
}

// MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED: the loud form above.
const MEDIA_ERR_SRC_NOT_SUPPORTED = 4;

// Chromium names the pipeline stage that failed in `error.message`; a demuxer or
// decoder stage means the bytes arrived and could not be read.
const DECODE_STAGE_MESSAGE = /DEMUXER_ERROR|DECODER_ERROR|PIPELINE_ERROR_DECODE|no supported streams/i;

/**
 * Either failure form, from an element or a plain property bag
 * `{ error: { code }, readyState, videoWidth, videoHeight }`: no picture will come.
 * A fetch failure (404, 5xx, refused) also gives the loud form, so this alone is
 * not a codec verdict; see `classifyMediaFailure`.
 */
export function mediaFailedToDecode(element) {
    if (!element || typeof element !== "object") return false;
    if (Number(element.error?.code) === MEDIA_ERR_SRC_NOT_SUPPORTED) return true;
    return videoTrackFailedToDecode(element);
}

/**
 * Whether a failure says something about the codec. "decode": these bytes never
 * give a picture here (the silent form, a decode-stage message, or a local blob,
 * whose bytes were fetched). "unconfirmed": the loud form with nothing to tell it
 * from a fetch failure; only a reachable source makes it a decode failure. "": none.
 */
export function classifyMediaFailure(element) {
    if (!element || typeof element !== "object") return "";
    if (Number(element.error?.code) === MEDIA_ERR_SRC_NOT_SUPPORTED) {
        const src = String(element.currentSrc || element.src || "");
        if (src.startsWith("blob:") || DECODE_STAGE_MESSAGE.test(String(element.error?.message || ""))) {
            return "decode";
        }
        return "unconfirmed";
    }
    // Aborted, network and mid-stream errors are not a verdict on the codec.
    if (element.error) return "";
    return videoTrackFailedToDecode(element) ? "decode" : "";
}
