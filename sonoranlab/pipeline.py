"""`sonoranlab process`: raw HD frames -> a validated job dir and, only on a validator PASS, a Godot export.

Per direction, per frame (processing-v1; parity with benchmark/tools/process.py on the same raw frames):
  alpha.extract_alpha -> alpha.despill -> pixelize -> alpha.downscale_alpha -> palette.snap (off-palette counted)
  -> alpha.remove_fringe -> palette.limit_colours -> alpha.compose
then register.register_clip per direction -> validate -> contact sheet -> (PASS only) slice.pack_sheet (west rows
mirrored from east) -> export.godot into JOB/export/. Every parameter comes from the profile (`processing:`, `alpha:`,
`palette:`, the character row); the pixelize scale is inferred as raw frame edge / cell and must be an exact integer.

The inputs are byte-copied into JOB/raw/ first and the chain reads only those copies. Stages land in
JOB/processed/{01_alpha,02_pixelize,03_palette,04_clean,05_registered}/. Nothing touches the network, ComfyUI or a GPU.
Exit codes (`run` returns them, `PipelineError` is 2): 0 PASS and exported, 1 validator FAIL (no export), 2 usage,
config or input error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

from sonoranlab import artifacts
from sonoranlab.alpha import compose, despill, downscale_alpha, extract_alpha, remove_fringe
from sonoranlab.contact import contact_sheet
from sonoranlab.export.godot import export_godot
from sonoranlab.palette import limit_colours, snap
from sonoranlab.pixelize import pixelize
from sonoranlab.profile import load_palette, load_profile
from sonoranlab.register import register_clip
from sonoranlab.slice import pack_sheet, read_frame_dir, slice_sheet
from sonoranlab.validate import validate

STAGES = ("01_alpha", "02_pixelize", "03_palette", "04_clean")
UPSTREAM = ("selection.json", "manifest.json")  # provenance files next to RAW (the generation side)
UPSTREAM_KEYS = ("set", "track", "character", "seed", "variant", "model", "workflow", "workflow_sha256", "job")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
GODOT_CHECK = Path(__file__).resolve().parent.parent / "tools" / "godot" / "spriteframes_check"


class PipelineError(Exception):
    """Usage, config or input error (exit 2); the message says what to fix."""


def _hex(c: str) -> tuple[int, int, int]:
    return tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))


def process_frame(
    im: Image.Image,
    *,
    scale: int,
    palette: np.ndarray,
    bg_key: tuple[int, int, int],
    key_tol: float,
    despill_px: int,
    downscaler: str,
    thickness: int,
    threshold: int,
    fringe_passes: int,
    max_colours: int,
) -> tuple[dict[str, np.ndarray], dict]:
    """One raw HD frame -> {stage: RGBA frame} for STAGES (04_clean is final, unregistered) and per-frame stats."""
    rgb, alpha = extract_alpha(im, bg_key, key_tol)
    rgb, despilled = despill(rgb, alpha, despill_px, bg_key)
    small = pixelize(rgb, scale, method=downscaler, thickness=thickness)
    opaque = downscale_alpha(alpha, scale, threshold)
    snapped, off = snap(small, palette)
    kept, fringe = remove_fringe(small, snapped, opaque, bg_key, fringe_passes)
    capped, merged = limit_colours(snapped, kept, max_colours)
    a8 = opaque.astype(np.uint8) * 255
    stages = {
        "01_alpha": np.dstack([rgb, np.rint(alpha).astype(np.uint8)]),
        "02_pixelize": np.dstack([small, a8]),
        "03_palette": np.dstack([snapped, a8]),
        "04_clean": compose(capped, kept),
    }
    stats = {
        "off_palette_before": int((off & opaque).sum()),
        "fringe_removed": fringe,
        "colours_merged": merged,
        "despilled_hd_px": despilled,
    }
    return stages, stats


def _pixeloe_logs_to_stderr() -> None:
    """PixelOE adds a stdout log handler on import unless one exists; stdout is for the CLI's result. lastResort
    writes WARNING and up to whatever sys.stderr is at the time."""
    logging.getLogger("PixelOE").handlers[:] = [logging.lastResort]


def _scale(size: tuple[int, int], cell: tuple[int, int], what: str) -> int:
    (w, h), (cw, ch) = size, cell
    if w % cw or h % ch or w // cw != h // ch or w < cw:
        raise PipelineError(
            f"{what}: {w}x{h} is not an integer multiple of the {cw}x{ch} cell (scale = raw edge / cell, e.g. x3)"
        )
    return w // cw


def _upstream(job: Path, names: list[str]) -> dict | None:
    out = {}
    for n in names:
        p = job / "raw" / n
        rec: dict = {"sha256": artifacts.sha256_file(p)}
        try:
            doc = json.loads(p.read_text())
        except (ValueError, UnicodeDecodeError):
            doc = None
        if isinstance(doc, dict):
            rec |= {k: doc[k] for k in UPSTREAM_KEYS if isinstance(doc.get(k), (str, int, float))}
        out[n] = rec
    return out or None


def expected_godot(
    sheet: np.ndarray, character: str, cell: tuple[int, int], rows: list[str], clips: list[dict]
) -> dict:
    """tools/godot/spriteframes_check expectations, computed from the sheet by hand (not through the exporter)."""
    cw, ch = cell
    sheet = sheet.copy()
    sheet[sheet[..., 3] == 0] = 0  # what the PNG stores: invisible pixels carry no colour
    anims = {}
    for c in clips:
        for r, d in enumerate(rows):
            frames = []
            for i in range(c["frames"]):
                x, y = (c["start"] + i) * cw, r * ch
                region = np.ascontiguousarray(sheet[y : y + ch, x : x + cw])
                frames.append([x, y, cw, ch, hashlib.sha256(region.tobytes()).hexdigest()])
            anims[f"{c['name']}_{d}"] = {"frames": frames, "speed": float(c["fps"]), "loop": c["loop"]}
    return {
        "sheet": f"res://{character}/{character}.png",
        "sheet_size": [sheet.shape[1], sheet.shape[0]],
        "animations": anims,
    }


GODOT_RETRIES = 2  # DYL-101: at most this many re-runs, only for the editor-singleton crash below
GODOT_BACKOFF = (1.0, 3.0)  # seconds slept before retry 1 and 2
_SINGLETON = ('Parameter "singleton" is null', "is_cmdline_mode")
_ERRORISH = re.compile(r"ERROR|Parse Error|Failed|failed|Cannot|Invalid")


def is_transient_singleton(p: subprocess.CompletedProcess) -> bool:
    """Godot 4.7.2's intermittent `--import` crash: EditorNode::is_cmdline_mode() runs with no EditorNode (SIGABRT).
    True only for returncode -6/134, both signature strings in stderr, and nothing else that reads as a failure: every
    ERROR-ish line must be one of the two signature lines and the checker's JSON verdict (if any) must not be a fail."""
    if p.returncode not in (-6, 134) or not all(s in p.stderr for s in _SINGLETON):
        return False
    for ln in (ANSI.sub("", p.stdout) + p.stderr).splitlines():
        if ln.startswith("{") and '"pass":false' in ln.replace(" ", ""):
            return False
        if _ERRORISH.search(ln) and not any(s in ln for s in _SINGLETON):
            return False
    return True


def run_godot(
    godot: str,
    args: list[str],
    *,
    cwd: Path,
    step: str = "godot",
    attempts: list[dict] | None = None,
    sleep=time.sleep,
) -> subprocess.CompletedProcess:
    """`godot *args`, re-run (<= GODOT_RETRIES, with backoff) only on the transient singleton crash. Every attempt is
    appended to `attempts` as {step, returncode, transient_signature, seconds}; the LAST attempt's result is returned
    and judged by the caller exactly as before, so a retry can never turn a real failure into a pass."""
    for n in range(GODOT_RETRIES + 1):
        t = time.perf_counter()
        p = subprocess.run([godot, *args], cwd=cwd, capture_output=True, text=True, timeout=300)
        transient = is_transient_singleton(p)
        if attempts is not None:
            attempts.append(
                {
                    "step": step,
                    "returncode": p.returncode,
                    "transient_signature": transient,
                    "seconds": round(time.perf_counter() - t, 3),
                }
            )
        if not transient or n == GODOT_RETRIES:
            return p
        sleep(GODOT_BACKOFF[n])
    raise AssertionError("unreachable")


def check_in_godot(
    sheet: np.ndarray, *, character: str, cell: tuple[int, int], rows: list[str], clips: list[dict]
) -> dict:
    """Export into a temp copy of tools/godot/spriteframes_check, `godot --import` it and run the checker there
    (as tests/sonoranlab/test_export_godot.py does). Returns the record for manifest.json; leaves nothing behind.
    `godot_attempts` lists every godot process run (see run_godot); only the final attempt of a step is judged."""
    godot = shutil.which("godot")
    if godot is None:
        raise PipelineError("--godot-check: godot not found on PATH")
    attempts: list[dict] = []

    def call(*args: str, cwd: Path, step: str) -> subprocess.CompletedProcess:
        return run_godot(godot, list(args), cwd=cwd, step=step, attempts=attempts)

    with tempfile.TemporaryDirectory(prefix="sonoranlab-godot-") as tmp:
        proj = Path(tmp) / "proj"
        shutil.copytree(GODOT_CHECK, proj)
        paths = export_godot(sheet, character=character, cell=cell, rows=rows, clips=clips, out_dir=proj, provenance={})
        (proj / "expected.json").write_text(json.dumps(expected_godot(sheet, character, cell, rows, clips)))
        version = call("--version", cwd=Path(tmp), step="version").stdout.strip()
        imp = call("--headless", "--path", str(proj), "--import", cwd=Path(tmp), step="import")
        chk = call(
            "--headless", "--path", str(proj), "--fixed-fps", "60", "--script", "res://check_spriteframes.gd", "--",
            f"res://{character}/{character}_frames.tres", "res://expected.json", cwd=Path(tmp), step="check",
        )  # fmt: skip
        log = imp.stdout + imp.stderr + chk.stdout + chk.stderr
        lines = [ANSI.sub("", ln) for ln in log.splitlines() if ln.strip() and not ln.startswith("{")]
        try:
            result = json.loads(chk.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            result = {"pass": False, "error": "checker printed no JSON result"}
        ok = imp.returncode == 0 and chk.returncode == 0 and result.get("pass") is True and "ERROR" not in log
        return {
            "pass": ok,
            "godot": version,
            "import_exit": imp.returncode,
            "check_exit": chk.returncode,
            "godot_attempts": attempts,
            "result": result,
            "checked_sha256": {Path(paths[k]).name: artifacts.sha256_file(Path(paths[k])) for k in ("png", "tres")},
            "log_tail": lines[-8:],
        }


def run(
    raw: Path,
    *,
    profile: Path,
    character: str,
    anim: str,
    out: Path,
    stills: Path | None = None,
    sheet: bool = False,
    start_col: int | None = None,
    anchors: Path | None = None,
    godot_check: bool = False,
) -> dict:
    """The whole chain; returns {"exit": 0|1, "job", "pass", "failures", "warnings", "report", "contact", "export",
    "godot_check"}. Raises PipelineError (exit 2) before touching `out` for every input problem it can see up front,
    and after writing manifest.json for a refused export, a failed Godot check or a raw copy that changed.
    ProfileError (also exit 2) propagates."""
    t0 = time.perf_counter()
    raw, out, profile = Path(raw).expanduser(), Path(out).expanduser(), Path(profile).expanduser()
    prof = load_profile(profile)
    spec = prof.character(character)
    pal = load_palette(prof)
    clip = spec.animations.get(anim)
    if clip is None:
        raise PipelineError(f"--anim {anim!r} is not a clip of {spec.name} ({', '.join(spec.animations)})")
    pr = prof.processing
    if pr is None:
        raise PipelineError(
            f"{profile}: no `processing:` section; `sonoranlab process` needs it (see profiles/twos-are-up.yaml)"
        )
    if spec.anchor_mode == "rig" or anchors is not None:
        raise PipelineError(
            "rig registration (--anchors) is not supported yet: the validator (ADR-0008) only gates anchor_mode foot "
            f"and bbox_bottom, so a rig clip cannot pass (character {spec.name} is anchor_mode {spec.anchor_mode})"
        )
    if start_col is not None and not sheet:
        raise PipelineError("--start-col only applies with --sheet")
    if out.resolve().is_relative_to(prof.export.godot_project.resolve()):
        raise PipelineError(f"--out {out} is inside the shipped game {prof.export.godot_project}; use a job dir")
    if godot_check and shutil.which("godot") is None:
        raise PipelineError("--godot-check: godot not found on PATH")
    order, gen, mirror = list(prof.directions.order), list(prof.directions.generate), prof.directions.mirror_animations

    # ---- find and size-check the inputs before the job dir exists
    sources: dict[str, Path] = {}
    if sheet:
        if not raw.is_file():
            raise PipelineError(f"{raw}: --sheet needs a sheet PNG file")
        start_col = 1 if start_col is None else start_col
        if start_col < 0:
            raise PipelineError(f"--start-col {start_col}: must be >= 0")
        with Image.open(raw) as im:
            w, h = im.size
            sheet_px = np.asarray(im.convert("RGBA"))
        cols = start_col + clip.frames
        if h % len(order) or w % cols:
            raise PipelineError(
                f"{raw}: sheet {w}x{h} is not {len(order)} rows x {cols} columns of square cells "
                f"(profile directions.order; clip {anim} = {clip.frames} frames from column {start_col})"
            )
        scale = _scale((w // cols, h // len(order)), spec.cell, f"{raw.name} cell")
        if start_col > 0 and (sheet_px[:, : spec.cell[0] * scale, 3] > 0).any():
            if stills is not None:
                raise PipelineError("--sheet column 0 and --stills: two sources of stills")
            if scale > 1:
                raise PipelineError(
                    f"--sheet column 0 holds still poses at HD scale {scale}; the chain has no still-processing "
                    "policy: pass final-resolution stills with --stills, or blank column 0"
                )
        sources[raw.name] = raw
    else:
        if not raw.is_dir():
            raise PipelineError(f"{raw}: not a directory of <direction>_<i>.png frames (or pass --sheet)")
        try:
            found = read_frame_dir(raw, gen)
        except ValueError as e:
            raise PipelineError(f"{e} (raw frames are the generated directions {', '.join(gen)})") from None
        if not found:
            raise PipelineError(f"{raw}: no <direction>_<i>.png frames for {', '.join(gen)}")
        sizes = {}
        for p in (p for ps in found.values() for p in ps):
            with Image.open(p) as im:
                sizes[p.name] = im.size
        if len(set(sizes.values())) > 1:
            raise PipelineError(f"{raw}: frames differ in size: {sorted(set(sizes.values()))}")
        name, size = next(iter(sizes.items()))
        scale = _scale(size, spec.cell, name)
        sources |= {p.name: p for ps in found.values() for p in ps}
    still_names = []
    if stills is not None:
        stills = Path(stills).expanduser()
        hits = [stills / f"still_{d}.png" for d in order if (stills / f"still_{d}.png").is_file()]
        if not hits:
            raise PipelineError(f"--stills {stills}: no still_<direction>.png for {', '.join(order)}")
        for p in hits:
            with Image.open(p) as im:
                if im.size != spec.cell:
                    raise PipelineError(f"{p}: still is {im.size[0]}x{im.size[1]}, the cell is {spec.cell}")
            sources[p.name] = p
            still_names.append(p.name)
    up_names = [n for n in UPSTREAM if (raw.parent / n).is_file()]
    sources |= {n: raw.parent / n for n in up_names}

    # ---- job dir and immutable raw copies; from here on only the copies are read
    try:
        job = artifacts.create_job(out)
        inputs = artifacts.ingest_raw(job, sources)
    except (FileExistsError, ValueError) as e:
        raise PipelineError(str(e)) from None
    rawdir = job / "raw"
    t_ingest = time.perf_counter()

    still_px = {}
    for n in still_names:
        with Image.open(rawdir / n) as im:
            still_px[n.removeprefix("still_").removesuffix(".png")] = np.asarray(im.convert("RGBA")).copy()
    if sheet:
        with Image.open(rawdir / raw.name) as im:
            sheet_px = np.asarray(im.convert("RGBA"))
            cells = slice_sheet(
                sheet_px, (spec.cell[0] * scale, spec.cell[1] * scale), order, clip.frames,
                start_col=start_col,
            )  # fmt: skip
        if start_col > 0 and scale == 1:
            for r, d in enumerate(order):
                f = sheet_px[r * spec.cell[1] : (r + 1) * spec.cell[1], : spec.cell[0]]
                if (f[..., 3] > 0).any():
                    still_px[d] = f.copy()
        frames_in = {d: [Image.fromarray(f, "RGBA") for f in cells[d]] for d in gen}
    else:
        frames_in = {d: [Image.open(p) for p in ps] for d, ps in read_frame_dir(rawdir, gen).items()}
    params = {
        "processing": pr.version,
        "downscaler": pr.downscaler,
        "thickness": pr.thickness,
        "despill_px": pr.despill_px,
        "key_tol": pr.key_tol,
        "scale": scale,
        "alpha_threshold": prof.alpha.threshold,
        "bg_key": prof.alpha.bg_key,
        "fringe_passes": prof.alpha.fringe_passes,
        "fringe_tolerance": prof.alpha.fringe_tolerance,
        "max_colours": prof.palette.max_colors_per_cell,
        "palette": {"path": str(prof.palette.source), "sha256": prof.palette.sha256},
        "cell": list(spec.cell),
        "anchor_mode": spec.anchor_mode,
        "foot_y": spec.foot_y,
        "anchor_x": spec.anchor_x,
        "padding": spec.padding,
        "limits": {k: v for k, v in vars(spec.limits).items() if v is not None},
        "limits_source": spec.limits_source,
        "animation": {"name": anim, "frames": clip.frames, "fps": clip.fps, "loop": clip.loop}
        | ({"loop_from": clip.loop_from} if clip.loop_from is not None else {}),
        "directions": {"order": order, "generate": gen, "mirror": dict(mirror)},
        "input": {"raw": str(raw.resolve()), "mode": "sheet" if sheet else "frames"}
        | ({"start_col": start_col} if sheet else {})
        | {"stills": sorted(still_names)},
    }
    chain = {
        "scale": scale,
        "palette": pal,
        "bg_key": _hex(prof.alpha.bg_key),
        "key_tol": pr.key_tol,
        "despill_px": pr.despill_px,
        "downscaler": pr.downscaler,
        "thickness": pr.thickness,
        "threshold": prof.alpha.threshold,
        "fringe_passes": prof.alpha.fringe_passes,
        "max_colours": prof.palette.max_colors_per_cell,
    }

    # ---- per-frame chain, one cell at a time
    _pixeloe_logs_to_stderr()
    staged: dict[str, dict[str, list[np.ndarray]]] = {s: {} for s in STAGES}
    pre: dict[str, list[dict]] = {}
    for d in [d for d in order if d in frames_in]:
        pre[d] = []
        for im in frames_in[d]:
            st, stats = process_frame(im, **chain)
            for s in STAGES:
                staged[s].setdefault(d, []).append(st[s])
            pre[d].append(stats)
            im.close()
    outputs: dict[str, str] = {}
    for s in STAGES:
        outputs |= artifacts.write_stage(job, s, staged[s])
    t_chain = time.perf_counter()

    registered, reg = {}, {}
    for d, fs in staged["04_clean"].items():
        registered[d], reg[d] = register_clip(
            fs,
            spec.cell,
            foot_y=spec.foot_y,
            anchor_x=spec.anchor_x,
            mode=spec.anchor_mode,
            anchor_frame=None if clip.loop or spec.anchor_mode == "rig" else 0,  # non-loop: stay where the still stands
        )
    outputs |= artifacts.write_stage(job, "05_registered", registered)
    try:
        rep = validate(
            registered,
            stills=still_px,
            cell=spec.cell,
            frames=clip.frames,
            directions=gen,
            palette=pal,
            foot_y=spec.foot_y,
            anchor_x=spec.anchor_x,
            padding=spec.padding,
            limits=spec.limits,
            max_colours=prof.palette.max_colors_per_cell,
            loop=clip.loop,
            anchor_mode=spec.anchor_mode,
            validator_mode=clip.validator_mode,
            fringe_tolerance=prof.alpha.fringe_tolerance,
            mirror=dict(mirror),
            pre=pre,
            registration=reg,
            limits_source=spec.limits_source,
        )
    except ValueError as e:
        raise PipelineError(f"validate: {e}") from None
    prov = {
        "profile": {"path": str(profile.resolve()), "sha256": artifacts.sha256_file(profile)},
        "inputs": dict(sorted(inputs.items())),
        "upstream": _upstream(job, up_names),
    }
    rep = {"pass": rep["pass"], "character": spec.name, "animation": anim, **rep, "params": params, "provenance": prov}
    outputs["report.json"] = artifacts.sha256_file(artifacts.write_json(job, "report.json", rep))
    img = contact_sheet(registered, rep, cell=spec.cell, foot_y=spec.foot_y, rows=list(registered))
    with open(job / "contact.png", "xb") as f:
        img.save(f, "PNG")
    outputs["contact.png"] = artifacts.sha256_file(job / "contact.png")
    t_validate = time.perf_counter()

    error, export, gc = None, None, None
    if rep["pass"]:
        clips = [{"name": anim, "start": 1, "frames": clip.frames, "fps": clip.fps, "loop": clip.loop}]
        if clip.loop_from is not None:
            clips[0]["loop_from"] = clip.loop_from
        try:
            packed = pack_sheet(
                registered, spec.cell, order, clip.frames, mirror=dict(mirror), stills=still_px or None, start_col=1,
                required=order,
            )  # fmt: skip
            if godot_check:
                gc = check_in_godot(packed, character=spec.name, cell=spec.cell, rows=order, clips=clips)
                if not gc["pass"]:
                    error = f"Godot check failed (no export written): {gc['result']}; log tail: {gc['log_tail'][-3:]}"
            if error is None:
                side = {
                    "tool": "sonoranlab process",
                    "processing": pr.version,
                    "animation": anim,
                    "validator": {"pass": True, "warnings": rep["warnings"]},
                    "report_sha256": outputs["report.json"],
                    **prov,
                }
                export = export_godot(
                    packed, character=spec.name, cell=spec.cell, rows=order, clips=clips, out_dir=job / "export",
                    provenance=side, forbid_under=prof.export.godot_project,
                )  # fmt: skip
                for p in export.values():
                    outputs[Path(p).relative_to(job.resolve()).as_posix()] = artifacts.sha256_file(Path(p))
                if gc and any(outputs[f"export/{spec.name}/{n}"] != h for n, h in gc["checked_sha256"].items()):
                    error = "the exported PNG/.tres differ from the ones the Godot check loaded"
        except ValueError as e:
            error = f"export: {e}"
    t_export = time.perf_counter()

    code = 2 if error else (0 if rep["pass"] else 1)
    timings = {
        "ingest_s": t_ingest - t0,
        "chain_s": t_chain - t_ingest,
        "register_validate_contact_s": t_validate - t_chain,
        "export_s": t_export - t_validate,
    }
    man = artifacts.build_manifest(
        job=job,
        profile_path=profile.resolve(),
        character=spec.name,
        animation=anim,
        inputs=inputs,
        outputs=outputs,
        params=params,
        timings={k: round(v, 3) for k, v in timings.items()} | {"total_s": round(time.perf_counter() - t0, 3)},
        upstream=prov["upstream"],
    )
    man["godot_check"] = gc
    man["result"] = {"pass": rep["pass"], "exit": code, "exported": export is not None, "error": error}
    artifacts.write_json(job, "manifest.json", man)
    if problems := artifacts.verify_raw(job):
        raise PipelineError(f"{job}: raw copies changed during the run: {'; '.join(problems)}")
    if error:
        raise PipelineError(error)
    return {
        "exit": code,
        "job": str(job),
        "pass": rep["pass"],
        "failures": rep["failures"],
        "warnings": rep["warnings"],
        "report": str(job / "report.json"),
        "contact": str(job / "contact.png"),
        "export": export,
        "godot_check": None if gc is None else {k: gc[k] for k in ("pass", "godot", "result", "godot_attempts")},
    }
