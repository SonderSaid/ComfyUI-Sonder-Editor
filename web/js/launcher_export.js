// Browser download lifetime for the Launcher's curated API export.
export function exportFilename(name) {
    const stem = String(name || "Workflow").replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, "_")
        .replace(/[. ]+$/g, "").slice(0, 120) || "Workflow";
    return (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(stem) ? "_" : "") + stem + ".json";
}
export async function downloadCuratedWorkflow(template, apiURL = path => path) {
    const response = await fetch(apiURL(`/sonder-editor/sdk/templates/${encodeURIComponent(template.template_id)}/export`));
    if (!response.ok) {
        let payload; try { payload = await response.json(); } catch { /* HTTP fallback */ }
        throw new Error(payload?.error || `Export failed (${response.status}).`);
    }
    const filename = exportFilename(template.name), url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    try { anchor.href = url; anchor.download = filename; anchor.hidden = true; document.body.append(anchor); anchor.click(); }
    finally { anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    return filename;
}
