"""A focused chip never leaves the author without a text caret.

Chip Focus and Caret plan (2026-11-23; Firefox Reddit report, bug tracker
"Firefox: a trailing Context chip can take the caret or keyboard focus").
These node tests pin the editor-side decisions. The browser harness
(`tests/browser/prompt_chip_selection/run.mjs`) is the authority for what real
browsers do with focus, pointer capture and native key delivery; the fakes here
cannot reproduce any of that.
"""

import json

from test_prompt_chip_enter import _SELECTION
from test_prompt_context_corrections import _run_chip_dom_script
from test_prompt_tool_integrity_baseline import _run_p3


# Three nodes with the chip in the middle, plus a trailing-chip variant.
_SETUP = r"""
const key = (editor, target, k, extra = {}) => {
    const event = { key: k, target, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false,
        isComposing: false, keyCode: 0, prevented: false,
        preventDefault() { this.prevented = true; }, stopPropagation() {}, ...extra };
    const handled = editor._sonderOwnedKeydown(event);
    return { handled, prevented: event.prevented };
};
const chipOf = (e, id = "c1") => e.childNodes.find((n) => n.dataset?.attachmentId === id);
const caret = (e) => {
    const r = getSelection().getRangeAt(0);
    if (!r) return null;
    if (r.startContainer === e) return { at: "editor", offset: r.startOffset, collapsed: r.collapsed };
    return { at: r.startContainer?.nodeType === 3 ? "text" : "element", collapsed: r.collapsed };
};
const middle = () => make([{ type: "text", node_id: "t1", text: "keep left " },
    { type: "attachment", node_id: "n1", attachment_id: "c1" },
    { type: "text", node_id: "t2", text: " keep right" }], [custom("c1")]);
"""


def _run(body):
    return _run_p3(_SETUP + body)


def test_paste_shortcut_on_a_focused_chip_moves_the_caret_after_it_without_consuming_the_key():
    """Ctrl+V, Ctrl+Shift+V and Shift+Insert on a focused chip: the caret goes
    after the chip and the key is NOT consumed, so the browser pastes into the
    now-focused editor. An older range is collapsed first, never replaced."""
    result = _run(r"""
const out = {};
for (const [label, k, extra] of [["ctrlV", "v", { ctrlKey: true }],
        ["ctrlShiftV", "V", { ctrlKey: true, shiftKey: true }],
        ["metaV", "v", { metaKey: true }],
        ["shiftInsert", "Insert", { shiftKey: true }]]) {
    const a = middle();
    place(firstText(spanOf(a, "t1")), 0, firstText(spanOf(a, "t2")), 6);   // an older range
    const chipEl = chipOf(a);
    document.activeElement = chipEl;
    const r = key(a, chipEl, k, extra);
    out[label] = { ...r, focus: document.activeElement === a ? "editor" : "chip", caret: caret(a),
        chipIndex: a.childNodes.indexOf(chipEl) };
}
console.log(JSON.stringify(out));
""")
    for label, row in result.items():
        assert row["handled"] is False and row["prevented"] is False, label
        assert row["focus"] == "editor", label
        assert row["caret"] == {"at": "editor", "offset": row["chipIndex"] + 1, "collapsed": True}, label


def test_a_paste_a_chip_receives_lands_after_that_chip_never_at_the_start():
    """Chromium fires the paste at the focused chip; inserting from there ran
    `editor.focus()` with no caret, which put the text at offset 0."""
    result = _run(r"""
const a = middle();
const chipEl = chipOf(a);
document.activeElement = chipEl;
active = null;                                    // no caret in the text at all
const { event } = clip({ "text/plain": "PASTED" });
event.target = chipEl;
fire(a, "paste", event);
const pasted = summary(a);

const locked = middle();
locked.disabled = true;
const lockedChip = chipOf(locked);
document.activeElement = lockedChip;
active = null;
const second = clip({ "text/plain": "PASTED" });
second.event.target = lockedChip;
fire(locked, "paste", second.event);
console.log(JSON.stringify({ pasted, locked: summary(locked) }));
""")
    assert result["pasted"]["nodes"] == ["keep left ", "[c1]", "PASTED keep right"]
    assert result["locked"]["nodes"] == ["keep left ", "[c1]", " keep right"]


def test_the_older_range_rule_still_wins_over_the_after_chip_paste():
    """The pinned Phase 3 M2 behaviour is unchanged: a chip-targeted paste with
    an older range still live replaces nothing and inserts nothing."""
    result = _run(r"""
const a = middle();
place(firstText(spanOf(a, "t1")), 0, firstText(spanOf(a, "t2")), 6);
const chipEl = chipOf(a);
document.activeElement = chipEl;
const { event } = clip({ "text/plain": "PASTED" });
event.target = chipEl;
fire(a, "paste", event);
console.log(JSON.stringify(summary(a)));
""")
    assert result["nodes"] == ["keep left ", "[c1]", " keep right"]


def test_typing_on_a_focused_chip_continues_after_it_and_space_and_enter_still_open_it():
    """Part B: a printable key moves the caret after the chip and is left to the
    browser (native input, one history entry, the @ menu). Space and Enter keep
    the button convention; commands, composition and a locked box are untouched."""
    result = _run(r"""
const out = {};
let opened = 0;
const build = (opts = {}) => {
    const e = mod.createPromptDocumentEditor({ document: { nodes: [
        { type: "text", node_id: "t1", text: "keep left " },
        { type: "attachment", node_id: "n1", attachment_id: "c1" },
        { type: "text", node_id: "t2", text: " keep right" }] },
        attachments: [custom("c1")], onChange: () => {},
        onActivateAttachment: () => { opened += 1; }, ...opts });
    e.focus = function () { document.activeElement = this; };
    e.isConnected = true;
    return e;
};
const press = (label, k, extra = {}, opts = {}) => {
    const e = build(opts);
    const chipEl = chipOf(e);
    document.activeElement = chipEl;
    active = null;
    const before = opened;
    const r = key(e, chipEl, k, extra);
    out[label] = { ...r, focus: document.activeElement === e ? "editor" : "chip",
        opened: opened - before, caret: active ? caret(e) : null, chipIndex: e.childNodes.indexOf(chipEl) };
};
press("letter", "x");
press("upper", "X", { shiftKey: true });
press("at", "@", { shiftKey: true });
press("emoji", "\u{1F600}");
press("dead", "Dead");
press("altGraph", "@", { ctrlKey: true, altKey: true, getModifierState: (m) => m === "AltGraph" });
press("space", " ");
press("enter", "Enter");
press("ctrlA", "a", { ctrlKey: true });
press("tab", "Tab");
press("composing", "x", { keyCode: 229 });
press("altLetter", "f", { altKey: true });
press("locked", "x", {}, { disabled: true });
console.log(JSON.stringify(out));
""")
    for label in ("letter", "upper", "at", "emoji", "dead", "altGraph"):
        row = result[label]
        assert row["handled"] is False and row["prevented"] is False, label
        assert row["focus"] == "editor" and row["opened"] == 0, label
        assert row["caret"] == {"at": "editor", "offset": row["chipIndex"] + 1, "collapsed": True}, label
    for label in ("space", "enter"):
        assert result[label]["handled"] is True and result[label]["opened"] == 1, label
        assert result[label]["focus"] == "chip", label
    for label in ("ctrlA", "tab", "composing", "locked", "altLetter"):
        assert result[label]["focus"] == "chip" and result[label]["opened"] == 0, label
        assert result[label]["caret"] is None, label


def test_arrow_keys_hand_focus_back_to_the_text_beside_the_chip():
    result = _run(r"""
const out = {};
for (const [label, k, locked] of [["left", "ArrowLeft", false], ["right", "ArrowRight", false],
        ["lockedRight", "ArrowRight", true]]) {
    const a = middle();
    if (locked) a.disabled = true;
    const chipEl = chipOf(a);
    document.activeElement = chipEl;
    active = null;
    const r = key(a, chipEl, k);
    out[label] = { ...r, focus: document.activeElement === a ? "editor" : "chip", caret: caret(a),
        chipIndex: a.childNodes.indexOf(chipEl) };
}
console.log(JSON.stringify(out));
""")
    left, right, locked = result["left"], result["right"], result["lockedRight"]
    assert left["handled"] is True and left["focus"] == "editor"
    assert left["caret"] == {"at": "editor", "offset": left["chipIndex"], "collapsed": True}
    assert right["focus"] == "editor"
    assert right["caret"] == {"at": "editor", "offset": right["chipIndex"] + 1, "collapsed": True}
    # A locked box cannot take focus: the old caret-only move stays.
    assert locked["focus"] == "chip"
    assert locked["caret"] == {"at": "editor", "offset": locked["chipIndex"] + 1, "collapsed": True}


def test_closing_a_chip_dialog_returns_the_caret_after_the_chip():
    """The dialog removes itself without returning focus; the page held focus
    and the author had no caret. A host that placed focus, an open modal and a
    locked box are left alone."""
    result = _run(r"""
const out = {};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const run = async (label, { focusAfter = "body", modal = false, locked = false } = {}) => {
    let release;
    const e = mod.createPromptDocumentEditor({ document: { nodes: [
        { type: "text", node_id: "t1", text: "keep left " },
        { type: "attachment", node_id: "n1", attachment_id: "c1" }] },
        attachments: [custom("c1")], onChange: () => {}, disabled: locked,
        onActivateAttachment: () => new Promise((resolve) => { release = resolve; }) });
    e.focus = function () { document.activeElement = this; };
    e.isConnected = true;
    const chipEl = chipOf(e);
    document.activeElement = chipEl;
    active = null;
    key(e, chipEl, "Enter");
    const backdrop = document.createElement("div");
    if (modal) { backdrop.dataset.sonderPromptContextModal = "1"; document.body.appendChild(backdrop); }
    const control = document.createElement("input");
    document.activeElement = focusAfter === "body" ? document.body : control;
    release(null);
    await settle();
    out[label] = { focus: document.activeElement === e ? "editor"
        : document.activeElement === document.body ? "body" : "other",
        caret: active ? caret(e) : null, chipIndex: e.childNodes.indexOf(chipEl) };
    backdrop.remove();
};
await run("cancelled");
await run("hostFocused", { focusAfter: "control" });
await run("modalOpen", { modal: true });
await run("locked", { locked: true });
console.log(JSON.stringify(out));
""")
    cancelled = result["cancelled"]
    assert cancelled["focus"] == "editor"
    assert cancelled["caret"] == {"at": "editor", "offset": cancelled["chipIndex"] + 1, "collapsed": True}
    assert result["hostFocused"]["focus"] == "other" and result["hostFocused"]["caret"] is None
    assert result["modalOpen"]["focus"] == "body" and result["modalOpen"]["caret"] is None
    assert result["locked"]["focus"] == "body" and result["locked"]["caret"] is None


def test_shift_f10_on_a_focused_chip_opens_the_insert_menu_at_the_chip():
    result = _run(r"""
// The menu measures the caret through `cloneRange`, which the fake lacks.
const plainRange = document.createRange;
document.createRange = () => Object.assign(plainRange(), {
    cloneRange() { const copy = document.createRange(); copy.setStart(this.startContainer, this.startOffset);
        copy.setEnd(this.endContainer, this.endOffset); return copy; },
    getClientRects: () => [], getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0 }) });
const a = middle();
place(firstText(spanOf(a, "t1")), 2);           // a stale caret elsewhere
const chipEl = chipOf(a);
document.activeElement = chipEl;
const r = key(a, chipEl, "F10", { shiftKey: true });
console.log(JSON.stringify({ ...r, focus: document.activeElement === a ? "editor" : "chip",
    caret: caret(a), chipIndex: a.childNodes.indexOf(chipEl),
    menu: document.querySelectorAll("[data-sonder-prompt-context-menu]").length }));
""")
    assert result["handled"] is True and result["menu"] >= 1
    assert result["focus"] == "editor"
    assert result["caret"] == {"at": "editor", "offset": result["chipIndex"] + 1, "collapsed": True}


# --- A1: a click right of a chip that ends its line -------------------------

_CLICK = r"""
const rect = { left: 10, right: 30, top: 5, bottom: 20, width: 20, height: 15 };
const build = (nodes) => {
    const e = mod.createPromptDocumentEditor({ document: { nodes },
        attachments: [{ attachment_id: "ref", kind: "reference", source: {}, config: {} }] });
    const chipEl = e.childNodes.find((n) => n.dataset?.nodeType === "attachment");
    chipEl.getBoundingClientRect = () => rect;
    chipEl.getClientRects = () => [rect];
    return { e, chipEl };
};
const textIn = (e, id) => e.childNodes.find((n) => n.dataset?.nodeId === id)
    .childNodes.find((n) => n.nodeType === 3);
// `live` is the browser's own result for the click, `point` what the point
// API reports; either alone may be the one that says "before the chip".
const click = (e, { x = 40, y = 10, live = null, point = undefined, positionApi = false } = {}) => {
    document.caretRangeFromPoint = undefined;
    document.caretPositionFromPoint = undefined;
    const pointRange = point === undefined ? live : point;
    if (positionApi) {
        document.caretPositionFromPoint = () => pointRange
            ? { offsetNode: pointRange.startContainer, offset: pointRange.startOffset } : null;
    } else {
        document.caretRangeFromPoint = () => pointRange;
    }
    active = live;
    e._handlers.click[0]({ target: e, button: 0, detail: 1, clientX: x, clientY: y, isComposing: false });
    const r = getSelection().getRangeAt(0);
    if (r === live) return "native";
    if (r?.startContainer === e) {
        const index = e.childNodes.findIndex((n) => n.dataset?.nodeType === "attachment");
        return r.startOffset === index + 1 ? "after" : r.startOffset === index ? "before" : "editor:" + r.startOffset;
    }
    return "moved";
};
const at = (container, offset) => { const r = document.createRange(); r.setStart(container, offset);
    r.collapse(true); return r; };
"""


def test_a_click_right_of_a_line_ending_chip_lands_after_it_even_when_gecko_says_before():
    result = _run_chip_dom_script(_SELECTION + _CLICK + r"""
const trailing = build([{ type: "text", node_id: "t1", text: "before " },
    { type: "attachment", node_id: "a1", attachment_id: "ref" }]);
const endOfText = () => at(textIn(trailing.e, "t1"), 7);
const out = {};
out.geckoLive = click(trailing.e, { live: endOfText() });
out.geckoEditorIndex = click(trailing.e, { live: at(trailing.e, 1) });
out.pointApiOnly = click(trailing.e, { live: null, point: endOfText() });
out.positionApi = click(trailing.e, { live: endOfText(), positionApi: true });
out.offLine = click(trailing.e, { live: endOfText(), y: 0 });       // above the chip's line
out.leftOfChipEdge = click(trailing.e, { live: endOfText(), x: 25 });
out.midText = click(trailing.e, { live: at(textIn(trailing.e, "t1"), 3) });

const middle = build([{ type: "text", node_id: "t1", text: "before " },
    { type: "attachment", node_id: "a1", attachment_id: "ref" },
    { type: "text", node_id: "t2", text: " after" }]);
out.lineEndChip = click(middle.e, { live: at(textIn(middle.e, "t1"), 7) });
out.textAfterChip = click(middle.e, { live: at(textIn(middle.e, "t2"), 2) });

// A bare text node the browser left between the text and the chip is
// authored text: the caret before it is not "immediately before" the chip.
const bare = build([{ type: "text", node_id: "t1", text: "before" },
    { type: "attachment", node_id: "a1", attachment_id: "ref" }]);
const stray = document.createTextNode(" typed");
bare.e.insertBefore(stray, bare.chipEl);
out.bareTextBetween = click(bare.e, { live: at(textIn(bare.e, "t1"), 6) });
out.endOfBareText = click(bare.e, { live: at(stray, 6) });
out.startOfBareText = click(bare.e, { live: at(stray, 0) });

// Gecko reports the start of a run of chips for a click right of its last.
const run = mod.createPromptDocumentEditor({ document: { nodes: [
    { type: "text", node_id: "t1", text: "before " },
    { type: "attachment", node_id: "a1", attachment_id: "ref" },
    { type: "attachment", node_id: "a2", attachment_id: "ref2" }] },
    attachments: [{ attachment_id: "ref", kind: "reference", source: {}, config: {} },
        { attachment_id: "ref2", kind: "reference", source: {}, config: {} }] });
const [chipA, chipB] = run.childNodes.filter((n) => n.dataset?.nodeType === "attachment");
chipA.getClientRects = () => [rect]; chipA.getBoundingClientRect = () => rect;
const rectB = { left: 32, right: 60, top: 5, bottom: 20, width: 28, height: 15 };
chipB.getClientRects = () => [rectB]; chipB.getBoundingClientRect = () => rectB;
const runClick = (x) => {
    const live = at(textIn(run, "t1"), 7);
    document.caretRangeFromPoint = () => live;
    active = live;
    run._handlers.click[0]({ target: run, button: 0, detail: 1, clientX: x, clientY: 10, isComposing: false });
    const r = getSelection().getRangeAt(0);
    return r === live ? "native" : r.startContainer === run ? "after" + (r.startOffset - run.childNodes.indexOf(chipA)) : "moved";
};
out.rightOfRun = runClick(70);         // after B (offset 2 past A's index)
out.betweenChips = runClick(31);       // right of A, left of B: after A

// A zero-width placeholder is not authored.
const empty = build([{ type: "text", node_id: "t1", text: "" },
    { type: "attachment", node_id: "a1", attachment_id: "ref" }]);
out.zeroWidthBefore = click(empty.e, { live: at(textIn(empty.e, "t1"), 0) });
console.log(JSON.stringify(out));
""")
    assert result == {
        "geckoLive": "after", "geckoEditorIndex": "after", "pointApiOnly": "after",
        "positionApi": "after", "offLine": "native", "leftOfChipEdge": "native",
        "midText": "native", "lineEndChip": "after", "textAfterChip": "native",
        "bareTextBetween": "native", "endOfBareText": "after", "startOfBareText": "native",
        "rightOfRun": "after2", "betweenChips": "after1", "zeroWidthBefore": "after",
    }


# --- A2: a drag that starts on a chip ---------------------------------------

def test_a_drag_from_a_chip_ends_with_a_collapsed_caret_at_the_release():
    result = _run_chip_dom_script(_SELECTION + r"""
const listeners = {};
document.addEventListener = (type, fn) => { (listeners[type] ||= []).push(fn); };
document.removeEventListener = (type, fn) => {
    listeners[type] = (listeners[type] || []).filter((f) => f !== fn); };
const chipRect = { left: 10, right: 30, top: 5, bottom: 20, width: 20, height: 15 };
const build = (opts = {}) => {
    const e = mod.createPromptDocumentEditor({ document: { nodes: [
        { type: "text", node_id: "t1", text: "keep left " },
        { type: "attachment", node_id: "a1", attachment_id: "ref" },
        { type: "text", node_id: "t2", text: " keep right" }] },
        attachments: [{ attachment_id: "ref", kind: "reference", source: {}, config: {} }], ...opts });
    e.focus = function () { document.activeElement = this; };
    e.isConnected = true;
    e.getBoundingClientRect = () => ({ left: 0, right: 200, top: 0, bottom: 60, width: 200, height: 60 });
    const chipEl = e.childNodes.find((n) => n.dataset?.nodeType === "attachment");
    chipEl.getBoundingClientRect = () => chipRect;
    chipEl.getClientRects = () => [chipRect];
    return { e, chipEl };
};
const tail = (e) => e.childNodes.find((n) => n.dataset?.nodeId === "t2").childNodes.find((n) => n.nodeType === 3);
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
const gesture = async ({ release = { x: 100, y: 10 }, type = "pointerup", opts = {}, older = false } = {}) => {
    const { e, chipEl } = build(opts);
    if (older) {                                   // a range live before the press
        const r = document.createRange();
        r.setStart(e.childNodes[0].childNodes.find((n) => n.nodeType === 3), 0);
        r.setEnd(tail(e), 6);
        active = r;
    } else {
        active = null;
    }
    const landing = document.createRange();
    landing.setStart(tail(e), 4); landing.collapse(true);
    document.caretRangeFromPoint = () => landing;
    document.activeElement = chipEl;                 // the press focused the chip
    for (const h of e._handlers.pointerdown) h({ button: 0, pointerType: "mouse", target: chipEl,
        clientX: 20, clientY: 10, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false });
    const armed = (listeners.pointerup || []).length;
    for (const h of [...(listeners[type] || [])]) h({ type, clientX: release.x, clientY: release.y });
    await settle();
    const r = getSelection().getRangeAt(0);
    const index = e.childNodes.indexOf(chipEl);
    return { armed, left: (listeners.pointerup || []).length,
        focus: document.activeElement === e ? "editor" : "chip",
        caret: !r ? null : r === landing ? "release"
            : r.startContainer === e && r.startOffset === index + 1 ? "afterChip" : "other",
        collapsed: r ? r.collapsed : null };
};
const out = {};
out.inside = await gesture();
out.onChip = await gesture({ release: { x: 20, y: 10 } });
out.outside = await gesture({ release: { x: 500, y: 500 } });
out.cancel = await gesture({ type: "pointercancel" });
out.olderRange = await gesture({ older: true });
out.locked = await gesture({ opts: { disabled: true } });
console.log(JSON.stringify(out));
""")
    assert result["inside"] == {"armed": 1, "left": 0, "focus": "editor", "caret": "release", "collapsed": True}
    # Released on the chip: a click, which opens the chip; nothing moves here.
    assert result["onChip"]["focus"] == "chip" and result["onChip"]["caret"] is None
    assert result["outside"]["focus"] == "editor" and result["outside"]["caret"] == "afterChip"
    assert result["cancel"]["focus"] == "chip" and result["cancel"]["left"] == 0
    # A range live before the press is never handed to the editor.
    assert result["olderRange"]["caret"] == "release" and result["olderRange"]["collapsed"] is True
    assert result["locked"]["armed"] == 0 and result["locked"]["focus"] == "chip"


def test_a_chip_dialog_that_fails_is_logged_not_swallowed():
    """Audit 7: the focus repair attaches to the host's own promise, so a failing
    dialog must reach the console instead of vanishing."""
    result = _run(r"""
const warned = [];
console.warn = (...args) => warned.push(args.map(String).join(" "));
const e = mod.createPromptDocumentEditor({ document: { nodes: [
    { type: "text", node_id: "t1", text: "keep left " },
    { type: "attachment", node_id: "n1", attachment_id: "c1" }] },
    attachments: [custom("c1")], onChange: () => {},
    onActivateAttachment: async () => { throw new Error("dialog broke"); } });
e.isConnected = true;
key(e, chipOf(e), "Enter");
await new Promise((resolve) => setTimeout(resolve, 5));
console.log(JSON.stringify({ warned }));
""")
    assert len(result["warned"]) == 1
    assert "Chip settings failed" in result["warned"][0] and "dialog broke" in result["warned"][0]


def test_keys_aimed_at_a_chip_behind_an_open_chip_dialog_do_nothing():
    """Audit 1: the dialog does not take focus, so the chip that opened it is
    still focused under the backdrop. Typing, pasting or an arrow there must not
    move focus into the editor behind the modal, where the host's post-dialog
    commit would save what landed unseen; Delete and Enter do nothing either."""
    result = _run(r"""
const out = {};
let opened = 0;
const backdrop = document.createElement("div");
backdrop.dataset.sonderPromptContextModal = "1";
document.body.appendChild(backdrop);
for (const [label, k, extra] of [["letter", "x", {}], ["paste", "v", { ctrlKey: true }],
        ["reload", "F5", {}], ["copy", "c", { ctrlKey: true }], ["escape", "Escape", {}],
        ["arrow", "ArrowRight", {}], ["delete", "Delete", {}], ["enter", "Enter", {}]]) {
    const e = mod.createPromptDocumentEditor({ document: { nodes: [
        { type: "text", node_id: "t1", text: "keep left " },
        { type: "attachment", node_id: "n1", attachment_id: "c1" }] },
        attachments: [custom("c1")], onChange: () => {}, onActivateAttachment: () => { opened += 1; } });
    e.focus = function () { document.activeElement = this; };
    e.isConnected = true;
    const chipEl = chipOf(e);
    document.activeElement = chipEl;
    active = null;
    const r = key(e, chipEl, k, extra);
    out[label] = { ...r, focus: document.activeElement === e ? "editor" : "chip", ...summary(e) };
}
const e = middle();
const chipEl = chipOf(e);
document.activeElement = chipEl;
active = null;
const pasteEvent = clip({ "text/plain": "PASTED" });
pasteEvent.event.target = chipEl;
fire(e, "paste", pasteEvent.event);
out.pasteEvent = { focus: document.activeElement === e ? "editor" : "chip", ...summary(e) };
out.opened = opened;
backdrop.remove();
console.log(JSON.stringify(out));
""")
    for label in ("letter", "paste", "arrow", "delete", "enter"):
        row = result[label]
        assert row["handled"] is True and row["focus"] == "chip", label
        assert row["nodes"] == ["keep left ", "[c1]"] and row["atts"] == [["c1", "c1"]], label
    # The browser's own keys, and the dialog's Escape, are not swallowed.
    for label in ("reload", "copy", "escape"):
        row = result[label]
        assert row["handled"] is not True and row["focus"] == "chip", label
        assert row["nodes"] == ["keep left ", "[c1]"], label
    assert result["opened"] == 0
    assert result["pasteEvent"]["nodes"] == ["keep left ", "[c1]", " keep right"]
    assert result["pasteEvent"]["focus"] == "chip"


def test_a_chip_press_is_abandoned_by_another_press_or_another_pointer():
    """Audit 6: a release lost to an iframe or a window switch must not let the
    next unrelated click settle the old press and pull focus into the prompt."""
    result = _run_chip_dom_script(_SELECTION + r"""
const listeners = {};
document.addEventListener = (type, fn) => { (listeners[type] ||= []).push(fn); };
document.removeEventListener = (type, fn) => {
    listeners[type] = (listeners[type] || []).filter((f) => f !== fn); };
const e = mod.createPromptDocumentEditor({ document: { nodes: [
    { type: "text", node_id: "t1", text: "keep left " },
    { type: "attachment", node_id: "a1", attachment_id: "ref" }] },
    attachments: [{ attachment_id: "ref", kind: "reference", source: {}, config: {} }] });
e.focus = function () { document.activeElement = this; };
e.isConnected = true;
e.getBoundingClientRect = () => ({ left: 0, right: 200, top: 0, bottom: 60 });
const chipEl = e.childNodes.find((n) => n.dataset?.nodeType === "attachment");
const press = (pointerId) => {
    document.activeElement = chipEl;
    for (const h of e._handlers.pointerdown) h({ button: 0, pointerType: "mouse", pointerId, target: chipEl,
        clientX: 20, clientY: 10, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false });
};
const fireDoc = (type, event) => { for (const h of [...(listeners[type] || [])]) h({ type, ...event }); };
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
const out = {};
press(1);
fireDoc("pointerup", { pointerId: 2, clientX: 100, clientY: 10 });    // another pointer
await settle();
out.otherPointer = { focus: document.activeElement === e ? "editor" : "chip",
    stillArmed: (listeners.pointerup || []).length };
fireDoc("pointerdown", { pointerId: 1 });                               // a new, unrelated press
fireDoc("pointerup", { pointerId: 1, clientX: 100, clientY: 10 });
await settle();
out.abandoned = { focus: document.activeElement === e ? "editor" : "chip",
    left: (listeners.pointerup || []).length + (listeners.pointerdown || []).length };
console.log(JSON.stringify(out));
""")
    assert result["otherPointer"] == {"focus": "chip", "stillArmed": 1}
    assert result["abandoned"] == {"focus": "chip", "left": 0}
