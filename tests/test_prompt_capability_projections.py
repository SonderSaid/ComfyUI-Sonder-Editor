"""Per-capability prompt projection contract and terminal-state coverage."""

from server import prompt_context


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


def _section(prompt_id, attachment, *, document=None, start=0, end=10):
    return {
        "prompt_id": prompt_id,
        "start_frame": start,
        "end_frame": end,
        "channels": {"visual": "body"},
        "channel_docs": ({"visual": document} if document else {}),
        "attachments": [attachment],
    }


def _compile(sections):
    return prompt_context.compile_prompt_context(
        global_channels={}, sections=sections, window_start=0, window_end=100,
        fps=24.0, template="standard", profile="generic@1")


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
    emitted = _compile([_section("literal", literal)])
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
        sections=[_section("one", attachment)],
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
                "definition": "a woman in a black coat",
                "source_members": [],
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
