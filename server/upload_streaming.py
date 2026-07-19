"""Bounded-memory multipart upload staging for Sonder project media."""

from __future__ import annotations

import asyncio
import errno
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiohttp.multipart import BodyPartReader

from .path_security import sanitize_filename_component


UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_TEXT_BYTES = 64 * 1024
UPLOAD_DISK_RESERVE_BYTES = 1024 * 1024 * 1024
UPLOAD_IDLE_TIMEOUT_SEC = 60.0
UPLOAD_STALE_AGE_SEC = 24 * 60 * 60
UPLOAD_STAGING_DIRNAME = ".sonder_upload_staging"
UPLOAD_SERVER_CONCURRENCY = 2

_STAGING_NAME_RE = re.compile(r"^[0-9a-f]{32}\.part(?:\.[A-Za-z0-9_-]+)?$")
_ACTIVE_PATHS: set[str] = set()
_ACTIVE_PATHS_LOCK = threading.Lock()
_COORDINATOR_LOOP = None
_SERVER_SEMAPHORE = None
_VOLUME_LOCKS: dict[int, asyncio.Lock] = {}


class UploadRequestError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


@dataclass
class StagedUpload:
    path: str
    filename: str
    fields: dict[str, str]
    size: int
    started_at: float


def _coordinator_for_current_loop() -> tuple[asyncio.Semaphore, dict[int, asyncio.Lock]]:
    global _COORDINATOR_LOOP, _SERVER_SEMAPHORE, _VOLUME_LOCKS
    loop = asyncio.get_running_loop()
    if _COORDINATOR_LOOP is not loop:
        _COORDINATOR_LOOP = loop
        _SERVER_SEMAPHORE = asyncio.Semaphore(UPLOAD_SERVER_CONCURRENCY)
        _VOLUME_LOCKS = {}
    return _SERVER_SEMAPHORE, _VOLUME_LOCKS


def _is_reparse_stat(st) -> bool:
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return bool(marker and attrs & marker)


def _prepare_staging_dir(staging_dir: str) -> None:
    os.makedirs(staging_dir, mode=0o700, exist_ok=True)
    st = os.lstat(staging_dir)
    if not stat.S_ISDIR(st.st_mode) or _is_reparse_stat(st) or os.path.islink(staging_dir):
        raise UploadRequestError(400, "Upload staging must be a real contained directory")


def _safe_remove_staging_file(path: str, staging_dir: str) -> None:
    try:
        if os.path.commonpath((os.path.abspath(staging_dir), os.path.abspath(path))) != os.path.abspath(staging_dir):
            return
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or _is_reparse_stat(st) or os.path.islink(path):
            return
        os.remove(path)
    except FileNotFoundError:
        pass


def _sweep_stale_parts(staging_dir: str) -> None:
    cutoff = time.time() - UPLOAD_STALE_AGE_SEC
    try:
        entries = list(os.scandir(staging_dir))
    except FileNotFoundError:
        return
    for entry in entries:
        if not _STAGING_NAME_RE.fullmatch(entry.name):
            continue
        path = os.path.abspath(entry.path)
        with _ACTIVE_PATHS_LOCK:
            if path in _ACTIVE_PATHS:
                continue
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(st.st_mode)
                or _is_reparse_stat(st)
                or os.path.islink(path)
                or st.st_mtime >= cutoff
            ):
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


async def _await_worker(func, *args):
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def _next_part(reader):
    try:
        return await asyncio.wait_for(reader.next(), timeout=UPLOAD_IDLE_TIMEOUT_SEC)
    except asyncio.TimeoutError as exc:
        raise UploadRequestError(408, "Upload timed out while waiting for request data") from exc
    except UploadRequestError:
        raise
    except Exception as exc:
        raise UploadRequestError(400, "Invalid multipart request") from exc


async def _read_part_chunk(field: BodyPartReader, size: int = UPLOAD_CHUNK_BYTES) -> bytes:
    try:
        return await asyncio.wait_for(field.read_chunk(size=size), timeout=UPLOAD_IDLE_TIMEOUT_SEC)
    except asyncio.TimeoutError as exc:
        raise UploadRequestError(408, "Upload timed out while waiting for request data") from exc


def _check_disk_space(staging_dir: str, required_bytes: int = 0) -> None:
    free = shutil.disk_usage(staging_dir).free
    if free - max(0, int(required_bytes)) < UPLOAD_DISK_RESERVE_BYTES:
        raise UploadRequestError(507, "Not enough free storage to import this file while preserving the 1 GiB reserve")


def ensure_upload_disk_space(staging_dir: str, required_bytes: int = 0) -> None:
    """Public synchronous reserve check for replacement rollback allocation."""
    _check_disk_space(staging_dir, required_bytes)


def active_upload_count() -> int:
    with _ACTIVE_PATHS_LOCK:
        return len(_ACTIVE_PATHS)


def _staging_filename(filename: str) -> str:
    extension = os.path.splitext(filename)[1].lower()
    if not re.fullmatch(r"\.[A-Za-z0-9_-]{1,24}", extension or ""):
        extension = ""
    return f"{uuid.uuid4().hex}.part{extension}"


def _sanitize_upload_filename(value: str) -> str:
    raw_basename = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    raw_stem, raw_extension = os.path.splitext(raw_basename)
    extension = raw_extension.lower()
    if not re.fullmatch(r"\.[A-Za-z0-9_-]{1,24}", extension or ""):
        extension = ""
    stem = sanitize_filename_component(raw_stem, fallback="imported_media")
    max_stem = max(1, 120 - len(extension))
    return f"{stem[:max_stem]}{extension}"


async def _read_text_field(field: BodyPartReader, consumed: int) -> tuple[str, int]:
    raw = bytearray()
    while True:
        chunk = await _read_part_chunk(field, min(UPLOAD_CHUNK_BYTES, UPLOAD_TEXT_BYTES + 1))
        if not chunk:
            break
        raw.extend(chunk)
        consumed += len(chunk)
        if consumed > UPLOAD_TEXT_BYTES:
            raise UploadRequestError(413, "Multipart text fields exceed the 64 KiB limit")
    try:
        return bytes(raw).decode("utf-8", errors="strict"), consumed
    except UnicodeDecodeError as exc:
        raise UploadRequestError(400, "Multipart text fields must be valid UTF-8") from exc


@asynccontextmanager
async def receive_project_upload(request, destination_dir: str, *, allowed_text_fields: set[str]):
    """Yield one staged upload while holding process and destination-volume admission."""
    destination_dir = os.path.abspath(str(destination_dir or ""))
    if not destination_dir:
        raise UploadRequestError(400, "Invalid upload destination")

    staging_dir = os.path.join(destination_dir, UPLOAD_STAGING_DIRNAME)
    await asyncio.to_thread(_prepare_staging_dir, staging_dir)
    await asyncio.to_thread(_sweep_stale_parts, staging_dir)

    try:
        volume_key = int((await asyncio.to_thread(os.stat, staging_dir)).st_dev)
    except OSError as exc:
        raise UploadRequestError(507, f"Upload staging is unavailable: {exc}") from exc

    server_semaphore, volume_locks = _coordinator_for_current_loop()
    volume_lock = volume_locks.setdefault(volume_key, asyncio.Lock())
    staged_path = ""
    file_handle = None
    started_at = time.monotonic()

    async with server_semaphore:
        async with volume_lock:
            try:
                declared_length = request.content_length
                if declared_length is not None and declared_length > 0:
                    await asyncio.to_thread(_check_disk_space, staging_dir, declared_length)
                else:
                    await asyncio.to_thread(_check_disk_space, staging_dir, 0)

                try:
                    reader = await request.multipart()
                except Exception as exc:
                    raise UploadRequestError(400, "Invalid multipart request") from exc

                seen: set[str] = set()
                fields: dict[str, str] = {}
                text_bytes = 0
                file_size = 0
                original_filename = ""

                while True:
                    field = await _next_part(reader)
                    if field is None:
                        break
                    if not isinstance(field, BodyPartReader):
                        raise UploadRequestError(400, "Nested multipart fields are not supported")
                    name = str(field.name or "")
                    if name in seen:
                        raise UploadRequestError(400, f"Duplicate multipart field: {name or 'unnamed'}")
                    seen.add(name)
                    if name == "file":
                        if not field.filename:
                            raise UploadRequestError(400, "The file field must include a filename")
                        transfer_encoding = str(field.headers.get("Content-Transfer-Encoding") or "").strip().lower()
                        if transfer_encoding not in {"", "binary"}:
                            raise UploadRequestError(400, "Encoded multipart file fields are not supported")
                        original_filename = _sanitize_upload_filename(field.filename)
                        staged_path = os.path.abspath(os.path.join(staging_dir, _staging_filename(original_filename)))
                        with _ACTIVE_PATHS_LOCK:
                            _ACTIVE_PATHS.add(staged_path)
                        try:
                            file_handle = await _await_worker(open, staged_path, "xb")
                            while True:
                                chunk = await _read_part_chunk(field)
                                if not chunk:
                                    break
                                await asyncio.to_thread(_check_disk_space, staging_dir, len(chunk))
                                try:
                                    await _await_worker(file_handle.write, chunk)
                                except OSError as exc:
                                    if exc.errno == errno.ENOSPC:
                                        raise UploadRequestError(507, "Storage became full while importing the file") from exc
                                    raise
                                file_size += len(chunk)
                            await _await_worker(file_handle.flush)
                            await _await_worker(os.fsync, file_handle.fileno())
                        finally:
                            if file_handle is not None:
                                await _await_worker(file_handle.close)
                                file_handle = None
                    elif name in allowed_text_fields:
                        value, text_bytes = await _read_text_field(field, text_bytes)
                        fields[name] = value
                    else:
                        raise UploadRequestError(400, f"Unknown multipart field: {name or 'unnamed'}")

                if "file" not in seen or not staged_path:
                    raise UploadRequestError(400, "A file field is required")
                if file_size <= 0:
                    raise UploadRequestError(400, "The uploaded file is empty")

                upload = StagedUpload(
                    path=staged_path,
                    filename=original_filename,
                    fields=fields,
                    size=file_size,
                    started_at=started_at,
                )
                yield upload
            finally:
                if file_handle is not None:
                    await _await_worker(file_handle.close)
                if staged_path:
                    await asyncio.to_thread(_safe_remove_staging_file, staged_path, staging_dir)
                    with _ACTIVE_PATHS_LOCK:
                        _ACTIVE_PATHS.discard(staged_path)
