"""The Reference Library paints its edits and deletes before the server answers.

Paint-first Phase 2: `update_reference`, `update_member`, `delete_reference`
and `delete_member` go through the same view-only overlays as create, add and
reorder. A form keeps the record it was opened from as its base: Save sends
only the keys the author changed, guarded by the base's values, and the form
closes at submit. A refusal drops the overlay and hands an edit's draft back,
with its base recaptured, into an empty slot. A lost answer keeps the paint
until a read decides it.

Harness: `test_reference_library_paint_first_js.py`'s -- the real `EditorWidget`
methods against a server the test answers by hand in send order, and the real
Library mounted on a minimal DOM. The staged-item half of a delete, and the
route's answers, are exercised on the real route in
`test_reference_library_writes_js.py`.
"""
from test_project_mutation_queue import _run_gesture_node
from test_reference_library_js import _run_node
from test_reference_library_paint_first_js import _DOM, _HOST, MODEL


# Helpers over the mounted Library, on top of the paint-first harness.
_FORMS = """
        const field = (label) => all(container).find((node) =>
            ['input', 'textarea', 'select'].includes(node.tag) && node.attributes['aria-label'] === label);
        const type = (label, value) => { const node = field(label); node.value = value; node.emit('input'); };
        const cardNamed = (name) => cards().find((card) => cardText(card).startsWith(name));
        const within = (node, text) => all(node).filter((child) => child.tag === 'button' && child.textContent === text);
        const memberLine = (card, prefix) => all(card).find((node) => node.tag === 'button'
            && node.textContent.startsWith(prefix));
        const memberControls = (card, prefix) => memberLine(card, prefix).parentElement.children.at(-1).children;
        const control = (card, prefix, label) => memberControls(card, prefix).find((node) => node.textContent === label);
        const posted = () => requests.filter((entry) => entry.method === 'POST').map((entry) => entry.body.operations);
        const errors = () => all(container).map((node) => node.textContent)
            .filter((value) => /not saved|no longer exists|could not be confirmed/.test(value));
"""


def test_the_model_paints_edits_and_deletes_and_keeps_only_creates_inert():
    _run_node(f"""
        import assert from 'node:assert/strict';
        import * as model from {MODEL!r};
        const member = (id, order, extra = {{}}) => ({{ member_id: id, asset_id: 'a-' + id, order,
            name: id, tags: [], prompt: '', ...extra }});
        const acknowledged = [
            {{ reference_id: 'r', name: 'R', description: '', members: [member('m0', 0), member('m1', 1), member('m2', 2)] }},
            {{ reference_id: 'q', name: 'Q', members: [member('n0', 0)] }},
        ];
        const frozen = JSON.stringify(acknowledged);
        const op = (operation, key) => model.referenceOverlayFromOperation(operation, key);
        const overlays = [
            op({{ type: 'update_reference', reference_id: 'r', fields: {{ name: 'Rider' }},
                expected: {{ name: 'R' }} }}, 'k1'),
            op({{ type: 'update_member', reference_id: 'r', member_id: 'm1', fields: {{ prompt: 'side' }},
                expected: {{ prompt: '' }} }}, 'k2'),
            op({{ type: 'delete_member', reference_id: 'r', member_id: 'm0', expected: member('m0', 0) }}, 'k3'),
            op({{ type: 'delete_reference', reference_id: 'q', expected: {{ member_ids: ['n0'] }} }}, 'k4'),
            op({{ type: 'create_member', reference_id: 'r', fields: {{ asset_id: 'a-new' }} }}, 'pending:5'),
        ];
        assert.equal(overlays[1].member_id, 'm1');
        assert.deepEqual(overlays[1].expected, {{ prompt: '' }});
        const shown = model.applyPendingReferenceOverlays(acknowledged, overlays);
        assert.equal(JSON.stringify(acknowledged), frozen, 'view-only');
        assert.deepEqual(shown.map((entry) => entry.reference_id), ['r'], 'the deleted Reference is gone');
        assert.equal(shown[0].name, 'Rider');
        assert.equal(shown[0].pendingStatus, 'saving');
        assert.equal(shown[0].pendingInert, undefined, 'an updated row keeps its real id and stays usable');
        // The delete renumbers densely, as the route does; the add goes last.
        assert.deepEqual(shown[0].members.map((entry) => [entry.member_id, entry.order,
            entry.pendingStatus ?? null, entry.pendingInert ?? null]),
            [['m1', 0, 'saving', null], ['m2', 1, null, null], ['pending:5', 2, 'saving', true]]);
        assert.equal(shown[0].members[0].prompt, 'side');

        // Reflected: a delete once its target is gone; an update never.
        assert.equal(model.referenceOverlayReflected(acknowledged, overlays[2]), false);
        assert.equal(model.referenceOverlayReflected(
            [{{ ...acknowledged[0], members: [member('m1', 0)] }}], overlays[2]), true);
        assert.equal(model.referenceOverlayReflected([acknowledged[0]], overlays[3]), true);
        assert.equal(model.referenceOverlayReflected(
            [{{ ...acknowledged[0], name: 'Rider' }}], overlays[0]), false);

        // What a delete in flight takes away is not displayed; the host's
        // `_referenceMemberBeingDeleted` is "stored, but not in the view".
        const displayed = shown.flatMap((entry) => entry.members.map((m) => m.member_id));
        assert.deepEqual(displayed.filter((id) => ['m0', 'n0'].includes(id)), []);

        // Stored-form comparison for a lost update.
        assert.equal(model.referenceFieldStoredAs('tags', ['Sonder:Portrait'], ['sonder:portrait']), true);
        assert.equal(model.referenceFieldStoredAs('crop', {{ x: 0.1, y: 0, w: 0.5, h: 1 }},
            {{ x: 0.1 + 1e-12, y: 0, w: 0.5, h: 1 }}), true);
        assert.equal(model.referenceFieldStoredAs('source_end_sec', null, null), true);
        assert.equal(model.referenceFieldStoredAs('source_end_sec', 2, 2.5), false);
        assert.equal(model.referenceFieldStoredAs('name', 'A', 'B'), false);
    """)


def test_an_identity_edit_paints_at_once_and_sends_only_what_changed():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha', [mem('m0')])];
        mounted.render();
        button('Manage').click();
        within(cardNamed('Alpha'), 'Edit')[0].click();
        type('Name', 'Alder');
        button('Save').click();
        assert.equal(field('Name'), undefined, 'the form closes at Save');
        assert.match(cardText(cards()[0]), /Alder/);
        assert.match(cardText(cards()[0]), /Saving…/);
        // Usable while saving: its id is real.
        assert.equal(within(cards()[0], 'Edit').length, 1);
        assert.equal(within(cards()[0], 'Delete').length, 1);
        await turns();
        assert.deepEqual(posted(), [[{ type: 'update_reference', reference_id: 'a',
            fields: { name: 'Alder' }, expected: { name: 'Alpha' } }]]);
        await reply('POST', 200, library([{ ...ref('a', 'Alder', [mem('m0')]) }],
            [{ type: 'update_reference', reference_id: 'a' }]));
        assert.doesNotMatch(cardText(cards()[0]), /Saving…/);
        assert.deepEqual(w._referenceOverlays, []);
    """)


def test_a_refused_identity_edit_rolls_back_and_returns_its_draft_once():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('Manage').click();
        within(cardNamed('Alpha'), 'Edit')[0].click();
        type('Name', 'Alder');
        button('Save').click();
        await turns();
        await reply('POST', 409, { error: 'Reference changed', code: 'identity_mismatch' });
        assert.deepEqual(w._referenceOverlays, [], 'the paint is dropped');
        assert.equal(field('Name').value, 'Alder', 'the draft is back');
        assert.equal(errors().filter((text) => text === 'The change was not saved.').length, 1);
        const toasts = toastTexts().filter((text) => /Reference/.test(text));
        assert.deepEqual(toasts, ['Reference changed elsewhere — Library refreshed.']);
    """)


def test_a_member_edit_sends_only_its_changed_keys_guarded_by_the_form_base():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('r', 'Rider', [{ ...mem('m0'), handle: 'rider', prompt: 'old' }])];
        w.assets = { image: [{ asset_id: 'a-m0', asset_type: 'image', name: 'm0.png' }] };
        mounted.render();
        cards()[0].children[0].click();
        control(cards()[0], 'Rider · m0 —', 'Edit').click();
        type('Prompt', 'new prompt');
        button('Save').click();
        await turns();
        assert.deepEqual(posted(), [[{ type: 'update_member', reference_id: 'r', member_id: 'm0',
            fields: { prompt: 'new prompt' }, expected: { prompt: 'old' } }]]);
        // Painted, still usable, and labelled.
        assert.match(cardText(cards()[0]), /new prompt/);
        assert.match(cardText(cards()[0]), /Saving…/);
        assert.equal(control(cards()[0], 'Rider · m0 —', 'Remove').disabled, false);
    """)


def test_a_member_remove_paints_and_its_guard_carries_no_overlay_keys():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('r', 'Rider', [mem('m0'), mem('m1')])];
        w.assets = { image: [{ asset_id: 'a-m0', asset_type: 'image', name: 'm0.png' },
            { asset_id: 'a-m1', asset_type: 'image', name: 'm1.png' }] };
        mounted.render();
        cards()[0].children[0].click();
        control(cards()[0], 'Rider · m1 —', 'Edit').click();
        type('Prompt', 'edited');
        button('Save').click();
        // Remove the member whose edit is still saving: the guard is the
        // displayed row, own paint included and decoration stripped.
        control(cards()[0], 'Rider · m1 —', 'Remove').click();
        assert.equal(memberLine(cards()[0], 'Rider · m1 —'), undefined, 'the member leaves at once');
        await turns();
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m0'), { ...mem('m1'), prompt: 'edited', order: 1 }])],
            [{ type: 'update_member', reference_id: 'r', member_id: 'm1' }]));
        const [, [remove]] = posted();
        assert.equal(remove.type, 'delete_member');
        assert.equal(remove.expected.prompt, 'edited');
        assert.equal('pendingStatus' in remove.expected, false);
        assert.equal('pendingInert' in remove.expected, false);
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m0')])],
            [{ type: 'delete_member', reference_id: 'r', member_id: 'm1' }]));
        assert.deepEqual(w._referenceOverlays, []);
        assert.equal(memberLine(cards()[0], 'Rider · m1 —'), undefined);
    """)


def test_a_reference_delete_paints_and_a_lost_one_that_did_not_land_comes_back():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha', [mem('m0')]), ref('b', 'Bravo')];
        mounted.render();
        button('Manage').click();
        within(cardNamed('Bravo'), 'Delete')[0].click();
        assert.deepEqual(cards().map(cardText).map((text) => text.split('|')[0]), ['Alpha']);
        await turns();
        assert.deepEqual(posted()[0][0].expected.member_ids, []);
        await lose('POST');
        // Kept hidden: the delete may have landed.
        assert.deepEqual(cards().map(cardText).map((text) => text.split('|')[0]), ['Alpha']);
        await reply('GET', 200, library([ref('a', 'Alpha', [mem('m0')]), ref('b', 'Bravo')]));
        assert.deepEqual(cards().map(cardText).map((text) => text.split('|')[0]), ['Alpha', 'Bravo']);
        assert.ok(toastTexts().includes('A Reference Library change was not saved.'));
    """)


def test_a_lost_save_then_an_accepted_save_of_the_same_field_resolves_as_saved():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('Manage').click();
        within(cardNamed('Alpha'), 'Edit')[0].click();
        type('Name', 'A-one');
        button('Save').click();
        within(cardNamed('A-one'), 'Edit')[0].click();
        type('Name', 'A-two');
        button('Save').click();
        await turns();
        const [[first], [second]] = [posted()[0], []];
        assert.equal(first.fields.name, 'A-one');
        await lose('POST');
        await turns();
        // The second was opened from the first's paint, so it is guarded by it.
        assert.deepEqual(posted()[1][0].expected, { name: 'A-one' });
        await reply('POST', 200, library([ref('a', 'A-two')], [{ type: 'update_reference', reference_id: 'a' }]));
        // The deciding read, by a later mutation payload: A counts as saved.
        assert.equal(field('Name'), undefined, 'no draft comes back');
        assert.ok(!toastTexts().includes('A Reference Library change was not saved.'));
        assert.deepEqual(cards().map(cardText).map((text) => text.split('|')[0]), ['A-two']);
    """)


def test_a_returned_draft_is_saved_against_the_row_displayed_when_it_is_saved_again():
    """A base captured at the handback could still hold the refused write's
    paint, or a follower's, or predate the Library refresh the refusal asked
    for; the returned draft would then be refused on every Save."""
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('Manage').click();
        within(cardNamed('Alpha'), 'Edit')[0].click();
        type('Name', 'Alder');
        button('Save').click();
        within(cardNamed('Alder'), 'Edit')[0].click();
        type('Name', 'Aldo');
        button('Save').click();
        await turns();
        await reply('POST', 409, { error: 'Reference changed', code: 'identity_mismatch' });
        // The follower, guarded by the first's paint, is refused with it; its
        // draft stays out, because the first draft took the one slot.
        await reply('POST', 409, { error: 'Reference changed', code: 'identity_mismatch' });
        assert.equal(field('Name').value, 'Alder');
        // Meanwhile another tab renamed it and wrote a description; a read
        // brought both in.
        w._applyReferencePayload(library([{ ...ref('a', 'Aspen'), description: 'elsewhere' }]),
            { requestSeq: w._referenceFetchSeq });
        button('Save').click();
        await turns();
        // Only what the author changed from the form's original base, so the
        // description written elsewhere is not reverted; guarded by the row
        // displayed now.
        assert.deepEqual(posted()[2][0], { type: 'update_reference', reference_id: 'a',
            fields: { name: 'Alder' }, expected: { name: 'Aspen' } });
    """)


def test_an_edit_of_a_reference_deleted_elsewhere_is_refused_locally_and_never_creates_one():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha'), ref('b', 'Bravo')];
        mounted.render();
        button('Manage').click();
        within(cardNamed('Alpha'), 'Edit')[0].click();
        type('Name', 'Alder');
        w._applyReferencePayload(library([ref('b', 'Bravo')]), { requestSeq: w._referenceFetchSeq });
        button('Save').click();
        await turns();
        assert.deepEqual(posted(), []);
        assert.ok(errors().includes('This Reference no longer exists.'));
    """)


def test_a_pending_create_stays_inert_while_an_edit_being_saved_does_not():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha', [mem('m0')])];
        w.assets = { image: [{ asset_id: 'a-m0', asset_type: 'image', name: 'm0.png' },
            { asset_id: 'a-new', asset_type: 'image', name: 'new.png' }] };
        mounted.render();
        button('Manage').click();
        cards()[0].children[0].click();
        pickAsset = ({ onPick }) => onPick(w.assets.image[1]);
        button('+ Member').click();
        button('Image').click();
        button('Save').click();
        // The new member is inert, and still gates Delete and the order controls.
        assert.equal(within(cards()[0], 'Delete')[0].disabled, true);
        assert.equal(control(cards()[0], 'Alpha · m0 —', 'Remove').disabled, true);
    """)


def test_an_edit_settling_while_the_next_form_is_typed_in_keeps_its_focus():
    _run_gesture_node(_HOST + _DOM + _FORMS + """
        w._references = [ref('a', 'Alpha'), ref('b', 'Bravo')];
        mounted.render();
        button('Manage').click();
        within(cardNamed('Alpha'), 'Edit')[0].click();
        type('Name', 'Alder');
        button('Save').click();
        within(cardNamed('Bravo'), 'Edit')[0].click();
        const typing = field('Name');
        typing.focus();
        typing.value = 'Brav';
        typing.emit('input');
        typing.setSelectionRange(3, 3);
        await turns();
        await reply('POST', 200, library([ref('a', 'Alder'), ref('b', 'Bravo')],
            [{ type: 'update_reference', reference_id: 'a' }]));
        assert.equal(document.activeElement, field('Name'));
        assert.equal(field('Name').value, 'Brav');
        assert.equal(field('Name').selectionStart, 3);
    """)
