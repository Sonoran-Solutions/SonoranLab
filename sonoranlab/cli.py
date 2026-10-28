"""`sonoranlab <verb>`: the CLI every MCP tool wraps (CLAUDE.md). Exit codes: 0 ok, 1 validation failed, 2 usage/config.

A new verb is one `add_parser` block in `build_parser` plus its handler function below.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import numpy as np

from sonoranlab.profile import ProfileError, check_profile, load_palette, load_profile, sha256_file
from sonoranlab.validate import frame_digest, read_frames, read_stills, validate

T = TypeVar("T")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sonoranlab", description=__doc__.splitlines()[0])
    verbs = p.add_subparsers(dest="verb", required=True)

    api = verbs.add_parser("api", help="call the shared tool registry and print its JSON envelope")
    api.add_argument("tool", nargs="?")
    api.add_argument("--list", action="store_true", dest="list_tools")
    api.add_argument("--params", metavar="FILE|-", help="JSON object (default: empty object)")
    api.set_defaults(func=api_cmd)
    mcp = verbs.add_parser("mcp", help="local MCP transport").add_subparsers(dest="action", required=True)
    mcp.add_parser("serve", help="serve MCP on stdio").set_defaults(func=mcp_serve_cmd)

    owner_registry = verbs.add_parser("registry", help="owner-only reference approval").add_subparsers(
        dest="action", required=True
    )
    approve = owner_registry.add_parser("approve-references", help="owner only; agents must never run this command")
    for name in ("project", "character", "ref-set", "reviewer", "evidence"):
        approve.add_argument("--" + name, required=True)
    approve.set_defaults(func=registry_approve_cmd)

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

    cr = verbs.add_parser("classreq", help="class-request intake (ADR-0011)").add_subparsers(
        dest="action", required=True
    )
    cc = cr.add_parser("check", help="check a class request + eight stills; exit 0 ready, 1 not ready, 2 usage")
    cc.add_argument("request", type=Path, help="sonoranlab.class_request/1 YAML or JSON")
    cc.add_argument("--profile", type=Path, required=True)
    cc.add_argument("--stills", type=Path, help="dir of still_<direction>.png (default: the request's stills_dir)")
    cc.add_argument("--state", type=Path, help="STATE root: read-only registry lookup for the character id")
    cc.add_argument("--out", type=Path, help="write the JSON here (never overwritten) instead of printing it")
    cc.add_argument("--contact", type=Path, help="write a 4x review contact sheet PNG (never overwritten)")
    cc.set_defaults(func=classreq_check_cmd)
    cp = cr.add_parser("propose-row", help="print a reviewable characters.<id> row; never edits the profile")
    cp.add_argument("request", type=Path)
    cp.add_argument("--profile", type=Path, required=True)
    cp.add_argument("--cell", type=int, required=True)
    cp.add_argument("--out", type=Path, help="write the snippet here (never overwritten)")
    cp.set_defaults(func=classreq_propose_cmd)

    cg = verbs.add_parser("campaign", help="resumable class campaign ledger (DYL-129); prints the API envelope")
    cga = cg.add_subparsers(dest="action", required=True)
    for name, help_text in CAMPAIGN_VERBS.items():
        sub = cga.add_parser(name, help=help_text)
        if name == "create":
            sub.add_argument("--project", required=True)
            sub.add_argument("--request", type=Path, required=True, help="sonoranlab.class_request/1 YAML or JSON")
            sub.add_argument("--reference", required=True, dest="reference_id", help="approved refs-<12 hex> set")
            sub.add_argument("--revise", metavar="CAMPAIGN_ID", help="freeze revision N+1 of this campaign")
            sub.add_argument("--max-seeds", type=int, dest="max_seeds_per_clip", help="stricter owner budget")
            sub.add_argument("--budget-decision-ref", help="owner decision reference for --max-seeds")
        else:
            sub.add_argument("--campaign", required=True, dest="campaign_id")
        if name in {"submit", "escalate"}:
            sub.add_argument("--clip", required=True)
        if name == "submit":
            sub.add_argument("--seed", type=int)
        if name == "decide":
            sub.add_argument("--job", required=True, dest="job_id")
            sub.add_argument("--decision", required=True, choices=("selected", "rejected"))
        if name in {"decide", "escalate"}:
            sub.add_argument("--reason", required=True)
        if name == "note":
            sub.add_argument("--text")
            sub.add_argument("--cost-source")
            sub.add_argument("--cost-units")
            sub.add_argument("--cost-amount", type=float)
        sub.set_defaults(func=campaign_cmd)
    od = cga.add_parser("owner-decision", help="OWNER ONLY (never an MCP tool); agents must never run this command")
    od.add_argument("--campaign", required=True, dest="campaign_id")
    od.add_argument(
        "--kind",
        required=True,
        choices=("budget_grant", "resume", "delivery", "install", "profile_row", "reference_approval", "note"),
    )
    od.add_argument("--verbatim", required=True, help="the owner's words, recorded verbatim")
    od.add_argument("--ref", help="job id / attempt key (resume), bundle (delivery, install), decision reference")
    od.add_argument("--clip", help="budget_grant clip")
    od.add_argument("--seeds", type=int, help="budget_grant: seeds ADDED to the clip budget")
    od.add_argument("--state", choices=("staged_and_engine_verified", "ready_to_install", "installed"))
    od.add_argument("--activity", choices=("preparation", "cleanup", "review"), help="owner time record")
    od.add_argument("--seconds", type=float, help="owner time for --activity")
    od.set_defaults(func=campaign_owner_cmd)

    rf = verbs.add_parser("refprep", help="HD turnaround -> candidate stills (deterministic, no model; ADR-0011)")
    rf.add_argument("--profile", type=Path, required=True)
    rf.add_argument("--request", type=Path, required=True)
    rf.add_argument("--turnaround", type=Path, help="HD grid image; optional only when --view gives all directions")
    rf.add_argument("--grid", default="4x2", help="COLSxROWS of the turnaround (default 4x2)")
    rf.add_argument("--order", help="comma-separated directions, row-major (default: profile directions.order)")
    rf.add_argument(
        "--view", action="append", default=[], metavar="DIRECTION=IMG", help="replace one view with a separate HD image"
    )
    rf.add_argument("--out", type=Path, required=True, help="new directory; refuses to exist")
    rf.add_argument("--cell", type=int, help="cell size (default: profile row, else the smallest fitting)")
    rf.add_argument("--bg-key", help="#rrggbb background key (default: profile alpha.bg_key)")
    rf.add_argument("--despill-px", type=int, help="magenta despill band in HD px (default: profile processing)")
    rf.set_defaults(func=refprep_cmd)

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

    gn = verbs.add_parser("generate", help="run one generation request (Track E: a Wan clip) against ComfyUI")
    gn.add_argument("--request", type=Path, required=True, help="GenerateRequest JSON (sonoranlab.comfy.adapters.base)")
    gn.add_argument("--base-url", default="http://127.0.0.1:8189")
    gn.add_argument("--timeout", type=float, default=1800, help="seconds to wait for the ComfyUI prompt")
    gn.add_argument("--need-gib", type=float, help="run-window memory budget (GiB), at least the track minimum")
    gn.add_argument("--allow-pause", action="append", default=[], metavar="UNIT", help="owner-approved units only")
    gn.add_argument(
        "--exec",
        dest="exec_container",
        metavar="CONTAINER",
        help="reach ComfyUI through `podman exec` (the `env/comfy.sh up --offline` container, e.g. sonoranlab-comfy)",
    )
    gn.set_defaults(func=generate_cmd)

    sw = verbs.add_parser("sweep", help="plan generation or rank clip/loop candidates").add_subparsers(
        dest="action", required=True
    )
    sr = sw.add_parser("rank", help="score directions, assemble the best and validate the full set")
    sr.add_argument("--profile", type=Path, required=True)
    sr.add_argument("--character", required=True)
    sr.add_argument("--anim", required=True)
    sr.add_argument("--attempts", type=Path, nargs="+", required=True)
    sr.add_argument("--carrier", type=Path, required=True, help="HD <direction>_<i>.png frames for other directions")
    sr.add_argument("--out", type=Path, required=True)
    sr.add_argument("--windows", choices=("default", "steady"), default="default")
    sr.add_argument("--stills", type=Path, help="dir with cell-size still_<direction>.png for final sheet column 0")
    sr.set_defaults(func=sweep_rank_cmd)
    sp = sw.add_parser("plan", help="write Track E requests without running generation")
    sp.add_argument("--character", required=True)
    sp.add_argument("--anim", required=True)
    sp.add_argument("--directions", nargs="+", required=True)
    sp.add_argument("--seeds", nargs="+", type=int, required=True)
    sp.add_argument("--stills-dir", type=Path, required=True)
    sp.add_argument("--out", type=Path, required=True)
    sp.add_argument("--background", default="#808080")
    sp.set_defaults(func=sweep_plan_cmd)
    sg = sw.add_parser("run", help="run requests serially through the run-window guard")
    sg.add_argument("--requests", nargs="+", type=Path, required=True)
    sg.add_argument(
        "--need-gib", type=float, default=None, help="run-window GiB; default is the per-track minimum (E 40 / C 60)"
    )
    sg.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    sg.add_argument("--exec", dest="exec_container", metavar="CONTAINER")
    sg.set_defaults(func=sweep_run_cmd)

    an = verbs.add_parser(
        "animate", help="reference stills -> Track E generation -> sweep -> validated export (exit 0/1/2, 3 guard)"
    )
    an.add_argument("--profile", type=Path, required=True)
    an.add_argument("--character", required=True)
    an.add_argument(
        "--reference",
        type=Path,
        required=True,
        help="dir with cell-size still_<direction>.png with alpha for every row",
    )
    an.add_argument("--anim", default="run")
    an.add_argument("--track", default="E", help="E only for now")
    an.add_argument("--seeds", type=_seeds, default=(1,), help="comma-separated, e.g. 1,2")
    an.add_argument("--out", type=Path, required=True, help="new directory; never overwritten")
    an.add_argument("--exec", dest="exec_container", metavar="CONTAINER", help="e.g. sonoranlab-comfy")
    an.add_argument("--need-gib", type=float, default=40)
    an.add_argument("--windows", choices=("default", "steady"), default="default")
    an.add_argument("--godot-check", action="store_true", help="import + check the export in Godot (needs godot)")
    an.set_defaults(func=animate_cmd)

    rv = verbs.add_parser("review", help="record human production review").add_subparsers(dest="action", required=True)
    rr = rv.add_parser("record", help="exclusively write review.json in a technically passing job")
    rr.add_argument("job", type=Path)
    rr.add_argument("--verdict", choices=("pass", "fail"), required=True)
    rr.add_argument("--reviewer", required=True)
    rr.add_argument("--cleanup-minutes", type=float)
    rr.add_argument("--started", required=True)
    rr.add_argument("--ended", required=True)
    rr.add_argument("--notes", default="")
    rr.add_argument("--supersedes", type=Path, help="earlier review file this replaces (move it aside first)")
    rr.add_argument("--carried-from", help="free-text provenance of an approval given before binding existed")
    rr.set_defaults(func=review_record_cmd)
    rc = rv.add_parser("check", help="exit 0 only if the job is production usable (bound review still matches)")
    rc.add_argument("job", type=Path)
    rc.set_defaults(func=review_check_cmd)

    rp = verbs.add_parser("repair", help="isolated repair workspace and lossless Aseprite interchange")

    def repair_error(message: str) -> None:
        print(json.dumps({"ok": False, "error": "usage_error", "message": message}))
        rp.exit(2)

    def repair_cmd(args: argparse.Namespace) -> int:
        if args.repair_action == "finalize":
            from sonoranlab.repair.cli import run

            return run(args)
        from sonoranlab.repair.commands import run

        return run(args)

    rp.error = repair_error
    parse_known = p.parse_known_args

    def repair_parse_known(
        args: list[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> tuple[argparse.Namespace, list[str]]:
        parsed, extra = parse_known(args, namespace)
        if parsed.verb == "repair" and extra:
            repair_error("unrecognized arguments: " + " ".join(extra))
        return parsed, extra

    p.parse_known_args = repair_parse_known
    repair_actions = rp.add_subparsers(dest="repair_action", required=True)
    for action in (
        "prepare",
        "export",
        "diff",
        "inspect",
        "abort",
        "authorize",
        "op",
        "read",
        "session",
        "relay",
        "finalize",
    ):
        command = repair_actions.add_parser(action)
        command.error = repair_error
        command.set_defaults(func=repair_cmd)
        if action == "prepare":
            command.add_argument("parent_job", type=Path)
            command.add_argument("--profile", type=Path, required=True)
            command.add_argument("--character", required=True)
            command.add_argument("--anim", required=True)
            command.add_argument("--out", type=Path, required=True)
        elif action == "session":
            sessions = command.add_subparsers(dest="session_action", required=True)
            for operation in ("start", "stop", "status", "checkpoint"):
                sub = sessions.add_parser(operation)
                sub.error = repair_error
                sub.add_argument("repair", type=Path)
                if operation == "start":
                    sub.add_argument("--detach", action="store_true")
                elif operation == "stop":
                    sub.add_argument("--checkpoint", action="store_true")
        else:
            command.add_argument("repair", type=Path)
            if action == "relay":
                command.add_argument("--agent-dir", type=Path, required=True)
            if action == "finalize":
                command.add_argument("--out", type=Path, required=True)
            if action in ("authorize", "op", "read"):
                command.add_argument("--spec", type=Path, required=True)
    return p


def api_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.api import call, registry
    from sonoranlab.api.registry import ToolError, failure

    if args.list_tools:
        if args.tool or args.params:
            envelope = failure(args.tool or "", ToolError("invalid_params", "--list takes no tool or parameters"))
        else:
            print(json.dumps([tool.summary() for tool in registry().values()]))
            return 0
    elif not args.tool:
        envelope = failure("", ToolError("invalid_params", "A tool name or --list is required", field="tool"))
    else:
        try:

            def reject_constant(value: str) -> None:
                raise ValueError(f"Non-finite JSON number: {value}")

            text = sys.stdin.read() if args.params == "-" else Path(args.params).read_text() if args.params else "{}"
            params = json.loads(text, parse_constant=reject_constant)
        except (OSError, ValueError) as exc:
            envelope = failure(args.tool, ToolError("invalid_params", str(exc), field="params"))
        else:
            envelope = call(args.tool, params)
    print(json.dumps(envelope))
    return 0 if envelope["ok"] else 2 if envelope["error"]["code"] == "invalid_params" else 1


def mcp_serve_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.mcp.server import main

    return main()


def registry_approve_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.api.registry import Context, ToolError
    from sonoranlab.registry import approve_references

    try:
        result = approve_references(
            args.project, args.character, args.ref_set, args.reviewer, args.evidence, ctx=Context()
        )
    except (ToolError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


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


def export_godot_checked(args: argparse.Namespace, *, sheet_pixels=None, write: bool = True) -> dict:
    """Shared CLI/API admission: profile, clip, still and pixel bindings."""
    from PIL import Image

    from sonoranlab.export.godot import export_godot
    from sonoranlab.slice import slice_sheet

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
    bound = report.get("provenance") if isinstance(report.get("provenance"), dict) else {}
    bound = bound.get("profile") if isinstance(bound.get("profile"), dict) else {}
    report_sha, profile_sha = bound.get("sha256"), sha256_file(args.profile)
    if not isinstance(report_sha, str) or not report_sha:
        raise ValueError("report has no profile provenance; export refused")
    if report_sha != profile_sha:
        raise ValueError(f"report was validated with a different profile (sha256 {report_sha}); export refused")
    anim_spec = spec.animations.get(clip["name"])
    if anim_spec is None:
        raise ValueError(
            f"--clip {clip['name']!r} is not a clip of {spec.name} ({', '.join(spec.animations)}); export refused"
        )
    for field in ("frames", "fps", "loop", "loop_from"):
        want, got = getattr(anim_spec, field), clip[field]
        if got != want:  # frames/fps/loop equal numerically; None == None for loop_from
            raise ValueError(
                f"--clip {field} {got} does not match the profile's {clip['name']} {field} {want}; export refused"
            )
    if clip["start"] < 1:
        raise ValueError("--clip must start after still column 0; export refused")
    if not isinstance(report.get("digests"), dict) or not report["digests"]:
        raise ValueError("report has no frame digests; export refused")
    remaining = dict(report["digests"])
    if sheet_pixels is None:
        with Image.open(args.sheet) as im:
            sheet = np.asarray(im.convert("RGBA"))
    else:
        sheet = sheet_pixels
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
    # Rows absent from the report are derived mirrors, checked pixel-for-pixel above.
    checked = validate(
        {d: fs for d, fs in cells.items() if f"{d}_0" in report["digests"]},
        stills=stills,
        cell=spec.cell,
        frames=anim_spec.frames,
        directions=list(prof.directions.generate),
        palette=load_palette(prof),
        foot_y=spec.foot_y,
        anchor_x=spec.anchor_x,
        padding=spec.padding,
        limits=spec.limits,
        max_colours=prof.palette.max_colors_per_cell,
        loop=anim_spec.loop,
        anchor_mode=spec.anchor_mode,
        fringe_tolerance=prof.alpha.fringe_tolerance,
        mirror=dict(prof.directions.mirror_animations),
        registration=report.get("registration"),
        limits_source=spec.limits_source,
    )
    if not checked["pass"]:
        raise ValueError(f"{checked['failures'][0]}; export refused")
    if checked["digests"] != report["digests"]:
        raise ValueError("revalidated frame digests differ from the report; export refused")
    prov = json.loads(args.provenance.read_text()) if args.provenance else {}
    if not isinstance(prov, dict):
        raise ValueError("provenance must be a JSON object")
    prov["validator"] = {
        "pass": True,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "character": args.character,
        "animation": clip["name"],
        "frames_bound": len(report["digests"]),
        "profile_sha256": profile_sha,
    }
    if not write:
        return {"sheet": sheet, "clips": [clip], "provenance": prov}
    paths = export_godot(
        sheet,
        character=args.character,
        cell=(args.cell, args.cell),
        rows=rows,
        clips=[clip],
        out_dir=args.out,
        provenance=prov,
        forbid_under=[prof.export.godot_project, *([args.forbid_under] if args.forbid_under else [])],
        anim_name=args.anim_name,
    )
    return paths


def export_godot_cmd(args: argparse.Namespace) -> int:
    try:
        paths = export_godot_checked(args)
    except (OSError, ValueError) as e:  # ValueError covers bad clips, bad JSON, refusals
        print(f"export godot: {e}", file=sys.stderr)
        return 2
    print(json.dumps(paths, indent=2))
    return 0


def validation_report(args: argparse.Namespace, *, loaded=None) -> dict:
    """Validate final-resolution cells, with the CLI's stills and registration checks."""
    if loaded is None:
        prof = load_profile(args.profile)
        spec = prof.character(args.character, cell=args.cell)
        pal = load_palette(prof)
        anim = spec.animations.get(args.anim)
        if anim is None:
            raise ValueError(f"{args.anim!r} is not a clip of {spec.name} ({', '.join(spec.animations)})")
    else:
        prof, spec, pal, anim = loaded
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
    return rep


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
    try:
        rep = validation_report(args, loaded=(prof, spec, pal, anim))
    except (ValueError, OSError) as e:
        print(f"validate: {e}", file=sys.stderr)
        return 2
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


_exec = os.execvp


def _cancel_on_term(fn: Callable[[], T]) -> T:
    """env/runwindow.py forwards TERM/HUP to its child. Python's default for those exits without unwinding, which would
    leave our prompt queued or running; as KeyboardInterrupt the client cancels it (prompt-scoped) and records why."""

    def interrupt(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    old = {s: signal.signal(s, interrupt) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return fn()
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def generate_cmd(args: argparse.Namespace) -> int:
    """Exit 0 ok, 1 generation failed, 2 bad request/config, 3 invalid window. The comfy client loads lazily."""
    import math

    from sonoranlab.comfy.adapters.base import GenerateRequest
    from sonoranlab.comfy.guard import guarded_argv, inside_guard, min_need_gib

    try:
        req = GenerateRequest.from_json(json.loads(args.request.read_text()))
        minimum = min_need_gib(req.track)
        if args.need_gib is not None and (not math.isfinite(args.need_gib) or args.need_gib < minimum):
            raise ValueError(f"refusing an under-budget run window; Track {req.track} needs {minimum:g} GiB")
        need = args.need_gib or minimum
        if "SONORANLAB_RUNWINDOW" in os.environ:
            if not inside_guard(need):
                print(
                    f"generate: REFUSED: invalid, stale or undersized run-window lease; "
                    f"Track {req.track} requires {need:g} GiB",
                    file=sys.stderr,
                )
                return 3
        else:
            child = ["--request", str(args.request), "--base-url", args.base_url, "--timeout", f"{args.timeout:g}"]
            child += ["--need-gib", f"{need:g}"] + (["--exec", args.exec_container] if args.exec_container else [])
            # --allow-pause belongs to the guard, not the child; the child sees it only through the run window
            argv = guarded_argv(
                [sys.executable, "-m", "sonoranlab.cli", "generate", *child], need, tuple(args.allow_pause)
            )
            print(f"generate: entering env/runwindow.py --need-gib {need:g}", file=sys.stderr, flush=True)
            _exec(argv[0], argv)
            return 0  # exec only returns in tests
        from sonoranlab.comfy.adapters import track_c, track_e  # lazy: only inside the guard

        work = track_e.default_work()
        if req.track == "E":
            client = track_e.make_client(args.base_url, work / "output", exec_container=args.exec_container)
            adapter = track_e.TrackE(client, work_root=work, timeout_s=args.timeout)
        elif req.track == "C":
            client = track_c.make_client(args.base_url, work / "output", exec_container=args.exec_container)
            adapter = track_c.TrackC(client, work_root=work, timeout_s=args.timeout)
        else:
            raise ValueError(f"track {req.track!r} has no adapter yet")
        res = _cancel_on_term(lambda: adapter.generate(req))
    except (OSError, ValueError, TypeError, KeyError) as e:  # ValueError covers bad JSON; TypeError/KeyError bad fields
        print(f"generate: {e!r}" if isinstance(e, (KeyError, TypeError)) else f"generate: {e}", file=sys.stderr)
        return 2
    print(json.dumps(res.to_json(), indent=2))
    return 0 if res.status == "ok" else 1


def sweep_rank_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.pipeline import PipelineError
    from sonoranlab.sweep import run_sweep

    try:
        result = run_sweep(
            args.attempts,
            profile=args.profile,
            character=args.character,
            anim=args.anim,
            carrier=args.carrier,
            out=args.out,
            windows=args.windows,
            stills=args.stills,
        )
    except (OSError, ValueError, KeyError, TypeError, PipelineError) as e:
        print(f"sweep rank: {e}", file=sys.stderr)
        return 2
    print(args.out / "sweep.json")
    if not result["technical_pass"]:
        for reason in result["final_verdict"]["failures"]:
            print(reason, file=sys.stderr)
    return 0 if result["technical_pass"] else 1


def sweep_plan_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.sweep import plan_generation

    try:
        paths = plan_generation(
            args.character, args.anim, args.directions, args.seeds, args.stills_dir, args.out, args.background
        )
    except (OSError, ValueError) as e:
        print(f"sweep plan: {e}", file=sys.stderr)
        return 2
    print(json.dumps([str(p) for p in paths], indent=2))
    return 0


def sweep_run_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.sweep import run_generation

    try:
        result = run_generation(
            args.requests, need_gib=args.need_gib, repo=args.repo, exec_container=args.exec_container
        )
    except (OSError, ValueError) as e:
        print(f"sweep run: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return result["stop_exit"] or int(any(r["exit"] != 0 for r in result["requests"]))


def _seeds(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: expected comma-separated integers") from None


def animate_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.animate import animate

    res = animate(
        profile=args.profile,
        character=args.character,
        reference_dir=args.reference,
        anim=args.anim,
        track=args.track,
        seeds=args.seeds,
        out=args.out,
        exec_container=args.exec_container,
        need_gib=args.need_gib,
        windows=args.windows,
        godot_check=args.godot_check,
        repo=Path(__file__).resolve().parents[1],
    )
    if res["outcome"] == "usage":
        print(f"animate: {res['error']}", file=sys.stderr)
        return 2
    t, c = res["timing"], res["counts"]
    stages = " ".join(f"{k}={v}s" for k, v in t.items() if v is not None)
    print(f"{res['outcome'].upper()} {args.character} {args.anim}: {stages}")
    if c:
        print(f"generations: {c['ok']}/{c['attempts']} ok ({c['failed']} failed, {c['not_run']} not run)")
    if res["error"]:
        print(f"error: {res['error']}", file=sys.stderr)
    for m in (res["verdict"] or {}).get("failures", [])[:8]:
        print(f"  - {m}", file=sys.stderr)
    if res["export"]:
        print(f"export: {Path(next(iter(res['export'].values()))).parent}")
    print(f"animate.json: {args.out / 'animate.json'}")
    return res["exit"]


def review_record_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.review import record_review

    try:
        path = record_review(
            args.job,
            verdict=args.verdict,
            reviewer=args.reviewer,
            cleanup_minutes=args.cleanup_minutes,
            started_at=args.started,
            ended_at=args.ended,
            notes=args.notes,
            supersedes=args.supersedes,
            carried_from=args.carried_from,
        )
    except (OSError, ValueError) as e:
        print(f"review record: {e}", file=sys.stderr)
        return 2
    print(path)
    return 0


def review_check_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.review import review_errors

    problems = review_errors(args.job)
    for error in problems:
        print(json.dumps(error))
    return int(bool(problems))


def _exclusive(*paths: Path | None) -> None:
    for p in paths:
        if p is not None and (p.exists() or p.is_symlink()):
            raise FileExistsError(f"{p} exists: outputs are never overwritten")


def classreq_check_cmd(args: argparse.Namespace) -> int:
    from sonoranlab import classreq

    try:
        _exclusive(args.out, args.contact)
        prof = load_profile(args.profile)
        raw, _ = classreq.load_request_file(args.request)
        stills = args.stills
        if stills is None and isinstance(raw, dict) and isinstance(raw.get("references"), dict):
            given = raw["references"].get("stills_dir")
            if isinstance(given, str) and given:
                stills = args.request.parent / given
        character = raw.get("character") if isinstance(raw, dict) else None
        reg = None
        if args.state and isinstance(character, str) and classreq.NAME_RE.match(character):
            reg = classreq.read_registry(args.state, prof.id, character)
        result, read = classreq.check(
            raw,
            prof,
            load_palette(prof),
            stills_dir=stills,
            stills_source=str(stills) if stills else None,
            registry=reg,
        )
        if args.contact:
            img = classreq.contact(result, read, prof.directions.order)
            with open(args.contact, "xb") as fh:
                img.save(fh, "PNG")
        text = json.dumps(result, indent=2)
        if args.out:
            with open(args.out, "x") as fh:
                fh.write(text + "\n")
            print(json.dumps({"status": result["status"], "out": str(args.out)}))
        else:
            print(text)
    except (OSError, ValueError) as e:
        print(f"classreq check: {e}", file=sys.stderr)
        return 2
    return 0 if result["status"] == "ready" else 1


def classreq_propose_cmd(args: argparse.Namespace) -> int:
    from sonoranlab import classreq

    try:
        _exclusive(args.out)
        prof = load_profile(args.profile)
        raw, _ = classreq.load_request_file(args.request)
        req, problems = classreq.parse_request(raw)
        if req is None:
            raise ValueError("class request invalid: " + "; ".join(problems))
        text = classreq.propose_row(prof, req["character"], args.cell, classreq.request_sha256(raw))
        if args.out:
            with open(args.out, "x") as fh:
                fh.write(text)
        print(text, end="")
    except (OSError, ValueError) as e:
        print(f"classreq propose-row: {e}", file=sys.stderr)
        return 2
    return 0


def refprep_cmd(args: argparse.Namespace) -> int:
    from sonoranlab.refprep import refprep

    try:
        views = {}
        for spec in args.view:
            d, sep, img = spec.partition("=")
            if not sep or not img or d in views:
                raise ValueError(f"--view {spec!r}: expected DIRECTION=IMG, once per direction")
            views[d] = Path(img)
        doc = refprep(
            profile_path=args.profile,
            request_path=args.request,
            out=args.out,
            turnaround=args.turnaround,
            grid=args.grid,
            order=args.order.split(",") if args.order else None,
            views=views,
            cell=args.cell,
            bg_key=args.bg_key,
            despill_px=args.despill_px,
        )
    except (OSError, ValueError) as e:
        print(f"refprep: {e}", file=sys.stderr)
        return 2
    status = doc["check"]["status"]
    print(
        json.dumps(
            {"out": str(args.out), "cell": doc["parameters"]["cell"], "status": status, "warnings": doc["warnings"]}
        )
    )
    return 0 if status in ("ready", "needs_profile_row") else 1


CAMPAIGN_VERBS = {
    "create": "admit + freeze a campaign; exit 1 when nothing was created (blocking state printed)",
    "status": "derived states, budgets, candidates and next actions (read-only)",
    "submit": "the only generation path: one Track E animate job, next unused seed",
    "decide": "agent candidate recommendation (selected needs technical PASS)",
    "escalate": "record best two candidates and diagnostics for the owner",
    "report": "per-clip attempts, failure classes, timings, explicit costs, interventions",
    "note": "append a note and/or an explicit cost record",
}


def campaign_cmd(args: argparse.Namespace) -> int:
    """CLI/MCP parity: the same registry call and envelope as `sonoranlab api campaign_<verb>`."""
    from sonoranlab import classreq
    from sonoranlab.api import call

    params = {
        k: v
        for k, v in vars(args).items()
        if v is not None and k not in {"verb", "action", "func", "request", "cost_source", "cost_units", "cost_amount"}
    }
    if args.action == "create":
        try:
            params["request"], _ = classreq.load_request_file(args.request)
        except (OSError, ValueError) as exc:
            print(f"campaign create: {exc}", file=sys.stderr)
            return 2
    if args.action == "note" and args.cost_source is not None:
        params["cost"] = {"source": args.cost_source, "units": args.cost_units, "amount": args.cost_amount}
    envelope = call(f"campaign_{args.action}", params)
    print(json.dumps(envelope))
    if not envelope["ok"]:
        return 2 if envelope["error"]["code"] == "invalid_params" else 1
    return 1 if args.action == "create" and not envelope["result"]["created"] else 0


def campaign_owner_cmd(args: argparse.Namespace) -> int:
    from sonoranlab import campaign
    from sonoranlab.api.registry import Context, ToolError

    options = {k: getattr(args, k) for k in ("ref", "clip", "seeds", "state", "activity", "seconds")}
    try:
        result = campaign.owner_decision(args.campaign_id, args.kind, args.verbatim, **options, ctx=Context())
    except ToolError as exc:
        print(json.dumps({"ok": False, "error": exc.error}))
        return 2 if exc.error["code"] == "invalid_params" else 1
    print(json.dumps({"ok": True, "result": result}))
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
