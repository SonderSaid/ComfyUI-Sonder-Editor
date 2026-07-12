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
    CLIP_LABEL_MODE_OPTIONS,
    CLIP_LABEL_HORIZONTAL_ALIGN_OPTIONS,
    CLIP_LABEL_VERTICAL_ALIGN_OPTIONS,
    CROP_POSITION_OPTIONS,
    DEFAULT_EDITOR_SETTINGS,
    DEFAULT_SAVE_PRESET,
    FIT_MODE_OPTIONS,
    GALLERY_SORT_OPTIONS,
    GALLERY_THUMBNAIL_SIZE_OPTIONS,
    PLAYBACK_RESOLUTION_OPTIONS,
    SAVE_PRESET_OPTIONS,
    SNAP_TARGET_OPTIONS,
    TAKE_PLACEMENT_MODE_OPTIONS,
    TIMECODE_MODE_OPTIONS,
    describeConstraintFormula,
    getAllModelTemplates,
    getTemplateById,
    getTemplateFpsValues,
    isBuiltinModelTemplate,
    previewConstraintValues,
} from "./editor_settings.js";

const RENDER_CACHE_ENTRY_PRESETS = [
    { value: "0", label: "Off" },
    { value: "1", label: "Keep 1" },
    { value: "3", label: "Keep 3" },
    { value: "5", label: "Keep 5" },
    { value: "10", label: "Keep 10" },
    { value: "25", label: "Keep 25" },
    { value: "unlimited", label: "Unlimited" },
];

const TRASH_SIZE_MB_PRESETS = [
    { value: "250", label: "250 MB" },
    { value: "500", label: "500 MB" },
    { value: "1000", label: "1 GB" },
    { value: "2000", label: "2 GB" },
    { value: "5000", label: "5 GB" },
    { value: "unlimited", label: "Unlimited" },
];

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

export function mountEditorSettingsPanel(host) {
    if (!host) {
        throw new Error("mountEditorSettingsPanel requires a host adapter.");
    }
    if (host._settingsPanelEl) {
        return createHandle(host);
    }
    showSettingsPanel.call(host);
    return createHandle(host);
}

function createHandle(host) {
    return {
        element: host._settingsPanelEl,
        controls: host._settingsPanelControls,
        renderModelTemplateSettings: () => host._renderModelTemplateSettings?.(),
        unregisterKeyboard: host._settingsPanelKeyOff,
        sync: () => syncSettingsPanelControls.call(host),
        cleanup: () => hideSettingsPanel.call(host),
    };
}

function syncSettingsPanelControls() {
    if (!this._settingsPanelControls) return;
    const controls = this._settingsPanelControls;
    if (controls.scaleToolbarLabel) controls.scaleToolbarLabel.textContent = `${Math.round(this._settings.layout.scaleToolbar * 100)}%`;
    if (controls.scaleTrackHeadersLabel) controls.scaleTrackHeadersLabel.textContent = `${Math.round(this._settings.layout.scaleTrackHeaders * 100)}%`;
    if (controls.scaleTimelineLabel) controls.scaleTimelineLabel.textContent = `${Math.round(this._settings.layout.scaleTimeline * 100)}%`;
    if (controls.scaleGalleryLabel) controls.scaleGalleryLabel.textContent = `${Math.round(this._settings.layout.scaleGallery * 100)}%`;
    if (controls.linkedVideoAudioDrop) controls.linkedVideoAudioDrop.checked = this._settings.timelineBehavior.linkedVideoAudioDrop !== false;
    if (controls.snappingEnabled) controls.snappingEnabled.checked = !!this._settings.timelineBehavior.snappingEnabled;
    if (controls.snapThreshold) controls.snapThreshold.value = String(this._settings.timelineBehavior.snapThreshold);
    for (const option of SNAP_TARGET_OPTIONS) {
        const control = controls[`snapTarget_${option.key}`];
        if (control) control.checked = !!this._settings.timelineBehavior.snapTargets[option.key];
    }
    if (controls.timecodeMode) controls.timecodeMode.value = this._settings.timelineBehavior.timecodeMode;
    if (controls.loopSelection) controls.loopSelection.checked = !!this._settings.playback.loopSelection;
    if (controls.autoScrollPlayhead) controls.autoScrollPlayhead.checked = !!this._settings.playback.autoScrollPlayhead;
    if (controls.returnToPlaybackStart) controls.returnToPlaybackStart.checked = !!this._settings.playback.returnToPlaybackStart;
    if (controls.playbackResolution) controls.playbackResolution.value = this._settings.playback.resolution;
    if (controls.prebufferEnabled) controls.prebufferEnabled.checked = !!this._settings.playback.prebufferEnabled;
    if (controls.prebufferLookaheadMs) controls.prebufferLookaheadMs.value = String(this._settings.playback.prebufferLookaheadMs);
    if (controls.prebufferBoundaryDepth) controls.prebufferBoundaryDepth.value = String(this._settings.playback.prebufferBoundaryDepth);
    if (controls.prebufferMaxEntries) controls.prebufferMaxEntries.value = String(this._settings.playback.prebufferMaxEntries);
    if (controls.decodeConcurrency) controls.decodeConcurrency.value = String(this._settings.playback.decodeConcurrency);
    if (controls.waveformAccent) controls.waveformAccent.value = this._settings.appearance.waveformAccent;
    if (controls.timelineBrightness) controls.timelineBrightness.value = String(this._settings.appearance.timelineBrightness);
    if (controls.timelineBrightnessLabel) controls.timelineBrightnessLabel.textContent = `${this._settings.appearance.timelineBrightness}%`;
    if (controls.clipLabelMode) controls.clipLabelMode.value = this._settings.appearance.clipLabelMode;
    if (controls.clipLabelVerticalAlign) controls.clipLabelVerticalAlign.value = this._settings.appearance.clipLabelVerticalAlign;
    if (controls.clipLabelHorizontalAlign) controls.clipLabelHorizontalAlign.value = this._settings.appearance.clipLabelHorizontalAlign;
    if (controls.sceneOutline) controls.sceneOutline.checked = this._settings.appearance.sceneOutline !== false;
    for (const tintKey of ["video", "audio", "motion_driver"]) {
        const tintInput = controls[`laneTintOverride_${tintKey}`];
        if (tintInput) {
            const stored = this._settings.appearance.laneTintOverrides?.[tintKey] || "";
            tintInput.value = stored || "#000000";
            tintInput.dataset.active = stored ? "1" : "0";
        }
    }
    if (controls.editorMarginTop) controls.editorMarginTop.value = String(this._settings.appearance.editorMargins?.top ?? 0);
    if (controls.editorMarginBottom) controls.editorMarginBottom.value = String(this._settings.appearance.editorMargins?.bottom ?? 0);
    if (controls.editorMarginSides) controls.editorMarginSides.value = String(this._settings.appearance.editorMargins?.sides ?? 0);
    if (controls.takePlacementMode) controls.takePlacementMode.value = this._settings.render?.takePlacementMode ?? "trimmed";
    if (controls.linkedTakePlacement) controls.linkedTakePlacement.checked = this._settings.render?.linkedTakePlacement !== false;
    if (controls.takePlacementMuted) controls.takePlacementMuted.checked = !!this._settings.render?.takePlacementMuted;
    if (controls.defaultSavePreset) {
        controls.defaultSavePreset.value = this._settings.render?.defaultSavePreset ?? DEFAULT_SAVE_PRESET;
        controls.defaultSavePreset._sonderSyncTitle?.();
    }
    if (controls.guideSnapshotMaxLongEdge) controls.guideSnapshotMaxLongEdge.value = String(this._settings.guides?.guideSnapshotMaxLongEdge ?? 0);
    if (controls.hoverPreviewEnabled) controls.hoverPreviewEnabled.checked = this._settings.guides?.hoverPreviewEnabled ?? true;
    if (controls.hoverPreviewSize) controls.hoverPreviewSize.value = String(this._guideHoverPreviewSize());
    for (const sync of controls._syncPresetNumberControls || []) {
        sync();
    }
    if (controls.trashRetentionDays) controls.trashRetentionDays.value = String(this._trashRetentionDays());
    if (controls.galleryStickyFolderHeaders) controls.galleryStickyFolderHeaders.checked = this._settings.gallery?.stickyFolderHeaders !== false;
    if (controls.batchRenderMaxFramesPerChunk) controls.batchRenderMaxFramesPerChunk.value = String(this._settings.batchRender.maxFramesPerChunk);
    if (controls.defaultProjectFps) controls.defaultProjectFps.value = String(this._settings.projectDefaults.fps);
    if (controls.defaultProjectWidth) controls.defaultProjectWidth.value = String(this._settings.projectDefaults.width);
    if (controls.defaultProjectHeight) controls.defaultProjectHeight.value = String(this._settings.projectDefaults.height);
    if (controls.defaultSceneDuration) controls.defaultSceneDuration.value = String(this._settings.projectDefaults.newSceneDuration);
    if (controls.defaultGuideStrength) controls.defaultGuideStrength.value = String(this._settings.projectDefaults.defaultGuideStrength);
    if (controls.defaultMotionDriverStrength) controls.defaultMotionDriverStrength.value = String(this._settings.projectDefaults.defaultMotionDriverStrength);
    if (controls.defaultFitMode) controls.defaultFitMode.value = this._settings.projectDefaults.defaultFitMode || DEFAULT_EDITOR_SETTINGS.projectDefaults.defaultFitMode;
    if (controls.defaultCropPosition) controls.defaultCropPosition.value = this._settings.projectDefaults.defaultCropPosition || "center";
    if (controls.defaultTemplateId) controls.defaultTemplateId.value = this._settings.projectDefaults.defaultTemplateId || "free";
    if (controls.gallerySortMode) controls.gallerySortMode.value = this._settings.gallery.sortMode;
    if (controls.galleryInspectorCollapsed) controls.galleryInspectorCollapsed.checked = !!this._settings.gallery.inspectorCollapsed;
    if (controls.galleryThumbnailSize) controls.galleryThumbnailSize.value = this._settings.gallery.thumbnailSize;
    if (controls.galleryArtifactInspectorExpanded) controls.galleryArtifactInspectorExpanded.checked = !!this._settings.gallery.artifactInspectorExpanded;
    if (controls.promptChannelLabels) controls.promptChannelLabels.checked = this._promptChannelLabels === true;
    if (controls.promptSectionDelimiter) controls.promptSectionDelimiter.value = String(this._promptSectionDelimiter ?? ".");
    if (controls.promptFrameThreshold) controls.promptFrameThreshold.value = String(this._promptFrameThreshold ?? 10);
    if (controls.allowExternalProjectLinks) {
        const resolved = this._serverSettingsLoaded === true;
        controls.allowExternalProjectLinks.disabled = !resolved;
        controls.allowExternalProjectLinks.checked = this._serverSettings?.allow_external_project_links === true;
        controls.allowExternalProjectLinks.title = resolved
            ? ""
            : "Loading server settingâ€¦";
    }
    this._renderModelTemplateSettings?.();
}

function showSettingsPanel() {
    if (this._settingsPanelEl) return;

    const backdrop = document.createElement("div");
    backdrop.style.cssText = `
        position: fixed; inset: 0; z-index: 10001;
        background: rgba(7,10,14,0.78);
        display: flex; align-items: center; justify-content: center;
        padding: 24px;
    `;

    const panel = document.createElement("div");
    panel.style.cssText = `${chromeOverlayPanelCss({ width: "min(860px, 96vw)", maxWidth: "860px", maxHeight: "min(780px, 88vh)", padding: "0", fontFamily: "'Segoe UI', Arial, sans-serif" })}`;

    const header = document.createElement("div");
    header.style.cssText = `
        display: flex; align-items: center; justify-content: space-between;
        padding: 18px 22px 14px;
        border-bottom: 1px solid ${COLORS.border};
        position: sticky; top: 0; background: ${COLORS.panel}; z-index: 1;
    `;
    const titleWrap = document.createElement("div");
    titleWrap.innerHTML = `
        <div style="font-size:15px;font-weight:700;color:#fff;">Editor Settings</div>
        <div style="font-size:11px;color:#909090;margin-top:3px;">Most settings are browser-local; project-wide controls are labeled inline.</div>
    `;
    const closeBtn = document.createElement("button");
    closeBtn.textContent = "Close";
    closeBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
    closeBtn.addEventListener("click", () => this._hideSettingsPanel());
    header.append(titleWrap, closeBtn);
    panel.appendChild(header);

    const body = document.createElement("div");
    body.style.cssText = `
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
        gap: 14px;
        padding: 18px 22px 22px;
    `;
    panel.appendChild(body);

    this._settingsPanelControls = {};
    const controls = this._settingsPanelControls;
    controls._syncPresetNumberControls = [];

    const updateCategory = (category, key, value) => {
        this._updateSettings({
            [category]: {
                [key]: value,
            },
        });
    };

    const createSection = (title, description) => {
        const section = document.createElement("section");
        section.style.cssText = `
            background: ${COLORS.panelRaised};
            border: 1px solid ${COLORS.border};
            border-radius: 10px;
            padding: 14px 14px 12px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            align-self: start;
        `;
        const heading = document.createElement("div");
        heading.innerHTML = `
            <div style="font-size:12px;font-weight:700;color:#f0f0f0;letter-spacing:0.02em;">${title}</div>
            <div style="font-size:10px;color:#888;margin-top:2px;line-height:1.35;">${description}</div>
        `;
        section.appendChild(heading);
        body.appendChild(section);
        return section;
    };

    const createRow = (section, label, description) => {
        const row = document.createElement("div");
        row.style.cssText = `
            display: flex; align-items: center; justify-content: space-between;
            gap: 12px; padding: 8px 0; border-top: 1px solid ${COLORS.borderSoft};
        `;
        const labelWrap = document.createElement("div");
        labelWrap.style.cssText = "flex: 1; min-width: 0;";
        labelWrap.innerHTML = `
            <div style="color:#ddd;font-size:11px;line-height:1.35;">${label}</div>
            <div style="color:#777;font-size:10px;line-height:1.35;margin-top:1px;">${description}</div>
        `;
        const controlWrap = document.createElement("div");
        controlWrap.style.cssText = "display:flex;align-items:center;gap:8px;flex-shrink:0;";
        row.append(labelWrap, controlWrap);
        section.appendChild(row);
        return controlWrap;
    };

    const addSectionReset = (section, label, description, applyReset) => {
        const controlWrap = createRow(section, label, description);
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Reset Section";
        button.style.cssText = chromeButtonCss({ variant: "subtle", padding: "5px 12px", fontSize: "11px", radius: "6px" });
        button.addEventListener("click", () => {
            applyReset?.();
            syncSettingsPanelControls.call(this);
        });
        controlWrap.appendChild(button);
        return button;
    };

    const createCheckbox = (section, key, label, description, getter, onChange) => {
        const controlWrap = createRow(section, label, description);
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !!getter();
        checkbox.addEventListener("change", () => onChange(checkbox.checked));
        controlWrap.appendChild(checkbox);
        controls[key] = checkbox;
        return checkbox;
    };

    const createSelect = (section, key, label, description, options, getter, onChange) => {
        const controlWrap = createRow(section, label, description);
        const select = document.createElement("select");
        select.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "4px 8px", textAlign: "left" })} min-width: 126px;`;
        for (const option of options) {
            const el = document.createElement("option");
            el.value = option.value;
            el.textContent = option.label;
            if (option.description) el.title = option.description;
            select.appendChild(el);
        }
        const syncTitle = () => {
            const selected = options.find((option) => option.value === select.value);
            select.title = selected?.description || description || "";
        };
        select.value = getter();
        syncTitle();
        select._sonderSyncTitle = syncTitle;
        select.addEventListener("change", () => {
            syncTitle();
            onChange(select.value);
        });
        controlWrap.appendChild(select);
        controls[key] = select;
        return select;
    };

    const createNumberInput = (section, key, label, description, config) => {
        const controlWrap = createRow(section, label, description);
        const input = document.createElement("input");
        input.type = "number";
        input.min = String(config.min);
        input.max = String(config.max);
        input.step = String(config.step ?? 1);
        input.value = String(config.getter());
        input.style.cssText = `${chromeInputCss({ width: "86px", fontSize: "11px", padding: "4px 8px", textAlign: "right" })}`;
        input.addEventListener("change", () => {
            const numeric = Number(input.value);
            const fallback = config.getter();
            const nextValue = Number.isFinite(numeric) ? numeric : fallback;
            config.onChange(nextValue);
        });
        controlWrap.appendChild(input);
        controls[key] = input;
        return input;
    };

    const createPresetNumberInput = (section, key, label, description, config) => {
        const controlWrap = createRow(section, label, description);
        controlWrap.style.flexDirection = "column";
        controlWrap.style.alignItems = "flex-end";
        const inputRow = document.createElement("div");
        inputRow.style.cssText = "display:flex;align-items:center;gap:8px;";

        const select = document.createElement("select");
        select.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "4px 8px", textAlign: "left" })} min-width: 126px;`;
        for (const option of config.options) {
            const el = document.createElement("option");
            el.value = option.value;
            el.textContent = option.label;
            select.appendChild(el);
        }
        const customOption = document.createElement("option");
        customOption.value = "custom";
        customOption.textContent = "Custom";
        select.appendChild(customOption);

        const input = document.createElement("input");
        input.type = "number";
        input.min = String(config.min);
        input.max = String(config.max);
        input.step = String(config.step ?? 1);
        input.style.cssText = `${chromeInputCss({ width: "92px", fontSize: "11px", padding: "4px 8px", textAlign: "right" })}`;

        const coerce = (value) => {
            if (value === null && config.allowNull) return null;
            const numeric = Number(value);
            if (!Number.isFinite(numeric)) return undefined;
            const clamped = Math.max(config.min, Math.min(config.max, numeric));
            return config.integer ? Math.round(clamped) : clamped;
        };
        const formatValue = (value) => {
            if (value === null || value === undefined) return String(config.customDefault ?? config.min);
            return config.integer ? String(Math.round(value)) : String(value);
        };
        const presetValueFor = (value) => {
            if (value === null && config.allowNull) return "unlimited";
            const normalized = coerce(value);
            if (normalized === undefined) return "custom";
            const preset = config.options.find((option) => {
                if (option.value === "unlimited") return false;
                const optionValue = coerce(option.value);
                return optionValue !== undefined && optionValue === normalized;
            });
            return preset ? preset.value : "custom";
        };
        const currentValue = () => {
            const value = config.getter();
            if (value === null && config.allowNull) return null;
            const normalized = coerce(value);
            return normalized === undefined ? coerce(config.customDefault) : normalized;
        };
        // The persisted number alone cannot distinguish a chosen preset from a
        // user-edited custom number that happens to match a preset.
        let customSelected = presetValueFor(config.getter()) === "custom";
        const sync = () => {
            const value = currentValue();
            const valueDerivedPreset = presetValueFor(value);
            if (valueDerivedPreset === "custom") customSelected = true;
            const selectedPreset = customSelected ? "custom" : valueDerivedPreset;
            select.value = selectedPreset;
            const showCustom = selectedPreset === "custom";
            input.style.display = showCustom ? "" : "none";
            input.value = formatValue(value === null ? coerce(config.customDefault) : value);
        };

        select.addEventListener("change", () => {
            if (select.value === "custom") {
                customSelected = true;
                sync();
                return;
            }
            customSelected = false;
            if (select.value === "unlimited") {
                config.onChange(null);
                sync();
                return;
            }
            const nextValue = coerce(select.value);
            if (nextValue !== undefined) {
                config.onChange(nextValue);
            }
            sync();
        });
        input.addEventListener("change", () => {
            const nextValue = coerce(input.value);
            config.onChange(nextValue === undefined ? (currentValue() ?? coerce(config.customDefault)) : nextValue);
            sync();
        });

        inputRow.append(select, input);
        controlWrap.appendChild(inputRow);
        controls[`${key}Preset`] = select;
        controls[`${key}Custom`] = input;
        controls._syncPresetNumberControls.push(sync);
        sync();
        return { select, input };
    };

    const createScaleRow = (section, key, label, description, getter) => {
        const controlWrap = createRow(section, label, description);
        const downBtn = document.createElement("button");
        downBtn.textContent = "-";
        downBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "3px 9px", fontSize: "13px", radius: "6px", lineHeight: "1" });
        downBtn.addEventListener("click", () => this._setScale(key, getter() - 0.1));

        const valueLabel = document.createElement("span");
        valueLabel.style.cssText = "font-size:11px;color:#cfcfcf;min-width:42px;text-align:center;";
        valueLabel.textContent = `${Math.round(getter() * 100)}%`;
        controls[`scale${key}Label`] = valueLabel;

        const upBtn = document.createElement("button");
        upBtn.textContent = "+";
        upBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "3px 9px", fontSize: "13px", radius: "6px", lineHeight: "1" });
        upBtn.addEventListener("click", () => this._setScale(key, getter() + 0.1));

        controlWrap.append(downBtn, valueLabel, upBtn);
    };

    const setSelectOptions = (select, options, selectedValue) => {
        if (!select) return;
        select.innerHTML = "";
        for (const option of options) {
            const el = document.createElement("option");
            el.value = option.value;
            el.textContent = option.label;
            select.appendChild(el);
        }
        select.value = Array.from(select.options).some((option) => option.value === String(selectedValue))
            ? String(selectedValue)
            : (options[0]?.value ?? "");
    };

    const parseDraftNumber = (value, integer = true) => {
        if (value === "" || value == null) return undefined;
        const numeric = Number(value);
        if (!Number.isFinite(numeric)) return undefined;
        return integer ? Math.round(numeric) : numeric;
    };

    const makeUniqueTemplateId = (name, existingId = "") => {
        const base = String(name || "")
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/^-+|-+$/g, "") || "custom-template";
        const usedIds = new Set(getAllModelTemplates(this._settings).map((template) => template.id).filter((id) => id !== existingId));
        let candidate = base;
        let suffix = 2;
        while (usedIds.has(candidate)) {
            candidate = `${base}-${suffix}`;
            suffix += 1;
        }
        return candidate;
    };

    const templateOptions = () => getAllModelTemplates(this._settings).map((template) => ({
        value: template.id,
        label: template.name,
    }));

    const modelTemplatesSection = createSection(
        "Model Templates",
        "Manage model-specific parameter constraints and choose which template new projects should start with."
    );
    const templateList = document.createElement("div");
    templateList.style.cssText = "display:flex;flex-direction:column;gap:8px;";
    const templateFormHost = document.createElement("div");
    templateFormHost.style.cssText = "display:flex;flex-direction:column;gap:8px;";
    modelTemplatesSection.append(templateList, templateFormHost);

    this._renderModelTemplateSettings = () => {
        const formState = this._templateFormState || { expanded: false, editId: "" };
        const templates = getAllModelTemplates(this._settings);
        const customTemplates = this._settings.modelTemplates.customTemplates || [];
        templateList.innerHTML = "";
        templateFormHost.innerHTML = "";

        if (controls.defaultTemplateId) {
            setSelectOptions(controls.defaultTemplateId, templateOptions(), this._settings.projectDefaults.defaultTemplateId || "free");
        }

        for (const template of templates) {
            const card = document.createElement("div");
            card.style.cssText = `
                border: 1px solid ${COLORS.borderSoft};
                border-radius: 8px;
                padding: 10px 11px;
                display: flex;
                flex-direction: column;
                gap: 7px;
                background: ${COLORS.panelMuted};
            `;

            const head = document.createElement("div");
            head.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:10px;";
            const nameWrap = document.createElement("div");
            const isOverriddenBuiltin = template.builtIn && !!this._settings.modelTemplates?.builtinOverrides?.[template.id];
            const badge = template.builtIn ? (isOverriddenBuiltin ? "Built-in (edited)" : "Built-in") : "Custom";
            const nameEl = document.createElement("div");
            nameEl.style.cssText = `font-size:11px;font-weight:700;color:${COLORS.text};`;
            nameEl.textContent = template.name;
            const badgeEl = document.createElement("div");
            badgeEl.style.cssText = `font-size:10px;color:${COLORS.textMuted};`;
            badgeEl.textContent = `${badge}${template.id === this._templateId ? " • Active Project Template" : ""}`;
            nameWrap.append(nameEl, badgeEl);
            head.appendChild(nameWrap);

            // `free` is the no-op template — no edit/reset. Built-ins are editable
            // (writes a browser-local override) with Reset shown once overridden.
            if (!template.builtIn || template.id !== "free") {
                const actions = document.createElement("div");
                actions.style.cssText = "display:flex;gap:6px;";

                const editBtn = document.createElement("button");
                editBtn.textContent = "Edit";
                editBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "4px 9px", fontSize: "10px", radius: "6px" });
                editBtn.addEventListener("click", () => {
                    this._templateFormState = { expanded: true, editId: template.id };
                    this._renderModelTemplateSettings?.();
                });
                actions.appendChild(editBtn);

                if (!template.builtIn) {
                    const deleteBtn = document.createElement("button");
                    deleteBtn.textContent = "Delete";
                    deleteBtn.style.cssText = chromeButtonCss({ variant: "danger", padding: "4px 9px", fontSize: "10px", radius: "6px" });
                    deleteBtn.addEventListener("click", async () => {
                        this._templateFormState = { expanded: false, editId: "" };
                        await this._deleteCustomModelTemplate(template.id);
                        this._renderModelTemplateSettings?.();
                    });
                    actions.appendChild(deleteBtn);
                } else if (isOverriddenBuiltin) {
                    const resetBtn = document.createElement("button");
                    resetBtn.textContent = "Reset";
                    resetBtn.title = "Reset this built-in template to its default values";
                    resetBtn.style.cssText = chromeButtonCss({ variant: "danger", padding: "4px 9px", fontSize: "10px", radius: "6px" });
                    resetBtn.addEventListener("click", () => {
                        this._templateFormState = { expanded: false, editId: "" };
                        this._updateSettings({ modelTemplates: { builtinOverrides: { [template.id]: null } } });
                    });
                    actions.appendChild(resetBtn);
                }

                head.appendChild(actions);
            }

            const summary = document.createElement("div");
            summary.style.cssText = "font-size:10px;color:#9ca9b5;line-height:1.45;";
            const summaryParts = [];
            if (template.id !== "free") {
                const dimStep = template.hard?.dimensionStep;
                if (dimStep > 0) {
                    summaryParts.push(`÷${dimStep}${template.hard?.evenLatentDimensions ? " (even latent)" : ""}`);
                }
                const frameStep = template.hard?.frameStep;
                if (frameStep > 1) {
                    summaryParts.push(`frames ${describeConstraintFormula({ step: frameStep, offset: template.hard?.frameOffset || 0 })}`);
                }
                const fpsValues = getTemplateFpsValues(template);
                if (fpsValues.length) summaryParts.push(`fps ${fpsValues.join("/")}`);
                const minDim = template.soft?.minDimension;
                if (minDim > 0) summaryParts.push(`min ${minDim}px`);
                const recRes = template.soft?.recommendedRes;
                if (Array.isArray(recRes) && recRes.length) {
                    summaryParts.push(`rec ${recRes.map((pair) => `~${pair[0]}×${pair[1]}`).join(", ")}`);
                }
                const maxRes = template.soft?.maxRes;
                if (Array.isArray(maxRes)) summaryParts.push(`max ~${maxRes[0]}×${maxRes[1]}`);
                const band = template.soft?.recommendedDuration;
                if (band) summaryParts.push(`~${band.minSec}–${band.maxSec}s`);
            }
            summary.textContent = summaryParts.join(" | ") || "No constraints";

            card.append(head, summary);
            templateList.appendChild(card);
        }

        const formToggle = document.createElement("button");
        formToggle.textContent = formState.expanded ? "Cancel Template Edit" : "Add Custom Template";
        formToggle.style.cssText = chromeButtonCss({ variant: "subtle", padding: "5px 12px", fontSize: "11px", radius: "7px" });
        formToggle.addEventListener("click", () => {
            this._templateFormState = formState.expanded
                ? { expanded: false, editId: "" }
                : { expanded: true, editId: "" };
            this._renderModelTemplateSettings?.();
        });
        templateFormHost.appendChild(formToggle);

        if (!formState.expanded) {
            return;
        }

        const editingTemplate = customTemplates.find((template) => template.id === formState.editId)
            || (formState.editId && formState.editId !== "free" && isBuiltinModelTemplate(formState.editId)
                ? getTemplateById(formState.editId, this._settings)
                : null);
        const form = document.createElement("div");
        form.style.cssText = `
            border: 1px solid ${COLORS.border};
            border-radius: 8px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            background: ${COLORS.panelMuted};
        `;

        const title = document.createElement("div");
        title.style.cssText = "font-size:11px;font-weight:700;color:#eef3f8;";
        title.textContent = editingTemplate ? `Edit ${editingTemplate.name}` : "Add Custom Template";
        form.appendChild(title);

        const nameRow = document.createElement("div");
        nameRow.style.cssText = "display:flex;align-items:center;gap:8px;";
        const nameLabel = document.createElement("div");
        nameLabel.style.cssText = "font-size:10px;color:#92a0ad;min-width:90px;";
        nameLabel.textContent = "Template Name";
        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.value = editingTemplate?.name || "";
        nameInput.placeholder = "My Model";
        nameInput.style.cssText = `${chromeInputCss({ fontSize: "11px", padding: "5px 8px", textAlign: "left" })} flex:1;`;
        nameRow.append(nameLabel, nameInput);
        form.appendChild(nameRow);

        // ── Form field helpers ──────────────────────────────────────
        const fieldLabelCss = "font-size:10px;color:#92a0ad;min-width:130px;";
        const helpCss = "font-size:10px;color:#8f9cab;line-height:1.4;";
        const numInput = (value, placeholder, step = "1") => {
            const input = document.createElement("input");
            input.type = "number";
            input.step = step;
            input.value = value ?? "";
            input.placeholder = placeholder;
            input.style.cssText = chromeInputCss({ width: "72px", fontSize: "10px", padding: "4px 6px", textAlign: "right" });
            return input;
        };
        const textInput = (value, placeholder) => {
            const input = document.createElement("input");
            input.type = "text";
            input.value = value ?? "";
            input.placeholder = placeholder;
            input.style.cssText = `${chromeInputCss({ fontSize: "10px", padding: "4px 6px", textAlign: "left" })} flex:1;`;
            return input;
        };
        const labeledRow = (labelText, ...children) => {
            const row = document.createElement("div");
            row.style.cssText = "display:flex;align-items:center;gap:8px;";
            const label = document.createElement("div");
            label.style.cssText = fieldLabelCss;
            label.textContent = labelText;
            row.append(label, ...children);
            form.appendChild(row);
            return row;
        };
        const sectionHeader = (text, help) => {
            const header = document.createElement("div");
            header.style.cssText = "font-size:10px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#c3d0dc;margin-top:6px;";
            header.textContent = text;
            form.appendChild(header);
            if (help) {
                const helpEl = document.createElement("div");
                helpEl.style.cssText = helpCss;
                helpEl.textContent = help;
                form.appendChild(helpEl);
            }
        };
        const fmtResList = (list) => Array.isArray(list) ? list.map((pair) => `${pair[0]}x${pair[1]}`).join(", ") : "";
        const fmtResPair = (pair) => Array.isArray(pair) ? `${pair[0]}x${pair[1]}` : "";
        // Lenient parsers — normalizeCustomTemplate re-validates everything on save.
        const parseFpsList = (value) => String(value || "")
            .split(/[,\s]+/).map((token) => Number(token)).filter((numeric) => Number.isFinite(numeric) && numeric > 0);
        const parseResPair = (value) => {
            const match = String(value || "").trim().match(/(\d+)\s*[x×,]\s*(\d+)/i);
            return match ? [Number(match[1]), Number(match[2])] : null;
        };
        const parseResList = (value) => String(value || "")
            .split(/[,;]+/).map((token) => parseResPair(token)).filter(Boolean);

        const hard = editingTemplate?.hard || {};
        const soft = editingTemplate?.soft || {};

        // ── Hard constraints ──
        sectionHeader("Hard Constraints", "Enforced by snapping — divisibility, the frame rule, and the fps allow-list.");
        const dimStepInput = numInput(hard.dimensionStep, "step");
        const dimOffsetInput = numInput(hard.dimensionOffset, "offset");
        labeledRow("Divisible by / offset", dimStepInput, dimOffsetInput);

        const evenLatentRow = document.createElement("label");
        evenLatentRow.style.cssText = "display:flex;align-items:center;gap:8px;font-size:10px;color:#d9e1e8;";
        const evenLatentInput = document.createElement("input");
        evenLatentInput.type = "checkbox";
        evenLatentInput.checked = hard.evenLatentDimensions === true;
        evenLatentRow.append(evenLatentInput, document.createTextNode("Even latent dimensions"));
        form.appendChild(evenLatentRow);

        const frameStepInput = numInput(hard.frameStep, "step");
        const frameOffsetInput = numInput(hard.frameOffset, "offset");
        labeledRow("Frame step / offset", frameStepInput, frameOffsetInput);
        const framePreview = document.createElement("div");
        framePreview.style.cssText = helpCss;
        form.appendChild(framePreview);
        const refreshFramePreview = () => {
            const step = parseDraftNumber(frameStepInput.value);
            const offset = parseDraftNumber(frameOffsetInput.value) ?? 0;
            const constraint = (step && step > 1) ? { step, offset } : null;
            framePreview.textContent = constraint
                ? `Formula: ${describeConstraintFormula(constraint)} | Sample: ${previewConstraintValues(constraint, 5).join(", ")}`
                : "Formula: Any";
        };
        frameStepInput.addEventListener("input", refreshFramePreview);
        frameOffsetInput.addEventListener("input", refreshFramePreview);
        refreshFramePreview();

        const fpsInput = textInput(Array.isArray(hard.fps) ? hard.fps.join(", ") : "", "24, 25, 48, 50");
        labeledRow("Allowed fps", fpsInput);

        // ── Soft recommendations ──
        sectionHeader("Soft Recommendations", "Advisory only — surfaced as hints; never clamps input.");
        const minDimInput = numInput(soft.minDimension, "64");
        labeledRow("Min dimension", minDimInput);
        const recResInput = textInput(fmtResList(soft.recommendedRes), "1280x720, 720x1280");
        labeledRow("Recommended res", recResInput);
        const recResPreview = document.createElement("div");
        recResPreview.style.cssText = helpCss;
        form.appendChild(recResPreview);
        const maxResInput = textInput(fmtResPair(soft.maxRes), "3840x2160");
        labeledRow("Max res (warn)", maxResInput);
        const maxResPreview = document.createElement("div");
        maxResPreview.style.cssText = helpCss;
        form.appendChild(maxResPreview);
        // Pixel-budget preview (W×H) beneath each res box, mirroring the frame formula preview.
        const refreshResPreviews = () => {
            const recPairs = parseResList(recResInput.value);
            recResPreview.textContent = recPairs.length
                ? `Pixel budget: ${recPairs.map(([w, h]) => `${w}×${h} = ${(w * h).toLocaleString()} px`).join(", ")}`
                : "";
            const maxPair = parseResPair(maxResInput.value);
            maxResPreview.textContent = maxPair
                ? `Pixel budget: ${maxPair[0]}×${maxPair[1]} = ${(maxPair[0] * maxPair[1]).toLocaleString()} px`
                : "";
        };
        recResInput.addEventListener("input", refreshResPreviews);
        maxResInput.addEventListener("input", refreshResPreviews);
        refreshResPreviews();
        const durMinInput = numInput(soft.recommendedDuration?.minSec, "min s", "0.1");
        const durMaxInput = numInput(soft.recommendedDuration?.maxSec, "max s", "0.1");
        labeledRow("Duration min / max (s)", durMinInput, durMaxInput);

        const actionRow = document.createElement("div");
        actionRow.style.cssText = "display:flex;justify-content:flex-end;gap:8px;margin-top:4px;";
        const saveBtn = document.createElement("button");
        saveBtn.textContent = editingTemplate ? "Save Template" : "Create Template";
        saveBtn.style.cssText = chromeButtonCss({ variant: "primary", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        saveBtn.addEventListener("click", () => {
            const name = String(nameInput.value || "").trim();
            if (!name) {
                alert("Template name is required.");
                return;
            }
            const nextTemplate = {
                id: editingTemplate?.id || makeUniqueTemplateId(name),
                name,
                hard: {
                    dimensionStep: parseDraftNumber(dimStepInput.value),
                    dimensionOffset: parseDraftNumber(dimOffsetInput.value),
                    evenLatentDimensions: !!evenLatentInput.checked,
                    frameStep: parseDraftNumber(frameStepInput.value),
                    frameOffset: parseDraftNumber(frameOffsetInput.value),
                    fps: parseFpsList(fpsInput.value),
                },
                soft: {
                    minDimension: parseDraftNumber(minDimInput.value),
                    recommendedRes: parseResList(recResInput.value),
                    maxRes: parseResPair(maxResInput.value),
                    recommendedDuration: {
                        minSec: parseDraftNumber(durMinInput.value, false),
                        maxSec: parseDraftNumber(durMaxInput.value, false),
                    },
                },
            };
            this._templateFormState = { expanded: false, editId: "" };
            if (editingTemplate && editingTemplate.builtIn) {
                // Editing a built-in writes a browser-local override keyed by its id;
                // deep-merge preserves other overrides + customTemplates.
                this._updateSettings({
                    modelTemplates: {
                        builtinOverrides: {
                            [editingTemplate.id]: { name: nextTemplate.name, hard: nextTemplate.hard, soft: nextTemplate.soft },
                        },
                    },
                });
            } else {
                const nextCustomTemplates = editingTemplate
                    ? customTemplates.map((template) => template.id === editingTemplate.id ? nextTemplate : template)
                    : [...customTemplates, nextTemplate];
                this._updateSettings({
                    modelTemplates: { customTemplates: nextCustomTemplates },
                });
            }
        });

        const cancelBtn = document.createElement("button");
        cancelBtn.textContent = "Cancel";
        cancelBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px", fontSize: "11px", radius: "7px" });
        cancelBtn.addEventListener("click", () => {
            this._templateFormState = { expanded: false, editId: "" };
            this._renderModelTemplateSettings?.();
        });

        actionRow.append(cancelBtn, saveBtn);
        form.appendChild(actionRow);
        templateFormHost.appendChild(form);
    };

    const layoutSection = createSection(
        "Layout & UI Scale",
        "Adjust the editor chrome and restore saved fullscreen sizing."
    );
    createScaleRow(layoutSection, "Toolbar", "Toolbar", "Toolbar and transport controls.", () => this._settings.layout.scaleToolbar);
    createScaleRow(layoutSection, "TrackHeaders", "Track Headers", "Lane labels, icons, and left-side track controls.", () => this._settings.layout.scaleTrackHeaders);
    createScaleRow(layoutSection, "Timeline", "Timeline", "Ruler, track heights, clip blocks, and inline editors.", () => this._settings.layout.scaleTimeline);
    createScaleRow(layoutSection, "Gallery", "Asset Gallery", "Gallery lists, tabs, metadata text, and inspector chrome.", () => this._settings.layout.scaleGallery);
    addSectionReset(
        layoutSection,
        "Reset Layout Section",
        "Clear saved fullscreen widths, label widths, track collapses, and UI scale overrides.",
        () => this._resetEditorLayout()
    );

    const timelineSection = createSection(
        "Timeline Behavior",
        "Default snapping behavior, candidate types, and frame/timecode display mode."
    );
    createCheckbox(
        timelineSection,
        "linkedVideoAudioDrop",
        "Link Dropped Video + Audio",
        "Video assets with extracted audio enter the timeline as linked siblings.",
        () => this._settings.timelineBehavior.linkedVideoAudioDrop !== false,
        (checked) => updateCategory("timelineBehavior", "linkedVideoAudioDrop", checked)
    );
    createCheckbox(
        timelineSection,
        "snappingEnabled",
        "Enable Snapping By Default",
        "Use snap guides for drag, trim, and move operations unless toggled off.",
        () => this._settings.timelineBehavior.snappingEnabled,
        (checked) => this._setSnappingEnabled(checked)
    );
    createNumberInput(
        timelineSection,
        "snapThreshold",
        "Snap Threshold",
        "Maximum distance, in frames, before the editor snaps to a candidate.",
        {
            min: 1,
            max: 60,
            step: 1,
            getter: () => this._settings.timelineBehavior.snapThreshold,
            onChange: (value) => updateCategory("timelineBehavior", "snapThreshold", Math.round(value)),
        }
    );
    for (const option of SNAP_TARGET_OPTIONS) {
        createCheckbox(
            timelineSection,
            `snapTarget_${option.key}`,
            option.label,
            "Include this target type when evaluating snap candidates.",
            () => this._settings.timelineBehavior.snapTargets[option.key],
            (checked) => {
                this._updateSettings({
                    timelineBehavior: {
                        snapTargets: {
                            [option.key]: checked,
                        },
                    },
                });
            }
        );
    }
    createSelect(
        timelineSection,
        "timecodeMode",
        "Default Time Display",
        "Choose whether editor inputs and readouts default to frame counts or timecode.",
        TIMECODE_MODE_OPTIONS,
        () => this._settings.timelineBehavior.timecodeMode,
        (value) => this._setTimecodeMode(value)
    );
    addSectionReset(
        timelineSection,
        "Reset Timeline Section",
        "Restore snapping, snap targets, and time display defaults.",
        () => this._updateSettings({ timelineBehavior: DEFAULT_EDITOR_SETTINGS.timelineBehavior })
    );

    const playbackSection = createSection(
        "Playback",
        "Controls for timeline playback flow and viewport preview quality."
    );
    createCheckbox(
        playbackSection,
        "loopSelection",
        "Loop Active Selection",
        "If a selection exists, playback repeats within that range.",
        () => this._settings.playback.loopSelection,
        (checked) => updateCategory("playback", "loopSelection", checked)
    );
    createCheckbox(
        playbackSection,
        "autoScrollPlayhead",
        "Auto-Scroll Playhead",
        "Keep the playhead visible as playback or keyboard scrubbing moves forward.",
        () => this._settings.playback.autoScrollPlayhead,
        (checked) => updateCategory("playback", "autoScrollPlayhead", checked)
    );
    createCheckbox(
        playbackSection,
        "returnToPlaybackStart",
        "Return To Start On Stop",
        "Stopping playback jumps back to the frame where playback began.",
        () => this._settings.playback.returnToPlaybackStart,
        (checked) => updateCategory("playback", "returnToPlaybackStart", checked)
    );
    createSelect(
        playbackSection,
        "playbackResolution",
        "Viewport Playback Resolution",
        "Lower preview resolution for smoother playback on heavier scenes.",
        PLAYBACK_RESOLUTION_OPTIONS,
        () => this._settings.playback.resolution,
        (value) => updateCategory("playback", "resolution", value)
    );
    createCheckbox(
        playbackSection,
        "prebufferEnabled",
        "Prebuffer Upcoming Clips",
        "Warm the next couple of clip boundaries ahead so playback crosses them without stalling.",
        () => this._settings.playback.prebufferEnabled,
        (checked) => updateCategory("playback", "prebufferEnabled", checked)
    );
    createNumberInput(
        playbackSection,
        "prebufferLookaheadMs",
        "Prebuffer Horizon",
        "How far ahead (ms) to look for upcoming clip boundaries to warm. Higher reaches farther boundaries on long clips; short clips are unaffected.",
        {
            min: 100,
            max: 5000,
            step: 100,
            getter: () => this._settings.playback.prebufferLookaheadMs,
            onChange: (value) => updateCategory("playback", "prebufferLookaheadMs", Math.round(value)),
        }
    );
    createNumberInput(
        playbackSection,
        "prebufferBoundaryDepth",
        "Prebuffer Boundary Depth",
        "How many upcoming clip boundaries to warm ahead. Lower does less speculative work; higher protects dense edits farther ahead.",
        {
            min: 1,
            max: 12,
            step: 1,
            getter: () => this._settings.playback.prebufferBoundaryDepth,
            onChange: (value) => updateCategory("playback", "prebufferBoundaryDepth", Math.round(value)),
        }
    );
    createNumberInput(
        playbackSection,
        "prebufferMaxEntries",
        "Prebuffer Max Warmed Clips",
        "Cap on clips warmed ahead at once. Lower uses less memory/cache pressure; higher keeps more upcoming clips ready.",
        {
            min: 1,
            max: 64,
            step: 1,
            getter: () => this._settings.playback.prebufferMaxEntries,
            onChange: (value) => updateCategory("playback", "prebufferMaxEntries", Math.round(value)),
        }
    );
    createNumberInput(
        playbackSection,
        "decodeConcurrency",
        "Playback Decode Concurrency",
        "Max video clips decoded at once during playback. Lower reduces CPU/disk contention; higher can keep stacked or rapid-cut scenes smoother.",
        {
            min: 1,
            max: 8,
            step: 1,
            getter: () => this._settings.playback.decodeConcurrency,
            onChange: (value) => updateCategory("playback", "decodeConcurrency", Math.round(value)),
        }
    );
    addSectionReset(
        playbackSection,
        "Reset Playback Section",
        "Restore playback flow, prebuffering, and decode tuning defaults.",
        () => this._updateSettings({ playback: DEFAULT_EDITOR_SETTINGS.playback })
    );

    const notificationsSection = createSection(
        "Notifications",
        "Browser-local toast timing. Hovering a toast pauses its countdown."
    );
    createNumberInput(
        notificationsSection,
        "toastDurationMs",
        "Toast Duration (ms)",
        "How long info and success toasts stay before auto-dismissing. Warnings and errors use separate timing.",
        {
            min: 1000,
            max: 30000,
            step: 250,
            getter: () => this._settings.notifications?.toastDurationMs ?? DEFAULT_EDITOR_SETTINGS.notifications.toastDurationMs,
            onChange: (value) => updateCategory("notifications", "toastDurationMs", Math.max(1000, Math.round(value))),
        }
    );
    createNumberInput(
        notificationsSection,
        "errorToastDurationMs",
        "Warning/Error Toast Duration (ms)",
        "How long warning and error toasts stay before auto-dismissing. 0 keeps them until dismissed.",
        {
            min: 0,
            max: 120000,
            step: 500,
            getter: () => this._settings.notifications?.errorToastDurationMs ?? DEFAULT_EDITOR_SETTINGS.notifications.errorToastDurationMs,
            onChange: (value) => updateCategory("notifications", "errorToastDurationMs", Math.max(0, Math.round(value))),
        }
    );
    addSectionReset(
        notificationsSection,
        "Reset Notifications Section",
        "Restore toast timing defaults.",
        () => this._updateSettings({ notifications: DEFAULT_EDITOR_SETTINGS.notifications })
    );

    const renderSection = createSection(
        "Render",
        "Browser-local take placement and render-cache policies."
    );
    createSelect(
        renderSection,
        "takePlacementMode",
        "Take Placement Mode",
        "Trimmed places only the generated portion on the timeline. Untrimmed includes pre/post context frames for seam inspection.",
        TAKE_PLACEMENT_MODE_OPTIONS,
        () => this._settings.render?.takePlacementMode ?? "trimmed",
        (value) => updateCategory("render", "takePlacementMode", value)
    );
    createCheckbox(
        renderSection,
        "linkedTakePlacement",
        "Link Take Video + Audio",
        "New take placements link generated video and extracted audio siblings.",
        () => this._settings.render?.linkedTakePlacement !== false,
        (checked) => updateCategory("render", "linkedTakePlacement", checked)
    );
    createCheckbox(
        renderSection,
        "takePlacementMuted",
        "New Takes Start Muted",
        "New take placements enter the timeline muted until explicitly enabled.",
        () => !!this._settings.render?.takePlacementMuted,
        (checked) => updateCategory("render", "takePlacementMuted", checked)
    );
    createSelect(
        renderSection,
        "defaultSavePreset",
        "Default Save Preset",
        "Applied to newly inserted Sonder Save Video nodes.",
        SAVE_PRESET_OPTIONS,
        () => this._settings.render?.defaultSavePreset ?? DEFAULT_SAVE_PRESET,
        (value) => updateCategory("render", "defaultSavePreset", value)
    );
    createPresetNumberInput(
        renderSection,
        "maxRenderCacheEntries",
        "Render Cache Entries",
        "Off prevents new timeline preview render-cache writes. Higher keep counts retain more project-local tensors; cleanup runs when the editor opens or this cap changes.",
        {
            options: RENDER_CACHE_ENTRY_PRESETS,
            min: 0,
            max: 100000,
            step: 1,
            integer: true,
            allowNull: true,
            customDefault: DEFAULT_EDITOR_SETTINGS.render.maxRenderCacheEntries,
            getter: () => this._settings.render?.maxRenderCacheEntries === null
                ? null
                : (this._settings.render?.maxRenderCacheEntries ?? DEFAULT_EDITOR_SETTINGS.render.maxRenderCacheEntries),
            onChange: (value) => updateCategory("render", "maxRenderCacheEntries", value),
        }
    );
    createNumberInput(
        renderSection,
        "batchRenderMaxFramesPerChunk",
        "Batch Max Frames",
        "Maximum total frames per chunk including pre/post context. Values above 0 snap up to the active template's frame rule; 0 uses the internal 97-frame budget.",
        {
            min: 0,
            max: 10000,
            step: 1,
            getter: () => this._settings.batchRender.maxFramesPerChunk,
            onChange: (value) => updateCategory("batchRender", "maxFramesPerChunk", Math.max(0, Math.round(value))),
        }
    );
    addSectionReset(
        renderSection,
        "Reset Render Section",
        "Restore take placement, save preset, render cache, and batch-frame defaults.",
        () => this._updateSettings({
            render: {
                takePlacementMode: DEFAULT_EDITOR_SETTINGS.render.takePlacementMode,
                linkedTakePlacement: DEFAULT_EDITOR_SETTINGS.render.linkedTakePlacement,
                takePlacementMuted: DEFAULT_EDITOR_SETTINGS.render.takePlacementMuted,
                defaultSavePreset: DEFAULT_EDITOR_SETTINGS.render.defaultSavePreset,
                maxRenderCacheEntries: DEFAULT_EDITOR_SETTINGS.render.maxRenderCacheEntries,
            },
            batchRender: DEFAULT_EDITOR_SETTINGS.batchRender,
        })
    );

    const guidesSection = createSection(
        "Guides",
        "Guide capture defaults for clip-frame extraction."
    );
    createNumberInput(
        guidesSection,
        "guideSnapshotMaxLongEdge",
        "Snapshot Max Long Edge",
        "0 keeps the automatic source/scene long-edge rule. Values above 0 cap captured guide PNGs.",
        {
            min: 0,
            max: 8192,
            step: 64,
            getter: () => this._settings.guides?.guideSnapshotMaxLongEdge ?? 0,
            onChange: (value) => updateCategory("guides", "guideSnapshotMaxLongEdge", Math.max(0, Math.round(value))),
        }
    );
    createCheckbox(
        guidesSection,
        "hoverPreviewEnabled",
        "Guide Hover Preview",
        "Show a larger guide thumbnail when hovering guide markers or guide rows.",
        () => this._settings.guides?.hoverPreviewEnabled ?? true,
        (checked) => {
            updateCategory("guides", "hoverPreviewEnabled", checked);
            if (!checked) this._hideGuideHoverPreview();
        }
    );
    createNumberInput(
        guidesSection,
        "hoverPreviewSize",
        "Hover Preview Size",
        "Maximum preview edge in pixels.",
        {
            min: 96,
            max: 360,
            step: 12,
            getter: () => this._guideHoverPreviewSize(),
            onChange: (value) => updateCategory("guides", "hoverPreviewSize", Math.max(96, Math.min(360, Math.round(value)))),
        }
    );
    addSectionReset(
        guidesSection,
        "Reset Guides Section",
        "Restore guide snapshot and hover preview defaults.",
        () => this._updateSettings({ guides: DEFAULT_EDITOR_SETTINGS.guides })
    );

    const serverSection = createSection(
        "Server",
        "Install-level trust settings. These apply to everyone who can reach this ComfyUI server."
    );
    const externalLinksToggle = createCheckbox(
        serverSection,
        "allowExternalProjectLinks",
        "Allow External Project Links",
        "Follow symlinks/junctions everywhere the editor resolves files. Anything inside linked folders becomes readable by anyone who can reach your ComfyUI server.",
        () => this._serverSettings?.allow_external_project_links === true,
        (checked) => {
            Promise.resolve(this._setAllowExternalProjectLinks(checked)).catch(() => {});
        }
    );
    externalLinksToggle.disabled = true;
    externalLinksToggle.title = "Loading server settingâ€¦";

    const promptsSection = createSection(
        "Prompts",
        "Prompt lane behavior. Channel labels, section delimiter, and boundary threshold are PROJECT-WIDE; the rest are browser-local preferences."
    );
    // — Project-wide (host-owned versioned project PUTs, not settings writes).
    //   syncSettingsPanelControls only syncs settings-backed controls, so
    //   these read host getters directly at build time.
    createCheckbox(
        promptsSection,
        "promptChannelLabels",
        "Channel Labels (project-wide)",
        "Prefix channels as [VISUAL]: / [SPEECH]: / [SOUNDS]: in composed prompt output. Saved into the project.",
        () => this._promptChannelLabels === true,
        (checked) => {
            Promise.resolve(this._togglePromptChannelLabels(checked)).catch(() => {});
        }
    );
    {
        const delimiterControls = createRow(
            promptsSection,
            "Section Delimiter (project-wide)",
            "Seam inserted between prompt sections in the composed output (default \".\"). Empty = plain space. Saved into the project."
        );
        const delimiterInput = document.createElement("input");
        delimiterInput.type = "text";
        delimiterInput.maxLength = 8;
        delimiterInput.value = String(this._promptSectionDelimiter ?? ".");
        delimiterInput.style.cssText = chromeInputCss({ width: "72px", textAlign: "center" });
        const commitDelimiter = () => {
            Promise.resolve(this._setPromptSectionDelimiter(delimiterInput.value))
                .then(() => { delimiterInput.value = String(this._promptSectionDelimiter ?? "."); })
                .catch(() => { delimiterInput.value = String(this._promptSectionDelimiter ?? "."); });
        };
        delimiterInput.addEventListener("change", commitDelimiter);
        delimiterInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") commitDelimiter();
            e.stopPropagation();
        });
        delimiterControls.appendChild(delimiterInput);
        controls.promptSectionDelimiter = delimiterInput;
    }
    {
        const thresholdControls = createRow(
            promptsSection,
            "Boundary Prompt Threshold % (project-wide)",
            "Ignore a prompt section in a render window when the selection only clips a small sliver of it at the window edge (under N% of that section's length). 0 = off. Saved into the project."
        );
        const thresholdInput = document.createElement("input");
        thresholdInput.type = "number";
        thresholdInput.min = "0";
        thresholdInput.max = "100";
        thresholdInput.step = "1";
        thresholdInput.value = String(this._promptFrameThreshold ?? 10);
        thresholdInput.style.cssText = chromeInputCss({ width: "72px", textAlign: "center" });
        const commitThreshold = () => {
            Promise.resolve(this._setPromptFrameThreshold(thresholdInput.value))
                .then(() => { thresholdInput.value = String(this._promptFrameThreshold ?? 10); })
                .catch(() => { thresholdInput.value = String(this._promptFrameThreshold ?? 10); });
        };
        thresholdInput.addEventListener("change", commitThreshold);
        thresholdInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") commitThreshold();
            e.stopPropagation();
        });
        thresholdControls.appendChild(thresholdInput);
        controls.promptFrameThreshold = thresholdInput;
    }
    // — Browser-local preferences
    createCheckbox(
        promptsSection,
        "queueSectionBatch",
        "Queue Sections as Batch",
        "On: Queue Prompt Section auto-chunks past the batch budget. Off: one job for the whole range.",
        () => this._settings.prompts?.queueSectionBatch !== false,
        (checked) => updateCategory("prompts", "queueSectionBatch", checked)
    );
    createCheckbox(
        promptsSection,
        "promptHoverPreviewEnabled",
        "Prompt Hover Preview",
        "Show the full composed prompt text when hovering sections or the global item on the timeline.",
        () => this._settings.prompts?.hoverPreviewEnabled !== false,
        (checked) => {
            updateCategory("prompts", "hoverPreviewEnabled", checked);
            if (!checked) this._hidePromptHoverPreview();
        }
    );
    addSectionReset(
        promptsSection,
        "Reset Prompts Section",
        "Restore browser-local prompt queueing and hover preview defaults. Project-wide prompt controls are not changed.",
        () => this._updateSettings({
            prompts: {
                queueSectionBatch: DEFAULT_EDITOR_SETTINGS.prompts.queueSectionBatch,
                hoverPreviewEnabled: DEFAULT_EDITOR_SETTINGS.prompts.hoverPreviewEnabled,
            },
        })
    );

    const appearanceSection = createSection(
        "Appearance",
        "Non-destructive visual preferences for timeline readability."
    );
    const waveformControls = createRow(appearanceSection, "Waveform Accent Color", "Tint used for the audio waveform overlay.");
    const waveformInput = document.createElement("input");
    waveformInput.type = "color";
    waveformInput.value = this._settings.appearance.waveformAccent;
    waveformInput.style.cssText = "width:44px;height:28px;padding:0;border:none;background:none;cursor:pointer;";
    waveformInput.addEventListener("change", () => updateCategory("appearance", "waveformAccent", waveformInput.value));
    waveformControls.appendChild(waveformInput);
    controls.waveformAccent = waveformInput;

    const brightnessControls = createRow(appearanceSection, "Timeline Background Brightness", "Lighten or darken timeline chrome without changing lane colors.");
    const brightnessInput = document.createElement("input");
    brightnessInput.type = "range";
    brightnessInput.min = "70";
    brightnessInput.max = "130";
    brightnessInput.step = "1";
    brightnessInput.value = String(this._settings.appearance.timelineBrightness);
    brightnessInput.style.width = "120px";
    brightnessInput.addEventListener("input", () => {
        updateCategory("appearance", "timelineBrightness", Math.round(Number(brightnessInput.value) || 100));
    });
    const brightnessLabel = document.createElement("span");
    brightnessLabel.style.cssText = `font-size:11px;color:${COLORS.text};min-width:38px;text-align:right;`;
    brightnessLabel.textContent = `${this._settings.appearance.timelineBrightness}%`;
    brightnessControls.append(brightnessInput, brightnessLabel);
    controls.timelineBrightness = brightnessInput;
    controls.timelineBrightnessLabel = brightnessLabel;

    createSelect(
        appearanceSection,
        "clipLabelMode",
        "Clip Label Content",
        "Choose how much text appears inside clip and audio blocks.",
        CLIP_LABEL_MODE_OPTIONS,
        () => this._settings.appearance.clipLabelMode,
        (value) => updateCategory("appearance", "clipLabelMode", value)
    );
    createSelect(
        appearanceSection,
        "clipLabelVerticalAlign",
        "Clip Label Vertical Alignment",
        "Vertical placement for video and audio timeline labels.",
        CLIP_LABEL_VERTICAL_ALIGN_OPTIONS,
        () => this._settings.appearance.clipLabelVerticalAlign,
        (value) => updateCategory("appearance", "clipLabelVerticalAlign", value)
    );
    createSelect(
        appearanceSection,
        "clipLabelHorizontalAlign",
        "Clip Label Horizontal Alignment",
        "Horizontal placement for video and audio timeline labels.",
        CLIP_LABEL_HORIZONTAL_ALIGN_OPTIONS,
        () => this._settings.appearance.clipLabelHorizontalAlign,
        (value) => updateCategory("appearance", "clipLabelHorizontalAlign", value)
    );

    createCheckbox(
        appearanceSection,
        "sceneOutline",
        "Scene Bounds Outline",
        "Stroke a thin border at the scene frame edge in the fullscreen viewport — helps read edge-pad / fill framing.",
        () => this._settings.appearance.sceneOutline,
        (checked) => updateCategory("appearance", "sceneOutline", checked)
    );

    const laneTintSpecs = [
        { key: "video", label: "Video Lane Tint", description: "Optional subtle color overlay on all video lane backgrounds." },
        { key: "audio", label: "Audio Lane Tint", description: "Optional subtle color overlay on all audio lane backgrounds." },
        { key: "motion_driver", label: "Driver Lane Tint", description: "Optional subtle color overlay on all driver lane backgrounds." },
    ];
    for (const spec of laneTintSpecs) {
        const row = createRow(appearanceSection, spec.label, spec.description);
        const input = document.createElement("input");
        input.type = "color";
        input.style.cssText = "width:44px;height:28px;padding:0;border:none;background:none;cursor:pointer;";
        const current = this._settings.appearance.laneTintOverrides?.[spec.key] || "";
        const displayHex = current || "#000000";
        input.value = displayHex;
        input.dataset.active = current ? "1" : "0";
        const resetBtn = document.createElement("button");
        resetBtn.type = "button";
        resetBtn.textContent = "Reset";
        resetBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "3px 8px", fontSize: "10px", radius: "5px" });
        const applyTint = (hex) => {
            updateCategory("appearance", "laneTintOverrides", {
                ...(this._settings.appearance.laneTintOverrides || {}),
                [spec.key]: hex,
            });
        };
        input.addEventListener("change", () => {
            input.dataset.active = "1";
            applyTint(input.value);
        });
        resetBtn.addEventListener("click", () => {
            input.value = "#000000";
            input.dataset.active = "0";
            applyTint("");
        });
        row.append(input, resetBtn);
        controls[`laneTintOverride_${spec.key}`] = input;
    }

    createNumberInput(
        appearanceSection,
        "editorMarginTop",
        "Editor Margin — Top",
        "Inset the fullscreen / mounted-tab editor from the screen edges (px), so controls aren't jammed against the border.",
        {
            min: 0,
            max: 64,
            step: 1,
            getter: () => this._settings.appearance.editorMargins?.top ?? 0,
            onChange: (value) => updateCategory("appearance", "editorMargins", { ...(this._settings.appearance.editorMargins || {}), top: value }),
        }
    );
    createNumberInput(
        appearanceSection,
        "editorMarginBottom",
        "Editor Margin — Bottom",
        "Bottom inset for the fullscreen / mounted-tab editor (px).",
        {
            min: 0,
            max: 64,
            step: 1,
            getter: () => this._settings.appearance.editorMargins?.bottom ?? 0,
            onChange: (value) => updateCategory("appearance", "editorMargins", { ...(this._settings.appearance.editorMargins || {}), bottom: value }),
        }
    );
    createNumberInput(
        appearanceSection,
        "editorMarginSides",
        "Editor Margin — Sides",
        "Left & right inset for the fullscreen / mounted-tab editor (px).",
        {
            min: 0,
            max: 64,
            step: 1,
            getter: () => this._settings.appearance.editorMargins?.sides ?? 0,
            onChange: (value) => updateCategory("appearance", "editorMargins", { ...(this._settings.appearance.editorMargins || {}), sides: value }),
        }
    );
    addSectionReset(
        appearanceSection,
        "Reset Appearance Section",
        "Restore waveform, label display, lane tint, scene outline, and editor margin defaults.",
        () => this._updateSettings({ appearance: DEFAULT_EDITOR_SETTINGS.appearance })
    );

    const projectDefaultsSection = createSection(
        "Project Defaults",
        "Values used to prefill new projects and new blank scenes."
    );
    createNumberInput(
        projectDefaultsSection,
        "defaultProjectFps",
        "Default FPS",
        "Used to prefill create-project widgets in SonderEditor.",
        {
            min: 1,
            max: 240,
            step: 1,
            getter: () => this._settings.projectDefaults.fps,
            onChange: (value) => updateCategory("projectDefaults", "fps", value),
        }
    );
    createNumberInput(
        projectDefaultsSection,
        "defaultProjectWidth",
        "Default Project Width",
        "Used as the starting width for newly created projects.",
        {
            min: 64,
            max: 8192,
            step: 8,
            getter: () => this._settings.projectDefaults.width,
            onChange: (value) => updateCategory("projectDefaults", "width", Math.round(value)),
        }
    );
    createNumberInput(
        projectDefaultsSection,
        "defaultProjectHeight",
        "Default Project Height",
        "Used as the starting height for newly created projects.",
        {
            min: 64,
            max: 8192,
            step: 8,
            getter: () => this._settings.projectDefaults.height,
            onChange: (value) => updateCategory("projectDefaults", "height", Math.round(value)),
        }
    );
    createNumberInput(
        projectDefaultsSection,
        "defaultSceneDuration",
        "Default New-Scene Duration",
        "Applies only when creating a new blank scene; duplicated scenes keep their source duration.",
        {
            min: 1,
            max: 99999,
            step: 1,
            getter: () => this._settings.projectDefaults.newSceneDuration,
            onChange: (value) => updateCategory("projectDefaults", "newSceneDuration", Math.round(value)),
        }
    );
    createNumberInput(
        projectDefaultsSection,
        "defaultGuideStrength",
        "Default Guide Strength",
        "Applied to new guide frames, including image drops and clip snapshots.",
        {
            min: 0,
            max: 1,
            step: 0.05,
            getter: () => this._settings.projectDefaults.defaultGuideStrength,
            onChange: (value) => updateCategory("projectDefaults", "defaultGuideStrength", value),
        }
    );
    createNumberInput(
        projectDefaultsSection,
        "defaultMotionDriverStrength",
        "Default Driver Strength",
        "Applied when creating driver clips.",
        {
            min: 0,
            max: 1,
            step: 0.05,
            getter: () => this._settings.projectDefaults.defaultMotionDriverStrength,
            onChange: (value) => updateCategory("projectDefaults", "defaultMotionDriverStrength", value),
        }
    );
    createSelect(
        projectDefaultsSection,
        "defaultFitMode",
        "Default Fit Mode",
        "How new clips and guides fill the scene frame when their aspect differs. Override per item in the timeline.",
        FIT_MODE_OPTIONS,
        () => this._settings.projectDefaults.defaultFitMode,
        (value) => updateCategory("projectDefaults", "defaultFitMode", value)
    );
    createSelect(
        projectDefaultsSection,
        "defaultCropPosition",
        "Default Crop Anchor",
        "Anchor used when a new item's fit mode is Fill (crop).",
        CROP_POSITION_OPTIONS,
        () => this._settings.projectDefaults.defaultCropPosition,
        (value) => updateCategory("projectDefaults", "defaultCropPosition", value)
    );
    createSelect(
        projectDefaultsSection,
        "defaultTemplateId",
        "Default Template",
        "Used when creating new projects from the editor node.",
        templateOptions(),
        () => this._settings.projectDefaults.defaultTemplateId || "free",
        (value) => updateCategory("projectDefaults", "defaultTemplateId", value)
    );
    addSectionReset(
        projectDefaultsSection,
        "Reset Project Defaults Section",
        "Restore new-project, new-scene, guide, driver, fit, crop, and template defaults.",
        () => this._updateSettings({ projectDefaults: DEFAULT_EDITOR_SETTINGS.projectDefaults })
    );
    body.appendChild(modelTemplatesSection);
    this._renderModelTemplateSettings();

    const gallerySection = createSection(
        "Asset Gallery",
        "Global defaults for gallery ordering and inspector presentation."
    );
    createSelect(
        gallerySection,
        "gallerySortMode",
        "Default Sort Mode",
        "New gallery instances use this order unless changed later.",
        GALLERY_SORT_OPTIONS,
        () => this._settings.gallery.sortMode,
        (value) => updateCategory("gallery", "sortMode", value)
    );
    createCheckbox(
        gallerySection,
        "galleryInspectorCollapsed",
        "Inspector Starts Collapsed",
        "Open the shared asset gallery with the right-side inspector hidden.",
        () => this._settings.gallery.inspectorCollapsed,
        (checked) => updateCategory("gallery", "inspectorCollapsed", checked)
    );
    createSelect(
        gallerySection,
        "galleryThumbnailSize",
        "Thumbnail Size",
        "Controls the default media preview size in gallery lists and trash.",
        GALLERY_THUMBNAIL_SIZE_OPTIONS,
        () => this._settings.gallery.thumbnailSize,
        (value) => updateCategory("gallery", "thumbnailSize", value)
    );
    createCheckbox(
        gallerySection,
        "galleryArtifactInspectorExpanded",
        "Artifact Inspector Expanded",
        "Show the artifact metadata inspector in its expanded state by default.",
        () => this._settings.gallery.artifactInspectorExpanded,
        (checked) => updateCategory("gallery", "artifactInspectorExpanded", checked)
    );
    createCheckbox(
        gallerySection,
        "galleryStickyFolderHeaders",
        "Sticky Folder Headers",
        "Keep the current Root, folder, or Trash header visible while scrolling the gallery.",
        () => this._settings.gallery.stickyFolderHeaders !== false,
        (checked) => updateCategory("gallery", "stickyFolderHeaders", checked)
    );
    createNumberInput(
        gallerySection,
        "trashRetentionDays",
        "Trash Retention Days",
        "Hard-delete trashed assets after this many days during asset refresh/open sync.",
        {
            min: 0,
            max: 36500,
            step: 1,
            getter: () => this._trashRetentionDays(),
            onChange: (value) => updateCategory("render", "trashRetentionDays", Math.max(0, Math.round(value))),
        }
    );
    createPresetNumberInput(
        gallerySection,
        "trashMaxSizeMB",
        "Trash Max Size",
        "Optional decimal-MB cap for trashed asset source files. Oldest trash is purged first during asset refresh/open sync.",
        {
            options: TRASH_SIZE_MB_PRESETS,
            min: 0,
            max: 100000000,
            step: 0.1,
            integer: false,
            allowNull: true,
            customDefault: 250,
            getter: () => this._settings.render?.trashMaxSizeMB ?? null,
            onChange: (value) => updateCategory("render", "trashMaxSizeMB", value),
        }
    );
    addSectionReset(
        gallerySection,
        "Reset Gallery Section",
        "Restore gallery sort, scope, view, thumbnail, inspector, sticky-header, and trash-cleanup defaults.",
        () => this._updateSettings({
            gallery: DEFAULT_EDITOR_SETTINGS.gallery,
            render: {
                trashRetentionDays: DEFAULT_EDITOR_SETTINGS.render.trashRetentionDays,
                trashMaxSizeMB: DEFAULT_EDITOR_SETTINGS.render.trashMaxSizeMB,
            },
        })
    );

    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);
    this._settingsPanelEl = backdrop;
    syncSettingsPanelControls.call(this);
    Promise.resolve(this._loadServerSettings?.()).catch(() => {});

    backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) this._hideSettingsPanel();
    });
    this._settingsPanelKeyOff = registerKeyboardConsumer({
        id: this._keyboardConsumerId("settings"),
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (e) => {
            if (e.key === "Escape") {
                this._hideSettingsPanel();
                return true;
            }
            return false;
        },
    });
}

function hideSettingsPanel() {
    if (this._settingsPanelEl) {
        this._settingsPanelEl.remove();
        this._settingsPanelEl = null;
        this._settingsPanelControls = null;
        this._renderModelTemplateSettings = null;
    }
    if (this._settingsPanelKeyOff) {
        this._settingsPanelKeyOff();
        this._settingsPanelKeyOff = null;
    }
}
