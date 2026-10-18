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

## Addendum (2026-10-18, DYL-74)

- `godot --headless` cannot render (dummy rasterizer), and this box has no Xvfb. `tools/godot/bake.sh` starts a
  private headless GNOME Shell (mutter) compositor on its own D-Bus session (no activatable services), runtime
  dir and XDG dirs, and Godot draws into it with Mesa llvmpipe. No window reaches the user's desktop.
- Rig and animation: Quaternius Universal Base Characters + Universal Animation Library (CC0 1.0, recorded
  in `docs/04`), taken from the Twos Are Up history, not downloaded. `Jog_Fwd_Loop` is the game's own run.
- Two bakes of the same inputs give identical joints and byte-identical PNGs (1170 files), so the bake output
  is reproducible from the command line in `templates/poses/run/template.yaml`.
