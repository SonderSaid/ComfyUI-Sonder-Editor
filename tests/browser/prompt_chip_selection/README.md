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

## Baseline (2026-11-20, before Phase 3)

| Browser | Controls | Older selection + chip drag |
|---|---|---|
| Firefox 160 | pass | **fail**: the older selection stays live, the old range is deleted, focus falls to BODY, and Ctrl+Z misses |
| Chrome 157 | pass | **fail**: the older selection stays live and the old range is deleted; focus and Undo hold |

After Phase 3 (2026-11-21), both browsers pass every case, so the runner exits 0. A failure now is a regression.

## Clipboard qualification

`clipboard.mjs` checks whether a custom clipboard type written in `copy`/`cut` comes back in `paste`, for Phase 3's chip clipboard. It covers the same tab, a second tab, Ctrl+Shift+V, another application reading the clipboard, and a paste after another application copied. It runs headed, because the system clipboard is the subject, and it records rather than judges. Results go to `clipboard-results.json`. It replaces the system clipboard. Saved text is put back afterwards, byte for byte (read as UTF-8 base64); an empty or non-text clipboard is cleared.

Result on 2026-11-20 (Firefox 160.0.1, Chrome 157, Windows):
- The custom type survives copy and cut into the same tab and into a second tab.
- Ctrl+Shift+V delivers `text/plain` only.
- Another application reads the plain text.
- After another application copies, no fragment remains.
