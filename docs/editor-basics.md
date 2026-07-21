# Editor Basics

How the Sonder Editor surface is organized and how you edit on it: the layout,
the timeline, every item type you can place, and the gestures and shortcuts
that move them around.

This guide covers editing. For selections, prompts, guides, Drivers, templates,
and the render queue, see [Generating](generating.md). For the asset gallery,
inspection, and export, see [Assets & Gallery](assets-and-gallery.md).

![The Sonder Editor fullscreen surface](images/editor.webp)

## Opening the editor

The **Sonder Editor** node on the ComfyUI canvas shows a compact dormant card
with the project selector and expandable Assets / Preview / Queue modules.
Click **Open Editor** to expand the full editing surface over the browser
viewport. The node card shows a placeholder until you exit.

If you've moved the editor to its own browser tab (see
[Mount in Tab](#mount-in-tab)), the button reads **Open Mounted Editor** and
focuses that tab instead of opening a second editor.

## Screen layout

From top to bottom:

1. **Header bar** — project pill, scene switcher (‹ › and the scene pill),
   **+ Scene**, and **Mount in Tab** / **✕ Exit**.
2. **Top row** — the **Assets sidebar** on the left (with the **Render Queue**
   panel docked at its bottom) and the **Viewport** in the center with its
   transport bar (Play, frame counter, clickable progress bar).
3. **Toolbar** — grouped controls: undo/redo, the generation-window block,
   timeline tools, view tools, queue & export, scene geometry, and the
   **?** shortcut overlay and **⚙** Settings buttons.
4. **Timeline** — the multi-lane editing canvas. Inline item editors appear
   directly beneath it.

The sidebar width and timeline height are drag-resizable and remembered
between sessions (**Settings ▸ Layout & UI Scale ▸ Reset Editor Layout**
clears them).

## The timeline

### Track order (top to bottom)

1. **Video lanes** (V1, V2, …) — stack for compositing; the higher-numbered
   lane renders in front.
2. **Audio lanes** (A1, A2, …) — independent rows.
3. **Driver lanes** — one Driver clip per lane (conditioning, not output).
4. **Guides** — a fixed track of guide-frame markers.
5. **Global** — the always-on scene-wide prompt.
6. **Prompt** — the segment prompt-section lane.

### Lane headers

Each media lane header has three independent controls plus the label:

- **▾ Collapse** — folds the lane visually. Layout only — it never affects
  rendering.
- **🔒 Lock** — blocks edits on that lane; locked items can't be selected and
  destructive actions refuse.
- **👁 / 🔊 Hide** — removes the lane's content from preview *and* render
  output. **Hiding is the only render-skip control**; collapsing never is.
- **Label** — drag across headers to select multiple lanes; rename via
  right-click.

Clicking a collapse/lock/hide icon on a selected header applies that state to
every selected lane at once.

The fixed **Guides / Global / Prompt** headers carry a **☰** icon that opens
their management panel. Their lock/hide semantics matter for generation — see
[Generating](generating.md).

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

An audio asset — or the extracted audio of a video — on an audio lane, drawn
with its waveform. Same select/move/trim/split/mute model as video clips.
The inline editor exposes start, volume, and mute.

### Guide frames

A reference image keyed to one frame on the Guides track, used for generation
conditioning and as the viewport animatic. A guide "holds" until the next
guide (or scene end). Create one by dropping an image on the Guides track,
using **Add Frame to Guides** on a clip (captures the clip's frame at the
playhead), or from Guide Management. Guides have a strength (0–1) and can be
muted, replaced in place, or re-keyed.

### Driver clips

A clip whose role is **motion/look conditioning, not visible output** —
Drivers steer the generated clip's composition, movement, look, or
characters, depending on what your model supports. Driver lanes hold at most
one clip each, and Driver clips never appear in normal preview or renders
(only in Animatic mode, as animated reference). Drivers can't be split — use
separate Driver lanes for separate conditioning sources. Convert a video clip
to a Driver (and back) from its right-click menu.

### Prompt sections

A text range on the Prompt lane. A section holds until the next section
starts, and each carries three channels — **Visual / Speech / Sounds**.
Double-click empty lane space to create one; double-click a section to edit
its text inline. The lane is no-overlap: drags abut neighbors or swap with
the hovered section. How sections compose into the generation prompt is
covered in [Generating](generating.md).

### The Global prompt

The scene-wide style/identity text — one full-width, non-draggable item on
the Global lane, composed in front of the covering section's text.
Double-click to edit.

### Linked groups

Any mix of items can be linked into a **symmetric edit group** (badge letter
A…Z): move, delete, split, and mute act on the whole group and refuse
atomically if any member is locked. Single-click selects the whole group;
**double-click isolates one member** for per-item edits. Video+audio drops
and generated takes auto-link (configurable in Settings). Link management
lives in the right-click menu.

## Editing gestures

- **Snapping** (`S` to toggle) pulls dragged edges to clip edges, guides, and
  the playhead, with a visible snap guide line. Threshold and per-type
  targets are in Settings ▸ Timeline Behavior.
- **Razor mode** (`C`): click any clip to split it at that point.
  **⌇ Split Here** splits the selected items — or whatever sits under the
  playhead — at the playhead.
- **Muting** (`M`): mutes/unmutes the selected items. Mute is per-item render
  participation and composes with lane Hide.
- **Deleting** (`Del`/`Backspace`): deleting the last item on a video/audio
  lane also removes the now-empty lane.
- **Undo** (`Ctrl+Z` / `Ctrl+Y`) covers timeline mutations broadly. Text
  fields keep native text undo; a committed Apply is one undoable step.

### Dropping assets onto the timeline

Where you drop a gallery asset decides what happens:

- **The ruler strip** → always creates a **new lane**. Video → a video lane;
  audio → an audio lane; a video with extracted audio → both lanes, linked.
  This is the only dual-drop path.
- **An existing lane** → places on exactly that lane, matching type only.
  Dropping a video on an audio lane places just its extracted audio; audio on
  a video lane refuses.
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
  audio pause together and resume in sync — audio never drifts ahead of the
  picture.

## Mount in Tab

**Mount in Tab** (header bar) moves the editor into its own browser tab,
freeing the ComfyUI canvas while staying connected: scene, selection, and
queue controls relay live to the node. The canvas page must stay open — the
tab shows a **● Canvas connected** pill while healthy and pauses
execution-bound controls when the canvas is gone. On the canvas, the dormant
button becomes **Open Mounted Editor** while the tab owns the session, and
**Force Release** is the explicit recovery action if the tab was closed
uncleanly.

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

Gallery and inspect-overlay shortcuts are listed in
[Assets & Gallery](assets-and-gallery.md).
