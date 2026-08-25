"""The scene-global prompt lane must show what the global prompt actually says.

The lane label read `Scene.prompt`, a mirror the backend derives with no
template, so it is empty for every non-legacy channel set — the lane drew
"Global prompt (empty)" over text plainly visible in the inline editor.

The fix is narrower than it looks. The global prompt is authored over
`globalChannelKeys`, NOT over every channel the template names: a template with
per-channel globals turned off collapses to the first key alone, while stored
`global_channels` keeps whatever channels 2..n held before the flag flipped
(`normalizeChannels` preserves them on purpose). Composing the whole template
would put text on screen that never reaches the model — the same class of lie,
pointed the other way.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "prompt_composition.js").as_uri()
TEMPLATE_MODULE_URL = (ROOT / "web" / "js" / "prompt_channel_templates.js").as_uri()

# Two global channels, so "all keys" and "first key only" are distinguishable.
TEMPLATE = {
    "id": "test:two",
    "name": "Two",
    "channels": [
        {"key": "alpha", "label": "[A]:", "description": ""},
        {"key": "beta", "label": "[B]:", "description": ""},
    ],
    "labels": "project",
    "label_separator": " ",
}
CHANNELS = {"alpha": "first text", "beta": "second text", "stray": "foreign key"}


def _lines(channels, template, labels_on=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the global prompt label test")
    script = (
        f"const {{ globalChannelLines }} = await import({json.dumps(MODULE_URL)});\n"
        f"console.log(JSON.stringify(globalChannelLines("
        f"{json.dumps(channels)}, {json.dumps(template)}, {json.dumps(labels_on)})));\n"
    )
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)


def _method_source(source, name):
    marker = f"    {name}("
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1].strip()
    raise AssertionError(f"Unclosed method {name}")


def _bar_probe(payload, section, template):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the prompt bar label test")
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    methods = "\n".join(_method_source(widget, name) for name in (
        "_promptCompiledBarLabel", "_promptSectionBarLabel", "_promptGlobalBarLabel"))
    script = f"""
const composition = await import({json.dumps(MODULE_URL)});
const templates = await import({json.dumps(TEMPLATE_MODULE_URL)});
const {{ composeSectionText, globalChannelLines, normalizeChannels,
  promptPreviewAuthoredFirstChannels }} = composition;
const {{ globalChannelKeys, templateChannelKeys }} = templates;
const PROMPT_BAR_LABEL_MEMO = new WeakMap();
class Subject {{
  constructor() {{
    this.payload = {json.dumps(payload)};
    this.activeScene = {{ prompt: "legacy global", global_channels: {{
      alpha: "fallback global", beta: "hidden global" }} }};
    this.template = {json.dumps(template)};
  }}
  _channelTemplate() {{ return this.template; }}
  _windowedPromptCandidate() {{ return this.payload; }}
  _globalChannelKeys() {{ return globalChannelKeys(this.template); }}
  {methods}
}}
const subject = new Subject();
console.log(JSON.stringify({{
  section: subject._promptSectionBarLabel({json.dumps(section)}),
  global: subject._promptGlobalBarLabel(),
}}));
"""
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def test_every_global_channel_shows_when_per_channel_globals_are_on():
    assert _lines(CHANNELS, {**TEMPLATE, "global_channels_enabled": True}) == [
        "first text", "second text",
    ]


def test_only_the_first_channel_shows_when_per_channel_globals_are_off():
    """The case that makes `composeSectionText` the wrong tool.

    `beta` still holds text — turning the flag off does not erase it — but the
    global prompt no longer serves it, so showing it would advertise text the
    model never sees.
    """
    assert _lines(CHANNELS, {**TEMPLATE, "global_channels_enabled": False}) == ["first text"]


def test_keys_outside_the_template_never_leak_onto_the_label():
    """`stray` is preserved in storage on purpose; it is not global text."""
    for enabled in (True, False):
        lines = _lines(CHANNELS, {**TEMPLATE, "global_channels_enabled": enabled})
        assert not any("foreign key" in line for line in lines)


def test_labels_follow_the_template_rule():
    on = _lines(CHANNELS, {**TEMPLATE, "global_channels_enabled": True}, labels_on=True)
    assert on == ["[A]: first text", "[B]: second text"]
    # `labels: never` overrides the project preference, same as sections.
    forced_off = _lines(
        CHANNELS, {**TEMPLATE, "global_channels_enabled": True, "labels": "never"}, labels_on=True)
    assert forced_off == ["first text", "second text"]


def test_blank_and_missing_channels_produce_no_lines():
    """An empty result is what lets the caller fall back to the legacy mirror."""
    assert _lines({}, TEMPLATE) == []
    assert _lines({"alpha": "   ", "beta": ""}, TEMPLATE) == []


def test_surfaces_consume_the_shared_helper_rather_than_the_legacy_mirror():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    canvas = (ROOT / "web" / "js" / "editor_timeline_canvas.js").read_text(encoding="utf-8")

    # The lane bar is label-free and joins with a space, mirroring the backend
    # derivation in prompt_payload.compose_range_prompt.
    assert "_promptGlobalBarLabel()" in widget
    assert "section_channel_previews" in widget
    assert "this._globalChannelKeys()" in widget
    assert "host._promptGlobalBarLabel" in canvas

    # The hover panel is multi-line and labelled, matching its section branch —
    # a flat blob above labelled sections would read as a different kind of
    # content rather than the same content scoped scene-wide.
    hover = widget.split("_showPromptHoverPreview(hit, clientX, clientY) {", 1)[1]
    hover = hover.split("\n    _hidePromptHoverPreview", 1)[0]
    assert "promptPreviewFullChannelLines(" in hover
    assert "this._globalChannelKeys()" in hover
    assert '"(empty global prompt)"' in hover


def test_bar_preview_is_authored_first_collapsed_and_falls_back_without_a_row():
    template = {**TEMPLATE, "labels": "never", "global_channels_enabled": False}
    payload = {"_stale": True, "section_channel_previews": {
        "sections": {"section": {
            "authored": {"alpha": "Authored\n  sentence", "beta": "other authored"},
            "bar": {"alpha": "Authored\n  sentence\nPrefix line\nSuffix line",
                    "beta": "other authored\nBeta prefix"},
            "full": {"alpha": "Prefix line\nAuthored\n  sentence\nSuffix line",
                     "beta": "Beta prefix\nother authored"},
        }},
        "global": {
            "authored": {"alpha": "Global authored", "beta": "must not show"},
            "bar": {"alpha": "Global authored\nGlobal prefix",
                    "beta": "must not show\nHidden prefix"},
            "full": {"alpha": "Global prefix\nGlobal authored",
                     "beta": "Hidden prefix\nmust not show"},
        },
    }}
    result = _bar_probe(payload, {
        "prompt_id": "section", "channels": {"alpha": "fallback section"},
        "prompt": "legacy section",
    }, template)
    assert result == {
        "section": ("Authored sentence Prefix line Suffix line "
                    "other authored Beta prefix"),
        "global": "Global authored Global prefix",
    }
    assert "must not show" not in result["global"]

    # The browser must consume the exact server-published bar mirror. Subtracting
    # authored prose from `full` cannot distinguish the prefix's "cat" from the
    # authored "cat" and used to corrupt this label.
    collision = _bar_probe({"section_channel_previews": {
        "sections": {"section": {
            "authored": {"alpha": "cat"},
            "bar": {"alpha": "cat a cat person"},
            "full": {"alpha": "a cat person cat"},
        }},
        "global": None,
    }}, {"prompt_id": "section", "channels": {}}, template)
    assert collision["section"] == "cat a cat person"

    fallback = _bar_probe({}, {
        "prompt_id": "missing", "channels": {"alpha": "fallback section"},
        "prompt": "legacy section",
    }, template)
    assert fallback == {
        "section": "fallback section",
        "global": "fallback global",
    }


def test_queue_display_uses_global_channels_without_changing_scene_prompt_contract():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    queue = _method_source(widget, "_buildQueueSnapshot")
    assert "const displayScenePrompt" in queue
    assert "globalChannelLines(this.activeScene.global_channels" in queue
    assert "prompt = [displayScenePrompt.trim()" in queue
    assert "scene_prompt: scenePrompt" in queue
    assert "scene_prompt: displayScenePrompt" not in queue
