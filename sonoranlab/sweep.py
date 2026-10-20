"""CPU candidate scoring; a candidate is one direction's clip AND selected loop window.

Generation imports stay inside the two generation helpers. Every processing job and rejected candidate is retained.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from importlib import import_module
from pathlib import Path

import numpy as np
from PIL import Image

from sonoranlab import pipeline
from sonoranlab.artifacts import write_json
from sonoranlab.loop import LoopPick, select_loop, write_loop
from sonoranlab.profile import HEX_RE, NAME_RE, load_profile
from sonoranlab.slice import read_frame_dir
from sonoranlab.validate import opening


@dataclass(frozen=True)
class Candidate:
    direction: str
    attempt_dir: Path
    seed: int
    loop: LoopPick
    gen_seconds: float
    source: dict
    mode: str = "default"


@dataclass
class Score:
    candidate: Candidate
    direction_pass: bool = True
    n_failures: int = 0
    n_core_failures: int = 0
    n_thin_failures: int = 0
    worst_ratio: float = 0.0
    accessory_cv: float = 0.0
    seam_score: float = 0.0
    gen_seconds: float = 0.0
    job_dir: Path | None = None
    failures: list[str] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _clip_frames(attempt_dir: Path) -> list[Path]:
    result = json.loads((attempt_dir / "result.json").read_text())
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError(f"{attempt_dir}: generation did not succeed")
    paths = []
    for name in result.get("frames", []):
        p = Path(name)
        if not p.is_absolute():
            p = p if p.is_file() and p.resolve().is_relative_to(attempt_dir.resolve()) else attempt_dir / p
        paths.append(p)
    if not paths:
        # RawResult order is authoritative; older attempts only have the clip directory.
        paths = sorted(
            (attempt_dir / "clip_rgba").glob("*.png"),
            key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p.name)],
        )
    if not paths:
        raise ValueError(f"{attempt_dir}: no clip frames")
    if any(not p.resolve().is_relative_to(attempt_dir.resolve()) or not p.is_file() for p in paths):
        raise ValueError(f"expected frames inside {attempt_dir}")
    return paths


def loop_candidates(attempt_dir: Path, *, n_frames: int, size: int, windows: str = "default") -> list[Candidate]:
    """Selector pick, plus distinct steady starts in `steady` mode (skip 6; seam endpoint must exist)."""
    if windows not in {"default", "steady"} or n_frames < 1 or size < 1:
        raise ValueError("windows must be default|steady; n_frames and size must be positive")
    attempt_dir = Path(attempt_dir).resolve()
    frames = _clip_frames(attempt_dir)
    result = json.loads((attempt_dir / "result.json").read_text())
    request = result["request"]
    direction, seed = request["direction"], request["seed"]
    if not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", direction):
        raise ValueError(f"invalid direction {direction!r}")
    timing = result.get("timing") or {}
    seconds = float(timing.get("wall_s", timing.get("wall", math.nan)))  # RunRecord timing uses wall_s
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 or not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{attempt_dir}: invalid seed or wall timing")
    pick = select_loop(frames, n_frames=n_frames, size=size)
    source = {
        "kind": "clip",
        **{k: result.get(k) for k in ("workflow_source", "workflow_source_sha256", "workflow_patched_sha256")},
        "prompt": result.get("params", {}).get("prompt", request.get("prompt", "")),
        "background": request.get("background", "#808080"),
    }
    best = Candidate(direction, attempt_dir, seed, pick, seconds, source)
    candidates = [best]
    if windows == "steady":
        small = []
        for path in frames:
            with Image.open(path) as im:
                rgba = np.asarray(im.convert("RGBA").resize((128, 128))).astype(np.float32) / 255
            small.append(rgba[..., :3] * rgba[..., 3:])
        for start in range(6, len(frames) - pick.period):
            if start == pick.start:
                continue
            picks = [start + round(k * pick.period / n_frames) for k in range(n_frames)]
            loop = replace(
                pick,
                start=start,
                picks=picks,
                files=[frames[i].name for i in picks],
                loop_seam_score=float(np.abs(small[start] - small[start + pick.period]).mean()),
            )
            candidates.append(replace(best, loop=loop, mode="steady"))
    return candidates


def accessory_cv(frames: Sequence[Path]) -> float:
    """CV of opaque thin-part counts on selected HD frames; a consistently absent accessory has CV zero."""
    counts = []
    for path in frames:
        with Image.open(path) as im:
            mask = np.asarray(im.convert("RGBA"))[..., 3] >= 128
        counts.append(int((mask & ~opening(mask, r=2)).sum()))
    values = np.asarray(counts, dtype=np.float64)
    return float(values.std() / values.mean()) if len(values) and values.mean() else 0.0


def _write_candidate(candidate: Candidate, out: Path) -> list[Path]:
    if not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", candidate.direction):
        raise ValueError(f"invalid direction {candidate.direction!r}")
    return write_loop(candidate.loop, _clip_frames(candidate.attempt_dir), out, candidate.direction)


def _direction_failure(message: str, direction: str) -> bool:
    # Frame issues and direction-wide frame_count/foot_line failures use different validator tags.
    return f"({direction}/" in message or bool(re.match(rf"^[^:]+: {re.escape(direction)}(?:\s|$)", message))


def _ratio(value: float, limit: float) -> float:
    return value / limit if limit else (0.0 if value == 0 else math.inf)


def _score(candidate: Candidate, report: dict, selected: list[Path], job: Path) -> Score:
    direction = candidate.direction
    failures = [f for f in report["failures"] if _direction_failure(f, direction)]
    diagnostics = [d for d in report.get("diagnostics", []) if d["direction"] == direction]
    failed = [d for d in diagnostics if d.get("gated") and d["value"] > d["limit"]]
    core = sum(d["rule"] == "foot_slide" or d.get("core_delta", 0) > d["limit"] for d in failed)
    thin = sum(d["rule"] == "bbox_drift" and d.get("core_delta", math.inf) <= d["limit"] for d in failed)
    metrics, limits = report["directions"].get(direction, {}), report["limits"]
    ratios = []
    for metric, limit, gated in (
        ("bbox_drift_max", "max_bbox_drift_px", limits.get("bbox_drift_gated", True)),
        ("foot_slide_max", "max_foot_slide_px", report.get("anchor_mode") != "bbox_bottom"),
    ):
        if gated and metrics.get(metric) is not None:
            ratios.append(_ratio(metrics[metric], limits[limit]))
    return Score(
        candidate=candidate,
        direction_pass=not failures,
        n_failures=len(failures),
        n_core_failures=core,
        n_thin_failures=thin,
        worst_ratio=max(ratios, default=0.0),
        accessory_cv=accessory_cv(selected),
        seam_score=candidate.loop.loop_seam_score,
        gen_seconds=candidate.gen_seconds,
        job_dir=job,
        failures=failures,
        diagnostics=diagnostics,
        metrics=metrics,
    )


def score_direction(
    candidates: Sequence[Candidate],
    *,
    profile: Path,
    character: str,
    anim: str,
    carrier: dict[str, Path],
    workdir: Path,
) -> list[Score]:
    """Process each candidate with the other carrier directions; retain only its direction's report slice."""
    directions = load_profile(profile).directions.generate
    if set(carrier) - set(directions) or any(c.direction not in directions for c in candidates):
        raise ValueError("candidate/carrier directions must be generated directions from the profile")
    needed = {d for c in candidates for d in directions if d != c.direction}
    if any(d not in carrier or not Path(carrier[d]).is_dir() for d in needed):
        raise ValueError("carrier must supply frame directories for the other directions")
    Path(workdir).mkdir(parents=True, exist_ok=True)
    scores = []
    for candidate in candidates:
        root = Path(tempfile.mkdtemp(prefix="candidate-", dir=workdir))
        selected = _write_candidate(candidate, root / "selected")
        raw = root / "raw-set"
        raw.mkdir()
        for d in directions:
            paths = selected if d == candidate.direction else read_frame_dir(carrier[d], directions).get(d, [])
            for p in paths:
                shutil.copyfile(p, raw / p.name)
        verdict = pipeline.run(raw, profile=profile, character=character, anim=anim, out=root / "job")
        report = json.loads(Path(verdict["report"]).read_text())
        scores.append(_score(candidate, report, selected, root / "job"))
    return scores


def rank(scores: Sequence[Score]) -> list[Score]:
    """Stable ascending sort by (not direction_pass, n_core_failures, n_failures, worst_ratio, accessory_cv,
    seam_score, gen_seconds). Equal keys preserve input order. Thin counts are diagnostic, never an extra key.
    """
    return sorted(
        scores,
        key=lambda s: (
            not s.direction_pass,
            s.n_core_failures,
            s.n_failures,
            s.worst_ratio,
            s.accessory_cv,
            s.seam_score,
            s.gen_seconds,
        ),
    )


def assemble_and_validate(choice: dict[str, Candidate], *, profile: Path, character: str, anim: str, out: Path) -> dict:
    """Copy selected frames into a final raw set and validate once; only this verdict grants technical_pass."""
    if not choice or any(d != c.direction for d, c in choice.items()):
        raise ValueError("choice must contain candidates keyed by their direction")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    raw = out / "raw-set"
    raw.mkdir()
    for d, candidate in choice.items():
        for p in _write_candidate(candidate, out / "selected" / d):
            shutil.copyfile(p, raw / p.name)
    return pipeline.run(raw, profile=profile, character=character, anim=anim, out=out / "job")


def write_sweep(scores: Sequence[Score], *, directions: Sequence[str], final_verdict: dict, out: Path) -> Path:
    """Keep every candidate, rank decision and blocking reason; production usability requires a separate review."""
    best = {d: next((s for s in rank(scores) if s.candidate.direction == d), None) for d in directions}
    blocking = {d: s.failures if s else ["no candidates"] for d, s in best.items() if s is None or not s.direction_pass}
    candidates = []
    for score in scores:
        chosen = best[score.candidate.direction] is score
        reasons = ["best rank"] if chosen else ["not best rank"]
        if not score.direction_pass:
            reasons.append(f"direction failed: {'; '.join(score.failures)}")
        entry = asdict(score)
        candidate = entry.pop("candidate")
        candidate["attempt_dir"] = str(candidate["attempt_dir"])
        entry["job_dir"] = str(score.job_dir) if score.job_dir is not None else None
        candidates.append({**candidate, "score": entry, "chosen": chosen, "rejected": not chosen, "reasons": reasons})
    return write_json(
        Path(out),
        "sweep.json",
        {
            "candidates": candidates,
            "qualified": not blocking,
            "best_blocking_reasons": blocking,
            "final_verdict": final_verdict,
            "technical_pass": final_verdict["pass"] is True,
            "production_usable": False,
        },
    )


def run_sweep(
    attempts: Sequence[Path],
    *,
    profile: Path,
    character: str,
    anim: str,
    carrier: Path,
    out: Path,
    windows: str = "default",
) -> dict:
    """Rank all attempt/windows per direction, assemble the best (including failing fallbacks), write sweep.json."""
    prof = load_profile(profile)
    spec = prof.character(character)
    if anim not in spec.animations:
        raise ValueError(f"{anim!r} is not a clip of {character}")
    if spec.cell[0] != spec.cell[1]:
        raise ValueError("loop selection requires square cells")
    candidates = []
    for attempt in attempts:
        # Loop frames keep the clip's own integer scale (Track E: canvas round16(cell*3) -> cell*3), not the profile's
        # gen_scale: resizing a x3 clip to x4 would change PixelOE's input and break parity with Phase 1/2 processing.
        with Image.open(_clip_frames(Path(attempt).resolve())[0]) as im:
            scale = im.width // spec.cell[0]
        if scale < 1:
            raise ValueError(f"{attempt}: clip frames are smaller than the {spec.cell[0]} px cell")
        candidates += loop_candidates(
            attempt, n_frames=spec.animations[anim].frames, size=spec.cell[0] * scale, windows=windows
        )
    if not candidates:
        raise ValueError("no candidates")
    if any(c.direction not in prof.directions.generate for c in candidates):
        raise ValueError("candidate direction is not in profile directions.generate")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    scores = score_direction(
        candidates,
        profile=profile,
        character=character,
        anim=anim,
        carrier=dict.fromkeys(prof.directions.generate, Path(carrier)),
        workdir=out / "candidates",
    )
    choice = {}
    for score in rank(scores):
        choice.setdefault(score.candidate.direction, score.candidate)
    verdict = assemble_and_validate(choice, profile=profile, character=character, anim=anim, out=out / "final")
    path = write_sweep(scores, directions=prof.directions.generate, final_verdict=verdict, out=out)
    return json.loads(path.read_text())


def plan_generation(
    character: str,
    anim: str,
    directions: Sequence[str],
    seeds: Sequence[int],
    stills_dir: Path,
    out_root: Path,
    background: str = "#808080",
) -> list[Path]:
    """Write one Track E GenerateRequest JSON per direction/seed; never create its attempt dir or run generation."""
    # The core AST gate permits no adapter import statements outside cli.py, even inside helpers.
    request_type = import_module("sonoranlab.comfy.adapters.base").GenerateRequest
    track_e = import_module("sonoranlab.comfy.adapters.track_e")

    if not NAME_RE.fullmatch(character) or not NAME_RE.fullmatch(anim) or not HEX_RE.fullmatch(background):
        raise ValueError("invalid character, animation or #rrggbb background")
    if not directions or len(set(directions)) != len(directions) or set(directions) - set(track_e.GENERATED_DIRECTIONS):
        raise ValueError("directions must be distinct Track E generated directions")
    if (
        not seeds
        or any(isinstance(s, bool) or not isinstance(s, int) or s < 0 for s in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise ValueError("seeds must be distinct non-negative integers")
    stills_dir, out_root = Path(stills_dir).resolve(), Path(out_root).resolve()
    for d in directions:
        with Image.open(stills_dir / f"still_{d}.png") as im:
            im.verify()
    out_root.mkdir(parents=True, exist_ok=False)
    requests = out_root / "requests"
    requests.mkdir()
    paths = []
    for d in directions:
        for seed in seeds:
            prompt = (
                track_e.RUN_PROMPTS[d] if anim == "run" else f"This character performing {anim} in place, facing {d}."
            )
            if background.lower() != "#808080":
                prompt = prompt.replace("grey background", f"{background} background")
            if anim != "run":
                prompt += f" Static camera, flat solid {background} background, 2D game sprite animation."
            request = request_type(
                track="E",
                character=character,
                anim=anim,
                direction=d,
                source_image=stills_dir / f"still_{d}.png",
                prompt=prompt,
                seed=seed,
                out_dir=out_root / "attempts" / f"{d}-{seed}",
                background=background,
            )
            paths.append(write_json(requests, f"{d}-{seed}.json", request.to_json()))
    return paths


def run_generation(
    request_paths: Sequence[Path],
    *,
    need_gib: float = 40,
    repo: Path,
    exec_container: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Run guarded requests serially, record every exit, continue failures; guard refusal (3/4) stops the sweep."""
    guarded_argv = import_module("sonoranlab.comfy.adapters.track_e").guarded_argv

    if not math.isfinite(need_gib) or need_gib <= 0 or not Path(repo).is_dir():
        raise ValueError("need_gib must be positive and repo must be a directory")
    paths = [Path(p).resolve() for p in request_paths]
    if not paths or any(not p.is_file() for p in paths):
        raise ValueError("requests must be existing JSON paths")
    records = []
    stopped = None
    for path in paths:
        argv = [sys.executable, "-m", "sonoranlab.cli", "generate", "--request", str(path)]
        if exec_container:
            argv += ["--exec", exec_container]
        guarded = guarded_argv(argv, need_gib)
        record = {"request": str(path), "argv": guarded}
        try:
            record["exit"] = runner(guarded, cwd=repo, check=False).returncode
        except OSError as e:
            record.update(exit=2, error=str(e))
        records.append(record)
        if record["exit"] in (3, 4):
            stopped = record["exit"]
            break
    return {"requests": records, "stop_exit": stopped, "remaining": [str(p) for p in paths[len(records) :]]}
