"""Original take geometry in placement-clock frames, independent of encoding FPS.

Save Video retains its existing frame-for-frame mapping when encoding FPS differs.
Persist placement_fps separately for reconstruction of the original clock.
"""


def calculate_take_placement(context: dict, encoded_frame_count: int) -> dict:
    def integer(key, default=0):
        try:
            return int(context.get(key, default) or 0)
        except (TypeError, ValueError, OverflowError):
            return default

    count = max(0, int(encoded_frame_count))
    padding = max(0, integer("frame_count_padding"))
    available = max(0, count - padding)
    start = integer("selection_start")
    end = integer("selection_end", start + count)
    pre = max(0, integer("actual_pre_context_frames",
                         integer("pre_context_frames", integer("context_frames"))))
    post = max(0, integer("actual_post_context_frames",
                          integer("post_context_frames", integer("context_frames"))))
    # Retire defaults when all live execution-context producers carry requested_*.
    # Historical asset restoration must resolve its policy before using this math.
    before = min(pre, max(0, integer("requested_mask_pre_offset", integer("mask_pre_offset"))))
    after = min(post, max(0, integer("requested_mask_post_offset", integer("mask_post_offset"))))
    if context.get("take_placement_mode") == "untrimmed":
        source_in, source_out = 0, available
        timeline_start = start - pre
    else:
        source_in = min(available, pre - before)
        source_out = max(source_in, min(available, pre + max(0, end - start) + after))
        timeline_start = start - before
    return {
        "source_in_frame": source_in,
        "source_out_frame": source_out,
        "total_source_frames": available,
        "source_origin_frame": 0,
        "timeline_start_frame": timeline_start,
        "timeline_end_frame": timeline_start + source_out - source_in,
    }
