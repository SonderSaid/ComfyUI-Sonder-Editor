# Prompt chip selection (Firefox and Chromium)

A real browser check of the shared Context-chip editor (`web/js/prompt_context_chips.js`). It is dev-only and runs outside pytest. It mounts one real `createPromptDocumentEditor` on a blank page served from this repository, then drags, presses keys and reads the model back.

It was moved from the 2026-11-20 Firefox probe. The probe's live mode, which drove a running ComfyUI, is not part of this harness; live checks run on a test server.

## Run

```
node tests/browser/prompt_chip_selection/run.mjs --browser=all
node tests/browser/prompt_chip_selection/run.mjs --browser=firefox --headed
node tests/browser/prompt_chip_selection/clipboard.mjs
```

The runner needs:
- Node 22 or later (global `WebSocket` for Firefox BiDi);
- installed Firefox (`FIREFOX_BIN` overrides `C:/Program Files/Mozilla Firefox/firefox.exe`);
- installed Google Chrome and a `playwright-core` package, given as `PLAYWRIGHT_CORE=<path>` or found in the npx cache.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | PASS |
| 1 | FAIL |
| 77 | SKIPPED: a browser or driver is missing. A skip is never a pass. |

Results go to `results-<browser>.json`, which is ignored by git.

`SONDER_JS_DIR=<dir>` points the page at another copy of `web/js`, for example a release extracted with `git archive v0.9.0 web/js`, to check whether a case failed there.

## Criteria

**Controls** (pass today; they guard against regressions). An ordinary drag from prose to prose across chips, then Delete, Backspace or Cut. The expected text remains, focus stays in the editor, and one Ctrl+Z restores the text and every attachment record.

**Older selection, then a drag from a chip.** Select prose across both chips, then drag from chip C's label into the prose before chip D. Then press Delete, Backspace or Cut. These criteria come from the Prompt Tool Integrity Pass, Phase 3, decision 3: a press on a chip collapses any older selection beside the chip.
- After the chip press, the older selection is no longer the live one.
- Chip D and the prose after the new drag remain.
- Focus stays in the editor.
- One Ctrl+Z restores the document.

**The 2026-11 user report** (kind `report`): "highlight half the text that contains chips and cut/delete it ... more text than you had highlighted was deleted ... Ctrl+Z didn't save me". Two sets:
- The older-selection class, widened: an older selection made four ways (two drags, Shift+arrows, cut then Undo), then a click or drag starting on chip C or D, then Delete, Backspace or Cut (60 cases). The prose outside the press's own span and the other chip survive, focus stays in the editor, and one Ctrl+Z restores. On 0.9.0 all 60 fail in Firefox 160 and 56 in Chrome 157 (Chrome keeps focus, so its Ctrl+Z works, but the older range still goes), matching the report.
- A partial highlight across chips, without an older selection: exactly the highlighted prose and the chips between the endpoints go, the field's prose equals the model's text (what is saved), and Ctrl+Z restores. These pass on 0.9.0 too: the report's loss needs the older selection.
- Since the Chip Focus plan (2026-11-23), a press that is a **drag** from a chip leaves a collapsed text caret where it was released. Its Delete or Backspace removes exactly one character there, Cut changes nothing, both chips stay and focus is in the editor. The span checks above apply to the click presses. "Older selection, Tab to a chip, then typing" keeps the older range and types `x` after chip C.

**Chip focus** (kind `focus`, the Firefox trailing-chip report, 2026-11-23). After each gesture plus a frame, `x` is typed (or text pasted) and its place is read from the model:
- a click just right of a trailing chip, and of a chip that ends its line, for a pill and for a handle (`@Hero`) chip: `x` lands after the chip;
- the same for the last of a run of chips, a chip alone on its line, a chip soft-wrapped to a new line, a handle whose label wraps, and a click in the padding above; a click on the line above a soft-wrapped chip stays on that line;
- a drag from a trailing chip to the blank beside or below it, and to outside the field: focus in the editor, `x` after the chip;
- Tab to a chip, then ←/→: `x` before/after it; then `@` and a letter: they continue after it;
- Tab to a chip, then Ctrl+V of text copied in the page (the editor's copy fills the browser's clipboard, so `--headed` may write the system one), with and without an older range live: the paste lands after the chip and the older range stays;
- a chip's settings closed with Cancel or Escape: `x` after the chip;
- a chip's settings open: typing, Ctrl+V, Ctrl+X, Delete, Enter and an arrow change nothing, open nothing and leave focus on the chip, with and without an older range live. The stand-in dialog takes no focus, as the real one does not, and registers its Escape with the product's keyboard owner;
- Space on a chip opens its settings and types nothing.

On HEAD before the fix (`SONDER_JS_DIR`), 35 of the 62 checks of this kind fail across both browsers (20 of 31 in Firefox, 15 in Chromium), and so do the 72 drag presses of the `report` kind and all 8 `stale` checks, which state the new drag rule. Still passing there: the arrow keys (the selection already moved focus), Space, the Firefox line-ending chip, and Chromium's clicks other than the wrapped handle, which already landed after the chip.

Known driver limit: WebDriver BiDi navigation keys (End, Home, arrows) do not move a native caret in Firefox on any page. The arrow cases pass because the editor moves the caret itself; native End past a chip is untested. Neither driver produces dead keys or IME input, so those are untested too.

## Baseline (2026-11-20, before Phase 3)

| Browser | Controls | Older selection + chip drag |
|---|---|---|
| Firefox 160 | pass | **fail**: the older selection stays live, the old range is deleted, focus falls to BODY, and Ctrl+Z misses |
| Chrome 157 | pass | **fail**: the older selection stays live and the old range is deleted; focus and Undo hold |

After Phase 3 (2026-11-21), both browsers pass every case, so the runner exits 0. A failure now is a regression. After the Chip Focus plan (2026-11-23), 117 cases pass in Firefox 160 and Chrome.

## Clipboard qualification

`clipboard.mjs` checks whether a custom clipboard type written in `copy`/`cut` comes back in `paste`, for Phase 3's chip clipboard. It covers the same tab, a second tab, Ctrl+Shift+V, another application reading the clipboard, and a paste after another application copied. It runs headed, because the system clipboard is the subject, and it records rather than judges. Results go to `clipboard-results.json`. It replaces the system clipboard. Saved text is put back afterwards, byte for byte (read as UTF-8 base64); an empty or non-text clipboard is cleared.

Result on 2026-11-20 (Firefox 160.0.1, Chrome 157, Windows):
- The custom type survives copy and cut into the same tab and into a second tab.
- Ctrl+Shift+V delivers `text/plain` only.
- Another application reads the plain text.
- After another application copies, no fragment remains.
