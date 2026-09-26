"""Favoriting inside the Inspect view's compare stage.

The slot resolver runs as imported. The gallery's overlay functions are sliced
from its source onto the paint-first harness, so a favorite goes through the
real paint, write chain and repaint decision; only the DOM-building renderers
are stubs.
"""
import re

from test_gallery_paint_first_js import GALLERY, GALLERY_HARNESS, _PENDING_HOST, gallery_write_source
from test_project_mutation_queue import _run_gesture_node

SCOPE_URL = (GALLERY.parent / "inspect_overlay_scope.js").as_uri()

_OVERLAY_FUNCTIONS = (
    "compareModeActive", "compareStageShown", "compareStageDrawn", "resolvedCompareSlots",
    "compareSideAsset", "overlayFavoriteTarget", "handleOverlayFavoriteKey", "refreshCompareInPlace",
    "compareListSignature", "syncOverlayWithAssets",
    "overlayAssets", "currentOverlayAsset", "sameTypeOverlayAssets",
)


def _gallery_function(source: str, name: str) -> str:
    return re.search(r"^    (?:async )?function " + name + r"\(.*?^    \}\n", source, re.M | re.S)[0]


def _overlay_source() -> str:
    source = GALLERY.read_text(encoding="utf-8")
    return "\n".join(_gallery_function(source, name) for name in _OVERLAY_FUNCTIONS)


# The overlay's inputs: the gallery view (All, or Favorites) and an open
# compare stage whose list and star hooks count their repaints. The render stub
# draws what the real one would: it closes without an anchor, and otherwise
# writes the resolved slots back and stamps the signature of the view it drew,
# so a sequence of rebuilds runs on states the real overlay can reach.
_OVERLAY_HARNESS = """
        const { resolveCompareSlots, resolveInspectOverlayScope } = await import('__SCOPE_URL__');
        let favoritesView = false;
        const activeNavigableAssets = () => data.assets.filter((entry) =>
            !isTrashed(entry) && (!favoritesView || entry.favorite));
        const sortAssets = (assets) => [...assets];
        const comparePickerHasMetadataQuery = () => false;
        const renderInspectOverlay = () => {
            overlayRenders += 1;
            const overlay = state.overlayState;
            if (!overlay.open || !currentOverlayAsset()) {
                Object.assign(overlay, { open: false, mediaSignature: '' });
                return;
            }
            if (compareStageShown()) {
                const resolved = resolvedCompareSlots();
                overlay.compareLeftAssetId = resolved.leftId;
                overlay.compareRightAssetId = resolved.rightId;
                overlay.mediaSignature = `c:video:${resolved.leftId}:${resolved.rightId}`;
                state.overlayCompareListSignature = compareListSignature();
            } else {
                overlay.mediaSignature = `s:${overlay.assetId}`;
            }
        };
        let choosersRefreshes = 0, starRefreshes = 0;
        const openCompare = (anchor, left, right, extra = {}) => {
            Object.assign(state.overlayState, { open: true, origin: 'gallery', compareMode: true,
                assetId: anchor, compareLeftAssetId: left, compareRightAssetId: right,
                compareCycleSide: 'B', mediaSignature: `c:video:${left}:${right}`, ...extra });
            state.overlayCompareChoosersRefresh = () => { choosersRefreshes += 1; };
            state.overlayCompareFavoriteRefresh = () => { starRefreshes += 1; };
            state.overlayCompareListSignature = compareListSignature();
        };
        const video = (id, extra = {}) => asset(id, { asset_type: 'video', path: `${id}.mp4`, ...extra });
""".replace("__SCOPE_URL__", SCOPE_URL)


# The paint-first harness's render stub and single-view sync give way to the
# drawing stub below and the real sync.
_HARNESS = re.sub(r"        const renderInspectOverlay = .*?(?=        const clearUsageView)", "",
                  GALLERY_HARNESS, count=1, flags=re.S)
assert "syncOverlayWithAssets" not in _HARNESS and "renderInspectOverlay" not in _HARNESS


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
    assert write_back < body.index("const signature = `c:")
    assert "overlay.compareRightAssetId = resolved.rightId;" in body
    # S goes through the named, tested handler in every overlay mode.
    assert 'if (event.key === "s" || event.key === "S") return handleOverlayFavoriteKey(event);' in body
