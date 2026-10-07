// Sonder App setup state for the editor's Settings → Sonder App section:
// whether the SDK packages are installed, the install/uninstall job, and the
// Launcher address. One client per page, shared by every editor on it.
//
// The server owns the job (`server/sdk_install.py`); this polls its status
// while a job runs and says how it ended. No import-time side effects: ComfyUI
// imports every script under web/ on its canvas page.

import { notifyError, notifySuccess, notifyWarning } from "./editor_notifications.js";

export const SDK_SETUP_POLL_MS = 1500;
// The one browser window the editor's Launcher button reuses. The Launcher
// claims it; an SDK editor tab, which the Launcher navigates into, gives it up,
// so the button never navigates an open editor away.
export const LAUNCHER_WINDOW_NAME = "sonder-launcher";
// Why trust changes (linking a folder, turning links on) are unavailable in a browser on
// another machine. Equal to server/request_locality.py MACHINE_ONLY_MESSAGE (tested).
export const MACHINE_ONLY_MESSAGE = "Change this on the ComfyUI machine.";
// While a job this page started or saw is running, a failed status read backs
// off to this, and keeps trying: the job's end must still be announced.
export const SDK_SETUP_POLL_MAX_MS = 12000;

async function readJson(resp) {
    try {
        const payload = await resp.json();
        return payload && typeof payload === "object" ? payload : {};
    } catch (_err) {
        return {};
    }
}

export function createSdkSetupClient({
    fetchImpl = (...args) => globalThis.fetch(...args),
    apiURL = (path) => path,
    setTimeoutImpl = (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeoutImpl = (id) => globalThis.clearTimeout(id),
    pollMs = SDK_SETUP_POLL_MS,
    notify = { success: notifySuccess, warning: notifyWarning, error: notifyError },
} = {}) {
    const state = {
        loaded: false,
        loading: false,
        status: null,
        error: "",
        requesting: "",
    };
    const listeners = new Set();
    let pollTimer = null;
    let pollFailures = 0;
    let watchedJob = null; // the running job this page saw, so its end is announced once

    const emit = () => {
        for (const listener of [...listeners]) {
            try {
                listener(state);
            } catch (err) {
                console.warn("[Sonder] Sonder App setup listener failed:", err);
            }
        }
    };

    const announce = (job) => {
        const result = job?.result || {};
        const verb = job?.action === "uninstall" ? "Uninstall" : "Install";
        const detail = [result.detail, ...(Array.isArray(result.notes) ? result.notes : [])].filter(Boolean).join("\n") || null;
        if (job?.state === "done") {
            notify.success(result.message || `${verb} finished`, { detail, source: "sdk-setup" });
        } else {
            notify.error(result.message || `${verb} failed`, { detail, source: "sdk-setup" });
        }
    };

    const schedulePoll = () => {
        if (pollTimer != null) return;
        const delay = Math.min(pollMs * (2 ** pollFailures), Math.max(pollMs, SDK_SETUP_POLL_MAX_MS));
        pollTimer = setTimeoutImpl(() => {
            pollTimer = null;
            void refresh();
        }, delay);
    };

    async function refresh() {
        state.loading = true;
        emit();
        try {
            const resp = await fetchImpl(apiURL("/sonder-editor/sdk/status"));
            const payload = await readJson(resp);
            if (!resp.ok) throw new Error(payload.error || `HTTP ${resp.status}`);
            state.status = payload;
            state.loaded = true;
            state.error = "";
            pollFailures = 0;
            const job = payload.installer?.job || null;
            if (job?.state === "running") {
                watchedJob = { action: job.action, started_at: job.started_at };
                schedulePoll();
            } else if (watchedJob && job && job.started_at === watchedJob.started_at) {
                watchedJob = null;
                announce(job);
            }
        } catch (err) {
            state.error = "Couldn't read the Sonder App status.";
            console.warn("[Sonder] Sonder App status failed:", err);
            if (watchedJob) {
                pollFailures = Math.min(pollFailures + 1, 8);
                schedulePoll();
            }
        } finally {
            state.loading = false;
            emit();
        }
        return state;
    }

    async function start(action) {
        if (state.requesting) return state;
        state.requesting = action;
        emit();
        try {
            const resp = await fetchImpl(apiURL(`/sonder-editor/sdk/${action}`), { method: "POST" });
            const payload = await readJson(resp);
            if (!resp.ok) {
                const message = action === "uninstall" ? "Couldn't uninstall." : "Couldn't install.";
                notify.error(message, { detail: payload.error || `HTTP ${resp.status}`, source: "sdk-setup" });
            } else if (payload.job) {
                watchedJob = { action: payload.job.action, started_at: payload.job.started_at };
            }
        } catch (err) {
            notify.error("Couldn't reach ComfyUI.", { detail: String(err?.message || err), source: "sdk-setup" });
        } finally {
            state.requesting = "";
        }
        return refresh();
    }

    return {
        state,
        subscribe(listener) {
            if (typeof listener !== "function") return () => {};
            listeners.add(listener);
            return () => listeners.delete(listener);
        },
        refresh,
        install: () => start("install"),
        uninstall: () => start("uninstall"),
        dispose() {
            if (pollTimer != null) clearTimeoutImpl(pollTimer);
            pollTimer = null;
            listeners.clear();
        },
    };
}

let pageClient = null;

export function getSdkSetupClient() {
    if (!pageClient) {
        pageClient = createSdkSetupClient({
            apiURL: (path) => globalThis.window?.comfyAPI?.api?.api?.apiURL?.(path) || path,
        });
    }
    return pageClient;
}

// What the Settings section shows, from a status payload. Pure.
export function describeSdkSetup(status, { requesting = "", error = "" } = {}) {
    if (!status) {
        return { line: error || "Checking…", tone: error ? "error" : "idle", install: { disabled: true, title: "Checking…" },
            uninstall: { disabled: true, title: "Checking…" }, messages: [] };
    }
    const install = status.install || {};
    const installer = status.installer || {};
    const job = installer.job || {};
    const pinned = status.pinned || {};
    const running = job.state === "running" || !!requesting;
    const canManage = status.can_manage === true;
    const describePackage = (name, info) => {
        if (!info?.installed) return `${name} not installed`;
        if (!info.supported) return `${name} ${info.version} (unsupported; needs ${pinned[name] || "another version"})`;
        return `${name} ${info.version}`;
    };
    let line;
    let tone;
    if (running) {
        const verb = (job.action || requesting) === "uninstall" ? "Uninstalling" : "Installing";
        const minutes = Math.floor((job.elapsed_sec || 0) / 60);
        line = job.still_running
            ? `${verb}… still running after ${minutes} minutes. pip is slow or waiting; it can't be stopped from here.`
            : `${verb}…`;
        tone = "pending";
    } else if (install.ready) {
        line = `Installed: ${describePackage("comfy-sdk", install.sdk)}, ${describePackage("comfy-api-proxy", install.proxy)}.`;
        tone = "running";
    } else {
        line = `${describePackage("comfy-sdk", install.sdk)}; ${describePackage("comfy-api-proxy", install.proxy)}.`;
        tone = "idle";
    }
    const removable = Array.isArray(installer.installed_by_sonder) ? installer.installed_by_sonder : [];
    const notLocal = "Install from the ComfyUI machine";
    const installState = !canManage ? { disabled: true, title: notLocal }
        : running ? { disabled: true, title: "An install or uninstall is running" }
            : install.ready ? { disabled: true, title: "Both packages are installed" }
                : { disabled: false, title: `Install ${Object.entries(pinned).map(([n, v]) => `${n} ${v}`).join(" and ")} into ComfyUI's Python` };
    const uninstallState = !canManage ? { disabled: true, title: notLocal }
        : running ? { disabled: true, title: "An install or uninstall is running" }
            : !removable.length ? { disabled: true, title: "Sonder didn't install these packages, so it won't remove them" }
                : { disabled: false, title: `Remove ${removable.join(" and ")}, which Sonder installed` };
    const messages = [];
    if (!running && job.result && job.state === "failed") {
        messages.push({ tone: "error", text: job.result.message || "The last install or uninstall failed." });
        if (job.result.detail) messages.push({ tone: "detail", text: String(job.result.detail) });
        if (job.result.commands?.pip) {
            messages.push({ tone: "code", text: job.result.commands.pip });
            messages.push({ tone: "code", text: job.result.commands.uv });
        }
    }
    if (installer.interrupted) {
        messages.push({ tone: "warning", text: installer.interrupted.message });
        if (installer.interrupted.commands?.pip) messages.push({ tone: "code", text: installer.interrupted.commands.pip });
    }
    if (!running && job.state === "done" && Array.isArray(job.result?.notes)) {
        for (const note of job.result.notes) messages.push({ tone: "detail", text: note });
    }
    if (!canManage) messages.push({ tone: "detail", text: "Install and Uninstall work only in a browser on the ComfyUI machine." });
    // The status shown is the last one read; say so when the latest read failed.
    if (error) messages.unshift({ tone: "error", text: `${error} What's shown may be out of date.` });
    return { line, tone, install: installState, uninstall: uninstallState, messages };
}
