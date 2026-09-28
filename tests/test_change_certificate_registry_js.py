"""The browser's change-certificate registry says "unchanged" only on proof.

`web/js/api_client.js` holds the certificates the server attaches to committed
scene edits and answers `certifiedUnchanged(...)` for consumers that want to keep
a result across newer project versions (`plans/cut-read-fanout.md` §2). Every
doubt must answer "refresh": a gap, a malformed or conflicting certificate, an
evicted link, a version reset or two alias histories being merged. And the
certificate must be registered BEFORE the version notification that consumers
act on, whichever transport delivers first.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = (ROOT / "web/js/api_client.js").as_uri()


def _run(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the certificate registry")
    script = f"""
import assert from 'node:assert/strict';
const client = await import({json.dumps(CLIENT)});
const V = (n) => `2026-09-27T10:00:${{String(n).padStart(2, "0")}}.000000`;
const cert = (base, next, over = {{}}) => ({{
    schema: 1, project_id: "canon", scene_id: "scene",
    base_modified_at: V(base), modified_at: V(next), prompt: false, bridge: false, ...over,
}});
const unchanged = (from, to, over = {{}}) => client.certifiedUnchanged({{
    projectId: "Folder", sceneId: "scene", fromVersion: V(from), toVersion: V(to),
    dependency: "bridge", ...over,
}});
{body}
console.log("ok");
"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("ok")


def test_a_continuous_chain_proves_a_read_unchanged_and_a_gap_does_not():
    _run("""
assert.equal(client.registerChangeCertificate(cert(1, 2), { projectId: "Folder" }), true);
assert.equal(client.registerChangeCertificate(cert(2, 3), { projectId: "Folder" }), true);
assert.equal(unchanged(1, 3), true);
assert.equal(unchanged(2, 3), true);
assert.equal(unchanged(3, 3), true, "no transition at all is trivially unchanged");
assert.equal(unchanged(1, 4), false, "no certificate reaches version 4");
assert.equal(unchanged(3, 1), false, "never backwards");
assert.equal(unchanged(0, 3), false, "nothing leads out of version 0");
assert.equal(unchanged(1, 3, { sceneId: "" }), false);
assert.equal(unchanged(1, 3, { dependency: "anything" }), false);
""")


def test_a_flag_on_the_consumers_scene_breaks_the_chain_and_another_scenes_does_not():
    _run("""
client.registerChangeCertificate(cert(1, 2, { scene_id: "other", bridge: true, prompt: true }),
    { projectId: "Folder" });
client.registerChangeCertificate(cert(2, 3, { prompt: true }), { projectId: "Folder" });
assert.equal(unchanged(1, 3), true, "another scene's edit is scene-confined");
assert.equal(unchanged(1, 3, { dependency: "prompt" }), false);
assert.equal(unchanged(1, 2, { dependency: "prompt" }), true);
""")


def test_a_malformed_or_misreported_certificate_is_refused():
    _run("""
const bad = [
    cert(1, 2, { schema: 2 }), cert(1, 2, { prompt: "false" }), cert(1, 2, { bridge: null }),
    cert(2, 2), cert(3, 2), cert(1, 2, { scene_id: "" }), cert(1, 2, { project_id: 7 }),
    null, "text", [], {},
];
for (const value of bad) {
    assert.equal(client.registerChangeCertificate(value, { projectId: "Folder" }), false,
        JSON.stringify(value));
}
assert.equal(client.registerChangeCertificate(cert(1, 2),
    { projectId: "Folder", reportedVersion: V(9) }), false, "a result version the response did not report");
assert.equal(unchanged(1, 2), false);
""")


def test_aliases_share_one_history_and_duplicates_are_idempotent():
    _run("""
client.registerChangeCertificate(cert(1, 2), { projectId: "Folder" });
assert.equal(client.sameProject("Folder", "canon"), true);
assert.equal(client.sameProject("Folder", "elsewhere"), false);
// The same transition delivered again, by the other transport and alias.
assert.equal(client.registerChangeCertificate(cert(1, 2), { projectId: "canon" }), true);
assert.equal(client.certifiedUnchanged({ projectId: "canon", sceneId: "scene",
    fromVersion: V(1), toVersion: V(2), dependency: "prompt" }), true);
assert.equal(unchanged(1, 2), true);
""")


def test_a_conflicting_certificate_poisons_its_transition_for_good():
    _run("""
client.registerChangeCertificate(cert(1, 2), { projectId: "Folder" });
assert.equal(client.registerChangeCertificate(cert(1, 2, { bridge: true }), { projectId: "Folder" }), false);
assert.equal(unchanged(1, 2), false);
assert.equal(client.registerChangeCertificate(cert(1, 2), { projectId: "Folder" }), false,
    "a late agreeing copy cannot restore a revoked transition");
assert.equal(unchanged(1, 2), false);
""")


def test_only_the_newest_transitions_are_kept_and_eviction_means_refresh():
    _run("""
for (let n = 1; n <= 40; n += 1) client.registerChangeCertificate(cert(n, n + 1), { projectId: "Folder" });
assert.equal(unchanged(1, 41), false, "the oldest links were evicted");
assert.equal(unchanged(20, 41), true);
""")


def test_a_version_reset_forgets_every_certificate():
    _run("""
client.registerChangeCertificate(cert(1, 2), { projectId: "Folder" });
client.rememberProjectVersion("Folder", V(2));
client.resetProjectVersion("Folder", V(1));
assert.equal(unchanged(1, 2), false);
""")


def test_merging_two_populated_histories_restarts_continuity():
    _run("""
client.registerChangeCertificate({ ...cert(1, 2), project_id: "uuid-a" }, { projectId: "folder-a" });
client.registerChangeCertificate({ ...cert(5, 6), project_id: "uuid-b" }, { projectId: "folder-b" });
assert.equal(client.certifiedUnchanged({ projectId: "folder-a", sceneId: "scene",
    fromVersion: V(1), toVersion: V(2), dependency: "bridge" }), true);
// A later response reveals the two histories belong to one project.
client.rememberProjectVersionFromPayload({ project: { project_id: "uuid-a",
    modified_at: V(7) } }, "folder-b");
assert.equal(client.certifiedUnchanged({ projectId: "folder-a", sceneId: "scene",
    fromVersion: V(1), toVersion: V(2), dependency: "bridge" }), false);
""")


def test_a_response_registers_its_certificate_before_consumers_hear_of_the_version():
    _run("""
const seen = [];
client.onProjectVersionChanged((projectId, modifiedAt) => {
    seen.push([projectId, client.certifiedUnchanged({ projectId, sceneId: "scene",
        fromVersion: V(1), toVersion: modifiedAt, dependency: "bridge" })]);
});
client.rememberProjectVersion("Folder", V(1));
seen.length = 0;
const headers = new Map([
    ["x-sonder-project-id", "canon"],
    ["x-sonder-project-modified-at", V(2)],
    ["x-sonder-change-certificate", JSON.stringify(cert(1, 2))],
]);
client.rememberProjectVersionFromResponse(
    { headers: { get: (name) => headers.get(name.toLowerCase()) || null } }, "Folder");
assert.ok(seen.length >= 1);
assert.ok(seen.every(([, proven]) => proven === true), JSON.stringify(seen));
""")


def test_websocket_before_http_and_an_unrelated_get_first():
    _run("""
const heard = [];
client.onProjectVersionChanged((projectId, modifiedAt) => heard.push(
    client.certifiedUnchanged({ projectId, sceneId: "scene", fromVersion: V(1),
        toVersion: modifiedAt, dependency: "bridge" })));
client.rememberProjectVersion("Folder", V(1));
heard.length = 0;
// The websocket echo lands first: registered on receipt, nothing notified yet.
assert.equal(client.registerChangeCertificateFromEvent({ type: "project_updated",
    project_id: "Folder", modified_at: V(2), change: cert(1, 2) }), true);
assert.equal(heard.length, 0);
// An unrelated GET then reports the version first; the certificate is held.
client.rememberProjectVersion("Folder", V(2));
assert.deepEqual(heard, [true]);
// An event whose certificate disagrees with the version it reports is refused.
assert.equal(client.registerChangeCertificateFromEvent({ type: "project_updated",
    project_id: "Folder", modified_at: V(9), change: cert(2, 3) }), false);
// A version reached with no certificate at all answers refresh.
client.rememberProjectVersion("Folder", V(4));
assert.deepEqual(heard, [true, false]);
""")
