"""Enter / Shift+Enter in the chip editor go through the model.

Chromium's native paragraph insertion restructures a DOM the editor rebuilds
from its model: the newline was lost inside text, the caret landed in front of
the break after a chip, and in front of a chip the chip was read back as
literal label text and its record deleted. These drive the real
`beforeinput` handler against the minimal DOM the other chip tests use.
"""

from test_prompt_context_corrections import _run_chip_dom_script


# Selection and Range primitives the minimal DOM does not carry. A range is a
# plain record; `collapsed` is derived so a test cannot set it inconsistently.
_SELECTION = r"""
let active = null;
const makeRange = () => ({
    startContainer: null, startOffset: 0, endContainer: null, endOffset: 0,
    setStart(c, o) { this.startContainer = c; this.startOffset = o;
        if (!this.endContainer) { this.endContainer = c; this.endOffset = o; } },
    setEnd(c, o) { this.endContainer = c; this.endOffset = o; },
    setStartAfter(n) { const p = n.parentElement;
        this.setStart(p, p.childNodes.indexOf(n) + 1); },
    setStartBefore(n) { const p = n.parentElement;
        this.setStart(p, p.childNodes.indexOf(n)); },
    collapse(toStart) { if (toStart !== false) { this.endContainer = this.startContainer;
        this.endOffset = this.startOffset; } },
    get collapsed() { return this.startContainer === this.endContainer
        && this.startOffset === this.endOffset; },
});
document.createRange = makeRange;
const selection = {
    get rangeCount() { return active ? 1 : 0; },
    getRangeAt() { return active; },
    removeAllRanges() { active = null; },
    addRange(range) { active = range; },
    get isCollapsed() { return !active || active.collapsed; },
};
globalThis.getSelection = () => selection;
const place = (startContainer, startOffset,
               endContainer = startContainer, endOffset = startOffset) => {
    const range = makeRange();
    range.setStart(startContainer, startOffset);
    range.setEnd(endContainer, endOffset);
    active = range;
};
const spanOf = (editor, nodeId) => editor.childNodes.find((child) =>
    child.dataset?.nodeId === nodeId);
const firstText = (span) => span.childNodes.find((child) => child.nodeType === 3);
const input = (editor, inputType, extra = {}) => {
    const event = { inputType, data: null, cancelable: true, isComposing: false,
        defaultPrevented: false, prevented: false, ...extra,
        preventDefault() { this.defaultPrevented = true; this.prevented = true; } };
    for (const handler of editor._handlers.beforeinput) handler(event);
    return event;
};
const undo = (editor) => editor._sonderOwnedKeydown({ key: "z", ctrlKey: true,
    metaKey: false, shiftKey: false, target: editor, isComposing: false,
    preventDefault() {}, stopPropagation() {} });
const nodes = (editor) => editor.promptDocument.nodes.map((node) =>
    node.type === "text" ? { t: node.text } : { a: node.attachment_id });
const chip = { attachment_id: "ref", kind: "reference", source: {}, config: {} };
const chip2 = { attachment_id: "ref2", kind: "reference", source: {}, config: {} };
"""


def _run(body):
    return _run_chip_dom_script(_SELECTION + body)


def test_every_line_break_input_type_inserts_one_model_newline_at_the_caret():
    result = _run(r"""
        const out = {};
        for (const [label, type, data] of [["paragraph", "insertParagraph", null],
                ["linebreak", "insertLineBreak", null],
                ["textLF", "insertText", "\n"], ["textCRLF", "insertText", "\r\n"]]) {
            const editor = mod.createPromptDocumentEditor({ document: { nodes: [
                { type: "text", node_id: "t", text: "hello world" }] } });
            place(firstText(spanOf(editor, "t")), 5);
            const event = input(editor, type, { data });
            const afterBreak = editor.value;
            const caret = editor.capturePromptSelection();
            undo(editor);
            out[label] = { prevented: event.prevented, afterBreak, caret,
                afterUndo: editor.value };
        }
        console.log(JSON.stringify(out));
    """)
    for label, value in result.items():
        assert value["prevented"] is True, label
        assert value["afterBreak"] == "hello\n world", label
        assert value["caret"]["start"] == {"node_id": "t", "offset": 6}, label
        # One history entry: a single Undo returns to the pre-break text.
        assert value["afterUndo"] == "hello world", label


def test_a_break_directly_before_a_chip_keeps_the_chip_and_its_record():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t0", text: "abc" },
            { type: "attachment", node_id: "c", attachment_id: "ref" },
            { type: "text", node_id: "t2", text: "def" }] }, attachments: [chip] });
        // Caret on the editor element, immediately before the chip: the shape a
        // section-end caret takes when the text before it ends at the chip.
        const chipIndex = editor.childNodes.indexOf(spanOf(editor, "c"));
        place(editor, chipIndex);
        const event = input(editor, "insertParagraph");
        console.log(JSON.stringify({ prevented: event.prevented, nodes: nodes(editor),
            records: editor.promptAttachments.map((value) => value.attachment_id) }));
    """)
    assert result["prevented"] is True
    assert result["records"] == ["ref"]
    # The break lands before the chip — as the end of the preceding text, or as
    # its own node directly in front of the chip — and the chip survives.
    joined = "".join(value.get("t", "|chip|") for value in result["nodes"])
    assert joined == "abc\n|chip|def"


def test_a_break_directly_after_a_chip_leaves_the_caret_after_the_break():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t0", text: "abc" },
            { type: "attachment", node_id: "c", attachment_id: "ref" },
            { type: "text", node_id: "t2", text: "def" }] }, attachments: [chip] });
        const chipIndex = editor.childNodes.indexOf(spanOf(editor, "c"));
        place(editor, chipIndex + 1);
        input(editor, "insertParagraph");
        console.log(JSON.stringify({ nodes: nodes(editor),
            caret: editor.capturePromptSelection(),
            records: editor.promptAttachments.map((value) => value.attachment_id) }));
    """)
    assert result["nodes"] == [{"t": "abc"}, {"a": "ref"}, {"t": "\ndef"}]
    assert result["caret"]["start"] == {"node_id": "t2", "offset": 1}
    assert result["records"] == ["ref"]


def test_a_caret_in_a_bare_text_node_between_adjacent_chips_breaks_at_the_caret():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t0", text: "a" },
            { type: "attachment", node_id: "c1", attachment_id: "ref" },
            { type: "attachment", node_id: "c2", attachment_id: "ref2" },
            { type: "text", node_id: "t3", text: "b" }] }, attachments: [chip, chip2] });
        // What Chromium leaves between two chips before the next `input` has
        // been read: a text node that is a direct child of the editor, which
        // no span id can name. (Reading it would re-render it away, because a
        // new model node changes the handle-paint signature.)
        const bare = document.createTextNode("xy");
        editor.insertBefore(bare, spanOf(editor, "c2"));
        bare.parentElement = editor;
        place(bare, 1);
        const unresolved = editor.capturePromptSelection();
        const event = input(editor, "insertParagraph");
        console.log(JSON.stringify({ unresolved, prevented: event.prevented,
            nodes: nodes(editor) }));
    """)
    # The precondition that made this case dangerous: no bookmark at all.
    assert result["unresolved"] is None
    assert result["prevented"] is True
    assert result["nodes"] == [{"t": "a"}, {"a": "ref"}, {"t": "x\ny"},
                               {"a": "ref2"}, {"t": "b"}]


def test_line_breaks_are_left_alone_when_cancelled_composing_or_disabled():
    result = _run(r"""
        const make = () => {
            const editor = mod.createPromptDocumentEditor({ document: { nodes: [
                { type: "text", node_id: "t", text: "hello" }] } });
            place(firstText(spanOf(editor, "t")), 2);
            return editor;
        };
        const cancelled = make();
        const cancelledEvent = input(cancelled, "insertParagraph",
            { defaultPrevented: true });
        const composing = make();
        const composingEvent = input(composing, "insertText",
            { data: "\n", isComposing: true });
        const disabled = make();
        disabled.disabled = true;
        const disabledEvent = input(disabled, "insertParagraph");
        const uncancelable = make();
        const uncancelableEvent = input(uncancelable, "insertParagraph",
            { cancelable: false });
        console.log(JSON.stringify({
            cancelled: [cancelledEvent.prevented, cancelled.value],
            composing: [composingEvent.prevented, composing.value],
            disabled: [disabledEvent.prevented, disabled.value],
            uncancelable: [uncancelableEvent.prevented, uncancelable.value],
        }));
    """)
    for label in ("cancelled", "composing", "disabled", "uncancelable"):
        assert result[label] == [False, "hello"], label


def test_an_unresolvable_caret_is_not_intercepted_and_never_appends_at_the_end():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "hello" }] } });
        // A caret outside the editor: nothing can name it.
        place(document.createTextNode("elsewhere"), 1);
        const event = input(editor, "insertParagraph");
        console.log(JSON.stringify({ prevented: event.prevented, value: editor.value }));
    """)
    assert result == {"prevented": False, "value": "hello"}


def test_a_trailing_break_paints_a_sentinel_the_model_never_sees():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "abc" }] } });
        place(firstText(spanOf(editor, "t")), 3);
        input(editor, "insertParagraph");
        const span = spanOf(editor, "t");
        const last = span.childNodes[span.childNodes.length - 1];
        console.log(JSON.stringify({
            sentinel: last.nodeValue, value: editor.value,
            documentText: editor.promptDocument.nodes[0].text,
            caretOnSentinel: active.startContainer === last && active.startOffset === 0,
            caret: editor.capturePromptSelection(),
        }));
    """)
    assert result["sentinel"] == "\u200b"
    assert result["value"] == "abc\n"
    assert result["documentText"] == "abc\n"
    assert result["caretOnSentinel"] is True
    assert result["caret"]["start"] == {"node_id": "t", "offset": 4}


def test_backspace_beside_the_sentinel_deletes_the_break_in_one_step():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "abc\n" }] } });
        const span = spanOf(editor, "t");
        const sentinel = span.childNodes[span.childNodes.length - 1];
        const out = {};
        for (const offset of [0, 1]) {
            editor.promptState = { document: { nodes: [
                { type: "text", node_id: "t", text: "abc\n" }] }, attachments: [] };
            const fresh = spanOf(editor, "t");
            place(fresh.childNodes[fresh.childNodes.length - 1], offset);
            let prevented = false;
            const claimed = editor._sonderOwnedKeydown({ key: "Backspace",
                ctrlKey: false, metaKey: false, shiftKey: false, target: editor,
                isComposing: false, preventDefault() { prevented = true; },
                stopPropagation() {} });
            const afterBackspace = editor.value;
            undo(editor);
            out[offset] = { claimed, prevented, afterBackspace, afterUndo: editor.value };
        }
        console.log(JSON.stringify({ sentinel: sentinel.nodeValue, out }));
    """)
    assert result["sentinel"] == "\u200b"
    for offset, value in result["out"].items():
        assert value["claimed"] is True, offset
        assert value["prevented"] is True, offset
        assert value["afterBackspace"] == "abc", offset
        assert value["afterUndo"] == "abc\n", offset


def test_a_selection_touching_a_chip_keeps_the_chip_and_its_record():
    result = _run(r"""
        const make = () => mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t0", text: "abc" },
            { type: "attachment", node_id: "c", attachment_id: "ref" },
            { type: "text", node_id: "t2", text: "def" }] }, attachments: [chip] });
        // Selection ending right before the chip ({chip, 0} end).
        const before = make();
        place(firstText(spanOf(before, "t0")), 1,
              before, before.childNodes.indexOf(spanOf(before, "c")));
        input(before, "insertParagraph");
        // Selection starting right after the chip ({chip, 1} start).
        const after = make();
        place(spanOf(after, "c"), 1, firstText(spanOf(after, "t2")), 2);
        input(after, "insertParagraph");
        // Paste shares the splice.
        const pasted = make();
        place(firstText(spanOf(pasted, "t0")), 1,
              pasted, pasted.childNodes.indexOf(spanOf(pasted, "c")));
        for (const handler of pasted._handlers.paste) handler({
            preventDefault() {}, stopPropagation() {},
            clipboardData: { getData: () => "Z" } });
        const view = (editor) => ({ nodes: nodes(editor),
            records: editor.promptAttachments.map((value) => value.attachment_id) });
        console.log(JSON.stringify({ before: view(before), after: view(after),
            pasted: view(pasted) }));
    """)
    assert result["before"]["records"] == ["ref"]
    assert "".join(value.get("t", "|chip|") for value in result["before"]["nodes"]) \
        == "a\n|chip|def"
    assert result["after"]["records"] == ["ref"]
    assert "".join(value.get("t", "|chip|") for value in result["after"]["nodes"]) \
        == "abc|chip|\nf"
    assert result["pasted"]["records"] == ["ref"]
    assert "".join(value.get("t", "|chip|") for value in result["pasted"]["nodes"]) \
        == "aZ|chip|def"


def test_only_the_last_node_carries_a_sentinel_in_a_rebuilt_writing_draft():
    # A Writing draft rebuilt from sections is made of `key:\n`, `\n\n` and
    # `\n---\n` nodes. A sentinel after each of them painted a blank line
    # before every block decoration and a dead keypress in the middle of
    # prose nobody had pressed Enter in.
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "h", text: "visual:\n" },
            { type: "text", node_id: "a", text: "A woman walks" },
            { type: "text", node_id: "sep", text: "\n---\n" },
            { type: "text", node_id: "b", text: "She stops\n" }] } });
        const sentinels = editor.childNodes.map((span) => (span.childNodes || [])
            .filter((child) => child.nodeType === 3 && child.nodeValue === ZW).length);
        console.log(JSON.stringify({ sentinels, value: editor.value }));
    """.replace("ZW", "String.fromCharCode(0x200b)"))
    assert result["sentinels"] == [0, 0, 0, 1]
    assert result["value"] == "visual:\nA woman walks\n---\nShe stops\n"


def test_a_native_edit_leaving_a_final_break_restores_the_sentinel():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "abc\nxyz" }] } });
        const text = firstText(spanOf(editor, "t"));
        // The browser deleting the last line's text in place.
        text.nodeValue = "abc\n";
        place(text, 4);
        for (const handler of editor._handlers.input) handler({});
        const span = spanOf(editor, "t");
        const last = span.childNodes[span.childNodes.length - 1];
        console.log(JSON.stringify({ value: editor.value,
            sentinel: last.nodeValue === String.fromCharCode(0x200b),
            caret: editor.capturePromptSelection() }));
    """)
    assert result["value"] == "abc\n"
    assert result["sentinel"] is True
    assert result["caret"]["start"] == {"node_id": "t", "offset": 4}


def test_reading_a_bare_text_node_keeps_the_caret_inside_it():
    # The render that absorbs a bare node used a bookmark that could not name
    # it, so the caret was dropped and the next keystroke landed elsewhere.
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t0", text: "a" },
            { type: "attachment", node_id: "c1", attachment_id: "ref" },
            { type: "attachment", node_id: "c2", attachment_id: "ref2" },
            { type: "text", node_id: "t3", text: "b" }] }, attachments: [chip, chip2] });
        document.activeElement = editor;
        const bare = document.createTextNode("xy");
        editor.insertBefore(bare, spanOf(editor, "c2"));
        bare.parentElement = editor;
        place(bare, 1);
        for (const handler of editor._handlers.input) handler({});
        const caret = editor.capturePromptSelection();
        const node = editor.promptDocument.nodes.find((value) =>
            value.node_id === caret?.start?.node_id);
        console.log(JSON.stringify({ nodes: nodes(editor), caretText: node?.text,
            offset: caret?.start?.offset }));
    """)
    assert result["nodes"] == [{"t": "a"}, {"a": "ref"}, {"t": "xy"},
                               {"a": "ref2"}, {"t": "b"}]
    assert result["caretText"] == "xy"
    assert result["offset"] == 1


def test_an_open_composition_is_never_intercepted():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "hello" }] } });
        place(firstText(spanOf(editor, "t")), 2);
        for (const handler of editor._handlers.compositionstart) handler({});
        // Some IMEs deliver the accepting Enter as a break inside insertText
        // without flagging the event itself as composing.
        const event = input(editor, "insertText", { data: "\n" });
        console.log(JSON.stringify({ prevented: event.prevented, value: editor.value }));
    """)
    assert result == {"prevented": False, "value": "hello"}


def test_edits_follow_the_live_editable_state_not_the_construction_option():
    result = _run(r"""
        const unlocked = mod.createPromptDocumentEditor({ disabled: true,
            document: { nodes: [{ type: "text", node_id: "t", text: "hello" }] } });
        unlocked.disabled = false;
        place(firstText(spanOf(unlocked, "t")), 2);
        const enter = input(unlocked, "insertParagraph");
        const attached = unlocked.insertAttachment(chip);
        const locked = mod.createPromptDocumentEditor({
            document: { nodes: [{ type: "text", node_id: "t", text: "hello" }] } });
        locked.disabled = true;
        place(firstText(spanOf(locked, "t")), 2);
        locked.insertText("zz");
        const lockedAttach = locked.insertAttachment(chip);
        console.log(JSON.stringify({ enter: enter.prevented, unlocked: unlocked.value,
            attached: Boolean(attached), locked: locked.value,
            lockedAttach: lockedAttach === null }));
    """)
    assert result == {"enter": True, "unlocked": "he\nllo", "attached": True,
                      "locked": "hello", "lockedAttach": True}


def test_delete_beside_the_final_sentinel_is_claimed_without_an_edit():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "abc\n" }] } });
        const span = spanOf(editor, "t");
        place(span.childNodes[span.childNodes.length - 1], 0);
        let prevented = false;
        const claimed = editor._sonderOwnedKeydown({ key: "Delete", ctrlKey: false,
            metaKey: false, shiftKey: false, target: editor, isComposing: false,
            preventDefault() { prevented = true; }, stopPropagation() {} });
        console.log(JSON.stringify({ claimed, prevented, value: editor.value }));
    """)
    assert result == {"claimed": True, "prevented": True, "value": "abc\n"}


def test_the_sentinel_never_reaches_the_model_or_the_clipboard():
    result = _run(r"""
        const ZW = String.fromCharCode(0x200b);
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "abc" }] } });
        place(firstText(spanOf(editor, "t")), 3);
        for (const handler of editor._handlers.paste) handler({
            preventDefault() {}, stopPropagation() {},
            clipboardData: { getData: () => "x\n" + ZW + "y" } });
        const pasted = editor.value;
        const copy = (selected) => {
            place(firstText(spanOf(editor, "t")), 0, firstText(spanOf(editor, "t")), 2);
            selection.toString = () => selected;
            let written = null;
            const event = { prevented: false, preventDefault() { this.prevented = true; },
                clipboardData: { setData: (type, value) => { written = [type, value]; } } };
            for (const handler of editor._handlers.copy) handler(event);
            return { prevented: event.prevented, written };
        };
        console.log(JSON.stringify({ pasted,
            withSentinel: copy("line\n" + ZW), plain: copy("line") }));
    """)
    assert result["pasted"] == "abcx\ny"
    assert result["withSentinel"] == {"prevented": True,
                                      "written": ["text/plain", "line\n"]}
    assert result["plain"] == {"prevented": False, "written": None}


def test_an_emptied_last_line_placeholder_is_not_read_as_a_second_newline():
    result = _run(r"""
        const editor = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "t", text: "She stops\nlast" }] } });
        const span = spanOf(editor, "t");
        // What Chromium leaves after the last line's text is deleted: the
        // break, then a placeholder <br> that innerText reads as another "\n".
        span.childNodes.length = 0;
        const text = document.createTextNode("She stops\n");
        span.appendChild(text);
        span.appendChild(document.createElement("br"));
        span.innerText = "She stops\n\n";
        place(text, 10);
        for (const handler of editor._handlers.input) handler({});
        // A real second break followed by the placeholder keeps the real one.
        const kept = mod.createPromptDocumentEditor({ document: { nodes: [
            { type: "text", node_id: "k", text: "a\n\nb" }] } });
        const keptSpan = spanOf(kept, "k");
        keptSpan.childNodes.length = 0;
        keptSpan.appendChild(document.createTextNode("a\n\n"));
        keptSpan.appendChild(document.createElement("br"));
        keptSpan.innerText = "a\n\n\n";
        for (const handler of kept._handlers.input) handler({});
        console.log(JSON.stringify({ value: editor.value, kept: kept.value }));
    """)
    assert result == {"value": "She stops\n", "kept": "a\n\n"}
