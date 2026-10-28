"""`sonoranlab compose`: per-clip exported jobs + the approved stills -> ONE game sheet in a new, reviewable job dir.

Each input is a passing `process`/`animate` final job whose export sheet is column 0 = stills, then one clip from
column 1. The composed sheet keeps the profile's 8 rows, column 0 = the approved stills (pixel-identical to every
job's column 0 and to STILLSDIR) and the clips in the order given (their `start` columns follow, as in the game's
characters.json). Every input job is integrity-checked as `review` checks it (report, manifest outputs, raw), every
cell is bound to its validated report digests, and the composed result is re-validated per clip with the existing
validator and the profile's unchanged limits. Exit codes: 0 PASS and exported, 1 validator FAIL (job dir with
report, nothing exported), 2 usage/input refusal (nothing written).

The output job dir has the structure `review record` / `review check` bind: raw/ (byte copies of each input's export
sheet, report and manifest, and the stills), report.json, manifest.json and export/<character>/.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

from sonoranlab import artifacts
from sonoranlab.export.godot import export_godot
from sonoranlab.profile import load_palette, load_profile
from sonoranlab.review import _provenance as job_integrity
from sonoranlab.slice import slice_sheet
from sonoranlab.validate import frame_digest, read_stills, validate

TOOL = "sonoranlab compose"


class ComposeError(Exception):
    """Usage or input refusal (exit 2); nothing was written."""


def _clean(f: np.ndarray) -> np.ndarray:
    """Invisible pixels carry no colour (what the exported PNG stores)."""
    f = f.copy()
    f[f[..., 3] == 0] = 0
    return f


def _load_rgba(p: Path) -> np.ndarray:
    with Image.open(p) as im:
        return np.asarray(im.convert("RGBA")).copy()


def _json(p: Path) -> dict:
    artifacts.require_regular_file(p)
    doc = json.loads(p.read_text())
    if not isinstance(doc, dict):
        raise ComposeError(f"{p}: not a JSON object")
    return doc


def _check_job(
    job: Path, name: str, *, prof, spec, profile_path: Path, profile_sha: str, stills: dict[str, np.ndarray]
) -> dict:
    """Refuse an input job that is not an intact, passing, single-clip export of `name` for this character/profile.
    Returns what composing needs: sheet path + pixels, report, hashes."""
    try:
        binds, problems = job_integrity(job)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ComposeError(f"--clips {name}={job}: cannot verify the job: {e}") from None
    if problems or binds is None:
        raise ComposeError(f"--clips {name}={job}: not an intact passing job: " + "; ".join(problems))
    report = _json(job / "report.json")
    manifest = _json(job / "manifest.json")
    if report.get("character") != spec.name or report.get("animation") != name:
        raise ComposeError(
            f"--clips {name}={job}: report is {report.get('character')}/{report.get('animation')}, "
            f"expected {spec.name}/{name}"
        )
    bound = ((report.get("provenance") or {}).get("profile") or {}).get("sha256")
    recorded = manifest.get("profile") if isinstance(manifest.get("profile"), dict) else {}
    # job_integrity already accepted the recorded profile for this character (same file, or only other rows changed)
    same_file = Path(str(recorded.get("path", ""))).resolve() == profile_path.resolve()
    if bound != profile_sha and not (bound == recorded.get("sha256") and same_file):
        raise ComposeError(f"--clips {name}={job}: validated with a different profile (sha256 {bound})")
    anim = spec.animations[name]
    exp = job / "export" / spec.name
    side = _json(exp / f"{spec.name}.sonoranlab.json")
    clips = (side.get("characters_json_entry") or {}).get("clips")
    want = {"name": name, "start": 1, "frames": anim.frames, "fps": anim.fps, "loop": anim.loop}
    want |= {"loop_from": anim.loop_from} if anim.loop_from is not None else {}
    if clips != [want]:
        raise ComposeError(f"--clips {name}={job}: export clip table {clips} is not the profile's single {want}")
    sheet_path = exp / f"{spec.name}.png"
    artifacts.require_regular_file(sheet_path, root=job)
    sheet = _load_rgba(sheet_path)
    order, gen = list(prof.directions.order), list(prof.directions.generate)
    cw, ch = spec.cell
    if sheet.shape[:2] != (len(order) * ch, (1 + anim.frames) * cw):
        raise ComposeError(f"--clips {name}={job}: sheet {sheet.shape[1]}x{sheet.shape[0]} is not the 1-clip layout")
    cells = slice_sheet(sheet, spec.cell, order, anim.frames, start_col=1)
    digests = report.get("digests")
    if not isinstance(digests, dict) or not digests:
        raise ComposeError(f"--clips {name}={job}: report has no frame digests")
    remaining = dict(digests)

    def bind(key: str, f: np.ndarray, tag: str) -> None:
        if remaining.pop(key, None) != frame_digest(f):
            raise ComposeError(f"--clips {name}={job}: sheet {tag} does not match the validated report")

    for d in gen:
        for i, f in enumerate(cells[d]):
            bind(f"{d}_{i}", f, f"{d} frame {i}")
    for d, src in prof.directions.mirror_animations.items():
        for i, f in enumerate(cells[d]):
            if not np.array_equal(f, cells[src][i][:, ::-1]):
                raise ComposeError(f"--clips {name}={job}: {d} frame {i} does not mirror {src}")
            if (key := f"{d}_{i}") in remaining:
                bind(key, f, f"{d} frame {i}")
    for r, d in enumerate(order):
        col0 = sheet[r * ch : (r + 1) * ch, :cw]
        if not (col0[..., 3] > 0).any():
            raise ComposeError(f"--clips {name}={job}: column 0 has no still for {d}")
        bind(f"still_{d}", col0, f"still {d}")
        if not np.array_equal(col0, stills[d]):
            raise ComposeError(f"--clips {name}={job}: column 0 {d} is not pixel-identical to --stills still_{d}.png")
    if remaining:
        raise ComposeError(f"--clips {name}={job}: report digest {next(iter(remaining))} has no matching sheet cell")
    return {
        "job": str(job.resolve()),
        "sheet": sheet_path,
        "report": report,
        "manifest": manifest,
        "sheet_sha256": artifacts.sha256_file(sheet_path),
        "report_sha256": binds["report_sha256"],
        "manifest_sha256": binds["manifest_sha256"],
    }


def compose(*, profile: Path, character: str, clips: Sequence[tuple[str, Path]], stills: Path, out: Path) -> dict:
    """Compose and re-validate; returns {"exit", "job", "pass", "failures", "warnings", "export"}.
    Raises ComposeError (exit 2) before anything is written for every refusal."""
    t0 = time.perf_counter()
    profile, stills, out = Path(profile).expanduser(), Path(stills).expanduser(), Path(out).expanduser()
    prof = load_profile(profile)
    spec = prof.character(character)
    pal = load_palette(prof)
    profile_sha = artifacts.sha256_file(profile)
    order, gen = list(prof.directions.order), list(prof.directions.generate)
    mirror = dict(prof.directions.mirror_animations)
    names = [n for n, _ in clips]
    if not clips or len(set(names)) != len(names):
        raise ComposeError(f"--clips must name each clip once, got {names}")
    if unknown := [n for n in names if n not in spec.animations]:
        raise ComposeError(f"--clips {unknown}: not clips of {spec.name} ({', '.join(spec.animations)})")
    game = prof.export.godot_project.expanduser().resolve()
    if out.exists() or out.is_symlink():
        raise ComposeError(f"--out {out} exists; composed jobs are created exclusively")
    if out.resolve() == game or out.resolve().is_relative_to(game):
        raise ComposeError(f"--out {out} is inside the shipped game {game}")
    try:
        still_px, still_files = read_stills(stills, order)
    except (OSError, ValueError) as e:
        raise ComposeError(f"--stills: {e}") from None
    if missing := [d for d in order if d not in still_px]:
        raise ComposeError(f"--stills {stills}: missing still_<direction>.png for {', '.join(missing)}")
    still_px = {d: _clean(f) for d, f in still_px.items()}
    checked = {
        n: _check_job(
            Path(j).expanduser(),
            n,
            prof=prof,
            spec=spec,
            profile_path=profile,
            profile_sha=profile_sha,
            stills=still_px,
        )
        for n, j in clips
    }

    # ---- job dir: immutable copies of everything the composition reads
    out.mkdir(parents=True)  # exclusive: FileExistsError if it appeared since the check
    job = artifacts.create_job(out)
    sources: dict[str, Path] = {p.name: p for p in still_files}
    for n, c in checked.items():
        jd = Path(c["job"])
        sources |= {f"{n}.sheet.png": c["sheet"], f"{n}.report.json": jd / "report.json"}
        sources[f"{n}.manifest.json"] = jd / "manifest.json"
    inputs = artifacts.ingest_raw(job, sources)
    raw = job / "raw"
    for n, c in checked.items():
        if inputs[f"{n}.sheet.png"] != c["sheet_sha256"] or inputs[f"{n}.report.json"] != c["report_sha256"]:
            raise ComposeError(f"{c['job']}: changed while being composed")
    stills_raw = {d: _clean(_load_rgba(raw / f"still_{d}.png")) for d in order}
    if any(not np.array_equal(stills_raw[d], still_px[d]) for d in order):
        raise ComposeError(f"--stills {stills}: changed while being composed")

    # ---- compose from the raw copies
    cw, ch = spec.cell
    total = sum(spec.animations[n].frames for n in names)
    sheet = np.zeros((len(order) * ch, (1 + total) * cw, 4), np.uint8)
    for r, d in enumerate(order):
        sheet[r * ch : (r + 1) * ch, :cw] = stills_raw[d]
    entry_clips, start = [], 1
    for n in names:
        a = spec.animations[n]
        src = _load_rgba(raw / f"{n}.sheet.png")
        sheet[:, start * cw : (start + a.frames) * cw] = src[:, cw : (1 + a.frames) * cw]
        entry_clips.append(
            {"name": n, "start": start, "frames": a.frames, "fps": a.fps, "loop": a.loop}
            | ({"loop_from": a.loop_from} if a.loop_from is not None else {})
        )
        start += a.frames

    # ---- re-validate every clip on the composed sheet (same validator, the profile's limits)
    per_clip, failures, warnings = {}, [], []
    stills_cells = {d: sheet[r * ch : (r + 1) * ch, :cw] for r, d in enumerate(order)}
    for c in entry_clips:
        n, a = c["name"], spec.animations[c["name"]]
        cells = slice_sheet(sheet[:, : (c["start"] + a.frames) * cw], spec.cell, order, a.frames, start_col=c["start"])
        rep = validate(
            {d: cells[d] for d in gen},
            stills=stills_cells,
            cell=spec.cell,
            frames=a.frames,
            directions=gen,
            palette=pal,
            foot_y=spec.foot_y,
            anchor_x=spec.anchor_x,
            padding=spec.padding,
            limits=spec.limits,
            max_colours=prof.palette.max_colors_per_cell,
            loop=a.loop,
            anchor_mode=spec.anchor_mode,
            fringe_tolerance=prof.alpha.fringe_tolerance,
            mirror=mirror,
            registration=checked[n]["report"].get("registration"),
            limits_source=spec.limits_source,
        )
        for d, src in mirror.items():
            for i, f in enumerate(cells[d]):
                if not np.array_equal(f, cells[src][i][:, ::-1]):
                    rep["failures"].append(f"direction_mirror: {d}/{i} is not {src}/{i} mirrored")
        if rep["digests"] != checked[n]["report"]["digests"]:
            rep["failures"].append("digests: composed cells differ from the clip's validated report")
        rep["pass"] = not rep["failures"]
        per_clip[n] = {"start": c["start"], **rep}
        failures += [f"{n}: {m}" for m in rep["failures"]]
        warnings += [f"{n}: {m}" for m in rep["warnings"]]
    ok = not failures
    prov = {
        "profile": {"path": str(profile.resolve()), "sha256": profile_sha},
        "inputs": dict(sorted(inputs.items())),
        "sources": {
            n: {k: c[k] for k in ("job", "sheet_sha256", "report_sha256", "manifest_sha256")}
            for n, c in checked.items()
        },
    }
    report = {
        "pass": ok,
        "character": spec.name,
        "animation": "+".join(names),
        "tool": TOOL,
        "clips": per_clip,
        "failures": failures,
        "warnings": warnings,
        "provenance": prov,
    }
    outputs = {"report.json": artifacts.sha256_file(artifacts.write_json(job, "report.json", report))}
    t_validate = time.perf_counter()

    export, error = None, None
    if ok:
        side = {"tool": TOOL, "validator": {"pass": True, "report_sha256": outputs["report.json"]}, **prov}
        try:
            export = export_godot(
                sheet, character=spec.name, cell=spec.cell, rows=order, clips=entry_clips, out_dir=job / "export",
                provenance=side, forbid_under=prof.export.godot_project,
            )  # fmt: skip
        except ValueError as e:
            error = f"export: {e}"
        for p in (export or {}).values():
            outputs[Path(p).relative_to(job.resolve()).as_posix()] = artifacts.sha256_file(Path(p))
    code = 2 if error else (0 if ok else 1)
    params = {
        "tool": TOOL,
        "clips": entry_clips,
        "columns": 1 + total,
        "cell": list(spec.cell),
        "rows": order,
        "stills": sorted(p.name for p in still_files),
        "limits": {k: v for k, v in vars(spec.limits).items() if v is not None},
        "limits_source": spec.limits_source,
    }
    upstream = {n: c["manifest"].get("upstream") for n, c in checked.items()}
    man = artifacts.build_manifest(
        job=job,
        profile_path=profile.resolve(),
        character=spec.name,
        animation=report["animation"],
        inputs=inputs,
        outputs=outputs,
        params=params,
        timings={"validate_s": round(t_validate - t0, 3), "total_s": round(time.perf_counter() - t0, 3)},
        upstream=upstream,
    )
    man["result"] = {"pass": ok, "exit": code, "exported": export is not None, "error": error}
    artifacts.write_json(job, "manifest.json", man)
    if problems := artifacts.verify_raw(job):
        raise ComposeError(f"{job}: raw copies changed during the run: {'; '.join(problems)}")
    if error:
        raise ComposeError(error)
    return {"exit": code, "job": str(job), "pass": ok, "failures": failures, "warnings": warnings, "export": export}
