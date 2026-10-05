// Pure intent planning. Hosts retain validation, mutation and persistence.
export const PANEL_ORDER_LIMIT = 9999;
export const comparePanelRows = (a,b) => (a.order||0)-(b.order||0)
    || String(a.label||'').localeCompare(String(b.label||'')) || a.base.localeCompare(b.base);
export const canArrange = d => !!d.base && !['missing_id','duplicate_id'].includes(d.status);

export function planPanelMove(entries, movingBase, group, beforeBase=null) {
    const moving=entries.find(d=>d.base===movingBase);
    if(!moving||!canArrange(moving))throw Error('This row cannot be arranged. Refresh the panel.');
    const list=entries.filter(d=>d.base!==movingBase&&(d.group||'')===group).sort(comparePanelRows);
    const index=beforeBase===null?list.length:list.findIndex(d=>d.base===beforeBase);
    if(index<0)throw Error('The destination changed. Try again.');
    list.splice(index,0,moving);
    if((moving.group||'')===group&&entries.filter(d=>(d.group||'')===group).sort(comparePanelRows).every((d,i)=>d.base===list[i].base))return {};
    // Grow only the crowded neighborhood; never split one drop into partial saves.
    const budget=(moving.group||'')===group?64:63;
    for(let count=1;count<=Math.min(budget,list.length);count++)for(let start=Math.max(0,index-count+1);start<=Math.min(index,list.length-count);start++){
        const end=start+count-1;
        const low=start?Math.max(-10000,Number(list[start-1].order||0)):-10000;
        const high=end<list.length-1?Math.min(10000,Number(list[end+1].order||0)):10000;
        if(high-low>count){
            const changes={};
            let blocked=false;
            if((moving.group||'')!==group)changes[moving.base+':group']=group;
            for(let i=start;i<=end;i++){
                const order=Math.floor(low+(high-low)*(i-start+1)/(count+1));
                if(order!==Number(list[i].order||0)){
                    if(!canArrange(list[i])){blocked=true;break;}
                    changes[list[i].base+':order']=order;
                }
            }
            if(blocked)continue;
            return changes;
        }
    }
    throw Error('Too many rows share this position. Space their panel orders on the canvas, then try again.');
}
