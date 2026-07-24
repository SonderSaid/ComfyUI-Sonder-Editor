const PROJECT_TYPE = "SONDER_PROJECT";
const DEFAULT_MAX_HOPS = 64;
const diagnosticKeys = new Set();
const graphKeys = new WeakMap();
let nextGraphKey = 1;

const nodeType = (node) => String(node?.comfyClass || node?.type || "");
const isEditorNode = (node) => nodeType(node) === "SonderEditor";
const isKJGetNode = (node) => nodeType(node) === "GetNode";
const isKJSetNode = (node) => nodeType(node) === "SetNode";
const isProjectType = (value) => String(value || "").trim().toUpperCase() === PROJECT_TYPE;

function graphKey(graph) {
    if (!graph || (typeof graph !== "object" && typeof graph !== "function")) return "no-graph";
    if (!graphKeys.has(graph)) graphKeys.set(graph, nextGraphKey++);
    return graphKeys.get(graph);
}

function result(status, {
    editor = null,
    via = [],
    reason = "",
    candidates = [],
} = {}) {
    return {
        status,
        editor,
        via,
        route: via.join(" -> "),
        reason,
        candidates,
    };
}

function graphLinks(graph) {
    return graph?.links || graph?._links || null;
}

function keyedValue(container, key) {
    if (!container || key == null) return null;
    if (typeof container.get === "function") {
        return container.get(key)
            ?? container.get(String(key))
            ?? (Number.isFinite(Number(key)) ? container.get(Number(key)) : null)
            ?? null;
    }
    return container[key]
        ?? container[String(key)]
        ?? (Number.isFinite(Number(key)) ? container[Number(key)] : null)
        ?? null;
}

export function getGraphLink(graph, linkId) {
    if (!graph || linkId == null) return null;
    if (typeof graph.getLink === "function") {
        const direct = graph.getLink(linkId)
            ?? graph.getLink(String(linkId))
            ?? (Number.isFinite(Number(linkId)) ? graph.getLink(Number(linkId)) : null);
        if (direct) return direct;
    }
    return keyedValue(graphLinks(graph), linkId);
}

export function getGraphNode(graph, nodeId) {
    if (!graph || nodeId == null) return null;
    if (typeof graph.getNodeById === "function") {
        const direct = graph.getNodeById(nodeId)
            ?? graph.getNodeById(String(nodeId))
            ?? (Number.isFinite(Number(nodeId)) ? graph.getNodeById(Number(nodeId)) : null);
        if (direct) return direct;
    }
    const indexed = keyedValue(graph._nodes_by_id || graph.nodesById, nodeId);
    if (indexed) return indexed;
    return (graph._nodes || graph.nodes || []).find((node) => String(node?.id) === String(nodeId)) || null;
}

function inputLinkId(input) {
    return input?.link ?? input?.linkId ?? null;
}

function physicalInputLink(node, inputIndex, graph) {
    const input = node?.inputs?.[inputIndex];
    const linkId = inputLinkId(input);
    if (linkId != null) return getGraphLink(graph, linkId);

    // Nodes 2.0 may keep the link on the graph rather than the legacy slot.
    if (!node?.isVirtualNode && typeof node?.getInputLink === "function") {
        try {
            return node.getInputLink(inputIndex) || null;
        } catch (_) {
            return null;
        }
    }
    if (typeof graph?.getInputLink === "function") {
        try {
            return graph.getInputLink(node, inputIndex) || null;
        } catch (_) {
            return null;
        }
    }
    return null;
}

function hostResultValues(value) {
    if (value == null || typeof value?.then === "function") return [];
    return Array.isArray(value) ? value : [value];
}

function hostEndpoint(value, fallbackGraph) {
    if (!value || typeof value !== "object") return null;
    if (value.origin_id != null) {
        const graph = value.graph || fallbackGraph;
        return {
            node: getGraphNode(graph, value.origin_id),
            slot: value.origin_slot ?? 0,
            graph,
            link: value,
        };
    }
    const wrappedNode = value.node?.node
        || value.node
        || value.outputNode
        // LiteGraph link.resolve() names the downstream endpoint inputNode,
        // so only treat inputNode as a source for other host result shapes.
        || (!value.link ? value.inputNode : null)
        || (value.id != null ? value : null);
    if (wrappedNode && (wrappedNode.id != null || isEditorNode(wrappedNode))) {
        return {
            node: wrappedNode,
            slot: value.origin_slot ?? value.slot ?? 0,
            graph: wrappedNode.graph || value.graph || fallbackGraph,
            link: null,
        };
    }
    return null;
}

function callHostResolvers(node, slot, graph) {
    const values = [];
    const calls = [
        [node, node?.resolveVirtualOutput, [slot]],
        [node, node?.resolveInput, [slot]],
        [graph, graph?.resolveInput, [node, slot]],
    ];
    for (const [owner, method, args] of calls) {
        if (typeof method !== "function") continue;
        try {
            values.push(...hostResultValues(method.apply(owner, args)));
        } catch (_) {
            // Host helpers vary between frontend generations. A legacy fallback
            // remains available and owns the diagnostic if it also cannot resolve.
        }
    }
    return values;
}

function callHostInputResolvers(node, inputIndex, graph, link) {
    const values = [];
    const input = node?.inputs?.[inputIndex];
    const calls = [
        [node, node?.resolveInput, [inputIndex]],
        [input, input?.resolveInput, [graph, node]],
        [graph, graph?.resolveInput, [node, inputIndex]],
        [link, link?.resolve, [graph]],
    ];
    for (const [owner, method, args] of calls) {
        if (typeof method !== "function") continue;
        try {
            values.push(...hostResultValues(method.apply(owner, args)));
        } catch (_) {
            // A graph-local legacy trace remains the compatibility fallback.
        }
    }
    return values;
}

function kjName(node) {
    const named = (node?.widgets || []).find((widget) =>
        ["constant", "name"].includes(String(widget?.name || "").trim().toLowerCase())
    );
    return String(named?.value ?? node?.widgets?.[0]?.value ?? node?.properties?.name ?? "").trim();
}

function graphNodes(graph) {
    const nodes = graph?._nodes || graph?.nodes;
    if (Array.isArray(nodes)) return nodes;
    if (nodes && typeof nodes.values === "function") return [...nodes.values()];
    const indexed = graph?._nodes_by_id || graph?.nodesById;
    if (indexed && typeof indexed.values === "function") return [...indexed.values()];
    return indexed && typeof indexed === "object" ? Object.values(indexed) : [];
}

function parentGraph(graph) {
    const candidates = [
        graph?.parentGraph,
        graph?._parentGraph,
        graph?._subgraph_node?.graph,
        graph?._subgraphNode?.graph,
        graph?.subgraphNode?.graph,
        graph?._ownerNode?.graph,
        graph?.ownerNode?.graph,
    ];
    return candidates.find((candidate) => candidate && candidate !== graph) || null;
}

function graphScopes(graph, maxHops) {
    const scopes = [];
    const seen = new Set();
    let current = graph;
    while (current && !seen.has(current) && scopes.length < maxHops) {
        scopes.push(current);
        seen.add(current);
        current = parentGraph(current);
    }
    return scopes;
}

function projectPassThroughInput(node, outputSlot, outgoingLink, graph) {
    const output = node?.outputs?.[outputSlot];
    if (!isProjectType(output?.type) && !isProjectType(outgoingLink?.type)) return null;

    const candidates = [];
    for (let index = 0; index < (node?.inputs || []).length; index += 1) {
        const input = node.inputs[index];
        const link = physicalInputLink(node, index, graph);
        if (!link) continue;
        if (isProjectType(input?.type) || isProjectType(link?.type)) {
            candidates.push({ index, link });
        }
    }
    return {
        input: candidates.length === 1 ? candidates[0] : null,
        candidateCount: candidates.length,
    };
}

function combineHostResults(results, via) {
    const resolved = results.filter((entry) => entry.status === "resolved");
    const byEditor = new Map();
    for (const entry of resolved) {
        const key = entry.editor?.id != null ? String(entry.editor.id) : entry.editor;
        byEditor.set(key, entry);
    }
    if (byEditor.size === 1) return [...byEditor.values()][0];
    if (byEditor.size > 1) {
        return result("ambiguous", {
            via,
            reason: "Host resolution returned multiple Sonder Editor candidates.",
            candidates: [...byEditor.values()].map((entry) => entry.editor),
        });
    }
    return results.find((entry) => entry.status === "ambiguous")
        || results.find((entry) => entry.status === "error")
        || null;
}

function traceEndpoint(node, outputSlot, graph, outgoingLink, context) {
    if (!node) {
        return result("error", {
            via: context.via,
            reason: "A project link refers to a node that is not present in its graph.",
        });
    }
    if (context.hops >= context.maxHops) {
        return result("error", {
            via: context.via,
            reason: `Project resolution exceeded ${context.maxHops} hops.`,
        });
    }

    const key = `${graphKey(graph)}:${String(node.id)}:${String(outputSlot)}`;
    if (context.visited.has(key)) {
        return result("error", {
            via: context.via,
            reason: "Cycle detected while resolving the project source.",
        });
    }
    const nextContext = {
        ...context,
        hops: context.hops + 1,
        visited: new Set(context.visited).add(key),
    };

    if (isEditorNode(node)) {
        const output = node?.outputs?.[outputSlot];
        if (!isProjectType(output?.type) && !isProjectType(outgoingLink?.type)) {
            return result("unsupported", {
                via: context.via,
                reason: "The linked Sonder Editor output is not a SONDER_PROJECT output.",
            });
        }
        const via = [...context.via, `editor:${String(node.id)}`];
        return result("resolved", {
            editor: node,
            via,
            reason: "Resolved a single Sonder Editor project source.",
        });
    }

    if (isKJGetNode(node)) {
        const name = kjName(node);
        if (!name) {
            return result("ambiguous", {
                via: [...context.via, "kj-fallback"],
                reason: "KJ Get has no selected setter name.",
            });
        }
        let setters = [];
        let setterGraph = null;
        for (const scope of graphScopes(graph, context.maxHops)) {
            const matches = graphNodes(scope).filter((candidate) =>
                isKJSetNode(candidate) && kjName(candidate) === name
            );
            if (matches.length) {
                setters = matches;
                setterGraph = scope;
                break;
            }
        }
        if (setters.length !== 1) {
            return result("ambiguous", {
                via: [...context.via, "kj-fallback"],
                reason: setters.length
                    ? `KJ Get "${name}" matches multiple setters in the same scope.`
                    : `KJ Get "${name}" has no setter in its graph or ancestor scope.`,
                candidates: setters,
            });
        }

        // Current KJNodes exposes virtual-output resolution to the host. Check
        // setter cardinality first because its legacy same-graph helper picks
        // the first matching Set node when a malformed workflow has duplicates.
        const hostValues = callHostResolvers(node, outputSlot, graph);
        if (hostValues.length) {
            const hostResults = hostValues
                .map((value) => hostEndpoint(value, graph))
                .filter((endpoint) => endpoint?.node && endpoint.node !== node)
                .map((endpoint) => traceEndpoint(
                    endpoint.node,
                    endpoint.slot,
                    endpoint.graph,
                    endpoint.link,
                    { ...nextContext, via: [...context.via, "kj-host"] },
                ));
            const combined = combineHostResults(hostResults, [...context.via, "kj-host"]);
            if (combined) return combined;
        }

        const setter = setters[0];
        const linkedInputs = (setter.inputs || [])
            .map((input, index) => ({ input, index, link: physicalInputLink(setter, index, setterGraph) }))
            .filter((entry) => entry.link && (
                isProjectType(entry.input?.type)
                || isProjectType(entry.link?.type)
                || (setter.inputs || []).length === 1
            ));
        if (linkedInputs.length !== 1) {
            return result("ambiguous", {
                via: [...context.via, "kj-fallback"],
                reason: `KJ Set "${name}" does not have one unambiguous project input.`,
            });
        }
        return traceLink(linkedInputs[0].link, setterGraph, {
            ...nextContext,
            via: [...context.via, `kj:${name}`],
        });
    }

    const passThrough = projectPassThroughInput(node, outputSlot, outgoingLink, graph);
    if (passThrough?.candidateCount > 1) {
        return result("ambiguous", {
            via: context.via,
            reason: `${nodeType(node) || "Pass-through node"} has multiple ${PROJECT_TYPE} inputs.`,
        });
    }
    if (passThrough?.input) {
        return traceLink(passThrough.input.link, graph, {
            ...nextContext,
            via: [...context.via, `pass-through:${nodeType(node) || node.id}`],
        });
    }

    const hostValues = callHostResolvers(node, outputSlot, graph);
    if (hostValues.length) {
        const hostResults = hostValues
            .map((value) => hostEndpoint(value, graph))
            .filter((endpoint) => endpoint?.node && endpoint.node !== node)
            .map((endpoint) => traceEndpoint(
                endpoint.node,
                endpoint.slot,
                endpoint.graph,
                endpoint.link,
                { ...nextContext, via: [...context.via, "host-resolveInput"] },
            ));
        const combined = combineHostResults(hostResults, [...context.via, "host-resolveInput"]);
        if (combined) return combined;
    }

    return result("unsupported", {
        via: context.via,
        reason: `${nodeType(node) || "Upstream node"} is not an unambiguous ${PROJECT_TYPE} pass-through.`,
    });
}

function traceLink(link, graph, context) {
    if (!link) {
        return result("error", {
            via: context.via,
            reason: "The project input references a missing graph link.",
        });
    }
    const originId = link.origin_id ?? link.originId;
    const originSlot = link.origin_slot ?? link.originSlot ?? 0;
    return traceEndpoint(
        getGraphNode(graph, originId),
        originSlot,
        graph,
        link,
        { ...context, via: [...context.via, `physical:${String(originId)}`] },
    );
}

export function resolveProjectSource(node, {
    inputName = "project",
    graph = node?.graph || null,
    maxHops = DEFAULT_MAX_HOPS,
} = {}) {
    try {
        if (!node) {
            return result("error", { reason: "Cannot resolve a project source without a node." });
        }
        const inputIndex = (node.inputs || []).findIndex((input) => input?.name === inputName);
        if (inputIndex < 0) {
            return result("unsupported", {
                reason: `Node has no named "${inputName}" input.`,
            });
        }
        if (!graph) {
            return result("error", { reason: "Node is not attached to a graph." });
        }
        const link = physicalInputLink(node, inputIndex, graph);
        if (!link) {
            return result("none", {
                reason: `The "${inputName}" input is disconnected.`,
            });
        }
        const context = {
            maxHops: Math.max(1, Number(maxHops) || DEFAULT_MAX_HOPS),
            hops: 0,
            visited: new Set(),
            via: [],
        };
        const physical = traceLink(link, graph, context);
        if (physical.status === "resolved" || physical.status === "ambiguous") {
            return physical;
        }

        // Subgraph and virtual-node details are frontend-version-specific. Ask
        // the host to resolve the already-confirmed physical input only when the
        // graph-local lineage cannot finish; this never replaces a physical
        // result with a separate virtual source.
        const hostResults = callHostInputResolvers(node, inputIndex, graph, link)
            .map((value) => hostEndpoint(value, graph))
            .filter((endpoint) => endpoint?.node && endpoint.node !== node)
            .map((endpoint) => traceEndpoint(
                endpoint.node,
                endpoint.slot,
                endpoint.graph,
                endpoint.link,
                { ...context, via: ["host-resolveInput"] },
            ));
        return combineHostResults(hostResults, ["host-resolveInput"]) || physical;
    } catch (error) {
        return result("error", {
            reason: error?.message || "Unexpected project-source resolution failure.",
        });
    }
}

export function projectResolutionStatusText(resolution) {
    switch (resolution?.status) {
        case "ambiguous":
            return `Project source is ambiguous. ${resolution.reason || ""}`.trim();
        case "unsupported":
            return `Project source is unsupported. ${resolution.reason || ""}`.trim();
        case "error":
            return `Project source could not be resolved. ${resolution.reason || ""}`.trim();
        case "none":
        default:
            return "Connect a Sonder Editor project.";
    }
}

export function logProjectResolutionDiagnostic(resolution, {
    context = "background refresh",
    node = null,
} = {}) {
    if (!resolution || resolution.status === "resolved") return;
    const key = [
        context,
        node?.id ?? "unknown",
        resolution.status,
        resolution.reason,
    ].join("|");
    if (diagnosticKeys.has(key)) return;
    diagnosticKeys.add(key);
    console.warn(
        `[Sonder] Skipped ${context}: ${resolution.status}. ${resolution.reason || ""}`.trim(),
    );
}
