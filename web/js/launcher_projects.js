import { el, button, component, input, date, badge } from './launcher_view.js';
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
    const rows = visibleProjects(state), counts = new Map();
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
    if (!state.projects.length && !state.projectData.unreadable.length) scroll.append(el('div', 'sl-empty', 'Start your first project'));
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
            else { const mark = el('img', 'sl-placeholder'); mark.src = 'static/assets/sonder-mark-small.svg'; mark.alt = ''; poster.append(mark); }
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
        for (const [label, key] of [['Name', 'name'], ['Scenes', 'scene_count'], ['Resolution', 'resolution'], ['fps', 'fps'], ['Last edited', 'modified_at'], ['Folder', 'folder']]) {
            const th = el('th'); th.setAttribute('aria-sort', state.sort.key === key ? (state.sort.direction === 1 ? 'ascending' : 'descending') : 'none');
            th.append(button(label + (state.sort.key === key ? (state.sort.direction === 1 ? ' ↑' : ' ↓') : ''), () => actions.sort(key))); tr.append(th);
        }
        head.append(tr); table.append(head); const body = el('tbody');
        for (const p of rows) {
            const row = el('tr'); wire(row, p);
            for (const value of [p.name + (p.linked ? ' · Linked' : '') + (p.too_new ? ' · 🔒' : '') + (p.unreadable ? ' · Can’t be read' : ''), p.scene_count ?? '', (p.resolution || []).join('×'), p.fps || '', date(p.modified_at), p.folder]) row.append(el('td', '', String(value)));
            body.append(row);
        }
        table.append(body); scroll.append(table);
    }
    if (state.projectSearch && !rows.length) {
        const empty = el('div', 'sl-empty', 'No projects match. '); empty.append(button('Clear search', () => actions.searchProjects(''))); scroll.append(empty);
    }
    const hidden = [...state.projects, ...state.projectData.unreadable].filter(p => p.hidden).length;
    if (hidden) { const note = el('div', 'sl-footer-notice', `${hidden} hidden projects`); note.append(button(state.prefs.showHidden ? 'Hide hidden' : 'Show hidden', () => actions.preference({ showHidden: !state.prefs.showHidden }))); scroll.append(note); }
    if (state.projectData.linked_hidden_count) {
        const note = el('div', 'sl-footer-notice', `${state.projectData.linked_hidden_count} linked projects are hidden because external links are off.`);
        if (state.projectData.can_manage) note.append(button('Turn on…', actions.enableLinks));
        scroll.append(note);
    }
    section.append(scroll); return component(section);
}
