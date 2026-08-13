/** Pure Reference-lane identity rules shared by the panel and node tests. */

export function preserveLaneRecipeIdentity(currentRecipe, nextRecipe, fallbackLaneId = "") {
    const laneId = String(currentRecipe?.lane_id || fallbackLaneId || "").trim();
    return {
        ...(nextRecipe || {}),
        ...(laneId ? { lane_id: laneId } : {}),
    };
}
