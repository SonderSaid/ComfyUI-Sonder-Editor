"""The gallery repaints on a scene adoption only when the scene's asset set changed.

Every scene replacement calls the gallery's `refreshCurrentScene()`. The scene
reaches the gallery only through the current-scene asset id set (the Scene badge
and the Current Scene scope), so a repaint with the set and project unchanged
paints no new pixel. The gate compares against the set a completed full render
painted, not the live set, because `setData`'s additive insert paints new rows
from the payload's set and leaves the rest alone.

The behavior tests run the gallery's own functions, sliced from its source,
with a `render` stub that stamps the way the real one does (null on entry, the
set it painted at the end). The source guards pin that stamping in the real
`render()` and every painter of badge or scope membership, so a new partial
painter cannot silently make the stamp lie.
"""
import re
from pathlib import Path

from test_project_mutation_queue import _run_node

ROOT = Path(__file__).resolve().parents[1]
GALLERY = ROOT / "web" / "js" / "shared_asset_gallery.js"
WIDGET = ROOT / "web" / "js" / "editor_widget.js"

_MODULE_FUNCTIONS = ("normalizeAssetIdSet", "sameAssetIdSet")
_GALLERY_FUNCTIONS = ("refreshCurrentSceneAssetIdsFromHost", "forgetPaintedSceneIfChanged",
                      "refreshCurrentScene", "setData")


def _gallery_source() -> str:
    return GALLERY.read_text(encoding="utf-8")


def _module_function(source: str, name: str) -> str:
    return re.search(r"^function " + name + r"\(.*?^\}\n", source, re.M | re.S)[0]


def _gallery_function(source: str, name: str) -> str:
    return re.search(r"^    (?:async )?function " + name + r"\(.*?^    \}\n", source, re.M | re.S)[0]


def _sliced(source: str) -> str:
    parts = [_module_function(source, name) for name in _MODULE_FUNCTIONS]
    parts += [_gallery_function(source, name) for name in _GALLERY_FUNCTIONS]
    return "\n".join(parts)


# The gallery state and the helpers the slice calls. `render` stamps like the
# real one: null on entry, the set it derived once the paint has completed.
HARNESS = """
        import assert from 'node:assert/strict';
        console.warn = () => {};
        const state = { destroyed: false, currentSceneAssetIds: new Set(), paintedSceneAssetIds: null,
            storageProjectId: '', selectedAssetId: '', focusedAssetId: '', selectionAnchorAssetId: '',
            showingUsagesFor: '', allowAutoFocus: false, query: '' };
        let projectDir = 'projects/one';
        const currentProjectDir = () => projectDir;
        const currentProjectId = () => projectDir.split('/').pop() || 'default';
        let hostIds = [], hostThrows = false, hostCalls = 0;
        const options = { getCurrentSceneAssetIds: () => {
            hostCalls += 1;
            if (hostThrows) throw new Error('derivation failed');
            return hostIds;
        } };
        let renders = 0, renderThrows = false;
        function render() {
            if (state.destroyed) return;
            state.paintedSceneAssetIds = null;
            renders += 1;
            state.storageProjectId = currentProjectId();
            const painted = new Set(refreshCurrentSceneAssetIdsFromHost());
            if (renderThrows) throw new Error('render failed');
            state.paintedSceneAssetIds = painted;
        }
        // setData's collaborators. `additive` decides whether the insert path
        // takes the list (true) or it falls back to a full render (false).
        const data = { assets: [], folders: [] };
        let dataProjectDir = projectDir, dataListVersion = '', additive = true;
        const detailLoader = { listChanged: () => {} };
        const detailProjectId = () => currentProjectId();
        const normalizeFolderName = (value) => String(value || '');
        const overlayPendingAssetPatches = () => {};
        const assetPatches = new Map(), assetAckedFields = new Map();
        const additiveRefreshAssets = () => [];
        const selectedAssetIdsList = () => [];
        const applySelectionState = () => {};
        const clearUsageView = () => {};
        const parseAssetSearchQuery = () => ({});
        const queryHasMetadataTerms = () => false;
        const comparePickerHasMetadataQuery = () => false;
        let additiveInsert = () => additive;
        const tryRenderAdditiveData = (...args) => additiveInsert(...args);
        const refreshThumbnailRepairObservation = () => {};
        const selectedAsset = () => null;
        const syncDetailDemand = () => {};
        // A full render with the host answering `ids`, as a scene adoption leaves it.
        const paintWith = (ids) => { hostIds = ids; render(); renders = 0; hostCalls = 0; };
"""


def _run(body: str, *, source: str | None = None) -> None:
    _run_node(HARNESS + _sliced(source or _gallery_source()) + body)


def test_same_asset_id_set_compares_membership_and_refuses_non_sets():
    _run("""
        assert.equal(sameAssetIdSet(new Set(['a', 'b']), new Set(['b', 'a'])), true);
        assert.equal(sameAssetIdSet(new Set(), new Set()), true);
        assert.equal(sameAssetIdSet(new Set(['a', 'b']), new Set(['a', 'c'])), false);
        assert.equal(sameAssetIdSet(new Set(['a']), new Set(['a', 'b'])), false);
        // Unknown is never equal, not even to an empty set.
        assert.equal(sameAssetIdSet(null, new Set()), false);
        assert.equal(sameAssetIdSet(new Set(), null), false);
        assert.equal(sameAssetIdSet(['a'], new Set(['a'])), false);
    """)


def test_an_unchanged_set_and_project_skip_the_repaint():
    _run("""
        paintWith(['a', 'b']);
        refreshCurrentScene();
        assert.equal(renders, 0);
        assert.equal(hostCalls, 1);
        // Order and container shape do not matter.
        hostIds = ['b', 'a'];
        refreshCurrentScene();
        hostIds = new Set(['a', 'b']);
        refreshCurrentScene();
        hostIds = { a: true, b: true, c: false };
        refreshCurrentScene();
        assert.equal(renders, 0);
    """)


def test_a_changed_set_repaints():
    _run("""
        for (const next of [['a', 'c'], ['a', 'b', 'c'], ['a']]) {
            paintWith(['a', 'b']);
            hostIds = next;
            refreshCurrentScene();
            assert.equal(renders, 1, JSON.stringify(next));
            assert.deepEqual([...state.paintedSceneAssetIds].sort(), [...next].sort());
            // Painted now, so the same set again is a skip.
            refreshCurrentScene();
            assert.equal(renders, 1, JSON.stringify(next));
        }
    """)


def test_a_project_change_repaints_with_an_equal_set():
    _run("""
        paintWith(['a']);
        projectDir = 'projects/two';
        refreshCurrentScene();
        assert.equal(renders, 1);
    """)


def test_an_unknown_stamp_repaints():
    _run("""
        // Never rendered.
        hostIds = ['a'];
        refreshCurrentScene();
        assert.equal(renders, 1);
        // A render that threw part-way leaves the stamp unknown, so the next
        // adoption retries even though the set did not change.
        paintWith(['a']);
        renderThrows = true;
        hostIds = ['a', 'b'];
        assert.throws(() => refreshCurrentScene(), /render failed/);
        assert.equal(state.paintedSceneAssetIds, null);
        renderThrows = false;
        refreshCurrentScene();
        assert.equal(renders, 2);
        refreshCurrentScene();
        assert.equal(renders, 2);
    """)


def test_a_failing_host_derivation_counts_as_the_empty_set():
    _run("""
        paintWith(['a']);
        hostThrows = true;
        refreshCurrentScene();
        assert.equal(renders, 1);
        assert.equal(state.currentSceneAssetIds.size, 0);
        assert.equal(state.paintedSceneAssetIds.size, 0);
        // Empty already painted: nothing to change.
        refreshCurrentScene();
        assert.equal(renders, 1);
    """)


def test_a_destroyed_gallery_neither_asks_the_host_nor_repaints():
    _run("""
        paintWith(['a']);
        state.destroyed = true;
        hostIds = ['b'];
        refreshCurrentScene();
        assert.equal(hostCalls, 0);
        assert.equal(renders, 0);
    """)


_ADDITIVE_RETURN_TO_PAINTED_SET = """
        paintWith(['s1']);
        // A list arrives while the host's scene briefly holds the new asset: the
        // insert paints its row with a Scene badge, the other rows keep theirs.
        additive = true;
        setData({ assets: [{ asset_id: 's1' }, { asset_id: 'new' }], folders: [],
            currentSceneAssetIds: ['s1', 'new'] });
        assert.equal(renders, 0);
        // The next adoption goes back to the painted set (a rollback, or Undo of
        // the drop). The new row's badge is now wrong, so this must repaint.
        hostIds = ['s1'];
        refreshCurrentScene();
"""


def test_an_additive_insert_from_another_set_forces_the_next_repaint():
    _run(_ADDITIVE_RETURN_TO_PAINTED_SET + """
        assert.equal(renders, 1);
        assert.deepEqual([...state.paintedSceneAssetIds], ['s1']);
    """)


def test_the_additive_case_fails_without_the_forget_step():
    # Proves the test above can fail: with the call removed, the stale stamp
    # matches the returning set and the wrong badge stays on screen.
    source = _gallery_source()
    call = "        forgetPaintedSceneIfChanged();\n"
    assert source.count(call) == 1
    _run(_ADDITIVE_RETURN_TO_PAINTED_SET + """
        assert.equal(renders, 0);
    """, source=source.replace(call, ""))


def test_an_additive_insert_that_throws_part_way_still_forces_the_next_repaint():
    _run("""
        paintWith(['s1']);
        // The insert paints a row from the payload's set, then fails.
        additiveInsert = () => { throw new Error('insert failed'); };
        assert.throws(() => setData({ assets: [{ asset_id: 's1' }, { asset_id: 'new' }], folders: [],
            currentSceneAssetIds: ['s1', 'new'] }), /insert failed/);
        hostIds = ['s1'];
        refreshCurrentScene();
        assert.equal(renders, 1);
    """)


def test_an_additive_insert_from_the_painted_set_keeps_the_skip():
    _run("""
        paintWith(['s1']);
        additive = true;
        setData({ assets: [{ asset_id: 's1' }, { asset_id: 'new' }], folders: [],
            currentSceneAssetIds: ['s1'] });
        // A payload without the field leaves the live set as it was.
        setData({ assets: [{ asset_id: 's1' }, { asset_id: 'new' }, { asset_id: 'more' }], folders: [] });
        hostIds = ['s1'];
        refreshCurrentScene();
        assert.equal(renders, 0);
        assert.deepEqual([...state.paintedSceneAssetIds], ['s1']);
    """)


def test_a_non_additive_set_data_repaints_and_restamps_from_the_host():
    _run("""
        paintWith(['s1']);
        additive = false;
        hostIds = ['s2'];
        setData({ assets: [{ asset_id: 's2' }], folders: [], currentSceneAssetIds: ['s1'] });
        assert.equal(renders, 1);
        // render() re-derives from the host, so the host's set is what was painted.
        assert.deepEqual([...state.paintedSceneAssetIds], ['s2']);
        refreshCurrentScene();
        assert.equal(renders, 1);
    """)


# ── Source guards ─────────────────────────────────────────────────────


def _function_spans(source: str) -> list[tuple[str, int, int]]:
    """Every gallery-level function (4-space indent) with its source span."""
    spans = []
    for match in re.finditer(r"^    (?:async )?function (\w+)\(", source, re.M):
        end = source.index("\n    }\n", match.start()) + len("\n    }\n")
        spans.append((match.group(1), match.start(), end))
    return spans


def _enclosing(spans, offset: int) -> str:
    owners = [name for name, start, end in spans if start <= offset < end]
    return owners[-1] if owners else "<top level>"


def _code_only(source: str) -> str:
    """Line comments blanked in place, so offsets and spans are unchanged."""
    return re.sub(r"//[^\n]*", lambda match: " " * len(match[0]), source)


def _callers(source: str, pattern: str) -> set[str]:
    code = _code_only(source)
    spans = _function_spans(code)
    return {_enclosing(spans, match.start()) for match in re.finditer(pattern, code)}


def _users(source: str, name: str) -> set[str]:
    """Every function naming `name` outside its own definition: calls, and
    references passed along (`.map(name)`) that would call it elsewhere."""
    return _callers(source, r"(?<!function )\b" + name + r"\b")


def test_badge_and_membership_painters_are_the_known_ones():
    source = _gallery_source()
    # Adding a user means adding a painter: make it a full render() or have
    # it clear the painted-scene stamp, then update this list.
    assert _users(source, "makeCurrentSceneMarker") == {"renderAssetText"}
    assert _users(source, "renderAssetText") == {"render", "renderActiveAssetRow", "renderTrashedAssetRow"}
    assert _users(source, "renderActiveAssetRow") == {"render", "tryRenderAdditiveData"}
    # renderTrashedAssetRow has no users (dead code, noted for cleanup).
    assert _users(source, "renderTrashedAssetRow") == set()
    assert _users(source, "tryRenderAdditiveData") == {"setData"}
    assert _users(source, "assetInCurrentScene") == {"makeCurrentSceneMarker", "assetMatchesCurrentScope"}
    assert _users(source, "currentSceneAssetIdSet") == {"assetInCurrentScene"}
    # In-place patches of existing rows and folder counts.
    assert _users(source, "assetRowElement") == {"tryRenderAdditiveData"}
    assert _users(source, "updateFolderHeaderCount") == {"tryRenderAdditiveData"}
    assert _users(source, "activeFolderCountFrom") == {"tryRenderAdditiveData"}
    # Rows enter or change in the list only through the two known painters.
    inserts = (r"listScroller\.(?:appendChild|append|prepend|insertBefore|replaceChildren|insertAdjacent\w+)\("
               r"|listScroller\.innerHTML\s*=|\banchor\.after\(|\.replaceWith\(")
    assert _callers(source, inserts) == {"render", "tryRenderAdditiveData"}


def test_the_scene_set_and_its_stamp_have_known_writers():
    source = _gallery_source()
    assert _callers(source, r"state\.currentSceneAssetIds\s*=(?!=)") == {
        "setData", "refreshCurrentSceneAssetIdsFromHost"}
    assert _callers(source, r"state\.paintedSceneAssetIds\s*=(?!=)") == {
        "render", "forgetPaintedSceneIfChanged"}
    assert "        paintedSceneAssetIds: null,\n" in source
    # Replaced, never edited in place, or a stamp could alias a changed set.
    assert not re.search(r"(?:currentSceneAssetIds|paintedSceneAssetIds)\.(?:add|delete|clear)\(", source)


def test_render_stamps_null_on_entry_and_the_painted_set_at_the_end():
    render = _gallery_function(_gallery_source(), "render")
    body = render.split("\n")
    statements = [line.strip() for line in body[1:-2] if line.strip() and not line.strip().startswith("//")]
    assert statements[0] == "if (state.destroyed) return;"
    assert statements[1] == "state.paintedSceneAssetIds = null;"
    assert statements[-1] == "state.paintedSceneAssetIds = paintedSceneAssetIds;"
    assert "const paintedSceneAssetIds = new Set(refreshCurrentSceneAssetIdsFromHost());" in statements
    # An early exit after the clear is safe: it leaves null, which repaints.


def test_set_data_forgets_before_it_paints_and_the_api_exposes_the_gate():
    source = _gallery_source()
    set_data = _gallery_function(source, "setData")
    assigned = set_data.index("state.currentSceneAssetIds = normalizeAssetIdSet(payload.currentSceneAssetIds);")
    forgotten = set_data.index("forgetPaintedSceneIfChanged();")
    assert assigned < forgotten < set_data.index("tryRenderAdditiveData(")
    assert forgotten < set_data.index("render();")
    assert re.search(r"^        refreshCurrentScene,$", source, re.M)
    widget = WIDGET.read_text(encoding="utf-8")
    set_active = re.search(r"^    _setActiveScene\(.*?^    \}\n", widget, re.M | re.S)[0]
    assert "this._assetGallery?.refreshCurrentScene?.();" in set_active
