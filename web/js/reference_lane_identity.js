/** Pure Reference-lane identity rules shared by the panel and node tests. */

export function preserveLaneRecipeIdentity(currentRecipe, nextRecipe, fallbackLaneId = "") {
    const laneId = String(currentRecipe?.lane_id || fallbackLaneId || "").trim();
    return {
        ...(nextRecipe || {}),
        ...(laneId ? { lane_id: laneId } : {}),
    };
}

const RECIPE_ID_POPULATIONS = {
    "sonder:minimax_h3_picture": "pictures",
    "sonder:minimax_h3_video": "videos",
    "sonder:minimax_h3_audio": "standalone_audios",
};

/** The physical population one lane recipe serves, or "" for a generic lane.
 *
 * Mirrors `minimax_h3.lane_population`, including its `recipe_id` fallback for
 * a lane whose recipe body is bare. Returns the PLURAL manifest key the Python
 * half uses; surfaces that render singular tokens translate locally.
 */
export function lanePopulation(recipeWrapper) {
    const wrapper = recipeWrapper && typeof recipeWrapper === "object" ? recipeWrapper : {};
    const recipe = wrapper.recipe && typeof wrapper.recipe === "object"
        ? wrapper.recipe : wrapper;
    const soft = recipe?.soft && typeof recipe.soft === "object" ? recipe.soft : {};
    const declared = String(soft.physical_population || "");
    if (declared) return declared;
    const recipeId = String(wrapper.recipe_id || recipe?.id || "");
    return RECIPE_ID_POPULATIONS[recipeId] || "";
}
