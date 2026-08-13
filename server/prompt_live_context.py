"""Shared live Prompt Context assembly for preview, execution, and Relay."""

from . import minimax_h3, prompt_channel_templates, prompt_context
from .reference_prompt_formatter import format_reference_prompt
from .reference_resolution import resolve_effective_references


def resolve_scene_prompt_context(project, scene, template, window_start,
                                 window_end, reference_threshold=0.0) -> dict:
    """Build the setup/ordinal context shared by every live compile entry point."""
    result = {"setup_manifest": {}, "ordinal_manifest": {},
              "unit_picture_ordinals": {}, "unit_source_labels": {},
              "generic_references": {}, "errors": [], "warnings": []}
    template = (template if isinstance(template, dict)
                else prompt_channel_templates.get_channel_template(template))
    template_id = str((template or {}).get("id") or "")
    setup = minimax_h3.active_setup(scene)
    if template_id == "minimax_h3_base":
        if setup is None:
            config = getattr(scene, "prompt_context_profile_config", {}) or {}
            setup = minimax_h3.implicit_base_setup(config.get("task_mode", "T2VA"))
        if setup["mode"] != "base":
            result["errors"].append({"code": "setup_mode_mismatch",
                                     "message": "MiniMax H3 Base requires a Base conditioning setup."})
        else:
            result = minimax_h3.resolve_setup(
                setup=setup, guide_frames=scene.guide_frames,
                scene_duration=scene.duration_frames,
                window_start=window_start, window_end=window_end)
    elif template_id == "minimax_h3_ref":
        if setup is None or setup["mode"] != "reference":
            result["errors"].append({"code": "missing_reference_setup",
                                     "message": "MiniMax H3 Full Reference requires an active Reference setup."})
        else:
            result = minimax_h3.resolve_setup(
                setup=setup, guide_frames=scene.guide_frames,
                reference_items=scene.reference_items,
                lane_recipes=scene.reference_lane_recipes,
                lane_configs=scene.reference_lane_configs,
                lane_count=scene.reference_lane_count,
                scene_duration=scene.duration_frames,
                window_start=window_start, window_end=window_end,
                references=project.references, assets=project.assets,
                semantic_units=project.prompt_semantic_units,
                frame_threshold_pct=reference_threshold)
    else:
        winners = resolve_effective_references(
            reference_items=scene.reference_items,
            lane_count=scene.reference_lane_count,
            scene_duration=scene.duration_frames,
            window_start=window_start, window_end=window_end,
            lane_configs=scene.reference_lane_configs,
            frame_threshold_pct=reference_threshold)
        member_lookup = {
            str(member.member_id): (reference, member)
            for reference in project.references for member in reference.members
        }
        asset_lookup = {str(asset.asset_id): asset for asset in project.assets}
        resolved_records = []
        for lane_index, winner in enumerate(winners):
            if not winner:
                continue
            item = winner.get("item")
            item_value = item.to_dict() if hasattr(item, "to_dict") else dict(item or {})
            records = []
            for member_ref in item_value.get("members") or []:
                if not isinstance(member_ref, dict):
                    continue
                pair = member_lookup.get(str(member_ref.get("member_id") or ""))
                if pair:
                    records.append((pair[0], pair[1]))
            resolved_records.append((lane_index, item_value, records))

        subjects, pictures, audios, speakers = {}, {}, {}, {}
        for _lane_index, _item, records in resolved_records:
            for reference, member in records:
                entity_id = str(reference.reference_id or "")
                member_id = str(member.member_id or "")
                asset = asset_lookup.get(str(member.asset_id or ""))
                if entity_id and entity_id not in subjects:
                    subjects[entity_id] = len(subjects) + 1
                if getattr(asset, "asset_type", "") == "audio":
                    if member_id and member_id not in audios:
                        audios[member_id] = len(audios) + 1
                    if (entity_id and "sonder:voice_identity" in (member.tags or [])
                            and entity_id not in speakers):
                        speakers[entity_id] = len(speakers) + 1
                elif member_id and member_id not in pictures:
                    pictures[member_id] = len(pictures) + 1
        recipes = list(scene.reference_lane_recipes or [])
        generic = {}
        for lane_index, item_value, records in resolved_records:
            wrapper = recipes[lane_index].to_dict() if lane_index < len(recipes) else {}
            recipe = wrapper.get("recipe") if isinstance(wrapper.get("recipe"), dict) else {}
            soft = recipe.get("soft") if isinstance(recipe.get("soft"), dict) else {}
            formatter_records = []
            for reference, member in records:
                formatter_records.append({
                    "name": reference.name,
                    "entity_name": reference.name,
                    "member_name": member.name,
                    "prompt": member.prompt,
                    "member_id": member.member_id,
                    "entity_id": reference.reference_id,
                    "registry_numbers": {
                        "subject_n": subjects.get(reference.reference_id, 0),
                        "picture_n": pictures.get(member.member_id, 0),
                        "audio_n": audios.get(member.member_id, 0),
                        "speaker_n": speakers.get(reference.reference_id, 0),
                    },
                })
            aggregate, fragments = format_reference_prompt(
                item=item_value, members=formatter_records, recipe=recipe)
            item_id = str(item_value.get("reference_item_id") or "")
            if item_id:
                generic[item_id] = {
                    "reference_item_id": item_id, "lane_index": lane_index,
                    "lane_id": str(wrapper.get("lane_id") or ""),
                    "recipe_id": str(wrapper.get("recipe_id") or ""),
                    "prompt": aggregate, "member_prompts": fragments,
                    "members": formatter_records,
                    "compatible_profiles": [str(value) for value in
                                            soft.get("compatible_profiles", ["generic@1"])],
                    "physical_population": str(soft.get("physical_population") or "none"),
                    "exposed_capabilities": [str(value) for value in
                                             soft.get("exposed_capabilities", ["derived_prompt"])],
                    "role_fields": [str(value) for value in soft.get("role_fields", [])],
                }
        result["generic_references"] = generic
    return result


def compile_live_scene_prompt_context(project, scene, *, template,
                                      window_start, window_end, fps,
                                      labels_on=False, delimiter=".",
                                      prompt_threshold=0.0,
                                      reference_threshold=0.0) -> dict:
    """Compile one live scene with the same complete context used by preview."""
    template = (template if isinstance(template, dict)
                else prompt_channel_templates.get_channel_template(template))
    setup_result = resolve_scene_prompt_context(
        project, scene, template, window_start, window_end,
        reference_threshold)
    global_hidden = bool(getattr(scene.global_prompt_track_config, "hidden", False))
    sections_hidden = bool(getattr(scene.prompt_track_config, "hidden", False))
    try:
        compiled = prompt_context.compile_prompt_context(
            global_documents={} if global_hidden else scene.global_channel_docs,
            global_channels={} if global_hidden else scene.global_channels,
            global_attachments=[] if global_hidden else scene.global_attachments,
            sections=[] if sections_hidden else scene.prompt_sections,
            window_start=window_start, window_end=window_end, fps=fps,
            template=template,
            profile=getattr(scene, "prompt_context_profile_id", "") or None,
            custom_profiles=project.prompt_context_profiles,
            context={
                "setup_manifest": setup_result.get("setup_manifest", {}),
                "ordinal_manifest": setup_result.get("ordinal_manifest", {}),
                "unit_picture_ordinals": setup_result.get("unit_picture_ordinals", {}),
                "unit_source_labels": setup_result.get("unit_source_labels", {}),
                "semantic_units": project.prompt_semantic_units,
                "references": [value.to_dict() for value in project.references],
                "generic_references": setup_result.get("generic_references", {}),
            },
            labels_on=labels_on, delimiter=delimiter,
            boundary_threshold_pct=prompt_threshold)
    except prompt_context.ProfileResolutionError as exc:
        compiled = prompt_context.profile_error_result(
            exc, window_start=window_start, window_end=window_end, fps=fps)
    compiled["warnings"] = list(setup_result.get("warnings", [])) + compiled["warnings"]
    compiled["errors"] = list(setup_result.get("errors", [])) + compiled["errors"]
    return compiled
