"""Assemble a benchmark set and record the exact generation attempts used.

For each part (the A/B grid or each C/E direction), consider only its documented
attempt directories, including retries -r1 through -r6. Select the successful
attempt (exit_code == 0) with the highest retry index; an unsuffixed attempt is
retry 0. A part with no successful attempt stays missing. Parts and retries are
enumerated in a fixed order, independent of directory listing order. This rule
reproduces the processing choices for the 2026-10-18 sets.

    python benchmark/tools/assemble_set.py RESULTS_DATE_DIR TRACK CHAR SEED --out OUT_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image

if __package__:
    from . import select_loop, slice_grid
else:
    import select_loop
    import slice_grid


DIRECTIONS = ("north", "north-east", "east", "south-east", "south")
CHARACTERS = {"samurai": (184, 7), "oathguard": (176, 6), "relay_sentinel": (136, 4)}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("results", type=Path)
    ap.add_argument("track", choices=("A", "B", "C", "E"))
    ap.add_argument("character", choices=CHARACTERS)
    ap.add_argument("seed", type=int)
    ap.add_argument("--variant", choices=("ck3000", "ck2750"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cell", type=positive_int, help="override character cell size for synthetic tests")
    ap.add_argument("--frames", type=positive_int, help="override C/E frame count for synthetic tests")
    a = ap.parse_args(argv)
    if not a.results.is_dir():
        ap.error("RESULTS_DATE_DIR must be an existing directory")
    a.results = a.results.resolve()
    if a.variant and a.track != "B":
        ap.error("--variant applies only to Track B")
    if a.track == "C" and a.character == "relay_sentinel":
        ap.error("Track C has no relay_sentinel sets")
    if a.track in ("A", "B") and a.frames not in (None, 3):
        ap.error("Tracks A/B always use 3 walk columns")

    variant = (a.variant or "ck3000") if a.track == "B" else ""
    cell, frames = CHARACTERS[a.character]
    cell = a.cell or cell
    frames = a.frames or (3 if a.track in ("A", "B") else frames)
    params = {"cell": cell, "frames": frames, "scale": 4 if a.track in ("A", "B") else 3,
              "resize": "none" if a.track in ("A", "B") else "lanczos"}
    base = a.results / a.track / a.character / str(a.seed)
    if a.track == "A":
        bases = {"grid": base / "smoke-try2" if a.character == "samurai" and a.seed == 1 else base}
    elif a.track == "B":
        bases = {"grid": base / variant}
    else:
        bases = {d: base / (f"{d}-r1024" if a.track == "C" else d) for d in DIRECTIONS}

    attempts = []
    chosen = {}
    for part, directory in bases.items():
        for retry in range(7):
            attempt_dir = directory.with_name(directory.name + (f"-r{retry}" if retry else ""))
            manifest_path = attempt_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
            relative = attempt_dir.relative_to(a.results).as_posix()
            if manifest["job"] != f"{a.results.name}/{relative}":
                raise ValueError(f"manifest job does not match attempt directory: {manifest_path}")
            attempt = {"job": manifest["job"], "dir": relative, "part": part, "retry": retry,
                       "exit_code": manifest["exit_code"], "wall_s": manifest["run"]["wall_s"],
                       "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "selected": False}
            attempts.append(attempt)
            if attempt["exit_code"] == 0:
                chosen[part] = attempt
    for attempt in chosen.values():
        attempt["selected"] = True

    a.out.mkdir(parents=True, exist_ok=False)
    raw_out = a.out / "raw"
    if a.track not in ("A", "B") or not chosen:
        raw_out.mkdir()
    loops = {}
    if a.track in ("A", "B"):
        cols = 6 if a.track == "A" else 4
        params["slice"] = {"cols": cols, "rows": 4, "walk_cols": 3, "bg_tol": 40}
    elif a.track == "E":
        params["loop"] = {"frames": frames, "size": cell * 3}
    for part, attempt in chosen.items():
        source = a.results / attempt["dir"] / "raw"
        if a.track in ("A", "B"):
            slice_grid.main([str(source / "out_00001_.png"), str(raw_out), "--cols", str(cols),
                             "--rows", "4", "--walk-cols", "3", "--bg-tol", "40"])
        elif a.track == "C":
            for i in range(frames):
                with Image.open(source / f"rgba_{i}_00001_.png") as image:
                    image.convert("RGBA").resize((cell * 3, cell * 3), Image.Resampling.LANCZOS).save(
                        raw_out / f"{part}_{i}.png")
        else:
            loop_out = a.out / "loops" / part
            select_loop.main([str(source), str(loop_out), "--direction", part, "--frames", str(frames),
                              "--size", str(cell * 3)])
            loops[part] = json.loads((loop_out / "loop.json").read_text())
            for i in range(frames):
                shutil.copyfile(loop_out / f"{part}_{i}.png", raw_out / f"{part}_{i}.png")

    selection = {"schema": "sonoranlab.selection/1",
                 "set": "/".join((a.track, a.character, str(a.seed), *([variant] if variant else []))),
                 "track": a.track, "character": a.character, "seed": str(a.seed), "variant": variant,
                 "attempts": attempts, "selected": {part: attempt["job"] for part, attempt in chosen.items()},
                 "missing_parts": [part for part in bases if part not in chosen],
                 "raw_frames": {p.name: sha256(p) for p in sorted(raw_out.glob("*.png"))},
                 "assembly": {"tool": "benchmark/tools/assemble_set.py", "tool_sha256": sha256(Path(__file__)),
                              "params": params}}
    if a.track == "E":
        selection["loops"] = loops
    (a.out / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    return 0 if chosen else 3


if __name__ == "__main__":
    raise SystemExit(main())
