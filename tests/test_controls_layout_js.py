"""Bounded ordering properties and Python/native INT contract parity."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from server.control_descriptors import PANEL_ORDER_LIMIT

def test_layout_orders_and_atomic_batch_bound():
    node=shutil.which('node')
    if not node:pytest.skip('node required for panel layout checks')
    uri=(Path(__file__).resolve().parents[1]/'web/js/controls_panel_layout.js').as_uri()
    script=r'''
import assert from 'node:assert/strict';
const {planPanelMove,comparePanelRows,PANEL_ORDER_LIMIT}=await import(URL);
assert.equal(PANEL_ORDER_LIMIT,EXPECTED_BOUND);
const row=(i,order,group='')=>({base:'binding:'+i,label:'Row '+i,order,group,status:'ok'});
const apply=(rows,c)=>rows.map(d=>({...d,order:c[d.base+':order']??d.order,group:c[d.base+':group']??d.group}));
const initial=[row(0,0),row(1,0),row(2,0),row(3,9999)];
const moved=planPanelMove(initial,'binding:2','','binding:1');
assert.deepEqual(apply(initial,moved).sort(comparePanelRows).map(d=>d.base),['binding:0','binding:2','binding:1','binding:3']);
assert.deepEqual(planPanelMove(initial,'binding:2','','binding:3'),{});
const big=Array.from({length:200},(_,i)=>row(i,i*10));
assert.equal(Object.keys(planPanelMove(big,'binding:199','','binding:20')).length,1);
assert.throws(()=>planPanelMove(big.map(d=>({...d,order:0,label:'Row'+d.base.split(':')[1].padStart(3,'0')})),'binding:199','','binding:100'),/Too many/);
const crowded=Array.from({length:100},(_,i)=>({...row(i,0),label:'Row'+String(i).padStart(3,'0')}));
const feasible=planPanelMove(crowded,'binding:99','','binding:40');assert.equal(Object.keys(feasible).length,41);
const expected=crowded.filter(d=>d.base!=='binding:99');expected.splice(40,0,crowded[99]);
assert.deepEqual(apply(crowded,feasible).sort(comparePanelRows).map(d=>d.base),expected.map(d=>d.base));
let state=1234567;const rnd=()=>{state=(Math.imul(state,1664525)+1013904223)>>>0;return state;};
for(let trial=0;trial<300;trial++){
    const rows=Array.from({length:25},(_,i)=>row(i,(rnd()%60)-30,i%3?'':'Advanced'));
    const moving=rows[rnd()%rows.length],group=rnd()%2?'Advanced':'';
    const list=rows.filter(d=>d!==moving&&d.group===group).sort(comparePanelRows),index=rnd()%(list.length+1);
    const before=list[index]?.base??null,changes=planPanelMove(rows,moving.base,group,before);
    list.splice(index,0,moving);
    assert.deepEqual(apply(rows,changes).filter(d=>d.group===group).sort(comparePanelRows).map(d=>d.base),list.map(d=>d.base));
    assert.ok(Object.keys(changes).length<=64);
    for(const [t,v] of Object.entries(changes))if(t.endsWith(':order'))assert.ok(Number.isInteger(v)&&Math.abs(v)<=EXPECTED_BOUND);
}
'''.replace('URL',json.dumps(uri)).replace('EXPECTED_BOUND',str(PANEL_ORDER_LIMIT))
    result=subprocess.run([node,'--input-type=module','-e',script],text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
