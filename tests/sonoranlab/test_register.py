"""sonoranlab.register: golden clips, exact parity with benchmark/tools/process.py::register_clip (synthetic + real)."""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

from sonoranlab.register import register_clip

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmark" / "tools"))
import process as P

CELL = 16
WORK = Path(os.environ.get("SONORANLAB_WORK", "~/sonoranlab-work")).expanduser()


def blob(w: int, h: int, x: int, y: int, size: int = 16) -> np.ndarray:
    """w x h opaque rectangle at (x, y) on a size x size transparent frame, with one distinguishing pixel."""
    f = np.zeros((size, size, 4), np.uint8)
    f[y : y + h, x : x + w] = (200, 30, 30, 255)
    f[y, x] = (0, 0, 255, 255)
    return f


def reg(frames, cell=CELL, **kw):
    return register_clip(frames, (cell, cell), foot_y=cell - 4, anchor_x=cell // 2, **kw)


def test_foot_golden_one_shift_keeps_bob() -> None:
    a, b = blob(4, 6, 2, 3), blob(4, 6, 2, 5)  # frame b stands 2 px lower (lowest rows 8 and 10), bbox centre x 4
    out, info = reg([a, b, a])
    assert info["mode"] == "foot" and info["shift_px"] == [4, 2]  # lowest 10 -> 12, centre 4 -> 8
    assert info["raw_lowest_rows"] == [8, 10, 8]
    assert info["raw_shifts_px"] == [[4, 4], [4, 2], [4, 4]]  # frame-alone shifts differ: model drift is visible
    assert info["clipped_px"] == [0, 0, 0]
    assert np.array_equal(out[0], np.roll(a, (2, 4), (0, 1)))
    assert np.array_equal(out[1], np.roll(b, (2, 4), (0, 1)))
    assert P.bbox(out[1])[3] == 12 and P.bbox(out[0])[3] == 10  # relative bob preserved
    assert np.array_equal(out[2], out[0])


def test_bbox_bottom_is_same_rule_for_floating_subject() -> None:
    f = [blob(4, 4, 6, 2), blob(4, 4, 6, 0)]
    out, info = reg(f, mode="bbox_bottom")
    assert info["mode"] == "bbox_bottom" and info["shift_px"] == [0, 7]  # lowest row 5 -> 12; centre 8 stays
    assert out[0][9:13, 6:10, 3].all() and out[1][7:11, 6:10, 3].all()
    assert out[0][..., :3].shape == (CELL, CELL, 3)
    assert reg(f)[1]["shift_px"] == info["shift_px"]


def test_negative_offsets_and_transparent_padding() -> None:
    f = [blob(4, 4, 11, 10)]  # centre 13, lowest 13 -> shift (-5, -1)
    out, info = reg(f)
    assert info["shift_px"] == [-5, -1]
    assert np.array_equal(np.nonzero(out[0][..., 3])[1].min(), 6)
    assert not out[0][..., 3][:, :6].any() and not out[0][..., 3][:, 10:].any()  # padding stays transparent
    assert out[0].shape == (CELL, CELL, 4) and out[0].dtype == np.uint8


def test_overlarge_silhouette_counts_clipped_on_the_right_frame() -> None:
    wide = np.zeros((16, 40, 4), np.uint8)
    wide[4:12, :] = (1, 2, 3, 255)  # 40 px wide in a 16 px cell
    ok = blob(4, 8, 6, 4)
    out, info = register_clip([ok, ok, wide], (CELL, CELL), foot_y=11, anchor_x=8)
    assert info["shift_px"] == [0, 0] and all(o.shape == (CELL, CELL, 4) for o in out)
    assert info["clipped_px"] == [0, 0, 8 * 24]  # 16 of 40 columns survive


def test_smaller_frames_and_non_square_cell() -> None:
    f = np.zeros((6, 6, 4), np.uint8)
    f[1:5, 1:5] = 255
    out, info = register_clip([f], (20, 12), foot_y=8, anchor_x=10)
    assert out[0].shape == (12, 20, 4) and info["shift_px"] == [7, 4]
    assert info["clipped_px"] == [0]


def test_rig_mode_uses_anchors() -> None:
    f = [blob(4, 4, 2, 2), blob(4, 4, 2, 2)]
    out, info = reg(f, mode="rig", anchors=[(3.0, 4.0), (5.0, 6.0)])
    assert info["shift_px"] == [4, 6]  # median x 4 -> 8, max y 6 -> 12
    assert info["raw_shifts_px"] == [[5, 8], [3, 6]]
    assert info["raw_lowest_rows"] == [5, 5]
    assert np.array_equal(out[0], np.roll(f[0], (6, 4), (0, 1)))
    for bad in (None, [(1.0, 1.0)]):
        with pytest.raises(ValueError, match="rig mode needs one"):
            reg(f, mode="rig", anchors=bad)
    with pytest.raises(ValueError, match="only used in rig"):
        reg(f, anchors=[(1, 1), (1, 1)])
    with pytest.raises(ValueError, match="unknown registration mode"):
        reg(f, mode="centre")
    with pytest.raises(ValueError, match="at least one"):
        reg([])


def test_empty_frame_reported_not_raised() -> None:
    out, info = reg([blob(4, 4, 2, 2), np.zeros((16, 16, 4), np.uint8)])
    assert info["error"] == "empty frame" and info["shift_px"] is None and info["empty_frames"] == [1]
    assert len(out) == 2 and not any(o.any() for o in out)


def _random_clip(rng: np.random.Generator, n: int, size: int) -> list[np.ndarray]:
    fs = []
    for _ in range(n):
        f = np.zeros((size, size, 4), np.uint8)
        w, h = rng.integers(2, size - 1, 2)
        x, y = rng.integers(0, size - w + 1), rng.integers(0, size - h + 1)
        f[y : y + h, x : x + w] = rng.integers(1, 255, (h, w, 4)) | np.array([0, 0, 0, 255], np.uint8)
        fs.append(f)
    return fs


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("size", [12, 16, 24, 40])  # smaller than, equal to and overlarge vs the 16 px cell
def test_parity_with_process_register_clip(seed: int, size: int) -> None:
    clip = _random_clip(np.random.default_rng(seed), 5, size)
    want, winfo = P.register_clip(clip, CELL)
    got, info = reg(clip)
    assert all(np.array_equal(a, b) for a, b in zip(want, got, strict=True))
    assert info["shift_px"] == winfo["shift_px"]
    for k in ("raw_lowest_rows", "raw_shifts_px", "clipped_px"):
        assert info[k] == winfo[k], k


def test_parity_on_real_relay_sentinel_frames() -> None:
    raw = WORK / "benchmark/results/2026-10-18/E/relay_sentinel/2/set/raw"
    pal_file = Path(P.PALETTE)
    if not raw.is_dir() or not pal_file.exists():
        pytest.skip(f"Phase 1 relay_sentinel set 2 raw frames or palette not present ({raw})")
    pal = P.load_palette(pal_file)
    checked = 0
    for d, ims in P.load_clips(raw).items():
        scale = 3  # Phase 1 Track E
        cell = ims[0].size[0] // scale
        frames = [P.process_frame(im, scale, pal, "test_nearest", 0)[0] for im in ims]
        want, winfo = P.register_clip(frames, cell)
        got, info = register_clip(frames, (cell, cell), foot_y=cell - 4, anchor_x=cell // 2)
        assert all(np.array_equal(a, b) for a, b in zip(want, got, strict=True)), d
        assert info["shift_px"] == winfo["shift_px"] and info["clipped_px"] == winfo["clipped_px"], d
        checked += 1
    assert checked


def test_anchor_frame_keeps_a_non_loop_clip_where_its_start_pose_stands():
    """A cast throws the rifle forward in later frames: the median bbox centre moves right, which would shift the whole
    clip left (the character jumps when it starts shooting). anchor_frame=0 places it by the start pose instead."""

    def frame(rifle: int) -> np.ndarray:
        f = np.zeros((40, 40, 4), np.uint8)
        f[10:38, 16:24] = (200, 40, 40, 255)  # body: centre x 20
        if rifle:
            f[18:20, 24 : 24 + rifle] = (30, 60, 220, 255)
        return f

    frames = [frame(0), frame(12), frame(12), frame(12), frame(0)]
    _, info_m = register_clip(frames, (40, 40), foot_y=37, anchor_x=20)
    by_start, info_s = register_clip(frames, (40, 40), foot_y=37, anchor_x=20, anchor_frame=0)
    assert info_m["shift_px"][0] == -6 and "anchor_frame" not in info_m  # loop clips: unchanged rule
    assert info_s["shift_px"][0] == 0 and info_s["anchor_frame"] == 0
    assert np.array_equal(by_start[0], frames[0])  # the start pose is not moved
    with pytest.raises(ValueError, match="anchor_frame"):
        register_clip(frames, (40, 40), foot_y=37, anchor_x=20, anchor_frame=5)


def _lying(x0: int, x1: int, y0: int, width: int = 48) -> np.ndarray:
    """A body occupying columns x0..x1 from row y0 down to row 28 of a `width` x 32 raw frame (wider than the cell,
    as a start_scale clip's downscaled canvas is)."""
    f = np.zeros((32, width, 4), np.uint8)
    f[y0:29, x0 : x1 + 1] = (200, 40, 40, 255)
    return f


def fall_reg(frames, **kw):
    return register_clip(frames, (32, 32), foot_y=28, anchor_x=16, anchor_frame=0, **kw)


def test_fall_fit_moves_the_clip_by_the_minimum_that_keeps_every_frame_inside():
    """Falls to the right: frame-0 anchoring would push the lying body past the right padding (x 33 > 29)."""
    frames = [_lying(14, 17, 13), _lying(14, 24, 19), _lying(14, 33, 25), _lying(14, 33, 25)]
    _, plain = fall_reg(frames)
    assert plain["shift_px"] == [0, 0] and plain["clipped_px"][-1] > 0 and "fit" not in plain
    out, info = fall_reg(frames, fit_padding=2)
    assert info["shift_px"] == [-4, 0] and info["fit"] == {"padding": 2, "offset_px": -4, "fits": True}
    assert info["clipped_px"] == [0, 0, 0, 0] and info["anchor_frame"] == 0
    assert info["raw_shifts_px"] == plain["raw_shifts_px"]  # per-frame provenance is unchanged
    xs = [np.nonzero(f[..., 3].any(0))[0] for f in out]
    assert min(x.min() for x in xs) >= 2 and max(x.max() for x in xs) == 29  # touches the padding, not past it
    _, info_l = fall_reg([f[:, ::-1] for f in frames], fit_padding=2)  # falling left: shifts right
    assert info_l["fit"]["offset_px"] > 0 and info_l["clipped_px"] == [0, 0, 0, 0]


def test_fall_fit_is_zero_when_the_clip_already_fits_and_never_forced_when_it_cannot():
    fits = [_lying(14, 17, 13), _lying(12, 20, 19), _lying(10, 24, 25)]
    _, info = fall_reg(fits, fit_padding=2)
    assert info["fit"] == {"padding": 2, "offset_px": 0, "fits": True} and info["shift_px"] == [0, 0]
    too_wide = [_lying(14, 17, 13), _lying(14, 30, 19), _lying(14, 45, 25)]  # union 32 px > the 28 px inside
    _, plain = fall_reg(too_wide)
    _, info = fall_reg(too_wide, fit_padding=2)
    assert info["fit"] == {"padding": 2, "offset_px": 0, "fits": False}
    assert info["shift_px"] == plain["shift_px"] and info["clipped_px"] == plain["clipped_px"]  # frame-0 anchored


def test_fit_needs_a_frame_anchored_non_rig_clip():
    """Loop clips (median anchoring) and rig clips never fit; pipeline passes fit_padding only for fall clips."""
    frames = [_lying(14, 17, 13), _lying(14, 33, 25)]
    with pytest.raises(ValueError, match="fit_padding"):
        register_clip(frames, (32, 32), foot_y=28, anchor_x=16, fit_padding=2)
    with pytest.raises(ValueError, match="fit_padding"):
        fall_reg(frames, fit_padding=-1)
