"""Prompt Tool Integrity Pass: the traced failures, as tests.

Phase 0 wrote these as strict-xfail tests from the delivery-order traces of
2026-11-20 (the plan's Phase 0 record). Phase 1 fixed the save-base failures
(traces 2–6), so those now pass and guard the fix. The L3 (Phase 2) and L1
(Phase 4) tests stay strict-xfail until their phases land.

What is real and what is modelled:

- `prompt_edit_intent.js` is imported as is: the record-level draft rows, the
  per-surface mount (`createPromptDraftMount`), the host-wide acknowledgement
  listener and the save drain.
- The host's save gesture (`_updatePromptSectionWithinGesture`) and its local
  paint (`_applyLocalPromptUpdate`) are lifted verbatim from `editor_widget.js`
  and run against a fake server that applies the route's record comparison,
  including Phase 1's agreement rule.
- The Prompt panel and the timeline bar cannot mount under node. What they add
  around the real module is MODELLED in `_PANEL_MODEL`: a mount per render, a
  live-render registry the listener feeds, and the save path by prompt id.
  `test_panel_and_bar_models_still_mirror_the_source` pins the source lines the
  model copies, so changing them fails that test until the model is ported.
- The chip-editor tests (L3) drive the real editor against the minimal DOM,
  but their host glue (sibling transactions, `_L3_HARNESS`) is a mirror pinned
  by the same drift guard. The task-type test (L1) drives the real control.
- A crash in the panel harness raises `HarnessError`, not AssertionError, so
  the strict-xfail markers cannot swallow it.
"""

import json

import pytest

from test_prompt_authoring_ux import ROOT, _method, _run_node, _source
from test_prompt_chip_enter import _SELECTION
from test_prompt_context_corrections import _run_chip_dom_script
from test_prompt_task_type_choices import CANDIDATE, _chip, _chip_script


def _url(relative):
    return json.dumps((ROOT / relative).as_uri())


# A host with the real save gesture, a fake server, and models of the panel and
# the timeline bar around the real draft module. `server.hold` keeps a mutation
# in flight; `server.writeOther` is a second writer; `server.failOnce` refuses
# one save without naming a record (a transient refusal).
_PANEL_MODEL = r"""
globalThis.window ||= { addEventListener() {}, removeEventListener() {} };
const intent = await import(__INTENT__);
const { promptDocumentText } = await import(__CHIPS__);
const { normalizeChannels, composeSectionText } = await import(__COMPOSITION__);
const { projectTemplateValue } = await import(__TEMPLATES__);
const { promptEditBody, promptEditFields, promptEditPaint, promptDraftKey, savePromptDraft,
    rebasePromptDraft, retryPromptDraft, resolvePromptDraftRecord, createPromptDraftMount,
    addPromptDraftListener } = intent;
const notifyWarning = () => {};
const clone = (value) => structuredClone(value);
const KEYS = ["visual", "speech", "sounds"];
const doc = (key, text) => ({ schema: "prompt_document_v1",
    nodes: [{ type: "text", node_id: "n-" + key, text }] });
const text = (d) => (d?.nodes || []).map((n) => n.text ?? "[" + n.attachment_id + "]").join("");
const section = (texts) => ({ prompt_id: "p1", start_frame: 0, end_frame: 48,
    channels: { ...texts }, channel_docs: Object.fromEntries(KEYS.map((k) => [k, doc(k, texts[k])])),
    attachments: [], prompt: "" });
const sameHash = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

const requests = [];
const server = {
    sections: [section({ visual: "v0", speech: "s0", sounds: "z0" })],
    hold: null, failOnce: false,
    writeOther(key, value) { this.sections[0].channel_docs[key] = doc(key, value);
        this.sections[0].channels[key] = value; },
    async mutate(operations) {
        const op = operations.find((value) => value.type === "update_prompt_section");
        const edit = op.fields.prompt_edit || { documents: {}, attachments: {} };
        requests.push(Object.fromEntries([
            ...Object.entries(edit.documents || {}).map(([k, v]) =>
                [k, { expected: text(v.expected), value: text(v.value) }]),
            ...Object.entries(edit.attachments || {}).map(([k, v]) =>
                ["att:" + k, { expected: v.expected ? "record" : null, value: v.value ? "record" : null }]),
        ]));
        const hold = this.hold;
        if (hold) await hold;
        if (this.failOnce) { this.failOnce = false; throw new Error("transient refusal"); }
        const current = this.sections[op.index];
        const chips = new Map((current.attachments || []).map((a) => [a.attachment_id, a]));
        // `_merge_prompt_edit_fields`: compare each record; one that does not
        // match but already holds the value agrees (Phase 1); else refuse all.
        const conflict = (kind, key, now) => Object.assign(new Error(
            "This prompt field changed elsewhere. Your draft was not saved."),
            { payload: { conflict: { record_kind: kind, record_key: key, current: clone(now ?? null) } } });
        const settledDocs = new Set(), settledChips = new Set();
        for (const [key, change] of Object.entries(edit.documents || {})) {
            const now = current.channel_docs[key] ?? null;
            if (sameHash(now, change.expected)) continue;
            if (sameHash(text(now), text(change.value)) && now && change.value) { settledDocs.add(key); continue; }
            throw conflict("document", key, now);
        }
        for (const [id, change] of Object.entries(edit.attachments || {})) {
            const now = chips.get(id) ?? null;
            if (sameHash(now, change.expected)) continue;
            if (sameHash(now, change.value)) { settledChips.add(id); continue; }
            throw conflict("attachment", id, now);
        }
        for (const [key, change] of Object.entries(edit.documents || {})) {
            if (settledDocs.has(key)) continue;
            current.channel_docs[key] = clone(change.value);
            current.channels[key] = text(change.value);
        }
        for (const [id, change] of Object.entries(edit.attachments || {})) {
            if (settledChips.has(id)) continue;
            if (change.value) chips.set(id, clone(change.value)); else chips.delete(id);
        }
        current.attachments = [...chips.values()];
        return { payload: { scene: { prompt_sections: clone(this.sections) } } };
    },
};

class Host {
    constructor() {
        this.activeScene = { prompt_sections: clone(server.sections) };
        this.activeSceneId = "scene";
        this.projectDir = "project";
        this._promptToolDrafts = new Map();
        this.onPanelRefresh = () => {};
    }
    _projectDirName() { return "project"; }
    _channelTemplate() { return "sonder"; }
    _isPromptTrackLocked() { return false; }
    _takePromptIdentityCreateIntents() { return []; }
    _settlePromptIdentityCreateIntents() {}
    _markPromptIdentityCreatesSending() {}
    _pushUndo() { return {}; }
    _discardUndoEntry() {}
    _renderSceneAfterLocalMutation() {}
    _adoptPromptIdentitiesFromMutation() {}
    _finalizePromptIdentityCreationHistory() {}
    async _fetchReferences() {}
    _refreshPromptContextDependencyConsumers() { this.onPanelRefresh("refusal"); }
    // The real runner refetches the scene after a refusal; the panel then
    // rebuilds from it (observed live: render, GET, render).
    async _runSceneMutation(operations) {
        try { return await server.mutate(operations); }
        catch (error) {
            setTimeout(() => { this.activeScene = { prompt_sections: clone(server.sections) };
                this.onPanelRefresh("refetch"); }, 0);
            throw error;
        }
    }
    async _updatePromptSection(...args) { return this._updatePromptSectionWithinGesture(...args); }
__HOST_METHODS__
}

const host = new Host();
const drafts = host._promptToolDrafts;
const key = promptDraftKey("project", "scene", "p1");
const template = projectTemplateValue("sonder");

// `editor_prompt_panel.js`: one listener feeds the live render's surface; a
// save resolves its section by prompt id (`sectionSaver`).
const liveMounts = new Map();
addPromptDraftListener(drafts, (event) => liveMounts.get(event.key)?.acknowledge(event));
const save = (edit) => host._updatePromptSection(-1, {}, { edit, promptId: "p1",
    projectId: "project", sceneId: "scene", template });
const panel = {
    live: null, serial: 0,
    render() {
        const current = host.activeScene.prompt_sections[0];
        const view = rebasePromptDraft(drafts.get(key), current);
        const r = { serial: ++this.serial, docs: clone(view.channel_docs || {}),
            attachments: clone(view.attachments || []) };
        r.mount = createPromptDraftMount({ drafts, key,
            shown: { documents: current.channel_docs, attachments: current.attachments },
            displayed: { documents: r.docs, attachments: r.attachments } });
        r.acknowledge = (event) => {
            const repaint = r.mount.acknowledge(event, { documents: r.docs, attachments: r.attachments });
            if (!repaint) return;
            Object.assign(r.docs, clone(repaint.documents));
            if (repaint.attachments) r.attachments = clone(repaint.attachments);
        };
        liveMounts.set(key, r);
        this.live = r;
        return r;
    },
    type(r, channel, value) {
        r.docs[channel] = doc(channel, value);
        r.mount.record({ documents: r.docs, attachments: r.attachments });
    },
    commit(r) {
        r.mount.record({ documents: r.docs, attachments: r.attachments });
        return drafts.get(key) ? savePromptDraft(drafts, key, save) : Promise.resolve(true);
    },
    // `applyAfterDialog`: a dialog result goes to the LIVE render's surface.
    afterDialog(build) {
        const r = liveMounts.get(key);
        r.attachments = build(r.attachments);
        return this.commit(r);
    },
    shown(r) { return Object.fromEntries(KEYS.map((k) => [k, text(r.docs[k])])); },
};
host.onPanelRefresh = () => panel.render();

// `_showPromptEditor`: a mount at open, records only, its own listener.
const bar = {
    open() {
        const current = host.activeScene.prompt_sections[0];
        const view = rebasePromptDraft(drafts.get(key), current);
        this.docs = clone(view.channel_docs);
        this.attachments = clone(view.attachments || []);
        this.mount = createPromptDraftMount({ drafts, key,
            shown: { documents: current.channel_docs, attachments: current.attachments },
            displayed: { documents: view.channel_docs, attachments: view.attachments } });
        addPromptDraftListener(drafts, (event) => {
            const repaint = this.mount.acknowledge(event, { documents: this.docs, attachments: this.attachments });
            if (!repaint) return;
            Object.assign(this.docs, clone(repaint.documents));
            if (repaint.attachments) this.attachments = clone(repaint.attachments);
        });
    },
    type(channel, value) { this.docs[channel] = doc(channel, value); },
    commit() {
        const row = this.mount.record({ documents: this.docs, attachments: this.attachments });
        return row ? savePromptDraft(drafts, key, save) : Promise.resolve(true);
    },
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const serverText = () => Object.fromEntries(KEYS.map((k) => [k, text(server.sections[0].channel_docs[k])]));
const rowText = () => { const row = drafts.get(key); return row ? {
    base: Object.fromEntries(Object.entries(row.base.documents).map(([k, v]) => [k, text(v)])),
    value: Object.fromEntries(Object.entries(row.value.documents).map(([k, v]) => [k, text(v)])),
    error: row.error || "" } : null; };
"""


class HarnessError(RuntimeError):
    """A broken harness, never the bug."""


def _run_panel(body):
    widget = _source("web/js/editor_widget.js")
    try:
        methods = "\n".join([
            _method(widget, "_updatePromptSectionWithinGesture", "_updateLinkedPromptAttachment"),
            _method(widget, "_applyLocalPromptUpdate", "_applyLocalPromptDelete"),
            _method(widget, "_resolvePanelPromptSection", "_deletePromptSectionWithinGesture"),
        ])
    except AssertionError as error:
        raise HarnessError(f"lifted host method missing: {error}") from None
    script = (_PANEL_MODEL
              .replace("__INTENT__", _url("web/js/prompt_edit_intent.js"))
              .replace("__CHIPS__", _url("web/js/prompt_context_chips.js"))
              .replace("__COMPOSITION__", _url("web/js/prompt_composition.js"))
              .replace("__TEMPLATES__", _url("web/js/prompt_channel_templates.js"))
              .replace("__HOST_METHODS__", methods))
    # A JS exception or a missing export fails `_run_node`'s returncode
    # assert; re-raised as a harness error, it cannot pass as an expected
    # failure (the markers accept only AssertionError).
    try:
        return _run_node(script + body)
    except AssertionError as error:
        raise HarnessError(f"panel harness crashed: {error}") from None


def test_panel_and_bar_models_still_mirror_the_source():
    """Drift guard for `_PANEL_MODEL` and `_L3_HARNESS`. Not xfail: when these
    lines change, the models must be ported before the tests mean anything."""
    panel = _source("web/js/editor_prompt_panel.js").replace("\r\n", "\n")
    for line in (
        # Section card: a surface per render, saves by prompt id.
        "const sectionView = rebasePromptDraft(drafts.get(sectionDraftKey), section);",
        "const sectionMount = createPromptDraftMount({ drafts, key: sectionDraftKey,",
        "shown: { documents: section.channel_docs, attachments: section.attachments },",
        "sectionMount.record({ documents: channelDocuments, attachments: sectionAttachments });",
        # The model records on every keystroke (`panel.type`) because the
        # card's onChange does; the bar model records only on commit.
        "channelDocuments[key] = nextDocument;\n"
        "                        sectionAttachments = attachments;\n"
        "                        const snapshot = draftSceneSnapshot();\n"
        "                        keepSectionDraft(snapshot);",
        "if (drafts.get(sectionDraftKey)) await savePromptDraft(drafts, sectionDraftKey, saveSection);",
        "host._updatePromptSection(-1, {}, { edit, promptId, projectId, sceneId, template });",
        # One listener feeds the live render; dialogs resolve the live render.
        "liveMounts.get(event.key)?.acknowledge(event);",
        "liveMounts.set(sectionDraftKey, {",
        "const live = mounted ? liveMounts.get(draftKey) : null;",
        # `_L3_HARNESS` mirrors the sibling glue: only a registry transaction
        # is recorded in siblings, and a box asks its siblings before pruning.
        "if (reason === \"attachment\" && origin === \"registry\") {\n"
        "                                sibling.recordSharedAttachmentTransaction?.(sectionAttachments);",
        "sibling.syncPromptAttachments?.(sectionAttachments);",
        "isAnchoredElsewhere: (attachmentId) => promptAttachmentAnchoredChannels(\n"
        "                        Object.fromEntries(Object.entries(channelInputs)\n"
        "                            .filter(([channelKey]) => channelKey !== key)",
        # The global host carries the same glue.
        "if (reason === \"attachment\" && origin === \"registry\") {\n"
        "                            sibling.recordSharedAttachmentTransaction?.(globalAttachments);",
        "isAnchoredElsewhere: (attachmentId) => promptAttachmentAnchoredChannels(\n"
        "                    Object.fromEntries(Object.entries(globalInputs)\n"
        "                        .filter(([channelKey]) => channelKey !== key)",
        # A chip dialog's result is the box's inline change; a scope-row
        # dialog is a registry transaction (Phase 2 audit L-a).
        "live.transactAttachments(build, channelKey, channelKey ? \"inline\" : \"registry\");",
        "?.transactPromptAttachments?.(build(sectionAttachments), null, { origin }),",
        "?.transactPromptAttachments?.(build(globalAttachments), null, { origin }),",
    ):
        assert line in panel, line
    widget = _source("web/js/editor_widget.js").replace("\r\n", "\n")
    for line in (
        "const mount = createPromptDraftMount({ drafts, key: draftKey,",
        "const row = mount.record({ documents: channelInputs.readDocuments(),",
        "const stopListening = addPromptDraftListener(drafts, (event) => {",
        "sibling.recordSharedAttachmentTransaction?.(sharedAttachments);",
        "if (reason === \"attachment\" && origin === \"registry\") {\n"
        "                            sibling.recordSharedAttachmentTransaction?.(sharedAttachments);",
        "isAnchoredElsewhere: (attachmentId) => promptAttachmentAnchoredChannels(\n"
        "                    Object.fromEntries(Object.entries(inputs)\n"
        "                        .filter(([channelKey]) => channelKey !== key)",
        # The refusal render the fake runner's refetch follows.
        "if (this._promptEditRevisions.get(key) === revision) Object.assign(section, previous);\n"
        "            this._refreshPromptContextDependencyConsumers();",
    ):
        assert line in widget, line
    intent = _source("web/js/prompt_edit_intent.js")
    for export in ("promptEditBody", "promptEditPaint", "promptDraftKey", "savePromptDraft",
                   "rebasePromptDraft", "retryPromptDraft", "resolvePromptDraftRecord",
                   "createPromptDraftMount", "addPromptDraftListener"):
        assert (f"export function {export}(" in intent
                or f"export async function {export}(" in intent), export


def test_trace_send_a_type_b_ack_a_save_b_carries_expected_a():
    """Trace 1 (green live): the second save names what the first stored."""
    result = _run_panel(r"""
let finish; server.hold = new Promise((resolve) => finish = resolve);
const r = panel.render();
panel.type(r, "visual", "A");
const first = panel.commit(r);
await tick();
panel.type(r, "visual", "B");
server.hold = null; finish(); await first;
await panel.commit(r);
console.log(JSON.stringify({ requests, server: serverText(), row: rowText() }));
""")
    assert result["requests"] == [
        {"visual": {"expected": "v0", "value": "A"}},
        {"visual": {"expected": "A", "value": "B"}},
    ]
    assert result["server"]["visual"] == "B"
    assert result["row"] is None


def test_a_rebuild_during_a_refused_save_keeps_the_typed_text_and_retry_sends_it():
    """Trace 2 (live before Phase 1: typed 'D' lost, Retry sent nothing)."""
    result = _run_panel(r"""
let finish; server.hold = new Promise((resolve) => finish = resolve);
const r = panel.render();
panel.type(r, "visual", "typed");
const pending = panel.commit(r);
await tick();
server.writeOther("visual", "other writer");
panel.render();                        // the panel rebuilds while the save is in flight
server.hold = null; finish(); await pending; await tick();
const afterRefusal = rowText();
const shown = panel.shown(panel.live);
const before = requests.length;
await retryPromptDraft(drafts, key); await tick();
console.log(JSON.stringify({ afterRefusal, shown, retrySent: requests.length - before,
    server: serverText() }));
""")
    assert result["afterRefusal"] is not None
    assert result["afterRefusal"]["value"]["visual"] == "typed"
    assert result["shown"]["visual"] == "typed"
    assert result["retrySent"] >= 1
    assert result["server"]["visual"] == "other writer"  # Retry stays neutral


def test_retry_after_a_refusal_and_refetch_does_not_overwrite_the_other_writer():
    """Trace 2c (live before Phase 1: Retry sent expected='SERVER TWO' and won)."""
    result = _run_panel(r"""
let finish; server.hold = new Promise((resolve) => finish = resolve);
const r = panel.render();
panel.type(r, "visual", "typed");
const pending = panel.commit(r);
await tick();
server.writeOther("visual", "other writer");
server.hold = null; finish(); await pending; await tick(); await tick();
const afterRefetch = rowText();
const before = requests.length;
await retryPromptDraft(drafts, key);
console.log(JSON.stringify({ afterRefetch, retry: requests.slice(before), server: serverText() }));
""")
    assert result["afterRefetch"]["value"]["visual"] == "typed"
    assert result["retry"][0]["visual"]["expected"] == "v0"
    assert result["server"]["visual"] == "other writer"


def test_after_a_retry_from_an_older_render_a_save_sends_only_what_was_touched():
    """Trace 3 / L6 (live before Phase 1: the speech save dragged visual with a
    stale `expected` and was refused, repeatedly). A transient refusal rebuilds
    the panel; Retry runs from the first render; the next edit in the live
    render must send only its own record and show what was stored."""
    result = _run_panel(r"""
const r1 = panel.render();
server.failOnce = true;
panel.type(r1, "visual", "typed");
await panel.commit(r1); await tick(); await tick();
const live = panel.live;                 // rebuilt by the refusal and the refetch
await retryPromptDraft(drafts, key);     // the row's save path, from r1's commit
await tick();
const before = requests.length;
panel.type(live, "speech", "speech edit");
await panel.commit(live);
console.log(JSON.stringify({ rebuilt: live !== r1, next: requests.slice(before),
    liveShows: panel.shown(live), row: rowText(), server: serverText() }));
""")
    assert result["rebuilt"] is True
    assert result["next"] == [{"speech": {"expected": "s0", "value": "speech edit"}}]
    assert result["liveShows"]["visual"] == "typed"
    assert result["row"] is None
    assert result["server"] == {"visual": "typed", "speech": "speech edit", "sounds": "z0"}


def test_use_servers_version_resolves_one_record_and_keeps_the_other():
    """Plan 1.7: a refused move across two records resolves one; the other is
    still sent, against the base its author saw."""
    result = _run_panel(r"""
let finish; server.hold = new Promise((resolve) => finish = resolve);
const r = panel.render();
panel.type(r, "visual", "moved out");
panel.type(r, "speech", "moved in");
const pending = panel.commit(r);
await tick();
server.writeOther("visual", "other writer");
server.hold = null; finish(); await pending; await tick(); await tick();
const resolved = resolvePromptDraftRecord(drafts, key);
const live = panel.live;
const before = requests.length;
await retryPromptDraft(drafts, key);
console.log(JSON.stringify({ resolved, retry: requests.slice(before), liveShows: panel.shown(live),
    row: rowText(), server: serverText() }));
""")
    assert result["resolved"] == {"resolved": "document visual", "remaining": ["document speech"],
                                  "followers": [], "settled": []}
    assert result["retry"] == [{"speech": {"expected": "s0", "value": "moved in"}}]
    assert result["liveShows"]["visual"] == "other writer"
    assert result["row"] is None
    assert result["server"]["visual"] == "other writer"
    assert result["server"]["speech"] == "moved in"


@pytest.mark.parametrize("bar_first", [False, True])
def test_a_bar_commit_does_not_revert_a_record_the_panel_just_saved(bar_first):
    """Trace 5 (live before Phase 1: the panel's acknowledged speech was
    reverted by the bar's follow-up save, 200, no notice). Both orders."""
    result = _run_panel(f"""
const barFirst = {json.dumps(bar_first)};
""" + r"""
bar.open();
let finish; server.hold = new Promise((resolve) => finish = resolve);
const r = panel.render();
let pending;
if (barFirst) {
    bar.type("visual", "bar visual");
    pending = bar.commit();
    await tick();
    panel.type(r, "speech", "panel speech");
    panel.commit(r);
} else {
    panel.type(r, "speech", "panel speech");
    pending = panel.commit(r);
    await tick();
    bar.type("visual", "bar visual");
    bar.commit();
}
server.hold = null; finish(); await pending; await tick();
const row = drafts.get(key); if (row?.pending) await row.pending;
await tick();
console.log(JSON.stringify({ requests, server: serverText(), row: rowText(),
    bar: Object.fromEntries(KEYS.map((k) => [k, text(bar.docs[k])])) }));
""")
    assert result["server"]["visual"] == "bar visual"
    assert result["server"]["speech"] == "panel speech"
    assert result["row"] is None
    # Each surface sent only its own record.
    assert all(set(request) <= {"visual", "speech"} for request in result["requests"])
    assert not any(request.get("speech", {}).get("value") == "s0" for request in result["requests"])


@pytest.mark.parametrize("panel_acked_first", [True, False])
def test_a_bar_edit_of_the_field_the_panel_saved_refuses_instead_of_overwriting(panel_acked_first):
    """Phase 1 audit, High: the bar never displayed the panel's save of the
    same field, so its commit must be sent against what the bar showed and
    refuse. Before the fix the bar's mount adopted the panel's stored text as
    its own base (acknowledged first) or the save's ack moved the row's base
    under the bar's value (still in flight): either way a silent overwrite."""
    result = _run_panel(f"""
const ackedFirst = {json.dumps(panel_acked_first)};
""" + r"""
bar.open();
let finish; server.hold = new Promise((resolve) => finish = resolve);
const r = panel.render();
panel.type(r, "visual", "panel text");
const pending = panel.commit(r);
await tick();
bar.type("visual", "bar text");            // the bar still shows v0
let later;
if (ackedFirst) { server.hold = null; finish(); await pending; await tick(); later = bar.commit(); }
else { later = bar.commit(); server.hold = null; finish(); await pending; }
await later; await tick();
const row = drafts.get(key); if (row?.pending) await row.pending;
console.log(JSON.stringify({ requests, server: serverText(), row: rowText() }));
""")
    assert result["server"]["visual"] == "panel text"
    assert result["requests"][-1]["visual"] == {"expected": "v0", "value": "bar text"}
    assert result["row"]["value"] == {"visual": "bar text"}
    assert result["row"]["error"]


def test_an_acknowledgement_keeps_a_bar_s_unrecorded_chip_insert_and_removal():
    """Phase 1 audit, High: an ack repainting the bar's chips must not count the
    bar's own unrecorded chip edits as drawn, or the next commit never sends
    them (an added Prompt link stays unsaved under its anchor; a removed one
    stays stored with no anchor)."""
    result = _run_panel(r"""
const chip = (id, extra = {}) => ({ attachment_id: id, kind: "custom", config: {}, ...extra });
server.sections[0].attachments = [chip("keep"), chip("gone")];
host.activeScene = { prompt_sections: clone(server.sections) };
bar.open();
const r = panel.render();
// The bar inserts one chip and removes another (onChange does not record).
bar.attachments = [chip("keep"), chip("added")];
// The panel configures `keep` and saves.
r.attachments = [chip("keep", { config: { x: 1 } }), chip("gone")];
await panel.commit(r); await tick();
await bar.commit(); await tick();
console.log(JSON.stringify({ stored: server.sections[0].attachments.map((a) => [a.attachment_id, a.config]) }));
""")
    assert sorted(result["stored"]) == [["added", {}], ["keep", {"x": 1}]]


def test_retry_after_reverting_every_refused_record_clears_the_refusal():
    """Phase 1 audit, Low: a refused draft whose author typed the field back to
    its base has nothing left to send; Retry must clear the save-failed bar
    instead of reporting success and leaving it up."""
    result = _run_panel(r"""
server.writeOther("visual", "other writer");
const r = panel.render();
panel.type(r, "visual", "mine");
await panel.commit(r); await tick();
const refused = rowText();
const live = panel.live;                    // the refetch rebuilt the panel
panel.type(live, "visual", "v0");           // back to the base it saw
const retried = await retryPromptDraft(drafts, key);
console.log(JSON.stringify({ refused, retried, row: rowText() }));
""")
    assert result["refused"]["error"]
    assert result["retried"] is True
    assert result["row"] is None


def test_an_attach_that_closes_after_a_rebuild_shows_in_the_live_render():
    """Trace 6 / L4 (live before Phase 1: a references reload rebuilt the panel
    twice while the Attach dialog was open; the link saved and never showed).
    The dialog's result now goes to the live render (`applyAfterDialog`)."""
    result = _run_panel(r"""
const drew = panel.render();
const live = panel.render();               // rebuilt while the dialog is open
await panel.afterDialog((list) => [...list, { attachment_id: "link", kind: "prompt_link_scope",
    source: { prompt_id: "earlier", channel_keys: ["visual"] }, config: {} }]);
console.log(JSON.stringify({ stored: server.sections[0].attachments.map((a) => a.attachment_id),
    live: live.attachments.map((a) => a.attachment_id), drewUntouched: drew.attachments.length,
    requests }));
""")
    assert result["stored"] == ["link"]
    assert result["live"] == ["link"]
    assert result["drewUntouched"] == 0
    assert result["requests"] == [{"att:link": {"expected": None, "value": "record"}}]


def test_an_acknowledgement_repaints_only_what_the_surface_still_shows():
    """Phase 1.4: a record typed after submission keeps its newer text and its
    advanced base; one left untouched repaints to the stored record."""
    result = _run_panel(r"""
let finish; server.hold = new Promise((resolve) => finish = resolve);
const r = panel.render();
panel.type(r, "visual", "A");
panel.type(r, "speech", "S");
const pending = panel.commit(r);
await tick();
panel.type(r, "speech", "S newer");
server.hold = null; finish(); await pending; await tick();
console.log(JSON.stringify({ shows: panel.shown(r), row: rowText() }));
""")
    assert result["shows"]["visual"] == "A"
    assert result["shows"]["speech"] == "S newer"
    assert result["row"]["base"] == {"speech": "S"}
    assert result["row"]["value"] == {"speech": "S newer"}


# --- L3: shared registry Undo (real chip editor, minimal DOM) --------------

# Host glue mirrored from the panel section host's `onChange`: an inline
# attachment change records a shared transaction in every sibling, anything
# else syncs siblings without an undo entry.
_L3_HARNESS = r"""
const link = (id) => ({ attachment_id: id, kind: "prompt_link",
    source: { prompt_id: "earlier", channel_key: "visual" }, config: {} });
let shared = [];
const boxes = {};
const mount = (name, nodes) => {
    const editor = mod.createPromptDocumentEditor({ document: { nodes },
        attachments: shared,
        isAnchoredElsewhere: (id) => mod.promptAttachmentAnchoredChannels(
            Object.fromEntries(Object.entries(boxes).filter(([other]) => other !== name)
                .map(([other, box]) => [other, box.promptDocument])), id).length > 0,
        onChange: ({ attachments, reason, origin }) => {
            shared = attachments;
            for (const [other, sibling] of Object.entries(boxes)) {
                if (other === name) continue;
                if (reason === "attachment" && origin === "registry") sibling.recordSharedAttachmentTransaction(shared);
                else sibling.syncPromptAttachments(shared);
            }
        } });
    boxes[name] = editor;
    return editor;
};
const anchored = () => new Set(Object.values(boxes).flatMap((e) => e.promptDocument.nodes)
    .filter((n) => n.type === "attachment").map((n) => n.attachment_id));
const orphans = () => shared.map((a) => a.attachment_id).filter((id) => !anchored().has(id));
"""


def _run_l3(body):
    # `_SELECTION` supplies the Range/Selection primitives the minimal DOM
    # lacks; `insertAttachment` places its chip through them.
    return _run_chip_dom_script(_SELECTION + _L3_HARNESS + body)


def test_undo_in_a_sibling_with_prior_typing_does_not_restore_a_removed_inline_chip():
    """L3 (strict-xfail until Phase 2): the sibling's older entry restored the
    whole shared list, resurrecting the removed chip's record as an orphan."""
    result = _run_l3(r"""
shared = [link("c1")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "before " },
    { type: "attachment", node_id: "na", attachment_id: "c1" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "other box" }]);
b.pushPromptHistory();                       // prior typing in the sibling
a.removeAttachment("c1");                     // the chip leaves box A
const afterRemove = shared.map((x) => x.attachment_id);
undo(b);
console.log(JSON.stringify({ afterRemove, afterUndo: shared.map((x) => x.attachment_id),
    orphans: orphans() }));
""")
    assert result["afterRemove"] == []
    assert result["orphans"] == []


def test_undo_in_a_sibling_keeps_a_chip_added_later_in_another_box():
    """L3 (strict-xfail until Phase 2): the sibling's older entry dropped a
    record another box had added since."""
    result = _run_l3(r"""
shared = [link("c1")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a " },
    { type: "attachment", node_id: "na", attachment_id: "c1" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
b.pushPromptHistory();                        // prior typing in the sibling
a.removeAttachment("c1");                     // B's shared entry holds [c1]
a.insertAttachment(link("c2"));               // anchored in A; B's newer entry holds []
const before = shared.map((x) => x.attachment_id);
undo(b);
console.log(JSON.stringify({ before, after: shared.map((x) => x.attachment_id),
    anchoredInA: a.promptDocument.nodes.filter((n) => n.type === "attachment").map((n) => n.attachment_id) }));
""")
    assert "c2" in result["before"]
    assert "c2" in result["anchoredInA"]
    assert "c2" in result["after"]


def test_undo_of_a_chip_configuration_in_its_own_box_restores_the_previous_configuration():
    """Not xfail: passes today. Phase 2's inverse transactions must keep it."""
    result = _run_l3(r"""
shared = [{ ...link("c1"), config: { note: "first" } }];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a " },
    { type: "attachment", node_id: "na", attachment_id: "c1" }]);
mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
a.replaceAttachment({ ...link("c1"), config: { note: "second" } });
const configured = shared[0].config.note;
undo(a);
console.log(JSON.stringify({ configured, restored: shared[0]?.config?.note }));
""")
    assert result == {"configured": "second", "restored": "first"}


_L3_MORE = r"""
const redo = (editor) => editor._sonderOwnedKeydown({ key: "y", ctrlKey: true,
    metaKey: false, shiftKey: false, target: editor, isComposing: false,
    preventDefault() {}, stopPropagation() {} });
const backspaceChip = (editor, attachmentId) => {
    const span = editor.childNodes.find((child) => child.dataset?.attachmentId === attachmentId);
    return editor._sonderOwnedKeydown({ key: "Backspace", ctrlKey: false, metaKey: false,
        shiftKey: false, target: span, isComposing: false,
        preventDefault() {}, stopPropagation() {} });
};
const ids = () => shared.map((x) => x.attachment_id);
const anchorsIn = (editor) => editor.promptDocument.nodes
    .filter((n) => n.type === "attachment").map((n) => n.attachment_id);
"""


def test_undo_and_redo_of_an_inline_insert_and_removal_in_its_own_box():
    """Phase 2.1: the box that made an inline change undoes and redoes exactly
    that record, with its anchor, and siblings only follow."""
    result = _run_l3(_L3_MORE + r"""
shared = [link("c1")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a " },
    { type: "attachment", node_id: "na", attachment_id: "c1" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
backspaceChip(a, "c1");
const removed = { ids: ids(), a: anchorsIn(a) };
undo(a);
const undone = { ids: ids(), a: anchorsIn(a) };
redo(a);
const redone = { ids: ids(), a: anchorsIn(a) };
undo(b);                                     // B holds no entry for it
console.log(JSON.stringify({ removed, undone, redone, afterB: ids(), orphans: orphans() }));
""")
    assert result["removed"] == {"ids": [], "a": []}
    assert result["undone"] == {"ids": ["c1"], "a": ["c1"]}
    assert result["redone"] == {"ids": [], "a": []}
    assert result["afterB"] == []
    assert result["orphans"] == []


def test_a_configuration_undo_leaves_a_record_someone_changed_since():
    """Phase 2.1: an inverse applies only to a record still as its entry left
    it; a later registry change from another box is not reverted."""
    result = _run_l3(_L3_MORE + r"""
shared = [{ ...link("c1"), config: { note: "first" } }];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a " },
    { type: "attachment", node_id: "na", attachment_id: "c1" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
a.replaceAttachment({ ...link("c1"), config: { note: "second" } });
// An inline configuration from B reaches A as a sync, with no entry there.
b.replaceAttachment({ ...link("c1"), config: { note: "third" } });
undo(a);
console.log(JSON.stringify({ note: shared[0]?.config?.note }));
""")
    assert result["note"] == "third"


def test_a_scope_transaction_is_undoable_from_any_box_once():
    """Phase 2.2: a registry (scope-row) change is recorded in every box; the
    first Undo reverses it and a second box's Undo finds nothing to reverse."""
    result = _run_l3(_L3_MORE + r"""
shared = [];
const scope = { attachment_id: "s1", kind: "custom", source: {}, config: {} };
const a = mount("a", [{ type: "text", node_id: "ta", text: "a" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
a.transactPromptAttachments([scope]);
const added = ids();
undo(b);
const afterB = ids();
undo(a);
const afterA = ids();
redo(b);
console.log(JSON.stringify({ added, afterB, afterA, redone: ids() }));
""")
    assert result == {"added": ["s1"], "afterB": [], "afterA": [], "redone": ["s1"]}


@pytest.mark.parametrize("removal", ["button", "backspace"])
def test_a_chip_anchored_in_two_boxes_keeps_its_record_while_one_still_holds_it(removal):
    """Phase 2.3: removing one of two anchors leaves the record; removing the
    last one takes it."""
    result = _run_l3(_L3_MORE + f"""
const removal = {json.dumps(removal)};
""" + r"""
shared = [link("c1")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a " },
    { type: "attachment", node_id: "na", attachment_id: "c1" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "b " },
    { type: "attachment", node_id: "nb", attachment_id: "c1" }]);
const drop = (editor) => removal === "button" ? editor.removeAttachment("c1") : backspaceChip(editor, "c1");
drop(a);
const oneLeft = { ids: ids(), a: anchorsIn(a), b: anchorsIn(b) };
drop(b);
console.log(JSON.stringify({ oneLeft, none: ids(), orphans: orphans() }));
""")
    assert result["oneLeft"] == {"ids": ["c1"], "a": [], "b": ["c1"]}
    assert result["none"] == []
    assert result["orphans"] == []


def _notices():
    url = json.dumps((ROOT / "web/js/editor_notifications.js").as_uri())
    return f"""
const notes = await import({url});
const seen = [];
notes.subscribe((list) => {{ for (const n of list) if (!seen.includes(n.message)) seen.push(n.message); }});
"""


_ORPHAN_NOTICE = "A Prompt link or Vocal event chip was not restored: its place in the text is gone."


def test_undo_does_not_restore_an_inline_only_record_with_no_anchor():
    """Phase 2.4: Undo of a scope-row removal of a legacy orphan Prompt link
    would bring the orphan back; it is left out with a notice. (Manual check
    498: the first version of this test never looked for the notice.)"""
    result = _run_l3(_notices() + _L3_MORE + r"""
shared = [link("orphan")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a" }]);
mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
a.transactPromptAttachments([]);
undo(a);
console.log(JSON.stringify({ ids: ids(), seen }));
""")
    assert result["ids"] == []
    assert _ORPHAN_NOTICE in result["seen"]


def test_undo_reverts_a_capability_toggle_after_the_server_stored_form_lands():
    """Phase 2 audit M1: the server fills sparse capability entries in and the
    acknowledgement repaints that stored form; Undo must still revert the
    author's own toggle."""
    result = _run_l3(_L3_MORE + r"""
const ref = (caps) => ({ attachment_id: "r1", kind: "reference", source: {}, config: {}, capabilities: caps });
shared = [ref([])];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a " },
    { type: "attachment", node_id: "na", attachment_id: "r1" }]);
mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
a.replaceAttachment(ref([{ capability_id: "text", kind: "text", enabled: false }]));
// The acknowledgement repaints every box with the stored (filled-in) record.
const stored = [ref([{ capability_id: "text", kind: "text", channel_key: "", placement: "",
    config: {}, enabled: false }])];
for (const box of Object.values(boxes)) box.syncPromptAttachments(stored);
shared = stored;
undo(a);
console.log(JSON.stringify({ caps: shared[0].capabilities }));
""")
    assert result["caps"] == []


def test_undo_brings_back_the_record_for_an_anchor_it_restores():
    """Phase 2 audit M2: an id anchored in two boxes, removed from both;
    Undo in the first box restores its anchor and must restore the record."""
    result = _run_l3(_L3_MORE + r"""
shared = [link("c1")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a " },
    { type: "attachment", node_id: "na", attachment_id: "c1" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "b " },
    { type: "attachment", node_id: "nb", attachment_id: "c1" }]);
backspaceChip(a, "c1");
backspaceChip(b, "c1");
undo(a);
console.log(JSON.stringify({ ids: ids(), a: anchorsIn(a), b: anchorsIn(b) }));
""")
    assert result == {"ids": ["c1"], "a": ["c1"], "b": []}


def test_an_open_entry_does_not_take_a_sibling_change_as_its_own():
    """Phase 2 audit L-b: while A composes, a sibling's removal reaches A as a
    sync; Undo in A must not bring the removed chip back."""
    result = _run_l3(_L3_MORE + r"""
const custom = (id) => ({ attachment_id: id, kind: "custom", source: {}, config: {} });
shared = [custom("c2")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a" }]);
const b = mount("b", [{ type: "text", node_id: "tb", text: "b " },
    { type: "attachment", node_id: "nb", attachment_id: "c2" }]);
for (const handler of a._handlers.compositionstart || []) handler({});
backspaceChip(b, "c2");
for (const handler of a._handlers.compositionend || []) handler({});
undo(a);
console.log(JSON.stringify({ ids: ids(), orphans: orphans() }));
""")
    assert result == {"ids": [], "orphans": []}


def test_writing_host_state_round_trips_through_undo_and_redo():
    """Phase 2.1: Writing's `blockMeta` host snapshot still travels with each
    entry alongside the inverse registry change."""
    result = _run_l3(r"""
let blockMeta = [{ id: "one" }];
const restored = [];
const editor = mod.createPromptDocumentEditor({
    document: { nodes: [{ type: "text", node_id: "t", text: "x" }] }, attachments: [],
    getHostSnapshot: () => blockMeta,
    onRestoreHostSnapshot: (value) => { restored.push(value); blockMeta = value; },
    onChange: () => {} });
editor.transactPromptAttachments([{ attachment_id: "s1", kind: "custom", source: {}, config: {} }],
    () => { blockMeta = [{ id: "one" }, { id: "two" }]; });
undo(editor);
const afterUndo = { meta: blockMeta, ids: editor.promptAttachments.map((x) => x.attachment_id) };
editor._sonderOwnedKeydown({ key: "y", ctrlKey: true, metaKey: false, shiftKey: false,
    target: editor, isComposing: false, preventDefault() {}, stopPropagation() {} });
console.log(JSON.stringify({ afterUndo, afterRedo: { meta: blockMeta,
    ids: editor.promptAttachments.map((x) => x.attachment_id) } }));
""")
    assert result["afterUndo"] == {"meta": [{"id": "one"}], "ids": []}
    assert result["afterRedo"] == {"meta": [{"id": "one"}, {"id": "two"}], "ids": ["s1"]}


# --- L1: Customize with no automatic task types ----------------------------

def test_customize_unlocks_the_task_types_when_no_role_derives_one():
    """L1 (strict-xfail until Phase 4): with no automatic task types, Customize
    left every box locked because an empty list displayed as automatic."""
    candidate = json.loads(json.dumps(CANDIDATE))
    candidate["h3_task_type_preview"]["role_derived"] = []
    candidate["h3_task_type_preview"]["scene_effective"] = []
    candidate["h3_task_type_preview"]["scene_source"] = "none"
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip())}, {json.dumps(candidate)},
          async ({{ press, toggle, read }}) => {{
            press("Customize");
            globalThis.__afterCustomize = read();
            toggle("audio reference", true);
          }});
        console.log(JSON.stringify({{ ...r, afterCustomize: globalThis.__afterCustomize }}));
    """)
    assert result["opened"]["disabled"] is True
    assert result["afterCustomize"]["mode"] == "customized"
    assert result["afterCustomize"]["disabled"] is False
    assert result["saved"]["config"]["overrides"]["task_types"] == ["audio reference"]


# --- Phase 1.6: client and server agree on what "the same document" is ------

_DOC_PAIRS = [
    # same text, different node ids
    ({"nodes": [{"type": "text", "node_id": "a", "text": "hello"}]},
     {"nodes": [{"type": "text", "node_id": "b", "text": "hello"}]}),
    # text split across runs vs one run
    ({"nodes": [{"type": "text", "node_id": "a", "text": "hel"},
                {"type": "text", "node_id": "b", "text": "lo"}]},
     {"nodes": [{"type": "text", "node_id": "c", "text": "hello"}]}),
    # an anchor, ids differ
    ({"nodes": [{"type": "text", "node_id": "a", "text": "x "},
                {"type": "attachment", "node_id": "n1", "attachment_id": "chip"}]},
     {"nodes": [{"type": "text", "node_id": "b", "text": "x "},
                {"type": "attachment", "node_id": "n2", "attachment_id": "chip"}]}),
    # different text
    ({"nodes": [{"type": "text", "node_id": "a", "text": "hello"}]},
     {"nodes": [{"type": "text", "node_id": "a", "text": "hello!"}]}),
    # different anchor
    ({"nodes": [{"type": "attachment", "node_id": "n", "attachment_id": "one"}]},
     {"nodes": [{"type": "attachment", "node_id": "n", "attachment_id": "two"}]}),
]


def test_client_and_server_document_agreement_match():
    """`promptRecordsEquivalent` (browser, deciding whether a record is still
    touched) and `_prompt_edit_agrees` (server, settling a stale record) are a
    mirrored pair; they must call the same documents the same."""
    from server import routes

    url = _url("web/js/prompt_edit_intent.js")
    client = _run_node(f"""
const {{ promptRecordsEquivalent }} = await import({url});
const pairs = {json.dumps(_DOC_PAIRS)};
console.log(JSON.stringify(pairs.map(([a, b]) => promptRecordsEquivalent("documents", a, b))));
""")
    server = [routes._prompt_edit_agrees("documents", a, b) for a, b in _DOC_PAIRS]
    assert client == server == [True, True, True, False, False]


def test_canonical_record_matches_the_servers_capability_defaults():
    """`canonicalPromptRecord` (browser, Undo's comparison) fills capability
    entries the way `normalize_attachment` stores them: a sparse record and its
    stored form compare equal, an authored difference does not (Phase 2 audit
    M1). A mirror of the server's defaults, so it owes this parity test."""
    from server import prompt_context

    sparse = {"attachment_id": "r1", "kind": "reference", "source": {}, "config": {},
              "capabilities": [{"capability_id": "text", "kind": "text", "enabled": False}]}
    stored = prompt_context.normalize_attachment(sparse)
    toggled = json.loads(json.dumps(sparse))
    toggled["capabilities"][0]["enabled"] = True
    stored_capability = stored["capabilities"][0]
    url = _url("web/js/prompt_context_chips.js")
    result = _run_chip_dom_script(f"""
const {{ canonicalPromptRecord }} = await import({url});
const c = (value) => JSON.stringify(canonicalPromptRecord(value));
const sparse = {json.dumps(sparse)}, toggled = {json.dumps(toggled)};
const storedCapability = {json.dumps(stored_capability)};
// The record-level fields the browser also fills are compared as the browser
// holds them; the capability entry is what the server fills in.
const storedForm = {{ ...sparse, capabilities: [storedCapability] }};
console.log(JSON.stringify({{ same: c(sparse) === c(storedForm), differs: c(toggled) !== c(storedForm) }}));
""")
    assert result == {"same": True, "differs": True}


# --- Phase 3: destructive selection and the chip clipboard -----------------

_P3 = r"""
const clip = (data = {}) => ({ data, event: { prevented: false,
    preventDefault() { this.prevented = true; }, stopPropagation() {},
    clipboardData: { setData: (t, v) => { data[t] = v; }, getData: (t) => data[t] || "" } } });
const custom = (id, group = id) => ({ attachment_id: id, emission_group_id: group, kind: "custom",
    source: {}, config: { text: "fixed " + id } });
const make = (nodes, atts, { scope = { project: "P", scene: "S", promptId: "p1" }, allowedKinds } = {}) => {
    const e = mod.createPromptDocumentEditor({ document: { nodes }, attachments: atts, onChange: () => {} });
    e.focus = function () { document.activeElement = this; };
    e.isConnected = true;
    mod.installPromptContextMenu({ editor: e, clipboardScope: () => scope,
        ...(allowedKinds ? { allowedKinds } : {}),
        referenceContext: () => ({ scene: { prompt_sections: [] }, semanticUnits: [] }) });
    return e;
};
const fire = (editor, type, event) => { for (const h of editor._handlers[type] || []) h(event); return event; };
const summary = (e) => ({ nodes: e.promptDocument.nodes.map((n) => n.type === "text" ? n.text : "[" + n.attachment_id + "]"),
    atts: e.promptAttachments.map((a) => [a.attachment_id, a.emission_group_id]) });
const source = () => make([{ type: "text", node_id: "t1", text: "one " },
    { type: "attachment", node_id: "n1", attachment_id: "c1" },
    { type: "text", node_id: "t2", text: " two" }], [custom("c1", "g1")]);
const selectAll = (e) => { place(firstText(spanOf(e, "t1")), 0, firstText(spanOf(e, "t2")), 4);
    document.activeElement = e; };
const target = (opts) => { const e = make([{ type: "text", node_id: "x", text: "X" }], [], opts);
    place(firstText(spanOf(e, "x")), 1); document.activeElement = e; return e; };
"""


def _run_p3(body):
    return _run_l3(_P3 + body)


def test_copy_and_paste_carry_chips_as_independent_copies():
    result = _run_p3(r"""
const a = source(); selectAll(a);
const { data, event } = clip();
fire(a, "copy", event);
const b = target();
fire(b, "paste", clip(data).event);
const pasted = summary(b);
console.log(JSON.stringify({ prevented: event.prevented, plain: data["text/plain"],
    types: Object.keys(data), source: summary(a), pasted }));
""")
    assert result["prevented"] is True
    assert result["plain"] == "one  two"                 # authored text only, no chip label
    assert "application/x-sonder-prompt-fragment" in result["types"]
    assert result["source"]["atts"] == [["c1", "g1"]]    # a copy leaves the source alone
    [[pasted_id, pasted_group]] = result["pasted"]["atts"]
    assert pasted_id != "c1" and pasted_group not in ("g1", "c1")
    assert "".join(result["pasted"]["nodes"]) == f"Xone [{pasted_id}] two"


def test_a_cut_moves_its_chips_once_then_pastes_copies():
    result = _run_p3(r"""
const a = source(); selectAll(a);
const { data, event } = clip();
fire(a, "cut", event);
const afterCut = summary(a);
const b = target();
fire(b, "paste", clip(data).event);
const first = summary(b);
fire(b, "paste", clip(data).event);
const second = summary(b);
undo(a);                                     // the source box gets its chip back
console.log(JSON.stringify({ afterCut, first, second, sourceUndone: summary(a) }));
""")
    assert result["afterCut"]["atts"] == []
    moved = result["first"]["atts"][0]
    assert moved[0] != "c1" and moved[1] == "g1"         # moved: fresh id, same link group
    again = [row for row in result["second"]["atts"] if row != moved][0]
    assert again[1] not in ("g1", moved[0])              # token consumed: now a copy
    assert result["sourceUndone"]["atts"] == [["c1", "g1"]]


@pytest.mark.parametrize("case", ["other-project", "other-scene", "no-fragment", "no-scope"])
def test_a_chip_paste_falls_back_to_plain_text(case):
    result = _run_p3(f"""
const which = {json.dumps(case)};
""" + r"""
const a = source(); selectAll(a);
const { data, event } = clip();
fire(a, "copy", event);
if (which === "no-fragment") delete data["application/x-sonder-prompt-fragment"];   // Ctrl+Shift+V
const scope = which === "other-project" ? { project: "Q", scene: "S" }
    : which === "other-scene" ? { project: "P", scene: "T" } : { project: "P", scene: "S" };
const b = which === "no-scope"
    ? (() => { const e = mod.createPromptDocumentEditor({ document: { nodes: [{ type: "text", node_id: "x", text: "X" }] },
        onChange: () => {} }); place(firstText(spanOf(e, "x")), 1); return e; })()
    : target({ scope });
fire(b, "paste", clip(data).event);
console.log(JSON.stringify(summary(b)));
""")
    assert "".join(result["nodes"]) == "Xone  two" and result["atts"] == []


def test_a_chip_the_target_box_does_not_take_is_left_out_and_the_text_still_lands():
    result = _run_p3(r"""
const a = source(); selectAll(a);
const { data, event } = clip();
fire(a, "copy", event);
const b = target({ allowedKinds: ["reference"] });
fire(b, "paste", clip(data).event);
console.log(JSON.stringify(summary(b)));
""")
    assert "".join(result["nodes"]) == "Xone  two" and result["atts"] == []


def test_an_empty_customize_with_nothing_automatic_is_refused_and_reset_restores_automatic():
    """Phase 4.1: Customize with no automatic types shows an editable,
    customized empty list; saving it is still refused (a roles-derived field
    would read empty as automatic), and Reset ends the customizing state."""
    candidate = json.loads(json.dumps(CANDIDATE))
    candidate["h3_task_type_preview"]["role_derived"] = []
    candidate["h3_task_type_preview"]["scene_effective"] = []
    candidate["h3_task_type_preview"]["scene_source"] = "none"
    result = _chip_script(f"""
        const steps = async ({{ press, read }}) => {{
            press("Customize");
            globalThis.__customized = read();
        }};
        steps.afterRefusal = async ({{ press, read }}) => {{
            press("Reset to inherited");
            globalThis.__afterReset = read();
        }};
        const r = await openChip({json.dumps(_chip())}, {json.dumps(candidate)}, steps);
        console.log(JSON.stringify({{ ...r, customized: globalThis.__customized,
            afterReset: globalThis.__afterReset }}));
    """)
    assert result["customized"]["mode"] == "customized"
    assert result["customized"]["disabled"] is False
    assert result["refused"] is True
    assert "at least one supported task type" in result["refusal"]["error"]
    assert result["afterReset"]["mode"] == "automatic"
    assert result["afterReset"]["disabled"] is True
    assert "task_types" not in result["saved"]["config"]["overrides"]


def test_hostile_fragments_are_rejected_before_normalization():
    result = _run_l3(r"""
const ok = { format: "sonder-prompt-fragment@1", project: "P", scene: "S", op: "copy",
    nodes: [{ type: "attachment", attachment_id: "c" }], attachments: [{ attachment_id: "c", kind: "custom" }] };
const variants = {
    valid: ok,
    badJson: "{",
    wrongFormat: { ...ok, format: "other" },
    duplicateIds: { ...ok, attachments: [ok.attachments[0], ok.attachments[0]] },
    danglingNode: { ...ok, nodes: [{ type: "attachment", attachment_id: "missing" }] },
    badCapability: { ...ok, nodes: [{ type: "attachment", attachment_id: "c", capability_id: "nope" }] },
    unknownKind: { ...ok, attachments: [{ attachment_id: "c", kind: "script" }] },
    numericId: { ...ok, attachments: [{ attachment_id: 7, kind: "custom" }] },
    unknownNode: { ...ok, nodes: [{ type: "html", text: "<b>" }] },
    deep: { ...ok, attachments: [{ attachment_id: "c", kind: "custom",
        config: JSON.parse("{\"a\":".repeat(20) + "1" + "}".repeat(20)) }] },
    huge: { ...ok, nodes: [{ type: "text", text: "x".repeat(300000) }] },
};
const out = {};
for (const [name, value] of Object.entries(variants))
    out[name] = mod.parsePromptFragment(typeof value === "string" ? value : JSON.stringify(value)) !== null;
console.log(JSON.stringify(out));
""")
    assert result.pop("valid") is True
    assert not any(result.values()), result


def test_the_paste_verdict_applies_each_surface_s_authoring_checks():
    result = _run_l3(r"""
const scene = { _context_consumer_start: 48, prompt_sections: [
    { prompt_id: "early", start_frame: 0 }, { prompt_id: "late", start_frame: 96 }] };
const ctx = { scene, semanticUnits: [{ semantic_unit_id: "u1" }] };
const v = (attachment, extra = {}) => mod.promptPasteVerdict(attachment, { referenceContext: () => ctx, ...extra });
console.log(JSON.stringify({
    customGlobal: v({ kind: "custom" }, { allowedKinds: ["reference", "custom"] }),
    linkGlobal: v({ kind: "prompt_link", source: { prompt_id: "early" } }, { allowedKinds: ["reference", "custom"] }),
    scopeOnlyInline: v({ kind: "timestamp" }),
    linkEarlier: v({ kind: "prompt_link", source: { prompt_id: "early" } }),
    linkLater: v({ kind: "prompt_link", source: { prompt_id: "late" } }),
    linkMissing: v({ kind: "prompt_link", source: { prompt_id: "gone" } }),
    vocalKnown: v({ kind: "vocal_event", source: { subject_ids: ["u1"] } }),
    vocalPending: v({ kind: "vocal_event", source: { subject_ids: ["pending"] } }),
    shotElsewhere: v({ kind: "shot" }, { shotPlacementAt: () => ({ offered: false }) }),
    shotMarker: v({ kind: "shot" }, { shotPlacementAt: () => ({ offered: true }) }),
    referencePending: v({ kind: "reference", source: { semantic_unit_ids: ["pending"] } }),
}));
""")
    assert result["customGlobal"] is None and result["linkEarlier"] is None
    assert result["vocalKnown"] is None and result["shotMarker"] is None
    for refused in ("linkGlobal", "scopeOnlyInline", "linkLater", "linkMissing",
                    "vocalPending", "shotElsewhere", "referencePending"):
        assert isinstance(result[refused], str) and result[refused], refused


def test_delete_on_a_focused_chip_ignores_an_older_range():
    """Phase 3.2: with focus on a chip, Delete/Backspace act on that chip only;
    an older prose range still live is not deleted."""
    result = _run_p3(r"""
const a = make([{ type: "text", node_id: "t1", text: "keep left " },
    { type: "attachment", node_id: "n1", attachment_id: "c1" },
    { type: "text", node_id: "t2", text: " keep right" }], [custom("c1")]);
place(firstText(spanOf(a, "t1")), 0, firstText(spanOf(a, "t2")), 6);   // the older range
const chipEl = a.childNodes.find((n) => n.dataset?.attachmentId === "c1");
document.activeElement = chipEl;
a._sonderOwnedKeydown({ key: "Delete", target: chipEl, ctrlKey: false, metaKey: false, shiftKey: false,
    isComposing: false, preventDefault() {}, stopPropagation() {} });
console.log(JSON.stringify(summary(a)));
""")
    assert "".join(result["nodes"]) == "keep left  keep right" and result["atts"] == []


@pytest.mark.parametrize("owner", ["chip", "page"])
def test_a_range_this_editor_does_not_own_is_never_deleted(owner):
    """Phase 3.2: a destructive input (here deleteByCut and deleteContent) on
    a range while focus is on a chip or elsewhere deletes nothing."""
    result = _run_p3(f"""
const owner = {json.dumps(owner)};
""" + r"""
const a = make([{ type: "text", node_id: "t1", text: "keep left " },
    { type: "attachment", node_id: "n1", attachment_id: "c1" },
    { type: "text", node_id: "t2", text: " keep right" }], [custom("c1")]);
place(firstText(spanOf(a, "t1")), 0, firstText(spanOf(a, "t2")), 6);
document.activeElement = owner === "chip" ? a.childNodes.find((n) => n.dataset?.attachmentId === "c1") : document.body;
const prevented = [];
for (const inputType of ["deleteByCut", "deleteContentBackward"]) {
    const event = { inputType, cancelable: true, defaultPrevented: false, isComposing: false,
        preventDefault() { this.defaultPrevented = true; } };
    fire(a, "beforeinput", event);
    prevented.push(event.defaultPrevented);
}
console.log(JSON.stringify({ prevented, ...summary(a) }));
""")
    assert result["prevented"] == [True, True]
    assert result["nodes"] == ["keep left ", "[c1]", " keep right"]


# --- Phase 3 audit fixes ---------------------------------------------------

def test_a_reference_bound_to_an_identity_pastes_with_a_host_shaped_context():
    """Phase 3 audit H1: hosts name the profile `resolvedProfile`; the verdict
    read `profile`, so every identity-bound Reference (MiniMax H3) was
    refused. Pending identities (M3) are refused."""
    result = _run_l3(r"""
const resolvedProfile = { identity_kinds: [{ key: "subject", label: "Subject" }], physical_populations: ["pictures"] };
const ctx = { scene: { prompt_sections: [] }, semanticUnits: [
        { semantic_unit_id: "u1", kind: "subject", name: "Hero", sources: [] },
        { semantic_unit_id: "u2", kind: "subject", name: "New", sources: [] }],
    profileId: "minimax_h3@1", scope: "global", resolvedProfile, references: [],
    pendingSemanticUnitIds: ["u2"] };
const v = (attachment) => mod.promptPasteVerdict(attachment, { referenceContext: () => ctx });
console.log(JSON.stringify({
    bound: v({ kind: "reference", source: { semantic_unit_ids: ["u1"] } }),
    pending: v({ kind: "reference", source: { semantic_unit_ids: ["u2"] } }),
    pendingSpeaker: v({ kind: "vocal_event", source: { subject_ids: ["u2"] } }),
}));
""")
    assert result["bound"] is None
    assert "being created" in result["pending"]
    assert "being created" in result["pendingSpeaker"]


@pytest.mark.parametrize("case", ["locked-source", "undone-in-source"])
def test_a_cut_that_did_not_remove_its_chip_pastes_a_copy(case):
    """Phase 3 audit M1: a cut that deleted nothing (a locked box), or whose
    source box undid it, must not paste a chip linked to the one that stayed."""
    result = _run_p3(f"""
const which = {json.dumps(case)};
""" + r"""
const a = source(); selectAll(a);
if (which === "locked-source") a.contentEditable = "false";
const { data, event } = clip();
fire(a, "cut", event);
if (which === "undone-in-source") undo(a);
const b = target();
fire(b, "paste", clip(data).event);
console.log(JSON.stringify({ source: summary(a), pasted: summary(b), op: JSON.parse(data["application/x-sonder-prompt-fragment"]).op }));
""")
    assert result["source"]["atts"] == [["c1", "g1"]]
    [[_pasted_id, pasted_group]] = result["pasted"]["atts"]
    assert pasted_group != "g1"
    if case == "locked-source":
        assert result["op"] == "copy"


def test_typing_or_pasting_over_a_range_the_editor_does_not_own_changes_nothing():
    """Phase 3 audit M2: with focus moved to a chip (Tab) and an older range
    still live, typing and paste must not replace that range."""
    result = _run_p3(r"""
const a = make([{ type: "text", node_id: "t1", text: "keep left " },
    { type: "attachment", node_id: "n1", attachment_id: "c1" },
    { type: "text", node_id: "t2", text: " keep right" }], [custom("c1")]);
place(firstText(spanOf(a, "t1")), 0, firstText(spanOf(a, "t2")), 6);
const chipEl = a.childNodes.find((n) => n.dataset?.attachmentId === "c1");
document.activeElement = chipEl;
const typed = { inputType: "insertText", data: "x", cancelable: true, defaultPrevented: false,
    isComposing: false, preventDefault() { this.defaultPrevented = true; } };
fire(a, "beforeinput", typed);
const { event } = clip({ "text/plain": "PASTED" });
event.target = chipEl;
fire(a, "paste", event);
console.log(JSON.stringify({ typedPrevented: typed.defaultPrevented, ...summary(a) }));
""")
    assert result["typedPrevented"] is True
    assert result["nodes"] == ["keep left ", "[c1]", " keep right"]


def test_writing_judges_a_pasted_chip_against_the_block_it_lands_in():
    """Phase 3 audit M4: a host's `pasteContext` (Writing's block context)
    decides, not the box-wide reference context."""
    result = _run_p3(r"""
const a = make([{ type: "text", node_id: "t1", text: "one " },
    { type: "attachment", node_id: "n1", attachment_id: "c1" },
    { type: "text", node_id: "t2", text: " two" }],
    [{ attachment_id: "c1", kind: "prompt_link", source: { prompt_id: "late", channel_key: "visual" }, config: {} }]);
selectAll(a);
const { data, event } = clip();
fire(a, "copy", event);
const b = mod.createPromptDocumentEditor({ document: { nodes: [{ type: "text", node_id: "x", text: "X" }] }, onChange: () => {} });
b.focus = function () { document.activeElement = this; };
const scene = { prompt_sections: [{ prompt_id: "late", start_frame: 96 }] };
mod.installPromptContextMenu({ editor: b, clipboardScope: () => ({ project: "P", scene: "S" }),
    referenceContext: () => ({ scene: { ...scene }, semanticUnits: [], scope: "global" }),
    pasteContext: () => ({ scene: { ...scene, _context_consumer_start: 48 }, semanticUnits: [], scope: "section" }) });
place(firstText(spanOf(b, "x")), 1); document.activeElement = b;
fire(b, "paste", clip(data).event);
console.log(JSON.stringify(summary(b)));
""")
    assert result["atts"] == []                       # a link to a later block is refused
    assert "".join(result["nodes"]) == "Xone  two"


def test_a_fragment_with_a_non_array_id_list_is_rejected_and_pastes_text():
    """Phase 3 audit L1: a crafted `subject_ids` used to throw inside the
    verdict after the paste was prevented, so nothing pasted at all."""
    result = _run_p3(r"""
const bad = { format: "sonder-prompt-fragment@1", project: "P", scene: "S", op: "copy",
    nodes: [{ type: "text", text: "hello " }, { type: "attachment", attachment_id: "v" }],
    attachments: [{ attachment_id: "v", kind: "vocal_event", source: { subject_ids: "u1" } }] };
const b = target();
fire(b, "paste", clip({ "text/plain": "hello ", "application/x-sonder-prompt-fragment": JSON.stringify(bad) }).event);
console.log(JSON.stringify({ parsed: mod.parsePromptFragment(JSON.stringify(bad)), ...summary(b) }));
""")
    assert result["parsed"] is None
    assert "".join(result["nodes"]) == "Xhello " and result["atts"] == []


# --- Phase 4 -----------------------------------------------------------------

def test_a_refusal_is_published_again_once_the_save_stops_pending():
    """Phase 4: the recovery bar is first drawn from the refusal published
    during the drain (save still pending, Discard disabled); the drain says it
    again once nothing is pending, so the bar redraws with Discard enabled."""
    url = _url("web/js/prompt_edit_intent.js")
    result = _run_node(f"""
const I = await import({url});
const drafts = new Map();
const events = [];
I.addPromptDraftListener(drafts, (event) => {{
    if (event.refused) events.push({{ pending: Boolean(drafts.get(event.key)?.pending) }});
}});
const doc = (t) => ({{ nodes: [{{ type: "text", node_id: "n", text: t }}] }});
const mount = I.createPromptDraftMount({{ drafts, key: "k", shown: {{ documents: {{ v: doc("A") }} }},
    displayed: {{ documents: {{ v: doc("A") }} }} }});
mount.record({{ documents: {{ v: doc("B") }} }});
await I.savePromptDraft(drafts, "k", async () => ({{ status: "refused", message: "no" }}));
console.log(JSON.stringify({{ events, pendingAfter: Boolean(drafts.get("k")?.pending) }}));
""")
    assert result["events"][0]["pending"] is True
    assert result["events"][-1]["pending"] is False
    assert result["pendingAfter"] is False


def test_discard_confirms_in_the_panel_never_with_a_native_dialog():
    """Phase 4: `confirm()` answered `false` in hosts that suppress dialogs,
    so Discard did nothing. Pinned by source: the panel cannot mount in node;
    the behaviour was checked live (plan record)."""
    panel = _source("web/js/editor_prompt_panel.js")
    start = panel.index('const discard = keepRecoveryFocus(makeBtn("Discard"')
    handler = panel[start:panel.index("draftRecovery.append(retry, useServer, dismiss, discard);", start)]
    for native in ("globalThis.confirm", "window.confirm", "confirm?.("):
        assert native not in handler, native
    assert "Discard this draft and reload the server's text?" in handler
    assert "registerKeyboardConsumer({" in handler
    assert "draftRecovery.contains(document.activeElement)" in handler


# --- Follow-up (manual checks 498, 509, 511) -------------------------------

def test_writing_finds_the_caret_s_block_by_position_not_node_id():
    """Manual check 509: the split gives text after a separator new ids, so
    looking the caret's node id up in the blocks fell back to block 1, and a
    Prompt link pasted into a later block was refused."""
    from test_prompt_authoring_ux import _run_panel_script
    chips = json.dumps((ROOT / "web/js/prompt_context_chips.js").as_uri())
    result = _run_panel_script(f"""
const chips = await import({chips});
""" + r"""
const nl = String.fromCharCode(10);
const t = (id, text) => ({ type: "text", node_id: id, text });
// The shape Reset rebuilds: each separator its own node.
const rebuilt = { nodes: [t("b1", "block 1"), t("s1", nl + "---" + nl), t("b2", "block 2"),
    t("s2", nl + "---" + nl), t("b3", "block 3"), t("s3", nl + "---" + nl), t("b4", "block 4")] };
// Hand-typed: one node across every separator.
const typed = { nodes: [t("T", "one" + nl + "---" + nl + "two" + nl + "---" + nl + "three"
    + nl + "---" + nl + "four")] };
const at = (draft, node_id, offset, keepEmpty = true) =>
    mod.writingBlockIndexAt(draft, { node_id, offset }, { keepEmpty });
const oldLookup = chips.splitWritingPromptDocument(rebuilt, { keepEmpty: true })
    .findIndex((block) => block.nodes.some((node) => node.node_id === "b4"));
const withEmpty = { nodes: [t("A", "one" + nl + "---" + nl + "   " + nl + "---" + nl + "three")] };
const typedText = typed.nodes[0].text;
// The verdict the paste runs, with the context built from the found block.
const verdictAt = (index) => chips.promptPasteVerdict(
    { kind: "prompt_link", source: { prompt_id: "p2" } },
    { referenceContext: () => ({ scene: { _context_consumer_start: index * 48, prompt_sections: [
        { prompt_id: "p1", start_frame: 0 }, { prompt_id: "p2", start_frame: 48 },
        { prompt_id: "p3", start_frame: 96 }, { prompt_id: "p4", start_frame: 144 }] },
        semanticUnits: [] }) });
console.log(JSON.stringify({
    oldLookup,
    rebuiltB4: at(rebuilt, "b4", 3), rebuiltB2: at(rebuilt, "b2", 0),
    typedFour: at(typed, "T", typedText.length - 2),
    typedThreeEnd: at(typed, "T", typedText.indexOf("three") + 5),
    unknown: at(typed, "missing", 0),
    emptyMiddleKeep: at(withEmpty, "A", withEmpty.nodes[0].text.length, true),
    emptyMiddleDrop: at(withEmpty, "A", withEmpty.nodes[0].text.length, false),
    pasteBlock4: verdictAt(at(rebuilt, "b4", 3)),
    pasteBlock1: verdictAt(at(rebuilt, "b1", 2)),
    pasteSameStart: verdictAt(at(rebuilt, "b2", 2)),
}));
""")
    assert result["oldLookup"] == -1            # the bug: b4 is in no block
    assert result["rebuiltB4"] == 3 and result["rebuiltB2"] == 1
    assert result["typedFour"] == 3 and result["typedThreeEnd"] == 2
    assert result["unknown"] == -1
    assert result["emptyMiddleKeep"] == 2 and result["emptyMiddleDrop"] == 1
    assert result["pasteBlock4"] is None
    assert "earlier section" in result["pasteBlock1"]
    assert "earlier section" in result["pasteSameStart"]


def _identity_save_host(global_scope=False):
    """The real section (or global) save, take and settle, on a host whose
    mutation queue the test answers by hand."""
    widget = _source("web/js/editor_widget.js")
    method = (_method(widget, "_updateSceneGlobalContext", "_setSectionGlobalInherit") if global_scope
              else _method(widget, "_updatePromptSection", "_updateLinkedPromptAttachment"))
    take = _method(widget, "_takePromptIdentityCreateIntents", "_adoptPromptIdentitiesFromMutation")
    resolve = _method(widget, "_resolvePanelPromptSection", "_deletePromptSectionWithinGesture")
    blocked = _method(widget, "_promptSaveBlocked", "_updateLinkedPromptAttachment")
    urls = {name: json.dumps((ROOT / f"web/js/{name}.js").as_uri()) for name in
            ("prompt_edit_intent", "prompt_composition", "prompt_channel_templates", "prompt_context_chips")}
    return f"""
const {{promptEditFields}}=await import({urls["prompt_edit_intent"]});
const {{normalizeChannels,composeSectionText}}=await import({urls["prompt_composition"]});
const {{projectTemplateValue,getChannelTemplate}}=await import({urls["prompt_channel_templates"]});
const {{promptPasteVerdict}}=await import({urls["prompt_context_chips"]});
const globalScope={json.dumps(global_scope)};
const docsKey=globalScope?"global_channel_docs":"channel_docs", channelsKey=globalScope?"global_channels":"channels", chipsKey=globalScope?"global_attachments":"attachments";
const baseline={{prompt_id:"stable",start_frame:0,end_frame:24,[docsKey]:{{visual:{{text:"v"}}}},[channelsKey]:{{visual:"v"}},[chipsKey]:[]}};
class Host {{
 {method}
 {take}
 {resolve}
 {blocked}
 constructor() {{this.projectDir="p";this.activeSceneId="s";this.row=structuredClone(baseline);this.activeScene=globalScope?this.row:{{prompt_sections:[this.row]}};this.calls=[];this._promptToolDrafts=new Map();}}
 _projectDirName(){{return "p";}} _channelTemplate(){{return getChannelTemplate("sonder");}}
 _isGlobalPromptTrackLocked(){{return false;}} _isPromptTrackLocked(){{return false;}}
 _pushUndo(label){{return {{label}};}} _discardUndoEntry(){{}}
 _renderSceneAfterLocalMutation(){{}} _refreshPromptContextDependencyConsumers(){{}}
 _adoptPromptIdentitiesFromMutation(){{}} _finalizePromptIdentityCreationHistory(){{}}
 _applyLocalPromptUpdate(index,fields){{Object.assign(this.row,fields);}}
 _fetchReferences(){{return Promise.resolve();}}
 _runSceneMutation(operations){{return new Promise((resolve,reject)=>this.calls.push({{operations,resolve,reject}}));}}
}}
const host=new Host();
const draftKey=JSON.stringify(["p","s",globalScope?"global":"stable"]);
const identity={{unit:{{semantic_unit_id:"u1",name:"Ada"}}}}, chip={{attachment_id:"chip1",kind:"reference",semantic_unit_id:"u1"}};
const pend=()=>{{host._pendingPromptIdentityCreateIntents=new Map([["u1",{{attachmentId:"chip1",intent:structuredClone(identity)}}]]);}};
const save=(text)=>{{const row=structuredClone(baseline);row[docsKey].visual.text=text;row[channelsKey].visual=text;row[chipsKey]=[chip];
 return globalScope?host._updateSceneGlobalContext(row[channelsKey],row[docsKey],row[chipsKey],{{baseline}}):host._updatePromptSection(0,{{channels:row.channels,channel_docs:row.channel_docs,attachments:row.attachments}},{{baseline}});}};
// The paste verdict exactly as the paste contexts build it: the map's keys.
const verdict=()=>promptPasteVerdict({{kind:"reference",source:{{semantic_unit_ids:["u1"],picture_ids:["pic"]}}}},{{referenceContext:()=>({{scene:{{}},
 semanticUnits:[{{semantic_unit_id:"u1"}}],pendingSemanticUnitIds:[...(host._pendingPromptIdentityCreateIntents?.keys?.()||[])]}})}});
const creates=(call)=>call.operations.filter(op=>op.unit).length;
"""


@pytest.mark.parametrize("global_scope", [False, True])
def test_a_pasted_chip_waits_for_its_identity_until_the_server_answers(global_scope):
    """Manual check 511: the save took the pending create out of the map when
    it was built, so a paste while the create was in flight bound a second
    chip to an identity the server did not have yet."""
    result = _run_node(_identity_save_host(global_scope) + """
pend();
const first=save("A");
const whileSending=verdict(), stillPending=host._pendingPromptIdentityCreateIntents.has("u1");
const second=save("B");                         // same chip, create still in flight
host.calls[0].resolve({payload:{}}); await first;
host.calls[1].resolve({payload:{}}); await second;
const afterAck=verdict(), leftAfterAck=host._pendingPromptIdentityCreateIntents.size;
// A refused create stays pending, unsent, and the retry sends it again.
pend();
const third=save("C");
host.calls[2].reject(Object.assign(new Error("busy"),{code:"project_version_conflict"})); await third;
const afterRefusal=verdict(), sendingAfterRefusal=!!host._pendingPromptIdentityCreateIntents.get("u1")?.sending;
const fourth=save("D");
host.calls[3].resolve({payload:{}}); await fourth;
const afterRetry=verdict();
console.log(JSON.stringify({whileSending, stillPending, afterAck, leftAfterAck, afterRefusal, sendingAfterRefusal, afterRetry,
 creates:host.calls.map(creates), left:host._pendingPromptIdentityCreateIntents.size}));
""")
    assert result["stillPending"] is True
    assert "still being created" in result["whileSending"]
    # No longer refused as pending (eligibility proper needs the catalogue).
    assert "still being created" not in str(result["afterAck"]) and result["leftAfterAck"] == 0
    assert "still being created" in result["afterRefusal"]
    assert result["sendingAfterRefusal"] is False
    assert "still being created" not in str(result["afterRetry"])
    # Sent once and not again by the save that overlapped it (single-flighted
    # per draft in the app, acknowledged here in order), then once more on
    # the retry after a refusal.
    assert result["creates"] == [1, 0, 1, 1]
    assert result["left"] == 0


def test_an_acknowledged_identity_is_not_put_back_by_the_draft_s_copy():
    """Audit M2: the draft kept its copy of the create after the server took
    it, and the next save of that draft put it back and sent it again."""
    result = _run_node(_identity_save_host(False) + """
host._promptToolDrafts.set(draftKey,{});
pend();
const first=save("A"); const copied=host._promptToolDrafts.get(draftKey).identityCreates.length;
host.calls[0].resolve({payload:{}}); await first;
const kept=host._promptToolDrafts.get(draftKey).identityCreates.length;
const second=save("B"); host.calls[1].resolve({payload:{}}); await second;
// A collision refusal means the identity exists: drop, do not retry.
pend(); host._promptToolDrafts.set(draftKey,{});
const third=save("C");
host.calls[2].reject(Object.assign(new Error("collision"),{payload:{code:"prompt_semantic_unit_collision"}})); await third;
const afterCollision=host._pendingPromptIdentityCreateIntents.has("u1");
const fourth=save("D"); host.calls[3].resolve({payload:{}}); await fourth;
console.log(JSON.stringify({copied, kept, afterCollision, creates:host.calls.map(creates)}));
""")
    assert result == {"copied": 1, "kept": 0, "afterCollision": False, "creates": [1, 0, 1, 0]}


def test_writing_paste_waits_for_an_identity_a_channel_box_is_creating():
    """Audit M3: Writing's paste context named no pending identities, so a
    chip copied from a box whose create was in flight pasted into Writing
    bound to an identity the server might refuse."""
    from test_prompt_authoring_ux import _run_panel_script
    chips = json.dumps((ROOT / "web/js/prompt_context_chips.js").as_uri())
    result = _run_panel_script(f"""
const {{ promptPasteVerdict }} = await import({chips});
""" + """
const host = new Map([["boxUnit", { attachmentId: "c1", sending: true }], ["ownUnit", { attachmentId: "c2" }]]);
const own = [{ unit: { semantic_unit_id: "ownUnit" } }];
const ids = mod.writingPastePendingIds(host, own);
const verdict = (unit) => promptPasteVerdict({ kind: "reference", source: { semantic_unit_ids: [unit] } },
    { referenceContext: () => ({ scene: {}, semanticUnits: [], pendingSemanticUnitIds: ids }) });
console.log(JSON.stringify({ ids, box: verdict("boxUnit"), none: mod.writingPastePendingIds(undefined, own) }));
""")
    assert result["ids"] == ["boxUnit"]
    assert "still being created" in result["box"]
    assert result["none"] == []
    panel = _source("web/js/editor_prompt_panel.js")
    paste = panel[panel.index("pasteContext: (bookmark) => {"):]
    paste = paste[:paste.index("writingAids:")]
    assert "writingPastePendingIds(" in paste and "_pendingPromptIdentityCreateIntents" in paste


def test_a_writing_split_does_not_copy_an_inline_only_record_away_from_its_anchor():
    """Audit M4: a block split in Writing copied its Prompt link record, under a
    new id and with no anchor, into the new block, and Apply then carried an
    orphan the server's anchor guard refuses."""
    from test_prompt_authoring_ux import _run_panel_script
    result = _run_panel_script(r"""
const nl = String.fromCharCode(10);
const link = { attachment_id: "L", kind: "prompt_link", source: { prompt_id: "p0" } };
const ref = { attachment_id: "R", kind: "reference", source: { picture_ids: ["pic"] } };
const shot = { attachment_id: "S", kind: "shot" };
let n = 0;
const clones = mod.cloneSplitScopedAttachments([link, ref, shot], () => `c${++n}`);
const blocks = [
    { nodes: [{ type: "text", node_id: "a", text: "left " }, { type: "attachment", node_id: "x", attachment_id: "L" }] },
    { nodes: [{ type: "text", node_id: "b", text: "right" }] },
];
const { sections } = mod.writingSectionsFromDraft({ blocks, attachments: [link, ref, ...clones],
    blockMeta: [{ source_prompt_id: "p1", attachments: [link, ref] }, { source_prompt_id: "p2", attachments: clones }],
    allocations: [{ length: 24 }, { length: 24 }], channelKeys: ["visual"], defaultKey: "visual" });
const orphans = sections.map((section) => {
    const anchored = new Set(Object.values(section.channel_docs).flatMap((d) => d.nodes)
        .filter((node) => node.type === "attachment").map((node) => node.attachment_id));
    return section.attachments.filter((a) => ["prompt_link", "vocal_event"].includes(a.kind)
        && !anchored.has(a.attachment_id)).map((a) => a.attachment_id);
});
console.log(JSON.stringify({ kinds: clones.map((c) => c.kind), ids: clones.map((c) => c.attachment_id), orphans,
    leftKinds: sections[0].attachments.map((a) => a.kind).sort() }));
""")
    assert result["kinds"] == ["reference"] and result["ids"] == ["c1"]
    assert result["orphans"] == [[], []]
    assert result["leftKinds"] == ["prompt_link", "reference"]
    panel = _source("web/js/editor_prompt_panel.js")
    assert panel.count("cloneSplitScopedAttachments(") == 3   # definition and both split paths


@pytest.mark.parametrize("case", ["rebuilt-chip-back", "rebuilt-chip-gone", "source-closed", "chip-back-in-source"])
def test_a_cut_moves_only_while_its_chips_stay_gone_from_their_box(case):
    """Audit M5: the cut token was cleared only by Undo in the very editor
    that cut. A panel rebuild replaced that editor, the replacement's Undo
    brought the chip back, and the paste still moved it: two chips in one
    link group. Follow-up audit F2: a source with no replacement (a closed
    timeline bar) committed the cut, so its paste still moves."""
    result = _run_p3(f"""
const which = {json.dumps(case)};
""" + r"""
const a = source(); a.dataset.sonderHistoryKey = "k"; selectAll(a);
const { data, event } = clip();
fire(a, "cut", event);
if (which.startsWith("rebuilt")) {
    a.isConnected = false;                                   // the panel rebuilt it
    const back = which === "rebuilt-chip-back";
    const replacement = make([{ type: "text", node_id: "r1", text: "one " },
        ...(back ? [{ type: "attachment", node_id: "rn", attachment_id: "c1" }] : []),
        { type: "text", node_id: "r2", text: " two" }], back ? [custom("c1", "g1")] : []);
    replacement.dataset.sonderHistoryKey = "k";
    document.body.appendChild(replacement);
} else if (which === "source-closed") {
    a.isConnected = false;                                   // a bar closed: no replacement
} else {
    // The record back in the source's registry without an Undo in that editor.
    a.syncPromptAttachments([custom("c1", "g1")]);
}
const b = target();
fire(b, "paste", clip(data).event);
console.log(JSON.stringify({ pasted: summary(b) }));
""")
    [[_pasted_id, pasted_group]] = result["pasted"]["atts"]
    moves = case in ("rebuilt-chip-gone", "source-closed")
    assert (pasted_group == "g1") is moves


def test_an_acknowledgement_for_another_channel_keeps_escape_s_text():
    """Audit M1: an acknowledgement for any channel set the focused box's
    Escape target to its current state, so Escape stopped dropping what the
    author had typed since focusing it."""
    result = _run_panel_script_json(r"""
const atFocus = { document: { nodes: [{ type: "text", node_id: "a", text: "at focus" }] }, attachments: [] };
const now = { document: { nodes: [{ type: "text", node_id: "a", text: "at focus + typed" }] },
    attachments: [{ attachment_id: "stored", kind: "custom" }] };
console.log(JSON.stringify({ other: mod.focusedRevertAfterAck(atFocus, now, false),
    own: mod.focusedRevertAfterAck(atFocus, now, true) }));
""")
    assert result["other"]["document"]["nodes"][0]["text"] == "at focus"
    assert result["other"]["attachments"] == [{"attachment_id": "stored", "kind": "custom"}]
    assert result["own"]["document"]["nodes"][0]["text"] == "at focus + typed"
    panel = _source("web/js/editor_prompt_panel.js")
    assert "guard.focusedBox.revert = box.promptState" not in panel
    assert panel.count("reviseFocusedRevert(box, Object.hasOwn(repaint.documents, channelKey));") == 2


def test_escape_s_target_after_another_channel_s_ack_stays_consistent():
    """Follow-up audit F4: keeping the focus-time text while taking the stored
    chip list could pair an anchor with no record (a chip deleted and saved
    since focus) or leave a record with no anchor (a chip this box inserted
    since focus, which Escape drops with the typing)."""
    result = _run_panel_script_json(r"""
const t = (id, text) => ({ type: "text", node_id: id, text });
const a = (id) => ({ type: "attachment", node_id: "n" + id, attachment_id: id });
const rec = (id) => ({ attachment_id: id, kind: "prompt_link" });
// At focus: text with chip X. Since then: X deleted (saved), Y inserted here
// (saved), and another box added Z.
const atFocus = { document: { nodes: [t("a", "before "), a("X"), t("b", " after")] }, attachments: [rec("X"), rec("K")] };
const now = { document: { nodes: [t("a", "before  after typed "), a("Y")] }, attachments: [rec("K"), rec("Y"), rec("Z")] };
const out = mod.focusedRevertAfterAck(atFocus, now, false);
console.log(JSON.stringify({ anchors: out.document.nodes.filter((n) => n.type === "attachment").map((n) => n.attachment_id),
    text: out.document.nodes.filter((n) => n.type === "text").map((n) => n.text).join(""),
    records: out.attachments.map((r) => r.attachment_id) }));
""")
    assert result["anchors"] == []                     # X's anchor goes with its record
    assert result["text"] == "before  after"           # the focus-time text otherwise
    assert result["records"] == ["K", "Z"]             # Y goes with the typing; Z is another box's


def _run_panel_script_json(body):
    from test_prompt_authoring_ux import _run_panel_script
    return _run_panel_script(body)


def test_closing_the_discard_confirm_gives_the_box_back_its_escape_target():
    """Audit L2: the confirm's teardown refocused the box, whose focus handler
    recorded what it held now, so Escape no longer dropped what had been typed
    before the confirm. Pinned by source (the panel cannot mount in node);
    checked live."""
    panel = _source("web/js/editor_prompt_panel.js")
    start = panel.index('discard.addEventListener("click", () => {')
    handler = panel[start:panel.index("const state = { teardown };", start)]
    assert "const guardAtOpen = guard.focusedBox?.el === returnFocus ? guard.focusedBox : null;" in handler
    refocus = handler.index("returnFocus.focus?.(")
    restore = handler.index("guard.focusedBox = guardAtOpen;")
    assert restore > refocus
    # Follow-up audit F3: only when the teardown itself gave focus back; focus
    # the author moved there meanwhile records its own target.
    assert "if (hadFocus && guardAtOpen && document.activeElement === guardAtOpen.el)" in handler


def test_undo_survives_a_rebuild_of_the_box_and_still_refuses_the_orphan():
    """Manual check 498: a panel refresh landed between removing a legacy
    orphan from the scope row and Ctrl+Z, replaced every box, and their
    histories went with them: Ctrl+Z did nothing and said nothing. The new
    box now adopts the old one's history when it shows the same document."""
    result = _run_l3(_notices() + _L3_MORE + r"""
shared = [link("orphan")];
const docA = [{ type: "text", node_id: "ta", text: "a" }];
const docB = [{ type: "text", node_id: "tb", text: "b" }];
let a = mount("a", docA);
let b = mount("b", docB);
a.transactPromptAttachments([]);               // scope-row removal of the orphan
// The rebuild: take every history, mount fresh boxes from the same documents.
const takenA = a.takeHistory(), takenB = b.takeHistory();
a = mount("a", docA); b = mount("b", docB);
const withoutCarry = { history: a.takeHistory().history.length };
const adoptedA = a.adoptHistory(takenA), adoptedB = b.adoptHistory(takenB);
undo(a);
const own = { ids: ids(), seen: [...seen] };
// Same in the sibling, whose entry is the registry transaction it recorded.
shared = [link("orphan")];
seen.length = 0;
let c = mount("c", docA), d = mount("d", docB);
c.transactPromptAttachments([]);
const takenD = d.takeHistory();
d = mount("d", docB);
const adoptedD = d.adoptHistory(takenD);
undo(d);
console.log(JSON.stringify({ withoutCarry, adoptedA, adoptedB, own, adoptedD,
    sibling: { ids: ids(), seen: [...seen] } }));
""")
    assert result["withoutCarry"]["history"] == 0      # what a rebuild used to leave
    assert result["adoptedA"] is True and result["adoptedB"] is True
    assert result["own"]["ids"] == [] and _ORPHAN_NOTICE in result["own"]["seen"]
    assert result["adoptedD"] is True
    assert result["sibling"]["ids"] == [] and _ORPHAN_NOTICE in result["sibling"]["seen"]


def test_a_rebuilt_box_refuses_history_for_another_document_or_over_its_own():
    """The entries are relative to the text they were taken over: a box now
    showing other text (an acknowledgement repainted it, another writer
    changed it) starts fresh, and a box with history of its own keeps it."""
    result = _run_l3(_L3_MORE + r"""
shared = [];
let a = mount("a", [{ type: "text", node_id: "ta", text: "before" }]);
a.pushPromptHistory();
const taken = a.takeHistory();
const changed = mount("a", [{ type: "text", node_id: "ta", text: "after" }]);
const sameShape = mount("a", [{ type: "text", node_id: "x1", text: "bef" }, { type: "text", node_id: "x2", text: "ore" }]);
const busy = mount("a", [{ type: "text", node_id: "ta", text: "before" }]);
busy.pushPromptHistory();
const ownLength = busy.takeHistory().history.length;
console.log(JSON.stringify({ changed: changed.adoptHistory(taken), sameShape: sameShape.adoptHistory(taken),
    busy: busy.adoptHistory(taken), busyKept: busy.takeHistory().history.length === ownLength,
    empty: mount("a", [{ type: "text", node_id: "ta", text: "before" }]).adoptHistory({ history: [], future: [], document: taken.document }) }));
""")
    assert result == {"changed": False, "sameShape": True, "busy": False, "busyKept": True, "empty": False}


def test_the_panel_carries_box_histories_across_its_rebuild():
    """Pinned by source (the panel cannot mount in node); checked live on the
    kit server (manual check 498 and rows 494-497 after a forced refresh)."""
    panel = _source("web/js/editor_prompt_panel.js")
    render = panel[panel.index("    renderNow = () => {"):]
    render = render[:render.index("body.textContent = \"\";")]
    assert render.index("carryBoxHistories(body);") < render.index("liveMounts.clear();")
    carry = panel[panel.index("const carryBoxHistories = (root) => {"):panel.index("    renderNow = () => {")]
    assert "box.takeHistory?.()" in carry and "box.adoptHistory?.(state)" in carry
    assert "queueMicrotask(" in carry
    for key in ('JSON.stringify([writingDraftKey(), "writing", writingGeneration])',
                "JSON.stringify([globalDraftKey, key])", "JSON.stringify([sectionDraftKey, key])"):
        assert f"dataset.sonderHistoryKey = {key};" in panel, key


def test_writing_history_does_not_cross_an_apply_reset_or_restore():
    """Follow-up audit F1: Apply reloads the draft by reconstructing it from
    the sections, which can read the same as before, so the rebuilt Writing
    box adopted the old draft's history and Ctrl+Z restored block metadata
    from before the Apply. Each new draft bumps the history key's generation.
    Pinned by source (the panel cannot mount in node)."""
    panel = _source("web/js/editor_prompt_panel.js")
    reconstruct = panel[panel.index("const reconstructDraftFromSections = () => {"):]
    assert reconstruct[:reconstruct.index("\n", reconstruct.index("{") + 2) + 60].count("writingGeneration += 1;") == 1
    restore = panel[panel.index('restoreBtn?.addEventListener("click", () => {'):]
    restore = restore[:restore.index("writingState.document = normalizePromptDocument(")]
    assert "writingGeneration += 1;" in restore
    assert 'JSON.stringify([writingDraftKey(), "writing", writingGeneration])' in panel


def test_a_save_that_throws_before_sending_leaves_its_creates_unmarked():
    """Follow-up audit F5: a throw between building the save and sending it
    left the create marked as sending, never re-sent and never settled, so
    every paste of that identity stayed refused."""
    result = _run_node(_identity_save_host(False) + """
pend();
const realPush = host._pushUndo;
host._pushUndo = () => { throw new Error("history full"); };
let threw = false;
try { await save("A"); } catch (_error) { threw = true; }
host._pushUndo = realPush;
const sendingAfterThrow = !!host._pendingPromptIdentityCreateIntents.get("u1")?.sending;
const next = save("B"); host.calls[0].resolve({payload:{}}); await next;
console.log(JSON.stringify({ threw, sendingAfterThrow, creates: host.calls.map(creates), left: host._pendingPromptIdentityCreateIntents.size }));
""")
    assert result == {"threw": True, "sendingAfterThrow": False, "creates": [1], "left": 0}


def test_an_acknowledgement_after_a_project_switch_still_clears_the_draft_copy():
    """Follow-up audit F6: the draft's copy was filtered only by ids still in
    the map, so a project switch clearing the map mid-flight left the
    acknowledged create in the copy, and the draft's next save sent it again."""
    result = _run_node(_identity_save_host(False) + """
host._promptToolDrafts.set(draftKey, {});
pend();
const first = save("A");
host._pendingPromptIdentityCreateIntents.clear();          // project reset mid-flight
host.calls[0].resolve({payload:{}}); await first;
const copy = host._promptToolDrafts.get(draftKey).identityCreates.length;
const second = save("B"); host.calls[1].resolve({payload:{}}); await second;
console.log(JSON.stringify({ copy, creates: host.calls.map(creates) }));
""")
    assert result == {"copy": 0, "creates": [1, 0]}


def test_a_pasted_reference_naming_an_unknown_identity_is_refused_even_with_physical_sources():
    """Follow-up audit F8: with a physical source selected, the verdict never
    checked the chip's identities, so after a reload (pending map empty) a
    chip bound to an identity whose create never landed pasted with a
    dangling id."""
    chips = json.dumps((ROOT / "web/js/prompt_context_chips.js").as_uri())
    result = _run_node(f"""
const {{ promptPasteVerdict }} = await import({chips});
const chip = {{ kind: "reference", source: {{ semantic_unit_ids: ["gone"], picture_ids: ["pic"] }} }};
const ctx = (units) => ({{ referenceContext: () => ({{ scene: {{}}, semanticUnits: units, pendingSemanticUnitIds: [] }}) }});
console.log(JSON.stringify({{ unknown: promptPasteVerdict(chip, ctx([])),
    known: promptPasteVerdict(chip, ctx([{{ semantic_unit_id: "gone" }}])) }}));
""")
    assert result["unknown"] == "its identity is not saved yet"
    assert result["known"] != "its identity is not saved yet"
