"""Gait-loop selection for a Track E clip (ported from benchmark/tools/select_loop.py, same algorithm, same picks).

Wan clips start from the still and run several strides. The gait period P is the lag (in source frames) with the
smallest mean difference over the steady part of the clip; the start s minimises the difference between frame s and
frame s+P. N frames are then sampled evenly over [s, s+P) and resized (Lanczos, still HD) to `size`, keeping the
subject's scale (no fitting). Pure CPU, deterministic; numpy/PIL only (no comfy imports).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

Frame = Path | Image.Image


@dataclass(frozen=True)
class LoopPick:
    period: int  # gait period in source frames
    start: int
    picks: list[int]  # source frame indices, n_frames long
    period_score: float
    loop_seam_score: float
    lag_scores: dict[int, float] = field(default_factory=dict)
    size: int = 0
    source_frames: int = 0
    clip: str | None = None  # directory of the source frames (Path input only)
    files: list[str] | None = None  # file names of the picks (Path input only)


def _load(frames: list[Frame]) -> list[Image.Image]:
    return [(f if isinstance(f, Image.Image) else Image.open(f)).convert("RGBA") for f in frames]


def select_loop(
    frames: list[Path] | list[Image.Image],
    *,
    n_frames: int,
    size: int,
    skip: int = 6,
    min_lag: int | None = None,
    max_lag: int = 24,
) -> LoopPick:
    """Pick `n_frames` evenly over one gait period of `frames` (time order). `size` is recorded for `write_loop`."""
    ims = _load(frames)
    n = len(ims)
    # premultiplied RGB, not just the mask: from the side a left step and a right step have nearly the same
    # silhouette, so a mask-only score locks onto the half stride; near/far limb colours tell them apart
    small = np.stack([np.asarray(im.resize((128, 128))).astype(np.float32) / 255 for im in ims])
    small = small[..., :3] * small[..., 3:]
    min_lag = max(min_lag or n_frames, n_frames)  # default: no duplicate picks
    steady = range(skip, n)
    lags = {
        lag: float(np.mean([np.abs(small[i] - small[i + lag]).mean() for i in steady if i + lag < n]))
        for lag in range(min_lag, min(max_lag, n - skip - 1) + 1)
    }
    if not lags:
        raise ValueError(f"clip of {n} frames is too short for lags {min_lag}..{max_lag} after skipping {skip}")
    period = min(lags, key=lags.__getitem__)
    starts = {s: float(np.abs(small[s] - small[s + period]).mean()) for s in steady if s + period < n}
    start = min(starts, key=starts.__getitem__)
    picks = [start + round(k * period / n_frames) for k in range(n_frames)]
    paths = [f for f in frames if isinstance(f, Path)]
    named = len(paths) == n
    return LoopPick(
        period=period,
        start=start,
        picks=picks,
        period_score=lags[period],
        loop_seam_score=starts[start],
        lag_scores=lags,
        size=size,
        source_frames=n,
        clip=str(paths[0].parent) if named else None,
        files=[paths[i].name for i in picks] if named else None,
    )


def write_loop(pick: LoopPick, frames: list[Path] | list[Image.Image], out_dir: Path, direction: str) -> list[Path]:
    """Exclusively create `out_dir` with `<direction>_<k>.png` (HD, `pick.size` square) and loop.json."""
    ims = _load(frames)
    out_dir.mkdir(parents=True, exist_ok=False)
    outs = []
    for k, i in enumerate(pick.picks):
        outs.append(out_dir / f"{direction}_{k}.png")
        ims[i].resize((pick.size, pick.size), Image.LANCZOS).save(outs[-1])
    info = {
        "clip": pick.clip,
        "source_frames": pick.source_frames,
        "period": pick.period,
        "start": pick.start,
        "picks": pick.picks,
        "period_score": pick.period_score,
        "loop_seam_score": pick.loop_seam_score,
        "lag_scores": pick.lag_scores,
        "size": pick.size,
        "files": pick.files,
    }
    (out_dir / "loop.json").write_text(json.dumps(info, indent=1) + "\n")
    return outs
