"""Phase 1 contracts for visible compile failures and cooperative prompt bars."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _method(source: str, name: str, next_name: str) -> str:
    def declaration(method_name: str, offset: int = 0) -> int:
        candidates = [source.find(prefix, offset) for prefix in (
            f"    {method_name}(", f"    async {method_name}(")]
        found = [value for value in candidates if value >= 0]
        if not found:
            raise AssertionError(f"method {method_name} not found")
        return min(found)

    start = declaration(name)
    return source[start:declaration(next_name, start + 1)]


def test_queue_refusals_surface_the_server_detail_for_single_and_batch_jobs():
    widget = _source("web/js/editor_widget.js")
    single = _method(widget, "_addToRenderQueue", "_addBatchToRenderQueue")
    batch = _method(widget, "_addBatchToRenderQueue", "_fetchRenderQueue")
    for method in (single, batch):
        assert "failureDetail: (error) => [error?.payload?.code || error?.code, error?.message]" in method
        assert '.filter(Boolean).join(": ")' in method


def test_inline_attachment_transactions_keep_every_prompt_bar_open():
    widget = _source("web/js/editor_widget.js")
    builder = _method(widget, "_buildChannelInputs", "_showPromptCreator")
    assert builder.count("onEnter?.({ close: false })") == 9
    global_bar = _method(widget, "_showGlobalPromptEditor", "_updateScenePrompt")
    assert "({ close = true } = {})" in global_bar
    assert "if (close) this._hidePromptEditor();" in global_bar


def test_time_is_template_gated_and_subjects_are_a_visible_list():
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert panel.count('template.shot_marker_channel\n                        ? ["shot", "timestamp"') >= 1
    assert 'data.sonderSubjectList' not in panel
    assert 'subjectList.dataset.sonderSubjectList = "1"' in panel
    assert 'badge.textContent = unit.generated ? "Reference-generated" : "authored"' in panel
    assert 'removeRow.disabled = true' in panel
    assert 'unit.generated_reference_name' in panel
    assert 'template.shot_marker_channel\n                        ? ["shot", "timestamp"' in widget


def test_linked_edits_use_exact_identity_snapshots_in_one_noncoalesced_batch():
    widget = _source("web/js/editor_widget.js")
    method = _method(widget, "_updateLinkedPromptAttachment", "_deletePromptSection")
    for identity_field in (
        "prompt_id", "start_frame", "end_frame", "prompt", "muted",
        "channels", "channel_docs", "attachments",
        "global_channel_exceptions",
    ):
        assert identity_field in method
    assert 'type: "update_prompt_section"' in method
    assert "coalesce: false" in method
    assert "retryOnConflict: false" in method


def test_prompt_diagnostics_use_stable_scrollable_geometry():
    panel = _source("web/js/editor_prompt_panel.js")
    assert "height:72px;overflow-y:auto;box-sizing:border-box" in panel


def test_prompt_tool_rebuilds_scope_rows_after_attachment_transactions():
    panel = _source("web/js/editor_prompt_panel.js")
    assert "renderGlobalScope();\n                            commitGlobal()" in panel
    assert "renderScope();\n                                commitChannels()" in panel


def test_context_actions_are_named_by_inline_vs_scope_semantics():
    chips = _source("web/js/prompt_context_chips.js")
    assert 'label: "Insert at cursor"' in chips
    assert 'label: "Writing aid"' in chips
    assert "export function installPromptContextMenu" in chips
    assert "createContextPicker" not in chips
    assert 'add.textContent = "Attach to this section/scene";' in chips
    scope = chips[chips.index("export function createScopeChipRow"):]
    assert "add.title" not in scope


def test_writing_grip_and_prompt_panel_use_the_advertised_space():
    chips = _source("web/js/prompt_context_chips.js")
    panel = _source("web/js/editor_prompt_panel.js")
    assert 'max-height:${compact ? "100px" : "none"}' in chips
    assert "flex:0 0 auto;" in chips
    assert "function promptBox" not in panel
    for key in ("panelDraftBoxHeight", "panelGlobalBoxHeight", "panelChannelBoxHeight"):
        assert f'applyBoxHeight(host, ' in panel
        assert f'"{key}"' in panel
    assert 'width: "min(1320px, 96vw)"' in panel
    assert 'maxWidth: "1320px"' in panel


def test_prompt_panel_consumes_only_windowed_candidate_diagnostics():
    panel = _source("web/js/editor_prompt_panel.js")
    widget = _source("web/js/editor_widget.js")
    assert "_promptPayloadCache" not in panel
    assert "currentCandidatePayload()?.attachment_previews" in panel
    assert "refreshDiagnostics: renderDiagnostics" in panel
    assert "_candidate_scene_id: sceneId" in widget
    assert "const candidateSelection = resolvePromptCandidateSelection(" in widget
    assert "selection_start: candidateSelection.selectionStart" in widget
    assert "selection_end: candidateSelection.selectionEnd" in widget
    assert "this._promptPayloadCache = payload" not in widget


def test_mounted_prompt_consumers_request_and_reconcile_dependencies():
    widget = _source("web/js/editor_widget.js")
    assert widget.count("if (this._promptContextConsumersMounted())") == 5
    show_panel = _method(widget, "_showPromptManagementPanel", "_channelTemplateSwitchImpact")
    assert "if (this._promptPanelHandle?.isMounted?.())" in show_panel
    builder = _method(widget, "_buildChannelInputs", "_showPromptCreator")
    assert "scheduleCandidatePreview();\n        return { wrap" in builder
    assert 'inputs[key] ? inputs[key].value.trim() : (channels[key] || "")' in builder
    assert "const snapshot = draftSceneSnapshot();" in builder
    reference_apply = _method(widget, "_applyReferencePayload", "_promptContextConsumersMounted")
    assert "this._refreshPromptContextDependencyConsumers();" in reference_apply
    replay = _method(widget, "_replayDeferredProjectBackedRefresh", "_schedulePostMutationSceneRefresh")
    assert "if (this.isDragging || this._timelineMutationDepth > 0)" in replay
    assert "this._pendingProjectRefreshDrain = false;" in replay
    drag_replay = _method(widget, "_flushDeferredDragState", "_shouldDeferSceneRefresh")
    assert "this._replayDeferredProjectBackedRefresh();" in drag_replay
    timeline_commit = _method(widget, "_withTimelineMutationCommit", "_buildDOM")
    assert "if (this._timelineMutationDepth === 0)" in timeline_commit
    project_update = _method(widget, "updateProject", "refresh")
    assert 'this._fetchReferences({ reason: "load_project", force: true })' in project_update


def test_fullscreen_background_paste_preserves_gallery_image_path():
    widget = _source("web/js/editor_widget.js")
    gallery = _source("web/js/shared_asset_gallery.js")
    keyboard_registration = widget[widget.index("this._editorKeyOff = registerKeyboardConsumer"):
        widget.index("// Track editor focus")]
    assert 'root.dataset.sonderAssetGallery = "1"' in gallery
    assert 'candidate?.closest?.("[data-sonder-asset-gallery=\'1\']")' in keyboard_registration
    assert 'String(item?.type || "").startsWith("image/")' in keyboard_registration
    assert "if (galleryImagePaste) return false;" in keyboard_registration
    assert "if (editable) return false;" in keyboard_registration
    assert "return true;" in keyboard_registration
    assert "notifyWarning" not in keyboard_registration
    assert 'source: "fullscreen-background-paste"' not in keyboard_registration
    assert "Paste needs a text field" not in keyboard_registration
    assert "fullscreen background paste is ignored" in widget


def test_prompt_panel_writing_and_global_views_use_bound_shared_overlays():
    panel = _source("web/js/editor_prompt_panel.js")
    writing = panel[panel.index("const renderWritingView"):panel.index("const renderContextSettings")]
    assert "attachmentContext: { scene, template: host._channelTemplate() }" in writing
    assert "attachmentContext: { scene, template }," not in writing
    assert "const draftGlobalSceneSnapshot = () => sceneWithDraftGlobal(scene" in panel
    assert "const snapshot = draftGlobalSceneSnapshot();" in panel


def test_candidate_diagnostic_projection_keys_chip_errors_and_labels_window():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Context diagnostics coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_diagnostics.js").as_uri()
    payload = {
        "execution_window": {
            "render_start": 8,
            "render_end": 56,
            "generation_start": 16,
            "generation_end": 48,
            "actual_pre": 8,
            "actual_post": 8,
        },
        "errors": [
            {"code": "unresolved_audio_speaker_binding", "message": "Choose a managed speaker.",
             "attachment_id": "chip-a"},
            {"code": "profile_error", "message": "Profile is invalid."},
        ],
        "warnings": [
            {"code": "quiet", "message": "Check this chip.", "attachment_id": "chip-a"},
        ],
    }
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"console.log(JSON.stringify(mod.buildPromptContextDiagnostics({json.dumps(payload)})));\n"
    )
    projected = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert projected["windowLabel"] == (
        "Effective render window: frames 8–56 (selection 16–48; pre 8f, post 8f).")
    assert [row["code"] for row in projected["byAttachment"]["chip-a"]] == [
        "unresolved_audio_speaker_binding", "quiet"]
    assert [row["code"] for row in projected["general"]] == ["profile_error"]
    assert projected["errorCount"] == 2
    assert projected["warningCount"] == 1


def test_candidate_selection_matches_full_scene_queue_semantics():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Context selection coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_diagnostics.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        "console.log(JSON.stringify(["
        "mod.resolvePromptCandidateSelection(0,0,96),"
        "mod.resolvePromptCandidateSelection(12,48,96),"
        "mod.resolvePromptCandidateSelection(120,-4,96)]));\n"
    )
    values = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert values == [
        {"selectionStart": 0, "selectionEnd": 96},
        {"selectionStart": 12, "selectionEnd": 48},
        {"selectionStart": 0, "selectionEnd": 96},
    ]


def test_live_prompt_draft_overlays_preserve_section_identity_and_global_scope():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt draft overlay coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    scene = {
        "prompt_sections": [{
            "prompt_id": "one", "start_frame": 0, "end_frame": 20,
            "muted": True, "global_channel_exceptions": ["speech"],
            "attachments": [{"attachment_id": "old", "kind": "custom"}],
        }],
        "global_attachments": [{"attachment_id": "global-old", "kind": "custom"}],
    }
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"const scene = {json.dumps(scene)};\n"
        "const section = mod.sceneWithDraftSection(scene, {index:0, promptId:'one', "
        "startFrame:4, endFrame:24, attachments:[{attachment_id:'new',kind:'custom'}], "
        "channels:{visual:'draft'}, channelDocs:{visual:{nodes:[]}}});\n"
        "const fallback = mod.sceneWithDraftSection(scene, {promptId:'one', "
        "startFrame:99, endFrame:100, attachments:[]});\n"
        "const identityOnly = mod.sceneWithDraftSection(scene, {promptId:'one', attachments:[]});\n"
        "const global = mod.sceneWithDraftGlobal(scene, {"
        "attachments:[{attachment_id:'global-new',kind:'custom'}]});\n"
        "console.log(JSON.stringify({section, fallback, identityOnly, global}));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True,
    ).stdout)
    overlaid = result["section"]["prompt_sections"]
    assert len(overlaid) == 1
    assert overlaid[0]["muted"] is True
    assert overlaid[0]["global_channel_exceptions"] == ["speech"]
    assert overlaid[0]["start_frame"] == 4
    assert overlaid[0]["attachments"][0]["attachment_id"] == "new"
    assert len(result["fallback"]["prompt_sections"]) == 1
    assert result["identityOnly"]["prompt_sections"][0]["start_frame"] == 0
    assert result["identityOnly"]["prompt_sections"][0]["end_frame"] == 20
    assert len(result["global"]["prompt_sections"]) == 1
    assert result["global"]["global_attachments"][0]["attachment_id"] == "global-new"


def test_new_section_creator_defers_chip_transactions_until_explicit_commit():
    widget = _source("web/js/editor_widget.js")
    creator = _method(widget, "_showPromptCreator", "_saveNewPromptSection")
    assert "const commit = ({ close = true } = {}) =>" in creator
    assert "if (!close) return;" in creator
    assert creator.index("if (!close) return;") < creator.index("this._saveNewPromptSection(")


def test_reference_chip_identity_inheritance_and_vocal_target_window_are_dynamic():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Reference chip authoring coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    references = [{
        "reference_id": "woman", "name": "Korean Woman", "members": [{
            "member_id": "portrait", "name": "Portrait",
            "prompt": "a poised woman in a blue coat",
        }, {
            "member_id": "inactive", "name": "Unused portrait",
            "prompt": "prose from a non-winning member",
        }],
    }]
    scene = {
        "duration_frames": 20,
        "reference_lane_count": 1,
        "reference_lane_configs": [{}],
        "reference_lane_recipes": [{"lane_id": "pictures"}],
        "active_minimax_h3_setup_id": "setup",
        "minimax_h3_conditioning_setups": [{
            "setup_id": "setup", "mode": "reference",
            "picture_lane_ids": ["pictures"],
        }],
        "reference_items": [{"reference_item_id": "item", "members": [{
            "entity_id": "woman", "member_id": "portrait"}]}],
        "prompt_sections": [
            {"start_frame": 0, "end_frame": 10, "attachments": [{
                "kind": "vocal_event", "source": {"subject_ids": ["subject"]}}]},
            {"start_frame": 10, "end_frame": 20, "attachments": [{
                "kind": "vocal_event", "source": {"subject_ids": ["later"]}}]},
            {"start_frame": 0, "end_frame": 10, "muted": True,
             "attachments": [{"kind": "vocal_event",
                              "source": {"subject_ids": ["muted"]}}]},
        ],
    }
    units = [{
        "semantic_unit_id": "subject", "name": "Korean Woman", "definition": "",
        "source_members": [
            {"entity_id": "woman", "member_id": "portrait"},
            {"entity_id": "woman", "member_id": "inactive"},
        ],
    }]
    attachment = {"kind": "reference", "source": {
        "semantic_unit_ids": ["subject"]}, "config": {"label": "stale label"}}
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"const refs = {json.dumps(references)};\n"
        f"const scene = {json.dumps(scene)};\n"
        f"const units = {json.dumps(units)};\n"
        f"const attachment = {json.dumps(attachment)};\n"
        "const options = {scene, references: refs, semanticUnits: units};\n"
        "const identity = mod.resolveReferenceAttachmentIdentity(attachment, options);\n"
        "const inherited = mod.resolveReferenceSelectionInheritance('subject', options);\n"
        "const physicalInherited = mod.resolveReferenceSelectionInheritance("
        "'physical:picture:portrait', options);\n"
        "const label = mod.attachmentLabel(attachment, '<Subject 1> preview', identity);\n"
        "console.log(JSON.stringify({identity, inherited, physicalInherited, label}));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert result == {
        "identity": "Korean Woman",
        "inherited": {
            "value": "a poised woman in a blue coat",
            "source": "Library member Korean Woman · Portrait",
        },
        "physicalInherited": {"value": "", "source": ""},
        "label": "Korean Woman — <Subject 1> preview",
    }


def test_every_reference_chip_surface_uses_runtime_identity_and_authoring_controls():
    chips = _source("web/js/prompt_context_chips.js")
    widget = _source("web/js/editor_widget.js")
    panel = _source("web/js/editor_prompt_panel.js")
    label_function = chips[chips.index(
        "export function attachmentLabel"):chips.index("function chipCss")]
    reference_case = label_function[:label_function.index(
        'if (attachment?.kind === "prompt_link")')]
    assert "attachment?.config?.label" not in reference_case
    assert widget.count("attachmentLabelFor,") == 3
    assert panel.count("attachmentLabelFor,") == 8
    assert "Inherited from ${inherited.source}" in chips
    assert "no managed Vocal Event in this window" in chips
    assert "managedVocalEventSubjectIds" not in chips
    assert widget.count("managedSpeakerSubjectIds:") == 3
    assert panel.count("managedSpeakerSubjectIds:") == 7
    assert 'referenceFieldRow("Summary task types", controls.taskTypes' in chips
    assert "const routeKey = String(current.kind || capabilityId);" in chips
    assert "declaration?.routes?.[routeKey]" in chips
    assert "else delete capability.channel_key;" in chips
    assert "Creative definition (never generated)" not in panel


def test_reference_backed_subject_dependency_audit_and_delete_guard_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Subject dependency audit coverage")
    panel = _source("web/js/editor_prompt_panel.js")
    start = panel.index("export function referenceBackedSubjectDependents")
    end = panel.index("/** Split a writing-mode draft", start)
    function_source = panel[start:end].replace("export function", "function", 1)
    scenes = [{
        "global_attachments": [{
            "source": {"semantic_unit_ids": ["unit:authored"]},
            "config": {"audio_speaker_subject_id": "unit:authored"},
        }],
        "prompt_sections": [{"attachments": [{
            "source": {"subject_ids": ["unit:authored"]},
        }]}],
    }]
    script = (
        f"{function_source}\n"
        f"console.log(JSON.stringify(referenceBackedSubjectDependents("
        f"'unit:authored', {json.dumps(scenes)})));\n"
    )
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    assert result == {
        "reference_chips": 1, "vocal_events": 1,
        "audio_speaker_bindings": 1,
    }
    assert "if (!current || current.generated) return false;" in panel
    assert "void deleteAuthoredSubject(button.dataset.deleteSubject)" in panel
    assert "They will remain visible as broken links" in panel
    assert "may renumber late-bound subject ordinals" in panel
    assert "save.disabled = !hasMembers" in panel
    assert "Remove the owning Reference" in panel


def test_prompt_context_pure_helpers_pin_eligibility_reuse_projection_and_suppression():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for prompt Context helper coverage")
    module_url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const base = {{
  attachment_id: "a-late", emission_group_id: "shared", kind: "custom",
  enabled: true, config: {{text: "soft blue edge light"}},
  capabilities: [{{capability_id: "prefix", kind: "custom", placement: "section_prefix", enabled: true}}]
}};
const early = {{...structuredClone(base), attachment_id: "a-early"}};
const scene = {{prompt_sections: [
  {{prompt_id: "later", start_frame: 20, attachments: [base]}},
  {{prompt_id: "early", start_frame: 0, attachments: [early]}}
]}};
const deduped = mod.dedupeReusableAttachments([base, early], {{scene}});
const labels = [
  mod.attachmentReuseLabel({{kind: "reference"}}),
  mod.attachmentReuseLabel({{kind: "shot", config: {{timestamp: true}}}}),
  mod.attachmentReuseLabel({{kind: "timestamp"}}),
  mod.attachmentReuseLabel(base),
  mod.attachmentReuseLabel({{kind: "prompt_link", source: {{prompt_id: "early", channel_key: "visual"}}}}, {{scene, template: {{channels: [{{key: "visual", label: "Visual"}}]}}}}),
  mod.attachmentReuseLabel({{kind: "prompt_link"}}),
  mod.attachmentReuseLabel({{kind: "guide", config: {{text: "first frame"}}}}),
  mod.attachmentReuseLabel({{kind: "vocal_event", config: {{event_type: "speech"}}}})
];
const configured = {{...structuredClone(base), capabilities: [{{...base.capabilities[0], enabled: true}}]}};
const target = {{...structuredClone(base), attachment_id: "target", capabilities: [{{...base.capabilities[0], enabled: false}}]}};
const propagated = mod.propagateLinkedPromptAttachment(configured, target, "a-late");
const source = mod.propagateLinkedPromptAttachment(configured, base, "a-late");
const suppressed = mod.setPromptAttachmentCapabilityEnabled(base, {{capability_id: "prefix"}}, false);
const eligibility = mod.subjectSourceEligibility({{sources: [{{member_id: "not-staged"}}]}});
const split = mod.splitCapabilityProjectionsByRegion([
  {{capability_id: "suffix", region: "after", order: 2}},
  {{capability_id: "prefix", region: "before", order: 1}}
]);
console.log(JSON.stringify({{
  deduped: deduped.map((row) => [row.attachment.attachment_id, row.count]),
  labels,
  propagatedEnabled: propagated.capabilities[0].enabled,
  sourceEnabled: source.capabilities[0].enabled,
  suppressedEnabled: suppressed.capabilities[0].enabled,
  eligibility,
  split: {{before: split.before.map((row) => row.capability_id), after: split.after.map((row) => row.capability_id)}}
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)

    assert result["deduped"] == [["a-early", 2]]
    assert len(result["labels"]) == 8
    assert result["labels"][5] == "Linked prompt"
    assert all(label and "a-early" not in label and "a-late" not in label
               for label in result["labels"])
    assert result["propagatedEnabled"] is False
    assert result["sourceEnabled"] is True
    assert result["suppressedEnabled"] is False
    assert result["eligibility"] == {
        "eligible": False,
        "appliesNow": False,
        "reason": "not staged in this scene",
        "suffix": " - not staged in this scene",
    }
    assert result["split"] == {"before": ["prefix"], "after": ["suffix"]}
