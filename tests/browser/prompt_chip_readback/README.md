# Prompt readback (Firefox 159, Firefox 160, Chromium)

A real-browser check that the shared Context-chip editor (`web/js/prompt_context_chips.js`) keeps what the author typed, and maps every selection the browser reports. It is dev-only, runs outside pytest, and reuses the drivers of `../prompt_chip_selection`.

It mounts one real `createPromptDocumentEditor` the way the Firefox 159 repro did: `compact`, 211 or 241 px wide, scrolled to the end, `@` mention menu armed. Nothing on the page reads layout while an edit is handled, because the defect only shows when the editor's own `input` handler is the first reader.

## Run

```
node tests/browser/prompt_chip_readback/run.mjs --browser=all
FIREFOX_BIN="F:/Claude/Agent Tools/Firefox 159/app/core/firefox.exe" node tests/browser/prompt_chip_readback/run.mjs --browser=firefox --label=ff159
SONDER_JS_DIR=<dir with web/js of a release> node tests/browser/prompt_chip_readback/run.mjs --label=v092
```

- `--no-active-window`: by default a headless Firefox is made the active window, as the 159 audit ran it.
- `--label=<name>`: results go to `results-<label>.json`, which git ignores.
- Exit codes: 0 PASS, 1 FAIL, 77 SKIPPED. A skipped browser makes the run 77 even when another browser passed: a skip is never a pass.

Firefox 159.0 is unpacked side by side on the maintainer's machine at `F:\Claude\Agent Tools\Firefox 159\app\core\firefox.exe`, with its own profile and updates off.

## Cases

- **readback** (16): long (817), terminal-mention (450), multiline and short (40) prompts.
  - Placing the caret: 14 cases place it at the very end by script; two (long and terminal, 241 px) use a real click just right of the last character. A case counts only if the caret is at the end before typing.
  - Then `X` or ` This line ends with @Test` is typed. The model must equal the prompt plus what was typed.
- **mention-range** (15): `keep @Test plus this whole suffix` is selected from offset 5 to the end of `@Test`, in three DOM shapes: (mark, 1), (span, 2), and the mark's text at 5.
  - Then Delete, Backspace, Enter, a typed `Z` or Ctrl+X. Only `@Test` may go.
  - A case fails if the browser hands the selection to the editor in any other shape than the one it names.
  - The text-node shape and typing are controls: native typing never went through the mapping.
- **mention-caret** (3): Enter with a collapsed caret at (mark, 1); Backspace at (mark, 0) after a chip; Delete at (mark, 1) before a chip. The break lands at the caret, and one character goes, never the chip. The same shape check applies.
- **native-shape** (4):
  - Backspace the field empty: the model must be `""`.
  - Backspace through a mention.
  - Two spaces typed at a line end.
  - A rich-markup drop. A browser that makes no native drop records `produced: false`.

**Oracle.** A capture-phase `input` listener on the host clones every text span before the editor's own handler runs. It reads structure only. After the case, each clone is read twice and differences are reported, not judged:
- by the production reader `promptSpanAuthoredText` (the fixture's own structural read when the module does not export it, as in 0.9.2);
- by `innerText`, mounted offscreen at the field's width and font.

These differences are intended, because the reader is structural by design:
- `<p>` gives one break, where `innerText` gives two;
- `style`, `script` and `hidden` content is skipped;
- table cells give `\t`.

## Results (2026-11-23, Windows, after the implementation audit)

| Browser | 0.9.2 (`git archive v0.9.2 web/js`) | Working tree |
|---|---|---|
| Firefox 159.0 | 11/38 | 38/38 |
| Firefox 160.0.1 | 27/38: the 11 mention cases fail; readback passes | 38/38 |
| Chrome 157 | 27/38: the same 11 | 38/38 |

**Firefox 159 on 0.9.2.**
- 15 of the 16 readback cases lose text. Two examples: 818 characters typed, model 806, ending "a total of 817 "; and the terminal-mention prompt at 445 of 451.
- The two real-click cases lose text too. The short prompt with `X` passes.
- All 11 mention cases fail.
- So does the two-space case, which loses the same tail.

**Oracle.** About 205 span shapes were captured per browser run. There were no differences from `innerText` on any run.

**Drop.**
- Chrome produced a native drop. It inserted the rich source as `<span>`/`<b>` wrappers with a line break between the paragraphs, and left out the `<style>` and `hidden` content itself.
- On the working tree that span was repainted at once, and the model matched the field. On 0.9.2 the wrappers stayed in the field, and the case passed there too, so the drop case does not discriminate.
- Firefox produced no drop through BiDi.
- The reader's skip rules for `style` and `hidden` are therefore covered only by the unit tests.

The browsers kept every element-shaped selection as set, so each 0.9.2 failure went through the editor's mapping. `../prompt_chip_selection` (117 cases) passes on the working tree in Firefox 159, Firefox 160 and Chrome.
