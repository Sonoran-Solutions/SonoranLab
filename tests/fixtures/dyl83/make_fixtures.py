"""DYL-83 golden fixtures: a tiny SYNTHETIC 24x24 figure (no game pixels or palette), one PNG per frame.

`clean/<direction>_<i>.png` is a passing 4-frame loop (east, south). Each `<case>/` dir holds only the frames that
case replaces; tests/sonoranlab/test_validate.py adds the structural cases (a dropped frame, a clipped registration,
mirrored west rows). Regenerate with `python tests/fixtures/dyl83/make_fixtures.py`; the test checks the PNGs on disk
equal `build()`.

Figure: body x 7..16 / y 5..19 (RED, INK outline, SKIN face, GOLD 2x2 accessory, BLUE belt that alternates sides
each frame), 1 px INK arms x 2..6 and 17..21 on row 11, foot run x 10..13 on row 20. Cell 24 -> foot_y 20, anchor_x
12, padding 2: the arms end exactly at the padding.
"""

from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
CELL, FOOT_Y, ANCHOR_X, PADDING, FRAMES = 24, 20, 12, 2, 4
INK, RED, SKIN, BLUE = (17, 13, 12), (204, 34, 34), (230, 180, 140), (40, 60, 200)
GOLD, GREEN, PURPLE = (220, 180, 40), (40, 160, 60), (120, 60, 160)
PALETTE = np.array([INK, RED, SKIN, BLUE, GOLD, GREEN, PURPLE], np.uint8)  # GREEN, PURPLE: unused by `clean`
MAX_COLOURS = 6  # clean uses 5, so one off-palette pixel stays under the cap
OFF = (200, 30, 30)  # not in the palette
FRINGE = (240, 10, 200)  # a blend toward the #ff00ff key


def figure(i: int) -> np.ndarray:
    f = np.zeros((CELL, CELL, 4), np.uint8)
    f[5:20, 7:17] = (*INK, 255)
    f[6:19, 8:16] = (*RED, 255)
    f[6:9, 9:15] = (*SKIN, 255)
    f[15:17, 11:13] = (*GOLD, 255)
    f[12, (9, 10) if i % 2 == 0 else (13, 14)] = (*BLUE, 255)
    f[11, 2:7] = f[11, 17:22] = (*INK, 255)
    f[20, 10:14] = (*INK, 255)
    return f


def clean() -> dict[str, list[np.ndarray]]:
    return {d: [figure(i) for i in range(FRAMES)] for d in ("south", "east")}  # profile sheet order


def overrides() -> dict[str, dict[str, np.ndarray]]:
    """case -> {"<direction>_<i>": replacement frame}."""
    out: dict[str, dict[str, np.ndarray]] = {}

    def put(case: str, name: str, f: np.ndarray) -> None:
        out.setdefault(case, {})[name] = f

    f = np.roll(figure(2), -2, axis=0)  # whole frame 2 px up: bbox top and bottom move 2
    put("shifted", "east_2", f)
    f = figure(1)
    f[20, 10:14] = 0
    f[20, 14:17] = (*INK, 255)  # planted foot 3.5 px right of the anchor, inside the body's columns
    put("foot_slide", "east_1", f)
    f = figure(1)
    f[13, 12] = (*OFF, 255)  # interior
    put("off_palette", "east_1", f)
    f = figure(2)
    f[14, 7] = (*FRINGE, 255)  # body outline pixel with a transparent left neighbour
    put("fringe", "east_2", f)
    f = figure(1)
    f[13, 12, 3] = 128
    put("semi_alpha", "east_1", f)
    f = figure(2)
    f[11, 1] = (*INK, 255)  # arm 1 px into the padding (bbox moves 1 px: within the drift limit)
    put("touches_edge", "east_2", f)
    put("canvas", "east_1", np.pad(figure(1), ((0, 0), (0, 1), (0, 0))))
    f = figure(2)
    f[13, 12] = (*GREEN, 255)
    f[13, 11] = (*PURPLE, 255)  # 7 colours, limit 6
    put("colours", "east_2", f)
    for i in range(FRAMES):
        put("foot_line", f"south_{i}", np.roll(figure(i), -1, axis=0))  # the whole south clip 1 px up
    for i in (1, 2):
        f = figure(i)
        f[15:17, 11:13] = (*RED, 255)
        put("accessory", f"east_{i}", f)
    for i in (2, 3):
        f = figure(i)
        f[2:5, 14] = (*INK, 255)  # 1 px blade above the head in frames 2-3: drift on entry (2) and at the seam (0)
        put("blade", f"east_{i}", f)
    return out


def build() -> dict[str, dict[str, np.ndarray]]:
    """dir name -> {file stem: frame}: `clean` plus every case's overrides."""
    return {"clean": {f"{d}_{i}": f for d, fs in clean().items() for i, f in enumerate(fs)}, **overrides()}


def main() -> None:
    for case, frames in build().items():
        (HERE / case).mkdir(exist_ok=True)
        for stem, f in frames.items():
            Image.fromarray(f).save(HERE / case / f"{stem}.png")


if __name__ == "__main__":
    main()
