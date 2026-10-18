"""Validate a bake_rig.gd output dir, and optionally compare it with a second bake of the same inputs.

    python tools/godot/check_bake.py BAKE_DIR [SECOND_BAKE_DIR]

Checks (stdlib only, reads bake.json + PNG bytes): every frame set / direction / frame present, lowest
opaque row == foot row at cell and gen canvas, >= 2 px transparent padding, and, with two dirs,
identical joints and byte-identical PNGs. Exit 1 on any problem.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PADDING = 2


def problems(bake_dir: Path) -> list[str]:
    rep = json.loads((bake_dir / "bake.json").read_text())
    out: list[str] = []
    for b in rep["bakes"]:
        tag = f"f{b['frames']}/c{b['cell']}"
        for d in rep["directions"]:
            low = b["lowest_row"][d]
            if low["cell"] != b["foot_row_cell"] or low["gen"] != b["foot_row_gen"]:
                out.append(
                    f"{tag} {d}: lowest row {low} != foot row {b['foot_row_cell']}/{b['foot_row_gen']}"
                )
            frames = b["frames_data"][d]
            if len(frames) != b["frames"]:
                out.append(f"{tag} {d}: {len(frames)} frames, expected {b['frames']}")
            for i, fr in enumerate(frames):
                for key, size in (("cell", b["cell"]), ("gen", b["gen_canvas"])):
                    x, y, w, _ = fr[key]["bbox"]
                    pad = PADDING * (1 if key == "cell" else rep["gen_scale"])
                    if x < pad or y < pad or x + w > size - pad:
                        out.append(
                            f"{tag} {d} {i} {key}: bbox {fr[key]['bbox']} inside {pad} px padding"
                        )
    return out


def png_hashes(bake_dir: Path) -> dict[str, str]:
    return {
        str(p.relative_to(bake_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in bake_dir.rglob("*.png")
    }


def joints(bake_dir: Path) -> list:
    rep = json.loads((bake_dir / "bake.json").read_text())
    return [rep["phase"]] + [
        (b["cell"], b["frames"], b["frames_data"]) for b in rep["bakes"]
    ]


def compare(a: Path, b: Path) -> list[str]:
    out = []
    if joints(a) != joints(b):
        out.append("joint positions differ between bakes")
    ha, hb = png_hashes(a), png_hashes(b)
    diff = sorted(k for k in ha.keys() | hb.keys() if ha.get(k) != hb.get(k))
    if diff:
        out.append(f"{len(diff)}/{len(ha)} PNGs differ, e.g. {diff[:3]}")
    return out


def main(argv: list[str]) -> int:
    dirs = [Path(p) for p in argv[1:]]
    if not 1 <= len(dirs) <= 2:
        print(__doc__)
        return 2
    found = [p for d in dirs for p in problems(d)]
    if len(dirs) == 2:
        found += compare(*dirs)
        n = len(png_hashes(dirs[0]))
        print(f"compared {n} PNGs and all joints")
    for p in found:
        print("FAIL", p)
    print("OK" if not found else f"{len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
