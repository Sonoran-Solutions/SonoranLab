# 05 — Pipeline tracks and the Phase 1 benchmark

A "track" is one way of getting raw frames out of a model. All tracks feed
the same deterministic chain (`docs/08-validation.md`). The adapter interface
is identical, so the orchestrator can switch tracks as a retry strategy.

## Tracks

### Track A — Single-shot grid LoRA (baseline)

- Base: Qwen-Image-Edit-2511 (FP8 + Lightning from the toolbox; bf16 if quality demands)
- Adapter: svntax combat LoRA (32×48; 6 cols × 4 rows: 3 walk, 2 attack, 1 hurt per direction)
- Input: one reference image + the LoRA's layout prompt, 768×768
- Output: a whole sheet in one shot; slice on a fixed 6×4 grid, 4× down
- Strength: fastest first draft; attack/hurt frames come free
- Weakness: no control over timing or weapon arcs; known back-row detail loss
- Role in v1: cheap candidate generator for static poses; benchmark floor

### Track B — Small-grid LoRA (baseline, different style)

- Base: FLUX.2-klein-base-4B (undistilled per the author; test distilled too)
- Adapter: svntax small LoRA (32×32; 4×4 grid), both 2750 and 3000-step weights
- Input: text or reference, 512×512 → 128×128
- Role: silhouette-readability comparison at 32×32 vs A's 32×48. Not expected to lead.

### Track C — Pose-conditioned frame transfer

- Base: Qwen-Image-Edit-2511 + InstantX ControlNet-Union (pose first; depth as fallback)
- Input: approved character reference + one control image per frame
  (OpenPose render from `templates/poses/`), generated one frame at a time or
  as a strip
- Output: HD frames (512–768 px per frame) that follow the template pose
- Strength: exact poses, exact timing, reusable templates across characters;
  this is the PixelLab-shaped approach
- Weakness: per-frame generation can drift in appearance between frames; the
  ControlNet was trained on photographic humans
- Mitigations to test: same seed per frame, reference image of the *previous*
  frame as a second input (2509+ supports multi-image), lower CFG, init-image
  refinement loop
- Role in v1: held poses, attack keyframes, and as the fallback when E drifts

### Track D — 3D-baked rig (no AI)

- Godot 4 scene: rigged humanoid (Mixamo-style FBX/glTF animation), orthographic
  camera, 4 (later 8) directions, fixed frame count per animation, transparent
  background, rendered at a multiple of the target cell size
- Output: perfectly consistent silhouette/pose frames with zero appearance
- Role: produces the pose templates for Track C (OpenPose rendered from the
  rig's joints, not estimated) and the seed/first frame for Track E. Also a
  direct option for monsters where a crude rig restyled by C/E beats diffusion
  improvising
- Why not Sprite Sage's baker: see `docs/02`. Godot has full glTF support and
  the project already targets Godot.

### Track E — HD-animate-then-pixelize (expected lead)

- Pattern: mor-o W1 → W2 → W3
  1. Pose the character into the animation's first frame (Track C single frame, or a Track D bake restyled)
  2. Wan 2.2 I2V 14B (base model, Lightx2v distill for speed) extrapolates motion at 512–768 px; prompt describes the motion ("walk cycle, side view, loop")
  3. BiRefNet removes background → RGBA frames
  4. Deterministic chain: PixelOE k-centroid ÷4–÷8 per frame → palette → alpha → register
- Strength: temporal coherence is the video model's job and it's good at it;
  pixel discipline is code's job
- Weakness: slowest track; loop closure (last frame → first) needs care; frame
  count selection (8–16 for walk) and foot contact timing are not controllable
  except through the prompt and the seed frame; direction changes mean one
  clip per direction
- Mitigations: generate more frames than needed and pick a clean loop by
  foot-anchor periodicity; use Track D's bake as the i2v seed so the pose and
  proportions are locked at frame 0
- Role in v1: locomotion (walk, idle, run)

## Deterministic chain (applied to every track, per frame)

```
raw frame (HD RGBA or RGB+bg)
  → alpha.extract      BiRefNet mask if present, else chroma key against the gen bg color
  → pixelize           PixelOE: outline expansion → k-centroid downscale to cell size
                       (A/B: PixelOE `contrast` mode, Median Fixer, nearest; pick by validator score)
  → palette.enforce    nearest color in profile HEX list; record off-palette count before snap
  → alpha.clean        threshold α ≥ profile.alpha_threshold; remove 1-px fringe; no semi-transparent pixels
  → register           find foot line (lowest opaque row inside the foot zone) or rig anchor; pad to
                       profile canvas; align feet to profile.foot_y; center horizontally on profile.anchor_x
  → validate           docs/08 rules → report.json
  → contact            8× NN contact sheet with badges
```

Order matters: pixelize before palette (quantizing first starves the
downscaler of gradient information), alpha clean after palette (the palette
step can produce new fringe colors), register last (needs the final
silhouette).

## Phase 1 benchmark protocol

> **Superseded in part by ADR-0007 (2026-10-18).** The frozen settings are in
> `benchmark/characters/2026-10-18.yaml`:
> - the clip is each character's `run` (#3: hover `idle`), over N/NE/E/SE/S with the west side mirrored;
> - model canvas is cell × 4; registration is per clip;
> - 4 seeds for C/E and 2 for A/B, smoke-tested on one direction first;
> - outputs live outside this public repo.
>
> The text below is the original protocol.

Characters: three approved Twos Are Up references as listed in `PLAN.md`.
Animations: `walk_4dir` only (12 frames: 3 per direction for A; 8 per
direction target for C/E). Seeds: 4 per configuration.

For each (track × character × seed):
1. Run the track adapter (or manual ComfyUI run in Phase 1).
2. Run the deterministic chain with the Twos Are Up profile.
3. Fill one row of `benchmark/scoring.csv`.
4. Keep everything under `benchmark/results/<date>/<track>/<char>/<seed>/`.

Headline metric: **minutes from "go" to a usable 4-direction walk cycle,
including human cleanup.** A cycle is usable when it passes the validator and
a human says it reads correctly at 1× in the engine.

Decision rule:
- Lead track = lowest median headline metric for characters #1 and #2.
- Any track whose best run for character #1 exceeds 60 minutes is out.
- Character #3 (non-humanoid) results are recorded but do not decide the lead;
  monsters may get their own track.

Write the decision as `docs/decisions/ADR-0004-lead-track.md`.

## What a track adapter looks like (Phase 3)

```python
@dataclass
class GenerateRequest:
    profile: Profile
    character: CharacterRef        # approved refs, id
    animation: AnimationSpec       # name, directions, frames, template path
    track: Literal["A", "B", "C", "E"]
    seed: int
    overrides: dict                # prompt tweaks, cfg, steps

@dataclass
class RawResult:
    frames: list[Path]             # raw/ paths, in animation order
    manifest: dict                 # workflow hash, model hashes, timings, seed
```

`generate(req) -> RawResult` is the whole contract. Everything after it is
the deterministic chain.
