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

/** Whether one asset may be staged on a Reference lane, by media alone.
 *
 *  Mirrors `server/routes.member_population_compatible`, which is the extracted
 *  form of the rule `_canonical_reference_member_refs` enforces. The client used
 *  to compare only `recipe.media_kind`, so an H3 Video lane (`media_kind:
 *  "image"`, population `videos`) highlighted a still image as a valid landing
 *  and the drop was then refused with `reference_media_kind_mismatch`.
 *
 *  `population` is read from `soft.physical_population` DIRECTLY, exactly as the
 *  server reads it — deliberately NOT through `lanePopulation`, whose
 *  `recipe_id` fallback would make the client stricter than the server for a
 *  bare-bodied lane carrying only `recipe_id`.
 */
export function memberPopulationCompatible(population, mediaKind, assetType, { hasAudio = false } = {}) {
    const pop = String(population || "");
    const kind = String(mediaKind || "");
    const type = String(assetType || "");
    if (kind === "image") {
        if (pop === "pictures") return type === "image";
        if (pop === "videos") return type === "video";
        return type === "image" || type === "video";
    }
    if (kind === "video") return type === "video";
    return type === "audio" || (type === "video" && !!hasAudio);
}

/** The population one lane recipe narrows staging to, as the SERVER reads it.
 *
 *  Accepts the same wrapper-or-bare-recipe shapes `lanePopulation` does, so the
 *  two near-identical names cannot be told apart by input tolerance — the only
 *  difference between them is the deliberate one: no `recipe_id` fallback, which
 *  would make the browser stricter than the staging route.
 */
export function laneStagingPopulation(recipeWrapper) {
    const wrapper = recipeWrapper && typeof recipeWrapper === "object" ? recipeWrapper : {};
    const recipe = wrapper.recipe && typeof wrapper.recipe === "object"
        ? wrapper.recipe : wrapper;
    const soft = recipe?.soft && typeof recipe.soft === "object" ? recipe.soft : {};
    return String(soft.physical_population || "");
}

/** A lane that has never been configured adopts the kind of the first drop. */
export function isUnconfiguredReferenceLaneRecipe(recipe) {
    const value = recipe || {};
    return (value.media_kind || "image") === "image"
        && !String(value.recipe_id || "")
        && !Object.keys(value.recipe || {}).length;
}

/** One verdict for a Reference drag over one lane, shared by hover and drop.
 *
 *  Hover and drop must run the SAME rule or the highlight promises a landing
 *  the drop refuses. Extending that shared predicate — rather than adding a
 *  parallel append path — is what keeps that true now that a drop has two
 *  possible landings.
 *
 *  `allowAppend` is NOT a preference: append is reachable only from an explicit
 *  pointer position over a bar. `addToTimeline` places at the playhead with no
 *  coordinates, and if the shared predicate simply started accepting occupied
 *  frames, that Library button would silently stop creating an item and start
 *  appending to whatever sits under the playhead.
 *
 *  Returns `{ verdict: "create" | "append" | "reject", reason, itemId }`.
 */
export function resolveReferenceDropVerdict(lane, {
    mediaKind = "",
    frame = 0,
    sceneDuration = 1,
    members = [],
    allowAppend = false,
} = {}) {
    const reject = (reason) => ({ verdict: "reject", reason, itemId: "" });
    if (!lane || !mediaKind) return reject("no_target");
    if (lane.collapsed) return reject("collapsed");
    if (lane.locked) return reject("locked");

    const items = Array.isArray(lane.items) ? lane.items : [];
    const duration = Math.max(1, Number(sceneDuration) || 1);
    const at = Math.min(duration - 1, Math.max(0, Math.round(Number(frame) || 0)));
    const recipe = lane.recipe || {};
    const kindMatches = String(recipe.media_kind || "image") === mediaKind;
    const adopts = !kindMatches && !items.length
        && isUnconfiguredReferenceLaneRecipe(recipe);
    if (!kindMatches && !adopts) return reject("media_kind");

    // An adopting lane is retyped to the default recipe, which declares no
    // population, so narrowing applies only to a lane that already has one.
    const population = adopts ? "" : laneStagingPopulation(recipe);
    for (const member of members || []) {
        if (!memberPopulationCompatible(population, mediaKind,
            member?.assetType, { hasAudio: member?.hasAudio === true })) {
            return reject("population");
        }
    }

    const covering = items.find((item) => {
        const end = item?.end_frame === -1 ? duration : Number(item?.end_frame || 0);
        return Number(item?.start_frame || 0) <= at && end > at;
    }) || null;
    if (!covering) return { verdict: "create", reason: "", itemId: "" };
    if (!allowAppend) return reject("occupied");

    const staged = new Set((covering.members || [])
        .map((value) => String(value?.member_id || "")));
    for (const member of members || []) {
        if (staged.has(String(member?.member_id || ""))) {
            return reject("duplicate_member");
        }
    }
    return {
        verdict: "append",
        reason: "",
        itemId: String(covering.reference_item_id || ""),
    };
}
