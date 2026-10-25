"""Live sampling previews (Output Views Phase 4): attribution, the live store, reads and bytes."""

import asyncio
import base64
import io
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from server import (kj_preview_adapter, run_output_routes, run_outputs, run_scope, sampling_previews as sp,
                    sdk_progress, sdk_runs)

OWNER = ("host", "proj", "host-1")
OTHER = ("host", "proj", "host-2")
R1 = "bbbbbbbb-0000-4000-8000-000000000001"
R2 = "bbbbbbbb-0000-4000-8000-000000000002"


def _node_class(**inputs):
    return SimpleNamespace(INPUT_TYPES=lambda: {"required": {name: (kind,) for name, kind in inputs.items()}})


# Declared input types, as ComfyUI's node classes give them (the model chain follows MODEL links).
CLASSES = {"KSampler": _node_class(model="MODEL", latent_image="LATENT"),
           "ModelPreviewOverrideKJ": _node_class(model="MODEL"),
           "Out": _node_class(v="INT", p="IMAGE")}
WEBP = base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 fake").decode("ascii")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for module in (sdk_runs, run_outputs, sp, kj_preview_adapter):
        module.reset_for_tests()
    run_scope.reset_for_tests({"SonderEditor": False, "KSampler": False, "ModelPreviewOverrideKJ": False,
                               "Out": False, "Loader": False})
    state = SimpleNamespace(context=None, registry=None)
    monkeypatch.setattr(sp, "_class_lookup", lambda name: CLASSES.get(name))
    monkeypatch.setattr(sp, "_context", lambda: state.context)
    monkeypatch.setattr(sp, "_registry_for",
                        lambda pid: state.registry if state.registry is not None and state.registry.prompt_id == pid else None)
    yield state
    for module in (sdk_runs, run_outputs, sp, kj_preview_adapter):
        module.reset_for_tests()
    run_scope.reset_for_tests()


def graph(receipt=R1, kj=("kj",), sampler_inputs=None):
    """Loader -> [kj ->] sampler -> out, with the Editor carrying the receipt."""
    prompt = {"ed": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_receipt": receipt}},
              "load": {"class_type": "Loader", "inputs": {}}}
    model = ["load", 0]
    for key in kj:
        prompt[key] = {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": model}, "_meta": {"title": "Preview " + key}}
        model = [key, 0]
    prompt["s"] = {"class_type": "KSampler", "inputs": sampler_inputs or {"model": model}, "_meta": {"title": "Pass 1"}}
    prompt["out"] = {"class_type": "Out", "inputs": {"v": ["s", 0], "p": ["ed", 0]}}
    return prompt


def queue(prompt_id, prompt, receipt=R1, owner=OWNER, workflow="wf", key="1"):
    code, _ = sdk_runs.register_receipt(receipt, owner, ["ed"])
    assert code == "ok"
    sdk_runs.bind_queued(prompt_id, prompt, ["out"], workflow_id=workflow)
    sdk_runs.open_release(key, prompt_id, prompt, ["out"])
    run_outputs.observe("execution_start", {"prompt_id": prompt_id})


def finish(prompt_id, key="1", outcome="success"):
    run_outputs.finish(key, prompt_id, {"outputs": {}, "meta": {}}, outcome)
    sdk_runs.close_release(key)


def ctx(prompt_id, node):
    return SimpleNamespace(prompt_id=prompt_id, node_id=node, list_index=None)


def registry(prompt_id, display=None, steps=None):
    display = display or {}
    nodes = {node: {"value": value, "max": total} for node, (value, total) in (steps or {}).items()}
    return SimpleNamespace(prompt_id=prompt_id, nodes=nodes,
                           dynprompt=SimpleNamespace(get_display_node_id=lambda node: display.get(node, node)))


def image(size=(8, 6), color=(200, 10, 10)):
    return ("JPEG", Image.new("RGB", size, color), 512)


def native4(prompt_id, node, display=None, picture=None):
    sp.observe_preview(4, (picture or image(), {"prompt_id": prompt_id, "node_id": node,
                                                "display_node_id": display or node}))


def kj(state, prompt_id, sampler, node_id, **payload):
    state.context = ctx(prompt_id, sampler)
    sp.observe_preview("kj_preview_override", {"node_id": node_id, "step": payload.pop("step", 3),
                                               "total": payload.pop("total", 10), **payload})


def live(*sources, owners=(OWNER,), workflow=""):
    return run_outputs.read_live(list(owners), workflow, list(sources))


# --- Native ------------------------------------------------------------------------

def test_event_4_is_attributed_by_its_metadata_and_takes_the_step_from_the_registry(_clean):
    queue("p1", graph())
    _clean.registry = registry("p1", steps={"s": (4, 20)})
    native4("p1", "s")
    answer = live("s")["sources"]["s"]
    assert answer["state"] == "live"
    record = answer["live"]
    assert (record["publisher"], record["step"], record["total"], record["mime"]) == ("native", 4, 20, "image/jpeg")
    assert record["class_type"] == "KSampler" and record["title"] == "Pass 1" and record["has_payload"]
    assert answer["run"]["preview_method"] == ""  # sampling_previews' own observer records it (below)


def test_event_2_needs_the_execution_context_and_never_guesses(_clean):
    queue("p1", graph())
    sp.observe_preview(2, image())
    assert sp.COUNTERS["unattributed_native"] == 1
    assert live("s")["sources"]["s"]["state"] == "waiting"
    _clean.context = ctx("p1", "s")
    sp.observe_preview(2, image())
    assert live("s")["sources"]["s"]["state"] == "live"


def test_unbound_foreign_and_malformed_previews_are_dropped(_clean):
    queue("p1", graph())
    native4("other-prompt", "s")
    native4("p1", "not-in-prompt")
    sp.observe_preview(4, ("garbage", {"prompt_id": "p1", "node_id": "s"}))
    sp.observe_preview(4, ("JPEG", object(), 512))
    assert sp.COUNTERS["foreign_source"] == 1
    assert run_outputs.read_live([OWNER], "", ["s", "not-in-prompt"])["sources"]["s"]["live"] is None


def test_a_source_inside_a_subgraph_is_its_full_prompt_key(_clean):
    prompt = graph()
    prompt["12:5"] = prompt.pop("s")
    prompt["out"]["inputs"]["v"] = ["12:5", 0]
    queue("p1", prompt)
    native4("p1", "12:5")
    assert live("12:5")["sources"]["12:5"]["state"] == "live"


def test_the_display_mapping_applies_only_to_its_own_prompt(_clean):
    queue("p1", graph())
    _clean.context = ctx("p1", "s.child")
    _clean.registry = registry("p0", display={"s.child": "s"})  # another prompt's registry
    sp.observe_preview(2, image())
    assert sp.COUNTERS["foreign_source"] == 1
    _clean.registry = registry("p1", display={"s.child": "s"})
    sp.observe_preview(2, image())
    assert live("s")["sources"]["s"]["state"] == "live"


def test_native_bytes_are_encoded_like_comfyui_and_cached(_clean):
    queue("p1", graph())
    native4("p1", "s", picture=("PNG", Image.new("RGBA", (1024, 512), (1, 2, 3, 4)), 256))
    record = live("s")["sources"]["s"]["live"]
    assert (record["mime"], record["w"], record["h"]) == ("image/png", 256, 128)
    code, public, payload = run_outputs.live_payload([OWNER], "p1", "s")
    assert code == "ok" and payload[0] == "pil"
    body = sp.encode_payload(payload)
    assert Image.open(io.BytesIO(body)).size == (256, 128)
    before = run_outputs.live_bytes_in_use()
    run_outputs.store_live_bytes("p1", "s", public["n"], body)
    assert run_outputs.live_payload([OWNER], "p1", "s")[2] == ("bytes", body)
    assert run_outputs.live_bytes_in_use() == before - 1024 * 512 * 4 + len(body)


# --- KJ ------------------------------------------------------------------------------

def test_kj_previews_belong_to_the_override_node_on_the_samplers_model_chain(_clean):
    queue("p1", graph())
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp", fps=8, w=64, h=48)
    record = live("kj")["sources"]["kj"]["live"]
    assert (record["publisher"], record["via"], record["mime"], record["fps"]) == ("kj", "s", "image/webp", 8)
    assert record["reattributed"] is False and record["title"] == "Preview kj"
    assert sp.encode_payload(run_outputs.live_payload([OWNER], "p1", "kj")[2]) == base64.b64decode(WEBP)


def test_a_cached_wrapper_naming_another_identical_node_is_reattributed_through_the_chain(_clean):
    # Two identical Override nodes: ComfyUI's cache leaves a V3 node id out of the
    # signature, so kj2's sampler can run with kj1's wrapper (and kj1's id).
    prompt = graph(kj=())
    prompt["kj1"] = {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": ["load", 0]}}
    prompt["kj2"] = {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": ["load", 0]}}
    prompt["s"]["inputs"] = {"model": ["kj1", 0]}
    prompt["s2"] = {"class_type": "KSampler", "inputs": {"model": ["kj2", 0]}}
    prompt["out2"] = {"class_type": "Out", "inputs": {"v": ["s2", 0]}}
    sdk_runs.register_receipt(R1, OWNER, ["ed"])
    sdk_runs.bind_queued("p1", prompt, ["out", "out2"], workflow_id="wf")
    kj(_clean, "p1", "s2", "kj1", image=WEBP, mime="image/webp")
    answer = live("kj1", "kj2")["sources"]
    assert answer["kj2"]["live"]["reattributed"] is True and answer["kj2"]["live"]["via"] == "s2"
    assert answer["kj1"]["live"] is None


def test_an_ambiguous_kj_source_is_dropped(_clean):
    prompt = graph(kj=("kj1", "kj2"))  # stacked: both upstream of the sampler
    queue("p1", prompt)
    kj(_clean, "p1", "s", "elsewhere", image=WEBP, mime="image/webp")
    assert sp.COUNTERS["kj_ambiguous"] == 1
    kj(_clean, "p1", "s", "kj2", image=WEBP, mime="image/webp")
    assert live("kj2")["sources"]["kj2"]["state"] == "live"


def test_kj_without_context_marks_the_adapter_degraded(_clean):
    queue("p1", graph())
    kj_preview_adapter._set(kj_preview_adapter.STATE_OK)
    _clean.context = None
    sp.observe_preview("kj_preview_override", {"node_id": "kj", "image": WEBP, "mime": "image/webp"})
    assert sp.COUNTERS["unattributed_kj"] == 1
    assert kj_preview_adapter.status()["state"] == "degraded"


def test_kj_boundary_zero_unsupported_and_too_large(_clean, monkeypatch):
    queue("p1", graph())
    kj(_clean, "p1", "s", "kj", step=0, sigmas=[1.0, 0.5])
    assert live("kj")["sources"]["kj"]["state"] == "waiting"
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/gif")
    assert live("kj")["sources"]["kj"]["state"] == "unsupported"
    monkeypatch.setattr(sp, "MAX_PAYLOAD_CHARS", 8)
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp")
    assert live("kj")["sources"]["kj"]["state"] == "too_large"
    assert run_outputs.live_bytes_in_use() == 0


def test_late_kj_payloads_count_within_the_finish_grace_only(_clean, monkeypatch):
    queue("p1", graph())
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp", step=1)
    finish("p1")
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp", step=2)  # the prompt is gone; the verdict stands
    assert live("kj")["sources"]["kj"]["live"]["step"] == 2
    finished_at = sdk_runs.bound_run("p1")["finished_at"]
    monkeypatch.setattr(sp.time, "time", lambda: finished_at + sp.FINISH_GRACE_SEC + 1)
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp", step=3)
    assert live("kj")["sources"]["kj"]["live"]["step"] == 2


# --- Store and reads -----------------------------------------------------------------

def test_newest_replaces_and_a_newer_run_supersedes_the_hosts_older_payload(_clean):
    queue("p1", graph())
    native4("p1", "s")
    first = live("s")["sources"]["s"]["live"]["n"]
    native4("p1", "s")
    assert live("s")["sources"]["s"]["live"]["n"] > first
    one_payload = run_outputs.live_bytes_in_use()
    finish("p1")
    queue("p2", graph(receipt=R2), receipt=R2, key="2")
    assert live("s")["sources"]["s"]["state"] == "previous"
    native4("p2", "s")
    assert run_outputs.live_bytes_in_use() == one_payload
    assert run_outputs.live_payload([OWNER], "p1", "s")[0] == "superseded"
    assert live("s")["sources"]["s"]["state"] == "live"


def test_supersession_is_per_workflow_and_per_host(_clean):
    queue("p1", graph(), workflow="wf-a")
    native4("p1", "s")
    finish("p1")
    queue("p2", graph(receipt=R2), receipt=R2, workflow="wf-b", key="2")
    native4("p2", "s")
    assert run_outputs.live_payload([OWNER], "p1", "s")[0] == "ok"
    assert run_outputs.read_live([OTHER], "", ["s"])["sources"]["s"]["state"] == "none"


def test_the_budget_evicts_oldest_payloads_and_its_counter_stays_exact(_clean, monkeypatch):
    monkeypatch.setattr(run_outputs, "LIVE_BUDGET_BYTES", 8 * 6 * 3 * 2)
    queue("p1", graph(kj=(), sampler_inputs={"model": ["load", 0]}))
    prompt = sdk_runs.bound_prompt("p1")
    for key in ("s2", "s3"):
        prompt[key] = {"class_type": "KSampler", "inputs": {}}
    for key in ("s", "s2", "s3"):
        native4("p1", key)
    states = {key: live(key)["sources"][key]["state"] for key in ("s", "s2", "s3")}
    assert states == {"s": "evicted", "s2": "live", "s3": "live"}
    assert run_outputs.live_bytes_in_use() == 8 * 6 * 3 * 2
    finish("p1")
    sdk_runs.reset_for_tests()
    run_outputs.finish("x", "p1", {}, "success")
    with run_outputs._LOCK:
        run_outputs._prune_locked(set())
    assert run_outputs.live_bytes_in_use() == 0 and not run_outputs._PAYLOADS


def test_from_cache_no_previews_and_suppression(_clean):
    queue("p1", graph())
    run_outputs.observe("executing", {"prompt_id": "p1", "node": "s"})
    native4("p1", "s")
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp")
    finish("p1")
    queue("p2", graph(receipt=R2), receipt=R2, key="2")
    finish("p2", key="2")  # the sampler came from cache: nothing executed
    answer = live("s", "kj")["sources"]
    assert answer["s"]["state"] == "cached" and answer["kj"]["state"] == "cached"
    queue("p3", graph(receipt="bbbbbbbb-0000-4000-8000-000000000003"),
          receipt="bbbbbbbb-0000-4000-8000-000000000003", key="3")
    run_outputs.observe("executing", {"prompt_id": "p3", "node": "s"})
    kj(_clean, "p3", "s", "kj", image=WEBP, mime="image/webp")
    finish("p3", key="3")
    answer = live("s")["sources"]["s"]
    assert answer["state"] == "no_previews" and answer["run"]["suppressed_by"] == "kj"


def test_seen_lists_publishers_of_retained_runs_newest_first(_clean):
    queue("p1", graph())
    native4("p1", "s")
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp")
    seen = live()["seen"]
    assert {item["source"]: item["publisher"] for item in seen} == {"s": "native", "kj": "kj"}
    assert run_outputs.read_live([OTHER])["seen"] == []
    assert run_outputs.read_live([OWNER], "other-workflow")["seen"] == []


def test_the_preview_method_is_recorded_at_execution_start(_clean, monkeypatch):
    import enum

    class Method(enum.Enum):
        NoPreviews = "none"

    fake = SimpleNamespace(args=SimpleNamespace(preview_method=Method.NoPreviews))
    monkeypatch.setitem(__import__("sys").modules, "comfy.cli_args", fake)
    queue("p1", graph())
    sp.observe_event("execution_start", {"prompt_id": "p1"})
    assert live("s")["sources"]["s"]["run"]["preview_method"] == "none"


def test_the_observer_never_breaks_send_sync(_clean, monkeypatch):
    sdk_progress.reset_for_tests()
    sent = []
    server = SimpleNamespace(send_sync=lambda *a, **kw: (sent.append(a), "sent")[1])
    assert sdk_progress.install(server)
    sp.install()
    monkeypatch.setattr(sp, "_observe_native", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert server.send_sync(4, ("bad",), "client") == "sent"
    assert server.send_sync("kj_preview_override", None, "client") == "sent"
    assert len(sent) == 2
    sdk_progress.reset_for_tests()


# --- Routes ----------------------------------------------------------------------------

def _client(owner=OWNER):
    async def gate_owner(request):
        return owner

    routes = web.RouteTableDef()

    @routes.get("/live")
    async def read(request):
        return await run_output_routes.live_response([owner], request.query.get("workflow_id", ""),
                                                     request.query.get("sources", ""))

    @routes.get("/live/{prompt_id}/bytes")
    async def raw(request):
        return await run_output_routes.live_bytes_response([owner], request.match_info["prompt_id"],
                                                           request.query.get("source", ""))

    app = web.Application()
    app.add_routes(routes)
    return TestClient(TestServer(app))


def test_routes_serve_the_newest_payload_with_its_n_and_refuse_superseded(_clean, monkeypatch):
    monkeypatch.setattr(run_output_routes, "_reap_removed_runs", lambda: None)
    queue("p1", graph())
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp")

    async def scenario():
        async with _client() as http:
            body = await (await http.get("/live", params={"sources": "kj"})).json()
            assert body["sources"]["kj"]["state"] == "live" and body["sampling"]["kj"]["state"] == "absent"
            kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp")
            response = await http.get("/live/p1/bytes", params={"source": "kj"})
            assert response.status == 200 and response.headers["Content-Type"] == "image/webp"
            assert response.headers["Cache-Control"] == "no-store"
            assert int(response.headers["X-Sonder-Live-N"]) > body["sources"]["kj"]["live"]["n"]
            assert await response.read() == base64.b64decode(WEBP)
            assert (await http.get("/live/p9/bytes", params={"source": "kj"})).status == 404
            finish("p1")
            queue("p2", graph(receipt=R2), receipt=R2, key="2")
            kj(_clean, "p2", "s", "kj", image=WEBP, mime="image/webp")
            assert (await http.get("/live/p1/bytes", params={"source": "kj"})).status == 409
        async with _client(OTHER) as http:
            assert (await http.get("/live/p2/bytes", params={"source": "kj"})).status == 404

    asyncio.new_event_loop().run_until_complete(scenario())


def test_live_routes_are_registered_before_the_run_wildcard():
    routes = web.RouteTableDef()
    run_output_routes.register_run_output_routes(routes)
    paths = [route.path for route in routes]
    wildcard = paths.index("/sonder-editor/project/{project_id}/runs/{prompt_id}")
    assert paths.index("/sonder-editor/project/{project_id}/runs/live") < wildcard
    assert "/sonder-editor/project/{project_id}/runs/live/{prompt_id}/bytes" in paths


# --- Hints and the KJ shape gate ------------------------------------------------------

def test_live_hints_are_throttled_with_a_trailing_hint(tmp_path, monkeypatch):
    from test_editor_node import _import_io_nodes
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    io_nodes._LIVE_HINT_STATE.clear()
    period = io_nodes.LIVE_HINT_PERIOD_SEC
    assert io_nodes._live_hint_delay("p", 10.0) == 0.0            # first: now
    assert io_nodes._live_hint_delay("p", 10.05) == pytest.approx(period - 0.05)  # trailing, once
    assert io_nodes._live_hint_delay("p", 10.10) is None          # already due
    assert io_nodes._live_hint_delay("p", 10.30) == pytest.approx(0.20)  # after the trailing one fired
    assert io_nodes._live_hint_delay("p", 11.0) == 0.0            # quiet again: now
    assert io_nodes._live_hint_delay("q", 10.05) == 0.0            # per prompt


def test_the_shape_gate_refuses_a_changed_kj(tmp_path):
    import types
    module = types.ModuleType("fake_kj")

    class _AsyncPreviewEncoder:
        def submit(self, fn, block=False):
            return True

    class _PreviewOverrideWrapper:
        def __call__(self):
            pass

    module._AsyncPreviewEncoder, module._PreviewOverrideWrapper = _AsyncPreviewEncoder, _PreviewOverrideWrapper
    assert kj_preview_adapter.check_shape(module) == "submit_signature"
    assert kj_preview_adapter.install(module)["state"] == "unsupported"
    assert kj_preview_adapter.check_shape(types.ModuleType("empty")) == "classes_missing"
    kj_preview_adapter.reset_for_tests()
    assert kj_preview_adapter.status()["state"] == "absent"


def test_the_submit_wrapper_falls_back_and_never_raises(monkeypatch):
    calls = []

    def original(self, fn):
        calls.append(fn)
        return "queued"

    wrapped = kj_preview_adapter._wrap(original)
    assert getattr(wrapped, kj_preview_adapter.MARKER) is True and wrapped.__wrapped__ is original
    assert wrapped(None, lambda: "ran") == "queued" and calls[-1]() == "ran"
    monkeypatch.setattr(kj_preview_adapter.contextvars, "copy_context", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert wrapped(None, "plain") == "queued" and calls[-1] == "plain"


# --- Phase 4A audit fixes ----------------------------------------------------------------

def test_a_second_pass_sampler_never_takes_the_first_pass_override(_clean):
    # Hires fix: s2 takes its latent from s. Identical Override nodes share one
    # cached wrapper, so s2's previews may name kj1; only s2's model chain counts.
    prompt = graph(kj=())
    prompt["kj1"] = {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": ["load", 0]}}
    prompt["kj2"] = {"class_type": "ModelPreviewOverrideKJ", "inputs": {"model": ["load", 0]}}
    prompt["s"]["inputs"] = {"model": ["kj1", 0]}
    prompt["s2"] = {"class_type": "KSampler", "inputs": {"model": ["kj2", 0], "latent_image": ["s", 0]}}
    prompt["out"]["inputs"]["v"] = ["s2", 0]
    queue("p1", prompt)
    kj(_clean, "p1", "s2", "kj1", image=WEBP, mime="image/webp")
    answer = live("kj1", "kj2")["sources"]
    assert answer["kj2"]["live"]["via"] == "s2" and answer["kj2"]["live"]["reattributed"] is True
    assert answer["kj1"]["live"] is None
    kj(_clean, "p1", "s", "kj1", image=WEBP, mime="image/webp")
    assert live("kj1")["sources"]["kj1"]["live"]["via"] == "s"


def test_a_queued_rerun_does_not_turn_the_streaming_preview_into_previous(_clean):
    queue("p1", graph())
    native4("p1", "s")
    queue("p2", graph(receipt=R2), receipt=R2, key="2")
    with sdk_runs._LOCK:
        sdk_runs._BOUND["p2"]["state"] = sdk_runs.BOUND_QUEUED  # bound, still waiting in the queue
    answer = live("s")["sources"]["s"]
    assert answer["state"] == "live" and answer["run"]["prompt_id"] == "p1"
    assert answer["latest_run"]["prompt_id"] == "p2" and answer["latest_run"]["in_scope"] is True


def test_latest_run_says_when_a_newer_run_left_the_source_out(_clean):
    queue("p1", graph())
    native4("p1", "s")
    finish("p1")
    other = {"ed": {"class_type": "SonderEditor", "inputs": {}, "_meta": {"sonder_receipt": R2}},
             "out2": {"class_type": "Out", "inputs": {"p": ["ed", 0]}}}
    sdk_runs.register_receipt(R2, OWNER, ["ed"])
    sdk_runs.bind_queued("p2", other, ["out2"], workflow_id="wf")
    answer = live("s")["sources"]["s"]
    assert answer["state"] == "last"
    assert answer["latest_run"]["prompt_id"] == "p2" and answer["latest_run"]["in_scope"] is False


def test_native_size_matches_the_bytes_even_for_a_small_preview(_clean):
    queue("p1", graph())
    native4("p1", "s", picture=("JPEG", Image.new("RGB", (64, 48)), 512))
    record = live("s")["sources"]["s"]["live"]
    body = sp.encode_payload(run_outputs.live_payload([OWNER], "p1", "s")[2])
    assert (record["w"], record["h"]) == Image.open(io.BytesIO(body)).size == (512, 384)


def test_event_4_from_another_prompts_thread_is_dropped(_clean):
    queue("p1", graph())
    _clean.context = ctx("p0", "s")  # metadata says p1, the sending context is another prompt's
    native4("p1", "s")
    assert sp.COUNTERS["context_mismatch"] == 1 and live("s")["sources"]["s"]["live"] is None


def test_a_late_payload_for_a_superseded_run_answers_superseded(_clean):
    queue("p1", graph())
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp")
    finish("p1")
    queue("p2", graph(receipt=R2), receipt=R2, key="2")
    kj(_clean, "p2", "s", "kj", image=WEBP, mime="image/webp")
    kj(_clean, "p1", "s", "kj", image=WEBP, mime="image/webp")  # the older run's encoder, draining late
    assert run_outputs.live_payload([OWNER], "p1", "kj")[0] == "superseded"
    assert live("kj")["sources"]["kj"]["live"]["prompt_id"] == "p2"


def test_expansion_ids_keep_their_display_mapping_after_the_registry_moves_on(_clean):
    queue("p1", graph())
    _clean.registry = registry("p1", display={"s.clone": "s"})
    kj(_clean, "p1", "s.clone", "kj", image=WEBP, mime="image/webp", step=1)
    finish("p1")
    _clean.registry = registry("p-next")  # the next prompt owns ComfyUI's registry now
    kj(_clean, "p1", "s.clone", "kj", image=WEBP, mime="image/webp", step=2)
    record = live("kj")["sources"]["kj"]["live"]
    assert record["via"] == "s" and record["step"] == 2


def test_a_pruned_run_is_never_recreated_by_a_late_write(_clean):
    queue("p1", graph())
    bound = sdk_runs.bound_run("p1")
    sdk_runs.reset_for_tests()
    with run_outputs._LOCK:
        run_outputs._prune_locked(set())
    run_outputs.record_live(bound, "s", {"publisher": "native", "via": "s"}, ("bytes", b"x"), 1)
    run_outputs.note_live_source_info("p1", "s", {"class_type": "KSampler", "title": ""})
    run_outputs.note_preview_method("p1", "auto")
    assert "p1" not in run_outputs._DATA and run_outputs.live_bytes_in_use() == 0


def test_sonder_app_payloads_no_read_shows_are_evicted_first(_clean, monkeypatch):
    def sdk_queue(prompt_id, token, run_id):
        prompt = graph(receipt="")
        prompt["ed"]["_meta"] = {"sonder_run": token}
        sdk_runs.register_token(token, [], run_id=run_id)
        sdk_runs.bind_queued(prompt_id, prompt, ["out"], workflow_id="tpl")

    sdk_queue("a1", "tok-a1", "run-a1")
    native4("a1", "s")
    sdk_queue("a2", "tok-a2", "run-a2")
    native4("a2", "s")
    queue("h1", graph())  # a canvas host's run, published after both
    native4("h1", "s")
    monkeypatch.setattr(run_outputs, "LIVE_BUDGET_BYTES", run_outputs.live_bytes_in_use() - 1)
    native4("h1", "s")  # over budget now: the older Sonder App payload goes first
    assert run_outputs.live_payload([("sdk", "run-a1")], "a1", "s")[0] == "unavailable"
    assert run_outputs.live_payload([("sdk", "run-a2")], "a2", "s")[0] == "ok"
    assert run_outputs.live_payload([OWNER], "h1", "s")[0] == "ok"


def test_cached_is_never_read_between_finished_and_ran(_clean, monkeypatch):
    queue("p1", graph())
    run_outputs.observe("executing", {"prompt_id": "p1", "node": "s"})
    native4("p1", "s")
    finish("p1")
    queue("p2", graph(receipt=R2), receipt=R2, key="2")
    run_outputs.observe("executing", {"prompt_id": "p2", "node": "s"})
    seen = {}
    original = sdk_runs.finish_bound

    def finish_and_read(*args, **kwargs):
        result = original(*args, **kwargs)
        seen["state"] = live("s")["sources"]["s"]["state"]  # a read racing the finish
        return result
    monkeypatch.setattr(sdk_runs, "finish_bound", finish_and_read)
    finish("p2", key="2")
    assert seen["state"] == "no_previews"


def test_capability_reports_native_only_with_the_wrap_in_place(_clean):
    sdk_progress.reset_for_tests()
    sp.install()
    assert sp.capability()["native"] is False  # observers registered, but nothing wraps send_sync
    assert sdk_progress.install(SimpleNamespace(send_sync=lambda *a, **kw: None))
    assert sp.capability()["native"] is True
    sdk_progress.reset_for_tests()


def test_announce_live_sends_ids_only_on_the_registry_loop_and_throttles(tmp_path, monkeypatch):
    from test_editor_node import _import_io_nodes
    io_nodes = _import_io_nodes(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(io_nodes, "schedule_project_event",
                        lambda pid, event, delay=0.0: sent.append((pid, event, delay)))
    captured = {}
    fake = SimpleNamespace(add_listener=lambda fn: captured.setdefault("all", fn),
                           add_live_listener=lambda fn: captured.setdefault("live", fn))
    monkeypatch.setattr(io_nodes, "_RUN_OUTPUT_HINTS_INSTALLED", False)
    io_nodes._LIVE_HINT_STATE.clear()
    io_nodes._install_run_output_hints(fake)
    hint = {"prompt_id": "p1", "seq": 3, "owners": [OWNER, ("sdk", "run-1")], "project_ids": ["proj", "folder"],
            "finished": False}
    for _ in range(3):
        captured["live"](dict(hint))
    assert [item[0] for item in sent] == ["proj", "folder", "proj", "folder"]
    first, trailing = sent[0], sent[2]
    assert first[2] == 0.0 and 0 < trailing[2] <= io_nodes.LIVE_HINT_PERIOD_SEC
    assert first[1] == {"type": "live_preview", "project_id": "proj", "prompt_id": "p1", "seq": 3,
                        "host_ids": ["host-1"], "run_ids": ["run-1"]}
    captured["all"]({**hint, "finished": True})
    assert "p1" not in io_nodes._LIVE_HINT_STATE
