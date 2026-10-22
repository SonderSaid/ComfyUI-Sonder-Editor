"""Proxy routes (``server/routes.py``, Proxy media section).

* The file route serves only the current ready rev, with Range and an immutable cache
  header; anything else is 404. Errors carry codes, never paths.
* ``GET /proxies`` reports settings, per-asset state with a usage category, and counts
  and sizes per category. Delete protects unchecked categories and always takes the
  proxies of trashed assets; Regenerate never creates proxies for decodable assets.
* Bodies are validated; a foreign Origin is refused; the project settings PUT validates
  the two enums and a timing change reaches the worker.
* The lean ``/assets`` list carries each video asset's proxy state from one folder listing.
"""

import asyncio
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server import external_links, proxy_media as pmx, project_manager as pm
from server.timeline_state import Asset, ClipReference, ReferenceEntity, ReferenceMember, Scene
from test_phase43_routes import _load_route_module
from test_proxy_media import fake_encode, make_worker, sources, wait_until  # noqa: F401  (fixture)
from legacy_projects import create_legacy_project


@pytest.fixture
def env(tmp_path, monkeypatch, sources):
    out = tmp_path / "out"
    base = out / "sonder-projects"
    base.mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_user_directory=lambda: str(tmp_path / "user"), get_output_directory=lambda: str(out),
        get_temp_directory=lambda: str(tmp_path / "temp")))
    monkeypatch.setattr(external_links, "is_enabled", lambda: False)
    worker = make_worker()
    monkeypatch.setattr(pmx, "_worker", worker)
    module = _load_route_module(monkeypatch)
    project = create_legacy_project("Film", base)
    media = Path(project.project_dir, "media")
    files = {"scene": "prores.mov", "ref": "ffv1.mkv", "unused": "odd.mov", "trash": "mjpeg.avi",
             "plain": "h264.mp4"}
    project.assets = []
    for asset_id, file in files.items():
        target = media / f"{asset_id}_{file}"
        target.write_bytes((sources / file).read_bytes())
        project.assets.append(Asset(asset_id=asset_id, name=file, asset_type="video",
                                    path=f"media/{asset_id}_{file}", width=96, height=54, fps=24.0))
    project.assets.append(Asset(asset_id="img", name="still.png", asset_type="image", path="media/still.png"))
    project.assets[3].trashed_at = "2026-10-21T00:00:00"
    scene = Scene(scene_id="s1", name="One", duration_frames=24)
    scene.clips = [ClipReference(source_path="media/scene_prores.mov", timeline_start_frame=0, timeline_end_frame=12)]
    project.scenes = [scene]
    project.references = [ReferenceEntity(reference_id="r1", name="Hero",
                                          members=[ReferenceMember(asset_id="ref")])]
    pm.save_project(project, notify=False)
    yield types.SimpleNamespace(module=module, worker=worker, project_dir=project.project_dir,
                                folder=os.path.basename(project.project_dir), tmp_path=tmp_path)
    worker.stop()


def client_for(module):
    app = web.Application(middlewares=[
        module._sonder_security_middleware, module._project_error_middleware,
        module._project_version_header_middleware,
    ])
    app.add_routes(module.routes)
    return TestClient(TestServer(app))


def run(env, scenario):
    async def main():
        async with client_for(env.module) as client:
            return await scenario(client, f"/sonder-editor/project/{env.folder}")
    return asyncio.run(main())


def build_all(env, ids):
    env.worker.enqueue(env.project_dir, ids, reason=pmx.REASON_USER)
    wait_until(lambda: all(env.worker.states(env.project_dir, [(i, {}, "")])[i]["state"] == "ready" for i in ids))


def test_the_file_route_serves_only_the_current_rev_with_range(env):
    build_all(env, ["scene"])
    rev = env.worker.states(env.project_dir, [("scene", {}, "")])["scene"]["rev"]

    async def scenario(client, prefix):
        full = await client.get(f"{prefix}/proxy/scene/{rev}.mp4")
        body = await full.read()
        ranged = await client.get(f"{prefix}/proxy/scene/{rev}.mp4", headers={"Range": "bytes=0-99"})
        part = await ranged.read()
        stale = await client.get(f"{prefix}/proxy/scene/{'0' * 16}.mp4")
        other = await client.get(f"{prefix}/proxy/ref/{rev}.mp4")
        bad = await client.get(f"{prefix}/proxy/..%2Fscene/{rev}.mp4")
        missing = await client.get(f"/sonder-editor/project/Nope/proxy/scene/{rev}.mp4")
        return full, body, ranged, part, stale, await stale.json(), other.status, bad.status, missing

    full, body, ranged, part, stale, stale_body, other, bad, missing = run(env, scenario)
    assert full.status == 200 and len(body) > 100
    assert full.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert full.headers["Content-Type"] == "video/mp4"
    assert ranged.status == 206 and len(part) == 100 and ranged.headers["Content-Range"].startswith("bytes 0-99/")
    assert stale.status == 404 and stale_body == {"error": "This proxy is not the current one",
                                                  "code": "proxy_not_current"}
    assert other == 404 and bad in (400, 404) and missing.status == 404
    assert str(env.tmp_path) not in json.dumps(stale_body)


def test_overview_reports_categories_counts_and_sizes(env):
    build_all(env, ["scene", "ref", "unused", "trash"])

    async def scenario(client, prefix):
        response = await client.get(f"{prefix}/proxies")
        return response.status, await response.json()

    status, payload = run(env, scenario)
    assert status == 200
    assert payload["settings"] == {"preset": "High Quality MP4", "timing": "idle"}
    assert payload["presets"] == list(pmx.PROXY_PRESETS) and payload["timings"] == ["idle", "now", "manual"]
    categories = {asset_id: item["category"] for asset_id, item in payload["items"].items()}
    assert categories == {"scene": "scenes", "ref": "references", "unused": "unused", "trash": "trashed",
                          "plain": "unused"}
    assert "img" not in payload["items"]
    assert {name: value["count"] for name, value in payload["categories"].items()} == \
        {"scenes": 1, "references": 1, "unused": 1, "trashed": 1}
    assert payload["total"]["count"] == 4 and payload["total"]["bytes"] == sum(
        value["bytes"] for value in payload["categories"].values()) > 0


def test_delete_protects_unchecked_categories_and_always_takes_trashed(env):
    build_all(env, ["scene", "ref", "unused", "trash"])

    async def scenario(client, prefix):
        response = await client.post(f"{prefix}/proxies/delete", json={"categories": ["unused"]})
        return response.status, await response.json()

    status, payload = run(env, scenario)
    assert status == 200 and payload["removed"]["count"] == 2
    states = env.worker.states(env.project_dir, [(i, {}, "") for i in ("scene", "ref", "unused", "trash")])
    assert {i: s["state"] for i, s in states.items()} == {
        "scene": "ready", "ref": "ready", "unused": "manual_only", "trash": "manual_only"}


def test_regenerate_rebuilds_existing_and_never_mass_creates_for_decodable_assets(env):
    build_all(env, ["scene"])
    before = env.worker.states(env.project_dir, [("scene", {}, "")])["scene"]["rev"]

    async def scenario(client, prefix):
        response = await client.post(f"{prefix}/proxies/regenerate",
                                     json={"categories": ["scenes", "references", "unused"]})
        return response.status, await response.json()

    status, payload = run(env, scenario)
    assert status == 200
    # The ids it queued, so the page can say what became of each.
    assert set(payload["asset_ids"]) >= {"scene"} and "trash" not in payload["asset_ids"]
    assert payload["queue"]["epoch"] > 0 and "builds" in payload["queue"]
    # scene (ready) is rebuilt; ref and unused want proxies; plain (H.264) is left alone.
    # Trashed assets are not in any category the dialog can tick.
    wait_until(lambda: env.worker.states(env.project_dir, [("scene", {}, "")])["scene"]["rev"] not in ("", before))
    wait_until(lambda: env.worker.states(env.project_dir, [("unused", {}, "")])["unused"]["state"] == "ready")
    plain = env.worker.states(env.project_dir, [("plain", {}, "")])["plain"]
    assert plain["state"] == "none" and plain["decision"] in ("pending", "decodable")


def test_bodies_are_validated_and_errors_carry_codes(env):
    async def scenario(client, prefix):
        results = []
        for path, body in [
            ("build", {"asset_ids": "scene"}),
            ("build", {"asset_ids": []}),
            ("build", {"asset_ids": ["../x"]}),
            ("build", {"asset_ids": ["scene"], "mode": "later"}),
            ("build", {"asset_ids": ["scene"], "rebuild": "yes"}),
            ("build", {"asset_ids": ["img"]}),
            ("regenerate", {"categories": ["everything"]}),
            ("delete", {}),
            ("delete", {"categories": ["unused"], "asset_ids": ["scene"]}),
            ("demand", {"asset_ids": [1]}),
            ("runtime-undecodable", {"asset_id": "scene", "variant": "proxy", "rev": "nope"}),
        ]:
            response = await client.post(f"{prefix}/proxies/{path}", json=body)
            results.append((path, response.status, await response.json()))
        raw = await client.post(f"{prefix}/proxies/build", data="{", headers={"Content-Type": "application/json"})
        results.append(("raw", raw.status, await raw.json()))
        return results

    for path, status, payload in run(env, scenario):
        assert status == 400, (path, payload)
        assert payload.get("code"), (path, payload)
        assert str(env.tmp_path) not in json.dumps(payload)


def test_a_foreign_origin_is_refused(env):
    async def scenario(client, prefix):
        response = await client.post(f"{prefix}/proxies/build", json={"asset_ids": ["scene"]},
                                     headers={"Origin": "http://evil.example"})
        return response.status

    assert run(env, scenario) == 403
    assert not env.worker.undecided and not env.worker.jobs


def test_build_cancel_demand_runtime_and_reconcile(env):
    started = threading.Event()
    env.worker._encode = fake_encode(delay=3.0, started=started)

    async def scenario(client, prefix):
        built = await (await client.post(f"{prefix}/proxies/build",
                                         json={"asset_ids": ["scene"], "mode": "now"})).json()
        await asyncio.to_thread(started.wait, 10)
        env.worker._encode = fake_encode()
        cancelled = await (await client.post(f"{prefix}/proxies/cancel")).json()
        demand = await client.post(f"{prefix}/proxies/demand", json={"asset_ids": ["ref"]})
        runtime = await (await client.post(f"{prefix}/proxies/runtime-undecodable",
                                           json={"asset_id": "plain"})).json()
        reconciled = await (await client.post(f"{prefix}/proxies/reconcile")).json()
        return built, cancelled, demand.status, runtime, reconciled

    built, cancelled, demand, runtime, reconciled = run(env, scenario)
    assert built["queued"] == 1 and "queue" in built
    # The summary a request leaves carries an epoch minted after it acted, newer than
    # any proxy_status event read before it (the page's progress compares the two).
    assert built["queue"]["user_pending"] >= 1 and built["queue"]["epoch"] > 0
    assert cancelled["queue"]["epoch"] > built["queue"]["epoch"]
    # The Build now was still undecided or building: Cancel stopped it (user-started).
    assert cancelled["cancelled"] == 1 and demand == 200
    # plain had never been decided, so a runtime report finds no decision to correct yet
    # and still records one.
    assert runtime == {"accepted": True}
    assert reconciled["queued"] >= 2 and reconciled["swept"] == 0
    wait_until(lambda: env.worker.states(env.project_dir, [("ref", {}, "")])["ref"]["state"] == "ready")
    wait_until(lambda: env.worker.states(env.project_dir, [("plain", {}, "")])["plain"]["state"] == "ready")
    assert env.worker.states(env.project_dir, [("plain", {}, "")])["plain"]["decision"] == "runtime"


def test_project_settings_put_validates_the_enums_and_reaches_the_worker(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.worker, "settings_changed", lambda project_dir: calls.append(project_dir))

    async def scenario(client, prefix):
        bad = await client.put(prefix, json={"metadata": {"proxy_build_timing": "never"}})
        bad_preset = await client.put(prefix, json={"metadata": {"proxy_preset": "ProRes 422 HQ"}})
        good = await client.put(prefix, json={"metadata": {"proxy_build_timing": "manual",
                                                           "proxy_preset": "Compatible MP4"}})
        preset_only = await client.put(prefix, json={"metadata": {"proxy_preset": "High Quality MP4"}})
        return (bad.status, await bad.json(), bad_preset.status, good.status, preset_only.status)

    bad, bad_body, bad_preset, good, preset_only = run(env, scenario)
    assert bad == 400 and bad_body["code"] == "invalid_proxy_setting" and bad_preset == 400
    assert good == 200 and preset_only == 200
    metadata = json.loads(Path(env.project_dir, "project.json").read_text(encoding="utf-8"))["metadata"]
    assert metadata["proxy_build_timing"] == "manual" and metadata["proxy_preset"] == "High Quality MP4"
    assert len(calls) == 1


def test_registration_gives_the_worker_the_routes_resolver(env):
    assert env.worker.resolve is env.module._project_dir_for_folder


def test_the_lean_asset_list_carries_proxy_state_from_one_listing(env, monkeypatch):
    build_all(env, ["scene"])
    # A missing proxy (the decision says it wants one) must not re-read project.json.
    pmx.write_sidecar(pmx.proxies_root(env.project_dir), "unused", {"decision": "undecodable", "state": "none"})
    listings = []
    real_listing = pmx._listing
    monkeypatch.setattr(pmx, "_listing", lambda root: listings.append(root) or real_listing(root))
    monkeypatch.setattr(pmx, "_read_document", lambda *a: pytest.fail("project.json re-read"))

    async def scenario(client, prefix):
        response = await client.get(f"{prefix}/assets?include_trashed=1")
        return response.status, await response.json()

    status, payload = run(env, scenario)
    assert status == 200 and len(listings) == 1
    by_id = {asset["asset_id"]: asset for asset in payload["assets"]}
    assert by_id["scene"]["proxy"]["state"] == "ready" and len(by_id["scene"]["proxy"]["rev"]) == 16
    assert by_id["scene"]["proxy"]["decision"] == "undecodable"
    assert "proxy" in by_id["plain"] and "proxy" not in by_id["img"]
