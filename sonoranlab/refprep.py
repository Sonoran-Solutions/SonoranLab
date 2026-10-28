"""`sonoranlab refprep`: deterministic conversion of owner-directed HD turnaround art into CANDIDATE stills (ADR-0011).

Pixel math only, no model. Per view: split the turnaround grid (or take a `--view DIRECTION=IMG` override) -> chroma
matte with the profile's key_tol + despill (alpha.py) -> keep components >= MIN_COMPONENT_SHARE of the largest ->
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
MARGIN = 2  # transparent final px around a view while it is processed
MAX_EDGE = 16384


class RefprepError(ValueError):
    """Usage or input error; nothing has been written."""


def components(mask: np.ndarray) -> list[dict]:
    """8-connected components of a boolean mask (row runs + union-find): [{runs, pixels, bbox}], largest first."""
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
        c = comps.setdefault(find(i), {"runs": [], "pixels": 0, "bbox": [s, y, e - 1, y]})
        c["runs"].append((y, s, e))
        c["pixels"] += e - s
        b = c["bbox"]
        c["bbox"] = [min(b[0], s), min(b[1], y), max(b[2], e - 1), max(b[3], y)]
    return sorted(comps.values(), key=lambda c: (-c["pixels"], c["bbox"]))


def keep_components(mask: np.ndarray, share: float = MIN_COMPONENT_SHARE) -> tuple[np.ndarray, list[dict]]:
    """(mask of the components >= share x the largest, [{pixels, bbox}] of the dropped ones)."""
    comps = components(mask)
    out = np.zeros_like(mask, dtype=bool)
    dropped = []
    for c in comps:
        if c["pixels"] >= share * comps[0]["pixels"]:
            for y, s, e in c["runs"]:
                out[y, s:e] = True
        else:
            dropped.append({"pixels": c["pixels"], "bbox_hd": c["bbox"]})
    return out, dropped


def _hex(c: str) -> tuple[int, int, int]:
    if not (isinstance(c, str) and len(c) == 7 and c[0] == "#"):
        raise RefprepError(f"bg key {c!r} is not #rrggbb")
    return tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))


def _open(path: Path) -> Image.Image:
    with Image.open(path) as im:
        if max(im.size) > MAX_EDGE:
            raise RefprepError(f"{path}: {im.size[0]}x{im.size[1]} exceeds {MAX_EDGE} px per edge")
        return im.convert("RGBA")


def _matte(im: Image.Image, d: str, key: tuple[int, int, int], key_tol: float, despill_px: int) -> dict:
    """Matted HD crop of one view: RGBA crop, dropped components and residual key-coloured pixels."""
    rgb, alpha = extract_alpha(im, key, key_tol)
    rgb, _ = despill(rgb, alpha, despill_px, key)
    mask = alpha >= 128
    if not mask.any():
        raise RefprepError(
            f"view {d}: no opaque pixels after the chroma key (blank grid cell?); views are never synthesised"
        )
    mask, dropped = keep_components(mask)
    ys, xs = np.nonzero(mask)
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    crop = np.zeros((y1 - y0 + 1, x1 - x0 + 1, 4), np.uint8)
    m = mask[y0 : y1 + 1, x0 : x1 + 1]
    crop[m, :3] = rgb[y0 : y1 + 1, x0 : x1 + 1][m]
    crop[m, 3] = 255
    dist = np.linalg.norm(crop[..., :3].astype(np.float32) - np.array(key, np.float32), axis=-1)
    residual = int((m & (dist <= RESIDUAL_KEY_FACTOR * key_tol)).sum())
    return {"crop": crop, "bbox_hd": [x0, y0, x1, y1], "dropped": dropped, "residual_key_px": residual}


def _grid_cells(img: Image.Image, cols: int, rows: int, order: list[str]) -> dict[str, tuple[Image.Image, list[int]]]:
    w, h = img.size
    out = {}
    for i, d in enumerate(order):
        c, r = i % cols, i // cols
        box = (c * w // cols, r * h // rows, (c + 1) * w // cols, (r + 1) * h // rows)
        out[d] = (img.crop(box), [c, r])
    return out


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

    # ---- matte every view (grid cell or override), in profile order
    sources: dict[str, dict] = {}
    images: dict[str, Image.Image] = {}
    if turnaround is not None:
        for d, (im, at) in _grid_cells(_open(turnaround), cols, rows, order).items():
            images[d], sources[d] = im, {"source": "turnaround", "grid_cell": at}
    for d, p in views.items():
        images[d] = _open(p)
        sources[d] = {"source": "view_override", "path": str(p), "sha256": sha256_file(p)}
    matted = {d: _matte(images[d], d, key, pr.key_tol, pr.despill_px) for d in profile_order}

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
        if matted[d]["dropped"]:
            warnings.append(f"dropped_components: {d} dropped {len(matted[d]['dropped'])} small matte islands")

    # ---- resize, process (processing-v1 chain), measure
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
            despill_px=pr.despill_px,
            downscaler=pr.downscaler,
            thickness=pr.thickness,
            threshold=prof.alpha.threshold,
            fringe_passes=prof.alpha.fringe_passes,
            max_colours=prof.palette.max_colors_per_cell,
        )
        f = stages["04_clean"]
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
            "despill_px": pr.despill_px,
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
                "dropped_components": matted[d]["dropped"],
                "residual_key_px": matted[d]["residual_key_px"],
                "chain": stats[d],
                "final_bbox": _bbox(stills[d]),
                "shift_px": placed[d],
            }
            for d in profile_order
        },
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
