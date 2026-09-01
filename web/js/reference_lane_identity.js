/** Pure Reference-lane identity rules shared by the panel and node tests. */

export function preserveLaneRecipeIdentity(currentRecipe, nextRecipe, fallbackLaneId = "") {
    const laneId = String(currentRecipe?.lane_id || fallbackLaneId || "").trim();
    return {
        ...(nextRecipe || {}),
        ...(laneId ? { lane_id: laneId } : {}),
    };
}

/** Advisories whose truth depends only on the materialized recipe shape. */
export function referenceConfigurationAdvisories(hard, soft, stagedCount = 0) {
    const hardValues = hard && typeof hard === "object" ? hard : {};
    const softValues = soft && typeof soft === "object" ? soft : {};
    const assembly = String(hardValues.assembly || "batch");
    const layout = String(hardValues.layout || "grid");
    const count = Math.max(0, Number(stagedCount) || 0);
    const advisories = [];

    if (softValues.silent_single_input && count > 1) {
        advisories.push({ voice: "suggestion", text: "This mechanism reads one input. Confirm the staged members are intentionally combined or that only the intended Bridge output is connected." });
    }
    if (softValues.crowded_sheet_padding && assembly === "sheet" && layout === "strip" && count > 2) {
        advisories.push({ voice: "suggestion", text: "A vertical strip pads heavily as members are added. Use Edit as custom to choose a different sheet layout." });
    }
    if (softValues.task_from_connectivity && assembly === "slots") {
        advisories.push({ voice: "suggestion", text: "This model infers its task from connected inputs. Set the Bridge to unused_slots: nothing so placeholder black images do not select a task." });
    }
    if (softValues.primary_model_position === "last" && assembly === "batch") {
        advisories.push({ voice: "suggestion", text: "The model moves the first staged member to the end of the batch, so member 1 arrives last." });
    }
    return advisories;
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
