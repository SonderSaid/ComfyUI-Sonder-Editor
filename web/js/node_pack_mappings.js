// The Manager's node → pack mappings and the lookup over them. Large, so each page
// fetches them at most once and only when a workflow names a missing node. Same
// origin; without the Manager, or if it doesn't answer, null: class names stand alone.
export const NODE_PACKS_PATH = "/v2/customnode/getmappings?mode=cache";

export async function fetchNodePackMappings(url, fetchImpl = (...args) => fetch(...args)) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
        const response = await fetchImpl(url, { signal: controller.signal });
        const payload = response.ok ? await response.json() : null;
        return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : null;
    } catch {
        return null;
    } finally {
        clearTimeout(timer);
    }
}

// A workflow switch replaces the Sonder App's Controls host, so what outlives it
// keeps the one fetch: the page's, even when the answer was that there's no Manager.
let once = null;
export function nodePackMappingsOnce(url, fetchImpl) {
    once ||= fetchNodePackMappings(url, fetchImpl);
    return once;
}

// Missing node classes grouped by the node pack that provides them, from the
// Manager's mappings ({pack: [[classes], {title, title_aux, nodename_pattern}]}).
// Exact class lists first, then a pack's name pattern; a class several packs
// list names them all. Without mappings (no Manager) each class stands alone.
const packIndexes = new WeakMap();
function packIndex(mappings) {
    let index = packIndexes.get(mappings);
    if (index) return index;
    index = { byClass: new Map(), patterns: [] };
    for (const [key, entry] of Object.entries(mappings)) {
        if (!Array.isArray(entry)) continue;
        const info = entry[1] && typeof entry[1] === "object" ? entry[1] : {};
        const title = String(info.title || info.title_aux || key);
        for (const name of Array.isArray(entry[0]) ? entry[0] : []) {
            const titles = index.byClass.get(name) || [];
            if (!titles.includes(title)) titles.push(title);
            index.byClass.set(name, titles);
        }
        if (info.nodename_pattern) {
            try { index.patterns.push([new RegExp(String(info.nodename_pattern)), title]); } catch { /* a Python-only pattern */ }
        }
    }
    packIndexes.set(mappings, index);
    return index;
}
export function missingNodePacks(classes, mappings) {
    const groups = new Map();
    const index = mappings && typeof mappings === "object" ? packIndex(mappings) : null;
    for (const name of classes || []) {
        let titles = index?.byClass.get(name) || [];
        if (!titles.length && index) titles = [...new Set(index.patterns.filter(([re]) => re.test(name)).map(([, title]) => title))];
        const pack = titles.length ? titles.slice(0, 3).join(" or ") : "";
        if (!groups.has(pack)) groups.set(pack, []);
        groups.get(pack).push(name);
    }
    return [...groups].map(([pack, names]) => ({ pack, classes: names }));
}
