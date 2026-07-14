import json
import shutil
import subprocess
from pathlib import Path

import pytest


def run_node_helper(script_body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")

    module_url = (Path(__file__).resolve().parents[1] / "web" / "js" / "shared_render_queue.js").as_uri()
    script = f"""
class FakeElement {{
    constructor(tagName) {{
        this.tagName = tagName;
        this.style = {{ cssText: "" }};
        this.dataset = {{}};
        this.children = [];
        this.parentNode = null;
        this.listeners = new Map();
        this.listenerOptions = new Map();
        this.attributes = new Map();
        this._innerHTML = "";
    }}

    append(...children) {{
        for (const child of children) this.appendChild(child);
    }}

    appendChild(child) {{
        child.parentNode = this;
        this.children.push(child);
        return child;
    }}

    addEventListener(type, listener, options) {{
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
        const optionList = this.listenerOptions.get(type) || [];
        optionList.push(options);
        this.listenerOptions.set(type, optionList);
    }}

    removeEventListener(type, listener) {{
        const listeners = this.listeners.get(type) || [];
        this.listeners.set(type, listeners.filter((candidate) => candidate !== listener));
    }}

    dispatch(type, event) {{
        for (const listener of [...(this.listeners.get(type) || [])]) listener(event);
    }}

    listenerCount(type) {{
        return (this.listeners.get(type) || []).length;
    }}

    setAttribute(name, value) {{
        this.attributes.set(name, String(value));
    }}

    remove() {{
        if (!this.parentNode) return;
        this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
        this.parentNode = null;
    }}

    set innerHTML(value) {{
        this._innerHTML = String(value);
        this.children = [];
    }}

    get innerHTML() {{
        return this._innerHTML;
    }}
}}

globalThis.document = {{
    createElement(tagName) {{
        return new FakeElement(tagName);
    }},
}};

const mod = await import({json.dumps(module_url)});
{script_body}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_dormant_queue_owns_wheel_without_canceling_native_scroll():
    result = run_node_helper(
        """
function makeWheelEvent() {
    return {
        defaultPrevented: false,
        propagationStopped: false,
        immediatePropagationStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.propagationStopped = true; },
        stopImmediatePropagation() { this.immediatePropagationStopped = true; },
    };
}

function mount(surface) {
    const container = new FakeElement("section");
    const consumeCalls = [];
    const handle = mod.mountSharedRenderQueue(container, {
        surface,
        jobs: [],
        consumePointer(event, options = {}) {
            consumeCalls.push({ preventDefault: options.preventDefault === true });
            if (options.preventDefault) event.preventDefault();
            event.stopPropagation();
            event.stopImmediatePropagation();
        },
    });
    return { container, consumeCalls, handle };
}

const dormant = mount("dormant");
const dormantEvent = makeWheelEvent();
dormant.handle.root.dispatch("wheel", dormantEvent);

const fullscreen = mount("fullscreen");
const fullscreenEvent = makeWheelEvent();
fullscreen.handle.root.dispatch("wheel", fullscreenEvent);

const dormantRoot = dormant.handle.root;
const fullscreenRoot = fullscreen.handle.root;
const dormantPassive = dormantRoot.listenerOptions.get("wheel")?.[0]?.passive === true;
dormant.handle.destroy();
fullscreen.handle.destroy();

console.log(JSON.stringify({
    dormantCss: dormantRoot.style.cssText,
    fullscreenCss: fullscreenRoot.style.cssText,
    dormantPassive,
    dormantConsumeCalls: dormant.consumeCalls,
    dormantEvent,
    fullscreenConsumeCalls: fullscreen.consumeCalls,
    fullscreenEvent,
    dormantListenersAfterDestroy: dormantRoot.listenerCount("wheel"),
    fullscreenListenersAfterDestroy: fullscreenRoot.listenerCount("wheel"),
    dormantRemoved: dormant.container.children.length === 0 && dormantRoot.parentNode === null,
    fullscreenRemoved: fullscreen.container.children.length === 0 && fullscreenRoot.parentNode === null,
}));
"""
    )

    assert "overflow-y: auto" in result["dormantCss"]
    assert "overscroll-behavior-y: contain" in result["dormantCss"]
    assert "overscroll-behavior-y" not in result["fullscreenCss"]
    assert result["dormantPassive"] is True
    assert result["dormantConsumeCalls"] == [{"preventDefault": False}]
    assert result["dormantEvent"] == {
        "defaultPrevented": False,
        "propagationStopped": True,
        "immediatePropagationStopped": True,
    }
    assert result["fullscreenConsumeCalls"] == []
    assert result["fullscreenEvent"] == {
        "defaultPrevented": False,
        "propagationStopped": False,
        "immediatePropagationStopped": False,
    }
    assert result["dormantListenersAfterDestroy"] == 0
    assert result["fullscreenListenersAfterDestroy"] == 0
    assert result["dormantRemoved"] is True
    assert result["fullscreenRemoved"] is True
