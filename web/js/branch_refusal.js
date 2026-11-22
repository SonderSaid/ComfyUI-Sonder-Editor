// The Views chip uses the same line budget and overflow marker as the App's
// validation_refusal.py. The canvas can describe its own submitted inputs.
const MAX_PROBLEMS = 6;
const MAX_LINE_CHARS = 300;
const UNSAFE_CHARS = /[\p{Cc}\p{Cf}]/gu;
const SAFE_CHOICE = /^[A-Za-z0-9_.+-]{1,120}$/;

function plainText(value, limit) {
    if (typeof value !== "string") return "";
    const chars = [...value.replace(/\s+/gu, " ").replace(UNSAFE_CHARS, "").trim()];
    return chars.length > limit ? chars.slice(0, limit - 1).join("").trimEnd() + "…" : chars.join("");
}

export function readBranchProblems(raw) {
    if (!Array.isArray(raw) || raw.length > MAX_PROBLEMS + 1) return [];
    return raw.filter(line => typeof line === "string" && line.length <= MAX_LINE_CHARS * 2)
        .map(line => plainText(line, MAX_LINE_CHARS)).filter(Boolean);
}

function describeReason(reason, node, classType, title) {
    const kind = reason?.type;
    if (typeof kind !== "string" || !kind) return "";
    const extra = reason.extra_info;
    const inputKey = extra?.input_name;
    const input = plainText(inputKey, 120);
    const name = input ? title + " · " + input : title;
    if (kind.startsWith("exception")) return "ComfyUI couldn't check “" + title + "”.";
    if (kind === "custom_validation_failed") return "“" + title + "” failed its own check.";
    if (kind === "required_input_missing" && input) return "“" + title + "” is missing its " + input + " input.";
    const value = input && node?.inputs && Object.hasOwn(node.inputs, inputKey) ? node.inputs[inputKey] : undefined;
    if (kind === "value_not_in_list") {
        const displayable = (classType === "BasicScheduler" && inputKey === "scheduler")
            || (classType === "KSamplerSelect" && inputKey === "sampler_name");
        if (displayable && typeof value === "string" && SAFE_CHOICE.test(value)) {
            return "“" + name + "”: “" + value + "” isn't one of its choices on this ComfyUI.";
        }
        return "“" + name + "” uses a choice that's unavailable on this ComfyUI.";
    }
    if (kind === "value_smaller_than_min" || kind === "value_bigger_than_max") {
        const smaller = kind === "value_smaller_than_min";
        const direction = smaller ? "below its minimum" : "above its maximum";
        const bound = extra?.input_config?.[1]?.[smaller ? "min" : "max"];
        if (Number.isFinite(value) && Number.isFinite(bound)) {
            return "“" + name + "”: " + value + " is " + direction + ", " + bound + ".";
        }
        return "“" + name + "” is " + direction + ".";
    }
    return "ComfyUI rejected “" + name + "”.";
}

export function describeBranchRefusal(nodeErrors, prompt) {
    if (!nodeErrors || typeof nodeErrors !== "object" || Array.isArray(nodeErrors)) return [];
    const distinct = new Set();
    for (const [id, entry] of Object.entries(nodeErrors)) {
        if (!Array.isArray(entry?.errors)) continue;
        const node = prompt && Object.hasOwn(prompt, id) ? prompt[id] : null;
        const classType = node?.class_type || entry.class_type;
        const title = plainText(node?._meta?.title, 120) || plainText(classType, 120) || "A node";
        for (const reason of entry.errors) {
            const line = plainText(describeReason(reason, node, classType, title), MAX_LINE_CHARS);
            if (line) distinct.add(line);
        }
    }
    const lines = [...distinct].slice(0, MAX_PROBLEMS);
    if (distinct.size > MAX_PROBLEMS) {
        lines.push("+" + (distinct.size - MAX_PROBLEMS) + " more. The ComfyUI console lists every one.");
    }
    return lines;
}
