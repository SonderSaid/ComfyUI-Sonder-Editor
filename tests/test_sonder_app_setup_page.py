"""docs/sonder-app-setup.md lists what each Sonder App starter needs.

The starter graph is the authority: every model file it loads, in the folder the
Launcher would show (model_requirements' own rule). The page's tables and both
copies of the Controls notes (the App starter and its canvas example) are checked
against it, so neither can go stale on its own. Sizes and Hugging Face access are
online facts the page dates in a comment; they aren't tested.
"""
import json
import re
from pathlib import Path

import pytest

from server import model_requirements as mr
from starter_choice_files import choice_files, starter_menu

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'docs' / 'sonder-app-setup.md'
STARTERS = {
    'sonder_ltx_2_5_edit_vfx': 'ltx_models_downloads',
    'sonder_ltx_2_5_refine_upscale': 'refine_models',
    'sonder_minimax_h3_references_swaps': 'h3_models',
}
# In the notes and on the page, but not loaded by the graph: a file you may
# choose instead in a model Control. The page marks it "Optional".
ALTERNATIVES = {
    'sonder_minimax_h3_references_swaps': {'minimax_h3_fl2va_pruned_int8_convrot.safetensors'},
}
LTX = {'sonder_ltx_2_5_edit_vfx', 'sonder_ltx_2_5_refine_upscale'}
LINK = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
REPO_LINK = re.compile(r'\]\((https://github\.com/[^/)\s]+/[^/)\s#]+)\)')


def graph_files(slug):
    """file name -> folder under ComfyUI/, as the Launcher places it."""
    api, _, _ = starter_menu(slug)
    files = {}
    for key, node in api.items():
        for name, value in node.get('inputs', {}).items():
            if not mr._is_model_value(node['class_type'], name, value):
                continue
            if mr.names_an_off_row(api, {'node_id': key, 'input': name}):
                continue
            folder = mr._folder(name, node['class_type'], ())
            assert folder, (slug, key, name)
            path = mr._normal(value)
            sub = path.rsplit('/', 1)[0] + '/' if '/' in path else ''
            files[path.rsplit('/', 1)[-1]] = f'models/{folder}/{sub}'
    return files


def page_section(slug):
    _, meta, _ = starter_menu(slug)
    text = PAGE.read_text(encoding='utf-8')
    parts = re.split(r'^## ', text, flags=re.M)
    section = next((p for p in parts if p.split('\n', 1)[0].strip() == meta['name']), None)
    assert section is not None, f"no '## {meta['name']}' section on the page"
    return section


def table(section, first_header):
    lines = section.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f'| {first_header} |'))
    rows = []
    for line in lines[start + 2:]:
        if not line.startswith('|'):
            break
        rows.append([cell.strip() for cell in line.strip().strip('|').split('|')])
    return rows


def page_models(slug):
    """file name -> (url, folder, size, access, needed by)."""
    out = {}
    for file_cell, folder, size, access, needed in table(page_section(slug), 'File'):
        name, url = LINK.fullmatch(file_cell).groups()
        assert name not in out, (slug, name)
        out[name] = (url, folder.strip('`'), size, access, needed)
    return out


def note_files(text):
    """file name -> (url, folder token) from a models note: the folder is on the
    file's own bullet (`→ **folder/**`) or in the heading above it (`put these in
    folder/`)."""
    out, heading_folder = {}, None
    for line in text.split('\n'):
        heading = re.search(r'put (?:them|these) in \**([A-Za-z0-9_]+(?:/[A-Za-z0-9_ .-]+)*/)', line)
        if heading:
            heading_folder = heading.group(1)
        for name, url in LINK.findall(line):
            if not name.endswith('.safetensors'):
                continue
            arrow = re.search(r'\]\(' + re.escape(url) + r'\)\s*→\s*\*\*([^*]+/)\*\*', line)
            out[name] = (url, arrow.group(1) if arrow else heading_folder)
    return out


def canvas_menu(slug):
    canvas = json.loads((ROOT / 'example_workflows' / (slug + '.json')).read_text(encoding='utf-8'))
    relay = next(n for n in canvas['nodes'] if n.get('type') == 'SonderControlRelay')
    return json.loads(relay['widgets_values'][0])


def expected_needed_by(slug, files):
    _, _, menu = starter_menu(slug)
    rows = [(c['name'], [menu['presets'][cid]['name'] for cid in c['choices']])
            for c in sorted(menu['preset_categories'].values(), key=lambda c: c.get('order', 0))]
    need = {(row, choice): choice_files(slug, choice) for row, choices in rows for choice in choices}
    out = {}
    for name in files:
        parts, every = [], True
        for row, choices in rows:
            using = [choice for choice in choices if name in need[(row, choice)]]
            if len(using) < len(choices):
                every = False
            if 0 < len(using) < len(choices):
                parts.append(f"{row} → {', '.join(using)}")
        out[name] = 'Every choice' if every else '; '.join(parts)
    return out


@pytest.mark.parametrize('slug', sorted(STARTERS))
def test_page_lists_exactly_the_graphs_files_in_the_launchers_folders(slug):
    graph, page = graph_files(slug), page_models(slug)
    alternatives = ALTERNATIVES.get(slug, set())
    assert set(graph) <= set(page), set(graph) - set(page)
    assert set(page) - set(graph) == alternatives
    for name, folder in graph.items():
        assert page[name][1] == folder, (name, page[name][1], folder)
    for name in alternatives:
        assert page[name][4] == 'Optional', name


@pytest.mark.parametrize('slug', sorted(STARTERS))
def test_app_and_canvas_notes_match_the_page(slug):
    _, _, menu = starter_menu(slug)
    page = page_models(slug)
    app = note_files(menu['notes'][STARTERS[slug]]['text'])
    canvas = note_files(canvas_menu(slug)['notes'][STARTERS[slug]]['text'])
    assert app == canvas
    assert set(app) == set(page)
    for name, (url, folder) in app.items():
        assert url == page[name][0], name
        assert folder is not None and 'models/' + folder == page[name][1], (name, folder, page[name][1])


@pytest.mark.parametrize('slug', sorted(STARTERS))
def test_needed_by_names_the_choices_that_load_each_file(slug):
    graph, page = graph_files(slug), page_models(slug)
    expected = expected_needed_by(slug, graph)
    for name in graph:
        assert page[name][4] == expected[name], (name, page[name][4], expected[name])


@pytest.mark.parametrize('slug', sorted(STARTERS))
def test_access_names_the_files_own_repository(slug):
    for name, (url, _, size, access, _) in page_models(slug).items():
        assert re.fullmatch(r'\d+(\.\d)? (GB|MB)', size), (name, size)
        if access == 'Open':
            continue
        repo_name, repo_url = LINK.fullmatch(access).groups()
        assert repo_url == f'https://huggingface.co/{repo_name}', access
        assert url.startswith(repo_url + '/blob/'), (name, url, repo_url)


@pytest.mark.parametrize('slug', sorted(STARTERS))
def test_pack_links_match_the_notes(slug):
    _, _, menu = starter_menu(slug)
    notes = '\n'.join(note['text'] for note in menu['notes'].values())
    named = set(REPO_LINK.findall(notes)) - {'https://github.com/SonderSaid/ComfyUI-Sonder-Editor'}
    packs = {LINK.match(row[0]).group(2) for row in table(page_section(slug), 'Pack')}
    assert packs == named
    canvas_notes = '\n'.join(note['text'] for note in canvas_menu(slug)['notes'].values())
    assert named <= set(REPO_LINK.findall(canvas_notes))
    res4lyf = 'Install RES4LYF even when the Launcher reads **Ready**' in page_section(slug)
    assert res4lyf == (slug in LTX)
