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
    assert not rejected and shape["view:n9:create"] == {"candidate": {"path": "77"}, "label": "", "feed": "output"}
    # Views never change what a run submits: the JS changesRun test covers the save gate
    # (test_control_views_js.py), and a View write plan names no widget or binding.
    plan, _ = cd.validate_binding_changes(view, {"view:n9:create": {"candidate": {"path": "12:7"}}}, structural="canvas")
    assert set(plan["view:n9:create"]) == {"candidate", "label", "order", "feed"}
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
    assert rejected and "View can't be part of a choice" in rejected[0]["reason"]
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


# ── Phase 4: sampling-preview Views (feed) ───────────────────────────────────

def test_feed_reads_as_sampling_or_absent_and_anything_else_skips_the_entry():
    items, error = cm.read_views({"v": 1, "items": {"s": {"feed": "sampling"}, "o": {"feed": "output"}, "d": {},
                                                     "bad": {"feed": "video"}, "num": {"feed": 1}}})
    assert error is None and set(items) == {"s", "o", "d"}
    assert items["s"]["feed"] == "sampling" and "feed" not in items["o"] and "feed" not in items["d"]


def test_sampling_candidates_inventory_and_one_view_per_node():
    prompt = prompt_with({"a": view_entry("9")}, **{"30": node("KSamplerAdvanced", title="Pass 2"),
                                                    "31": node("CustomSampler"), "32": node("ModelPreviewOverrideKJ")})
    authoring = {"40": {"class_type": "SamplerCustom", "inputs": {}, "_meta": {}, "mode": "muted", "by": "self", "label": "Off"}}
    view = describe(prompt, authoring=authoring)
    offered = {c["path"]: c for c in view["view_sampling_candidates"]}
    # The core classes only; a custom sampler is offered once a run saw it publish (the pane picker).
    assert set(offered) == {"6", "30", "32", "40"}
    assert offered["30"]["title"] == "Pass 2" and offered["40"]["inactive"]["mode"] == "muted"
    index = view["_view_sampling_index"]
    # Any node but an output node, the Editor or the Relay; uncapped, a path and a class only.
    assert {"6", "30", "31", "32", "40"} <= set(index) and index["31"] == "CustomSampler"
    assert not {"1", "90", "9", "12:7"} & set(index)
    assert "_view_sampling_index" not in cd.json_safe(view)
    # A node that has a View (any feed) is offered in neither list.
    prompt = prompt_with({"a": view_entry("9"), "s": view_entry("6", "KSampler", feed="sampling")})
    view = describe(prompt)
    assert "6" not in {c["path"] for c in view["view_sampling_candidates"]}
    assert "9" not in {c["path"] for c in view["view_candidates"]}


def test_describe_sampling_views_never_run_and_need_no_output_node():
    views = {"s": view_entry("6", "KSampler", feed="sampling"), "k": view_entry("6", "KSampler"),
             "e": view_entry("1", "SonderEditor", feed="sampling"),
             "m": view_entry("40", "SamplerCustom", feed="sampling")}
    prompt = prompt_with(views)
    authoring = {"40": {"class_type": "SamplerCustom", "inputs": {}, "_meta": {}, "mode": "bypassed", "by": "self", "label": ""}}
    rows = {d["view_id"]: d for d in describe(prompt, authoring=authoring)["views"]}
    assert rows["s"]["status"] == "ok" and rows["s"]["feed"] == "sampling" and rows["s"]["participation"] == "active"
    assert rows["s"]["runnable"] is False and "while another output runs" in rows["s"]["run_reason"]
    assert rows["k"]["status"] == "kind" and rows["k"]["feed"] == "output"  # an output View still needs an output
    assert rows["e"]["status"] == "unsupported"
    assert rows["m"]["participation"] == "inactive" and rows["m"]["reason"] == "Its node is bypassed."


def test_create_a_sampling_view_and_its_refusals():
    prompt = prompt_with({"a": view_entry("9")}, **{"31": node("CustomSampler")})
    view = describe(prompt, authoring={})
    cleaned, rejected = cd.validate_binding_changes(
        view, {"view:n1:create": {"candidate": {"path": "31"}, "feed": "sampling"}}, structural="canvas")
    assert not rejected and cleaned["view:n1:create"]["feed"] == "sampling"
    assert cleaned["view:n1:create"]["candidate"] == {"path": "31", "class_type": "CustomSampler", "host": "31", "rel": ""}
    for changes, reason in [
        ({"view:n2:create": {"candidate": {"path": "9"}, "feed": "sampling"}}, "output node"),
        ({"view:n2:create": {"candidate": {"path": "12:15:3"}, "feed": "sampling"}}, "output node"),
        ({"view:n2:create": {"candidate": {"path": "77"}, "feed": "sampling"}}, "switched off or no longer"),
        ({"view:n2:create": {"candidate": {"path": "31"}, "feed": "video"}}, "sampling preview"),
        ({"view:n2:create": {"candidate": {"path": "1"}, "feed": "sampling"}}, "switched off or no longer"),
        ({"view:n2:create": {"candidate": {"path": "../x"}, "feed": "sampling"}}, "Choose a node"),
        ({"view:n2:create": {"candidate": {"path": "31"}}}, "no longer in the workflow"),  # not an output node
    ]:
        _, rejected = cd.validate_binding_changes(view, changes, structural="canvas")
        assert rejected and reason in rejected[0]["reason"], (changes, rejected)
    shape, rejected = cd.validate_binding_changes(
        view, {"view:n9:create": {"candidate": {"path": "77"}, "feed": "sampling"}}, structural="relay")
    assert not rejected and shape["view:n9:create"]["feed"] == "sampling"


def test_a_sampling_view_has_no_timing():
    prompt = prompt_with({"s": view_entry("6", "KSampler", feed="sampling")})
    view = describe(prompt)
    _, rejected = cd.validate_binding_changes(
        view, {"view:s:timing": {"anchor": "render_start", "source_offset": 0, "fps": "editor"}}, structural="canvas")
    assert rejected and "own rate" in rejected[0]["reason"]
    cleaned, rejected = cd.validate_binding_changes(view, {"view:s:timing": None}, structural="canvas")
    assert not rejected and cleaned == {"view:s:timing": None}


def test_sdk_sampling_views_store_copy_export_and_switch_off():
    prompt = prompt_with({"s": view_entry("6", "KSampler", feed="sampling")}, **{"31": node("CustomSampler")})
    view = describe(copy.deepcopy(prompt))
    view["_node_modes"] = {"6": "muted"}
    stored = {"views": {"n": {"label": "", "order": 1, "source": {"path": "31", "class_type": "CustomSampler"},
                              "feed": "sampling"},
                        "odd": {"label": "", "order": 2, "source": {"path": "31", "class_type": "CustomSampler"},
                                "feed": "film"}}}
    cv.apply_sdk_views(view, prompt, stored, class_lookup=CLASSES.get)
    rows = {d["view_id"]: d for d in view["views"]}
    assert set(rows) == {"s", "n"}
    assert rows["n"]["feed"] == "sampling" and rows["n"]["status"] == "ok" and rows["n"]["origin"] == "app"
    assert rows["s"]["participation"] == "inactive"  # its sampler is switched off in the app
    assert {c["path"]: c.get("inactive", {}).get("mode") for c in view["view_sampling_candidates"]} == {}
    assert set(view["_view_sampling_index"]) >= {"31", "6"}
    # Renaming the workflow's own sampling View copies it whole: it stays a sampling View.
    data = {"views": {}}
    cv.store_sdk_change(data, view, "s", "label", "Pass 1 live")
    assert data["views"]["s"]["feed"] == "sampling" and data["views"]["s"]["label"] == "Pass 1 live"
    cv.store_sdk_change(data, view, "m", "create",
                        {"candidate": {"path": "31", "class_type": "CustomSampler"}, "label": "", "order": 3, "feed": "sampling"})
    assert data["views"]["m"]["feed"] == "sampling"
    assert cv.stored_views(data)["m"]["feed"] == "sampling"
    # Export as API keeps it a sampling View.
    section = cv.curated_views(view)
    assert section["items"]["n"]["feed"] == "sampling" and section["items"]["s"]["feed"] == "sampling"
    assert cm.read_views(section)[0]["n"]["feed"] == "sampling"


def test_the_mounted_publication_checks_feed_and_the_sampling_list():
    from server import session_registry as sr
    base = {"bindings": [], "notes": [], "views": [{"view_id": "s", "status": "ok", "feed": "sampling"}],
            "view_sampling_candidates": [{"path": "6"}]}

    def publish(view):
        return sr._prepare_control_publication({"view_seq": 1, "prompt_hash": "h", "view": view})

    assert publish(copy.deepcopy(base))["view"]["views"][0]["feed"] == "sampling"
    with pytest.raises(ValueError):
        publish({**copy.deepcopy(base), "views": [{"view_id": "s", "status": "ok", "feed": "film"}]})
    with pytest.raises(ValueError):
        publish({**copy.deepcopy(base), "view_sampling_candidates": [{"path": str(i)} for i in range(cv.MAX_CANDIDATES + 1)]})


def test_sdk_sampling_view_create_rename_export_and_reimport(app):
    st, identity, prompt, validation, snapshot, write = app
    assert {c["path"] for c in snapshot()[2]["view_sampling_candidates"]} == {"6"}
    data = write({"view:s1:create": {"candidate": {"path": "6"}, "feed": "sampling"}})
    assert data["views"]["s1"]["feed"] == "sampling"
    write({"view:s1:label": "Pass 1 live"})
    row = snapshot()[2]["views"][0]
    assert (row["feed"], row["label"], row["status"], row["runnable"]) == ("sampling", "Pass 1 live", "ok", False)
    assert snapshot()[2]["view_sampling_candidates"] == []
    # Export materializes a Relay carrying the feed; the import reads it back as a sampling View.
    exported = json.loads(st.export_curated(identity))
    assert cm.find_relay(exported)["manifest"]["views"]["s1"]["feed"] == "sampling"
    back = {d["view_id"]: d for d in describe(exported)["views"]}
    assert back["s1"]["feed"] == "sampling" and back["s1"]["status"] == "ok"


def test_a_sampling_view_on_an_output_node_or_a_subgraph_says_why():
    views = {"o": view_entry("9", "PreviewImage", feed="sampling"),
             "g": view_entry("44", "SamplerCustom", status="kind", feed="sampling"),
             "gone": view_entry("77", "SamplerCustom", status="missing", feed="sampling"),
             "t": view_entry("6", "KSampler", feed="sampling",
                             timing={"mode": "editor_window", "anchor": "render_start", "source_offset": 0, "fps": "editor"})}
    rows = {d["view_id"]: d for d in describe(prompt_with(views))["views"]}
    assert rows["o"]["status"] == "kind" and "give it an output View" in rows["o"]["reason"]
    assert rows["g"]["status"] == "kind" and "can't show sampling previews" in rows["g"]["reason"]
    assert rows["gone"]["status"] == "missing" and "add the node again" in rows["gone"]["reason"]
    # A stored timing on a sampling View is ignored: it plays at its own rate.
    assert rows["t"]["status"] == "ok" and rows["t"]["timing"] is None


def test_one_view_per_node_holds_across_feeds_in_imports_and_the_sonder_app():
    # A hand-made export with two Views on one node: the later one reads duplicate.
    views = {"a": view_entry("6", "KSampler", feed="sampling", order=1), "b": view_entry("6", "KSampler", feed="sampling", order=2)}
    rows = {d["view_id"]: d for d in describe(prompt_with(views))["views"]}
    assert rows["a"]["status"] == "ok" and rows["b"]["status"] == "duplicate" and not rows["b"]["runnable"]
    # The Sonder App: an app-only View on a node the replaced workflow now gives its own View.
    prompt = prompt_with({"w": view_entry("6", "KSampler", feed="sampling", order=5)})
    view = describe(copy.deepcopy(prompt))
    stored = {"views": {"app1": {"label": "", "order": 0, "source": {"path": "6", "class_type": "KSampler"}, "feed": "sampling"}}}
    cv.apply_sdk_views(view, prompt, stored, class_lookup=CLASSES.get)
    rows = {d["view_id"]: d for d in view["views"]}
    assert rows["w"]["status"] == "ok" and rows["app1"]["status"] == "duplicate"


def test_the_sampling_index_is_uncapped():
    many = {str(1000 + i): node("Foo") for i in range(2100)}
    prompt = prompt_with(**many, **{"5000": node("KSampler")})
    view = describe(prompt)
    assert "5000" in {c["path"] for c in view["view_sampling_candidates"]} and "5000" in view["_view_sampling_index"]
    cleaned, rejected = cd.validate_binding_changes(
        view, {"view:n1:create": {"candidate": {"path": "5000"}, "feed": "sampling"}}, structural="canvas")
    assert not rejected


def test_the_publication_refuses_a_malformed_sampling_list():
    from server import session_registry as sr
    with pytest.raises(ValueError):
        sr._prepare_control_publication({"view_seq": 1, "prompt_hash": "h",
                                         "view": {"bindings": [], "notes": [], "view_sampling_candidates": "6"}})


# ── View order (live-workflow Phase 5) ───────────────────────────────────────

def test_a_new_view_follows_the_last_view_not_the_panel_order():
    prompt = prompt_with({"a": view_entry("9", order=3)})
    view = describe(prompt, authoring={})
    cleaned, rejected = cd.validate_binding_changes(view, {
        "view:n1:create": {"candidate": {"path": "12:7"}},
        "view:n2:create": {"candidate": {"path": "12:15:3"}},
        "binding:b1:expose": {"candidate": {"path": "6", "input": "seed"}},
    }, structural="canvas")
    assert not rejected, rejected
    assert [cleaned["view:n1:create"]["order"], cleaned["view:n2:create"]["order"]] == [4, 5]
    # Bindings keep their own counter: a View create never moves them.
    assert cleaned["binding:b1:expose"]["order"] == 1
    # A later batch (the reported case: Views added one after another).
    later = prompt_with({"a": view_entry("9", order=3), "n1": view_entry("12:7", "SaveVideo", order=4)})
    again, rejected = cd.validate_binding_changes(describe(later, authoring={}),
                                                  {"view:n2:create": {"candidate": {"path": "12:15:3"}}}, structural="canvas")
    assert not rejected and again["view:n2:create"]["order"] == 5


def test_sdk_view_creates_follow_the_apps_visible_views(app):
    st, identity, prompt, validation, snapshot, write = app
    authored = {**prompt, "90": relay({"a": view_entry("9", order=4), "b": view_entry("12:7", "SaveVideo", order=9)})}
    st.replace_template(identity, json.dumps(authored).encode(), validate=validation)
    write({"view:b:remove": True})
    # The app's copy of a workflow View carries its own order: it counts.
    write({"view:a:order": 7})
    data = write({"view:n1:create": {"candidate": {"path": "12:7"}}})
    assert data["views"]["n1"]["order"] == 8  # the removed View's 9 no longer counts
    # A later batch takes the next one (a sampler's preview View here).
    data = write({"view:n2:create": {"candidate": {"path": "6"}, "feed": "sampling"}})
    assert data["views"]["n2"]["order"] == 9
    views = {d["view_id"]: (d["order"], d["origin"]) for d in snapshot()[2]["views"]}
    assert views == {"a": (7, "app_copy"), "n1": (8, "app"), "n2": (9, "app")}
