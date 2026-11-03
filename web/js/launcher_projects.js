import { el, button, component, input, date, badge, listState, listNotice } from './launcher_view.js';
// The projects pane's line before its tiles: loading, couldn't load, or the empty
// state, which only shows once the list has really loaded with nothing in it.
export function projectsNotice(state) {
    const list = listState(state, 'projects');
    if (list === 'loading') return { text: 'Loading projects…', failed: false };
    if (list === 'slow') return { text: 'Still loading projects. ComfyUI may be busy, and a large projects folder can take a while.', failed: false };
    if (list === 'failed') return { text: 'Projects couldn’t load.', failed: true };
    return !state.projects.length && !state.projectData.unreadable.length ? { text: 'Start your first project', failed: false } : null;
}
export function visibleProjects(state) {
    const query = state.projectSearch.toLowerCase();
    return [...state.projects, ...state.projectData.unreadable.map(p => ({ ...p, name: p.folder, unreadable: true }))]
        .filter(p => (state.prefs.showHidden || !p.hidden) && `${p.name} ${p.folder}`.toLowerCase().includes(query))
        .sort((a, b) => {
            const key = state.prefs.view === 'list' ? state.sort.key : 'modified_at';
            const av = key === 'resolution' ? (a.resolution || []).join('×') : a[key];
            const bv = key === 'resolution' ? (b.resolution || []).join('×') : b[key];
            const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av || '').localeCompare(String(bv || ''), undefined, { numeric: true });
            return cmp * (state.prefs.view === 'list' ? state.sort.direction : -1) || a.folder.localeCompare(b.folder);
        });
}
// What a tile shows: Off nothing, Still the poster, Animated the poster plus a clip on
// hover. Reduced motion turns Animated into Still.
export function thumbnailMode(prefs, reducedMotion = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) {
    const mode = prefs?.thumbnails || 'animated';
    return mode === 'animated' && reducedMotion ? 'still' : mode;
}
// Fill a poster with the project's published thumbnail, or the placeholder mark.
function paintPoster(poster, p, state, actions) {
    const shown = thumbnailMode(state.prefs) === 'off' ? '' : state.thumbnails?.[p.folder]?.shown || '';
    if (poster.dataset.shown === shown && poster.firstChild) return;
    poster.dataset.shown = shown; poster.querySelectorAll('img.sl-thumb,.sl-placeholder').forEach(n => n.remove());
    let media;
    if (shown) { media = el('img', 'sl-thumb'); media.src = actions.thumbnailURL(p.folder, shown, 'jpg'); media.loading = 'lazy'; }
    else { media = el('img', 'sl-placeholder'); media.src = 'static/assets/sonder-mark-small.svg'; }
    media.alt = ''; media.draggable = false; poster.prepend(media);
}
function hoverClip(poster, p, state, actions) {
    let video = null;
    const enter = () => {
        const shown = poster.dataset.shown;
        if (!shown || thumbnailMode(state.prefs) !== 'animated') return;
        if (!video || video.dataset.key !== shown) {
            video?.remove(); video = el('video', 'sl-thumb'); video.dataset.key = shown;
            Object.assign(video, { muted: true, loop: true, playsInline: true, preload: 'none' });
            video.setAttribute('muted', ''); video.setAttribute('playsinline', ''); video.src = actions.thumbnailURL(p.folder, shown, 'mp4');
            poster.querySelector('.sl-project-badges')?.before(video);
        }
        video.hidden = false; void video.play().catch(() => {});
    };
    const leave = () => { if (video) { video.pause(); video.hidden = true; } };
    poster.addEventListener('mouseenter', enter); poster.addEventListener('mouseleave', leave);
    return () => { leave(); video?.removeAttribute('src'); video?.load?.(); };
}
export function mountProjects(state, actions) {
    const section = el('section', 'sl-projects'), toolbar = el('div', 'sl-project-toolbar');
    toolbar.append(el('h2', 'sl-title', 'Projects'), input('Search projects', state.projectSearch, actions.searchProjects, 'project-search'));
    for (const view of ['grid', 'list']) {
        const b = button(view === 'grid' ? '▦' : '☷', () => actions.preference({ view }), { title: `${view === 'grid' ? 'Grid' : 'List'} view` });
        b.setAttribute('aria-pressed', String(state.prefs.view === view)); toolbar.append(b);
    }
    const sizes = el('div', 'sl-row'); sizes.setAttribute('aria-label', 'Tile size');
    for (const tileSize of ['S', 'M', 'L']) {
        const b = button(tileSize, () => actions.preference({ tileSize }), { title: `${tileSize} tiles` });
        b.setAttribute('aria-pressed', String(state.prefs.tileSize === tileSize)); sizes.append(b);
    }
    toolbar.append(sizes); section.append(toolbar);
    const scroll = el('div', 'sl-project-scroll'); scroll.dataset.scrollKey = 'projects';
    const rows = visibleProjects(state), counts = new Map(), posters = new Map(), disposers = [];
    for (const p of state.projects) counts.set(p.name, (counts.get(p.name) || 0) + 1);
    const wire = (node, p) => {
        node.tabIndex = 0; node.dataset.projectFolder = p.folder; node.dataset.focusKey = `project:${p.folder}`;
        node.classList.toggle('selected', state.selectedProject === p.folder); node.classList.toggle('hidden', !!p.hidden);
        node.setAttribute('aria-selected', String(state.selectedProject === p.folder));
        node.addEventListener('click', () => actions.selectProject(p.folder));
        node.addEventListener('dblclick', () => actions.openProject(p.folder));
        node.addEventListener('contextmenu', e => { e.preventDefault(); actions.projectMenu(p, e); });
        node.addEventListener('keydown', e => {
            if (e.isComposing || e.keyCode === 229) return;
            if (e.key === ' ') { e.preventDefault(); actions.selectProject(p.folder); }
            if (e.key.startsWith('Arrow')) {
                let step = e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1;
                if (state.prefs.view === 'grid' && ['ArrowUp', 'ArrowDown'].includes(e.key)) step *= getComputedStyle(node.parentElement).gridTemplateColumns.split(' ').length;
                const next = rows[Math.max(0, Math.min(rows.length - 1, rows.indexOf(p) + step))];
                if (next) { e.preventDefault(); actions.selectProject(next.folder, true); }
            }
        });
    };
    const notice = projectsNotice(state);
    if (notice) scroll.append(listNotice(notice.text, notice.failed, actions.checkAgain));
    if (state.prefs.view === 'grid') {
        const grid = el('div', 'sl-grid'); grid.style.setProperty('--tile-min', `${{ S: 160, M: 220, L: 300 }[state.prefs.tileSize]}px`);
        const create = el('div', 'sl-project sl-new'); create.tabIndex = 0; create.setAttribute('role', 'button'); create.setAttribute('aria-label', 'New project');
        const poster = el('div', 'sl-poster'); poster.append(el('span', 'sl-plus', '+'), el('span', '', 'New project')); create.append(poster);
        create.addEventListener('click', actions.create); create.addEventListener('keydown', e => { if (['Enter', ' '].includes(e.key) && !e.isComposing) { e.preventDefault(); actions.create(); } });
        grid.append(create);
        for (const p of rows) {
            const tile = el('div', 'sl-project'); tile.setAttribute('role', 'option'); tile.title = p.folder; wire(tile, p);
            const poster = el('div', 'sl-poster');
            if (p.unreadable || p.too_new) poster.append(el('span', 'sl-dim', p.unreadable ? 'Can’t be read' : '🔒 Newer project format'));
            else { paintPoster(poster, p, state, actions); posters.set(p.folder, { poster, p }); disposers.push(hoverClip(poster, p, state, actions)); }
            const badges = el('div', 'sl-project-badges');
            if (p.linked) badges.append(badge('Linked'));
            if (p.hidden) badges.append(badge('Hidden'));
            poster.append(badges);
            const caption = el('div', 'sl-project-meta'); caption.append(el('div', 'sl-project-name', p.name));
            caption.append(el('div', '', p.unreadable ? 'Can’t be read' : `${p.scene_count} scenes · ${date(p.modified_at)}`));
            if (counts.get(p.name) > 1) caption.append(el('div', 'sl-folder', p.folder));
            tile.append(poster, caption); grid.append(tile);
        }
        scroll.append(grid);
    } else {
        scroll.append(button('+ New project', actions.create));
        const table = el('table', 'sl-table'), head = el('thead'), tr = el('tr');
        const posterHead = el('th'); posterHead.setAttribute('aria-label', 'Poster'); tr.append(posterHead);
        for (const [label, key] of [['Name', 'name'], ['Scenes', 'scene_count'], ['Resolution', 'resolution'], ['fps', 'fps'], ['Last edited', 'modified_at'], ['Folder', 'folder']]) {
            const th = el('th'); th.setAttribute('aria-sort', state.sort.key === key ? (state.sort.direction === 1 ? 'ascending' : 'descending') : 'none');
            th.append(button(label + (state.sort.key === key ? (state.sort.direction === 1 ? ' ↑' : ' ↓') : ''), () => actions.sort(key))); tr.append(th);
        }
        head.append(tr); table.append(head); const body = el('tbody');
        for (const p of rows) {
            const row = el('tr'); wire(row, p);
            const cell = el('td', 'sl-list-poster'), poster = el('div', 'sl-poster');
            if (!p.unreadable && !p.too_new) { paintPoster(poster, p, state, actions); posters.set(p.folder, { poster, p }); }
            cell.append(poster); row.append(cell);
            for (const value of [p.name + (p.linked ? ' · Linked' : '') + (p.too_new ? ' · 🔒' : '') + (p.unreadable ? ' · Can’t be read' : ''), p.scene_count ?? '', (p.resolution || []).join('×'), p.fps || '', date(p.modified_at), p.folder]) row.append(el('td', '', String(value)));
            body.append(row);
        }
        table.append(body); scroll.append(table);
    }
    if (state.projectSearch && !rows.length && listState(state, 'projects') === 'ready') {
        const empty = el('div', 'sl-empty', 'No projects match. '); empty.append(button('Clear search', () => actions.searchProjects(''))); scroll.append(empty);
    }
    const hidden = [...state.projects, ...state.projectData.unreadable].filter(p => p.hidden).length;
    if (hidden) { const note = el('div', 'sl-footer-notice', `${hidden} hidden projects`); note.append(button(state.prefs.showHidden ? 'Hide hidden' : 'Show hidden', () => actions.preference({ showHidden: !state.prefs.showHidden }))); scroll.append(note); }
    if (state.projectData.linked_hidden_count) {
        const note = el('div', 'sl-footer-notice', `${state.projectData.linked_hidden_count} linked projects are hidden because external links are off.`);
        if (state.projectData.can_manage) note.append(button('Turn on…', actions.enableLinks));
        scroll.append(note);
    }
    section.append(scroll);
    // Ask the host for thumbnails of the tiles in view only (never with Thumbnails off).
    let observer = null;
    if (thumbnailMode(state.prefs) !== 'off' && posters.size && globalThis.IntersectionObserver) {
        const seen = new Set();
        observer = new IntersectionObserver(entries => {
            for (const entry of entries) { const folder = entry.target.closest('[data-project-folder]')?.dataset.projectFolder; if (folder) entry.isIntersecting ? seen.add(folder) : seen.delete(folder); }
            actions.thumbnailsVisible([...seen]);
        }, { root: scroll, rootMargin: '200px 0px' });
        for (const { poster } of posters.values()) observer.observe(poster);
    }
    const view = component(section, () => { observer?.disconnect(); for (const dispose of disposers) dispose(); });
    // Update selection and posters in place, so polling never rebuilds the grid.
    view.sync = next => {
        for (const node of section.querySelectorAll('[data-project-folder]')) {
            const selected = node.dataset.projectFolder === next.selectedProject;
            node.classList.toggle('selected', selected); node.setAttribute('aria-selected', String(selected));
        }
        for (const { poster, p } of posters.values()) paintPoster(poster, p, next, actions);
    };
    return view;
}
