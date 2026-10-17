"""Timeline snapping: the pure target/search module, the editor's move, ruler
and Razor snap methods (sliced from editor_widget.js, which Node cannot
import), and the snap settings' defaults."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(script, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the JavaScript behavior probe")
    path = tmp_path / "snap_test.mjs"
    path.write_text(script, encoding="utf-8")
    p = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "ok"


def url(name):
    return json.dumps((ROOT / "web/js" / name).as_uri())


def test_snap_candidates_and_nearest(tmp_path):
    run(f"const snap=await import({url('timeline_snap.js')});" + r'''
import assert from 'node:assert/strict';
const all={playhead:true,selection:true,clipEdges:true,audioEdges:true,guides:true,promptSections:true,referenceItems:true,sceneBounds:true};
const scene={clips:[{clip_id:'c1',timeline_start_frame:10,timeline_end_frame:40}],
  audio_tracks:[{track_id:'a1',timeline_start_frame:50,timeline_end_frame:70}],
  guide_frames:[{frame_index:2},{frame_index:-1}],
  prompt_sections:[{start_frame:3,end_frame:20},{start_frame:20,end_frame:30}],
  reference_items:[{reference_item_id:'r1',start_frame:60,end_frame:-1}]};
const base={playhead:5,selectionStart:7,selectionEnd:9,totalFrames:100,targets:all};
assert.deepEqual(snap.collectSnapCandidates(scene,base),[5,7,9,10,40,50,70,2,99,3,20,20,30,60,100,0,100]);
// Each target type is its own switch; Reference edges use the effective end.
assert.deepEqual(snap.collectSnapCandidates(scene,{...base,targets:{referenceItems:true}}),[60,100]);
assert.deepEqual(snap.collectSnapCandidates(scene,{...base,targets:{}}),[]);
// Exclusion is type-qualified: guide 2 does not drop prompt section 2's...
// and prompt index 1 does not drop the guide at frame 1.
const ex=k=>snap.collectSnapCandidates(scene,{...base,targets:{guides:true,promptSections:true},exclude:new Set(k)});
assert.deepEqual(ex(['guide:2']),[99,3,20,20,30]);
assert.deepEqual(ex(['prompt:1']),[2,99,3,20]);
assert.deepEqual(ex(['clip:1','audio:0']),[2,99,3,20,20,30]);
// Nearest pairing: start or end, whichever is closer; ties keep the earlier point.
assert.deepEqual(snap.nearestSnap([12,42],[45],5),{distance:3,delta:3,frame:45,point:42});
assert.equal(snap.nearestSnap([12,42],[48],5),null);
assert.equal(snap.nearestSnap([10,20],[13,17],5).point,10);
// The snap-back guard never picks an excluded frame.
assert.equal(snap.nearestSnap([11],[10],5,new Set([10])),null);
// Razor: inside the item, and on the pointer's side of each linked partner.
assert.deepEqual(snap.razorSnapTargets([5,10,15,20],{start:5,end:20},[],12),[10,15]);
assert.deepEqual(snap.razorSnapTargets([6,10,15],{start:5,end:20},[{start:8,end:30}],12),[10,15],'pointer inside the partner');
assert.deepEqual(snap.razorSnapTargets([6,8,10],{start:5,end:20},[{start:8,end:30}],7),[6,8],'pointer before the partner');
console.log('ok');
''', tmp_path)


def test_snap_settings_defaults(tmp_path):
    run(f"globalThis.localStorage={{getItem(){{return null;}},setItem(){{}}}};const s=await import({url('editor_settings.js')});" + r'''
import assert from 'node:assert/strict';
const tb=s.normalizeEditorSettings().timelineBehavior;
assert.equal(tb.snapPlayhead,false);assert.equal(tb.snapRazor,false);assert.equal(tb.snapTargets.referenceItems,true);
assert.ok(s.SNAP_TARGET_OPTIONS.some(o=>o.key==='referenceItems'));
// Settings saved before these existed read the defaults; saved choices survive.
const old=s.normalizeEditorSettings({timelineBehavior:{snapTargets:{clipEdges:false}}}).timelineBehavior;
assert.equal(old.snapTargets.referenceItems,true);assert.equal(old.snapTargets.clipEdges,false);assert.equal(old.snapPlayhead,false);
const on=s.normalizeEditorSettings({timelineBehavior:{snapPlayhead:1,snapRazor:'yes'}}).timelineBehavior;
assert.equal(on.snapPlayhead,true);assert.equal(on.snapRazor,true);
console.log('ok');
''', tmp_path)


def test_editor_move_ruler_and_razor_snapping(tmp_path):
    source = (ROOT / "web/js/editor_widget.js").read_text(encoding="utf-8")
    a = source.index("    _snapCandidates({ exclude = [], playhead = true } = {}) {")
    b = source.index("\n    _setRazorMode(on) {", a)
    run(f"const {{collectSnapCandidates,nearestSnap,razorSnapTargets,snapItemKey}}=await import({url('timeline_snap.js')});"
        f"const {{referenceItemEffectiveBounds}}=await import({url('scene_reference_geometry.js')});"
        "const methods=(class{" + source[a:b] + "}).prototype;" + r'''
import assert from 'node:assert/strict';
const targets={playhead:true,selection:false,clipEdges:true,audioEdges:false,guides:true,promptSections:false,referenceItems:true,sceneBounds:true};
const scene={clips:[{clip_id:'a',timeline_start_frame:0,timeline_end_frame:20},{clip_id:'b',timeline_start_frame:50,timeline_end_frame:80}],
  guide_frames:[],reference_items:[]};
const host=(extra={})=>Object.assign(Object.create(methods),{activeScene:scene,snappingEnabled:true,_snapThreshold:5,playhead:33,
  selectionStart:0,selectionEnd:0,totalFrames:100,_settings:{timelineBehavior:{snapTargets:targets,snapPlayhead:false,snapRazor:false}},
  _expandItemsWithLinked:items=>items},extra);

// Move: the grabbed clip [10,30) is dragged right by 17 -> end at 47, three
// short of b's start: the END snaps, which the start-only rule never did.
const grabbed={type:'clip',id:'g',data:{timeline_start_frame:10,timeline_end_frame:30},origStart:10,origEnd:30};
let h=host({_dragItemsOrig:[grabbed],_dragItemOrigStart:10,_dragItemOrigEnd:30});
h._moveSnapCandidates=h._snapCandidates({exclude:['clip:g'],playhead:false});
assert.deepEqual(h._moveSnapDelta(17),{delta:20,frame:50});
// The start still snaps (to the playhead at 33).
assert.deepEqual(h._moveSnapDelta(22),{delta:23,frame:33});
// The playhead is read live (playback can run during a drag).
h.playhead=60;assert.deepEqual(h._moveSnapDelta(22),{delta:20,frame:50});h.playhead=33;
// Snap-back guard: the clip's own origin edges are never targets.
h._moveSnapCandidates=[10,30,55];h.playhead=500;assert.deepEqual(h._moveSnapDelta(1),{delta:1,frame:null});
// The guide shows only where the grabbed item actually lands.
h=host({_dragItemsOrig:[grabbed],_dragItemOrigStart:10,_dragItemOrigEnd:30,_dragAnchorType:'clip',_dragAnchorId:'g'});
grabbed.data.timeline_start_frame=30;assert.equal(h._moveSnapIndicator({delta:20,frame:50},20),50);
assert.equal(h._moveSnapIndicator({delta:20,frame:50},18),null,'a clamp changed the delta');
grabbed.data.timeline_start_frame=70;assert.equal(h._moveSnapIndicator({delta:20,frame:50},20),null,'a swap or hold placed it elsewhere');
assert.equal(h._moveSnapIndicator({delta:3,frame:null},3),null);
const g={type:'guide',id:-1,data:{_previewFrameIndex:95},origStart:-1,origEnd:-1};
h=host({_dragItemsOrig:[g],_dragItemOrigStart:-1,_dragAnchorType:'guide',_dragAnchorId:-1});
assert.equal(h._moveSnapIndicator({delta:-4,frame:95},-4),95,'a scene-end guide reads its preview frame');
grabbed.data.timeline_start_frame=10;h=host({_dragItemsOrig:[grabbed],_dragItemOrigStart:10,_dragItemOrigEnd:30});
// Snapping off: the raw delta.
h.snappingEnabled=false;assert.deepEqual(h._moveSnapDelta(17),{delta:17,frame:null});

// A group snaps on the grabbed item and the selection's outer bounds, not
// on every member: here only the far member's end (outer bound) is near 50.
const far={type:'clip',id:'f',data:{},origStart:35,origEnd:45};
h=host({playhead:500,_dragItemsOrig:[grabbed,far],_dragItemOrigStart:10,_dragItemOrigEnd:30});
h._moveSnapCandidates=[50];
assert.deepEqual(h._moveSnapDelta(4),{delta:5,frame:50});
const mid={type:'clip',id:'m',data:{},origStart:31,origEnd:33};
h=host({playhead:500,_dragItemsOrig:[grabbed,mid,far],_dragItemOrigStart:10,_dragItemOrigEnd:30});
h._moveSnapCandidates=[41];  // only the middle member's end (33+4=37) is near
assert.deepEqual(h._moveSnapDelta(4),{delta:4,frame:null});

// A scene-end guide (-1) snaps from where it is drawn, not from -1.
h=host({playhead:500,_dragItemsOrig:[{type:'guide',id:-1,data:{},origStart:-1,origEnd:-1}],_dragItemOrigStart:-1,_dragItemOrigEnd:-1});
h._moveSnapCandidates=[95];
assert.deepEqual(h._moveSnapDelta(-3),{delta:-4,frame:95});

// The ruler snaps only when its setting is on, and never to the playhead itself.
h=host();assert.equal(h._snapPlayheadFrame(48),48);
h._settings.timelineBehavior.snapPlayhead=true;assert.equal(h._snapPlayheadFrame(48),50);assert.equal(h._snapIndicator,50);
h.playhead=46;assert.equal(h._snapPlayheadFrame(47),50);

// Razor: off by default; on, the nearest target strictly inside the item
// and its linked partners, never the item's own edges.
const clipB={type:'clip',id:'b',data:scene.clips[1]};
h=host({playhead:62});assert.equal(h._razorFrame(clipB,60),60);
h._settings.timelineBehavior.snapRazor=true;
assert.equal(h._razorFrame(clipB,60),62);
assert.equal(h._razorFrame(clipB,52),52,'the clip\'s own start is not a cut target');
// A linked partner the pointer lies before stays undivided: a target before it too is fine.
const partner={type:'audio',id:'p',data:{timeline_start_frame:61,timeline_end_frame:90}};
h=host({playhead:58,_expandItemsWithLinked:()=>[clipB,partner]});
h._settings.timelineBehavior.snapRazor=true;
assert.equal(h._razorFrame(clipB,60),58);
// One that would cut the partner differently from the pointer is skipped.
partner.data.timeline_start_frame=55;h.playhead=53;
assert.equal(h._razorFrame(clipB,57),57,'the pointer cuts the partner, the playhead would not');
console.log('ok');
''', tmp_path)
