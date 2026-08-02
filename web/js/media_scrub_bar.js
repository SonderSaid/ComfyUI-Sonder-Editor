function clamp(value, min, max) {
    return Math.min(max, Math.max(min, Number(value) || 0));
}

export function normalizeMediaWindow(startSec, endSec, durationSec) {
    const duration = Math.max(0, Number(durationSec) || 0);
    if (!(duration > 0)) return { start: 0, end: 0, duration: 0 };
    const start = clamp(startSec, 0, duration);
    const rawEnd = endSec == null || endSec === "" ? duration : Number(endSec);
    const end = clamp(Number.isFinite(rawEnd) ? rawEnd : duration, start, duration);
    return { start, end, duration: Math.max(0, end - start) };
}

export function mediaTimeToWindowRatio(timeSec, windowValue) {
    const span = Math.max(0, Number(windowValue?.duration) || 0);
    if (!(span > 0)) return 0;
    return clamp((Number(timeSec || 0) - windowValue.start) / span, 0, 1);
}

export function mediaWindowTimeFromRatio(ratio, windowValue) {
    return Number(windowValue?.start || 0)
        + clamp(ratio, 0, 1) * Math.max(0, Number(windowValue?.duration) || 0);
}

function formatClockTime(seconds) {
    const safe = Math.max(0, Number(seconds) || 0);
    const whole = Math.floor(safe);
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export function formatMediaScrubLabel(currentSeconds, windowValue, { fps = 0, relative = false } = {}) {
    const current = clamp(currentSeconds, windowValue.start, windowValue.end || windowValue.start);
    if (relative) {
        const elapsed = Math.max(0, current - windowValue.start);
        return `${formatClockTime(elapsed)} / ${formatClockTime(windowValue.duration)} · source ${formatClockTime(current)}`;
    }
    const clock = `${formatClockTime(current)} / ${formatClockTime(windowValue.end)}`;
    const safeFps = Number(fps) || 0;
    if (!(safeFps > 0) || !(windowValue.end > 0)) return clock;
    const totalFrames = Math.max(1, Math.round(windowValue.end * safeFps));
    const currentFrame = clamp(Math.round(current * safeFps), 0, totalFrames);
    return `${clock} · f${currentFrame}/${totalFrames}`;
}

export function mountMediaScrubBar(mediaEl, options = {}) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid rgba(120,145,165,.25);";
    const track = document.createElement("div");
    track.style.cssText = "position:relative;flex:1 1 auto;height:10px;border-radius:999px;background:#1a2631;cursor:pointer;overflow:hidden;touch-action:none;";
    track.dataset.sonderMediaScrubTrack = "1";
    const fill = document.createElement("div");
    fill.style.cssText = "position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,#6fa7d8,#8fc0f0);";
    const thumb = document.createElement("div");
    thumb.style.cssText = "position:absolute;top:50%;width:12px;height:12px;border-radius:50%;background:#d9ebfb;border:1px solid rgba(0,0,0,0.35);transform:translate(-50%,-50%);left:100%;pointer-events:none;box-shadow:0 1px 3px rgba(0,0,0,0.35);";
    fill.appendChild(thumb);
    track.appendChild(fill);
    const label = document.createElement("div");
    label.style.cssText = "color:#a9bccb;font-size:10px;white-space:nowrap;min-width:72px;text-align:right;";
    wrap.append(track, label);

    let destroyed = false;
    let activePointerId = null;
    let windowStartSec = Number(options.windowStartSec) || 0;
    let windowEndSec = options.windowEndSec ?? null;
    let relativeLabel = options.relativeLabel === true;

    const duration = () => {
        const value = Number(mediaEl?.duration);
        return Number.isFinite(value) && value > 0 ? value : 0;
    };
    const activeWindow = () => normalizeMediaWindow(windowStartSec, windowEndSec, duration());

    const refresh = () => {
        if (destroyed) return;
        const mediaWindow = activeWindow();
        const current = clamp(Number(mediaEl?.currentTime) || 0, mediaWindow.start, mediaWindow.end || mediaWindow.start);
        const ratio = mediaTimeToWindowRatio(current, mediaWindow);
        fill.style.width = `${ratio * 100}%`;
        const enabled = mediaWindow.duration > 0;
        track.style.cursor = enabled ? "pointer" : "not-allowed";
        track.setAttribute("aria-disabled", enabled ? "false" : "true");
        label.textContent = enabled
            ? formatMediaScrubLabel(current, mediaWindow, { fps: options.fps, relative: relativeLabel })
            : "Loading duration…";
    };

    const seekFromClientX = (clientX) => {
        const mediaWindow = activeWindow();
        if (!(mediaWindow.duration > 0)) return;
        const rect = track.getBoundingClientRect();
        const ratio = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
        mediaEl.currentTime = mediaWindowTimeFromRatio(ratio, mediaWindow);
        options.onSeek?.(mediaEl.currentTime, mediaWindow);
        refresh();
    };
    const pointerMove = (event) => {
        if (event.pointerId !== activePointerId) return;
        seekFromClientX(event.clientX);
    };
    const pointerUp = (event) => {
        if (event.pointerId !== activePointerId) return;
        seekFromClientX(event.clientX);
        try { track.releasePointerCapture?.(activePointerId); } catch { /* ignore */ }
        activePointerId = null;
    };
    const pointerDown = (event) => {
        if (!(activeWindow().duration > 0)) return;
        event.preventDefault();
        activePointerId = event.pointerId;
        track.setPointerCapture?.(event.pointerId);
        seekFromClientX(event.clientX);
    };

    track.addEventListener("pointerdown", pointerDown);
    track.addEventListener("pointermove", pointerMove);
    track.addEventListener("pointerup", pointerUp);
    track.addEventListener("pointercancel", pointerUp);
    for (const eventName of ["timeupdate", "loadedmetadata", "durationchange", "ended"]) {
        mediaEl?.addEventListener?.(eventName, refresh);
    }
    refresh();

    return {
        el: wrap,
        setWindow({ startSec = 0, endSec = null, relative = false } = {}) {
            windowStartSec = Number(startSec) || 0;
            windowEndSec = endSec;
            relativeLabel = relative === true;
            refresh();
        },
        refresh,
        cleanup() {
            if (destroyed) return;
            destroyed = true;
            if (activePointerId != null) {
                try { track.releasePointerCapture?.(activePointerId); } catch { /* ignore */ }
            }
            activePointerId = null;
            track.removeEventListener("pointerdown", pointerDown);
            track.removeEventListener("pointermove", pointerMove);
            track.removeEventListener("pointerup", pointerUp);
            track.removeEventListener("pointercancel", pointerUp);
            for (const eventName of ["timeupdate", "loadedmetadata", "durationchange", "ended"]) {
                mediaEl?.removeEventListener?.(eventName, refresh);
            }
        },
    };
}
