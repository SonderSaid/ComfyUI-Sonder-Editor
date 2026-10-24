"""Records what the viewport's draw and guide helpers do, for a characterization fixture.

Run from the repository root: ``python tests/fixtures/characterize_viewport_draw.py [source]``.
The fixture ``viewport_draw_characterization.json`` was recorded from
``viewport_surface.js`` as it stood before ``drawFitted`` and ``guideAtFrame``
were extracted (Output Views Phase 3A); ``tests/test_view_panes_js.py`` replays
the current file and compares.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _function(source: str, name: str) -> str:
    start = source.index("function " + name + "(")
    brace = source.index(") {", start) + 2
    depth, pos = 1, brace + 1
    while depth:
        depth += (source[pos] == "{") - (source[pos] == "}")
        pos += 1
    return source[start:pos]


def harness(source: str) -> str:
    draw = source[source.index("    function drawImageLike("):source.index("    // Scene workspace outline")]
    guide = _function(source, "getGuideAtFrame")
    extracted = ""
    for name in ("drawFitted", "guideAtFrame"):
        if ("export function " + name + "(") in source:
            extracted += _function(source, name) + "\n"
    support = (ROOT / "web/js/media_preview_support.js").as_uri()
    scratch = (ROOT / "web/js/viewport_video_scratch.js").as_uri()
    return r"""
const {mediaFailedToDecode}=await import(""" + json.dumps(support) + r""");
const {createVideoScratchCache,videoDestDownscale,VIDEO_SCRATCH_SCALE_THRESHOLD}=await import(""" + json.dumps(scratch) + r""");
const calls=[];
const ctx={globalAlpha:1,imageSmoothingQuality:'low',globalCompositeOperation:'source-over',
 save(){},restore(){},drawImage(...a){calls.push(a.slice(1).map(v=>Math.round(v*1000)/1000));}};
const state={canvas:{width:640,height:360},compositePassSeq:1};
const videoScratchCache=createVideoScratchCache({createCanvas:()=>null});
const getCanvasContext=()=>ctx, updatePlaybackPerfCounters=()=>{}, noteMediaDecodeFailure=()=>true;
const VIEWPORT_FIT_MODES=new Set(['fit','pad_edge','cover','stretch']);
const clamp=(v,a,b)=>Math.min(b,Math.max(a,v));
""" + _function(source, "fitRect") + "\n" + _function(source, "drawEdgePadBars") + "\n" + extracted + draw + r"""
const out={draw:[],guides:[]};
for (const [w,h] of [[1920,1080],[1080,1920],[640,360],[100,100]]) {
 for (const mode of ['fit','pad_edge','cover','stretch','unknown']) {
  for (const crop of ['center','left','right','top','bottom']) {
   calls.length=0;
   const ok=drawImageLike({naturalWidth:w,naturalHeight:h},{fitMode:mode,cropPosition:crop,opacity:.5});
   out.draw.push({w,h,mode,crop,ok,calls:calls.slice()});
  }
 }
}
let scene=null, total=48, hidden=false;
const getScene=()=>scene, totalFrames=()=>total, isGuideTrackHidden=()=>hidden;
""" + guide + r"""
const lists=[[],[{frame_index:0,id:'a'}],[{frame_index:10,id:'a'},{frame_index:-1,id:'end'},{frame_index:'20',id:'s'}],
 [{frame_index:5,id:'m',muted:true},{frame_index:5,id:'b'},{frame_index:5,id:'c'}],[{frame_index:-1,id:'only'}]];
for (const [i,list] of lists.entries()) for (const flag of [false,true]) for (const frame of [0,4,5,10,19,20,46,47,48,60]) {
 scene={guide_frames:list}; hidden=flag;
 out.guides.push({list:i,hidden:flag,frame,id:getGuideAtFrame(frame)?.id??null});
}
scene=null; out.guides.push({scene:null,id:getGuideAtFrame(3)?.id??null});
console.log(JSON.stringify(out));
"""


def record(source_text: str) -> dict:
    node = shutil.which("node")
    result = subprocess.run([node, "--input-type=module", "-e", harness(source_text)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "web/js/viewport_surface.js"
    data = record(path.read_text(encoding="utf-8"))
    (ROOT / "tests/fixtures/viewport_draw_characterization.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(len(data["draw"]), "draws,", len(data["guides"]), "guides")
