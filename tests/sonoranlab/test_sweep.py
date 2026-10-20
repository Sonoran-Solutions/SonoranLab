"""Seed/window sweeps with tiny synthetic clips, real CPU processing and a fake generation runner."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from test_pipeline import CELL, GEN, SCALE, figure, raw_set, toy_profile, tree_hashes

from sonoranlab import sweep
from sonoranlab.cli import main
from sonoranlab.comfy.adapters.base import GenerateRequest, RawResult
from sonoranlab.loop import LoopPick


def attempt(root: Path, direction: str = "east", *, lift: int = 0, seed: int = 1, n: int = 12) -> Path:
    clip = root / "clip_rgba"
    clip.mkdir(parents=True)
    paths = []
    for i in range(n):
        paths.append(clip / f"rgba_{i:02}.png")
        f = figure(direction, i % 2, lift if i % 2 else 0).repeat(SCALE, 0).repeat(SCALE, 1)
        Image.fromarray(f).save(paths[-1])
    request = GenerateRequest("E", "hero", "idle", direction, paths[0], "toy prompt", seed, root)
    RawResult(
        request,
        "ok",
        frames=paths,
        timing={"wall_s": 12.5},
        params={"prompt": "patched prompt"},
        workflow_source="toy.json",
        workflow_source_sha256="abc",
        workflow_patched_sha256="def",
    ).write()
    return root


def candidate(root: Path, direction: str = "east", **kw) -> sweep.Candidate:
    return sweep.loop_candidates(attempt(root, direction, **kw), n_frames=2, size=CELL * SCALE)[0]


def bare_candidate() -> sweep.Candidate:
    return sweep.Candidate("east", Path("attempt"), 1, LoopPick(2, 6, [6, 7], 0, 0), 1, {})


KEYS = ["direction_pass", "n_core_failures", "n_failures", "worst_ratio", "accessory_cv", "seam_score", "gen_seconds"]


@pytest.mark.parametrize("index", range(len(KEYS)), ids=KEYS)
def test_rank_each_key_precedes_all_later_keys(index):
    good, bad = sweep.Score(bare_candidate()), sweep.Score(bare_candidate())
    setattr(bad, KEYS[index], False if index == 0 else 1)
    for later in KEYS[index + 1 :]:
        setattr(good, later, 100)
    assert sweep.rank([bad, good]) == [good, bad]


def test_rank_stable_and_thin_count_is_not_a_key():
    a = sweep.Score(bare_candidate(), n_thin_failures=100)
    b = replace(a, n_thin_failures=0)
    assert sweep.rank([a, b])[0] is a
    assert sweep.rank([b, a])[0] is b


def test_default_and_steady_windows(tmp_path):
    root = attempt(tmp_path / "attempt")
    default = sweep.loop_candidates(root, n_frames=2, size=48)
    steady = sweep.loop_candidates(root, n_frames=2, size=48, windows="steady")
    assert len(default) == 1 and default[0].loop.period == 2
    assert len(steady) == 12 - 6 - 2
    assert steady[0] == default[0] and steady[0].mode == "default"
    assert all(c.mode == "steady" for c in steady[1:])
    assert {c.loop.start for c in steady} == set(range(6, 10))
    assert all(c.loop.start + c.loop.period < 12 for c in steady)
    assert default[0].source == {
        "kind": "clip",
        "workflow_source": "toy.json",
        "workflow_source_sha256": "abc",
        "workflow_patched_sha256": "def",
        "prompt": "patched prompt",
        "background": "#808080",
    }
    assert default[0].gen_seconds == 12.5
    with pytest.raises(ValueError):
        sweep.loop_candidates(root, n_frames=2, size=48, windows="all")
    with pytest.raises(ValueError, match="too short"):
        sweep.loop_candidates(root, n_frames=20, size=48)


def test_clip_input_rejects_failed_attempt_and_external_frames(tmp_path):
    root = attempt(tmp_path / "attempt")
    path = root / "result.json"
    doc = json.loads(path.read_text())
    doc["status"] = "failed"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="did not succeed"):
        sweep.loop_candidates(root, n_frames=2, size=48)
    doc.update(status="ok", frames=[str(tmp_path / "outside.png")])
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="inside"):
        sweep.loop_candidates(root, n_frames=2, size=48)


def test_accessory_cv_flicker_and_alpha_threshold(tmp_path):
    paths = []
    for i, alpha in enumerate([255, 255, 0, 127, 128]):
        f = np.zeros((32, 32, 4), np.uint8)
        f[10:26, 10:22] = (200, 80, 30, 255)
        f[8:20, 22:23] = (200, 80, 30, alpha)
        path = tmp_path / f"{i}.png"
        Image.fromarray(f).save(path)
        paths.append(path)
    assert sweep.accessory_cv(paths[:2]) == 0
    assert sweep.accessory_cv(paths[:3]) > 0.5
    assert sweep.accessory_cv([paths[2], paths[3]]) == 0
    assert sweep.accessory_cv([paths[0], paths[4]]) == 0


def test_direction_slicing_classifies_core_thin_and_ignores_ungated(tmp_path, monkeypatch):
    c = candidate(tmp_path / "attempt")
    prof, raw = toy_profile(tmp_path), raw_set(tmp_path)
    diagnostics = [
        {"direction": d, "rule": rule, "frame": i, "value": 8, "limit": 2, "gated": gated, "core_delta": core}
        for i, (d, rule, core, gated) in enumerate(
            [
                ("east", "bbox_drift", 1, True),
                ("east", "bbox_drift", 8, True),
                ("east", "foot_slide", 0, True),
                ("north", "bbox_drift", 8, True),
                ("east", "bbox_drift", 8, False),
            ]
        )
    ]
    failures = [f"{d['rule']}: frame {i} ({d['direction']}/{i})" for i, d in enumerate(diagnostics[:4])]
    failures += ["frame_count: north has 1 frames, expected 2", "foot_line: east clip lowest row wrong"]
    report = {
        "pass": False,
        "failures": failures,
        "diagnostics": diagnostics,
        "anchor_mode": "foot",
        "directions": {"east": {"bbox_drift_max": 8, "foot_slide_max": 3}, "north": {"bbox_drift_max": 100}},
        "limits": {"bbox_drift_gated": True, "max_bbox_drift_px": 2, "max_foot_slide_px": 3},
    }
    jobs = []

    def run(raw, *, out, **kwargs):
        assert not out.exists()
        out.mkdir()
        path = out / "report.json"
        path.write_text(json.dumps(report))
        jobs.append(out)
        return {"report": str(path)}

    monkeypatch.setattr(sweep.pipeline, "run", run)
    scores = sweep.score_direction(
        [c, c], profile=prof, character="hero", anim="idle", carrier=dict.fromkeys(GEN, raw), workdir=tmp_path / "work"
    )
    s = scores[0]
    assert not s.direction_pass and s.n_failures == 4
    assert s.n_core_failures == 2 and s.n_thin_failures == 1 and s.worst_ratio == 4
    assert s.diagnostics == [d for d in diagnostics if d["direction"] == "east"]
    assert s.metrics == report["directions"]["east"]
    assert s.seam_score == c.loop.loop_seam_score and s.gen_seconds == 12.5
    assert len(set(jobs)) == 2 and all(p.is_dir() for p in jobs)
    report["failures"] = ["bbox_drift: frame 0 (north/0)"]
    report["limits"]["bbox_drift_gated"] = False
    s = sweep.score_direction(
        [c], profile=prof, character="hero", anim="idle", carrier=dict.fromkeys(GEN, raw), workdir=tmp_path / "work"
    )[0]
    assert s.direction_pass and s.n_failures == 0 and s.worst_ratio == 1


def test_assemble_pass_fail_and_candidate_slice_is_not_set_verdict(tmp_path):
    prof = toy_profile(tmp_path)
    raw = raw_set(tmp_path, lift={("north", 1): 3})
    choice = {d: candidate(tmp_path / d, d) for d in GEN}
    east = sweep.score_direction(
        [choice["east"]],
        profile=prof,
        character="hero",
        anim="idle",
        carrier=dict.fromkeys(GEN, raw),
        workdir=tmp_path / "scores",
    )[0]
    assert east.direction_pass
    assert not json.loads((east.job_dir / "report.json").read_text())["pass"]
    before = tree_hashes(tmp_path / "east")
    good = sweep.assemble_and_validate(choice, profile=prof, character="hero", anim="idle", out=tmp_path / "good")
    assert good["pass"] and good["exit"] == 0
    assert tree_hashes(tmp_path / "east") == before
    selected = tmp_path / "good/selected/east/east_0.png"
    copied = tmp_path / "good/raw-set/east_0.png"
    assert selected.read_bytes() == copied.read_bytes() and selected.stat().st_ino != copied.stat().st_ino
    choice["north"] = candidate(tmp_path / "bad-north", "north", lift=3)
    bad = sweep.assemble_and_validate(choice, profile=prof, character="hero", anim="idle", out=tmp_path / "bad")
    assert not bad["pass"] and bad["exit"] == 1
    assert any("(north/" in f for f in bad["failures"])
    assert all("(east/" not in f for f in bad["failures"])
    with pytest.raises(FileExistsError):
        sweep.assemble_and_validate(choice, profile=prof, character="hero", anim="idle", out=tmp_path / "bad")


def test_sweep_json_keeps_rejections_and_none_qualifies(tmp_path):
    a, b = bare_candidate(), replace(bare_candidate(), seed=2)
    fail = sweep.Score(a, direction_pass=False, n_failures=1, failures=["bbox_drift: frame 0 (east/0)"])
    passing = sweep.Score(b)
    verdict = {"pass": False, "failures": ["full set failed"]}
    path = sweep.write_sweep([fail, passing], directions=["east"], final_verdict=verdict, out=tmp_path)
    doc = json.loads(path.read_text())
    assert doc["qualified"] and not doc["technical_pass"] and not doc["production_usable"]
    assert doc["final_verdict"] == verdict
    assert doc["candidates"][0]["rejected"] and not doc["candidates"][0]["chosen"]
    assert doc["candidates"][0]["reasons"] == ["not best rank", "direction failed: bbox_drift: frame 0 (east/0)"]
    assert doc["candidates"][1]["chosen"] and doc["candidates"][1]["reasons"] == ["best rank"]
    none = tmp_path / "none"
    none.mkdir()
    path = sweep.write_sweep([fail], directions=["east", "north"], final_verdict=verdict, out=none)
    doc = json.loads(path.read_text())
    assert not doc["qualified"] and not doc["technical_pass"]
    assert doc["best_blocking_reasons"] == {"east": fail.failures, "north": ["no candidates"]}
    assert doc["candidates"][0]["chosen"] and doc["candidates"][0]["reasons"][1].startswith("direction failed:")
    with pytest.raises(FileExistsError):
        sweep.write_sweep([fail], directions=["east"], final_verdict=verdict, out=none)


@pytest.mark.parametrize("lift,code", [(0, 0), (3, 1)])
def test_rank_cli_end_to_end(tmp_path, capsys, lift, code):
    prof, raw = toy_profile(tmp_path), raw_set(tmp_path)
    attempts = [attempt(tmp_path / d, d, lift=lift if d == "north" else 0) for d in GEN]
    attempts.append(attempt(tmp_path / "worse-east", "east", lift=3, seed=2))
    out = tmp_path / "sweep"
    argv = [
        "sweep",
        "rank",
        "--profile",
        str(prof),
        "--character",
        "hero",
        "--anim",
        "idle",
        "--carrier",
        str(raw),
        "--out",
        str(out),
        "--attempts",
        *map(str, attempts),
    ]
    assert main(argv) == code
    doc = json.loads((out / "sweep.json").read_text())
    assert doc["technical_pass"] is (code == 0) and doc["qualified"] is (code == 0)
    assert not doc["production_usable"] and len(doc["candidates"]) == 6
    assert len(list((out / "candidates").glob("*/job/report.json"))) == 6
    assert sum(c["chosen"] for c in doc["candidates"]) == 5
    if code:
        assert "bbox_drift" in capsys.readouterr().err
        assert "north" in doc["best_blocking_reasons"]
    assert main(argv) == 2  # no overwrite
    argv[argv.index("--anim") + 1] = "missing"
    assert main(argv) == 2


def test_plan_generation_and_cli(tmp_path):
    stills = tmp_path / "stills"
    stills.mkdir()
    for d in ("north", "east"):
        Image.fromarray(figure(d, 0)).save(stills / f"still_{d}.png")
    paths = sweep.plan_generation("hero", "run", ["north", "east"], [2, 1], stills, tmp_path / "plan")
    requests = [GenerateRequest.from_json(json.loads(p.read_text())) for p in paths]
    assert [(r.direction, r.seed) for r in requests] == [("north", 2), ("north", 1), ("east", 2), ("east", 1)]
    assert all(r.background == "#808080" and r.track == "E" and not r.out_dir.exists() for r in requests)
    assert all(r.prompt and r.source_image.is_file() for r in requests)
    assert (
        main(
            [
                "sweep",
                "plan",
                "--character",
                "hero",
                "--anim",
                "idle",
                "--directions",
                "east",
                "--seeds",
                "4",
                "--stills-dir",
                str(stills),
                "--out",
                str(tmp_path / "cli-plan"),
                "--background",
                "#123456",
            ]
        )
        == 0
    )
    custom = json.loads((tmp_path / "cli-plan/requests/east-4.json").read_text())
    assert custom["background"] == "#123456" and "idle" in custom["prompt"]
    for directions, seeds in [(["west"], [1]), (["east"], [-1]), (["east"], [True]), (["east"], [1, 1])]:
        with pytest.raises(ValueError):
            sweep.plan_generation("hero", "run", directions, seeds, stills, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("exits", [[0, 1, 2, 0], [1, 3, 0, 0], [0, 4, 0, 0]])
def test_generation_serial_guard_per_request_continues_and_stops(tmp_path, monkeypatch, exits):
    from sonoranlab.comfy.adapters import track_e

    paths = [tmp_path / f"{i}.json" for i in range(4)]
    for p in paths:
        p.write_text('{"track": "E"}')
    calls, guarded = [], []
    actual_guard = track_e.guarded_argv

    def guard(argv, need_gib):
        guarded.append(argv)
        return actual_guard(argv, need_gib)

    def runner(argv, *, cwd, check):
        assert len(guarded) == len(calls) + 1  # next request isn't guarded until this runner returns
        assert cwd == tmp_path and check is False and "--gtt-credit" not in argv
        calls.append(argv)
        return subprocess.CompletedProcess(argv, exits[len(calls) - 1])

    monkeypatch.setattr(track_e, "guarded_argv", guard)
    result = sweep.run_generation(paths, repo=tmp_path, exec_container="offline", runner=runner)
    n = 2 if exits[1] in (3, 4) else 4
    assert len(calls) == len(guarded) == n
    assert [r["exit"] for r in result["requests"]] == exits[:n]
    assert result["stop_exit"] == (exits[1] if n == 2 else None)
    assert result["remaining"] == [str(p) for p in paths[n:]]
    for i, argv in enumerate(guarded):
        assert argv == [
            sys.executable,
            "-m",
            "sonoranlab.cli",
            "generate",
            "--request",
            str(paths[i]),
            "--exec",
            "offline",
        ]
    assert all(argv[:4] == ["python3", str(track_e.REPO / "env/runwindow.py"), "--need-gib", "40"] for argv in calls)


def test_sweep_import_is_core_safe():
    code = "import sys, sonoranlab.sweep; assert not any(m.startswith('sonoranlab.comfy') for m in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("exit_code", [0, 1, 3, 4])
def test_generation_cli_reports_helper_exit(tmp_path, monkeypatch, capsys, exit_code):
    calls = []

    def run(paths, **kwargs):
        calls.append((paths, kwargs))
        return {
            "requests": [{"request": "request.json", "exit": exit_code}],
            "stop_exit": exit_code if exit_code in (3, 4) else None,
            "remaining": [],
        }

    monkeypatch.setattr(sweep, "run_generation", run)
    path = tmp_path / "request.json"
    assert main(["sweep", "run", "--requests", str(path), "--repo", str(tmp_path), "--exec", "offline"]) == exit_code
    assert calls == [([path], {"need_gib": None, "repo": tmp_path, "exec_container": "offline"})]
    assert json.loads(capsys.readouterr().out)["requests"][0]["exit"] == exit_code


def test_generation_spawn_error_is_recorded_and_next_request_runs(tmp_path):
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for p in paths:
        p.write_text('{"track": "E"}')
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert "--exec" not in argv
        if len(calls) == 1:
            raise OSError("cannot spawn guard")
        return subprocess.CompletedProcess(argv, 0)

    result = sweep.run_generation(paths, repo=tmp_path, runner=runner)
    assert [r["exit"] for r in result["requests"]] == [2, 0]
    assert result["requests"][0]["error"] == "cannot spawn guard" and result["stop_exit"] is None


def _requests(tmp_path, *tracks):
    paths = []
    for i, track in enumerate(tracks):
        paths.append(tmp_path / f"{i}.json")
        paths[-1].write_text(json.dumps({"track": track}))
    return paths


def _recording_runner(calls):
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    return runner


def test_generation_budget_defaults_to_each_requests_track_minimum(tmp_path):
    calls = []
    paths = _requests(tmp_path, "E", "C", "C")
    result = sweep.run_generation(paths, repo=tmp_path, runner=_recording_runner(calls))
    assert [a[a.index("--need-gib") + 1] for a in calls] == ["40", "60", "60"]
    assert [r["need_gib"] for r in result["requests"]] == [40, 60, 60]
    assert all("--gtt-credit" not in a for a in calls)


def test_generation_explicit_budget_below_track_minimum_is_refused_before_any_run(tmp_path):
    calls = []
    paths = _requests(tmp_path, "E", "C")  # the E request alone would be fine at 40; the C one is not
    with pytest.raises(ValueError, match="under-budget run window: Track C needs 60 GiB"):
        sweep.run_generation(paths, need_gib=40, repo=tmp_path, runner=_recording_runner(calls))
    assert calls == []
    result = sweep.run_generation(paths, need_gib=70, repo=tmp_path, runner=_recording_runner(calls))
    assert [r["need_gib"] for r in result["requests"]] == [70, 70]


@pytest.mark.parametrize("body", ['{"track": "X"}', "{}", "[]", '{"track": "e"}'])
def test_generation_unknown_track_is_refused_before_any_run(tmp_path, body):
    calls = []
    paths = _requests(tmp_path, "E")
    paths.append(tmp_path / "bad.json")
    paths[-1].write_text(body)
    with pytest.raises(ValueError, match="track"):
        sweep.run_generation(paths, repo=tmp_path, runner=_recording_runner(calls))
    assert calls == []


def test_generation_cli_refuses_track_c_at_40_gib(tmp_path, capsys):
    (path,) = _requests(tmp_path, "C")
    assert main(["sweep", "run", "--requests", str(path), "--repo", str(tmp_path), "--need-gib", "40"]) == 2
    assert "Track C needs 60 GiB" in capsys.readouterr().err
