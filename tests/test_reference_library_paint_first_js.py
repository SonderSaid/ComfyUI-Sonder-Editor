"""The Reference Library paints create identity, add member and reorder before the server answers.

The Library shows acknowledged References plus pending overlays, and each
overlay is owned by its own mutation: it leaves when that mutation settles,
never because a fetch landed, since a fetch sent while the write was queued
describes the Library without it (Finding L). A refusal the server answered
drops the overlay (rollback without the network); a lost response keeps it as
unconfirmed until a read that postdates the write decides.

The host half runs the real `EditorWidget` methods against a server the test
answers by hand; the UI half mounts the real Library on a minimal DOM with that
same widget as its host.
"""
import json
from pathlib import Path

from test_project_mutation_queue import _run_gesture_node
from test_reference_library_js import _run_node

ROOT = Path(__file__).resolve().parents[1]
MODEL = (ROOT / "web" / "js" / "reference_library_model.js").as_uri()
LIBRARY = (ROOT / "web" / "js" / "editor_reference_library.js").as_uri()
NOTIFICATIONS = (ROOT / "web" / "js" / "editor_notifications.js").as_uri()


def test_overlays_paint_creates_and_reorders_over_acknowledged_data():
    _run_node(f"""
        import assert from 'node:assert/strict';
        import * as model from {MODEL!r};
        const member = (id, order) => ({{ member_id: id, asset_id: 'a-' + id, order }});
        const acknowledged = [
            {{ reference_id: 'r', name: 'R', kind: 'character', members: [member('m0', 0), member('m1', 1)] }},
        ];
        const frozen = JSON.stringify(acknowledged);
        const overlays = [
            model.referenceOverlayFromOperation({{ type: 'create_reference',
                fields: {{ name: 'New', kind: 'location' }} }}, 'pending:1'),
            model.referenceOverlayFromOperation({{ type: 'reorder_members', reference_id: 'r',
                expected_member_ids: ['m0', 'm1'], member_ids: ['m1', 'm0'] }}, 'pending:2'),
            model.referenceOverlayFromOperation({{ type: 'create_member', reference_id: 'r',
                fields: {{ asset_id: 'a-new', name: 'Side' }} }}, 'pending:3'),
            model.referenceOverlayFromOperation({{ type: 'create_member', reference_id: 'gone',
                fields: {{ asset_id: 'a-x' }} }}, 'pending:4'),
        ];
        assert.equal(model.referenceOverlayFromOperation({{ type: 'update_reference' }}, 'k'), null);
        const shown = model.applyPendingReferenceOverlays(acknowledged, overlays);
        // View-only: acknowledged data is never written.
        assert.equal(JSON.stringify(acknowledged), frozen);
        assert.deepEqual(shown.map((entry) => entry.reference_id), ['r', 'pending:1']);
        assert.equal(shown[1].pendingStatus, 'saving');
        assert.equal(shown[1].kind, 'location');
        assert.equal(shown[1].reference_class, 'context');
        assert.deepEqual(shown[1].members, []);
        // Reorder, then the add appended at `order = len(members)`, as the route does.
        assert.deepEqual(shown[0].members.map((entry) => [entry.member_id, entry.order, entry.pendingStatus]),
            [['m1', 0, undefined], ['m0', 1, undefined], ['pending:3', 2, 'saving']]);
        // A create on a Reference acknowledged data no longer holds paints nothing.
        assert.equal(shown.some((entry) => entry.reference_id === 'gone'), false);

        // Reflected only by what the server returned.
        const create = {{ ...overlays[0] }};
        assert.equal(model.referenceOverlayReflected(acknowledged, create), false);
        create.committedId = 'r';
        assert.equal(model.referenceOverlayReflected(acknowledged, create), true);
        const add = {{ ...overlays[2], committedId: 'm1' }};
        assert.equal(model.referenceOverlayReflected(acknowledged, add), true);
        // A matching order is not evidence: Up then Down restores it.
        assert.equal(model.referenceOverlayReflected(
            [{{ ...acknowledged[0], members: [member('m1', 0), member('m0', 1)] }}], overlays[1]), false);
    """)


# The real widget methods, a server the test answers by hand in send order,
# and the Library's view of the widget.
_HOST = """
        const notes = await import(%(notifications)r);
        let toastList = [];
        notes.subscribe((list) => { toastList = list; });
        const toastTexts = () => toastList.map((item) => item.message);
        const w = makeWidget();
        Object.assign(w, {
            _references: [], _referenceOverlays: [], _referenceOverlaySeq: 0,
            _referenceFetchSeq: 0, _referenceMutationSeq: 0, _referenceSeqSource: 'fetch',
            _referencesLoaded: true, _referencesLoading: false, _referencesError: '',
            _staleReplayTimers: new Map(), _staleReplayGovernors: new Map(),
            assets: {}, scenes: [], _promptSemanticUnits: [],
            _settings: { layout: { fullscreenSidebarContent: 'references' } },
        });
        w._refreshPromptContextDependencyConsumers = () => {};
        let libraryRenders = 0;
        w._referenceLibraryHandle = { render: () => { libraryRenders += 1; } };
        // The real deferral and replay: a deferred Library read is sent once
        // the queue drains, so the test answers it like any other request.
        const deferred = [];
        w._deferProjectBackedRefresh = function (keys, reason) {
            deferred.push([...keys, reason]);
            return EditorWidget.prototype._deferProjectBackedRefresh.call(this, keys, reason);
        };
        w._replayDeferredProjectBackedRefresh = EditorWidget.prototype._replayDeferredProjectBackedRefresh;
        w._fetchProjectSettings = () => {};
        const requests = [], waiting = [];
        globalThis.fetch = (url, init = {}) => {
            const request = { url: String(url), method: init.method || 'GET',
                body: init.body ? JSON.parse(init.body) : null };
            requests.push(request);
            return new Promise((resolve, reject) => waiting.push({ request, resolve, reject }));
        };
        const turns = async () => { for (let i = 0; i < 10; i += 1) await new Promise((r) => setTimeout(r, 0)); };
        const nextOf = (method) => waiting.findIndex((entry) => entry.request.method === method);
        const reply = async (method, status, body) => {
            const [entry] = waiting.splice(nextOf(method), 1);
            entry.resolve(new Response(JSON.stringify(body), { status }));
            await turns();
        };
        const lose = async (method) => {
            const [entry] = waiting.splice(nextOf(method), 1);
            entry.reject(new TypeError('Failed to fetch'));
            await turns();
        };
        const library = (references, results = []) => ({ status: 'ok', results, references });
        const ref = (id, name, members = []) => ({ reference_id: id, name, kind: 'character',
            reference_class: 'subject', description: '', visual_intent: 'preserve',
            audio_intent: 'reference_characteristics', members });
        const mem = (id) => ({ member_id: id, asset_id: 'a-' + id, name: id, tags: [], prompt: '',
            crop: null, source_start_sec: 0, source_end_sec: null });
        const shown = () => w._referenceLibraryData().references;
        const ids = () => shown().map((entry) => entry.reference_id + (entry.pendingStatus ? '*' + entry.pendingStatus : ''));
        const createOp = (name) => ({ type: 'create_reference', fields: { name, kind: 'character',
            reference_class: 'subject', description: '', visual_intent: 'preserve',
            audio_intent: 'reference_characteristics' } });
        const settled = (promise) => promise.then((value) => ({ ok: value }), (error) => ({ error }));
        // Long timers (the unconfirmed retry) are captured, not run.
        const realTimeout = globalThis.setTimeout, scheduled = [];
        globalThis.setTimeout = (fn, ms, ...rest) => ms >= 1000
            ? (scheduled.push({ fn, ms }), scheduled.length) : realTimeout(fn, ms, ...rest);
""" % {"notifications": NOTIFICATIONS}


def test_a_create_paints_at_once_and_its_own_payload_replaces_the_overlay():
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        assert.deepEqual(ids(), ['a', 'pending:1*saving']);
        assert.equal(libraryRenders, 1);
        // Only the Library reads overlays.
        assert.deepEqual(w._references.map((entry) => entry.reference_id), ['a']);
        await turns();
        assert.deepEqual(requests.map((r) => [r.method, r.body?.operations?.[0]?.type]),
            [['POST', 'create_reference']]);
        await reply('POST', 200, library([ref('a', 'A'), ref('b', 'New')],
            [{ type: 'create_reference', reference_id: 'b' }]));
        assert.ok((await done).ok);
        assert.deepEqual(ids(), ['a', 'b']);
        assert.equal(w._referenceOverlays.length, 0);
        assert.deepEqual(deferred, [], 'an applied payload needs no refresh');
    """)


def test_a_fetch_during_the_write_never_clears_it_and_the_change_is_not_hidden():
    # Finding L: a forced fetch sent while the create is queued takes the
    # generation, so the create's own payload cannot apply.
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        const read = w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'probe' });
        await turns();
        // The read answers first, from before the write committed.
        await reply('GET', 200, library([ref('a', 'A')]));
        await read;
        assert.deepEqual(ids(), ['a', 'pending:1*saving'], 'a fetch never clears an overlay');
        await reply('POST', 200, library([ref('a', 'A'), ref('b', 'New')],
            [{ type: 'create_reference', reference_id: 'b' }]));
        assert.ok((await done).ok);
        // Its payload lost the gate: the row stays until a later read shows it...
        assert.deepEqual(w._references.map((entry) => entry.reference_id), ['a']);
        assert.deepEqual(ids(), ['a', 'pending:1*saving']);
        // ...and that read is requested, behind the queue, and sent once it drains.
        assert.deepEqual(deferred, [['references', 'reference_mutation_superseded']]);
        assert.equal(waiting.length, 1);
        await reply('GET', 200, library([ref('a', 'A'), ref('b', 'New')]));
        assert.deepEqual(ids(), ['a', 'b'], 'shown once, from acknowledged data');
        assert.equal(w._referenceOverlays.length, 0);
    """)


def test_an_acknowledged_create_leaves_when_data_already_shows_its_committed_id():
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'probe' });
        await turns();
        // This read was sent before the commit but answered after it.
        await reply('GET', 200, library([ref('a', 'A'), ref('b', 'New')]));
        assert.deepEqual(ids(), ['a', 'b', 'pending:1*saving'], 'a pending row may double briefly');
        await reply('POST', 200, library([ref('a', 'A'), ref('b', 'New')],
            [{ type: 'create_reference', reference_id: 'b' }]));
        assert.ok((await done).ok);
        assert.deepEqual(ids(), ['a', 'b'], 'the committed id is already shown');
    """)


def test_a_refused_write_drops_only_its_own_overlay():
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        const first = settled(w._mutateReferencesPaintFirst([createOp('One')]));
        const second = settled(w._mutateReferencesPaintFirst([createOp('Two')]));
        assert.deepEqual(ids(), ['a', 'pending:1*saving', 'pending:2*saving']);
        await turns();
        await reply('POST', 400, { error: 'Reference name is required', code: 'invalid_reference' });
        const refused = await first;
        assert.equal(refused.error.status, 400);
        // Rolled back locally; the other write is untouched and still queued.
        assert.deepEqual(ids(), ['a', 'pending:2*saving']);
        assert.equal(waiting.length, 1);
        await reply('POST', 200, library([ref('a', 'A'), ref('c', 'Two')],
            [{ type: 'create_reference', reference_id: 'c' }]));
        assert.ok((await second).ok);
        assert.deepEqual(ids(), ['a', 'c']);
    """)


def test_a_lost_response_keeps_the_row_as_unconfirmed_until_a_later_read():
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        await turns();
        // A read already in flight when the write settles cannot decide it.
        const early = w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'early' });
        await turns();
        await lose('POST');
        const lost = await done;
        assert.equal(lost.error.status, undefined);
        assert.deepEqual(ids(), ['a', 'pending:1*unconfirmed']);
        // The settle sends one read of its own, even with the sidebar elsewhere.
        assert.equal(waiting.filter((entry) => entry.request.method === 'GET').length, 2);
        assert.ok(deferred.some((entry) => entry.includes('reference_unconfirmed')));
        await reply('GET', 200, library([ref('a', 'A')]));
        await early;
        assert.deepEqual(ids(), ['a', 'pending:1*unconfirmed']);
        // A read sent after the settle does decide: here the write had committed.
        await reply('GET', 200, library([ref('a', 'A'), ref('b', 'New')]));
        assert.deepEqual(ids(), ['a', 'b']);
    """)


def test_an_unconfirmed_row_keeps_re_reading_while_the_server_is_unreachable():
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        await turns();
        await lose('POST');
        await done;
        await lose('GET');
        assert.deepEqual(scheduled.map((entry) => entry.ms), [2000]);
        scheduled.shift().fn();
        await turns();
        await lose('GET');
        assert.deepEqual(scheduled.map((entry) => entry.ms), [4000], 'backs off');
        assert.deepEqual(ids(), ['a', 'pending:1*unconfirmed']);
        scheduled.shift().fn();
        await turns();
        // The server is back and never saw the write: the row goes.
        await reply('GET', 200, library([ref('a', 'A')]));
        assert.deepEqual(ids(), ['a']);
        assert.equal(w._referenceUnconfirmedRetryAttempt, 0);
        // Nothing unconfirmed is left, so a failed read schedules nothing.
        const read = w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'x' });
        await turns();
        await lose('GET');
        await read;
        assert.deepEqual(scheduled, []);
    """)


def test_a_later_mutation_payload_settles_an_earlier_acknowledged_overlay():
    _run_gesture_node(_HOST + """
        w._references = [ref('r', 'R', [mem('m0'), mem('m1')])];
        const add = settled(w._mutateReferencesPaintFirst([{ type: 'create_member', reference_id: 'r',
            fields: { asset_id: 'a-new', name: 'New' } }]));
        const rename = settled(w._mutateReferences([{ type: 'update_reference', reference_id: 'r',
            fields: { name: 'R2' }, expected: { name: 'R' } }]));
        await turns();
        const members = () => shown()[0].members.map((entry) => entry.member_id + (entry.pendingStatus ? '*' : ''));
        assert.deepEqual(members(), ['m0', 'm1', 'pending:1*']);
        // The add's payload loses the gate to the queued rename, which runs after
        // it: no refresh is needed, and the row stays until that payload lands.
        await reply('POST', 200, library([ref('r', 'R', [mem('m0'), mem('m1'), mem('m2')])],
            [{ type: 'create_member', reference_id: 'r', member_id: 'm2' }]));
        assert.ok((await add).ok);
        assert.deepEqual(members(), ['m0', 'm1', 'pending:1*']);
        assert.deepEqual(deferred, []);
        await reply('POST', 200, library([ref('r', 'R2', [mem('m0'), mem('m1'), mem('m2')])],
            [{ type: 'update_reference', reference_id: 'r' }]));
        assert.ok((await rename).ok);
        assert.deepEqual(members(), ['m0', 'm1', 'm2']);
        assert.equal(w._referenceOverlays.length, 0);
    """)


def test_a_project_switch_drops_overlays_and_a_late_settle_touches_nothing():
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        const done = settled(w._mutateReferencesPaintFirst([createOp('New')]));
        await turns();
        for (const name of ['_clearStaleReplayState', '_updateSceneIdentity', '_updateProjectIdentity',
            '_stopPlayback', '_clearVideoCache', '_sweepRenderCache', '_fetchProjectSettings',
            '_renderQueuePanel']) w[name] = () => {};
        w._fetchAssets = () => Promise.resolve();
        w._renderCacheSweepGeneration = 0;
        w._referenceUnconfirmedRetryTimer = 7;
        w._referenceUnconfirmedRetryAttempt = 3;
        w.updateProject('other');
        assert.deepEqual(w._referenceOverlays, []);
        assert.equal(w._referenceUnconfirmedRetryTimer, null);
        assert.equal(w._referenceUnconfirmedRetryAttempt, 0);
        await reply('POST', 200, library([ref('a', 'A'), ref('b', 'New')],
            [{ type: 'create_reference', reference_id: 'b' }]));
        await done;
        assert.deepEqual(ids(), []);
    """)


# A minimal DOM for the Library, as in test_reference_library_js.py.
_DOM = """
        class Element {
            constructor(tag) {
                this.tag = tag; this.style = {}; this.dataset = {}; this.attributes = {};
                this.children = []; this.parentElement = null; this.listeners = new Map();
                this.scrollTop = 0; this.value = ''; this.textContent = ''; this.title = '';
                this.disabled = false; this.draggable = false;
            }
            set innerHTML(value) { for (const child of this.children) child.parentElement = null; this.children = []; }
            get innerHTML() { return ''; }
            get childNodes() { return this.children; }
            append(...items) { for (const item of items) this.appendChild(item); }
            appendChild(child) { this.children.push(child); child.parentElement = this; return child; }
            setAttribute(key, value) { this.attributes[key] = String(value); }
            getAttribute(key) { return this.attributes[key]; }
            addEventListener(key, fn) {
                if (!this.listeners.has(key)) this.listeners.set(key, []);
                this.listeners.get(key).push(fn);
            }
            removeEventListener(key, fn) {
                this.listeners.set(key, (this.listeners.get(key) || []).filter((item) => item !== fn));
            }
            emit(key, event = {}) {
                for (const fn of this.listeners.get(key) || []) fn({ preventDefault() {}, stopPropagation() {}, ...event });
            }
            click() { if (!this.disabled) this.emit('click'); }
            get isConnected() { return this === document.body || !!this.parentElement?.isConnected; }
            contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
            getClientRects() { return [{ width: 300 }]; }
            focus() { document.activeElement = this; }
            setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
            querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
            querySelectorAll(selector) {
                const match = (node) => selector === '[data-reference-library-body="true"]'
                    ? node.dataset.referenceLibraryBody === 'true'
                    : (selector === '[data-reference-search="true"]'
                        ? node.dataset.referenceSearch === 'true' : node.tag === selector);
                const found = [];
                const visit = (node) => { for (const child of node.children || []) { if (match(child)) found.push(child); visit(child); } };
                visit(this);
                return found;
            }
        }
        globalThis.document = new Element('document');
        document.createElement = (tag) => new Element(tag);
        document.body = new Element('body');
        const { mountReferenceLibrary } = await import(%(library)r);
        const container = new Element('container');
        document.body.appendChild(container);
        let pickAsset = () => {};
        const mounted = mountReferenceLibrary(container, {
            getData: () => w._referenceLibraryData(),
            mutate: (operations) => w._mutateReferences(operations),
            mutatePaintFirst: (operations, options) => w._mutateReferencesPaintFirst(operations, options),
            confirm: () => true, pickAsset: (args) => pickAsset(args), assetPreviewUrl: () => null,
        });
        w._referenceLibraryHandle = mounted;
        const all = (node) => [node, ...node.children.flatMap(all)];
        const buttons = (text) => all(container).filter((node) => node.tag === 'button' && node.textContent === text);
        const button = (text) => buttons(text)[0];
        const cards = () => container.querySelectorAll('section');
        const cardText = (card) => all(card).map((node) => node.textContent).filter(Boolean).join('|');
        const nameInput = () => all(container).find((node) => node.tag === 'input' && node.attributes['aria-label'] === 'Name');
""" % {"library": LIBRARY}


def test_the_create_form_closes_at_submit_and_paints_an_inert_card():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('a', 'Alpha', [mem('m0')])];
        mounted.render();
        button('Manage').click();
        button('+').click();
        nameInput().value = 'Bravo';
        nameInput().emit('input');
        button('Save').click();
        // Closed at submit: a second Save cannot create a duplicate.
        assert.equal(nameInput(), undefined);
        assert.equal(cards().length, 2);
        const pending = cards()[1];
        assert.match(cardText(pending), /Bravo/);
        assert.match(cardText(pending), /Saving…/);
        assert.equal(pending.children[0].draggable, false);
        assert.equal(pending.children[0].listeners.size, 0, 'no expand, drag or context menu');
        // Manage shows Edit/Delete for the settled card only.
        assert.equal(buttons('Edit').length, 1);
        assert.equal(buttons('Delete').length, 1);
        await turns();
        await reply('POST', 200, library([ref('a', 'Alpha', [mem('m0')]), ref('b', 'Bravo')],
            [{ type: 'create_reference', reference_id: 'b' }]));
        assert.doesNotMatch(cardText(cards()[1]), /Saving…/);
        assert.equal(buttons('Delete').length, 2);
    """)


def test_a_refused_create_returns_the_form_with_its_text_and_the_error():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('+').click();
        nameInput().value = 'Bravo';
        nameInput().emit('input');
        button('Save').click();
        await turns();
        await reply('POST', 409, { error: 'Prompt handle is taken', code: 'prompt_handle_conflict' });
        assert.equal(cards().length, 0, 'the entity form is open again');
        assert.equal(nameInput().value, 'Bravo');
        assert.ok(all(container).some((node) => node.textContent === 'Prompt handle is taken'));
    """)


def test_an_unconfirmed_create_keeps_the_card_and_does_not_return_the_draft():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('+').click();
        nameInput().value = 'Bravo';
        nameInput().emit('input');
        button('Save').click();
        await turns();
        await lose('POST');
        assert.equal(nameInput(), undefined, 'a re-save could duplicate a committed write');
        assert.match(cardText(cards()[1]), /Not confirmed/);
        assert.ok(all(container).some((node) => /could not be confirmed/.test(node.textContent)));
        assert.ok(deferred.some((entry) => entry.includes('references')), 'the Library is re-read');
    """)


def test_an_added_member_paints_and_locks_the_order_controls_until_it_settles():
    _run_gesture_node(_HOST + _DOM + """
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
        assert.equal(button('Save'), undefined, 'the member form closes at submit');
        assert.deepEqual(requests, [], 'painted before anything is sent');
        assert.match(cardText(cards()[0]), /Rider · Side/);
        assert.match(cardText(cards()[0]), /Saving…/);
        for (const label of ['Up', 'Down', 'Remove']) {
            assert.ok(buttons(label).every((node) => node.disabled), label + ' waits for the add');
        }
        assert.ok(buttons('Edit').filter((node) => node.parentElement?.tag === 'div').some((node) => !node.disabled));
        assert.equal(button('Delete').disabled, true, 'the entity delete names the exact member list');
        await turns();
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m0'), mem('m1'), mem('m2')])],
            [{ type: 'create_member', reference_id: 'r', member_id: 'm2' }]));
        assert.equal(requests[0].body.operations[0].fields.asset_id, 'a-new');
        assert.doesNotMatch(cardText(cards()[0]), /Saving…/);
        assert.ok(buttons('Up').every((node) => !node.disabled));
        assert.equal(button('Delete').disabled, false);
    """)


def test_a_refused_member_add_returns_its_form():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('r', 'Rider', [mem('m0')])];
        w.assets = { image: [{ asset_id: 'a-new', asset_type: 'image', name: 'new.png' }] };
        mounted.render();
        cards()[0].children[0].click();
        pickAsset = ({ onPick }) => onPick(w.assets.image[0]);
        button('+ Member').click();
        button('Image').click();
        const memberName = () => all(container).find((node) => node.tag === 'input' && node.attributes['aria-label'] === 'Member name');
        memberName().value = 'Side';
        memberName().emit('input');
        button('Save').click();
        await turns();
        await reply('POST', 409, { error: 'Prompt handle is taken', code: 'prompt_handle_conflict' });
        assert.equal(memberName().value, 'Side');
        assert.doesNotMatch(cardText(cards()[0]), /Saving…/);
        assert.ok(all(container).some((node) => node.textContent === 'Prompt handle is taken'));
    """)


def test_up_twice_sends_two_reorders_each_expecting_the_order_before_it():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('r', 'Rider', [mem('m0'), mem('m1'), mem('m2')])];
        mounted.render();
        cards()[0].children[0].click();
        const order = () => all(cards()[0]).filter((node) => node.tag === 'button'
            && node.textContent.startsWith('Rider · ')).map((node) => node.textContent.split(' — ')[0].slice(8));
        const up = (name) => {
            const line = all(cards()[0]).find((node) => node.tag === 'button' && node.textContent.startsWith('Rider · ' + name + ' —'));
            line.parentElement.children.at(-1).children.find((node) => node.textContent === 'Up').click();
        };
        up('m2');
        assert.deepEqual(order(), ['m0', 'm2', 'm1'], 'painted before the server answers');
        up('m2');
        assert.deepEqual(order(), ['m2', 'm0', 'm1']);
        await turns();
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m0'), mem('m2'), mem('m1')])],
            [{ type: 'reorder_members', reference_id: 'r' }]));
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m2'), mem('m0'), mem('m1')])],
            [{ type: 'reorder_members', reference_id: 'r' }]));
        const sent = requests.map((r) => r.body.operations[0]);
        assert.deepEqual(sent.map((op) => [op.expected_member_ids, op.member_ids]), [
            [['m0', 'm1', 'm2'], ['m0', 'm2', 'm1']],
            [['m0', 'm2', 'm1'], ['m2', 'm0', 'm1']],
        ]);
        assert.deepEqual(order(), ['m2', 'm0', 'm1']);
        assert.equal(w._referenceOverlays.length, 0);
    """)


def test_a_refused_reorder_rolls_back_and_takes_its_follower_with_it():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('r', 'Rider', [mem('m0'), mem('m1'), mem('m2')])];
        mounted.render();
        cards()[0].children[0].click();
        const order = () => all(cards()[0]).filter((node) => node.tag === 'button'
            && node.textContent.startsWith('Rider · ')).map((node) => node.textContent.split(' — ')[0].slice(8));
        const up = (name) => {
            const line = all(cards()[0]).find((node) => node.tag === 'button' && node.textContent.startsWith('Rider · ' + name + ' —'));
            line.parentElement.children.at(-1).children.find((node) => node.textContent === 'Up').click();
        };
        up('m2');
        up('m2');
        await turns();
        // Changed elsewhere: terminal for this write, never a replay.
        await reply('POST', 409, { error: 'Reference member order changed', code: 'identity_mismatch' });
        await reply('POST', 409, { error: 'Reference member order changed', code: 'identity_mismatch' });
        assert.deepEqual(order(), ['m0', 'm1', 'm2'], 'acknowledged order, without the network');
        assert.equal(w._referenceOverlays.length, 0);
        assert.equal(requests.length, 2);
    """)


def test_a_library_refresh_keeps_the_list_on_screen():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        const read = w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'external_refresh' });
        assert.equal(cards().length, 1, 'no "Loading references…" over a loaded Library');
        await turns();
        await reply('GET', 200, library([ref('a', 'Alpha'), ref('b', 'Bravo')]));
        await read;
        assert.equal(cards().length, 2);
        // A project with nothing loaded still shows the placeholder.
        w._referencesLoaded = false;
        w._referencesLoading = true;
        mounted.render();
        assert.equal(cards().length, 0);
        assert.ok(all(container).some((node) => node.textContent === 'Loading references…'));
    """)


def test_an_unconfirmed_create_that_did_not_land_says_so_and_returns_the_form():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('+').click();
        nameInput().value = 'Bravo';
        nameInput().emit('input');
        button('Save').click();
        await turns();
        await lose('POST');
        assert.equal(nameInput(), undefined);
        // The read that decides: the server holds no Bravo.
        await reply('GET', 200, library([ref('a', 'Alpha')]));
        await turns();
        assert.equal(cards().length, 0, 'the entity form is back');
        assert.equal(nameInput().value, 'Bravo');
        assert.ok(all(container).some((node) => /was not saved/.test(node.textContent)));
        assert.ok(toastTexts().includes('A Reference Library change was not saved.'));
    """)


def test_an_unconfirmed_create_that_did_land_keeps_the_row_and_clears_the_warning():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('+').click();
        nameInput().value = 'Bravo';
        nameInput().emit('input');
        button('Save').click();
        await turns();
        await lose('POST');
        await reply('GET', 200, library([ref('a', 'Alpha'), ref('b', 'Bravo')]));
        await turns();
        assert.equal(nameInput(), undefined, 'no draft: it is saved');
        assert.equal(cards().length, 2);
        assert.doesNotMatch(cardText(cards()[1]), /Not confirmed|Saving/);
        assert.equal(all(container).some((node) => /could not be confirmed/.test(node.textContent)), false);
        assert.equal(toastTexts().includes('A Reference Library change was not saved.'), false);
    """)


def test_an_acknowledged_add_whose_refresh_failed_keeps_re_reading():
    # Audit #2: Finding L, then the superseded refresh fails. Without a retry the
    # committed member stayed "Saving…" and its Reference's order controls locked.
    _run_gesture_node(_HOST + """
        w._references = [ref('r', 'R', [mem('m0'), mem('m1')])];
        const add = settled(w._mutateReferencesPaintFirst([{ type: 'create_member', reference_id: 'r',
            fields: { asset_id: 'a-new', name: 'New' } }]));
        const read = w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'probe' });
        await turns();
        await reply('GET', 200, library([ref('r', 'R', [mem('m0'), mem('m1')])]));
        await read;
        await reply('POST', 200, library([ref('r', 'R', [mem('m0'), mem('m1'), mem('m2')])],
            [{ type: 'create_member', reference_id: 'r', member_id: 'm2' }]));
        assert.ok((await add).ok);
        await lose('GET');
        assert.deepEqual(scheduled.map((entry) => entry.ms), [2000]);
        scheduled.shift().fn();
        await turns();
        await reply('GET', 200, library([ref('r', 'R', [mem('m0'), mem('m1'), mem('m2')])]));
        assert.deepEqual(shown()[0].members.map((entry) => entry.member_id), ['m0', 'm1', 'm2']);
        assert.equal(w._referenceOverlays.length, 0);
    """)


def test_the_retry_gives_up_after_ten_reads():
    _run_gesture_node(_HOST + """
        w._references = [ref('a', 'A')];
        settled(w._mutateReferencesPaintFirst([createOp('New')]));
        await turns();
        await lose('POST');
        await lose('GET');
        const delays = [];
        while (scheduled.length) {
            const next = scheduled.shift();
            delays.push(next.ms);
            next.fn();
            await turns();
            await lose('GET');
        }
        assert.deepEqual(delays, [2000, 4000, 8000, 16000, 30000, 30000, 30000, 30000, 30000, 30000]);
        assert.deepEqual(ids(), ['a', 'pending:1*unconfirmed'], 'still shown; any later read settles it');
    """)


def test_up_then_down_under_a_stale_read_never_shows_an_order_the_server_lacks():
    # Audit #4: [m0, m1] -> Up (m1, m0) -> Down (m0, m1); a read from before both
    # takes the generation. The second reorder must not leave before the first.
    _run_gesture_node(_HOST + """
        w._references = [ref('r', 'R', [mem('m0'), mem('m1')])];
        const up = settled(w._mutateReferencesPaintFirst([{ type: 'reorder_members', reference_id: 'r',
            expected_member_ids: ['m0', 'm1'], member_ids: ['m1', 'm0'] }]));
        const down = settled(w._mutateReferencesPaintFirst([{ type: 'reorder_members', reference_id: 'r',
            expected_member_ids: ['m1', 'm0'], member_ids: ['m0', 'm1'] }]));
        const order = () => shown()[0].members.map((entry) => entry.member_id).join(',');
        const read = w._fetchReferences({ ignoreMutationGate: true, force: true, reason: 'probe' });
        await turns();
        await reply('GET', 200, library([ref('r', 'R', [mem('m0'), mem('m1')])]));
        await read;
        await reply('POST', 200, library([ref('r', 'R', [mem('m1'), mem('m0')])],
            [{ type: 'reorder_members', reference_id: 'r' }]));
        await reply('POST', 200, library([ref('r', 'R', [mem('m0'), mem('m1')])],
            [{ type: 'reorder_members', reference_id: 'r' }]));
        await Promise.all([up, down]);
        assert.equal(order(), 'm0,m1', 'what the server holds');
        assert.equal(w._referenceOverlays.length, 2, 'both wait for a read that postdates them');
    """)


def test_a_refused_reorder_names_the_change_elsewhere():
    # Audit #5: the route's code arrives on the payload, not on `error.code`.
    _run_gesture_node(_HOST + """
        w._references = [ref('r', 'R', [mem('m0'), mem('m1')])];
        const up = settled(w._mutateReferencesPaintFirst([{ type: 'reorder_members', reference_id: 'r',
            expected_member_ids: ['m0', 'm1'], member_ids: ['m1', 'm0'] }]));
        await turns();
        await reply('POST', 409, { error: 'Reference member order changed', code: 'identity_mismatch' });
        await up;
        assert.ok(toastTexts().includes('Reference changed elsewhere — Library refreshed.'), toastTexts().join(' | '));
    """)


def test_a_lost_response_on_any_library_write_re_reads_the_library():
    # Audit #8: the "refreshing" message is kept for writes without overlays too.
    _run_gesture_node(_HOST + """
        w._references = [ref('r', 'R')];
        const rename = settled(w._mutateReferences([{ type: 'update_reference', reference_id: 'r',
            fields: { name: 'R2' }, expected: { name: 'R' } }]));
        await turns();
        await lose('POST');
        await rename;
        assert.ok(deferred.some((entry) => entry.includes('reference_unconfirmed')));
        assert.equal(waiting.filter((entry) => entry.request.method === 'GET').length, 1);
    """)


def test_a_save_settling_while_the_next_form_is_typed_in_keeps_its_focus():
    # Audit #3: every host render rebuilds the tree.
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('a', 'Alpha')];
        mounted.render();
        button('+').click();
        nameInput().value = 'Bravo';
        nameInput().emit('input');
        button('Save').click();
        button('+').click();
        const typing = nameInput();
        typing.focus();
        typing.value = 'Char';
        typing.emit('input');
        typing.setSelectionRange(4, 4);
        await turns();
        await reply('POST', 200, library([ref('a', 'Alpha'), ref('b', 'Bravo')],
            [{ type: 'create_reference', reference_id: 'b' }]));
        assert.notEqual(nameInput(), typing, 'the tree was rebuilt');
        assert.equal(document.activeElement, nameInput());
        assert.equal(nameInput().value, 'Char');
        assert.equal(nameInput().selectionStart, 4);
    """)


def test_overlay_renders_keep_the_list_scroll_and_a_new_card_is_revealed():
    _run_gesture_node(_HOST + _DOM + """
        w._references = [ref('r', 'Rider', [mem('m0'), mem('m1'), mem('m2')]), ref('a', 'Alpha')];
        mounted.render();
        const body = () => container.querySelector('[data-reference-library-body="true"]');
        cards()[0].children[0].click();
        body().scrollTop = 57;
        body().emit('scroll');
        const line = all(cards()[0]).find((node) => node.tag === 'button' && node.textContent.startsWith('Rider · m2 —'));
        line.parentElement.children.at(-1).children.find((node) => node.textContent === 'Up').click();
        assert.equal(body().scrollTop, 57, 'the reorder paint keeps the list where it was');
        await turns();
        await reply('POST', 200, library([ref('r', 'Rider', [mem('m0'), mem('m2'), mem('m1')]), ref('a', 'Alpha')],
            [{ type: 'reorder_members', reference_id: 'r' }]));
        assert.equal(body().scrollTop, 57, 'and so does its settle');
        // A new Reference is appended at the end; the list goes there.
        body().scrollHeight = 900;
        Object.defineProperty(Element.prototype, 'scrollHeight', { get() { return 900; }, configurable: true });
        button('+').click();
        nameInput().value = 'Zed';
        nameInput().emit('input');
        button('Save').click();
        assert.equal(body().scrollTop, 900);
    """)


def test_the_fullscreen_host_forwards_the_library_callback():
    # The Library hears how an unconfirmed write turned out through the options
    # argument; an adapter that drops it silently loses the returned draft.
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    assert ("mutatePaintFirst: (operations, options) => "
            "this._mutateReferencesPaintFirst(operations, options),") in widget
