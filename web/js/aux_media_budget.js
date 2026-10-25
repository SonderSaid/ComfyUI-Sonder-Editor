// The page's budget for Output View follower seeks (Phase 3B): at most two in
// flight at once, latest-wins per holder (a pane's newer request replaces its
// queued one), and none granted while the request's editor clock rebuffers, so
// panes never compete with the main surface's own decode for the clock. The
// surface's playbackDecodeLimiter is separate and untouched: panes never
// starve the editor clock.
//
// A grant is held only around one seek (the currentTime write to `seeked`).
// Release is idempotent; a 3 s watchdog releases a grant its holder never
// gave back, so one stuck element can't block every pane on the page. A
// request is cancelled when its follower goes away.

export const MAX_IN_FLIGHT = 2;
export const WATCHDOG_MS = 3000;

export function createAuxMediaBudget({ maxInFlight = MAX_IN_FLIGHT, watchdogMs = WATCHDOG_MS,
    setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = id => clearTimeout(id) } = {}) {
    const queue = []; // {holder, clock, resolve, unsubscribe, settled}
    let inFlight = 0;
    const stats = { granted: 0, superseded: 0, watchdog: 0, cancelled: 0 };

    const blocked = request => request.clock?.get?.()?.hold === "rebuffer";
    const settle = (request, value) => {
        if (request.settled) return;
        request.settled = true;
        request.unsubscribe?.();
        request.unsubscribe = null;
        request.resolve(value);
    };

    function grant(request) {
        inFlight += 1;
        stats.granted += 1;
        let released = false;
        let timer = null;
        const release = () => {
            if (released) return;
            released = true;
            clearTimer(timer);
            inFlight -= 1;
            pump();
        };
        timer = setTimer(() => { if (!released) { stats.watchdog += 1; release(); } }, watchdogMs);
        timer?.unref?.();
        request.release = release;
        settle(request, release);
    }

    function pump() {
        while (inFlight < maxInFlight) {
            const index = queue.findIndex(request => !blocked(request));
            if (index === -1) return;
            const [request] = queue.splice(index, 1);
            grant(request);
        }
    }

    return {
        // A seek slot for `holder`: {promise -> release()|null, cancel()}. null
        // means superseded by the holder's newer request, or cancelled.
        request(holder, { clock = null } = {}) {
            for (let i = queue.length - 1; i >= 0; i--) {
                if (queue[i].holder !== holder) continue;
                const [old] = queue.splice(i, 1);
                stats.superseded += 1;
                settle(old, null);
            }
            let resolve;
            const promise = new Promise(fn => { resolve = fn; });
            const request = { holder, clock, resolve, settled: false, release: null, unsubscribe: null };
            // A rebuffering clock is waited out by its own events, never polled.
            request.unsubscribe = clock?.subscribe?.(() => pump()) || null;
            queue.push(request);
            pump();
            return {
                promise,
                cancel() {
                    const index = queue.indexOf(request);
                    if (index !== -1) {
                        queue.splice(index, 1);
                        stats.cancelled += 1;
                        settle(request, null);
                    } else request.release?.();
                },
            };
        },
        snapshot: () => ({ inFlight, queued: queue.length, ...stats }),
    };
}

// One budget per page: every editor and every pane on it share it.
export const pageBudget = createAuxMediaBudget();
