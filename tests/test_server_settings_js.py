"""Settings ▸ Server in the editor: Share Projects Between ComfyUI Servers.

The methods are sliced from ``editor_widget.js`` and the sync block from
``editor_settings_panel.js``, and run in Node with ``fetch`` scripted.
"""
import re
from pathlib import Path

from test_project_mutation_queue import _run_node

ROOT = Path(__file__).resolve().parents[1]
WIDGET = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
PANEL = (ROOT / "web/js/editor_settings_panel.js").read_text(encoding="utf-8")


def _method(name: str) -> str:
    return re.search(r"^    (?:async )?" + name + r"\(.*?^    \}\n", WIDGET, re.M | re.S)[0]


_SYNC = re.search(r"^    if \(controls\.allowExternalProjectLinks\) \{.*?^    if \(controls\.shareProjectsBetweenServers\) \{.*?^    \}\n",
                  PANEL, re.M | re.S)[0]

_HARNESS = """
    import assert from 'node:assert/strict';
    const MACHINE_ONLY_MESSAGE = 'Change this on the ComfyUI machine.';
    const api = { apiURL: (path) => path };
    const withEditorMutationDiagnostics = (init) => init;
    const notes = [];
    const notifySuccess = (message, opts = {}) => notes.push(['ok', message, opts.detail || '']);
    const notifyError = (message, opts = {}) => notes.push(['error', message, opts.detail || '']);
    const console = { warn: () => {} };
    // Each request waits for the test to answer it.
    const requests = [];
    globalThis.fetch = (url, init = {}) => new Promise((resolve) => {
        requests.push({ method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null,
            answer: (status, data) => resolve({ ok: status < 400, status, json: async () => data }) });
    });
    const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
    const all = (links, share) => ({ allow_external_project_links: links, share_projects_between_servers: share, can_manage: true });
    class Editor {
        constructor() {
            this._serverSettings = null; this._serverSettingsLoaded = false;
            this._serverSettingsEpoch = 0; this._serverSettingsWrites = null;
            this.refreshes = 0; this.syncs = 0;
        }
        _syncSettingsPanelControls() { this.syncs += 1; }
        async _refreshProjectChoicesAndSwitch() { this.refreshes += 1; }
""" + "".join(_method(name) for name in (
    "_loadServerSettings", "_keepServerSettingsAfterStaleLoad", "_putServerSettings",
    "_setAllowExternalProjectLinksWithinGesture",
    "_setShareProjectsBetweenServersWithinGesture")) + """
    }
    function syncPanel(controls) {
""" + _SYNC + """
    }
"""


def test_the_toggle_sends_only_its_setting_and_does_not_refresh_projects():
    _run_node(_HARNESS + """
        const editor = new Editor();
        const done = editor._setShareProjectsBetweenServersWithinGesture({}, true);
        await settle();
        assert.equal(requests.length, 1);
        assert.deepEqual([requests[0].method, requests[0].body], ['PUT', { share_projects_between_servers: true }]);
        requests[0].answer(200, all(false, true));
        await done;
        assert.equal(editor._serverSettings.share_projects_between_servers, true);
        assert.equal(editor.refreshes, 0);
        assert.deepEqual(notes.at(-1), ['ok', 'Project sharing turned on', 'Turn it on in the other ComfyUI servers too.']);
        // A refusal says what the server said and keeps the held settings.
        const refused = editor._setShareProjectsBetweenServersWithinGesture({}, false);
        await settle();
        requests[1].answer(409, { error: "The projects folder is on a drive that doesn't support file locks." });
        await refused;
        assert.deepEqual(notes.at(-1), ['error', "Couldn't turn off project sharing",
            "The projects folder is on a drive that doesn't support file locks."]);
        assert.equal(editor._serverSettings.share_projects_between_servers, true);
    """)


def test_a_links_change_keeps_sharing_and_writes_run_one_at_a_time():
    _run_node(_HARNESS + """
        const editor = new Editor();
        editor._serverSettings = all(false, true); editor._serverSettingsLoaded = true;
        const first = editor._setAllowExternalProjectLinksWithinGesture({}, true);
        const second = editor._setShareProjectsBetweenServersWithinGesture({}, false);
        await settle();
        assert.equal(requests.length, 1, 'the second write waits for the first');
        requests[0].answer(200, all(true, true));
        await settle(); await settle();
        assert.equal(editor._serverSettings.share_projects_between_servers, true, 'the links answer keeps sharing on');
        assert.equal(requests.length, 2);
        requests[1].answer(200, all(true, false));
        await Promise.all([first, second]);
        assert.deepEqual(editor._serverSettings, all(true, false));
    """)


def test_a_load_that_overlapped_a_write_is_discarded():
    _run_node(_HARNESS + """
        const editor = new Editor();
        const load = editor._loadServerSettings();
        await settle();
        const write = editor._setShareProjectsBetweenServersWithinGesture({}, true);
        await settle();
        const [get, put] = requests;
        put.answer(200, all(false, true));
        await write;
        get.answer(200, all(false, false));  // read before the write landed
        await load;
        assert.equal(editor._serverSettings.share_projects_between_servers, true);
        assert.equal(editor._serverSettingsLoaded, true);
        // A load with no write in between is used.
        const fresh = editor._loadServerSettings();
        await settle();
        requests[2].answer(200, all(true, true));
        assert.deepEqual(await fresh, all(true, true));
    """)


def test_a_load_that_overlapped_a_refused_write_leaves_the_panel_usable():
    _run_node(_HARNESS + """
        const editor = new Editor();
        editor._serverSettings = all(false, false); editor._serverSettingsLoaded = true;
        const write = editor._setShareProjectsBetweenServersWithinGesture({}, true);
        await settle();
        const load = editor._loadServerSettings();  // the panel reopened meanwhile
        await settle();
        const [put, get] = requests;
        put.answer(409, { error: "The projects folder is on a drive that doesn't support file locks." });
        await write;
        get.answer(200, all(false, false));
        await load;
        assert.equal(editor._serverSettingsLoaded, true, 'not stuck on Loading');
        assert.deepEqual(editor._serverSettings, all(false, false));
    """)


def test_the_box_waits_for_the_settings_and_is_open_to_every_client():
    _run_node(_HARNESS + """
        const box = () => ({ disabled: true, checked: false, title: '' });
        const controls = { allowExternalProjectLinks: box(), shareProjectsBetweenServers: box() };
        const self = { _serverSettingsLoaded: false, _serverSettings: null };
        syncPanel.call(self, controls);
        assert.equal(controls.shareProjectsBetweenServers.disabled, true);
        assert.equal(controls.shareProjectsBetweenServers.title, 'Loading server setting…');
        self._serverSettingsLoaded = true;
        self._serverSettings = { ...all(false, false), can_manage: false };
        syncPanel.call(self, controls);
        assert.equal(controls.allowExternalProjectLinks.disabled, true, 'links-on stays machine-only');
        assert.equal(controls.shareProjectsBetweenServers.disabled, false);
        self._serverSettings = { ...all(false, true), can_manage: false };
        syncPanel.call(self, controls);
        assert.equal(controls.shareProjectsBetweenServers.checked, true);
    """)
