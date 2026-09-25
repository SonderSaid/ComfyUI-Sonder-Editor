"""The gallery paints favorite, rename, folder, trash and restore before the host answers.

The gallery shows the host's list plus its own pending patches; a patch leaves
when its write settles, so a failure rolls back to acknowledged data without the
network. These run the gallery's own functions, sliced from its source, against
a host that settles each write when the test says so. The fullscreen host's half
(versioned send, 409 classification, acknowledgement without a list reload) runs
on the real `EditorWidget` methods.
"""
import re
from pathlib import Path

from test_project_mutation_queue import _run_gesture_node

ROOT = Path(__file__).resolve().parents[1]
GALLERY = ROOT / "web" / "js" / "shared_asset_gallery.js"

# Gallery functions the write paths use, beyond the paint-first block itself.
_WRITE_FUNCTIONS = (
    "applyAssetUpdate", "applyAssetUpdateWithinGesture", "handleToggleFavorite",
    "handleAssetDelete", "handleAssetDeleteWithinGesture", "trashAssetsWithinGesture",
    "handleBulkDelete", "handleBulkDeleteWithinGesture",
    "handleAssetRestore", "handleBulkRestore", "restoreAssetsWithGesture",
    "handleOverlayAssetDelete", "handleFolderDelete", "handleFolderDeleteWithinGesture",
    "handleAssetPermanentDelete", "handleEmptyTrash",
)


def gallery_write_source() -> str:
    """The paint-first block and the gesture functions, verbatim from the gallery."""
    source = GALLERY.read_text(encoding="utf-8")
    start = source.index("    // ── Paint-first asset writes")
    tail = re.search(r"    function reportAssetWriteFailure\(.*?^    \}\n", source[start:], re.M | re.S)
    parts = [source[start:start + tail.end()]]
    for name in _WRITE_FUNCTIONS:
        parts.append(re.search(r"    (?:async )?function " + name + r"\(.*?^    \}\n",
                               source, re.M | re.S)[0])
    return "\n".join(parts)


# The gallery's own state and the helpers the slice calls, reduced to what the
# write paths read. `options` is left to each test.
GALLERY_HARNESS = """
        const toasts = [], marks = [], refreshes = [];
        let renders = 0, overlayRenders = 0;
        const state = { destroyed: false, selectedAssetId: '',
            overlayState: { open: false, compareMode: false, assetId: '' } };
        const data = { assets: [], folders: [] };
        let projectDir = 'project';
        const currentProjectDir = () => projectDir, detailProjectId = () => projectDir;
        const markProjectAssetMutation = (id, reason) => { marks.push(reason); };
        const normalizeFolderName = (value) => String(value || '').replace(/^[/]+|[/]+$/g, '');
        const compareStrings = (a, b) => a.localeCompare(b);
        const isTrashed = (asset) => !!asset?.trashed_at;
        const render = () => { renders += 1; };
        const renderInspectOverlay = () => { overlayRenders += 1; };
        const clearUsageView = () => {}, scrollAssetIntoView = () => {};
        let selection = [];
        const applySelectionState = (ids, primary) => { selection = [...ids]; state.selectedAssetId = primary; };
        const successorAssetIdAfterRemoval = (ids) => data.assets.find((entry) =>
            !ids.includes(entry.asset_id) && !isTrashed(entry))?.asset_id || '';
        let preflight = async () => false, confirmAnswer = true, confirms = 0;
        const resolveTrashForceDecision = (assets) => preflight(assets);
        const confirmTrashProtection = () => { confirms += 1; return confirmAnswer; };
        const toast = (tier) => (message, opts = {}) => { toasts.push({ tier, message, ...opts }); };
        const notifySuccess = toast('success'), notifyInfo = toast('info');
        const notifyWarning = toast('warning'), notifyError = toast('error');
        const normalizeSelection = (ids) => ({ ids });
        const selectedAssetIdsList = () => selection;
        const selectedAsset = () => data.assets.find((entry) => entry.asset_id === state.selectedAssetId) || null;
        const resetOverlayTransform = () => {};
        const closeInspectOverlay = () => { state.overlayState.open = false; };
        const folderAssetsRecursive = () => [], removeFolderLocally = () => {}, updateAsset = () => {};
        let confirm = () => true;
        const getBulkUsagePayload = async () => ({ usages: [], usage_count: 0 });
        const summarizeUsageTypes = () => ({});
        const assetDisplayName = (entry) => entry?.name || '';
        const removeAssetsByIds = (ids) => { data.assets = data.assets.filter((entry) => !ids.includes(entry.asset_id)); };
        const trashedAssets = () => data.assets.filter(isTrashed);
        const settleTurns = async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0)); };
        const httpError = (status, message = 'refused') => Object.assign(new Error(message), { status });
        const asset = (id, extra = {}) => ({ asset_id: id, name: id, folder: '', favorite: false,
            trashed_at: '', trash_previous_folder: '', ...extra });
        const shown = (id) => data.assets.find((entry) => entry.asset_id === id);
"""

# A host whose every write waits for the test, in send order.
_PENDING_HOST = """
        const calls = [], pending = [];
        const hostCall = (kind) => (...args) => {
            calls.push({ kind, args });
            return new Promise((resolve, reject) => pending.push({ resolve, reject }));
        };
        const options = {
            onUpdateAsset: hostCall('update'), onDeleteAsset: hostCall('delete'),
            onBulkDeleteAssets: hostCall('bulkDelete'), onRestoreAsset: hostCall('restore'),
            onBulkRestoreAssets: hostCall('bulkRestore'),
            onPermanentDeleteAsset: hostCall('permanent'), onEmptyTrash: hostCall('emptyTrash'),
            onRequestAssetListRefresh: (request) => { refreshes.push(request); },
        };
        const answer = async (value) => { pending.shift().resolve(value); await settleTurns(); };
        const fail = async (error) => { pending.shift().reject(error); await settleTurns(); };
"""


def _run(body: str) -> None:
    _run_gesture_node(gallery_write_source() + GALLERY_HARNESS + _PENDING_HOST + body)


def test_a_favorite_paints_before_the_host_answers():
    _run("""
        data.assets = [asset('a')];
        const done = handleToggleFavorite(shown('a'));
        assert.equal(shown('a').favorite, true);
        assert.equal(renders, 1);
        // No epoch mark at paint: a list landing mid-chain is painted over.
        assert.deepEqual(marks, []);
        await settleTurns();
        assert.deepEqual(calls.map((c) => [c.kind, c.args[1]]), [['update', { favorite: true }]]);
        await answer({ asset_id: 'a', favorite: true });
        assert.equal(await done, true);
        assert.equal(shown('a').favorite, true);
        assert.deepEqual(toasts.map((t) => [t.message, t.source]), [['Added to Favorites', 'gallery-favorite']]);
    """)


def test_three_quick_favorites_write_in_order_and_end_toggled_once():
    _run("""
        data.assets = [asset('a')];
        const done = [handleToggleFavorite(shown('a')), handleToggleFavorite(shown('a')),
            handleToggleFavorite(shown('a'))];
        // Each reads what the gallery shows, so the three alternate.
        assert.equal(shown('a').favorite, true);
        await settleTurns();
        // Serial: the second is not sent until the first has answered.
        assert.equal(calls.length, 1);
        await answer({ favorite: true });
        assert.equal(calls.length, 2);
        await answer({ favorite: false });
        await answer({ favorite: true });
        await Promise.all(done);
        assert.deepEqual(calls.map((c) => c.args[1].favorite), [true, false, true]);
        assert.equal(shown('a').favorite, true);
    """)


def test_a_burst_whose_later_writes_fail_shows_the_last_acknowledged_value():
    _run("""
        data.assets = [asset('a')];
        handleToggleFavorite(shown('a'));      // S1 -> true
        handleToggleFavorite(shown('a'));      // S2 -> false
        handleToggleFavorite(shown('a'));      // S3 -> true
        await settleTurns();
        await answer({ favorite: true });      // S1 accepted
        await fail(httpError(500));            // S2 refused
        assert.equal(shown('a').favorite, true);   // S3's paint still pending
        await fail(httpError(500));            // S3 refused
        // Neither S2's paint nor the pre-burst value: what the server accepted.
        assert.equal(shown('a').favorite, true);
        assert.ok(toasts.some((t) => t.tier === 'error' && t.message === 'Failed to update favorite.'));
    """)


def test_both_failures_roll_back_to_the_value_before_the_burst():
    _run("""
        data.assets = [asset('a')];
        handleToggleFavorite(shown('a'));
        handleToggleFavorite(shown('a'));
        await settleTurns();
        await fail(httpError(500));
        await fail(httpError(500));
        assert.equal(shown('a').favorite, false);
    """)


def test_a_list_refresh_during_the_chain_keeps_pending_paints():
    _run("""
        data.assets = [asset('a')];
        handleToggleFavorite(shown('a'));
        await settleTurns();
        // A take lands and the host delivers a list that predates the write.
        data.assets = [asset('a', { name: 'renamed elsewhere' })];
        overlayPendingAssetPatches();
        assert.equal(shown('a').favorite, true);
        assert.equal(shown('a').name, 'renamed elsewhere');
        // A failure now falls back to the refreshed list, the newest truth.
        await fail(httpError(500));
        assert.equal(shown('a').favorite, false);
        assert.equal(shown('a').name, 'renamed elsewhere');
    """)


def test_set_data_repaints_pending_patches_and_a_project_switch_drops_them():
    source = GALLERY.read_text(encoding="utf-8")
    body = re.search(r"    function setData\(nextData\) \{.*?^    \}\n", source, re.M | re.S)[0]
    assert "overlayPendingAssetPatches();" in body
    assert body.index("data.assets = nextAssets;") < body.index("overlayPendingAssetPatches();")
    assert "assetPatches.clear();" in body and "assetAckedFields.clear();" in body
    # Additivity is judged on what is shown, pending paints included.
    assert body.index("overlayPendingAssetPatches();") < body.index("additiveRefreshAssets(")


def test_rename_and_move_paint_at_once_and_a_blank_name_paints_the_kept_one():
    _run("""
        data.assets = [asset('a', { name: 'Old' })];
        applyAssetUpdate(shown('a'), { name: '  New  ' });
        assert.equal(shown('a').name, 'New');
        applyAssetUpdate(shown('a'), { folder: '/Shots/' });
        assert.equal(shown('a').folder, 'Shots');
        assert.deepEqual(data.folders, ['Shots']);
        applyAssetUpdate(shown('a'), { name: '   ' });
        assert.equal(shown('a').name, 'New');
        await settleTurns();
        await answer({ name: 'New' });
        await answer({ folder: 'Shots' });
        await answer({ name: 'New' });
        assert.equal(calls[1].args[1].folder, 'Shots');
    """)


def test_trash_waits_for_the_protection_decision_then_paints():
    _run("""
        data.assets = [asset('a', { folder: 'Shots' }), asset('b')];
        state.selectedAssetId = 'a';
        let decide;
        preflight = () => new Promise((resolve) => { decide = resolve; });
        const done = handleAssetDelete(shown('a'));
        await settleTurns();
        // Confirmation first: a protected asset never flickers into Trash.
        assert.equal(shown('a').trashed_at, '');
        assert.equal(calls.length, 0);
        decide(true);
        await settleTurns();
        assert.ok(shown('a').trashed_at);
        assert.equal(shown('a').folder, '');
        assert.equal(shown('a').trash_previous_folder, 'Shots');
        assert.equal(state.selectedAssetId, 'b');
        assert.deepEqual(calls[0].args.slice(0, 2), ['a', true]);
        await answer({ status: 'trashed', trashed_at: 'server-time', trash_previous_folder: 'Shots' });
        assert.equal(await done, true);
        assert.equal(shown('a').trashed_at, 'server-time');
        assert.deepEqual(toasts.map((t) => [t.message, t.source]), [['Moved to Trash', 'gallery-trash']]);
    """)


def test_a_late_protection_conflict_brings_the_asset_back_and_asks_again():
    _run("""
        data.assets = [asset('a'), asset('b')];
        state.selectedAssetId = 'a';
        const done = handleAssetDelete(shown('a'));
        await settleTurns();
        assert.ok(shown('a').trashed_at);
        confirmAnswer = true;
        pending.shift().resolve({ status: 'conflict', usage_count: 1 });
        await settleTurns();
        assert.equal(confirms, 1);
        // Painted again after the Yes, and forced.
        assert.ok(shown('a').trashed_at);
        assert.deepEqual(calls.map((c) => c.args[1]), [false, true]);
        await answer({ status: 'trashed' });
        assert.equal(await done, true);
        assert.ok(shown('a').trashed_at);
    """)


def test_a_declined_late_conflict_leaves_the_asset_where_it_was():
    _run("""
        data.assets = [asset('a', { folder: 'Shots' }), asset('b')];
        state.selectedAssetId = 'a';
        const done = handleAssetDelete(shown('a'));
        await settleTurns();
        confirmAnswer = null;
        await answer({ status: 'conflict', usage_count: 1 });
        assert.equal(await done, false);
        assert.equal(shown('a').trashed_at, '');
        assert.equal(shown('a').folder, 'Shots');
        assert.equal(state.selectedAssetId, 'a');
        assert.equal(calls.length, 1);
    """)


def test_the_inspect_view_moves_on_when_the_asset_leaves_the_list():
    _run("""
        data.assets = [asset('a'), asset('b')];
        state.selectedAssetId = 'a';
        state.overlayState = { open: true, compareMode: false, assetId: 'a' };
        const done = handleOverlayAssetDelete(shown('a'));
        await settleTurns();
        // Before the host has answered.
        assert.equal(state.overlayState.assetId, 'b');
        assert.equal(pending.length, 1);
        await answer({ status: 'trashed' });
        assert.equal(await done, true);
    """)


def test_restore_returns_to_its_folder_at_once():
    _run("""
        data.assets = [asset('a', { trashed_at: 't', trash_previous_folder: 'Shots' })];
        const done = handleAssetRestore(shown('a'));
        assert.equal(shown('a').trashed_at, '');
        assert.equal(shown('a').folder, 'Shots');
        assert.deepEqual(data.folders, ['Shots']);
        await settleTurns();
        await answer({ status: 'restored', asset: { asset_id: 'a', folder: 'Shots', trashed_at: '' } });
        assert.equal(await done, true);
        assert.equal(shown('a').folder, 'Shots');
    """)


def test_bulk_trash_and_bulk_restore_paint_every_asset():
    _run("""
        data.assets = [asset('a'), asset('b'), asset('c'), asset('d')];
        state.selectedAssetId = 'a';
        const trashed = handleBulkDelete(['a', 'b', 'c']);
        await settleTurns();
        assert.deepEqual(['a', 'b', 'c'].map((id) => !!shown(id).trashed_at), [true, true, true]);
        assert.equal(state.selectedAssetId, 'd');
        await answer({ status: 'trashed', trashed: ['a', 'b', 'c'] });
        assert.equal(await trashed, true);
        const restored = handleBulkRestore(['a', 'b', 'c']);
        await settleTurns();
        assert.deepEqual(['a', 'b', 'c'].map((id) => !!shown(id).trashed_at), [false, false, false]);
        await answer({ status: 'restored', restored: ['a', 'b', 'c'] });
        assert.equal(await restored, true);
    """)


def test_a_write_without_an_answer_says_so_and_reloads_the_list():
    _run("""
        data.assets = [asset('a')];
        handleToggleFavorite(shown('a'));
        await settleTurns();
        await fail(new TypeError('Failed to fetch'));
        assert.equal(shown('a').favorite, false);
        const warning = toasts.find((t) => t.tier === 'warning');
        assert.equal(warning.message, 'The change could not be confirmed. Reloading the asset list.');
        assert.equal(warning.detail, 'Failed to fetch');
        assert.deepEqual(refreshes, [{ reason: 'gallery_write_unconfirmed' }]);
    """)


def test_a_write_queued_past_a_project_switch_is_never_sent():
    _run("""
        data.assets = [asset('a')];
        handleToggleFavorite(shown('a'));
        const second = handleToggleFavorite(shown('a'));
        await settleTurns();
        projectDir = 'other';
        await answer({ favorite: true });
        assert.equal(await second, false);
        assert.equal(calls.length, 1);
        assert.deepEqual(toasts.filter((t) => t.tier !== 'success').map((t) => t.message),
            ['The project changed before the change was saved.']);
    """)



def test_a_trash_costs_one_rebuild_when_only_the_server_time_differs():
    _run("""
        data.assets = [asset('a'), asset('b')];
        state.selectedAssetId = 'a';
        const done = handleAssetDelete(shown('a'));
        await settleTurns();
        const afterPaint = renders;
        await answer({ status: 'trashed', trashed_at: 'server-time', trash_previous_folder: '' });
        await done;
        assert.equal(renders, afterPaint);
        assert.equal(shown('a').trashed_at, 'server-time');
    """)


def test_writes_that_depend_on_a_failed_trash_are_not_sent():
    _run("""
        data.assets = [asset('a', { folder: 'Shots' }), asset('b')];
        state.selectedAssetId = 'a';
        handleAssetDelete(shown('a'));
        await settleTurns();
        // Painted in Trash, so Restore and Delete Permanently are offered.
        const restored = handleAssetRestore(shown('a'));
        const deleted = handleAssetPermanentDelete(shown('a'));
        await settleTurns();
        await fail(httpError(500));            // the trash is refused
        assert.equal(await restored, false);
        await deleted;
        // Neither reached the host: only the trash was ever sent.
        assert.deepEqual(calls.map((c) => c.kind), ['delete']);
        assert.equal(shown('a').folder, 'Shots');
        assert.equal(shown('a').trashed_at, '');
        const warnings = toasts.filter((t) => t.tier === 'warning').map((t) => t.message);
        assert.ok(warnings.includes('Not restored: the asset was no longer in Trash.'));
        assert.ok(warnings.includes('Nothing was deleted: an asset was no longer in Trash.'));
    """)


def test_empty_trash_refuses_when_trash_changed_after_the_confirm():
    _run("""
        data.assets = [asset('x', { trashed_at: 't', trash_previous_folder: 'Shots' }),
            asset('y', { trashed_at: 't' })];
        handleAssetRestore(shown('x'));        // painted out of Trash
        const emptied = handleEmptyTrash();    // confirms one asset: y
        await settleTurns();
        await fail(httpError(500));            // the restore is refused: x is still in Trash
        await emptied;
        assert.deepEqual(calls.map((c) => c.kind), ['restore']);
        assert.ok(toasts.some((t) => t.message === 'Trash changed before it was emptied, so nothing was deleted.'));
        assert.ok(shown('x').trashed_at);
    """)


def test_a_failed_trash_gives_the_selection_back_only_if_untouched():
    _run("""
        data.assets = [asset('a'), asset('b'), asset('c')];
        state.selectedAssetId = 'a';
        handleAssetDelete(shown('a'));
        await settleTurns();
        assert.equal(state.selectedAssetId, 'b');
        applySelectionState(['c'], 'c');       // the author moves on
        await fail(httpError(500));
        assert.equal(state.selectedAssetId, 'c');
        assert.equal(shown('a').trashed_at, '');
    """)


def test_a_declined_late_conflict_brings_the_inspect_view_back():
    _run("""
        data.assets = [asset('a'), asset('b')];
        state.selectedAssetId = 'a';
        state.overlayState = { open: true, compareMode: false, assetId: 'a' };
        const done = handleOverlayAssetDelete(shown('a'));
        await settleTurns();
        assert.equal(state.overlayState.assetId, 'b');
        confirmAnswer = null;
        await answer({ status: 'conflict', usage_count: 1 });
        assert.equal(await done, false);
        assert.equal(state.overlayState.assetId, 'a');
    """)


def test_a_conflict_with_nothing_to_confirm_says_so():
    _run("""
        data.assets = [asset('a'), asset('b')];
        state.selectedAssetId = 'a';
        const done = handleAssetDelete(shown('a'));
        await settleTurns();
        confirmAnswer = false;                 // no usage, no favorite
        await answer({ status: 'conflict', error: 'stale', usage_count: 0 });
        assert.equal(await done, false);
        assert.equal(shown('a').trashed_at, '');
        const error = toasts.find((t) => t.tier === 'error');
        assert.equal(error.message, 'Failed to move asset to Trash.');
        assert.equal(error.detail, 'stale');
    """)


def test_a_failed_move_to_a_new_folder_leaves_no_empty_folder():
    _run("""
        data.assets = [asset('a')];
        data.folders = ['Old'];
        applyAssetUpdate(shown('a'), { folder: 'Brand New' });
        assert.deepEqual(data.folders, ['Brand New', 'Old']);
        await settleTurns();
        await fail(httpError(500));
        assert.deepEqual(data.folders, ['Old']);
        assert.equal(shown('a').folder, '');
    """)


def test_a_gallery_torn_down_mid_chain_still_sends_what_was_painted():
    _run("""
        data.assets = [asset('a')];
        handleToggleFavorite(shown('a'));
        handleToggleFavorite(shown('a'));
        await settleTurns();
        state.destroyed = true;
        const before = renders;
        await answer({ favorite: true });
        assert.equal(calls.length, 2);
        await answer({ favorite: false });
        assert.equal(renders, before);
    """)

_HOST = """
        const w = makeWidget(), requests = [];
        let libraryRenders = 0, fetchedAssets = 0;
        w._fetchAssets = async () => { fetchedAssets += 1; };
        w._referenceLibraryHandle = { render: () => { libraryRenders += 1; } };
        w.assets = { image: [{ asset_id: 'a', path: 'media/a.png', asset_type: 'image',
            favorite: false, folder: 'Shots', trashed_at: '', trash_previous_folder: '' }] };
        w._pathToAsset = { 'media/a.png': w.assets.image[0] };
        const replies = [];
        globalThis.fetch = async (url, init) => {
            requests.push({ url: String(url), method: init.method,
                body: init.body ? JSON.parse(init.body) : null, headers: new Headers(init.headers) });
            const [status, body] = replies.shift();
            return new Response(JSON.stringify(body), { status });
        };
"""


def test_the_host_acknowledges_without_reloading_the_asset_list():
    _run_gesture_node(_HOST + """
        replies.push([200, { asset_id: 'a', favorite: true, folder: 'Shots' }]);
        const updated = await w._updateAssetMetadata('a', { favorite: true });
        assert.equal(updated.favorite, true);
        assert.deepEqual(requests.map((r) => r.method), ['PUT']);
        assert.equal(fetchedAssets, 0);
        requests.length = 0;
        assert.equal(w.assets.image[0].favorite, true);
        assert.equal(w._pathToAsset['media/a.png'].favorite, true);
        assert.equal(libraryRenders, 1);
        // A rename redraws the timeline: clip labels read the asset's name.
        let timelineRenders = 0;
        w._renderTimeline = () => { timelineRenders += 1; };
        replies.push([200, { asset_id: 'a', name: 'Renamed' }]);
        await w._updateAssetMetadata('a', { name: 'Renamed' });
        assert.equal(w._pathToAsset['media/a.png'].name, 'Renamed');
        assert.equal(timelineRenders, 1);
        replies.push([200, { trashed: true, asset_id: 'a', trashed_at: 'T', trash_previous_folder: 'Shots' }]);
        const trashed = await w._deleteAsset('a', false);
        assert.equal(trashed.status, 'trashed');
        assert.equal(w.assets.image[0].trashed_at, 'T');
        assert.equal(w.assets.image[0].folder, '');
        replies.push([200, { restored: true, asset_id: 'a', folder: 'Shots',
            asset: { asset_id: 'a', folder: 'Shots', trashed_at: '' } }]);
        await w._restoreAsset('a');
        assert.equal(w.assets.image[0].folder, 'Shots');
        assert.equal(w.assets.image[0].trashed_at, '');
        assert.equal(fetchedAssets, 0);
    """)


def test_the_host_classifies_each_asset_write_conflict():
    _run_gesture_node(_HOST + """
        // Protection: a 409 without a code is for the gallery to confirm.
        replies.push([409, { error: 'Asset is used or favorited', usage_count: 2 }]);
        const protectedResult = await w._deleteAsset('a', false);
        assert.equal(protectedResult.status, 'conflict');
        assert.equal(protectedResult.usage_count, 2);
        // A stale version wrote nothing, so it is retried once.
        replies.push([409, { code: 'project_version_conflict', actual_modified_at: 'v9' }]);
        replies.push([200, { asset_id: 'a', favorite: true }]);
        const retried = await w._updateAssetMetadata('a', { favorite: true });
        assert.equal(retried.favorite, true);
        assert.equal(requests.length, 3);
        // A frozen input cannot be forced: a hard refusal, never "conflict".
        replies.push([409, { code: 'queued_reference_input', error: 'Asset is frozen by a pending Reference generation' }]);
        await assert.rejects(w._deleteAsset('a', true), (error) => {
            assert.equal(error.status, 409);
            assert.equal(error.message, 'Asset is frozen by a pending Reference generation');
            return true;
        });
    """)


CONTROLLER = ROOT / "web" / "js" / "editor_node_controller.js"


def _card_method(name: str) -> str:
    source = CONTROLLER.read_text(encoding="utf-8")
    card = source[source.index("class DormantNodeCard {"):]
    return re.search(r"    " + name + r"\(\) \{.*?^    \}\n", card, re.M | re.S)[0]


def test_the_dormant_card_hands_new_asset_data_to_the_mounted_gallery():
    # Every dormant asset write replaces the cached list object; remounting on
    # that identity change destroyed the gallery -- its pending paints and its
    # write chain -- in the middle of its own write.
    _run_gesture_node("""
        let teardowns = 0, mounts = 0, updates = [];
        const card = {
            """ + _card_method("_renderModuleState") + "," + _card_method("_teardownModule") + """,
            _moduleContainerEl: { style: {}, innerHTML: '', appendChild() {} },
            _applyModuleContainerSizing() {}, syncModuleContainerHeight() {},
            shouldAutoResizeNode: () => false,
        };
        const origTeardown = card._teardownModule;
        card._teardownModule = function () { teardowns += 1; return origTeardown.call(this); };
        let accept = true;
        card.controller = {
            state: { expandedModuleId: 'assets' },
            moduleStatus: { assets: { loading: false, error: '' } },
            moduleCache: {},
            modules: { assets: {
                mount: () => { mounts += 1; return () => {}; },
                update: (data) => { updates.push(data); return accept; },
            } },
            queueResize() {}, _loadModule() {},
        };
        const first = { assets: [] }, second = { assets: [] }, third = { assets: [] };
        card.controller.moduleCache.assets = first;
        card._renderModuleState();
        assert.equal(mounts, 1);
        card.controller.moduleCache.assets = second;
        card._renderModuleState();
        assert.equal(mounts, 1);
        assert.equal(teardowns, 1);            // only the initial mount's clear
        assert.deepEqual(updates, [second]);
        assert.equal(card._mountedModuleData, second);
        // A module that cannot take the data in place is remounted as before.
        accept = false;
        card.controller.moduleCache.assets = third;
        card._renderModuleState();
        assert.equal(mounts, 2);
    """)


def test_the_dormant_host_keeps_its_gallery_and_classifies_409s():
    controller_url = CONTROLLER.as_uri()
    _run_gesture_node(f"""
        const {{ EditorNodeController }} = await import({controller_url!r});
        const c = Object.create(EditorNodeController.prototype);
        let given = [];
        c._activeDormantAssetGallery = {{ setData: (data) => given.push(data) }};
        const data = {{ assets: [] }};
        c._dormantAssetGalleryData = data;
        assert.equal(c._updateAssetsModule(data), true);
        assert.deepEqual(given, []);           // already handed over
        const next = {{ assets: [] }};
        assert.equal(c._updateAssetsModule(next), true);
        assert.deepEqual(given, [next]);
        c._activeDormantAssetGallery = null;
        assert.equal(c._updateAssetsModule(next), false);
        // Trash keeps the Assets module's list; the Preview module still drops.
        c.modules = {{ assets: {{ invalidate: () => true }}, preview: {{ invalidate: () => true }} }};
        c.moduleCache = {{ assets: data, preview: {{}} }};
        c.moduleStatus = {{ assets: {{}}, preview: {{}} }};
        c._abortModuleLoad = () => {{}};
        c._invalidateModules(['assets'], {{ keepModuleIds: ['assets'] }});
        assert.equal(c.moduleCache.assets, data);
        assert.equal(c.moduleCache.preview, undefined);
        // Only a code-less 409 is a protection to confirm.
        c.state = {{ projectDir: 'project' }};
        let reply;
        globalThis.fetch = async () => reply;
        reply = new Response(JSON.stringify({{ usage_count: 1 }}), {{ status: 409 }});
        assert.equal((await c._deleteAsset('a')).status, 'conflict');
        reply = new Response(JSON.stringify({{ code: 'queued_reference_input', error: 'Frozen' }}), {{ status: 409 }});
        await assert.rejects(c._deleteAsset('a', true), (error) => error.status === 409 && error.message === 'Frozen');
        reply = new Response(JSON.stringify({{ code: 'project_version_conflict' }}), {{ status: 409 }});
        await assert.rejects(c._bulkDeleteAssets(['a']), (error) => error.status === 409);
    """)
