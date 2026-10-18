"""Golden tests for benchmark/tools/process.py on a tiny synthetic clip (no game pixels anywhere).

3 frames, 16 px cell, scale 4 -> 64 px raw frames on the #ff00ff key, a 4-colour palette made here.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmark" / "tools"))
import process as P

CELL, SCALE = 16, 4
INK, RED, SKIN, BLUE = (17, 13, 12), (204, 34, 34), (230, 180, 140), (40, 60, 200)
RED_OFF = (200, 30, 30)  # model "fought the palette": snaps to RED
FRINGE = (240, 10, 200)  # blend toward the key: nearer #ff00ff than any palette colour


def sprite() -> np.ndarray:
    """6x8 figure at cell coords x 4..9, y 3..10 (bbox centre x 7, lowest row 10 -> shift (+1, +2))."""
    s = np.zeros((CELL, CELL, 4), np.uint8)
    s[3:11, 4:10] = INK + (255,)
    s[4:6, 5:9] = SKIN + (255,)
    s[6:10, 5:9] = RED_OFF + (255,)
    s[7, 6:8] = BLUE + (255,)
    return s


def write_raw(raw: Path, frames: list[np.ndarray], direction: str = "east") -> None:
    raw.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(frames):
        up = np.repeat(np.repeat(f, SCALE, 0), SCALE, 1)
        rgb = np.where(up[..., 3:] > 0, up[..., :3], np.array(P.BG_KEY, np.uint8))
        Image.fromarray(rgb, "RGB").save(raw / f"{direction}_{i}.png")


def shifted(f: np.ndarray, dx: int = 0, dy: int = 0) -> np.ndarray:
    return np.roll(f, (dy, dx), (0, 1))


@pytest.fixture
def palette(tmp_path: Path) -> Path:
    p = tmp_path / "palette.png"
    Image.fromarray(np.array([[INK, RED, SKIN, BLUE]], np.uint8), "RGB").save(p)
    return p


def go(tmp_path: Path, palette: Path, frames: list[np.ndarray], **kw) -> dict:
    write_raw(tmp_path / "raw", frames)
    return P.run(
        tmp_path / "raw",
        tmp_path / "out",
        character="synthetic",
        clip="run",
        cell=CELL,
        frames=len(frames),
        palette=palette,
        directions=["east"],
        downscaler="test_nearest",
        **kw,
    )


def flagged(rep: dict, rule: str) -> list[int]:
    return [r["frame"] for r in rep["frames"] if any(i.startswith(rule) for i in r["issues"])]


def test_golden_clip(tmp_path: Path, palette: Path) -> None:
    base = sprite()
    base[8, 4] = FRINGE + (255,)  # one silhouette-border fringe pixel on frame 0 only
    clean = sprite()
    clean[8, 4] = 0
    frames = [base, shifted(sprite(), dy=-1), sprite()]  # frame 1 bobs up 1 px
    rep = go(tmp_path, palette, frames)
    out = tmp_path / "out"

    assert sorted(p.name for p in (out / "processed").iterdir()) == ["east_0.png", "east_1.png", "east_2.png"]
    assert (out / "raw").exists() is False and len(list((tmp_path / "raw").iterdir())) == 3  # raw untouched
    got = [np.asarray(Image.open(out / "processed" / f"east_{i}.png")) for i in range(3)]
    assert all(g.shape == (CELL, CELL, 4) for g in got)
    assert all(set(np.unique(g[..., 3])) <= {0, 255} for g in got)

    # golden: palette-snapped sprite moved by the one per-clip shift (+1, +2)
    def golden(f: np.ndarray) -> np.ndarray:
        g = f.copy()
        g[(g[..., :3] == RED_OFF).all(-1) & (g[..., 3] > 0), :3] = RED
        return shifted(g, 1, 2)

    assert np.array_equal(got[0], golden(clean))
    assert np.array_equal(got[1], golden(shifted(sprite(), dy=-1)))
    assert rep["registration"]["east"]["shift_px"] == [1, 2]
    assert rep["registration"]["east"]["raw_lowest_rows"] == [10, 9, 10]
    assert rep["directions"]["east"]["foot_rows"] == [CELL - 4, CELL - 5, CELL - 4]  # bob kept
    assert rep["directions"]["east"]["lowest_row_ok"]

    fr = rep["frames"]
    assert [r["off_palette_before"] for r in fr] == [15, 14, 14]  # 4x4 RED_OFF minus 2 BLUE; frame 0 + its fringe pixel
    assert [r["off_palette_after"] for r in fr] == [0, 0, 0]
    assert [r["fringe_removed"] for r in fr] == [1, 0, 0]
    assert rep["off_palette_before"] == 43 and rep["pass"], rep["failures"]
    assert rep["frame_count"] == {"expected": 3, "actual": 3, "ok": True}
    assert rep["cycle"]["foot_slide_max"] == 0

    sheet = np.asarray(Image.open(out / "sheet.png"))
    assert sheet.shape == (8 * CELL, 4 * CELL, 4)
    row = {d: sheet[r * CELL : (r + 1) * CELL] for r, d in enumerate(P.ROWS)}
    assert np.array_equal(row["east"][:, CELL : 2 * CELL], got[0])
    assert np.array_equal(row["west"][:, CELL : 2 * CELL], got[0][:, ::-1])  # west mirrored from east
    assert not row["north"].any() and not row["east"][:, :CELL].any()  # not generated / no still given
    assert Image.open(out / "contact.png").size == (3 * CELL * 8, CELL * 8)
    assert json.loads((out / "report.json").read_text())["pass"]


def test_edge_touch_flags_only_that_frame(tmp_path: Path, palette: Path) -> None:
    spear = sprite()
    spear[6, 0:4] = INK + (255,)  # reaches the raw frame's left edge; median centring leaves it at x = 1
    rep = go(tmp_path, palette, [sprite(), sprite(), spear])
    assert flagged(rep, "touches_edge") == [2]
    assert rep["frames"][2]["touches_edge"] == ["left"] and not rep["pass"]


def test_horizontal_shift_flags_only_that_frame(tmp_path: Path, palette: Path) -> None:
    rep = go(tmp_path, palette, [sprite(), shifted(sprite(), dx=2), sprite()], max_foot_slide=1)
    assert [r["foot_slide"] for r in rep["frames"]] == [0, 2, 0]
    assert flagged(rep, "foot_slide") == [1]
    assert rep["registration"]["east"]["shift_px"] == [1, 2]  # one bad frame does not move the clip


def test_missing_direction_fails_frame_count(tmp_path: Path, palette: Path) -> None:
    write_raw(tmp_path / "raw", [sprite()])
    rep = P.run(
        tmp_path / "raw",
        tmp_path / "out",
        character="s",
        clip="run",
        cell=CELL,
        frames=1,
        palette=palette,
        directions=["east", "north"],
        downscaler="test_nearest",
    )
    assert not rep["frame_count"]["ok"] and rep["frame_count"]["actual"] == 1 and not rep["pass"]


def test_pixeloe_kcentroid_runs(palette: Path) -> None:
    pytest.importorskip("pixeloe")
    up = np.repeat(np.repeat(sprite(), SCALE, 0), SCALE, 1)
    rgb = np.where(up[..., 3:] > 0, up[..., :3], np.array(P.BG_KEY, np.uint8))
    f, stats = P.process_frame(Image.fromarray(rgb, "RGB"), SCALE, P.load_palette(palette), "k_centroid")
    assert f.shape == (CELL, CELL, 4)
    assert set(np.unique(f[..., 3])) <= {0, 255} and f[..., 3].any()
    pal = {INK, RED, SKIN, BLUE}
    assert {tuple(c) for c in f[f[..., 3] > 0, :3].tolist()} <= pal
    assert stats["off_palette_before"] >= 0
