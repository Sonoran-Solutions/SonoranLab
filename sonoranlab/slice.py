"""Read raw frame dirs, slice sheets into frames and pack registered frames into the game sheet layout.

Read-only on inputs. Frames are `(H, W, 4)` uint8 RGBA arrays. Mirroring lives here (not in register.py): west rows
are the horizontal flip of their east source, packed at sheet time; a generated direction is never mirrored.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

FRAME_RE = re.compile(r"^([a-z]+(?:-[a-z]+)*)_(\d+)\.png$")
ROW_RE = re.compile(r"^[a-z]+(-[a-z]+)*$")


def _check_rows(rows: Sequence[str]) -> None:
    for row in rows:
        if not isinstance(row, str) or not ROW_RE.fullmatch(row):
            raise ValueError(f"row {row!r}: name must match {ROW_RE.pattern}")


def read_frame_dir(path: Path, directions: Sequence[str]) -> dict[str, list[Path]]:
    """Find `<direction>_<i>.png` in `path`: {direction: paths in index order}. Directions with no files are absent.

    ValueError naming the file for a frame-like name with an unknown direction, a duplicate index (`east_1`,
    `east_01`) or an index gap (indices must be 0..n-1). Other files (still_south.png, json) are ignored.
    """
    _check_rows(directions)
    found: dict[str, dict[int, Path]] = {}
    for p in sorted(Path(path).glob("*.png")):
        m = FRAME_RE.match(p.name)
        if not m:
            continue
        if m[1] not in directions:
            raise ValueError(f"{p}: unknown direction {m[1]!r}; expected one of {', '.join(directions)}")
        i = int(m[2])
        if i in found.setdefault(m[1], {}):
            raise ValueError(f"{p}: duplicate index {i} for {m[1]} (also {found[m[1]][i].name})")
        found[m[1]][i] = p
    out: dict[str, list[Path]] = {}
    for d in directions:
        idx = found.get(d)
        if not idx:
            continue
        missing = sorted(set(range(max(idx) + 1)) - set(idx))
        if missing:
            raise ValueError(f"{Path(path) / f'{d}_{missing[0]}.png'}: missing; {d} frames must be 0..{max(idx)}")
        out[d] = [idx[i] for i in range(len(idx))]
    return out


def load_clips(paths: dict[str, list[Path]]) -> dict[str, list[np.ndarray]]:
    """Read every file as an RGBA uint8 array (RGB inputs get alpha 255; alpha extraction is alpha.py's job)."""
    clips = {}
    for d, ps in paths.items():
        clips[d] = []
        for p in ps:
            with Image.open(p) as im:
                clips[d].append(np.asarray(im.convert("RGBA")).copy())
    return clips


def slice_sheet(
    sheet: np.ndarray, cell: tuple[int, int], rows: Sequence[str], frames: int, *, start_col: int = 0
) -> dict[str, list[np.ndarray]]:
    """Cut a fixed grid: one row per direction in `rows` order, `frames` cells from column `start_col`.

    The sheet must be exactly len(rows) * cell_h high and (start_col + frames) * cell_w wide.
    """
    _check_rows(rows)
    cw, ch = cell
    want = (len(rows) * ch, (start_col + frames) * cw)
    if sheet.ndim != 3 or sheet.shape[2] != 4 or sheet.shape[:2] != want:
        raise ValueError(
            f"sheet is {sheet.shape[1]}x{sheet.shape[0]}, expected {want[1]}x{want[0]} "
            f"({start_col + frames} columns x {len(rows)} rows of {cw}x{ch})"
        )
    return {
        d: [sheet[r * ch : (r + 1) * ch, (start_col + i) * cw : (start_col + i + 1) * cw].copy() for i in range(frames)]
        for r, d in enumerate(rows)
    }


def slice_boxes(sheet: np.ndarray, boxes: dict[str, list[tuple[int, int, int, int]]]) -> dict[str, list[np.ndarray]]:
    """Cut explicit (x, y, w, h) boxes per direction; ValueError for a box that is empty or leaves the sheet."""
    H, W = sheet.shape[:2]
    out = {}
    for d, bs in boxes.items():
        out[d] = []
        for i, (x, y, w, h) in enumerate(bs):
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > W or y + h > H:
                raise ValueError(f"{d} box {i} {(x, y, w, h)} is empty or outside the {W}x{H} sheet")
            out[d].append(sheet[y : y + h, x : x + w].copy())
    return out


def pack_sheet(
    clips: dict[str, list[np.ndarray]],
    cell: tuple[int, int],
    rows: Sequence[str],
    frames: int,
    *,
    mirror: dict[str, str],
    stills: dict[str, np.ndarray] | None = None,
    start_col: int = 1,
    required: Sequence[str] = (),
) -> np.ndarray:
    """Game layout: len(rows) rows in `rows` order, column 0 = still (if given), clip frames from `start_col`.

    `mirror` maps {mirrored: source}; a row absent from `clips` but in `mirror` is the horizontal flip of its source.
    ValueError for: a row in both `clips` and `mirror` (the game mirrors west from east; asymmetric gear is never
    re-mirrored), a clip direction not in `rows`, a `required` row with neither frames nor a mirror source, a wrong
    frame count or frame/still size. Rows with nothing to draw (and not required) stay transparent.
    """
    _check_rows(rows)
    cw, ch = cell
    both = sorted(set(clips) & set(mirror))
    if both:
        raise ValueError(
            f"{', '.join(both)}: has generated frames and a mirror source; mirrored rows are never generated"
        )
    stray = sorted(set(clips) - set(rows))
    if stray:
        raise ValueError(f"clip direction(s) {', '.join(stray)} are not in the sheet rows")
    if stills and start_col < 1:
        raise ValueError("stills need start_col >= 1 (column 0)")
    out = np.zeros((len(rows) * ch, (start_col + frames) * cw, 4), np.uint8)
    for r, d in enumerate(rows):
        src = clips.get(d)
        if src is None and mirror.get(d) in clips:
            src = [f[:, ::-1] for f in clips[mirror[d]]]
        if src is None:
            if d in required:
                raise ValueError(f"required row {d} has no frames and no mirror source")
            src = []
        elif len(src) != frames:
            raise ValueError(f"{d}: {len(src)} frames, expected {frames}")
        cells = [(f"{d} frame {i}", start_col + i, f) for i, f in enumerate(src)]
        if stills and d in stills:
            cells.append((f"still {d}", 0, stills[d]))
        for name, c, f in cells:
            if f.shape != (ch, cw, 4) or f.dtype != np.uint8:
                raise ValueError(f"{name}: shape {f.shape} dtype {f.dtype}, expected ({ch}, {cw}, 4) uint8")
            out[r * ch : (r + 1) * ch, c * cw : (c + 1) * cw] = f
    return out
