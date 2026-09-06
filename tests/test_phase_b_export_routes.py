"""Export job reads validate storage ownership without loading project data."""
import asyncio
from functools import partial
import os
import threading

import pytest
from aiohttp.test_utils import make_mocked_request

from test_phase43_routes import _load_route_module, _response_json
from server.timeline_export import TimelineExportJob, TimelineExportManager
from server.timeline_state import TimelineProject, Scene, Asset, ClipReference
from server.project_manager import save_project


def request(module, method='GET', project_id='project', origin=None):
    suffix = '/cancel' if method == 'POST' else ''
    headers = {'Host': 'localhost', 'If-Match': 'stale'}
    if origin:
        headers['Origin'] = origin
    req = make_mocked_request(method, f'/api/sonder-editor/project/{project_id}/render_timeline/job{suffix}',
                              headers=headers, match_info={'project_id': project_id, 'job_id': 'job'})
    handler = module.api_cancel_render_timeline_job if method == 'POST' else module.api_get_render_timeline_job
    for middleware in [module._route_timing_middleware, module._sonder_security_middleware,
                       module._project_version_header_middleware, module._project_conflict_middleware]:
        handler = partial(middleware, handler=handler)
    return asyncio.run(handler(req))


def job_manager(module, monkeypatch, project_dir, status='running'):
    job = TimelineExportJob('job', os.path.abspath(project_dir), 'canonical', str(project_dir), {}, status=status)
    manager = TimelineExportManager()
    # Use the production cancellation method and its event, without starting a render.
    manager._jobs['job'] = job
    monkeypatch.setattr(module, '_TIMELINE_EXPORTS', manager)
    return job


def forbid(*args, **kwargs):
    pytest.fail('Running job lookup must not parse or write a project')


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_folder_job_lookup_never_loads_or_writes(tmp_path, monkeypatch, method):
    module = _load_route_module(monkeypatch)
    folder = tmp_path / 'project'
    folder.mkdir()
    document = folder / 'project.json'
    document.write_bytes(b'not JSON')
    before = document.stat().st_mtime_ns
    monkeypatch.setattr(module, '_get_base_dir', lambda: str(tmp_path))
    for name in ['_load_project_from_request', 'load_project', 'save_project']:
        monkeypatch.setattr(module, name, forbid)
    job = job_manager(module, monkeypatch, folder)
    events = []
    monkeypatch.setattr(module, 'record_diag_event', lambda kind, **kw: events.append(kind))
    response = request(module, method)
    assert response.status == 200
    assert job.cancel_event.is_set() == (method == 'POST')
    assert 'X-Sonder-Project-Id' not in response.headers
    assert 'X-Sonder-Project-Modified-At' not in response.headers
    assert 'mutation_route_entry' not in events
    assert document.read_bytes() == b'not JSON'
    assert document.stat().st_mtime_ns == before


def test_cancel_still_rejects_foreign_origin(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    job = job_manager(module, monkeypatch, tmp_path)
    monkeypatch.setattr(module, '_project_dir_without_model', forbid)
    response = request(module, 'POST', origin='https://foreign.invalid')
    assert response.status == 403
    assert not job.cancel_event.is_set()


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_wrong_project_job_is_not_disclosed(tmp_path, monkeypatch, method):
    module = _load_route_module(monkeypatch)
    monkeypatch.setattr(module, '_project_dir_without_model', lambda req, **kwargs: str(tmp_path / 'other'))
    monkeypatch.setattr(module, '_load_project_from_request', forbid)
    job = job_manager(module, monkeypatch, tmp_path / 'owned')
    assert request(module, method).status == 404
    assert not job.cancel_event.is_set()


def test_job_ownership_compares_realpaths(monkeypatch, tmp_path):
    module = _load_route_module(monkeypatch)
    job = job_manager(module, monkeypatch, tmp_path / 'junction')
    original = os.path.realpath
    monkeypatch.setattr(os.path, 'realpath', lambda path: str(tmp_path / 'target')
                        if str(path) == str(tmp_path / 'junction') else original(path))
    assert module._timeline_job_matches_project(job, str(tmp_path / 'target'))
    assert not module._timeline_job_matches_project(job, str(tmp_path / 'other'))


@pytest.mark.parametrize('project_id', ['project', 'canonical'])
def test_completed_payload_and_headers_are_built_off_loop(tmp_path, monkeypatch, project_id):
    module = _load_route_module(monkeypatch)
    folder = tmp_path / 'project'
    folder.mkdir()
    project = TimelineProject(project_id='canonical', project_dir=str(folder), scenes=[Scene(scene_id='scene')],
                              assets=[Asset(asset_id='asset', name='export.mp4', asset_type='video', path='media/export.mp4')])
    save_project(project, notify=False)
    monkeypatch.setattr(module, '_get_base_dir', lambda: str(tmp_path))
    job = job_manager(module, monkeypatch, folder, 'completed')
    job.result_asset_id, job.result_scene_id = 'asset', 'scene'
    main_thread = threading.get_ident()
    real_load = module._load_project_from_request
    calls = []
    def load(*args, **kwargs):
        calls.append(threading.get_ident())
        return real_load(*args, **kwargs)
    monkeypatch.setattr(module, '_load_project_from_request', load)
    response = request(module, project_id=project_id)
    assert response.status == 200
    payload = _response_json(response)
    assert payload['result']['asset']['asset_id'] == 'asset'
    assert payload['result']['scene']['scene_id'] == 'scene'
    assert response.headers['X-Sonder-Project-Id'] == 'canonical'
    assert response.headers['X-Sonder-Project-Modified-At']
    assert len(calls) == (2 if project_id == 'canonical' else 1)
    assert all(thread != main_thread for thread in calls)


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_alias_lookup_is_once_without_repair_or_version_gate(tmp_path, monkeypatch, method):
    module = _load_route_module(monkeypatch)
    folder = tmp_path / 'project'
    folder.mkdir()
    scene = Scene(scene_id='scene', clips=[ClipReference(clip_id='clip', source_out_frame=20, total_source_frames=0)])
    project = TimelineProject(project_id='canonical', project_dir=str(folder), scenes=[scene])
    save_project(project, notify=False)
    document = folder / 'project.json'
    before = document.read_bytes(), document.stat().st_mtime_ns
    monkeypatch.setattr(module, '_get_base_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module, 'save_project', forbid)
    job_manager(module, monkeypatch, folder)
    real_load = module._load_project_from_request
    calls = []
    def load(req, **kwargs):
        calls.append(kwargs)
        return real_load(req, **kwargs)
    monkeypatch.setattr(module, '_load_project_from_request', load)
    response = request(module, method, project_id='canonical')
    assert response.status == 200
    assert 'X-Sonder-Project-Id' not in response.headers
    assert 'X-Sonder-Project-Modified-At' not in response.headers
    assert calls == [{'repair_missing_frames': False, 'version_checked': False}]
    assert (document.read_bytes(), document.stat().st_mtime_ns) == before


def test_guide_entry_records_dispatch_before_off_loop_load(monkeypatch):
    module = _load_route_module(monkeypatch)
    events = []
    main_thread = threading.get_ident()
    monkeypatch.setattr(module, 'record_diag_event', lambda kind, **kw: events.append((kind, kw)))
    def load(req):
        assert threading.get_ident() != main_thread
        assert events[0][0] == 'bridge_guides_route_entry'
        return TimelineProject(project_id='project', scenes=[Scene(scene_id='scene')])
    monkeypatch.setattr(module, '_load_project_from_request', load)
    req = make_mocked_request('GET', '/api/sonder-editor/project/project/scenes/scene/bridge-guides',
        match_info={'project_id': 'project', 'scene_id': 'scene'}, headers={
            'X-Sonder-Guide-Request-Id': 'guide-test', 'X-Sonder-Guide-Generation': 'wave',
            'X-Sonder-Guide-Origin': 'lifecycle', 'X-Sonder-Guide-Node-Id': '7'})
    response = asyncio.run(module.api_bridge_guides(req))
    assert response.status == 200
    assert events == [('bridge_guides_route_entry', {
        'project_id': 'project', 'rel_url': str(req.rel_url), 'request_id': 'guide-test',
        'generation': 'wave', 'origin': 'lifecycle', 'node_id': '7'})]
