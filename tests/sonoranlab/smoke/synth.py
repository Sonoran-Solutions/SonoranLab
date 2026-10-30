"""Synthetic fixtures for the smoke render tests: asymmetric 8-row sheets, a toy profile and a fake engine.
Nothing here is game art; the shader stand-in is written for the tests and is not the game's shader."""

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

from sonoranlab import smoke
from sonoranlab.export.godot import export_godot

ORDER = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"]
MIRROR = {"north-west": "north-east", "west": "east", "south-west": "south-east"}
PALETTE = [(17, 13, 12), (204, 34, 34), (230, 180, 140), (40, 60, 200)]
LIMITS = {"max_foot_slide_px": 3, "max_bbox_drift_px": 2, "opaque_area_cv_warn": 0.2, "loop_closure_ratio_warn": 1.5}
CLIPS = [
    {"name": "idle", "start": 1, "frames": 2, "fps": 5, "loop": True},
    {"name": "run", "start": 3, "frames": 3, "fps": 10, "loop": True},
]
CELLS = {"hero": 24, "ally": 32, "boss": 40, "kin": 16, "extra": 24}
STAND_SHADER = """shader_type spatial;
render_mode unshaded, cull_disabled;
uniform sampler2D sheet : source_color, filter_nearest, repeat_disable;
uniform vec2 grid = vec2(25.0, 8.0);
uniform vec2 cell = vec2(0.0);
uniform float cell_pixels = 96.0;
uniform vec4 border_color : source_color = vec4(1.0);
void fragment() { ALBEDO = vec3(0.0); }
"""


def toy_profile(root: Path, density=62) -> Path:
    pal = root / "palette.png"
    Image.fromarray(np.array([PALETTE], np.uint8)).save(pal)
    (root / "game").mkdir(exist_ok=True)
    doc = {
        "id": "toy",
        "name": "Toy",
        "style": "pixel",
        "canvas": {"cell": "per_character", "gen_scale": 2, "padding": 2, "sheet_layout": "rows_by_direction"}
        | ({"density_px_per_m": density} if density else {}),
        "palette": {"source": "palette.png", "sha256": hashlib.sha256(pal.read_bytes()).hexdigest(), "colours": 4}
        | {"max_colors_per_cell": 4, "dither": "none"},
        "anchor": {"mode": "foot", "foot_y": "cell_minus_4", "anchor_x": "cell_half", "registration": "per_clip"},
        "alpha": {"threshold": 128, "bg_key": "#ff00ff", "fringe_passes": 1, "fringe_tolerance": 0},
        "directions": {"order": ORDER, "generate": ["north", "north-east", "east", "south-east", "south"]}
        | {"mirror_animations": MIRROR},
        "animations": {
            "idle": {"frames": 2, "fps": 5, "loop": True},
            "run": {"frames": 3, "fps": 10, "loop": True},
        },
        "validator": {"label": "toy defaults", **LIMITS},
        "characters": {name: {"cell": cell, "anchor_mode": "foot", **LIMITS} for name, cell in CELLS.items()},
        "export": {"godot_project": "game", "sprites_dir": "res://sprites", "manifest": "characters.json"},
        "autonomy": {"allow_auto_approve": False, "max_seeds_per_attempt": 4, "max_attempts_per_animation": 3}
        | {"escalate_after": "ask"},
    }
    (root / "toy.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return root / "toy.yaml"


def cell_art(size: int, row: int, col: int, tint: int) -> np.ndarray:
    """Asymmetric figure: body, a belt whose length depends on the row, a hand that moves with the column."""
    f = np.zeros((size, size, 4), np.uint8)
    top, bottom, left = 4, size - 5, size // 2 - 4
    f[top : bottom + 1, left : left + 8] = (*PALETTE[0], 255)
    f[top + 1 : bottom, left + 1 : left + 7] = (*PALETTE[2 + tint % 2], 255)
    f[top + 3, left + 1 : left + 2 + row % 5] = (*PALETTE[1], 255)
    f[top + 5 + col % 4, left + 8] = (*PALETTE[3], 255)
    return f


def make_sheet(label: str, clips=CLIPS, columns=None, tint=0) -> np.ndarray:
    size = CELLS[label]
    columns = columns or 1 + sum(c["frames"] for c in clips)
    sheet = np.zeros((8 * size, columns * size, 4), np.uint8)
    for r in range(8):
        for c in range(columns):
            sheet[r * size : (r + 1) * size, c * size : (c + 1) * size] = cell_art(size, r, c, tint)
    for dst, src in MIRROR.items():  # clip frames only; stills stay independent
        d, s = ORDER.index(dst), ORDER.index(src)
        for c in range(1, columns):
            src = sheet[s * size : (s + 1) * size, c * size : (c + 1) * size]
            sheet[d * size : (d + 1) * size, c * size : (c + 1) * size] = src[:, ::-1]
    return sheet


def export_job(root: Path, label: str, clips=CLIPS, sheet=None) -> Path:
    """A real `sonoranlab` export folder (png, SpriteFrames, sidecar) under root/<label>-job/."""
    job = root / f"{label}-job"
    if sheet is None and job.exists():
        return job  # same fixture twice in one test
    sheet = make_sheet(label, clips) if sheet is None else sheet
    export_godot(
        sheet,
        character=label,
        cell=(CELLS[label], CELLS[label]),
        rows=ORDER,
        clips=clips,
        out_dir=job / "export",
        provenance={},
    )
    return job


def game_sheet(root: Path, label: str, clips=CLIPS) -> Path:
    """A staged game sheet: PNG plus a characters.json entry (the game's layout)."""
    sheet = make_sheet(label, clips, tint=1)
    d = root / f"staged-{label}"
    d.mkdir()
    Image.fromarray(sheet).save(d / f"{label}.png")
    entry = {"sheet": f"{label}.png", "cell": CELLS[label], "columns": sheet.shape[1] // CELLS[label], "clips": clips}
    (d / "characters.json").write_text(
        json.dumps(
            {"density_px_per_m": 62, "rows": ORDER, "mirrored_animation_rows": MIRROR, "characters": {label: entry}}
        )
    )
    return d / f"{label}.png"


def _rings(opaque: np.ndarray):
    """Independent (brute force) keyline/outer ring masks of the 3x3 / 5x5 neighbourhoods inside one cell."""
    _h, _w = opaque.shape

    def near(r):
        out = np.zeros_like(opaque)
        for y, x in zip(*np.nonzero(opaque), strict=True):
            out[max(0, y - r) : y + r + 1, max(0, x - r) : x + r + 1] = True
        return out

    return near(1) & ~opaque, near(2) & ~near(1)


def fake_supervise(mode="ok", border_colors=((14, 11, 10), (255, 255, 255)), calls=None):
    """A stand-in for smoke.supervise that draws what the Godot script would (nearest, optional border)."""

    def fake(cmd, *, cwd, env, timeout, **kw):
        if calls is not None:
            calls.append((cmd, env))
        base = {"returncode": 0, "timed_out": False, "cancelled": False, "output_flood": False, "seconds": 0.1}
        if "--version" in cmd:
            return base | {"bytes_total": 12, "head": "4.7.2.stable.fake\n", "tail": ""}
        proj = Path(env["SLR_PROJ"])
        raw = Path(cmd[cmd.index("--out") + 1])
        plan = json.loads((proj / "plan.json").read_text())
        border = plan["shader"].endswith("PixelStandee.gdshader")
        sheets = [
            np.asarray(Image.open(proj / s["path"].removeprefix("res://")).convert("RGBA")) for s in plan["sheets"]
        ]
        raw.mkdir(parents=True, exist_ok=True)
        if mode == "fail":
            return base | {"returncode": 1, "bytes_total": 5, "head": "ERROR: boom\n", "tail": ""}
        if mode == "timeout":
            return base | {"returncode": -9, "timed_out": True, "bytes_total": 0, "head": "", "tail": ""}
        for r in plan["renders"]:
            s = r["scale"]
            img = np.zeros((r["height"] * s, r["width"] * s, 4), np.uint8)
            for q in r["quads"]:
                sh = plan["sheets"][q["sheet"]]
                c = sh["cell"]
                src = sheets[q["sheet"]][q["row"] * c : (q["row"] + 1) * c, q["col"] * c : (q["col"] + 1) * c]
                if mode == "blur":
                    src = src.copy()
                    src[:, 1:, :3] = (src[:, 1:, :3].astype(int) + src[:, :-1, :3]) // 2
                up = np.repeat(np.repeat(src, s, 0), s, 1)
                region = np.zeros_like(up)
                if border:
                    k, o = _rings(src[..., 3] >= 128)
                    region[np.repeat(np.repeat(k, s, 0), s, 1)] = (*border_colors[0], 255)
                    region[np.repeat(np.repeat(o, s, 0), s, 1)] = (*border_colors[1], 255)
                opaque = up[..., 3] >= 128
                region[opaque] = up[opaque]
                if mode == "dark":  # the engine's colour path: dark channels come out a few codes darker, consistently
                    px = region[opaque].astype(int)
                    region[opaque, :3] = np.where(px[:, :3] < 48, np.maximum(px[:, :3] - 5, 0), px[:, :3])
                if mode == "semi":
                    region[opaque & (np.arange(region.shape[1]) % 7 == 0)[None, :]] = (1, 2, 3, 128)
                img[q["y"] * s : (q["y"] + c) * s, q["x"] * s : (q["x"] + c) * s] = region
            Image.fromarray(img, "RGBA").save(raw / f"{r['name']}.png")
        return base | {"bytes_total": 80, "head": json.dumps({"status": "ok"}) + "\n", "tail": ""}

    return fake


def patch_engine(monkeypatch, mode="ok", calls=None, **kw):
    monkeypatch.setattr(smoke, "find_engine", lambda godot=None: ("/fake/godot", ""))
    monkeypatch.setattr(smoke, "supervise", fake_supervise(mode, calls=calls, **kw))


def names(doc) -> set[str]:
    return set(doc["renders"])


SHADER_UNIFORMS = re.compile(r"uniform\s+\w+\s+(\w+)")
