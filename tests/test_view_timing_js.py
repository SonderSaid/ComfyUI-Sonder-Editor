"""Output Views Phase 3B in the browser: view_timing.js and text_hash.js.

- parity with server/view_timing.py: the timing reader, the two-lane hash and
  the content projection (``contentStatus`` against records the Python side
  builds, ``test_view_timing.parity_cases``);
- golden mapping tables for ``resolveLink``: trusted (a take) and authored
  timing, frame-centre boundaries, rates other than the scene's, the length
  check, every unlinked reason and the Editor context choice.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import view_timing as vt

from test_view_timing import TIMING_CASES, parity_cases

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "js"


def node_run(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stderr
    return json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))[7:])


def module(name):
    return json.dumps((JS / name).as_uri())


def test_content_status_matches_the_server_records(tmp_path):
    cases = parity_cases()
    # Too long for a command line: the cases travel in a file.
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases), encoding="utf-8")
    out = node_run(f"""
const T = await import({module("view_timing.js")});
const {{ readFileSync }} = await import('node:fs');
const cases = JSON.parse(readFileSync({json.dumps(str(data))}, 'utf-8'));
const out = cases.map(c => {{
    const assets = new Map(c.assets.map(a => [T.normalizeSource(a.path), a]));
    return [c.name, T.contentStatus(c.scene, c.content, {{ fps: 24, width: 640, height: 360,
        assetForSourcePath: p => assets.get(T.normalizeSource(p)) || null, assetsLoaded: c.assets_loaded }})];
}});
console.log('RESULT=' + JSON.stringify(out));
""")
    assert out == [[case["name"], case["expect"]] for case in cases]


def test_content_status_unknown_without_a_record_and_changed_with_another_size():
    case = parity_cases()[0]
    out = node_run(f"""
const T = await import({module("view_timing.js")});
const c = {json.dumps(case)};
const helpers = {{ fps: 24, width: 640, height: 360, assetsLoaded: true }};
console.log('RESULT=' + JSON.stringify([T.contentStatus(c.scene, null, helpers), T.contentStatus(c.scene, {{...c.content, v: 2}}, helpers),
    T.contentStatus(c.scene, c.content, {{...helpers, width: 1280}}), T.contentStatus(c.scene, c.content, {{...helpers, fps: 25}})]));
""")
    assert out == ["unknown", "unknown", "changed", "changed"]


def test_timing_reader_and_hash_parity():
    strings = ["", "a", "é", "🎬", "\ud83d", "ΑΣ", " \x7f\"\\", "x" * 300]
    out = node_run(f"""
const T = await import({module("view_timing.js")});
const H = await import({module("text_hash.js")});
const timings = {json.dumps([raw for raw, _ in TIMING_CASES])};
const strings = {json.dumps(strings)};
console.log('RESULT=' + JSON.stringify({{
    timing: timings.map(T.normalizeTiming),
    hash: strings.map(H.twoLaneHash),
    canonical: T.canonicalText(['a', 1, true, ['é', -1, false], '🎬']),
    micro: [0.1, 1/3, -0.5, null, '', true, false, 'abc', NaN, Infinity, '0.25', [1]].map(T.micro),
}}));
""")
    assert out["timing"] == [expected for _, expected in TIMING_CASES]
    assert out["hash"] == [vt.two_lane_hash(text) for text in strings]
    assert out["canonical"] == vt.canonical(["a", 1, True, ["é", -1, False], "🎬"])
    assert out["micro"] == [vt.micro(v) for v in (0.1, 1 / 3, -0.5, None, "", True, False, "abc", float("nan"), float("inf"), "0.25")] + [-1]


def test_prompt_hash_fallback_is_unchanged():
    # The canvas Controls fingerprint on LAN origins (no Web Crypto) now
    # delegates to text_hash.js: same text, same value as the inline code it replaced.
    out = node_run(f"""
const H = await import({module("text_hash.js")});
function legacy(text) {{
    let a = 0x811c9dc5, b = 0x9e3779b9;
    for (let i = 0; i < text.length; i++) {{ const c = text.charCodeAt(i); a = Math.imul(a ^ c, 0x01000193); b = Math.imul(b ^ c, 0x85ebca6b); }}
    return `${{(a >>> 0).toString(16).padStart(8, "0")}}${{(b >>> 0).toString(16).padStart(8, "0")}}`;
}}
const texts = ['', '{{"1":{{"class_type":"X"}}}}', 'é🎬', 'x'.repeat(5000)];
console.log('RESULT=' + JSON.stringify(texts.map(t => H.twoLaneHash(t) === legacy(t))));
""")
    assert out == [True, True, True, True]
    source = (JS / "controls_host_canvas.js").read_text(encoding="utf-8")
    assert "return `local:${twoLaneHash(text)}`;" in source


LINK = r"""
const T = await import(TIMING_URL);
const ctx = (over = {}) => ({ project_folder: 'P', scene_id: 's1', fps: 24, render_start: 10, render_end: 40, generation_start: 14,
    generation_end: 34, source_frame_count: 30, ...over });
const editor = { projectId: 'P', sceneId: 's1', fps: 24, editorKey: '' };
const video = (over = {}) => ({ kind: 'video', file: { filename: 'o.mp4' }, ...over });
const authored = (over = {}) => ({ mode: 'editor_window', anchor: 'render_start', source_offset: 0, fps: 'editor', ...over });
const at = (link, f) => { const m = link.map(f + 0.5); return m.inRange ? Math.round(m.sourceTime * 1e6) / 1e6 : null; };
const out = {};
"""


def link_run(body: str):
    return node_run(LINK.replace("TIMING_URL", module("view_timing.js")) + body + "\nconsole.log('RESULT=' + JSON.stringify(out));")


def test_trusted_take_maps_like_its_take_clip():
    out = link_run("""
// A take placed at scene 12..38, its clip's source in at 2 (scene-frame units).
const take = video({ fps: 24, scene_map: { scene_id: 's1', scene_start: 12, scene_end: 38, source_offset: 2, fps: 24 } });
const link = T.resolveLink({ entry: take, contexts: { '5': ctx() }, editor });
out.kind = link.kind;
out.frames = [11, 12, 13, 37, 38].map(f => at(link, f));
// The same as the editor's own clip: (source_in + f - start + 0.5) / F.
out.expected = [null, (2 + 0.5) / 24, (3 + 0.5) / 24, (27 + 0.5) / 24, null].map(v => v === null ? null : Math.round(v * 1e6) / 1e6);
out.required = link.required;
out.short = T.resolveLink({ entry: take, contexts: { '5': ctx() }, editor, durationSec: 27 / 24 }).reason;
out.long = T.resolveLink({ entry: take, contexts: { '5': ctx() }, editor, durationSec: 28 / 24 }).linked;
// A take needs its Editor's context: a reused one without it, another scene, a retimed scene.
out.reused = T.resolveLink({ entry: take, contexts: null, fresh: false, editor }).reason;
out.recovered = T.resolveLink({ entry: take, contexts: null, fresh: null, editor }).reason;  // from history: freshness unknown
out.otherScene = T.resolveLink({ entry: take, contexts: { '5': ctx({ scene_id: 's2' }) }, editor }).reason;
out.retimed = T.resolveLink({ entry: take, contexts: { '5': ctx() }, editor: { ...editor, fps: 30 } }).reason;
out.otherProject = T.resolveLink({ entry: take, contexts: { '5': ctx({ project_folder: 'Q' }) }, editor }).reason;
""")
    assert out["kind"] == "trusted"
    assert out["frames"] == out["expected"]
    assert out["required"] == 28
    assert out["short"] == "too_short" and out["long"] is True
    assert out["recovered"] == "reused"
    assert out["reused"] == "reused" and out["otherScene"] == "other_scene" and out["retimed"] == "fps"
    assert out["otherProject"] == "other_scene"


@pytest.mark.parametrize("rate, offset", [(24, 0), (12, 0), (25, 3), (30, 0)])
def test_authored_timing_samples_frame_centres_at_the_output_rate(rate, offset):
    out = link_run(f"""
const link = T.resolveLink({{ entry: video(), timing: authored({{ fps: {rate}, source_offset: {offset} }}), contexts: {{ '5': ctx() }}, editor }});
out.range = [link.S0, link.S1, link.Fm, link.required];
out.frames = Array.from({{ length: 32 }}, (_, i) => i + 9).map(f => at(link, f));
""")
    S0, S1 = 10, 40
    assert out["range"][:3] == [S0, S1, rate]
    import math
    expected = []
    for f in range(9, 41):
        if not S0 <= f < S1:
            expected.append(None)
            continue
        s = offset + math.floor((f + 0.5 - S0) * rate / 24 + 1e-9)
        expected.append(round((s + 0.5) / rate * 1e6) / 1e6)
    assert out["frames"] == expected
    # The last frame centre's source frame is the last one required.
    assert out["range"][3] == offset + math.floor((S1 - S0 - 0.5) * rate / 24 + 1e-9) + 1


def test_authored_generation_anchor_reasons_and_context_choice():
    out = link_run("""
const gen = T.resolveLink({ entry: video(), timing: authored({ anchor: 'generation_start' }), contexts: { '5': ctx() }, editor });
out.gen = [gen.S0, gen.S1];
out.audio = (() => { const l = T.resolveLink({ entry: { kind: 'audio', file: {} }, timing: authored({ source_offset: 12, fps: 24 }), contexts: { '5': ctx() }, editor });
    return [l.audioMap(10.5).sourceTime, l.audioMap(20).sourceTime, l.audioMap(41).inRange]; })();
out.reasons = [
    T.resolveLink({ entry: video(), contexts: { '5': ctx() }, editor }).reason,
    T.resolveLink({ entry: video(), timing: authored(), contexts: {}, editor }).reason,
    T.resolveLink({ entry: video(), timing: authored(), contexts: { '5': ctx({ fps: 25 }) }, editor }).text,
    T.resolveLink({ entry: { kind: 'image', file: {} }, timing: authored(), contexts: { '5': ctx() }, editor }).reason,
    T.resolveLink({ entry: { kind: 'animated_image', file: {} }, timing: authored(), contexts: { '5': ctx() }, editor }).reason,
];
const two = { '5': ctx(), '57:12': ctx() };
out.several = T.resolveLink({ entry: video(), timing: authored(), contexts: two, editor }).reason;
out.own = T.resolveLink({ entry: video(), timing: authored(), contexts: two, editor: { ...editor, editorKey: '5' } }).contextKey;
// An inner Editor's key never matches a root id by accident.
out.inner = T.resolveLink({ entry: video(), timing: authored(), contexts: { '12': ctx(), '57:12': ctx() }, editor: { ...editor, editorKey: '12' } }).contextKey;
out.oneMatching = T.resolveLink({ entry: video(), timing: authored(), contexts: { '5': ctx(), '6': ctx({ scene_id: 's9' }) }, editor }).contextKey;
out.unknownLength = T.resolveLink({ entry: video(), timing: authored(), contexts: { '5': ctx() }, editor, durationSec: Infinity }).linked;
""")
    assert out["gen"] == [14, 34]
    assert out["audio"][0] == pytest.approx(0.5 / 24 + 0.5) and out["audio"][1] == pytest.approx(10 / 24 + 0.5) and out["audio"][2] is False
    assert out["reasons"] == ["no_timing", "no_editor", "Made at 25 fps; this scene plays at 24", "not_linkable", "not_linkable"]
    assert out["several"] == "several_editors" and out["own"] == "5" and out["inner"] == "12" and out["oneMatching"] == "5"
    assert out["unknownLength"] is True
