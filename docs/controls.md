# Controls

Change a workflow's settings — steps, a sampler, a seed, a Selector choice —
from the editor, without going back to the graph. The settings come from a
**Sonder Control Relay** node in the workflow.

## Open the Controls tab

In the editor, click the **Controls** tab in the sidebar at the left, beside
**Assets** and **References**.

The **Editor** group at the top holds take settings such as
**Takes start muted**. Below it are the settings the workflow exposes, under
the group names its author chose.

![The Controls tab: the workflow's description, the Editor group with Takes start muted, Link take video + audio, Max frames per batch and Output preset, then a Generation group with a note, Seed set to Randomize, Steps and Scheduler](images/controls-tab.webp)
<p align="center"><em>The Controls tab with the Editor group and one
workflow group, Generation, which starts with an author's note.</em></p>

A workflow without Controls says *No Controls yet*. Click
[Manage](#manage) and add a setting; on the canvas, the editor adds the
**Sonder Control Relay** node to the graph for you (one per workflow).

<!-- Placeholder: link the Controls example workflow here when it ships. -->

## Change a setting

Click a value and change it. Long option lists have a filter at the top. A
seed shows its mode beside it (fixed, increment, decrement, randomize).

In the Sonder App, values are saved for that imported workflow.

A setting that can't be used — its node was removed, muted or linked to
another node — says why and is skipped, so the workflow's own value runs.

Each take records the values it used. Open the take in **Assets** and look for
**Controls at run** in its details.

## Presets

A preset is a named set of Controls that sets up one job (text-to-video,
detailing, and so on) in one choice.

- **Apply one:** choose it in the **Preset** picker near the top of the
  Controls tab (below the workflow description). Controls outside the preset
  keep their values. The picker reads **Custom** once you change a member.
- **Create one:** click **Manage**, then **+ Preset**. Tick **In preset** on
  each Control it should set, then click **Create**.
- **Update one:** in **Manage**, choose the preset and click **Update from
  current values**.

A preset whose Control is gone or refused is greyed out and names it; fix it
in **Manage**. Presets are saved in the workflow and in its API export. In the
Sonder App, editing a preset keeps your copy when you re-import the workflow;
**Reset to workflow's version** brings back the imported one.

## Manage

Click **Manage** at the top right of the Controls tab. Click **Done** to leave.

- **Add a setting:** click **+ Setting** (in the row with **+ Note** and
  **+ Group**). Settings are listed under each node's title; type in **Filter
  by node or setting** to find one, then click **Add** beside it. In the Sonder
  App, **+ Setting** adds an *App only* Control, which never appears on the
  canvas.
- **Bring back removed settings:** **+ Setting → Exposed before → Restore all**
  (up to 64 per click; repeat for more).
- **Group and order:** **+ Group** adds a group. Drag a row by its handle to
  reorder it or move it to another group.
- **Notes:** **+ Note** adds a Markdown note. Double-click a note to edit it;
  **Ctrl+Enter** (**⌘+Enter** on macOS) or **Done** saves, **Esc** cancels. A
  note whose first line is bold (`**Before you start**`) or a heading
  (`## Before you start`), with more text after it, collapses: click that line
  to fold or unfold the note (in **Manage**, the small **v** / **>** button at
  its left). A note can link to any heading of these docs.
- **Workflow description:** up to 1,000 characters of Markdown, with a
  preview. **Ctrl+Enter** saves, **Esc** cancels. The Sonder App Launcher shows
  it beside the workflow.
- **Clean up unavailable…** lists what no longer resolves in this workflow (a
  setting whose node was deleted, saved app values from an older version,
  preset members pointing at those). It removes only what you leave ticked.
  Controls that **Restore Controls** can reconnect are not listed. On the
  canvas the cleanup is one Undo step.

## Choosing lanes and folders from Controls

The **Sonder Reference Selector**'s lanes, the **Sonder Driver Selector**'s
lane and the **Sonder Save Bridge**'s target folder can be Controls, with the
same pickers their nodes show.

1. [Open the Controls tab](#open-the-controls-tab) and click **Manage**.
2. Click **+ Setting**. Type the node's name in **Filter by node or setting**,
   then click **Add** beside `reference_lanes` (Sonder Reference Selector),
   `driver_lane_index` (Sonder Driver Selector) or `target_folder` (Sonder Save
   Bridge).
3. Click **Done**.

The target folder appears in the **Editor** group; the lanes appear with your
other settings. Each offers choices from the open project and scene:

- **Reference lanes:** the selected lanes with their recipe and status. Click
  **×** on a lane to remove it, or **+ Add lane** to pick another. A lane that
  can't join the others is greyed out with the reason underneath.
- **Driver lane:** a menu of the scene's Driver lanes by name. If no lanes can
  be read (no scene open, or the read failed), type the lane number and press
  **Enter**; **Retry** appears when the read failed.
- **Target folder:** type a folder name and press **Enter** (or click
  elsewhere). The project's folders and **Root** are suggested as you type.
  **Esc** abandons what you typed.

On the canvas, the **Sonder Control Relay** node shows a summary of these
settings (for example *Lanes 1, 3*). Change them in Controls or on their own
node.

## Switch workflows in the Sonder App

In the Sonder App, the **Workflow** menu at the top of the Controls tab opens
another imported workflow on the same project. The project, scene and timeline
stay; takes already generating finish with the workflow they started with. Star
a workflow there, or in the Launcher's **⋮** menu, to keep it at the top of both
lists.

## Share Controls

- **From the canvas:** save the workflow (it carries the Controls and presets),
  or use ComfyUI's **Export (API)** for the Sonder App.
- **From the Sonder App:** in the Launcher, open the workflow's **⋮** menu and
  click **Export as API**. The file carries current values, seed modes, labels,
  notes, group order, **Advanced** flags, the description and active app-only
  Controls. Hidden entries are left out; disabled Controls stay disabled. If a
  Relay can't be read or the file would be too large to import, Export says so
  instead of downloading.

**Re-importing** a workflow into the Sonder App keeps your values, seed modes,
app-only Controls, hidden settings and edited presets. It resets labels, notes,
order, groups, **Advanced** flags and the description to the workflow's. A
saved value whose setting now points somewhere else resets to the workflow's
value. The Launcher lists what was kept and what was reset.

Loading an API file on the ComfyUI canvas flattens subgraphs and can lose some
node settings; send the saved canvas workflow too when the recipient uses the
canvas. To reconnect the Controls of an API file loaded on the canvas, see the
next section.

## Reconnect Controls after loading an API file

1. Load the API file on the ComfyUI canvas. On the **Sonder Editor** node,
   select your project, then click **Open Editor**.
2. Open the **Controls** tab. In the notice at the top, click **Restore
   Controls**. (Or, on the canvas, find the **Sonder Control Relay** node,
   searching for **Control Relay** if it is off-screen, and click **Restore N
   Controls from this file**; the editor doesn't need to be open.)
3. Restore reconnects every setting it can match and leaves values, labels and
   presets as they are. To undo it, click the canvas and press **Ctrl+Z**
   (**⌘+Z** on macOS) once.
4. If the message says some Controls **need Rebind**, click **Manage**, then
   **Rebind** beside each one and choose its setting.
5. Save the workflow for the canvas, and **Export (API)** again for the Sonder
   App.

Until you restore, the Controls stay missing, including in an API export made
from that canvas. **Exposed before** suggestions in an API file are not
restored by **Restore Controls**; add them again with **+ Setting**. Restore
doesn't rebuild subgraphs or node settings the API load lost.
