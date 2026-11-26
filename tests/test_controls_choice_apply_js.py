"""A Modes click carries what it saw (server/control_expectations.py): the
choice's token from the view it was drawn on. A fresh view whose token moved
refuses before the "already selected" shortcut can skip the check."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import assert from 'node:assert/strict';
const {applyChoice, choiceExpectation} = await import(APPLY_URL);
const row = (token, extra={}) => ({id:'off', name:'Mask Off', state:'differs', token, ...extra});
const view = (token, extra={}) => ({state:'ready', workflow_id:'wf', presets:[row(token, extra)]});
const make = (clicked, next, fresh=true) => {
    const calls = [];
    const host = {getView:()=>clicked, freshView:async()=>({view:next, fresh}),
        applyChanges:async(changes, options)=>{calls.push([changes, options]); return {ok:true};}};
    return {host, calls};
};
const opts = (decided=[]) => ({flushPending:async()=>true, track:()=>{}, decide:(v, fresh)=>{decided.push(fresh); return true;}});

// Unchanged since the click: the write carries the click's token.
let {host, calls} = make(view('t1'), view('t1'));
assert.equal((await applyChoice(host, 'off', opts())).status, 'applied');
assert.deepEqual(calls[0], [{'preset:off:apply':true}, {expect:{workflow:'wf', targets:{'preset:off:apply':{token:'t1'}}}}]);

// Its effect moved after the click (a node joined its group): refused before
// decide, so an "already selected" answer can't skip the check either.
({host, calls} = make(view('t1'), view('t2', {state:'match'})));
const decided = [];
const moved = await applyChoice(host, 'off', opts(decided));
assert.equal(moved.status, 'refused'); assert.match(moved.reason, /“Mask Off” changed after you clicked it/);
assert.equal(calls.length, 0); assert.deepEqual(decided, []);

// A view that isn't fresh decides nothing: the writer checks the click's token.
({host, calls} = make(view('t1'), view('t2'), false));
assert.equal((await applyChoice(host, 'off', opts())).status, 'applied');
assert.equal(calls[0][1].expect.targets['preset:off:apply'].token, 't1');

// No token (an unavailable choice, an older server): the write carries none.
({host, calls} = make(view(null), view(null)));
assert.equal((await applyChoice(host, 'off', opts())).status, 'applied');
assert.equal(calls[0][1], undefined);
assert.equal(choiceExpectation({presets:[row('t1')]}, 'off').workflow, '');
assert.equal(choiceExpectation(view('t1'), 'gone'), null);
console.log('ok');
'''


def test_a_click_carries_its_token_and_a_moved_effect_refuses(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required')
    path = tmp_path / 'apply.mjs'
    path.write_text(SCRIPT.replace('APPLY_URL', json.dumps((ROOT / 'web/js/controls_choice_apply.js').as_uri())), encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
