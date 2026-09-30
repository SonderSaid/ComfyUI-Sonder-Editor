"""Regression tripwire, not an exhaustive or semantic coverage guarantee.

Lexical containment would have accepted a wrapper around a deferred picker
builder (the original plan's F1 defect). It cannot infer writes reached through
already-wrapped methods, or establish that a write remains in the synchronous
prefix of its callback. Behavioral tests exercise those boundaries separately.
Raw mutating fetch sites and queue/versioned helper calls are included here.
"""
import re

import pytest
from pathlib import Path

from test_project_mutation_queue import _run_gesture_node
from test_gallery_paint_first_js import GALLERY_HARNESS, gallery_write_source

ROOT = Path(__file__).resolve().parents[1]

# These are infrastructure/auxiliary/read-only writers, never user boundaries.
# Keep each exception narrow so a newly added handler must be reviewed.
EXEMPT = {
    "_queuePromptProjectWrite": "Uses the caller's snapshot and queue diagnostics.",
    "_applyReferenceHistoryOperations": "All seven callers thread history/recovery diagnostics and owner token.",
    "_mutateReferences": ("Queues caller diagnostics. Its callers are the Library callback and the "
                          "module-facing writers, each inside its own gesture; modules never call it "
                          "(but `rollbackPromptPhysicalAttachment`, a compensation)."),
    "_runSceneMutation": "Serializes already-attributed intent.",
    "_runQueueMutation": "Serializes already-attributed intent.",
    "_handleAssetDropWithinGesture": "Existing threaded asset-drop composite.",
    "_commitReferenceStageWithinGesture": (
        "Shared Reference stage tail; called only from inside the placeReferencePayload "
        "and stageReferenceItemOnLane gestures, which own the diagnostics and the Undo step."),
    "_queueUndoWithinGesture": "Existing FIFO history boundary.",
    "_queueRedoWithinGesture": "Existing FIFO history boundary.",
    "_restoreScene": "Threaded history restore/token requests; never a new gesture.",
    "_getBulkAssetUsages": "Read-shaped POST.",
    "_requestPromptContextCompile": "Read-shaped POST.",
    "_requestPromptPreviewPair": "Read-shaped POST (the streamed paired preview compile).",
    "_sweepRenderCache": "Background cache maintenance, deliberately unscoped.",
    "_maybeHealFrameConstraint": "Background metadata healing, deliberately unscoped.",
    "_maybeHealDimensionConstraint": "Background metadata healing, deliberately unscoped.",
    "_revealProjectFolder": "Opens a folder, not a project-data mutation.",
    "_runGalleryAssetWrite": "Sends the caller's diagnostics; its callers are checked as writers.",
}


def _assert_live_writer_exemptions(source):
    methods = {m[1] for m in re.finditer(
        r"^    (?:async )?(\w+)\(.*?^    \}\n",
        source[source.index("export class EditorWidget {"):], re.M | re.S)}
    assert not EXEMPT.keys() - methods, f"Stale writer exemptions: {sorted(EXEMPT.keys() - methods)}"
    assert all(reason.strip() for reason in EXEMPT.values())

def test_editor_writer_exemptions_are_live():
    _assert_live_writer_exemptions((ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8"))

def test_writer_exemption_ratchet_rejects_a_dead_entry(monkeypatch):
    monkeypatch.setitem(EXEMPT, "_retiredWriter", "Fixture: remove with this probe.")
    with pytest.raises(AssertionError, match="Stale writer exemptions.*_retiredWriter"):
        test_editor_writer_exemptions_are_live()

def test_editor_writer_boundary_inventory():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    methods = {m[1]: m[0] for m in re.finditer(
        r"^    (?:async )?(\w+)\(.*?^    \}\n", source[source.index("export class EditorWidget {"):], re.M | re.S)}
    helper_write = re.compile(r"this\.(?:_runSceneMutation|_runQueueMutation|"
                              r"_queueProjectMutation|_queuePromptProjectWrite|"
                              r"_mutateReferences|_mutateReferencesPaintFirst|"
                              r"_runVersionedProjectMutation|"
                              r"_runGalleryAssetWrite)\(")
    uncovered = []
    for name, body in methods.items():
        raw_write = "fetch(api.apiURL(" in body and re.search(
            r'method:\s*["\'](?:POST|PUT|PATCH|DELETE)["\']', body)
        if not raw_write and not helper_write.search(body):
            continue
        owner = name.removesuffix("WithinGesture")
        wrapped = "this._withMutationGesture(" in body or (
            owner != name and "this._withMutationGesture(" in methods.get(owner, ""))
        if not (wrapped or "this._withTimelineMutationCommit(" in body or name in EXEMPT):
            uncovered.append(name)
    assert not uncovered, f"Review new writer boundaries: {uncovered}"
    for upload in ("importFileIntoProject", "replaceAssetInProject"):
        body = re.search(r"export async function " + upload + r"\(.*?^\}\n",
                         source, re.M | re.S)[0]
        assert "diagnostics = null" in body
        assert "withEditorMutationDiagnostics(" in body


def test_gallery_trash_retry_preserves_host_gesture_with_fresh_request_ids():
    # The gallery's own trash functions (sliced, see test_gallery_paint_first_js)
    # against the real fullscreen host methods: one gesture per trash, a fresh
    # physical request id for the forced retry, and a code-less 409 read as
    # protection that the author confirms.
    _run_gesture_node(gallery_write_source() + GALLERY_HARNESS + """
        const w = makeWidget(), requests = [];
        w._fetchRenderQueue = async () => {};
        let count = 0;
        globalThis.fetch = async (url, init) => {
            requests.push({url,headers:new Headers(init.headers),body:JSON.parse(init.body)});
            return new Response('{}', {status: ++count % 2 ? 409 : 200});
        };
        const options = {
            withMutationGesture: (kind, callback) => w._withMutationGesture(kind, callback),
            onDeleteAsset: (...args) => w._deleteAsset(...args),
            onDeleteFolder: (...args) => w._deleteAssetFolder(...args),
            onBulkDeleteAssets: (...args) => w._bulkDeleteAssets(...args),
        };
        data.assets = [asset('a'), asset('b')];
        assert.equal(await handleAssetDelete(shown('a')), true);
        await handleFolderDelete('folder');
        assert.equal(await handleBulkDelete(['a','b']), true);
        assert.equal(requests.length, 6);
        assert.equal(starts().length, 3);
        assert.equal(confirms, 3);
        for (const offset of [0,2,4]) {
            const first = requests[offset], second = requests[offset+1];
            assert.ok(first.headers.get('X-Sonder-Gesture-Id'));
            assert.equal(first.headers.get('X-Sonder-Gesture-Id'), second.headers.get('X-Sonder-Gesture-Id'));
            assert.notEqual(first.headers.get('X-Sonder-Request-Id'), second.headers.get('X-Sonder-Request-Id'));
            assert.equal(first.body.force, false); assert.equal(second.body.force, true);
        }
        assert.notEqual(requests[0].headers.get('X-Sonder-Gesture-Id'), requests[2].headers.get('X-Sonder-Gesture-Id'));
        // Dormant gallery hosts need no new hook or gesture machinery.
        delete options.withMutationGesture;
        let dormantCalls = 0;
        options.onDeleteAsset = async () => { dormantCalls++; return {status:'trashed'}; };
        data.assets.push(asset('dormant'));
        await handleAssetDelete(shown('dormant'));
        assert.equal(dormantCalls, 1); assert.equal(starts().length, 3);
    """)



# Module-facing Library writers (Library paint-first Phase 4). Every Library
# write a module makes goes through one of these, each its own gesture.
LIBRARY_MODULE_WRITERS = (
    "_writePromptReferenceMember", "_forkReferenceRecipe", "_updateReferenceRecipe",
    "_deleteReferenceRecipe", "_deletePromptContextProfile",
)

_WIDGET_METHOD = re.compile(r"^    (?:async )?(\w+)\(.*?^    \}\n", re.M | re.S)


def _widget_methods():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    return {m[1]: m[0] for m in _WIDGET_METHOD.finditer(
        source[source.index("export class EditorWidget {"):])}


def test_modules_write_the_library_only_through_the_host_writers():
    from test_scene_mutation_registration import _code_mask, _enclosing_scope, _scopes
    call = re.compile(r"\b_?mutateReferences\??\.?\(")
    offenders = []
    for path in sorted((ROOT / "web/js").glob("*.js")):
        if path.name == "editor_widget.js":
            continue
        source = path.read_text(encoding="utf-8")
        mask = _code_mask(source)
        scopes = None
        for match in call.finditer(source):
            if not mask[match.start()]:
                continue
            scopes = scopes or _scopes(source, mask)
            scope = _enclosing_scope(scopes, match.start())
            if (path.name, scope) != ("editor_prompt_panel.js", "rollbackPromptPhysicalAttachment"):
                offenders.append((path.name, scope))
    assert not offenders, f"Write the Library through a host writer: {offenders}"


def test_each_module_facing_library_writer_is_its_own_gesture():
    methods = _widget_methods()
    for name in LIBRARY_MODULE_WRITERS:
        assert "this._withMutationGesture(" in methods.get(name, ""), name


def test_the_fork_reserves_its_lane_step_in_the_gesture_turn():
    """The fork's Undo step is reserved by `_saveLaneConfigWithinGesture` (the
    `laneConfig` unit the authoring contract scans), two levels below the
    gesture, which that scan does not follow; so this pins that the fork calls
    it without awaiting first, and that nothing else reaches it that way."""
    methods = _widget_methods()
    body = methods["_forkReferenceRecipeWithinGesture"]
    assert "await " not in body[:body.index("this._saveLaneConfigWithinGesture(")]
    callers = sorted(name for name, text in methods.items()
                     if "this._saveLaneConfigWithinGesture(" in text)
    assert callers == ["_forkReferenceRecipeWithinGesture", "_saveLaneConfig"]
