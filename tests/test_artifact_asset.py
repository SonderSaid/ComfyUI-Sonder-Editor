"""Tests for artifact asset serialization and media-sync classification."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.project_manager import create_project
from server.routes import _build_dormant_summary, _replace_project_asset, _sync_media_folder
from server.timeline_state import Asset, TimelineProject


def test_asset_artifact_kind_serializes():
    asset = Asset(
        asset_id="artifact-1",
        name="metadata.json",
        asset_type="artifact",
        artifact_kind="json",
        path="media/metadata.json",
    )

    restored = Asset.from_dict(asset.to_dict())

    assert restored.asset_type == "artifact"
    assert restored.artifact_kind == "json"


def test_asset_default_artifact_kind_is_empty():
    asset = Asset()

    assert asset.artifact_kind == ""
    assert Asset.from_dict(asset.to_dict()).artifact_kind == ""


def test_media_sync_classifies_artifact_extensions():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Artifact Sync", base_dir=base_dir)
        json_path = os.path.join(project.project_dir, "media", "metadata.json")
        other_path = os.path.join(project.project_dir, "media", "tensor.xyz")
        with open(json_path, "w", encoding="utf-8") as handle:
            handle.write('{"hello":"world"}')
        with open(other_path, "w", encoding="utf-8") as handle:
            handle.write("opaque")

        changed = _sync_media_folder(project)

        assert changed is True
        by_name = {asset.name: asset for asset in project.assets}
        assert by_name["metadata.json"].asset_type == "artifact"
        assert by_name["metadata.json"].artifact_kind == "json"
        assert by_name["tensor.xyz"].asset_type == "artifact"
        assert by_name["tensor.xyz"].artifact_kind == "other"


def test_summary_counts_include_artifacts():
    project = TimelineProject(
        project_dir="/tmp/test-project",
        assets=[
            Asset(asset_id="vid-1", name="clip.mp4", asset_type="video", path="media/clip.mp4"),
            Asset(asset_id="art-1", name="weights.safetensors", asset_type="artifact", artifact_kind="model", path="media/weights.safetensors"),
        ],
    )

    summary = _build_dormant_summary(project)

    assert summary["asset_counts"] == {
        "video": 1,
        "image": 0,
        "audio": 0,
        "artifact": 1,
        "total": 2,
    }


def test_replace_artifact_updates_artifact_kind():
    with tempfile.TemporaryDirectory() as base_dir:
        project = create_project("Artifact Replace", base_dir=base_dir)
        original_path = os.path.join(project.project_dir, "media", "metadata.json")
        replacement_path = os.path.join(base_dir, "notes.txt")
        with open(original_path, "w", encoding="utf-8") as handle:
            handle.write('{"hello":"world"}')
        with open(replacement_path, "w", encoding="utf-8") as handle:
            handle.write("hello")

        asset = Asset(
            asset_id="artifact-1",
            name="metadata.json",
            asset_type="artifact",
            artifact_kind="json",
            path="media/metadata.json",
        )
        project.assets.append(asset)

        _replace_project_asset(project, asset, replacement_path)

        assert asset.asset_type == "artifact"
        assert asset.artifact_kind == "text"
