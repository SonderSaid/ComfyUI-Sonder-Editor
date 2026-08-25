/**
 * Move the long-lived Render Queue between its mounted gallery owner and the
 * fullscreen sidebar without rebuilding either tree.
 */
export function dockQueueInFullscreen(queueSection, sidebar) {
    if (!queueSection || !sidebar) return null;
    const placement = {
        parent: queueSection.parentElement,
        nextSibling: queueSection.nextSibling,
    };
    queueSection.style.flex = "0 0 auto";
    sidebar.appendChild(queueSection);
    return placement;
}

export function restoreQueueFromFullscreen(queueSection, placement) {
    const parent = placement?.parent;
    if (!queueSection || !parent) return false;
    queueSection.style.flex = "";
    // A sibling may disappear while fullscreen is open. Only reuse the saved
    // anchor while it still belongs to the saved parent; otherwise append.
    const nextSibling = placement.nextSibling?.parentElement === parent
        ? placement.nextSibling
        : null;
    parent.insertBefore(queueSection, nextSibling);
    return true;
}
