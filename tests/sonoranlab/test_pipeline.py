"""`sonoranlab process` (DYL-86) end to end on a synthetic toy character with real CPU PixelOE: exit codes, the job dir,
raw immutability, determinism, sheet input, the Godot check (when `godot` is on PATH) and, when the private Phase 1
sets are on disk, processing-v1 parity on real frames."""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from sonoranlab import pipeline
from sonoranlab.artifacts import verify_raw
from sonoranlab.cli import main
from sonoranlab.profile import check_profile, load_profile

REPO = Path(__file__).resolve().parents[2]
ORDER = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"]
GEN = ["north", "north-east", "east", "south-east", "south"]
MIRROR = {"north-west": "north-east", "west": "east", "south-west": "south-east"}
PALETTE = [(17, 13, 12), (204, 34, 34), (230, 180, 140), (40, 60, 200)]  # synthetic, not the game's
CELL, SCALE = 24, 2
LIMITS = {"max_foot_slide_px": 3, "max_bbox_drift_px": 2, "opaque_area_cv_warn": 0.2, "loop_closure_ratio_warn": 1.5}
PROCESSING = {"version": "processing-v1", "downscaler": "k_centroid", "thickness": 0, "despill_px": 2, "key_tol": 48}


def toy_profile(root: Path, edit=None) -> Path:
    pal = root / "palette.png"
    Image.fromarray(np.array([PALETTE], np.uint8)).save(pal)
    (root / "game").mkdir(exist_ok=True)
    doc = {
        "id": "toy",
        "name": "Toy",
        "style": "pixel",
        "canvas": {"cell": "per_character", "gen_scale": SCALE, "padding": 2, "sheet_layout": "rows_by_direction"},
        "palette": {"source": "palette.png", "sha256": hashlib.sha256(pal.read_bytes()).hexdigest(), "colours": 4}
        | {"max_colors_per_cell": 4, "dither": "none"},
        "anchor": {"mode": "foot", "foot_y": "cell_minus_4", "anchor_x": "cell_half", "registration": "per_clip"},
        "alpha": {"threshold": 128, "bg_key": "#ff00ff", "fringe_passes": 1, "fringe_tolerance": 0},
        "directions": {"order": ORDER, "generate": GEN, "mirror_animations": MIRROR},
        "animations": {"idle": {"frames": 2, "fps": 5, "loop": True}},
        "validator": {"label": "toy defaults", **LIMITS},
        "characters": {"hero": {"cell": CELL, "anchor_mode": "foot", **LIMITS}},
        "export": {"godot_project": "game", "sprites_dir": "res://sprites", "manifest": "characters.json"},
        "autonomy": {"allow_auto_approve": False, "max_seeds_per_attempt": 4, "max_attempts_per_animation": 3}
        | {"escalate_after": "ask"},
        "processing": dict(PROCESSING),
    }
    if edit:
        edit(doc)
    (root / "toy.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return root / "toy.yaml"


def figure(d: str, i: int, lift: int = 0) -> np.ndarray:
    """Cell-size RGBA toy: outlined body, a belt whose length depends on the direction (mirrors are checkable) and,
    in frame 1, a hand."""
    f = np.zeros((CELL, CELL, 4), np.uint8)
    top, bottom = 6 - lift, 19 - lift
    f[top : bottom + 1, 8:16] = (*PALETTE[0], 255)
    f[top + 1 : bottom, 9:15] = (*PALETTE[2], 255)
    k = GEN.index(d)
    f[top + 4, 9 : 10 + k] = (*PALETTE[1], 255)  # asymmetric belt, length per direction
    f[top + 8, 9:15] = (*PALETTE[3], 255)
    if i == 1:
        f[top + 6, 16] = (*PALETTE[0], 255)
    return f


def hd(f: np.ndarray) -> np.ndarray:
    """Cell RGBA -> HD RGB on the magenta key (raw frames are RGB-on-key renders, edge = cell x SCALE)."""
    big = f.repeat(SCALE, 0).repeat(SCALE, 1)
    out = np.full((*big.shape[:2], 3), (255, 0, 255), np.uint8)
    out[big[..., 3] > 0] = big[big[..., 3] > 0, :3]
    return out


def raw_set(root: Path, *, lift: dict | None = None, skip: str | None = None) -> Path:
    raw = root / "set" / "raw"
    raw.mkdir(parents=True)
    for d in GEN:
        if d == skip:
            continue
        for i in range(2):
            Image.fromarray(hd(figure(d, i, (lift or {}).get((d, i), 0)))).save(raw / f"{d}_{i}.png")
    (root / "set" / "selection.json").write_text(json.dumps({"set": "toy/1", "seed": 7, "track": "E"}))
    return raw


def stills_dir(root: Path) -> Path:
    s = root / "stills"
    s.mkdir()
    for k, d in enumerate(ORDER):
        f = np.zeros((CELL, CELL, 4), np.uint8)
        f[4 : 6 + k, 10:14] = (*PALETTE[k % 4], 255)
        Image.fromarray(f).save(s / f"still_{d}.png")
    return s


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def process(raw: Path, prof: Path, out: Path, *extra: str, character: str = "hero", anim: str = "idle") -> int:
    argv = ["process", str(raw), "--profile", str(prof), "--character", character, "--anim", anim, "--out", str(out)]
    return main([*argv, *extra])


@pytest.fixture
def toy(tmp_path):
    return raw_set(tmp_path), toy_profile(tmp_path)


def test_pass_writes_every_artifact_and_never_touches_raw(tmp_path, toy, capsys):
    raw, prof = toy
    stills = stills_dir(tmp_path)
    before = tree_hashes(raw.parent) | {f"stills/{k}": v for k, v in tree_hashes(stills).items()}
    job = tmp_path / "job"
    assert process(raw, prof, job, "--stills", str(stills)) == 0
    out = capsys.readouterr().out
    assert out.startswith("PASS hero idle") and "export:" in out
    after = tree_hashes(raw.parent) | {f"stills/{k}": v for k, v in tree_hashes(stills).items()}
    assert before == after  # sources untouched

    sources = {p.name: p for p in [*raw.iterdir(), *stills.iterdir(), raw.parent / "selection.json"]}
    assert sorted(p.name for p in (job / "raw").iterdir()) == sorted(sources) and len(sources) == 10 + 8 + 1
    for p in (job / "raw").iterdir():
        assert p.read_bytes() == sources[p.name].read_bytes() and not p.stat().st_mode & 0o222, p.name
    for stage in ("01_alpha", "02_pixelize", "03_palette", "04_clean", "05_registered"):
        files = sorted(p.name for p in (job / "processed" / stage).iterdir())
        assert files == sorted(f"{d}_{i}.png" for d in GEN for i in range(2)), stage
    assert Image.open(job / "processed" / "01_alpha" / "east_0.png").size == (CELL * SCALE,) * 2
    assert Image.open(job / "processed" / "05_registered" / "east_0.png").size == (CELL, CELL)

    rep = json.loads((job / "report.json").read_text())
    assert rep["pass"] and rep["failures"] == [] and rep["params"]["scale"] == SCALE
    assert rep["params"]["processing"] == "processing-v1" and rep["chain"]["off_palette_before"] == 0
    man = json.loads((job / "manifest.json").read_text())
    assert man["result"] == {"pass": True, "exit": 0, "exported": True, "error": None}
    assert man["upstream"]["selection.json"]["seed"] == 7 and verify_raw(job) == []
    assert man["params"]["thickness"] == 0 and man["params"]["key_tol"] == 48 and man["godot_check"] is None
    for rel, h in man["outputs"].items():
        assert hashlib.sha256((job / rel).read_bytes()).hexdigest() == h, rel
    assert {"report.json", "contact.png", "export/hero/hero.png", "export/hero/hero_frames.tres"} <= set(man["outputs"])

    sheet = np.asarray(Image.open(job / "export" / "hero" / "hero.png").convert("RGBA"))
    assert sheet.shape == (len(ORDER) * CELL, 3 * CELL, 4)

    def cell(r: str, c: int) -> np.ndarray:
        return sheet[ORDER.index(r) * CELL : (ORDER.index(r) + 1) * CELL, c * CELL : (c + 1) * CELL]

    for c in (1, 2):
        reg = np.asarray(Image.open(job / "processed" / "05_registered" / f"east_{c - 1}.png").convert("RGBA"))
        assert np.array_equal(cell("east", c), reg) and np.array_equal(cell("west", c), reg[:, ::-1])
    assert np.array_equal(
        cell("north-west", 0), np.asarray(Image.open(stills / "still_north-west.png").convert("RGBA"))
    )
    side = json.loads((job / "export" / "hero" / "hero.sonoranlab.json").read_text())
    assert side["provenance"]["report_sha256"] == man["outputs"]["report.json"]
    assert side["characters_json_entry"]["clips"] == [{"name": "idle", "start": 1, "frames": 2, "fps": 5, "loop": True}]


def test_rerun_is_byte_identical(tmp_path, toy):
    raw, prof = toy
    assert process(raw, prof, tmp_path / "a") == 0 and process(raw, prof, tmp_path / "b") == 0
    ma, mb = (json.loads((tmp_path / j / "manifest.json").read_text()) for j in "ab")
    assert ma["outputs"] == mb["outputs"] and len(ma["outputs"]) == 5 * 10 + 2 + 3
    assert (tmp_path / "a" / "report.json").read_bytes() == (tmp_path / "b" / "report.json").read_bytes()
    assert tree_hashes(tmp_path / "a" / "export") == tree_hashes(tmp_path / "b" / "export")


def test_failing_clip_exits_1_without_export(tmp_path, capsys):
    raw, prof = raw_set(tmp_path, lift={("east", 1): 3}), toy_profile(tmp_path)
    job = tmp_path / "job"
    assert process(raw, prof, job) == 1
    err = capsys.readouterr().err
    assert err.startswith("FAIL hero idle") and "(east/1)" in err and "bbox_drift" in err
    assert str(job / "report.json") in err and str(job / "contact.png") in err
    assert not (job / "export").exists()
    rep = json.loads((job / "report.json").read_text())
    assert not rep["pass"] and any(f.startswith("bbox_drift: frame 7 (east/1)") for f in rep["failures"])
    man = json.loads((job / "manifest.json").read_text())
    assert man["result"]["exit"] == 1 and not man["result"]["exported"] and (job / "contact.png").is_file()
    assert not any(k.startswith("export/") for k in man["outputs"])


def test_missing_direction_is_a_validator_failure(tmp_path, capsys):
    raw, prof = raw_set(tmp_path, skip="south"), toy_profile(tmp_path)
    assert process(raw, prof, tmp_path / "job") == 1
    assert "frame_count: south has 0 frames, expected 2" in capsys.readouterr().err
    assert not (tmp_path / "job" / "export").exists()


@pytest.mark.parametrize(
    "case, msg",
    [
        ("not_empty", "not an empty directory"),
        ("bad_scale", "not an integer multiple"),
        ("unknown_character", "not in `characters:`"),
        ("unknown_anim", "is not a clip of hero"),
        ("inside_game", "inside the shipped game"),
        ("start_col", "--start-col only applies with --sheet"),
        ("anchors", "rig registration"),
        ("no_processing", "no `processing:` section"),
        ("west_frames", "unknown direction 'west'"),
        ("no_frames", "no <direction>_<i>.png frames"),
    ],
)
def test_usage_and_input_errors_exit_2(tmp_path, toy, capsys, case, msg):
    raw, prof = toy
    job, extra, kw = tmp_path / "job", [], {}
    if case == "not_empty":
        job.mkdir()
        (job / "x").write_text("x")
    elif case == "bad_scale":
        for p in raw.glob("*.png"):
            Image.open(p).resize((50, 50)).save(p)
    elif case == "unknown_character":
        kw["character"] = "nobody"
    elif case == "unknown_anim":
        kw["anim"] = "run"
    elif case == "inside_game":
        job = tmp_path / "game" / "job"
    elif case == "start_col":
        extra = ["--start-col", "1"]
    elif case == "anchors":
        extra = ["--anchors", str(tmp_path / "anchors.json")]
    elif case == "no_processing":
        prof = toy_profile(tmp_path, lambda d: d.pop("processing"))
    elif case == "west_frames":
        shutil.copy(raw / "east_0.png", raw / "west_0.png")
    elif case == "no_frames":
        for p in raw.glob("*.png"):
            p.unlink()
    assert process(raw, prof, job, *extra, **kw) == 2
    assert msg in capsys.readouterr().err
    if case != "not_empty":
        assert not job.exists()


def test_raw_sheet_input_matches_frame_dir(tmp_path, toy):
    raw, prof = toy
    sheet = np.zeros((len(ORDER) * CELL * SCALE, 3 * CELL * SCALE, 3), np.uint8)
    sheet[:] = (255, 0, 255)
    e = CELL * SCALE
    for r, d in enumerate(ORDER):
        for i in range(2):
            if d in GEN:
                sheet[r * e : (r + 1) * e, (1 + i) * e : (2 + i) * e] = np.asarray(Image.open(raw / f"{d}_{i}.png"))
    (tmp_path / "sheets").mkdir()
    Image.fromarray(sheet).save(tmp_path / "sheets" / "raw_sheet.png")
    assert process(tmp_path / "sheets" / "raw_sheet.png", prof, tmp_path / "s", "--sheet") == 0
    assert process(raw, prof, tmp_path / "d") == 0
    for stage in ("04_clean", "05_registered"):
        assert tree_hashes(tmp_path / "s" / "processed" / stage) == tree_hashes(tmp_path / "d" / "processed" / stage)
    assert (tmp_path / "s" / "raw" / "raw_sheet.png").is_file()
    assert json.loads((tmp_path / "s" / "manifest.json").read_text())["params"]["input"]["start_col"] == 1
    assert process(tmp_path / "sheets" / "raw_sheet.png", prof, tmp_path / "t", "--sheet", "--start-col", "0") == 2


def test_process_frame_matches_processing_v1(tmp_path):
    """Soft-alpha HD frame with key spill and off-palette colours: the package chain equals process.py's, stats too."""
    sys.path.insert(0, str(REPO / "benchmark" / "tools"))
    import process as P

    rng = np.random.default_rng(86)
    a = np.zeros((48, 48, 4), np.uint8)
    a[8:40, 12:36, :3] = rng.integers(0, 256, (32, 24, 3))
    a[8:40, 12:36, 3] = 255
    a[7, 12:36, 3] = a[40, 12:36, 3] = 90
    a[7, 12:36, :3] = (230, 40, 230)
    im = Image.fromarray(a, "RGBA")
    pal = np.unique(np.array([*PALETTE, (250, 250, 250), (120, 10, 120)], np.uint8), axis=0)
    want, stats = P.process_frame(im, 2, pal, "k_centroid", 0, 48, 2, 4)
    kw = {"bg_key": (255, 0, 255), "key_tol": 48, "despill_px": 2, "downscaler": "k_centroid", "thickness": 0}
    got, got_stats = pipeline.process_frame(
        im, scale=2, palette=pal, threshold=128, fringe_passes=1, max_colours=4, **kw
    )
    assert np.array_equal(got["04_clean"], want) and got_stats == stats


@pytest.mark.parametrize(
    "edit, msg",
    [
        (lambda p: p.update(version="processing-v2"), "processing.version: 'processing-v2' is not one of"),
        (lambda p: p.update(downscaler="bilinear"), "processing.downscaler: 'bilinear' is not one of"),
        (lambda p: p.update(thickness=9), "processing.thickness: 9 is outside 0..6"),
        (lambda p: p.update(key_tol=-1), "processing.key_tol: -1 is outside 0.."),
        (lambda p: p.pop("despill_px"), "processing.despill_px: missing"),
        (lambda p: p.update(extra=1), "processing.extra: unknown key"),
    ],
)
def test_processing_section_schema(tmp_path, edit, msg):
    prof = toy_profile(tmp_path, lambda d: edit(d["processing"]))
    assert any(msg in m for m in check_profile(prof, check_paths=False)), check_profile(prof, check_paths=False)


def test_processing_despill_needs_the_magenta_key(tmp_path):
    prof = toy_profile(tmp_path, lambda d: d["alpha"].update(bg_key="#00ff00"))
    assert any("despill is magenta-only" in m for m in check_profile(prof, check_paths=False))
    assert load_profile(REPO / "profiles" / "twos-are-up.yaml").processing.thickness == 0


def test_godot_check_without_godot_is_an_error(tmp_path, toy, capsys, monkeypatch):
    raw, prof = toy
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    assert process(raw, prof, tmp_path / "job", "--godot-check") == 2
    assert "godot not found" in capsys.readouterr().err and not (tmp_path / "job").exists()


@pytest.mark.skipif(shutil.which("godot") is None, reason="godot is not on PATH (CI has none)")
def test_godot_check_imports_and_plays_the_export(tmp_path, toy, capsys):
    raw, prof = toy
    assert process(raw, prof, tmp_path / "job", "--godot-check", "--stills", str(stills_dir(tmp_path))) == 0
    assert "import + SpriteFrames check passed (8 animations)" in capsys.readouterr().out
    gc = json.loads((tmp_path / "job" / "manifest.json").read_text())["godot_check"]
    assert gc["pass"] and gc["import_exit"] == 0 and gc["check_exit"] == 0 and gc["result"]["animations_checked"] == 8


# --- real Phase 1 sets (private; skipped when absent) ----------------------------------------------------------------

WORK = Path(os.environ.get("SONORANLAB_WORK", "~/sonoranlab-work")).expanduser()
E = WORK / "benchmark" / "results" / "2026-10-18" / "E"
REAL = REPO / "profiles" / "twos-are-up.yaml"


def real_set(name: str) -> Path:
    raw = E / name / "set" / "raw"
    if not raw.is_dir():
        pytest.skip(f"{raw} not present (private Phase 1 data)")
    if not load_profile(REAL).palette.source.is_file():
        pytest.skip("the game palette is not present (private)")
    return raw


def test_real_relay_sentinel_passes_with_processing_v1_parity(tmp_path):
    raw = real_set("relay_sentinel/2")
    stills = WORK / "benchmark" / "characters" / "2026-10-18" / "relay_sentinel"
    before = tree_hashes(raw)
    job = tmp_path / "job"
    argv = ["--stills", str(stills)] if stills.is_dir() else []
    assert process(raw, REAL, job, *argv, character="relay_sentinel", anim="idle") == 0
    phase1 = raw.parent / "processed" / "processed"
    ours = job / "processed" / "05_registered"
    assert sorted(p.name for p in ours.iterdir()) == sorted(p.name for p in phase1.iterdir())
    for p in phase1.iterdir():
        assert (ours / p.name).read_bytes() == p.read_bytes(), p.name
    assert tree_hashes(raw) == before and verify_raw(job) == []
    assert (job / "export" / "relay_sentinel" / "relay_sentinel_frames.tres").is_file()


def test_real_samurai_fails_without_export(tmp_path):
    raw = real_set("samurai/1")
    job = tmp_path / "job"
    assert process(raw, REAL, job, character="samurai", anim="run") == 1
    rules = {f.split(":")[0] for f in json.loads((job / "report.json").read_text())["failures"]}
    assert rules and rules <= {"bbox_drift", "foot_slide"} and not (job / "export").exists()
