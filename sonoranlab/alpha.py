"""Alpha stages of the per-frame chain (processing-v1 parity): key extraction, despill, block downscale, fringe.

Ported from `benchmark/tools/process.py` with the same math and tie-breaks; the background key and every tolerance are
plain parameters so the pipeline (DYL-86) can map them from the profile. No function here mutates its inputs.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

MAGENTA = (255, 0, 255)  # the only key despill knows; profiles/twos-are-up.yaml alpha.bg_key


def extract_alpha(im: Image.Image, bg_key: tuple[int, int, int], key_tol: float) -> tuple[np.ndarray, np.ndarray]:
    """HD RGB composited over `bg_key`, and HD alpha: the image's own alpha if it has any, else a chroma key.

    RGBA-with-soft-alpha input is flattened onto the key so despill and the palette see one colour space; otherwise a
    pixel within Euclidean distance `key_tol` of the key in RGB is transparent (RGB-on-key renders)."""
    a = np.asarray(im.convert("RGBA")).astype(np.float32)
    rgb, alpha = a[..., :3], a[..., 3]
    if im.mode in ("RGBA", "LA", "PA") and (alpha < 255).any():
        w = alpha[..., None] / 255
        rgb = rgb * w + np.array(bg_key, np.float32) * (1 - w)
    else:
        alpha = np.where(np.linalg.norm(rgb - np.array(bg_key, np.float32), axis=-1) <= key_tol, 0.0, 255.0)
    return np.rint(rgb).astype(np.uint8), alpha


def despill(
    rgb: np.ndarray, alpha: np.ndarray, band: int, bg_key: tuple[int, int, int] = MAGENTA
) -> tuple[np.ndarray, int]:
    """Remove magenta key spill from the silhouette's edge band (`band` HD px): subtract min(R, B) - G where positive.

    Only pixels with alpha > 0 within `band` px of the matte edge are touched, so interior purples survive. The formula
    is specific to the magenta key; any other key with a non-zero band is an error. Returns the image and how many
    pixels changed."""
    if band > 0 and tuple(bg_key) != MAGENTA:
        raise ValueError(
            f"despill is implemented for the magenta key only (bg_key {tuple(bg_key)} is not (255, 0, 255))"
        )
    if band <= 0:
        return rgb.copy(), 0
    solid = alpha >= 255
    edge = (alpha > 0) & ~solid
    near = edge | (alpha <= 0)
    for _ in range(band):  # grow the "near the edge" region into the solid interior
        p = np.pad(near, 1)
        near = near | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    m = (alpha > 0) & near
    c = rgb.astype(np.int16)
    spill = np.minimum(c[..., 0], c[..., 2]) - c[..., 1]
    hit = m & (spill > 0)
    c[..., 0] -= np.where(hit, spill, 0)
    c[..., 2] -= np.where(hit, spill, 0)
    return c.clip(0, 255).astype(np.uint8), int(hit.sum())


def downscale_alpha(alpha: np.ndarray, scale: int, threshold: int = 128) -> np.ndarray:
    """HD alpha -> one bool per `scale` x `scale` block: True where the block mean is >= `threshold`.

    The frame must divide exactly, else ValueError naming the sizes (the caller infers scale from raw size / cell)."""
    h, w = alpha.shape[:2]
    if scale < 1 or h % scale or w % scale:
        raise ValueError(f"alpha {w}x{h} is not a multiple of scale {scale}")
    return alpha.reshape(h // scale, scale, w // scale, scale).mean((1, 3)) >= threshold


def border(mask: np.ndarray) -> np.ndarray:
    """Pixels of `mask` with a False (or out-of-frame) 4-neighbour."""
    mask = np.asarray(mask, dtype=bool)
    p = np.pad(mask, 1, constant_values=False)
    inner = p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return mask & ~inner


def remove_fringe(
    pre_rgb: np.ndarray,
    snapped_rgb: np.ndarray,
    opaque: np.ndarray,
    bg_key: tuple[int, int, int],
    passes: int = 1,
) -> tuple[np.ndarray, int]:
    """Drop silhouette-border pixels whose pre-snap colour is nearer the key than the palette colour it snapped to.

    `passes` > 1 repeats the rule on the new border (each pass can only remove pixels). Returns the new opaque mask
    and how many pixels were removed."""
    out = np.asarray(opaque, dtype=bool).copy()
    pre = pre_rgb.astype(np.float32)
    key = np.array(bg_key, np.float32)
    to_key = np.linalg.norm(pre - key, axis=-1)
    to_pal = np.linalg.norm(pre - snapped_rgb, axis=-1)
    near_key = to_key < to_pal
    removed = 0
    for _ in range(max(0, passes)):
        fringe = border(out) & near_key
        n = int(fringe.sum())
        if not n:
            break
        out &= ~fringe
        removed += n
    return out, removed


def compose(rgb: np.ndarray, opaque: np.ndarray) -> np.ndarray:
    """(H, W, 4) uint8 RGBA with alpha exactly {0, 255} and RGB zeroed where transparent."""
    opaque = np.asarray(opaque, dtype=bool)
    out = np.zeros((*opaque.shape, 4), np.uint8)
    out[opaque, :3] = np.asarray(rgb)[opaque]
    out[opaque, 3] = 255
    return out
