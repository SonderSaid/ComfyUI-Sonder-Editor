"""Derived cache routes resolve contained storage without project mutation."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from test_phase43_routes import DummyRequest, _load_route_module, _response_json, _route_handler
from server.timeline_state import ClipReference, Scene, TimelineProject
from server.project_manager import save_project


ROUTES = [
    ('GET', '/cache/renders'),
    ('POST', '/cache/renders/sweep'),
    ('DELETE', '/cache/renders/{filename}'),
]


def _request(module, method, suffix, project_id='project', **kwargs):
    handler = _route_handler(module, method, '/sonder-editor/project/{project_id}' + suffix)
    request = DummyRequest(
        match_info={'project_id': project_id, 'filename': 'old.pt'},
        body={'max_size_bytes': 0}, method=method,
        headers={'If-Match': 'deliberately-stale'},
        path='/sonder-editor/project/' + project_id + suffix,
        **kwargs,
    )
    return asyncio.run(module._project_version_header_middleware(request, handler))


def _forbid(*args, **kwargs):
    pytest.fail('Cache route must not load, repair, or enumerate an unresolved project')


@pytest.mark.parametrize('method,suffix', ROUTES)
def test_folder_routes_never_parse_project_and_ignore_stale_version(tmp_path, monkeypatch, method, suffix):
    module = _load_route_module(monkeypatch)
    project_dir = tmp_path / 'project'
    cache_dir = project_dir / 'cache' / 'renders'
    cache_dir.mkdir(parents=True)
    # Deliberately unreadable model: only project-file existence is required.
    project_file = project_dir / 'project.json'
    project_file.write_bytes(b'not JSON')
    (cache_dir / 'old.pt').write_bytes(b'cache')
    before = project_file.stat().st_mtime_ns
    monkeypatch.setattr(module, '_get_base_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module, '_load_project_from_request', _forbid)
    monkeypatch.setattr(module, 'load_project', _forbid)
    monkeypatch.setattr(module, 'save_project', _forbid)

    response = _request(module, method, suffix)
    assert response.status == 200
    assert 'X-Sonder-Project-Id' not in response.headers
    assert 'X-Sonder-Project-Modified-At' not in response.headers
    assert project_file.read_bytes() == b'not JSON'
    assert project_file.stat().st_mtime_ns == before
    if method != 'GET':
        assert not (cache_dir / 'old.pt').exists()


@pytest.mark.parametrize('method,suffix', ROUTES)
def test_canonical_alias_fallback_neither_repairs_nor_checks_version(tmp_path, monkeypatch, method, suffix):
    module = _load_route_module(monkeypatch)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    scene = Scene(scene_id='scene')
    scene.clips = [ClipReference(clip_id='clip', source_out_frame=20, total_source_frames=0)]
    project = TimelineProject(project_id='canonical-id', project_dir=str(project_dir), scenes=[scene])
    save_project(project, notify=False)
    cache_dir = project_dir / 'cache' / 'renders'
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / 'old.pt').write_bytes(b'cache')
    project_file = project_dir / 'project.json'
    before = (project_file.read_bytes(), project_file.stat().st_mtime_ns)
    monkeypatch.setattr(module, '_get_base_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module, 'save_project', _forbid)
    loaded = []
    real_load = module.load_project

    def load(path):
        model = real_load(path)
        loaded.append(model)
        return model

    monkeypatch.setattr(module, 'load_project', load)
    response = _request(module, method, suffix, 'canonical-id')
    assert response.status == 200
    assert len(loaded) == 1
    assert loaded[0].scenes[0].clips[0].total_source_frames == 0
    assert (project_file.read_bytes(), project_file.stat().st_mtime_ns) == before
    # The fallback really loads a model, so existing middleware headers remain.
    assert response.headers['X-Sonder-Project-Id'] == 'canonical-id'


@pytest.mark.parametrize('method,suffix', ROUTES)
def test_unresolved_empty_directory_never_reaches_cache_or_cwd(tmp_path, monkeypatch, method, suffix):
    module = _load_route_module(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, '_direct_project_dir_from_request', lambda request: None)
    monkeypatch.setattr(module, '_load_project_from_request', lambda request, **kwargs: SimpleNamespace(project_dir=''))
    monkeypatch.setattr(module, '_list_render_cache_entries', _forbid)
    monkeypatch.setattr(module, 'delete_render_cache_entry', _forbid)
    monkeypatch.setattr(module, 'enforce_render_cache_budget', _forbid)
    monkeypatch.setattr(os, 'scandir', _forbid)
    response = _request(module, method, suffix)
    assert 400 <= response.status < 500


@pytest.mark.parametrize('body', [{}, None, [], {'max_size_bytes': True}, {'max_size_bytes': -1},
                                 {'max_size_bytes': 1.5}, {'max_size_bytes': '0'},
                                 {'max_size_bytes': 9_007_199_254_740_992}])
def test_budget_rejected_before_directory_resolution(monkeypatch, body):
    module = _load_route_module(monkeypatch)
    monkeypatch.setattr(module, '_project_dir_without_model', _forbid)
    response = asyncio.run(module.api_sweep_render_cache(DummyRequest(body=body, method='POST')))
    assert response.status == 400


def test_invalid_json_rejected_before_directory_resolution(monkeypatch):
    module = _load_route_module(monkeypatch)
    monkeypatch.setattr(module, '_project_dir_without_model', _forbid)

    class InvalidJsonRequest(DummyRequest):
        async def json(self):
            raise json.JSONDecodeError('invalid', '{', 0)

    assert asyncio.run(module.api_sweep_render_cache(InvalidJsonRequest())).status == 400


@pytest.mark.parametrize('method,suffix', ROUTES)
@pytest.mark.parametrize('project_id,query', [('missing', {}), ('../escape', {}), ('project', {'path': 'elsewhere'})])
def test_cache_routes_keep_project_path_validation(tmp_path, monkeypatch, method, suffix, project_id, query):
    module = _load_route_module(monkeypatch)
    monkeypatch.setattr(module, '_get_base_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module, 'enforce_render_cache_budget', _forbid)
    monkeypatch.setattr(module, 'delete_render_cache_entry', _forbid)
    monkeypatch.setattr(module, '_list_render_cache_entries', _forbid)
    response = _request(module, method, suffix, project_id, query=query)
    assert 400 <= response.status < 500
