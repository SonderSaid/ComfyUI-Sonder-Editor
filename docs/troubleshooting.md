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

## Logic nodes

### Sonder Switch runs every branch or reports a selected branch unavailable

Sonder Switch's branch skipping requires ComfyUI **0.40.0 or newer**. Older
ComfyUI cores can evaluate every connected branch before the Switch selects its
output because their scheduler misses lazy metadata on growing inputs. Sonder
does not work around that upstream scheduling defect. A branch also used by
another active consumer may still run independently of the Switch.

If ComfyUI 0.40+ reports `Selected branch 'itemN' is not available (None)`, update
Sonder Editor as well, then restart ComfyUI. Older Sonder builds did not recognize
the executor's lazy-input placeholders, a defect exposed by the scheduler fix.
The fix preserves existing Switch sockets and connections; rewiring is not needed.

This is a Switch-specific scheduling requirement, not a minimum ComfyUI version
for the entire node pack. Cluster and Gate use fixed inputs and are unaffected
by this growing-input issue.

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

### Why do Sonder App takes show no embedded workflow?

The Sonder App submits the workflow's API form, without the canvas workflow that
ComfyUI normally embeds. So a take's details say the workflow is **Not
embedded**, and Open Source Workflow is unavailable for it. Metadata Collector
sections are titled by their wired label, else the node's class name, because
Sonder reads node titles from the canvas workflow; a value from inside a subgraph is
listed under the inner node rather than the subgraph. **Controls at run** is
still recorded, with the Sonder App's labels, groups and seed modes.

### How do I share a workflow curated in the Sonder App?

Use the workflow's **⋮ → Export as API** action in the Launcher, then import
the downloaded JSON into another Sonder App. It includes current Control values
and seed modes, active app-only Controls, labels, notes, groups, Advanced flags
and the workflow description. Hidden entries are omitted; disabled authored
Controls stay disabled. Export reports unreadable Relays or a menu/file that
exceeds import limits instead of downloading a partial file.

Loading API JSON on the ComfyUI canvas flattens subgraphs and can lose some
custom-node settings. Controls can be reconnected with **Restore Controls**
using the steps below. Share the saved UI workflow alongside the API JSON when
the recipient needs the canvas.

Replacing an existing Sonder App import keeps your Control values and seed
modes, app-only Controls, hidden settings and the presets you edited (Reset to
workflow's version brings back the imported one). A saved value whose setting
now points somewhere else resets to the workflow's value. It resets your
labels, notes, order, groups and Advanced flags on the workflow's own Controls,
and the app description. The Launcher's notice lists what was kept and reset.

### Reconnect Controls after loading an API file

1. Load the API JSON on the ComfyUI canvas. Find the **Sonder Editor** node,
   select your project on its card, then click **Open Editor**.
2. Open the **Controls** tab in the editor's sidebar. In the notice at the top,
   click **Restore Controls**. The same notice works in a mounted editor tab.
3. Alternatively, stay on the canvas and find the **Sonder Control Relay** node
   (search for **Control Relay** if it is off-screen). Click its
   **Restore N Controls from this file** button. An open editor is not required.
4. Restore reconnects every setting it can verify, including uniquely matched
   settings from flattened subgraphs. It leaves values, labels and presets
   unchanged. To undo the whole restore, return focus to the canvas and press
   **Ctrl+Z** (**⌘+Z** on macOS) once.
5. If the message says some Controls **need Rebind**, open **Controls → Manage**.
   Click **Rebind** beside each remaining item and choose its setting. Ambiguous,
   unavailable, already-used and multi-setting matches need this explicit choice.
6. Save the UI workflow for future canvas use. Export (API) again to share the
   restored Controls and their presets with the Sonder App.

Editing or saving before Restore keeps the recovery hints. An API export made
before Restore also keeps them, but its Controls honestly remain missing in the
Sonder App and direct you back to Restore on the canvas. **Clean up unavailable…**
keeps automatically restorable Controls out of its deletion list; unmatched
items remain available for explicit cleanup.

**Exposed before** contains saved canvas anchors for Controls removed from the
menu. Open **Manage → Add settings → Exposed before → Restore all** to expose
them again; above 64 items the button reads **Restore 64**, and can be repeated.
An API file's Exposed-before suggestions are not recovery hints: Restore Controls
recovers listed Controls, not those suggestions. It also does not rebuild
subgraphs or custom-node settings lost during ComfyUI's API import.

### Where is the Launcher shortcut, and why does it stop working after a port change?

On Windows, installing the Sonder App saves **Sonder Launcher.url** in your
Sonder projects folder (`ComfyUI/output/sonder-projects/` unless you changed it). Copy it wherever you like; double-clicking it
opens the Launcher in your default browser while ComfyUI is running. Sonder
rewrites the file each time ComfyUI starts, so it follows a new `--port`, but a
copy you made elsewhere keeps the old address: copy it again after changing the
port. On macOS and Linux there is no shortcut file; bookmark the Launcher page
(`http://127.0.0.1:<port>/sonder-editor/app`) instead.

## Playback

### Video does not play in the editor when ComfyUI runs with `--disable-api-nodes`

That option makes ComfyUI replace the editor's content security policy with one
that blocks the video the editor loads for playback. Start ComfyUI without it.
