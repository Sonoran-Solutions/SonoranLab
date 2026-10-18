"""Slice a Track A/B grid sheet into process.py's RAW/<direction>_<i>.png frames (disposable Phase 1 tooling).

svntax grids: A = 768^2, 6 cols x 4 rows (3 walk, 2 attack, 1 hurt); B = 512^2, 4 x 4 (3 walk, 1 pose). Rows are
down, left, right, up -> south, west, east, north. Only the walk columns are kept (the locomotion clip). Non-square
cells are padded to a square, bottom-aligned and centred, so process.py's square-cell chain applies unchanged.
The svntax LoRAs draw on a flat light background, not our key, so each cell's background is flood-filled from
its four corners (connected pixels only, --bg-tol) with #ff00ff before padding: process.py's chroma key then applies.
Left (west) is kept as its own file for the record; process.py's game layout mirrors west from east anyway.

    uv run --no-project --with pillow python -I benchmark/tools/slice_grid.py SHEET.png OUT_RAW --cols 6 --rows 4
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

ROWS = ["south", "west", "east", "north"]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sheet", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--cols", type=int, required=True)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--walk-cols", type=int, default=3)
    ap.add_argument("--bg-tol", type=int, default=40, help="flood-fill threshold from the cell corners")
    a = ap.parse_args(argv)
    im = Image.open(a.sheet).convert("RGB")
    cw, ch = im.width // a.cols, im.height // a.rows
    side = max(cw, ch)
    a.out.mkdir(parents=True, exist_ok=False)   # never overwrite a previous slice
    for r, direction in enumerate(ROWS[: a.rows]):
        for c in range(a.walk_cols):
            cell = im.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
            for xy in ((0, 0), (cw - 1, 0), (0, ch - 1), (cw - 1, ch - 1)):
                ImageDraw.floodfill(cell, xy, (255, 0, 255), thresh=a.bg_tol)
            sq = Image.new("RGB", (side, side), (255, 0, 255))
            sq.paste(cell, ((side - cw) // 2, side - ch))
            sq.save(a.out / f"{direction}_{c}.png")
    print(f"{a.rows * a.walk_cols} frames, cell {cw}x{ch} -> {side}^2 in {a.out}")


if __name__ == "__main__":
    main()
