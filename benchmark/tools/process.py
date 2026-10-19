"""Phase 1 per-frame post-processing + scoring (DYL-71). Disposable benchmark tooling, not the Phase 2 package.

Chain per frame (docs/05, frozen in benchmark/characters/2026-10-18.yaml):
  alpha_extract -> pixeloe_kcentroid -> palette_enforce -> alpha_clean -> register_per_clip -> validate -> contact

  run        <raw_dir> of <direction>_<i>.png HD frames -> <out>/processed/, sheet.png, report.json, contact.png
  calibrate  shipped ground-truth clips (extract_refs.py output) -> observed limits + proposed thresholds

    uv run --no-project --with pillow --with numpy --with pixeloe==1.0.0 python -I benchmark/tools/process.py \
      run RAW_DIR OUT_DIR --character samurai --clip run --cell 184 --frames 7
    uv run --no-project --with pillow --with numpy python -I benchmark/tools/process.py \
      calibrate ~/sonoranlab-work/benchmark/characters/2026-10-18 ~/sonoranlab-work/benchmark/calibration-2026-10-18.json

--config PATH (benchmark/characters/2026-10-18-processing-v1.yaml) supplies the frozen downscaler, thickness, despill,
key_tol, colour cap, validator warn limits and the per-character anchor mode / thresholds; explicit flags override it.
report.json "provenance" records the config sha256, every effective parameter (in "params"), input and output file
hashes, wall time, this tool's sha256 and, when RAW's parent holds a selection.json, that file's sha256 and "set".

Definitions (frame coordinates after registration, bbox inclusive):
  foot_y        lowest opaque row of a frame.
  contact frame foot_y within 1 px of the clip's lowest row (per direction).
  foot_x        centre of the longest opaque run on the frame's foot_y row ((start + end + 1) / 2).
  foot_slide    contact frames only: |foot_x - cell/2|, the horizontal distance of the planted foot from the anchor.
  bbox_drift    max |delta| over the four bbox edges vs the previous frame (frame 0 vs the last for loops).
  opaque_area_cv  population std / mean of opaque pixel counts across the clip.
  loop_closure_delta  share of the last+first opaque union whose alpha or colour differs between last and first.
  loop_closure_ratio  loop_closure_delta / mean delta of the in-clip adjacent pairs (~1 = seamless loop).
Registration is per clip and per direction (ADR-0007): one (dx, dy) for every frame of a direction, so the clip's
lowest opaque row lands on cell - 4 and the median per-frame bbox centre lands on cell/2. The median (not the union
bbox) keeps one bad frame from moving the whole clip. anchor_mode bbox_bottom uses the same rule: with no foot zone in
the profile, the bbox bottom IS the lowest opaque row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# Values from profiles/twos-are-up.yaml (frozen 2026-10-18).
BG_KEY = (255, 0, 255)
ALPHA_THRESHOLD = 128
PADDING = 2
FOOT_UP = 4  # lowest opaque row of the clip = cell - 4
MAX_COLOURS = 64
ROWS = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"]
GEN_DIRS = ["north", "north-east", "east", "south-east", "south"]
MIRROR = {"north-west": "north-east", "west": "east", "south-west": "south-east"}
PALETTE = Path("/home/dq/Projects/TwosAreUp/assets/pixel/v0/characters/palette.png")
DOWNSCALERS = ("k_centroid", "contrast", "nearest", "test_nearest")  # test_nearest: tests only, no PixelOE
CV_WARN = 0.10  # docs/08 default, until the calibrated addendum replaces it
# docs/08's loop_closure warn (> 0.15 of pixels) does not survive calibration: the shipped loops differ in 71-90% of
# their opaque pixels last->first, about as much as any adjacent pair. So the warning gates loop_closure_ratio instead.
FRAME_RE = re.compile(r"^([a-z-]+)_(\d+)\.png$")
CONFIG_VERSIONS = ("processing-v1",)  # --config files this tool understands (benchmark/characters/2026-10-18-processing-v1.yaml)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path: Path) -> dict:
    """The frozen processing configuration (yaml). Refuses a missing or unknown `version`."""
    import yaml  # only --config needs it

    cfg = yaml.safe_load(path.read_text())
    if not isinstance(cfg, dict) or cfg.get("version") not in CONFIG_VERSIONS:
        got = cfg.get("version") if isinstance(cfg, dict) else None
        raise SystemExit(f"{path}: config version {got!r} is not one of {CONFIG_VERSIONS}")
    return cfg


# ---------------------------------------------------------------- per-frame chain


def load_palette(path: Path) -> np.ndarray:
    px = np.asarray(Image.open(path).convert("RGB")).reshape(-1, 3)
    pal = np.unique(px, axis=0)
    assert not (pal == BG_KEY).all(1).any(), "background key collides with the palette"
    return pal


def extract_alpha(im: Image.Image, key_tol: float) -> tuple[np.ndarray, np.ndarray]:
    """HD RGB composited over the key, and HD alpha: the image's own alpha if it has any, else a chroma key."""
    a = np.asarray(im.convert("RGBA")).astype(np.float32)
    rgb, alpha = a[..., :3], a[..., 3]
    if im.mode in ("RGBA", "LA", "PA") and (alpha < 255).any():
        w = alpha[..., None] / 255
        rgb = rgb * w + np.array(BG_KEY, np.float32) * (1 - w)
    else:
        alpha = np.where(np.linalg.norm(rgb - BG_KEY, axis=-1) <= key_tol, 0.0, 255.0)
    return np.rint(rgb).astype(np.uint8), alpha


def despill(rgb: np.ndarray, alpha: np.ndarray, band: int) -> tuple[np.ndarray, int]:
    """Remove magenta key spill from the silhouette's edge band (HD px): subtract min(R, B) - G where positive.

    Track C/E mattes (BiRefNet) keep the video's magenta-tinted edge colours, and soft edges are composited over the key
    here, so thin parts (a katana blade) snapped to purples (DYL-77, 2026-10-18). Only opaque pixels within `band` px of
    the matte edge are touched, so interior purples (relay_sentinel's orb) survive. Returns pixels changed."""
    if band <= 0:
        return rgb, 0
    solid = alpha >= 255
    edge = (alpha > 0) & ~solid
    near = edge | (alpha <= 0)
    for _ in range(band):   # grow the "near the edge" region into the solid interior
        p = np.pad(near, 1)
        near = near | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    m = (alpha > 0) & near
    c = rgb.astype(np.int16)
    spill = np.minimum(c[..., 0], c[..., 2]) - c[..., 1]
    hit = m & (spill > 0)
    c[..., 0] -= np.where(hit, spill, 0)
    c[..., 2] -= np.where(hit, spill, 0)
    return c.clip(0, 255).astype(np.uint8), int(hit.sum())


def downscale(rgb: np.ndarray, scale: int, method: str, thickness: int) -> np.ndarray:
    h, w, _ = rgb.shape
    if method == "test_nearest":  # ponytail: tests only; the benchmark path must use PixelOE k_centroid
        return rgb[scale // 2 :: scale, scale // 2 :: scale][: h // scale, : w // scale].copy()
    import torch
    from pixeloe.torch.pixelize import pixelize

    t = torch.from_numpy(rgb).permute(2, 0, 1)[None].float() / 255
    with torch.no_grad():
        out = pixelize(t, pixel_size=scale, thickness=thickness, mode=method, no_post_upscale=True, backend="torch")
    return np.rint(out[0].permute(1, 2, 0).clamp(0, 1).numpy() * 255).astype(np.uint8)


def snap(rgb: np.ndarray, pal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Nearest palette colour in RGB, and the mask of pixels that were not already a palette colour."""
    flat = rgb.reshape(-1, 3).astype(np.int32)
    d = ((flat[:, None, :] - pal[None].astype(np.int32)) ** 2).sum(-1)
    idx = d.argmin(1)
    off = d[np.arange(len(flat)), idx] > 0
    return pal[idx].reshape(rgb.shape), off.reshape(rgb.shape[:2])


def limit_colours(rgb: np.ndarray, opaque: np.ndarray, limit: int) -> tuple[np.ndarray, int]:
    """Merge the least-used colour into its nearest remaining colour until <= limit remain (stays on the palette).

    Shipped Twos Are Up cells use 30-56 colours; a palette snap alone leaves 60-80 on generated frames (DYL-77).
    Deterministic: ties broken by colour value. Returns the new image and how many colours were merged away."""
    out = rgb.copy()
    cols, counts = np.unique(out[opaque].reshape(-1, 3), axis=0, return_counts=True)
    merged = 0
    while len(cols) > limit:
        i = int(np.lexsort((*cols.T[::-1], counts))[0])          # least used, then smallest colour value
        rest = np.delete(np.arange(len(cols)), i)
        j = rest[((cols[rest].astype(np.int32) - cols[i].astype(np.int32)) ** 2).sum(1).argmin()]
        m = opaque & (out == cols[i]).all(-1)
        out[m] = cols[j]
        counts[j] += counts[i]
        cols, counts = cols[rest], counts[rest]
        merged += 1
    return out, merged


def border(mask: np.ndarray) -> np.ndarray:
    """Opaque pixels with a transparent (or out-of-frame) 4-neighbour."""
    p = np.pad(mask, 1, constant_values=False)
    inner = p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return mask & ~inner


def process_frame(
    im: Image.Image, scale: int, pal: np.ndarray, method: str = "k_centroid", thickness: int = 2, key_tol: float = 48,
    despill_px: int = 0, max_colours: int = MAX_COLOURS,
) -> tuple[np.ndarray, dict]:
    """One raw HD frame -> RGBA uint8 at (H/scale, W/scale), binary alpha, palette-locked. Not yet registered."""
    rgb, alpha = extract_alpha(im, key_tol)
    rgb, despilled = despill(rgb, alpha, despill_px)
    h, w = alpha.shape
    assert h % scale == 0 and w % scale == 0, f"raw frame {w}x{h} is not a multiple of scale {scale}"
    small = downscale(rgb, scale, method, thickness)
    a_small = alpha.reshape(h // scale, scale, w // scale, scale).mean((1, 3))
    opaque = a_small >= ALPHA_THRESHOLD
    snapped, off = snap(small, pal)
    off_before = int((off & opaque).sum())
    # fringe: a silhouette-border pixel whose pre-snap colour is nearer the key than the palette colour it snapped to
    pre = small.astype(np.float32)
    to_key = np.linalg.norm(pre - BG_KEY, axis=-1)
    to_pal = np.linalg.norm(pre - snapped, axis=-1)
    fringe = border(opaque) & (to_key < to_pal)  # fringe_passes: 1
    opaque &= ~fringe
    snapped, merged = limit_colours(snapped, opaque, max_colours)
    out = np.zeros((h // scale, w // scale, 4), np.uint8)
    out[opaque, :3] = snapped[opaque]
    out[opaque, 3] = 255
    return out, {"off_palette_before": off_before, "fringe_removed": int(fringe.sum()), "colours_merged": merged, "despilled_hd_px": despilled}


# ---------------------------------------------------------------- registration


def bbox(f: np.ndarray) -> list[int] | None:
    ys, xs = np.nonzero(f[..., 3])
    return None if len(xs) == 0 else [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def register_clip(frames: list[np.ndarray], cell: int) -> tuple[list[np.ndarray], dict]:
    """One shift for the whole clip: lowest opaque row -> cell - 4, median bbox centre -> cell / 2."""
    boxes = [bbox(f) for f in frames]
    if any(b is None for b in boxes):
        return [np.zeros((cell, cell, 4), np.uint8) for _ in frames], {"error": "empty frame", "shift_px": None}
    centres = [(b[0] + b[2] + 1) / 2 for b in boxes]
    lowest = [b[3] for b in boxes]
    dx = round_half_up(cell / 2 - float(np.median(centres)))
    dy = (cell - FOOT_UP) - max(lowest)
    out, clipped = [], []
    for f in frames:
        c = np.zeros((cell, cell, 4), np.uint8)
        h, w = f.shape[:2]
        sx0, sy0 = max(0, -dx), max(0, -dy)
        sx1, sy1 = min(w, cell - dx), min(h, cell - dy)
        if sx1 > sx0 and sy1 > sy0:
            c[sy0 + dy : sy1 + dy, sx0 + dx : sx1 + dx] = f[sy0:sy1, sx0:sx1]
        clipped.append(int((f[..., 3] > 0).sum() - (c[..., 3] > 0).sum()))
        out.append(c)
    return out, {
        "shift_px": [dx, dy],
        "raw_lowest_rows": lowest,
        "raw_shifts_px": [[round_half_up(cell / 2 - cx), (cell - FOOT_UP) - y] for cx, y in zip(centres, lowest)],
        "clipped_px": clipped,
    }


# ---------------------------------------------------------------- validation


def foot_x(f: np.ndarray, row: int) -> float:
    xs = np.nonzero(f[row, :, 3])[0]
    runs = np.split(xs, np.nonzero(np.diff(xs) > 1)[0] + 1)
    r = max(runs, key=len)  # first longest run on ties
    return (int(r[0]) + int(r[-1]) + 1) / 2


def frame_delta(a: np.ndarray, b: np.ndarray) -> float:
    union = (a[..., 3] > 0) | (b[..., 3] > 0)
    diff = (a != b).any(-1) & union
    return float(diff.sum() / max(1, union.sum()))


def validate(
    clips: dict[str, list[np.ndarray]],
    cell: int,
    frames: int,
    directions: list[str],
    pal: np.ndarray,
    *,
    loop: bool = True,
    max_foot_slide: float | None = None,
    max_bbox_drift: int | None = None,
    cv_warn: float = CV_WARN,
    loop_ratio_warn: float | None = None,
    pre: dict[str, list[dict]] | None = None,
    max_colours: int = MAX_COLOURS,
) -> dict:
    """docs/08 report for registered clips {direction: [RGBA cell frames]}."""
    palset = {tuple(c) for c in pal.tolist()}
    failures, warnings, rows, cycles = [], [], [], {}
    actual = sum(len(clips.get(d, [])) for d in directions)
    missing = [d for d in directions if len(clips.get(d, [])) != frames]
    if missing:
        failures.append(f"frame_count: expected {frames} frames in each of {directions}; wrong/missing: {missing}")
    for d in [d for d in ROWS if d in clips]:
        fs = clips[d]
        boxes = [bbox(f) for f in fs]
        lows = [b[3] if b else None for b in boxes]
        clip_low = max((y for y in lows if y is not None), default=None)
        recs = []
        for i, (f, b) in enumerate(zip(fs, boxes)):
            r: dict = {"index": len(rows), "direction": d, "frame": i, "bbox": b, "foot_y": lows[i], "issues": []}
            tag = f"frame {len(rows)} ({d}/{i})"
            r.update(pre[d][i] if pre and d in pre else {})
            a = f[..., 3]
            opaque = a == 255
            r["canvas"] = list(f.shape[1::-1])
            r["semi_alpha_pixels"] = int(((a > 0) & (a < 255)).sum())
            r["opaque_pixels"] = int(opaque.sum())
            cols = {tuple(c) for c in f[opaque, :3].tolist()}
            r["colours"] = len(cols)
            r["off_palette_after"] = int(sum(1 for c in f[opaque, :3].tolist() if tuple(c) not in palset))
            edges = []
            if b:
                edges = [
                    n
                    for n, hit in (
                        ("left", b[0] < PADDING),
                        ("top", b[1] < PADDING),
                        ("right", b[2] > cell - 1 - PADDING),
                        ("bottom", b[3] > cell - 1 - PADDING),
                    )
                    if hit
                ]
            r["touches_edge"] = edges or False
            contact = b is not None and lows[i] >= clip_low - 1
            r["contact"] = contact
            r["foot_x"] = foot_x(f, lows[i]) if b else None
            # ponytail: anchor-relative, so a stride's natural fore/aft foot placement counts too (shipped max 30 px);
            # a true slide metric follows the planted foot across consecutive contact frames (Phase 2 validator).
            r["foot_slide"] = abs(r["foot_x"] - cell / 2) if contact else None
            prev = boxes[i - 1] if (i > 0 or (loop and len(fs) > 1)) else None
            r["bbox_drift"] = max(abs(p - q) for p, q in zip(b, prev)) if b and prev else None
            if r["canvas"] != [cell, cell]:
                r["issues"].append(f"canvas: {tag} is {r['canvas']}, expected {cell}x{cell}")
            if b is None:
                r["issues"].append(f"empty: {tag} has no opaque pixels")
            if r["off_palette_after"]:
                r["issues"].append(f"palette: {tag} has {r['off_palette_after']} off-palette pixels")
            if r["semi_alpha_pixels"]:
                r["issues"].append(f"semi_alpha: {tag} has {r['semi_alpha_pixels']} semi-transparent pixels")
            if r["colours"] > max_colours:
                r["issues"].append(f"colours: {tag} uses {r['colours']} colours, limit {max_colours}")
            if edges:
                r["issues"].append(f"touches_edge: {tag} {'/'.join(edges)} edge inside padding {PADDING}, clipping")
            if max_foot_slide is not None and r["foot_slide"] is not None and r["foot_slide"] > max_foot_slide:
                r["issues"].append(f"foot_slide: {tag} foot {r['foot_slide']:g}px from anchor, limit {max_foot_slide}")
            if max_bbox_drift is not None and r["bbox_drift"] is not None and r["bbox_drift"] > max_bbox_drift:
                r["issues"].append(f"bbox_drift: {tag} bbox moved {r['bbox_drift']}px, limit {max_bbox_drift}")
            r["ok"] = not r["issues"]
            failures += r["issues"]
            recs.append(r)
            rows.append(r)
        areas = np.array([r["opaque_pixels"] for r in recs], np.float64)
        cyc: dict = {
            "lowest_row": clip_low,
            "lowest_row_ok": clip_low == cell - FOOT_UP,
            "foot_rows": lows,
            "contact_frames": [r["frame"] for r in recs if r["contact"]],
            "foot_slide_max": max((r["foot_slide"] for r in recs if r["foot_slide"] is not None), default=None),
            "bbox_drift_max": max((r["bbox_drift"] for r in recs if r["bbox_drift"] is not None), default=None),
            "opaque_area_cv": float(areas.std() / areas.mean()) if areas.mean() else None,
        }
        if loop and len(fs) > 1:
            adj = [frame_delta(fs[i], fs[i + 1]) for i in range(len(fs) - 1)]
            cyc["loop_closure_delta"] = frame_delta(fs[-1], fs[0])
            cyc["loop_closure_ratio"] = cyc["loop_closure_delta"] / np.mean(adj) if np.mean(adj) else None
        if not cyc["lowest_row_ok"]:
            failures.append(f"foot_line: {d} clip lowest opaque row {clip_low}, expected {cell - FOOT_UP}")
        if cyc["opaque_area_cv"] is not None and cyc["opaque_area_cv"] > cv_warn:
            warnings.append(f"opaque_area_cv {cyc['opaque_area_cv']:.3f} in {d}; > {cv_warn} can mean a lost accessory")
        lr = cyc.get("loop_closure_ratio")
        if loop_ratio_warn is not None and lr is not None and lr > loop_ratio_warn:
            warnings.append(f"loop_closure_ratio {lr:.2f} in {d}: last->first jumps more than the clip's own steps")
        cycles[d] = cyc
    for name, lim in (
        ("max_foot_slide", max_foot_slide),
        ("max_bbox_drift", max_bbox_drift),
        ("loop_ratio_warn", loop_ratio_warn if loop else 0),
    ):
        if lim is None:
            warnings.append(f"{name} not set: measured, not gated (calibrated limits: 2026-10-18.yaml addendum)")

    def cmax(k: str) -> float | None:
        return max((c[k] for c in cycles.values() if c.get(k) is not None), default=None)

    return {
        "pass": not failures,
        "frame_count": {"expected": frames * len(directions), "actual": actual, "ok": not missing},
        "frames": rows,
        "cycle": {
            k: cmax(k)
            for k in ("foot_slide_max", "bbox_drift_max", "opaque_area_cv", "loop_closure_delta", "loop_closure_ratio")
        },
        "directions": cycles,
        "failures": failures,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- outputs


def sheet(clips: dict[str, list[np.ndarray]], cell: int, frames: int, stills: Path | None) -> Image.Image:
    """Game layout: 8 rows (ROWS order), column 0 = still (or empty), then the clip; west rows mirror east."""
    out = Image.new("RGBA", ((frames + 1) * cell, len(ROWS) * cell))
    for r, d in enumerate(ROWS):
        if stills and (stills / f"still_{d}.png").exists():
            out.paste(Image.open(stills / f"still_{d}.png").convert("RGBA"), (0, r * cell))
        src = clips.get(d) or [f[:, ::-1] for f in clips.get(MIRROR.get(d, ""), [])]
        for i, f in enumerate(src):
            out.paste(Image.fromarray(np.ascontiguousarray(f)), ((i + 1) * cell, r * cell))
    return out


def contact(clips: dict[str, list[np.ndarray]], cell: int, report: dict, scale: int = 8) -> Image.Image:
    """scale x nearest, grey background, red foot line at cell - 4, label per frame (! = frame has issues)."""
    dirs = [d for d in ROWS if d in clips]
    n = max(len(clips[d]) for d in dirs)
    s = cell * scale
    out = Image.new("RGB", (n * s, len(dirs) * s), (96, 96, 96))
    draw = ImageDraw.Draw(out)
    bad = {(r["direction"], r["frame"]) for r in report["frames"] if not r["ok"]}
    for r, d in enumerate(dirs):
        for i, f in enumerate(clips[d]):
            x, y = i * s, r * s
            draw.rectangle([x, y, x + s - 1, y + s - 1], fill=(128, 128, 128), outline=(64, 64, 64))
            big = Image.fromarray(f).resize((s, s), Image.NEAREST)
            out.paste(big, (x, y), big)
            fy = y + (cell - FOOT_UP + 1) * scale - 1
            draw.line([x, fy, x + s - 1, fy], fill=(255, 0, 0))
            draw.text((x + 4, y + 4), f"{d} {i}{' !' if (d, i) in bad else ''}", fill=(255, 255, 0))
    return out


def frame_files(raw: Path) -> dict[str, dict[int, Path]]:
    found: dict[str, dict[int, Path]] = {}
    for p in sorted(raw.glob("*.png")):
        m = FRAME_RE.match(p.name)
        if m and m[1] in ROWS:
            found.setdefault(m[1], {})[int(m[2])] = p
    return found


def load_clips(raw: Path) -> dict[str, list[Image.Image]]:
    return {d: [Image.open(fs[i]) for i in sorted(fs)] for d, fs in frame_files(raw).items()}


def run(
    raw: Path,
    out: Path,
    *,
    character: str,
    clip: str,
    cell: int,
    frames: int,
    scale: int = 4,
    palette: Path = PALETTE,
    anchor_mode: str = "foot",
    directions: list[str] = GEN_DIRS,
    downscaler: str = "k_centroid",
    thickness: int = 2,
    key_tol: float = 48,
    despill_px: int = 0,
    loop: bool = True,
    stills: Path | None = None,
    max_foot_slide: float | None = None,
    max_bbox_drift: int | None = None,
    loop_ratio_warn: float | None = None,
    contact_scale: int = 8,
    max_colours: int = MAX_COLOURS,
    cv_warn: float = CV_WARN,
    config: Path | None = None,
) -> dict:
    t0 = time.perf_counter()
    raw, out = raw.resolve(), out.resolve()
    assert raw not in (out, out / "processed"), "raw inputs are never overwritten: pick another out dir"
    assert downscaler in DOWNSCALERS, downscaler
    pal = load_palette(palette)
    clips, pre, reg = {}, {}, {}
    for d, ims in load_clips(raw).items():
        res = [process_frame(im, scale, pal, downscaler, thickness, key_tol, despill_px, max_colours) for im in ims]
        clips[d], reg[d] = register_clip([f for f, _ in res], cell)
        pre[d] = [s for _, s in res]
    rep = validate(
        clips,
        cell,
        frames,
        directions,
        pal,
        loop=loop,
        max_foot_slide=max_foot_slide,
        max_bbox_drift=max_bbox_drift,
        loop_ratio_warn=loop_ratio_warn,
        pre=pre,
        max_colours=max_colours,
        cv_warn=cv_warn,
    )
    for d, info in reg.items():
        if info.get("clipped_px") and any(info["clipped_px"]):
            rep["failures"].append(f"registration: {d} shift {info['shift_px']} clipped {info['clipped_px']} px")
            rep["pass"] = False
    rep = {
        "character": character,
        "animation": clip,
        "anchor_mode": anchor_mode,
        "pass": rep.pop("pass"),
        **rep,
        "off_palette_before": sum(s["off_palette_before"] for v in pre.values() for s in v),
        "registration": reg,
        "params": {
            "raw_dir": str(raw),
            "cell": cell,
            "scale": scale,
            "downscaler": downscaler,
            "thickness": thickness,
            "key_tol": key_tol,
            "despill_px": despill_px,
            "max_colours": max_colours,
            "alpha_threshold": ALPHA_THRESHOLD,
            "cv_warn": cv_warn,
            "palette": str(palette),
            "loop": loop,
            "max_foot_slide": max_foot_slide,
            "max_bbox_drift": max_bbox_drift,
            "loop_ratio_warn": loop_ratio_warn,
        },
    }
    (out / "processed").mkdir(parents=True, exist_ok=True)
    for d, fs in clips.items():
        for i, f in enumerate(fs):
            Image.fromarray(f).save(out / "processed" / f"{d}_{i}.png")
    sheet(clips, cell, frames, stills).save(out / "sheet.png")
    if clips:
        contact(clips, cell, rep, contact_scale).save(out / "contact.png")
    prov = {
        "config": {"path": str(config.resolve()), "sha256": sha256(config), "version": load_config(config)["version"]}
        if config
        else None,
        "inputs": {p.name: sha256(p) for fs in frame_files(raw).values() for p in fs.values()},
        "outputs": {f"{d}_{i}.png": sha256(out / "processed" / f"{d}_{i}.png") for d, fs in clips.items() for i in range(len(fs))}
        | {"sheet.png": sha256(out / "sheet.png")},
        "process_seconds": round(time.perf_counter() - t0, 3),
        "tool_sha256": sha256(Path(__file__)),
    }
    if (raw.parent / "selection.json").exists():
        sel = raw.parent / "selection.json"
        prov["selection_sha256"] = sha256(sel)
        prov["selection_set"] = json.loads(sel.read_text()).get("set")
    rep["provenance"] = prov
    (out / "report.json").write_text(json.dumps(rep, indent=1) + "\n")
    return rep


# ---------------------------------------------------------------- calibration


def ceil_to(x: float | None, places: int) -> float | None:
    return None if x is None else math.ceil(x * 10**places - 1e-9) / 10**places


def calibrate(inputs: Path, out_json: Path, palette: Path = PALETTE) -> dict:
    """Validator metrics on the shipped clips (already final pixels: no pixelize/palette, only registration)."""
    man = json.loads((inputs / "manifest.json").read_text())
    pal = load_palette(palette)
    palset = {tuple(c) for c in pal.tolist()}
    keys = ("foot_slide_max", "bbox_drift_max", "opaque_area_cv", "loop_closure_delta", "loop_closure_ratio")
    res: dict = {"inputs": str(inputs), "palette_sha256": man["palette_sha256"], "characters": {}}
    for name, c in man["characters"].items():
        cell, spec = c["cell"], c["benchmark_clip"]
        clips, reg = {}, {}
        for d in GEN_DIRS:
            fs = [
                np.asarray(Image.open(inputs / name / f"gt_{spec['name']}_{d}_{i}.png").convert("RGBA"))
                for i in range(spec["frames"])
            ]
            for f in fs:  # shipped pixels must pass palette/alpha trivially
                a = f[..., 3]
                assert f.shape[:2] == (cell, cell) and not ((a > 0) & (a < 255)).any(), (name, d)
                assert all(tuple(p) in palset for p in f[a == 255, :3].tolist()), (name, d, "off-palette")
            clips[d], reg[d] = register_clip(fs, cell)
        rep = validate(clips, cell, spec["frames"], GEN_DIRS, pal, loop=spec["loop"])
        res["characters"][name] = {
            "cell": cell,
            "clip": spec,
            "pass_without_slide_drift_limits": rep["pass"],
            "failures": rep["failures"],
            "registration": reg,
            "directions": {
                d: {k: v.get(k) for k in (*keys, "lowest_row", "foot_rows", "contact_frames")}
                for d, v in rep["directions"].items()
            },
            "observed_max": {
                k: max((v[k] for v in rep["directions"].values() if v.get(k) is not None), default=None) for k in keys
            },
        }
    obs = [v["observed_max"] for v in res["characters"].values()]

    def propose(o: dict) -> dict:
        return {
            "max_foot_slide_px": ceil_to(o["foot_slide_max"], 0),
            "max_bbox_drift_px": ceil_to(o["bbox_drift_max"], 0),
            "opaque_area_cv_warn": ceil_to(o["opaque_area_cv"], 2),
            "loop_closure_ratio_warn": ceil_to(o["loop_closure_ratio"], 2),
            "loop_closure_delta_observed": ceil_to(o["loop_closure_delta"], 2),
        }

    res["proposed_thresholds"] = {
        "all": propose({k: max(o[k] for o in obs if o[k] is not None) for k in keys}),
        **{n: propose(v["observed_max"]) for n, v in res["characters"].items()},
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(res, indent=1) + "\n")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("raw", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument("--character", required=True)
    r.add_argument("--clip", required=True)
    r.add_argument("--cell", type=int, required=True)
    r.add_argument("--frames", type=int, required=True)
    r.add_argument("--scale", type=int, default=4)
    r.add_argument("--palette", type=Path, default=PALETTE)
    r.add_argument("--config", type=Path, help="frozen processing yaml (version: processing-v1); flags below override it")
    r.add_argument("--anchor-mode", choices=["foot", "bbox_bottom"])
    r.add_argument("--directions", default=",".join(GEN_DIRS))
    r.add_argument("--downscaler", help="comma list compares downscalers into out/<name>/ (default k_centroid)")
    r.add_argument("--thickness", type=int, help="PixelOE outline expansion (default 2, as the Phase 0 node)")
    r.add_argument("--key-tol", type=float, help="default 48")
    r.add_argument("--despill", type=int, help="magenta despill band in HD px around the matte edge (default 0)")
    r.add_argument("--max-colours", type=int, help=f"colour cap per cell (default {MAX_COLOURS})")
    r.add_argument("--no-loop", action="store_true")
    r.add_argument("--stills", type=Path, help="dir with still_<dir>.png for sheet column 0")
    r.add_argument("--max-foot-slide", type=float)
    r.add_argument("--max-bbox-drift", type=int)
    r.add_argument("--loop-ratio-warn", type=float)
    r.add_argument("--contact-scale", type=int, default=8)
    c = sub.add_parser("calibrate")
    c.add_argument("inputs", type=Path)
    c.add_argument("out_json", type=Path)
    c.add_argument("--palette", type=Path, default=PALETTE)
    a = ap.parse_args()
    if a.cmd == "calibrate":
        print(json.dumps(calibrate(a.inputs, a.out_json, a.palette)["proposed_thresholds"], indent=1))
        return
    cfg = load_config(a.config) if a.config else {}
    ch = cfg.get("characters", {}).get(a.character, {})

    def pick(flag, *cfg_vals, default=None):  # explicit flag > config (character, then global) > legacy default
        return next((v for v in (flag, *cfg_vals) if v is not None), default)

    methods = pick(a.downscaler, cfg.get("downscaler"), default="k_centroid").split(",")
    summary = {}
    for m in methods:
        out = a.out if len(methods) == 1 else a.out / m
        rep = run(
            a.raw,
            out,
            character=a.character,
            clip=a.clip,
            cell=a.cell,
            frames=a.frames,
            scale=a.scale,
            palette=a.palette,
            anchor_mode=pick(a.anchor_mode, ch.get("anchor_mode"), default="foot"),
            directions=a.directions.split(","),
            downscaler=m,
            thickness=pick(a.thickness, cfg.get("thickness"), default=2),
            key_tol=pick(a.key_tol, cfg.get("key_tol"), default=48),
            despill_px=pick(a.despill, cfg.get("despill_px"), default=0),
            loop=not a.no_loop,
            stills=a.stills,
            max_foot_slide=pick(a.max_foot_slide, ch.get("max_foot_slide_px")),
            max_bbox_drift=pick(a.max_bbox_drift, ch.get("max_bbox_drift_px")),
            loop_ratio_warn=pick(a.loop_ratio_warn, ch.get("loop_ratio_warn"), cfg.get("loop_ratio_warn")),
            contact_scale=a.contact_scale,
            max_colours=pick(a.max_colours, cfg.get("max_colours"), default=MAX_COLOURS),
            cv_warn=pick(None, cfg.get("opaque_area_cv_warn"), default=CV_WARN),
            config=a.config,
        )
        summary[m] = {
            "pass": rep["pass"],
            "failures": len(rep["failures"]),
            "off_palette_before": rep["off_palette_before"],
            **rep["cycle"],
        }
    if len(methods) > 1:
        (a.out / "compare.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
