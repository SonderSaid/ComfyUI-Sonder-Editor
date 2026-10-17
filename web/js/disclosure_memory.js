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
    const read = () => {
        try {
            const parsed = JSON.parse(store?.getItem?.(String(storageKey || "")) || "{}");
            return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch (_error) {
            return {};
        }
    };
    let values = read();
    // Patches one key over what is stored now, so two open panels (a canvas
    // editor and a mounted tab) do not overwrite each other's keys.
    const persist = (name, open) => {
        values = { ...values, [name]: open };
        try {
            store?.setItem?.(String(storageKey || ""), JSON.stringify({ ...read(), [name]: open }));
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
            persist(String(key || ""), !!open);
        },
    };
}
