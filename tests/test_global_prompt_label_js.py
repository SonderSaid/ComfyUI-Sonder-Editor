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
    assert 'globalChannelLines(scene.global_channels, this._channelTemplate(), false).join(" ")' in widget
    assert "host._promptGlobalBarLabel" in canvas

    # The hover panel is multi-line and labelled, matching its section branch —
    # a flat blob above labelled sections would read as a different kind of
    # content rather than the same content scoped scene-wide.
    hover = widget.split("_showPromptHoverPreview(hit, clientX, clientY) {", 1)[1]
    hover = hover.split("\n    _hidePromptHoverPreview", 1)[0]
    assert "globalChannelLines(this.activeScene?.global_channels," in hover
    assert "this._channelTemplate(), labelsOn)" in hover
    assert '"(empty global prompt)"' in hover
