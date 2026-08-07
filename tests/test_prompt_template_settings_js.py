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
                "subject_ids": ["ref-a", "ref-b"],
            },
            {
                "start_frame": 96,
                "end_frame": 192,
                "channels": {key: f"{key} two" for key in MINIMAX_KEYS},
                "starts_new_shot": False,
                "shot_timestamp": True,
                "subject_ids": [],
            },
        ],
    }


def test_six_channel_template_keeps_every_channel():
    [template] = _round_trip([_minimax_template()])
    assert len(template["sections"]) == 2
    for index, section in enumerate(template["sections"]):
        assert sorted(section["channels"]) == sorted(MINIMAX_KEYS)
        suffix = "text" if index == 0 else "two"
        for key in MINIMAX_KEYS:
            assert section["channels"][key] == f"{key} {suffix}"


def test_round_trip_keeps_per_section_shot_and_subject_fields():
    [template] = _round_trip([_minimax_template()])
    first, second = template["sections"]
    assert first["starts_new_shot"] is True
    assert first["shot_timestamp"] is False
    assert first["subject_ids"] == ["ref-a", "ref-b"]
    assert second["starts_new_shot"] is False
    assert second["shot_timestamp"] is True
    assert second["subject_ids"] == []


def test_round_trip_keeps_global_channels_and_source_template():
    [template] = _round_trip([_minimax_template()])
    assert template["source_channel_template"] == "minimax_h3_ref"
    assert template["global_channels"] == {
        "detailed_description": "A rain-soaked alley.",
        "overall_soundscape": "Steady rain on metal.",
    }
    assert template["source_fps"] == 24


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
