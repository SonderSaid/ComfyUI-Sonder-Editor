// Public docs the Sonder App links to, and when. Pure: no DOM, no imports.
// tests/test_docs_links.py maps each URL to its file in this repository.
export const SETUP_GUIDE_URL = "https://github.com/SonderSaid/ComfyUI-Sonder-Editor/blob/main/docs/sonder-app-setup.md";

// A missing model item names a file to install. A native LoRA row reading "None"
// names none: it's fixed in Controls, not by installing anything.
export function namesAFile(item) {
    const name = String(item?.file || "").split(/[\\/]/).pop();
    return /\.[A-Za-z0-9]+$/.test(name);
}

// Whether these needs ({missing_nodes, missing_models}) ask for an install, so the
// setup guide helps. A problem alone (a switched-off Save) never does.
export function asksForSetup(needs) {
    return !!needs && ((needs.missing_nodes?.length || 0) > 0 || (needs.missing_models || []).some(namesAFile));
}
