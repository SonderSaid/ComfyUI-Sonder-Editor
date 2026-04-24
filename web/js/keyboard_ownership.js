/**
 * KeyboardOwnership — single window-capture root that beats LiteGraph's
 * document-capture handlers regardless of registration order.
 *
 * Consumers register with a priority. On each event, consumers are dispatched
 * highest priority first; same priority dispatches LIFO (last-registered wins).
 * The first consumer to return `true` consumes the event — root then calls
 * `stopImmediatePropagation` + `preventDefault` so neither LiteGraph nor any
 * other document-level listener sees it. Returning `false` lets dispatch
 * continue to the next consumer; if every consumer returns `false`, the event
 * propagates normally.
 *
 * Element-level listeners attached to the actual event target still run after
 * the root pass when the root does not consume — window capture does not
 * cancel bubble-phase listeners on its own.
 */

export const PRIORITY = Object.freeze({
    OVERLAY: 100,
    GALLERY: 50,
    EDITOR: 10,
});

const MODULE_VERSION = "2026-04-23-debug-probe";
const consumers = [];
let attached = false;

export function isKeyboardDebugEnabled() {
    if (typeof window === "undefined") return false;
    try {
        return window.__SONDER_KEYBOARD_DEBUG__ === true
            || window.localStorage?.getItem("sonder_keyboard_debug") === "1";
    } catch {
        return window.__SONDER_KEYBOARD_DEBUG__ === true;
    }
}

function describeDebugTarget(target) {
    if (!target) return "null";
    const tag = String(target.tagName || target.nodeName || "").toLowerCase();
    if (!tag) return String(target);
    const id = target.id ? `#${target.id}` : "";
    const classes = typeof target.className === "string" && target.className.trim()
        ? `.${target.className.trim().split(/\s+/).slice(0, 3).join(".")}`
        : "";
    return `${tag}${id}${classes}`;
}

function eventSummary(event) {
    return {
        key: event?.key ?? "",
        ctrl: !!event?.ctrlKey,
        meta: !!event?.metaKey,
        shift: !!event?.shiftKey,
        alt: !!event?.altKey,
        target: describeDebugTarget(event?.target),
    };
}

function debugLog(message, details) {
    if (!isKeyboardDebugEnabled()) return;
    if (details === undefined) {
        console.log("[Sonder][KeyboardOwnership]", message);
        return;
    }
    console.log("[Sonder][KeyboardOwnership]", message, details);
}

function installDebugProbe() {
    if (typeof window === "undefined") return;
    window.__SONDER_KEYBOARD_OWNERSHIP__ = {
        version: MODULE_VERSION,
        isAttached: () => attached,
        listConsumers: () => _debugListConsumers(),
        isDebugEnabled: () => isKeyboardDebugEnabled(),
    };
    debugLog("module evaluated", {
        version: MODULE_VERSION,
    });
}

function dispatch(eventName, event) {
    if (!consumers.length) return;
    const ordered = consumers.slice().sort((a, b) => {
        if (b.priority !== a.priority) return b.priority - a.priority;
        return b.registeredAt - a.registeredAt;
    });
    debugLog(`dispatch ${eventName}`, {
        event: eventSummary(event),
        consumers: ordered.map((consumer) => ({
            id: consumer.id,
            priority: consumer.priority,
        })),
    });
    for (const consumer of ordered) {
        const handler = consumer[eventName];
        if (typeof handler !== "function") continue;
        let result;
        try {
            result = handler(event);
        } catch (err) {
            console.error(`[KeyboardOwnership] consumer ${consumer.id} threw on ${eventName}:`, err);
            continue;
        }
        debugLog(`consumer ${consumer.id} ${eventName}`, {
            event: eventSummary(event),
            result: result === true ? "consume" : (result === false ? "pass" : String(result)),
        });
        if (result === true) {
            debugLog(`consumed ${eventName}`, {
                consumerId: consumer.id,
                event: eventSummary(event),
            });
            event.stopImmediatePropagation();
            event.preventDefault();
            return;
        }
    }
    debugLog(`unclaimed ${eventName}`, {
        event: eventSummary(event),
    });
}

function onKeydown(event) { dispatch("keydown", event); }
function onKeyup(event) { dispatch("keyup", event); }

function attachIfNeeded() {
    if (attached) return;
    window.addEventListener("keydown", onKeydown, true);
    window.addEventListener("keyup", onKeyup, true);
    attached = true;
    debugLog("attached window listeners");
}

function detachIfIdle() {
    if (!attached || consumers.length) return;
    window.removeEventListener("keydown", onKeydown, true);
    window.removeEventListener("keyup", onKeyup, true);
    attached = false;
    debugLog("detached window listeners");
}

let registrationCounter = 0;

/**
 * Register a keyboard consumer.
 *
 * @param {object} options
 * @param {string} options.id - Stable identifier for diagnostics (must include
 *   the owning widget's node id when there can be more than one widget).
 * @param {number} options.priority - Higher wins. Use the PRIORITY constants.
 * @param {(event: KeyboardEvent) => boolean} [options.keydown] - Return true
 *   to consume; false to let dispatch continue.
 * @param {(event: KeyboardEvent) => boolean} [options.keyup] - Same contract.
 * @returns {() => void} unregister closure (idempotent).
 */
export function register({ id, priority, keydown, keyup }) {
    if (typeof id !== "string" || !id) {
        throw new Error("[KeyboardOwnership] register requires a string id");
    }
    if (typeof priority !== "number") {
        throw new Error("[KeyboardOwnership] register requires a numeric priority");
    }
    const consumer = {
        id,
        priority,
        keydown: typeof keydown === "function" ? keydown : null,
        keyup: typeof keyup === "function" ? keyup : null,
        registeredAt: ++registrationCounter,
    };
    consumers.push(consumer);
    attachIfNeeded();
    debugLog("registered consumer", {
        id: consumer.id,
        priority: consumer.priority,
        keydown: !!consumer.keydown,
        keyup: !!consumer.keyup,
        totalConsumers: consumers.length,
    });

    let active = true;
    return function unregister() {
        if (!active) return;
        active = false;
        const idx = consumers.indexOf(consumer);
        if (idx !== -1) consumers.splice(idx, 1);
        debugLog("unregistered consumer", {
            id: consumer.id,
            remainingConsumers: consumers.length,
        });
        detachIfIdle();
    };
}

export function _debugListConsumers() {
    return consumers.map((c) => ({ id: c.id, priority: c.priority }));
}

installDebugProbe();
