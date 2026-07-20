import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_cache_setting_resolution_and_direct_widget_application():
    root = Path(__file__).resolve().parents[1]
    settings_url = (root / "web" / "js" / "editor_settings.js").as_uri()
    activation_url = (root / "web" / "js" / "render_cache_activation.js").as_uri()
    script = f"""
const values = new Map();
const storageHandlers = [];
globalThis.window = {{
  localStorage: {{
    getItem: (key) => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
  }},
  addEventListener: (name, handler) => {{ if (name === "storage") storageHandlers.push(handler); }},
}};
const settings = await import({json.dumps(settings_url)});
const activation = await import({json.dumps(activation_url)});
const resolveCases = [
  settings.isRenderCacheEnabled({{ render: {{ maxRenderCacheEntries: 0 }} }}),
  settings.isRenderCacheEnabled({{ render: {{ maxRenderCacheEntries: 1 }} }}),
  settings.isRenderCacheEnabled({{ render: {{ maxRenderCacheEntries: null }} }}),
  settings.isRenderCacheEnabled({{ render: {{ maxRenderCacheEntries: "bad" }} }}),
  settings.isRenderCacheEnabled({{}}),
];
let callbacks = 0;
const widget = {{ name: "render_cache_enabled", value: true, callback: () => callbacks++ }};
const node = {{ type: "SonderEditor", widgets: [widget] }};
const staleTrueChanged = activation.applyRenderCacheSettingToNode(
  node, {{ render: {{ maxRenderCacheEntries: 0 }} }}
);
const staleTrueValue = widget.value;
const staleFalseChanged = activation.applyRenderCacheSettingToNode(
  node, {{ render: {{ maxRenderCacheEntries: null }} }}
);
const staleFalseValue = widget.value;
const noChange = activation.applyRenderCacheSettingToNode(
  node, {{ render: {{ maxRenderCacheEntries: null }} }}
);
const existing = {{ type: "SonderEditor", widgets: [{{ name: "render_cache_enabled", value: false }}] }};
const unrelated = {{ type: "Other", widgets: [{{ name: "render_cache_enabled", value: false }}] }};
const changedCount = activation.applyRenderCacheSettingToNodes(
  [existing, unrelated], {{ render: {{ maxRenderCacheEntries: 3 }} }}
);
const notifications = [];
settings.subscribeEditorSettings((snapshot) => {{
  notifications.push(snapshot.render.maxRenderCacheEntries);
  activation.applyRenderCacheSettingToNode(node, snapshot);
}});
settings.updateEditorSettings({{ render: {{ maxRenderCacheEntries: 0 }} }});
const sameWindowValue = widget.value;
values.set("sonder-editor-settings", JSON.stringify({{ render: {{ maxRenderCacheEntries: null }} }}));
storageHandlers[0]({{ key: "sonder-editor-settings" }});
const crossWindowValue = widget.value;
console.log(JSON.stringify({{
  resolveCases, staleTrueChanged, staleTrueValue, staleFalseChanged, staleFalseValue,
  noChange, callbacks, changedCount, existingValue: existing.widgets[0].value,
  unrelatedValue: unrelated.widgets[0].value, sameWindowValue, crossWindowValue,
  notifications,
}}));
"""
    result = _run_node(script)
    assert result["resolveCases"] == [False, True, True, False, False]
    assert result["staleTrueChanged"] is True
    assert result["staleTrueValue"] is False
    assert result["staleFalseChanged"] is True
    assert result["staleFalseValue"] is True
    assert result["noChange"] is False
    assert result["callbacks"] == 0
    assert result["changedCount"] == 1
    assert result["existingValue"] is True
    assert result["unrelatedValue"] is False
    assert result["sameWindowValue"] is False
    assert result["crossWindowValue"] is True
    assert result["notifications"] == [0, None]


def test_canvas_host_owns_cache_widget_lifecycle_without_relay():
    root = Path(__file__).resolve().parents[1]
    extension = (root / "web" / "js" / "extension.js").read_text(encoding="utf-8")
    editor_widget = (root / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    controller = (root / "web" / "js" / "editor_node_controller.js").read_text(encoding="utf-8")

    assert 'applyRenderCacheSettingToNode(node, getEditorSettings());' in extension
    configure = re.search(
        r"node\.onConfigure\s*=\s*function \(info\) \{(.*?)\n\s*\};",
        extension,
        re.DOTALL,
    )
    assert configure
    body = configure.group(1)
    assert body.index("origNodeOnConfigure?.apply") < body.index("applyRenderCacheSettingToNode")
    assert "setTimeout" not in body[body.index("origNodeOnConfigure?.apply"):body.index("applyRenderCacheSettingToNode")]
    assert "syncRenderCacheSettingToCanvas();" in extension
    assert re.search(r"subscribeEditorSettings\(\(settings\)\s*=>\s*\{.*syncRenderCacheSettingToCanvas\(settings\)", extension, re.DOTALL)
    assert '_setWidgetValue("render_cache_enabled"' not in editor_widget

    fields_match = re.search(r"EDITOR_WIDGET_FIELDS\s*=\s*\[(.*?)\]", controller, re.DOTALL)
    assert fields_match
    fields = re.findall(r'"([^"]+)"', fields_match.group(1))
    assert len(fields) == 13
    assert "render_cache_enabled" not in fields
