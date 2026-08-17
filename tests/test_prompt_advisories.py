from server import prompt_context


def _compile(*, sections=None, context=None, template="minimax_h3_ref"):
    return prompt_context.compile_prompt_context(
        global_channels={}, sections=sections or [], window_start=0,
        window_end=24, fps=24, template=template, context=context or {})


def _codes(result, tier):
    return {value["code"] for value in result[tier]}


def test_empty_channels_are_advisory_for_minimax_and_general_templates():
    minimax = _compile(context={
        "setup_manifest": {"setup": {"mode": "reference"}},
    })
    assert "missing_h3_reference_field" in _codes(minimax, "warnings")
    assert "missing_h3_reference_field" not in _codes(minimax, "errors")

    generic = _compile(template="sonder")
    empty = [value for value in generic["warnings"]
             if value["code"] == "empty_channel"]
    assert empty
    assert all(value.get("channel_key") for value in empty)
    assert "empty_channel" not in _codes(generic, "errors")


def test_h3_noncanonical_summary_and_retention_detail_are_advisory():
    retention = prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"semantic_unit_ids": ["unit:one"]},
        "config": {"definition": "a woman"},
        "capabilities": [{
            "capability_id": "retention", "kind": "retention",
            "channel_key": "retention_analysis", "placement": "section_prefix",
        }],
    })
    result = _compile(sections=[{
        "prompt_id": "one", "start_frame": 0, "end_frame": 24,
        "channels": {"summary": "noncanonical opener"},
        "attachments": [retention],
    }], context={
        "setup_manifest": {"setup": {"mode": "reference"}},
        "ordinal_manifest": {"subjects": {"unit:one": 1}},
        "unit_source_labels": {"unit:one": ["<Picture 1>"]},
        "semantic_units": [{"semantic_unit_id": "unit:one", "name": "One",
                            "definition": "a woman", "sources": [{
                                "entity_id": "reference", "member_id": "portrait",
                            }]}],
    })
    assert {"invalid_h3_task_prefix", "missing_h3_retention_detail"} <= _codes(
        result, "warnings")
    assert not ({"invalid_h3_task_prefix", "missing_h3_retention_detail"}
                & _codes(result, "errors"))


def test_missing_h3_reference_definitions_are_advisory():
    subject = prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"semantic_unit_ids": ["unit:one"]},
    })
    physical = prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"picture_ids": ["portrait"]},
    })
    result = _compile(sections=[{
        "prompt_id": "one", "start_frame": 0, "end_frame": 24,
        "channels": {"summary": "[Subject Reference] action"},
        "attachments": [subject, physical],
    }], context={
        "setup_manifest": {"setup": {"mode": "reference"}},
        "ordinal_manifest": {
            "subjects": {"unit:one": 1}, "pictures": {"portrait": 1}},
        "unit_source_labels": {"unit:one": ["<Audio 1>"]},
        "semantic_units": [{"semantic_unit_id": "unit:one", "name": "One",
                            "definition": "", "sources": []}],
    })
    expected = {"missing_h3_subject_definition", "missing_h3_audio_definition",
                "missing_h3_physical_definition"}
    assert expected <= _codes(result, "warnings")
    assert not (expected & _codes(result, "errors"))


def test_missing_h3_shot_identity_is_advisory():
    result = _compile(template="minimax_h3_base", context={
        "setup_manifest": {"setup": {"mode": "base", "task_mode": "I2VA"}},
    })
    assert "missing_h3_shot_identity" in _codes(result, "warnings")
    assert "missing_h3_shot_identity" not in _codes(result, "errors")


def test_unnamed_staged_reference_warns_on_the_member_not_the_channel():
    """A staged member nothing in the prompt refers to.

    Deliberately a separate code from `missing_h3_reference_field`, which is the
    format-labelled variant of `empty_channel` and names an empty prompt
    channel: retargeting that one would destroy a different diagnostic and
    still not name the missing identity.
    """
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[], window_start=0, window_end=10, fps=24,
        template="minimax_h3_ref",
        context={
            "profile": "minimax_h3_ref@1",
            "semantic_units": [{"semantic_unit_id": "named", "sources": [
                {"entity_id": "e", "member_id": "described"}]}],
            "setup_manifest": {"pictures": [
                {"member_id": "described", "display_name": "Woman · Portrait",
                 "member_prompt": "", "role": "identity", "slot_number": 1},
                {"member_id": "prose_only", "display_name": "Street",
                 "member_prompt": "a wet street at night", "role": "style",
                 "slot_number": 2},
                {"member_id": "orphan", "display_name": "Prop",
                 "member_prompt": "", "role": "identity", "slot_number": 3},
            ]},
        })
    unnamed = [value for value in compiled["warnings"]
               if value["code"] == "unnamed_physical_reference"]
    # Only the member with neither an identity nor prose is flagged.
    assert [value["member_id"] for value in unnamed] == ["orphan"]
    # The advisory names the control the author must actually find. A rename on
    # the row without this leaves the server pointing at a button that no longer
    # exists, which is worse than no instruction at all.
    assert "+ Identity" in unnamed[0]["message"]
    # It is a warning, not a blocker: the pixels still reach the model.
    assert not [value for value in compiled["errors"]
                if value["code"] == "unnamed_physical_reference"]
