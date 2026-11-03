// The mounted tab's identity across a reload, what a page does when another tab
// takes its session over, and what it reads when a run ends. DOM-free: callers pass
// storage, timers and the page's own work.

import { randomToken } from "./session_ids.js";

const SECRET_KEY = "sonder.mountedTabSecret";
const RUN_READ_DELAY_MS = 250;
const DISPLACED_DRAIN_TIMEOUT_MS = 10000;

export function storedMountedSecret(storage) {
    try {
        return storage?.getItem(SECRET_KEY) || "";
    } catch (_err) {
        return "";
    }
}

/**
 * This tab's reclaim secret, minted before its first claim so a reload during that
 * claim still holds it. Tab identity, not network auth: a duplicated tab copies it,
 * and blocked storage answers "" (the tab then claims by handoff alone).
 */
export function mountedSecret(storage) {
    const stored = storedMountedSecret(storage);
    if (stored) return stored;
    try {
        const secret = randomToken(32);
        storage.setItem(SECRET_KEY, secret);
        return secret;
    } catch (_err) {
        return "";
    }
}

/** Why this tab can't open the editor, from the owner every refusal carries. */
export function mountedRefusalText(owner) {
    if (owner?.host_mode === "tab") return "This editor is open in another tab.";
    if (owner) return "This editor is open in fullscreen on the ComfyUI page. Choose ✕ Exit or Mount in Tab there.";
    return "This tab's link has expired. In ComfyUI, open the Editor, then choose Mount in Tab.";
}

/**
 * What a `session_changed` owner means to a mounted tab: "own" (still this tab),
 * "displaced" (another tab took over), or "released". Only another *tab* displaces:
 * after a Force Release the owner is nobody, or the canvas's fullscreen editor.
 */
export function ownerChangeAction(owner, sessionTag) {
    if (sessionTag && owner?.session_tag === sessionTag) return "own";
    return owner?.host_mode === "tab" ? "displaced" : "released";
}

/** The same question for a heartbeat answer; the server says ok only to the owner. */
export function heartbeatAction(result) {
    if (result?.ok) return "own";
    return result?.code === "locked" && result?.owner?.host_mode === "tab" ? "displaced" : "released";
}

/**
 * What a page with no canvas of its own reads when a run ends, after its jobs have
 * settled. The `project_updated` guard can't be the only trigger: any project read
 * that returns the newest version inside its debounce makes it skip the refresh.
 */
export function runFinishedRefreshKeys(event) {
    return event?.take_saved || event?.leased_job_ids?.length ? ["queue", "scenes", "assets"] : ["queue"];
}

/**
 * The reads a run's end and a socket reopen trigger: one per burst, whatever each
 * one asked for. A reopen reads because project events sent while the socket was
 * down are gone; the first open has missed nothing. Neither is an edit, so these are
 * not gated by a change certificate (`architecture.md`, ungated reads).
 */
export function createRunReadDriver({
    refresh,
    delayMs = RUN_READ_DELAY_MS,
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = id => clearTimeout(id),
}) {
    const keys = new Set();
    let timer = null, openedOnce = false, stopped = false;
    const schedule = (wanted) => {
        if (stopped) return;
        for (const key of wanted) keys.add(key);
        if (timer != null) return;
        timer = setTimer(() => {
            timer = null;
            const reading = [...keys];
            keys.clear();
            refresh(reading);
        }, delayMs);
    };
    return {
        runFinished: event => schedule(runFinishedRefreshKeys(event)),
        connection(state) {
            if (state !== "open") return;
            if (openedOnce) schedule(["queue"]);
            openedOnce = true;
        },
        stop() {
            stopped = true;
            if (timer != null) clearTimer(timer);
            timer = null;
        },
    };
}

/**
 * Another tab holds this session now, so this page stops being an editor. Project
 * writes aren't tied to a session and a blocker doesn't stop the keyboard, so a live
 * editor behind one could still change the project: keys stop at once, pending work
 * drains for a bounded time, and the editor goes. Take over reloads the page, and
 * the claim starts everything fresh.
 *
 * Returns the function to call; later calls do nothing.
 */
export function createPageDisplacement({
    stopWork,
    drain,
    destroyEditor,
    show,
    blockKeys = () => () => {},
    drainTimeoutMs = DISPLACED_DRAIN_TIMEOUT_MS,
    setTimer = (fn, ms) => setTimeout(fn, ms),
}) {
    let displaced = false;
    const attempt = (label, step) => {
        try {
            return step();
        } catch (error) {
            console.warn(`[Sonder] ${label} on a displaced page failed:`, error);
            return null;
        }
    };
    const displace = () => {
        if (displaced) return false;
        displaced = true;
        const unblockKeys = attempt("Blocking keys", blockKeys);
        attempt("Stopping work", stopWork);
        attempt("Showing the blocker", show);
        void Promise.race([
            Promise.resolve().then(drain),
            new Promise(resolve => setTimer(resolve, drainTimeoutMs)),
        ]).catch(() => {}).finally(() => {
            attempt("Closing the editor", destroyEditor);
            if (unblockKeys) attempt("Releasing keys", unblockKeys);
        });
        return true;
    };
    displace.isDisplaced = () => displaced;
    return displace;
}
