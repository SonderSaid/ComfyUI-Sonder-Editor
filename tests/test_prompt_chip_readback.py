"""The chip editor reads what the author wrote, and maps every position.

Two defects in `web/js/prompt_context_chips.js` (bug tracker, 2026-11-23):

1. `readDom` read each text span's `innerText`. Firefox 159 answered it from
   line boxes it had not yet updated after a keystroke in a wrapped field, so
   the model and the save lost the prompt's tail. The minimal DOM's
   `innerText` is a plain property, so a test that sets it to a truncated
   string stands in for that stale read: the reader must not consult it.
2. An ELEMENT position inside a text span (a handle mark, or the span with a
   child index) snapped to 0 or to the span's end, in `selectionBoundary` and
   in its copy in chip adjacency. A range ending at (mark, 1) deleted every
   word after the handle; Backspace at (mark, 0) deleted the chip before it.

Cases marked HEAD-FAIL fail on 0.9.2; the others are controls or structure.
"""

import json

from test_prompt_chip_enter import _SELECTION
from test_prompt_context_corrections import _run_chip_dom_script


_HELPERS = r"""
const ZW = String.fromCharCode(0x200b);
const SENTENCE = "keep @Test plus this whole suffix";
const make = (nodes, records = []) => mod.createPromptDocumentEditor({
    document: { nodes }, attachments: records });
const one = (text) => make([{ type: "text", node_id: "t", text }]);
const marks = (editor, id = "t") => spanOf(editor, id).childNodes.filter((child) =>
    child.dataset?.sonderPromptHandle === "1");
const keydown = (editor, key) => {
    let prevented = false;
    const claimed = editor._sonderOwnedKeydown({ key, ctrlKey: false, metaKey: false,
        shiftKey: false, target: editor, isComposing: false,
        preventDefault() { prevented = true; }, stopPropagation() {} });
    return { claimed: Boolean(claimed), prevented };
};
const paste = (editor, text) => {
    for (const handler of editor._handlers.paste) handler({
        preventDefault() {}, stopPropagation() {},
        clipboardData: { getData: (type) => type === "text/plain" ? text : "" } });
};
const fireInput = (editor) => { for (const handler of editor._handlers.input) handler({}); };
const caret = (editor) => editor.capturePromptSelection();
const element = (tag, children = [], dataset = {}) => {
    const value = document.createElement(tag);
    Object.assign(value.dataset, dataset);
    for (const child of children) value.appendChild(typeof child === "string"
        ? document.createTextNode(child) : child);
    return value;
};
const record = (id) => ({ attachment_id: id, kind: "reference", source: {}, config: {} });
"""


def _run(body):
    return _run_chip_dom_script(_SELECTION + _HELPERS + body)


# ── Readback ──────────────────────────────────────────────────────────────

_LONG = ("@Test is @Test. " + "This is some text that follows 'Test'.. " * 20)[:776] \
    + " this line has a total of 817 characters."
_TERMINAL = ("@Test is @Test and now we are adding @Test. "
             + "This is a bunch more text. . " * 13 + "@Test @Test @Test @Test @Test")


def test_typing_keeps_the_whole_prompt_when_the_layout_read_is_stale():
    """HEAD-FAIL: the Firefox 159 shape, a stale `innerText` after a keystroke."""
    result = _run(r"""
        const out = {};
        for (const [label, text, cut] of [["long", LONG, 16], ["terminal", TERMINAL, 6]]) {
            const editor = one(text);
            const span = spanOf(editor, "t");
            // The browser typed `X` at the end, after the last run.
            span.appendChild(document.createTextNode("X"));
            // What Firefox 159 reported for the span at that moment.
            span.innerText = (text + "X").slice(0, -cut);
            fireInput(editor);
            out[label] = editor.value;
        }
        console.log(JSON.stringify(out));
    """.replace("LONG", json.dumps(_LONG)).replace("TERMINAL", json.dumps(_TERMINAL)))
    assert result["long"] == _LONG + "X"
    assert result["terminal"] == _TERMINAL + "X"
    assert result["terminal"].endswith("@Test @TestX")


def test_structure_is_read_without_layout():
    result = _run(r"""
        const out = {};
        const read = (label, build) => {
            const editor = one("ab");
            const span = spanOf(editor, "t");
            span.childNodes.length = 0;
            for (const child of build()) span.appendChild(child);
            fireInput(editor);
            const painted = spanOf(editor, "t").childNodes.every((child) =>
                child.nodeType === 3 || child.dataset?.sonderPromptHandle === "1");
            out[label] = { value: editor.value, painted };
        };
        read("br", () => [document.createTextNode("ab"), element("br"), document.createTextNode("cd")]);
        read("block", () => [document.createTextNode("ab"), element("div", ["cd"]),
            document.createTextNode("ef")]);
        read("leadingBlock", () => [element("div", ["ab"]), document.createTextNode("cd")]);
        read("trailingBlock", () => [document.createTextNode("ab"), element("p", ["cd"])]);
        read("wrapper", () => [element("b", ["ab", element("font", ["cd"])])]);
        read("nonProse", () => [document.createTextNode("ab"), element("style", ["x{}"]),
            element("script", ["y()"]), Object.assign(element("span", ["hid"]), { hidden: true }),
            document.createTextNode("cd")]);
        read("chipMarkup", () => [document.createTextNode("ab"),
            element("span", ["@Hero", "✎", "×"], { nodeType: "attachment", attachmentId: "x" }),
            document.createTextNode("cd")]);
        read("decoration", () => [document.createTextNode("ab"),
            element("div", ["rendered contribution"], { sonderDecoration: "1" }),
            document.createTextNode("cd")]);
        read("table", () => [element("table", [element("tr", [element("td", ["a"]),
            element("td", ["b"])]), element("tr", [element("td", ["c"])])])]);
        read("zeroWidth", () => [document.createTextNode("a" + ZW + "b")]);
        console.log(JSON.stringify(out));
    """)
    assert result["br"]["value"] == "ab\ncd"
    assert result["block"]["value"] == "ab\ncd\nef"
    assert result["leadingBlock"]["value"] == "ab\ncd"
    assert result["trailingBlock"]["value"] == "ab\ncd"
    assert result["wrapper"]["value"] == "abcd"
    assert result["nonProse"]["value"] == "abcd"
    assert result["chipMarkup"]["value"] == "abcd"
    assert result["decoration"]["value"] == "abcd"
    assert result["table"]["value"] == "a\tb\nc"
    assert result["zeroWidth"]["value"] == "ab"
    # Every reshaped span is painted again at once, so caret positions are
    # resolved in the shape `render()` makes.
    assert all(case["painted"] for case in result.values()), result


def test_stray_elements_under_the_editor_read_only_their_children():
    """A padding `<br>` Firefox leaves under the editor reads as nothing."""
    result = _run(r"""
        const padded = one("");
        padded.appendChild(element("br"));
        fireInput(padded);
        const block = one("ab");
        block.appendChild(element("div", ["cd"]));
        fireInput(block);
        console.log(JSON.stringify({ padded: padded.value, block: block.value,
            strayLeft: [...padded.childNodes, ...block.childNodes].some((child) =>
                ["BR", "DIV"].includes(child.tagName) && !child.dataset?.nodeId
                && child.dataset?.sonderDecoration == null) }));
    """)
    assert result == {"padded": "", "block": "abcd", "strayLeft": False}


def test_a_trailing_placeholder_br_is_still_dropped():
    """Control: the Chromium placeholder rule survives the new reader."""
    result = _run(r"""
        const editor = one("She stops\nlast");
        const span = spanOf(editor, "t");
        span.childNodes.length = 0;
        span.appendChild(document.createTextNode("She stops\n"));
        span.appendChild(element("br"));
        fireInput(editor);
        console.log(JSON.stringify({ value: editor.value }));
    """)
    assert result == {"value": "She stops\n"}


# ── Positions ─────────────────────────────────────────────────────────────

def test_every_dom_shape_of_a_mention_boundary_maps_to_the_same_offset():
    """HEAD-FAIL for the element shapes; the text-node shapes are controls."""
    result = _run(r"""
        const editor = one(SENTENCE);
        const span = spanOf(editor, "t");
        const mark = marks(editor)[0];
        const at = (container, offset) => { place(container, offset);
            return caret(editor).start.offset; };
        console.log(JSON.stringify({
            children: span.childNodes.length,
            markEnd: at(mark, 1), spanAfterMark: at(span, 2),
            markStart: at(mark, 0), spanBeforeMark: at(span, 1),
            textEnd: at(mark.childNodes[0], 5), textStart: at(mark.childNodes[0], 0),
            spanStart: at(span, 0), spanEnd: at(span, span.childNodes.length) }));
    """)
    assert result["children"] == 3
    assert result["markEnd"] == 10
    assert result["spanAfterMark"] == 10
    assert result["markStart"] == 5
    assert result["spanBeforeMark"] == 5
    assert result["textEnd"] == 10
    assert result["textStart"] == 5
    assert result["spanStart"] == 0
    assert result["spanEnd"] == 33


def test_element_positions_before_textless_children():
    """HEAD-FAIL: (span, k) before an emptied mark or a `<br>`."""
    result = _run(r"""
        const out = {};
        for (const [label, middle] of [["emptyMark", () => element("span", [],
                { sonderPromptHandle: "1" })], ["br", () => element("br")]]) {
            const editor = one("abcd");
            const span = spanOf(editor, "t");
            span.childNodes.length = 0;
            span.appendChild(document.createTextNode("ab"));
            span.appendChild(middle());
            span.appendChild(document.createTextNode("cd"));
            place(span, 1);
            const before = caret(editor).start.offset;
            place(span, 2);
            const after = caret(editor).start.offset;
            out[label] = { before, after };
        }
        console.log(JSON.stringify(out));
    """)
    assert result["emptyMark"] == {"before": 2, "after": 2}
    assert result["br"] == {"before": 2, "after": 3}


def test_deleting_or_replacing_a_selected_mention_keeps_the_suffix():
    """HEAD-FAIL: the reproduced Delete, and Enter and paste over the same range."""
    result = _run(r"""
        const out = {};
        const select = (editor) => {
            place(firstText(spanOf(editor, "t")), 5, marks(editor)[0], 1);
            document.activeElement = editor;
        };
        for (const op of ["Delete", "Backspace", "Enter", "paste"]) {
            const editor = one(SENTENCE);
            select(editor);
            if (op === "Enter") input(editor, "insertParagraph");
            else if (op === "paste") paste(editor, "Z");
            else keydown(editor, op);
            undo(editor);
            const restored = editor.value;
            const selection = caret(editor);
            // Redo the edit to read its result.
            select(editor);
            if (op === "Enter") input(editor, "insertParagraph");
            else if (op === "paste") paste(editor, "Z");
            else keydown(editor, op);
            out[op] = { value: editor.value, restored,
                undoSelection: [selection.start.offset, selection.end.offset] };
        }
        console.log(JSON.stringify(out));
    """)
    suffix = " plus this whole suffix"
    assert result["Delete"]["value"] == "keep " + suffix
    assert result["Backspace"]["value"] == "keep " + suffix
    assert result["Enter"]["value"] == "keep \n" + suffix
    assert result["paste"]["value"] == "keep Z" + suffix
    for op in ("Delete", "Backspace", "Enter", "paste"):
        assert result[op]["restored"] == "keep @Test plus this whole suffix", op
        # Undo restores the selection the author made, not one to the end.
        assert result[op]["undoSelection"] == [5, 10], op


def test_a_caret_inside_a_mention_inserts_where_it_is():
    """HEAD-FAIL: a collapsed element caret took Enter, paste and attach to the span's end."""
    result = _run(r"""
        const out = {};
        for (const op of ["Enter", "paste", "attach"]) {
            const editor = make([{ type: "text", node_id: "t", text: SENTENCE }]);
            place(marks(editor)[0], 1);
            document.activeElement = editor;
            if (op === "Enter") input(editor, "insertParagraph");
            else if (op === "paste") paste(editor, "Z");
            else editor.insertAttachment(record("new"));
            out[op] = nodes(editor).map((node) => node.t ?? "|" + node.a + "|").join("");
        }
        console.log(JSON.stringify(out));
    """)
    assert result["Enter"] == "keep @Test\n plus this whole suffix"
    assert result["paste"] == "keep @TestZ plus this whole suffix"
    assert result["attach"] == "keep @Test|new| plus this whole suffix"


def test_backspace_and_delete_beside_a_mention_leave_the_neighbouring_chip():
    """HEAD-FAIL: the chip-adjacency copy of the snapping."""
    result = _run(r"""
        // A chip, then "see @Test": the caret at (mark, 0) is after "see ".
        const before = make([{ type: "attachment", node_id: "c", attachment_id: "ref" },
            { type: "text", node_id: "t", text: "see @Test" }], [record("ref")]);
        place(marks(before)[0], 0);
        document.activeElement = before;
        const back = keydown(before, "Backspace");
        // "@Test see", then a chip: the caret at (mark, 1) is mid-span.
        const after = make([{ type: "text", node_id: "t", text: "@Test see" },
            { type: "attachment", node_id: "c", attachment_id: "ref" }], [record("ref")]);
        place(marks(after)[0], 1);
        document.activeElement = after;
        const forward = keydown(after, "Delete");
        // Controls: the real boundaries still reach the chip.
        const edgeBefore = make([{ type: "attachment", node_id: "c", attachment_id: "ref" },
            { type: "text", node_id: "t", text: "@Test see" }], [record("ref")]);
        place(marks(edgeBefore)[0], 0);
        document.activeElement = edgeBefore;
        keydown(edgeBefore, "Backspace");
        console.log(JSON.stringify({
            back, backNodes: nodes(before), backRecords: before.promptAttachments.length,
            forward, forwardNodes: nodes(after), forwardRecords: after.promptAttachments.length,
            edgeNodes: nodes(edgeBefore) }));
    """)
    assert result["back"]["prevented"] is False
    assert result["backNodes"] == [{"a": "ref"}, {"t": "see @Test"}]
    assert result["backRecords"] == 1
    assert result["forward"]["prevented"] is False
    assert result["forwardNodes"] == [{"t": "@Test see"}, {"a": "ref"}]
    assert result["forwardRecords"] == 1
    # (mark, 0) at the very start of the span IS the span's start.
    assert result["edgeNodes"] == [{"t": "@Test see"}]


def test_typing_over_a_mention_range_records_the_selection_it_replaced():
    """HEAD-FAIL: the history entry of a native type-over named the span's end."""
    result = _run(r"""
        const editor = one(SENTENCE);
        place(firstText(spanOf(editor, "t")), 5, marks(editor)[0], 1);
        document.activeElement = editor;
        input(editor, "insertText", { data: "Z" });
        undo(editor);
        const replaced = caret(editor);
        console.log(JSON.stringify([replaced.start.offset, replaced.end.offset]));
    """)
    assert result == [5, 10]


def test_a_writing_aid_wraps_a_selection_ending_on_a_mention_element():
    """HEAD-FAIL: element endpoints degraded the wrap to an insert in front."""
    result = _run(r"""
        const editor = one(SENTENCE);
        const mark = marks(editor)[0];
        place(mark, 0, mark, 1);
        const elementEnds = mod.promptSelectionSnapshot(editor).text;
        place(firstText(spanOf(editor, "t")), 0, mark, 1);
        const mixed = mod.promptSelectionSnapshot(editor).text;
        console.log(JSON.stringify({ elementEnds, mixed }));
    """)
    assert result == {"elementEnds": "@Test", "mixed": "keep @Test"}


# ── Audit follow-ups (implementation audit, 2026-11-23) ──────────────────

def test_points_on_or_inside_skipped_content_map_where_they_sit():
    """A point the walk does not enter read as the span's end; Delete there took the next chip."""
    result = _run(r"""
        const out = {};
        const shaped = (middle) => {
            const editor = make([{ type: "text", node_id: "t", text: "abcd" },
                { type: "attachment", node_id: "c", attachment_id: "ref" }], [record("ref")]);
            const span = spanOf(editor, "t");
            span.childNodes.length = 0;
            const inner = middle();
            span.appendChild(document.createTextNode("ab"));
            span.appendChild(inner);
            span.appendChild(document.createTextNode("cd"));
            return { editor, span, inner };
        };
        const at = (label, middle, pick) => {
            const { editor, span, inner } = shaped(middle);
            place(...pick(span, inner));
            out[label] = caret(editor).start.offset;
        };
        at("insideStyle", () => element("style", ["x{}"]), (s, i) => [i.childNodes[0], 1]);
        at("insideDecoration", () => element("div", ["rendered"], { sonderDecoration: "1" }),
            (s, i) => [i.childNodes[0], 3]);
        at("onBr", () => element("br"), (s, i) => [i, 0]);
        at("pastLastChild", () => element("b", ["x"]), (s) => [s, 99]);
        // Delete with the caret inside a nested decoration keeps the chip after the span.
        const { editor, inner } = shaped(() => element("div", ["rendered"], { sonderDecoration: "1" }));
        place(inner.childNodes[0], 2);
        document.activeElement = editor;
        const key = keydown(editor, "Delete");
        out.deleteKeepsChip = { prevented: key.prevented, records: editor.promptAttachments.length };
        console.log(JSON.stringify(out));
    """)
    assert result["insideStyle"] == 2
    assert result["insideDecoration"] == 2
    assert result["onBr"] == 2
    assert result["pastLastChild"] == 5
    assert result["deleteKeepsChip"] == {"prevented": False, "records": 1}


def test_reader_skips_table_source_whitespace_and_until_found_and_breaks_new_blocks():
    result = _run(r"""
        const read = (build) => {
            const editor = one("ab");
            const span = spanOf(editor, "t");
            span.childNodes.length = 0;
            for (const child of build()) span.appendChild(child);
            fireInput(editor);
            return editor.value;
        };
        const hidden = element("span", ["FOUND"]);
        hidden.setAttribute("hidden", "until-found");
        console.log(JSON.stringify({
            table: read(() => [element("table", [document.createTextNode("\n  "),
                element("tr", [document.createTextNode("\n    "), element("td", ["a"]),
                    document.createTextNode("\n    "), element("td", ["b"])])])]),
            untilFound: read(() => [document.createTextNode("ab"), hidden, document.createTextNode("cd")]),
            section: read(() => [document.createTextNode("ab"), element("section", ["cd"])]),
        }));
    """)
    assert result == {"table": "a\tb", "untilFound": "abcd", "section": "ab\ncd"}


def test_a_caret_inside_a_stray_element_survives_the_render():
    """Pre-existing: a caret in a stray element under the editor was dropped by the render."""
    result = _run(r"""
        const editor = make([{ type: "attachment", node_id: "c", attachment_id: "ref" },
            { type: "text", node_id: "t", text: "end" }], [record("ref")]);
        // A drop or a browser wrapper left a stray element between the chip and the span.
        const stray = element("b", ["typed"]);
        editor.insertBefore(stray, spanOf(editor, "t"));
        place(stray.childNodes[0], 3);
        document.activeElement = editor;
        fireInput(editor);
        const inside = caret(editor);
        const insideNode = editor.promptDocument.nodes.find((n) => n.node_id === inside?.start?.node_id);
        // The field emptied to a padding <br> alone, the caret after it.
        const emptied = one("ab");
        emptied.childNodes.length = 0;
        emptied.appendChild(element("br"));
        place(emptied, 1);
        document.activeElement = emptied;
        fireInput(emptied);
        console.log(JSON.stringify({ order: nodes(editor),
            inside: inside && { text: insideNode?.text, offset: inside.start.offset },
            emptied: { value: emptied.value, caret: Boolean(caret(emptied)) } }));
    """)
    assert result["order"] == [{"a": "ref"}, {"t": "typed"}, {"t": "end"}]
    assert result["inside"] == {"text": "typed", "offset": 3}
    assert result["emptied"] == {"value": "", "caret": True}
