"""Reference Library writes and the timeline, on the real host paths.

Harness contract: the Reference Lane Setup harness
(``test_reference_panel_item_writes_js.py``) -- a real ``EditorWidget``, the
shipped queue, ``_runVersionedProjectMutation``, ``_fetchScenes`` and
``_setActiveScene`` -- with a route child that also runs Library batches and
asset permanent deletes on the same project it runs scene batches on:

* ``POST /references/mutations`` runs the real
  ``routes._apply_reference_mutation_operations`` and answers with its payload,
  so a delete's staged-item cascade and its per-scene report are the route's;
* ``GET /references`` answers ``routes._references_payload``;
* ``POST /assets/permanent``, ``/assets/bulk-permanent-delete`` and
  ``/assets/empty-trash`` run the real
  ``routes._remove_reference_members_for_assets`` and remove the assets, and
  answer in each handler's shape. The handler's usage check, staging and
  media moves are the asset route's business and are not modelled here.

A test may script the next Library answer as a refusal, a lost answer (the
route commits, the client hears nothing) or an older server's answer (the
route commits and the report is stripped), and may hold a Library write.
"""

import json
import shutil
import subprocess
import sys

import pytest

from server.timeline_state import (
    Asset, LaneConfig, ReferenceEntity, ReferenceItem, ReferenceLaneRecipe,
    ReferenceMember, Scene, TimelineProject,
)
from test_reference_panel_item_writes_js import ROOT, _HARNESS, fixture_project


_ROUTE_CHILD = r"""
import copy, json, sys
sys.path.insert(0, sys.argv[1])
from server import routes
from server.timeline_state import TimelineProject
with open(sys.argv[2], encoding="utf-8") as handle:
    project = TimelineProject.from_dict(json.load(handle))
print("@@ready", flush=True)

def answer(body):
    print("@@" + json.dumps(body, default=str, sort_keys=True), flush=True)

def reload(candidate):
    return TimelineProject.from_dict(json.loads(json.dumps(candidate.to_dict(), default=str)))

for line in sys.stdin:
    request = json.loads(line)
    if request.get("read"):
        answer({"ok": True, "scene": project.get_scene(request["sceneId"]).to_dict()})
        continue
    if request.get("referencesRead"):
        answer({"ok": True, "payload": routes._references_payload(project)})
        continue
    candidate = copy.deepcopy(project)
    try:
        if "references" in request:
            payload = routes._apply_reference_mutation_operations(candidate, request["references"])
        elif "assetDelete" in request:
            kind = request["assetDelete"]
            if kind == "empty-trash":
                asset_ids = [asset.asset_id for asset in candidate.assets if asset.trashed_at]
            else:
                asset_ids = list(request["assetIds"])
            cleanup = routes._remove_reference_members_for_assets(candidate, set(asset_ids))
            for asset_id in asset_ids:
                candidate.remove_asset(asset_id)
            if kind == "single":
                payload = {"deleted": True, "asset_id": asset_ids[0], "usages_orphaned": 0, **cleanup}
            elif kind == "bulk":
                payload = {"deleted": asset_ids, "usages_orphaned": 0, **cleanup}
            else:
                payload = {"deleted": asset_ids, "emptied": len(asset_ids), **cleanup}
        else:
            _committed, payload = routes._apply_scene_mutation_batch(
                candidate, request["sceneId"], request["operations"])
    except routes.ProjectMutationRequestError as exc:
        answer({"ok": False, "status": exc.status, "code": exc.code, "message": exc.message})
        continue
    except Exception as exc:  # the route would answer 500; keep the child alive
        answer({"ok": False, "status": 500, "code": "server_error", "message": repr(exc)})
        continue
    project = reload(candidate)
    answer({"ok": True, "payload": payload})
"""


# Routes the Library and asset endpoints to the child, and everything else to
# the Lane Setup harness's own stand-in.
_LIBRARY = r"""
w._referenceFetchSeq = 0;
w._referenceMutationSeq = 0;
w._referenceOverlays = [];
w._referenceOverlaySeq = 0;
w._fetchAssets = async () => null;
w._fetchRenderQueue = async () => null;
// The Library as the route holds it: a load renumbers member order.
w._references = (await ask({ referencesRead: true })).payload.references;
const libraryScripted = [];
const librarySent = [];
const sceneFetch = globalThis.fetch;
globalThis.fetch = async (url, init = {}) => {
  const path = String(url).split('?')[0];
  const method = String(init.method || 'GET').toUpperCase();
  if (method === 'POST' && /\/references\/mutations$/.test(path)) {
    const operations = JSON.parse(init.body).operations;
    librarySent.push(structuredClone(operations));
    if (holdNext > 0) { holdNext -= 1; await new Promise((resolve, reject) => held.push({resolve, reject})); }
    const script = libraryScripted.shift();
    if (script?.kind === 'refuse') return json(script.status, { error: script.code, code: script.code });
    const reply = await ask({ references: operations });
    if (script?.kind === 'lost') throw new TypeError('Failed to fetch');
    if (!reply.ok) return json(reply.status, { error: reply.message, code: reply.code });
    if (script?.kind === 'old-server') {
      for (const result of reply.payload.results) delete result.cascade;
    }
    return json(200, reply.payload);
  }
  if (method === 'GET' && /\/references$/.test(path)) {
    return json(200, (await ask({ referencesRead: true })).payload);
  }
  const assetDelete = /\/assets\/(permanent|bulk-permanent-delete|empty-trash)$/.exec(path);
  if (method === 'POST' && assetDelete) {
    const body = init.body ? JSON.parse(init.body) : {};
    const kind = { 'permanent': 'single', 'bulk-permanent-delete': 'bulk', 'empty-trash': 'empty-trash' }[assetDelete[1]];
    const reply = await ask({ assetDelete: kind, assetIds: body.asset_ids || (body.asset_id ? [body.asset_id] : []) });
    return reply.ok ? json(200, reply.payload) : json(reply.status, { error: reply.message, code: reply.code });
  }
  return sceneFetch(url, init);
};
const libraryRefuseNext = (code, status = 409) => libraryScripted.push({ kind: 'refuse', code, status });
const libraryLostNext = () => libraryScripted.push({ kind: 'lost' });
const oldServerNext = () => libraryScripted.push({ kind: 'old-server' });
const libraryMember = (id) => structuredClone(w._references.flatMap((ref) => ref.members)
  .find((member) => member.member_id === id));
const removeMember = (id) => w._mutateReferences([{ type: 'delete_member', reference_id: 'entity-1',
  member_id: id, expected: libraryMember(id) }], 'remove Library member').catch((error) => error);
const rows = (scene = w.activeScene) => Object.fromEntries((scene.reference_items || [])
  .map((row) => [row.reference_item_id, row.members.map((member) => member.member_id)]));
const serverRows = async (sceneId = 'scene') => rows((await ask({ read: true, sceneId })).scene);
"""


def run_library(body, tmp_path, project=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Reference Library writes harness")
    project = project or fixture_project()
    project_file = tmp_path / "project.json"
    project_file.write_text(json.dumps(project.to_dict()), encoding="utf-8")
    scenes = [scene.to_dict() for scene in project.scenes]
    assets = {}
    for asset in project.assets:
        assets.setdefault(asset.asset_type, []).append(asset.to_dict())
    fixture = {"scenes": scenes, "sceneIds": [scene["scene_id"] for scene in scenes],
               "assets": assets,
               "references": [reference.to_dict() for reference in project.references]}
    script = (_HARNESS
              .replace("__PYTHON__", json.dumps(sys.executable))
              .replace("__CHILD__", json.dumps(_ROUTE_CHILD))
              .replace("__ROOT__", json.dumps(str(ROOT)))
              .replace("__PROJECT_FILE__", json.dumps(str(project_file)))
              .replace("__WIDGET__", json.dumps((ROOT / "web/js/editor_widget.js").as_uri()))
              .replace("__QUEUE__", json.dumps((ROOT / "web/js/project_mutation_queue.js").as_uri()))
              .replace("__PANEL__", json.dumps((ROOT / "web/js/editor_reference_panel.js").as_uri()))
              .replace("__NOTES__", json.dumps((ROOT / "web/js/editor_notifications.js").as_uri()))
              .replace("__FIXTURE__", json.dumps(fixture, sort_keys=True))
              .replace("__BODY__", _LIBRARY + body))
    completed = subprocess.run([node, "--input-type=module"], input=script,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=120)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _staged_elsewhere_project():
    """The Lane Setup fixture plus a second scene staging member-c alone."""
    project = fixture_project()
    project.scenes.append(Scene(
        scene_id="scene-b", duration_frames=100, reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(lane_id="lane-b", media_kind="image")],
        reference_items=[ReferenceItem(reference_item_id="far", lane_index=0, start_frame=0,
                                       end_frame=20, members=[
                                           {"entity_id": "entity-1", "member_id": "member-c"}])]))
    return project


# The fixture's scene: item-1 stages members a and b, item-2 member c alone,
# item-4 the audio member s.
FIXTURE_ROWS = {"item-1": ["member-a", "member-b"], "item-2": ["member-c"], "item-4": ["member-s"]}


def test_removing_the_only_member_of_a_staged_item_takes_its_bar_off_the_timeline(tmp_path):
    result = run_library("""
    await removeMember('member-c');
    await settle();
    return { local: rows(), server: await serverRows(), gets: gets.length,
      mismatches: diag('reference_cascade_heal').length };
    """, tmp_path)
    expected = {key: value for key, value in FIXTURE_ROWS.items() if key != "item-2"}
    assert result == {"local": expected, "server": expected, "gets": 0, "mismatches": 0}


def test_a_thinned_item_keeps_its_other_member_and_its_next_panel_edit_lands(tmp_path):
    result = run_library("""
    const before = w.activeScene.reference_items.find((row) => row.reference_item_id === 'item-1');
    const beforeMembers = before.members;
    await removeMember('member-a');
    await settle();
    const thinned = { members: rows()['item-1'], sameRow: before === item('item-1'),
      newArray: item('item-1').members !== beforeMembers };
    const outcome = await w._writeReferenceItemFromPanel('item-1', { strength: 0.5 }, 'change reference strength');
    await settle();
    const stored = (await ask({ read: true, sceneId: 'scene' })).scene.reference_items
      .find((row) => row.reference_item_id === 'item-1');
    return { thinned, outcome, stored: stored.strength, storedMembers: stored.members.map((m) => m.member_id),
      gets: gets.length, toasts: toasts.map((t) => t.message) };
    """, tmp_path)
    assert result["thinned"] == {"members": ["member-b"], "sameRow": True, "newArray": True}
    assert result["outcome"] == "ok"
    assert result["stored"] == 0.5 and result["storedMembers"] == ["member-b"]
    assert result["gets"] == 0 and result["toasts"] == []


def test_a_delete_reaches_the_scene_the_author_is_not_looking_at(tmp_path):
    result = run_library("""
    const far = w.scenes.find((scene) => scene.scene_id === 'scene-b');
    await removeMember('member-c');
    await settle();
    return { far: rows(far), farServer: await serverRows('scene-b'), active: rows(), gets: gets.length,
      mismatches: diag('reference_cascade_heal').length };
    """, tmp_path, project=_staged_elsewhere_project())
    assert result["far"] == result["farServer"] == {}
    assert "item-2" not in result["active"]
    assert result["gets"] == 0 and result["mismatches"] == 0


def test_an_older_server_without_the_report_is_healed_by_one_gated_scenes_read(tmp_path):
    result = run_library("""
    oldServerNext();
    const pending = removeMember('member-c');
    const readsWhileQueued = gets.length;
    await pending;
    await settle(12);
    return { readsWhileQueued, gets: gets.length, local: rows(),
      reasons: diag('reference_cascade_heal').map((event) => event.reason ?? event.data?.reason) };
    """, tmp_path)
    assert result["readsWhileQueued"] == 0
    assert result["gets"] == 1
    assert "item-2" not in result["local"]
    assert result["reasons"] == ["no_report"]


def test_a_delete_that_settles_during_a_drag_touches_nothing_until_the_drag_ends(tmp_path):
    result = run_library("""
    w.isDragging = true;
    await removeMember('member-c');
    await settle(12);
    const during = { local: rows(), gets: gets.length };
    w.isDragging = false;
    w._replayDeferredProjectBackedRefresh();
    await settle(12);
    return { during, after: rows(), gets: gets.length,
      reasons: diag('reference_cascade_heal').map((event) => event.reason ?? event.data?.reason) };
    """, tmp_path)
    assert result["during"] == {"local": FIXTURE_ROWS, "gets": 0}
    assert "item-2" not in result["after"] and result["gets"] == 1
    assert result["reasons"] == ["gesture_active"]


def test_a_panel_edit_queued_before_the_delete_and_refused_rolls_back_onto_the_thinned_row(tmp_path):
    """The panel write settles first (the queue is serial) and rolls its field
    back; the delete then thins the row. Nothing needs a read."""
    result = run_library("""
    hold(1);
    refuseNext('identity_mismatch');
    const editing = w._writeReferenceItemFromPanel('item-1', { strength: 0.5 }, 'change reference strength');
    const removing = removeMember('member-a');
    await release();
    await editing; await removing;
    await settle(12);
    const row = item('item-1');
    return { members: rows()['item-1'], strength: row.strength, server: await serverRows(), gets: gets.length };
    """, tmp_path)
    assert result["members"] == ["member-b"] and result["strength"] == 1
    assert result["server"]["item-1"] == ["member-b"]
    assert result["gets"] == 0


def test_a_member_edit_painted_behind_a_delete_that_thins_its_row_converges_on_the_server(tmp_path):
    """The delete runs first and replaces the row's members; the member write
    behind it names the removed member and is refused, and its rollback sees
    that the members are no longer its paint, so it defers to a read."""
    result = run_library("""
    hold(1);
    const removing = removeMember('member-a');
    const moving = w._editReferenceItemMembersFromPanel('item-1',
      { kind: 'move', memberId: 'member-b', direction: -1 }, 'reorder reference members');
    await release();
    await removing; await moving;
    await settle(16);
    return { local: rows(), server: await serverRows() };
    """, tmp_path)
    assert result["local"] == result["server"]
    assert result["server"]["item-1"] == ["member-b"]


def test_a_permanent_asset_delete_of_a_staged_members_asset_reaches_the_timeline(tmp_path):
    result = run_library("""
    const outcome = await w._permanentDeleteAssetWithinGesture(null, 'asset-c', true);
    await settle();
    return { status: outcome.status, local: rows(), server: await serverRows(), gets: gets.length,
      mismatches: diag('reference_cascade_heal').length };
    """, tmp_path)
    expected = {key: value for key, value in FIXTURE_ROWS.items() if key != "item-2"}
    assert result == {"status": "deleted", "local": expected, "server": expected, "gets": 0,
                      "mismatches": 0}


def test_two_objects_for_one_scene_are_both_painted_and_checked_once(tmp_path):
    """After a refused scene switch the active scene can be a second object for
    a scene the list also holds."""
    result = run_library("""
    w.activeScene = structuredClone(w.scenes[0]);
    const listed = w.scenes[0];
    await removeMember('member-c');
    await settle();
    return { active: rows(), listed: rows(listed), gets: gets.length,
      mismatches: diag('reference_cascade_heal').length };
    """, tmp_path)
    expected = {key: value for key, value in FIXTURE_ROWS.items() if key != "item-2"}
    assert result == {"active": expected, "listed": expected, "gets": 0, "mismatches": 0}


def test_a_refused_delete_changes_nothing_on_the_timeline(tmp_path):
    result = run_library("""
    libraryRefuseNext('identity_mismatch');
    const outcome = await removeMember('member-c');
    await settle();
    return { refused: outcome?.payload?.code ?? null, local: rows(), gets: gets.length };
    """, tmp_path)
    assert result == {"refused": "identity_mismatch", "local": FIXTURE_ROWS, "gets": 0}


def test_a_delete_whose_answer_was_lost_is_healed_by_a_scenes_read(tmp_path):
    result = run_library("""
    libraryLostNext();
    await removeMember('member-c');
    await settle(16);
    return { local: rows(), server: await serverRows(), gets: gets.length,
      reasons: diag('reference_cascade_heal').map((event) => event.reason ?? event.data?.reason) };
    """, tmp_path)
    assert result["local"] == result["server"]
    assert "item-2" not in result["server"]
    assert result["gets"] == 1 and result["reasons"] == ["unconfirmed"]


def test_bulk_delete_and_empty_trash_reach_the_timeline_too(tmp_path):
    project = fixture_project()
    project.get_asset("asset-c").trashed_at = "2026-09-30T00:00:00"
    result = run_library("""
    const bulk = await w._bulkPermanentDeleteAssetsWithinGesture(null, ['asset-s'], true);
    await settle();
    const afterBulk = rows();
    const emptied = await w._emptyTrashWithinGesture(null);
    await settle();
    return { statuses: [bulk.status, emptied.status], afterBulk, local: rows(), server: await serverRows(),
      gets: gets.length, heals: diag('reference_cascade_heal').length };
    """, tmp_path, project=project)
    assert result["statuses"] == ["deleted", "deleted"]
    assert result["afterBulk"] == {"item-1": ["member-a", "member-b"], "item-2": ["member-c"]}
    assert result["local"] == result["server"] == {"item-1": ["member-a", "member-b"]}
    assert result["gets"] == 0 and result["heals"] == 0


def test_a_scenes_read_served_before_an_asset_delete_cannot_put_its_bars_back(tmp_path):
    """The asset routes run outside the mutation queue, so the adoption
    invalidates a scenes read already in flight; it replays after instead."""
    result = run_library("""
    const inner = globalThis.fetch;
    let gateOpen = null;
    globalThis.fetch = async (url, init = {}) => {
      const path = String(url).split('?')[0];
      if (String(init.method || 'GET').toUpperCase() === 'GET' && /\\/scenes$/.test(path) && !gateOpen) {
        const response = await inner(url, init);   // served now, before the delete
        await new Promise((resolve) => { gateOpen = resolve; });
        return response;                            // delivered after it
      }
      return inner(url, init);
    };
    const reading = w._fetchScenes({ ignoreMutationGate: true, reason: 'test' });
    await settle(8);
    await w._permanentDeleteAssetWithinGesture(null, 'asset-c', true);
    await settle();
    gateOpen();
    await reading;
    await settle(12);
    return { local: rows(), server: await serverRows() };
    """, tmp_path)
    assert "item-2" not in result["server"]
    assert result["local"] == result["server"]


def test_an_unpainted_member_write_on_a_thinned_row_is_healed_by_a_read(tmp_path):
    """A role patch is sent unpainted, and the route re-canonicalizes the row's
    members when it applies, so the local thinning cannot be vouched for."""
    result = run_library("""
    hold(1);
    const removing = removeMember('member-a');
    const patching = w._editReferenceItemMembersFromPanel('item-1',
      { kind: 'patch', memberId: 'member-b', patch: { role: 'identity' } }, 'role');
    await settle(4);
    await release();
    await removing; await patching;
    await settle(16);
    return { local: rows(), server: await serverRows(), gets: gets.length,
      reasons: diag('reference_cascade_heal').map((event) => event.reason ?? event.data?.reason) };
    """, tmp_path)
    assert result["reasons"] == ["unpainted_member_write"]
    assert result["local"] == result["server"]
    assert result["gets"] == 1


def test_a_reported_scene_the_editor_does_not_hold_is_healed_by_a_read(tmp_path):
    result = run_library("""
    w.scenes = w.scenes.filter((scene) => scene.scene_id !== 'scene-b');
    await removeMember('member-c');
    await settle(12);
    return { gets: gets.length,
      reasons: diag('reference_cascade_heal').map((event) => event.reason ?? event.data?.reason) };
    """, tmp_path, project=_staged_elsewhere_project())
    assert result == {"gets": 1, "reasons": ["scene_not_held"]}


def test_the_item_editor_open_on_a_removed_item_closes(tmp_path):
    result = run_library("""
    const hidden = [];
    w._hideItemEditor = () => hidden.push(w.selectedItem?.id ?? null);
    w._reconcileSelection = EditorWidget.prototype._reconcileSelection;
    w._clearSelection = EditorWidget.prototype._clearSelection;
    const row = item('item-2');
    w.selectedItems = [{ type: 'reference', id: 'item-2', data: row }];
    w.selectedItem = w.selectedItems[0];
    await removeMember('member-c');
    await settle();
    return { hidden: hidden.length > 0, selected: w.selectedItems.map((hit) => hit.id) };
    """, tmp_path)
    assert result == {"hidden": True, "selected": []}


def test_an_ordered_write_queued_behind_the_delete_takes_the_thinned_scene_as_its_undo_baseline(tmp_path):
    """A Library slot names no scene, so without the cascade the next ordered
    write's Undo before-state would be the pre-cascade scene, and undoing an
    unrelated edit would bring the deleted member back."""
    from server.scene_history_merge import merge_scene_history

    result = run_library("""
    hold(1);
    const first = w._writeReferenceItemFromPanel('item-4', { strength: 0.5 }, 'change reference strength');
    const removing = removeMember('member-a');
    const second = w._writeReferenceItemFromPanel('item-2', { strength: 0.25 }, 'change reference strength');
    await release();
    await first; await removing; await second;
    await settle(16);
    const entry = w._undoStack[w._undoStack.length - 1];
    return { before: entry.snapshot, post: entry.postSnapshot, stored: await serverScene() };
    """, tmp_path)
    members = {row["reference_item_id"]: [member["member_id"] for member in row["members"]]
               for row in result["before"]["reference_items"]}
    assert members["item-1"] == ["member-b"]
    merged = merge_scene_history(result["post"], result["before"], result["stored"])
    restored = {row["reference_item_id"]: row for row in merged["reference_items"]}
    assert [member["member_id"] for member in restored["item-1"]["members"]] == ["member-b"]
    assert restored["item-2"]["strength"] == 1
