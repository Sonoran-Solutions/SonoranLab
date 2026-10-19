"""`sonoranlab <verb>`: the CLI every MCP tool wraps (CLAUDE.md). Exit codes: 0 ok, 1 validation failed, 2 usage/config.

A new verb is one `add_parser` block in `build_parser` plus its handler function below.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from sonoranlab.profile import ProfileError, check_profile, load_palette, load_profile, sha256_file
from sonoranlab.validate import read_frames, validate


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

    v = verbs.add_parser("validate", help="validate registered <direction>_<i>.png cells (docs/08); exit 1 on failure")
    v.add_argument("frames_dir", type=Path)
    v.add_argument("--profile", type=Path, required=True)
    v.add_argument("--character", required=True)
    v.add_argument("--anim", required=True, help="clip name in the character's animations (frame count, loop)")
    v.add_argument("--cell", type=int, help="cell size for a character without a profile row")
    v.add_argument("--out", type=Path, help="write the report here (never overwrites) instead of printing it")
    v.set_defaults(func=validate_frames)
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


def validate_frames(args: argparse.Namespace) -> int:
    try:
        prof = load_profile(args.profile)
        spec = prof.character(args.character, cell=args.cell)
        pal = load_palette(prof)
    except ProfileError as e:
        for msg in e.problems:
            print(f"{args.profile}: {msg}", file=sys.stderr)
        return 2
    anim = spec.animations.get(args.anim)
    if anim is None:
        print(f"{args.anim!r} is not a clip of {spec.name} ({', '.join(spec.animations)})", file=sys.stderr)
        return 2
    if args.out and args.out.exists():
        print(f"{args.out}: exists; reports are never overwritten", file=sys.stderr)
        return 2
    kw = {
        "cell": spec.cell,
        "frames": anim.frames,
        "directions": list(prof.directions.generate),
        "foot_y": spec.foot_y,
        "anchor_x": spec.anchor_x,
        "padding": spec.padding,
        "max_colours": prof.palette.max_colors_per_cell,
        "loop": anim.loop,
        "anchor_mode": spec.anchor_mode,
        "fringe_tolerance": prof.alpha.fringe_tolerance,
        "mirror": dict(prof.directions.mirror_animations),
    }
    try:
        clips, files = read_frames(args.frames_dir, prof.directions.order)
        rep = validate(clips, palette=pal, limits=spec.limits, limits_source=spec.limits_source, **kw)
    except (ValueError, OSError) as e:
        print(f"validate: {e}", file=sys.stderr)
        return 2
    rep = {
        "pass": rep["pass"],
        "character": spec.name,
        "animation": args.anim,
        **rep,
        "params": {"frames_dir": str(args.frames_dir.resolve()), **kw},
        "provenance": {
            "profile": {"path": str(args.profile.resolve()), "sha256": sha256_file(args.profile)},
            "inputs": {p.name: sha256_file(p) for p in files},
        },
    }
    text = json.dumps(rep, indent=1) + "\n"
    if args.out:
        try:
            with args.out.open("x") as fh:
                fh.write(text)
        except OSError as e:
            print(f"{args.out}: {e}", file=sys.stderr)
            return 2
        print(f"{args.out}: " + ("pass" if rep["pass"] else f"fail ({len(rep['failures'])} failures)"))
    else:
        print(text, end="")
    return 0 if rep["pass"] else 1


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
