"""Server-certified evidence of which reads a committed scene edit can affect.

After every timeline edit the browser used to re-read the Reference bridges and
recompile the prompt previews, even for a lane lock that changes neither (measured
in `roadmap.md`, "Cut the read fan-out"). A **change certificate** lets it skip
that work, and it is only safe because the server issues it:

* It is computed from the ACTUAL before/after state of the committing attempt,
  never from the gesture's name. A clip move can move a linked prompt section.
* It covers only a batch made entirely of `CERTIFIED_OPERATIONS`. Those write
  nothing outside the one scene they address, which
  `tests/test_change_certificates.py` proves at runtime, including the linked
  branches. Any other operation, or any other writer, publishes no certificate,
  and a consumer treats a missing certificate as "refresh".
* It is bound to one exact version transition (`base_modified_at` ->
  `modified_at`) under the commit's compare-and-swap, and it is published only
  after that save succeeds. A refused, failed or no-op attempt publishes nothing.
* It is ephemeral. It travels on the mutation response header and on the
  `project_updated` event, is never persisted, and is never accepted from a
  client.

The two projections are deliberately narrow: they read only the scene fields the
consumer reads. `SCENE_FIELD_DEPENDENCIES` classifies every serialized Scene
field, so a new field cannot silently fall outside both projections -- the test
suite fails until it is classified.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json

SCHEMA_VERSION = 1
HEADER = "X-Sonder-Change-Certificate"
EVENT_KEY = "change"

# Operations whose handlers write only the scene they address. The set is the
# plan's initial allowlist and is independent of `_SCENE_ONLY_MUTATIONS` (the
# no-op-save allowlist): that one answers "did the saved document change", this
# one answers "can this change reach anything but its own scene".
# Expiry: an allow-list; it needs no expiry, but every addition owes the runtime
# proof in `tests/test_change_certificates.py`.
CERTIFIED_OPERATIONS = frozenset({
    "update_scene_fields",
    "update_lane_config",
    "update_lane_configs",
    "update_clip",
    "update_audio_track",
    "split_clip",
    "split_audio_track",
    "create_prompt_section",
    "update_prompt_section",
    "delete_prompt_section",
    "split_prompt_section",
    "replace_prompt_sections",
    "swap_prompt_sections",
})

# Every serialized Scene field, and which projection reads it. `"prompt"` means
# the live Prompt Context compile (`compile_live_scene_prompt_context` and the
# candidate overlay in `_compile_prompt_context_candidate_sync`); `"bridge"`
# means `GET .../bridge-references`. An empty tuple is a traced claim that
# neither reads it.
SCENE_FIELD_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "scene_id": (),                     # identity; the certificate names it
    "name": ("bridge",),                # `scene_name` in the bridge payload
    "order": (),
    "duration_frames": ("prompt", "bridge"),
    "fps": ("prompt",),                 # effective FPS for timestamps and windows
    "width": (),
    "height": (),
    "prompt": (),                       # legacy flat mirror; compile reads documents
    "global_channels": ("prompt",),
    "global_channel_docs": ("prompt",),
    "global_attachments": ("prompt",),
    "prompt_sections": ("prompt",),
    "prompt_context_profile_id": ("prompt",),
    # Not read by the live compile (only the queue freeze is), but the candidate
    # overlay accepts it; kept conservatively, since a false "changed" is safe.
    "prompt_context_profile_config": ("prompt",),
    "prompt_track_config": ("prompt",),              # `hidden` only
    "global_prompt_track_config": ("prompt",),       # `hidden` only
    "guide_track_config": ("prompt", "bridge"),      # `hidden` only, conservatively
    "guide_frames": ("prompt", "bridge"),
    "reference_items": ("prompt", "bridge"),
    "reference_lane_count": ("prompt", "bridge"),
    "reference_lane_configs": ("prompt", "bridge"),  # prompt: `hidden`; bridge: +`name`
    "reference_lane_recipes": ("prompt", "bridge"),
    "clips": (),
    "audio_tracks": (),
    "linked_item_groups": (),           # a linked edit's effect lands in the fields above
    "asset_ids": (),
    "video_lane_count": (),
    "motion_driver_lane_count": (),
    "audio_lane_count": (),
    "video_lane_configs": (),
    "motion_driver_lane_configs": (),
    "audio_lane_configs": (),
    "generation_params": (),
    "batch_config": (),
    "is_bridge": (),
    "saved_selections": (),
}


def _dicts(values) -> list:
    return [value.to_dict() if hasattr(value, "to_dict") else value
            for value in (values or [])]


def _hidden(config) -> bool:
    if isinstance(config, dict):
        return bool(config.get("hidden", False))
    return bool(getattr(config, "hidden", False))


def prompt_dependency_projection(scene) -> dict:
    """Exactly the scene state the live Prompt Context compile reads.

    Detached by `copy.deepcopy`: a projection must never alias the live scene it
    describes, or an in-place edit moves the "before" side too and a real change
    reads as "unchanged" -- the one wrong answer a certificate must never give.
    """
    return copy.deepcopy({
        "duration_frames": getattr(scene, "duration_frames", 0),
        "fps": getattr(scene, "fps", 0.0),
        "global_channels": getattr(scene, "global_channels", None),
        "global_channel_docs": getattr(scene, "global_channel_docs", None),
        "global_attachments": getattr(scene, "global_attachments", None),
        "prompt_sections": _dicts(getattr(scene, "prompt_sections", [])),
        "prompt_context_profile_id": getattr(scene, "prompt_context_profile_id", ""),
        "prompt_context_profile_config": getattr(scene, "prompt_context_profile_config", None),
        "prompt_track_hidden": _hidden(getattr(scene, "prompt_track_config", None)),
        "global_prompt_track_hidden": _hidden(getattr(scene, "global_prompt_track_config", None)),
        "guide_track_hidden": _hidden(getattr(scene, "guide_track_config", None)),
        "guide_frames": _dicts(getattr(scene, "guide_frames", [])),
        "reference_items": _dicts(getattr(scene, "reference_items", [])),
        "reference_lane_count": getattr(scene, "reference_lane_count", 0),
        # Staging reads `hidden` alone (`resolve_reference_staging`,
        # `resolve_effective_references`); lock, colour and name are presentation.
        "reference_lane_hidden": [_hidden(config) for config in
                                  getattr(scene, "reference_lane_configs", []) or []],
        "reference_lane_recipes": _dicts(getattr(scene, "reference_lane_recipes", [])),
    })


def bridge_dependency_projection(scene) -> dict:
    """Exactly the scene state `GET .../bridge-references` reads (detached)."""
    configs = getattr(scene, "reference_lane_configs", []) or []
    return copy.deepcopy({
        "name": getattr(scene, "name", ""),
        "duration_frames": getattr(scene, "duration_frames", 0),
        "guide_track_hidden": _hidden(getattr(scene, "guide_track_config", None)),
        "guide_frames": _dicts(getattr(scene, "guide_frames", [])),
        "reference_items": _dicts(getattr(scene, "reference_items", [])),
        "reference_lane_count": getattr(scene, "reference_lane_count", 0),
        # The row label and the dead-lane verdict; lock and colour never reach it.
        "reference_lanes": [
            {"name": str(getattr(config, "name", "") if not isinstance(config, dict)
                         else config.get("name", "")) or "",
             "hidden": _hidden(config)}
            for config in configs],
        "reference_lane_recipes": _dicts(getattr(scene, "reference_lane_recipes", [])),
    })


def batch_is_certifiable(operations) -> bool:
    return (isinstance(operations, list) and bool(operations)
            and all(isinstance(operation, dict)
                    and str(operation.get("type") or "") in CERTIFIED_OPERATIONS
                    for operation in operations))


# `Scene._ensure_stable_link_item_ids` mints a RANDOM id on every load for a
# guide, prompt section or Reference item whose stored id is missing or
# duplicated. Two loads of one version then compile differently, so a witness
# comparing one draw against itself would call a read "unchanged" while the
# client's earlier result carries ids the document no longer has. Such a scene
# is not certified; its first save persists the ids and ends the condition.
_LOAD_MINTED_ID_FIELDS = (
    ("guide_frames", "guide_id"),
    ("prompt_sections", "prompt_id"),
    ("reference_items", "reference_item_id"),
)


def scene_ids_minted_at_load(scene) -> bool:
    """Whether loading this scene invented an id its stored data did not hold.

    A scene built in memory has no raw data and minted nothing.
    """
    raw = getattr(scene, "_raw_data", None)
    if not isinstance(raw, dict) or not raw:
        return False
    for field, key in _LOAD_MINTED_ID_FIELDS:
        stored = [str(value.get(key) or "") for value in (raw.get(field) or [])
                  if isinstance(value, dict)]
        current = [str(getattr(value, key, "") or "")
                   for value in (getattr(scene, field, None) or [])]
        if sorted(stored) != sorted(current) or len(set(stored)) != len(stored):
            return True
    return False


class SceneDependencyWitness:
    """Before/after projections of one scene across one commit attempt."""

    def __init__(self, scene):
        self.scene_id = str(getattr(scene, "scene_id", "") or "")
        self._prompt = prompt_dependency_projection(scene)
        self._bridge = bridge_dependency_projection(scene)

    def flags(self, scene) -> dict:
        return {
            "scene_id": self.scene_id,
            "prompt": prompt_dependency_projection(scene) != self._prompt,
            "bridge": bridge_dependency_projection(scene) != self._bridge,
        }


def build_certificate(*, project_id: str, scene_id: str, base_modified_at: str,
                      modified_at: str, prompt: bool, bridge: bool) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "project_id": str(project_id or ""),
        "scene_id": str(scene_id or ""),
        "base_modified_at": str(base_modified_at or ""),
        "modified_at": str(modified_at or ""),
        "prompt": bool(prompt),
        "bridge": bool(bridge),
    }


def encode_header(certificate: dict) -> str:
    """Compact ASCII JSON, safe for an HTTP header value."""
    return json.dumps(certificate, separators=(",", ":"), ensure_ascii=True,
                      sort_keys=True)


# The certificate a save in THIS thread is about to publish. Set only around one
# guarded `save_project` call, so the saved hook can attach it to the
# `project_updated` event of exactly that save and of no other.
_pending: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "sonder_pending_change_certificate", default=None)


@contextlib.contextmanager
def pending_certificate(*, project_dir: str, project_id: str, scene_id: str,
                        base_modified_at: str, prompt: bool, bridge: bool):
    token = _pending.set({
        "project_dir": str(project_dir or ""),
        "project_id": str(project_id or ""),
        "scene_id": str(scene_id or ""),
        "base_modified_at": str(base_modified_at or ""),
        "prompt": bool(prompt),
        "bridge": bool(bridge),
    })
    try:
        yield
    finally:
        _pending.reset(token)


def certificate_for_saved(project) -> dict | None:
    """The certificate for the save that just published `project`, or None.

    Called from the saved hook. The pending record must name this project's
    directory and base version, so a save the record was not armed for -- a
    nested or unrelated save in the same context -- never borrows it.
    """
    pending = _pending.get()
    if not pending:
        return None
    if str(getattr(project, "project_dir", "") or "") != pending["project_dir"]:
        return None
    modified_at = str(getattr(project, "modified_at", "") or "")
    if not modified_at or modified_at == pending["base_modified_at"]:
        return None
    certificate = build_certificate(
        project_id=pending["project_id"], scene_id=pending["scene_id"],
        base_modified_at=pending["base_modified_at"], modified_at=modified_at,
        prompt=pending["prompt"], bridge=pending["bridge"])
    # The response header reuses THIS object (`published_certificate`), so the
    # header and the event can never disagree -- including when this refuses.
    pending["certificate"] = certificate
    return certificate


def published_certificate() -> dict | None:
    """The certificate the armed save's hook actually published, or None.

    Read inside `pending_certificate`, after the save. None when the hook
    refused, raised or never ran, so a response never certifies a transition
    its `project_updated` events did not.
    """
    pending = _pending.get()
    certificate = pending.get("certificate") if pending else None
    return dict(certificate) if isinstance(certificate, dict) else None
