"""Regression tripwire, not an exhaustive or semantic coverage guarantee.

Lexical containment would have accepted a wrapper around a deferred picker
builder (the original plan's F1 defect). It cannot infer writes reached through
already-wrapped methods, or establish that a write remains in the synchronous
prefix of its callback. Behavioral tests exercise those boundaries separately.
Raw mutating fetch sites and queue/versioned helper calls are included here.
"""
import re
from pathlib import Path

from test_project_mutation_queue import _run_gesture_node

ROOT = Path(__file__).resolve().parents[1]

# These are infrastructure/auxiliary/read-only writers, never user boundaries.
# Keep each exception narrow so a newly added handler must be reviewed.
EXEMPT = {
    "_queuePromptProjectWrite": "Uses the caller's snapshot and queue diagnostics.",
    "_applyReferenceHistoryOperations": "All seven callers thread history/recovery diagnostics and owner token.",
    "_mutateReferences": "Queues caller diagnostics; Library callback is the user boundary.",
    "_runSceneMutation": "Serializes already-attributed intent.",
    "_runQueueMutation": "Serializes already-attributed intent.",
    "_handleAssetDropWithinGesture": "Existing threaded asset-drop composite.",
    "_queueUndoWithinGesture": "Existing FIFO history boundary.",
    "_queueRedoWithinGesture": "Existing FIFO history boundary.",
    "_restoreScene": "Threaded history restore/token requests; never a new gesture.",
    "_getBulkAssetUsages": "Read-shaped POST.",
    "_requestPromptContextCompile": "Read-shaped POST.",
    "_sweepRenderCache": "Background cache maintenance, deliberately unscoped.",
    "_maybeHealFrameConstraint": "Background metadata healing, deliberately unscoped.",
    "_maybeHealDimensionConstraint": "Background metadata healing, deliberately unscoped.",
    "_revealProjectFolder": "Opens a folder, not a project-data mutation.",
}


def test_editor_writer_boundary_inventory():
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    methods = {m[1]: m[0] for m in re.finditer(
        r"^    (?:async )?(\w+)\(.*?^    \}\n", source[source.index("export class EditorWidget {"):], re.M | re.S)}
    helper_write = re.compile(r"this\.(?:_runSceneMutation|_runQueueMutation|"
                              r"_queueProjectMutation|_queuePromptProjectWrite|"
                              r"_mutateReferences|_runVersionedProjectMutation)\(")
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
    source = (ROOT / "web/js/shared_asset_gallery.js").read_text(encoding="utf-8")
    functions = "\n".join(re.search(
        r"    async function " + name + r"\(.*?^    \}\n", source, re.M | re.S)[0]
        for name in ("handleAssetDelete", "handleAssetDeleteWithinGesture",
                     "handleFolderDelete", "handleFolderDeleteWithinGesture",
                     "handleBulkDelete", "handleBulkDeleteWithinGesture"))
    _run_gesture_node(functions + """
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
        const successorAssetIdAfterRemoval = () => '';
        const resolveTrashForceDecision = async () => { await Promise.resolve(); return false; };
        const confirmTrashProtection = () => true, confirm = () => true;
        const normalizeFolderName = value => value || '';
        const updateAsset = () => {}, clearUsageView = () => {}, applySelectionState = () => {};
        const render = () => {}, scrollAssetIntoView = () => {}, notifyInfo = () => {}, notifyError = () => {};
        const folderAssetsRecursive = () => [], isTrashed = () => false;
        const removeFolderLocally = () => {};
        await handleAssetDelete({asset_id:'a'});
        await handleFolderDelete('folder');
        const state = {selectedAssetId:'a'}, data = {assets:[{asset_id:'a'},{asset_id:'b'}]};
        const normalizeSelection = ids => ({ids});
        await handleBulkDelete(['a','b']);
        assert.equal(requests.length, 6);
        assert.equal(starts().length, 3);
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
        await handleAssetDelete({asset_id:'dormant'});
        assert.equal(dormantCalls, 1); assert.equal(starts().length, 3);
    """)
