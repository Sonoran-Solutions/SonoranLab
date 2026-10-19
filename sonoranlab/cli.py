"""`sonoranlab <verb>`: the CLI every MCP tool wraps (CLAUDE.md). Exit codes: 0 ok, 1 validation failed, 2 usage/config.

A new verb is one `add_parser` block in `build_parser` plus its handler function below.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

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

    sl = verbs.add_parser("slice", help="cut a fixed-grid sheet into <direction>_<i>.png frames (never overwrites)")
    sl.add_argument("sheet", type=Path)
    sl.add_argument("out_dir", type=Path, help="must not exist")
    sl.add_argument("--cell", type=int, required=True, help="square cell edge in px")
    sl.add_argument("--rows", required=True, help="comma-separated direction names, top row first")
    sl.add_argument("--frames", type=int, required=True)
    sl.add_argument("--start-col", type=int, default=0, help="first clip column (1 for the game layout)")
    sl.set_defaults(func=slice_cmd)
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


def slice_cmd(args: argparse.Namespace) -> int:
    import numpy as np
    from PIL import Image

    from sonoranlab.slice import slice_sheet

    try:
        if args.out_dir.exists():
            raise ValueError(f"{args.out_dir}: already exists; slice never overwrites, pick a new directory")
        rows = [r for r in args.rows.split(",") if r]
        with Image.open(args.sheet) as im:
            sheet = np.asarray(im.convert("RGBA"))
        clips = slice_sheet(sheet, (args.cell, args.cell), rows, args.frames, start_col=args.start_col)
        args.out_dir.mkdir(parents=True)
        for d, fs in clips.items():
            for i, f in enumerate(fs):
                Image.fromarray(f).save(args.out_dir / f"{d}_{i}.png")
    except (ValueError, OSError) as e:
        print(f"slice: {e}", file=sys.stderr)
        return 2
    print(f"{args.out_dir}: {sum(map(len, clips.values()))} frames in {len(clips)} directions")
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
