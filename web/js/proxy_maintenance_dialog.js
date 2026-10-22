// Settings → Proxies → Regenerate Proxies… / Delete Proxies…: one dialog for both,
// with a checkbox per usage category and what each would touch. The host owns the
// requests (`onConfirm(categories)`); this module owns the DOM, its listeners and
// its keys, and returns `close`.
//
// Categories come from `GET /proxies` (`server/routes.py`, `_proxy_overview`): every
// video asset is in exactly one of scenes, references, unused or trashed. Proxies of
// trashed assets are always deleted, so trashed has no checkbox.

import { THEME, TYPE, SPACE, RADIUS, chromeButtonCss, chromeOverlayPanelCss, setButtonDisabled } from "./editor_theme.js";
import { register as registerKeyboardConsumer, PRIORITY as KEY_PRIORITY } from "./keyboard_ownership.js";

export const PROXY_CATEGORIES = Object.freeze([
    { id: "scenes", label: "Used in scenes" },
    { id: "references", label: "Used by References" },
    { id: "unused", label: "Unused" },
]);

// Decisions under which an asset wants a proxy (server `proxy_media.PROXY_DECISIONS`).
const WANTS_PROXY = new Set(["undecodable", "manual", "runtime"]);

/** "1.2 MB", "340 KB", "0 B". */
export function formatProxyBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${bytes} B`;
}

function plural(count, one, many = `${one}s`) {
    return `${count} ${count === 1 ? one : many}`;
}

/**
 * Per category, what the action would touch:
 * `{ [category]: { proxies, bytes, missing, unchecked } }`. `proxies`/`bytes`: ready
 * proxies. `missing`: assets that need a proxy and have none (Regenerate creates
 * those; it never creates one for an asset that plays as is). `unchecked`: videos
 * not decided yet, which Regenerate has checked and builds only if they need one.
 * This only previews the count: the server applies the same rule
 * (`api_regenerate_proxies`, `api_delete_proxies`) and is the authority.
 */
export function proxyCategorySummary(overview) {
    const summary = {};
    for (const id of [...PROXY_CATEGORIES.map((entry) => entry.id), "trashed"]) {
        summary[id] = { proxies: 0, bytes: 0, missing: 0, unchecked: 0 };
    }
    for (const item of Object.values(overview?.items || {})) {
        const bucket = summary[item?.category];
        if (!bucket) continue;
        if (item.state === "ready") {
            bucket.proxies += 1;
            bucket.bytes += Math.max(0, Number(item.bytes) || 0);
        } else if (WANTS_PROXY.has(String(item.decision || ""))) {
            bucket.missing += 1;
        } else if (!item.decision || item.decision === "pending") {
            bucket.unchecked += 1;
        }
    }
    return summary;
}

/** What the confirm button would do, for its label and enablement. */
export function proxyActionTotals(overview, mode, categories) {
    const summary = proxyCategorySummary(overview);
    const chosen = new Set(categories || []);
    let proxies = 0;
    let bytes = 0;
    let missing = 0;
    let unchecked = 0;
    for (const [id, bucket] of Object.entries(summary)) {
        // Trashed proxies go with every Delete; Delete needs a category all the same.
        const included = chosen.has(id) || (mode === "delete" && id === "trashed" && chosen.size > 0);
        if (!included) continue;
        proxies += bucket.proxies;
        bytes += bucket.bytes;
        if (mode === "regenerate" && id !== "trashed") {
            missing += bucket.missing;
            unchecked += bucket.unchecked;
        }
    }
    return {
        proxies, bytes, missing, unchecked,
        any: mode === "delete" ? proxies > 0 : proxies + missing + unchecked > 0,
    };
}

/** Delete starts with Unused only; Regenerate with every category. */
export function defaultProxyCategories(mode) {
    return mode === "delete" ? ["unused"] : PROXY_CATEGORIES.map((entry) => entry.id);
}

const COPY = {
    regenerate: {
        title: "Regenerate Proxies",
        intro: "Rebuilds existing proxies with this project's current proxy preset, and builds missing ones for videos that can't play in the browser. Builds follow the Build Proxies setting.",
        confirm: "Regenerate",
        variant: "primary",
    },
    delete: {
        title: "Delete Proxies",
        intro: "Removes playable copies from this project's cache. The original videos are untouched. A video that can't play in the browser shows a notice until its proxy is built again.",
        confirm: "Delete",
        variant: "danger",
    },
};

function categoryLine(mode, bucket) {
    const parts = [`${plural(bucket.proxies, "proxy", "proxies")} · ${formatProxyBytes(bucket.bytes)}`];
    if (mode === "regenerate" && bucket.missing) parts.push(`${bucket.missing} to build`);
    if (mode === "regenerate" && bucket.unchecked) parts.push(`${bucket.unchecked} not checked yet`);
    return parts.join(" · ");
}

/**
 * Open the dialog. `overview` is `GET /proxies`; `onConfirm(categories)` returns a
 * promise (the host shows any failure); `keyboardConsumerId` names the Escape/Enter
 * owner. Returns `{ close }`.
 */
export function openProxyMaintenanceDialog({
    mode = "regenerate",
    overview = null,
    keyboardConsumerId = "proxy-maintenance",
    onConfirm = async () => {},
    onClose = () => {},
} = {}) {
    const copy = COPY[mode] || COPY.regenerate;
    const summary = proxyCategorySummary(overview);
    const chosen = new Set(defaultProxyCategories(mode));
    let closed = false;
    let busy = false;

    const backdrop = document.createElement("div");
    backdrop.dataset.sonderProxyMaintenance = mode;
    backdrop.style.cssText = `position:fixed;inset:0;z-index:10003;background:rgba(7,10,14,0.72);display:flex;align-items:center;justify-content:center;padding:${SPACE.s5}px;`;
    const panel = document.createElement("div");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.style.cssText = `${chromeOverlayPanelCss({ width: "min(440px, 94vw)", maxWidth: "440px", padding: `${SPACE.s4}px ${SPACE.s5}px` })}display:flex;flex-direction:column;gap:${SPACE.s3}px;`;

    const title = document.createElement("div");
    title.textContent = copy.title;
    title.style.cssText = `font-size:${TYPE.t14}px;font-weight:${TYPE.fwBold};color:${THEME.fg0};`;
    panel.setAttribute("aria-label", copy.title);
    const intro = document.createElement("div");
    intro.textContent = copy.intro;
    intro.style.cssText = `font-size:${TYPE.t11}px;line-height:1.5;color:${THEME.fg1};`;

    const list = document.createElement("div");
    list.style.cssText = `display:flex;flex-direction:column;gap:${SPACE.s1}px;`;
    const checkboxes = new Map();
    for (const category of PROXY_CATEGORIES) {
        const row = document.createElement("label");
        row.style.cssText = `display:flex;align-items:center;gap:${SPACE.s2}px;padding:${SPACE.s2}px;border:1px solid ${THEME.line2};border-radius:${RADIUS.r2}px;background:${THEME.bg2};cursor:pointer;`;
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = chosen.has(category.id);
        box.dataset.category = category.id;
        box.style.cssText = "margin:0;cursor:pointer;";
        box.addEventListener("change", () => {
            if (box.checked) chosen.add(category.id);
            else chosen.delete(category.id);
            sync();
        });
        const text = document.createElement("div");
        text.style.cssText = "display:flex;flex-direction:column;gap:2px;min-width:0;";
        const name = document.createElement("div");
        name.textContent = category.label;
        name.style.cssText = `font-size:${TYPE.t12}px;color:${THEME.fg0};`;
        const detail = document.createElement("div");
        detail.textContent = categoryLine(mode, summary[category.id]);
        detail.style.cssText = `font-size:${TYPE.t10}px;color:${THEME.fg2};`;
        text.append(name, detail);
        row.append(box, text);
        list.appendChild(row);
        checkboxes.set(category.id, box);
    }

    const note = document.createElement("div");
    note.style.cssText = `font-size:${TYPE.t10}px;line-height:1.5;color:${THEME.fg2};`;
    const trashed = summary.trashed;
    note.textContent = mode === "delete"
        ? `An unchecked category keeps its proxies. Proxies of trashed assets are always deleted${trashed.proxies ? ` (${plural(trashed.proxies, "proxy", "proxies")} · ${formatProxyBytes(trashed.bytes)})` : ""}.`
        : "An unchecked category keeps its proxies as they are. Trashed assets are never rebuilt.";

    const status = document.createElement("div");
    status.style.cssText = `font-size:${TYPE.t10}px;color:${THEME.fg1};min-height:14px;`;

    const actions = document.createElement("div");
    actions.style.cssText = `display:flex;justify-content:flex-end;gap:${SPACE.s2}px;`;
    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.textContent = "Cancel";
    cancelButton.style.cssText = chromeButtonCss({ variant: "subtle", padding: "6px 12px" });
    const confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.dataset.proxyConfirm = mode;
    confirmButton.style.cssText = chromeButtonCss({ variant: copy.variant, padding: "6px 12px" });
    actions.append(cancelButton, confirmButton);

    panel.append(title, intro, list, note, status, actions);
    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);

    function sync() {
        const totals = proxyActionTotals(overview, mode, [...chosen]);
        const what = mode === "delete"
            ? `${plural(totals.proxies, "proxy", "proxies")} · ${formatProxyBytes(totals.bytes)}`
            : [totals.proxies ? `${plural(totals.proxies, "proxy", "proxies")} to rebuild` : "",
                totals.missing ? `${totals.missing} to build` : "",
                totals.unchecked ? `${totals.unchecked} to check` : ""].filter(Boolean).join(", ");
        status.textContent = totals.any ? `${copy.confirm}: ${what}.` : "Nothing to do in the checked categories.";
        confirmButton.textContent = copy.confirm;
        setButtonDisabled(confirmButton, busy || !totals.any);
        confirmButton.title = totals.any ? "" : "Nothing to do in the checked categories.";
        for (const box of checkboxes.values()) box.disabled = busy;
        setButtonDisabled(cancelButton, busy);
    }

    async function confirm() {
        if (busy || confirmButton.disabled) return;
        busy = true;
        sync();
        try {
            await onConfirm([...chosen]);
            close();
        } catch (_error) {
            // The host reported the failure; keep the dialog so the user can retry.
            busy = false;
            if (!closed) sync();
        }
    }

    function close() {
        if (closed) return;
        closed = true;
        keyOff();
        backdrop.remove();
        try { onClose(); } catch (_) {}
    }

    cancelButton.addEventListener("click", () => { if (!busy) close(); });
    confirmButton.addEventListener("click", () => { void confirm(); });
    backdrop.addEventListener("click", (event) => { if (event.target === backdrop && !busy) close(); });
    // An OVERLAY over Settings: Escape must close this, never the panel behind it.
    const keyOff = registerKeyboardConsumer({
        id: keyboardConsumerId,
        priority: KEY_PRIORITY.OVERLAY,
        keydown: (event) => {
            if (event.isComposing || event.keyCode === 229) return false;
            if (event.key === "Escape") {
                if (!busy) close();
                return true;
            }
            if (event.key === "Enter") {
                // Enter on Cancel or a checkbox does what that control does, never Delete.
                const target = event.target;
                if (target && target !== confirmButton && panel.contains?.(target)
                    && (target.tagName === "BUTTON" || target.tagName === "INPUT")) {
                    return false;
                }
                void confirm();
                return true;
            }
            return false;
        },
    });
    sync();
    // A Delete starts on Cancel, so a stray Enter or Space never deletes.
    (mode === "delete" ? cancelButton : confirmButton).focus?.();
    return { close, element: backdrop };
}
