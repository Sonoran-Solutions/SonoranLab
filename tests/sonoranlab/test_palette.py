"""sonoranlab.palette on synthetic images (no game pixels): parity with benchmark/tools/process.py and golden rules."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmark" / "tools"))
import process as P

from sonoranlab.palette import CHUNK_PX, limit_colours, off_palette_count, snap

INK, RED, SKIN, BLUE = (17, 13, 12), (204, 34, 34), (230, 180, 140), (40, 60, 200)
PAL = np.array([INK, RED, SKIN, BLUE], np.uint8)


def random_rgb(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    return rng.integers(0, 256, (*shape, 3), dtype=np.uint8)


def test_snap_parity_with_process():
    rng = np.random.default_rng(81)
    pal = rng.integers(0, 256, (40, 3)).astype(np.uint8)
    rgb = random_rgb(rng, (24, 31))
    got, off = snap(rgb, pal)
    want, want_off = P.snap(rgb, pal)
    assert np.array_equal(got, want) and np.array_equal(off, want_off)


def test_snap_parity_across_chunk_boundaries():
    rng = np.random.default_rng(82)
    pal = rng.integers(0, 256, (255, 3)).astype(np.uint8)  # the real palette size
    n = CHUNK_PX * 2 + 17
    rgb = rng.integers(0, 256, (n, 3), dtype=np.uint8).reshape(1, n, 3)
    got, off = snap(rgb, pal)
    want, want_off = P.snap(rgb, pal)
    assert np.array_equal(got, want) and np.array_equal(off, want_off)


def test_snap_is_not_limited_by_chunk_size():
    """A frame bigger than a chunk still gets the exact unchunked answer (552x552 = 304 704 px)."""
    rng = np.random.default_rng(83)
    pal = rng.integers(0, 256, (64, 3)).astype(np.uint8)
    rgb = rng.integers(0, 256, (552, 552, 3), dtype=np.uint8)
    got, off = snap(rgb, pal)
    want, want_off = P.snap(rgb, pal)
    assert np.array_equal(got, want) and np.array_equal(off, want_off)


def test_snap_golden_exact_adherence_and_ties():
    rng = np.random.default_rng(84)
    rgb = random_rgb(rng, (16, 16))
    rgb[0, 0] = RED  # already a palette colour
    out, off = snap(rgb, PAL)
    assert not off[0, 0] and off.sum() == rgb.shape[0] * rgb.shape[1] - 1
    assert {tuple(c) for c in out.reshape(-1, 3).tolist()} <= {tuple(c) for c in PAL.tolist()}
    # a real tie: two palette rows equidistant from the pixel, the first row wins (as in process.snap)
    pal = np.array([(0, 0, 0), (2, 0, 0)], np.uint8)
    tie, _ = snap(np.array([[[1, 0, 0]]], np.uint8), pal)
    assert tuple(tie[0, 0]) == (0, 0, 0)  # first row wins, as in process.snap
    # deterministic
    assert np.array_equal(snap(rgb, PAL)[0], out)


def test_limit_colours_parity_with_process():
    rng = np.random.default_rng(85)
    pal = rng.integers(0, 256, (90, 3)).astype(np.uint8)
    rgb = pal[rng.integers(0, len(pal), (20, 20))]
    opaque = rng.random((20, 20)) > 0.2
    got, got_merged = limit_colours(rgb, opaque, 64)
    want, want_merged = P.limit_colours(rgb, opaque, 64)
    assert np.array_equal(got, want) and got_merged == want_merged


def test_limit_colours_golden_gate_and_tie_break():
    rng = np.random.default_rng(86)
    pal = rng.integers(0, 256, (80, 3)).astype(np.uint8)
    img = np.repeat(pal[None], 4, 0)  # 4 x 80, every colour used exactly 4 times: one big count tie
    img[:, :10] = pal[0]
    opaque = np.ones(img.shape[:2], bool)
    opaque[0, 79] = False
    out, merged = limit_colours(img, opaque, 64)
    used = {tuple(c) for c in out[opaque]}
    assert len(used) <= 64 and merged == len({tuple(c) for c in img[opaque]}) - len(used)
    assert used <= {tuple(c) for c in pal.tolist()}
    assert (out[:, :10] == pal[0]).all() and (out[0, 79] == img[0, 79]).all()
    assert np.array_equal(limit_colours(img, opaque, 64)[0], out)  # deterministic
    # tie-break: with equal counts the smallest colour value merges away first, into its nearest survivor
    small = np.array([[[10, 10, 10], [9, 9, 9]]], np.uint8)
    two, n = limit_colours(small, np.ones((1, 2), bool), 1)
    assert n == 1 and (two[0] == (10, 10, 10)).all()
    small = np.array([[[10, 10, 10, 255], [9, 9, 9, 0]]], np.uint8)  # transparent pixels are never touched
    two, n = limit_colours(small[..., :3], np.array([[True, False]]), 1)
    assert n == 0 and (two[0, 1] == (9, 9, 9)).all()


def test_snap_flat_pixel_list_parity_with_process():
    flat = np.array([RED, (200, 30, 30)], np.uint8)
    out, off = snap(flat, PAL)
    assert out.shape == (2, 3) and off.tolist() == [False, True]
    assert np.array_equal(out, P.snap(flat[None], PAL)[0][0])  # same nearest rows as process.snap


def test_off_palette_count():
    f = np.zeros((4, 4, 4), np.uint8)
    f[0, 0] = (*RED, 255)
    f[0, 1] = (200, 30, 30, 255)  # off-palette
    f[1, 0] = (200, 30, 30, 0)  # transparent: not counted
    assert off_palette_count(f, PAL) == 1
    f[...] = 0
    assert off_palette_count(f, PAL) == 0


def test_inputs_are_not_mutated():
    rgb = np.array([[[1, 2, 3], [200, 30, 30]]], np.uint8)
    pal = PAL.copy()
    rgb_before, pal_before = rgb.copy(), pal.copy()
    snap(rgb, pal)
    opaque = np.ones((1, 2), bool)
    limit_colours(rgb, opaque, 1)
    off_palette_count(np.dstack([rgb, np.full((1, 2), 255, np.uint8)]), pal)
    assert np.array_equal(rgb, rgb_before) and np.array_equal(pal, pal_before) and opaque.all()


def test_bad_palette_shape_raises():
    with pytest.raises(ValueError, match="palette must be"):
        snap(np.zeros((2, 2, 3), np.uint8), np.zeros((0, 3), np.uint8))
