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
from test_control_descriptors import CLASSES, _node, _control

@pytest.fixture
def prompt(monkeypatch):
    original=cd.describe_controls
    monkeypatch.setattr(cd,'describe_controls',lambda p: original(p,class_lookup=CLASSES.get))
    return {'1':_node('SonderEditor'), '2':_node('SonderEditor'),
        '3':_node('SonderSaveVideo',{'project':['1',0],'mark_queue_complete':False,'place_audio_on_timeline':True}),
        '4':_node('SonderSaveVideo',{'project':['2',0],'mark_queue_complete':True,'place_audio_on_timeline':False}),
        '5':_control('20','INT',control_id='steps'), '6':_node('KSampler',{'steps':['5',0]})}

def describe(body):
    return routes._describe_controls_body(json.dumps(body).encode())

def test_canvas_scope_and_canonical_atomic_validation(prompt):
    view=describe({'prompt':prompt,'editor_key':'1','changes':{'field:SonderSaveVideo/mark_queue_complete':True,'control:steps:value':'31'}})
    assert view['ok'] and view['changes']=={'field:SonderSaveVideo/mark_queue_complete':True,'control:steps:value':'31'}
    assert {f['key'] for f in view['fields']}=={'3'}
    bad=describe({'prompt':prompt,'editor_key':'1','changes':{'control:steps:value':'31','other':1}})
    assert not bad['ok'] and bad['changes']=={}

@pytest.mark.parametrize('key',[None,'missing','subgraph:1'])
def test_ambiguous_or_nested_editor_fields_are_unavailable(prompt,key):
    view=describe({'prompt':prompt,'editor_key':key,'changes':{'field:SonderSaveVideo/mark_queue_complete':True}})
    assert not view['ok'] and view['rejected']
    assert all(f['status']=='unsupported' for f in view['fields'])

def test_full_combo_validation_precedes_preview_limit(prompt,monkeypatch):
    class LongCombo:
        @classmethod
        def INPUT_TYPES(cls):return {'required':{'value':([str(i) for i in range(2501)],)}}
    monkeypatch.setitem(CLASSES,'LongCombo',LongCombo)
    prompt['5']=_control('2001','COMBO',control_id='steps')
    prompt['6']=_node('LongCombo',{'value':['5',0]})
    view=describe({'prompt':prompt,'editor_key':'1','changes':{'control:steps:value':'2500'}})
    assert view['ok'] and view['changes']['control:steps:value']=='2500'
    assert view['controls'][0]['options_truncated'] and len(view['controls'][0]['options'])==2000
    assert not describe({'prompt':prompt,'editor_key':'1','changes':{'control:steps:value':'bad'}})['ok']

def test_canvas_validation_http_encodes_long_values_off_loop(prompt, monkeypatch):
    prompt['5']=_control('','STRING',control_id='steps')
    prompt['6']=_node('TextNode',{'text':['5',0]})
    body=json.dumps({'prompt':prompt,'editor_key':'1','changes':{'control:steps:value':'x'*500000}}).encode()
    entered,resume=threading.Event(),threading.Event()
    original=json.dumps
    loop_thread=threading.get_ident()
    threads=[]
    def tracked(value,*args,**kwargs):
        if isinstance(value,dict) and 'controls' in value and 'changes' in value:
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
            assert result['ok'] and len(result['changes']['control:steps:value'])==500000
    asyncio.run(run())
    assert threads and all(t!=loop_thread for t in threads)
