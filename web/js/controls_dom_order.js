// Writes text only when it differs. Setting textContent replaces the text node
// even with equal text, and a press whose text node is replaced between
// mousedown and mouseup gets no click (Chromium): the click is lost.
export function setText(el, text) {
    const next = String(text ?? "");
    if (el.textContent !== next) el.textContent = next;
}

// Read by screen readers, never seen: descriptions an element points at.
export const VISUALLY_HIDDEN_CSS = "position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;";

// Adds or removes one id in an element's aria-describedby, keeping the others:
// several descriptions (why a setting is off, the node's own help) share it.
export function describedBy(el, id, on) {
    if (!el || !id) return;
    const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    const next = on ? (ids.includes(id) ? ids : [...ids, id]) : ids.filter(item => item !== id);
    if (next.join(" ") === ids.join(" ")) return;
    if (next.length) el.setAttribute("aria-describedby", next.join(" "));
    else el.removeAttribute("aria-describedby");
}

// Puts `wanted` in order inside `body` without moving the element that holds
// focus: moving it blurs it, which ends a rename or discards a draft, and a
// click whose mousedown blurred a field would land on a moved button. Its
// siblings move around it instead.
export function placeInOrder(body, wanted) {
    // Already in place: move nothing. Re-appending an element detaches it for
    // a moment, and a press inside it (a mousedown that blurred a field whose
    // save re-renders) then gets no click.
    const tail = [...body.children].slice(-wanted.length);
    if (tail.length === wanted.length && tail.every((el, i) => el === wanted[i])) return;
    const active = document.activeElement;
    const anchor = wanted.find(el => el.parentElement === body && el.contains(active));
    if (!anchor) {
        for (const el of wanted) body.append(el);
        return;
    }
    const at = wanted.indexOf(anchor);
    for (const el of wanted.slice(0, at)) anchor.before(el);
    let previous = anchor;
    for (const el of wanted.slice(at + 1)) {
        previous.after(el);
        previous = el;
    }
}
