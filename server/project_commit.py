import logging

from .project_manager import ProjectVersionConflict, load_project, save_project
from .timeline_state import LaneConfig, TimelineProject

logger = logging.getLogger("sonder_editor")


def _asset_is_generated(asset) -> bool:
    params = getattr(asset, "generation_params", None) or {}
    return bool(params)


def _clip_is_generated(clip) -> bool:
    return bool(getattr(clip, "is_generated", False) or getattr(clip, "take_metadata", None))


def _normalize_asset_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip()


def _audio_is_generated(track, generated_paths: set[str]) -> bool:
    return _normalize_asset_path(getattr(track, "source_path", "")) in generated_paths


def _same_path_placeholder_can_upgrade(asset) -> bool:
    return (
        not str(getattr(asset, "trashed_at", "") or "")
        and not str(getattr(asset, "prompt", "") or "")
        and not (getattr(asset, "generation_params", None) or {})
        and not str(getattr(asset, "folder", "") or "")
    )


def _asset_has_generated_registration(asset) -> bool:
    return bool(
        str(getattr(asset, "prompt", "") or "")
        or (getattr(asset, "generation_params", None) or {})
    )


def _copy_generated_asset_registration(target, source) -> bool:
    changed = False
    for attr in (
        "name",
        "asset_type",
        "artifact_kind",
        "path",
        "prompt",
        "generation_params",
        "width",
        "height",
        "frame_count",
        "fps",
        "duration_sec",
        "sample_rate",
        "has_audio",
        "folder",
    ):
        value = getattr(source, attr, None)
        if getattr(target, attr, None) != value:
            setattr(target, attr, value)
            changed = True

    for attr in ("has_audio_checked", "duration_checked"):
        value = bool(getattr(source, attr, False) or getattr(target, attr, False))
        if getattr(target, attr, None) != value:
            setattr(target, attr, value)
            changed = True
    source_signature = str(getattr(source, "media_probe_signature", "") or "")
    if source_signature and getattr(target, "media_probe_signature", "") != source_signature:
        target.media_probe_signature = source_signature
        changed = True
    return changed


def _remap_asset_id_values(value, asset_id_remap: dict[str, str]):
    if isinstance(value, str):
        return asset_id_remap.get(value, value)
    if isinstance(value, list):
        return [_remap_asset_id_values(item, asset_id_remap) for item in value]
    if isinstance(value, dict):
        return {
            key: _remap_asset_id_values(item, asset_id_remap)
            for key, item in value.items()
        }
    return value


def snapshot_item_ids(project) -> dict:
    """Capture the clip/asset/audio-track ids currently in a project. Diff a
    later snapshot against this to identify items created in between (the basis
    for telling 'generated this run' from 'pre-existing generated, user-deleted')."""
    clips, assets, audio = set(), set(), set()
    for asset in getattr(project, "assets", None) or []:
        assets.add(asset.asset_id)
    for scene in getattr(project, "scenes", None) or []:
        for clip in getattr(scene, "clips", None) or []:
            clips.add(clip.clip_id)
        for track in getattr(scene, "audio_tracks", None) or []:
            audio.add(track.track_id)
    return {"clips": clips, "assets": assets, "audio": audio}


def created_ids_since(before: dict, project) -> dict:
    """Ids present in `project` now but absent from the `before` snapshot — the
    clips/assets/audio created since `before` was taken."""
    after = snapshot_item_ids(project)
    return {key: after[key] - (before.get(key) or set()) for key in after}


def _append_generated_lanes(items, idx_attr, current_count, current_configs):
    """Append generated items onto the committed lane space.

    Each item's source lane index (from ``produced``) is remapped: indices that
    still exist in ``current`` are kept; indices beyond ``current``'s lane count
    are assigned fresh appended lanes. This replaces the old absolute-position
    config slice, which duplicated or resurrected lanes when the user deleted a
    non-tail lane mid-generation (``current``'s indices shifted relative to
    ``produced``).

    Appended lanes get a fresh default ``LaneConfig`` — matching take placement,
    which appends a default config — rather than copying ``produced``'s config by
    index, which could duplicate a *surviving* lane's identity when the take's
    produced index coincides with a shifted user lane.

    Mutates ``current_configs`` and each item's index attribute in place;
    returns the new lane count.
    """
    src_indices = sorted({int(getattr(it, idx_attr, 0) or 0) for it in items})
    remap = {}
    next_new = current_count
    for src in src_indices:
        if src < current_count:
            remap[src] = src
        else:
            remap[src] = next_new
            next_new += 1
    for it in items:
        setattr(it, idx_attr, remap[int(getattr(it, idx_attr, 0) or 0)])
    while len(current_configs) < next_new:
        current_configs.append(LaneConfig())
    return next_new


def _merge_generated_outputs(current: TimelineProject, produced: TimelineProject, created_ids=None, asset_id_remap=None) -> bool:
    # When `created_ids` is provided ({"clips","assets","audio"} → id sets), ONLY
    # items created during this run are merged in. Generated items the user
    # deleted mid-generation are still present in `produced` (the start-of-run
    # snapshot) but are NOT in `created_ids`, so they are not resurrected. When
    # `created_ids` is None, fall back to legacy "add any generated item not in
    # current" (preserves behavior for callers that do not supply the set).
    created_assets = created_ids.get("assets") if created_ids else None
    created_clips = created_ids.get("clips") if created_ids else None
    created_audio = created_ids.get("audio") if created_ids else None

    if asset_id_remap is None:
        asset_id_remap = {}

    changed = False
    current_assets_by_id = {asset.asset_id: asset for asset in current.assets}
    current_assets = set(current_assets_by_id)
    current_assets_by_path = {}
    for current_asset in current.assets:
        normalized_path = _normalize_asset_path(getattr(current_asset, "path", ""))
        if normalized_path and normalized_path not in current_assets_by_path:
            current_assets_by_path[normalized_path] = current_asset
    generated_paths = set()
    touched_assets = []

    for asset in produced.assets:
        is_generated = _asset_is_generated(asset)
        normalized_path = _normalize_asset_path(getattr(asset, "path", ""))
        if is_generated:
            generated_paths.add(normalized_path)
        if asset.asset_id in current_assets:
            current_same_id = current_assets_by_id.get(asset.asset_id)
            if (
                current_same_id is not None
                and _asset_has_generated_registration(asset)
                and _same_path_placeholder_can_upgrade(current_same_id)
                and _normalize_asset_path(getattr(current_same_id, "path", "")) == normalized_path
            ):
                changed = _copy_generated_asset_registration(current_same_id, asset) or changed
                touched_assets.append(current_same_id)
            continue
        if created_assets is not None:
            if asset.asset_id not in created_assets:
                continue
        elif not is_generated:
            continue
        current_same_path = current_assets_by_path.get(normalized_path)
        if current_same_path is not None:
            asset_id_remap[asset.asset_id] = current_same_path.asset_id
            if _same_path_placeholder_can_upgrade(current_same_path):
                changed = _copy_generated_asset_registration(current_same_path, asset) or changed
                touched_assets.append(current_same_path)
            continue
        current.assets.append(asset)
        current_assets.add(asset.asset_id)
        current_assets_by_id[asset.asset_id] = asset
        if normalized_path:
            current_assets_by_path.setdefault(normalized_path, asset)
        touched_assets.append(asset)
        changed = True

    if asset_id_remap:
        for asset in touched_assets:
            params = getattr(asset, "generation_params", None)
            if not params:
                continue
            remapped = _remap_asset_id_values(params, asset_id_remap)
            if remapped != params:
                asset.generation_params = remapped
                changed = True

    current_jobs = {job.job_id: job for job in current.generation_queue}
    for job in produced.generation_queue:
        current_job = current_jobs.get(job.job_id)
        if not current_job:
            continue
        produced_status = str(getattr(job, "status", "") or "").lower()
        if produced_status not in {"running", "completed", "failed"}:
            continue
        current_job.status = job.status
        current_job.progress = job.progress
        current_job.error = job.error
        current_job.completed_at = job.completed_at
        current_job.result_asset_id = asset_id_remap.get(job.result_asset_id, job.result_asset_id)
        if hasattr(current_job, "base_modified_at"):
            current_job.base_modified_at = getattr(job, "base_modified_at", "")
        changed = True

    current_scenes = {scene.scene_id: scene for scene in current.scenes}
    for produced_scene in produced.scenes:
        current_scene = current_scenes.get(produced_scene.scene_id)
        if not current_scene:
            continue

        # Collect generated items to add (dedup by stable id). Generated clips
        # are split by role because render and motion-driver lanes are separate
        # index spaces.
        current_clip_ids = {clip.clip_id for clip in current_scene.clips}
        new_render = []
        new_driver = []
        for clip in produced_scene.clips:
            if clip.clip_id in current_clip_ids:
                continue
            if created_clips is not None:
                if clip.clip_id not in created_clips:
                    continue
            elif not _clip_is_generated(clip):
                continue
            current_clip_ids.add(clip.clip_id)
            role = getattr(clip, "role", "render") or "render"
            (new_driver if role == "motion_driver" else new_render).append(clip)

        current_track_ids = {track.track_id for track in current_scene.audio_tracks}
        new_audio = []
        for track in produced_scene.audio_tracks:
            if track.track_id in current_track_ids:
                continue
            if created_audio is not None:
                if track.track_id not in created_audio:
                    continue
            elif not _audio_is_generated(track, generated_paths):
                continue
            current_track_ids.add(track.track_id)
            new_audio.append(track)

        # Reconcile onto the COMMITTED (current) lane space by appending new
        # lanes — never by copying produced's base configs by absolute position,
        # which duplicated/resurrected lanes when the user deleted a non-tail
        # lane mid-generation (current's indices shifted relative to produced).
        role_specs = (
            ("video_lane_count", "video_lane_configs", "track_index", new_render, "clips"),
            ("motion_driver_lane_count", "motion_driver_lane_configs", "track_index", new_driver, "clips"),
            ("audio_lane_count", "audio_lane_configs", "lane_index", new_audio, "audio_tracks"),
        )
        for count_attr, configs_attr, idx_attr, items, list_attr in role_specs:
            if not items:
                continue
            current_count = int(getattr(current_scene, count_attr, 0) or 0)
            current_configs = list(getattr(current_scene, configs_attr, []) or [])
            new_count = _append_generated_lanes(
                items, idx_attr, current_count, current_configs
            )
            getattr(current_scene, list_attr).extend(items)
            setattr(current_scene, configs_attr, current_configs)
            setattr(current_scene, count_attr, new_count)
            changed = True

    return changed


def save_generated_project(project: TimelineProject, base_modified_at: str = "", created_ids=None) -> TimelineProject:
    """Save generated outputs without overwriting unrelated live editor changes.

    `created_ids` (optional {"clips","assets","audio"} → id sets, from
    `created_ids_since`) restricts the conflict-path merge to items created during
    this run, so generated items the user deleted mid-generation are not
    resurrected. When omitted, legacy add-any-generated-item behavior applies.

    The returned project may carry transient `_asset_id_remap` data when a
    conflict merge reconciles a generated asset onto an existing same-path
    auto-sync placeholder while preserving the placeholder's observed ID."""
    if not base_modified_at:
        save_project(project)
        setattr(project, "_asset_id_remap", {})
        return project

    try:
        save_project(project, expected_modified_at=base_modified_at)
        setattr(project, "_asset_id_remap", {})
        return project
    except ProjectVersionConflict:
        current = load_project(project.project_dir)
        asset_id_remap = {}
        changed = _merge_generated_outputs(current, project, created_ids, asset_id_remap=asset_id_remap)
        if not changed and not asset_id_remap:
            raise
        if changed:
            save_project(current, expected_modified_at=current.modified_at)
        setattr(current, "_asset_id_remap", asset_id_remap)
        logger.info("Merged generated outputs into newer project version at %s", project.project_dir)
        return current
