"""Reference Prompting Streamline, Phase 2 (`plans/reference-prompting-streamline.md`).

The server half compiles through the real routes: the Attach dialog's
`create_prompt_identity_with_sources`, its replay and refusal rules, the
preview overlay and the identity PUT. The browser half drives the shipped
modules: New Subject in Attach, the replay match, the H3 attached count, the
lane panel's read-only preview and the first-stage auto-open.
"""

import asyncio
import copy
import json

import pytest

from server import prompt_context
from test_change_certificates import _project, _section
from test_project_mutation_pipeline_backend import (
    DummyRequest, _apply_scene_operations, _load_route_module, _response_json,
    _route_handler)

H3_CHANNELS = {"summary": "[reference generation] A dusty saloon at noon.",
               "overall_soundscape": "Wind against the shutters.",
               "non_diegetic_music": "None."}


def _h3_project(tmp_path):
    """One staged picture (`cast`/`m_img`) and no identities yet."""
    project = _project(tmp_path)
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    project.prompt_semantic_units = []
    return project


def _create(unit_id="cowboy", *, sources=(("cast", "m_img"),), handle="Cowboy", **fields):
    return {"type": "create_prompt_identity_with_sources", "handle_suggestion": handle,
            "unit": {"semantic_unit_id": unit_id, "name": "Cowboy", "kind": "subject",
                     "definition": "a weathered cowboy",
                     "attachment_defaults": {"retention_detail": "his hat and coat"},
                     "sources": [{"entity_id": entity, "member_id": member, "contribution": ""}
                                 for entity, member in sources],
                     **fields}}


def _subject_chip(unit_id="cowboy"):
    """What Attach stores for a new Subject: no `enabled`, no placement."""
    return prompt_context.normalize_attachment({
        "attachment_id": "new-chip", "kind": "reference",
        "source": {"semantic_unit_ids": [unit_id]}, "config": {},
        "capabilities": [{"capability_id": kind, "kind": kind}
                         for kind in ("definitions", "retention", "mentions")]})


def _compile(route_module, project, **body):
    scene = project.get_scene("scene")
    request = {"channel_template": "minimax_h3_ref", "labels_on": False,
               "window_start": 0, "window_end": 200, "selection_start": 0,
               "selection_end": 200, "scene": scene.to_dict(), **body}
    return route_module._compile_prompt_context_candidate_sync(project, "scene", request)


# -- server: the new operation -------------------------------------------------------

def test_new_subject_compiles_to_one_sourced_definition_without_warnings(monkeypatch, tmp_path):
    """One request creates the identity and binds the chip; the compile renders
    `<Subject 1> is … from <Picture 1>`, mentions it once, and warns about nothing."""
    route_module = _load_route_module(monkeypatch)
    project = _h3_project(tmp_path)
    scene = project.get_scene("scene")
    section = _section("p1", 0, 200, "@Cowboy walks into the saloon.", [_subject_chip()])
    section.channels.update(H3_CHANNELS)
    saves = []
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        _create(),
        {"type": "replace_prompt_sections",
         "expected": {"sections": [value.to_dict() for value in scene.prompt_sections]},
         "sections": [section.to_dict()]},
    ], saves)
    assert response.status == 200, _response_json(response)
    assert len(saves) == 1
    created = _response_json(response)["results"][0]
    assert created["type"] == "create_prompt_identity_with_sources"
    assert created["created"] is True
    assert created["unit"]["handle"] == "Cowboy"
    assert created["unit"]["sources"] == [
        {"entity_id": "cast", "member_id": "m_img", "contribution": ""}]

    status, payload = _compile(route_module, project)
    assert status == 200, payload
    assert payload["channels"]["subject_definitions"] == (
        "<Subject 1> is a weathered cowboy from <Picture 1>")
    assert payload["channels"]["detailed_description"] == "<Subject 1> walks into the saloon."
    assert payload["warnings"] == [] and payload["errors"] == []


def test_a_replay_is_the_same_create_only_with_the_same_sources(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = _h3_project(tmp_path)
    scene = project.get_scene("scene")
    first = route_module._apply_scene_mutation_operation(project, scene, _create())
    # A retry may carry another handle suggestion: the server owns the handle.
    replay = route_module._apply_scene_mutation_operation(
        project, scene, _create(handle="SomethingElse"))
    assert (first["created"], replay["created"]) == (True, False)
    assert replay["unit"] == first["unit"]
    assert len(project.prompt_semantic_units) == 1

    with pytest.raises(route_module.ProjectMutationRequestError) as raised:
        route_module._apply_scene_mutation_operation(project, scene, _create(sources=()))
    assert raised.value.code == "prompt_semantic_unit_collision"
    assert project.prompt_semantic_units[0]["sources"] == first["unit"]["sources"]


@pytest.mark.parametrize(("op", "status", "code"), [
    (_create(sources=(("cast", "gone"),)), 409, "prompt_identity_source_missing"),
    # A real member, claimed by the wrong Reference.
    (_create(sources=(("other", "m_img"),)), 409, "prompt_identity_source_missing"),
    (_create(kind="place"), 400, "prompt_identity_kind_undeclared"),
    ({**_create(), "unit": {**_create()["unit"], "sources": [
        {"entity_id": "cast", "member_id": "m_img", "contribution": "not_a_contribution"}]}},
     400, "prompt_identity_contribution_undeclared"),
    (_create(sources=tuple(("cast", f"m{index}") for index in range(65))),
     400, "prompt_identity_sources_over_cap"),
])
def test_a_create_that_cannot_resolve_is_refused_whole(monkeypatch, tmp_path, op, status, code):
    route_module = _load_route_module(monkeypatch)
    project = _h3_project(tmp_path)
    saves = []
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [op], saves)
    assert (response.status, _response_json(response)["code"]) == (status, code)
    assert saves == [] and project.prompt_semantic_units == []


def test_a_server_without_the_operation_refuses_it_rather_than_dropping_sources(monkeypatch, tmp_path):
    """Sources travel on their own operation type. A server that predates it
    reaches the dispatch fallthrough and refuses the whole request; had they
    been a field on `create_prompt_semantic_unit`, it would have created the
    identity and silently dropped them."""
    route_module = _load_route_module(monkeypatch)
    project = _h3_project(tmp_path)
    saves = []
    unknown = {**_create(), "type": "create_prompt_identity_with_sources_from_the_future"}
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [unknown], saves)
    assert (response.status, _response_json(response)["code"]) == (400, "unsupported_project_mutation")
    assert saves == [] and project.prompt_semantic_units == []


def test_the_preview_names_a_source_deleted_since_the_draft_on_its_chip(monkeypatch, tmp_path):
    """A pending Writing create may cite a member deleted after it was drafted.
    The preview still compiles and says so on the chip (the compile's own
    blocking `broken_reference_source`); Apply refuses it, naming the Subject."""
    route_module = _load_route_module(monkeypatch)
    project = _h3_project(tmp_path)
    scene = project.get_scene("scene")
    section = _section("p1", 0, 200, "@Cowboy waits.", [_subject_chip()])
    section.channels.update(H3_CHANNELS)
    scene.prompt_sections = [section]
    stale = _create(sources=(("cast", "m_img"), ("cast", "deleted-member")))
    status, payload = _compile(route_module, project, prompt_semantic_unit_creates=[stale])
    assert status == 200, payload
    broken = [row for row in payload["errors"] if row["code"] == "broken_reference_source"]
    assert broken and broken[0]["attachment_id"] == "new-chip"
    assert project.prompt_semantic_units == [], "a preview never writes"

    saves = []
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [stale], saves)
    refusal = _response_json(response)
    assert refusal["code"] == "prompt_identity_source_missing"
    assert "'Cowboy' cites" in refusal["error"]


def _put_units(route_module, monkeypatch, project, units):
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved, **_kwargs: None)
    monkeypatch.setattr(route_module, "verify_project_version", lambda _project, **_kwargs: None)
    handler = _route_handler(route_module, "PUT", "/sonder-editor/project/{project_id}")
    return asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"}, method="PUT",
        body={"prompt_semantic_units": units})))


def test_the_identity_put_keeps_a_stale_stored_source_and_refuses_a_new_bad_one(monkeypatch, tmp_path):
    """A source whose member was deleted stays editable (the editor offers it for
    repair); only a source the write ADDS must resolve."""
    route_module = _load_route_module(monkeypatch)
    project = _h3_project(tmp_path)
    stale = {"entity_id": "cast", "member_id": "deleted-member", "contribution": ""}
    project.prompt_semantic_units = [prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "cowboy", "handle": "Cowboy", "name": "Cowboy",
        "kind": "subject", "sources": [stale]})]

    renamed = copy.deepcopy(project.prompt_semantic_units)
    renamed[0]["name"] = "Old Cowboy"
    response = _put_units(route_module, monkeypatch, project, renamed)
    assert response.status == 200, _response_json(response)
    assert project.prompt_semantic_units[0]["sources"] == [stale]
    assert project.prompt_semantic_units[0]["name"] == "Old Cowboy"

    added = copy.deepcopy(project.prompt_semantic_units)
    added[0]["sources"].append({"entity_id": "cast", "member_id": "never-existed"})
    response = _put_units(route_module, monkeypatch, project, added)
    assert (response.status, _response_json(response)["code"]) == (409, "prompt_identity_source_missing")
    assert project.prompt_semantic_units[0]["sources"] == [stale]


# -- browser: New Subject in Attach ------------------------------------------------

def _chip_dom(body, **values):
    """`prompt_context_chips.js` on the shared minimal DOM, stderr kept."""
    import shutil
    import subprocess
    from test_prompt_context_corrections import _MINIMAL_DOM, ROOT
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Attach dialog tests")
    for key, value in values.items():
        body = body.replace(f"__{key}__", json.dumps(value))
    url = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    script = _MINIMAL_DOM + f"\nconst mod = await import({json.dumps(url)});\n" + body
    completed = subprocess.run([node, "--input-type=module", "-e", script],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


_ATTACH = r"""
// The stub has no dispatchEvent; the dialog re-fires `change` after the editor saves.
Object.getPrototypeOf(document.body).dispatchEvent = function (event) {
  for (const h of [...(this._handlers[event.type] || [])]) h(event); return true; };
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((r) => setTimeout(r, 0)); };
const all = (root = document.body) => { const out = [];
  const walk = (n) => { for (const c of n.children) { out.push(c); walk(c); } }; walk(root); return out; };
const fire = async (node, type) => { for (const h of [...(node._handlers[type] || [])])
  await h({ target: node, stopImmediatePropagation() {}, preventDefault() {}, stopPropagation() {} }); };
const button = (text) => all().filter((n) => n.tagName === "BUTTON" && n.textContent === text).at(-1);
const references = [{ reference_id: "cast", name: "Cowboy", members: [
  { member_id: "m_img", name: "Portrait", asset_id: "img_a", prompt: "a tall stranger" }] }];
const scene = { duration_frames: 200, _context_channel_keys: ["detailed_description"],
  reference_lane_count: 1, reference_lane_configs: [{}],
  reference_lane_recipes: [{ lane_id: "L0", media_kind: "image", recipe: { soft: {
    compatible_profiles: ["minimax_h3_ref@1"], physical_population: "pictures" } } }],
  reference_items: [{ reference_item_id: "r0", lane_index: 0, start_frame: 0, end_frame: -1,
    members: [{ entity_id: "cast", member_id: "m_img" }] }] };
const open = (profile, profileId) => mod.configurePromptAttachment({ attachment_id: "new-chip",
    kind: "reference", source: { picture_ids: ["m_img"] }, config: {}, capabilities: [] }, {
  scene, profileId, profile, references, semanticUnits: [],
  assets: [{ asset_id: "img_a", asset_type: "image" }],
  candidate: { setup_manifest: { pictures: [{ member_id: "m_img", picture_ordinal: 1 }] } },
  placementPhases: [{ value: "section_prefix", label: "Section prefix" }],
});
// The stub keeps inserted options out of `options`; read the children.
const targetSelect = () => all().find((n) => n.tagName === "SELECT"
  && n.children.some((o) => String(o.value).startsWith("physical:picture:")));
"""


def test_new_subject_in_attach_creates_and_binds_one_subject_that_compiles(monkeypatch, tmp_path):
    """The dialog's own output, sent as the host sends it, compiles to the guide's
    Subject route with nothing for the author to fix."""
    dialog = _chip_dom(_ATTACH + r"""
    const pending = open(__H3__, "minimax_h3_ref@1");
    const select = targetSelect();
    const offered = select.children.map((o) => o.textContent);
    select.value = "__new_identity__";
    await fire(select, "change");
    await settle();
    const editor = all().find((n) => n.dataset?.promptIdentityEditor === "1");
    // The sentinel is never a selection: the select keeps the current target.
    const kept = select.value;
    await fire(button("Use for this attachment"), "click");
    await settle();
    const after = { value: select.value, closed: !all().find((n) => n.dataset?.promptIdentityEditor === "1"),
      labels: select.children.map((o) => o.textContent) };
    await fire(button("Attach"), "click");
    const saved = await pending;
    console.log(JSON.stringify({ offered, editor: !!editor, kept, after,
      attachment: saved.attachment, intent: saved.identityCreateIntent }));
    """, H3=prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    assert "New Subject…" in dialog["offered"]
    assert dialog["editor"] and dialog["kept"] == "physical:picture:m_img"
    intent, attachment = dialog["intent"], dialog["attachment"]
    unit_id = intent["unit"]["semantic_unit_id"]
    assert dialog["after"]["closed"] and dialog["after"]["value"] == unit_id
    assert "Subject: Cowboy (new)" in dialog["after"]["labels"]
    assert intent["type"] == "create_prompt_identity_with_sources"
    assert intent["unit"]["sources"] == [
        {"entity_id": "cast", "member_id": "m_img", "contribution": ""}]
    # Never a stored `<Picture N>`: the definition follows the member's prose.
    assert intent["unit"]["definition"] == ""
    assert attachment["source"] == {"semantic_unit_ids": [unit_id]}
    assert not any("enabled" in value for value in attachment["capabilities"])

    route_module = _load_route_module(monkeypatch)
    project = _h3_project(tmp_path)
    scene = project.get_scene("scene")
    section = _section("p1", 0, 200, "@Cowboy walks into the saloon.",
                       [prompt_context.normalize_attachment(attachment)])
    section.channels.update(H3_CHANNELS)
    saves = []
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": intent["type"], "handle_suggestion": intent["handle_suggestion"],
         "unit": intent["unit"]},
        {"type": "replace_prompt_sections",
         "expected": {"sections": [value.to_dict() for value in scene.prompt_sections]},
         "sections": [section.to_dict()]},
    ], saves)
    assert response.status == 200, _response_json(response)
    # The client's replay match accepts what the server actually stored.
    stored = _response_json(response)["results"][0]["unit"]
    match = _module("web/js/prompt_identity_transactions.js",
                    "console.log(JSON.stringify(mod.promptIdentityMatchesCreateIntent("
                    + json.dumps(stored) + ", " + json.dumps(intent) + ")));")
    assert match is True
    status, payload = _compile(route_module, project)
    assert status == 200, payload
    assert payload["channels"]["subject_definitions"] == (
        "<Subject 1> is a tall stranger from <Picture 1>")
    assert payload["channels"]["detailed_description"] == "<Subject 1> walks into the saloon."
    # Retention detail is the author's (Preservation detail); its advice is the
    # only thing a fresh Subject still asks for.
    assert {row["code"] for row in payload["warnings"]} <= {"missing_h3_retention_detail"}
    assert payload["errors"] == []


def test_arrow_keys_pass_over_new_subject_and_enter_opens_it():
    """Walking the closed list with the arrows must not pop a modal mid-walk."""
    result = _chip_dom(_ATTACH + r"""
    const pending = open(__H3__, "minimax_h3_ref@1");
    const select = targetSelect();
    const key = async (name) => { for (const h of [...(select._handlers.keydown || [])])
      await h({ key: name, target: select, preventDefault() {}, stopPropagation() {} }); };
    const editorOpen = () => !!all().find((n) => n.dataset?.promptIdentityEditor === "1");
    await key("ArrowUp");
    select.value = "__new_identity__";
    await fire(select, "change");
    await settle();
    const afterWalk = editorOpen();
    await key("Enter");
    await settle();
    const afterEnter = editorOpen();
    console.log(JSON.stringify({ afterWalk, afterEnter, kept: select.value }));
    """, H3=prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    assert result == {"afterWalk": False, "afterEnter": True, "kept": "physical:picture:m_img"}


def test_new_identity_is_offered_only_where_the_format_declares_identities():
    result = _chip_dom(_ATTACH + r"""
    const generic = open(__GENERIC__, "generic@1");
    const offered = all().filter((n) => n.tagName === "SELECT")
      .flatMap((n) => n.children.map((o) => String(o.value)));
    console.log(JSON.stringify({ sentinel: offered.includes("__new_identity__") }));
    """, GENERIC=prompt_context.BUILTIN_PROFILES["generic@1"])
    assert result == {"sentinel": False}


def _module(path, body):
    import shutil
    import subprocess
    from test_prompt_context_corrections import ROOT
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser module tests")
    script = (f"const mod = await import({json.dumps((ROOT / path).as_uri())});\n" + body)
    completed = subprocess.run([node, "--input-type=module", "-e", script],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_client_replay_match_and_operation_carry_sources():
    """An uncertain Apply, a conflict retry and Redo all decide "already created"
    with this match, so it must agree with the server's field-by-field rule."""
    result = _module("web/js/prompt_identity_transactions.js", r"""
    const intent = { type: "create_prompt_identity_with_sources", handle_suggestion: "Cowboy",
      unit: { semantic_unit_id: "cowboy", name: "Cowboy", kind: "subject", definition: "",
        attachment_defaults: { retention_detail: "hat" }, disabled_capabilities: [],
        visual_intent: "preserve", audio_intent: "reference_characteristics",
        sources: [{ entity_id: "cast", member_id: "m_img", contribution: "" }] } };
    // As the server stores it: its own handle and order, keys in its order.
    const stored = { order: 3, handle: "Cowboy2", ...structuredClone(intent.unit),
      sources: [{ contribution: "", member_id: "m_img", entity_id: "cast" }] };
    const other = { ...structuredClone(stored), sources: [] };
    const legacy = { type: "create_prompt_semantic_unit", handle_suggestion: "Narrator",
      unit: { semantic_unit_id: "n", name: "Narrator", kind: "subject", definition: "" } };
    console.log(JSON.stringify({
      same: mod.promptIdentityMatchesCreateIntent(stored, intent),
      otherSources: mod.promptIdentityMatchesCreateIntent(other, intent),
      otherDefaults: mod.promptIdentityMatchesCreateIntent(
        { ...stored, attachment_defaults: {} }, intent),
      legacyStillEmpty: mod.promptIdentityMatchesCreateIntent(
        { ...legacy.unit, handle: "Narrator", order: 0, sources: [] }, legacy),
      legacyRefusesSources: mod.promptIdentityMatchesCreateIntent(
        { ...legacy.unit, handle: "Narrator", order: 0, sources: stored.sources }, legacy),
      operation: mod.promptIdentityCreateOperation(intent),
      legacyOperation: mod.promptIdentityCreateOperation(legacy).type,
      unknownFallsBack: mod.promptIdentityCreateOperation({ ...legacy, type: "x" }).type,
    }));
    """)
    assert result["same"] is True
    assert result["otherSources"] is False and result["otherDefaults"] is False
    assert result["legacyStillEmpty"] is True and result["legacyRefusesSources"] is False
    assert result["operation"]["type"] == "create_prompt_identity_with_sources"
    assert result["operation"]["unit"]["sources"][0]["member_id"] == "m_img"
    assert result["legacyOperation"] == result["unknownFallsBack"] == "create_prompt_semantic_unit"


def test_h3_counts_a_chip_attached_to_an_item_through_its_members():
    """Generic binds a chip to the item; H3 binds it to members, directly or
    through an identity's sources. A disabled chip never counts."""
    result = _module("web/js/reference_resolution.js", r"""
    const chip = (id, source, extra = {}) => ({ attachment_id: id, kind: "reference", source, ...extra });
    const scene = {
      reference_items: [
        { reference_item_id: "item-1", members: [{ member_id: "m1" }, { member_id: "m2" }] },
        { reference_item_id: "item-2", members: [{ member_id: "m3" }] }],
      global_attachments: [chip("g", { semantic_unit_ids: ["cowboy"] })],
      prompt_sections: [{ attachments: [
        chip("direct", { picture_ids: ["m2"] }),
        chip("generic", { reference_item_id: "item-1" }),
        chip("off", { picture_ids: ["m1"] }, { enabled: false }),
        chip("elsewhere", { picture_ids: ["m3"] })] }] };
    const semanticUnits = [{ semantic_unit_id: "cowboy", sources: [{ member_id: "m1" }] }];
    const count = (referenceItemId, declaredSources) => mod.countAttachedReferenceChips({
      scene, referenceItemId, declaredSources, semanticUnits });
    console.log(JSON.stringify({ h3: count("item-1", true), h3Other: count("item-2", true),
      generic: count("item-1", false), genericOther: count("item-2", false) }));
    """)
    assert result == {"h3": 2, "h3Other": 1, "generic": 1, "genericOther": 0}


# -- browser: the lane panel's read-only preview ---------------------------------------

_PREVIEW = r"""
Object.getPrototypeOf(document.body).replaceWith = function (next) {
  const list = this.parentElement.children; list.splice(list.indexOf(this), 1, next);
  next.parentElement = this.parentElement; this.parentElement = null; };
// And a bare `[data-x]` selector, which the preview refresh uses.
const baseQuery = Object.getPrototypeOf(document.body).querySelectorAll;
Object.getPrototypeOf(document.body).querySelectorAll = function (sel) {
  const bare = /^\[data-([\w-]+)\]$/.exec(String(sel).trim());
  if (!bare) return baseQuery.call(this, sel);
  const key = bare[1].replace(/-(\w)/g, (_m, c) => c.toUpperCase());
  return walk(this).filter((n) => n !== this && n.dataset?.[key] !== undefined); };
const text = (n) => walk(n).map((c) => c.textContent).filter(Boolean).join(" | ");
const previews = () => Object.fromEntries(nodes().filter((n) => n.dataset.sonderReferencePromptPreview)
  .map((n) => [n.dataset.sonderReferencePromptPreview, text(n)]));
w._promptContextCatalog = __CATALOG__;
w._selectionContextRange = () => ({ contextStart: 0, contextEnd: 45 });
const candidate = (pictures) => ({ _candidate_scene_id: 'scene', setup_manifest: { pictures } });
"""


def _catalog():
    from server import routes
    from server.timeline_state import TimelineProject
    return routes._references_payload(TimelineProject(project_id="p"))["prompt_context_catalog"]


def test_h3_lane_preview_shows_picture_numbers_roles_and_citations(tmp_path):
    from test_reference_panel_item_writes_js import run_item_panel
    result = run_item_panel((_PREVIEW + r"""
    w.activeScene.prompt_context_profile_id = 'minimax_h3_ref@1';
    w._promptSemanticUnits = [{ semantic_unit_id: 'u', handle: 'Cowboy',
      sources: [{ entity_id: 'entity-1', member_id: 'member-a' }] }];
    w._promptContextCandidateCache = candidate([
      { member_id: 'member-a', picture_ordinal: 1, role: 'identity', member_prompt: 'a tall stranger' },
      { member_id: 'member-b', picture_ordinal: 2, role: '', member_prompt: '' }]);
    let opened = 0; w._showPromptManagementPanel = () => { opened += 1; };
    mount();
    const before = previews();
    const recipeInput = nodes().find((n) => n.tagName === 'INPUT');
    // A compile lands that now stages item-2 too: only the previews repaint.
    w._promptContextCandidateCache = candidate([
      { member_id: 'member-a', picture_ordinal: 1, role: 'identity', member_prompt: 'a tall stranger' },
      { member_id: 'member-c', picture_ordinal: 2, role: 'identity', member_prompt: 'a red bottle' }]);
    handle.applyCandidate();
    const after = previews();
    nodes().find((n) => n.dataset.sonderReferencePromptPreview === 'item-1').dispatch('click');
    return { before, after, opened, inputKept: nodes().includes(recipeInput), sent: sent.length,
      locked: nodes().filter((n) => n.tagName === 'BUTTON'
        && ['Edit as override', 'Clear'].includes(n.textContent)).length };
    """).replace("__CATALOG__", json.dumps(_catalog())), tmp_path)
    assert result["before"]["item-1"] == (
        "<Picture 1> · Identity · a tall stranger | Cited by @Cowboy"
        " | <Picture 2> · No Library text")
    # Outside the selection, so no picture number: the reason, never a guess.
    assert result["before"]["item-2"].startswith("Not in the current window — no picture number yet")
    assert result["after"]["item-2"] == "<Picture 2> · Identity · a red bottle"
    assert result["inputKept"] and result["opened"] == 1
    assert result["sent"] == 0, "a preview never writes"
    assert result["locked"] == 0, "the override moved to the Prompt tool"


def test_generic_lane_preview_marks_an_override_from_the_prompt_tool(tmp_path):
    from test_reference_panel_item_writes_js import run_item_panel
    result = run_item_panel((_PREVIEW + r"""
    w.activeScene.prompt_context_profile_id = 'generic@1';
    item('item-2').prompt_override = 'a bottle on the bar';
    mount();
    return previews();
    """).replace("__CATALOG__", json.dumps(_catalog())), tmp_path)
    assert result["item-2"] == "a bottle on the bar | Overridden in the Prompt tool"
    assert "Overridden" not in result["item-1"]


# -- browser: Reference Lane Setup opens after a first stage --------------------------

_AUTO_OPEN = r"""
    const tick = () => new Promise((r) => setTimeout(r, 0));
    const staged = (scene, settings = {}) => {
        const w = referenceWidget(scene);
        w._settings = { references: { openLaneSetupOnFirstStage: true, ...settings } };
        w.opened = [];
        w._showReferenceLanePanel = async (entry) => { w.opened.push(entry.laneIndex); };
        return w;
    };
    const land = async (w, pending, index = 0) => {
        await tick();
        w.released[index].resolve({payload: {results: []}});
        await pending; await tick(); await tick();
    };
"""


def test_the_first_item_on_an_empty_lane_opens_its_setup_after_the_write():
    from test_project_mutation_queue import _CLASS_C_SETUP, _run_gesture_node
    _run_gesture_node(_CLASS_C_SETUP + _AUTO_OPEN + """
        const w = staged(referenceScene());
        const pending = w._placeReferencePayload({members: [drag('member-a')]}, 40);
        await tick();
        assert.deepEqual(w.opened, [], 'never while the write is in flight');
        await land(w, pending);
        assert.deepEqual(w.opened, [0]);

        // A second item on the same lane is not a first stage.
        const again = w._placeReferencePayload({members: [drag('member-b')]}, 600);
        await land(w, again, 1);
        assert.deepEqual(w.opened, [0]);
    """)


def test_lane_setup_stays_closed_when_the_author_moved_on_or_opted_out():
    from test_project_mutation_queue import _CLASS_C_SETUP, _run_gesture_node
    _run_gesture_node(_CLASS_C_SETUP + _AUTO_OPEN + """
        const off = staged(referenceScene(), { openLaneSetupOnFirstStage: false });
        await land(off, off._placeReferencePayload({members: [drag('member-a')]}, 40));
        assert.deepEqual(off.opened, [], 'the setting is off');

        const busy = staged(referenceScene());
        const pending = busy._placeReferencePayload({members: [drag('member-a')]}, 40);
        busy._referencePanelHandle = { close() {} };
        await land(busy, pending);
        assert.deepEqual(busy.opened, [], 'a panel is already open');

        const locked = staged(referenceScene());
        locked._isLaneLocked = () => true;
        await land(locked, locked._placeReferencePayload({members: [drag('member-a')]}, 40));
        assert.deepEqual(locked.opened, [], 'never for a locked lane');

        const refused = staged(referenceScene());
        const write = refused._placeReferencePayload({members: [drag('member-a')]}, 40);
        await tick();
        refused.released[0].reject(Object.assign(new Error('refused'), { status: 409 }));
        await write; await tick(); await tick();
        assert.deepEqual(refused.opened, [], 'a refused stage opens nothing');

        const moved = staged(referenceScene());
        const staging = moved._placeReferencePayload({members: [drag('member-a')]}, 40);
        await tick();
        moved.released[0].resolve({payload: {results: []}});
        await staging;
        moved._withMutationGesture('moveClip', () => null);  // the author moved on
        await tick(); await tick();
        assert.deepEqual(moved.opened, [], 'a gesture since the stage wins');

        const guides = staged(referenceScene());
        const guideWrite = guides._placeReferencePayload({members: [drag('member-a')]}, 40);
        guides._guideManagerEl = { isConnected: true };
        await land(guides, guideWrite);
        assert.deepEqual(guides.opened, [], 'the Guide manager is open');

        const superseded = staged(referenceScene());
        const first = superseded._placeReferencePayload({members: [drag('member-a')]}, 40);
        await tick();
        superseded._laneSetupAutoOpenSeq += 1;  // a newer stage began meanwhile
        await land(superseded, first);
        assert.deepEqual(superseded.opened, [], 'a newer stage wins');
    """)


# -- browser: Writing holds the Subject until Apply -------------------------------------

def test_writing_keeps_a_pending_subject_with_its_sources_through_reload():
    """Attach in Writing stages the create; the draft that reloads still names it
    by type and sources, and the chip keeps it reachable until Apply clears it."""
    from test_prompt_authoring_ux import _run_panel_script
    result = _run_panel_script(r"""
        const create = {type:"create_prompt_identity_with_sources", handle_suggestion:"Cowboy",
            unit:{semantic_unit_id:"pending-subject", name:"Cowboy", kind:"subject",
                sources:[{entity_id:"cast", member_id:"m_img", contribution:""}]}};
        const chip = {attachment_id:"c", emission_group_id:"c", kind:"reference",
            source:{semantic_unit_ids:["pending-subject"]}, config:{}, capabilities:[]};
        const staged = mod.stageWritingSemanticUnitCreate([], create).creates;
        const saved = JSON.parse(JSON.stringify({draft:"@Cowboy waits.",
            pendingSemanticUnitCreates:staged}));
        const load = mod.writingDraftLoadState(saved);
        console.log(JSON.stringify({
            useSaved: load.useSavedDraft,
            reloaded: saved.pendingSemanticUnitCreates,
            reachable: mod.reachableWritingSemanticUnitCreates(saved.pendingSemanticUnitCreates, [chip], [])
                .map((value) => value.unit.semantic_unit_id),
            orphaned: mod.reachableWritingSemanticUnitCreates(saved.pendingSemanticUnitCreates, [], []).length,
            afterApply: mod.writingAppliedRestoreSnapshot(saved).pendingSemanticUnitCreates,
        }));
    """)
    assert result["useSaved"] is True
    assert result["reloaded"][0]["type"] == "create_prompt_identity_with_sources"
    assert result["reloaded"][0]["unit"]["sources"][0]["member_id"] == "m_img"
    assert result["reachable"] == ["pending-subject"] and result["orphaned"] == 0
    assert result["afterApply"] == []


def test_history_finalizes_the_new_create_for_undo_cleanup():
    """Undo deletes a created identity only when Apply's result says `created`;
    the finalizer must read the new operation's results as it reads the old."""
    from test_prompt_authoring_ux import _method, _run_node, _source
    from test_prompt_context_corrections import ROOT
    widget = _source("web/js/editor_widget.js")
    method = _method(widget, "_finalizePromptIdentityCreationHistory", "_mergeQueueMutationIntents")
    transactions = (ROOT / "web/js/prompt_identity_transactions.js").as_uri()
    result = _run_node(f"""
const {{PROMPT_IDENTITY_CREATE_TYPES}}=await import({json.dumps(transactions)});
class Harness {{
{method}
}}
const unit={{semantic_unit_id:"one",name:"One",kind:"subject",definition:"",
  sources:[{{entity_id:"cast",member_id:"m_img",contribution:""}}]}};
const intent={{type:"create_prompt_identity_with_sources",unit}};
const entry={{promptIdentityCreateIntents:[intent]}};
const created=new Harness()._finalizePromptIdentityCreationHistory(entry,{{payload:{{results:[{{
  type:"create_prompt_identity_with_sources",created:true,
  unit:{{...unit,handle:"One",order:0,attachment_defaults:{{}},disabled_capabilities:[]}}}}]}}}});
console.log(JSON.stringify({{created:[...created],
  cleanupSources:intent.cleanup_expected?.sources||null}}));
""")
    assert result["created"] == ["one"]
    assert result["cleanupSources"] == [{"entity_id": "cast", "member_id": "m_img", "contribution": ""}]
