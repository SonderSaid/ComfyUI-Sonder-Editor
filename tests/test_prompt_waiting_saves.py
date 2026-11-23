"""A prompt save waiting behind a running one survives leaving (2026-11-23).

A row of the draft registry sends one save at a time; a save asked for while one
runs waits, and is in no mutation queue until its turn. Leaving at that moment
dropped it (scene switch), closed the editor before it was sent (Exit), or let
later work overtake it (Add to Queue). The real gestures are covered by the
probe named in the plan; these pin the registry and the host methods.
"""

import json

import pytest

from test_prompt_authoring_ux import ROOT, _method, _run_node, _source

INTENT = json.dumps((ROOT / "web/js/prompt_edit_intent.js").as_uri())
QUEUE = json.dumps((ROOT / "web/js/project_mutation_queue.js").as_uri())

_ROW = f"""
const I = await import({INTENT});
const {{ touchPromptDraft, savePromptDraft, hasWaitingPromptSave, pendingPromptDraftSaves,
    promptSavesSent, addPromptSaveProgressListener, resolvePromptDraftRecord }} = I;
const doc = (text) => ({{ text }});
const drafts = new Map(), key = "draft";
const set = (value, shown = "base", k = key) =>
    touchPromptDraft(drafts, k, {{ documents: {{ visual: {{ shown: doc(shown), value: doc(value) }} }} }});
const calls = [];
const gate = () => {{ let open; const held = new Promise((r) => open = r); return {{ open, held }}; }};
const tick = () => new Promise((r) => setTimeout(r, 2));
const gates = [gate(), gate(), gate(), gate()];
const save = async (edit) => {{
    const n = calls.push([edit.documents.visual?.expected.text, edit.documents.visual?.value.text]) - 1;
    return await gates[n].held;
}};
"""


# ── The registry: one request, one save ──────────────────────────────────────


def test_asking_again_for_the_same_text_neither_waits_nor_sends_twice():
    """Save commits, then closing the bar commits again; a blur then Save ask
    with one text. Both used to queue a second send, so a refusal came twice."""
    result = _run_node(_ROW + """
set("A");
const first = savePromptDraft(drafts, key, save);
const second = savePromptDraft(drafts, key, save);      // the bar's own close
const waiting = hasWaitingPromptSave(drafts), joined = pendingPromptDraftSaves(drafts).length;
gates[0].open({ status: "refused", message: "no" });
const outcomes = await Promise.all([first, second]);
console.log(JSON.stringify({ waiting, joined, calls, outcomes, kept: drafts.get(key)?.error,
    left: pendingPromptDraftSaves(drafts).length }));
""")
    assert result == {"waiting": False, "joined": 1, "calls": [["base", "A"]],
                      "outcomes": [False, False], "kept": "no", "left": 0}


@pytest.mark.parametrize("first_ok", [True, False])
def test_a_newer_text_waits_and_is_sent_after_either_outcome(first_ok):
    result = _run_node(_ROW + f"""
set("A");
const first = savePromptDraft(drafts, key, save);
set("B");
const second = savePromptDraft(drafts, key, save);
const waiting = hasWaitingPromptSave(drafts);
gates[0].open({json.dumps(first_ok)});
await tick();
const sent = {{ calls: structuredClone(calls), waiting: hasWaitingPromptSave(drafts),
    pending: pendingPromptDraftSaves(drafts).length }};
gates[1].open(true);
await Promise.all([first, second]);
console.log(JSON.stringify({{ waiting, sent, clean: !drafts.has(key) }}));
""")
    assert result["waiting"] is True
    # Sent: it waits no longer, but is still in flight.
    expected = "A" if first_ok else "base"
    assert result["sent"] == {"calls": [["base", "A"], [expected, "B"]], "waiting": False, "pending": 1}
    assert result["clean"] is True


def test_text_changed_and_changed_back_while_in_flight_is_still_sent():
    result = _run_node(_ROW + """
set("A");
const first = savePromptDraft(drafts, key, save);
set("B"); set("base");                                   // the author undid it all
const second = savePromptDraft(drafts, key, save);
gates[0].open(true); await tick(); gates[1].open(true);
await Promise.all([first, second]);
console.log(JSON.stringify({ calls, clean: !drafts.has(key) }));
""")
    assert result == {"calls": [["base", "A"], ["A", "base"]], "clean": True}


def test_a_retry_after_a_refusal_sends_the_same_text_again():
    result = _run_node(_ROW + """
set("A");
gates[0].open({ status: "refused", message: "no" });
await savePromptDraft(drafts, key, save);
gates[1].open(true);
const retried = await drafts.get(key).retry();
console.log(JSON.stringify({ calls, retried, clean: !drafts.has(key) }));
""")
    assert result == {"calls": [["base", "A"], ["base", "A"]], "retried": True, "clean": True}


def test_saves_sent_resolves_when_the_waiting_save_is_sent_not_when_it_lands():
    result = _run_node(_ROW + """
set("A");
const first = savePromptDraft(drafts, key, save);
let idle = false; await promptSavesSent(drafts); idle = true;     // only one in flight
set("B");
const second = savePromptDraft(drafts, key, save);
let sent = false; const when = promptSavesSent(drafts).then(() => { sent = true; });
await tick(); const before = sent;
gates[0].open(true); await tick(); const after = sent;
gates[1].open(true); await Promise.all([first, second, when]);
console.log(JSON.stringify({ idle, before, after }));
""")
    assert result == {"idle": True, "before": False, "after": True}


def test_a_waiting_save_with_nothing_left_to_send_still_releases():
    """The running save is refused, then every record is resolved to the
    server's: the drain sends nothing more and must still say it ended."""
    result = _run_node(_ROW + """
set("A");
const first = savePromptDraft(drafts, key, async () => {
    calls.push("sent"); await gates[0].held;
    const row = drafts.get(key); row.value.documents = {}; row.base.documents = {};
    return { status: "refused", message: "no" };
});
set("B");
const second = savePromptDraft(drafts, key, save);
const waiting = hasWaitingPromptSave(drafts);
let released = 0; addPromptSaveProgressListener(drafts, () => { if (!hasWaitingPromptSave(drafts)) released += 1; });
gates[0].open(); await Promise.all([first, second]);
console.log(JSON.stringify({ waiting, calls, released: released > 0, stillWaiting: hasWaitingPromptSave(drafts) }));
""")
    assert result == {"waiting": True, "calls": ["sent"], "released": True, "stillWaiting": False}


# ── Leaving waits: the drain and Exit ────────────────────────────────────────

_HOST = f"""
const {{ ProjectMutationQueue }} = await import({QUEUE});
const {{ pendingPromptDraftSaves: _p, hasWaitingPromptSave: _h, promptSavesSent: _s,
    addPromptSaveProgressListener: _a }} = I;
"""


def _drain_class(extra=""):
    widget = _source("web/js/editor_widget.js")
    return f"""
class Host {{
{_method(widget, "_drainProjectMutations", "_runVersionedProjectMutation")}
{_method(widget, "_promptDrafts", "_hasPendingHistoryCommit")}
{extra}
  constructor() {{ this._projectMutationQueue = new ProjectMutationQueue(); this._promptToolDrafts = drafts;
    this.projectDir = "p"; this.activeSceneId = "A"; this.replays = 0; this.isFullscreen = true; }}
  _replayDeferredProjectBackedRefresh() {{ this.replays += 1; }}
  // What the savers do: one queue slot per send, and an answer several
  // microtasks behind the slot, which is past the queue's own drain grace.
  async save(edit) {{ const n = calls.push(edit.documents.visual.value.text) - 1;
    const result = await this._projectMutationQueue.enqueue({{ key: "k" + n, coalesce: false, run: () => gates[n].held }});
    for (let i = 0; i < 6; i += 1) await null;
    return result; }}
}}
const host = new Host();
"""


def test_the_drain_waits_for_a_save_that_is_not_in_the_queue_yet():
    result = _run_node(_ROW + _HOST + _drain_class() + """
const send = (edit) => host.save(edit);
set("A"); void savePromptDraft(drafts, key, send);
set("B"); void savePromptDraft(drafts, key, send);          // waits
let drained = false; const drain = host._drainProjectMutations("exit").then(() => { drained = true; });
gates[0].open(true); await tick(); await tick();
const between = { drained, calls: [...calls] };
gates[1].open(true); await drain;
console.log(JSON.stringify({ between, after: { calls, busy: host._projectMutationQueue.isBusy(), clean: !drafts.has(key) }, replays: host.replays }));
""")
    assert result["between"] == {"drained": False, "calls": ["A", "B"]}
    assert result["after"] == {"calls": ["A", "B"], "busy": False, "clean": True}
    assert result["replays"] == 1


def test_the_drain_covers_two_rows_and_does_not_hang_on_a_refused_one():
    result = _run_node(_ROW + _HOST + _drain_class() + """
const send = (edit) => host.save(edit);
set("X", "base", "refused"); await savePromptDraft(drafts, "refused", async () => ({ status: "refused", message: "no" }));
set("A", "base", "one"); void savePromptDraft(drafts, "one", send);
set("C", "base", "two"); void savePromptDraft(drafts, "two", send);
set("B", "base", "one"); void savePromptDraft(drafts, "one", send);
for (const g of gates) g.open(true);
await host._drainProjectMutations("exit");
console.log(JSON.stringify({ calls, rows: [...drafts.keys()] }));
""")
    assert sorted(result["calls"]) == ["A", "B", "C"]
    assert result["rows"] == ["refused"]


def test_exit_waits_says_so_and_commits_a_bar_opened_meanwhile():
    widget = _source("web/js/editor_widget.js")
    exit_method = _method(widget, "_requestExitFullscreen", "_exitFullscreen")
    result = _run_node(_ROW + _HOST + """
const notices = [];
globalThis.notifyProgress = (opts) => { notices.push(opts.message); return { dismiss: () => notices.push("dismissed") }; };
""" + _drain_class(exit_method + """
  _stopPlayback() {}
  _hidePromptEditor() { this.hides = (this.hides || 0) + 1; const bar = this._promptEditorEl; this._promptEditorEl = null; bar?.commit?.(); }
  _exitFullscreen() { this.exitedWith = [...calls]; this.isFullscreen = false; }
""") + """
const send = (edit) => host.save(edit);
set("A"); void savePromptDraft(drafts, key, send);
set("B"); void savePromptDraft(drafts, key, send);          // waits
const exit = host._requestExitFullscreen({ reason: "toolbar" });
await new Promise((r) => setTimeout(r, 650));
const shown = [...notices];
// The author opens a bar and types while it waits.
host._promptEditorEl = { commit: () => { set("D", "base", "late"); void savePromptDraft(drafts, "late", send); } };
gates[0].open(true); await tick(); gates[1].open(true); await tick();
const stillOpen = host.isFullscreen;
gates[2].open(true);
await exit;
console.log(JSON.stringify({ shown, notices, stillOpen, exitedWith: host.exitedWith, left: drafts.size }));
""")
    assert result["shown"] == ["Saving prompt text before closing…"]
    assert result["notices"][-1] == "dismissed"
    assert result["stillOpen"] is True
    assert result["exitedWith"] == ["A", "B", "D"]
    assert result["left"] == 0


def test_exit_commits_a_panel_box_typed_in_while_it_waits():
    """Closing the editor tears the panel down without committing (audit 5)."""
    widget = _source("web/js/editor_widget.js")
    exit_method = _method(widget, "_requestExitFullscreen", "_exitFullscreen")
    result = _run_node(_ROW + _HOST + """
globalThis.notifyProgress = () => ({ dismiss() {} });
""" + _drain_class(exit_method + """
  _stopPlayback() {} _hidePromptEditor() {}
  _exitFullscreen() { this.exitedWith = [...calls]; this.isFullscreen = false; }
""") + """
const send = (edit) => host.save(edit);
let typing = null;
host._promptPanelHandle = { flushFocused: () => { if (!typing) return false; const text = typing; typing = null;
    set(text, "base", "panel"); void savePromptDraft(drafts, "panel", send); return true; } };
set("A"); void savePromptDraft(drafts, key, send);
const exit = host._requestExitFullscreen({});
await tick(); typing = "P";
gates[0].open(true); await tick(); gates[1].open(true);
await exit;
console.log(JSON.stringify({ exitedWith: host.exitedWith, left: drafts.size }));
""")
    assert result == {"exitedWith": ["A", "P"], "left": 0}


def test_the_panel_flush_commits_through_the_box_s_own_blur():
    panel = _source("web/js/editor_prompt_panel.js")
    at = panel.index("flushFocused: () => {")
    assert "guard.focusedBox.el.blur();" in panel[at:at + 160]
    assert "guard.focusedBox.el === document.activeElement" in panel[at - 220:at]


def test_a_quick_exit_says_nothing():
    widget = _source("web/js/editor_widget.js")
    exit_method = _method(widget, "_requestExitFullscreen", "_exitFullscreen")
    result = _run_node(_ROW + _HOST + """
const notices = []; globalThis.notifyProgress = (opts) => { notices.push(opts.message); return { dismiss() {} }; };
""" + _drain_class(exit_method + """
  _stopPlayback() {} _hidePromptEditor() {} _exitFullscreen() { this.isFullscreen = false; }
""") + """
set("A"); void savePromptDraft(drafts, key, (edit) => host.save(edit)); gates[0].open(true);
await host._requestExitFullscreen({});
await new Promise((r) => setTimeout(r, 650));
console.log(JSON.stringify({ notices, left: host.isFullscreen }));
""")
    assert result == {"notices": [], "left": False}


# ── A waiting save holds the scene ───────────────────────────────────────────


def _guard_class():
    widget = _source("web/js/editor_widget.js")
    return f"""
globalThis.sessionDiagRecord = () => {{}}; globalThis.coerceBoolean = Boolean;
const notices = []; globalThis.notifyInfo = (message) => notices.push(message); globalThis.notifyWarning = (message) => notices.push(message);
class Host {{
{_method(widget, "_promptDrafts", "applyWidgetState")}
{_method(widget, "applyWidgetState", "_flushDeferredDragState")}
{_method(widget, "_cycleScene", "_renameScene")}
{_method(widget, "_replayDeferredHistoryWidgetStateIfIdle", "_runUndo")}
  constructor() {{ this.activeSceneId = "A"; this.activeScene = {{ scene_id: "A" }}; this.scenes = [{{ scene_id: "A" }}, {{ scene_id: "B" }}];
    this._undoStack = []; this._redoStack = []; this.projectDir = "p"; this.selectionStart = 0; this.selectionEnd = 9; this.playhead = 0; this.totalFrames = 9; }}
  _setActiveScene(scene) {{ this.activeSceneId = scene.scene_id; this.activeScene = scene; }}
  _setHostValueLocal() {{}} _setPlayhead() {{}} _refreshContextInputs() {{}} _refreshSelectionInputs() {{}} _updateToolbar() {{}}
  _renderTimeline() {{}} _renderQueuePanel() {{}} _replayDeferredProjectBackedRefresh() {{}} _promptContextConsumersMounted() {{ return false; }}
}}
const host = new Host();
const registry = host._promptDrafts();
const put = (value) => touchPromptDraft(registry, key, {{ documents: {{ visual: {{ shown: doc("base"), value: doc(value) }} }} }});
"""


def test_only_a_waiting_save_holds_the_scene():
    result = _run_node(_ROW + _HOST + _guard_class() + """
put("A"); const first = savePromptDraft(registry, key, save);
const inFlight = host._historyIsBusy();
host._cycleScene(1); const movedInFlight = host.activeSceneId; host._setActiveScene(host.scenes[0]);
put("B"); const second = savePromptDraft(registry, key, save);
const waiting = host._historyIsBusy();
host._cycleScene(1); const heldCycle = host.activeSceneId;
const lifecycle = host._beginSceneHistoryLifecycle("duplicate active scene", { allowSceneSwitch: true });
gates[0].open(true); await tick();                      // the waiting save is now sent
const afterSent = host._historyIsBusy();
host._cycleScene(1); const movedAfter = host.activeSceneId;
gates[1].open(true); await Promise.all([first, second]);
console.log(JSON.stringify({ inFlight, movedInFlight, waiting, heldCycle, lifecycle, afterSent, movedAfter, notices }));
""")
    assert result["inFlight"] is False and result["movedInFlight"] == "B"
    assert result["waiting"] is True and result["heldCycle"] == "A" and result["lifecycle"] is None
    assert result["afterSent"] is False and result["movedAfter"] == "B"
    assert result["notices"] == ["Finish saving, Undo, or Redo before switching scenes.",
                                 "Another scene change is still finishing. Try again in a moment."]


def test_work_that_stays_on_the_scene_is_not_held_and_commits_nothing():
    """The Prompt panel's Format and Attach take the same lifecycle without a
    scene switch (implementation audit 4)."""
    result = _run_node(_ROW + _HOST + _guard_class() + """
put("A"); const first = savePromptDraft(registry, key, save);
put("B"); const second = savePromptDraft(registry, key, save);
let commits = 0; host._promptEditorCommit = () => { commits += 1; };
const token = host._beginSceneHistoryLifecycle("attach prompt Reference");
const taken = !!token; host._endSceneHistoryLifecycle(token);
gates[0].open(true); await tick(); gates[1].open(true); await Promise.all([first, second]);
console.log(JSON.stringify({ taken, commits, notices }));
""")
    assert result == {"taken": True, "commits": 0, "notices": []}


def test_create_scene_sends_what_waits_before_it_takes_the_author_away():
    """Its lifecycle owns the switch, so the guard lets it through (audit 3)."""
    widget = _source("web/js/editor_widget.js")
    result = _run_node(_ROW + _HOST + """
globalThis.api = { apiURL: (value) => value };
let answer; globalThis.fetch = () => new Promise((resolve) => { answer = () => resolve({ ok: true, json: async () => ({ scene_id: "NEW" }) }); });
globalThis.withEditorMutationDiagnostics = (init) => init;
""" + _guard_class().replace("class Host {", "class Host {\n" + _method(widget, "_createSceneWithinGesture", "_setActiveScene")
                             + "\n  _defaultNewSceneDuration() { return 10; }") + """
const sentOn = [];
const record = async (edit) => { sentOn.push(host.activeSceneId); return await save(edit); };
const create = host._createSceneWithinGesture();
put("A"); void savePromptDraft(registry, key, record);          // while the POST runs
put("B"); void savePromptDraft(registry, key, record);          // waits
answer(); await tick();
const held = host.activeSceneId;
gates[0].open(true); await tick();
const after = host.activeSceneId;
gates[1].open(true); await create;
console.log(JSON.stringify({ held, after, sentOn, owner: host._sceneHistoryLifecycleOwner }));
""")
    assert result == {"held": "A", "after": "NEW", "sentOn": ["A", "A"], "owner": None}


def test_duplicate_scene_waits_the_same_way():
    duplicate = _method(_source("web/js/editor_widget.js"), "_duplicateSceneWithinGesture", "_allProjectAssetsForGallery")
    jump = duplicate.index("this._setActiveScene(newScene, { lifecycleToken });")
    assert "await this._promptSavesSentBeforeLeaving?.();" in duplicate[:jump]


def test_leaving_does_not_resend_a_refused_draft_that_has_not_changed():
    result = _run_node(_ROW + """
const { promptDraftNeedsSend } = I;
set("A"); gates[0].open({ status: "refused", message: "no" });
await savePromptDraft(drafts, key, save);
const unchanged = promptDraftNeedsSend(drafts.get(key));
set("B"); const changed = promptDraftNeedsSend(drafts.get(key));
console.log(JSON.stringify({ unchanged, changed, none: promptDraftNeedsSend(null) }));
""")
    assert result == {"unchanged": False, "changed": True, "none": False}
    widget = _source("web/js/editor_widget.js")
    assert "this._promptEditorCommit?.({ retryRefused: false });" in widget
    assert widget.count("(retryRefused || promptDraftNeedsSend(row))") == 2      # section and global bars


@pytest.mark.parametrize("first_ok", [True, False])
def test_a_remote_scene_change_waits_and_is_then_applied(first_ok):
    result = _run_node(_ROW + _HOST + _guard_class() + f"""
put("A"); const first = savePromptDraft(registry, key, save);
put("B"); const second = savePromptDraft(registry, key, save);
host.applyWidgetState({{ scene_id: "B", selection_start: 3 }});
const held = host.activeSceneId;
gates[0].open({json.dumps(first_ok)}); await tick();
const applied = [host.activeSceneId, host.selectionStart];
gates[1].open(true); await Promise.all([first, second]);
console.log(JSON.stringify({{ held, applied, left: host._pendingHistoryWidgetState || null }}));
""")
    assert result == {"held": "A", "applied": ["B", 3], "left": None}


@pytest.mark.parametrize("leave", [
    "host._cycleScene(1);",
    'host.applyWidgetState({ scene_id: "B" });',
    'host._beginSceneHistoryLifecycle("create scene", { allowSceneSwitch: true });',
])
def test_an_open_dirty_bar_is_committed_before_the_guard_is_read(leave):
    """`_setActiveScene` hides the bar after the guard, so a commit made there
    would become the waiting save and be dropped by the very switch that made it."""
    result = _run_node(_ROW + _HOST + _guard_class() + """
put("A"); const first = savePromptDraft(registry, key, save);
host._promptEditorCommit = () => { put("B"); void savePromptDraft(registry, key, save); };
LEAVE
const held = host.activeSceneId;
gates[0].open(true); await tick(); gates[1].open(true); await first;
console.log(JSON.stringify({ held, calls }));
""".replace("LEAVE", leave))
    assert result["held"] == "A"
    assert result["calls"] == [["base", "A"], ["A", "B"]]


def test_the_real_scene_switch_commits_the_bar_and_is_refused():
    widget = _source("web/js/editor_widget.js")
    set_scene = _method(widget, "_setActiveScene", "_refreshDurationInput")
    result = _run_node(_ROW + _HOST + _guard_class().replace(
        "  _setActiveScene(scene) { this.activeSceneId = scene.scene_id; this.activeScene = scene; }\n", set_scene) + """
put("A"); const first = savePromptDraft(registry, key, save);
host._promptEditorCommit = () => { put("B"); void savePromptDraft(registry, key, save); };
const refused = host._setActiveScene(host.scenes[1]);
gates[0].open(true); await tick(); gates[1].open(true); await first;
console.log(JSON.stringify({ refused, active: host.activeSceneId, calls, notices }));
""")
    assert result["refused"] is False and result["active"] == "A"
    assert result["calls"] == [["base", "A"], ["A", "B"]]
    assert result["notices"] == ["Finish saving, Undo, or Redo before switching scenes."]


# ── Work authored after a waiting save ───────────────────────────────────────


def _queue_class():
    """The real single add; what it needs around it is stubbed at the edges."""
    widget = _source("web/js/editor_widget.js")
    return _drain_class(
        _method(widget, "_addToRenderQueue", "_addBatchToRenderQueue")
        + _method(widget, "_historyIsBusy", "_endSceneHistoryLifecycle") + """
  _withMutationGesture(kind, run) { this._gesture = kind; try { return run(); } finally { this._gesture = null; } }
  _snapshotMutationDiagnostics() { return { gestureKind: this._gesture }; }
  _hasPendingHistoryCommit() { return false; }
  _resolveQueueSelectionRange() { return { selStart: this.selectionStart, selEnd: this.selectionEnd }; }
  _buildQueueSnapshot(start, end) { return { scene_id: this.activeSceneId, range: [start, end] }; }
  _applyStoredQueueBatchCollapseState() {} _renderQueuePanel() { this.painted = (this._renderQueue || []).map((job) => job.range.join("-")); }
  _warnForGuideCollisionPredictions() {} _warnForRemovedReferenceSources() {} _flashQueueButton() {}
  _projectDirName() { return "p"; } _mutationDiagnosticHeaders() { return {}; }
  _queueProjectMutation({ key, intent, diagnostics }) {
    return this._projectMutationQueue.enqueue({ key, coalesce: false, run: async () => {
      order.push(`job ${intent.snapshot.scene_id} ${intent.snapshot.range.join("-")} by ${diagnostics?.gestureKind ?? this._gesture ?? "click"}`);
      return { payload: { job_id: key, ...intent.snapshot } }; } }); }
""").replace("const host = new Host();", """const order = [];
const host = new Host();
const send = async (edit) => { const text = edit.documents.visual.value.text; const n = calls.push(text) - 1;
    const result = await host._projectMutationQueue.enqueue({ key: "s" + n, coalesce: false, run: async () => { order.push("text " + text); return await gates[n].held; } });
    for (let i = 0; i < 6; i += 1) await null; return result; };
host.selectionStart = 0; host.selectionEnd = 10;
""")


def test_a_job_queued_behind_a_waiting_save_keeps_the_click_s_range():
    """The wait once came before the range was read, so a second click, or a
    remote selection, changed the first job (implementation audit 1, 6)."""
    result = _run_node(_ROW + _HOST + _queue_class() + """
set("A"); void savePromptDraft(drafts, key, send);
set("B"); void savePromptDraft(drafts, key, send);              // waits
const one = host._addToRenderQueue();
const painted = [...host.painted];                              // at the click
host.selectionStart = 50; host.selectionEnd = 60;
const two = host._addToRenderQueue();
host.selectionStart = 7; host.activeSceneId = "B";              // a remote change lands meanwhile
gates[0].open(true); await tick(); gates[1].open(true);
await Promise.all([one, two]);
console.log(JSON.stringify({ painted, order }));
""")
    assert result["painted"] == ["0-10"]
    assert result["order"] == ["text A", "text B", "job A 0-10 by addToRenderQueue",
                               "job A 50-60 by addToRenderQueue"]


def test_a_job_with_nothing_waiting_is_queued_at_once():
    result = _run_node(_ROW + _HOST + _queue_class() + """
set("A"); void savePromptDraft(drafts, key, send);
const job = host._addToRenderQueue();
const slots = host._projectMutationQueue.slots().length;        // both already in the queue
gates[0].open(true); await job;
console.log(JSON.stringify({ slots, order }));
""")
    assert result["slots"] == 2
    assert result["order"] == ["text A", "job A 0-10 by click"]


def test_an_open_dirty_bar_is_sent_ahead_of_the_job():
    result = _run_node(_ROW + _HOST + _queue_class() + """
set("A"); void savePromptDraft(drafts, key, send);
host._promptEditorCommit = () => { set("B"); void savePromptDraft(drafts, key, send); };
const job = host._addToRenderQueue();
gates[0].open(true); await tick(); gates[1].open(true); await job;
console.log(JSON.stringify(order));
""")
    assert result == ["text A", "text B", "job A 0-10 by addToRenderQueue"]


def test_every_place_that_queues_jobs_waits_for_the_prompt_text():
    import re
    widget = _source("web/js/editor_widget.js")
    posts = [m.start() for m in re.finditer(r"/queue(?:/batch)?`", widget)
             if 'method: "POST"' in widget[m.start():m.start() + 220]]
    assert len(posts) == 3, "a new queue post must wait for prompt saves too"
    for at in posts:
        call = widget.rindex("await this._queueProjectMutation({", 0, at)
        assert "this._jobBehindPromptSaves()" in widget[call - 160:call]
        assert "diagnostics: jobDiagnostics," in widget[call:at]
    for name, following in (("_addToRenderQueue", "_addToRenderQueueWithinGesture"),
                            ("_addBatchToRenderQueue", "_addBatchToRenderQueueWithinGesture"),
                            ("_queueSelectedPromptSections", "_queueSelectedPromptSectionsWithinGesture")):
        assert "this._commitOpenPromptBar?.();" in _method(widget, name, following)


# ── No silent or false outcome ───────────────────────────────────────────────


@pytest.mark.parametrize("global_scope", [False, True])
def test_a_save_that_cannot_be_sent_is_refused_and_told_once(global_scope):
    widget = _source("web/js/editor_widget.js")
    method = _method(widget, "_updateSceneGlobalContext", "_setSectionGlobalInherit") if global_scope \
        else _method(widget, "_updatePromptSection", "_promptSaveBlocked")
    blocked = _method(widget, "_promptSaveBlocked", "_updateLinkedPromptAttachment")
    take = _method(widget, "_takePromptIdentityCreateIntents", "_adoptPromptIdentitiesFromMutation")
    resolve = _method(widget, "_resolvePanelPromptSection", "_deletePromptSectionWithinGesture")
    urls = {name: json.dumps((ROOT / f"web/js/{name}.js").as_uri()) for name in
            ("prompt_edit_intent", "prompt_composition", "prompt_channel_templates", "prompt_context_chips")}
    result = _run_node(f"""
const {{ promptEditFields, promptEditBody, promptEditPaint }} = await import({urls["prompt_edit_intent"]});
const {{ normalizeChannels, composeSectionText }} = await import({urls["prompt_composition"]});
const {{ projectTemplateValue, getChannelTemplate }} = await import({urls["prompt_channel_templates"]});
const promptDocumentText = (d) => String(d?.text || "");
const notices = []; globalThis.notifyWarning = (message) => notices.push(message);
class Host {{
{method}
{blocked}
{take}
{resolve}
  constructor() {{ this.projectDir = "p"; this.activeSceneId = "s"; this.locked = false; this.sent = 0; this.discarded = 0;
    this.row = {{ prompt_id: "x", start_frame: 0, end_frame: 9, channel_docs: {{}}, channels: {{}}, attachments: [] }};
    this.activeScene = {{ scene_id: "s", prompt_sections: [this.row], global_channel_docs: {{}}, global_channels: {{}}, global_attachments: [] }}; }}
  _projectDirName() {{ return "p"; }} _channelTemplate() {{ return getChannelTemplate("sonder"); }}
  _isGlobalPromptTrackLocked() {{ return this.locked; }} _isPromptTrackLocked() {{ return this.locked; }}
  _pushUndo() {{ return {{}}; }} _discardUndoEntry() {{ this.discarded += 1; }}
  _renderSceneAfterLocalMutation() {{}} _refreshPromptContextDependencyConsumers() {{}}
  _adoptPromptIdentitiesFromMutation() {{}} _finalizePromptIdentityCreationHistory() {{}}
  _applyLocalPromptUpdate(index, fields) {{ Object.assign(this.row, fields); }}
  _fetchReferences() {{ return Promise.resolve(); }}
  _runSceneMutation() {{ this.sent += 1; return Promise.resolve(this.answer); }}
}}
const edit = {{ documents: {{ visual: {{ expected: null, value: {{ text: "T" }} }} }}, attachments: {{}} }};
const run = async (change, options = {{}}) => {{
    const host = new Host(); notices.length = 0; change(host);
    const opts = {{ edit, projectId: "p", sceneId: "s", promptId: "x", ...options }};
    const outcome = {json.dumps(global_scope)} ? await host._updateSceneGlobalContext(null, null, null, opts)
        : await host._updatePromptSection(0, {{}}, opts);
    return {{ status: outcome?.status ?? outcome, sent: host.sent, notices: notices.length, discarded: host.discarded }};
}};
console.log(JSON.stringify({{
    ok: await run((h) => {{ h.answer = {{ payload: {{}} }}; }}),
    locked: await run((h) => {{ h.locked = true; }}),
    otherScene: await run((h) => {{ h.activeSceneId = "other"; }}),
    otherProject: await run(() => {{}}, {{ projectId: "q" }}),
    notSent: await run((h) => {{ h.answer = null; }}),
    gone: {json.dumps(global_scope)} ? null : await run((h) => {{ h.activeScene.prompt_sections = []; }}),
}}));
""")
    assert result["ok"] == {"status": "acknowledged", "sent": 1, "notices": 0, "discarded": 0}
    for name in ("locked", "otherScene", "otherProject"):
        assert result[name] == {"status": "refused", "sent": 0, "notices": 1, "discarded": 0}, name
    # Nothing was sent, yet a null result used to read as stored.
    assert result["notSent"] == {"status": "refused", "sent": 1, "notices": 1, "discarded": 1}
    if not global_scope:
        assert result["gone"] == {"status": "refused", "sent": 0, "notices": 1, "discarded": 0}


def test_every_surface_takes_the_registry_from_the_host():
    widget, panel = _source("web/js/editor_widget.js"), _source("web/js/editor_prompt_panel.js")
    assert widget.count("_promptToolDrafts = new Map()") == 1
    assert "_promptToolDrafts ||= new Map()" not in widget
    assert "const drafts = host._promptDrafts?.() ||" in panel
