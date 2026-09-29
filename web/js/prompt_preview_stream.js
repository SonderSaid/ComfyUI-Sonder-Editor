// @server-mirror server/routes.py::PROMPT_PREVIEW_STREAM
// @server-mirror server/routes.py::PROMPT_PREVIEW_PROJECTIONS
// Scope and parity disposition: tests/test_mutation_authoring_contract.py::MIRRORED_MODULES
// Reading the streamed paired prompt preview (`plans/cut-read-fanout.md` §3).
//
// Leaf module: DOM-free and host-free, so it can be tested under Node.
// The compile route answers `preview_response: "stream-v1"` with newline-
// delimited JSON, one record per requested projection, windowed first:
//
//   {"projection": "windowed", "status": 200, "payload": {...},
//    "candidate_base_modified_at": "..."}
//
// A refusal before the stream opens (a version conflict, a bad request) is an
// ordinary JSON response instead, so the caller checks `isPromptPreviewStream`
// before reading.

export const PROMPT_PREVIEW_STREAM = "stream-v1";
export const PROMPT_PREVIEW_PROJECTIONS = Object.freeze(["windowed", "scene"]);
export const PROMPT_PREVIEW_STREAM_CONTENT_TYPE = "application/x-ndjson";

export class PromptPreviewStreamError extends Error {
    constructor(message) {
        super(message);
        this.name = "PromptPreviewStreamError";
    }
}

export function isPromptPreviewStream(response) {
    const type = String(response?.headers?.get?.("Content-Type") || "");
    return response?.ok === true
        && type.split(";")[0].trim().toLowerCase() === PROMPT_PREVIEW_STREAM_CONTENT_TYPE;
}

function parseRecord(line, requested) {
    let record;
    try {
        record = JSON.parse(line);
    } catch {
        throw new PromptPreviewStreamError("The prompt preview stream sent an unreadable record.");
    }
    if (!record || typeof record !== "object" || Array.isArray(record)
            || !requested.has(record.projection)
            || !Number.isInteger(record.status)
            || !record.payload || typeof record.payload !== "object"
            || Array.isArray(record.payload)) {
        throw new PromptPreviewStreamError("The prompt preview stream sent a malformed record.");
    }
    return record;
}

/**
 * Deliver each record of a streamed preview as it arrives.
 *
 * Records may arrive split across chunks, including inside a multi-byte
 * character. A malformed record throws: nothing after it can be attributed
 * with confidence, so the caller fails every branch still unsettled. Ending
 * without a record for some projection is not an error here; the caller owns
 * what a missing record means.
 */
export async function readPromptPreviewRecords(response, projections, onRecord) {
    const requested = new Set(projections);
    const reader = response.body?.getReader?.();
    if (!reader) throw new PromptPreviewStreamError("The prompt preview stream has no body.");
    const decoder = new TextDecoder("utf-8");
    let buffered = "";
    const drain = (final) => {
        let newline = buffered.indexOf("\n");
        while (newline >= 0) {
            const line = buffered.slice(0, newline).trim();
            buffered = buffered.slice(newline + 1);
            if (line) onRecord(parseRecord(line, requested));
            newline = buffered.indexOf("\n");
        }
        if (final && buffered.trim()) {
            // The server terminates every record; an unterminated tail is a
            // record cut off in transit.
            throw new PromptPreviewStreamError("The prompt preview stream ended mid-record.");
        }
    };
    for (;;) {
        const { value, done } = await reader.read();
        if (done) {
            buffered += decoder.decode();
            drain(true);
            return;
        }
        buffered += decoder.decode(value, { stream: true });
        drain(false);
    }
}
