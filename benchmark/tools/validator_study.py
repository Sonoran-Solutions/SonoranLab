"""DYL-83 validator study: what foot_slide / bbox_drift measure on shipped clips vs the Phase 1 Track E sets.

Read-only analysis (no images written). Reuses process.py's registration and validator, then adds alternative metrics:

  edge deltas   per-edge |delta| of the bbox vs the previous frame (the 4 terms whose max is bbox_drift).
  core          the silhouette after a binary opening with a (2r+1)^2 square (r = --core-radius, default 2): parts
                thinner than 2r+1 px (blades, hair strands, tassel tips) are removed. core_drift = bbox_drift of it.
  protrusion    how far the full bbox edge sticks out past the core bbox edge (thin part defines that edge).
  centroid_step max(|dx|, |dy|) of the opaque-pixel centroid vs the previous frame.
  foot_body     contact frames: |foot_x - core bbox centre x| (foot vs the body, not vs the clip anchor).
  planted_step  consecutive contact frames: |x of the nearest foot-row run - previous frame's foot_x| (how far the
                planted foot moves between two contact frames; nearest match, so optimistic).
  foot_run_w    width of the run foot_x is taken from (a 1-3 px run is a tip, not a sole).
  ranges        over the clip: bbox centre x, core centre x, centroid x/y, bbox bottom (hover drift and bob).

    uv run --no-project --with pillow --with numpy --with pyyaml python -I benchmark/tools/validator_study.py \
      ~/Projects/TwosAreUp ~/sonoranlab-work/benchmark OUT.json --tables OUT.md

GAME is the private game checkout, WORK the private benchmark dir (characters/2026-10-18 gt clips and
results/2026-10-18/E sets). OUT.json gets every per-frame number; --tables writes the markdown tables of the study.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process as P

GT_CHARS = {"samurai": "run", "oathguard": "run", "relay_sentinel": "idle"}
EDGES = ("left", "top", "right", "bottom")


def opening(m: np.ndarray, r: int) -> np.ndarray:
    """Binary opening with a (2r+1)^2 square."""
    h, w = m.shape
    k = 2 * r + 1

    def sweep(x: np.ndarray, fill: bool, op) -> np.ndarray:
        p = np.pad(x, r, constant_values=fill)
        out = p[:h, :w].copy()
        for dy in range(k):
            for dx in range(k):
                out = op(out, p[dy : dy + h, dx : dx + w])
        return out

    return sweep(sweep(m, True, np.logical_and), False, np.logical_or)


def runs(row: np.ndarray) -> list[tuple[int, int]]:
    xs = np.nonzero(row)[0]
    return [(int(r[0]), int(r[-1])) for r in np.split(xs, np.nonzero(np.diff(xs) > 1)[0] + 1) if len(r)]


def study_clip(fs: list[np.ndarray], cell: int, pal: np.ndarray, r: int, loop: bool = True) -> dict:
    """Registered frames of one direction -> process.validate's per-frame values plus the alternative metrics."""
    rep = P.validate({"east": fs}, cell, len(fs), ["east"], pal, loop=loop)  # direction name is a label only
    rows = rep["frames"]
    n = len(fs)
    for f, row in zip(fs, rows):
        m = f[..., 3] > 0
        core = opening(m, r)
        cb = P.bbox(np.dstack([core] * 4).astype(np.uint8)) or row["bbox"]
        ys, xs = np.nonzero(m)
        b = row["bbox"]
        row |= {
            "core_bbox": cb,
            "core_share": round(float(core.sum() / m.sum()), 3),
            "protrusion": [cb[0] - b[0], cb[1] - b[1], b[2] - cb[2], b[3] - cb[3]],
            "centroid": [round(float(xs.mean()), 2), round(float(ys.mean()), 2)],
            "cx": (b[0] + b[2] + 1) / 2,
            "core_cx": (cb[0] + cb[2] + 1) / 2,
        }
        row["foot_run_w"] = next(e - s + 1 for s, e in runs(m[row["foot_y"]]) if s <= row["foot_x"] - 0.5 <= e + 0.5)
        row["foot_body"] = abs(row["foot_x"] - row["core_cx"]) if row["contact"] else None
    for i, row in enumerate(rows):
        if i == 0 and not loop:
            row |= {"edge_d": None, "core_drift": None, "centroid_step": None, "planted_step": None, "planted_dx": None}
            continue
        p = rows[i - 1]
        row["edge_d"] = [abs(a - c) for a, c in zip(row["bbox"], p["bbox"])]
        row["drift_edge"] = EDGES[int(np.argmax(row["edge_d"]))]
        row["core_edge_d"] = [abs(a - c) for a, c in zip(row["core_bbox"], p["core_bbox"])]
        row["core_drift"] = max(row["core_edge_d"])
        row["centroid_step"] = round(max(abs(a - c) for a, c in zip(row["centroid"], p["centroid"])), 2)
        row["planted_step"] = row["planted_dx"] = None
        if row["contact"] and p["contact"] and n > 1:
            cand = runs(fs[i][row["foot_y"], :, 3] > 0)
            row["planted_dx"] = min(((s + e + 1) / 2 - p["foot_x"] for s, e in cand), key=abs)
            row["planted_step"] = abs(row["planted_dx"])

    def mx(k: str) -> float | None:
        v = [x[k] for x in rows if x.get(k) is not None]
        return max(v) if v else None

    def rng(vals: list[float]) -> float:
        return round(max(vals) - min(vals), 2)

    return {
        "frames": [
            {
                k: x.get(k)
                for k in (
                    "frame",
                    "bbox",
                    "foot_y",
                    "contact",
                    "foot_x",
                    "foot_slide",
                    "bbox_drift",
                    "edge_d",
                    "drift_edge",
                    "core_bbox",
                    "core_share",
                    "core_edge_d",
                    "core_drift",
                    "protrusion",
                    "centroid",
                    "centroid_step",
                    "foot_body",
                    "planted_dx",
                    "planted_step",
                    "foot_run_w",
                )
            }
            for x in rows
        ],
        "foot_slide_max": mx("foot_slide"),
        "bbox_drift_max": mx("bbox_drift"),
        "seam_drift": rows[0].get("bbox_drift"),
        "core_drift_max": mx("core_drift"),
        "centroid_step_max": mx("centroid_step"),
        "foot_body_max": mx("foot_body"),
        "planted_step_max": mx("planted_step"),
        "cx_range": rng([x["cx"] for x in rows]),
        "core_cx_range": rng([x["core_cx"] for x in rows]),
        "centroid_x_range": rng([x["centroid"][0] for x in rows]),
        "centroid_y_range": rng([x["centroid"][1] for x in rows]),
        "bottom_range": rng([x["bbox"][3] for x in rows]),
        "min_core_share": min(x["core_share"] for x in rows),
    }


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def shipped(game: Path, gt: Path, pal: np.ndarray, r: int) -> tuple[dict, dict]:
    """Every shipped run and idle clip (5 generated directions) from the game sheets, registered per clip."""
    root = game / "assets/pixel/v0/characters"
    meta = json.loads((root / "characters.json").read_text())
    out, hashes = {}, {"characters.json": sha(root / "characters.json")}
    for name, c in meta["characters"].items():
        cell = c["cell"]
        hashes[c["sheet"]] = sha(root / c["sheet"])
        sheet = np.asarray(Image.open(root / c["sheet"]).convert("RGBA"))
        for clip in (k for k in c["clips"] if k["name"] in ("run", "idle")):
            dirs = {}
            for d in P.GEN_DIRS:
                row = meta["rows"].index(d)
                fs = [
                    sheet[row * cell : (row + 1) * cell, col * cell : (col + 1) * cell].copy()
                    for col in range(clip["start"], clip["start"] + clip["frames"])
                ]
                if GT_CHARS.get(name) == clip["name"]:  # same pixels as the frozen calibration inputs
                    for i, f in enumerate(fs):
                        g = np.asarray(Image.open(gt / name / f"gt_{clip['name']}_{d}_{i}.png").convert("RGBA"))
                        assert (g == f).all(), (name, d, i, "game sheet differs from the frozen gt clip")
                reg, _ = P.register_clip(fs, cell)
                dirs[d] = study_clip(reg, cell, pal, r, clip["loop"])
            out.setdefault(name, {})[clip["name"]] = {"cell": cell, "frames": clip["frames"], "dirs": dirs}
    return out, hashes


def generated(sets: Path, pal: np.ndarray, r: int) -> dict:
    """Track E sets: bound processed frames (hash-checked against report.json), recomputed and cross-checked."""
    out = {}
    for rp in sorted(sets.glob("*/*/set/processed/report.json")):
        rep = json.loads(rp.read_text())
        name, seed = rp.parts[-5], rp.parts[-4]
        cell, prm = rep["params"]["cell"], rep["params"]
        dirs = {}
        for d in P.GEN_DIRS:
            files = sorted(rp.parent.glob(f"processed/{d}_*.png"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
            for p in files:
                assert sha(p) == rep["provenance"]["outputs"][p.name], (p, "not the bound processed frame")
            dirs[d] = study_clip([np.asarray(Image.open(p).convert("RGBA")) for p in files], cell, pal, r)
            bound = [x for x in rep["frames"] if x["direction"] == d]
            for mine, theirs in zip(dirs[d]["frames"], bound):
                assert (mine["foot_slide"], mine["bbox_drift"]) == (theirs["foot_slide"], theirs["bbox_drift"]), (rp, d)
        out.setdefault(name, {})[seed] = {
            "cell": cell,
            "anchor_mode": rep["anchor_mode"],
            "pass": rep["pass"],
            "failures": rep["failures"],
            "limits": {"foot_slide": prm["max_foot_slide"], "bbox_drift": prm["max_bbox_drift"]},
            "report_sha256": sha(rp),
            "dirs": dirs,
        }
    return out


# ---------------------------------------------------------------- tables


def f(x: float | None, nd: int = 1) -> str:
    return "–" if x is None else (f"{x:.{nd}f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x))


CLIP_KEYS = (
    "foot_slide_max",
    "bbox_drift_max",
    "seam_drift",
    "core_drift_max",
    "centroid_step_max",
    "foot_body_max",
    "planted_step_max",
)


def cmax(dirs: dict, k: str) -> float | None:
    v = [d[k] for d in dirs.values() if d.get(k) is not None]
    return max(v) if v else None


def tables(ship: dict, gen: dict) -> str:
    md = []
    hdr = "| clip | cell | foot_slide | bbox_drift | seam | core_drift | centroid_step | foot_body | planted_step |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    for clip in ("run", "idle"):
        md += [f"\n### Shipped {clip} clips (max over N/NE/E/SE/S)\n", hdr, sep]
        for name, cl in sorted(ship.items()):
            if clip in cl:
                c = cl[clip]
                md.append(f"| {name} | {c['cell']} | " + " | ".join(f(cmax(c["dirs"], k)) for k in CLIP_KEYS) + " |")
    md += [
        "\n### Generated Track E sets (max over N/NE/E/SE/S; limits foot_slide / bbox_drift)\n",
        "| set | limits | pass | " + hdr[9:],
        "|---|---" + sep,
    ]
    for name, seeds in gen.items():
        for seed, s in seeds.items():
            lim = f"{s['limits']['foot_slide']} / {s['limits']['bbox_drift']}"
            md.append(
                f"| {name} {seed} | {lim} | {'yes' if s['pass'] else 'no'} | {s['cell']} | "
                + " | ".join(f(cmax(s["dirs"], k)) for k in CLIP_KEYS)
                + " |"
            )
    md += [
        (
            "\n### Per-direction, calibration characters: shipped vs generated (foot_slide / bbox_drift / core_drift"
            " / foot_body / planted_step)\n"
        ),
        "| char | dir | shipped | seed 1 | seed 2 | seed 3 | seed 4 |",
        "|---|---|---|---|---|---|---|",
    ]
    keys = ("foot_slide_max", "bbox_drift_max", "core_drift_max", "foot_body_max", "planted_step_max")
    for name, clip in GT_CHARS.items():
        for d in P.GEN_DIRS:
            cells = [" / ".join(f(ship[name][clip]["dirs"][d][k]) for k in keys)]
            cells += [" / ".join(f(gen[name][s]["dirs"][d][k]) for k in keys) for s in sorted(gen[name])]
            md.append(f"| {name} | {d} | " + " | ".join(cells) + " |")
    md += [
        "\n### Failing frames of the bound reports, decomposed\n",
        (
            "| set | dir/frame | metric | value | limit | edge (Δ l,t,r,b) | core Δ on that edge | protrusion prev→cur"
            " | seam | foot_body | foot_run_w | planted_step |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, seeds in gen.items():
        for seed, s in seeds.items():
            for d, c in s["dirs"].items():
                fr = c["frames"]
                for x in fr:
                    p = fr[x["frame"] - 1]
                    if x["bbox_drift"] is not None and x["bbox_drift"] > s["limits"]["bbox_drift"]:
                        e = EDGES.index(x["drift_edge"])
                        md.append(
                            f"| {name} {seed} | {d}/{x['frame']} | bbox_drift | {x['bbox_drift']} | "
                            f"{s['limits']['bbox_drift']} | {x['drift_edge']} ({','.join(map(str, x['edge_d']))})"
                            f" | {x['core_edge_d'][e]} | {p['protrusion'][e]}→{x['protrusion'][e]} | "
                            f"{'yes' if x['frame'] == 0 else ''} | | | |"
                        )
                    if x["foot_slide"] is not None and x["foot_slide"] > s["limits"]["foot_slide"]:
                        md.append(
                            f"| {name} {seed} | {d}/{x['frame']} | foot_slide | {f(x['foot_slide'])} | "
                            f"{s['limits']['foot_slide']} | | | | | {f(x['foot_body'])} | {x['foot_run_w']} | "
                            f"{f(x['planted_step'])} |"
                        )
    md += [
        "\n### Hover (relay_sentinel idle, bbox_bottom): drift and bob over the loop, per direction\n",
        (
            "| clip | dir | foot_slide | foot_run_w (contact) | cx_range | core_cx_range | centroid_x_range |"
            " centroid_y_range | bottom_range | centroid_step |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    hov = [("shipped", ship["relay_sentinel"]["idle"]["dirs"])]
    hov += [(f"seed {s}", gen["relay_sentinel"][s]["dirs"]) for s in sorted(gen["relay_sentinel"])]
    for label, dirs in hov:
        for d, c in dirs.items():
            w = [x["foot_run_w"] for x in c["frames"] if x["contact"]]
            md.append(
                f"| {label} | {d} | {f(c['foot_slide_max'])} | {w} | "
                + " | ".join(
                    f(c[k], 2)
                    for k in (
                        "cx_range",
                        "core_cx_range",
                        "centroid_x_range",
                        "centroid_y_range",
                        "bottom_range",
                        "centroid_step_max",
                    )
                )
                + " |"
            )
    md += forward_table(ship, gen) + limits_table(ship) + options_table(options(ship, gen), gen)
    return "\n".join(md) + "\n"


FACING_X = ("north-east", "east", "south-east")  # +x is the facing side in these rows


def fwd(dirs: dict) -> tuple[int, int]:
    """(planted steps that move > 1 px toward the facing side, all planted steps) over NE/E/SE."""
    dx = [x["planted_dx"] for d in FACING_X for x in dirs[d]["frames"] if x["planted_dx"] is not None]
    return sum(v > 1 for v in dx), len(dx)


def forward_table(ship: dict, gen: dict) -> list[str]:
    md = [
        "\n### Planted-foot direction, NE/E/SE: steps moving > 1 px toward the facing side / all planted steps\n",
        "| clip | forward / steps |",
        "|---|---|",
    ]
    for name, cl in sorted(ship.items()):
        if "run" in cl:
            md.append(f"| shipped {name} run | {'/'.join(map(str, fwd(cl['run']['dirs'])))} |")
    for name in ("samurai", "oathguard"):
        for seed, st in gen[name].items():
            md.append(f"| E {name} {seed} | {'/'.join(map(str, fwd(st['dirs'])))} |")
    return md


def limits_table(ship: dict) -> list[str]:
    """How many SHIPPED clips each calibrated limit pair would fail (current semantics)."""
    md = [
        (
            "\n### Shipped clips failing the calibrated limits (current semantics; foot_slide > limit, bbox_drift > limit)"
            "\n"
        ),
        (
            "| limits (foot / drift) | run clips failing foot_slide | run clips failing bbox_drift |"
            " idle clips failing foot_slide | idle clips failing bbox_drift |"
        ),
        "|---|---|---|---|---|",
    ]
    for name, (fs, bd) in LIMITS.items():
        row = [f"| {name} ({fs} / {bd}) |"]
        for clip in ("run", "idle"):
            cl = {n: c[clip]["dirs"] for n, c in ship.items() if clip in c}
            for k, lim in (("foot_slide_max", fs), ("bbox_drift_max", bd)):
                bad = sorted(n for n, dirs in cl.items() if (cmax(dirs, k) or 0) > lim)
                row.append(f" {len(bad)}/{len(cl)}: {', '.join(bad) or '–'} |")
        md.append("".join(row))
    return md


LIMITS = {"samurai": (30, 16), "oathguard": (20, 8), "relay_sentinel": (15, 10), "all": (30, 16)}  # 2026-10-18.yaml


def ceil(x: float) -> int:
    return int(np.ceil(x - 1e-9))


def options(ship: dict, gen: dict) -> dict:
    """Pass/fail of every Phase 1 E set under each option (limits = shipped observed max rounded up, as DYL-71)."""
    run = [c["run"]["dirs"] for c in ship.values() if "run" in c]
    pooled = {k: ceil(max(cmax(d, k) for d in run)) for k in ("foot_slide_max", "bbox_drift_max")}
    res = {"pooled_run_limits": pooled, "sets": {}}
    for name, seeds in gen.items():
        own = ship[name][GT_CHARS[name]]["dirs"]
        lim = {k: ceil(cmax(own, k)) for k in ("core_drift_max", "foot_body_max", "core_cx_range")}
        res.setdefault("own_limits", {})[name] = lim
        for seed, s in seeds.items():
            assert all(x.split(":")[0] in ("foot_slide", "bbox_drift") for x in s["failures"]), s["failures"]
            m = {
                k: cmax(s["dirs"], k)
                for k in ("foot_slide_max", "bbox_drift_max", "core_drift_max", "foot_body_max", "core_cx_range")
            }
            fs_ok = m["foot_slide_max"] <= s["limits"]["foot_slide"]
            bd_ok = m["bbox_drift_max"] <= s["limits"]["bbox_drift"]
            o = {"keep": s["pass"]}
            if s["anchor_mode"] == "foot":
                o["H1 core drift"] = fs_ok and m["core_drift_max"] <= lim["core_drift_max"]
                o["H2 core drift + foot vs body"] = (
                    m["core_drift_max"] <= lim["core_drift_max"] and m["foot_body_max"] <= lim["foot_body_max"]
                )
                o["H3 pooled run limits"] = (
                    m["foot_slide_max"] <= pooled["foot_slide_max"] and m["bbox_drift_max"] <= pooled["bbox_drift_max"]
                )
            else:
                o["F1 body x-drift"] = bd_ok and m["core_cx_range"] <= lim["core_cx_range"]
                o["F2 no foot_slide"] = bd_ok
            res["sets"][f"{name} {seed}"] = {"metrics": m, "pass": o}
    return res


def options_table(opt: dict, gen: dict) -> list[str]:
    md = [
        (
            f"\n### Options: pass/fail per Phase 1 E set\n\npooled shipped run limits (13 clips): "
            f"foot_slide {opt['pooled_run_limits']['foot_slide_max']}, bbox_drift {opt['pooled_run_limits']['bbox_drift_max']}"
            f"; own-clip limits: {json.dumps(opt['own_limits'])}\n"
        )
    ]
    for kind in ("foot", "bbox_bottom"):
        sets = [k for k in opt["sets"] if gen[k.split()[0]][k.split()[1]]["anchor_mode"] == kind]
        cols = list(opt["sets"][sets[0]]["pass"])
        md += ["| set | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        md += [
            f"| {k} | " + " | ".join("pass" if opt["sets"][k]["pass"][c] else "FAIL" for c in cols) + " |" for k in sets
        ]
        md += [
            "| **passing** | " + " | ".join(str(sum(opt["sets"][k]["pass"][c] for k in sets)) for c in cols) + " |",
            "",
        ]
    return md


def sens_table(sens: dict) -> str:
    """Which sets pass each option as the core opening radius changes (only options using the core depend on it)."""
    names = list(next(iter(sens.values()))["sets"])
    opts = list(dict.fromkeys(o for k in names for o in sens[1]["sets"][k]["pass"]))
    md = [
        "\n### Core-radius sensitivity: sets passing each option at opening radius r = 1 / 2 / 3\n",
        "| option | r = 1 | r = 2 | r = 3 |",
        "|---|---|---|---|",
    ]
    for o in opts:
        md.append(
            f"| {o} | "
            + " | ".join(", ".join(k for k in names if sens[r]["sets"][k]["pass"].get(o)) or "none" for r in (1, 2, 3))
            + " |"
        )
    return "\n".join(md) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game", type=Path)
    ap.add_argument("work", type=Path)
    ap.add_argument("out_json", type=Path)
    ap.add_argument("--tables", type=Path)
    ap.add_argument("--core-radius", type=int, default=2)
    a = ap.parse_args()
    game, work = a.game.expanduser(), a.work.expanduser()
    pal = P.load_palette(game / "assets/pixel/v0/characters/palette.png")
    ship, hashes = shipped(game, work / "characters/2026-10-18", pal, a.core_radius)
    gen = generated(work / "results/2026-10-18/E", pal, a.core_radius)
    commit = subprocess.run(
        ["git", "-C", str(game), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    res = {
        "params": {
            "core_radius": a.core_radius,
            "game_commit": commit.stdout.strip() or None,
            "tool_sha256": sha(Path(__file__)),
            "process_sha256": sha(Path(P.__file__)),
        },
        "game_hashes": hashes,
        "options": options(ship, gen),
        "shipped": ship,
        "generated": gen,
    }
    sens = {
        r: options(
            shipped(game, work / "characters/2026-10-18", pal, r)[0], generated(work / "results/2026-10-18/E", pal, r)
        )
        for r in (1, 2, 3)
    }
    res["core_radius_sensitivity"] = sens
    md = (
        f"Game checkout {res['params']['game_commit']}, characters.json sha256 {hashes['characters.json'][:12]}; "
        f"core radius {a.core_radius}.\n" + tables(ship, gen) + sens_table(sens)
    )
    a.out_json.parent.mkdir(parents=True, exist_ok=True)
    a.out_json.write_text(json.dumps(res, indent=1) + "\n")
    if a.tables:
        a.tables.write_text(md)
    print(md)


if __name__ == "__main__":
    main()
