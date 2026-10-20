"""select_loop/write_loop must stay byte-identical to benchmark/tools/select_loop.py (the Phase 1 script)."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sonoranlab.loop import select_loop, write_loop

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
