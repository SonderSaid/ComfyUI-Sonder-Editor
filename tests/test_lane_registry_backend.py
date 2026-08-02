from dataclasses import asdict
from pathlib import Path

import pytest

from server import lane_registry
from server import project_commit
from server import routes
from server.timeline_state import AudioTrack, ClipReference, LaneConfig, Scene, TimelineProject


ROOT = Path(__file__).resolve().parents[1]


def _lane_surfaces(scene: Scene) -> dict:
    return {
        "video_lane_count": scene.video_lane_count,
        "motion_driver_lane_count": scene.motion_driver_lane_count,
        "audio_lane_count": scene.audio_lane_count,
        "video_lane_configs": [asdict(config) for config in scene.video_lane_configs],
        "motion_driver_lane_configs": [asdict(config) for config in scene.motion_driver_lane_configs],
        "audio_lane_configs": [asdict(config) for config in scene.audio_lane_configs],
        "guide_track_config": asdict(scene.guide_track_config),
        "prompt_track_config": asdict(scene.prompt_track_config),
        "global_prompt_track_config": asdict(scene.global_prompt_track_config),
    }


@pytest.mark.parametrize(
    "shape,configs,expected_names",
    [
        ("absent", None, ["", "", ""]),
        ("empty", [], ["", "", ""]),
        ("short", [{"name": "first"}], ["first", "", ""]),
        ("exact", [{"name": "first"}, {"name": "second"}, {"name": "third"}], ["first", "second", "third"]),
        ("long", [{"name": "first"}, {"name": "second"}, {"name": "third"}, {"name": "extra"}], ["first", "second", "third", "extra"]),
    ],
)
def test_scene_round_trip_preserves_lane_surface_shape_matrix(shape, configs, expected_names):
    payload = {
        "scene_id": f"lane-roundtrip-{shape}",
        "video_lane_count": 3,
        "motion_driver_lane_count": 3,
        "audio_lane_count": 3,
        "guide_track_config": {"name": "Guides", "locked": True},
        "prompt_track_config": {"name": "Prompt", "hidden": True},
        "global_prompt_track_config": {"name": "Global", "locked": True, "hidden": True},
    }
    if configs is not None:
        for field in (
            "video_lane_configs",
            "motion_driver_lane_configs",
            "audio_lane_configs",
        ):
            payload[field] = [dict(config) for config in configs]

    scene = Scene.from_dict(payload)
    before = _lane_surfaces(scene)
    after = _lane_surfaces(Scene.from_dict(scene.to_dict()))
    assert after == before
    for field in (
        "video_lane_configs",
        "motion_driver_lane_configs",
        "audio_lane_configs",
    ):
        assert [config["name"] for config in before[field]] == expected_names
    assert before["guide_track_config"] == asdict(LaneConfig(name="Guides", locked=True))
    assert before["prompt_track_config"] == asdict(LaneConfig(name="Prompt", hidden=True))
    assert before["global_prompt_track_config"] == asdict(
        LaneConfig(name="Global", locked=True, hidden=True)
    )


def test_padding_helpers_preserve_counts_and_do_not_trim_extra_configs():
    scene = Scene(
        video_lane_count=2,
        motion_driver_lane_count=3,
        audio_lane_count=1,
        video_lane_configs=[LaneConfig(name="one")],
        motion_driver_lane_configs=[],
        audio_lane_configs=[LaneConfig(), LaneConfig(name="extra")],
    )
    lane_registry.pad_lane_configs(scene, LaneConfig)
    assert len(scene.video_lane_configs) == 2
    assert len(scene.motion_driver_lane_configs) == 3
    assert len(scene.audio_lane_configs) == 2

    descriptor = lane_registry.ensure_lane_index(scene, "video", 4, LaneConfig)
    assert descriptor.count_attr == "video_lane_count"
    assert scene.video_lane_count == 5
    assert len(scene.video_lane_configs) == 5


def test_lane_items_and_descriptor_flags_preserve_family_behavior():
    clip = lambda role, lane, clip_id: type(
        "Clip", (), {"role": role, "track_index": lane, "clip_id": clip_id}
    )()
    audio = lambda lane, track_id: type(
        "Audio", (), {"lane_index": lane, "track_id": track_id}
    )()
    scene = Scene()
    scene.clips = [clip("render", 0, "render"), clip("motion_driver", 0, "driver"), clip("foo", 0, "unknown")]
    scene.audio_tracks = [audio(0, "audio")]

    assert [item.clip_id for item in lane_registry.lane_items(scene, "video", 0)] == ["render"]
    assert [item.clip_id for item in lane_registry.lane_items(scene, "motion_driver", 0)] == ["driver"]
    assert [item.track_id for item in lane_registry.lane_items(scene, "audio", 0)] == ["audio"]
    assert lane_registry.descriptor_for_lane_type("motion_driver").max_items_per_lane == 1
    assert not lane_registry.descriptor_for_lane_type("motion_driver").supports_compaction
    assert not lane_registry.descriptor_for_lane_type("motion_driver").supports_multi_lane_delete
    assert lane_registry.descriptor_for_lane_type("video").supports_compaction
    assert lane_registry.descriptor_for_lane_type("audio").supports_multi_lane_delete


@pytest.mark.parametrize("lane_type", ["guide", "prompt", "prompt_global", ""])
@pytest.mark.parametrize(
    "helper,args",
    [
        (routes._scene_lane_configs, ()),
        (routes._scene_lane_count, ()),
        (routes._set_scene_lane_count, (2,)),
    ],
)
def test_route_lane_helpers_preserve_exact_unknown_type_errors(lane_type, helper, args):
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        helper(Scene(), lane_type, *args)
    assert (raised.value.message, raised.value.status, raised.value.code) == (
        f"Unknown lane type: {lane_type}",
        400,
        "invalid_project_mutation",
    )


@pytest.mark.parametrize("lane_type", lane_registry.VARIABLE_LANE_TYPES)
def test_route_lane_helpers_resolve_registry_fields(lane_type):
    scene = Scene()
    descriptor = lane_registry.descriptor_for_lane_type(lane_type)
    assert routes._scene_lane_configs(scene, lane_type) is getattr(scene, descriptor.configs_attr)
    assert routes._scene_lane_count(scene, lane_type) == 1
    routes._set_scene_lane_count(scene, lane_type, 3)
    assert getattr(scene, descriptor.count_attr) == 3
    assert len(getattr(scene, descriptor.configs_attr)) == 3


def _scene_for_lane_removal(lane_type, state):
    descriptor = lane_registry.variable_descriptor(lane_type)
    count = 1 if state == "only_lane" else 3
    scene = Scene()
    setattr(scene, descriptor.count_attr, count)
    setattr(
        scene,
        descriptor.configs_attr,
        [LaneConfig(name=f"{lane_type}-{index}") for index in range(count)],
    )
    lane_index = 0 if state == "only_lane" else (count if state == "out_of_range" else 1)
    if state == "locked":
        getattr(scene, descriptor.configs_attr)[lane_index].locked = True
    if state == "occupied":
        if lane_type == "audio":
            scene.audio_tracks.append(
                AudioTrack(track_id="occupied", lane_index=lane_index, timeline_end_frame=4)
            )
        else:
            scene.clips.append(
                ClipReference(
                    clip_id="occupied",
                    role="motion_driver" if lane_type == "motion_driver" else "render",
                    track_index=lane_index,
                    timeline_end_frame=4,
                )
            )
    return scene, descriptor, lane_index


@pytest.mark.parametrize("item_policy", ["require_empty", "move_items", "delete_items"])
@pytest.mark.parametrize("lane_type", lane_registry.VARIABLE_LANE_TYPES)
@pytest.mark.parametrize("state", ["empty", "occupied", "only_lane", "out_of_range", "locked"])
def test_remove_media_lane_policy_family_state_matrix(item_policy, lane_type, state):
    scene, descriptor, lane_index = _scene_for_lane_removal(lane_type, state)

    expected_error = None
    if state == "occupied" and item_policy == "require_empty":
        expected_error = ("Lane is not empty", 409, "lane_not_empty")
    elif state == "only_lane":
        expected_error = ("Cannot remove the only lane", 409, "invalid_lane_operation")
    elif state == "out_of_range":
        expected_error = (f"Lane index out of range: {lane_index}", 404, "item_not_found")
    elif state == "locked":
        expected_error = ("Lane is locked", 409, "track_locked")

    if expected_error is not None:
        with pytest.raises(routes.ProjectMutationRequestError) as raised:
            routes._remove_media_lane(scene, lane_type, lane_index, item_policy)
        assert (raised.value.message, raised.value.status, raised.value.code) == expected_error
        return

    routes._remove_media_lane(scene, lane_type, lane_index, item_policy)
    assert getattr(scene, descriptor.count_attr) == 2
    assert [config.name for config in getattr(scene, descriptor.configs_attr)] == [
        f"{lane_type}-0",
        f"{lane_type}-2",
    ]
    remaining = lane_registry.lane_items(scene, lane_type, 0)
    if state == "occupied" and item_policy == "move_items":
        assert [getattr(item, descriptor.item_id_attr) for item in remaining] == ["occupied"]
    elif state == "occupied" and item_policy == "delete_items":
        assert remaining == []


def test_remove_driver_lane_move_revalidates_single_item_limit():
    scene, _, lane_index = _scene_for_lane_removal("motion_driver", "occupied")
    scene.clips.append(
        ClipReference(
            clip_id="destination",
            role="motion_driver",
            track_index=0,
            timeline_start_frame=10,
            timeline_end_frame=14,
        )
    )
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        routes._remove_media_lane(scene, "motion_driver", lane_index, "move_items", target_lane=0)
    assert (raised.value.message, raised.value.status, raised.value.code) == (
        "Only one driver clip is allowed per driver lane",
        409,
        "driver_lane_occupied",
    )


def test_generated_commit_skips_registry_only_future_lane_family(monkeypatch):
    future_descriptor = lane_registry.LaneDescriptor(
        track_type="reference",
        lane_type="reference",
        variable=True,
        header_controllable=True,
        count_attr="reference_lane_count",
        configs_attr="reference_lane_configs",
        items_attr="reference_items",
        item_index_attr="lane_index",
        item_id_attr="reference_item_id",
        item_predicate="all",
        lane_removable=True,
    )
    monkeypatch.setattr(
        project_commit,
        "VARIABLE_LANE_DESCRIPTORS",
        (*lane_registry.VARIABLE_LANE_DESCRIPTORS, future_descriptor),
    )
    current = TimelineProject(scenes=[Scene(scene_id="scene")])
    produced = TimelineProject(scenes=[Scene(scene_id="scene")])

    assert project_commit._merge_generated_outputs(current, produced) is False


def test_io_node_take_placement_uses_registry_padding_for_video_and_audio():
    source = (ROOT / "nodes" / "io_nodes.py").read_text(encoding="utf-8")
    assert source.count("ensure_lane_index(scene,") == 2
    assert 'ensure_lane_index(scene, "video", new_lane, LaneConfig)' in source
    assert 'ensure_lane_index(scene, "audio", new_audio_lane, LaneConfig)' in source
    assert "while len(scene.video_lane_configs)" not in source
    assert "while len(scene.audio_lane_configs)" not in source
