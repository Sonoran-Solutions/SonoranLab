"""`sonoranlab refprep`: deterministic conversion of owner-directed HD turnaround art into CANDIDATE stills (ADR-0011).

Pixel math only, no model. Per source image (the turnaround, or a `--view DIRECTION=IMG` override): chroma matte with
the profile's key_tol + despill (alpha.py) -> segment the WHOLE image into figures (one per grid cell, by centroid;
detached parts join the nearest figure within a bound) and crop each from its own pixels ->
ONE common scale for the turnaround views (median of target height / view height, target = height_m x density;
an override view from another canvas is normalised to the target height on its own) ->
Lanczos to gen_scale x the target size -> the processing-v1 chain (pipeline.process_frame: PixelOE, palette lock,
alpha threshold, fringe clean, colour cap) -> foot registration into the cell (register.register_clip). Views are
never mirrored and a missing view is never synthesised. Outputs carry no approval: the owner approves exact hashes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from sonoranlab import classreq
from sonoranlab.alpha import despill, extract_alpha
from sonoranlab.profile import load_palette, load_profile, sha256_file

MIN_COMPONENT_SHARE = 0.005  # drop matte islands smaller than 0.5% of the largest component (key noise, specks)
HEIGHT_DEVIATION_WARN = 0.04  # a view whose height differs > 4% from the target after scaling
RESIDUAL_KEY_FACTOR = 2  # opaque pixels within this x key_tol of the key after matting are reported as residual key
FIGURE_SHARE = 0.1  # components >= 10% of the largest are figures (exactly one per grid cell / override image)
ATTACH_FRACTION = 0.05  # a detached part joins the nearest figure within 5% of the median figure height (>= 8 HD px)
KEY_TINT = 24  # HD pixel is key-tinted when min(R, B) - G > this after despill (magenta spill on metal)
MARGIN = 2  # transparent final px around a view while it is processed
MAX_EDGE = 16384


class RefprepError(ValueError):
    """Usage or input error; nothing has been written."""


def components(mask: np.ndarray) -> list[dict]:
    """8-connected components of a boolean mask (row runs + union-find): [{runs, pixels, bbox, centroid [x, y]}],
    largest first."""
    h, _ = mask.shape
    parent: list[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs: list[tuple[int, int, int]] = []  # (row, start, end exclusive)
    prev: list[int] = []
    for y in range(h):
        row = np.concatenate(([0], mask[y].astype(np.int8), [0]))
        edges = np.flatnonzero(np.diff(row))
        cur = []
        for s, e in zip(edges[::2], edges[1::2], strict=True):
            i = len(runs)
            runs.append((y, int(s), int(e)))
            parent.append(i)
            for j in prev:
                _, ps, pe = runs[j]
                if ps <= e and s <= pe:  # 8-connectivity: overlap including the diagonal neighbour
                    a, b = find(i), find(j)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
            cur.append(i)
        prev = cur
    comps: dict[int, dict] = {}
    for i, (y, s, e) in enumerate(runs):
        c = comps.setdefault(find(i), {"runs": [], "pixels": 0, "bbox": [s, y, e - 1, y], "sx": 0.0, "sy": 0.0})
        c["runs"].append((y, s, e))
        c["pixels"] += e - s
        c["sx"] += (s + e - 1) / 2 * (e - s)
        c["sy"] += y * (e - s)
        b = c["bbox"]
        c["bbox"] = [min(b[0], s), min(b[1], y), max(b[2], e - 1), max(b[3], y)]
    for c in comps.values():
        c["centroid"] = [c.pop("sx") / c["pixels"], c.pop("sy") / c["pixels"]]
    return sorted(comps.values(), key=lambda c: (-c["pixels"], c["bbox"]))


def _hex(c: str) -> tuple[int, int, int]:
    if not (isinstance(c, str) and len(c) == 7 and c[0] == "#"):
        raise RefprepError(f"bg key {c!r} is not #rrggbb")
    return tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))


def _open(path: Path) -> Image.Image:
    with Image.open(path) as im:
        if max(im.size) > MAX_EDGE:
            raise RefprepError(f"{path}: {im.size[0]}x{im.size[1]} exceeds {MAX_EDGE} px per edge")
        return im.convert("RGBA")


def _mask(runs: list[tuple[int, int, int]], shape: tuple[int, int]) -> np.ndarray:
    m = np.zeros(shape, bool)
    for y, a, b in runs:
        m[y, a:b] = True
    return m


def _wave(seeds: np.ndarray, steps: int) -> tuple[np.ndarray, np.ndarray]:
    """Multi-source Chebyshev growth of a label image for `steps` px: (labels, distance; -1 where unreached).
    Ties go to the first neighbour in a fixed order, so the result is deterministic."""
    lab = seeds.copy()
    dist = np.where(lab > 0, 0, -1)
    h, w = lab.shape
    shifts = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
    for step in range(1, steps + 1):
        p = np.pad(lab, 1)
        new = np.zeros_like(lab)
        for dy, dx in shifts:
            nb = p[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
            new = np.where(new == 0, nb, new)
        grow = (lab == 0) & (new > 0)
        if not grow.any():
            break
        lab[grow], dist[grow] = new[grow], step
    return lab, dist


def _segment(
    im: Image.Image, cells: dict[str, tuple[int, int, int, int]], key, key_tol: float, despill_px: int, what: str
) -> tuple[dict[str, dict], dict]:
    """Matte one source image and split it into figures over the WHOLE image (no grid cropping, so a prop crossing
    a grid line stays with its figure). Figures are the components >= FIGURE_SHARE of the largest; each wanted cell
    must contain exactly one figure centroid. Specks < MIN_COMPONENT_SHARE are dropped; other detached parts join
    the nearest figure by pixel distance within a bound, else they are dropped and reported. Each figure is cropped
    from its own pixel set."""
    rgb, alpha = extract_alpha(im, key, key_tol)
    rgb, _ = despill(rgb, alpha, despill_px, key)
    mask = alpha >= 128
    comps = components(mask)
    if not comps:
        raise RefprepError(f"{what}: no opaque pixels after the chroma key; views are never synthesised")
    biggest = comps[0]["pixels"]
    figs = [c for c in comps if c["pixels"] >= FIGURE_SHARE * biggest]
    owner: dict[str, dict] = {}
    for d, (x0, y0, x1, y1) in cells.items():
        inside = [c for c in figs if x0 <= c["centroid"][0] < x1 and y0 <= c["centroid"][1] < y1]
        if not inside:
            raise RefprepError(f"view {d}: no figure in its {what} cell (blank?); views are never synthesised")
        if len(inside) > 1:
            raise RefprepError(
                f"view {d}: {len(inside)} figure-sized components in its {what} cell (ambiguous; sizes "
                f"{[c['pixels'] for c in inside]} HD px); prepare that view separately with --view"
            )
        owner[d] = inside[0]
    seeds = np.zeros(mask.shape, np.int32)
    for k, c in enumerate(figs, 1):
        for y, a, b in c["runs"]:
            seeds[y, a:b] = k
    bound = max(8, round(ATTACH_FRACTION * float(np.median([c["bbox"][3] - c["bbox"][1] + 1 for c in figs]))))
    lab, dist = _wave(seeds, bound)
    attached: dict[int, list[dict]] = {k: [] for k in range(1, len(figs) + 1)}
    dropped: list[dict] = []
    fig_ids = {id(c) for c in figs}
    for c in comps:
        if id(c) in fig_ids:
            continue
        info = {"pixels": c["pixels"], "bbox_hd": c["bbox"]}
        if c["pixels"] < MIN_COMPONENT_SHARE * biggest:
            dropped.append(info | {"reason": "speck"})
            continue
        best = None
        for y, a, b in c["runs"]:
            row = dist[y, a:b]
            ok = np.flatnonzero(row >= 0)
            if len(ok):
                i = ok[np.argmin(row[ok])]
                if best is None or row[i] < best[0]:
                    best = (int(row[i]), int(lab[y, a + i]))
        if best is None:
            dropped.append(info | {"reason": f"farther than {bound} HD px from every figure"})
        else:
            attached[best[1]].append(info | {"distance_hd": best[0], "c": c})
    views = {}
    for d, c in owner.items():
        k = next(i for i, f in enumerate(figs, 1) if f is c)
        runs = c["runs"] + [r for a in attached[k] for r in a["c"]["runs"]]
        m = _mask(runs, mask.shape)
        ys, xs = np.nonzero(m)
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
        crop = np.zeros((y1 - y0 + 1, x1 - x0 + 1, 4), np.uint8)
        mc = m[y0 : y1 + 1, x0 : x1 + 1]
        crop[mc, :3] = rgb[y0 : y1 + 1, x0 : x1 + 1][mc]
        crop[mc, 3] = 255
        far = np.linalg.norm(crop[..., :3].astype(np.float32) - np.array(key, np.float32), axis=-1)
        views[d] = {
            "crop": crop,
            "bbox_hd": [x0, y0, x1, y1],
            "attached": [{k2: v for k2, v in a.items() if k2 != "c"} for a in attached[k]],
            "residual_key_px": int((mc & (far <= RESIDUAL_KEY_FACTOR * key_tol)).sum()),
        }
    summary = {"figures": len(figs), "attach_bound_hd": bound, "dropped": dropped}
    return views, summary


def _grid_boxes(size: tuple[int, int], cols: int, rows: int, order: list[str]) -> dict[str, tuple[int, ...]]:
    w, h = size
    return {
        d: (i % cols * w // cols, i // cols * h // rows, (i % cols + 1) * w // cols, (i // cols + 1) * h // rows)
        for i, d in enumerate(order)
    }


def refprep(
    *,
    profile_path: Path,
    request_path: Path,
    out: Path,
    turnaround: Path | None = None,
    grid: str = "4x2",
    order: list[str] | None = None,
    views: dict[str, Path] | None = None,
    cell: int | None = None,
    bg_key: str | None = None,
    despill_px: int | None = None,
) -> dict:
    """Write candidate stills, crops, contact and refprep.json into the new directory `out`; return refprep.json."""
    from sonoranlab.pipeline import _pixeloe_logs_to_stderr, process_frame
    from sonoranlab.pixelize import versions
    from sonoranlab.register import register_clip

    t0 = time.monotonic()
    if out.exists() or out.is_symlink():
        raise RefprepError(f"{out} exists: refprep never overwrites, pick a new directory")
    prof = load_profile(profile_path)
    pal = load_palette(prof)
    if prof.processing is None or not prof.canvas.density_px_per_m:
        raise RefprepError("profile needs `processing:` and canvas.density_px_per_m")
    raw, request_file_sha = classreq.load_request_file(request_path)
    req, problems = classreq.parse_request(raw)
    if req is None:
        raise RefprepError("class request invalid: " + "; ".join(problems))
    char = req["character"]
    profile_order = list(prof.directions.order)
    order = order or profile_order
    if sorted(order) != sorted(profile_order) or len(set(order)) != len(order):
        raise RefprepError(f"--order must list each profile direction once: {', '.join(profile_order)}")
    views = dict(views or {})
    if unknown := sorted(set(views) - set(profile_order)):
        raise RefprepError(f"--view directions {unknown} are not profile directions")
    try:
        cols, rows = (int(x) for x in grid.lower().split("x"))
    except ValueError:
        raise RefprepError(f"--grid {grid!r}: expected COLSxROWS, e.g. 4x2") from None
    if turnaround is None and len(views) != len(profile_order):
        raise RefprepError("--turnaround is required unless every direction is given with --view")
    if turnaround is not None and cols * rows != len(order):
        raise RefprepError(f"--grid {grid}: {cols * rows} cells for {len(order)} directions")
    key = _hex(bg_key or prof.alpha.bg_key)
    pr, gen = prof.processing, prof.canvas.gen_scale
    target_h = round(req["height_m"] * prof.canvas.density_px_per_m)

    despill_px = pr.despill_px if despill_px is None else despill_px
    if isinstance(despill_px, bool) or not isinstance(despill_px, int) or despill_px < 0:
        raise RefprepError(f"--despill-px {despill_px!r}: expected a non-negative integer")

    # ---- matte and segment every source image (the turnaround as a whole, each override alone)
    sources: dict[str, dict] = {}
    matted: dict[str, dict] = {}
    segmentation: dict[str, dict] = {}
    if turnaround is not None:
        img = _open(turnaround)
        boxes = _grid_boxes(img.size, cols, rows, order)
        wanted = {d: b for d, b in boxes.items() if d not in views}
        found, segmentation["turnaround"] = _segment(img, wanted, key, pr.key_tol, despill_px, "turnaround grid")
        matted |= found
        for d in wanted:
            i = order.index(d)
            sources[d] = {"source": "turnaround", "grid_cell": [i % cols, i // cols]}
    for d, p in views.items():
        img = _open(p)
        found, segmentation[d] = _segment(img, {d: (0, 0, *img.size)}, key, pr.key_tol, despill_px, "override image")
        matted |= found
        sources[d] = {"source": "view_override", "path": str(p), "sha256": sha256_file(p)}

    # ---- scale (final px per HD px) per source image: the turnaround views share ONE common scale (median of target
    # height / view height) so their proportions stay consistent; an override comes from another canvas, so its own
    # standing height is normalised to the target. With overrides only, the median over them is the common scale.
    heights = {d: m["crop"].shape[0] for d, m in matted.items()}
    common = [d for d in profile_order if sources[d]["source"] == "turnaround"] or list(profile_order)
    scale = float(np.median([target_h / heights[d] for d in common]))
    ref_h = float(np.median([heights[d] * scale for d in common]))  # the common views' median standing height
    scales = {d: scale if d in common else target_h / heights[d] for d in profile_order}
    warnings = []
    for d in profile_order:
        dev = heights[d] * scales[d] / (target_h if d in common else ref_h) - 1
        basis = "common_median" if d in common else "override_to_target"
        sources[d] |= {"scale": scales[d], "scale_basis": basis, "height_deviation": round(dev, 4)}
        if abs(dev) > HEIGHT_DEVIATION_WARN:
            ref = f"the target {target_h}px" if d in common else f"the common views' median {ref_h:g}px"
            warnings.append(f"height: {d} stands {dev:+.1%} from {ref} ({basis})")
        if n := matted[d]["residual_key_px"]:
            warnings.append(
                f"residual_key: {d} keeps {n} opaque HD px within {RESIDUAL_KEY_FACTOR}x key_tol of the key after "
                "matting (uneven background or key-coloured art); inspect crops/"
            )

    # ---- resize, process (processing-v1 chain), measure
    for name, seg in segmentation.items():
        if seg["dropped"]:
            warnings.append(f"dropped_components: {name} dropped {len(seg['dropped'])} detached matte islands")
    _pixeloe_logs_to_stderr()
    small: dict[str, np.ndarray] = {}
    stats: dict[str, dict] = {}
    for d in profile_order:
        crop = matted[d]["crop"]
        sw, sh = max(1, round(crop.shape[1] * scales[d])), max(1, round(crop.shape[0] * scales[d]))
        hd = Image.fromarray(crop, "RGBA").resize((sw * gen, sh * gen), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", ((sw + 2 * MARGIN) * gen, (sh + 2 * MARGIN) * gen), (0, 0, 0, 0))
        canvas.paste(hd, (MARGIN * gen, MARGIN * gen))
        stages, st = process_frame(
            canvas,
            scale=gen,
            palette=pal,
            bg_key=key,
            key_tol=pr.key_tol,
            despill_px=despill_px,
            downscaler=pr.downscaler,
            thickness=pr.thickness,
            threshold=prof.alpha.threshold,
            fringe_passes=prof.alpha.fringe_passes,
            max_colours=prof.palette.max_colors_per_cell,
        )
        f = stages["04_clean"]
        hd_px = stages["01_alpha"].astype(np.int16)
        tint = (hd_px[..., 3] >= 128) & (np.minimum(hd_px[..., 0], hd_px[..., 2]) - hd_px[..., 1] > KEY_TINT)
        h, w = f.shape[:2]
        tinted = int((tint.reshape(h, gen, w, gen).any((1, 3)) & (f[..., 3] > 0)).sum())
        st = st | {"key_tinted_px": tinted}
        if tinted:
            warnings.append(
                f"key_tinted: {d} has {tinted} opaque px palette-locked from key-tinted HD pixels (magenta spill; "
                f"raise --despill-px, now {despill_px}, or fix the art)"
            )
        if not f[..., 3].any():
            raise RefprepError(f"view {d}: empty after pixelization (view too small for the target height?)")
        small[d], stats[d] = f, st | {"target_size": [sw, sh]}

    # ---- cell: profile row, --cell, or the smallest fitting multiple of 8
    pad = prof.canvas.padding
    boxes = {d: _bbox(f) for d, f in small.items()}
    widths = {d: b[2] - b[0] + 1 for d, b in boxes.items()}
    tall = max(b[3] - b[1] + 1 for b in boxes.values())
    need = max(max(w + 2 * pad + w % 2 for w in widths.values()), tall + 4 + pad)
    if cell is not None:
        source = "--cell"
    elif char in prof.characters:
        cell, source = prof.character(char).cell[0], "profile_row"
    else:
        cell, source = max(classreq.CELL_MIN, -(-need // 8) * 8), "computed"
    if cell % 8 or not classreq.CELL_MIN <= cell <= classreq.CELL_MAX:
        raise RefprepError(f"cell {cell}: expected a multiple of 8 within {classreq.CELL_MIN}..{classreq.CELL_MAX}")
    if need > cell:
        raise RefprepError(f"views need a {need}px cell ({source} cell is {cell}); max {classreq.CELL_MAX}")
    spec = prof.character(char, cell=None if char in prof.characters else cell)
    stills, placed = {}, {}
    for d in profile_order:
        [stills[d]], info = register_clip([small[d]], spec.cell, foot_y=spec.foot_y, anchor_x=spec.anchor_x)
        if any(info["clipped_px"]):
            raise RefprepError(f"view {d}: {info['clipped_px'][0]} px fall outside the {cell}px cell")
        placed[d] = info["shift_px"]

    # ---- write (exclusive) and check
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir()
    (out / "crops").mkdir()
    hashes = {}

    def save(rel: str, img: Image.Image) -> None:
        with open(out / rel, "xb") as fh:
            img.save(fh, "PNG")
        hashes[rel] = sha256_file(out / rel)

    for d in profile_order:
        save(f"still_{d}.png", Image.fromarray(stills[d], "RGBA"))
        save(f"crops/{d}_hd.png", Image.fromarray(matted[d]["crop"], "RGBA"))
    result, read = classreq.check(raw, prof, pal, stills_dir=out, stills_source=str(out))
    save("contact.png", classreq.contact(result, read, prof.directions.order))
    doc = {
        "schema": "sonoranlab.refprep/1",
        "character": char,
        "note": "candidate stills only: no approval; the owner approves exact hashes after registration",
        "inputs": {
            "turnaround": None if turnaround is None else {"path": str(turnaround), "sha256": sha256_file(turnaround)},
            "views": {d: s for d, s in sources.items() if s["source"] == "view_override"},
            "request": {
                "path": str(request_path),
                "file_sha256": request_file_sha,
                "request_sha256": result["request_sha256"],
            },
            "profile": {"path": str(profile_path), "sha256": sha256_file(profile_path)},
        },
        "parameters": {
            "grid": grid if turnaround is not None else None,
            "order": order,
            "bg_key": "#" + bytes(key).hex(),
            "key_tol": pr.key_tol,
            "despill_px": despill_px,
            "despill_px_profile": pr.despill_px,
            "figure_share": FIGURE_SHARE,
            "attach_fraction": ATTACH_FRACTION,
            "key_tint": KEY_TINT,
            "min_component_share": MIN_COMPONENT_SHARE,
            "processing_version": pr.version,
            "downscaler": pr.downscaler,
            "thickness": pr.thickness,
            "gen_scale": gen,
            "alpha_threshold": prof.alpha.threshold,
            "fringe_passes": prof.alpha.fringe_passes,
            "max_colours": prof.palette.max_colors_per_cell,
            "density_px_per_m": prof.canvas.density_px_per_m,
            "height_m": req["height_m"],
            "target_height_px": target_h,
            "common_scale": scale,
            "cell": cell,
            "cell_source": source,
            "cell_needed": need,
            "padding": pad,
            "foot_y": spec.foot_y,
            "anchor_x": spec.anchor_x,
        },
        "views": {
            d: sources[d]
            | {
                "bbox_hd": matted[d]["bbox_hd"],
                "height_hd": heights[d],
                "attached_components": matted[d]["attached"],
                "residual_key_px": matted[d]["residual_key_px"],
                "chain": stats[d],
                "final_bbox": _bbox(stills[d]),
                "shift_px": placed[d],
            }
            for d in profile_order
        },
        "segmentation": segmentation,
        "warnings": warnings,
        "outputs": hashes,
        "check": result,
        "versions": versions(),
        "elapsed_s": round(time.monotonic() - t0, 3),
    }
    with open(out / "refprep.json", "x") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return doc


def _bbox(f: np.ndarray) -> list[int]:
    ys, xs = np.nonzero(f[..., 3])
    return [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
