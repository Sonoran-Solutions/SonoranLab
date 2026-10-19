"""Palette stages of the per-frame chain (processing-v1 parity): nearest-colour snap and per-cell colour cap.

Ported from `benchmark/tools/process.py` with the same math and tie-breaks (argmin picks the first nearest palette row,
so the palette order decides ties; the colour cap breaks count ties by colour value). Outputs stay on the palette.
No function here mutates its inputs.
"""

from __future__ import annotations

import numpy as np

CHUNK_PX = 65_536  # pixels per snap block: bounds the (chunk x N_palette) distance matrix


def _nearest(flat: np.ndarray, pal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Index of the nearest palette row (first on ties) and squared distance, for every pixel of `flat`."""
    p = pal.astype(np.int32)
    idx = np.empty(len(flat), np.int64)
    dist = np.empty(len(flat), np.int64)
    for start in range(0, len(flat), CHUNK_PX):
        f = flat[start : start + CHUNK_PX]
        d = ((f[:, None, :] - p[None]) ** 2).sum(-1)
        i = d.argmin(1)
        idx[start : start + CHUNK_PX] = i
        dist[start : start + CHUNK_PX] = d[np.arange(len(f)), i]
    return idx, dist


def snap(rgb: np.ndarray, pal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Nearest palette colour in RGB (squared distance, first row on ties), and the mask of pixels not already one.

    Takes an (H, W, 3) image or a flat (N, 3) pixel list (the mask is then (N,)). Chunked so a 552x552 frame or larger
    does not build an (N_pixels x N_palette) matrix at once; the result is identical to the unchunked computation."""
    if pal.ndim != 2 or pal.shape[1] != 3 or not len(pal):
        raise ValueError(f"palette must be an (N, 3) array with N >= 1, got shape {pal.shape}")
    rgb = np.asarray(rgb)
    shape = rgb.shape
    if shape[-1] != 3 or not (rgb.ndim == 3 or rgb.ndim == 2):
        raise ValueError(f"rgb must be (H, W, 3) or (N, 3), got shape {shape}")
    flat = rgb.reshape(-1, 3).astype(np.int32)
    idx, dist = _nearest(flat, pal)
    return pal[idx].reshape(shape).astype(np.uint8), (dist > 0).reshape(shape[:-1])


def limit_colours(rgb: np.ndarray, opaque: np.ndarray, limit: int) -> tuple[np.ndarray, int]:
    """Merge the least-used opaque colour into its nearest remaining colour until <= `limit` remain.

    Deterministic: least used first, ties broken by smallest colour value; the merge target is the first nearest
    remaining colour. Pixels outside `opaque` are untouched, every result colour is one of the input colours."""
    if limit < 1:
        raise ValueError(f"colour limit must be >= 1, got {limit}")
    out = np.array(rgb, copy=True)
    opaque = np.asarray(opaque, dtype=bool)
    cols, counts = np.unique(out[opaque].reshape(-1, 3), axis=0, return_counts=True)
    merged = 0
    while len(cols) > limit:
        i = int(np.lexsort((*cols.T[::-1], counts))[0])  # least used, then smallest colour value
        rest = np.delete(np.arange(len(cols)), i)
        j = rest[((cols[rest].astype(np.int32) - cols[i].astype(np.int32)) ** 2).sum(1).argmin()]
        m = opaque & (out == cols[i]).all(-1)
        out[m] = cols[j]
        counts[j] += counts[i]
        cols, counts = cols[rest], counts[rest]
        merged += 1
    return out, merged


def off_palette_count(frame_rgba: np.ndarray, pal: np.ndarray) -> int:
    """Opaque pixels of a (H, W, 4) frame whose RGB is not a palette colour."""
    f = np.asarray(frame_rgba)
    opaque = f[..., 3] > 0
    if not opaque.any():
        return 0
    return int(snap(f[opaque, :3], pal)[1].sum())
