import { ASPECT_RATIO_PRESETS, snapToConstraint } from "./editor_settings.js";
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

export function buildEditorSceneBar(widget, { sceneBarHeight = 36 } = {}) {
    const bar = document.createElement("div");
    bar.style.cssText = `
        display: flex;
        align-items: center;
        gap: 5px;
        padding: 5px 7px;
        background: ${THEME.bg1};
        border-bottom: 1px solid ${THEME.line2};
        min-height: ${sceneBarHeight}px;
        box-sizing: border-box;
    `;

    const prevBtn = makeButton("\u25c0", "Previous scene", BUTTON_OPTIONS.tertiary);
    prevBtn.addEventListener("click", () => widget._cycleScene(-1));

    widget.sceneLabel = document.createElement("span");
    widget.sceneLabel.style.cssText = `
        flex: 1;
        min-width: 0;
        text-align: center;
        font-size: ${TYPE.t12}px;
        font-weight: ${TYPE.fwBold};
        color: ${THEME.fg0};
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        cursor: pointer;
    `;
    widget.sceneLabel.textContent = "No Scene";
    widget.sceneLabel.title = "Double-click to rename - Right-click for options";
    widget.sceneLabel.addEventListener("dblclick", () => widget._renameScene());
    widget.sceneLabel.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!widget.activeScene) return;
        widget._showContextMenu(event.clientX, event.clientY, [
            { label: "Rename Scene", action: () => widget._renameScene() },
            { label: "Duplicate Scene", action: () => widget._duplicateScene() },
            { label: "Delete Scene", action: () => widget._deleteScene(), danger: true },
        ]);
    });

    const nextBtn = makeButton("\u25b6", "Next scene", BUTTON_OPTIONS.tertiary);
    nextBtn.addEventListener("click", () => widget._cycleScene(1));

    const addBtn = makeButton("+", "Create new scene", BUTTON_OPTIONS.primary);
    addBtn.addEventListener("click", () => widget._createScene());

    widget._durLabel = document.createElement("span");
    widget._durLabel.style.cssText = labelCss({ marginLeft: "8px" });
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
    ctxLabel.style.cssText = labelCss({ marginLeft: "6px" });
    ctxLabel.textContent = "Ctx:";

    const ctxInputStyle = topInputCss({ width: "40px", fontSize: `${TYPE.t10}px`, padding: "2px 3px" });
    const preCtxLabel = document.createElement("span");
    preCtxLabel.style.cssText = labelCss({ fontSize: "9px" });
    preCtxLabel.textContent = "Pre";
    widget._preContextInput = document.createElement("input");
    widget._preContextInput.type = "number";
    widget._preContextInput.min = 0;
    widget._preContextInput.max = 256;
    widget._preContextInput.step = 1;
    widget._preContextInput.value = 0;
    widget._preContextInput.title = "Frames to include before the selected generation range";
    widget._preContextInput.style.cssText = ctxInputStyle;
    widget._preContextInput.addEventListener("change", () => widget._updateContextFrameWidgets());

    const postCtxLabel = document.createElement("span");
    postCtxLabel.style.cssText = labelCss({ fontSize: "9px" });
    postCtxLabel.textContent = "Post";
    widget._postContextInput = document.createElement("input");
    widget._postContextInput.type = "number";
    widget._postContextInput.min = 0;
    widget._postContextInput.max = 256;
    widget._postContextInput.step = 1;
    widget._postContextInput.value = 0;
    widget._postContextInput.title = "Frames to include after the selected generation range";
    widget._postContextInput.style.cssText = ctxInputStyle;
    widget._postContextInput.addEventListener("change", () => widget._updateContextFrameWidgets());

    const maskLabel = document.createElement("span");
    maskLabel.style.cssText = labelCss({ marginLeft: "4px" });
    maskLabel.textContent = "Mask Offset:";
    const maskPreLabel = document.createElement("span");
    maskPreLabel.style.cssText = labelCss({ fontSize: "9px" });
    maskPreLabel.textContent = "-";
    widget._maskPreOffsetInput = document.createElement("input");
    widget._maskPreOffsetInput.type = "number";
    widget._maskPreOffsetInput.min = 0;
    widget._maskPreOffsetInput.max = 256;
    widget._maskPreOffsetInput.step = 1;
    widget._maskPreOffsetInput.value = 0;
    widget._maskPreOffsetInput.title = "Extra pre-context frames excluded from denoise mask start";
    widget._maskPreOffsetInput.style.cssText = ctxInputStyle;
    widget._maskPreOffsetInput.addEventListener("change", () => widget._updateContextFrameWidgets());

    const maskPostLabel = document.createElement("span");
    maskPostLabel.style.cssText = labelCss({ fontSize: "9px" });
    maskPostLabel.textContent = "+";
    widget._maskPostOffsetInput = document.createElement("input");
    widget._maskPostOffsetInput.type = "number";
    widget._maskPostOffsetInput.min = 0;
    widget._maskPostOffsetInput.max = 256;
    widget._maskPostOffsetInput.step = 1;
    widget._maskPostOffsetInput.value = 0;
    widget._maskPostOffsetInput.title = "Extra post-context frames included in denoise mask end";
    widget._maskPostOffsetInput.style.cssText = ctxInputStyle;
    widget._maskPostOffsetInput.addEventListener("change", () => widget._updateContextFrameWidgets());

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
    widget._aspectRatioSelect.style.cssText = topSelectCss({ width: "72px", fontSize: "9px", padding: "1px 4px" });
    for (const preset of ASPECT_RATIO_PRESETS) {
        const option = document.createElement("option");
        option.value = widget._aspectRatioOptionValue(preset.a, preset.b);
        option.textContent = preset.label;
        widget._aspectRatioSelect.appendChild(option);
    }
    widget._aspectRatioSelect.addEventListener("change", () => {
        widget._resetFreeAspectTierDraft();
        widget._updateResolutionInputMode();
        widget._recalculateResolution();
    });

    widget._resTierSelect = document.createElement("select");
    widget._resTierSelect.style.cssText = topSelectCss({ width: "92px", fontSize: "9px", padding: "1px 4px" });
    widget._resTierSelect.addEventListener("change", () => {
        widget._resetFreeAspectTierDraft();
        widget._recalculateResolution();
    });

    widget._templateSelect = document.createElement("select");
    widget._templateSelect.style.cssText = topSelectCss({ width: "108px", fontSize: "9px", padding: "1px 4px" });
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
        const value = template.id === "free"
            ? rawValue
            : Number(snapToConstraint(rawValue, template?.constraints?.fps).toFixed(3));
        widget._fpsInput.value = value || "";
        if (widget.activeScene) {
            widget._updateSceneFps(value);
        }
    });

    bar.append(
        prevBtn, widget.sceneLabel, nextBtn, addBtn,
        widget._durLabel, widget.durationInput,
        ctxLabel, preCtxLabel, widget._preContextInput, postCtxLabel, widget._postContextInput,
        maskLabel, maskPreLabel, widget._maskPreOffsetInput, maskPostLabel, widget._maskPostOffsetInput,
        resLabel, widget._resWInput, xLabel, widget._resHInput, widget._aspectRatioSelect, widget._resTierSelect, widget._templateSelect,
        fpsLabel, widget._fpsInput
    );

    widget._sceneBar = bar;
    widget.container.appendChild(bar);
    return bar;
}

export function buildEditorToolbar(widget) {
    const toolbar = document.createElement("div");
    toolbar.style.cssText = `
        display: flex;
        align-items: center;
        gap: 3px;
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
    const frameInputCss = `${topInputCss({ width: "58px", fontSize: `${TYPE.t10}px`, padding: "2px 4px", textAlign: "right" })}min-width:0;`;
    widget._playheadFrameInput = makeTextInput(
        "Playhead frame",
        frameInputCss,
        (value) => {
            const parsed = widget._parsePositionInput(value);
            if (!Number.isFinite(parsed)) {
                widget._refreshPlayheadInput();
                return;
            }
            const maxFrame = Math.max(0, widget.activeScene?.duration_frames || widget.totalFrames);
            widget.playhead = Math.max(0, Math.min(maxFrame, Math.round(parsed)));
            if (widget.isPlaying) widget._stopPlayback();
            widget._renderTimeline();
            widget._renderViewportFrame();
            widget._updateToolbar();
        },
        () => widget._refreshPlayheadInput()
    );

    const inLabel = document.createElement("span");
    inLabel.style.cssText = labelCss({ marginLeft: "2px", fontSize: "9px" });
    inLabel.textContent = "In";
    const outLabel = document.createElement("span");
    outLabel.style.cssText = labelCss({ fontSize: "9px" });
    outLabel.textContent = "Out";
    const selectionInputCss = `${topInputCss({ width: "58px", fontSize: `${TYPE.t10}px`, padding: "2px 4px", textAlign: "right" })}min-width:0;`;
    widget._selectionStartInput = makeTextInput(
        "Selection in-point",
        selectionInputCss,
        (value) => {
            const frame = widget._parsePositionInput(value);
            if (Number.isFinite(frame)) {
                const maxFrame = Math.max(0, widget.activeScene?.duration_frames || widget.totalFrames);
                widget._setSelectionStartFrame(widget._snapSelectionFrame(frame, { direction: "up", clampMax: maxFrame }));
            } else {
                widget._refreshSelectionInputs();
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
            } else {
                widget._refreshSelectionInputs();
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

    const spacer = document.createElement("span");
    spacer.style.flex = "1";

    const helpBtn = makeButton("?", "Keyboard Shortcuts", { ...BUTTON_OPTIONS.tertiary, radius: "999px" });
    helpBtn.addEventListener("click", () => widget._showShortcutOverlay());

    const settingsBtn = makeButton("\u2699", "Editor Settings", { ...BUTTON_OPTIONS.tertiary, radius: "999px", fontSize: `${TYPE.t12}px` }, "margin-left:4px;");
    settingsBtn.addEventListener("click", () => widget._showSettingsPanel());

    widget._bookmarkBtn = makeButton("\ud83d\udd16", "Saved Selections", BUTTON_OPTIONS.tertiary, "position:relative;");
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

    widget._fullscreenBtn = makeButton("\u26f6", "Toggle fullscreen", BUTTON_OPTIONS.icon);
    widget._fullscreenBtn.style.fontSize = `${TYPE.t14}px`;
    widget._fullscreenBtn.addEventListener("click", () => widget._toggleFullscreen());

    toolbar.append(
        undoBtn, redoBtn, makeDivider(), widget._toolBtnSnap, widget._toolBtnRazor, cutHereBtn, makeDivider(),
        frameLabel, widget._playheadFrameInput, inLabel, widget._selectionStartInput, outLabel, widget._selectionEndInput,
        clearSelBtn, widget._bookmarkBtn, makeDivider(), fitBtn, widget._toolBtnTimecode, widget._toolBtnAnimatic,
        widget._queueBtn, widget._batchQueueBtn, widget._queueStatusWrap, widget._exportBtn, spacer,
        zoomOut, zoomIn, widget._fullscreenBtn, helpBtn, settingsBtn
    );

    widget._toolbar = toolbar;
    widget.container.appendChild(toolbar);
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
