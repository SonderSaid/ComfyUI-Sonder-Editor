"""Phase F — bounded custom Prompt Format authoring.

Gate: a custom format declaring its own capability and vocabulary compiles and
drives the authoring UI with no MiniMax fallback, and a fork preserves role
labels rather than collapsing them to their machine values.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from server import prompt_context, routes
from server.timeline_state import TimelineProject


ROOT = Path(__file__).resolve().parents[1]
MODULE = (ROOT / "web/js/prompt_format_editor.js").as_uri()

# A minimal DOM for the declaration editor: `details/summary` disclosure,
# `<select>` options, dataset markers, and input events. Deliberately local —
# `conftest.py` has no shared node runner and 38 test files declare their own.
_DOM = r"""
class N {
  constructor(tag) {
    this.tagName=String(tag).toUpperCase(); this.children=[]; this.options=[];
    this.style={cssText:""}; this.dataset={}; this.attributes={}; this._handlers={};
    this.value=""; this.textContent=""; this.disabled=false; this.checked=false;
    this.open=false; this.rows=0; this.placeholder=""; this.type="";
  }
  appendChild(c) {
    if (typeof c === "string") { const t=new N("#text"); t.textContent=c; c=t; }
    this.children.push(c); c.parentElement=this;
    if (c.tagName === "OPTION") this.options.push(c);
    return c;
  }
  append(...cs) { cs.forEach((c) => c != null && this.appendChild(c)); }
  replaceChildren(...cs) { this.children=[]; this.options=[]; this.append(...cs); }
  addEventListener(t,h) { (this._handlers[t] ||= []).push(h); }
  dispatch(t) { let n=this; while (n) { for (const h of n._handlers[t] || []) h({target:this}); n=n.parentElement; } }
  setAttribute(k,v) { this.attributes[k]=String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  querySelector() { return null; }
  remove() {
    if (this.parentElement) this.parentElement.children =
      this.parentElement.children.filter((c) => c !== this);
  }
}
globalThis.document={createElement:(t)=>new N(t), body:new N("body")};
globalThis.localStorage={getItem(){return null;},setItem(){}};
const walk=(n,out=[])=>{out.push(n);n.children.forEach((c)=>walk(c,out));return out;};
const text=(n)=>[n.textContent,...n.children.map(text)].join(" ");
"""


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Format authoring coverage")
    # The served catalog is far past the Windows command-line limit, so the
    # script goes to a file rather than `-e`.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "case.mjs"
        path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [node, str(path)], capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


_GENERIC_TEMPLATE = {
    "id": "standard", "name": "Standard",
    "channels": [{"key": "visual", "label": "Visual"},
                 {"key": "speech", "label": "Speech"},
                 {"key": "sounds", "label": "Sound"}],
}


def _catalog() -> dict:
    return routes._references_payload(TimelineProject(
        project_id="project"))["prompt_context_catalog"]


def _mount_script(definition, *, template=None, catalog=None, body="") -> str:
    return (
        _DOM
        + f"const mod = await import({json.dumps(MODULE)});\n"
        + "const editor = mod.mountPromptFormatDeclarationEditor({"
        + f"definition: {json.dumps(definition)},"
        + f"template: {json.dumps(template or _GENERIC_TEMPLATE)},"
        + f"catalog: {json.dumps(catalog if catalog is not None else _catalog())}"
        + "});\nconst nodes = walk(editor.element);\n" + body
    )


def test_fork_preserves_role_labels_and_untouched_declaration_groups():
    """The defect this phase fixes: `{value,label}` collapsing to `value`.

    Forking MiniMax used to read only the value half and rebuild bare strings,
    so `_normalized_role_catalog`'s `label = value` fallback turned
    "First frame" into "first_frame".
    """
    seed = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    assert any(entry["label"] != entry["value"]
               for values in seed["role_catalogs"].values()
               for entry in values), "fixture must contain a divergent label"
    result = _run_node(_mount_script(
        seed, template={"id": "minimax_h3_ref", "channels": [
            {"key": key} for key in (
                "detailed_description", "subject_definitions", "summary",
                "retention_analysis")]},
        body="console.log(JSON.stringify(editor.collect()));\n"))

    assert result["role_catalogs"] == seed["role_catalogs"]
    # Everything the editor did not touch survives byte-for-byte.
    for key in ("physical_populations", "identity_kinds", "speaker_policy",
                "contribution_catalog", "separators", "validators"):
        if key in seed:
            assert result[key] == seed[key], key
    assert (result["capabilities"]["reference"]["derived"]
            == seed["capabilities"]["reference"]["derived"])


def test_role_label_round_trip_survives_the_authored_text_form():
    seed = {"physical_populations": [{"key": "picture"}],
            "role_catalogs": {"picture": [
                {"value": "first_frame", "label": "First frame"},
                {"value": "identity", "label": "identity"}]}}
    result = _run_node(_mount_script(seed, body=r"""
const roles = nodes.find((n) => n.attributes["aria-label"] === "picture roles");
const authored = roles.value;
roles.value = authored + "\nlast_frame = Last frame";
roles.dispatch("input");
console.log(JSON.stringify({authored, collected: editor.collect().role_catalogs}));
"""))
    # A label equal to its value stays unadorned; a divergent one is explicit.
    assert result["authored"] == "first_frame = First frame\nidentity"
    assert result["collected"]["picture"] == [
        {"value": "first_frame", "label": "First frame"},
        {"value": "identity", "label": "identity"},
        {"value": "last_frame", "label": "Last frame"},
    ]


def test_declaration_editor_offers_only_served_vocabulary():
    """No provider vocabulary in the browser: every choice comes from the
    served catalog or the bound channel template."""
    catalog = _catalog()
    result = _run_node(_mount_script(
        prompt_context.profile_fork_seed(
            prompt_context.BUILTIN_PROFILES["generic@1"]),
        catalog=catalog, body=r"""
const options = (label) => (nodes.find(
  (n) => n.attributes["aria-label"] === label)?.options || []).map((o) => o.value);
console.log(JSON.stringify({
  channels: options("derived_prompt destination channel"),
  placements: options("derived_prompt placement"),
  kinds: options("New prompt part"),
  groups: nodes.filter((n) => n.dataset.promptFormatGroup)
    .map((n) => n.dataset.promptFormatGroup),
  rendered: text(editor.element),
}));
"""))
    assert result["channels"] == ["visual", "speech", "sounds"]
    assert result["placements"] == [row["value"] for row in catalog["placement_phases"]]
    assert result["kinds"] == [row["value"] for row in catalog["reference_capability_kinds"]]
    assert result["groups"] == ["derived", "populations", "identity_kinds",
                                "vocabularies", "speaker_policy",
                                "vocal_event_policy"]
    # A generic format must show nothing MiniMax-specific.
    lowered = result["rendered"].lower()
    for literal in ("minimax", "reference generation", "video editing",
                    "subject_definitions", "retention_analysis"):
        assert literal not in lowered, literal


def test_editing_a_population_never_reverts_typed_vocabulary():
    """Regression: re-rendering the vocabulary rows must read the authored
    store, not the seed, or fixing a typo elsewhere silently reverts work."""
    result = _run_node(_mount_script(
        {"physical_populations": [{"key": "pic", "label": "Pic"}],
         "role_catalogs": {"pic": [{"value": "identity", "label": "Identity"}]},
         "contribution_catalog": {
             "*": [{"value": "appearance", "label": "Appearance"}]}},
        body=r"""
const find = (label) => nodes.find((n) => n.attributes["aria-label"] === label);
find("pic roles").value = "identity = Identity\nstyle = Style";
find("pic roles").dispatch("input");
find("* contributions").value = "appearance = Appearance\nmood = Mood";
find("* contributions").dispatch("input");
const typed = editor.collect();
// Commit an unrelated edit on the population row.
const label = walk(editor.element).find((n) => n.tagName === "INPUT" && n.value === "Pic");
label.value = "Pictures";
label.dispatch("input");
label.dispatch("change");
const after = editor.collect();
console.log(JSON.stringify({
  starBoxes: walk(editor.element)
    .filter((n) => n.attributes["aria-label"] === "* contributions").length,
  typedRoles: typed.role_catalogs, typedContributions: typed.contribution_catalog,
  afterRoles: after.role_catalogs, afterContributions: after.contribution_catalog,
}));
"""))
    # Exactly one "*" row, so an edit cannot land in a discarded duplicate.
    assert result["starBoxes"] == 1
    expected_roles = {"pic": [{"value": "identity", "label": "Identity"},
                              {"value": "style", "label": "Style"}]}
    expected_contributions = {"*": [{"value": "appearance", "label": "Appearance"},
                                    {"value": "mood", "label": "Mood"}]}
    assert result["typedRoles"] == expected_roles
    assert result["typedContributions"] == expected_contributions
    assert result["afterRoles"] == expected_roles
    assert result["afterContributions"] == expected_contributions


def test_a_renamed_population_keeps_its_vocabulary_visible_for_repair():
    """Preserve-don't-coerce: the orphaned key stays authored AND stays on
    screen, labelled, rather than surviving under a key nothing can reach."""
    result = _run_node(_mount_script(
        {"physical_populations": [{"key": "pic", "label": "Pic"}],
         "role_catalogs": {"pic": [{"value": "identity", "label": "Identity"}]}},
        body=r"""
const key = walk(editor.element).find((n) => n.tagName === "INPUT" && n.value === "pic");
// Keystrokes must not each mint a row; only the committed key does.
key.value = "p"; key.dispatch("input");
key.value = "pi"; key.dispatch("input");
key.value = "pics"; key.dispatch("input"); key.dispatch("change");
const collected = editor.collect();
console.log(JSON.stringify({
  rows: walk(editor.element)
    .filter((n) => String(n.attributes["aria-label"] || "").endsWith(" roles"))
    .map((n) => n.attributes["aria-label"]),
  orphanNoted: walk(editor.element).some((n) =>
    String(n.textContent || "").includes("no such population")),
  populations: collected.physical_populations.map((p) => p.key),
  roleKeys: Object.keys(collected.role_catalogs),
}));
"""))
    # The declared population leads; the orphaned key trails it, still visible.
    assert result["rows"] == ["pics roles", "pic roles"]
    assert result["orphanNoted"] is True
    assert result["populations"] == ["pics"]
    # The authored roles are preserved under the old key rather than silently
    # dropped; the new key has none yet, and empty lists are not persisted.
    assert result["roleKeys"] == ["pic"]


def test_editing_a_vocabulary_preserves_metadata_the_text_form_cannot_express():
    """A per-value `description` is legal declaration data with no column in the
    `value = Label` form, so it must survive every edit rather than the first."""
    result = _run_node(_mount_script(
        {"capabilities": {"reference": {"derived": {"summary": {
            "order": 1, "channel_key": "visual", "placement": "section_prefix",
            "label": "Summary", "fields": {"task_types": {
                "type": "enum_multi", "values": [
                    {"value": "sb", "label": "Storyboard",
                     "description": "House storyboard pass."}]}}}}}}},
        body=r"""
const values = nodes.find((n) =>
  n.attributes["aria-label"] === "task_types bounded values");
const untouched = editor.collect();
values.dispatch("input");
const reentered = editor.collect();
values.value = values.value + "\ncolour = Colour, final";
values.dispatch("input");
values.dispatch("input");
const read = (result) => result.capabilities.reference.derived.summary
  .fields.task_types.values;
console.log(JSON.stringify({
  untouched: read(untouched), reentered: read(reentered),
  appended: read(editor.collect()),
}));
"""))
    described = {"value": "sb", "label": "Storyboard",
                 "description": "House storyboard pass."}
    assert result["untouched"] == [described]
    assert result["reentered"] == [described]
    # A comma belongs to the label; splitting on it would mint a bogus value.
    assert result["appended"] == [described,
                                  {"value": "colour", "label": "Colour, final"}]


def test_editing_a_legacy_routes_format_drops_the_converted_shape():
    """`routes` and `derived` must never be saved side by side.

    With the dedicated migration action removed, **Edit as new version…** is the
    repair path for a format still holding the pre-`derived` shape: the editor
    drops `routes`, and the resulting declaration is refused until the author
    declares its parts — a visible dead end rather than a silent half-conversion.
    """
    seed = {"template_id": "standard", "capabilities": {"reference": {
        "placement": "section_prefix",
        "routes": {"summary": "visual"}}}}
    result = _run_node(_mount_script(
        seed, body="console.log(JSON.stringify(editor.collect()));\n"))
    reference = result["capabilities"]["reference"]
    assert "routes" not in reference
    assert reference["derived"] == {}
    errors = prompt_context.profile_declaration_errors(
        {"profile_id": "x", "version": "1", **result},
        template={"id": "standard", "channels": [{"key": "visual"}]})
    assert [row["code"] for row in errors] == ["incomplete_capability_declaration"]


def test_declaring_and_removing_a_part_are_both_reversible():
    result = _run_node(_mount_script(
        {"capabilities": {"reference": {"derived": {}}}}, body=r"""
const kind = nodes.find((n) => n.attributes["aria-label"] === "New prompt part");
kind.value = "summary";
nodes.find((n) => n.attributes["aria-label"] === "Declare a Reference prompt part")
  .dispatch("click");
const declared = editor.collect();
walk(editor.element).find((n) =>
  n.attributes["aria-label"] === "Stop declaring the summary prompt part")
  .dispatch("click");
console.log(JSON.stringify({
  declared: Object.keys(declared.capabilities.reference.derived),
  removed: Object.keys(editor.collect().capabilities.reference.derived),
}));
"""))
    assert result["declared"] == ["summary"]
    assert result["removed"] == []


def test_contribution_opt_out_stays_distinct_from_inheritance():
    """For CONTRIBUTIONS the two are different declarations and the server
    keeps them apart. Speaker policy has no such distinction — see below."""
    inheriting = _run_node(_mount_script(
        {"physical_populations": [{"key": "picture"}]},
        body="console.log(JSON.stringify(editor.collect()));\n"))
    assert "contribution_catalog" not in inheriting
    assert "speaker_policy" not in inheriting

    opted_out = _run_node(_mount_script(
        {"physical_populations": [{"key": "picture"}]}, body=r"""
const toggle = nodes.find((n) => n.attributes["aria-label"]
  === "Declare this format's own contribution vocabulary");
toggle.checked = true; toggle.dispatch("change");
console.log(JSON.stringify(editor.collect()));
"""))
    assert opted_out["contribution_catalog"] == {}
    # Server side: absent inherits the shared default, `{}` opts out.
    assert prompt_context.effective_contribution_catalog(inheriting)
    assert prompt_context.effective_contribution_catalog(opted_out) == {}


def test_speaker_policy_has_no_opt_out_and_the_editor_does_not_imply_one():
    """`normalize_profile` always materializes `speaker_policy` and
    `profile_declaration_errors` refuses a partial one, so omitting the key
    stores the default rather than opting out. The editor must not suggest
    otherwise, or an author disables speakers by leaving a box unchecked and
    silently gets the enabled default instead."""
    declared = _run_node(_mount_script({}, body=r"""
const toggle = nodes.find((n) => n.attributes["aria-label"]
  === "Declare this format's own speaker policy");
toggle.checked = true;
console.log(JSON.stringify(editor.collect()));
"""))
    assert declared["speaker_policy"] == {
        "enabled": False, "token_template": "", "compound_join": ",",
        "compound_order": "authored"}
    # An empty policy is refused, so `{}` cannot mean "opted out".
    assert "invalid_speaker_policy" in {
        row["code"] for row in prompt_context.profile_declaration_errors(
            {"speaker_policy": {}})}
    # Omitting it entirely resolves to the server default, not to "no speakers".
    assert prompt_context.effective_speaker_policy({}) == {
        "enabled": False, "token_template": "", "compound_join": ",",
        "compound_order": "authored"}


def test_vocal_event_policy_editor_collects_one_closed_complete_group():
    declared = _run_node(_mount_script({}, body=r"""
const toggle = nodes.find((n) => n.attributes["aria-label"]
  === "Declare a Vocal Event policy");
toggle.checked = true; toggle.dispatch("change");
const prefix = nodes.find((n) => n.attributes["aria-label"]
  === "Vocal Event identity prefix");
prefix.value = "selected";
const delivery = nodes.find((n) => n.attributes["aria-label"]
  === "Vocal Events support delivery prose");
delivery.checked = true;
console.log(JSON.stringify(editor.collect()));
"""))
    assert declared["capabilities"]["vocal_event"]["event_policy"] == {
        "identity_prefix": "selected",
        "delivery": True,
        "voiceover_subject_override": False,
    }

    absent = _run_node(_mount_script({}, body=
        "console.log(JSON.stringify(editor.collect()));\n"))
    assert "vocal_event" not in absent.get("capabilities", {})


@pytest.mark.parametrize("raw_policy", [
    {"identity_prefix": "selected", "delivery": True,
     "voiceover_subject_override": True, "future_flag": "keep-me"},
    {"identity_prefix": "selected"},
    "malformed-but-preserved",
])
def test_untouched_malformed_vocal_event_policy_round_trips_byte_for_byte(
        raw_policy):
    seed = {"capabilities": {"vocal_event": {
        "placement": "inline", "event_policy": raw_policy}}}
    collected = _run_node(_mount_script(
        seed, body="console.log(JSON.stringify(editor.collect()));\n"))
    assert collected["capabilities"]["vocal_event"]["event_policy"] == raw_policy


def test_a_custom_format_declaring_its_own_vocabulary_compiles_with_no_fallback():
    """Phase F gate — the seam is proven by a format that is not MiniMax."""
    definition = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    definition["capabilities"]["reference"]["derived"] = {
        "summary": {
            "order": 1, "channel_key": "sounds", "placement": "channel_suffix",
            "label": "Brief", "description": "A house-style brief.",
            "example": "[house brief] ...", "help": "Presentation only.",
            "fields": {"task_types": {
                "type": "enum_multi", "label": "House operations",
                "values": [{"value": "storyboard", "label": "Storyboard"},
                           {"value": "colour pass", "label": "Colour pass"}],
            }},
        },
    }
    house = prompt_context.normalize_profile({
        "profile_id": "house", "version": "1", "name": "House", **definition})
    assert not prompt_context.profile_declaration_errors(
        house, template={"id": "sonder", "channels": [
            {"key": "visual"}, {"key": "speech"}, {"key": "sounds"}]})

    attachment = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "reference",
        "source": {"reference_item_id": "item"},
        "capabilities": [{"capability_id": "summary", "kind": "summary",
                          "enabled": True,
                          "config": {"task_types": ["colour pass", "storyboard"]}}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channels": {"visual": "A corridor."}, "attachments": [attachment],
        }], window_start=0, window_end=10, template="sonder", profile=house,
        context={"generic_references": {"item": {
            "prompt": "reference text", "compatible_profiles": ["house@1"],
            "exposed_capabilities": ["summary"]}}})

    assert not compiled["errors"], compiled["errors"]
    # The declared channel, and canonical DECLARED order rather than the order
    # the chip happened to store — the same rule MiniMax task types follow.
    assert "[storyboard + colour pass]" in compiled["channels"]["sounds"]
    # No MiniMax fallback anywhere in the result: not its task vocabulary, and
    # not its destination channels — this format routes to its own.
    serialized = json.dumps(compiled)
    for literal in ("reference generation", "video editing", "subject_definitions",
                    "retention_analysis", "detailed_description"):
        assert literal not in serialized, literal
    # The declared placement is honoured and the unrelated authored channel is
    # untouched: the brief trails `sounds`, and nothing lands in `visual`.
    assert compiled["channels"]["visual"] == "A corridor."
    assert compiled["channels"]["sounds"].endswith("[storyboard + colour pass]")

    # A value outside the declared vocabulary is preserved and reported, never
    # coerced into the declared set.
    unknown = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "reference",
        "source": {"reference_item_id": "item"},
        "capabilities": [{"capability_id": "summary", "kind": "summary",
                          "enabled": True,
                          "config": {"task_types": ["reference generation"]}}],
    })
    reported = prompt_context.compile_prompt_context(
        global_channels={}, sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channels": {"visual": ""}, "attachments": [unknown],
        }], window_start=0, window_end=10, template="sonder", profile=house,
        context={"generic_references": {"item": {
            "prompt": "reference text", "compatible_profiles": ["house@1"],
            "exposed_capabilities": ["summary"]}}})
    assert "unknown_declared_field_value" in {
        row["code"] for row in reported["errors"] + reported["warnings"]}


def _house_profile(*, channel, placement):
    """A custom format whose Reference-prompt routing the test can move."""
    definition = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    definition["capabilities"]["reference"]["derived"] = {
        "derived_prompt": {
            "order": 1, "channel_key": channel, "placement": placement,
            "label": "Brief", "fields": {},
        },
    }
    return prompt_context.normalize_profile({
        "profile_id": "house", "version": "1", "name": "House", **definition})


def _compile_house(profile, capability):
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "reference",
        "source": {"reference_item_id": "item"},
        "capabilities": [capability],
    })
    return prompt_context.compile_prompt_context(
        global_channels={}, sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channels": {"visual": "A corridor."}, "attachments": [attachment],
        }], window_start=0, window_end=10, template="sonder", profile=profile,
        context={"generic_references": {"item": {
            "prompt": "reference text", "compatible_profiles": ["house@1"],
            "exposed_capabilities": ["derived_prompt"]}}})


def test_blank_capability_routing_follows_a_changed_declaration():
    """The manual row 117 failure: the panel moved, compiled output did not.

    A chip attached under one format froze the declared channel and placement
    into its record, and `_route_for` prefers a stored value — so changing the
    format re-routed nothing. Blank routing must follow the declaration.
    """
    blank = {"capability_id": "derived_prompt", "kind": "derived_prompt",
             "enabled": True, "channel_key": "", "placement": "", "config": {}}

    before = _compile_house(
        _house_profile(channel="sounds", placement="channel_suffix"), blank)
    assert not before["errors"], before["errors"]
    assert "reference text" in before["channels"]["sounds"]
    assert not before["channels"]["speech"]

    after = _compile_house(
        _house_profile(channel="speech", placement="section_prefix"), blank)
    assert not after["errors"], after["errors"]
    # The emission MOVED with the declaration — the whole point of the fix.
    assert "reference text" in after["channels"]["speech"]
    assert not after["channels"]["sounds"]


def test_a_genuinely_deviating_capability_still_overrides_the_declaration():
    """Sparse routing must not weaken a real override."""
    override = {"capability_id": "derived_prompt", "kind": "derived_prompt",
                "enabled": True, "channel_key": "speech",
                "placement": "section_prefix", "config": {}}
    compiled = _compile_house(
        _house_profile(channel="sounds", placement="channel_suffix"), override)
    assert not compiled["errors"], compiled["errors"]
    assert "reference text" in compiled["channels"]["speech"]
    assert not compiled["channels"]["sounds"]


def test_chip_editor_round_trip_keeps_inherited_routing_blank():
    """Opening and saving a chip must not materialize the format default.

    Guards the two sites that silently undid sparse routing: the `current`
    literal seeded `placement` from the declaration, and the placement select
    fell back to it, so the save wrote the default back as if authored.
    """
    module = (ROOT / "web/js/prompt_context_chips.js").as_uri()
    result = _run_node("\n".join([
        f"const mod = await import({json.dumps(module)});",
        "const current = {capability_id: 'summary', kind: 'summary', enabled: true};",
        "console.log(JSON.stringify({",
        # An untouched row reports "" for both selects (the Provider default
        # option), which must round-trip to a record storing neither key.
        "  inherited: mod.sparseCapabilityRecord(current,"
        " {capabilityId: 'summary', enabled: true, channelKey: '', placement: ''}),",
        # A real deviation is still stored.
        "  deviating: mod.sparseCapabilityRecord(current,"
        " {capabilityId: 'summary', enabled: true, channelKey: 'sounds',"
        " placement: 'channel_suffix'}),",
        # Resetting one axis clears only that axis.
        "  reset: mod.sparseCapabilityRecord("
        "{...current, channel_key: 'sounds', placement: 'channel_suffix'},"
        " {capabilityId: 'summary', enabled: true, channelKey: '', placement: 'inline'}),",
        "}));",
    ]))
    # `enabled` matching the inherited default is dropped, exactly like the two
    # routing axes: only a genuine deviation is stored.
    assert result["inherited"] == {
        "capability_id": "summary", "kind": "summary"}
    assert result["deviating"]["channel_key"] == "sounds"
    assert result["deviating"]["placement"] == "channel_suffix"
    # Reset drops the stored key entirely rather than writing the default back.
    assert "channel_key" not in result["reset"]
    assert result["reset"]["placement"] == "inline"

    lines = (ROOT / "web/js/prompt_context_chips.js").read_text(
        encoding="utf-8").splitlines()
    # Comments legitimately name what they forbid, so screen code only. This
    # single source assertion covers the one revert the projection cannot see:
    # the SELECT falling back to the declaration before the save ever runs.
    source = "\n".join(line for line in lines
                       if not line.lstrip().startswith(("//", "*", "/*")))
    assert "|| capabilityDeclaration?.placement" not in source, (
        "the placement select falls back to the declaration again")


def test_modal_draft_guard_confirms_only_when_dirty():
    module = (ROOT / "web/js/modal_draft_guard.js").as_uri()
    result = _run_node("\n".join([
        f"const mod = await import({json.dumps(module)});",
        "const asked = [];",
        "const text = {value: 'name'};",
        "const box = {type: 'checkbox', checked: false};",
        "const multi = {selectedOptions: [{value: 'a'}]};",
        "const controls = () => [text, box, multi];",
        "const guard = mod.createModalDraftGuard({controls,",
        "  confirm: (message) => { asked.push(message); return false; }});",
        "const clean = {dirty: guard.isDirty(), allowed: guard.confirmDismiss()};",
        "text.value = 'name edited';",
        "const dirtyText = {dirty: guard.isDirty(), allowed: guard.confirmDismiss()};",
        "text.value = 'name';",
        "box.checked = true;",
        "const dirtyBox = {dirty: guard.isDirty(), allowed: guard.confirmDismiss()};",
        "box.checked = false;",
        "multi.selectedOptions = [{value: 'a'}, {value: 'b'}];",
        "const dirtyMulti = {dirty: guard.isDirty(), allowed: guard.confirmDismiss()};",
        "multi.selectedOptions = [{value: 'a'}];",
        "const accepting = mod.createModalDraftGuard({controls,",
        "  confirm: () => true});",
        "text.value = 'changed again';",
        "console.log(JSON.stringify({clean, dirtyText, dirtyBox, dirtyMulti,",
        "  asked: asked.length, accepted: accepting.confirmDismiss()}));",
    ]))
    # A clean modal closes silently — the confirm is never reached.
    assert result["clean"] == {"dirty": False, "allowed": True}
    # Every control kind is observed, and a declined confirm keeps it open.
    assert result["dirtyText"] == {"dirty": True, "allowed": False}
    assert result["dirtyBox"] == {"dirty": True, "allowed": False}
    assert result["dirtyMulti"] == {"dirty": True, "allowed": False}
    assert result["asked"] == 3, "the confirm fires once per dismissal attempt"
    # Accepting the loss allows the close.
    assert result["accepted"] is True


def test_modal_dismissal_is_guarded_without_blocking_teardown():
    """Cancel and programmatic teardown must never prompt.

    `close` doubles as each modal's cleanup handle and runs when another editor
    opens or the panel unmounts; a confirm there would stall an unrelated flow.
    Escape must also claim the key even when the confirm is declined, or the
    Prompt panel's own OVERLAY consumer closes the panel underneath the modal.
    """
    panel = (ROOT / "web/js/prompt_identity_panel.js").read_text(encoding="utf-8")
    editor = panel[panel.index("function openIdentityEditor("):
                   panel.index("export function mountPromptIdentityPanel(")]

    close_body = editor[editor.index("const close = () => {"):]
    close_body = close_body[:close_body.index("};")]
    assert "confirmDismiss" not in close_body, (
        "the dirty guard leaked into close(), which also runs during teardown")

    assert "cancel.addEventListener(\"click\", close);" in editor, (
        "Cancel must discard immediately — it is explicit intent")
    assert "event.target === backdrop && draftGuard.confirmDismiss()" in editor

    escape = editor[editor.index("keydown: (event) => {"):]
    escape = escape[:escape.index("},")]
    assert "if (draftGuard.confirmDismiss()) close();" in escape
    assert escape.rstrip().endswith("return true;"), (
        "Escape must claim the key even when the confirm is declined")

    # The attachment target picker now carries an authored override fieldset,
    # not just one <select>, so it is guarded on the two accidental gestures
    # like every other draft-holding modal. It was unguarded while it asked only
    # "where", when a dirty-confirm on a dropdown would have been noise.
    picker = panel[panel.index("function openAttachmentTargetPicker("):
                   panel.index("function openIdentityEditor(")]
    assert "createModalDraftGuard({" in picker
    assert "event.target === backdrop && draftGuard.confirmDismiss()" in picker
    assert 'cancel.addEventListener("click", close);' in picker
    picker_escape = picker[picker.index("keydown: (event) => {"):]
    picker_escape = picker_escape[:picker_escape.index("},")]
    assert "if (draftGuard.confirmDismiss()) close();" in picker_escape
    assert picker_escape.rstrip().endswith("return true;"), (
        "Escape must claim the key even when the confirm is declined")

    # The format editor carries far more state and is guarded the same way.
    format_panel = (ROOT / "web/js/editor_prompt_panel.js").read_text(encoding="utf-8")
    assert "createModalDraftGuard({" in format_panel
    assert "event.target === backdrop && draftGuard.confirmDismiss()" in format_panel
    assert 'cancel.addEventListener("click", close);' in format_panel


def test_declaration_editor_module_never_hardcodes_provider_vocabulary():
    """Secondary absence guard for the one module Phase F adds.

    A grep is not coverage on its own — the behavioural gates above are — but
    it catches a later author reintroducing a browser copy of the vocabulary
    this plan exists to centralize.
    """
    lines = (ROOT / "web/js/prompt_format_editor.js").read_text(
        encoding="utf-8").splitlines()
    # Comments are excluded deliberately: the module's own comments name the
    # vocabularies they forbid, and a guard that failed on prose would train
    # the next author to delete the explanation instead of the duplication.
    code = "\n".join(line for line in lines
                     if not line.lstrip().startswith(("//", "*", "/*"))).lower()
    for literal in ("minimax", "reference generation", "video editing",
                    "subject_definitions", "retention_analysis",
                    "derived_prompt", "audio_relationship"):
        assert literal not in code, literal
    # Anti-vacuity: the screen must be able to fail.
    assert "minimax" in "\n".join(lines).lower()


def test_format_menu_actions_are_gated_on_custom_ownership():
    """The gate is a predicate, so it is tested by running it."""
    module = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    custom = {"builtin": False, "fork_seed": {"capabilities": {
        "reference": {"derived": {"summary": {"order": 1}}}}}}
    result = _run_node("\n".join([
        f"const mod = await import({json.dumps(module)});",
        "console.log(JSON.stringify({",
        f"  custom: mod.promptFormatMenuActions({json.dumps(custom)}),",
        "  builtin: mod.promptFormatMenuActions({builtin: true}),",
        "  missing: mod.promptFormatMenuActions(undefined),",
        "}));",
    ]))
    assert result["custom"] == {"edit": True, "remove": True}
    # Built-ins are immutable, and an unresolved descriptor offers nothing.
    assert result["builtin"] == {"edit": False, "remove": False}
    assert result["missing"] == {"edit": False, "remove": False}
    # The removed migration action must not come back by accident.
    assert "migrate" not in json.dumps(result)


def test_delete_targets_list_every_custom_format_with_its_served_reason():
    """The reachability gate, tested by running it.

    Delete was unreachable by construction: the menu acted on the selected
    format, and selecting a format is exactly what the server counts as a
    usage. This predicate is what makes an UNUSED custom format reachable, so
    it is tested under node rather than by matching menu source.

    Mutation this must not survive: filtering the list down to the active
    descriptor again, or recomputing "in use" in the browser instead of reading
    the served usages.
    """
    module = (ROOT / "web/js/editor_prompt_panel.js").as_uri()
    profiles = [
        {"key": "generic@1", "name": "Generic", "builtin": True},
        {"key": "used@1", "name": "Used", "builtin": False,
         "profile_id": "used", "version": "1",
         "usages": [{"type": "scene", "scene_id": "s1", "scene_name": "Strut"}]},
        {"key": "free@1", "name": "Free", "builtin": False,
         "profile_id": "free", "version": "1", "usages": []},
        {"key": "tmpl@1", "name": "Templated", "builtin": False,
         "profile_id": "tmpl", "version": "1",
         "usages": [{"type": "channel_template", "name": "MiniMax H3"}]},
        # A custom format whose usages key is absent entirely must not be
        # treated as deletable-unknown; absent means none were reported.
        {"key": "bare@1", "name": "Bare", "builtin": False,
         "profile_id": "bare", "version": "1"},
    ]
    result = _run_node("\n".join([
        f"const mod = await import({json.dumps(module)});",
        f"const rows = mod.promptFormatDeleteTargets({json.dumps(profiles)});",
        "console.log(JSON.stringify({",
        "  keys: rows.map((r) => r.key),",
        "  deletable: rows.filter((r) => r.deletable).map((r) => r.key),",
        "  reasons: Object.fromEntries(rows.map((r) => [r.key, r.reason])),",
        "  expected: Object.fromEntries(rows.map((r) =>",
        "    [r.key, [r.profile_id, r.version, r.name]])),",
        "  empty: mod.promptFormatDeleteTargets([]).length,",
        "  missing: mod.promptFormatDeleteTargets(undefined).length,",
        "}));",
    ]))

    # Built-ins never appear: they are undeletable regardless of use.
    assert "generic@1" not in result["keys"]
    # Sorted by name so the list is stable between openings.
    assert result["keys"] == ["bare@1", "free@1", "tmpl@1", "used@1"]
    assert sorted(result["deletable"]) == ["bare@1", "free@1"]
    # The reason names the actual blocker rather than saying "in use".
    assert "Strut" in result["reasons"]["used@1"]
    assert "MiniMax H3" in result["reasons"]["tmpl@1"]
    assert result["reasons"]["free@1"] == ""
    # Exact prior values travel with the row, so the delete never parses the
    # key — a profile_id may legally contain the separator.
    assert result["expected"]["used@1"] == ["used", "1", "Used"]
    # The empty-project case is a real state the menu must render, not a crash.
    assert result["empty"] == 0 and result["missing"] == 0


def test_format_menu_defers_to_that_predicate_and_declares_no_migration():
    """Source-level only, and deliberately narrow.

    `mountPromptManagementPanel` needs a whole fullscreen host, so the menu
    wiring is not reachable under node. These assertions pin only that the menu
    defers to the tested predicate above — they are NOT coverage of the gate,
    which the predicate test owns.
    """
    panel = (ROOT / "web/js/editor_prompt_panel.js").read_text(encoding="utf-8")
    assert "const actions = promptFormatMenuActions(descriptor);" in panel
    # Disabled state goes through the theme helper, which dims the control as
    # well as blocking the click — `button.disabled` alone is invisible here.
    assert "setButtonDisabled(edit, !actions.edit);" in panel
    assert "setButtonDisabled(removeOne, !target.deletable);" in panel
    # Delete targets the LIST, never the selected descriptor: selecting a format
    # is what puts it in use, so acting on the selection made Delete reachable
    # only when the server was guaranteed to refuse it.
    assert "promptFormatDeleteTargets(host._promptContextCatalog?.profiles)" in panel
    assert 'type: "delete_prompt_context_profile",' in panel
    # The whole-list PUT deleted by omission and must not come back here.
    assert "_savePromptContextProfiles" not in panel
    # The declaration editor replaced the label-flattening role inputs.
    assert "mountPromptFormatDeclarationEditor({" in panel
    assert "declarations.collect()" in panel
    assert "declarations.cleanup()" in panel
    flatten = '.map((value) => typeof value === "string" ? value : value?.value)'
    assert flatten not in panel, "role labels are flattened again"

    # The legacy-routes migration is gone from every surface that carried it.
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    for source, name in ((panel, "editor_prompt_panel.js"),
                         (widget, "editor_widget.js")):
        for literal in ("migrate_prompt_context_profile",
                        "_migratePromptContextProfile", "Migrate this format"):
            assert literal not in source, f"{literal} survives in {name}"

    # The legacy-routes migration is gone from every surface that carried it.
    widget = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    for source, name in ((panel, "editor_prompt_panel.js"),
                         (widget, "editor_widget.js")):
        for literal in ("migrate_prompt_context_profile",
                        "_migratePromptContextProfile", "Migrate this format"):
            assert literal not in source, f"{literal} survives in {name}"
