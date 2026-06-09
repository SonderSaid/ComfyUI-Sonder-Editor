// Bottom-right toast-stack presenter for the Sonder Editor notification system.
//
// Page-level chrome: mounted once per page (canvas page from extension.js, tab
// page from tab_entry.js) at page entry, page-lifetime — NOT tied to fullscreen
// enter/exit. Works because fullscreen is a position:fixed overlay on the same
// document.body as the dormant canvas, so one page-level stack serves both.
//
// Subscribes to the DOM-free Core (editor_notifications.js) and renders. Owns its
// DOM/listeners and returns cleanup. Theme-only via editor_theme.js. No
// backdrop-filter (durable rule). No keydown listener — dismissal is pointer/✕/
// hover-pause + aria-live; if Esc-dismiss is ever added it MUST go through
// keyboard_ownership.js at PRIORITY.OVERLAY.

import {
    subscribe,
    pause,
    resume,
    dismiss,
    formatProgress,
    progressFraction,
} from "./editor_notifications.js";
import { THEME, TYPE, RADIUS, FONT, MOTION, chromeButtonCss } from "./editor_theme.js";

const STYLE_ID = "sonder-toast-stack-style";
// Above the gallery inspect overlay (99999) + gallery modals (100001/100002),
// the extension.js notice (100000), the fullscreen overlay (9999) and the old
// toast (10020). The original 10040 would have hidden gallery failure toasts.
const STACK_Z = 1000100;

function tierAccent(tier) {
    switch (tier) {
        case "success": return THEME.statusCompleted;
        case "warning": return THEME.statusPending;
        case "error": return THEME.statusFailed;
        case "progress":
        case "info":
        default: return THEME.accent;
    }
}

function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
        @keyframes sonder-toast-indet {
            0% { left: -40%; }
            100% { left: 100%; }
        }
        .sonder-toast { transition: opacity ${MOTION.dur} ${MOTION.ease}, transform ${MOTION.dur} ${MOTION.ease}; }
        @media (prefers-reduced-motion: reduce) {
            .sonder-toast { transition: opacity 120ms linear !important; transform: none !important; }
            .sonder-toast-indet-fill { animation: none !important; }
        }
    `;
    (document.head || document.documentElement).appendChild(style);
}

function formatElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    if (total < 60) return `${total}s`;
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
}

function copyText(text) {
    if (!text) return;
    try {
        if (navigator.clipboard?.writeText) {
            navigator.clipboard.writeText(text);
            return;
        }
    } catch (_) {}
    try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none;";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
    } catch (_) {}
}

// Collapse long text by default; expand (with bounded scroll) on hover so the
// full message — e.g. a long ffmpeg error — is readable, then collapse on leave.
function applyExpand(el) {
    const expanded = !!el._expanded;
    const t = el._titleEl;
    if (t) {
        t.style.display = "block";
        t.style.whiteSpace = expanded ? "normal" : "nowrap";
        t.style.overflow = expanded ? "auto" : "hidden";
        t.style.textOverflow = expanded ? "clip" : "ellipsis";
        t.style.wordBreak = expanded ? "break-word" : "normal";
        t.style.maxHeight = expanded ? "30vh" : "";
    }
    const msg = el._msgEl;
    if (msg) {
        if (expanded) {
            msg.style.display = "block";
            msg.style.webkitLineClamp = "";
            msg.style.maxHeight = "40vh";
            msg.style.overflowY = "auto";
        } else {
            msg.style.display = "-webkit-box";
            msg.style.webkitBoxOrient = "vertical";
            msg.style.webkitLineClamp = "2";
            msg.style.maxHeight = "";
            msg.style.overflowY = "hidden";
        }
    }
}

function flashCopied(el) {
    if (el._copiedBadge) el._copiedBadge.remove();
    const badge = document.createElement("div");
    badge.textContent = "Copied";
    badge.style.cssText = `position:absolute; top:6px; right:26px; font-size:${TYPE.t10}px; color:${THEME.statusCompleted}; background:${THEME.bg0}; border:1px solid ${THEME.line2}; border-radius:${RADIUS.r1}px; padding:1px 6px; pointer-events:none; opacity:1; transition:opacity 200ms ease;`;
    el.appendChild(badge);
    el._copiedBadge = badge;
    window.setTimeout(() => { badge.style.opacity = "0"; }, 700);
    window.setTimeout(() => { if (badge.parentNode) badge.remove(); if (el._copiedBadge === badge) el._copiedBadge = null; }, 1000);
}

export function mountToastStack(target = document.body) {
    if (!target) return () => {};
    ensureStyle();

    const container = document.createElement("div");
    container.setAttribute("data-sonder-toast-stack", "1");
    container.style.cssText = `
        position: fixed;
        right: 16px;
        bottom: 16px;
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 8px;
        z-index: ${STACK_Z};
        pointer-events: none;
        max-width: 360px;
    `;
    target.appendChild(container);

    const els = new Map(); // id -> outer element
    let elapsedTimer = null;

    function ensureElapsedTimer(active) {
        if (active && elapsedTimer == null) {
            elapsedTimer = window.setInterval(tickElapsed, 1000);
        } else if (!active && elapsedTimer != null) {
            window.clearInterval(elapsedTimer);
            elapsedTimer = null;
        }
    }

    function tickElapsed() {
        const now = Date.now();
        let any = false;
        for (const el of els.values()) {
            const span = el.querySelector("[data-elapsed]");
            if (!span) continue;
            any = true;
            const created = Number(el.dataset.created || now);
            span.textContent = formatElapsed(now - created);
        }
        if (!any) ensureElapsedTimer(false);
    }

    function createToast(n) {
        const el = document.createElement("div");
        el.className = "sonder-toast";
        el.dataset.created = String(n.createdAt);
        el.style.cssText = `
            position: relative;
            pointer-events: auto;
            box-sizing: border-box;
            min-width: 240px;
            max-width: 360px;
            background: ${THEME.bg2};
            border: 1px solid ${THEME.line2};
            border-radius: ${RADIUS.r3}px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
            padding: 9px 11px 9px 14px;
            font-family: ${FONT.sans};
            color: ${THEME.fg1};
            overflow: hidden;
            opacity: 0;
            transform: translateX(12px);
        `;

        const bar = document.createElement("span");
        bar.dataset.accent = "1";
        bar.style.cssText = `position:absolute; left:0; top:0; bottom:0; width:3px;`;
        el.appendChild(bar);

        const body = document.createElement("div");
        body.dataset.body = "1";
        el.appendChild(body);

        el.title = "Hover to expand · right-click to copy";
        // Hover pauses the auto-dismiss countdown (no-op for sticky tiers) and
        // expands long text; leaving collapses + resumes.
        el.addEventListener("mouseenter", () => { el._expanded = true; applyExpand(el); pause(n.id); });
        el.addEventListener("mouseleave", () => { el._expanded = false; applyExpand(el); resume(n.id); });
        // Right-click copies the full toast text (esp. useful for long errors).
        el.addEventListener("contextmenu", (e) => {
            e.preventDefault();
            copyText(el._copyText || "");
            flashCopied(el);
        });

        renderInner(el, body, n);

        // Slide-in on next frame (reduced-motion forces fade-only via CSS).
        requestAnimationFrame(() => {
            el.style.opacity = "1";
            el.style.transform = "translateX(0)";
        });
        return el;
    }

    function renderInner(el, body, n) {
        const accent = tierAccent(n.tier);
        const barEl = el.querySelector("[data-accent]");
        if (barEl) barEl.style.background = accent;

        const isError = n.tier === "error";
        el.setAttribute("role", isError ? "alert" : "status");
        el.setAttribute("aria-live", isError ? "assertive" : "polite");

        body.replaceChildren();

        // ── Header: title + count + dismiss ──
        const header = document.createElement("div");
        header.style.cssText = `display:flex; align-items:center; gap:8px;`;

        const titleText = n.verb || n.message || "";
        const title = document.createElement("span");
        title.style.cssText = `flex:1 1 auto; min-width:0; font-size:${TYPE.t12}px; font-weight:${TYPE.fwBold}; color:${THEME.fg0};`;
        title.textContent = titleText;
        header.appendChild(title);
        el._titleEl = title;

        if (n.count > 1) {
            const badge = document.createElement("span");
            badge.style.cssText = `flex:0 0 auto; font-size:${TYPE.t10}px; font-weight:${TYPE.fwMed}; color:${THEME.fg2}; background:${THEME.bg3}; border:1px solid ${THEME.line2}; border-radius:${RADIUS.r1}px; padding:0 5px; line-height:16px;`;
            badge.textContent = `×${n.count}`;
            header.appendChild(badge);
        }

        const close = document.createElement("button");
        close.setAttribute("aria-label", "Dismiss");
        close.textContent = "✕";
        close.style.cssText = `flex:0 0 auto; appearance:none; background:transparent; border:none; color:${THEME.fg2}; cursor:pointer; font-size:${TYPE.t12}px; line-height:1; padding:2px 2px; border-radius:${RADIUS.r1}px;`;
        close.addEventListener("mouseenter", () => { close.style.color = THEME.fg0; });
        close.addEventListener("mouseleave", () => { close.style.color = THEME.fg2; });
        close.addEventListener("click", () => dismiss(n.id));
        header.appendChild(close);
        body.appendChild(header);

        // ── Message (only when title used the verb) ──
        el._msgEl = null;
        const bodyText = n.verb ? n.message : n.detail;
        if (bodyText) {
            const msg = document.createElement("div");
            msg.style.cssText = `margin-top:3px; font-size:${TYPE.t11}px; color:${THEME.fg1}; word-break:break-word;`;
            msg.textContent = bodyText;
            body.appendChild(msg);
            el._msgEl = msg;
        }
        if (n.verb && n.detail) {
            const det = document.createElement("div");
            det.style.cssText = `margin-top:2px; font-size:${TYPE.t10}px; color:${THEME.fg2}; word-break:break-word;`;
            det.textContent = n.detail;
            body.appendChild(det);
        }

        // ── Progress ──
        if (n.tier === "progress") {
            const frac = progressFraction(n.progress);
            const label = formatProgress(n.progress);
            const wrap = document.createElement("div");
            wrap.style.cssText = `margin-top:7px; display:flex; align-items:center; gap:8px;`;

            const track = document.createElement("div");
            track.style.cssText = `position:relative; flex:1 1 auto; height:4px; background:${THEME.bg4}; border-radius:999px; overflow:hidden;`;
            const fill = document.createElement("div");
            if (frac == null) {
                // Indeterminate: a moving stripe.
                fill.className = "sonder-toast-indet-fill";
                fill.style.cssText = `position:absolute; top:0; bottom:0; width:40%; background:${accent}; border-radius:999px; animation:sonder-toast-indet 1.1s ${MOTION.ease} infinite;`;
            } else {
                fill.style.cssText = `position:absolute; left:0; top:0; bottom:0; width:${Math.round(frac * 100)}%; background:${accent}; border-radius:999px; transition:width ${MOTION.dur} ${MOTION.ease};`;
            }
            track.appendChild(fill);
            wrap.appendChild(track);

            const labelEl = document.createElement("span");
            labelEl.style.cssText = `flex:0 0 auto; font-size:${TYPE.t10}px; font-variant-numeric:tabular-nums; color:${THEME.fg2};`;
            if (label != null) {
                labelEl.textContent = label;
            } else {
                // Indeterminate → live elapsed counter.
                labelEl.dataset.elapsed = "1";
                labelEl.textContent = formatElapsed(Date.now() - n.createdAt);
            }
            wrap.appendChild(labelEl);
            body.appendChild(wrap);
        }

        // ── Actions (Retry on error + any custom actions) ──
        const actions = [];
        if (n.tier === "error" && typeof n.onRetry === "function") {
            actions.push({ label: "Retry", variant: "active", fn: () => { try { n.onRetry(); } finally { dismiss(n.id); } } });
        }
        if (Array.isArray(n.actions)) {
            for (const a of n.actions) {
                if (a && typeof a.fn === "function" && a.label) {
                    actions.push({ label: a.label, variant: a.variant || "muted", fn: () => { try { a.fn(); } finally { dismiss(n.id); } } });
                }
            }
        }
        if (actions.length) {
            const row = document.createElement("div");
            row.style.cssText = `margin-top:8px; display:flex; gap:6px; justify-content:flex-end;`;
            for (const a of actions) {
                const btn = document.createElement("button");
                btn.textContent = a.label;
                btn.style.cssText = chromeButtonCss({ variant: a.variant, padding: "3px 9px", fontSize: `${TYPE.t10}px` });
                btn.addEventListener("click", a.fn);
                row.appendChild(btn);
            }
            body.appendChild(row);
        }

        // Full text for right-click copy, and (re)apply collapse/expand state.
        el._copyText = [n.verb ? `${n.verb}: ${n.message}` : n.message, n.detail].filter(Boolean).join("\n");
        applyExpand(el);
    }

    function removeWithExit(el) {
        el.style.opacity = "0";
        el.style.transform = "translateX(12px)";
        window.setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 180);
    }

    function render(list) {
        const seen = new Set();
        let hasIndeterminate = false;
        for (const n of list) {
            seen.add(n.id);
            let el = els.get(n.id);
            const body = () => el.querySelector("[data-body]");
            if (!el) {
                el = createToast(n);
                els.set(n.id, el);
                container.appendChild(el);
            } else {
                renderInner(el, body(), n);
            }
            if (n.tier === "progress" && progressFraction(n.progress) == null) hasIndeterminate = true;
        }
        for (const [id, el] of els) {
            if (!seen.has(id)) {
                removeWithExit(el);
                els.delete(id);
            }
        }
        // Reorder DOM to match list order (oldest first → newest at the bottom).
        for (const n of list) {
            const el = els.get(n.id);
            if (el) container.appendChild(el);
        }
        ensureElapsedTimer(hasIndeterminate);
    }

    const unsubscribe = subscribe(render);

    return function cleanup() {
        unsubscribe();
        ensureElapsedTimer(false);
        if (container.parentNode) container.parentNode.removeChild(container);
        els.clear();
    };
}
