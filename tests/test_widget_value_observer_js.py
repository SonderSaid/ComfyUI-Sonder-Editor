"""`observeWidgetValue` sees every callback write and never becomes a writer."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OBSERVER = (ROOT / "web/js/widget_value_observer.js").as_uri()


def test_observer_wraps_once_and_preserves_the_callback_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the widget observer")
    script = f"""
import assert from 'node:assert/strict';
const {{ observeWidgetValue }} = await import({json.dumps(OBSERVER)});
const calls = [];
const widget = {{ name: "lane", value: 0,
  callback(...args) {{ calls.push({{ self: this, args }}); return "original-result"; }} }};
const node = {{ widgets: [widget] }};
const seen = [];
const observer = (value, w) => seen.push([value, w === widget]);
const unsubscribe = observeWidgetValue(node, "lane", observer);
const wrapped = widget.callback;
assert.equal(observeWidgetValue(node, "lane", observer) !== null, true);
assert.equal(widget.callback, wrapped, "a second observe never re-wraps");
const other = [];
observeWidgetValue(node, "lane", (value) => other.push(value));

widget.value = 3;
const receiver = {{ marker: true }};
const result = widget.callback.call(receiver, 3, "canvas", node);
assert.equal(result, "original-result");
assert.equal(calls.length, 1);
assert.equal(calls[0].self, receiver);
assert.deepEqual(calls[0].args, [3, "canvas", node]);
assert.deepEqual(seen, [[3, true]], "registered once despite two observe calls");
assert.deepEqual(other, [3]);

// A failing observer neither breaks the callback nor the other observers.
observeWidgetValue(node, "lane", () => {{ throw new Error("boom"); }});
const warn = console.warn; console.warn = () => {{}};
widget.value = 4;
assert.equal(widget.callback(4), "original-result");
console.warn = warn;
assert.deepEqual(other, [3, 4]);

unsubscribe();
widget.value = 5; widget.callback(5);
assert.deepEqual(seen, [[3, true], [4, true]], "unsubscribed");

// The observer reports the widget's value AFTER the original callback ran.
const clamp = {{ name: "c", value: 0, callback(v) {{ this.value = Math.min(v, 9); }} }};
const clampSeen = [];
observeWidgetValue({{ widgets: [clamp] }}, "c", (value) => clampSeen.push(value));
clamp.value = 12; clamp.callback(12);
assert.deepEqual(clampSeen, [9]);

// No callback at all still notifies; a missing widget or observer is refused.
const bare = {{ name: "b", value: "x" }};
const bareSeen = [];
observeWidgetValue({{ widgets: [bare] }}, "b", (value) => bareSeen.push(value));
assert.equal(bare.callback("x"), undefined);
assert.deepEqual(bareSeen, ["x"]);
assert.equal(observeWidgetValue({{ widgets: [] }}, "nope", () => {{}}), null);
assert.equal(observeWidgetValue(node, "lane", null), null);
assert.equal(JSON.stringify(widget).includes("Observers"), false, "symbol state never serializes");
console.log("ok");
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "ok"
