"""Favoriting inside the Inspect view's compare stage, and the elimination it
drives in the Favorites view.

The slot resolver and the removal planner run as imported. The gallery's overlay
functions are sliced from its source onto the paint-first harness, with the real
scope filter and successor rule, so a favorite goes through the real paint,
write chain, placement and repaint decision; only the DOM-building renderers
and the per-side search are stubs.
"""
import re

from test_gallery_paint_first_js import GALLERY, GALLERY_HARNESS, _PENDING_HOST, gallery_write_source
from test_project_mutation_queue import _run_gesture_node

SCOPE_URL = (GALLERY.parent / "inspect_overlay_scope.js").as_uri()

_OVERLAY_FUNCTIONS = (
    "compareModeActive", "compareStageShown", "compareStageDrawn", "compareStageSignature",
    "resolvedCompareSlots", "compareSideAsset", "overlayFavoriteTarget", "handleOverlayFavoriteKey",
    "refreshCompareInPlace", "compareListSignature", "syncOverlayWithAssets",
    "overlayAssets", "currentOverlayAsset", "sameTypeOverlayAssets",
    "compareQueryRef", "compareFilteredCandidates", "assetMatchesCurrentScope",
    "successorAssetIdAfterRemoval", "overlayPlaceJustReplaced", "handleCompareStarClick",
)


def _gallery_function(source: str, name: str) -> str:
    return re.search(r"^    (?:async )?function " + name + r"\(.*?^    \}\n", source, re.M | re.S)[0]


def _overlay_source() -> str:
    source = GALLERY.read_text(encoding="utf-8")
    guard = re.search(r"^const OVERLAY_REPLACED_CLICK_GUARD_MS = \d+;$", source, re.M)[0]
    return "\n".join([guard] + [_gallery_function(source, name) for name in _OVERLAY_FUNCTIONS])


# The overlay's inputs: the gallery scope (All, or Favorites, through the real
# scope filter) and an open compare stage whose list and star hooks count their
# repaints. The render stub draws what the real one would: it closes without an
# anchor, and otherwise writes the resolved slots back and stamps the signature
# of the view it drew, so a sequence of rebuilds runs on states the real overlay
# can reach. A side's search matches names containing it; `tracked:` stays
# "not ready" until `metadataReady` is set.
_OVERLAY_HARNESS = """
        const { planCompareRemoval, resolveCompareSlots, resolveInspectOverlayScope } = await import('__SCOPE_URL__');
        Object.assign(state, { scopeMode: 'all', overlaySession: 1 });
        state.overlayState.replacedAt = { A: 0, B: 0, single: 0 };
        let favoritesView = false;
        const assetInCurrentScene = () => false;
        const activeNavigableAssets = () => {
            state.scopeMode = favoritesView ? 'favorites' : 'all';
            return data.assets.filter((entry) => !isTrashed(entry) && assetMatchesCurrentScope(entry));
        };
        const sortAssets = (assets) => [...assets];
        const DEFAULT_SORT_MODE = 'newest';
        const sortAssetsByMode = (assets) => assets;
        const parseAssetSearchQuery = (query) => String(query || '');
        const queryHasMetadataTerms = (query) => query.startsWith('tracked:');
        let metadataReady = false;
        const prepareMetadataSearch = (query) => !queryHasMetadataTerms(query) || metadataReady ? 'ready' : 'loading';
        const assetMatchesParsedQuery = (entry, query) => !query || queryHasMetadataTerms(query)
            || entry.name.includes(query);
        const comparePickerHasMetadataQuery = () => false;
        const closeInspectOverlay = () => {
            Object.assign(state.overlayState, { open: false, assetId: '', compareMode: false,
                compareLeftAssetId: '', compareRightAssetId: '', mediaSignature: '' });
        };
        const renderInspectOverlay = () => {
            overlayRenders += 1;
            const overlay = state.overlayState;
            if (!overlay.open || !currentOverlayAsset()) {
                closeInspectOverlay();
                return;
            }
            if (compareStageShown()) {
                const resolved = resolvedCompareSlots();
                overlay.compareLeftAssetId = resolved.leftId;
                overlay.compareRightAssetId = resolved.rightId;
                overlay.mediaSignature = compareStageSignature(currentOverlayAsset().asset_type,
                    resolved.leftId, resolved.rightId);
                state.overlayCompareListSignature = compareListSignature();
            } else {
                overlay.mediaSignature = `s:${overlay.assetId}`;
            }
        };
        let choosersRefreshes = 0, starRefreshes = 0;
        const openCompare = (anchor, left, right, extra = {}) => {
            Object.assign(state.overlayState, { open: true, origin: 'gallery', compareMode: true,
                assetId: anchor, compareLeftAssetId: left, compareRightAssetId: right,
                comparePickerQuery: '', comparePickerQueryB: '',
                compareCycleSide: 'B', mediaSignature: `c:video:${left}:${right}`, ...extra });
            state.overlayCompareChoosersRefresh = () => { choosersRefreshes += 1; };
            state.overlayCompareFavoriteRefresh = () => { starRefreshes += 1; };
            state.overlayCompareListSignature = compareListSignature();
        };
        const openSingle = (assetId) => {
            Object.assign(state.overlayState, { open: true, origin: 'gallery', compareMode: false,
                assetId, compareLeftAssetId: assetId, compareRightAssetId: '', mediaSignature: `s:${assetId}` });
        };
        const video = (id, extra = {}) => asset(id, { asset_type: 'video', path: `${id}.mp4`, ...extra });
        const image = (id, extra = {}) => asset(id, { asset_type: 'image', path: `${id}.png`, ...extra });
        const fav = (make, ...ids) => ids.map((id) => make(id, { favorite: true }));
        const drawn = () => {
            const o = state.overlayState;
            return o.open ? [o.mediaSignature, o.assetId] : ['closed', ''];
        };
""".replace("__SCOPE_URL__", SCOPE_URL)


# The paint-first harness's render stub, single-view sync, close stub and
# successor stub give way to the drawing stubs above and the real functions.
_HARNESS = re.sub(r"        const renderInspectOverlay = .*?(?=        const clearUsageView)", "",
                  GALLERY_HARNESS, count=1, flags=re.S)
_HARNESS = re.sub(r"        const successorAssetIdAfterRemoval = .*?\n.*?\n", "", _HARNESS, count=1)
_HARNESS = re.sub(r"        const activeNavigableAssets = .*?\n", "", _HARNESS, count=1)
_HARNESS = _HARNESS.replace(
    "        const closeInspectOverlay = () => { state.overlayState.open = false; };\n", "")
for _stubbed in ("syncOverlayWithAssets", "renderInspectOverlay", "successorAssetIdAfterRemoval",
                 "closeInspectOverlay", "activeNavigableAssets"):
    assert _stubbed not in _HARNESS, _stubbed


def _run(body: str) -> None:
    _run_gesture_node(gallery_write_source() + _overlay_source()
                      + _HARNESS + _PENDING_HOST + _OVERLAY_HARNESS + body)


def test_the_slot_resolver_keeps_valid_slots_and_never_repeats_a():
    _run("""
        const r = (ids, anchor, left, right) => {
            const { leftId, rightId } = resolveCompareSlots(ids, anchor, left, right);
            return [leftId, rightId];
        };
        const ids = ['a', 'b', 'c'];
        assert.deepEqual(r(ids, 'a', 'b', 'c'), ['b', 'c']);
        // A slot whose asset left takes its stand-in: A the anchor, B the first other.
        assert.deepEqual(r(ids, 'a', 'gone', 'c'), ['a', 'c']);
        assert.deepEqual(r(ids, 'a', 'c', 'gone'), ['c', 'a']);
        assert.deepEqual(r(ids, 'a', '', ''), ['a', 'b']);
        // B equal to A moves on, including when a stale A falls back onto B's asset.
        assert.deepEqual(r(ids, 'a', 'b', 'b'), ['b', 'a']);
        assert.deepEqual(r(['a', 'b'], 'a', 'gone', 'a'), ['a', 'b']);
        // Only a single candidate shows the same asset twice.
        assert.deepEqual(r(['a'], 'a', 'a', ''), ['a', 'a']);
        // An anchor that is not a candidate cannot be shown.
        assert.deepEqual(r(['b', 'c'], 'a', '', ''), ['b', 'c']);
        assert.deepEqual(r([], 'a', 'a', 'b'), ['', '']);
    """)


def test_s_on_the_compare_stage_favorites_the_cycle_side():
    _run("""
        data.assets = [video('v1'), video('v2'), video('v3')];
        openCompare('v1', 'v1', 'v2');
        assert.equal(compareStageShown(), true);
        assert.equal(overlayFavoriteTarget().asset_id, 'v2');
        state.overlayState.compareCycleSide = 'A';
        assert.equal(overlayFavoriteTarget().asset_id, 'v1');
        assert.equal(handleOverlayFavoriteKey({ repeat: false }), true);
        assert.equal(shown('v1').favorite, true);
        assert.equal(shown('v2').favorite, false);
        await settleTurns();
        assert.deepEqual(calls.map((c) => [c.args[0], c.args[1]]), [['v1', { favorite: true }]]);
    """)


def test_a_held_s_toggles_once():
    _run("""
        data.assets = [video('v1'), video('v2')];
        openCompare('v1', 'v1', 'v2');
        handleOverlayFavoriteKey({ repeat: false });
        for (let i = 0; i < 4; i += 1) assert.equal(handleOverlayFavoriteKey({ repeat: true }), true);
        assert.equal(shown('v2').favorite, true);
        await settleTurns();
        assert.equal(calls.length, 1);
    """)


def test_compare_left_with_one_candidate_favorites_the_single_asset():
    _run("""
        // Compare stays on while the overlay draws the single view.
        data.assets = [video('v1'), asset('i1', { asset_type: 'image', path: 'i1.png' })];
        openCompare('v1', 'v1', 'v1', { mediaSignature: 's:v1' });
        assert.equal(compareModeActive(), true);
        assert.equal(compareStageShown(), false);
        assert.equal(overlayFavoriteTarget().asset_id, 'v1');
    """)


def test_a_favorite_that_keeps_the_pair_repaints_lists_and_stars_only():
    _run("""
        data.assets = [video('v1'), video('v2'), video('v3')];
        openCompare('v1', 'v1', 'v2');
        const done = handleOverlayFavoriteKey({ repeat: false });
        // At paint: the gallery rows, then the compare lists and stars once
        // each, and no stage rebuild that would reload either side's media.
        assert.equal(renders, 1);
        assert.equal(overlayRenders, 0);
        assert.deepEqual([choosersRefreshes, starRefreshes], [1, 1]);
        await settleTurns();
        await answer({ asset_id: 'v2', favorite: true });
        // The server confirmed what was painted: nothing repaints again.
        assert.equal(overlayRenders, 0);
        assert.deepEqual([choosersRefreshes, starRefreshes], [1, 1]);
    """)


def test_a_failed_favorite_repaints_the_stars_back_in_place():
    _run("""
        data.assets = [video('v1'), video('v2')];
        openCompare('v1', 'v1', 'v2');
        handleOverlayFavoriteKey({ repeat: false });
        await settleTurns();
        await fail(httpError(500));
        assert.equal(shown('v2').favorite, false);
        assert.equal(overlayRenders, 0);
        assert.deepEqual([choosersRefreshes, starRefreshes], [2, 2]);
    """)


def test_a_favorite_that_changes_the_pair_rebuilds_the_stage():
    _run("""
        // In the Favorites view, unfavoriting B takes it out of the candidates.
        favoritesView = true;
        data.assets = [video('v1', { favorite: true }), video('v2', { favorite: true }),
            video('v3', { favorite: true })];
        openCompare('v1', 'v1', 'v2');
        handleOverlayFavoriteKey({ repeat: false });
        assert.equal(shown('v2').favorite, false);
        assert.equal(overlayRenders, 1);
        assert.equal(starRefreshes, 0);
        // The rebuild names what it shows: B took the remaining candidate.
        assert.deepEqual([state.overlayState.compareLeftAssetId, state.overlayState.compareRightAssetId],
            ['v1', 'v3']);
        assert.equal(overlayFavoriteTarget().asset_id, 'v3');
    """)


def test_s_follows_the_drawn_view_through_a_failed_removal():
    _run("""
        // Two favorites: unfavoriting B leaves one, so compare draws single v1.
        favoritesView = true;
        data.assets = [video('v1', { favorite: true }), video('v2', { favorite: true })];
        openCompare('v1', 'v1', 'v2');
        handleOverlayFavoriteKey({ repeat: false });
        assert.equal(state.overlayState.mediaSignature, 's:v1');
        // Drawn single: S means the asset on screen, never the departed B.
        assert.equal(overlayFavoriteTarget().asset_id, 'v1');
        await settleTurns();
        await fail(httpError(500));
        // The rollback brings v2 back, and the stage it can draw again is drawn.
        assert.equal(shown('v2').favorite, true);
        assert.equal(state.overlayState.mediaSignature, 'c:video:v1:v2');
        assert.equal(overlayFavoriteTarget().asset_id, 'v2');
    """)


def test_a_new_list_that_drops_the_stage_below_two_draws_the_single_view():
    _run("""
        favoritesView = true;
        data.assets = [video('v1', { favorite: true }), video('v2', { favorite: true })];
        openCompare('v1', 'v1', 'v2');
        // Another window unfavorited v2.
        data.assets = [video('v1', { favorite: true }), video('v2')];
        syncOverlayWithAssets([], { listArrival: true });
        assert.equal(overlayRenders, 1);
        assert.equal(state.overlayState.mediaSignature, 's:v1');
        assert.equal(overlayFavoriteTarget().asset_id, 'v1');
        // A list whose anchor is gone leaves the view as it is.
        data.assets = [video('v2')];
        syncOverlayWithAssets([], { listArrival: true });
        assert.equal(overlayRenders, 1);
        assert.equal(state.overlayState.open, true);
    """)


def test_s_on_a_stage_kept_after_its_anchor_left_still_means_the_drawn_side():
    _run("""
        data.assets = [video('v1'), video('v2'), video('v3')];
        openCompare('v1', 'v1', 'v2');
        // A list without the anchor leaves the stage drawn, so live candidates
        // and the screen disagree: S follows the screen.
        data.assets = [video('v2'), video('v3')];
        syncOverlayWithAssets([], { listArrival: true });
        assert.equal(state.overlayState.mediaSignature, 'c:video:v1:v2');
        assert.equal(compareStageShown(), false);
        assert.equal(overlayFavoriteTarget()?.asset_id, 'v2');
    """)


def test_a_new_list_repaints_the_compare_lists_only_when_what_they_show_changed():
    _run("""
        data.assets = [video('v1'), video('v2'), video('v3')];
        openCompare('v1', 'v1', 'v2');
        data.assets = data.assets.map((entry) => ({ ...entry }));
        syncOverlayWithAssets([], { listArrival: true });
        assert.deepEqual([overlayRenders, choosersRefreshes, starRefreshes], [0, 0, 0]);
        data.assets = [video('v1'), video('v2'), video('v3', { name: 'renamed' })];
        syncOverlayWithAssets([], { listArrival: true });
        assert.deepEqual([overlayRenders, choosersRefreshes, starRefreshes], [0, 1, 1]);
        data.assets = [video('v1'), video('v2', { favorite: true }), video('v3', { name: 'renamed' })];
        syncOverlayWithAssets([], { listArrival: true });
        assert.deepEqual([overlayRenders, choosersRefreshes, starRefreshes], [0, 2, 2]);
    """)


def test_the_single_view_star_still_rebuilds_its_own_asset_only():
    _run("""
        data.assets = [video('v1'), video('v2')];
        Object.assign(state.overlayState, { open: true, origin: 'gallery', compareMode: false,
            assetId: 'v1', mediaSignature: 's:v1' });
        handleToggleFavorite(shown('v2'));
        assert.equal(overlayRenders, 0);
        handleOverlayFavoriteKey({ repeat: false });
        assert.equal(shown('v1').favorite, true);
        assert.equal(overlayRenders, 1);
    """)


def test_the_render_writes_the_resolved_slots_back_before_the_media_signature():
    # renderInspectOverlay builds DOM, so its write-back is pinned by order:
    # the slots are resolved and stored before the signature reads them.
    source = GALLERY.read_text(encoding="utf-8")
    body = _gallery_function(source, "renderInspectOverlay")
    write_back = body.index("overlay.compareLeftAssetId = resolved.leftId;")
    assert body.index("const resolved = resolvedCompareSlots();") < write_back
    assert write_back < body.index("const signature = compareStageSignature(asset.asset_type,")
    assert "overlay.compareRightAssetId = resolved.rightId;" in body
    # S goes through the named, tested handler in every overlay mode.
    assert 'if (event.key === "s" || event.key === "S") return handleOverlayFavoriteKey(event);' in body


# ── Elimination in the Favorites view ──────────────────────────────────────


def test_the_removal_planner_steps_in_the_next_contender_per_side():
    _run("""
        const plan = (args) => planCompareRemoval({ sideLists: {}, ...args });
        const all = ['v1', 'v2', 'v3', 'v4'];
        // B leaves: its next contender, as the down arrow would.
        assert.deepEqual(plan({ sameTypeIds: all, leftId: 'v1', rightId: 'v2', removedId: 'v2',
            sideLists: { A: all, B: all } }), { compare: true, leftId: 'v1', rightId: 'v3' });
        // Wrapping at the end skips the other side's asset.
        assert.deepEqual(plan({ sameTypeIds: all, leftId: 'v1', rightId: 'v4', removedId: 'v4',
            sideLists: { A: all, B: all } }), { compare: true, leftId: 'v1', rightId: 'v2' });
        // A leaves: A resolves first, never onto B.
        assert.deepEqual(plan({ sameTypeIds: all, leftId: 'v1', rightId: 'v2', removedId: 'v1',
            sideLists: { A: all, B: all } }), { compare: true, leftId: 'v3', rightId: 'v2' });
        // Each side walks its own filtered list.
        assert.deepEqual(plan({ sameTypeIds: all, leftId: 'v1', rightId: 'v2', removedId: 'v2',
            sideLists: { A: all, B: ['v2', 'v4'] } }), { compare: true, leftId: 'v1', rightId: 'v4' });
        // A side list that is not ready, empty, lacks the asset or has nothing
        // else falls back to every same-type candidate.
        for (const B of [null, [], ['v3', 'v4'], ['v2', 'v1']]) {
            assert.deepEqual(plan({ sameTypeIds: all, leftId: 'v1', rightId: 'v2', removedId: 'v2',
                sideLists: { A: all, B } }), { compare: true, leftId: 'v1', rightId: 'v3' }, JSON.stringify(B));
        }
        // A == B: both sides move, and not onto each other.
        assert.deepEqual(plan({ sameTypeIds: all, leftId: 'v2', rightId: 'v2', removedId: 'v2' }),
            { compare: true, leftId: 'v3', rightId: 'v4' });
        // Below two candidates, compare ends on the survivor.
        assert.deepEqual(plan({ sameTypeIds: ['v1', 'v2'], leftId: 'v1', rightId: 'v2', removedId: 'v2' }),
            { compare: false, survivorId: 'v1' });
        assert.deepEqual(plan({ sameTypeIds: ['v2'], leftId: 'v2', rightId: 'v2', removedId: 'v2' }),
            { compare: false, survivorId: '' });
    """)


def test_elimination_steps_the_next_contender_into_the_side_that_lost():
    _run("""
        favoritesView = true;
        data.assets = [...fav(video, 'v1', 'v2', 'v3', 'v4'), ...fav(image, 'i1')];
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        assert.deepEqual(drawn(), ['c:video:v1:v3', 'v1']);
        assert.equal(overlayRenders, 1);
        handleToggleFavorite(shown('v3'));
        assert.deepEqual(drawn(), ['c:video:v1:v4', 'v1']);
        // Unfavoriting A, the overlay's own asset: A takes its next contender
        // and the overlay follows it, instead of closing.
        handleToggleFavorite(shown('v1'));
        // One video left: single Inspect on the true favorite.
        assert.deepEqual(drawn(), ['s:v4', 'v4']);
        assert.equal(state.overlayState.compareMode, false);
        // The last video goes: the next favorite of any type, then closed.
        handleToggleFavorite(shown('v4'));
        assert.deepEqual(drawn(), ['s:i1', 'i1']);
        handleToggleFavorite(shown('i1'));
        assert.deepEqual(drawn(), ['closed', '']);
        await settleTurns();
        for (let i = 0; i < 5; i += 1) await answer({ favorite: false });
        assert.deepEqual(calls.map((c) => c.args[0]), ['v2', 'v3', 'v1', 'v4', 'i1']);
    """)


def test_unfavoriting_a_moves_a_to_its_next_contender_and_keeps_b():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2', { compareCycleSide: 'A' });
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual([state.overlayState.compareLeftAssetId, state.overlayState.compareRightAssetId],
            ['v3', 'v2']);
        assert.equal(state.overlayState.assetId, 'v3');
    """)


def test_a_side_filtered_by_search_walks_its_own_list_and_waits_for_metadata():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'take2', 'v3', 'take4');
        openCompare('v1', 'v1', 'take2', { comparePickerQueryB: 'take' });
        handleToggleFavorite(shown('take2'));
        assert.equal(state.overlayState.compareRightAssetId, 'take4');
        // A metadata search still loading: every same-type candidate stands in.
        data.assets = fav(video, 'v1', 'take2', 'v3', 'take4');
        openCompare('v1', 'v1', 'take2', { comparePickerQueryB: 'tracked:seed' });
        handleToggleFavorite(shown('take2'));
        assert.equal(state.overlayState.compareRightAssetId, 'v3');
    """)


def test_unfavoriting_in_the_all_view_moves_nothing():
    _run("""
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        assert.deepEqual(drawn(), ['c:video:v1:v2', 'v1']);
        assert.equal(overlayRenders, 0);
        assert.equal(state.overlayState.replacedAt.B, 0);
    """)


def test_single_inspect_moves_on_like_trash_instead_of_closing():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openSingle('v2');
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual(drawn(), ['s:v3', 'v3']);
        // At the end of the list: the previous one.
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual(drawn(), ['s:v1', 'v1']);
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual(drawn(), ['closed', '']);
    """)


def test_a_failed_removal_puts_back_only_what_still_shows_its_placement():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3', 'v4');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        assert.equal(state.overlayState.compareRightAssetId, 'v3');
        await settleTurns();
        await fail(httpError(500));
        // B still showed the contender the removal put there: v2 is back on B.
        assert.deepEqual(drawn(), ['c:video:v1:v2', 'v1']);

        // The author moved B on before the failure: B stays where they put it.
        handleToggleFavorite(shown('v2'));
        state.overlayState.compareRightAssetId = 'v4';
        renderInspectOverlay();
        await settleTurns();
        await fail(httpError(500));
        assert.deepEqual(drawn(), ['c:video:v1:v4', 'v1']);
    """)


def test_a_failed_drop_to_single_returns_to_the_old_pair():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v1'));
        assert.deepEqual(drawn(), ['s:v2', 'v2']);
        await settleTurns();
        await fail(httpError(500));
        assert.deepEqual(drawn(), ['c:video:v1:v2', 'v1']);
        assert.equal(state.overlayState.compareMode, true);
    """)


def test_a_failed_single_view_removal_comes_back_unless_the_author_moved():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openSingle('v2');
        handleOverlayFavoriteKey({ repeat: false });
        await settleTurns();
        await fail(httpError(500));
        assert.deepEqual(drawn(), ['s:v2', 'v2']);
        handleOverlayFavoriteKey({ repeat: false });
        openSingle('v1');
        await settleTurns();
        await fail(httpError(500));
        assert.deepEqual(drawn(), ['s:v1', 'v1']);
    """)


def test_a_failed_removal_still_hidden_by_newer_state_puts_nothing_back():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        // A list from another window says v2 is no longer a favorite.
        data.assets = [video('v1', { favorite: true }), video('v2'), video('v3', { favorite: true })];
        overlayPendingAssetPatches();
        await settleTurns();
        await fail(httpError(500));
        assert.equal(shown('v2').favorite, false);
        assert.deepEqual(drawn(), ['c:video:v1:v3', 'v1']);
    """)


def test_a_failed_removal_puts_nothing_back_into_a_later_inspect_session():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        closeInspectOverlay();
        state.overlaySession += 1;
        openCompare('v1', 'v1', 'v3');
        await settleTurns();
        await fail(httpError(500));
        assert.deepEqual([state.overlayState.compareLeftAssetId, state.overlayState.compareRightAssetId],
            ['v1', 'v3']);
    """)


def test_a_star_ignores_the_second_click_of_a_double_click_on_a_newcomer():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3', 'v4');
        openCompare('v1', 'v1', 'v2');
        const firstClick = performance.now();
        assert.equal(handleCompareStarClick('B', { timeStamp: firstClick }), true);
        assert.equal(state.overlayState.compareRightAssetId, 'v3');
        // The second click, even one queued behind the rebuild, finds the newcomer.
        assert.equal(handleCompareStarClick('B', { timeStamp: firstClick + 150 }), false);
        assert.equal(handleCompareStarClick('B', { timeStamp: firstClick - 5 }), false);
        assert.equal(shown('v3').favorite, true);
        // The other side, and a click after the guard, still act.
        assert.equal(overlayPlaceJustReplaced('A', firstClick + 150), false);
        assert.equal(handleCompareStarClick('B', { timeStamp: performance.now() + 600 }), true);
        assert.equal(shown('v3').favorite, false);
    """)


def test_single_inspect_moving_on_guards_its_toolbar_star():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openSingle('v1');
        const firstClick = performance.now();
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual(drawn(), ['s:v2', 'v2']);
        assert.equal(overlayPlaceJustReplaced('single', firstClick + 150), true);
        assert.equal(overlayPlaceJustReplaced('single', performance.now() + 600), false);
    """)
    source = GALLERY.read_text(encoding="utf-8")
    button = _gallery_function(source, "makeFavoriteButton")
    guard = 'if (overlay && overlayPlaceJustReplaced("single", event.timeStamp)) return;'
    assert button.index(guard) < button.index("await handleToggleFavorite(asset);")
    render = _gallery_function(source, "renderInspectOverlay")
    assert "makeFavoriteButton(asset, { overlay: true })" in render


def test_the_compare_star_goes_through_the_guarded_handler():
    source = GALLERY.read_text(encoding="utf-8")
    chip = _gallery_function(source, "makeCompareSideChip")
    assert "if (!star.disabled) handleCompareStarClick(side, event);" in chip
    assert "handleToggleFavorite" not in chip


def test_each_inspect_opening_is_a_new_session_with_no_guard_carried_over():
    source = GALLERY.read_text(encoding="utf-8")
    body = _gallery_function(source, "openInspectOverlay")
    opened = body.index("state.overlayState.open = true;")
    assert opened < body.index("state.overlaySession += 1;")
    assert opened < body.index("state.overlayState.replacedAt = { A: 0, B: 0, single: 0 };")


def test_a_stage_kept_after_its_anchor_left_still_eliminates_within_the_type():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3', 'v4');
        openCompare('v1', 'v1', 'v2');
        // Another window unfavorited the anchor; the stage stays as drawn.
        data.assets = [video('v1'), ...fav(video, 'v2', 'v3', 'v4')];
        syncOverlayWithAssets([], { listArrival: true });
        handleToggleFavorite(shown('v2'));
        // The departed anchor's A was stale too: both sides take contenders,
        // and the overlay follows A instead of closing.
        assert.deepEqual(drawn(), ['c:video:v3:v4', 'v3']);
    """)


def test_a_throwing_placement_still_sends_the_write():
    _run("""
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        console.warn = () => {};
        const done = applyAssetUpdate(shown('v3'), { favorite: false }, { hooks: {
            onPainted: () => { throw new Error('boom'); },
            onUnpainted: () => { throw new Error('boom'); },
        } });
        await settleTurns();
        assert.equal(calls.length, 1);
        await fail(httpError(500));
        assert.equal(await done, false);
        assert.equal(shown('v3').favorite, true);
        assert.ok(toasts.some((t) => t.tier === 'error'));
    """)


# ── Undo on the removal notice ─────────────────────────────────────────────


_REMOVAL = """
        const removals = () => toasts.filter((t) => t.source?.startsWith('gallery-favorite-removal:'));
        const undo = (notice) => notice.actions.find((a) => a.label === 'Undo').fn();
"""


def test_a_removal_raises_one_undo_notice_at_the_paint_and_no_success_toast():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3', 'v4');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        // Raised before the host answers, naming the asset.
        assert.equal(calls.length, 0);
        assert.equal(removals().length, 1);
        assert.equal(removals()[0].verb, 'Removed from Favorites');
        assert.equal(removals()[0].message, 'v2');
        assert.deepEqual(removals()[0].actions.map((a) => a.label), ['Undo']);
        await settleTurns();
        await answer({ favorite: false });
        assert.equal(toasts.some((t) => t.message === 'Removed from Favorites'), false);
        // The next removal replaces the notice rather than stacking or counting.
        handleToggleFavorite(shown('v3'));
        assert.equal(removals().length, 2);
        assert.equal(removals()[0].dismissed, true);
        assert.equal(removals()[1].dismissed, false);
        assert.notEqual(removals()[0].source, removals()[1].source);
    """)


def test_an_unfavorite_that_stays_in_view_keeps_the_ordinary_toast():
    _run(_REMOVAL + """
        data.assets = fav(video, 'v1', 'v2');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        await settleTurns();
        await answer({ favorite: false });
        assert.equal(removals().length, 0);
        assert.ok(toasts.some((t) => t.message === 'Removed from Favorites'));
    """)


def test_undo_favorites_again_and_puts_the_asset_back_on_its_side():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3', 'v4');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        await settleTurns();
        await answer({ favorite: false });
        assert.deepEqual(drawn(), ['c:video:v1:v3', 'v1']);
        assert.equal(undo(removals()[0]), true);
        assert.equal(shown('v2').favorite, true);
        assert.deepEqual(drawn(), ['c:video:v1:v2', 'v1']);
        await settleTurns();
        await answer({ favorite: true });
        assert.deepEqual(calls.map((c) => c.args[1].favorite), [false, true]);
        assert.equal(toasts.some((t) => t.message === 'Added to Favorites'), false);
    """)


def test_undo_while_the_removal_is_still_queued_writes_behind_it():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        undo(removals()[0]);
        assert.deepEqual(drawn(), ['c:video:v1:v2', 'v1']);
        await settleTurns();
        await answer({ favorite: false });
        await answer({ favorite: true });
        assert.deepEqual(calls.map((c) => c.args[1].favorite), [false, true]);
        assert.equal(shown('v2').favorite, true);
    """)


def test_undo_after_the_author_moved_on_favorites_without_moving_the_view():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3', 'v4');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        state.overlayState.compareRightAssetId = 'v4';
        renderInspectOverlay();
        undo(removals()[0]);
        assert.equal(shown('v2').favorite, true);
        assert.deepEqual(drawn(), ['c:video:v1:v4', 'v1']);
    """)


def test_undo_brings_single_inspect_back_and_a_closed_viewer_stays_closed():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2');
        openSingle('v1');
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual(drawn(), ['s:v2', 'v2']);
        undo(removals()[0]);
        assert.deepEqual(drawn(), ['s:v1', 'v1']);
        // The last favorite closes the viewer; Undo favorites it without reopening.
        handleOverlayFavoriteKey({ repeat: false });
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual(drawn(), ['closed', '']);
        undo(removals()[removals().length - 1]);
        assert.equal(shown('v2').favorite, true);
        assert.deepEqual(drawn(), ['closed', '']);
    """)


def test_a_failed_removal_takes_its_notice_down_and_the_failure_stands():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        await settleTurns();
        await fail(httpError(500));
        assert.equal(removals()[0].dismissed, true);
        assert.ok(toasts.some((t) => t.tier === 'error' && t.message === 'Failed to update favorite.'));
        assert.deepEqual(drawn(), ['c:video:v1:v2', 'v1']);
    """)


def test_a_failed_undo_leaves_again_like_a_removal_instead_of_closing():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2', { compareCycleSide: 'A' });
        handleOverlayFavoriteKey({ repeat: false });
        assert.deepEqual(drawn(), ['c:video:v3:v2', 'v3']);
        await settleTurns();
        await answer({ favorite: false });
        undo(removals()[0]);
        assert.deepEqual(drawn(), ['c:video:v1:v2', 'v1']);
        await settleTurns();
        await fail(httpError(500));
        // v1 left again: A takes a contender and the viewer stays open.
        assert.equal(shown('v1').favorite, false);
        assert.deepEqual(drawn(), ['c:video:v3:v2', 'v3']);
    """)


def test_undo_is_inert_after_teardown_or_a_project_change_and_never_throws():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        const notice = removals()[0];
        projectDir = 'other';
        assert.equal(undo(notice), false);
        projectDir = 'project';
        state.destroyed = true;
        assert.equal(undo(notice), false);
        state.destroyed = false;
        console.warn = () => {};
        Object.defineProperty(state, 'destroyed', { get() { throw new Error('boom'); }, configurable: true });
        assert.equal(undo(notice), false);
        await settleTurns();
        assert.equal(calls.length, 1);
    """)


def test_a_removal_in_the_gallery_list_offers_undo_too():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2');
        handleToggleFavorite(shown('v1'));
        assert.equal(removals().length, 1);
        undo(removals()[0]);
        assert.equal(shown('v1').favorite, true);
        await settleTurns();
        await answer({ favorite: false });
        await answer({ favorite: true });
        assert.deepEqual(calls.map((c) => c.args[1].favorite), [false, true]);
    """)


def test_teardown_takes_the_notice_down():
    # destroy() tears down DOM and observers, so its call is pinned by source;
    # setData's project switch is run for real in test_gallery_current_scene_refresh_js.
    source = GALLERY.read_text(encoding="utf-8")
    destroy = _gallery_function(source, "destroy")
    assert "dismissFavoriteRemovalNotice();" in destroy


def test_an_older_failure_leaves_the_newer_notice_the_one_that_is_tracked():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3', 'v4', 'v5');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        handleToggleFavorite(shown('v3'));
        await settleTurns();
        await fail(httpError(500));
        // v2's failure took down only its own notice, long gone; v3's stays.
        assert.equal(removals()[1].dismissed, false);
        handleToggleFavorite(shown('v4'));
        assert.equal(removals()[1].dismissed, true);
        assert.equal(removals().filter((t) => !t.dismissed).length, 1);
    """)


def test_a_second_undo_click_sends_nothing_and_undo_never_favorites_a_trashed_asset():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        const notice = removals()[0];
        assert.equal(undo(notice), true);
        assert.equal(undo(notice), false);
        await settleTurns();
        await answer({ favorite: false });
        await answer({ favorite: true });
        assert.equal(calls.length, 2);
        handleToggleFavorite(shown('v3'));
        const shownIndex = data.assets.findIndex((entry) => entry.asset_id === 'v3');
        data.assets[shownIndex] = { ...data.assets[shownIndex], trashed_at: 'now' };
        assert.equal(undo(removals()[1]), false);
    """)


def test_a_failure_a_later_write_answers_stays_quiet_and_the_last_one_speaks():
    _run(_REMOVAL + """
        favoritesView = true;
        data.assets = fav(video, 'v1', 'v2', 'v3');
        openCompare('v1', 'v1', 'v2');
        handleToggleFavorite(shown('v2'));
        undo(removals()[0]);
        await settleTurns();
        await fail(httpError(500));
        // The removal failed, but the Undo already asked for the same value.
        assert.equal(toasts.some((t) => t.tier === 'error'), false);
        await fail(httpError(500));
        assert.ok(toasts.some((t) => t.tier === 'error' && t.message === 'Failed to update favorite.'));
    """)


# ── Compare rebuilds carry each side's media ──────────────────────────────


def test_a_new_compare_video_stays_hidden_over_the_old_frame_until_it_has_its_own():
    source = GALLERY.read_text(encoding="utf-8")
    cap = re.search(r"^    const VIDEO_UNDERLAY_MAX_MS = \d+;$", source, re.M)[0]
    helper = _gallery_function(source, "holdVideoOverUnderlay")
    _run_gesture_node(cap + "\n" + helper + """
        const video = () => Object.assign(new EventTarget(), { readyState: 0, seeking: false, style: {} });
        const under = () => ({ removed: false, remove() { this.removed = true; } });
        // Waits for a frame, and for a restore seek to land.
        let layer = video(), old = under(), shown = 0;
        holdVideoOverUnderlay(layer, old, () => { shown += 1; });
        assert.equal(layer.style.opacity, '0');
        layer.readyState = 1;
        layer.dispatchEvent(new Event('canplay'));
        assert.equal(old.removed, false);
        layer.readyState = 4; layer.seeking = true;
        layer.dispatchEvent(new Event('loadeddata'));
        assert.equal(old.removed, false);
        layer.seeking = false;
        layer.dispatchEvent(new Event('seeked'));
        assert.deepEqual([old.removed, layer.style.opacity, shown], [true, '', 1]);
        layer.dispatchEvent(new Event('seeked'));
        assert.equal(shown, 1);
        // A load error ends the hold rather than leaving the old frame.
        layer = video(); old = under();
        holdVideoOverUnderlay(layer, old);
        layer.dispatchEvent(new Event('error'));
        assert.equal(old.removed, true);
        // A rebuild's cleanup stops listening without touching the DOM.
        layer = video(); old = under();
        const stop = holdVideoOverUnderlay(layer, old);
        stop();
        layer.readyState = 4;
        layer.dispatchEvent(new Event('loadeddata'));
        assert.deepEqual([old.removed, layer.style.opacity], [false, '0']);
        // Nothing to hold: the new video shows as it loads.
        layer = video();
        holdVideoOverUnderlay(layer, null);
        assert.equal(layer.style.opacity, undefined);
    """)


def test_each_compare_render_hands_its_on_screen_media_to_the_next():
    source = GALLERY.read_text(encoding="utf-8")
    render = _gallery_function(source, "renderInspectOverlay")
    assert render.index("const carriedCompareMedia = overlay.compareStageMedia;") \
        < render.index("clearOverlayRuntime();")
    assert "renderCompareOverlay(asset, mediaWrap, carriedCompareMedia);" in render
    close = _gallery_function(source, "closeInspectOverlay")
    assert "state.overlayState.compareStageMedia = null;" in close
