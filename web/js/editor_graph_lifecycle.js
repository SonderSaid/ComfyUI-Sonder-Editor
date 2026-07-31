export function chainAfterGraphConfigured(node, callback) {
    const original = node?.onAfterGraphConfigured;
    node.onAfterGraphConfigured = function (...args) {
        const result = original?.apply(this, args);
        callback?.apply(this, args);
        return result;
    };
    return original;
}

export function chainWidgetCallback(widget, callback) {
    const original = widget?.callback;
    widget.callback = function (...args) {
        if (args.length > 0 && widget.value !== args[0]) {
            widget.value = args[0];
        }
        const result = original?.apply(this, args);
        callback?.apply(this, args);
        return result;
    };
    return original;
}

export function createGraphAwareReconciler({
    isConfiguringGraph,
    getCurrentValue,
    applySynchronousState,
    hydrate,
    onError,
}) {
    let runId = 0;
    let pendingPromise = null;

    const reportError = (error) => {
        try {
            onError?.(error);
        } catch {
            // Error reporting must not escape into ComfyUI's graph lifecycle.
        }
    };

    const request = ({ force = false } = {}) => {
        if (!force && isConfiguringGraph?.()) return null;

        const currentRunId = ++runId;
        const value = getCurrentValue?.() ?? "";
        const isCurrent = () => (
            currentRunId === runId
            && (getCurrentValue?.() ?? "") === value
        );

        try {
            applySynchronousState?.(value, isCurrent);
        } catch (error) {
            reportError(error);
            pendingPromise = Promise.resolve(false);
            return pendingPromise;
        }

        pendingPromise = Promise.resolve()
            .then(async () => {
                if (!isCurrent()) return false;
                await hydrate?.(value, isCurrent);
                return isCurrent();
            })
            .catch((error) => {
                reportError(error);
                return false;
            });
        return pendingPromise;
    };

    return {
        request,
        cancel() {
            runId += 1;
        },
        get pendingPromise() {
            return pendingPromise;
        },
    };
}
