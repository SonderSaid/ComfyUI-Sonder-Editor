"""Empty and corrupt frozen Reference catalog behavior."""

import pytest

from server.frozen_reference import (
    FrozenReferenceSnapshotError,
    decode_frozen_reference_catalog,
)
from server.timeline_state import Asset, GenerationJob, ReferenceEntity, ReferenceMember


def test_legitimately_empty_frozen_reference_state_stays_empty():
    job = GenerationJob(
        reference_item_snapshots=[], reference_input_snapshots=[],
        minimax_h3_setup_snapshot={})
    references, assets = decode_frozen_reference_catalog(job)
    assert references == [] and assets == []


def test_claimed_member_without_catalog_is_controlled_corruption():
    job = GenerationJob(reference_item_snapshots=[{
        "reference_item_id": "item", "members": [{"member_id": "missing"}],
    }])
    with pytest.raises(FrozenReferenceSnapshotError) as exc:
        decode_frozen_reference_catalog(job)
    assert exc.value.code == "invalid_frozen_reference_snapshot"
    assert "members=missing" in str(exc.value)


def test_claimed_member_without_its_asset_is_controlled_corruption():
    reference = ReferenceEntity(
        reference_id="entity", members=[
            ReferenceMember(member_id="member", asset_id="asset")])
    job = GenerationJob(
        reference_item_snapshots=[{
            "reference_item_id": "item", "members": [{"member_id": "member"}],
        }],
        reference_input_snapshots=[{
            "kind": "reference", "value": reference.to_dict(),
        }])
    with pytest.raises(FrozenReferenceSnapshotError) as exc:
        decode_frozen_reference_catalog(job)
    assert "assets=asset" in str(exc.value)


def test_complete_claimed_catalog_decodes_typed_values():
    reference = ReferenceEntity(
        reference_id="entity", members=[
            ReferenceMember(member_id="member", asset_id="asset")])
    asset = Asset(asset_id="asset", asset_type="image", path="media/image.png")
    job = GenerationJob(
        minimax_h3_setup_snapshot={"pictures": [{"member_id": "member"}]},
        reference_input_snapshots=[
            {"kind": "reference", "value": reference.to_dict()},
            {"kind": "asset", "value": asset.to_dict()},
        ])
    references, assets = decode_frozen_reference_catalog(job)
    assert [value.reference_id for value in references] == ["entity"]
    assert [value.asset_id for value in assets] == ["asset"]
