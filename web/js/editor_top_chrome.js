import { ASPECT_RATIO_PRESETS } from "./editor_settings.js";
import { subscribeForeground, formatProgress } from "./editor_notifications.js";
import {
    EDITOR_COLORS as COLORS,
    FONT,
    THEME,
    TYPE,
    chromeButtonCss,
    chromeDividerCss,
    chromeInputCss as themeChromeInputCss,
    chromeSelectCss as themeChromeSelectCss,
    setButtonVariant,
    statusPillCss,
} from "./editor_theme.js";

const BUTTON_OPTIONS = Object.freeze({
    primary: { variant: "primary", padding: "3px 9px", fontSize: `${TYPE.t11}px`, radius: "4px" },
    secondary: { variant: "secondary", padding: "3px 9px", fontSize: `${TYPE.t11}px`, radius: "4px" },
    tertiary: { variant: "tertiary", padding: "3px 8px", fontSize: `${TYPE.t11}px`, radius: "4px" },
    icon: { variant: "tertiary", padding: "2px 7px", fontSize: `${TYPE.t12}px`, radius: "4px" },
});

function labelCss({ marginLeft = "0", fontSize = `${TYPE.t10}px` } = {}) {
    return `
        color: ${THEME.fg2};
        font-family: ${FONT.sans};
        font-size: ${fontSize};
        margin-left: ${marginLeft};
        white-space: nowrap;
    `;
}

function topInputCss({ width = "auto", fontSize = `${TYPE.t11}px`, padding = "3px 6px", textAlign = "center" } = {}) {
    return `
        ${themeChromeInputCss({ padding, fontSize })}
        width: ${width};
        background: ${COLORS.panelRaised};
        text-align: ${textAlign};
        box-sizing: border-box;
    `;
}

function topSelectCss({ width = "auto", fontSize = `${TYPE.t10}px`, padding = "2px 5px", textAlign = "left" } = {}) {
    return `
        ${themeChromeSelectCss({ padding, fontSize })}
        width: ${width};
        background: ${COLORS.panelRaised};
        text-align: ${textAlign};
        box-sizing: border-box;
    `;
}

function applyTopButtonVariant(button, variant = "secondary", options = BUTTON_OPTIONS.secondary, extraCss = "") {
    if (!button) return button;
    setButtonVariant(button, variant, options);
    if (extraCss) {
        button.style.cssText += extraCss;
    }
    return button;
}

function makeButton(text, title = "", options = BUTTON_OPTIONS.secondary, extraCss = "") {
    const button = document.createElement("button");
    button.textContent = text;
    button.title = title;
    applyTopButtonVariant(button, options.variant || "secondary", options, extraCss);
    return button;
}

function makeToolButton(label, shortcut, tooltip, toggle) {
    const button = makeButton(
        label,
        shortcut ? `${tooltip} [${shortcut}]` : tooltip,
        BUTTON_OPTIONS.secondary,
        "white-space:nowrap;"
    );
    button.addEventListener("click", toggle);
    return button;
}

function makeDivider(height = 16) {
    const divider = document.createElement("span");
    divider.style.cssText = chromeDividerCss(height);
    return divider;
}

function makeInlineGroup(...children) {
    const group = document.createElement("span");
    group.style.cssText = "display:inline-flex;align-items:center;gap:4px;white-space:nowrap;flex:0 0 auto;";
    group.append(...children);
    return group;
}

function makeTextInput(title, cssText, apply, onEscape) {
    const input = document.createElement("input");
    input.type = "text";
    input.inputMode = "decimal";
    input.title = title;
    input.style.cssText = cssText;
    input.addEventListener("change", () => apply(input.value));
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            apply(input.value);
            event.preventDefault();
        } else if (event.key === "Escape") {
            onEscape?.();
        }
        event.stopPropagation();
    });
    return input;
}

function makeStepButtons(title, onStep) {
    const wrap = document.createElement("span");
    wrap.style.cssText = "display:inline-flex;flex-direction:column;gap:1px;flex:0 0 auto;";
    const makeStep = (direction, label) => {
        const button = makeButton(
            label,
            `${title} ${direction === "up" ? "up" : "down"}`,
            { ...BUTTON_OPTIONS.tertiary, padding: "0", fontSize: "8px", radius: "3px" },
            "width:13px;height:11px;min-width:13px;line-height:9px;display:inline-flex;align-items:center;justify-content:center;"
        );
        button.type = "button";
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            onStep(direction);
        });
        return button;
    };
    wrap.append(makeStep("up", "\u25b2"), makeStep("down", "\u25bc"));
    return wrap;
}

function buildQueueBadges(widget, queue = widget._renderQueue) {
    const counts = { running: 0, pending: 0 };
    for (const job of Array.isArray(queue) ? queue : []) {
        const status = String(job?.status || "pending").trim().toLowerCase();
        if (status === "running") {
            counts.running += 1;
        } else if (status === "pending") {
            counts.pending += 1;
        }
    }

    if (widget.renderQueueActive === false) {
        return {
            badges: [{ text: "Inactive", state: "idle" }],
            counts,
        };
    }

    const badges = [];
    if (counts.running > 0) {
        badges.push({ text: `${counts.running} Running`, state: "running" });
    }
    if (counts.pending > 0) {
        badges.push({ text: `${counts.pending} Pending`, state: "pending" });
    }
    if (!badges.length) {
        badges.push({ text: "Idle", state: "idle" });
    }
    return { badges, counts };
}

function makeStatusPill({ text, state }) {
    const pill = document.createElement("span");
    pill.style.cssText = `${statusPillCss({ state, padding: "2px 7px" })}font-size:${TYPE.t10}px;line-height:1.35;`;

    const dot = document.createElement("span");
    dot.style.cssText = `
        width: 6px;
        height: 6px;
        border-radius: 999px;
        background: var(--sonder-status-color);
        flex: 0 0 auto;
    `;

    const label = document.createElement("span");
    label.textContent = text;
    pill.append(dot, label);
    return pill;
}

// Foreground-progress pill: a single toolbar pill showing the highest-priority
// active foreground progress (encode/export/bridge). Coexists with — never
// replaces — the Idle/Running/Pending queue pills. Driven by the notification
// Core's subscribeForeground (most-recent foreground progress wins).
function makeForegroundPill() {
    const pill = document.createElement("span");
    pill.dataset.sonderForegroundPill = "1";
    pill.style.cssText = `${statusPillCss({ state: "progress", padding: "2px 7px" })}font-size:${TYPE.t10}px;line-height:1.35;margin-left:6px;display:none;max-width:200px;`;
    const dot = document.createElement("span");
    dot.style.cssText = `width:6px;height:6px;border-radius:999px;background:var(--sonder-status-color);flex:0 0 auto;`;
    const label = document.createElement("span");
    label.style.cssText = `overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;
    pill.append(dot, label);
    pill._label = label;
    return pill;
}

function updateForegroundPill(widget, item) {
    const pill = widget._foregroundPill;
    if (!pill) return;
    if (!item) {
        pill.style.display = "none";
        return;
    }
    const prog = formatProgress(item.progress);
    let text;
    if (item.verb) text = prog ? `${item.verb}: ${prog}` : `${item.verb}…`;
    else text = item.message || "Working…";
    pill._label.textContent = text;
    pill.title = text;
    pill.style.display = "inline-flex";
}

function wireForegroundPill(widget) {
    if (widget._foregroundPillUnsub) {
        widget._foregroundPillUnsub();
        widget._foregroundPillUnsub = null;
    }
    widget._foregroundPillUnsub = subscribeForeground((item) => updateForegroundPill(widget, item));
}

export function buildEditorSceneBar(widget, { sceneBarHeight = 36 } = {}) {
    // Scene geometry + generation context/mask inputs. No longer a standalone bar \u2014
    // buildEditorToolbar assembles these groups into the single toolbar row (geometry
    // on the right under the viewport; ctx/mask inside the generation-window block).
    // Scene switching lives in the fullscreen breadcrumb identity zone.

    widget._durLabel = document.createElement("span");
    widget._durLabel.style.cssText = labelCss({});
    widget._durLabel.textContent = "Frames:";

    widget.durationInput = document.createElement("input");
    widget.durationInput.type = "number";
    widget.durationInput.min = 1;
    widget.durationInput.max = 99999;
    widget.durationInput.value = widget.totalFrames;
    widget.durationInput.style.cssText = topInputCss({ width: "55px", fontSize: `${TYPE.t11}px`, padding: "2px 4px" });
    widget.durationInput.addEventListener("change", () => {
        if (widget._timecodeMode === "timecode") {
            const sec = parseFloat(widget.durationInput.value) || 0;
            widget.totalFrames = Math.max(1, widget._secondsToFrames(sec));
        } else {
            widget.totalFrames = Math.max(1, parseInt(widget.durationInput.value, 10) || 200);
        }
        widget.totalFrames = widget._snapSceneDurationToTemplate(widget.totalFrames);
        widget._refreshDurationInput();
        if (widget.activeScene) {
            widget._updateSceneDuration(widget.totalFrames);
        }
        widget._renderTimeline();
    });

    const ctxLabel = document.createElement("span");
    ctxLabel.style.cssText = labelCss({ marginLeft: "0" });
    ctxLabel.textContent = "Ctx:";

    const ctxInputStyle = topInputCss({ width: "40px", fontSize: `${TYPE.t10}px`, padding: "2px 3px", textAlign: "left" });
    const preCtxLabel = document.createElement("span");
    preCtxLabel.style.cssText = labelCss({ fontSize: "9px" });
    preCtxLabel.textContent = "Pre";
    widget._preContextInput = makeTextInput(
        "Frames to include before the selected generation range",
        ctxInputStyle,
        () => widget._updateContextFrameWidgets(),
        () => widget._refreshContextInputs()
    );

    const postCtxLabel = document.createElement("span");
    postCtxLabel.style.cssText = labelCss({ fontSize: "9px" });
    postCtxLabel.textContent = "Post";
    widget._postContextInput = makeTextInput(
        "Frames to include after the selected generation range",
        ctxInputStyle,
        () => widget._updateContextFrameWidgets(),
        () => widget._refreshContextInputs()
    );

    const maskLabel = document.createElement("span");
    maskLabel.style.cssText = labelCss({ marginLeft: "4px" });
    maskLabel.textContent = "Mask:";
    const maskPreLabel = document.createElement("span");
    maskPreLabel.style.cssText = labelCss({ fontSize: "9px" });
    maskPreLabel.textContent = "-";
    widget._maskPreOffsetInput = makeTextInput(
        "Extra pre-context frames excluded from denoise mask start",
        ctxInputStyle,
        () => widget._updateContextFrameWidgets(),
        () => widget._refreshContextInputs()
    );

    const maskPostLabel = document.createElement("span");
    maskPostLabel.style.cssText = labelCss({ fontSize: "9px" });
    maskPostLabel.textContent = "+";
    widget._maskPostOffsetInput = makeTextInput(
        "Extra post-context frames included in denoise mask end",
        ctxInputStyle,
        () => widget._updateContextFrameWidgets(),
        () => widget._refreshContextInputs()
    );

    const resLabel = document.createElement("span");
    resLabel.style.cssText = labelCss({ marginLeft: "6px" });
    resLabel.textContent = "Res:";
    const resolutionInputStyle = topInputCss({ width: "48px", fontSize: `${TYPE.t10}px`, padding: "2px 3px" });
    widget._resWInput = document.createElement("input");
    widget._resWInput.type = "number";
    widget._resWInput.min = 0;
    widget._resWInput.max = 4096;
    widget._resWInput.step = 8;
    widget._resWInput.placeholder = "W";
    widget._resWInput.style.cssText = resolutionInputStyle;
    widget._resWInput.addEventListener("change", () => widget._onResolutionChange("w"));

    const xLabel = document.createElement("span");
    xLabel.style.cssText = labelCss({ fontSize: "9px" });
    xLabel.textContent = "x";
    widget._resHInput = document.createElement("input");
    widget._resHInput.type = "number";
    widget._resHInput.min = 0;
    widget._resHInput.max = 4096;
    widget._resHInput.step = 8;
    widget._resHInput.placeholder = "H";
    widget._resHInput.style.cssText = resolutionInputStyle;
    widget._resHInput.addEventListener("change", () => widget._onResolutionChange("h"));

    widget._aspectRatioSelect = document.createElement("select");
    widget._aspectRatioSelect.style.cssText = topSelectCss({ width: "48px", fontSize: "9px", padding: "1px 4px" });
    for (const preset of ASPECT_RATIO_PRESETS) {
        const option = document.createElement("option");
        option.value = widget._aspectRatioOptionValue(preset.a, preset.b);
        option.textContent = preset.label;
        widget._aspectRatioSelect.appendChild(option);
    }
    widget._aspectRatioSelect.addEventListener("change", () => {
        widget._markResolutionSelectionPinned?.();
        widget._resetFreeAspectTierDraft();
        widget._updateResolutionInputMode();
        widget._recalculateResolution();
    });

    widget._resTierSelect = document.createElement("select");
    widget._resTierSelect.style.cssText = topSelectCss({ width: "92px", fontSize: "9px", padding: "1px 4px" });
    widget._resTierSelect.addEventListener("change", () => {
        widget._markResolutionSelectionPinned?.();
        widget._resetFreeAspectTierDraft();
        widget._recalculateResolution();
    });

    widget._templateSelect = document.createElement("select");
    widget._templateSelect.style.cssText = topSelectCss({ width: "64px", fontSize: "9px", padding: "1px 4px" });
    widget._templateSelect.addEventListener("change", () => widget._handleTemplateSelectionChange());
    widget._rebuildTemplateOptions();
    widget._rebuildResolutionTierOptions();
    widget._applyTemplateConstraintMetadata();
    widget._updateResolutionInputMode();

    const fpsLabel = document.createElement("span");
    fpsLabel.style.cssText = labelCss({ marginLeft: "6px" });
    fpsLabel.textContent = "FPS:";
    widget._fpsInput = document.createElement("input");
    widget._fpsInput.type = "number";
    widget._fpsInput.min = 0;
    widget._fpsInput.max = 120;
    widget._fpsInput.step = 0.001;
    widget._fpsInput.placeholder = String(widget.fps);
    widget._fpsInput.style.cssText = topInputCss({ width: "42px", fontSize: `${TYPE.t10}px`, padding: "2px 3px" });
    widget._fpsInput.addEventListener("change", () => {
        const template = widget._getActiveTemplate();
        const rawValue = parseFloat(widget._fpsInput.value) || 0;
        const value = widget._snapSceneFpsToTemplate(rawValue, template);
        widget._fpsInput.value = value || "";
        if (widget.activeScene) {
            widget._updateSceneFps(value);
        }
    });

    // Context + Mask sub-group — placed inside the generation-window block by the toolbar.
    widget._ctxMaskGroup = document.createElement("div");
    widget._ctxMaskGroup.style.cssText = "display:flex; align-items:center; gap:4px; row-gap:3px; flex-wrap:wrap; min-width:0; max-width:100%; flex:0 1 auto;";
    widget._ctxMaskGroup.append(
        ctxLabel,
        makeInlineGroup(preCtxLabel, widget._preContextInput, makeStepButtons("Pre context", (direction) => widget._stepContextFrameInput("pre", direction))),
        makeInlineGroup(postCtxLabel, widget._postContextInput, makeStepButtons("Post context", (direction) => widget._stepContextFrameInput("post", direction))),
        maskLabel,
        makeInlineGroup(maskPreLabel, widget._maskPreOffsetInput, makeStepButtons("Mask pre offset", (direction) => widget._stepContextFrameInput("maskPre", direction))),
        makeInlineGroup(maskPostLabel, widget._maskPostOffsetInput, makeStepButtons("Mask post offset", (direction) => widget._stepContextFrameInput("maskPost", direction)))
    );

    // Scene geometry group — placed on the right of the toolbar row (under the viewport).
    widget._sceneGeometryGroup = document.createElement("div");
    widget._sceneGeometryGroup.style.cssText = "display:flex; align-items:center; align-content:flex-start; justify-content:center; gap:6px; row-gap:4px; flex-wrap:wrap-reverse; min-width:0; max-width:100%; flex:0 1 720px; margin-bottom:3px;";
    widget._sceneGeometryGroup.append(
        makeInlineGroup(widget._durLabel, widget.durationInput),
        makeInlineGroup(resLabel, widget._resWInput, xLabel, widget._resHInput),
        widget._aspectRatioSelect, widget._resTierSelect, widget._templateSelect,
        makeInlineGroup(fpsLabel, widget._fpsInput)
    );
}

export function buildEditorToolbar(widget) {
    const toolbar = document.createElement("div");
    // The toolbar is `flex-wrap: nowrap`: it holds four group children (history, tools, geometry,
    // utilities) that always stay side-by-side; each wraps its OWN items internally (wrap-reverse).
    // No align-items (default stretch) so every group fills the toolbar height: the one-row end
    // groups (history, utilities) then center their buttons vertically (their own align-items:center),
    // while the geometry group pins its row to the floor via align-content:flex-start (with
    // wrap-reverse, cross-start = bottom).
    toolbar.style.cssText = `
        display: flex;
        gap: 3px;
        flex-wrap: nowrap;
        row-gap: 4px;
        min-width: 0;
        padding: 4px 7px;
        background: ${THEME.bg1};
        border-bottom: 1px solid ${THEME.line2};
        font-size: ${TYPE.t10}px;
        min-height: 28px;
        box-sizing: border-box;
    `;

    widget._toolBtnSnap = makeToolButton("\u229e Snap", "S", "Toggle snapping", () => {
        widget._setSnappingEnabled(!widget.snappingEnabled);
    });

    widget._toolBtnRazor = makeToolButton("\u2702 Cut", "C", "Toggle razor/cut mode", () => {
        widget._razorMode = !widget._razorMode;
        widget._updateToolbar();
    });

    const cutHereBtn = makeToolButton("\u2307 Split Here", "", "Split clip/audio at playhead", async () => {
        const selectedTargets = widget.selectedItems
            .filter((item) => (item.type === "clip" || item.type === "audio")
                && widget.playhead > item.data.timeline_start_frame
                && widget.playhead < item.data.timeline_end_frame);
        if (selectedTargets.length) {
            for (const hit of selectedTargets) {
                await widget._splitClipAtFrame(hit, widget.playhead);
            }
            return;
        }

        const clip = widget._getClipAtFrame(widget.playhead) || widget._getMotionDriverClipAtFrame(widget.playhead);
        if (clip) {
            await widget._splitClipAtFrame({ type: "clip", id: clip.clip_id, data: clip }, widget.playhead);
        }
        const audio = widget._getAudioAtFrame(widget.playhead);
        if (audio) {
            await widget._splitClipAtFrame({ type: "audio", id: audio.track_id, data: audio }, widget.playhead);
        }
    });
    cutHereBtn.title = "Split clip/audio at current playhead position";

    const frameLabel = document.createElement("span");
    frameLabel.style.cssText = labelCss({ marginLeft: "2px", fontSize: "9px" });
    frameLabel.textContent = "F";
    const frameInputCss = `${topInputCss({ width: "44px", fontSize: `${TYPE.t10}px`, padding: "2px 4px", textAlign: "left" })}min-width:0;`;
    widget._playheadFrameInput = makeTextInput(
        "Playhead frame",
        frameInputCss,
        (value) => {
            const parsed = widget._parsePositionInput(value);
            if (!Number.isFinite(parsed)) {
                widget._refreshPlayheadInput({ force: true });
                return;
            }
            const maxFrame = Math.max(0, widget.activeScene?.duration_frames || widget.totalFrames);
            widget.playhead = Math.max(0, Math.min(maxFrame, Math.round(parsed)));
            if (widget.isPlaying) widget._stopPlayback();
            widget._ensureFrameVisible(widget.playhead, { center: true });
            widget._renderTimeline();
            widget._renderViewportFrame();
            widget._updateToolbar();
            widget._refreshPlayheadInput({ force: true });
        },
        () => widget._refreshPlayheadInput()
    );

    const inLabel = document.createElement("span");
    inLabel.style.cssText = labelCss({ marginLeft: "2px", fontSize: "9px" });
    inLabel.textContent = "In";
    const outLabel = document.createElement("span");
    outLabel.style.cssText = labelCss({ fontSize: "9px" });
    outLabel.textContent = "Out";
    const selectionInputCss = `${topInputCss({ width: "44px", fontSize: `${TYPE.t10}px`, padding: "2px 4px", textAlign: "left" })}min-width:0;`;
    widget._selectionStartInput = makeTextInput(
        "Selection in-point",
        selectionInputCss,
        (value) => {
            const frame = widget._parsePositionInput(value);
            if (Number.isFinite(frame)) {
                const maxFrame = Math.max(0, widget.activeScene?.duration_frames || widget.totalFrames);
                widget._setSelectionStartFrame(widget._snapSelectionFrame(frame, { direction: "up", clampMax: maxFrame }));
                widget._refreshSelectionInputs({ force: true });
            } else {
                widget._refreshSelectionInputs({ force: true });
            }
        },
        () => widget._refreshSelectionInputs()
    );
    widget._selectionEndInput = makeTextInput(
        "Selection out-point",
        selectionInputCss,
        (value) => {
            const frame = widget._parsePositionInput(value);
            if (Number.isFinite(frame)) {
                const maxFrame = Math.max(0, widget.activeScene?.duration_frames || widget.totalFrames);
                widget._setSelectionEndFrame(widget._snapSelectionFrame(frame, { direction: "up", clampMax: maxFrame }));
                widget._refreshSelectionInputs({ force: true });
            } else {
                widget._refreshSelectionInputs({ force: true });
            }
        },
        () => widget._refreshSelectionInputs()
    );

    const clearSelBtn = makeToolButton("\u2715", "X", "Clear selection", () => widget._clearTimelineSelection());
    clearSelBtn.style.padding = "2px 5px";
    clearSelBtn.style.fontSize = "9px";

    const fitBtn = makeToolButton("\u229e Fit", "F", "Fit timeline to view", () => widget._fitToView());
    widget._toolBtnTimecode = makeToolButton("TC", "T", "Toggle timecode/frame display", () => widget._toggleTimecodeMode());

    const undoBtn = makeButton("\u21a9", "Undo (Ctrl+Z)", BUTTON_OPTIONS.icon);
    undoBtn.style.fontSize = `${TYPE.t14}px`;
    undoBtn.addEventListener("click", () => widget._undo());

    const redoBtn = makeButton("\u21aa", "Redo (Ctrl+Y)", BUTTON_OPTIONS.icon);
    redoBtn.style.fontSize = `${TYPE.t14}px`;
    redoBtn.addEventListener("click", () => widget._redo());

    const helpBtn = makeButton("?", "Keyboard Shortcuts", { ...BUTTON_OPTIONS.tertiary, radius: "999px" });
    helpBtn.addEventListener("click", () => widget._showShortcutOverlay());

    const settingsBtn = makeButton("\u2699", "Editor Settings", { ...BUTTON_OPTIONS.tertiary, radius: "999px", fontSize: `${TYPE.t12}px` }, "margin-left:4px;");
    settingsBtn.addEventListener("click", () => widget._showSettingsPanel());

    widget._bookmarkBtn = makeButton("", "Saved Selections", BUTTON_OPTIONS.tertiary, "position:relative; display:inline-flex; align-items:center; justify-content:center;");
    // Simple outline bookmark (Feather "bookmark"); stroke:currentColor inherits the
    // tertiary button color + hover, replacing the filled white/red \ud83d\udd16 emoji.
    widget._bookmarkBtn.innerHTML = `<svg width="11" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" style="display:block;"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>`;
    widget._bookmarkBtn.addEventListener("click", (event) => widget._toggleSavedSelectionsDropdown(event));

    widget._queueBtn = makeButton("+ Queue", "Add current selection to render queue", BUTTON_OPTIONS.primary, "white-space:nowrap;");
    widget._queueBtn.addEventListener("click", () => widget._addToRenderQueue());

    widget._batchQueueBtn = makeButton("+ Batch (1)", "Add the current selection to the render queue as chunked jobs", BUTTON_OPTIONS.primary, "white-space:nowrap;");
    widget._batchQueueBtn.addEventListener("click", () => widget._addBatchToRenderQueue());

    widget._queueStatusWrap = document.createElement("div");
    widget._queueStatusWrap.style.cssText = `
        display: flex;
        align-items: center;
        gap: 4px;
        margin-left: 4px;
        min-width: 0;
        white-space: nowrap;
    `;
    widget._queueStatusWrap.title = "Queue status";

    widget._foregroundPill = makeForegroundPill();

    widget._exportBtn = makeButton("Export", "Export the current scene timeline", BUTTON_OPTIONS.primary, "white-space:nowrap;");
    widget._exportBtn.addEventListener("click", () => widget._showExportPanel());

    widget._toolBtnAnimatic = makeToolButton("\ud83d\udc41 Anim", "A", "Toggle animatic mode (hide all video)", () => {
        widget._toggleAnimatic();
    });

    const zoomOut = makeButton("\u2212", "Zoom out [-]", BUTTON_OPTIONS.icon);
    zoomOut.style.fontSize = `${TYPE.t14}px`;
    zoomOut.addEventListener("click", () => widget._zoom(-1));

    const zoomIn = makeButton("+", "Zoom in [+]", BUTTON_OPTIONS.icon);
    zoomIn.style.fontSize = `${TYPE.t14}px`;
    zoomIn.addEventListener("click", () => widget._zoom(1));

    // \u2500\u2500 Generation-window block: the one contained/tinted group \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    const navSelDivider = document.createElement("span");
    navSelDivider.style.cssText = `width:1px; align-self:stretch; background:${THEME.line2}; margin:0 2px;`;
    const genLabel = document.createElement("span");
    genLabel.style.cssText = `font-size:9px; font-weight:${TYPE.fwBold}; letter-spacing:0.09em; text-transform:uppercase; color:${THEME.accentHi}; margin-right:3px; white-space:nowrap;`;
    genLabel.textContent = "Gen";
    widget._genReadout = document.createElement("span");
    widget._genReadout.style.cssText = `font-size:9px; color:${THEME.accentHi}; white-space:nowrap; border-left:1px solid ${THEME.accentLo}; padding-left:7px; margin-left:3px; min-width:64px; box-sizing:border-box;`;
    widget._genWindowGroup = document.createElement("div");
    widget._genWindowGroup.style.cssText = `display:flex; align-items:center; gap:4px 5px; flex-wrap:wrap; min-width:0; max-width:100%; flex:0 1 auto; box-sizing:border-box; border:1px solid ${THEME.accentLo}; border-radius:7px; padding:3px 9px;`;
    widget._genWindowGroup.append(
        genLabel,
        makeInlineGroup(frameLabel, widget._playheadFrameInput),
        navSelDivider,
        makeInlineGroup(inLabel, widget._selectionStartInput, makeStepButtons("Selection in-point", (direction) => widget._stepSelectionInput("start", direction))),
        makeInlineGroup(outLabel, widget._selectionEndInput, makeStepButtons("Selection out-point", (direction) => widget._stepSelectionInput("end", direction))),
        clearSelBtn, widget._bookmarkBtn,
        widget._ctxMaskGroup, widget._genReadout
    );

    // Four side-by-side flex groups (the toolbar container is nowrap, so they never stack):
    // history, generation/timeline tools, scene geometry, and utilities. Each group wraps its
    // OWN items upward (flex-wrap:wrap-reverse) — keeping undo/redo and help/settings in their
    // own groups means the big tools group wraps above ITSELF (above the Generation Window)
    // rather than above the undo icons. margin-left:auto on the geometry group pins it +
    // utilities to the right edge; the spare width lands between the tools and geometry groups.
    const makeToolbarGroup = (justify, fixed = false) => {
        const group = document.createElement("div");
        // `fixed` groups (history, utilities) never shrink or wrap — their two buttons stay on one
        // line, bottom-aligned with the primary row. Tools/geometry shrink and wrap upward as needed.
        group.style.cssText = `display:flex; align-items:center; gap:3px; row-gap:4px; flex-wrap:${fixed ? "nowrap" : "wrap-reverse"}; min-width:0; flex:${fixed ? "0 0 auto" : "0 1 auto"}; justify-content:${justify};`;
        return group;
    };
    // Full-height divider that marks a TOP-LEVEL group boundary (align-self:stretch spans every
    // wrapped row), vs the 16px makeDivider() ticks used between sub-clusters inside a group.
    const makeGroupDivider = () => {
        const d = document.createElement("span");
        d.style.cssText = `width:1px; align-self:stretch; background:${THEME.line2}; margin:0 4px; flex:0 0 auto;`;
        return d;
    };
    // Atomic sub-cluster of related tool buttons inside toolbarToolsGroup. flex:0 0 auto +
    // nowrap makes the whole cluster a single flex item, so a wrap moves the related group up
    // together instead of orphaning one button. The leading makeDivider() travels with the
    // cluster, so the separator wraps alongside its group rather than stranding at a line edge.
    const makeToolCluster = (...children) => {
        const cluster = document.createElement("div");
        cluster.style.cssText = "display:flex; align-items:center; gap:3px; flex-wrap:nowrap; flex:0 0 auto;";
        cluster.append(makeDivider(), ...children);
        return cluster;
    };

    const toolbarHistoryGroup = makeToolbarGroup("flex-start", true);
    toolbarHistoryGroup.append(undoBtn, redoBtn);

    const toolbarToolsGroup = makeToolbarGroup("flex-start");
    toolbarToolsGroup.style.flex = "0 1.25 auto";
    toolbarToolsGroup.append(
        widget._genWindowGroup,
        makeToolCluster(widget._toolBtnSnap, widget._toolBtnRazor, cutHereBtn),
        makeToolCluster(fitBtn, widget._toolBtnTimecode, widget._toolBtnAnimatic, zoomOut, zoomIn),
        makeToolCluster(widget._queueBtn, widget._batchQueueBtn, widget._queueStatusWrap, widget._foregroundPill, widget._exportBtn)
    );

    // Scene geometry is its own group, pushed to the right edge (with utilities) via margin-left:auto.
    widget._sceneGeometryGroup.style.marginLeft = "auto";

    const toolbarUtilsGroup = makeToolbarGroup("flex-end", true);
    toolbarUtilsGroup.append(helpBtn, settingsBtn);

    // Group boundaries: history │ tools  …gap…  geometry │ utilities. The tools↔geometry boundary
    // is the margin-left:auto gap (a stronger separator than a line), so no divider is placed there.
    toolbar.append(
        toolbarHistoryGroup, makeGroupDivider(),
        toolbarToolsGroup,
        widget._sceneGeometryGroup, makeGroupDivider(),
        toolbarUtilsGroup
    );

    widget._toolbar = toolbar;
    // Wrapper so UI scaling (transform:scale) can reserve real layout height via the
    // wrapper's explicit height — pushing the timeline down instead of overlapping it
    // (transform/zoom are visual-only and reserve no flow space on their own).
    widget._toolbarWrap = document.createElement("div");
    // overflow MUST stay visible: at toolbar scale < 100% the scaled toolbar gets
    // width:100/st% (wider than the wrap), which makes overflow-x:auto spawn a spurious
    // horizontal scrollbar that clips the bottom row under overflow-y:hidden. The
    // container clips the blast radius, and flex-wrap (not scroll) handles genuine width.
    widget._toolbarWrap.style.cssText = "width:100%; overflow:visible; box-sizing:border-box; flex-shrink:0;";
    widget._toolbarWrap.appendChild(toolbar);
    widget.container.appendChild(widget._toolbarWrap);
    wireForegroundPill(widget);
    updateEditorToolbar(widget);
    return toolbar;
}

export function queueChromeBadges(widget, queue = widget._renderQueue) {
    return buildQueueBadges(widget, queue);
}

export function updateQueueChromeStatus(widget) {
    if (!widget._queueStatusWrap) return;
    widget._queueStatusWrap.innerHTML = "";

    const label = document.createElement("span");
    label.textContent = "Queue";
    label.style.cssText = `
        color: ${THEME.fg2};
        font-size: ${TYPE.t10}px;
        font-weight: ${TYPE.fwBold};
        letter-spacing: 0.02em;
    `;
    widget._queueStatusWrap.appendChild(label);

    const { badges, counts } = buildQueueBadges(widget);
    for (const badge of badges) {
        widget._queueStatusWrap.appendChild(makeStatusPill(badge));
    }

    const titleParts = [];
    if (counts.running > 0) titleParts.push(`${counts.running} running`);
    if (counts.pending > 0) titleParts.push(`${counts.pending} pending`);
    if (widget.renderQueueActive === false) {
        widget._queueStatusWrap.title = titleParts.length
            ? `Queue inactive: ${titleParts.join(", ")}`
            : "Queue inactive";
    } else {
        widget._queueStatusWrap.title = titleParts.length ? `Queue: ${titleParts.join(", ")}` : "Queue: idle";
    }
}

export function updateEditorToolbar(widget) {
    if (!widget._toolBtnSnap) return;

    applyTopButtonVariant(widget._toolBtnSnap, widget.snappingEnabled ? "primary" : "secondary", BUTTON_OPTIONS.secondary, "white-space:nowrap;");
    applyTopButtonVariant(widget._toolBtnRazor, widget._razorMode ? "primary" : "secondary", BUTTON_OPTIONS.secondary, "white-space:nowrap;");
    if (widget._toolBtnTimecode) {
        applyTopButtonVariant(widget._toolBtnTimecode, widget._timecodeMode === "timecode" ? "primary" : "secondary", BUTTON_OPTIONS.secondary, "white-space:nowrap;");
    }

    widget._refreshSelectionInputs();

    if (widget._toolBtnAnimatic) {
        applyTopButtonVariant(widget._toolBtnAnimatic, widget._animaticMode ? "primary" : "secondary", BUTTON_OPTIONS.secondary, "white-space:nowrap;");
    }
    widget._updateBatchButtonLabel();
    updateQueueChromeStatus(widget);
}
