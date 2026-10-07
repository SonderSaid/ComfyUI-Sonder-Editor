// A project's format: model template, channels, aspect ratio, resolution and frame
// rate. The pure rules are shared by the editor toolbar (which edits an open scene) and
// the Launcher's New project dialog (which picks them before a project exists), so the
// two cannot drift; tests/fixtures/project_format_controls_characterization.json pins
// them to the toolbar's behaviour before they moved here. No import-time DOM.

import {
    ASPECT_RATIO_PRESETS,
    RESOLUTION_TIERS,
    computeResolutionFromTier,
    detectResolutionPresetSelections,
    getAllModelTemplates,
    getAllPromptChannelTemplates,
    getDimensionConstraint,
    getMaxRes,
    getRecommendedResolutions,
    getTemplateById,
    getTemplateFpsValues,
    snapFpsToAllowed,
    snapResolution,
    snapToConstraint,
} from "./editor_settings.js";
import { THEME, TYPE, SPACE, chromeInputCss, chromeSelectCss } from "./editor_theme.js";

export function aspectOptionValue(a, b) {
    return `${a},${b}`;
}

export function parseAspectValue(value) {
    const [a, b] = String(value || "").split(",").map(Number);
    return {
        a: Number.isFinite(a) ? a : 0,
        b: Number.isFinite(b) ? b : 0,
    };
}

export function matchesAspectRatio(width, height, a, b, tolerance = 0.01) {
    if (!(width > 0 && height > 0 && a > 0 && b > 0)) return false;
    const actualRatio = width / height;
    const targetRatio = a / b;
    return Math.abs(actualRatio - targetRatio) / targetRatio <= tolerance;
}

export function findMatchingAspectPreset(width, height, template, tolerance = 0.03) {
    return detectResolutionPresetSelections(width, height, template, { aspectTolerance: tolerance })?.aspectPreset
        || ASPECT_RATIO_PRESETS.find(
            (preset) => preset.a > 0 && preset.b > 0 && matchesAspectRatio(width, height, preset.a, preset.b, tolerance)
        )
        || null;
}

export function findNearestResolutionTier(width, height, template, tolerance = 0.10) {
    const snappedDetection = detectResolutionPresetSelections(width, height, template, { tierTolerance: tolerance });
    if (snappedDetection?.tier) return snappedDetection.tier;
    if (!(width > 0 && height > 0)) return null;
    const c = Math.sqrt(width * height);
    let bestTier = null;
    let bestDiff = Infinity;
    for (const tier of RESOLUTION_TIERS) {
        const diff = Math.abs(tier.c - c) / tier.c;
        if (diff < bestDiff) {
            bestTier = tier;
            bestDiff = diff;
        }
    }
    return bestDiff <= tolerance ? bestTier : null;
}

// Resolution tiers offered for a template: the global RESOLUTION_TIERS plus a
// template-specific option for any recommended resolution that NO standard tier
// approximates (within 5% on the total-pixel / geometric-mean basis). This is how
// an unconventional native res (e.g. CogVideoX 1360×768) becomes directly selectable.
export function templateResolutionTiers(template) {
    const TOL = 0.05;
    const tiers = RESOLUTION_TIERS.map((tier) => ({ value: String(tier.c), label: tier.label, c: tier.c }));
    for (const [w, h] of getRecommendedResolutions(template)) {
        const c = Math.sqrt(w * h);
        if (RESOLUTION_TIERS.some((tier) => Math.abs(tier.c - c) / tier.c <= TOL)) continue;
        const value = String(Math.round(c));
        if (tiers.some((tier) => tier.value === value)) continue;
        tiers.push({ value, label: `${w}×${h}`, c });
    }
    return tiers;
}

// Tier value (a number matching a tier's option value) to mark with the ★. Stars
// the exact template-specific tier when the primary recommended res has no near
// standard tier; otherwise the nearest standard tier.
export function recommendedTierValue(template) {
    const recommended = getRecommendedResolutions(template);
    if (!recommended.length) return null;
    const [w, h] = recommended[0];
    const c = Math.sqrt(w * h);
    const TOL = 0.05;
    let nearest = null;
    let nearestDiff = Infinity;
    for (const tier of RESOLUTION_TIERS) {
        const diff = Math.abs(tier.c - c);
        if (diff < nearestDiff) {
            nearest = tier.c;
            nearestDiff = diff;
        }
    }
    if (nearest != null && nearestDiff / nearest <= TOL) return nearest;
    return Math.round(c);
}

// Which inputs drive the size: an aspect preset and a tier lock it; one of them leaves
// the other side typed; neither is free entry.
export function resolutionControlMode({ a, b, tier }) {
    const hasAspectPreset = a > 0 && b > 0;
    const hasResolutionTier = !!tier;
    if (hasAspectPreset && hasResolutionTier) return "locked";
    if (hasAspectPreset) return "aspect-custom";
    if (hasResolutionTier) return "tier-custom-aspect";
    return "free-custom";
}

// An aspect preset with a typed side: the other side follows, both snapped to the template.
export function resolveAspectCustomResolution({ a, b, width, height, axis = "w", template }) {
    if (!(a > 0 && b > 0)) return null;
    const snap = (value) => Math.round(snapToConstraint(value, getDimensionConstraint(template)));
    if (axis === "h" && height > 0) {
        if (template.id !== "free") height = snap(height);
        width = Math.max(1, Math.round(height * a / b));
        if (template.id !== "free") width = snap(width);
    } else if (width > 0) {
        if (template.id !== "free") width = snap(width);
        height = Math.max(1, Math.round(width * b / a));
        if (template.id !== "free") height = snap(height);
    } else if (height > 0) {
        if (template.id !== "free") height = snap(height);
        width = Math.max(1, Math.round(height * a / b));
        if (template.id !== "free") width = snap(width);
    } else {
        return null;
    }
    return template.id === "free" ? { width, height } : snapResolution(width, height, template);
}

// A free-entry size after the template changes.
export function templateChangeResolution(width, height, template) {
    return template.id === "free" ? { width, height } : snapResolution(width, height, template);
}

export function snapFpsToTemplate(fps, template) {
    const numeric = Math.max(0, Number(fps) || 0);
    const values = getTemplateFpsValues(template);
    if (template?.id === "free" || !values.length) {
        return numeric;
    }
    return Number(snapFpsToAllowed(numeric, values).toFixed(3));
}

// fps is a HARD allow-list: constrained templates offer a discrete choice (a single
// value is effectively locked); `free` keeps free entry.
export function fpsChoices(template) {
    const values = getTemplateFpsValues(template);
    return { constrained: template.id !== "free" && values.length > 0, values };
}

// Soft warning: above the template's recommended pixel budget (5% tolerance for one
// divisibility-snap step). Never clamps.
export function aboveRecommendedMax(width, height, template) {
    const maxRes = getMaxRes(template);
    if (template.id === "free" || !maxRes || !(width > 0 && height > 0)) return null;
    return width * height > maxRes[0] * maxRes[1] * 1.05 ? maxRes : null;
}

// ── New-project controls ───────────────────────────────────────────────────────

function field(label, control) {
    const row = document.createElement("label");
    row.style.cssText = `display:grid;grid-template-columns:120px minmax(0,1fr);align-items:center;gap:${SPACE.s3}px;color:${THEME.fg1};font-size:${TYPE.t12}px`;
    row.append(document.createTextNode(label), control);
    return row;
}

function select(options, value, ariaLabel) {
    const node = document.createElement("select");
    node.style.cssText = chromeSelectCss({ fontSize: `${TYPE.t12}px`, padding: "6px 8px" });
    node.setAttribute("aria-label", ariaLabel);
    for (const [optionValue, text] of options) {
        const option = document.createElement("option");
        option.value = String(optionValue);
        option.textContent = text;
        node.append(option);
    }
    node.value = String(value);
    return node;
}

function numberInput(value, ariaLabel, width = "84px") {
    const node = document.createElement("input");
    node.type = "number";
    node.min = "1";
    node.style.cssText = `${chromeInputCss({ fontSize: `${TYPE.t12}px`, padding: "6px 8px" })}width:${width}`;
    node.setAttribute("aria-label", ariaLabel);
    node.value = String(value || "");
    return node;
}

/**
 * Model template, Channels, Aspect, Resolution and fps for a project that does not exist
 * yet, using the toolbar's rules. Owns its DOM and listeners; returns
 * `{ element, read, cleanup }`, where `read()` gives
 * `{ templateId, channelTemplateId, width, height, fps }`.
 */
export function mountProjectFormatControls({ settings, initial = {}, onChange = () => {} }) {
    const root = document.createElement("div");
    root.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s3}px`;
    const listeners = [];
    const on = (node, type, handler) => { node.addEventListener(type, handler); listeners.push(() => node.removeEventListener(type, handler)); };

    let template = getTemplateById(initial.templateId, settings);
    let width = Math.max(0, Math.round(Number(initial.width) || 0));
    let height = Math.max(0, Math.round(Number(initial.height) || 0));
    if (template.id !== "free") ({ width, height } = snapResolution(width, height, template));
    let fps = snapFpsToTemplate(initial.fps, template);

    const templateSelect = select(getAllModelTemplates(settings).map((t) => [t.id, t.name]), template.id, "Model template");
    const channels = getAllPromptChannelTemplates(settings);
    const channelSelect = select(channels.map((t) => [t.id, t.name]),
        channels.some((t) => t.id === initial.channelTemplateId) ? initial.channelTemplateId : channels[0]?.id || "", "Channels");
    const aspectSelect = select(ASPECT_RATIO_PRESETS.map((p) => [aspectOptionValue(p.a, p.b), p.label]), aspectOptionValue(0, 0), "Aspect ratio");
    const tierSelect = select([], "custom", "Resolution");
    const widthInput = numberInput(width, "Width");
    const heightInput = numberInput(height, "Height");
    const fpsSelect = select([], "", "Frame rate");
    const fpsInput = numberInput(fps, "Frame rate", "84px");
    fpsInput.step = "0.001";
    const hint = document.createElement("div");
    hint.style.cssText = `color:${THEME.fg2};font-size:${TYPE.t11}px;min-height:1em`;

    const size = document.createElement("div");
    size.style.cssText = `display:flex;align-items:center;gap:${SPACE.s2}px;min-width:0;flex-wrap:wrap`;
    const times = document.createElement("span");
    times.textContent = "×";
    size.append(tierSelect, widthInput, times, heightInput);
    const rate = document.createElement("div");
    rate.style.cssText = `display:flex;gap:${SPACE.s2}px`;
    rate.append(fpsSelect, fpsInput);

    const detected = detectResolutionPresetSelections(width, height, template);
    const preset = findMatchingAspectPreset(width, height, template);
    aspectSelect.value = preset ? aspectOptionValue(preset.a, preset.b) : aspectOptionValue(0, 0);

    function rebuildTiers(selected) {
        const recommended = recommendedTierValue(template);
        tierSelect.replaceChildren();
        for (const tier of templateResolutionTiers(template)) {
            const option = document.createElement("option");
            option.value = tier.value;
            option.textContent = tier.label + (String(recommended) === tier.value ? " ★" : "");
            tierSelect.append(option);
        }
        const custom = document.createElement("option");
        custom.value = "custom";
        custom.textContent = "Custom";
        tierSelect.append(custom);
        tierSelect.value = Array.from(tierSelect.options).some((o) => o.value === String(selected)) ? String(selected) : "custom";
    }
    function rebuildFps() {
        const { constrained, values } = fpsChoices(template);
        fps = snapFpsToTemplate(fps, template);
        fpsSelect.replaceChildren(...values.map((value) => {
            const option = document.createElement("option");
            option.value = String(value);
            option.textContent = `${value} fps`;
            return option;
        }));
        fpsSelect.hidden = !constrained;
        fpsInput.hidden = constrained;
        if (constrained) fpsSelect.value = String(fps);
        else fpsInput.value = String(fps || "");
    }
    const tier = () => { const value = Number(tierSelect.value); return tierSelect.value !== "custom" && value > 0 ? value : null; };
    const mode = () => resolutionControlMode({ ...parseAspectValue(aspectSelect.value), tier: tier() });
    function show() {
        widthInput.value = String(width || "");
        heightInput.value = String(height || "");
        const locked = mode() === "locked";
        for (const input of [widthInput, heightInput]) { input.readOnly = locked; input.style.opacity = locked ? "0.68" : "1"; }
        const max = aboveRecommendedMax(width, height, template);
        hint.textContent = max ? `Above ${template.name}'s recommended maximum (${max[0]}×${max[1]}).` : "";
        onChange(read());
    }
    function recalculate(axis = "w") {
        const { a, b } = parseAspectValue(aspectSelect.value);
        const current = mode();
        let next = null;
        if (current === "locked") next = computeResolutionFromTier(tier(), a, b, template);
        else if (current === "aspect-custom") next = resolveAspectCustomResolution({ a, b, width, height, axis, template });
        else if (current === "tier-custom-aspect" && width > 0 && height > 0) next = computeResolutionFromTier(tier(), width, height, template);
        else next = templateChangeResolution(width, height, template);
        if (next) ({ width, height } = next);
        show();
    }
    function read() {
        return { templateId: template.id, channelTemplateId: channelSelect.value, width, height, fps };
    }

    rebuildTiers(detected?.tierValue || "custom");
    rebuildFps();
    on(templateSelect, "change", () => {
        template = getTemplateById(templateSelect.value, settings);
        rebuildTiers(tierSelect.value);
        rebuildFps();
        recalculate();
    });
    on(channelSelect, "change", () => onChange(read()));
    on(aspectSelect, "change", () => recalculate());
    on(tierSelect, "change", () => recalculate());
    for (const [input, axis] of [[widthInput, "w"], [heightInput, "h"]]) {
        on(input, "change", () => {
            const value = Math.max(0, Math.round(Number(input.value) || 0));
            if (axis === "w") width = value; else height = value;
            recalculate(axis);
        });
    }
    on(fpsSelect, "change", () => { fps = Number(fpsSelect.value) || fps; onChange(read()); });
    on(fpsInput, "change", () => { fps = Math.max(0, Number(fpsInput.value) || 0); onChange(read()); });

    root.append(
        field("Model template", templateSelect),
        field("Channels", channelSelect),
        field("Aspect ratio", aspectSelect),
        field("Resolution", size),
        field("Frame rate", rate),
        hint,
    );
    show();
    return {
        element: root,
        read,
        cleanup() { for (const off of listeners) off(); root.remove(); },
    };
}
