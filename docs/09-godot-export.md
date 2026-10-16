# 09 — Godot export

Target: Godot 4.x. Output is what the game already consumes: one sheet PNG
per character plus a `SpriteFrames` resource. Match the existing Twos Are Up
conventions exactly; fill them in here during Phase 2 after inspecting the
current project.

## Outputs

```
<godot_project>/<sprites_dir>/<character>/
  <character>.png              packed sheet, rows by direction, columns by frame
  <character>_frames.tres      SpriteFrames with one animation per (animation, direction)
  <character>.sonoranlab.json  provenance: job ids, manifest hashes, validator pass, export time
```

Optional: `<character>.tscn` with an `AnimatedSprite2D` wired to the
SpriteFrames, for drag-in testing.

## SpriteFrames contract

- Animation names: `profile.export.anim_name` template, default
  `{animation}_{direction}` → `walk_down`, `walk_left`, ...
- `loop` and `speed` (fps) from the profile's animation spec.
- Each frame is an `AtlasTexture` over the sheet with a `region` equal to its
  cell. No per-frame textures; keeps draw calls and import simple.
- Frame order within an animation is the pipeline's frame order; no
  reordering at export.
- If `mirror_right_from_left` is true, right-facing animations still get
  their own entries (mirrored at export), so the engine never needs `flip_h`
  logic for this character.

Write the `.tres` as text with `Resource` sub-resources for the atlases;
Godot's text resource format is stable enough to template, and it diffs in
git. Verify by opening the project headless (`godot --headless --import`) and
checking the import log for errors.

## Sheet packing

- Rows = directions in `profile.directions.order`; columns = frames.
- Cell = `profile.canvas.cell`; no spacing, no margin (atlas regions make
  spacing unnecessary and the pixel-perfect import is simpler).
- Multiple animations are stacked vertically: `walk` rows, then `idle` rows,
  etc. The `.sonoranlab.json` sidecar records the row map.
- PNG: RGBA, no interlace, no color profile chunk, palette-indexed when the
  sprite's colors ≤ 256 (smaller file, and a free second palette check).

## Import settings

Godot pixel-art imports need filter off and mipmaps off. Write a
`<character>.png.import` alongside with:

```
[params]
compress/mode=0
mipmaps/generate=false
process/fix_alpha_border=false
```

and set the project-level default texture filter to Nearest if it isn't
already. Do not rely on per-sprite `texture_filter` overrides.

## Smoke render (Phase 5)

`tools/godot/smoke_render.gd` is a headless script that:
1. loads the new SpriteFrames and one `style_refs` sibling,
2. places them side by side at 1× and 4×,
3. plays `walk_down` for one cycle,
4. screenshots to `artifacts/.../smoke.png` and exits.

The orchestrator looks at `smoke.png` as its last check. Pixel-level
problems are already gone by this point; this is for "does the new class
look like it belongs next to the old ones."
