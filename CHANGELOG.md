# Changelog

All notable changes to **ComfyUI-Sonder-Editor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section is dated `YYYY-MM-DD`. Add new entries under `[Unreleased]`
as you work; on release, rename that heading to the new version + date and start
a fresh `[Unreleased]` block.

## [Unreleased]

### Added
- Added links to Project-Sample, the LTX 2.3 example project hosted on Hugging Face.
- Reference recipes carry a **Prompt suffix** beside the existing prefix and
  per-member token, so a model format whose reference block is closed by a
  second label stays one recipe-owned definition. **LTX IC-LoRA Ingredients**
  now derives `Reference sheet: … Generated video:`, rendered identically by the
  Prompt Bridge `reference_prompt` output and a Reference Context chip, and
  lanes created before this change pick the suffix up on open.

### Changed
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
