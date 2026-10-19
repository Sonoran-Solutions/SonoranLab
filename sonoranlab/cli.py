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

    px = verbs.add_parser("pixelize", help="downscale one frame with CPU PixelOE")
    px.add_argument("input", type=Path, metavar="IN.png")
    px.add_argument("output", type=Path, metavar="OUT.png")
    px.add_argument("--scale", type=int, required=True)
    px.add_argument("--method", choices=("k_centroid", "contrast", "nearest"), default="k_centroid")
    px.add_argument("--thickness", type=int, default=0)
    px.add_argument("--bg", default="#ff00ff", help="#rrggbb background for alpha compositing")
    px.set_defaults(func=pixelize_frame)

    sl = verbs.add_parser("slice", help="cut a fixed-grid sheet into <direction>_<i>.png frames (never overwrites)")
    sl.add_argument("sheet", type=Path)
    sl.add_argument("out_dir", type=Path, help="must not exist")
    sl.add_argument("--cell", type=int, required=True, help="square cell edge in px")
    sl.add_argument("--rows", required=True, help="comma-separated direction names, top row first")
    sl.add_argument("--frames", type=int, required=True)
    sl.add_argument("--start-col", type=int, default=0, help="first clip column (1 for the game layout)")
    sl.set_defaults(func=slice_cmd)

    ct = verbs.add_parser("contact", help="render a review contact sheet PNG from registered frames + a report")
    ct.add_argument("frames_dir", type=Path, help="directory of <direction>_<i>.png registered frames")
    ct.add_argument("report", type=Path, help="validator report.json")
    ct.add_argument("out", type=Path, help="contact sheet PNG to write (never overwritten)")
    ct.add_argument("--cell", type=int, required=True, help="cell edge in px (square)")
    ct.add_argument("--foot-y", type=int, required=True, help="foot line row (cell - 4)")
    ct.add_argument("--scale", type=int, default=8)
    ct.set_defaults(func=contact)

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


def pixelize_frame(args: argparse.Namespace) -> int:
    """Read one frame, composite alpha over the key, and exclusively create an RGB PNG."""
    import re

    import numpy as np
    from PIL import Image, ImageColor

    from sonoranlab.pixelize import pixelize

    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError("OUT must differ from IN; raw inputs cannot be overwritten")
        if args.output.exists():
            raise ValueError(f"OUT already exists: {args.output}")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", args.bg):
            raise ValueError("--bg must be a #rrggbb colour")
        bg = np.array(ImageColor.getrgb(args.bg), np.float32)
        with Image.open(args.input) as im:
            rgba = np.asarray(im.convert("RGBA")).astype(np.float32)
        weight = rgba[..., 3:] / 255
        rgb = np.rint(rgba[..., :3] * weight + bg * (1 - weight)).astype(np.uint8)
        result = pixelize(rgb, args.scale, method=args.method, thickness=args.thickness)
        with args.output.open("xb") as out:
            Image.fromarray(result).save(out, format="PNG")
    except (OSError, ValueError) as e:
        print(f"pixelize: {e}", file=sys.stderr)
        return 2
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
