// Leaf module (no DOM, no imports) deciding how playback media loads:
// direct streaming `src = /view?...` (requires HTTP Range support for seeking)
// vs whole-file blob URLs. Shared by the fullscreen viewport surface, the
// dormant preview, and the gallery overlay; each surface resolves its own
// effective mode explicitly — modes are never inherited across surfaces.

const STREAMING_MODES = new Set(["auto", "direct", "blob"]);

// One probe per page session: the canvas page and the persistent tab are
// separate module instances, but every media URL hits the same backend, so a
// single cached verdict is correct. Probe URLs are caller-supplied and must
// already be built via api.apiURL() — this leaf knows nothing about /view.
let rangeProbePromise = null;

export async function probeRangeSupport(url) {
    if (!url) return false;
    if (!rangeProbePromise) {
        rangeProbePromise = fetch(url, {
            headers: { Range: "bytes=0-0" },
            // The HTTP cache can answer 200 for a previously downloaded file
            // even when the server honors Range; bypass it for the probe.
            cache: "no-store",
        })
            .then((response) => {
                // Drain/cancel the 1-byte body so the connection is released.
                try { response.body?.cancel?.(); } catch (_) { /* ignore */ }
                return response.status === 206;
            })
            .catch(() => false);
    }
    return rangeProbePromise;
}

// settingValue: editor_settings playback.streamingMode ("auto"|"direct"|"blob").
// probeUrlProvider: () => string|null — a real media URL for the auto probe.
// Returns "direct" or "blob". When auto cannot probe yet (no media URL), this
// call returns "blob" WITHOUT caching so a later call with a real URL probes.
export async function resolveEffectiveStreamingMode(settingValue, probeUrlProvider) {
    const mode = STREAMING_MODES.has(settingValue) ? settingValue : "auto";
    if (mode === "direct") return "direct";
    if (mode === "blob") return "blob";
    let probeUrl = null;
    try {
        probeUrl = typeof probeUrlProvider === "function" ? probeUrlProvider() : null;
    } catch (_) {
        probeUrl = null;
    }
    if (!probeUrl) return "blob";
    return (await probeRangeSupport(probeUrl)) ? "direct" : "blob";
}
