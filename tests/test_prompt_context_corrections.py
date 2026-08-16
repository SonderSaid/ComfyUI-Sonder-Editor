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
    this.textContent=""; this.title=""; this.disabled=false; this.multiple=false;
    this._handlers={}; }
  appendChild(c){ this.children.push(c); c.parentElement=this;
    if (c.tagName === "OPTION") this.options.push(c); return c; }
  append(...cs){ for (const c of cs) if (c && c.tagName) this.appendChild(c); }
  addEventListener(t,h){ (this._handlers[t] ||= []).push(h); }
  removeEventListener(){}
  setAttribute(k,v){ this.attributes[k]=String(v); }
  getAttribute(k){ return this.attributes[k] ?? null; }
  querySelector(s){ return this.querySelectorAll(s)[0] || null; }
  querySelectorAll(sel){
    const want = String(sel).toUpperCase();
    const out = [];
    const walk = (n) => { for (const c of n.children) {
      if (c.tagName === want) out.push(c); walk(c); } };
    walk(this); return out; }
  contains(n){ if (n === this) return true;
    return this.children.some((c) => c.contains && c.contains(n)); }
  get selectedOptions(){ return this.options.filter((o) =>
    o.selected === true || (!this.multiple
      && String(o.value) === String(this.value))); }
  closest(){ return null; }
  focus(){}
  remove(){}
}
globalThis.document = {
  createElement: (t) => new N(t),
  createTextNode: (t) => ({ nodeType: 3, nodeValue: t }),
  body: new N("body"), activeElement: null,
};
globalThis.window = { addEventListener(){}, removeEventListener(){} };
globalThis.Node = { TEXT_NODE: 3 };
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
        const channelSelect = selects.find((select) => select.multiple === true);
        const attach = modal.querySelectorAll("button")
            .find((button) => button.textContent === "Attach");
        attach._handlers.click[0]();
        const configured = await configuredPromise;
        console.log(JSON.stringify({
            exportable: attachment.link_exportable,
            actions: [removed, copied],
            multiple: channelSelect.multiple,
            channelKeys: configured.source.channel_keys,
            sourcePrompt: configured.source.prompt_id,
        }));
    """)
    assert result == {
        "exportable": True,
        "actions": [1, 1],
        "multiple": True,
        "channelKeys": ["visual", "audio"],
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
        globalThis.prompt = () => null;
        await aidItems[1].submenu.find((item) => item.writingAidId === "cancel_aid").action();

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
    assert result["realBookmark"]["start"]["node_id"]
    assert result["realBookmark"]["start"]["node_id"] == result["realBookmark"]["end"]["node_id"]
    assert result["realBookmark"]["start"]["offset"] == 3
    assert result["realBookmark"]["end"]["offset"] == 3
    assert result["realRestored"] is True
    assert result["detachedRestored"] is False
    assert result["realRestoredOffset"] == 3
    assert result["focusCount"] == 1
    assert result["exposesBookmarkContract"] is True


def test_custom_guide_binding_follows_template_not_profile_id_spelling():
    result = _run_chip_dom_script("""
        const hasGuideBinding = () => document.body.querySelectorAll("select")
            .some((select) => select.options.some((option) => option.value === "first"));
        mod.configurePromptAttachment({ kind: "custom" }, {
                profileId: "forked_profile@1",
                profile: { validators: ["minimax_reference_setup"] } });
        const forkedH3 = hasGuideBinding();
        document.body.children = [];
        mod.configurePromptAttachment({ kind: "custom" }, {
                profileId: "minimax_h3_misleading@1", profile: { validators: [] } });
        console.log(JSON.stringify({ forkedH3, misleadingGeneric: hasGuideBinding() }));
    """)
    assert result == {"forkedH3": True, "misleadingGeneric": False}

    panel = (ROOT / "web" / "js" / "editor_prompt_panel.js").read_text(encoding="utf-8")
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    assert panel.count("profile: currentPromptProfile()") >= 7
    assert widget.count("profile: this._resolvedPromptContextProfile") >= 3


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
    assert "audio_relationship: controls.audioRelationship.value" in chips


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
