import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script_body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    visibility_url = (ROOT / "web" / "js" / "widget_visibility.js").as_uri()
    shape_url = (ROOT / "web" / "js" / "metadata_collector_shape.js").as_uri()
    script = f"""
const visibility = await import({json.dumps(visibility_url)});
const shape = await import({json.dumps(shape_url)});
{script_body}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_widget_visibility_round_trips_properties_and_batches_once():
    result = _run_node(
        """
const inheritedComputeSize = () => [9, 9];
const prototype = { computeSize: inheritedComputeSize };
const widget = Object.assign(Object.create(prototype), {
    name: "state",
    value: "keep",
    options: { untouched: 7 },
});
const optionsRef = widget.options;

let widgets = [widget];
let widgetRefreshes = 0;
let resizes = 0;
let dirties = 0;
const node = {
    get widgets() { return widgets; },
    set widgets(value) { widgetRefreshes += 1; widgets = value; },
    computeSize() { return [100, 200]; },
    setSize(value) { resizes += 1; this.size = value; },
    setDirtyCanvas() { dirties += 1; },
};

const firstHide = visibility.setWidgetHidden(widget, true);
const secondHide = visibility.setWidgetHidden(widget, true);
const hiddenState = {
    effective: visibility.isWidgetEffectivelyHidden(widget),
    option: widget.options.hidden,
    legacy: widget.hidden,
    collapsed: widget.computeSize(),
    value: widget.value,
    optionsIdentity: widget.options === optionsRef,
    untouched: widget.options.untouched,
};
visibility.commitWidgetVisibility(node, { resize: true });
const shown = visibility.setWidgetHidden(widget, false);
visibility.commitWidgetVisibility(node);

console.log(JSON.stringify({
    firstHide,
    secondHide,
    hiddenState,
    shown,
    restored: {
        effective: visibility.isWidgetEffectivelyHidden(widget),
        hasOwnHidden: Object.hasOwn(widget, "hidden"),
        hasOwnComputeSize: Object.hasOwn(widget, "computeSize"),
        inheritedComputeRestored: widget.computeSize === inheritedComputeSize,
        optionsIdentity: widget.options === optionsRef,
        hasOptionHidden: Object.hasOwn(widget.options, "hidden"),
        value: widget.value,
    },
    widgetRefreshes,
    resizes,
    dirties,
    size: node.size,
}));
"""
    )

    assert result == {
        "firstHide": True,
        "secondHide": False,
        "hiddenState": {
            "effective": True,
            "option": True,
            "legacy": True,
            "collapsed": [0, -4],
            "value": "keep",
            "optionsIdentity": True,
            "untouched": 7,
        },
        "shown": True,
        "restored": {
            "effective": False,
            "hasOwnHidden": False,
            "hasOwnComputeSize": False,
            "inheritedComputeRestored": True,
            "optionsIdentity": True,
            "hasOptionHidden": False,
            "value": "keep",
        },
        "widgetRefreshes": 2,
        "resizes": 1,
        "dirties": 2,
        "size": [100, 200],
    }


def test_widget_visibility_preserves_preexisting_hidden_state_and_absent_options():
    result = _run_node(
        """
const preHidden = { options: { hidden: true }, hidden: false };
const absent = {};
visibility.setWidgetHidden(preHidden, true);
visibility.setWidgetHidden(preHidden, false);
visibility.setWidgetHidden(absent, true);
visibility.setWidgetHidden(absent, false);

console.log(JSON.stringify({
    preHidden: {
        option: preHidden.options.hidden,
        legacy: preHidden.hidden,
        hasComputeSize: Object.hasOwn(preHidden, "computeSize"),
    },
    absent: {
        hasOptions: Object.hasOwn(absent, "options"),
        hasHidden: Object.hasOwn(absent, "hidden"),
        hasComputeSize: Object.hasOwn(absent, "computeSize"),
    },
}));
"""
    )

    assert result == {
        "preHidden": {
            "option": True,
            "legacy": False,
            "hasComputeSize": False,
        },
        "absent": {
            "hasOptions": False,
            "hasHidden": False,
            "hasComputeSize": False,
        },
    }


def test_widget_visibility_does_not_mutate_inherited_options():
    result = _run_node(
        """
const inheritedOptions = { hidden: false, inheritedSetting: 4 };
const widget = Object.create({ options: inheritedOptions });
visibility.setWidgetHidden(widget, true);
const whileHidden = {
    hasOwnOptions: Object.hasOwn(widget, "options"),
    inheritedUntouched: inheritedOptions.hidden,
    optionHidden: widget.options.hidden,
    inheritedSetting: widget.options.inheritedSetting,
};
visibility.setWidgetHidden(widget, false);

console.log(JSON.stringify({
    whileHidden,
    restored: {
        hasOwnOptions: Object.hasOwn(widget, "options"),
        optionsIdentity: widget.options === inheritedOptions,
        hidden: widget.options.hidden,
        inheritedSetting: widget.options.inheritedSetting,
    },
}));
"""
    )

    assert result == {
        "whileHidden": {
            "hasOwnOptions": True,
            "inheritedUntouched": False,
            "optionHidden": True,
            "inheritedSetting": 4,
        },
        "restored": {
            "hasOwnOptions": False,
            "optionsIdentity": True,
            "hidden": False,
            "inheritedSetting": 4,
        },
    }


def test_widget_visibility_restores_exact_own_descriptors():
    result = _run_node(
        """
const originalCompute = () => [8, 6];
const options = {};
Object.defineProperty(options, "hidden", {
    value: "original-option",
    writable: false,
    enumerable: false,
    configurable: true,
});
const widget = { options };
Object.defineProperty(widget, "hidden", {
    value: "original-hidden",
    writable: false,
    enumerable: false,
    configurable: true,
});
Object.defineProperty(widget, "computeSize", {
    value: originalCompute,
    writable: false,
    enumerable: false,
    configurable: true,
});
const before = {
    options: Object.getOwnPropertyDescriptor(options, "hidden"),
    hidden: Object.getOwnPropertyDescriptor(widget, "hidden"),
    computeSize: Object.getOwnPropertyDescriptor(widget, "computeSize"),
};
visibility.setWidgetHidden(widget, true);
visibility.setWidgetHidden(widget, false);
const after = {
    options: Object.getOwnPropertyDescriptor(options, "hidden"),
    hidden: Object.getOwnPropertyDescriptor(widget, "hidden"),
    computeSize: Object.getOwnPropertyDescriptor(widget, "computeSize"),
};

console.log(JSON.stringify({
    sameOptionsDescriptor:
        before.options.value === after.options.value
        && before.options.writable === after.options.writable
        && before.options.enumerable === after.options.enumerable
        && before.options.configurable === after.options.configurable,
    sameHiddenDescriptor:
        before.hidden.value === after.hidden.value
        && before.hidden.writable === after.hidden.writable
        && before.hidden.enumerable === after.hidden.enumerable
        && before.hidden.configurable === after.hidden.configurable,
    sameComputeDescriptor:
        before.computeSize.value === after.computeSize.value
        && before.computeSize.writable === after.computeSize.writable
        && before.computeSize.enumerable === after.computeSize.enumerable
        && before.computeSize.configurable === after.computeSize.configurable,
    optionsIdentity: widget.options === options,
}));
"""
    )

    assert result == {
        "sameOptionsDescriptor": True,
        "sameHiddenDescriptor": True,
        "sameComputeDescriptor": True,
        "optionsIdentity": True,
    }


def test_metadata_collector_shape_supports_legacy_and_v3_slots():
    result = _run_node(
        """
const legacy = {
    inputs: [{ name: "project" }, { name: "value_0" }, { name: "value_3" }],
    widgets: Array.from({ length: 12 }, (_, index) => ({ name: `label_${index}` })),
};
const v3 = {
    inputs: [{ name: "project" }, { name: "values.value_0" }, { name: "values.value_7" }],
    widgets: Array.from({ length: 32 }, (_, index) => ({ name: `label_${index}` })),
};
console.log(JSON.stringify({
    parsed: [
        shape.collectorValueIndex("value_2"),
        shape.collectorValueIndex("values.value_9"),
        shape.collectorValueIndex("label_2"),
    ],
    legacyCapacity: shape.collectorCapacity(legacy),
    legacyVisible: shape.visibleCollectorValueCount(legacy),
    v3Capacity: shape.collectorCapacity(v3),
    v3Visible: shape.visibleCollectorValueCount(v3),
    clamped: shape.visibleCollectorValueCount(v3, 4),
}));
"""
    )

    assert result == {
        "parsed": [2, 9, -1],
        "legacyCapacity": 12,
        "legacyVisible": 4,
        "v3Capacity": 32,
        "v3Visible": 8,
        "clamped": 4,
    }


def test_visibility_consumers_share_the_same_primitive():
    extension = (ROOT / "web" / "js" / "extension.js").read_text(encoding="utf-8")
    bridges = (ROOT / "web" / "js" / "bridge_nodes.js").read_text(encoding="utf-8")
    collector = (ROOT / "web" / "js" / "metadata_collector.js").read_text(encoding="utf-8")

    for source in (extension, bridges, collector):
        assert 'from "./widget_visibility.js"' in source
        assert "widget.computeSize = () => [0, -4]" not in source

    assert "commitWidgetVisibility(node);" in extension
    assert "commitWidgetVisibility(node, { dirty: false })" in extension
    assert "commitWidgetVisibility(node, { resize: false })" in bridges
    assert "resize: false, dirty: false" in collector
    assert 'const V3_TARGET = "SonderMetadataCollectorV3";' in collector
