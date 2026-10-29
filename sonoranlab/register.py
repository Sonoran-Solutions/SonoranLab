"""Clip registration on the foot line / rig anchor (CLAUDE.md non-negotiable 3), never centre of mass.

One (dx, dy) for the whole clip, so a frame's bob relative to its neighbours survives. Plain numpy, no profile.
Parity reference: `benchmark/tools/process.py::register_clip` (processing-v1).
"""

from __future__ import annotations

import math

import numpy as np

MODES = ("foot", "bbox_bottom", "rig")


def bbox(f: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) inclusive of the opaque pixels, or None for an empty frame."""
    ys, xs = np.nonzero(f[..., 3])
    return None if len(xs) == 0 else (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def _place(f: np.ndarray, cell_w: int, cell_h: int, dx: int, dy: int) -> np.ndarray:
    """f shifted by (dx, dy) onto a transparent cell_w x cell_h canvas; whatever falls outside is cut."""
    c = np.zeros((cell_h, cell_w, 4), np.uint8)
    h, w = f.shape[:2]
    sx0, sy0 = max(0, -dx), max(0, -dy)
    sx1, sy1 = min(w, cell_w - dx), min(h, cell_h - dy)
    if sx1 > sx0 and sy1 > sy0:
        c[sy0 + dy : sy1 + dy, sx0 + dx : sx1 + dx] = f[sy0:sy1, sx0:sx1]
    return c


def register_clip(
    frames: list[np.ndarray],
    cell: tuple[int, int],
    *,
    foot_y: int,
    anchor_x: int,
    mode: str = "foot",
    anchors: list[tuple[float, float]] | None = None,
    anchor_frame: int | None = None,
) -> tuple[list[np.ndarray], dict]:
    """Register one clip (one direction) into `cell` = (width, height) frames; returns (frames, info).

    `foot` / `bbox_bottom` (identical rule; a floating subject has no foot zone, so its bbox bottom, the lowest opaque
    row, is the anchor): dy moves the clip's lowest opaque row to `foot_y`, dx moves the MEDIAN per-frame bbox
    centre (x0 + x1 + 1) / 2 to `anchor_x` (round half up). `rig`: `anchors` holds one (x, y) per frame in that
    frame's own pixel coordinates; dx moves the median anchor x to `anchor_x`, dy moves the largest anchor y to
    `foot_y`. Frames may be any size; output is exactly the cell. Pixels pushed outside it are counted per frame in
    `clipped_px` (the validator turns nonzero into a failure). Info: `mode`, `shift_px` [dx, dy], `raw_lowest_rows`,
    `raw_shifts_px` (the shift each frame alone would need, to tell model drift from a fixed clip), `clipped_px`.
    `anchor_frame`: dx comes from that frame alone instead of the median. A non-looping clip (cast, death) starts
    from the approved still pose at frame 0 and must stay where the still and idle stand; a median over an action
    that throws a weapon or the body sideways would shift the whole clip and the character would jump when the action
    starts. dy is unchanged (the clip's lowest row on the foot line).
    An empty frame returns blank cells and `{"error": "empty frame", "shift_px": None, "empty_frames": [i, ...]}`.
    """
    if mode not in MODES:
        raise ValueError(f"unknown registration mode {mode!r}; expected one of {', '.join(MODES)}")
    if not frames:
        raise ValueError("register_clip needs at least one frame")
    cw, ch = cell
    if mode == "rig":
        if anchors is None or len(anchors) != len(frames):
            raise ValueError(
                f"rig mode needs one (x, y) anchor per frame: {len(frames)} frames, "
                f"{'no anchors' if anchors is None else f'{len(anchors)} anchors'}"
            )
    elif anchors is not None:
        raise ValueError(f"anchors are only used in rig mode, not {mode!r}")
    boxes = [bbox(f) for f in frames]
    empty = [i for i, b in enumerate(boxes) if b is None]
    if empty:
        blank = [np.zeros((ch, cw, 4), np.uint8) for _ in frames]
        return blank, {"mode": mode, "error": "empty frame", "shift_px": None, "empty_frames": empty}
    lowest = [b[3] for b in boxes]
    if mode == "rig":
        xs, ys = [float(a[0]) for a in anchors], [float(a[1]) for a in anchors]
    else:
        xs, ys = [(b[0] + b[2] + 1) / 2 for b in boxes], lowest
    if anchor_frame is not None and not 0 <= anchor_frame < len(frames):
        raise ValueError(f"anchor_frame {anchor_frame} is outside the clip's {len(frames)} frames")
    dx = round_half_up(anchor_x - (xs[anchor_frame] if anchor_frame is not None else float(np.median(xs))))
    dy = round_half_up(foot_y - max(ys))
    out = [_place(f, cw, ch, dx, dy) for f in frames]
    return out, {
        "mode": mode,
        "shift_px": [dx, dy],
        **({"anchor_frame": anchor_frame} if anchor_frame is not None else {}),
        "raw_lowest_rows": lowest,
        "raw_shifts_px": [
            [round_half_up(anchor_x - x), round_half_up(foot_y - y)] for x, y in zip(xs, ys, strict=True)
        ],
        "clipped_px": [int((f[..., 3] > 0).sum() - (c[..., 3] > 0).sum()) for f, c in zip(frames, out, strict=True)],
    }
