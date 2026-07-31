const VALUE_SLOT_PATTERN = /^(?:values\.)?value_(\d+)$/;
const LABEL_WIDGET_PATTERN = /^label_(\d+)$/;

export function collectorValueIndex(slotOrName) {
    const name = typeof slotOrName === "string" ? slotOrName : slotOrName?.name;
    if (typeof name !== "string") return -1;
    const match = VALUE_SLOT_PATTERN.exec(name);
    return match ? Number.parseInt(match[1], 10) : -1;
}

export function collectorLabelIndex(widgetOrName) {
    const name = typeof widgetOrName === "string" ? widgetOrName : widgetOrName?.name;
    if (typeof name !== "string") return -1;
    const match = LABEL_WIDGET_PATTERN.exec(name);
    return match ? Number.parseInt(match[1], 10) : -1;
}

export function collectorCapacity(node) {
    let max = -1;
    for (const widget of node?.widgets || []) {
        max = Math.max(max, collectorLabelIndex(widget));
    }
    for (const slot of node?.inputs || []) {
        max = Math.max(max, collectorValueIndex(slot));
    }
    return Math.max(1, max + 1);
}

export function visibleCollectorValueCount(node, capacity = collectorCapacity(node)) {
    let max = -1;
    for (const slot of node?.inputs || []) {
        max = Math.max(max, collectorValueIndex(slot));
    }
    return Math.max(1, Math.min(capacity, max + 1));
}
