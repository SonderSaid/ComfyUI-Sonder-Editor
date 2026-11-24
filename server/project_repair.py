"""Check a project's stored components and repair what is missing or damaged.

Nothing here runs on a load: a check is read-only, and a repair is the user's explicit
choice, made against the plan the check showed. Until the repaired ``project.json`` is
written, no existing file is moved or changed, so a repair that fails leaves the project
as it was. The old document is kept byte for byte beside the components
(``state/<sha256>.bak``), and every component file there is now is held from collection,
so the two together stay what they were.

A file that cannot be read is not a missing file. Only a file absent from a ``state``
folder that was listed, or one read in full with the wrong content, is repaired; anything
else makes the project not repairable for now, because a disconnected drive would
otherwise be repaired into permanent loss.
"""
import copy
import hashlib
import json
import logging
import os
import stat
import time

from . import project_storage as ps
from .path_security import project_state_path, project_state_root

logger = logging.getLogger("sonder_editor")

INTACT, ABSENT, CORRUPT, UNAVAILABLE = "intact", "absent", "corrupt", "unavailable"

HEALTHY, DAMAGED, UNREPAIRABLE = "healthy", "damaged", "unrepairable"

_GROUPS = ("recovery_previous", "authoring_previous")


class RepairPlanStale(Exception):
    """The project, or what is damaged in it, is not what the confirmed check showed."""


class RepairRefused(Exception):
    """The project cannot be repaired. ``reason`` is the check's reason code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Unrepairable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def inspect_storage(project_dir):
    """``{status, reason, problems, digest}``. Reads only."""
    from .project_manager import _project_write_lock
    with _project_write_lock(project_dir):
        return _Plan(project_dir).report()


def repair_storage(project_dir, digest):
    """Carry out the plan ``digest`` confirmed: ``{backup, left_in_place, reopened}``.

    Raises `RepairPlanStale` when the plan changed since the check, `RepairRefused` when
    the project cannot be repaired, and `ProjectBusyError` before anything was written.
    Once the root is written the repair has happened, and nothing after that raises.
    """
    from .project_manager import (
        _next_modified_at, _project_commit_lock, load_project, notify_project_saved, write_project_root)
    with _project_commit_lock(project_dir) as held:
        if not held:  # no lock to keep another process's save or sweep out
            raise RepairRefused("no_locks")
        plan = _Plan(project_dir)
        report = plan.report()
        if report["status"] == UNREPAIRABLE:
            raise RepairRefused(report["reason"])
        if report["digest"] != digest:
            raise RepairPlanStale()
        if report["status"] == HEALTHY:
            raise RepairRefused("healthy")
        backup = ps.backup_root(project_dir, plan.root)
        repaired = plan.repaired_document()
        _hold_present_files(project_dir, plan, backup)
        ps.validate_storage(project_dir, repaired)
        plan.confirm_loadable(repaired)
        referenced = ps.document_names(project_dir, repaired["storage"])
        ps.restart_retention(project_dir, repaired["storage"])
        repaired["modified_at"] = _next_modified_at(str(plan.data.get("modified_at", "") or ""))
        write_project_root(project_dir, repaired)
        left_in_place = _set_corrupt_files_aside(project_dir, plan, referenced)
    try:
        notify_project_saved(load_project(project_dir))
        reopened = True
    except Exception:  # noqa: BLE001 - the repair is on disk; saying it failed would be false
        logger.exception("A repaired project could not be reopened")
        reopened = False
    return {"backup": f"state/{backup}", "left_in_place": left_in_place, "reopened": reopened}


class _Plan:
    """What is damaged and what a repair would do. Deterministic for one state on disk:
    every outcome is decided here, so the digest covers all of them."""

    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.problems = []
        self.states = {}         # file name -> state
        self.restored = {}       # component name -> the descriptor that still has its bytes
        self.retained = {}       # group -> the components it can keep
        self.history = None      # the replacement, see _inspect_history
        self.data = self.storage = None
        self.reason = ""
        try:
            self.root = ps._read_bytes(os.path.join(project_dir, "project.json"))
            self._inspect()
        except OSError:
            self.reason = "unavailable"
        except _Unrepairable as exc:
            self.reason = exc.reason
        except (TypeError, AttributeError, KeyError, RecursionError):
            # A hand-edited or foreign document in a shape nothing here expects.
            logger.warning("A project's document has an unexpected shape", exc_info=True)
            self.reason = "invalid_storage"

    # -- reading -------------------------------------------------------------

    def _inspect(self):
        try:
            self.data = json.loads(self.root.decode("utf-8"))  # as the loader reads it: no BOM, no UTF-16
        except ValueError:
            raise _Unrepairable("invalid_json") from None
        if not isinstance(self.data, dict):
            raise _Unrepairable("invalid_json")
        if ps.stored_format_version(self.data) > ps.CURRENT_FORMAT_VERSION:
            raise _Unrepairable("too_new")
        try:
            self.storage = ps.storage_of(self.data)
        except ps.ProjectStorageError:
            raise _Unrepairable("invalid_storage") from None
        if self.storage is None:
            return  # legacy layout: everything is in project.json, which parsed
        self.version = self.storage["format_version"]
        self._list_state()
        self._inspect_history()
        self._inspect_provenance()
        self._inspect_queue()
        self._inspect_unused()
        self._inspect_leftovers()
        if not self.problems:
            self._confirm_healthy()
            return
        self._inspect_retained()
        try:
            ps.read_held(self.project_dir)
        except ps.ProjectStorageError:
            raise _Unrepairable("held_unreadable") from None
        try:
            json.dumps(self.data, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:  # no save can write it either
            raise _Unrepairable("invalid_storage") from None

    def _list_state(self):
        root = project_state_root(self.project_dir, must_exist=True)
        try:
            self.files = ps._component_entries(root) if root and os.path.isdir(root) else None
        except OSError:
            self.files = None
        if self.files is None:
            raise _Unrepairable("state_unreadable")
        self.lower = {name.lower() for name in self.files}

    def _read(self, name, descriptor, check=None):
        """``(state, value, descriptor)`` of one component. ``check`` raises for content its
        reader refuses. The same bytes under the other spelling of the name count as found,
        and the descriptor returned names the file that has them."""
        try:
            named = ps._descriptor_name(self.project_dir, name, descriptor)
        except ps.ProjectStorageError:
            raise _Unrepairable("invalid_storage") from None
        state, value = self._read_content(named, descriptor, check)
        if state in (ABSENT, CORRUPT):
            digest = descriptor["sha256"]
            for path in (f"state/{digest}.json", f"state/{name}-{digest}.json"):
                if path != descriptor["path"]:
                    other = {**descriptor, "path": path}
                    found, other_value = self._read_content(os.path.basename(path), other, check)
                    if found == INTACT:
                        return INTACT, other_value, other
        return state, value, descriptor

    def _read_content(self, file_name, descriptor, check):
        state, payload = self._read_file(file_name)
        if state == INTACT:
            if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
                state = CORRUPT
            elif len(payload) != descriptor["bytes"]:
                # The file is what its name says; the document describes it wrongly.
                raise _Unrepairable("invalid_storage")
        self.states[file_name] = state
        if state == UNAVAILABLE:
            raise _Unrepairable("unavailable")
        if state != INTACT:
            return state, None
        try:
            value = json.loads(payload)
            if check is not None:
                check(value)
        except (ValueError, ps.ProjectStorageError):
            return CORRUPT, None
        return INTACT, value

    def _read_file(self, file_name):
        info = self.files.get(file_name)
        if info is None:
            # Windows opens another case of the name; the listing does not show it as this one.
            return (UNAVAILABLE if file_name.lower() in self.lower else ABSENT), None
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            return UNAVAILABLE, None
        path = project_state_path(self.project_dir, file_name)
        if not path:
            return UNAVAILABLE, None
        try:
            return INTACT, ps._read_bytes(path)
        except OSError:  # gone since the listing included: not a stable absence
            return UNAVAILABLE, None

    # -- history -------------------------------------------------------------

    def _read_index(self, descriptor, version):
        """``(state, kept, lost, descriptor, moved)`` of an index. ``kept`` are the entries
        that can still be read and ``moved`` whether one is under its other spelling. A
        format-1 history has no entry files: ``kept`` is None."""
        state, value, descriptor = self._read(
            "prompt_history", descriptor, lambda value: ps._history_entry_descriptors(value, version))
        if state != INTACT:
            return state, None, 0, descriptor, False
        entries = ps._history_entry_descriptors(value, version)
        if entries is None:
            return INTACT, None, 0, descriptor, False
        kept = []
        for entry in entries:
            found, _value, entry = self._read("history_entry", entry)
            if found == INTACT:
                kept.append(entry)
        return INTACT, kept, len(entries) - len(kept), descriptor, kept != entries and len(kept) == len(entries)

    def _inspect_history(self):
        """``self.history`` is the replacement: a descriptor to use as it is, the entries of a
        new index, or a format-1 list to convert."""
        current = self.storage["components"]["prompt_history"]
        state, kept, lost, found, moved = self._read_index(current, self.version)
        if state == INTACT:
            if lost:
                self.history = {"entries": kept}
                self.problems.append({"kind": "history", "state": "entries", "outcome": "entries_dropped",
                                      "kept": len(kept), "lost": lost})
            elif moved or found != current:
                self.history = {"entries": kept} if moved else {"descriptor": found}
                self.problems.append({"kind": "history", "state": "absent", "outcome": "restored",
                                      "kept": len(kept) if kept is not None else None, "lost": 0})
            return
        for outcome, candidate in self._history_candidates(current):
            # The document's own retained sets must be judged; a file nothing names may be
            # anything, and is only skipped.
            try:
                answer = self._candidate(candidate)
            except _Unrepairable as exc:
                if outcome == "previous" and exc.reason == "unavailable":
                    raise
                continue
            if answer is None:
                continue
            self.history, kept, lost = answer
            self.problems.append({"kind": "history", "state": state, "outcome": outcome, "kept": kept, "lost": lost})
            return
        self.history = {"entries": []}
        self.problems.append({"kind": "history", "state": state, "outcome": "emptied", "kept": 0, "lost": None})

    def _candidate(self, descriptor):
        """``(history, kept, lost)`` of a usable replacement index, or None. One with nothing
        left to keep is no better than an empty history."""
        found, kept, lost, descriptor, moved = self._read_index(descriptor, None)
        if found != INTACT:
            return None
        if kept is None:
            values = self._read("prompt_history", descriptor)[1]
            if not values:
                return None
            return ({"descriptor": descriptor} if self.version == 1 else {"convert": descriptor}), len(values), 0
        if not kept or self.version == 1:
            return None
        return ({"entries": kept} if lost or moved else {"descriptor": descriptor}), len(kept), lost

    def _history_candidates(self, current):
        """The retained sets, then any index left in the folder, newest first: that one may
        not be what was last saved."""
        named = {os.path.basename(str(current["path"]))}
        for group in _GROUPS:
            retained = self.storage.get(group)
            descriptor = retained.get("prompt_history") if isinstance(retained, dict) else None
            if isinstance(descriptor, dict) and os.path.basename(str(descriptor.get("path", ""))) not in named:
                named.add(os.path.basename(str(descriptor.get("path", ""))))
                yield "previous", descriptor
        for group in ("components", *_GROUPS):
            descriptors = self.storage.get(group)
            if isinstance(descriptors, dict):
                named |= {os.path.basename(str(d.get("path", ""))) for d in descriptors.values() if isinstance(d, dict)}
        for name, payload in self._leftover_files(named):
            found = hashlib.sha256(payload).hexdigest()
            if name not in (f"{found}.json", f"prompt_history-{found}.json"):
                continue
            try:
                value = json.loads(payload)
                if not isinstance(value, dict):
                    continue
                ps._history_entry_descriptors(value)
            except (ValueError, ps.ProjectStorageError):
                continue
            yield "salvaged", {"path": f"state/{name}", "sha256": found, "bytes": len(payload)}

    def _leftover_files(self, named):
        """``(name, bytes)`` of readable component files not in ``named``, newest first."""
        for name in sorted((name for name in self.files if name not in named and ps._COMPONENT_FILE.fullmatch(name)),
                           key=lambda name: (-self.files[name].st_mtime_ns, name)):
            state, payload = self._read_file(name)
            if state == INTACT:
                yield name, payload

    # -- provenance, queue and the rest --------------------------------------

    def _inspect_record(self, name, check, problem):
        """A component a record needs. ``problem`` is what its loss is reported as."""
        descriptor = self.storage["components"].get(name)
        state, found = ABSENT, None
        if descriptor is not None:
            state, _value, found = self._read(name, descriptor, check)
        if state != INTACT:
            self.problems.append({**problem, "state": state, "outcome": "dropped"})
        elif found != descriptor:
            self.restored[name] = found
            self.problems.append({**problem, "state": ABSENT, "outcome": "restored"})

    def _inspect_provenance(self):
        for asset in self._records("assets"):
            name = ps.asset_component_name(asset["asset_id"])
            # A load attaches a component to its asset with or without the marker.
            if asset.get("has_frozen_provenance") or name in self.storage["components"]:
                self._inspect_record(
                    name, lambda cold, a=asset: ps.check_provenance_payload(a["asset_id"], cold),
                    {"kind": "details", "asset_id": asset["asset_id"],
                     "name": str(asset.get("name") or asset["asset_id"])})

    def _inspect_queue(self):
        jobs = self._records("generation_queue")
        if "queue" not in self.storage.get("partitions", []):
            if any(field not in job for job in jobs for field in ps.FROZEN_JOB_FIELDS):
                raise _Unrepairable("invalid_storage")
            return
        for job in jobs:
            self._inspect_record(
                ps.job_component_name(job["job_id"]),
                lambda payload, j=job: ps.check_queue_payload(j["job_id"], payload),
                {"kind": "job", "job_id": job["job_id"], "status": str(job.get("status") or "pending")})

    def _inspect_unused(self):
        """Components no record names: a load still reads them."""
        named = {"prompt_history"}
        named |= {ps.asset_component_name(asset["asset_id"]) for asset in self._records("assets")}
        named |= {ps.job_component_name(job["job_id"]) for job in self._records("generation_queue")}
        for name, descriptor in self.storage["components"].items():
            if name not in named:
                state, _value, found = self._read(name, descriptor)
                if state != INTACT or found != descriptor:
                    self.problems.append({"kind": "unused", "state": state, "outcome": "dropped", "component": name})

    def _inspect_leftovers(self):
        """Damaged files nothing names. One would fail the next save that publishes the
        content its name promises."""
        for name, payload in self._leftover_files(set(self.states)):
            digest = hashlib.sha256(payload).hexdigest()
            if not name.endswith(f"{digest}.json"):
                self.states[name] = CORRUPT
        corrupt = [name for name, state in self.states.items() if state == CORRUPT]
        if corrupt and not self.problems:
            self.problems.append({"kind": "leftover", "state": CORRUPT, "outcome": "set_aside", "count": len(corrupt)})

    def _inspect_retained(self):
        """What each retained set can keep. Nothing loads these, so a component that is gone,
        damaged or wrongly described is only dropped from its set."""
        for group in _GROUPS:
            if group not in self.storage:
                continue
            descriptors = self.storage[group]
            kept = self.retained[group] = {}
            for name, descriptor in descriptors.items() if isinstance(descriptors, dict) else ():
                check = (lambda value: ps._history_entry_descriptors(value)) if name == "prompt_history" else None
                try:
                    state, value, found = self._read(name, descriptor, check)
                    if state == INTACT and name == "prompt_history":  # its entries must at least be well named
                        for entry in ps._history_entry_descriptors(value) or []:
                            ps._descriptor_name(self.project_dir, "history_entry", entry)
                except ps.ProjectStorageError:
                    continue
                except _Unrepairable as exc:
                    if exc.reason == "unavailable":
                        raise
                    continue
                if state == INTACT:
                    kept[name] = found

    def _records(self, key):
        records = self.data.get(key, [])
        id_key = "asset_id" if key == "assets" else "job_id"
        if not isinstance(records, list) or not all(
                isinstance(item, dict) and isinstance(item.get(id_key), str) and item[id_key] for item in records):
            raise _Unrepairable("invalid_storage")
        return records

    def _confirm_healthy(self):
        """A load may refuse for a reason this check has no repair for."""
        for attempt in (0, 1):
            try:
                ps.validate_storage(self.project_dir, self.data)
                return
            except ps.ProjectStorageError:
                time.sleep(0.1)  # once more: another program may have held a file for a moment
        raise _Unrepairable("invalid_storage")

    def confirm_loadable(self, document):
        """The repaired document must build a project, or it must not be written."""
        from .timeline_state import TimelineProject
        try:
            project = TimelineProject.from_dict(copy.deepcopy(document), project_dir=self.project_dir)
            ps.attach_job_descriptors(project, document)
            ps.attach_asset_descriptors(project, document)
        except Exception:  # noqa: BLE001 - whatever a load would raise
            logger.exception("A repaired document would not load")
            raise RepairRefused("invalid_storage") from None

    # -- the plan ------------------------------------------------------------

    def report(self):
        if self.reason:
            return {"status": UNREPAIRABLE, "reason": self.reason, "problems": [], "digest": ""}
        plan = {"root": hashlib.sha256(self.root).hexdigest(), "states": self.states, "history": self.history,
                "restored": self.restored, "retained": self.retained, "problems": self.problems}
        return {"status": DAMAGED if self.problems else HEALTHY, "reason": "", "problems": self.problems,
                "digest": hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()}

    def repaired_document(self):
        """The document after the repair. Publishes the replacement history it needs."""
        data = copy.deepcopy(self.data)
        storage = data["storage"]
        components = storage["components"]
        if self.history is not None:
            components["prompt_history"] = self._replacement_history()
        components.update(self.restored)
        dropped = [item for item in self.problems if item["outcome"] == "dropped"]
        assets = {item["asset_id"] for item in dropped if item["kind"] == "details"}
        for asset in data.get("assets", []):
            if asset["asset_id"] in assets:
                asset.pop("has_frozen_provenance", None)
                params = asset.get("generation_params")
                asset["generation_summary"] = ps.generation_summary(params if isinstance(params, dict) else {})
                components.pop(ps.asset_component_name(asset["asset_id"]), None)
        jobs = {item["job_id"] for item in dropped if item["kind"] == "job"}
        if jobs:
            data["generation_queue"] = [job for job in data["generation_queue"] if job["job_id"] not in jobs]
            for job_id in jobs:
                components.pop(ps.job_component_name(job_id), None)
        for item in dropped:
            if item["kind"] == "unused":
                components.pop(item["component"], None)
        storage.update(copy.deepcopy(self.retained))
        return data

    def _replacement_history(self):
        if "descriptor" in self.history:
            return self.history["descriptor"]
        if "convert" in self.history:
            values = self._read("prompt_history", self.history["convert"])[1]
            entries = [self._publish("history_entry", value) for value in values]
        else:
            entries = self.history["entries"]
        return self._publish("prompt_history", [] if self.version == 1 else {
            "kind": "prompt_history_index", "schema_version": 1, "entries": entries})

    def _publish(self, name, value):
        """Publish a component where no damaged file has the name: that file may not be
        touched yet, and publication refuses to take its place."""
        for repeat in range(8):
            attempt = {**value, "repaired": repeat} if repeat and isinstance(value, dict) else value
            payload = json.dumps(attempt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            spellings = (self.version, 2 if self.version == 3 else 3)  # 3 is the short name, 2 the long one
            for spelling in spellings:
                path = os.path.basename(ps.component_relative_path(name, digest, spelling))
                if self._read_file(path)[0] == ABSENT or self.states.get(path) == INTACT or self._holds(path, digest):
                    return ps.publish_component(self.project_dir, name, attempt, format_version=spelling)
        raise RepairRefused("unavailable")

    def _holds(self, file_name, digest):
        state, payload = self._read_file(file_name)
        return state == INTACT and hashlib.sha256(payload).hexdigest() == digest


def _hold_present_files(project_dir, plan, backup):
    """Keep every component file there is, whether the repaired document names it or not: a
    later edit would drop the named ones, and nothing can enumerate the entries of an index
    that is gone. A repair that fails after this leaves them held, which loses nothing."""
    held = ps.read_held(project_dir)
    ps.write_held(project_dir, {**{name: backup for name in plan.files if ps._COMPONENT_FILE.fullmatch(name)}, **held})


def _set_corrupt_files_aside(project_dir, plan, referenced):
    """Rename what would make a later publication of the same content fail. The repair has
    been saved, so a file that cannot be renamed now is only reported."""
    left = []
    for name, state in sorted(plan.states.items()):
        if state == CORRUPT and name not in referenced and not _set_aside(project_state_path(project_dir, name)):
            left.append(name)
    return left


def _set_aside(path):
    """Never over an earlier one, and never through a replace that deletes its source."""
    base = path[:-len(".json")]
    for delay in (0.025, 0.1, None):
        target = next((name for name in (f"{base}.bad", *(f"{base}.bad{n}" for n in range(2, 100)))
                       if not os.path.lexists(name)), None)
        try:
            if target is None:
                return False
            os.rename(path, target)
            return True
        except OSError:
            if delay is None:
                return False
            time.sleep(delay)
    return False
