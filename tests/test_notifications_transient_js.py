"""The notification core's `transient` lifetime (hardening Phase 5): an
immediate refusal that changed nothing follows the user's configured warning
duration, but is never sticky, and its lifetime never leaks to a sticky
notification on another source."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_transient_refusals_close_on_their_own_and_follow_the_setting(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    core = json.dumps((ROOT / "web/js/editor_notifications.js").as_uri())
    script = f"""
import assert from 'node:assert/strict';
const armed = [];
globalThis.setTimeout = (fn, ms) => (armed.push(ms), armed.length);
globalThis.clearTimeout = () => {{}};
const N = await import({core});
const last = () => armed.at(-1);
// Setting at 0 ("stay until dismissed"): a transient warning still closes.
N.configureNotifications({{ warning: 0 }});
let before = armed.length;
N.notifyWarning('sticky', {{ source: 'server-refused' }});
assert.equal(armed.length, before, 'a plain warning stays sticky');
N.notifyWarning('Stage separately', {{ source: 'drop-refused', transient: true }});
assert.equal(last(), N.TRANSIENT_MS);
// A configured duration wins over the fallback.
N.configureNotifications({{ warning: 2500 }});
N.notifyWarning('again', {{ source: 'drop-refused-2', transient: true }});
assert.equal(last(), 2500);
// An explicit duration still wins over both.
N.notifyWarning('explicit', {{ source: 'drop-refused-3', transient: true, durationMs: 900 }});
assert.equal(last(), 900);
// Its own source keeps the sticky one sticky: a later update there arms nothing.
N.configureNotifications({{ warning: 0 }});
before = armed.length;
N.notifyWarning('sticky again', {{ source: 'server-refused' }});
assert.equal(armed.length, before);
// Progress is never transient.
before = armed.length;
N.notify({{ tier: 'progress', message: 'working', source: 'p', transient: true }});
assert.equal(armed.length, before);
console.log('ok');
"""
    path = tmp_path / "transient.mjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
