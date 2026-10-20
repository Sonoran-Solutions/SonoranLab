"""Godot 4 export (docs/09): the game-layout sheet PNG, a text SpriteFrames `.tres` and a provenance sidecar.

Pure function of its inputs: same arrays and parameters give byte-identical files (no timestamps, no random UIDs).
Writes only under `out_dir/<character>/` and refuses to if that is inside the shipped game (`forbid_under`, one or more
protected directories, all checked, never replaced).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_MAX_PALETTE = 255  # index 0 is the transparent entry, as in the game's own sheets


def _check_inputs(sheet: np.ndarray, character: str, cell: tuple[int, int], rows: Sequence[str], clips: Sequence[dict]):
    if not _NAME.match(character):
        raise ValueError(f"character {character!r} must match {_NAME.pattern} (it names files and the directory)")
    cw, ch = cell
    if cw != ch or cw < 1:
        raise ValueError(f"cell {cell} must be square (the game's characters.json stores one int per character)")
    if sheet.ndim != 3 or sheet.shape[2] != 4 or sheet.dtype != np.uint8:
        raise ValueError(f"sheet must be (H, W, 4) uint8 RGBA, got {sheet.shape} {sheet.dtype}")
    if len(rows) != len(set(rows)):
        raise ValueError(f"duplicate direction in rows: {list(rows)}")
    if sheet.shape[0] != len(rows) * ch or sheet.shape[1] % cw:
        raise ValueError(f"sheet {sheet.shape[1]}x{sheet.shape[0]} is not {len(rows)} rows of {cw}x{ch} cells")
    columns = sheet.shape[1] // cw
    seen = set()
    for c in clips:
        name, start, n, fps = c["name"], c["start"], c["frames"], c["fps"]
        if name in seen or not _NAME.match(str(name)):
            raise ValueError(f"clip name {name!r} is duplicated or not a plain identifier")
        seen.add(name)
        if not (isinstance(start, int) and isinstance(n, int) and start >= 0 and n >= 1 and start + n <= columns):
            raise ValueError(f"clip {name}: columns {start}..{start + n - 1} do not fit the {columns}-column sheet")
        if not fps > 0:
            raise ValueError(f"clip {name}: fps must be > 0, got {fps}")
        lf = c.get("loop_from")
        if lf is not None and not (0 <= lf < n):
            raise ValueError(f"clip {name}: loop_from {lf} outside 0..{n - 1}")
    return columns


def animation_specs(
    rows: Sequence[str], cell: tuple[int, int], clips: Sequence[dict], anim_name: str = "{animation}_{direction}"
) -> dict[str, dict]:
    """`{animation name: {"frames": [[x, y, w, h], ...], "speed": fps, "loop": bool}}` in export order.

    A non-looping clip with `loop_from` is loop=false here (Godot's SpriteFrames cannot loop from the middle).
    """
    cw, ch = cell
    out: dict[str, dict] = {}
    for c in clips:
        for r, direction in enumerate(rows):
            name = anim_name.format(animation=c["name"], direction=direction)
            if name in out:
                raise ValueError(f"anim_name {anim_name!r} gives {name!r} twice")
            out[name] = {
                "frames": [[(c["start"] + i) * cw, r * ch, cw, ch] for i in range(c["frames"])],
                "speed": float(c["fps"]),
                "loop": bool(c["loop"]),
            }
    return out


def _png_bytes(sheet: np.ndarray) -> tuple[bytes, str, int]:
    """Lossless PNG bytes, mode, opaque colour count. Palette-indexed like the game's sheets when it fits."""
    sheet = sheet.copy()
    sheet[sheet[..., 3] == 0] = 0  # invisible pixels carry no colour: stable bytes, nothing to bleed
    opaque = sheet[sheet[..., 3] > 0]
    colours = np.unique(opaque[:, :3], axis=0)
    buf = io.BytesIO()
    if (opaque[:, 3] == 255).all() and len(colours) <= _MAX_PALETTE:
        # index 0 = transparent, then the colours in sorted RGB order
        idx = np.searchsorted(
            (colours.astype(np.uint32) @ np.array([65536, 256, 1], np.uint32)),
            sheet[..., :3].astype(np.uint32) @ np.array([65536, 256, 1], np.uint32),
        )
        pix = np.where(sheet[..., 3] == 0, 0, idx + 1).astype(np.uint8)
        im = Image.fromarray(pix, "P")
        im.putpalette(np.vstack([[0, 0, 0], colours]).astype(np.uint8).tobytes())
        im.save(buf, "PNG", optimize=True, transparency=bytes([0]))
        mode = "P"
    else:
        Image.fromarray(sheet, "RGBA").save(buf, "PNG", optimize=True)
        mode = "RGBA"
    return buf.getvalue(), mode, len(colours)


def _tres(character: str, specs: dict[str, dict]) -> str:
    atlas, anims = [], []
    for name, spec in specs.items():
        frames = []
        for x, y, w, h in spec["frames"]:
            sid = f"AtlasTexture_{len(atlas)}"
            atlas.append(f'[sub_resource type="AtlasTexture" id="{sid}"]\natlas = ExtResource("1_sheet")\n')
            atlas[-1] += f"region = Rect2({x}, {y}, {w}, {h})\n"
            frames.append(f'{{\n"duration": 1.0,\n"texture": SubResource("{sid}")\n}}')
        anims.append(
            f'{{\n"frames": [{", ".join(frames)}],\n"loop": {"true" if spec["loop"] else "false"},\n'
            f'"name": &"{name}",\n"speed": {spec["speed"]!r}\n}}'
        )
    head = '[gd_resource type="SpriteFrames" format=3]\n\n'
    # relative path: Godot resolves it against the .tres, so the folder can be copied into any project
    ext = f'[ext_resource type="Texture2D" path="{character}.png" id="1_sheet"]\n\n'
    return head + ext + "\n".join(atlas) + "\n[resource]\nanimations = [" + ", ".join(anims) + "]\n"


def export_godot(
    sheet: np.ndarray,
    *,
    character: str,
    cell: tuple[int, int],
    rows: Sequence[str],
    clips: Sequence[dict],
    out_dir: Path,
    provenance: dict,
    forbid_under: Path | Sequence[Path] | None = None,
    anim_name: str = "{animation}_{direction}",
) -> dict[str, str]:
    """Write `out_dir/<character>/{<character>.png, <character>_frames.tres, <character>.sonoranlab.json}`.

    `sheet` is already in the game layout: one row per `rows` entry (mirrored west rows as real pixels), column 0
    the still or empty, clip frames from each clip's `start`. Returns `{"png", "tres", "sidecar"}` -> path.
    ValueError for bad inputs, a target inside any `forbid_under` directory, or a target directory that is not empty.
    """
    columns = _check_inputs(sheet, character, cell, rows, clips)
    target = (Path(out_dir) / character).resolve()
    if forbid_under is None:
        protected: Sequence[Path] = ()
    elif isinstance(forbid_under, (str, Path)):
        protected = (forbid_under,)
    else:
        protected = forbid_under
    for protect in protected:
        game = Path(protect).expanduser().resolve()
        if target == game or game in target.parents:
            raise ValueError(f"refusing to write inside the shipped game {game}: {target}")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"{target} exists and is not an empty directory; exports never overwrite")
    specs = animation_specs(rows, cell, clips, anim_name)
    png, mode, colours = _png_bytes(sheet)
    tres = _tres(character, specs).encode()
    entry_clips = [
        {k: c[k] for k in ("name", "start", "frames", "fps", "loop")}
        | ({"loop_from": c["loop_from"]} if c.get("loop_from") is not None else {})
        for c in clips
    ]
    sidecar = {
        "schema": 1,
        "tool": "sonoranlab export godot",
        "character": character,
        "provenance": provenance,
        "export": {
            "cell": list(cell),
            "rows": list(rows),
            "columns": columns,
            "sheet_size": [sheet.shape[1], sheet.shape[0]],
            "png_mode": mode,
            "anim_name": anim_name,
            "animations": len(specs),
            "loop_from": {c["name"]: c["loop_from"] for c in clips if c.get("loop_from") is not None},
        },
        "sha256": {
            f"{character}.png": hashlib.sha256(png).hexdigest(),
            f"{character}_frames.tres": hashlib.sha256(tres).hexdigest(),
        },
        "characters_json_entry": {
            "sheet": f"{character}.png",
            "cell": cell[0],
            "columns": columns,
            "clips": entry_clips,
            "colours": colours,
        },
        "notes": [
            "standing_pixels and height_m are omitted: the game owns them.",
            "SpriteFrames cannot loop from the middle: a non-looping clip with loop_from exports loop=false; "
            "export.loop_from records it for the game's own player.",
        ],
    }
    target.mkdir(parents=True, exist_ok=True)
    paths = {"png": target / f"{character}.png", "tres": target / f"{character}_frames.tres"}
    paths["sidecar"] = target / f"{character}.sonoranlab.json"
    paths["png"].write_bytes(png)
    paths["tres"].write_bytes(tres)
    paths["sidecar"].write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    return {k: str(v) for k, v in paths.items()}
