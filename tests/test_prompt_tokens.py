import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import prompt_context, prompt_tokens


ROOT = Path(__file__).resolve().parents[1]


def _section(attachment, text="body"):
    return {
        "prompt_id": "section",
        "start_frame": 0,
        "end_frame": 24,
        "channels": {"visual": text, "summary": text,
                     "subject_definitions": text},
        "attachments": [attachment],
    }


def _reference_context(subject_ordinal=1, picture_ordinal=1):
    return {
        "setup_manifest": {
            "setup": {"mode": "reference"},
            "pictures": [{"member_id": "portrait", "picture_ordinal": picture_ordinal}],
            "presentation": [{"kind": "picture", "member_id": "portrait",
                              "picture_ordinal": picture_ordinal}],
        },
        "ordinal_manifest": {
            "subjects": {"unit:woman": subject_ordinal},
            "pictures": {"portrait": picture_ordinal},
            "videos": {"motion": 2},
            "audios": {"voice": 3},
        },
        "unit_source_labels": {"unit:woman": [f"<Picture {picture_ordinal}>"]},
        "semantic_units": [{
            "semantic_unit_id": "unit:woman", "name": "Woman",
            "definition": "the person beside @picture(portrait)",
            "source_members": [{"entity_id": "woman", "member_id": "portrait"}],
        }],
    }


def _reference_attachment(capability, config):
    return prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"semantic_unit_ids": ["unit:woman"]},
        "config": config,
        "capabilities": [{
            "capability_id": capability,
            "kind": capability,
            "channel_key": ({"summary": "summary",
                             "definitions": "subject_definitions"}.get(
                                 capability, "visual")),
            "placement": "section_prefix",
        }],
    })


def test_python_and_javascript_token_vocabularies_match():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt token grammar parity")
    module_url = (ROOT / "web" / "js" / "prompt_tokens.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        "console.log(JSON.stringify(Object.fromEntries(Object.entries("
        "mod.PROMPT_TOKEN_KINDS).map(([key,value]) => [key, "
        "[value.manifestKey,value.label]]))));\n"
    )
    js_vocabulary = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    py_vocabulary = {key: list(value)
                     for key, value in prompt_tokens.PROMPT_TOKEN_KINDS.items()}
    assert js_vocabulary == py_vocabulary


def test_token_resolves_in_summary_definition_and_custom_text_paths():
    context = _reference_context()
    summary = _reference_attachment(
        "summary", {"summary": "The target follows @subject(unit:woman)"})
    summary_result = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(summary)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref", context=context)
    assert "The target follows <Subject 1>" in summary_result["prompt"]

    definition = _reference_attachment("definitions", {})
    definition_result = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(definition)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref", context=context)
    assert "<Subject 1> is the person beside <Picture 1>" in definition_result["prompt"]

    custom = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "Track @video(motion) and @audio(voice)"},
    })
    custom_result = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(custom, "")], window_start=0,
        window_end=24, fps=24, template="standard", context=context)
    assert custom_result["prompt"] == "Track <Video 2> and <Audio 3>"


def test_reordering_setup_renumbers_without_changing_authored_prose():
    attachment = _reference_attachment(
        "summary", {"summary": "Follow @subject(unit:woman)"})
    first = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context(subject_ordinal=1))
    second = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context(subject_ordinal=2))
    assert "<Subject 1>" in first["prompt"]
    assert "<Subject 2>" in second["prompt"]
    assert attachment["config"]["summary"] == "Follow @subject(unit:woman)"


def test_deleted_token_source_blocks_and_literal_provider_label_is_untouched():
    attachment = _reference_attachment(
        "summary", {"summary": "Follow @subject(unit:deleted), not <Subject 1>"})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context())
    diagnostic = next(value for value in compiled["errors"]
                      if value["code"] == "unresolved_prompt_token")
    assert diagnostic["attachment_id"] == attachment["attachment_id"]
    assert diagnostic["prompt_token_id"] == "unit:deleted"
    assert "@subject(unit:deleted), not <Subject 1>" in compiled["prompt"]


def test_attachment_limit_is_measured_after_token_resolution():
    attachment = prompt_context.normalize_attachment({
        "kind": "custom",
        "config": {"text": " ".join(["@subject(x)"] * 1200)},
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment, "")], window_start=0,
        window_end=24, fps=24, template="standard",
        context={"ordinal_manifest": {"subjects": {"x": 123456}}})
    assert any(value["code"] == "attachment_output_limit"
               for value in compiled["errors"])


def test_one_attachment_exposes_split_channel_projections_outside_content_hash():
    attachment = prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"semantic_unit_ids": ["unit:woman"]},
        "config": {"summary": "Summary line", "text": "Mention line"},
        "capabilities": [
            {"capability_id": "definitions", "kind": "definitions",
             "channel_key": "subject_definitions", "placement": "section_prefix"},
            {"capability_id": "summary", "kind": "summary",
             "channel_key": "summary", "placement": "section_prefix"},
            {"capability_id": "mentions", "kind": "mentions",
             "channel_key": "detailed_description", "placement": "section_prefix"},
        ],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(attachment)], window_start=0,
        window_end=24, fps=24, template="minimax_h3_ref",
        context=_reference_context())

    split = compiled["attachment_channel_previews"][attachment["attachment_id"]]
    assert set(split) == {"subject_definitions", "summary", "detailed_description"}
    assert "<Subject 1>" in split["subject_definitions"]
    assert split["summary"] == "Summary line"
    assert split["detailed_description"] == "Mention line"
    # The field is explanatory projection metadata, not frozen prompt content.
    expected_hash = prompt_context.content_hash({
        "prompt": compiled["prompt"], "channels": compiled["channels"],
        "segments": compiled["segments"], "window": compiled["window"],
        "profile_hash": compiled["profile_hash"],
        "setup_manifest": compiled["setup_manifest"],
    })
    assert compiled["content_hash"] == expected_hash


def test_token_in_authored_prose_stays_literal_and_is_advisory():
    authored = "Keep @subject(unit:woman) literal"
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 24,
            "channel_docs": {"visual": {"nodes": [{
                "type": "text", "node_id": "text", "text": authored,
            }]}}, "attachments": [],
        }], window_start=0, window_end=24, fps=24, template="standard",
        context=_reference_context())
    assert authored in compiled["prompt"]
    assert not compiled["errors"]
    advisory = next(value for value in compiled["warnings"]
                    if value["code"] == "authored_prompt_token_literal")
    assert advisory["origin"] == "section"
    assert advisory["channel_key"] == "visual"
