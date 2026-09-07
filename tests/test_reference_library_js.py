import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server.prompt_context import MINIMAX_H3_ROLE_CATALOGS
from server.timeline_state import REFERENCE_TAG_FAMILIES, REFERENCE_TAG_PRESETS


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_reference_model_tags_drafts_search_and_ordering():
    model = (ROOT / "web" / "js" / "reference_library_model.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import * as model from {model!r};
        const catalog = [
            {{ id: 'sonder:portrait', label: 'Portrait', asset_types: ['image', 'video'], requires_audio: false }},
            {{ id: 'sonder:motion_reference', label: 'Motion', asset_types: ['video'], requires_audio: false }},
            {{ id: 'sonder:voice_identity', label: 'Voice', asset_types: ['audio', 'video'], requires_audio: true }},
            {{ id: 'sonder:minimax_h3_identity', label: 'Identity', family: 'minimax_h3', asset_types: ['image', 'video'], requires_audio: false }},
        ];
        const families = {{ minimax_h3: {{ label: 'MiniMax H3', short: 'H3' }} }};

        assert.equal(model.formatReferenceTag('sonder:minimax_h3_identity', {{ catalog, families }}), 'MiniMax H3 · Identity');
        assert.equal(model.formatReferenceTag('sonder:minimax_h3_identity', {{ catalog, families, density: 'short' }}), 'H3·Identity');
        assert.equal(model.formatReferenceTag('sonder:portrait', {{ catalog, families }}), 'Portrait');
        assert.equal(model.formatReferenceTag('custom:handmade', {{ catalog, families }}), 'custom:handmade');
        assert.equal(model.referenceTagSearchText('sonder:minimax_h3_identity', {{ catalog, families }}), 'sonder:minimax_h3_identity MiniMax H3 · Identity');

        assert.deepEqual(
            model.normalizeReferenceTags(['  Blue   Coat ', 'blue coat', 'sonder:portrait'], catalog, {{ strict: true }}),
            {{ tags: ['Blue Coat', 'sonder:portrait'], errors: [] }},
        );
        assert.equal(model.normalizeReferenceTags(['sonder:unknown'], catalog, {{ strict: true }}).errors.length, 1);

        const imageDraft = model.createMemberDraft({{
            asset_id: 'image-1', tags: ['sonder:portrait'], prompt: '  calm  ',
            crop: {{ x: .1, y: .2, w: .5, h: .6 }}, source_start_sec: 0, source_end_sec: null,
        }}, {{ asset_id: 'image-1', asset_type: 'image' }});
        assert.deepEqual(imageDraft.crop, {{ x: 10, y: 20, w: 50, h: 60 }});
        assert.deepEqual(model.validateMemberDraft(imageDraft, catalog), []);
        assert.deepEqual(model.serializeMemberDraft(imageDraft, catalog).crop, {{ x: .1, y: .2, w: .5, h: .6 }});

        const audioDraft = model.createMemberDraft(null, {{ asset_id: 'audio-1', asset_type: 'audio' }});
        audioDraft.tags = ['sonder:voice_identity'];
        audioDraft.source_start_sec = '1.25';
        audioDraft.source_end_sec = '3.5';
        assert.deepEqual(model.validateMemberDraft(audioDraft, catalog), []);
        assert.deepEqual(model.serializeMemberDraft(audioDraft, catalog).source_end_sec, 3.5);
        audioDraft.tags = ['sonder:portrait'];
        assert.match(model.validateMemberDraft(audioDraft, catalog)[0], /does not accept audio/);

        const silentVideo = {{ asset_id: 'video-1', asset_type: 'video', has_audio: false }};
        const videoDraft = model.createMemberDraft(null, silentVideo);
        videoDraft.tags = ['sonder:portrait', 'sonder:voice_identity'];
        videoDraft.has_audio = false;
        assert.deepEqual(model.compatibleReferencePresets(catalog, silentVideo).map((item) => item.id), ['sonder:portrait', 'sonder:motion_reference', 'sonder:minimax_h3_identity']);
        assert.deepEqual(model.incompatibleReferencePresetTags(videoDraft.tags, catalog, silentVideo), ['sonder:voice_identity']);
        assert.match(model.validateMemberDraft(videoDraft, catalog, silentVideo, families)[0], /does not accept video/);
        const h3AudioDraft = model.createMemberDraft(null, {{ asset_id: 'audio-h3', asset_type: 'audio' }});
        h3AudioDraft.tags = ['sonder:minimax_h3_identity'];
        assert.equal(model.validateMemberDraft(h3AudioDraft, catalog, {{ asset_type: 'audio' }}, families)[0], 'MiniMax H3 · Identity does not accept audio assets.');
        const audioVideo = {{ ...silentVideo, asset_id: 'video-2', has_audio: true }};
        assert.equal(model.compatibleReferencePresets(catalog, audioVideo).some((item) => item.id === 'sonder:voice_identity'), true);

        const cropped = {{ ...videoDraft, crop: {{ x: 10, y: 10, w: 80, h: 80 }}, source_start_sec: 2, source_end_sec: 4 }};
        const toAudio = model.replaceMemberDraftAsset(cropped, {{ asset_id: 'audio-2', asset_type: 'audio' }});
        assert.equal(toAudio.draft.crop, null);
        assert.equal(toAudio.draft.source_start_sec, 2);
        assert.equal(toAudio.notices.length, 1);
        const toImage = model.replaceMemberDraftAsset(cropped, {{ asset_id: 'image-2', asset_type: 'image' }});
        assert.deepEqual(toImage.draft.crop, cropped.crop);
        assert.equal(toImage.draft.source_start_sec, 0);
        assert.equal(toImage.draft.source_end_sec, '');

        assert.deepEqual(model.moveReferenceCrop({{ x: 10, y: 10, w: 50, h: 50 }}, -20, 80), {{ x: 0, y: 50, w: 50, h: 50 }});
        assert.deepEqual(model.resizeReferenceCrop({{ x: 10, y: 10, w: 50, h: 50 }}, 'nw', -20, -20), {{ x: 0, y: 0, w: 60, h: 60 }});
        assert.deepEqual(model.normalizeReferenceTrim(4, null, 10), {{ start: 4, end: 10 }});
        assert.deepEqual(model.normalizeReferenceTrim(12, 2, 10), {{ start: 9.99, end: 10 }});
        assert.deepEqual(model.shiftReferenceTrim(2, 5, 10, 20), {{ start: 7, end: 10 }});
        assert.deepEqual(model.shiftReferenceTrim(2, 5, 10, -20), {{ start: 0, end: 3 }});
        assert.deepEqual(model.referenceMediaWindow('source', 2, 5, 10), {{ start: 0, end: 10, duration: 10 }});
        assert.deepEqual(model.referenceMediaWindow('result', 2, 5, 10), {{ start: 2, end: 5, duration: 3 }});
        assert.deepEqual(model.sliceReferenceWaveformPeaks([0,1,2,3,4,5,6,7,8,9], 2, 5, 10), [2,3,4]);

        const fittedWide = model.fitReferenceCropToAspect({{ x: 0, y: 0, w: 100, h: 100 }}, 100, 100, 16, 9);
        assert.equal(fittedWide.error, '');
        assert.equal(Math.round(fittedWide.crop.w), 100);
        assert.equal(Math.round(fittedWide.crop.h * 100) / 100, 56.25);
        const impossible = model.fitReferenceCropToAspect({{ x: 0, y: 0, w: 100, h: 100 }}, 1920, 1080, 1, 100000);
        assert.match(impossible.error, /minimum crop size/);
        const locked = model.resizeReferenceCropLocked(fittedWide.crop, 'e', -10, 0, 100, 100, 16, 9);
        assert.ok(Math.abs((locked.w / locked.h) - (16 / 9)) < 1e-9);
        const lockedSize = model.setReferenceCropLockedSize(fittedWide.crop, 'h', 40, 100, 100, 16, 9);
        assert.ok(Math.abs((lockedSize.w / lockedSize.h) - (16 / 9)) < 1e-9);

        const members = [
            {{ member_id: 'a', order: 7 }},
            {{ member_id: 'b', order: 2 }},
            {{ member_id: 'c', order: 9 }},
        ];
        assert.deepEqual(model.moveMember(members, 'b', -1).map((item) => [item.member_id, item.order]), [['b', 0], ['a', 1], ['c', 2]]);
        const references = [
            {{ name: 'Chloe', kind: 'character', description: '', members: [{{ asset_id: 'image-1', tags: ['Blue Coat', 'sonder:minimax_h3_identity'] }}] }},
            {{ name: 'Cafe', kind: 'location', description: 'night', members: [] }},
        ];
        const assets = [{{ asset_id: 'image-1', name: 'portrait.png' }}];
        assert.equal(model.filterReferences(references, 'portrait.png', assets)[0].name, 'Chloe');
        assert.equal(model.filterReferences(references, 'MiniMax H3 · Identity', assets, catalog, families)[0].name, 'Chloe');
        assert.equal(model.filterReferences(references, 'night', assets)[0].name, 'Cafe');

        assert.equal(model.shouldApplyReferenceResponse({{
            requestedProject: 'p1', currentProject: 'p1', requestGeneration: 4, currentGeneration: 4,
        }}), true);
        assert.equal(model.shouldApplyReferenceResponse({{
            requestedProject: 'p1', currentProject: 'p2', requestGeneration: 4, currentGeneration: 4,
        }}), false);
        assert.equal(model.shouldApplyReferenceResponse({{
            requestedProject: 'p1', currentProject: 'p1', requestGeneration: 3, currentGeneration: 4,
        }}), false);
    """)


def test_rendered_reference_tag_labels_are_unique_and_do_not_collide_with_roles():
    model = (ROOT / "web" / "js" / "reference_library_model.js").as_uri()
    catalog = json.dumps(list(REFERENCE_TAG_PRESETS))
    families = json.dumps(REFERENCE_TAG_FAMILIES)
    role_labels = json.dumps([
        str(role["label"])
        for roles in MINIMAX_H3_ROLE_CATALOGS.values()
        for role in roles
    ])
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ formatReferenceTag }} from {model!r};
        const catalog = {catalog};
        const families = {families};
        const roleLabels = {role_labels};
        const labels = catalog.map((entry) => formatReferenceTag(entry.id, {{ catalog, families }}));
        assert.equal(new Set(labels).size, labels.length);
        for (const label of labels) assert.equal(roleLabels.includes(label), false, label);
    """)


def test_reference_mutations_are_not_coalesced():
    queue = (ROOT / "web" / "js" / "project_mutation_queue.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ ProjectMutationQueue }} from {queue!r};
        const queue = new ProjectMutationQueue();
        const calls = [];
        const operations = ['create', 'update', 'reorder'];
        await Promise.all(operations.map((operation) => queue.enqueue({{
            key: 'references',
            coalesce: false,
            intent: operation,
            run: async (intent) => {{ calls.push(intent); return intent; }},
        }})));
        assert.deepEqual(calls, operations);
    """)


def test_reference_sidebar_refresh_and_settings_contracts_are_wired():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    controller = (ROOT / "web" / "js" / "editor_node_controller.js").read_text(encoding="utf-8")
    tab = (ROOT / "web" / "js" / "tab_entry.js").read_text(encoding="utf-8")
    settings = (ROOT / "web" / "js" / "editor_settings.js").read_text(encoding="utf-8")

    assert 'coalesce: false' in widget
    assert 'this._referenceLibraryHandle?.render?.();' in widget
    assert 'this._referenceLibraryHandle?.restoreScroll?.();' in widget
    assert 'wanted.has("references")' in widget
    assert '["project", "assets", "scenes", "queue", "references"]' in controller
    assert '["project", "assets", "scenes", "queue", "references"]' in tab
    assert 'fullscreenSidebarContent: "assets"' in settings
    assert settings.index("VALID_FULLSCREEN_SIDEBAR_CONTENT") < settings.index("let currentSettings")
    assert 'VALID_FULLSCREEN_SIDEBAR_CONTENT.has(stored?.layout?.fullscreenSidebarContent)' in settings
    assert 'referenceMediaViewMode: "source"' in settings
    assert settings.index("VALID_REFERENCE_MEDIA_VIEW_MODES") < settings.index("let currentSettings")
    assert 'VALID_REFERENCE_MEDIA_VIEW_MODES.has(stored?.inspector?.referenceMediaViewMode)' in settings


def test_fullscreen_queue_lifecycle_is_wired_to_enter_and_exit():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    assert "this._queueSection = queueSection;" in widget

    enter = widget.split("    _enterFullscreen() {", 1)[1].split(
        "    async _requestExitFullscreen", 1)[0]
    assert enter.index("this._fsSidebar.appendChild(this.galleryEl);") < enter.index(
        "this._showFullscreenSidebarContent") < enter.index("dockQueueInFullscreen(")

    exit_block = widget.split("    _exitFullscreen() {", 1)[1].split(
        "    _toggleFullscreen()", 1)[0]
    restore = "restoreQueueFromFullscreen(this._queueSection, this._queuePlacement);"
    assert restore in exit_block
    assert exit_block.index(restore) < exit_block.index(
        "this.container.insertBefore(this.galleryEl, this._galleryNextSibling || null);")


def test_fullscreen_queue_lifecycle_executes_twice_and_preserves_state():
    lifecycle = (ROOT / "web" / "js" / "fullscreen_queue_lifecycle.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ dockQueueInFullscreen, restoreQueueFromFullscreen }} from {lifecycle!r};

        class Element {{
            constructor(name) {{
                this.name = name;
                this.style = {{}};
                this.children = [];
                this.parentElement = null;
            }}
            get nextSibling() {{
                if (!this.parentElement) return null;
                const siblings = this.parentElement.children;
                return siblings[siblings.indexOf(this) + 1] || null;
            }}
            appendChild(child) {{
                if (child.parentElement) {{
                    const old = child.parentElement.children;
                    old.splice(old.indexOf(child), 1);
                }}
                this.children.push(child);
                child.parentElement = this;
                return child;
            }}
            insertBefore(child, sibling) {{
                if (sibling && sibling.parentElement !== this) throw new Error('wrong parent');
                if (child.parentElement) {{
                    const old = child.parentElement.children;
                    old.splice(old.indexOf(child), 1);
                }}
                const index = sibling ? this.children.indexOf(sibling) : this.children.length;
                this.children.splice(index, 0, child);
                child.parentElement = this;
                return child;
            }}
        }}

        const gallery = new Element('gallery');
        const assetGrid = new Element('assets');
        const queue = new Element('queue');
        const queueBody = new Element('queue-body');
        queueBody.style.maxHeight = '180px';
        queue.appendChild(queueBody);
        gallery.appendChild(assetGrid);
        gallery.appendChild(queue);
        const sidebar = new Element('sidebar');
        const references = new Element('references');
        sidebar.appendChild(references);

        for (let cycle = 0; cycle < 2; cycle += 1) {{
            const placement = dockQueueInFullscreen(queue, sidebar);
            assert.equal(queue.parentElement, sidebar);
            assert.equal(queue.style.flex, '0 0 auto');
            assert.equal(queueBody.style.maxHeight, '180px');
            // References hides the gallery wholesale; the queue remains a
            // visible sibling and retains the expansion state on its child.
            gallery.style.display = 'none';
            references.style.display = 'flex';
            assert.notEqual(queue.parentElement.style.display, 'none');
            assert.equal(queueBody.style.maxHeight, '180px');
            // Exercise the stale-sibling recovery on the second cycle.
            if (cycle === 1) placement.nextSibling = new Element('stale');
            assert.equal(restoreQueueFromFullscreen(queue, placement), true);
            assert.equal(queue.parentElement, gallery);
            assert.equal(queue.style.flex, '');
            assert.deepEqual(gallery.children.map((child) => child.name), ['assets', 'queue']);
            gallery.style.display = '';
        }}
    """)


def test_reference_library_scroll_and_context_menu_pointer_lifecycle():
    library = (ROOT / "web" / "js" / "editor_reference_library.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';

        class Element {{
            constructor(tag) {{
                this.tag = tag;
                this.style = {{}};
                this.dataset = {{}};
                this.attributes = {{}};
                this.children = [];
                this.parentElement = null;
                this.listeners = new Map();
                this.scrollTop = 0;
                this.value = '';
                this.textContent = '';
                this._visible = true;
            }}
            set innerHTML(value) {{
                for (const child of this.children) child.parentElement = null;
                this.children = [];
            }}
            get innerHTML() {{ return ''; }}
            append(...items) {{ for (const item of items) this.appendChild(item); }}
            appendChild(child) {{
                this.children.push(child);
                child.parentElement = this;
                return child;
            }}
            setAttribute(key, value) {{ this.attributes[key] = String(value); }}
            addEventListener(key, fn) {{
                if (!this.listeners.has(key)) this.listeners.set(key, []);
                this.listeners.get(key).push(fn);
            }}
            emit(key, event = {{}}) {{
                for (const fn of this.listeners.get(key) || []) fn({{
                    preventDefault() {{}}, stopPropagation() {{}}, ...event,
                }});
            }}
            get isConnected() {{ return this === document.body || !!this.parentElement?.isConnected; }}
            contains(node) {{ return node === this || this.children.some(child => child.contains(node)); }}
            removeEventListener(key, fn) {{
                this.listeners.set(key, (this.listeners.get(key) || []).filter(item => item !== fn));
            }}
            getClientRects() {{ return this._visible ? [{{ width: 300 }}] : []; }}
            focus() {{}}
            setSelectionRange() {{}}
            remove() {{
                if (!this.parentElement) return;
                const siblings = this.parentElement.children;
                siblings.splice(siblings.indexOf(this), 1);
                this.parentElement = null;
            }}
            querySelector(selector) {{ return this.querySelectorAll(selector)[0] || null; }}
            querySelectorAll(selector) {{
                const match = (node) => {{
                    if (selector === '[data-reference-library-body="true"]')
                        return node.dataset.referenceLibraryBody === 'true';
                    if (selector === '[data-reference-search="true"]')
                        return node.dataset.referenceSearch === 'true';
                    return node.tag === selector;
                }};
                const found = [];
                const visit = (node) => {{
                    for (const child of node.children || []) {{
                        if (match(child)) found.push(child);
                        visit(child);
                    }}
                }};
                visit(this);
                return found;
            }}
        }}

        globalThis.document = new Element('document');
        document.createElement = tag => new Element(tag);
        document.body = new Element('body');
        globalThis.window = new Element('window');
        globalThis.requestAnimationFrame = () => 0;
        const {{ mountReferenceLibrary }} = await import({library!r});
        const container = new Element('container');
        document.body.appendChild(container);
        const reference = (id, name) => ({{
            reference_id: id, name, kind: 'character', reference_class: 'subject',
            description: '', members: [],
        }});
        const data = {{
            projectKey: 'project-a', references: [reference('a', 'Alpha'),
                reference('b', 'Beta')], assets: [], scenes: [], semanticUnits: [],
            catalog: [], loading: false, error: '',
        }};
        const timelineAdds = [];
        const host = {{
            addToTimeline: payload => timelineAdds.push(payload),
            getData: () => data,
            mutate: async (operations) => {{
                for (const operation of operations) {{
                    if (operation.type !== 'update_member') continue;
                    const target = data.references.find((entry) =>
                        entry.reference_id === operation.reference_id)?.members.find(
                            (entry) => entry.member_id === operation.member_id);
                    if (target) Object.assign(target, operation.fields);
                }}
            }},
            confirm: () => true,
            pickAsset() {{}},
            assetPreviewUrl: () => null,
        }};
        const mounted = mountReferenceLibrary(container, host);
        const body = () => container.querySelector('[data-reference-library-body="true"]');
        const toolbar = () => container.children[0];

        body().scrollTop = 73;
        data.references.push(reference('c', 'Gamma'));
        mounted.render();
        assert.equal(body().scrollTop, 73, 'same project/list keeps scroll');

        const search = toolbar().children[0];
        search.value = 'Beta';
        search.selectionStart = 4;
        search.emit('input');
        assert.equal(body().scrollTop, 0, 'query change rejects stale offset');

        body().scrollTop = 41;
        body().emit('scroll');
        container._visible = false;
        mounted.render();
        assert.equal(body().scrollTop, 0, 'hidden render does not restore');
        container._visible = true;
        assert.equal(mounted.restoreScroll(), true);
        assert.equal(body().scrollTop, 41, 'reveal restores without requiring a render');

        body().scrollTop = 47;
        body().emit('scroll');
        data.loading = true;
        mounted.render();
        assert.equal(body().dataset.referenceListView, undefined);
        data.loading = false;
        mounted.render();
        assert.equal(body().scrollTop, 47, 'loading did not discard outgoing scroll');

        body().scrollTop = 29;
        toolbar().children[1].emit('click');
        assert.equal(body().scrollTop, 0, 'Manage mode change rejects stale offset');

        body().scrollTop = 55;
        mounted.render();
        assert.equal(body().scrollTop, 55);
        data.projectKey = 'project-b';
        mounted.reset();
        assert.equal(body().scrollTop, 0, 'project change rejects stale offset');
        data.projectKey = 'project-a';
        mounted.reset();
        assert.equal(body().scrollTop, 0, 'returning to a project does not revive its old offset');

        body().scrollTop = 64;
        toolbar().children[2].emit('click');
        const cancel = container.querySelectorAll('button').find(
            (button) => button.textContent === 'Cancel');
        cancel.emit('click');
        assert.equal(body().scrollTop, 0, 'draft tree invalidates list offset');

        // A member editor is embedded in the list rather than replacing it.
        // Its own key must not overwrite the list key needed after Save.
        const resetSearch = toolbar().children[0];
        resetSearch.value = '';
        resetSearch.selectionStart = 0;
        resetSearch.emit('input');
        if (toolbar().children[1].attributes['aria-pressed'] === 'true') {{
            toolbar().children[1].emit('click');
        }}
        const member = {{ member_id: 'm1', asset_id: 'image-1', name: 'Front',
            tags: [], prompt: '', crop: null, source_start_sec: 0,
            source_end_sec: null, order: 0 }};
        data.references = [{{ ...reference('member-ref', 'Member Ref'), members: [member] }}];
        data.assets = [{{ asset_id: 'image-1', asset_type: 'image', name: 'front.png' }}];
        data.projectKey = 'project-member';
        mounted.render();
        const section = container.querySelector('section');
        section.children[0].emit('click');
        body().scrollTop = 88;
        body().emit('scroll');
        const edit = container.querySelectorAll('button').find(
            (button) => button.textContent === 'Edit');
        edit.emit('click');
        const memberName = container.querySelectorAll('input').find(
            (input) => input.attributes['aria-label'] === 'Member name');
        memberName.value = 'Portrait';
        memberName.emit('input');
        const save = container.querySelectorAll('button').find(
            (button) => button.textContent === 'Save');
        save.emit('click');
        await new Promise((resolve) => setTimeout(resolve, 20));
        assert.equal(body().scrollTop, 88, 'member save returns to list offset');
        const all = node => [node, ...node.children.flatMap(all)];
        const menuRow = () => all(document.body).find(node =>
            node.attributes.role === 'menuitem' || node.tag === 'button' && node.textContent === 'Add to timeline' && !container.contains(node));
        const targets = all(container).filter(node => node.listeners.has('contextmenu'));
        assert.equal(targets.length, 2, 'card header and single-member row');
        for (const target of targets) {{
            target.emit('contextmenu', {{clientX: 40, clientY: 50}});
            await new Promise(resolve => setTimeout(resolve, 20));
            const row = menuRow();
            assert.ok(row);
            document.emit('pointerdown', {{target: row}});
            document.emit('mousedown', {{target: row}});
            if (row.isConnected) row.emit('click');
            assert.equal(timelineAdds.length, targets.indexOf(target) + 1);
            assert.equal(menuRow(), undefined, 'action dismisses menu');
            assert.deepEqual(timelineAdds.at(-1).members, [{{entity_id:'member-ref', member_id:'m1'}}]);
        }}
        targets[1].emit('contextmenu');
        await new Promise(resolve => setTimeout(resolve, 20));
        window.emit('keydown', {{key:'Escape', stopImmediatePropagation() {{}}}});
        assert.equal(menuRow(), undefined, 'Escape dismisses canonical menu');
        targets[1].emit('contextmenu');
        await new Promise(resolve => setTimeout(resolve, 20));
        document.emit('mousedown', {{target: container}});
        assert.equal(menuRow(), undefined, 'outside press dismisses menu');
        assert.equal(timelineAdds.length, 2, 'dismissals never run the action');
        mounted.destroy();
    """)


def test_shared_gallery_renders_reference_usage_and_delete_semantics():
    source = (ROOT / "web" / "js" / "shared_asset_gallery.js").read_text(encoding="utf-8")
    assert 'reference_member: 0' in source
    assert 'if (type === "reference_member") return "Reference Member"' in source
    assert '["References", counts.reference_member]' in source
    assert 'Library memberships: ${counts.reference_member} (will be removed)' in source
    assert 'Timeline and queue references will stay in place as missing placeholders.' in source
    assert 'inspectAsset,' in source


def test_reference_picker_inspection_and_media_editor_are_wired():
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    library = (ROOT / "web" / "js" / "editor_reference_library.js").read_text(encoding="utf-8")
    media = (ROOT / "web" / "js" / "reference_media_editor.js").read_text(encoding="utf-8")
    assert 'onInspect = null' in widget
    assert 'onInspect(asset.asset_id);' in widget
    assert 'this._hideImagePicker();\n                        onPick(asset.asset_id);' in widget
    assert 'Inspect Source' in library
    assert 'Crop, Trim & Preview' in library
    assert 'Apply to Draft' in media
    assert 'source_end_sec: endIsRemainder ? "" : range.end' in media
    assert 'abortController.abort();' in media
    assert 'registerKeyboardConsumer' in media
    assert 'document.addEventListener("keydown"' not in media
    assert 'REFERENCE_MEDIA_EDITOR_SHORTCUTS' in media
    assert 'mountMediaScrubBar' in media
    assert 'ASPECT_RATIO_PRESETS' in media
    assert 'Play Selection' not in media
    assert 'Play Full Source' not in media
    assert 'media.controls = true' not in media
    assert 'referenceMediaViewMode' in widget
    assert 'Reference Media Editor' in widget
    assert 'shell.tabIndex = -1' in media
    assert 'overflow:hidden;outline:none;color:#e6ebf0' in media
    assert 'shell.focus?.({ preventScroll: true })' in media
    assert 'selectedRange.focus?.({ preventScroll: true })' in media
    assert 'event.preventDefault();\n        event.stopPropagation();' in media


def test_direct_inspection_scope_ignores_gallery_filters_without_admitting_artifacts():
    scope = (ROOT / "web" / "js" / "inspect_overlay_scope.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ resolveInspectOverlayScope }} from {scope!r};
        const image = {{ asset_id: 'i', asset_type: 'image', path: 'i.png' }};
        const audio = {{ asset_id: 'a', asset_type: 'audio', path: 'a.wav' }};
        const video = {{ asset_id: 'v', asset_type: 'video', path: 'v.mp4' }};
        const artifact = {{ asset_id: 'r', asset_type: 'artifact', path: 'run.json' }};
        const missing = {{ asset_id: 'm', asset_type: 'audio', path: 'm.wav', missing: true }};
        const trashed = {{ asset_id: 't', asset_type: 'video', path: 't.mp4', trashed_at: 'now' }};
        const all = [video, artifact, audio, missing, image, trashed];
        const visible = [image];
        assert.deepEqual(
            resolveInspectOverlayScope({{ origin: 'gallery', requestedAssetId: 'a', visibleAssets: visible, sortedProjectAssets: all }}).assets.map(x => x.asset_id),
            ['i'],
        );
        const direct = resolveInspectOverlayScope({{ origin: 'direct', requestedAssetId: 'a', visibleAssets: visible, sortedProjectAssets: all }});
        assert.equal(direct.asset, audio);
        assert.deepEqual(direct.assets.map(x => x.asset_id), ['v', 'a', 'i']);
        for (const id of ['i', 'a', 'v']) {{
            assert.equal(resolveInspectOverlayScope({{ origin: 'direct', requestedAssetId: id, visibleAssets: [], sortedProjectAssets: all }}).asset.asset_id, id);
        }}
    """)


def test_shared_media_scrub_bar_maps_active_window_and_cleans_up():
    scrub = (ROOT / "web" / "js" / "media_scrub_bar.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        class FakeElement {{
            constructor(tag) {{ this.tag = tag; this.style = {{}}; this.dataset = {{}}; this.children = []; this.listeners = new Map(); this.attrs = {{}}; }}
            append(...items) {{ this.children.push(...items); }}
            appendChild(item) {{ this.children.push(item); return item; }}
            setAttribute(k, v) {{ this.attrs[k] = v; }}
            addEventListener(k, fn) {{ if (!this.listeners.has(k)) this.listeners.set(k, new Set()); this.listeners.get(k).add(fn); }}
            removeEventListener(k, fn) {{ this.listeners.get(k)?.delete(fn); }}
            getBoundingClientRect() {{ return {{ left: 0, width: 100 }}; }}
            setPointerCapture() {{}}
            releasePointerCapture() {{}}
            emit(k, event = {{}}) {{ for (const fn of this.listeners.get(k) || []) fn({{ pointerId: 1, clientX: 0, preventDefault() {{}}, ...event }}); }}
        }}
        globalThis.document = {{ createElement: (tag) => new FakeElement(tag) }};
        const mediaListeners = new Map();
        const media = {{
            duration: 10, currentTime: 0,
            addEventListener(k, fn) {{ if (!mediaListeners.has(k)) mediaListeners.set(k, new Set()); mediaListeners.get(k).add(fn); }},
            removeEventListener(k, fn) {{ mediaListeners.get(k)?.delete(fn); }},
        }};
        const mod = await import({scrub!r});
        assert.deepEqual(mod.normalizeMediaWindow(2, 6, 10), {{ start: 2, end: 6, duration: 4 }});
        assert.equal(mod.mediaWindowTimeFromRatio(.5, {{ start: 2, end: 6, duration: 4 }}), 4);
        const mounted = mod.mountMediaScrubBar(media);
        mounted.setWindow({{ startSec: 2, endSec: 6, relative: true }});
        const track = mounted.el.children[0];
        track.emit('pointerdown', {{ clientX: 50 }});
        assert.equal(media.currentTime, 4);
        assert.match(mounted.el.children[1].textContent, /source/);
        mounted.cleanup();
        assert.equal(track.listeners.get('pointerdown').size, 0);
        assert.equal(mediaListeners.get('timeupdate').size, 0);
    """)
