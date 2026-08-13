"""Browser prompt templates must survive a settings round trip intact.

`normalizePromptTemplates` in web/js/editor_settings.js runs on EVERY settings
load, so any channel key it does not name is deleted from storage. It used to
name `visual`/`speech`/`sounds` literally, which meant a template saved under a
six-field MiniMax project came back with its section boundaries and no text at
all — the reported "applies blank" failure.

Prompt templates are browser-local and cross-project by design: this side cannot
know which channel set is the right one, so it must keep every authored key.
These tests are the regression guard for that.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "editor_settings.js").as_uri()

MINIMAX_KEYS = [
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
]


def _round_trip(templates):
    """Run `templates` through normalizeEditorSettings and return them back."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the prompt-template settings tests")
    script = (
        f"const mod = await import({json.dumps(MODULE_URL)});\n"
        f"const stored = {{ promptTemplates: {json.dumps(templates)} }};\n"
        "console.log(JSON.stringify(mod.normalizeEditorSettings(stored).promptTemplates));\n"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(result.stdout)


def _normalize_settings(stored):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the settings tests")
    script = (
        f"const mod = await import({json.dumps(MODULE_URL)});\n"
        f"console.log(JSON.stringify(mod.normalizeEditorSettings({json.dumps(stored)})));\n"
    )
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)


def _all_channel_templates(stored):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the settings tests")
    script = (
        f"const mod = await import({json.dumps(MODULE_URL)});\n"
        f"const settings = mod.normalizeEditorSettings({json.dumps(stored)});\n"
        "console.log(JSON.stringify(mod.getAllPromptChannelTemplates(settings)));\n"
    )
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)


def _minimax_template():
    return {
        "id": "pt-minimax",
        "name": "Test",
        "global": "A rain-soaked alley.",
        "global_channels": {
            "detailed_description": "A rain-soaked alley.",
            "overall_soundscape": "Steady rain on metal.",
        },
        "source_fps": 24,
        "source_channel_template": "minimax_h3_ref",
        "sections": [
            {
                "start_frame": 0,
                "end_frame": 96,
                "channels": {key: f"{key} text" for key in MINIMAX_KEYS},
                "starts_new_shot": True,
                "shot_timestamp": False,
                "subject_ids": [
                    {"entity_id": "ref-a", "retention": "fully_preserved"},
                    {"entity_id": "ref-b", "retention": "partially_preserved"},
                ],
            },
            {
                "start_frame": 96,
                "end_frame": 192,
                "channels": {key: f"{key} two" for key in MINIMAX_KEYS},
                "starts_new_shot": False,
                "shot_timestamp": True,
                "subject_ids": [],
            },
            {
                "start_frame": 192,
                "end_frame": 288,
                "channels": {key: f"{key} three" for key in MINIMAX_KEYS},
                "starts_new_shot": True,
                "shot_timestamp": True,
                "subject_ids": [
                    {"entity_id": "ref-c", "retention": "attribute_transfer"},
                    {"entity_id": "ref-d", "retention": "weak_reference"},
                ],
            },
        ],
    }


def _binding(entity_id, retention="fully_preserved"):
    return {"entity_id": entity_id, "retention": retention}


def test_six_channel_template_keeps_every_channel():
    [template] = _round_trip([_minimax_template()])
    assert len(template["sections"]) == 3
    for suffix, section in zip(("text", "two", "three"), template["sections"]):
        assert sorted(section["channels"]) == sorted(MINIMAX_KEYS)
        for key in MINIMAX_KEYS:
            assert section["channels"][key] == f"{key} {suffix}"


def test_round_trip_keeps_shot_fields_and_drops_legacy_subject_ids():
    [template] = _round_trip([_minimax_template()])
    first, second, third = template["sections"]
    assert first["starts_new_shot"] is True
    assert first["shot_timestamp"] is False
    # Bindings are `{entity_id, retention}` objects and must survive as such —
    # the settings normalizer used to String() each one into "[object Object]"
    # and then dedupe them all into a single entry.
    assert "subject_ids" not in first
    assert second["starts_new_shot"] is False
    assert second["shot_timestamp"] is True
    assert "subject_ids" not in second
    # Authored objects survive with their own retention markers intact.
    assert "subject_ids" not in third


def test_round_trip_drops_legacy_bindings_without_conversion():
    """The defect that motivated this: every binding String()-ed to the same
    literal, so the normalizer's own dedupe then collapsed them into one."""
    template = _minimax_template()
    template["sections"][0]["subject_ids"] = [
        {"entity_id": "ref-a", "retention": "fully_preserved"},
        {"entity_id": "ref-b", "retention": "partially_preserved"},
        {"entity_id": "ref-c", "retention": "weak_reference"},
    ]
    [out] = _round_trip([template])
    assert "subject_ids" not in out["sections"][0]


@pytest.mark.parametrize("stored", [{}, {"entity_id": "ref-a"}, 5, "abc", None, True])
def test_non_list_legacy_subject_ids_never_aborts_the_settings_module(stored):
    """`normalizeEditorSettings` evaluates at module scope over user-editable
    localStorage with no try/catch, so a throw here would abort every static
    importer of editor_settings.js. A string must not iterate per character
    either. Matches prompt_payload.normalize_subject_ids, which returns []."""
    template = _minimax_template()
    template["sections"][0]["subject_ids"] = stored
    [out] = _round_trip([template])
    assert "subject_ids" not in out["sections"][0]


def test_bare_string_legacy_bindings_are_dropped():
    """A binding is an object. `retention` shipped in the same commit as
    `subject_ids`, so no bare-string form was ever authored — and the strings
    actually sitting in dev browser storage are the literal "[object Object]"
    the pre-fix normalizer wrote. Reviving either would mint a binding to an
    entity that never existed, one `_prune_prompt_subject_ids` can never clear
    because it only drops ids that were actually removed."""
    template = _minimax_template()
    template["sections"][0]["subject_ids"] = ["[object Object]", "ref-a"]
    [out] = _round_trip([template])
    assert "subject_ids" not in out["sections"][0]


def test_round_trip_keeps_global_channels_and_source_template():
    [template] = _round_trip([_minimax_template()])
    assert template["source_channel_template"] == "minimax_h3_ref"
    assert template["global_channels"] == {
        "detailed_description": "A rain-soaked alley.",
        "overall_soundscape": "Steady rain on metal.",
    }
    assert template["source_fps"] == 24


def test_round_trip_keeps_context_documents_attachments_and_dependencies():
    template = _minimax_template()
    template.update({
        "global_channel_docs": {
            "detailed_description": {"schema": "prompt_document_v1", "nodes": [
                {"type": "text", "node_id": "g-text", "text": "rain "},
                {"type": "attachment", "node_id": "g-chip",
                 "attachment_id": "guide-1"},
            ]},
        },
        "global_attachments": [{
            "attachment_id": "guide-1", "emission_group_id": "guide-group",
            "kind": "guide", "source": {}, "config": {"text": "continues"},
        }],
        "prompt_context_profile_id": "custom:test@1",
        "prompt_context_profile_config": {"mode": "strict"},
        "prompt_context_profiles": [{
            "profile_id": "custom:test", "version": "1", "name": "Test",
            "template_id": "minimax_h3_ref", "capabilities": {},
            "writing_aids": [],
        }],
        "prompt_semantic_units": [{
            "semantic_unit_id": "unit-1", "name": "Granny",
            "source_members": [{"entity_id": "ref-1", "member_id": "member-1"}],
        }],
        "minimax_h3_conditioning_setups": [{
            "setup_id": "setup-1", "mode": "reference",
        }],
        "active_minimax_h3_setup_id": "setup-1",
    })
    template["sections"][0].update({
        "prompt_id": "prompt-1",
        "channel_docs": {"detailed_description": {
            "schema": "prompt_document_v1",
            "nodes": [{"type": "attachment", "node_id": "s-chip",
                       "attachment_id": "shot-1"}],
        }},
        "attachments": [{
            "attachment_id": "shot-1", "emission_group_id": "shot-group",
            "kind": "shot", "source": {}, "config": {},
        }],
        "muted": True,
    })

    [out] = _round_trip([template])
    assert out["global_channel_docs"]["detailed_description"]["nodes"][1]["attachment_id"] == "guide-1"
    assert out["global_attachments"][0]["attachment_id"] == "guide-1"
    assert out["sections"][0]["prompt_id"] == "prompt-1"
    assert out["sections"][0]["attachments"][0]["kind"] == "shot"
    assert out["sections"][0]["muted"] is True
    assert out["prompt_context_profile_id"] == "custom:test@1"
    assert out["prompt_context_profile_config"] == {"mode": "strict"}
    assert out["prompt_context_profiles"][0]["profile_id"] == "custom:test"
    assert out["prompt_semantic_units"][0]["semantic_unit_id"] == "unit-1"
    assert out["minimax_h3_conditioning_setups"][0]["setup_id"] == "setup-1"
    assert out["active_minimax_h3_setup_id"] == "setup-1"


def test_round_trip_is_idempotent():
    """A stored template is normalized on every load, not just the first."""
    once = _round_trip([_minimax_template()])
    twice = _round_trip(once)
    assert twice == once


def test_legacy_flat_template_still_loads():
    """Entries predating channels seed the first channel from `prompt`."""
    [template] = _round_trip([{
        "id": "pt-legacy",
        "name": "Legacy",
        "global": "A quiet room.",
        "sections": [{"start_frame": 0, "end_frame": 48, "prompt": "A man sits."}],
    }])
    assert template["sections"][0]["channels"] == {"visual": "A man sits."}
    assert template["global_channels"] == {"visual": "A quiet room."}


def test_empty_global_stays_null_so_apply_can_replace_it():
    """An empty bag would MERGE server-side and silently keep the target's
    global; null keeps the entry on the flat-text path, which replaces."""
    [template] = _round_trip([{
        "id": "pt-noglobal",
        "name": "No global",
        "global": "",
        "global_channels": {"visual": "", "speech": ""},
        "sections": [{"start_frame": 0, "end_frame": 48, "channels": {"visual": "x"}}],
    }])
    assert template["global_channels"] is None


def test_unknown_channel_keys_survive():
    """A custom template's channel keys are unknown to this module by design."""
    [template] = _round_trip([{
        "id": "pt-custom",
        "name": "Custom",
        "sections": [{
            "start_frame": 0, "end_frame": 24,
            "channels": {"mood": "tense", "camera": "handheld"},
        }],
    }])
    assert template["sections"][0]["channels"] == {"mood": "tense", "camera": "handheld"}


def test_channel_template_catalog_drops_malformed_entries_and_renames_collisions():
    valid = {
        "id": "custom:mine", "name": "Mine",
        "channels": [{"key": "body", "label": "Body"}],
        "labels": "always",
    }
    settings = _normalize_settings({
        "promptChannelTemplates": {
            "customTemplates": [valid, {**valid}, {"id": "broken", "channels": []}],
        },
    })
    templates = settings["promptChannelTemplates"]["customTemplates"]
    assert [entry["id"] for entry in templates] == ["custom:mine", "custom:mine-2"]
    assert all([channel["key"] for channel in entry["channels"]] == ["body"]
               for entry in templates)


def test_new_project_defaults_are_standard_and_model_agnostic():
    settings = _normalize_settings({})
    assert settings["projectDefaults"]["defaultChannelTemplateId"] == "standard"
    assert settings["projectDefaults"]["defaultTemplateId"] == "free"


def test_channel_template_catalog_keeps_builtins_read_only_and_customs_owned():
    templates = _all_channel_templates({
        "promptChannelTemplates": {"customTemplates": [{
            "id": "custom:mine", "name": "Mine",
            "channels": [{"key": "body", "label": "Body"}],
            "labels": "always",
        }]},
    })
    builtins = [entry for entry in templates if entry["id"] == "sonder"]
    customs = [entry for entry in templates if entry["id"] == "custom:mine"]
    assert len(builtins) == 1 and builtins[0]["builtin"] is True
    assert len(customs) == 1 and customs[0].get("builtin") is not True


def test_channel_catalog_normalization_never_touches_prompt_template_text_bags():
    prompt = _minimax_template()
    settings = _normalize_settings({
        "promptChannelTemplates": {
            "customTemplates": [{
                "id": "custom:mine", "name": "Mine",
                "channels": [{"key": "mood", "label": "Mood"}],
                "labels": "always",
            }],
        },
        "promptTemplates": [prompt],
    })
    section = settings["promptTemplates"][0]["sections"][0]
    assert sorted(section["channels"]) == sorted(MINIMAX_KEYS)


def test_prompt_template_keeps_whole_source_definition_alongside_legacy_id():
    prompt = _minimax_template()
    prompt["source_channel_template_id"] = "custom:authored"
    prompt["source_channel_template"] = {
        "id": "custom:authored", "name": "Authored",
        "channels": [{"key": "body", "label": "Body"}],
        "labels": "always",
    }
    [restored] = _round_trip([prompt])
    assert restored["source_channel_template_id"] == "custom:authored"
    assert restored["source_channel_template"]["id"] == "custom:authored"
    assert [entry["key"] for entry in restored["source_channel_template"]["channels"]] == ["body"]
