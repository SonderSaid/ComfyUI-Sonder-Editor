"""Group authoring planners (web/js/controls_panel_groups.js): pure change builders."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import assert from 'node:assert/strict';
globalThis.window = {addEventListener(){}, removeEventListener(){}};
globalThis.document = {addEventListener(){}, removeEventListener(){}};
const g = await import(MODULE_URL);
const order = ['A', 'B', 'C'];
// Up goes before the previous group; down before the one after next (or last).
assert.deepEqual(g.groupStepChanges(order, 'B', -1), {'group:B:move': {before: 'A'}});
assert.deepEqual(g.groupStepChanges(order, 'B', 1), {'group:B:move': {before: null}});
assert.deepEqual(g.groupStepChanges(order, 'A', 1), {'group:A:move': {before: 'C'}});
assert.equal(g.groupStepChanges(order, 'A', -1), null);
assert.equal(g.groupStepChanges(order, 'C', 1), null);
assert.throws(() => g.groupStepChanges(order, 'Z', 1), /changed/);
// A drop that leaves the order as it is sends nothing.
assert.deepEqual(g.groupDropChanges(order, 'C', 'A'), {'group:C:move': {before: 'A'}});
assert.deepEqual(g.groupDropChanges(order, 'A', null), {'group:A:move': {before: null}});
assert.equal(g.groupDropChanges(order, 'A', 'B'), null);
assert.equal(g.groupDropChanges(order, 'C', null), null);
assert.equal(g.groupDropChanges(order, 'B', 'B'), null);
// Mark all expands to one flag per Control that differs; notes never get one.
const members = [{source: 'binding', binding_id: 'a', advanced: true}, {source: 'binding', binding_id: 'b'},
    {source: 'note', note_id: 'n'}];
assert.deepEqual(g.groupAdvancedChanges(members, true), {'binding:b:advanced': true});
assert.deepEqual(g.groupAdvancedChanges(members, false), {'binding:a:advanced': false});
const many = Array.from({length: 65}, (_, i) => ({source: 'binding', binding_id: 'm' + i}));
assert.throws(() => g.groupAdvancedChanges(many, true), /more than 64 Controls/);
assert.equal(Object.keys(g.groupAdvancedChanges(many.slice(1), true)).length, 64);
// Menu items reflect the group's place and members.
const applied = [], notices = [];
let items = g.groupMenuItems({name: 'A', order, members, apply: c => applied.push(c), rename(){}, notify: m => notices.push(m)});
const item = label => items.find(i => i.label === label);
assert.equal(item('Move up').disabled, true);
assert.equal(item('Move down').disabled, false);
assert.ok(item('Mark all Advanced'));
item('Move down').action();
assert.deepEqual(applied.at(-1), {'group:A:move': {before: 'C'}});
item('Ungroup').action();
assert.deepEqual(applied.at(-1), {'group:A:ungroup': true});
items = g.groupMenuItems({name: 'C', order, members: [{source: 'binding', binding_id: 'a', advanced: true}],
    apply: c => applied.push(c), rename(){}, notify: m => notices.push(m)});
assert.ok(item('Clear Advanced'));
assert.equal(item('Move down').disabled, true);
items = g.groupMenuItems({name: 'B', order, members: many, apply: c => applied.push(c), rename(){}, notify: m => notices.push(m)});
const count = applied.length;
item('Mark all Advanced').action();
assert.equal(applied.length, count);
assert.match(notices.at(-1), /more than 64/);
items = g.groupMenuItems({name: 'B', order, members: [], apply(){}, rename(){}, notify(){}});
assert.equal(item('Mark all Advanced').disabled, true);
assert.match(item('Ungroup').hint, /empty group/);
// Move to group lists the other groups, Ungrouped only when grouped, then New group.
const labels = list => list.map(i => i.type === 'separator' ? '---' : i.label);
const moves = [];
items = g.moveToGroupItems({order, current: 'B', planMove: group => ({moved: group}), apply: c => moves.push(c),
    newGroup(){}, notify(){}});
assert.deepEqual(labels(items), ['A', 'C', 'Ungrouped', '---', 'New group…']);
items.find(i => i.label === 'Ungrouped').action();
assert.deepEqual(moves.at(-1), {moved: ''});
items = g.moveToGroupItems({order: [], current: '', planMove(){}, apply(){}, newGroup(){}, notify(){}});
assert.deepEqual(labels(items), ['New group…']);
console.log('ok');
'''


def test_group_planners():
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for the group planner harness')
    script = SCRIPT.replace('MODULE_URL', json.dumps((ROOT / 'web/js/controls_panel_groups.js').as_uri()))
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True,
                            encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
