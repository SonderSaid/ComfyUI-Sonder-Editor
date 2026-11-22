// Page fixture for the shared Context-chip editor: one real
// `createPromptDocumentEditor` mounted on a blank page, with helpers the
// runner calls through `evaluate`. Moved from the 2026-11-20 Firefox probe and
// pointed at this repository's `web/js`.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
// SONDER_JS_DIR points the page at another copy of web/js (a release, say).
const JS = process.env.SONDER_JS_DIR || resolve(HERE, "..", "..", "..", "web", "js");

const MARKUP = `<!doctype html><meta charset="utf-8"><title>Sonder prompt chip selection</title>
<style>body{margin:24px;font:16px sans-serif}#host{width:540px}#other{margin-top:40px}</style>
<div id="host"></div><div id="other">Outside field and other selectable text</div>
<script type="module" src="/fixture.js"></script>`;

// `setup` mounts a variant; `point` maps a model position to page pixels;
// `state` reports text, model, attachments, focus and the live selection.
const FIXTURE = String.raw`
import { createPromptDocumentEditor } from '/js/prompt_context_chips.js';
const text = (node_id, text) => ({ node_id, type: 'text', text });
const chip = (node_id) => ({ node_id, type: 'attachment', attachment_id: node_id });
let e, changes;
const info = (n, o) => {
  const p = n?.nodeType === 3 ? n.parentElement : n;
  return [p === e ? '$root' : p?.closest?.('[data-node-id]')?.dataset.nodeId || p?.id || p?.nodeName, o];
};
const selection = () => {
  const s = window.getSelection();
  return { text: s.toString(), ranges: Array.from({ length: s.rangeCount }, (_, i) => {
    const r = s.getRangeAt(i); return [info(r.startContainer, r.startOffset), info(r.endContainer, r.endOffset)]; }),
    active: document.activeElement === e ? '$editor'
      : document.activeElement?.closest?.('[data-node-id]')?.dataset.nodeId
        ? 'chip:' + document.activeElement.closest('[data-node-id]').dataset.nodeId
        : document.activeElement?.tagName || null };
};
const record = () => [e.promptDocument.nodes.map((n) => [n.node_id, n.type === 'text' ? n.text : n.attachment_id]),
  e.promptAttachments.map((a) => a.attachment_id)];
window.state = () => ({ doc: e.value, data: record(), changes: [...changes], selection: selection() });
window.setup = ({ variant = 'pills', nodes: custom = null } = {}) => {
  const nodes = custom ? custom : variant === 'plain' ? [text('L', 'KEEP-LEFT alpha ONE\nline TWO beta KEEP-RIGHT')]
    : variant === 'one' ? [text('L', 'KEEP-LEFT alpha '), chip('C'), text('M', ' beta KEEP-RIGHT')]
    : [text('L', 'KEEP-LEFT alpha '), chip('C'), text('M', ' beta ONE\nline TWO '), chip('D'), text('R', ' omega KEEP-RIGHT')];
  changes = [];
  e = createPromptDocumentEditor({ document: { nodes },
    attachments: nodes.filter((n) => n.type === 'attachment').map((n) =>
      ({ attachment_id: n.attachment_id, kind: 'reference', source: {}, config: {} })),
    attachmentLabelFor: (a) => 'CHIP ' + a.attachment_id,
    onChange: (d) => changes.push(d.reason) });
  window.e = e;
  document.getElementById('host').replaceChildren(e.promptField || e);
  e.focus();
  getSelection().collapse(...endpoint([nodes[0].node_id, 0]));
  return window.state();
};
const endpoint = ([id, offset]) => {
  if (id === '$outside') return [document.querySelector('#other').firstChild, offset];
  const el = e.querySelector('[data-node-id="' + id + '"]');
  if (el.dataset.nodeType === 'attachment')
    return [el.querySelector('[data-sonder-context-chip-label]').firstChild, offset];
  return [el.firstChild, offset];
};
window.selectRange = (a, b) => { getSelection().setBaseAndExtent(...endpoint(a), ...endpoint(b)); return selection(); };
window.point = (where) => {
  const [n, o] = endpoint(where); const r = document.createRange(); r.setStart(n, o); r.collapse(true);
  const b = r.getBoundingClientRect();
  return { x: Math.round(b.x), y: Math.round(b.y + b.height / 2) };
};
window.focusEditor = () => { e.focus(); return document.activeElement === e; };
// What the author sees: the highlighted prose and the field's prose, with chip
// labels and zero-width padding left out (what the model's text holds).
const prose = (root) => { root.querySelectorAll?.('[data-node-type="attachment"]').forEach((n) => n.remove());
  root.querySelectorAll?.('br').forEach((b) => b.replaceWith('\n'));
  return root.textContent.split('\u200b').join('').split('\ufeff').join(''); };
window.highlighted = () => { const s = getSelection(); return s.rangeCount ? prose(s.getRangeAt(0).cloneContents()) : ''; };
window.screenProse = () => prose(e.cloneNode(true));
`;

// Clipboard qualification page (clipboard.mjs): `copy`/`cut` write text/plain
// plus a custom type; every `paste` records which types arrived.
export const FRAGMENT_TYPE = "application/x-sonder-prompt-fragment";
const CLIP_MARKUP = `<!doctype html><meta charset="utf-8"><title>Sonder clipboard qualification</title>
<style>body{margin:24px;font:16px sans-serif}div[contenteditable]{border:1px solid #888;padding:8px;margin:8px 0;min-height:24px}</style>
<div id="src" contenteditable="true">source prose to copy</div><div id="dst" contenteditable="true">paste here</div>
<script>
window.clipLog = [];
window.nonce = '';
const write = (event) => {
  event.clipboardData.setData('text/plain', 'SONDER-PLAIN-' + window.nonce);
  event.clipboardData.setData('${"application/x-sonder-prompt-fragment"}',
    JSON.stringify({ format: 'sonder-prompt-fragment@1', nonce: window.nonce }));
  event.preventDefault();
  window.clipLog.push({ type: event.type, types: [...event.clipboardData.types] });
};
document.addEventListener('copy', write);
document.addEventListener('cut', write);
document.addEventListener('paste', (event) => {
  const data = event.clipboardData;
  window.clipLog.push({ type: 'paste', types: [...data.types], plain: data.getData('text/plain'),
    custom: data.getData('${"application/x-sonder-prompt-fragment"}') });
  event.preventDefault();
});
window.selectSource = () => { const s = document.getElementById('src'); s.focus();
  getSelection().selectAllChildren(s); return getSelection().toString(); };
window.focusTarget = () => { const d = document.getElementById('dst'); d.focus();
  getSelection().selectAllChildren(d); getSelection().collapseToEnd(); return document.activeElement === d; };
</script>`;

export function serveFixture() {
    const server = createServer(async (request, response) => {
        try {
            const url = new URL(request.url, "http://127.0.0.1");
            if (url.pathname === "/") {
                response.setHeader("Content-Type", "text/html");
                return response.end(MARKUP);
            }
            if (url.pathname === "/clip") {
                response.setHeader("Content-Type", "text/html");
                return response.end(CLIP_MARKUP);
            }
            response.setHeader("Content-Type", "text/javascript");
            if (url.pathname === "/fixture.js") return response.end(FIXTURE);
            if (/^\/js\/[\w.-]+\.js$/.test(url.pathname))
                return response.end(await readFile(join(JS, url.pathname.split("/").at(-1))));
            response.writeHead(404).end();
        } catch (error) {
            response.writeHead(500).end(String(error));
        }
    });
    return new Promise((done) => server.listen(0, "127.0.0.1", () => done(server)));
}
