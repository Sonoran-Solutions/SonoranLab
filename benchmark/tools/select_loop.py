"""Pick one gait loop out of a Track E clip and write it as process.py RAW frames (disposable Phase 1 tooling).

Wan clips start from the still and run several strides. The gait period P is the lag (in source frames) with the
smallest mean alpha-mask difference over the steady part of the clip; the start s minimises the difference between
frame s and frame s+P. N frames are then sampled evenly over [s, s+P) and resized (Lanczos, still HD) to cell*scale,
keeping the subject's scale (no fitting). Everything is deterministic and logged to loop.json.

    uv run --no-project --with pillow --with numpy python -I benchmark/tools/select_loop.py CLIP_DIR RAW_OUT \
      --direction east --frames 7 --size 552 [--skip 6] [--min-lag N --max-lag 24]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clip", type=Path, help="dir with rgba_*.png in order")
    ap.add_argument("out", type=Path)
    ap.add_argument("--direction", required=True)
    ap.add_argument("--frames", type=int, required=True)
    ap.add_argument("--size", type=int, required=True, help="output frame size = cell * scale")
    ap.add_argument("--skip", type=int, default=6, help="ignore the run-up from the still")
    ap.add_argument("--min-lag", type=int, help="default: --frames (no duplicate picks)")
    ap.add_argument("--max-lag", type=int, default=24)
    a = ap.parse_args(argv)
    files = sorted(a.clip.glob("rgba_*.png"))
    ims = [Image.open(f).convert("RGBA") for f in files]
    # premultiplied RGB, not just the mask: from the side a left step and a right step have nearly the same
    # silhouette, so a mask-only score locks onto the half stride; near/far limb colours tell them apart
    masks = np.stack([np.asarray(im.resize((128, 128))).astype(np.float32) / 255 for im in ims])
    masks = masks[..., :3] * masks[..., 3:]
    n = len(ims)
    a.min_lag = max(a.min_lag or a.frames, a.frames)
    steady = range(a.skip, n)
    lags = {lag: float(np.mean([np.abs(masks[i] - masks[i + lag]).mean() for i in steady if i + lag < n]))
            for lag in range(a.min_lag, min(a.max_lag, n - a.skip - 1) + 1)}
    period = min(lags, key=lags.get)
    starts = {s: float(np.abs(masks[s] - masks[s + period]).mean()) for s in steady if s + period < n}
    start = min(starts, key=starts.get)
    picks = [start + round(k * period / a.frames) for k in range(a.frames)]
    a.out.mkdir(parents=True, exist_ok=False)
    for k, i in enumerate(picks):
        ims[i].resize((a.size, a.size), Image.LANCZOS).save(a.out / f"{a.direction}_{k}.png")
    info = {"clip": str(a.clip), "source_frames": n, "period": period, "start": start, "picks": picks,
            "period_score": lags[period], "loop_seam_score": starts[start], "lag_scores": lags,
            "size": a.size, "files": [files[i].name for i in picks]}
    (a.out / "loop.json").write_text(json.dumps(info, indent=1) + "\n")
    print(json.dumps({k: info[k] for k in ("period", "start", "picks", "period_score", "loop_seam_score")}))


if __name__ == "__main__":
    main()
