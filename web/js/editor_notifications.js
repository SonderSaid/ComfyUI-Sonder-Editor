// Surface-agnostic notification Core (the bus) for the Sonder Editor.
//
// Leaf module: DOM-free, no editor-surface imports — importable from anywhere
// (fullscreen widget, persistent tab, dormant controller, gallery, extension
// hooks). Per `durable_rules.md` > Notification System, ALL user-facing status
// routes through here; presenters (toast stack, foreground pill) subscribe and
// render. This module owns notification state + lifecycle timers + coalescing;
// it never touches the DOM or the theme.
//
// Lazy-initialized singleton (no eager module-level state) so a static importer
// cannot trip the editor_settings.js-class TDZ trap (durable_rules.md > Technical
// Traps). State is created on first use via `_ensure()`.

// ── Tiers ────────────────────────────────────────────────────────────────────
export const TIERS = Object.freeze(["info", "progress", "success", "warning", "error"]);

// Auto-dismiss lifetimes (ms). 0 / null = sticky until resolved/dismissed.
// Defaults: info/success auto-dismiss, progress/warning/error sticky. The
// browser-local settings layer overrides these via configureNotifications()
// (kept as a pushed setter so the Core stays import-free / TDZ-safe).
let _config = {
    info: 4000,
    success: 5000,
    progress: null, // always sticky until resolved
    warning: null,  // sticky until dismissed
    error: null,    // sticky until dismissed
};

function _ttlForTier(tier) {
    if (tier === "progress") return null;
    const v = _config[tier];
    return typeof v === "number" && v > 0 ? v : null;
}

function _ttlOverride(value) {
    const v = Number(value);
    return Number.isFinite(v) && v > 0 ? v : null;
}

// Push lifetime overrides (ms; 0 = sticky). Only known keys are applied.
export function configureNotifications(patch = {}) {
    for (const key of ["info", "success", "warning", "error"]) {
        if (key in patch) {
            const v = Number(patch[key]);
            _config[key] = Number.isFinite(v) && v > 0 ? v : null;
        }
    }
}

let _state = null;

function _ensure() {
    if (_state) return _state;
    _state = {
        seq: 0,
        notifications: new Map(), // id -> notif
        timers: new Map(),        // id -> timeout handle
        listeners: new Set(),     // (list) => void
        fgListeners: new Set(),   // (item|null) => void
    };
    return _state;
}

function _now() {
    // App runtime (browser) — Date.now() is fine here; the Workflow-script
    // restriction on Date.now() does not apply to editor code.
    return Date.now();
}

function clamp01(n) {
    return Math.max(0, Math.min(1, n));
}

// ── Progress formatting (shared by pill + toast so labels stay identical) ──────
export function formatProgress(progress) {
    if (progress == null) return null;
    if (typeof progress === "number") {
        return `${Math.round(clamp01(progress) * 100)}%`;
    }
    if (typeof progress === "object" && Number(progress.total) > 0) {
        const unit = progress.unit || "";
        return `${progress.current ?? 0}/${progress.total}${unit}`;
    }
    return null; // indeterminate
}

// Returns a 0..1 fraction for a determinate bar, or null for indeterminate.
export function progressFraction(progress) {
    if (typeof progress === "number") return clamp01(progress);
    if (progress && typeof progress === "object" && Number(progress.total) > 0) {
        return clamp01((progress.current ?? 0) / progress.total);
    }
    return null;
}

// ── Public view (clones — presenters must not mutate Core state) ───────────────
function _publicView(notif) {
    return {
        id: notif.id,
        tier: notif.tier,
        verb: notif.verb,
        message: notif.message,
        detail: notif.detail,
        progress: notif.progress,
        foreground: notif.foreground,
        source: notif.source,
        onRetry: notif.onRetry,
        actions: notif.actions,
        count: notif.count,
        createdAt: notif.createdAt,
        updatedAt: notif.updatedAt,
    };
}

function _list(s) {
    // Oldest first → presenter appends newest at the bottom of the stack.
    return Array.from(s.notifications.values())
        .sort((a, b) => a.createdAt - b.createdAt)
        .map(_publicView);
}

function _foreground(s) {
    let best = null;
    for (const n of s.notifications.values()) {
        if (!n.foreground || n.tier !== "progress") continue;
        if (!best || n.updatedAt > best.updatedAt) best = n;
    }
    return best ? _publicView(best) : null;
}

function _emit(s) {
    const list = _list(s);
    for (const fn of s.listeners) {
        try { fn(list); } catch (e) { console.error("[Sonder] notification listener failed", e); }
    }
    const fg = _foreground(s);
    for (const fn of s.fgListeners) {
        try { fn(fg); } catch (e) { console.error("[Sonder] foreground listener failed", e); }
    }
}

// ── Timer lifecycle (Core-owned so timing survives surface swaps) ──────────────
function _clearTimer(s, id) {
    const t = s.timers.get(id);
    if (t != null) {
        clearTimeout(t);
        s.timers.delete(id);
    }
}

function _arm(s, notif, { reset = false } = {}) {
    _clearTimer(s, notif.id);
    if (notif._ttl == null) return; // sticky
    if (reset || notif._remaining == null) notif._remaining = notif._ttl;
    notif._armedAt = _now();
    s.timers.set(notif.id, setTimeout(() => {
        _clearTimer(s, notif.id);
        if (s.notifications.delete(notif.id)) _emit(s);
    }, notif._remaining));
}

function _coalesceKey(opts, tier) {
    if (opts.source) return `src:${opts.source}`;
    return `${tier}:${opts.message ?? opts.verb ?? ""}`;
}

function _findByKey(s, key) {
    for (const n of s.notifications.values()) {
        if (n._key === key) return n;
    }
    return null;
}

function _applyOpts(notif, opts) {
    if ("tier" in opts && opts.tier) notif.tier = opts.tier;
    if ("verb" in opts) notif.verb = opts.verb ?? null;
    if ("message" in opts) notif.message = opts.message ?? "";
    if ("detail" in opts) notif.detail = opts.detail ?? null;
    if ("progress" in opts) notif.progress = opts.progress ?? null;
    if ("foreground" in opts) notif.foreground = !!opts.foreground;
    if ("onRetry" in opts) notif.onRetry = opts.onRetry ?? null;
    if ("actions" in opts) notif.actions = opts.actions ?? null;
    if ("durationMs" in opts) {
        notif._hasDurationOverride = true;
        notif._durationOverride = _ttlOverride(opts.durationMs);
    }
    notif._ttl = notif._hasDurationOverride ? notif._durationOverride : _ttlForTier(notif.tier);
}

// ── Core API ───────────────────────────────────────────────────────────────────

// notify({ tier, verb, message, detail, progress, foreground, source, onRetry, actions, durationMs })
// Returns a handle: { id, update, progress, resolve, dismiss }.
export function notify(opts = {}) {
    const s = _ensure();
    const tier = TIERS.includes(opts.tier) ? opts.tier : "info";
    const key = _coalesceKey(opts, tier);

    // Coalesce: an identical key still on screen bumps count + refreshes instead
    // of stacking a duplicate. Same-tier only — a failure must not silently fold
    // into a warning of the same text.
    const existing = _findByKey(s, key);
    if (existing && existing.tier === tier) {
        existing.count += 1;
        existing.updatedAt = _now();
        _applyOpts(existing, { ...opts, tier });
        _arm(s, existing, { reset: true });
        _emit(s);
        return _handle(existing.id);
    }

    const id = `sn${++s.seq}`;
    const hasDurationOverride = "durationMs" in opts;
    const notif = {
        id,
        tier,
        verb: opts.verb ?? null,
        message: opts.message ?? "",
        detail: opts.detail ?? null,
        progress: opts.progress ?? null,
        foreground: !!opts.foreground,
        source: opts.source ?? null,
        onRetry: opts.onRetry ?? null,
        actions: opts.actions ?? null,
        count: 1,
        createdAt: _now(),
        updatedAt: _now(),
        _key: key,
        _hasDurationOverride: hasDurationOverride,
        _durationOverride: hasDurationOverride ? _ttlOverride(opts.durationMs) : null,
        _ttl: hasDurationOverride ? _ttlOverride(opts.durationMs) : _ttlForTier(tier),
        _remaining: null,
        _armedAt: 0,
    };
    s.notifications.set(id, notif);
    _arm(s, notif, { reset: true });
    _emit(s);
    return _handle(id);
}

function _update(id, patch = {}) {
    const s = _ensure();
    const notif = s.notifications.get(id);
    if (!notif) return _handle(id);
    _applyOpts(notif, patch);
    notif.updatedAt = _now();
    _arm(s, notif, { reset: true });
    _emit(s);
    return _handle(id);
}

// Resolve an in-flight (usually progress) notification into a terminal tier.
// Defaults to success; clears progress so the bar/pill stops.
function _resolve(id, patch = {}) {
    const s = _ensure();
    const notif = s.notifications.get(id);
    if (!notif) return _handle(id);
    const tier = TIERS.includes(patch.tier) ? patch.tier : "success";
    // Clear verb by default so a resolved item reads as its message
    // (title "Export complete", not "Exporting: Export complete"); a caller may
    // still override by passing verb in the patch.
    _applyOpts(notif, { verb: null, ...patch, tier, progress: null, foreground: false });
    notif.updatedAt = _now();
    _arm(s, notif, { reset: true });
    _emit(s);
    return _handle(id);
}

export function dismiss(id) {
    const s = _ensure();
    _clearTimer(s, id);
    if (s.notifications.delete(id)) _emit(s);
}

// Hover-pause hooks for presenters: pause freezes the auto-dismiss countdown,
// resume continues it from the remaining time.
export function pause(id) {
    const s = _ensure();
    const notif = s.notifications.get(id);
    if (!notif || notif._ttl == null || !s.timers.has(id)) return;
    const elapsed = _now() - notif._armedAt;
    notif._remaining = Math.max(0, (notif._remaining ?? notif._ttl) - elapsed);
    _clearTimer(s, id);
}

export function resume(id) {
    const s = _ensure();
    const notif = s.notifications.get(id);
    if (!notif || notif._ttl == null || s.timers.has(id)) return;
    _arm(s, notif, { reset: false });
}

function _handle(id) {
    return {
        id,
        update: (patch) => _update(id, patch),
        progress: (value) => _update(id, { progress: value }),
        resolve: (patch) => _resolve(id, patch),
        dismiss: () => dismiss(id),
    };
}

// ── Subscriptions ──────────────────────────────────────────────────────────────
// subscribe(fn): fn(list) on every change; returns an unsubscribe fn. Fires once
// immediately with the current list.
export function subscribe(fn) {
    const s = _ensure();
    s.listeners.add(fn);
    try { fn(_list(s)); } catch (e) { console.error("[Sonder] notification subscribe failed", e); }
    return () => s.listeners.delete(fn);
}

// subscribeForeground(fn): fn(item|null) with the single highest-priority active
// foreground progress item (most recently updated wins).
export function subscribeForeground(fn) {
    const s = _ensure();
    s.fgListeners.add(fn);
    try { fn(_foreground(s)); } catch (e) { console.error("[Sonder] foreground subscribe failed", e); }
    return () => s.fgListeners.delete(fn);
}

// ── Convenience sugar ───────────────────────────────────────────────────────────
export function notifyInfo(message, opts = {}) { return notify({ ...opts, tier: "info", message }); }
export function notifySuccess(message, opts = {}) { return notify({ ...opts, tier: "success", message }); }
export function notifyWarning(message, opts = {}) { return notify({ ...opts, tier: "warning", message }); }
export function notifyError(message, opts = {}) { return notify({ ...opts, tier: "error", message }); }
export function notifyProgress(opts = {}) { return notify({ ...opts, tier: "progress" }); }

// Test/diagnostic helper — not for production paths.
export function _resetForTest() {
    if (!_state) return;
    for (const t of _state.timers.values()) clearTimeout(t);
    _state = null;
}
