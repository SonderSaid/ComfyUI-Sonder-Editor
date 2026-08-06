"""Channel template editing: forking a preset, and what an edit does to text.

Two halves. The JS half covers the overlay's draft validation and the dict it
saves as. The Python half covers what `api_update_project` does with that dict —
normalizing it before it becomes the template every later read resolves against,
and collapsing section and global text when the channel set moves.

Editing a custom template in place keeps its id while changing what it means, so
an id-only comparison would silently skip the rewrite and strand text under keys
nothing reads. That is what the recollapse tests here pin.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import prompt_channel_templates as pct
import server.routes as routes
from server.timeline_state import LaneConfig, PromptSection, Scene, TimelineProject


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "editor_channel_template_editor.js").as_uri()


def _node_json(expression):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the channel-template editor tests")
    script = (
        f"const mod = await import({json.dumps(MODULE_URL)});\n"
        f"console.log(JSON.stringify({expression}));\n"
    )
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)


_VALID_DRAFT = {
    "id": "sonder", "name": "My Set", "description": "",
    "channels": [{"key": "body", "label": "body", "description": "The body."}],
    "field_separator": "\n\n", "label_separator": ": ", "labels": "always",
    "shot_marker_channel": "body", "global_merge": "per_channel",
    "global_channels_enabled": True,
}


def _draft(**overrides):
    draft = json.loads(json.dumps(_VALID_DRAFT))
    draft.update(overrides)
    return draft


# --- draft validation (JS) ----------------------------------------------------

def test_a_complete_draft_validates():
    assert _node_json(f"mod.validateTemplateDraft({json.dumps(_VALID_DRAFT)})") == []


@pytest.mark.parametrize("draft,fragment", [
    (_draft(name="  "), "name"),
    (_draft(channels=[]), "at least one channel"),
    (_draft(channels=[{"key": "", "label": "x"}]), "needs a key"),
    # A duplicate key makes one channel unreachable rather than merely odd.
    (_draft(channels=[{"key": "a", "label": "a"}, {"key": "a", "label": "b"}]), "Duplicate"),
    # The writing tool parses `key:` at line start, so a key with spaces could
    # not be typed back in.
    (_draft(channels=[{"key": "two words", "label": "x"}]), "lowercase"),
    (_draft(channels=[{"key": "a", "label": ""}]), "header for every channel"),
])
def test_incomplete_drafts_are_refused(draft, fragment):
    errors = _node_json(f"mod.validateTemplateDraft({json.dumps(draft)})")
    assert errors, draft
    assert any(fragment in error for error in errors), errors


def test_a_label_free_template_needs_no_headers():
    draft = _draft(labels="never", channels=[{"key": "body", "label": ""}])
    assert _node_json(f"mod.validateTemplateDraft({json.dumps(draft)})") == []


def test_forking_a_builtin_mints_a_custom_id_and_never_shadows_a_preset():
    forked = _node_json(
        f"mod.templateFromDraft({json.dumps(_VALID_DRAFT)}, {{fork: true}})")
    assert forked["id"] == "custom:my-set"
    assert forked["builtin"] is False
    # A fork must not be able to take a preset's id, or resolving that id would
    # return the preset and the project would compose under the wrong template.
    assert forked["id"] not in pct.PROMPT_CHANNEL_TEMPLATE_PRESETS


def test_editing_a_custom_template_keeps_its_id():
    draft = _draft(id="custom:my-set", name="My Set")
    saved = _node_json(f"mod.templateFromDraft({json.dumps(draft)}, {{fork: false}})")
    assert saved["id"] == "custom:my-set"


# --- what the backend does with it -------------------------------------------

def _project_with_text():
    project = TimelineProject(name="Project")
    project.metadata = {pct.PROJECT_TEMPLATE_KEY: "minimax_h3_base"}
    scene = Scene(scene_id="scene-1", duration_frames=240)
    scene.prompt_track_config = LaneConfig()
    scene.global_prompt_track_config = LaneConfig()
    scene.prompt_sections = [PromptSection(0, 120, channels={
        "integrated_multimodal_description": "a dog walks",
        "overall_soundscape": "rain",
    })]
    scene.set_global_channels({
        "integrated_multimodal_description": "cinematic",
        "overall_soundscape": "a low hum",
    })
    project.scenes.append(scene)
    return project


def _fork_with(template, **overrides):
    """A project-owned copy of `template` with fields overridden.

    `template_freeze_value` returns a bare id for a built-in by design, so a
    fork is built from the preset's own fields rather than from its freeze.
    """
    raw = {key: value for key, value in template.items() if key != "builtin"}
    raw["channels"] = [dict(channel) for channel in template["channels"]]
    raw.update(overrides)
    return pct.normalize_channel_template(raw)


def test_renaming_a_channel_moves_its_text_under_a_header():
    project = _project_with_text()
    previous = pct.get_channel_template("minimax_h3_base")
    renamed = pct.normalize_channel_template({
        "id": "custom:mine", "name": "Mine",
        "channels": [{"key": "body", "label": "body"},
                     {"key": "overall_soundscape", "label": "overall_soundscape"}],
        "labels": "always",
    })
    routes._recollapse_prompt_channels(project, previous, renamed)
    channels = project.scenes[0].prompt_sections[0].channels
    # The text is visible and movable under its old channel name rather than
    # stranded under a key the new template never reads.
    assert "integrated_multimodal_description:" in channels["body"]
    assert "a dog walks" in channels["body"]
    # A channel the rename kept is untouched.
    assert channels["overall_soundscape"] == "rain"


def test_turning_global_channels_off_folds_the_others_into_one_box():
    project = _project_with_text()
    previous = pct.get_channel_template("minimax_h3_base")
    off = _fork_with(previous, id="custom:off", global_channels_enabled=False)
    assert pct.global_channel_keys(off) == ("integrated_multimodal_description",)

    routes._recollapse_scene_globals(project, previous, off)
    globals_now = project.scenes[0].global_channels
    lead = globals_now["integrated_multimodal_description"]
    assert "cinematic" in lead
    assert "a low hum" in lead
    assert "overall_soundscape:" in lead

    # ...and turning it back on puts them where they were.
    routes._recollapse_scene_globals(project, off, previous)
    restored = project.scenes[0].global_channels
    assert restored["integrated_multimodal_description"].strip() == "cinematic"
    assert restored["overall_soundscape"].strip() == "a low hum"


def test_the_global_mirror_follows_a_collapse():
    project = _project_with_text()
    previous = pct.get_channel_template("minimax_h3_base")
    off = _fork_with(previous, id="custom:off", global_channels_enabled=False)
    routes._recollapse_scene_globals(project, previous, off)
    scene = project.scenes[0]
    # `prompt` is a derived mirror; leaving it stale would serve the pre-collapse
    # text to every reader that still uses the flat field.
    assert scene.prompt == scene.to_dict()["prompt"]

    # The mirror is LEGACY three-channel, so it is empty for a MiniMax channel
    # set either way. A leading merge must therefore read the global channels
    # themselves, or turning per-channel globals off would silently drop the
    # whole global prompt.
    assert scene.prompt == ""
    composed = scene.get_prompt_for_range(0, 120, delimiter=".", template=off, fps=24.0)
    assert "cinematic" in composed
    assert "a low hum" in composed
    assert composed.startswith("cinematic")


def test_a_malformed_custom_template_falls_back_rather_than_composing_nothing():
    # A hand-edited project.json (or a bad client) must not be able to leave the
    # project with a template that has no channels at all.
    fallback = pct.normalize_channel_template({"id": "custom:broken", "channels": []})
    assert fallback["id"] == pct.DEFAULT_CHANNEL_TEMPLATE_ID
    assert pct.template_channel_keys(fallback)
