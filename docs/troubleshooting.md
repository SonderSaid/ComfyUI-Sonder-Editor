# Troubleshooting

Fixes for problems you might hit while installing Sonder Editor or working with
projects. For a first setup, see [Getting Started](getting-started.md).

## Installation & environment

### The Editor is blank or shows raw controls after installing through Manager

After Manager finishes restarting ComfyUI, refresh the browser page as well
(`F5` or `Ctrl+R`). A backend restart can leave the open page without the
newly installed frontend extensions initialized. Refreshing the page loads
the Editor interface.

### `torch` / `torchaudio` got reinstalled and GPU stopped working

ComfyUI ships a torch build matched to your GPU/CUDA. This pack intentionally
does **not** list `torch`/`torchaudio` in its requirements so an automatic
`pip install` can't overwrite that build with a mismatched (often CPU-only)
wheel. Sonder's audio decode, mixing and export use FFmpeg. An optional waveform
thumbnail fallback can use ComfyUI's existing `torchaudio` installation.

### `cv2` import errors after installing another custom node

This pack uses `opencv-python-headless` (no GUI dependencies, correct for a
server). Some other custom nodes install the full `opencv-python` package, and
the two conflict — whichever was installed last wins, and the other's `cv2` can
break. If you hit this, pick one variant for your whole environment (headless is
the safe choice for ComfyUI) and reinstall it so it's the only OpenCV present.

### `ffmpeg` not found / export or decode fails

Install FFmpeg **7.0 or newer** and make sure it's on your `PATH`, then restart ComfyUI.
Audio preparation checks the selected binary; an older system installation takes
precedence over the bundled binary and must be updated or removed from `PATH`. The
bundled `imageio-ffmpeg` binary is used as a fallback, but a system ffmpeg is
more capable across formats.

## Projects & storage

### An older Sonder version refuses a project saved by a newer one

Sonder 0.6.0 moves projects to storage format 3 the first time it saves them;
after that they need 0.6.0 or newer. Keep the whole project folder, including
`state/`, when moving it.

### A project won't open, save or extract on Windows

Windows limits file paths to 260 characters unless
[long-path support](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation)
is enabled. This is a Windows limit, not specific to Sonder. Since 0.6.0 Sonder
keeps its own file names short to stay well inside it, but a deeply nested
ComfyUI install can still reach it. Enabling long-path support is the
recommended fix. Windows' built-in compressed-folder tool (**Send to →
Compressed folder** and **Extract All**) ignores long-path support even when
it's enabled, so use a third-party archive tool to compress or extract
projects. Otherwise, keep ComfyUI in a short location and extract downloaded
projects directly into `ComfyUI/output/sonder-projects/`. If a project already
fails to open, move it somewhere shallower and open it there.

### An export says the files were saved but registration could not be confirmed

The exported video and audio are finished and safe — the message names their
paths inside the project. Only the project bookkeeping failed, so press
**Refresh** in the gallery and the files are registered as basic assets; their
generation details and automatic take placement are not recovered. On Windows
this is usually the 260-character path limit, which the message calls out when
it can detect it: move or rename the project so its folder path is shorter, or
enable long-path support in Windows, then export again.

### I can't link a project on another drive or a UNC share

Enable **Allow External Project Links** in Editor Settings first, then use
**Link project folder...** from the project menu and paste the server-visible
path. Local Windows drives use a junction without elevated privileges; UNC
paths need a true symlink, which requires Windows Developer Mode or an elevated
ComfyUI process. Do not toggle the setting while a render or Save Bridge job is
in flight; its finalization safely remains pending until the matching path is
trusted again.

## Playback

### Video does not play in the editor when ComfyUI runs with `--disable-api-nodes`

That option makes ComfyUI replace the editor's content security policy with one
that blocks the video the editor loads for playback. Start ComfyUI without it.
