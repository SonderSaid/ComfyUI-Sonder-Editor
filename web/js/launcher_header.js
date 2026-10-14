import { el, button, component } from './launcher_view.js';

export function environmentState(status) {
    if (!status || status.error) return { label: 'Unreachable', kind: 'bad', message: status?.error || 'Checking the connection…' };
    const install = status.install || {};
    if ([install.sdk, install.proxy].some(p => p?.installed && !p.supported)) return { label: 'Unsupported', kind: 'bad', message: 'A generation package has an unsupported version. Open Settings → Sonder App in the editor.' };
    if (status.proxy?.error) return { label: 'Connection problem', kind: 'bad', message: status.proxy.error };
    if (!install.ready) return { label: 'Generate unavailable', kind: 'warn', message: 'Generate needs comfy-sdk and comfy-api-proxy. You can still open and edit projects.' };
    return { label: 'Ready', kind: 'good', message: '' };
}
// ComfyUI's own root for this page, whether it was opened at /sonder-editor/app
// or through the /api/ prefix (or under a base path): ComfyUI's logos live there.
export function comfyRoot(pathname) {
    // One leading slash: "//host/…" must never become a protocol-relative URL.
    let path = String(pathname || '/').replace(/^\/+/, '/').replace(/sonder-editor\/app\/?$/, '');
    if (path.endsWith('/api/')) path = path.slice(0, -'api/'.length);
    return path.endsWith('/') ? path : `${path}/`;
}
function logo(url, cls, fallback) {
    const image = el('img', cls); image.src = url; image.alt = fallback;
    image.addEventListener('error', () => image.replaceWith(el('span', '', fallback)), { once: true });
    return image;
}
export function mountHeader(state, actions) {
    const header = el('header', 'sl-header');
    const brand = el('div', 'sl-brand');
    const wordmark = el('div', 'sl-wordmark', 'SONDER'); wordmark.append(el('small', '', 'EDITOR'));
    const attribution = el('div', 'sl-attribution', 'powered by');
    attribution.append(logo(`${comfyRoot(location.pathname)}assets/images/comfy-logo-wordmark.svg`, '', 'Comfy'));
    brand.append(logo('static/assets/sonder-mark.svg', 'sl-mark', 'S'), wordmark, attribution);
    const env = environmentState(state.status), right = el('div', 'sl-row');
    const status = button('', e => actions.statusMenu(e.currentTarget), { title: 'ComfyUI connection and package versions' });
    status.classList.add('sl-status', `sl-${env.kind}`);
    status.append(logo(`${comfyRoot(location.pathname)}assets/images/comfy-logo-mono.svg`, 'sl-status-icon', ''), el('span', '', 'ComfyUI'), el('span', `sl-status-dot sl-${env.kind}`), el('span', `sl-${env.kind}`, env.label));
    right.append(status, button('⚙', e => actions.settingsMenu(e.currentTarget), { title: 'Launcher preferences' }));
    header.append(brand, right);
    return component(header);
}
