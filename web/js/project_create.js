// Creating a Sonder project the way the editor node's "+ Create New" does: the
// browser's project defaults (frame-constraint template, channel template,
// first scene length) applied to a name and a size. Shared by the canvas
// (extension.js) and the Sonder App Launcher, so the two cannot drift.

import {
    DEFAULT_EDITOR_SETTINGS,
    getEditorSettings,
    getPromptChannelTemplateById,
    getTemplateById,
    getTemplateFpsValues,
    resolveFrameConstraintForTemplate,
    snapFpsToAllowed,
} from "./editor_settings.js";
import { projectTemplateValue } from "./prompt_channel_templates.js";

export function snapFpsToTemplate(fps, template) {
    const numeric = Math.max(0, Number(fps) || 0);
    const values = getTemplateFpsValues(template);
    if (template?.id === "free" || !values.length) {
        return numeric;
    }
    return Number(snapFpsToAllowed(numeric, values).toFixed(3));
}

// The project key everywhere a project is named in a URL, the session
// registry and the editor's `project` choice.
export function projectFolderName(project) {
    return String(project?.path || "").split(/[/\\]/).pop() || "";
}

function defaultApiURL(path) {
    return globalThis.window?.comfyAPI?.api?.api?.apiURL?.(path) || path;
}

async function errorMessage(resp, fallback) {
    try {
        const data = await resp.json();
        if (data?.error) return data.error;
    } catch (_err) {}
    return fallback;
}

export async function listSonderProjects({ apiURL = defaultApiURL, fetchImpl = (...args) => globalThis.fetch(...args) } = {}) {
    const resp = await fetchImpl(apiURL("/sonder-editor/projects"));
    if (!resp.ok) {
        throw new Error(`Failed to list projects: ${resp.status}`);
    }
    const data = await resp.json();
    return data.projects || [];
}

// An error after the project exists carries the refreshed list, so a caller
// can still show the new project (the canvas dropdown always did).
function afterCreateError(message, projects, folder = "") {
    const error = new Error(message);
    error.projects = projects;
    error.folder = folder;
    return error;
}

/**
 * Create a project and its first scene.
 * Resolves to `{ created, project, folder, projects }`: the created payload,
 * its list entry, its folder name, and the refreshed project list.
 */
export async function createSonderProject({
    name,
    fps,
    width,
    height,
    settings = getEditorSettings(),
    apiURL = defaultApiURL,
    fetchImpl = (...args) => globalThis.fetch(...args),
} = {}) {
    const defaults = DEFAULT_EDITOR_SETTINGS.projectDefaults;
    const projectName = String(name || "").trim();
    if (!projectName) {
        throw new Error("Project name is required");
    }
    const defaultSceneDuration = Math.max(1, Number(settings?.projectDefaults?.newSceneDuration || defaults.newSceneDuration));
    const templateId = settings.projectDefaults.defaultTemplateId || "free";
    const template = getTemplateById(templateId, settings);
    const channelTemplate = getPromptChannelTemplateById(
        settings.projectDefaults.defaultChannelTemplateId, settings);

    const resp = await fetchImpl(apiURL("/sonder-editor/project"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            name: projectName,
            fps: snapFpsToTemplate(Number(fps || defaults.fps), template),
            width: Number(width || defaults.width),
            height: Number(height || defaults.height),
            template_id: templateId,
            frame_constraint: resolveFrameConstraintForTemplate(templateId, settings),
            prompt_channel_template: projectTemplateValue(channelTemplate),
        }),
    });
    if (!resp.ok) {
        throw new Error(await errorMessage(resp, `Project creation failed: ${resp.status}`));
    }

    const created = await resp.json();
    const projects = await listSonderProjects({ apiURL, fetchImpl });
    const project = projects.find((entry) => entry.project_id === created.project_id)
        || projects.find((entry) => entry.name === created.name);
    const folder = projectFolderName(project);
    if (!folder) {
        throw afterCreateError("Created project was not found after refreshing the project list", projects);
    }

    if ((Number(project.scene_count) || 0) <= 0) {
        const sceneResp = await fetchImpl(apiURL(`/sonder-editor/project/${encodeURIComponent(folder)}/scenes`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: "Scene 1",
                duration_frames: defaultSceneDuration,
            }),
        });
        if (!sceneResp.ok) {
            throw afterCreateError(await errorMessage(sceneResp, `Initial scene creation failed: ${sceneResp.status}`), projects, folder);
        }
    }
    return { created, project, folder, projects };
}
