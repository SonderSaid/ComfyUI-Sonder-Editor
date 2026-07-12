import { EDITOR_COLORS as COLORS, THEME } from "./editor_theme.js";
import {
    LABEL_WIDTH,
    LABEL_WIDTH_FS,
    RULER_HEIGHT,
    TRACK_COLLAPSED_HEIGHT,
    TRACK_HEIGHT,
    TRACK_TYPE,
} from "./editor_timeline_constants.js";
export {
    LABEL_WIDTH,
    LABEL_WIDTH_FS,
    RULER_HEIGHT,
    TRACK_COLLAPSED_HEIGHT,
    TRACK_HEIGHT,
    TRACK_TYPE,
} from "./editor_timeline_constants.js";

function timelineLabelPoint(host, rectX, rectY, rectW, rectH, fontSize) {
    const scale = host._scaleTimeline || 1;
    const padY = Math.max(2, Math.round(3 * scale));
    const horizontal = host._clipLabelHorizontalAlign?.() || "start";
    const vertical = host._clipLabelVerticalAlign?.() || "middle";
    const align = horizontal === "end" ? "right" : (horizontal === "middle" ? "center" : "left");
    const x = align === "right"
        ? rectX + rectW
        : (align === "center" ? rectX + rectW / 2 : rectX);

    let y;
    if (vertical === "top") {
        y = rectY + padY + fontSize;
    } else if (vertical === "bottom") {
        y = rectY + rectH - padY;
    } else {
        y = rectY + rectH / 2 + Math.round(fontSize * 0.34);
    }
    const minY = rectY + Math.min(rectH - 1, fontSize);
    const maxY = rectY + Math.max(1, rectH - padY);
    return {
        x,
        y: Math.max(Math.min(minY, maxY), Math.min(maxY, y)),
        align,
    };
}

export function _trackY(host, layoutIdx) {
    const ts = host._scaleTimeline;
    let y = Math.round(RULER_HEIGHT * ts);
    for (let i = 0; i < layoutIdx; i++) {
        y += Math.round((host._trackLayout[i]?.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT) * ts);
    }
    return y;
}

export function _trackH(host, layoutIdx) {
    const entry = host._trackLayout[layoutIdx];
    return Math.round((entry?.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT) * host._scaleTimeline);
}

export function _totalTracksHeight(host) {
    const ts = host._scaleTimeline;
    let h = 0;
    for (const entry of host._trackLayout) {
        h += Math.round((entry.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT) * ts);
    }
    return h;
}

export function _timelineRulerHeight(host) {
    return Math.round(RULER_HEIGHT * host._scaleTimeline);
}

export function _visibleTimelineContentHeight(host) {
    return Math.max(0, host._timelineHeight - host._timelineRulerHeight());
}

export function _clampScrollY(host) {
    const maxScroll = Math.max(0, host._totalTracksHeight() - host._visibleTimelineContentHeight());
    host.scrollY = Math.max(0, Math.min(maxScroll, host.scrollY));
}

export function _trackContentYFromRawY(host, rawY) {
    const rulerH = host._timelineRulerHeight();
    if (rawY < rulerH) return null;
    return rawY - rulerH + host.scrollY;
}

export function _layoutIndexFromRawY(host, rawY) {
    const contentY = host._trackContentYFromRawY(rawY);
    if (contentY === null) return -1;
    let offset = 0;
    for (let i = 0; i < host._trackLayout.length; i++) {
        const trackH = host._trackH(i);
        if (contentY >= offset && contentY < offset + trackH) {
            return i;
        }
        offset += trackH;
    }
    return -1;
}
export function _drawTimelineItemRail(host, ctx, x, y, w, h, color) {
        if (w <= 4 || h <= 4 || !color) return;
        const railW = Math.min(Math.max(2, Math.round(4 * host._scaleTimeline)), Math.max(2, w - 4));
        ctx.fillStyle = color;
        ctx.fillRect(x + 2, y + 2, railW, h - 4);
        ctx.save();
        ctx.globalAlpha *= 0.68;
        ctx.fillRect(x + 2, y + 2, Math.max(0, w - 4), 2);
        ctx.restore();
    }
export function _renderTimeline(host) {
        const canvas = host.timelineCanvas;
        const rect = canvas.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const rulerH = host._timelineRulerHeight();
        const canvasH = Math.max(rulerH + 1, host._timelineHeight);
        host._clampScrollX();
        host._clampScrollY();

        // Canvas at 1:1 — per-section scales handle individual elements
        canvas.width = width;
        canvas.height = canvasH;
        canvas.style.width = width + "px";
        canvas.style.height = canvasH + "px";

        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.save();

        // Background
        ctx.fillStyle = host._timelineColor(COLORS.bg);
        ctx.fillRect(0, 0, width, canvasH);

        host._drawRuler(ctx, width);
        drawPlaybackWarmStrip(host, ctx, width, rulerH);

        // Drag-drop new-lane cue: the ruler strip is the explicit
        // lane-creation drop zone (zone-model targeting).
        if (host._dropHoverTarget?.kind === "ruler") {
            ctx.save();
            ctx.fillStyle = "rgba(99, 179, 237, 0.16)";
            ctx.fillRect(0, 0, width, rulerH);
            ctx.strokeStyle = COLORS.accent;
            ctx.lineWidth = 1;
            ctx.strokeRect(0.5, 0.5, Math.max(0, width - 1), Math.max(0, rulerH - 1));
            ctx.fillStyle = COLORS.text;
            ctx.font = host._canvasSansFont(Math.max(9, Math.round(10 * host._scaleTimeline)), 600);
            ctx.textAlign = "center";
            ctx.fillText("Drop here: new lane", width / 2, rulerH / 2 + 4);
            ctx.restore();
        }
        host._drawPlayheadTriangle(ctx, width);

        ctx.save();
        ctx.beginPath();
        ctx.rect(0, rulerH, width, Math.max(0, canvasH - rulerH));
        ctx.clip();
        ctx.translate(0, -host.scrollY);
        host._drawTracks(ctx, width);
        ctx.restore();

        ctx.save();
        ctx.beginPath();
        ctx.rect(host._labelW, rulerH, Math.max(0, width - host._labelW), Math.max(0, canvasH - rulerH));
        ctx.clip();
        ctx.translate(0, -host.scrollY);
        host._drawSelection(ctx, width);
        host._drawGuideMarkers(ctx, width);
        host._drawClips(ctx, width);
        host._drawPlayheadLine(ctx, width);
        ctx.restore();

        host._drawDragSelectOverlay?.(ctx, width, canvasH);
        host._drawSnapIndicator(ctx, width, canvasH);
        host._drawVerticalScrollbar(ctx, width, canvasH);
        ctx.restore();
    }

export function _labelW(host) {
        const userW = host.isFullscreen ? host._labelWidthUserFS : host._labelWidthUser;
        const baseW = userW > 0 ? userW : (host.isFullscreen ? LABEL_WIDTH_FS : LABEL_WIDTH);
        return Math.round(baseW * host._scaleTrackHeaders);
    }

export function _frameToX(host, frame) {
        return host._labelW + (frame - host.scrollX) * host.pixelsPerFrame;
    }

export function _xToFrame(host, x) {
        return Math.round((x - host._labelW) / host.pixelsPerFrame + host.scrollX);
    }

export function _clampScrollX(host) {
        const rect = host.timelineCanvas?.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const visibleFrames = Math.max(1, (width - host._labelW) / host.pixelsPerFrame);
        const totalFrames = host.activeScene?.duration_frames ?? host.totalFrames;
        const maxScroll = Math.max(0, totalFrames - visibleFrames);
        host.scrollX = Math.max(0, Math.min(maxScroll, host.scrollX));
    }

export function _drawRuler(host, ctx, width) {
        const ts = host._scaleTimeline;
        const rulerH = Math.round(RULER_HEIGHT * ts);
        ctx.fillStyle = host._timelineColor(COLORS.ruler);
        ctx.fillRect(0, 0, width, rulerH);

        ctx.save();
        ctx.beginPath();
        ctx.rect(host._labelW, 0, Math.max(0, width - host._labelW), rulerH);
        ctx.clip();

        ctx.strokeStyle = COLORS.rulerTick;
        ctx.fillStyle = COLORS.rulerText;
        ctx.font = host._canvasMonoFont(Math.max(11, Math.round(9 * ts)), 500);
        ctx.textAlign = "center";

        // Determine tick spacing based on zoom
        let majorEvery = 10;
        if (host.pixelsPerFrame < 2) majorEvery = 50;
        else if (host.pixelsPerFrame < 5) majorEvery = 25;
        else if (host.pixelsPerFrame > 10) majorEvery = 5;

        // For timecode mode, adjust major ticks to align with seconds
        if (host._timecodeMode === "timecode") {
            const fps = host._effectiveFps;
            if (host.pixelsPerFrame * fps < 80) {
                majorEvery = fps * 5; // every 5 seconds
            } else {
                majorEvery = fps; // every 1 second
            }
        }

        const totalFrames = host.activeScene?.duration_frames ?? host.totalFrames;
        const startFrame = Math.max(0, Math.floor(host.scrollX));
        const endFrame = Math.min(totalFrames, Math.ceil(host.scrollX + (width - host._labelW) / host.pixelsPerFrame));

        for (let f = startFrame; f <= endFrame; f++) {
            const x = host._frameToX(f);
            if (x < 0 || x > width) continue;

            if (f % majorEvery === 0) {
                ctx.beginPath();
                ctx.moveTo(x, rulerH - Math.round(12 * ts));
                ctx.lineTo(x, rulerH);
                ctx.stroke();
                const label = host._frameToTimecode(f);
                const labelHalfW = ctx.measureText(label).width / 2;
                const labelPad = Math.round(4 * ts);
                const minLabelX = host._labelW + labelPad + labelHalfW;
                const maxLabelX = width - labelPad - labelHalfW;
                const labelX = maxLabelX >= minLabelX
                    ? Math.max(minLabelX, Math.min(maxLabelX, x))
                    : width / 2;
                ctx.fillText(label, labelX, rulerH - Math.round(13 * ts));
            } else if (f % (majorEvery / 5) === 0 && host.pixelsPerFrame > 1.5) {
                ctx.beginPath();
                ctx.moveTo(x, rulerH - Math.round(6 * ts));
                ctx.lineTo(x, rulerH);
                ctx.stroke();
            }
        }
        ctx.restore();
    }

function drawPlaybackWarmStrip(host, ctx, width, rulerH) {
        const snapshot = host._playbackWarmState;
        if (!host.isFullscreen || !snapshot?.entries?.length) return;
        const labelW = host._labelW;
        const visibleW = Math.max(0, width - labelW);
        if (visibleW <= 0 || host.pixelsPerFrame <= 0) return;

        const ts = host._scaleTimeline || 1;
        const stripH = Math.max(2, Math.round(3 * ts));
        const stripY = Math.max(0, rulerH - stripH);
        const startFrame = Math.max(0, Math.floor(host.scrollX));
        const endFrame = Math.min(host.totalFrames, Math.ceil(host.scrollX + visibleW / host.pixelsPerFrame));
        if (endFrame <= startFrame) return;

        const colorForState = {
            cold: THEME.statusIdle,
            warming: THEME.statusPending,
            warm: THEME.statusRunning,
            blocked: THEME.statusFailed,
        };
        const alphaForState = {
            cold: 0.18,
            warming: 0.58,
            warm: 0.62,
            blocked: 0.72,
        };
        const priority = { warming: 1, warm: 2, blocked: 3 };

        ctx.save();
        ctx.beginPath();
        ctx.rect(labelW, stripY, visibleW, stripH);
        ctx.clip();

        ctx.globalAlpha *= alphaForState.cold;
        ctx.fillStyle = colorForState.cold;
        ctx.fillRect(labelW, stripY, visibleW, stripH);
        ctx.restore();

        const entries = [...snapshot.entries]
            .filter((entry) => entry && entry.endFrame > startFrame && entry.startFrame < endFrame)
            .sort((a, b) => (priority[a.state] || 0) - (priority[b.state] || 0));

        for (const entry of entries) {
            const color = colorForState[entry.state];
            if (!color) continue;
            const x1 = Math.max(labelW, host._frameToX(Math.max(startFrame, entry.startFrame)));
            const x2 = Math.min(width, host._frameToX(Math.min(endFrame, entry.endFrame)));
            const rectW = Math.max(1, Math.ceil(x2 - x1));
            ctx.save();
            ctx.globalAlpha *= alphaForState[entry.state] ?? 0.5;
            ctx.fillStyle = color;
            ctx.fillRect(Math.floor(x1), stripY, rectW, stripH);
            ctx.restore();
        }
    }

export function _drawTracks(host, ctx, width) {
        const hs = host._scaleTrackHeaders;
        const headerW = host._labelW; // already scaled by _scaleTrackHeaders
        const fs = host.isFullscreen;
        for (let i = 0; i < host._trackLayout.length; i++) {
            const entry = host._trackLayout[i];
            const y = host._trackY(i);
            const h = host._trackH(i);
            const collapsed = entry.collapsed;
            const isLane = host._isLaneTrackType(entry.type);
            const hasHeaderControls = host._isHeaderControllableTrackType(entry.type);

            // Track background: alternating navy base, plus optional per-type tint overlay from settings
            ctx.fillStyle = i % 2 === 0 ? host._timelineColor(COLORS.track) : host._timelineColor(COLORS.bg);
            ctx.fillRect(0, y, width, h);
            if (isLane) {
                const tint = host._resolveLaneTint(entry.type);
                if (tint) {
                    ctx.save();
                    ctx.globalAlpha = 0.18;
                    ctx.fillStyle = tint;
                    ctx.fillRect(0, y, width, h);
                    ctx.restore();
                }
            }
            if (host._isLaneSelected?.(entry)) {
                ctx.save();
                ctx.fillStyle = "rgba(99, 179, 237, 0.16)";
                ctx.fillRect(0, y, width, h);
                ctx.strokeStyle = COLORS.accent;
                ctx.lineWidth = 1;
                ctx.strokeRect(0.5, y + 0.5, Math.max(0, headerW - 1), Math.max(0, h - 1));
                ctx.restore();
            }
            // Drag-drop landing preview: same visual as lane selection, full-row
            // outline so the user sees exactly which lane the drop will land on.
            if (host._dropHoverTarget?.kind === "lane" && host._dropHoverTarget.layoutIdx === i) {
                ctx.save();
                ctx.fillStyle = "rgba(99, 179, 237, 0.16)";
                ctx.fillRect(0, y, width, h);
                ctx.strokeStyle = COLORS.accent;
                ctx.lineWidth = 1;
                ctx.strokeRect(0.5, y + 0.5, Math.max(0, width - 1), Math.max(0, h - 1));
                ctx.restore();
            }

            if (collapsed) {
                // Collapsed: just arrow + short label
                ctx.fillStyle = COLORS.textMuted;
                ctx.font = host._canvasSansFont(Math.round(8 * hs), 500);
                ctx.textAlign = "left";
                ctx.fillText(`▸ ${entry.label}`, Math.round((fs ? 6 : 3) * hs), y + h / 2 + 2);
            } else {
                // --- Header layout (left to right) ---
                // Positions scale with fullscreen AND _scaleTrackHeaders
                const arrowX = Math.round((fs ? 6 : 3) * hs);
                const iconSize = Math.round((fs ? 14 : 11) * hs);
                let curX = arrowX;

                // 1. Collapse arrow
                ctx.fillStyle = COLORS.textDim;
                ctx.font = host._canvasSansFont(iconSize, 500);
                ctx.textAlign = "left";
                ctx.fillText("▾", curX, y + h / 2 + Math.round((fs ? 5 : 4) * hs));
                curX += iconSize + Math.round(2 * hs);

                if (hasHeaderControls) {
                    // 2. Lock icon.
                    if (entry.locked) {
                        // Draw bright background indicator for locked state
                        ctx.fillStyle = "rgba(178, 100, 100, 0.22)";
                        ctx.fillRect(curX - 1, y + 2, iconSize + 1, h - 4);
                    }
                    ctx.fillStyle = entry.locked ? COLORS.dangerText : COLORS.textMuted;
                    ctx.font = host._canvasSansFont(iconSize - Math.round(2 * hs), 500);
                    ctx.fillText(entry.locked ? "🔒" : "🔓", curX, y + h / 2 + Math.round((fs ? 4 : 3) * hs));
                    curX += iconSize + Math.round(1 * hs);

                    // 3. Hide/Mute icon
                    const visibilityState = host._trackVisibilityState(entry);
                    const isAudioLike = entry.type === TRACK_TYPE.AUDIO || entry.type === TRACK_TYPE.PROMPT
                        || entry.type === TRACK_TYPE.PROMPT_GLOBAL;
                    ctx.fillStyle = visibilityState === "hidden"
                        ? COLORS.dangerText
                        : visibilityState === "partial"
                            ? COLORS.accentHi
                            : COLORS.textMuted;
                    const visibleIcon = isAudioLike ? "🔊" : "👁";
                    const hiddenIcon = isAudioLike ? "🔇" : "🚫";
                    ctx.fillText(
                        visibilityState === "partial" ? "◐" : (visibilityState === "hidden" ? hiddenIcon : visibleIcon),
                        curX,
                        y + h / 2 + Math.round((fs ? 4 : 3) * hs)
                    );
                    curX += iconSize + Math.round(1 * hs);

                    // 3b. Manage icon (☰) — fixed tracks only; opens the
                    // guide/prompt management panel. Advance matches the
                    // hit-test zone width exactly so glyph and zone align.
                    if (!isLane) {
                        ctx.fillStyle = COLORS.textMuted;
                        ctx.font = host._canvasSansFont(iconSize - Math.round(2 * hs), 500);
                        ctx.fillText("☰", curX, y + h / 2 + Math.round((fs ? 4 : 3) * hs));
                        curX += iconSize + Math.round(1 * hs);
                    }

                    // 4. Color bar
                    if (isLane && entry.color) {
                        ctx.fillStyle = entry.color;
                        ctx.fillRect(curX, y + 2, Math.round(4 * hs), h - 4);
                    }
                    curX += Math.round(7 * hs);
                }

                // 5. Label
                ctx.fillStyle = hasHeaderControls && host._trackVisibilityState(entry) === "hidden" ? COLORS.textMuted : COLORS.textDim;
                ctx.font = host._canvasSansFont(Math.round((fs ? 10 : 8) * hs), 500);
                ctx.textAlign = "left";
                const labelText = entry.label;
                const maxLabelW = headerW - curX - 2;
                ctx.save();
                ctx.beginPath();
                ctx.rect(curX, y, maxLabelW, h);
                ctx.clip();
                ctx.fillText(labelText, curX, y + h / 2 + Math.round(3 * hs));
                ctx.restore();
            }

            // Border
            ctx.strokeStyle = host._timelineColor(COLORS.trackBorder);
            ctx.beginPath();
            ctx.moveTo(0, y + h);
            ctx.lineTo(width, y + h);
            ctx.stroke();
        }

        // Header/timeline boundary separator (draggable)
        const bx = host._labelW;
        ctx.strokeStyle = COLORS.trackBorder;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(bx, 0);
        ctx.lineTo(bx, host._trackY(host._trackLayout.length - 1) + host._trackH(host._trackLayout.length - 1));
        ctx.stroke();
    }

export function _drawSelection(host, ctx, width) {
        const range = host._selectionContextRange();
        if (!range) return;

        const x1 = host._frameToX(range.selectionStart);
        const x2 = host._frameToX(range.selectionEnd);
        const contextX1 = host._frameToX(range.contextStart);
        const contextX2 = host._frameToX(range.contextEnd);
        const y = host._timelineRulerHeight();
        const h = host._totalTracksHeight();

        if (range.hasPreContext) {
            ctx.fillStyle = COLORS.selectionContext;
            ctx.fillRect(contextX1, y, x1 - contextX1, h);
            ctx.strokeStyle = COLORS.selectionContextBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(contextX1, y);
            ctx.lineTo(contextX1, y + h);
            ctx.stroke();
        }

        if (range.hasPostContext) {
            ctx.fillStyle = COLORS.selectionContext;
            ctx.fillRect(x2, y, contextX2 - x2, h);
            ctx.strokeStyle = COLORS.selectionContextBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(contextX2, y);
            ctx.lineTo(contextX2, y + h);
            ctx.stroke();
        }

        if (range.hasMaskPre) {
            const maskX1 = host._frameToX(range.maskStart);
            ctx.fillStyle = COLORS.maskOffset;
            ctx.fillRect(maskX1, y, x1 - maskX1, h);
            ctx.strokeStyle = COLORS.maskOffsetBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(maskX1, y);
            ctx.lineTo(maskX1, y + h);
            ctx.stroke();
        }

        if (range.hasMaskPost) {
            const maskX2 = host._frameToX(range.maskEnd);
            ctx.fillStyle = COLORS.maskOffset;
            ctx.fillRect(x2, y, maskX2 - x2, h);
            ctx.strokeStyle = COLORS.maskOffsetBorder;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(maskX2, y);
            ctx.lineTo(maskX2, y + h);
            ctx.stroke();
        }

        // Fill
        ctx.fillStyle = COLORS.selection;
        ctx.fillRect(x1, y, x2 - x1, h);

        // Border
        ctx.strokeStyle = COLORS.selectionBorder;
        ctx.lineWidth = 1;
        ctx.strokeRect(x1, y, x2 - x1, h);

        // Handle bars
        ctx.fillStyle = COLORS.selectionBorder;
        ctx.fillRect(x1 - 2, y, 4, h);
        ctx.fillRect(x2 - 2, y, 4, h);
    }

export function _drawMutedOverlay(host, ctx, x, y, w, h, label = "Hidden") {
        if (w <= 1 || h <= 1) return;
        ctx.save();
        ctx.beginPath();
        ctx.rect(x, y, w, h);
        ctx.clip();
        ctx.fillStyle = "rgba(0, 0, 0, 0.28)";
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = "rgba(230, 230, 230, 0.26)";
        ctx.lineWidth = 1;
        for (let lx = x - h; lx < x + w + h; lx += 8) {
            ctx.beginPath();
            ctx.moveTo(lx, y + h);
            ctx.lineTo(lx + h, y);
            ctx.stroke();
        }
        if (w > 28) {
            ctx.fillStyle = "rgba(0, 0, 0, 0.56)";
            const badgeW = Math.min(w - 4, Math.max(24, label.length * 6 + 8));
            const badgeH = Math.min(14, h - 4);
            ctx.fillRect(x + 3, y + 3, badgeW, badgeH);
            ctx.fillStyle = COLORS.text;
            ctx.font = host._canvasSansFont(Math.max(8, Math.round(8 * host._scaleTimeline)), 500);
            ctx.textAlign = "left";
            ctx.fillText(label, x + 7, y + 3 + badgeH - 4);
        }
        ctx.restore();
    }

function drawLinkBadge(host, ctx, x, y, w, h, group) {
        if (w <= 22 || h <= 12) return;
        // Group identity label (A..Z, AA..) instead of a generic "link" tag;
        // a group containing a locked member shows a lock glyph in danger color
        // on EVERY member so effective lock is visible group-wide.
        const label = (group && host._linkGroupLabel?.(group)) || "link";
        const locked = !!(group && host._isLinkGroupLocked?.(group));
        const text = locked ? `🔒${label}` : label;
        ctx.save();
        ctx.font = host._canvasSansFont(Math.max(7, Math.round(7 * host._scaleTimeline)), 700);
        const textW = ctx.measureText(text).width;
        const badgeW = Math.min(w - 4, Math.max(18, Math.ceil(textW) + 10));
        const badgeH = Math.min(13, Math.max(10, h - 6));
        const badgeX = x + w - badgeW - 4;
        const badgeY = y + 4;
        ctx.globalAlpha = Math.min(1, ctx.globalAlpha * 0.95);
        ctx.fillStyle = "rgba(4, 8, 14, 0.68)";
        ctx.fillRect(badgeX, badgeY, badgeW, badgeH);
        ctx.strokeStyle = locked ? COLORS.dangerText : COLORS.accent;
        ctx.lineWidth = 1;
        ctx.strokeRect(badgeX + 0.5, badgeY + 0.5, badgeW - 1, badgeH - 1);
        ctx.fillStyle = locked ? COLORS.dangerText : COLORS.text;
        ctx.textAlign = "center";
        ctx.fillText(text, badgeX + badgeW / 2, badgeY + badgeH - 3);
        ctx.restore();
    }

export function _drawGuideMarkers(host, ctx, width) {
        if (!host.activeScene) return;
        const gi = host._guidesLayoutIdx();
        if (gi < 0 || host._trackLayout[gi].collapsed) return;

        const guides = host.activeScene.guide_frames || [];
        const y = host._trackY(gi);
        const h = host._trackH(gi);
        const trackHidden = !!host._trackLayout[gi]?.hidden;

        for (const guide of guides) {
            let idx = guide._previewFrameIndex ?? guide.frame_index;
            if (idx === -1) idx = host.totalFrames - 1;
            const guideAsset = host._getGuideAsset(guide);
            const isMissingGuide = !guideAsset || !!guideAsset.missing;

            const x = host._frameToX(idx);
            if (x < 0 || x > width) continue;

            // Diamond marker
            const isSelectedGuide = host._isSelected("guide", guide.frame_index);
            const guideHidden = trackHidden || !!guide.muted;
            const markerHalfW = Math.max(7, Math.round(8 * host._scaleTimeline));
            const markerTop = y + Math.max(4, Math.round(4 * host._scaleTimeline));
            const markerBottom = y + h - Math.max(4, Math.round(4 * host._scaleTimeline));
            const markerCenterY = y + h / 2;
            ctx.save();
            ctx.beginPath();
            ctx.rect(host._labelW, y, Math.max(0, width - host._labelW), h);
            ctx.clip();
            ctx.globalAlpha = guideHidden ? 0.42 : 1.0;
            ctx.fillStyle = isMissingGuide
                ? (isSelectedGuide ? COLORS.missingMediaSelected : COLORS.missingMedia)
                : (isSelectedGuide ? COLORS.guideSelected : COLORS.guide);
            ctx.beginPath();
            ctx.moveTo(x, markerTop);
            ctx.lineTo(x + markerHalfW, markerCenterY);
            ctx.lineTo(x, markerBottom);
            ctx.lineTo(x - markerHalfW, markerCenterY);
            ctx.closePath();
            ctx.fill();
            if (guideHidden) {
                ctx.strokeStyle = "rgba(231,236,242,0.42)";
                ctx.lineWidth = 1;
                ctx.stroke();
            } else {
                ctx.strokeStyle = isMissingGuide
                    ? COLORS.missingMediaBorder
                    : (isSelectedGuide ? COLORS.accent : COLORS.guideBorder);
                ctx.lineWidth = isSelectedGuide ? 2 : 1;
                ctx.stroke();
            }
            ctx.globalAlpha = 1.0;

            // Frame label, kept inside the guide row so it cannot overlap Prompt.
            const label = `f${idx}`;
            const labelFontSize = Math.max(10, Math.round(8 * host._scaleTimeline));
            ctx.fillStyle = isMissingGuide ? COLORS.missingMediaText : (guideHidden ? COLORS.textDim : COLORS.text);
            ctx.font = host._canvasMonoFont(labelFontSize, 500);
            ctx.textAlign = "left";
            const labelPadX = Math.max(3, Math.round(3 * host._scaleTimeline));
            const labelGap = Math.max(4, Math.round(4 * host._scaleTimeline));
            const labelW = Math.ceil(ctx.measureText(label).width) + labelPadX * 2;
            const labelH = labelFontSize + Math.max(3, Math.round(3 * host._scaleTimeline));
            let labelX = x + markerHalfW + labelGap;
            if (labelX + labelW > width - 2) labelX = x - markerHalfW - labelGap - labelW;
            const labelY = Math.round(y + h / 2 + labelFontSize * 0.35);
            if (labelX + labelW > host._labelW && labelX < width) {
                ctx.fillStyle = COLORS.guideLabelBg;
                ctx.fillRect(labelX, labelY - labelFontSize, labelW, labelH);
                ctx.fillStyle = isMissingGuide ? COLORS.missingMediaText : (guideHidden ? COLORS.textDim : COLORS.text);
                ctx.fillText(label, labelX + labelPadX, labelY);
            }
            {
                const linkGroup = host._linkGroupForItem?.({ type: "guide", id: guide.frame_index, data: guide });
                if (linkGroup) drawLinkBadge(host, ctx, x - markerHalfW - 6, markerTop, markerHalfW * 2 + 12, markerBottom - markerTop, linkGroup);
            }
            ctx.restore();
        }
    }

export function _drawClips(host, ctx, width) {
        if (!host.activeScene) return;

        // Helper: draw trimmed-off ghost region during active trim drag
        const drawTrimGhost = (trimItem, trackY, trackH, color) => {
            if (!host._trimItem || host._trimItem.data !== trimItem) return;
            const item = host._trimItem;
            const isPrompt = item.type === "prompt";
            const curStart = isPrompt ? item.data.start_frame : item.data.timeline_start_frame;
            const curEnd = isPrompt ? item.data.end_frame : item.data.timeline_end_frame;
            ctx.globalAlpha = 0.25;
            ctx.fillStyle = color;
            if (item.edge === "left" && curStart > item.origStart) {
                const ghostX1 = host._frameToX(item.origStart);
                const ghostX2 = host._frameToX(curStart);
                ctx.fillRect(ghostX1 + 1, trackY + 2, ghostX2 - ghostX1 - 1, trackH - 4);
            } else if (item.edge === "right" && curEnd < item.origEnd) {
                const ghostX1 = host._frameToX(curEnd);
                const ghostX2 = host._frameToX(item.origEnd);
                ctx.fillRect(ghostX1, trackY + 2, ghostX2 - ghostX1 - 1, trackH - 4);
            }
            ctx.globalAlpha = 1.0;
        };

        // Video and driver clips.
        const allClips = host.activeScene.clips || [];
        for (let _vli = 0; _vli < host._trackLayout.length; _vli++) {
            const _vlEntry = host._trackLayout[_vli];
            if (_vlEntry.type !== TRACK_TYPE.VIDEO && _vlEntry.type !== TRACK_TYPE.MOTION_DRIVER) continue;
            if (_vlEntry.collapsed) continue;
            const videoY = host._trackY(_vli);
            const videoH = host._trackH(_vli);
            const laneHidden = _vlEntry.hidden;
            const isMotionDriverLane = _vlEntry.type === TRACK_TYPE.MOTION_DRIVER;
            const clips = allClips.filter(c => host._clipMatchesTrackEntry(c, _vlEntry));
            for (const clip of clips) {
                const x1 = host._frameToX(clip.timeline_start_frame);
                const x2 = host._frameToX(clip.timeline_end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelectedClip = host._isSelected("clip", clip.clip_id);
                const opacity = clip.opacity ?? 1.0;
                const clipMuted = !!clip.muted;
                const baseAlpha = (laneHidden || clipMuted) ? 0.3 : (opacity < 1.0 ? Math.max(0.3, opacity) : 1.0);
                const clipAsset = host._getAssetForSourcePath(clip.source_path);
                const isMissingClip = !clipAsset || !!clipAsset.missing;
                const roleBaseColor = isMotionDriverLane ? COLORS.motionDriver : COLORS.clip;
                const roleSelectedColor = isMotionDriverLane ? COLORS.motionDriverSelected : COLORS.clipSelected;
                const laneAccentColor = host._timelineLaneAccent(_vlEntry);
                const clipFillColor = isSelectedClip
                    ? roleSelectedColor
                    : roleBaseColor;
                ctx.globalAlpha = baseAlpha;

                // Draw base fill
                ctx.fillStyle = isMissingClip
                    ? (isSelectedClip ? COLORS.missingMediaSelected : COLORS.missingMedia)
                    : clipFillColor;
                ctx.fillRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);

                // Thumbnail strip filmstrip (tiled at natural aspect ratio)
                if (clipAsset && !isMissingClip && (x2 - x1) > 10) {
                    const strip = host._getOrLoadThumbStrip(clipAsset.asset_id);
                    if (strip && strip.loaded && strip.img.naturalWidth > 0) {
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                        ctx.clip();
                        ctx.globalAlpha = baseAlpha * 0.55;

                        const destH = videoH - 4;
                        // Scale each strip frame to fill track height, preserving aspect ratio
                        const tileW = Math.max(1, Math.round(strip.frameWidth * destH / strip.img.naturalHeight));
                        // Strip columns span the whole native file, while clip
                        // source offsets are persisted in scene-frame units.
                        // Use the full media duration in that same unit space;
                        // clip.total_source_frames is only the split segment extent.
                        const totalSourceFrames = host._mediaTimelineFrames(clipAsset);
                        const srcIn = clip.source_in_frame || 0;
                        const srcOut = clip.source_out_frame || totalSourceFrames;
                        // Tile frames across the clip width
                        const clipPixelW = x2 - x1 - 2;
                        for (let px = 0; px < clipPixelW; px += tileW) {
                            // Map this pixel position to a source frame, then to a strip column
                            const frac = px / clipPixelW;
                            const sourceFrame = srcIn + frac * (srcOut - srcIn);
                            const col = Math.floor(sourceFrame / totalSourceFrames * strip.numFrames);
                            const clampedCol = Math.min(col, strip.numFrames - 1);
                            const sx = clampedCol * strip.frameWidth;
                            const drawW = Math.min(tileW, clipPixelW - px);
                            const srcDrawW = drawW / tileW * strip.frameWidth;
                            ctx.drawImage(strip.img, sx, 0, srcDrawW, strip.img.naturalHeight,
                                          x1 + 1 + px, videoY + 2, drawW, destH);
                        }
                        ctx.restore();
                    }
                }
                host._drawTimelineItemRail(ctx, x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4, laneAccentColor);
                if (isSelectedClip) {
                    ctx.strokeStyle = COLORS.accent;
                    ctx.lineWidth = 1.5;
                    ctx.strokeRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                } else if (isMissingClip) {
                    ctx.strokeStyle = COLORS.missingMediaBorder;
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                }
                ctx.globalAlpha = baseAlpha;

                // Opacity visual: diagonal hash lines when opacity < 100%
                if (opacity < 1.0) {
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                    ctx.clip();
                    ctx.strokeStyle = "rgba(0,0,0,0.4)";
                    ctx.lineWidth = 1;
                    const step = 6;
                    for (let lx = x1 - videoH; lx < x2; lx += step) {
                        ctx.beginPath();
                        ctx.moveTo(lx, videoY + videoH - 2);
                        ctx.lineTo(lx + videoH, videoY + 2);
                        ctx.stroke();
                    }
                    ctx.restore();
                }

                // Clip label
                const label = host._formatClipTimelineLabel(clip, clipAsset, isMissingClip);
                if (label) {
                    ctx.fillStyle = isMissingClip ? COLORS.missingMediaText : COLORS.text;
                    const fontSize = Math.round(9 * host._scaleTimeline);
                    ctx.font = host._canvasSansFont(fontSize, 600);
                    const railPad = Math.round(9 * host._scaleTimeline);
                    const labelX = isMotionDriverLane ? x1 + Math.round(30 * host._scaleTimeline) : x1 + railPad;
                    const labelW = Math.max(0, x2 - labelX - 4);
                    const labelH = videoH - 4;
                    const labelPoint = timelineLabelPoint(host, labelX, videoY + 2, labelW, labelH, fontSize);
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(labelX, videoY + 2, labelW, labelH);
                    ctx.clip();
                    ctx.textAlign = labelPoint.align;
                    ctx.fillText(label, labelPoint.x, labelPoint.y);
                    ctx.restore();
                }

                if (isMotionDriverLane && (x2 - x1) > 24) {
                    const badgeW = Math.round(18 * host._scaleTimeline);
                    const badgeH = Math.round(12 * host._scaleTimeline);
                    const badgeX = x1 + Math.round(4 * host._scaleTimeline);
                    const badgeY = videoY + Math.round(5 * host._scaleTimeline);
                    ctx.globalAlpha = baseAlpha;
                    ctx.fillStyle = "rgba(0, 0, 0, 0.42)";
                    ctx.fillRect(badgeX, badgeY, badgeW, badgeH);
                    ctx.fillStyle = COLORS.text;
                    ctx.font = host._canvasSansFont(Math.round(8 * host._scaleTimeline), 600);
                    ctx.textAlign = "center";
                    ctx.fillText("MD", badgeX + badgeW / 2, badgeY + badgeH - Math.round(3 * host._scaleTimeline));
                }

                if (isMissingClip) {
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                    ctx.clip();
                    ctx.strokeStyle = "rgba(223,177,177,0.32)";
                    ctx.lineWidth = 1;
                    for (let lx = x1 - videoH; lx < x2 + videoH; lx += 8) {
                        ctx.beginPath();
                        ctx.moveTo(lx, videoY + videoH - 2);
                        ctx.lineTo(lx + videoH, videoY + 2);
                        ctx.stroke();
                    }
                    ctx.restore();
                }

                if (clipMuted) {
                    host._drawMutedOverlay(ctx, x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4, "Hidden");
                }
                {
                    const linkGroup = host._linkGroupForItem?.({ type: "clip", id: clip.clip_id, data: clip });
                    if (linkGroup) drawLinkBadge(host, ctx, x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4, linkGroup);
                }

                // Permanent trim ghost
                const clipOrigin = clip.source_origin_frame || 0;
                const clipTotal = clip.total_source_frames || 0;
                if (clipTotal > 0) {
                    const leftTrimmed = (clip.source_in_frame || 0) - clipOrigin;
                    const visibleDur = clip.timeline_end_frame - clip.timeline_start_frame;
                    const rightTrimmed = clipTotal - visibleDur - leftTrimmed;
                    if (leftTrimmed > 0) {
                        const ghostX = host._frameToX(clip.timeline_start_frame - leftTrimmed);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = roleBaseColor;
                        ctx.fillRect(ghostX + 1, videoY + 2, x1 - ghostX - 1, videoH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                    if (rightTrimmed > 0) {
                        const ghostX2 = host._frameToX(clip.timeline_end_frame + rightTrimmed);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = roleBaseColor;
                        ctx.fillRect(x2 - 1, videoY + 2, ghostX2 - x2, videoH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                }

                // Active trim drag ghost (during edge-drag)
                if (host.dragType === "trimEdge") drawTrimGhost(clip, videoY, videoH, roleBaseColor);
            }
            ctx.globalAlpha = 1.0;
        }

        // Audio tracks (all audio lanes)
        const allAudioTracks = host.activeScene.audio_tracks || [];
        for (let _ali = 0; _ali < host._trackLayout.length; _ali++) {
            const _alEntry = host._trackLayout[_ali];
            if (_alEntry.type !== TRACK_TYPE.AUDIO || _alEntry.collapsed) continue;
            const audioY = host._trackY(_ali);
            const audioH = host._trackH(_ali);
            const audioLaneHidden = _alEntry.hidden;
            const audioTracks = allAudioTracks.filter(a => (a.lane_index || 0) === _alEntry.laneIndex);
            for (const track of audioTracks) {
                const x1 = host._frameToX(track.timeline_start_frame);
                const x2 = host._frameToX(track.timeline_end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelectedAudio = host._isSelected("audio", track.track_id);
                const vol = track.volume ?? 1.0;
                const audioAsset = host._getAssetForSourcePath(track.source_path);
                const isMissingAudio = !audioAsset || !!audioAsset.missing;
                const audioMuted = audioLaneHidden || !!track.muted;
                const roleBaseColor = COLORS.audioClip;
                const laneAccentColor = host._timelineLaneAccent(_alEntry);
                const audioFillColor = isSelectedAudio ? COLORS.audioClipSelected : roleBaseColor;
                ctx.globalAlpha = audioMuted ? 0.3 : 1.0;
                ctx.fillStyle = isMissingAudio
                    ? (isSelectedAudio ? COLORS.missingMediaSelected : COLORS.missingMedia)
                    : audioFillColor;
                ctx.fillRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                host._drawTimelineItemRail(ctx, x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4, laneAccentColor);

                // Waveform visualization
                if (audioAsset && !isMissingAudio && (x2 - x1) > 6) {
                    const waveform = host._getOrLoadWaveform(audioAsset.asset_id);
                    if (waveform && waveform.loaded && waveform.peaks.length > 0) {
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                        ctx.clip();

                        const clipW = x2 - x1 - 2;
                        const centerY = audioY + audioH / 2;
                        const halfH = (audioH - 8) / 2;

                        // Map visible source frames to waveform buckets
                        const totalDurFrames = audioAsset.duration_sec * host._effectiveFps;
                        const srcIn = track.source_in_frame || 0;
                        const visibleFrames = track.timeline_end_frame - track.timeline_start_frame;
                        const startFrac = totalDurFrames > 0 ? srcIn / totalDurFrames : 0;
                        const endFrac = totalDurFrames > 0 ? (srcIn + visibleFrames) / totalDurFrames : 1;
                        const startBucket = Math.floor(startFrac * waveform.numBuckets);
                        const endBucket = Math.ceil(endFrac * waveform.numBuckets);
                        const bucketSpan = Math.max(1, endBucket - startBucket);

                        ctx.strokeStyle = track.muted ? "rgba(182,191,203,0.35)" : host._waveformAccentColor();
                        ctx.lineWidth = 1;
                        ctx.beginPath();
                        for (let px = 0; px < clipW; px++) {
                            const bi = startBucket + Math.floor(px / clipW * bucketSpan);
                            const peak = waveform.peaks[Math.min(bi, waveform.peaks.length - 1)];
                            if (!peak) continue;
                            const y1 = centerY - peak[1] * halfH;
                            const y2 = centerY - peak[0] * halfH;
                            ctx.moveTo(x1 + 1 + px, y1);
                            ctx.lineTo(x1 + 1 + px, y2);
                        }
                        ctx.stroke();
                        ctx.restore();
                    }
                }

                if (isSelectedAudio) {
                    ctx.strokeStyle = COLORS.accent;
                    ctx.lineWidth = 1.5;
                    ctx.strokeRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                } else if (isMissingAudio) {
                    ctx.strokeStyle = COLORS.missingMediaBorder;
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                }

                // Volume indicator: thin bar at bottom of audio clip
                if (vol < 1.0 && !track.muted) {
                    const volBarW = (x2 - x1 - 4) * vol;
                    ctx.fillStyle = "rgba(231,236,242,0.22)";
                    ctx.fillRect(x1 + 2, audioY + audioH - 5, volBarW, 2);
                }

                // Audio label
                if ((x2 - x1) > 30) {
                    const audioLabel = host._formatAudioTimelineLabel(track, audioAsset, isMissingAudio);
                    if (audioLabel) {
                        ctx.fillStyle = isMissingAudio ? COLORS.missingMediaText : COLORS.text;
                        const fontSize = Math.round(8 * host._scaleTimeline);
                        ctx.font = host._canvasSansFont(fontSize, 600);
                        const labelX = x1 + Math.round(9 * host._scaleTimeline);
                        const labelW = Math.max(0, x2 - labelX - 4);
                        const labelH = audioH - 4;
                        const labelPoint = timelineLabelPoint(host, labelX, audioY + 2, labelW, labelH, fontSize);
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(labelX, audioY + 2, labelW, labelH);
                        ctx.clip();
                        ctx.textAlign = labelPoint.align;
                        ctx.fillText(audioLabel, labelPoint.x, labelPoint.y);
                        ctx.restore();
                    }
                }

                if (isMissingAudio) {
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                    ctx.clip();
                    ctx.strokeStyle = "rgba(223,177,177,0.32)";
                    ctx.lineWidth = 1;
                    for (let lx = x1 - audioH; lx < x2 + audioH; lx += 8) {
                        ctx.beginPath();
                        ctx.moveTo(lx, audioY + audioH - 2);
                        ctx.lineTo(lx + audioH, audioY + 2);
                        ctx.stroke();
                    }
                    ctx.restore();
                }

                if (track.muted) {
                    host._drawMutedOverlay(ctx, x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4, "Muted");
                }
                {
                    const linkGroup = host._linkGroupForItem?.({ type: "audio", id: track.track_id, data: track });
                    if (linkGroup) drawLinkBadge(host, ctx, x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4, linkGroup);
                }

                // Permanent trim ghost for audio
                const audioOrigin = track.source_origin_frame || 0;
                const audioTotal = track.total_source_frames || 0;
                if (audioTotal > 0) {
                    const audioLeftTrim = (track.source_in_frame || 0) - audioOrigin;
                    const audioVisibleDur = track.timeline_end_frame - track.timeline_start_frame;
                    const audioRightTrim = audioTotal - audioVisibleDur - audioLeftTrim;
                    if (audioLeftTrim > 0) {
                        const ghostX = host._frameToX(track.timeline_start_frame - audioLeftTrim);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = roleBaseColor;
                        ctx.fillRect(ghostX + 1, audioY + 2, x1 - ghostX - 1, audioH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                    if (audioRightTrim > 0) {
                        const ghostX2 = host._frameToX(track.timeline_end_frame + audioRightTrim);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = roleBaseColor;
                        ctx.fillRect(x2 - 1, audioY + 2, ghostX2 - x2, audioH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                }

                // Active trim drag ghost
                if (host.dragType === "trimEdge") drawTrimGhost(track, audioY, audioH, roleBaseColor);
            }
            ctx.globalAlpha = 1.0;
        }

        // Global prompt lane — one full-width non-draggable item showing the
        // scene-global prompt text (Scene.prompt)
        const gpi = host._globalPromptLayoutIdx();
        if (gpi >= 0 && !host._trackLayout[gpi].collapsed) {
            const globalY = host._trackY(gpi);
            const globalH = host._trackH(gpi);
            const globalHidden = !!host._trackLayout[gpi]?.hidden;
            const gx1 = host._frameToX(0);
            const gx2 = host._frameToX(host.totalFrames || host.activeScene.duration_frames || 0);
            if (gx2 >= 0 && gx1 <= width) {
                const globalText = String(host.activeScene.prompt || "");
                const isSelected = host._isSelected("prompt_global", 0);
                ctx.globalAlpha = globalHidden ? 0.42 : 1.0;
                ctx.fillStyle = isSelected ? COLORS.promptSectionSelected : COLORS.promptSection;
                ctx.fillRect(gx1 + 1, globalY + 2, gx2 - gx1 - 2, globalH - 4);
                host._drawTimelineItemRail(ctx, gx1 + 1, globalY + 2, gx2 - gx1 - 2, globalH - 4, COLORS.lanePrompt);
                ctx.strokeStyle = isSelected ? COLORS.accent : COLORS.promptBorder;
                ctx.lineWidth = isSelected ? 1.5 : 1;
                ctx.strokeRect(gx1 + 1, globalY + 2, gx2 - gx1 - 2, globalH - 4);
                if ((gx2 - gx1) > 20) {
                    ctx.fillStyle = COLORS.text;
                    ctx.font = host._canvasSansFont(Math.round(9 * host._scaleTimeline), 500);
                    ctx.textAlign = "left";
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(gx1 + 3, globalY + 2, gx2 - gx1 - 6, globalH - 4);
                    ctx.clip();
                    const labelText = globalText || "Global prompt (empty)";
                    ctx.globalAlpha = (globalHidden ? 0.42 : 1.0) * (globalText ? 1.0 : 0.55);
                    ctx.fillText(labelText, gx1 + Math.round(9 * host._scaleTimeline), globalY + globalH / 2 + Math.round(3 * host._scaleTimeline));
                    ctx.restore();
                }
                ctx.globalAlpha = 1.0;
                if (globalHidden) {
                    host._drawMutedOverlay(ctx, gx1 + 1, globalY + 2, gx2 - gx1 - 2, globalH - 4, "Hidden");
                }
            }
        }

        // Prompt sections
        const pi = host._promptLayoutIdx();
        if (pi >= 0 && !host._trackLayout[pi].collapsed) {
            const sections = host.activeScene.prompt_sections || [];
            const promptY = host._trackY(pi);
            const promptH = host._trackH(pi);
            const promptHidden = !!host._trackLayout[pi]?.hidden;
            for (let si = 0; si < sections.length; si++) {
                const section = sections[si];
                const x1 = host._frameToX(section.start_frame);
                const x2 = host._frameToX(section.end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelected = host._isSelected("prompt", si) ||
                    (host._selectedPromptIdx !== null &&
                    host._selectedPromptIdx < sections.length &&
                    sections[host._selectedPromptIdx] === section);
                const sectionMuted = !!section.muted;
                const sectionHidden = promptHidden || sectionMuted;

                ctx.globalAlpha = sectionHidden ? 0.42 : 1.0;
                ctx.fillStyle = isSelected ? COLORS.promptSectionSelected : COLORS.promptSection;
                ctx.fillRect(x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4);
                host._drawTimelineItemRail(ctx, x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4, COLORS.lanePrompt);

                ctx.strokeStyle = isSelected ? COLORS.accent : COLORS.promptBorder;
                ctx.lineWidth = isSelected ? 1.5 : 1;
                ctx.strokeRect(x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4);

                // Prompt text label (truncated)
                if (section.prompt && (x2 - x1) > 20) {
                    ctx.fillStyle = COLORS.text;
                    ctx.font = host._canvasSansFont(Math.round(9 * host._scaleTimeline), 500);
                    ctx.textAlign = "left";
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 3, promptY + 2, x2 - x1 - 6, promptH - 4);
                    ctx.clip();
                    ctx.fillText(section.prompt, x1 + Math.round(9 * host._scaleTimeline), promptY + promptH / 2 + Math.round(3 * host._scaleTimeline));
                    ctx.restore();
                }
                ctx.globalAlpha = 1.0;
                if (sectionHidden) {
                    host._drawMutedOverlay(ctx, x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4, sectionMuted ? "Muted" : "Hidden");
                }
                // Selection prompt-usage highlight (keyed on authored start_frame):
                // a strong top accent for sections the current selection window
                // will output, a dim "Ignored" hatch for sections it clips but
                // the boundary threshold drops.
                if (host._promptUsedSections?.has(section.start_frame)) {
                    ctx.fillStyle = COLORS.accent;
                    ctx.fillRect(x1 + 1, promptY + 2, x2 - x1 - 2, Math.max(2, Math.round(3 * host._scaleTimeline)));
                } else if (host._promptDroppedSections?.has(section.start_frame)) {
                    ctx.save();
                    ctx.globalAlpha = 0.5;
                    host._drawMutedOverlay(ctx, x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4, "Ignored");
                    ctx.restore();
                }
                {
                    const linkGroup = host._linkGroupForItem?.({ type: "prompt", id: si, data: section });
                    if (linkGroup) drawLinkBadge(host, ctx, x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4, linkGroup);
                }

                // Trim ghost
                if (host.dragType === "trimEdge") drawTrimGhost(section, promptY, promptH, COLORS.motionDriver);
            }
        }
    }

export function _drawPlayheadTriangle(host, ctx, width) {
        const x = host._frameToX(host.playhead);
        if (x < host._labelW || x > width) return;

        // Triangle at top
        ctx.save();
        ctx.beginPath();
        ctx.rect(host._labelW, 0, Math.max(0, width - host._labelW), host._timelineRulerHeight());
        ctx.clip();
        ctx.fillStyle = COLORS.playhead;
        ctx.beginPath();
        ctx.moveTo(x - 6, 0);
        ctx.lineTo(x + 6, 0);
        ctx.lineTo(x, 8);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
    }

export function _drawPlayheadLine(host, ctx, width) {
        const x = host._frameToX(host.playhead);
        if (x < host._labelW || x > width) return;

        const rulerH = host._timelineRulerHeight();
        const totalH = rulerH + host._totalTracksHeight();
        ctx.strokeStyle = COLORS.playhead;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x, rulerH);
        ctx.lineTo(x, totalH);
        ctx.stroke();
    }

export function _drawSnapIndicator(host, ctx, width, height = host.timelineCanvas?.height || 0) {
        if (host._snapIndicator === null) return;
        const x = host._frameToX(host._snapIndicator);
        if (x < host._labelW || x > width) return;

        ctx.save();
        ctx.beginPath();
        ctx.rect(host._labelW, 0, Math.max(0, width - host._labelW), height);
        ctx.clip();
        ctx.strokeStyle = COLORS.snapIndicator;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, height);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
    }

export function _drawVerticalScrollbar(host, ctx, width, height) {
        const rulerH = host._timelineRulerHeight();
        const visibleH = Math.max(0, height - rulerH);
        const contentH = host._totalTracksHeight();
        if (visibleH <= 0 || contentH <= visibleH) return;

        const trackX = Math.max(host._labelW + 4, width - 8);
        const trackY = rulerH + 2;
        const trackH = Math.max(8, visibleH - 4);
        const thumbH = Math.max(18, Math.round((visibleH / contentH) * trackH));
        const maxThumbOffset = Math.max(0, trackH - thumbH);
        const scrollRatio = host.scrollY / Math.max(1, contentH - visibleH);
        const thumbY = trackY + Math.round(maxThumbOffset * scrollRatio);

        ctx.fillStyle = "rgba(0,0,0,0.32)";
        ctx.fillRect(trackX, trackY, 5, trackH);
        ctx.fillStyle = "rgba(190,205,220,0.58)";
        ctx.fillRect(trackX + 1, thumbY, 3, thumbH);
    }

    // _updateInfoLabel removed — merged into _updateToolbar()

    // ── Hit Testing ──────────────────────────────────────────────────
    /** Hit-test track header area — returns { layoutIdx, zone } or null */
export function _hitTestTrackHeader(host, x, rawY) {
        const headerWidth = host._labelW; // already scaled by _scaleTrackHeaders
        if (x > headerWidth) return null;
        const fs = host.isFullscreen;
        const hs = host._scaleTrackHeaders;
        const iconSize = Math.round((fs ? 14 : 11) * hs);
        const layoutIdx = host._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = host._trackLayout[layoutIdx];
        const hasHeaderControls = host._isHeaderControllableTrackType(entry.type);
        if (entry.collapsed || !hasHeaderControls) {
            return { layoutIdx, zone: "collapse" };
        }
        // Zone detection (left to right) matching _drawTracks layout
        const arrowX = Math.round((fs ? 6 : 3) * hs);
        let zoneEnd = arrowX + iconSize + Math.round(2 * hs);
        if (x < zoneEnd) return { layoutIdx, zone: "collapse" };
        zoneEnd += iconSize + Math.round(1 * hs);
        if (x < zoneEnd) return { layoutIdx, zone: "lock" };
        zoneEnd += iconSize + Math.round(1 * hs);
        if (x < zoneEnd) return { layoutIdx, zone: "hide" };
        // Fixed tracks carry a 4th "manage" icon (☰) opening the respective
        // management panel — discoverability for the guide/prompt tooling
        if (entry.type === TRACK_TYPE.GUIDES || entry.type === TRACK_TYPE.PROMPT
            || entry.type === TRACK_TYPE.PROMPT_GLOBAL) {
            zoneEnd += iconSize + Math.round(1 * hs);
            if (x < zoneEnd) return { layoutIdx, zone: "manage" };
        }
        return { layoutIdx, zone: "label" };
    }

    /** Hit-test the header/timeline boundary for drag resize */
export function _hitTestHeaderEdge(host, x, y) {
        const headerW = host._labelW;
        const rulerHeight = Math.round(RULER_HEIGHT * (host._scaleTimeline || 1));
        return Math.abs(x - headerW) <= 4 && y >= 0 && y < rulerHeight;
    }

export function _hitTestClip(host, x, rawY) {
        if (!host.activeScene) return null;
        const layoutIdx = host._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = host._trackLayout[layoutIdx];
        if ((entry.type !== TRACK_TYPE.VIDEO && entry.type !== TRACK_TYPE.MOTION_DRIVER) || entry.collapsed) return null;
        const clips = host.activeScene.clips || [];
        for (const clip of clips) {
            if (!host._clipMatchesTrackEntry(clip, entry)) continue;
            const x1 = host._frameToX(clip.timeline_start_frame);
            const x2 = host._frameToX(clip.timeline_end_frame);
            if (x >= x1 && x <= x2) {
                return { type: "clip", id: clip.clip_id, data: clip };
            }
        }
        return null;
    }

export function _hitTestAudio(host, x, rawY) {
        if (!host.activeScene) return null;
        const layoutIdx = host._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = host._trackLayout[layoutIdx];
        if (entry.type !== TRACK_TYPE.AUDIO || entry.collapsed) return null;
        const tracks = host.activeScene.audio_tracks || [];
        for (const track of tracks) {
            if ((track.lane_index || 0) !== entry.laneIndex) continue;
            const x1 = host._frameToX(track.timeline_start_frame);
            const x2 = host._frameToX(track.timeline_end_frame);
            if (x >= x1 && x <= x2) {
                return { type: "audio", id: track.track_id, data: track };
            }
        }
        return null;
    }

export function _hitTestGuide(host, x, rawY) {
        if (!host.activeScene) return null;
        const gi = host._guidesLayoutIdx();
        if (gi < 0 || host._trackLayout[gi].collapsed || host._layoutIndexFromRawY(rawY) !== gi) return null;
        const guides = host.activeScene.guide_frames || [];

        for (const guide of guides) {
            let idx = guide.frame_index;
            if (idx === -1) idx = host.totalFrames - 1;
            const gx = host._frameToX(idx);
            if (Math.abs(x - gx) <= 10) {
                return { type: "guide", id: guide.frame_index, data: guide };
            }
        }
        return null;
    }

export function _hitTestPrompt(host, x, rawY) {
        if (!host.activeScene) return null;
        const pli = host._promptLayoutIdx();
        if (pli < 0 || host._trackLayout[pli].collapsed || host._layoutIndexFromRawY(rawY) !== pli) return null;
        const sections = host.activeScene.prompt_sections || [];

        for (let i = 0; i < sections.length; i++) {
            const section = sections[i];
            const x1 = host._frameToX(section.start_frame);
            const x2 = host._frameToX(section.end_frame);
            if (x >= x1 && x <= x2) {
                return { type: "prompt", id: i, data: section };
            }
        }
        return null;
    }

export function _hitTestGlobalPrompt(host, x, rawY) {
        if (!host.activeScene) return null;
        const gpi = host._globalPromptLayoutIdx();
        if (gpi < 0 || host._trackLayout[gpi].collapsed || host._layoutIndexFromRawY(rawY) !== gpi) return null;
        const x1 = host._frameToX(0);
        const x2 = host._frameToX(host.totalFrames || host.activeScene.duration_frames || 0);
        if (x >= x1 && x <= x2) {
            return { type: "prompt_global", id: 0, data: { prompt: host.activeScene.prompt || "" } };
        }
        return null;
    }

export function _hitTestItem(host, x, rawY) {
        return host._hitTestClip(x, rawY) || host._hitTestAudio(x, rawY) || host._hitTestGuide(x, rawY) || host._hitTestPrompt(x, rawY) || host._hitTestGlobalPrompt(x, rawY);
    }

export function _hitTestEdge(host, x, rawY) {
        const edgePx = 6;
        if (!host.activeScene) return null;
        const layoutIdx = host._layoutIndexFromRawY(rawY);
        if (layoutIdx < 0) return null;
        const entry = host._trackLayout[layoutIdx];
        const candidates = [];
        const addCandidate = (type, id, data, edge, edgeX, startFrame) => {
            const distance = Math.abs(x - edgeX);
            if (distance >= edgePx) return;
            candidates.push({
                type,
                id,
                data,
                edge,
                edgeX,
                startFrame,
                distance,
                selected: !!host._isSelected?.(type, id),
            });
        };

        if ((entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.MOTION_DRIVER) && !entry.collapsed) {
            for (const clip of (host.activeScene.clips || [])) {
                if (!host._clipMatchesTrackEntry(clip, entry)) continue;
                const x1 = host._frameToX(clip.timeline_start_frame);
                const x2 = host._frameToX(clip.timeline_end_frame);
                addCandidate("clip", clip.clip_id, clip, "left", x1, clip.timeline_start_frame);
                addCandidate("clip", clip.clip_id, clip, "right", x2, clip.timeline_start_frame);
            }
        }

        if (entry.type === TRACK_TYPE.AUDIO && !entry.collapsed) {
            for (const track of (host.activeScene.audio_tracks || [])) {
                if ((track.lane_index || 0) !== entry.laneIndex) continue;
                const x1 = host._frameToX(track.timeline_start_frame);
                const x2 = host._frameToX(track.timeline_end_frame);
                addCandidate("audio", track.track_id, track, "left", x1, track.timeline_start_frame);
                addCandidate("audio", track.track_id, track, "right", x2, track.timeline_start_frame);
            }
        }

        if (entry.type === TRACK_TYPE.PROMPT && !entry.collapsed) {
            const sections = host.activeScene.prompt_sections || [];
            for (let i = 0; i < sections.length; i++) {
                const section = sections[i];
                const x1 = host._frameToX(section.start_frame);
                const x2 = host._frameToX(section.end_frame);
                addCandidate("prompt", i, section, "left", x1, section.start_frame);
                addCandidate("prompt", i, section, "right", x2, section.start_frame);
            }
        }

        candidates.sort((left, right) => {
            if (left.distance !== right.distance) return left.distance - right.distance;
            const leftSideRank = x < left.edgeX
                ? (left.edge === "right" ? 0 : 1)
                : x > left.edgeX
                    ? (left.edge === "left" ? 0 : 1)
                    : (left.edge === "right" ? 0 : 1);
            const rightSideRank = x < right.edgeX
                ? (right.edge === "right" ? 0 : 1)
                : x > right.edgeX
                    ? (right.edge === "left" ? 0 : 1)
                    : (right.edge === "right" ? 0 : 1);
            if (leftSideRank !== rightSideRank) return leftSideRank - rightSideRank;
            if (left.selected !== right.selected) return left.selected ? -1 : 1;
            if (left.startFrame !== right.startFrame) return left.startFrame - right.startFrame;
            return String(left.id).localeCompare(String(right.id));
        });
        const hit = candidates[0];
        if (!hit) return null;
        return { type: hit.type, id: hit.id, data: hit.data, edge: hit.edge };
    }
