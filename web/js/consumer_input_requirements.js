// Whether a node's output feeds a consumer input that is proven required.
//
// Shared by the Reference Bridges (a dead slot under `nothing`) and the Sonder
// Gate (a closed lane): both emit no value, which only an optional input can
// take. Advisories must be proven from the consumer's captured definition;
// unknown or unmodelled shapes, including subgraph instances, fail quiet.
//
// Pure: no host imports. Each extension keeps its own `beforeRegisterNodeDef`
// hook and calls `captureInputDefinition`, so the registry fills no matter
// which extension the host loads first.

const INPUT_DEFINITIONS = new Map();

const namedEntries = (value) => (
    value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : []
);

/** Reduce one /object_info node definition to the input facts this module models. */
export function distillInputDefinition(nodeData = {}) {
    const input = nodeData?.input && typeof nodeData.input === "object" ? nodeData.input : {};
    const requiredEntries = namedEntries(input.required);
    const optionalEntries = namedEntries(input.optional);
    const autogrow = [];
    for (const [, value] of [...requiredEntries, ...optionalEntries]) {
        const options = Array.isArray(value) && value[1] && typeof value[1] === "object" ? value[1] : null;
        const template = options?.template;
        if (!template || typeof template !== "object") continue;

        let templateRequired = null;
        for (const [category, entries] of namedEntries(template.input)) {
            if (!entries || typeof entries !== "object" || !Object.keys(entries).length) continue;
            templateRequired = category === "required";
            break;
        }
        // An Autogrow shape with no readable template input is unknown. Failing
        // quiet is safer than labelling a valid graph as broken.
        if (templateRequired === null) continue;

        const names = Array.isArray(template.names)
            ? template.names.map((name) => String(name))
            : null;
        const prefix = typeof template.prefix === "string" ? template.prefix : null;
        if (!names && prefix === null) continue;
        const parsedMin = Number(template.min);
        const parsedMax = Number(template.max);
        autogrow.push({
            prefix,
            names,
            min: Number.isInteger(parsedMin) && parsedMin >= 0 ? parsedMin : 0,
            max: names
                ? names.length
                : (Number.isInteger(parsedMax) && parsedMax >= 1 ? parsedMax : 0),
            required: templateRequired,
        });
    }
    return {
        required: new Set(requiredEntries.map(([name]) => name)),
        optional: new Set(optionalEntries.map(([name]) => name)),
        autogrow,
    };
}

/** Return required/optional only when the captured input shape proves it. */
export function inputRequirement(definition, inputName) {
    const name = String(inputName || "");
    if (!definition || !name) return "unknown";
    if (definition.required instanceof Set && definition.required.has(name)) return "required";
    if (definition.optional instanceof Set && definition.optional.has(name)) return "optional";
    for (const template of Array.isArray(definition.autogrow) ? definition.autogrow : []) {
        let index = -1;
        if (Array.isArray(template?.names)) {
            index = template.names.indexOf(name);
        } else if (typeof template?.prefix === "string" && name.startsWith(template.prefix)) {
            const suffix = name.slice(template.prefix.length);
            if (/^\d+$/.test(suffix)) index = Number(suffix);
            if (!Number.isInteger(index) || index < 0 || index >= Number(template.max)) index = -1;
        }
        if (index < 0) continue;
        return template.required && index < Number(template.min) ? "required" : "optional";
    }
    return "unknown";
}

/** Record one node type's input shape; call from every `beforeRegisterNodeDef`. */
export function captureInputDefinition(nodeData) {
    const name = String(nodeData?.name || "");
    if (name) INPUT_DEFINITIONS.set(name, distillInputDefinition(nodeData));
}

export function inputDefinitionFor(typeName) {
    return INPUT_DEFINITIONS.get(String(typeName || ""));
}

const nodeTypeName = (node) => String(node?.comfyClass || node?.type || "");

/**
 * Names of `node`'s outputs with at least one link into a proven-required input.
 *
 * @param getLink  (linkId) -> link record with target_id/target_slot, or null
 * @param getNode  (nodeId) -> graph node, or null
 * @param include  (outputSlot) -> whether this output is considered
 */
export function requiredConsumerOutputNames(node, { getLink, getNode, include = () => true } = {}) {
    const required = new Set();
    for (const output of node?.outputs || []) {
        const name = String(output?.name || "");
        if (!name || !include(output)) continue;
        const linkIds = Array.isArray(output?.links)
            ? output.links
            : (output?.link != null ? [output.link] : []);
        for (const linkId of linkIds) {
            const link = getLink?.(linkId);
            const target = getNode?.(link?.target_id);
            const targetInput = target?.inputs?.[Number(link?.target_slot)];
            const definition = inputDefinitionFor(nodeTypeName(target));
            if (inputRequirement(definition, targetInput?.name) === "required") {
                required.add(name);
                break;
            }
        }
    }
    return [...required];
}
