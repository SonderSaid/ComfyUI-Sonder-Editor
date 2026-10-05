// Sonder App Launcher (`/sonder-editor/app`): pick a workflow (an API export
// with a Sonder Editor) and a project, then open the editor on them in this
// tab. SDK mode's own entry surface; the editor it opens is the shared one.
//
// The session secret goes to sessionStorage, never into the URL. Coming back
// from the editor (Exit), the Launcher says how many takes are still
// generating; reopening the same workflow and project reattaches.

import { installComfyApiShim } from "./comfy_api_shim.js";
import {
    EDITOR_COLORS as COLORS,
    FONT,
    RADIUS,
    SPACE,
    THEME,
    TYPE,
    applyNativeControlTheme,
    chromeButtonCss,
    chromeInputCss,
    injectSonderFontFaces,
    setButtonDisabled,
    setButtonVariant,
    statusPillCss,
} from "./editor_theme.js";
import { mountToastStack } from "./editor_toast_stack.js";
import { configureNotifications, notifyError, notifySuccess } from "./editor_notifications.js";
import { getEditorSettings, notificationCoreConfig, subscribeEditorSettings } from "./editor_settings.js";
import { createSonderProject, listSonderProjects, projectFolderName } from "./project_create.js";
import { SDK_SECRET_HEADER, isUnfinishedRun, readSdkSecret, storeSdkSecret, takeSdkExitFlag } from "./sdk_host.js";
import { LAUNCHER_WINDOW_NAME } from "./sdk_setup_client.js";

// ComfyUI loads every script under web/ as an extension on its own page, so
// this module does nothing on import: it starts only on the Launcher page,
// which marks its <body> (sdk_app.html).
let api = null;
let root = null;
let createNameInput = null;

const BUTTON = Object.freeze({
    primary: { variant: "primary", padding: "5px 12px", fontSize: `${TYPE.t12}px` },
    secondary: { variant: "secondary", padding: "4px 10px", fontSize: `${TYPE.t11}px` },
    tertiary: { variant: "tertiary", padding: "4px 8px", fontSize: `${TYPE.t11}px` },
    danger: { variant: "danger", padding: "4px 10px", fontSize: `${TYPE.t11}px` },
});

const state = {
    status: null,
    templates: [],
    projects: [],
    selectedTemplateId: "",
    selectedProject: "",
    renamingId: "",
    confirmDeleteId: "",
    importProblems: null,
    renameDraft: "",
    exitNotice: null,
    opening: false,
    creating: false,
};

// ── Small DOM helpers ─────────────────────────────────────────────────────

function el(tag, cssText = "", text = "") {
    const node = document.createElement(tag);
    if (cssText) node.style.cssText = cssText;
    if (text) node.textContent = text;
    return node;
}

function button(label, title, kind = "secondary", onClick = null) {
    const node = el("button");
    node.type = "button";
    node.textContent = label;
    node.title = title || "";
    setButtonVariant(node, BUTTON[kind]?.variant || kind, BUTTON[kind] || BUTTON.secondary);
    if (onClick) node.addEventListener("click", onClick);
    return node;
}

function statusDot(stateName, label) {
    const pill = el("span", `${statusPillCss({ state: stateName, padding: "2px 8px" })} font-size:${TYPE.t10}px;`);
    const dot = el("span", "width:6px;height:6px;border-radius:999px;background:var(--sonder-status-color);flex:0 0 auto;");
    pill.append(dot, document.createTextNode(label));
    return pill;
}

const CARD_CSS = `
    background: ${THEME.bg1};
    border: 1px solid ${THEME.line2};
    border-radius: ${RADIUS.r3}px;
    padding: ${SPACE.s4}px;
    display: flex;
    flex-direction: column;
    gap: ${SPACE.s3}px;
    min-width: 0;
`;
const TITLE_CSS = `font-size:${TYPE.t14}px; font-weight:${TYPE.fwBold}; color:${THEME.fg0};`;
const HELP_CSS = `font-size:${TYPE.t11}px; color:${THEME.fg2}; line-height:1.45;`;

function cardHeader(title, help, ...actions) {
    const header = el("div", "display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap;");
    const text = el("div", "display:flex; flex-direction:column; gap:3px; min-width:0; flex:1 1 240px;");
    text.append(el("div", TITLE_CSS, title));
    if (help) text.append(el("div", HELP_CSS, help));
    const actionWrap = el("div", "display:flex; gap:6px; flex-wrap:wrap; flex:0 0 auto;");
    actionWrap.append(...actions.filter(Boolean));
    header.append(text, actionWrap);
    return header;
}

function rowCss(selected) {
    return `
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        border-radius: ${RADIUS.r2}px;
        border: 1px solid ${selected ? COLORS.accentBorder : THEME.line1};
        background: ${selected ? COLORS.accentSoft : THEME.bg2};
        cursor: pointer;
        min-width: 0;
        text-align: left;
        width: 100%;
        box-sizing: border-box;
        font-family: ${FONT.sans};
        color: ${THEME.fg0};
    `;
}

function list(items) {
    const wrap = el("div", "display:flex; flex-direction:column; gap:6px; min-width:0;");
    wrap.append(...items);
    return wrap;
}

function issueList(lines, color) {
    const ul = el("ul", `margin:0; padding-left:18px; display:flex; flex-direction:column; gap:3px; font-size:${TYPE.t11}px; color:${color}; line-height:1.45;`);
    for (const line of lines) ul.append(el("li", "", line));
    return ul;
}

// ── Server calls ──────────────────────────────────────────────────────────

async function call(path, init = {}) {
    try {
        const resp = await fetch(api.apiURL(path), init);
        let payload = {};
        try {
            payload = await resp.json();
        } catch (_err) {
            payload = {};
        }
        return { ok: resp.ok, status: resp.status, payload: payload && typeof payload === "object" ? payload : {} };
    } catch (err) {
        return { ok: false, status: 0, payload: { error: "Couldn't reach ComfyUI.", detail: String(err?.message || err) } };
    }
}

async function loadStatus() {
    const result = await call("/sonder-editor/sdk/status");
    state.status = result.ok ? result.payload : { error: result.payload.error || `Status unavailable (${result.status}).` };
}

async function loadTemplates() {
    const result = await call("/sonder-editor/sdk/templates");
    if (!result.ok) {
        notifyError("Couldn't list the workflows.", { detail: result.payload.error || null, source: "launcher-templates" });
        return;
    }
    state.templates = Array.isArray(result.payload.templates) ? result.payload.templates : [];
    if (state.selectedTemplateId && !state.templates.some((t) => t.template_id === state.selectedTemplateId)) {
        state.selectedTemplateId = "";
    }
}

async function loadProjects() {
    try {
        state.projects = await listSonderProjects({ apiURL: (path) => api.apiURL(path) });
    } catch (err) {
        notifyError("Couldn't list the projects.", { detail: String(err?.message || err), source: "launcher-projects" });
        return;
    }
    if (state.selectedProject && !state.projects.some((p) => projectFolderName(p) === state.selectedProject)) {
        state.selectedProject = "";
    }
}

function selectedTemplate() {
    return state.templates.find((t) => t.template_id === state.selectedTemplateId) || null;
}

function baseName(fileName) {
    return String(fileName || "").replace(/\.[^.]+$/, "");
}

function pickFile(onFile) {
    const input = el("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.style.display = "none";
    input.addEventListener("change", () => {
        const file = input.files?.[0];
        input.remove();
        if (file) void onFile(file);
    });
    document.body.appendChild(input);
    input.click();
}

function reportTemplateRefusal(result, verb) {
    const validation = result.payload.validation;
    state.importProblems = validation?.errors?.length ? validation.errors : [result.payload.error || `${verb} failed (${result.status}).`];
    notifyError(`This workflow can't be ${verb === "Import" ? "imported" : "used"}.`, {
        detail: state.importProblems.join("\n"),
        source: "launcher-import",
    });
}

async function importWorkflow(file) {
    const query = new URLSearchParams({ source_name: file.name, name: baseName(file.name) });
    const result = await call(`/sonder-editor/sdk/templates?${query}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: await file.arrayBuffer(),
    });
    if (!result.ok) {
        reportTemplateRefusal(result, "Import");
        render();
        return;
    }
    state.importProblems = null;
    await loadTemplates();
    state.selectedTemplateId = result.payload.template?.template_id || state.selectedTemplateId;
    notifySuccess("Workflow imported", { source: "launcher-import" });
    render();
}

async function replaceWorkflow(template, file) {
    const query = new URLSearchParams({ source_name: file.name });
    const result = await call(`/sonder-editor/sdk/templates/${encodeURIComponent(template.template_id)}?${query}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: await file.arrayBuffer(),
    });
    if (!result.ok) {
        reportTemplateRefusal(result, "Replace");
        render();
        return;
    }
    state.importProblems = null;
    await loadTemplates();
    notifySuccess("Workflow replaced; its Control values were kept", { source: "launcher-import" });
    render();
}

async function renameWorkflow(template, name) {
    const trimmed = String(name || "").trim();
    state.renamingId = "";
    if (!trimmed || trimmed === template.name) {
        render();
        return;
    }
    const result = await call(`/sonder-editor/sdk/templates/${encodeURIComponent(template.template_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
    });
    if (!result.ok) notifyError("Couldn't rename the workflow.", { detail: result.payload.error || null, source: "launcher-rename" });
    await loadTemplates();
    render();
}

async function deleteWorkflow(template) {
    state.confirmDeleteId = "";
    const result = await call(`/sonder-editor/sdk/templates/${encodeURIComponent(template.template_id)}`, { method: "DELETE" });
    if (!result.ok) notifyError("Couldn't delete the workflow.", { detail: result.payload.error || null, source: "launcher-delete" });
    else notifySuccess("Workflow deleted", { source: "launcher-delete" });
    await loadTemplates();
    render();
}

// Size and frame rate for a new project: the selected workflow's editor, as
// "+ Create New" uses the node's; otherwise the editor's project defaults.
function creationSize() {
    const settings = getEditorSettings();
    const defaults = selectedTemplate()?.validation?.editor_defaults || {};
    const pick = (value, fallback) => (Number(value) > 0 ? Number(value) : Number(fallback));
    return {
        fps: pick(defaults.fps, settings.projectDefaults.fps),
        width: pick(defaults.width, settings.projectDefaults.width),
        height: pick(defaults.height, settings.projectDefaults.height),
        fromWorkflow: Number(defaults.fps) > 0 || Number(defaults.width) > 0,
    };
}

async function createProject(name) {
    if (state.creating) return;
    const size = creationSize();
    state.creating = true;
    render();
    try {
        const { folder } = await createSonderProject({
            name,
            fps: size.fps,
            width: size.width,
            height: size.height,
            settings: getEditorSettings(),
            apiURL: (path) => api.apiURL(path),
        });
        await loadProjects();
        state.selectedProject = folder;
        createNameInput.value = "";
        notifySuccess("Project created", { source: "launcher-create" });
    } catch (err) {
        notifyError("Couldn't create the project.", { detail: String(err?.message || err), source: "launcher-create" });
    } finally {
        state.creating = false;
        render();
    }
}

async function openEditor() {
    const template = selectedTemplate();
    if (!template || !state.selectedProject || state.opening) return;
    state.opening = true;
    render();
    const result = await call("/sonder-editor/sdk/session/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ template_id: template.template_id, project: state.selectedProject }),
    });
    if (!result.ok || !result.payload.session_id || !result.payload.tab_url) {
        state.opening = false;
        notifyError("Couldn't open the editor.", { detail: result.payload.error || `HTTP ${result.status}`, source: "launcher-open" });
        render();
        return;
    }
    if (!storeSdkSecret(result.payload.session_id, result.payload.secret)) {
        state.opening = false;
        notifyError("Couldn't open the editor.", { detail: "This browser blocked session storage for the page.", source: "launcher-open" });
        render();
        return;
    }
    window.location.assign(api.apiURL(result.payload.tab_url));
}

// ── Back from the editor ──────────────────────────────────────────────────

let exitPollTimer = null;

// The poll replaces only the notice card: rebuilding the page every 2 s would
// blur a field being typed in and redraw buttons a pending action disabled.
function setExitNotice(notice) {
    state.exitNotice = notice;
    const current = root?.querySelector?.("[data-sonder-exit-notice]");
    const next = renderExitNotice();
    if (current && next) current.replaceWith(next);
    else if (current) current.remove();
    else if (next) render();
}

async function pollExitSession(flag) {
    clearTimeout(exitPollTimer);
    const secret = readSdkSecret(flag.session_id);
    if (!secret) {
        setExitNotice(null);
        return;
    }
    const result = await call(`/sonder-editor/sdk/session/${encodeURIComponent(flag.session_id)}/status`, {
        headers: { [SDK_SECRET_HEADER]: secret },
    });
    if (!result.ok) {
        setExitNotice(null);
        return;
    }
    const runs = Array.isArray(result.payload.runs) ? result.payload.runs : [];
    const generating = runs.filter(isUnfinishedRun).length;
    const wasGenerating = state.exitNotice?.generating > 0;
    setExitNotice(generating || wasGenerating ? { generating, project: flag.project } : null);
    if (generating) exitPollTimer = setTimeout(() => void pollExitSession(flag), 2000);
}

// ── Rendering ─────────────────────────────────────────────────────────────


function renderHeader() {
    const header = el("div", "display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;");
    header.append(
        el("div", `font-size:${TYPE.t16}px; font-weight:${TYPE.fwBold}; color:${THEME.fg0};`, "Sonder App"),
        el("span", `font-size:${TYPE.t10}px; letter-spacing:0.08em; text-transform:uppercase; color:${THEME.accentHi}; border:1px solid ${THEME.accentLo}; border-radius:${RADIUS.r2}px; padding:1px 6px;`, "Experimental"),
    );
    const sub = el("div", HELP_CSS, "Choose a workflow and a project, then open the Sonder Editor on them. Generate runs the workflow on this ComfyUI and the takes land on the timeline.");
    const wrap = el("div", "display:flex; flex-direction:column; gap:6px;");
    wrap.append(header, sub);
    return wrap;
}

function packageRow(label, info) {
    const row = el("div", "display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;");
    row.append(el("span", `font-size:${TYPE.t12}px; color:${THEME.fg1};`, label));
    if (!info?.installed) row.append(statusDot("pending", "Not installed"));
    else if (!info.supported) row.append(statusDot("failed", `Unsupported version ${info.version}`));
    else row.append(statusDot("running", info.version));
    return row;
}

function renderEnvironment() {
    const card = el("section", CARD_CSS);
    const refresh = button("Refresh", "Check again", "tertiary", async () => {
        await loadStatus();
        render();
    });
    card.append(cardHeader("Environment", "", refresh));
    const status = state.status;
    if (!status || status.error) {
        card.append(el("div", `font-size:${TYPE.t12}px; color:${COLORS.dangerText};`, status?.error || "Checking…"));
        return card;
    }
    const install = status.install || {};
    const proxy = status.proxy || {};
    card.append(packageRow("comfy-sdk", install.sdk), packageRow("comfy-api-proxy", install.proxy));
    const connection = el("div", "display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;");
    connection.append(el("span", `font-size:${TYPE.t12}px; color:${THEME.fg1};`, "Connection"));
    if (proxy.running) {
        connection.append(statusDot("running", proxy.managed ? `Running at ${proxy.url}` : `Using ${proxy.url}`));
    } else if (proxy.error) {
        connection.append(statusDot("failed", "Not running"));
    } else {
        connection.append(statusDot("idle", "Starts when you open a workflow"));
    }
    card.append(connection);
    if (proxy.upstream) {
        card.append(el("div", HELP_CSS, `ComfyUI: ${proxy.upstream}`));
    }
    if (proxy.error) {
        card.append(el("div", `font-size:${TYPE.t11}px; color:${COLORS.warningText}; line-height:1.45;`, proxy.error));
    }
    if (!install.ready) {
        card.append(el("div", `font-size:${TYPE.t11}px; color:${COLORS.warningText}; line-height:1.45;`,
            "Generate needs both packages. Install them from the editor: Settings → Sonder App. You can still open and edit a project."));
    }
    return card;
}

function renderExitNotice() {
    const notice = state.exitNotice;
    if (!notice) return null;
    const card = el("section", `${CARD_CSS} border-color:${COLORS.accentBorder}; background:${COLORS.accentSoft};`);
    card.dataset.sonderExitNotice = "true";
    const text = notice.generating > 0
        ? `${notice.generating} ${notice.generating === 1 ? "take is" : "takes are"} still generating; they'll land on the timeline of ${notice.project}.`
        : `The takes have finished in ${notice.project}.`;
    card.append(el("div", `font-size:${TYPE.t12}px; color:${THEME.fg0}; line-height:1.45;`, text));
    return card;
}

function templateBadge(template) {
    const validation = template.validation || {};
    if (validation.blocking?.length) return statusDot("failed", "Can't generate yet");
    if (validation.warnings?.length) {
        const count = validation.warnings.length;
        return statusDot("pending", `${count} ${count === 1 ? "note" : "notes"}`);
    }
    return statusDot("running", "Ready");
}

// Template times are Unix seconds (sdk_templates); 0 means never.
function formatDate(seconds) {
    if (!(Number(seconds) > 0)) return "";
    const date = new Date(Number(seconds) * 1000);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function renderTemplateDetails(template) {
    const details = el("div", `display:flex; flex-direction:column; gap:8px; padding:4px 10px 6px; min-width:0;`);
    const validation = template.validation || {};
    if (validation.blocking?.length) {
        details.append(el("div", `font-size:${TYPE.t11}px; color:${THEME.fg1};`, "Generate refuses until the workflow is fixed and replaced:"),
            issueList(validation.blocking, COLORS.dangerText));
    }
    if (validation.warnings?.length) details.append(issueList(validation.warnings, COLORS.warningText));
    const controls = Array.isArray(validation.controls) ? validation.controls : [];
    details.append(el("div", HELP_CSS, controls.length
        ? `Controls: ${controls.map((control) => control.label).join(", ")}`
        : "No Sonder Controls in this workflow."));

    const actions = el("div", "display:flex; gap:6px; flex-wrap:wrap;");
    if (state.renamingId === template.template_id) {
        const input = el("input", `${chromeInputCss({ padding: "4px 8px", fontSize: `${TYPE.t12}px` })} flex:1 1 200px; min-width:0;`);
        input.type = "text";
        input.value = state.renameDraft;
        input.maxLength = 120;
        input.addEventListener("input", () => { state.renameDraft = input.value; });
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") void renameWorkflow(template, input.value);
            else if (event.key === "Escape") {
                state.renamingId = "";
                render();
            }
        });
        actions.append(input,
            button("Save", "Save the new name", "secondary", () => void renameWorkflow(template, input.value)),
            button("Cancel", "Keep the current name", "tertiary", () => {
                state.renamingId = "";
                render();
            }));
        queueMicrotask(() => input.select());
    } else {
        actions.append(
            button("Rename", "Rename this workflow", "secondary", () => {
                state.renamingId = template.template_id;
                state.renameDraft = template.name || "";
                state.confirmDeleteId = "";
                render();
            }),
            button("Replace…", "Import a new export of this workflow; its Control values are kept", "secondary", () => {
                pickFile((file) => replaceWorkflow(template, file));
            }),
        );
        const confirming = state.confirmDeleteId === template.template_id;
        const del = button(confirming ? "Delete? Click again" : "Delete",
            confirming ? "Delete this workflow and its saved Control values" : "Delete this workflow",
            confirming ? "danger" : "tertiary",
            () => {
                if (state.confirmDeleteId === template.template_id) {
                    void deleteWorkflow(template);
                    return;
                }
                state.confirmDeleteId = template.template_id;
                render();
                setTimeout(() => {
                    if (state.confirmDeleteId === template.template_id) {
                        state.confirmDeleteId = "";
                        render();
                    }
                }, 4000);
            });
        actions.append(del);
    }
    details.append(actions);
    return details;
}

function renderWorkflows() {
    const card = el("section", CARD_CSS);
    const importBtn = button("Import…", "Import an API workflow export", "primary", () => pickFile(importWorkflow));
    card.append(cardHeader(
        "Workflows",
        "API exports that contain a Sonder Editor (in ComfyUI: Workflow → Export (API)). Add a Sonder Control on a seed for new takes on every Generate.",
        importBtn,
    ));
    if (state.importProblems?.length) {
        const box = el("div", `border:1px solid ${COLORS.dangerBorder}; background:${COLORS.dangerSoft}; border-radius:${RADIUS.r2}px; padding:8px 10px; display:flex; flex-direction:column; gap:4px;`);
        box.append(el("div", `font-size:${TYPE.t11}px; color:${COLORS.dangerText}; font-weight:${TYPE.fwMed};`, "That file wasn't imported:"),
            issueList(state.importProblems, COLORS.dangerText));
        card.append(box);
    }
    if (!state.templates.length) {
        card.append(el("div", HELP_CSS, "No workflows yet. Export one from ComfyUI, then import it here."));
        return card;
    }
    card.append(list(state.templates.map((template) => {
        const selected = template.template_id === state.selectedTemplateId;
        const wrap = el("div", "display:flex; flex-direction:column; gap:2px; min-width:0;");
        const row = el("button", rowCss(selected));
        row.type = "button";
        row.setAttribute("aria-pressed", selected ? "true" : "false");
        const text = el("div", "display:flex; flex-direction:column; gap:2px; min-width:0; flex:1 1 auto;");
        text.append(el("div", `font-size:${TYPE.t12}px; font-weight:${TYPE.fwMed}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`, template.name || "Workflow"));
        const meta = [template.source_name, formatDate(template.replaced_at || template.imported_at)].filter(Boolean).join(" · ");
        if (meta) text.append(el("div", `font-size:${TYPE.t10}px; color:${THEME.fg2}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`, meta));
        row.append(text, templateBadge(template));
        row.addEventListener("click", () => {
            if (state.selectedTemplateId !== template.template_id) {
                state.selectedTemplateId = template.template_id;
                state.renamingId = "";
                state.confirmDeleteId = "";
                render();
            }
        });
        wrap.append(row);
        if (selected) wrap.append(renderTemplateDetails(template));
        return wrap;
    })));
    return card;
}

function renderProjects() {
    const card = el("section", CARD_CSS);
    card.append(cardHeader("Projects", "Takes land on the selected project's timeline."));

    const size = creationSize();
    const createRow = el("div", "display:flex; gap:6px; flex-wrap:wrap; align-items:center;");
    const createBtn = button(state.creating ? "Creating…" : "Create", "Create a project with this name", "secondary");
    setButtonDisabled(createBtn, state.creating);
    const submit = () => {
        const name = createNameInput.value.trim();
        if (!name) {
            createNameInput.focus();
            return;
        }
        void createProject(name);
    };
    createBtn.addEventListener("click", submit);
    createNameInput.onkeydown = (event) => {
        if (event.key === "Enter") submit();
    };
    createRow.append(createNameInput, createBtn);
    card.append(createRow);
    card.append(el("div", HELP_CSS, `New projects are ${size.width}×${size.height} at ${size.fps} fps, ${size.fromWorkflow ? "from the selected workflow's editor" : "from your editor defaults"}.`));

    if (!state.projects.length) {
        card.append(el("div", HELP_CSS, "No projects yet. Create one above."));
        return card;
    }
    const projectList = list(state.projects.map((project) => {
        const folder = projectFolderName(project);
        const selected = folder === state.selectedProject;
        const row = el("button", rowCss(selected));
        row.type = "button";
        row.setAttribute("aria-pressed", selected ? "true" : "false");
        const text = el("div", "display:flex; flex-direction:column; gap:2px; min-width:0; flex:1 1 auto;");
        text.append(el("div", `font-size:${TYPE.t12}px; font-weight:${TYPE.fwMed}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`, project.name || folder));
        const resolution = Array.isArray(project.resolution) ? `${project.resolution[0]}×${project.resolution[1]}` : "";
        const scenes = Number(project.scene_count) || 0;
        const meta = [folder !== project.name ? folder : "", resolution, project.fps ? `${project.fps} fps` : "", `${scenes} ${scenes === 1 ? "scene" : "scenes"}`]
            .filter(Boolean).join(" · ");
        text.append(el("div", `font-size:${TYPE.t10}px; color:${THEME.fg2}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`, meta));
        row.append(text);
        row.addEventListener("click", () => {
            state.selectedProject = folder;
            render();
        });
        return row;
    }));
    // A long project list scrolls inside the card, so Workflows stays in reach.
    projectList.style.maxHeight = "min(420px, 55vh)";
    projectList.style.overflowY = "auto";
    projectList.style.paddingRight = "2px";
    projectList.dataset.sonderProjectList = "true";
    card.append(projectList);
    return card;
}

function renderOpenBar() {
    const bar = el("div", `
        position: fixed;
        left: 0;
        right: 0;
        bottom: 0;
        background: ${THEME.bg1};
        border-top: 1px solid ${THEME.line2};
        padding: 10px ${SPACE.s4}px;
        display: flex;
        justify-content: center;
        z-index: 10;
    `);
    const inner = el("div", "width:100%; max-width:848px; display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;");
    const template = selectedTemplate();
    const summary = el("div", `font-size:${TYPE.t11}px; color:${THEME.fg1}; min-width:0; flex:1 1 240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`);
    summary.textContent = template && state.selectedProject
        ? `${template.name} on ${state.selectedProject}`
        : "Choose a workflow and a project.";
    const open = button(state.opening ? "Opening…" : "Open in the editor", "", "primary", () => void openEditor());
    const reason = !template ? "Choose a workflow first" : (!state.selectedProject ? "Choose a project first" : "");
    setButtonDisabled(open, !!reason || state.opening);
    open.title = reason || "Open the Sonder Editor on this workflow and project, in this tab";
    inner.append(summary, open);
    bar.append(inner);
    return bar;
}

function render() {
    const scrollY = window.scrollY;
    const listScroll = root.querySelector?.("[data-sonder-project-list]")?.scrollTop || 0;
    root.replaceChildren(...[
        renderHeader(),
        renderExitNotice(),
        renderEnvironment(),
        renderWorkflows(),
        renderProjects(),
        renderOpenBar(),
    ].filter(Boolean));
    window.scrollTo(0, scrollY);
    // The rebuilt project list keeps its scroll position (durable rules:
    // rerendering a scrollable collection preserves its viewport).
    const projectList = root.querySelector?.("[data-sonder-project-list]");
    if (projectList) projectList.scrollTop = listScroll;
}

async function main() {
    if (!document.querySelector("[data-sonder-toast-stack]")) mountToastStack(document.body);
    configureNotifications(notificationCoreConfig());
    subscribeEditorSettings(() => configureNotifications(notificationCoreConfig()));

    const flag = takeSdkExitFlag();
    if (flag) {
        state.selectedProject = String(flag.project || "");
        state.selectedTemplateId = String(flag.template_id || "");
    }
    await Promise.all([loadStatus(), loadTemplates(), loadProjects()]);
    document.body.classList.add("ready");
    render();
    if (flag) void pollExitSession(flag);
}

export function isLauncherPage(doc = globalThis.document) {
    return doc?.body?.dataset?.sonderLauncher === "true";
}

function boot() {
    window.name = LAUNCHER_WINDOW_NAME;
    installComfyApiShim();
    injectSonderFontFaces();
    applyNativeControlTheme(document.documentElement);
    api = window.comfyAPI.api.api;
    root = document.getElementById("app");
    root.style.cssText = `
        box-sizing: border-box;
        width: 100%;
        max-width: 880px;
        margin: 0 auto;
        padding: ${SPACE.s5}px ${SPACE.s4}px 96px;
        display: flex;
        flex-direction: column;
        gap: ${SPACE.s4}px;
        font-family: ${FONT.sans};
        color: ${THEME.fg0};
    `;
    createNameInput = el("input", `${chromeInputCss({ padding: "5px 8px", fontSize: `${TYPE.t12}px` })} flex:1 1 200px; min-width:0;`);
    createNameInput.type = "text";
    createNameInput.placeholder = "New project name";
    createNameInput.maxLength = 120;
    // Restored from the back/forward cache (Back from the editor): "Opening…"
    // and the exit notice would be stale, so load afresh.
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) window.location.reload();
    });
    main().catch((err) => {
        console.warn("[Sonder] Launcher failed:", err);
        const status = document.getElementById("status");
        if (status) status.textContent = "The Launcher couldn't start. Reload the page.";
    });
}

if (isLauncherPage()) boot();
