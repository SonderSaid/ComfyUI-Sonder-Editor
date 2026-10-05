import { mountComboPicker, wantsComboPicker } from './controls_combo_picker.js';
import { mountPanelArrangement } from './controls_panel_arrangement.js';
import { comparePanelRows, canArrange } from './controls_panel_layout.js';
import { renderPanelMarkdown } from './panel_markdown.js';
import { createDisclosureMemory } from './disclosure_memory.js';
import { register, PRIORITY, PRESERVE_DEFAULT } from './keyboard_ownership.js';
import { EDITOR_COLORS as COLORS, FONT, RADIUS, SPACE } from './editor_theme.js';

let nextPanel = 0;
const SEED_MODES = ['fixed','increment','decrement','randomize'];
const editable = d => ['ok','unlinked'].includes(d.status);
const same = (a,b) => String(a ?? '') === String(b ?? '');
const make = (tag,text) => { const el=document.createElement(tag); if(text!==undefined)el.textContent=text;return el; };

export function mountControlsPanel(container, host) {
    const panelId=++nextPanel,rows=new Map(),groups=new Map(),pending=new Set();
    const disclosure=createDisclosureMemory('sonder-controls-disclosure-v1');
    let destroyed=false,lastView=null,noteEditor=null;
    const failed=new Set();
    let validTargets=null,layoutSaving=false;
    const layoutFailures=new Set();
    container.dataset.sonderControlsPanel='true';
    container.style.cssText=`display:flex;flex-direction:column;flex:1;min-height:0;overflow:auto;padding:${SPACE.s4}px;gap:${SPACE.s3}px;font:12px ${FONT.sans};color:${COLORS.text};`;
    const message=make('div');message.style.cssText=`color:${COLORS.textSecondary};line-height:1.5;`;
    const editorSection=make('details');editorSection.open=true;editorSection.append(make('summary','Editor'));
    const editorBody=make('div');editorBody.style.cssText='display:flex;flex-direction:column;gap:8px;padding-top:8px;';editorSection.append(editorBody);
    const settingsRows=[];
    for(const [key,label] of [['takePlacementMuted','Takes start muted'],['linkedTakePlacement','Link take video + audio']]){
        const row=make('label'),input=make('input');input.type='checkbox';row.style.cssText='display:flex;gap:8px;align-items:center;';
        row.append(input,make('span',label));input.addEventListener('change',()=>host.setEditorSetting('render.'+key,input.checked));
        editorBody.append(row);settingsRows.push({key,input});
    }
    const fieldsBody=make('div'),workflowBody=make('div');
    fieldsBody.style.cssText=workflowBody.style.cssText='display:flex;flex-direction:column;gap:10px;';
    workflowBody.dataset.panelDropGroup='';
    const mainDrop=make('div','Main section');mainDrop.dataset.panelDropGroup='';
    mainDrop.style.cssText=`font-size:11px;color:${COLORS.textSecondary};padding:${SPACE.s2}px 0;`;
    editorBody.append(fieldsBody);container.append(editorSection,message,workflowBody);
    const warning=reason=>host.notify?.(reason || 'The change could not be saved.');
    const inputCss=`width:100%;min-width:0;box-sizing:border-box;border:1px solid ${COLORS.border};border-radius:${RADIUS.r2}px;background:${COLORS.panelMuted};color:${COLORS.text};font:12px ${FONT.sans};padding:${SPACE.s2}px;`;
    const settingValues=()=>host.getEditorSettings?.()?.render || {};

    function readInput(row) { return row.input.type==='checkbox'?row.input.checked:row.input.value; }
    function setInput(row,value) {
        if(row.input.type==='checkbox'){row.input.checked=!!value;row.input.indeterminate=!!row.spec.mixed;}
        else {
            if(row.picker){row.picker.setValue(value);return;}
            if(row.input.tagName==='SELECT' && ![...row.input.options].some(o=>o.value===String(value??''))){row.input.append(new Option(String(value??''),String(value??'')));}
            row.input.value=String(value??'');
        }
    }
    function track(work) { pending.add(work);work.finally(()=>pending.delete(work));return work; }
    function layoutEntries(){
        const view=host.getView();return [...(view.controls||[]),...(view.notes||[])].map(d=>({
            ...d,base:d.control_id?'control:'+d.control_id:d.note_id?'note:'+d.note_id:'',
        }));
    }
    async function saveLayout(changes){
        layoutSaving=true;for(const t of layoutFailures)failed.delete(t);layoutFailures.clear();
        let timer;const actual=Promise.resolve().then(()=>host.applyChanges(changes));
        const work=(async()=>{
            let result;try{result=await Promise.race([actual,new Promise(resolve=>{timer=setTimeout(()=>resolve({ok:false,rejected:[{reason:'The arrangement was not confirmed. Try again once connected.'}]}),5000);})]);}
            catch(error){result={ok:false,rejected:[{reason:error.message}]};}clearTimeout(timer);
            if(destroyed)return false;layoutSaving=false;
            if(!result.ok){for(const t of Object.keys(changes))if(!validTargets||validTargets.has(t)){failed.add(t);layoutFailures.add(t);}warning(result.rejected?.[0]?.reason);}
            render();return !!result.ok;
        })();actual.catch(()=>{});return track(work);
    }
    const arrangement=mountPanelArrangement(container,{getEntries:layoutEntries,
        isBusy:()=>layoutSaving||pending.size>0||!!noteEditor||[...rows.values()].some(r=>r.editing),apply:saveLayout,notify:warning});
    function commit(row,target,value,before) {
        const token=(row.mutations.get(target)?.token||0)+1;
        row.mutations.set(target,{token,pending:true});failed.delete(target);
        const optimisticKey=target===row.target?'optimistic':target.endsWith(':seed_mode')?'modeOptimistic':'noteOptimistic';
        row[optimisticKey]=value;
        let timer;
        const actual=Promise.resolve().then(()=>host.applyChanges({[target]:value}));
        const work=(async()=>{
            let result;
            try {
                result=await Promise.race([actual,new Promise(resolve=>{timer=setTimeout(()=>resolve({ok:false,rejected:[{target,reason:'The change was not confirmed. Try again once connected.'}]}),5000);})]);
            } catch(error){result={ok:false,rejected:[{target,reason:error?.message||'The change could not be saved.'}]};}
            clearTimeout(timer);
            if(destroyed || (validTargets&&!validTargets.has(target)) || token!==row.mutations.get(target)?.token)return true;
            row.mutations.get(target).pending=false;
            if(!result.ok){failed.add(target);row[optimisticKey]=undefined;
                if(row.input&&target===row.target&&(!row.editing||same(readInput(row),value))){setInput(row,row.spec.value??row.spec.seed??before);row.editStart=readInput(row);}
                if(row.mode&&target.endsWith(':seed_mode')&&same(row.mode.value,value)){row.mode.value=row.spec.seed_mode??before;row.modeEditStart=row.mode.value;}
                warning(result.rejected?.[0]?.reason);}
            render();return !!result.ok;
        })();
        actual.catch(()=>{});return track(work);
    }
    function commitValue(row) {
        if(row.suppress || !row.input || row.input.disabled)return Promise.resolve(true);
        const value=readInput(row),before=row.editStart;
        row.editing=document.activeElement===row.input && row.input.type!=='checkbox';
        if(same(value,before))return Promise.resolve(true);
        row.editStart=value;return commit(row,row.target,value,before);
    }
    function closeNote(save) {
        const edit=noteEditor;if(!edit)return Promise.resolve(true);
        if(edit.composing)return Promise.resolve(false);
        noteEditor=null;edit.unregister();edit.row.editing=false;
        const next=edit.area.value;edit.box.replaceChildren();
        if(!save || next===edit.before){render();return Promise.resolve(true);}
        edit.row.noteOptimistic=next;render();
        return commit(edit.row,edit.target,next,edit.before).then(ok=>{render();return ok;});
    }
    function openNote(row,box,target,text,canEdit) {
        if(!canEdit || destroyed)return;
        if(noteEditor)void closeNote(true);
        const area=make('textarea'),done=make('button','Done');area.value=String(text??'');area.maxLength=4000;
        area.dataset.sonderTextUndo='true';area.style.cssText=inputCss+'min-height:100px;resize:vertical;';
        done.type='button';done.style.cssText=`border:1px solid ${COLORS.border};background:${COLORS.accentSoft};color:${COLORS.text};border-radius:4px;padding:5px 10px;align-self:flex-end;cursor:pointer;`;
        box.replaceChildren(area,done);row.editing=true;
        const edit={row,box,target,area,before:String(text??''),composing:false,unregister:null};noteEditor=edit;
        area.addEventListener('compositionstart',()=>edit.composing=true);area.addEventListener('compositionend',()=>edit.composing=false);
        edit.unregister=register({id:`controls-note:${panelId}`,priority:PRIORITY.OVERLAY,keydown:e=>{
            if(noteEditor!==edit || !box.contains(document.activeElement))return false;
            if(edit.composing||e.isComposing||e.keyCode===229)return PRESERVE_DEFAULT;
            if(e.key==='Escape'){void closeNote(false);return true;}
            if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){void closeNote(true);return true;}
            return PRESERVE_DEFAULT;
        }});
        area.addEventListener('blur',()=>{if(noteEditor===edit&&!edit.composing)void closeNote(true);});
        done.addEventListener('mousedown',e=>e.preventDefault());done.addEventListener('click',()=>void closeNote(true));area.focus();
    }
    function noteBox(row,target,text,canEdit) {
        const box=make('div');box.dataset.noteTarget=target;
        box.style.cssText=`line-height:1.5;color:${COLORS.textSecondary};overflow-wrap:anywhere;cursor:${canEdit?'text':'default'};`;
        box.addEventListener('dblclick',()=>openNote(row,box,target,row.noteText,!!(row.spec.control_id||row.spec.note_id)&&!['missing_id','duplicate_id'].includes(row.spec.status)&&!row.spec.note_truncated));
        return box;
    }
    function build(spec,target) {
        const row={spec,target,kind:spec.kind,mutations:new Map(),editing:false,suppress:false,optimistic:undefined,noteOptimistic:undefined};
        row.el=make('div');row.el.dataset.controlTarget=target;
        row.el.style.cssText=`display:flex;flex-direction:column;gap:5px;padding:8px 0;border-bottom:1px solid ${COLORS.border};min-width:0;`;
        if(spec.source!=='field'){
            row.grip=make('span','⋮⋮');row.grip.dataset.panelDragHandle='true';row.grip.draggable=true;
            row.grip.title='Drag to reorder or move to another section';row.grip.setAttribute('aria-label','Drag to arrange');
            row.el.style.position='relative';row.el.style.paddingRight='20px';
            row.grip.style.cssText=`position:absolute;right:0;top:8px;cursor:grab;color:${COLORS.textSecondary};user-select:none;font-size:14px;`;
            row.el.append(row.grip);
        }
        if(spec.source==='note'){
            row.note=noteBox(row,target,spec.text,spec.status==='ok'&&!spec.note_truncated);row.el.append(row.note);
        }else{
            row.label=make('label',spec.label);row.label.style.fontWeight='600';row.el.append(row.label);
            if(spec.kind==='COMBO'&&wantsComboPicker(spec)){
                row.picker=mountComboPicker({inputCss,onLeave:()=>{row.editing=false;render();},choose:()=>void commitValue(row),cancel:()=>{row.suppress=true;setInput(row,row.editStart);row.input.blur();row.suppress=false;}});
                row.input=row.picker.input;
            }else row.input=make(spec.kind==='COMBO'?'select':spec.kind==='STRING'&&spec.multiline?'textarea':'input');
            if(row.input.tagName==='INPUT'&&!row.picker)row.input.type=spec.kind==='BOOLEAN'?'checkbox':['INT','FLOAT'].includes(spec.kind)?'number':'text';
            if(spec.kind==='SEED')row.input.inputMode='numeric';
            row.input.style.cssText=spec.kind==='BOOLEAN'?'align-self:flex-start;':inputCss;
            row.input.dataset.sonderTextUndo='true';row.input.setAttribute('aria-label',spec.label||'Control value');
            row.input.id=`controls-${panelId}-${rows.size}`;row.label.htmlFor=row.input.id;
            if(spec.kind==='COMBO'&&!row.picker)for(const option of spec.options||[])row.input.append(new Option(String(option),String(option)));
            if(spec.min!=null)row.input.min=String(spec.min);if(spec.max!=null)row.input.max=String(spec.max);
            if(spec.kind==='INT')row.input.step='1';if(spec.kind==='FLOAT')row.input.step=String(spec.step||'any');
            row.el.append(row.picker?.box||row.input);
            row.input.addEventListener('focus',()=>{if(!row.editing)row.editStart=readInput(row);row.editing=true;});
            row.input.addEventListener('change',()=>{if(!row.picker)void commitValue(row);});
            row.input.addEventListener('blur',()=>{if(row.picker)return;if(row.input.tagName==='TEXTAREA')void commitValue(row);row.editing=false;row.picker?.close();render();});
            row.input.addEventListener('compositionstart',()=>row.composing=true);row.input.addEventListener('compositionend',()=>row.composing=false);
            if(spec.kind==='SEED'){
                row.mode=make('select');row.mode.setAttribute('aria-label',(spec.label||'Seed')+' mode');row.mode.style.cssText=inputCss;
                for(const mode of SEED_MODES)row.mode.append(new Option(mode[0].toUpperCase()+mode.slice(1),mode));
                row.mode.addEventListener('focus',()=>{row.editing=true;row.modeEditStart=row.mode.value;});
                row.mode.addEventListener('blur',()=>{row.editing=false;render();});
                row.mode.addEventListener('change',()=>{row.modeEditStart=row.mode.value;void commit(row,`control:${spec.control_id}:seed_mode`,row.mode.value,row.spec.seed_mode);});row.el.append(row.mode);
            }
            if(spec.source==='control'){row.note=noteBox(row,`control:${spec.control_id}:note`,spec.note,!!spec.control_id&&!['missing_id','duplicate_id'].includes(spec.status)&&!spec.note_truncated);row.el.append(row.note);}
        }
        row.status=make('div');row.status.style.cssText=`font-size:11px;color:${COLORS.textDim};line-height:1.4;`;row.el.append(row.status);
        return row;
    }
    function update(row,spec,fresh) {
        row.spec=spec;
        if(row.grip){const base=spec.control_id?'control:'+spec.control_id:spec.note_id?'note:'+spec.note_id:'';
            const allowed=canArrange({...spec,base});row.grip.draggable=allowed;row.grip.hidden=!allowed;
            if(allowed)row.el.dataset.panelBase=base;else delete row.el.dataset.panelBase;
        }
        if(row.label){row.label.textContent=spec.label||'';row.input.setAttribute('aria-label',spec.label||'Control value');}
        if(row.mode)row.mode.setAttribute('aria-label',(spec.label||'Seed')+' mode');
        if(row.editing)return;
        if(fresh){
            if(!row.mutations.get(row.target)?.pending)row.optimistic=undefined;
            if(!row.mutations.get(`control:${spec.control_id}:seed_mode`)?.pending)row.modeOptimistic=undefined;
            const noteTarget=spec.source==='note'?row.target:`control:${spec.control_id}:note`;
            if(!row.mutations.get(noteTarget)?.pending)row.noteOptimistic=undefined;
        }
        if(row.input){row.input.disabled=!editable(spec);
            if(row.picker)row.picker.update(spec);
            if(spec.kind==='COMBO'&&!row.picker&&row.optionsKey!==JSON.stringify(spec.options||[])){row.optionsKey=JSON.stringify(spec.options||[]);row.input.replaceChildren(...(spec.options||[]).map(o=>new Option(String(o),String(o))));}
            row.input.min=spec.min==null?'':String(spec.min);row.input.max=spec.max==null?'':String(spec.max);
            setInput(row,row.optimistic??(spec.kind==='SEED'?spec.seed:spec.value));}
        if(row.mode){row.mode.disabled=!editable(spec);row.mode.value=row.modeOptimistic??spec.seed_mode??'fixed';}
        if(row.note){row.noteText=row.noteOptimistic??(spec.source==='note'?spec.text:spec.note)??'';row.note.replaceChildren(renderPanelMarkdown(row.noteText));
            if(!row.noteText){row.note.textContent=spec.source==='note'?'Double-click to add a note.':'Add note';row.note.style.fontStyle='italic';}else row.note.style.fontStyle='normal';}
        row.status.textContent=spec.note_truncated?'Edit this longer note on the canvas.':spec.reason||spec.error||'';
    }
    function render(view=host.getView()) {
        if(destroyed)return;
        const fresh=view!==lastView;lastView=view;
        for(const {key,input} of settingsRows)input.checked=!!settingValues()[key];
        message.textContent=view.error||(!view.controls?.length&&!view.notes?.length?'Add a Sonder Control to a widget and a Sonder Panel Note for instructions.':'');
        const fieldMap=new Map();for(const d of view.fields||[]){const target=`field:${d.class_type}/${d.field}`;const first=fieldMap.get(target);
            if(!first)fieldMap.set(target,{...d,source:'field'});else{first.mixed=first.mixed||!same(first.value,d.value);if(!editable(d))Object.assign(first,{status:d.status,reason:d.reason});}}
        const entries=[...fieldMap].map(([target,spec])=>({target,spec,group:'Editor'}));
        for(const spec of [...(view.controls||[]),...(view.notes||[])]){const source=spec.source||('note_id'in spec?'note':'control');
            const target=source==='note'?`note:${spec.note_id}:text`:`control:${spec.control_id}:${spec.kind==='SEED'?'seed':'value'}`;
            entries.push({target,spec:{...spec,source},group:spec.group||''});}
        if(view.state==='ready'){
            validTargets=new Set(entries.flatMap(({target,spec})=>[target,
                ...(spec.source!=='field'&&canArrange({...spec,base:spec.control_id||spec.note_id})?[target.replace(/:[^:]+$/,':order'),target.replace(/:[^:]+$/,':group')]:[]),
                ...(spec.source==='control'?
                [`control:${spec.control_id}:note`,...(spec.kind==='SEED'?[`control:${spec.control_id}:seed_mode`]:[])]:[])]));
            for(const target of failed)if(!validTargets.has(target))failed.delete(target);
        }
        entries.sort((a,b)=>comparePanelRows({...a.spec,base:a.target},{...b.spec,base:b.target}));
        workflowBody.prepend(mainDrop);
        const alive=new Set(),groupOrder=[];
        for(const {target,spec,group} of entries){alive.add(target);let row=rows.get(target);if(row&&(row.kind!==spec.kind||(spec.kind==='COMBO'&&!!row.picker!==wantsComboPicker(spec)))&&!row.editing){row.picker?.destroy();row.el.remove();rows.delete(target);row=null;}if(!row){row=build(spec,target);rows.set(target,row);}update(row,spec,fresh);
            let body=spec.source==='field'?fieldsBody:workflowBody;
            if(group&&spec.source!=='field'){if(!groups.has(group)){const detail=make('details'),summary=make('summary',group),content=make('div');
                summary.dataset.panelDropGroup=group;content.dataset.panelDropGroup=group;detail.open=disclosure.isOpen(group,group.toLowerCase()!=='advanced');content.style.cssText='display:flex;flex-direction:column;gap:8px;padding-top:6px;';detail.append(summary,content);
                detail.addEventListener('toggle',()=>disclosure.remember(group,detail.open));groups.set(group,{detail,content});}
                body=groups.get(group).content;if(!groupOrder.includes(group))groupOrder.push(group);}
            if(!row.editing)body.append(row.el);
        }
        for(const [target,row] of rows)if(!alive.has(target)&&!row.editing){row.picker?.destroy();row.el.remove();rows.delete(target);}
        for(const group of groupOrder)workflowBody.append(groups.get(group).detail);
        for(const [group,obj] of groups)if(!groupOrder.includes(group)&&![...rows.values()].some(r=>r.editing&&obj.detail.contains(r.el))){obj.detail.remove();groups.delete(group);}
    }
    const unregister=register({id:`controls-values:${panelId}`,priority:PRIORITY.TEXT_EDITOR,keydown:e=>{
        const row=[...rows.values()].find(row=>row.input===document.activeElement||row.mode===document.activeElement||row.picker?.contains(document.activeElement));if(!row)return false;
        if(row.mode===document.activeElement)return PRESERVE_DEFAULT;
        if(row.composing||e.isComposing||e.keyCode===229)return PRESERVE_DEFAULT;
        if(row.picker&&row.picker.keydown(e))return true;
        if(e.key==='Escape'){row.suppress=true;setInput(row,row.editStart);row.input.blur();row.suppress=false;return true;}
        if(e.key==='Enter'&&(row.input.tagName!=='TEXTAREA'||e.ctrlKey||e.metaKey)){void commitValue(row);row.input.blur();return true;}
        return PRESERVE_DEFAULT;
    }});
    const unsubscribe=host.subscribe(view=>render(view));render();
    return {
        render,reset(){if(noteEditor)void closeNote(false);arrangement.reset();for(const row of rows.values())row.picker?.destroy();rows.clear();fieldsBody.replaceChildren();workflowBody.replaceChildren();groups.clear();failed.clear();render();},
        async flushPending(){if(noteEditor&&!await closeNote(true))return false;const row=[...rows.values()].find(row=>row.input===document.activeElement);
            if(row&&!row.picker&&!await commitValue(row))return false;const results=await Promise.all([...pending]);return !failed.size&&results.every(Boolean);},
        destroy(){if(noteEditor)void closeNote(false);destroyed=true;arrangement.destroy();for(const row of rows.values())row.picker?.destroy();unsubscribe?.();unregister();rows.clear();groups.clear();container.replaceChildren();},
    };
}
