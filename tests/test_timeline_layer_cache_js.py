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
    p = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_page_scroll_boundaries_and_disabled():
    source=(ROOT/'web/js/editor_widget.js').read_text(encoding='utf-8')
    a=source.index('    _maybeAutoScrollToPlayhead() {'); b=source.index('\n    _ensureFrameVisible(',a)
    run('const method = ({'+source[a:b]+'})._maybeAutoScrollToPlayhead;'+r'''
const {strict:assert}=await import('node:assert');
for (const span of [1,1.25,2,3.99,4,8.5,16.67,100,173.333]) {
 const h={_settings:{playback:{autoScrollPlayhead:true}},scrollX:0,playhead:span,
 _visibleTimelineFrameSpan:()=>span,_clampScrollX(){this.scrollX=Math.max(0,Math.min(1000-span,this.scrollX));}};
 method.call(h); assert(h.scrollX>span*.8 && h.scrollX<span);
 const page=h.scrollX; method.call(h);assert.equal(h.scrollX,page);
 h.playhead+=span*.2;method.call(h);assert.equal(h.scrollX,page);
 h.playhead=1000;method.call(h);assert.equal(h.scrollX,1000-span);
 method.call(h);assert.equal(h.scrollX,1000-span);
 h.playhead=0;method.call(h);assert.equal(h.scrollX,0);
 h._settings.playback.autoScrollPlayhead=false;h.playhead=500;method.call(h);assert.equal(h.scrollX,0);
}
''')


def test_layer_cache_invalidation_geometry_order_and_context_restoration():
    run('const mod=await import('+json.dumps((ROOT/'web/js/editor_timeline_canvas.js').as_uri())+');'+r'''
const {strict:assert}=await import('node:assert');
function context(){return {ops:[],x:0,y:0,globalAlpha:1,stack:[],
 save(){this.stack.push([this.x,this.y,this.globalAlpha]);},
 restore(){[this.x,this.y,this.globalAlpha]=this.stack.pop();},
 translate(x,y){this.x+=x;this.y+=y;},beginPath(){},rect(...a){this.ops.push(['rect',...a]);},clip(){},
 clearRect(){},fillRect(...a){this.ops.push(['fill',this.fillStyle,...a]);},
 drawImage(...a){this.ops.push(['blit',a[0],a[1]+this.x,a[2]+this.y]);},
 mark(name){this.ops.push([name,this.x,this.y]);}};}
const allocated=[];
function canvas(){const c={width:600,height:200,style:{},ctx:context(),getContext(){return this.ctx;}};allocated.push(c);return c;}
let width=600;
const c=canvas();c.parentElement={getBoundingClientRect:()=>({width})};c.ownerDocument={createElement:canvas};
const h={timelineCanvas:c,_timelineHeight:200,_labelW:100,pixelsPerFrame:2,scrollX:0,scrollY:5,totalFrames:1000,
 isFullscreen:true,_scaleTimeline:1,_snapIndicator:null,
 _timelineRulerHeight:()=>20,_clampScrollX(){this.scrollX=Math.max(0,Math.min(750,this.scrollX));},
 _clampScrollY(){this.scrollY=Math.max(0,Math.min(80,this.scrollY));},_timelineColor:c=>c,
 _frameToX:f=>100+f*2,_playbackWarmState:{entries:[{startFrame:0,endFrame:10,state:'warm'}]},
};
for(const name of ['Ruler','Tracks','Selection','GuideMarkers','Clips','PlayheadTriangle','PlayheadLine','DragSelectOverlay','SnapIndicator','VerticalScrollbar'])h['_draw'+name]=(ctx)=>ctx.mark(name);
mod._renderTimeline(h);const directLine=c.ctx.ops.find(o=>o[0]==='PlayheadLine');
assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false);
assert(h._timelineLayerCache.valid);
c.ctx.ops=[];
assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,true);
assert(c.ctx.ops.some(o=>o[0]==='Ruler'));assert(c.ctx.ops.some(o=>o[0]==='fill'&&o[3]===17));
assert(!c.ctx.ops.some(o=>o[0]==='Clips'));
assert.deepEqual(c.ctx.ops.find(o=>o[0]==='PlayheadLine'),directLine);
assert.deepEqual(c.ctx.ops.filter(o=>o[0]==='blit').map(o=>o.slice(2)),[[0,20],[0,20]]);
assert(c.ctx.ops.findIndex(o=>o[0]==='PlayheadTriangle')<c.ctx.ops.findIndex(o=>o[0]==='blit'));
assert.equal(c.ctx.stack.length,0);assert.equal(c.ctx.y,0);
mod._renderTimeline(h);assert.equal(h._timelineLayerCache.valid,false);
assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false);
for (const prop of ['scrollX','scrollY','pixelsPerFrame','_labelW','_timelineHeight','totalFrames']) {
 h[prop]+=1;assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false,prop);
 assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,true,prop);
}
width+=1;assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false);
for(const prop of ['isDragging','dragType','_dropHoverTarget','_selectionDraftAnchor','_trimItem','_snapIndicator']){
 h[prop]=true;c.ctx.ops=[];assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false,prop);
 assert(c.ctx.ops.some(o=>o[0]==='Clips'));h[prop]=null;
 assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false);
 assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,true);
}
h.scrollY=999;mod._renderTimelinePlaybackFrame(h);assert.equal(h.scrollY,80);
assert.equal(h._timelineLayerCache.key[6],80);assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,true);
const old=h._timelineLayerCache;old.fixed.ctx.isContextLost=()=>true;c.ctx.ops=[];
assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false);assert(c.ctx.ops.some(o=>o[0]==='Clips'));
old.fixed.ctx.isContextLost=()=>false;assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false);
mod._releaseTimelineLayerCache(h);assert.equal(h._timelineLayerCache,null);
assert.equal(old.fixed.canvas.width,0);assert.equal(old.scroll.canvas.height,0);
c.ownerDocument={createElement:()=>({getContext:()=>null})};c.ctx.ops=[];
assert.equal(mod._renderTimelinePlaybackFrame(h).timelineCacheHit,false);assert(c.ctx.ops.some(o=>o[0]==='Clips'));
''')


def test_single_playback_route_full_invalidation_and_release_source_contract():
    source=(ROOT/'web/js/editor_widget.js').read_text(encoding='utf-8')
    canvas=(ROOT/'web/js/editor_timeline_canvas.js').read_text(encoding='utf-8')
    assert source.count('this._renderTimelinePlaybackFrame()')==1
    assert 'this.isPlaying ? this._renderTimelinePlaybackFrame() : this._renderTimeline()' in source
    assert 'export function _renderTimeline(host) {\n    _invalidateTimelineLayerCache(host);' in canvas
    for name in ['_exitFullscreen()', 'destroy()']:
        start=source.index('    '+name+' {')
        assert 'TimelineCanvas._releaseTimelineLayerCache(this)' in source[start:start+450]
    # A second direct timeline painter bypasses the structural invalidation gate.
    for path in (ROOT/'web/js').glob('*.js'):
        if path.name!='editor_timeline_canvas.js':
            text=path.read_text(encoding='utf-8')
            assert 'timelineCanvas.getContext(' not in text, path
            assert 'timelineCanvas?.getContext(' not in text, path



def test_asset_and_project_only_refresh_invalidate_even_without_warm_state():
    source=(ROOT/'web/js/editor_widget.js').read_text(encoding='utf-8')
    a=source.index('    _applyAssetPayload(data, {'); b=source.index('\n    async _fetchAssets(',a)
    asset=source[a:b]
    a=source.index('    async _fetchProjectSettings('); b=source.index('\n    ',source.index('\n    }',a)+6)
    project=source[a:b]
    run('const asset=({'+asset+'})._applyAssetPayload;const project=({'+project+'})._fetchProjectSettings;'+r"""
const {strict:assert}=await import('node:assert');
const getProjectAssetMutationEpoch=()=>1,sessionDiagRecord=()=>{},api={apiURL:x=>x};
const getTemplateById=()=>({id:'free'}),PROJECT_TEMPLATE_KEY='channel_template',DEFAULT_CHANNEL_TEMPLATE_ID='standard';
const DEFAULT_EDITOR_SETTINGS={projectDefaults:{width:1280,height:720}};
const h={projectDir:'test',_timelineLayerCache:{valid:true},_playbackWarmState:null,
 _projectDirName:()=> 'test',_currentSceneAssetIdsForGallery:()=>[],
 _clearPlaybackWarmOverlay(reason,opts){assert.equal(opts.render,false);},
 _renderTimeline(){this._timelineLayerCache.valid=false;},
 _hasPendingProjectMutations:()=>false,_maybeHealFrameConstraint:async()=>{},_maybeHealDimensionConstraint:async()=>{},
 _syncSceneResolutionControls(){},_updateViewportHeader(){},_resizeViewportCanvas(){}};
assert.equal(asset.call(h,{assets:[{path:'a.mp4',asset_type:'video',name:'renamed'}]}),true);
assert.equal(h._timelineLayerCache.valid,false);assert.equal(h._pathToAsset['a.mp4'].name,'renamed');
h._timelineLayerCache.valid=true;
globalThis.fetch=async()=>({ok:true,json:async()=>({fps:30,metadata:{reference_frame_threshold:40}})});
await project.call(h);
assert.equal(h._referenceFrameThreshold,40);assert.equal(h.fps,30);assert.equal(h._timelineLayerCache.valid,false);
""")
