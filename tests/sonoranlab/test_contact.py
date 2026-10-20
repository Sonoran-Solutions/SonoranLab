"""sonoranlab.contact: border replica, sheet geometry/badges/determinism, `sonoranlab contact`."""

import hashlib
import io
import json

import numpy as np
import pytest
from PIL import Image

from sonoranlab import contact as ct
from sonoranlab.cli import main

CELL = (9, 8)  # (w, h)
FOOT_Y = 5


def dot(x=4, y=4) -> np.ndarray:
    f = np.zeros((CELL[1], CELL[0], 4), np.uint8)
    f[y, x] = (200, 40, 40, 255)
    return f


def report(ok_flags: dict[tuple[str, int], bool], **extra) -> dict:
    frames = [
        {"index": n, "direction": d, "frame": i, "ok": ok, "issues": [] if ok else ["bbox_drift: moved 20px, limit 16"]}
        for n, ((d, i), ok) in enumerate(ok_flags.items())
    ]
    failures = [f"{r['direction']} {r['frame']}: {r['issues'][0]}" for r in frames if not r["ok"]]
    return {"pass": not failures, "failures": failures, "warnings": [], "frames": frames, **extra}


CLIPS = {"south": [dot(), dot(3), dot(5)], "east": [dot(), dot(3), dot(5)]}
ALL_OK = report({(d, i): True for d in CLIPS for i in range(3)}, character="toy", animation="walk")
ONE_BAD = report({(d, i): not (d == "east" and i == 1) for d in CLIPS for i in range(3)}, character="toy")


def sheet(rep=ALL_OK, **kw) -> Image.Image:
    return ct.contact_sheet(CLIPS, rep, cell=CELL, foot_y=FOOT_Y, **kw)


def test_standee_border_rings_around_a_dot():
    out = ct.standee_border(dot())
    got = {tuple(map(int, p)): tuple(out[tuple(p)]) for p in np.argwhere(out[..., 3] > 0)}
    want = {}
    for y in range(2, 7):
        for x in range(2, 7):
            d = max(abs(y - 4), abs(x - 4))
            want[(y, x)] = {0: (200, 40, 40, 255), 1: (14, 11, 10, 255), 2: (255, 255, 255, 255)}[d]
    assert got == want
    assert out.shape == dot().shape


def test_standee_border_touching_nothing_is_unchanged_and_input_untouched():
    empty = np.zeros((8, 9, 4), np.uint8)
    assert (ct.standee_border(empty) == empty).all()
    f = dot()
    before = f.copy()
    ct.standee_border(f)
    assert (f == before).all()


def test_standee_border_uses_alpha_128_threshold_and_custom_colours():
    f = np.zeros((7, 7, 4), np.uint8)
    f[3, 3] = (1, 2, 3, 127)  # below the threshold: not opaque, so no ring
    assert (ct.standee_border(f) == f).all()
    f[3, 3, 3] = 128
    out = ct.standee_border(f, keyline=(1, 1, 1), border=(2, 2, 2))
    assert tuple(out[3, 4]) == (1, 1, 1, 255) and tuple(out[3, 5]) == (2, 2, 2, 255) and out[3, 6, 3] == 0


def expected_size(rows, cols, scale):
    tw, th = CELL[0] * scale, ct.LABEL_H + CELL[1] * scale + ct.BADGE_H
    return (
        cols * (tw + ct.GAP) + ct.GAP,
        ct.HEADER_H + rows * (th + ct.GAP) + ct.GAP + rows * (CELL[1] + ct.GAP) + ct.FOOTER_H,
    )


@pytest.mark.parametrize("scale", [1, 4, 8])
def test_contact_sheet_size(scale):
    assert sheet(scale=scale).size == expected_size(2, 3, scale)
    assert sheet(scale=scale, rows=["east"]).size == expected_size(1, 3, scale)


def badge_px(img: Image.Image, row: int, col: int, scale=8) -> tuple[int, int, int]:
    tw, th = CELL[0] * scale, ct.LABEL_H + CELL[1] * scale + ct.BADGE_H
    x = ct.GAP + col * (tw + ct.GAP) + tw - 1  # right edge: clear of badge text
    y = ct.HEADER_H + row * (th + ct.GAP) + ct.GAP + ct.LABEL_H + ct.BADGE_H - 1  # badge sits under its own label
    return img.getpixel((x, y))[:3]


def test_fail_badge_only_on_failing_frames_and_header_follows_pass():
    img = sheet(ONE_BAD)
    for row, d in enumerate(CLIPS):
        for col in range(3):
            want = ct.RED if (d, col) == ("east", 1) else ct.GREEN
            assert badge_px(img, row, col) == want, (d, col)
    assert img.getpixel((img.width - 1, 0))[:3] == ct.RED
    assert sheet(ALL_OK).getpixel((img.width - 1, 0))[:3] == ct.GREEN


def test_frames_without_a_record_get_a_grey_badge():
    rep = report({("south", i): True for i in range(3)})
    assert badge_px(sheet(rep), 1, 0) == ct.GREY


def test_foot_line_and_border_are_drawn_on_the_frame():
    img = sheet(scale=8, border=True)
    x0, y0 = ct.GAP, ct.HEADER_H + ct.GAP + ct.LABEL_H + ct.BADGE_H
    assert img.getpixel((x0 + 4 * 8 + 3, y0 + (FOOT_Y + 1) * 8 - 1))[:3] == (255, 0, 0)  # foot line, 1 px tall
    assert img.getpixel((x0 + 4 * 8 + 3, y0 + (FOOT_Y + 1) * 8 - 2))[:3] != (255, 0, 0)
    assert img.getpixel((x0 + 3 * 8 + 3, y0 + 4 * 8 + 3))[:3] == (14, 11, 10)  # keyline left of the dot
    assert sheet(border=False).getpixel((x0 + 3 * 8 + 3, y0 + 4 * 8 + 3))[:3] in ct.CHECKER


def png_hash(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return hashlib.sha256(buf.getvalue()).hexdigest()


def test_deterministic_bytes_and_no_metadata():
    assert png_hash(sheet(ONE_BAD)) == png_hash(sheet(ONE_BAD))
    assert png_hash(sheet(ONE_BAD)) != png_hash(sheet(ALL_OK))
    buf = io.BytesIO()
    sheet(ONE_BAD).save(buf, "PNG")
    assert Image.open(buf).info == {}


def test_long_failure_list_fits_the_fixed_footer():
    many = report({(d, i): False for d in CLIPS for i in range(3)})
    many["failures"] = [f"south {i}: bbox_drift: " + "very long measured detail " * 6 for i in range(30)]
    assert sheet(many).size == expected_size(2, 3, 8)


def test_rejects_wrong_cell_and_foot_y():
    with pytest.raises(ValueError, match="expected cell"):
        ct.contact_sheet(CLIPS, ALL_OK, cell=(8, 8), foot_y=FOOT_Y)
    with pytest.raises(ValueError, match="foot_y"):
        ct.contact_sheet(CLIPS, ALL_OK, cell=CELL, foot_y=CELL[1])


def write_frames(d, clips=CLIPS):
    d.mkdir()
    for name, frames in clips.items():
        for i, f in enumerate(frames):
            Image.fromarray(f, "RGBA").save(d / f"{name}_{i}.png")


def test_load_clips_orders_directions_and_numeric_frames(tmp_path):
    clips = {"east": [dot(i % 9, i // 9) for i in range(11)], "north": [dot()]}
    write_frames(tmp_path / "f", clips)
    got = ct.load_clips(tmp_path / "f")
    assert list(got) == ["north", "east"]  # profile order, not alphabetical
    assert [int(np.argwhere(f[..., 3])[0][1]) for f in got["east"]] == [i % 9 for i in range(11)]  # _2 before _10
    with pytest.raises(ValueError, match="no <direction>"):
        (tmp_path / "empty").mkdir()
        ct.load_clips(tmp_path / "empty")


def run(tmp_path, *extra, rep=ONE_BAD):
    (tmp_path / "report.json").write_text(json.dumps(rep))
    if not (tmp_path / "f").exists():
        write_frames(tmp_path / "f")
    return main(
        ["contact", str(tmp_path / "f"), str(tmp_path / "report.json"), str(tmp_path / "c.png"), "--cell", "9", *extra]
    )


def test_cli_writes_sheet_and_refuses_overwrite(tmp_path, capsys):
    # CELL is 9x8 but the CLI takes a square cell: use 9x9 frames
    clips = {d: [np.pad(f, ((0, 1), (0, 0), (0, 0))) for f in fr] for d, fr in CLIPS.items()}
    write_frames(tmp_path / "f", clips)
    assert run(tmp_path, "--foot-y", "5", "--scale", "2") == 0
    assert Image.open(tmp_path / "c.png").size[0] == 3 * (9 * 2 + ct.GAP) + ct.GAP
    first = (tmp_path / "c.png").read_bytes()
    assert run(tmp_path, "--foot-y", "5") == 2
    assert "never overwritten" in capsys.readouterr().err
    assert (tmp_path / "c.png").read_bytes() == first


def test_cli_exit_2_on_bad_input(tmp_path, capsys):
    (tmp_path / "f").mkdir()
    assert run(tmp_path, "--foot-y", "5") == 2  # no frames
    assert "no <direction>" in capsys.readouterr().err
    assert (
        main(
            [
                "contact",
                str(tmp_path / "f"),
                str(tmp_path / "nope.json"),
                str(tmp_path / "c.png"),
                "--cell",
                "9",
                "--foot-y",
                "5",
            ]
        )
        == 2
    )
    assert not (tmp_path / "c.png").exists()
    assert run(tmp_path, "--foot-y", "5", rep={"failures": []}) == 2  # report without "pass" cannot be rendered
