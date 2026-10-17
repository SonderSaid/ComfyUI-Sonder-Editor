// Safe, small Markdown subset. Author text never becomes HTML or attributes.
import { SPACE, TYPE } from "./editor_theme.js";

// Theme-sized blocks: browser defaults made a heading the panel's largest
// text (18px h2 against 12px panel text). Body size and colour inherit.
const HEADING_SIZE = { 1: TYPE.t12, 2: TYPE.t12, 3: TYPE.t11 };
const BLOCK_MARGIN = `margin:${SPACE.s1}px 0;`;
const blockCss = (tag) => {
    const heading = /^h([1-3])$/.exec(tag);
    if (heading) return `${BLOCK_MARGIN}font-size:${HEADING_SIZE[heading[1]]}px;font-weight:${TYPE.fwBold};line-height:1.35;`;
    if (tag === "ul" || tag === "ol") return `${BLOCK_MARGIN}padding-left:${SPACE.s4}px;`;
    return BLOCK_MARGIN;
};
function block(fragment, tag) {
    const el = document.createElement(tag);
    el.style.cssText = blockCss(tag) + (fragment.children.length ? "" : "margin-top:0;");
    fragment.append(el);
    return el;
}

function inline(parent, text, depth = 0) {
    if (depth > 8) {
        parent.append(document.createTextNode(text));
        return;
    }
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^\s)]+\))/g;
    let last = 0;
    for (const match of text.matchAll(pattern)) {
        parent.append(document.createTextNode(text.slice(last, match.index)));
        const token = match[0];
        let element = null;
        if (token.startsWith("`")) {
            element = document.createElement("code");
            element.textContent = token.slice(1, -1);
        } else if (token.startsWith("**")) {
            element = document.createElement("strong");
            inline(element, token.slice(2, -2), depth + 1);
        } else if (token.startsWith("*") || token.startsWith("_")) {
            element = document.createElement("em");
            inline(element, token.slice(1, -1), depth + 1);
        } else {
            const parts = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
            try {
                const url = new URL(parts[2]);
                if (url.protocol === "http:" || url.protocol === "https:") {
                    element = document.createElement("a");
                    element.textContent = parts[1];
                    element.href = url.href;
                    element.target = "_blank";
                    element.rel = "noopener noreferrer";
                }
            } catch (_) {}
        }
        parent.append(element || document.createTextNode(token));
        last = match.index + token.length;
    }
    parent.append(document.createTextNode(text.slice(last)));
}

export function renderPanelMarkdown(text) {
    const fragment = document.createDocumentFragment();
    const lines = String(text ?? "")
        .replace(/\r\n?/g, "\n")
        .split("\n");
    let paragraph = null,
        list = null;
    for (const line of lines) {
        const heading = /^(#{1,3})\s+(.+)$/.exec(line);
        const item = /^\s*(?:([-*])\s+|(\d+)\.\s+)(.*)$/.exec(line);
        if (!line.trim()) {
            paragraph = list = null;
            continue;
        }
        if (heading) {
            paragraph = list = null;
            inline(block(fragment, "h" + heading[1].length), heading[2]);
            continue;
        }
        if (item) {
            paragraph = null;
            const tag = item[2] ? "ol" : "ul";
            if (!list || list.tagName.toLowerCase() !== tag) list = block(fragment, tag);
            const el = document.createElement("li");
            inline(el, item[3]);
            list.append(el);
            continue;
        }
        list = null;
        if (!paragraph) paragraph = block(fragment, "p");
        else paragraph.append(document.createElement("br"));
        inline(paragraph, line);
    }
    const last = fragment.children[fragment.children.length - 1];
    if (last) last.style.marginBottom = "0";
    return fragment;
}

const TITLE_HEADING = /^(#{1,3})\s+(.+)$/;
const TITLE_BOLD = /^\s*\*\*([^*\n]+)\*\*\s*$/;

/**
 * A note's title, read from its first line: a `#`-`###` heading or a line
 * that is only bold text. Returns `{title, body}` with `title` as plain text,
 * or null when the first line is neither or nothing follows it (a title-only
 * note renders as it always did).
 */
export function splitPanelMarkdownTitle(text) {
    const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
    const first = lines.findIndex((line) => line.trim());
    if (first < 0) return null;
    const match = TITLE_HEADING.exec(lines[first])?.[2] ?? TITLE_BOLD.exec(lines[first])?.[1];
    if (match === undefined) return null;
    const body = lines.slice(first + 1).join("\n").replace(/^(\s*\n)+/, "").trimEnd();
    if (!body.trim()) return null;
    const holder = document.createElement("span");
    inline(holder, match.trim());
    const title = holder.textContent.trim();
    return title ? { title, body } : null;
}
