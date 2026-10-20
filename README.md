# SonoranLab

**A local, agent-driven sprite animation pipeline for Godot.**

SonoranLab turns a few reference images of a character into game-ready,
palette-locked, foot-anchored sprite sheets and animations, using only models
running on your own hardware. It exposes that pipeline as a small set of MCP
tools so an orchestrating agent (Claude Code / Opus) can create and animate a
new character class end to end, the way it currently does with a hosted
service, except nothing leaves the machine.

It is not a pixel editor and it is not a new inference engine. ComfyUI does the
generating. SonoranLab does everything ComfyUI is bad at: pose discipline,
pixel discipline, validation, project memory, and export.

## The one design rule

> **Generate motion at high resolution. Pixelize last. Deterministically.**

Diffusion models are good at temporally coherent HD motion and bad at
pixel-exact grids. Every "AI sprite sheet" approach that asks the model to emit
a 32×48 grid directly is fighting the model. SonoranLab gets a clean HD 2D
animation first (pose-conditioned edits or an image-to-video model), then runs
every frame through the **same** PixelOE → palette → grid-snap → foot-anchor
chain in code. Temporal consistency comes from the model. Pixel consistency
comes from code. Nothing about the pixel grid, the palette, the canvas, or the
foot line is ever delegated to a prompt.

## What it produces

- Four-direction (later eight) walk / idle / attack / hurt cycles for a character
- Static class art, portraits, icons, props
- Layered cosmetics (body + gear sheets, pixel-aligned for runtime stacking) — later phase
- Godot 4 `SpriteFrames` resources plus sheet PNGs, dropped straight into a project
- A validation report for every asset: frame count, bbox drift, foot slide,
  off-palette pixels, clipping, alpha fringe

## Layout

```
README.md                 you are here
PLAN.md                   phased implementation plan with exit criteria
CLAUDE.md                 conventions for agents working in this repo
docs/
  00-vision.md            what this is, what it is not, success criteria
  01-architecture.md      layers, data flow, where code vs model lives
  02-research-findings.md verified facts behind every decision, with sources
  03-hardware.md          Strix Halo / ROCm setup, the 3080's role, known jank
  04-models-and-licensing.md
  05-pipeline-tracks.md   Tracks A–E and the Phase 1 benchmark protocol
  06-tool-surface.md      the MCP tool contract the orchestrator sees
  07-project-profile.md   profile.yaml spec (palette, canvas, foot line, refs)
  08-validation.md        validator rules and thresholds
  09-godot-export.md      SpriteFrames / sheet export contract
  10-orchestrator-loop.md how Opus makes "a new class" out of references
  decisions/              ADRs
profiles/                 per-game profiles (Twos Are Up lives here)
templates/poses/          reusable pose/skeleton templates per animation
workflows/                ComfyUI workflows in API-JSON form (not UI graphs)
benchmark/                Phase 1 benchmark protocol and scoring sheet
```

Code lands in `sonoranlab/` (Python package) starting in Phase 2. See
[PLAN.md](PLAN.md).

## Status

Phases 0 (environment) and 1 (benchmark; lead track E, ADR-0004) are done.
Phase 2, the `sonoranlab` Python package (`uv sync`, `uv run sonoranlab --help`),
is in progress. Read [docs/02-research-findings.md](docs/02-research-findings.md)
before changing a model or tool choice.

## How to run

One command takes raw HD frames (`<direction>_<i>.png`, edge = cell × an integer
scale) to a validated job dir and, only if the validator passes, a Godot export:

```
uv run sonoranlab process RAW_DIR --profile profiles/twos-are-up.yaml \
  --character relay_sentinel --anim idle --out JOB_DIR [--stills DIR] [--godot-check]
```

Exit 0 = PASS and exported, 1 = validator FAIL (report and contact sheet written,
no export), 2 = usage/config/input error. `--sheet` takes one raw sheet in the
game layout instead of a frame dir. Column-0 stills are validated and preserved
at scale 1; HD sheets need transparent column 0. Populated column 0 and `--stills`
cannot be combined. Every parameter comes from the profile; the job
dir layout is in [docs/01](docs/01-architecture.md), the export in
[docs/09](docs/09-godot-export.md). Nothing runs on the network, ComfyUI or a GPU.

## Hardware this is designed for

- **Primary:** AMD Ryzen AI Max+ 395 "Strix Halo" (Radeon 8060S, large unified
  memory), Ubuntu, ROCm. All generation runs here.
- **Secondary (optional):** RTX 3080 for static SDXL/klein work only. It is not
  on the critical path and cannot run the video track.

See [docs/03-hardware.md](docs/03-hardware.md).

## Licensing

Repo license: TBD (owner's call). Model and adapter licenses are tracked per
model in [docs/04-models-and-licensing.md](docs/04-models-and-licensing.md);
the default stack is Apache-2.0 end to end so shipped game assets are clean.
