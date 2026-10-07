"""Rebuild web/assets/sonder.ico from the Sonder mark SVGs.

Development-only (``tools/`` is in ``.comfyignore``). Run it with any Python that
has Pillow, on Windows with Microsoft Edge installed:

    python tools/icons/build_icons.py

The SVGs are rasterized by headless Edge at 512 px (no SVG library is needed),
then Pillow resizes them: 16 and 24 px come from ``sonder-mark-small.svg``, the
larger sizes from ``sonder-mark.svg``. Every size sits on the same dark rounded
tile as ``sonder-favicon.svg``, because the light S disappears on light desktops
and tab strips. Review the 16 and 24 px renders before committing a change.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "web" / "assets"
RENDER = 512
SIZES = (16, 24, 32, 48, 64, 128, 256)
SMALL_MAX = 24
TILE_FILL = (12, 16, 21, 255)      # editor_theme.js THEME.bg0
TILE_EDGE = (44, 55, 68, 255)      # THEME.line2
TILE_RADIUS = 0.22                 # matches sonder-favicon.svg (rx 88 of 400)
MARK_SCALE = 0.84                  # matches sonder-favicon.svg's inner scale


def find_edge() -> str:
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if base and candidate.is_file():
            return str(candidate)
    found = shutil.which("msedge")
    if not found:
        sys.exit("Microsoft Edge was not found; it rasterizes the SVGs.")
    return found


def rasterize(edge: str, svg: Path, work: Path) -> Image.Image:
    page = work / f"{svg.stem}.html"
    page.write_text(
        "<!doctype html><html><head><style>html,body{margin:0;padding:0;background:transparent;"
        f"overflow:hidden}}img{{display:block;width:{RENDER}px;height:{RENDER}px}}</style></head>"
        f"<body><img src=\"{svg.as_uri()}\"></body></html>",
        encoding="utf-8",
    )
    shot = work / f"{svg.stem}.png"
    subprocess.run(
        [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
         f"--user-data-dir={work / 'profile'}", f"--window-size={RENDER},{RENDER}", f"--screenshot={shot}",
         page.as_uri()],
        check=True, timeout=120, capture_output=True,
    )
    return Image.open(shot).convert("RGBA")


def on_tile(mark: Image.Image, size: int) -> Image.Image:
    tile = Image.new("RGBA", (RENDER, RENDER), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, RENDER - 1, RENDER - 1), radius=int(RENDER * TILE_RADIUS),
        fill=TILE_FILL, outline=TILE_EDGE, width=max(2, RENDER // 64))
    inner = mark.resize((int(RENDER * MARK_SCALE),) * 2, Image.LANCZOS)
    tile.alpha_composite(inner, ((RENDER - inner.width) // 2,) * 2)
    return tile.resize((size, size), Image.LANCZOS)


def main() -> None:
    edge = find_edge()
    with tempfile.TemporaryDirectory(prefix="sonder-icons-") as tmp:
        work = Path(tmp)
        full = rasterize(edge, ASSETS / "sonder-mark.svg", work)
        small = rasterize(edge, ASSETS / "sonder-mark-small.svg", work)
    images = [on_tile(small if size <= SMALL_MAX else full, size) for size in SIZES]
    out = ASSETS / "sonder.ico"
    images[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    # Pillow before 8.1 ignores append_images and shrinks the 256 px frame instead,
    # which would silently replace the simplified small mark: check every frame.
    with Image.open(out) as ico:
        for size, expected in zip(SIZES, images):
            ico.size = (size, size)
            if ico.convert("RGBA").tobytes() != expected.tobytes():
                sys.exit(f"{out}: the {size} px frame is not the one built; update Pillow and rerun.")
    print(f"Wrote {out} ({out.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
