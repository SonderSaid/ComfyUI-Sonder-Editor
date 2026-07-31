const VISIBILITY_STATE = new WeakMap();
const HIDDEN_COMPUTE_SIZE = () => [0, -4];
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

function propertySnapshot(target, key) {
    return {
        hadOwn: hasOwn(target, key),
        descriptor: Object.getOwnPropertyDescriptor(target, key),
    };
}

function restoreProperty(target, key, snapshot) {
    if (snapshot.hadOwn && snapshot.descriptor) {
        Object.defineProperty(target, key, snapshot.descriptor);
    } else {
        delete target[key];
    }
}

function setTransientProperty(target, key, value) {
    const descriptor = Object.getOwnPropertyDescriptor(target, key);
    if (descriptor && !descriptor.configurable) {
        target[key] = value;
        return;
    }
    Object.defineProperty(target, key, {
        value,
        writable: true,
        enumerable: descriptor?.enumerable ?? true,
        configurable: true,
    });
}

export function isWidgetEffectivelyHidden(widget) {
    return !!(widget && (widget.hidden === true || widget.options?.hidden === true));
}

export function setWidgetHidden(widget, hidden) {
    if (!widget) return false;

    if (hidden) {
        let state = VISIBILITY_STATE.get(widget);
        if (!state) {
            const inheritedOrOwnOptions =
                widget.options && typeof widget.options === "object" ? widget.options : null;
            const options = hasOwn(widget, "options")
                ? inheritedOrOwnOptions
                : { ...(inheritedOrOwnOptions || {}) };
            state = {
                options: propertySnapshot(widget, "options"),
                optionsHidden: options
                    ? propertySnapshot(options, "hidden")
                    : { hadOwn: false, descriptor: undefined },
                optionsTarget: options,
                hidden: propertySnapshot(widget, "hidden"),
                computeSize: propertySnapshot(widget, "computeSize"),
            };
            VISIBILITY_STATE.set(widget, state);

            if (options !== inheritedOrOwnOptions) {
                setTransientProperty(widget, "options", options);
            }
        }

        if (!state.optionsTarget) {
            state.optionsTarget = {};
            setTransientProperty(widget, "options", state.optionsTarget);
        }

        const changed = !isWidgetEffectivelyHidden(widget) || widget.computeSize !== HIDDEN_COMPUTE_SIZE;
        setTransientProperty(state.optionsTarget, "hidden", true);
        setTransientProperty(widget, "hidden", true);
        setTransientProperty(widget, "computeSize", HIDDEN_COMPUTE_SIZE);
        return changed;
    }

    const state = VISIBILITY_STATE.get(widget);
    if (!state) return false;

    if (state.optionsTarget) {
        restoreProperty(state.optionsTarget, "hidden", state.optionsHidden);
    }
    restoreProperty(widget, "options", state.options);
    restoreProperty(widget, "hidden", state.hidden);
    restoreProperty(widget, "computeSize", state.computeSize);
    VISIBILITY_STATE.delete(widget);
    return true;
}

export function commitWidgetVisibility(node, { resize = false, dirty = true } = {}) {
    if (!node) return;

    if (Array.isArray(node.widgets)) {
        // Nodes 2.0 extracts a render-safe widget snapshot. Replacing the array
        // invalidates that snapshot without changing widget identity or order.
        node.widgets = [...node.widgets];
    }

    if (resize && typeof node.computeSize === "function" && typeof node.setSize === "function") {
        node.setSize(node.computeSize());
    }

    if (dirty) {
        if (typeof node.setDirtyCanvas === "function") {
            node.setDirtyCanvas(true, true);
        } else {
            node.graph?.setDirtyCanvas?.(true, true);
        }
    }
}
