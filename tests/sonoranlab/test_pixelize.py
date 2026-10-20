"""Per-frame PixelOE checks using generated synthetic colours; private parity is optional."""

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sonoranlab.cli import main
from sonoranlab.pixelize import pixelize, versions

REPO = Path(__file__).resolve().parents[2]
BLOCKS = np.array([[(224, 80, 48), (48, 192, 80)], [(64, 80, 208), (224, 208, 80)]], np.uint8)


def synthetic_frame() -> np.ndarray:
    """24x24 colour quadrants with a one-HD-pixel rectangular dark contour."""
    rgb = BLOCKS.repeat(12, 0).repeat(12, 1)
    rgb[6:18, 6] = rgb[6:18, 17] = (8, 8, 8)
    rgb[6, 6:18] = rgb[17, 6:18] = (8, 8, 8)
    return rgb


def test_golden() -> None:
    result = pixelize(synthetic_frame(), 3)
    with Image.open(REPO / "tests/fixtures/dyl80/k_centroid.png") as im:
        expected = np.asarray(im)
    np.testing.assert_array_equal(result, expected)
    assert result.shape == (8, 8, 3) and result.dtype == np.uint8
    # Interiors of all four uniform blocks retain their exact input colours.
    np.testing.assert_array_equal(result[np.ix_([0, 7], [0, 7])], BLOCKS)
    assert (result == (8, 8, 8)).all(-1).any()


@pytest.mark.parametrize("method", ["k_centroid", "contrast", "nearest"])
@pytest.mark.parametrize("thickness", [0, 2])
def test_deterministic_across_calls_and_threads(method: str, thickness: int) -> None:
    import torch

    rgb = np.random.default_rng(80).integers(0, 256, (24, 30, 3), dtype=np.uint8)
    before = rgb.copy()
    threads = torch.get_num_threads()
    rng = torch.get_rng_state().clone()
    try:
        default = pixelize(rgb, 3, method=method, thickness=thickness)
        np.testing.assert_array_equal(pixelize(rgb, 3, method=method, thickness=thickness), default)
        torch.set_num_threads(1)
        np.testing.assert_array_equal(pixelize(rgb, 3, method=method, thickness=thickness), default)
        assert torch.equal(torch.get_rng_state(), rng)
        np.testing.assert_array_equal(rgb, before)
    finally:
        torch.set_num_threads(threads)


def test_slicing_prevents_outline_bleed() -> None:
    left = np.full((24, 24, 3), 224, np.uint8)
    right = np.full_like(left, 80)
    left[4:20, 14:24] = 160
    left[4:20, 23] = 8  # one-pixel contour touching this cell's right border
    right[4:20, :10] = 192
    right[4:20, 0] = 8  # one-pixel contour touching this cell's left border
    sheet = np.concatenate([left, right], axis=1)
    frames = [pixelize(cell, 3, thickness=1) for cell in (left, right)]
    sliced = [pixelize(sheet[:, i : i + 24], 3, thickness=1) for i in (0, 24)]
    for actual, expected in zip(sliced, frames, strict=True):
        np.testing.assert_array_equal(actual, expected)
    separate = np.concatenate(sliced, axis=1)
    whole = pixelize(sheet, 3, thickness=1)
    assert np.any(whole[:, 7:9] != separate[:, 7:9])  # output columns on both sides of the shared border


@pytest.mark.parametrize(
    "rgb, scale, kwargs, message",
    [
        ([], 3, {}, "uint8"),
        (np.zeros((6, 6), np.uint8), 3, {}, "uint8"),
        (np.zeros((6, 6, 4), np.uint8), 3, {}, "uint8"),
        (np.zeros((6, 6, 3), np.float32), 3, {}, "uint8"),
        (np.zeros((0, 6, 3), np.uint8), 3, {}, "positive dimensions"),
        (np.zeros((7, 6, 3), np.uint8), 3, {}, "multiples of scale 3"),
        (np.zeros((6, 7, 3), np.uint8), 3, {}, "multiples of scale 3"),
        (np.zeros((6, 6, 3), np.uint8), 0, {}, "positive integer"),
        (np.zeros((6, 6, 3), np.uint8), -1, {}, "positive integer"),
        (np.zeros((6, 6, 3), np.uint8), 1.5, {}, "positive integer"),
        (np.zeros((6, 6, 3), np.uint8), True, {}, "positive integer"),
        (np.zeros((6, 6, 3), np.uint8), 3, {"method": "unknown"}, "unknown pixelize method"),
        (np.zeros((6, 6, 3), np.uint8), 3, {"thickness": -1}, "thickness"),
        (np.zeros((6, 6, 3), np.uint8), 3, {"thickness": 7}, "thickness"),
        (np.zeros((6, 6, 3), np.uint8), 3, {"thickness": 1.5}, "thickness"),
    ],
)
def test_invalid_input(rgb, scale, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        pixelize(rgb, scale, **kwargs)


def test_readonly_and_reversed_input() -> None:
    rgb = synthetic_frame()[:, ::-1]
    rgb.flags.writeable = False
    np.testing.assert_array_equal(pixelize(rgb, 3), pixelize(rgb.copy(), 3))


def test_versions_and_lazy_import() -> None:
    installed = versions()
    assert installed["pixeloe"] == "1.0.0"
    assert set(installed) == {"pixeloe", "torch", "numpy", "pillow"}
    assert all(installed.values())
    code = "import sys, sonoranlab.pixelize as p; p.versions(); assert 'torch' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_phase1_parity() -> None:
    work = Path(os.environ.get("SONORANLAB_WORK", "~/sonoranlab-work")).expanduser()
    raw = work / "benchmark/results/2026-10-18/E/relay_sentinel/2/set/raw/north_0.png"
    if not raw.is_file():
        pytest.skip(f"private Phase 1 raw frame {raw} not present")
    sys.path.insert(0, str(REPO / "benchmark/tools"))
    try:
        import process
    finally:
        sys.path.pop(0)
    with Image.open(raw) as im:
        rgb, _ = process.extract_alpha(im, 48)
    np.testing.assert_array_equal(pixelize(rgb, 3, thickness=0), process.downscale(rgb, 3, "k_centroid", 0))


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "LA", "P"])
def test_cli_composites_alpha(tmp_path: Path, mode: str) -> None:
    rgb = synthetic_frame()
    alpha = np.full(rgb.shape[:2], 255, np.uint8)
    alpha[:12] = 128
    im = Image.fromarray(np.dstack([rgb, alpha])).convert(mode)
    if mode == "P":
        im.info["transparency"] = 0
    source, out = tmp_path / "in.png", tmp_path / "out.png"
    im.save(source)
    original = source.read_bytes()
    with Image.open(source) as saved:
        rgba = np.asarray(saved.convert("RGBA")).astype(np.float32)
    weight = rgba[..., 3:] / 255
    # Non-default synthetic background tests the CLI's --bg handling.
    expected = pixelize(np.rint(rgba[..., :3] * weight + (40, 120, 200) * (1 - weight)).astype(np.uint8), 3)
    assert main(["pixelize", str(source), str(out), "--scale", "3", "--bg", "#2878c8"]) == 0
    with Image.open(out) as result:
        assert result.mode == "RGB"
        np.testing.assert_array_equal(np.asarray(result), expected)
    assert source.read_bytes() == original


def test_cli_default_background_and_options(tmp_path: Path) -> None:
    source, out = tmp_path / "in.png", tmp_path / "out.png"
    rgb = synthetic_frame()
    rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
    rgba[:12, :, 3] = 0
    Image.fromarray(rgba).save(source)
    rgb[:12] = (255, 0, 255)
    expected = pixelize(rgb, 3, method="nearest", thickness=1)
    assert main(["pixelize", str(source), str(out), "--scale", "3", "--method", "nearest", "--thickness", "1"]) == 0
    with Image.open(out) as result:
        np.testing.assert_array_equal(np.asarray(result), expected)


def test_cli_refuses_overwrite_and_bad_input(tmp_path: Path, capsys) -> None:
    source, out = tmp_path / "in.png", tmp_path / "out.png"
    Image.fromarray(synthetic_frame()).save(source)
    original = source.read_bytes()
    assert main(["pixelize", str(source), str(source), "--scale", "3"]) == 2
    assert "OUT must differ" in capsys.readouterr().err
    alias = tmp_path / "alias.png"
    alias.symlink_to(source)
    assert main(["pixelize", str(source), str(alias), "--scale", "3"]) == 2
    out.write_bytes(b"keep")
    assert main(["pixelize", str(source), str(out), "--scale", "3"]) == 2
    assert "OUT already exists" in capsys.readouterr().err
    assert out.read_bytes() == b"keep" and source.read_bytes() == original
    out.unlink()
    for options in (["--scale", "5"], ["--scale", "0"], ["--scale", "3", "--bg", "nope"]):
        assert main(["pixelize", str(source), str(out), *options]) == 2
        assert capsys.readouterr().err and not out.exists()
    assert main(["pixelize", str(tmp_path / "missing.png"), str(out), "--scale", "3"]) == 2
    assert capsys.readouterr().err and not out.exists()
    with pytest.raises(SystemExit) as exc:
        main(["pixelize", str(source), str(out), "--scale", "3", "--method", "unknown"])
    assert exc.value.code == 2
