import copy

import pytest

from server import prompt_context
from server.timeline_state import PromptSection


def _event(subject_ids=(), *, voice_id="", event_type="dialogue",
           phrase="", delivery="", text="Line."):
    return prompt_context.normalize_attachment({
        "attachment_id": "event",
        "kind": "vocal_event",
        "provider_id": "minimax_h3_ref",
        "provider_version": "1",
        "source": {"subject_ids": list(subject_ids), "voice_id": voice_id},
        "config": {
            "event_type": event_type,
            "language": "English",
            "subject_phrase": phrase,
            "delivery": delivery,
            "text": text,
        },
    })


def _compile(event, *, profile="minimax_h3_ref@1", units=(),
             subject_ordinals=None, preceding=""):
    channel = ("integrated_multimodal_description"
               if profile == "minimax_h3_base@1" else "detailed_description")
    section = PromptSection(
        0, 10, attachments=[event], channel_docs={channel: {"nodes": [
            {"type": "text", "node_id": "text", "text": preceding},
            {"type": "attachment", "node_id": "anchor",
             "attachment_id": event["attachment_id"]},
        ]}})
    return prompt_context.compile_prompt_context(
        sections=[section], window_start=0, window_end=10, fps=24,
        template=("minimax_h3_base" if profile == "minimax_h3_base@1"
                  else "minimax_h3_ref"),
        profile=profile,
        context={
            "semantic_units": list(units),
            "ordinal_manifest": {"subjects": dict(subject_ordinals or {})},
            "setup_manifest": {},
        })


def _unit(unit_id="u", *, name="Anna", handle="anna", definition="Anna in red",
          sourced=True):
    return {
        "semantic_unit_id": unit_id,
        "kind": "subject",
        "name": name,
        "handle": handle,
        "definition": definition,
        "sources": ([{"entity_id": "entity", "member_id": f"member-{unit_id}"}]
                    if sourced else []),
    }


def _codes(compiled):
    return {row["code"] for row in compiled["errors"]}


def test_vocal_event_policy_is_closed_complete_and_round_trips():
    source = copy.deepcopy(prompt_context.BUILTIN_PROFILES["generic@1"])
    source["profile_id"] = "custom:vocal"
    source["builtin"] = False
    source["capabilities"]["vocal_event"]["event_policy"] = {
        "identity_prefix": "selected",
        "delivery": True,
        "voiceover_subject_override": False,
    }
    source["identity_kinds"] = [copy.deepcopy(prompt_context.MINIMAX_SUBJECT_KIND)]
    normalized = prompt_context.normalize_profile(source)
    assert prompt_context.profile_declaration_errors(normalized) == []
    expected = source["capabilities"]["vocal_event"]["event_policy"]
    assert prompt_context.profile_fork_seed(normalized)["capabilities"][
        "vocal_event"]["event_policy"] == expected
    assert prompt_context.resolved_profile_definition(normalized)["capabilities"][
        "vocal_event"]["event_policy"] == expected
    hash_input = {key: value for key, value in normalized.items()
                  if key != "content_hash"}
    assert normalized["content_hash"] == prompt_context.content_hash(hash_input)

    malformed = copy.deepcopy(normalized)
    malformed["capabilities"]["vocal_event"]["event_policy"] = {
        "identity_prefix": "selected", "delivery": True, "unknown": False}
    assert "invalid_vocal_event_policy" in {
        row["code"] for row in prompt_context.profile_declaration_errors(malformed)}

    no_speaker = copy.deepcopy(normalized)
    no_speaker["identity_kinds"][0]["speaks"] = False
    assert "invalid_vocal_event_policy" in {
        row["code"] for row in prompt_context.profile_declaration_errors(no_speaker)}


def test_absent_vocal_event_policy_preserves_explicit_behavior():
    assert prompt_context.effective_vocal_event_policy({}) == {
        "identity_prefix": "explicit",
        "delivery": False,
        "voiceover_subject_override": False,
    }


def test_selected_identity_uses_full_reference_label_and_base_definition():
    unit = _unit()
    full = _compile(_event(["u"]), units=[unit], subject_ordinals={"u": 1})
    assert "<Subject 1> (S1) says:" in full["prompt"]

    base_event = _event(["u"])
    base_event["provider_id"] = "minimax_h3_base"
    base = _compile(base_event, profile="minimax_h3_base@1", units=[unit])
    assert "Anna in red (S1) says:" in base["prompt"]

    assetless = _compile(_event(["u"]), units=[_unit(sourced=False)])
    assert "Anna in red (S1) says:" in assetless["prompt"]


def test_full_reference_selected_identity_blocks_when_not_staged():
    compiled = _compile(_event(["u"]), units=[_unit()])
    assert "vocal_identity_not_applicable" in _codes(compiled)


def test_custom_selected_policy_blocks_a_sourced_identity_without_a_label():
    profile = copy.deepcopy(prompt_context.BUILTIN_PROFILES["generic@1"])
    profile["profile_id"] = "custom_vocal"
    profile["builtin"] = False
    profile["identity_kinds"] = [copy.deepcopy(prompt_context.MINIMAX_SUBJECT_KIND)]
    profile["capabilities"]["vocal_event"]["event_policy"] = {
        "identity_prefix": "selected",
        "delivery": False,
        "voiceover_subject_override": False,
    }
    profile = prompt_context.normalize_profile(profile)
    event = _event(["u"])
    event["provider_id"] = "custom_vocal"
    section = PromptSection(0, 10, attachments=[event], channel_docs={
        "visual": {"nodes": [{"type": "attachment", "node_id": "a",
                                "attachment_id": "event"}]}})
    compiled = prompt_context.compile_prompt_context(
        sections=[section], window_start=0, window_end=10, fps=24,
        template="standard", profile=profile,
        context={"semantic_units": [_unit()]})
    assert "vocal_identity_not_applicable" in _codes(compiled)


def test_explicit_phrase_wins_and_exact_adjacent_handle_dedupes():
    unit = _unit()
    explicit = _compile(
        _event(["u"], phrase="The woman on the balcony"),
        units=[unit], subject_ordinals={"u": 1})
    assert "The woman on the balcony (S1) says:" in explicit["prompt"]
    assert explicit["prompt"].count("<Subject 1>") == 0

    adjacent = _compile(
        _event(["u"]), units=[unit], subject_ordinals={"u": 1},
        preceding="@anna")
    assert adjacent["prompt"].count("<Subject 1>") == 1
    assert "<Subject 1> (S1) says:" in adjacent["prompt"]

    punctuated = _compile(
        _event(["u"]), units=[unit], subject_ordinals={"u": 1},
        preceding="@anna,")
    assert punctuated["prompt"].count("<Subject 1>") == 2

    uppercase = _compile(
        _event(["u"]), units=[unit], subject_ordinals={"u": 1},
        preceding="@ANNA")
    assert uppercase["prompt"].count("<Subject 1>") == 1
    assert "<Subject 1> (S1) says:" in uppercase["prompt"]

    email = _compile(
        _event(["u"]), units=[unit], subject_ordinals={"u": 1},
        preceding="email@anna")
    assert "email@anna<Subject 1> (S1) says:" in email["prompt"]


@pytest.mark.parametrize("event_type,clause", [
    ("dialogue", "says with an authoritative tone:"),
    ("group_speech", "say together with an authoritative tone:"),
    ("singing", "sings with an authoritative tone:"),
    ("narration", "narrates with an authoritative tone:"),
    ("voiceover", "says in an off-screen voiceover with an authoritative tone:"),
])
def test_delivery_renders_outside_dialogue_markup_for_every_grammar(
        event_type, clause):
    units = [_unit("u", sourced=False)]
    subjects = ["u"]
    if event_type == "group_speech":
        units.append(_unit("v", name="Ben", handle="ben",
                           definition="Ben in blue", sourced=False))
        subjects.append("v")
    compiled = _compile(
        _event(subjects, event_type=event_type,
               delivery="  with an   authoritative\n tone  "),
        units=units)
    assert clause in compiled["prompt"]
    assert f"{clause} <d>[English]" in compiled["prompt"]


def test_delivery_limit_and_unsupported_delivery_are_blocking_without_rewrite():
    unit = _unit(sourced=False)
    too_long = "😀" * (prompt_context.MAX_VOCAL_DELIVERY_CODEPOINTS + 1)
    compiled = _compile(_event(["u"], delivery=too_long), units=[unit])
    assert "vocal_delivery_too_long" in _codes(compiled)

    event = _event(["u"], delivery="softly")
    event["provider_id"] = "generic"
    section = PromptSection(0, 10, attachments=[event], channel_docs={
        "visual": {"nodes": [
            {"type": "attachment", "node_id": "a", "attachment_id": "event"}]}})
    generic = prompt_context.compile_prompt_context(
        sections=[section], window_start=0, window_end=10, fps=24,
        template="standard", profile="generic@1",
        context={"semantic_units": [unit]})
    assert "unsupported_vocal_delivery" in _codes(generic)
    assert "softly" not in generic["prompt"]
    assert event["config"]["delivery"] == "softly"


def test_cardinality_voice_only_and_dual_binding_validation_are_separate():
    units = [_unit("u", sourced=False), _unit("v", sourced=False)]
    ordinary_many = _compile(_event(["u", "v"]), units=units)
    assert "invalid_vocal_speaker_cardinality" in _codes(ordinary_many)

    group_one = _compile(
        _event(["u"], event_type="group_speech"), units=units)
    assert "invalid_vocal_speaker_cardinality" in _codes(group_one)

    duplicate_group = _compile(
        _event(["u", "u"], event_type="group_speech"), units=units)
    assert "invalid_vocal_speaker_cardinality" in _codes(duplicate_group)

    voice_only = _compile(_event(voice_id="stable-narrator"), units=[])
    assert "broken_vocal_identity" not in _codes(voice_only)
    assert "missing_vocal_binding" not in _codes(voice_only)

    dual = _compile(
        _event(["u"], voice_id="preserved-but-ignored"), units=units)
    assert "broken_vocal_identity" not in _codes(dual)
    assert "preserved-but-ignored" not in dual["managed_speaker_subject_ids"]


def test_vocal_events_refuse_transitive_prompt_link_export():
    unit = _unit()
    event = _event(["u"])
    event["link_exportable"] = True
    source = PromptSection(0, 10, attachments=[event], channel_docs={
        "detailed_description": {"nodes": [
            {"type": "text", "node_id": "t", "text": "@anna"},
            {"type": "attachment", "node_id": "a",
             "attachment_id": "event"}]}})
    link = prompt_context.normalize_attachment({
        "attachment_id": "link",
        "kind": "prompt_link",
        "source": {"prompt_id": source.prompt_id,
                   "channel_key": "detailed_description"},
    })
    consumer = PromptSection(10, 20, attachments=[link], channel_docs={
        "detailed_description": {"nodes": [
            {"type": "attachment", "node_id": "link-anchor",
             "attachment_id": "link"}]}})
    compiled = prompt_context.compile_prompt_context(
        sections=[source, consumer], window_start=10, window_end=20, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={"semantic_units": [unit],
                 "ordinal_manifest": {"subjects": {"u": 1}},
                 "setup_manifest": {}})
    assert "vocal_event_not_link_exportable" in _codes(compiled)
    assert "<Subject 1>says" not in compiled["prompt"]


def test_h3_compound_order_is_ascending_while_generic_is_authored():
    h3 = prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]
    generic = prompt_context.BUILTIN_PROFILES["generic@1"]
    assert h3["speaker_policy"]["compound_order"] == "ascending"
    assert generic["speaker_policy"]["compound_order"] == "authored"
    event = _event(["u", "v"], event_type="group_speech",
                   phrase="The pair")
    assert "(S1,S2)" in prompt_context._render_vocal_event(
        event, [2, 1], h3["speaker_policy"])
    assert "(S2,S1)" in prompt_context._render_vocal_event(
        event, [2, 1], generic["speaker_policy"])
