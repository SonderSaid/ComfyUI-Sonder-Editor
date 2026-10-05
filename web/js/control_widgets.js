// Shared native widget lookup. Pure on import, including standalone hosts.
export const CONTROL_SEED_MODES = new Set(["fixed", "increment", "decrement", "randomize"]);
export const findControlWidget = (node, name) => (node?.widgets || []).find(w => w?.name === name) || null;
export const seedModeWidget = (seed) => Array.isArray(seed?.linkedWidgets)
    ? seed.linkedWidgets.find(w => w?.name === "control_after_generate" || CONTROL_SEED_MODES.has(w?.value))
    : undefined;
