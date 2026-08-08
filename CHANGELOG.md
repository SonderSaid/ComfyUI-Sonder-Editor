# Changelog

All notable changes to **ComfyUI-Sonder-Editor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section is dated `YYYY-MM-DD`. Add new entries under `[Unreleased]`
as you work; on release, rename that heading to the new version + date and start
a fresh `[Unreleased]` block.

## [Unreleased]

### Added
- Replaced the mixed 39-output Reference Bridge with separate, type-homogeneous
  Image, Audio, and Prompt bridges. Their numbered blocks grow only to the
  staged or connected-slot ceiling, keeping links index-safe while removing the
  unusable wall of empty sockets.
- Reference items now author conditioning strength and temporal sequence length.
  Recipes can bound the short image edge and choose native, scene, or custom
  reference frame rate; video members serve their trimmed spans with streaming
  decode and resampling, and audio recipes can expose multiple members.
- Existing Reference recipes migrate the retired Bridge liveness vocabulary in
  scene copies, frozen jobs, project custom recipes, and built-in presets.
- Prompt channels are now a project-wide template rather than a fixed three.
  Pick one in Settings > Prompts: **Standard** (one plain channel and the
  new-project default), **Visual + Speech + Sound** (the previous three), or **MiniMax H3** in
  base and full-reference form. Each channel carries its own authoring guidance,
  and each template owns whether its field names are written. Switching asks
  first and says what it is about to rewrite, across every scene.
- Channel templates now have their own Settings catalog. Built-ins are read-only
  and offer **Save as Custom**; custom templates persist independently of the
  project using them and support explicit new, copy, edit, delete, and default-
  for-new-project actions. The editor covers channel keys and headers, guidance,
  shot-marker placement, field-name policy, separators, and scene-global mode.
  Channel-key changes rewrite text through the same guarded project transaction
  as a template switch.
- Prompt sections can open a new shot and stamp a cut time as fully independent
  choices — either, both or neither. A section can read `At 00:07.000, he takes
  out a gun` while continuing the same shot, and the first section is no longer
  forced to open one. Timestamps are relative to the render window, so the same
  section reads correctly whether you render the whole scene or one slice of it.
- The scene-global prompt is per channel too, so a style opening can sit at the
  head of the description rather than ahead of the whole prompt. Each prompt
  section chooses which global channels it takes; a global channel is written
  once, and only if some section in the render actually takes it. Templates that
  turn per-channel globals off get a single global box instead, written ahead of
  everything — and switching either way keeps the text.
- The writing tool speaks channels. A line like `summary:` starts that channel
  and `---` still splits sections, so a whole MiniMax-format model output can be
  pasted in and arranges itself.
- Switching channel templates now rewrites your text instead of hiding it. Text
  in channels the new template does not use is collapsed into the first channel
  under its old channel name, visible and movable; switching back puts it where
  it was. The same applies when a saved prompt template was written under a
  different channel set.
- Library entries gained a one-line **Description** so a crowded Reference
  Library stays readable when the name alone is not enough. It is project-only
  and never enters a prompt — per-member prompt text is what reaches the model.
- Reference prompt patterns gained `{subject_n}`, `{picture_n}`, `{audio_n}`
  and `{speaker_n}`, which number the same entity identically on every lane —
  an image lane and an audio lane staging one character agree. `{n}` keeps its
  existing per-lane meaning.
- Added scene-durable Reference lanes and scoped Reference items with drag/drop,
  range editing, hide/mute/lock, undoable exact mutations, hard media-kind
  enforcement, built-in and project-custom assembly recipes, and queue freezes.
- Added guarded Nodes 2.0 **Sonder Reference Selector** and **Sonder Reference
  Bridge** nodes with lazy effective-window presence, per-recipe image/audio
  assembly (segmented subject sequences, looped panel sheets, equal-width
  strips), a per-recipe output liveness map, fixed `r01`–`r16` workflow sockets
  that follow the staged member count, and loud staged-media failures.
- Added a project-durable Reference Library with conflict-safe atomic editing,
  hybrid built-in/custom member tags, image/video crops, audio/video source trims, and an
  **Assets | References** authoring switch in fullscreen and mounted editors.
- Reference authoring now includes media-aware preset filtering, video members,
  non-committing picker inspection, richer cards and Manage mode, plus a focused
  visual crop/trim editor with full-source and applied-result preview.
- Asset **Where Used**, Trash warnings, and permanent-deletion results now
  include Reference Library memberships and identify which edges are removed.
- Reference lanes now open a full setup overlay from the ☰ icon: the recipe's
  assembly, geometry, frame grid, member cap, prompt convention and live Bridge
  outputs are all shown and explained, built-in templates fork into editable
  project recipes, and each staged item lists its Library members with a
  searchable picker and reorder controls.
- The Reference lane overlay leads with the staged items, marks which one
  actually reaches the model for the current window, shows the derived prompt so
  it can be read and copied before overriding it, and completes suggested member
  tags on Tab.
- Reference recipe geometry is now Output size (scene, native or custom) plus a
  snap-to multiple that can be copied from the scene's model template.
- Sonder Reference Selector gained a lane dropdown with a status line in place
  of a bare index, every Reference socket gained a hover description, and Sonder
  Reference Bridge marks every output a recipe does not drive — and every
  numbered slot past the staged member count — as unused.
- Reference lane headers show the recipe in use, and item bars show member tags
  when there is room for them.
- Reference recipe options now explain themselves: choosing an Assembly or a
  Bridge output says what that choice does and what an unchecked output emits.
- Reference recipe frame grid, snap multiple and sheet loop length can be pegged
  to the scene's model template or to the render window instead of being typed
  once, so they follow whatever model the scene uses.
- Reference prompts take a per-member pattern with `{n}`, `{index}`, `{prompt}`
  and `{name}` placeholders, usable more than once, so a recipe can compose
  `<Subject 1> is a redhead woman, from <Picture 1>`. Each member's expansion is
  also emitted on its own `p01`–`p16` Bridge output.
- Added a project-wide Reference Threshold: a staged Reference is ignored for a
  render window that covers too little of its own span. Queueing a batch warns
  when this changes whether a lane resolves between chunks.
- Sonder Reference Selector lists the tags of the references staged on the
  selected lane.
- Reference items now show what a render will do with them: the item the current
  window sends to the model takes an accent bar, while one another item
  supersedes, or one the Reference Threshold drops, is hatched with the reason.
  The lane panel uses the same wording, and nothing is marked without a
  selection.

### Removed
- Reference Library entries no longer carry **Notes**. Description replaced it —
  the two overlapped, and neither had shipped. Existing note text is dropped.

### Fixed

- **Apply in the prompt writing tool no longer erases the scene-global prompt.**
  It rewrote sections correctly but handed the global text back through the flat
  legacy field, which clears every channel past the first — so on a MiniMax or
  custom channel set the whole global prompt vanished, and on the default set
  the three channels were flattened into the first. Globals authored before this
  fix and lost to it cannot be recovered.
- The scene-global lane on the timeline shows its text again. The lane bar and
  the hover preview read a legacy three-channel mirror, so any other channel set
  drew "Global prompt (empty)" over text plainly visible in the inline editor.
  The hover preview now lists the global text per channel, the way it already
  did for prompt sections.
- Reference Bridge outputs a recipe does not drive now read `(unused)` even when
  something is plugged into them. `reference_frames` is wired in almost every
  workflow, so it was the one output that could never show the mark — while
  quietly feeding a black-frame fallback down that link.
- Batching a scene with a Reference staged over part of the range no longer
  reports it as a problem. The warning could not tell "this chunk is outside the
  range you scoped" from "the Reference Threshold dropped a chunk this item does
  cover", and blamed the threshold for both. Both are still announced, because
  either one changes which sockets are wired mid-batch, but they now say which
  happened and what to do about it — including when the item is simply muted or
  its lane hidden.
- A saved prompt template now comes back with all its text. A template saved
  under a six-field MiniMax project used to reload with its section ranges
  intact and every channel blank, because the browser-local settings normalizer
  only knew the three original channel names. Templates saved before this fix
  were emptied at rest and need re-saving.
- Custom channel templates remain available after switching to a preset. Their
  definitions now live in a browser catalog instead of only in the active
  project's metadata, while projects keep an independent copy so catalog edits
  never propagate silently.
- Prompt history and browser prompt templates retain the full source channel
  template and compare channel-key sets when applied. Custom ids no longer fall
  through to the default, and same-structure MiniMax text is not needlessly
  collapsed and reparsed.
- Prompt boxes in the Prompt panel obey their drag-resize again, and the inline
  prompt bar on the timeline no longer clips its lower half when a template with
  many channels wraps it onto a second row.
- Opening the inline prompt bar under a MiniMax template no longer throws while
  trying to focus a channel that template does not have.
- A queued job now freezes the complete resolved channel template, including
  label policy. Later preset or custom-catalog edits cannot rewrite pending work,
  while legacy jobs carrying a bare preset id plus the old label toggle retain
  their original behavior.
- A Reference member with a prompt pattern but no prompt text no longer renders
  a dangling clause like `<Subject 2> is the  from <Picture 2>`; it falls back
  to the Reference's name rather than vanishing while its image still reaches
  the model.
- Deleting a Reference now also clears it from any prompt section that named it,
  across every scene.
- Reference item-bar tags are legible again.
- A pegged Reference recipe value now displays what the render will use instead
  of the number it replaced, read-only, with its source beside it.
- Reference batch warnings now name the lane, how many chunks were affected, and
  the remedy that actually applies.
- Sonder Reference Bridge `p01`-`p16` deliver their prompt text instead of image
  data. Trimming the `r01`-`r16` block shifted the prompt sockets into image
  positions; no Reference Bridge socket is removed any more, only marked.
- Reference Bridge outputs a recipe does not drive now read as unused on Nodes
  2.0 as well as legacy LiteGraph.

### Changed
- New projects start with **Standard** channels and **No Model Template**, a
  model-agnostic one-field baseline. Existing projects and stored browser
  defaults are unchanged.
- **Visual + Speech + Sound now always emits `[VISUAL]:`, `[SPEECH]:`, and
  `[SOUNDS]:` field names, including for released projects whose old Channel
  Labels toggle was off.** The project-level toggle has been removed; Standard
  remains unlabelled, both MiniMax templates remain labelled, and legacy queued
  jobs replay their frozen setting.
- Reference crop/trim preview now remembers Full Source versus Applied Result,
  uses the shared gallery seek bar for audio and video, and makes audio
  waveform-first with movable trim ranges and visible playback lines.
- Reference image/video cropping now supports the timeline aspect presets plus
  Free and user-entered Custom ratio locking.

### Fixed
- Reference source inspection now resolves the requested image, audio, or video
  independently of the Asset Gallery's active filters and last media type.
- Focused Reference overlays now own Space, Escape, and geometry shortcuts
  through the editor keyboard registry, open on a neutral focus target, restore
  crop/trim focus on pointer interaction, suppress the neutral shell's visual
  focus ring, and suppress the browser context menu.
- Reference field help no longer displays a duplicate browser-native tooltip.
- The external-links setting no longer shows a garbled ellipsis while its
  server value loads.

## [0.2.2] - 2026-08-13

### Fixed
- Saved videos keep every rendered frame when the workflow also supplies audio.
  Generated audio is often a fraction of a second shorter than the video, and
  ffmpeg was ending the file at the audio instead — dropping the last frame or
  two. Chained renders then read that missing frame as black at the start of
  each new segment, which showed up as a hard seam between clips.

## [0.2.1] - 2026-08-03

### Added
- Added a **MiniMax H3** model template: 24 fps, multiple-of-32 dimensions, and
  `17k+5` frame counts.

## [0.2.0] - 2026-08-01

### Added
- Recent ComfyUI builds now expose the native V3 Metadata Collector with
  heterogeneous Autogrow inputs while retaining the established executable V1
  node.

### Changed
- Metadata Collector naming now keeps the established V1 node as **Sonder
  Metadata Collector** and labels the native V3 Autogrow entry **Sonder
  Metadata Collector Nodes 2.0**.
- Prompt text now saves when a field loses focus, so clicking away from a
  timeline prompt bar keeps the edit instead of requiring Enter. Esc, Cancel,
  and Delete remain the ways to throw an edit away.

### Fixed
- Save Bridge now publishes generated files durably while retaining native Save
  Image staging previews for a bounded 60-second grace period, preventing
  finalization-time `/api/view` 404s without delaying asset registration.
- Sonder Editor now uses one renderer-stable DOM surface for both project
  creation and the dormant UI, so Nodes 2.0 can show the Create action and
  shrink scrollable Assets, Preview, and Queue modules to the saved node size.
- Save Video and Preview Video graph players now follow their live host-node
  width in both renderers instead of retaining their creation width.
- Sonder Editor now cold-loads its saved project UI under Nodes 2.0 by waiting
  for workflow widget restoration before committing visibility and node size.
- Gallery-to-canvas drops now register nested input paths with ComfyUI's loader
  combo so Nodes 2.0 displays them as imported assets, and videos fall back to
  the core Load Video node when Video Helper Suite is unavailable.
- Post-save asset refreshes now coalesce per browser window and use read-only
  registry reads, avoiding duplicate synchronization scans and summary-request
  abort churn while preserving explicit discovery and repair.

## [0.1.1] - 2026-07-27

### Added
- Added shared frontend project-source resolution for direct wires, typed
  pass-through/reroute chains, and current KJNodes Set/Get scopes.
- README now carries a 90-second overview video and three technique clips —
  Prompt Relay, Guides, and IC-LoRA motion transfer — from the sample project.

### Changed
- Rewrote the Registry and Manager listing description in plain language that
  names what the editor does, replacing the "NLE-style" framing.

### Fixed
- Sonder Editor now skips unused timeline-frame, direct-guide, and audio
  materialization while preserving project, queue, bridge, and metadata
  context.
- Guide **Set Selection In/Out** actions now use the same two-stage manual
  endpoint workflow as toolbar and shortcut entry.
- Animatic video suppression is now ephemeral and no longer overwrites durable
  lane visibility when the mode is toggled or a scene changes.
- Save Bridge restores its serialized target-folder field after workflow load
  without invoking the widget callback or dirtying the graph.
- Bridge asset-arrival notices now compare asset identities per execution
  settlement instead of relying on total counts.

## [0.1.0] - 2026-07-22

### Added
- Initial public release of **Sonder Editor**.
- Project documentation, an example LTX 2.3 workflow, and a downloadable
  showcase project.

### Notes
- Requires ComfyUI and `ffmpeg` (see README). `torch`/`torchaudio` are provided
  by ComfyUI and are intentionally excluded from this pack's requirements.
