// Safe, small Markdown subset. Author text never becomes HTML or attributes.
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
            const el = document.createElement("h" + heading[1].length);
            inline(el, heading[2]);
            fragment.append(el);
            continue;
        }
        if (item) {
            paragraph = null;
            const tag = item[2] ? "ol" : "ul";
            if (!list || list.tagName.toLowerCase() !== tag) {
                list = document.createElement(tag);
                fragment.append(list);
            }
            const el = document.createElement("li");
            inline(el, item[3]);
            list.append(el);
            continue;
        }
        list = null;
        if (!paragraph) {
            paragraph = document.createElement("p");
            fragment.append(paragraph);
        } else paragraph.append(document.createElement("br"));
        inline(paragraph, line);
    }
    return fragment;
}
