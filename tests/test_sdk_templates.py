"""SDK templates, their Control values and the SDK editor position (server/sdk_templates.py)."""

import json
import os
import sys
import threading
import types

import pytest

from server import sdk_templates, session_registry
from server.sdk_templates import TemplateError


@pytest.fixture(autouse=True)
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    return tmp_path


INSTALLED = {"SonderEditor", "SonderSaveVideo", "SonderControl", "KSampler"}


def lookup(class_type):
    return object() if class_type in INSTALLED else None


def api_prompt(**extra_nodes):
    prompt = {
        "1": {"class_type": "SonderEditor", "inputs": {"project": "P", "project_name": "x", "fps": 24.0, "width": 960, "height": 544}},
        "2": {"class_type": "SonderSaveVideo", "inputs": {"project": ["1", 0], "mark_queue_complete": True}},
    }
    prompt.update(extra_nodes)
    return prompt


def describe_with(controls=(), fields=()):
    return lambda prompt: {"controls": list(controls), "fields": list(fields), "editors": ["1"]}


SEED_CONTROL = {"control_id": "abcd1234", "label": "Seed", "kind": "SEED", "status": "ok", "error": None, "keys": ["5"]}


def validate(prompt, controls=(SEED_CONTROL,), fields=()):
    return sdk_templates.validate_template(prompt, class_lookup=lookup, describe=describe_with(controls, fields))


def raw(prompt):
    return json.dumps(prompt).encode("utf-8")


# ── Parsing and validation ───────────────────────────────────────────────────

def test_a_ui_workflow_or_non_api_json_is_refused_with_a_reason():
    with pytest.raises(TemplateError) as exc:
        sdk_templates.parse_template_bytes(raw({"nodes": [], "links": [], "last_node_id": 1}))
    assert exc.value.code == "ui_format"
    for bad in (b"not json", b"[]", b"{}", raw({"1": {"inputs": {}}})):
        with pytest.raises(TemplateError):
            sdk_templates.parse_template_bytes(bad)
    with pytest.raises(TemplateError) as exc:
        sdk_templates.parse_template_bytes(b" " * (sdk_templates.MAX_TEMPLATE_BYTES + 1))
    assert exc.value.status == 413


def test_validation_requires_one_editor_a_reachable_save_and_installed_nodes():
    assert validate(api_prompt())["ok"]
    no_save = api_prompt()
    no_save["2"]["inputs"]["project"] = "elsewhere"
    assert "takes would not land" in " ".join(validate(no_save)["errors"])
    two = api_prompt(**{"9": {"class_type": "SonderEditor", "inputs": {}}})
    assert "more than one" in " ".join(validate(two)["errors"])
    missing = api_prompt(**{"7": {"class_type": "SomePackNode", "inputs": {}}})
    result = validate(missing)
    assert not result["ok"] and "SomePackNode" in " ".join(result["errors"])
    none = {"2": {"class_type": "KSampler", "inputs": {}}}
    assert "no Sonder Editor" in " ".join(validate(none)["errors"])


def test_validation_reports_editor_defaults_and_control_warnings():
    result = validate(api_prompt())
    assert result["editor_key"] == "1"
    assert result["editor_defaults"] == {"fps": 24.0, "width": 960, "height": 544}
    assert result["controls"][0]["control_id"] == "abcd1234"
    assert result["blocking"] == [] and result["warnings"] == []

    unlinked = dict(SEED_CONTROL, kind="INT", status="unlinked", label="Steps")
    result = validate(api_prompt(), controls=[unlinked])
    assert result["blocking"] == []
    assert any("not connected" in w for w in result["warnings"])
    assert any("No Seed Control" in w for w in result["warnings"])


@pytest.mark.parametrize("change, phrase", [
    ({"status": "stale_kind"}, "type no longer matches"),
    ({"status": "duplicate_id"}, "share its id"),
    ({"status": "unsupported"}, "can't set"),
    ({"status": "mixed_targets"}, "different types"),
    ({"error": "Value 9 is above the maximum 8."}, "above the maximum"),
    ({"control_id": ""}, "has no id"),
])
def test_generate_refusals_follow_the_umbrella_list(change, phrase):
    control = dict(SEED_CONTROL, **change)
    assert phrase in sdk_templates.generate_refusal(control)
    assert phrase in " ".join(validate(api_prompt(), controls=[control])["blocking"])


def test_unlinked_is_not_refused():
    assert sdk_templates.generate_refusal(dict(SEED_CONTROL, status="unlinked")) == ""


# ── Store ────────────────────────────────────────────────────────────────────

def test_import_keeps_the_exact_bytes_and_lists_rename_and_delete(user_dir):
    body = b'{"1": {"class_type": "SonderEditor", "inputs": {}},\n "2": {"class_type": "SonderSaveVideo", "inputs": {"project": ["1", 0]}}}'
    template = sdk_templates.import_template(body, source_name="C:/x/My i2va.json", validate=lambda p: validate(p))
    template_id = template["template_id"]
    assert sdk_templates.TEMPLATE_ID_RE.match(template_id)
    assert template["name"] == "My i2va"
    assert (user_dir / "sonder-editor" / "templates" / f"{template_id}.json").read_bytes() == body

    assert [t["template_id"] for t in sdk_templates.list_templates()] == [template_id]
    assert sdk_templates.rename_template(template_id, "  Dialogue   take ")["name"] == "Dialogue take"
    prompt, meta = sdk_templates.load_template(template_id)
    assert prompt["1"]["class_type"] == "SonderEditor" and meta["name"] == "Dialogue take"

    sdk_templates.delete_template(template_id)
    assert sdk_templates.list_templates() == []
    with pytest.raises(TemplateError) as exc:
        sdk_templates.load_template(template_id)
    assert exc.value.status == 404


def test_an_invalid_template_is_not_stored(user_dir):
    with pytest.raises(TemplateError) as exc:
        sdk_templates.import_template(raw({"2": {"class_type": "KSampler", "inputs": {}}}), validate=lambda p: validate(p))
    assert exc.value.details["validation"]["errors"]
    assert sdk_templates.list_templates() == []


@pytest.mark.parametrize("bad", ["", "../x", "ABCDEF0123456789", "0123", "0123456789abcdef/../"])
def test_template_ids_are_validated_before_any_path(bad):
    with pytest.raises(TemplateError) as exc:
        sdk_templates.check_template_id(bad)
    assert exc.value.code == "unknown_template"


def test_replace_keeps_the_id_and_control_values(user_dir):
    template_id = sdk_templates.import_template(raw(api_prompt()), validate=lambda p: validate(p))["template_id"]
    sdk_templates.update_control_values(template_id, lambda d: d["controls"].update({
        "abcd1234": {"seed": 7}, "gone0000": {"seed": 3},
    }))
    replaced = sdk_templates.replace_template(template_id, raw(api_prompt()), source_name="v2.json", validate=lambda p: validate(p))
    assert replaced["template_id"] == template_id and replaced["replaced_at"] > 0
    values = sdk_templates.read_control_values(template_id)
    # Present and removed Controls keep their values (removed ones are ignored at Generate).
    assert values["controls"] == {"abcd1234": {"seed": 7}, "gone0000": {"seed": 3}}
    assert values["fields"] == {} and values["version"] == 1


def test_control_values_preserve_unknown_keys_and_refuse_a_deleted_template(user_dir):
    template_id = sdk_templates.import_template(raw(api_prompt()), validate=lambda p: validate(p))["template_id"]
    path = user_dir / "sonder-editor" / "templates" / f"{template_id}.controls.json"
    path.write_text(json.dumps({"version": 1, "controls": {}, "fields": {}, "future": {"keep": True}}), encoding="utf-8")
    sdk_templates.update_control_values(template_id, lambda d: d["controls"].update({"abcd1234": {"seed": 1}}))
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["future"] == {"keep": True}

    sdk_templates.delete_template(template_id)
    with pytest.raises(TemplateError):
        sdk_templates.update_control_values(template_id, lambda d: None)
    assert not path.exists()


def test_a_corrupt_control_values_file_is_moved_aside_not_overwritten(user_dir):
    template_id = sdk_templates.import_template(raw(api_prompt()), validate=lambda p: validate(p))["template_id"]
    folder = user_dir / "sonder-editor" / "templates"
    path = folder / f"{template_id}.controls.json"
    path.write_text("{not json", encoding="utf-8")
    assert sdk_templates.read_control_values(template_id)["controls"] == {}
    aside = [name for name in os.listdir(folder) if ".controls.json.corrupt-" in name]
    assert len(aside) == 1
    assert (folder / aside[0]).read_text(encoding="utf-8") == "{not json"


def test_a_transient_read_error_is_raised_not_quarantined(user_dir, monkeypatch):
    from server import sdk_paths

    template_id = sdk_templates.import_template(raw(api_prompt()), validate=lambda p: validate(p))["template_id"]
    folder = user_dir / "sonder-editor" / "templates"
    path = folder / f"{template_id}.controls.json"
    path.write_text(json.dumps({"version": 1, "controls": {"abcd1234": {"seed": 4}}, "fields": {}}), encoding="utf-8")

    def busy(*args, **kwargs):
        raise PermissionError("sharing violation")

    with monkeypatch.context() as patch:
        patch.setattr(sdk_paths, "open", busy, raising=False)
        with pytest.raises(PermissionError):
            sdk_templates.read_control_values(template_id)
    assert sdk_templates.read_control_values(template_id)["controls"] == {"abcd1234": {"seed": 4}}
    assert not [name for name in os.listdir(folder) if ".corrupt-" in name]


def test_concurrent_control_value_writes_do_not_lose_updates(user_dir):
    template_id = sdk_templates.import_template(raw(api_prompt()), validate=lambda p: validate(p))["template_id"]

    def bump(_):
        for _ in range(20):
            def mutate(data):
                entry = data["controls"].setdefault("abcd1234", {"seed": 0})
                entry["seed"] += 1
            sdk_templates.update_control_values(template_id, mutate)

    threads = [threading.Thread(target=bump, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sdk_templates.read_control_values(template_id)["controls"]["abcd1234"]["seed"] == 80


# ── SDK editor position ──────────────────────────────────────────────────────

def test_editor_state_fields_match_the_relay_contract():
    assert set(sdk_templates.EDITOR_STATE_FIELDS) == session_registry.WIDGET_FIELD_NAMES


def test_editor_state_keeps_valid_values_and_drops_the_rest(user_dir):
    stored = sdk_templates.write_editor_state("My Project", {
        "scene_id": "scene-a",
        "selection_start": 40,
        "selection_end": 79,
        "pre_context_frames": 17,
        "post_context_frames": 999,  # above the editor's max (256)
        "take_placement_mode": "sideways",  # not an option
        "take_placement_muted": "yes",  # not a boolean
        "render_queue_active": False,
        "render_cache_max_bytes": 5,  # not an SDK editor value
        "unknown": 1,
    })
    assert stored == {
        "scene_id": "scene-a", "selection_start": 40, "selection_end": 79,
        "pre_context_frames": 17, "render_queue_active": False,
    }
    merged = sdk_templates.write_editor_state("My Project", {"selection_start": 41})
    assert merged["selection_start"] == 41 and merged["scene_id"] == "scene-a"
    assert sdk_templates.read_editor_state("My Project") == merged
    assert sdk_templates.read_editor_state("Other Project") == {}


@pytest.mark.parametrize("bad", ["../x", "", "a/b", "CON"])
def test_editor_state_refuses_unsafe_project_names(bad):
    with pytest.raises(TemplateError):
        sdk_templates.write_editor_state(bad, {"selection_start": 1})
