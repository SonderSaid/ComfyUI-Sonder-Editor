// Which Mode choices can't run on this install, in words (Sonder App only: on the
// canvas, ComfyUI's Run validates the graph). Pure text over the server's answer
// (`sdk_templates.mode_availability`). Guidance only: Generate stays the refusal,
// and none of this enters the Controls view or decides which choice is selected.
import { modelMessage } from "./launcher_view.js";
import { missingNodePacks } from "./node_pack_mappings.js";

// A tip or notice stops being read past this many.
export const MAX_NEEDS_SHOWN = 4;

export function needsOf(overlay, presetId) {
    return overlay?.choices?.[presetId] || null;
}

function nodeLines(classes, packs) {
    const lines = [];
    for (const { pack, classes: names } of missingNodePacks(classes, packs)) {
        if (pack) lines.push(`Needs the node pack ${pack} (${names.join(", ")}).`);
        else for (const name of names) lines.push(`Needs the node ${name}.`);
    }
    return lines;
}

// A file, or a LoRA row naming no file: that one is a whole sentence already.
function modelLine(item) {
    const text = modelMessage(item);
    return text.startsWith("A LoRA row") ? text : `Needs ${text}`;
}

export function needsLines(needs, packs) {
    if (!needs) return [];
    return [...nodeLines(needs.missing_nodes || [], packs), ...(needs.missing_models || []).map(modelLine)];
}

function capped(lines) {
    if (lines.length <= MAX_NEEDS_SHOWN) return lines;
    return [...lines.slice(0, MAX_NEEDS_SHOWN), `and ${lines.length - MAX_NEEDS_SHOWN} more`];
}

// A greyed choice's tip and notice.
export function needsText(needs, packs) {
    return capped(needsLines(needs, packs)).join("\n");
}

const counted = (count, noun) => (count === 1 ? `1 ${noun}` : `${count} ${noun}s`);

// The line above Modes: what the current run lacks, or null when it can run. One
// sentence, since on a bare install the list is long and its paths longer: the
// list opens on request (`needs`). A problem is a sentence already, so it shows.
export function runSummary(overlay, packs, { hasModes = true } = {}) {
    const run = overlay?.run;
    if (!run) return null;
    const needs = needsLines(run, packs), problems = run.problems || [];
    if (!needs.length && !problems.length) return null;
    const lead = hasModes ? "This workflow can't run with the current Modes" : "This workflow can't run yet";
    const files = run.missing_models?.length || 0, nodes = run.missing_nodes?.length || 0;
    const what = [files ? counted(files, "file") : "", nodes ? counted(nodes, "node") : ""].filter(Boolean).join(" and ");
    return { title: what ? `${lead}: ${what} missing.` : `${lead}.`, needs, problems };
}

// Whether an answer names a node, so the pack names are worth fetching.
export function namesANode(overlay) {
    return !!overlay && [overlay.run, ...Object.values(overlay.choices || {})].some(needs => needs?.missing_nodes?.length);
}

export function hasEntries(overlay) {
    return !!overlay && (Object.keys(overlay.choices || {}).length > 0 || !!runSummary(overlay));
}
