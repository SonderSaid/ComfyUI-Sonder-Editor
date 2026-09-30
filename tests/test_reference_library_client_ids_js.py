"""A new Reference or member is usable while it saves (Library paint-first Phase 3).

When the server advertises `client_ids` in its Library payload, the host mints
the id of each create (`_mutateReferencesPaintFirst`) and the row is painted
under it: an Edit, a Remove, a reorder, a stage or a drag naming it is queued
behind the create, and the row is decided by that id rather than by matching
its name or asset. Without `client_ids` -- a browser refreshed onto a newer
pack while ComfyUI still runs the older route -- nothing is minted and the row
stays inert until acknowledged; the tests in
`test_reference_library_paint_first_js.py` run in that mode and pin it.

Same harness as that file: the real `EditorWidget` against a server the test
answers by hand, and the real Library on a minimal DOM.
"""

from test_project_mutation_queue import _run_gesture_node
from test_reference_library_paint_first_js import _DOM, _HOST

_MINTING = """
        w._referenceClientIds = true;
        const diag = (kind) => (globalThis.window?.__SONDER_CANVAS_DIAG?.events || [])
            .filter((event) => event.kind === kind);
        const HEX32 = /^[0-9a-f]{32}$/;
"""


def test_a_create_is_sent_under_a_minted_id_and_painted_under_it():
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        const minted = w._referenceOverlays[0].createdId;
        assert.match(minted, HEX32);
        assert.deepEqual(ids(), ['a', minted + '*saving']);
        const row = shown()[1];
        assert.equal(row.pendingInert, undefined, 'usable while it saves');
        assert.equal(row.pendingCreate, true, 'still skipped by chip pickers');
        await turns();
        const op = requests[0].body.operations[0];
        assert.equal(op.reference_id, minted, 'at operation level, which an older route ignores');
        assert.equal('reference_id' in op.fields, false, 'never in fields, which an older route refuses');
        await reply('POST', 200, library([ref('a', 'A'), ref(minted, 'New')],
            [{ type: 'create_reference', reference_id: minted }]));
        assert.ok((await done).ok);
        assert.deepEqual(ids(), ['a', minted]);
    """)


def test_a_create_already_shown_by_a_read_is_not_painted_twice():
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        const minted = w._referenceOverlays[0].createdId;
        w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'probe' });
        await turns();
        // A read sent before the commit but answered after it.
        await reply('GET', 200, library([ref('a', 'A'), ref(minted, 'New')]));
        assert.deepEqual(ids(), ['a', minted], 'no duplicate row: the id is already shown');
        await reply('POST', 200, library([ref('a', 'A'), ref(minted, 'New')],
            [{ type: 'create_reference', reference_id: minted }]));
        assert.ok((await done).ok);
        assert.deepEqual(ids(), ['a', minted]);
        assert.equal(w._referenceOverlays.length, 0);
    """)


def test_a_lost_create_is_decided_by_its_id_not_a_same_named_reference():
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        const minted = w._referenceOverlays[0].createdId;
        await turns();
        await lose('POST');
        await done;
        assert.deepEqual(ids(), ['a', minted + '*unconfirmed']);
        // Someone else created a Reference with the same name meanwhile; the
        // write itself never landed.
        await reply('GET', 200, library([ref('a', 'A'), ref('other', 'New')]));
        assert.deepEqual(ids(), ['a', 'other']);
        assert.ok(toastTexts().includes('A Reference Library change was not saved.'));
    """)


def test_a_lost_create_that_landed_is_kept_by_its_id():
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        const minted = w._referenceOverlays[0].createdId;
        await turns();
        await lose('POST');
        await done;
        // Renamed elsewhere after it landed: the id decides, not the name.
        await reply('GET', 200, library([ref('a', 'A'), ref(minted, 'Renamed')]));
        assert.deepEqual(ids(), ['a', minted]);
        assert.equal(toastTexts().includes('A Reference Library change was not saved.'), false);
    """)


def test_a_minted_id_the_server_ignores_is_recorded_adopted_and_not_minted_again():
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        await turns();
        await reply('POST', 200, library([ref('a', 'A'), ref('server-id', 'New')],
            [{ type: 'create_reference', reference_id: 'server-id' }]));
        assert.ok((await done).ok);
        // The canonical payload heals the row; the author is told nothing.
        assert.deepEqual(ids(), ['a', 'server-id']);
        assert.deepEqual(diag('reference_minted_id_ignored').map((event) =>
            event.operation_type ?? event.data?.operation_type), ['create_reference']);
        assert.deepEqual(toastTexts(), []);
        assert.equal(w._referenceClientIdsDemoted, true);
        const next = settled(w._mutateReferencesPaintFirst([createOp('Next')]));
        assert.equal(w._referenceOverlays[0].createdId, '', 'minting stops for this project');
        assert.equal(shown()[2].pendingInert, true);
        await turns();
        assert.equal('reference_id' in requests[1].body.operations[0], false);
        await reply('POST', 200, library([ref('a', 'A'), ref('server-id', 'New'), ref('n', 'Next')],
            [{ type: 'create_reference', reference_id: 'n' }]));
        await next;
    """)


def test_minting_follows_the_payload_and_resets_with_the_project():
    _run_gesture_node(_HOST + """
        const payload = (clientIds) => ({ references: [ref('a', 'A')],
            ...(clientIds ? { client_ids: clientIds } : {}) });
        w._applyReferencePayload(payload(['reference', 'member', 'recipe']));
        assert.equal(w._referenceClientIds, true);
        // A server restarted into an older build stops the minting at its next answer.
        w._applyReferencePayload(payload(null));
        assert.equal(w._referenceClientIds, false);
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        assert.deepEqual(ids(), ['a', 'pending:1*saving']);
        assert.equal(shown()[1].pendingInert, true, 'inert: no request may name a temporary id');
        await turns();
        assert.equal('reference_id' in requests[0].body.operations[0], false);
        await reply('POST', 200, library([ref('a', 'A'), ref('b', 'New')],
            [{ type: 'create_reference', reference_id: 'b' }]));
        await done;
    """)


def test_a_new_reference_can_be_edited_and_deleted_before_its_create_answers():
    _run_gesture_node(_HOST + _DOM + _MINTING + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('Manage').click();
        button('+').click();
        nameInput().value = 'Bravo';
        nameInput().emit('input');
        button('Save').click();
        const minted = w._referenceOverlays[0].createdId;
        assert.match(cardText(cards()[1]), /Saving…/);
        assert.equal(buttons('Edit').length, 2, 'the new card offers Edit at once');
        assert.equal(buttons('Delete').length, 2);
        buttons('Edit')[1].click();
        assert.equal(nameInput().value, 'Bravo');
        const description = all(container).find((node) => node.tag === 'textarea'
            && node.attributes['aria-label'] === 'Description');
        description.value = 'Outlaw';
        description.emit('input');
        button('Save').click();
        assert.match(cardText(cards()[1]), /Outlaw/);
        await turns();
        assert.equal(requests.length, 1, 'the edit waits behind the create');
        await reply('POST', 200, library([ref('a', 'Alpha'), ref(minted, 'Bravo')],
            [{ type: 'create_reference', reference_id: minted }]));
        const edit = requests[1].body.operations[0];
        assert.deepEqual([edit.type, edit.reference_id, edit.fields, edit.expected],
            ['update_reference', minted, { description: 'Outlaw' }, { description: '' }]);
        await reply('POST', 200, library([ref('a', 'Alpha'), { ...ref(minted, 'Bravo'), description: 'Outlaw' }],
            [{ type: 'update_reference', reference_id: minted }]));
        assert.deepEqual(ids(), ['a', minted]);
        assert.equal(w._referenceOverlays.length, 0);
    """)


def test_a_new_member_is_usable_while_it_saves_and_remove_sends_its_whole_record():
    _run_gesture_node(_HOST + _DOM + _MINTING + """
        w._references = [ref('r', 'Rider', [mem('m0'), mem('m1')])];
        w.assets = { image: [{ asset_id: 'a-new', asset_type: 'image', name: 'new.png' }] };
        mounted.render();
        button('Manage').click();
        cards()[0].children[0].click();
        pickAsset = ({ onPick }) => onPick(w.assets.image[0]);
        button('+ Member').click();
        button('Image').click();
        const memberName = all(container).find((node) => node.tag === 'input' && node.attributes['aria-label'] === 'Member name');
        memberName.value = 'Side';
        memberName.emit('input');
        button('Save').click();
        const minted = w._referenceOverlays[0].createdId;
        assert.match(minted, HEX32);
        assert.match(cardText(cards()[0]), /Rider · Side/);
        assert.match(cardText(cards()[0]), /Saving…/);
        for (const label of ['Up', 'Down', 'Remove']) {
            assert.ok(buttons(label).every((node) => !node.disabled), label + ' does not wait for the add');
        }
        assert.equal(button('Delete').disabled, false, 'the entity delete names the minted member');
        const line = all(cards()[0]).find((node) => node.tag === 'button' && node.textContent.startsWith('Rider · Side'));
        const row = line.parentElement.parentElement;
        assert.equal(row.draggable, true, 'the new member can be dragged to the timeline');
        assert.ok(row.listeners.has('dragstart') && row.listeners.has('contextmenu'));
        // Remove the member still saving.
        line.parentElement.children.at(-1).children.find((node) => node.textContent === 'Remove').click();
        assert.doesNotMatch(cardText(cards()[0]), /Rider · Side/);
        await turns();
        assert.equal(requests.length, 1, 'the Remove waits behind the add');
        const added = requests[0].body.operations[0];
        assert.equal(added.member_id, minted);
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m0'), mem('m1'),
            { member_id: minted, asset_id: 'a-new', name: 'Side', handle: '', visual_intent: '',
              audio_intent: '', attachment_defaults: {}, disabled_capabilities: [], tags: [],
              prompt: '', crop: null, order: 2, source_start_sec: 0, source_end_sec: null }])],
            [{ type: 'create_member', reference_id: 'r', member_id: minted }]));
        const remove = requests[1].body.operations[0];
        assert.equal(remove.type, 'delete_member');
        // Every `to_dict` key `delete_member` requires, and no view decoration.
        assert.deepEqual(Object.keys(remove.expected).sort(), ['asset_id', 'attachment_defaults',
            'audio_intent', 'crop', 'disabled_capabilities', 'handle', 'member_id', 'name', 'order',
            'prompt', 'source_end_sec', 'source_start_sec', 'tags', 'visual_intent']);
        assert.equal(remove.expected.order, 2);
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m0'), mem('m1')])],
            [{ type: 'delete_member', reference_id: 'r', member_id: minted }]));
        assert.equal(w._referenceOverlays.length, 0);
    """)


def test_no_compile_preview_names_a_member_whose_create_is_still_saving():
    """The compile resolves staged members from the stored Library; the payload
    that acknowledges the create refreshes the Prompt Context consumers."""
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('r', 'R', [mem('m0')])];
        const done = settled(w._mutateReferencesPaintFirst([{ type: 'create_member', reference_id: 'r',
            fields: { asset_id: 'a-new' } }]));
        const minted = w._referenceOverlays[0].createdId;
        let planned = 0;
        Object.assign(w, { projectDir: 'project', activeSceneId: 's', _selectionContextRange: () => null,
            _projectDirName: () => 'project',
            _planPromptPreviewBranches: () => { planned += 1;
                return { windowed: { state: 'current' }, scene: { state: 'current' } }; } });
        const stage = (memberId) => ({ scene_id: 's', duration_frames: 10, reference_items: [
            { reference_item_id: 'i', members: [{ entity_id: 'r', member_id: memberId }] }] });
        w.activeScene = stage(minted);
        w._previewPromptContextCandidate({}, 0);
        assert.equal(planned, 0, 'not previewed while the create is saving');
        w.activeScene = stage('m0');
        w._previewPromptContextCandidate({}, 0);
        assert.equal(planned, 1);
        await turns();
        await reply('POST', 200, library([ref('r', 'R', [mem('m0'), mem(minted)])],
            [{ type: 'create_member', reference_id: 'r', member_id: minted }]));
        await done;
        w.activeScene = stage(minted);
        w._previewPromptContextCandidate({}, 0);
        assert.equal(planned, 2, 'previewed once the create is acknowledged');
    """)


def test_a_lost_create_then_an_accepted_remove_of_it_is_not_reported_unsaved():
    """Phase 3 audit #3: a later own write naming the created id was accepted,
    so the create landed; the add form must not come back."""
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('r', 'R', [mem('m0')])];
        const decided = [];
        const adding = settled(w._mutateReferencesPaintFirst([{ type: 'create_member', reference_id: 'r',
            fields: { asset_id: 'a-new' } }], { onUnconfirmedResolved: (saved) => decided.push(saved) }));
        const minted = w._referenceOverlays[0].createdId;
        const guard = (({ pendingStatus, pendingCreate, ...rest }) => rest)(
            w._referencesView()[0].members.find((m) => m.member_id === minted));
        const removing = settled(w._mutateReferencesPaintFirst([{ type: 'delete_member', reference_id: 'r',
            member_id: minted, expected: guard }]));
        await turns();
        await lose('POST');
        await adding;
        await reply('POST', 200, library([ref('r', 'R', [mem('m0')])],
            [{ type: 'delete_member', reference_id: 'r', member_id: minted }]));
        assert.ok((await removing).ok);
        await turns();
        assert.deepEqual(decided, [true]);
        assert.equal(toastTexts().includes('A Reference Library change was not saved.'), false);
    """)


def test_a_chip_configuration_naming_a_member_still_saving_is_refused():
    """Phase 3 audit #1: a staged row offers its member as a physical source
    before the member's create answers; the chip write would not check it."""
    _run_gesture_node(_HOST + _MINTING + """
        w._references = [ref('r', 'R', [mem('m0')])];
        void settled(w._mutateReferencesPaintFirst([{ type: 'create_member', reference_id: 'r',
            fields: { asset_id: 'a-new' } }]));
        const minted = w._referenceOverlays[0].createdId;
        const chip = (id) => ({ attachment: { attachment_id: 'c', kind: 'reference',
            source: { picture_ids: [id] } } });
        assert.equal(w._acceptPromptAttachmentConfiguration(chip(minted)), null);
        assert.ok(toastTexts().some((text) => /still being saved/.test(text)));
        assert.notEqual(w._acceptPromptAttachmentConfiguration(chip('m0')), null);
    """)


def test_an_inert_row_is_never_resolvable_against_an_older_server():
    """Phase 3 audit #4: without `client_ids` the row's id is temporary, so
    staging, the drop and Lane Setup's picker cannot name it."""
    _run_gesture_node(_HOST + """
        w._references = [ref('r', 'R', [mem('m0')])];
        void settled(w._mutateReferencesPaintFirst([{ type: 'create_member', reference_id: 'r',
            fields: { asset_id: 'a-new' } }]));
        assert.equal(shown()[0].members[1].member_id, 'pending:1');
        assert.equal(w._referenceMemberForRef({ member_id: 'pending:1' }), null);
        assert.deepEqual(w._referencesOfferable()[0].members.map((m) => m.member_id), ['m0']);
    """)
