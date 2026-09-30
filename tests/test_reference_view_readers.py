"""Who reads the Library as the server holds it, and who reads it as displayed.

Since Library paint-first Phase 3, surfaces that display or offer Library data
read the effective view (`EditorWidget._referencesView`: acknowledged
`_references` plus pending overlays), so a row still saving is usable and a row
being deleted is not offered. `_references` keeps its meaning -- the server's
acknowledged payload -- and the paths that describe server truth read it.

A new reader of `_references` is server truth by default, which is the safe
failure; this pin makes that choice explicit. Every `this._references` /
`host._references` and `_customReferenceRecipes` site in `web/js` must sit in a
listed scope, as a writer or as an acknowledged reader with its reason, or be
the legacy fallback of a view read (`host._referencesView?.() ?? ...`). Every
listed scope must still read or write it.

A second pin keeps the view's memo honest: the overlay list and its fields are
written only through the two helpers that bump `_referenceOverlayVersion`.

Scope resolution reuses `test_scene_mutation_registration.py`'s scanners
(innermost named scope, strings and comments masked). It is a lexical pin, not
data-flow analysis: a value read here and passed elsewhere is not followed.
"""

import re
from pathlib import Path

from test_reference_library_paint_first_js import _HOST
from test_project_mutation_queue import _run_gesture_node
from test_scene_mutation_registration import _code_mask, _enclosing_scope, _scopes

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"

_READ = re.compile(r"\b(?:this|host)\._(references|customReferenceRecipes)\b")
# The legacy fallback of a view read, for a host without the view.
_VIEW_FALLBACK = re.compile(r"_(?:referencesView|referencesOfferable|referenceRecipesView)\?\.\(\)\s*\?\?\s*\(?$")

WRITERS = {
    ("editor_widget.js", "constructor"),
    ("editor_widget.js", "_applyReferencePayload"),
    ("editor_widget.js", "updateProject"),
}

ACKNOWLEDGED = {
    ("editor_widget.js", "_referencesView"):
        "builds the view from the acknowledged payload",
    ("editor_widget.js", "_referenceRecipesView"):
        "builds the recipe view from the acknowledged payload",
    ("editor_widget.js", "_referenceOverlayReflected"):
        "an overlay leaves when acknowledged data shows what it painted",
    ("editor_widget.js", "_referenceStoredHandle"):
        "Attach materializes against the handle the server stored",
    ("editor_widget.js", "stateFor"):
        "a history operation compares server truth after a forced read",
    ("editor_widget.js", "_referenceOverlayKnownIds"):
        "records what the server held when an answer was lost",
    ("editor_widget.js", "_unconfirmedReferenceOverlaySaved"):
        "decides a lost answer from what the server holds",
    ("editor_widget.js", "_materializeReferenceMemberHandleWithinGesture"):
        "materialize returns the handle the server stored",
    ("editor_widget.js", "_referenceMemberBeingDeleted"):
        "held by the server but no longer displayed is what a delete in flight means",
    ("editor_widget.js", "_candidateNamesPendingReferenceCreate"):
        "which creates the server does not hold yet",
    ("editor_prompt_panel.js", "rollbackPromptPhysicalAttachment"):
        "a compensation compares the stored handle after a forced read",
    ("editor_prompt_panel.js", "runCopyContribution"):
        "handles pair with a server-compiled copy plan",
}


def _sites():
    """`(module, scope, field, kind)` for every masked read or write."""
    found = []
    for path in sorted(JS.glob("*.js")):
        source = path.read_text(encoding="utf-8")
        mask = _code_mask(source)
        scopes = None
        for match in _READ.finditer(source):
            if not mask[match.start()]:
                continue
            scopes = scopes or _scopes(source, mask)
            line_start = source.rfind("\n", 0, match.start()) + 1
            before = source[line_start:match.start()].rstrip()
            if _VIEW_FALLBACK.search(before):
                kind = "view-fallback"
            elif re.match(r"\s*=(?!=)", source[match.end():match.end() + 4]):
                kind = "write"
            else:
                kind = "read"
            found.append((path.name, _enclosing_scope(scopes, match.start()), match.group(1), kind))
    return found


def test_every_acknowledged_library_reader_is_classified():
    unlisted = []
    for module, scope, field, kind in _sites():
        if kind == "view-fallback":
            continue
        key = (module, scope)
        if kind == "write" and key in WRITERS:
            continue
        if kind == "read" and (key in ACKNOWLEDGED or key in WRITERS):
            continue
        unlisted.append((module, scope, field, kind))
    assert not unlisted, (
        "Classify each new reader of acknowledged Library data: display and offers read "
        f"`_referencesView()`; server-truth readers are listed with a reason: {unlisted}")


def test_every_listed_scope_still_reads_or_writes_library_data():
    live = {(module, scope) for module, scope, _field, kind in _sites() if kind != "view-fallback"}
    stale = sorted((set(ACKNOWLEDGED) | WRITERS) - live)
    assert not stale, f"Listed scopes that no longer read Library data: {stale}"
    assert all(reason.strip() for reason in ACKNOWLEDGED.values())


def test_view_fallbacks_are_only_for_hosts_without_the_view():
    fallbacks = {(module, scope) for module, scope, _field, kind in _sites() if kind == "view-fallback"}
    # Modules only: the widget itself always reads the view directly.
    assert fallbacks and all(module != "editor_widget.js" for module, _scope in fallbacks)


def test_overlays_are_written_only_through_the_version_bumping_helpers():
    source = (JS / "editor_widget.js").read_text(encoding="utf-8")
    mask = _code_mask(source)
    scopes = _scopes(source, mask)
    writes = [
        (re.compile(r"\b_referenceOverlays\s*=(?!=)"), {"constructor", "_setReferenceOverlays"}),
        (re.compile(r"\b_referenceOverlays\.(?:push|splice|pop|shift|unshift|sort|reverse)\("), set()),
        (re.compile(r"\b_referenceOverlayVersion\s*=(?!=)"),
         {"constructor", "_setReferenceOverlays", "_setReferenceOverlayFields"}),
        (re.compile(r"\b(?:overlay|earlier|later)\.(?:state|status|committedId|createdId|knownIds"
                    r"|provenSaved|clearAfterSeq|cascade|fields)\s*=(?!=)"), set()),
        (re.compile(r"Object\.assign\(\s*overlay\b"), {"_setReferenceOverlayFields"}),
        # Overlay-only field names, on any variable.
        (re.compile(r"\.(?:createdId|committedId|knownIds|provenSaved|clearAfterSeq)\s*=(?!=)"), set()),
    ]
    offenders = []
    for pattern, allowed in writes:
        for match in pattern.finditer(source):
            if mask[match.start()] and _enclosing_scope(scopes, match.start()) not in allowed:
                offenders.append((pattern.pattern, _enclosing_scope(scopes, match.start())))
    assert not offenders, offenders


def test_a_pending_create_resolves_through_the_view_but_not_as_server_truth():
    _run_gesture_node(_HOST + """
        w._references = [ref('r', 'R', [mem('m0')])];
        w._referenceClientIds = true;
        w._findAssetById = () => null;
        const done = settled(w._mutateReferencesPaintFirst([{ type: 'create_member', reference_id: 'r',
            fields: { asset_id: 'a-new', name: 'New', tags: [], prompt: '', crop: null,
                source_start_sec: 0, source_end_sec: null } }]));
        const minted = w._referenceOverlays[0].createdId;
        assert.match(minted, /^[0-9a-f]{32}$/);
        // Displayed and usable: staging, drop and labels resolve it.
        assert.equal(w._referenceMemberForRef({ member_id: minted })?.reference?.reference_id, 'r');
        assert.equal(w._referencesView()[0].members[1].member_id, minted);
        // Not server truth, and not offered to a chip picker.
        assert.equal(w._references[0].members.length, 1);
        assert.deepEqual(w._referencesOfferable()[0].members.map((m) => m.member_id), ['m0']);
        assert.equal(w._referenceMemberBeingDeleted(minted), false);
        await turns();
        await reply('POST', 200, library([ref('r', 'R', [mem('m0'), { ...mem(minted), member_id: minted }])],
            [{ type: 'create_member', reference_id: 'r', member_id: minted }]));
        assert.ok((await done).ok);
        assert.deepEqual(w._referencesOfferable()[0].members.map((m) => m.member_id), ['m0', minted]);
    """)


def test_reference_prompting_and_chip_pickers_are_offered_no_pending_create():
    """Phase 3 audit #2: an identity's sources are saved by a direct write that
    never checks the member exists, so Reference Prompting is given the
    offerable list, as the chip pickers are."""
    source = (JS / "editor_prompt_panel.js").read_text(encoding="utf-8")
    start = source.index("identityPanelCleanup = mountPromptIdentityPanel(card, {")
    block = source[start:source.index("});", start)]
    assert "references: host._referencesOfferable?.()" in block
    assert "offerableReferences = () => host._referencesOfferable?.()" in source
