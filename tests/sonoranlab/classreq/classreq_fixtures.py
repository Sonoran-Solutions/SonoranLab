"""Synthetic class-request fixtures: the toy profile (synthetic 4-colour palette, never the game's) with Phase 5
clips, an asymmetric eight-direction still set and HD turnarounds drawn with numpy/Pillow."""

import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # test_pipeline (toy profile)

from test_pipeline import LIMITS, ORDER, PALETTE

CELL = 120
DARK, RED, SKIN, BLUE = PALETTE
KEY = (255, 0, 255)


def edit(doc):
    doc["canvas"]["density_px_per_m"] = 62
    doc["animations"] = {
        "idle": {"frames": 2, "fps": 5, "loop": True, "track_default": "E"},
        "run": {"frames": 2, "fps": 10, "loop": True, "track_default": "E"},
        "attack": {"frames": 2, "fps": 12, "loop": False, "track_default": "C"},
    }
    doc["characters"] = {"hero": {"cell": CELL, "anchor_mode": "foot", **LIMITS}}
    doc["references"] = {"style_refs": ["samurai", "relay_sentinel"]}


def figure(d: str, height: int = 40, width: int = 16) -> np.ndarray:
    """Small RGBA figure, lowest row = last row: outlined body, a belt whose length and a blue mark whose column
    depend on the direction (so no still is a mirror of another)."""
    k = ORDER.index(d)
    f = np.zeros((height, width, 4), np.uint8)
    f[:, :] = (*DARK, 255)
    f[1:-1, 1:-1] = (*SKIN, 255)
    f[height // 3, 1 : 2 + k] = (*RED, 255)
    f[height // 2, 1 + k % 6 : 3 + k % 6] = (*BLUE, 255)
    return f


def still(d: str, cell: int = CELL) -> np.ndarray:
    f = figure(d)
    out = np.zeros((cell, cell, 4), np.uint8)
    h, w = f.shape[:2]
    x0, y0 = cell // 2 - w // 2, cell - 4 - h + 1
    out[y0 : y0 + h, x0 : x0 + w] = f
    return out


def write_stills(root: Path, frames: dict | None = None, skip=()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for d in ORDER:
        if d not in skip:
            Image.fromarray((frames or {}).get(d, still(d)), "RGBA").save(root / f"still_{d}.png")
    return root


def request(**changes) -> dict:
    doc = {
        "schema": "sonoranlab.class_request/1",
        "project": "toy",
        "character": "hero",
        "display_name": "Hero",
        "status": "draft",
        "height_m": 0.7,
        "visual_role": "test figure",
        "silhouette": "a box",
        "invariants": ["belt on the left"],
        "palette_notes": "toy palette",
        "clips": ["run", "idle"],
        "source_design": "synthetic",
        "gameplay": "none",
        "references": {"provenance": ["synthetic fixture"], "approved_by": None},
        "owner_decisions": ["O4 pending"],
    }
    doc.update(changes)
    return {k: v for k, v in doc.items() if v is not ...}


def write_request(path: Path, **changes) -> Path:
    path.write_text(yaml.safe_dump(request(**changes), sort_keys=False))
    return path


def hd_view(d: str, size: int = 256, scale: int = 4, noise: bool = False, seed: int = 0) -> np.ndarray:
    """One HD turnaround cell: the direction's figure at `scale`x nearest on magenta (optionally noisy)."""
    f = figure(d, height=43)
    big = f.repeat(scale, 0).repeat(scale, 1)
    out = np.empty((size, size, 3), np.uint8)
    out[:] = KEY
    if noise:
        rng = np.random.default_rng(seed)
        out = np.clip(out.astype(int) + rng.integers(-12, 13, out.shape), 0, 255).astype(np.uint8)
    h, w = big.shape[:2]
    y0, x0 = size - 20 - h, (size - w) // 2 + ORDER.index(d)
    region = out[y0 : y0 + h, x0 : x0 + w]
    region[big[..., 3] > 0] = big[big[..., 3] > 0, :3]
    return out


def turnaround(path: Path, blank=(), noise: bool = False) -> Path:
    rows = []
    for r in range(2):
        cells = []
        for c in range(4):
            d = ORDER[r * 4 + c]
            cells.append(
                np.full((256, 256, 3), KEY, np.uint8) if d in blank else hd_view(d, noise=noise, seed=r * 4 + c)
            )
        rows.append(np.concatenate(cells, 1))
    Image.fromarray(np.concatenate(rows, 0), "RGB").save(path)
    return path
