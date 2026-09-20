import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the JavaScript behavior probe")
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_scratch_bounds_lru_same_pass_and_unavailable_context():
    run('const mod = await import(' + json.dumps((ROOT/'web/js/viewport_video_scratch.js').as_uri()) + ');' + r'''
const { strict: assert } = await import('node:assert');
const { createVideoScratchCache: cache, videoDestDownscale: scale } = mod;
for (const mode of ['fit','pad_edge','stretch']) assert.equal(scale(mode,100,200,100,50),.25);
assert.equal(scale('cover',100,200,100,50),1);
assert.equal(scale('fit',0,100,50,50),Infinity);
const allocated=[];
const createCanvas=()=>{const c={getContext:()=>({})}; allocated.push(c); return c;};
const pool=cache({createCanvas,maxEntries:2,maxTotalPixels:500});
const a=pool.acquire(10,10,1), b=pool.acquire(20,10,1);
assert.equal(pool.acquire(10,10,1),a);
assert.equal(pool.acquire(15,10,1),null);
assert.equal(pool.acquire(30,20,2),null);
assert.equal(pool.acquire(10,10,2),a);
const c=pool.acquire(15,10,2);
assert(c); assert.equal(b.canvas.width,0); assert.equal(a.canvas.width,10);
assert.deepEqual(pool.stats(),{entries:2,totalPixels:250});
pool.clear(); assert(allocated.every(c=>c.width===0 && c.height===0));
assert.equal(cache().acquire(10,10,1),null);
assert.equal(cache({createCanvas:()=>({getContext:()=>null})}).acquire(10,10,1),null);
assert.equal(cache({createCanvas:()=>{throw Error('unavailable')}}).acquire(10,10,1),null);
''')


def test_playback_draw_uses_one_to_one_copy_and_scratch_for_every_fit_draw():
    source = (ROOT/'web/js/viewport_surface.js').read_text(encoding='utf-8')
    # Execute the real closure bodies with a stateful recording canvas, leaving
    # media scheduling out of this pixel-routing/return-contract test.
    def function(name):
        start = source.index('function '+name+'(')
        brace = source.index('{', source.index(') {', start)) if name != 'drawImageLike' else source.index(') {', start)+2
        depth=1; pos=brace+1
        while depth:
            depth += (source[pos]=='{')-(source[pos]=='}'); pos+=1
        return source[start:pos]
    draw = source[source.index('    function drawImageLike('):source.index('    // Scene workspace outline')]
    run('const mod = await import(' + json.dumps((ROOT/'web/js/viewport_video_scratch.js').as_uri()) + ');\n' + r'''
const { strict: assert } = await import('node:assert');
const {createVideoScratchCache,videoDestDownscale,VIDEO_SCRATCH_SCALE_THRESHOLD}=mod;
function context() {
 const c={globalAlpha:.4,imageSmoothingQuality:'low',globalCompositeOperation:'source-over',calls:[],stack:[],
 save(){this.stack.push([this.globalAlpha,this.imageSmoothingQuality,this.globalCompositeOperation]);},
 restore(){[this.globalAlpha,this.imageSmoothingQuality,this.globalCompositeOperation]=this.stack.pop();},
 drawImage(...a){this.calls.push({a,op:this.globalCompositeOperation,quality:this.imageSmoothingQuality});}};
 return c;
}
const ctx=context(), state={canvas:{width:554,height:313},compositePassSeq:1};
const canvases=[];
const videoScratchCache=createVideoScratchCache({createCanvas:()=>{const c={ctx:context(),getContext(){return this.ctx;}};canvases.push(c);return c;}});
const getCanvasContext=()=>ctx, updatePlaybackPerfCounters=()=>{};
const VIEWPORT_FIT_MODES=new Set(['fit','pad_edge','cover','stretch']);
const clamp=(v,a,b)=>Math.min(b,Math.max(a,v));
''' + function('fitRect') + '\n' + function('drawEdgePadBars') + '\n' + draw + r'''
const video={videoWidth:1920,videoHeight:1088,readyState:4};
for (const mode of VIEWPORT_FIT_MODES) {
 ctx.calls=[];
 assert.equal(drawImageLike(video,{fitMode:mode,allowScratch:true}),true);
 assert(ctx.calls.length>0); assert(ctx.calls.every(c=>c.a[0]===canvases[0]));
 assert.equal(ctx.globalAlpha,.4); assert.equal(ctx.imageSmoothingQuality,'low');
 assert.equal(canvases[0].width,1920); assert.equal(canvases[0].height,1088);
 assert.equal(canvases[0].ctx.globalCompositeOperation,'source-over');
 const copy=canvases[0].ctx.calls.at(-1);
 assert.deepEqual(copy.a,[video,0,0]); assert.equal(copy.op,'copy');
}
// Mismatched aspect forces pad bars, so all source sites are exercised.
state.canvas.height=400; ctx.calls=[];
drawImageLike(video,{allowScratch:true}); assert(ctx.calls.length>=3);
assert(ctx.calls.every(c=>c.a[0]===canvases[0]));
for (const options of [{fitMode:'fit'}, {fitMode:'fit',allowScratch:true}]) {
 if(options.allowScratch) video.readyState=1;
 ctx.calls=[]; assert.equal(drawImageLike(video,options),true);
 assert.equal(ctx.calls.length,1); assert.equal(ctx.calls[0].a[0],video);
 assert.equal(state.lastVideoScratchDraw,false);
}
video.readyState=4;
canvases[0].ctx.isContextLost=()=>true;ctx.calls=[];
drawImageLike(video,{allowScratch:true});assert.equal(ctx.calls[0].a[0],video);
let lost=false;canvases[0].ctx.isContextLost=()=>lost;
const originalDraw=canvases[0].ctx.drawImage;
canvases[0].ctx.drawImage=()=>{lost=true;};ctx.calls=[];
drawImageLike(video,{allowScratch:true});assert.equal(ctx.calls[0].a[0],video);
lost=false;canvases[0].ctx.drawImage=originalDraw;ctx.calls=[];
drawImageLike(video,{allowScratch:true});assert.equal(ctx.calls[0].a[0],canvases[0]);
video.readyState=4; state.canvas.width=1920;state.canvas.height=1088;ctx.calls=[];
drawImageLike(video,{allowScratch:true});assert.equal(ctx.calls[0].a[0],video);
''')


def test_scratch_only_playback_and_lifecycle_telemetry_wired():
    source = (ROOT/'web/js/viewport_surface.js').read_text(encoding='utf-8')
    assert source.count('allowScratch: true') == 1
    assert 'if (reason === "scene-switch") videoScratchCache.clear();' in source
    assert 'function clearMediaCache() {\n        videoScratchCache.clear();' in source
    assert 'scratch: state.lastVideoScratchDraw' in source
    for name in ['videoScratchDraws','videoScratchDirectDraws','videoScratchPeakEntries']:
        assert f'{name}: 0' in source
        assert f'{name}: counters?.{name} || 0' in source
