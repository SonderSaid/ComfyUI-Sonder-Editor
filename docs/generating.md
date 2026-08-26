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

The Global document and every prompt section in the render window compile
into the one prompt a job carries. Composition, Channel Templates, prompt
formats, Context chips, and the Prompt Management panel are covered in
[Prompts](prompts.md).

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
(**Prompt Relay Bridge**), Drivers (**Driver Selector/Bridge**), and
References (**Reference Selector** plus the Image, Audio, and Prompt Bridges —
see [References](references.md)). Results come back through **Sonder Save Video** or **Sonder Save Bridge**, which
register outputs as project assets. The full node list is in the
[README](../README.md#nodes).

### Masking an audio-video latent

To regenerate only part of a clip while keeping the context frames, the Masks
Bridge can build the noise masks itself. Encode the editor's frames and audio
into an AV latent, split it, mask each half, and rejoin:

```
VAE Encode (editor frames + audio)
  -> Concat AV Latent -> Separate AV Latent
       video_latent -> Set Latent Noise Mask (mask <- bridge video_mask)
       audio_latent -> Set Latent Noise Mask (mask <- bridge audio_mask)
  -> Concat AV Latent -> sampler
```

Wire each latent, and the VAE that encoded it, back into the Masks Bridge — it
needs **both** to build that channel's mask. The pixel-to-latent geometry comes
from the VAE itself, so it is correct on LTX, MiniMax H3 and Wan without any
per-model setting. What is *not* universal is composition — see the last two
notes below.

Notes worth knowing:

- **Freeze** emits an all-zero mask, so that channel is kept from source. This
  is how you regenerate audio over locked picture, or the reverse.
- Wire only half a pair and that channel emits a keep-everything mask and logs
  a warning. A latent that was not encoded from this render window is refused
  outright rather than masked at the wrong scale.
- The two mask outputs are **not** interchangeable. Video is a batch with one
  entry per latent frame; audio is a single image whose time axis differs by
  model. Swapping them resizes silently instead of erroring.
- Masking only one stream leaves the other fully regenerated, because **Concat
  AV Latent** fills an absent mask with all-ones. Mask both, or Freeze the one
  you want kept.
- **On LTX with guides or a start image, use `LTXVAudioVideoMask` (kjnodes)
  instead of Set Latent Noise Mask.** Drive it from this node's four *time*
  outputs rather than the MASK outputs, and place it **before** the guide chain:

  ```
  Separate AV Latent -> LTXVAudioVideoMask -> [guide chain] -> Concat AV Latent
                          max_length: partial     <- NOT the default
                          video_start/end_time, audio_start/end_time <- bridge FLOATs
  ```

  It writes the 5D noise mask that LTX's own nodes expect, so `LTXVAddGuide`
  *extends* it rather than colliding with it, and the guides keep their claims.
  Two things to get right: `max_length` defaults to `truncate`, which slices the
  latent and destroys the guide tail — set it to `partial`; and place it before
  the guide chain, because afterwards it either loses your context or silently
  rewrites guide strengths.

  The cost is that you retype `video_fps`, and the node is LTX-only — its 8
  frames-per-latent and 25 audio-latents-per-second are hardcoded, so it is
  wrong on Wan and on MiniMax H3. **Keep the MASK outputs and Set Latent Noise
  Mask for H3 and Wan**, which have no such conflict.

  What goes wrong if you use Set Latent Noise Mask anyway on LTX: those nodes
  read the mask through one helper that expects the 5D form, while ComfyUI's
  `SetLatentNoiseMask` stores 4D. Before `LTXVAddGuide` that raises
  `IndexError: tuple index out of range`. Before `LTXVImgToVideoInplace` it is
  **silent and total** — that node writes over the leading latent frames, but on
  a 4D mask that axis has length one, so it overwrites the entire mask and
  nothing generates at all. After the guide chain the guides are discarded when
  your window reaches the end of the clip, or survive with a silently shifted
  window when it does not. MiniMax H3 is exempt throughout, because its guide
  node returns only conditioning.
- **A mask replaces, it never composes.** Anything that already wrote a
  `noise_mask` upstream loses it — including start-image and continuation nodes
  such as `WanImageToVideo`, `WanAnimateToVideo`, the Cosmos image-to-video
  pair, HunyuanVideo's *v2 (replace)* mode and SCAIL continuation, all of which
  pin their reference frames that way. Those pins are silently destroyed.
  Chaining through the Sonder timeline is unaffected, because it carries context
  as encoded frames inside the window rather than as a producer-written pin.
  Stock `SetLatentNoiseMask` behaves identically; this is not specific to the
  Masks Bridge.
- Masks are hard 0/1, so the boundary latent is fully regenerated and the seam
  is a hard cut at latent resolution — around a third of a second for LTX
  video, and much finer for audio.

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
| **Prompts** | canonical documents/Context chips, complete immutable profile and hash, setup/ordinal manifest, chronological Relay channels, diagnostics, and the byte-exact final compiled prompt |
| **References** | effective winning Reference inputs and the resolved MiniMax H3 setup slot plan, when used |
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

- Edit **conditioning** (guides, prompts, References, Drivers, geometry, template)
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
