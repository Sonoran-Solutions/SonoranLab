# ADR-0005 — Godot-native rig bake instead of Sprite Sage

**Status:** accepted (2026-10-16)

## Context

Earlier planning rounds assumed Sprite Sage offered local Qwen generation
and a usable `.glb` baker. Verification (2026-10-16) showed its README lists
only OpenAI/Google providers, Windows-only build steps, and a baker limited to
a constrained glTF subset. The project already targets Godot 4, which has a
complete glTF importer and headless rendering.

## Decision

Track D's rig bake is a Godot headless script (`tools/godot/bake_rig.gd`):
load a rigged animation, orthographic camera per direction, render N frames
to transparent PNGs, and emit OpenPose-style joint renders from the skeleton
bones. Sprite Sage is not a dependency or a reference implementation.

## Consequences

- One fewer external tool; bake and smoke render share a Godot setup.
- OpenPose templates come from true bone positions, not pose estimation, so
  they are exact for unusual proportions.
- Rig/animation sourcing (Mixamo-style libraries or hand-authored) needs its
  own license check in `docs/04`.
