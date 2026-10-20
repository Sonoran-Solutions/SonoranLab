"""Track E start-image staging: the approved cell-size still, nearest x3, on a flat background, centred on the canvas.

Same math as benchmark/tools/prep_inputs.py except the background is a parameter (grey by default, ADR-0004).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor

_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def gen_canvas(cell: int, scale: int = 3) -> int:
    """Generation canvas edge: cell * scale rounded up to a multiple of 16 (Wan/VAE alignment)."""
    return -(-cell * scale // 16) * 16


def parse_hex(background: str) -> tuple[int, int, int]:
    if not _HEX.fullmatch(background):
        raise ValueError(f"background {background!r} must be #rrggbb")
    return ImageColor.getrgb(background)[:3]


def prepare_start_image(
    still_rgba: Path, cell: int, out: Path, *, background: str = "#808080", scale: int = 3
) -> dict[str, Any]:
    """Exclusively create `out` (RGB PNG, canvas x canvas). Refuses a still that is not cell x cell or has no alpha."""
    bg = parse_hex(background)
    with Image.open(still_rgba) as im:
        if im.size != (cell, cell):
            raise ValueError(f"{still_rgba}: still is {im.width}x{im.height}, expected {cell}x{cell}")
        if "A" not in im.getbands() and "transparency" not in im.info:
            raise ValueError(f"{still_rgba}: still has no alpha channel (expected an RGBA cell-size still)")
        big = im.convert("RGBA").resize((cell * scale,) * 2, Image.NEAREST)
    canvas = gen_canvas(cell, scale)
    flat = Image.new("RGBA", big.size, (*bg, 255))
    flat.alpha_composite(big)
    page = Image.new("RGB", (canvas, canvas), bg)
    page.paste(flat.convert("RGB"), ((canvas - big.width) // 2, (canvas - big.height) // 2))
    with open(out, "xb") as f:
        page.save(f, format="PNG")
    return {
        "path": out,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "canvas": canvas,
        "background": background,
    }
