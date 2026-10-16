# ADR-0001 — Pixelize last, deterministically

**Status:** accepted (2026-10-16)

## Context

Every "AI sprite sheet" LoRA asks a diffusion model to emit a pixel grid
directly. Their own model cards document the failure modes: off-grid
"logical pixels," clipped accessories, lost details on back-facing rows,
palette drift. Meanwhile a working general-2D pipeline
(mor-o/comfyui-2d-character-pipeline) gets coherent motion by letting a video
model work at its native resolution and doing everything else in code.

## Decision

Generation targets a high-resolution canvas (`cell × gen_scale`). All
pixel-domain operations (downscale via PixelOE k-centroid, palette
enforcement, alpha cleanup, grid snap, foot-anchor registration, sheet
packing) happen afterward in the `sonoranlab` package, per frame, with the
same parameters for every frame of an animation. No prompt ever mentions the
palette, the canvas, or the grid as a constraint the model must honor.

## Consequences

- Consistency responsibilities are split: temporal/identity → model;
  pixel/palette/anchor → code. Each can be tested and improved separately.
- Grid LoRAs (Tracks A/B) remain useful as cheap candidate generators but
  their outputs still go through the chain.
- The deterministic core must exist before any generation adapter is worth
  much (Phase 2 precedes Phase 3).
- Resolution choice becomes a tuning parameter per animation.
