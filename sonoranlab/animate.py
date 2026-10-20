"""`sonoranlab animate`: reference stills -> Track E generation -> loop/seed sweep -> validated Godot export.

Only composes sweep.py (plan/run generation, rank, assemble) and pipeline.py (via run_sweep); no new pixel math.
Stages are timed separately into animate.json. Exports land only in the final sweep job's `export/` on a validator
PASS and never under the profile's export.godot_project. Exit: 0 technical PASS, 1 FAIL / no candidates / all
generations failed, 2 usage or processing error, 3 run-window guard refused (3/4). `production_usable` is always false:
a human review (`sonoranlab review record`) is a separate, timed step.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from PIL import Image

from sonoranlab import sweep
from sonoranlab.artifacts import sha256_file, write_json
from sonoranlab.loop import write_loop
from sonoranlab.pipeline import PipelineError
from sonoranlab.profile import ProfileError, load_profile

SCHEMA = "sonoranlab.animate/1"
STAGES = ("prepare", "generate", "select_and_process", "validate_export")
HUMAN = {
    "cleanup_minutes": None,
    "review": None,
    "instruction": "time cleanup with a stopwatch; record with `sonoranlab review record`",
}


class UsageError(Exception):
    """Bad input, found before anything is written (exit 2)."""


def _rev(repo: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _preflight(profile, character, anim, track, seeds, windows, need_gib, godot_check, reference_dir, out, repo):
    """Everything pipeline.run / run_sweep / the stills would reject, checked before `out` exists."""
    if track != "E":
        raise UsageError(
            f"track {track!r}: only Track E is supported (Track C posed first frames are a later option)"
            if track == "C"
            else f"track {track!r}: expected E"
        )
    if windows not in ("default", "steady"):
        raise UsageError("windows must be default|steady")
    if not isinstance(need_gib, (int, float)) or not math.isfinite(need_gib) or need_gib <= 0:
        raise UsageError("need_gib must be a positive number")
    if (
        not seeds
        or len({*seeds}) != len(seeds)
        or any(isinstance(s, bool) or not isinstance(s, int) or s < 0 for s in seeds)
    ):
        raise UsageError("seeds must be distinct non-negative integers")
    if not repo.is_dir():
        raise UsageError(f"repo {repo}: not a directory")
    try:
        prof = load_profile(profile)
        spec = prof.character(character)
    except ProfileError as e:
        raise UsageError("; ".join(e.problems)) from None
    if anim not in spec.animations:
        raise UsageError(f"anim {anim!r} is not a clip of {character} ({', '.join(spec.animations)})")
    if prof.processing is None or spec.anchor_mode == "rig" or spec.cell[0] != spec.cell[1]:
        raise UsageError("profile needs a `processing:` section, a square cell and a non-rig anchor_mode")
    if godot_check and shutil.which("godot") is None:
        raise UsageError("--godot-check: godot not found on PATH")
    if out.exists():
        raise UsageError(f"out {out} exists: reruns go to a new directory; nothing is overwritten")
    if out.resolve().is_relative_to(prof.export.godot_project.resolve()):
        raise UsageError(f"out {out} is inside the shipped game {prof.export.godot_project}")
    if not reference_dir.is_dir():
        raise UsageError(f"reference {reference_dir}: not a directory")
    stills = {}
    for d in prof.directions.generate:
        p = reference_dir / f"still_{d}.png"
        if not p.is_file():
            raise UsageError(f"{p}: missing still for generated direction {d}")
        try:
            with Image.open(p) as im:
                mode, size = im.mode, im.size
        except OSError as e:
            raise UsageError(f"{p}: unreadable image ({e})") from None
        if mode != "RGBA" or size != spec.cell:
            raise UsageError(
                f"{p}: is {mode} {size[0]}x{size[1]}, expected RGBA {spec.cell[0]}x{spec.cell[1]} (the cell)"
            )
        stills[d] = p
    return prof, spec, stills


def _attempt_records(gen: dict, planned: Sequence[Path]) -> list[dict]:
    ran = {str(Path(r["request"]).resolve()): r for r in gen["requests"]}
    records = []
    for path in planned:
        req = json.loads(Path(path).read_text())
        run = ran.get(str(Path(path).resolve()))
        rec = {
            "direction": req["direction"],
            "seed": req["seed"],
            "request": str(path),
            "attempt_dir": req["out_dir"],
            "argv": run["argv"] if run else None,
            "exit": run["exit"] if run else None,
            "status": "not_run",
            "gen_seconds": None,
            "error": run.get("error") if run else None,
        }
        if run:
            rec["status"], rec["error"] = (
                "failed",
                rec["error"] or (None if run["exit"] == 0 else f"exit {run['exit']}"),
            )
            try:
                res = json.loads((Path(req["out_dir"]) / "result.json").read_text())
                timing = res.get("timing") or {}
                rec["gen_seconds"] = timing.get("wall_s", timing.get("wall"))
                rec["error"] = res.get("error") or rec["error"]
                if res.get("status") == "ok" and run["exit"] == 0:
                    rec["status"] = "ok"
            except (OSError, ValueError):
                rec["error"] = rec["error"] or "no readable result.json"
        records.append(rec)
    return records


def _carrier(ok: dict[str, Path], *, n_frames: int, cell: int, out: Path) -> Path:
    """A flat dir of `<direction>_<k>.png`: each direction's first ok attempt, default loop pick, own integer scale."""
    carrier = out / "carrier"
    carrier.mkdir()
    for d, attempt in ok.items():
        with Image.open(sweep._clip_frames(attempt)[0]) as im:  # same scale rule as run_sweep
            scale = im.width // cell
        if scale < 1:
            raise ValueError(f"{attempt}: clip frames are smaller than the {cell} px cell")
        pick = sweep.loop_candidates(attempt, n_frames=n_frames, size=cell * scale)[0].loop
        for p in write_loop(pick, sweep._clip_frames(attempt), out / "carrier-src" / d, d):
            shutil.copyfile(p, carrier / p.name)
    return carrier


def _summary(doc: dict) -> dict:
    chosen = [
        {
            "direction": c["direction"],
            "seed": c["seed"],
            "mode": c["mode"],
            "attempt_dir": c["attempt_dir"],
            "loop_start": c["loop"]["start"],
            "period": c["loop"]["period"],
            "score": {
                k: c["score"][k]
                for k in (
                    "direction_pass",
                    "n_failures",
                    "n_core_failures",
                    "worst_ratio",
                    "accessory_cv",
                    "seam_score",
                )
            },
        }
        for c in doc["candidates"]
        if c["chosen"]
    ]
    return {
        "chosen": chosen,
        "n_candidates": len(doc["candidates"]),
        "qualified": doc["qualified"],
        "technical_pass": doc["technical_pass"],
        "best_blocking_reasons": doc["best_blocking_reasons"],
    }


def animate(
    *,
    profile: Path,
    character: str,
    reference_dir: Path,
    anim: str = "run",
    track: str = "E",
    seeds: Sequence[int] = (1,),
    out: Path,
    exec_container: str | None = None,
    need_gib: float = 40,
    windows: str = "default",
    godot_check: bool = False,
    repo: Path,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = datetime.now,
) -> dict:
    """Run the whole loop and write `out/animate.json`; returns that document (plus `exit`).

    Usage errors return {"exit": 2, "outcome": "usage", "error": ...} with nothing written. outcome is one of
    pass | validator_fail | blocked | no_candidates | error (processing/IO error after `out` was created)."""
    t0 = clock()
    profile, reference_dir, out, repo = Path(profile), Path(reference_dir), Path(out), Path(repo)
    seeds = list(seeds)
    try:
        prof, spec, stills = _preflight(
            profile, character, anim, track, seeds, windows, need_gib, godot_check, reference_dir, out, repo
        )
    except UsageError as e:
        return {"exit": 2, "outcome": "usage", "error": str(e)}
    started = wall().astimezone().isoformat(timespec="seconds")
    out.mkdir(parents=True)
    ref = out / "reference"
    ref.mkdir()
    for p in stills.values():
        shutil.copyfile(p, ref / p.name)
    gen_dirs = list(prof.directions.generate)
    timing: dict[str, float | None] = dict.fromkeys(STAGES)
    timing["prepare"] = clock() - t0
    doc: dict = {
        "schema": SCHEMA,
        "outcome": "error",
        "exit": 2,
        "error": None,
        "inputs": {
            "profile": {"path": str(profile.resolve()), "sha256": sha256_file(profile)},
            "character": character,
            "anim": anim,
            "track": track,
            "seeds": seeds,
            "directions": gen_dirs,
            "reference": {p.name: sha256_file(p) for p in sorted(ref.glob("*.png"))},
            "exec_container": exec_container,
            "need_gib": need_gib,
            "windows": windows,
            "godot_check": godot_check,
            "repo_rev": _rev(repo),
        },
        "started_at": started,
        "generation": None,
        "counts": None,
        "sweep": None,
        "verdict": None,
        "export": None,
        "godot_check": None,
        "production_usable": False,
        "human": dict(HUMAN),
    }
    try:
        t = clock()
        planned = sweep.plan_generation(character, anim, gen_dirs, seeds, ref, out / "gen", background="#808080")
        gen = sweep.run_generation(planned, need_gib=need_gib, repo=repo, exec_container=exec_container, runner=runner)
        records = _attempt_records(gen, planned)
        ok = [r for r in records if r["status"] == "ok"]
        ran = [r for r in records if r["status"] != "not_run"]
        doc["generation"] = {"requests": records, "stop_exit": gen["stop_exit"]}
        doc["counts"] = {
            "planned": len(records),
            "attempts": len(ran),
            "ok": len(ok),
            "failed": len(ran) - len(ok),
            "not_run": len(records) - len(ran),
            "first_pass_ok_rate": len(ok) / len(ran) if ran else None,
        }
        timing["generate"] = clock() - t
        if gen["stop_exit"] is not None:
            doc.update(outcome="blocked", exit=3, error=f"run-window guard refused (exit {gen['stop_exit']})")
            return _finish(doc, out, timing, t0, clock, wall)
        first_ok: dict[str, Path] = {}
        for r in ok:
            first_ok.setdefault(r["direction"], Path(r["attempt_dir"]))
        missing = [d for d in gen_dirs if d not in first_ok]
        if missing:
            doc.update(outcome="no_candidates", exit=1, error=f"no successful generation for: {', '.join(missing)}")
            return _finish(doc, out, timing, t0, clock, wall)

        t = clock()
        n_frames = spec.animations[anim].frames
        carrier = _carrier(first_ok, n_frames=n_frames, cell=spec.cell[0], out=out)
        result = sweep.run_sweep(
            [Path(r["attempt_dir"]) for r in ok],
            profile=profile,
            character=character,
            anim=anim,
            carrier=carrier,
            out=out / "sweep",
            windows=windows,
            godot_check=godot_check,
        )
        verdict = result["final_verdict"]
        wall_s = clock() - t
        try:  # run_sweep ends with the final pipeline.run; its own total is the validate/export share
            val_s = json.loads((Path(verdict["job"]) / "manifest.json").read_text())["timings"]["total_s"]
        except (OSError, ValueError, KeyError):
            val_s = 0.0
        timing["validate_export"] = min(val_s, wall_s)
        timing["select_and_process"] = wall_s - timing["validate_export"]
        passed = verdict["pass"] is True
        doc["sweep"] = _summary(result) | {"path": str(out / "sweep" / "sweep.json")}
        doc["verdict"] = {k: verdict[k] for k in ("pass", "failures", "warnings", "job", "report", "contact")}
        doc["export"] = verdict["export"] if passed else None  # pipeline exports only on PASS; keep it that way
        doc["godot_check"] = verdict["godot_check"] if passed else None
        doc.update(outcome="pass" if passed else "validator_fail", exit=0 if passed else 1)
    except (OSError, ValueError, KeyError, TypeError, PipelineError) as e:
        doc.update(outcome="error", exit=2, error=f"{type(e).__name__}: {e}")
    return _finish(doc, out, timing, t0, clock, wall)


def _finish(
    doc: dict, out: Path, timing: dict, t0: float, clock: Callable[[], float], wall: Callable[[], datetime]
) -> dict:
    timing["total"] = clock() - t0
    doc["timing"] = {k: None if v is None else round(v, 3) for k, v in timing.items()}
    doc["ended_at"] = wall().astimezone().isoformat(timespec="seconds")
    write_json(out, "animate.json", doc)
    return doc
