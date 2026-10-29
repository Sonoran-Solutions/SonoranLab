"""Gait-loop selection for a Track E clip (ported from benchmark/tools/select_loop.py, same algorithm, same picks).

Wan clips start from the still and run several strides. The gait period P is the lag (in source frames) with the
smallest mean difference over the steady part of the clip; the start s minimises the difference between frame s and
frame s+P. N frames are then sampled evenly over [s, s+P) and resized (Lanczos, still HD) to `size`, keeping the
subject's scale (no fitting). Pure CPU, deterministic; numpy/PIL only (no comfy imports).

`select_once` is the `loop: false` counterpart (cast, death): the clip starts at frame 0 (the approved still) and
N frames are sampled evenly over [0, E]. E is chosen by an end rule (`end_rule`): `return` (an action that comes back
to the start pose) or `settle` (validator_mode fall: motion that ends at rest). Loop clips never reach it.
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
    window: str = "loop"  # loop (select_loop) | return | settle (select_once end rules)
    end: dict | None = None  # select_once: how E was chosen and its metric


END_RULES = ("return", "settle")
MAX_ONCE_END = 64  # select_once never searches past this source frame (Wan clips are 33 frames)
SETTLE_FRACTION = 0.2  # fallback only (frames without alpha): every later step < this share of the largest step
# settled (alpha frames): every later frame's silhouette is within this share of the final frame's, the same bound the
# validator's fall_hold puts on the exported last pair (validate.FALL_HOLD_MAX_DELTA; kept equal by a test)
SETTLE_SILHOUETTE = 0.02


def _load(frames: list[Frame]) -> list[Image.Image]:
    return [(f if isinstance(f, Image.Image) else Image.open(f)).convert("RGBA") for f in frames]


def _small(ims: list[Image.Image]) -> np.ndarray:
    # premultiplied RGB, not just the mask: from the side a left step and a right step have nearly the same
    # silhouette, so a mask-only score locks onto the half stride; near/far limb colours tell them apart
    small = np.stack([np.asarray(im.resize((128, 128))).astype(np.float32) / 255 for im in ims])
    return small[..., :3] * small[..., 3:]


def _pick(frames: list[Frame], picks: list[int]) -> tuple[str | None, list[str] | None]:
    paths = [f for f in frames if isinstance(f, Path)]
    named = len(paths) == len(frames)
    return (str(paths[0].parent), [paths[i].name for i in picks]) if named else (None, None)


def end_rule(anim) -> str | None:
    """None for a loop clip (select_loop); `settle` for validator_mode fall; else `return`. `anim` is a profile
    Animation. Explicit only: hold_last never selects a rule."""
    if anim.loop:
        return None
    return "settle" if anim.validator_mode == "fall" else "return"


def select_once(
    frames: list[Path] | list[Image.Image], *, n_frames: int, size: int, end: str, max_end: int = MAX_ONCE_END
) -> LoopPick:
    """Pick `n_frames` evenly over [0, E] of a non-looping clip: frame 0 is the approved still pose, E is the last pick.

    Per-frame metric: mean abs difference of the premultiplied 128x128 RGB (select_loop's representation).
      return: E >= n_frames minimises |frame E - frame 0|, searched only after the frame farthest from frame 0 (the
              action's peak) so a clip that has not moved yet is never picked; no candidate -> the last frame.
      settle: threshold = SETTLE_FRACTION x the largest consecutive step; E = the first frame >= n_frames - 1 from
              which every later consecutive step stays below it; never settles -> the last frame.
    The search is bounded by the clip and `max_end`. `loop_seam_score` (the sweep's tiebreak) is |E - frame 0| for
    return and the step between the last two picks for settle."""
    if end not in END_RULES:
        raise ValueError(f"end rule {end!r}: expected one of {', '.join(END_RULES)}")
    if n_frames < 2 or max_end < n_frames:
        raise ValueError(f"n_frames {n_frames} must be >= 2 and max_end {max_end} >= n_frames")
    ims = _load(frames)
    n = len(ims)
    if n <= n_frames:
        raise ValueError(f"clip of {n} frames is too short for {n_frames} picks after frame 0")
    small = _small(ims)
    last = min(n - 1, max_end)
    first = [float(np.abs(small[i] - small[0]).mean()) for i in range(last + 1)]
    steps = [float(np.abs(small[i + 1] - small[i]).mean()) for i in range(n - 1)]
    if end == "return":
        peak = int(np.argmax(first))
        lo = max(n_frames, peak + 1)
        e = min(range(lo, last + 1), key=first.__getitem__) if lo <= last else last
        info = {"rule": end, "peak": peak, "peak_delta": first[peak], "search": [lo, last], "end_delta": first[e]}
    else:
        masks = [np.asarray(im)[..., 3] > 127 for im in ims[: last + 1]]
        if any(m.any() and not m.all() for m in masks):
            # Silhouette against the final frame, not step size: a body sliding slowly after it lands takes small
            # steps that add up (live gun death seeds: "settled" by steps at frame 25, still 20% off the final pose).
            final = masks[last]

            def off(m: np.ndarray) -> float:
                return float((m ^ final).sum() / max(1, (m | final).sum()))

            to_final = [off(m) for m in masks]
            settled = [i for i in range(n_frames - 1, last) if all(v <= SETTLE_SILHOUETTE for v in to_final[i:])]
            info = {"rule": end, "metric": "silhouette_to_final", "threshold": SETTLE_SILHOUETTE}
            info |= {"to_final": [round(v, 4) for v in to_final]}
        else:
            threshold = SETTLE_FRACTION * max(steps)
            # the last frame has no later step to judge: it is the fallback, never "settled"
            settled = [i for i in range(n_frames - 1, min(last, n - 2) + 1) if all(s < threshold for s in steps[i:])]
            info = {"rule": end, "metric": "step", "threshold": threshold, "max_step": max(steps)}
        e = settled[0] if settled else last
        info |= {"settled": bool(settled), "search": [n_frames - 1, last], "steps": steps}
    picks = [round(k * e / (n_frames - 1)) for k in range(n_frames)]
    if end == "settle" and settled:
        # The held final pair must both be resting: spread the first n-1 picks over [0, E] and take the last one a
        # stride later (every step after E is below the settle threshold), so fall_hold judges two resting frames.
        stride = max(1, round(e / (n_frames - 2)))
        hold = last if info["metric"] == "silhouette_to_final" else min(last, e + stride)  # both resting either way
        picks = [round(k * e / (n_frames - 2)) for k in range(n_frames - 1)] + [hold]
        info["hold_pick"] = picks[-1]
    seam = first[e] if end == "return" else float(np.abs(small[picks[-1]] - small[picks[-2]]).mean())
    clip, files = _pick(frames, picks)
    return LoopPick(
        period=e,
        start=0,
        picks=picks,
        period_score=first[e],
        loop_seam_score=seam,
        size=size,
        source_frames=n,
        clip=clip,
        files=files,
        window=end,
        end=info | {"end": e},
    )


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
    small = _small(ims)
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
    if pick.window != "loop":  # loop clips keep their loop.json byte-identical
        info |= {"window": pick.window, "end": pick.end}
    (out_dir / "loop.json").write_text(json.dumps(info, indent=1) + "\n")
    return outs
