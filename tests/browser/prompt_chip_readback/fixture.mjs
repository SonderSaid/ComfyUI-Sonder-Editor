// Page fixture for the readback harness: one real `createPromptDocumentEditor`
// mounted the way the Firefox 159 repro mounted it (compact, narrow, scrolled,
// the `@` mention menu armed), with helpers the runner calls through
// `evaluate`. Nothing here reads layout while an edit is being handled: the
// stale read only shows when Sonder's own `input` handler is the first reader.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
// SONDER_JS_DIR points the page at another copy of web/js (a release, say).
export const JS = process.env.SONDER_JS_DIR || resolve(HERE, "..", "..", "..", "web", "js");

const MARKUP = `<!doctype html><meta charset="utf-8"><title>Sonder prompt readback</title>
<style>body{margin:24px;font-family:system-ui}#host{width:241px}
#rich{margin-top:40px;width:300px;border:1px dashed #888;padding:6px}</style>
<div id="host"></div>
<div id="rich"><p>Dropped <b>bold</b> para</p><p>second para</p><style>.dropped-style{color:red}</style><span hidden>HIDDEN</span></div>
<script type="module" src="/fixture.js"></script>`;

const FIXTURE = String.raw`
import * as chips from '/js/prompt_context_chips.js';
const { createPromptDocumentEditor, installPromptMentionMenu } = chips;
const host = document.querySelector('#host');
const ZW = '\u200b';
let box, dispose;
// The oracle's evidence. A capture listener on the host runs BEFORE the
// editor's own input handler, so it sees each text span in the shape the
// browser's edit left, before any repaint. It only clones (structure, no
// layout): a layout read here would mask the Firefox 159 defect.
let captured = [];
host.addEventListener('input', (event) => {
  if (!box || !box.contains(event.target)) return;
  for (const span of box.querySelectorAll('[data-node-type="text"]'))
    captured.push({ trusted: event.isTrusted, inputType: event.inputType, clone: span.cloneNode(true) });
}, true);
// What the field holds, read by structure here so 0.9.2 modules can be judged
// too: text without U+200B, <br> as \n, chips and decorations left out.
const structural = (root) => {
  const clone = root.cloneNode(true);
  clone.querySelectorAll('[data-node-type="attachment"],[data-sonder-decoration],style,script,[hidden]')
    .forEach((n) => n.remove());
  clone.querySelectorAll('br').forEach((b) => b.replaceWith('\n'));
  return clone.textContent.split(ZW).join('');
};
const order = () => box.promptDocument.nodes.map((n) => n.type === 'text' ? n.text
  : '[' + n.attachment_id + ']').join('');
const describe = (n, o) => !n ? null : n.nodeType === 3
  ? { type: 'text', in: n.parentElement?.dataset?.sonderPromptHandle ? 'mark'
      : n.parentElement?.dataset?.nodeType || n.parentElement?.tagName, offset: o }
  : { type: n.dataset?.sonderPromptHandle ? 'mark' : n.dataset?.nodeType || n.tagName, offset: o,
      children: n.childNodes.length };
const textOffset = (node, offset) => {
  if (!node || !box.contains(node)) return null;
  const r = document.createRange(); r.setStart(box, 0); r.setEnd(node, offset);
  return r.toString().split(ZW).join('').length;
};
window.mount = ({ text = null, nodes = null, width = 241, compact = true } = {}) => {
  dispose?.();
  captured = [];
  host.style.width = width + 'px';
  box = nodes
    ? createPromptDocumentEditor({ document: { nodes }, compact,
        attachments: nodes.filter((n) => n.type === 'attachment').map((n) =>
          ({ attachment_id: n.attachment_id, kind: 'reference', source: {}, config: {} })),
        attachmentLabelFor: (a) => 'CHIP ' + a.attachment_id })
    : createPromptDocumentEditor({ text, compact });
  host.replaceChildren(box.promptField || box);
  dispose = installPromptMentionMenu(box, { sources: () => ['Test'],
    onAccepted: ({ handle, replaceTextRange }) => box.insertText('@' + handle, { replaceTextRange }) });
  box.focus();
  return window.snapshot();
};
// The caret at the very end, the field scrolled to it, as the repro did.
window.caretEnd = () => {
  box.focus();
  box.scrollTop = box.scrollHeight;
  const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  let last = null;
  while (walker.nextNode()) if (walker.currentNode.nodeValue.split(ZW).join('')) last = walker.currentNode;
  const r = document.createRange();
  r.setStart(last, last.nodeValue.length); r.collapse(true);
  getSelection().removeAllRanges(); getSelection().addRange(r);
  return window.snapshot();
};
const mark = (n = 0) => box.querySelectorAll('[data-sonder-prompt-handle]')[n];
const spanOf = (id) => box.querySelector('[data-node-id="' + id + '"]');
// Named DOM points, so a case states its shape exactly.
const POINTS = {
  'text:5': () => [spanOf('t').firstChild, 5],
  'mark:0': () => [mark(), 0],
  'mark:1': () => [mark(), 1],
  'markText:0': () => [mark().firstChild, 0],
  'markText:5': () => [mark().firstChild, 5],
  'span:1': () => [spanOf('t'), 1],
  'span:2': () => [spanOf('t'), 2],
};
window.select = (from, to = from) => {
  box.focus();
  getSelection().setBaseAndExtent(...POINTS[from](), ...POINTS[to]());
  return window.snapshot();
};
window.snapshot = () => {
  const s = getSelection();
  const r = s.rangeCount ? s.getRangeAt(0) : null;
  return {
    model: box.value, order: order(), dom: structural(box),
    active: document.activeElement === box,
    caret: r ? [textOffset(r.startContainer, r.startOffset), textOffset(r.endContainer, r.endOffset)] : null,
    shape: r ? [describe(r.startContainer, r.startOffset), describe(r.endContainer, r.endOffset)] : null,
    children: [...box.childNodes].map((n) => n.nodeType === 3 ? '#text'
      : (n.dataset?.nodeType || n.tagName) + (n.dataset?.nodeType === 'text'
        ? '(' + [...n.childNodes].map((c) => c.nodeType === 3 ? 't' : c.dataset?.sonderPromptHandle ? 'm' : c.tagName).join('') + ')' : '')),
  };
};
// The oracle, read in its own task: every span shape captured during the case
// is read by the production reader (when this web/js exports it; 0.9.2 does
// not) and, mounted offscreen with the field's styles and width, by innerText
// after layout. Differences are reported, not judged.
const painted = (span) => [...span.childNodes].every((c) => c.nodeType === 3
  || (c.dataset?.sonderPromptHandle === '1' && [...c.childNodes].every((i) => i.nodeType === 3)));
window.oracle = async () => {
  await new Promise((done) => requestAnimationFrame(() => setTimeout(done, 0)));
  const stage = document.createElement('div');
  const style = getComputedStyle(box);
  stage.style.cssText = 'position:absolute;left:-10000px;top:0;white-space:pre-wrap;overflow-wrap:anywhere;'
    + 'width:' + box.clientWidth + 'px;font-family:' + style.fontFamily + ';font-size:' + style.fontSize
    + ';line-height:' + style.lineHeight + ';';
  document.body.append(stage);
  const reader = chips.promptSpanAuthoredText || null;
  const rows = [];
  for (const { trusted, inputType, clone } of captured) {
    stage.replaceChildren(clone);
    stage.getBoundingClientRect();
    const inner = clone.innerText.split(ZW).join('');
    const read = reader ? reader(clone) : structural(clone);
    rows.push({ trusted, inputType, reshaped: !painted(clone), read, inner });
  }
  stage.remove();
  return { reader: reader ? 'promptSpanAuthoredText' : 'fixture structural',
    captured: rows.length, reshaped: rows.filter((r) => r.reshaped).length,
    diffs: rows.filter((r) => r.read !== r.inner)
      .map(({ inputType, reshaped, read, inner }) => ({ inputType, reshaped, read, inner })) };
};
// A point just right of the caret at the very end, for a real click there.
window.endPoint = () => {
  window.caretEnd();
  const r = getSelection().getRangeAt(0).getBoundingClientRect();
  const b = box.getBoundingClientRect();
  getSelection().removeAllRanges();
  box.blur();
  return { x: Math.round(Math.min(b.right - 5, r.right + 2)), y: Math.round(r.top + r.height / 2) };
};
// A drag source with rich markup: select it whole, and give both ends' points.
window.richDrag = () => {
  const rich = document.querySelector('#rich');
  getSelection().selectAllChildren(rich);
  const from = rich.querySelector('b').getBoundingClientRect();
  const field = box.getBoundingClientRect();
  return { from: { x: Math.round(from.left + from.width / 2), y: Math.round(from.top + from.height / 2) },
    to: { x: Math.round(field.left + 12), y: Math.round(field.top + 10) } };
};
`;

export function serveFixture() {
    const server = createServer(async (request, response) => {
        try {
            const url = new URL(request.url, "http://127.0.0.1");
            if (url.pathname === "/") {
                response.setHeader("Content-Type", "text/html");
                return response.end(MARKUP);
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
