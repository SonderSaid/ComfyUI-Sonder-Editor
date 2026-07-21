# Generating

How the editor talks to your generation workflow: what you select, what your
model is told, what gets queued, and which of your later edits still affect a
queued job.

Editing itself is covered in [Editor Basics](editor-basics.md); assets and
export in [Assets & Gallery](assets-and-gallery.md).

## Model-agnostic, capability-bounded

Sonder Editor doesn't require any specific model. The editor works with
**timeline math and your assets** — arranging, trimming, compositing,
prompting, and queueing are pure editing operations that never touch a model.
Generation happens in *your* ComfyUI graph, with whatever model you wire up.

What the editor *hands* to your graph, however, is only useful if your model
can act on it. The editing features always work; the **generation features
light up based on what your chosen model supports**:

| If you want to… | Your model needs… |
|---|---|
| Chain clips into long-form video, or regenerate/inpaint a section inside existing footage | **Masked (in-context) generation** — the ability to hold provided frames fixed and generate the rest |
| Drive video from audio, or audio from video | **Joint audio-video generation** |
| Feed time-aligned prompt sections into the sampler | **Prompt relay support** in your workflow |
| Use Drivers — unrendered clips that steer motion, composition, look, or characters | **Reference/conditioning inputs** for that kind of signal |
| Use guide frames | **Image conditioning at arbitrary frames** |

The showcase workflows use **LTX 2.3** because it currently has the most
complete suite of these capabilities in one model — not because the editor
depends on it. Model templates ship for Wan, HunyuanVideo, CogVideoX and
others, and the Free template removes all constraints for anything else.

## The generation window

Everything a render will cover is defined by the **GEN block** in the toolbar.

- **Selection (In/Out)** is the live range a render executes. It persists per
  project + scene in your browser, so scene switches and refreshes restore
  it. No selection means **Full scene** — renders and queues then cover the
  whole scene. Committed In/Out values snap to the active template's frame
  grid; the ▲▼ steppers move directly along it.
- **Context frames (Ctx Pre/Post)** extend the source window beyond the
  selection, feeding your graph already-rendered frames for generation
  overlap — this is what makes seamless chaining possible.
- **Mask offsets (Mask −/+)** fine-tune which of those context frames are
  masked for regeneration versus passed through untouched.
- **Saved Selections** (the bookmark button) capture the *full* recall state
  — in/out, context, and mask offsets — and live in the project, so they're
  shareable, unlike the live selection.
- Quick setters live throughout the UI: `I`/`O` at the playhead, *Set
  Selection to Clip/Audio/Prompt*, *Select Guide Range*, and per-guide
  *Set Selection In/Out*.

All GEN inputs accept simple arithmetic (`+ - * /`) on commit and switch
between frames and seconds with Timecode mode (`T`).

### Empty timeline means black frames

The editor always hands your workflow real frames. Where the timeline has
no visible content, those frames are **black** — an empty range isn't
"nothing," it's black video. That matters for denoise strength, in any
diffusion workflow:

- A workflow that **fully denoises** its generation window (denoise 1.0,
  as in masked/in-context generation or a plain T2V pass) generates freely
  there — the black source is discarded.
- A workflow that **partially denoises** (vid2vid-style strength below
  1.0) preserves part of its source signal — over an empty range that
  source is black, and the result gets pulled toward it.

Rule of thumb: first passes over empty timeline need a full denoise;
partial-strength passes belong on ranges that already have content.

## Model templates

A **model template** is a named set of constraints for a target model, chosen
in the toolbar's scene-geometry group.

- **Hard constraints snap**: dimension step/offset, the frame rule
  (e.g. LTX's `8n+1`), and the allowed-FPS list.
- **Soft constraints only advise**: recommended/max resolution and a
  recommended duration band show a **⚠** hint when exceeded but never clamp
  your values.

Built-in templates: **No Model Template (Free)**, **LTX 2.3**,
**Wan 2.1 / 2.2 (14B)**, **Wan 2.2 (TI2V-5B)**, **HunyuanVideo 1.5**,
**CogVideoX 1.5 (T2V)**, and **CogVideoX 1.5 (I2V)**. Manage them in
**Settings ▸ Model Templates**: create custom templates, edit any built-in
(with reset), and pick the default for new projects. Template definitions
live in your browser; the project stores only the selected template id, and
falls back to Free on a machine without your custom template.

Switching to a template with a *different* frame rule clears the In/Out
selection (the old endpoints would be off-grid); same-rule switches keep it.
Already-queued jobs are never affected.

## Prompts

### How the output prompt is composed

The **Global** text plus every prompt section in the render window compose
into one prompt, joined by the project-wide **Section Delimiter**. Each
section carries three channels — Visual / Speech / Sounds. With **Channel
Labels** on, output groups by channel (`[VISUAL]:` …); off, it's a plain
temporal concatenation.

- **Boundary Prompt Threshold** (project-wide) drops a section from a window
  when the selection clips only a tiny edge sliver of it — so frame snapping
  can't bleed a neighbor's text into your generation. The timeline shows
  affected slivers with a dim "Ignored" hatch, and sections that *will*
  compose get a strong accent.
- Lane hiding is part of composition: Prompt lane hidden → global-only
  output; Global hidden → sections only; both hidden → empty prompt.

### The Prompt Management panel

Open it with **☰** on the Prompt or Global header. Two modes:

- **Structured mode** — global text, per-section rows with range and channel
  fields, insert/add controls, per-row **Select** / **Queue**, reusable
  prompt **templates**, and an enqueue-captured **history** with one-click
  Apply.
- **Writing mode** — one narrative draft box split into sections with `---`
  break lines, plus an allocation strip to distribute frames per block.
  **Apply** replaces the lane's sections in one undoable step and can extend
  the scene if the draft runs past it.

## Guides

Guide frames condition generation with a reference image at a specific frame
(and double as the viewport animatic — see
[Editor Basics](editor-basics.md#the-viewport)).

- Each guide has a **strength** (0–1) and a mute toggle; muted guides are
  skipped everywhere.
- **Guide Management** (☰ on the Guides header) lists every guide with
  re-keying, strength, mute, in-place image replacement, and deletion.
- **Add Frame to Guides** on any clip captures that clip's frame at the
  playhead as a new image asset + guide.
- For LTX-style workflows, **Settings ▸ Guides** exposes a project-durable
  *Guide collision auto-offset* toggle (default on) that moves single-image
  guides off temporal slots already occupied by a Driver, recording the
  applied move in the generated asset's metadata.

## Drivers

Driver clips steer motion, composition, look, or characters without ever
appearing in output (their editing rules are in
[Editor Basics](editor-basics.md#driver-clips)). Each Driver has a
**strength** (0–1). Downstream, the **Sonder Driver Selector** resolves one
Driver lane by position — without decoding media — and exposes a `has_driver`
flag for lazy routing; the **Sonder Driver Bridge** then decodes that lane's
frames for the render window.

## Wiring your graph

The editor's output socket feeds your generation workflow; optional bridge
nodes carry each conditioning stream to where your graph needs it — guides
(**Guides Bridge Start/End**), masks (**Masks Bridge**), prompts
(**Prompt Relay Bridge**), and Drivers (**Driver Selector/Bridge**). Results
come back through **Sonder Save Video** or **Sonder Save Bridge**, which
register outputs as project assets. The full node list is in the
[README](../README.md#nodes).

## The render queue

The queue panel docks at the bottom of the Assets sidebar.

- **+ Queue** freezes the current generation window into a job (see the next
  section for exactly what freezes). No selection queues the full scene.
- **+ Batch (N)** splits the selection into contiguous chunks sharing a batch
  id; chunk size follows the template's frame rule and the Batch Max Frames
  setting. With zero context the chunks are independent; with context, later
  chunks read earlier chunks' placed takes.
- **Queue Active** decides what a ComfyUI run renders: **ON** → the first
  pending queued job (FIFO, one per run); **OFF** → the live editor
  selection.
- Job completion is owned by the terminal save node (**Sonder Save Video**
  or **Save Bridge** with `mark_queue_complete` on). If a batch chunk fails,
  later chunks in that batch are auto-skipped so a progressive batch can't
  continue over a gap.
- New takes enter the timeline per your Render settings: linked video+audio
  and optionally starting muted.

## What's frozen vs. what stays live

Queuing takes a **snapshot**. Knowing what's inside it tells you which later
edits affect a queued job and which don't.

### Frozen into the job at queue time

| Snapshotted | Detail |
|---|---|
| **Selection range** | In/Out (or the full scene if nothing was selected) |
| **Context frames** | pre + post as set at queue time |
| **Mask offsets** | mask − / mask + values |
| **Guides** | every guide in the window: image, frame, strength, mute state |
| **Prompts** | the Global text, every covering section (all channels), delimiter and label settings, plus the final composed prompt string |
| **Drivers** | Driver clip snapshots and lane configuration |
| **Scene geometry** | width, height, FPS at queue time |
| **Model template** | template id and frame rule — later switches never re-snap a job |
| **Take placement mode** | trimmed/untrimmed, per job |

### Still live at execution time

| Live | Consequence |
|---|---|
| **Render clips & audio tracks** | trims, moves, mutes, and lane hides apply as they are when the job runs — the timeline state at execution is what renders |
| **Newly placed takes** | with context, later batch chunks read earlier chunks' takes through the normal compositor |
| **Guides-Bridge per-guide overrides** | the bridge node's override map is read at execution, not from the snapshot |
| **Take link/mute settings** | resolved from live Settings when the take is placed |
| **Asset gallery state** | folders, favorites, renames never affect queued jobs |

Rules of thumb:

- Edit **conditioning** (guides, prompts, Drivers, geometry, template)
  *before* queueing — those are locked per job.
- **Picture is live, and that cuts both ways.** Clips and audio render as
  they are at the moment a job executes. That's what lets a later batch
  chunk read earlier chunks' placed takes — but it also means muting,
  moving, or trimming content that a pending job expects to read (for
  example, a clip inside its pre-context window) changes that job's result.
  While jobs are pending or running, leave the timeline content inside their
  windows alone; edit freely outside them.
- The queue row always shows the frozen range/context/mask — what you see in
  the row is what will render.
