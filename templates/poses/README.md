# Pose templates

Reusable animation templates, independent of any character. See ADR-0003.

```
templates/poses/<animation>/
  template.yaml            clip, directions, source rig + license + sha256, reproducibility,
                           canvas-independent joints per frame set / direction / frame
  <direction>/
    pose_000.png           OpenPose-style skeleton render (black bg), one gen canvas
```

Bake outputs that stay outside the repo (`~/sonoranlab-work/benchmark/results/<date>/D/<rig>/`):
`f<frames>/{c<cell>,g<gen>}/{rgba,depth,pose}/<direction>/NNN.png` plus `bake.json` (per-canvas pixel
joints, bboxes, lowest opaque rows).

- Produced by `tools/godot/bake.sh` → `tools/godot/bake_rig.gd` (Track D, ADR-0005) from a rigged
  animation; check a bake with `tools/godot/check_bake.py`.
- Joints are true bone positions (OpenPose COCO-18 order; neck = shoulder midpoint; face points ride on the
  head bone and are `null` when facing away), in cell pixels at the profile density: `x` right of
  `anchor_x` (cell/2), `h` above the foot edge. Any canvas of width `W`: `s = W/cell`,
  `px = W/2 + x*s`, `py = (cell - 3)*s - h*s`. Per frame, `foot` is the lower ball (toe) joint.
- Registration is per clip (ADR-0007): the clip's lowest opaque row is `cell - 4`, frames keep their bob.
- Camera: eye level, orthographic, no pitch. `south` = front view, `east` = rig faces screen-right,
  `north` = back view; the west side is mirrored from east.
- `template.yaml` records where the rig/animation came from and its license (`docs/04`). Templates derived
  from non-permissive animation libraries do not ship; the *poses* are fine to reuse, the *mesh renders*
  may not be.
- Frame 0 of every direction is the one Track E uses as its seed: the bake picks right-foot touchdown.

Other cells: `sonoranlab/posetemplate.py` redraws these joints for any cell (`pose_frame` does this per job); the
shipped PNGs are the cell-136 renders and match it to within a few edge pixels (tests/sonoranlab/posetemplate).

Templates: `run` (6 and 7 frames, N/NE/E/SE/S; Phase 1, ADR-0007).
