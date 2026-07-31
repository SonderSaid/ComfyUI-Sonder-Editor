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

    module_url = (ROOT / "web" / "js" / "editor_node_surface.js").as_uri()
    script = f"""
class FakeElement extends EventTarget {{
    constructor(tag) {{
        super();
        this.tagName = String(tag).toUpperCase();
        this.style = {{}};
        this.children = [];
        this.attributes = new Map();
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.removed = false;
    }}
    append(...children) {{ for (const child of children) this.appendChild(child); }}
    appendChild(child) {{ this.children.push(child); child.parentNode = this; return child; }}
    remove() {{ this.removed = true; }}
    setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
    getAttribute(name) {{ return this.attributes.get(name) ?? null; }}
}}
globalThis.document = {{
    createElement: (tag) => new FakeElement(tag),
}};
const surfaceModule = await import({json.dumps(module_url)});
{script_body}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_editor_surface_is_single_size_contained_mode_aware_dom_owner():
    result = _run_node(
        """
const controller = new FakeElement("section");
const surface = surfaceModule.createEditorNodeSurface({
    controllerElement: controller,
});
const initial = {
    childCount: surface.element.children.length,
    controllerVisible: !controller.hidden && controller.style.display !== "none",
    createHidden: surface.createRow.hidden,
    minHeight: surface.getMinHeight(),
};
const createChanged = surface.setCreateMode(true);
const create = {
    createChanged,
    controllerHidden: controller.hidden && controller.style.display === "none",
    createVisible: !surface.createRow.hidden && surface.createRow.style.display === "flex",
    minHeight: surface.getMinHeight(),
};
const repeatedChanged = surface.setCreateMode(true);
const existingChanged = surface.setCreateMode(false);
const existing = {
    existingChanged,
    repeatedChanged,
    controllerVisible: !controller.hidden && controller.style.display === "",
    createHidden: surface.createRow.hidden && surface.createRow.style.display === "none",
    minHeight: surface.getMinHeight(),
};
console.log(JSON.stringify({
    initial,
    create,
    existing,
    css: surface.element.style.cssText,
}));
"""
    )

    assert result["initial"] == {
        "childCount": 2,
        "controllerVisible": True,
        "createHidden": True,
        "minHeight": 150,
    }
    assert result["create"] == {
        "createChanged": True,
        "controllerHidden": True,
        "createVisible": True,
        "minHeight": 56,
    }
    assert result["existing"] == {
        "existingChanged": True,
        "repeatedChanged": False,
        "controllerVisible": True,
        "createHidden": True,
        "minHeight": 150,
    }
    compact_css = result["css"].replace(" ", "")
    assert "contain:size;" in compact_css
    assert "contain:sizelayout" not in compact_css
    assert "min-width:0;" in compact_css
    assert "min-height:0;" in compact_css
    assert "overflow:hidden;" in compact_css


def test_create_action_is_single_flight_pointer_safe_and_latest_intent_guarded():
    result = _run_node(
        """
const controller = new FakeElement("section");
const events = [];
let resolveCreate;
const gate = new Promise((resolve) => { resolveCreate = resolve; });
let capturedIsCurrent = null;
const surface = surfaceModule.createEditorNodeSurface({
    controllerElement: controller,
    onCreate: async (isCurrent) => {
        capturedIsCurrent = isCurrent;
        events.push(`start:${isCurrent()}`);
        await gate;
        events.push(`finish:${isCurrent()}`);
    },
    onError: (error) => events.push(`error:${error.message}`),
});
surface.setCreateMode(true);

let stopped = 0;
const pointerEvent = new Event("pointerdown");
pointerEvent.stopPropagation = () => { stopped += 1; };
surface.createRow.dispatchEvent(pointerEvent);

const first = surface.runCreate();
const duplicate = await surface.runCreate();
const whilePending = {
    pending: surface.isCreatePending,
    disabled: surface.createButton.disabled,
    label: surface.createButton.textContent,
    ariaBusy: surface.createButton.getAttribute("aria-busy"),
};
surface.setCreateMode(false);
const staleAfterModeChange = capturedIsCurrent();
resolveCreate();
const firstResult = await first;
const after = {
    pending: surface.isCreatePending,
    disabled: surface.createButton.disabled,
    label: surface.createButton.textContent,
    ariaBusy: surface.createButton.getAttribute("aria-busy"),
};
surface.destroy();

console.log(JSON.stringify({
    stopped,
    duplicate,
    whilePending,
    staleAfterModeChange,
    firstResult,
    after,
    removed: surface.element.removed,
    events,
}));
"""
    )

    assert result == {
        "stopped": 1,
        "duplicate": False,
        "whilePending": {
            "pending": True,
            "disabled": True,
            "label": "Creating...",
            "ariaBusy": "true",
        },
        "staleAfterModeChange": False,
        "firstResult": False,
        "after": {
            "pending": False,
            "disabled": False,
            "label": "Create",
            "ariaBusy": "false",
        },
        "removed": True,
        "events": ["start:true", "finish:false"],
    }


def test_create_action_contains_current_failure_and_ignores_activation_after_destroy():
    result = _run_node(
        """
const controller = new FakeElement("section");
const errors = [];
let calls = 0;
const surface = surfaceModule.createEditorNodeSurface({
    controllerElement: controller,
    onCreate: async () => {
        calls += 1;
        throw new Error("creation failed");
    },
    onError: (error) => errors.push(error.message),
});
surface.setCreateMode(true);
const failed = await surface.runCreate();
surface.destroy();
const afterDestroy = await surface.runCreate();
console.log(JSON.stringify({
    failed,
    afterDestroy,
    calls,
    errors,
    pending: surface.isCreatePending,
}));
"""
    )

    assert result == {
        "failed": False,
        "afterDestroy": False,
        "calls": 1,
        "errors": ["creation failed"],
        "pending": False,
    }


def test_extension_uses_one_dom_surface_and_no_native_create_widget_or_height_override():
    source = (ROOT / "web" / "js" / "extension.js").read_text(encoding="utf-8")

    assert 'createEditorNodeSurface({' in source
    assert 'addDOMWidget("sonder_editor_ui"' in source
    assert 'addWidget("button", "Create"' not in source
    assert "setWidgetVisible(editorDOMWidget" not in source
    assert "editorDOMWidget.computeSize" not in source
    assert "getMaxHeight: () => controller.getHeight()" not in source
    assert "getHeight: () => controller.getHeight()" not in source
