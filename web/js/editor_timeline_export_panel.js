/**
 * Sonder Editor — Timeline Export Panel.
 *
 * Extracted Phase 3 seam: owns the fullscreen "Export Timeline" modal DOM,
 * its local control wiring, custom-encode option UI, and overlay keyboard
 * registration. It does NOT own the export job lifecycle.
 *
 * Host contract (the editor widget instance passed to `mountTimelineExportPanel`):
 *   Reads (live, at event time):
 *     - host.activeScene            (scene_id, duration_frames)
 *     - host._exportStartPending    (true between Export click and job-id resolution)
 *     - host._exportJobId           (set once the backend returns a job id)
 *   Calls:
 *     - host._resolveQueueSelectionRange()
 *     - host._exportSettings()
 *     - host._defaultCustomExportOptions()
 *     - host._sceneHasAudio()
 *     - host._updateExportSettings(partial)
 *     - host._keyboardConsumerId(suffix)
 *     - host._startTimelineExport(payload, ui)
 *     - host._cancelTimelineExport(progressEl)
 *     - host._hideExportPanel()
 *   Writes:
 *     - host._exportPanelSeq        (advanced to mint this mount's unique token,
 *                                    which the host adopts from the handle)
 *
 * Job execution, polling, cancellation, completion refresh, asset reveal, and
 * teardown bookkeeping all stay on the host. The returned handle exposes the
 * DOM root, the `ui` ref bundle the host lifecycle methods mutate, the keyboard
 * unregister fn, and a DOM-only `cleanup()`.
 */

import {
    EDITOR_COLORS as COLORS,
    FONT,
    lightenColor,
} from "./editor_theme.js";
import {
    register as registerKeyboardConsumer,
    PRIORITY as KEY_PRIORITY,
} from "./keyboard_ownership.js";
import {
    CUSTOM_AUDIO_CODEC_OPTIONS,
    CUSTOM_CONTAINER_OPTIONS,
    CUSTOM_ENCODER_PRESET_OPTIONS,
    CUSTOM_OUTPUT_KIND_VIDEO,
    CUSTOM_PIX_FMT_OPTIONS,
    CUSTOM_VIDEO_CODEC_OPTIONS,
    DEFAULT_SAVE_PRESET,
    SAVE_PRESET_OPTIONS,
} from "./editor_settings.js";

// ── Chrome helpers (kept seam-local and identical to editor_widget.js to
// avoid any visual/layout drift; see editor_settings_panel.js for the same
// copy-not-share precedent). ────────────────────────────────────────────────
const BUTTON_VARIANTS = {
    muted: {
        background: COLORS.sceneBtn,
        hoverBackground: COLORS.sceneBtnHover,
        border: COLORS.borderStrong,
        text: COLORS.textDim,
    },
    subtle: {
        background: COLORS.panelRaised,
        hoverBackground: COLORS.panelRaisedHover,
        border: COLORS.border,
        text: COLORS.textDim,
    },
    primary: {
        background: COLORS.accentSoft,
        hoverBackground: COLORS.accentSoftHover,
        border: COLORS.accentBorder,
        text: "#f7fbff",
    },
    active: {
        background: COLORS.sceneBtnActive,
        hoverBackground: lightenColor(COLORS.sceneBtnActive, 0.08),
        border: lightenColor(COLORS.sceneBtnActive, 0.2),
        text: "#ffffff",
    },
    warning: {
        background: COLORS.warningSoft,
        hoverBackground: lightenColor(COLORS.warningSoft, 0.08),
        border: COLORS.warningBorder,
        text: COLORS.warningText,
    },
    danger: {
        background: COLORS.dangerSoft,
        hoverBackground: lightenColor(COLORS.dangerSoft, 0.08),
        border: COLORS.dangerBorder,
        text: COLORS.dangerText,
    },
};

function buttonPalette(variant = "muted") {
    return BUTTON_VARIANTS[variant] || BUTTON_VARIANTS.muted;
}

function chromeButtonCss({
    variant = "muted",
    padding = "2px 8px",
    fontSize = "12px",
    radius = "6px",
    lineHeight = "1.4",
    fontWeight = "600",
} = {}) {
    const palette = buttonPalette(variant);
    return `
        background: ${palette.background};
        border: 1px solid ${palette.border};
        color: ${palette.text};
        padding: ${padding};
        cursor: pointer;
        border-radius: ${radius};
        font-size: ${fontSize};
        line-height: ${lineHeight};
        font-weight: ${fontWeight};
        transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
    `;
}

function setButtonVariant(btn, variant = "muted", { persist = true } = {}) {
    if (!btn) return;
    const palette = buttonPalette(variant);
    btn.style.background = palette.background;
    btn.style.borderColor = palette.border;
    btn.style.color = palette.text;
    if (persist) {
        btn.dataset.sonderBaseVariant = variant;
    }
}

function chromeInputCss({ width = "auto", fontSize = "11px", padding = "2px 4px", textAlign = "center" } = {}) {
    return `
        width: ${width};
        background: ${COLORS.panelRaised};
        border: 1px solid ${COLORS.borderStrong};
        color: ${COLORS.text};
        padding: ${padding};
        font-size: ${fontSize};
        border-radius: 6px;
        text-align: ${textAlign};
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    `;
}

function chromeOverlayPanelCss({ width = "90%", maxWidth = "520px", maxHeight = "80vh", padding = "20px 28px", fontFamily = FONT.sans } = {}) {
    return `
        background: ${COLORS.panel};
        border: 1px solid ${COLORS.borderStrong};
        border-radius: 12px;
        padding: ${padding};
        width: ${width};
        max-width: ${maxWidth};
        max-height: ${maxHeight};
        overflow-y: auto;
        color: ${COLORS.text};
        font-family: ${fontFamily};
        font-size: 12px;
        box-shadow: 0 24px 60px rgba(0,0,0,0.46);
    `;
}

/**
 * Build and mount the export modal for `host`. Returns a handle, or null when
 * the scene has no resolvable range (host wrapper treats null as a no-op).
 */
export function mountTimelineExportPanel(host) {
    if (!host) {
        throw new Error("mountTimelineExportPanel requires a host.");
    }
    return buildExportPanel(host);
}

function buildExportPanel(host) {
    const rangeInfo = host._resolveQueueSelectionRange();
    if (!rangeInfo) return null;

    const settings = host._exportSettings();
    const customState = host._defaultCustomExportOptions();
    let sourceMode = rangeInfo.hasSelection ? "selection" : "scene";
    const customDescriptions = {
        custom_container: "Output file wrapper. MP4 is safest for browser playback, MOV suits ProRes/PCM handoff, and MKV suits FFV1/FLAC archival exports.",
        custom_video_codec: "Video encoder. H.264 is most compatible, H.265 is smaller but slower, ProRes is editing-friendly, and FFV1 is lossless archival.",
        custom_pix_fmt: "Pixel format and chroma layout. yuv420p previews broadly, yuv444p keeps more chroma detail, yuv422p10le suits ProRes, and gbrp preserves RGB for FFV1.",
        custom_encoder_preset: "Speed/compression tradeoff for encoders that support presets. Slower presets usually make smaller files but take longer.",
        custom_audio_codec: "Audio stream codec. AAC previews broadly, PCM is editing-friendly in MOV, FLAC is lossless in MKV, and none omits audio.",
        custom_crf: "Quality target for CRF encoders. Lower is higher quality and larger; higher is smaller and more compressed.",
        custom_audio_bitrate_kbps: "AAC bitrate in kilobits per second when AAC audio is selected.",
    };
    const customOptionDescriptions = {
        custom_container: {
            mp4: "Browser-friendly container for H.264/H.265 and AAC.",
            mov: "Editing handoff container, especially for ProRes or PCM audio.",
            mkv: "Flexible archival container, useful for FFV1 and FLAC.",
        },
        custom_video_codec: {
            libx264: "H.264 encoder with the broadest preview and sharing compatibility.",
            libx265: "H.265 encoder for smaller files, with slower encoding and weaker compatibility.",
            prores_ks: "ProRes encoder for editing handoff; use MOV and yuv422p10le.",
            ffv1: "Lossless FFV1 encoder for archival or diagnostic files; use MKV and gbrp.",
        },
        custom_pix_fmt: {
            yuv420p: "Most compatible 8-bit 4:2:0 format for browser playback.",
            yuv444p: "8-bit 4:4:4 format with more chroma detail; preview support varies.",
            yuv422p10le: "10-bit 4:2:2 format used by ProRes handoff files.",
            gbrp: "Planar RGB format used for lossless FFV1 exports.",
        },
        custom_encoder_preset: Object.fromEntries(CUSTOM_ENCODER_PRESET_OPTIONS.map((value) => [
            value,
            `${value}: encoder speed/compression preset. Slower usually means smaller output and longer export time.`,
        ])),
        custom_audio_codec: {
            aac: "Compressed audio for MP4/browser playback.",
            pcm_s16le: "Uncompressed 16-bit PCM audio for editing handoff.",
            flac: "Lossless compressed audio, usually paired with MKV.",
            none: "Do not include an audio stream.",
        },
    };

    const backdrop = document.createElement("div");
    backdrop.style.cssText = `
        position: fixed; inset: 0; z-index: 10001;
        background: rgba(7,10,14,0.78);
        display: flex; align-items: center; justify-content: center;
        padding: 24px;
    `;

    const panel = document.createElement("div");
    panel.style.cssText = `${chromeOverlayPanelCss({ width: "min(620px, 96vw)", maxWidth: "620px", maxHeight: "min(760px, 88vh)", padding: "0", fontFamily: "'Segoe UI', Arial, sans-serif" })}`;
    panel.addEventListener("click", (event) => event.stopPropagation());

    const header = document.createElement("div");
    header.style.cssText = `display:flex;align-items:center;justify-content:space-between;gap:14px;padding:16px 20px 12px;border-bottom:1px solid ${COLORS.border};position:sticky;top:0;background:${COLORS.panel};z-index:1;`;
    const title = document.createElement("div");
    title.innerHTML = `<div style="font-size:15px;font-weight:700;color:#fff;">Export Timeline</div>`;
    const closeBtn = document.createElement("button");
    closeBtn.textContent = "Close";
    closeBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
    closeBtn.addEventListener("click", () => host._hideExportPanel());
    header.append(title, closeBtn);
    panel.appendChild(header);

    const body = document.createElement("div");
    body.style.cssText = "padding:16px 20px 18px;display:flex;flex-direction:column;gap:12px;";
    panel.appendChild(body);

    const makeRow = (labelText, description = "") => {
        const row = document.createElement("label");
        row.style.cssText = "display:grid;grid-template-columns:140px minmax(0,1fr);gap:12px;align-items:center;";
        if (description) row.title = description;
        const label = document.createElement("div");
        label.textContent = labelText;
        label.style.cssText = `font-size:11px;color:${COLORS.textMuted};font-weight:700;`;
        if (description) label.title = description;
        const control = document.createElement("div");
        control.style.cssText = "min-width:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;";
        if (description) control.title = description;
        row.append(label, control);
        body.appendChild(row);
        return control;
    };

    const sourceWrap = makeRow("Source", "Export either the entire active scene or the current In/Out selection.");
    const sourceRadioName = `${host._keyboardConsumerId("export-source")}`;
    const sceneRadio = document.createElement("input");
    sceneRadio.type = "radio";
    sceneRadio.name = sourceRadioName;
    sceneRadio.value = "scene";
    sceneRadio.title = "Use the full active scene duration.";
    const sceneLabel = document.createElement("span");
    sceneLabel.textContent = "Full scene";
    sceneLabel.style.cssText = "font-size:11px;color:#dbe3ea;";
    sceneLabel.title = sceneRadio.title;
    const selectionRadio = document.createElement("input");
    selectionRadio.type = "radio";
    selectionRadio.name = sourceRadioName;
    selectionRadio.value = "selection";
    selectionRadio.disabled = !rangeInfo.hasSelection;
    selectionRadio.title = rangeInfo.hasSelection ? "Use the current In/Out selection." : "Set an In/Out selection to export only part of the scene.";
    const selectionLabel = document.createElement("span");
    selectionLabel.textContent = "Active selection";
    selectionLabel.style.cssText = `font-size:11px;color:${rangeInfo.hasSelection ? "#dbe3ea" : COLORS.textMuted};`;
    selectionLabel.title = selectionRadio.title;
    sceneRadio.checked = sourceMode === "scene";
    selectionRadio.checked = sourceMode === "selection";
    sourceWrap.append(sceneRadio, sceneLabel, selectionRadio, selectionLabel);

    const prefixInput = document.createElement("input");
    prefixInput.type = "text";
    prefixInput.value = settings.filenamePrefix || "";
    prefixInput.placeholder = "export";
    prefixInput.maxLength = 64;
    prefixInput.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "6px 8px", textAlign: "left" })} width:100%;`;
    prefixInput.title = "Filename stem used before the scene name and timestamp.";
    makeRow("Filename prefix", prefixInput.title).appendChild(prefixInput);

    const presetSelect = document.createElement("select");
    presetSelect.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "6px 8px", textAlign: "left" })} min-width:190px;`;
    for (const option of SAVE_PRESET_OPTIONS) {
        const opt = document.createElement("option");
        opt.value = option.value;
        opt.textContent = option.label;
        opt.title = option.description || "";
        presetSelect.appendChild(opt);
    }
    presetSelect.value = SAVE_PRESET_OPTIONS.some((option) => option.value === settings.lastPreset)
        ? settings.lastPreset
        : DEFAULT_SAVE_PRESET;
    const presetWrap = makeRow("Save preset", "Choose the output container, codecs, pixel format, and compatibility target.");
    presetWrap.style.flexDirection = "column";
    presetWrap.style.alignItems = "stretch";
    presetWrap.appendChild(presetSelect);
    const presetHelp = document.createElement("div");
    presetHelp.style.cssText = `font-size:10px;color:${COLORS.textMuted};line-height:1.35;`;
    presetWrap.appendChild(presetHelp);
    const syncPresetDescription = () => {
        const option = SAVE_PRESET_OPTIONS.find((entry) => entry.value === presetSelect.value);
        const description = option?.description || "Custom allowlisted encode settings.";
        presetHelp.textContent = description;
        presetHelp.title = description;
        presetSelect.title = description;
    };

    const customPanel = document.createElement("div");
    customPanel.style.cssText = `display:none;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px;border:1px solid ${COLORS.borderSoft};border-radius:8px;background:${COLORS.panelMuted};`;
    body.appendChild(customPanel);

    const customControls = [];
    const makeCustomSelect = (key, labelText, options) => {
        const wrap = document.createElement("label");
        wrap.style.cssText = "display:flex;flex-direction:column;gap:4px;min-width:0;";
        wrap.title = customDescriptions[key] || "";
        const label = document.createElement("span");
        label.textContent = labelText;
        label.style.cssText = `font-size:10px;color:${COLORS.textMuted};`;
        label.title = wrap.title;
        const select = document.createElement("select");
        select.style.cssText = chromeInputCss({ fontSize: "10px", padding: "5px 7px", textAlign: "left" });
        for (const value of options) {
            const opt = document.createElement("option");
            opt.value = value;
            opt.textContent = value;
            opt.title = customOptionDescriptions[key]?.[value] || wrap.title;
            select.appendChild(opt);
        }
        select.value = customState[key];
        const syncTitle = () => {
            select.title = customOptionDescriptions[key]?.[select.value] || wrap.title;
        };
        select.addEventListener("change", () => {
            customState[key] = select.value;
            syncTitle();
        });
        wrap.append(label, select);
        customPanel.appendChild(wrap);
        customControls.push(select);
        syncTitle();
        return select;
    };
    const makeCustomNumber = (key, labelText, min, max) => {
        const wrap = document.createElement("label");
        wrap.style.cssText = "display:flex;flex-direction:column;gap:4px;min-width:0;";
        wrap.title = customDescriptions[key] || "";
        const label = document.createElement("span");
        label.textContent = labelText;
        label.style.cssText = `font-size:10px;color:${COLORS.textMuted};`;
        label.title = wrap.title;
        const input = document.createElement("input");
        input.type = "number";
        input.min = String(min);
        input.max = String(max);
        input.step = "1";
        input.value = String(customState[key]);
        input.style.cssText = chromeInputCss({ fontSize: "10px", padding: "5px 7px", textAlign: "right" });
        input.title = wrap.title;
        input.addEventListener("change", () => {
            const numeric = Number(input.value);
            customState[key] = Number.isFinite(numeric) ? Math.max(min, Math.min(max, Math.round(numeric))) : customState[key];
            input.value = String(customState[key]);
        });
        wrap.append(label, input);
        customPanel.appendChild(wrap);
        customControls.push(input);
        return input;
    };
    makeCustomSelect("custom_container", "Container", CUSTOM_CONTAINER_OPTIONS);
    makeCustomSelect("custom_video_codec", "Video codec", CUSTOM_VIDEO_CODEC_OPTIONS);
    makeCustomSelect("custom_pix_fmt", "Pixel format", CUSTOM_PIX_FMT_OPTIONS);
    makeCustomSelect("custom_encoder_preset", "Encoder preset", CUSTOM_ENCODER_PRESET_OPTIONS);
    makeCustomSelect("custom_audio_codec", "Audio codec", CUSTOM_AUDIO_CODEC_OPTIONS);
    makeCustomNumber("custom_crf", "CRF", 0, 51);
    makeCustomNumber("custom_audio_bitrate_kbps", "AAC kbps", 1, 10000);

    const makeCheckboxRow = (labelText, checked, description = "") => {
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !!checked;
        input.title = description;
        makeRow(labelText, description).appendChild(input);
        return input;
    };
    const includeVideo = makeCheckboxRow("Include video", settings.includeVideo !== false, "Encode the visible timeline video layers into the export.");
    const includeAudio = makeCheckboxRow("Include audio", settings.includeAudio !== false && host._sceneHasAudio(), "Mix unmuted, visible audio lanes into the export when the scene has audio.");
    const placeAsTake = makeCheckboxRow("Place as take", settings.placeAsTake !== false, "After export, place the video result onto a fresh timeline lane in the active scene.");
    const linkedTakePlacement = makeCheckboxRow("Link take video + audio", host._settings?.render?.linkedTakePlacement !== false, "When an exported take includes embedded audio, link the placed video and audio items.");
    const takePlacementMuted = makeCheckboxRow("New take starts muted", !!host._settings?.render?.takePlacementMuted, "Place exported takes muted so they do not affect the active composite until enabled.");

    const errorEl = document.createElement("div");
    errorEl.style.cssText = `min-height:16px;font-size:11px;color:${COLORS.warningText};`;
    body.appendChild(errorEl);
    const progressEl = document.createElement("div");
    progressEl.style.cssText = `display:none;font-size:11px;color:${COLORS.textMuted};padding:8px 10px;border:1px solid ${COLORS.borderSoft};border-radius:8px;background:${COLORS.panelMuted};`;
    body.appendChild(progressEl);

    const footer = document.createElement("div");
    footer.style.cssText = `display:flex;justify-content:flex-end;gap:8px;padding:12px 20px 16px;border-top:1px solid ${COLORS.border};`;
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    cancelBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
    cancelBtn.onclick = () => host._hideExportPanel();
    const exportBtn = document.createElement("button");
    exportBtn.textContent = "Export";
    exportBtn.style.cssText = chromeButtonCss({ variant: "primary", padding: "6px 12px", fontSize: "11px", radius: "7px" });
    footer.append(cancelBtn, exportBtn);
    panel.appendChild(footer);

    const controls = [sceneRadio, selectionRadio, prefixInput, presetSelect, includeVideo, includeAudio, placeAsTake, linkedTakePlacement, takePlacementMuted, ...customControls];
    const syncState = () => {
        syncPresetDescription();
        customPanel.style.display = presetSelect.value === "Custom" ? "grid" : "none";
        includeAudio.disabled = !host._sceneHasAudio();
        if (!host._sceneHasAudio()) includeAudio.checked = false;
        placeAsTake.disabled = !includeVideo.checked;
        if (!includeVideo.checked) placeAsTake.checked = false;
        linkedTakePlacement.disabled = !placeAsTake.checked;
        takePlacementMuted.disabled = !placeAsTake.checked;
        const valid = includeVideo.checked || includeAudio.checked;
        errorEl.textContent = valid ? "" : "Enable video or audio to export";
        exportBtn.disabled = !valid;
        setButtonVariant(exportBtn, valid ? "primary" : "muted");
    };
    sceneRadio.addEventListener("change", () => { if (sceneRadio.checked) sourceMode = "scene"; });
    selectionRadio.addEventListener("change", () => { if (selectionRadio.checked) sourceMode = "selection"; });
    presetSelect.addEventListener("change", syncState);
    includeVideo.addEventListener("change", syncState);
    includeAudio.addEventListener("change", syncState);
    placeAsTake.addEventListener("change", syncState);
    syncState();

    const ui = { errorEl, progressEl, controls, exportBtn, closeBtn, cancelBtn, syncState };

    exportBtn.addEventListener("click", async () => {
        const sceneDuration = Math.max(0, parseInt(host.activeScene?.duration_frames, 10) || 0);
        const useSelection = sourceMode === "selection" && rangeInfo.hasSelection;
        const start = useSelection ? rangeInfo.selStart : 0;
        const end = useSelection ? rangeInfo.selEnd : sceneDuration;
        const customOptions = presetSelect.value === "Custom"
            ? { ...customState, custom_output_kind: CUSTOM_OUTPUT_KIND_VIDEO }
            : null;
        host._updateExportSettings({
            lastPreset: presetSelect.value,
            lastCustomEncode: customOptions,
            filenamePrefix: prefixInput.value,
            includeVideo: includeVideo.checked,
            includeAudio: includeAudio.checked,
            placeAsTake: placeAsTake.checked,
        });
        host._updateSettings({
            render: {
                linkedTakePlacement: linkedTakePlacement.checked,
                takePlacementMuted: takePlacementMuted.checked,
            },
        });
        controls.forEach((control) => { control.disabled = true; });
        exportBtn.disabled = true;
        closeBtn.disabled = true;
        progressEl.style.display = "";
        progressEl.textContent = "Starting export...";
        errorEl.textContent = "";
        cancelBtn.textContent = "Cancel Export";
        cancelBtn.onclick = () => host._cancelTimelineExport(progressEl);
        await host._startTimelineExport({
            scene_id: host.activeScene.scene_id,
            range: { start, end },
            filename_prefix: prefixInput.value,
            save_preset: presetSelect.value,
            custom_options: customOptions,
            include_video: includeVideo.checked,
            include_audio: includeAudio.checked,
            place_as_take: placeAsTake.checked,
            take_placement_linked: linkedTakePlacement.checked,
            take_placement_muted: takePlacementMuted.checked,
            take_fit_mode: host._defaultFitMode(),
            take_crop_position: host._defaultCropPosition(),
        }, ui);
    });

    backdrop.appendChild(panel);
    backdrop.addEventListener("click", () => {
        if (host._exportStartPending || host._exportJobId) {
            progressEl.textContent = "Export running. Use Cancel Export to stop it.";
            return;
        }
        host._hideExportPanel();
    });
    document.body.appendChild(backdrop);

    const keyOff = registerKeyboardConsumer({
        id: host._keyboardConsumerId("export"),
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (event.key !== "Escape") return false;
            if (host._exportStartPending || host._exportJobId) {
                progressEl.textContent = "Export running. Use Cancel Export to stop it.";
                return true;
            }
            host._hideExportPanel();
            return true;
        },
    });

    // Advance the seq counter to mint a unique mount token; the host (lifecycle
    // owner) adopts it from the returned handle and is the sole writer of
    // host._exportPanelToken.
    const token = (host._exportPanelSeq = (host._exportPanelSeq || 0) + 1);

    let cleaned = false;
    const cleanup = () => {
        if (cleaned) return;
        cleaned = true;
        if (backdrop.parentNode) backdrop.remove();
    };

    return {
        element: backdrop,
        ui,
        unregisterKeyboard: keyOff,
        cleanup,
        isMounted: () => !cleaned && !!backdrop.parentNode,
        token,
    };
}
