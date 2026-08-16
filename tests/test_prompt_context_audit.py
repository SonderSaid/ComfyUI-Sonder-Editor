"""Regressions for the adversarial audit of the Prompt Context batch.

Each test pins one confirmed audit finding: a flow the batch made permanently
uncompilable, an authoring refusal the compiler contradicts, or a crash a plain
`ValueError` was laundering into a user-blaming diagnostic. The corrections
themselves are pinned by `test_prompt_context_corrections.py`.
"""

import copy

import pytest

from server import prompt_channel_templates, prompt_context, routes
from server.timeline_state import PromptSection, Scene, TimelineProject


def _copied_minimax_template(template_id="custom-my-minimax"):
    """What the template editor mints when a built-in is copied: a NEW id."""
    source = prompt_channel_templates.get_channel_template("minimax_h3_base")
    return prompt_channel_templates.normalize_channel_template({
        **copy.deepcopy(source),
        "id": template_id,
        "name": "My MiniMax",
        "channels": [dict(channel) for channel in source["channels"]],
        "default_context_profile": "minimax_h3_base@1",
    })


def _scene_with_text(profile_id=""):
    scene = Scene(scene_id="scene", duration_frames=24)
    scene.prompt_context_profile_id = profile_id
    scene.prompt_sections = [PromptSection(start_frame=0, end_frame=24,
                                           channels={"visual": "a quiet room"})]
    return scene


# H1 — a copied MiniMax template must not brick every scene that inherits it.

def test_a_template_declaring_its_own_default_format_stays_compilable():
    template = _copied_minimax_template()
    assert template["default_context_profile"] == "minimax_h3_base@1"
    # The copy minted a new id, so the format's `template_id` cannot match it.
    assert prompt_context.profile_compatible_templates(
        prompt_context.BUILTIN_PROFILES["minimax_h3_base@1"]) == {"minimax_h3_base"}

    resolved = prompt_context.resolve_profile("minimax_h3_base@1", template=template)
    assert resolved["profile_id"] == "minimax_h3_base"

    scene = _scene_with_text()
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    project.metadata["prompt_channel_template"] = (
        prompt_channel_templates.project_template_value(template))
    compiled = routes.compile_live_scene_prompt_context(
        project, scene, template=template, window_start=0, window_end=24, fps=24.0)
    assert [value["code"] for value in compiled["errors"]] == []


def test_a_template_default_does_not_license_an_unrelated_format():
    """Only the format the template actually names is licensed by the default."""
    template = _copied_minimax_template()
    with pytest.raises(prompt_context.ProfileResolutionError,
                       match="profile_template_incompatible"):
        prompt_context.resolve_profile("minimax_h3_ref@1", template=template)


# H2 — a template switch must not strand an explicitly selected format.

def test_switching_templates_releases_a_now_incompatible_scene_format():
    stranded = _scene_with_text("minimax_h3_ref@1")
    kept = _scene_with_text("generic@1")
    project = TimelineProject(project_id="project", scenes=[stranded, kept],
                              fps=24.0)
    released = routes._release_incompatible_scene_profiles(
        project, prompt_channel_templates.get_channel_template("standard"))
    assert released == 1
    # Falling back to the template default is what an untouched scene does; the
    # picker renders a stranded selection disabled, so leaving it is a dead end.
    assert stranded.prompt_context_profile_id == ""
    assert kept.prompt_context_profile_id == "generic@1"
    compiled = routes.compile_live_scene_prompt_context(
        project, stranded,
        template=prompt_channel_templates.get_channel_template("standard"),
        window_start=0, window_end=24, fps=24.0)
    assert [value["code"] for value in compiled["errors"]] == []


def test_switching_templates_keeps_a_scene_on_a_format_that_needs_migrating():
    """A broken DECLARATION is not a template incompatibility.

    `resolve_profile` raises for both, so releasing on the exception type would
    silently detach every scene from a legacy-`routes` format on an unrelated
    template switch — and the explicit "Migrate this format" action would then
    find nothing left to repoint.
    """
    legacy = prompt_context.normalize_profile({
        "profile_id": "legacy", "version": "1", "name": "Legacy",
        "template_id": "standard", "writing_aids": [],
        "capabilities": {"reference": {"routes": {"summary": "visual"}}},
    })
    scene = _scene_with_text("legacy@1")
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0,
                              prompt_context_profiles=[legacy])
    # The format really is unresolvable, so this is not a vacuous fixture.
    with pytest.raises(prompt_context.ProfileResolutionError,
                       match="invalid_profile_declaration"):
        prompt_context.resolve_profile(
            "legacy@1", template=prompt_channel_templates.get_channel_template("sonder"),
            custom_profiles=[legacy])

    released = routes._release_incompatible_scene_profiles(
        project, prompt_channel_templates.get_channel_template("sonder"))
    assert released == 0
    assert scene.prompt_context_profile_id == "legacy@1"
    # So the migration still has something to move.
    assert routes._prompt_context_profile_usages(project, "legacy@1")


def test_switching_templates_releases_a_scene_pointing_at_a_deleted_format():
    scene = _scene_with_text("deleted@1")
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    released = routes._release_incompatible_scene_profiles(
        project, prompt_channel_templates.get_channel_template("standard"))
    assert released == 1
    assert scene.prompt_context_profile_id == ""


def test_switching_templates_keeps_a_format_the_new_template_accepts():
    scene = _scene_with_text("minimax_h3_ref@1")
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    released = routes._release_incompatible_scene_profiles(
        project, prompt_channel_templates.get_channel_template("minimax_h3_ref"))
    assert released == 0
    assert scene.prompt_context_profile_id == "minimax_h3_ref@1"


# H3 — `compatible_templates` is the multi-template escape hatch, not decoration.

def test_compatible_templates_survives_normalization_and_is_honored():
    normalized = prompt_context.normalize_profile({
        "profile_id": "house", "version": "1", "name": "House",
        "template_id": "sonder", "compatible_templates": ["sonder", "standard"],
        "capabilities": {"custom": {"placement": "inline"}},
        "writing_aids": [],
    })
    assert normalized["compatible_templates"] == ["sonder", "standard"]
    assert prompt_context.profile_compatible_templates(normalized) == {
        "sonder", "standard"}
    for template_id in ("sonder", "standard"):
        assert prompt_context.resolve_profile(
            normalized, template=template_id)["profile_id"] == "house"
    with pytest.raises(prompt_context.ProfileResolutionError,
                       match="profile_template_incompatible"):
        prompt_context.resolve_profile(normalized, template="minimax_h3_base")


def test_malformed_compatible_templates_is_refused_not_ignored():
    for raw in ("standard", [""], [{"id": "standard"}]):
        with pytest.raises(ValueError,
                           match="invalid_profile_compatible_templates"):
            prompt_context.normalize_profile({
                "profile_id": "house", "version": "1", "name": "House",
                "compatible_templates": raw,
                "capabilities": {"custom": {"placement": "inline"}},
        "writing_aids": []})


def test_catalog_publishes_custom_multi_template_compatibility():
    project = TimelineProject(project_id="project")
    project.prompt_context_profiles = [prompt_context.normalize_profile({
        "profile_id": "house", "version": "1", "name": "House",
        "template_id": "sonder", "compatible_templates": ["sonder", "standard"],
        "capabilities": {"custom": {"placement": "inline"}},
        "writing_aids": []})]
    catalog = routes._references_payload(project)["prompt_context_catalog"]
    # The surface must not have to re-derive compatibility from `template_id`.
    descriptor = next(value for value in catalog["profiles"]
                      if value["key"] == "house@1")
    assert descriptor["compatible_templates"] == ["sonder", "standard"]


# M5 — a fork must inherit the SOURCE format's binding, not the active template.

def test_a_fork_inheriting_several_templates_resolves_under_each():
    forked = prompt_context.normalize_profile({
        "profile_id": "fork", "version": "1", "name": "Fork",
        "template_id": "sonder", "compatible_templates": ["sonder", "standard"],
        "capabilities": {"custom": {"placement": "inline"}},
        "writing_aids": []})
    scene = _scene_with_text("fork@1")
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    project.prompt_context_profiles = [forked]
    for template_id in ("sonder", "standard"):
        compiled = routes.compile_live_scene_prompt_context(
            project, scene,
            template=prompt_channel_templates.get_channel_template(template_id),
            window_start=0, window_end=24, fps=24.0)
        assert [value["code"] for value in compiled["errors"]] == []


# M1 / M2 / L8 — every authoring site refuses what the compiler refuses.

def _attachments(count):
    return [{"attachment_id": f"a{index}", "kind": "custom",
             "config": {"text": "x"}} for index in range(count)]


def _over_cap():
    return prompt_context.MAX_ATTACHMENTS_PER_SCENE + 5


def test_creating_a_section_refuses_an_over_cap_attachment_list():
    scene = Scene(scene_id="scene", duration_frames=48)
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_create_prompt_section(scene, {
            "start_frame": 0, "end_frame": 24,
            "attachments": _attachments(_over_cap())})
    assert refused.value.code == "attachment_limit"
    assert scene.prompt_sections == []


def test_writing_apply_refuses_an_over_cap_reconciliation():
    scene = Scene(scene_id="scene", duration_frames=48)
    original = [PromptSection(start_frame=0, end_frame=48)]
    scene.prompt_sections = list(original)
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_replace_prompt_sections(scene, [
            {"prompt_id": "one", "start_frame": 0, "end_frame": 24,
             "attachments": _attachments(_over_cap())}])
    assert refused.value.code == "attachment_limit"
    # An atomic reconciliation that refuses must leave the scene untouched.
    assert scene.prompt_sections == original


def test_the_scene_total_is_enforced_where_the_writes_happen():
    """Three individually legal section writes must not compile to a hard block.

    The compiler's cap is per SCENE. When authoring only checked one list at a
    time, a project could be assembled that every preview and every enqueue
    then refused, with nothing to delete in the section being edited.
    """
    per_section = prompt_context.MAX_ATTACHMENTS_PER_SCENE // 2
    scene = Scene(scene_id="scene", duration_frames=96)
    routes._apply_create_prompt_section(scene, {
        "start_frame": 0, "end_frame": 24,
        "attachments": _attachments(per_section)})
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_create_prompt_section(scene, {
            "start_frame": 24, "end_frame": 48,
            "attachments": _attachments(per_section + 1)})
    assert refused.value.code == "attachment_limit"

    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=scene.prompt_sections,
        window_start=0, window_end=96, fps=24, template="standard")
    assert "attachment_limit" not in [
        value if isinstance(value, str) else value.get("code")
        for value in compiled["errors"]]


def test_the_raw_list_is_bounded_before_it_is_normalized():
    """The refusal must not require building every over-cap object first."""
    built = 0
    original = prompt_context.normalize_attachment

    def counting(raw):
        nonlocal built
        built += 1
        return original(raw)

    prompt_context.normalize_attachment = counting
    try:
        with pytest.raises(routes.ProjectMutationRequestError):
            routes._validated_attachments(_attachments(_over_cap()))
    finally:
        prompt_context.normalize_attachment = original
    assert built == 0


# M7 — only a profile-resolution failure may be reported as one.

def test_an_internal_value_error_is_not_reported_as_a_bad_prompt_format():
    scene = _scene_with_text()
    scene.prompt_sections = [PromptSection(start_frame=0, end_frame=24,
                                           channels={"visual": "text"})]
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    original = prompt_context.compile_prompt_context

    def exploding(*args, **kwargs):
        raise ValueError("invalid literal for int() with base 10: 'abc'")

    prompt_context.compile_prompt_context = exploding
    try:
        with pytest.raises(ValueError, match="invalid literal"):
            routes.compile_live_scene_prompt_context(
                project, scene,
                template=prompt_channel_templates.get_channel_template("standard"),
                window_start=0, window_end=24, fps=24.0)
    finally:
        prompt_context.compile_prompt_context = original


def test_a_profile_resolution_failure_is_still_reported_to_the_surface():
    scene = _scene_with_text("minimax_h3_ref@1")
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    compiled = routes.compile_live_scene_prompt_context(
        project, scene,
        template=prompt_channel_templates.get_channel_template("standard"),
        window_start=0, window_end=24, fps=24.0)
    assert [value["code"] for value in compiled["errors"]] == [
        "profile_template_incompatible"]


# L1 — authored data reaches the normalizers outside any request handler.

def test_a_non_numeric_semantic_unit_order_does_not_crash():
    unit = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "unit", "name": "Granny", "order": "abc"})
    assert unit["order"] == 0
    assert prompt_context.normalize_semantic_unit(
        {"semantic_unit_id": "unit", "order": "3"})["order"] == 3
