import { EDITOR_CHROME as COLORS, chromeMenuCss } from "./editor_theme.js";
import { PRIORITY as KEY_PRIORITY, register as registerKeyboardConsumer } from "./keyboard_ownership.js";

let activeClose = null;
let sequence = 0;

const isComposing = (event) => event?.isComposing === true || event?.keyCode === 229;

/** Open one accessible menu stack. Returns an idempotent close function. */
export function openContextMenu({ x = 0, y = 0, items = [], closeOnScroll = false } = {}) {
    activeClose?.();
    const stack = [];
    const timers = new Set();
    let closed = false;
    let mouseInstalled = false;
    const menuId = `sonder-context-menu-${++sequence}`;

    const clearTimers = () => {
        for (const timer of timers) clearTimeout(timer);
        timers.clear();
    };
    const closeFrom = (depth) => {
        clearTimers();
        while (stack.length > depth) stack.pop().element.remove();
    };
    const close = () => {
        if (closed) return;
        closed = true;
        clearTimers();
        closeFrom(0);
        if (mouseInstalled) document.removeEventListener("mousedown", outsideMouseDown, true);
        if (closeOnScroll) window.removeEventListener("scroll", scrollClose, true);
        keyOff?.();
        if (activeClose === close) activeClose = null;
    };
    activeClose = close;

    const focusRow = (panel, index) => {
        if (!panel?.rows.length) return;
        const enabled = panel.rows.filter((entry) => !entry.item.disabled);
        if (!enabled.length) return;
        const target = panel.rows[index]?.item.disabled ? enabled[0] : panel.rows[index];
        for (const entry of panel.rows) entry.row.tabIndex = entry === target ? 0 : -1;
        target.row.focus({ preventScroll: true });
        panel.activeIndex = panel.rows.indexOf(target);
    };
    const stepFocus = (panel, delta) => {
        const enabledIndices = panel.rows.map((entry, index) => entry.item.disabled ? -1 : index)
            .filter((index) => index >= 0);
        if (!enabledIndices.length) return;
        const current = enabledIndices.indexOf(panel.activeIndex);
        const next = enabledIndices[(Math.max(0, current) + delta + enabledIndices.length)
            % enabledIndices.length];
        focusRow(panel, next);
    };

    const resolveSubmenu = async (entry, depth, focusFirst = false) => {
        if (!entry?.item?.submenu || entry.item.disabled || closed) return;
        let submenuItems = typeof entry.item.submenu === "function"
            ? entry.item.submenu() : entry.item.submenu;
        submenuItems = await Promise.resolve(submenuItems).catch(() => []);
        if (closed || !Array.isArray(submenuItems) || !submenuItems.length) return;
        closeFrom(depth + 1);
        const rect = entry.row.getBoundingClientRect();
        const child = renderPanel(submenuItems, rect.right - 2, rect.top, depth + 1, entry);
        if (focusFirst) focusRow(child, 0);
    };
    const scheduleSubmenu = (entry, depth) => {
        clearTimers();
        if (!entry.item.submenu) {
            closeFrom(depth + 1);
            return;
        }
        const timer = setTimeout(() => {
            timers.delete(timer);
            resolveSubmenu(entry, depth);
        }, 180);
        timers.add(timer);
    };

    function renderPanel(panelItems, panelX, panelY, depth, parentEntry = null) {
        const element = document.createElement("div");
        element.dataset.sonderPromptContextMenu = "1";
        element.dataset.sonderContextMenuDepth = String(depth);
        element.setAttribute("role", "menu");
        element.setAttribute("aria-label", depth ? "Context submenu" : "Context menu");
        element.style.cssText = `${chromeMenuCss(160)}left:${panelX}px;top:${panelY}px;z-index:${10000 + depth};`;
        const panel = { element, rows: [], activeIndex: -1, parentEntry };
        stack[depth] = panel;

        for (const item of panelItems.filter(Boolean)) {
            if (item.type === "separator") {
                const separator = document.createElement("div");
                separator.setAttribute("role", "separator");
                separator.style.cssText = `height:1px;margin:4px 8px;background:${COLORS.borderSoft};`;
                element.appendChild(separator);
                continue;
            }
            const row = document.createElement("div");
            const entry = { row, item };
            row.setAttribute("role", "menuitem");
            row.tabIndex = -1;
            row.setAttribute("aria-disabled", item.disabled ? "true" : "false");
            if (item.submenu) row.setAttribute("aria-haspopup", "menu");
            row.style.cssText = `display:flex;align-items:center;justify-content:space-between;gap:12px;padding:6px 14px;outline:none;cursor:${item.disabled ? "default" : "pointer"};color:${item.disabled ? COLORS.textMuted : (item.danger ? COLORS.dangerText : COLORS.text)};`;
            const label = document.createElement("span");
            label.textContent = String(item.label || "");
            row.appendChild(label);
            if (item.submenu) {
                const arrow = document.createElement("span");
                arrow.textContent = "›";
                arrow.setAttribute("aria-hidden", "true");
                row.appendChild(arrow);
            }
            row.addEventListener("focus", () => {
                panel.activeIndex = panel.rows.indexOf(entry);
                row.style.background = item.disabled ? "transparent" : COLORS.panelRaisedHover;
            });
            row.addEventListener("blur", () => { row.style.background = "transparent"; });
            row.addEventListener("mouseenter", () => {
                if (!item.disabled) focusRow(panel, panel.rows.indexOf(entry));
                scheduleSubmenu(entry, depth);
            });
            row.addEventListener("mousedown", (event) => {
                event.preventDefault();
                if (!item.disabled) focusRow(panel, panel.rows.indexOf(entry));
            });
            row.addEventListener("click", () => {
                if (item.disabled) return;
                if (item.submenu) {
                    resolveSubmenu(entry, depth, true);
                    return;
                }
                close();
                item.action?.();
            });
            if (typeof item.onContextMenu === "function") {
                row.addEventListener("contextmenu", (event) => {
                    if (item.disabled) return;
                    event.preventDefault();
                    event.stopPropagation();
                    item.onContextMenu(event);
                });
            }
            panel.rows.push(entry);
            element.appendChild(row);
        }

        document.body.appendChild(element);
        requestAnimationFrame(() => {
            if (closed || !element.isConnected) return;
            const rect = element.getBoundingClientRect();
            let left = panelX;
            if (depth && parentEntry && left + rect.width > window.innerWidth - 4) {
                left = parentEntry.row.getBoundingClientRect().left - rect.width + 2;
            }
            left = Math.max(4, Math.min(left, window.innerWidth - rect.width - 4));
            const top = Math.max(4, Math.min(panelY, window.innerHeight - rect.height - 4));
            element.style.left = `${left}px`;
            element.style.top = `${top}px`;
        });
        return panel;
    }

    const outsideMouseDown = (event) => {
        if (!stack.some((panel) => panel.element.contains(event.target))) close();
    };
    const scrollClose = () => close();
    const keyOff = registerKeyboardConsumer({
        id: menuId,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (isComposing(event)) return false;
            const panel = stack[stack.length - 1];
            if (!panel) return false;
            if (event.key === "Escape") {
                if (stack.length > 1) {
                    const parent = panel.parentEntry;
                    closeFrom(stack.length - 1);
                    parent?.row?.focus?.({ preventScroll: true });
                } else close();
                return true;
            }
            if (event.key === "ArrowDown") { stepFocus(panel, 1); return true; }
            if (event.key === "ArrowUp") { stepFocus(panel, -1); return true; }
            if (event.key === "Home") { focusRow(panel, 0); return true; }
            if (event.key === "End") { focusRow(panel, panel.rows.length - 1); return true; }
            const entry = panel.rows[panel.activeIndex];
            if (event.key === "ArrowRight" && entry?.item?.submenu) {
                resolveSubmenu(entry, stack.length - 1, true); return true;
            }
            if (event.key === "ArrowLeft" && stack.length > 1) {
                const parent = panel.parentEntry;
                closeFrom(stack.length - 1);
                parent?.row?.focus?.({ preventScroll: true });
                return true;
            }
            if ((event.key === "Enter" || event.key === " ") && entry && !entry.item.disabled) {
                entry.row.click(); return true;
            }
            return false;
        },
    });

    const root = renderPanel(Array.isArray(items) ? items : [], Number(x) || 0, Number(y) || 0, 0);
    close.element = root.element;
    close.isOpen = () => !closed;
    setTimeout(() => {
        if (closed) return;
        document.addEventListener("mousedown", outsideMouseDown, true);
        mouseInstalled = true;
        if (closeOnScroll) window.addEventListener("scroll", scrollClose, true);
    }, 10);
    return close;
}
