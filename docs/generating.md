# Generating

How the editor talks to your generation workflow: what you select, what your
model is told, what gets queued, and which of your later edits still affect a
queued job.

Editing itself is covered in [Editor Basics](editor-basics.md); assets and
export in [Assets & Gallery](assets-and-gallery.md).

## What your model needs

<a id="model-agnostic-capability-bounded"></a>
Sonder Editor works with any model: editing, prompting and queueing never touch
one, and generation runs in your ComfyUI graph. What the editor hands your
graph is useful only if the model can act on it:

| If you want to… | Your model needs… |
|---|---|
| Chain clips into long-form video, or regenerate/inpaint a section inside existing footage | **Masked (in-context) generation** — the ability to hold provided frames fixed and generate the rest |
| Drive video from audio, or audio from video | **Joint audio-video generation** |
| Interpret timed Prompt sections within one generated clip | **A compatible prompt format or time-varying conditioning path**; compiling sections into text alone does not guarantee frame-exact changes |
| Use Drivers — unrendered video clips that steer motion, composition, look, or characters | **Video conditioning inputs** for that kind of signal |
| Use References — reusable characters, locations, props and voices, staged per window | **Reference image, video or audio inputs** — a dedicated reference slot, or any image input that will take an assembled sheet |
| Use guide frames | **Image conditioning at arbitrary frames** |

For example, the showcase workflows use **LTX 2.3**, which covers most of
these in one model, and **MiniMax H3**, which has dedicated reference inputs.
[Model templates](../README.md#models) also ship for Wan, HunyuanVideo,
CogVideoX and others, and **No Model Template** removes every constraint for
anything else.

## The generation window

The **Selection** is the **In**–**Out** range you set. The **generation
window** is what a render covers: the Selection plus any context frames. Both
are set in the **GEN** box of the toolbar, which also shows their frame counts.
Moving the playhead (the **F** field, or clicking the ruler) doesn't change
either.

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
- **✕** in the **GEN** box clears the Selection (the **X** key does too).

### Ways to set In and Out

- **Type** a frame in **In** or **Out** and press Enter. The fields accept
  arithmetic (`120+48`), and **TC** in the toolbar (or **T**) switches them to
  seconds.
- **At the playhead:** click the ruler, then press **I** for In or **O** for
  Out.
- **From an item:** right-click a clip, an audio track or a Prompt section and
  choose **Set Selection to Clip**, **Set Selection to Audio** or **Set
  Selection to Prompt**.
- **From guides:** right-click a guide for **Set Selection In** or **Set
  Selection Out** at its frame, or **Select Guide Range**.
- **From a saved selection:** click the bookmark in the **GEN** box.

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

The built-in templates are listed in the [README](../README.md#models). Manage
them in **Settings ▸ Model Templates**: create custom templates, edit any
built-in (with reset), and pick the default for new projects. Custom templates
are saved in your browser, so a project opened on another computer without
your custom template uses **No Model Template**.

Switching to a template with a different frame rule clears **In** and **Out**;
a template with the same rule keeps them. Queued jobs keep the template they
were queued with.

When your generation window does not land on the frame rule, the editor rounds
**up** rather than trimming your selection, and fills the extra frames itself:
video holds the last frame, audio mirrors the end of its own window. Those
frames are left out of the take placed on the timeline, but they are inside the
generated file.

## Prompts

The Global document and every prompt section in the generation window compile
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
- **Settings ▸ Guides ▸ Collision Auto-Offset (project-wide)**, on by
  default, moves a single-image guide off a frame a Driver already uses (for
  LTX-style workflows), and the take's details record the move. With it off,
  queueing warns about the collision instead.
- Guides reach a graph two ways, and you pick one. The editor node's
  `guide_images` output carries every guide in the window as a single IMAGE
  batch, with `guide_idx` and `guide_strengths` as lists aligned to it, for a
  consumer that takes them all at once. **Sonder Guides Bridge Start / End**
  instead wrap your generation body in a loop and emit one guide per pass, for
  a consumer that takes a single guide per call. Do not inject the same project
  guides through both.

## Drivers

[Convert a Video clip to Driver](editor-basics.md#driver-clips) from its
right-click menu. The original video remains an asset, but the Driver is no
longer a visible picture layer. To exclude an old Driver from this workflow's
guidance, hide its **Driver lane** with the eye icon at the left of that row.
This does not hide a separate Audio lane. Collapse only folds the row; it does
not disable guidance. A workflow's explicit Driver override can take priority
over lane hiding, so check the selected workflow's instructions.

Driver clips steer motion, composition, look, or characters without ever
appearing in output (their editing rules are in
[Editor Basics](editor-basics.md#driver-clips)). Each Driver has a
**strength** (0–1). In the graph, **Sonder Driver Selector** picks a Driver
row from its lane menu and outputs `has_driver`; **Sonder Driver Bridge**
outputs that row's frames for the generation window.

## References

Reference items stage Library members — characters, locations, props,
voices — over frame ranges, and reach the model as media assembled by the
lane's recipe plus prompt text derived from those members (authoring is in
[References](references.md)). Each item has a **strength** (0–1) and a mute
toggle, and for any generation window each lane resolves which of its items
applies. The nodes that carry them into a graph are in
[References › Wiring the nodes](references.md#wiring-the-nodes).

## Wiring your graph

The **Sonder Editor** node has only outputs. Apart from the project fields on
its card, its widgets are set by the editor.

| Output | Type | What it carries |
|---|---|---|
| `project` | SONDER_PROJECT | A handle to the project. Every other Sonder node takes this. |
| `rendered_frames` | IMAGE | The timeline's own frames for the generation window. |
| `prompt` | STRING | The compiled prompt for that window, chips resolved. |
| `audio` | AUDIO | The mixed timeline audio for that window. |
| `guide_images` | IMAGE | Every guide frame in the window, as one batch. |
| `guide_idx` / `guide_strengths` | STRING | Comma-separated frame positions and strengths, aligned with that batch. |
| `frame_count` · `fps` · `width` · `height` | INT · FLOAT · INT · INT | The window's geometry, for your sampler and latents. |
| `mask_start_time` / `mask_end_time` | FLOAT | Where the regenerated span begins and ends. |

A text-to-video graph needs three of them and no bridge nodes:

```
Sonder Editor ──prompt────────> your text encoder ──> sampler ──> VAE Decode
              ──frame_count───>                                        │
              ──width/height──>                                        ▼
                                                               Sonder Save Video
```

End every graph with **Sonder Save Video** (or **Sonder Save Bridge**) taking
`project` from the editor, so results come back to the project. Wire `project`
directly: Get/Set nodes, reroutes and Anything Everywhere break it.

Add bridge nodes only for what your graph uses: guides (**Guides Bridge
Start/End**), masks (**Masks Bridge**), timed prompts (**Prompt Relay
Bridge**), Drivers (**Driver Selector/Bridge**) and References
([Reference Selector and Bridges](references.md#wiring-the-nodes)). The full
node list is in the [README](../README.md#nodes).

![The showcase workflow: the Sonder Editor node feeding conditioning, sampling, and finally Sonder Save Video](images/showcase-workflow.webp)
<p align="center"><em>The showcase pipeline. Your graph can be as small as
editor → sampler → Sonder Save Video.</em></p>

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
  is how you regenerate audio over locked picture, or the reverse. A frozen
  channel keeps the rounded-up filler too, so if the window had to be rounded
  to the frame rule, its last fraction of a second is editor-generated rather
  than timeline content. Freeze is not the only way that happens - a window
  with post-context keeps the filler on both channels - so treat the filler as
  something the model sees, not something it replaces.
- Wire only half a pair and that channel emits a keep-everything mask and logs
  a warning. A latent that was not encoded from this generation window is refused
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
  id; chunk size follows the template's frame rule and **Max frames per
  batch** (**Settings ▸ Render**). With zero context the chunks are independent; with context, later
  chunks read earlier chunks' placed takes.
- **Queue Active** decides what a ComfyUI run renders: **ON** → the first
  pending queued job (FIFO, one per run); **OFF** → the live editor
  selection.
- Job completion is owned by the terminal save node (**Sonder Save Video**
  or **Save Bridge** with `mark_queue_complete` on). If a batch chunk fails,
  later chunks in that batch are auto-skipped so a progressive batch can't
  continue over a gap.
- New takes land on the timeline as described in
  [Takes](assets-and-gallery.md#takes).

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
- The queue row always shows the frozen range/context/mask.
