"""One policy for a durable-storage integrity failure: a shaped 500 carrying no path.

`ProjectStorageError` means the server cannot read its own state. Five route-level
policies used to claim it — 400, 409, a broad `except Exception` 500, an explicit
re-raise and an implicit one — and three of those rendered `str(exc)`, which for the
component-read raise site is an absolute filesystem path. Per-route guards were written
three times and still missed twelve sites, because lazy provenance hydration makes the
raise point any read of `generation_params` — including the ones `Asset.to_dict()` and
`_asset_payload()` perform on the caller's behalf, which name nothing a reviewer greps.

These are the full-stack proofs that one middleware arm owns the response, plus the
structural scan that stops the next `except Exception as e: _json_error(str(e), 500)`
from reopening it site by site.
"""
import ast
import asyncio
import json
from functools import partial
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from test_project_mutation_pipeline_backend import (
    DummyRequest, _load_route_module, _route_handler)
from server import project_manager as pm
from server.timeline_state import Asset

ASSET_ID = "frozen-asset"


def _project_with_frozen_storage(tmp_path):
    """A saved project carrying both a history and an asset-provenance component."""
    created = pm.create_project("Storage policy", base_dir=str(tmp_path))
    project = pm.load_project(created.project_dir)
    project.assets = [Asset(asset_id=ASSET_ID, name="clip.mp4", asset_type="video",
                            path="media/clip.mp4",
                            generation_params={"prompt": "kept",
                                               "editor_export": {"snapshot": True}})]
    project.metadata["prompt_history"] = [
        {"hash": "h", "sections": [{"channels": {"visual": "v"}}]}]
    pm.save_project(project, notify=False)
    media = Path(project.project_dir) / "media"
    media.mkdir(exist_ok=True)
    (media / "clip.mp4").write_bytes(b"video")
    return project.project_dir


def _delete_component(project_dir, prefix):
    """Remove one published component so its next read fails.

    Which message that produces depends on the reader. `load_project` runs
    `validate_storage`, whose `os.scandir` snapshot rejects a *missing* entry before
    the open, giving the path-free message. The lazy readers behind the three ex-409
    routes pass no snapshot at all, so they reach `open()` and raise the message that
    interpolates an absolute path. Deletion is therefore the corruption that separates
    the two, which is why it is the one used here.

    Note the snapshot gate is not a redaction: it screens absence, non-regular files
    and reparse points only. A component that exists and still cannot be opened —
    permissions, a sharing violation, an I/O error, MAX_PATH — reaches the path-bearing
    branch through `validate_storage` too, on any `load_project`.
    """
    root = Path(project_dir) / "project.json"
    components = json.loads(root.read_bytes())["storage"]["components"]
    matches = [descriptor for name, descriptor in components.items()
               if name.startswith(prefix)]
    assert len(matches) == 1, f"expected one {prefix} component, found {len(matches)}"
    target = root.parent / matches[0]["path"]
    target.unlink()
    return str(target)


def _read_the_same_component(project_dir, route):
    """Call the reader the route calls, directly, so the test can prove what it leaks."""
    from server import project_storage as ps
    if route == "history":
        return ps.read_prompt_history(project_dir)
    if route == "provenance_batch":
        return ps.read_asset_provenance_batch(project_dir, [ASSET_ID])
    return ps.read_asset_provenance(project_dir, ASSET_ID)


def _full_stack(module, handler):
    """Production middleware order: the project error middleware ends up outermost."""
    for middleware in [module._route_timing_middleware, module._sonder_security_middleware,
                       module._project_version_header_middleware,
                       module._project_error_middleware]:
        handler = partial(middleware, handler=handler)
    return handler


def _body(response):
    return json.loads(response.body.decode("utf-8"))


def _decoded_values(payload):
    """Every string the client actually reads back.

    Path assertions must run against these, not against `response.body`: JSON escapes
    the backslashes in a Windows path, so a substring check on the wire text passes
    today while the path is sitting in the body.
    """
    return " | ".join(str(value) for value in payload.values())


# ---------------------------------------------------------------------------
# T7 — the keystone. A real route, real corruption, and the whole Sonder stack.
# ---------------------------------------------------------------------------

def test_empty_trash_reports_unreadable_storage_as_a_shaped_server_error(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project_dir = _project_with_frozen_storage(tmp_path)
    leaked_path = _delete_component(project_dir, "prompt_history")
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(tmp_path))
    project_id = Path(project_dir).name

    path = "/sonder-editor/project/{project_id}/assets/empty-trash"
    handler = _full_stack(module, _route_handler(module, "POST", path))
    request = make_mocked_request(
        "POST", "/api" + path.format(project_id=project_id),
        headers={"Host": "localhost"}, match_info={"project_id": project_id})
    response = asyncio.run(handler(request))

    payload = _body(response)
    readable = _decoded_values(payload)
    assert response.status == 500, payload
    assert payload["code"] == "project_storage_unreadable"
    assert payload["error"] == module.PROJECT_STORAGE_UNREADABLE_MESSAGE
    assert set(payload) == {"error", "code"}
    # This route loads the project, so today's message is the path-free one. What it
    # still discloses is the internal component name, and the curated copy replaces it.
    assert "prompt_history" not in readable
    assert leaked_path not in readable
    assert str(tmp_path) not in readable


# ---------------------------------------------------------------------------
# T9 — the three routes that mapped this to 409 and rendered the message, which here
# is the one carrying an absolute path: they read components directly and never run
# `validate_storage`. Both provenance routes sit on the gallery's refresh path, not
# just the history panel.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["history", "provenance_batch", "provenance_single"])
def test_storage_reads_that_returned_409_now_return_the_shaped_500(tmp_path, monkeypatch, route):
    module = _load_route_module(monkeypatch)
    project_dir = _project_with_frozen_storage(tmp_path)
    project_id = Path(project_dir).name
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(tmp_path))

    query = ""
    match_info = {"project_id": project_id}
    if route == "history":
        leaked_path = _delete_component(project_dir, "prompt_history")
        path = "/sonder-editor/project/{project_id}/prompt-history"
    else:
        leaked_path = _delete_component(project_dir, "provenance_")
        if route == "provenance_batch":
            path = "/sonder-editor/project/{project_id}/assets/provenance"
            query = f"?asset_id={ASSET_ID}"
        else:
            path = "/sonder-editor/project/{project_id}/assets/{asset_id}/provenance"
            match_info["asset_id"] = ASSET_ID

    handler = _full_stack(module, _route_handler(module, "GET", path))
    request = make_mocked_request("GET", "/api" + path.format(**match_info) + query,
                                  headers={"Host": "localhost"}, match_info=match_info)
    response = asyncio.run(handler(request))

    payload = _body(response)
    readable = _decoded_values(payload)
    assert response.status == 500, payload
    assert payload["code"] == "project_storage_unreadable"
    # Anchor the disclosure check against the reader itself, not against the shape of
    # the variable: prove this route's reader really does produce the absolute path, so
    # the absence assertion below is answering a question that could have gone the other
    # way. `assert Path(leaked_path).is_absolute()` looked like this check and was not —
    # it says nothing about what the message contains.
    with pytest.raises(Exception) as raised:
        _read_the_same_component(project_dir, route)
    assert leaked_path in str(raised.value)

    assert leaked_path not in readable
    assert str(tmp_path) not in readable


# ---------------------------------------------------------------------------
# T13 — structural. The two `except Exception as e: _json_error(str(e), 500)` sites
# were missed by three separate hand-written guard passes; a scan is what finds the
# fourth. Modelled on the off-loop AST guard in `test_project_executor.py`.
# ---------------------------------------------------------------------------

ROUTES_SOURCE = Path(__file__).resolve().parents[1] / "server" / "routes.py"

# Callables that can raise `ProjectStorageError`, directly or through the lazy
# provenance hydration `Asset.__getattribute__` performs on `generation_params` — and
# only on that name, not on any attribute. `to_dict` is a leaf because it is how a
# handler reads it without naming it: `Asset.to_dict(include_provenance=True)` reads
# `generation_params` for the caller, which is exactly the hop `_asset_payload` takes.
# Its closure lives in `timeline_state.py`, so the scan cannot derive this one.
STORAGE_LEAVES = {
    "load_project", "save_project", "create_project", "list_projects",
    "save_generated_project", "hydrate_job", "hydrate_asset", "resolve_queue_job",
    "read_component", "read_prompt_history", "read_asset_provenance",
    "read_asset_provenance_batch", "storage_of", "descriptor_path",
    "generation_params", "to_dict", "_load_project_from_request", "_asset_payload",
    "_asset_payloads",
}

# Broad catches that see a `ProjectStorageError` now that it is an ordinary
# `Exception`. A catch naming some other specific subclass is deliberate.
BROAD_CATCHES = {"Exception", "BaseException", "ProjectStorageError"}


def _referenced_names(node):
    """Every name the node mentions, including ones it only passes along.

    Bare `ast.Name` counts, not just calls: the storage-reaching work in these
    handlers is routinely handed to `run_project_io(_load_project_from_request, ...)`
    as a reference, so a call-only scan misses the try bodies that matter most. Over-
    matching a same-named local is the safe direction — it reports a site for review
    rather than certifying one.
    """
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            names.add(getattr(child.func, "id", None) or getattr(child.func, "attr", None))
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.Name):
            names.add(child.id)
    return {name for name in names if name}


def _storage_reaching_names(tree):
    bodies = {node.name: _referenced_names(node) for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    reaching = set(STORAGE_LEAVES)
    changed = True
    while changed:
        changed = False
        for name, called in bodies.items():
            if name not in reaching and called & reaching:
                reaching.add(name)
                changed = True
    return reaching


def _caught_names(handler):
    node = handler.type
    if node is None:
        return {"BaseException"}
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    return {name for name in
            (getattr(part, "id", None) or getattr(part, "attr", None) for part in parts)
            if name}


def _mentions(node, names):
    return any(isinstance(child, ast.Name) and child.id in names
               for child in ast.walk(node))


def _tainted_names(handler):
    """The bound exception, plus every local that carries its text.

    `payload = {"error": str(exc), ...}` then `return web.json_response(payload)` is
    the shape four handlers in this file already use, and `msg = f"...{e}"` then
    `_json_error(msg, 500)` is the same laundering one hop shorter. A matcher that
    only looks for the bound name *inside* the builder call misses both, which would
    leave the scan checking spelling rather than behaviour.
    """
    tainted = {handler.name}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(handler):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets, value = [node.target], node.value
            else:
                continue
            if value is None or not _mentions(value, tainted):
                continue
            for target in targets:
                for inner in ast.walk(target):
                    if isinstance(inner, ast.Name) and inner.id not in tainted:
                        tainted.add(inner.id)
                        changed = True
    return tainted


def _renders_bound_exception(handler):
    """True when the handler puts its own exception's text into what it returns.

    Two shapes count, because a route handler's `return` *is* its response: an
    explicit response builder carrying tainted data, and any returned expression
    carrying it. The second is what keeps the check from being an allowlist of the
    builder names that happen to be in use today.
    """
    if not handler.name:
        return False
    tainted = _tainted_names(handler)
    builders = {"_json_error", "json_response", "Response", "_mutation_error"}
    for node in ast.walk(handler):
        if isinstance(node, ast.Return) and node.value is not None:
            if _mentions(node.value, tainted):
                return True
        if not isinstance(node, ast.Call):
            continue
        callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if callee not in builders:
            continue
        arguments = list(node.args) + [keyword.value for keyword in node.keywords]
        if any(_mentions(argument, tainted) for argument in arguments):
            return True
    return False


def _hands_on_to_the_middleware(handler):
    """A route's arm for this class may only pass it on — but may log on the way.

    Checked because "does not interpolate the exception" is too weak a property on its
    own. A guard rewritten from `raise` to `return _json_error("Storage unreadable",
    400)` reverses the whole policy — a server-side integrity failure reported as the
    caller's mistake, decided per route again — while still rendering nothing, so a
    matcher that only looks for interpolation reads it as compliant.

    The test is the arm's exit, not its length: it must end in a bare `raise` and must
    not build a response. Requiring a *sole* `raise` would reject `logger.warning(...)`
    before it, which changes nothing about the response and is a reasonable thing for a
    handler to add.
    """
    body = [node for node in handler.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    if not body:
        return False
    last = body[-1]
    if not (isinstance(last, ast.Raise) and last.exc is None):
        return False
    return not any(isinstance(node, ast.Return) for node in ast.walk(handler))


def _storage_policy_violations():
    """Two distinct violations, reported separately because they read differently.

    `renders` is a broad arm putting the exception's text into its response body.
    `decides` is a named arm answering this class itself instead of passing it on.
    Collapsing them would report a route that never touches the body under a message
    saying it does, which is how a maintainer ends up 'fixing' the wrong thing.
    """
    tree = ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))
    reaching = _storage_reaching_names(tree)
    renders, decides = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body_names = set()
        for statement in node.body:
            body_names |= _referenced_names(statement)
        if not body_names & reaching:
            continue
        claimed = False
        for handler in node.handlers:
            caught = _caught_names(handler)
            if "ProjectStorageError" in caught:
                # Either way the arm has claimed this class, so the broad catch below
                # is no longer answerable for it — reporting both would describe the
                # sibling as an offender for something it cannot see.
                claimed = True
                if _hands_on_to_the_middleware(handler):
                    continue
                # A named arm that answers instead of passing on is a route deciding
                # this class's response for itself: the five-policy state being removed.
                decides.append((handler.lineno, sorted(caught)))
                continue
            if not caught & BROAD_CATCHES or claimed:
                continue
            if _renders_bound_exception(handler):
                renders.append((handler.lineno, sorted(caught)))
    return renders, decides


def test_no_route_handler_renders_a_caught_storage_failure_into_its_response():
    """Structural, because a per-site guard provably cannot be completed here.

    `Asset.__getattribute__` hydrates `generation_params` lazily, so the raise point is
    any read of that one attribute — including the reads `to_dict()` and `_asset_payload()`
    perform for the caller, which put no greppable call name in the handler. What is checkable is the shape of the catch: a broad `except` over a try body
    that can reach durable storage must not interpolate the exception into the body it
    returns, unless a `ProjectStorageError` arm ahead of it already claimed that case.
    """
    renders, decides = _storage_policy_violations()
    assert renders == [], (
        "routes.py handlers render a caught durable-storage failure into the response "
        f"body (line, caught): {renders}")
    assert decides == [], (
        "routes.py handlers answer a durable-storage failure themselves instead of "
        f"re-raising it to the middleware (line, caught): {decides}")


# ---------------------------------------------------------------------------
# One end-to-end proof that a guard in front of a broad `except Exception` actually
# changes the response, not just the AST. `api_create_project` is the cheap one: it
# takes a JSON body, and `create_project` LOADS when the folder already exists, so
# re-creating a project whose storage is broken reaches the guard through production
# code with nothing about storage monkeypatched.
#
# The structural half of this is `_hands_on_to_the_middleware` above, which covers the two
# multipart routes — they would need PIL and cv2 fixtures to drive, and a policy
# reversal on them is caught by the scan rather than by a fixture.
# ---------------------------------------------------------------------------

def test_recreating_a_project_with_unreadable_storage_is_a_shaped_server_error(
        tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project_dir = _project_with_frozen_storage(tmp_path)
    _delete_component(project_dir, "prompt_history")
    monkeypatch.setattr(module, "_configured_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(module, "_get_base_dir", lambda: str(tmp_path))

    path = "/sonder-editor/project"
    handler = _full_stack(module, _route_handler(module, "POST", path))
    request = DummyRequest(match_info={}, method="POST", path=path,
                           body={"name": "Storage policy"})
    response = asyncio.run(handler(request))

    payload = _body(response)
    assert response.status == 500, payload
    assert payload["code"] == "project_storage_unreadable"
    assert payload["error"] == module.PROJECT_STORAGE_UNREADABLE_MESSAGE
    assert set(payload) == {"error", "code"}
    # No path assertion here: this route reaches the failure through `load_project`, so
    # the message it would otherwise have rendered is the path-free one. Asserting the
    # path's absence would pass with or without the guard. The disclosure is pinned by
    # the ex-409 routes above, where the reader really does produce it; what this case
    # proves is the policy — the guard turns a rendered 400/500 into the shaped 500.
