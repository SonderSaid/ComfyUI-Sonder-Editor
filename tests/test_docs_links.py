"""Every link into this repository's docs lands, checked locally (0.8 hardening,
Phase 12B, B4).

Covers the README, docs/ and CHANGELOG, and the GitHub URLs shipped inside the
pack: workflow notes and descriptions (starters and canvas examples), the web UI,
server and node tooltips, and pyproject.toml. Each URL maps to its file here, and
an anchor must be a heading's slug (GitHub's rule, below) or an explicit
<a id>. The real render on GitHub is checked after a release; this test is what
can run before one.
"""
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = 'SonderSaid/ComfyUI-Sonder-Editor'
DOCS = [ROOT / 'README.md', ROOT / 'CHANGELOG.md', *sorted((ROOT / 'docs').glob('*.md'))]
SHIPPED = [*sorted((ROOT / 'sdk_starters').glob('*.json')), *sorted((ROOT / 'example_workflows').glob('*.json')),
           *sorted((ROOT / 'web').rglob('*.js')), *sorted((ROOT / 'web').rglob('*.html')),
           *sorted((ROOT / 'server').rglob('*.py')), *sorted((ROOT / 'nodes').rglob('*.py')), ROOT / 'pyproject.toml']
# A URL inside a JSON string, Markdown or code: it ends at whitespace, a quote, a
# closing bracket or an escape, so an escaped JSON manifest needs no decoding.
REPO_URL = re.compile(r'https?://(?:github\.com|raw\.githubusercontent\.com)/' + re.escape(REPO) + r'[^\s"\'`)\]\\<>]*')
MD_LINK = re.compile(r'!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
HTML_LINK = re.compile(r'<(?:a|img)\b[^>]*?\b(?:href|src)="([^"]+)"')
HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
EXPLICIT = re.compile(r'<a\s+(?:id|name)="([^"]+)"')

# Anchors and paths that outlive any one link to them (the docs guidelines' anchor
# rule): installed copies of older versions and the Registry listing still point here.
PUBLISHED = [
    'docs/troubleshooting.md#the-launcher-says-missing-node-packs',  # launcher_workflows.js
    'README.md#documentation',  # pyproject.toml, the H3 workflow note
    'docs/images/sonder-editor-registry-icon.png',  # pyproject.toml
    'docs/references.md',  # the H3 workflow note
    'docs/generating.md',  # nodes/masks_bridge.py tooltip
    'docs/sonder-app-setup.md',  # web/js/docs_links.js SETUP_GUIDE_URL
    # Old anchors kept by <a id> (roadmap: Docs — public render and anchors).
    'README.md#quickstart', 'README.md#the-node-in-your-graph', 'README.md#example-workflows', 'README.md#example-projects',
    *[f'docs/getting-started.md#{n}' for n in (
        '1-install-the-pack', '2-add-the-node-and-create-a-project', '3-pick-a-model-template', '4-write-your-prompt',
        '5-choose-what-to-generate', '6-wire-the-handoff', '7-run-it', '8-bring-in-media', '9-where-to-go-next')],
    'docs/generating.md#model-agnostic-capability-bounded', 'docs/prompts.md#the-prompt-management-panel',
    'docs/references.md#recipes-and-lane-setup', 'docs/references.md#prompts-from-references',
]


def slug(heading):
    """GitHub's heading id: the rendered text, lowercased, with everything but
    letters, digits, `_`, `-` and spaces dropped, and spaces turned into `-`."""
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', heading)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('`', '').replace('*', '')
    text = re.sub(r'[^\w\- ]', '', text.strip().lower())
    return text.replace(' ', '-')


def prose_lines(path):
    """The lines of a Markdown file outside fenced code blocks."""
    fenced = False
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if not fenced:
            yield line


_anchors = {}


def anchors(path):
    if path not in _anchors:
        found, seen = set(), {}
        for line in prose_lines(path):
            found.update(EXPLICIT.findall(line))
            match = HEADING.match(line)
            if match:
                base = slug(match.group(2))
                count = seen.get(base, 0)
                seen[base] = count + 1
                found.add(base if count == 0 else f'{base}-{count}')
        _anchors[path] = found
    return _anchors[path]


def resolve(target, origin):
    """None when the link lands; otherwise why not. `target` is a relative path
    (from `origin`'s folder), `#anchor`, or `path#anchor`."""
    path, _, anchor = target.partition('#')
    file = (origin.parent / unquote(path)).resolve() if path else origin
    if ROOT not in file.parents and file != ROOT:
        return f'outside the repository: {target}'
    if not file.exists():
        return f'no such file: {target}'
    if anchor and file.suffix == '.md' and anchor not in anchors(file):
        return f'no anchor #{anchor} in {file.relative_to(ROOT).as_posix()}'
    return None


def repo_target(url):
    """A GitHub URL into this repository, as a path relative to the repository
    root, or None for one that names no file (the clone URL)."""
    raw = re.match(r'https?://raw\.githubusercontent\.com/' + re.escape(REPO) + r'/main/(.+)$', url)
    if raw:
        return raw.group(1)
    blob = re.match(r'https?://github\.com/' + re.escape(REPO) + r'/(?:blob|tree)/main/(.+)$', url)
    if blob:
        return blob.group(1)
    root = re.match(r'https?://github\.com/' + re.escape(REPO) + r'/?(#.+)?$', url)
    if root:
        return 'README.md' + (root.group(1) or '')
    if url.endswith('.git'):
        return None
    raise AssertionError(f'a repository URL this test can\'t map: {url}')


def doc_links(path):
    for line in prose_lines(path):
        # Inline code isn't a link.
        line = re.sub(r'`[^`]*`', '', line)
        yield from MD_LINK.findall(line)
        yield from HTML_LINK.findall(line)


def test_the_slug_rule_gives_githubs_ids():
    assert slug('LTX 2.5 Edit & VFX') == 'ltx-25-edit--vfx'
    assert slug('MiniMax H3 — References & Swaps') == 'minimax-h3--references--swaps'
    assert slug('The Launcher says *Missing node packs*') == 'the-launcher-says-missing-node-packs'
    assert slug('Install in Settings → Sonder App is refused or doesn\'t finish') == \
        'install-in-settings--sonder-app-is-refused-or-doesnt-finish'
    assert slug('`torch` / `torchaudio` got reinstalled') == 'torch--torchaudio-got-reinstalled'
    assert slug('Wiring your graph') == 'wiring-your-graph'


def test_no_reference_style_links_escape_the_check():
    for path in DOCS:
        for line in prose_lines(path):
            assert not re.match(r'^\s{0,3}\[[^\]]+\]:\s', line), (path.name, line)


@pytest.mark.parametrize('path', DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_link_in_the_docs_lands(path):
    problems = []
    for target in doc_links(path):
        if target.startswith(('mailto:', 'data:')):
            continue
        if re.match(r'https?://', target):
            if not REPO_URL.fullmatch(target):
                continue  # Another site: checked on GitHub after a release.
            mapped = repo_target(target)
            problem = mapped and resolve(mapped, ROOT / 'README.md')
        else:
            problem = resolve(target, path)
        if problem:
            problems.append(problem)
    assert not problems, '\n'.join(problems)


def test_every_repository_url_shipped_in_the_pack_lands():
    problems, seen = [], 0
    for path in SHIPPED:
        for url in REPO_URL.findall(path.read_text(encoding='utf-8')):
            seen += 1
            mapped = repo_target(url)
            problem = mapped and resolve(mapped, ROOT / 'README.md')
            if problem:
                problems.append(f'{path.relative_to(ROOT).as_posix()}: {problem}')
    assert seen > 100, 'the scan found too few URLs to be reading the files'
    assert not problems, '\n'.join(problems)


def test_bare_docs_paths_in_code_name_real_pages():
    problems = []
    for path in SHIPPED:
        if path.suffix not in {'.py', '.js'}:
            continue
        for mention in re.findall(r'(?<![\w/.-])docs/[\w-]+\.md(?:#[\w-]+)?', path.read_text(encoding='utf-8')):
            problem = resolve(mention, ROOT / 'README.md')
            if problem:
                problems.append(f'{path.relative_to(ROOT).as_posix()}: {problem}')
    assert not problems, '\n'.join(problems)


@pytest.mark.parametrize('target', PUBLISHED)
def test_published_paths_and_anchors_still_land(target):
    assert resolve(target, ROOT / 'README.md') is None, target
