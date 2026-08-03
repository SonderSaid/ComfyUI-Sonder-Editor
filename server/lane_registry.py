"""Pure lane-family metadata shared by backend timeline consumers.

This module intentionally imports nothing from ``server``.  It sits below the
timeline data model so routes, render/export code, and nodes can consume the
same persisted-field contract without creating an import cycle.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable


@dataclass(frozen=True)
class LaneDescriptor:
    track_type: str
    lane_type: str
    variable: bool
    header_controllable: bool
    count_attr: str = ""
    configs_attr: str = ""
    fixed_config_attr: str = ""
    items_attr: str = ""
    item_index_attr: str = ""
    item_id_attr: str = ""
    item_predicate: str = "none"
    max_items_per_lane: int | None = None
    supports_multi_lane_delete: bool = False
    supports_compaction: bool = False
    lane_removable: bool = False
    snapshot_count_attr: str = ""
    snapshot_configs_attr: str = ""
    recipe_attr: str = ""


LANE_DESCRIPTORS = (
    LaneDescriptor(
        track_type="video",
        lane_type="video",
        variable=True,
        header_controllable=True,
        count_attr="video_lane_count",
        configs_attr="video_lane_configs",
        items_attr="clips",
        item_index_attr="track_index",
        item_id_attr="clip_id",
        item_predicate="render",
        supports_multi_lane_delete=True,
        supports_compaction=True,
        lane_removable=True,
    ),
    LaneDescriptor(
        track_type="motion_driver",
        lane_type="motion_driver",
        variable=True,
        header_controllable=True,
        count_attr="motion_driver_lane_count",
        configs_attr="motion_driver_lane_configs",
        items_attr="clips",
        item_index_attr="track_index",
        item_id_attr="clip_id",
        item_predicate="motion_driver",
        max_items_per_lane=1,
        lane_removable=True,
        snapshot_count_attr="driver_lane_count",
        snapshot_configs_attr="driver_lane_configs",
    ),
    LaneDescriptor(
        track_type="audio",
        lane_type="audio",
        variable=True,
        header_controllable=True,
        count_attr="audio_lane_count",
        configs_attr="audio_lane_configs",
        items_attr="audio_tracks",
        item_index_attr="lane_index",
        item_id_attr="track_id",
        item_predicate="all",
        supports_multi_lane_delete=True,
        supports_compaction=True,
        lane_removable=True,
    ),
    LaneDescriptor(
        track_type="guides",
        lane_type="guide",
        variable=False,
        header_controllable=True,
        fixed_config_attr="guide_track_config",
        items_attr="guide_frames",
        item_index_attr="frame_index",
        item_id_attr="guide_id",
        item_predicate="all",
    ),
    LaneDescriptor(
        track_type="prompt_global",
        lane_type="prompt_global",
        variable=False,
        header_controllable=True,
        fixed_config_attr="global_prompt_track_config",
    ),
    LaneDescriptor(
        track_type="prompt",
        lane_type="prompt",
        variable=False,
        header_controllable=True,
        fixed_config_attr="prompt_track_config",
        items_attr="prompt_sections",
        item_index_attr="start_frame",
        item_id_attr="prompt_id",
        item_predicate="all",
    ),
    LaneDescriptor(
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
        snapshot_count_attr="reference_lane_count",
        snapshot_configs_attr="reference_lane_configs",
        recipe_attr="reference_lane_recipes",
    ),
)

LANE_DESCRIPTORS_BY_TRACK_TYPE = MappingProxyType(
    {descriptor.track_type: descriptor for descriptor in LANE_DESCRIPTORS}
)
LANE_DESCRIPTORS_BY_LANE_TYPE = MappingProxyType(
    {descriptor.lane_type: descriptor for descriptor in LANE_DESCRIPTORS}
)

# This order is a backend reconciliation contract, not frontend presentation
# order.  In particular Driver intentionally precedes audio here.
VARIABLE_LANE_TYPES = ("video", "motion_driver", "audio", "reference")
VARIABLE_LANE_DESCRIPTORS = tuple(
    LANE_DESCRIPTORS_BY_LANE_TYPE[lane_type] for lane_type in VARIABLE_LANE_TYPES
)


def descriptor_for_track_type(track_type: str) -> LaneDescriptor | None:
    return LANE_DESCRIPTORS_BY_TRACK_TYPE.get(str(track_type or ""))


def descriptor_for_lane_type(lane_type: str) -> LaneDescriptor | None:
    return LANE_DESCRIPTORS_BY_LANE_TYPE.get(str(lane_type or ""))


def variable_descriptor(lane_type: str) -> LaneDescriptor | None:
    descriptor = descriptor_for_lane_type(lane_type)
    return descriptor if descriptor is not None and descriptor.variable else None


def is_render_clip(clip) -> bool:
    return getattr(clip, "role", "render") in ("", "render")


def clip_lane_type(clip) -> str:
    # Preserve the current backend fallback: every non-render role, including
    # an unknown role, is treated as Driver here.
    return "video" if is_render_clip(clip) else "motion_driver"


def item_matches_descriptor(item, descriptor: LaneDescriptor) -> bool:
    if descriptor.item_predicate == "render":
        return is_render_clip(item)
    if descriptor.item_predicate == "motion_driver":
        return getattr(item, "role", "render") == "motion_driver"
    return descriptor.item_predicate == "all"


def lane_items(scene, lane_type: str, lane_index: int) -> list:
    descriptor = variable_descriptor(lane_type)
    if descriptor is None:
        raise KeyError(lane_type)
    return [
        item
        for item in (getattr(scene, descriptor.items_attr, []) or [])
        if item_matches_descriptor(item, descriptor)
        and int(getattr(item, descriptor.item_index_attr, 0) or 0) == lane_index
    ]


def pad_config_list(configs: list, count: int, factory: Callable[[], object]) -> list:
    while len(configs) < count:
        configs.append(factory())
    return configs


def pad_lane_configs(scene, factory: Callable[[], object]) -> None:
    for descriptor in VARIABLE_LANE_DESCRIPTORS:
        configs = getattr(scene, descriptor.configs_attr)
        count = getattr(scene, descriptor.count_attr)
        pad_config_list(configs, count, factory)


def pad_lane_recipes(scene, factory: Callable[[], object]) -> None:
    """Pad recipe-bearing lane families without coupling this leaf module to models."""
    for descriptor in VARIABLE_LANE_DESCRIPTORS:
        if not descriptor.recipe_attr:
            continue
        recipes = getattr(scene, descriptor.recipe_attr)
        count = getattr(scene, descriptor.count_attr)
        pad_config_list(recipes, count, factory)


def trim_lane_recipes(
    recipes: list,
    removed_index: int,
    target_count: int,
    factory: Callable[[], object],
) -> None:
    """Remove the recipe at the same index as its lane, then reconcile length."""
    if 0 <= removed_index < len(recipes):
        recipes.pop(removed_index)
    while len(recipes) > target_count:
        recipes.pop()
    pad_config_list(recipes, target_count, factory)


def ensure_lane_index(
    scene,
    lane_type: str,
    lane_index: int,
    factory: Callable[[], object],
) -> LaneDescriptor:
    # Current callers create only video/audio lanes. A future caller for a
    # recipe-bearing lane must also pad descriptor.recipe_attr explicitly.
    descriptor = variable_descriptor(lane_type)
    if descriptor is None:
        raise KeyError(lane_type)
    current_count = getattr(scene, descriptor.count_attr)
    if current_count <= lane_index:
        setattr(scene, descriptor.count_attr, lane_index + 1)
    pad_config_list(
        getattr(scene, descriptor.configs_attr),
        getattr(scene, descriptor.count_attr),
        factory,
    )
    return descriptor


def hidden_lane_indexes(scene, lane_type: str) -> set[int]:
    descriptor = variable_descriptor(lane_type)
    if descriptor is None:
        raise KeyError(lane_type)
    return {
        index
        for index, config in enumerate(getattr(scene, descriptor.configs_attr, []) or [])
        if getattr(config, "hidden", False)
    }
