"""Isolated side-by-side Godot style render (DYL-131, docs/09 "Smoke render").

One candidate sheet beside up to three approved sibling sheets, drawn in a disposable Godot project under OUT/project/:
each character in its own square cell (never cropped), bottoms on one baseline, 62 px/m, one background, nearest
filtering, the game's sticker-border shader when the owner's private copy is given. The tool renders, then checks the
pixels in Python (nearest sampling, binary alpha, border zones, enumeration, timing). It never fills
`agent_recommendation` or `owner_style_acceptance`: whether the new class looks like it belongs is the owner's call.

Exit codes of `run_smoke` results: 0 pass, 1 an objective check or the engine failed, 3 engine unavailable (never a
pass). Bad inputs raise SmokeError before anything is written (CLI exit 2).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from sonoranlab import profile as profiles
from sonoranlab.api import paths
from sonoranlab.api.registry import ToolError
from sonoranlab.artifacts import require_regular_file, sha256_file

TOOL = Path(__file__).resolve().parents[1] / "tools/godot/side_by_side"
REPO = Path(__file__).resolve().parents[1]
SCALES = (1, 4)
MAX_SIBLINGS = 3
BACKGROUND = "#3a3a3a"
MARGIN, GAP, GROUP_GAP = 8, 8, 16  # 1x pixels: canvas margin, between characters, between groups / lines
MAX_EDGE = 12288  # render pixels per image edge (llvmpipe textures are safe up to 16384)
MAX_TOTAL_PIXELS = 300_000_000
MAX_TOTAL_BYTES = 32 << 20
COLOR_TOL = (
    2  # max channel deviation accepted between source and render (sRGB round trip); the observed max is recorded
)
REQUIRED_UNIFORMS = ("sheet", "grid", "cell", "cell_pixels")
LABEL = re.compile(r"^[a-z0-9][a-z0-9_]*$")
SHADER_NAME = "PixelStandee.gdshader"


class SmokeError(ValueError):
    """Bad or unsafe input; nothing has been written."""


# ---------------------------------------------------------------- inputs


@dataclass
class Sheet:
    label: str
    form: str  # "job" (export dir with a sonoranlab sidecar) or "game" (characters.json entry beside the PNG)
    png: Path
    png_sha256: str
    meta_files: dict[str, str]  # file name -> sha256 of the sidecar / characters.json that described the sheet
    cell: int
    columns: int
    rows: list[str]
    clips: list[dict]
    mirrored: dict[str, str] | None
    pixels: np.ndarray  # (H, W, 4) uint8

    def cell_at(self, row: int, col: int) -> np.ndarray:
        c = self.cell
        return self.pixels[row * c : (row + 1) * c, col * c : (col + 1) * c]

    def describe(self) -> dict:
        return {
            "label": self.label,
            "form": self.form,
            "sheet": self.png.name,
            "png_sha256": self.png_sha256,
            "metadata_sha256": self.meta_files,
            "cell": self.cell,
            "columns": self.columns,
            "rows": self.rows,
            "clips": self.clips,
        }


def _roots(roots):
    return tuple(roots) if roots is not None else (*paths.read_roots(), paths.state_root())


def _confined(path: str | Path, roots) -> Path:
    """Resolve inside the read roots / STATE (no game checkout, no escaping link); a symlinked leaf is refused too."""
    try:
        given = paths._path(path)
        resolved = paths._confine(given, _roots(roots))
        if given.is_symlink():
            raise SmokeError(f"{path}: symlinks are refused")
        if given.is_file():
            require_regular_file(given)
    except ToolError as exc:
        raise SmokeError(f"{path}: {exc.error['message']}") from exc
    except (ValueError, OSError) as exc:
        raise SmokeError(str(exc)) from exc
    return resolved


def _sidecars(base: Path, label: str | None = None) -> list[Path]:
    """`<label>.sonoranlab.json` files of an export: beside, under export/<c>/, or under <c>/ (as godot_check looks)."""
    name = f"{label}.sonoranlab.json" if label else "*.sonoranlab.json"
    return sorted({p for pattern in (name, f"export/*/{name}", f"*/{name}") for p in base.glob(pattern)})


def _json(path: Path, roots) -> dict:
    try:
        doc = json.loads(_confined(path, roots).read_text())
    except (OSError, ValueError) as exc:
        raise SmokeError(f"{path}: unreadable JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise SmokeError(f"{path}: expected a JSON object")
    return doc


def _entry_ok(entry, where: str) -> None:
    need = {"sheet": str, "cell": int, "columns": int, "clips": list}
    if not isinstance(entry, dict) or any(not isinstance(entry.get(k), t) for k, t in need.items()):
        raise SmokeError(f"{where}: characters.json entry needs sheet, cell, columns and clips")
    for clip in entry["clips"]:
        if not isinstance(clip, dict) or any(k not in clip for k in ("name", "start", "frames", "fps", "loop")):
            raise SmokeError(f"{where}: clip needs name, start, frames, fps, loop")
        if not (clip["start"] >= 0 and clip["frames"] >= 1 and clip["start"] + clip["frames"] <= entry["columns"]):
            raise SmokeError(f"{where}: clip {clip['name']!r} does not fit {entry['columns']} columns")
        if not clip["fps"] > 0 or Path(clip["name"]).name != clip["name"] or clip["name"] in ("still", "overview"):
            raise SmokeError(f"{where}: clip {clip['name']!r} has a bad name or fps")


def load_sheet(source: Path, label: str, profile: profiles.Profile, roots=None) -> Sheet:
    """A job/export dir (sonoranlab sidecar), a dir holding characters.json, or a sheet PNG beside its metadata."""
    source = Path(source).expanduser()
    base = _confined(source, roots)
    found: list[Path] = []
    if base.is_dir():
        found = _sidecars(base, label)
        if len(found) > 1:
            raise SmokeError(f"{source}: several exports for {label!r}: {[str(p) for p in found]}")
        directory = found[0].parent if found else base
    else:
        directory = base.parent
        if (side := base.with_suffix(".sonoranlab.json")).is_file():
            found = [side]
    if found:
        side = found[0]
        doc = _json(side, roots)
        entry, rows = doc.get("characters_json_entry"), (doc.get("export") or {}).get("rows")
        if doc.get("character") != label:
            raise SmokeError(f"{side}: sidecar is for {doc.get('character')!r}, not {label!r}")
        _entry_ok(entry, str(side))
        meta, form, mirrored = {side.name: sha256_file(side)}, "job", None
    else:
        cj = directory / "characters.json"
        if not cj.is_file():
            raise SmokeError(f"{source}: no {label}.sonoranlab.json and no characters.json beside it")
        doc = _json(cj, roots)
        entry = (doc.get("characters") or {}).get(label)
        if entry is None:
            raise SmokeError(f"{cj}: no entry for {label!r}")
        _entry_ok(entry, f"{cj}[{label}]")
        rows, mirrored = doc.get("rows"), doc.get("mirrored_animation_rows")
        meta, form = {cj.name: sha256_file(cj)}, "game"
        if base.is_file() and base.name != entry["sheet"]:
            raise SmokeError(f"{source}: characters.json says {label!r} is {entry['sheet']!r}")
    if rows != list(profile.directions.order):
        raise SmokeError(f"{source}: rows {rows} differ from the profile's {list(profile.directions.order)}")
    png = _confined(directory / entry["sheet"], roots)
    digest = sha256_file(png)
    if form == "job" and digest != (doc.get("sha256") or {}).get(png.name):
        raise SmokeError(f"{png}: sha256 differs from its sidecar; refusing a changed export")
    with Image.open(png) as im:
        pixels = np.array(im.convert("RGBA"))
    cell, columns = entry["cell"], entry["columns"]
    if pixels.shape[:2] != (len(rows) * cell, columns * cell):
        raise SmokeError(f"{png}: {pixels.shape[1]}x{pixels.shape[0]} is not {columns} x {len(rows)} cells of {cell}")
    return Sheet(label, form, png, digest, meta, cell, columns, list(rows), entry["clips"], mirrored, pixels)


# ---------------------------------------------------------------- source checks


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """Chebyshev dilation inside the array (outside counts as empty), as the shader's per-cell mask_at does."""
    h, w = mask.shape
    pad = np.pad(mask, r)
    out = np.zeros_like(mask)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out |= pad[dy : dy + h, dx : dx + w]
    return out


def zones(cell_rgba: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(opaque, keyline ring, outer ring) masks: the 3x3 and 5x5 neighbourhoods of the shader's border."""
    opaque = cell_rgba[..., 3] >= 128
    near = _dilate(opaque, 1)
    return opaque, near & ~opaque, _dilate(opaque, 2) & ~near


def baked_border_share(cell_rgba: np.ndarray) -> float:
    """Share of silhouette-edge pixels that are near-white (a baked sticker border); 0 for tiny or empty cells."""
    opaque = cell_rgba[..., 3] >= 128
    inner = np.pad(opaque, 1)
    four = inner[:-2, 1:-1] & inner[2:, 1:-1] & inner[1:-1, :-2] & inner[1:-1, 2:]
    edge = opaque & ~four
    if edge.sum() < 8:
        return 0.0
    return float((cell_rgba[..., :3][edge].min(axis=-1) >= 235).mean())


def source_checks(sheet: Sheet) -> dict:
    a = sheet.pixels[..., 3]
    semi = int(((a > 0) & (a < 255)).sum())
    worst, where = 0.0, None
    for r in range(len(sheet.rows)):
        for c in range(sheet.columns):
            cell = sheet.cell_at(r, c)
            if cell[..., 3].any() and (share := baked_border_share(cell)) > worst:
                worst, where = share, [r, c]
    return {
        "alpha_binary": {"pass": semi == 0, "semi_transparent_pixels": semi},
        "no_baked_border": {"pass": worst < 0.5, "worst_near_white_edge_share": round(worst, 3), "cell": where},
    }


def compare_manifest(sheet: Sheet, spec: profiles.CharacterSpec) -> dict:
    """The candidate's recorded clips against the profile's animations of the same name (start is the sheet's own)."""
    bad = []
    for c in sheet.clips:
        want = spec.animations.get(c["name"])
        if want is None:
            bad.append(f"{c['name']}: not a profile animation")
            continue
        got = (c["frames"], float(c["fps"]), bool(c["loop"]), c.get("loop_from"))
        exp = (want.frames, float(want.fps), bool(want.loop), want.loop_from)
        if got != exp:
            bad.append(f"{c['name']}: sheet {got} != profile {exp} (frames, fps, loop, loop_from)")
    if sheet.cell != spec.cell[0]:
        bad.append(f"cell {sheet.cell} != profile {spec.cell[0]}")
    return {
        "pass": not bad,
        "mismatches": bad,
        "profile_clips_absent_from_sheet": sorted(set(spec.animations) - {c["name"] for c in sheet.clips}),
    }


def mirrored_rows_exact(sheet: Sheet, mirror: dict[str, str]) -> dict:
    bad = []
    for dst, src in mirror.items():
        for clip in sheet.clips:
            for f in range(clip["frames"]):
                col = clip["start"] + f
                a, b = sheet.cell_at(sheet.rows.index(dst), col), sheet.cell_at(sheet.rows.index(src), col)
                if not np.array_equal(a, b[:, ::-1]):
                    bad.append(f"{dst}/{clip['name']}[{f}] is not the flip of {src}")
    return {"pass": not bad, "mismatches": bad[:20], "rows": mirror}


# ---------------------------------------------------------------- layout


@dataclass(frozen=True)
class Quad:
    part: int
    col: int
    row: int
    x: int
    y: int  # top-left of the character's own cell, 1x pixels


@dataclass
class RenderSpec:
    name: str
    scale: int
    width: int
    height: int  # 1x pixels
    quads: list[Quad]
    meta: dict


def layout(groups: list[list[tuple[int, int, int]]], cells: list[int], scale_max: int = max(SCALES)):
    """Flow `groups` (each a list of (part, col, row)) left to right, wrapping at MAX_EDGE; characters in a group
    stand side by side on the group's baseline (cell bottoms aligned), each in its own uncropped cell."""
    limit = MAX_EDGE // scale_max
    placed: list[tuple[int, int, int, int, int]] = []  # (part, col, row, x, y)
    x = y = MARGIN
    line_h = width = 0
    for g in groups:
        gw = sum(cells[p] for p, _, _ in g) + GAP * (len(g) - 1)
        gh = max(cells)  # one baseline for every group: cell bottoms aligned at the tallest cell
        if gw + 2 * MARGIN > limit:
            raise SmokeError(f"a group of {gw}px does not fit the {limit}px render width limit")
        if x > MARGIN and x + gw + MARGIN > limit:
            x, y, line_h = MARGIN, y + line_h + GROUP_GAP, 0
        gx = x
        for p, col, row in g:
            placed.append((p, col, row, gx, y + gh - cells[p]))
            gx += cells[p] + GAP
        x, line_h, width = x + gw + GROUP_GAP, max(line_h, gh), max(width, x + gw + MARGIN)
    height = y + line_h + MARGIN
    if height * scale_max > MAX_EDGE:
        raise SmokeError(f"render height {height * scale_max}px exceeds the {MAX_EDGE}px limit")
    return width, height, [Quad(*q) for q in placed]


def plan_renders(parts: list[Sheet], scales=SCALES) -> list[RenderSpec]:
    """Overview (stills of all rows) plus, per row, the column-0 still and every frame of every candidate clip."""
    cells = [p.cell for p in parts]
    cand, rows = parts[0], parts[0].rows
    specs: list[RenderSpec] = []

    def add(name: str, groups, meta: dict) -> None:
        w, h, quads = layout(groups, cells)
        for s in scales:
            specs.append(RenderSpec(f"{name}_{s}x", s, w, h, quads, meta))

    add("overview", [[(i, 0, r) for i in range(len(parts))] for r in range(len(rows))], {"kind": "overview"})
    for r, row in enumerate(rows):
        add(f"{row}_still", [[(i, 0, r) for i in range(len(parts))]], {"kind": "still", "row": row, "clip": "still"})
        for clip in cand.clips:
            have = [{c["name"]: c for c in p.clips}.get(clip["name"]) for p in parts]
            n = max(c["frames"] for c in have if c)  # the longest shows every frame; shorter clips wrap (see meta)
            groups = [
                [(i, (c["start"] + g % c["frames"]) if c else 0, r) for i, c in enumerate(have)] for g in range(n)
            ]
            meta = {
                "kind": "clip",
                "row": row,
                "clip": clip["name"],
                "groups": n,
                "timing": {
                    k: clip[k] for k in ("start", "frames", "fps", "loop", "loop_from") if clip.get(k) is not None
                }
                | {"seconds": round(clip["frames"] / clip["fps"], 4)},
                "frames_per_character": {
                    p.label: (c["frames"] if c else None) for p, c in zip(parts, have, strict=True)
                },
            }
            add(f"{row}_{clip['name']}", groups, meta)
    return specs


def budget(specs: list[RenderSpec]) -> int:
    pixels = sum(s.width * s.height * s.scale**2 for s in specs)
    if pixels > MAX_TOTAL_PIXELS:
        raise SmokeError(f"planned {pixels:,} render pixels exceed the {MAX_TOTAL_PIXELS:,} budget; refusing")
    return pixels


# ---------------------------------------------------------------- project


def uniforms(shader_text: str) -> set[str]:
    return set(re.findall(r"^\s*uniform\s+\w+\s+(\w+)", re.sub(r"//.*", "", shader_text), re.M))


def write_project(out: Path, parts: list[Sheet], specs: list[RenderSpec], density: float, shader: Path | None) -> dict:
    project = out / "project"
    (project / "sheets").mkdir(parents=True)
    for name in ("project.godot", "render_plan.gd", "plain.gdshader"):
        shutil.copyfile(TOOL / name, project / name)
    if shader is not None:
        shutil.copyfile(shader, project / SHADER_NAME)
    for p in parts:
        shutil.copyfile(p.png, project / "sheets" / f"{p.label}.png")
        if sha256_file(project / "sheets" / f"{p.label}.png") != p.png_sha256:
            raise SmokeError(f"{p.png}: changed while being copied")
    plan = {
        "px_per_m": density,
        "shader": f"res://{SHADER_NAME if shader else 'plain.gdshader'}",
        "max_raw_bytes": 4 * MAX_TOTAL_BYTES,
        "sheets": [
            {"id": p.label, "path": f"res://sheets/{p.label}.png", "cell": p.cell, "columns": p.columns} for p in parts
        ],
        "renders": [
            {
                "name": s.name,
                "scale": s.scale,
                "width": s.width,
                "height": s.height,
                "quads": [{"sheet": q.part, "col": q.col, "row": q.row, "x": q.x, "y": q.y} for q in s.quads],
            }
            for s in specs
        ],
    }
    (project / "plan.json").write_text(json.dumps(plan, indent=1))
    return plan


# ---------------------------------------------------------------- supervised engine


def find_engine(godot: str | None = None) -> tuple[str | None, str]:
    """(godot path, "") or (None, reason). The renderer needs a private headless compositor: gnome-shell + dbus."""
    binary = godot or os.environ.get("GODOT") or shutil.which("godot")
    if not binary and (home := Path.home() / ".local/bin/godot").is_file():
        binary = str(home)
    if not binary or (not os.access(binary, os.X_OK) and not shutil.which(binary)):
        return None, "godot not found (PATH, $GODOT, --godot, ~/.local/bin/godot)"
    for tool in ("gnome-shell", "dbus-run-session", "bash"):
        if shutil.which(tool) is None:
            return None, f"{tool} not found (needed for the private headless compositor)"
    return binary, ""


def supervise(
    cmd, *, cwd, env, timeout, grace=5.0, head=1 << 16, tail=1 << 16, max_output=1 << 24, cancelled=None
) -> dict:
    """Run `cmd` in its own process group with a wall-clock deadline and bounded output (head + rolling tail; a flood
    above `max_output` bytes kills the group). Descendants are TERMed, then KILLed after `grace`, and reaped."""
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    state = {"total": 0, "head": b"", "tail": b"", "flood": False}

    def killpg(sig) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)

    def drain() -> None:
        while chunk := proc.stdout.read1(1 << 16):
            state["total"] += len(chunk)
            state["head"] += chunk[: max(0, head - len(state["head"]))]
            state["tail"] = (state["tail"] + chunk)[-tail:]
            if state["total"] > max_output and not state["flood"]:
                state["flood"] = True
                killpg(signal.SIGKILL)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = stopped = False
    while True:
        try:
            proc.wait(0.25)
            break
        except subprocess.TimeoutExpired:
            timed_out = time.monotonic() - t0 > timeout
            stopped = bool(cancelled and cancelled())
            if timed_out or stopped or state["flood"]:
                break
    # Whatever the exit reason, nothing this job started may outlive it: TERM the group, grace, KILL, reap.
    killpg(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    killpg(signal.SIGKILL)
    proc.wait()
    reader.join(5)
    text = lambda b: b.decode("utf-8", "replace")  # noqa: E731
    return {
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "cancelled": stopped,
        "output_flood": state["flood"],
        "seconds": round(time.monotonic() - t0, 3),
        "bytes_total": state["total"],
        "head": text(state["head"]),
        "tail": text(state["tail"]),
    }


def render_in_engine(godot: str, out: Path, timeout: float, cancelled=None) -> tuple[dict, str]:
    version = supervise(
        [godot, "--version"], cwd=out, env=dict(os.environ, HOME=os.environ.get("HOME", "")), timeout=30
    )
    priv = out / "xdg"
    priv.mkdir()
    env = {**os.environ, "SLR_PRIV": str(priv), "SLR_GODOT": godot, "SLR_PROJ": str(out / "project")}
    raw = out / "project" / "raw"
    cmd = ["bash", str(TOOL / "render.sh"), "--", "--plan", "res://plan.json", "--out", str(raw)]
    run = supervise(cmd, cwd=out, env=env, timeout=timeout, cancelled=cancelled)
    (out / "engine.log").write_text(
        f"# exit {run['returncode']} timed_out {run['timed_out']} flood {run['output_flood']} "
        f"bytes {run['bytes_total']}\n"
        f"--- head ---\n{run['head']}\n--- tail ---\n{run['tail']}\n"
    )
    return run, version["head"].strip().splitlines()[0] if version["head"].strip() else ""


# ---------------------------------------------------------------- render checks


def _hex(color: str) -> np.ndarray:
    return np.array([int(color[i : i + 2], 16) for i in (1, 3, 5)], np.int16)


def _up(a: np.ndarray, s: int) -> np.ndarray:
    return np.repeat(np.repeat(a, s, 0), s, 1)


def check_render(img: np.ndarray, spec: RenderSpec, parts: list[Sheet], bg: np.ndarray, border: bool, tol: int) -> dict:
    """Pixel facts of one composited render (H, W, 3): opaque source pixels equal their nearest-upscaled source
    colour, the rest is background (or, with the border shader, a 1-px keyline ring then a 1-px outer ring)."""
    s = spec.scale
    img = img.astype(np.int16)
    covered = np.zeros(img.shape[:2], bool)
    max_dev = bad_opaque = bad_bg = bad_cell = 0
    ring1: list[np.ndarray] = []
    ring2: list[np.ndarray] = []
    for q in spec.quads:
        c = parts[q.part].cell
        y0, x0 = q.y * s, q.x * s
        region = img[y0 : y0 + c * s, x0 : x0 + c * s]
        if region.shape[:2] != (c * s, c * s):
            bad_cell += 1
            continue
        covered[y0 : y0 + c * s, x0 : x0 + c * s] = True
        src = parts[q.part].cell_at(q.row, q.col)
        opaque, z1, z2 = zones(src)
        op, m1, m2 = _up(opaque, s), _up(z1, s), _up(z2, s)
        dev = np.abs(region - _up(src[..., :3].astype(np.int16), s)).max(-1)
        if op.any():
            max_dev = max(max_dev, int(dev[op].max()))
            bad_opaque += int((dev[op] > tol).sum())
        rest = ~op & ~m1 & ~m2 if border else ~op
        bad_bg += int((np.abs(region - bg).max(-1)[rest] > tol).sum())
        if border:
            ring1.append(region[m1])
            ring2.append(region[m2])
    outside = ~covered
    bad_bg += int((np.abs(img - bg).max(-1)[outside] > tol).sum())
    out = {
        "max_opaque_deviation": max_dev,
        "bad_opaque_pixels": bad_opaque,
        "bad_background_pixels": bad_bg,
        "bad_cells": bad_cell,
    }
    if border:
        meds, bad = [], 0
        for ring in (ring1, ring2):
            px = np.concatenate(ring) if ring else np.zeros((0, 3), np.int16)
            med = np.median(px, axis=0).astype(int).tolist() if len(px) else None
            bad += int((np.abs(px - med).max(-1) > tol).sum()) if len(px) else 0
            meds.append(med)

        def close(a, b) -> bool:
            return max(abs(x - y) for x, y in zip(a, b, strict=True)) <= tol

        # the two rings must be flat colours that differ from each other and from the background
        bad += int(all(meds) and close(*meds)) + sum(1 for m in meds if m and close(m, bg.tolist()))
        out |= {"bad_border_pixels": bad, "border_colors": {"keyline": meds[0], "outer": meds[1]}}
    return out


def composite(raw: Path, bg: np.ndarray, spec: RenderSpec) -> tuple[np.ndarray, int]:
    """(RGB over the background, count of semi-transparent raw pixels)."""
    with Image.open(raw) as im:
        rgba = np.asarray(im.convert("RGBA"))
    if rgba.shape[:2] != (spec.height * spec.scale, spec.width * spec.scale):
        raise SmokeError(
            f"{raw.name}: {rgba.shape[1]}x{rgba.shape[0]}, planned {spec.width * spec.scale}x{spec.height * spec.scale}"
        )
    a = rgba[..., 3]
    semi = int(((a > 0) & (a < 255)).sum())
    rgb = np.where((a >= 128)[..., None], rgba[..., :3], bg.astype(np.uint8))
    return rgb.astype(np.uint8), semi


# ---------------------------------------------------------------- driver


def _fresh_out(out: Path, profile: profiles.Profile) -> Path:
    out = Path(out).expanduser().absolute()
    protected = [REPO, profile.export.godot_project.expanduser().absolute()]
    if any(out.is_relative_to(p) or p.is_relative_to(out) for p in protected):
        raise SmokeError(f"{out}: the render project must live outside the repository and the game checkout")
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise SmokeError(f"{out} exists and is not an empty directory; smoke renders never overwrite")
    return out


def run_smoke(
    *,
    profile: Path,
    candidate: Path,
    siblings: dict[str, Path],
    out: Path,
    shader: Path | None = None,
    godot: str | None = None,
    timeout: float = 600.0,
    roots=None,
    background: str = BACKGROUND,
    cancelled=None,
) -> dict:
    """Render, check and write OUT/smoke.json. Returns the smoke.json document plus `exit` (0 / 1 / 3)."""
    prof = profiles.load_profile(profile)
    density = prof.canvas.density_px_per_m
    if not density:
        raise SmokeError(f"{profile}: canvas.density_px_per_m is required for the shared 62 px/m scale")
    if len(siblings) > MAX_SIBLINGS:
        raise SmokeError(f"at most {MAX_SIBLINGS} siblings, got {len(siblings)}")
    for label in siblings:
        if not LABEL.match(label):
            raise SmokeError(f"sibling id {label!r} must match {LABEL.pattern}")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", background):
        raise SmokeError(f"background {background!r} must be #rrggbb")
    cand = Path(candidate).expanduser()
    base = _confined(cand, roots)
    names = {p.name.removesuffix(".sonoranlab.json") for p in _sidecars(base)} if base.is_dir() else set()
    if len(names) != 1:
        raise SmokeError(f"{candidate}: expected exactly one *.sonoranlab.json export, found {sorted(names)}")
    label = names.pop()
    if label in siblings:
        raise SmokeError(f"sibling id {label!r} is the candidate")
    parts = [load_sheet(cand, label, prof, roots)] + [load_sheet(Path(p), k, prof, roots) for k, p in siblings.items()]
    try:
        spec = prof.character(label, cell=parts[0].cell)
    except profiles.ProfileError as exc:
        raise SmokeError(
            f"{label}: not usable with this profile ({exc.problems[0]}); add a reviewed characters: row"
        ) from exc
    shader_path = _confined(shader, roots) if shader else None
    if shader_path is not None and (
        missing := [u for u in REQUIRED_UNIFORMS if u not in uniforms(shader_path.read_text())]
    ):
        raise SmokeError(f"{shader}: shader lacks uniforms {missing}; it is not the standee shader")
    out = _fresh_out(out, prof)
    specs = plan_renders(parts)
    pixels = budget(specs)
    out.mkdir(parents=True, exist_ok=True)
    write_project(out, parts, specs, float(density), shader_path)
    bg, border, mirror = _hex(background), shader_path is not None, prof.directions.mirror_animations
    checks: dict[str, dict] = {
        "inputs_hashed": {"pass": True, "count": len(parts)},
        "frames_and_timing_match_manifest": compare_manifest(parts[0], spec),
        "mirrored_rows_exact": mirrored_rows_exact(parts[0], mirror),
        "cells_uncropped": {
            "pass": all(
                q.x >= 0 and q.y >= 0 and q.x + parts[q.part].cell <= s.width and q.y + parts[q.part].cell <= s.height
                for s in specs
                for q in s.quads
            ),
            "cells": {p.label: p.cell for p in parts},
            "note": "each quad is its sheet's own cell size; pixel truth is in nearest_filtering",
        },
    }
    for p in parts:
        for name, result in source_checks(p).items():
            checks.setdefault(name, {"pass": True, "per_sheet": {}})["per_sheet"][p.label] = result
    for name in ("alpha_binary", "no_baked_border"):
        checks[name]["pass"] = all(r["pass"] for r in checks[name]["per_sheet"].values())
    doc = {
        "schema": "sonoranlab.smoke/1",
        "tool": "sonoranlab smoke-render",
        "profile": {"path": str(prof.path), "id": prof.id, "sha256": sha256_file(prof.path)},
        "inputs": {"candidate": parts[0].describe(), "siblings": [p.describe() for p in parts[1:]]},
        "settings": {
            "px_per_m": float(density),
            "scales": list(SCALES),
            "filtering": "nearest",
            "background": background,
            "margin_gap_group_gap_1x_px": [MARGIN, GAP, GROUP_GAP],
            "baseline": "cell bottoms aligned; each character in its own uncropped square cell",
            "shader_sha256": sha256_file(shader_path) if shader_path else None,
            "uniforms_set": ["sheet", "grid", "cell", "cell_pixels"],
            "color_tolerance": COLOR_TOL,
            "timeout_s": timeout,
            "planned_renders": len(specs),
            "planned_pixels": pixels,
            "budget": {"max_total_bytes": MAX_TOTAL_BYTES, "max_total_pixels": MAX_TOTAL_PIXELS, "max_edge": MAX_EDGE},
        },
        "border": "rendered" if border else "not_rendered",
        "engine": "unavailable",
        "godot": None,
        "supervision": None,
        "renders": {},
        "checks": checks,
        "status": "unavailable",
        "agent_recommendation": "",
        "owner_style_acceptance": "",
    }
    binary, reason = find_engine(godot)
    if binary is None:
        doc["engine_detail"] = reason
        return _finish(out, doc, 3)
    run, version = render_in_engine(binary, out, timeout, cancelled)
    doc["godot"], doc["supervision"] = (
        version,
        {k: run[k] for k in ("returncode", "timed_out", "output_flood", "seconds", "bytes_total")},
    )
    doc["engine"] = "ran" if run["returncode"] == 0 and not run["timed_out"] and not run["output_flood"] else "failed"
    doc["engine_log"] = "engine.log"
    render_dir, raw_dir = out / "render", out / "project" / "raw"
    render_dir.mkdir()
    total_bytes, semi_raw, per_render, missing = 0, 0, {}, []
    for s in specs:
        raw = raw_dir / f"{s.name}.png"
        if not raw.is_file():
            missing.append(s.name)
            continue
        try:
            rgb, semi = composite(raw, bg, s)
        except (SmokeError, OSError, ValueError) as exc:
            missing.append(f"{s.name}: {exc}")
            continue
        path = render_dir / f"{s.name}.png"
        Image.fromarray(rgb, "RGB").save(path, "PNG")
        total_bytes += path.stat().st_size
        if total_bytes > MAX_TOTAL_BYTES:
            path.unlink()
            missing.append(f"{s.name}: byte budget {MAX_TOTAL_BYTES} exceeded")
            break
        semi_raw += semi
        res = check_render(rgb, s, parts, bg, border, COLOR_TOL)
        per_render[s.name] = res
        doc["renders"][s.name] = {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "size": [rgb.shape[1], rgb.shape[0]],
            "scale": s.scale,
            **s.meta,
        }
    shutil.rmtree(raw_dir, ignore_errors=True)
    checks["render_complete"] = {
        "pass": not missing and len(doc["renders"]) == len(specs),
        "missing": missing[:20],
        "rendered": len(doc["renders"]),
        "planned": len(specs),
        "total_bytes": total_bytes,
    }
    expected = {
        f"{r}_{c}_{s}x" for r in parts[0].rows for c in ("still", *(c["name"] for c in parts[0].clips)) for s in SCALES
    }
    expected |= {f"overview_{s}x" for s in SCALES}
    frames = {n: m["groups"] for n, m in doc["renders"].items() if m.get("kind") == "clip"}
    longest = {
        c["name"]: max(q["frames"] for p in parts for q in p.clips if q["name"] == c["name"]) for c in parts[0].clips
    }
    want = {f"{r}_{n}_{s}x": f for r in parts[0].rows for n, f in longest.items() for s in SCALES}
    checks["all_stills_and_clip_frames_rendered"] = {
        "pass": set(doc["renders"]) == expected and frames == want,
        "stills": sum(m.get("kind") == "still" for m in doc["renders"].values()),
        "expected_renders": len(expected),
    }
    devs = list(per_render.values())
    checks["nearest_filtering"] = {
        "pass": bool(devs) and all(d["bad_opaque_pixels"] == 0 and d["bad_cells"] == 0 for d in devs),
        "method": "every opaque source pixel compared with its nearest-upscaled block in each render",
        "max_deviation_observed": max((d["max_opaque_deviation"] for d in devs), default=None),
        "tolerance": COLOR_TOL,
        "bad_pixels": sum(d["bad_opaque_pixels"] for d in devs),
    }
    colors = [d["border_colors"] for d in devs if d.get("border_colors")]
    bad_border = sum(d.get("bad_border_pixels", 0) for d in devs)
    consistent = all(c == colors[0] for c in colors) if colors else True
    checks["alpha_and_border"] = {
        "pass": bool(devs)
        and semi_raw == 0
        and bad_border == 0
        and all(d["bad_background_pixels"] == 0 for d in devs)
        and consistent,
        "semi_transparent_render_pixels": semi_raw,
        "bad_background_pixels": sum(d["bad_background_pixels"] for d in devs),
        "bad_border_pixels": bad_border,
        "border_colors_observed": colors[0] if colors else None,
        "note": "alpha is binary in every render; border pixels exist only as the shader's two rings"
        if border
        else "no shader: the render must hold no pixel outside the opaque sprite and the background",
    }
    ok = doc["engine"] == "ran" and all(c["pass"] for c in checks.values())
    doc["status"] = "pass" if ok else "fail"
    return _finish(out, doc, 0 if ok else 1)


def _finish(out: Path, doc: dict, code: int) -> dict:
    tmp = out / ".smoke.json.tmp"
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, out / "smoke.json")
    return doc | {"exit": code, "smoke_json": str(out / "smoke.json")}


# ---------------------------------------------------------------- staging a game sheet for the registry


def stage_game_sheet(profile: Path, characters_json: Path, label: str, out: Path, roots=None) -> Path:
    """Wrap one sheet of the game's characters.json (a read-only staged copy) in a job-shaped directory that
    `import_job` accepts, so MCP `smoke_render` can name it as a sibling. The sidecar is the game's own entry, the
    manifest says plainly that SonoranLab did not make or validate the sheet (report.pass is false)."""
    prof = profiles.load_profile(profile)
    sheet = load_sheet(Path(characters_json).parent / f"{label}.png", label, prof, roots)
    if sheet.form != "game":
        raise SmokeError(f"{characters_json}: expected the game's characters.json beside {label}.png")
    cj = Path(characters_json)
    entry = json.loads(cj.read_text())["characters"][label]
    out = Path(out).expanduser().absolute()
    if out.exists():
        raise SmokeError(f"{out} exists; staging never overwrites")
    folder = out / "export" / label
    (out / "raw").mkdir(parents=True)
    folder.mkdir(parents=True)
    shutil.copyfile(sheet.png, folder / f"{label}.png")
    side = {
        "schema": 1,
        "tool": "sonoranlab.smoke stage",
        "character": label,
        "provenance": {"staged_game_sheet": True, "characters_json_sha256": sha256_file(cj)},
        "export": {"rows": sheet.rows, "cell": sheet.cell, "columns": sheet.columns},
        "sha256": {f"{label}.png": sheet.png_sha256},
        "characters_json_entry": entry,
    }
    (folder / f"{label}.sonoranlab.json").write_text(json.dumps(side, indent=2, sort_keys=True) + "\n")
    note = "staged copy of a game sheet; not generated or validated by SonoranLab"
    (out / "report.json").write_text(json.dumps({"pass": False, "staged_game_sheet": True, "note": note}))
    outputs = {f"export/{label}/{f.name}": sha256_file(f) for f in sorted(folder.iterdir())}
    manifest = {
        "schema": "sonoranlab.manifest/1",
        "character": label,
        "inputs": {},
        "outputs": outputs,
        "profile": {"path": str(prof.path), "sha256": sha256_file(prof.path)},
        "result": {"pass": False},
        "note": note,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m sonoranlab.smoke", description="stage a game sheet for the registry")
    ap.add_argument("stage", choices=["stage"])
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--characters-json", type=Path, required=True)
    ap.add_argument("--id", required=True, help="character id in characters.json; the sheet PNG sits beside it")
    ap.add_argument("--out", type=Path, required=True, help="new job-shaped directory (under a read root)")
    args = ap.parse_args(argv)
    try:
        print(stage_game_sheet(args.profile, args.characters_json, args.id, args.out))
    except (SmokeError, profiles.ProfileError) as exc:
        print(f"smoke stage: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
