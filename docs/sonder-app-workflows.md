# Sonder App Workflows

**Sonder App ships with starter workflows for generating video and sound,
editing existing footage, working with References, and refining or upscaling
your results.** Their graphs are already wired: choose a workflow in the
Launcher, then prepare your media, choose what to do and generate from the app.

- **[LTX 2.5 Edit & VFX](#ltx-25-edit--vfx)** — generate new video, edit with
  words or a picture, follow a source video's layout, movement or outlines,
  remove people, and create masks. Keep existing audio or generate new sound.
  **LTX 2.5 Edit & VFX — Prompt Relay** is the same workflow with
  [timed prompts](#try-timed-prompts).
- **[LTX 2.5 Refine & Upscale](#ltx-25-refine--upscale)** — refine existing
  footage, double its width and height, or do both. Optionally focus refinement
  on a character or the environment.
- **[MiniMax H3 — References & Swaps](#minimax-h3--references--swaps)** —
  generate using picture, video and sound References, or replace a character
  or head while keeping the original audio.

You work through the **timeline**, **Controls**, **Modes** and **viewport
Views**, without opening or editing the ComfyUI graph. Each workflow includes
setup notes and instructions inside **Controls**.

## Before your first generation

[Open your project and workflow](sonder-app.md#generate) from the Launcher.
In the editor's left sidebar, open **Controls** and read the workflow's setup
and **Models and downloads** notes. ComfyUI needs the required custom nodes
and model files installed before you generate.

If your models are saved in different folders, click the **advanced hidden**
line below Modes if it appears, then select your installed files in the
matching model controls.

Choose your [Modes](controls.md#use-a-mode) before adjusting individual
settings. Start with a short **[In / Out](generating.md#ways-to-set-in-and-out)**
selection and **×1** beside **Generate**.

## LTX 2.5 Edit & VFX

Generate new footage or change an existing video. **Task** chooses what happens
to the picture; **Sound** independently chooses whether to keep the timeline's
audio or generate new sound.

For example, to generate a video that follows the outlines of existing footage:

1. **Supply the source video.** Open **Assets** at the upper left and drag
   files into the gallery, or click **Import**. Drag the video onto a **Video**
   lane, then right-click its clip and choose **Convert to Driver**. The
   [Driver](editor-basics.md#driver-clips) supplies guidance and disappears
   from the normal preview.
2. **Choose the task.** In **Controls → Modes → Task**, select **Follow
   Outlines**.
3. **Choose the sound.** Select **Sound → Use Timeline Audio** and place the
   soundtrack on an **Audio** lane. Choose **Generate New Audio** instead if
   you want new sound. See [Dropping media on the timeline](editor-basics.md#dropping-assets-onto-the-timeline)
   to place a video's picture and sound together.
4. **Describe the result.** Double-click empty
   **[Prompt](editor-basics.md#prompt-sections)** space near the bottom of
   the timeline, write what you want to see, then click **Create**.
5. **Check and generate.** Set **In / Out** above the timeline within the
   source footage, pressing **Enter** after each value. To inspect the
   outlines first, choose **Processed Driver** from a viewport pane's source
   menu and click that pane's **Run**. Click **Generate** when ready.

Other tasks use different inputs. Read the selected choice's description and
the workflow's **Prepare your media** note. **Edit With a Picture**, for
example, also needs an edited picture on **Guides**.

To generate sound for existing footage, keep the footage on **Video** and
choose **Task → Keep Existing Video** and **Sound → Generate New Audio**.

### Try timed prompts

We recommend [Prompt Relay](prompts.md#prompt-relay) when you want closer
control over when actions change. It lets different timeline **Prompt**
sections guide different parts of the same generation.

Choose **LTX 2.5 Edit & VFX — Prompt Relay** in the Launcher instead. It needs
[ComfyUI-PromptRelay](https://github.com/kijai/ComfyUI-PromptRelay): install
it, restart ComfyUI and refresh the page before opening the workflow. In
**Controls → Modes → Prompt Method**, choose **Timed Prompts (Prompt Relay)**
to use it, or **Standard** for normal LTX prompting.

## LTX 2.5 Refine & Upscale

Process the footage visible in your scene. Open an existing scene, or import
a video through **Assets** and place it on a **Video** lane.

1. **Choose the processing.** In **Controls → Modes → Passes**, select
   **Refine and Upscale Pass**, **Refining Pass Only**, or **Upscale Pass Only**.
2. **Set the working resolution.** Use **Res** above the timeline. Refinement
   uses this size; upscaling doubles its width and height. For example,
   **896 × 512** becomes **1792 × 1024**.
3. **Describe the refinement.** When using the refine pass, create a timeline
   **Prompt** describing the look you want.
4. **Start without a mask.** Leave **Refine Pass Spatial Mask → Mask Off**.
   To focus refinement on a character or the environment, follow the
   workflow's **Use a mask** note.
5. **Generate a short section.** Set **In / Out** above the timeline within
   the footage, pressing **Enter** after each value, then click **Generate**.

With both passes, the refine result stays in **Assets**, and only the upscaled
result is placed on the timeline as a [Take](assets-and-gallery.md#takes).
With either single-pass choice, its result is placed.

Masking affects the refine pass only.

## MiniMax H3 — References & Swaps

Generate using picture, video or sound [References](references.md), or replace
a character's appearance in existing footage.

Above the timeline, choose **Model → MiniMax H3** and **Channels → MiniMax H3
(full reference)**.

For generation, choose a generation Task under **Controls → Modes → Task**,
then follow the workflow's **Use References** note. References must be placed
in time to participate; adding them to the Library alone does not send them
to the model.

For a masked character swap:

1. **Keep the original picture and sound.** Place the footage on **Video** and
   its soundtrack on **Audio**. Keep one copy of the sound.
2. **Supply the performance.** Add a second copy of the video to another
   **Video** lane, then right-click its clip and choose **Convert to Driver**.
   Match its timing, trim and framing to the visible footage.
3. **Supply the replacement appearance.** Follow **Use References** in
   **Controls** to place one replacement picture on a **MiniMax H3 Pictures**
   Reference lane. Describe the replacement in that Library member's
   **Prompt**. See [H3 Subject or Picture setup](references.md#minimax-h3-full-reference)
   for the Reference options.
4. **Choose the swap.** Select **Modes → Task → Masked Character Swap**.
   In **Who do you want to mask and swap in your target video?** below Modes,
   describe the person to replace, such as `the woman`.
5. **Check the target and generate.** Set **In / Out** inside both source
   clips. Choose **Masked Driver Preview** from a viewport pane's source menu
   and click its **Run**. Check the selected person, then click **Generate**.

All four swap choices keep the original timeline audio. Their replacement
description comes from the picture member's **Prompt**; the timeline Prompt
is ignored.

Keep both Video and Driver copies for masked swaps: Video supplies the picture
outside the mask, while Driver guides the replacement.

## Review your results

Open **Assets** to review results. In **Controls → Editor**, **Save as → Take**
also places a result on the timeline; **Video** saves it without placing it.
Refine & Upscale has separate **Refine save as** and **Upscale save as**
controls, set automatically by its **Passes** choices.

In **Controls → Editor**, turn **Takes start muted** on to keep testing against
the original footage, or off to build on each new result. Check which existing
clips are visible before generating again.

For further editing, choose **Lossless FFV1 (RGB)** or **Editing Master MP4**
in the workflow's output-format control. Use **High Quality MP4** for a
finished video.

For comparing versions and exporting the assembled timeline, see
[Compare](assets-and-gallery.md#compare-mode) and
[Export the timeline](assets-and-gallery.md#export-timeline).
