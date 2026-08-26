# References

A **Reference** is a reusable piece of a project — a character, a location, a
prop, an outfit — kept in one place and used wherever it applies.

Three surfaces divide the work, and it helps to keep them apart:

| Surface | What it is for |
|---|---|
| **Reference Library** — sidebar | Registering and managing References and their media. |
| **Reference lanes and recipes** — timeline | When a Reference applies, and how it is served to your graph. |
| **Reference Prompting** — Prompt Management | Naming References in prompts, and deriving text from them dynamically. |

Assets themselves are covered in [Assets & Gallery](assets-and-gallery.md),
timeline gestures in [Editor Basics](editor-basics.md), and generation in
[Generating](generating.md).

## Reference Library

Switch the fullscreen or mounted left sidebar from **Assets** to
**References**.

- Create a named **character**, **location**, **prop**, or **outfit**, choose
  whether it is a **subject** or **context**, and give it a one-line
  description. Locations begin as context; the other kinds begin as subjects,
  and either can be changed.
- Cards show their kind, class, description, member count, and unresolved
  state. **Manage** reveals entity Edit and Delete.
- Add one image, audio, or video asset at a time. The picker can **Inspect** a
  source without selecting it. After selection, only compatible built-in tags
  are offered; custom tags remain unrestricted.
- Members can carry tags, prompt text, an image/video crop, and an
  audio/video start/end trim. A blank end uses the rest of the source.
- **Crop & Preview**, **Trim & Preview**, or **Crop, Trim & Preview** opens the
  focused visual editor. **Full Source** shows editable crop/trim geometry;
  **Applied Result** shows only the cropped or trimmed result. Audio edits its
  trim on the waveform and seeks on a separate bar; video uses the same
  transport, with a waveform when it has audio.
- Drag a crop's interior to move it, its edges or corners to resize. Choose the
  same ratio presets used by the timeline, **Free**, or **Custom** with a
  width/height ratio such as `3 × 10`. Trim handles resize the interval;
  dragging its middle moves it without changing its duration.
- **Space** plays or pauses the active view, and focused crop and trim controls
  take Arrow-key nudges — the full list is in the editor's **?** shortcut
  atlas. **Apply to Draft** returns your edit to the member; nothing reaches
  the project until you Save.
- **Edit**, **Replace**, **Remove**, and **Up/Down** act on individual members,
  with explicit Save and Cancel.
- Search matches reference names, kinds, descriptions, member tags, and current
  asset names. Missing, Trashed, and unresolved members stay visible.

## Tags

Tags are how a member says what it *is* — a face close-up, a turnaround, a
voice. They do two jobs.

**They make the Library readable.** A wall of near-identical thumbnails is hard
to scan; tags give each member a label you can search on, alongside names,
kinds, and descriptions.

**They let a recipe ask for what it needs.** Each recipe carries the tags it
works best with, and a lane whose staged members don't include one raises a
suggestion to stage it. LTX Best Face ID asks for a face close-up; MiniMax H3
Pictures asks for identity, environment, style, or motion references. These are
suggestions, not requirements — nothing is blocked, and the only hard limit is
the recipe's member cap.

Built-in tags are media-aware, so once you pick an asset only the tags that fit
it are offered: Voice Identity takes standalone audio, and video only when the
clip has embedded audio. Custom tags are free text and carry no media rules.

Tags never reach the model on their own. They describe a member and guide
staging; what the model receives is its media and its prompt text.

## Reference lanes

A Reference lane holds **Reference items** — ranges saying which Library
members apply over which frames, say this character's face plus this outfit
from frame 0 to 120.

The item points at the Library rather than holding a copy of anything. Edit a
member there and every item using it follows; move or trim an item and you
change *when* those references apply, never the media itself. An item always
carries at least one member, and a blank end runs to scene end.

Each lane holds one media kind, so items can't move between an image lane and
an audio lane. Items on a lane can't overlap. Each item can be muted and
carries its own conditioning strength. The lane header shows the lane's recipe
name; item bars show their member names, then tags.

Create items by dragging from the **References** sidebar onto the timeline; the
drop rules are in
[Editor Basics › Reference items](editor-basics.md#reference-items).

A Reference lane header's **☰** (also on its right-click menu) opens the lane
Setup overlay — staged members, their order, and the recipe that decides how
they're assembled for the model.

## Recipes and lane setup

A lane's **recipe** decides how its staged members become model input. Open it
from the lane header's **☰**.

Every recipe starts with an **Assembly**, the choice that shapes everything else:

| Assembly | What the model receives |
|---|---|
| **Batch** | Each member stays a separate image, stacked into one batch — a set of identities. |
| **Sheet** | Members are composited into one image, a panel grid or a vertical strip, for models that read a single reference image. |
| **Temporal** | Members are laid out along time, each holding a contiguous run of frames. |
| **Slots** | Each member goes out on its own numbered socket (`r01`, `r02`, …) for models that take separately wired inputs. |
| **Audio** | The member is trimmed audio rather than an image; only the audio outputs carry anything. |

Recipes that ship built in:

| Recipe | Media | Assembly | Members |
|---|---|---|---|
| LTX Multiple Subject Reference | image | temporal | 5 |
| LTX IC-LoRA Ingredients | image | sheet | 16 |
| LTX Best Face ID | image | sheet | 4 |
| LTX ID-LoRA Voice Identity | audio | audio | 16 |
| Wan VACE Reference Sheet | image | sheet | 16 |
| Wan Phantom Identities | image | batch | 4 |
| Wan SCAIL Identities | image | batch | 6 |
| Wan Bernini-R References | image | slots | 8 |
| MiniMax H3 Pictures | image | slots | 9 |
| MiniMax H3 Videos (IMAGE sequences) | image | slots | 3 |
| MiniMax H3 Standalone Audio | audio | audio | 3 |

A built-in recipe is read-only, so a lane naming one always matches what that
node expects. **Edit as custom** forks it into a project recipe you can change.

The overlay shows only the controls your Assembly actually uses, grouped as
Assembly, Members, Geometry, Frame grid, Frame rate, Bridge outputs, Prompt,
Prompt Context, and Advisories. Staged members, ranges, state, and the advisory
count stay visible; the rest expands when you want it.

Some values are **pegged** rather than typed — the frame grid and snap multiple
follow the scene's model template, a sheet's loop length follows the render
window. A pegged value names the source it follows, and falls back to its
authored number when that source is unset.

## What a render window resolves to

Staging an item doesn't guarantee the model sees it. For the current generation
window every staged item gets a verdict, shown on the timeline and in the lane
panel alike:

| Verdict | Meaning |
|---|---|
| **In window** | Most-specific-wins resolved this item; it's the one the model receives. |
| **Superseded** | Another item on this lane covers this window more tightly, so that one is sent instead. |
| **Below threshold** | The window covers too little of this item's own span, so nothing is sent. |
| **Outside window** | The item doesn't overlap the window. |
| **Excluded** | Muted, or on a hidden lane, so it never participates. |

**Reference Threshold %** (Settings, project-wide) is what drops an item whose
own span the window barely touches. Unlike prompts, this can leave a lane with
*nothing* — the lane reports no reference at all, which is exactly what makes a
reference stop applying outside its scope.

With no selection nothing is marked, since the marks answer "what will this
render use", which isn't a question until a window exists.

Queueing a **batch** predicts all of this per chunk before anything runs, and
says whether a lane drops because of the threshold or because that chunk falls
outside the staged range. A lane that resolves in no chunk always warns.

## Prompts from References

References reach a prompt two ways, and they suit different jobs.

### The lane's derived prompt

A staged item carries prompt text as well as media, assembled from the members
themselves so it tracks what you stage.

The lane panel shows it in one of two states. **Prompt — derived from members**
is read-only, built from each member's own text through the recipe's pattern,
and follows every staging change. **Copy** takes the text; **Edit as override**
hands you the same text as an editable draft. Once overridden the header reads
**Prompt — overridden**, the derived version stays visible beside it, and
**Clear** goes back to following the members.

Two recipe fields shape the derived text:

- **Prompt prefix** — static text placed once at the front, not repeated per
  member.
- **Per-member token** — a pattern expanded once per staged member.
  Placeholders are `{n}` (member number from 1), `{index}` (from 0), `{prompt}`
  (the member's own text), `{name}` (its prompt-safe `Entity_Member` label),
  and `{entity_name}` / `{member_name}` for the pieces separately. So
  `<Subject {n}> is {prompt}, from <Picture {n}>` composes a sentence rather
  than prefixing a token. With no `{prompt}` or `{name}`, the member text is
  appended after the pattern.

Each expansion also goes out on its own numbered Prompt Bridge output, so a
graph can wire one member's text separately from the aggregate.

### Attaching a Context chip

A fixed pattern only goes so far. When the wording depends on which References
actually win the window, or the provider expects numbered labels you would
otherwise count by hand, **attach a Context chip to the prompt field** instead.
**+ Attach** picks the target Reference or identity and configures its
overrides in the same dialog; the chip then resolves against the selected
render window when the prompt compiles.

This is how **MiniMax H3 (full reference)** prompts are built. That format
declares subject, picture, video, and audio populations, and the chips take
their ordinals from the compiled setup rather than from anything you number
yourself.

**Reference Prompting** in Prompt Management is where this is authored. It shows
the physical media resolved from the active setup and window, and is where you
give each member the handle and prompt text its chips will use. Staging stays
in Reference lanes — Reference Prompting never moves anything on the timeline.

Beside it, **Identity Prompting** authors semantic prompt identities —
provider-neutral records that may combine several Library members or exist from
a description alone. An identity is not a Reference, and the Library never
creates one for you; see [Prompts](prompts.md).

### Mentions

Reference members and prompt identities share one set of **handles**. Typing
`@Anna` in any prompt field compiles to whatever label your prompt format uses,
and the editor marks it so you can see it is live.

Mentions are ordinary text, which is what lets them survive copy, cut, and
paste. Prose can name a handle that doesn't exist yet and wire itself up when
you create it. An `@` that isn't one of your handles — an email address
included — is left exactly as you wrote it.

Reference **Context chips**, which carry the same intent without typing
provider ordinals by hand, are covered in
[Prompts](prompts.md#how-the-output-prompt-is-composed).

## Wiring the nodes

Four nodes carry References into a graph: the **Selector** resolves lanes
without decoding anything, and the three **Bridges** decode what it resolved.

```
Sonder Editor ──project──> Reference Selector ──reference_set──> Image Bridge
                                    │                            Audio Bridge
                                    └── has_reference ── gate ──> Prompt Bridge
```

**Sonder Reference Selector** takes the editor's `project` output, and its own
panel is where you choose **one or more Reference lanes** to send on. `+` lists
the lanes available, each chosen lane becomes a removable row, and several
lanes combine into one ordered set. A lane that no longer exists is skipped
rather than quietly substituting another.

It emits:

- `reference_set` — fan out to whichever Bridges you need.
- `has_reference` — `0` when no selected lane has an item effective for this
  window, `1` when any does. Gate the branch holding the Bridges on this so it
  genuinely does not execute.
- `reference_strength` — the authored strength of the first effective lane in
  lane order, or `0.0`.

Co-selected lanes must agree. A candidate whose media kind, materialized
recipe, or effective prompt override differs from the lanes already selected is
listed but disabled, with the reason shown; a blank or detached recipe can only
be selected alone.

Each Bridge exposes 16 numbered outputs:

| Bridge | Outputs |
|---|---|
| **Image** | `r01`–`r16` |
| **Audio** | `a01`–`a16` |
| **Prompt** | `reference_prompt`, `reference_names`, then `p01`–`p16` |

Slot order follows staged member order, and lanes concatenate in lane-index
order. Non-slot assemblies emit one payload per lane; slot recipes emit one
member per output.

**Slots never move.** A selected lane reserves its widest staged member count
whether or not it is currently effective, so an inactive earlier lane leaves
its outputs reading `(unused)` rather than shifting later lanes down — a
socket's position is what ComfyUI type-checks against.

The Image and Audio Bridges take an **`unused_slots`** choice for outputs the
recipe does not drive:

- **`placeholder`** (default) — a black scene-size image, or a one-second
  silent stereo track. A consumer whose input is *required* needs this.
- **`nothing`** — no value at all, which a consumer with an *optional* input
  skips entirely. Use it when a placeholder would be read as real content.

A dead slot wired to a proven-required input reads `(unused · required input)`,
so the mismatch shows on the canvas.

To wire a full block up front: stage members to the recipe's cap, wire every
output you want, then remove the surplus. The connected slots stay visible, so
the links hold.
