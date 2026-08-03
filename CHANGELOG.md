# Changelog

All notable changes to **ComfyUI-Sonder-Editor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section is dated `YYYY-MM-DD`. Add new entries under `[Unreleased]`
as you work; on release, rename that heading to the new version + date and start
a fresh `[Unreleased]` block.

## [Unreleased]

### Added
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
  Reference Bridge now marks the outputs a recipe does not drive as unused and
  shows only as many `r01`–`r16` slots as the lane stages.
- Reference lane headers show the recipe in use, and item bars show member tags
  when there is room for them.

### Changed
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
