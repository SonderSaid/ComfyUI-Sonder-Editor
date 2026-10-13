import json
import asyncio
import importlib
import threading
from types import SimpleNamespace
import pytest
import server
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from server import control_descriptors as cd, routes
from test_control_descriptors import CLASSES, _binding, _node, _relay

@pytest.fixture
def prompt(monkeypatch):
    original=cd.describe_bindings
    monkeypatch.setattr(cd,'describe_bindings',lambda p, relay_census=None, **kw: original(p,relay_census=relay_census,class_lookup=CLASSES.get,**kw))
    return {'1':_node('SonderEditor'),
        '3':_node('SonderSaveVideo',{'project':['1',0],'mark_queue_complete':False,'place_audio_on_timeline':True}),
        '4':_node('SonderSaveVideo',{'project':['1',0],'mark_queue_complete':True,'place_audio_on_timeline':False}),
        '6':_node('KSampler',{'steps':20}),
        '9':_relay({'steps':_binding('6','steps',label='Steps'),
                    'advance':_binding('3','mark_queue_complete',label='Advance',section='editor')})}

def describe(body):
    return routes._describe_controls_body(json.dumps(body).encode())

def test_canonical_atomic_validation_and_per_instance_editor_bindings(prompt):
    view=describe({'prompt':prompt,'changes':{'binding:advance:value':True,'binding:steps:value':'31'}})
    assert view['ok'] and view['changes']=={'binding:advance:value':True,'binding:steps:value':'31'}
    advance=next(b for b in view['bindings'] if b['binding_id']=='advance')
    # Only the anchored instance is exposed; the second Save Video is untouched.
    assert advance['section']=='editor' and [t['path'] for t in advance['targets']]==['3']
    bad=describe({'prompt':prompt,'changes':{'binding:steps:value':'31','other':1}})
    assert not bad['ok'] and bad['changes']=={}

def test_the_canvas_census_turns_a_muted_relay_into_a_view_error(prompt):
    del prompt['9']
    view=describe({'prompt':prompt,'relay_census':{'active':0,'muted':1},'changes':{'binding:steps:value':'31'}})
    assert view['relay']['status']=='error' and 'muted' in view['error'] and not view['ok']

def test_full_combo_validation_precedes_preview_limit(prompt,monkeypatch):
    class LongCombo:
        @classmethod
        def INPUT_TYPES(cls):return {'required':{'value':([str(i) for i in range(2501)],)}}
    monkeypatch.setitem(CLASSES,'LongCombo',LongCombo)
    prompt['7']=_node('LongCombo',{'value':'2001'})
    prompt['9']=_relay({'pick':_binding('7','value')})
    view=describe({'prompt':prompt,'changes':{'binding:pick:value':'2500'}})
    assert view['ok'] and view['changes']['binding:pick:value']=='2500'
    assert view['bindings'][0]['options_truncated'] and len(view['bindings'][0]['options'])==2000
    assert not describe({'prompt':prompt,'changes':{'binding:pick:value':'bad'}})['ok']

def test_canvas_validation_http_encodes_long_values_off_loop(prompt, monkeypatch):
    prompt['7']=_node('TextNode',{'text':''})
    prompt['9']=_relay({'text':_binding('7','text')})
    body=json.dumps({'prompt':prompt,'changes':{'binding:text:value':'x'*500000}}).encode()
    entered,resume=threading.Event(),threading.Event()
    original=json.dumps
    loop_thread=threading.get_ident()
    threads=[]
    def tracked(value,*args,**kwargs):
        if isinstance(value,dict) and 'bindings' in value and 'changes' in value:
            threads.append(threading.get_ident());entered.set()
            assert resume.wait(5)
        return original(value,*args,**kwargs)
    async def run():
        monkeypatch.setattr(server,'PromptServer',SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef())),raising=False)
        module=importlib.reload(routes)
        handler=next(r.handler for r in module.routes if r.path=='/sonder-editor/controls/describe')
        app=web.Application();app.router.add_post('/sonder-editor/controls/describe',handler)
        async def alive(request):return web.Response(text='alive')
        app.router.add_get('/alive',alive)
        async with TestClient(TestServer(app)) as client:
            monkeypatch.setattr(json,'dumps',tracked)
            task=asyncio.create_task(client.post('/sonder-editor/controls/describe',data=body))
            try:
                assert await asyncio.to_thread(entered.wait,3)
                assert (await asyncio.wait_for(client.get('/alive'),1)).status==200
            finally:resume.set()
            response=await task
            assert response.status==200
            result=await response.json()
            assert result['ok'] and len(result['changes']['binding:text:value'])==500000
    asyncio.run(run())
    assert threads and all(t!=loop_thread for t in threads)


def test_restore_route_verifies_candidates_and_publishes_cleanup_exclusion(prompt, monkeypatch):
    original = cd.candidate_inventory
    monkeypatch.setattr(cd, "candidate_inventory", lambda p, cs: original(p, cs, class_lookup=CLASSES.get))
    prompt["9"] = _relay({"steps": {"status": "missing", "targets": [], "restore_targets": [{"path": "6", "input": "steps"}]}})
    body = {"prompt": prompt, "restore_plan": {"restorable": [{"binding_id": "steps", "path": "6", "input": "steps"}], "unmatched": []},
            "candidates": [{"path": "6", "input": "steps", "targets": [{"path": "6", "input": "steps"}]}],
            "changes": {"workflow:restore": {"bindings": ["steps"]}}}
    result = describe(body)
    assert result["ok"] and len(result["restore"]["restorable"]) == 1
    assert result["cleanup"]["bindings"] == []
    prompt["6"]["inputs"]["steps"] = ["1", 0]
    result = describe(body)
    assert not result["ok"] and result["restore"]["unmatched"]
    assert not result["restore"]["restorable"]
    assert result["cleanup"]["bindings"]
