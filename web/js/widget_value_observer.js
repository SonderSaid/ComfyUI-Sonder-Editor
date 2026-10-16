// Observe a canvas widget's value changes without becoming a writer.
// Pure on import: no app, DOM or ComfyUI module, so harnesses load it directly.
//
// Every writer that should be seen -- a panel, the Controls canvas writer, the
// Relay -- ends with `widget.callback(value, ...)`. The callback is wrapped once
// per widget; the wrap keeps the receiver, arguments and return value, then tells
// each observer the widget's value. Observers only re-render or schedule reads:
// they never write a widget, record history or dirty the graph, because work an
// observer starts can land outside the writer's `captureCanvasState` bracket.
// Undo is a reload, which rebuilds the nodes and so the wraps.

const OBSERVERS = Symbol.for("sonder.widgetValueObservers");

export function findObservedWidget(node, name) {
    return (node?.widgets || []).find((widget) => widget?.name === name) || null;
}

/**
 * Call `onChange(value, widget)` after every callback of `node`'s widget `name`.
 * Returns an unsubscribe function, or null when the widget does not exist.
 * Observing the same function twice registers it once.
 */
export function observeWidgetValue(node, name, onChange) {
    const widget = findObservedWidget(node, name);
    if (!widget || typeof onChange !== "function") return null;
    let observers = widget[OBSERVERS];
    if (!observers) {
        observers = new Set();
        const original = widget.callback;
        widget.callback = function (...args) {
            const result = original?.apply(this, args);
            for (const observer of [...observers]) {
                try {
                    observer(widget.value, widget);
                } catch (error) {
                    console.warn(`[Sonder] widget observer for ${name} failed:`, error);
                }
            }
            return result;
        };
        widget[OBSERVERS] = observers;
    }
    observers.add(onChange);
    return () => observers.delete(onChange);
}
