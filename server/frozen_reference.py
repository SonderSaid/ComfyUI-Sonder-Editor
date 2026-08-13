"""Pure validation/decoding for queued Reference catalog snapshots."""

from __future__ import annotations

from .timeline_state import Asset, ReferenceEntity


class FrozenReferenceSnapshotError(ValueError):
    code = "invalid_frozen_reference_snapshot"


def _claimed_member_ids(job) -> set[str]:
    claimed = {
        str(member.get("member_id") or "")
        for item in (getattr(job, "reference_item_snapshots", []) or [])
        if isinstance(item, dict)
        for member in (item.get("members") or [])
        if isinstance(member, dict)
    }
    setup = getattr(job, "minimax_h3_setup_snapshot", {}) or {}
    for population in ("pictures", "videos", "standalone_audios"):
        claimed.update(
            str(row.get("member_id") or "")
            for row in (setup.get(population) or [])
            if isinstance(row, dict)
        )
    claimed.discard("")
    return claimed


def decode_frozen_reference_catalog(job) -> tuple[list[ReferenceEntity], list[Asset]]:
    """Decode a job-owned catalog or fail if claimed members are unavailable.

    An envelope with no claimed members is a legitimate empty Reference state;
    it returns empty lists and never inherits the live Library.
    """
    entries = [value for value in (getattr(job, "reference_input_snapshots", []) or [])
               if isinstance(value, dict)]
    references = [
        ReferenceEntity.from_dict(value.get("value") or {})
        for value in entries if value.get("kind") == "reference"
    ]
    assets = [
        Asset.from_dict(value.get("value") or {})
        for value in entries if value.get("kind") == "asset"
    ]
    claimed = _claimed_member_ids(job)
    if not claimed:
        return references, assets

    members = {
        str(member.member_id): member
        for reference in references
        for member in reference.members
    }
    missing_members = sorted(claimed.difference(members))
    asset_ids = {str(asset.asset_id) for asset in assets}
    missing_assets = sorted({
        str(members[member_id].asset_id)
        for member_id in claimed.intersection(members)
        if str(members[member_id].asset_id) not in asset_ids
    })
    if missing_members or missing_assets:
        details = []
        if missing_members:
            details.append(f"members={','.join(missing_members)}")
        if missing_assets:
            details.append(f"assets={','.join(missing_assets)}")
        raise FrozenReferenceSnapshotError(
            "Frozen Reference catalog is incomplete: " + "; ".join(details))
    return references, assets
