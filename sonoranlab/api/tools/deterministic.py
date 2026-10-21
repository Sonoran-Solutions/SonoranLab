"""Deterministic operations: confined inputs, fresh outputs, shared CLI checks.

ponytail: confinement is checked before each library call, like api.paths; hostile
same-UID ancestor renames require directory-fd IO or mount isolation.
"""

from __future__ import annotations

import re
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sonoranlab import registry as records
from sonoranlab.api import paths
from sonoranlab.api.registry import Context, Tool, ToolError
from sonoranlab.artifacts import sha256_file
from sonoranlab.jobs import manager, store

SYNC_TOOLS = ("validate", "contact_sheet", "compare")


def _reference(value: str, ctx: Context) -> tuple[Path, str]:
    identifier, _, relative = value.partition(":")
    rel = paths._path(relative) if relative else Path(".")
    if rel.is_absolute():
        raise ToolError("path_refused", "Artifact path must be relative")
    if native := records._native_job(identifier):
        root = store.output(native)
    else:
        hits = [paths.state_root() / "outputs" / tool / identifier for tool in SYNC_TOOLS]
        hits = [p for p in hits if re.fullmatch(store.JOB_ID, identifier) and p.exists()]
        if hits:
            root = hits[0]
        else:
            entry = records._find_job(identifier, ctx)
            original = records._directory(entry["job_dir"], ctx)
            for name, digest in entry["hashes"].items():
                if sha256_file(records._job_file(original, name)) != digest:
                    raise ToolError("refused", "Imported artifact changed since registration")
            root = Path(entry["review_job"])
    root = records._directory(root, ctx)
    target = paths._confine(root / rel, (root,))
    records._protected(target, ctx)
    return target, identifier


def input_path(value: str, ctx: Context) -> Path:
    if ":" in value or re.fullmatch(store.JOB_ID, value) or value.startswith("import-"):
        return _reference(value, ctx)[0]
    return paths._path(value)


def input_file(value: str, ctx: Context) -> Path:
    path = paths.resolve_input(input_path(value, ctx))
    records._protected(path, ctx)
    return path


def resolve_input_dir(value: str | Path, ctx: Context) -> Path:
    """Confine a directory and every descendant before any library can read it."""
    root = records._directory(input_path(str(value), ctx), ctx)
    for child in root.rglob("*"):
        records._protected(child, ctx)
        if child.is_symlink():
            raise ToolError("path_refused", "Symlinks inside input directories are refused")
        if child.is_dir():
            paths._confine(child, (root,))
        else:
            file = paths.resolve_input(child)
            if not file.is_relative_to(root):
                raise ToolError("path_refused", "Input file escapes its directory")
    return root


def profile_for(params: dict, ctx: Context):
    profile = records._character(params["project"], params["character"], ctx)
    if params["anim"] not in profile.character(params["character"]).animations:
        raise ToolError("invalid_params", "Unknown profile animation", field="anim")
    return profile


def output_dir(tool: str, ctx: Context) -> Path:
    parent = store.directory(f"outputs/{tool}", create=True)
    records._protected(parent, ctx)
    op = datetime.now(UTC).strftime("job-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    out = paths.resolve_output(parent / op)
    out.mkdir()
    store.fsync_dir(parent)
    return out


def artifact(out: Path, path: Path, identifier: str | None = None) -> str:
    return f"{identifier or out.name}:{path.relative_to(out).as_posix()}"


def validation_args(params: dict, ctx: Context) -> Namespace:
    profile = profile_for(params, ctx)
    return Namespace(
        profile=profile.path,
        character=params["character"],
        anim=params["anim"],
        cell=None,
        frames_dir=resolve_input_dir(params["frames_dir"], ctx),
        stills=resolve_input_dir(params["stills"], ctx) if params.get("stills") else None,
        registration=input_file(params["registration"], ctx) if params.get("registration") else None,
    )


def validate(params: dict, ctx: Context) -> dict:
    from sonoranlab.cli import validation_report

    args = validation_args(params, ctx)
    report = validation_report(args)
    out = output_dir("validate", ctx)
    store.atomic(out / "report.json", report, exclusive=True)
    # Keep the original input locations, not caller-controlled report pointers, for later export.
    store.atomic(
        out / "inputs.json",
        {
            "report_sha256": sha256_file(out / "report.json"),
            **{k: str(v) for k, v in vars(args).items() if v is not None},
        },
        exclusive=True,
    )
    return {"report": report, "ref": artifact(out, out / "report.json")}


def _size(width: int, height: int) -> None:
    if max(width, height) > 16384 or width * height * 4 > 64 * 1024 * 1024:
        raise ToolError("refused", "Contact sheet exceeds 16384 px edge or 64 MiB decoded size")


def _dimensions(clips: dict, spec, zoom: int) -> tuple[int, int]:
    from sonoranlab import contact

    cols, rows = max(map(len, clips.values())), len(clips)
    cw, ch = spec.cell
    width = cols * (cw * zoom + contact.GAP) + contact.GAP
    height = (
        contact.HEADER_H
        + rows * (contact.LABEL_H + ch * zoom + contact.BADGE_H + contact.GAP)
        + contact.GAP
        + rows * (ch + contact.GAP)
        + contact.FOOTER_H
    )
    return width, height


def render(clips: dict, report: dict, spec, zoom: int):
    from sonoranlab import contact

    _size(*_dimensions(clips, spec, zoom))
    return contact.contact_sheet(clips, report, cell=spec.cell, foot_y=spec.foot_y, scale=zoom)


def _save(image, path: Path) -> None:
    with path.open("xb") as stream:
        image.save(stream, "PNG")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ToolError("refused", "Contact sheet exceeds 64 MiB")


def contact_sheet(params: dict, ctx: Context) -> dict:
    from sonoranlab.contact import load_clips

    profile = profile_for(params, ctx)
    frames = resolve_input_dir(params["frames_dir"], ctx)
    report = store.load(input_file(params["report"], ctx))
    if not isinstance(report, dict) or type(report.get("pass")) is not bool:
        raise ToolError("invalid_params", "Expected a validator report")
    clips, spec = load_clips(frames), profile.character(params["character"])
    zoom = params.get("zoom")
    if zoom is None:
        for zoom in range(8, 0, -1):
            width, height = _dimensions(clips, spec, zoom)
            if max(width, height) <= 16384 and width * height * 4 <= 64 * 1024 * 1024:
                break
    image = render(clips, report, spec, zoom)
    out = output_dir("contact_sheet", ctx)
    _save(image, out / "contact.png")
    return {"ref": artifact(out, out / "contact.png"), "dimensions": list(image.size), "zoom": zoom}


def compare(params: dict, ctx: Context) -> dict:
    from PIL import Image

    from sonoranlab.cli import validation_report
    from sonoranlab.profile import load_palette
    from sonoranlab.repair.compare import changed_coordinates, coordinates, pixel_problems
    from sonoranlab.validate import bbox, foot_x, read_frames

    profile = profile_for(params, ctx)
    spec = profile.character(params["character"])
    args = [validation_args({**params, "frames_dir": params[side]}, ctx) for side in ("before", "after")]
    reports = [validation_report(a) for a in args]
    pairs = [read_frames(a.frames_dir, profile.directions.order) for a in args]
    if sorted(p.name for p in args[0].frames_dir.glob("*.png")) != sorted(
        p.name for p in args[1].frames_dir.glob("*.png")
    ):
        raise ToolError("invalid_params", "Comparison requires identical frame names")
    palette = ["#" + bytes(c).hex() for c in load_palette(profile).tolist()]
    changes = {}

    def delta(a, b):
        if a is None or b is None:
            return None
        return [y - x for x, y in zip(a, b, strict=True)] if isinstance(a, list) else b - a

    def bounded(coords):
        return {"coordinates": coords[:2000], "count": len(coords), "truncated": len(coords) > 2000}

    for d, frames in pairs[0][0].items():
        for i, before in enumerate(frames):
            after = pairs[1][0][d][i]
            coords = changed_coordinates(before, after)
            boxes = [bbox(f[..., 3] > 0) for f in (before, after)]
            feet = [foot_x(f, box[3]) if box else None for f, box in zip((before, after), boxes, strict=True)]
            problems = [pixel_problems(f, palette) for f in (before, after)]
            colours = [set(map(tuple, f[f[..., 3] == 255, :3].tolist())) for f in (before, after)]
            changes[f"{d}_{i}.png"] = {
                "changed": bool(coords),
                **bounded(coords),
                "alpha": bounded(coordinates(before[..., 3] != after[..., 3])),
                "palette": {"added": sorted(colours[1] - colours[0]), "removed": sorted(colours[0] - colours[1])},
                "pixel_problems": {
                    key: {
                        "before": bounded(problems[0][key]),
                        "after": bounded(problems[1][key]),
                        "delta": len(problems[1][key]) - len(problems[0][key]),
                    }
                    for key in ("off_palette", "invalid_alpha")
                },
                "bbox": {"before": boxes[0], "after": boxes[1], "delta": delta(*boxes)},
                "foot_anchor": {"before": feet[0], "after": feet[1], "delta": delta(*feet)},
            }
    images = [render(pair[0], report, spec, params.get("zoom", 8)) for pair, report in zip(pairs, reports, strict=True)]
    _size(sum(im.width for im in images), max(im.height for im in images))
    side = Image.new("RGB", (sum(im.width for im in images), max(im.height for im in images)))
    side.paste(images[0], (0, 0))
    side.paste(images[1], (images[0].width, 0))
    out = output_dir("compare", ctx)
    refs = {}
    for name, image in zip(("before", "after", "side_by_side"), [*images, side], strict=True):
        _save(image, out / f"{name}.png")
        refs[name] = artifact(out, out / f"{name}.png")
    result = {
        "frames": changes,
        "changed_frames": sum(c["changed"] for c in changes.values()),
        "changed_pixels": sum(c["count"] for c in changes.values()),
        "reports": dict(zip(("before", "after"), reports, strict=True)),
        "verdict": {
            "before": reports[0]["pass"],
            "after": reports[1]["pass"],
            "changed": reports[0]["pass"] != reports[1]["pass"],
        },
        "contacts": refs,
    }
    store.atomic(out / "compare.json", result, exclusive=True)
    return {**result, "ref": artifact(out, out / "compare.json")}


def submit(params: dict, ctx: Context, kind: str) -> dict:
    profile_for(params, ctx)
    return manager.submit(kind, params, params.get("idempotency_key"))


STRING = {"type": "string", "minLength": 1}
COMMON = {"project": STRING, "character": STRING, "anim": STRING}
ZOOM = {"type": "integer", "minimum": 1, "maximum": 8, "default": 8}
FINAL = {"const": True, "description": "Inputs are already at final resolution; no processing or registration."}


def schema(extra: dict, required: tuple[str, ...]) -> dict:
    return {
        "type": "object",
        "properties": {**COMMON, **extra},
        "required": [*COMMON, *required],
        "additionalProperties": False,
    }


TOOLS = (
    Tool(
        "validate",
        "Validate final-resolution cells with the CLI gates; write a fresh report.",
        schema(
            {"frames_dir": STRING, "stills": STRING, "registration": STRING, "final_resolution": FINAL},
            ("frames_dir", "final_resolution"),
        ),
        False,
        "sync",
        validate,
    ),
    Tool(
        "contact_sheet",
        "Render a size-capped review contact sheet.",
        schema(
            {"frames_dir": STRING, "report": STRING, "zoom": {k: v for k, v in ZOOM.items() if k != "default"}},
            ("frames_dir", "report"),
        ),
        False,
        "sync",
        contact_sheet,
    ),
    Tool(
        "compare",
        "Compare final-resolution pixels and validator verdicts.",
        schema(
            {"before": STRING, "after": STRING, "final_resolution": FINAL, "zoom": ZOOM},
            ("before", "after", "final_resolution"),
        ),
        False,
        "sync",
        compare,
    ),
    *(
        Tool(
            name,
            description,
            schema(extra | {"idempotency_key": STRING}, required),
            False,
            "job",
            lambda p, c, kind=name: submit(p, c, kind),
        )
        for name, description, extra, required in (
            (
                "process",
                "Process raw HD cells through the CLI pipeline under the heavy lock.",
                {
                    "raw": STRING,
                    "mode": {"const": "raw"},
                    "sheet": {"type": "boolean"},
                    "start_col": {"type": "integer", "minimum": 0},
                    "stills": STRING,
                    "anchors": STRING,
                    "godot_check": {"type": "boolean"},
                },
                ("raw", "mode"),
            ),
            (
                "export_godot",
                "Export from a bound job/report reference into a fresh job.",
                {"source": STRING},
                ("source",),
            ),
            (
                "godot_check",
                "Check an export in an isolated Godot project; retain every retry attempt.",
                {"source": STRING},
                ("source",),
            ),
        )
    ),
)
