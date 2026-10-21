"""CPU pipeline, bound exports and isolated Godot checks for durable workers."""

from __future__ import annotations

import shutil
from argparse import Namespace
from pathlib import Path

from sonoranlab.api.registry import Context, ToolError
from sonoranlab.api.tools import deterministic as ops
from sonoranlab.artifacts import sha256_file
from sonoranlab.jobs import store


def _hashes(path: Path) -> dict:
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    return {str(p): sha256_file(p) for p in files}


def _clip(profile, params: dict) -> dict:
    anim = profile.character(params["character"]).animations[params["anim"]]
    clip = {"name": params["anim"], "start": 1, "frames": anim.frames, "fps": anim.fps, "loop": anim.loop}
    if anim.loop_from is not None:
        clip["loop_from"] = anim.loop_from
    return clip


def _export(params: dict, ctx: Context, out: Path | None = None) -> tuple[Namespace, dict]:
    """Admit a report and current pixels through the exact standalone CLI checks."""
    import numpy as np
    from PIL import Image

    from sonoranlab.cli import export_godot_checked
    from sonoranlab.slice import pack_sheet
    from sonoranlab.validate import read_frames, read_stills

    profile = ops.profile_for(params, ctx)
    spec = profile.character(params["character"])
    source, _ = ops._reference(params["source"], ctx)
    report_path = source / "report.json" if source.is_dir() else source
    report_path = ops.input_file(str(report_path), ctx)
    if report_path.name != "report.json":
        raise ToolError("refused", "Expected a job or report.json artifact reference")
    root = ops.resolve_input_dir(report_path.parent, ctx)
    report = store.load(report_path)
    if not isinstance(report, dict) or report.get("pass") is not True:
        raise ToolError("refused", "Export requires a passing bound report")
    clip = _clip(profile, params)
    if not isinstance(report.get("params"), dict):
        raise ToolError("refused", "Report has no validation parameters")
    animation = report["params"].get("animation")
    if animation is not None and animation != {k: v for k, v in clip.items() if k != "start"}:
        raise ToolError("refused", "Report clip timing differs from the profile")
    manifest = root / "manifest.json"
    if manifest.exists():
        doc = store.load(ops.input_file(str(manifest), ctx))
        for rel, expected in doc["outputs"].items():
            if sha256_file(ops.records._job_file(root, rel)) != expected:
                raise ToolError("refused", "Job output hash differs from its manifest")
    sheet_path = root / "export" / spec.name / f"{spec.name}.png"
    if sheet_path.exists():
        side = store.load(ops.input_file(str(sheet_path.with_suffix(".sonoranlab.json")), ctx))
        if side["characters_json_entry"]["clips"] != [clip]:
            raise ToolError("refused", "Export clip timing differs from profile")
        sheet_path = ops.input_file(str(sheet_path), ctx)
        with Image.open(sheet_path) as im:
            sheet = np.asarray(im.convert("RGBA"))
    else:
        # Sync validation records its admitted input locations separately from report metadata.
        if (root / "inputs.json").exists():
            inputs = store.load(ops.input_file(str(root / "inputs.json"), ctx))
            if inputs.get("report_sha256") != sha256_file(report_path):
                raise ToolError("refused", "Validated report changed")
        elif manifest.exists():
            inputs = {"frames_dir": str(root / "processed/05_registered"), "stills": str(root / "raw")}
        else:
            raise ToolError("refused", "Report has no bound input record")
        frames = ops.resolve_input_dir(inputs["frames_dir"], ctx)
        stills = ops.resolve_input_dir(inputs["stills"], ctx) if inputs.get("stills") else None
        clips, _ = read_frames(frames, profile.directions.order)
        still_px, _ = read_stills(stills, profile.directions.order) if stills else ({}, [])
        for direction, source in profile.directions.mirror_animations.items():
            if direction in clips and any(
                not np.array_equal(a, b[:, ::-1]) for a, b in zip(clips[direction], clips[source], strict=True)
            ):
                raise ToolError("refused", "Mirrored frame pixels differ")
        sheet = pack_sheet(
            {d: frames for d, frames in clips.items() if d not in profile.directions.mirror_animations},
            spec.cell,
            profile.directions.order,
            clip["frames"],
            mirror=profile.directions.mirror_animations,
            stills=still_px,
            start_col=1,
            required=profile.directions.order,
        )
    text = f"{clip['name']}:{clip['start']}:{clip['frames']}:{clip['fps']}:{'loop' if clip['loop'] else 'once'}"
    if "loop_from" in clip:
        text += f":{clip['loop_from']}"
    args = Namespace(
        report=report_path,
        profile=profile.path,
        character=spec.name,
        cell=spec.cell[0],
        rows=",".join(profile.directions.order),
        clip=[text],
        sheet=sheet_path,
        out=out,
        provenance=None,
        forbid_under=profile.export.godot_project,
        anim_name="{animation}_{direction}",
    )
    try:
        admitted = export_godot_checked(args, sheet_pixels=sheet, write=False)
    except ValueError as exc:
        raise ToolError("refused", str(exc)) from exc
    return args, admitted


def _check(params: dict, ctx: Context) -> tuple[dict, object]:
    import numpy as np
    from PIL import Image

    from sonoranlab.export.godot import _tres, animation_specs

    profile = ops.profile_for(params, ctx)
    spec = profile.character(params["character"])
    source, _ = ops._reference(params["source"], ctx)
    root = source if source.is_dir() else source.parent
    root = ops.resolve_input_dir(root, ctx)
    candidates = [root / spec.name, root / "export" / spec.name, root]
    folder = next((p for p in candidates if (p / f"{spec.name}.sonoranlab.json").exists()), None)
    if folder is None:
        raise ToolError("refused", "Expected an export artifact reference")
    folder = ops.resolve_input_dir(folder, ctx)
    sidecar = store.load(ops.input_file(str(folder / f"{spec.name}.sonoranlab.json"), ctx))
    clip = _clip(profile, params)
    entry = sidecar["characters_json_entry"]
    if entry["clips"] != [clip] or entry["cell"] != spec.cell[0]:
        raise ToolError("refused", "Export clip timing or cell differs from profile")
    for name in (f"{spec.name}.png", f"{spec.name}_frames.tres"):
        if sha256_file(ops.input_file(str(folder / name), ctx)) != sidecar["sha256"].get(name):
            raise ToolError("refused", "Export hash mismatch")
    expected_tres = _tres(spec.name, animation_specs(profile.directions.order, spec.cell, [clip]))
    if ops.input_file(str(folder / f"{spec.name}_frames.tres"), ctx).read_text() != expected_tres:
        raise ToolError("refused", "Export SpriteFrames differs from the checked profile layout")
    with Image.open(ops.input_file(str(folder / f"{spec.name}.png"), ctx)) as im:
        sheet = np.asarray(im.convert("RGBA"))
    return {"character": spec.name, "cell": spec.cell, "rows": list(profile.directions.order), "clips": [clip]}, sheet


class Deterministic:
    gpu = False

    def __init__(self, kind: str):
        self.kind = kind
        self.heavy_cpu = kind == "process"

    def need_gib(self, params: dict) -> None:
        return None

    def validate(self, params: dict) -> tuple[dict, dict]:
        ctx = Context()
        profile = ops.profile_for(params, ctx)
        from sonoranlab.profile import load_palette

        load_palette(profile)
        hashes = _hashes(profile.path)
        hashes[str(profile.palette.source)] = profile.palette.sha256  # verified by the profile loader above
        if self.kind == "process":
            raw = (
                ops.input_file(params["raw"], ctx) if params.get("sheet") else ops.resolve_input_dir(params["raw"], ctx)
            )
            hashes.update(_hashes(raw))
            # pipeline.run also ingests provenance files beside the raw source.
            for name in ("selection.json", "manifest.json"):
                path = raw.parent / name
                if path.exists() or path.is_symlink():
                    hashes.update(_hashes(ops.input_file(str(path), ctx)))
            for key in ("stills", "anchors"):
                if params.get(key):
                    path = (
                        ops.resolve_input_dir(params[key], ctx) if key == "stills" else ops.input_file(params[key], ctx)
                    )
                    hashes.update(_hashes(path))
            if params.get("mode") != "raw" or ("start_col" in params and not params.get("sheet")):
                raise ToolError("invalid_params", "process requires mode raw; start_col requires sheet")
        else:
            if self.kind == "export_godot":
                args, _ = _export(params, ctx)
                root = args.report.parent
                inputs = root / "inputs.json"
                if inputs.exists():
                    for key, value in store.load(inputs).items():
                        if key in ("frames_dir", "stills"):
                            hashes.update(_hashes(ops.resolve_input_dir(value, ctx)))
            else:
                _check(params, ctx)
                path, _ = ops._reference(params["source"], ctx)
                root = path if path.is_dir() else path.parent
            hashes.update(_hashes(ops.resolve_input_dir(root, ctx)))
        return params, hashes

    def run(self, job: dict, ctx) -> dict:
        params, hashes = self.validate(job["params"])
        if hashes != job["input_hashes"]:
            raise ToolError("refused", "Frozen job inputs changed before execution")
        api_ctx = Context()
        out = ops.paths._confine(ctx.output_dir, (ops.paths.state_root(),))
        if any(out.iterdir()):
            raise ToolError("conflict", "Job output must be empty before execution")
        profile = ops.profile_for(params, api_ctx)
        if self.kind == "process":
            from sonoranlab.pipeline import run

            ctx.set_stage("select_and_process")
            result = run(
                ops.input_file(params["raw"], api_ctx)
                if params.get("sheet")
                else ops.resolve_input_dir(params["raw"], api_ctx),
                profile=profile.path,
                character=params["character"],
                anim=params["anim"],
                out=out,
                stills=ops.resolve_input_dir(params["stills"], api_ctx) if params.get("stills") else None,
                sheet=params.get("sheet", False),
                start_col=params.get("start_col"),
                anchors=ops.input_file(params["anchors"], api_ctx) if params.get("anchors") else None,
                godot_check=params.get("godot_check", False),
            )
            gc = result.get("godot_check")
            technical = {
                "validator": "pass" if result["pass"] else "fail",
                "godot": "not_run" if gc is None else "pass" if gc["pass"] else "fail",
            }
            for key in ("report", "contact"):
                result[key] = ops.artifact(out, Path(result[key]), job["job_id"])
            result["job"] = job["job_id"]
            if result["export"]:
                result["export"] = {k: ops.artifact(out, Path(v), job["job_id"]) for k, v in result["export"].items()}
        elif self.kind == "export_godot":
            from sonoranlab.cli import export_godot_checked

            ctx.set_stage("validate_export")
            args, admitted = _export(params, api_ctx, out)
            paths = export_godot_checked(args, sheet_pixels=admitted["sheet"])
            result = {"export": {k: ops.artifact(out, Path(v), job["job_id"]) for k, v in paths.items()}}
            technical = {"validator": "pass", "godot": "not_run"}
        else:
            from sonoranlab.pipeline import check_in_godot

            ctx.set_stage("check")
            kwargs, sheet = _check(params, api_ctx)
            if shutil.which("godot") is None:
                gc = {"available": False, "godot_attempts": []}
                verdict = "unavailable"
            else:
                gc = check_in_godot(sheet, **kwargs)
                verdict = "pass" if gc["pass"] else "fail"
            store.atomic(out / "godot_check.json", gc, exclusive=True)
            result = {"godot_check": gc, "ref": ops.artifact(out, out / "godot_check.json", job["job_id"])}
            technical = {"validator": None, "godot": verdict}
        ctx.check_cancelled()
        return {**result, "technical": technical}

    def stage(self, job: dict) -> dict:
        root = store.output(job)
        result = job.get("result") or {}
        report = (root / "report.json").is_file()
        return {
            "stage": "validate_export" if self.kind == "process" and report else job["stage"],
            "report": report,
            "technical": result.get("technical"),
        }


EXECUTORS = {name: Deterministic(name) for name in ("process", "export_godot", "godot_check")}
