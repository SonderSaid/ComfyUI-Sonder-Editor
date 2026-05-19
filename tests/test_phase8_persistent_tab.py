import asyncio
import os
import tempfile

import pytest

from server.project_commit import save_generated_project
from server.project_manager import (
    ProjectVersionConflict,
    create_project,
    load_project,
    save_project,
)
from server.session_registry import (
    claim_session,
    create_handoff,
    get_canvas_host,
    get_project_debug_state,
    get_owner,
    get_widget_state,
    heartbeat_session,
    register_canvas_host,
    release_session,
    refresh_canvas_host,
    seed_widget_state,
    sweep_stale_sessions_once,
    unregister_canvas_host,
    update_widget_state,
)
from server.timeline_state import Asset, AudioTrack, ClipReference, LaneConfig, Scene


async def _claim_tab_owner(project_id: str, host_id: str, source_node_id: str = "node-a", session_id: str = "tab-1"):
    await release_session(project_id, "cleanup", force=True, host_id=host_id)
    fullscreen = await claim_session(project_id, f"{session_id}-fs", "fullscreen", {"host_id": host_id, "source_node_id": source_node_id})
    assert fullscreen["ok"] is True
    handoff = await create_handoff(project_id, f"{session_id}-fs", host_id=host_id, source_node_id=source_node_id)
    assert handoff["ok"] is True
    tab = await claim_session(project_id, session_id, "tab", {"host_id": host_id, "source_node_id": source_node_id}, handoff_token=handoff["token"])
    assert tab["ok"] is True
    return tab


def test_versioned_save_rejects_stale_writer():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Phase 8 Conflict", base_dir=base_dir)
        base_version = project.modified_at

        current = load_project(project.project_dir)
        current.name = "Current"
        save_project(current, expected_modified_at=base_version)

        stale = load_project(project.project_dir)
        stale.name = "Stale"
        with pytest.raises(ProjectVersionConflict):
            save_project(stale, expected_modified_at=base_version)

        restored = load_project(project.project_dir)
        assert restored.name == "Current"


def test_generated_commit_merges_into_newer_editor_version():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Phase 8 Merge", base_dir=base_dir)
        scene = Scene(scene_id="scene-1", name="Scene", duration_frames=12)
        project.scenes.append(scene)
        save_project(project)

        produced = load_project(project.project_dir)
        base_version = produced.modified_at

        current = load_project(project.project_dir)
        current.get_scene("scene-1").name = "Edited Scene"
        save_project(current, expected_modified_at=base_version)

        generated_asset = Asset(
            asset_id="asset-1",
            name="take.mp4",
            asset_type="video",
            path=os.path.join("media", "take.mp4"),
            generation_params={"save_preset": "test"},
        )
        produced.assets.append(generated_asset)
        produced.get_scene("scene-1").clips.append(ClipReference(
            clip_id="clip-1",
            source_path=generated_asset.path,
            timeline_start_frame=0,
            timeline_end_frame=12,
            is_generated=True,
        ))

        save_generated_project(produced, base_version)

        restored = load_project(project.project_dir)
        assert restored.get_scene("scene-1").name == "Edited Scene"
        assert restored.get_asset("asset-1") is not None
        assert restored.get_scene("scene-1").clips[0].clip_id == "clip-1"


def test_generated_commit_does_not_resurrect_empty_tail_lanes():
    """The merge must not bring back lanes the user deleted unless the
    produced output actually places generated content on those lanes."""
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Phase 8 Lane Tail", base_dir=base_dir)
        scene = Scene(scene_id="scene-1", name="Scene", duration_frames=12)
        # Base project has 4 video lanes, each with its own config.
        scene.video_lane_count = 4
        scene.video_lane_configs = [
            LaneConfig(name="V0"),
            LaneConfig(name="V1"),
            LaneConfig(name="V2"),
            LaneConfig(name="V3"),
        ]
        # Also exercise audio lane tail.
        scene.audio_lane_count = 3
        scene.audio_lane_configs = [
            LaneConfig(name="A0"),
            LaneConfig(name="A1"),
            LaneConfig(name="A2"),
        ]
        project.scenes.append(scene)
        save_project(project)

        produced = load_project(project.project_dir)
        base_version = produced.modified_at

        # Editor side: user deletes the tail video lane (V3) and the tail
        # audio lane (A2) while execution is in flight.
        current = load_project(project.project_dir)
        current_scene = current.get_scene("scene-1")
        current_scene.video_lane_count = 3
        current_scene.video_lane_configs = current_scene.video_lane_configs[:3]
        current_scene.audio_lane_count = 2
        current_scene.audio_lane_configs = current_scene.audio_lane_configs[:2]
        save_project(current, expected_modified_at=base_version)

        # Produced side: generated content placed on existing lanes only
        # (lane index 0). Nothing on the deleted tails.
        generated_asset = Asset(
            asset_id="asset-1",
            name="take.mp4",
            asset_type="video",
            path=os.path.join("media", "take.mp4"),
            generation_params={"save_preset": "test"},
        )
        produced.assets.append(generated_asset)
        produced.get_scene("scene-1").clips.append(ClipReference(
            clip_id="clip-1",
            source_path=generated_asset.path,
            timeline_start_frame=0,
            timeline_end_frame=12,
            is_generated=True,
            track_index=0,
        ))

        save_generated_project(produced, base_version)

        restored = load_project(project.project_dir)
        restored_scene = restored.get_scene("scene-1")
        # Deleted tail lanes stay deleted because no generated content used them.
        assert restored_scene.video_lane_count == 3
        assert len(restored_scene.video_lane_configs) == 3
        assert restored_scene.audio_lane_count == 2
        assert len(restored_scene.audio_lane_configs) == 2
        # The generated clip is still merged in.
        assert restored_scene.clips[0].clip_id == "clip-1"


def test_generated_commit_extends_lanes_only_when_generated_content_uses_them():
    """Inverse of the above: when generated content lands on a higher lane
    index than current has, the lane count and configs must grow to fit."""
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Phase 8 Lane Grow", base_dir=base_dir)
        scene = Scene(scene_id="scene-1", name="Scene", duration_frames=12)
        scene.video_lane_count = 2
        scene.video_lane_configs = [LaneConfig(name="V0"), LaneConfig(name="V1")]
        project.scenes.append(scene)
        save_project(project)

        produced = load_project(project.project_dir)
        base_version = produced.modified_at
        # Produced side adds a fresh tail lane and places content on it.
        produced_scene = produced.get_scene("scene-1")
        produced_scene.video_lane_count = 3
        produced_scene.video_lane_configs.append(LaneConfig(name="V2_take"))

        # Editor side does an unrelated edit so the merge path is exercised.
        current = load_project(project.project_dir)
        current.get_scene("scene-1").name = "Edited"
        save_project(current, expected_modified_at=base_version)

        generated_asset = Asset(
            asset_id="asset-1",
            name="take.mp4",
            asset_type="video",
            path=os.path.join("media", "take.mp4"),
            generation_params={"save_preset": "test"},
        )
        produced.assets.append(generated_asset)
        produced.get_scene("scene-1").clips.append(ClipReference(
            clip_id="clip-1",
            source_path=generated_asset.path,
            timeline_start_frame=0,
            timeline_end_frame=12,
            is_generated=True,
            track_index=2,
        ))

        save_generated_project(produced, base_version)

        restored = load_project(project.project_dir)
        restored_scene = restored.get_scene("scene-1")
        assert restored_scene.name == "Edited"
        assert restored_scene.video_lane_count == 3
        assert len(restored_scene.video_lane_configs) == 3
        assert restored_scene.video_lane_configs[2].name == "V2_take"
        assert restored_scene.clips[0].clip_id == "clip-1"


def test_tab_session_requires_handoff_and_transfers_owner():
    async def run():
        project_id = "phase-8-session"
        host_a = "canvas-a:node-1"
        host_b = "canvas-b:node-1"
        await release_session(project_id, "cleanup", force=True, host_id=host_a)
        await release_session(project_id, "cleanup", force=True, host_id=host_b)

        direct_tab = await claim_session(project_id, "tab-1", "tab", {"host_id": host_a, "source_node_id": "node-1"})
        assert direct_tab["ok"] is False
        assert direct_tab["code"] == "invalid_handoff"

        fullscreen = await claim_session(project_id, "fs-1", "fullscreen", {"host_id": host_a, "source_node_id": "node-1"})
        assert fullscreen["ok"] is True

        independent = await claim_session(project_id, "fs-2", "fullscreen", {"host_id": host_b, "source_node_id": "node-1"})
        assert independent["ok"] is True

        handoff = await create_handoff(project_id, "fs-1", host_id=host_a)
        assert handoff["ok"] is True
        assert handoff["token"]

        wrong_host = await claim_session(project_id, "tab-wrong", "tab", {"host_id": host_b}, handoff_token=handoff["token"])
        assert wrong_host["ok"] is False

        tab = await claim_session(project_id, "tab-1", "tab", {"host_id": host_a, "source_node_id": "node-1"}, handoff_token=handoff["token"])
        assert tab["ok"] is True

        owner = await get_owner(project_id, host_id=host_a)
        assert owner["session_id"] == "tab-1"
        assert owner["host_mode"] == "tab"

        other_owner = await get_owner(project_id, host_id=host_b)
        assert other_owner["session_id"] == "fs-2"

        await release_session(project_id, "tab-1", host_id=host_a)
        assert await get_owner(project_id, host_id=host_a) is None
        assert (await get_owner(project_id, host_id=host_b))["session_id"] == "fs-2"
        await release_session(project_id, "fs-2", host_id=host_b)

    asyncio.run(run())


def test_widget_state_is_scoped_by_source_node_and_whitelisted():
    async def run():
        project_id = "phase-8-widget-state"
        await release_session(project_id, "cleanup", force=True, host_id="host-a")
        await release_session(project_id, "cleanup", force=True, host_id="host-b")

        seeded = await seed_widget_state(project_id, "node-a", "session-a", {
            "scene_id": "scene-a",
            "selection_start": 4,
            "selection_end": 9,
            "ignored": "nope",
        }, host_id="host-a")
        assert seeded["values"] == {
            "scene_id": "scene-a",
            "selection_start": 4,
            "selection_end": 9,
        }

        await update_widget_state(project_id, "node-a", "", {"scene_id": "scene-b"}, host_id="host-b")

        node_a = await get_widget_state(project_id, "node-a", host_id="host-a")
        node_b = await get_widget_state(project_id, "node-a", host_id="host-b")
        assert node_a["state"]["scene_id"] == "scene-a"
        assert node_a["state"]["selection_start"] == 4
        assert "ignored" not in node_a["state"]
        assert node_b["state"]["scene_id"] == "scene-b"

    asyncio.run(run())


def test_canvas_host_presence_is_source_node_scoped():
    async def run():
        project_id = "phase-8-host-presence"
        await release_session(project_id, "cleanup", force=True, host_id="host-a")
        await seed_widget_state(project_id, "node-a", "session-a", {"scene_id": "scene-a"}, host_id="host-a")

        before = await get_widget_state(project_id, "node-a", host_id="host-a")
        assert before["canvas_host_connected"] is False

        await register_canvas_host(project_id, "node-a", "canvas-session", workflow_label="Workflow A", host_id="host-a")
        connected = await get_widget_state(project_id, "node-a", host_id="host-a")
        late_presence = await get_canvas_host(project_id, "node-a", host_id="host-a")
        debug = await get_project_debug_state(project_id, "node-a", host_id="host-a")
        other = await get_widget_state(project_id, "node-b", host_id="host-b")
        assert connected["canvas_host_connected"] is True
        assert connected["host"]["workflow_label"] == "Workflow A"
        assert late_presence["host_id"] == "host-a"
        assert late_presence["source_node_id"] == "node-a"
        assert debug["canvas_host_connected"] is True
        assert debug["matching_host"]["host_id"] == "host-a"
        assert debug["hosts"][0]["source_node_id"] == "node-a"
        assert other["canvas_host_connected"] is False

        refreshed = await refresh_canvas_host(project_id, "host-a", "node-a", "canvas-session")
        assert refreshed["ok"] is True

        await unregister_canvas_host(project_id, "node-a", "canvas-session", host_id="host-a")
        disconnected = await get_widget_state(project_id, "node-a", host_id="host-a")
        assert disconnected["canvas_host_connected"] is False

    asyncio.run(run())


def test_canvas_host_survives_sixty_second_heartbeat_gap():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-host-ttl-grace"
        host_id = "host-ttl-grace"
        await register_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)

        key = registry._host_key(project_id, host_id, "node-a")
        registry._canvas_hosts[key]["last_seen"] = registry._now() - 60

        state = await get_widget_state(project_id, "node-a", host_id=host_id)
        assert state["canvas_host_connected"] is True
        assert (await get_canvas_host(project_id, "node-a", host_id=host_id)) is not None

        await unregister_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)

    asyncio.run(run())


def test_canvas_host_eviction_after_extended_ttl_blocks_tab_write():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-host-ttl-evict"
        host_id = "host-ttl-evict"
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-ttl")
        await register_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)

        key = registry._host_key(project_id, host_id, "node-a")
        registry._canvas_hosts[key]["last_seen"] = registry._now() - registry.CANVAS_HOST_TTL_SECONDS - 1

        rejected = await update_widget_state(project_id, "node-a", "tab-ttl", {"selection_start": 12}, host_id=host_id)
        assert rejected["ok"] is False
        assert rejected["code"] == "canvas_host_disconnected"
        assert await get_canvas_host(project_id, "node-a", host_id=host_id) is None

        await release_session(project_id, "tab-ttl", host_id=host_id)

    asyncio.run(run())


def test_tab_owner_becomes_orphaned_not_released_after_session_ttl():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-tab-orphan"
        host_id = "host-orphan"
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-orphan")
        await seed_widget_state(project_id, "node-a", "tab-orphan", {"scene_id": "scene-a"}, host_id=host_id)

        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0
        owner = await get_owner(project_id, host_id=host_id)

        assert owner["session_id"] == "tab-orphan"
        assert owner["status"] == "orphaned"
        assert owner["orphan_expires_at"] > 0

        state = await get_widget_state(project_id, "node-a", host_id=host_id)
        assert state["state"]["scene_id"] == "scene-a"

        blocked = await claim_session(project_id, "fs-other", "fullscreen", {"host_id": host_id, "source_node_id": "node-a"})
        assert blocked["ok"] is False
        assert blocked["code"] == "locked"
        assert blocked["owner"]["status"] == "orphaned"

        await release_session(project_id, "tab-orphan", force=True, host_id=host_id)

    asyncio.run(run())


def test_orphaned_owner_reactivates_on_same_session_heartbeat():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-tab-reactivate"
        host_id = "host-reactivate"
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-reactivate")
        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0

        orphaned = await get_owner(project_id, host_id=host_id)
        assert orphaned["status"] == "orphaned"

        heartbeat = await heartbeat_session(project_id, "tab-reactivate", host_id=host_id, source_node_id="node-a")
        assert heartbeat["ok"] is True
        assert heartbeat["owner"]["status"] == "active"
        assert heartbeat["owner"]["orphan_expires_at"] == 0.0

        await release_session(project_id, "tab-reactivate", host_id=host_id)

    asyncio.run(run())


def test_orphaned_owner_releases_after_orphan_ttl_and_clears_widget_state():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-tab-orphan-expiry"
        host_id = "host-expiry"
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-expiry")
        await seed_widget_state(project_id, "node-a", "tab-expiry", {"selection_start": 7}, host_id=host_id)
        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0

        owner = await get_owner(project_id, host_id=host_id)
        assert owner["status"] == "orphaned"
        registry._sessions[(project_id, host_id)]["orphan_expires_at"] = registry._now() - 1

        evicted = await sweep_stale_sessions_once()
        assert evicted >= 1
        assert await get_owner(project_id, host_id=host_id) is None
        state = await get_widget_state(project_id, "node-a", host_id=host_id)
        assert state["state"] == {}

    asyncio.run(run())


def test_force_release_clears_orphaned_owner_and_widget_state():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-force-release-orphan"
        host_id = "host-force-orphan"
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-force")
        await seed_widget_state(project_id, "node-a", "tab-force", {"selection_end": 12}, host_id=host_id)
        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0

        owner = await get_owner(project_id, host_id=host_id)
        assert owner["status"] == "orphaned"

        released = await release_session(project_id, "other-session", force=True, host_id=host_id)
        assert released["ok"] is True
        assert await get_owner(project_id, host_id=host_id) is None
        state = await get_widget_state(project_id, "node-a", host_id=host_id)
        assert state["state"] == {}

    asyncio.run(run())


def test_canvas_host_presence_survives_owner_orphan_and_release():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-host-owner-independent"
        host_id = "host-independent"
        await register_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-independent")
        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0

        owner = await get_owner(project_id, host_id=host_id)
        assert owner["status"] == "orphaned"
        assert (await get_canvas_host(project_id, "node-a", host_id=host_id)) is not None

        registry._sessions[(project_id, host_id)]["orphan_expires_at"] = registry._now() - 1
        await sweep_stale_sessions_once()
        assert await get_owner(project_id, host_id=host_id) is None
        assert (await get_canvas_host(project_id, "node-a", host_id=host_id)) is not None

        await unregister_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)

    asyncio.run(run())


def test_tab_widget_write_rejected_while_owner_orphaned_until_reactivated():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-tab-orphan-write"
        host_id = "host-orphan-write"
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-write")
        await register_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)
        await seed_widget_state(project_id, "node-a", "canvas-session", {"selection_start": 2}, host_id=host_id)
        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0

        owner = await get_owner(project_id, host_id=host_id)
        assert owner["status"] == "orphaned"

        rejected = await update_widget_state(project_id, "node-a", "tab-write", {"selection_start": 9}, host_id=host_id)
        assert rejected["ok"] is False
        assert rejected["code"] == "session_orphaned"
        state = await get_widget_state(project_id, "node-a", host_id=host_id)
        assert state["state"]["selection_start"] == 2

        heartbeat = await heartbeat_session(project_id, "tab-write", host_id=host_id, source_node_id="node-a")
        assert heartbeat["ok"] is True
        assert heartbeat["owner"]["status"] == "active"

        accepted = await update_widget_state(project_id, "node-a", "tab-write", {"selection_start": 9}, host_id=host_id)
        assert accepted["ok"] is True
        assert accepted["state"]["selection_start"] == 9

        await unregister_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)
        await release_session(project_id, "tab-write", host_id=host_id)

    asyncio.run(run())


def test_heartbeat_idempotent_no_session_changed_event_when_status_unchanged():
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-heartbeat-idempotent"
        host_id = "host-idempotent"
        await claim_session(project_id, "fs-idempotent", "fullscreen", {"host_id": host_id, "source_node_id": "node-a"})

        events = []
        original_schedule = registry.schedule_project_event
        registry.schedule_project_event = lambda project, event: events.append((project, event))
        try:
            heartbeat = await heartbeat_session(project_id, "fs-idempotent", host_id=host_id, source_node_id="node-a")
            assert heartbeat["ok"] is True
            assert events == []
        finally:
            registry.schedule_project_event = original_schedule
            await release_session(project_id, "fs-idempotent", host_id=host_id)

    asyncio.run(run())


def test_sweeper_releases_stale_session_and_clears_widget_state():
    async def run():
        project_id = "phase-8-stale-sweep"
        await release_session(project_id, "cleanup", force=True, host_id="host-a")
        await release_session(project_id, "cleanup", force=True, host_id="host-b")
        await claim_session(project_id, "stale-session", "fullscreen", {"host_id": "host-a", "source_node_id": "node-a"})
        await claim_session(project_id, "fresh-session", "fullscreen", {"host_id": "host-b", "source_node_id": "node-b"})
        await seed_widget_state(project_id, "node-a", "stale-session", {"scene_id": "scene-a"}, host_id="host-a")
        await seed_widget_state(project_id, "node-b", "fresh-session", {"scene_id": "scene-b"}, host_id="host-b")

        import server.session_registry as registry

        registry._sessions[(project_id, "host-a")]["last_seen"] = 0.0
        evicted = await sweep_stale_sessions_once()

        assert evicted >= 1
        assert await get_owner(project_id, host_id="host-a") is None
        assert (await get_owner(project_id, host_id="host-b"))["session_id"] == "fresh-session"
        state = await get_widget_state(project_id, "node-a", host_id="host-a")
        assert state["state"] == {}
        fresh_state = await get_widget_state(project_id, "node-b", host_id="host-b")
        assert fresh_state["state"]["scene_id"] == "scene-b"

        await release_session(project_id, "fresh-session", host_id="host-b")

    asyncio.run(run())


def test_tab_widget_write_requires_live_canvas_host():
    async def run():
        project_id = "phase-8-tab-gating"
        host_id = "canvas-a:node-a"
        await release_session(project_id, "cleanup", force=True, host_id=host_id)

        fullscreen = await claim_session(project_id, "fs-1", "fullscreen", {"host_id": host_id, "source_node_id": "node-a"})
        assert fullscreen["ok"] is True
        handoff = await create_handoff(project_id, "fs-1", host_id=host_id)
        tab = await claim_session(project_id, "tab-1", "tab", {"host_id": host_id, "source_node_id": "node-a"}, handoff_token=handoff["token"])
        assert tab["ok"] is True

        rejected = await update_widget_state(project_id, "node-a", "tab-1", {"selection_start": 12}, host_id=host_id)
        assert rejected["ok"] is False
        assert rejected["code"] == "canvas_host_disconnected"

        await register_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)
        accepted = await update_widget_state(project_id, "node-a", "tab-1", {"selection_start": 12}, host_id=host_id)
        assert accepted["ok"] is True
        assert accepted["values"]["selection_start"] == 12

        await release_session(project_id, "tab-1", host_id=host_id)
        stale_tab = await update_widget_state(project_id, "node-a", "tab-1", {"selection_start": 18}, host_id=host_id)
        assert stale_tab["ok"] is False
        assert stale_tab["code"] == "no_owner"

        await unregister_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)

    asyncio.run(run())


def test_fullscreen_owner_still_released_immediately_on_session_ttl():
    """Fullscreen owners have no orphan grace: a stale heartbeat moves directly
    to released and widget state is cleared, distinguishing them from tab
    owners which transition via the orphaned state.
    """
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-fullscreen-no-orphan"
        host_id = "host-fullscreen"
        await release_session(project_id, "cleanup", force=True, host_id=host_id)

        claimed = await claim_session(
            project_id,
            "fs-stale",
            "fullscreen",
            {"host_id": host_id, "source_node_id": "node-a"},
        )
        assert claimed["ok"] is True
        await seed_widget_state(
            project_id,
            "node-a",
            "fs-stale",
            {"scene_id": "scene-fs"},
            host_id=host_id,
        )

        # Force the owner past SESSION_TTL_SECONDS without entering an orphan
        # grace window (fullscreen has none).
        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0

        # Lifecycle advancement is lazy; touch get_owner to trigger it.
        owner = await get_owner(project_id, host_id=host_id)
        assert owner is None

        state = await get_widget_state(project_id, "node-a", host_id=host_id)
        assert state["state"] == {}

    asyncio.run(run())


def test_widget_state_cleared_on_explicit_release_non_orphaned():
    """An active (not orphaned) tab owner that releases via release_session
    with a matching session_id (force=False) must clear widget state. This is
    distinct from force-release of an orphaned owner.
    """
    async def run():
        project_id = "phase-8-explicit-release-active"
        host_id = "host-active-release"
        await release_session(project_id, "cleanup", force=True, host_id=host_id)

        await _claim_tab_owner(project_id, host_id, "node-a", "tab-active-release")
        await register_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)
        await seed_widget_state(
            project_id,
            "node-a",
            "tab-active-release",
            {"selection_start": 4, "selection_end": 7},
            host_id=host_id,
        )

        owner = await get_owner(project_id, host_id=host_id)
        assert owner is not None
        assert owner["status"] == "active"

        released = await release_session(
            project_id,
            "tab-active-release",
            force=False,
            host_id=host_id,
        )
        assert released["ok"] is True
        assert released["owner"] is None
        assert await get_owner(project_id, host_id=host_id) is None

        state = await get_widget_state(project_id, "node-a", host_id=host_id)
        assert state["state"] == {}

        await unregister_canvas_host(project_id, "node-a", "canvas-session", host_id=host_id)

    asyncio.run(run())


def test_concurrent_claim_and_release_same_session_race():
    """Concurrent claim_session and release_session for the same session_id
    against a fresh (no-owner) host must produce a deterministic outcome under
    the registry's single asyncio.Lock. State must not be partially-applied
    (no orphaned ghost owner, no leaked widget state).
    """
    async def run():
        project_id = "phase-8-race-claim-release"
        host_id = "host-race"
        await release_session(project_id, "cleanup", force=True, host_id=host_id)

        claim_coro = claim_session(
            project_id,
            "race-session",
            "fullscreen",
            {"host_id": host_id, "source_node_id": "node-a"},
        )
        release_coro = release_session(
            project_id,
            "race-session",
            force=False,
            host_id=host_id,
        )
        claim_result, release_result = await asyncio.gather(claim_coro, release_coro)

        assert claim_result["ok"] is True
        # release_result is deterministic given the registry lock serializes
        # the two operations. Both orderings are valid outcomes:
        #   (a) claim acquired first -> release sees owner with matching
        #       session -> release ok, owner removed.
        #   (b) release acquired first -> no owner -> release ok with owner
        #       None -> claim runs, owner present.
        assert release_result["ok"] is True

        owner = await get_owner(project_id, host_id=host_id)
        if owner is None:
            # Order (a): owner removed by the release. Widget state cleared.
            state = await get_widget_state(project_id, "node-a", host_id=host_id)
            assert state["state"] == {}
        else:
            # Order (b): owner present and active; never partially-applied.
            assert owner["session_id"] == "race-session"
            assert owner["status"] == "active"
            assert owner["orphan_expires_at"] == 0.0
            await release_session(project_id, "race-session", host_id=host_id)

    asyncio.run(run())


def test_concurrent_claim_and_force_release_from_different_session():
    """Concurrent claim_session(new_session_id) and force release_session by
    a different session against an orphaned owner. The registry lock
    serializes both operations; whichever runs second observes the cleared
    state. Documented expected ordering: force-release wins by clearing the
    orphan before the new claim runs, or the new claim sees the orphan and
    is rejected.
    """
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-race-claim-force"
        host_id = "host-race-force"
        await release_session(project_id, "cleanup", force=True, host_id=host_id)

        # Set up an orphaned owner (claim, then advance past SESSION_TTL,
        # then trigger lifecycle).
        await _claim_tab_owner(project_id, host_id, "node-a", "tab-stale")
        registry._sessions[(project_id, host_id)]["last_seen"] = 0.0
        orphaned = await get_owner(project_id, host_id=host_id)
        assert orphaned["status"] == "orphaned"

        # Establish a canvas host so the new fullscreen claim, if it wins,
        # would still need handoff_token semantics. We use fullscreen here
        # since fullscreen does not require a handoff token.
        new_claim = claim_session(
            project_id,
            "fs-new",
            "fullscreen",
            {"host_id": host_id, "source_node_id": "node-a"},
        )
        force_release = release_session(
            project_id,
            "different-session",
            force=True,
            host_id=host_id,
        )
        claim_result, release_result = await asyncio.gather(new_claim, force_release)

        # Force-release always succeeds (force ignores session match).
        assert release_result["ok"] is True

        owner = await get_owner(project_id, host_id=host_id)
        if claim_result["ok"]:
            # Ordering: force-release ran first, cleared the orphan, then
            # claim found a clean key and succeeded.
            assert owner is not None
            assert owner["session_id"] == "fs-new"
            assert owner["status"] == "active"
            await release_session(project_id, "fs-new", host_id=host_id)
        else:
            # Ordering: claim ran first while the owner was orphaned ->
            # rejected as locked; then force-release cleared the orphan and
            # the host is now empty.
            assert claim_result["code"] == "locked"
            assert owner is None

    asyncio.run(run())


def test_session_changed_event_payloads_on_active_orphaned_released_transitions():
    """Pin the session_changed event payload shape (host_mode, session_id,
    status, source_node_id, workflow_label) across the three lifecycle
    transitions: active->orphaned, orphaned->active, orphaned->released.
    """
    async def run():
        import server.session_registry as registry

        project_id = "phase-8-event-payloads"
        host_id = "host-events"
        await release_session(project_id, "cleanup", force=True, host_id=host_id)

        # Capture session_changed events via the schedule_project_event
        # indirection (matches the idempotent-heartbeat test pattern).
        events: list[tuple[str, dict]] = []
        original_schedule = registry.schedule_project_event
        registry.schedule_project_event = lambda project, event: events.append((project, event))
        try:
            await _claim_tab_owner(project_id, host_id, "node-a", "tab-events")
            # Drop initial claim/handoff events; we're interested in the
            # transitions below.
            events.clear()

            # active -> orphaned (lazy lifecycle advance via get_owner)
            registry._sessions[(project_id, host_id)]["last_seen"] = 0.0
            orphaned = await get_owner(project_id, host_id=host_id)
            assert orphaned["status"] == "orphaned"

            orphan_events = [
                event for project, event in events
                if project == project_id and event.get("type") == "session_changed"
                and event.get("owner") and event["owner"].get("status") == "orphaned"
            ]
            assert len(orphan_events) >= 1
            owner_payload = orphan_events[-1]["owner"]
            for field in ("host_mode", "session_id", "status", "source_node_id", "workflow_label"):
                assert field in owner_payload, f"missing field {field} in orphan event"
            assert owner_payload["host_mode"] == "tab"
            assert owner_payload["session_id"] == "tab-events"
            assert owner_payload["status"] == "orphaned"
            assert owner_payload["source_node_id"] == "node-a"

            # orphaned -> active (heartbeat with matching session_id)
            events.clear()
            heartbeat = await heartbeat_session(
                project_id,
                "tab-events",
                host_id=host_id,
                source_node_id="node-a",
            )
            assert heartbeat["ok"] is True
            assert heartbeat["owner"]["status"] == "active"

            active_events = [
                event for project, event in events
                if project == project_id and event.get("type") == "session_changed"
                and event.get("owner") and event["owner"].get("status") == "active"
            ]
            assert len(active_events) >= 1
            active_payload = active_events[-1]["owner"]
            for field in ("host_mode", "session_id", "status", "source_node_id", "workflow_label"):
                assert field in active_payload
            assert active_payload["status"] == "active"
            assert active_payload["session_id"] == "tab-events"

            # orphaned -> released (advance past SESSION_TTL to orphan, then
            # past ORPHAN_TTL to release, trigger via get_owner).
            events.clear()
            registry._sessions[(project_id, host_id)]["last_seen"] = 0.0
            await get_owner(project_id, host_id=host_id)  # active->orphaned
            registry._sessions[(project_id, host_id)]["orphan_expires_at"] = registry._now() - 1
            final = await get_owner(project_id, host_id=host_id)
            assert final is None

            released_events = [
                event for project, event in events
                if project == project_id and event.get("type") == "session_changed"
                and event.get("owner") is None
            ]
            assert len(released_events) >= 1
            # Released event payload identifies the host whose owner was
            # cleared; owner field is None per _session_changed_event(.., None).
            released_payload = released_events[-1]
            assert released_payload["project_id"] == project_id
            assert released_payload["host_id"] == host_id
            assert released_payload["owner"] is None
        finally:
            registry.schedule_project_event = original_schedule
            await release_session(project_id, "cleanup", force=True, host_id=host_id)

    asyncio.run(run())


def test_get_owner_advances_lifecycle_lazily_and_sweeper_does_too():
    """Both lifecycle-advancement paths must transition a stale tab owner to
    orphaned: (1) the lazy advance inside get_owner and (2) the explicit
    sweep_stale_sessions_once. Existing sweeper test at line 382 covers
    fullscreen-release-via-sweeper; this test pins the tab-orphan path on
    both code paths.
    """
    async def run():
        import server.session_registry as registry

        # Path 1: lazy advancement via get_owner (no sweeper call).
        project_id_a = "phase-8-lazy-get-owner"
        host_id_a = "host-lazy"
        await release_session(project_id_a, "cleanup", force=True, host_id=host_id_a)
        await _claim_tab_owner(project_id_a, host_id_a, "node-a", "tab-lazy")
        registry._sessions[(project_id_a, host_id_a)]["last_seen"] = 0.0

        # Do NOT call the sweeper here. get_owner must drive the transition.
        owner_a = await get_owner(project_id_a, host_id=host_id_a)
        assert owner_a is not None, "owner should be orphaned (preserved), not None"
        assert owner_a["status"] == "orphaned"
        assert owner_a["orphan_expires_at"] > 0

        await release_session(project_id_a, "tab-lazy", force=True, host_id=host_id_a)

        # Path 2: explicit sweeper without a prior get_owner call.
        project_id_b = "phase-8-sweeper-tab-orphan"
        host_id_b = "host-sweeper"
        await release_session(project_id_b, "cleanup", force=True, host_id=host_id_b)
        await _claim_tab_owner(project_id_b, host_id_b, "node-b", "tab-sweeper")
        registry._sessions[(project_id_b, host_id_b)]["last_seen"] = 0.0

        # Do NOT call get_owner first. The sweeper must drive the transition.
        evicted = await sweep_stale_sessions_once()
        assert evicted >= 1

        owner_b = await get_owner(project_id_b, host_id=host_id_b)
        assert owner_b is not None, "tab owner should be orphaned by sweeper, not released"
        assert owner_b["status"] == "orphaned"
        assert owner_b["orphan_expires_at"] > 0

        await release_session(project_id_b, "tab-sweeper", force=True, host_id=host_id_b)

    asyncio.run(run())
