"""`sonoranlab animate` on the toy character: real CPU processing, a fake generation runner (no ComfyUI, no GPU)."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from test_pipeline import CELL, GEN, ORDER, SCALE, figure, stills_dir, toy_profile

from sonoranlab import animate as animate_mod
from sonoranlab import pipeline, sweep
from sonoranlab.animate import animate, check_still_column
from sonoranlab.artifacts import sha256_file
from sonoranlab.cli import main
from sonoranlab.comfy.adapters.base import GenerateRequest, RawResult

STAGES = ("prepare", "generate", "select_and_process", "validate_export", "total")


class Fake:
    """Stands in for the run-window guard: reads the request JSON and writes the attempt dir Track E would."""

    def __init__(self, *, fail=(), refuse=None, lift=None):
        self.fail, self.refuse, self.lift, self.calls, self.argv = set(fail), refuse, lift or {}, [], []

    def __call__(self, argv, *, cwd, check):
        req = GenerateRequest.from_json(json.loads(Path(argv[argv.index("--request") + 1]).read_text()))
        self.calls.append((req.direction, req.seed))
        self.argv.append(argv)
        if self.refuse and len(self.calls) == self.refuse[0]:
            return subprocess.CompletedProcess(argv, self.refuse[1])
        if (req.direction, req.seed) in self.fail:
            req.out_dir.mkdir(parents=True)
            RawResult(req, "failed", error={"code": "boom"}).write()
            return subprocess.CompletedProcess(argv, 1)
        clip = req.out_dir / "clip_rgba"
        clip.mkdir(parents=True)
        paths = []
        for i in range(12):
            paths.append(clip / f"rgba_{i:02}.png")
            f = figure(req.direction, i % 2, self.lift.get(req.direction, 0) if i % 2 else 0)
            Image.fromarray(f.repeat(SCALE, 0).repeat(SCALE, 1)).save(paths[-1])
        RawResult(req, "ok", frames=paths, timing={"wall_s": 7.5}).write()
        return subprocess.CompletedProcess(argv, 0)


def run(tmp_path, runner, **kw):
    args = dict(
        profile=toy_profile(tmp_path),
        character="hero",
        anim="idle",
        reference_dir=tmp_path / "stills" if (tmp_path / "stills").exists() else stills_dir(tmp_path),
        out=tmp_path / "out",
        repo=tmp_path,
        runner=runner,
    )
    return animate(**(args | kw))


def test_pass_exports_and_records_everything(tmp_path):
    fake = Fake()
    res = run(tmp_path, fake, exec_container="offline", seeds=(1, 2))
    out = tmp_path / "out"
    assert res["exit"] == 0 and res["outcome"] == "pass"
    assert json.loads((out / "animate.json").read_text()) == json.loads(json.dumps(res))
    assert set(res["timing"]) == set(STAGES) and all(isinstance(v, float) for v in res["timing"].values())
    assert res["counts"] == {
        "planned": 10,
        "attempts": 10,
        "ok": 10,
        "failed": 0,
        "not_run": 0,
        "first_pass_ok_rate": 1.0,
    }
    assert len(fake.calls) == 10 and all(a[-2:] == ["--exec", "offline"] for a in fake.argv)
    assert {r["gen_seconds"] for r in res["generation"]["requests"]} == {7.5}
    assert [c["direction"] for c in res["sweep"]["chosen"]] == GEN and res["sweep"]["technical_pass"]
    assert res["verdict"]["pass"] and res["production_usable"] is False
    assert res["human"]["cleanup_minutes"] is None and res["human"]["review"] is None
    inp = res["inputs"]
    assert inp["track"] == "E" and inp["seeds"] == [1, 2] and len(inp["reference"]) == 8 and inp["profile"]["sha256"]
    assert inp["reference"] == {p.name: sha256_file(p) for p in (tmp_path / "stills").glob("*.png")}
    assert res["stills"] == {"directions": ORDER, "exported_column0": True}
    sheet = Path(res["export"]["png"])
    assert sheet.is_file() and sheet.is_relative_to(out / "sweep/final/job/export")
    with Image.open(sheet) as im:
        assert im.mode == "P"
        pixels = np.asarray(im.convert("RGBA"))
    poses = []
    for r, d in enumerate(ORDER):
        with Image.open(tmp_path / "stills" / f"still_{d}.png") as im:
            poses.append(np.asarray(im.convert("RGBA")))
        assert np.array_equal(pixels[r * CELL : (r + 1) * CELL, :CELL], poses[-1]), d
        assert (out / "reference" / f"still_{d}.png").read_bytes() == (
            tmp_path / "stills" / f"still_{d}.png"
        ).read_bytes()
    assert len({p.tobytes() for p in poses}) == len(ORDER)
    assert not np.array_equal(poses[ORDER.index("west")], poses[ORDER.index("east")][:, ::-1])
    assert (out / "reference/still_east.png").is_file() and (out / "carrier/east_0.png").is_file()
    assert list((tmp_path / "game").rglob("*")) == []  # the shipped game is never written


def test_fail_has_reasons_and_no_export(tmp_path):
    res = run(tmp_path, Fake(lift={"north": 3}))
    assert res["exit"] == 1 and res["outcome"] == "validator_fail"
    assert res["export"] is None and res["verdict"]["failures"] and not res["sweep"]["technical_pass"]
    assert not (tmp_path / "out/sweep/final/job/export").exists()
    assert list((tmp_path / "game").rglob("*")) == []


def test_failed_generation_is_kept_and_others_continue(tmp_path):
    fake = Fake(fail=[("east", 1)])
    res = run(tmp_path, fake, seeds=(1, 2))
    assert len(fake.calls) == 10 and res["exit"] == 0
    c = res["counts"]
    assert (c["attempts"], c["ok"], c["failed"], c["first_pass_ok_rate"]) == (10, 9, 1, 0.9)
    bad = [r for r in res["generation"]["requests"] if r["status"] == "failed"]
    assert [(r["direction"], r["seed"], r["exit"], r["error"]) for r in bad] == [("east", 1, 1, {"code": "boom"})]
    assert [c["seed"] for c in res["sweep"]["chosen"] if c["direction"] == "east"] == [2]
    assert (tmp_path / "out/gen/attempts/east-1/result.json").is_file()  # the failure is retained


def test_direction_without_any_ok_attempt_is_no_candidates(tmp_path):
    res = run(tmp_path, Fake(fail=[("east", 1)]))
    assert res["exit"] == 1 and res["outcome"] == "no_candidates" and "east" in res["error"]
    assert not (tmp_path / "out/sweep").exists() and (tmp_path / "out/animate.json").is_file()


@pytest.mark.parametrize("code", [3, 4])
def test_guard_refusal_stops_as_blocked(tmp_path, code):
    fake = Fake(refuse=(3, code))
    res = run(tmp_path, fake)
    assert res["exit"] == 3 and res["outcome"] == "blocked" and len(fake.calls) == 3
    assert res["generation"]["stop_exit"] == code
    assert (res["counts"]["attempts"], res["counts"]["not_run"], res["counts"]["ok"]) == (3, 2, 2)
    assert not (tmp_path / "out/sweep").exists() and res["export"] is None


def test_usage_errors_write_nothing(tmp_path):
    prof, stills, game = toy_profile(tmp_path), stills_dir(tmp_path), tmp_path / "game"
    out = tmp_path / "out"
    fake = Fake()

    def bad(**kw):
        res = animate(
            **dict(
                profile=prof, character="hero", anim="idle", reference_dir=stills, out=out, repo=tmp_path, runner=fake
            )
            | kw
        )
        assert res["exit"] == 2 and res["outcome"] == "usage" and not fake.calls
        return res["error"]

    assert "Track E" in bad(track="C")
    assert "missing still" in bad(reference_dir=_without(stills, tmp_path / "s1", "still_east.png"))
    assert "expected 24x24" in bad(reference_dir=_resized(stills, tmp_path / "s2"))
    assert "not a clip" in bad(anim="missing")
    bad(character="nobody")  # no row in a per_character canvas: a profile error, not a crash
    assert "seeds" in bad(seeds=(1, 1))
    assert "inside the shipped game" in bad(out=game / "sub")
    assert not out.exists() and list(game.rglob("*")) == []
    out.mkdir()
    (out / "keep").write_text("x")
    assert "exists" in bad()
    assert [p.name for p in out.iterdir()] == ["keep"]


@pytest.mark.parametrize("need", [39.9, 0, float("nan"), float("inf"), True])
def test_under_budget_preflight_writes_nothing(tmp_path, need):
    fake = Fake()
    res = run(tmp_path, fake, need_gib=need)
    assert res["exit"] == 2 and res["outcome"] == "usage"
    assert "Track E needs 40 GiB" in res["error"]
    assert not (tmp_path / "out").exists() and not fake.calls


def test_minimum_budget_proceeds_to_generation(tmp_path):
    fake = Fake(refuse=(1, 3))
    assert run(tmp_path, fake, need_gib=40)["outcome"] == "blocked"
    assert len(fake.calls) == 1 and (tmp_path / "out").exists()


def test_cli_under_budget_preflight_writes_nothing(tmp_path, capsys):
    assert (
        main(
            [
                "animate",
                "--profile",
                str(toy_profile(tmp_path)),
                "--character",
                "hero",
                "--anim",
                "idle",
                "--reference",
                str(stills_dir(tmp_path)),
                "--out",
                str(tmp_path / "out"),
                "--need-gib",
                "10",
            ]
        )
        == 2
    )
    assert "Track E needs 40 GiB" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def _without(src: Path, dst: Path, name: str) -> Path:
    dst.mkdir()
    for p in src.glob("*.png"):
        if p.name != name:
            (dst / p.name).write_bytes(p.read_bytes())
    return dst


def _resized(src: Path, dst: Path) -> Path:
    dst.mkdir()
    for p in src.glob("*.png"):
        Image.open(p).resize((CELL * 2, CELL * 2)).save(dst / p.name)
    return dst


@pytest.mark.parametrize("problem", ["missing", "size", "alpha", "unreadable"])
def test_bad_west_still_is_usage_error_before_writing(tmp_path, problem):
    prof, stills = toy_profile(tmp_path), stills_dir(tmp_path)
    path = stills / "still_west.png"
    if problem == "missing":
        path.unlink()
    elif problem == "size":
        Image.new("RGBA", (CELL + 1, CELL)).save(path)
    elif problem == "alpha":
        Image.new("RGB", (CELL, CELL)).save(path)
    else:
        path.write_bytes(b"not a PNG")
    fake = Fake()
    res = animate(
        profile=prof,
        character="hero",
        anim="idle",
        reference_dir=stills,
        out=tmp_path / "out",
        repo=tmp_path,
        runner=fake,
    )
    assert res["exit"] == 2 and res["outcome"] == "usage"
    assert "west" in res["error"] and "export needs the full still set" in res["error"]
    assert not (tmp_path / "out").exists() and not fake.calls


@pytest.mark.parametrize("channel", [0, 3])
def test_check_still_column_reports_mismatching_row(tmp_path, channel):
    stills = stills_dir(tmp_path)
    sheet = np.concatenate([np.asarray(Image.open(stills / f"still_{d}.png")) for d in ORDER])
    path = tmp_path / "sheet.png"
    # Hidden RGB need not survive palette export.
    sheet[0, 0, :3] = (1, 2, 3)
    Image.fromarray(sheet).save(path)
    assert check_still_column(path, stills, ORDER, (CELL, CELL)) == []
    sheet[ORDER.index("west") * CELL + 4, 10, channel] ^= 255
    Image.fromarray(sheet).save(path)
    problems = check_still_column(path, stills, ORDER, (CELL, CELL))
    assert len(problems) == 1 and "west" in problems[0] and "mismatch" in problems[0]


def test_incomplete_export_is_error_even_after_validator_pass(tmp_path, monkeypatch):
    real = sweep.run_sweep

    def corrupt(*args, **kwargs):
        result = real(*args, **kwargs)
        path = Path(result["final_verdict"]["export"]["png"])
        with Image.open(path) as im:
            sheet = np.asarray(im.convert("RGBA")).copy()
        sheet[ORDER.index("west") * CELL : (ORDER.index("west") + 1) * CELL, :CELL] = 0
        Image.fromarray(sheet).save(path)
        return result

    monkeypatch.setattr(sweep, "run_sweep", corrupt)
    res = run(tmp_path, Fake())
    assert res["verdict"]["pass"] and res["exit"] == 2 and res["outcome"] == "error"
    assert "west" in res["error"] and "still verification failed" in res["error"]
    assert res["export"] is None and res["stills"]["exported_column0"] is False
    assert json.loads((tmp_path / "out/animate.json").read_text())["outcome"] == "error"


def test_godot_check_reaches_only_the_final_run(tmp_path, monkeypatch):
    real, seen = pipeline.run, []

    def spy(*a, **k):
        seen.append((k.get("godot_check", False), k.get("stills")))
        return real(*a, **k | {"godot_check": False})  # no godot in CI

    monkeypatch.setattr(sweep.pipeline, "run", spy)
    monkeypatch.setattr(animate_mod.shutil, "which", lambda _: None)
    assert "godot not found" in run(tmp_path, Fake(), godot_check=True)["error"]
    monkeypatch.setattr(animate_mod.shutil, "which", lambda _: "/bin/godot")
    assert run(tmp_path, Fake(), godot_check=True)["exit"] == 0
    assert seen[-1] == (True, tmp_path / "out/reference")
    assert all(call == (False, None) for call in seen[:-1])


def test_cli_maps_args_and_exit(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"exit": 1, "outcome": "validator_fail", "error": None, "timing": dict.fromkeys(STAGES, 1.0),
                "counts": None, "verdict": {"failures": ["why"]}, "export": None}  # fmt: skip

    monkeypatch.setattr(animate_mod, "animate", fake)
    argv = ["animate", "--profile", "p.yaml", "--character", "hero", "--reference", "r", "--out", str(tmp_path / "o")]
    assert main([*argv, "--seeds", "1,3", "--exec", "c", "--windows", "steady", "--need-gib", "30"]) == 1
    assert seen["seeds"] == (1, 3) and seen["exec_container"] == "c" and seen["windows"] == "steady"
    assert seen["track"] == "E" and seen["anim"] == "run" and seen["repo"] == Path(sweep.__file__).parents[1]
    assert "why" in capsys.readouterr().err
    monkeypatch.undo()
    assert main([*argv, "--track", "C"]) == 2
    assert "Track E" in capsys.readouterr().err


def test_non_loop_fall_clip_end_to_end(tmp_path):
    """A gun-style death (loop false, validator_mode fall, prompt_motion): profile motion in every request, select_once
    picks for the carrier and the sweep, the fall validator on the final job, a clip table without a hold field."""
    death = {"frames": 3, "fps": 8, "loop": False, "hold_last": True, "track_default": "E"}
    death |= {"validator_mode": "fall", "prompt_motion": "is hit and collapses, ending lying still"}
    (tmp_path / "p").mkdir()  # run() rewrites tmp_path/toy.yaml
    prof = toy_profile(tmp_path / "p", lambda d: d["animations"].update(death=death))
    fake = Fake()
    res = run(tmp_path, fake, profile=prof, anim="death")
    assert res["exit"] == 0 and res["outcome"] == "pass", res
    out = tmp_path / "out"
    prompts = {json.loads(p.read_text())["prompt"] for p in (out / "gen/requests").glob("*.json")}
    assert prompts == {
        f"This character is hit and collapses, ending lying still, facing {d}. Static camera, flat solid #808080 "
        "background, 2D game sprite animation."
        for d in GEN
    }
    carrier = json.loads((out / "carrier-src/east/loop.json").read_text())
    assert carrier["window"] == "settle" and carrier["picks"][0] == 0 and carrier["picks"][-1] == 11
    report = json.loads(Path(res["verdict"]["report"]).read_text())
    assert report["validator_mode"] == "fall" and all("fall" in report["directions"][d] for d in GEN)
    side = json.loads(next((out / "sweep/final/job/export").rglob("*.sonoranlab.json")).read_text())
    assert side["characters_json_entry"]["clips"] == [
        {"name": "death", "start": 1, "frames": 3, "fps": 8, "loop": False}
    ]
