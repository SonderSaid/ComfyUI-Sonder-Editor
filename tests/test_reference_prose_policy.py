"""Out-of-window Reference text: staging, policy and execution contracts."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import prompt_context, prompt_live_context
from server.reference_resolution import (
    REFERENCE_VERDICT, REFERENCE_VERDICT_LABEL, resolve_reference_verdicts,
    resolve_effective_references, resolve_reference_staging,
)
from server.timeline_state import Scene, TimelineProject
from test_minimax_h3_format_coverage import _identity_dormancy_fixture, _reference_chip

ROOT = Path(__file__).resolve().parents[1]


def _node_binary():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Reference JavaScript comparison")
    return node


def test_reference_verdict_python_js_parity():
    items = [
        {"start_frame": 0, "end_frame": 100},
        {"start_frame": 30, "end_frame": 70},
        {"start_frame": 30, "end_frame": 70},
        {"start_frame": 50, "end_frame": 90},
        {"start_frame": 50, "end_frame": 50},
        {"start_frame": 50, "end_frame": 40},
        {"lane_index": 7}, {"muted": True},
        {"lane_index": 1, "start_frame": 0, "end_frame": 45},
        {"lane_index": 2, "start_frame": 0, "end_frame": 30},
        {"lane_index": 3},
    ]
    kwargs = dict(reference_items=items, lane_count=4, scene_duration=100,
                  window_start=40, window_end=80, frame_threshold_pct=50,
                  lane_configs=[{}, {}, {}, {"hidden": True}])
    actual = resolve_reference_verdicts(**kwargs)
    uri = (ROOT / "web/js/reference_resolution.js").as_uri()
    script = f"""
const m = await import({json.dumps(uri)});
const r = m.resolveReferenceVerdicts({{referenceItems: {json.dumps(items)},
 laneCount:4, sceneDuration:100, windowStart:40, windowEnd:80,
 frameThresholdPct:50, laneConfigs:[{{}},{{}},{{}},{{hidden:true}}]}});
console.log(JSON.stringify({{verdicts:Object.fromEntries(r.verdicts),
 values:m.REFERENCE_VERDICT, labels:m.REFERENCE_VERDICT_LABEL}}));
"""
    js = json.loads(subprocess.run([_node_binary(), "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True).stdout)
    assert {str(k): v for k, v in actual["verdicts"].items()} == js["verdicts"]
    assert list(actual["verdicts"].values()) == [
        "superseded", "superseded", "superseded", "winner", "outside",
        "outside", "excluded", "excluded", "below_threshold", "outside", "excluded"]
    assert js["values"] == REFERENCE_VERDICT
    assert js["labels"] == REFERENCE_VERDICT_LABEL
    assert resolve_effective_references(**kwargs) == actual["winners"]
    assert actual["winners"][0]["item"] is items[3]


def test_staging_preserves_all_items_and_member_bindings():
    items = [dict(reference_item_id="a", lane_index=0, members=[{"member_id":"m"}]),
             SimpleNamespace(reference_item_id="b", lane_index=1, muted=True,
                             members=[{"member_id":"m"}])]
    result = resolve_reference_staging(reference_items=items, lane_count=2,
        scene_duration=100, window_start=20, window_end=40)
    assert result["resolved"] is True
    assert result["members"] == {"m": ["a", "b"]}
    assert {k:v["verdict"] for k,v in result["items"].items()} == {"a":"winner", "b":"excluded"}
    assert "deleted" not in result["items"]


@pytest.mark.parametrize("project_policy", ["drop", "keep"])
@pytest.mark.parametrize("override", [None, "drop", "keep"])
def test_chip_override_precedence(project_policy, override):
    config = {} if override is None else {"reference_prose": override}
    assert prompt_context.resolve_reference_prose_policy(
        {"config": config}, {"reference_prose_policy":project_policy}) == (override or project_policy)
    assert config == ({} if override is None else {"reference_prose": override})


def test_compile_assembly_supplies_staging_and_project_policy(monkeypatch):
    captures = []
    def capture(**kwargs):
        captures.append(kwargs["context"])
        return {"errors": [], "warnings": []}
    monkeypatch.setattr(prompt_context, "compile_prompt_context", capture)
    scene = Scene(duration_frames=100)
    project = TimelineProject(metadata={"reference_prose_policy":"keep"})
    prompt_live_context.compile_live_scene_prompt_context(project, scene,
        template="standard", window_start=0, window_end=50, fps=24)
    scene.compile_prompt_context(0, 50, template="standard")
    assert all(row["reference_staging"]["resolved"] for row in captures)
    assert captures[0]["reference_prose_policy"] == "keep"


def test_projectless_compile_preserves_requested_reference_threshold(monkeypatch):
    from server.timeline_state import ReferenceItem
    captured = []
    def capture(**kwargs):
        captured.append(kwargs["context"]["reference_staging"])
        return {"errors": [], "prompt": "composed"}
    monkeypatch.setattr(prompt_context, "compile_prompt_context", capture)
    scene = Scene(duration_frames=100, reference_items=[ReferenceItem(
        reference_item_id="edge", start_frame=0, end_frame=45)])
    scene.global_attachments = [{"kind":"reference"}]
    assert scene.get_prompt_for_range(40, 80, reference_threshold_pct=50) == "composed"
    assert captured[0]["items"]["edge"]["verdict"] == "below_threshold"


def _policy_compile(monkeypatch, policy, **kwargs):
    original = prompt_context.compile_prompt_context
    def compile_with_policy(**values):
        values["context"] = {**values.get("context", {}), "reference_prose_policy": policy}
        return original(**values)
    with monkeypatch.context() as patch:
        patch.setattr(prompt_context, "compile_prompt_context", compile_with_policy)
        return _identity_dormancy_fixture(**kwargs)


def _codes(compiled, tier="errors"):
    return {row["code"] for row in compiled[tier]}


@pytest.mark.parametrize("scope", ["global_chip", "section_chip"])
def test_keep_emits_authored_text_and_summary_types_in_both_scopes(monkeypatch, scope):
    chip = _reference_chip("kept", {"semantic_unit_ids":["dormant"]},
        {"text":"AUTHORED MENTION", "summary":"AUTHORED SUMMARY", "task_types":["video editing"]},
        capabilities=("mentions", "summary"))
    dropped = _policy_compile(monkeypatch, "drop", **{scope:chip})
    kept = _policy_compile(monkeypatch, "keep", **{scope:chip})
    assert not kept["errors"]
    assert "AUTHORED MENTION" in kept["prompt"] and "AUTHORED SUMMARY" in kept["prompt"]
    assert "video editing" in kept["prompt"] and "video editing" not in dropped["prompt"]
    assert "AUTHORED" not in dropped["prompt"]
    assert kept["content_hash"] != dropped["content_hash"]
    assert "reference_prose_preserved" in _codes(kept, "warnings")
    assert all(row["state_reason"] == prompt_context.PRESERVED_REFERENCE_REASON
               for row in kept["attachment_capability_projections"]
               if row["attachment_id"] == "kept" and row["state"] == "emitted")


def test_default_equals_explicit_drop_entire_compiled_result(monkeypatch):
    chip = _reference_chip("drop", {"semantic_unit_ids":["dormant"]},
        {"text":"MUST NOT EMIT"}, capabilities=("mentions",))
    # The fixture mints a Shot id each call; pin it for a full-envelope comparison.
    original = prompt_context.shot_attachment
    shot = original()
    monkeypatch.setattr(prompt_context, "shot_attachment", lambda: shot)
    assert _identity_dormancy_fixture(global_chip=chip) == _policy_compile(
        monkeypatch, "drop", global_chip=chip)


@pytest.mark.parametrize("scope", ["global_chip", "section_chip"])
def test_keep_surfaces_output_limit_and_drop_does_not(monkeypatch, scope):
    chip = _reference_chip("large", {"semantic_unit_ids":["dormant"]},
        {"summary":"x" * (prompt_context.MAX_ATTACHMENT_OUTPUT + 1)}, capabilities=("summary",))
    assert "attachment_output_limit" not in _codes(_policy_compile(monkeypatch, "drop", **{scope:chip}))
    result = _policy_compile(monkeypatch, "keep", **{scope:chip})
    assert "attachment_output_limit" in _codes(result)
    assert "reference_source_dormant" not in _codes(result, "warnings")


@pytest.mark.parametrize("scope", ["global_chip", "section_chip"])
@pytest.mark.parametrize("fault", ["deleted", "token", "route", "declaration"])
def test_blockers_take_precedence_in_each_scope(monkeypatch, scope, fault):
    chip = _reference_chip("broken", {"semantic_unit_ids":["dormant"]},
                           {"text":"authored"}, capabilities=("mentions",))
    extra = {}
    code = "broken_reference_source"
    if fault == "deleted":
        extra["catalog_members"] = []
    elif fault == "token":
        chip["config"]["overrides"]["text"] = "@subject(missing)"
        code = "unresolved_prompt_token"
    elif fault == "route":
        chip["capabilities"][0]["channel_key"] = "missing"
        code = "invalid_attachment_route"
    else:
        chip["capabilities"][0]["kind"] = "made_up"
        code = "undeclared_reference_capability"
    result = _policy_compile(monkeypatch, "drop", **{scope:chip}, **extra)
    assert code in _codes(result)
    assert "reference_source_dormant" not in _codes(result, "warnings")


def test_deleted_physical_member_blocks_but_unstaged_member_warns():
    chip = _reference_chip("physical", {"picture_ids":["dormant-member"]},
                           {"text":"prose"}, capabilities=("mentions",))
    result = _identity_dormancy_fixture(section_chip=chip)
    assert not result["errors"]
    warning = next(row for row in result["warnings"] if row["code"] == "reference_source_dormant")
    assert "Cast" in warning["message"] and "dormant-member" not in warning["message"]
    result = _identity_dormancy_fixture(section_chip=chip, catalog_members=[])
    assert "broken_reference_source" in _codes(result)


def _generic_project():
    from server.timeline_state import PromptSection, ReferenceItem
    item = ReferenceItem(reference_item_id="item", start_frame=0, end_frame=100, muted=True)
    chip = prompt_context.normalize_attachment({"attachment_id":"generic", "kind":"reference",
        "source":{"reference_item_id":"item"}, "config":{"text":"CHIP TEXT"},
        "capabilities":[{"kind":"derived_prompt", "capability_id":"derived_prompt"}]})
    scene = Scene(scene_id="scene", duration_frames=100, reference_items=[item],
        prompt_sections=[PromptSection(0, 100, channels={"visual":"WALK"}, attachments=[chip])])
    project = TimelineProject(scenes=[scene])
    return project, scene


@pytest.mark.parametrize("verdict", ["excluded", "outside", "below_threshold", "superseded"])
def test_generic_item_warns_for_each_staging_verdict(verdict):
    from server.timeline_state import ReferenceItem
    project, scene = _generic_project()
    item = scene.reference_items[0]
    threshold = 0
    if verdict != "excluded":
        item.muted = False
    if verdict == "outside":
        item.end_frame = 20
    if verdict == "below_threshold":
        item.end_frame = 45
        threshold = 50
    if verdict == "superseded":
        scene.reference_items.append(ReferenceItem(reference_item_id="winner", start_frame=40, end_frame=80))
    result = prompt_live_context.compile_live_scene_prompt_context(project, scene,
        template="standard", window_start=40, window_end=80, fps=24, reference_threshold=threshold)
    assert not result["errors"]
    row = next(row for row in result["warnings"] if row["code"] == "reference_source_dormant")
    assert row["verdict"] == verdict
    assert REFERENCE_VERDICT_LABEL[verdict] in row["message"]
    assert "CHIP TEXT" not in result["prompt"] and "WALK" in result["prompt"]


def test_legacy_projectless_compose_and_deleted_item_refusal():
    _project, scene = _generic_project()
    assert "WALK" in scene.get_prompt_for_range(0, 100)
    scene.reference_items = []
    assert scene.get_prompt_for_range(0, 100) == ""
    assert "broken_reference_source" in _codes(scene.compile_prompt_context(0, 100))


def test_missing_staging_fails_open_and_never_seeds_override():
    _project, scene = _generic_project()
    result = prompt_context.compile_prompt_context(sections=scene.prompt_sections,
        window_start=0, window_end=100, fps=24, template="standard")
    assert not result["errors"] and "reference_source_dormant" in _codes(result, "warnings")
    assert "reference_prose" not in scene.to_dict()["prompt_sections"][0]["attachments"][0]["config"]


def test_enqueue_freezes_policy_and_requeue_retains_it():
    from server import routes, prompt_channel_templates
    from server.timeline_state import GenerationJob
    project, scene = _generic_project()
    project.metadata["reference_prose_policy"] = "keep"
    job = GenerationJob(scene_id=scene.scene_id, selection_start=0, selection_end=100,
        params={"snapshot_version":1, "prompt_context_format":"prompt_context_v1",
                prompt_channel_templates.PROJECT_TEMPLATE_KEY:prompt_channel_templates.get_channel_template("standard")})
    routes._compose_frozen_job_prompt(project, job)
    assert job.params["reference_prose_policy"] == "keep"
    assert "WALK" in job.prompt
    assert "CHIP TEXT" not in job.prompt  # Derived recipe text requires a winning item.
    original = job.prompt
    project.metadata["reference_prose_policy"] = "drop"
    routes._compose_frozen_job_prompt(project, job)
    assert job.prompt == original


def test_prompt_bridge_accepts_unstaged_and_refuses_deleted_item():
    from test_prompt_bridge import _import_prompt_bridge
    bridge = _import_prompt_bridge()
    project, scene = _generic_project()
    project._execution_context = {"scene_id":scene.scene_id, "context_start":0, "context_end":100}
    assert "WALK" in bridge.build_window_relay_payload(project)["smart_prompt"]
    scene.reference_items = []
    with pytest.raises(RuntimeError, match="broken_reference_source"):
        bridge.build_window_relay_payload(project)


@pytest.mark.parametrize("shared", ["group", "unit"])
def test_preserved_retention_cannot_read_a_live_shared_appearance_bucket(monkeypatch, shared):
    live_source = {"semantic_unit_ids": ["staged"]} if shared == "group" else {
        "semantic_unit_ids": ["dormant"], "picture_ids": ["staged-member"]}
    live = _reference_chip("live", live_source, {}, capabilities=("mentions",), group="shared")
    dormant = _reference_chip("dormant", {"semantic_unit_ids":["dormant"]},
        {"reference_prose":"keep", "retention_detail":"AUTHORED DETAIL"},
        capabilities=("retention",), group="shared" if shared == "group" else "own")
    result = _identity_dormancy_fixture(global_chip=live, section_chip=dormant)
    assert not result["errors"]
    emitted = [row["text"] for row in result["emissions"] if row["attachment_id"] == "dormant"]
    assert emitted and "AUTHORED DETAIL" in " ".join(emitted)
    assert "appears in" not in " ".join(emitted)


def test_mixed_bindings_use_any_live_source():
    context = {"profile":prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"],
        "ordinal_manifest":{"pictures":{"live":1}}, "generic_references":{}}
    chip = {"source":{"reference_item_id":"absent", "picture_ids":["live"]}}
    assert prompt_context.reference_chip_conditions_window(chip, context)
    chip["source"]["picture_ids"] = []
    assert not prompt_context.reference_chip_conditions_window(chip, context)
    context["generic_references"]["absent"] = {}
    assert prompt_context.reference_chip_conditions_window(chip, context)


def test_aggregated_diagnostics_mark_each_chip_without_inflating_count():
    uri = (ROOT / "web/js/prompt_context_diagnostics.js").as_uri()
    payload = {"warnings":[{"code":"reference_source_dormant", "message":"Two chips",
        "attachments":[{"origin":"global", "attachment_id":"a"},
                       {"origin":"section", "attachment_id":"b"}]}]}
    script = f"""
const m = await import({json.dumps(uri)});
console.log(JSON.stringify(m.buildPromptContextDiagnostics({json.dumps(payload)})));
"""
    result = json.loads(subprocess.run([_node_binary(), "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True).stdout)
    assert result["warningCount"] == 1
    assert set(result["byAttachment"]) == {"a", "b"}
    assert result["byAttachment"]["b"][0]["origin"] == "section"


@pytest.mark.parametrize("capability,field", [("definitions","audio_definition"), ("retention","audio_retention_detail")])
def test_keep_audio_authored_fields(capability, field):
    chip = _reference_chip("audio", {"audio_ids":["dormant-member"]},
        {"reference_prose":"keep", field:"KEEP MY VOICE"}, capabilities=(capability,))
    result = _identity_dormancy_fixture(global_chip=chip)
    assert not result["errors"]
    assert "KEEP MY VOICE" in result["prompt"]


def test_keep_preserves_retention_label_associations():
    chip = _reference_chip("physical", {"picture_ids":["dormant-member"]},
        {"reference_prose":"keep", "retention_details":{"<Picture 1>":"red dress", "<Picture 2>":"blue suit"}},
        capabilities=("retention",))
    result = _identity_dormancy_fixture(global_chip=chip)
    assert "<Picture 1>: red dress" in result["prompt"]
    assert "<Picture 2>: blue suit" in result["prompt"]
    assert "authored_ordinal_literal" in _codes(result, "warnings")


def test_keep_preserves_identity_member_prose():
    from server.timeline_state import ReferenceMember
    chip = _reference_chip("identity", {"semantic_unit_ids":["dormant"]},
        {"reference_prose":"keep"}, capabilities=("definitions",))
    units = [{"semantic_unit_id":"dormant", "name":"Person", "kind":"subject",
              "sources":[{"entity_id":"entity", "member_id":"dormant-member"}]}]
    result = _identity_dormancy_fixture(global_chip=chip, units=units,
        catalog_members=[ReferenceMember(member_id="dormant-member", prompt="LIBRARY DESCRIPTION")])
    assert "LIBRARY DESCRIPTION" in result["prompt"]


def test_linked_prose_overrides_remain_local():
    from test_prompt_context_corrections import _run_chip_dom_script
    result = _run_chip_dom_script("""
const source = {attachment_id:'source', kind:'reference', config:{reference_prose:'drop'}};
const targets = [{}, {reference_prose:'keep'}, {reference_prose:'drop'}];
console.log(JSON.stringify(targets.map(config => mod.propagateLinkedPromptAttachment(
 source, {attachment_id:'target',kind:'reference',config}, 'source').config)));
""")
    assert "reference_prose" not in result[0]
    assert [row["reference_prose"] for row in result[1:]] == ["keep", "drop"]


@pytest.mark.parametrize("state", ["outside", "muted", "superseded", "missing"])
@pytest.mark.parametrize("policy", ["drop", "keep", ""])
def test_inactive_saved_reference_dialog_applies_local_policy(state, policy):
    from test_prompt_context_corrections import _run_chip_dom_script
    result = _run_chip_dom_script("""
const state = STATE, policy = POLICY;
const item = {reference_item_id:'first',lane_index:0,start_frame:0,end_frame:50,members:[]};
const scene = {duration_frames:100,_context_consumer_start:50,_context_consumer_end:100,
 reference_lane_count:1,reference_items:[item]};
if (state === 'muted') { item.end_frame=100; item.muted=true; }
if (state === 'superseded') {
 item.end_frame=100;
 scene.reference_items.push({...item,reference_item_id:'winner',start_frame:50});
}
if (state === 'missing') scene.reference_items=[];
const edited = {attachment_id:'later',emission_group_id:'linked',kind:'reference',
 source:{reference_item_id:'first'},config:{reference_prose:'keep'}};
let saved = null;
mod.configurePromptAttachment(edited, {scene,scope:'section',referenceProsePolicy:'keep'})
 .then(value => saved=value?.attachment);
const selects = document.body.querySelectorAll('select');
const source = selects.find(value => value.value === 'item:first');
const prose = selects.find(value => value.options.some(o => o.value === 'drop'));
prose.value=policy;
document.body.querySelectorAll('button').find(value => value.textContent === 'Attach')
 ._handlers.click[0]();
await Promise.resolve();
const targets = [edited, {...edited,attachment_id:'parent',config:{}},
 {...edited,attachment_id:'sibling',config:{reference_prose:'drop'}}];
console.log(JSON.stringify({sourceDisabled:source.selectedOptions[0].disabled,
 open:document.body.querySelectorAll('[data-sonder-prompt-context-modal]').length > 0,
 saved, configs:saved ? targets.map(target => mod.propagateLinkedPromptAttachment(
 saved,target,'later').config) : []}));
""".replace("STATE", json.dumps(state)).replace("POLICY", json.dumps(policy)))
    assert result["sourceDisabled"] is True
    assert result["open"] is False
    assert result["saved"]["source"] == {"reference_item_id": "first"}
    assert result["configs"][0].get("reference_prose", "") == policy
    assert "reference_prose" not in result["configs"][1]
    assert result["configs"][2]["reference_prose"] == "drop"


def test_reference_dialog_still_refuses_new_ineligible_binding():
    from test_prompt_context_corrections import _run_chip_dom_script
    result = _run_chip_dom_script("""
let saved = null;
mod.configurePromptAttachment({kind:'reference'}, {scope:'section',scene:{
 duration_frames:100,_context_consumer_start:50,_context_consumer_end:100,
 reference_items:[{reference_item_id:'outside',lane_index:0,start_frame:0,end_frame:50,members:[]}]}})
 .then(value => saved=value);
const source=document.body.querySelectorAll('select')[0];
source.value='item:outside';
document.body.querySelectorAll('button').find(value => value.textContent === 'Attach')
 ._handlers.click[0]();
await Promise.resolve();
console.log(JSON.stringify({saved,open:document.body.querySelectorAll(
 '[data-sonder-prompt-context-modal]').length > 0}));
""")
    assert result == {"saved": None, "open": True}


def test_frontend_policy_forwarding_and_sparse_controls():
    for file in ["editor_widget.js", "editor_prompt_panel.js"]:
        source = (ROOT / "web/js" / file).read_text(encoding="utf-8")
        assert source.count("configurePromptAttachment(") == source.count("referenceProsePolicy:")
    source = (ROOT / "web/js/prompt_context_chips.js").read_text(encoding="utf-8")
    assert "delete attachment.config.reference_prose" in source
    assert "attachment.config.reference_prose = controls.referenceProse.value" in source
    assert "Out-of-window Reference text (project-wide)" in (ROOT / "web/js/editor_settings_panel.js").read_text(encoding="utf-8")


def test_scope_override_badge_survives_diagnostics_sweep():
    from test_prompt_context_corrections import _run_chip_dom_script
    # Execute the real panel sweep against a marked scope chip.
    panel_source = (ROOT / "web/js/editor_prompt_panel.js").read_text(encoding="utf-8")
    start = panel_source.index('        for (const chip of panel.querySelectorAll("[data-attachment-id]"))')
    end = panel_source.index('        refreshWritingCompiled(payload);', start)
    sweep = panel_source[start:end]
    uri = (ROOT / "web/js/prompt_context_diagnostics.js").as_uri()
    result = _run_chip_dom_script(f"""
const {{promptCapabilityDiagnostics, promptContextDiagnosticTitle}} = await import({json.dumps(uri)});
const COLORS = {{dangerText:'red',warningText:'yellow'}};
const panel = mod.createScopeChipRow({{attachments:[{{attachment_id:'a',kind:'reference',
 config:{{reference_prose:'keep'}},source:{{}}}}],allowedKinds:['reference']}});
const state = {{byAttachment:{{a:[{{tier:'warning',message:'Advisory',origin:''}}]}}}};
const chip = panel.querySelectorAll('button').find(x=>x.dataset.attachmentId==='a');
const title = chip.title;
chip.removeAttribute = (name) => {{ delete chip.attributes[name]; }};
{sweep}
const marker = panel.querySelectorAll('span').find(x=>x.dataset.sonderReferenceProse==='keep');
console.log(JSON.stringify({{base:chip.dataset.sonderDiagnosticBaseTitle,title,
 marker:marker?.textContent, markerOwnsId:Boolean(marker?.dataset.attachmentId),tier:chip.dataset.sonderDiagnosticTier}}));
""")
    assert result["base"] == result["title"]
    assert result["marker"] == "keep" and not result["markerOwnsId"]
    assert result["tier"] == "warning"


@pytest.mark.parametrize("kind,field", [("mentions","text"), ("summary","summary"), ("audio_relationship","audio_relationship")])
def test_preserved_capabilities_report_authored_ordinals(kind, field):
    chip = _reference_chip("kept", {"semantic_unit_ids":["dormant"]},
        {"reference_prose":"keep", field:"the man from <Picture 99>"}, capabilities=(kind,))
    result = _identity_dormancy_fixture(global_chip=chip)
    assert "<Picture 99>" in result["prompt"]
    assert "authored_ordinal_literal" in _codes(result, "warnings")


def test_keep_identity_definition_does_not_hide_member_voice():
    from server.timeline_state import ReferenceMember
    chip = _reference_chip("identity", {"semantic_unit_ids":["dormant"]},
        {"reference_prose":"keep"}, capabilities=("definitions",))
    result = _identity_dormancy_fixture(global_chip=chip,
        catalog_members=[ReferenceMember(member_id="dormant-member", prompt="SOFT VOICE")])
    assert "the dormant subject" in result["prompt"] and "SOFT VOICE" in result["prompt"]


def test_project_policy_setter_uses_ordered_prompt_mutations():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    method = source.split("async _setReferenceProsePolicy(value) {",1)[1].split("async _setReferenceFrameThreshold",1)[0]
    assert "await this._queuePromptProjectWrite(" in method
    assert "this._destroyed || this._projectDirName() !== dirName" in method


def test_editor_node_accepts_unstaged_reference_and_supplies_staging(tmp_path, monkeypatch):
    from test_editor_node import _import_editor_node, _patch_render_and_audio
    node = _import_editor_node(tmp_path, monkeypatch)
    project, scene = _generic_project()
    project.project_dir = str(tmp_path)
    monkeypatch.setattr(node, "load_project", lambda _path: project)
    monkeypatch.setattr(node, "save_project", lambda _project: None)
    _patch_render_and_audio(node, monkeypatch)
    # Package aliases are how node tests import their relative server module.
    import sys
    compiler = prompt_context
    original = compiler.compile_prompt_context
    captured = []
    def capture(**kwargs):
        captured.append(kwargs["context"]["reference_staging"])
        return original(**kwargs)
    monkeypatch.setattr(compiler, "compile_prompt_context", capture)
    node.SonderEditor().execute(project="Existing Project", project_name="Ignored",
        fps=24.0, width=64, height=64, scene_id=scene.scene_id,
        selection_start=0, selection_end=20)
    assert captured and all(row["resolved"] for row in captured)
    assert captured[0]["items"]["item"]["verdict"] == "excluded"
