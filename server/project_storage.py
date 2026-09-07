"""Immutable payload storage; project_manager owns the lock and commit point."""

import copy
import hashlib
import json
import os
import re
import stat
import uuid

from .atomic_io import atomic_replace
from .path_security import project_state_path, project_state_root

FROZEN_JOB_FIELDS = (
    "prompt_sections", "compiled_prompt_context", "reference_input_snapshots",
    "minimax_h3_setup_snapshot", "reference_item_snapshots", "guide_frame_snapshots",
    "driver_clip_snapshots", "reference_lane_configs", "reference_lane_recipes",
    "driver_lane_configs",
)

INLINE_GENERATION_PARAM_KEYS = frozenset((
    "save_preset", "codec", "pix_fmt", "color_managed", "color_space", "color_range",
    "fps", "selection_start", "scene_id",
))


def asset_component_name(asset_id):
    return "provenance_" + hashlib.sha256(str(asset_id).encode("utf-8")).hexdigest()


def has_generation_provenance(asset):
    return bool(getattr(asset, "_generation_descriptor", None)
                or (asset.generation_params_for_storage if hasattr(asset, "generation_params_for_storage")
                    else getattr(asset, "generation_params", None)))


def generation_summary(params):
    export = params.get("editor_export", {})
    flag = export.get("has_embedded_workflow") if isinstance(export, dict) else None
    return {"has_embedded_workflow": flag if isinstance(flag, bool) else None}


def provenance_revision(params, summary, descriptor=None):
    return hashlib.sha256(json.dumps([descriptor["sha256"] if descriptor else params, summary],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def attach_asset_descriptors(project, data):
    storage = storage_of(data)
    if not storage or "assets" not in storage.get("partitions", []):
        return
    raw_assets = {item.get("asset_id"): item for item in data.get("assets", [])}
    for asset in project.assets:
        raw = raw_assets.get(asset.asset_id, {})
        asset._generation_base_known = True
        asset._generation_summary = copy.deepcopy(raw.get("generation_summary", {}))
        descriptor = storage["components"].get(asset_component_name(asset.asset_id))
        if descriptor is None:
            asset._generation_original = copy.deepcopy(asset.generation_params_for_storage)
            continue
        asset._generation_descriptor = copy.deepcopy(descriptor)
        asset._generation_project_dir = project.project_dir
        asset._generation_unhydrated = True
        asset._generation_projection = asset.generation_params_for_storage
        asset._generation_inline_original = copy.deepcopy(asset.generation_params_for_storage)


def hydrate_asset(asset):
    if not getattr(asset, "_generation_unhydrated", False):
        return asset
    from .project_manager import _project_write_lock
    project_dir = asset._generation_project_dir
    with _project_write_lock(project_dir):
        if not getattr(asset, "_generation_unhydrated", False):
            return asset
        cold = read_component(project_dir, asset_component_name(asset.asset_id), asset._generation_descriptor)
        if not isinstance(cold, dict) or any(key in INLINE_GENERATION_PARAM_KEYS for key in cold):
            raise ProjectStorageError(f"Asset {asset.asset_id}: invalid provenance partition")
        full = {**cold, **asset.generation_params_for_storage}
        object.__setattr__(asset, "generation_params", full)
        asset._generation_original = copy.deepcopy(full)
        asset._generation_unhydrated = False
    return asset


def job_component_name(job_id):
    return "queue_" + hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()


class UnhydratedJobPayload:
    def _refuse(self, *args, **kwargs):
        raise ProjectStorageError("Queue snapshot must be hydrated by job id before use")
    __bool__ = __iter__ = __len__ = __getitem__ = _refuse
    get = items = keys = values = _refuse


def attach_job_descriptors(project, data):
    storage = storage_of(data)
    if not storage or "queue" not in storage.get("partitions", []):
        return
    for job in project.generation_queue:
        descriptor = storage["components"][job_component_name(job.job_id)]
        job._frozen_descriptor = copy.deepcopy(descriptor)
        job._frozen_unhydrated = True
        for field in FROZEN_JOB_FIELDS:
            setattr(job, field, UnhydratedJobPayload())


def hydrate_job(project, job):
    if job is None or not getattr(job, "_frozen_unhydrated", False):
        return job
    from .project_manager import _project_write_lock
    with _project_write_lock(project.project_dir):
        payload = read_component(project.project_dir, job_component_name(job.job_id), job._frozen_descriptor)
    if not isinstance(payload, dict) or any(field not in payload for field in FROZEN_JOB_FIELDS):
        raise ProjectStorageError(f"Queue {job.job_id}: incomplete frozen snapshot")
    for field in FROZEN_JOB_FIELDS:
        setattr(job, field, copy.deepcopy(payload[field]))
    job._frozen_original = payload
    job._frozen_unhydrated = False
    return job


def resolve_queue_job(project, job_id):
    return hydrate_job(project, next((job for job in project.generation_queue
                                     if job.job_id == job_id), None))


class ProjectStorageError(ValueError):
    """A durable component cannot safely be read or published."""


def storage_of(data):
    if "storage" not in data:
        # Legacy reader expires only after a breaking release ships an offline
        # inventory scanner and exact-byte rollback tool, with no legacy roots
        # remaining in the maintainer inventory.
        return None
    storage = data["storage"]
    if (not isinstance(storage, dict)
            or type(storage.get("format_version")) is not int
            or storage["format_version"] not in (1, 2)
            or not isinstance(storage.get("components"), dict)):
        raise ProjectStorageError("Unsupported or malformed project storage format")
    if "prompt_history" not in storage["components"]:
        raise ProjectStorageError("prompt_history: required component descriptor is missing")
    partitions = storage.get("partitions", [])
    if (not isinstance(partitions, list) or any(part not in {"queue", "assets"} for part in partitions)
            or len(partitions) != len(set(partitions))):
        raise ProjectStorageError("Unsupported or malformed storage partitions")
    if "queue" not in partitions and any(name.startswith("queue_") for name in storage["components"]):
        raise ProjectStorageError("Queue partition declaration is missing")
    if "assets" not in partitions and any(name.startswith("provenance_") for name in storage["components"]):
        raise ProjectStorageError("Asset provenance partition declaration is missing")
    if "assets" not in partitions and any(asset.get("has_frozen_provenance") for asset in data.get("assets", [])):
        raise ProjectStorageError("Asset provenance marker requires its storage partition")
    return storage


def descriptor_path(project_dir, name, descriptor, *, state_root=None):
    if not isinstance(descriptor, dict):
        raise ProjectStorageError(f"{name}: malformed component descriptor")
    digest = descriptor.get("sha256")
    count = descriptor.get("bytes")
    path = descriptor.get("path")
    if (not re.fullmatch(r"prompt_history|history_entry|(?:queue|provenance)_[0-9a-f]{64}", name)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(count) is not int or count < 0
            or path != f"state/{name}-{digest}.json"):
        raise ProjectStorageError(f"{name}: invalid component path, hash, or byte length")
    if state_root is not None:
        return os.path.join(state_root, path[6:])
    resolved = project_state_path(project_dir, path)
    if not resolved:
        raise ProjectStorageError(f"{name}: component path is not contained")
    return resolved


def read_component(project_dir, name, descriptor, *, state_root=None, state_entries=None):
    path = descriptor_path(project_dir, name, descriptor, state_root=state_root)
    if state_entries is not None:
        info = state_entries.get(os.path.basename(path))
        if (info is None or not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ProjectStorageError(f"{name}: missing or non-regular component")
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        raise ProjectStorageError(f"{name}: missing or unreadable component: {path}") from exc
    if len(payload) != descriptor["bytes"]:
        raise ProjectStorageError(f"{name}: component byte length mismatch")
    if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
        raise ProjectStorageError(f"{name}: component SHA-256 mismatch")
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise ProjectStorageError(f"{name}: component JSON is invalid") from exc


def sync_directory(directory):
    # Python's Windows fsync supports files, not directory handles. Components
    # still fail closed after power loss; do not promise filesystem durability.
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_bytes(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp, path)
        sync_directory(os.path.dirname(path))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def publish_component(project_dir, name, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    descriptor = {"path": f"state/{name}-{digest}.json", "sha256": digest, "bytes": len(payload)}
    path = descriptor_path(project_dir, name, descriptor)
    if os.path.exists(path):
        read_component(project_dir, name, descriptor)
    else:
        publish_bytes(path, payload)
    return descriptor


def validate_storage(project_dir, data):
    storage = storage_of(data)
    if storage is None:
        return
    # All object names are validated single filename components. Resolve the
    # state root once per locked pass, then reject reparse/non-regular children
    # from one directory snapshot; do not re-walk ancestors for every object.
    state_root = project_state_root(project_dir, must_exist=True)
    if not state_root:
        raise ProjectStorageError("Project state directory is missing or invalid")
    with os.scandir(state_root) as entries:
        state_entries = {entry.name: entry.stat(follow_symlinks=False) for entry in entries}
    cache = {}
    def read(name, descriptor):
        path = descriptor_path(project_dir, name, descriptor, state_root=state_root)
        key = (path, descriptor["sha256"], descriptor["bytes"])
        if key not in cache:
            cache[key] = read_component(project_dir, name, descriptor, state_root=state_root, state_entries=state_entries)
        return cache[key]

    for name, descriptor in storage["components"].items():
        if name != "prompt_history" and not re.fullmatch(r"(?:queue|provenance)_[0-9a-f]{64}", name):
            raise ProjectStorageError(f"{name}: unsupported storage component")
        if name == "prompt_history":
            _read_history_component(project_dir, descriptor, storage["format_version"], read=read)
        else:
            read(name, descriptor)
    if "queue" in storage.get("partitions", []):
        for job in data.get("generation_queue", []):
            name = job_component_name(job["job_id"])
            if name not in storage["components"]:
                raise ProjectStorageError(f"Queue {job['job_id']}: required component descriptor is missing")
    else:
        for job in data.get("generation_queue", []):
            if any(field not in job for field in FROZEN_JOB_FIELDS):
                raise ProjectStorageError("Inline queue snapshot is incomplete without a queue partition")
    if "assets" in storage.get("partitions", []):
        for asset in data.get("assets", []):
            if asset.get("has_frozen_provenance"):
                if asset_component_name(asset["asset_id"]) not in storage["components"]:
                    raise ProjectStorageError(f"Asset {asset['asset_id']}: required provenance descriptor is missing")


def _read_history_component(project_dir, descriptor, format_version=None, *, read=None):
    """Read format-1 history or a format-2 index and its immediate entry objects."""
    if read is None:
        read = lambda name, desc: read_component(project_dir, name, desc)
    value = read("prompt_history", descriptor)
    # Maintainer format-1 projects remain readable until their next history write.
    # Remove this branch only once that inventory has migrated to indexed history.
    if isinstance(value, list) and format_version != 2:
        return value, []
    if (format_version == 1 or not isinstance(value, dict)
            or value.get("kind") != "prompt_history_index"
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("entries"), list)):
        raise ProjectStorageError("prompt_history: malformed or unsupported history index")
    entries = value["entries"]
    return [read("history_entry", entry) for entry in entries], entries


def publish_history(project_dir, history):
    if not isinstance(history, list):
        raise ProjectStorageError("prompt_history: expected a history list")
    entries = [publish_component(project_dir, "history_entry", entry) for entry in history]
    return publish_component(project_dir, "prompt_history", {
        "kind": "prompt_history_index", "schema_version": 1, "entries": entries})


def read_prompt_history(project_or_dir):
    from .project_manager import _project_write_lock, _read_project_json
    if isinstance(project_or_dir, str):
        project_dir = project_or_dir
        with _project_write_lock(project_dir):
            data = _read_project_json(os.path.join(project_dir, "project.json"))
            storage = storage_of(data)
            if storage and "prompt_history" in storage["components"]:
                return _read_history_component(project_dir, storage["components"]["prompt_history"], storage["format_version"])[0]
            return copy.deepcopy(data.get("metadata", {}).get("prompt_history", []))
    project = project_or_dir
    staged = getattr(project, "_staged_components", {})
    if "prompt_history" in staged:
        return copy.deepcopy(staged["prompt_history"][1])
    storage = storage_of(project._raw_data)
    if storage and "prompt_history" in storage["components"]:
        with _project_write_lock(project.project_dir):
            return _read_history_component(project.project_dir, storage["components"]["prompt_history"], storage["format_version"])[0]
    return copy.deepcopy(project.metadata.get("prompt_history", []))


def read_asset_provenance(project_dir, asset_id):
    result = read_asset_provenance_batch(project_dir, [asset_id])
    if asset_id not in result["provenance"]:
        raise FileNotFoundError("Asset not found")
    return {"asset_id": asset_id, "modified_at": result["modified_at"],
            "generation_params": result["provenance"][asset_id]}


def read_asset_provenance_batch(project_dir, asset_ids):
    from .project_manager import _project_write_lock, _read_project_json
    with _project_write_lock(project_dir):
        data = _read_project_json(os.path.join(project_dir, "project.json"))
        storage = storage_of(data)
        wanted = set(asset_ids)
        values, revisions = {}, {}
        for asset in data.get("assets", []):
            asset_id = asset.get("asset_id")
            if asset_id not in wanted:
                continue
            params = copy.deepcopy(asset.get("generation_params", {}))
            descriptor = storage["components"].get(asset_component_name(asset_id)) if storage else None
            if asset.get("has_frozen_provenance") and descriptor is None:
                raise ProjectStorageError(f"Asset {asset_id}: required provenance descriptor is missing")
            summary = {**{key: value for key, value in params.items() if key in INLINE_GENERATION_PARAM_KEYS},
                       **(asset.get("generation_summary", {}) if descriptor else generation_summary(params))}
            revisions[asset_id] = provenance_revision(params, summary, descriptor)
            if descriptor:
                params = {**read_component(project_dir, asset_component_name(asset_id), descriptor), **params}
            values[asset_id] = params
        return {"modified_at": data.get("modified_at", ""), "provenance": values, "revisions": revisions}


def stage_prompt_history(project, history):
    storage = storage_of(project._raw_data)
    base = storage["components"].get("prompt_history") if storage else None
    staged = getattr(project, "_staged_components", None)
    if staged is None:
        staged = project._staged_components = {}
    staged["prompt_history"] = (copy.deepcopy(base), copy.deepcopy(history))


def assemble_for_save(project, data, current, *, bump_modified_at):
    """Called only inside the manager's commit lock; never commits the root."""
    from .project_manager import ProjectVersionConflict, project_conflict_projection
    old = storage_of(current)
    staged = getattr(project, "_staged_components", {})
    if staged and not bump_modified_at:
        raise ProjectStorageError("A no-bump save cannot stage a frozen component")
    has_history = isinstance(data.get("metadata"), dict) and "prompt_history" in data["metadata"]
    has_provenance = any(asset.get("generation_params") for asset in data.get("assets", []))
    migrate = old is None and bump_modified_at and (has_history or bool(staged) or bool(data.get("generation_queue")) or has_provenance)
    if old is None and not migrate:
        return data
    job_ids = [job.get("job_id") for job in data.get("generation_queue", [])]
    if any(not isinstance(job_id, str) or not job_id for job_id in job_ids) or len(job_ids) != len(set(job_ids)):
        raise ProjectStorageError("Queue snapshot storage requires unique nonempty job ids")
    asset_ids = [asset.get("asset_id") for asset in data.get("assets", [])]
    if any(not isinstance(asset_id, str) or not asset_id for asset_id in asset_ids) or len(asset_ids) != len(set(asset_ids)):
        raise ProjectStorageError("Provenance storage requires unique nonempty asset ids")
    if old:
        validate_storage(project.project_dir, current)
    storage = copy.deepcopy(old) if old else {"format_version": 2, "components": {}}
    components = storage["components"]
    for name, (base, value) in staged.items():
        if components.get(name) != base:
            raise ProjectVersionConflict(project_dir=project.project_dir,
                expected_modified_at=str(project._raw_data.get("modified_at", "")),
                actual_modified_at=str(current.get("modified_at", "")),
                current_data=project_conflict_projection(current))
    if migrate and current:
        # Backup original bytes, not a reconstruction. It is permanent and is
        # verified before the first split document can become authoritative.
        with open(os.path.join(project.project_dir, "project.json"), "rb") as handle:
            legacy = handle.read()
        digest = hashlib.sha256(legacy).hexdigest()
        backup = project_state_path(project.project_dir, f"legacy/project-{digest}.json")
        if not backup:
            raise ProjectStorageError("Legacy backup path is not contained")
        if not os.path.exists(backup):
            publish_bytes(backup, legacy)
        with open(backup, "rb") as handle:
            if handle.read() != legacy:
                raise ProjectStorageError("Legacy backup byte verification failed")
    previous = copy.deepcopy(components)
    if migrate:
        components["prompt_history"] = publish_history(project.project_dir,
            staged["prompt_history"][1] if "prompt_history" in staged
            else data.get("metadata", {}).get("prompt_history", []))
    for name, (_, value) in staged.items():
        if name != "prompt_history":
            raise ProjectStorageError(f"Unsupported staged component: {name}")
        if not migrate:
            components[name] = publish_history(project.project_dir, value)
        storage["format_version"] = 2
    split_queue = bump_modified_at or "queue" in storage.get("partitions", [])
    if split_queue:
        jobs_by_id = {job.job_id: job for job in project.generation_queue}
        queue_names = set()
        for header in data.get("generation_queue", []):
            job = jobs_by_id[header["job_id"]]
            name = job_component_name(job.job_id)
            queue_names.add(name)
            payload = {field: header.pop(field) for field in FROZEN_JOB_FIELDS if field in header}
            if hasattr(job, "_frozen_descriptor"):
                if name not in components:
                    raise ProjectStorageError(f"Queue {job.job_id}: current snapshot descriptor is missing")
                if not getattr(job, "_frozen_unhydrated", False) and payload != job._frozen_original:
                    raise ProjectStorageError(f"Queue {job.job_id}: frozen snapshot cannot be modified")
            else:
                if not bump_modified_at:
                    raise ProjectStorageError("A no-bump save cannot stage queue snapshots")
                if name not in components:
                    components[name] = publish_component(project.project_dir, name, payload)
        for name in list(components):
            if name.startswith("queue_") and name not in queue_names:
                del components[name]
        storage["partitions"] = sorted(set(storage.get("partitions", [])) | {"queue"})
    if bump_modified_at or "assets" in storage.get("partitions", []):
        assets_by_id = {asset.asset_id: asset for asset in project.assets}
        current_assets = {asset["asset_id"]: asset for asset in current.get("assets", [])}
        asset_names = set()
        for record in data.get("assets", []):
            asset = assets_by_id[record["asset_id"]]
            name = asset_component_name(asset.asset_id)
            params = record.get("generation_params", {})
            if getattr(asset, "_generation_unhydrated", False):
                record["generation_summary"] = copy.deepcopy(getattr(asset, "_generation_summary", {}))
                if (asset.generation_params_for_storage is not asset._generation_projection
                        or params != asset._generation_inline_original):
                    raise ProjectStorageError(f"Asset {asset.asset_id}: cannot save changed unhydrated provenance")
                if name not in components:
                    raise ProjectStorageError(f"Asset {asset.asset_id}: current provenance descriptor is missing")
            else:
                record["generation_summary"] = generation_summary(params)
                cold = {key: value for key, value in params.items() if key not in INLINE_GENERATION_PARAM_KEYS}
                original = getattr(asset, "_generation_original", None)
                changed = original is None or params != original
                if changed:
                    base = getattr(asset, "_generation_descriptor", None)
                    base_known = getattr(asset, "_generation_base_known", False)
                    if (base is not None or base_known) and components.get(name) != base:
                        raise ProjectVersionConflict(project_dir=project.project_dir,
                            expected_modified_at=str(project._raw_data.get("modified_at", "")),
                            actual_modified_at=str(current.get("modified_at", "")),
                            current_data=project_conflict_projection(current))
                    if not bump_modified_at and (cold or base is not None):
                        raise ProjectStorageError("A no-bump save cannot stage asset provenance")
                    if cold:
                        # A stale legacy model has not declared a replacement
                        # for a descriptor that only appeared after it loaded.
                        if base is not None or base_known or name not in components:
                            components[name] = publish_component(project.project_dir, name, cold)
                    elif base is not None or base_known:
                        components.pop(name, None)
            record["generation_params"] = {key: value for key, value in params.items()
                                           if key in INLINE_GENERATION_PARAM_KEYS}
            if name in components:
                prior = getattr(asset, "_generation_descriptor", None)
                if prior != components[name] and (
                        getattr(asset, "_generation_unhydrated", False)
                        or params == getattr(asset, "_generation_original", None)
                        or (prior is None and name in (old or {}).get("components", {}))):
                    record["generation_summary"] = copy.deepcopy(current_assets.get(asset.asset_id, {}).get("generation_summary", {}))
                record["has_frozen_provenance"] = True
                asset_names.add(name)
            else:
                record.pop("has_frozen_provenance", None)
        for name in list(components):
            if name.startswith("provenance_") and name not in asset_names:
                del components[name]
        storage["partitions"] = sorted(set(storage.get("partitions", [])) | {"assets"})
    if old and has_history and "prompt_history" not in staged:
        # A stale legacy model may still have inline history. Its undeclared
        # payload cannot replace the current disk component.
        if storage_of(project._raw_data) is not None:
            raise ProjectStorageError("Prompt history must be explicitly staged")
    if isinstance(data.get("metadata"), dict):
        data["metadata"].pop("prompt_history", None)
    storage["recovery_previous"] = previous
    if bump_modified_at:
        storage["authoring_previous"] = previous
    data["storage"] = storage
    return data


def collect_unreferenced_components(project_dir):
    """Maintenance worker/explicit sweep only; never inline in a load or save."""
    from .project_manager import _project_write_lock, _read_project_json
    with _project_write_lock(project_dir):
        data = _read_project_json(os.path.join(project_dir, "project.json"))
        storage = storage_of(data)
        if not storage:
            return []
        validate_storage(project_dir, data)
        keep = set()
        from .project_storage_lifecycle import live_descriptors
        leases = live_descriptors(project_dir)
        leased = [(lease.name, lease.descriptor) for lease in leases]
        for name, descriptor in leased:
            keep.add(descriptor_path(project_dir, name, descriptor))
            if name == "prompt_history":
                _, entries = _read_history_component(project_dir, descriptor)
                for child in entries:
                    keep.add(descriptor_path(project_dir, "history_entry", child))
        for group in ("components", "recovery_previous", "authoring_previous"):
            descriptors = storage.get(group, {})
            if not isinstance(descriptors, dict):
                raise ProjectStorageError(f"Invalid retained component set: {group}")
            for name, descriptor in descriptors.items():
                keep.add(descriptor_path(project_dir, name, descriptor))
                if name == "prompt_history":
                    _, entries = _read_history_component(project_dir, descriptor)
                    for child in entries:
                        keep.add(descriptor_path(project_dir, "history_entry", child))
        root = project_state_root(project_dir, must_exist=True)
        removed = []
        for entry in os.scandir(root):
            if not re.fullmatch(r"(?:prompt_history|history_entry|(?:queue|provenance)_[0-9a-f]{64})-[0-9a-f]{64}\.json", entry.name):
                continue
            path = project_state_path(project_dir, entry.name)
            if path and path not in keep and entry.is_file(follow_symlinks=False):
                os.remove(path)
                removed.append(entry.name)
        return removed
