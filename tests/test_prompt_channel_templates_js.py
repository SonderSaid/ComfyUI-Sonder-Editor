"""JS<->Python parity for the prompt channel-template catalog (plan P1).

`web/js/prompt_channel_templates.js` is the panel's authoring mirror of
`server/prompt_channel_templates.py`. A drift in channel keys, labels or
separators would let the panel author into a channel the composer never emits,
which is silent and invisible until a generation comes back wrong.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import prompt_channel_templates as pct


ROOT = Path(__file__).resolve().parents[1]
MODULE_URL = (ROOT / "web" / "js" / "prompt_channel_templates.js").as_uri()


def _node_json(expression):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the channel-template parity tests")
    script = (
        f"const mod = await import({json.dumps(MODULE_URL)});\n"
        f"console.log(JSON.stringify({expression}));\n"
    )
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)


def _python_catalog():
    return {
        template_id: {
            "id": template["id"],
            "channels": [
                {"key": channel["key"], "label": channel["label"],
                 "description": channel["description"]}
                for channel in template["channels"]
            ],
            "field_separator": template["field_separator"],
            "label_separator": template["label_separator"],
            "labels": template["labels"],
            "shot_marker_channel": template["shot_marker_channel"],
            "global_merge": template["global_merge"],
            "global_channels_enabled": template["global_channels_enabled"],
            "name": template["name"],
        }
        for template_id, template in pct.PROMPT_CHANNEL_TEMPLATE_PRESETS.items()
    }


def test_preset_catalog_matches_between_python_and_javascript():
    expected = _python_catalog()
    actual = _node_json(
        "Object.fromEntries(Object.entries(mod.PROMPT_CHANNEL_TEMPLATE_PRESETS)"
        ".map(([id, t]) => [id, {"
        "id: t.id,"
        " channels: t.channels.map((c) => ({key: c.key, label: c.label,"
        " description: c.description})),"
        " field_separator: t.field_separator,"
        " label_separator: t.label_separator,"
        " labels: t.labels,"
        " shot_marker_channel: t.shot_marker_channel,"
        " global_merge: t.global_merge,"
        " global_channels_enabled: t.global_channels_enabled,"
        " name: t.name}]))"
    )
    assert actual == expected
    # Anti-vacuity: an empty catalog on both sides would compare equal.
    assert len(expected) >= 4
    assert sum(len(entry["channels"]) for entry in expected.values()) >= 13


def test_default_preset_display_name_is_not_the_product_name():
    # The id stays `sonder` for stored projects and frozen jobs; only the label
    # moves, because this channel split is not ours to claim.
    assert pct.PROMPT_CHANNEL_TEMPLATE_PRESETS["sonder"]["name"] == "Visual + Speech + Sound"
    assert pct.DEFAULT_CHANNEL_TEMPLATE_ID == "sonder"


_GLOBAL_FLAG_CASES = [
    {"id": "custom:on", "channels": [{"key": "a", "label": "a"}],
     "global_merge": "per_channel"},
    {"id": "custom:off", "channels": [{"key": "a", "label": "a"},
                                      {"key": "b", "label": "b"}],
     "global_merge": "per_channel", "global_channels_enabled": False},
]


def test_global_channel_flag_matches_between_python_and_javascript():
    expected = [
        [pct.global_channels_enabled(pct.normalize_channel_template(case)),
         pct.effective_global_merge(pct.normalize_channel_template(case)),
         list(pct.global_channel_keys(pct.normalize_channel_template(case)))]
        for case in _GLOBAL_FLAG_CASES
    ]
    actual = _node_json(
        f"{json.dumps(_GLOBAL_FLAG_CASES)}.map((raw) => {{"
        " const t = mod.normalizeChannelTemplate(raw);"
        " return [mod.globalChannelsEnabled(t), mod.effectiveGlobalMerge(t),"
        " mod.globalChannelKeys(t)]; })"
    )
    assert actual == expected
    # Absent means on; explicitly off forces `leading` and one global key.
    assert expected[0] == [True, "per_channel", ["a"]]
    assert expected[1] == [False, "leading", ["a"]]


_UNKNOWN_TEMPLATE_CASES = [
    "minimax_h3_ref", "minimax_h3_base", "sonder", "standard",
    "no-such-template", "", None,
]


def test_template_resolution_matches_between_python_and_javascript():
    expected = [list(pct.template_channel_keys(pct.get_channel_template(case)))
                for case in _UNKNOWN_TEMPLATE_CASES]
    actual = _node_json(
        f"{json.dumps(_UNKNOWN_TEMPLATE_CASES)}"
        ".map((id) => mod.templateChannelKeys(mod.getChannelTemplate(id)))"
    )
    assert actual == expected
    assert expected[0] != expected[2], "expected distinct templates in the fixtures"


_LABEL_CASES = [["minimax_h3_ref", False], ["minimax_h3_ref", True],
                ["standard", True], ["standard", False],
                ["sonder", True], ["sonder", False]]


def test_label_policy_matches_between_python_and_javascript():
    expected = [pct.template_labels_on(pct.get_channel_template(template_id), project_on)
                for template_id, project_on in _LABEL_CASES]
    actual = _node_json(
        f"{json.dumps(_LABEL_CASES)}"
        ".map(([id, on]) => mod.templateLabelsOn(mod.getChannelTemplate(id), on))"
    )
    assert actual == expected
    assert set(expected) == {True, False}, "fixtures must exercise both outcomes"


_CUSTOM_TEMPLATE_CASES = [
    {"id": "custom:a", "name": "A",
     "channels": [{"key": "one", "label": "one"}, {"key": "", "label": "x"},
                  {"key": "one", "label": "dupe"}, {"key": "two", "label": "two"}]},
    {"id": "custom:b", "channels": [], "labels": "always"},
    {"id": "custom:c", "channels": [{"key": "k", "label": "K"}],
     "shot_marker_channel": "missing", "labels": "sometimes",
     "global_merge": "somehow"},
    {"id": "custom:d", "channels": [{"key": "k", "label": "K"}],
     "field_separator": "\n\n", "label_separator": ":\n",
     "shot_marker_channel": "k"},
    None,
    "not a template",
]


def test_custom_template_normalization_matches_between_python_and_javascript():
    def normalized(raw):
        template = pct.normalize_channel_template(raw)
        return {
            "id": template["id"],
            "keys": list(pct.template_channel_keys(template)),
            "labels": template["labels"],
            "shot_marker_channel": template["shot_marker_channel"],
            "global_merge": template["global_merge"],
            "field_separator": template["field_separator"],
            "label_separator": template["label_separator"],
            "builtin": template["builtin"],
        }

    expected = [normalized(case) for case in _CUSTOM_TEMPLATE_CASES]
    actual = _node_json(
        f"{json.dumps(_CUSTOM_TEMPLATE_CASES)}.map((raw) => {{"
        " const t = mod.normalizeChannelTemplate(raw);"
        " return {id: t.id, keys: mod.templateChannelKeys(t), labels: t.labels,"
        " shot_marker_channel: t.shot_marker_channel, global_merge: t.global_merge,"
        " field_separator: t.field_separator, label_separator: t.label_separator,"
        " builtin: t.builtin};"
        "})"
    )
    assert actual == expected
    # Anti-vacuity: the fixtures must exercise both the fallback and a real fork.
    assert any(entry["builtin"] for entry in expected)
    assert any(not entry["builtin"] for entry in expected)


_NORMALIZER_CASES = [
    # [raw channels, legacy prompt, template keys or null]
    [{"visual": "v"}, "", None],
    [None, "flat", None],
    [{"visual": None, "speech": "s"}, "", None],
    # Unknown-key preservation: the six-channel dict keeps every authored key
    # AND gains the legacy three, which is the no-truncation contract.
    [{"detailed_description": "d", "summary": "s"}, "", None],
    [{"detailed_description": "d"}, "", ["subject_definitions", "detailed_description"]],
    [None, "flat", ["detailed_description", "summary"]],
    [{}, "", None],
    [{"visual": "v", "extra": "kept"}, "", ["visual"]],
]


def test_channel_normalizer_matches_between_python_and_javascript():
    from server import prompt_payload as pp

    expected = [pp.normalize_channels(raw, legacy, keys)
                for raw, legacy, keys in _NORMALIZER_CASES]
    actual = _node_json(
        f"await (async () => {{"
        f" const c = await import({json.dumps((ROOT / 'web' / 'js' / 'prompt_composition.js').as_uri())});"
        f" return {json.dumps(_NORMALIZER_CASES)}"
        f".map(([raw, legacy, keys]) => c.normalizeChannels(raw, legacy, keys));"
        f"}})()"
    )
    assert actual == expected
    # Anti-vacuity: the fixtures must actually exercise preservation.
    assert "detailed_description" in expected[3] and "visual" in expected[3]
    assert expected[7] == {"visual": "v", "extra": "kept"}


_TIMECODE_CASES = [[0, 24.0], [240, 24.0], [84, 24.0], [1, 23.976],
                   [24 * 60 * 75, 24.0], [24, 0], [-1, 24.0], [30, 30.0]]


def test_timecode_matches_between_python_and_javascript():
    expected = [pct.format_shot_timecode(frames, fps) for frames, fps in _TIMECODE_CASES]
    actual = _node_json(
        f"{json.dumps(_TIMECODE_CASES)}.map(([f, r]) => mod.formatShotTimecode(f, r))"
    )
    assert actual == expected
    assert expected[:4] == ["00:00.000", "00:10.000", "00:03.500", "00:00.042"]
