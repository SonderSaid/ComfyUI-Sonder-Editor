# Changelog

All notable changes to **ComfyUI-Sonder-Editor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section is dated `YYYY-MM-DD`. Add new entries under `[Unreleased]`
as you work; on release, rename that heading to the new version + date and start
a fresh `[Unreleased]` block.

## [Unreleased]

### Added
- A Reference contribution can now be copied out of Writing mode as text you own. Copy uses live `@handles` wherever possible; when a staged marker or ordinal has no live spelling, it copies the rendered text and says whether it merely stopped following intent or froze a number staging can change. Pasting a provider ordinal into authored prompt text raises a non-blocking warning that says whether the number currently resolves. Each supported contribution row opens a small menu with Copy and a way to stop that chip contributing there; unsupported Custom and Vocal Event rows no longer offer a Copy that cannot work. The block itself lost its buttons: every field your References feed now shows its own heading in the order the format lists them, with its contributions underneath, and you can type in any of them.
- Reference mentions are now ordinary text. Typing `@KWoman` in any prompt field compiles to that format's label, and the editor marks it so you can see it is live. Because it is text it survives copy, cut and paste — previously a copied mention came back as dead words with its Reference silently dropped — and prose can name handles that do not exist yet, wiring itself up when you create them. `@` completion is now offered in every prompt field rather than only the Writing draft. An `@` that is not one of your handles, an email address included, is left exactly as you wrote it.
- The Masks Bridge can now compile the generation window straight into hard video and audio latent noise masks. Wire each half of a separated AV latent together with its VAE, and feed the two new mask outputs to **Set Latent Noise Mask** — no retyped frame rate, no seconds round-trip, and the context frames outside the window are kept exactly. Geometry is read from the VAE, so it is correct on LTX, MiniMax H3 and Wan with no per-model setting. On LTX, a graph that also uses guides or a start image should drive kjnodes' `LTXVAudioVideoMask` from the four time outputs instead of feeding the masks to **Set Latent Noise Mask** — the generating guide has the wiring. Note also that a mask replaces rather than composes, so it overwrites any pin a start-image or continuation node set upstream.

### Changed
- Prompt sections outside the current render window no longer look empty. Select one section on the timeline and the others still show what their References contribute, marked once as outside the window and numbered across the whole scene — which can differ from what that section will actually render. A section that contributes nothing for another reason now says which: muted, or clipped at the window edge and dropped by the boundary threshold.
- The Context strip under every prompt section reads as one thing again. It names what it belongs to, keeps one **+ Attach** in a fixed position before its chips, and shows compact handles while the full identity and preview remain on hover. Its menu offers the chip kinds and lets you reuse one you already have. Everything you could do to a chip — configure it, unlink it, remove it, or see how many sections use it — now lives in a menu on the chip itself, reachable by mouse or keyboard, and a seventh chip is no longer hidden behind a count you could not open.
- Prompt previews no longer blink or disturb the caret while you type: ordinary prose edits leave diagnostics, inline projections, and Writing contribution blocks in place, while the Compiled tab still follows each landed compile.
- Scene-wide Context now claims shared Reference output before section Context, so its position and winning text no longer change with the timeline selection; a disagreeing section override is reported on the section chip the author can change.
- Unstored handle suggestions no longer masquerade as live `@handles`. Physical rows use a placeholder and identities say **no handle** until the author saves or attaches them; attaching materializes the handle as part of the same undoable transaction.
- Writing aids now ask for their choices in the menu itself instead of a browser popup: an aid with nothing left to decide inserts straight away, one with a single choice opens a submenu, and one needing several opens a small panel. Select a line first and an aid wraps it — highlight a spoken line, pick Dialogue, and it comes back in the format's own syntax.
- Writing aids are offered only in the channels their prompt format declares them for, so a soundscape or retention field no longer lists dialogue and camera aids, and each menu row previews the text it inserts.
- MiniMax H3 camera motion now follows H3's own documented grammar — motion type plus optional amplitude and speed, across all twenty documented moves — instead of inheriting the shorter generic list. Shot distance, composition, depth of field and field of view are available to every format.
- A Reference or prompt identity can now be mentioned straight from the right-click menu by picking its handle: it lands as an inline mention in the sentence you are writing — `@KWoman is leaning then @Doggo appears barking`, each resolving to the format's own label — using the defaults it already carries. Sources that cannot be attached here stay visible with the reason, and the full configuration dialog is one row away and still opens from the chip.
- Prompt Link channels and Vocal Event subjects are now checkboxes with All/None rather than a list needing ctrl-click, and a section-scope Prompt Link can have individual channels switched off instead of only the whole chip.
- Custom prompt formats can now edit an existing writing aid's choices and channels, not only set them when it is first created, and an aid whose placeholder has no choices says so instead of silently producing a format that cannot be saved.
- The Context chip is now offered only by prompt formats that declare one, so it no longer appears under formats that have no use for it while staying available to any format that does.
- Physical References now carry the same prompt defaults an Identity does, so a Reference attached anywhere follows its own prompt text, preservation detail, summary and handling instead of needing every field retyped on every chip. Whether a Reference contributes a prompt part at all is now set once on the Reference, while where that part lands stays per-attachment.
- Attaching a Reference now configures it in the same dialog rather than only asking where to put it, and the chip editor shows the fields you have actually overridden with the rest collapsed behind a line naming what they follow. Both surfaces say which level of defaults they edit.
- Reference field labels, help and availability now come from the prompt format's own declarations, so a format that declares no preservation or summary no longer shows those fields, and no MiniMax wording appears under an unrelated format.
- Staged Reference rows read their declared role names and say when nothing in the prompt refers to a staged Reference yet. Handle suggestions are short enough to type, follow the Name field while you write it, and are corrected as you type instead of only on refusal.
- A staged Reference row now reads `Role: <name> · <slot> · 1 prompt identity`, so a role that happens to be called "Identity" no longer contradicts the identity count beside it, and the row no longer stays permanently tinted when a Style Reference legitimately has no identity. Creating one is a labelled **+ Identity** button that fits on one line.
- The collapsed override summary now names the most specific default a chip is following instead of counting how many sources disagree.
- Prompt tool colour, spacing and buttons now come from the shared editor theme, so its panels match the rest of the editor and disabled controls in it look disabled instead of silently doing nothing.
- A prompt format now declares what a Reference contributes — its name, destination, placement, guidance, and bounded choices — and the editor reads those declarations instead of carrying its own copy of the provider's vocabulary. Switching formats changes the labels, help, and choices you see.
- Custom prompt formats can now be authored in full: Reference prompt parts, physical populations, identity kinds, roles, contributions, and speaker policy, each behind its own disclosure. Forking a format keeps its readable role names instead of replacing them with their machine values.
- Reference and Identity rows now share one layout, prompt section `+` and `×` stay together as one control, glyph buttons have accessible names, and the identity editor opens with only Core identity expanded, adds searchable physical sources, reports missing required fields visibly, and shows a read-only panel of the format's routing defaults.
- Prompt authoring now compiles, labels, links, and counts reuse from the same live draft; opening one prompt surface no longer initializes another, and late Reference data refreshes every mounted consumer behind mutation guards.
- Declared inline capability placement is now available without a caret anchor, with truthful effective-phase reporting, resolved-text-first projections, caret-render deduplication, readable Prompt Link targets, responsive routing help, and fixed diagnostics geometry.
- Prompt Context placement now accounts for every capability independently, shows compiler-resolved in-box projections and non-emitting reasons, and supports per-section capability suppression without deleting the chip.
- Prompt formats now declare their physical populations, identity kinds, contribution vocabulary, speaker policy, and token grammar; MiniMax behavior no longer depends on duplicated channel-template checks.
- Unqueued dormant prompt preview now uses the same project-aware compilation path as live candidate/execution/Relay, while queued dormant previews remain frozen snapshots.
- Live Editor execution and Prompt Relay now share the project-aware Prompt Context compiler used by preview/enqueue, with demand-gated blocking diagnostics instead of silent blank prompts.
- Prompt diagnostics retain a visibly stale scene-keyed result while recompiling, avoiding per-keystroke placeholder reflow and cross-scene preview leakage.
- Context projections now follow every compiler placement above/below channel text, including multiple placements in one channel, and keep routed silent or linked-deduped chips visible with an explanation.
- Shot and standalone section Time chips now have real editors and are the only authored marker state; Time remains window-local and template-gated.
- Scope rows can reuse configured chips as linked emission groups with atomic propagated edits and Unlink, while the Prompt tool now separates compact Prompt Format, physical Reference Prompting, and explicit Identity Prompting.
- Prompt authoring now defaults inline bars to an all-channel view, uses a shared caret-aware context menu, shows per-channel Reference-chip projections, supports format-declared stable-id tokens, treats resolvable empty content as advisory, and restores persisted box heights. Semantic identities can now be description-only or combine attributed physical sources and voice.
- Timestamp is now an option on the Shot Context chip. Guide authoring is folded
  into Custom text; ComfyUI's own Add Guide for MiniMax H3 node takes the first-
  and last-frame images straight from the Guides Bridge, so no prompt-side
  binding is involved. Unsupported raw Guide/unknown kinds remain visible for
  repair and block while enabled.
- A MiniMax H3 Full Reference lane now feeds the model as soon as you stage it.
  Set a Reference lane to Pictures, Videos, or Audio and drop a Reference in —
  there is no separate step that registers the lane, and a brand-new scene works
  the same as one you copied. Lane order decides which is Picture 1. To leave a
  lane out, hide it or mute the item. Existing projects gain any H3 lane they had
  staged but never registered, which can renumber slots in a scene that has more
  than one. Reference lane Members and Advisories start collapsed, remember
  browser-local disclosure choices, and use larger thumbnails that open the
  existing read-only media inspector.
- MiniMax H3 Picture, Video, and standalone Audio populations now flow through
  the generic Reference Selector and media-typed bridges. Video remains a
  video-only authored population but is served as a 24 fps IMAGE sequence on
  the `17n+5` frame grid; bridge shape and labels follow the effective render
  window or the running job's frozen window.
- New queue jobs use an explicit `prompt_context_v1` envelope with complete
  Prompt Context, template, and Reference freezes; released unmarked v0.2.2
  jobs replay through an isolated frozen-only composer.
- Prompt profile, role, recipe, and validator authoring now consumes one
  schema-versioned server catalog, including sanitized built-in fork seeds.
- Reference chips now inherit identity defaults and store only sparse per-chip
  deviations; reset and explicit empty values remain distinct, with
  capability-owned config retaining final precedence.
- MiniMax H3 Picture, Video, and Audio population creation is one atomic,
  version-checked server mutation that reuses only safe unowned lanes.

### Removed
- Removed unpublished Shot/Time marker mirrors and migrations, Guide Context
  compilation, role aliases, paired-Audio semantics, retired Reference output
  vocabulary, implicit ordinal/audio text fallbacks, and frozen-to-live
  Reference/template fallbacks.
- Removed the six provider-shaped MiniMax H3 setup/bridge workflow nodes and
  setup-owned paired Audio. Paired Audio remains unavailable pending its own
  replacement design; Base physical keyframes use the existing Guides Bridge
  with upstream `MiniMaxH3AddGuide`, without a Sonder-specific replacement.

### Added
- Writing mode now shows what each attached Reference actually contributes, as prose, under the channel it is staged in — so you can read the sentence your References will produce beside the sentence you are writing, without leaving the draft. That includes channels you have not written in yet, which is where most of a Reference's output usually goes. A capability that routes to a channel but resolves to nothing says so, and says which one it is.
- Writing mode now completes `@` mentions. Typing `@` and the start of a
  handle offers this project's References — both prompt identities and
  physical sources — and Enter writes the handle, which compiles to the
  format's own label.

### Fixed
- A field your References feed that the prompt format lists first now shows its contributions inside the section you wrote them in, rather than stacked at the top of the Writing draft — and it stays there while you type, instead of dropping to the bottom each time the preview catches up.
- Writing mode no longer opens blank on a scene that has prompt sections, and Reset from sections no longer asks permission to discard a draft that is not there. An emptied draft was being treated as real work, which also meant a blank panel plus one Apply could clear every section in the scene.
- Shot and Time markers in Writing mode now show what they contribute — `[Shot 1] At 00:00.000,` — instead of an internal note about the section composer, and are named as Shots rather than as References. Chips attached to a section were mislabelled the same way.
- Splitting a section no longer stacks both halves' Reference contributions under the second one.
- The Writing draft's compiled preview now shows the same sections Apply will write. Preview and Apply each built that projection separately, and after merging two sections the preview could still show a Prompt Link pointing at the section that was absorbed.
- A Reference mention typed into a sentence now reads as part of that sentence rather than as a bordered token. It stayed on its own line and broke the paragraph around it, and it repeated the chip's description after the handle — so prose showed `@KWoman — <Subject 1> is the korean woman…` where it should read `@KWoman`. It still behaves as a single unit for the caret, its description still appears on hover, and its edit and remove controls appear on keyboard focus, taking their own space in the line rather than covering the words after it.
- Applying a Writing draft no longer adds a blank line between every section each time. Repeated Applies had accumulated more than a dozen, and Reset from sections now also clears padding a project already built up.
- Applying a Writing draft is no longer refused because the project changed. Any save anywhere in the project — moving a clip, importing an asset, editing another scene — used to mark every open draft stale, and the only offered recovery rebuilt the draft from the lane instead of applying what you wrote, so authored text had no way in at all. Apply now replaces the lane with your draft, as it says it does, and reports when it changes the number of sections.
- A Writing draft is no longer discarded when Apply is refused. The refusal could arrive after the editor had already cleared the draft and reported success, taking the draft, its chips and the restore copy with it. Applying now also keeps a restorable copy of what it applied.
- Undo no longer wipes the scene-wide prompt. Restoring any scene edit fed back a text-only mirror that is empty under MiniMax H3, clearing every global field with it. Undo now restores the global fields themselves.
- Text pasted from Windows apps now keeps its field headers. Carriage returns stopped `detailed_description:` and the rest from being recognized, so headers were left as literal text and everything landed in one field. Existing drafts carrying them are repaired on load.
- **Split here** now keeps you in the field you were writing in. Splitting
  mid-paragraph under a heading used to send everything after the break to the
  default field; the new section now carries that heading. It also tells you
  when other fields in the section you are leaving will not come with it.
- In Writing mode, text written above the first field header no longer becomes a
  subject definition under MiniMax H3 (full reference). It now goes to the
  body field — `detailed_description` — because that template leads with
  `subject_definitions`, so "the first field" was the wrong place for narrative
  prose. The panel names the field it will use, and a channel template can point
  this anywhere; drafts written before this change keep landing where they always
  did, so nothing already unapplied moves.
- Mentioning a Reference from the right-click menu now attaches the part that
  belongs to the field you are writing in. Picking a handle always attached a
  scene mention, whose output goes to the body field, so a Reference inserted
  while writing definitions or retention emitted into a different field than the
  one it was placed in.
- An unapplied Writing draft could be discarded without warning. Applying a
  draft left behind an emptied record stamped with the current time, and those
  records competed for the same forty slots as real ones — so enough applied
  scenes would silently evict the one draft still holding unwritten work. The
  emptied records are now dropped instead of hoarding a slot.
- Prompt text can now cite a shot instead of hard-coding its number. Writing
  `@shot(...)` against a Shot marker compiles to `[Shot 1]`, `[Shot 2]` and so
  on, and the number follows the marker — adding a shot earlier in the scene
  renumbers the citation instead of leaving it pointing at the wrong shot.
  Citing a shot outside the render window is refused rather than compiling as
  if it were there.
- MiniMax H3 reference definitions and retention lines now get one line each
  when they come from separate Reference chips, instead of being run together
  into a single paragraph. Three Subjects described on three chips compiled as
  one long line; MiniMax's own guide asks for a line per reference label. Prompts
  using more than one Reference chip in these channels will compile differently
  — and closer to the format — than they did before.
- **Reset from sections** in Writing mode no longer throws your draft away. It
  keeps a copy first and offers **Restore draft**, so the one control available
  when Apply is locked is no longer the one that destroys unapplied work. The
  copy survives a browser reload and is dropped once you Apply.
- Writing drafts no longer overwrite each other between editor windows. Saving
  or applying a draft rewrote the whole set of drafts from whatever snapshot
  that window last read, reverting any draft another window had changed since;
  each draft is now written on its own.
- Escape now closes the dialog you are actually in. Pressing it over the
  Reference attachment dialog, the writing-aid panel, the prompt format editor
  or the format actions menu closed the Prompt tool behind them instead, leaving
  the dialog stranded over a shut editor. Escape in the channel template editor
  did nothing at all for the same reason, and now closes it.
- A Reference chip now follows its Prompt Format when the format's declared
  destination or placement changes, instead of staying where it was first
  attached; per-part routing is stored only when it genuinely differs, so
  **Provider default** and Reset work on it like every other field.
- A refused prompt edit no longer leaves the Prompt tool showing text the server
  rejected.
- The identity and prompt format editors now confirm before a stray click or
  Escape discards an unsaved draft; Cancel still discards immediately.
- Dragging a Reference onto the timeline ruler creates a lane, matching how
  clips and audio already behave, and lane highlighting during the drag now
  matches what the drop will accept. A Reference mixing images and voice audio
  is refused when the drag starts, with an explanation, rather than after it
  lands.
- Subject definitions no longer read `…combat boots. from <Picture 1>`; the
  source citation is woven into the sentence.
- The prompt format actions menu now closes from its own button, from an outside
  click, and with Escape; unavailable actions are visibly dimmed and explain why
  instead of appearing to do nothing.
- Custom prompt formats can now actually be deleted. The actions menu lists every
  custom format in the project instead of only the one currently selected — which
  was always the one in use, and so always refused — and a format that cannot be
  deleted is dimmed and names the scenes, channel template, or Reference recipe
  still using it. Deleting now names the single format rather than rewriting the
  whole list, so a format created in another window is no longer destroyed
  alongside it.
- Reference Prompting now fills in as soon as the scene compiles, instead of
  showing every population as empty until the Prompt tool was closed and
  reopened.
- A Reference chip opened before its prompt format finishes loading now says that
  format-declared fields are still loading, and that saved values are untouched,
  instead of silently showing no task-type or handling rows at all.
- Disabled editor buttons no longer highlight on hover, and the prompt section
  `+` and `×` controls are the same size.
- Prompt Context chips now wrap to two container-bounded lines, scope placement reads **After section prefixes**, routing rows use title-case capability labels and stack cleanly at constrained widths, and fullscreen background paste is ignored without an intrusive warning.
- Fullscreen now owns background paste through the shared keyboard registry, refusing hidden graph paste while preserving native prompt-field paste; linked suppression uses a warning toast, and composer-owned Shot/Time markers no longer expose inert capability suppression.
- Prompt editor paste now stays inside the active contenteditable, authored token-like prose gets a literal-text advisory, and deletion is pinned through canonical document, save/reload, candidate, and newly frozen queue state.
- Reference/member deletion now prunes prompt-identity source and voice bindings,
  and identities are explicitly authored/deletable instead of auto-minted by
  the Reference Library.
- Reference Context chips now derive their visible name from the bound prompt
  identity or Library item and show the candidate compile emission. MiniMax
  identity definitions inherit through the effective sparse attachment config,
  then the authored identity and contributing Library member prompts; the
  attachment menu identifies that source, disables speaker targets without a
  Vocal Event in the render window, and exposes explicit Summary task-type
  overrides while staged Roles remain the default.
- Queue refusals now show the server diagnostic code and message. The Prompt
  tool reports compile errors for the effective render window, marks the exact
  affected Context chips, and treats no selection as the full-scene queue
  window. Context insertion no longer closes inline bars, scope rows refresh
  immediately, the Writing grip reaches its advertised height, and the Prompt
  tool has more room for six-channel templates.
- Reference lane identities now survive recipe edits, lane-count changes, and
  undo/restore; MiniMax setup bindings are repointed or pruned atomically, and
  stale unambiguous project bindings self-repair on load.

### Added
- Added project-durable **Context chips** and canonical prompt documents across
  timeline prompt bars and the Prompt tool's Structured and Writing modes.
  Shot, Reference, Vocal Event, Prompt Link, and declarative Custom capabilities
  now resolve from stable ids and the effective render
  window; profile-owned Writing aids insert fixed syntax without an LLM.
- Added immutable Prompt Context profiles (`generic@1`, MiniMax H3 Base, and
  MiniMax H3 Full Reference), project-scoped semantic identities, exact live
  compile diagnostics, collision-safe browser-template dependency closures,
  and enqueue-frozen prompts/Relay/setup manifests.
- Added project-unique physical/semantic handles, a section-scope live Prompt
  Link with explicit copy/unlink actions, and a reload-safe Writing Source view
  paired with read-only compiler output.
- Added one scene-authoritative MiniMax H3 conditioning setup with typed Picture,
  Video, and standalone Audio recipes and roles. The generic Reference bridges
  follow the same frozen physical slot plan and late-bound ordinals as prompt
  compilation.
- Replaced the mixed 39-output Reference Bridge with separate, type-homogeneous
  Image, Audio, and Prompt bridges. Their numbered blocks grow only to the
  staged or connected-slot ceiling, keeping links index-safe while removing the
  unusable wall of empty sockets.
- Reference items now author conditioning strength and temporal sequence length.
  Recipes can bound the short image edge and choose native, scene, or custom
  reference frame rate; video members serve their trimmed spans with streaming
  decode and resampling, and audio recipes can expose multiple members.
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
- Prompt sections can open a new Shot and optionally stamp that Shot's cut time.
  Times are relative to the render window, so the same Shot reads correctly
  whether you render the whole scene or one slice of it.
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

- Reference entity visual/audio defaults and staged role/preservation overrides
  now survive exact mutations, reload, undo, queue freezing, and compilation.
  Image-only References may retain a dormant audio default without blocking
  edits, and unsupported enum values are controlled validation errors.
- Structured scene-global documents now outrank their flat mirrors in combined
  mutations, and anchored flat replacement returns a controlled structured-edit
  conflict. Internal project reads share the canonical project lock and retry
  only short-lived Windows `PermissionError` races.
- Prompt editors now claim keyboard events above the timeline/graph while
  preserving native browser input. Space, arrows, deletion, and undo no longer
  leak into the timeline or LiteGraph, and every inline chip has an accessible
  remove action. The graph-load guard also covers ComfyUI builds whose undo
  capture runs before extension listeners, while document history restores the
  stable node/offset caret before continued typing.
- Prompt Context compilation now chooses effective segment origins before
  validation/emission, so dormant out-of-window chips cannot block a job or
  steal Prompt Link fallback text from the earliest selected consumer. Live
  candidate preview uses the same constraint-aware execution-window resolver
  as enqueue.
- Writing mode now round-trips muted state, global-channel exclusions, and
  stable empty sections. Range deletion removes both an inline chip anchor and
  its attachment record, and IME composition receives a real undo snapshot.
- MiniMax H3 Reference setup creation now installs complete typed recipe
  declarations. Physical Picture/Video/Audio definitions are authorable,
  setup overflow is diagnosed instead of truncated, and managed
  group/voiceover syntax follows the H3 guide.
- Prompt section split now preserves global-channel exclusions; transitive
  Prompt Links export their dependency edge by default; custom formatters are
  bounded to 4 KiB and resolve both `{text}` and legacy `{value}`.
- Legacy Timestamp-only prompt sections migrate to a timed Shot instead of
  losing authored timing intent; new projects persist one Shot attachment with
  a timestamp option.
- Browser prompt templates now retain structured documents, inline/scope chips,
  stable prompt ids, profile/setup state, semantic-unit dependencies, and muted
  state instead of silently flattening or dropping them during settings reload.

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
- Prompt Links and Vocal Events are inserted into the prompt text, not attached
  to the section as a whole. A section-level Prompt Link resolved to nothing and
  a section-level Vocal Event was pushed to the end of the spoken order, both
  silently. The scope row no longer offers either; any chip already sitting
  there stays visible, is marked, and blocks the job until it is re-inserted or
  removed.
- A prompt format can only be chosen alongside a channel template it targets.
  Pairing MiniMax H3 Full Reference with Standard channels previously skipped
  every MiniMax requirement while still claiming the format; incompatible
  choices are now shown disabled and refused on compile.
- MiniMax H3 Base no longer re-invents its implicit conditioning setup on every
  compile, so repeated previews agree with each other and with the live H3
  Bridge selector.
- A Vocal Event must name at least one Subject or a stable voice. Without one
  the speaker number was minted from the chip's own id and referred to nobody.
- MiniMax H3 Audio slots refuse a video that carries no audio track, in both
  standalone and paired positions.
- Malformed custom prompt formats are refused when saved instead of failing
  during compilation, and an unusable format now blocks with a readable message
  rather than a server error.
- Over-cap Context attachments and capabilities are refused on save instead of
  being silently trimmed, so authored work cannot disappear after a successful
  save. Existing over-cap projects still load intact.
- Context chips carrying a provider this build does not understand block instead
  of rendering under guessed semantics, and custom-capability fields are held to
  their declared names and values.
- Overlapping Reference Context chips no longer print the same Subject twice,
  and two chips that disagree about one Subject now report the conflict.
- Prompt history keeps runs with identical text but different task modes or
  conditioning setups as separate entries.
- Enter, Escape, and Backspace pressed while an IME candidate window is open now
  reach the IME instead of committing or discarding the prompt.
- Reference deletion confirms conditioning intents whenever they have been
  changed from their defaults, so a stale tab cannot delete work it never saw.
- The MiniMax singing writing aid emits the bounded-language `<d>[…] …</d>`
  envelope instead of the plain Generic form.
- Disabled Reference capabilities no longer take part in validation, so a
  capability that emits nothing cannot block a job.

### Changed
- Timeline prompt bars now show one remembered active channel with hidden
  non-empty indicators while retaining all channel editors, carets, undo state,
  and one host-owned attachment registry. IME composition must finish before a
  channel switch.
- Reference setup uses progressive disclosure and friendlier terms: prompt
  format, model input, prompt part, per-member options, Subject, and what to
  preserve. Reference Context pickers list staged sources only and distinguish
  section applicability from global authoring.
- Reference members may carry an optional suffix. Displays use
  `Reference · Member`; prompt patterns and `reference_names` use the normalized
  composite token while `{entity_name}` and `{member_name}` offer explicit
  custom-recipe control.
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
