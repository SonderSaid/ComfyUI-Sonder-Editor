"""Clipboard-event ownership at the shared window-capture seam."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_paste_dispatch_priority_consumption_pass_and_lifecycle():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for keyboard ownership coverage")
    module_url = (ROOT / "web" / "js" / "keyboard_ownership.js").as_uri()
    script = f"""
        const listeners = new Map();
        globalThis.window = {{
            addEventListener(name, fn) {{ listeners.set(name, fn); }},
            removeEventListener(name, fn) {{ if (listeners.get(name) === fn) listeners.delete(name); }},
            localStorage: {{ getItem() {{ return null; }} }},
        }};
        const mod = await import({json.dumps(module_url)});
        const calls = [];
        const offLow = mod.register({{ id:"low", priority:1,
            paste() {{ calls.push("low"); return true; }} }});
        const offNoPaste = mod.register({{ id:"keys", priority:50,
            keydown() {{ return false; }} }});
        const offHigh = mod.register({{ id:"high", priority:100,
            paste() {{ calls.push("high"); return false; }} }});
        const event = {{ stopped:false, prevented:false,
            stopImmediatePropagation() {{ this.stopped = true; }},
            preventDefault() {{ this.prevented = true; }},
        }};
        listeners.get("paste")(event);
        const first = {{ calls:[...calls], stopped:event.stopped, prevented:event.prevented }};
        offHigh(); offLow(); offNoPaste();
        const detached = !listeners.has("paste") && !window.__SONDER_KEYBOARD_OWNERSHIP__.isAttached();
        const preserveCalls = [];
        const offPreserve = mod.register({{ id:"preserve", priority:1,
            paste() {{ preserveCalls.push("preserve"); return mod.PRESERVE_DEFAULT; }} }});
        const preserveEvent = {{ stopped:false, prevented:false,
            stopImmediatePropagation() {{ this.stopped = true; }},
            preventDefault() {{ this.prevented = true; }},
        }};
        listeners.get("paste")(preserveEvent);
        offPreserve();
        console.log(JSON.stringify({{ first, detached, preserveCalls,
            preserveStopped: preserveEvent.stopped,
            preservePrevented: preserveEvent.prevented }}));
    """
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True,
    ).stdout)
    assert result["first"] == {
        "calls": ["high", "low"], "stopped": True, "prevented": True}
    assert result["detached"] is True
    assert result["preserveCalls"] == ["preserve"]
    assert result["preserveStopped"] is True
    assert result["preservePrevented"] is False
