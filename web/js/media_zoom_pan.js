// Wheel zoom, drag pan and double-click reset over a media surface. The state
// is the caller's ({zoomLevel, panX, panY}, mutated in place), so the gallery's
// Inspect overlay and an Output View pane each keep their own. Returns cleanup.

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

export function attachZoomPan(surface, targets, { transformState, onTransform = null, onClick = null, maxZoom = 16 } = {}) {
    if (!transformState) throw new TypeError("attachZoomPan needs a transform state.");
    const targetList = (Array.isArray(targets) ? targets : [targets]).filter(Boolean);
    let dragging = false;
    let pendingDrag = false;
    let suppressClick = false;
    let lastX = 0;
    let lastY = 0;
    let dragStartX = 0;
    let dragStartY = 0;
    let lastDragMouseUpAt = 0;

    const applyTransform = () => {
        const transform = `translate(${transformState.panX}px, ${transformState.panY}px) scale(${transformState.zoomLevel})`;
        for (const target of targetList) {
            target.style.transformOrigin = "center center";
            target.style.transform = transform;
        }
        onTransform?.();
    };

    const updateCursor = () => {
        surface.style.cursor = pendingDrag || dragging ? "grabbing" : "grab";
    };

    const handleWheel = (event) => {
        event.preventDefault();
        const rect = surface.getBoundingClientRect();
        const previousZoom = transformState.zoomLevel;
        const zoomingIn = event.deltaY < 0;
        const nextZoom = clamp(previousZoom * (zoomingIn ? 1.12 : (1 / 1.12)), 1, maxZoom);
        if (nextZoom === previousZoom) return;
        if (zoomingIn) {
            const cursorX = event.clientX - rect.left - rect.width / 2;
            const cursorY = event.clientY - rect.top - rect.height / 2;
            const scale = nextZoom / previousZoom;
            transformState.panX -= cursorX * (scale - 1);
            transformState.panY -= cursorY * (scale - 1);
        } else {
            const denom = Math.max(0.0001, previousZoom - 1);
            const factor = Math.max(0, (nextZoom - 1) / denom);
            transformState.panX *= factor;
            transformState.panY *= factor;
        }
        transformState.zoomLevel = nextZoom;
        applyTransform();
    };

    const handleMouseMove = (event) => {
        if (!pendingDrag && !dragging) return;
        if (!dragging) {
            const distance = Math.hypot(event.clientX - dragStartX, event.clientY - dragStartY);
            if (distance < 5) return;
            dragging = true;
            lastX = event.clientX;
            lastY = event.clientY;
            event.preventDefault();
        }
        transformState.panX += event.clientX - lastX;
        transformState.panY += event.clientY - lastY;
        lastX = event.clientX;
        lastY = event.clientY;
        applyTransform();
    };

    const handleMouseUp = (event) => {
        const dragged = dragging;
        if (dragged) lastDragMouseUpAt = performance.now();
        dragging = false;
        pendingDrag = false;
        updateCursor();
        window.removeEventListener("mousemove", handleMouseMove);
        window.removeEventListener("mouseup", handleMouseUp);
        if (dragged || (!dragged && onClick)) {
            suppressClick = true;
            setTimeout(() => {
                suppressClick = false;
            }, 0);
        }
        if (!dragged && onClick && (!event?.target || surface.contains(event.target))) {
            onClick(event);
        }
    };

    const handleMouseDown = (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        pendingDrag = true;
        dragging = false;
        dragStartX = event.clientX;
        dragStartY = event.clientY;
        lastX = event.clientX;
        lastY = event.clientY;
        updateCursor();
        window.addEventListener("mousemove", handleMouseMove);
        window.addEventListener("mouseup", handleMouseUp);
    };

    const handleDragStart = (event) => {
        event.preventDefault();
    };

    const handleClickCapture = (event) => {
        if (!suppressClick) return;
        suppressClick = false;
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation?.();
    };

    const handleDoubleClick = () => {
        if (performance.now() - lastDragMouseUpAt < 350) return;
        transformState.zoomLevel = 1;
        transformState.panX = 0;
        transformState.panY = 0;
        applyTransform();
    };

    surface.addEventListener("wheel", handleWheel, { passive: false });
    surface.addEventListener("mousedown", handleMouseDown);
    surface.addEventListener("click", handleClickCapture, true);
    surface.addEventListener("dblclick", handleDoubleClick);
    surface.addEventListener("dragstart", handleDragStart);
    updateCursor();
    applyTransform();

    return () => {
        dragging = false;
        pendingDrag = false;
        suppressClick = false;
        surface.removeEventListener("wheel", handleWheel);
        surface.removeEventListener("mousedown", handleMouseDown);
        surface.removeEventListener("click", handleClickCapture, true);
        surface.removeEventListener("dblclick", handleDoubleClick);
        surface.removeEventListener("dragstart", handleDragStart);
        window.removeEventListener("mousemove", handleMouseMove);
        window.removeEventListener("mouseup", handleMouseUp);
        surface.style.cursor = "";
    };
}
