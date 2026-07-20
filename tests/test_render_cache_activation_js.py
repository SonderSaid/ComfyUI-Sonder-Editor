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
values.set("sonder-editor-settings", JSON.stringify({{ render: {{ maxRenderCacheEntries: 25 }} }}));
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
  settings.renderCacheMaxBytes({{ render: {{ maxRenderCacheSizeBytes: 0 }} }}),
  settings.renderCacheMaxBytes({{ render: {{ maxRenderCacheSizeBytes: 5000000000 }} }}),
  settings.renderCacheMaxBytes({{ render: {{ maxRenderCacheSizeBytes: null }} }}),
  settings.renderCacheMaxBytes({{ render: {{ maxRenderCacheSizeBytes: "bad" }} }}),
  settings.renderCacheMaxBytes({{}}),
];
let callbacks = 0;
const widget = {{ name: "render_cache_max_bytes", value: -1, callback: () => callbacks++ }};
const node = {{ type: "SonderEditor", widgets: [widget] }};
const staleTrueChanged = activation.applyRenderCacheSettingToNode(
  node, {{ render: {{ maxRenderCacheSizeBytes: 0 }} }}
);
const staleTrueValue = widget.value;
const staleFalseChanged = activation.applyRenderCacheSettingToNode(
  node, {{ render: {{ maxRenderCacheSizeBytes: null }} }}
);
const staleFalseValue = widget.value;
const noChange = activation.applyRenderCacheSettingToNode(
  node, {{ render: {{ maxRenderCacheSizeBytes: null }} }}
);
const existing = {{ type: "SonderEditor", widgets: [{{ name: "render_cache_max_bytes", value: 0 }}] }};
const unrelated = {{ type: "Other", widgets: [{{ name: "render_cache_max_bytes", value: 0 }}] }};
const changedCount = activation.applyRenderCacheSettingToNodes(
  [existing, unrelated], {{ render: {{ maxRenderCacheSizeBytes: 5000000000 }} }}
);
const notifications = [];
settings.subscribeEditorSettings((snapshot) => {{
  notifications.push(snapshot.render.maxRenderCacheSizeBytes);
  activation.applyRenderCacheSettingToNode(node, snapshot);
}});
const legacyIgnored = settings.getEditorSettings().render;
settings.updateEditorSettings({{ render: {{ maxRenderCacheSizeBytes: 25000000000 }} }});
const sameWindowValue = widget.value;
values.set("sonder-editor-settings", JSON.stringify({{ render: {{ maxRenderCacheSizeBytes: null }} }}));
storageHandlers[0]({{ key: "sonder-editor-settings" }});
const crossWindowValue = widget.value;
console.log(JSON.stringify({{
  resolveCases, staleTrueChanged, staleTrueValue, staleFalseChanged, staleFalseValue,
  noChange, callbacks, changedCount, existingValue: existing.widgets[0].value,
  unrelatedValue: unrelated.widgets[0].value, sameWindowValue, crossWindowValue,
  notifications, legacyIgnored,
}}));
"""
    result = _run_node(script)
    assert result["resolveCases"] == [0, 5_000_000_000, -1, 0, 0]
    assert result["staleTrueChanged"] is True
    assert result["staleTrueValue"] == 0
    assert result["staleFalseChanged"] is True
    assert result["staleFalseValue"] == -1
    assert result["noChange"] is False
    assert result["callbacks"] == 0
    assert result["changedCount"] == 1
    assert result["existingValue"] == 5_000_000_000
    assert result["unrelatedValue"] == 0
    assert result["sameWindowValue"] == 25_000_000_000
    assert result["crossWindowValue"] == -1
    assert result["notifications"] == [25_000_000_000, None]
    assert result["legacyIgnored"]["maxRenderCacheSizeBytes"] == 0
    assert "maxRenderCacheEntries" not in result["legacyIgnored"]


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
    assert 'render_cache_enabled' not in editor_widget
    assert 'render_cache_max_bytes' in extension

    fields_match = re.search(r"EDITOR_WIDGET_FIELDS\s*=\s*\[(.*?)\]", controller, re.DOTALL)
    assert fields_match
    fields = re.findall(r'"([^"]+)"', fields_match.group(1))
    assert len(fields) == 13
    assert "render_cache_enabled" not in fields
    assert "render_cache_max_bytes" not in fields


def test_cache_budget_settings_ui_and_sweep_contract():
    root = Path(__file__).resolve().parents[1]
    panel = (root / "web" / "js" / "editor_settings_panel.js").read_text(encoding="utf-8")
    editor_widget = (root / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    editor_node = (root / "nodes" / "editor_node.py").read_text(encoding="utf-8")

    for label in ["Off", "5 GB", "10 GB", "25 GB", "50 GB", "100 GB", "Unlimited"]:
        assert f'label: "{label}"' in panel
    assert "widespread changes in long or high-resolution scenes may still write several GB per run" in panel
    assert "The limit controls retained cache size, not per-render writes." in panel
    assert "Current Project Cache" in panel
    assert "Clear Render Cache…" in panel
    assert "This cannot be undone." in panel
    assert "Open a project to view cache usage." in panel
    assert "/cache/renders/sweep" in editor_widget
    assert 'method: "POST"' in editor_widget
    assert "max_size_bytes: maxSizeBytes" in editor_widget
    assert "this._renderCacheSweepPending" in editor_widget
    assert "queue_remaining" in editor_widget
    assert '"render_cache_max_bytes"' in editor_node
    assert "render_cache_enabled" not in editor_node
    assert "maxRenderCacheEntries" not in panel
