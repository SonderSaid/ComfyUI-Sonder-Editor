"""Emptying the trash must not destroy media before the document that drops it commits.

Two failure modes, in opposite directions, and the staging design exists to avoid both:

* delete-then-save (the old shape) loses a compare-and-swap *after* the bytes are
  gone, leaving project records pointing at deleted media;
* save-then-delete leaves a failed unlink under ``media/``, where ``_sync_media_folder``
  re-registers it as a brand-new untrashed asset on the next gallery Refresh.
"""
import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from test_project_mutation_pipeline_backend import (
    DummyRequest, _load_route_module, _route_handler)
from server import project_manager as pm
from server.timeline_state import Asset


def _png_bytes(colour=(10, 20, 30)):
    """A real decodable image.

    Unprobeable bytes would be discarded by `_sync_media_folder`'s MediaProbeError
    branch, so a scan test written with them passes whether or not the reserved
    directory is skipped — which is the one property the staging design rests on.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), colour).save(buffer, "PNG")
    return buffer.getvalue()


def _project_with_trashed_media(tmp_path, *, name="doomed.png"):
    project = pm.create_project("Trash", 24, 512, 512, "free", str(tmp_path))
    project_dir = Path(project.project_dir)
    media_file = project_dir / "media" / name
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(_png_bytes())

    loaded = pm.load_project(str(project_dir))
    loaded.assets.append(Asset(
        asset_id="asset-1", name=name, asset_type="image",
        path=f"media/{name}", trashed_at="2026-01-01T00:00:00"))
    pm.save_project(loaded, notify=False)
    return project_dir, media_file


def _empty_trash(routes, project_dir):
    handler = _route_handler(
        routes, "POST", "/sonder-editor/project/{project_id}/assets/empty-trash")
    return asyncio.run(handler(DummyRequest(
        match_info={"project_id": project_dir.name}, method="POST", body={})))


def test_a_refused_commit_leaves_the_media_exactly_where_it_was(tmp_path, monkeypatch):
    routes = _load_route_module(monkeypatch)
    project_dir, media_file = _project_with_trashed_media(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    def always_conflict(saved, **kwargs):
        raise routes.ProjectVersionConflict(
            project_dir=str(project_dir), expected_modified_at="expected",
            actual_modified_at="actual", current_data={})

    monkeypatch.setattr(routes, "save_project", always_conflict)

    with pytest.raises(routes.ProjectVersionConflict):
        _empty_trash(routes, project_dir)

    assert media_file.is_file(), "a refused empty-trash destroyed the media anyway"
    assert media_file.read_bytes() == _png_bytes()
    stored = json.loads((project_dir / "project.json").read_bytes())
    assert [asset["asset_id"] for asset in stored["assets"]] == ["asset-1"], (
        "the record survived but its bytes did not, or vice versa")
    assert not (project_dir / "media" / ".trash-staging").exists(), (
        "rollback left the staging directory behind")


def test_a_successful_empty_trash_removes_both_record_and_bytes(tmp_path, monkeypatch):
    routes = _load_route_module(monkeypatch)
    project_dir, media_file = _project_with_trashed_media(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    response = _empty_trash(routes, project_dir)

    assert response.status == 200
    assert json.loads(response.body)["deleted"] == ["asset-1"]
    assert not media_file.exists()
    assert json.loads((project_dir / "project.json").read_bytes())["assets"] == []
    assert not (project_dir / "media" / ".trash-staging").exists()


def test_the_staging_directory_is_invisible_to_the_media_snapshot(tmp_path, monkeypatch):
    """The property the whole design rests on: staged bytes are not project media.

    Asserted against the snapshot rather than the scan, because the scan now also
    sweeps staging — which would make an "it was not adopted" assertion pass for a
    reason unrelated to the skip.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, _media_file = _project_with_trashed_media(tmp_path)
    staging = project_dir / "media" / routes.TRASH_STAGING_DIRNAME
    staging.mkdir(parents=True)
    (staging / "asset-1").write_bytes(_png_bytes((200, 100, 50)))
    (project_dir / "media" / "loose.png").write_bytes(_png_bytes((200, 100, 50)))

    snapshot = routes._project_media_snapshot(pm.load_project(str(project_dir)))

    assert "media/loose.png" in snapshot, "control: ordinary media is snapshotted"
    assert not [key for key in snapshot if routes.TRASH_STAGING_DIRNAME in key], (
        "staged media reached a scan that treats what it finds as project media")


def test_a_staged_orphan_whose_record_is_gone_is_reclaimed(tmp_path, monkeypatch):
    """Its removal committed, so the bytes are unreferenced; the scan reclaims them."""
    routes = _load_route_module(monkeypatch)
    project_dir, media_file = _project_with_trashed_media(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    staging = project_dir / "media" / routes.TRASH_STAGING_DIRNAME
    staging.mkdir(parents=True)
    staged_file = staging / "asset-1"
    staged_file.write_bytes(_png_bytes())
    media_file.unlink()

    project = pm.load_project(str(project_dir))
    project.assets.clear()
    routes._sync_media_folder(project)

    assert not staged_file.exists(), "an unreferenced staged file was left forever"
    assert [asset.path for asset in project.assets] == [], (
        "the scan adopted a staged file as a new asset")


def test_a_staged_file_whose_record_survives_is_restored(tmp_path, monkeypatch):
    """A crash between the move and the commit must not strand user media.

    Without the sweep the bytes sit in a directory every scan deliberately ignores,
    the gallery shows the asset as missing, and nothing inside the product can get
    them back.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, media_file = _project_with_trashed_media(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    staging = project_dir / "media" / routes.TRASH_STAGING_DIRNAME
    staging.mkdir(parents=True)
    # Exactly the state a process death between `stage` and `discard` leaves behind:
    # bytes aside, record intact.
    (staging / "asset-1").write_bytes(_png_bytes())
    media_file.unlink()

    project = pm.load_project(str(project_dir))
    # `purge_trashed=False` isolates the sweep: the fixture's asset was trashed long
    # enough ago to be expired, so the retention purge would delete the very media
    # the sweep just restored and hide whether the restore happened at all.
    #
    # The return is False even though the sweep did its work, and that is the contract,
    # not an oversight: it reports whether the DOCUMENT needs saving, and moving bytes
    # back to a path already recorded changes nothing to save. The recovery is on disk.
    assert routes._sync_media_folder(project, purge_trashed=False) is False
    assert media_file.is_file(), "staged media was not recovered"
    assert media_file.read_bytes() == _png_bytes()
    assert not staging.exists()


def test_a_filesystem_only_recovery_is_not_reported_as_a_document_change(
        tmp_path, monkeypatch):
    """`_sync_media_folder`'s return means "save me", not "something happened".

    The recovery sweep moves bytes back to a path the document already records, and
    whether an asset's media exists is computed per request rather than stored — so a
    restore changes nothing that could be written. Its one consumer,
    `_save_versioned_sync_phase`, reads the return as "the in-memory document differs
    from disk" and saves on a truthy value, so reporting a restore there costs a full
    compare-and-swap write of an unchanged document on the gallery Refresh after every
    crash recovery. On a large project that is seconds of lock hold for nothing.

    The asset is an IMAGE deliberately. The video, colour-backfill and audio repair
    passes all gate on `asset_type`, so a video or audio record would dirty the document
    by itself and this test would pass without proving anything.
    """
    routes = _load_route_module(monkeypatch)
    project = pm.create_project("Sweep Survives", 24, 512, 512, "free", str(tmp_path))
    project_dir = Path(project.project_dir)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    restored = project_dir / "media" / "restored.png"
    restored.parent.mkdir(parents=True, exist_ok=True)

    loaded = pm.load_project(str(project_dir))
    # Nothing in the project carries `trashed_at`, so the retention purge has nothing to
    # delete and returns False — the value that used to overwrite the sweep's True.
    loaded.assets.append(Asset(
        asset_id="img-1", name="restored.png", asset_type="image",
        path="media/restored.png"))
    pm.save_project(loaded, notify=False)

    staging = project_dir / "media" / routes.TRASH_STAGING_DIRNAME
    staging.mkdir(parents=True)
    (staging / "img-1").write_bytes(_png_bytes())

    saves = []
    real_save = routes.save_project
    monkeypatch.setattr(routes, "save_project", lambda saved, **kwargs: (
        saves.append(kwargs), real_save(saved, **kwargs))[1])

    scanning = pm.load_project(str(project_dir))
    before = json.loads(json.dumps(scanning.to_dict()))
    # The real production entry, not the raw scan: it is the one that decides whether a
    # write happens, and its first phase passes `purge_trashed=False` — so the sweep's
    # result is the whole of `changed` there.
    routes._sync_media_folder_versioned(scanning)

    assert restored.is_file(), "staged media was not recovered"
    assert restored.read_bytes() == _png_bytes()
    assert json.loads(json.dumps(scanning.to_dict())) == before, (
        "the recovery sweep mutated the document")
    assert saves == [], (
        "a filesystem-only recovery cost a full compare-and-swap write of an unchanged "
        "document, on the gallery Refresh after every crash recovery")


def test_a_concurrent_writer_committing_the_same_removal_does_not_resurrect_media(
        tmp_path, monkeypatch):
    """Rollback must ask the document, not its own control flow.

    Two windows empty the same trash. We stage the bytes first; the other writer
    loads, finds the file already gone, stages nothing of its own, removes the
    records and wins the compare-and-swap. Restoring unconditionally at that point
    would put the bytes back underneath a record that no longer exists — and the
    next gallery Refresh would adopt them as a brand-new untrashed asset, which is
    exactly the resurrection staging exists to prevent.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, media_file = _project_with_trashed_media(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    real_save = routes.save_project
    saves = []

    def the_other_window_wins_first(saved, **kwargs):
        saves.append(saved)
        if len(saves) == 1:
            assert not media_file.exists(), "our bytes should be staged by now"
            other = pm.load_project(str(project_dir))
            other.assets.clear()
            pm.save_project(other, notify=False)
            raise routes.ProjectVersionConflict(
                project_dir=str(project_dir), expected_modified_at="ours",
                actual_modified_at="theirs", current_data={})
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", the_other_window_wins_first)

    response = _empty_trash(routes, project_dir)

    assert response.status == 200
    assert json.loads((project_dir / "project.json").read_bytes())["assets"] == []
    assert not media_file.exists(), (
        "rollback restored bytes whose record another writer had already removed")
    assert not (project_dir / "media" / routes.TRASH_STAGING_DIRNAME).exists()

    # The resurrection this guards against: a Refresh adopting the restored file.
    project = pm.load_project(str(project_dir))
    assert routes._sync_media_folder(project) is False
    assert [asset.path for asset in project.assets] == []


def test_an_asset_id_that_cannot_be_a_filename_refuses_instead_of_deleting(
        tmp_path, monkeypatch):
    """Ids are never path authority; a quarantined one must not fall through."""
    routes = _load_route_module(monkeypatch)
    project = pm.create_project("Hostile", 24, 512, 512, "free", str(tmp_path))
    project_dir = Path(project.project_dir)
    media_file = project_dir / "media" / "victim.png"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(_png_bytes())
    loaded = pm.load_project(str(project_dir))
    loaded.assets.append(Asset(
        asset_id="../../escaped", name="victim.png", asset_type="image",
        path="media/victim.png", trashed_at="2026-01-01T00:00:00"))
    pm.save_project(loaded, notify=False)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    response = _empty_trash(routes, project_dir)

    assert response.status == 400
    assert json.loads(response.body)["code"] == "unsafe_asset_id"
    assert media_file.is_file(), "a hostile id must not cause media loss"
    assert not (project_dir.parent / "escaped").exists()
    assert not (project_dir / "media" / "escaped").exists()
    stored = json.loads((project_dir / "project.json").read_bytes())
    assert [asset["asset_id"] for asset in stored["assets"]] == ["../../escaped"]


def test_a_refresh_during_the_staging_window_does_not_resurrect_the_media(
        tmp_path, monkeypatch):
    """The sweep must not 'recover' bytes a delete is still in the middle of removing.

    `_apply_project_versioned_sync` holds no lock between its load and its save, so a
    gallery Refresh lands inside the staging window by construction — and the on-disk
    state there (record present, original path empty) is exactly what a crash leaves.
    Restoring at that moment means the commit then drops the record and the next scan
    adopts the restored file as a brand-new untrashed asset: the resurrection staging
    exists to prevent, arriving through the recovery sweep.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, media_file = _project_with_trashed_media(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    real_save = routes.save_project
    observed = []

    def refresh_midway(saved, **kwargs):
        # Precondition: we are inside the window — bytes staged, record still there.
        observed.append(("staged", not media_file.exists()))
        scanning = pm.load_project(str(project_dir))
        routes._sync_media_folder(scanning, purge_trashed=False)
        observed.append(("restored_by_refresh", media_file.exists()))
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", refresh_midway)

    response = _empty_trash(routes, project_dir)

    assert response.status == 200
    assert observed[0] == ("staged", True), "the gesture had not staged yet"
    assert observed[1] == ("restored_by_refresh", False), (
        "a concurrent refresh restored media belonging to an in-flight delete")
    assert json.loads((project_dir / "project.json").read_bytes())["assets"] == []
    assert not media_file.exists(), "the deleted media came back"

    # And the resurrection itself: a later scan must not adopt anything.
    after = pm.load_project(str(project_dir))
    assert routes._sync_media_folder(after) is False
    assert [asset.path for asset in after.assets] == []


def test_rollback_does_not_restore_media_the_document_no_longer_records(
        tmp_path, monkeypatch):
    """A concurrent replacement moves the asset's path while our bytes are staged.

    Rollback asked whether the *asset id* was still recorded, not whether the path we
    took the bytes from still was. A replacement keeps the id and changes the path
    (`_replace_project_asset` mints a new name whenever the extension differs), so the
    id test passes and the bytes go back to a location nothing references. The next
    media scan adopts that unreferenced file under `media/` as a brand-new untrashed
    asset — the resurrection this class exists to prevent, arriving from the side the
    `os.path.exists(original_path)` guard does not cover, because a path-changing
    replacement leaves the old path empty.

    The winning writer *intended* to delete those bytes: both replace paths
    (`routes.py:9221` and the streamed commit at `:8964`) remove the old file when the
    path changes and no other asset shares it. It could not, because we had already
    moved the file aside. Dropping them here completes that removal rather than
    guessing.
    """
    routes = _load_route_module(monkeypatch)
    project_dir, media_file = _project_with_trashed_media(tmp_path)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))
    replacement = project_dir / "media" / "replacement.png"

    real_save = routes.save_project
    saves = []

    def the_replacement_wins_first(saved, **kwargs):
        saves.append(saved)
        if len(saves) == 1:
            assert not media_file.exists(), "our bytes should be staged by now"
            replacement.write_bytes(_png_bytes((200, 50, 50)))
            other = pm.load_project(str(project_dir))
            other.get_asset("asset-1").path = "media/replacement.png"
            pm.save_project(other, notify=False)
            raise routes.ProjectVersionConflict(
                project_dir=str(project_dir), expected_modified_at="ours",
                actual_modified_at=other.modified_at, current_data={})
        return real_save(saved, **kwargs)

    monkeypatch.setattr(routes, "save_project", the_replacement_wins_first)

    response = _empty_trash(routes, project_dir)

    assert response.status == 200
    assert json.loads((project_dir / "project.json").read_bytes())["assets"] == []
    assert not replacement.exists(), "the retry did not remove the media it did name"
    assert not media_file.exists(), (
        "rollback restored bytes to a path the committed document no longer records")
    assert not (project_dir / "media" / routes.TRASH_STAGING_DIRNAME).exists()

    # The consequence, stated separately: a Refresh must find nothing to adopt.
    project = pm.load_project(str(project_dir))
    assert routes._sync_media_folder(project) is False
    assert [asset.path for asset in project.assets] == [], (
        "the scan adopted the restored orphan as a brand-new untrashed asset")


def test_one_unresolvable_record_does_not_abort_the_whole_media_scan(tmp_path, monkeypatch):
    """The sweep is a janitor, not a gate, and it now runs first in every scan.

    `_require_asset_media_source` raises — correct where it gates a destructive gesture
    (`empty_trash` refuses outright rather than deleting a record it cannot resolve),
    wrong here. One quarantined or unresolvable path would take the gallery refresh
    down for the entire project, and strand its own staged media in the process, since
    this sweep is the only route those bytes have back.
    """
    routes = _load_route_module(monkeypatch)
    project = pm.create_project("Sweep", 24, 512, 512, "free", str(tmp_path))
    project_dir = Path(project.project_dir)
    monkeypatch.setattr(routes, "_get_base_dir", lambda: str(tmp_path))

    recoverable = project_dir / "media" / "recoverable.png"
    recoverable.parent.mkdir(parents=True, exist_ok=True)

    loaded = pm.load_project(str(project_dir))
    # Sorts before the good one, so the scan meets it first however `os.listdir`
    # orders the staging directory.
    loaded.assets.append(Asset(
        asset_id="aaa-quarantined", name="escaped.png", asset_type="image",
        path="elsewhere/escaped.png"))
    loaded.assets.append(Asset(
        asset_id="zzz-recoverable", name="recoverable.png", asset_type="image",
        path="media/recoverable.png"))
    pm.save_project(loaded, notify=False)

    staging = project_dir / "media" / routes.TRASH_STAGING_DIRNAME
    staging.mkdir(parents=True)
    (staging / "aaa-quarantined").write_bytes(_png_bytes((1, 2, 3)))
    (staging / "zzz-recoverable").write_bytes(_png_bytes())

    scanning = pm.load_project(str(project_dir))
    # The claim is that this RETURNS rather than raising: one unresolvable record must
    # not take the scan down with it. False is the correct value — the sweep restores
    # bytes to an already-recorded path, which leaves nothing to save — so the recovery
    # itself is asserted on disk below, not through this return.
    # `purge_trashed=False` keeps the pass to the one phase under test; neither asset
    # here carries `trashed_at`, so the retention purge has nothing to do either way.
    assert routes._sync_media_folder(scanning, purge_trashed=False) is False

    assert recoverable.is_file(), "the resolvable staged file was not recovered"
    assert recoverable.read_bytes() == _png_bytes()
    # The unresolvable one is left alone rather than guessed at, and it stays inside a
    # directory every scan skips, so it is not adopted as a new asset either.
    assert (staging / "aaa-quarantined").is_file()
    assert sorted(asset.asset_id for asset in scanning.assets) == [
        "aaa-quarantined", "zzz-recoverable"], "the scan invented or dropped a record"
