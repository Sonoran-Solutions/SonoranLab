# 07 — Project profile (`profiles/<project>.yaml`)

The profile is the project's memory and the pipeline's source of truth. The
agent never passes canvas, palette, or foot-line values at call time; it reads
them from here and, if it must change one, edits this file so the change is
reviewable in git.

Example: `profiles/twos-are-up.yaml`. The real Twos Are Up values (ADR-0007) extend the example below:
- `cell: per_character` (square, from the game's manifest);
- `palette.source` + `sha256` instead of an inline hex list (the art is private, this repo is public);
- 8 directions with `generate` and `mirror_animations`;
- per-clip registration, and `foot_y: cell_minus_4`;
- the game's clip names (idle, run, attack, cast, death, channel).

Phase 2 (DYL-79) folds these into the schema.

```yaml
id: twos-are-up
name: Twos Are Up
style: pixel            # pixel | papercraft | ...  (other styles get their own chain later)

canvas:
  cell: [32, 48]        # final sprite cell size, px
  gen_scale: 16         # model canvas = cell * gen_scale per frame  (32*16=512, 48*16=768)
  padding: 2            # transparent px kept around the silhouette inside the cell
  sheet_layout: rows_by_direction   # rows_by_direction | strip

palette:
  hex:                  # exact shipped palette; Enforce Palette snaps to this
    - "#000000"
    - "#1a1c2c"
    # ...
  max_colors_per_sprite: 16
  dither: none          # none | ordered | error_diffusion

anchor:
  foot_y: 46            # row (0-based, from top) the foot line sits on in the cell
  anchor_x: 16          # column the character's center sits on
  foot_zone: [38, 47]   # rows scanned to find the lowest opaque pixel = foot line
  max_foot_slide_px: 1  # validator threshold across a cycle
  max_bbox_drift_px: 2  # validator threshold for silhouette bbox between adjacent frames

alpha:
  threshold: 128        # α below this becomes 0, at/above becomes 255
  bg_key: "#00ff00"     # color the generation workflows use as background when not producing RGBA
  fringe_passes: 1

directions:
  order: [down, left, right, up]   # row order in sheets and in SpriteFrames naming
  mirror_right_from_left: false    # true = generate left only, mirror in export (asymmetric gear forbids this)

animations:
  walk_4dir:  {frames: 8,  fps: 10, loop: true,  track_default: E, template: templates/poses/walk}
  idle_4dir:  {frames: 4,  fps: 6,  loop: true,  track_default: E, template: templates/poses/idle}
  attack_4dir:{frames: 4,  fps: 12, loop: false, track_default: C, template: templates/poses/attack_1h}
  hurt_4dir:  {frames: 2,  fps: 8,  loop: false, track_default: C, template: templates/poses/hurt}

references:
  style_refs:           # approved existing sprites used for style-drift comparison
    - assets/approved/knight_walk.png
    - assets/approved/mage_walk.png
  proportions: "chibi, ~2.5 heads tall, large head, short legs"
  outline: "1px dark outline, no anti-aliasing"
  camera: "3/4 top-down, slight downward angle"

export:
  godot_project: /home/dylan/games/twos-are-up
  sprites_dir: res://assets/sprites/classes
  spriteframes_name: "{character}_frames"
  anim_name: "{animation}_{direction}"

autonomy:
  allow_auto_approve: false   # may the agent approve its own candidates?
  max_seeds_per_attempt: 4
  max_attempts_per_animation: 3
  escalate_after: "ask"        # ask | switch_track | give_up
```

## Semantics

- `canvas.gen_scale` determines the model canvas per frame and the PixelOE
  downscale factor. Track A's LoRAs fix their own canvas (768² for a 6×4 of
  32×48 → effective 4×); the adapter reports the real factor in the manifest.
- `anchor.foot_zone` exists because flying/floating characters have no foot
  line; for those, set `anchor.mode: rig` and provide the anchor from the Track
  D bake, or `anchor.mode: bbox_bottom`.
- `directions.mirror_right_from_left` is a shipping decision, not a generation
  shortcut. With asymmetric gear it must be false.
- `references.style_refs` are what `smoke_render` places the new class next to
  and what the agent compares contact sheets against.
- `autonomy` bounds the orchestrator loop (`docs/10`).

## Validation of the profile itself

`sonoranlab profile check profiles/twos-are-up.yaml` verifies: every palette
entry is a valid hex; `foot_y` is inside `foot_zone`; every animation's
template exists and has the right direction/frame count; export paths exist.
The MCP server refuses to start on a profile that fails this.
