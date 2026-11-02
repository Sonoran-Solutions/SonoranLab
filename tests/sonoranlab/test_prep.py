import hashlib
from pathlib import Path

import pytest
from PIL import Image

from sonoranlab.prep import (
    gen_canvas,
    prepare_pose_1024,
    prepare_reference_1024,
    prepare_start_image,
    start_image_from_rgba,
)


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
        "scale": 3,
        "offset": [4, 4],
    }
    with Image.open(out) as im:
        assert im.mode == "RGB" and im.size == (32, 32)
        off = (32 - 24) // 2  # 8 * 3 = 24 centred on 32
        assert im.getpixel((0, 0)) == (128, 128, 128)  # canvas margin
        assert im.getpixel((off, off)) == (128, 128, 128)  # transparent still pixel -> background
        assert all(im.getpixel((off + 6 + dx, off + 9 + dy)) == (200, 10, 10) for dx in range(3) for dy in range(3))
    with pytest.raises(FileExistsError):
        prepare_start_image(tmp_path / "s.png", 8, out)


def test_start_scale_keeps_the_canvas_and_leaves_room(tmp_path: Path) -> None:
    """A fall clip's x2 still on the x3 canvas: same canvas edge (same Wan tokens), centred, background margins."""
    src = still(tmp_path / "s.png", size=160)
    x3 = prepare_start_image(src, 160, tmp_path / "x3.png")
    x2 = prepare_start_image(src, 160, tmp_path / "x2.png", scale=2, canvas=gen_canvas(160))
    assert x3["canvas"] == x2["canvas"] == 480 and x3["offset"] == [0, 0]
    assert (x2["scale"], x2["offset"]) == (2, [80, 80])
    with Image.open(tmp_path / "x2.png") as im:
        assert im.size == (480, 480)
        # still pixel (2, 3) at x2 covers x 84..85, y 86..87 only
        assert all(im.getpixel((84 + dx, 86 + dy)) == (200, 10, 10) for dx in range(2) for dy in range(2))
        assert im.getpixel((83, 86)) == im.getpixel((86, 86)) == (128, 128, 128)
    with pytest.raises(ValueError, match="smaller than the still"):
        prepare_start_image(src, 160, tmp_path / "bad.png", scale=3, canvas=320)


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


def pose_render(path: Path, edge: int = 32) -> Path:
    """A stand-in Track D render: black background, one bright keypoint dot."""
    im = Image.new("RGB", (edge, edge), (0, 0, 0))
    im.putpixel((edge // 2, edge // 2), (255, 0, 0))
    im.save(path)
    return path


def test_prepare_reference_1024(tmp_path: Path) -> None:
    out = tmp_path / "ref.png"
    info = prepare_reference_1024(still(tmp_path / "s.png"), 8, out)
    assert info == {
        "path": out,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "canvas": 1024,
        "background": "#808080",
    }
    with Image.open(out) as im:
        assert im.mode == "RGB" and im.size == (1024, 1024)
        off = (1024 - 24) // 2  # 8 * 3 = 24, centred on the 1024 canvas
        assert im.getpixel((0, 0)) == (128, 128, 128) and im.getpixel((1023, 1023)) == (128, 128, 128)
        assert im.getpixel((off, off)) == (128, 128, 128)  # transparent still pixel -> background
        assert all(im.getpixel((off + 6 + dx, off + 9 + dy)) == (200, 10, 10) for dx in range(3) for dy in range(3))
        assert im.getpixel((off - 1, off)) == (128, 128, 128)  # nothing bleeds outside the pasted square
    with pytest.raises(FileExistsError):
        prepare_reference_1024(tmp_path / "s.png", 8, out)


def test_prepare_reference_1024_options(tmp_path: Path) -> None:
    out = tmp_path / "ref.png"
    info = prepare_reference_1024(still(tmp_path / "s.png"), 8, out, background="#ff00ff", scale=4, canvas=256)
    assert info["canvas"] == 256 and info["background"] == "#ff00ff"
    with Image.open(out) as im:
        assert im.size == (256, 256) and im.getpixel((0, 0)) == (255, 0, 255)


def test_prepare_reference_1024_refusals(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected 9x9"):
        prepare_reference_1024(still(tmp_path / "s.png"), 9, tmp_path / "a.png")
    with pytest.raises(ValueError, match="no alpha"):
        prepare_reference_1024(still(tmp_path / "rgb.png", alpha=False), 8, tmp_path / "b.png")
    with pytest.raises(ValueError, match="#rrggbb"):
        prepare_reference_1024(still(tmp_path / "s.png"), 8, tmp_path / "c.png", background="grey")
    assert not any((tmp_path / n).exists() for n in ("a.png", "b.png", "c.png"))


def test_prepare_pose_1024(tmp_path: Path) -> None:
    out = tmp_path / "pose.png"
    info = prepare_pose_1024(pose_render(tmp_path / "p.png"), 8, out)
    assert info == {
        "path": out,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "canvas": 1024,
        "source_scale": 4,
    }
    with Image.open(out) as im:
        assert im.mode == "RGB" and im.size == (1024, 1024)
        off = (1024 - 24) // 2
        assert im.getpixel((0, 0)) == (0, 0, 0) and im.getpixel((1023, 1023)) == (0, 0, 0)
        # LANCZOS of a 32 -> 24 downscale keeps the bright dot near the centre on a black canvas
        reds = [(x, y) for y in range(off, off + 24) for x in range(off, off + 24) if im.getpixel((x, y))[0] > 0]
        assert reds
        cx = sum(x for x, _ in reds) / len(reds)
        cy = sum(y for _, y in reds) / len(reds)
        assert abs(cx - 511.5) <= 2 and abs(cy - 511.5) <= 2  # dot stays centred
    with pytest.raises(FileExistsError):
        prepare_pose_1024(tmp_path / "p.png", 8, out)


def test_prepare_pose_1024_refusals(tmp_path: Path) -> None:
    Image.new("RGB", (32, 16), (0, 0, 0)).save(tmp_path / "wide.png")
    with pytest.raises(ValueError, match="square"):
        prepare_pose_1024(tmp_path / "wide.png", 8, tmp_path / "a.png")
    with pytest.raises(ValueError, match="multiple of cell"):
        prepare_pose_1024(pose_render(tmp_path / "p3.png", 20), 8, tmp_path / "b.png")
    assert not (tmp_path / "a.png").exists() and not (tmp_path / "b.png").exists()


def test_start_image_from_rgba(tmp_path: Path) -> None:
    frame = tmp_path / "frame.png"
    im = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    im.paste((10, 20, 30, 255), (4, 4, 8, 8))
    im.save(frame)
    out = tmp_path / "start.png"
    info = start_image_from_rgba(frame, out)
    assert info["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert info["size"] == [12, 12] and info["background"] == "#808080"
    with Image.open(out) as o:
        assert o.mode == "RGB" and o.size == (12, 12)
        assert o.getpixel((0, 0)) == (128, 128, 128) and o.getpixel((6, 6)) == (10, 20, 30)
    with pytest.raises(FileExistsError):
        start_image_from_rgba(frame, out)
    with pytest.raises(ValueError, match="no alpha"):
        start_image_from_rgba(still(tmp_path / "rgb.png", alpha=False), tmp_path / "x.png")
