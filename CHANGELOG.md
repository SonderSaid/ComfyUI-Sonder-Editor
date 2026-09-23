# Changelog

All notable changes to **ComfyUI-Sonder-Editor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section is dated `YYYY-MM-DD`. Add new entries under `[Unreleased]`
as you work; on release, rename that heading to the new version + date and start
a fresh `[Unreleased]` block.

## [Unreleased]

**Project format change: a project saved by this version can no longer be opened by 0.5.0 or earlier.**
Projects move to storage format 3, which uses shorter full-hash filenames for durable components, and
nothing in the project is lost or altered. Format-2 projects migrate automatically on their next
version-bumping save. Format-1 projects keep their format until a history edit, then migrate on the
following save.

### Changed
- Linking and unlinking timeline items now updates group badges immediately, with canonical Undo baselines and recovery for failed queued edits.
- **⌇ Split Here** now acts only on the selection, and covers prompt sections as well as clips, audio and Reference items. It no longer falls back to cutting whatever happens to sit under the playhead when nothing is selected — it says so instead. Splitting several selected items at once is one undo step.
- Project route reads and saves run off the event loop, including the first automatic component migration.
- Project reads and saves now use their own worker threads, so a long automatic migration no longer delays thumbnails, media probes or uploads waiting behind it.
- Eligible Undo and Redo actions update the timeline after the history token arrives, before the restore finishes; failed predictions reconcile safely.
- Setting a scene's resolution, duration or lane options to the values they already have no longer saves the project. Previously every such edit rewrote the file and moved the project's version, which made other open editors refresh and could make a save in another tab fail and retry for no reason.
- When a save collides with another editor or a running render, the editor now retries only the edits it can safely repeat — those that name what they change by an identity the server checks. An edit that names a lane, a section or a position by its place in the list is refused and the timeline restored, instead of being replayed onto a document it was never written against, where it could land on the wrong lane.

### Removed
- The dedicated guide-swap route; swaps now use the guarded scene mutations endpoint.
- The standalone `DELETE` routes for clips, guides, prompt sections and audio tracks. These operations are owned by the scene mutations endpoint, which accepts an identity snapshot of the target that the old routes could not send, and which rewrites the link groups they left behind.

### Fixed
- The Prompt, Guides and Reference Lane panels now update as soon as Undo, Redo or another save changes what they show, and never while you are typing in them.
- The Prompt, Guides and Reference Lane panels no longer act on a different item after Undo. An action on something that has changed since the panel was drawn is refused with a message and changes nothing.
- Undo no longer gets stuck after locking several lanes in quick succession, or after toggling one setting several times while a save is running. Each lane's lock is its own Undo step, like hiding a lane, and repeated edits to one thing made while an earlier save is still running undo together in one step.
- Undo and Redo of a lane change (lock, rename, hide, recipe) no longer fail with "Scene changed elsewhere" after a timeline export or a generation placed a take on a new lane. The new lane and its take are kept.
- Renaming a lane and changing a Reference lane's recipe are now Undo steps. Previously either one made the next Undo of an earlier lane change fail with "Scene changed elsewhere" until the editor was reopened. Pressing Escape while renaming a lane no longer saves the new name.
- While a Prompt, Guides or Reference Lane panel is open, only Undo and Redo reach the timeline, so Delete, Space and other shortcuts can't edit the timeline hidden behind it. Escape closes the Guides panel instead of leaving fullscreen.
- Enter and Shift+Enter in the prompt editors now insert a line break exactly where the caret is. In the Writing draft they no longer lose the break, move the caret, or turn a prompt link into plain text. Deleting the last line's text no longer leaves an extra blank line.
- Fullscreen editor undo/redo stays in the editor after button clicks and cannot advance ComfyUI graph history, including editors inside subgraphs.
- Guide swaps now update immediately and support Undo/Redo; refused swaps preserve the previous Redo history and report the conflict.
- Sonder Cluster preserves its saved lane and branch counts when loading or switching workflows in newer ComfyUI frontends, and keeps existing connections attached to their named sockets when its layout changes.
- Dragging a second Library member onto a Reference item that was still saving no longer refuses it with an identity error and loses the drop. Staging now appears on the timeline immediately instead of waiting for the project to save, so the next drag is aimed at the bar that is really there: a drop onto it appends, where before it tried to create a second item in the same place and was refused for overlapping.
- Moving an item to a typed frame, moving it to a new lane, and consolidating a selection onto one lane now update the timeline immediately instead of waiting for the project to save. A failed move restores the timeline and says why; moving to a new lane previously neither repainted nor restored anything when it failed.
- An edit made to a clip or audio track that was just split, before the split finished saving, now applies to its whole linked group rather than to that piece alone. Previously the move, trim, delete or mute reached the server without the link, which applied it to one row, left its partner behind, and reported success.
- Splitting now applies to the timeline immediately instead of waiting for the project to save, so a second cut lands on the half the first one made. Cuts made in quick succession used to be aimed at the clip as it was before any of them, and did nothing at all — three rapid cuts on one clip now produce four pieces. A split that divided nothing says so; one that divided something no longer stays silent about a half it could not confirm.
- A split that cannot happen now says why. Aiming a cut outside an item, at a locked lane, at a Driver clip, or at nothing at all used to do nothing and show nothing; each of those now names what it refused. Splitting a selection where only some items cross the cut splits those and names the rest.
- Splitting a prompt section now refreshes the Prompt tool, which previously kept showing the sections as they were before the cut until it was refreshed by hand. This includes cutting a clip that is linked to a prompt section, where the section is split too.
- A split that is refused now says that none of its cuts were applied, when it covered more than one item. Every cut in one split is saved together, so one refused item stops them all; previously the message named only the item that was refused.
- Splitting two linked items that were both selected no longer sends two cuts for one group, where the second could not apply.
- A cut aimed at a clip, audio track or prompt section that no longer spans that frame is now refused and said so, instead of quietly doing nothing while still saving the whole project. This happened whenever a second cut was made before the first had finished saving.
- Rapid mute toggles on one selection no longer queue a full project save each; they collapse the way lane-header clicks now do. Repeated clip-role conversions on one clip collapse too, though that gesture is reached through a context menu and rarely repeats fast enough to matter.
- Rapid clicks on a lane header's hide control no longer queue one full project save each. A burst now costs one save per save already in flight, instead of one per click: six quick clicks were measured at 24.2 seconds of serialized saving, and a longer burst at over seven minutes. Clicks on different lanes are still saved separately, so each stays its own undo step.
- Staging a Reference item is refused, instead of being created shorter than it was drawn, when another item on that lane was deleted or moved while the write was on its way.
- Writing mode's Apply is refused, instead of silently discarding the change, when prompt sections were added, removed, reordered or retimed elsewhere first.
- Deleting a linked selection now honours the identity check it already carried. Previously that check was discarded whenever the selection was linked, so a prompt section or guide that had moved could be deleted in place of the one selected.
- Deleting a timeline lane is refused, instead of deleting a different lane, when that lane changed elsewhere while the delete was on its way. Adding a lane in another tab no longer interferes, because it moves nothing.
- Adding a guide frame, or dragging one onto an occupied frame, is refused instead of silently destroying what was there, when another editor put a different guide on that frame first. Replacing a guide you can see still works as before.
- Linking or unlinking timeline items now follows a prompt section or guide that an Undo moved while the action was still queued, instead of acting on whatever row inherited its position. A row the Undo removed outright is still sent as authored.
- A scene duration or resolution edit that fails while a second edit of the same kind is still queued now returns the timeline to the value it started from, instead of the intermediate value the first edit had shown.
- A failure to read the project's own stored data is now reported as a server error rather than a bad request, and no longer puts the server's folder path in the editor's error message, in a failed export, or in the saved project file. The cases that previously ended the request without a proper reply now answer with the editor's security headers and content policy like every other response.
- Version-conflict responses now carry the editor's security headers and content policy, which they previously shipped without.
- Emptying the trash, or permanently deleting assets, no longer removes the media before the change is saved. If the save cannot proceed, the files are put back and the assets stay in the trash; previously the media was already gone and the gallery was left pointing at missing files.
- Deleting or duplicating a scene, or deleting a saved selection, no longer discards a change made at the same moment elsewhere. All three settle against the newer version and still succeed, including while a render is writing to the project in the background. A saved-selection delete that can no longer identify the row it named — because the row itself changed, or because two rows are now identical — is refused rather than removing a different selection, and the editor says so instead of quietly restoring the row. When the connection drops before an answer arrives, the editor now says the outcome could not be confirmed and suggests reloading, rather than reporting a failure for work the server may already have saved.
- Finished timeline exports survive registration failures and late cancellation, with retained paths and gallery recovery guidance.
- Undo preserves existing out-of-bounds and overlapping timeline items while refusing new merge conflicts.
- Undo refusal messages identify the violated rule and affected timeline items, lanes, and frame ranges.

## [0.5.0] - 2026-09-14

Audio processing now has a minimum FFmpeg version. If exports or previews
start refusing audio after this upgrade, see **Troubleshooting** in the
README — a system `ffmpeg` on your `PATH` takes precedence over the binary
this pack bundles, and an older one must be updated or removed.

### Added
- Project-wide Keep/Drop for out-of-window Reference text, with independent per-chip overrides and visible markers.

### Changed
- Audio processing now requires FFmpeg 7.0 or newer. An older binary is refused by name rather than mixing incorrectly, and a system `ffmpeg` on `PATH` takes precedence over the bundled one, so an older system install must be updated or removed.
- The pack now requires `imageio-ffmpeg` 0.6.0 or newer, whose bundled ffmpeg meets the audio floor above; a fresh install no longer leaves an older bundled binary in place. `Pillow` is pinned to 9.1 or newer for the resampling API the thumbnailer already used, and `imageio`, which the pack never imported, is no longer installed.
- Playback auto-scroll advances by pages, keeping more upcoming timeline content visible.
- Video presets now set a keyframe interval so clips scrub and play smoothly: Editing Master MP4 is all-intra (every frame a keyframe; it scrubs instantly but files are roughly 2.3x larger), and Compatible/High Quality MP4 place a keyframe every 2 seconds.
- Fullscreen playback keeps a soft memory target for the video it holds in RAM, freeing idle media oldest-first. Media in use by the playhead or prebuffer is never dropped, so the figure can exceed the target on heavy scenes.
- Editing Master MP4 and audio-only exports now use 24-bit FLAC; ProRes uses 24-bit PCM and FFV1 uses 24-bit FLAC.
- The Editing Master MP4 preset description now names it the recommended round-trip master and warns that browser and OS preview are not guaranteed.
- Silent Editor output uses 48 kHz; Reference placeholders follow the highest live native rate, or 48 kHz when empty. Audio socket shapes and ordering remain unchanged.
- Stored audio volumes above 100% now render at 100%, matching timeline playback; stored project values remain intact.
- Non-winning Reference chips warn instead of refusing generation; deleted bindings still block. Legacy project-less replay now composes prompts for non-winning Reference chips.
- Boundary Prompt and Reference thresholds measure overlap relative to the shorter item/window span; short windows can retain neighbors previously dropped, including at the default prompt threshold of 10%.
- Frozen threshold values use the current coverage rule when resolved again, including legacy v0.2.2 prompt replay.
- The README and guides are corrected and expanded: the editor node's outputs, its three surfaces, project creation and a minimal text-to-video graph are documented, MiniMax H3 is listed among the built-in model templates, and out-of-window Reference chip text, lane reorder and the References section of Generating are covered for the first time. "Render window" is now "generation window" throughout, matching the toolbar.

### Fixed
- Reduce small-viewport playback draw cost with a bounded source-sized video scratch canvas.
- Reuse unchanged timeline layers during playback to reduce per-frame repaint work.
- Incoming pre-rolled clips target their source-frame timestamp so startup jitter does not advance the picture by one frame at a cut.
- Cancelled playback prefetches release their acquisition protection, allowing the memory target to reclaim them while preserving other consumers and immediate teardown cancellation.
- The Save Video preset description no longer paints over the widgets below it; the help box now reserves the height its text actually needs.
- Fullscreen playback pre-rolls eligible incoming clips to reduce boundary freezes; clips without source lead-in keep normal preparation.
- Passed playback clips release their media holders so the memory target can reclaim idle sources; scrubbing back under pressure may re-fetch them.
- Paused previews avoid fetching fully covered video layers and fall back when the covering media cannot resolve or cover the canvas.
- Playback memory presets retain their selected values, default to the named 1 GB preset, and hide the custom-unit suffix beside named presets.
- ProRes and Lossless FFV1 assets show a still frame and an explanation in the asset gallery instead of a black rectangle; this browser has no decoder for those codecs, and the files remain intact and export normally.
- Save Video and Preview retain valid video when supplied audio cannot be prepared, with visible alerts; failed take-audio placement rolls back its additions without discarding the video. Audio processing uses the configured temp volume and avoids unnecessary copies.
- Corrupt audio only blocks windows using that source; empty, muted and zero-volume windows export video without manufacturing silent audio tracks. RF64 float WAVs now read back correctly, and digital silence stays silent in integer delivery.
- Reference chip dialogs save and close when their existing source is inactive, including later linked copies.
- Audio exports retain track balance when clips end, including mono dialogue mixed with stereo music; editor audio and exports share sample-accurate float mixing, float sidecars, and reported constant headroom protection.
- References covering the entire generation window survive every threshold; high Boundary Prompt thresholds retain the stronger section instead of a neighboring sliver.
- Fixed false project conflicts during multi-file import and replacement, preserved committed replacement media when cleanup fails, and named failed imports in gallery notifications.
- Sonder Save Bridge accepts a typed or existing **Target Folder** again. Picking or typing any label other than Root previously failed the run during prompt validation; the label is now created when the outputs register.
- The README states the released version, and no longer suggests the pack may be missing from the Registry.

## [0.4.0] - 2026-09-08

Registry users upgrading from 0.2.2 also receive the features listed under
0.3.0 below. Version 0.3.0 was released through Git; its Registry publication
failed. This release includes those features and the changes below.

### Removed
- Removed MiniMax H3 Base task-mode and first/last Guide setup controls and automatic alignment prose; the Base prompt format and Guides Bridge remain available.

### Added
- Reference lanes can be reordered. **Move Lane Up** and **Move Lane Down** sit
  in the lane header menu; lane order numbers the MiniMax H3 Picture, Video and
  Audio ordinals, so the move renumbers them and says what else changes with
  them, including that a Reference Selector naming a lane by number now points
  at the other lane.
- Staged Reference items can be split, with the razor tool or **Split Here**,
  the way clips, audio and prompt sections already are. Both halves keep
  identical staging, and a half running to scene end keeps following it.
- Dragging a Library member onto an already-staged Reference bar adds it to that
  item instead of refusing. The drag highlight now also respects the lane's
  declared model input, so a still image no longer looks like a valid landing on
  a Video reference lane.
- The toolbar now pairs labeled Model and Channels template controls, with a project-wide channel picker and a shortcut to Manage Channel Templates.
- Added links to Project-Sample, the LTX 2.3 example project hosted on Hugging Face.
- Reference recipes carry a **Prompt suffix** beside the existing prefix and
  per-member token, so a model format whose reference block is closed by a
  second label stays one recipe-owned definition. **LTX IC-LoRA Ingredients**
  now derives `Reference sheet: … Generated video:`, rendered identically by the
  Prompt Bridge `reference_prompt` output and a Reference Context chip, and
  lanes created before this change pick the suffix up on open.

### Changed
- Updated the MiniMax H3 example sampling defaults, dependency notes, and setup guidance.
- Large-project saves keep prompt history, queued render snapshots and asset provenance in separate immutable components. History updates reuse unchanged entries, and background cleanup protects active readers.
- Older projects migrate on their first edit with an exact backup; that first edit has a one-time migration cost. Copy the whole project folder, including `state/`, when moving a project.
- Gallery lists stay small while metadata search and inspection load provenance on demand. Nodes, generated takes, exports and single-asset responses retain complete metadata.
- Simplified shortcut help: Undo and Redo show their actions, and Ctrl+V reads “Paste”.
- The Reference lane panel's derived-prompt row names the two exits that can
  carry the text — a Reference Context chip and the Prompt Bridge output — and
  says whether a chip is attached, rather than claiming the text reaches the
  model when by default neither exit is live.
- **Reference Prompting** in Prompt Management now lists the recipe-derived
  prompt of every staged Reference, with its window verdict, attached-chip
  count, and the prefix and suffix the recipe contributes. It previously showed
  only formats' declared physical populations, so on the default Generic format
  it reported that nothing existed while a staged Reference was contributing
  text to the prompt being compiled.

### Fixed
- Clarified per-member prompt patterns in the Reference guide and tooltip, directing MiniMax H3 numbering to Prompt Context.
- The Reference Library's right-click **Add to timeline** action now runs and its menu dismisses with Escape or an outside click.
- Reference lane configuration preserves lane identity and checks known lane IDs so edits cannot silently retarget a reordered lane.
- Gallery metadata loads correctly for absolute project directories and ignores stale callbacks after switching projects.
- Export refusals remain visible when controls re-enable; retry clears the previous request error.
- Improve editor responsiveness during timeline export polling and coalesce repeated Guides Bridge and Driver Selector refreshes.
- Media drops that add lanes now create those lanes, the clip or audio track,
  extracted audio, and their link in one project write instead of two.
- Undo now reverses successful video, audio, and Driver drops from their exact
  canonical post-state; a concurrent edit that makes lane removal unsafe is
  refused instead of silently stranding media.
- Reduced render-cache maintenance delays when opening the editor or viewing cache usage, with reliable deferred cleanup after rendering finishes.
- Undo and Redo now queue behind in-flight project writes in request order instead of discarding the action, with durable waiting feedback and execution-time graph-undo suppression.
- Redo no longer reverts work done elsewhere while an Undo was in flight. The
  reverse action is now measured against the state the Undo asked for rather
  than the scene the server happened to return, so a take committed by a render,
  or an edit made in another window, survives instead of being quietly undone.
- An edit made straight after an Undo now lands on the lane it was aimed at.
  Video, audio, Driver, and Reference destinations keep their selected index
  when only contents moved; actual lane additions/removals retarget uniquely
  identifiable lanes and refuse ambiguous targets with a lane-specific message.
- Failed changes no longer wedge Undo or Redo: unusable history is removed by
  exact entry, and a shortcut that encounters one skips only that action and
  explains that the next press reaches the next available change. Skipping
  removes only the history entry; it leaves the scene content unchanged.
- Dropping an image onto the timeline during a queued Undo now creates its guide
  in queue order rather than racing ahead of the restore.
- An Undo whose outcome the server can no longer confirm — a lost response for a
  change that never committed, followed by a restart — no longer blocks every
  later edit to that scene. The editor refreshes from the server, explains that
  the earlier action could not be confirmed, and carries on.

## [0.3.0] - 2026-09-01

### Added
- Added a project-durable **Reference Library**: named Reference entities, each
  holding one or more image, video, or audio members, with a one-line
  Description, media-aware built-in tags alongside free-text custom ones,
  per-member image and video crops, audio and video source trims, and a focused
  editor that previews the full source beside the applied result. A tag
  describing content is universal — **Motion Reference** — while one naming a
  provider input slot carries its family, as **MiniMax H3 · Motion** does; every
  surface from the Library to the live graph Selector resolves both from the same
  declared label. An **Assets | References** switch opens the Library in both the
  fullscreen and mounted editors. Asset **Where Used**, Trash warnings, and
  permanent-deletion results now name Library memberships and identify which
  edges a deletion removes.
- Added scene-durable **Reference lanes**. Drop a Reference onto the timeline —
  including onto the ruler, which creates a lane the way clips and audio already
  do — and it occupies a range you can move, trim, hide, mute, or lock. A lane's
  ☰ icon opens a setup overlay that leads with the staged items, marks which one
  actually reaches the model for the current window, and shows the derived
  prompt so it can be read and copied before you override it. Lane headers name
  the recipe in use.
- Reference lanes assemble their members through **recipes**: segmented subject
  sequences, looped panel sheets, and equal-width strips, with built-in
  templates that fork into editable project recipes. A recipe owns output size
  (scene, native, or custom) plus a snap-to multiple, its frame grid, member cap,
  and prompt convention — and its grid, snap multiple, and sheet loop length can
  be pegged to the scene's model template or the render window instead of typed
  once. Reference items also author conditioning strength and temporal sequence
  length; video members serve their trimmed spans with streaming decode and
  resampling.
- Reference prompts take a per-member pattern with `{n}`, `{index}`, `{prompt}`
  and `{name}` placeholders, usable more than once, so a recipe can compose
  `<Subject 1> is a redhead woman, from <Picture 1>`. `{subject_n}`,
  `{picture_n}`, `{audio_n}` and `{speaker_n}` number the same entity
  identically on every lane, so an image lane and an audio lane staging one
  character agree.
- Added a project-wide **Reference Threshold**: a staged Reference is ignored
  for a render window that covers too little of its own span. Reference items
  show what a render will do with them — the item the current window sends to
  the model takes an accent bar, while one another item supersedes, or one the
  Threshold drops, is hatched with the reason. Queueing a batch warns when this
  changes whether a lane resolves between chunks, naming the lane, the number of
  chunks affected, and the remedy that applies.
- Added guarded Nodes 2.0 **Sonder Reference Selector**, **Sonder Reference
  Image Bridge**, **Sonder Reference Audio Bridge**, and **Sonder Reference
  Prompt Bridge** nodes. The Selector combines several compatible lanes into one
  stable, lane-ordered Reference set, with a lane dropdown, a status line, the
  staged tags, and hover descriptions on every socket. The bridges are
  type-homogeneous and their numbered blocks grow only to the staged or
  connected-slot ceiling; outputs a recipe does not drive are marked unused on
  both renderers, unstaged slots emit nothing, and a persistent canvas advisory
  appears when that would starve a required input.
- Added project-durable **Context chips** and canonical prompt documents across
  timeline prompt bars and the Prompt tool's Structured and Writing modes. Shot,
  Reference, Vocal Event, Prompt Link, and declarative Custom capabilities
  resolve from stable ids and the effective render window. A prompt section can
  open a new Shot and optionally stamp its cut time, relative to the render
  window, so the same Shot reads correctly whether you render the whole scene or
  one slice of it. Prompt text can cite a shot rather than hard-code its
  number — `@shot(...)` compiles to `[Shot 1]` and follows the marker when an
  earlier shot is added.
- Added immutable **Prompt Context profiles** (`generic@1`, MiniMax H3 Base, and
  MiniMax H3 Full Reference) with project-scoped semantic identities, exact live
  compile diagnostics, and enqueue-frozen prompts, Relay payloads, and setup
  manifests. A prompt format declares what a Reference contributes — its name,
  destination, placement, guidance, and bounded choices — and the editor reads
  those declarations rather than carrying its own copy of a provider's
  vocabulary, so switching formats changes the labels, help, and choices you
  see.
- Custom prompt formats can be authored in full: Reference prompt parts,
  physical populations, identity kinds, roles, contributions, and speaker
  policy, each behind its own disclosure. Formats can be forked, edited, and
  deleted; a format that cannot be deleted is dimmed and names the scenes,
  channel template, or Reference recipe still using it. A format can only be
  chosen alongside a channel template it targets, so pairing MiniMax H3 (full
  reference) with Standard channels is shown disabled rather than silently
  claiming a format whose requirements the channels cannot carry.
- Prompt formats declare **writing aids** — fixed syntax the editor inserts for
  you, with no model call involved. An aid with nothing left to decide inserts
  straight away, one with a single choice opens a submenu, and one needing
  several opens a small panel; each menu row previews the text it will insert.
  Select a line first and the aid wraps it: highlight a spoken line, pick
  Dialogue, and it comes back in the format's own syntax. Aids are offered only
  in the channels their format declares them for, so a soundscape or retention
  field does not list dialogue and camera aids, and custom formats can edit an
  existing aid's choices and channels rather than only setting them at creation.
  MiniMax H3's camera aid follows H3's own documented grammar — motion type plus
  optional amplitude and speed, across all twenty documented moves — while shot
  distance, composition, depth of field, and field of view are available to
  every format.
- Physical References carry the same prompt defaults an Identity does, so a
  Reference attached anywhere follows its own prompt text, preservation detail,
  summary, and handling instead of needing every field retyped on every chip.
  Whether a Reference contributes a prompt part at all is set once on the
  Reference; where that part lands stays per-attachment, and a chip stores only
  the fields you actually overrode.
- Added project-unique **`@handles`**. Typing `@KWoman` in any prompt field
  compiles to the active format's label and is marked live so you can see it
  resolve. Because it is ordinary text it survives copy, cut, and paste, and
  prose can name a handle that does not exist yet, wiring itself up when you
  create it. `@` completion is offered in every prompt field. An `@` that is not
  one of your handles — an email address included — is left exactly as written.
- **Writing mode** now shows what each attached Reference contributes, as prose,
  under the channel it is staged in, so you can read the sentence your
  References will produce beside the sentence you are writing. That includes
  channels you have not written in yet, which is where most of a Reference's
  output usually goes; a capability that routes to a channel but resolves to
  nothing says so, and says which one. A contribution can be copied out as text
  you own, using live `@handles` wherever possible.
- Writing mode now speaks channels. A line like `summary:` starts that channel
  and `---` still splits sections, so a whole model output can be pasted in and
  arranges itself. Previously every word in a draft landed in the Visual
  channel.
- Prompt channels are now a project-wide **template** rather than a fixed three.
  Pick one in Settings > Prompts: **Standard** (one plain channel and the
  new-project default), **Visual + Speech + Sound** (the previous three), or
  **MiniMax H3** in base and full-reference form. Each channel carries its own
  authoring guidance, and each template owns whether its field names are
  written. Switching asks first and says what it is about to rewrite, across
  every scene — text in channels the new template does not use is collapsed into
  the first channel under its old channel name, visible and movable, and
  switching back puts it where it was.
- Channel templates have their own Settings catalog. Built-ins are read-only and
  offer **Save as Custom**; custom templates persist independently of the project
  using them and support new, copy, edit, delete, and default-for-new-project
  actions, covering channel keys and headers, guidance, shot-marker placement,
  field-name policy, separators, and scene-global mode.
- The scene-global prompt is per channel too, so a style opening can sit at the
  head of the description rather than ahead of the whole prompt. Each prompt
  section chooses which global channels it takes; a global channel is written
  once, and only if some section in the render actually takes it. Templates that
  turn per-channel globals off get a single global box written ahead of
  everything, and switching either way keeps the text.
- **MiniMax H3 (full reference)** carries H3's six reference-bearing sections and
  is driven by Reference lanes. Set a Reference lane to Pictures, Videos, or
  Audio and drop a Reference in, and it feeds the model — there is no separate
  step that registers the lane. Lane order decides which is Picture 1; to leave
  one out, hide the lane or mute the item. Picture, Video, and standalone Audio
  populations flow through the generic Reference Selector and media-typed
  bridges, and video is served as a 24 fps IMAGE sequence on the `17n+5` frame
  grid.
- **MiniMax H3** (base) carries the three core fields for text- and
  keyframe-driven generation, with a setup authored in the Prompt tool: pick the
  task mode — T2VA, I2VA, FL2VA, L2VA — and bind first- and last-frame Guide
  anchors. **Treat this setup as provisional: it is superseded and will change.**
  Placing a Guide on the timeline and driving ComfyUI's own Add Guide for MiniMax
  H3 node from the Guides Bridge reaches the same first- and last-frame result
  far more directly, and is the route to prefer. Do not build on the anchor
  binding.
- The **Masks Bridge** can now compile the generation window straight into hard
  video and audio latent noise masks. Wire each half of a separated AV latent
  together with its VAE, and feed the two new mask outputs to **Set Latent Noise
  Mask** — no retyped frame rate, no seconds round-trip, and the context frames
  outside the window are kept exactly. Geometry is read from the VAE, so it is
  correct on LTX, MiniMax H3, and Wan with no per-model setting. On LTX, a graph
  that also uses guides or a start image should drive kjnodes'
  `LTXVAudioVideoMask` from the four time outputs instead. Note that a mask
  replaces rather than composes, so it overwrites any pin a start-image or
  continuation node set upstream.
- Added a second example workflow, **Sonder MiniMax H3 References**: a
  ready-wired graph in which Reference lanes reach MiniMax H3 through the
  Reference Selector and the Image and Audio bridges, keyframes arrive as first-
  and last-frame guides through the Guides Bridge, and the Masks Bridge writes
  latent noise masks so part of a window can be regenerated in place. Both
  example workflows are linked from the README.

### Changed
- **Visual + Speech + Sound now always emits `[VISUAL]:`, `[SPEECH]:`, and
  `[SOUNDS]:` field names, including for existing projects whose Channel Labels
  toggle was off.** The project-level toggle has been removed. Standard remains
  unlabelled, both MiniMax templates remain labelled, and queued jobs replay
  their frozen setting. Prompts from affected projects will compile differently
  than they did in 0.2.2.
- New projects start with **Standard** channels — one plain field — rather than
  the three Visual/Speech/Sound channels. Existing projects are unchanged.
- Timeline prompt bars now show one remembered active channel with indicators
  for the non-empty channels that are hidden, rather than every channel's
  textarea at once, while retaining all channel editors, carets, undo state, and
  one host-owned attachment registry.
- **Prompt Relay** and live Editor execution now compile through the same
  project-aware Prompt Context compiler used by preview and enqueue, with
  demand-gated blocking diagnostics instead of silent blank prompts.
- New queue jobs carry an explicit `prompt_context_v1` envelope with complete
  Prompt Context, template, and Reference freezes, including the resolved
  channel template and its label policy, so later preset or catalog edits cannot
  rewrite pending work. Jobs queued by 0.2.2 replay through an isolated
  frozen-only composer and keep their original behavior.

### Fixed
- Generated video no longer starts on wrong content when the render window has
  to be rounded up to the model's frame rule. The rounded-up tail was filled
  with digital silence, and generation frequently keeps those filler frames
  rather than regenerating them — always when a channel is frozen, and whenever
  the window carries post-context — so the silence reached the model as if it
  were real recording. The tail now mirrors the end of the window's own audio.
- An unapplied writing draft could be discarded without warning. Applying a
  draft left behind an emptied record stamped with the current time, and those
  records competed for the same forty slots as real ones — so enough applied
  scenes would silently evict the one draft still holding unwritten work. The
  emptied records are now dropped instead of hoarding a slot.
- Writing drafts no longer overwrite each other between editor windows. Saving
  or applying a draft rewrote the whole set of drafts from whatever snapshot
  that window last read, reverting any draft another window had changed since;
  each draft is now written on its own.
- **Reset from sections** in Writing mode no longer throws your draft away. It
  keeps a copy first and offers **Restore draft**, so the one control available
  when Apply is locked is no longer the one that destroys unapplied work. The
  copy survives a browser reload and is dropped once you Apply.
- Scene Undo and Redo now reverse only their own acknowledged edit through a
  conflict-checked three-way merge, preserving concurrent generated takes and
  other unrelated work while refusing unverifiable or genuinely conflicting
  history.
- Large projects no longer stall on save and conflict handling. Project-version
  conflicts return only the fields needed to heal instead of serializing every
  scene and asset, and project saves reuse one exact pretty-JSON serialization
  for both disk and compatibility state while cleaning up failed temporary
  writes.
- Prompt fields no longer leak keystrokes into the timeline or the graph. Space,
  arrows, deletion, and undo were stopped only after LiteGraph's own
  document-level handlers had already seen them, so typing in a prompt could
  scrub playback or undo a graph edit. The guard also covers ComfyUI builds
  whose undo capture runs before extension listeners.
- Enter, Escape, and Backspace pressed while an IME candidate window is open now
  reach the IME instead of committing or discarding the prompt.
- Pasting into the fullscreen editor background no longer reaches the hidden
  graph behind it. Fullscreen now owns background paste through the shared
  keyboard registry while preserving native paste inside prompt fields.
- The external-links setting no longer shows a garbled ellipsis while its server
  value loads.

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
  Prompt Relay, Guides, and IC-LoRA motion transfer — from a showcase project.

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
- Project documentation and an example LTX 2.3 workflow.

### Notes
- Requires ComfyUI and `ffmpeg` (see README). `torch`/`torchaudio` are provided
  by ComfyUI and are intentionally excluded from this pack's requirements.
