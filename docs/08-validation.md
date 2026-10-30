# 08 — Validation

The validator is the only quality gate. Everything else is advisory. It runs
on processed frames (post-chain) and returns a JSON report the orchestrator
can read and act on. Thresholds come from the profile.

## Report shape

Implemented by `sonoranlab/validate.py` (`validate(...)`, DYL-83) and `sonoranlab validate FRAMES_DIR --profile P
--character C --anim A [--stills DIR] [--out report.json]` (exit 0 pass, 1 failed, 2 usage/config/input error; `--out` never
overwrites). Abridged, with illustrative values:

```json
{
  "pass": false,
  "character": "samurai", "animation": "run",
  "scope": "technical; does not judge readability or style - a 1x human review is still required",
  "anchor_mode": "foot",
  "limits": {"max_foot_slide_px": 30, "max_bbox_drift_px": 16, "opaque_area_cv_warn": 0.11,
             "loop_closure_ratio_warn": 1.18, "source": "characters.samurai", "bbox_drift_gated": true},
  "notes": [],
  "frame_count": {"expected": 35, "actual": 35, "ok": true},
  "digests": {"east_6": "<sha256>", "still_south": "<sha256>"},
  "stills": [{"direction": "south", "canvas": [184, 184], "issues": [], "ok": true}],
  "frames": [
    {
      "index": 27, "direction": "east", "frame": 6, "ok": false,
      "issues": ["bbox_drift: frame 27 (east/6) bbox moved 18px (right edge; core 9px), limit 16"],
      "canvas": [184, 184], "bbox": [61, 40, 140, 180], "foot_y": 180, "contact": true,
      "foot_x": 101.5, "foot_slide": 9.5, "foot_body": 4.0, "bbox_drift": 18,
      "opaque_pixels": 5210, "colours": 48, "off_palette_after": 0, "fringe_pixels": 0,
      "semi_alpha_pixels": 0, "touches_edge": false,
      "pre": {"off_palette_before": 183, "fringe_removed": 2, "colours_merged": 4, "despilled_hd_px": 0}
    }
  ],
  "cycle": {"foot_slide_max": 38, "bbox_drift_max": 26, "opaque_area_cv": 0.07,
            "loop_closure_delta": 0.8, "loop_closure_ratio": 1.02},
  "directions": {"east": {"lowest_row": 180, "lowest_row_ok": true, "foot_rows": [], "contact_frames": [],
                          "foot_slide_max": 9.5, "bbox_drift_max": 18, "opaque_area_cv": 0.05,
                          "loop_closure_delta": 0.8, "loop_closure_ratio": 1.02}},
  "failures": ["bbox_drift: frame 27 (east/6) bbox moved 18px (right edge; core 9px), limit 16"],
  "warnings": ["opaque_area_cv: east 0.154 > 0.11; can mean a lost accessory"],
  "diagnostics": [
    {"index": 27, "direction": "east", "frame": 6, "rule": "bbox_drift", "value": 18, "limit": 16, "gated": true,
     "edge": "right", "edge_deltas": {"left": 16, "top": 2, "right": 18, "bottom": 0},
     "core_delta": 9, "protrusion": [1, 28], "seam": false}
  ],
  "chain": {"note": "frames[].pre and registration record what the processing chain changed ...",
            "off_palette_before": 183},
  "registration": {"east": {"shift_px": [-1, 3], "raw_shifts_px": [], "clipped_px": [0, 0, 0, 0, 0, 0, 0]}},
  "params": {}, "provenance": {}
}
```

- `failures` is prose on purpose: it is written for the agent to read and turn into a retry decision (regenerate
  frame 9, or redo the `east` direction). Frame rules read `<rule>: frame <index> (<direction>/<i>) <measured>,
  limit <limit>`; direction rules `<rule>: <direction> ...`. `pass` is true iff `failures` is empty (asserted).
- `diagnostics` (one per frame over the foot_slide or bbox_drift limit) are reported, never gated. They let an agent
  tell a weapon tip from body motion when picking a retry (ADR-0008): the edge that moved and all four edge deltas,
  the **core** delta on that edge (core = silhouette after a 5×5 binary opening, parts thinner than 5 px removed),
  the protrusion of that edge past the core before → after, `seam` (the frame 0 vs last comparison of a loop), and
  `foot_body` (planted foot vs the core centre instead of the clip anchor).
- `pre` (per frame) and `registration` (per direction) are provenance: what the chain changed. They are not verdicts
  and not proof the raw poses were consistent. `params` and `provenance` (profile + input hashes) are added by the
  CLI / pipeline.
- `digests` binds every validated clip cell (`<direction>_<i>`) and supplied still (`still_<direction>`) to its
  pixels: SHA-256 of `"<height>x<width>x<channels>:".encode() + frame.tobytes()`, independent of PNG encoding.
- `--stills DIR` reads `still_<direction>.png`. Stills use the same canvas, empty, palette, fringe, binary-alpha,
  colour-cap and padding gates as frames, with failures such as `palette: still south has 3 off-palette pixels`.
  They have no foot, motion, loop or registration gates. Unknown still directions are input errors.
  `stills` holds their records separately from `frames`; bad stills fail the report and prevent pipeline export.
- Frames are indexed in the order the clips are passed (the CLI uses the profile's sheet row order).

## Rules

Metric definitions are processing-v1's (`benchmark/tools/process.py`, calibrated on shipped clips, DYL-71) and are
unchanged in Phase 2 (ADR-0008 H0); `tests/sonoranlab/test_validate.py` checks parity with it. Every limit comes from
the profile: a character row, or the labelled `validator:` defaults. No limit may be unset.

| Rule | What it checks | Threshold (profile) | Severity |
|---|---|---|---|
| `frame_count` | each direction in `directions.generate` has the clip's frame count | exact | fail |
| `canvas` | every frame is exactly the character's cell | exact | fail |
| `empty` | the frame has opaque pixels | — | fail |
| `palette` | opaque pixels outside the profile palette after enforcement | 0 | fail |
| `fringe` | opaque silhouette-border pixels (4-neighbour transparent) whose colour is not in the palette. The chain already removes key-ward fringe (`pre.fringe_removed`); any such pixel is also a `palette` failure | ≤ `alpha.fringe_tolerance` | fail |
| `semi_alpha` | no pixels with 0 < α < 255 | 0 | fail |
| `colours` | distinct opaque colours per frame | ≤ `palette.max_colors_per_cell` | fail |
| `touches_edge` | bbox inside the `canvas.padding` margin | none | fail (clipping) |
| `registration` | registration pushed opaque pixels off the cell (`clipped_px`) | 0 | fail |
| `foot_line` | the direction's lowest opaque row is `anchor.foot_y` (cell − 4) | exact | fail |
| `foot_slide` | anchor_mode `foot`, contact frames (foot row within 1 px of the clip's lowest row): \|foot_x − anchor_x\|, foot_x = centre of the longest run on the frame's lowest row. Not a slide: the planted foot's offset from the clip anchor, a gross-error gate | ≤ `max_foot_slide_px` | fail |
| `bbox_drift` | max \|Δ\| of the 4 bbox edges vs the previous frame (frame 0 vs the last for loops) | ≤ `max_bbox_drift_px` | fail (warn when the caller passes `gate_bbox_drift=False`) |
| `direction_mirror` | when a mirrored row (`directions.mirror_animations`) and its source are both present, the mirrored frames equal the source flipped | exact | fail |
| `opaque_area_cv` | population std / mean of opaque pixel counts per direction | warn > `opaque_area_cv_warn` | warn |
| `loop_closure_ratio` | (share of last+first opaque union that differs) / mean of the in-clip adjacent deltas; loops only | warn > `loop_closure_ratio_warn` | warn |
| `hover_x_drift` | anchor_mode `bbox_bottom`: range of the core bbox centre x over the direction's frames | warn > `hover_x_drift_warn_px` | warn |
| `hover_bob` | anchor_mode `bbox_bottom`: range of the opaque centroid y over the direction's frames | warn < `hover_min_bob_px` | warn |
| `accessory_presence` | (optional, caller passes `accessory={"colours", "min_pixels", "min_share"}`) frames with ≥ `min_pixels` of the accessory colours | present in ≥ `min_share` (0.9) of frames | warn |
| `style_drift` | (Phase 5) palette histogram distance vs `style_refs` | warn above profile value | warn |

Owner decisions in ADR-0008 (accepted 2026-10-19), evidence in
`benchmark/results/2026-10-19-phase2/validator-study.md`:
- **H0**: humanoid (`foot`) foot_slide / bbox_drift and per-character limits stay as calibrated.
- **H4**: the uncalibrated `validator:` default bbox drift is 25 px (observed max of the 13 shipped runs, pooled;
  the old 16 rejected 4 of them). foot_slide stays 30. Calibrated characters keep their own limits.
- **F2**: for `bbox_bottom` (floating subjects) foot_slide is not computed or gated (reported `null`, with a note):
  the lowest row is a tassel tip, not a foot. bbox_drift and `foot_line` stay gates. The two hover warnings replace
  it; relay_sentinel sets 3 px (shipped core-centre x range max) and 1.5 px (half the shipped minimum bob, 3.11 px).
  A `bbox_bottom` character without both hover limits is a profile error.

Fall mode (ADR-0008 addendum 2026-10-29; animation `validator_mode: fall`, `loop: false` only, today the gun's
`death`). The report gains `validator_mode: "fall"`, `limits.fall_hold_max_delta` and per direction a `fall` block
(`drift_max`, `foot_slide_frame0`, `hold_delta`, `final_inside`, `static`, and `start_offset_px` when registration
recorded a fit). Changes, nothing else:

| Rule | Fall-mode behaviour | Threshold | Severity |
|---|---|---|---|
| `foot_slide` | gated on frame 0 only; later contact frames reported | ≤ `max_foot_slide_px` | fail (frame 0) |
| `bbox_drift` | gated value = max(\|Δ bottom edge\|, \|Δ core bbox centre x\|) vs the previous frame; top/left/right edges over the limit are warnings | ≤ `max_bbox_drift_px` | fail / warn |
| `fall_final` | the last frame's bbox lies inside `canvas.padding` and registration clipped none of its pixels | exact | fail |
| `fall_hold` | `silhouette_delta(last − 1, last)`: share of the pair's opaque union whose opacity differs (shape; colour shimmer of a resting Track E body is ignored) | ≤ 0.02 (`FALL_HOLD_MAX_DELTA`; owner 2026-10-29, calibrated on approved idles: ≤ 0.011) | fail |
| `static_clip` | every frame equals frame 0 (the game's `--pixel-check` repeated-still rule) | — | fail |
| `fall_start_offset` | registration fit the clip inside the cell (fit, then anchor; ADR-0008 fall addendum): frame 0 stands `offset_px` from the still's anchor | any non-zero offset | warn |

Fall clips register "fit, then anchor": frame-0 anchoring, plus the minimum horizontal shift that keeps the union of
all frames inside the padding when such a shift exists (`registration.<dir>.fit`: `padding`, `offset_px`, `fits`).
An unfittable clip keeps frame-0 anchoring and fails `touches_edge`/`fall_final`; frame 0's `foot_slide` gate still
measures against the cell anchor.

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

`tests/fixtures/dyl83/` holds tiny synthetic 24×24 clips (generated by `make_fixtures.py`; a test checks the PNGs
on disk equal the generator's output): a clean loop plus one defect per rule (shifted frame, foot slide, off-palette,
fringe, semi-alpha, touching the edge, wrong canvas, colour cap, foot line, missing accessory, thin-blade seam,
missing frame, clipped registration, mirror mismatch). `tests/sonoranlab/test_validate.py` asserts each rule fires on
exactly the intended (rule, direction, frame) and nothing else, that `pass` is false whenever `failures` is non-empty,
and parity with the Phase 1 `process.py` validator. With the private Phase 1 data present it also re-validates all 12
Track E sets: the 8 humanoid sets fail with exactly their bound reports' failures; relay_sentinel 1–4 pass under
ADR-0008 F2 with the predicted hover warnings. Any change to a rule updates the fixture and says why.

Production approval is separate: `sonoranlab review check JOB` rechecks schema 2 metadata and artifact hashes on every
call, as does `production_usable`. Reviews require a named reviewer, `pass|fail`, start/end timestamps in order with
the strict shape `YYYY-MM-DDTHH:MM:SS[.ffffff](Z|±HH:MM)` (1–6 fractional digits, offset hours ≤ 23 and minutes ≤ 59),
and finite non-negative numeric `cleanup_minutes` for a pass (zero is valid; booleans, numeric strings and integers
that overflow float conversion are rejected). Review, manifest and report JSON reads are limited to 16 MiB; excessive
nesting, duplicate object keys and non-standard constants (`NaN`, `Infinity`) are refusals. Optional `notes` and
`carried_from` must be strings; `supersedes` must contain string path, hash and prior review metadata. The writer
checks the complete document using the reader's metadata rules and size limit, and serializes with `allow_nan=False`
before creating a file. Check failures are JSON objects
with `code`, `field`, and `message`: `REVIEW_SCHEMA_INVALID`, `REVIEW_REVIEWER_INVALID`, `REVIEW_VERDICT_INVALID`,
`REVIEW_TIMESTAMP_INVALID`, `REVIEW_INTERVAL_INVALID`, `REVIEW_CLEANUP_INVALID`, `REVIEW_METADATA_INVALID`,
`REVIEW_UNREADABLE`, `REVIEW_NOT_PASS`,
or `REVIEW_BINDING_INVALID`. Bindings cover report, manifest, exports, contact sheet when present, all manifest outputs,
raw inputs, and the manifest's profile file; missing or changed files prevent approval. Every entry under `export/`,
including its root, is checked without following directory symlinks. Symlinks and special files in exports or hashed
paths are refused; JSON reads and hashing check `lstat` before opening a regular file. Schema version stays 2.
Bindings also cover the resolved job path and `repair-manifest.json` when present; a declared repair child requires
that regular, non-symlink sidecar. Adding, removing or changing it invalidates approval.
A moved or copied job needs a new review recorded at its new path, superseding the old review with `--supersedes`.
Move the copied `review.json` aside before recording its replacement; there is no silent relocation.

## What the validator does not do

- Judge whether the animation *reads*. That is the contact sheet + a human
  (or the agent) in Phase 5's smoke render.
- Fix anything. It reports. Fixing is the chain's job or a regeneration.
