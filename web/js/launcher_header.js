import { el, button, component } from './launcher_view.js';

// Where the packages are managed, as the docs give it (docs/sonder-app.md).
const SETTINGS_ROUTE = 'open a project, click ⚙ at the right end of the toolbar above the timeline, then go to Settings ▸ Sonder App (experimental)';
// The one sentence about the generation packages, said by the header notice; the
// Open bar points back to it.
export const PACKAGES_NEEDED = `Generate needs the comfy-sdk and comfy-api-proxy packages at supported versions. To install them, ${SETTINGS_ROUTE} and click Install. You can still open and edit projects.`;
const CHECK_AGAIN = 'click ComfyUI at the top right and choose Check again';
// The status as the page knows it: null until the first answer, `{slow: true}`
// while that first answer is late, `{error, network}` when the call failed.
export function environmentState(status) {
    if (!status) return { label: 'Checking…', kind: '', message: 'Checking ComfyUI. Right after ComfyUI starts this can take a few seconds.' };
    if (status.slow) return { label: 'Checking…', kind: 'warn', message: `ComfyUI is slow to answer. It may still be starting. Wait a moment, or ${CHECK_AGAIN}.` };
    if (status.error) {
        return { label: 'Unreachable', kind: 'bad', message: status.network
            ? `ComfyUI didn’t answer. Check that it is running, then ${CHECK_AGAIN}.`
            : `ComfyUI couldn’t report its status: ${String(status.error).replace(/[.\s]+$/, '')}. ${CHECK_AGAIN[0].toUpperCase()}${CHECK_AGAIN.slice(1)}, or look at the ComfyUI console.` };
    }
    const install = status.install || {};
    if ([install.sdk, install.proxy].some(p => p?.installed && !p.supported)) return { label: 'Unsupported', kind: 'bad', message: `A generation package has an unsupported version. To see which and what to do, ${SETTINGS_ROUTE}.` };
    // Missing packages come before a proxy error: a failed Generate records "not
    // installed" as the proxy's error, and the remedy is the install, not the connection.
    if (!install.ready) return { label: 'Generate unavailable', kind: 'warn', message: PACKAGES_NEEDED };
    if (status.proxy?.error) return { label: 'Connection problem', kind: 'bad', message: status.proxy.error };
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
