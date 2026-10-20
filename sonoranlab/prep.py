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


def _require_alpha(path: Path, cell: int) -> Image.Image:
    """The still must be exactly cell x cell and carry alpha; returns a detached RGBA copy."""
    with Image.open(path) as im:
        if im.size != (cell, cell):
            raise ValueError(f"{path}: still is {im.width}x{im.height}, expected {cell}x{cell}")
        if "A" not in im.getbands() and "transparency" not in im.info:
            raise ValueError(f"{path}: still has no alpha channel (expected an RGBA cell-size still)")
        return im.convert("RGBA")


def prepare_reference_1024(
    still_rgba: Path, cell: int, out: Path, *, background: str = "#808080", scale: int = 3, canvas: int = 1024
) -> dict[str, Any]:
    """Track C image2: the character still, nearest xscale, composited on `background`, centred on a canvas x canvas
    RGB image. Qwen Edit's encoder rescales every input to ~1 MP, so a 1024 canvas keeps the subject at its x3 size."""
    bg = parse_hex(background)
    big = _require_alpha(still_rgba, cell).resize((cell * scale,) * 2, Image.NEAREST)
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


def prepare_pose_1024(pose_png: Path, cell: int, out: Path, *, scale: int = 3, canvas: int = 1024) -> dict[str, Any]:
    """Track C image1: a Track D OpenPose render (square, an integer multiple k of `cell`), LANCZOS-resized to
    cell x scale and centred on a black canvas, exactly like benchmark/tools/prep_inputs.py's `place(..., (0,0,0))`."""
    with Image.open(pose_png) as im:
        size = im.size
        pose = im.convert("RGB")
    if size[0] != size[1]:
        raise ValueError(f"{pose_png}: pose render is {size[0]}x{size[1]}, expected a square")
    if size[0] % cell:
        raise ValueError(f"{pose_png}: pose render edge {size[0]} is not a multiple of cell {cell}")
    small = pose.resize((cell * scale,) * 2, Image.LANCZOS)
    page = Image.new("RGB", (canvas, canvas), (0, 0, 0))
    page.paste(small, ((canvas - small.width) // 2, (canvas - small.height) // 2))
    with open(out, "xb") as f:
        page.save(f, format="PNG")
    return {
        "path": out,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "canvas": canvas,
        "source_scale": size[0] // cell,
    }


def start_image_from_rgba(rgba: Path, out: Path, *, background: str = "#808080") -> dict[str, Any]:
    """Track C result -> Track E start image: composite an RGBA frame on `background`, same size, RGB PNG.
    No resize here: the caller passes the still/pose pair that Track E's own `prepare_start_image` handles."""
    bg = parse_hex(background)
    with Image.open(rgba) as im:
        if "A" not in im.getbands() and "transparency" not in im.info:
            raise ValueError(f"{rgba}: frame has no alpha channel (expected the RGBA Track C output)")
        frame = im.convert("RGBA")
    flat = Image.new("RGBA", frame.size, (*bg, 255))
    flat.alpha_composite(frame)
    with open(out, "xb") as f:
        flat.convert("RGB").save(f, format="PNG")
    return {
        "path": out,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "size": list(frame.size),
        "background": background,
    }
