// Pure frontend projections of the server-owned Prompt Format declaration.
// This module never invents provider vocabulary; it only selects and orders
// fields already present in the schema-v1 catalog or a compiled candidate.

export function promptProfileKey(profile = {}) {
    const id = String(profile?.profile_id || "");
    const version = String(profile?.version || "1");
    return id ? `${id}@${version}` : "";
}

export function resolvedPromptProfile({ profileId = "", candidate = null,
    catalog = null, customProfiles = [] } = {}) {
    const key = String(profileId || "");
    const candidateProfile = candidate?.profile;
    const descriptor = (catalog?.profiles || []).find((value) =>
        String(value?.key || "") === key);
    if (descriptor?.resolved && typeof descriptor.resolved === "object") {
        return descriptor.resolved;
    }
    if (candidateProfile && (!key || promptProfileKey(candidateProfile) === key)) {
        return candidateProfile;
    }
    return (customProfiles || []).find((value) => promptProfileKey(value) === key) || {};
}

export function referenceDerivedDeclarations(profile = {}) {
    const derived = profile?.capabilities?.reference?.derived;
    return derived && typeof derived === "object" && !Array.isArray(derived)
        ? derived : {};
}

export function orderedReferenceDerived(profile = {}) {
    return Object.entries(referenceDerivedDeclarations(profile))
        .filter(([, declaration]) => declaration && typeof declaration === "object")
        .sort(([leftKind, left], [rightKind, right]) =>
            Number(left?.order || 0) - Number(right?.order || 0)
            || leftKind.localeCompare(rightKind));
}

export function declaredFieldChoices(field = {}) {
    const result = [];
    const seen = new Set();
    for (const entry of Array.isArray(field?.values) ? field.values : []) {
        const value = typeof entry === "string"
            ? entry : String(entry?.value || "");
        if (!value || seen.has(value)) continue;
        seen.add(value);
        result.push({
            value,
            label: typeof entry === "string" ? entry : String(entry?.label || value),
            description: typeof entry === "string"
                ? "" : String(entry?.description || ""),
        });
    }
    return result;
}

export function referenceFieldDeclaration(profile, capabilityKind, fieldName) {
    const fields = referenceDerivedDeclarations(profile)?.[capabilityKind]?.fields;
    const field = fields && typeof fields === "object" ? fields[fieldName] : null;
    return field && typeof field === "object" ? field : null;
}

export function referenceCapabilityChoicesForProfiles(catalog, profileIds = []) {
    const wanted = new Set((profileIds || []).map(String).filter(Boolean));
    if (!wanted.size) wanted.add("generic@1");
    const values = new Map();
    for (const descriptor of catalog?.profiles || []) {
        if (!wanted.has(String(descriptor?.key || ""))) continue;
        for (const [kind, declaration] of orderedReferenceDerived(
            descriptor?.resolved || {})) {
            if (!values.has(kind)) {
                values.set(kind, {
                    value: kind,
                    label: String(declaration?.label || kind),
                });
            }
        }
    }
    return [...values.values()];
}

export function referenceRoleChoices(profile = {}, population = "") {
    const catalog = profile?.role_catalogs?.[String(population || "")];
    const result = [];
    const seen = new Set();
    for (const entry of Array.isArray(catalog) ? catalog : []) {
        const value = typeof entry === "string"
            ? entry : String(entry?.value || "");
        if (!value || seen.has(value)) continue;
        seen.add(value);
        result.push({
            value,
            label: typeof entry === "string" ? entry : String(entry?.label || value),
        });
    }
    return result;
}
