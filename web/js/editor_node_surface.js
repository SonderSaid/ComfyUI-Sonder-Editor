export const EDITOR_SURFACE_EXISTING_MIN_HEIGHT = 150;
export const EDITOR_SURFACE_CREATE_MIN_HEIGHT = 56;

const POINTER_EVENTS = [
    "pointerdown",
    "pointermove",
    "pointerup",
    "mousedown",
    "click",
];

function stopCanvasInteraction(event) {
    event.stopPropagation?.();
}

function setButtonPending(button, pending) {
    button.disabled = !!pending;
    button.setAttribute?.("aria-busy", pending ? "true" : "false");
    button.textContent = pending ? "Creating..." : "Create";
    button.style.opacity = pending ? "0.65" : "1";
    button.style.cursor = pending ? "wait" : "pointer";
}

/**
 * Creates the single DOM surface owned by a Sonder Editor node.
 *
 * Project state, reconciliation and networking remain host-owned. This module
 * owns only the surface DOM, its local listeners and its ephemeral Create
 * request state.
 */
export function createEditorNodeSurface({
    controllerElement,
    onCreate,
    onError,
} = {}) {
    if (!controllerElement) {
        throw new Error("Editor surface requires a controller element");
    }

    const element = document.createElement("div");
    element.className = "sonder-editor-node-surface";
    element.style.cssText = `
        box-sizing: border-box;
        width: 100%;
        height: 100%;
        min-width: 0;
        min-height: 0;
        overflow: hidden;
        contain: size;
        display: flex;
        flex-direction: column;
    `;

    const createRow = document.createElement("div");
    createRow.className = "sonder-editor-create-row";
    createRow.style.cssText = `
        box-sizing: border-box;
        width: 100%;
        min-width: 0;
        height: 36px;
        min-height: 36px;
        flex: 0 0 36px;
        display: none;
        align-items: stretch;
    `;

    const createButton = document.createElement("button");
    createButton.type = "button";
    createButton.textContent = "Create";
    createButton.style.cssText = `
        box-sizing: border-box;
        width: 100%;
        min-width: 0;
        min-height: 32px;
        border: 1px solid var(--border-color, #4b5563);
        border-radius: 6px;
        background: var(--comfy-input-bg, #24272d);
        color: var(--input-text, #e5e7eb);
        font: inherit;
        cursor: pointer;
    `;
    createRow.appendChild(createButton);
    element.append(controllerElement, createRow);
    controllerElement.hidden = false;
    createRow.hidden = true;

    let createMode = false;
    let createPending = false;
    let destroyed = false;
    let intentGeneration = 0;

    const syncPendingPresentation = () => {
        if (destroyed) return;
        setButtonPending(createButton, createPending);
    };

    const setCreateMode = (nextCreateMode) => {
        const next = !!nextCreateMode;
        const changed = next !== createMode;
        createMode = next;
        intentGeneration += 1;

        controllerElement.hidden = next;
        controllerElement.style.display = next ? "none" : "";
        createRow.hidden = !next;
        createRow.style.display = next ? "flex" : "none";
        syncPendingPresentation();
        return changed;
    };

    const runCreate = async () => {
        if (destroyed || !createMode || createPending) return false;

        createPending = true;
        const requestGeneration = intentGeneration;
        const isCurrent = () => (
            !destroyed
            && createMode
            && requestGeneration === intentGeneration
        );
        syncPendingPresentation();

        try {
            await onCreate?.(isCurrent);
            return isCurrent();
        } catch (error) {
            if (isCurrent()) {
                try {
                    onError?.(error);
                } catch {
                    // Error reporting must not escape into ComfyUI callbacks.
                }
            }
            return false;
        } finally {
            createPending = false;
            syncPendingPresentation();
        }
    };

    const onButtonClick = (event) => {
        stopCanvasInteraction(event);
        void runCreate();
    };
    createButton.addEventListener("click", onButtonClick);
    for (const eventName of POINTER_EVENTS) {
        createRow.addEventListener(eventName, stopCanvasInteraction);
    }

    return {
        element,
        createRow,
        createButton,
        setCreateMode,
        getMinHeight() {
            return createMode
                ? EDITOR_SURFACE_CREATE_MIN_HEIGHT
                : EDITOR_SURFACE_EXISTING_MIN_HEIGHT;
        },
        get isCreateMode() {
            return createMode;
        },
        get isCreatePending() {
            return createPending;
        },
        runCreate,
        destroy() {
            if (destroyed) return;
            destroyed = true;
            intentGeneration += 1;
            createButton.removeEventListener("click", onButtonClick);
            for (const eventName of POINTER_EVENTS) {
                createRow.removeEventListener(eventName, stopCanvasInteraction);
            }
            element.remove?.();
        },
    };
}
