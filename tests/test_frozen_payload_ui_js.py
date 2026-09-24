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
