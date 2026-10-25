# Output Views linked-playback fixture (Phase 3B)

A real browser check of linked output panes. It is dev-only and runs outside pytest.

It uses the real code:
- `viewport_surface.js` plays a 24 fps scene: a barcode composite clip and a timeline audio track.
- Three output panes at 12, 25 and 30 fps are mounted by `output_pane_media.js`.
- Each pane is linked by `view_timing.js`, follows the clock through `clock_follower.js` and the page budget (`aux_media_budget.js`), and plays under the audio arbiter (`audio_ownership.js`).

Every video frame paints its own index as a stripe barcode, so a pixel read tells which source frame is showing. Each file carries its own tone.

## Run

```
"<test venv>/Scripts/python.exe" -I tests/browser/views_barcode/make_media.py
node tests/browser/views_barcode/run.mjs
```

The runner needs:
- installed Google Chrome, which plays H.264 (Playwright's own Chromium does not);
- a `playwright-core` package, given as `PLAYWRIGHT_CORE=<path>` or found in the npx cache.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | PASS |
| 1 | FAIL |
| 77 | SKIPPED: no media, no Chrome, or no playwright-core. A skip is never a pass. |

Results go to `results.json`, which is ignored by git along with `media/`.

## Criteria

These are fixture criteria, not a product frame-accuracy claim.

- **Paused frames.** 32 random paused frames read exact by pixel, for the Composite and for each pane. The expected source frame is `floor((f + 0.5)·Fm/24)`.
- **Stall recovery.** Two 220 ms stalls on the 30 fps pane: a frozen decode, and a seek that takes 220 ms to land. After each, the pane is back within 2 periods of the slowest source (2/12 s) within 1 s.
- **The editor clock never rebuffers** because of a pane.
- **Audio.** A calibration first checks that the owner reads loud and muted elements read silent. Then exactly one source is heard through Listen and Mute: the timeline, a pane, another pane, then the timeline again.
- **Recorded, not judged:**
  - steady-state p95 error per pane;
  - Catching-up reports;
  - presented frame rates with the Composite visible and parked (decision D2).
