"""`sonoranlab <verb>`: the CLI every MCP tool wraps (CLAUDE.md). Exit codes: 0 ok, 1 validation failed, 2 usage/config.

A new verb is one `add_parser` block in `build_parser` plus its handler function below.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from sonoranlab.profile import ProfileError, check_profile, load_palette, load_profile, sha256_file
from sonoranlab.validate import frame_digest, read_frames, read_stills, validate


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
    g.add_argument("--profile", type=Path, required=True)
    g.add_argument("--character", required=True)
    g.add_argument("--cell", type=int, required=True, help="square cell edge in pixels")
    g.add_argument("--rows", required=True, help="comma-separated directions, top row first (profile order)")
    g.add_argument(
        "--clip",
        action="append",
        required=True,
        metavar="NAME:START:FRAMES:FPS:loop|once[:LOOP_FROM]",
        help="one clip per validated report",
    )
    g.add_argument("--out", type=Path, required=True, help="writes OUT/<character>/; must not already hold files")
    g.add_argument("--provenance", type=Path, help="JSON object copied into the sidecar")
    g.add_argument("--report", type=Path, help="required: validator report JSON with pass: true")
    g.add_argument("--forbid-under", type=Path, help="refuse any --out inside this tree (the shipped game)")
    g.add_argument("--anim-name", default="{animation}_{direction}")
    g.set_defaults(func=export_godot_cmd)

    v = verbs.add_parser("validate", help="validate registered <direction>_<i>.png cells (docs/08); exit 1 on failure")
    v.add_argument("frames_dir", type=Path)
    v.add_argument("--profile", type=Path, required=True)
    v.add_argument("--character", required=True)
    v.add_argument("--anim", required=True, help="clip name in the character's animations (frame count, loop)")
    v.add_argument("--cell", type=int, help="cell size for a character without a profile row")
    v.add_argument("--out", type=Path, help="write the report here (never overwrites) instead of printing it")
    v.add_argument("--stills", type=Path, help="dir with final-resolution still_<direction>.png cells")
    v.add_argument("--registration", type=Path, help="registration dict JSON from a pipeline report or manifest")
    v.set_defaults(func=validate_frames)

    pr = verbs.add_parser(
        "process", help="raw HD frames -> validated job dir -> Godot export on PASS (exit 0 pass, 1 fail, 2 error)"
    )
    pr.add_argument("raw", type=Path, help="dir of <direction>_<i>.png HD frames, or with --sheet one raw sheet PNG")
    pr.add_argument("--profile", type=Path, required=True)
    pr.add_argument("--character", required=True)
    pr.add_argument("--anim", required=True, help="clip name in the character's animations")
    pr.add_argument("--out", type=Path, required=True, help="job dir: must not exist or be empty (never overwritten)")
    pr.add_argument("--stills", type=Path, help="dir with still_<direction>.png (cell size) for sheet column 0")
    pr.add_argument("--sheet", action="store_true", help="RAW is a sheet: rows in profile order, clip from --start-col")
    pr.add_argument("--start-col", type=int, help="first clip column of a --sheet (default 1, the game layout)")
    pr.add_argument("--anchors", type=Path, help="per-frame rig anchors (refused until the validator gates rig)")
    pr.add_argument("--godot-check", action="store_true", help="import + check the export in Godot (needs godot)")
    pr.set_defaults(func=process_cmd)
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
        rows = args.rows.split(",")
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
    from sonoranlab.slice import slice_sheet

    try:
        try:
            report_bytes = args.report.read_bytes() if args.report else b"null"
            report = json.loads(report_bytes)
        except (OSError, ValueError) as e:
            raise ValueError("export needs a passing validator report") from e
        if not isinstance(report, dict) or report.get("pass") is not True:
            raise ValueError("export needs a passing validator report")
        prof = load_profile(args.profile)
        spec = prof.character(args.character, cell=args.cell)
        rows = list(prof.directions.order)
        if args.rows.split(",") != rows or (args.cell, args.cell) != spec.cell:
            raise ValueError("--rows and --cell must match the profile; export refused")
        if len(args.clip) != 1:
            raise ValueError("export needs a single --clip matching the validated report; export refused")
        clip = _parse_clip(args.clip[0])
        if report.get("character") != args.character:
            raise ValueError("report character does not match --character; export refused")
        if report.get("animation") != clip["name"]:
            raise ValueError("report animation does not match --clip; export refused")
        if clip["start"] < 1:
            raise ValueError("--clip must start after still column 0; export refused")
        if not isinstance(report.get("digests"), dict) or not report["digests"]:
            raise ValueError("report has no frame digests; export refused")
        remaining = dict(report["digests"])
        with Image.open(args.sheet) as im:
            sheet = np.asarray(im.convert("RGBA"))
        cells = slice_sheet(sheet, spec.cell, rows, clip["frames"], start_col=clip["start"])

        def bind(key: str, f: np.ndarray, tag: str) -> None:
            digest = frame_digest(f)
            if remaining.pop(key, None) != digest:
                raise ValueError(f"sheet {tag} does not match the validated report (digest {digest}); export refused")

        for d in rows:
            if d in prof.directions.generate:
                for i, f in enumerate(cells[d]):
                    bind(f"{d}_{i}", f, f"{d} frame {i}")
        for d, src in prof.directions.mirror_animations.items():
            for i, f in enumerate(cells[d]):
                if not np.array_equal(f, cells[src][i][:, ::-1]):
                    raise ValueError(f"sheet {d} frame {i} does not mirror {src}; export refused")
                if (key := f"{d}_{i}") in remaining:
                    bind(key, f, f"{d} frame {i}")
        cw, ch = spec.cell
        stills = {}
        for r, d in enumerate(rows):
            f = sheet[r * ch : (r + 1) * ch, :cw]
            if (f[..., 3] > 0).any():
                bind(f"still_{d}", f, f"still {d}")
                stills[d] = f
        if remaining:
            raise ValueError(f"report digest {next(iter(remaining))} has no matching sheet cell; export refused")
        if (sheet[:, cw : clip["start"] * cw, 3] > 0).any():
            raise ValueError("sheet has populated columns outside --clip and still column 0; export refused")
        still_report = validate(
            {},
            stills=stills,
            cell=spec.cell,
            frames=0,
            directions=rows,
            palette=load_palette(prof),
            foot_y=spec.foot_y,
            anchor_x=spec.anchor_x,
            padding=spec.padding,
            limits=spec.limits,
            max_colours=prof.palette.max_colors_per_cell,
            loop=False,
            anchor_mode=spec.anchor_mode,
            fringe_tolerance=prof.alpha.fringe_tolerance,
        )
        if not still_report["pass"]:
            raise ValueError(f"{still_report['failures'][0]}; export refused")
        prov = json.loads(args.provenance.read_text()) if args.provenance else {}
        if not isinstance(prov, dict):
            raise ValueError("provenance must be a JSON object")
        prov["validator"] = {
            "pass": True,
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "character": args.character,
            "animation": clip["name"],
            "frames_bound": len(report["digests"]),
        }
        paths = export_godot(
            sheet,
            character=args.character,
            cell=(args.cell, args.cell),
            rows=rows,
            clips=[clip],
            out_dir=args.out,
            provenance=prov,
            forbid_under=args.forbid_under or prof.export.godot_project,
            anim_name=args.anim_name,
        )
    except (OSError, ValueError) as e:  # ValueError covers bad clips, bad JSON, refusals
        print(f"export godot: {e}", file=sys.stderr)
        return 2
    print(json.dumps(paths, indent=2))
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
        stills, still_files = read_stills(args.stills, prof.directions.order) if args.stills else ({}, [])
        if args.stills and not still_files:
            raise ValueError(f"{args.stills}: no still_<direction>.png cells")
        files += still_files
        registration = json.loads(args.registration.read_text()) if args.registration else None
        if args.registration and (
            not isinstance(registration, dict)
            or any(
                d not in clips
                or not isinstance(info, dict)
                or not isinstance(info.get("clipped_px"), list)
                or len(info["clipped_px"]) != anim.frames
                or any(type(n) is not int or n < 0 for n in info["clipped_px"])
                for d, info in registration.items()
            )
        ):
            raise ValueError("registration must map clip directions to per-frame non-negative clipped_px counts")
        rep = validate(
            clips,
            stills=stills,
            palette=pal,
            limits=spec.limits,
            limits_source=spec.limits_source,
            registration=registration,
            **kw,
        )
        if not args.registration:
            rep["warnings"].append("registration provenance not supplied: clipping off the cell cannot be checked")
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


def process_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.pipeline import PipelineError, run

    try:
        res = run(
            args.raw,
            profile=args.profile,
            character=args.character,
            anim=args.anim,
            out=args.out,
            stills=args.stills,
            sheet=args.sheet,
            start_col=args.start_col,
            anchors=args.anchors,
            godot_check=args.godot_check,
        )
    except (PipelineError, OSError) as e:
        print(f"process: {e}", file=sys.stderr)
        return 2
    tag = f"{args.character} {args.anim}"
    if res["exit"] == 1:
        fails = res["failures"]
        lines = [f"FAIL {tag}: {len(fails)} validator failure(s); no export written", *(f"  - {m}" for m in fails[:8])]
        if len(fails) > 8:
            lines.append(f"  ... {len(fails) - 8} more")
        lines += [f"report:  {res['report']}", f"contact: {res['contact']}"]
        print("\n".join(lines), file=sys.stderr)
        return 1
    gc = res["godot_check"]
    print(f"PASS {tag}: {len(res['warnings'])} warning(s)")
    for w in res["warnings"]:
        print(f"  ~ {w}")
    print(f"job:     {res['job']}\nreport:  {res['report']}\ncontact: {res['contact']}")
    print(f"export:  {Path(res['export']['png']).parent}")
    if gc:
        n = gc["result"].get("animations_checked")
        print(f"godot:   {gc['godot']}: import + SpriteFrames check passed ({n} animations)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ProfileError as e:
        src = getattr(args, "file", None) or getattr(args, "profile", "profile")
        for msg in e.problems:
            print(f"{src}: {msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
