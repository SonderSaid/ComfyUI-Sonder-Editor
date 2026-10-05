"""Settings → Sonder App (SDK v2 Phase 4c), driven under Node with the real modules.

* ``describeSdkSetup``: the status line and why Install or Uninstall is greyed
  out (not on the ComfyUI machine, a job running, already installed, nothing
  Sonder installed), the failure and interrupted-install messages with the
  commands to run by hand.
* ``createSdkSetupClient``: polls while a job runs and announces how it ended
  once; a failed status read during a job keeps polling, backing off, and the
  panel says the status shown may be stale; a refused request is a toast with
  the server's reason in ``detail``.
* A status poll repaints only the Sonder App section (pinned wiring).
* The toolbar's Launcher button follows the browser-local setting.
* The settings default is off.

The Settings section itself is looked at live (Phase 4c manual rows).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"

SCRIPT = r"""
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.window = { comfyAPI: { api: { api: { apiURL: (p) => p } } }, localStorage: globalThis.localStorage,
    addEventListener() {}, removeEventListener() {} };
globalThis.document = { createElement: () => ({ style: {}, dataset: {}, setAttribute() {}, addEventListener() {} }),
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {} };
const setup = await import(__SETUP__);
const chrome = await import(__CHROME__);
const settings = await import(__SETTINGS__);
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };
const out = {};

const installed = { sdk: { installed: true, version: "0.4.0", supported: true }, proxy: { installed: true, version: "0.1.6", supported: true }, ready: true };
const missing = { sdk: { installed: true, version: "0.4.0", supported: true }, proxy: { installed: false, version: "", supported: false }, ready: false };
const idleJob = { state: "idle", action: "", elapsed_sec: 0, still_running: false, result: null };
const pinned = { "comfy-sdk": "0.4.0", "comfy-api-proxy": "0.1.6" };
const status = (over = {}) => ({ install: missing, pinned, can_manage: true, launcher_url: "/sonder-editor/app",
    installer: { job: idleJob, installed_by_sonder: [], added_dependencies: [], interrupted: null }, ...over });

out.views = {
    loading: setup.describeSdkSetup(null),
    error: setup.describeSdkSetup(null, { error: "Couldn't read the Sonder App status." }),
    missing: setup.describeSdkSetup(status()),
    ready_external: setup.describeSdkSetup(status({ install: installed })),
    ready_ours: setup.describeSdkSetup(status({ install: installed, installer: { job: idleJob, installed_by_sonder: ["comfy-api-proxy"], interrupted: null } })),
    remote: setup.describeSdkSetup(status({ can_manage: false })),
    running: setup.describeSdkSetup(status({ installer: { job: { ...idleJob, state: "running", action: "install", elapsed_sec: 12 }, installed_by_sonder: [] } })),
    slow: setup.describeSdkSetup(status({ installer: { job: { ...idleJob, state: "running", action: "uninstall", elapsed_sec: 660, still_running: true }, installed_by_sonder: [] } })),
    requesting: setup.describeSdkSetup(status(), { requesting: "install" }),
    unsupported: setup.describeSdkSetup(status({ install: { ...missing, sdk: { installed: true, version: "0.3.1", supported: false } } })),
    failed_manual: setup.describeSdkSetup(status({ installer: { job: { ...idleJob, state: "failed", action: "install",
        result: { ok: false, code: "manual", message: "pip isn't available.", commands: { pip: "py -m pip install x", uv: "uv pip install x" } } }, installed_by_sonder: [] } })),
    interrupted: setup.describeSdkSetup(status({ installer: { job: idleJob, installed_by_sonder: [],
        interrupted: { message: "An install didn't finish.", commands: { pip: "py -m pip install x" } } } })),
    done_notes: setup.describeSdkSetup(status({ install: installed, installer: { job: { ...idleJob, state: "done", action: "uninstall",
        result: { ok: true, notes: ["comfy-sdk stays loaded until ComfyUI restarts."] } }, installed_by_sonder: [] } })),
    stale: setup.describeSdkSetup(status({ installer: { job: { ...idleJob, state: "running", action: "install", elapsed_sec: 3 }, installed_by_sonder: [] } }),
        { error: "Couldn't read the Sonder App status." }),
};

// ── The client ────────────────────────────────────────────────────────────
{
    const toasts = [];
    const timers = [];
    let responses = [];
    const calls = [];
    const client = setup.createSdkSetupClient({
        fetchImpl: async (url, init = {}) => {
            calls.push(`${init.method || "GET"} ${url}`);
            const next = responses.shift();
            if (next instanceof Error) throw next;
            return { ok: next.status < 400, status: next.status, json: async () => next.body };
        },
        setTimeoutImpl: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        clearTimeoutImpl: () => {},
        notify: {
            success: (m, o) => toasts.push(["success", m, o.detail]),
            warning: (m, o) => toasts.push(["warning", m, o.detail]),
            error: (m, o) => toasts.push(["error", m, o.detail]),
        },
    });
    const seen = [];
    client.subscribe((s) => seen.push(s.requesting || (s.loading ? "loading" : "idle")));
    const runningJob = { state: "running", action: "install", started_at: 50, elapsed_sec: 1 };
    responses = [
        { status: 202, body: { ok: true, job: runningJob } },
        { status: 200, body: status({ installer: { job: runningJob, installed_by_sonder: [] } }) },
    ];
    await client.install();
    out.client_start = { calls: [...calls], polls: timers.length, poll_ms: timers[0]?.ms, toasts: [...toasts] };
    responses = [{ status: 200, body: status({ install: installed, installer: { job: { state: "done", action: "install", started_at: 50,
        result: { ok: true, message: "Installed comfy-api-proxy 0.1.6.", notes: ["Launcher shortcut: X"] } }, installed_by_sonder: ["comfy-api-proxy"] } }) }];
    timers.shift().fn();
    await settle();
    out.client_done = { toasts: [...toasts], polls: timers.length, ready: client.state.status.install.ready };
    // A later status read of the same finished job does not announce it again.
    responses = [{ status: 200, body: status({ install: installed, installer: { job: { state: "done", action: "install", started_at: 50, result: { ok: true } }, installed_by_sonder: [] } }) }];
    await client.refresh();
    out.client_no_repeat = toasts.length;
    // A refused request: the reason in detail, then the status is read again.
    responses = [{ status: 409, body: { ok: false, code: "runs_active", error: "Takes are still generating." } },
        { status: 200, body: status() }];
    await client.uninstall();
    out.client_refused = toasts.at(-1);
    responses = [new Error("offline")];
    const originalWarn = console.warn; console.warn = () => {};
    await client.refresh();
    console.warn = originalWarn;
    out.client_offline = { error: client.state.error, kept: !!client.state.status, polls: timers.length };
    out.client_seen_requesting = seen.includes("install") && seen.includes("uninstall");
    // A failed read while a job runs: polling continues, backing off, and the
    // job's end is still announced once the server answers again.
    const laterJob = { state: "running", action: "uninstall", started_at: 60, elapsed_sec: 1 };
    responses = [{ status: 202, body: { ok: true, job: laterJob } }, new Error("offline")];
    console.warn = () => {};
    await client.uninstall();
    out.client_job_offline = { polls: timers.length, ms: timers[0]?.ms, error: client.state.error };
    responses = [new Error("offline")];
    timers.shift().fn();
    await settle();
    out.client_job_backoff = { polls: timers.length, ms: timers[0]?.ms };
    responses = [{ status: 200, body: status({ installer: { job: { state: "done", action: "uninstall", started_at: 60,
        result: { ok: true, message: "Removed comfy-api-proxy." } }, installed_by_sonder: [] } }) }];
    timers.shift().fn();
    await settle();
    console.warn = originalWarn;
    out.client_job_recovered = { toast: toasts.at(-1), polls: timers.length, error: client.state.error };
}

// ── Launcher button and the setting ───────────────────────────────────────
{
    const button = { style: { display: "" } };
    chrome.syncLauncherButton({ _launcherBtn: button, _settings: { sdk: { showLauncherButton: false } } });
    const off = button.style.display;
    chrome.syncLauncherButton({ _launcherBtn: button, _settings: { sdk: { showLauncherButton: true } } });
    const on = button.style.display;
    chrome.syncLauncherButton({ _launcherBtn: button, _settings: {} });
    out.launcher_button = { off, on, missing: button.style.display };
    out.setting = {
        default: settings.DEFAULT_EDITOR_SETTINGS.sdk.showLauncherButton,
        normalized_on: settings.normalizeEditorSettings({ sdk: { showLauncherButton: 1 } }).sdk.showLauncherButton,
        normalized_absent: settings.normalizeEditorSettings({}).sdk.showLauncherButton,
    };
}

console.log(JSON.stringify(out));
process.exit(0);
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Sonder App setup tests")
    script = (SCRIPT
              .replace("__SETUP__", json.dumps((JS / "sdk_setup_client.js").as_uri()))
              .replace("__CHROME__", json.dumps((JS / "editor_top_chrome.js").as_uri()))
              .replace("__SETTINGS__", json.dumps((JS / "editor_settings.js").as_uri())))
    result = subprocess.run([node, "--input-type=module"], input=script, capture_output=True,
                            text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_status_line_and_buttons(results):
    v = results["views"]
    assert v["loading"]["line"] == "Checking…" and v["loading"]["install"]["disabled"] is True
    assert v["error"]["tone"] == "error"
    assert v["missing"]["line"] == "comfy-sdk 0.4.0; comfy-api-proxy not installed."
    assert v["missing"]["install"] == {"disabled": False, "title": "Install comfy-sdk 0.4.0 and comfy-api-proxy 0.1.6 into ComfyUI's Python"}
    assert v["missing"]["uninstall"]["disabled"] is True
    assert "didn't install" in v["missing"]["uninstall"]["title"]
    assert v["ready_external"]["line"] == "Installed: comfy-sdk 0.4.0, comfy-api-proxy 0.1.6."
    assert v["ready_external"]["install"] == {"disabled": True, "title": "Both packages are installed"}
    assert v["ready_ours"]["uninstall"] == {"disabled": False, "title": "Remove comfy-api-proxy, which Sonder installed"}
    assert v["unsupported"]["line"].startswith("comfy-sdk 0.3.1 (unsupported; needs 0.4.0)")


def test_install_is_greyed_out_away_from_the_comfyui_machine(results):
    remote = results["views"]["remote"]
    assert remote["install"] == {"disabled": True, "title": "Install from the ComfyUI machine"}
    assert remote["uninstall"] == {"disabled": True, "title": "Install from the ComfyUI machine"}
    assert {"tone": "detail", "text": "Install and Uninstall work only in a browser on the ComfyUI machine."} in remote["messages"]


def test_a_running_job_disables_both_and_says_when_it_is_slow(results):
    v = results["views"]
    assert v["running"]["line"] == "Installing…" and v["running"]["tone"] == "pending"
    assert v["running"]["install"]["disabled"] is True and v["running"]["uninstall"]["disabled"] is True
    assert v["slow"]["line"].startswith("Uninstalling… still running after 11 minutes.")
    assert v["requesting"]["install"]["disabled"] is True


def test_failure_and_interruption_show_the_commands(results):
    failed = results["views"]["failed_manual"]["messages"]
    assert failed[0] == {"tone": "error", "text": "pip isn't available."}
    assert {"tone": "code", "text": "py -m pip install x"} in failed and {"tone": "code", "text": "uv pip install x"} in failed
    interrupted = results["views"]["interrupted"]["messages"]
    assert interrupted == [{"tone": "warning", "text": "An install didn't finish."}, {"tone": "code", "text": "py -m pip install x"}]
    assert results["views"]["done_notes"]["messages"] == [{"tone": "detail", "text": "comfy-sdk stays loaded until ComfyUI restarts."}]


def test_a_failed_read_says_the_status_shown_may_be_stale(results):
    stale = results["views"]["stale"]
    assert stale["line"] == "Installing…"
    assert stale["messages"][0] == {"tone": "error", "text": "Couldn't read the Sonder App status. What's shown may be out of date."}


def test_the_client_polls_a_running_job_and_announces_its_end_once(results):
    start = results["client_start"]
    assert start["calls"] == ["POST /sonder-editor/sdk/install", "GET /sonder-editor/sdk/status"]
    assert start["polls"] == 1 and start["poll_ms"] == 1500 and start["toasts"] == []
    done = results["client_done"]
    assert done["toasts"] == [["success", "Installed comfy-api-proxy 0.1.6.", "Launcher shortcut: X"]]
    assert done["polls"] == 0 and done["ready"] is True
    assert results["client_no_repeat"] == 1


def test_a_refused_request_is_a_toast_with_the_reason(results):
    assert results["client_refused"] == ["error", "Couldn't uninstall.", "Takes are still generating."]
    # No job is watched, so a failed read schedules nothing.
    assert results["client_offline"] == {"error": "Couldn't read the Sonder App status.", "kept": True, "polls": 0}
    assert results["client_seen_requesting"] is True


def test_a_failed_read_during_a_job_keeps_polling_and_still_announces_its_end(results):
    assert results["client_job_offline"] == {"polls": 1, "ms": 3000, "error": "Couldn't read the Sonder App status."}
    assert results["client_job_backoff"] == {"polls": 1, "ms": 6000}
    recovered = results["client_job_recovered"]
    assert recovered["toast"] == ["success", "Removed comfy-api-proxy.", None]
    assert recovered["polls"] == 0 and recovered["error"] == ""


def test_the_launcher_button_follows_the_setting(results):
    assert results["launcher_button"] == {"off": "none", "on": "inline-flex", "missing": "none"}
    assert results["setting"] == {"default": False, "normalized_on": True, "normalized_absent": False}


def test_the_settings_section_reaches_the_host():
    """Pinned wiring: the panel calls these through the adapter (durable rule)."""
    panel = (JS / "editor_settings_panel.js").read_text(encoding="utf-8")
    widget = (JS / "editor_widget.js").read_text(encoding="utf-8")
    for member in ("_sdkSetup", "_loadSdkSetup", "_installSdk", "_uninstallSdk", "_openSonderLauncher"):
        assert f"this._{member[1:]}" in panel or f"host.{member}" in panel, member
        assert member in widget.split("_createSettingsPanelHost() {", 1)[1].split("\n    }\n", 1)[0], member
    # A status poll repaints only the Sonder App section: a full panel sync
    # rebuilds the template forms and loses what is being typed there.
    assert "syncSdkSetup: () => syncSdkSetupControls.call(host)" in panel
    assert "getSdkSetupClient().subscribe(() => this._settingsPanelHandle?.syncSdkSetup?.())" in widget
    # The last section the panel creates (Phase 4 Decision 2).
    sections = panel.split("createSection(")
    assert sections[-1].lstrip().startswith('"Sonder App (experimental)"')
