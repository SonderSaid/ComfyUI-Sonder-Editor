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
    assert result["resolved"] == {"resolved": "document visual", "remaining": ["document speech"]}
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


def test_undo_does_not_restore_an_inline_only_record_with_no_anchor():
    """Phase 2.4: Undo of a scope-row removal of a legacy orphan Prompt link
    would bring the orphan back; it is left out with a notice."""
    result = _run_l3(_L3_MORE + r"""
shared = [link("orphan")];
const a = mount("a", [{ type: "text", node_id: "ta", text: "a" }]);
mount("b", [{ type: "text", node_id: "tb", text: "b" }]);
a.transactPromptAttachments([]);
undo(a);
console.log(JSON.stringify({ ids: ids() }));
""")
    assert result["ids"] == []


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

@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="L1; Phase 4 (an empty customized list displays as automatic)")
def test_customize_unlocks_the_task_types_when_no_role_derives_one():
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
