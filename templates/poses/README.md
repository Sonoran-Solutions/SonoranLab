# Pose templates

Reusable animation templates, independent of any character. See ADR-0003.

```
templates/poses/<animation>/
  template.yaml            frames, fps, loop, directions, anchor per frame, source rig + license
  <direction>/
    pose_000.png           OpenPose-style skeleton render (black bg), model-canvas size
    depth_000.png          optional depth render
    bake_000.png           optional raw Track D bake (grey mannequin, RGBA)
```

- Produced by `tools/godot/bake_rig.gd` (Track D) from a rigged animation,
  or hand-authored in any editor that can draw a skeleton.
- `template.yaml` records where the rig/animation came from and its license
  (`docs/04`). Templates derived from non-permissive animation libraries do
  not ship; the *poses* are fine to reuse, the *mesh renders* may not be.
- Frame 0 of every direction is the one Track E uses as its seed; make it a
  clean contact pose.
- Anchor per frame (`foot_y`, `anchor_x` in model-canvas pixels) comes from
  the rig's foot bones, so registration for Track C/E can use rig anchors
  instead of silhouette scanning when `anchor.mode: rig`.

First templates to build (Phase 1 Track D): `walk` (8 frames), `idle` (4),
`attack_1h` (4), `hurt` (2), four directions each.
