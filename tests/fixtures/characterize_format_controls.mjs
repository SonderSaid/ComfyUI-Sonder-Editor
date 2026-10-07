// Characterization of the editor toolbar's create-format logic, recorded once from the
// toolbar's own methods (copied verbatim from editor_widget.js at 2ac8da7, before they
// moved to project_format_controls.js). The recorded JSON pins the shared module to the
// toolbar's behaviour: tests/test_project_format_controls_js.py runs the shared module
// against the same inputs and compares.
//
// Usage: node characterize_format_controls.mjs <web/js URL> [shared] > out.json
// Without "shared" it runs the verbatim copies below; with it, the shared module.
const base = process.argv[2];
const useShared = process.argv[3] === 'shared';
const S = await import(base + 'editor_settings.js');
const {
    ASPECT_RATIO_PRESETS, RESOLUTION_TIERS, BUILTIN_MODEL_TEMPLATES, computeResolutionFromTier,
    detectResolutionPresetSelections, getDimensionConstraint, getRecommendedResolutions,
    getTemplateFpsValues, snapFpsToAllowed, snapResolution, snapToConstraint,
} = S;

// ── Verbatim toolbar methods (receiver state passed as arguments) ──────────────
const original = {
    aspectOptionValue(a, b) { return `${a},${b}`; },
    parseAspectValue(value) {
        const [a, b] = String(value || "").split(",").map(Number);
        return { a: Number.isFinite(a) ? a : 0, b: Number.isFinite(b) ? b : 0 };
    },
    matchesAspectRatio(width, height, a, b, tolerance = 0.01) {
        if (!(width > 0 && height > 0 && a > 0 && b > 0)) return false;
        const actualRatio = width / height;
        const targetRatio = a / b;
        return Math.abs(actualRatio - targetRatio) / targetRatio <= tolerance;
    },
    findMatchingAspectPreset(width, height, template, tolerance = 0.03) {
        return detectResolutionPresetSelections(width, height, template, { aspectTolerance: tolerance })?.aspectPreset
            || ASPECT_RATIO_PRESETS.find(
                (preset) => preset.a > 0 && preset.b > 0 && original.matchesAspectRatio(width, height, preset.a, preset.b, tolerance)
            )
            || null;
    },
    findNearestResolutionTier(width, height, template, tolerance = 0.10) {
        const snappedDetection = detectResolutionPresetSelections(width, height, template, { tierTolerance: tolerance });
        if (snappedDetection?.tier) return snappedDetection.tier;
        if (!(width > 0 && height > 0)) return null;
        const c = Math.sqrt(width * height);
        let bestTier = null;
        let bestDiff = Infinity;
        for (const tier of RESOLUTION_TIERS) {
            const diff = Math.abs(tier.c - c) / tier.c;
            if (diff < bestDiff) { bestTier = tier; bestDiff = diff; }
        }
        return bestDiff <= tolerance ? bestTier : null;
    },
    templateResolutionTiers(template) {
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
    },
    recommendedTierValue(template) {
        const recommended = getRecommendedResolutions(template);
        if (!recommended.length) return null;
        const [w, h] = recommended[0];
        const c = Math.sqrt(w * h);
        const TOL = 0.05;
        let nearest = null;
        let nearestDiff = Infinity;
        for (const tier of RESOLUTION_TIERS) {
            const diff = Math.abs(tier.c - c);
            if (diff < nearestDiff) { nearest = tier.c; nearestDiff = diff; }
        }
        if (nearest != null && nearestDiff / nearest <= TOL) return nearest;
        return Math.round(c);
    },
    resolutionControlMode({ a, b, tier }) {
        const hasAspectPreset = a > 0 && b > 0;
        const hasResolutionTier = !!tier;
        if (hasAspectPreset && hasResolutionTier) return "locked";
        if (hasAspectPreset) return "aspect-custom";
        if (hasResolutionTier) return "tier-custom-aspect";
        return "free-custom";
    },
    resolveAspectCustomResolution({ a, b, width, height, axis, template }) {
        if (!(a > 0 && b > 0)) return null;
        if (axis === "h" && height > 0) {
            if (template.id !== "free") height = Math.round(snapToConstraint(height, getDimensionConstraint(template)));
            width = Math.max(1, Math.round(height * a / b));
            if (template.id !== "free") width = Math.round(snapToConstraint(width, getDimensionConstraint(template)));
        } else if (width > 0) {
            if (template.id !== "free") width = Math.round(snapToConstraint(width, getDimensionConstraint(template)));
            height = Math.max(1, Math.round(width * b / a));
            if (template.id !== "free") height = Math.round(snapToConstraint(height, getDimensionConstraint(template)));
        } else if (height > 0) {
            if (template.id !== "free") height = Math.round(snapToConstraint(height, getDimensionConstraint(template)));
            width = Math.max(1, Math.round(height * a / b));
            if (template.id !== "free") width = Math.round(snapToConstraint(width, getDimensionConstraint(template)));
        } else {
            return null;
        }
        return template.id === "free" ? { width, height } : snapResolution(width, height, template);
    },
    snapFpsToTemplate(fps, template) {
        const numeric = Math.max(0, Number(fps) || 0);
        const values = getTemplateFpsValues(template);
        if (template?.id === "free" || !values.length) return numeric;
        return Number(snapFpsToAllowed(numeric, values).toFixed(3));
    },
    fpsChoices(template) {
        const values = getTemplateFpsValues(template);
        return { constrained: template.id !== "free" && values.length > 0, values };
    },
    templateChangeResolution(width, height, template) {
        return template.id === "free" ? { width, height } : snapResolution(width, height, template);
    },
};

const F = useShared ? await import(base + 'project_format_controls.js') : original;
const SIZES = [[1280, 720], [832, 480], [1024, 1024], [720, 1280], [1360, 768], [1000, 701], [1920, 1080], [640, 360], [0, 0]];
const out = {};
for (const template of BUILTIN_MODEL_TEMPLATES) {
    const tiers = F.templateResolutionTiers(template);
    const aspects = ASPECT_RATIO_PRESETS.filter(p => p.a > 0 && p.b > 0);
    out[template.id] = {
        tiers,
        recommended: F.recommendedTierValue(template),
        locked: aspects.flatMap(p => tiers.map(t => [p.label, t.value, computeResolutionFromTier(Number(t.value), p.a, p.b, template)])),
        aspectCustom: aspects.flatMap(p => [[500, 0, 'w'], [777, 0, 'w'], [1280, 721, 'w'], [0, 333, 'h'], [1000, 720, 'h'], [0, 0, 'w']]
            .map(([width, height, axis]) => [p.label, width, height, axis, F.resolveAspectCustomResolution({ a: p.a, b: p.b, width, height, axis, template })])),
        detect: SIZES.map(([w, h]) => [w, h, F.findMatchingAspectPreset(w, h, template)?.label ?? null, F.findNearestResolutionTier(w, h, template)?.c ?? null]),
        templateChange: SIZES.map(([w, h]) => F.templateChangeResolution(w, h, template)),
        fps: { ...F.fpsChoices(template), snapped: [0, 12, 23.976, 24, 25, 29.97, 30, 60].map(v => F.snapFpsToTemplate(v, template)) },
    };
}
out.modes = [[16, 9, 960], [16, 9, null], [0, 0, 960], [0, 0, null]].map(([a, b, tier]) => F.resolutionControlMode({ a, b, tier }));
out.aspectValues = [F.aspectOptionValue(16, 9), F.parseAspectValue('4,3'), F.parseAspectValue('x'), F.parseAspectValue('')];
console.log(JSON.stringify(out));
