import pytest

from server import prompt_context
from test_prompt_context_corrections import _run_chip_dom_script


def compile_subject(*, chip=True, definitions=True, dormant=False, detail="", prose="@Hero moves beside @HERO.",
                    profile=None, extra_sections=()):
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "hero-chip", "kind": "reference",
        "source": {"semantic_unit_ids": ["hero"]},
        "config": {"overrides": {"retention_detail": detail}},
        "capabilities": [
            {"capability_id": "definitions", "kind": "definitions", "enabled": definitions,
             "channel_key": "subject_definitions", "placement": "section_prefix"},
            {"capability_id": "retention", "kind": "retention",
             "channel_key": "retention_analysis", "placement": "section_prefix"},
        ],
    })
    context = {
        "setup_manifest": {"setup": {"mode": "reference"}},
        "ordinal_manifest": {"subjects": {"hero": 1}},
        "unit_source_labels": {"hero": ["<Picture 1>"]},
        "semantic_units": [{"semantic_unit_id": "hero", "name": "Hero", "handle": "Hero",
                            "definition": "a cowboy", "sources": [
                                {"entity_id": "ref", "member_id": "portrait"}]}],
    }
    sections = [{"prompt_id": "live", "start_frame": 0, "end_frame": 24,
                 "channels": {"detailed_description": prose},
                 "attachments": [attachment] if chip and not dormant else []}]
    if dormant:
        sections.append({"prompt_id": "dormant", "start_frame": 48, "end_frame": 72,
                         "channels": {"detailed_description": "other scene"},
                         "attachments": [attachment]})
    sections.extend(extra_sections)
    return prompt_context.compile_prompt_context(
        sections=sections, context=context, template="minimax_h3_ref",
        window_start=0, window_end=24, fps=24, **({"profile": profile} if profile else {}))


# `dormant` puts the only chip in a section outside the window, which the
# compile never selects: it is the out-of-window case, not an in-window one.
@pytest.mark.parametrize("options", [{"chip": False}, {"definitions": False}, {"dormant": True}])
def test_mentioned_subject_needs_an_emitted_definition(options):
    result = compile_subject(**options)
    warnings = [row for row in result["warnings"] if row["code"] == "mentioned_subject_without_definition"]
    assert len(warnings) == 1
    assert warnings[0]["origin"] == "live"
    assert warnings[0]["channel_key"] == "detailed_description"
    assert warnings[0]["semantic_unit_id"] == "hero"
    assert "Reference chip" in warnings[0]["message"]
    assert "<Subject 1>" in result["channels"]["detailed_description"]
    assert not result["errors"]


def test_mentioned_subject_warns_once_however_often_it_is_mentioned():
    result = compile_subject(chip=False, prose="@Hero waits.", extra_sections=[
        {"prompt_id": "later", "start_frame": 12, "end_frame": 24,
         "channels": {"summary": "@Hero leaves.", "detailed_description": "@Hero runs."}}])
    warnings = [row for row in result["warnings"] if row["code"] == "mentioned_subject_without_definition"]
    assert [(row["origin"], row["semantic_unit_id"]) for row in warnings] == [("live", "hero")]


def test_format_without_definitions_never_asks_for_one():
    import copy
    profile = copy.deepcopy(prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    del profile["capabilities"]["reference"]["derived"]["definitions"]
    result = compile_subject(chip=False, profile=profile)
    assert "<Subject 1>" in result["channels"]["detailed_description"]
    assert "mentioned_subject_without_definition" not in {row["code"] for row in result["warnings"]}


def test_voice_definition_line_defines_its_subject():
    segments = [
        {"kind": prompt_context.SEGMENT_LABEL, "text": "<Audio 1>"},
        {"kind": prompt_context.SEGMENT_TEXT, "text": " is a gravelly voice"},
        {"kind": prompt_context.SEGMENT_SPEAKER, "text": "", "subject_id": "hero"},
    ]
    assert prompt_context.defined_unit_ids(("audio_definition", "<Audio 1>"), segments) == {"hero"}
    assert prompt_context.defined_unit_ids(("subject_definition", "hero"), [
        {"kind": prompt_context.SEGMENT_TEXT, "text": "  "}]) == set()
    assert prompt_context.defined_unit_ids(("physical_definition", "picture", "m"), segments) == set()


def test_retention_fallback_labels_match_the_chip_editor():
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "web/js/prompt_context_chips.js").read_text(encoding="utf-8")
    table = re.search(r"const REFERENCE_VALUE_LABELS = Object\.freeze\(\{(.*?)\}\);", source, re.S).group(1)
    labels = dict(re.findall(r'(\w+): "([^"]*)"', table))
    for field, label in prompt_context.RETENTION_FIELD_LABELS.items():
        assert labels[field] == label


def test_emitted_definition_satisfies_mention_and_authored_detail_satisfies_retention():
    result = compile_subject(detail="Keep the cowboy's coat and face.")
    assert "<Subject 1> is a cowboy from <Picture 1>" in result["channels"]["subject_definitions"]
    assert not ({"mentioned_subject_without_definition", "missing_h3_retention_detail"}
                & {row["code"] for row in result["warnings"]})


def test_retention_warning_names_label_field_and_chip():
    result = compile_subject()
    warning = next(row for row in result["warnings"] if row["code"] == "missing_h3_retention_detail")
    assert warning["attachment_id"] == "hero-chip"
    assert warning["field"] == "retention_detail"
    assert '<Subject 1>' in warning["message"]
    assert '"Preservation detail"' in warning["message"]
    assert not result["errors"]


def test_missing_subject_binding_is_not_retargeted():
    result = prompt_context.compile_prompt_context(
        sections=[{"prompt_id": "live", "start_frame": 0, "end_frame": 24,
                   "attachments": [{"attachment_id": "stale", "kind": "reference",
                                    "source": {"semantic_unit_ids": ["unit:old-reference"]}}]}],
        context={"setup_manifest": {"setup": {"mode": "reference"}},
                 "semantic_units": [{"semantic_unit_id": "new-identity", "name": "Old Reference"}]},
        template="minimax_h3_ref", window_start=0, window_end=24, fps=24)
    assert "broken_reference_source" in {row["code"] for row in result["errors"]}


def test_no_synthetic_subject_options():
    result = _run_chip_dom_script(r'''
        const profile = { identity_kinds: [{ key: "subject" }], physical_populations: [] };
        const sources = mod.promptReferenceSourceOptions({ references: [{ reference_id: "ref", name: "Hero" }],
            semanticUnits: [], resolvedProfile: profile });
        console.log(JSON.stringify({ units: sources.unitOptions, declared: sources.usesDeclaredSources }));
    ''')
    assert result == {"units": [], "declared": True}


def test_guidance_is_a_sibling_and_tracks_model_composition_and_chips():
    result = _run_chip_dom_script(r'''
        const editor = mod.createPromptDocumentEditor({ placeholder: "Write the scene" });
        const wrapper = mod.createPromptProjectionBox(editor, document.createElement("div"), document.createElement("div"));
        const hint = editor.promptPlaceholder;
        const shown = () => hint.style.display !== "none" && hint.style.visibility !== "hidden";
        // Focused: the hidden guidance still holds the box's height.
        const holds = () => hint.style.display !== "none" && hint.style.visibility === "hidden";
        const empty = shown();
        const sibling = hint.parentElement === editor.parentElement && !editor.contains(hint);
        for (const fn of editor._handlers.compositionstart) fn({});
        const composing = shown();
        for (const fn of editor._handlers.compositionend) fn({});
        const ended = shown();
        editor.value = "authored";
        const typed = shown();
        document.activeElement = editor;
        for (const fn of editor._handlers.focus) fn({});
        const focusedTyped = holds();
        document.activeElement = document.body;
        for (const fn of editor._handlers.blur) fn({});
        const blurredTyped = hint.style.display;
        editor.value = "";
        const cleared = shown();
        editor.promptState = { document: { nodes: [{ type: "attachment", node_id: "n", attachment_id: "chip" }] },
            attachments: [{ attachment_id: "chip", kind: "reference", source: {}, config: {} }] };
        const chip = shown();
        const labelled = mod.createPromptDocumentEditor({ placeholder: "Same", ariaLabel: "Same" });
        console.log(JSON.stringify({empty,sibling,composing,ended,typed,focusedTyped,blurredTyped,cleared,chip,
            text: editor.value, description: editor.getAttribute("aria-describedby") === hint.id,
            hidden: hint.getAttribute("aria-hidden"), noRepeat: labelled.getAttribute("aria-describedby")}));
    ''')
    assert result == dict(empty=True, sibling=True, composing=False, ended=True, typed=False,
                          focusedTyped=True, blurredTyped="none", cleared=True, chip=False, text="",
                          description=True, hidden="true", noRepeat=None)


@pytest.mark.parametrize("config", [
    {"retention_detail": "Keep the red coat"},
    {"retention_details": {"<Picture 1>": "red coat", "<Picture 2>": "blue suit"}},
])
def test_marker_free_preserved_prose_never_requests_marker_detail(config):
    from test_minimax_h3_format_coverage import _identity_dormancy_fixture, _reference_chip
    chip = _reference_chip("physical", {"picture_ids": ["dormant-member"]},
                           {"reference_prose": "keep", **config}, capabilities=("retention",))
    result = _identity_dormancy_fixture(global_chip=chip)
    assert not result["errors"]
    assert "red coat" in result["prompt"]
    assert "missing_h3_retention_detail" not in {row["code"] for row in result["warnings"]}


def test_template_keyboard_preview_commits_once_and_mouse_commits_immediately(tmp_path):
    from test_reference_panel_item_writes_js import run_item_panel
    result = run_item_panel(r'''
        w._referenceRecipePresets = [
            {id:'a',name:'Image A',media_kind:'image',builtIn:true},
            {id:'b',name:'Image B',media_kind:'image',builtIn:true},
            {id:'c',name:'Image C',media_kind:'image',builtIn:true},
            {id:'audio',name:'Voice',media_kind:'audio',builtIn:true},
            {id:'legacy',name:'Legacy image',builtIn:true},
        ];
        const writes = [];
        w._saveLaneConfig = async (entries) => {
            writes.push(entries[0].referenceRecipe.recipe_id);
            w.activeScene.reference_lane_recipes[0] = entries[0].referenceRecipe;
        };
        mount();
        const picker = () => nodes().find(n => n.dataset.sonderReferenceTemplate === '1');
        let select = picker();
        const audio = select.options.find(o => o.value === 'audio');
        const legacy = select.options.find(o => o.value === 'legacy');
        const disabled = audio.disabled && /audio.*image/.test(audio.textContent) && !legacy.disabled;
        select.focus();
        for (const value of ['a','b','c']) {
            select._sonderTemplateKeydown({key:'ArrowDown'});
            select.value = value; select.dispatch('change');
        }
        const beforeCommit = writes.length;
        select._sonderTemplateKeydown({key:'Enter'});
        await settle();
        const afterEnter = writes.slice();
        select = picker();
        select._sonderTemplateKeydown({key:'ArrowUp'});
        select.value = 'b'; select.dispatch('change'); select.dispatch('blur');
        await settle();
        select = picker();
        select.dispatch('pointerdown'); select.value = 'a'; select.dispatch('change');
        await settle();
        return {disabled,beforeCommit,afterEnter,writes};
    ''', tmp_path)
    assert result == dict(disabled=True, beforeCommit=0, afterEnter=['c'], writes=['c','b','a'])


def test_exported_prompt_link_definition_satisfies_a_mentioned_subject():
    context = {
        "setup_manifest": {"setup": {"mode": "reference"}},
        "ordinal_manifest": {"subjects": {"hero": 1}},
        "unit_source_labels": {"hero": ["<Picture 1>"]},
        "semantic_units": [{"semantic_unit_id": "hero", "name": "Hero", "handle": "Hero",
                            "definition": "a cowboy", "sources": [
                                {"entity_id": "ref", "member_id": "portrait"}]}],
    }
    definition = {"attachment_id": "def", "kind": "reference", "link_exportable": True,
                  "source": {"semantic_unit_ids": ["hero"]}, "capabilities": [
                      {"capability_id": "definitions", "kind": "definitions", "enabled": True,
                       "channel_key": "subject_definitions", "placement": "inline"}]}
    link = {"attachment_id": "link", "kind": "prompt_link",
            "source": {"prompt_id": "early", "channel_key": "subject_definitions"}}
    sections = [
        {"prompt_id": "early", "start_frame": 0, "end_frame": 24,
         "channel_docs": {"subject_definitions": {"nodes": [
             {"type": "attachment", "node_id": "d", "attachment_id": "def", "capability_id": "definitions"}]}},
         "attachments": [definition]},
        {"prompt_id": "live", "start_frame": 24, "end_frame": 48,
         "channel_docs": {"subject_definitions": {"nodes": [
             {"type": "attachment", "node_id": "l", "attachment_id": "link"}]},
                          "detailed_description": {"nodes": [{"type": "text", "text": "@Hero walks."}]}},
         "attachments": [link]},
    ]
    result = prompt_context.compile_prompt_context(
        sections=sections, context=context, template="minimax_h3_ref", window_start=24, window_end=48, fps=24)
    assert not result["errors"]
    assert result["channels"]["subject_definitions"] == "<Subject 1> is a cowboy from <Picture 1>"
    assert result["channels"]["detailed_description"] == "<Subject 1> walks."
    assert "mentioned_subject_without_definition" not in {row["code"] for row in result["warnings"]}


def test_retention_warning_follows_custom_format_field_label():
    import copy
    profile = copy.deepcopy(prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    profile["capabilities"]["reference"]["derived"]["retention"].setdefault("fields", {})["retention_detail"] = {
        "type": "enum", "values": ["Keep the face"], "label": "Keep these features"}
    result = prompt_context.compile_prompt_context(
        sections=[{"prompt_id": "live", "start_frame": 0, "end_frame": 24,
                   "attachments": [{"attachment_id": "retain", "kind": "reference",
                                    "source": {"semantic_unit_ids": ["hero"]}, "capabilities": [
                                        {"capability_id": "retention", "kind": "retention",
                                         "channel_key": "retention_analysis", "placement": "section_prefix"}]}]}],
        context={"setup_manifest": {"setup": {"mode": "reference"}},
                 "ordinal_manifest": {"subjects": {"hero": 1}},
                 "unit_source_labels": {"hero": ["<Picture 1>"]},
                 "semantic_units": [{"semantic_unit_id": "hero", "handle": "Hero", "definition": "a cowboy",
                                     "sources": [{"entity_id": "ref", "member_id": "portrait"}]}]},
        profile=profile, template="minimax_h3_ref", window_start=0, window_end=24, fps=24)
    assert not result["errors"]
    warning = next(row for row in result["warnings"] if row["code"] == "missing_h3_retention_detail")
    assert '"Keep these features"' in warning["message"]


def test_pending_template_selection_survives_same_lane_refresh(tmp_path):
    from test_reference_panel_item_writes_js import run_item_panel
    result = run_item_panel(r'''
        w._referenceRecipePresets = [
            {id:'a',name:'Image A',media_kind:'image',builtIn:true},
            {id:'b',name:'Image B',media_kind:'image',builtIn:true},
        ];
        const writes = [];
        w._saveLaneConfig = async entries => {
            writes.push(entries[0].referenceRecipe.recipe_id);
            w.activeScene.reference_lane_recipes[0] = entries[0].referenceRecipe;
        };
        mount();
        const picker = () => nodes().find(n => n.dataset.sonderReferenceTemplate === '1');
        let select = picker();
        select.focus();
        select._sonderTemplateKeydown({key:'ArrowDown'});
        select.value = 'b'; select.dispatch('change');
        handle.refresh();
        select = picker();
        const restored = select.value;
        select._sonderTemplateKeydown({key:'Enter'});
        await settle();
        return {restored,writes};
    ''', tmp_path)
    assert result == {"restored": "b", "writes": ["b"]}


def test_template_escape_reverts_and_leaving_commits_what_is_shown(tmp_path):
    from test_reference_panel_item_writes_js import run_item_panel
    result = run_item_panel(r'''
        w._referenceRecipePresets = [
            {id:'a',name:'Image A',media_kind:'image',builtIn:true},
            {id:'b',name:'Image B',media_kind:'image',builtIn:true},
        ];
        const writes = [];
        w._saveLaneConfig = async entries => {
            writes.push(entries[0].referenceRecipe.recipe_id);
            w.activeScene.reference_lane_recipes[0] = entries[0].referenceRecipe;
        };
        mount();
        const picker = () => nodes().find(n => n.dataset.sonderReferenceTemplate === '1');
        let select = picker();
        const start = select.value;
        select.focus();
        // Escape with a pending pick reverts it and keeps the panel open.
        select._sonderTemplateKeydown({key:'ArrowDown'});
        select.value = 'a'; select.dispatch('change');
        const claimed = select._sonderTemplateKeydown({key:'Escape'});
        const reverted = select.value === start;
        select.dispatch('blur');
        await settle();
        const afterEscape = writes.slice();
        // A pending pick re-chosen with the pointer fires no change; leaving
        // the select still commits the value it shows.
        select._sonderTemplateKeydown({key:'ArrowDown'});
        select.value = 'a'; select.dispatch('change');
        select.dispatch('pointerdown');
        select.dispatch('blur');
        await settle();
        const afterRepick = writes.slice();
        // Space and Alt+Arrow open the popup; they are not previews, so the
        // popup's pick commits at once.
        select = picker();
        select._sonderTemplateKeydown({key:' '});
        select._sonderTemplateKeydown({key:'ArrowDown', altKey:true});
        select.value = 'b'; select.dispatch('change');
        await settle();
        const afterPopup = writes.slice();
        // Closing by the backdrop keeps a pending pick.
        select = picker();
        select._sonderTemplateKeydown({key:'ArrowUp'});
        select.value = 'a'; select.dispatch('change');
        const backdrop = handle.element;
        const openBefore = backdrop.isConnected;
        backdrop.dispatch('mousedown', {target: backdrop});
        await settle();
        return {claimed, reverted, afterEscape, afterRepick, afterPopup, writes,
            closed: openBefore && !backdrop.isConnected};
    ''', tmp_path)
    assert result == {"claimed": True, "reverted": True, "afterEscape": [], "afterRepick": ["a"],
                      "afterPopup": ["a", "b"], "writes": ["a", "b", "a"], "closed": True}


def test_retention_diagnostic_expands_and_focuses_its_field_without_writing():
    result = _run_chip_dom_script(r'''
        Object.getPrototypeOf(document.body).focus = function () { document.activeElement = this; };
        const profile = {profile_id:'test@1',identity_kinds:[{key:'subject'}],
            capabilities:{reference:{derived:{retention:{order:1,channel_key:'retention_analysis',
                placement:'section_prefix',label:'Preservation'}}}}};
        const attachment = {attachment_id:'chip',kind:'reference',source:{semantic_unit_ids:['hero']},config:{}};
        const before = JSON.stringify(attachment);
        const all = n => [n,...n.children.flatMap(all)];
        const open = async (request) => {
            document.activeElement = null;
            if (request) mod.requestAttachmentFieldFocus(request, 'retention_detail');
            const pending = mod.configurePromptAttachment(attachment, {
                scene:{duration_frames:24,_context_channel_keys:['retention_analysis']},
                semanticUnits:[{semantic_unit_id:'hero',handle:'Hero',name:'Hero',sources:[]}],
                profile,profileId:'test@1',origin:'live',candidate:{warnings:[{
                    code:'missing_h3_retention_detail',attachment_id:'chip',origin:'live',
                    field:'retention_detail'}]},scope:'global',
            });
            const focused = document.activeElement?.tagName === 'TEXTAREA';
            const nodes = all(document.body);
            const expanded = nodes.some(n => n.tagName === 'BUTTON' && n.textContent === 'Hide following');
            nodes.filter(n => n.tagName === 'BUTTON' && n.textContent === 'Cancel').at(-1)._handlers.click[0]();
            return {focused, expanded, cancelled: await pending};
        };
        // A plain open, even with the warning in the candidate, moves nothing.
        const plain = await open(null);
        const fix = await open('chip');
        // A request for another chip is dropped by the next open.
        const other = await open('other');
        const after = await open(null);
        console.log(JSON.stringify({plain,fix,other:other.focused,after:after.focused,
            unchanged:JSON.stringify(attachment) === before}));
    ''')
    assert result == {"plain": {"focused": False, "expanded": False, "cancelled": None},
                      "fix": {"focused": True, "expanded": True, "cancelled": None},
                      "other": False, "after": False, "unchanged": True}
