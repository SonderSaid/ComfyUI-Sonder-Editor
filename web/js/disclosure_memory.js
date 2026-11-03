// Browser-local disclosure state. It remembers presentation only; callers
// must never use it as project or render authority.

function resolveStore(storage) {
    if (storage) return storage;
    try {
        return globalThis.localStorage;
    } catch (_error) {
        // Some privacy/sandbox contexts throw while resolving localStorage.
        return null;
    }
}

export function createDisclosureMemory(storageKey, { storage = null } = {}) {
    const store = resolveStore(storage);
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

// Browser-local presentation numbers (a text box's height), one per key, the
// `limit` most recently remembered kept. Same rules as disclosure state: never
// project or render authority, and a write patches its own key over what is
// stored now, so two open panels keep each other's values.
export function createValueMemory(storageKey, { storage = null, limit = 64, now = () => Date.now() } = {}) {
    const store = resolveStore(storage);
    const name = String(storageKey || "");
    const read = () => {
        try {
            const parsed = JSON.parse(store?.getItem?.(name) || "{}");
            return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch (_error) {
            return {};
        }
    };
    let values = read();
    const entry = record => (record && typeof record === "object" && Number.isFinite(record.v) ? record : null);
    return {
        value(key) {
            return entry(values[String(key || "")])?.v ?? null;
        },
        remember(key, value) {
            const id = String(key || "");
            if (!id || !Number.isFinite(value)) return;
            const merged = { ...read(), [id]: { v: value, ts: now() } };
            const kept = Object.entries(merged)
                .filter(([, record]) => entry(record))
                .sort((a, b) => (Number(a[1].ts) || 0) - (Number(b[1].ts) || 0))
                .slice(-Math.max(1, limit));
            values = Object.fromEntries(kept);
            try {
                store?.setItem?.(name, JSON.stringify(values));
            } catch (_error) {
                // Optional presentation state.
            }
        },
    };
}
