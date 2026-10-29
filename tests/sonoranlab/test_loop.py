"""select_loop/write_loop must stay byte-identical to benchmark/tools/select_loop.py (the Phase 1 script)."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sonoranlab.loop import end_rule, select_loop, select_once, write_loop

SCRIPT = Path(__file__).resolve().parents[2] / "benchmark/tools/select_loop.py"
PERIOD = 8


def make_clip(clip: Path, n: int = 33) -> list[Path]:
    """A blob with two limbs swinging with period 8 after a 6-frame run-up (the still settling into the gait)."""
    clip.mkdir(parents=True)
    files = []
    for i in range(n):
        a = np.zeros((96, 96, 4), np.uint8)
        t = 2 * np.pi * i / PERIOD
        grow = min(i, 6) / 6
        x = 40 + round(12 * np.sin(t) * grow)
        a[30:70, x : x + 14] = (200, 40, 40, 255)  # body
        y = 30 + round(10 * np.cos(t) * grow)
        a[y : y + 8, 20:40] = (30, 60, 220, 255)  # near limb; the far one is half a period behind
        a[100 - y - 8 : 100 - y, 55:75] = (30, 220, 60, 255)
        files.append(clip / f"rgba_{i:02d}.png")
        Image.fromarray(a).save(files[-1])
    return files


def load_script():
    spec = importlib.util.spec_from_file_location("select_loop_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parity_with_script(tmp_path: Path) -> None:
    files = make_clip(tmp_path / "clip")
    load_script().main(
        [str(tmp_path / "clip"), str(tmp_path / "old"), "--direction", "east", "--frames", "7", "--size", "64"]
    )
    pick = select_loop(files, n_frames=7, size=64)
    write_loop(pick, files, tmp_path / "new", "east")
    old = json.loads((tmp_path / "old/loop.json").read_text())
    assert (pick.period, pick.start, pick.picks) == (old["period"], old["start"], old["picks"])
    assert pick.period == PERIOD
    assert sorted(p.name for p in (tmp_path / "old").iterdir()) == sorted(p.name for p in (tmp_path / "new").iterdir())
    for p in (tmp_path / "old").iterdir():
        assert p.read_bytes() == (tmp_path / "new" / p.name).read_bytes(), p.name


def test_images_give_the_same_picks_and_output_is_exclusive(tmp_path: Path) -> None:
    files = make_clip(tmp_path / "clip")
    ims = [Image.open(f) for f in files]
    by_path = select_loop(files, n_frames=7, size=64)
    by_image = select_loop(ims, n_frames=7, size=64)
    assert by_image.picks == by_path.picks and by_image.clip is None and by_image.files is None
    write_loop(by_image, ims, tmp_path / "out", "north")
    assert (tmp_path / "out/north_6.png").exists()
    with pytest.raises(FileExistsError):
        write_loop(by_image, ims, tmp_path / "out", "north")


def test_too_short_clip(tmp_path: Path) -> None:
    files = make_clip(tmp_path / "clip", n=10)
    with pytest.raises(ValueError, match="too short"):
        select_loop(files, n_frames=7, size=64)


# ---------------------------------------------------------------- select_once (loop: false clips)


def once_clip(kind: str, n: int = 33) -> list[Image.Image]:
    """Tiny synthetic Track E clips. cast: still 0-5, the arm rises to frame 14, holds, is back at rest from 24.
    fall: still 0-4, the body tips over 5-15 and lies still from 16. drift: never settles (slides 1 px per frame)."""
    frames = []
    for i in range(n):
        a = np.zeros((64, 64, 4), np.uint8)
        if kind == "cast":
            a[20:60, 28:36] = (200, 40, 40, 255)  # body
            lift = 0 if i <= 5 or i >= 24 else min(i - 5, 9, 24 - i) * 2
            a[30 - lift : 36 - lift, 36:50] = (30, 60, 220, 255)  # arm/rifle
        elif kind == "fall":
            t = min(max(i - 5, 0), 10) / 10
            h, w = round(40 - 32 * t), round(8 + 32 * t)
            a[60 - h : 60, 32 - w // 2 : 32 - w // 2 + w] = (200, 40, 40, 255)
        else:
            a[20:60, 10 + i : 18 + i] = (200, 40, 40, 255)
        frames.append(Image.fromarray(a))
    return frames


def test_select_once_cast_returns_near_the_start_pose_golden() -> None:
    pick = select_once(once_clip("cast"), n_frames=8, size=48, end="return")
    assert pick.end["peak"] in range(8, 16) and pick.end["search"] == [pick.end["peak"] + 1, 32]
    assert pick.period == 24 and pick.end["end"] == 24 and pick.end["end_delta"] == 0.0  # first frame back at rest
    assert pick.picks == [0, 3, 7, 10, 14, 17, 21, 24]  # golden: frame 0 + 7 even steps up to E
    assert (pick.start, pick.window, pick.loop_seam_score) == (0, "return", 0.0)


def test_select_once_return_ignores_the_unmoved_run_up() -> None:
    # frames 1..5 equal frame 0 (delta 0): without the peak rule E = n_frames would pick a clip that never moves
    pick = select_once(once_clip("cast"), n_frames=4, size=48, end="return")
    assert pick.period > pick.end["peak"] >= 4


def test_select_once_fall_ends_when_motion_settles_golden() -> None:
    pick = select_once(once_clip("fall"), n_frames=6, size=48, end="settle")
    assert pick.end["settled"] is True and pick.end["end"] == 15  # the last tipping step ends at frame 15
    # golden: 5 picks over [0, 15], then one resting frame a stride later, so the held final pair is two resting frames
    assert pick.picks == [0, 4, 8, 11, 15, 19] and pick.end["hold_pick"] == 19
    assert pick.loop_seam_score == 0 and len(pick.end["steps"]) == 32  # resting pair: no motion between them


def test_select_once_fall_that_never_settles_uses_the_last_frame() -> None:
    pick = select_once(once_clip("drift"), n_frames=6, size=48, end="settle")
    assert pick.end["settled"] is False and pick.picks[-1] == 32 and pick.picks[0] == 0


def test_select_once_bounds_and_loop_json(tmp_path: Path) -> None:
    clip = once_clip("cast")
    assert select_once(clip, n_frames=8, size=48, end="return", max_end=20).picks[-1] <= 20
    with pytest.raises(ValueError, match="end rule"):
        select_once(clip, n_frames=8, size=48, end="loop")
    with pytest.raises(ValueError, match="too short"):
        select_once(clip[:8], n_frames=8, size=48, end="return")
    write_loop(select_once(clip, n_frames=8, size=48, end="return"), clip, tmp_path / "once", "east")
    info = json.loads((tmp_path / "once/loop.json").read_text())
    assert info["window"] == "return" and info["end"]["end"] == 24 and info["picks"][0] == 0
    files = make_clip(tmp_path / "loop")
    write_loop(select_loop(files, n_frames=7, size=64), files, tmp_path / "loopout", "east")
    assert "window" not in json.loads((tmp_path / "loopout/loop.json").read_text())  # loop clips: unchanged json


def test_end_rule_is_explicit() -> None:
    from sonoranlab.profile import Animation

    assert end_rule(Animation(6, 10, True)) is None
    assert end_rule(Animation(8, 10, False)) == "return"
    assert end_rule(Animation(6, 8, False, hold_last=True)) == "return"  # hold_last never implies fall
    assert end_rule(Animation(6, 8, False, hold_last=True, validator_mode="fall")) == "settle"
