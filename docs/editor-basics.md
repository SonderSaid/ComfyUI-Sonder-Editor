# Editor Basics

How the Sonder Editor surface is organized and how you edit on it: the layout,
the timeline, every item type you can place, and the gestures and shortcuts
that move them around.

This guide covers editing. For selections, prompts, guides, Drivers, templates,
and the render queue, see [Generating](generating.md). For the asset gallery,
inspection, and export, see [Assets & Gallery](assets-and-gallery.md).

![The Sonder Editor fullscreen surface](images/editor.webp)

## Opening the editor

### The node card

Add the **Sonder Editor** node to your graph: double-click an empty spot on the
canvas, type **Sonder Editor** and pick it. Closed, the node shows a card with
the **project** selector, the project's size, length and selection, and
**Assets**, **Preview** and **Queue** buttons that open small panels without
leaving the graph.

![The Sonder Editor node card with a project loaded and its Preview module expanded](images/node-card.webp)

### Fullscreen

Click **Open Editor** on the card. The editor covers the browser window, and
the card shows a placeholder until you click **✕ Exit** at the top right.

### Mount in Tab

**Mount in Tab**, at the top right of the editor, moves the editor into a
browser tab of its own, so the editor and the graph are both in view. Keep the
canvas page open: the tab shows **● Canvas connected** while it can run the
graph, and pauses run controls when the canvas is gone. On the canvas, the
card's button reads **Open Mounted Editor** and brings that tab forward. If the
tab was closed without exiting, click **Force Release** on the card.

## Creating a project

A project is a folder holding its media, scenes and generated takes, under
`ComfyUI/output/sonder-projects/`.

1. On the **Sonder Editor** card, click the **project** selector and choose
   **+ Create New**.
2. Type a name in **project_name** and set **fps**, **width** and **height**.
3. Click **Create**. The card switches to the new project.

To open another project, choose it in the same selector. Inside the editor,
**+ Scene** at the top adds a scene to the project, and the scene pill beside
it switches scenes.

## Screen layout

From top to bottom:

1. **Header bar** — project pill, scene switcher (‹ › and the scene pill),
   **+ Scene**, and **Mount in Tab** / **✕ Exit**.
2. **Top row** — the **Assets | References | Controls sidebar** on the left (Assets keeps
   the **Render Queue** panel docked at its bottom) and the **Viewport** in the center with its
   transport bar (Play, frame counter, clickable progress bar).
3. **Toolbar** — grouped controls: undo/redo, the generation-window block,
   timeline tools, view tools, queue & export, scene geometry, and the
   **?** shortcut overlay and **⚙** Settings buttons.
4. **Timeline** — the multi-lane editing canvas. Inline item editors appear
   directly beneath it.

The sidebar width and timeline height are drag-resizable and remembered
between sessions (**Settings ▸ Layout & UI Scale ▸ Reset Layout Section**
clears them).

**References** is the project-level authoring Library for reusable character,
location, prop, outfit, voice, and video media. The Library holds the media
itself; **Reference lanes** on the timeline are what scope its members over a
range of frames. See
[References](references.md).

## The timeline

### Track order (top to bottom)

1. **Video lanes** (V1, V2, …) — stack for compositing; the higher-numbered
   lane renders in front.
2. **Audio lanes** (A1, A2, …) — independent rows.
3. **Driver lanes** — one Driver clip per lane (conditioning, not output).
4. **Reference lanes** (R1, R2, …) — scope Library members over a range
   (conditioning, not output).
5. **Guides** — a fixed track of guide-frame markers.
6. **Global** — the always-on scene-wide prompt.
7. **Prompt** — the segment prompt-section lane.

### Lane headers

Each media lane header has three independent controls plus the label:

- **▾ Collapse** — folds the lane to make room on the timeline.
- **🔒 Lock** — prevents selecting and editing items on that lane.
- **👁 / 🔊 Hide** — excludes the lane from preview and generation.
- **Label** — drag across headers to select multiple lanes; rename via
  right-click.

Clicking a collapse/lock/hide icon on a selected header applies that state to
every selected lane at once.

The fixed **Guides / Global / Prompt** headers carry a **☰** icon:
**Guide Management** on Guides, and
[Prompt Management](prompts.md#opening-prompt-management) on Global and
Prompt.

### Navigation

| Gesture | Action |
|---|---|
| Wheel | Scroll lanes vertically |
| Ctrl+Wheel | Pan horizontally |
| Shift+Wheel | Zoom |
| Ruler click/drag | Move the playhead |
| Drag empty timeline space | Box-select items |
| Drag across lane headers | Select lanes |

## Item types

### Video clips

A video or image asset on a video lane. Lanes composite top-down with
per-clip opacity. Click selects (a linked clip selects its whole group);
double-click isolates one item and opens its inline editor (start, opacity,
visibility, fit mode). Drag moves, edge-drag trims, and dragging onto an
occupied lane of the same type swaps lanes with the displaced item. Clip
starts clamp to the scene; tails may overhang.

Each clip has a **fit mode** for when its aspect ratio differs from the
scene: *Fit (black bars)*, *Fit (edge pad)* (default — pads by replicating
the source edge), *Fill (crop)* with a crop anchor, or *Stretch*.

### Audio tracks

An audio asset, or the sound of a video, on an Audio row, drawn with its
waveform. Move, trim, split and mute work as for video clips; double-click
opens its start, volume and mute.

Where to drop an audio file or a video's sound is in
[Dropping assets onto the timeline](#dropping-assets-onto-the-timeline). Keep
one copy of a sound; placing it twice doubles what you hear. To mute only the
sound of a linked clip, see [Linked groups](#linked-groups).

### Guide frames

A reference image keyed to one frame on the Guides track, used for generation
conditioning and as the viewport animatic. A guide "holds" until the next
guide (or scene end). Create one by dropping an image on the Guides track,
using **Add Frame to Guides** on a clip (captures the clip's frame at the
playhead), or from Guide Management. Guides have a strength (0–1) and can be
muted, replaced in place, or re-keyed.

### Driver clips

A Driver uses a clip to steer generation without appearing in the output.
To create one, right-click a Video clip and choose **Convert to Driver**.
To show it as footage again, right-click it and choose
**Convert to Render Clip**. For the graph connections, follow
[Drive with a control video](getting-started.md#drive-with-a-control-video).

Each Driver lane holds one clip. Drivers appear in **Animatic** preview;
they cannot be split. Use separate lanes for separate conditioning sources.

### Reference items

A [Reference item](references.md#reference-lanes) places Library members over
a range of frames. Drag to move it; trim its edges to change the range.

Create one by dragging a Reference from the **References** sidebar onto the
timeline. Dropping on the **ruler** always makes a new lane; dropping on an
existing lane stages into it, and is refused with a message if that lane is
locked, already occupied at that frame, or holds the other media kind. Stage
image/video references separately from voice-reference audio — a mixed
selection is refused before the drag starts.

An expanded Library card also offers **Add to timeline**, which stages at the
playhead on the first compatible lane, making a new one only if none fits.

Lane rules, recipes, and what a lane resolves to for a generation window are in
[References](references.md).

### Prompt sections

A text range on the Prompt lane. A section holds until the next section
starts, and its fields come from the project's **Channel Template** — a new
project starts on **Standard**, one plain text field, while other templates
split it into labelled channels or a model's own field set. Double-click
empty lane space to create one; double-click a section to edit its text
inline. The lane is no-overlap: drags abut neighbors or swap with the hovered
section. How sections compose into the generation prompt, and which templates
exist, is covered in [Prompts](prompts.md).

### The Global prompt

The **Global** bar spans the whole scene and can't be moved or split. Its text
goes in front of every Prompt section's text: put what holds for the whole
scene there (style, place, characters), and what happens at a moment in a
Prompt section. Double-click the bar and type. There is no **Create** button:
Global keeps your text when you press Enter or click elsewhere, and **Esc**
discards what you typed.

### Linked groups

Any mix of items can be linked into a **symmetric edit group** (badge letter
A…Z): move, delete, split, and mute act on the whole group and refuse
atomically if any member is locked. Single-click selects the whole group;
**double-click isolates one member** for properties such as volume or
opacity, while mute still affects the group. Video+audio drops and generated
takes auto-link (configurable in Settings). Right-click **Unlink Linked Items**
before muting only one member.

## Editing gestures

![A working timeline: imported clips, placed takes, a Driver lane, guide frames, and prompts](images/timeline-editing.webp)

- **Snapping** (`S` to toggle, or the **Snap** button above the timeline)
  pulls a dragged item's start **or end**, and a trimmed edge, to nearby
  clip, audio, prompt and Reference item edges, guides, the playhead, In/Out
  and the scene's ends, with a dashed snap guide line where it lands. When
  several items move together, the one you grabbed and the outer edges of
  the group snap. The distance (in frames) and each kind of target are in
  **Settings** (the gear at the right of the timeline toolbar) ▸ **Timeline
  Behavior**. Two more switches there are off by default: **Snap Playhead on
  the Ruler** pulls the playhead to those targets when you click or drag on
  the ruler, and **Snap Razor Cuts** moves a Razor cut to a target inside
  the item you click. Because the distance is in frames, it reaches further on screen
  when you zoom in; press `S` to scrub or cut freely.
- **Razor mode** (`C`): click a clip, audio track, prompt section or Reference
  item to split it at that point (with **Snap Razor Cuts** on, at the snap
  guide shown while you hover, when a target is close). **⌇ Split Here** does the same to everything
  selected, at the playhead. Both cover the same four item types, and both say
  why when something cannot be cut — an item the frame falls outside, a locked
  lane, or a Driver clip. Split Here acts only on the **selection**: with
  nothing selected it tells you so rather than cutting whatever happens to sit
  under the playhead. Splitting several items at once is one undo step.
- **Muting** (`M`): mutes/unmutes the selected items. Mute is per-item render
  participation and composes with lane Hide.
- **Deleting** (`Del`/`Backspace`): deleting the last item on a video/audio
  lane also removes the now-empty lane.
- **Undo** (`Ctrl+Z` / `Ctrl+Y`) covers timeline mutations broadly. Text
  fields keep native text undo; a committed Apply is one undoable step.

### Dropping assets onto the timeline

Where you drop a gallery asset decides what happens:

- **The ruler strip** (the frame numbers at the top) → always a **new row**.
  A video → a Video row; audio → an Audio row; a video with sound → both,
  linked. This is the only drop that places picture and sound together.
- **An existing row** → that row only. A video with sound on a **Video** row
  places the picture; on an **Audio** row, the sound. Audio on a Video row is
  refused.
- **The Guides track** → an image dropped there becomes a guide frame.
- Locked lanes, overlaps, and dead space refuse with a toast pointing at the
  ruler gesture. A type-aware highlight always shows where the drop will land.

## The viewport

The center panel previews the scene at the playhead: all visible render clips
composite in lane order, guide frames render beneath video as an animatic
layer, and Driver clips are excluded so preview matches what actually
renders.

- **Animatic mode** (`A`) hides all video lanes so you see only guides and
  Driver reference playback.
- **Playback preferences** (loop selection, auto-scroll, return-to-start,
  playback resolution) are personal and live in Settings ▸ Playback.
- Heavy scenes rebuffer adaptively: if decoding falls behind, playhead and
  audio pause together and resume in sync.
- A clip your browser can't play (ProRes, Lossless FFV1 and the like) shows
  its [proxy](assets-and-gallery.md#proxies). Until the proxy is ready, a band
  across the viewport names the clip and what it waits for, such as **Proxy
  waiting for ComfyUI** or **No proxy yet**; the other layers still show.

### Panes and Output Views

The viewport can show up to four things side by side. Click the buttons at
the right of the bar above the viewport: **1** (one pane), **2** or **3**
(side by side) or **2×2** (four).

Each pane has a source button at its top left (**Composite ▾** at first).
Click it and choose:

- **Editor**: **Composite** (the scene preview described above), **Guides**
  (the guide frame at the playhead) or **Driver 1**, **Driver 2**… (that
  Driver lane's frame at the playhead).
- **Views**: outputs of the workflow you have already added as Views.
- **Add output…**: the workflow's output nodes that have no View yet
  (**Preview Image**, **Sonder Preview Video**, **Sonder Save Video** and
  the like, including ones inside a subgraph). Click one to add it as a View
  and show it in this pane. On the canvas, adding a View is one Undo step.
- **Sampling previews**: samplers whose live preview you can watch. See
  [Watching a sampler's preview](#watching-a-samplers-preview).

The panes and their sources are remembered for each workflow in this
browser. Fullscreen and **Mount in Tab** share them; each workflow in the
[Sonder App](sonder-app.md) keeps its own.

A View pane shows the newest result of its output node from runs started in
this editor (in the Sonder App, from this app workflow). Its bar has:

- **Run**: runs that output node and only what it needs, not the whole
  workflow. **Cancel** takes it off the queue or stops it while it runs
  (**Canceling…** shows until the node stops).
- **Hold**: keeps the result you are looking at when the output runs again.
  **Release** follows the newest again. To compare before and after, show the
  same View in two panes and hold one.
- **◀ 1/3 ▶**: steps through several images.
- **Fit** / **Fill**: shows the whole output, or fills the pane. The mouse
  wheel zooms toward the cursor and dragging pans, as in
  [the inspect overlay](assets-and-gallery.md#the-inspect-overlay).
- **Unmute** (**Listen** while the pane is linked): hear this pane instead of
  the timeline. Only one source plays sound at a time; **Mute** gives the
  sound back to the timeline.
- **Link** / **Linked**: on video and audio outputs. See
  [Following the timeline](#following-the-timeline).

The text beside the source button says what is happening: **No result yet**,
**Running**, **Waiting for run**, **Last run was canceled**, **From cache**
(ComfyUI reused an earlier result) or **Didn't run:** and the reason. **This
View is no longer in the workflow** means its node was deleted: pick another
source, or delete the View. In a mounted tab, panes read **Disconnected**
while the canvas page is closed or reloading.

To rename, reorder or delete Views, see [Manage](controls.md#manage).

#### Following the timeline

A video or audio View pane can play and scrub with the editor instead of on
its own, so you can compare it with the **Composite** frame by frame. The
**Linked** button on the pane's bar turns this on (it is on by default, and
comes back on when you pick another source); click it to read **Link** and
play the output on its own again.

While linked, the pane's own play button and scrub bar are hidden and the
editor's **Play**, the playhead and the timeline move it. The pane knows where
the output belongs:

- **A take** from **Sonder Save Video** that was placed on the timeline
  follows the frames its take clip covers.
- **Any other output** follows the Timing you set for its View: open the
  **Controls** tab, click **Manage**, and under **Views** find the View's
  **Timing** row. Choose **Follows from the render start** (the output's
  first frame is the first frame the Editor rendered, context included) or
  **Follows from the generation start** (the first frame of your selection).
  **Skip first** drops that many frames from the start of the output. Leave
  the frame-rate box empty when the output has the scene's frame rate, or
  type its rate. **No timing set** leaves the output unlinked.

What the pane shows while linked:

- **Outside generated range**: the playhead is outside the frames this output
  was made for. The picture comes back when the playhead returns.
- **Catching up**: the pane fell behind (slow decoding) and is catching up.
  The editor never waits for it.
- **Previous context**: the scene changed inside this output's range after it
  ran, so it may no longer match what is around it. A new take never counts.
  **Undo** of the change clears it.
- **Unlinked:** and the reason, when the pane cannot follow: for example the
  output was made for another scene or at another frame rate, or its View has
  no Timing (**No timing for this output. Set it in Manage ▸ Views**).

A linked pane sounds only while the editor plays inside its range.

#### Watching a sampler's preview

A pane can show the preview a sampler draws while it runs, the same picture
ComfyUI shows on the sampler node, with the step it is on.

1. Make sure ComfyUI draws previews. Open ComfyUI's **Settings** (the gear
   icon), search for `preview`, and set **Live preview method** to **auto**,
   **latent2rgb** or **taesd**. **default** uses the option ComfyUI was
   started with, which draws no previews unless ComfyUI was started with
   `--preview-method`. The [Sonder App](sonder-app.md) always uses that start
   option, so for it start ComfyUI with `--preview-method auto`.
2. In the editor, click a pane's source button and look under **Sampling
   previews**. It lists the workflow's **KSampler**, **KSampler (Advanced)**,
   **SamplerCustom**, **SamplerCustomAdvanced** and KJNodes **Model Preview
   Override** nodes, and any other node that sent previews in a recent run
   (marked **Sent previews in a recent run**). In fullscreen, a greyed-out
   row says why it can't be added, for example **This node is no longer in
   the workflow**; in a mounted tab or the Sonder App, a message says so
   when you click it.
3. Click the sampler. It becomes a View, and the pane shows it from now on.
4. Run the workflow as usual (**Queue** on the canvas, or **Generate** or
   another pane's **Run** in the Sonder App). A sampling View has no **Run**
   of its own.

The pane shows the newest preview only. Its bar has **Fit** / **Fill**, and
the mouse wheel and dragging zoom and pan. The text beside the source button
says:

- **Sampling · step 12/30** while the sampler runs, and **Last preview ·
  step 30/30** after it finishes.
- **Running** and **Waiting for previews** before the first preview arrives.
- **Previous run · Running**: a new run has started, and the picture is still
  the last run's.
- **From cache**: ComfyUI reused the sampler's earlier result, so the picture
  is from the run that made it.
- **No previews**, with the reason: previews were off for the run (see step
  1), the node came from ComfyUI's cache, or a **Model Preview Override** on
  the sampler's model took them (add a View on the Override node instead).
  **Last run was canceled** or **Last run failed** when the run ended before
  the node sent one.
- **Not in the newest run** (added to the text): your last run didn't include
  this node, so the picture is from an earlier one.
- **Switched off**: the node is muted or bypassed; the last picture stays.
- **Unavailable**: the preview is no longer kept, or is too large to show
  here; for a **Model Preview Override** View, this KJNodes version can't
  tell which run a preview belongs to.
- **Unsupported**: the browser can't show this preview's format.

Through **Model Preview Override**, a preview can be a short looping clip. A
sampler inside a subgraph works the same way. Previews are not kept: after a
ComfyUI restart the pane reads **No result yet** until the next run.

## Safety & recovery

- Asset deletes route to a **Trash** first; permanent deletion is
  confirmation-gated; deleting an in-use asset warns with its usages.
- **Missing files** show placeholders while references stay recoverable —
  **Relink** restores them (see [Assets & Gallery](assets-and-gallery.md)).
- Saves are **version-checked**, so edits made during a long render or export
  merge instead of overwriting each other.
- Locked lanes and items make destructive actions refuse atomically.

## Keyboard shortcuts

Press **?** in the editor for this list in-app. Shortcuts never fire while
you're typing in a text field.

### Playback

| Key | Action |
|---|---|
| Space | Play / Pause |
| ← / → | Frame back / forward |
| Shift+← / → | 10 frames back / forward |
| Home / End | First / last frame |

### Selection

| Key | Action |
|---|---|
| I / O | Anchor/set In / Out at the playhead (template-snapped) |
| X | Clear selection |

### Tools & editing

| Key | Action |
|---|---|
| C | Toggle razor / cut mode |
| M | Mute / unmute selected item(s) |
| S | Toggle snapping |
| T | Toggle timecode display |
| A | Toggle animatic mode |
| F | Fit timeline to view |
| Shift+F | Zoom to selection |
| + / − | Timeline zoom |
| Del / Backspace | Delete selected items |
| Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z | Undo / Redo |
| ? | Shortcut overlay |
| Esc | Exit fullscreen / dismiss overlay / clear selection |

Gallery, inspect-overlay, and focused Reference-media shortcuts are listed in
[Assets & Gallery](assets-and-gallery.md).
