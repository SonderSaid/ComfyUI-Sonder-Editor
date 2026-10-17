"""The mounted tab names its page after the project; the canvas and the Sonder
App keep their own titles."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_project_identity_reports_each_name_change_once():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the JavaScript behavior probe")
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    a = source.index("    _updateProjectIdentity() {")
    b = source.index("\n    _showSceneDropdown(", a)
    c = source.index("    _projectDirName() {")
    d = source.index("\n    }\n", c) + 7
    script = "const methods=(class{" + source[a:b] + source[c:d] + "}).prototype;" + r'''
import assert from 'node:assert/strict';
const names=[];
const h=Object.assign(Object.create(methods),{projectDir:'MyProject',onProjectIdentityChanged:n=>names.push(n),_reportedProjectName:undefined});
h._updateProjectIdentity();h._updateProjectIdentity();
// A mounted switch can swap the folder id for its full path: same name, no report.
h.projectDir='D:\\Projects\\MyProject';h._updateProjectIdentity();
h.projectDir='Other';h._fsProjectLabel={textContent:''};h._updateProjectIdentity();
assert.deepEqual(names,['MyProject','Other']);
assert.equal(h._fsProjectLabel.textContent,'Other');
// Without the option (the canvas and Sonder App), nothing is reported.
const quiet=Object.assign(Object.create(methods),{projectDir:'X',onProjectIdentityChanged:null});
quiet._updateProjectIdentity();
console.log('ok');
'''
    p = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "ok"


def test_only_the_mounted_tab_sets_the_project_title():
    tab = (ROOT / "web/js/tab_entry.js").read_text(encoding="utf-8")
    mounted = tab[tab.index("async function main"):]
    sdk = tab[tab.index("async function runSdkTab"):tab.index("async function main")]
    assert "onProjectIdentityChanged" in mounted
    assert "· Sonder Editor" in mounted
    assert "onProjectIdentityChanged" not in sdk
    controller = (ROOT / "web/js/editor_node_controller.js").read_text(encoding="utf-8")
    assert "onProjectIdentityChanged" not in controller
