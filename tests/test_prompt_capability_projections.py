"""Per-capability prompt projection contract and terminal-state coverage."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import prompt_context

ROOT = Path(__file__).resolve().parents[1]


def _attachment(text="value", *, attachment_id="a", group_id=None,
                capability_id="custom", placement="section_prefix",
                channel="visual", enabled=True):
    return prompt_context.normalize_attachment({
        "attachment_id": attachment_id,
        "emission_group_id": group_id or attachment_id,
        "kind": "custom",
        "config": {"text": text},
        "capabilities": [{
            "capability_id": capability_id,
            "kind": "custom",
            "channel_key": channel,
            "placement": placement,
            "enabled": enabled,
        }],
    })


def _section(prompt_id, attachment, *, document=None, document_channel="visual",
             start=0, end=10):
    return {
        "prompt_id": prompt_id,
        "start_frame": start,
        "end_frame": end,
        "channels": {"visual": "body"},
        "channel_docs": ({document_channel: document} if document else {}),
        "attachments": [attachment],
    }


def _compile(sections, *, profile="generic@1", template="standard"):
    return prompt_context.compile_prompt_context(
        global_channels={}, sections=sections, window_start=0, window_end=100,
        fps=24.0, template=template, profile=profile)


def test_projection_contract_shape_scope_inline_and_region():
    attachment = _attachment(placement="inline")
    result = _compile([_section("one", attachment)])
    row = result["attachment_capability_projections"][0]
    assert set(row) == {
        "attachment_id", "emission_group_id", "attachment_kind",
        "capability_id", "capability_kind", "channel_key",
        "declared_placement", "effective_phase", "region", "origin",
        "order", "state", "state_reason", "text", "rendered_at_anchor",
    }
    assert (row["channel_key"], row["declared_placement"],
            row["effective_phase"], row["region"], row["state"]) == (
        "visual", "inline", "inline", "before", "emitted")
    assert row["state_reason"] == (
        "Section-scope chips have no caret anchor; this is placed after "
        "section-prefix contributions and before authored text.")
    assert row["rendered_at_anchor"] is False


def test_disabled_output_limit_and_invalid_route_are_closed_states():
    empty = _attachment("", attachment_id="empty")
    disabled = _attachment(attachment_id="disabled", enabled=False)
    oversized = _attachment("x" * (16 * 1024 + 1), attachment_id="oversized")
    invalid = _attachment(attachment_id="invalid", channel="missing")
    result = _compile([
        _section("empty", empty, start=0, end=10),
        _section("disabled", disabled, start=10, end=20),
        _section("oversized", oversized, start=20, end=30),
        _section("invalid", invalid, start=30, end=40),
    ])
    states = {row["attachment_id"]: row["state"]
              for row in result["attachment_capability_projections"]}
    assert states == {
        "empty": "empty",
        "disabled": "disabled",
        "oversized": "output_limit",
        "invalid": "invalid_route",
    }


_ANCHORED_ENUM_PROFILE = {
    "profile_id": "anchored-enum", "version": "1", "template_id": "sonder",
    "writing_aids": [],
    "capabilities": {"custom": {
        "placement": "inline", "formatter": "The camera {motion}.",
        "fields": {"motion": {"type": "enum",
                              "values": ["pushes in", "pulls out"]}},
    }},
}


def _compile_anchored_global_custom(*, exceptions, anchor_channels):
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "global-custom", "kind": "custom",
        "config": {"motion": "invalid"},
        "capabilities": [{"capability_id": "custom", "kind": "custom",
                          "placement": "inline"}],
    })
    documents = {
        channel: {"nodes": [{
            "type": "attachment", "node_id": f"anchor-{channel}",
            "attachment_id": "global-custom", "capability_id": "custom",
        }]}
        for channel in anchor_channels
    }
    return prompt_context.compile_prompt_context(
        global_documents=documents, global_attachments=[attachment],
        sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channels": {"visual": "body"},
            "global_channel_exceptions": list(exceptions),
        }],
        window_start=0, window_end=10, fps=24, template="sonder",
        profile=_ANCHORED_ENUM_PROFILE)


def test_anchored_global_applicability_uses_actual_anchor_route():
    excluded_anchor = _compile_anchored_global_custom(
        exceptions=["speech"], anchor_channels=["speech"])
    assert not any(row["code"] == "invalid_custom_capability_field"
                   for row in excluded_anchor["errors"])
    assert excluded_anchor["attachment_capability_projections"][0][
        "state"] == "not_inherited"

    inherited_anchor = _compile_anchored_global_custom(
        exceptions=["visual"], anchor_channels=["speech"])
    assert any(row["code"] == "invalid_custom_capability_field"
               for row in inherited_anchor["errors"])
    assert inherited_anchor["attachment_capability_projections"][0][
        "state"] == "emitted"


def test_multi_anchor_global_capability_is_effective_when_any_anchor_is_inherited():
    compiled = _compile_anchored_global_custom(
        exceptions=["speech"], anchor_channels=["visual", "speech"])
    assert any(row["code"] == "invalid_custom_capability_field"
               for row in compiled["errors"])
    states = {row["channel_key"]: row["state"]
              for row in compiled["attachment_capability_projections"]}
    assert states == {"visual": "emitted", "speech": "unresolved"}


def test_enabled_empty_section_vote_is_shared_by_projection_and_composition():
    global_attachment = _attachment("CAP", attachment_id="global")
    empty_section_attachment = _attachment("", attachment_id="empty")
    compiled = prompt_context.compile_prompt_context(
        global_documents={"visual": {"nodes": [
            {"type": "text", "node_id": "authored", "text": "AUTHORED "},
            {"type": "attachment", "node_id": "global-anchor",
             "attachment_id": "global", "capability_id": "custom"},
        ]}},
        global_attachments=[global_attachment],
        sections=[{
            "prompt_id": "empty-section", "start_frame": 0, "end_frame": 10,
            "attachments": [empty_section_attachment],
            "global_channel_exceptions": ["visual"],
        }],
        window_start=0, window_end=10, fps=24,
        template="sonder", profile="generic@1")

    global_projection = next(row for row in compiled[
        "attachment_capability_projections"] if row["attachment_id"] == "global")
    assert global_projection["state"] == "not_inherited"
    assert compiled["prompt"] == ""
    assert compiled["relay"]["global_prompt"] == ""


def test_multi_anchor_identity_preserves_deduplicated_row():
    attachment = _attachment(attachment_id="multi", placement="inline")
    document = {"nodes": [
        {"type": "attachment", "node_id": "anchor-a",
         "attachment_id": "multi", "capability_id": "custom"},
        {"type": "text", "node_id": "text", "text": " between "},
        {"type": "attachment", "node_id": "anchor-b",
         "attachment_id": "multi", "capability_id": "custom"},
    ]}
    result = _compile([_section("one", attachment, document=document)])
    rows = result["attachment_capability_projections"]
    assert len(rows) == 2
    assert [row["state"] for row in rows] == ["emitted", "deduplicated"]
    assert [row["order"] for row in rows] == [0, 1]
    assert all(row["rendered_at_anchor"] for row in rows)


def test_anchored_capability_routed_elsewhere_is_not_rendered_at_anchor():
    attachment = _attachment(attachment_id="rerouted", placement="inline",
                             channel="speech")
    document = {"nodes": [{
        "type": "attachment", "node_id": "anchor",
        "attachment_id": "rerouted", "capability_id": "custom",
    }]}
    result = _compile([_section("one", attachment, document=document)])
    row = result["attachment_capability_projections"][0]
    assert row["channel_key"] == "speech"
    assert row["rendered_at_anchor"] is False


def test_anchored_prompt_link_is_rendered_at_anchor():
    source = _section("source", _attachment("", attachment_id="source-empty"),
                      document={"nodes": [{"type": "text", "node_id": "source-text",
                                           "text": "earlier text"}]},
                      start=0, end=10)
    link = prompt_context.normalize_attachment({
        "attachment_id": "link", "kind": "prompt_link",
        "source": {"prompt_id": "source", "channel_key": "visual"},
    })
    document = {"nodes": [{
        "type": "attachment", "node_id": "link-anchor",
        "attachment_id": "link", "capability_id": "prompt_link",
    }]}
    result = _compile([source, _section("consumer", link, document=document,
                                       start=10, end=20)])
    row = next(value for value in result["attachment_capability_projections"]
               if value["attachment_id"] == "link")
    # The selected source already emits, so the Prompt Link is intentionally
    # empty; its projection still records that the anchor rendered the result.
    assert row["state"] == "empty"
    assert row["rendered_at_anchor"] is True


def test_group_dedupe_reports_linked_elsewhere_server_side():
    first = _attachment(attachment_id="first", group_id="group")
    second = _attachment(attachment_id="second", group_id="group")
    result = _compile([
        _section("one", first, start=0, end=10),
        _section("two", second, start=10, end=20),
    ])
    states = {row["attachment_id"]: row["state"]
              for row in result["attachment_capability_projections"]}
    assert states == {"first": "emitted", "second": "linked_elsewhere"}


def test_unresolved_overrides_empty_but_never_emitted():
    link = prompt_context.normalize_attachment({
        "attachment_id": "broken-link",
        "kind": "prompt_link",
        "source": {"prompt_id": "missing", "channel_key": "visual"},
    })
    document = {"nodes": [{
        "type": "attachment", "node_id": "link-anchor",
        "attachment_id": "broken-link", "capability_id": "prompt_link",
    }]}
    broken = _compile([_section("consumer", link, document=document)])
    assert broken["attachment_capability_projections"][0]["state"] == "unresolved"

    literal = _attachment("@subject(missing)", attachment_id="literal")
    emitted = _compile([_section("literal", literal)], profile={
        "profile_id": "declared-token", "version": "1", "name": "Declared token",
        "template_id": "standard", "capabilities": {}, "writing_aids": [],
        "identity_kinds": [prompt_context.MINIMAX_SUBJECT_KIND],
    })
    row = emitted["attachment_capability_projections"][0]
    assert row["state"] == "emitted"
    assert any(error["code"] == "unresolved_prompt_token"
               for error in emitted["errors"])


def test_shot_and_time_use_composer_markers_without_legacy_route_change():
    shot = prompt_context.normalize_attachment({
        "attachment_id": "shot", "kind": "shot", "config": {"timestamp": True}})
    time = prompt_context.normalize_attachment({
        "attachment_id": "time", "kind": "timestamp", "config": {"standalone": True}})
    result = _compile([{
        "prompt_id": "marked", "start_frame": 0, "end_frame": 24,
        "channels": {"visual": "body"}, "attachments": [shot, time],
    }])
    rows = {row["attachment_id"]: row
            for row in result["attachment_capability_projections"]}
    assert rows["shot"]["state"] == rows["time"]["state"] == "marker"
    assert rows["shot"]["text"] == "[Shot 1] At 00:00.000,"
    assert rows["time"]["text"] == "At 00:00.000,"
    assert result["prompt"].count("[Shot 1]") == 1
    assert "shot" not in result["attachment_channel_routes"]
    assert result["attachment_channel_routes"]["time"] == {
        "visual": "section_prefix"}


def test_composer_marker_state_ignores_inert_capability_suppression_flags():
    shot = prompt_context.normalize_attachment({
        "attachment_id": "shot", "kind": "shot",
        "capabilities": [{"capability_id": "shot", "kind": "shot",
                          "enabled": False}],
    })
    row = _compile([_section("one", shot)])["attachment_capability_projections"][0]
    assert row["state"] == "marker"
    assert row["state_reason"] == (
        "This marker is composed by the prompt section composer.")


def test_scope_inline_non_emitting_reasons_are_not_replaced_by_placement_help():
    disabled = _attachment(attachment_id="disabled-inline", placement="inline",
                           enabled=False)
    invalid = _attachment(attachment_id="invalid-inline", placement="inline",
                          channel="missing")
    oversized = _attachment("x" * (16 * 1024 + 1),
                            attachment_id="oversized-inline", placement="inline")
    result = _compile([
        _section("disabled", disabled, start=0, end=10),
        _section("invalid", invalid, start=10, end=20),
        _section("oversized", oversized, start=20, end=30),
    ])
    rows = {row["attachment_id"]: row
            for row in result["attachment_capability_projections"]}
    assert "disabled" in rows["disabled-inline"]["state_reason"].lower()
    assert "not in the active channel template" in rows["invalid-inline"]["state_reason"]
    assert "limit" in rows["oversized-inline"]["state_reason"].lower()


def test_suffix_region_is_server_owned():
    attachment = _attachment(placement="channel_suffix")
    row = _compile([_section("one", attachment)])["attachment_capability_projections"][0]
    assert (row["effective_phase"], row["region"]) == ("channel_suffix", "after")


def test_minimax_reference_accounts_for_all_five_capabilities_and_resolved_routes():
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "reference",
        "kind": "reference",
        "source": {"semantic_unit_ids": ["subject"]},
        "config": {
            "definition": "a woman in a black coat",
            "summary": "continue the action",
            "retention_detail": "keep identity and wardrobe",
            "text": "<Subject 1> waits by the door",
            "audio_relationship": "ambient sound remains independent",
        },
        "capabilities": [
            {"capability_id": "definitions", "kind": "definitions",
             "channel_key": "subject_definitions", "placement": "section_prefix"},
            {"capability_id": "summary", "kind": "summary",
             "channel_key": "summary", "placement": "section_prefix"},
            {"capability_id": "retention", "kind": "retention",
             "channel_key": "retention_analysis", "placement": "section_prefix"},
            {"capability_id": "mentions", "kind": "mentions",
             "channel_key": "detailed_description", "placement": "inline"},
            {"capability_id": "audio_relationship", "kind": "audio_relationship",
             "channel_key": "summary", "placement": "section_prefix"},
        ],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_section("one", attachment, document_channel="detailed_description",
                           document={"nodes": [{"type": "text", "node_id": "text",
                                                "text": "@KWoman walks forward."}]})],
        window_start=0,
        window_end=10,
        fps=24.0,
        template="minimax_h3_ref",
        profile="minimax_h3_ref@1",
        context={
            "setup_manifest": {"setup": {"mode": "reference"}},
            "ordinal_manifest": {"subjects": {"subject": 1}},
            "semantic_units": [{
                "semantic_unit_id": "subject",
                "name": "Korean Woman",
                "handle": "KWoman",
                "definition": "a woman in a black coat",
                "sources": [{"entity_id": "reference", "member_id": "member"}],
            }],
        },
    )

    rows = compiled["attachment_capability_projections"]
    assert [row["capability_id"] for row in rows] == [
        "definitions", "summary", "retention", "mentions", "audio_relationship"
    ]
    assert {row["capability_id"]: row["channel_key"] for row in rows} == {
        "definitions": "subject_definitions",
        "summary": "summary",
        "retention": "retention_analysis",
        "mentions": "detailed_description",
        "audio_relationship": "summary",
    }
    mention = next(row for row in rows if row["capability_id"] == "mentions")
    assert (mention["declared_placement"], mention["effective_phase"],
             mention["region"]) == ("inline", "inline", "before")

    preview = compiled["section_channel_previews"]["sections"]["one"]
    assert preview["index"] == 0
    assert preview["authored"]["detailed_description"] == (
        "<Subject 1> walks forward.")
    assert "@KWoman" not in preview["authored"]["detailed_description"]
    assert "a woman in a black coat" in preview["full"]["subject_definitions"]


def test_channel_previews_use_original_ids_and_skip_synthetic_ids():
    named = _section("global", _attachment("prefix", attachment_id="named-chip"),
                     document={"nodes": [{"type": "text", "node_id": "named-text",
                                          "text": "authored body"}]}, start=0, end=10)
    unnamed = _section("", _attachment("other", attachment_id="unnamed-chip"),
                       start=10, end=20)
    result = _compile([named, unnamed])
    previews = result["section_channel_previews"]
    assert previews["sections"]["global"] == {
        "index": 0,
        "authored": {"visual": "authored body"},
        "bar": {"visual": "authored body prefix"},
        "full": {"visual": "prefix authored body"},
    }
    assert previews["global"] == {
        "authored": {"visual": ""},
        "bar": {"visual": ""},
        "full": {"visual": ""},
    }
    assert not any(key.startswith("__compile_section_")
                   for key in previews["sections"])


def _enabled_probe(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for capability enabled coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    script = f"""
        const mod = await import({json.dumps(module_url)});
        {body}
    """
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def test_capability_enabled_is_stored_only_when_it_deviates():
    """Sparse like the routing beside it: absent means inherit.

    Writing `true` unconditionally made every record read as a deliberate
    override, so a shared Reference default could never take effect.
    """
    result = _enabled_probe("""
        const record = (enabled, inheritedEnabled) =>
            mod.sparseCapabilityRecord({}, {
                capabilityId: "summary", enabled, inheritedEnabled });
        console.log(JSON.stringify({
            onFollowingOn: record(true, true),
            offFollowingOff: record(false, false),
            offAgainstOn: record(false, true),
            onAgainstOff: record(true, false),
        }));
    """)
    assert "enabled" not in result["onFollowingOn"]
    assert "enabled" not in result["offFollowingOff"]
    assert result["offAgainstOn"]["enabled"] is False
    assert result["onAgainstOff"]["enabled"] is True


def test_inherited_capability_enabled_reads_the_reference_tiers():
    result = _enabled_probe("""
        const references = [{ reference_id: "ref", members: [
            { member_id: "m", disabled_capabilities: ["summary"] }] }];
        const semanticUnits = [
            { semantic_unit_id: "lead", disabled_capabilities: ["mentions"] }];
        const call = (capabilityId, selected) =>
            mod.resolveInheritedCapabilityEnabled(capabilityId, {
                selected, references, semanticUnits });
        console.log(JSON.stringify({
            memberOff: call("summary", "physical:picture:m"),
            memberOn: call("mentions", "physical:picture:m"),
            identityOff: call("mentions", "lead"),
            identityOn: call("summary", "lead"),
            unknownSelection: call("summary", "physical:picture:missing"),
        }));
    """)
    assert result["memberOff"] is False
    assert result["memberOn"] is True
    assert result["identityOff"] is False
    assert result["identityOn"] is True
    assert result["unknownSelection"] is True


def test_linked_propagation_never_carries_enabled_to_an_inheriting_target():
    """Per-section suppression is local and must not ride a linked edit.

    The previous shape restored the target's flag only when the target already
    held a record for that capability — true while every attach seeded one, but
    once records became sparse an inheriting target silently adopted the source
    chip's suppression.
    """
    result = _enabled_probe("""
        const configured = {
            attachment_id: "src", emission_group_id: "grp", kind: "reference",
            source: {}, config: {},
            capabilities: [
                { capability_id: "summary", kind: "summary", enabled: false },
                { capability_id: "mentions", kind: "mentions", enabled: false },
            ],
        };
        const inheriting = {
            attachment_id: "dst", emission_group_id: "grp", kind: "reference",
            source: {}, config: {},
            capabilities: [{ capability_id: "summary", kind: "summary" }],
        };
        const stating = {
            attachment_id: "dst2", emission_group_id: "grp", kind: "reference",
            source: {}, config: {},
            capabilities: [{ capability_id: "summary", kind: "summary",
                             enabled: true }],
        };
        const pick = (result, id) => (result.capabilities || []).find(
            (value) => value.capability_id === id);
        const toInheriting = mod.propagateLinkedPromptAttachment(
            configured, inheriting, "src");
        const toStating = mod.propagateLinkedPromptAttachment(
            configured, stating, "src");
        console.log(JSON.stringify({
            inheritingSummary: pick(toInheriting, "summary"),
            inheritingMentions: pick(toInheriting, "mentions"),
            statingSummary: pick(toStating, "summary"),
        }));
    """)
    # The target was inheriting and stays inheriting.
    assert "enabled" not in result["inheritingSummary"]
    # A capability the target held no record for must not gain the source's flag.
    assert "enabled" not in result["inheritingMentions"]
    # A target that stated its own value keeps it.
    assert result["statingSummary"]["enabled"] is True


@pytest.mark.parametrize("sibling_channel", ["speech", "visual"])
def test_emission_conflict_does_not_block_an_unrelated_capability(sibling_channel):
    first = _attachment("ignored", attachment_id="first", group_id="shared")
    second = _attachment("ignored", attachment_id="second", group_id="shared")
    for attachment, bad_text in ((first, "original"), (second, "conflicting")):
        attachment["capabilities"] = [
            {"capability_id": "bad", "kind": "custom", "placement": "section_prefix",
             "channel_key": "visual", "config": {"text": bad_text}},
            {"capability_id": "good", "kind": "custom", "placement": "section_prefix",
             "channel_key": sibling_channel, "config": {"text": "same"}},
        ]
    result = _compile([_section("one", first, start=0, end=10),
                       _section("two", second, start=10, end=20)], template="sonder")
    errors = [row for row in result["errors"] if row["code"] == "conflicting_emission"]
    assert [(row["attachment_id"], row["channel_key"], row["capability_id"])
            for row in errors] == [("second", "visual", "bad")]
    rows = {row["capability_id"]: row for row in result["attachment_capability_projections"]
            if row["attachment_id"] == "second"}
    assert rows["bad"]["state"] == "unresolved"
    # Same-group output elsewhere still owns the good row; it is not blocked.
    assert rows["good"]["state"] == "linked_elsewhere"


def test_output_limit_diagnostic_does_not_relabel_empty_sibling():
    attachment = _attachment(attachment_id="a")
    attachment["capabilities"] = [
        {"capability_id": "big", "kind": "custom", "placement": "section_prefix",
         "channel_key": "visual", "config": {"text": "x" * (16 * 1024 + 1)}},
        {"capability_id": "empty", "kind": "custom", "placement": "section_prefix",
         "channel_key": "speech", "config": {"text": ""}},
    ]
    result = _compile([_section("one", attachment)], template="sonder")
    errors = [row for row in result["errors"] if row["code"] == "attachment_output_limit"]
    assert [(row["channel_key"], row["capability_id"]) for row in errors] == [("visual", "big")]
    states = {row["capability_id"]: row["state"]
              for row in result["attachment_capability_projections"]}
    assert states == {"big": "output_limit", "empty": "empty"}
