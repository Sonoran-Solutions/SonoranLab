# 09 — Godot export

Target: Godot 4.7 (verified with 4.7.2.stable). Phase 2 exports **both** what the game consumes today (one sheet PNG
per character plus a `characters.json` entry) and a text `SpriteFrames` for drag-in testing. Code:
`sonoranlab/export/godot.py` (`export_godot`), CLI `sonoranlab export godot`. Everything below marked *verified* was
checked against Godot 4.7.2 and the private game checkout (read-only) on 2026-10-19.

## Outputs

`sonoranlab process` (README "How to run") calls the exporter itself, only after a validator PASS, with the clip
starting at column 1 and the west rows mirrored from east as real pixels; `--stills DIR` fills column 0 from
`DIR/still_<direction>.png`, validated before export. In `--sheet` mode, populated column-0 cells are preserved
and validated when scale is 1. HD sheets (scale > 1) must have transparent column 0; populated column 0 plus
`--stills` is also an input error. Both errors occur before the job dir is created. With `--godot-check` it first
exports into a temp copy of `tools/godot/spriteframes_check`,
runs the import and the checker below on it and writes `<job>/export/` only if both pass and the checked PNG/.tres hash
the same as the final ones (result in `manifest.json` `godot_check`); without `godot` on PATH that flag is an error. Godot 4.7.2 intermittently
aborts `--import` with `Parameter "singleton" is null ... is_cmdline_mode` (DYL-101); `pipeline.run_godot` re-runs only that exact
signature, at most twice, and lists every attempt in `godot_check.godot_attempts`.

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

Standalone `sonoranlab export godot` requires `--profile P --report report.json` and a single `--clip`.
The report must have `pass: true`, matching `character` and `animation`, and pixel digests for every generated
clip cell and every non-empty column-0 still. All report digests must match a sheet cell; mirrored clip rows must
be exact horizontal flips of their sources. Stills also pass the profile's cell rules. `--rows` and `--cell` must
match the profile; clip start must be ≥ 1, and unused columns must be transparent. Old reports without digests
are refused (exit 2, no files written). Re-run validation to obtain a bound report.
The report is also bound to the profile that validated it: its `provenance.profile.sha256` (written by both
`sonoranlab process` and `sonoranlab validate`) must equal the sha256 of `--profile`, and the `--clip` playback
metadata (frames, fps, loop, loop_from) must equal the profile's own animation of that name. Reports without profile
provenance, from a different profile, or with an altered clip are refused (exit 2, no files written).
The profile's `export.godot_project` is always protected; `--forbid-under` adds further directories (it never replaces
the game), and an `--out` inside any of them is refused. Sidecar `provenance.validator` records `pass`,
`report_sha256`, `profile_sha256`, `character`, `animation`, and `frames_bound` (matched clip and still digest count).

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
cp -r tools/godot/spriteframes_check /tmp/p
sonoranlab export godot sheet.png --profile toy.yaml --report report.json --character toy --cell 16 \
  --rows north,north-west,west,south-west,south,south-east,east,north-east \
  --clip run:1:4:10:loop --out /tmp/p
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
on the desktop) and saves a PNG per frame. It draws into a SubViewport sized from the frame (w x h cells give a
(24 + 5w) x (16 + 4h) image, 1x at (8, 8), 4x at (16 + w, 8)); the first version used a fixed 160x96 window that only
fit 16 px cells and cropped real ones (DYL-86). Both scales were pixel-exact against the source cells (no blended
colours at 4x) on the synthetic fixture and on the 136 px relay_sentinel export (2026-10-19).

## Smoke render (Phase 5, DYL-131)

`sonoranlab smoke-render --profile P --candidate JOBDIR --sibling ID=JOBDIR_OR_SHEET [...] --out OUT [--shader PATH]
[--godot BIN] [--timeout S]` loads the candidate's exact export and up to three approved siblings in a disposable Godot
project under `OUT/project/` (refused inside the repo or the game checkout) and renders them side by side. Exit 0 pass,
1 an objective check or the engine failed, 2 bad input (nothing written), 3 engine unavailable (`engine: unavailable`,
never a pass). Code: `sonoranlab/smoke.py`; engine side: `tools/godot/side_by_side/` (project template, `render_plan.gd`,
`plain.gdshader`, `render.sh`). `render_animation.gd` stays the single-animation tool.

**Inputs.** A candidate is an export dir (`<id>.sonoranlab.json` + PNG, sha256 re-checked). A sibling is an export dir, a
dir holding the game's `characters.json` plus its sheet, or the sheet PNG beside `characters.json`. `smoke.json` records
each sheet's sha256 and its sidecar / `characters.json` sha256. Paths must be inside the read roots / STATE, never the game
checkout; symlinks (leaf, ancestors escaping the root) are refused. Rows must equal the profile's order.

**Layout.** One baseline, 62 px/m (`canvas.density_px_per_m`): each character keeps its own square cell (never cropped),
cell bottoms aligned (feet at `cell - 4` for everyone), `#3a3a3a` background, 8/8/16 px margin/gap/group gap, groups wrap
below 12288 render px. The quad is a `QuadMesh` of `cell / 62` m under an orthographic camera, so 1 sheet pixel is exactly
1 (or 4) render pixels. The camera and quads are driven like `PixelStandee.cs`: `ShaderMaterial` with `sheet`, `grid`
(`columns`, 8), `cell` (column, row) and `cell_pixels` only (`border_color` stays at the shader default, `hit_flash` 0, no
dye). With `--shader` (the owner's staged private `PixelStandee.gdshader`; copied into `OUT/project/`, only its sha256 is
recorded, never commit it) the sticker border is drawn by that shader; without it `plain.gdshader` (same UV math, no
border) is used and the result says `border: not_rendered`. Sheets are read with `Image.load_from_file` (lossless, no
import step); the game imports the same pixels (`compress/mode=0`).

**Renders** (`OUT/render/`, 1x and 4x, plus `overview_{1x,4x}.png` = all 8 stills): `<row>_still_Nx.png` and
`<row>_<clip>_Nx.png` for every clip of the candidate, one group per frame, candidate first then siblings. The longest
clip sets the number of groups; shorter clips wrap, a sibling without the clip shows its still (recorded under
`frames_per_character`). Clip timing (start, frames, fps, loop, loop_from, seconds) is in `smoke.json`. Mirrored rows are
the sheet's own pixels. Godot saves transparent PNGs; Python composites the background, so it is exact. Budgets: 300 M
planned pixels (refused before anything is written) and 32 MiB of PNG (extra renders are not written, the check fails).

**Objective checks** (`smoke.json` `checks`; every one must pass): `alpha_binary` and `no_baked_border` per sheet (no
semi-transparent source pixel; no near-white silhouette ring), `frames_and_timing_match_manifest` (candidate clips vs the
profile's animations), `mirrored_rows_exact`, `cells_uncropped`, `render_complete`, `all_stills_and_clip_frames_rendered`,
`nearest_filtering` (every opaque source pixel equals its block in each render within `color_tolerance` 2; the observed
maximum is recorded) and `alpha_and_border` (render alpha binary; outside the sprite only the background, or with the
shader a flat keyline ring then a flat outer ring of two different colours, observed colours recorded). The tolerance and
the border-colour handling were chosen without a real run: confirm them on the first real render. `agent_recommendation`
and `owner_style_acceptance` are always empty; style acceptance is the owner's.

**Supervision.** The render runs `render.sh` (private headless GNOME Shell + llvmpipe, as `spriteframes_check/render.sh`)
in its own process group: wall-clock `--timeout` (default 600 s), output kept as 64 KiB head + 64 KiB rolling tail
(`OUT/engine.log`; a flood above 16 MiB is killed), `ulimit -f` 64 MiB, XDG config/data/cache/runtime dirs and the Godot
log under `OUT/xdg`, TERM, 5 s grace, KILL of the group and reaping on every exit; cancellation from the job worker does the
same. Nothing is matched by process name. Missing godot, gnome-shell or dbus-run-session is `engine: unavailable`.

**Siblings for the MCP job.** The MCP job accepts registry refs only. To use a staged game sheet, wrap it and import it:

```
uv run python -m sonoranlab.smoke stage --profile profiles/twos-are-up.yaml \
  --characters-json ~/sonoranlab-work/phase5/staged/game-<rev>/characters/characters.json --id pyromancer \
  --out ~/sonoranlab-work/phase5/staged-jobs/pyromancer
# then the MCP/API tool: import_job {project, character: "pyromancer", job_dir: <that dir>} -> artifact_id
```

The staged job holds the game's PNG and its `characters.json` entry as a sidecar, `report.pass: false` and a note that
SonoranLab neither made nor validated it. The artifact id is the `siblings` entry. Copy the owner's private shader to
`STATE/smoke/PixelStandee.gdshader` to get the border in MCP runs.
