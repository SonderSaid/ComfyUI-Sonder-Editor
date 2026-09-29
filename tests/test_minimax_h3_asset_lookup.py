"""H3 setup resolution reads asset media facts, never asset provenance.

`minimax_h3._asset_lookup` used to call `Asset.to_dict()`, which reads
`generation_params` and so hydrates every asset's provenance from its state
component. On a 757-asset project that was about 2 s of every read that
resolves an H3 setup (`plans/cut-read-fanout.md`, Phase 5). It now uses the
backing accessor, `to_dict(include_provenance=False)`. These tests pin:

1. Resolution with live `Asset` objects equals resolution with full records.
2. None of the reads that resolve a setup hydrates provenance, each checked
   on its own: the prompt compile, `bridge-references`, `prompt-payload` and
   the dormant summary.
"""

import asyncio
import copy
import json

import pytest

from server import minimax_h3, project_storage
from server.timeline_state import Asset

from test_change_certificates import (  # noqa: F401
    DummyRequest, _bridge, _compile, _project, route_module)


def _resolve(project, assets):
    scene = project.get_scene("scene")
    return minimax_h3.resolve_setup(
        setup=minimax_h3.implicit_reference_setup(),
        guide_frames=scene.guide_frames, reference_items=scene.reference_items,
        lane_recipes=scene.reference_lane_recipes,
        lane_configs=scene.reference_lane_configs,
        lane_count=scene.reference_lane_count, scene_duration=scene.duration_frames,
        window_start=0, window_end=scene.duration_frames,
        references=project.references, assets=assets,
        semantic_units=project.prompt_semantic_units)


def test_live_assets_resolve_exactly_like_full_records(tmp_path):
    project = _project(tmp_path)
    # A video in the audio lane exercises the silence and duration reads.
    project.assets.append(Asset(asset_id="vid_a", asset_type="video", duration_sec=3.2,
                                has_audio=False, has_audio_checked=True))
    member = project.references[0].members[1]
    for has_audio, checked in ((False, True), (False, False), (True, True)):
        for asset_id in ("aud_a", "vid_a"):
            member.asset_id = asset_id
            project.assets[-1].has_audio = has_audio
            project.assets[-1].has_audio_checked = checked
            live = _resolve(project, project.assets)
            full = _resolve(project, [asset.to_dict() for asset in project.assets])
            assert live == full, (asset_id, has_audio, checked)
            assert live["setup_manifest"], "the fixture must resolve a setup"


def _prompt_payload(route_module, monkeypatch, project):
    handler = next(route.handler for route in route_module.routes
                   if route.method == "GET" and route.path.endswith("/prompt-payload"))
    monkeypatch.setattr(route_module, "_load_scene_for_bridge",
                        lambda _request, sid: (project, project.get_scene(sid), None))
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"},
        query={"window_start": "40", "window_end": "100"})))
    assert response.status == 200
    return json.loads(response.body.decode("utf-8"))


def _dormant_summary(route_module, _monkeypatch, project):
    return route_module._build_dormant_summary(
        project, scene_id="scene", selection_start=40, selection_end=100)


READS = {
    "compile": lambda module, _monkeypatch, project: _compile(module, project, "scene", (0, 200)),
    "bridge-references": lambda module, monkeypatch, project: _bridge(
        module, monkeypatch, project, "scene", (40, 100)),
    "prompt-payload": _prompt_payload,
    "dormant summary": _dormant_summary,
}


@pytest.mark.parametrize("read", list(READS))
def test_a_read_that_resolves_a_setup_hydrates_no_provenance(
        route_module, monkeypatch, tmp_path, read):  # noqa: F811
    project = _project(tmp_path)
    # The reads that take the template from the project need it to be H3, or
    # they never resolve a setup and this would pass without proving anything.
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    expected = READS[read](route_module, monkeypatch, copy.deepcopy(project))
    for asset in project.assets:
        # As a format-3 load leaves them: provenance still in its component.
        asset.__dict__["_generation_unhydrated"] = True
    hydrated = []

    def refuse(asset):
        hydrated.append(asset.asset_id)
        raise AssertionError("setup resolution must not hydrate provenance")

    monkeypatch.setattr(project_storage, "hydrate_asset", refuse)
    assert READS[read](route_module, monkeypatch, project) == expected
    assert hydrated == []
