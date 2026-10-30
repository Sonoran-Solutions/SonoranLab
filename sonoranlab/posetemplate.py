"""Render an OpenPose control image for any cell from a pose template's canvas-independent joints.

Pixel math lives here, not in a prompt (CLAUDE.md #1). This mirrors tools/godot/bake_rig.gd `_draw_pose`:
filled-ellipse limbs at 0.6x colour, then full-colour joint discs, on black; null face points skipped.
Joint mapping is the template's `to_canvas`: s = W/cell, px = W/2 + x*s, py = (cell - 3)*s - h*s.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image

# Same lists as bake_rig.gd (ControlNet / DWPose draw_bodypose).
LIMBS = [(1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9), (9, 10), (1, 11), (11, 12), (12, 13),
         (1, 0), (0, 14), (14, 16), (0, 15), (15, 17)]  # fmt: skip
COLORS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0), (0, 255, 0),
          (0, 255, 85), (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255),
          (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85)]  # fmt: skip
CONTROL_SCALE = 4  # control canvas = cell * 4, the scale of the shipped 544 px (cell 136) renders


def load_template(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(Path(path).read_text())
    if not isinstance(doc, dict) or "frame_sets" not in doc:
        raise ValueError(f"{path}: not a pose template")
    return doc


def joints_px(template: dict[str, Any], direction: str, frame: int, cell: int, canvas: int, frame_set: int = 6):
    """COCO-18 keypoints in canvas pixels (None for hidden points)."""
    try:
        kp = template["frame_sets"][frame_set][direction][frame]["kp"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"template has no frame {frame} for {direction!r} in the {frame_set}-frame set") from exc
    s = canvas / cell
    return [None if p is None else (canvas / 2 + p[0] * s, (cell - 3) * s - p[1] * s) for p in kp]


def _ellipse(buf: np.ndarray, c: tuple[float, float], axis: tuple[float, float], minor: float, col) -> None:
    size = buf.shape[0]
    length = math.hypot(*axis)
    major = max(length * 0.5, minor)
    ux, uy = (axis[0] / length, axis[1] / length) if length > 0 else (1.0, 0.0)
    r = math.ceil(major) + 1
    y0, y1 = max(0, math.floor(c[1]) - r), min(size, math.floor(c[1]) + r + 1)
    x0, x1 = max(0, math.floor(c[0]) - r), min(size, math.floor(c[0]) + r + 1)
    if y0 >= y1 or x0 >= x1:
        return
    ys, xs = np.mgrid[y0:y1, x0:x1]
    dx, dy = xs + 0.5 - c[0], ys + 0.5 - c[1]
    a = (dx * ux + dy * uy) / major
    b = (-dx * uy + dy * ux) / minor
    buf[y0:y1, x0:x1][a * a + b * b <= 1.0] = col


def render_openpose(
    template: dict[str, Any], direction: str, frame: int, cell: int, canvas: int | None = None, frame_set: int = 6
) -> Image.Image:
    """RGB OpenPose render of one template frame on a square black `canvas` (default cell * CONTROL_SCALE)."""
    canvas = canvas or cell * CONTROL_SCALE
    kp = joints_px(template, direction, frame, cell, canvas, frame_set)
    buf = np.zeros((canvas, canvas, 3), np.uint8)
    sw = max(1.0, math.floor(4.0 * canvas / 512.0 + 0.5))
    for (i, j), col in zip(LIMBS, COLORS, strict=False):
        a, b = kp[i], kp[j]
        if a is None or b is None:
            continue
        _ellipse(
            buf, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), (b[0] - a[0], b[1] - a[1]), sw, [int(v * 0.6) for v in col]
        )
    for p, col in zip(kp, COLORS, strict=True):
        if p is not None:
            _ellipse(buf, p, (0.0, 0.0), sw, col)
    return Image.fromarray(buf, "RGB")


def control_png(template_yaml: Path, direction: str, frame: int, cell: int) -> bytes:
    """Deterministic PNG bytes of the control image Track C wants for a `cell` x `cell` character."""
    buf = io.BytesIO()
    render_openpose(load_template(template_yaml), direction, frame, cell).save(buf, format="PNG")
    return buf.getvalue()
