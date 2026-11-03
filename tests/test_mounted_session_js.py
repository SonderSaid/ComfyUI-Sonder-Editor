"""The mounted tab's identity, refusals and displacement, and what makes it read the
queue when a run ends (0.8 hardening Phase 8). `tab_entry.js` runs a page on import, so
its wiring is read from source; its behavior is in the modules and in the live checks."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"

SCRIPT = r"""
import assert from "node:assert/strict";
const session = await import(MOUNTED_URL);
const ids = await import(IDS_URL);
const out = {};
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };

// Ids and secrets work without crypto.randomUUID (an insecure origin).
{
    const realRandomUUID = globalThis.crypto.randomUUID;
    Object.defineProperty(globalThis.crypto, "randomUUID", { configurable: true, value: undefined });
    out.ids = { token: ids.randomToken(32).length, session: /^tab-[0-9a-f]{32}$/.test(ids.newSessionId("tab")),
        differ: ids.randomToken() !== ids.randomToken(),
        // The shape the run lane's route checks (`_RUN_REQUEST_ID`).
        uuid: /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(ids.randomUuid()) };
    Object.defineProperty(globalThis.crypto, "randomUUID", { configurable: true, value: realRandomUUID });
}

// The secret: minted once, kept, and blocked storage never throws.
{
    const map = new Map();
    const store = { getItem: k => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, v) };
    const before = session.storedMountedSecret(store);
    const first = session.mountedSecret(store);
    const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
    out.secret = { before, length: first.length, same: session.mountedSecret(store) === first,
        stored: session.storedMountedSecret(store) === first,
        blocked: [session.mountedSecret(blocked), session.storedMountedSecret(blocked), session.mountedSecret(null), session.storedMountedSecret(null)] };
}

out.refusals = {
    none: session.mountedRefusalText(null),
    fullscreen: session.mountedRefusalText({ host_mode: "fullscreen" }),
    tab: session.mountedRefusalText({ host_mode: "tab" }),
};

// What a session change or a heartbeat answer means. Swapping these branches would
// tear down a live tab, so the decision is a table.
out.owner_actions = {
    own: session.ownerChangeAction({ session_tag: "me", host_mode: "tab", status: "orphaned" }, "me"),
    other_tab: session.ownerChangeAction({ session_tag: "them", host_mode: "tab" }, "me"),
    fullscreen: session.ownerChangeAction({ session_tag: "them", host_mode: "fullscreen" }, "me"),
    nobody: session.ownerChangeAction(null, "me"),
    no_tag_yet: session.ownerChangeAction({ session_tag: "", host_mode: "fullscreen" }, ""),
};
out.heartbeat_actions = {
    ok: session.heartbeatAction({ ok: true, owner: { status: "active" } }),
    other_tab: session.heartbeatAction({ ok: false, code: "locked", owner: { host_mode: "tab" } }),
    fullscreen: session.heartbeatAction({ ok: false, code: "locked", owner: { host_mode: "fullscreen" } }),
    no_owner: session.heartbeatAction({ ok: false, code: "no_owner", owner: null }),
};

// Reads a run's end and a socket reopen trigger, on a fake timer.
{
    const reads = [];
    let pending = null, cleared = 0;
    const driver = session.createRunReadDriver({ refresh: keys => reads.push(keys),
        setTimer: (fn) => { pending = fn; return 1; }, clearTimer: () => { pending = null; cleared += 1; } });
    const fire = () => { const fn = pending; pending = null; fn?.(); };
    // No version check anywhere: a run's end reads whatever the guard thinks.
    driver.connection("connecting");
    driver.connection("open");
    const firstOpen = pending === null;
    driver.runFinished({ leased_job_ids: [], take_saved: false });
    fire();
    // A burst becomes one read of everything it asked for.
    driver.runFinished({ leased_job_ids: [], take_saved: false });
    driver.runFinished({ leased_job_ids: ["j"], take_saved: false });
    driver.runFinished({ leased_job_ids: [], take_saved: true });
    fire();
    driver.connection("reconnecting");
    driver.connection("open");
    fire();
    const beforeStop = reads.length;
    driver.runFinished({});
    driver.stop();
    driver.runFinished({});
    driver.connection("open");
    out.run_reads = { firstOpen, reads, beforeStop, afterStop: reads.length, cleared, pending: pending === null };
}

// Displacement: keys stop first, then work, the blocker shows, pending work drains,
// then the editor goes and the keys are released. It happens once, and a throwing
// step never strands the rest.
{
    const log = [];
    let finishDrain;
    const displace = session.createPageDisplacement({
        blockKeys: () => { log.push("block"); return () => log.push("unblock"); },
        stopWork: () => log.push("stop"),
        drain: () => new Promise(r => { log.push("drain"); finishDrain = r; }),
        destroyEditor: () => log.push("destroy"),
        show: () => log.push("show"),
    });
    const before = displace.isDisplaced();
    const first = displace();
    const second = displace();
    await settle();
    const whileDraining = [...log];
    finishDrain();
    await settle();
    out.displace = { before, first, second, after: displace.isDisplaced(), whileDraining, log };

    const failing = [];
    const realWarn = console.warn; console.warn = () => {};
    const broken = session.createPageDisplacement({
        stopWork: () => { throw new Error("stop failed"); },
        drain: () => { throw new Error("drain failed"); },
        destroyEditor: () => failing.push("destroy"),
        show: () => failing.push("show"),
    });
    broken();
    await settle();
    console.warn = realWarn;
    out.displace_failing = failing;

    // A drain that never answers doesn't keep the editor alive.
    const hung = [];
    let timeout = null;
    session.createPageDisplacement({
        stopWork() {}, show() {},
        drain: () => new Promise(() => {}),
        destroyEditor: () => hung.push("destroy"),
        setTimer: (fn, ms) => { timeout = { fn, ms }; },
    })();
    await settle();
    const beforeTimeout = [...hung];
    timeout.fn();
    await settle();
    out.displace_hung = { beforeTimeout, after: hung, ms: timeout.ms };
}

out.keys = {
    plain: session.runFinishedRefreshKeys({ outcome: "success", leased_job_ids: [], take_saved: false }),
    queued: session.runFinishedRefreshKeys({ leased_job_ids: ["j1"], take_saved: false }),
    saved: session.runFinishedRefreshKeys({ leased_job_ids: [], take_saved: true }),
    missing: session.runFinishedRefreshKeys(undefined),
};

// The guard race, on the real handler from tab_entry.js: a read that returns the
// newest version inside the debounce makes it skip, so only run_finished can read.
{
    const source = HANDLER_SOURCE;
    const timers = [];
    const realSet = globalThis.setTimeout, realClear = globalThis.clearTimeout;
    const refreshes = [];
    const editor = { refresh: keys => refreshes.push(keys) };
    let version = "v1";
    const make = new Function("projectId", "tabDiagRecord", "setTimeout", "clearTimeout", source + "\nreturn createProjectUpdatedHandler;");
    const handler = make("P", () => {}, (fn) => { timers.push(fn); return timers.length; }, () => {})(editor, () => version);
    handler({ modified_at: "v2" });
    version = "v2";  // a scene or assets read came back with the new version
    timers.at(-1)();
    const afterGuard = refreshes.length;
    handler({ modified_at: "v3" });
    timers.at(-1)();  // a genuinely newer version still refreshes
    out.guard = { afterGuard, afterNewer: refreshes.length };
}

// A refresh while a project mutation is pending defers, so it isn't lost.
{
    const start = REFRESH_SOURCE;
    const proto = new Function("return ({" + start + "})")();
    const log = [];
    const widget = { _hasPendingProjectMutations: () => true, _deferProjectBackedRefresh: (keys) => log.push(["defer", keys]),
        _fetchRenderQueue: () => log.push(["queue"]) };
    proto.refresh.call(widget, ["queue"]);
    const idle = [];
    proto.refresh.call({ _hasPendingProjectMutations: () => false, _fetchRenderQueue: () => idle.push("queue") }, ["queue"]);
    out.refresh = { pending: log, idle };
}

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    tab = (JS / "tab_entry.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    handler = tab[tab.index("function createProjectUpdatedHandler("):tab.index("function mountToastsOnce(")]
    widget = (JS / "editor_widget.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    refresh = widget[widget.index("    refresh(keys = [], options = {}) {"):widget.index("    /** A staged member's Library record")]
    header = (f"const MOUNTED_URL={json.dumps((JS / 'mounted_session.js').as_uri())};"
              f"const IDS_URL={json.dumps((JS / 'session_ids.js').as_uri())};"
              f"const HANDLER_SOURCE={json.dumps(handler)};"
              f"const REFRESH_SOURCE={json.dumps(refresh)};")
    run = subprocess.run([node, "--input-type=module", "-e", header + SCRIPT], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_ids_and_secrets_work_on_an_insecure_origin(results):
    assert results["ids"] == {"token": 64, "session": True, "differ": True, "uuid": True}


def test_the_secret_is_minted_once_and_blocked_storage_never_throws(results):
    assert results["secret"] == {"before": "", "length": 64, "same": True, "stored": True, "blocked": ["", "", "", ""]}


def test_refusals_say_who_holds_the_editor(results):
    assert "link has expired" in results["refusals"]["none"]
    assert "Mount in Tab" in results["refusals"]["none"]
    assert "fullscreen on the ComfyUI page" in results["refusals"]["fullscreen"]
    assert results["refusals"]["tab"] == "This editor is open in another tab."


def test_a_displaced_page_stops_then_drains_then_closes_once(results):
    assert results["displace"] == {
        "before": False, "first": True, "second": False, "after": True,
        "whileDraining": ["block", "stop", "show", "drain"],
        "log": ["block", "stop", "show", "drain", "destroy", "unblock"]}
    assert results["displace_failing"] == ["show", "destroy"]
    assert results["displace_hung"] == {"beforeTimeout": [], "after": ["destroy"], "ms": 10000}


def test_only_another_tab_displaces_a_mounted_tab(results):
    assert results["owner_actions"] == {"own": "own", "other_tab": "displaced", "fullscreen": "released",
                                        "nobody": "released", "no_tag_yet": "released"}
    assert results["heartbeat_actions"] == {"ok": "own", "other_tab": "displaced", "fullscreen": "released",
                                            "no_owner": "released"}


def test_a_run_end_always_reads_the_queue_and_a_burst_reads_once(results):
    # Fails with the guard as the only trigger: nothing here looks at a version.
    assert results["run_reads"]["firstOpen"] is True
    reads = results["run_reads"]["reads"]
    assert reads[0] == ["queue"]
    assert sorted(reads[1]) == ["assets", "queue", "scenes"]
    assert reads[2] == ["queue"]  # the reopen
    assert results["run_reads"]["beforeStop"] == 3
    assert results["run_reads"]["afterStop"] == 3 and results["run_reads"]["cleared"] == 1


def test_a_run_end_reads_the_queue_and_more_only_when_it_saved_or_held_a_job(results):
    assert results["keys"]["plain"] == ["queue"] == results["keys"]["missing"]
    assert results["keys"]["queued"] == ["queue", "scenes", "assets"] == results["keys"]["saved"]


def test_the_version_guard_skips_a_version_another_read_already_saw(results):
    # Why run_finished has to read: this skip is the G7-F01 stale panel.
    assert results["guard"] == {"afterGuard": 0, "afterNewer": 1}


def test_a_run_end_refresh_during_a_mutation_defers_instead_of_dropping(results):
    assert results["refresh"]["pending"] == [["defer", ["queue"]]]
    assert results["refresh"]["idle"] == ["queue"]


def _tab_source():
    return (JS / "tab_entry.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_both_tab_pages_wire_the_read_driver():
    source = _tab_source()
    assert source.count("onRunFinished: (event) => runReads.runFinished(event),") == 2
    assert source.count("runReads.connection(state);") == 2
    assert source.count("runReads.stop();") == 2
    assert source.count("onProjectUpdated.cancel();") == 2


def test_the_mounted_tab_claims_before_it_reads_and_words_refusals_by_owner():
    source = _tab_source()
    main = source[source.index("async function main()"):]
    assert main.index("claimEditorSession(projectId, sessionId") < main.index("initialState = await getEditorWidgetState")
    assert "setStatus(mountedRefusalText(claim.owner));" in main
    assert "already open in another editor host" not in source
    assert "reclaim_secret: mountedSecret(tabStorage)," in main
    assert "!handoffToken && !storedMountedSecret(tabStorage)" in main


def test_both_tab_pages_handle_displacement_and_the_back_forward_cache():
    source = _tab_source()
    app = source[source.index("async function runSdkTab()"):source.index("async function main()")]
    main = source[source.index("async function main()"):]
    for page in (app, main):
        assert "createPageDisplacement({" in page
        assert 'onClick: () => window.location.reload()' in page
        assert "if (displace.isDisplaced()) return;" in page[page.index('addEventListener("pagehide"'):]
        assert "event.persisted && !displace.isDisplaced()" in page
        assert "blockKeys: () => blockEditorKeys(registerKeyboardConsumer, KEYBOARD_PRIORITY)," in page
    assert 'if (state === "displaced") displace();' in app
    assert "const action = heartbeatAction(result);" in main
    assert "const action = ownerChangeAction(owner, sessionTag);" in main
    assert main.count('action === "displaced"') == 2
    assert "runFeedback.destroy();" in main
    assert "if (displace.isDisplaced()) return;\n        if (code === \"canvas_host_disconnected\")" in main


def test_no_page_sends_or_compares_a_public_session_id():
    for name in ("tab_entry.js", "editor_node_controller.js", "sdk_host.js", "controls_host_remote.js", "controls_canvas_relay.js"):
        source = (JS / name).read_text(encoding="utf-8")
        assert not re.search(r"(owner|host|row|pending|event|payload|normalized)\??\.session_id", source), name
    controller = (JS / "editor_node_controller.js").read_text(encoding="utf-8")
    assert "browser_instance_id: this._editorSessionId" not in controller
    assert "sonder-editor-tab-${this._editorSessionId}" not in controller
    assert "browser_instance_id: sessionId" not in _tab_source()
    assert "|| sessionId," not in _tab_source()


def test_force_release_needs_no_id_from_the_owner():
    source = (JS / "editor_node_controller.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    method = source[source.index("    async forceReleaseOwner(owner) {"):source.index("    _startSessionHeartbeat() {")]
    assert "owner?.session_id" not in method
    assert "releaseEditorSession(projectId, this._editorSessionId, true," in method


def test_the_tab_gives_its_claim_back_if_it_cannot_read_its_values():
    main = _tab_source()[_tab_source().index("async function main()"):]
    read = main[main.index("let initialState;"):main.index("const host = new TabWidgetHost(")]
    assert "releaseEditorSession(projectId, sessionId, false, hostId, sourceNodeId)" in read
    assert "return;" in read


def test_the_subscribed_reply_names_only_the_callers_own_tag():
    routes = (ROOT / "server" / "routes.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert '"session_tag": session_tag(data.get("session_id"))}' in routes


def test_the_panel_says_the_canvas_is_gone():
    panel = (JS / "editor_controls_panel.js").read_text(encoding="utf-8")
    assert "The canvas is not connected. Your Controls come back" in panel
    assert 'view.state === "disconnected" ? CANVAS_DISCONNECTED' in panel
