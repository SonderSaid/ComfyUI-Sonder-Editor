"""Shared live Prompt Context assembly for preview, execution, and Relay."""

from . import minimax_h3, prompt_channel_templates, prompt_context
from .reference_prompt_formatter import build_reference_formatter_context
from .reference_resolution import resolve_effective_references


def resolve_scene_prompt_context(project, scene, template, window_start,
                                 window_end, reference_threshold=0.0,
                                 profile=None) -> dict:
    """Build the setup/ordinal context shared by every live compile entry point."""
    result = {"setup_manifest": {}, "ordinal_manifest": {},
              "unit_picture_ordinals": {}, "unit_source_labels": {},
              "unit_source_members": {},
              "generic_references": {}, "errors": [], "warnings": []}
    template = (template if isinstance(template, dict)
                else prompt_channel_templates.get_channel_template(template))
    profile_request = (prompt_context.profile_key(profile)
                       if isinstance(profile, dict) else profile)
    resolved_profile = prompt_context.resolve_profile(
        profile_request or getattr(scene, "prompt_context_profile_id", "") or None,
        template=template,
        custom_profiles=getattr(project, "prompt_context_profiles", []) or [])
    validator_ids = {
        str(value) for value in resolved_profile.get("validators") or []
        if isinstance(value, str)
    }
    if "minimax_base_format" in validator_ids:
        # Base is text-only. Keep physical reference chips inapplicable and
        # avoid resolving generic Reference lanes for this format.
        return result
    if "minimax_reference_setup" in validator_ids:
        # No registration step: every Reference lane whose recipe declares a
        # physical population participates, so there is nothing a scene can
        # fail to author and no stored setup to consult.
        result = minimax_h3.resolve_setup(
            setup=minimax_h3.implicit_reference_setup(),
            guide_frames=scene.guide_frames,
            reference_items=scene.reference_items,
            lane_recipes=scene.reference_lane_recipes,
            lane_configs=scene.reference_lane_configs,
            lane_count=scene.reference_lane_count,
            scene_duration=scene.duration_frames,
            window_start=window_start, window_end=window_end,
            references=project.references, assets=project.assets,
            semantic_units=project.prompt_semantic_units,
            frame_threshold_pct=reference_threshold,
            profile=resolved_profile)
    else:
        winners = resolve_effective_references(
            reference_items=scene.reference_items,
            lane_count=scene.reference_lane_count,
            scene_duration=scene.duration_frames,
            window_start=window_start, window_end=window_end,
            lane_configs=scene.reference_lane_configs,
            frame_threshold_pct=reference_threshold)
        formatter_context = build_reference_formatter_context(
            winners=winners,
            catalog_records=project.references,
            assets=project.assets,
            recipes=scene.reference_lane_recipes,
            setup_data=result,
        )
        result["generic_references"] = formatter_context["generic_references"]
    return result


def compile_live_scene_prompt_context(project, scene, *, template,
                                      window_start, window_end, fps,
                                      labels_on=False, delimiter=".",
                                      prompt_threshold=0.0,
                                      reference_threshold=0.0,
                                      copy_plan_for=None) -> dict:
    """Compile one live scene with the same complete context used by preview.

    `copy_plan_for` is `{"attachment_id", "capability_id"}` and asks, in the
    same request, what "Copy with handles" would write for that capability.
    It is answered INSIDE the compile, where the enriched context lives. An
    earlier version rebuilt that context by hand out here and silently produced
    empty plans: the keys the assemblers actually read (`profile`,
    `semantic_units_by_id`, `speaker_order`, `references_by_id`) are injected
    during compilation and cannot be enumerated from outside.
    """
    template = (template if isinstance(template, dict)
                else prompt_channel_templates.get_channel_template(template))
    setup_result = {"errors": [], "warnings": []}
    global_hidden = bool(getattr(scene.global_prompt_track_config, "hidden", False))
    sections_hidden = bool(getattr(scene.prompt_track_config, "hidden", False))
    try:
        resolved_profile = prompt_context.resolve_profile(
            getattr(scene, "prompt_context_profile_id", "") or None,
            template=template,
            custom_profiles=project.prompt_context_profiles)
        setup_result = resolve_scene_prompt_context(
            project, scene, template, window_start, window_end,
            reference_threshold, profile=resolved_profile)
        compiled = prompt_context.compile_prompt_context(
            global_documents={} if global_hidden else scene.global_channel_docs,
            global_channels={} if global_hidden else scene.global_channels,
            global_attachments=[] if global_hidden else scene.global_attachments,
            sections=[] if sections_hidden else scene.prompt_sections,
            window_start=window_start, window_end=window_end, fps=fps,
            template=template,
            profile=prompt_context.profile_key(resolved_profile),
            custom_profiles=project.prompt_context_profiles,
            context={
                "setup_manifest": setup_result.get("setup_manifest", {}),
                "ordinal_manifest": setup_result.get("ordinal_manifest", {}),
                "unit_picture_ordinals": setup_result.get("unit_picture_ordinals", {}),
                "unit_source_labels": setup_result.get("unit_source_labels", {}),
                "unit_source_members": setup_result.get("unit_source_members", {}),
                "semantic_units": project.prompt_semantic_units,
                "references": [value.to_dict() for value in project.references],
                "generic_references": setup_result.get("generic_references", {}),
            },
            labels_on=labels_on, delimiter=delimiter,
            boundary_threshold_pct=prompt_threshold,
            copy_plan_for=copy_plan_for)
    except prompt_context.ProfileResolutionError as exc:
        compiled = prompt_context.profile_error_result(
            exc, window_start=window_start, window_end=window_end, fps=fps)
    compiled["warnings"] = list(setup_result.get("warnings", [])) + compiled["warnings"]
    compiled["errors"] = list(setup_result.get("errors", [])) + compiled["errors"]
    return compiled
