"""Run notifications in the real modules: the notice model, the per-page driver
and the notification Core's tier-safe coalescing (live-workflow Phase 3B,
plans/live-workflow-phase-3-job-feedback.md D5/D6)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r'''
import assert from 'node:assert/strict';
const N = await import(NOTIFY_URL);
const M = await import(NOTICE_URL);
let list = [];
N.subscribe(l => { list = l; });
const reset = () => { N._resetForTest(); list = []; N.subscribe(l => { list = l; }); };
let clock = 1_000_000;
const now = () => clock;
const rec = (pid, seq, state, extra = {}) => ({prompt_id: pid, seq, epoch: 'e1', state, purpose: 'run', host_ids: ['h1'], run_ids: [],
    stage: '', node: '', node_title: '', count: null, waited_ms: 0, elapsed_ms: 0, receipt_rev: 1, ...extra});
const RECEIPT = {requested: [{editor: '1', scene_id: 's', selection_start: 0, selection_end: 48, linked: false, render_queue_active: false}],
    windows: [], jobs: [], modes: null};
const driver = (extra = {}) => M.createRunFeedbackDriver({page: 'p', ownerMatch: r => (r.host_ids || []).includes('h1'),
    notify: N.notify, now, ...extra});
const progressItems = () => list.filter(n => n.tier === 'progress');
'''

MODEL = r'''
// Stages: declared by the working node, counts only where they belong.
assert.deepEqual(M.stageLine(rec('a', 1, 'queued')), {verb: 'Queued', note: "Waiting in ComfyUI's queue", progress: null});
assert.deepEqual(M.stageLine(rec('a', 1, 'running', {stage: 'rendering', count: {value: 17, max: 48}})).progress, {current: 17, total: 48});
assert.equal(M.stageLine(rec('a', 1, 'running', {stage: 'preparing_inputs'})).verb, 'Preparing graph inputs');
assert.equal(M.stageLine(rec('a', 1, 'running', {stage: 'encoding', count: {value: 3, max: 9}})).verb, 'Encoding frames');
assert.equal(M.stageLine(rec('a', 1, 'running', {stage: 'saving'})).verb, 'Saving the take');
assert.equal(M.stageLine(rec('a', 1, 'running', {stage: 'saving', purpose: 'branch'})).verb, 'Saving the output');
assert.deepEqual(M.stageLine(rec('a', 1, 'running', {node_title: 'KSampler', count: {value: 4, max: 20}})),
    {verb: 'Running KSampler', note: '', progress: {current: 4, total: 20}});
assert.equal(M.stageLine(rec('a', 1, 'finishing')).verb, 'Finishing');
// The App's own states lead where the record can't know them.
assert.equal(M.stageLine(null, {state: 'submitting'}).verb, 'Sending');
assert.equal(M.stageLine(rec('a', 1, 'queued'), {state: 'queued', canceling: true}).verb, 'Canceling');
assert.equal(M.stageLine(rec('a', 1, 'completed'), {state: 'finishing'}).verb, 'Finishing', 'the App still lands the take');

// Receipts: the range, the queue, the Modes; useful without Controls.
assert.deepEqual(M.receiptLines(rec('a', 1, 'queued', {receipt: RECEIPT})), ['In 0 · Out 48 (48)', 'Render Queue off']);
assert.deepEqual(M.receiptLines(rec('a', 1, 'queued', {receipt: {...RECEIPT, requested: [{...RECEIPT.requested[0], linked: true, render_queue_active: true}]}})),
    ['Range set by the graph', 'Render Queue on — the job is picked when the run starts']);
const consumed = {...RECEIPT, requested: [{...RECEIPT.requested[0], render_queue_active: true}],
    windows: [{editor: '1', generation_start: 0, generation_end: 48, frame_count_padding: 1, fps: 24, queue: 'consume'}],
    jobs: [{editor: '1', job_id: 'j', scene_name: 'Scene 1', mode: 'consume', batch_index: 1, batch_total: 3}],
    modes: [{id: 't', name: 'Task', status: 'selected', choice: 'Alpha', matches: ['Alpha']},
            {id: 'l', name: 'Look', status: 'ambiguous', choice: '', matches: ['Warm', 'Cold']},
            {id: 's', name: 'Style', status: 'custom', choice: '', matches: []}]};
assert.deepEqual(M.receiptLines(rec('a', 1, 'running', {receipt: consumed})), [
    'In 0 · Out 48 (48) · +1 padding frame', 'Render Queue job: Scene 1 · Chunk 2 of 3',
    'Task: Alpha', 'Look: “Warm” and “Cold” both match. Fix in Manage.', 'Style: Custom']);
assert.equal(M.receiptLines(rec('a', 1, 'running', {receipt: consumed}), {timecodeMode: 'timecode'})[0], 'In 0:00:00 · Out 0:02:00 (0:02:00) · +1 padding frame');
assert.deepEqual(M.receiptLines(rec('a', 1, 'queued', {receipt: {...RECEIPT, requested: [{...RECEIPT.requested[0], selection_end: 0}]}})),
    ['Render Queue off'], 'an empty selection names no range');
const peeked ={...consumed, modes: null, jobs: [{...consumed.jobs[0], mode: 'peek', batch_total: 1}]};
assert.equal(M.receiptLines(rec('a', 1, 'running', {receipt: peeked}))[1], 'Render Queue job: Scene 1 — previewed, not consumed');
const empty = {...consumed, modes: null, jobs: [], windows: [{...consumed.windows[0], queue: 'live', frame_count_padding: 0}]};
assert.equal(M.receiptLines(rec('a', 1, 'running', {receipt: empty}))[1], 'Render Queue had no pending job — the live selection ran');

// Outcomes: one classification from the server's terminal, App states for App runs.
const out = (state, terminal, extra = {}) => M.outcomeFor(rec('a', 2, state, {terminal, ...extra}));
assert.equal(out('completed', {outcome: 'success', take_saved: true}).message, 'Take landed on the timeline');
assert.equal(out('completed', {outcome: 'success', take_saved: false}).message, 'Run completed');
assert.equal(out('completed', {outcome: 'success'}, {purpose: 'branch'}).message, 'Output ready');
assert.deepEqual(out('failed', {outcome: 'error', node_type: 'ImageToMask'}), {tier: 'error', message: 'Failed in ImageToMask', kind: 'failed', note: ''});
assert.equal(out('cancelled', {outcome: 'interrupted', returned_job_ids: ['j']}, {elapsed_ms: 900}).message, 'Canceled — the job returned to pending');
assert.equal(out('removed', {outcome: 'removed'}).message, 'Removed before it started');
assert.equal(M.outcomeFor(rec('a', 2, 'removed'), {state: 'canceled'}).kind, 'cancelled:unstarted');
assert.equal(M.outcomeFor(rec('a', 2, 'cancelled', {terminal: {outcome: 'interrupted'}}), {state: 'canceled'}).kind, 'cancelled:started');
assert.equal(M.outcomeFor(null).message, M.UNCONFIRMED_END);
// An App branch run reads as an output, never a take, unless it saved one.
const branch = state => M.outcomeFor(null, {state, purpose: 'branch', note: 'Why.'});
assert.deepEqual(['completed', 'landed', 'failed', 'ended'].map(s => branch(s).message),
    ['Output ready', 'Take landed on the timeline', 'Output failed', 'Output run ended']);
assert.equal(branch('failed').note, 'Why.');
assert.equal(M.outcomeFor(null, {state: 'ended', note: 'The run finished without completing its job.'}).message, 'Take ended without landing');
// The live notice: stage title, receipt lines, how many more wait.
const live = M.noticeFor(rec('a', 1, 'queued', {receipt: RECEIPT}), null, {waiting: 2});
assert.deepEqual([live.verb, live.message, live.detail], ['Queued', "Waiting in ComfyUI's queue · In 0 · Out 48 (48) · Render Queue off", '+2 waiting']);
console.log('ok');
'''

SINGLE = r'''
// One run, one toast: Queued -> Running -> completed resolves the same notification.
const d = driver();
d.applyRecord(rec('a', 1, 'queued', {receipt: RECEIPT}));
assert.equal(list.length, 1); const id = list[0].id;
assert.equal(list[0].verb, 'Queued');
clock += 500;
d.applyRecord(rec('a', 2, 'running', {stage: 'rendering', count: {value: 5, max: 48}, elapsed_ms: 200}));
assert.equal(list.length, 1); assert.equal(list[0].id, id);
assert.equal(list[0].verb, 'Rendering frames'); assert.deepEqual(list[0].progress, {current: 5, total: 48});
assert.equal(list[0].startedAt, clock - 200, 'the elapsed clock counts from the run start');
assert.equal(list[0].message, 'In 0 · Out 48 (48) · Render Queue off', 'the receipt is kept when an update omits it');
d.applyRecord(rec('a', 1, 'queued'));
assert.equal(list[0].verb, 'Rendering frames', 'a lower seq is dropped');
d.applyRecord(rec('a', 3, 'completed', {terminal: {outcome: 'success', take_saved: true}, elapsed_ms: 900}));
assert.equal(list.length, 1); assert.equal(list[0].id, id);
assert.deepEqual([list[0].tier, list[0].verb, list[0].message], ['success', null, 'Take landed on the timeline']);
assert.match(list[0].detail, /Render Queue off/);
// Another host's record is not this page's.
d.applyRecord({...rec('b', 1, 'running'), host_ids: ['h2']});
assert.equal(list.length, 1);
console.log('ok');
'''

BATCH = r'''
// A batch of three: one live toast with "+N waiting"; earlier outcomes become
// receipts that coalesce and still list their runs; the last run resolves the live toast.
const d = driver();
for (const pid of ['a', 'b', 'c']) d.applyRecord(rec(pid, 1, 'queued', {receipt: RECEIPT}));
assert.equal(progressItems().length, 1); assert.equal(list.length, 1);
assert.equal(list[0].detail, '+2 waiting');
d.applyRecord(rec('a', 2, 'running', {stage: 'rendering', count: {value: 1, max: 48}, elapsed_ms: 10}));
d.applyRecord(rec('a', 3, 'completed', {terminal: {outcome: 'success', take_saved: true}}));
assert.equal(progressItems().length, 1, 'the live toast moves on');
assert.equal(progressItems()[0].detail, '+1 waiting');
const landed = list.filter(n => n.tier === 'success');
assert.equal(landed.length, 1); assert.equal(landed[0].message, 'Take landed on the timeline');
d.applyRecord(rec('b', 2, 'running', {elapsed_ms: 10}));
d.applyRecord(rec('b', 3, 'completed', {terminal: {outcome: 'success', take_saved: true}}));
const merged = list.filter(n => n.tier === 'success');
assert.equal(merged.length, 1); assert.equal(merged[0].count, 2);
assert.equal(merged[0].detail.split('\n').length, 2, 'the merged receipt names both runs');
const liveId = progressItems()[0].id;
d.applyRecord(rec('c', 2, 'running', {elapsed_ms: 10}));
d.applyRecord(rec('c', 3, 'failed', {terminal: {outcome: 'error', node_type: 'VAEDecode'}}));
assert.equal(progressItems().length, 0);
const failed = list.find(n => n.id === liveId);
assert.deepEqual([failed.tier, failed.message], ['error', 'Failed in VAEDecode'], 'the last run resolves the live toast');

// Started and unstarted cancels never merge.
reset();
const e = driver();
e.applyRecord(rec('x', 1, 'queued')); e.applyRecord(rec('y', 1, 'queued')); e.applyRecord(rec('z', 1, 'queued'));
e.applyRecord(rec('x', 2, 'running', {elapsed_ms: 50}));
e.applyRecord(rec('y', 2, 'removed', {terminal: {outcome: 'removed'}}));
e.applyRecord(rec('x', 3, 'cancelled', {elapsed_ms: 60, terminal: {outcome: 'interrupted'}}));
const warnings = list.filter(n => n.tier === 'warning').map(n => [n.message, n.count]);
assert.deepEqual(warnings.sort(), [['Canceled', 1], ['Removed before it started', 1]]);
assert.equal(progressItems().length, 1, 'the third run still has the live toast');
console.log('ok');
'''

READ = r'''
// A fresh page shows live runs only; a held run the read no longer knows ends unconfirmed.
const d = driver();
d.applyRead({ok: true, epoch: 'e1', records: [rec('old', 9, 'completed', {terminal: {outcome: 'success'}}), rec('now', 4, 'running', {elapsed_ms: 1000})]});
assert.equal(list.length, 1); assert.equal(list[0].tier, 'progress');
d.applyRead({ok: true, epoch: 'e1', records: []});
assert.deepEqual([list[0].tier, list[0].message], ['warning', M.UNCONFIRMED_END]);
// A restarted server (new epoch) ends what the page was showing, never a run
// the new server reports.
reset();
const e = driver();
e.applyRecord(rec('p', 1, 'running', {elapsed_ms: 10}));
e.applyRead({ok: true, epoch: 'e2', records: [{...rec('n', 1, 'running', {elapsed_ms: 5}), epoch: 'e2'}]});
assert.equal(list.filter(n => n.message === M.UNCONFIRMED_END).length, 1, 'the old run ends');
assert.equal(list.filter(n => n.tier === 'progress').length, 1, 'the new-epoch run stays live');
// A record from a new server process ends the runs held from the old one.
reset();
const g = driver();
g.applyRecord(rec('old', 1, 'running', {elapsed_ms: 10}));
g.applyRecord({...rec('new', 1, 'queued'), epoch: 'e3'});
assert.equal(list.some(n => n.message === M.UNCONFIRMED_END), true);
// A dismissed live toast stays closed for that run; the next run opens one.
reset();
const f = driver();
f.applyRecord(rec('q', 1, 'running', {elapsed_ms: 10})); f.applyRecord(rec('r', 1, 'queued'));
N.dismiss(list[0].id);
f.applyRecord(rec('q', 2, 'running', {elapsed_ms: 20, stage: 'rendering'}));
assert.equal(list.length, 0);
f.applyRecord(rec('q', 3, 'completed', {terminal: {outcome: 'success', take_saved: true}}));
assert.equal(progressItems().length, 1, 'the next run leads with a new toast');
assert.equal(list.filter(n => n.tier === 'success').length, 1, 'the dismissed run still reports how it ended');

// A read speaks only for its scope (one Editor host) and for what it was asked
// about: another host's run and a run bound while the read was in flight stay live.
reset();
const h = driver({ownerMatch: r => (r.host_ids || []).some(x => ['h1', 'h2'].includes(x))});
h.applyRecord({...rec('b', 1, 'running', {elapsed_ms: 10}), host_ids: ['h2']});
let answer;
const reading = h.read(() => new Promise(r => { answer = r; }), {scope: r => (r?.host_ids || []).includes('h1')});
h.applyRecord(rec('late', 1, 'queued'));
answer({ok: true, epoch: 'e1', records: []});
await reading;
assert.equal(list.some(n => n.message === M.UNCONFIRMED_END), false);
assert.equal(list.filter(n => n.tier === 'progress').length, 1);
// A read that keeps failing ends what it covers as unconfirmed.
reset();
const k = driver({readRetriesMs: [0, 0]});
k.applyRecord(rec('s', 1, 'running', {elapsed_ms: 10}));
let tries = 0;
await k.read(async () => { tries++; return null; });
assert.equal(tries, 3);
assert.equal(list[0].message, M.UNCONFIRMED_END);
// A read for a project the Editor left speaks for nothing.
reset();
const s = driver();
s.applyRecord(rec('t', 1, 'running', {elapsed_ms: 10}));
await s.read(async () => ({ok: true, skip: true}));
assert.equal(list[0].tier, 'progress');

// Detach: a socket closed and nothing on the page relays the run's project:
// a neutral note instead of a clock ticking forever; its records revive it.
reset();
let relayed = true;
const owners = new Set(['h1']);
const dt = driver({ownerMatch: r => (r.host_ids || []).some(x => owners.has(x))});
dt.applyRecord(rec('w', 1, 'running', {elapsed_ms: 10}), {project: 'P'});
dt.detach({watched: () => relayed});
assert.equal(list[0].tier, 'progress', 'another socket still relays P');
relayed = false; owners.clear();
dt.detach({watched: () => relayed});
assert.deepEqual([list[0].tier, list[0].message], ['info', 'Still running in another workflow']);
dt.applyRecord(rec('w', 2, 'running', {elapsed_ms: 20, stage: 'encoding'}), {project: 'P'});
assert.equal(list.find(n => n.tier === 'progress')?.verb, 'Encoding frames', 'a held run revives');

// A queued follower's clock counts its own wait, not the previous run's toast.
reset();
const qd = driver();
clock = 5_000_000;
qd.applyRecord(rec('q1', 1, 'running', {elapsed_ms: 1000}));
qd.applyRecord(rec('q2', 1, 'queued', {waited_ms: 300}));
qd.applyRecord(rec('q1', 2, 'completed', {terminal: {outcome: 'success', take_saved: true}}));
const follower = list.find(n => n.tier === 'progress');
assert.equal(follower.verb, 'Queued'); assert.equal(follower.startedAt, 5_000_000 - 300);
console.log('ok');
'''

APP = r'''
// The Sonder App: its runs are the state; records add stage and receipt by prompt id.
const runs = [{run_id: 'r1', state: 'running', prompt_id: 'p1', purpose: 'run', template_name: 'Wan'},
              {run_id: 'r2', state: 'queued', prompt_id: 'p2', purpose: 'run', template_name: 'Wan'}];
const d = driver({app: true, ownerMatch: r => (r.run_ids || []).some(id => ['r1', 'r2'].includes(id)),
                  failureDetail: async () => 'CUDA out of memory'});
d.applyAppRuns(runs, {primed: false});
d.applyRecord({...rec('p1', 1, 'running', {stage: 'rendering', count: {value: 3, max: 9}, elapsed_ms: 100, receipt: RECEIPT}), host_ids: [], run_ids: ['r1']});
assert.equal(list.length, 1);
assert.equal(list[0].verb, 'Rendering frames'); assert.match(list[0].detail, /Wan/); assert.match(list[0].detail, /\+1 waiting/);
// Cancel all: the live toast follows the App's canceling state, never "waiting".
d.applyAppRuns(runs.map(r => ({...r, canceling: true})));
assert.equal(list[0].verb, 'Canceling');
d.applyAppRuns([{...runs[0], state: 'canceled'}, {...runs[1], state: 'canceled'}]);
const msgs = list.map(n => n.message).sort();
assert.deepEqual(msgs, ['Canceled', 'Canceled before it started'], 'started and unstarted cancels stay apart');
assert.equal(progressItems().length, 0);
// A failed App run waits for its detail, then resolves the live toast with it.
reset();
const e = driver({app: true, ownerMatch: () => true, failureDetail: async () => 'CUDA out of memory'});
e.applyAppRuns([{run_id: 'r3', state: 'running', prompt_id: 'p3', purpose: 'run'}]);
const id = list[0].id;
e.applyAppRuns([{run_id: 'r3', state: 'failed', prompt_id: 'p3', purpose: 'run', note: 'The run failed.'}]);
assert.equal(list[0].tier, 'progress', 'still the live toast while the detail is fetched');
await new Promise(r => setTimeout(r, 0));
assert.equal(list.length, 1); assert.equal(list[0].id, id);
assert.equal(list[0].tier, 'error'); assert.match(list[0].detail, /CUDA out of memory/);
// A reload's first snapshot never replays finished runs.
reset();
const f = driver({app: true, ownerMatch: () => true});
// A replayed read before the App's first status opens nothing (App mode from the start).
f.applyRead({ok: true, epoch: 'e1', records: [{...rec('px', 3, 'running', {elapsed_ms: 5}), run_ids: ['other']}]});
assert.equal(list.length, 0);
f.applyAppRuns([{run_id: 'r9', state: 'landed', prompt_id: 'p9'}], {primed: false});
assert.equal(list.length, 0);
console.log('ok');
'''

COALESCE = r'''
// D6: a coalescing slot is one source and one tier.
for (const tier of ['info', 'warning', 'info', 'warning']) N.notify({tier, source: 's', message: tier});
assert.deepEqual(list.map(n => [n.tier, n.count]), [['info', 2], ['warning', 2]]);
// A tier change through a handle clears a stale verb.
const h = N.notify({tier: 'progress', verb: 'Rendering', message: 'm', startedAt: 5});
h.update({tier: 'error', message: 'Failed'});
const item = list.find(n => n.id === h.id);
assert.deepEqual([item.verb, item.startedAt], [null, 5]);
assert.equal(h.alive(), true); h.dismiss(); assert.equal(h.alive(), false);
assert.equal(N.formatElapsed(65_000), '1:05');
console.log('ok');
'''


def run(body, tmp_path, name):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required for run notification tests')
    script = PRELUDE.replace('NOTIFY_URL', json.dumps((ROOT / 'web/js/editor_notifications.js').as_uri()))
    script = script.replace('NOTICE_URL', json.dumps((ROOT / 'web/js/run_feedback_notice.js').as_uri()))
    path = tmp_path / f'{name}.mjs'
    path.write_text(script + body, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith('ok'), result.stdout


@pytest.mark.parametrize('name,body', [('model', MODEL), ('single', SINGLE), ('batch', BATCH), ('read', READ),
                                       ('app', APP), ('coalesce', COALESCE)])
def test_run_notifications(tmp_path, name, body):
    run(body, tmp_path, name)
