// The Sonder App's read of which Mode choices can't run (`GET .../availability`).
// The host owns this; canvas and mounted hosts have none, so the Modes table draws
// as it always did there. It depends on no project data, and a project edit never
// triggers it. Subscribers are told only when the answer's content changed, and
// never through the host's main emit, which would re-render the panel and make
// Views read for an overlay that changes neither.
import { hasEntries, namesANode } from "./controls_modes_availability.js";

export const AVAILABILITY_DEBOUNCE_MS = 250;
// A Controls write moves the seq, so typing asks often and each answer is work on
// the server: wait for a pause.
export const AVAILABILITY_WRITE_DEBOUNCE_MS = 1000;

// `request(fresh)` answers the route's body or throws; `active()` is whether anyone
// can see the answer; `viewSeq()` the Controls view's seq; `templateId()` the
// session's workflow.
export function createAvailabilityReader({ request, loadPacks = async () => null, active, viewSeq, templateId }) {
    const listeners = new Set();
    let overlay = null, overlayKey = "null", packs, destroyed = false, timer = null, timerDelay = 0,
        running = null, queued = false, wantFresh = false;
    const changed = () => { for (const cb of [...listeners]) cb(); };
    const keep = next => {
        overlay = next;
        const key = next ? JSON.stringify([next.run, next.choices]) : "null";
        if (key === overlayKey) return;
        overlayKey = key;
        changed();
    };
    const fetchPacks = () => {
        if (packs !== undefined || !namesANode(overlay)) return;
        packs = null;
        void Promise.resolve().then(loadPacks).then(found => {
            if (destroyed || !found) return;
            packs = found;
            changed();
        }).catch(() => {});
    };
    // True only for an answer this page adopted.
    async function readOnce(fresh) {
        let payload = null;
        try {
            payload = await request(fresh);
        } catch (_) {
            payload = null;
        }
        if (destroyed) return false;
        // A failed read shows nothing grey: Generate still refuses.
        if (!payload || payload.template_id !== templateId()) {
            keep(null);
            return false;
        }
        // Older than the view on screen: the read that view scheduled is due.
        if (Number(payload.seq) < viewSeq()) return false;
        keep(payload);
        fetchPacks();
        return true;
    }
    function pump() {
        if (running) return running;
        running = (async () => {
            let adopted = false;
            do {
                const fresh = wantFresh;
                wantFresh = false;
                queued = false;
                adopted = await readOnce(fresh);
            } while (queued && !destroyed);
            return adopted;
        })().finally(() => { running = null; });
        return running;
    }
    return {
        get: () => overlay,
        packs: () => packs,
        hasEntries: () => hasEntries(overlay),
        subscribe(cb) {
            listeners.add(cb);
            return () => listeners.delete(cb);
        },
        // A trigger: debounced, one read in flight, one queued, no polling.
        schedule(fresh = false, delayMs = AVAILABILITY_DEBOUNCE_MS) {
            if (destroyed || !active()) return;
            wantFresh = wantFresh || !!fresh;
            // The panel showing and its first view arrive together: a write's longer
            // wait never pushes back a sooner read already due.
            if (timer !== null && timerDelay < delayMs) return;
            clearTimeout(timer);
            timerDelay = delayMs;
            timer = setTimeout(() => {
                timer = null;
                if (destroyed) return;
                if (running) queued = true;
                else void pump();
            }, delayMs);
        },
        // A read begun after this call, within `timeoutMs`. Resolves true when it
        // was adopted; false when it failed or ran out of time (which may adopt
        // later, harmlessly). Never rejects.
        refresh({ fresh = false, timeoutMs = 3000 } = {}) {
            if (destroyed) return Promise.resolve(false);
            wantFresh = wantFresh || !!fresh;
            if (running) queued = true;
            const work = pump();
            let expiry = null;
            return Promise.race([work, new Promise(resolve => { expiry = setTimeout(() => resolve(false), timeoutMs); })])
                .finally(() => clearTimeout(expiry));
        },
        destroy() {
            destroyed = true;
            clearTimeout(timer);
            timer = null;
            listeners.clear();
        },
    };
}
