# 09 — Godot export

Target: Godot 4.7 (verified with 4.7.2.stable). Phase 2 exports **both** what the game consumes today (one sheet PNG
per character plus a `characters.json` entry) and a text `SpriteFrames` for drag-in testing. Code:
`sonoranlab/export/godot.py` (`export_godot`), CLI `sonoranlab export godot`. Everything below marked *verified* was
checked against Godot 4.7.2 and the private game checkout (read-only) on 2026-10-19.

## Outputs

Export writes only under the job dir (`<job>/export/<character>/`), never into the game; it refuses any `--out` that is
inside the profile's `export.godot_project` (`forbid_under`), and never overwrites (the target must not hold files).
Copying into the game is a separate, human-reviewed step.

```
<character>/
  <character>.png              sheet in the game layout (see below)
  <character>_frames.tres      SpriteFrames, one animation per (animation, direction)
  <character>.sonoranlab.json  provenance + sha256 of the PNG and .tres + a characters.json entry fragment
```

Deterministic: same inputs give byte-identical files (no timestamps, no UIDs, Pillow's PNG encoder at a fixed setting).

## Sheet (the game's convention, verified in `assets/pixel/v0/characters/`)

- One PNG per character; rows = the profile's 8 directions in profile order (`north, north-west, west, south-west,
  south, south-east, east, north-east`), the west rows exported as real mirrored pixels; square cells, no spacing or
  margin; column 0 = the still (may be empty), clip frames from each clip's `start`.
- PNG mode. The game's sheets are 8-bit **palette-indexed, non-interlaced, index 0 = transparent** (`PLTE` + `tRNS`, plus
  an `sRGB` chunk). The export matches that when the sheet has at most 255 opaque colours and alpha only in {0, 255};
  otherwise it writes RGBA and says so (`export.png_mode` in the sidecar). No `sRGB`/`gAMA`/`iCCP`/`tIME`/text chunks:
  Godot ignores them for imported textures and they only make bytes unstable. Fully transparent pixels are written as
  (0, 0, 0, 0) in both modes.
- The sidecar's `characters_json_entry` is `{sheet, cell, columns, clips, colours}` in the game's schema (`colours` =
  distinct opaque colours in the sheet). `standing_pixels` and `height_m` are the game's to set and are omitted.

## SpriteFrames contract (*verified* by `tools/godot/spriteframes_check`)

- Animation names: `{animation}_{direction}` (`anim_name` parameter), e.g. `run_south-east`, for **every** row including
  the mirrored west rows, so the engine never needs `flip_h`.
- `speed` = the clip's fps, `loop` = the clip's `loop`. Godot's SpriteFrames cannot loop from the middle, so a
  non-looping clip with `loop_from` (e.g. the game's `channel`) exports `loop = false`; the sidecar records `loop_from`.
- Each frame is an `AtlasTexture` over the sheet with `region` = its cell, `duration` 1.0, in clip frame order. No
  per-frame textures. One `AtlasTexture` per (animation, direction, frame).
- Header is `[gd_resource type="SpriteFrames" format=3]`, as Godot 4.7.2 itself writes it (no `load_steps`, which it
  drops, and no `uid`: a resource with no `uid=` loads fine; the engine falls back to the path).
- The sheet is referenced by a **relative** `ext_resource` path (`path="<character>.png"`): Godot resolves it against the
  `.tres`, so the folder can be copied to any place in any project. (After the first save in the editor Godot rewrites it
  to `res://...`; that is expected.)

## Import settings (*verified*)

- **No `.png.import` is written.** On first import Godot 4.7.2 generates one itself, and its `[params]` block is
  byte-identical to the one in the game's own `characters/*.png.import`: `compress/mode=0` (lossless),
  `mipmaps/generate=false`, `process/fix_alpha_border=true`, `detect_3d/compress_to=1`. That already is the pixel-art
  setting we need, so a hand-written file would add nothing and could only go stale. The earlier draft of this doc asked
  for `fix_alpha_border=false`; with nearest sampling and zeroed transparent pixels it has no visible effect, so the
  default stays. Caveat for 3D use: `detect_3d/compress_to=1` switches a texture to VRAM-compressed if Godot sees it
  used in a 3D material on import; the game's sheets are sampled by a spatial shader and stayed lossless, but a
  consuming project that notices blur or banding should check that setting.
- **Texture filter belongs to the consuming project.** The game's `project.godot` sets no default texture filter (its
  `PixelStandee.gdshader` declares `filter_nearest` on the sheet sampler instead). A `SpriteFrames` / `AnimatedSprite2D`
  in a project without `rendering/textures/canvas_textures/default_texture_filter=0` (Nearest) is drawn with linear
  filtering and blurs when scaled. The fixture project sets it; the export does not touch project settings.

## Verifying an export in the engine

`tools/godot/spriteframes_check/` is a tiny disposable Godot 4 project plus two scripts. Copy it, export into the copy,
import, check:

```
cp -r tools/godot/spriteframes_check /tmp/p && sonoranlab export godot sheet.png --character toy --cell 16 \
  --rows north,north-west,west,south-west,south,south-east,east,north-east \
  --clip run:1:4:10:loop --clip cast:5:3:6:once:1 --out /tmp/p
godot --headless --path /tmp/p --import                       # must print no ERROR
godot --headless --path /tmp/p --fixed-fps 60 --script res://check_spriteframes.gd -- \
  res://toy/toy_frames.tres res://expected.json               # JSON result, exit 0 = pass
```

`check_spriteframes.gd` loads the `.tres`, asserts every expected animation (name, frame count, speed, loop, each
frame's `AtlasTexture` region and the sha256 of the pixels in that region read from the PNG), then plays each one in an
`AnimatedSprite2D` for real main-loop iterations at a fixed 1/60 s step and checks the frame order, the advance rate
(frame changes in 2 s vs fps, +-1), looping, and that a non-looping clip holds its last frame and emits
`animation_finished` once. `expected.json` is built independently of the exporter (the pytest does it by hand).
`tests/sonoranlab/test_export_godot.py` runs all of this when `godot` is on PATH and skips otherwise.

`render.sh <imported project copy> -- --tres res://... --anim NAME --out DIR [--frames N]` renders one animation at
1x and 4x (nearest) into a private headless GNOME Shell compositor with llvmpipe (same approach as `bake.sh`; no window
on the desktop) and saves a PNG per frame. On the synthetic fixture both scales were pixel-exact against the source
cells (no blended colours at 4x).

## Smoke render (Phase 5)

A later step: load the new SpriteFrames and one existing character side by side at 1x and 4x, play one cycle, and
look at the screenshot. `render_animation.gd` is the starting point. Pixel-level problems are already gone by then;
this is for "does the new class look like it belongs next to the old ones."
