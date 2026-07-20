"""Revision-safe, flat on-disk cache stores for composited timeline blocks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import torch

from . import external_links
from .atomic_io import atomic_replace
from .path_security import PathSecurityError, path_within, resolve_project_path, safe_route_token

logger = logging.getLogger("sonder_editor")

CACHE_FORMAT_VERSION = 3
CACHE_PIPELINE_VERSION = "cm2"
MAX_BLOCK_FRAMES = 32
MAX_BLOCK_BYTES = 256 * 1024 * 1024
STALE_TEMP_SECONDS = 24 * 60 * 60

_STORE_RE = re.compile(r"^rc3_[0-9a-f]{32}\.cache$")
_BLOCK_RE = re.compile(r"^block_(\d{8})\.pt$")
_TEMP_RE = re.compile(r"^\.block_(\d{8})_([0-9a-f]{32})\.tmp$")
_LEGACY_RE = re.compile(r"^[^/\\]+\.pt$")

_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ACTIVE_STORES: dict[str, dict[str, int]] = {}


class RenderCacheError(ValueError):
    """A render-cache entry is invalid or unsafe to operate on."""


class RenderCacheActiveError(RenderCacheError):
    """A render-cache store cannot be deleted while a render is using it."""


@dataclass(frozen=True)
class RenderCacheStore:
    root: str
    path: str
    token: str
    block_frames: int


@dataclass(frozen=True)
class StagedBlock:
    block_index: int
    temp_path: str
    final_path: str


def block_frame_count(width: int, height: int) -> int:
    frame_bytes = max(1, int(width or 1)) * max(1, int(height or 1)) * 3
    return min(MAX_BLOCK_FRAMES, max(1, MAX_BLOCK_BYTES // frame_bytes))


def _float_identity(value: float) -> str:
    return float(value).hex()


def _store_lock(path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STORE_LOCKS[key] = lock
        return lock


def _root_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _root_lock(path: str) -> threading.RLock:
    key = _root_key(path)
    with _LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


def active_store_tokens(root: str) -> set[str]:
    key = _root_key(root)
    with _LOCKS_GUARD:
        return {
            token for token, count in _ACTIVE_STORES.get(key, {}).items()
            if count > 0
        }


@contextmanager
def active_cache_store(store: RenderCacheStore):
    """Protect a store from retention and explicit deletion while it is in use."""
    _validate_store_path(store)
    key = _root_key(store.root)
    root_lock = _root_lock(store.root)
    with root_lock:
        with _LOCKS_GUARD:
            stores = _ACTIVE_STORES.setdefault(key, {})
            stores[store.token] = stores.get(store.token, 0) + 1
    try:
        yield store
    finally:
        with root_lock:
            with _LOCKS_GUARD:
                stores = _ACTIVE_STORES.get(key)
                if stores is not None:
                    remaining = stores.get(store.token, 0) - 1
                    if remaining > 0:
                        stores[store.token] = remaining
                    else:
                        stores.pop(store.token, None)
                    if not stores:
                        _ACTIVE_STORES.pop(key, None)


def _is_reparse(path: str) -> bool:
    parent, name = os.path.dirname(path), os.path.basename(path)
    try:
        return os.path.islink(path) or external_links.is_reparse_child(parent, name)
    except OSError:
        return os.path.islink(path)


def _validate_cache_root_path(root: str) -> None:
    cache_dir = os.path.dirname(root)
    project_dir = os.path.dirname(cache_dir)
    if os.path.basename(cache_dir) != "cache" or os.path.basename(root) != "renders":
        raise RenderCacheError("Invalid render cache root")
    if (
        (os.path.lexists(cache_dir) and external_links.is_reparse_child(project_dir, "cache"))
        or (os.path.lexists(root) and external_links.is_reparse_child(cache_dir, "renders"))
    ):
        raise RenderCacheError("Render cache root contains a reparse-point ancestor")


def render_cache_root(project, *, create: bool = False) -> str:
    root = resolve_project_path(project, os.path.join("cache", "renders"), purpose="render cache root")
    if not root:
        return root
    _validate_cache_root_path(root)
    if create:
        os.makedirs(root, exist_ok=True)
        _validate_cache_root_path(root)
    return root


def cache_store(project, scene_id: str, width: int, height: int, fps: float) -> RenderCacheStore | None:
    root = render_cache_root(project)
    if not root:
        return None
    frames = block_frame_count(width, height)
    identity = {
        "format": CACHE_FORMAT_VERSION,
        "pipeline": CACHE_PIPELINE_VERSION,
        "scene_id": str(scene_id or ""),
        "width": max(1, int(width or 1)),
        "height": max(1, int(height or 1)),
        "fps": _float_identity(fps),
        "block_frames": frames,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
    token = f"rc3_{digest}.cache"
    path = resolve_project_path(project, os.path.join("cache", "renders", token), purpose="render cache store")
    if not path:
        return None
    return RenderCacheStore(root=root, path=path, token=token, block_frames=frames)


def _block_name(block_index: int) -> str:
    return f"block_{max(0, int(block_index)):08d}.pt"


def _validate_store_path(store: RenderCacheStore) -> None:
    _validate_cache_root_path(store.root)
    if not _STORE_RE.fullmatch(store.token):
        raise RenderCacheError("Invalid render cache store")
    if not path_within(store.root, store.path):
        raise RenderCacheError("Render cache store escapes its root")
    if os.path.lexists(store.path) and _is_reparse(store.path):
        raise RenderCacheError("Render cache store is a reparse point")


def _scan_store(store: RenderCacheStore) -> list[tuple[str, os.stat_result, str, int]]:
    """Return (path, stat, kind, block_index) for a valid flat store."""
    _validate_store_path(store)
    if not os.path.isdir(store.path):
        return []
    children = []
    with os.scandir(store.path) as scan:
        for entry in scan:
            path = entry.path
            if _is_reparse(path) or entry.is_dir(follow_symlinks=False) or not entry.is_file(follow_symlinks=False):
                raise RenderCacheError("Render cache store contains an unsafe entry")
            block_match = _BLOCK_RE.fullmatch(entry.name)
            temp_match = _TEMP_RE.fullmatch(entry.name)
            if not block_match and not temp_match:
                raise RenderCacheError("Render cache store contains an unknown entry")
            stat = entry.stat(follow_symlinks=False)
            if block_match:
                children.append((path, stat, "block", int(block_match.group(1))))
            else:
                children.append((path, stat, "temp", int(temp_match.group(1))))
    return children


def prepare_store(store: RenderCacheStore, scene_duration: int) -> None:
    """Prune unreachable tail blocks and abandoned request temps from a valid store."""
    with _store_lock(store.path):
        if not os.path.isdir(store.path):
            return
        max_blocks = (max(0, int(scene_duration)) + store.block_frames - 1) // store.block_frames
        cutoff = time.time() - STALE_TEMP_SECONDS
        for path, stat, kind, index in _scan_store(store):
            if (kind == "block" and index >= max_blocks) or (kind == "temp" and stat.st_mtime < cutoff):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


def _expected_storage_bytes(frames: torch.Tensor) -> int:
    return int(frames.numel()) * int(frames.element_size())


def validate_block_payload(payload, *, block_index: int, start: int, end: int,
                           width: int, height: int, fingerprint: str) -> torch.Tensor | None:
    if not isinstance(payload, dict):
        return None
    expected = {
        "format_version": CACHE_FORMAT_VERSION,
        "pipeline_version": CACHE_PIPELINE_VERSION,
        "fingerprint": str(fingerprint),
        "block_index": int(block_index),
        "start": int(start),
        "end": int(end),
        "width": int(width),
        "height": int(height),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return None
    frames = payload.get("frames")
    expected_shape = (max(0, end - start), height, width, 3)
    if (
        not torch.is_tensor(frames)
        or frames.dtype != torch.uint8
        or frames.device.type != "cpu"
        or frames.layout != torch.strided
        or tuple(frames.shape) != expected_shape
        or not frames.is_contiguous()
        or frames.storage_offset() != 0
    ):
        return None
    try:
        storage_bytes = int(frames.untyped_storage().nbytes())
    except Exception:
        return None
    if storage_bytes != _expected_storage_bytes(frames):
        return None
    return frames


def load_block(store: RenderCacheStore, *, block_index: int, start: int, end: int,
               width: int, height: int, fingerprint: str) -> torch.Tensor | None:
    path = os.path.join(store.path, _block_name(block_index))
    with _store_lock(store.path):
        try:
            _validate_store_path(store)
            if not os.path.isfile(path) or _is_reparse(path):
                return None
            payload = torch.load(path, weights_only=True, map_location="cpu")
        except Exception as exc:
            logger.warning("Failed to load render cache block %s: %s", path, exc)
            return None
    return validate_block_payload(
        payload,
        block_index=block_index,
        start=start,
        end=end,
        width=width,
        height=height,
        fingerprint=fingerprint,
    )


def stage_block(store: RenderCacheStore, *, request_id: str, block_index: int,
                start: int, end: int, width: int, height: int,
                fingerprint: str, frames: torch.Tensor) -> StagedBlock:
    expected_shape = (max(0, int(end) - int(start)), int(height), int(width), 3)
    if not torch.is_tensor(frames) or tuple(frames.shape) != expected_shape:
        raise RenderCacheError("Render cache block has an invalid shape")
    owned = frames.detach()
    needs_copy = (
        owned.device.type != "cpu"
        or owned.dtype != torch.uint8
        or owned.layout != torch.strided
        or not owned.is_contiguous()
        or owned.storage_offset() != 0
    )
    if not needs_copy:
        try:
            needs_copy = int(owned.untyped_storage().nbytes()) != _expected_storage_bytes(owned)
        except Exception:
            needs_copy = True
    if needs_copy:
        owned = owned.to(device="cpu", dtype=torch.uint8).contiguous().clone()
    if owned.storage_offset() != 0 or int(owned.untyped_storage().nbytes()) != _expected_storage_bytes(owned):
        raise RenderCacheError("Render cache block does not own its storage")
    payload = {
        "format_version": CACHE_FORMAT_VERSION,
        "pipeline_version": CACHE_PIPELINE_VERSION,
        "fingerprint": str(fingerprint),
        "block_index": int(block_index),
        "start": int(start),
        "end": int(end),
        "width": int(width),
        "height": int(height),
        "frames": owned,
    }
    request_token = re.sub(r"[^0-9a-f]", "", str(request_id).lower())[:32] or uuid.uuid4().hex
    temp_name = f".block_{int(block_index):08d}_{request_token:0<32}.tmp"
    final_path = os.path.join(store.path, _block_name(block_index))
    temp_path = os.path.join(store.path, temp_name)
    with _store_lock(store.path):
        _validate_store_path(store)
        os.makedirs(store.path, exist_ok=True)
        _validate_store_path(store)
        if os.path.lexists(temp_path):
            raise RenderCacheError("Render cache temp path already exists")
        try:
            torch.save(payload, temp_path)
        except Exception:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            raise
    return StagedBlock(block_index=int(block_index), temp_path=temp_path, final_path=final_path)


def discard_staged(staged: list[StagedBlock]) -> None:
    parents = set()
    for item in staged:
        parents.add(os.path.dirname(item.temp_path))
        try:
            if os.path.isfile(item.temp_path):
                os.remove(item.temp_path)
        except OSError:
            pass
    for parent in parents:
        try:
            os.rmdir(parent)
        except OSError:
            pass


def publish_staged(store: RenderCacheStore, staged: list[StagedBlock], delete_indices: set[int]) -> None:
    with _store_lock(store.path):
        _validate_store_path(store)
        for item in staged:
            if not path_within(store.path, item.temp_path) or not path_within(store.path, item.final_path):
                raise RenderCacheError("Render cache block escapes its store")
            if not os.path.isfile(item.temp_path) or _is_reparse(item.temp_path):
                raise RenderCacheError("Render cache staged block is missing or unsafe")
        for item in staged:
            atomic_replace(item.temp_path, item.final_path)
        for block_index in sorted(delete_indices):
            path = os.path.join(store.path, _block_name(block_index))
            if os.path.isfile(path) and not _is_reparse(path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


def _legacy_path(root: str, token: str) -> str:
    try:
        safe_route_token(token, "render cache entry")
    except PathSecurityError as exc:
        raise RenderCacheError("Invalid render cache entry") from exc
    if not _LEGACY_RE.fullmatch(token):
        raise RenderCacheError("Invalid render cache entry")
    path = os.path.abspath(os.path.join(root, token))
    if not path_within(root, path):
        raise RenderCacheError("Invalid render cache entry")
    return path


def _store_from_token(project, token: str) -> RenderCacheStore:
    root = render_cache_root(project)
    if not root or not _STORE_RE.fullmatch(str(token or "")):
        raise RenderCacheError("Invalid render cache entry")
    path = os.path.abspath(os.path.join(root, token))
    if not path_within(root, path):
        raise RenderCacheError("Invalid render cache entry")
    return RenderCacheStore(root=root, path=path, token=token, block_frames=1)


def _list_render_cache_entries_unlocked(project) -> list[dict]:
    root = render_cache_root(project)
    if not root or not os.path.isdir(root):
        return []
    entries = []
    with os.scandir(root) as scan:
        root_entries = list(scan)
    for entry in root_entries:
        try:
            if _is_reparse(entry.path):
                continue
            if _LEGACY_RE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                entries.append({"filename": entry.name, "mtime": stat.st_mtime, "size_bytes": stat.st_size})
                continue
            if not _STORE_RE.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            store = RenderCacheStore(root=root, path=entry.path, token=entry.name, block_frames=1)
            with _store_lock(store.path):
                children = _scan_store(store)
                store_stat = entry.stat(follow_symlinks=False)
            size = sum(stat.st_size for _path, stat, _kind, _index in children)
            mtime = max([store_stat.st_mtime] + [stat.st_mtime for _path, stat, _kind, _index in children])
            entries.append({"filename": entry.name, "mtime": mtime, "size_bytes": size})
        except (OSError, RenderCacheError):
            logger.warning("Skipping unsafe render cache entry: %s", entry.path)
    entries.sort(key=lambda item: (item["mtime"], item["filename"]))
    return entries


def list_render_cache_entries(project) -> list[dict]:
    root = render_cache_root(project)
    if not root or not os.path.isdir(root):
        return []
    with _root_lock(root):
        return _list_render_cache_entries_unlocked(project)


def _delete_render_cache_entry_unlocked(project, root: str, token: str) -> dict:
    token = str(token or "").strip()
    if token in active_store_tokens(root):
        raise RenderCacheActiveError("Render cache entry is active")
    if _STORE_RE.fullmatch(token):
        store = _store_from_token(project, token)
        with _store_lock(store.path):
            if not os.path.isdir(store.path):
                raise FileNotFoundError("Render cache entry not found")
            children = _scan_store(store)
            for path, _stat, _kind, _index in children:
                os.remove(path)
            os.rmdir(store.path)
        return {"deleted": True, "filename": token}
    path = _legacy_path(root, token)
    with _store_lock(path):
        if not os.path.isfile(path) or _is_reparse(path):
            raise FileNotFoundError("Render cache entry not found")
        os.remove(path)
    return {"deleted": True, "filename": token}


def delete_render_cache_entry(project, token: str) -> dict:
    root = render_cache_root(project)
    if not root:
        raise FileNotFoundError("Render cache entry not found")
    with _root_lock(root):
        return _delete_render_cache_entry_unlocked(project, root, token)


def enforce_render_cache_budget(project, max_size_bytes: int | None, *,
                                protected_tokens=()) -> dict:
    """Evict oldest whole entries until the project cache fits its soft budget."""
    if max_size_bytes is not None:
        if isinstance(max_size_bytes, bool) or not isinstance(max_size_bytes, int):
            raise RenderCacheError("Render cache budget must be a non-negative integer or null")
        if max_size_bytes < 0:
            raise RenderCacheError("Render cache budget must be a non-negative integer or null")

    root = render_cache_root(project)
    if not root or not os.path.isdir(root):
        return {
            "entry_count": 0,
            "size_bytes": 0,
            "deleted": [],
            "deleted_bytes": 0,
            "protected": [],
            "over_budget_bytes": 0,
            "pending": False,
            "failures": [],
        }

    with _root_lock(root):
        entries = _list_render_cache_entries_unlocked(project)
        protected = {
            str(token) for token in protected_tokens or ()
            if isinstance(token, str) and token
        }
        protected.update(active_store_tokens(root))
        total_size = sum(max(0, int(entry.get("size_bytes", 0) or 0)) for entry in entries)
        deleted = []
        deleted_bytes = 0
        failures = []

        if max_size_bytes is not None and total_size > max_size_bytes:
            for entry in entries:
                if total_size <= max_size_bytes:
                    break
                token = str(entry.get("filename") or "")
                if not token or token in protected:
                    continue
                entry_size = max(0, int(entry.get("size_bytes", 0) or 0))
                try:
                    _delete_render_cache_entry_unlocked(project, root, token)
                except FileNotFoundError:
                    total_size = max(0, total_size - entry_size)
                except (OSError, RenderCacheError) as exc:
                    failures.append(token)
                    logger.warning("Failed to evict render cache entry %s: %s", token, exc)
                else:
                    deleted.append(token)
                    deleted_bytes += entry_size
                    total_size = max(0, total_size - entry_size)

        remaining = _list_render_cache_entries_unlocked(project)
        total_size = sum(max(0, int(entry.get("size_bytes", 0) or 0)) for entry in remaining)
        remaining_tokens = {str(entry.get("filename") or "") for entry in remaining}
        protected_remaining = sorted(protected & remaining_tokens)
        over_budget = 0 if max_size_bytes is None else max(0, total_size - max_size_bytes)
        result = {
            "entry_count": len(remaining),
            "size_bytes": total_size,
            "deleted": deleted,
            "deleted_bytes": deleted_bytes,
            "protected": protected_remaining,
            "over_budget_bytes": over_budget,
            "pending": bool(over_budget and (protected_remaining or failures)),
            "failures": failures,
        }
        logger.info(
            "Render cache retention budget=%s entries=%d size_bytes=%d deleted=%s "
            "deleted_bytes=%d protected=%s over_budget_bytes=%d pending=%s failures=%s",
            "unlimited" if max_size_bytes is None else max_size_bytes,
            result["entry_count"],
            result["size_bytes"],
            result["deleted"],
            result["deleted_bytes"],
            result["protected"],
            result["over_budget_bytes"],
            result["pending"],
            result["failures"],
        )
        return result
