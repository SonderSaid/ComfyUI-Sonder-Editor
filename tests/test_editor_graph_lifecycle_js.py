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

    module_url = (ROOT / "web" / "js" / "editor_graph_lifecycle.js").as_uri()
    script = f"""
const lifecycle = await import({json.dumps(module_url)});
{script_body}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_saved_workflow_defers_defaults_and_commits_restored_state_before_hydration():
    result = _run_node(
        """
let configuring = true;
let value = "+ Create New";
const events = [];
const observations = [];

const reconciler = lifecycle.createGraphAwareReconciler({
    isConfiguringGraph: () => configuring,
    getCurrentValue: () => value,
    applySynchronousState: (project) => {
        events.push(`sync:${project}`);
        observations.push(project);
    },
    hydrate: async (project) => {
        events.push(`hydrate:${project}`);
    },
    onError: (error) => events.push(`error:${error.message}`),
});

const deferred = reconciler.request();
value = "MetadataTest";
configuring = false;

const context = { marker: "node-context" };
const argsSeen = [];
const node = {
    onAfterGraphConfigured(...args) {
        argsSeen.push({
            sameThis: this === context,
            args,
        });
        events.push("original");
        return "original-result";
    },
};
lifecycle.chainAfterGraphConfigured(node, () => {
    events.push("sonder");
    reconciler.request({ force: true });
});

const sonderCallback = node.onAfterGraphConfigured;
node.onAfterGraphConfigured = function (...args) {
    const callbackResult = sonderCallback.apply(this, args);
    events.push(`vue:${observations.at(-1)}`);
    return callbackResult;
};

const callbackResult = node.onAfterGraphConfigured.call(context, "graph", 7);
const beforeHydration = [...events];
await reconciler.pendingPromise;

console.log(JSON.stringify({
    deferredWasNull: deferred === null,
    callbackResult,
    argsSeen,
    beforeHydration,
    finalEvents: events,
}));
"""
    )

    assert result == {
        "deferredWasNull": True,
        "callbackResult": "original-result",
        "argsSeen": [
            {
                "sameThis": True,
                "args": ["graph", 7],
            }
        ],
        "beforeHydration": [
            "original",
            "sonder",
            "sync:MetadataTest",
            "vue:MetadataTest",
        ],
        "finalEvents": [
            "original",
            "sonder",
            "sync:MetadataTest",
            "vue:MetadataTest",
            "hydrate:MetadataTest",
        ],
    }


def test_widget_callback_sets_supplied_value_and_preserves_callback_contract():
    result = _run_node(
        """
const events = [];
const widget = {
    value: "Old",
    callback(...args) {
        events.push({
            kind: "original",
            sameThis: this === widget,
            currentValue: widget.value,
            args,
        });
        return "callback-result";
    },
};
lifecycle.chainWidgetCallback(widget, function (...args) {
    events.push({
        kind: "sonder",
        sameThis: this === widget,
        currentValue: widget.value,
        args,
    });
});

const result = widget.callback("New", 3);

let chainedAfterThrow = false;
const failure = new Error("original failure");
const throwingWidget = {
    value: "Before",
    callback() {
        throw failure;
    },
};
lifecycle.chainWidgetCallback(throwingWidget, () => {
    chainedAfterThrow = true;
});
let sameError = false;
try {
    throwingWidget.callback("After");
} catch (error) {
    sameError = error === failure;
}

let graphChainedAfterThrow = false;
const graphFailure = new Error("graph failure");
const graphNode = {
    onAfterGraphConfigured() {
        throw graphFailure;
    },
};
lifecycle.chainAfterGraphConfigured(graphNode, () => {
    graphChainedAfterThrow = true;
});
let sameGraphError = false;
try {
    graphNode.onAfterGraphConfigured();
} catch (error) {
    sameGraphError = error === graphFailure;
}

console.log(JSON.stringify({
    result,
    value: widget.value,
    events,
    throwingValue: throwingWidget.value,
    chainedAfterThrow,
    sameError,
    graphChainedAfterThrow,
    sameGraphError,
}));
"""
    )

    assert result == {
        "result": "callback-result",
        "value": "New",
        "events": [
            {
                "kind": "original",
                "sameThis": True,
                "currentValue": "New",
                "args": ["New", 3],
            },
            {
                "kind": "sonder",
                "sameThis": True,
                "currentValue": "New",
                "args": ["New", 3],
            },
        ],
        "throwingValue": "After",
        "chainedAfterThrow": False,
        "sameError": True,
        "graphChainedAfterThrow": False,
        "sameGraphError": True,
    }


def test_reconciler_is_immediate_latest_wins_and_contains_failures():
    result = _run_node(
        """
let value = "A";
const events = [];
const gates = {};
const gateFor = (key) => new Promise((resolve) => {
    gates[key] = resolve;
});
const gateA = gateFor("A");
const gateB = gateFor("B");

const reconciler = lifecycle.createGraphAwareReconciler({
    isConfiguringGraph: () => false,
    getCurrentValue: () => value,
    applySynchronousState: (project) => events.push(`sync:${project}`),
    hydrate: async (project, isCurrent) => {
        events.push(`start:${project}`);
        await (project === "A" ? gateA : gateB);
        if (isCurrent()) events.push(`finish:${project}`);
    },
    onError: (error) => events.push(`error:${error.message}`),
});

const promiseA = reconciler.request();
const immediatelyAfterA = [...events];
await Promise.resolve();
value = "B";
const promiseB = reconciler.request();
const immediatelyAfterB = [...events];
await Promise.resolve();
gates.B();
await promiseB;
gates.A();
await promiseA;

value = "Broken";
const failing = lifecycle.createGraphAwareReconciler({
    isConfiguringGraph: () => false,
    getCurrentValue: () => value,
    applySynchronousState: () => {
        throw new Error("sync failure");
    },
    hydrate: async () => {
        events.push("unexpected-hydration");
    },
    onError: (error) => events.push(`error:${error.message}`),
});
const failureResult = await failing.request();

const asyncFailing = lifecycle.createGraphAwareReconciler({
    isConfiguringGraph: () => false,
    getCurrentValue: () => value,
    applySynchronousState: () => events.push("async-failure-sync"),
    hydrate: async () => {
        await Promise.resolve();
        throw new Error("async failure");
    },
    onError: (error) => events.push(`error:${error.message}`),
});
const asyncFailureResult = await asyncFailing.request();

console.log(JSON.stringify({
    immediatelyAfterA,
    immediatelyAfterB,
    events,
    failureResult,
    asyncFailureResult,
}));
"""
    )

    assert result == {
        "immediatelyAfterA": ["sync:A"],
        "immediatelyAfterB": ["sync:A", "start:A", "sync:B"],
        "events": [
            "sync:A",
            "start:A",
            "sync:B",
            "start:B",
            "finish:B",
            "error:sync failure",
            "async-failure-sync",
            "error:async failure",
        ],
        "failureResult": False,
        "asyncFailureResult": False,
    }


def test_cancelled_reconciler_does_not_start_hydration():
    result = _run_node(
        """
const events = [];
const reconciler = lifecycle.createGraphAwareReconciler({
    isConfiguringGraph: () => false,
    getCurrentValue: () => "Project",
    applySynchronousState: () => events.push("sync"),
    hydrate: async () => events.push("hydrate"),
    onError: () => events.push("error"),
});

const pending = reconciler.request();
reconciler.cancel();
await pending;
console.log(JSON.stringify(events));
"""
    )

    assert result == ["sync"]


def test_extension_wires_graph_guard_without_timer_or_choice_refresh_replay():
    source = (ROOT / "web" / "js" / "extension.js").read_text(encoding="utf-8")

    assert "chainAfterGraphConfigured(node" in source
    assert "isConfiguringGraph: () => !!app.configuringGraph" in source
    assert "applySynchronousState: applyVisibilityAndSize" in source
    assert "node._sonderVisibilityPromise = promise" in source
    assert "setTimeout?.(() => node._sonderRunUpdateVisibility" not in source
    assert ".then(() => runUpdateVisibility())" not in source
