// Browser-local disclosure state. It remembers presentation only; callers
// must never use it as project or render authority.

export function createDisclosureMemory(storageKey, { storage = null } = {}) {
    let store = storage;
    if (!store) {
        try {
            store = globalThis.localStorage;
        } catch (_error) {
            // Some privacy/sandbox contexts throw while resolving localStorage.
            store = null;
        }
    }
    let values = {};
    try {
        const parsed = JSON.parse(store?.getItem?.(String(storageKey || "")) || "{}");
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            values = parsed;
        }
    } catch (_error) {
        values = {};
    }
    const persist = () => {
        try {
            store?.setItem?.(String(storageKey || ""), JSON.stringify(values));
        } catch (_error) {
            // Local disclosure memory is optional presentation state.
        }
    };
    return {
        isOpen(key, fallback = false) {
            const name = String(key || "");
            return Object.hasOwn(values, name) ? !!values[name] : !!fallback;
        },
        remember(key, open) {
            values[String(key || "")] = !!open;
            persist();
        },
    };
}
