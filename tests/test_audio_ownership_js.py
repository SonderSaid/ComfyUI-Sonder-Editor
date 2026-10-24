"""Output Views 3A: one audible source per editor page (audio_ownership.js)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_claim_release_suspend_and_never_moving_on_its_own():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    module = (ROOT / "web" / "js" / "audio_ownership.js").as_uri()
    script = f"""
const {{ createAudioOwnership, TIMELINE }} = await import({json.dumps(module)});
const muted = [];
const audio = createAudioOwnership({{ setTimelineMuted: value => muted.push(value) }});
const heard = [];
audio.subscribe((owner, suspended) => heard.push([owner, suspended]));
const out = {{ start: [audio.owner(), audio.audible(TIMELINE), audio.audible('pane:0')] }};
audio.claim('pane:0');
out.claimed = [audio.audible('pane:0'), audio.audible('pane:1'), audio.audible(TIMELINE)];
audio.claim('pane:1');                 // another pane takes it; the first goes quiet
out.moved = [audio.audible('pane:0'), audio.audible('pane:1')];
audio.release('pane:0');               // not the owner: nothing changes
out.notOwner = audio.owner();
audio.suspend(true);                   // gallery Inspect: the pane owner is quiet, the timeline is untouched
out.suspended = [audio.audible('pane:1'), audio.owner()];
audio.suspend(false);
out.resumed = audio.audible('pane:1');
audio.release('pane:1');
out.back = [audio.owner(), audio.audible(TIMELINE)];
// Fifty random sequences: exactly one source is ever audible.
let seed = 7;
const rand = n => {{ seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; }};
let ok = true;
for (let run = 0; run < 50; run++) {{
    const a = createAudioOwnership();
    for (let step = 0; step < 30; step++) {{
        const id = `pane:${{rand(4)}}`;
        [() => a.claim(id), () => a.release(id), () => a.claim(TIMELINE), () => a.release(a.owner())][rand(4)]();
        const audible = [TIMELINE, 'pane:0', 'pane:1', 'pane:2', 'pane:3'].filter(x => a.audible(x));
        if (audible.length !== 1) ok = false;
    }}
}}
out.oneAudible = ok;
audio.claim('pane:2');
audio.destroy();
out.destroyed = [audio.owner(), muted.at(-1)];
out.muted = muted;
out.heard = heard.length;
console.log('RESULT=' + JSON.stringify(out));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[7:])
    assert out["start"] == ["timeline", True, False]
    assert out["claimed"] == [True, False, False]
    assert out["moved"] == [False, True]
    assert out["notOwner"] == "pane:1"
    assert out["suspended"] == [False, "pane:1"] and out["resumed"] is True
    assert out["back"] == ["timeline", True]
    assert out["oneAudible"]
    assert out["destroyed"] == ["timeline", False]
    # The timeline is muted while a pane owns the sound, never by Inspect.
    assert out["muted"] == [True, True, True, True, False, True, False]
