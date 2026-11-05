# Troubleshooting

Fixes for problems you might hit while installing Sonder Editor or working with
projects. For a first setup, see [Getting Started](getting-started.md).

## Installation & environment

- **Controls compatibility:** ComfyUI-Custom-Scripts' **Preset Text** extension
  (`pysssss.PresetText`) can make exposed Controls appear as disabled "Setting"
  rows even when their nodes are still in the workflow.

### The Editor is blank or shows raw controls after installing through Manager

After Manager finishes restarting ComfyUI, refresh the browser page as well
(`F5` or `Ctrl+R`). A backend restart can leave the open page without the
newly installed frontend extensions initialized. Refreshing the page loads
the Editor interface.

### `torch` / `torchaudio` got reinstalled and GPU stopped working

Stop ComfyUI and restore the PyTorch packages required by your ComfyUI
installation and GPU. Use that installation's Python environment, not a
system Python:

1. For a managed installation, use its installer's repair or dependency
   reinstall procedure.
2. For a manual installation, follow the GPU-specific PyTorch commands in
   [ComfyUI's installation instructions](https://docs.comfy.org/installation/manual_install).
   Reinstall the matching `torch`, `torchvision` and `torchaudio` set together.
3. Restart ComfyUI and check its startup output identifies your GPU rather
   than CPU mode.

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

## Logic nodes

### Sonder Switch runs every branch or reports a selected branch unavailable

Update ComfyUI to **0.40.0 or newer**, then restart it. An older scheduler
can run every connected Switch branch.

If it reports `Selected branch 'itemN' is not available (None)`, update
Sonder Editor too and restart ComfyUI. Keep the existing connections.
If a branch still runs, check whether another active node also consumes it.

## Projects & storage

### An older Sonder version refuses a project saved by a newer one

Update Sonder Editor to **0.6.0 or newer** to open a storage-format-3
project. Keep the whole project folder, including `state/`, when moving it.

### A project won't open, save or extract on Windows

Update Sonder Editor to **0.6.0 or newer**, which uses shorter file names.
If the error still reports a path that is too long, move the project to a
shallower folder or enable
[Windows long-path support](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation).
Use a third-party archive tool when Windows **Extract All** fails on long
paths, and extract projects directly into `ComfyUI/output/sonder-projects/`.

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
**Link project folder…** from the project menu and paste the server-visible
path. Local Windows drives use a junction without elevated privileges; UNC
paths need a true symlink, which requires Windows Developer Mode or an elevated
ComfyUI process. Do not toggle the setting while a render or Save Bridge job is
in flight; its finalization safely remains pending until the matching path is
trusted again. The Launcher's **New project → Link existing** does the same and
shows the project before you link it.

### Link project folder and the external links switch are greyed out

Both work only in a browser on the ComfyUI machine itself, because they let the
server read and write folders outside its projects folder. From another
computer, through a reverse proxy, or on a headless server, either open the
editor on that machine, or stop ComfyUI and set
`"allow_external_project_links": true` in `ComfyUI/user/sonder-editor/settings.json`,
then link the folder by creating a junction or symlink to it inside the
Sonder projects folder. Turning links off and unlinking work from anywhere.

## Sonder App

### Install in Settings → Sonder App is refused or doesn't finish

Install adds `comfy-sdk` and `comfy-api-proxy` at pinned versions to ComfyUI's
own Python and never changes a package that is already there. What each message
means:

- **"Install and Uninstall work only in a browser on the ComfyUI machine."**:
  they work only in a browser on the computer running ComfyUI, opened at `127.0.0.1` or
  `localhost` and not through a reverse proxy or tunnel. Other browsers can see
  the status but not change it.
- **"pip isn't available in ComfyUI's Python"**: run one of the two commands
  shown (pip or uv) yourself; they target ComfyUI's interpreter.
- **"… is already installed, and the Sonder App needs …"**: another version of
  that package is installed, and Install only adds the exact pinned versions.
  Any `comfy-sdk` 0.4.x and `comfy-api-proxy` 0.1.x already work, so if yours is
  in that range, install only the missing package by hand. Otherwise update it
  yourself if nothing else needs that version.
- **"Installing would change packages ComfyUI already has"**: the packages
  would upgrade or downgrade something ComfyUI uses, so Sonder stopped. Install
  them by hand only if you accept that change.
- **"pip couldn't plan the install."**, **"pip couldn't install the
  packages."** or **"pip finished, but the installed versions don't match"**:
  the detail below shows pip's own error, often a network problem or no package
  build for this Python. Fix that and run Install again, or install by hand.
- **"An install didn't finish"**: ComfyUI stopped during an install. Run Install
  again, or use the command shown.
- **Still running after ten minutes**: pip is slow or waiting on the network.
  It can't be stopped from the editor; it finishes or fails on its own.

Uninstall removes only the packages Sonder installed, still at the version it
installed, and only if nothing else needs them. Dependencies it added are kept
and listed. It refuses while Sonder App takes are still generating (it waits up
to 30 seconds for them first): wait for them or cancel them, then uninstall. A
package that was already loaded stays in memory until ComfyUI restarts.

### Generate says "The Sonder App couldn't connect to ComfyUI"

Generate runs through a small local proxy (`comfy-api-proxy`) that Sonder starts
on demand on the first free port from 8189 to 8198, or reuses if one there
already serves this ComfyUI. The detail under the message gives the reason:

- **"Could not start the SDK proxy on ports 8189–8198"**: either every port in
  that range is taken by something else, or the proxy Sonder started didn't
  come up within 15 seconds. Check the proxy log (below); if the ports are the
  problem, free one and Generate again.
- **"Sonder App does not support ComfyUI over HTTPS yet"**: ComfyUI was started
  with `--tls-keyfile`/`--tls-certfile`. Start it without them to use the
  Sonder App.
- Anything else: a proxy that Sonder started logs to your ComfyUI user folder
  under `sonder-editor/logs/sdk_proxy.log`.

To use a proxy you run yourself, set `SONDER_SDK_PROXY_URL` (and
`SONDER_SDK_PROXY_TOKEN` if it needs one) before starting ComfyUI. It must be on
this machine unless you also set `SONDER_SDK_ALLOW_REMOTE=1`, and it must serve
this same ComfyUI.

### The Launcher says *Missing node packs*

The workflow's current Modes use nodes this ComfyUI doesn't have. The Launcher
marks it **Missing nodes**, and the bar at the bottom says so in red, lists the
node packs that provide them and names the Mode choice that needs them. Without
ComfyUI-Manager it lists the node names instead. **Open** still works: under
**Modes**, the choices that need a missing node are
[greyed out](controls.md#in-the-sonder-app), and you can choose the others.
To use those Modes, install the packs:

1. Find the workflow's canvas file: the `.json` shipped next to the API file
   you imported. For a supplied workflow under **Starters**, open it instead:
   click **Templates** in ComfyUI's left bar, then **sonder-editor** under
   **EXTENSIONS**, click the template with the workflow's name, and go on to
   step 3.
2. Open the ComfyUI page in your browser and drag that file onto it.
3. A **Missing node** message appears at the top right. Click
   **View details**. The **Workflow Overview** panel opens on its **Issues**
   tab.
4. Click **Install All**, or **Install** beside each pack. Pack names there
   can differ slightly from the Launcher's.
5. Restart ComfyUI when asked, then reload the Launcher page. The choices that
   needed them are no longer greyed out.

A pack the list names as **no known pack** isn't in ComfyUI-Manager's index.
Search its node name on [registry.comfy.org](https://registry.comfy.org) or
ask the workflow's author. ComfyUI-PromptRelay, for **Timed Prompts**, installs by
following [its repository's instructions](https://github.com/kijai/ComfyUI-PromptRelay).

If you have only the API file, or your ComfyUI runs without ComfyUI-Manager
(no **Install** buttons appear), install each listed pack by hand, following
that pack's own instructions, then restart ComfyUI. To see pack names instead of
node names, start ComfyUI with its `--enable-manager` option and reload the
Launcher.

Models are checked only for nodes that are installed. After the packs are in,
select the workflow again: their loaders may list more missing models.

**Missing models** are listed beside the workflow when you select it, each with
the place it goes. To install them, follow
[Install Models and Node Packs](sonder-app-setup.md#the-steps). A model whose
row says **folder unknown** belongs to a loader Sonder can't place; check the
node's own documentation.

### The Launcher shortcut stopped working after changing the port

Copy **Sonder Launcher.url** again from your Sonder projects folder
(`ComfyUI/output/sonder-projects/` unless you changed it). Sonder updates that
file to the new port each time ComfyUI starts; copies made earlier keep the old
address.

## Playback

### A clip shows a proxy notice ("Proxy queued", "No proxy yet"…) instead of its picture

Your browser can't play that video's format, and its
[proxy](assets-and-gallery.md#proxies) isn't ready yet.

- To make it now: select the video in the gallery and click **Build now**
  under the notice in the inspector, or right-click it and choose **Build
  Proxy Now**.
- **Building proxy…**: wait.
- **Proxy queued**: wait, or build it now as above.
- **Proxy waiting for ComfyUI**: proxies build once ComfyUI has been idle for
  30 seconds. To build them while you generate, set **Settings ▸ Proxies ▸
  Build Proxies** to **Right away**.
- **No proxy yet**: the project is set to **Only when I ask**, or its proxy was
  deleted. Build it as above.
- **Proxy failed**: right-click the video and choose **Build Proxy Now** to try
  again. If it fails again, check the ComfyUI console for an `ffmpeg` error
  (see [`ffmpeg` not found](#ffmpeg-not-found--export-or-decode-fails)).

### Video does not play in the editor when ComfyUI runs with `--disable-api-nodes`

Update ComfyUI to **0.40.0 or newer** and restart it; there the option is called
`--offline` and video plays. On an older ComfyUI, start it without the
option. If the browser console still reports a `media-src` policy violation,
check the policy added by your proxy or another extension; it must allow
`blob:` media.
