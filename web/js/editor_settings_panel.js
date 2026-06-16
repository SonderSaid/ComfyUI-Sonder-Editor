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
    CROP_POSITION_OPTIONS,
    DEFAULT_EDITOR_SETTINGS,
    DEFAULT_SAVE_PRESET,
    FIT_MODE_OPTIONS,
    GALLERY_SORT_OPTIONS,
    GALLERY_THUMBNAIL_SIZE_OPTIONS,
    MODEL_TEMPLATE_PARAM_KEYS,
    PLAYBACK_RESOLUTION_OPTIONS,
    SAVE_PRESET_OPTIONS,
    SNAP_TARGET_OPTIONS,
    TAKE_PLACEMENT_MODE_OPTIONS,
    TIMECODE_MODE_OPTIONS,
    describeConstraintFormula,
    getAllModelTemplates,
    previewConstraintValues,
} from "./editor_settings.js";

const RENDER_CACHE_ENTRY_PRESETS = [
    { value: "3", label: "3" },
    { value: "5", label: "5" },
    { value: "10", label: "10" },
    { value: "25", label: "25" },
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
    if (controls.waveformAccent) controls.waveformAccent.value = this._settings.appearance.waveformAccent;
    if (controls.timelineBrightness) controls.timelineBrightness.value = String(this._settings.appearance.timelineBrightness);
    if (controls.timelineBrightnessLabel) controls.timelineBrightnessLabel.textContent = `${this._settings.appearance.timelineBrightness}%`;
    if (controls.clipLabelMode) controls.clipLabelMode.value = this._settings.appearance.clipLabelMode;
    if (controls.sceneOutline) controls.sceneOutline.checked = this._settings.appearance.sceneOutline !== false;
    for (const tintKey of ["video", "audio", "motion_driver"]) {
        const tintInput = controls[`laneTintOverride_${tintKey}`];
        if (tintInput) {
            const stored = this._settings.appearance.laneTintOverrides?.[tintKey] || "";
            tintInput.value = stored || "#000000";
            tintInput.dataset.active = stored ? "1" : "0";
        }
    }
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
    if (controls.batchRenderMaxFramesPerChunk) controls.batchRenderMaxFramesPerChunk.value = String(this._settings.batchRender.maxFramesPerChunk);
    if (controls.defaultProjectFps) controls.defaultProjectFps.value = String(this._settings.projectDefaults.fps);
    if (controls.defaultProjectWidth) controls.defaultProjectWidth.value = String(this._settings.projectDefaults.width);
    if (controls.defaultProjectHeight) controls.defaultProjectHeight.value = String(this._settings.projectDefaults.height);
    if (controls.defaultSceneDuration) controls.defaultSceneDuration.value = String(this._settings.projectDefaults.newSceneDuration);
    if (controls.defaultGuideStrength) controls.defaultGuideStrength.value = String(this._settings.projectDefaults.defaultGuideStrength);
    if (controls.defaultMotionDriverStrength) controls.defaultMotionDriverStrength.value = String(this._settings.projectDefaults.defaultMotionDriverStrength);
    if (controls.defaultFitMode) controls.defaultFitMode.value = this._settings.projectDefaults.defaultFitMode || "pad_edge";
    if (controls.defaultCropPosition) controls.defaultCropPosition.value = this._settings.projectDefaults.defaultCropPosition || "center";
    if (controls.defaultTemplateId) controls.defaultTemplateId.value = this._settings.projectDefaults.defaultTemplateId || "free";
    if (controls.gallerySortMode) controls.gallerySortMode.value = this._settings.gallery.sortMode;
    if (controls.galleryInspectorCollapsed) controls.galleryInspectorCollapsed.checked = !!this._settings.gallery.inspectorCollapsed;
    if (controls.galleryThumbnailSize) controls.galleryThumbnailSize.value = this._settings.gallery.thumbnailSize;
    if (controls.galleryArtifactInspectorExpanded) controls.galleryArtifactInspectorExpanded.checked = !!this._settings.gallery.artifactInspectorExpanded;
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
        <div style="font-size:11px;color:#909090;margin-top:3px;">Local browser preferences for layout, playback, appearance, project defaults, and gallery behavior.</div>
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

    const summarizeConstraint = (constraint, key) => {
        if (!constraint || typeof constraint !== "object") return `${key}: Any`;
        const formula = describeConstraintFormula(constraint);
        if (constraint.min != null || constraint.max != null) {
            const min = constraint.min != null ? constraint.min : "-";
            const max = constraint.max != null ? constraint.max : "-";
            return `${key}: ${formula} [${min}..${max}]`;
        }
        return `${key}: ${formula}`;
    };

    const parseDraftNumber = (value, integer = true) => {
        if (value === "" || value == null) return undefined;
        const numeric = Number(value);
        if (!Number.isFinite(numeric)) return undefined;
        return integer ? Math.round(numeric) : numeric;
    };

    const buildConstraintDraft = (paramKey, fields) => {
        const integer = paramKey !== "fps";
        const constraint = {};
        const step = parseDraftNumber(fields.step.value, integer);
        const offset = parseDraftNumber(fields.offset.value, integer);
        const min = parseDraftNumber(fields.min.value, integer);
        const max = parseDraftNumber(fields.max.value, integer);
        if (step != null && step > 0) constraint.step = step;
        if (offset != null) constraint.offset = offset;
        if (min != null) constraint.min = min;
        if (max != null) constraint.max = max;
        if (constraint.min != null && constraint.max != null && constraint.max < constraint.min) {
            constraint.max = constraint.min;
        }
        return Object.keys(constraint).length ? constraint : null;
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
            const badge = template.builtIn ? "Built-in" : "Custom";
            nameWrap.innerHTML = `
                <div style="font-size:11px;font-weight:700;color:${COLORS.text};">${template.name}</div>
                <div style="font-size:10px;color:${COLORS.textMuted};">${badge}${template.id === this._templateId ? " &bull; Active Project Template" : ""}</div>
            `;
            head.appendChild(nameWrap);

            if (!template.builtIn) {
                const actions = document.createElement("div");
                actions.style.cssText = "display:flex;gap:6px;";

                const editBtn = document.createElement("button");
                editBtn.textContent = "Edit";
                editBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "4px 9px", fontSize: "10px", radius: "6px" });
                editBtn.addEventListener("click", () => {
                    this._templateFormState = { expanded: true, editId: template.id };
                    this._renderModelTemplateSettings?.();
                });

                const deleteBtn = document.createElement("button");
                deleteBtn.textContent = "Delete";
                deleteBtn.style.cssText = chromeButtonCss({ variant: "danger", padding: "4px 9px", fontSize: "10px", radius: "6px" });
                deleteBtn.addEventListener("click", async () => {
                    this._templateFormState = { expanded: false, editId: "" };
                    await this._deleteCustomModelTemplate(template.id);
                    this._renderModelTemplateSettings?.();
                });

                actions.append(editBtn, deleteBtn);
                head.appendChild(actions);
            }

            const summary = document.createElement("div");
            summary.style.cssText = "font-size:10px;color:#9ca9b5;line-height:1.45;";
            const summaryParts = MODEL_TEMPLATE_PARAM_KEYS
                .map((key) => summarizeConstraint(template.constraints?.[key], key));
            if ((template.constraints?.batchMaxFrames || 0) > 0) {
                summaryParts.push(`batch: ${template.constraints.batchMaxFrames}`);
            }
            summary.textContent = summaryParts.join(" | ");

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

        const editingTemplate = customTemplates.find((template) => template.id === formState.editId) || null;
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

        const constraintInputs = {};
        const previewRows = [];
        for (const paramKey of MODEL_TEMPLATE_PARAM_KEYS) {
            const existingConstraint = editingTemplate?.constraints?.[paramKey] || {};
            const row = document.createElement("div");
            row.style.cssText = `
                display:grid;
                grid-template-columns: 84px repeat(4, minmax(0, 1fr));
                gap: 6px;
                align-items: center;
            `;
            const header = document.createElement("div");
            header.style.cssText = "font-size:10px;color:#d9e1e8;font-weight:700;text-transform:capitalize;";
            header.textContent = paramKey;
            row.appendChild(header);
            const fields = {};
            for (const fieldKey of ["step", "offset", "min", "max"]) {
                const input = document.createElement("input");
                input.type = "number";
                input.step = paramKey === "fps" ? "0.001" : "1";
                input.value = existingConstraint[fieldKey] ?? "";
                input.placeholder = fieldKey;
                input.style.cssText = chromeInputCss({ fontSize: "10px", padding: "4px 6px", textAlign: "right" });
                fields[fieldKey] = input;
                row.appendChild(input);
            }
            constraintInputs[paramKey] = fields;
            form.appendChild(row);

            const preview = document.createElement("div");
            preview.style.cssText = "margin-left:84px;font-size:10px;color:#8f9cab;line-height:1.4;";
            form.appendChild(preview);
            previewRows.push({ key: paramKey, el: preview });
        }

        const refreshConstraintPreviews = () => {
            for (const preview of previewRows) {
                const constraint = buildConstraintDraft(preview.key, constraintInputs[preview.key]);
                const values = previewConstraintValues(constraint, 5);
                const formula = describeConstraintFormula(constraint);
                preview.el.textContent = values.length
                    ? `Formula: ${formula} | Sample: ${values.join(", ")}`
                    : `Formula: ${formula}`;
            }
        };

        for (const paramKey of MODEL_TEMPLATE_PARAM_KEYS) {
            for (const field of Object.values(constraintInputs[paramKey])) {
                field.addEventListener("input", refreshConstraintPreviews);
            }
        }
        refreshConstraintPreviews();

        const batchRow = document.createElement("div");
        batchRow.style.cssText = "display:flex;align-items:center;gap:8px;";
        const batchLabel = document.createElement("div");
        batchLabel.style.cssText = "font-size:10px;color:#92a0ad;min-width:90px;";
        batchLabel.textContent = "Batch Max Frames";
        const batchInput = document.createElement("input");
        batchInput.type = "number";
        batchInput.min = "1";
        batchInput.step = "1";
        batchInput.value = editingTemplate?.constraints?.batchMaxFrames ?? "";
        batchInput.placeholder = "97";
        batchInput.style.cssText = `${chromeInputCss({ width: "120px", fontSize: "10px", padding: "4px 6px", textAlign: "right" })}`;
        batchRow.append(batchLabel, batchInput);
        form.appendChild(batchRow);

        const batchHelp = document.createElement("div");
        batchHelp.style.cssText = "margin-left:90px;font-size:10px;color:#8f9cab;line-height:1.4;";
        batchHelp.textContent = "Default batch chunk ceiling for this template. The active frame constraint still snaps/clamps the final chunk size.";
        form.appendChild(batchHelp);

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
                constraints: {},
            };
            for (const paramKey of MODEL_TEMPLATE_PARAM_KEYS) {
                const constraint = buildConstraintDraft(paramKey, constraintInputs[paramKey]);
                if (constraint) {
                    nextTemplate.constraints[paramKey] = constraint;
                }
            }
            const batchMaxFrames = Number(batchInput.value);
            if (Number.isFinite(batchMaxFrames) && batchMaxFrames > 0) {
                nextTemplate.constraints.batchMaxFrames = Math.round(batchMaxFrames);
            }
            const nextCustomTemplates = editingTemplate
                ? customTemplates.map((template) => template.id === editingTemplate.id ? nextTemplate : template)
                : [...customTemplates, nextTemplate];
            this._templateFormState = { expanded: false, editId: "" };
            this._updateSettings({
                modelTemplates: { customTemplates: nextCustomTemplates },
            });
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
    createScaleRow(layoutSection, "Toolbar", "Toolbar & Bars", "Scene strip, toolbar, and transport controls.", () => this._settings.layout.scaleToolbar);
    createScaleRow(layoutSection, "TrackHeaders", "Track Headers", "Lane labels, icons, and left-side track controls.", () => this._settings.layout.scaleTrackHeaders);
    createScaleRow(layoutSection, "Timeline", "Timeline", "Ruler, track heights, clip blocks, and inline editors.", () => this._settings.layout.scaleTimeline);
    createScaleRow(layoutSection, "Gallery", "Asset Gallery", "Gallery lists, tabs, metadata text, and inspector chrome.", () => this._settings.layout.scaleGallery);
    const resetControls = createRow(layoutSection, "Reset Editor Layout", "Clear saved fullscreen widths, label widths, and UI scale overrides.");
    const resetBtn = document.createElement("button");
    resetBtn.textContent = "Reset";
    resetBtn.style.cssText = chromeButtonCss({ variant: "subtle", padding: "5px 12px", fontSize: "11px", radius: "6px" });
    resetBtn.addEventListener("click", () => this._resetEditorLayout());
    resetControls.appendChild(resetBtn);

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

    const notificationsSection = createSection(
        "Notifications",
        "Toast auto-dismiss timing. Hovering a toast pauses its countdown."
    );
    createNumberInput(
        notificationsSection,
        "toastDurationMs",
        "Toast Duration (ms)",
        "How long info and success toasts stay before auto-dismissing.",
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
        "Error Toast Duration (ms)",
        "How long error toasts stay before auto-dismissing. 0 keeps them until dismissed.",
        {
            min: 0,
            max: 120000,
            step: 500,
            getter: () => this._settings.notifications?.errorToastDurationMs ?? DEFAULT_EDITOR_SETTINGS.notifications.errorToastDurationMs,
            onChange: (value) => updateCategory("notifications", "errorToastDurationMs", Math.max(0, Math.round(value))),
        }
    );

    const renderSection = createSection(
        "Render",
        "Take placement, cache retention, trash cleanup, and batch render defaults."
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
        "Maximum cached render tensors to retain for this project. Older entries are evicted when the editor opens or this cap changes.",
        {
            options: RENDER_CACHE_ENTRY_PRESETS,
            min: 1,
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
        "trashRetentionDays",
        "Trash Retention Days",
        "Hard-delete trashed assets after this many days during asset refresh.",
        {
            min: 0,
            max: 36500,
            step: 1,
            getter: () => this._trashRetentionDays(),
            onChange: (value) => updateCategory("render", "trashRetentionDays", Math.max(0, Math.round(value))),
        }
    );
    createPresetNumberInput(
        renderSection,
        "trashMaxSizeMB",
        "Trash Max Size",
        "Optional decimal-MB cap for trashed asset source files. Oldest trash is purged first.",
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
    createNumberInput(
        renderSection,
        "batchRenderMaxFramesPerChunk",
        "Batch Max Frames",
        "Maximum total frames per chunk including pre/post context (the rendered tensor size cap). Snaps up to the active template's frame constraint. A value of 0 uses the active template's batch ceiling.",
        {
            min: 0,
            max: 10000,
            step: 1,
            getter: () => this._settings.batchRender.maxFramesPerChunk,
            onChange: (value) => updateCategory("batchRender", "maxFramesPerChunk", Math.max(0, Math.round(value))),
        }
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

    const promptsSection = createSection(
        "Prompts",
        "Prompt lane behavior. The first two controls are PROJECT-WIDE and change the text sent to the model; the rest are browser-local preferences."
    );
    // — Project-wide (host-owned versioned project PUTs, not settings writes).
    //   syncSettingsPanelControls only syncs settings-backed controls, so
    //   these two read host getters directly at build time.
    createCheckbox(
        promptsSection,
        "promptChannelLabels",
        "Channel Labels (project-wide)",
        "Prefix channels as [VISUAL]: / [SPEECH]: / [SOUNDS]: in composed prompt output. Saved into the project.",
        () => this._promptChannelLabels !== false,
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
        { key: "motion_driver", label: "Driver Lane Tint", description: "Optional subtle color overlay on all motion-driver lane backgrounds." },
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
        "Applied to new guide frames created from image drops.",
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
        "Default Motion-Driver Strength",
        "Applied when creating motion-driver clips.",
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

    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);
    this._settingsPanelEl = backdrop;
    syncSettingsPanelControls.call(this);

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
