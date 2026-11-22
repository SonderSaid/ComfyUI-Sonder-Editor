""""Use server's version" on a document also settles the chips that text decided.

Bug tracker High (2026-11-22): resolving a contested document kept the draft's
chip records whose only place was in the discarded text (case A, refused by
the server's anchor guard on the retry), and kept the draft's deletion of a
chip the server's text still places (case B, a silent dangling anchor).

What is real: the draft rows, the surface mount and the resolution are the
real `prompt_edit_intent.js`, and the author's edit is recorded through
`createPromptDraftMount().record` the way a surface records it. The refusal
that names the contested record comes from the real route
(`routes._apply_update_prompt_section` on a `PromptSection` the second writer
changed), and the retry is judged by the same route and its anchor guard.
What is not: the chip editor's pruning of a deleted chip's record is given
as the surface's attachment list (the record is simply absent).
"""

import json

import pytest

import server.routes as routes
from server.timeline_state import PromptSection, Scene
from test_prompt_authoring_ux import ROOT, _method, _run_node, _source

INTENT = json.dumps((ROOT / "web/js/prompt_edit_intent.js").as_uri())


def _doc(*parts):
    nodes = []
    for index, part in enumerate(parts):
        if isinstance(part, str):
            nodes.append({"type": "text", "node_id": f"t{index}", "text": part})
        else:
            nodes.append({"type": "attachment", "node_id": f"a{index}",
                          "attachment_id": part["id"]})
    return {"schema": "prompt_document_v1", "nodes": nodes}


def _vocal(attachment_id):
    return {"attachment_id": attachment_id, "kind": "vocal_event", "config": {
        "event_type": "dialogue", "language": "en", "words": "Evening."}}


def _resolve(case):
    """Record the author's edit through a surface mount, print the save it
    makes, then (given the server's refusal) resolve it and report what the
    surfaces and the retry see."""
    return _run_node(f"""
const intent = await import({INTENT});
const {{ resolvePromptDraftRecord, promptDraftEdit, createPromptDraftMount,
    addPromptDraftListener, promptDraftRecordLabels }} = intent;
const c = {json.dumps(case)};
const drafts = new Map();
const key = "row";
const events = [];
addPromptDraftListener(drafts, (event) => events.push(structuredClone(event)));
const mount = createPromptDraftMount({{ drafts, key, global: Boolean(c.global),
    shown: {{ documents: c.shown_docs, attachments: c.shown_atts }},
    displayed: {{ documents: c.shown_docs, attachments: c.shown_atts }} }});
mount.record({{ documents: c.value_docs, attachments: c.value_atts }});
const row = drafts.get(key);
const initial = promptDraftEdit(row);
if (!c.conflict) {{ console.log(JSON.stringify({{ initial }})); }}
else {{
    row.error = "refused";
    row.conflict = c.conflict;
    const resolved = resolvePromptDraftRecord(drafts, key);
    // Each surface folds every resolution, in order, the way the panel's
    // listener does.
    let docs = structuredClone(c.value_docs), atts = structuredClone(c.value_atts);
    for (const event of events.filter((value) => value.resolution)) {{
        const repaint = mount.acknowledge(event, {{ documents: docs, attachments: atts }});
        Object.assign(docs, repaint?.documents || {{}});
        if (repaint?.attachments) atts = repaint.attachments;
    }}
    // What the surface now shows records nothing new.
    mount.record({{ documents: docs, attachments: atts }});
    const left = drafts.get(key);
    console.log(JSON.stringify({{ resolved,
        edit: left ? promptDraftEdit(left) : {{ documents: {{}}, attachments: {{}} }},
        labelsAfterRecord: left ? promptDraftRecordLabels(left) : [],
        resolutions: events.filter((value) => value.resolution).map((value) =>
            ({{ ...value.resolution, global: value.global }})),
        shownAttachments: atts }}));
}}
""")


def _identity(section):
    return {"prompt_id": section.prompt_id, "start_frame": section.start_frame,
            "end_frame": section.end_frame}


def _apply(section, edit):
    scene = Scene(scene_id="s", duration_frames=48, prompt_sections=[section])
    if edit["documents"] or edit["attachments"]:
        routes._apply_update_prompt_section(scene, 0, {"prompt_edit": edit}, _identity(section))
    return scene.prompt_sections[0]


def _refusal(section_factory, edit):
    """The route's own refusal of the author's first save."""
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        _apply(section_factory(), edit)
    assert raised.value.code == "prompt_edit_conflict"
    details = raised.value.details
    return {"record_kind": details["record_kind"], "record_key": details["record_key"],
            "current": details["current"]}


def _anchors(section):
    return {node["attachment_id"] for doc in section.channel_docs.values()
            for node in (doc or {}).get("nodes", []) if node.get("type") == "attachment"}


def test_a_chip_placed_only_in_the_discarded_text_leaves_with_it():
    """Case A: a Vocal event inserted in `visual`, speech typed too; a second
    writer changed `visual`. One "Use server's version" takes the server's
    `visual`, drops the chip, and the retry is accepted."""
    seen_visual, seen_speech, other = _doc("v0"), _doc("s0"), _doc("other writer")
    case = {"shown_docs": {"visual": seen_visual, "speech": seen_speech}, "shown_atts": [],
            "value_docs": {"visual": _doc("v0 ", {"id": "vx"}), "speech": _doc("s1")},
            "value_atts": [_vocal("vx")]}

    def stored_by_other_writer():
        return PromptSection(0, 48, channels={"visual": "other writer", "speech": "s0"},
                             channel_docs={"visual": other, "speech": seen_speech})

    conflict = _refusal(stored_by_other_writer, _resolve(case)["initial"])
    assert (conflict["record_kind"], conflict["record_key"]) == ("document", "visual")
    result = _resolve(dict(case, conflict=conflict))

    # The retry, judged first by the server as the second writer left it.
    stored = _apply(stored_by_other_writer(), result["edit"])
    assert [a["attachment_id"] for a in stored.attachments] == []
    assert stored.channels["speech"] == "s1"
    assert stored.channels["visual"] == "other writer"

    assert result["resolved"]["resolved"] == "document visual"
    assert result["resolved"]["remaining"] == ["document speech"]
    assert result["resolved"]["followers"] == ["attachment vx"]
    assert list(result["edit"]["documents"]) == ["speech"] and result["edit"]["attachments"] == {}
    # The document first, then the chip it decided; surfaces drop the chip.
    assert result["resolutions"][0]["recordKey"] == "visual"
    assert [(r["kind"], r["recordKey"], r["value"]) for r in result["resolutions"][1:]] == [
        ("attachments", "vx", None)]
    assert result["shownAttachments"] == []
    assert result["labelsAfterRecord"] == ["document speech"]


def test_a_chip_deleted_in_the_discarded_text_comes_back_with_the_server_text():
    """Case B: the author deleted a placed chip; the second writer changed other
    text of the same field. The server's text still places the chip, so the
    deletion leaves with the discarded text instead of leaving an anchor with
    no record."""
    placed = _vocal("y")
    seen = _doc("keep ", {"id": "y"}, " tail")
    other = _doc("keep ", {"id": "y"}, " tail OTHER")
    case = {"shown_docs": {"visual": seen}, "shown_atts": [placed],
            "value_docs": {"visual": _doc("keep  tail")}, "value_atts": []}

    def stored_by_other_writer():
        return PromptSection(0, 48, channels={"visual": "keep  tail OTHER"},
                             channel_docs={"visual": other}, attachments=[placed])

    conflict = _refusal(stored_by_other_writer, _resolve(case)["initial"])
    result = _resolve(dict(case, conflict=conflict))
    stored = _apply(stored_by_other_writer(), result["edit"])
    assert _anchors(stored) <= {a["attachment_id"] for a in stored.attachments}

    assert result["resolved"]["followers"] == ["attachment y"]
    assert result["edit"] == {"documents": {}, "attachments": {}}
    assert result["resolutions"][1]["recordKey"] == "y"
    assert result["resolutions"][1]["value"] == placed
    assert [a["attachment_id"] for a in result["shownAttachments"]] == ["y"]
    assert result["labelsAfterRecord"] == []


@pytest.mark.parametrize("case", ["scope_chip", "placed_elsewhere", "server_removed_too"])
def test_chips_the_discarded_text_did_not_decide_stay_in_the_draft(case):
    rec = _vocal("c")
    other = _doc("other writer")
    if case == "scope_chip":
        # Created through + Attach: never placed in any text.
        spec = {"shown_docs": {"visual": _doc("v0")}, "shown_atts": [],
                "value_docs": {"visual": _doc("v1")}, "value_atts": [dict(rec, kind="reference")]}
    elif case == "placed_elsewhere":
        spec = {"shown_docs": {"visual": _doc("v0"), "speech": _doc("s0")}, "shown_atts": [],
                "value_docs": {"visual": _doc("v0", {"id": "c"}), "speech": _doc("s0", {"id": "c"})},
                "value_atts": [rec]}
    else:
        # The author deleted it, and the second writer's text no longer has it.
        spec = {"shown_docs": {"visual": _doc("a ", {"id": "c"})}, "shown_atts": [rec],
                "value_docs": {"visual": _doc("a ")}, "value_atts": []}
    spec["conflict"] = {"record_kind": "document", "record_key": "visual", "current": other}
    result = _resolve(spec)
    assert result["resolved"]["followers"] == []
    assert "c" in result["edit"]["attachments"]


def test_an_attachment_conflict_still_resolves_only_the_attachment():
    rec = _vocal("c")
    changed = dict(rec, config=dict(rec["config"], words="Changed."))
    result = _resolve({
        "shown_docs": {"visual": _doc("a ", {"id": "c"})}, "shown_atts": [rec],
        "value_docs": {"visual": _doc("a ", {"id": "c"}, " more")},
        "value_atts": [dict(rec, config=dict(rec["config"], words="Mine."))],
        "conflict": {"record_kind": "attachment", "record_key": "c", "current": changed},
    })
    assert result["resolved"]["followers"] == []
    assert result["resolved"]["remaining"] == ["document visual"]
    assert [(r["kind"], r["recordKey"]) for r in result["resolutions"]] == [("attachments", "c")]


def test_global_scope_drops_a_reference_placed_only_in_the_discarded_text():
    """The global boxes have no anchor guard; the chip would silently become a
    scene-level Reference the author never placed there."""
    ref = {"attachment_id": "g", "kind": "reference", "config": {}}
    result = _resolve({"global": True,
                       "shown_docs": {"summary": _doc("g0")}, "shown_atts": [],
                       "value_docs": {"summary": _doc("g0 ", {"id": "g"})}, "value_atts": [ref],
                       "conflict": {"record_kind": "document", "record_key": "summary",
                                    "current": _doc("other")}})
    assert result["resolved"]["followers"] == ["attachment g"]
    assert result["edit"] == {"documents": {}, "attachments": {}}
    assert all(r["global"] for r in result["resolutions"])


def _park_host(script):
    widget = _source("web/js/editor_widget.js")
    methods = (_method(widget, "_takePromptIdentityCreateIntents", "_markPromptIdentityCreatesSending")
               + _method(widget, "_parkPromptIdentityCreatesForAttachments",
                         "_adoptPromptIdentitiesFromMutation"))
    return _run_node("""
class Host {
""" + methods + """
}
const host = new Host();
const intent = (id) => ({ unit: { semantic_unit_id: id, name: id } });
const vocal = (id, speaker) => ({ attachment_id: id, kind: "vocal_event", source: { subject_ids: [speaker] } });
const ids = (intents) => intents.map((value) => value.unit.semantic_unit_id);
""" + script)


def test_a_dropped_chip_parks_its_unsent_identity_create():
    """Implementation audit: the create rode only with the dropped chip, so it
    stops being pending (no paste refusal, no optimistic identity) and the
    draft's copy cannot put it back; a stored identity and one being sent are
    left alone."""
    result = _park_host("""
host._pendingPromptIdentityCreateIntents = new Map([
    ["u-drop", { attachmentId: "vx", intent: intent("u-drop") }],
    ["u-keep", { attachmentId: "other", intent: intent("u-keep") }],
    ["u-sending", { attachmentId: "vx2", intent: intent("u-sending"), sending: true }],
]);
host._promptSemanticUnits = [{ semantic_unit_id: "u-drop", handle: "" },
    { semantic_unit_id: "u-keep", handle: "" }, { semantic_unit_id: "u-sending", handle: "" },
    { semantic_unit_id: "stored", handle: "Stored" }];
const draft = { value: { attachments: {} }, identityCreates: [
    ["u-drop", { attachmentId: "vx", intent: intent("u-drop") }],
    ["u-keep", { attachmentId: "other", intent: intent("u-keep") }]] };
const parked = host._parkPromptIdentityCreatesForAttachments(["vx", "vx2"], draft);
// The section save's put-back from the draft's copy.
const store = host._pendingPromptIdentityCreateIntents;
for (const [id, pending] of draft.identityCreates) if (!store.has(id)) store.set(id, pending);
console.log(JSON.stringify({ parked, pending: [...store.keys()],
    units: host._promptSemanticUnits.map((unit) => unit.semantic_unit_id),
    copy: draft.identityCreates.map(([id]) => id),
    sentWithoutTheChip: ids(host._takePromptIdentityCreateIntents([vocal("other", "u-keep")])) }));
""")
    assert result["parked"] == ["u-drop"]
    assert result["pending"] == ["u-keep", "u-sending"]
    assert result["units"] == ["u-keep", "u-sending", "stored"]
    assert result["copy"] == ["u-keep"]
    assert result["sentWithoutTheChip"] == ["u-keep"]


def test_a_parked_create_comes_back_with_its_chip_or_another_chip_naming_it():
    """Implementation audit M2 (Ctrl+Z restores the chip) and M1 (another chip
    names the identity): a save that commits such a chip sends the create, so
    the server never stores a chip naming an identity that is never created."""
    result = _park_host("""
const run = (committed) => {
    host._pendingPromptIdentityCreateIntents = new Map([["u", { attachmentId: "vx", intent: intent("u") }]]);
    host._parkedPromptIdentityCreateIntents = new Map();
    host._parkPromptIdentityCreatesForAttachments(["vx"], { value: { attachments: {} }, identityCreates: [] });
    return ids(host._takePromptIdentityCreateIntents(committed));
};
console.log(JSON.stringify({ undo: run([vocal("vx", "u")]), other: run([vocal("r2", "u")]),
    unrelated: run([vocal("r3", "z")]) }));
""")
    assert result == {"undo": ["u"], "other": ["u"], "unrelated": []}


def test_a_chip_still_in_the_draft_takes_over_the_create():
    """Implementation audit M1: a chip of the same draft names the identity, so
    the create moves to it instead of parking."""
    result = _park_host("""
host._pendingPromptIdentityCreateIntents = new Map([["u", { attachmentId: "vx", intent: intent("u") }]]);
host._promptSemanticUnits = [{ semantic_unit_id: "u", handle: "" }];
const draft = { value: { attachments: { r2: vocal("r2", "u") } },
    identityCreates: [["u", { attachmentId: "vx", intent: intent("u") }]] };
const parked = host._parkPromptIdentityCreatesForAttachments(["vx"], draft);
console.log(JSON.stringify({ parked, owner: host._pendingPromptIdentityCreateIntents.get("u")?.attachmentId,
    copyOwner: draft.identityCreates[0][1].attachmentId,
    units: host._promptSemanticUnits.length,
    sent: ids(host._takePromptIdentityCreateIntents([draft.value.attachments.r2])) }));
""")
    assert result == {"parked": [], "owner": "r2", "copyOwner": "r2", "units": 1, "sent": ["u"]}


def test_the_recovery_bar_names_the_chips_and_parks_their_identity_creates():
    panel = _source("web/js/editor_prompt_panel.js")
    start = panel.index("const alsoChanged")
    bar = panel[start - 400:start + 2600]
    # The bar does not promise to keep a chip it drops, the button says it
    # resolves the chips too, is off while a save is in flight, and parks the
    # creates of the chips the resolution itself dropped.
    assert "promptResolutionFollowers(" in panel[start - 400:start]
    assert "!followers.includes(label)" in bar
    assert "`${target} and ${followers.join(\", \")}`" in bar
    assert "useServer.disabled = !conflict || Boolean(row.pending);" in bar
    assert "host._parkPromptIdentityCreatesForAttachments?.((resolved.settled || [])" in bar
    assert ".filter((follower) => follower.value == null)" in bar
