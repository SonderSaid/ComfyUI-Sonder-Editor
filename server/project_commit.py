import logging

from .project_manager import ProjectVersionConflict, load_project, save_project
from .timeline_state import TimelineProject

logger = logging.getLogger("sonder_editor")


def _asset_is_generated(asset) -> bool:
    params = getattr(asset, "generation_params", None) or {}
    return bool(params)


def _clip_is_generated(clip) -> bool:
    return bool(getattr(clip, "is_generated", False) or getattr(clip, "take_metadata", None))


def _audio_is_generated(track, generated_paths: set[str]) -> bool:
    return str(getattr(track, "source_path", "") or "") in generated_paths


def _merge_generated_outputs(current: TimelineProject, produced: TimelineProject) -> bool:
    changed = False
    current_assets = {asset.asset_id for asset in current.assets}
    generated_paths = set()

    for asset in produced.assets:
        if not _asset_is_generated(asset):
            continue
        generated_paths.add(str(getattr(asset, "path", "") or ""))
        if asset.asset_id in current_assets:
            continue
        current.assets.append(asset)
        current_assets.add(asset.asset_id)
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
        current_job.result_asset_id = job.result_asset_id
        if hasattr(current_job, "base_modified_at"):
            current_job.base_modified_at = getattr(job, "base_modified_at", "")
        changed = True

    current_scenes = {scene.scene_id: scene for scene in current.scenes}
    for produced_scene in produced.scenes:
        current_scene = current_scenes.get(produced_scene.scene_id)
        if not current_scene:
            continue

        # Track the highest lane index actually used by appended generated
        # content. The lane-config/count extension below honors these caps so
        # that lanes the user deleted are not resurrected when no generated
        # content was placed on them.
        max_lane_index = {
            "render": -1,
            "motion_driver": -1,
            "audio": -1,
        }

        current_clip_ids = {clip.clip_id for clip in current_scene.clips}
        for clip in produced_scene.clips:
            if clip.clip_id in current_clip_ids or not _clip_is_generated(clip):
                continue
            current_scene.clips.append(clip)
            current_clip_ids.add(clip.clip_id)
            role = getattr(clip, "role", "render") or "render"
            track_idx = int(getattr(clip, "track_index", 0) or 0)
            if role in max_lane_index:
                max_lane_index[role] = max(max_lane_index[role], track_idx)
            changed = True

        current_track_ids = {track.track_id for track in current_scene.audio_tracks}
        for track in produced_scene.audio_tracks:
            if track.track_id in current_track_ids or not _audio_is_generated(track, generated_paths):
                continue
            current_scene.audio_tracks.append(track)
            current_track_ids.add(track.track_id)
            lane_idx = int(getattr(track, "lane_index", 0) or 0)
            max_lane_index["audio"] = max(max_lane_index["audio"], lane_idx)
            changed = True

        # Map each lane attribute to the role that drives its content guard.
        config_role_map = (
            ("video_lane_configs", "render"),
            ("motion_driver_lane_configs", "motion_driver"),
            ("audio_lane_configs", "audio"),
        )
        for attr, role in config_role_map:
            current_configs = getattr(current_scene, attr, [])
            produced_configs = getattr(produced_scene, attr, [])
            # Only extend to cover lanes actually used by appended generated
            # content. `produced_configs` may be longer because of a tail the
            # user deleted on `current` — that tail stays deleted unless we
            # placed generated content on it.
            content_cap = max_lane_index[role] + 1
            target_len = min(len(produced_configs), max(len(current_configs), content_cap))
            if target_len > len(current_configs):
                current_configs.extend(produced_configs[len(current_configs):target_len])
                setattr(current_scene, attr, current_configs)
                changed = True

        count_role_map = (
            ("video_lane_count", "render"),
            ("motion_driver_lane_count", "motion_driver"),
            ("audio_lane_count", "audio"),
        )
        for attr, role in count_role_map:
            current_count = int(getattr(current_scene, attr, 0) or 0)
            produced_count = int(getattr(produced_scene, attr, 0) or 0)
            content_cap = max_lane_index[role] + 1
            target = min(produced_count, max(current_count, content_cap))
            if target > current_count:
                setattr(current_scene, attr, target)
                changed = True

    return changed


def save_generated_project(project: TimelineProject, base_modified_at: str = "") -> TimelineProject:
    """Save generated outputs without overwriting unrelated live editor changes."""
    if not base_modified_at:
        save_project(project)
        return project

    try:
        save_project(project, expected_modified_at=base_modified_at)
        return project
    except ProjectVersionConflict:
        current = load_project(project.project_dir)
        if not _merge_generated_outputs(current, project):
            raise
        save_project(current, expected_modified_at=current.modified_at)
        logger.info("Merged generated outputs into newer project version at %s", project.project_dir)
        return current
