"""Inline Shot markers render where they are placed.

A Shot that OPENS its section (a section chip, or an inline Shot with nothing
before it) marks the section start and may carry the section time. Every other
Shot renders `[Shot N]` at its anchor, takes the next number in reading order,
and never carries a time. Prompt Relay never carries shot markers.
"""

import itertools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import prompt_channel_templates as pct
from server import prompt_context as pc
from server import prompt_payload as pp
from server.timeline_state import PromptSection

import test_minimax_h3_format_coverage as cov
import test_prompt_tokens as token_fixtures


ROOT = Path(__file__).resolve().parents[1]
KEY = "integrated_multimodal_description"
TEMPLATE = "minimax_h3_base"


def shot(attachment_id, timestamp=False, **extra):
    return {"attachment_id": attachment_id, "kind": "shot",
            "config": {"timestamp": timestamp}, **extra}


_NODE_IDS = itertools.count()


def text(value, node_id=None):
    return {"type": "text", "node_id": node_id or f"t{next(_NODE_IDS)}",
            "text": value}


def anchor(attachment_id):
    return {"type": "attachment", "node_id": f"n-{attachment_id}",
            "attachment_id": attachment_id}


def section(start, end, nodes, attachments, prompt_id=None, key=KEY):
    value = {"start_frame": start, "end_frame": end, "channels": {key: ""},
             "channel_docs": {key: {"nodes": nodes}}, "attachments": attachments}
    if prompt_id:
        value["prompt_id"] = prompt_id
    return PromptSection.from_dict(value)


def compile_sections(sections, *, start=0, end=None, template=TEMPLATE, **kwargs):
    end = end if end is not None else max(value.end_frame for value in sections)
    compiled = pc.compile_prompt_context(
        sections=sections, window_start=start, window_end=end, delimiter=".",
        template=pct.get_channel_template(template), fps=24.0, **kwargs)
    assert compiled["errors"] == [], compiled["errors"]
    return compiled


def three_shots():
    return section(0, 120, [
        anchor("s1"), text(" wide on the street. "),
        anchor("s2"), text(" close on her face. "),
        anchor("s3"), text(" over the shoulder")], [
        shot("s1", True), shot("s2", True), shot("s3")], prompt_id="first")


def test_inline_shots_render_where_they_sit_numbered_in_reading_order():
    compiled = compile_sections([
        three_shots(),
        section(120, 240, [text("next section")], [shot("s4", True)],
                prompt_id="second")])
    assert compiled["prompt"] == (
        f"{KEY}: [Shot 1] At 00:00.000, wide on the street. [Shot 2] close on "
        "her face. [Shot 3] over the shoulder. [Shot 4] At 00:05.000, next section")
    assert compiled["ordinal_manifest"]["shots"] == {"s1": 1, "s2": 2, "s3": 3, "s4": 4}


def test_a_shot_after_text_never_carries_the_time_even_when_stored():
    compiled = compile_sections([three_shots()])
    assert compiled["prompt"].count("At ") == 1
    assert "[Shot 2] close" in compiled["prompt"]


def test_a_leading_inline_shot_compiles_exactly_like_a_section_chip():
    inline = compile_sections([section(0, 48, [anchor("s1"), text(" a dog walks")],
                                       [shot("s1", True)])])
    chip = compile_sections([section(0, 48, [text("a dog walks")], [shot("s1", True)])])
    assert inline["prompt"] == chip["prompt"] == f"{KEY}: [Shot 1] At 00:00.000, a dog walks"


def test_whitespace_and_marker_anchors_before_a_shot_do_not_count():
    """The preserved legacy "inline Time, then Shot" keeps its output."""
    time_anchor = {"attachment_id": "t1", "kind": "timestamp",
                   "config": {"standalone": True}}
    compiled = compile_sections([section(0, 48, [
        text("  	"), anchor("t1"), anchor("s1"), text(" text")],
        [time_anchor, shot("s1", True)])])
    assert compiled["prompt"] == f"{KEY}: [Shot 1] At 00:00.000, text"


def test_a_disabled_or_missing_chip_before_a_shot_does_not_count():
    disabled = {"attachment_id": "c1", "kind": "custom", "enabled": False,
                "config": {"text": "x"}}
    compiled = compile_sections([section(0, 48, [
        anchor("gone"), anchor("c1"), anchor("s1"), text(" text")],
        [disabled, shot("s1", True)])])
    assert compiled["prompt"] == f"{KEY}: [Shot 1] At 00:00.000, text"


def test_a_section_without_an_opener_continues_the_previous_shot():
    compiled = compile_sections([
        section(0, 48, [text("a")], [shot("s1")]),
        section(48, 96, [text("b "), anchor("s2"), text(" c")], [shot("s2", True)])])
    assert compiled["prompt"] == f"{KEY}: [Shot 1] a. b [Shot 2] c"


def test_a_shot_chip_opens_and_later_inline_shots_follow_it():
    compiled = compile_sections([section(0, 48, [
        text("a "), anchor("s2"), text(" b")], [shot("s1", True), shot("s2")])])
    assert compiled["prompt"] == f"{KEY}: [Shot 1] At 00:00.000, a [Shot 2] b"


def test_two_shot_chips_still_open_one_shot():
    compiled = compile_sections([section(0, 48, [text("a")],
                                         [shot("s1"), shot("s2", True)])])
    assert compiled["prompt"] == f"{KEY}: [Shot 1] At 00:00.000, a"
    assert compiled["ordinal_manifest"]["shots"] == {"s1": 1, "s2": 1}


def test_a_marker_touching_words_is_spaced_and_whitespace_is_kept():
    compiled = compile_sections([section(0, 48, [
        text("street."), anchor("s1"), text("close\n"), anchor("s2"), text("\nend")],
        [shot("s1"), shot("s2")])])
    assert compiled["prompt"] == f"{KEY}: street. [Shot 1] close\n[Shot 2]\nend"


def test_a_slice_renumbers_from_one():
    compiled = compile_sections([
        section(0, 48, [text("a")], [shot("s0", True)]),
        section(48, 96, [text("b "), anchor("s1"), text(" c")], [shot("s1")])],
        start=48, end=96)
    assert compiled["prompt"] == f"{KEY}: b [Shot 1] c"


def test_a_shot_only_section_no_longer_shifts_later_numbers():
    """It composes to nothing and is dropped, so it must not take a number."""
    compiled = compile_sections([
        section(0, 60, [text("a")], [shot("a1", True)]),
        section(60, 120, [], [shot("b1", True)]),
        section(120, 180, [text("c "), anchor("c2"), text(" d")],
                [shot("c1"), shot("c2")])])
    assert compiled["prompt"] == f"{KEY}: [Shot 1] At 00:00.000, a. [Shot 2] c [Shot 3] d"
    assert compiled["ordinal_manifest"]["shots"] == {"a1": 1, "c1": 2, "c2": 3}
    rows = [row for row in compiled["attachment_capability_projections"]
            if row["attachment_id"] == "b1"]
    assert [row["state"] for row in rows] == ["empty"]
    assert rows[0]["state_reason"] == pc.EMPTY_SECTION_SHOT_REASON


def test_resolve_shot_markers_counts_in_place_shots_and_honours_explicit_numbers():
    segments = [
        {"_opens_shot": True, "_inline_shot_count": 2, "start": 0},
        {"_opens_shot": True, "start": 24},
        {"_opens_shot": True, "_shot_number": 7, "start": 48},
        {"_opens_shot": True, "start": 72},
    ]
    assert pp.resolve_shot_markers(segments, 24.0) == [
        "[Shot 1]", "[Shot 4]", "[Shot 7]", "[Shot 8]"]


def test_segments_without_in_place_shots_keep_their_exact_shape():
    compiled = compile_sections([section(0, 48, [text("a")], [shot("s1", True)])])
    assert set(compiled["segments"][0]) == {
        "text", "channels", "prompt_id", "_opens_shot", "_shot_timestamp",
        "global_channel_exceptions", "section_start", "start", "end"}


def test_prompt_relay_never_carries_shot_markers():
    compiled = compile_sections([
        section(0, 48, [text("wide. "), anchor("s1"), text(" close.")],
                [shot("s1")], key="visual"),
        section(48, 96, [text("next")], [shot("s2", True)], key="visual")],
        template="standard")
    assert "[Shot 1] close." in compiled["prompt"]
    assert compiled["relay"]["local_prompts"] == "wide. close. | next"
    assert "[Shot" not in json.dumps(compiled["relay"])


def test_a_shot_outside_the_shot_channel_still_opens_its_section():
    compiled = compile_sections([PromptSection.from_dict({
        "start_frame": 0, "end_frame": 48,
        "channels": {KEY: "a", "audio_description": ""},
        "channel_docs": {
            KEY: {"nodes": [text("a")]},
            "audio_description": {"nodes": [text("rain "), anchor("s1")]}},
        "attachments": [shot("s1", True)]})])
    assert compiled["prompt"].startswith(f"{KEY}: [Shot 1] At 00:00.000, a")


def test_templates_without_a_shot_channel_compile_no_markers():
    template = pct.get_channel_template("sonder")
    template = {**template, "shot_marker_channel": ""}
    compiled = pc.compile_prompt_context(
        sections=[section(0, 48, [text("a "), anchor("s1"), text(" b")],
                          [shot("s1")], key="visual")],
        window_start=0, window_end=48, template=template, fps=24.0)
    assert "[Shot" not in compiled["prompt"]


def test_projection_rows_per_role():
    compiled = compile_sections([section(0, 48, [
        anchor("s1"), text(" a "), anchor("s2"), text(" b")],
        [shot("s1", True), shot("s2", True)])])
    rows = [(row["attachment_id"], row["state"], row["text"], row["rendered_at_anchor"])
            for row in compiled["attachment_capability_projections"]]
    # An in-place Shot is exactly one row, at its anchor: no strip pill beside it.
    assert [row for row in rows if row[0] == "s2"] == [("s2", "marker", "[Shot 2]", True)]
    # The opener's marker row carries the composed marker. (Its anchor also keeps
    # the generic capability row every anchored opener had before this change.)
    assert ("s1", "marker", "[Shot 1] At 00:00.000,", False) in rows
    assert compiled["attachment_previews"]["s1"] == "[Shot 1] At 00:00.000,"
    assert compiled["attachment_previews"]["s2"] == "[Shot 2]"
    assert compiled["attachment_channel_previews"]["s2"] == {KEY: "[Shot 2]"}


def test_shot_token_cites_an_in_place_shot_by_its_own_number():
    summary = token_fixtures._reference_attachment(
        "summary", {"summary": "Cut to @shot(shot-c)"})
    later = pc.normalize_attachment(shot("shot-c"))
    compiled = pc.compile_prompt_context(
        global_channels={},
        sections=[{
            "prompt_id": "only", "start_frame": 0, "end_frame": 24,
            "channels": {"detailed_description": "", "summary": "body",
                         "subject_definitions": "body"},
            "channel_docs": {"detailed_description": {"nodes": [
                text("open "), anchor("shot-c"), text(" close")]}},
            "attachments": [token_fixtures._shot("shot-a"), later, summary]}],
        window_start=0, window_end=24, fps=24, template="minimax_h3_ref",
        context=token_fixtures._reference_context())
    assert not [value for value in compiled["errors"]
                if value["code"] == "unresolved_prompt_token"]
    assert "Cut to [Shot 2]" in compiled["prompt"]
    assert "[Shot 1] open [Shot 2] close" in compiled["prompt"]


def test_appearance_lists_name_every_shot_a_section_reaches():
    """The continued shot and each in-place Shot the section opens."""
    entity = cov.ReferenceEntity(reference_id="e", name="Dog", members=[
        cov.ReferenceMember(member_id="m1", asset_id="img_a")])
    recipes = [cov.ReferenceLaneRecipe(lane_id="lp", media_kind="image",
                                       recipe=cov.PICTURE_RECIPE)]
    items = [cov.ReferenceItem(reference_item_id="ip", lane_index=0, start_frame=0,
                               end_frame=cov.WINDOW_END, members=[
                                   {"entity_id": "e", "member_id": "m1",
                                    "role": "identity"}])]
    units = [{"semantic_unit_id": "u", "name": "Dog", "order": 0,
              "definition": "the fluffy white Samoyed",
              "sources": [{"entity_id": "e", "member_id": "m1"}]}]
    resolved = cov._resolve(setup={"mode": "reference", "picture_lane_ids": ["lp"]},
                            entities=[entity], items=items, recipes=recipes,
                            units=units)
    sections = [
        PromptSection(0, 50, channels={"detailed_description": "It sits"},
                      attachments=[pc.shot_attachment()]),
        PromptSection.from_dict({
            "start_frame": 50, "end_frame": cov.WINDOW_END,
            "channels": {"detailed_description": ""},
            "channel_docs": {"detailed_description": {"nodes": [
                text("It runs. "), anchor("cut"), text(" It jumps.")]}},
            "attachments": [shot("cut"), cov._reference_chip(
                "c", {"semantic_unit_ids": ["u"]}, {"retention_detail": "coat"})]}),
    ]
    compiled = cov._compile(resolved, units, sections)
    assert "[Shot 1] It sits. It runs. [Shot 2] It jumps." in compiled["prompt"]
    assert "(appears in [Shot 1], [Shot 2])" in compiled["prompt"]


def test_shot_plan_and_its_browser_mirror_agree():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for shot plan parity")
    cases = [
        ([shot("a"), shot("b")], {KEY: {"nodes": [anchor("a"), text("x "), anchor("b")]}}),
        ([shot("a", True)], {KEY: {"nodes": [text(" ​﻿ \t"), anchor("a")]}}),
        ([shot("a"), {"attachment_id": "t", "kind": "timestamp"}],
         {KEY: {"nodes": [anchor("t"), anchor("a")]}}),
        ([shot("a"), {"attachment_id": "c", "kind": "custom", "enabled": False}],
         {KEY: {"nodes": [anchor("c"), anchor("ghost"), anchor("a")]}}),
        ([shot("a"), {"attachment_id": "r", "kind": "reference"}],
         {KEY: {"nodes": [anchor("r"), anchor("a")]}}),
        ([shot("a"), shot("b", True), shot("c", enabled=False)],
         {KEY: {"nodes": [anchor("c"), text("\x1c"), anchor("a")]}}),
        ([shot("a"), shot("b")], {"other": {"nodes": [text("x"), anchor("a")]}}),
        ([shot("a")], {KEY: {"nodes": [text("x"), anchor("a"), anchor("a")]}}),
    ]
    expected = [pc.shot_plan([pc.normalize_attachment(value) for value in attachments],
                             docs, KEY) for attachments, docs in cases]
    script = (
        f"const mod = await import({json.dumps((ROOT / 'web/js/prompt_shot_plan.js').as_uri())});\n"
        f"const cases = {json.dumps(cases)};\n"
        f"console.log(JSON.stringify({{whitespace: mod.SHOT_LEAD_WHITESPACE, plans: cases.map("
        f"([attachments, docs]) => mod.promptShotPlan(attachments, docs, {json.dumps(KEY)}))}}));\n")
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result["whitespace"] == pc.SHOT_LEAD_WHITESPACE
    assert result["plans"] == expected


def test_the_caret_check_agrees_with_inserting_a_shot_there():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for shot plan parity")
    attachments = [{"attachment_id": "r", "kind": "reference"},
                   {"attachment_id": "t", "kind": "timestamp"}]
    nodes = [text("  ", "w"), anchor("t"), text(" ab", "x"), anchor("r"), text("cd", "y")]
    carets = [None, {"start": {"node_id": "w", "offset": 2}},
              {"start": {"node_id": "x", "offset": 1}},
              {"start": {"node_id": "x", "offset": 2}},
              {"start": {"node_id": "n-t", "offset": 1}},
              {"start": {"node_id": "n-r", "offset": 0}},
              {"start": {"node_id": "n-r", "offset": 1}}]

    def inserted(caret):
        out = []
        for node_value in nodes:
            start = (caret or {}).get("start") or {}
            if node_value["node_id"] == start.get("node_id"):
                if node_value["type"] == "text":
                    offset = start["offset"]
                    out += [text(node_value["text"][:offset], "a1"), anchor("new"),
                            text(node_value["text"][offset:], "a2")]
                else:
                    out += ([node_value, anchor("new")] if start["offset"]
                            else [anchor("new"), node_value])
                return out + nodes[nodes.index(node_value) + 1:]
            out.append(node_value)
        return out + [anchor("new")]

    expected = []
    for caret in carets:
        plan = pc.shot_plan([pc.normalize_attachment(value)
                             for value in [*attachments, shot("new")]],
                            {KEY: {"nodes": inserted(caret)}}, KEY)
        expected.append("new" in plan["openers"])
    script = (
        f"const mod = await import({json.dumps((ROOT / 'web/js/prompt_shot_plan.js').as_uri())});\n"
        f"const doc = {{nodes: {json.dumps(nodes)}}};\n"
        f"console.log(JSON.stringify({json.dumps(carets)}.map((caret) =>"
        f" mod.promptShotOpensAtCaret(doc, caret, {json.dumps(attachments)}))));\n")
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == expected == [False, True, True, False, True, False, False]


def test_a_section_dropped_after_numbering_pins_the_later_opener():
    """Its other chips rendered nothing, so the final resolve dropped it after
    the numbering ran. Markers keep the numbers citations already use."""
    empty_custom = {"attachment_id": "x", "kind": "custom", "config": {"text": ""}}
    compiled = pc.compile_prompt_context(
        sections=[section(0, 60, [text("a")], [shot("a1")], prompt_id="a"),
                  section(60, 120, [], [shot("b1"), empty_custom], prompt_id="b"),
                  section(120, 180, [text("c")], [shot("c1")], prompt_id="c")],
        window_start=0, window_end=180, delimiter=".",
        template=pct.get_channel_template(TEMPLATE), fps=24.0)
    assert compiled["ordinal_manifest"]["shots"] == {"a1": 1, "b1": 2, "c1": 3}
    assert compiled["prompt"] == f"{KEY}: [Shot 1] a. [Shot 3] c"
    assert [segment.get("_shot_number") for segment in compiled["segments"]] == [None, 3]


def test_split_leaves_in_place_shots_on_the_left():
    import server.routes as routes
    from server.timeline_state import LaneConfig, Scene
    left = section(0, 240, [text("a "), anchor("s1"), text(" b")], [shot("s1")])
    scene = Scene(scene_id="scene-1", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = [left]
    right = routes._split_prompt_object(scene, left, 120)
    assert not any(value["kind"] == "shot" for value in right.attachments)
    assert not any(node.get("type") == "attachment"
                   for document in right.channel_docs.values()
                   for node in document.get("nodes", []))
    compiled = compile_sections([left, right])
    # The right half continues the left half's last shot.
    assert compiled["prompt"].startswith(f"{KEY}: a [Shot 1] b. ")
    assert "[Shot 2]" not in compiled["prompt"]


# --- Browser surfaces ------------------------------------------------------

def _chips(body):
    from test_prompt_context_corrections import _run_chip_dom_script
    return _run_chip_dom_script(body)


def _panel(body):
    from test_prompt_authoring_ux import _run_panel_script
    return _run_panel_script(body)


_CHIP_EDITOR = """
const doc = (nodes) => ({ nodes });
const editorFor = (nodes, attachments = []) => ({
    promptDocument: doc(nodes), promptAttachments: attachments,
    capturePromptSelection() { return null; },
    restorePromptSelection() { return true; },
    insertAttachment(value) { this.inserted = value; },
});
const shotItem = (items) => items.find((item) => item.label === "Insert at cursor")
    ?.submenu.find((item) => item.kind === "shot");
const dialog = () => document.body.children.at(-1);
const timeBox = () => dialog().querySelectorAll("input")
    .find((input) => input.type === "checkbox");
const save = () => dialog().querySelectorAll("button")
    .find((button) => button.textContent !== "Cancel")._handlers.click[0]();
"""


def test_insert_at_cursor_offers_shot_only_where_the_host_places_it():
    result = _chips(_CHIP_EDITOR + """
const editor = editorFor([{ type: "text", node_id: "a", text: "x" }]);
const offered = (placement) => Boolean(shotItem(mod.createPromptContextMenuItems({
    editor, bookmark: null, shotPlacementAt: placement })));
console.log(JSON.stringify({
    noHost: offered(null),
    shotChannel: offered(() => ({ offered: true })),
    otherChannel: offered(() => ({ offered: false })),
    otherKindsStay: mod.createPromptContextMenuItems({ editor, bookmark: null,
        shotPlacementAt: () => ({ offered: false }) })
        .find((item) => item.label === "Insert at cursor").submenu
        .map((item) => item.kind),
}));
""")
    assert result == {"noHost": True, "shotChannel": True, "otherChannel": False,
                      "otherKindsStay": ["vocal_event", "prompt_link", "custom"]}


def test_a_shot_inserted_after_text_cannot_take_the_time():
    result = _chips(_CHIP_EDITOR + """
const run = async (nodes, bookmark) => {
    const editor = editorFor(nodes);
    const items = mod.createPromptContextMenuItems({ editor, bookmark,
        onCreate: (attachment) => mod.configurePromptAttachment(attachment, {}) });
    const pending = shotItem(items).action();
    const box = timeBox();
    const state = { disabled: box.disabled, checked: box.checked,
        notice: Boolean(dialog().querySelectorAll("div")
            .find((value) => value.dataset.sonderShotInPlace === "1")) };
    save();
    await pending;
    return { ...state, timestamp: editor.inserted?.config?.timestamp ?? null };
};
console.log(JSON.stringify({
    atStart: await run([{ type: "text", node_id: "a", text: "  " }],
        { start: { node_id: "a", offset: 2 } }),
    afterText: await run([{ type: "text", node_id: "a", text: "wide. " }],
        { start: { node_id: "a", offset: 6 } }),
}));
""")
    assert result["atStart"] == {"disabled": False, "checked": False,
                                 "notice": False, "timestamp": False}
    assert result["afterText"] == {"disabled": True, "checked": False,
                                   "notice": True, "timestamp": False}


def test_a_disabled_time_box_never_erases_a_stored_time():
    result = _chips(_CHIP_EDITOR + """
const stored = { attachment_id: "s2", kind: "shot", config: { timestamp: true } };
const pending = mod.configurePromptAttachment(stored, { shotOpensSection: false });
const notice = dialog().querySelectorAll("div")
    .find((value) => value.dataset.sonderShotInPlace === "1").textContent;
save();
const saved = await pending;
console.log(JSON.stringify({ timestamp: saved?.attachment?.config?.timestamp
    ?? saved?.config?.timestamp, kept: notice.includes("saved time is kept") }));
""")
    assert result == {"timestamp": True, "kept": True}


def test_dialog_role_and_label_read_the_live_documents_and_previews():
    result = _chips("""
const attachments = [{ attachment_id: "s1", kind: "shot" },
    { attachment_id: "s2", kind: "shot", config: { timestamp: true } }];
const documents = {
    body: { nodes: [{ type: "attachment", node_id: "n1", attachment_id: "s1" },
        { type: "text", node_id: "t", text: " wide " },
        { type: "attachment", node_id: "n2", attachment_id: "s2" }] } };
const role = (id) => mod.promptShotDialogRole({ attachment: attachments.find(
    (value) => value.attachment_id === id) || { attachment_id: id, kind: "shot" },
    documents, attachments, shotChannel: "body" });
console.log(JSON.stringify({
    leading: role("s1"), inPlace: role("s2"), notAnchored: role("s3") ?? null,
    labelResolved: mod.attachmentLabel(attachments[1], "[Shot 2]"),
    labelFallback: mod.attachmentLabel(attachments[1], ""),
}));
""")
    assert result == {"leading": True, "inPlace": False, "notAnchored": None,
                      "labelResolved": "[Shot 2]", "labelFallback": "Shot + time"}


def test_writing_placement_follows_the_real_block_and_channel_split():
    keys = ["summary", "detailed_description"]
    result = _panel("const keys = " + json.dumps(keys) + ";" + chr(10) + r"""
const nl = String.fromCharCode(10);
const draft = { nodes: [
    { type: "text", node_id: "t1", text: "summary:" + nl + "one" + nl
        + "detailed_description:" + nl + "wide. " },
    { type: "attachment", node_id: "n1", attachment_id: "s1" },
    { type: "text", node_id: "t2", text: " close" + nl + "---" + nl + "fresh" } ] };
const attachments = [{ attachment_id: "s1", kind: "shot" }];
const place = (options) => mod.writingShotPlacement({ document: draft, attachments,
    channelKeys: keys, defaultKey: "detailed_description",
    shotChannel: "detailed_description", ...options });
const text1 = draft.nodes[0].text;
console.log(JSON.stringify({
    existing: place({ attachmentId: "s1" }),
    inSummary: place({ bookmark: { start: { node_id: "t1", offset: 10 } } }),
    headerLineStart: place({ bookmark: { start: { node_id: "t1",
        offset: text1.indexOf("detailed_description") } } }),
    bodyStart: place({ bookmark: { start: { node_id: "t1",
        offset: text1.indexOf("wide") } } }),
    afterWide: place({ bookmark: { start: { node_id: "t1", offset: text1.length } } }),
    nextBlockStart: place({ bookmark: { start: { node_id: "t2",
        offset: draft.nodes[2].text.indexOf("fresh") } } }),
    appended: place({ bookmark: null }),
}));
""")
    assert result == {
        "existing": {"offered": True, "opensSection": False},
        "inSummary": {"offered": False, "opensSection": True},
        # The header line belongs to the channel above it until it is read.
        "headerLineStart": {"offered": False, "opensSection": True},
        "bodyStart": {"offered": True, "opensSection": True},
        "afterWide": {"offered": True, "opensSection": False},
        "nextBlockStart": {"offered": True, "opensSection": True},
        "appended": {"offered": True, "opensSection": False},
    }


def test_insert_at_cursor_hands_on_create_exactly_one_argument():
    """Hosts pass wrappers whose SECOND parameter is their own
    (`anchoredChannels`, `nodeId`); the Shot role must never arrive there."""
    result = _chips(_CHIP_EDITOR + """
const seen = [];
const editor = editorFor([{ type: "text", node_id: "a", text: "x" }]);
const items = mod.createPromptContextMenuItems({ editor, bookmark: null,
    onCreate: (...args) => { seen.push(args.length); return null; } });
for (const item of items.find((value) => value.label === "Insert at cursor").submenu) {
    await item.action();
}
console.log(JSON.stringify(seen));
""")
    assert result and set(result) == {1}


def test_a_marker_only_section_sends_prompt_relay_nothing():
    """A chip that renders nothing still counts as content, so the Shot after it
    is in place; the section's only output is the marker, which Relay drops."""
    silent = {"attachment_id": "x", "kind": "custom", "config": {"text": ""}}
    compiled = compile_sections([
        section(0, 48, [text("wide")], [shot("s1")], key="visual"),
        section(48, 96, [anchor("x"), anchor("s2")], [silent, shot("s2")], key="visual")],
        template="standard")
    assert "[Shot 2]" in compiled["prompt"]
    assert "[Shot" not in json.dumps(compiled["relay"])


def test_relay_keeps_words_apart_where_a_marker_sat():
    compiled = compile_sections([section(0, 48, [
        text("wide."), anchor("s1"), text("close.")], [shot("s1")], key="visual")],
        template="standard")
    assert "wide. [Shot 1] close." in compiled["prompt"]
    assert compiled["relay"]["local_prompts"] == "wide. close."


def test_authored_private_use_characters_survive_in_place_markers():
    authored = "glyph a "
    compiled = compile_sections([section(0, 48, [
        text(authored), anchor("s1"), text(" b")], [shot("s1")], key="visual")],
        template="standard")
    assert compiled["prompt"] == "glyph a [Shot 1] b"
    assert compiled["relay"]["local_prompts"] == "glyph a b"
