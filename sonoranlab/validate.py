"""The validator (docs/08): the technical gate on registered frames. It reports and never fixes.

Metric definitions are processing-v1's (benchmark/tools/process.py, calibrated on shipped clips by DYL-71) and stay
identical to it (ADR-0008 H0). Frame coordinates are after registration; bboxes are inclusive.
  foot_y        lowest opaque row of a frame.
  contact frame foot_y within 1 px of the clip's lowest row (per direction).
  foot_x        centre of the longest opaque run on the frame's foot_y row ((start + end + 1) / 2, first on ties).
  foot_slide    contact frames only, anchor_mode foot: |foot_x - anchor_x|. Not computed for bbox_bottom (ADR-0008
                F2): the hover warnings below replace it. `foot_slide` is the one place this depends on anchor_mode.
  bbox_drift    max |delta| over the four bbox edges vs the previous frame (frame 0 vs the last for loops: the seam).
  opaque_area_cv  population std / mean of opaque pixel counts across a direction's clip.
  loop_closure_delta  share of the last+first opaque union whose alpha or colour differs between last and first.
  loop_closure_ratio  loop_closure_delta / mean delta of the in-clip adjacent pairs (~1 = seamless loop).
Diagnostics and hover metrics (validator study, benchmark/results/2026-10-19-phase2/validator-study.md):
  core          the silhouette after a binary opening with a 5x5 square: parts thinner than 5 px are removed.
  protrusion    how far a bbox edge sticks out past the core bbox edge (a thin part defines that edge).
  foot_body     contact frames, anchor_mode foot: |foot_x - core bbox centre x| (foot vs the body, not the anchor).
  hover x-drift range of the core bbox centre x over a direction's frames (warn above hover_x_drift_warn_px).
  hover bob     range of the opaque centroid y over a direction's frames (warn below hover_min_bob_px).
Fall mode (`validator_mode: fall`, death clips only, ADR-0008 fall addendum). A falling body legitimately moves its
top and sides and its feet leave the anchor, so: foot_slide is gated on frame 0 only (the standing still pose);
bbox_drift is gated on the bottom edge and the core centre x only (`fall_drift` = max of the two, same limit), the
top/left/right edges are reported as warnings. Three gates are added per direction:
  fall_final    the last frame lies inside the cell padding and registration clipped none of its pixels.
  fall_hold     silhouette_delta(second-to-last, last) <= FALL_HOLD_MAX_DELTA: the held last frame is a settled pose.
  static_clip   not every frame equals frame 0 (the game's --pixel-check refuses a repeated-still death row).
Every other rule, limit and warning is unchanged; loop rules never run for loop=False.
Diagnostics are reported, never gated. `pass` is True iff `failures` is empty.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from functools import reduce
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

LIMIT_KEYS = ("max_foot_slide_px", "max_bbox_drift_px", "opaque_area_cv_warn", "loop_closure_ratio_warn")
HOVER_KEYS = ("hover_x_drift_warn_px", "hover_min_bob_px")
ANCHOR_MODES = ("foot", "bbox_bottom")
EDGES = ("left", "top", "right", "bottom")
CORE_RADIUS = 2  # 5x5 opening, the study's r = 2
FRAME_RE = re.compile(r"^([a-z-]+)_(0|[1-9]\d*)\.png$")
SCOPE = "technical; does not judge readability or style - a 1x human review is still required"
CHAIN_NOTE = (
    "frames[].pre and registration record what the processing chain changed (provenance, not verdicts); "
    "they are not proof that the raw poses were consistent"
)
VALIDATOR_MODES = ("fall",)
# fall_hold: at most 2% of the last pair's opaque union may change SHAPE (alpha). Colour is ignored: Track E frames of a
# body at rest re-quantise differently (approved gun/thief idle pairs differ in 24-38% of pixels by colour but at most
# 1.1% by silhouette). Owner 2026-10-29: "Silhouette <= 2% (Recommended)", about twice the approved idles.
FALL_HOLD_MAX_DELTA = 0.02
FALL_NOTE = (
    "validator_mode fall: foot_slide gated on frame 0 only; bbox_drift gated on bottom edge and core centre x only "
    "(top/left/right reported); fall_final, fall_hold and static_clip gated (ADR-0008 fall addendum)"
)
F2_NOTE = "foot_slide: not computed or gated for anchor_mode bbox_bottom (ADR-0008 F2); hover warnings instead"


def frame_digest(frame: np.ndarray) -> str:
    """Bind the canvas and RGBA pixels independently of their PNG encoding."""
    h, w, c = frame.shape
    return hashlib.sha256(f"{h}x{w}x{c}:".encode() + frame.tobytes()).hexdigest()


def bbox(mask: np.ndarray) -> list[int] | None:
    """[x0, y0, x1, y1] (inclusive) of a boolean mask, or None if empty."""
    ys, xs = np.nonzero(mask)
    return None if len(xs) == 0 else [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]


def border(mask: np.ndarray) -> np.ndarray:
    """Pixels of `mask` with a 4-neighbour outside it (or outside the frame)."""
    p = np.pad(mask, 1, constant_values=False)
    return mask & ~(p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:])


def opening(mask: np.ndarray, r: int = CORE_RADIUS) -> np.ndarray:
    """Binary opening with a (2r+1)^2 square (the study's definition: erosion pads with True, dilation with False)."""
    h, w = mask.shape
    k = 2 * r + 1

    def sweep(x: np.ndarray, fill: bool, op: Any) -> np.ndarray:
        p = np.pad(x, r, constant_values=fill)
        return reduce(op, (p[dy : dy + h, dx : dx + w] for dy in range(k) for dx in range(k)))

    return sweep(sweep(mask, True, np.logical_and), False, np.logical_or)


def foot_x(f: np.ndarray, row: int) -> float:
    xs = np.nonzero(f[row, :, 3])[0]
    runs = np.split(xs, np.nonzero(np.diff(xs) > 1)[0] + 1)
    r = max(runs, key=len)  # first longest run on ties
    return (int(r[0]) + int(r[-1]) + 1) / 2


def foot_slide(anchor_mode: str, fx: float, anchor_x: int) -> float | None:
    """The gated foot measure of one contact frame. foot: |foot_x - anchor_x| (processing-v1, ADR-0008 H0).
    bbox_bottom: None. The lowest row of a floating subject is a tassel tip, not a foot (ADR-0008 F2)."""
    if anchor_mode == "foot":
        return abs(fx - anchor_x)
    if anchor_mode == "bbox_bottom":
        return None
    raise ValueError(f"anchor_mode {anchor_mode!r}: the validator knows {', '.join(ANCHOR_MODES)}")


def frame_delta(a: np.ndarray, b: np.ndarray) -> float:
    union = (a[..., 3] > 0) | (b[..., 3] > 0)
    diff = (a != b).any(-1) & union
    return float(diff.sum() / max(1, union.sum()))


def silhouette_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Share of the two frames' opaque union whose opacity differs (shape change; colour ignored)."""
    ma, mb = a[..., 3] > 0, b[..., 3] > 0
    return float((ma ^ mb).sum() / max(1, (ma | mb).sum()))


def _protrusion(b: list[int], cb: list[int]) -> list[int]:
    return [cb[0] - b[0], cb[1] - b[1], b[2] - cb[2], b[3] - cb[3]]


def _rng(vals: list[float]) -> float:
    return round(max(vals) - min(vals), 2)


def validate(
    clips: dict[str, list[np.ndarray]],
    *,
    cell: tuple[int, int],
    frames: int,
    directions: Sequence[str],
    palette: np.ndarray,
    foot_y: int,
    anchor_x: int,
    padding: int,
    limits: Any,
    max_colours: int,
    loop: bool,
    anchor_mode: str = "foot",
    validator_mode: str | None = None,
    gate_bbox_drift: bool = True,
    fringe_tolerance: int = 0,
    mirror: Mapping[str, str] | None = None,
    stills: dict[str, np.ndarray] | None = None,
    accessory: Mapping[str, Any] | None = None,
    pre: Mapping[str, list[dict]] | None = None,
    registration: Mapping[str, dict] | None = None,
    limits_source: str | None = None,
) -> dict:
    """docs/08 report for registered clips {direction: [RGBA (h, w, 4) uint8 frames]}, frames indexed in `clips`
    order (pass the profile's sheet order). `limits` has LIMIT_KEYS (and HOVER_KEYS for bbox_bottom) as attributes;
    none may be None. `directions` are the rows the clip must have (`frames` each); every row in `clips` is checked.
    `mirror` {mirrored: source}: when both rows are present the mirrored frames must equal the source flipped.
    `stills` are final-resolution cells; only the shared cell rules apply (no clip/anchor rules).
    `accessory` {"colours": [[r, g, b], ...], "min_pixels": 1, "min_share": 0.9}: warn when fewer frames show it.
    `pre` (per frame) and `registration` (per direction) are the chain's records: copied into the report, and a
    registration with clipped_px is a failure. `validator_mode` "fall" applies the fall rules (module docstring)."""
    if anchor_mode not in ANCHOR_MODES:
        raise ValueError(f"anchor_mode {anchor_mode!r}: the validator knows {', '.join(ANCHOR_MODES)}")
    if validator_mode not in (None, *VALIDATOR_MODES) or (validator_mode == "fall" and loop):
        raise ValueError(f"validator_mode {validator_mode!r}: expected None or fall (fall needs loop=False)")
    fall = validator_mode == "fall"
    hover = anchor_mode == "bbox_bottom"
    keys = LIMIT_KEYS + (HOVER_KEYS if hover else ())
    lim = {k: getattr(limits, k, None) for k in keys}
    if unset := [k for k, v in lim.items() if v is None]:
        raise ValueError(f"limits {unset} unset: every validator limit is required (no 'measured, not gated')")
    max_slide, max_drift = lim["max_foot_slide_px"], lim["max_bbox_drift_px"]
    w, h = cell
    palset = {tuple(c) for c in np.asarray(palette).tolist()}
    acc = None
    if accessory:
        acc = {tuple(c) for c in accessory["colours"]}, accessory.get("min_pixels", 1), accessory.get("min_share", 0.9)

    def check_cell(f: np.ndarray, b: list[int] | None, tag: str) -> dict:
        a = f[..., 3]
        opaque = a == 255
        px = [tuple(c) for c in f[opaque, :3].tolist()]
        fringe = sum(tuple(c) not in palset for c in f[border(opaque), :3].tolist())
        edges = []
        if b:
            hits = (b[0] < padding, b[1] < padding, b[2] > w - 1 - padding, b[3] > h - 1 - padding)
            edges = [n for n, hit in zip(EDGES, hits, strict=True) if hit]
        r: dict = {
            "ok": True,
            "issues": [],
            "canvas": list(f.shape[1::-1]),
            "bbox": b,
            "opaque_pixels": int(opaque.sum()),
            "colours": len(set(px)),
            "off_palette_after": sum(c not in palset for c in px),
            "fringe_pixels": fringe,
            "semi_alpha_pixels": int(((a > 0) & (a < 255)).sum()),
            "touches_edge": edges or False,
        }
        issues = r["issues"]
        if r["canvas"] != [w, h]:
            issues.append(f"canvas: {tag} is {r['canvas'][0]}x{r['canvas'][1]}, expected {w}x{h}")
        if b is None:
            issues.append(f"empty: {tag} has no opaque pixels")
        if r["off_palette_after"]:
            issues.append(f"palette: {tag} has {r['off_palette_after']} off-palette pixels")
        if fringe > fringe_tolerance:
            issues.append(
                f"fringe: {tag} has {fringe} off-palette silhouette-border pixels, tolerance {fringe_tolerance}"
            )
        if r["semi_alpha_pixels"]:
            issues.append(f"semi_alpha: {tag} has {r['semi_alpha_pixels']} semi-transparent pixels")
        if r["colours"] > max_colours:
            issues.append(f"colours: {tag} uses {r['colours']} colours, limit {max_colours}")
        if edges:
            issues.append(f"touches_edge: {tag} {'/'.join(edges)} edge inside padding {padding}, clipping")
        r["ok"] = not issues
        return r

    failures: list[str] = []
    warnings: list[str] = []
    diagnostics: list[dict] = []
    rows: list[dict] = []
    cycles: dict[str, dict] = {}
    still_rows: list[dict] = []
    digests = {}
    for d, f in (stills or {}).items():
        if d not in set(directions) | set(mirror or {}):
            raise ValueError(f"still direction {d!r} is not in the profile direction order")
        r = {"direction": d, **check_cell(f, bbox(f[..., 3] > 0), f"still {d}")}
        still_rows.append(r)
        failures += r["issues"]
        digests[f"still_{d}"] = frame_digest(f)
    for d in directions:
        if (n := len(clips.get(d, []))) != frames:
            failures.append(f"frame_count: {d} has {n} frames, expected {frames}")
    for d, fs in clips.items():
        if not fs:
            continue
        masks = [f[..., 3] > 0 for f in fs]
        boxes = [bbox(m) for m in masks]
        cores = [None if b is None else bbox(opening(m)) or b for m, b in zip(masks, boxes, strict=True)]
        lows = [b[3] if b else None for b in boxes]
        clip_low = max((y for y in lows if y is not None), default=None)
        src = clips.get(mirror.get(d, "")) if mirror else None
        clipped = ((registration or {}).get(d) or {}).get("clipped_px") or []
        recs = []
        for i, (f, b, cb) in enumerate(zip(fs, boxes, cores, strict=True)):
            digests[f"{d}_{i}"] = frame_digest(f)
            idx = len(rows)
            tag = f"frame {idx} ({d}/{i})"
            contact = b is not None and lows[i] >= clip_low - 1
            fx = foot_x(f, lows[i]) if b else None
            # fall mode gates frame 0 (the standing still pose) whatever the later frames' foot line does
            slide = foot_slide(anchor_mode, fx, anchor_x) if contact or (fall and i == 0 and fx is not None) else None
            body = abs(fx - (cb[0] + cb[2] + 1) / 2) if slide is not None else None
            prev = boxes[i - 1] if (i > 0 or (loop and len(fs) > 1)) else None
            drift = max(abs(p - q) for p, q in zip(b, prev, strict=True)) if b and prev else None
            r: dict = {
                "index": idx,
                "direction": d,
                "frame": i,
                **check_cell(f, b, tag),
                "foot_y": lows[i],
                "contact": contact,
                "foot_x": fx,
                "foot_slide": slide,
                "foot_body": body,
                "bbox_drift": drift,
            }
            if acc:
                r["accessory_pixels"] = sum(tuple(c) in acc[0] for c in f[f[..., 3] == 255, :3].tolist())
            r["pre"] = dict(pre[d][i]) if pre and d in pre and i < len(pre[d]) else None
            issues = r["issues"]
            if fall:
                prev_cb = cores[i - 1] if prev and cb else None
                r["fall_drift"] = (
                    max(abs(b[3] - prev[3]), abs((cb[0] + cb[2]) - (prev_cb[0] + prev_cb[2])) / 2) if prev_cb else None
                )
            if slide is not None and slide > max_slide and (not fall or i == 0):
                issues.append(
                    f"foot_slide: {tag} foot {slide:g}px from anchor x {anchor_x} ({body:g}px from the body core), "
                    f"limit {max_slide:g}"
                )
                diagnostics.append(
                    {"index": idx, "direction": d, "frame": i, "rule": "foot_slide", "value": slide, "limit": max_slide}
                    | {"gated": True, "foot_x": fx, "anchor_x": anchor_x, "foot_body": body}
                )
            if fall and drift is not None:
                deltas = dict(zip(EDGES, (abs(p - q) for p, q in zip(b, prev, strict=True)), strict=True))
                fd = r["fall_drift"]
                if fd > max_drift:
                    issues.append(
                        f"bbox_drift: {tag} bottom edge or body core centre moved {fd:g}px (fall), limit {max_drift:g}"
                    )
                    diagnostics.append(
                        {"index": idx, "direction": d, "frame": i, "rule": "bbox_drift", "value": fd}
                        | {"limit": max_drift, "gated": True, "edge": "bottom/core_centre_x", "edge_deltas": deltas}
                        | {"core_delta": abs((cb[0] + cb[2]) - (prev_cb[0] + prev_cb[2])) / 2, "seam": False}
                    )
                if over := [e for e in ("left", "top", "right") if deltas[e] > max_drift]:
                    warnings.append(
                        f"bbox_drift: {tag} {'/'.join(over)} edge moved {max(deltas[e] for e in over)}px, "
                        f"limit {max_drift:g} (not gated in fall mode)"
                    )
            elif drift is not None and drift > max_drift:
                deltas = [abs(p - q) for p, q in zip(b, prev, strict=True)]
                e = int(np.argmax(deltas))
                pcb = cores[i - 1]
                core_d = abs(cb[e] - pcb[e])
                seam = i == 0
                msg = (
                    f"bbox_drift: {tag} bbox moved {drift}px ({EDGES[e]} edge; core {core_d}px"
                    f"{'; loop seam' if seam else ''}), limit {max_drift:g}"
                )
                (issues if gate_bbox_drift else warnings).append(msg + ("" if gate_bbox_drift else " (not gated)"))
                diagnostics.append(
                    {"index": idx, "direction": d, "frame": i, "rule": "bbox_drift", "value": drift, "limit": max_drift}
                    | {"gated": gate_bbox_drift, "edge": EDGES[e], "edge_deltas": dict(zip(EDGES, deltas, strict=True))}
                    | {"core_delta": core_d, "protrusion": [_protrusion(prev, pcb)[e], _protrusion(b, cb)[e]]}
                    | {"seam": seam}
                )
            if i < len(clipped) and clipped[i]:
                shift = registration[d].get("shift_px")
                issues.append(f"registration: {tag} lost {clipped[i]} opaque px off the cell (clip shift {shift})")
            if src is not None:
                m = src[i][:, ::-1] if i < len(src) else None
                if m is None:
                    issues.append(f"direction_mirror: {tag} has no {mirror[d]}/{i} to mirror")
                elif m.shape != f.shape:
                    issues.append(f"direction_mirror: {tag} is not the size of {mirror[d]}/{i}")
                elif (m != f).any():
                    n = int((m != f).any(-1).sum())
                    issues.append(f"direction_mirror: {tag} differs from {mirror[d]}/{i} mirrored in {n} px")
            r["ok"] = not issues
            failures += issues
            recs.append(r)
            rows.append(r)
        areas = np.array([r["opaque_pixels"] for r in recs], np.float64)
        cyc: dict = {
            "lowest_row": clip_low,
            "lowest_row_ok": clip_low == foot_y,
            "foot_rows": lows,
            "contact_frames": [r["frame"] for r in recs if r["contact"]],
            "foot_slide_max": max((r["foot_slide"] for r in recs if r["foot_slide"] is not None), default=None),
            "bbox_drift_max": max((r["bbox_drift"] for r in recs if r["bbox_drift"] is not None), default=None),
            "opaque_area_cv": float(areas.std() / areas.mean()) if areas.mean() else None,
        }
        same = all(f.shape == fs[0].shape for f in fs)
        if loop and len(fs) > 1 and same:
            adj = float(np.mean([frame_delta(fs[i], fs[i + 1]) for i in range(len(fs) - 1)]))
            cyc["loop_closure_delta"] = frame_delta(fs[-1], fs[0])
            cyc["loop_closure_ratio"] = cyc["loop_closure_delta"] / adj if adj else None
        if hover and all(b is not None for b in boxes):
            cyc["hover_x_drift_px"] = _rng([(cb[0] + cb[2] + 1) / 2 for cb in cores])
            cyc["hover_bob_px"] = _rng([round(float(np.nonzero(m)[0].mean()), 2) for m in masks])
        if not cyc["lowest_row_ok"]:
            failures.append(f"foot_line: {d} clip lowest opaque row {clip_low}, expected {foot_y}")
        if fall:
            failures += _fall_gates(d, fs, boxes, clipped, cyc, recs, w, h, padding)
        cv, cv_lim = cyc["opaque_area_cv"], lim["opaque_area_cv_warn"]
        if cv is not None and cv > cv_lim:
            warnings.append(f"opaque_area_cv: {d} {cv:.3f} > {cv_lim:g}; can mean a lost accessory")
        lr, lr_lim = cyc.get("loop_closure_ratio"), lim["loop_closure_ratio_warn"]
        if lr is not None and lr > lr_lim:
            warnings.append(
                f"loop_closure_ratio: {d} {lr:.2f} > {lr_lim:g}; last->first jumps more than the clip's steps"
            )
        if (x := cyc.get("hover_x_drift_px")) is not None and x > lim["hover_x_drift_warn_px"]:
            warnings.append(
                f"hover_x_drift: {d} body core centre moves {x:g}px across the loop > {lim['hover_x_drift_warn_px']:g}"
            )
        if (y := cyc.get("hover_bob_px")) is not None and y < lim["hover_min_bob_px"]:
            warnings.append(
                f"hover_bob: {d} centroid bobs {y:g}px < {lim['hover_min_bob_px']:g}; the hover barely moves"
            )
        cycles[d] = cyc
    if mirror:
        for d, s in mirror.items():
            if d in clips and s in clips and len(clips[d]) < len(clips[s]):
                failures.append(f"direction_mirror: {d} has {len(clips[d])} frames, {s} has {len(clips[s])}")
    if acc and rows:
        missing = [r for r in rows if r["accessory_pixels"] < acc[1]]
        share = 1 - len(missing) / len(rows)
        if share < acc[2]:
            names = ", ".join(f"{r['index']} ({r['direction']}/{r['frame']})" for r in missing)
            warnings.append(
                f"accessory_presence: in {len(rows) - len(missing)}/{len(rows)} frames ({share:.0%}) < "
                f"{acc[2]:.0%}; missing in frames {names}"
            )

    def cmax(k: str) -> float | None:
        return max((c[k] for c in cycles.values() if c.get(k) is not None), default=None)

    report = {
        "pass": not failures,
        "scope": SCOPE,
        "anchor_mode": anchor_mode,
        "limits": {**lim, "source": limits_source, "bbox_drift_gated": gate_bbox_drift},
        "notes": ([F2_NOTE] if hover else []) + ([FALL_NOTE] if fall else []),
        "frame_count": {
            "expected": frames * len(directions),
            "actual": sum(len(clips.get(d, [])) for d in directions),
            "ok": all(len(clips.get(d, [])) == frames for d in directions),
        },
        "frames": rows,
        "stills": still_rows,
        "digests": digests,
        "cycle": {
            k: cmax(k)
            for k in ("foot_slide_max", "bbox_drift_max", "opaque_area_cv", "loop_closure_delta", "loop_closure_ratio")
        },
        "directions": cycles,
        "failures": failures,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "chain": {
            "note": CHAIN_NOTE,
            "off_palette_before": sum(s.get("off_palette_before", 0) for v in pre.values() for s in v) if pre else None,
        },
        "registration": {d: dict(v) for d, v in (registration or {}).items()},
    }
    if fall:  # default-mode reports keep their exact keys
        report["validator_mode"] = "fall"
        report["limits"]["fall_hold_max_delta"] = FALL_HOLD_MAX_DELTA
    assert report["pass"] is (not report["failures"]), "a report with failures can never pass"
    return report


def _fall_gates(d, fs, boxes, clipped, cyc, recs, w, h, padding) -> list[str]:
    """fall_final, fall_hold and static_clip for one direction (module docstring); records metrics in `cyc`."""
    out = []
    b, lost = boxes[-1], clipped[len(fs) - 1] if len(clipped) >= len(fs) else 0
    inside = b is not None and b[0] >= padding and b[1] >= padding and b[2] <= w - 1 - padding
    inside = inside and b[3] <= h - 1 - padding
    if not inside or lost:
        out.append(f"fall_final: {d} last frame bbox {b} must lie inside padding {padding} with no clipped px ({lost})")
    same = all(f.shape == fs[0].shape for f in fs)
    hold = silhouette_delta(fs[-2], fs[-1]) if len(fs) > 1 and same else None
    if hold is not None and hold > FALL_HOLD_MAX_DELTA:
        out.append(f"fall_hold: {d} last two frames' silhouettes differ in {hold:.3f} > {FALL_HOLD_MAX_DELTA:g}")
    static = same and all(np.array_equal(f, fs[0]) for f in fs[1:])
    if static:
        out.append(f"static_clip: {d} is a repeated still (every frame equals frame 0)")
    gated = [r["fall_drift"] for r in recs if r.get("fall_drift") is not None]
    slide0 = recs[0]["foot_slide"] if recs else None
    cyc["fall"] = {"drift_max": max(gated, default=None), "foot_slide_frame0": slide0, "hold_delta": hold}
    cyc["fall"] |= {"final_inside": inside and not lost, "static": static}
    return out


def read_stills(stills_dir: Path, order: Sequence[str]) -> tuple[dict[str, np.ndarray], list[Path]]:
    """Read final-resolution still_<direction>.png cells, refusing unknown directions."""
    stills = {}
    paths = sorted(Path(stills_dir).glob("still_*.png"))
    for p in paths:
        d = p.stem.removeprefix("still_")
        if d not in order:
            raise ValueError(f"{p}: still direction {d!r} is not in the profile direction order")
        with Image.open(p) as im:
            stills[d] = np.asarray(im.convert("RGBA")).copy()
    return stills, paths


def read_frames(frames_dir: Path, order: Sequence[str]) -> tuple[dict[str, list[np.ndarray]], list[Path]]:
    """`<direction>_<i>.png` cells in `frames_dir` -> ({direction: RGBA frames} in `order`, the files read). Other
    names are ignored; refuses a direction outside `order`, numbering with gaps, and a dir without frames."""
    found: dict[str, dict[int, Path]] = {}
    for p in sorted(Path(frames_dir).glob("*.png")):
        if m := FRAME_RE.match(p.name):
            if m[1] not in order:
                raise ValueError(f"{p}: direction {m[1]!r} is not one of {', '.join(order)}")
            found.setdefault(m[1], {})[int(m[2])] = p
    if not found:
        raise ValueError(f"{frames_dir}: no <direction>_<i>.png frames")
    for d, fs in found.items():
        if sorted(fs) != list(range(len(fs))):
            raise ValueError(f"{frames_dir}: {d} frames are numbered {sorted(fs)}, expected 0..{len(fs) - 1}")
    paths = [found[d][i] for d in order if d in found for i in range(len(found[d]))]
    clips = {
        d: [np.asarray(Image.open(found[d][i]).convert("RGBA")) for i in range(len(found[d]))]
        for d in order
        if d in found
    }
    return clips, paths
