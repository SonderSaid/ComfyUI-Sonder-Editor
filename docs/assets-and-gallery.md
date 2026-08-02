# Assets & Gallery

Every piece of media in a project — imports, generated takes, captures,
exports — lives in the **asset gallery** under **Assets** in the editor's left sidebar. The
same gallery powers the dormant node card's Assets module, so what you see
here is what you see there.

The fullscreen and mounted sidebar also has a **References** tab. It groups
reusable image, voice, and video assets into project-level characters, locations,
props, and outfits; see [Reference Library](#reference-library).

Editing with these assets is covered in [Editor Basics](editor-basics.md);
generation in [Generating](generating.md).

## Finding things

- **Type tabs** with live counts — All · Videos · Images · Audio ·
  Artifacts.
- **Scope** — *All*, *Favorites*, or *Current Scene* (everything the active
  scene references through clips, Drivers, audio, and guides — including
  muted, hidden, and even trashed-but-referenced assets).
- **View** — *Folders* (collapsible groups, `Root` included) or *Flat*.
- **Sort** — Newest, Oldest, Name, Type, Duration, Resolution.
- **Search** — free text matches display names, plus token filters:
  `kind:` and `ext:` for artifacts, and `field:<name>=<value>` /
  `tracked:<text>` for tracked generation metadata (see below).

Rows carry badges: a **Scene** marker when the active scene uses the asset,
a **★ favorite** star, and a **Missing** placeholder when the file is gone
but the entry survives.

**Importing**: the Import button, dropping OS files onto the gallery
(imports into the hovered folder), or dropping an OS *folder* (imports as a
gallery folder). Drag a row onto the timeline to place it — the drop-zone
rules are in [Editor Basics](editor-basics.md#dropping-assets-onto-the-timeline).

## Asset lifecycle & safety

- **Rename** changes the display name only — the file on disk is never
  renamed.
- **Replace File…** swaps the media behind an asset while keeping the asset
  identity and every reference to it.
- **Move to Trash** is a soft delete: the asset moves to the **Trash**
  virtual folder at the bottom of the gallery, references stay recoverable,
  and **Restore** returns it to its original folder. Only **Delete
  Permanently** and **Empty Trash** are destructive, and both ask for
  confirmation. Trash can auto-purge by age/size (Settings ▸ Render).
- Deleting an asset that's still in use warns first with its usages. Timeline
  and queue usages become **Missing** placeholders after permanent deletion;
  Reference Library memberships are removed because they are explicit asset
  edges. Soft Trash, missing files, and automatic Trash purges preserve those
  Library memberships for repair or restore.
- **Missing assets** (file moved or deleted on disk) keep their entry and
  references — **Relink…** points them at the file again and everything
  reconnects.
- **Where Used…** answers "can I safely delete this?" — a grouped summary with
  per-reference rows for clips, guides, audio, generation jobs, and Reference
  Library members. Timeline-addressed usages can navigate to their scene;
  Library rows identify the owning entity and member instead.
- **Open Source Workflow** loads the ComfyUI workflow embedded in a
  generated file (PNG/MP4/M4V/MOV/MKV) straight onto the canvas — every
  generated asset can carry its own recipe.

## Reference Library

Switch the fullscreen or mounted left sidebar from **Assets** to
**References**. The choice is remembered locally; search text, open editors,
and unfinished drafts are not.

- Create a named **character**, **location**, **prop**, or **outfit**, choose
  whether it is a **subject** or **context**, and add notes. Locations begin as
  context; the other kinds begin as subjects, and either can be changed. Focus
  or hover the field help controls for an explanation.
- Cards always show their kind, class, notes, member count, and unresolved state.
  **Manage** reveals entity Edit/Delete; expansion and **+ Member** remain normal
  authoring actions.
- Add one image, audio, or video asset at a time. The picker can **Inspect** a
  source without selecting it. After selection, only compatible built-in tags
  are offered; custom tags remain unrestricted.
- Members can carry multiple tags and custom tags, prompt text, an image/video
  crop in source percentages, and an audio/video start/end trim in seconds. A
  blank end uses the rest of the source.
- **Crop & Preview**, **Trim & Preview**, or **Crop, Trim & Preview** opens the
  focused visual editor. **Full Source** shows editable crop/trim geometry;
  **Applied Result** shows only the cropped/trimmed result, and that choice is
  remembered locally. Audio uses the waveform for trim editing and a separate
  seek bar for playback, so dragging the selected waveform region moves the
  complete trim without colliding with seeking. Video uses the same seek bar
  and displays a waveform when it has audio or a neutral time rail when silent.
- Crop interiors use grab/drag movement and edge/corner handles resize. Choose
  the same ratio presets used by the timeline, **Free**, or **Custom** and enter
  a width/height ratio such as `3 × 10`. Ratio locking is an editing aid; only
  the resulting crop coordinates are saved. Trim handles resize, while dragging
  the middle of the selected interval preserves its duration and moves it.
- Use **Space** to play or pause the active Source/Result view. Focused crop and
  trim controls own their Arrow-key precision adjustments; the complete list is
  in the editor's **?** shortcut atlas. Finish with **Apply to Draft**; the
  member's main Save remains the only project mutation.
- **Edit**, **Replace**, **Remove**, and **Up/Down** act on individual members.
  Save and Cancel are explicit, so opening an editor does not change the
  project.
- Search matches reference names, kinds, notes, member tags, and current asset
  names. Missing, Trashed, and unresolved members stay visible.

Built-in tags use the reserved `sonder:` namespace and are media-aware. Voice
Identity accepts standalone audio and video only when the clip has embedded
audio. Custom tags may use any other wording;
capitalization from the first occurrence is retained. These tags are advisory
metadata only—they do not create timeline items or change prompts by
themselves.

## Generated assets & tracked metadata

Assets produced through the save nodes with a **Sonder Metadata Collector**
in the graph show a **tracked metadata** section in the inspector, above the
raw generation dump:

- **Field rows are clickable filters** — clicking toggles a
  `field:<name>=<value>` search token, turning the inspector into a search
  surface ("show me everything generated with this seed/model/LoRA").
  Free-form tracked search uses `tracked:<text>`.
- **Pins** lift fields you care about to a *Pinned Fields* card at the top
  (right-click a field or section header). Pins re-apply to any asset
  exposing the same labels.
- A **Raw** collapsible keeps the untouched generation dump.

**Artifacts** (latents, JSON, checkpoints, other backend outputs) are
first-class gallery entries: searchable and inspectable with a metadata
card, but they render no media preview and can't be dropped on the timeline.

## Manage mode & bulk actions

**Manage** toggles multi-select (checkboxes; Ctrl/Shift-click also work in
normal browsing). With a multi-selection, a bulk toolbar appears: **Move**
and **Trash** for active assets, **Restore** and **Delete Permanently** for
trashed ones. Bulk actions apply across both the fullscreen and dormant
galleries.

## The inspect overlay

Double-click (or Space) a gallery asset for fullscreen inspection.

- **Images** — wheel zoom pivots toward the cursor up to **16×** for
  pixel-level checks; left-drag pans; `F` fits.
- **Video** — Space plays/pauses; ←/→ steps one frame (Shift = 10,
  Ctrl = 1 s); **right-drag anywhere on the video scrubs** from the current
  playhead; the scrub bar handles absolute seeks.
- **Audio** — an interactive waveform: click/drag seeks, the played portion
  highlights.
- ↑/↓ cycle through assets without leaving the overlay. Toggling the
  **Metadata** panel never reloads the media — scrub position and play
  state survive.

## Compare mode

**Compare** (`C` in the overlay) puts two same-type assets side by side —
built for take review.

- **Layouts** — a screen-fixed **divider** over one shared stage (pan/zoom
  moves both together), or explicit **side-by-side** with linked zoom.
- **Pickers** — each side gets its own search box and keeps its own scroll
  position; a sticky toggle chooses which side the arrow keys cycle, so you
  can hold one take fixed and flip the other through candidates.
- **Video** — linked transport with drift recovery; an **A | B | None**
  control picks which side's audio plays.
- **Audio** — stacked waveforms; monitor **A / B / Both / Mute** on keys
  `1 / 2 / 3 / 0`, hold Shift to momentarily flip sides.
- **Metadata** — panels flank the media (A left, B right). Left-click a
  tracked field to filter the **A** picker, right-click to filter **B** —
  compare takes against their own generation parameters.

## Export timeline

The toolbar's **Export** button renders the timeline itself (full scene or
active selection) to a file through a streaming compositing path:

- **Save presets** — Compatible MP4, High Quality MP4, Editing Master MP4,
  ProRes 422 HQ, Lossless FFV1 (RGB), or Custom with expert encode
  controls. Video presets encode and tag BT.709, and timeline decoding
  honors source color tags, so exports match the browser preview.
- **Include video / audio** checkboxes, and **Place as take** to drop the
  result straight onto a fresh lane in the active scene (with optional
  linked, muted take placement).
- Exports show determinate progress and land in the gallery's `Exports`
  folder; place-as-take exports land in Root like takes.

## Gallery & overlay shortcuts

### Gallery (focused)

| Key | Action |
|---|---|
| Arrow keys | Move asset focus (inspector follows) |
| Space | Open the inspect overlay |
| Ctrl+A | Select all visible |
| S | Favorite / unfavorite |
| Delete | Trash (or permanently delete a trashed selection) |
| Esc | Clear or reduce selection |

### Inspect overlay

| Key | Action |
|---|---|
| ← / → | Video: step 1 frame · Image/Audio: previous / next asset |
| Shift+← / → | Video: step 10 frames |
| Ctrl+← / → | Video: step 1 second |
| ↑ / ↓ | Previous / next asset (compare: cycle the active side) |
| Space | Play / Pause |
| Right-drag video | Scrub from the current playhead |
| 1 / 2 / 3 / 0 | Audio compare monitor: A / B / Both / Mute |
| Shift (hold) | Temporarily flip the A/B monitor |
| C | Toggle Compare |
| S | Favorite / unfavorite |
| Delete | Move to Trash |
| F | Fit |
| + / − | Zoom |
| Wheel | Zoom (image/video) / waveform zoom (audio) |
| Esc | Close |
