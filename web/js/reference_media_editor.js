import {
    fitReferenceCropToAspect,
    moveReferenceCrop,
    normalizeReferenceCropPercent,
    normalizeReferenceTrim,
    referenceMediaWindow,
    resizeReferenceCrop,
    resizeReferenceCropLocked,
    setReferenceCropLockedSize,
    shiftReferenceTrim,
    sliceReferenceWaveformPeaks,
} from "./reference_library_model.js";
import { ASPECT_RATIO_PRESETS } from "./editor_settings.js";
import { register as registerKeyboardConsumer, PRIORITY as KEY_PRIORITY } from "./keyboard_ownership.js";
import { mountMediaScrubBar } from "./media_scrub_bar.js";

export const REFERENCE_MEDIA_EDITOR_SHORTCUTS = Object.freeze([
    ["Space", "Play / Pause the active Reference Source/Result view"],
    ["Arrow keys", "Focused crop: move/resize by 1%"],
    ["Shift + Arrow keys", "Focused crop: move/resize by 5%"],
    ["Left / Right", "Focused trim handle/selection: adjust by 0.1 seconds"],
    ["Shift + Left / Right", "Focused trim handle/selection: adjust by 1 second"],
    ["Esc", "Close or cancel the Reference media editor"],
]);

const BUTTON = "border:1px solid #43505c;border-radius:7px;background:#252e37;color:#e6ebf0;padding:6px 9px;font:11px 'Segoe UI',sans-serif;cursor:pointer;";
const INPUT = "width:100%;box-sizing:border-box;background:#151a20;border:1px solid #38414b;border-radius:6px;color:#e6ebf0;padding:6px 8px;font:11px 'Segoe UI',sans-serif;";

function node(tag, text = "", style = "") {
    const result = document.createElement(tag);
    if (text) result.textContent = text;
    if (style) result.style.cssText = style;
    return result;
}

function finite(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
}

function cropIsFull(crop) {
    return Math.abs(crop.x) < 1e-6 && Math.abs(crop.y) < 1e-6
        && Math.abs(crop.w - 100) < 1e-6 && Math.abs(crop.h - 100) < 1e-6;
}

function formatSeconds(value) {
    return `${finite(value).toFixed(2)}s`;
}

export function openReferenceMediaEditor({
    asset,
    crop = null,
    sourceStartSec = 0,
    sourceEndSec = null,
    mediaUrl = "",
    waveformUrl = "",
    readOnly = false,
    initialViewMode = "source",
    onViewModeChange = () => {},
    keyboardConsumerId = "sonder-reference-media-editor",
    onApply = () => {},
    onClose = () => {},
} = {}) {
    if (!asset || !mediaUrl) return null;
    const opener = document.activeElement;
    const mediaType = String(asset.asset_type || "");
    const visual = mediaType === "image" || mediaType === "video";
    const timed = mediaType === "audio" || mediaType === "video";
    const cleanup = [];
    const abortController = new AbortController();
    let destroyed = false;
    let cropValue = normalizeReferenceCropPercent(crop);
    let cropNull = crop == null;
    let startValue = Math.max(0, finite(sourceStartSec));
    let endValue = sourceEndSec == null || sourceEndSec === "" ? null : finite(sourceEndSec);
    let endIsRemainder = endValue == null;
    let duration = Math.max(0, finite(asset.duration_sec));
    let viewMode = initialViewMode === "result" ? "result" : "source";
    let waveformPeaks = null;
    let waveformLoaded = false;
    let ratioMode = "free";
    let customRatioWidth = "";
    let customRatioHeight = "";
    let ratioError = "";
    let playbackFrame = 0;

    const overlay = node("div", "", "position:fixed;inset:0;z-index:100003;background:rgba(5,8,11,.9);display:flex;align-items:stretch;justify-content:center;padding:20px;box-sizing:border-box;");
    overlay.dataset.sonderReferenceMediaEditor = "1";
    const shell = node("section", "", "width:min(1040px,100%);height:100%;min-height:0;border:1px solid #34414c;border-radius:12px;background:#11171c;box-shadow:0 24px 70px rgba(0,0,0,.55);display:flex;flex-direction:column;overflow:hidden;outline:none;color:#e6ebf0;font-family:'Segoe UI',sans-serif;");
    shell.tabIndex = -1;
    const header = node("header", "", "display:flex;align-items:center;gap:10px;padding:11px 13px;border-bottom:1px solid #303941;flex:0 0 auto;");
    const title = node("div", asset.name || asset.path || "Reference media", "font-size:13px;font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;");
    const mode = node("div", "", "display:flex;gap:3px;");
    const sourceButton = node("button", "Full Source", BUTTON);
    const resultButton = node("button", "Applied Result", BUTTON);
    const closeButton = node("button", "Close", BUTTON);
    sourceButton.type = resultButton.type = closeButton.type = "button";
    mode.append(sourceButton, resultButton);
    header.append(title, mode, closeButton);
    const content = node("div", "", "display:flex;flex-direction:column;gap:10px;padding:12px;min-height:0;overflow:auto;flex:1;");
    const stage = node("div", "", "position:relative;display:flex;align-items:center;justify-content:center;min-height:260px;flex:1 1 360px;background:#05080b;border:1px solid #27333d;border-radius:10px;overflow:hidden;");
    const mediaFrame = node("div", "", "position:relative;overflow:hidden;background:#000;flex:0 0 auto;");
    stage.appendChild(mediaFrame);
    content.appendChild(stage);

    const media = mediaType === "image" ? node("img") : node(mediaType === "video" ? "video" : "audio");
    media.src = mediaUrl;
    if (mediaType === "image") {
        media.alt = asset.name || "Reference image";
        media.draggable = false;
    } else {
        media.preload = mediaType === "audio" ? "auto" : "metadata";
        media.playsInline = true;
    }

    let cropBox = null;
    const handles = new Map();
    if (visual) {
        media.style.cssText = "position:absolute;display:block;user-select:none;max-width:none;max-height:none;";
        mediaFrame.appendChild(media);
        cropBox = node("div", "", `position:absolute;border:2px solid #72b9e6;box-shadow:0 0 0 9999px rgba(0,0,0,.58);box-sizing:border-box;touch-action:none;cursor:${readOnly ? "default" : "grab"};z-index:3;`);
        cropBox.dataset.referenceGeometryRole = "crop-box";
        cropBox.tabIndex = readOnly ? -1 : 0;
        cropBox.setAttribute("aria-label", readOnly ? "Applied crop area" : "Crop rectangle. Arrow keys move by one percent; hold Shift for five percent.");
        if (!readOnly) {
            const cursors = { nw: "nwse-resize", n: "ns-resize", ne: "nesw-resize", e: "ew-resize", se: "nwse-resize", s: "ns-resize", sw: "nesw-resize", w: "ew-resize" };
            for (const handle of ["nw", "n", "ne", "e", "se", "s", "sw", "w"]) {
                const handleNode = node("button", "", `position:absolute;width:12px;height:12px;padding:0;border:1px solid #0a1117;border-radius:2px;background:#9ed7f5;transform:translate(-50%,-50%);touch-action:none;cursor:${cursors[handle]};`);
                handleNode.type = "button";
                handleNode.dataset.cropHandle = handle;
                handleNode.dataset.referenceGeometryRole = "crop-handle";
                handleNode.setAttribute("aria-label", `Resize crop from ${handle}`);
                const positions = {
                    nw: ["0%", "0%"], n: ["50%", "0%"], ne: ["100%", "0%"], e: ["100%", "50%"],
                    se: ["100%", "100%"], s: ["50%", "100%"], sw: ["0%", "100%"], w: ["0%", "50%"],
                }[handle];
                handleNode.style.left = positions[0];
                handleNode.style.top = positions[1];
                cropBox.appendChild(handleNode);
                handles.set(handle, handleNode);
            }
        }
        mediaFrame.appendChild(cropBox);
    } else {
        media.style.display = "none";
        mediaFrame.style.display = "none";
        mediaFrame.appendChild(media);
        stage.style.display = "none";
        stage.style.minHeight = "0";
        stage.style.flex = "0 0 auto";
    }

    let ratioSelect = null;
    let customRatioWrap = null;
    let ratioErrorEl = null;
    if (visual && !readOnly) {
        const ratioPanel = node("div", "", "display:flex;align-items:end;gap:8px;flex-wrap:wrap;padding:8px;border:1px solid #303b45;border-radius:8px;background:#0b1116;");
        const ratioLabel = node("label", "", "display:flex;flex-direction:column;gap:4px;color:#91a5b5;font-size:10px;min-width:150px;");
        ratioLabel.appendChild(document.createTextNode("Crop aspect"));
        ratioSelect = node("select", "", INPUT);
        for (const preset of ASPECT_RATIO_PRESETS) {
            const option = node("option", preset.label);
            option.value = preset.a > 0 && preset.b > 0 ? `${preset.a}:${preset.b}` : "free";
            ratioSelect.appendChild(option);
        }
        const customOption = node("option", "Custom");
        customOption.value = "custom";
        ratioSelect.appendChild(customOption);
        ratioSelect.value = "free";
        ratioLabel.appendChild(ratioSelect);
        customRatioWrap = node("div", "", "display:none;grid-template-columns:80px auto 80px;align-items:end;gap:6px;");
        const makeRatioInput = (labelText) => {
            const label = node("label", "", "display:flex;flex-direction:column;gap:4px;color:#91a5b5;font-size:10px;");
            label.appendChild(document.createTextNode(labelText));
            const input = node("input", "", INPUT);
            input.type = "number";
            input.min = "0.001";
            input.step = "any";
            label.appendChild(input);
            return { label, input };
        };
        const customWidth = makeRatioInput("Width");
        const customHeight = makeRatioInput("Height");
        customRatioWrap.append(customWidth.label, node("span", "×", "padding-bottom:7px;color:#91a5b5;"), customHeight.label);
        ratioErrorEl = node("div", "", "display:none;flex:1 1 100%;color:#e8a1a1;font-size:10px;");
        ratioPanel.append(ratioLabel, customRatioWrap, ratioErrorEl);
        content.appendChild(ratioPanel);
        const applyRatio = () => {
            const ratio = activeRatio();
            if (!ratio) { ratioError = ratioMode === "custom" ? "Enter positive width and height values." : ""; renderRatioControls(); return; }
            const fitted = fitReferenceCropToAspect(cropValue, sourceWidth(), sourceHeight(), ratio.a, ratio.b);
            ratioError = fitted.error;
            if (!ratioError) { cropValue = fitted.crop; cropNull = false; }
            render();
        };
        ratioSelect.addEventListener("change", () => {
            ratioMode = ratioSelect.value;
            ratioError = "";
            if (ratioMode === "free") renderRatioControls(); else applyRatio();
        });
        customWidth.input.addEventListener("input", () => { customRatioWidth = customWidth.input.value; if (ratioMode === "custom") applyRatio(); });
        customHeight.input.addEventListener("input", () => { customRatioHeight = customHeight.input.value; if (ratioMode === "custom") applyRatio(); });
    }

    const trimWrap = timed ? node("div", "", "display:flex;flex-direction:column;gap:6px;padding:8px;border:1px solid #303b45;border-radius:8px;background:#0b1116;") : null;
    const trimRail = timed ? node("div", "", `position:relative;height:${mediaType === "audio" ? 190 : 92}px;border-radius:6px;background:#081018;overflow:hidden;touch-action:none;`) : null;
    const waveformCanvas = timed ? node("canvas", "", "position:absolute;inset:0;width:100%;height:100%;") : null;
    const selectedRange = timed ? node("div", "", `position:absolute;top:0;bottom:0;border:1px solid #72b9e6;background:rgba(70,130,170,.14);box-sizing:border-box;touch-action:none;cursor:${readOnly ? "default" : "grab"};`) : null;
    const excludedLeft = timed ? node("div", "", "position:absolute;left:0;top:0;bottom:0;background:rgba(0,0,0,.58);pointer-events:none;") : null;
    const excludedRight = timed ? node("div", "", "position:absolute;right:0;top:0;bottom:0;background:rgba(0,0,0,.58);pointer-events:none;") : null;
    const startHandle = timed && !readOnly ? node("button", "", "position:absolute;top:0;bottom:0;width:12px;transform:translateX(-50%);border:0;border-left:2px solid #9ed7f5;border-right:2px solid #9ed7f5;background:rgba(114,185,230,.22);cursor:ew-resize;touch-action:none;") : null;
    const endHandle = timed && !readOnly ? node("button", "", "position:absolute;top:0;bottom:0;width:12px;transform:translateX(-50%);border:0;border-left:2px solid #9ed7f5;border-right:2px solid #9ed7f5;background:rgba(114,185,230,.22);cursor:ew-resize;touch-action:none;") : null;
    const playbackLine = timed ? node("div", "", "position:absolute;top:0;bottom:0;left:0;width:2px;background:#f2c879;box-shadow:0 0 5px rgba(242,200,121,.7);pointer-events:none;z-index:6;") : null;
    const timeReadout = timed ? node("div", "", "display:flex;justify-content:space-between;color:#91a5b5;font-size:10px;") : null;
    if (timed) {
        selectedRange.dataset.referenceGeometryRole = "trim-selection";
        selectedRange.tabIndex = readOnly ? -1 : 0;
        selectedRange.setAttribute("aria-label", readOnly ? "Applied trim range" : "Trim range. Drag or use Left and Right arrows to move it.");
        if (startHandle && endHandle) {
            startHandle.type = endHandle.type = "button";
            startHandle.dataset.referenceGeometryRole = "trim-handle";
            startHandle.dataset.trimHandle = "start";
            endHandle.dataset.referenceGeometryRole = "trim-handle";
            endHandle.dataset.trimHandle = "end";
            startHandle.setAttribute("aria-label", "Trim start. Arrow keys adjust by 0.1 seconds; hold Shift for one second.");
            endHandle.setAttribute("aria-label", "Trim end. Arrow keys adjust by 0.1 seconds; hold Shift for one second.");
        }
        trimRail.append(waveformCanvas, selectedRange, excludedLeft, excludedRight);
        if (startHandle && endHandle) trimRail.append(startHandle, endHandle);
        trimRail.appendChild(playbackLine);
        trimWrap.append(trimRail, timeReadout);
        content.appendChild(trimWrap);
    }

    const controls = node("div", "", "display:flex;gap:7px;align-items:center;flex-wrap:wrap;");
    let playPause = null;
    let scrubBar = null;
    if (timed) {
        playPause = node("button", "Play", BUTTON);
        playPause.type = "button";
        playPause.addEventListener("click", () => togglePlayback());
        controls.appendChild(playPause);
        if (!readOnly) {
            const resetTrim = node("button", "Reset Trim", BUTTON);
            resetTrim.type = "button";
            resetTrim.addEventListener("click", () => { media.pause(); startValue = 0; endValue = null; endIsRemainder = true; render(); });
            controls.appendChild(resetTrim);
        }
        if (mediaType === "video" && asset.has_audio) {
            const mute = node("button", "Mute", BUTTON);
            mute.type = "button";
            mute.setAttribute("aria-pressed", "false");
            mute.addEventListener("click", () => {
                media.muted = !media.muted;
                mute.textContent = media.muted ? "Unmute" : "Mute";
                mute.setAttribute("aria-pressed", media.muted ? "true" : "false");
            });
            controls.appendChild(mute);
        }
    }
    if (visual && !readOnly) {
        const resetCrop = node("button", "Reset Crop", BUTTON);
        resetCrop.type = "button";
        resetCrop.addEventListener("click", () => {
            cropValue = { x: 0, y: 0, w: 100, h: 100 };
            cropNull = true;
            ratioMode = "free";
            ratioError = "";
            render();
        });
        controls.appendChild(resetCrop);
    }
    content.appendChild(controls);
    if (timed) {
        scrubBar = mountMediaScrubBar(media, {
            fps: mediaType === "video" ? finite(asset.fps) : 0,
            onSeek: () => renderTimedVisuals(),
        });
        content.appendChild(scrubBar.el);
        cleanup.push(scrubBar.cleanup);
    }

    const precision = node("div", "", "display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:7px;");
    const numericInputs = new Map();
    const numericInput = (label, value, onChange, { blank = false, key = "" } = {}) => {
        const wrap = node("label", "", "display:flex;flex-direction:column;gap:4px;color:#91a5b5;font-size:10px;");
        wrap.appendChild(document.createTextNode(label));
        const input = node("input", "", INPUT);
        input.type = "number";
        input.step = "0.1";
        input.value = blank && value == null ? "" : String(value ?? "");
        input.disabled = readOnly;
        if (key) numericInputs.set(key, input);
        input.addEventListener("input", () => onChange(input.value));
        wrap.appendChild(input);
        return wrap;
    };
    if (visual && !readOnly) {
        for (const key of ["x", "y", "w", "h"]) {
            precision.appendChild(numericInput(`Crop ${key.toUpperCase()} (%)`, cropValue[key], (value) => {
                const numeric = finite(value);
                const ratio = activeRatio();
                if (key === "x") cropValue = moveReferenceCrop(cropValue, numeric - cropValue.x, 0);
                else if (key === "y") cropValue = moveReferenceCrop(cropValue, 0, numeric - cropValue.y);
                else if (ratio) cropValue = setReferenceCropLockedSize(cropValue, key, numeric, sourceWidth(), sourceHeight(), ratio.a, ratio.b);
                else cropValue = normalizeReferenceCropPercent({ ...cropValue, [key]: numeric });
                cropNull = false;
                render();
            }, { key: `crop_${key}` }));
        }
    }
    if (timed && !readOnly) {
        precision.appendChild(numericInput("Start (seconds)", startValue, (value) => { startValue = Math.max(0, finite(value)); render(); }, { key: "trim_start" }));
        precision.appendChild(numericInput("End (blank = remainder)", endValue, (value) => {
            endIsRemainder = value === "";
            endValue = value === "" ? null : finite(value);
            render();
        }, { blank: true, key: "trim_end" }));
    }
    if (!readOnly) content.appendChild(precision);

    const footer = node("footer", "", "display:flex;gap:7px;justify-content:flex-end;padding:10px 13px;border-top:1px solid #303941;flex:0 0 auto;");
    const cancelButton = node("button", readOnly ? "Close" : "Cancel", BUTTON);
    cancelButton.type = "button";
    footer.appendChild(cancelButton);
    if (!readOnly) {
        const applyButton = node("button", "Apply to Draft", `${BUTTON}background:#476d88;border-color:#668ca7;`);
        applyButton.type = "button";
        applyButton.addEventListener("click", () => {
            const range = currentTrim();
            onApply({
                ...(visual ? { crop: cropNull || cropIsFull(cropValue) ? null : { ...cropValue } } : {}),
                ...(timed ? { source_start_sec: range.start, source_end_sec: endIsRemainder ? "" : range.end } : {}),
            });
            destroy();
        });
        footer.appendChild(applyButton);
    }
    shell.append(header, content, footer);
    overlay.appendChild(shell);
    document.body.appendChild(overlay);

    function sourceWidth() {
        return finite(media.naturalWidth) || finite(media.videoWidth) || finite(asset.width);
    }

    function sourceHeight() {
        return finite(media.naturalHeight) || finite(media.videoHeight) || finite(asset.height);
    }

    function activeRatio() {
        if (ratioMode === "free") return null;
        if (ratioMode === "custom") {
            const a = Number(customRatioWidth);
            const b = Number(customRatioHeight);
            return a > 0 && b > 0 && Number.isFinite(a) && Number.isFinite(b) ? { a, b } : null;
        }
        const [a, b] = ratioMode.split(":").map(Number);
        return a > 0 && b > 0 ? { a, b } : null;
    }

    function currentTrim() {
        if (duration > 0) return normalizeReferenceTrim(startValue, endIsRemainder ? null : endValue, duration);
        const start = Math.max(0, finite(startValue));
        return { start, end: endIsRemainder ? start : Math.max(start + 0.01, finite(endValue, start + 0.01)) };
    }

    function activeWindow() {
        return referenceMediaWindow(viewMode, startValue, endIsRemainder ? null : endValue, duration);
    }

    function mediaRatio() {
        const width = sourceWidth() || 16;
        const height = sourceHeight() || 9;
        if (viewMode === "result" && visual) return (width * cropValue.w) / Math.max(1, height * cropValue.h);
        return width / Math.max(1, height);
    }

    function sizeFrame() {
        if (!visual) return;
        const width = Math.max(1, stage.clientWidth - 20);
        const height = Math.max(1, stage.clientHeight - 20);
        const ratio = mediaRatio();
        let frameWidth = width;
        let frameHeight = frameWidth / ratio;
        if (frameHeight > height) {
            frameHeight = height;
            frameWidth = frameHeight * ratio;
        }
        mediaFrame.style.width = `${frameWidth}px`;
        mediaFrame.style.height = `${frameHeight}px`;
    }

    function drawWaveform() {
        if (!waveformCanvas) return;
        const rect = waveformCanvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        const width = Math.max(1, Math.floor(rect.width));
        const height = Math.max(1, Math.floor(rect.height));
        waveformCanvas.width = Math.floor(width * dpr);
        waveformCanvas.height = Math.floor(height * dpr);
        const context = waveformCanvas.getContext("2d");
        context.setTransform(dpr, 0, 0, dpr, 0, 0);
        context.fillStyle = "#081018";
        context.fillRect(0, 0, width, height);
        context.strokeStyle = "rgba(255,255,255,.08)";
        context.beginPath(); context.moveTo(0, height / 2); context.lineTo(width, height / 2); context.stroke();
        const range = currentTrim();
        const peaks = viewMode === "result"
            ? sliceReferenceWaveformPeaks(waveformPeaks, range.start, range.end, duration)
            : waveformPeaks;
        if (!Array.isArray(peaks) || !peaks.length) {
            context.fillStyle = "#708391";
            context.font = "11px 'Segoe UI', sans-serif";
            context.textAlign = "center";
            context.fillText(waveformLoaded ? "Waveform unavailable · timed rail" : "Loading waveform…", width / 2, height / 2 - 10);
            return;
        }
        context.strokeStyle = "#72b9e6";
        const step = Math.max(1, Math.ceil(peaks.length / width));
        for (let index = 0; index < peaks.length; index += step) {
            const peak = peaks[index];
            const low = Array.isArray(peak) ? finite(peak[0]) : -Math.abs(finite(peak));
            const high = Array.isArray(peak) ? finite(peak[1]) : Math.abs(finite(peak));
            const x = (index / Math.max(1, peaks.length - 1)) * width;
            context.beginPath();
            context.moveTo(x, (1 - (high + 1) / 2) * height);
            context.lineTo(x, (1 - (low + 1) / 2) * height);
            context.stroke();
        }
    }

    function renderRatioControls() {
        if (!ratioSelect) return;
        ratioSelect.value = ratioMode;
        customRatioWrap.style.display = ratioMode === "custom" ? "grid" : "none";
        ratioErrorEl.textContent = ratioError || (ratioMode === "custom" && !activeRatio() ? "Enter positive width and height values." : "");
        ratioErrorEl.style.display = ratioErrorEl.textContent ? "block" : "none";
    }

    function renderTimedVisuals() {
        if (!timed || destroyed) return;
        const range = currentTrim();
        const mediaWindow = activeWindow();
        const hasDuration = duration > 0;
        const left = hasDuration && viewMode === "source" ? (range.start / duration) * 100 : 0;
        const right = hasDuration && viewMode === "source" ? (range.end / duration) * 100 : 100;
        selectedRange.style.left = `${left}%`;
        selectedRange.style.width = `${Math.max(0, right - left)}%`;
        selectedRange.style.pointerEvents = !readOnly && viewMode === "source" && hasDuration ? "auto" : "none";
        selectedRange.style.cursor = !readOnly && viewMode === "source" && hasDuration ? "grab" : "default";
        excludedLeft.style.display = viewMode === "source" ? "block" : "none";
        excludedRight.style.display = viewMode === "source" ? "block" : "none";
        excludedLeft.style.width = `${left}%`;
        excludedRight.style.width = `${100 - right}%`;
        if (startHandle && endHandle) {
            const visible = viewMode === "source" && hasDuration;
            startHandle.style.display = visible ? "block" : "none";
            endHandle.style.display = visible ? "block" : "none";
            startHandle.style.left = `${left}%`;
            endHandle.style.left = `${right}%`;
        }
        const playheadRatio = mediaWindow.duration > 0
            ? Math.max(0, Math.min(1, (finite(media.currentTime) - mediaWindow.start) / mediaWindow.duration))
            : 0;
        playbackLine.style.left = `${playheadRatio * 100}%`;
        playbackLine.style.display = mediaWindow.duration > 0 ? "block" : "none";
        timeReadout.textContent = "";
        if (viewMode === "result") {
            timeReadout.append(node("span", `Result ${formatSeconds(mediaWindow.duration)}`), node("span", `Source ${formatSeconds(mediaWindow.start)} → ${formatSeconds(mediaWindow.end)}`));
        } else {
            timeReadout.append(node("span", `Start ${formatSeconds(range.start)}`), node("span", `${endIsRemainder ? "Remainder" : "End"} ${formatSeconds(range.end)} / ${formatSeconds(duration)}`));
        }
        scrubBar?.setWindow({ startSec: mediaWindow.start, endSec: mediaWindow.end, relative: viewMode === "result" });
        if (playPause) playPause.textContent = media.paused ? "Play" : "Pause";
        const startInput = numericInputs.get("trim_start");
        const endInput = numericInputs.get("trim_end");
        if (startInput && document.activeElement !== startInput) startInput.value = String(Number(range.start.toFixed(3)));
        if (endInput && document.activeElement !== endInput) endInput.value = endIsRemainder ? "" : String(Number(range.end.toFixed(3)));
        drawWaveform();
    }

    function render() {
        if (destroyed) return;
        sourceButton.style.background = viewMode === "source" ? "#476d88" : "#252e37";
        resultButton.style.background = viewMode === "result" ? "#476d88" : "#252e37";
        renderRatioControls();
        if (visual) {
            sizeFrame();
            if (viewMode === "source") {
                media.style.left = "0"; media.style.top = "0"; media.style.width = "100%"; media.style.height = "100%"; media.style.objectFit = "contain";
                if (cropBox) {
                    cropBox.style.display = readOnly && cropNull ? "none" : "block";
                    cropBox.style.left = `${cropValue.x}%`; cropBox.style.top = `${cropValue.y}%`;
                    cropBox.style.width = `${cropValue.w}%`; cropBox.style.height = `${cropValue.h}%`;
                }
            } else {
                media.style.left = `${-(cropValue.x / cropValue.w) * 100}%`;
                media.style.top = `${-(cropValue.y / cropValue.h) * 100}%`;
                media.style.width = `${(100 / cropValue.w) * 100}%`;
                media.style.height = `${(100 / cropValue.h) * 100}%`;
                media.style.objectFit = "fill";
                if (cropBox) cropBox.style.display = "none";
            }
            for (const key of ["x", "y", "w", "h"]) {
                const input = numericInputs.get(`crop_${key}`);
                if (input && document.activeElement !== input) input.value = String(Number(cropValue[key].toFixed(3)));
            }
        }
        renderTimedVisuals();
    }

    function ensurePlayablePosition() {
        const mediaWindow = activeWindow();
        if (!(mediaWindow.duration > 0)) return false;
        const current = finite(media.currentTime);
        if (current < mediaWindow.start || current >= mediaWindow.end - 0.01) media.currentTime = mediaWindow.start;
        return true;
    }

    function togglePlayback() {
        if (!timed) return;
        if (!media.paused) { media.pause(); return; }
        if (ensurePlayablePosition()) void media.play();
    }

    function setViewMode(nextMode) {
        const next = nextMode === "result" ? "result" : "source";
        if (viewMode === next) return;
        if (timed) media.pause();
        viewMode = next;
        if (timed && viewMode === "result") ensurePlayablePosition();
        onViewModeChange(viewMode);
        render();
    }

    function cropPointerDown(event) {
        if (readOnly || viewMode !== "source") return;
        const handle = event.target?.dataset?.cropHandle || "move";
        const frameRect = mediaFrame.getBoundingClientRect();
        const origin = { x: event.clientX, y: event.clientY, crop: { ...cropValue } };
        event.preventDefault();
        (event.target?.closest?.("[data-reference-geometry-role]") || cropBox).focus?.({ preventScroll: true });
        cropBox.style.cursor = handle === "move" ? "grabbing" : "grab";
        event.currentTarget?.setPointerCapture?.(event.pointerId);
        const move = (moveEvent) => {
            const dx = ((moveEvent.clientX - origin.x) / Math.max(1, frameRect.width)) * 100;
            const dy = ((moveEvent.clientY - origin.y) / Math.max(1, frameRect.height)) * 100;
            const ratio = activeRatio();
            cropValue = handle === "move"
                ? moveReferenceCrop(origin.crop, dx, dy)
                : ratio
                    ? resizeReferenceCropLocked(origin.crop, handle, dx, dy, sourceWidth(), sourceHeight(), ratio.a, ratio.b)
                    : resizeReferenceCrop(origin.crop, handle, dx, dy);
            cropNull = false;
            render();
        };
        const up = () => { cropBox.style.cursor = "grab"; window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up, { once: true });
        cleanup.push(up);
    }

    function cropKeyDown(event) {
        if (readOnly || viewMode !== "source" || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return false;
        const amount = event.shiftKey ? 5 : 1;
        const dx = event.key === "ArrowLeft" ? -amount : event.key === "ArrowRight" ? amount : 0;
        const dy = event.key === "ArrowUp" ? -amount : event.key === "ArrowDown" ? amount : 0;
        const handle = event.target?.dataset?.cropHandle;
        const ratio = activeRatio();
        cropValue = handle
            ? ratio
                ? resizeReferenceCropLocked(cropValue, handle, dx, dy, sourceWidth(), sourceHeight(), ratio.a, ratio.b)
                : resizeReferenceCrop(cropValue, handle, dx, dy)
            : moveReferenceCrop(cropValue, dx, dy);
        cropNull = false;
        render();
        return true;
    }

    function trimPointerDown(event, which) {
        if (readOnly || viewMode !== "source" || !(duration > 0)) return;
        event.preventDefault();
        event.currentTarget?.focus?.({ preventScroll: true });
        const railRect = trimRail.getBoundingClientRect();
        const update = (moveEvent) => {
            const seconds = Math.max(0, Math.min(1, (moveEvent.clientX - railRect.left) / Math.max(1, railRect.width))) * Math.max(duration, 0.01);
            const range = currentTrim();
            if (which === "start") startValue = Math.min(seconds, range.end - 0.01);
            else { endValue = Math.max(seconds, range.start + 0.01); endIsRemainder = false; }
            render();
        };
        const up = () => { window.removeEventListener("pointermove", update); window.removeEventListener("pointerup", up); };
        window.addEventListener("pointermove", update);
        window.addEventListener("pointerup", up, { once: true });
        cleanup.push(up);
    }

    function trimKeyDown(event, which) {
        if (readOnly || viewMode !== "source" || !(duration > 0) || !["ArrowLeft", "ArrowRight"].includes(event.key)) return false;
        const delta = (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 1 : 0.1);
        const range = currentTrim();
        if (which === "start") startValue = Math.max(0, Math.min(range.end - 0.01, range.start + delta));
        else { endValue = Math.min(Math.max(duration, range.end), Math.max(range.start + 0.01, range.end + delta)); endIsRemainder = false; }
        render();
        return true;
    }

    function trimSelectionPointerDown(event) {
        if (readOnly || viewMode !== "source" || !(duration > 0)) return;
        if (event.target === startHandle || event.target === endHandle) return;
        event.preventDefault();
        selectedRange.focus?.({ preventScroll: true });
        selectedRange.style.cursor = "grabbing";
        const rect = trimRail.getBoundingClientRect();
        const originX = event.clientX;
        const originRange = currentTrim();
        const move = (moveEvent) => {
            const delta = ((moveEvent.clientX - originX) / Math.max(1, rect.width)) * duration;
            const shifted = shiftReferenceTrim(originRange.start, originRange.end, duration, delta);
            startValue = shifted.start;
            endValue = shifted.end;
            endIsRemainder = false;
            render();
        };
        const up = () => {
            selectedRange.style.cursor = "grab";
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up, { once: true });
        cleanup.push(up);
    }

    function trimSelectionKeyDown(event) {
        if (readOnly || viewMode !== "source" || !(duration > 0) || !["ArrowLeft", "ArrowRight"].includes(event.key)) return false;
        const delta = (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 1 : 0.1);
        const range = currentTrim();
        const shifted = shiftReferenceTrim(range.start, range.end, duration, delta);
        startValue = shifted.start;
        endValue = shifted.end;
        endIsRemainder = false;
        render();
        return true;
    }

    if (cropBox && !readOnly) {
        cropBox.addEventListener("pointerdown", cropPointerDown);
    }
    if (timed && !readOnly) {
        startHandle?.addEventListener("pointerdown", (event) => trimPointerDown(event, "start"));
        endHandle?.addEventListener("pointerdown", (event) => trimPointerDown(event, "end"));
        selectedRange.addEventListener("pointerdown", trimSelectionPointerDown);
    }
    const metadataReady = () => {
        duration = Math.max(duration, finite(media.duration));
        if (visual) {
            sizeFrame();
            const ratio = activeRatio();
            if (ratio && sourceWidth() > 0 && sourceHeight() > 0) {
                const fitted = fitReferenceCropToAspect(cropValue, sourceWidth(), sourceHeight(), ratio.a, ratio.b);
                ratioError = fitted.error;
                if (!ratioError) { cropValue = fitted.crop; cropNull = false; }
            }
        }
        render();
    };
    media.addEventListener(mediaType === "image" ? "load" : "loadedmetadata", metadataReady);
    const playbackTick = () => {
        playbackFrame = 0;
        if (destroyed || !timed) return;
        const mediaWindow = activeWindow();
        if (!media.paused && mediaWindow.duration > 0 && media.currentTime >= mediaWindow.end - 0.01) {
            media.pause();
            media.currentTime = mediaWindow.end;
        }
        renderTimedVisuals();
        if (!media.paused) playbackFrame = requestAnimationFrame(playbackTick);
    };
    if (timed) {
        media.addEventListener("play", () => { if (!playbackFrame) playbackFrame = requestAnimationFrame(playbackTick); renderTimedVisuals(); });
        media.addEventListener("pause", renderTimedVisuals);
        media.addEventListener("timeupdate", renderTimedVisuals);
    }
    sourceButton.addEventListener("click", () => setViewMode("source"));
    resultButton.addEventListener("click", () => setViewMode("result"));
    closeButton.addEventListener("click", () => destroy());
    cancelButton.addEventListener("click", () => destroy());
    overlay.addEventListener("mousedown", (event) => { if (event.target === overlay) destroy(); });
    overlay.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        event.stopPropagation();
    });
    const overlayCaptureKeys = new Set([" ", "Spacebar", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "Delete", "Backspace", "?", "=", "+", "-", "_", "0", "1", "2", "3", "a", "A", "c", "C", "f", "F", "i", "I", "o", "O", "s", "S", "t", "T", "x", "X"]);
    const keyboardOff = registerKeyboardConsumer({
        id: keyboardConsumerId,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (destroyed) return false;
            if (event.key === "Escape") { destroy(); return true; }
            const role = event.target?.dataset?.referenceGeometryRole || "";
            if ((role === "crop-box" || role === "crop-handle") && cropKeyDown(event)) return true;
            if (role === "trim-selection" && trimSelectionKeyDown(event)) return true;
            if (role === "trim-handle" && trimKeyDown(event, event.target?.dataset?.trimHandle)) return true;
            if (event.target?.closest?.("input, textarea, select, [contenteditable='true']")) return false;
            const button = event.target?.closest?.("button");
            if (button && (event.key === " " || event.key === "Spacebar")) {
                if (!event.repeat) button.click();
                return true;
            }
            if (button && event.key === "Enter") return false;
            if (event.key === " " || event.key === "Spacebar") { if (timed) togglePlayback(); return true; }
            if ((event.ctrlKey || event.metaKey) && ["z", "y"].includes(String(event.key || "").toLowerCase())) return true;
            return overlayCaptureKeys.has(event.key);
        },
        keyup: (event) => {
            if (destroyed || event.target?.closest?.("input, textarea, select, [contenteditable='true']")) return false;
            return overlayCaptureKeys.has(event.key);
        },
    });
    cleanup.push(keyboardOff);
    const resizeObserver = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => render()) : null;
    if (stage.style.display !== "none") resizeObserver?.observe(stage);
    if (trimRail) resizeObserver?.observe(trimRail);
    cleanup.push(() => resizeObserver?.disconnect());

    if (timed && waveformUrl && (mediaType === "audio" || asset.has_audio)) {
        fetch(waveformUrl, { signal: abortController.signal })
            .then((response) => response.ok ? response.json() : null)
            .then((payload) => { if (!destroyed) { waveformLoaded = true; waveformPeaks = Array.isArray(payload?.peaks) ? payload.peaks : null; render(); } })
            .catch(() => { if (!destroyed) { waveformLoaded = true; waveformPeaks = null; render(); } });
    } else if (timed) {
        waveformLoaded = true;
    }

    function destroy() {
        if (destroyed) return;
        destroyed = true;
        abortController.abort();
        if (playbackFrame) cancelAnimationFrame(playbackFrame);
        playbackFrame = 0;
        try { media.pause?.(); } catch { /* ignore */ }
        media.removeAttribute?.("src");
        try { media.load?.(); } catch { /* ignore */ }
        while (cleanup.length) {
            try { cleanup.pop()?.(); } catch { /* ignore */ }
        }
        overlay.remove();
        try { opener?.focus?.(); } catch { /* ignore */ }
        onClose();
    }

    requestAnimationFrame(() => {
        render();
        shell.focus?.({ preventScroll: true });
    });
    return { destroy };
}
