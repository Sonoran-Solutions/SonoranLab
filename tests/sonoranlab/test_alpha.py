"""sonoranlab.alpha on synthetic images (no game pixels): parity with benchmark/tools/process.py and golden rules.

The real-asset test reads one Phase 1 raw frame from the private work dir and skips without it; it compares only the
pre-registration stages against process.process_frame's fast `test_nearest` path.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmark" / "tools"))
import process as P

from sonoranlab.alpha import (
    border,
    compose,
    despill,
    downscale_alpha,
    extract_alpha,
    remove_fringe,
)
from sonoranlab.palette import limit_colours, snap

REPO = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get("SONORANLAB_WORK", "~/sonoranlab-work")).expanduser()
REAL_RAW = WORK / "benchmark" / "results" / "2026-10-18" / "E" / "relay_sentinel" / "2" / "set" / "raw"
PROFILE = REPO / "profiles" / "twos-are-up.yaml"

INK, RED, SKIN, BLUE = (17, 13, 12), (204, 34, 34), (230, 180, 140), (40, 60, 200)
PAL = np.array([INK, RED, SKIN, BLUE], np.uint8)
KEY = (255, 0, 255)


def downsample(rgb: np.ndarray, alpha: np.ndarray, scale: int) -> tuple[np.ndarray, np.ndarray]:
    """HD RGB + alpha -> process.py's `test_nearest` colour sample and the block-mean alpha mask."""
    h, w = alpha.shape
    return rgb[scale // 2 :: scale, scale // 2 :: scale][: h // scale, : w // scale], downscale_alpha(alpha, scale)


def on_key(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Flatten RGBA over the key: an RGB image the chroma key can extract."""
    w = alpha[..., None] / 255
    return np.rint(rgb.astype(np.float32) * w + np.array(KEY, np.float32) * (1 - w)).astype(np.uint8)


def sprite(h: int = 24, w: int = 24) -> tuple[np.ndarray, np.ndarray]:
    """A solid figure with an anti-aliased magenta edge, one fringe pixel and an interior purple."""
    rgb = np.zeros((h, w, 3), np.uint8)
    alpha = np.zeros((h, w), np.float32)
    rgb[4:16, 5:15], alpha[4:16, 5:15] = RED, 255
    rgb[6:14, 7:13] = SKIN
    rgb[8:12, 8:12] = (150, 60, 160)  # interior purple away from the edge (relay_sentinel's orb)
    rgb[3, 6:14], alpha[3, 6:14] = (240, 10, 200), 128  # anti-aliased edge blended toward the key
    rgb[10, 4], alpha[10, 4] = (240, 10, 200), 255  # opaque fringe pixel on the silhouette border
    rgb[15, 5], alpha[15, 5] = (200, 40, 30), 255  # red on the bottom border: not nearer the key
    return rgb, alpha


# ---------------------------------------------------------------- parity with process.py


def test_extract_alpha_parity_soft_rgba_and_rgb_on_key():
    rng = np.random.default_rng(81)
    rgb = rng.integers(0, 256, (16, 20, 3), dtype=np.uint8)
    alpha = rng.integers(0, 256, (16, 20), dtype=np.uint8)
    alpha[:4] = 255
    soft = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    got = extract_alpha(soft, KEY, 48)
    assert np.array_equal(got[0], P.extract_alpha(soft, 48)[0])
    assert np.array_equal(got[1], P.extract_alpha(soft, 48)[1])
    keyed = Image.fromarray(on_key(rgb, alpha), "RGB")
    got = extract_alpha(keyed, KEY, 48)
    want = P.extract_alpha(keyed, 48)
    for a, b in zip(got, want, strict=False):
        assert np.array_equal(a, b)
    assert set(np.unique(got[1]).tolist()) <= {0.0, 255.0}


def test_despill_parity():
    rng = np.random.default_rng(82)
    rgb = rng.integers(0, 256, (20, 20, 3), dtype=np.uint8)
    alpha = np.zeros((20, 20), np.float32)
    alpha[3:17, 3:17] = 255
    alpha[3, 4:16] = 128
    for band in (0, 1, 2, 3):
        got, n = despill(rgb, alpha, band)
        want, m = P.despill(rgb, alpha, band)
        assert np.array_equal(got, want) and n == m


def test_fringe_rule_parity_with_process_frame():
    """The full pre-registration chain with the fast test_nearest downscaler: every stage and stat matches."""
    rng = np.random.default_rng(83)
    rgb, alpha = sprite()
    rgb[4:16, 5:15] = np.where(
        rng.random((12, 10, 1)) < 0.3, rng.integers(0, 256, (12, 10, 3), dtype=np.uint8), rgb[4:16, 5:15]
    )
    scale = 3
    up = np.repeat(np.repeat(on_key(rgb, alpha), scale, 0), scale, 1)
    im = Image.fromarray(up, "RGB")

    want, stats = P.process_frame(im, scale, PAL, "test_nearest", 0, 48, 2, 64)

    pre_rgb, hd_alpha = extract_alpha(im, KEY, 48)
    pre_rgb, despilled = despill(pre_rgb, hd_alpha, 2)
    assert despilled == stats["despilled_hd_px"]
    small, opaque = downsample(pre_rgb, hd_alpha, scale)
    snapped, _ = snap(small, PAL)
    assert int((snap(small, PAL)[1] & opaque).sum()) == stats["off_palette_before"]
    opaque, removed = remove_fringe(small, snapped, opaque, KEY, passes=1)
    assert removed == stats["fringe_removed"]
    snapped, merged = limit_colours(snapped, opaque, 64)
    assert merged == stats["colours_merged"]
    assert np.array_equal(compose(snapped, opaque), want)


def test_downscale_alpha_border_parity():
    rng = np.random.default_rng(84)
    alpha = rng.integers(0, 256, (24, 24)).astype(np.float32)
    assert np.array_equal(downscale_alpha(alpha, 3), alpha.reshape(8, 3, 8, 3).mean((1, 3)) >= 128)
    mask = rng.random((12, 13)) > 0.5
    assert np.array_equal(border(mask), P.border(mask))


# ---------------------------------------------------------------- golden behaviour


def test_golden_fringe_outline_kept_interior_survives():
    rgb, alpha = sprite()
    scale = 3
    hd_rgb = np.repeat(np.repeat(rgb, scale, 0), scale, 1)
    hd_alpha = np.repeat(np.repeat(alpha, scale, 0), scale, 1)
    small_rgb, hd = downsample(hd_rgb, hd_alpha, scale)
    snapped, _ = snap(small_rgb, PAL)
    out, removed = remove_fringe(small_rgb, snapped, hd, KEY)

    assert hd.shape == small_rgb.shape[:2] == alpha.shape and hd[5, 8] and hd[10, 4]
    assert removed == 9  # the 8 anti-aliased edge blocks of row 3 plus the one opaque fringe pixel (10, 4)
    assert not out[3, 6:14].any()  # the edge blended toward magenta is gone
    assert not out[10, 4]  # the opaque fringe pixel on the silhouette border too
    assert out[5:15, 6:13].all()  # interior kept, including the purple orb
    assert out[8, 9] and tuple(small_rgb[8, 9]) == (150, 60, 160)  # the purple itself (snaps to the nearest blue)

    # a near-black 1 px outline next to transparency STAYS: it is nearer a dark palette colour than the key
    dark = np.zeros((9, 9, 3), np.uint8)
    dark[3:6, 3:6] = SKIN
    dark[2, 3:6] = dark[5, 3:6] = dark[3:5, 2] = dark[3:5, 5] = INK
    mask = np.zeros((9, 9), bool)
    mask[2:6, 2:6] = True
    dark_snapped, _ = snap(dark, PAL)
    kept, n = remove_fringe(dark, dark_snapped, mask, KEY)
    assert n == 0 and kept[2, 3] and kept[4, 2]


def test_golden_despill_keeps_interior_purple():
    rgb, alpha = sprite()
    out, n = despill(rgb, alpha, 2)
    assert (out[8:12, 8:12] == (150, 60, 160)).all()  # interior purple untouched
    assert out[3, 8].tolist() == [50, 10, 10]  # edge: min(240, 200) - 10 = 190 removed from R and B
    assert out[10, 4].tolist() == [50, 10, 10]  # same spill on the silhouette
    assert (out[15, 5] == (200, 40, 30)).all()  # red on the border: min(R,B) < G -> unchanged
    assert n == int(((out != rgb).any(-1)).sum())


def test_golden_despill_parity_with_process_frame_uses_magenta_only():
    rgb, alpha = sprite()
    with pytest.raises(ValueError, match="magenta key only"):
        despill(rgb, alpha, 1, bg_key=(0, 255, 0))
    assert np.array_equal(despill(rgb, alpha, 0, bg_key=(0, 255, 0))[0], rgb)  # band 0 is a no-op for any key


def test_golden_compose_binary_alpha_and_zeroed_rgb():
    rgb = np.array([[[200, 30, 30], [1, 2, 3]]], np.uint8)
    out = compose(rgb, np.array([[True, False]]))
    assert out.shape == (1, 2, 4) and out.dtype == np.uint8
    assert out[0, 0].tolist() == [200, 30, 30, 255]
    assert out[0, 1].tolist() == [0, 0, 0, 0]
    assert set(np.unique(out[..., 3]).tolist()) <= {0, 255}


def test_no_input_is_mutated():
    rgb, alpha = sprite()
    rgb_before, alpha_before = rgb.copy(), alpha.copy()
    snapped = np.zeros_like(rgb)
    mask = alpha > 0
    mask_before = mask.copy()
    despill(rgb, alpha, 2)
    remove_fringe(rgb, snapped, mask, KEY, passes=2)
    compose(rgb, mask)
    downscale_alpha(np.repeat(np.repeat(alpha, 2, 0), 2, 1), 2)
    assert np.array_equal(rgb, rgb_before) and np.array_equal(alpha, alpha_before)
    assert np.array_equal(mask, mask_before)


def test_downscale_alpha_rejects_a_scale_that_does_not_divide():
    with pytest.raises(ValueError, match="not a multiple of scale 3"):
        downscale_alpha(np.zeros((10, 9), np.float32), 3)


# ---------------------------------------------------------------- real Phase 1 data (private, skipped in CI)


def test_real_frame_matches_process_frame_before_registration():
    raw = REAL_RAW / "east_0.png"
    if not raw.is_file() or not PROFILE.is_file():
        pytest.skip(f"{raw} not present (private Phase 1 work dir)")
    from sonoranlab.profile import ProfileError, load_palette, load_profile

    try:
        pal = load_palette(load_profile(PROFILE))
    except ProfileError as e:
        pytest.skip(f"palette unavailable: {e}")
    im = Image.open(raw)
    scale = 3
    key_tol, despill_px, max_colours = 48, 2, 64

    want, stats = P.process_frame(im, scale, pal, "test_nearest", 0, key_tol, despill_px, max_colours)

    pre_rgb, hd_alpha = extract_alpha(im, KEY, key_tol)
    pre_rgb, _ = despill(pre_rgb, hd_alpha, despill_px)
    small, opaque = downsample(pre_rgb, hd_alpha, scale)
    snapped, off = snap(small, pal)
    assert int((off & opaque).sum()) == stats["off_palette_before"]
    opaque, removed = remove_fringe(small, snapped, opaque, KEY)
    assert removed == stats["fringe_removed"]
    snapped, merged = limit_colours(snapped, opaque, max_colours)
    assert merged == stats["colours_merged"]
    got = compose(snapped, opaque)
    assert np.array_equal(got, want)
    assert got.shape == (im.size[1] // scale, im.size[0] // scale, 4)
    assert set(np.unique(got[..., 3]).tolist()) <= {0, 255}
    assert {tuple(c) for c in got[got[..., 3] > 0, :3].tolist()} <= {tuple(c) for c in pal.tolist()}
