"""Regressions for the Prompt Context correctness recovery batch.

Each test here pins one previously-confirmed semantic defect: silent output,
a crash reachable from authored data, or an authority disagreement between the
compiler and a surface that renders the same state.
"""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import minimax_h3, prompt_channel_templates, prompt_context, routes
from server.timeline_state import (
    Asset,
    GenerationJob,
    PromptSection,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    Scene,
    TimelineProject,
)


ROOT = Path(__file__).resolve().parents[1]


def _section(start, end, text="", *, prompt_id=None, attachments=None,
             channel_docs=None):
    section = PromptSection(
        start_frame=start, end_frame=end,
        channels={"visual": text} if text and not channel_docs else {},
        channel_docs=channel_docs, attachments=attachments)
    if prompt_id:
        section.prompt_id = prompt_id
    return section


def _h3_base_project(task_mode="T2VA"):
    scene = Scene(scene_id="scene", duration_frames=48)
    scene.prompt_context_profile_config = {"task_mode": task_mode}
    scene.prompt_sections = [PromptSection(
        start_frame=0, end_frame=48,
        channel_docs={"integrated_multimodal_description": {"nodes": [
            {"type": "text", "node_id": "body", "text": "A quiet room."}]}})]
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    project.metadata["prompt_channel_template"] = "minimax_h3_base"
    return project, scene


def _compile_base(project, scene):
    return routes.compile_live_scene_prompt_context(
        project, scene,
        template=prompt_channel_templates.get_channel_template("minimax_h3_base"),
        window_start=0, window_end=48, fps=24.0)


# 1 / 18 — implicit Base setup identity and strict mode validation.

def test_implicit_h3_base_setup_is_stable_and_hashes_identically():
    assert (minimax_h3.implicit_base_setup()
            == minimax_h3.implicit_base_setup("T2VA"))
    assert (minimax_h3.implicit_base_setup()["setup_id"]
            == minimax_h3.IMPLICIT_BASE_SETUP_ID)

    project, scene = _h3_base_project()
    first = _compile_base(project, scene)
    second = _compile_base(project, scene)
    assert first["errors"] == []
    assert (first["setup_manifest"]["setup"]["setup_id"]
            == minimax_h3.IMPLICIT_BASE_SETUP_ID)
    # A fresh UUID per compile changed the manifest and therefore the frozen
    # content hash on every preview.
    assert first["content_hash"] == second["content_hash"]


def test_unknown_h3_mode_and_task_mode_are_reported_not_coerced():
    setup = minimax_h3.normalize_setup({"mode": "wormhole", "task_mode": "X2Y"})
    assert setup["mode"] == "wormhole"
    assert setup["task_mode"] == "X2Y"
    codes = {value["code"] for value in minimax_h3.setup_validation_errors(setup)}
    assert codes == {"invalid_h3_setup_mode", "invalid_h3_task_mode"}
    # Absent values still default.
    blank = minimax_h3.normalize_setup({})
    assert (blank["mode"], blank["task_mode"]) == ("base", "T2VA")
    assert minimax_h3.setup_validation_errors(blank) == []

    resolved = minimax_h3.resolve_setup(setup=setup, scene_duration=10,
                                        window_start=0, window_end=10)
    assert {value["code"] for value in resolved["errors"]} == codes

    scene = Scene(scene_id="scene")
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_scene_fields(None, scene, {
            "minimax_h3_conditioning_setups": [{"setup_id": "s", "mode": "wormhole"}],
            "active_minimax_h3_setup_id": "s",
        })
    assert refused.value.code == "invalid_h3_setup_mode"


# 2 — malformed custom profiles must not crash compilation.

@pytest.mark.parametrize("raw,message", [
    ({"capabilities": {"custom": "not-an-object"}}, "invalid_profile_capability"),
    ({"capabilities": {}, "separators": {"attachment": 7}},
     "invalid_profile_separator"),
    ({"capabilities": {}, "separators": {"paragraph": " "}},
     "unknown_profile_separator"),
    ({"capabilities": {}, "separators": []}, "invalid_profile_separators"),
    ({"capabilities": {"not_a_kind": {}}}, "unknown_profile_capability_kind"),
    ({"capabilities": {"custom": {"placement": "somewhere"}}},
     "invalid_capability_placement"),
    ({"capabilities": {"custom": {"channel_key": 5}}},
     "invalid_capability_channel_key"),
    ({"capabilities": {"reference": {"routes": {"definitions": 5}}}},
     "invalid_capability_routes"),
    ({"capabilities": {"custom": {"formatter": ["x"]}}},
     "invalid_capability_formatter"),
    ({"capabilities": {"custom": {"fields": []}}}, "invalid_capability_fields"),
])
def test_malformed_profile_fields_are_refused_during_normalization(raw, message):
    with pytest.raises(ValueError, match=message):
        prompt_context.normalize_profile({
            "profile_id": "broken", "version": "1", "template_id": "standard",
            "writing_aids": [], **raw})


def test_malformed_profile_returns_a_controlled_mutation_error():
    project = TimelineProject(project_id="project")
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._normalize_prompt_context_profile_update(project, [{
            "profile_id": "broken", "version": "1", "template_id": "standard",
            "writing_aids": [], "capabilities": {"custom": "not-an-object"}}])
    assert refused.value.code == "invalid_prompt_context_profile"
    assert refused.value.status == 400


# 3 — prompt format / channel template compatibility.

def test_minimax_profile_cannot_be_selected_under_a_standard_template():
    assert prompt_context.profile_compatible_templates(
        prompt_context.BUILTIN_PROFILES["generic@1"]) == {
            prompt_context.UNIVERSAL_PROFILE_TEMPLATE}
    assert prompt_context.profile_compatible_templates(
        prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]) == {"minimax_h3_ref"}

    with pytest.raises(ValueError, match="profile_template_incompatible"):
        prompt_context.resolve_profile("minimax_h3_ref@1", template="standard")
    # The matching template still resolves, and Generic stays universal.
    assert prompt_context.resolve_profile(
        "minimax_h3_ref@1", template="minimax_h3_ref")["profile_id"] == "minimax_h3_ref"
    assert prompt_context.resolve_profile(
        "generic@1", template="minimax_h3_ref")["profile_id"] == "generic"


def test_incompatible_profile_compiles_to_a_blocking_error_not_a_crash():
    scene = Scene(scene_id="scene", duration_frames=24)
    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    scene.prompt_sections = [_section(0, 24, "text")]
    project = TimelineProject(project_id="project", scenes=[scene], fps=24.0)
    compiled = routes.compile_live_scene_prompt_context(
        project, scene,
        template=prompt_channel_templates.get_channel_template("standard"),
        window_start=0, window_end=24, fps=24.0)
    assert compiled["prompt"] == ""
    assert [value["code"] for value in compiled["errors"]] == [
        "profile_template_incompatible"]


def test_catalog_publishes_profile_template_compatibility():
    payload = routes._references_payload(TimelineProject(project_id="project"))
    catalog = payload["prompt_context_catalog"]
    assert catalog["universal_template"] == prompt_context.UNIVERSAL_PROFILE_TEMPLATE
    descriptors = {value["key"]: value for value in catalog["profiles"]}
    assert descriptors["minimax_h3_base@1"]["compatible_templates"] == [
        "minimax_h3_base"]
    assert descriptors["generic@1"]["compatible_templates"] == [
        prompt_context.UNIVERSAL_PROFILE_TEMPLATE]
    task_types = descriptors["minimax_h3_ref@1"]["resolved"]["capabilities"][
        "reference"]["derived"]["summary"]["fields"]["task_types"]
    assert [row["value"] for row in task_types["values"]] == list(
        prompt_context.MINIMAX_TASK_TYPES)


# 5 — disabled capabilities never contribute to validation.

def test_disabled_reference_capability_neither_renders_nor_blocks():
    attachment = prompt_context.normalize_attachment({
        "kind": "reference",
        "source": {"reference_item_id": "item"},
        "capabilities": [
            {"capability_id": "derived_prompt", "kind": "derived_prompt",
             "placement": "section_prefix", "channel_key": "visual"},
            {"capability_id": "summary", "kind": "summary", "enabled": False,
             "placement": "section_prefix", "channel_key": "visual"},
        ],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "scene",
                                               attachments=[attachment])],
        window_start=0, window_end=10, fps=24, template="standard",
        context={"generic_references": {"item": {
            "prompt": "a red door", "compatible_profiles": ["generic@1"],
            "exposed_capabilities": ["derived_prompt"]}}})
    assert [value["code"] for value in compiled["errors"]] == []
    assert "a red door" in compiled["prompt"]


# 6 — Audio Relationship key parity between the chip editor and the compiler.

def test_audio_relationship_reads_the_key_the_chip_editor_writes():
    context = {"ordinal_manifest": {"subjects": {"unit": 1}},
               "semantic_units_by_id": {},
               "profile": prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]}
    authored = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["unit"]},
        "config": {"audio_relationship": "Voice belongs to <Subject 1>."}})
    capability = prompt_context.normalize_capability({
        "capability_id": "audio_relationship", "kind": "audio_relationship"})
    assert prompt_context._render_reference_capability(
        authored, capability, context) == "Voice belongs to <Subject 1>."
    legacy = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["unit"]},
        "config": {"text": "Legacy text."}})
    assert prompt_context._render_reference_capability(
        legacy, capability, context) == ""


# 7 — overlapping Subject selections dedupe per unit.

def _reference_context():
    return {
        "ordinal_manifest": {"subjects": {"a": 1, "b": 2}},
        "unit_source_labels": {"a": ["<Picture 1>"], "b": ["<Picture 2>"]},
        "semantic_units": [
            {"semantic_unit_id": "a", "name": "A", "definition": "a woman",
             "sources": [{"entity_id": "reference", "member_id": "am"}]},
            {"semantic_unit_id": "b", "name": "B", "definition": "a man",
             "sources": [{"entity_id": "reference", "member_id": "bm"}]},
        ],
        "setup_manifest": {"setup": {"mode": "reference"},
                           "pictures": [{"member_id": "am", "picture_ordinal": 1},
                                        {"member_id": "bm", "picture_ordinal": 2}]},
    }


_REFERENCE_IDENTITY_PROFILE = {
    "profile_id": "test_identity", "version": "1", "name": "Test identity",
    "template_id": "standard", "capabilities": {"reference": {"derived": {
        "definitions": {"order": 1, "channel_key": "visual",
                        "placement": "section_prefix", "label": "Definition",
                        "fields": {}},
    }}}, "writing_aids": [],
    "identity_kinds": [prompt_context.MINIMAX_SUBJECT_KIND],
}


def test_overlapping_subject_definitions_emit_once_per_unit():
    wide = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["a", "b"]},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "placement": "section_prefix", "channel_key": "visual"}],
    })
    narrow = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["a"]},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "placement": "section_prefix", "channel_key": "visual"}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_section(0, 10, "scene", attachments=[wide, narrow])],
        window_start=0, window_end=10, fps=24, template="standard",
        profile=_REFERENCE_IDENTITY_PROFILE,
        context=_reference_context())
    assert compiled["prompt"].count("<Subject 1> is a woman") == 1
    assert compiled["prompt"].count("<Subject 2> is a man") == 1


def test_contradictory_overlapping_definitions_are_reported():
    wide = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["a", "b"]},
        "config": {"definition": "a woman in red"},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "placement": "section_prefix", "channel_key": "visual"}],
    })
    narrow = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["a"]},
        "config": {"definition": "a woman in blue"},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "placement": "section_prefix", "channel_key": "visual"}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_section(0, 10, "scene", attachments=[wide, narrow])],
        window_start=0, window_end=10, fps=24, template="standard",
        profile=_REFERENCE_IDENTITY_PROFILE,
        context=_reference_context())
    assert any(value["code"] == "conflicting_emission"
               for value in compiled["errors"])


# 8 — provider registry.

def test_unknown_attachment_provider_blocks_only_while_enabled():
    unknown = prompt_context.normalize_attachment({
        "kind": "guide", "provider_id": "some_future_model",
        "provider_version": "3", "config": {"text": "guide"}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "scene",
                                               attachments=[unknown])],
        window_start=0, window_end=10, fps=24, template="standard")
    assert any(value["code"] == "unsupported_attachment_provider"
               for value in compiled["errors"])

    disabled = prompt_context.normalize_attachment({**unknown, "enabled": False})
    quiet = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "scene",
                                               attachments=[disabled])],
        window_start=0, window_end=10, fps=24, template="standard")
    assert not any(value["code"] == "unsupported_attachment_provider"
                   for value in quiet["errors"])

    # Legacy records normalize to the supported generic provider.
    legacy = prompt_context.normalize_attachment({"kind": "guide"})
    assert (legacy["provider_id"], legacy["provider_version"]) == ("generic", "1")


# 9 — custom capability field/enum validation.

_ENUM_PROFILE = {
    "profile_id": "enums", "version": "1", "template_id": "standard",
    "writing_aids": [],
    "capabilities": {"custom": {
        "channel_key": "visual", "placement": "section_prefix",
        "formatter": "The camera {motion}.",
        "fields": {"motion": {"type": "enum",
                              "values": ["pushes in", "pulls out"]}}}},
}


def _compile_custom(config):
    attachment = prompt_context.normalize_attachment(
        {"kind": "custom", "config": config})
    return prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "", attachments=[attachment])],
        window_start=0, window_end=10, fps=24, template="standard",
        profile=copy.deepcopy(_ENUM_PROFILE))


def test_custom_capability_enum_values_are_bounded():
    valid = _compile_custom({"motion": "pushes in"})
    assert valid["errors"] == []
    assert valid["prompt"] == "The camera pushes in."

    injected = _compile_custom({"motion": "<script>alert(1)</script>"})
    assert any(value["code"] == "invalid_custom_capability_field"
               for value in injected["errors"])
    assert "<script>" not in injected["prompt"]

    missing = _compile_custom({})
    assert any(value["code"] == "missing_custom_capability_field"
               for value in missing["errors"])

    undeclared = _compile_custom({"fields": {"motion": "pushes in",
                                             "rogue": "anything"}})
    assert any(value["code"] == "unknown_custom_capability_field"
               for value in undeclared["errors"])


# 11 — unbound Vocal Events.

def test_unbound_vocal_event_blocks_instead_of_inventing_a_speaker():
    unbound = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {},
        "config": {"event_type": "dialogue", "language": "English",
                   "text": "Hello."}})
    section = PromptSection(
        start_frame=0, end_frame=10, attachments=[unbound],
        channel_docs={"visual": {"nodes": [
            {"type": "text", "node_id": "t", "text": "scene "},
            {"type": "attachment", "node_id": "a",
             "attachment_id": unbound["attachment_id"]}]}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[section], window_start=0, window_end=10,
        fps=24, template="standard")
    assert any(value["code"] == "missing_vocal_binding"
               for value in compiled["errors"])

    bound = prompt_context.normalize_attachment(
        {**unbound, "source": {"voice_id": "narrator"}})
    section.attachments = [bound]
    section.channel_docs["visual"]["nodes"][1]["attachment_id"] = bound["attachment_id"]
    accepted = prompt_context.compile_prompt_context(
        global_channels={}, sections=[section], window_start=0, window_end=10,
        fps=24, template="standard")
    assert not any(value["code"] == "missing_vocal_binding"
                   for value in accepted["errors"])


# 13 — MiniMax singing writing aid.

def test_minimax_singing_aid_uses_the_bounded_language_envelope():
    aids = {value["id"]: value for value in
            prompt_context.BUILTIN_PROFILES["minimax_h3_base@1"]["writing_aids"]}
    assert aids["singing"]["text"] == "Singing: <d>[{language}] {text}</d>"
    assert (set(aids["singing"]["fields"]["language"]["values"])
            == prompt_context.DIALOGUE_LANGUAGES)
    generic = {value["id"]: value for value in
               prompt_context.BUILTIN_PROFILES["generic@1"]["writing_aids"]}
    assert generic["singing"]["text"] == "Singing: {text}"


def test_frontend_writing_aids_have_no_canonical_fallback_copy():
    chips = (ROOT / "web" / "js" / "prompt_context_chips.js").read_text(
        encoding="utf-8")
    assert "DEFAULT_WRITING_AIDS" not in chips
    assert "Array.isArray(writingAids) ? writingAids : []" in chips


# 14 — silent video in an H3 audio slot.

def _audio_slot_setup(has_audio, checked=True):
    video_asset = Asset(asset_id="vi", asset_type="video", duration_sec=5,
                        has_audio=has_audio, has_audio_checked=checked)
    members = [ReferenceMember(member_id="am", asset_id="vi")]
    references = [ReferenceEntity(reference_id="entity", name="Room",
                                  members=members)]
    recipes = [ReferenceLaneRecipe(lane_id="audio", media_kind="audio", recipe={
        "soft": {"compatible_profiles": ["minimax_h3_ref@1"],
                 "physical_population": "standalone_audios"}})]
    items = [ReferenceItem(reference_item_id="a", lane_index=0, start_frame=0,
                           end_frame=100,
                           members=[{"entity_id": "entity", "member_id": "am"}])]
    return minimax_h3.resolve_setup(
        setup={"mode": "reference", "audio_lane_ids": ["audio"]},
        reference_items=items, lane_recipes=recipes, lane_count=1,
        scene_duration=100, window_start=0, window_end=100,
        references=references, assets=[video_asset])


def test_silent_video_is_refused_in_a_standalone_audio_slot():
    refused = _audio_slot_setup(False)
    assert any(value["code"] == "silent_video_audio_slot"
               for value in refused["errors"])
    # The offending slot stays in the manifest so the surface can point at it;
    # the blocking error is what stops the job, matching `invalid_setup_media`.
    assert [value["asset_id"] for value in
            refused["setup_manifest"]["standalone_audios"]] == ["vi"]
    assert _audio_slot_setup(True)["errors"] == []


def test_unprobed_video_audio_warns_instead_of_blocking():
    # `has_audio` defaults to False, so an asset registered before the probe
    # existed is indistinguishable from a silent one. Refusing there would block
    # a legitimate job over missing metadata.
    unprobed = _audio_slot_setup(False, checked=False)
    assert unprobed["errors"] == []
    assert any(value["code"] == "unverified_video_audio_slot"
               for value in unprobed["warnings"])


# 15 — Reference delete identity covers non-default intents.

def test_delete_reference_requires_intents_once_they_leave_the_defaults():
    reference = ReferenceEntity(
        reference_id="entity", name="Granny", kind="character",
        reference_class="subject", description="",
        visual_intent="transfer_attributes")
    project = TimelineProject(project_id="project", references=[reference])
    stale = {"name": "Granny", "kind": "character", "reference_class": "subject",
             "description": "", "member_ids": []}
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_delete_reference(project, {
            "reference_id": "entity", "expected": stale})
    assert refused.value.code == "missing_expected_identity"

    routes._apply_delete_reference(project, {
        "reference_id": "entity",
        "expected": {**stale, "visual_intent": "transfer_attributes"}})
    assert project.references == []


def test_delete_reference_stays_compatible_while_intents_are_default():
    reference = ReferenceEntity(reference_id="entity", name="Granny",
                                kind="character", reference_class="subject")
    project = TimelineProject(project_id="project", references=[reference])
    routes._apply_delete_reference(project, {
        "reference_id": "entity",
        "expected": {"name": "Granny", "kind": "character",
                     "reference_class": "subject", "description": "",
                     "member_ids": []}})
    assert project.references == []


# 16 — prompt history digest separates profile config and setup.

def _history_job(task_mode, setup_id):
    return GenerationJob(
        scene_id="scene", selection_start=0, selection_end=10,
        scene_prompt="same authored text", params={
            "prompt_context_format": "prompt_context_v1",
            "prompt_context_profile_config": {"task_mode": task_mode}},
        prompt_sections=[],
        compiled_prompt_context={"profile": prompt_context.BUILTIN_PROFILES[
            "minimax_h3_base@1"]},
        minimax_h3_setup_snapshot={"setup": {"setup_id": setup_id,
                                             "mode": "base",
                                             "task_mode": task_mode}})


def test_prompt_history_separates_task_mode_and_setup_for_identical_text():
    project = TimelineProject(project_id="project")
    routes._record_prompt_history(project, [
        _history_job("T2VA", minimax_h3.IMPLICIT_BASE_SETUP_ID),
        _history_job("I2VA", minimax_h3.IMPLICIT_BASE_SETUP_ID),
        _history_job("I2VA", "authored-setup"),
    ])
    history = project.metadata["prompt_history"]
    assert len({entry["hash"] for entry in history}) == 3

    # A byte-identical re-enqueue still collapses onto its existing entry.
    routes._record_prompt_history(project, [
        _history_job("T2VA", minimax_h3.IMPLICIT_BASE_SETUP_ID)])
    assert len(project.metadata["prompt_history"]) == 3


# 19 — over-cap authored data is refused rather than sliced.

def test_attachment_and_capability_overflow_is_preserved_then_refused():
    many = [{"kind": "guide", "attachment_id": f"a{index}"}
            for index in range(prompt_context.MAX_ATTACHMENTS_PER_SCENE + 1)]
    normalized = prompt_context.normalize_attachments(many)
    assert len(normalized) == prompt_context.MAX_ATTACHMENTS_PER_SCENE + 1
    assert [value["code"] for value in
            prompt_context.attachment_limit_errors(normalized)] == ["attachment_limit"]

    wide = prompt_context.normalize_attachment({
        "kind": "reference",
        "capabilities": [{"capability_id": f"c{index}", "kind": "mentions"}
                         for index in range(prompt_context.MAX_CAPABILITIES + 1)]})
    assert len(wide["capabilities"]) == prompt_context.MAX_CAPABILITIES + 1
    assert [value["code"] for value in
            prompt_context.attachment_limit_errors([wide])] == ["capability_limit"]

    scene = Scene(scene_id="scene")
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_scene_fields(None, scene, {"global_attachments": many})
    assert refused.value.code == "attachment_limit"
    assert refused.value.status == 400


# 4 / 10 / 12 / 17 — frontend surfaces that must agree with the compiler.

def _run_chip_dom_script(body):
    """Drive prompt_context_chips.js against a minimal DOM under node.

    Behaviour, not source text: a grep passes even when the fix is reverted as
    long as the comment survives.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Context JS tests")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    script = f"""
        {_MINIMAL_DOM}
        const mod = await import({json.dumps(module_url)});
        {body}
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


# The module needs only enough DOM to build and query elements; it registers a
# keyboard consumer at import, so `window.addEventListener` must exist.
_MINIMAL_DOM = """
class N {
  constructor(tag){ this.tagName=String(tag).toUpperCase(); this.children=[];
    this.childNodes=this.children; this.style={cssText:"",setProperty(){}};
    this.dataset={}; this.attributes={}; this.options=[]; this.value="";
    this._text=""; this.title=""; this.disabled=false; this.multiple=false;
    this._handlers={}; }
  // textContent is MODELLED, not stored: setting it replaces the children,
  // reading it concatenates them. A stored string would make a test that
  // strips a nested element pass without the strip ever running.
  get textContent(){ return this._text
    + this.children.map((c) => c.textContent ?? "").join(""); }
  set textContent(v){ this.children.length = 0; this._text = String(v ?? ""); }
  appendChild(c){ this.children.push(c); c.parentElement=this;
    if (c.tagName === "OPTION") this.options.push(c); return c; }
  insertBefore(c, ref){
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) return this.appendChild(c);
    this.children.splice(at, 0, c); c.parentElement=this; return c; }
  append(...cs){ for (const c of cs) if (c && c.tagName) this.appendChild(c); }
  addEventListener(t,h){ (this._handlers[t] ||= []).push(h); }
  removeEventListener(){}
  setAttribute(k,v){ this.attributes[k]=String(v); }
  getAttribute(k){ return this.attributes[k] ?? null; }
  cloneNode(deep){
    const copy = new N(this.tagName);
    copy._text = this._text;
    copy.dataset = { ...this.dataset };
    copy.attributes = { ...this.attributes };
    if (deep) for (const c of this.children) copy.appendChild(c.cloneNode(true));
    return copy; }
  get nextSibling(){
    const kids = this.parentElement?.children || [];
    return kids[kids.indexOf(this) + 1] || null; }
  get previousElementSibling(){
    const kids = this.parentElement?.children || [];
    return kids[kids.indexOf(this) - 1] || null; }
  get nextElementSibling(){ return this.nextSibling; }
  querySelector(s){ return this.querySelectorAll(s)[0] || null; }
  querySelectorAll(sel){
    const raw = String(sel).trim();
    // Parsed without a regex on purpose: this string passes through a Python
    // triple-quote and a shell before node sees it, and a bracket class does
    // not survive that reliably.
    const isAttr = raw.startsWith('[') && raw.endsWith(']');
    const inner = isAttr ? raw.slice(1, -1) : '';
    const eq = inner.indexOf('=');
    const rawName = eq < 0 ? inner : inner.slice(0, eq);
    const rawValue = eq < 0 ? undefined : inner.slice(eq + 1).split('"').join('');
    const key = isAttr ? rawName.replace('data-', '').split('-').map((part, i) =>
      i ? part.charAt(0).toUpperCase() + part.slice(1) : part).join('') : '';
    const want = raw.toUpperCase();
    const out = [];
    const walk = (n) => { for (const c of n.children) {
      const hit = isAttr
        ? (c.dataset[key] !== undefined
           && (rawValue === undefined || String(c.dataset[key]) === rawValue))
        : c.tagName === want;
      if (hit) out.push(c); walk(c); } };
    walk(this); return out; }
  // Walk the parent chain too, so a TEXT NODE inside a span counts as
  // contained. Without this `selectionPoint` rejects every caret and no
  // test could place one.
  contains(n){ if (n === this) return true;
    let p = n && n.parentElement;
    while (p) { if (p === this) return true; p = p.parentElement; }
    return this.children.some((c) => c.contains && c.contains(n)); }
  get selectedOptions(){ return this.options.filter((o) =>
    o.selected === true || (!this.multiple
      && String(o.value) === String(this.value))); }
  closest(){ return null; }
  focus(){}
  remove(){ const kids = this.parentElement?.children;
    if (!kids) return;
    const at = kids.indexOf(this);
    if (at >= 0) kids.splice(at, 1);
    this.parentElement = null; }
}
globalThis.document = {
  createElement: (t) => new N(t),
  createTextNode: (t) => ({ nodeType: 3, nodeValue: t }),
  body: new N("body"), activeElement: null,
};
globalThis.window = { addEventListener(){}, removeEventListener(){} };
globalThis.Node = { TEXT_NODE: 3 };
// Without this every `instanceof HTMLElement` guard throws, which silently
// put `readDom` out of reach of every test using this stub.
globalThis.HTMLElement = N;
globalThis.getSelection = () => null;
"""


def test_reference_routing_uses_human_placement_without_rewriting_inline():
    result = _run_chip_dom_script("""
        const attachment = {
            attachment_id: "ref-chip", emission_group_id: "ref-chip",
            kind: "reference",
            source: { semantic_unit_ids: ["unit:woman"] }, config: {},
            capabilities: [{ capability_id: "mentions", kind: "mentions",
                placement: "inline", enabled: true }],
        };
        const scene = {
            duration_frames: 100,
            _context_channel_keys: ["subject_definitions", "summary",
                "retention_analysis", "detailed_description"],
            reference_lane_count: 1,
            reference_lane_configs: [{}],
            reference_lane_recipes: [{ lane_id: "lane-picture", recipe: { soft: {
                compatible_profiles: ["minimax_h3_ref@1"],
                physical_population: "pictures",
            } } }],
            reference_items: [{ reference_item_id: "item-1", lane_index: 0,
                start_frame: 0, end_frame: 100,
                members: [{ entity_id: "entity-1", member_id: "member-1" }] }],
            active_minimax_h3_setup_id: "setup-1",
            minimax_h3_conditioning_setups: [{ setup_id: "setup-1", mode: "reference",
                picture_lane_ids: ["lane-picture"], video_lane_ids: [], audio_lane_ids: [] }],
        };
        const semanticUnits = [{ semantic_unit_id: "unit:woman", name: "Woman",
            sources: [{ entity_id: "entity-1", member_id: "member-1" }] }];
        const references = [{ reference_id: "entity-1", name: "Woman",
            members: [{ member_id: "member-1", prompt: "A woman" }] }];
        const derived = {
            definitions: {order:1, channel_key:"subject_definitions",
                placement:"section_prefix", label:"Definition"},
            summary: {order:2, channel_key:"summary",
                placement:"section_prefix", label:"Summary"},
            retention: {order:3, channel_key:"retention_analysis",
                placement:"section_prefix", label:"Retention"},
            mentions: {order:4, channel_key:"detailed_description",
                placement:"inline", label:"Scene mention"},
            audio_relationship: {order:5, channel_key:"summary",
                placement:"section_prefix", label:"Audio relationship"},
        };
        const candidate = {
            profile: { physical_populations:[{key:"pictures"}],
                identity_kinds:[{key:"subject"}],
                capabilities: { reference: { derived } } },
            attachment_capability_projections: [{
                attachment_id: "ref-chip", capability_id: "mentions",
                channel_key: "detailed_description", effective_phase: "inline",
                rendered_at_anchor: false, state: "emitted",
                state_reason: "Placed after section-prefix contributions.",
                text: "<Subject 1>",
            }],
        };
        const open = (anchoredChannels) => mod.configurePromptAttachment(attachment, {
            scene, references, semanticUnits, channelKey: "detailed_description",
            profileId: "minimax_h3_ref@1", scope: "global", candidate,
            profile: candidate.profile,
            placementPhases: [
                {value:"document_preamble",label:"Document preamble",description:"Preamble"},
                {value:"channel_prefix",label:"Channel prefix",description:"Channel start"},
                {value:"global_document",label:"Global document",description:"Global"},
                {value:"section_prefix",label:"Section prefix",description:"Section start"},
                {value:"inline",label:"Inline",description:"At cursor"},
                {value:"section_suffix",label:"Section suffix",description:"Section end"},
                {value:"channel_suffix",label:"Channel suffix",description:"Channel end"},
            ], anchoredChannels,
        });
        const scopePromise = open([]);
        const rows = document.body.querySelectorAll("div")
            .filter((value) => value.className === "sonder-prompt-routing-row");
        const mentions = rows.find((row) => row.children[0].children[1].title === "mentions");
        const definitions = rows.find((row) =>
            row.children[0].children[1].title === "definitions");
        const summary = rows.find((row) =>
            row.children[0].children[1].title === "summary");
        const retention = rows.find((row) =>
            row.children[0].children[1].title === "retention");
        const audio = rows.find((row) =>
            row.children[0].children[1].title === "audio_relationship");
        const scopeControls = mentions.children[1];
        const scopePlacement = scopeControls.children[1];
        const scopeInline = scopePlacement.options.find((option) => option.value === "inline");
        const routingCss = document.body.querySelectorAll("style")[0].textContent;
        const attach = document.body.querySelectorAll("button")
            .find((button) => button.textContent === "Attach");
        attach._handlers.click[0]();
        const saved = await scopePromise;
        const savedMention = saved.capabilities.find((value) =>
            value.capability_id === "mentions");

        document.body.children = [];
        open(["detailed_description"]);
        const anchoredRows = document.body.querySelectorAll("div")
            .filter((value) => value.className === "sonder-prompt-routing-row");
        const anchoredMentions = anchoredRows.find((row) =>
            row.children[0].children[1].title === "mentions");
        const anchoredPlacement = anchoredMentions.children[1].children[1];
        const anchoredInline = anchoredPlacement.options.find((option) =>
            option.value === "inline");
        console.log(JSON.stringify({
            scopeValue: scopePlacement.value,
            scopeLabel: scopeInline.textContent,
            scopeHelp: mentions.children[2].textContent,
            scopeState: scopeControls.children[2].textContent,
            providerLabel: scopeControls.children[0].options[0].textContent,
            savedPlacement: savedMention.placement,
            capabilityLabels: [definitions, summary, retention, mentions, audio]
                .map((row) => row.children[0].children[1].textContent),
            audioLabel: audio.children[0].children[1].textContent,
            containerType: document.body.children[0].children[0].style.containerType,
            routingCss,
            anchoredLabel: anchoredInline.textContent,
            anchoredHelp: anchoredMentions.children[2].textContent,
            compiledLabel: mentions.children[3].children[0].children[0].textContent,
            compiledValue: mentions.children[3].children[0].children[1].textContent,
            compiledSource: mentions.children[3].children[0].children[2].textContent,
        }));
    """)
    assert result["scopeValue"] == "inline"
    assert result["scopeLabel"] == "After section prefixes"
    assert result["scopeHelp"] == (
        "Placed after section-prefix contributions and before authored text.")
    assert "After section prefixes" in result["scopeState"]
    assert result["providerLabel"] == "Provider default → detailed_description"
    assert result["savedPlacement"] == "inline"
    assert result["capabilityLabels"] == [
        "Definition", "Summary", "Retention", "Scene mention", "Audio relationship"]
    assert result["audioLabel"] == "Audio relationship"
    assert result["containerType"] == "inline-size"
    assert "@container (max-width:600px)" in result["routingCss"]
    assert result["anchoredLabel"] == "Inline at cursor"
    assert "rerouted capability appears after section prefixes" in result["anchoredHelp"]
    assert result["compiledLabel"] == "Last compiled output"
    assert result["compiledValue"] == "<Subject 1>"
    assert result["compiledSource"] == "Compiler projection"


def test_reference_effective_values_distinguish_authority_and_reset_authored_empty():
    result = _run_chip_dom_script("""
        const raw = {
            attachment_id:"chip",kind:"reference",
            source:{semantic_unit_ids:["identity"]},
            config:{overrides:{summary:""}},
            capabilities:[{capability_id:"summary",kind:"summary",enabled:true,
                config:{future_value:"preserved"}}],
        };
        const options = {
            scene:{duration_frames:20,_context_channel_keys:["summary"],
                reference_lane_count:1,reference_lane_configs:[{}],
                reference_lane_recipes:[{lane_id:"lane",recipe:{soft:{
                    compatible_profiles:["format_a@1"],physical_population:"pictures"}}}],
                reference_items:[{reference_item_id:"item",lane_index:0,
                    start_frame:0,end_frame:20,
                    members:[{entity_id:"entity",member_id:"member"}]}],
                active_minimax_h3_setup_id:"setup",
                minimax_h3_conditioning_setups:[{setup_id:"setup",mode:"reference",
                    picture_lane_ids:["lane"],video_lane_ids:[],audio_lane_ids:[]}]},
            references:[{reference_id:"entity",members:[{member_id:"member"}]}],
            semanticUnits:[{semantic_unit_id:"identity",handle:"Lead",
                name:"Lead",definition:"Person",
                sources:[{entity_id:"entity",member_id:"member"}],
                attachment_defaults:{summary:"Identity summary"}}],
            profileId:"format_a@1",
            profile:{name:"Format A",identity_kinds:[{key:"subject"}],
                physical_populations:[{key:"pictures"}],capabilities:{reference:{
                defaults:{summary:"Format summary"},
                capability_defaults:{summary:{task_types:["reference generation"]}},
                derived:{summary:{order:1,channel_key:"summary",
                    placement:"section_prefix",label:"Summary",
                    fields:{summary:{label:"Summary"},
                        task_types:{type:"enum_multi",label:"Task categories",
                            values:["reference generation"]}}}},
            }}},
            placementPhases:[{value:"section_prefix",label:"Section prefix",
                description:"Before text"}],
        };
        const open = () => mod.configurePromptAttachment(raw, options);
        const pending = open();
        const routingRow = document.body.querySelectorAll("div")
            .find((value) => value.className === "sonder-prompt-routing-row");
        const effective = routingRow.children[3];
        // The deliberately tiny DOM does not implement textContent clearing
        // child nodes, so inspect the latest rendered capability pair.
        const snapshot = () => effective.children.slice(-2).map((line) => ({
            label: line.children[0].textContent,
            value: line.children[1].textContent,
            source: line.children[2].textContent,
            tier: line.children[2].dataset.sonderAuthorityTier,
        }));
        const before = snapshot();
        const reset = document.body.querySelectorAll("button").find((button) =>
            button.textContent === "Reset"
            && button.parentElement?.children?.[2]?.textContent === "Chip override");
        const resetEnabled = reset?.disabled === false;
        reset._handlers.click[0]();
        const after = snapshot();
        const resetDisabled = reset.disabled === true;
        const attach = document.body.querySelectorAll("button")
            .find((button) => button.textContent === "Attach");
        attach._handlers.click[0]();
        const saved = await pending;
        document.body.children = [];
        const emptyPending = open();
        const emptyAttach = document.body.querySelectorAll("button")
            .find((button) => button.textContent === "Attach");
        emptyAttach._handlers.click[0]();
        const emptySaved = await emptyPending;
        console.log(JSON.stringify({before, after, resetEnabled, resetDisabled,
            savedOverrides:saved.config.overrides,
            emptySavedOverrides:emptySaved.config.overrides,
            savedCapabilityConfig:saved.capabilities[0].config,
            emptySavedCapabilityConfig:emptySaved.capabilities[0].config}));
    """)
    assert result["before"] == [
        {"label": "Input · Summary", "value": "(authored empty)",
         "source": "Chip override", "tier": "chip"},
        {"label": "Input · Task categories", "value": "reference generation",
         "source": "Prompt Format default · Format A", "tier": "format"},
    ]
    assert result["after"] == [
        {"label": "Input · Summary", "value": "Identity summary",
         "source": "Shared identity default · @Lead", "tier": "shared"},
        {"label": "Input · Task categories", "value": "reference generation",
         "source": "Prompt Format default · Format A", "tier": "format"},
    ]
    assert result["resetEnabled"] is True
    assert result["resetDisabled"] is True
    assert "summary" not in result["savedOverrides"]
    assert result["emptySavedOverrides"]["summary"] == ""
    assert result["savedCapabilityConfig"] == {"future_value": "preserved"}
    assert result["emptySavedCapabilityConfig"] == {"future_value": "preserved"}


def test_reference_chip_save_preserves_task_types_without_or_beyond_vocabulary():
    result = _run_chip_dom_script("""
        const phases = [
            {value:"section_prefix",label:"Section prefix",description:"Before text"},
            {value:"inline",label:"Inline",description:"At cursor"},
        ];
        const sceneFor = (profileId, capability) => ({
            duration_frames: 20, _context_channel_keys: ["visual"],
            reference_lane_count: 1, reference_lane_configs: [{}],
            reference_lane_recipes: [{lane_id:"lane", recipe:{soft:{
                compatible_profiles:[profileId], exposed_capabilities:[capability],
            }}}],
            reference_items: [{reference_item_id:"item", lane_index:0,
                start_frame:0, end_frame:20, members:[]}],
        });
        const save = async (attachment, profileId, profile) => {
            const pending = mod.configurePromptAttachment(attachment, {
                scene: sceneFor(profileId, attachment.capabilities[0].kind),
                profileId, profile, placementPhases: phases,
            });
            const unsupported = document.body.querySelectorAll("option")
                .filter((option) => option.textContent.startsWith("Unsupported saved value:"))
                .map((option) => option.textContent);
            const attach = document.body.querySelectorAll("button")
                .find((button) => button.textContent === "Attach");
            attach._handlers.click[0]();
            return {saved: await pending, unsupported};
        };
        const noVocabulary = await save({
            attachment_id:"one", kind:"reference",
            source:{reference_item_id:"item"},
            config:{overrides:{task_types:["video editing"],summary:"kept"}},
            capabilities:[{capability_id:"derived_prompt",kind:"derived_prompt",
                placement:"inline",enabled:true}],
        }, "generic@1", {capabilities:{reference:{derived:{
            derived_prompt:{order:1,channel_key:"visual",placement:"inline",
                label:"Reference prompt",fields:{}},
        }}}});
        document.body.children = [];
        const unknownVocabulary = await save({
            attachment_id:"two", kind:"reference",
            source:{reference_item_id:"item"},
            config:{overrides:{task_types:["future task"]}},
            capabilities:[{capability_id:"summary",kind:"summary",
                placement:"section_prefix",enabled:true}],
        }, "custom@1", {capabilities:{reference:{derived:{
            summary:{order:1,channel_key:"visual",placement:"section_prefix",
                label:"Summary",fields:{task_types:{type:"enum_multi",
                    values:[{value:"known task",label:"Known task"}]}}},
        }}}});
        console.log(JSON.stringify({
            noVocabulary: noVocabulary.saved.config.overrides,
            unknownVocabulary: unknownVocabulary.saved.config.overrides,
            unsupported: unknownVocabulary.unsupported,
        }));
    """)
    assert result == {
        "noVocabulary": {"task_types": ["video editing"], "summary": "kept"},
        "unknownVocabulary": {"task_types": ["future task"]},
        "unsupported": ["Unsupported saved value: future task"],
    }


def test_reference_chip_says_when_declared_fields_could_not_resolve():
    """An unresolved format explains itself; a field-less one stays silent.

    Opened before the catalog landed, `resolvedProfile` is `{}`, so every
    declared control returned null and Summary task types plus both retention
    intents rendered NO ROW AT ALL with nothing saying why — indistinguishable
    from a format that genuinely declares none.

    The discrimination is the point: keying this on "the catalog is absent"
    would show a loading row forever on a legitimate format declaring no such
    fields. Mutation this must not survive: render the notice whenever a
    declared control is missing.
    """
    result = _run_chip_dom_script("""
        const scene = {
            duration_frames: 20, _context_channel_keys: ["visual"],
            reference_lane_count: 1, reference_lane_configs: [{}],
            reference_lane_recipes: [{lane_id:"lane", recipe:{soft:{
                compatible_profiles:["generic@1"],
                exposed_capabilities:["derived_prompt"],
            }}}],
            reference_items: [{reference_item_id:"item", lane_index:0,
                start_frame:0, end_frame:20, members:[]}],
        };
        const attachment = () => ({
            attachment_id:"one", kind:"reference",
            source:{reference_item_id:"item"}, config:{overrides:{}},
            capabilities:[{capability_id:"derived_prompt",kind:"derived_prompt",
                placement:"inline",enabled:true}],
        });
        const notices = () => document.body.querySelectorAll("div")
            .filter((node) => node.dataset.sonderPromptDeclaredFieldsState)
            .map((node) => node.textContent);
        // The catalog has not arrived: the chip receives no profile at all.
        mod.configurePromptAttachment(attachment(), {scene, profileId:"generic@1"});
        const unresolved = notices();
        document.body.children = [];
        // A REAL format that declares a reference capability but no task types
        // or intents. Nothing is wrong here, so nothing should be said.
        mod.configurePromptAttachment(attachment(), {
            scene, profileId:"generic@1",
            profile:{profile_id:"generic", capabilities:{reference:{derived:{
                derived_prompt:{order:1,channel_key:"visual",placement:"inline",
                    label:"Reference prompt",fields:{}},
            }}}},
        });
        const declaredNone = notices();
        console.log(JSON.stringify({
            unresolvedCount: unresolved.length,
            unresolvedText: unresolved[0] || "",
            declaredNoneCount: declaredNone.length,
        }));
    """)
    assert result["unresolvedCount"] == 1
    assert "still loading" in result["unresolvedText"]
    # It must reassure rather than imply the values were dropped — the stored
    # overrides really are untouched, because a control that never rendered is
    # excluded from `overrideControls` and its key never enters the save.
    assert "untouched" in result["unresolvedText"]
    assert result["declaredNoneCount"] == 0


def test_active_chip_intersects_recipe_union_but_keeps_saved_undeclared_parts():
    result = _run_chip_dom_script("""
        const scene = {
            duration_frames:20, _context_channel_keys:["visual"],
            reference_lane_count:1, reference_lane_configs:[{}],
            reference_lane_recipes:[{lane_id:"lane",recipe:{soft:{
                compatible_profiles:["format_a@1","format_b@1"],
                exposed_capabilities:["definitions","audio_relationship"],
            }}}],
            reference_items:[{reference_item_id:"item",lane_index:0,
                start_frame:0,end_frame:20,members:[]}],
        };
        const profile = {capabilities:{reference:{derived:{
            definitions:{order:1,channel_key:"visual",placement:"section_prefix",
                label:"Definitions",fields:{}},
        }}}};
        const open = (capabilities) => mod.configurePromptAttachment({
            attachment_id:"chip",kind:"reference",
            source:{reference_item_id:"item"},config:{overrides:{}},capabilities,
        }, {scene,profileId:"format_a@1",profile,placementPhases:[
            {value:"section_prefix",label:"Section prefix",description:"Before text"},
        ]});
        const freshPromise = open([]);
        const freshRows = document.body.querySelectorAll("div")
            .filter((value) => value.className === "sonder-prompt-routing-row");
        const freshKinds = freshRows.map((row) => row.children[0].children[1].title);
        document.body.querySelectorAll("button").find((button) =>
            button.textContent === "Cancel")._handlers.click[0]();
        await freshPromise;

        document.body.children = [];
        const savedPromise = open([{capability_id:"audio_relationship",kind:"audio_relationship",
            placement:"section_prefix",enabled:true}]);
        const savedRows = document.body.querySelectorAll("div")
            .filter((value) => value.className === "sonder-prompt-routing-row");
        const savedKinds = savedRows.map((row) => row.children[0].children[1].title);
        const voice = savedRows.find((row) =>
            row.children[0].children[1].title === "audio_relationship");
        const warning = voice.children[0].title;
        document.body.querySelectorAll("button").find((button) =>
            button.textContent === "Cancel")._handlers.click[0]();
        await savedPromise;
        console.log(JSON.stringify({freshKinds,savedKinds,warning}));
    """)
    assert result["freshKinds"] == ["definitions"]
    assert result["savedKinds"] == ["definitions", "audio_relationship"]
    assert "not declared by the active Prompt Format" in result["warning"]


def test_document_and_scope_chips_share_two_line_container_bounded_labels():
    result = _run_chip_dom_script("""
        const longLabel = "Prompt link → Section 1 (0–241) · non-diegetic music target";
        const inlineAttachment = { attachment_id: "inline-link", kind: "prompt_link",
            source: {}, config: { label: longLabel } };
        const editor = mod.createPromptDocumentEditor({
            document: { nodes: [{ type: "attachment", node_id: "anchor",
                attachment_id: "inline-link" }] },
            attachments: [inlineAttachment],
        });
        const inlineChip = editor.children.find((value) =>
            value.dataset.attachmentId === "inline-link");
        const inlineLabel = inlineChip.children.find((value) =>
            value.dataset.sonderContextChipLabel === "1");
        const inlineRemove = inlineChip.children.find((value) => value.tagName === "BUTTON");

        const scopeAttachment = { attachment_id: "scope-reference", kind: "reference",
            source: {}, config: {} };
        const scope = mod.createScopeChipRow({ attachments: [scopeAttachment],
            attachmentLabelFor: () => longLabel, onRemove() {} });
        const scopeChip = scope.querySelectorAll("button").find((value) =>
            value.dataset.attachmentId === "scope-reference");
        const scopeLabel = scopeChip.children.find((value) =>
            value.dataset.sonderContextChipLabel === "1");
        const scopeHolder = scopeChip.parentElement;
        const scopeRemove = scopeHolder.children.find((value) =>
            String(value.attributes["aria-label"] || "").startsWith("Remove "));
        console.log(JSON.stringify({
            inlineAtomic: inlineChip.contentEditable,
            inlineChipCss: inlineChip.style.cssText,
            inlineLabelCss: inlineLabel.style.cssText,
            inlineTitle: inlineChip.title,
            inlineAria: inlineChip.attributes["aria-label"],
            inlineRemoveCss: inlineRemove.style.cssText,
            scopeChipCss: scopeChip.style.cssText,
            scopeLabelCss: scopeLabel.style.cssText,
            scopeTitle: scopeChip.title,
            scopeAria: scopeChip.attributes["aria-label"],
            scopeHolderCss: scopeHolder.style.cssText,
            scopeRemoveCss: scopeRemove.style.cssText,
        }));
    """)
    for key in ("inlineChipCss", "scopeChipCss"):
        assert "max-width:min(180px,calc(100% - 4px))" in result[key]
        assert "box-sizing:border-box" in result[key]
        assert "white-space:normal" in result[key]
    for key in ("inlineLabelCss", "scopeLabelCss"):
        assert "-webkit-line-clamp:2" in result[key]
        assert "overflow-wrap:anywhere" in result[key]
    assert result["inlineAtomic"] == "false"
    assert "Prompt link → Section 1" in result["inlineTitle"]
    assert "Prompt link → Section 1" in result["inlineAria"]
    assert "Prompt link → Section 1" in result["scopeTitle"]
    assert "Prompt link → Section 1" in result["scopeAria"]
    assert "flex:0 0 auto" in result["inlineRemoveCss"]
    assert "max-width:100%" in result["scopeHolderCss"]
    assert "flex:0 0 auto" in result["scopeRemoveCss"]


def test_scope_rows_never_offer_inline_only_kinds():
    result = _run_chip_dom_script("""
        const optionValues = (row) => row.querySelectorAll("select")
            .flatMap((s) => s.options.map((o) => o.value));
            const explicit = mod.createScopeChipRow({ allowedKinds: [
                "shot","timestamp","reference","guide","vocal_event","prompt_link",
                "prompt_link_scope","custom"] });
        const bare = mod.createScopeChipRow({});
        const legacy = mod.createScopeChipRow({ attachments: [
            { attachment_id: "legacy", kind: "vocal_event", config: {} }] });
        const chip = legacy.querySelectorAll("button")
            .find((b) => /must be placed inline/.test(b.title || ""));
        console.log(JSON.stringify({
            inlineOnly: mod.INLINE_ONLY_KINDS,
            explicitlyRequested: optionValues(explicit),
            byDefault: optionValues(bare),
            legacyChipMarked: !!chip,
        }));
    """)
    assert result["inlineOnly"] == ["prompt_link", "vocal_event"]
    # Asking for the inline-only kinds explicitly must still not offer them.
    assert result["explicitlyRequested"] == [
        "shot", "timestamp", "reference", "prompt_link_scope", "custom"]
    assert result["byDefault"] == result["explicitlyRequested"]
    # A legacy scope chip stays visible and reachable so it can be rebound.
    assert result["legacyChipMarked"] is True


def test_section_scope_prompt_link_picker_defaults_all_channels_and_separates_actions():
    result = _run_chip_dom_script("""
        const attachment = mod.normalizePromptAttachment({
            kind: "prompt_link_scope", source: {prompt_id: "source"}
        });
        let removed = 0, copied = 0;
        const row = mod.createScopeChipRow({
            attachments: [attachment], onRemove: () => { removed += 1; },
            onConvertPromptLinkCopy: () => { copied += 1; },
        });
        const actionButtons = row.querySelectorAll("button");
        actionButtons.find((button) => button.textContent === "Unlink")._handlers.click[0]();
        actionButtons.find((button) => button.textContent === "Convert to copy")._handlers.click[0]();
        const configuredPromise = mod.configurePromptAttachment({kind: "prompt_link_scope"}, {
            scene: {
                _context_consumer_start: 10,
                _context_channel_keys: ["visual", "audio"],
                prompt_sections: [
                    {prompt_id: "source", start_frame: 0, end_frame: 10, prompt: "source"},
                    {prompt_id: "consumer", start_frame: 10, end_frame: 20, prompt: "consumer"},
                ],
            },
            channelKey: "visual", profileId: "generic@1",
        });
        const modal = document.body.children.at(-1);
        const selects = modal.querySelectorAll("select");
        selects[0].value = "source";
        // Channels are checkboxes, not a ctrl-click multi-select.
        const boxes = modal.querySelectorAll("input")
            .filter((input) => input.type === "checkbox");
        const buttons = modal.querySelectorAll("button");
        const defaulted = boxes.map((box) => [box.value, box.checked]);
        // "None" then "All" proves both bulk actions reach every row.
        buttons.find((button) => button.textContent === "None")._handlers.click[0]();
        const cleared = boxes.every((box) => !box.checked);
        buttons.find((button) => button.textContent === "All")._handlers.click[0]();
        const restored = boxes.every((box) => box.checked);
        // Narrow to one channel and confirm only that one is stored.
        boxes.find((box) => box.value === "audio").checked = false;
        buttons.find((button) => button.textContent === "Attach")._handlers.click[0]();
        const configured = await configuredPromise;
        console.log(JSON.stringify({
            exportable: attachment.link_exportable,
            actions: [removed, copied],
            defaulted, cleared, restored,
            role: modal.querySelectorAll("div")
                .some((node) => node.attributes.role === "group"),
            channelKeys: configured.source.channel_keys,
            sourcePrompt: configured.source.prompt_id,
        }));
    """)
    assert result == {
        "exportable": True,
        "actions": [1, 1],
        # No stored selection still means every channel.
        "defaulted": [["visual", True], ["audio", True]],
        "cleared": True,
        "restored": True,
        # A checkbox group carries its own accessible name.
        "role": True,
        "channelKeys": ["visual"],
        "sourcePrompt": "source",
    }


def test_inline_context_menus_still_offer_the_inline_only_kinds():
    result = _run_chip_dom_script("""
        console.log(JSON.stringify({
            section: mod.promptContextAuthoringKinds(),
            global: mod.promptContextAuthoringKinds(
                ["reference", "guide", "custom"]),
        }));
    """)
    assert "vocal_event" in result["section"] and "prompt_link" in result["section"]
    assert "timestamp" not in result["section"]
    assert result["global"] == ["reference", "custom"]


def test_attachment_channel_projection_is_split_and_clicks_the_same_attachment():
    result = _run_chip_dom_script("""
        const attachment = { attachment_id: "chip-1", kind: "reference",
            source: { semantic_unit_ids: ["unit:woman"] }, config: {} };
        const candidate = {
            attachment_channel_previews: { "chip-1": {
                visual: "Visual contribution", summary: "Summary contribution",
                subject_definitions: "Definition contribution" } },
            emissions: [
                { attachment_id: "chip-1", channel_key: "visual",
                    origin: "section", placement: "section_prefix" },
                { attachment_id: "chip-1", channel_key: "summary",
                    origin: "section", placement: "section_suffix" },
                { attachment_id: "chip-1", channel_key: "subject_definitions",
                    origin: "global", placement: "global_prefix" },
            ],
            attachment_channel_routes: { "chip-1": {
                visual: "section_prefix", summary: "section_suffix",
                subject_definitions: "global_document" } },
        };
        const activated = [];
        const projections = ["visual", "summary", "subject_definitions"].map(
            (channelKey) => mod.createAttachmentChannelProjections({
                channelKey, attachments: [attachment], candidate,
                attachmentLabelFor: () => "Granny",
                onActivate: (value) => activated.push(value.attachment_id),
            }));
        projections[0].beforeHost.children[0]._handlers.click[0]();
        console.log(JSON.stringify({
            counts: projections.map((value) =>
                value.beforeHost.children.length + value.afterHost.children.length),
            regions: projections.map((value) =>
                value.afterHost.children.length ? "after" : "before"),
            labels: projections.map((value) =>
                (value.beforeHost.children[0] || value.afterHost.children[0]).textContent),
            titles: projections.map((value) =>
                (value.beforeHost.children[0] || value.afterHost.children[0]).title),
            activated,
            hoverHandlers: projections.map((value) =>
                Object.keys((value.beforeHost.children[0] || value.afterHost.children[0])._handlers).filter((key) =>
                    key.includes("mouse") && key !== "mousedown")),
        }));
    """)
    assert result["counts"] == [1, 1, 1]
    assert result["regions"] == ["before", "after", "before"]
    assert all("Granny" in value for value in result["labels"])
    assert "Visual contribution" in result["titles"][0]
    assert "Section prefix" in result["titles"][0]
    assert result["activated"] == ["chip-1"]
    assert result["hoverHandlers"] == [[], [], []]


def test_silent_linked_projection_stays_visible_with_an_explanation():
    result = _run_chip_dom_script("""
        const attachment = { attachment_id: "later", emission_group_id: "group",
            kind: "reference", source: {}, config: {} };
        const candidate = {
            attachment_channel_previews: { later: { visual: "" } },
            attachment_channel_routes: { later: { visual: "section_prefix" } },
            emissions: [{ attachment_id: "earlier", emission_group_id: "group",
                channel_key: "visual", text: "once" }],
        };
        const hosts = mod.createAttachmentChannelProjections({
            channelKey: "visual", attachments: [attachment], candidate });
        const chip = hosts.beforeHost.children[0];
        console.log(JSON.stringify({ count: hosts.beforeHost.children.length,
            muted: chip.dataset.sonderPromptProjectionMuted,
            title: chip.title }));
    """)
    assert result["count"] == 1
    assert result["muted"] == "1"
    assert "linked chip elsewhere" in result["title"]


def test_capability_projection_presentation_anchor_marker_and_linked_warning():
    result = _run_chip_dom_script("""
        const attachments = [
            { attachment_id: "visible", emission_group_id: "group", kind: "custom", config: {} },
            { attachment_id: "anchored", emission_group_id: "anchored", kind: "prompt_link", config: {} },
            { attachment_id: "marker", emission_group_id: "marker", kind: "shot", config: {} },
        ];
        const candidate = { attachment_capability_projections: [
            { attachment_id: "visible", emission_group_id: "group", capability_id: "custom",
              channel_key: "visual", declared_placement: "section_prefix",
              effective_phase: "section_prefix", region: "before", state: "emitted",
              state_reason: "Emitted", text: "Resolved first", rendered_at_anchor: false },
            { attachment_id: "sibling", emission_group_id: "group", capability_id: "custom",
              channel_key: "visual", declared_placement: "section_prefix",
              effective_phase: "section_prefix", region: "before", state: "emitted",
              state_reason: "Emitted", text: "Sibling", rendered_at_anchor: false },
            { attachment_id: "anchored", emission_group_id: "anchored", capability_id: "prompt_link",
              channel_key: "visual", declared_placement: "inline", effective_phase: "inline",
              region: "before", state: "emitted", state_reason: "At caret", text: "Linked",
              rendered_at_anchor: true },
            { attachment_id: "marker", emission_group_id: "marker", capability_id: "shot",
              channel_key: "visual", declared_placement: "section_prefix",
              effective_phase: "section_prefix", region: "before", state: "marker",
              state_reason: "Composer marker", text: "[Shot 1]", rendered_at_anchor: false },
        ] };
        const warnings = [];
        const hosts = mod.createAttachmentChannelProjections({
            channelKey: "visual", attachments, candidate,
            onSetCapabilityEnabled() {},
            onLinkedSuppressionWarning: (value) => warnings.push(value),
        });
        const buttons = hosts.beforeHost.querySelectorAll("button");
        const projectionButtons = buttons.filter((value) =>
            value.dataset.sonderPromptProjection === "1");
        const suppressionButtons = buttons.filter((value) => value.textContent === "x");
        suppressionButtons[0]._handlers.click[0]();
        console.log(JSON.stringify({
            projectionLabels: projectionButtons.map((value) => value.textContent),
            projectionIds: projectionButtons.map((value) => value.dataset.attachmentId),
            suppressionCount: suppressionButtons.length,
            warnings,
        }));
    """)
    assert result["projectionIds"] == ["visible", "marker"]
    assert result["projectionLabels"][0].startswith("Resolved first")
    assert "custom" not in result["projectionLabels"][0]
    assert result["suppressionCount"] == 1
    assert result["warnings"] == [{"channelKey": "visual", "linkedCount": 1}]


def test_non_emitting_projection_label_is_reason_then_channel_only():
    result = _run_chip_dom_script("""
        const attachment = { attachment_id: "disabled", emission_group_id: "disabled",
            kind: "custom", config: {} };
        const candidate = { attachment_capability_projections: [{
            attachment_id: "disabled", emission_group_id: "disabled",
            capability_id: "custom", channel_key: "visual",
            declared_placement: "section_prefix", effective_phase: "section_prefix",
            region: "before", state: "disabled", state_reason: "Suppressed here",
            text: "", rendered_at_anchor: false,
        }] };
        const hosts = mod.createAttachmentChannelProjections({
            channelKey: "visual", attachments: [attachment], candidate,
            onSetCapabilityEnabled() {},
        });
        console.log(JSON.stringify(hosts.beforeHost
            .querySelectorAll("button")[0].textContent));
    """)
    assert result == "Suppressed here · visual"


def test_one_chip_projects_each_non_inline_placement_in_the_same_channel():
    result = _run_chip_dom_script("""
        const attachment = { attachment_id: "multi", kind: "custom",
            source: {}, config: {} };
        const hosts = mod.createAttachmentChannelProjections({
            channelKey: "visual", attachments: [attachment], candidate: {
                attachment_channel_previews: { multi: { visual: "EDGE" } },
                attachment_channel_routes: { multi: { visual: [
                    "section_prefix", "inline", "section_suffix"] } },
                emissions: [],
            },
        });
        console.log(JSON.stringify({
            before: hosts.beforeHost.children.map((value) => value.title),
            after: hosts.afterHost.children.map((value) => value.title),
        }));
    """)
    assert len(result["before"]) == 1
    assert len(result["after"]) == 1
    assert "Section prefix" in result["before"][0]
    assert "Section suffix" in result["after"][0]


def test_reuse_and_unlink_keep_one_grouping_concept_with_fresh_ids():
    result = _run_chip_dom_script("""
        const source = mod.normalizePromptAttachment({ attachment_id: "source",
            emission_group_id: "group", kind: "reference",
            source: { semantic_unit_ids: ["unit"] },
            config: { definition: "original" },
            capabilities: [{ capability_id: "definitions", kind: "definitions" }] });
        const reused = mod.reusePromptAttachment(source);
        const unlinked = mod.unlinkPromptAttachment(reused);
            reused.config.overrides.definition = "changed";
        console.log(JSON.stringify({
            idsFresh: reused.attachment_id !== source.attachment_id,
            groupKept: reused.emission_group_id === source.emission_group_id,
                deepCopy: source.config.overrides.definition,
            unlinkKeepsId: unlinked.attachment_id === reused.attachment_id,
            unlinkFreshGroup: unlinked.emission_group_id !== reused.emission_group_id,
        }));
    """)
    assert result == {
        "idsFresh": True,
        "groupKept": True,
        "deepCopy": "original",
        "unlinkKeepsId": True,
        "unlinkFreshGroup": True,
    }


def test_scope_row_shows_linked_badge_unlink_and_reuse_picker():
    result = _run_chip_dom_script("""
        const current = { attachment_id: "a", emission_group_id: "group",
            kind: "reference", source: {}, config: {} };
        const linked = { attachment_id: "b", emission_group_id: "group",
            kind: "reference", source: {}, config: {} };
        const reusable = { attachment_id: "c", emission_group_id: "other",
            kind: "reference", source: {}, config: {} };
        const row = mod.createScopeChipRow({ attachments: [current],
            allSceneAttachments: [current, linked, reusable],
            reusableAttachments: [linked, reusable],
            onReuse() {}, onUnlink() {}, attachmentLabelFor: () => "Granny" });
        console.log(JSON.stringify({
            linkedBadges: row.querySelectorAll("span").filter((value) =>
                value.dataset.sonderLinkedAttachment === "1").length,
            buttons: row.querySelectorAll("button").map((value) => value.textContent),
            selectCount: row.querySelectorAll("select").length,
        }));
    """)
    assert result["linkedBadges"] == 1
    assert "Unlink" in result["buttons"]
    assert "Reuse an existing chip" in result["buttons"]
    assert result["selectCount"] == 2


def test_scope_reuse_picker_respects_scope_and_template_kind_gates():
    result = _run_chip_dom_script("""
        const row = mod.createScopeChipRow({
            attachments: [],
            allowedKinds: ["reference", "custom"],
            reusableAttachments: [
                { attachment_id: "reference", emission_group_id: "ref-group",
                    kind: "reference", source: {}, config: {} },
                { attachment_id: "time", emission_group_id: "time-group",
                    kind: "timestamp", source: {}, config: { standalone: true } },
                { attachment_id: "link", emission_group_id: "link-group",
                    kind: "prompt_link", source: {}, config: {} },
            ],
            onReuse() {}, attachmentLabelFor: (value) => value.attachment_id,
        });
        const selects = row.querySelectorAll("select");
        console.log(JSON.stringify({
            selectCount: selects.length,
            reusableValues: selects[1].children.map((value) => value.value),
        }));
    """)
    assert result == {"selectCount": 2, "reusableValues": ["reference"]}


def test_reuse_picker_disambiguates_identical_independent_groups_by_origin():
    result = _run_chip_dom_script("""
        const first = { attachment_id:"first", emission_group_id:"group-a",
            kind:"reference", source:{}, config:{} };
        const second = { attachment_id:"second", emission_group_id:"group-b",
            kind:"reference", source:{}, config:{} };
        const scene = { prompt_sections: [
            { prompt_id:"one", start_frame:0, end_frame:24, attachments:[first] },
            { prompt_id:"two", start_frame:24, end_frame:48, attachments:[second] },
        ] };
        const row = mod.createScopeChipRow({
            attachments: [], allowedKinds:["reference"],
            reusableAttachments:[first, second], allSceneAttachments:[first, second],
            reuseContext:{scene}, attachmentLabelFor:()=>"Korean Woman", onReuse() {},
        });
        const select = row.querySelectorAll("select")[1];
        console.log(JSON.stringify(select.options.map((option)=>option.textContent)));
    """)
    assert result == [
        "Reference: Korean Woman — section 1 [0-24] — used in 1 section",
        "Reference: Korean Woman — section 2 [24-48] — used in 1 section",
    ]


def test_async_context_menu_restores_caret_bookmark_before_insert():
    result = _run_chip_dom_script("""
        const calls = [];
        const bookmark = { start: { node_id: "text", offset: 2 },
            end: { node_id: "text", offset: 5 } };
        const editor = {
            capturePromptSelection() { calls.push("capture"); return bookmark; },
            restorePromptSelection(value) {
                calls.push("restore");
                this.restored = value;
                return true;
            },
            insertAttachment() { calls.push("insert"); },
        };
        const insertionBookmark = mod.promptInsertionBookmark(editor);
        const items = mod.createPromptContextMenuItems({
            editor,
            bookmark: insertionBookmark,
            onCreate: async (attachment) => {
                calls.push("configure");
                return attachment;
            },
        });
        await items[0].submenu[0].action();

        const cancelCalls = [];
        const cancelEditor = {
            capturePromptSelection() { cancelCalls.push("capture"); return bookmark; },
            restorePromptSelection() { cancelCalls.push("restore"); return true; },
            insertAttachment() { cancelCalls.push("insert"); },
        };
        const cancelItems = mod.createPromptContextMenuItems({
            editor: cancelEditor,
            bookmark: mod.promptInsertionBookmark(cancelEditor),
            onCreate: async () => { cancelCalls.push("configure"); return null; },
        });
        await cancelItems[0].submenu[0].action();

        const staleCalls = [];
        const staleEditor = {
            capturePromptSelection() { staleCalls.push("capture"); return bookmark; },
            restorePromptSelection() { staleCalls.push("restore"); return false; },
            insertAttachment() { staleCalls.push("insert"); },
        };
        const staleItems = mod.createPromptContextMenuItems({
            editor: staleEditor,
            bookmark: mod.promptInsertionBookmark(staleEditor),
            onCreate: async (attachment) => {
                staleCalls.push("configure");
                return attachment;
            },
        });
        await staleItems[0].submenu[0].action();

        const aidCalls = [];
        const aidEditor = {
            capturePromptSelection() { aidCalls.push("capture"); return bookmark; },
            restorePromptSelection() { aidCalls.push("restore"); return true; },
            insertText() { aidCalls.push("insert"); },
        };
        const aidItems = mod.createPromptContextMenuItems({
            editor: aidEditor,
            bookmark: mod.promptInsertionBookmark(aidEditor),
            writingAids: [{ id: "cancel_aid", label: "Cancel aid", text: "{text}" }],
        });
        // An aid needing free text opens the bounded dialog rather than a native
        // prompt. The dialog is built synchronously, so it can be driven before
        // awaiting the action it belongs to.
        globalThis.prompt = () => { throw new Error("native prompt must not be used"); };
        const aidPromise = aidItems[1].submenu
            .find((item) => item.writingAidId === "cancel_aid").action();
        const aidBackdrop = document.body.children.at(-1);
        const aidDialogButtons = aidBackdrop.querySelectorAll("button")
            .map((button) => button.textContent);
        aidBackdrop.querySelectorAll("button")
            .find((button) => button.textContent === "Cancel")
            ._handlers.click[0]();
        await aidPromise;

        const documentEditor = mod.createPromptDocumentEditor({ text: "hello" });
        const span = documentEditor.children[0];
        // The tiny DOM's textContent property does not synthesize a Text node
        // as a browser does, so materialize that one browser primitive here.
        const textNode = document.createTextNode(span.textContent);
        textNode.parentElement = span;
        span.children = [textNode];
        span.childNodes = span.children;
        span.firstChild = textNode;
        documentEditor.contains = (node) => [documentEditor, span, textNode].includes(node);
        documentEditor.querySelectorAll = (selector) =>
            String(selector).includes("data-node-id") ? [span] : [];
        span.closest = () => span;
        let focusCount = 0;
        documentEditor.focus = () => { focusCount += 1; };
        documentEditor.isConnected = true;
        let activeRange = {
            startContainer: textNode, startOffset: 3,
            endContainer: textNode, endOffset: 3,
        };
        const selection = {
            rangeCount: 1,
            getRangeAt() { return activeRange; },
            removeAllRanges() {},
            addRange(range) { activeRange = range; },
        };
        globalThis.getSelection = () => selection;
        document.createRange = () => ({
            setStart(container, offset) { this.startContainer = container; this.startOffset = offset; },
            setEnd(container, offset) { this.endContainer = container; this.endOffset = offset; },
            collapse() {},
        });
        const realBookmark = documentEditor.capturePromptSelection();
        const realRestored = documentEditor.restorePromptSelection(realBookmark);
        documentEditor.isConnected = false;
        const detachedRestored = documentEditor.restorePromptSelection(realBookmark);
        console.log(JSON.stringify({
            calls,
            restored: editor.restored,
            cancelCalls,
            staleCalls,
            aidCalls,
            aidDialogButtons,
            realBookmark,
            realRestored,
            detachedRestored,
            realRestoredOffset: activeRange.startOffset,
            focusCount,
            exposesBookmarkContract:
                typeof documentEditor.capturePromptSelection === "function"
                && typeof documentEditor.restorePromptSelection === "function",
        }));
    """)
    assert result["calls"] == ["capture", "configure", "restore", "insert"]
    assert result["restored"] == {
        "start": {"node_id": "text", "offset": 2},
        "end": {"node_id": "text", "offset": 2},
    }
    assert result["cancelCalls"] == ["capture", "configure", "restore"]
    assert result["staleCalls"] == ["capture", "configure", "restore"]
    assert result["aidCalls"] == ["capture", "restore"]
    # Cancelling the bounded dialog restores the caret and inserts nothing, the
    # same contract the native prompt used to carry.
    assert result["aidDialogButtons"] == ["Cancel", "Insert"]
    assert result["realBookmark"]["start"]["node_id"]
    assert result["realBookmark"]["start"]["node_id"] == result["realBookmark"]["end"]["node_id"]
    assert result["realBookmark"]["start"]["offset"] == 3
    assert result["realBookmark"]["end"]["offset"] == 3
    assert result["realRestored"] is True
    assert result["detachedRestored"] is False
    assert result["realRestoredOffset"] == 3
    assert result["focusCount"] == 1
    assert result["exposesBookmarkContract"] is True


def test_chip_editor_defers_owned_keys_during_ime_composition():
    """The handler must not CLAIM the key while an IME candidate is open.

    Claiming returns `true`, which makes the ownership root `preventDefault()`
    and breaks the IME; falsy yields PRESERVE_DEFAULT, which still stops
    propagation so timeline/graph shortcuts stay isolated.
    """
    result = _run_chip_dom_script("""
        const editor = mod.createPromptDocumentEditor({ text: "hello" });
        let commits = 0;
        editor.addOwnedKeyHandler((e) => {
            if (e.key === "Enter") { commits += 1; return true; }
            return false;
        });
        const send = (key, composing) => ({
            claimed: editor._sonderOwnedKeydown({
                key, isComposing: composing, keyCode: composing ? 229 : 13,
                ctrlKey: false, metaKey: false, shiftKey: false,
                target: {}, preventDefault(){}, stopPropagation(){},
            }) === true,
        });
        const during = send("Enter", true);
        const commitsDuring = commits;
        const after = send("Enter", false);
        console.log(JSON.stringify({
            claimedDuringComposition: during.claimed,
            commitsDuringComposition: commitsDuring,
            claimedAfterComposition: after.claimed,
            commitsAfterComposition: commits,
        }));
    """)
    assert result["claimedDuringComposition"] is False
    assert result["commitsDuringComposition"] == 0
    assert result["claimedAfterComposition"] is True
    assert result["commitsAfterComposition"] == 1


def test_vocal_event_editor_requires_a_subject_or_voice():
    chips = (ROOT / "web" / "js" / "prompt_context_chips.js").read_text(
        encoding="utf-8")
    assert "bindingNotice" in chips
    assert "if (!subjectIds.length && !voiceId) {" in chips
    # A stray grep for the Reference save block's `audio_relationship` line
    # used to live here. It belonged to neither this test's subject nor a
    # behavioural check, and the field list it pinned is now declaration-driven;
    # test_reference_fieldset_renders_only_declared_fields_with_declared_copy
    # covers that surface for real.


def test_timeline_global_inline_picker_matches_structured_kinds():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    menu = widget.split("installPromptContextMenu({", 1)[1]
    assert 'allowedKinds: globalScope' in menu.split("});", 1)[0]
    assert '["reference", "custom"]' in menu.split("});", 1)[0]
    assert '"guide"' not in menu.split("});", 1)[0]


def _run_bridge_shape_script(body):
    """Drive the pure generic Reference bridge shaping module.

    The extension half imports `/scripts/app.js` at module scope and can never
    load under node, which is why the shaping rules live in their own module —
    the same split `reference_bridge_shape.js` uses.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for MiniMax H3 bridge shape tests")
    module_url = (ROOT / "web" / "js" / "reference_bridge_shape.js").as_uri()
    script = f"""
        const fakeNode = (comfyClass, names, connectedNames = []) => ({{
            comfyClass,
            outputs: names.map((name) => ({{
                name, type: "IMAGE", label: name, localized_name: name,
                link: connectedNames.includes(name) ? 7 : null,
            }})),
            addOutput(name, type, options = {{}}) {{
                this.outputs.push({{ name, type, label: options.label ?? name,
                    localized_name: options.label ?? name, link: null }});
            }},
            removeOutput(index) {{ this.outputs.splice(index, 1); }},
        }});
        const labels = (n) => n.outputs.map((slot) => slot.label);
        const localized = (n) => n.outputs.map((slot) => slot.localized_name);
        const mod = await import({json.dumps(module_url)});
        {body}
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def test_reference_bridge_labels_show_compact_audio_ordinals():
    result = _run_bridge_shape_script("""
        const standalone = fakeNode("SonderReferenceAudioBridge",
            ["a01", "a02"]);
        mod.resolveBridgeOutputs(standalone, {
            audioSlotCount: 2, liveOutputs: ["audio_slots"],
            slotLabels: ["Audio 2", "Audio 3"],
        });
        console.log(JSON.stringify({
            standalone: labels(standalone),
            standaloneLocalized: localized(standalone),
        }));
    """)
    # The ordinal is the one the prompt uses, not the socket suffix.
    assert result["standalone"] == ["a01 · Audio 2", "a02 · Audio 3"]
    assert result["standaloneLocalized"] == result["standalone"]


def test_reference_bridge_marks_connected_but_unstaged_sockets_unused():
    """Liveness is decoupled from wiring.

    A connected socket the setup does not drive still emits its type-correct
    fallback into a live link, so it is exactly the case worth announcing. The
    connection pins the removal ceiling; it must not buy the slot a live label.
    """
    result = _run_bridge_shape_script("""
        const numbered = fakeNode("SonderReferenceAudioBridge",
            ["a01", "a02", "a03"], ["a03"]);
        mod.resolveBridgeOutputs(numbered, {
            audioSlotCount: 1, liveOutputs: ["audio_slots"],
            slotLabels: ["Audio 1", "Audio 2", "Audio 3"],
        });
        console.log(JSON.stringify({
            numbered: labels(numbered),
            numberedLocalized: localized(numbered),
        }));
    """)
    # The connection pins the ceiling at 3, so the block never shrinks past it
    # and no lower hole opens — but neither unstaged slot is live.
    assert result["numbered"] == ["a01 · Audio 1",
                                  "a02 · Audio 2 (unused)",
                                  "a03 · Audio 3 (unused)"]
    assert result["numberedLocalized"] == result["numbered"]


def test_reference_bridge_shows_every_socket_while_liveness_is_unknown():
    """A null manifest means "we don't know", not "nothing is driven"."""
    result = _run_bridge_shape_script("""
        const node = fakeNode("SonderReferenceImageBridge", ["r01"]);
        const changed = mod.resolveBridgeOutputs(node, { liveOutputs: null });
        const again = mod.resolveBridgeOutputs(node, { liveOutputs: null });
        console.log(JSON.stringify({
            labels: labels(node), changed, again,
        }));
    """)
    assert len(result["labels"]) == 16
    assert not any("(unused)" in value for value in result["labels"])
    assert result["labels"][0] == "r01"
    # Re-marking an already-marked node must be a no-op, or a graph reload would
    # drift the label on every pass.
    assert result["changed"] is True and result["again"] is False


def test_profile_picker_disables_incompatible_channel_templates():
    panel = (ROOT / "web" / "js" / "editor_prompt_panel.js").read_text(
        encoding="utf-8")
    assert "catalogByKey" in panel
    assert "option.disabled = !compatible(value)" in panel
    assert "not available for this channel template" in panel


def _fieldset_probe(profile_json, overrides_json="{}"):
    """Render createReferenceOverrideFieldset and report what it built."""
    return _run_chip_dom_script(f"""
        const fieldset = mod.createReferenceOverrideFieldset({{
            profile: {profile_json},
            references: [], semanticUnits: [], setupManifest: {{}},
            overrides: {overrides_json}, selected: "",
        }});
        const described = fieldset.fields.map((field) => {{
            const row = fieldset.row(field);
            // fieldRow builds <label><span title=help>LABEL</span>…</label>,
            // with the visible help span appended last.
            const spans = row.children.filter((c) => c.tagName === "SPAN");
            return {{
                field,
                label: spans[0]?.textContent || "",
                help: spans[0]?.title || "",
                multiple: fieldset.controls.get(field)?.multiple === true,
                tag: fieldset.controls.get(field)?.tagName || "",
            }};
        }});
        console.log(JSON.stringify({{
            fields: fieldset.fields,
            described,
            collected: fieldset.collect(),
        }}));
    """)


_DECLARED_PROFILE = """{
    profile_id: "probe", version: "1", name: "Probe Format",
    capabilities: { reference: { derived: {
        definitions: { order: 1, channel_key: "defs", placement: "section_prefix",
            label: "Portrayal", help: "Declared definition guidance.", fields: {} },
        retention: { order: 3, channel_key: "ret", placement: "section_prefix",
            label: "Retention", help: "Declared retention guidance.", fields: {
                visual_intent: { type: "enum", label: "Look handling",
                    help: "Declared visual guidance.",
                    values: [{ value: "preserve", label: "Keep" }] } } },
    } } },
}"""

_DEFINITIONS_ONLY_PROFILE = """{
    profile_id: "probe-min", version: "1", name: "Minimal Format",
    capabilities: { reference: { derived: {
        definitions: { order: 1, channel_key: "defs", placement: "section_prefix",
            label: "Portrayal", help: "Only capability.", fields: {} },
    } } },
}"""


def test_reference_fieldset_renders_only_declared_fields_with_declared_copy():
    """Field presence, labels, and help come from the format, not the browser.

    Replaces a source grep for one `overridableFieldRow(...)` call shape, which
    passed whether or not the declaration was ever consulted.
    """
    result = _fieldset_probe(_DECLARED_PROFILE)
    by_field = {row["field"]: row for row in result["described"]}

    # Declared capabilities contribute their value fields; undeclared ones
    # (summary, mentions, audio_relationship) contribute nothing at all.
    assert set(result["fields"]) == {
        "definition", "audio_definition", "retention_detail", "visual_intent"}
    assert "summary" not in by_field
    assert "task_types" not in by_field
    assert "text" not in by_field
    assert "audio_relationship" not in by_field

    # A declared field wins on both label and help.
    assert by_field["visual_intent"]["label"] == "Look handling"
    assert by_field["visual_intent"]["help"] == "Declared visual guidance."
    assert by_field["visual_intent"]["tag"] == "SELECT"

    # A floor field with no field declaration keeps the renderer's own name but
    # takes the owning capability's declared help.
    assert by_field["definition"]["label"] == "Definition"
    assert by_field["definition"]["help"] == "Declared definition guidance."
    assert by_field["retention_detail"]["help"] == "Declared retention guidance."

    # No provider vocabulary leaks from the browser into an unrelated format.
    for row in result["described"]:
        assert "MiniMax" not in row["help"]
        assert "<Subject 1>" not in row["help"]


def test_reference_fieldset_gates_enum_fields_on_a_declared_vocabulary():
    """audio_intent is a floor field of `retention` but has no declared values.

    Rendering it as free text would author values the compiler must reject.
    """
    result = _fieldset_probe(_DECLARED_PROFILE)
    assert "visual_intent" in result["fields"]
    assert "audio_intent" not in result["fields"]


def test_reference_fieldset_collect_survives_undeclared_fields():
    """The save reads the control map, so a gated-away field cannot throw.

    A fixed field list here dereferenced controls the gate had never created.
    """
    result = _fieldset_probe(
        _DEFINITIONS_ONLY_PROFILE,
        '{"summary": "stored under a format that no longer declares it"}')
    assert set(result["fields"]) == {"definition", "audio_definition"}
    # An override for an undeclared field is neither rendered nor dropped by
    # collect(): no control means no authored change, so the stored value is
    # preserved rather than silently deleted on the next save.
    assert result["collected"]["summary"] == (
        "stored under a format that no longer declares it")


def test_reference_fieldset_collapses_following_fields_but_never_an_override():
    """Progressive disclosure by tier state, not by category.

    Rendering every declared field as an equal editable row put ~35 controls in
    front of an author whose chip usually deviates in none of them. Hiding an
    actual override would be worse than showing everything, so a deviation
    always stays visible.
    """
    result = _run_chip_dom_script(f"""
        const fieldset = mod.createReferenceOverrideFieldset({{
            profile: {_DECLARED_PROFILE},
            references: [], semanticUnits: [], setupManifest: {{}},
            overrides: {{ retention_detail: "authored here" }}, selected: "",
        }});
        const visible = () => fieldset.fields.filter(
            (field) => fieldset.row(field).style.display !== "none");
        const collapsed = visible();
        const summaryWhenCollapsed = fieldset.summaryRow.children[0].textContent;
        fieldset.setExpanded(true);
        const openedUp = visible();
        fieldset.setExpanded(false);
        // Overriding a second field must pull it out of the collapsed group.
        fieldset.controls.get("definition").value = "now authored";
        const handlers = fieldset.controls.get("definition")._handlers.input || [];
        handlers.forEach((handler) => handler());
        console.log(JSON.stringify({{
            collapsed, openedUp, afterOverriding: visible(),
            summaryWhenCollapsed,
            summaryAfter: fieldset.summaryRow.children[0].textContent,
        }}));
    """)
    # Only the override shows while collapsed.
    assert result["collapsed"] == ["retention_detail"]
    # Expanding shows every declared field.
    assert set(result["openedUp"]) == {
        "definition", "audio_definition", "retention_detail", "visual_intent"}
    # A newly authored field joins the visible set without expanding.
    assert set(result["afterOverriding"]) == {"definition", "retention_detail"}
    assert "3 fields following" in result["summaryWhenCollapsed"]
    assert "2 fields following" in result["summaryAfter"]


def test_collapsed_summary_names_the_most_specific_source_not_a_count():
    """Mixed tiers are the normal case, so a count is the normal output.

    A physical member typically states one or two defaults while the rest fall to
    the format, which made the summary read `N fields following 3 sources` almost
    always — technically true and useless, because it never named the tier the
    author actually authored. Ranking the chain lets it name the most specific
    source in play; `tier` alone cannot, since it calls member, staged, entity
    and identity all "shared".
    """
    profile = _DECLARED_PROFILE.replace(
        "capabilities: { reference: { derived: {",
        'capabilities: { reference: { defaults: { retention_detail: "from format" },'
        " derived: {")
    result = _run_chip_dom_script(f"""
        const references = [{{ reference_id: "ref", name: "Korean Woman",
            members: [{{ member_id: "m1", handle: "Portrait",
                attachment_defaults: {{ definition: "from the member" }} }}] }}];
        const mixed = mod.createReferenceOverrideFieldset({{
            profile: {profile}, references, semanticUnits: [], setupManifest: {{}},
            overrides: {{}}, selected: "physical:picture:m1",
        }});
        // Nothing selected and no declared defaults: every field falls to the
        // one format source, so there is nothing to disambiguate.
        const single = mod.createReferenceOverrideFieldset({{
            profile: {_DECLARED_PROFILE},
            references: [], semanticUnits: [], setupManifest: {{}},
            overrides: {{}}, selected: "",
        }});
        console.log(JSON.stringify({{
            mixed: mixed.summaryRow.children[0].textContent,
            single: single.summaryRow.children[0].textContent,
        }}));
    """)
    # The member tier outranks both format tiers present, so it is named.
    assert result["mixed"].startswith("4 fields following Physical Reference default · @Portrait")
    # The others are counted, not listed — the point is to name one, not all.
    assert "+2 more" in result["mixed"]
    assert "sources" not in result["mixed"]
    # With a single source there is nothing to disambiguate and no suffix.
    assert result["single"] == "4 fields following Prompt Format default · Probe Format"


_CAMERA_AID = """{
    id: "camera_motion", label: "Camera motion",
    text: "The camera {motion} {amplitude} {speed}.",
    fields: {
        motion: { type: "enum", values: ["pushes in", "pans right"] },
        amplitude: { type: "enum", optional: true,
            values: [{ value: "with small amplitude", label: "Small" }] },
        speed: { type: "enum", optional: true,
            values: [{ value: "at slow speed", label: "Slow" }] },
    },
}"""


def test_writing_aid_menu_shape_follows_what_the_aid_still_needs():
    """0 fields insert, 1 bounded choice nests, free text or several opens a dialog."""
    result = _run_chip_dom_script(f"""
        const editor = {{
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            insertText() {{}},
        }};
        const aids = [
            {{ id: "cutoff", label: "Cutoff", text: "<cutoff>" }},
            {{ id: "dialogue", label: "Dialogue", text: "<d>[{{language}}] {{text}}</d>",
              fields: {{ language: {{ type: "enum", values: ["English", "Spanish"] }} }} }},
            {_CAMERA_AID},
            {{ id: "tags", label: "Tags", text: "[{{tags}}]",
              fields: {{ tags: {{ type: "enum_multi", values: ["a", "b"] }} }} }},
        ];
        // A live selection satisfies `{{text}}`, which is what turns Dialogue
        // from a dialog into a one-choice submenu.
        const withSelection = mod.createPromptContextMenuItems({{
            editor, writingAids: aids,
            selection: {{ bookmark: null, text: "Hey, I am over here" }},
        }})[1].submenu;
        const withoutSelection = mod.createPromptContextMenuItems({{
            editor, writingAids: aids,
        }})[1].submenu;
        const shape = (rows) => rows.map((row) => ({{
            label: row.label,
            kind: row.submenu ? "submenu" : "action",
            choices: row.submenu ? row.submenu.map((entry) => entry.label) : null,
            hint: row.hint || "",
        }}));
        console.log(JSON.stringify({{
            withSelection: shape(withSelection),
            withoutSelection: shape(withoutSelection),
        }}));
    """)
    selected = {row["label"]: row for row in result["withSelection"]}
    bare = {row["label"]: row for row in result["withoutSelection"]}

    # Nothing left to ask: inserts on the spot in both cases.
    assert selected["Cutoff"]["kind"] == "action"
    assert bare["Cutoff"]["kind"] == "action"
    # The row previews the text it inserts.
    assert selected["Cutoff"]["hint"] == "<cutoff>"

    # One bounded choice with `{text}` already satisfied nests in the menu...
    assert selected["Dialogue"]["kind"] == "submenu"
    assert selected["Dialogue"]["choices"] == ["English", "Spanish"]
    # ...but with nothing selected it still needs free text, so it opens a dialog.
    assert bare["Dialogue"]["kind"] == "action"

    # Several choices always earn the dialog, selection or not.
    assert selected["Camera motion"]["kind"] == "action"
    assert bare["Camera motion"]["kind"] == "action"
    # A menu row cannot express picking two of five, so enum_multi does too.
    assert selected["Tags"]["kind"] == "action"


def test_single_optional_choice_offers_not_set_in_the_submenu():
    result = _run_chip_dom_script("""
        const editor = {
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            insertText() {},
        };
        const rows = mod.createPromptContextMenuItems({
            editor,
            writingAids: [
                { id: "req", label: "Required", text: "a {v}",
                  fields: { v: { type: "enum", values: ["x"] } } },
                { id: "opt", label: "Optional", text: "a {v}",
                  fields: { v: { type: "enum", optional: true, values: ["x"] } } },
            ],
        })[1].submenu;
        console.log(JSON.stringify(Object.fromEntries(
            rows.map((row) => [row.label, row.submenu.map((e) => e.label)]))));
    """)
    # A declared vocabulary cannot carry an empty value, so "leave it out"
    # belongs to the control and appears only where the format allows omission.
    assert result["Required"] == ["x"]
    assert result["Optional"] == ["— not set —", "x"]


def test_writing_aid_wraps_a_selection_and_closes_up_omitted_fields():
    result = _run_chip_dom_script(f"""
        const inserted = [];
        let restoredWith = "none";
        const editor = {{
            capturePromptSelection: () => null,
            restorePromptSelection(bookmark) {{
                restoredWith = bookmark === null ? "null" : "bookmark";
                return true;
            }},
            insertText(value) {{ inserted.push(value); }},
        }};
        const wrapRows = mod.createPromptContextMenuItems({{
            editor,
            writingAids: [{{ id: "dialogue", label: "Dialogue",
                text: "<d>[{{language}}] {{text}}</d>",
                fields: {{ language: {{ type: "enum", values: ["English"] }} }} }}],
            selection: {{ bookmark: null, text: "Hey, I am over here" }},
        }})[1].submenu[0];
        await wrapRows.submenu.find((e) => e.label === "English").action();

        const cameraRows = mod.createPromptContextMenuItems({{
            editor, writingAids: [{_CAMERA_AID}],
        }})[1].submenu[0];
        const cameraPromise = cameraRows.action();
        const backdrop = document.body.children.at(-1);
        const selects = backdrop.querySelectorAll("select");
        selects[0].value = "pushes in";
        backdrop.querySelectorAll("button")
            .find((b) => b.textContent === "Insert")._handlers.click[0]();
        await cameraPromise;
        console.log(JSON.stringify({{ inserted, restoredWith }}));
    """)
    # The selection becomes the `{text}` value, so the aid wraps the words
    # instead of landing in front of them.
    assert result["inserted"][0] == "<d>[English] Hey, I am over here</d>"
    # Optional fields left unset collapse their whitespace and close up the
    # punctuation rather than emitting "The camera pushes in  ."
    assert result["inserted"][1] == "The camera pushes in."


def test_writing_aid_with_an_undeclared_vocabulary_reports_instead_of_no_op():
    result = _run_chip_dom_script("""
        const calls = [];
        const editor = {
            capturePromptSelection: () => null,
            restorePromptSelection() { calls.push("restore"); return true; },
            insertText() { calls.push("insert"); },
        };
        const row = mod.createPromptContextMenuItems({
            editor,
            writingAids: [{ id: "broken", label: "Broken", text: "a {v}",
                fields: { v: { type: "enum", values: [] } } }],
        })[1].submenu[0];
        await row.action();
        console.log(JSON.stringify({ calls, kind: row.submenu ? "submenu" : "action" }));
    """)
    # This used to return silently without even restoring the caret, so an aid
    # a format could legally save simply did nothing.
    assert result["kind"] == "action"
    assert result["calls"] == ["restore"]
    assert "insert" not in result["calls"]


def test_writing_aids_are_hidden_outside_the_channels_they_declare():
    result = _run_chip_dom_script("""
        const editor = {
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            insertText() {},
        };
        const writingAids = [
            { id: "dialogue", label: "Dialogue", text: "d",
              channel_keys: ["detailed_description"] },
            { id: "anywhere", label: "Anywhere", text: "a" },
        ];
        const row = (channelKey) => {
            const entry = mod.createPromptContextMenuItems({
                editor, writingAids, channelKey })[1];
            return { labels: entry.submenu.map((e) => e.label),
                disabled: Boolean(entry.disabled), hint: entry.hint || "" };
        };
        console.log(JSON.stringify({
            description: row("detailed_description"),
            soundscape: row("overall_soundscape"),
            unfiltered: row(""),
        }));
    """)
    # An aid declaring a channel is offered only there; one declaring none
    # reaches every channel, so a format predating the key is unchanged.
    assert result["description"]["labels"] == ["Dialogue", "Anywhere"]
    assert result["soundscape"]["labels"] == ["Anywhere"]
    # No channel of its own — the Writing draft box — sees the whole set.
    assert result["unfiltered"]["labels"] == ["Dialogue", "Anywhere"]


def test_a_channel_with_no_aids_says_so_instead_of_showing_a_dead_row():
    result = _run_chip_dom_script("""
        const editor = {
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            insertText() {},
        };
        const entry = (writingAids, channelKey) => {
            const row = mod.createPromptContextMenuItems({
                editor, writingAids, channelKey })[1];
            return { disabled: Boolean(row.disabled), hint: row.hint || "" };
        };
        console.log(JSON.stringify({
            filteredOut: entry(
                [{ id: "d", label: "D", text: "d", channel_keys: ["other"] }],
                "overall_soundscape"),
            noneAtAll: entry([], "overall_soundscape"),
        }));
    """)
    # "This channel declares none" and "this format has none" are different
    # facts, and a dead disabled row looks broken for either reason.
    assert result["filteredOut"] == {
        "disabled": True, "hint": "None for overall_soundscape"}
    assert result["noneAtAll"] == {"disabled": True, "hint": ""}


def test_context_kind_is_offered_only_where_the_format_declares_it():
    result = _run_chip_dom_script("""
        const declaring = { capabilities: { custom: { formatter: "{text}" },
            reference: {}, shot: {} } };
        const notDeclaring = { capabilities: { reference: {}, shot: {} } };
        const scopeKinds = (profile) => {
            const row = mod.createScopeChipRow({
                allowedKinds: ["shot", "reference", "custom"], profile });
            return row.querySelectorAll("select")[0].options.map((o) => o.value);
        };
        console.log(JSON.stringify({
            menuDeclaring: mod.promptContextAuthoringKinds(
                ["reference", "custom"], declaring),
            menuNotDeclaring: mod.promptContextAuthoringKinds(
                ["reference", "custom"], notDeclaring),
            menuNoProfile: mod.promptContextAuthoringKinds(["reference", "custom"]),
            scopeDeclaring: scopeKinds(declaring),
            scopeNotDeclaring: scopeKinds(notDeclaring),
        }));
    """)
    # `custom` is the model-agnostic escape hatch, so a format that declares one
    # keeps it and a format that does not never offers it.
    assert result["menuDeclaring"] == ["reference", "custom"]
    assert result["menuNotDeclaring"] == ["reference"]
    # Both authoring routes agree — the caret menu and the scope row.
    assert result["scopeDeclaring"] == ["shot", "reference", "custom"]
    assert result["scopeNotDeclaring"] == ["shot", "reference"]
    # An absent profile is "no opinion", not "declares nothing": the catalog is
    # async and must not silently drop a kind the format really does declare.
    assert result["menuNoProfile"] == ["reference", "custom"]


def test_no_builtin_format_offers_the_context_kind():
    """None of the three declares `capabilities.custom`, so none offers it."""
    from server import prompt_context as pc
    for key, profile in pc.BUILTIN_PROFILES.items():
        assert "custom" not in (profile.get("capabilities") or {}), key


_ATTACH_SCENE = """{
    duration_frames: 100, reference_lane_count: 1,
    reference_lane_configs: [{}],
    _context_consumer_start: 0, _context_consumer_end: 100,
    reference_lane_recipes: [
        { lane_id: "lane0", recipe: { soft: { compatible_profiles: ["generic@1"] } } },
        { lane_id: "lane1", recipe: { soft: { compatible_profiles: ["other@1"] } } },
    ],
    reference_items: [
        { reference_item_id: "hero", lane_index: 0, start_frame: 0, end_frame: 100,
          members: [{ member_id: "m1", entity_id: "e1" }] },
        { reference_item_id: "wrong", lane_index: 1, start_frame: 0, end_frame: 100,
          members: [{ member_id: "m2", entity_id: "e2" }] },
    ],
}"""


def test_reference_row_attaches_a_handle_without_the_dialog():
    result = _run_chip_dom_script(f"""
        const inserted = [];
        const editor = {{
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            insertAttachment(value) {{ inserted.push(value); }},
        }};
        let dialogOpened = 0;
        const row = mod.createPromptContextMenuItems({{
            editor, allowedKinds: ["reference"],
            onCreate: async (a) => {{ dialogOpened += 1; return a; }},
            referenceContext: {{
                scene: {_ATTACH_SCENE},
                references: [{{ reference_id: "e1", name: "Hero" }},
                            {{ reference_id: "e2", name: "Wrong" }}],
                semanticUnits: [], profileId: "generic@1", scope: "section",
                resolvedProfile: {{ profile_id: "generic" }},
            }},
        }})[0].submenu[0];
        const entries = row.submenu.map((e) => ({{
            label: e.label || "", disabled: Boolean(e.disabled),
            separator: e.type === "separator",
        }}));
        await row.submenu.find((e) => (e.label || "").includes("Hero")).action();
        console.log(JSON.stringify({{ entries, inserted, dialogOpened }}));
    """)
    labels = [entry["label"] for entry in result["entries"]]
    # The dialog is one row away, not gone.
    assert labels[0] == "Configure…"
    assert result["dialogOpened"] == 0

    hero = next(e for e in result["entries"] if "Hero" in e["label"])
    wrong = next(e for e in result["entries"] if "Wrong" in e["label"])
    assert not hero["disabled"]
    # Ineligible sources stay visible and dimmed, carrying the dialog's reason.
    assert wrong["disabled"]
    assert "incompatible prompt format" in wrong["label"]

    # The chip is inserted with no overrides, so it FOLLOWS its Reference
    # defaults rather than freezing a copy of them at attach time. This stub
    # profile declares no capabilities at all, so nothing is seeded either —
    # the mention seed is covered by the test below.
    assert len(result["inserted"]) == 1
    chip = result["inserted"][0]
    assert chip["kind"] == "reference"
    assert chip["source"]["reference_item_id"] == "hero"
    assert chip["capabilities"] == []
    assert not chip["config"].get("overrides")


_MENTION_PROFILE = """{
    profile_id: "mentions_format", version: "1",
    capabilities: { reference: { derived: {
        definitions: { order: 1, channel_key: "subject_definitions",
                       placement: "section_prefix", label: "Definition" },
        mentions: { order: 4, channel_key: "detailed_description",
                    placement: "inline", label: "Scene mention" },
    } } },
}"""
_NO_MENTION_PROFILE = """{
    profile_id: "generic", version: "1",
    capabilities: { reference: { derived: {
        derived_prompt: { order: 1, channel_key: "visual",
                          placement: "inline", label: "Reference prompt" },
    } } },
}"""


def test_a_handle_attach_seeds_the_declared_mention_capability():
    """A handle is a MENTION, not a definition block.

    `@KWoman is leaning then @Doggo appears` wants each handle resolved to the
    format's canonical token inside the sentence. Seeding no capability made
    the compiler fall back to the LOWEST-`order` declaration instead, which is
    `definitions` — so a handle emitted a definition line into another channel.
    """
    result = _run_chip_dom_script(f"""
        const inserted = [];
        const editor = {{
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            insertAttachment(value) {{ inserted.push(value); }},
        }};
        const attach = async (resolvedProfile) => {{
            const row = mod.createPromptContextMenuItems({{
                editor, allowedKinds: ["reference"],
                referenceContext: {{
                    scene: {_ATTACH_SCENE},
                    references: [{{ reference_id: "e1", name: "Hero" }}],
                    semanticUnits: [], profileId: "generic@1", scope: "section",
                    resolvedProfile,
                }},
            }})[0].submenu[0];
            await row.submenu.find((e) => (e.label || "").includes("Hero")).action();
            return inserted[inserted.length - 1];
        }};
        const declaring = await attach({_MENTION_PROFILE});
        const notDeclaring = await attach({_NO_MENTION_PROFILE});
        console.log(JSON.stringify({{
            declaring: declaring.capabilities,
            notDeclaring: notDeclaring.capabilities,
        }}));
    """)
    # Exact equality is the sparsity assertion: `capability_id` and `kind` and
    # nothing else. A stored `channel_key`/`placement` would freeze this chip's
    # routing against a later format change, and a stored `enabled` would turn
    # the tri-state into an authored deviation from a default that may be off.
    assert result["declaring"] == [
        {"capability_id": "mentions", "kind": "mentions"}]
    # Declaration-driven, so a format without `mentions` seeds nothing and
    # keeps the compiler's format default — `generic@1` is exactly that case.
    assert result["notDeclaring"] == []


def _mention_seed_compile(capabilities):
    """Compile one handle-attached chip anchored in the description channel."""
    return prompt_context.compile_prompt_context(
        global_channels={},
        sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 24,
            "channel_docs": {"detailed_description": {
                "schema": "prompt_document_v1",
                "nodes": [
                    {"type": "text", "node_id": "before", "text": "Before "},
                    {"type": "attachment", "node_id": "anchor",
                     "attachment_id": "handle-chip"},
                    {"type": "text", "node_id": "after", "text": " leans in."},
                ],
            }},
            "attachments": [prompt_context.normalize_attachment({
                "attachment_id": "handle-chip",
                "emission_group_id": "handle-chip",
                "kind": "reference",
                "source": {"semantic_unit_ids": ["subject"]},
                "capabilities": capabilities,
            })],
        }],
        window_start=0, window_end=24, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": {
                "setup": {"mode": "reference", "setup_id": "setup"},
                "pictures": [{"member_id": "portrait", "role": "identity",
                              "picture_ordinal": 1}],
                "videos": [], "standalone_audios": [],
                "presentation": [{"kind": "picture", "member_id": "portrait",
                                  "picture_ordinal": 1}],
            },
            "ordinal_manifest": {
                "subjects": {"subject": 1},
                "pictures": {"portrait": 1}, "videos": {}, "audios": {},
            },
            "unit_source_labels": {"subject": ["<Picture 1>"]},
            "semantic_units": [{
                "semantic_unit_id": "subject", "name": "KWoman",
                "definition": "a poised woman",
                "sources": [{"entity_id": "woman", "member_id": "portrait"}],
            }],
        },
        labels_on=True,
    )


def test_the_seeded_mention_compiles_to_an_inline_token_not_a_definition():
    seeded = _mention_seed_compile(
        [{"capability_id": "mentions", "kind": "mentions"}])
    # The token lands in the sentence the author was writing, and the sparse
    # record still inherits the declared `detailed_description`/`inline`.
    assert seeded["channels"]["detailed_description"] == (
        "Before <Subject 1> leans in.")
    assert seeded["channels"]["subject_definitions"] == ""
    assert [(row["capability_id"], row["placement"], row["channel_key"])
            for row in seeded["emissions"]] == [
                ("mentions", "inline", "detailed_description")]

    # The same chip WITHOUT the seed is the defect: the compiler's lowest-order
    # fallback emits a definition into another channel and the sentence keeps a
    # hole where the handle was.
    unseeded = _mention_seed_compile([])
    assert unseeded["channels"]["detailed_description"] == "Before  leans in."
    assert unseeded["channels"]["subject_definitions"] == (
        "<Subject 1> is a poised woman from <Picture 1>")


def test_the_attachment_dialog_owns_escape_over_the_panel_that_opened_it():
    """Escape closed the Prompt tool BEHIND this dialog, leaving it open.

    The dialog owned no Escape at all, and `KeyboardOwnership` listens at window
    CAPTURE — so the Prompt tool's own consumer, registered earlier and closing
    unconditionally, took the key and stopped it before any element listener on
    the dialog could run. An element listener is structurally too late here; the
    dialog has to register, which also makes it the newest OVERLAY consumer.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Context JS tests")
    chips_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    keyboard_url = (ROOT / "web" / "js" / "keyboard_ownership.js").as_uri()
    capturing_window = """
globalThis.__windowKeydown = null;
globalThis.window = {
  addEventListener(type, fn) { if (type === "keydown") globalThis.__windowKeydown = fn; },
  removeEventListener(type, fn) {
    if (type === "keydown" && globalThis.__windowKeydown === fn) globalThis.__windowKeydown = null; },
};
"""
    dom = _MINIMAL_DOM.replace(
        "globalThis.window = { addEventListener(){}, removeEventListener(){} };",
        capturing_window)
    assert capturing_window in dom, "minimal DOM window stub moved"
    script = f"""
        {dom}
        const mod = await import({json.dumps(chips_url)});
        const keyboard = await import({json.dumps(keyboard_url)});
        // The surface behind: registered FIRST, and it closes on any Escape.
        let panelClosed = false;
        keyboard.register({{
            id: "panel-behind", priority: keyboard.PRIORITY.OVERLAY,
            keydown: (event) => {{
                if (event.key !== "Escape") return false;
                panelClosed = true;
                return true;
            }},
        }});
        const pending = mod.configurePromptAttachment({{ kind: "shot" }}, {{}});
        const escape = {{ key: "Escape", stopImmediatePropagation() {{}},
                         preventDefault() {{}} }};
        globalThis.__windowKeydown(escape);
        const resolved = await pending;
        console.log(JSON.stringify({{
            panelClosed, resolved,
            consumerIds: keyboard._debugListConsumers().map((value) => value.id),
        }}));
    """
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    # Escape cancels the dialog, exactly as its Cancel button does...
    assert result["resolved"] is None
    # ...and the surface behind never sees the key.
    assert result["panelClosed"] is False
    # The dialog also releases its consumer on close, or the next Escape would
    # be swallowed by a modal that is no longer on screen.
    assert not [value for value in result["consumerIds"]
                if value.startswith("sonder-prompt-modal-")]


def test_reference_row_falls_back_to_the_dialog_without_a_context():
    result = _run_chip_dom_script("""
        const editor = {
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            insertAttachment() {},
        };
        const row = mod.createPromptContextMenuItems({
            editor, allowedKinds: ["reference"],
            onCreate: async (a) => a,
        })[0].submenu[0];
        console.log(JSON.stringify({
            hasSubmenu: Boolean(row.submenu),
            isAction: typeof row.action === "function",
        }));
    """)
    # A surface that cannot describe its window must not guess at eligibility;
    # it offers the dialog, which resolves everything itself.
    assert result == {"hasSubmenu": False, "isAction": True}


def test_an_unresolvable_caret_appends_instead_of_inserting_at_position_zero():
    """A null bookmark must never be treated as "insert at the start"."""
    result = _run_chip_dom_script("""
        const calls = [];
        const editor = {
            capturePromptSelection: () => null,
            restorePromptSelection: () => true,
            focusEnd() { calls.push("focusEnd"); },
            insertText(v) { calls.push("insert:" + v); },
        };
        const row = mod.createPromptContextMenuItems({
            editor, writingAids: [{ id: "c", label: "Cutoff", text: "<cutoff>" }],
            selection: { bookmark: null, text: "" },
        })[1].submenu[0];
        await row.action();
        console.log(JSON.stringify({ calls }));
    """)
    # `insertText` focuses the editor, and a bare focus() collapses to the START
    # of a contenteditable — so an unknown caret used to insert at position 0 and,
    # against a stale offset, split authored prose mid-word. It must land at the
    # end, which can never cut existing text in half.
    assert result["calls"] == ["focusEnd", "insert:<cutoff>"]


def test_a_handle_renders_inline_and_drops_the_chip_preview():
    """A handle must read as one word inside its paragraph.

    Two defects in one element. `contextChipLabel` was `display:-webkit-box` —
    block-level — so a handle mid-clause forced a line break either side of it
    and split the paragraph it belonged to. And it carried the chip preview, so
    the sentence read `@KWoman - <Subject 1> is the korean woman...` where a
    mention should read `@KWoman`. Both were invisible to every style probe and
    only showed when a handle was read inside real prose, which is why this
    asserts the composition: what the handle DISPLAYS, how it lays out, and that
    the descriptive form still reaches the title.
    """
    result = _run_chip_dom_script("""
        const preview = "<Subject 1> is the korean woman with long black hair";
        const chipFor = (identity) => {
            const attachment = { attachment_id: "ref", kind: "reference",
                source: {}, config: {} };
            const editor = mod.createPromptDocumentEditor({
                document: { nodes: [
                    { type: "text", node_id: "before", text: "she has an " },
                    { type: "attachment", node_id: "anchor", attachment_id: "ref" },
                    { type: "text", node_id: "after", text: " editorial style." },
                ] },
                attachments: [attachment],
                previews: { ref: preview },
                attachmentLabelFor: () => identity,
            });
            const chip = editor.children.find((value) =>
                value.dataset.attachmentId === "ref");
            const findLabel = (node) => node.dataset?.sonderContextChipLabel === "1"
                ? node : (node.children || []).map(findLabel).find(Boolean);
            return { chip, label: findLabel(chip) };
        };
        const handle = chipFor("@KWoman");
        const pill = chipFor("Woman");
        const glyphWidthOwners = (node) => (node.children || []).flatMap((child) =>
            child.dataset?.sonderChipEditAffordance === "1"
                ? [node.style.cssText] : glyphWidthOwners(child));
        console.log(JSON.stringify({
            handleText: handle.label.textContent,
            handleLabelCss: handle.label.style.cssText,
            handleChipCss: handle.chip.style.cssText,
            handleTitle: handle.chip.title,
            pillText: pill.label.textContent,
            pillLabelCss: pill.label.style.cssText,
            handleGlyphHost: glyphWidthOwners(handle.chip),
            pillGlyphHost: glyphWidthOwners(pill.chip),
            handleRevealEvents: Object.keys(handle.chip._handlers || {})
                .filter((name) => name.startsWith("mouse") || name.startsWith("focus")),
            pillRevealEvents: Object.keys(pill.chip._handlers || {})
                .filter((name) => name.startsWith("mouse") || name.startsWith("focus")),
        }));
    """)
    # The handle displays its identity ALONE. The preview is chip chrome: a pill
    # has room to describe itself, a sentence does not.
    assert result["handleText"] == "@KWoman"
    assert "<Subject 1>" not in result["handleText"]
    # ...while the descriptive form still reaches the title, where describing
    # the chip is the whole point.
    assert "<Subject 1> is the korean woman" in result["handleTitle"]
    # Inline, and specifically NOT the block-level box that broke the paragraph.
    assert "display:inline;" in result["handleLabelCss"]
    assert "-webkit-box" not in result["handleLabelCss"]
    # The pill is untouched — it shares the element and must keep its clamp.
    assert result["pillText"].startswith("Woman")
    assert "<Subject 1>" in result["pillText"]
    assert "-webkit-line-clamp:2" in result["pillLabelCss"]
    # The edit/remove glyphs hide with `opacity:0`, which keeps layout, so on a
    # handle they left a blank gap after every mention. On a handle they are
    # taken out of flow; on a pill they stay in the capsule's flex row.
    assert result["handleGlyphHost"] and all(
        "position:absolute" in css for css in result["handleGlyphHost"])
    assert result["pillGlyphHost"] and not any(
        "position:absolute" in css for css in result["pillGlyphHost"])
    # Out of flow, those controls sit ON TOP of the words after the mention.
    # Revealing them on hover therefore put a live Remove button over prose the
    # author was about to click into — measured 14-18px past the handle in the
    # real browser, exactly where a caret click lands. A handle reveals on FOCUS
    # only, so a mouse moving toward the next word never arms a delete.
    assert result["handleRevealEvents"] == ["focusin", "focusout"]
    # The pill needs no reveal at all: its controls sit inside the capsule, in
    # flow, always visible. Asserted so that "hide the pill's controls too"
    # cannot be adopted quietly as a symmetry that was never there.
    assert result["pillRevealEvents"] == []


def test_the_writing_section_separator_round_trips_without_growing():
    """Apply must not add a blank line to every section each time it runs.

    `joinWritingSectionDocuments` writes the break as "\n---\n", but by the
    time `splitWritingPromptDocument` reads the "---" line the preceding line
    has already contributed its own "\n" suffix, so the separator's LEADING
    newline lands in the block being closed and is re-emitted with a fresh
    separator next time. Stored channels were measured holding 13 and 14.

    This drives the real pair over five cycles rather than checking either half:
    the growth is a disagreement BETWEEN them, so testing them apart cannot see
    it. An odd measured count is also why the channel joiner was ruled out —
    its `\n\n` is trimmed whole, and can only move a count by two.
    """
    result = _run_chip_dom_script(r"""
        const keys = ["summary", "detailed_description"];
        const doc = (text) => ({ nodes: [{ type: "text", node_id: "n", text }] });
        let sections = [
            { channel_docs: { summary: doc("alpha"),
                // An AUTHORED blank line before the break. An over-eager trim
                // would eat this, which is the opposite failure.
                detailed_description: doc("says one\n") } },
            { channel_docs: { summary: doc("beta"),
                detailed_description: doc("says two") } },
            { channel_docs: { summary: doc("gamma"),
                detailed_description: doc("says three") } },
        ];
        const cycles = [];
        for (let pass = 0; pass < 5; pass += 1) {
            const joined = mod.joinWritingSectionDocuments(sections, keys);
            const blocks = mod.splitWritingPromptDocument(joined, { keepEmpty: true });
            sections = blocks.map((block) => ({
                channel_docs: mod.splitPromptDocumentChannels(block, keys) }));
            cycles.push(sections.map((section) => keys.map((key) =>
                mod.promptDocumentText(section.channel_docs[key]))));
        }
        console.log(JSON.stringify({ first: cycles[0], last: cycles.at(-1),
            stable: cycles.every((pass) =>
                JSON.stringify(pass) === JSON.stringify(cycles[0])) }));
    """)
    # Idempotent from the FIRST cycle, not merely converging later.
    assert result["stable"], result["cycles"] if "cycles" in result else result
    assert result["first"] == result["last"]
    # The authored blank line survives every pass...
    assert result["last"][0][1] == "says one\n"
    # ...and no section acquired one it did not have. The last block never grew
    # even before the fix, so it is the control, not the evidence.
    assert result["last"][1][1] == "says two"
    assert result["last"][2][1] == "says three"
    assert all(text in ("alpha", "beta", "gamma")
               for text in [row[0] for row in result["last"]])


def test_reset_heals_separator_padding_a_project_already_accumulated():
    """Stopping the growth leaves the damage already on disk.

    The separator fix removes only the newline it just added, so a channel
    holding thirteen keeps thirteen forever. `healSeparatorPadding` is the
    one-time repair, applied where the draft is rebuilt from sections and the
    Reset stash makes it recoverable. It keeps ONE trailing newline, because a
    single authored blank line is indistinguishable from one unit of damage.
    """
    result = _run_chip_dom_script(r"""
        const text = (value) => mod.promptDocumentText(
            mod.healSeparatorPadding(value));
        const doc = (value) => ({ nodes: [{ type: "text", node_id: "n", text: value }] });
        const shape = (value) => mod.healSeparatorPadding(value).nodes
            .map((node) => node.type === "text"
                ? "[" + node.text.replaceAll("\n", "NL") + "]"
                : "<" + node.attachment_id + ">").join("|");
        console.log(JSON.stringify({
            damaged: text(doc("says two\n\n\n\n\n\n\n")),
            authored: text(doc("says one\n")),
            clean: text(doc("says one")),
            // Padding split across nodes is the shape the joiner actually
            // produces, so the walk must cross a node boundary.
            split: text({ nodes: [
                { type: "text", node_id: "a", text: "says two\n" },
                { type: "text", node_id: "b", text: "\n\n\n" } ] }),
            // A trailing anchor ends the run: a chip is not padding, and
            // walking past it would strip prose that precedes it.
            anchorKinds: shape({ nodes: [
                { type: "text", node_id: "a", text: "sees " },
                { type: "attachment", node_id: "b", attachment_id: "chip" } ] }),
            // Padding AFTER an anchor. The kept newline has to land at the end
            // of the document; appending it to the last TEXT node puts it on
            // the wrong side of the chip and pushes the mention onto its own
            // line, which is the defect inline handles exist to prevent.
            afterAnchor: shape({ nodes: [
                { type: "text", node_id: "a", text: "she looks like " },
                { type: "attachment", node_id: "b", attachment_id: "chip" },
                { type: "text", node_id: "c", text: "\n\n\n\n" } ] }),
            // The empty text node between two adjacent chips is the caret slot
            // the renderer materializes. Dropping empties document-wide instead
            // of only in the run just walked removes the author's ability to
            // type between them.
            caretSlot: shape({ nodes: [
                { type: "attachment", node_id: "a", attachment_id: "one" },
                { type: "text", node_id: "b", text: "" },
                { type: "attachment", node_id: "c", attachment_id: "two" },
                { type: "text", node_id: "d", text: "tail\n\n\n" } ] }),
        }));
    """)
    assert result["damaged"] == "says two\n"
    assert result["split"] == "says two\n"
    # Untouched where there is nothing to heal — the heal must not be a trim.
    assert result["authored"] == "says one\n"
    assert result["clean"] == "says one"
    assert result["anchorKinds"] == "[sees ]|<chip>"
    assert result["afterAnchor"] == "[she looks like ]|<chip>|[NL]"
    assert result["caretSlot"] == "<one>|[]|<two>|[tailNL]"


def test_a_decoration_never_enters_the_document_and_never_eats_a_keystroke():
    """The four ways host chrome inside a contenteditable can destroy authoring.

    Decorations are read-only prose the host paints BETWEEN model nodes. They
    are not document content, and `readDom` rebuilds the model from the DOM on
    every keystroke, so each of these is a silent-corruption path rather than a
    cosmetic one:

    1. `readDom`'s else-branch turns any unrecognised direct child into a text
       node from its `textContent` — a whole rendered contribution injected into
       the author's prose.
    2. It walks DIRECT children only, so a decoration nested inside a text span
       is absorbed into that span's text instead.
    3. Backspace/Delete reach a neighbouring chip by DOM adjacency, and a
       decoration between them makes the chip undeletable from the keyboard.
    4. A caret beside a decoration must still resolve to a model node; a null
       bookmark makes `render()` drop the caret, which the bug tracker records
       producing text inserted at position 0, inside chip spans.
    """
    result = _run_chip_dom_script("""
        const attachment = { attachment_id: "ref", kind: "reference",
            source: {}, config: {} };
        const decoration = () => {
            const box = document.createElement("div");
            box.textContent = "<Subject 1> is the korean woman";
            return box;
        };
        const editor = mod.createPromptDocumentEditor({
            document: { nodes: [
                { type: "text", node_id: "t1", text: "she wears " },
                { type: "attachment", node_id: "a1", attachment_id: "ref" },
                { type: "text", node_id: "t2", text: " in the shot." },
            ] },
            attachments: [attachment],
            attachmentLabelFor: () => "@KWoman",
            decorations: () => [{ afterIndex: 0, element: decoration() }],
        });
        const painted = editor.children.filter((child) =>
            child.dataset.sonderDecoration === "1");

        // 1. A top-level decoration must be invisible to the model rebuild.
        editor._handlers.input?.[0]?.();
        const afterTopLevel = mod.promptDocumentText(editor.promptDocument);

        // 2. Nest one INSIDE a text span, which editing can do, and rebuild.
        const span = editor.children.find((child) => child.dataset.nodeId === "t2");
        const nested = decoration();
        nested.dataset.sonderDecoration = "1";
        span.appendChild(nested);
        editor._handlers.input?.[0]?.();
        const afterNested = mod.promptDocumentText(editor.promptDocument);

        console.log(JSON.stringify({
            paintedCount: painted.length,
            paintedIsAtomic: painted[0]?.contentEditable,
            paintedTabIndex: painted[0]?.tabIndex,
            paintedRefusesPointer: !!painted[0]?._handlers?.mousedown?.length,
            afterTopLevel,
            afterNested,
            // 3. Adjacency: the chip must still be reachable across a decoration.
            decorationSitsBetween: editor.children
                .map((c) => c.dataset.sonderDecoration === "1" ? "D"
                    : (c.dataset.nodeType === "attachment" ? "C" : "T")).join(""),
        }));
    """)
    # Painted, atomic, unfocusable, and refusing the click that would put a
    # caret inside it.
    assert result["paintedCount"] == 1
    assert result["paintedIsAtomic"] == "false"
    assert result["paintedTabIndex"] == -1
    assert result["paintedRefusesPointer"] is True
    # 1 + 2: the document is exactly the author's prose, both times. The chip
    # contributes no text, so this is the whole document.
    assert result["afterTopLevel"] == "she wears  in the shot."
    assert result["afterNested"] == "she wears  in the shot."
    assert "<Subject 1>" not in result["afterTopLevel"]
    assert "<Subject 1>" not in result["afterNested"]
    # 3: the decoration really is interposed between a text span and the chip,
    # which is the arrangement that broke Backspace.
    assert result["decorationSitsBetween"] == "TDCT"


def test_decorations_refresh_without_rebuilding_the_draft():
    """A landed candidate must not rebuild the editor under the caret.

    The compiled candidate arrives on a debounce while the author is still
    typing. Repainting through the panel's `render()` would recreate the draft
    element, destroying the caret and this editor's closure-local undo history
    — the trap the durable rules already document. `refreshDecorations` touches
    only `[data-sonder-decoration]`.
    """
    result = _run_chip_dom_script("""
        let generation = 0;
        const editor = mod.createPromptDocumentEditor({
            document: { nodes: [
                { type: "text", node_id: "t1", text: "alpha" },
                { type: "text", node_id: "t2", text: "beta" },
            ] },
            decorations: () => {
                generation += 1;
                const box = document.createElement("div");
                box.textContent = "generation " + generation;
                return [{ afterIndex: 1, element: box }];
            },
        });
        const identity = () => editor.children
            .filter((child) => child.dataset.nodeId)
            .map((child) => child.dataset.nodeId + ":" + child.textContent);
        const before = identity();
        const beforeText = editor.children.find((c) =>
            c.dataset.sonderDecoration === "1")?.textContent;
        editor.refreshDecorations();
        const after = identity();
        const afterText = editor.children.find((c) =>
            c.dataset.sonderDecoration === "1")?.textContent;
        console.log(JSON.stringify({
            before, after, beforeText, afterText,
            decorationCount: editor.children
                .filter((c) => c.dataset.sonderDecoration === "1").length,
            order: editor.children.map((c) =>
                c.dataset.sonderDecoration === "1" ? "D" : c.dataset.nodeId).join(","),
        }));
    """)
    # The model spans are the SAME elements with the same ids and text: nothing
    # was rebuilt, so a caret inside them would have survived.
    assert result["before"] == result["after"]
    # The decoration itself did re-render, which is the point.
    assert result["beforeText"] == "generation 1"
    assert result["afterText"] == "generation 2"
    # Exactly one — a refresh that appended instead of replacing would stack
    # a new copy on every debounced keystroke.
    assert result["decorationCount"] == 1
    # And it is re-inserted at its region, not swept to the end of the draft.
    assert result["order"] == "t1,t2,D"


def test_channel_regions_are_reported_per_block():
    """Decorations need the last node of each channel IN EACH BLOCK.

    `splitPromptDocumentChannels` cannot answer this — it mints a fresh id for
    every text node it emits — and a single index per channel would pile every
    block's contributions onto the last block, which is the failure this shape
    exists to prevent.
    """
    result = _run_chip_dom_script("""
        const t = (id, text) => ({ type: "text", node_id: id, text });
        const doc = { nodes: [
            t("h1", "summary:" + String.fromCharCode(10)), t("b1", "alpha"),
            t("br", String.fromCharCode(10) + "---" + String.fromCharCode(10)),
            t("h2", "summary:" + String.fromCharCode(10)), t("b2", "beta"),
            t("h3", "detailed_description:" + String.fromCharCode(10)), t("b3", "says two"),
        ] };
        console.log(JSON.stringify({ regions: mod.channelRegionsByNode(
            doc, ["summary", "detailed_description"],
            { defaultKey: "detailed_description" }) }));
    """)
    assert result["regions"] == [
        {"block": 0, "channelKey": "summary", "afterIndex": 1},
        {"block": 1, "channelKey": "summary", "afterIndex": 4},
        {"block": 1, "channelKey": "detailed_description", "afterIndex": 6},
    ]


def test_a_decoration_does_not_make_a_chip_undeletable():
    """Guard 3, exercised through Backspace rather than through DOM shape.

    Backspace at the start of a text run deletes the chip before it, found by
    `previousElementSibling`. A decoration painted between them is not a
    neighbour in the model but IS one in the DOM, so without `adjacentModelElement`
    stepping over it the chip becomes undeletable from the keyboard and the
    browser default runs instead — silently eating a character somewhere else.

    Asserting the DOM arrangement instead of the keystroke is what let this go
    untested the first time: the arrangement is what `render()` produced, not
    what the guard does with it.
    """
    result = _run_chip_dom_script("""
        const attachment = { attachment_id: "ref", kind: "reference",
            source: {}, config: {} };
        const editor = mod.createPromptDocumentEditor({
            document: { nodes: [
                { type: "attachment", node_id: "a1", attachment_id: "ref" },
                { type: "text", node_id: "t1", text: "follows." },
            ] },
            attachments: [attachment],
            attachmentLabelFor: () => "@KWoman",
            decorations: () => {
                const box = document.createElement("div");
                box.textContent = "contribution";
                return [{ afterIndex: 0, element: box }];
            },
        });
        const order = editor.children.map((c) => c.dataset.sonderDecoration === "1"
            ? "D" : (c.dataset.nodeType === "attachment" ? "C" : "T")).join("");
        const span = editor.children.find((c) => c.dataset.nodeId === "t1");
        // A caret at the very start of the text run, which is the position from
        // which Backspace is supposed to reach the chip.
        globalThis.getSelection = () => ({
            rangeCount: 1,
            getRangeAt: () => ({ startContainer: span, startOffset: 0,
                collapsed: true, endContainer: span, endOffset: 0 }),
            removeAllRanges() {}, addRange() {},
        });
        let defaultPrevented = false;
        editor._handlers.keydown[0]({
            key: "Backspace", target: span,
            preventDefault() { defaultPrevented = true; },
            stopPropagation() {}, stopImmediatePropagation() {},
        });
        console.log(JSON.stringify({
            order,
            defaultPrevented,
            anchorsAfter: mod.normalizePromptDocument(editor.promptDocument).nodes
                .filter((n) => n.type === "attachment").map((n) => n.attachment_id),
        }));
    """)
    # The decoration really is interposed between the chip and the text.
    assert result["order"] == "CDT"
    # The editor claimed the keystroke rather than letting the browser default
    # run, and the chip is gone from the document.
    assert result["defaultPrevented"] is True
    assert result["anchorsAfter"] == []


def test_a_caret_beside_a_decoration_still_resolves_to_a_model_node():
    """Guard 4, the one with a recorded prior incident.

    `selectionBoundary` needs a `[data-node-id]` neighbour. A caret resting
    against a decoration has a decoration on one side, and if the walk does not
    step over it the bookmark comes back null — which makes `render()` drop the
    caret. The bug tracker records what that produced last time: text inserted
    at position 0, landing inside chip spans.

    Both directions are covered because the walk is directional: a decoration
    before the caret and one after it fail independently.
    """
    result = _run_chip_dom_script("""
        const build = (afterIndex) => {
            const editor = mod.createPromptDocumentEditor({
                document: { nodes: [
                    { type: "text", node_id: "t1", text: "alpha" },
                    { type: "text", node_id: "t2", text: "beta" },
                ] },
                decorations: () => {
                    const box = document.createElement("div");
                    box.textContent = "contribution";
                    return [{ afterIndex, element: box }];
                },
            });
            return editor;
        };
        const probe = (editor, childOffset) => {
            globalThis.getSelection = () => ({
                rangeCount: 1,
                getRangeAt: () => ({ startContainer: editor, startOffset: childOffset,
                    collapsed: true, endContainer: editor, endOffset: childOffset }),
                removeAllRanges() {}, addRange() {},
            });
            return editor.capturePromptSelection();
        };
        // Decoration after node 0 -> children are [t1, D, t2]. A caret at child
        // offset 1 has the decoration immediately AFTER it.
        const editor = build(0);
        const shape = editor.children.map((c) =>
            c.dataset.sonderDecoration === "1" ? "D" : c.dataset.nodeId).join(",");
        // A caret at the very END of a draft that ENDS in a decoration is the
        // isolating case: scanning forward runs off the end, so the backward
        // scan is the only chance, and it lands on the decoration. Probing
        // anywhere with a model node on one side is rescued by the other
        // direction and proves nothing about the guard.
        const trailing = build(1);
        const trailingShape = trailing.children.map((c) =>
            c.dataset.sonderDecoration === "1" ? "D" : c.dataset.nodeId).join(",");
        console.log(JSON.stringify({
            shape,
            trailingShape,
            forward: probe(editor, 1),
            atEndAfterDecoration: probe(trailing, trailing.children.length),
        }));
    """)
    assert result["shape"] == "t1,D,t2"
    assert result["trailingShape"] == "t1,t2,D"
    # Neither position may come back null, and each must name a REAL model node.
    assert result["forward"] is not None
    assert result["forward"]["start"]["node_id"] in ("t1", "t2")
    assert result["atEndAfterDecoration"] is not None
    assert result["atEndAfterDecoration"]["start"]["node_id"] == "t2"


def test_refresh_places_decorations_exactly_where_render_does():
    """The two paint paths must agree, or a block moves when a compile lands.

    `render()` walks model nodes; `refreshDecorations` walks DOM children. Those
    lists diverge whenever a model node emits no element, and two blocks sharing
    one index reverse if each is inserted against the same anchor. Either way
    the author sees a contribution jump on a keystroke that changed nothing.
    """
    result = _run_chip_dom_script("""
        const label = (editor) => editor.children.map((c) =>
            c.dataset.sonderDecoration === "1" ? "[" + c.textContent + "]"
                : c.dataset.nodeId).join(",");
        // Two blocks sharing one index — the hand-edited-draft case the region
        // walk explicitly warns about.
        const make = () => {
            const box = (text) => { const d = document.createElement("div");
                d.textContent = text; return d; };
            return [{ afterIndex: 0, element: box("first") },
                    { afterIndex: 0, element: box("second") }];
        };
        const editor = mod.createPromptDocumentEditor({
            document: { nodes: [
                { type: "text", node_id: "t1", text: "alpha" },
                { type: "text", node_id: "t2", text: "beta" },
            ] },
            decorations: make,
        });
        const rendered = label(editor);
        editor.refreshDecorations();
        const refreshed = label(editor);
        console.log(JSON.stringify({ rendered, refreshed }));
    """)
    assert result["rendered"] == "t1,[first],[second],t2"
    # The reversal this catches was reproducible: inserting both against the
    # same anchor puts the second one first.
    assert result["refreshed"] == result["rendered"]


def test_a_channel_contribution_resolves_its_text_label_and_placement():
    """One answer for two surfaces, pinned on the three ways the copy got it wrong.

    Writing mode re-derived these facts from a narrower source than the pill
    strip did, and produced three visible defects in one block:

    1. Marker text. A Shot has NO `attachment_channel_previews` entry — its text
       is only on the projection row — so reading previews printed the row's
       `state_reason` ("composed by the prompt section composer") where the
       author should have seen `[Shot 1] At 00:00.000,`.
    2. Labels. Anything without a Reference identity fell back to the literal
       "Reference", so a Shot announced itself as a Reference.
    3. Inline. It decided placement from the published route table, but a
       DISABLED inline capability publishes no route at all, so it read as
       "show it" and appeared under a heading it can never render in.
       `rendered_at_anchor` is the authority.
    """
    result = _run_chip_dom_script("""
        const attachments = [
            { attachment_id: "shot-1", kind: "shot", source: {}, config: {} },
            { attachment_id: "ref-1", kind: "reference", source: {}, config: {} },
            { attachment_id: "inline-1", kind: "reference", source: {}, config: {} },
        ];
        const candidate = {
            // Deliberately EMPTY: a marker contributes nothing here, and reading
            // this map is what produced the excuse instead of the marker.
            attachment_channel_previews: {},
            attachment_capability_projections: [
                { attachment_id: "shot-1", capability_id: "shot",
                  channel_key: "detailed_description", state: "marker",
                  text: "[Shot 1] At 00:00.000,",
                  state_reason: "This marker is composed by the prompt section composer.",
                  rendered_at_anchor: false, effective_phase: "section_prefix" },
                { attachment_id: "ref-1", capability_id: "definitions",
                  channel_key: "detailed_description", state: "emitted",
                  text: "<Subject 1> is the korean woman",
                  rendered_at_anchor: false, effective_phase: "section_prefix" },
                { attachment_id: "inline-1", capability_id: "mentions",
                  channel_key: "detailed_description", state: "emitted",
                  text: "<Subject 1>", rendered_at_anchor: true,
                  effective_phase: "inline" },
            ],
        };
        const rows = mod.channelContributionRows({
            channelKey: "detailed_description", attachments, candidate,
            attachmentLabelFor: (a) => a.attachment_id === "ref-1" ? "@KWoman" : "",
        });
        console.log(JSON.stringify(rows.map((r) => ({
            id: r.attachmentId, label: r.label, text: r.text,
            resolved: r.resolved, emitting: r.emitting, atAnchor: r.atAnchor,
        }))));
    """)
    byId = {row["id"]: row for row in result}
    # 1. The marker's own text, not its reason.
    assert byId["shot-1"]["resolved"] == "[Shot 1] At 00:00.000,"
    assert "composer" not in byId["shot-1"]["resolved"]
    assert byId["shot-1"]["emitting"] is True
    # 2. A Shot is labelled a Shot. A Reference with an identity keeps it.
    assert byId["shot-1"]["label"] != "Reference"
    assert byId["ref-1"]["label"] == "@KWoman"
    # 3. Inline is reported so the caller can drop it, and it is decided by
    # `rendered_at_anchor` rather than by any route table.
    assert byId["inline-1"]["atAnchor"] is True
    assert byId["ref-1"]["atAnchor"] is False


def test_a_contribution_that_resolves_to_nothing_still_reports_itself():
    """Silence must be stated, not implied by absence.

    A capability that routes to a channel and produces no text is a different
    fact from a Reference that does not apply there at all. Omitting it reads as
    the second when it is the first.
    """
    result = _run_chip_dom_script("""
        const rows = mod.channelContributionRows({
            channelKey: "summary",
            attachments: [{ attachment_id: "ref-1", kind: "reference", source: {}, config: {} }],
            candidate: { attachment_capability_projections: [
                { attachment_id: "ref-1", capability_id: "summary",
                  channel_key: "summary", state: "empty", text: "",
                  state_reason: "This capability is disabled and contributes no text.",
                  rendered_at_anchor: false }] },
            attachmentLabelFor: () => "@KWoman",
        });
        console.log(JSON.stringify(rows.map((r) => ({
            emitting: r.emitting, text: r.text, resolved: r.resolved }))));
    """)
    assert len(result) == 1
    assert result[0]["emitting"] is False
    assert result[0]["text"] == ""
    assert "disabled" in result[0]["resolved"]


def test_a_break_inside_a_text_node_collapses_two_blocks_onto_one_anchor():
    """Why Split must write the `---` as its own node.

    Decoration placement is node-granular. When the break lives INSIDE a text
    node, the blocks either side of it report the same index, and both blocks'
    contributions render stacked under the second one — measured in a real
    project as `{block:1, afterIndex:6}` and `{block:2, afterIndex:6}`. When the
    break is its own node, each block anchors separately.

    `joinWritingSectionDocuments` already emits the separator standalone, so
    reconstruction was always fine; only authoring produced the merged shape,
    because `insertText` grows the surrounding node by design to keep the
    caret's bookmarked id alive.

    This pins the READER's behaviour over both document shapes. Driving the
    editor's caret to produce them is not expressible in this DOM stub — that
    half is a manual row.
    """
    result = _run_chip_dom_script(r"""
        const NL = String.fromCharCode(10);
        const keys = ["detailed_description"];
        const shape = (nodes) => mod.channelRegionsByNode({ nodes }, keys,
            { defaultKey: "detailed_description" }).map((r) => r.block + ":" + r.afterIndex);
        console.log(JSON.stringify({
            // The break merged into the surrounding prose, as growing produced.
            merged: shape([
                { type: "text", node_id: "a", text: "alpha" + NL + "---" + NL + "beta" },
            ]),
            // The break standing alone, as the reconstruction has always done.
            standalone: shape([
                { type: "text", node_id: "a", text: "alpha" },
                { type: "text", node_id: "b", text: NL + "---" + NL },
                { type: "text", node_id: "c", text: "beta" },
            ]),
        }));
    """)
    merged, standalone = result["merged"], result["standalone"]
    # Two blocks either way...
    assert len(merged) == 2 and len(standalone) == 2, result
    # ...but merged puts both on the SAME node, which is the stacking defect.
    assert merged[0].split(":")[1] == merged[1].split(":")[1], merged
    # Standalone gives each its own anchor.
    assert standalone[0].split(":")[1] != standalone[1].split(":")[1], standalone


def test_split_here_writes_the_break_as_its_own_node():
    """The writer half of the rule above.

    Source-level because the caret path is not drivable here: what matters is
    that Split asks for `asOwnNode`, since the default — growing the node — is
    exactly what produced the merged shape.
    """
    root = Path(__file__).resolve().parents[1]
    panel = (root / "web" / "js" / "editor_prompt_panel.js").read_text(
        encoding="utf-8")
    chips = (root / "web" / "js" / "prompt_context_chips.js").read_text(
        encoding="utf-8")
    assert "{ asOwnNode: true }" in panel
    assert "asOwnNode = false" in chips
    # Growing must remain the DEFAULT: ordinary typing depends on it to keep the
    # id the caret is bookmarked against alive across the re-render.
    assert "asOwnNode && target && model.nodes[target.index]?.type" in chips
