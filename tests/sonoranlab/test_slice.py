"""sonoranlab.slice + `sonoranlab slice`: frame dirs, sheet slicing, game-layout packing, round trip. All synthetic."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sonoranlab.cli import main
from sonoranlab.slice import load_clips, pack_sheet, read_frame_dir, slice_boxes, slice_sheet

ROWS = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"]
MIRROR = {"north-west": "north-east", "west": "east", "south-west": "south-east"}
GENERATED = [d for d in ROWS if d not in MIRROR]
CELL, FRAMES = (6, 5), 3  # non-square on purpose


def frame(seed: int) -> np.ndarray:
    """Asymmetric (left and right differ), opaque everywhere, unique per seed."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (CELL[1], CELL[0], 4), dtype=np.uint8)


def clips() -> dict[str, list[np.ndarray]]:
    return {d: [frame(10 * r + i) for i in range(FRAMES)] for r, d in enumerate(GENERATED)}


def test_round_trip_all_eight_directions() -> None:
    c = clips()
    stills = {d: frame(900 + r) for r, d in enumerate(GENERATED)}
    sheet = pack_sheet(c, CELL, ROWS, FRAMES, mirror=MIRROR, stills=stills, required=ROWS)
    assert sheet.shape == (8 * CELL[1], (FRAMES + 1) * CELL[0], 4)
    back = slice_sheet(sheet, CELL, ROWS, FRAMES, start_col=1)
    assert list(back) == ROWS
    for d in GENERATED:
        assert all(np.array_equal(a, b) for a, b in zip(c[d], back[d], strict=True))
    for d, src in MIRROR.items():
        assert all(np.array_equal(b, a[:, ::-1]) for a, b in zip(c[src], back[d], strict=True))
        assert not np.array_equal(back[d][0], c[src][0])  # the flip is real
    col0 = slice_sheet(sheet, CELL, ROWS, FRAMES + 1)  # start_col 0 -> still is frame 0
    assert all(np.array_equal(col0[d][0], s) for d, s in stills.items())
    assert not col0["west"][0].any()  # no still for mirrored rows


def test_pack_leaves_unrequired_rows_transparent_and_requires_when_asked() -> None:
    c = {"east": clips()["east"]}
    sheet = pack_sheet(c, CELL, ROWS, FRAMES, mirror=MIRROR)
    row = {d: sheet[r * CELL[1] : (r + 1) * CELL[1]] for r, d in enumerate(ROWS)}
    assert [d for d in ROWS if row[d].any()] == ["west", "east"]  # east and its mirror only
    assert np.array_equal(row["west"][:, CELL[0] :], np.concatenate([f[:, ::-1] for f in c["east"]], 1))
    assert not sheet[:, : CELL[0]].any()  # column 0 empty without stills
    with pytest.raises(ValueError, match="required row north has no frames"):
        pack_sheet(c, CELL, ROWS, FRAMES, mirror=MIRROR, required=["east", "north"])
    with pytest.raises(ValueError, match="required row north-east"):
        pack_sheet({}, CELL, ROWS, FRAMES, mirror=MIRROR, required=["north-east"])


def test_pack_errors() -> None:
    c = clips()
    with pytest.raises(ValueError, match=r"west.*mirror source"):
        pack_sheet({**c, "west": c["east"]}, CELL, ROWS, FRAMES, mirror=MIRROR)
    with pytest.raises(ValueError, match="not in the sheet rows"):
        pack_sheet({**c, "up": c["east"]}, CELL, ROWS, FRAMES, mirror=MIRROR)
    with pytest.raises(ValueError, match="2 frames, expected 3"):
        pack_sheet({**c, "east": c["east"][:2]}, CELL, ROWS, FRAMES, mirror=MIRROR)
    with pytest.raises(ValueError, match="south frame 1: shape"):
        pack_sheet(
            {**c, "south": [c["south"][0], np.zeros((5, 5, 4), np.uint8), c["south"][2]]},
            CELL,
            ROWS,
            FRAMES,
            mirror=MIRROR,
        )
    with pytest.raises(ValueError, match="still north"):
        pack_sheet(c, CELL, ROWS, FRAMES, mirror=MIRROR, stills={"north": np.zeros((2, 2, 4), np.uint8)})
    with pytest.raises(ValueError, match="start_col >= 1"):
        pack_sheet(c, CELL, ROWS, FRAMES, mirror=MIRROR, stills={"north": frame(1)}, start_col=0)


def test_pack_does_not_touch_inputs() -> None:
    c = clips()
    before = {d: [f.copy() for f in fs] for d, fs in c.items()}
    pack_sheet(c, CELL, ROWS, FRAMES, mirror=MIRROR)
    assert all(np.array_equal(a, b) for d in c for a, b in zip(c[d], before[d], strict=True))


def test_slice_sheet_dimension_errors() -> None:
    sheet = pack_sheet(clips(), CELL, ROWS, FRAMES, mirror=MIRROR)
    with pytest.raises(ValueError, match="expected 30x40"):
        slice_sheet(sheet, CELL, ROWS, FRAMES, start_col=2)
    with pytest.raises(ValueError, match="expected 24x35"):
        slice_sheet(sheet, CELL, ROWS[:7], FRAMES + 1, start_col=0)
    out = slice_sheet(sheet, CELL, ROWS, FRAMES, start_col=1)
    out["east"][0][:] = 0  # slices are copies
    assert sheet.any()


def test_slice_boxes() -> None:
    sheet = np.arange(10 * 12 * 4, dtype=np.uint8).reshape(10, 12, 4)
    got = slice_boxes(sheet, {"east": [(0, 0, 4, 3), (4, 5, 8, 5)], "north": [(2, 2, 1, 1)]})
    assert np.array_equal(got["east"][1], sheet[5:10, 4:12]) and got["east"][0].shape == (3, 4, 4)
    for box in [(9, 0, 4, 3), (0, 8, 2, 3), (-1, 0, 2, 2), (0, 0, 0, 2)]:
        with pytest.raises(ValueError, match="east box 0"):
            slice_boxes(sheet, {"east": [box]})


def write(p: Path, a: np.ndarray) -> None:
    Image.fromarray(a).save(p)


def test_read_frame_dir_and_load_clips(tmp_path: Path) -> None:
    for name in ["east_0", "east_1", "east_2", "north-east_0", "south_0"]:
        write(tmp_path / f"{name}.png", frame(1))
    write(tmp_path / "still_south.png", frame(2))  # not a frame name: ignored
    (tmp_path / "selection.json").write_text("{}")
    found = read_frame_dir(tmp_path, ROWS)
    assert list(found) == ["south", "east", "north-east"]  # profile direction order
    assert [p.name for p in found["east"]] == ["east_0.png", "east_1.png", "east_2.png"]
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    clips_ = load_clips(found)
    assert clips_["east"][0].shape == (CELL[1], CELL[0], 4) and np.array_equal(clips_["east"][0], frame(1))
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_load_clips_rgb_gets_opaque_alpha(tmp_path: Path) -> None:
    Image.fromarray(frame(1)[..., :3], "RGB").save(tmp_path / "east_0.png")
    f = load_clips(read_frame_dir(tmp_path, ROWS))["east"][0]
    assert f.shape[2] == 4 and (f[..., 3] == 255).all()


@pytest.mark.parametrize(
    "names, match",
    [
        (["east_0", "east_2"], r"east_1\.png: missing"),
        (["east_1"], r"east_0\.png: missing"),
        (["east_1", "east_01", "east_0"], r"east_(01|1)\.png: duplicate index 1"),
        (["east_0", "up_0"], r"up_0\.png: unknown direction"),
    ],
)
def test_read_frame_dir_errors(tmp_path: Path, names: list[str], match: str) -> None:
    for n in names:
        write(tmp_path / f"{n}.png", frame(1))
    with pytest.raises(ValueError, match=match):
        read_frame_dir(tmp_path, ROWS)


def test_cli_slice(tmp_path: Path, capsys) -> None:
    cell = 6
    sq = lambda seed: np.random.default_rng(seed).integers(0, 256, (cell, cell, 4), dtype=np.uint8)  # noqa: E731
    c = {d: [sq(10 * r + i) for i in range(FRAMES)] for r, d in enumerate(GENERATED)}
    sheet = pack_sheet(c, (cell, cell), ROWS, FRAMES, mirror=MIRROR)
    write(tmp_path / "sheet.png", sheet)
    out = tmp_path / "frames"
    argv = ["slice", str(tmp_path / "sheet.png"), str(out), "--cell", "6", "--rows", ",".join(ROWS), "--frames", "3"]
    assert main([*argv, "--start-col", "1"]) == 0
    assert len(list(out.glob("*.png"))) == 8 * FRAMES
    assert np.array_equal(np.asarray(Image.open(out / "east_2.png")), c["east"][2])
    assert np.array_equal(np.asarray(Image.open(out / "west_0.png")), c["east"][0][:, ::-1])
    assert main([*argv, "--start-col", "1"]) == 2  # exists: no overwrite
    assert "already exists" in capsys.readouterr().err
    assert main([*argv[:2], str(tmp_path / "x"), *argv[3:], "--start-col", "0"]) == 2  # wrong grid
    assert not (tmp_path / "x").exists()
    assert (
        main(
            ["slice", str(tmp_path / "nope.png"), str(tmp_path / "y"), "--cell", "6", "--rows", "east", "--frames", "1"]
        )
        == 2
    )


@pytest.mark.parametrize("row", ["../raw/east", "/east", "East", "east_1", "east--west", "east\n", ""])
def test_invalid_rows_rejected(tmp_path, capsys, row):
    sheet = np.zeros((6, 6, 4), np.uint8)
    for call in (
        lambda: read_frame_dir(tmp_path, [row]),
        lambda: slice_sheet(sheet, (6, 6), [row], 1),
        lambda: pack_sheet({}, (6, 6), [row], 1, mirror={}),
    ):
        with pytest.raises(ValueError) as exc:
            call()
        assert repr(row) in str(exc.value)
    source, out = tmp_path / "sheet.png", tmp_path / "frames"
    write(source, sheet)
    raw = tmp_path / "raw"
    raw.mkdir()
    sentinel = raw / "east_0.png"
    sentinel.write_bytes(b"raw input")
    assert main(["slice", str(source), str(out), "--cell", "6", "--rows", row, "--frames", "1"]) == 2
    assert repr(row) in capsys.readouterr().err
    assert not out.exists() and sentinel.read_bytes() == b"raw input"
