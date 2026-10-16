# 00 — Vision

## The job to be done

Today: an orchestrating agent (Claude Opus via Claude Code) is handed reference
images and a class description, and uses a hosted pixel-art service's API to
produce the sprite sheets and animations for a new class in *Twos Are Up*.

Tomorrow: the same agent, the same inputs, the same output in the Godot
project, but every model runs on the Strix Halo box in the house. No
subscription, no upload, no rate limit, and a pipeline that improves whenever a
better open model drops.

## What SonoranLab is

- A **deterministic asset pipeline**: pixelize, palette-lock, alpha-clean,
  slice, foot-anchor, validate, export. Pure code. Runs anywhere.
- A **set of generation adapters** that drive ComfyUI workflows for the
  nondeterministic parts (character generation, pose-conditioned frames, HD
  animation).
- A **project profile** per game that holds the canvas, palette, foot line,
  approved references, and existing assets, so the pipeline has memory.
- An **MCP tool surface** so an agent can drive all of it with a handful of
  well-named calls and structured results.

## What SonoranLab is not

- Not a pixel editor. Pixelorama / Aseprite do manual touch-ups.
- Not a UI. A contact sheet PNG at 8× zoom is the review surface until that
  proves insufficient.
- Not a "PixelLab clone." PixelLab's shape (REST + MCP + skeleton tool) is the
  right shape and we borrow it. Its specific models are tuned for its task and
  we will not match their per-call hit rate on day one; we compensate with seed
  sweeps, validators, and a project profile that it doesn't have.
- Not a research project. If an approach can't produce a usable walk cycle
  for a plain humanoid inside an hour, it's cut.

## Success criteria

1. **Offline.** Full generate → export with the network cable out.
2. **One command / one tool call per animation.** No manual steps between
   reference images and a validated Godot `SpriteFrames` for a 4-direction
   walk cycle.
3. **Under 60 minutes** of total wall time + human cleanup per new class walk
   cycle in Phase 5, trending down.
4. **No off-palette pixels, no foot slide > 1px, no clipped sprites** in any
   exported asset. Enforced by the validator, not by inspection.
5. **Licensing clean.** Every model in the default stack is Apache-2.0 or
   equivalent, recorded in `docs/04`.

## Why this is the right scope

The market already solved "local static pixel art" (Retro Diffusion runs
locally in Aseprite for a one-time fee). Nobody ships **local, pose-driven
sprite animation with project memory and an agent interface.** That gap is
exactly the thing an RPG with many classes and equipment needs, and exactly
the thing that compounds: one approved walk cycle template gets reused across
every adventurer and every gear set.
