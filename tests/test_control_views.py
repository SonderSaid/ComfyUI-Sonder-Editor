"""Output Views Phase 3A: the Relay's `views` section and its server authority.

control_manifest.read_views reads the section; control_views lists the outputs
a View could name, describes each View against the prompt (and the canvas's
inactive bodies), validates create/remove/label/order, and overlays the Sonder
App's own Views. A View is inspection only: never a preset member, never in
provenance, a seed loop or a widget writer. JS parity: test_control_views_js.py.
"""

import copy
import json

import pytest

from server import control_descriptors as cd, control_manifest as cm, control_views as cv, controls_snapshot


class PreviewImage:
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",)}}


class SaveVideo(PreviewImage):
    pass


class KSampler:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"seed": ("INT", {"default": 0, "min": 0, "max": 2 ** 32})}}


class SonderEditor:
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}


CLASSES = {"PreviewImage": PreviewImage, "SaveVideo": SaveVideo, "KSampler": KSampler, "SonderEditor": SonderEditor}


def node(class_type, inputs=None, title=None):
    body = {"class_type": class_type, "inputs": dict(inputs or {})}
    if title:
        body["_meta"] = {"title": title}
    return body


def view_entry(path, class_type="PreviewImage", status="ok", label="", order=0, **extra):
    return {"label": label, "order": order, "status": status, "path": path, "class_type": class_type,
            "title": extra.pop("title", class_type), **extra}


def relay(views=None, version=1, **sections):
    manifest = {"v": 1, "bindings": {}, "notes": {}, **sections}
    if views is not None:
        manifest["views"] = {"v": version, "items": views}
    return node("SonderControlRelay", {"manifest": json.dumps(manifest)})


def prompt_with(views=None, **extra):
    prompt = {"1": node("SonderEditor"), "6": node("KSampler", {"seed": 5}), "9": node("PreviewImage", title="Pass 1 preview"),
              "12:7": node("SaveVideo", title="Inner save"), "12:15:3": node("PreviewImage", title="Nested")}
    prompt.update(extra)
    prompt["90"] = relay(views)
    return prompt


def describe(prompt, **kwargs):
    return cd.describe_bindings(prompt, class_lookup=CLASSES.get, **kwargs)


# ── Manifest ─────────────────────────────────────────────────────────────────

def test_views_section_reads_tolerantly_and_another_version_turns_off_only_itself():
    text = json.dumps({"v": 1, "views": {"v": 1, "items": {
        "a": view_entry("9", label=" Preview ", order=3),
        "bad id!": view_entry("9"), "b": {"label": 5}, "c": "nope",
        "d": view_entry("12:7", "SaveVideo", participation="inactive",
                        inactive_reason={"mode": "muted", "by": "parent", "label": "Pass two"}),
    }}})
    parsed = cm.parse_manifest(text)
    assert set(parsed["views"]) == {"a", "d"} and parsed["views_error"] is None
    assert parsed["views"]["a"] == {"label": "Preview", "order": 3, "status": "ok", "path": "9", "class_type": "PreviewImage",
                                    "title": "PreviewImage"}
    assert parsed["views"]["d"]["inactive_reason"] == {"mode": "muted", "by": "parent", "label": "Pass two"}
    for version in (2, "1", True, None):
        if version is None:
            continue
        newer = cm.parse_manifest(json.dumps({"v": 1, "views": {"v": version, "items": {"a": view_entry("9")}}}))
        assert newer["views"] == {} and "another Sonder version" in newer["views_error"], version
    assert cm.read_views({"v": 1.0, "items": {"a": view_entry("9")}})[0]  # JSON 1.0 is 1
    assert cm.read_views({"items": {"constructor": view_entry("9")}}) == ({}, None)
    assert cm.read_views({"items": []}) == ({}, None) and cm.read_views("x") == ({}, None)


def test_views_cap_paths_and_depth():
    many = {f"v{i}": view_entry("9") for i in range(40)}
    assert len(cm.read_views({"v": 1, "items": many})[0]) == cm.MAX_VIEWS == 32
    assert cm.clean_path("12:15:3") == "12:15:3" and cm.clean_path("1:2:3:4:5:6:7:8") is not None
    for bad in ("", "1:2:3:4:5:6:7:8:9", "a::b", ":1", "1:", "a/b", "__proto__", "1:constructor", 5, None):
        assert cm.clean_path(bad) is None, bad
    # Depth with an inactive reason: manifest > views > items > id > reason > key.
    deep = {"v": 1, "views": {"v": 1, "items": {"a": view_entry("9", inactive_reason={"mode": "muted", "by": "self", "label": ""})}}}
    cm.parse_manifest(json.dumps(deep))


# ── Candidates ───────────────────────────────────────────────────────────────

def test_output_candidates_root_inner_nested_inactive_and_cap():
    prompt = prompt_with()
    authoring = {"20": {"class_type": "PreviewImage", "inputs": {}, "_meta": {"title": "Muted preview"}, "mode": "muted", "by": "self", "label": "Muted preview"},
                 "30:4": {"class_type": "SaveVideo", "inputs": {}, "_meta": {}, "mode": "bypassed", "by": "parent", "label": "Pass two"},
                 "21": {"class_type": "KSampler", "inputs": {}, "_meta": {}, "mode": "muted", "by": "self", "label": ""}}
    found, truncated = cv.output_candidates(prompt, authoring, class_lookup=CLASSES.get)
    assert not truncated
    rows = {row["path"]: row for row in found}
    # The Editor and the Relay are never Views; a non-output node never is one.
    assert set(rows) == {"9", "12:7", "12:15:3", "20", "30:4"}
    assert rows["9"] == {"path": "9", "class_type": "PreviewImage", "title": "Pass 1 preview", "host": "9", "rel": ""}
    assert (rows["12:15:3"]["host"], rows["12:15:3"]["rel"]) == ("12", "15:3")
    assert rows["20"]["inactive"] == {"mode": "muted", "by": "self", "label": "Muted preview"}
    assert rows["30:4"]["inactive"]["by"] == "parent"
    big = {str(i): node("PreviewImage") for i in range(1, 200)}
    listed, truncated = cv.output_candidates(big, None, class_lookup=CLASSES.get)
    assert len(listed) == cv.MAX_CANDIDATES and truncated


def test_v3_output_node_classproperty_counts():
    class V3Like:
        _OUTPUT_NODE = None

        @classmethod
        def _schema(cls):
            return True

    class Meta(type):
        @property
        def OUTPUT_NODE(cls):  # noqa: N802 - mirrors the V3 classproperty
            return True

    V3 = Meta("V3", (), {})
    assert cv.is_output_class("V3", {"V3": V3}.get)
    assert not cv.is_output_class("V3Like", {"V3Like": V3Like}.get)
    assert not cv.is_output_class("SonderEditor", CLASSES.get)  # never, whatever its class says


# ── Describe ─────────────────────────────────────────────────────────────────

def test_describe_ok_inactive_missing_kind_and_candidates_minus_views():
    views = {"a": view_entry("9", label="First pass"), "b": view_entry("12:7", "SaveVideo", title="Inner save"),
             "m": view_entry("20", participation="inactive", inactive_reason={"mode": "muted", "by": "self", "label": "x"}),
             "gone": view_entry("44"), "k": view_entry("6", "KSampler"), "dup": {"label": "", "order": 0, "status": "duplicate"}}
    prompt = prompt_with(views)
    authoring = {"20": {"class_type": "PreviewImage", "inputs": {}, "_meta": {}, "mode": "muted", "by": "self", "label": "Muted"}}
    view = describe(prompt, authoring=authoring)
    rows = {d["view_id"]: d for d in view["views"]}
    assert rows["a"]["status"] == "ok" and rows["a"]["participation"] == "active" and rows["a"]["runnable"] and rows["a"]["label"] == "First pass"
    assert rows["b"]["label"] == "Inner save" and rows["b"]["default_label"] == "Inner save"
    # Inactive is not missing: the canvas sent its body.
    assert rows["m"]["status"] == "ok" and rows["m"]["participation"] == "inactive" and not rows["m"]["runnable"]
    assert rows["m"]["reason"] == "Its node is muted."
    assert rows["gone"]["status"] == "missing" and "Refresh" in rows["gone"]["reason"]
    assert rows["k"]["status"] == "kind"
    assert rows["dup"]["status"] == "duplicate"
    # Candidates leave out outputs that already have a View.
    assert {c["path"] for c in view["view_candidates"]} == {"12:15:3"}
    assert "_view_inventory" in view and "_view_inventory" not in cd.json_safe(view)
    # Without bodies (an import), an exported inactive View stays inactive, never missing.
    imported = describe(prompt)
    assert {d["view_id"]: (d["status"], d["participation"]) for d in imported["views"]}["m"] == ("ok", "inactive")


def test_view_section_error_and_relay_error_keep_controls():
    prompt = prompt_with()
    prompt["90"] = relay({"a": view_entry("9")}, version=2)
    view = describe(prompt)
    assert view["views"] == [] and "another Sonder version" in view["views_error"]
    assert view["view_candidates"]  # candidates still listed


# ── Validation ───────────────────────────────────────────────────────────────

def test_create_remove_label_order_and_refusals():
    prompt = prompt_with({"a": view_entry("9")})
    view = describe(prompt, authoring={})
    cleaned, rejected = cd.validate_binding_changes(view, {"view:n1:create": {"candidate": {"path": "12:7"}, "label": " Save "}},
                                                    structural="canvas")
    assert not rejected
    plan = cleaned["view:n1:create"]
    assert plan["candidate"] == {"path": "12:7", "class_type": "SaveVideo", "host": "12", "rel": "7"} and plan["label"] == "Save"
    for changes, reason in [
        ({"view:n2:create": {"candidate": {"path": "9"}}}, "already has a View"),
        ({"view:n2:create": {"candidate": {"path": "6"}}}, "no longer in the workflow"),
        ({"view:n2:create": {"candidate": {"path": "../9"}}}, "Choose an output"),
        ({"view:a:create": {"candidate": {"path": "12:7"}}}, "already in use"),
        ({"view:n2:create": {"candidate": {"path": "12:7"}}, "view:n3:create": {"candidate": {"path": "12:7"}}}, "same output"),
        ({"view:a:remove": False}, "Send true"),
        ({"view:a:label": 7}, "label"),
        ({"view:a:order": 99999}, "order"),
        ({"view:a:reset": True}, "Sonder App"),
        ({"view:zz:label": "x"}, "unavailable"),
    ]:
        _, rejected = cd.validate_binding_changes(view, changes, structural="canvas")
        assert rejected and reason in rejected[0]["reason"], (changes, rejected)
    # A retry of a landed create and a remove of a gone View change nothing.
    assert cd.validate_binding_changes(view, {"view:a:create": {"candidate": {"path": "9"}}}, structural="canvas")[0] == \
        {"view:a:create": {"noop": True}}
    assert cd.validate_binding_changes(view, {"view:zz:remove": True}, structural="canvas")[0] == {"view:zz:remove": {"noop": True}}
    ok, _ = cd.validate_binding_changes(view, {"view:a:label": " Look ", "view:a:order": 2}, structural="canvas")
    assert ok == {"view:a:label": "Look", "view:a:order": 2}
    # The mounted pre-check checks shape only; the canvas writer revalidates.
    shape, rejected = cd.validate_binding_changes(view, {"view:n9:create": {"candidate": {"path": "77"}}}, structural="relay")
    assert not rejected and shape["view:n9:create"] == {"candidate": {"path": "77"}, "label": ""}
    # Views never change what a run submits: the JS changesRun test covers the save gate
    # (test_control_views_js.py), and a View write plan names no widget or binding.
    plan, _ = cd.validate_binding_changes(view, {"view:n9:create": {"candidate": {"path": "12:7"}}}, structural="canvas")
    assert set(plan["view:n9:create"]) == {"candidate", "label", "order"}
    assert set(plan["view:n9:create"]["candidate"]) == {"path", "class_type", "host", "rel"}


def test_view_cap_counts_the_batch():
    views = {f"v{i}": view_entry(str(100 + i)) for i in range(cm.MAX_VIEWS - 1)}
    prompt = prompt_with(views, **{str(100 + i): node("PreviewImage") for i in range(cm.MAX_VIEWS + 1)})
    view = describe(prompt, authoring={})
    _, rejected = cd.validate_binding_changes(view, {"view:x1:create": {"candidate": {"path": "9"}},
                                                     "view:x2:create": {"candidate": {"path": "12:7"}}}, structural="canvas")
    assert rejected and "At most 32 Views" in rejected[0]["reason"]


def test_views_never_enter_presets_provenance_or_seeds():
    prompt = prompt_with({"a": view_entry("9")})
    prompt["90"] = relay({"a": view_entry("9")}, bindings={"seed": {"label": "", "status": "ok", "targets": [{"path": "6", "input": "seed"}]}})
    view = describe(prompt, authoring={})
    _, rejected = cd.validate_binding_changes(view, {"preset:p1:create": {"name": "P", "members": ["a"]}}, prompt=prompt, structural="canvas")
    assert rejected and "View can't be part of a preset" in rejected[0]["reason"]
    # Controls at run: a saved asset's provenance names its settings, never a View.
    prompt["9"]["inputs"]["images"] = ["6", 0]
    snapshot = controls_snapshot.controls_snapshot(prompt, ["9"], class_lookup=CLASSES.get)
    assert snapshot is not None and "PreviewImage" not in json.dumps(snapshot) and "views" not in json.dumps(snapshot)
    # The SDK curation delta and seed loop read bindings only.
    delta = controls_snapshot.sdk_overlay_delta(view, prompt)
    assert "view" not in json.dumps(delta or {})
    assert all(b.get("binding_id") != "a" for b in view["bindings"])


# ── Sonder App overlay ───────────────────────────────────────────────────────

def test_sdk_overlay_copy_remove_app_only_and_switch_off():
    prompt = prompt_with({"a": view_entry("9"), "b": view_entry("12:7", "SaveVideo")})
    stored = {"views": {"a": {"label": "Mine", "order": 5, "source": {"path": "9", "class_type": "PreviewImage"}},
                        "b": {"removed": True},
                        "c": {"label": "", "order": 1, "source": {"path": "12:15:3", "class_type": "PreviewImage"}},
                        "x": {"label": "", "order": 0, "source": {"path": "9", "class_type": "SaveVideo"}},
                        "bad": {"source": {"path": "../x", "class_type": "P"}}}}
    view = describe(copy.deepcopy(prompt))
    view["_node_modes"] = {"12": "muted"}
    cv.apply_sdk_views(view, prompt, stored, class_lookup=CLASSES.get)
    rows = {d["view_id"]: d for d in view["views"]}
    assert set(rows) == {"a", "c", "x"}
    assert rows["a"]["origin"] == "app_copy" and rows["a"]["label"] == "Mine" and rows["a"]["order"] == 5
    assert rows["c"]["origin"] == "app" and rows["c"]["participation"] == "inactive"  # its instance is off in the app
    assert rows["x"]["status"] == "missing"  # a class mismatch never resolves
    assert {c["path"] for c in view["view_candidates"]} == {"12:7"}
    # The sidecar's store: copy-on-write, tombstones for the workflow's own, reset deletes.
    data = {"views": {}}
    cv.store_sdk_change(data, view, "a", "order", 9)
    assert data["views"]["a"]["order"] == 9 and data["views"]["a"]["source"]["path"] == "9"
    cv.store_sdk_change(data, view, "a", "reset", True)
    assert "a" not in data["views"]
    cv.store_sdk_change(data, {"views": [{"view_id": "a", "origin": "authored"}]}, "a", "remove", True)
    assert data["views"]["a"] == {"removed": True}
    cv.store_sdk_change(data, {"views": [{"view_id": "c", "origin": "app"}]}, "c", "remove", True)
    assert "c" not in data["views"]
    cv.store_sdk_change(data, view, "n", "create", {"candidate": {"path": "12:7", "class_type": "SaveVideo"}, "label": "", "order": 2})
    assert data["views"]["n"] == {"label": "", "order": 2, "source": {"path": "12:7", "class_type": "SaveVideo"}}


def test_curated_views_materialize_by_source():
    prompt = prompt_with({"a": view_entry("9")})
    view = describe(prompt)
    cv.apply_sdk_views(view, prompt, {"views": {"c": {"label": "Inner", "order": 1, "source": {"path": "12:7", "class_type": "SaveVideo"}}}},
                       class_lookup=CLASSES.get)
    section = cv.curated_views(view)
    assert section["v"] == 1 and set(section["items"]) == {"a", "c"}
    assert section["items"]["c"] == {"label": "Inner", "order": 1, "status": "ok", "path": "12:7", "class_type": "SaveVideo",
                                     "title": "Inner save"}
    assert cm.read_views(section)[0]["c"]["path"] == "12:7"


# ── Sonder App sidecar (sdk_templates) ───────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    import sys
    import types
    from server import sdk_templates as st
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(get_user_directory=lambda: str(tmp_path)))
    monkeypatch.setattr(cd, "_comfy_class_lookup", CLASSES.get)
    prompt = {"6": node("KSampler", {"seed": 5}), "9": node("PreviewImage", title="Pass 1 preview"),
              "12:7": node("SaveVideo", title="Inner save")}
    validation = lambda p: {"ok": True, "warnings": [], "errors": [], "description": ""}
    identity = st.import_template(json.dumps(prompt).encode(), validate=validation)["template_id"]

    def snapshot():
        stored = st.read_control_values(identity)
        current, _ = st.load_template(identity)
        return current, stored, st.apply_stored_values(current, st.describe_stored(current, stored), stored)

    def write(changes):
        current, _stored, projected = snapshot()
        cleaned, refused = cd.validate_binding_changes(projected, changes, prompt=current, structural="sdk")
        assert not refused, refused
        return st.store_binding_changes(identity, cleaned, projected, current)

    return st, identity, prompt, validation, snapshot, write


def test_sdk_views_only_workflow_gets_a_relay_on_export_and_survives_replace(app):
    st, identity, prompt, validation, snapshot, write = app
    # No Relay in the import: candidates still list its outputs.
    assert {c["path"] for c in snapshot()[2]["view_candidates"]} == {"9", "12:7"}
    data = write({"view:n1:create": {"candidate": {"path": "12:7"}, "label": "Second pass"}})
    assert data["views"]["n1"]["source"] == {"path": "12:7", "class_type": "SaveVideo"}
    views = snapshot()[2]["views"]
    assert [(d["view_id"], d["origin"], d["status"], d["label"]) for d in views] == [("n1", "app", "ok", "Second pass")]
    # Export materializes a Relay for Views alone; the import reads them back.
    exported = json.loads(st.export_curated(identity))
    relay_node = cm.find_relay(exported)
    assert relay_node["status"] == "ok" and relay_node["manifest"]["views"]["n1"]["path"] == "12:7"
    reimported = describe(exported)
    assert reimported["views"][0]["status"] == "ok" and reimported["views"][0]["label"] == "Second pass"
    # Replace keeps the app's Views (the sidecar is copy-on-write over the workflow's).
    st.replace_template(identity, json.dumps(prompt).encode(), validate=validation)
    assert [d["view_id"] for d in snapshot()[2]["views"]] == ["n1"]
    # A replacement without that output reads missing, never another node.
    smaller = {key: value for key, value in prompt.items() if key != "12:7"}
    st.replace_template(identity, json.dumps(smaller).encode(), validate=validation)
    assert snapshot()[2]["views"][0]["status"] == "missing"


def test_sdk_copy_on_write_reset_remove_and_projection_cap(app):
    st, identity, prompt, validation, snapshot, write = app
    authored = {**prompt, "90": relay({"a": view_entry("9")})}
    st.replace_template(identity, json.dumps(authored).encode(), validate=validation)
    write({"view:a:label": "Mine"})
    row = snapshot()[2]["views"][0]
    assert (row["origin"], row["label"]) == ("app_copy", "Mine")
    write({"view:a:reset": True})
    assert snapshot()[2]["views"][0]["origin"] == "authored"
    write({"view:a:remove": True})
    assert snapshot()[2]["views"] == [] and snapshot()[1]["views"]["a"] == {"removed": True}
    assert json.loads(st.export_curated(identity))["90"]["inputs"]["manifest"].count('"a"') == 0
    # Tombstones are not Views: 32 shown plus a removed stub still fit.
    st.update_control_values(identity, lambda data: data["views"].update(
        {f"k{i}": {"label": "", "order": i, "source": {"path": "9", "class_type": "PreviewImage"}} for i in range(cm.MAX_VIEWS)}))
    write({"view:k1:label": "Still fits"})
    assert snapshot()[1]["views"]["a"] == {"removed": True}
    # The projection check refuses a sidecar the export could not carry.
    st.update_control_values(identity, lambda data: data["views"].update(
        {f"x{i}": {"label": "y" * 120, "order": i, "source": {"path": "9", "class_type": "PreviewImage"}} for i in range(1200)}))
    with pytest.raises(st.TemplateError):
        write({"view:x1:label": "z"})


def test_sdk_renaming_a_view_off_at_export_keeps_it_inactive_and_exported(app):
    """An authored View whose node was muted at export is not in the API prompt:
    the app's copy (a rename, a move) stays that View, inactive, not missing."""
    st, identity, prompt, validation, snapshot, write = app
    off = view_entry("40", participation="inactive", inactive_reason={"mode": "muted", "by": "self", "label": ""}, title="Muted preview")
    authored = {**prompt, "90": relay({"a": view_entry("9"), "m": off})}
    st.replace_template(identity, json.dumps(authored).encode(), validate=validation)
    rows = {d["view_id"]: d for d in snapshot()[2]["views"]}
    assert (rows["m"]["status"], rows["m"]["participation"], rows["m"]["origin"]) == ("ok", "inactive", "authored")
    # Manage's move renumbers every View: each becomes an app copy.
    write({"view:m:label": "Muted pass", "view:m:order": 0, "view:a:order": 1})
    rows = {d["view_id"]: d for d in snapshot()[2]["views"]}
    assert (rows["m"]["status"], rows["m"]["participation"], rows["m"]["origin"], rows["m"]["label"]) == \
        ("ok", "inactive", "app_copy", "Muted pass")
    assert rows["m"]["reason"] == cv.OFF_AT_EXPORT_REASON and not rows["m"]["runnable"]
    exported = cm.find_relay(json.loads(st.export_curated(identity)))["manifest"]["views"]
    assert exported["m"]["participation"] == "inactive" and exported["m"]["path"] == "40" and exported["m"]["label"] == "Muted pass"
