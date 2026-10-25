// Output Views linked playback (Phase 3B): how an output's frames sit on the
// scene that fed it, and whether that scene changed in the output's range
// since. Pure: no DOM, app or network.
//
// A pane links an output in one of two ways:
// - trusted: a Save Video take's `scene_map`, the same placement as its take
//   clip (offset in scene frames, played at the scene rate);
// - authored: the View's `timing` resolved against the Editor's executed
//   window (`editor_context`), its offset in output frames at the output rate.
// Frames are sampled at their centres (`c = frame + 0.5`), as the editor's own
// clips are (`sourceFrameTime`); intervals are half-open.
//
// "Previous context" compares the Editor's recorded content digest with the
// same projection built here from the scene the editor holds. The projection
// and canonical text mirror server/view_timing.py exactly (parity-tested).
import { twoLaneHash } from "./text_hash.js";

export const TIMING_MODES = Object.freeze(["editor_window"]);
export const TIMING_ANCHORS = Object.freeze(["render_start", "generation_start"]);
export const MAX_SOURCE_OFFSET = 1000000;
export const MAX_FPS = 240;
const EPS = 1e-9;

const isPlainObject = value => !!value && typeof value === "object" && !Array.isArray(value);

// ── Timing (server/view_timing.py clean_timing) ──────────────────────────────

export function normalizeTiming(raw) {
    if (!isPlainObject(raw)) return null;
    if (!TIMING_MODES.includes(raw.mode) || !TIMING_ANCHORS.includes(raw.anchor)) return null;
    const offset = Object.hasOwn(raw, "source_offset") ? raw.source_offset : 0;
    if (typeof offset !== "number" || !Number.isInteger(offset) || offset < 0 || offset > MAX_SOURCE_OFFSET) return null;
    const fps = Object.hasOwn(raw, "fps") ? raw.fps : "editor";
    if (fps !== "editor" && (typeof fps !== "number" || !Number.isFinite(fps) || !(fps > 0 && fps <= MAX_FPS))) return null;
    return { mode: raw.mode, anchor: raw.anchor, source_offset: offset, fps };
}

// ── Linking ──────────────────────────────────────────────────────────────────

export const UNLINKED = Object.freeze({
    no_timing: "No timing for this output. Set it in Manage ▸ Views",
    no_editor: "No Editor fed this output",
    other_scene: "Made for another scene",
    several_editors: "Several Editors fed this output",
    reused: "A reused result whose timing isn't known",
    too_short: "Shorter than the range it was made for",
});
export const fpsMismatchText = (made, plays) => `Made at ${+Number(made).toFixed(3)} fps; this scene plays at ${+Number(plays).toFixed(3)}`;

export const isLinkable = entry => entry?.kind === "video" || entry?.kind === "audio";

// The Editor context a pane resolves against (contexts: {editorKey: ctx}).
export function pickContext(contexts, { projectId = "", sceneId = "", editorKey = "" } = {}) {
    const keys = isPlainObject(contexts) ? Object.keys(contexts) : [];
    if (!keys.length) return { reason: "no_editor" };
    const matching = keys.filter(key => contexts[key]?.project_folder === projectId && contexts[key]?.scene_id === sceneId);
    if (!matching.length) return { reason: "other_scene" };
    if (matching.length === 1) return { key: matching[0], context: contexts[matching[0]] };
    const own = matching.filter(key => editorKey && key === editorKey);
    return own.length === 1 ? { key: own[0], context: contexts[own[0]] } : { reason: "several_editors" };
}

const int = value => (Number.isFinite(Number(value)) ? Math.round(Number(value)) : 0);

// The link for one entry, or why it does not link. `durationSec` is the
// media's duration once known (null before): the length check needs it.
// Returns {linked: true, kind, S0, S1, Fm, required, map(c), audioMap(c), contextKey, context}
// | {linked: false, reason, text}.
export function resolveLink({ entry, timing = null, contexts = null, fresh = true, editor = {}, durationSec = null } = {}) {
    const unlinked = (reason, text = UNLINKED[reason] || "") => ({ linked: false, reason, text });
    if (!isLinkable(entry)) return unlinked("not_linkable", "");
    const sceneMap = isPlainObject(entry.scene_map) ? entry.scene_map : null;
    const clean = normalizeTiming(timing);
    if (!sceneMap && !clean) return unlinked("no_timing");
    const hasContexts = isPlainObject(contexts) && Object.keys(contexts).length > 0;
    // Freshness unknown (null: a result recovered from history) reads as reused too.
    if (!hasContexts) return unlinked(fresh === false || fresh === null ? "reused" : "no_editor");
    const picked = pickContext(contexts, editor);
    if (!picked.context) return unlinked(picked.reason);
    const ctx = picked.context;
    const F = Number(editor.fps);
    const made = Number(ctx.fps);
    if (!(F > 0) || !(made > 0) || Math.abs(made - F) >= 1e-6) return unlinked("fps", fpsMismatchText(made, F));
    let S0, S1, Fm, required, sourceTime;
    if (sceneMap) {
        if (sceneMap.scene_id !== editor.sceneId) return unlinked("other_scene");
        S0 = int(sceneMap.scene_start);
        S1 = int(sceneMap.scene_end);
        const o = int(sceneMap.source_offset);
        Fm = Number(sceneMap.fps) > 0 ? Number(sceneMap.fps) : Number(entry.fps) > 0 ? Number(entry.fps) : F;
        // The take clip's own placement: offset in scene frames, at the scene rate.
        sourceTime = c => (o + c - S0) / F;
        required = Math.floor((o + S1 - S0 - 0.5) * Fm / F + EPS) + 1;
    } else {
        const generation = clean.anchor === "generation_start";
        S0 = int(generation ? ctx.generation_start : ctx.render_start);
        const span = generation ? int(ctx.generation_end) - int(ctx.generation_start)
            : (Number.isFinite(Number(ctx.source_frame_count)) ? int(ctx.source_frame_count) : int(ctx.render_end) - int(ctx.render_start));
        S1 = S0 + Math.max(0, span);
        const o = clean.source_offset;
        Fm = clean.fps === "editor" ? made : clean.fps;
        sourceTime = c => (o + Math.floor((c - S0) * Fm / F + EPS) + 0.5) / Fm;
        required = o + Math.floor((S1 - S0 - 0.5) * Fm / F + EPS) + 1;
    }
    if (!(S1 > S0)) return unlinked("too_short");
    const duration = Number(durationSec);
    if (Number.isFinite(duration) && duration > 0 && Math.floor(duration * Fm + 0.5) < required) return unlinked("too_short");
    const limit = Number.isFinite(duration) && duration > 0 ? Math.max(0, duration - 0.5 / Fm) : Infinity;
    const inRange = c => c - 0.5 >= S0 && c - 0.5 < S1;
    const offsetSec = sceneMap ? 0 : clean.source_offset / Fm;
    const audioStart = sceneMap ? int(sceneMap.source_offset) / F : 0;
    return {
        linked: true, kind: sceneMap ? "trusted" : "authored", S0, S1, Fm, required, contextKey: picked.key, context: ctx,
        // `c` is a frame centre (frame + 0.5), fractional while playing.
        map: c => (inRange(c) ? { inRange: true, sourceTime: Math.min(limit, Math.max(0, sourceTime(c))) } : { inRange: false }),
        // Audio has no frames: continuous time, timeline audio's convention.
        audioMap: c => (inRange(c) ? { inRange: true, sourceTime: Math.min(limit, Math.max(0, (c - S0) / F + offsetSec + audioStart)) }
            : { inRange: false }),
    };
}

// ── Content projection (server/view_timing.py content_text) ──────────────────

function compareUnits(a, b) {
    const n = Math.min(a.length, b.length);
    for (let i = 0; i < n; i++) if (a.charCodeAt(i) !== b.charCodeAt(i)) return a.charCodeAt(i) - b.charCodeAt(i);
    return a.length - b.length;
}

// Python float(): a bool is 0 or 1; null, "", objects and lists do not convert.
const pyFloat = value => {
    if (typeof value === "boolean") return value ? 1 : 0;
    if (value === null || value === undefined || typeof value === "object" || (typeof value === "string" && !value.trim())) return NaN;
    return Number(value);
};

export function micro(value) {
    const number = pyFloat(value);
    if (!Number.isFinite(number)) return -1;
    return Math.floor(number * 1e6 + 0.5);
}

export function canonicalText(value) {
    if (typeof value === "boolean") return value ? "1" : "0";
    if (typeof value === "number") return String(value);
    if (typeof value === "string") return `${value.length}:${value}`;
    if (Array.isArray(value)) return `[${value.map(canonicalText).join(",")}]`;
    throw new TypeError(`Not canonical: ${typeof value}`);
}

const get = (item, name, fallback) => {
    const value = item?.[name];
    return value === null || value === undefined ? fallback : value;
};
const roundInt = value => {
    const number = pyFloat(value);
    return Number.isFinite(number) ? Math.floor(number + 0.5) : 0;
};

// server/view_timing.py normalize_source: "/" separators, no leading "./",
// case kept (Python and JavaScript case mapping differ).
export function normalizeSource(path) {
    let value = String(path || "").replaceAll("\\", "/");
    while (value.startsWith("./")) value = value.slice(2);
    return value;
}

const laneHidden = (configs, index) => {
    const list = Array.isArray(configs) ? configs : [];
    return index >= 0 && index < list.length ? !!get(list[index], "hidden", false) : false;
};

function overlap(item, window) {
    const start = roundInt(get(item, "timeline_start_frame", 0)), end = roundInt(get(item, "timeline_end_frame", 0));
    const lo = Math.max(start, window[0]), hi = Math.min(end, window[1]);
    if (hi <= lo) return null;
    return [lo, hi, roundInt(get(item, "source_in_frame", 0)) + lo - start];
}

const rowOrder = (a, b) => a[0] - b[0] || a[3] - b[3] || a[4] - b[4] || compareUnits(a[2], b[2]);

function clipRows(clips, window, configs, { strength }) {
    const rows = [];
    for (const clip of clips) {
        const found = overlap(clip, window);
        if (!found) continue;
        const lane = roundInt(get(clip, "track_index", 0));
        rows.push([lane, laneHidden(configs, lane), normalizeSource(get(clip, "source_path", "")), found[0], found[1], found[2],
            micro(get(clip, strength ? "strength" : "opacity", 1.0)), !!get(clip, "muted", false),
            String(get(clip, "fit_mode", "pad_edge") || "pad_edge"), String(get(clip, "crop_position", "center") || "center")]);
    }
    return rows.sort(rowOrder);
}

function audioRows(tracks, window, configs) {
    const rows = [];
    for (const track of tracks) {
        const found = overlap(track, window);
        if (!found) continue;
        const lane = roundInt(get(track, "lane_index", 0));
        rows.push([lane, laneHidden(configs, lane), normalizeSource(get(track, "source_path", "")), found[0], found[1], found[2],
            micro(get(track, "volume", 1.0)), !!get(track, "muted", false)]);
    }
    return rows.sort(rowOrder);
}

function guideRows(guides, window, { trackHidden, duration, fit }) {
    if (trackHidden) return [];
    const rows = [];
    for (const guide of guides) {
        if (get(guide, "muted", false)) continue;
        let at = roundInt(get(guide, "frame_index", 0));
        if (at === -1) at = duration - 1;
        if (!(window[0] <= at && at < window[1])) continue;
        const row = [at, String(get(guide, "asset_id", "") || ""), micro(get(guide, "strength", 1.0))];
        if (fit) row.push(String(get(guide, "fit_mode", "pad_edge") || "pad_edge"), String(get(guide, "crop_position", "center") || "center"));
        rows.push(row);
    }
    return rows.sort((a, b) => a[0] - b[0] || compareUnits(a[1], b[1]) || a[2] - b[2]
        || compareUnits(a[3] || "", b[3] || "") || compareUnits(a[4] || "", b[4] || ""));
}

// The canonical text of a scene's inputs inside `window` (clips, audio) and
// `inputWindow` (Drivers, guides): server/view_timing.py content_text.
export function contentText({ fps, width, height, window, inputWindow, guideFit, renderClips = [], renderConfigs = [],
    driverClips = [], driverConfigs = [], guides = [], guideTrackHidden = false, duration = 0, audioTracks = [], audioConfigs = [] }) {
    return canonicalText(["v1", micro(fps), roundInt(width), roundInt(height), !!guideFit,
        clipRows(renderClips, window, renderConfigs, { strength: false }),
        clipRows(driverClips, inputWindow, driverConfigs, { strength: true }),
        guideRows(guides, inputWindow, { trackHidden: guideTrackHidden, duration: roundInt(duration), fit: guideFit }),
        audioRows(audioTracks, window, audioConfigs)]);
}

const isDriver = clip => clip?.role === "motion_driver";

// Whether the scene changed in an output's range since its run: "same",
// "changed", or "unknown" (no record, or a new audio track whose asset is not
// loaded or not in the asset list yet). New generated items (a take and its audio, the run's own or a
// later one) never count.
// helpers: {fps, width, height, assetForSourcePath(path) -> asset|null, assetsLoaded}.
export function contentStatus(scene, content, helpers = {}) {
    if (!scene || !isPlainObject(content) || content.v !== 1 || typeof content.digest !== "string"
        || !Array.isArray(content.window) || !Array.isArray(content.input_window)) return "unknown";
    const present = new Set((content.present || []).map(String));
    const clips = Array.isArray(scene.clips) ? scene.clips : [];
    // An id-less clip is never ignored: "" would drop every id-less clip.
    const ignoredClips = new Set(clips.filter(clip => clip?.is_generated && clip.clip_id && !present.has(String(clip.clip_id)))
        .map(clip => String(clip.clip_id)));
    // An audio track linked to an ignored take clip goes with it.
    const linkedToIgnored = new Set();
    for (const group of Array.isArray(scene.linked_item_groups) ? scene.linked_item_groups : []) {
        const items = Array.isArray(group?.items) ? group.items : [];
        if (!items.some(item => item?.type === "clip" && ignoredClips.has(String(item.id || "")))) continue;
        for (const item of items) if (item?.type === "audio") linkedToIgnored.add(String(item.id || ""));
    }
    const audio = [];
    for (const track of Array.isArray(scene.audio_tracks) ? scene.audio_tracks : []) {
        const id = String(track?.track_id || "");
        if (present.has(id)) { audio.push(track); continue; }
        if (linkedToIgnored.has(id)) continue;
        // A new track: a take's audio (its asset has provenance) or the user's.
        if (!overlap(track, content.window)) continue;
        if (!helpers.assetsLoaded) return "unknown";
        const asset = helpers.assetForSourcePath?.(track.source_path) || null;
        // Not in the asset list yet (a take's audio lands before the list refreshes).
        if (!asset) return "unknown";
        if (asset.has_provenance) continue;
        audio.push(track);
    }
    const kept = clips.filter(clip => !ignoredClips.has(String(clip?.clip_id || "")));
    const text = contentText({
        fps: helpers.fps, width: helpers.width, height: helpers.height,
        window: content.window.map(roundInt), inputWindow: content.input_window.map(roundInt), guideFit: !!content.guide_fit,
        renderClips: kept.filter(clip => !isDriver(clip)), renderConfigs: scene.video_lane_configs,
        driverClips: kept.filter(isDriver), driverConfigs: scene.motion_driver_lane_configs,
        guides: Array.isArray(scene.guide_frames) ? scene.guide_frames : [], guideTrackHidden: !!scene.guide_track_config?.hidden,
        duration: scene.duration_frames, audioTracks: audio, audioConfigs: scene.audio_lane_configs,
    });
    return twoLaneHash(text) === content.digest ? "same" : "changed";
}
