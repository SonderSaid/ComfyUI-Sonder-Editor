export const DORMANT_WIDGET_FLOOR = 150;
export const DORMANT_NODE_MIN_WIDTH = 240;

function finiteNumber(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
}

export function computeNodeOverhead(computedSize, widgetFloor = DORMANT_WIDGET_FLOOR) {
    const computedHeight = Array.isArray(computedSize)
        ? finiteNumber(computedSize[1], widgetFloor)
        : finiteNumber(computedSize, widgetFloor);
    return Math.max(0, computedHeight - widgetFloor);
}

export function widgetHeightFromNodeHeight(nodeHeight, overhead, widgetFloor = DORMANT_WIDGET_FLOOR) {
    return Math.max(
        widgetFloor,
        finiteNumber(nodeHeight, 0) - Math.max(0, finiteNumber(overhead, 0))
    );
}

export function nodeHeightFromWidgetHeight(widgetHeight, overhead, widgetFloor = DORMANT_WIDGET_FLOOR) {
    return Math.max(widgetFloor, finiteNumber(widgetHeight, widgetFloor))
        + Math.max(0, finiteNumber(overhead, 0));
}

export function clampNodeSizeToSafety(
    width,
    height,
    { minWidth = DORMANT_NODE_MIN_WIDTH, minHeight = 0 } = {}
) {
    return [
        Math.max(finiteNumber(minWidth, 0), finiteNumber(width, 0)),
        Math.max(finiteNumber(minHeight, 0), finiteNumber(height, 0)),
    ];
}

export function shouldAutoResizeDormantModule(moduleId, moduleDef) {
    if (!moduleId) return false;
    const hostSizing = moduleDef?.hostSizing || "auto";
    const nodeResize = moduleDef?.nodeResize || (hostSizing === "fill" ? "manual" : "auto");
    return nodeResize === "auto";
}

export function computeAutoResizeNodeHeight({
    currentNodeHeight,
    measuredWidgetHeight,
    overhead,
    widgetFloor = DORMANT_WIDGET_FLOOR,
    tolerance = 1,
    allowShrink = false,
} = {}) {
    const currentTotal = Math.max(0, finiteNumber(currentNodeHeight, 0));
    const currentWidget = widgetHeightFromNodeHeight(currentTotal, overhead, widgetFloor);
    const measuredWidget = Math.max(
        widgetFloor,
        Math.ceil(finiteNumber(measuredWidgetHeight, widgetFloor))
    );
    const nextWidget = allowShrink
        ? measuredWidget
        : Math.max(currentWidget, measuredWidget);
    const nextTotal = nodeHeightFromWidgetHeight(nextWidget, overhead, widgetFloor);

    if (Math.abs(nextTotal - currentTotal) <= Math.max(0, finiteNumber(tolerance, 0))) {
        return {
            nodeHeight: currentTotal,
            widgetHeight: currentWidget,
            shouldResize: false,
        };
    }

    return {
        nodeHeight: nextTotal,
        widgetHeight: nextWidget,
        shouldResize: true,
    };
}
