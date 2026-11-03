// Launcher DOM primitives and theme. No import-time DOM or listeners.
import { THEME as T, FONT, TYPE, SPACE, RADIUS, EDITOR_COLORS as C, chromeButtonCss, chromeInputCss, setButtonDisabled } from './editor_theme.js';
export { setButtonDisabled };
export function el(tag, cls = '', text = '') {
    const node = document.createElement(tag);
    node.className = cls;
    node.textContent = text;
    return node;
}
export function button(text, click, { title = text, primary = false, disabled = false } = {}) {
    const node = el('button', 'sl-button', text);
    node.type = 'button'; node.title = title;
    node.style.cssText = chromeButtonCss({ variant: primary ? 'primary' : 'secondary', fontSize: `${TYPE.t12}px`, padding: '6px 10px' });
    setButtonDisabled(node, disabled);
    if (click) node.addEventListener('click', click);
    return node;
}
export function input(placeholder, value, onInput, key) {
    const node = el('input', 'sl-input');
    node.style.cssText = chromeInputCss({ fontSize: `${TYPE.t12}px`, padding: '7px 10px' });
    node.placeholder = placeholder; node.setAttribute('aria-label', placeholder);
    node.value = value || ''; node.dataset.focusKey = key;
    node.addEventListener('input', () => onInput(node.value));
    return node;
}
export function component(element, dispose = () => {}) {
    return { element, cleanup() { dispose(); element.remove(); } };
}
export function date(value) {
    const time = typeof value === 'number' ? value * 1000 : value;
    const d = new Date(time);
    return time && !Number.isNaN(d.getTime()) ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '';
}
// Where a missing model goes: the absolute file on the ComfyUI machine, else
// models/<folder>/<the value's own subfolders>/<file>.
export function modelMessage(item) {
    const name = String(item.file || '').split(/[\\/]/).pop();
    const target = item.path || item.relative;
    return target ? `${name} → ${target} (${item.node})` : `${name} → folder unknown (${item.node} · ${item.input || ''})`;
}
export function badge(label, kind = '') { return el('span', `sl-badge ${kind}`, label); }
// Whether the named lists ('templates', 'projects') can be shown: 'ready' once all
// have loaded, 'failed' when one failed before it ever loaded, else 'loading', or
// 'slow' once that first load has taken a while.
export function listState(state, ...lists) {
    if (lists.every(name => state.loaded[name])) return 'ready';
    if (lists.some(name => !state.loaded[name] && state.loadFailed[name])) return 'failed';
    return state.listsSlow ? 'slow' : 'loading';
}
// A pane's loading or couldn't-load line, with Check again when it failed.
export function listNotice(text, failed, checkAgain) {
    const node = el('div', 'sl-empty', text);
    if (failed) { node.append(' '); node.append(button('Check again', checkAgain)); }
    return node;
}
export function mountLauncherStyle() {
    const style = el('style');
    // Dimensions implement the accepted Launcher proportions; visual tokens
    // (including type, colour, padding and radius) come from the editor theme.
    style.textContent = `
    .sl {height:100dvh;display:flex;flex-direction:column;font:${TYPE.t12}px ${FONT.sans};color:${T.fg0};background:${T.bg0};overflow:hidden}
    .sl *, .sl-popup *, .sl-dialog * {box-sizing:border-box}
    .sl-header {height:64px;flex:none;display:flex;align-items:center;justify-content:space-between;padding:0 ${SPACE.s5}px;border-bottom:1px solid ${T.line2};background:${T.bg1};gap:${SPACE.s4}px}
    .sl-brand,.sl-row {display:flex;align-items:center;gap:${SPACE.s2}px;min-width:0}
    .sl-brand {gap:${SPACE.s3}px}.sl-mark{width:40px;height:40px}.sl-wordmark{font-size:${TYPE.t14}px;font-weight:${TYPE.fwBold};letter-spacing:.18em;line-height:1.25}
    .sl-wordmark small {display:block;font-size:${TYPE.t10}px;color:${T.fg1};letter-spacing:.34em}
    .sl-attribution{display:flex;align-items:center;gap:${SPACE.s2}px;border-left:1px solid ${T.line2};padding-left:${SPACE.s4}px;margin-left:${SPACE.s2}px;color:${T.fg2};font-size:${TYPE.t10}px}.sl-attribution img{width:80px;max-height:24px}
    .sl-status-icon{width:16px;height:16px}.sl-status-dot{width:6px;height:6px;border-radius:50%;background:currentColor}.sl-status{display:flex;align-items:center;gap:${SPACE.s2}px}
    .sl-main{display:flex;flex:1;min-height:0}.sl-sidebar{width:260px;flex:none;background:${T.bg1};border-right:1px solid ${T.line2};display:flex;flex-direction:column;min-height:0}
    .sl-sidebar-head{padding:${SPACE.s4}px;display:flex;flex-direction:column;gap:${SPACE.s3}px}.sl-title{font-size:${TYPE.t14}px;font-weight:${TYPE.fwBold};margin:0}.sl-section-label{color:${T.fg2};font-size:${TYPE.t10}px;letter-spacing:.12em;text-transform:uppercase;padding:${SPACE.s3}px ${SPACE.s2}px}
    .sl-workflow-list{flex:1;min-height:0;overflow:auto;padding:0 ${SPACE.s2}px ${SPACE.s2}px}.sl-workflow{padding:${SPACE.s3}px ${SPACE.s2}px;border:1px solid transparent;border-radius:${RADIUS.r3}px;margin-bottom:${SPACE.s1}px;cursor:pointer;outline-offset:-2px}.sl-workflow:hover{background:${T.bg2}}.sl-workflow.selected{background:${C.accentSoft};border-color:${C.accentBorder}}.sl-workflow.hidden,.sl-project.hidden{opacity:.55}.sl-workflow-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:${TYPE.fwMed}}
    .sl-workflow-sub{margin-top:${SPACE.s2}px;display:flex;flex-wrap:wrap;gap:${SPACE.s2}px;align-items:center;font-size:${TYPE.t10}px;color:${T.fg2}}.sl-workflow-details{padding-top:${SPACE.s3}px;color:${T.fg1};font-size:${TYPE.t11}px;line-height:1.5;overflow-wrap:anywhere}.sl-workflow-details p{margin:${SPACE.s1}px 0}.sl-more{margin-left:auto}
    .sl-badge{font-size:${TYPE.t10}px;padding:2px 6px;border:1px solid ${T.line2};border-radius:${RADIUS.r1}px;color:${T.fg1};white-space:nowrap}.sl-badge.good,.sl-good{color:${T.statusRunning}}.sl-badge.warn,.sl-warn{color:${C.warningText}}.sl-badge.bad,.sl-bad{color:${C.dangerText}}
    .sl-samples{padding:${SPACE.s4}px;border-top:1px solid ${T.line2};font-size:${TYPE.t11}px;color:${T.fg2};line-height:1.5}.sl-samples a{color:${T.fg1};font-weight:${TYPE.fwMed};text-decoration:none}.sl-samples p{margin:${SPACE.s2}px 0 0}
    .sl-projects{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column}.sl-project-toolbar{display:flex;align-items:center;gap:${SPACE.s3}px;padding:${SPACE.s5}px;flex-wrap:wrap}.sl-project-toolbar .sl-input{margin-left:auto;width:240px}.sl-input{min-width:0;max-width:100%}.sl-project-scroll{flex:1;overflow:auto;padding:0 ${SPACE.s5}px ${SPACE.s5}px}.sl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,var(--tile-min,220px)),1fr));gap:${SPACE.s4}px;align-items:start}
    .sl-project{min-width:0;cursor:pointer;outline-offset:3px;border-radius:${RADIUS.r3}px}.sl-poster{aspect-ratio:16/9;border:1px solid ${T.line2};border-radius:${RADIUS.r3}px;background:${T.bg2};display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden}.sl-project:hover .sl-poster{border-color:${T.fg2}}.sl-project.selected .sl-poster{border:2px solid ${T.accentHi};background:${T.accentBg}}.sl-placeholder{width:44px;height:44px;opacity:.24}.sl-thumb{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:${T.bg0}}.sl-list-poster{width:72px}.sl-list-poster .sl-poster{width:64px}.sl-list-poster .sl-placeholder{width:16px;height:16px}.sl-project-meta{padding:${SPACE.s2}px 0;font-size:${TYPE.t11}px;color:${T.fg2};line-height:1.55}.sl-project-name{color:${T.fg0};font-size:${TYPE.t12}px;font-weight:${TYPE.fwMed};overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sl-folder{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:${TYPE.t10}px}.sl-project-badges{position:absolute;left:${SPACE.s2}px;bottom:${SPACE.s2}px;display:flex;gap:${SPACE.s1}px}.sl-project-badges .sl-badge{background:${T.bg0}}.sl-new .sl-poster{border-style:dashed;background:transparent;flex-direction:column;gap:${SPACE.s2}px;color:${T.fg1}}.sl-plus{font-size:32px;font-weight:300;color:${T.fg2}}
    .sl-notice{padding:${SPACE.s3}px ${SPACE.s5}px;background:${T.bg2};border-bottom:1px solid ${T.line2};line-height:1.5;display:flex;align-items:center;gap:${SPACE.s3}px;flex-wrap:wrap}.sl-notice button{margin-left:auto}.sl-empty{padding:${SPACE.s5}px 0;color:${T.fg2};line-height:1.6}.sl-footer-notice{display:flex;align-items:center;gap:${SPACE.s3}px;flex-wrap:wrap;color:${T.fg2};margin-top:${SPACE.s5}px}
    .sl-open{flex:none;min-height:52px;max-height:34dvh;overflow:auto;border-top:1px solid ${T.line2};background:${T.bg1};padding:${SPACE.s3}px ${SPACE.s5}px;display:flex;align-items:flex-start;gap:${SPACE.s4}px}.sl-open-content{flex:1;min-width:0}.sl-open-summary{line-height:28px;color:${T.fg1}}.sl-problem{font-size:${TYPE.t11}px;line-height:1.5;overflow-wrap:anywhere;margin-top:${SPACE.s1}px}.sl-spinner{display:inline-block;animation:sl-spin 1s linear infinite}@keyframes sl-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.sl-spinner{animation:none}}
    .sl-table{width:100%;border-collapse:collapse;text-align:left;white-space:nowrap}.sl-table th{color:${T.fg2};font-size:${TYPE.t11}px;font-weight:${TYPE.fwNormal};border-bottom:1px solid ${T.line2};padding:${SPACE.s2}px}.sl-table td{padding:${SPACE.s2}px;border-bottom:1px solid ${T.line1};font-size:${TYPE.t11}px;max-width:320px;overflow:hidden;text-overflow:ellipsis}.sl-table tr.selected{background:${T.accentBg}}.sl-table tbody tr:hover{background:${T.bg2}}.sl-table tbody tr{cursor:pointer}
    .sl-popup,.sl-dialog{font:${TYPE.t12}px ${FONT.sans};color:${T.fg0};background:${T.bg2};border:1px solid ${T.line2};border-radius:${RADIUS.r3}px;padding:${SPACE.s4}px;box-shadow:0 12px 40px ${T.bg0};line-height:1.5}.sl-popup{position:fixed;z-index:200;max-width:min(400px,calc(100vw - 16px));max-height:80dvh;overflow:auto;display:flex;flex-direction:column;gap:${SPACE.s2}px;min-width:220px}.sl-popup button{text-align:left}.sl-popup label{display:flex;justify-content:space-between;align-items:center;gap:${SPACE.s4}px}.sl-popup select{background:${T.bg1};color:${T.fg0};border:1px solid ${T.line2};padding:${SPACE.s1}px;border-radius:${RADIUS.r2}px}.sl-popup hr{width:100%;border:0;border-top:1px solid ${T.line2}}.sl-dim{color:${T.fg2};font-size:${TYPE.t11}px;overflow-wrap:anywhere}.sl-dialog{box-sizing:border-box;width:min(460px,calc(100vw - 32px));max-height:85dvh;overflow:auto}.sl-dialog::backdrop{background:${T.bg0}bb}.sl-dialog h2{font-size:${TYPE.t16}px;margin:0 0 ${SPACE.s4}px}.sl-dialog .sl-input{width:100%}.sl-dialog-actions{display:flex;justify-content:flex-end;gap:${SPACE.s2}px;margin-top:${SPACE.s4}px}.sl-project-dialog{width:min(560px,calc(100vw - 32px))}.sl-tabs{display:flex;gap:${SPACE.s1}px;margin-bottom:${SPACE.s4}px}.sl-tabs button[aria-selected="true"]{background:${C.accentSoft}!important;border-color:${C.accentBorder}!important;color:${T.fg0}!important}.sl-tab-panel{display:flex;flex-direction:column;gap:${SPACE.s3}px}.sl-tab-panel[hidden]{display:none}.sl-tab-panel p{margin:0}.sl-link-preview{min-height:44px;padding:${SPACE.s3}px;border:1px solid ${T.line2};border-radius:${RADIUS.r2}px;background:${T.bg1};line-height:1.5}.sl-link-preview:empty{display:none}.sl-dialog p{color:${T.fg1}}.sl-mobile-picker{display:none}
    @media(max-width:899px){.sl-attribution{display:none}.sl-header{padding:0 ${SPACE.s4}px}.sl-main{flex-direction:column;position:relative}.sl-sidebar{width:100%;border-right:0;border-bottom:1px solid ${T.line2};max-height:55dvh}.sl-mobile-picker{display:block;margin:${SPACE.s2}px ${SPACE.s4}px}.sl-sidebar:not(.expanded) .sl-sidebar-head,.sl-sidebar:not(.expanded) .sl-workflow-list,.sl-sidebar:not(.expanded) .sl-samples{display:none}.sl-project-toolbar{padding:${SPACE.s4}px;gap:${SPACE.s2}px}.sl-project-toolbar .sl-input{width:180px}.sl-project-scroll{padding:0 ${SPACE.s4}px ${SPACE.s4}px}.sl-grid{grid-template-columns:repeat(auto-fill,minmax(min(100%,220px),1fr))}.sl-open{padding:${SPACE.s2}px ${SPACE.s4}px}.sl-header .sl-wordmark{font-size:${TYPE.t12}px}}
    `;
    document.head.append(style);
    return () => style.remove();
}
