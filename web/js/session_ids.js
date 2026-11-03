// Random ids for editor sessions and tab secrets. `getRandomValues` because
// `crypto.randomUUID` is missing on an insecure origin, which is what a browser on
// another machine has over plain HTTP with `--listen`. No imports: the mounted tab
// loads this before the ComfyUI API shim is installed.

const hex = values => Array.from(values, value => value.toString(16).padStart(2, "0")).join("");

export function randomToken(bytes = 16) {
    const values = new Uint8Array(bytes);
    globalThis.crypto.getRandomValues(values);
    return hex(values);
}

export function newSessionId(prefix = "editor") {
    return `${prefix}-${randomToken()}`;
}

/** A version 4 UUID, for ids a route checks by that shape (run requests). */
export function randomUuid() {
    if (typeof globalThis.crypto.randomUUID === "function") return globalThis.crypto.randomUUID();
    const values = new Uint8Array(16);
    globalThis.crypto.getRandomValues(values);
    values[6] = (values[6] & 0x0f) | 0x40;
    values[8] = (values[8] & 0x3f) | 0x80;
    const text = hex(values);
    return `${text.slice(0, 8)}-${text.slice(8, 12)}-${text.slice(12, 16)}-${text.slice(16, 20)}-${text.slice(20)}`;
}
