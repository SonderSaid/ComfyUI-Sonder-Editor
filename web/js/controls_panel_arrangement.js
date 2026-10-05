import { planPanelMove,comparePanelRows } from './controls_panel_layout.js';
import { EDITOR_COLORS as COLORS } from './editor_theme.js';

const ancestor=(el,test,root)=>{for(let n=el;n&&n!==root;n=n.parentElement)if(test(n))return n;return null;};
let nextDrag=0;
export function mountPanelArrangement(container,{getEntries,isBusy,apply,notify}) {
    const mime='application/x-sonder-controls-'+(++nextDrag);
    let moving=null,marked=null;
    const stamp=()=>JSON.stringify(getEntries().map(d=>[d.base,d.order,d.group,d.label]));
    function clear(){if(marked)marked.style.outline='';marked=null;}
    function finish(){clear();if(moving)moving.el.style.opacity='';moving=null;}
    function destination(e){
        const row=ancestor(e.target,n=>n.dataset?.panelBase,container);
        const entries=getEntries();
        if(row){
            const d=entries.find(d=>d.base===row.dataset.panelBase);
            if(!d||d.base===moving?.base)return null;
            const rect=row.getBoundingClientRect?.(),after=rect&&e.clientY>rect.top+rect.height/2;
            const list=entries.filter(x=>(x.group||'')===(d.group||'')&&x.base!==moving.base).sort(comparePanelRows);
            return {el:row,group:d.group||'',before:after?(list[list.findIndex(x=>x.base===d.base)+1]?.base??null):d.base};
        }
        const zone=ancestor(e.target,n=>n.dataset?.panelDropGroup!==undefined,container);
        return zone?{el:zone,group:zone.dataset.panelDropGroup,before:null}:null;
    }
    const start=e=>{
        const grip=ancestor(e.target,n=>n.dataset?.panelDragHandle==='true',container);
        if(!grip)return;
        if(isBusy()){e.preventDefault();notify('Finish the current edit or save before arranging the panel.');return;}
        const el=ancestor(grip,n=>n.dataset?.panelBase,container);if(!el)return;
        moving={base:el.dataset.panelBase,el,stamp:stamp()};e.dataTransfer.setData(mime,moving.base);e.dataTransfer.effectAllowed='move';el.style.opacity='0.5';
    };
    const over=e=>{if(!moving)return;const dest=destination(e);if(!dest)return;
        e.preventDefault();e.dataTransfer.dropEffect='move';clear();marked=dest.el;marked.style.outline=`1px solid ${COLORS.accent}`;};
    const drop=e=>{
        if(!moving||e.dataTransfer.getData(mime)!==moving.base)return;
        const changed=moving.stamp!==stamp(),dest=destination(e),base=moving.base;finish();if(!dest)return;e.preventDefault();
        if(changed||isBusy()){notify('The panel changed during this drag. Try again.');return;}
        try {const changes=planPanelMove(getEntries(),base,dest.group,dest.before);if(Object.keys(changes).length)void apply(changes);}
        catch(error){notify(error.message);}
    };
    container.addEventListener('dragstart',start);container.addEventListener('dragover',over);container.addEventListener('drop',drop);container.addEventListener('dragend',finish);
    return {reset:finish,destroy(){finish();container.removeEventListener('dragstart',start);container.removeEventListener('dragover',over);container.removeEventListener('drop',drop);container.removeEventListener('dragend',finish);}};
}
