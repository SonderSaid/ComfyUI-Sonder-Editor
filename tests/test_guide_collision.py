import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.guide_collision import (
    check_frame_count_excess,
    driver_occupied_coords,
    driver_window_span,
    resolve_execution_window,
    resolve_guide_collisions,
    snap_driver_start,
)


CONSTRAINT = {"step": 8, "offset": 1}


def _resolve(guides, drivers, *, enabled=True, frame_count=121, constraint=CONSTRAINT):
    return resolve_guide_collisions(
        guides=guides,
        drivers=drivers,
        frame_count=frame_count,
        frame_constraint=constraint,
        auto_offset_enabled=enabled,
    )


def test_live_anchor_driver_zero_guide_zero_moves_to_two():
    result = _resolve(
        [{"guide_id": "g", "local_idx": 0}],
        [{"clip_id": "d", "lane_index": 0, "local_idx": 0, "pixel_len": 121}],
    )
    assert result["entries"][0]["effective_local_idx"] == 2
    assert result["predicted_unresolved"] is False


def test_live_anchor_driver_one_does_not_move_guide_zero():
    result = _resolve(
        [{"guide_id": "g", "local_idx": 0}],
        [{"clip_id": "d", "lane_index": 0, "local_idx": 1, "pixel_len": 120}],
    )
    assert result["entries"][0]["effective_local_idx"] == 0


def test_live_anchor_driver_zero_does_not_move_guide_two():
    result = _resolve(
        [{"guide_id": "g", "local_idx": 2}],
        [{"clip_id": "d", "lane_index": 0, "local_idx": 0, "pixel_len": 121}],
    )
    assert result["entries"][0]["effective_local_idx"] == 2


def test_guide_chain_is_deterministic_by_id():
    result = _resolve(
        [{"guide_id": "b", "local_idx": 0}, {"guide_id": "a", "local_idx": 0}],
        [{"clip_id": "d", "lane_index": 0, "local_idx": 0, "pixel_len": 121}],
    )
    assert [(e["guide_id"], e["effective_local_idx"]) for e in result["entries"]] == [
        ("a", 2), ("b", 3)
    ]


def test_backward_fallback_and_full_window_failure():
    result = _resolve(
        [{"guide_id": "g", "local_idx": 2}],
        [
            {"clip_id": "a", "lane_index": 0, "local_idx": 0, "pixel_len": 1},
            {"clip_id": "b", "lane_index": 1, "local_idx": 2, "pixel_len": 1},
        ],
        frame_count=3,
    )
    assert result["entries"][0]["effective_local_idx"] == 1
    with pytest.raises(ValueError):
        _resolve(
            [{"guide_id": "g", "local_idx": 0}],
            [
                {"clip_id": "a", "lane_index": 0, "local_idx": 0, "pixel_len": 1},
                {"clip_id": "b", "lane_index": 1, "local_idx": 1, "pixel_len": 1},
            ],
            frame_count=2,
        )


def test_disabled_offsets_are_suggestions_and_remain_unresolved():
    result = _resolve(
        [{"guide_id": "g", "local_idx": 0}],
        [{"clip_id": "d", "lane_index": 0, "local_idx": 0, "pixel_len": 121}],
        enabled=False,
    )
    assert result["entries"][0]["effective_local_idx"] == 2
    assert result["unresolved_collision_count"] == 1
    assert result["predicted_unresolved"] is True


def test_driver_driver_collisions_remain_unresolved_when_enabled():
    result = _resolve([], [
        {"clip_id": "a", "lane_index": 0, "local_idx": 0, "pixel_len": 17},
        {"clip_id": "b", "lane_index": 1, "local_idx": 0, "pixel_len": 17},
    ])
    assert result["driver_driver_collision_count"] == 3
    assert result["unresolved_collision_count"] == 3
    assert result["predicted_unresolved"] is True


def test_duplicate_ids_do_not_drop_real_injections():
    result = _resolve(
        [{"guide_id": "same", "local_idx": 2}, {"guide_id": "same", "local_idx": 2}],
        [],
    )
    assert [entry["guide_id"] for entry in result["entries"]] == ["same", "same#1"]
    assert result["entries"][1]["effective_local_idx"] == 3


def test_constraint_passthrough_and_coordinate_formulas():
    result = _resolve([{"guide_id": "g", "local_idx": 0}], [], constraint=None)
    assert result["entries"][0]["effective_local_idx"] == 0
    assert snap_driver_start(9, 8, 1) == 9
    assert driver_occupied_coords(0, 121, 8, 1) == [0, 1, 9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97, 105, 113]
    assert driver_occupied_coords(1, 120, 8, 1) == [1, 9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97, 105, 113]
    assert driver_occupied_coords(3, 1, 4, 1) == [3]


def test_frame_count_excess_predicate():
    assert check_frame_count_excess(121, 129, 8, 1)
    assert not check_frame_count_excess(121, 137, 8, 1)
    assert not check_frame_count_excess(121, 128, 8, 1)
    assert not check_frame_count_excess(121, 242, 8, 1)


def test_execution_window_matches_constraint_expansion_and_scene_edge():
    expanded = resolve_execution_window(
        scene_duration=300,
        selection_start=20,
        selection_end=100,
        pre_context_frames=8,
        post_context_frames=9,
        frame_constraint=CONSTRAINT,
    )
    assert expanded["actual_pre"] == 9
    assert expanded["actual_post"] == 16
    assert expanded["render_start"] == 11
    assert expanded["frame_count"] == 105

    edge = resolve_execution_window(
        scene_duration=100,
        selection_start=4,
        selection_end=98,
        pre_context_frames=4,
        post_context_frames=2,
        frame_constraint=CONSTRAINT,
    )
    assert edge["actual_pre"] == 4
    assert edge["actual_post"] == 2
    assert edge["render_start"] == 0
    assert edge["render_end"] == 100
    assert edge["frame_count"] == 105


def test_driver_window_span_carries_the_padding_tail_only_to_the_window_end():
    # [pre | gen | post | pad]: the window is 10..30 (20 real frames) plus a 7-frame tail.
    full = driver_window_span(0, 50, 10, 30, 7)
    assert full == {"overlap_start": 10, "overlap_end": 30, "local_idx": 0, "real_len": 20, "tail_len": 7}
    ends_at_window_end = driver_window_span(10, 30, 10, 30, 7)
    assert (ends_at_window_end["real_len"], ends_at_window_end["tail_len"]) == (20, 7)
    late_start = driver_window_span(22, 40, 10, 30, 7)
    assert (late_start["local_idx"], late_start["real_len"], late_start["tail_len"]) == (12, 8, 7)
    early_end = driver_window_span(0, 25, 10, 30, 7)
    assert (early_end["local_idx"], early_end["real_len"], early_end["tail_len"]) == (0, 15, 0)
    unpadded = driver_window_span(0, 50, 10, 30, 0)
    assert unpadded["tail_len"] == 0
    assert driver_window_span(30, 40, 10, 30, 7) is None
    assert driver_window_span(0, 10, 10, 30, 7) is None


def test_driver_window_span_keeps_a_single_frame_overlap_single():
    # One frame keeps the single exact coordinate the collision model gives an image;
    # with a tail the model would snap it to the grid like any longer span.
    single = driver_window_span(29, 40, 10, 30, 7)
    assert (single["local_idx"], single["real_len"], single["tail_len"]) == (19, 1, 0)
    assert driver_occupied_coords(single["local_idx"], single["real_len"], 8, 1) == [19]


def test_driver_tail_occupies_the_coordinates_the_guide_node_will_use():
    # LTX: 10 real frames round up to 17. With its 7-frame tail the Driver from local 2
    # spans 15 frames, so the collision model (conservative: it counts one latent more
    # than LTXVAddGuide keeps for a mid-video guide) reserves coordinate 9 as well as 1.
    window = resolve_execution_window(
        scene_duration=10, selection_start=0, selection_end=10, frame_constraint=CONSTRAINT,
    )
    assert (window["frame_count"], window["frame_count_padding"]) == (17, 7)
    span = driver_window_span(2, 10, 0, 10, window["frame_count_padding"])
    pixel_len = span["real_len"] + span["tail_len"]
    assert pixel_len == 15
    assert driver_occupied_coords(span["local_idx"], pixel_len, 8, 1) == [1, 9]
    result = _resolve(
        [{"guide_id": "g", "local_idx": 9}],
        [{"clip_id": "d", "local_idx": span["local_idx"], "pixel_len": pixel_len}],
        frame_count=window["frame_count"],
    )
    assert result["entries"][0]["effective_local_idx"] == 10
