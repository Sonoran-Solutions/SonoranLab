"""Phase 1 reference extraction (disposable benchmark tooling, not the Phase 2 package).

Reads the approved Twos Are Up sheets from the private game checkout and writes, outside this public repo:
  <out>/<char>/still_<dir>.png        column-0 still per direction, native cell size
  <out>/<char>/still_<dir>_x4.png     4x nearest upscale on the generation background key (model input)
  <out>/<char>/gt_<clip>_<dir>_<i>.png  ground-truth clip frames for the generated directions
  <out>/manifest.json                 every file's sha256 + the measurements the frozen profile relies on

    uv run --no-project --with pillow python benchmark/tools/extract_refs.py <game_repo> <out_dir>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

CHARS = {"samurai": "run", "oathguard": "run", "relay_sentinel": "idle"}  # #3 has no run: hover idle is its loop
GEN_DIRS = ["north", "north-east", "east", "south-east", "south"]  # west side mirrors east in the game
SCALE = 4
BG_KEY = (255, 0, 255)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(game: Path, out: Path) -> None:
    root = game / "assets/pixel/v0/characters"
    meta = json.loads((root / "characters.json").read_text())
    rows = meta["rows"]
    palette = Image.open(root / "palette.png").convert("RGB")
    pal = set(palette.getdata())
    assert BG_KEY not in pal, "background key collides with the shipped palette"
    man = {"game_repo": str(game), "palette_sha256": sha(root / "palette.png"), "palette_colours": len(pal),
           "bg_key": "#%02x%02x%02x" % BG_KEY, "scale": SCALE, "gen_dirs": GEN_DIRS, "characters": {}}
    for name, clip_name in CHARS.items():
        c = meta["characters"][name]
        sheet_path = root / c["sheet"]
        sheet = Image.open(sheet_path).convert("RGBA")
        cell = c["cell"]
        assert sheet.size == (c["columns"] * cell, len(rows) * cell), (name, sheet.size)
        clip = next(k for k in c["clips"] if k["name"] == clip_name)
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        files, feet, semi, offpal = {}, [], 0, 0
        for r, direction in enumerate(rows):
            for col in [0] + ([*range(clip["start"], clip["start"] + clip["frames"])] if direction in GEN_DIRS else []):
                im = sheet.crop((col * cell, r * cell, (col + 1) * cell, (r + 1) * cell))
                a = im.getchannel("A")
                semi += sum(1 for v in a.getdata() if 0 < v < 255)
                offpal += sum(1 for px in im.getdata() if px[3] and px[:3] not in pal)
                bbox = a.getbbox()
                if col == 0:
                    feet.append(bbox[3] - 1)
                    p = d / f"still_{direction}.png"
                    im.save(p)
                    files[p.name] = sha(p)
                    up = Image.new("RGBA", (cell * SCALE, cell * SCALE), BG_KEY + (255,))
                    big = im.resize((cell * SCALE, cell * SCALE), Image.NEAREST)
                    up.alpha_composite(big)
                    p = d / f"still_{direction}_x{SCALE}.png"
                    up.convert("RGB").save(p)
                    files[p.name] = sha(p)
                else:
                    p = d / f"gt_{clip_name}_{direction}_{col - clip['start']}.png"
                    im.save(p)
                    files[p.name] = sha(p)
        man["characters"][name] = {
            "sheet": c["sheet"], "sheet_sha256": sha(sheet_path), "cell": cell, "height_m": c["height_m"],
            "standing_pixels": c["standing_pixels"], "benchmark_clip": clip, "gen_canvas": cell * SCALE,
            "still_lowest_opaque_rows": sorted(set(feet)), "semi_transparent_px": semi, "off_palette_px": offpal,
            "files": files}
    (out / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    print(json.dumps({k: {x: v[x] for x in ("cell", "gen_canvas", "still_lowest_opaque_rows", "semi_transparent_px",
                                            "off_palette_px")} for k, v in man["characters"].items()}, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
