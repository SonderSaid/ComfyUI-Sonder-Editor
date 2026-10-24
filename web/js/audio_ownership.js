// One audible source per editor page. The timeline is audible by default;
// unmuting a pane claims the sound, muting the timeline and every other pane.
// Muting, closing or tearing down the owner returns the sound to the timeline.
// An owner whose media becomes unavailable stays the owner, silent: ownership
// never moves on its own. Gallery Inspect suspends the pane owner (`suspend`)
// and restores it on close. Pure: the host applies mute state through
// `setTimelineMuted` and each pane through its subscription.

export const TIMELINE = "timeline";

export function createAudioOwnership({ setTimelineMuted = () => {} } = {}) {
    let owner = TIMELINE;
    let suspended = false;
    const listeners = new Set();
    const apply = () => {
        // Inspect suspends a pane owner only; the timeline keeps its own behaviour.
        try { setTimelineMuted(owner !== TIMELINE); } catch (error) { console.warn("[Sonder] timeline mute failed:", error); }
        for (const listener of [...listeners]) {
            try { listener(owner, suspended); } catch (error) { console.warn("[Sonder] audio owner listener failed:", error); }
        }
    };
    return {
        owner: () => owner,
        // Whether `id` may sound now.
        audible: id => owner === id && (id === TIMELINE || !suspended),
        claim(id) {
            const next = String(id || TIMELINE);
            if (next === owner) return;
            owner = next;
            apply();
        },
        // Only the owner gives the sound back; anyone else releasing is a no-op.
        release(id) {
            if (String(id) !== owner || owner === TIMELINE) return;
            owner = TIMELINE;
            apply();
        },
        // Another overlay (gallery Inspect) has the sound for a while.
        suspend(on) {
            if (suspended === !!on) return;
            suspended = !!on;
            apply();
        },
        subscribe(listener) {
            if (typeof listener !== "function") return () => {};
            listeners.add(listener);
            return () => listeners.delete(listener);
        },
        destroy() {
            listeners.clear();
            owner = TIMELINE;
            suspended = false;
            try { setTimelineMuted(false); } catch (_error) { /* the surface is gone */ }
        },
    };
}
