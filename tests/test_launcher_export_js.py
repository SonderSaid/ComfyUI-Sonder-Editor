import json
import shutil
import subprocess
from pathlib import Path

def test_download_safe_filename_blob_cleanup_and_error():
    root = Path(__file__).resolve().parents[1]
    module = (root/'web/js/launcher_export.js').as_uri()
    script = r"""
import assert from 'node:assert/strict';
const {exportFilename,downloadCuratedWorkflow}=await import(MODULE);
assert.equal(exportFilename('CON'), '_CON.json');
assert.equal(exportFilename('../bad:file?'), '.._bad_file_.json');
assert.equal(exportFilename('...'), 'Workflow.json');
let appended=0,removed=0,clicked=0,revoked=0;
globalThis.document={body:{append(a){appended++;}},createElement(){return {click(){clicked++;assert.equal(this.download,'Workflow.json');},remove(){removed++;}};}};
URL.createObjectURL=blob=>{assert.ok(blob instanceof Blob);return 'blob:test';};
URL.revokeObjectURL=url=>{assert.equal(url,'blob:test');revoked++;};
globalThis.setTimeout=f=>f();
globalThis.fetch=async url=>{assert.equal(url,'/sonder-editor/sdk/templates/abc/export');return {ok:true,blob:async()=>new Blob(['{}'])};};
assert.equal(await downloadCuratedWorkflow({template_id:'abc'}),'Workflow.json');
assert.deepEqual([appended,removed,clicked,revoked],[1,1,1,1]);
globalThis.fetch=async()=>({ok:false,status:400,json:async()=>({error:'Menu too large'})});
await assert.rejects(()=>downloadCuratedWorkflow({template_id:'abc'}),/Menu too large/);
assert.equal(clicked,1);
""".replace('MODULE', json.dumps(module))
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
