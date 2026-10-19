"""Stage Track C/E model inputs at canvas = round16(cell * 3) (disposable Phase 1 tooling, ADR-0007 addendum).

Per character and generated direction, into OUT/<char>/:
  still_<dir>.png          the approved still, nearest x3, centred on a #ff00ff canvas (Track C image2, Track E start)
  pose_<dir>_<i>.png       Track D OpenPose frame i, resized from the x4 bake to x3, same canvas (Track C image1)
  still_<dir>_r1024.png, pose_<dir>_<i>_r1024.png   the same, centred on a 1024 canvas (Track C image1/image2)
The subject keeps 62 px/m x 3 in all of them, so poses and stills share one scale. Writes OUT/inputs.json with sha256.
Why r1024: Qwen Edit's encoder rescales every input image to ~1 MP. A canvas-sized (e.g. 560) reference is blown up
~1.83x in latent space and the subject comes out ~1.8x too large on a 560 output (2026-10-18, C/samurai/1/north:
legs cut off; 736 inputs gave ~1.39x). On a 1024 canvas the rescale is ~1:1, so the subject keeps its size.

    uv run --no-project --with pillow python -I benchmark/tools/prep_inputs.py CHAR_DIR BAKE_DIR OUT
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

CHARS = {"samurai": (184, 7), "oathguard": (176, 6), "relay_sentinel": (136, 0)}   # cell, run frames (0 = no run)
DIRS = ["north", "north-east", "east", "south-east", "south"]
KEY = (255, 0, 255)
SCALE = 3
REF = 1024   # Qwen Edit's reference-encoder resolution (~1 MP)


def canvas(cell: int) -> int:
    return -(-cell * SCALE // 16) * 16


def place(im: Image.Image, size: int, bg) -> Image.Image:
    out = Image.new("RGB", (size, size), bg)
    out.paste(im.convert("RGB"), ((size - im.width) // 2, (size - im.height) // 2))
    return out


def main(chars: Path, bake: Path, out: Path) -> None:
    man = {}
    for ch, (cell, frames) in CHARS.items():
        d = out / ch
        d.mkdir(parents=True, exist_ok=True)
        size = canvas(cell)
        for di in DIRS:
            still = Image.open(chars / ch / f"still_{di}.png").convert("RGBA")
            big = still.resize((cell * SCALE,) * 2, Image.NEAREST)
            bg = Image.new("RGBA", big.size, KEY + (255,))
            bg.alpha_composite(big)
            place(bg, size, KEY).save(d / f"still_{di}.png")
            place(bg, REF, KEY).save(d / f"still_{di}_r{REF}.png")
            for i in range(frames):
                pose = Image.open(bake / f"f{frames}" / f"g{cell * 4}" / "pose" / di / f"{i:03d}.png")
                small = pose.resize((cell * SCALE,) * 2, Image.LANCZOS)
                place(small, size, (0, 0, 0)).save(d / f"pose_{di}_{i}.png")
                place(small, REF, (0, 0, 0)).save(d / f"pose_{di}_{i}_r{REF}.png")
        man[ch] = {"cell": cell, "canvas": size, "frames": frames,
                   "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.png"))}}
    (out / "inputs.json").write_text(json.dumps(man, indent=1) + "\n")
    print({k: (v["canvas"], len(v["files"])) for k, v in man.items()})


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
