"""Every Sonder node the cache pipeline must know about declares a cache policy.

This test owns the canonical list (architecture.md, Execution Cache Pipeline):
it walks the pack's registered V1 and V3 nodes, as the root ``__init__.py``
publishes them to ComfyUI, and selects

- ``SonderEditor``;
- every node with an input of a project-derived type: the closure from
  ``SONDER_PROJECT`` over the ``SONDER_*`` custom types selected nodes output.
  Concrete ComfyUI types and wildcards never select a node;
- every output node;
- every node with hidden ``PROMPT``, ``UNIQUE_ID``, ``DYNPROMPT`` or
  ``EXTRA_PNGINFO``.

A selected node fails on a missing or stale output declaration, ``EXACT`` on a
non-literal type, ``INHERITS`` with no reason, ``DESCRIPTOR`` with no registered
invalidation cases, or (output and hidden-input nodes) a missing node-level
declaration or an ``ALWAYS_RUNS`` proof that does not hold.

The walk needs ComfyUI's real ``comfy_api`` to finalise V3 schemas. ComfyUI's
``nodes`` and ``execution`` modules collide with this repository's packages, so
it runs in a subprocess from the ComfyUI root (``SONDER_COMFYUI_ROOT``, or the
install whose venv runs this test), and skips without one.
"""

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "sonder_cache_policy_test_pkg"


def _comfy_root() -> Path | None:
    candidates = []
    if os.environ.get("SONDER_COMFYUI_ROOT"):
        candidates.append(Path(os.environ["SONDER_COMFYUI_ROOT"]))
    candidates.extend(Path(sys.executable).resolve().parents[:3])
    for root in candidates:
        if (root / "execution.py").is_file() and (root / "comfy_api").is_dir():
            return root
    return None


def _policy_module():
    if TEST_PACKAGE not in sys.modules:
        package = types.ModuleType(TEST_PACKAGE)
        package.__path__ = [str(REPO_ROOT)]
        sys.modules[TEST_PACKAGE] = package
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.cache_policy")


# --- Pure lookups (no ComfyUI) -----------------------------------------------------


def test_lookup_prefers_an_exact_name_over_a_pattern_and_defaults_to_inherits():
    policy = _policy_module()

    class Node:
        RETURN_NAMES = ("r01", "r02", "label")
        SONDER_CACHE_POLICY = {"r??": policy.INHERITS("media"), "r02": policy.EXACT, "label": policy.PER_RUN}

    assert policy.slot_policy(Node, 0) == policy.INHERITS("media")
    assert policy.slot_policy(Node, 1) == policy.EXACT
    assert policy.slot_policy(Node, 2) == policy.PER_RUN
    assert policy.slot_policy(Node, 3) is policy.UNDECLARED
    assert policy.slot_policy(Node, True) is policy.UNDECLARED
    assert policy.output_policy(object, "anything") is policy.UNDECLARED
    assert isinstance(policy.UNDECLARED, policy.Inherits)
    assert policy.execution_policy(Node) is None


# --- The registry walk (real comfy_api) ---------------------------------------------

SCRIPT = r'''
import importlib, json, math, sys, types
repo, comfy_root = sys.argv[1], sys.argv[2]
sys.path.insert(0, comfy_root)
import nodes  # noqa: F401  ComfyUI's, before the pack's relative imports run
import execution  # noqa: F401

package = types.ModuleType("sonder_registry_pkg")
package.__path__ = [repo]
package.__file__ = repo + "/__init__.py"
sys.modules["sonder_registry_pkg"] = package
source = open(repo + "/__init__.py", encoding="utf-8").read()
exec(compile(source, repo + "/__init__.py", "exec"), package.__dict__)
policy = importlib.import_module("sonder_registry_pkg.nodes.cache_policy")
run_scope = importlib.import_module("sonder_registry_pkg.server.run_scope")

HIDDEN_SELECTING = {"PROMPT", "UNIQUE_ID", "DYNPROMPT", "EXTRA_PNGINFO"}
mapping = dict(package.NODE_CLASS_MAPPINGS)
out = {"nodes": sorted(mapping), "selected": {}, "failures": [], "reasons": {}}


def socket_type(spec):
    value = spec[0] if isinstance(spec, (tuple, list)) else spec
    return value if isinstance(value, str) else "COMBO"


def inputs_of(cls):
    types_ = cls.INPUT_TYPES() or {}
    return {category: {name: socket_type(spec) for name, spec in (types_.get(category) or {}).items()}
            for category in ("required", "optional")}, types_.get("hidden") or {}


info = {}
for node_id, cls in mapping.items():
    inputs, hidden = inputs_of(cls)
    info[node_id] = {
        "inputs": inputs,
        "hidden": sorted(run_scope._hidden_name(value) for value in hidden.values()),
        "returns": list(getattr(cls, "RETURN_TYPES", ()) or ()),
        "names": list(getattr(cls, "RETURN_NAMES", ()) or ()),
        "output_node": bool(getattr(cls, "OUTPUT_NODE", False)),
    }

# Closure from SONDER_PROJECT over the SONDER_* types that selected nodes output.
derived, selected = {"SONDER_PROJECT"}, {"SonderEditor": "the Editor"}
changed = True
while changed:
    changed = False
    for node_id, item in info.items():
        if node_id in selected:
            continue
        taken = {kind for category in item["inputs"].values() for kind in category.values()}
        if taken & derived:
            selected[node_id] = "takes " + ",".join(sorted(taken & derived))
            changed = True
    for node_id in list(selected):
        for kind in info[node_id]["returns"]:
            if isinstance(kind, str) and kind.startswith("SONDER_") and kind not in derived:
                derived.add(kind)
                changed = True
needs_execution = set()
for node_id, item in info.items():
    if item["output_node"]:
        selected.setdefault(node_id, "output node")
        needs_execution.add(node_id)
    if set(item["hidden"]) & HIDDEN_SELECTING:
        selected.setdefault(node_id, "hidden " + ",".join(sorted(set(item["hidden"]) & HIDDEN_SELECTING)))
        needs_execution.add(node_id)
out["selected"] = selected
out["derived"] = sorted(derived)


def fail(node_id, message):
    out["failures"].append(f"{node_id}: {message}")


def valid_output_policy(value):
    return value in (policy.EXACT, policy.DESCRIPTOR, policy.PER_RUN, policy.HANDLE) or isinstance(value, policy.Inherits)


for node_id in sorted(selected):
    cls, item = mapping[node_id], info[node_id]
    declared = policy.declared_policies(cls)
    if not isinstance(getattr(cls, policy.POLICY_ATTRIBUTE, None), dict):
        fail(node_id, "declares no SONDER_CACHE_POLICY")
    names = item["names"] or [f"#{index}" for index in range(len(item["returns"]))]
    for slot, name in enumerate(names):
        value = policy.output_policy(cls, name)
        if not any(policy.declaration_matches(key, name) for key in declared):
            fail(node_id, f"output {name} has no cache policy")
            continue
        if not valid_output_policy(value):
            fail(node_id, f"output {name} has an unknown policy {value!r}")
        if value == policy.EXACT and item["returns"][slot] not in policy.LITERAL_TYPES:
            fail(node_id, f"output {name} is EXACT on non-literal type {item['returns'][slot]}")
        if isinstance(value, policy.Inherits) and not value.reason.strip():
            fail(node_id, f"output {name} INHERITS without a reason")
        if value == policy.DESCRIPTOR:
            cases = (getattr(cls, policy.INVALIDATION_ATTRIBUTE, None) or {}).get(name)
            if not cases:
                fail(node_id, f"output {name} is DESCRIPTOR with no registered invalidation cases")
    for key in declared:
        if not any(policy.declaration_matches(key, name) for name in names):
            fail(node_id, f"declares {key!r}, which matches no output")
    if node_id in needs_execution:
        execution_policy = policy.execution_policy(cls)
        if isinstance(execution_policy, policy.AlwaysRuns):
            if execution_policy.proof not in policy.PROOFS:
                fail(node_id, f"ALWAYS_RUNS proof {execution_policy.proof!r} is not a known proof")
            elif execution_policy.proof == policy.TAKES_PROJECT and "SONDER_PROJECT" not in item["inputs"]["required"].values():
                fail(node_id, "ALWAYS_RUNS(takes SONDER_PROJECT) without a required SONDER_PROJECT input")
            elif execution_policy.proof == policy.NAN_FINGERPRINT:
                fingerprint = getattr(cls, "IS_CHANGED", None)
                try:
                    value = fingerprint() if fingerprint is not None else None
                except Exception as exc:
                    value = exc
                if not (isinstance(value, float) and math.isnan(value)):
                    fail(node_id, "ALWAYS_RUNS(NaN fingerprint) but IS_CHANGED is not NaN")
        elif isinstance(execution_policy, policy.Cacheable):
            if not execution_policy.reason.strip():
                fail(node_id, "CACHEABLE without a reason")
        else:
            fail(node_id, f"{selected[node_id]} needs SONDER_CACHE_EXECUTION (ALWAYS_RUNS or CACHEABLE)")

editor = info.get("SonderEditor")
out["editor_hidden"] = editor["hidden"] if editor else None
out["editor_exact"] = [slot for slot, name in enumerate(editor["names"])
                       if policy.output_policy(mapping["SonderEditor"], name) == policy.EXACT] if editor else None
print("REGISTRY_RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def registry():
    comfy_root = _comfy_root()
    if comfy_root is None:
        pytest.skip("needs a ComfyUI install with comfy_api (SONDER_COMFYUI_ROOT or the test install's venv)")
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(REPO_ROOT), str(comfy_root)],
        cwd=str(comfy_root), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    line = next((item for item in completed.stdout.splitlines() if item.startswith("REGISTRY_RESULT ")), None)
    assert line is not None, completed.stderr[-4000:]
    return json.loads(line[len("REGISTRY_RESULT "):])


def test_every_selected_node_declares_a_valid_cache_policy(registry):
    assert registry["failures"] == []


def test_the_selection_covers_the_editor_bridges_savers_and_previews(registry):
    selected = set(registry["selected"])
    # A sanity floor so a broken walk cannot pass by selecting nothing.
    assert {
        "SonderEditor", "SonderMetadataCollector", "SonderSaveVideo", "SonderSaveBridge", "SonderPreviewVideo",
        "SonderGuidesBridgeStart", "SonderGuidesBridgeEnd", "SonderDriverSelector", "SonderDriverBridge",
        "SonderMasksBridge", "SonderPromptRelayBridge",
    } <= selected
    # Unconditional: the walk runs with real comfy_api, so the V3 nodes must register.
    assert {"SonderReferenceSelector", "SonderReferenceImageBridge", "SonderReferenceAudioBridge",
            "SonderReferencePromptBridge", "SonderMetadataCollectorV3"} <= selected
    assert {"SONDER_PROJECT", "SONDER_DRIVER_REF", "SONDER_REFERENCE_SET"} <= set(registry["derived"])
    # Pure value nodes are not selected: nothing per-run, nothing project-derived.
    assert "SonderSelector" not in selected and "SonderLazySwitch" not in selected


def test_the_editor_has_only_the_hidden_inputs_the_early_run_supplies(registry):
    # cache_pipeline runs the Editor with extra_data={}: valid only while its
    # hidden inputs are exactly PROMPT and UNIQUE_ID.
    assert registry["editor_hidden"] == ["PROMPT", "UNIQUE_ID"]
    assert registry["editor_exact"] == [3, 4, 5, 6, 7, 8, 9, 11, 12]
