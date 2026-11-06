# Install Models and Node Packs

**Before a Sonder App workflow can generate, ComfyUI needs the node packs and
model files it uses.** This page takes you from nothing installed to a workflow
the Launcher reads as **Ready**. The steps work for any workflow; the tables
list every file the three supplied workflows need.

- [The steps](#the-steps)
- [LTX 2.5 Edit & VFX](#ltx-25-edit--vfx)
- [LTX 2.5 Refine & Upscale](#ltx-25-refine--upscale)
- [MiniMax H3 — References & Swaps](#minimax-h3--references--swaps)

## The steps

1. **Set up the Sonder App first.** Install Sonder Editor
   ([Installation](../README.md#installation)) and follow
   [Set it up](sonder-app.md#set-it-up) on the Sonder App page.
2. **Install the node packs.** Each workflow's section below lists its packs.
   For your own workflow, use the pack names the Launcher lists.
   1. In ComfyUI, click **Extensions** at the top right. **Nodes Manager**
      opens.
   2. Click the **Search** box at the top and type the pack's **Search for**
      word from the tables below. Wait a moment for the list to settle, then
      click **Install** on the pack with that name.
   3. When every pack is installed, click **Apply Changes** in the bar that
      asks you to restart ComfyUI. When ComfyUI is back, reload the page in
      your browser. If ComfyUI doesn't come back, start it again the way you
      usually do.

   If there is no **Extensions** button, ComfyUI was started without its
   Manager: start it with the `--enable-manager` option, or install each pack
   by following the instructions on its GitHub page, then restart ComfyUI.
3. **Find where each file goes.** Open the
   [Launcher](sonder-app.md#open-the-launcher) and click the workflow (the
   supplied ones are under **Starters**). Its row lists each missing file as *file → place*. Save each
   file there. If you already have a file in another models folder, or under
   another name, keep it where it is instead: open the workflow, click
   **advanced hidden** below **Modes** in **Controls** if it appears, and pick
   your file in the matching model setting. The tables below list every file,
   including those other Modes need.
4. **Get access to the gated files.** Files whose **Access** column names a
   repository need a free Hugging Face account. Click **Sign Up** at the top
   right of [huggingface.co](https://huggingface.co) to create one, or
   **Log In**. Then open each repository named in the **Access** column and
   click **Agree and Access**. Access is granted at once.
5. **Download each file.** Click a file's name in its table to open its page.
   - On Hugging Face, click **Download** just above the file on the left, then
     **Download file**.
   - On GitHub, click **Download raw file**: the download arrow at the right
     end of the bar above the file.

   Save it in the place from step 3, subfolders included. Create a folder if
   it doesn't exist yet. Keep the file name as it is.
6. **Check again.** In the Launcher, click **ComfyUI** at the top right, then
   **Check again**. No restart is needed for model files. The workflow then
   reads **Ready**.

**Using the workflow on the canvas?** Download every file in its table except
the optional one.

## LTX 2.5 Edit & VFX

**Node packs:**

| Pack | Search for |
|---|---|
| [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) | `KJNodes` |
| [RES4LYF](https://github.com/ClownsharkBatwing/RES4LYF) | `RES4LYF` |
| [ComfyUI-PromptRelay](https://github.com/kijai/ComfyUI-PromptRelay), optional | Not in Nodes Manager |

Install RES4LYF even when the Launcher reads **Ready**: the workflow uses its
sampling method, and the Launcher can't tell when it's missing. Without it,
**Generate** stops with *ComfyUI didn't accept the run*.

ComfyUI-PromptRelay is needed only for **Controls → Modes → Prompt Method →
Timed Prompts (Prompt Relay)**. It isn't in Nodes Manager, so install it with
[Git](https://git-scm.com/downloads):

1. Open a terminal in ComfyUI's `custom_nodes` folder. On Windows: open that
   folder in File Explorer, click the address bar, type `cmd` and press
   Enter.
2. Type `git clone https://github.com/kijai/ComfyUI-PromptRelay` and press
   Enter.
3. Restart ComfyUI and reload the page in your browser.

The LTX, MoGe and SDPose nodes come with ComfyUI. If the Launcher lists one of
them as missing, update ComfyUI.

**Model files** (about 55.6 GB in all; **Make a New Video** and **Keep
Existing Video** need 47.6 GB of them):

<!-- Sizes from the Hugging Face and GitHub file pages, 2026-11-04. -->

| File | Folder | Size | Access | Needed by |
|---|---|---|---|---|
| [ltx-2.5-22b-dev-transformer-comfy-int8-convrot.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/diffusion_models/ltx-2.5-22b-dev-transformer-comfy-int8-convrot.safetensors) | `models/diffusion_models/` | 21.5 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors) | `models/text_encoders/` | 15.4 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [ltx-2.5-video-vae-conv-bf16.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/vae/ltx-2.5-video-vae-conv-bf16.safetensors) | `models/vae/LTX/` | 1.5 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [ltx-2.5-audio-vae-bf16.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/vae/ltx-2.5-audio-vae-bf16.safetensors) | `models/vae/LTX/` | 365 MB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [ltx-2.5-22b-distilled-lora-450-bf16.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors) | `models/loras/LTX/` | 8.9 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [taeltx2_3.safetensors](https://github.com/madebyollin/taehv/blob/main/safetensors/taeltx2_3.safetensors) | `models/vae_approx/` | 24 MB | Open | Every choice |
| [edit_anything_v2_ltx2.5.safetensors](https://huggingface.co/Alissonerdx/EditAnything/blob/main/edit_anything_v2_ltx2.5.safetensors) | `models/loras/LTX/` | 2.5 GB | Open | Task → Edit With Words |
| [LTX25_Ripple_v11.safetensors](https://huggingface.co/WepeNerd/LTX-Ripple/blob/main/LTX25_Ripple_v11.safetensors) | `models/loras/LTX/` | 654 MB | Open | Task → Edit With a Picture |
| [ltx-2.5-22b-ic-lora-clean-plate-1.0.safetensors](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Clean-Plate/blob/main/ltx-2.5-22b-ic-lora-clean-plate-1.0.safetensors) | `models/loras/LTX/` | 327 MB | [Lightricks/LTX-2.5-22b-IC-LoRA-Clean-Plate](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Clean-Plate) | Task → Remove People |
| [ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen/blob/main/ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors) | `models/loras/LTX/` | 1.3 GB | [Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen) | Task → Make a Subject Mask |
| [ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control/blob/main/ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors) | `models/loras/LTX/` | 654 MB | Open | Task → Follow Scene Layout, Follow Body Movement, Follow Outlines |
| [moge_2_vitl_normal_fp16.safetensors](https://huggingface.co/Comfy-Org/MoGe/blob/main/geometry_estimation/moge_2_vitl_normal_fp16.safetensors) | `models/geometry_estimation/` | 662 MB | Open | Task → Follow Scene Layout |
| [sdpose_wholebody_fp16.safetensors](https://huggingface.co/Comfy-Org/SDPose/blob/main/checkpoints/sdpose_wholebody_fp16.safetensors) | `models/checkpoints/` | 1.9 GB | Open | Task → Follow Body Movement |

## LTX 2.5 Refine & Upscale

**Node packs:**

| Pack | Search for |
|---|---|
| [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) | `KJNodes` |
| [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo) | `LTXVideo`, the one by LTX.io |
| [RES4LYF](https://github.com/ClownsharkBatwing/RES4LYF) | `RES4LYF` |

Install RES4LYF even when the Launcher reads **Ready**: the workflow uses its
sampling method, and the Launcher can't tell when it's missing. Without it,
**Generate** stops with *ComfyUI didn't accept the run*.

**Model files** (about 50.8 GB in all; **Refining Pass Only** needs 47.6 GB of
them). The first six are the same files as in LTX 2.5 Edit & VFX, so download
them once:

<!-- Sizes from the Hugging Face and GitHub file pages, 2026-11-04. -->

| File | Folder | Size | Access | Needed by |
|---|---|---|---|---|
| [ltx-2.5-22b-dev-transformer-comfy-int8-convrot.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/diffusion_models/ltx-2.5-22b-dev-transformer-comfy-int8-convrot.safetensors) | `models/diffusion_models/` | 21.5 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors) | `models/text_encoders/` | 15.4 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [ltx-2.5-video-vae-conv-bf16.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/vae/ltx-2.5-video-vae-conv-bf16.safetensors) | `models/vae/LTX/` | 1.5 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [ltx-2.5-22b-distilled-lora-450-bf16.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors) | `models/loras/LTX/` | 8.9 GB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Every choice |
| [ltx-2.5-audio-vae-bf16.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/vae/ltx-2.5-audio-vae-bf16.safetensors) | `models/vae/LTX/` | 365 MB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Passes → Refine and Upscale Pass, Refining Pass Only |
| [taeltx2_3.safetensors](https://github.com/madebyollin/taehv/blob/main/safetensors/taeltx2_3.safetensors) | `models/vae_approx/` | 24 MB | Open | Passes → Refine and Upscale Pass, Refining Pass Only |
| [ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors](https://huggingface.co/Lightricks/LTX-2.5/blob/main/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors) | `models/latent_upscale_models/` | 996 MB | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | Passes → Refine and Upscale Pass, Upscale Pass Only |
| [ltx-2.5-22b-ic-lora-refine-details-1.0.safetensors](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Refine-Details/blob/main/ltx-2.5-22b-ic-lora-refine-details-1.0.safetensors) | `models/loras/LTX/` | 1.3 GB | [Lightricks/LTX-2.5-22b-IC-LoRA-Refine-Details](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Refine-Details) | Passes → Refine and Upscale Pass, Upscale Pass Only |
| [ltx-2.5-22b-ic-lora-decompression-0.9.safetensors](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Decompression/blob/main/ltx-2.5-22b-ic-lora-decompression-0.9.safetensors) | `models/loras/LTX/` | 906 MB | [Lightricks/LTX-2.5-22b-IC-LoRA-Decompression](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Decompression) | Passes → Refine and Upscale Pass, Upscale Pass Only |

## MiniMax H3 — References & Swaps

**Node packs:**

| Pack | Search for |
|---|---|
| [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) | `KJNodes` |
| [RES4LYF](https://github.com/ClownsharkBatwing/RES4LYF) | `RES4LYF` |

The H3, SAM3 and LoRA-list nodes come with ComfyUI. If the Launcher lists one
of them as missing, update ComfyUI.

**Model files** (about 56.0 GB without the optional one; **Mask Off** with
**Standard Generation** needs 54.3 GB of them):

<!-- Sizes from the Hugging Face file pages, 2026-11-04. -->

| File | Folder | Size | Access | Needed by |
|---|---|---|---|---|
| [minimax_h3_ref2va_pruned_int8_convrot.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors) | `models/diffusion_models/` | 21.0 GB | Open | Every choice |
| [qwen3vl_32b_minimax_h3_int8_convrot.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors) | `models/text_encoders/` | 27.1 GB | Open | Every choice |
| [minimax_h3_video_vae_fp16.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/vae/minimax_h3_video_vae_fp16.safetensors) | `models/vae/H3/` | 5.2 GB | Open | Every choice |
| [minimax_h3_audio_vae_fp32.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/vae/minimax_h3_audio_vae_fp32.safetensors) | `models/vae/H3/` | 605 MB | Open | Every choice |
| [minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors](https://huggingface.co/Kijai/MiniMax-H3-experimental/blob/main/loras/minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors) | `models/loras/H3/` | 367 MB | Open | Every choice |
| [taeh3.safetensors](https://huggingface.co/Kijai/MiniMax-H3-TAE/blob/main/vae_approx/taeh3.safetensors) | `models/vae_approx/` | 10 MB | Open | Every choice |
| [minimax_h3_character_swap_v1.1_ref2va_fro.safetensors](https://huggingface.co/Alissonerdx/Minimax-H3-ComfyUI/blob/main/loras/minimax_h3_character_swap_v1.1_ref2va_fro.safetensors) | `models/loras/H3/` | 81 MB | Open | Task → Character Swap |
| [minimax_h3_head_swap_v1.0_r32.safetensors](https://huggingface.co/Alissonerdx/BFS-Best-Face-Swap-Video/blob/main/h3/minimax_h3_head_swap_v1.0_r32.safetensors) | `models/loras/H3/` | 310 MB | Open | Task → Head Swap |
| [sam3.1_multiplex_int8_convrot_selective_fp16clip_fp32source.safetensors](https://huggingface.co/Sparknight/sam3.1-int8-int4-convrot/blob/main/sam3.1_multiplex_int8_convrot_selective_fp16clip_fp32source.safetensors) | `models/checkpoints/` | 1.3 GB | Open | Mask → Mask On |
| [minimax_h3_fl2va_pruned_int8_convrot.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors) | `models/diffusion_models/` | 21.0 GB | Open | Optional |

The optional **fl2va** model gives better sound and overall quality, at the
cost of Reference accuracy. To use it, open **Controls**, click **advanced
hidden** below **Modes** if it appears, and select it in **Models and LoRAs →
Video Model**.
