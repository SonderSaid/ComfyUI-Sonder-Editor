# Sonder App

The Sonder App runs a workflow on a project from its own browser page, without
the ComfyUI canvas. Its **Launcher** lists your projects and imported workflows
and opens the editor on the pair you choose, with **Generate** (1–8 takes per
click) beside **+ Queue** and the workflow's settings in the
[Controls](controls.md) tab. ComfyUI still does the generating.

The app is optional: the editor and every node work without it.

## Set it up

Do this once, in a browser on the ComfyUI machine.

1. On the ComfyUI canvas, double-click an empty spot, type **Sonder Editor** and
   pick it. On its card, create a project (or pick one), then click **Open
   Editor**.
2. Open **Settings**: the **⚙** at the right end of the toolbar just above the
   timeline, next to **?**. Scroll to the bottom; **Sonder App (experimental)**
   is the last section.
3. Click **Install**. It adds two packages, `comfy-sdk` 0.4.0 and
   `comfy-api-proxy` 0.1.6, to ComfyUI's Python. No restart is needed. If
   either is already installed at another version, or installing would change
   a package ComfyUI already has, it stops and says why; see
   [Troubleshooting](troubleshooting.md#install-in-settings--sonder-app-is-refused-or-doesnt-finish).

## Open the Launcher

- In **Settings ▸ Sonder App (experimental)**, click **Open Launcher**.
- Or open `http://127.0.0.1:<port>/sonder-editor/app`, with your ComfyUI port.
- On Windows, Install saves a **Sonder Launcher.url** shortcut in your Sonder
  projects folder (`ComfyUI/output/sonder-projects/` unless you changed it).
  Copy it to your Desktop, or anywhere else, to open the Launcher from there
  while ComfyUI runs. On macOS and Linux, bookmark the Launcher page instead.
- **Show Launcher Button In The Editor**, in the same Settings section, adds a
  Launcher button beside **⚙**.

## Import a workflow

1. On the ComfyUI canvas, open your workflow and set its nodes as you want
   them (the **In the graph** steps in [Getting Started](getting-started.md),
   for example). Use ComfyUI's **Export (API)** to save its API file. It needs
   exactly one **Sonder Editor** node and a **Sonder Save Video** or **Sonder
   Save Bridge** that takes `project` from it.
2. In the Launcher, click **Import…** in the Workflows area, or drop the file
   there.

A workflow that needs node packs you don't have is still imported, marked
**Missing nodes** with the packs to install; see
[The Launcher says *Missing node packs*](troubleshooting.md#the-launcher-says-missing-node-packs).

## Generate

1. In the Launcher, click **New project** in the project grid for an empty
   project (enter **Project name**, then **Create** or **Create and open**), or
   select an existing project.
2. Select the project **first**, then the workflow: selecting a project can
   bring back the workflow it last used.
3. Click **Open** in the bottom bar.
4. Set up the scene and the Selection with the **In the editor** steps of
   [Make a first take](getting-started.md#make-a-first-take), from step 4.
5. Leave the take count beside **Generate** at **×1** and click **Generate**.
   **+ Queue** adds the job to the Render Queue instead.

The workflow's settings are in the **Controls** tab; its **Workflow** menu
switches workflows in place ([Controls](controls.md#switch-workflows-in-the-sonder-app)).

## The local connection

When you open a workflow from the Launcher, Sonder starts a local connection to
ComfyUI (`comfy-api-proxy`) on a free port from 8189 to 8198, or reuses one
already serving this ComfyUI. It stops the one it started when ComfyUI exits or
after half an hour without a Sonder App session. ComfyUI started with its own
TLS options (`--tls-keyfile`, `--tls-certfile`) isn't supported. If Generate
can't connect, see
[Troubleshooting](troubleshooting.md#generate-says-the-sonder-app-couldnt-connect-to-comfyui).

## Takes made in the app

The app sends ComfyUI the workflow's API form, without the canvas workflow, so:

- a take's details say the workflow is **Not embedded**, and **Open Source
  Workflow** is unavailable for it;
- **Sonder Metadata Collector** sections are titled by their wired label, or the
  node's type, and a value from inside a subgraph is listed under the inner
  node.

**Controls at run** is still recorded, with the app's labels, groups and seed
modes.
