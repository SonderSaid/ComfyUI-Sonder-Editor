// Whole-file blobs of output media for Output View panes, one cache per
// editor page. An entry is keyed by its file ({type, subfolder, filename}) and
// revision (size and modification time, from the read), so a cached rerun
// under a new prompt id reuses the blob and changed bytes at the same path are
// never shown as the old file. Whole files, as the editor's own playback uses,
// so a seek is exact.
//
// Holders pin an entry; an idle entry (no holder) stays until the soft cap
// needs its bytes, oldest first. A file over the per-file ceiling is refused
// ("too_large") without being kept. Teardown aborts fetches and revokes URLs.

export const SOFT_CAP_BYTES = 512 * 1024 * 1024;
export const FILE_CEILING_BYTES = 256 * 1024 * 1024;

export const fileKey = entry => {
    const file = entry?.file;
    if (!file?.filename) return "";
    return `${file.type || "output"}/${file.subfolder || ""}/${file.filename}#${entry.revision || ""}`;
};

export function createOutputMediaCache({ fetchImpl = (...args) => globalThis.fetch(...args), urlFor,
    softCapBytes = SOFT_CAP_BYTES, fileCeilingBytes = FILE_CEILING_BYTES,
    createObjectURL = blob => URL.createObjectURL(blob), revokeObjectURL = url => URL.revokeObjectURL(url),
    now = () => Date.now() } = {}) {
    if (typeof urlFor !== "function") throw new TypeError("The output media cache needs a URL builder.");
    const entries = new Map();
    let destroyed = false;

    const drop = (key, entry) => {
        entries.delete(key);
        entry.controller?.abort();
        if (entry.blobUrl) revokeObjectURL(entry.blobUrl);
        entry.blobUrl = null;
    };
    const total = () => [...entries.values()].reduce((sum, entry) => sum + (entry.bytes || 0), 0);
    const sweep = () => {
        let bytes = total();
        if (bytes <= softCapBytes) return;
        const idle = [...entries.entries()].filter(([, entry]) => !entry.holders.size && entry.state === "ready")
            .sort((a, b) => a[1].lastUsed - b[1].lastUsed);
        for (const [key, entry] of idle) {
            if (bytes <= softCapBytes) break;
            bytes -= entry.bytes || 0;
            drop(key, entry);
        }
    };

    async function load(key, entry, url) {
        try {
            const response = await fetchImpl(url, { signal: entry.controller.signal });
            if (response.status === 404) return { error: "missing_file" };
            if (!response.ok) return { error: "failed" };
            const declared = Number(response.headers?.get?.("Content-Length"));
            if (Number.isFinite(declared) && declared > fileCeilingBytes) {
                entry.controller.abort();
                return { error: "too_large" };
            }
            const type = response.headers?.get?.("Content-Type") || "";
            let blob;
            if (response.body?.getReader) {
                const reader = response.body.getReader();
                const chunks = [];
                let size = 0;
                for (;;) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    size += value.byteLength;
                    if (size > fileCeilingBytes) {
                        entry.controller.abort();
                        return { error: "too_large" };
                    }
                    chunks.push(value);
                }
                blob = new Blob(chunks, type ? { type } : {});
            } else {
                blob = await response.blob();
                if (blob.size > fileCeilingBytes) return { error: "too_large" };
            }
            if (destroyed || entries.get(key) !== entry) return { error: "aborted" };
            entry.bytes = blob.size;
            entry.blobUrl = createObjectURL(blob);
            entry.state = "ready";
            return { url: entry.blobUrl };
        } catch (error) {
            return { error: error?.name === "AbortError" ? "aborted" : "failed" };
        }
    }

    return {
        // A pane's claim on one entry's media: `ready` resolves to {url} or
        // {error: "missing_file"|"too_large"|"failed"|"aborted"}. Release once.
        acquire(entry, holder) {
            const key = fileKey(entry);
            if (!key || destroyed) return { ready: Promise.resolve({ error: key ? "aborted" : "failed" }), release() {} };
            let cached = entries.get(key);
            if (!cached) {
                cached = { holders: new Set(), bytes: 0, blobUrl: null, state: "loading", lastUsed: now(), controller: new AbortController() };
                entries.set(key, cached);
                cached.promise = load(key, cached, urlFor(entry.file)).then(result => {
                    // A refusal or failure is never kept: the next claim tries again.
                    if (result.error && entries.get(key) === cached) drop(key, cached);
                    else sweep();
                    return result;
                });
            }
            cached.holders.add(holder);
            cached.lastUsed = now();
            const mine = cached;
            let released = false;
            return {
                ready: mine.promise,
                release() {
                    if (released) return;
                    released = true;
                    mine.holders.delete(holder);
                    mine.lastUsed = now();
                    // A fetch nobody waits for any more is abandoned, and a
                    // forgotten file goes once its last pane lets it go.
                    if (!mine.holders.size && (mine.state === "loading" || mine.forgotten) && entries.get(key) === mine) drop(key, mine);
                    else sweep();
                },
            };
        },
        // A held file the server reports changed or removed: forget its bytes,
        // now if no pane shows it, else when the last one lets it go.
        forget(entry) {
            const key = fileKey(entry);
            const cached = entries.get(key);
            if (!cached) return;
            if (cached.holders.size) cached.forgotten = true;
            else drop(key, cached);
        },
        snapshot: () => ({ entries: entries.size, bytes: total(), held: [...entries.values()].filter(e => e.holders.size).length }),
        clear() {
            for (const [key, entry] of [...entries]) drop(key, entry);
        },
        destroy() {
            destroyed = true;
            for (const [key, entry] of [...entries]) drop(key, entry);
        },
    };
}
