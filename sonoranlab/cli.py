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

    ct = verbs.add_parser("contact", help="render a review contact sheet PNG from registered frames + a report")
    ct.add_argument("frames_dir", type=Path, help="directory of <direction>_<i>.png registered frames")
    ct.add_argument("report", type=Path, help="validator report.json")
    ct.add_argument("out", type=Path, help="contact sheet PNG to write (never overwritten)")
    ct.add_argument("--cell", type=int, required=True, help="cell edge in px (square)")
    ct.add_argument("--foot-y", type=int, required=True, help="foot line row (cell - 4)")
    ct.add_argument("--scale", type=int, default=8)
    ct.set_defaults(func=contact)
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


def contact(args: argparse.Namespace) -> int:
    from sonoranlab.contact import contact_sheet, load_clips

    try:
        if args.out.exists():
            raise FileExistsError(f"{args.out} exists: contact sheets are never overwritten")
        report = json.loads(args.report.read_text())
        img = contact_sheet(
            load_clips(args.frames_dir), report, cell=(args.cell, args.cell), foot_y=args.foot_y, scale=args.scale
        )
        with open(args.out, "xb") as f:
            img.save(f, "PNG")
    except (OSError, ValueError, KeyError) as e:  # ValueError covers bad JSON; KeyError a report without "pass"
        print(f"contact: {e!r}" if isinstance(e, KeyError) else f"contact: {e}", file=sys.stderr)
        return 2
    print(args.out)
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
