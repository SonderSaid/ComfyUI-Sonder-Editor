import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_project_mutation_queue_contract_and_version_headers():
    queue_url = (ROOT / "web" / "js" / "project_mutation_queue.js").as_uri()
    api_url = (ROOT / "web" / "js" / "api_client.js").as_uri()
    _run_node(f"""
        import assert from 'node:assert/strict';
        import {{ ProjectMutationQueue }} from {queue_url!r};
        import {{
            fetchProjectJson,
            getProjectVersion,
            rememberProjectVersion,
        }} from {api_url!r};

        const q1 = new ProjectMutationQueue();
        const calls1 = [];
        const p1 = q1.enqueue({{
            key: 'same',
            intent: {{ value: 1 }},
            run: async (intent) => {{ calls1.push(intent.value); return intent.value; }},
        }});
        const p2 = q1.enqueue({{
            key: 'same',
            intent: {{ value: 2 }},
            run: async (intent) => {{ calls1.push(intent.value); return intent.value; }},
        }});
        assert.deepEqual(await Promise.all([p1, p2]), [2, 2]);
        assert.deepEqual(calls1, [2]);

        const q2 = new ProjectMutationQueue();
        const calls2 = [];
        await Promise.all([
            q2.enqueue({{ key: 'a', intent: 'a', run: async (intent) => {{ calls2.push(intent); }} }}),
            q2.enqueue({{ key: 'b', intent: 'b', run: async (intent) => {{ calls2.push(intent); }} }}),
        ]);
        assert.deepEqual(calls2, ['a', 'b']);

        const q3 = new ProjectMutationQueue();
        const merged = q3.enqueue({{
            key: 'merge',
            intent: {{ fields: {{ opacity: 0.5 }} }},
            merge: (oldIntent, nextIntent) => ({{ fields: {{ ...oldIntent.fields, ...nextIntent.fields }} }}),
            run: async (intent) => intent.fields,
        }});
        const merged2 = q3.enqueue({{
            key: 'merge',
            intent: {{ fields: {{ muted: true }} }},
            merge: (oldIntent, nextIntent) => ({{ fields: {{ ...oldIntent.fields, ...nextIntent.fields }} }}),
            run: async (intent) => intent.fields,
        }});
        assert.deepEqual(await merged, {{ opacity: 0.5, muted: true }});
        assert.deepEqual(await merged2, {{ opacity: 0.5, muted: true }});

        const q4 = new ProjectMutationQueue();
        let releaseActive;
        const active = q4.enqueue({{
            key: 'active',
            run: async () => await new Promise((resolve) => {{ releaseActive = resolve; }}),
        }});
        const drained = q4.drain('test');
        let drainedDone = false;
        drained.then(() => {{ drainedDone = true; }});
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(drainedDone, false);
        releaseActive('ok');
        await active;
        await drained;
        assert.equal(drainedDone, true);

        const q5 = new ProjectMutationQueue();
        const failed = q5.enqueue({{ key: 'fail', run: async () => {{ throw new Error('boom'); }} }});
        const after = q5.enqueue({{ key: 'after', run: async () => 'after' }});
        await assert.rejects(failed, /boom/);
        assert.equal(await after, 'after');

        const requests = [];
        rememberProjectVersion('proj', 'v0');
        globalThis.fetch = async (_url, init = {{}}) => {{
            const headers = new Headers(init.headers || {{}});
            requests.push(headers.get('If-Match') || '');
            const version = `v${{requests.length}}`;
            return new Response(JSON.stringify({{ status: 'ok' }}), {{
                status: 200,
                headers: {{
                    'X-Sonder-Project-Id': 'proj',
                    'X-Sonder-Project-Modified-At': version,
                }},
            }});
        }};
        await fetchProjectJson('/sonder-editor/project/proj/scenes/s/mutations', {{ method: 'POST' }});
        await fetchProjectJson('/sonder-editor/project/proj/scenes/s/mutations', {{ method: 'POST' }});
        assert.deepEqual(requests, ['v0', 'v1']);
        assert.equal(getProjectVersion('proj'), 'v2');
    """)
