"""`sonoranlab <verb>`: the CLI every MCP tool wraps (CLAUDE.md). Exit codes: 0 ok, 1 validation failed, 2 usage/config.

A new verb is one `add_parser` block in `build_parser` plus its handler function below.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import numpy as np

from sonoranlab.profile import ProfileError, check_profile, load_profile


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sonoranlab", description=__doc__.splitlines()[0])
    verbs = p.add_subparsers(dest="verb", required=True)

    prof = verbs.add_parser("profile", help="check or show a project profile (docs/07)").add_subparsers(
        dest="action", required=True
    )
    c = prof.add_parser("check", help="print every problem; exit 0 if clean, 2 otherwise")
    c.add_argument("file", type=Path)
    c.add_argument("--no-path-check", action="store_true", help="skip the palette file and export project checks")
    c.set_defaults(func=profile_check)
    s = prof.add_parser("show", help="print a character's resolved spec as JSON")
    s.add_argument("file", type=Path)
    s.add_argument("--character", required=True)
    s.add_argument("--cell", type=int, help="cell size for a character without a profile row")
    s.set_defaults(func=profile_show)

    exp = verbs.add_parser("export", help="export a processed sheet").add_subparsers(dest="action", required=True)
    g = exp.add_parser(
        "godot", help="write the game-layout PNG, a SpriteFrames .tres and a provenance sidecar (docs/09)"
    )
    g.add_argument("sheet", type=Path, help="RGBA sheet PNG already in the game layout (a row per direction)")
    g.add_argument("--character", required=True)
    g.add_argument("--cell", type=int, required=True, help="square cell edge in pixels")
    g.add_argument("--rows", required=True, help="comma-separated directions, top row first (profile order)")
    g.add_argument(
        "--clip",
        action="append",
        required=True,
        metavar="NAME:START:FRAMES:FPS:loop|once[:LOOP_FROM]",
        help="repeatable",
    )
    g.add_argument("--out", type=Path, required=True, help="writes OUT/<character>/; must not already hold files")
    g.add_argument("--provenance", type=Path, help="JSON object copied into the sidecar")
    g.add_argument("--forbid-under", type=Path, help="refuse any --out inside this tree (the shipped game)")
    g.add_argument("--anim-name", default="{animation}_{direction}")
    g.set_defaults(func=export_godot_cmd)
    return p


def profile_check(args: argparse.Namespace) -> int:
    problems = check_profile(args.file, check_paths=not args.no_path_check)
    for msg in problems:
        print(f"{args.file}: {msg}")
    if not problems:
        print(f"{args.file}: ok")
    return 2 if problems else 0


def profile_show(args: argparse.Namespace) -> int:
    spec = load_profile(args.file).character(args.character, cell=args.cell)
    print(json.dumps(dataclasses.asdict(spec), indent=2, default=str))
    return 0


def _parse_clip(spec: str) -> dict:
    name, start, frames, fps, mode, *lf = spec.split(":")
    if mode not in ("loop", "once") or len(lf) > 1:
        raise ValueError(f"clip {spec!r}: expected NAME:START:FRAMES:FPS:loop|once[:LOOP_FROM]")
    return {
        "name": name,
        "start": int(start),
        "frames": int(frames),
        "fps": float(fps) if "." in fps else int(fps),
        "loop": mode == "loop",
        "loop_from": int(lf[0]) if lf else None,
    }


def export_godot_cmd(args: argparse.Namespace) -> int:
    from PIL import Image

    from sonoranlab.export.godot import export_godot

    try:
        with Image.open(args.sheet) as im:
            sheet = np.asarray(im.convert("RGBA"))
        prov = json.loads(args.provenance.read_text()) if args.provenance else {}
        paths = export_godot(
            sheet,
            character=args.character,
            cell=(args.cell, args.cell),
            rows=args.rows.split(","),
            clips=[_parse_clip(c) for c in args.clip],
            out_dir=args.out,
            provenance=prov,
            forbid_under=args.forbid_under,
            anim_name=args.anim_name,
        )
    except (OSError, ValueError) as e:  # ValueError covers bad clips, bad JSON, refusals
        print(f"export godot: {e}", file=sys.stderr)
        return 2
    print(json.dumps(paths, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ProfileError as e:
        for msg in e.problems:
            print(f"{args.file}: {msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
