"""Cross-lane subject registry and project-scoped prompt tokens (plan P3).

Numbering is by STAGING order — lane, then item, then member. Prompt sections
carry durable `subject_ids` bindings, but they no longer feed composition and so
must not influence these numbers.
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.timeline_state import (
    Asset, LaneConfig, PromptSection, ReferenceEntity, ReferenceItem, ReferenceMember,
    Scene, TimelineProject,
)


ROOT = Path(__file__).resolve().parents[1]
_CORE_PACKAGE = "reference_registry_testpkg"


def _core(monkeypatch):
    if _CORE_PACKAGE not in sys.modules:
        package = types.ModuleType(_CORE_PACKAGE)
        package.__path__ = [str(ROOT)]
        monkeypatch.setitem(sys.modules, _CORE_PACKAGE, package)
    module_name = f"{_CORE_PACKAGE}.nodes.reference_core"
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


# --- fixtures -------------------------------------------------------------------

def _project():
    project = TimelineProject(name="Project")
    project.assets = [
        Asset(asset_id="img-a", path="media/a.png", asset_type="image"),
        Asset(asset_id="img-b", path="media/b.png", asset_type="image"),
        Asset(asset_id="aud-a", path="media/a.wav", asset_type="audio"),
    ]
    chloe = ReferenceEntity(reference_id="ent_chloe", name="Chloe",
                            description="the young woman with long dark hair")
    chloe.members = [
        ReferenceMember(member_id="m-chloe-img", asset_id="img-a", order=0),
        ReferenceMember(member_id="m-chloe-voice", asset_id="aud-a", order=1,
                        tags=["sonder:voice_identity"]),
    ]
    letter = ReferenceEntity(reference_id="ent_letter", name="Letter",
                             description="the folded letter she is holding")
    letter.members = [ReferenceMember(member_id="m-letter-img", asset_id="img-b", order=0)]
    project.references = [chloe, letter]

    scene = Scene(scene_id="scene-1", duration_frames=360)
    scene.prompt_track_config = LaneConfig()
    scene.prompt_sections = [
        PromptSection(0, 240, channels={"visual": "a"}),
        PromptSection(240, 360, channels={"visual": "b"}),
    ]
    project.scenes = [scene]
    return project, scene


def _lane(members):
    item = ReferenceItem(lane_index=0, start_frame=0, end_frame=360)
    item.members = [{"member_id": member_id} for member_id in members]
    return {"item": item}


# --- registry ---------------------------------------------------------------------

def test_registry_numbers_the_four_populations_independently(monkeypatch):
    core = _core(monkeypatch)
    project, _scene = _project()
    # An image lane and an audio lane, both staging Chloe.
    lanes = [_lane(["m-chloe-img", "m-letter-img"]), _lane(["m-chloe-voice"])]
    registry = core.build_reference_registry(project, lanes)

    assert registry["subjects"] == {"ent_chloe": 1, "ent_letter": 2}
    assert registry["pictures"] == {"m-chloe-img": 1, "m-letter-img": 2}
    assert registry["audios"] == {"m-chloe-voice": 1}
    # A speaker is a DIFFERENT population, which is why MiniMax's own example
    # pairs <Subject 3> with (S1).
    assert registry["speakers"] == {"ent_chloe": 1}


def test_same_entity_gets_one_subject_number_across_lanes(monkeypatch):
    core = _core(monkeypatch)
    project, scene = _project()
    lanes = [_lane(["m-chloe-img"]), _lane(["m-chloe-voice"])]
    registry = core.build_reference_registry(project, lanes)
    image_numbers = core.registry_numbers_for(
        registry, project.references[0], project.references[0].members[0])
    audio_numbers = core.registry_numbers_for(
        registry, project.references[0], project.references[0].members[1])
    assert image_numbers["subject_n"] == audio_numbers["subject_n"] == 1
    # …while picture/audio/speaker number independently.
    assert image_numbers["picture_n"] == 1 and image_numbers["audio_n"] == 0
    assert audio_numbers["audio_n"] == 1 and audio_numbers["picture_n"] == 0
    assert audio_numbers["speaker_n"] == 1


def test_registry_numbers_by_staging_order_not_prompt_sections(monkeypatch):
    # Staging order IS the rule (plan decision 8). The prompt sections here bind
    # Chloe first, and that must NOT influence numbering: a section's
    # subject_ids are durable but no longer feed composition, so letting them
    # seed the order would make the Bridge sockets depend on prompt-lane state
    # they cannot see.
    core = _core(monkeypatch)
    project, scene = _project()
    registry = core.build_reference_registry(
        project, [_lane(["m-letter-img", "m-chloe-img"])])
    assert registry["subjects"] == {"ent_letter": 1, "ent_chloe": 2}


def test_lane_order_wins_over_member_order_across_lanes(monkeypatch):
    core = _core(monkeypatch)
    project, _scene = _project()
    registry = core.build_reference_registry(
        project, [_lane(["m-letter-img"]), _lane(["m-chloe-img"])])
    assert registry["subjects"] == {"ent_letter": 1, "ent_chloe": 2}


def test_registry_tolerates_a_broken_member_link(monkeypatch):
    # The registry rides the selector fingerprint, so a dangling member on a
    # lane the user is not rendering must not raise.
    core = _core(monkeypatch)
    project, _scene = _project()
    registry = core.build_reference_registry(project, [_lane(["m-gone"])])
    assert registry["pictures"] == {}
    assert registry["subjects"] == {}


# --- token vocabulary --------------------------------------------------------------

def test_project_scoped_tokens_expand_from_the_registry(monkeypatch):
    core = _core(monkeypatch)
    fragment = core.member_prompt_fragment(
        "<Subject {subject_n}> is {prompt}, from <Picture {picture_n}> ({n})",
        0, "a redhead woman", "Chloe",
        {"subject_n": 3, "picture_n": 2, "audio_n": 0, "speaker_n": 1},
    )
    # The supplied index is the emitted slot position; registry ordinals are
    # independently project-scoped.
    assert fragment == "<Subject 3> is a redhead woman, from <Picture 2> (1)"


def test_speaker_token_is_independent_of_subject(monkeypatch):
    core = _core(monkeypatch)
    fragment = core.member_prompt_fragment(
        "<Subject {subject_n}> (S{speaker_n}) says {prompt}", 0, "hello", "Chloe",
        {"subject_n": 3, "speaker_n": 1})
    assert fragment == "<Subject 3> (S1) says hello"


def test_missing_registry_numbers_expand_to_zero(monkeypatch):
    core = _core(monkeypatch)
    assert core.member_prompt_fragment("<Subject {subject_n}>: {prompt}", 0, "x", "N", None) == (
        "<Subject 0>: x")


def test_empty_member_prompt_falls_back_to_the_entity_name(monkeypatch):
    # Previously emitted a dangling clause ("<Subject 2> is the  from ...")
    # because the name fallback only existed on the no-placeholder branch.
    core = _core(monkeypatch)
    fragment = core.member_prompt_fragment(
        "<Subject {subject_n}> is {prompt}, from <Picture {picture_n}>",
        1, "", "Chloe", {"subject_n": 2, "picture_n": 2})
    assert fragment == "<Subject 2> is Chloe, from <Picture 2>"
    # Never dropped: omitting it would hide a member whose image still reaches
    # the model.
    assert fragment


def test_existing_patterns_compose_identically(monkeypatch):
    # The four new tokens must be collision-free against {n} and {index}.
    core = _core(monkeypatch)
    assert core.member_prompt_fragment("image{index}", 2, "a cat", "Cat") == (
        "image2: a cat")
    assert core.member_prompt_fragment("", 0, "a cat", "Cat") == "a cat"
    assert core.member_prompt_fragment("ref{n}", 0, "", "Cat") == "ref1: Cat"


def test_member_suffix_changes_name_token_without_changing_legacy_blank(monkeypatch):
    core = _core(monkeypatch)
    assert core.member_prompt_fragment(
        "{name}|{entity_name}|{member_name}", 0, "", "Granny",
        None, "Front view") == "Granny_Front_view|Granny|Front_view"
    assert core.member_prompt_fragment("{name}", 0, "", "Granny") == "Granny"
    labels = core.reference_member_labels("Granny Bear", "Front / View")
    assert labels == {
        "entity_name": "Granny_Bear", "member_name": "Front_View",
        "name": "Granny_Bear_Front_View",
        "display_name": "Granny Bear · Front / View",
    }


# --- JS parity ---------------------------------------------------------------------

_FRAGMENT_CASES = [
    ["<Subject {subject_n}> is {prompt}, from <Picture {picture_n}> ({n})", 0,
     "a redhead woman", "Chloe",
     {"subject_n": 3, "picture_n": 2, "audio_n": 0, "speaker_n": 1}],
    ["<Subject {subject_n}> (S{speaker_n}) says {prompt}", 0, "hello", "Chloe",
     {"subject_n": 3, "speaker_n": 1}],
    ["<Subject {subject_n}> is {prompt}, from <Picture {picture_n}>", 1, "", "Chloe",
     {"subject_n": 2, "picture_n": 2}],
    ["image{index}", 2, "a cat", "Cat", None],
    ["", 0, "a cat", "Cat", None],
    ["ref{n}", 0, "", "Cat", None],
    ["<Audio {audio_n}> reused twice: {audio_n}", 0, "voice", "Chloe", {"audio_n": 4}],
    ["{name} only", 0, "", "Chloe", {"subject_n": 1}],
    ["{name}|{entity_name}|{member_name}", 0, "", "Granny", None, "Front view"],
]


def test_member_prompt_fragment_matches_between_python_and_javascript(monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the token parity test")
    core = _core(monkeypatch)
    expected = [core.member_prompt_fragment(*case) for case in _FRAGMENT_CASES]

    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        f"console.log(JSON.stringify({json.dumps(_FRAGMENT_CASES)}"
        f".map((c) => mod.memberPromptFragment(c[0], c[1], c[2], c[3], c[4], c[5]))));\n"
    )
    actual = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert actual == expected
    # Anti-vacuity: the fixtures must exercise real expansions, not all-empty.
    assert len([value for value in expected if value]) >= 7
    assert "<Subject 3>" in expected[0]


def test_javascript_derived_prompt_honors_emitted_slot_index():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for emitted-slot prompt coverage")
    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const {{ deriveReferencePrompt }} = await import({json.dumps(module_url)});
console.log(JSON.stringify(deriveReferencePrompt({{
  members: [{{prompt: 'villain', entity_name: 'Villain', slot_index: 4}}],
  soft: {{prompt_tokens: 'reference {{n}}'}},
}})));
"""
    actual = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert actual == "reference 5: villain"


def test_token_vocabulary_matches_between_python_and_javascript(monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the token parity test")
    core = _core(monkeypatch)
    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = (
        f"const mod = await import({json.dumps(module_url)});\n"
        "console.log(JSON.stringify(mod.PROJECT_SCOPED_PROMPT_TOKENS));\n"
    )
    actual = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert actual == list(core.PROJECT_SCOPED_PROMPT_TOKENS)
    assert len(actual) == 4
