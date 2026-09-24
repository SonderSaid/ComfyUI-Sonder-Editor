"""H3 Summary task types as checkboxes that never freeze an automatic choice.

The compiler publishes a response-only preview of what staged roles imply and
what the scene-wide Summary prefix is; the identity editor, the Attach dialog
and the chip editor show those as checks without writing them. Only Customize
and Reset change what is stored, and an untouched control saves exactly what it
loaded.
"""

import json

import pytest

from server import prompt_context, routes
from test_minimax_h3_format_coverage import (
    _compile_cross_scope_summary,
    _identity_dormancy_fixture,
    _reference_chip,
)
from test_prompt_context_corrections import ROOT, _run_chip_dom_script


BOTH_ROLES = ["reference generation", "audio reference"]


# --- Compiler preview ------------------------------------------------------

def test_preview_derives_roles_when_no_chip_states_a_choice():
    # A stored empty list is "no explicit choice": roles decide.
    compiled = _compile_cross_scope_summary([], None)
    assert compiled["h3_task_type_preview"] == {
        "role_derived": BOTH_ROLES,
        "scene_effective": BOTH_ROLES,
        "scene_source": "roles",
    }


def test_preview_reports_a_peer_chip_selection_without_moving_role_checks():
    compiled = _compile_cross_scope_summary(["video editing"], None)
    preview = compiled["h3_task_type_preview"]
    assert preview["role_derived"] == BOTH_ROLES
    assert preview["scene_effective"] == ["video editing"]
    assert preview["scene_source"] == "explicit"
    assert "[video editing]" in compiled["prompt"]


def test_preview_unions_explicit_chips_in_canonical_order():
    compiled = _compile_cross_scope_summary(
        ["audio reference"], ["keyframe completion"])
    assert compiled["h3_task_type_preview"]["scene_effective"] == [
        "keyframe completion", "audio reference"]


def test_dormant_chip_selection_does_not_reach_the_scene_preview():
    dormant = _reference_chip(
        "dormant-task", {"semantic_unit_ids": ["dormant"]},
        {"task_types": ["video editing"]}, capabilities=("summary",))
    live = _reference_chip(
        "live-task", {"semantic_unit_ids": ["staged"]}, {},
        capabilities=("summary",))
    preview = _identity_dormancy_fixture(
        global_chips=[dormant, live])["h3_task_type_preview"]
    assert preview["scene_source"] == "roles"
    assert preview["scene_effective"] == preview["role_derived"]
    assert "video editing" not in preview["scene_effective"]


def test_unsupported_only_selection_is_not_credited_with_the_prefix():
    compiled = _compile_cross_scope_summary(["future task"], None)
    preview = compiled["h3_task_type_preview"]
    assert preview["scene_source"] == "roles"
    assert preview["scene_effective"] == BOTH_ROLES


def test_no_live_summary_reports_no_scene_task_types():
    dormant = _reference_chip(
        "dormant-only", {"semantic_unit_ids": ["dormant"]},
        {"task_types": ["video editing"]}, capabilities=("summary",))
    compiled = _identity_dormancy_fixture(global_chips=[dormant])
    preview = compiled["h3_task_type_preview"]
    assert preview["scene_source"] == "none"
    assert preview["scene_effective"] == []
    assert "video editing" not in compiled["prompt"]


def test_preview_is_h3_only_and_outside_the_content_hash():
    generic = prompt_context.compile_prompt_context(
        sections=[{"prompt_id": "p", "start_frame": 0, "end_frame": 10,
                   "channels": {"visual": "A room."}}],
        window_start=0, window_end=10, fps=24.0)
    assert "h3_task_type_preview" not in generic

    compiled = _compile_cross_scope_summary(["video editing"], None)
    assert prompt_context.content_hash({
        "prompt": compiled["prompt"], "channels": compiled["channels"],
        "segments": compiled["segments"], "window": compiled["window"],
        "profile_hash": compiled["profile_hash"],
        "setup_manifest": compiled["setup_manifest"],
    }) == compiled["content_hash"]


def test_freeze_drops_the_preview(monkeypatch):
    """Behavioural: the enqueue freeze must not carry the preview."""
    from test_reference_prose_policy import _generic_project
    from server import prompt_channel_templates
    from server.timeline_state import GenerationJob

    original = routes.compile_live_scene_prompt_context
    produced = {}

    def with_preview(*args, **kwargs):
        result = original(*args, **kwargs)
        result["h3_task_type_preview"] = {
            "role_derived": ["reference generation"],
            "scene_effective": ["reference generation"],
            "scene_source": "roles"}
        produced.update(result)
        return result

    monkeypatch.setattr(routes, "compile_live_scene_prompt_context", with_preview)
    project, scene = _generic_project()
    job = GenerationJob(
        scene_id=scene.scene_id, selection_start=0, selection_end=100,
        params={"snapshot_version": 1, "prompt_context_format": "prompt_context_v1",
                prompt_channel_templates.PROJECT_TEMPLATE_KEY:
                    prompt_channel_templates.get_channel_template("standard")})
    routes._compose_frozen_job_prompt(project, job)
    assert "h3_task_type_preview" in produced
    assert "h3_task_type_preview" not in job.compiled_prompt_context
    assert job.compiled_prompt_context["content_hash"] == produced["content_hash"]


# --- Authoring surfaces -----------------------------------------------------

H3_TASK_FIELD = {
    "type": "enum_multi", "label": "Summary task types", "default_source": "roles",
    "values": [
        {"value": "keyframe completion", "label": "Keyframe completion"},
        {"value": "reference generation", "label": "Reference generation"},
        {"value": "audio reference", "label": "Audio reference"},
        {"value": "video editing", "label": "Video editing"},
    ],
}
H3_PROFILE = {
    "profile_id": "minimax_h3_ref@1", "name": "MiniMax H3 Full Reference",
    "capabilities": {"reference": {"derived": {
        "summary": {"order": 1, "channel_key": "summary",
                    "placement": "section_prefix", "label": "Summary",
                    "fields": {"task_types": H3_TASK_FIELD}},
    }}},
}
CANDIDATE = {
    "window": {"start_frame": 0, "end_frame": 48},
    "setup_manifest": {},
    "h3_task_type_preview": {
        "role_derived": ["reference generation"],
        "scene_effective": ["reference generation", "audio reference"],
        "scene_source": "explicit",
    },
}

# Opens the chip editor on `attachment`, runs `steps` against the task-type
# group, presses Attach, and reports what the dialog showed and saved.
_CHIP_HARNESS = """
const PROFILE = __PROFILE__;
const UNITS = [{semantic_unit_id:"u1", handle:"Anna", name:"Anna",
  attachment_defaults:{task_types:["video editing"]}},
  {semantic_unit_id:"u2", handle:"Bo", name:"Bo"}];
const scene = { duration_frames: 48, _context_channel_keys: ["summary"],
  reference_lane_count: 1, reference_lane_configs: [{}],
  reference_lane_recipes: [], reference_items: [] };
const walk = (n, out = []) => { out.push(n);
  for (const c of n.childNodes || []) if (c && c.tagName) walk(c, out); return out; };
async function openChip(attachment, candidate, steps = async () => {}) {
  document.body.childNodes = [];
  let settled = false;
  const pending = mod.configurePromptAttachment(attachment, {
    scene, semanticUnits: UNITS, profileId: "minimax_h3_ref@1",
    profile: PROFILE, candidate,
  }).then((value) => { settled = true; return value; });
  const group = document.body.querySelector("[data-sonder-task-type-choices]");
  const read = () => ({
    mode: group.querySelector("[data-sonder-task-type-mode]")?.dataset.sonderTaskTypeMode,
    label: group.querySelector("[data-sonder-task-type-mode]")?.textContent,
    checked: walk(group).filter((n) => n.tagName === "INPUT" && n.checked).map((n) => n.value),
    disabled: walk(group).filter((n) => n.tagName === "INPUT").every((n) => n.disabled),
    text: group.textContent,
    scene: group.querySelector("[data-sonder-task-type-scene-preview]")?.textContent || "",
    error: group.querySelector("[data-sonder-task-type-error]")?.style.display === "block"
      ? group.querySelector("[data-sonder-task-type-error]").textContent : "",
  });
  const press = (label) => walk(group).find((n) => n.tagName === "BUTTON"
    && n.textContent === label)._handlers.click[0]();
  const toggle = (value, checked) => {
    const box = walk(group).find((n) => n.tagName === "INPUT" && n.value === value);
    box.checked = checked; box._handlers.change[0]();
  };
  const opened = read();
  await steps({ press, toggle, read });
  const beforeSave = read();
  const attach = () => walk(document.body).find((n) => n.tagName === "BUTTON"
    && n.textContent === "Attach")._handlers.click[0]();
  attach();
  await Promise.resolve();
  const refused = !settled;
  let saved = null;
  if (refused) {
    const refusal = read();
    await steps.afterRefusal?.({ press, toggle, read });
    attach();
    saved = await pending;
    return { opened, beforeSave, refused, refusal, saved: saved.attachment };
  }
  saved = await pending;
  return { opened, beforeSave, refused, saved: saved.attachment };
}
"""


def _chip_script(body):
    return _run_chip_dom_script(
        _CHIP_HARNESS.replace("__PROFILE__", json.dumps(H3_PROFILE)) + body)


def _chip(**config):
    capability = {"capability_id": "summary", "kind": "summary"}
    if "capability_config" in config:
        capability["config"] = config.pop("capability_config")
    return {"attachment_id": "chip", "kind": "reference",
            "source": {"semantic_unit_ids": [config.pop("unit", "u2")]},
            "config": {"overrides": config.pop("overrides", {})},
            "capabilities": [capability]}


def test_chip_shows_role_checks_and_saves_nothing_when_untouched():
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip())}, {json.dumps(CANDIDATE)});
        console.log(JSON.stringify(r));
    """)
    opened = result["opened"]
    assert opened["mode"] == "automatic"
    assert opened["label"] == "Automatic from staged roles"
    assert opened["checked"] == ["reference generation"]
    assert opened["disabled"] is True
    assert "frames 0–48" in opened["text"]
    # The scene line is the last compile, and names where it came from.
    assert "reference generation + audio reference" in opened["scene"]
    assert "combined from customized chips" in opened["scene"]
    # A no-op save is byte-identical authoring, so the compiled prompt and its
    # content hash cannot move.
    assert result["saved"]["config"]["overrides"] == {}
    assert result["saved"]["capabilities"] == [
        {"capability_id": "summary", "kind": "summary"}]


def test_chip_follows_identity_default_and_saves_nothing_when_untouched():
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip(unit="u1"))}, {json.dumps(CANDIDATE)});
        console.log(JSON.stringify(r));
    """)
    assert result["opened"]["mode"] == "following"
    assert result["opened"]["label"] == "Following Shared identity default · @Anna"
    assert result["opened"]["checked"] == ["video editing"]
    assert "task_types" not in result["saved"]["config"]["overrides"]


def test_customize_matching_the_automatic_checks_is_an_explicit_choice():
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip())}, {json.dumps(CANDIDATE)},
          async ({{ press }}) => press("Customize"));
        console.log(JSON.stringify(r));
    """)
    assert result["beforeSave"]["mode"] == "customized"
    assert result["beforeSave"]["checked"] == ["reference generation"]
    assert result["beforeSave"]["disabled"] is False
    assert result["saved"]["config"]["overrides"]["task_types"] == [
        "reference generation"]


def test_customized_h3_selection_needs_one_supported_choice_and_reset_clears_it():
    result = _chip_script(f"""
        const steps = async ({{ press, toggle }}) => {{
          press("Customize"); toggle("reference generation", false); }};
        steps.afterRefusal = async ({{ press }}) => press("Reset to inherited");
        const r = await openChip({json.dumps(_chip())}, {json.dumps(CANDIDATE)}, steps);
        console.log(JSON.stringify(r));
    """)
    assert result["refused"] is True
    assert "at least one supported task type" in result["refusal"]["error"]
    assert "task_types" not in result["saved"]["config"]["overrides"]


def test_reset_names_where_it_returns_to():
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip(unit="u1", overrides={"task_types": ["audio reference"]}))},
          {json.dumps(CANDIDATE)});
        console.log(JSON.stringify(r));
    """)
    assert result["opened"]["mode"] == "customized"
    assert "Returns to: Following Shared identity default · @Anna" in result["opened"]["text"]
    assert result["saved"]["config"]["overrides"]["task_types"] == ["audio reference"]


def test_stored_empty_list_is_shown_automatic_and_kept_verbatim():
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip(unit="u1", overrides={"task_types": []}))},
          {json.dumps(CANDIDATE)});
        console.log(JSON.stringify(r));
    """)
    # Under H3 an explicit empty list falls back to staged roles even though
    # the identity below states a list.
    assert result["opened"]["mode"] == "automatic"
    assert result["opened"]["checked"] == ["reference generation"]
    assert "stores an empty choice" in result["opened"]["text"]
    assert result["saved"]["config"]["overrides"]["task_types"] == []


def test_unsupported_saved_value_stays_until_repaired():
    result = _chip_script(f"""
        const kept = await openChip({json.dumps(_chip(overrides={"task_types": ["future task"]}))},
          {json.dumps(CANDIDATE)});
        const repaired = await openChip({json.dumps(_chip(overrides={"task_types": ["future task"]}))},
          {json.dumps(CANDIDATE)}, async ({{ toggle }}) => {{
            toggle("future task", false); toggle("video editing", true); }});
        console.log(JSON.stringify({{ kept, repaired }}));
    """)
    assert "Unsupported saved value: future task" in result["kept"]["opened"]["text"]
    assert result["kept"]["opened"]["checked"] == ["future task"]
    assert result["kept"]["saved"]["config"]["overrides"]["task_types"] == ["future task"]
    assert result["repaired"]["saved"]["config"]["overrides"]["task_types"] == [
        "video editing"]


def test_legacy_capability_value_is_authoritative_until_edited_then_moves():
    legacy = _chip(capability_config={"task_types": ["video editing"], "note": "keep"})
    result = _chip_script(f"""
        const untouched = await openChip({json.dumps(legacy)}, {json.dumps(CANDIDATE)});
        const edited = await openChip({json.dumps(legacy)}, {json.dumps(CANDIDATE)},
          async ({{ toggle }}) => toggle("audio reference", true));
        const reset = await openChip({json.dumps(legacy)}, {json.dumps(CANDIDATE)},
          async ({{ press }}) => press("Reset to inherited"));
        console.log(JSON.stringify({{ untouched, edited, reset }}));
    """)
    untouched = result["untouched"]
    assert untouched["opened"]["mode"] == "customized"
    assert untouched["opened"]["checked"] == ["video editing"]
    assert "task_types" not in untouched["saved"]["config"]["overrides"]
    assert untouched["saved"]["capabilities"][0]["config"] == {
        "task_types": ["video editing"], "note": "keep"}

    edited = result["edited"]["saved"]
    assert edited["config"]["overrides"]["task_types"] == [
        "audio reference", "video editing"]
    assert edited["capabilities"][0]["config"] == {"note": "keep"}

    reset = result["reset"]["saved"]
    assert "task_types" not in reset["config"]["overrides"]
    assert reset["capabilities"][0]["config"] == {"note": "keep"}


def test_missing_preview_says_unavailable_instead_of_guessing():
    candidate = {"window": {"start_frame": 0, "end_frame": 48}, "setup_manifest": {}}
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip())}, {json.dumps(candidate)});
        console.log(JSON.stringify(r));
    """)
    opened = result["opened"]
    assert opened["mode"] == "automatic"
    assert opened["label"] == "Automatic from staged roles · unavailable"
    assert opened["checked"] == []
    assert "Role suggestion unavailable" in opened["text"]
    assert "unavailable until the scene compiles" in opened["scene"]
    assert "task_types" not in result["saved"]["config"]["overrides"]


def test_non_roles_format_keeps_its_empty_list_semantics():
    """A format without a roles default may customize to nothing."""
    profile = json.loads(json.dumps(H3_PROFILE))
    del profile["capabilities"]["reference"]["derived"]["summary"]["fields"][
        "task_types"]["default_source"]
    body = _CHIP_HARNESS.replace("__PROFILE__", json.dumps(profile)) + f"""
        const r = await openChip({json.dumps(_chip())}, {json.dumps(CANDIDATE)},
          async ({{ press }}) => press("Customize"));
        console.log(JSON.stringify(r));
    """
    result = _run_chip_dom_script(body)
    assert result["opened"]["mode"] == "following"
    assert result["opened"]["checked"] == []
    assert result["refused"] is False
    assert result["saved"]["config"]["overrides"]["task_types"] == []


def test_saved_values_match_the_way_the_compiler_matches_them():
    """The compiler trims and case-folds; the checkboxes must agree."""
    result = _chip_script(f"""
        const untouched = await openChip({json.dumps(_chip(overrides={"task_types": ["Video Editing", " audio reference"]}))},
          {json.dumps(CANDIDATE)});
        const retoggled = await openChip({json.dumps(_chip(overrides={"task_types": ["Video Editing"]}))},
          {json.dumps(CANDIDATE)}, async ({{ toggle }}) => {{
            toggle("video editing", false); toggle("video editing", true); }});
        console.log(JSON.stringify({{ untouched, retoggled }}));
    """)
    opened = result["untouched"]["opened"]
    assert opened["checked"] == ["audio reference", "video editing"]
    assert "Unsupported saved value" not in opened["text"]
    # Untouched keeps the raw spelling; the compiler reads it the same way.
    assert result["untouched"]["saved"]["config"]["overrides"]["task_types"] == [
        "Video Editing", " audio reference"]
    assert result["retoggled"]["refused"] is False
    assert result["retoggled"]["saved"]["config"]["overrides"]["task_types"] == [
        "video editing"]


def test_routing_readout_agrees_with_the_checkboxes():
    result = _chip_script(f"""
        let body = "";
        await openChip({json.dumps(_chip())}, {json.dumps(CANDIDATE)},
          async () => {{ body = document.body.textContent; }});
        console.log(JSON.stringify({{ body }}));
    """)
    assert ("Input · Summary task typesreference generationAutomatic from staged roles"
            in result["body"])
    assert "Input · Summary task types(empty)" not in result["body"]


def _fieldset_script(body):
    return _run_chip_dom_script(f"""
        const PROFILE = {json.dumps(H3_PROFILE)};
        const CANDIDATE = {json.dumps(CANDIDATE)};
        const guards = await import({json.dumps((ROOT / "web/js/modal_draft_guard.js").as_uri())});
        const choices = await import({json.dumps((ROOT / "web/js/prompt_task_type_choices.js").as_uri())});
        const walk = (n, out = []) => {{ out.push(n);
          for (const c of n.childNodes || []) if (c && c.tagName) walk(c, out); return out; }};
        {body}
    """)


def test_attach_fieldset_collects_nothing_and_collapses_to_one_line():
    result = _fieldset_script("""
        const fieldset = mod.createReferenceOverrideFieldset({
          profile: PROFILE, overrides: {}, selected: "",
          taskTypePreview: choices.captureTaskTypePreview(CANDIDATE),
          showScenePreview: true,
        });
        console.log(JSON.stringify({
          collected: fieldset.collect(),
          rowHidden: fieldset.row("task_types").style.display === "none",
          compact: fieldset.summaryRow.querySelector("[data-sonder-compact-task-types]").textContent,
        }));
    """)
    assert result["collected"] == {}
    assert result["rowHidden"] is True
    assert result["compact"] == (
        "Summary task types: reference generation · Automatic from staged roles")


def test_modal_dirty_detection_sees_mode_even_with_identical_checks():
    result = _fieldset_script("""
        const fieldset = mod.createReferenceOverrideFieldset({
          profile: PROFILE, overrides: {}, selected: "",
          taskTypePreview: choices.captureTaskTypePreview(CANDIDATE),
        });
        const guard = guards.createModalDraftGuard({
          controls: () => [...fieldset.controls.values()] });
        const group = fieldset.controls.get("task_types");
        const press = (label) => walk(group).find((n) => n.tagName === "BUTTON"
          && n.textContent === label)._handlers.click[0]();
        const clean = guard.isDirty();
        press("Customize");
        const customized = guard.isDirty();
        const checks = walk(group).filter((n) => n.tagName === "INPUT" && n.checked)
          .map((n) => n.value);
        press("Reset to inherited");
        const reset = guard.isDirty();
        console.log(JSON.stringify({ clean, customized, checks, reset }));
    """)
    assert result == {"clean": False, "customized": True,
                      "checks": ["reference generation"], "reset": False}


def test_reset_in_a_collapsed_fieldset_keeps_keyboard_focus_on_the_panel():
    result = _fieldset_script("""
        N.prototype.focus = function () { document.activeElement = this; };
        const fieldset = mod.createReferenceOverrideFieldset({
          profile: PROFILE, overrides: {task_types: ["future task"]}, selected: "",
          taskTypePreview: choices.captureTaskTypePreview(CANDIDATE),
        });
        const group = fieldset.controls.get("task_types");
        const visibleRows = () => walk(group).filter((n) => n.tagName === "LABEL"
          && n.style.display !== "none").map((n) => n.textContent);
        const before = visibleRows();
        walk(group).find((n) => n.tagName === "BUTTON"
          && n.textContent === "Reset to inherited")._handlers.click[0]();
        console.log(JSON.stringify({
          before, after: visibleRows(),
          rowHidden: fieldset.row("task_types").style.display === "none",
          focused: document.activeElement?.textContent,
          collected: fieldset.collect(),
        }));
    """)
    assert "Unsupported saved value: future task" in result["before"]
    assert not any("Unsupported" in row for row in result["after"])
    assert result["rowHidden"] is True
    assert result["focused"] == "Override…"
    assert result["collected"] == {}


# --- Project-wide identity editor -------------------------------------------

_IDENTITY_HARNESS = r"""
class N {
  constructor(tag) {
    this.tagName=String(tag).toUpperCase(); this.children=[]; this.options=[];
    this.style={cssText:""}; this.dataset={}; this.attributes={}; this._handlers={};
    this.value=""; this.textContent=""; this.disabled=false; this.checked=false;
    this.open=false; this.multiple=false; this.selected=false;
  }
  appendChild(c) {
    if (typeof c === "string") { const text=new N("#text"); text.textContent=c; c=text; }
    this.children.push(c); c.parentElement=this;
    if (c.tagName === "OPTION") this.options.push(c);
    return c;
  }
  append(...cs) { cs.forEach((c) => c != null && this.appendChild(c)); }
  addEventListener(t,h) { (this._handlers[t] ||= []).push(h); }
  setAttribute(k,v) { this.attributes[k]=String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  querySelector() { return null; }
  remove() {
    if (this.parentElement) this.parentElement.children =
      this.parentElement.children.filter((c) => c !== this);
  }
  focus() { globalThis.document.activeElement=this; }
  get selectedOptions() { return this.options.filter((option) => option.selected); }
}
globalThis.document={
  createElement:(t)=>new N(t), createTextNode:(v)=>{const n=new N("#text");n.textContent=String(v);return n;},
  body:new N("body"), activeElement:null,
};
const windowHandlers={};
globalThis.window={
  addEventListener(t,h){(windowHandlers[t] ||= []).push(h);},
  removeEventListener(t,h){windowHandlers[t]=(windowHandlers[t] || []).filter((v)=>v!==h);},
};
let confirmations=0;
globalThis.confirm=()=>{ confirmations += 1; return false; };
globalThis.localStorage={getItem(){return null;},setItem(){}};
globalThis.CSS={escape:(v)=>String(v)};
const mod=await import(__MODULE__);
const walk=(n,out=[])=>{out.push(n);n.children.forEach((c)=>walk(c,out));return out;};
const text=(n)=>[n.textContent,...n.children.map(text)].join(" ");
async function editIdentity(unit, steps = () => {}) {
  document.body.children = [];
  let saved = null;
  const root = new N("div");
  mod.mountPromptIdentityPanel(root, {
    projectKey:"task-types", profile:__PROFILE__, references:[], assets:[],
    // Mounted before the compile landed; the modal must read the newer one.
    semanticUnits: unit ? [unit] : [], candidate: {setup_manifest:{}},
    currentCandidate: () => (__CANDIDATE__),
    saveSemanticUnitChange: async (change) => { saved = change.value; },
  });
  const opener = walk(root).find((n) => n.tagName === "BUTTON" && (unit
    ? n.textContent === "Edit"
    : n.attributes["aria-label"] === "Create prompt identity"));
  opener._handlers.click[0]();
  const modal = document.body.children.at(-1);
  const group = walk(modal).find((n) => n.dataset.sonderTaskTypeChoices);
  const read = () => ({
    mode: walk(group).find((n) => n.dataset.sonderTaskTypeMode !== undefined)?.dataset.sonderTaskTypeMode,
    label: walk(group).find((n) => n.dataset.sonderTaskTypeMode !== undefined)?.textContent,
    checked: walk(group).filter((n) => n.tagName === "INPUT" && n.checked).map((n) => n.value),
    text: text(group).replace(/\s+/g, " "),
  });
  const press = (label) => walk(group).find((n) => n.tagName === "BUTTON"
    && n.textContent === label)._handlers.click[0]();
  const opened = read();
  steps({ press, modal });
  const name = walk(modal).find((n) => n.tagName === "INPUT" && n.placeholder === "Identity name");
  if (!name.value) name.value = "Someone";
  await walk(modal).find((n) => n.tagName === "BUTTON"
    && n.textContent === "Save identity")._handlers.click[0]();
  return { opened, saved };
}
"""


def _identity_script(body):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt identity editor coverage")
    script = (_IDENTITY_HARNESS
              .replace("__MODULE__", json.dumps(
                  (ROOT / "web/js/prompt_identity_panel.js").as_uri()))
              .replace("__PROFILE__", json.dumps({
                  **H3_PROFILE,
                  "identity_kinds": [{"key": "subject", "label": "Subject"}]}))
              .replace("__CANDIDATE__", json.dumps(CANDIDATE))) + body
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def test_identity_editor_previews_current_scene_roles_and_saves_sparsely():
    result = _identity_script("""
        const created = await editIdentity(null);
        const stored = await editIdentity({semantic_unit_id:"u", handle:"Anna", name:"Anna",
          kind:"subject", attachment_defaults:{task_types:[], summary:"S"}});
        const customized = await editIdentity({semantic_unit_id:"u", handle:"Anna",
          name:"Anna", kind:"subject", attachment_defaults:{}},
          ({ press }) => press("Customize"));
        console.log(JSON.stringify({ created, stored, customized }));
    """)
    created = result["created"]
    assert created["opened"]["mode"] == "automatic"
    assert created["opened"]["label"] == "Current-scene preview"
    assert created["opened"]["checked"] == ["reference generation"]
    assert "task_types" not in created["saved"]["attachment_defaults"]

    # A stored empty list round-trips untouched.
    assert result["stored"]["saved"]["attachment_defaults"]["task_types"] == []

    assert result["customized"]["saved"]["attachment_defaults"]["task_types"] == [
        "reference generation"]


def test_identity_editor_dismissal_guard_sees_customize_with_identical_checks():
    result = _identity_script("""
        const escape = () => windowHandlers.keydown[0]({key:"Escape",
          isComposing:false, keyCode:27, stopImmediatePropagation(){}, preventDefault(){}});
        const open = (modal) => document.body.children.includes(modal);
        let asked = {};
        await editIdentity(null, ({ press, modal }) => {
          press("Customize"); escape();
          asked.customized = confirmations; asked.keptOpen = open(modal);
          press("Reset to inherited"); escape();
          asked.reset = confirmations; asked.closed = !open(modal);
        });
        console.log(JSON.stringify(asked));
    """)
    # Customize alone prompts (declined, so the editor stays). Customize then
    # Reset is back to the opening state: Escape closes without asking.
    assert result == {"customized": 1, "keptOpen": True, "reset": 1, "closed": True}


def test_scene_line_says_when_no_summary_contributes():
    candidate = {**CANDIDATE, "h3_task_type_preview": {
        "role_derived": ["reference generation"], "scene_effective": [],
        "scene_source": "none"}}
    result = _chip_script(f"""
        const r = await openChip({json.dumps(_chip())}, {json.dumps(candidate)});
        console.log(JSON.stringify(r));
    """)
    assert result["opened"]["scene"].endswith(
        "): none · no Summary contributes in this window")
