// A Controls view read after the caller asked, within a time bound
// (plans/controls-modes-phase-b.md §5). Each host supplies `attempt`: one read
// that resolves to the view it adopted, or null when that read was not adopted
// (the graph moved, an older read, an error). The whole call races a timer and
// never rejects; a read still running at the deadline may adopt later, which
// is harmless.
export const FRESH_VIEW_TIMEOUT_MS = 2000;
export const FRESH_VIEW_TRIES = 3;
// Between tries, so a mounted tab's retry can see the canvas republish.
export const FRESH_VIEW_BACKOFF_MS = 150;

export const usableView = view => !!view && view.state === "ready" && !view.error && !view.view_stale;

export async function freshWithin(getView, attempt, { timeoutMs = FRESH_VIEW_TIMEOUT_MS, tries = FRESH_VIEW_TRIES,
    backoffMs = FRESH_VIEW_BACKOFF_MS } = {}) {
    const deadline = Date.now() + timeoutMs;
    let expired = false, timer = null;
    const loop = (async () => {
        for (let i = 0; i < tries && !expired && Date.now() < deadline; i++) {
            if (i > 0) await new Promise(resolve => setTimeout(resolve, backoffMs));
            if (expired) break;
            try {
                const view = await attempt();
                if (usableView(view)) return { view, fresh: true };
            } catch (_) { /* the next try, or not fresh */ }
        }
        return null;
    })();
    const expiry = new Promise(resolve => { timer = setTimeout(() => { expired = true; resolve(null); }, timeoutMs); });
    try {
        return (await Promise.race([loop, expiry])) || { view: getView(), fresh: false };
    } finally {
        clearTimeout(timer);
    }
}
