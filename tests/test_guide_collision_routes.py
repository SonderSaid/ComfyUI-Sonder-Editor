import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import routes
from server.timeline_state import Asset, ClipReference, GenerationJob, GuideFrame, Scene, TimelineProject


def test_queue_freezes_toggle_and_attaches_authoritative_prediction(tmp_path):
    project_dir = tmp_path / "project"
    media = project_dir / "media"
    media.mkdir(parents=True)
    (media / "guide.png").write_bytes(b"guide")
    (media / "driver.mp4").write_bytes(b"driver")

    project = TimelineProject(project_dir=str(project_dir), metadata={"guide_collision_auto_offset": False})
    project.assets = [
        Asset(asset_id="guide-asset", asset_type="image", path="media/guide.png"),
        Asset(asset_id="driver-asset", asset_type="video", path="media/driver.mp4"),
    ]
    scene = Scene(scene_id="scene", duration_frames=121)
    project.scenes = [scene]
    guide = GuideFrame(guide_id="guide-1", frame_index=0, asset_id="guide-asset")
    driver = ClipReference(
        clip_id="driver-1",
        source_path="media/driver.mp4",
        timeline_start_frame=0,
        timeline_end_frame=121,
        role="motion_driver",
        track_index=0,
    )
    job = GenerationJob(
        scene_id=scene.scene_id,
        selection_start=0,
        selection_end=121,
        guide_frame_snapshots=[guide.to_dict()],
        driver_clip_snapshots=[driver.to_dict()],
        frame_constraint={"step": 8, "offset": 1},
        params={"snapshot_version": 1},
    )

    routes._freeze_guide_collision_param(project, job)
    routes._queue_guide_collision_prediction(project, job)

    assert job.params["guide_collision_auto_offset"] is False
    prediction = job.params["guide_collision_prediction"]
    assert prediction["entries"][0]["effective_local_idx"] == 2
    assert prediction["predicted_unresolved"] is True
    assert prediction["execution_window"]["frame_count"] == 121
