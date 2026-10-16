# 08 — Validation

The validator is the only quality gate. Everything else is advisory. It runs
on processed frames (post-chain) and returns a JSON report the orchestrator
can read and act on. Thresholds come from the profile.

## Report shape

```json
{
  "pass": false,
  "animation": "walk_4dir",
  "frame_count": {"expected": 32, "actual": 32, "ok": true},
  "frames": [
    {
      "index": 0, "direction": "down", "frame": 0,
      "bbox": [6, 3, 26, 46],
      "foot_y": 46, "foot_slide": 0,
      "off_palette_before": 183, "off_palette_after": 0,
      "semi_alpha_pixels": 0, "fringe_pixels": 2,
      "touches_edge": false, "opaque_pixels": 411,
      "ok": true, "issues": []
    }
  ],
  "cycle": {
    "foot_slide_max": 3, "bbox_drift_max": 2, "opaque_area_cv": 0.07,
    "loop_closure_delta": 0.11
  },
  "failures": [
    "foot_slide: frame 9 (down/1) slid 3px, limit 1",
    "touches_edge: frame 20 (right/4) right edge, probable clipping"
  ],
  "warnings": [
    "opaque_area_cv 0.07 across cycle; >0.10 usually means a lost accessory"
  ]
}
```

`failures` is prose on purpose: it is written for the agent to read and turn
into a retry decision (regenerate frame 9, or redo the `right` direction).

## Rules

| Rule | What it checks | Default threshold | Severity |
|---|---|---|---|
| `frame_count` | number of frames matches the animation spec | exact | fail |
| `canvas` | every frame is exactly `cell` size | exact | fail |
| `palette` | zero pixels outside the profile palette after enforcement | 0 | fail |
| `semi_alpha` | no pixels with 0 < α < 255 | 0 | fail |
| `fringe` | count of opaque pixels whose color is a blend toward `bg_key` or toward white/black not in palette, on the silhouette border | ≤ `alpha.fringe_tolerance` (default 0) | fail |
| `touches_edge` | opaque pixels on the cell border (outside `padding`) | none | fail (clipping) |
| `foot_slide` | per-frame foot line vs `anchor.foot_y` after registration | ≤ `max_foot_slide_px` | fail |
| `bbox_drift` | silhouette bbox change between adjacent frames in a cycle | ≤ `max_bbox_drift_px` (walk); attack frames exempt via spec | fail |
| `opaque_area_cv` | coefficient of variation of opaque pixel count across a cycle | warn > 0.10 | warn |
| `loop_closure` | pixel difference between last frame and first for looping anims (normalized) | warn > 0.15 | warn |
| `direction_mirror` | if `mirror_right_from_left` is true, right frames must equal mirrored left | exact | fail |
| `accessory_presence` | (optional, needs a tagged region in the approved reference) presence of a color cluster in each frame, e.g. the red plume | present in ≥ 90% of frames | warn |
| `style_drift` | (Phase 5) palette histogram distance vs `style_refs` | warn above profile value | warn |

## Where the numbers come from

- **Foot line**: lowest row inside `anchor.foot_zone` containing an opaque
  pixel. For `anchor.mode: rig`, the Track D bake supplies the anchor per
  frame. For `bbox_bottom`, the silhouette's bottom row.
- **Registration happens before validation**; `foot_slide` therefore measures
  what registration could not fix (e.g. a frame whose feet are actually drawn
  in a different place), not raw model offset. Raw offset is logged separately
  in the manifest as `registration.shift_px` per frame so the agent can tell
  "model drifted but we fixed it" from "model drew the feet wrong."
- **Off-palette before** is informational and is the single best proxy for
  "the model fought the style." Track it over time.

## Golden tests

`tests/fixtures/` contains tiny synthetic sheets (4×4 cells of 8×8) with
known defects: one frame shifted 2px, one with a fringe, one touching the
edge, one off-palette. `tests/test_validate.py` asserts each rule fires on
exactly the right frame and nothing else. Any change to a rule updates the
fixture and says why.

## What the validator does not do

- Judge whether the animation *reads*. That is the contact sheet + a human
  (or the agent) in Phase 5's smoke render.
- Fix anything. It reports. Fixing is the chain's job or a regeneration.
