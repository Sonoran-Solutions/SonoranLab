"""sonoranlab.export.godot on a synthetic sheet: file contents, determinism, refusals, CLI, and (when `godot` is on
PATH) the real engine loading, playing and checking the export (tools/godot/spriteframes_check)."""

import hashlib
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sonoranlab.export.godot import animation_specs, export_godot

REPO = Path(__file__).resolve().parents[2]
CHECK = REPO / "tools" / "godot" / "spriteframes_check"
ROWS = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"]
CELL = 16
CLIPS = [
    {"name": "run", "start": 1, "frames": 4, "fps": 10, "loop": True},
    {"name": "cast", "start": 5, "frames": 3, "fps": 6, "loop": False, "loop_from": 1},
]
COLUMNS = 8
PALETTE = np.array([(17, 13, 12), (200, 30, 30), (230, 180, 140), (40, 60, 200), (30, 160, 60)], np.uint8)  # synthetic


def make_sheet(columns: int = COLUMNS) -> np.ndarray:
    """Every cell gets its own asymmetric random glyph; the west rows are the horizontally mirrored east rows."""
    rng = np.random.default_rng(85)
    sheet = np.zeros((len(ROWS) * CELL, columns * CELL, 4), np.uint8)
    for r in range(len(ROWS)):
        for c in range(1, columns):
            cell = sheet[r * CELL : (r + 1) * CELL, c * CELL : (c + 1) * CELL]
            ys, xs = rng.integers(2, CELL - 2, (2, 40))
            cell[ys, xs, :3] = PALETTE[rng.integers(0, len(PALETTE), 40)]
            cell[ys, xs, 3] = 255
    for west, east in (("north-west", "north-east"), ("west", "east"), ("south-west", "south-east")):
        w, e = ROWS.index(west), ROWS.index(east)
        for c in range(columns):
            sheet[w * CELL : (w + 1) * CELL, c * CELL : (c + 1) * CELL] = sheet[
                e * CELL : (e + 1) * CELL, c * CELL : (c + 1) * CELL, :
            ][:, ::-1]
    return sheet


def export(sheet, out, **kw):
    args = {
        "character": "toy",
        "cell": (CELL, CELL),
        "rows": ROWS,
        "clips": CLIPS,
        "out_dir": out,
        "provenance": {"job": "unit", "validator_pass": True},
    }
    return export_godot(sheet, **{**args, **kw})


def test_files_and_tres_contents(tmp_path):
    sheet = make_sheet()
    paths = export(sheet, tmp_path)
    assert {k: Path(v).name for k, v in paths.items()} == {
        "png": "toy.png",
        "tres": "toy_frames.tres",
        "sidecar": "toy.sonoranlab.json",
    }
    assert sorted(p.name for p in (tmp_path / "toy").iterdir()) == ["toy.png", "toy.sonoranlab.json", "toy_frames.tres"]

    tres = Path(paths["tres"]).read_text()
    assert tres.startswith('[gd_resource type="SpriteFrames" ') and "format=3" in tres.splitlines()[0]
    assert tres.count("[ext_resource") == 1 and 'path="toy.png"' in tres
    assert tres.count("[sub_resource") == len(ROWS) * (4 + 3)
    for name in ("run_north-west", "cast_south-east", "run_east"):
        assert f'"name": &"{name}"' in tres
    assert len(re.findall(r'"name": &"', tres)) == len(ROWS) * 2
    # one animation by hand: run_east = row 6, columns 1..4, 10 fps, loop
    assert "region = Rect2(16, 96, 16, 16)" in tres and "region = Rect2(64, 96, 16, 16)" in tres
    specs = animation_specs(ROWS, (CELL, CELL), CLIPS)
    assert specs["run_east"] == {"frames": [[16 * c, 96, 16, 16] for c in range(1, 5)], "speed": 10.0, "loop": True}
    assert specs["cast_west"]["loop"] is False and specs["cast_west"]["speed"] == 6.0
    assert specs["cast_west"]["frames"][0] == [80, 32, 16, 16]
    assert len(specs) == 16


def test_png_is_palette_indexed_lossless_and_clean(tmp_path):
    sheet = make_sheet()
    png = Path(export(sheet, tmp_path)["png"]).read_bytes()
    im = Image.open(io.BytesIO(png))
    assert im.mode == "P" and im.size == sheet.shape[1::-1] and not im.info.get("interlace")
    assert np.array_equal(np.asarray(im.convert("RGBA")), sheet)
    chunks, i = [], 8
    while i < len(png):
        n = int.from_bytes(png[i : i + 4], "big")
        chunks.append(png[i + 4 : i + 8].decode())
        i += 12 + n
    assert set(chunks) == {"IHDR", "PLTE", "tRNS", "IDAT", "IEND"}  # no sRGB/gAMA/iCCP/tIME/text


def test_png_falls_back_to_rgba(tmp_path):
    sheet = make_sheet()
    sheet[2, 17] = (1, 2, 3, 128)
    meta = json.loads(Path(export(sheet, tmp_path / "a")["sidecar"]).read_text())
    assert meta["export"]["png_mode"] == "RGBA"
    im = Image.open(tmp_path / "a" / "toy" / "toy.png")
    assert im.mode == "RGBA" and np.array_equal(np.asarray(im), sheet)


def test_sidecar(tmp_path):
    paths = export(make_sheet(), tmp_path)
    meta = json.loads(Path(paths["sidecar"]).read_text())
    assert meta["provenance"] == {"job": "unit", "validator_pass": True}
    for name in ("toy.png", "toy_frames.tres"):
        assert meta["sha256"][name] == hashlib.sha256((tmp_path / "toy" / name).read_bytes()).hexdigest()
    entry = meta["characters_json_entry"]
    assert set(entry) == {"sheet", "cell", "columns", "clips", "colours"}
    assert entry["sheet"] == "toy.png" and entry["cell"] == CELL and entry["columns"] == COLUMNS
    assert entry["clips"] == [
        {"name": "run", "start": 1, "frames": 4, "fps": 10, "loop": True},
        {"name": "cast", "start": 5, "frames": 3, "fps": 6, "loop": False, "loop_from": 1},
    ]
    assert entry["colours"] == len(PALETTE)
    assert meta["export"]["loop_from"] == {"cast": 1}
    assert any("standing_pixels" in n for n in meta["notes"])


def test_deterministic(tmp_path):
    a, b = export(make_sheet(), tmp_path / "a"), export(make_sheet(), tmp_path / "b")
    for k in a:
        assert Path(a[k]).read_bytes() == Path(b[k]).read_bytes(), k


@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"character": "../evil"}, "character"),
        ({"cell": (16, 8)}, "square"),
        ({"rows": ROWS[:7]}, "rows of"),
        ({"clips": [{"name": "run", "start": 6, "frames": 4, "fps": 10, "loop": True}]}, "do not fit"),
        ({"clips": [{"name": "run", "start": 1, "frames": 2, "fps": 0, "loop": True}]}, "fps"),
        ({"clips": [{"name": "run", "start": 1, "frames": 2, "fps": 5, "loop": False, "loop_from": 2}]}, "loop_from"),
        ({"anim_name": "{animation}"}, "twice"),
    ],
)
def test_bad_inputs_write_nothing(tmp_path, kw, msg):
    with pytest.raises(ValueError, match=msg):
        export(make_sheet(), tmp_path, **kw)
    assert not any(tmp_path.iterdir())


def test_refuses_inside_the_game_and_never_overwrites(tmp_path):
    game = tmp_path / "game"
    (game / "assets").mkdir(parents=True)
    for out in (game, game / "assets", game / "assets" / "toy"):
        with pytest.raises(ValueError, match="shipped game"):
            export(make_sheet(), out, forbid_under=game)
    link = tmp_path / "link"
    link.symlink_to(game)
    with pytest.raises(ValueError, match="shipped game"):
        export(make_sheet(), link, forbid_under=game)
    assert not (game / "toy").exists()
    export(make_sheet(), tmp_path / "ok", forbid_under=game)  # a sibling is fine
    with pytest.raises(ValueError, match="never overwrite"):
        export(make_sheet(), tmp_path / "ok")
    (tmp_path / "pre" / "toy").mkdir(parents=True)  # existing but empty is fine
    export(make_sheet(), tmp_path / "pre")


def test_custom_anim_name(tmp_path):
    paths = export(make_sheet(), tmp_path, anim_name="{direction}/{animation}")
    assert '&"north/run"' in Path(paths["tres"]).read_text()


# --- the real engine -------------------------------------------------------------------------------------------------


def expected_json(sheet: np.ndarray, character: str = "toy") -> dict:
    """The checker's expectations, computed from the inputs by hand (not through animation_specs)."""
    anims = {}
    for clip in CLIPS:
        for r, d in enumerate(ROWS):
            frames = []
            for i in range(clip["frames"]):
                x, y = (clip["start"] + i) * CELL, r * CELL
                cell = np.ascontiguousarray(sheet[y : y + CELL, x : x + CELL])
                frames.append([x, y, CELL, CELL, hashlib.sha256(cell.tobytes()).hexdigest()])
            anims[f"{clip['name']}_{d}"] = {"frames": frames, "speed": float(clip["fps"]), "loop": clip["loop"]}
    return {
        "sheet": f"res://{character}/{character}.png",
        "sheet_size": [sheet.shape[1], sheet.shape[0]],
        "animations": anims,
    }


def godot_project(tmp_path: Path, sheet: np.ndarray) -> Path:
    proj = tmp_path / "proj"
    shutil.copytree(CHECK, proj)
    export(sheet, proj)
    (proj / "expected.json").write_text(json.dumps(expected_json(sheet)))
    return proj


def godot(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["godot", "--headless", *args], cwd=cwd, capture_output=True, text=True, timeout=300)


@pytest.mark.skipif(shutil.which("godot") is None, reason="godot is not on PATH (CI has none)")
def test_godot_imports_plays_and_matches(tmp_path):
    proj = godot_project(tmp_path, make_sheet())
    imp = godot("--path", str(proj), "--import", cwd=tmp_path)
    log = imp.stdout + imp.stderr
    assert imp.returncode == 0 and "ERROR" not in log, log
    # Godot's own default import is already the game's pixel-art setting: no .import is written by the export
    dot_import = (proj / "toy" / "toy.png.import").read_text()
    assert "compress/mode=0" in dot_import and "mipmaps/generate=false" in dot_import
    run = godot(
        "--path", str(proj), "--fixed-fps", "60", "--script", "res://check_spriteframes.gd", "--",
        "res://toy/toy_frames.tres", "res://expected.json", cwd=tmp_path,
    )  # fmt: skip
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert run.returncode == 0 and result["pass"] and result["animations_checked"] == 16, run.stdout + run.stderr
    assert "ERROR" not in run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("godot") is None, reason="godot is not on PATH (CI has none)")
def test_godot_checker_catches_a_wrong_tres(tmp_path):
    proj = godot_project(tmp_path, make_sheet())
    tres = proj / "toy" / "toy_frames.tres"
    tres.write_text(
        tres.read_text().replace('"speed": 10.0', '"speed": 12.0', 1).replace("Rect2(16, 0,", "Rect2(0, 0,", 1)
    )
    godot("--path", str(proj), "--import", cwd=tmp_path)
    run = godot(
        "--path", str(proj), "--fixed-fps", "60", "--script", "res://check_spriteframes.gd", "--",
        "res://toy/toy_frames.tres", "res://expected.json", cwd=tmp_path,
    )  # fmt: skip
    assert run.returncode == 1
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert any("speed" in f for f in out["failures"]) and any("region" in f for f in out["failures"])
