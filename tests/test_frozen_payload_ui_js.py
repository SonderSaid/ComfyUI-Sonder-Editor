"""Execute production callback closures with controlled delayed transport/DOM state."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    result = subprocess.run(["node", "--input-type=module", "-e", script],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_provenance_callbacks_use_route_identity_and_reject_old_lifecycles():
    source = (ROOT / "web/js/shared_asset_gallery.js").read_text(encoding="utf-8")
    body = source.split("export function mountSharedAssetGallery(container, options = {}) {", 1)[1].split("    const initialSettings", 1)[0]
    result = run_js("""
let dir = 'D:\\samples\\Project-A';
const state = {destroyed:false, overlayState:{open:false}};
const requests=[], errors=[], adopted=[];
const epochs = {'Project-A':7, 'Project-B':9};
const currentProjectDir=()=>dir;
const projectIdFromDir=d=>d.split(/[/\\\\]/).pop();
const getProjectAssetMutationEpoch=id=>epochs[id];
const notifyError=e=>errors.push(e);
const setData=d=>adopted.push(d);
const render=()=>{}, renderInspectOverlay=()=>{};
const requestProjectAssetRefresh=d=>new Promise((resolve,reject)=>requests.push({d,resolve,reject}));
""".replace("'D:\\samples\\Project-A'", json.dumps("D:\\samples\\Project-A")) + body + """
const asset={asset_id:'a',provenance_revision:'r'};
const response=epoch=>({epoch,payload:{assets:[{...asset}],provenance:{a:{prompt:'full'}}}});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
ensureProvenance([asset]);
requests[0].resolve(response(7)); await tick();
const full=adopted[0].assets[0].generation_params.prompt;
ensureProvenance([asset]);
dir='Project-B'; ensureProvenance([asset]);
requests[1].reject(new Error('old project')); await tick();
const newerPending=provenancePending!==null;
requests[2].resolve(response(9)); await tick();
ensureProvenance([asset]);
dir='Empty'; syncProvenanceProject();
dir='Project-B'; syncProvenanceProject();
ensureProvenance([asset]);
requests[3].resolve(response(9)); await tick();
const abaPending=provenancePending!==null;
state.destroyed=true;
requests[4].reject(new Error('destroyed')); await tick();
console.log(JSON.stringify({ids:requests.map(x=>x.d.projectId),full,newerPending,abaPending,errors,adopted:adopted.length}));
""")
    assert result == {"ids": ["Project-A", "Project-A", "Project-B", "Project-B", "Project-B"],
                      "full": "full", "newerPending": True, "abaPending": True, "errors": [], "adopted": 2}


def test_export_control_sync_preserves_request_failure():
    source = (ROOT / "web/js/editor_timeline_export_panel.js").read_text(encoding="utf-8")
    sync = source.split("const syncState = () => {", 1)[1].split("\n    };", 1)[0]
    result = run_js("""
const host={_sceneHasAudio:()=>true};
const syncPresetDescription=()=>{},setButtonVariant=()=>{};
const customPanel={style:{}},presetSelect={value:'normal'};
const includeAudio={checked:false},includeVideo={checked:true};
const placeAsTake={checked:false},linkedTakePlacement={},takePlacementMuted={},exportBtn={};
const errorEl={},ui={requestError:'An export is already running.'};
const syncState=()=>{ """ + sync + """ };
syncState(); const refusal=errorEl.textContent;
includeVideo.checked=false; syncState(); const invalid=errorEl.textContent;
includeVideo.checked=true; syncState(); const retained=errorEl.textContent;
ui.requestError=''; syncState();
console.log(JSON.stringify({refusal,invalid,retained,retry:errorEl.textContent}));
""")
    assert result == {"refusal": "An export is already running.", "invalid": "Enable video or audio to export",
                      "retained": "An export is already running.", "retry": ""}
