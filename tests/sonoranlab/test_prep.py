import hashlib
from pathlib import Path

import pytest
from PIL import Image

from sonoranlab.prep import gen_canvas, prepare_start_image


def still(path: Path, size: int = 8, alpha: bool = True) -> Path:
    im = Image.new("RGBA" if alpha else "RGB", (size, size), (0, 0, 0, 0) if alpha else (0, 0, 0))
    im.putpixel((2, 3), (200, 10, 10, 255) if alpha else (200, 10, 10))
    im.save(path)
    return path


def test_gen_canvas() -> None:
    assert [gen_canvas(c) for c in (184, 176, 136, 16, 17)] == [560, 528, 416, 48, 64]
    assert gen_canvas(184, scale=4) == 736


def test_prepare_start_image(tmp_path: Path) -> None:
    out = tmp_path / "start.png"
    info = prepare_start_image(still(tmp_path / "s.png"), 8, out)
    assert info == {
        "path": out,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "canvas": 32,
        "background": "#808080",
    }
    with Image.open(out) as im:
        assert im.mode == "RGB" and im.size == (32, 32)
        off = (32 - 24) // 2  # 8 * 3 = 24 centred on 32
        assert im.getpixel((0, 0)) == (128, 128, 128)  # canvas margin
        assert im.getpixel((off, off)) == (128, 128, 128)  # transparent still pixel -> background
        assert all(im.getpixel((off + 6 + dx, off + 9 + dy)) == (200, 10, 10) for dx in range(3) for dy in range(3))
    with pytest.raises(FileExistsError):
        prepare_start_image(tmp_path / "s.png", 8, out)


def test_background_parameter(tmp_path: Path) -> None:
    prepare_start_image(still(tmp_path / "s.png"), 8, tmp_path / "m.png", background="#ff00ff")
    with Image.open(tmp_path / "m.png") as im:
        assert im.getpixel((0, 0)) == (255, 0, 255)
    with pytest.raises(ValueError, match="#rrggbb"):
        prepare_start_image(tmp_path / "s.png", 8, tmp_path / "x.png", background="grey")


def test_refusals(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected 9x9"):
        prepare_start_image(still(tmp_path / "s.png"), 9, tmp_path / "a.png")
    with pytest.raises(ValueError, match="no alpha"):
        prepare_start_image(still(tmp_path / "rgb.png", alpha=False), 8, tmp_path / "b.png")
    assert not (tmp_path / "a.png").exists() and not (tmp_path / "b.png").exists()
