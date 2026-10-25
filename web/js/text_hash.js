// A small text hash that needs no Web Crypto (HTTP LAN origins lack it): two
// 32-bit FNV-style lanes over the string's UTF-16 code units, as 16 hex digits.
// Not cryptographic. server/view_timing.py `two_lane_hash` mirrors it exactly.
export function twoLaneHash(text) {
    let a = 0x811c9dc5, b = 0x9e3779b9;
    for (let i = 0; i < text.length; i++) {
        const c = text.charCodeAt(i);
        a = Math.imul(a ^ c, 0x01000193);
        b = Math.imul(b ^ c, 0x85ebca6b);
    }
    return `${(a >>> 0).toString(16).padStart(8, "0")}${(b >>> 0).toString(16).padStart(8, "0")}`;
}
