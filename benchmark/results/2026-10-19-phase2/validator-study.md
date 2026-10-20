# Validator study: foot_slide and bbox_drift (DYL-83), 2026-10-19

Ticket: DYL-83 (Phase 2 validator), the threshold/semantics part only. This document is evidence plus a proposal.
**No threshold or validator semantics are changed by it**: the options and the recommendation are in
[ADR-0008](../../../docs/decisions/ADR-0008-validator-semantics.md) (proposed, awaiting owner approval).

Public repo, private art: no images or palette values here. Every number below is printed by
[`benchmark/tools/validator_study.py`](../../tools/validator_study.py), which reads the private data from disk.
The script output is reproduced verbatim at the end of this file ("Script output").

## Method

```
uv run --no-project --with pillow --with numpy --with pyyaml python -I benchmark/tools/validator_study.py \
  ~/Projects/TwosAreUp ~/sonoranlab-work/benchmark OUT.json --tables OUT.md
```

Inputs (read-only):
- **Generated:** the 12 Phase 1 Track E sets, `results/2026-10-18/E/<char>/<seed>/set/processed/`, i.e. the frames the
  owner reviewed and `report.json` is bound to. The script checks every processed frame's sha256 against the report's
  `provenance.outputs` and recomputes `foot_slide` / `bbox_drift` per frame with `process.validate`; it asserts that
  every value equals the bound report's.
- **Shipped:** every `run` (13 characters) and `idle` (21 characters) clip in the game's `characters.json`, 5 generated
  directions (N, NE, E, SE, S), cut from the sheets at game checkout `6f684b1` and registered with
  `process.register_clip` (the same per-clip registration the tracks get). For the 3 calibration characters, the script
  asserts the cut frames are pixel-identical to the frozen `characters/2026-10-18/gt_*.png` clips.

Current definitions (process.py, processing-v1), after per-clip registration (lowest row of the clip → cell − 4, median
bbox centre → cell / 2, one shift per direction):
- `foot_slide`: contact frames only (foot row within 1 px of the clip's lowest row); |foot_x − cell / 2|, foot_x = centre
  of the longest opaque run on the frame's lowest row.
- `bbox_drift`: max |Δ| of the 4 bbox edges vs the previous frame (frame 0 vs the last: the loop **seam**).
- Limits: observed max on the one shipped clip of each character, rounded up: samurai 30 / 16, oathguard 20 / 8,
  relay_sentinel 15 / 10 (foot_slide / bbox_drift); uncalibrated characters get `all` = 30 / 16.

Alternative metrics (all in the script's docstring):
- **edge Δ**: the 4 terms whose max is `bbox_drift`, so each failure names the edge that moved.
- **core**: the silhouette after a binary opening with a 5×5 square (radius r = 2): parts thinner than 5 px (blade,
  hair strand, tassel tip) are removed. `core_drift` = `bbox_drift` of the core. **protrusion** = how far the full bbox
  edge sticks out past the core edge (large = a thin part defines that edge). r = 1 and 3 are reported as a sensitivity
  check.
- **foot_body**: contact frames, |foot_x − core bbox centre x|. Same foot, measured from the body instead of from the
  clip anchor. foot_slide − foot_body ≈ how much of foot_slide is the anchor being pulled by an accessory.
- **planted_step** / **planted_dx**: between two consecutive contact frames, the displacement of the nearest foot-row run
  from the previous foot_x (nearest match, so optimistic). This is the "same foot stays planted" slide.
- **hover**: over the loop, the range of bbox centre x, core centre x, centroid x (horizontal drift) and of centroid y
  and bbox bottom (vertical bob); `foot_run_w` = width of the run foot_x is taken from.

## FACTS

### F1. Where the Track E failures are (question 1)

All counts are from the "Failing frames" table of the script output.

**samurai (limit bbox_drift 16, foot_slide 30): 18 bbox_drift + 4 foot_slide failing frames.**
- bbox_drift: 12 of 18 are **thin-protrusion driven**: the core edge moved ≤ 16 while the full edge moved 17–28. The
  protrusion column shows a thin part (the katana: 13–40 px past the core) appearing on or vanishing from that edge,
  e.g. samurai 1 east/6: protrusion 1 → 28, core Δ 9, full Δ 18. They spread over S (5), SE (3), NE (2) and E (2).
- 6 of 18 are **core driven**, all in the east row of seeds 3 and 4: core Δ 20–26 on the edge (seed 3: the trailing
  left edge, i.e. the stride; seed 4: the right/facing edge). The shipped samurai east core_drift is 20.
- 5 of 18 are at the loop seam (frame 0 vs last). Shipped clips also peak at the seam: for 9 of the 13 shipped runs, the
  max bbox_drift equals the seam value.
- foot_slide: all 4 are in **north-east** (32–38 px against 30). foot_body is 22.5–27, so 9–11 px of each value is the
  anchor offset (the bbox centre is pulled toward the katana). The rest is real: the shipped samurai NE has foot_body 1
  and foot_slide 12.5, so the generated NE places the feet like a side view. This matches the "NE facing drift" failure
  mode seen in the Phase 1 review. The metric caught a real content error, though not a slide.

**oathguard (limit bbox_drift 8, foot_slide 20): 26 bbox_drift + 6 foot_slide failing frames.**
- 19 of 26 bbox failures and all 6 foot failures are in the **east** row. 18 of 26 moved the right (facing) edge, the
  side the sword is swung on in the generated east clips (visual check of the local contact sheets).
- 22 of 26 are core driven (core Δ > 8): the moving part is at least 5 px thick, so the opening does not remove it (a
  blade that pixelizes ≥ 5 px wide, or arm and sword together). Only 4 of 26 are thin-protrusion driven.
- Generated east values (max per set): bbox_drift 20–28, core_drift 11–35, against shipped oathguard east 7 / 7.
- foot_slide: foot_body is 14–25.5 against shipped east 11; foot_slide − foot_body is 0–11 px of anchor offset. The
  generated east stride is about twice the shipped oathguard's (planted_step 17–27 vs 10), and the foot sits further
  from the body.

**relay_sentinel (limit foot_slide 15): 6 foot_slide failing frames, all in the east row of seeds 1, 3 and 4.**
- foot_run_w is 1–5 px: the "foot" is the tip of the trailing tassel, the lowest thing on a floating construct.
- foot_body ≈ foot_slide (16–26.5), so the anchor offset doesn't explain it: the generated east tassel trails further
  behind the body than the shipped one (shipped east foot_slide 14.5 is also a 1 px tassel tip).

### F2. Is foot_slide a slide? (question 2)

- **No.** It is the horizontal offset of the lowest-row run from the clip's bbox-centred anchor. The planted foot of a
  correct run-in-place moves in sprite space by design: the shipped planted foot moves 8.5–36 px between consecutive
  contact frames (planted_step, shipped runs). So foot_slide grows with stride length and with any accessory that shifts
  the bbox centre. The 2026-10-18 freeze already says that foot_slide "is a gross-error gate only".
- **No moonwalking in the generated sets.** Planted-foot direction over NE/E/SE: in the generated humanoids, 2 of 49
  planted steps move > 1 px toward the facing side, against 32 of 132 in the shipped runs (part of the shipped count is
  the nearest-run match switching feet in three-quarter views). No generated planted foot moves forward more often than
  shipped art does.
- **A world-locked slide metric would fail all shipped art.** The game's `PlayerController.RunSpeed` is 7. If the unit
  is metres, at 62 px/m and 10 fps the ground moves 43.4 px per frame, more than any shipped planted step (≤ 36). So
  shipped runs are not foot-locked to the ground, and "true slide" can only be measured relative to shipped sprite-space
  behaviour.
- **Alternative metrics on shipped vs generated** (max over directions; shipped run pool = 13 clips):

  | metric | shipped runs (min–max) | samurai E 1–4 | oathguard E 1–4 | own shipped clip (samurai / oathguard) |
  |---|---|---|---|---|
  | foot_slide (current) | 12.5–29.5 | 32–38 | 23–35 | 29.5 / 20 |
  | bbox_drift (current) | 8–25 | 20–28 | 20–28 | 16 / 8 |
  | core_drift (r = 2) | 10–24 | 15–26 | 14–35 | 20 / 11 |
  | foot_body | 13.5–29 | 22.5–27 | 17–25.5 | 29 / 18.5 |
  | planted_step | 8.5–36 | 12–24.5 | 1.5–27 | 31.5 / 28.5 |
  | centroid_step | 3–9 | 6.3–8 | 7.5–8.9 | 6.6 / 3.5 |

- **The alternatives still reject the generated humanoids against their own shipped clips.** Body-only drift (core,
  r = 2, own-clip limits samurai 20 / oathguard 11) passes 0 of 8 sets with foot_slide unchanged. Core drift plus
  foot_body passes samurai 1 and 2 at r = 2, but none at r = 1 and samurai 1, 2 and 4 at r = 3. That verdict depends on
  a free parameter, so it isn't robust. Oathguard fails every own-clip variant: its east row exceeds the shipped oathguard east on every
  measure (bbox_drift 20–28 vs 7, core_drift 11–35 vs 7, foot_body up to 25.5 vs 11).
- **centroid_step** (mass-weighted, insensitive to thin parts) is 6.3–8.9 for all 8 generated humanoid sets, against
  3.5 (oathguard) and 6.6 (samurai) for their shipped clips and 3–9 across all 13 shipped runs.

### F3. Floating subject (question 3)

- On the shipped relay_sentinel, the bottom-row run foot_x is taken from is 1–2 px wide in every direction (a tassel
  tip). Anchor-relative foot_x on that row measures how far the lowest tassel tip trails from the bbox centre. It says
  nothing about the hover's motion.
- **Horizontal drift over the loop** (core centre x range): shipped 0–3 px. Generated: seed 1 east 6, seed 4 east 3.5,
  seed 3 ≤ 2, seed 2 ≤ 0.5. Centroid x range: shipped ≤ 2.93; generated seed 1 east 6.87, seed 4 east 4.08, others
  ≤ 2.57.
- **Vertical bob** (centroid y range): shipped 3.11–6.68 px in every direction (bbox bottom range 2–10). Generated:
  16 of 20 directions bob less than 1 px. Only seed 2 SE (3.33), seed 3 SE/S (3.53 / 3.16) and seed 4 E (3.07) come close to
  the shipped minimum of 3.11. Seed 2, the only passing set, bobs ≤ 0.6 px in 4 of 5 directions. **The current validator can't
  see a hover that does not hover.**
- relay_sentinel's per-frame bbox_drift (2–6 generated vs shipped 10) is not the problem; the tassel-tip foot_slide is.

### F4. Calibration robustness (question 4)

- **The uncalibrated default `all` = 30 / 16 rejects shipped art:** 4 of 13 shipped run clips exceed bbox_drift 16
  (cleric 25, companion_ranged 19, road_warden 19, pyromancer 17), as do 2 of 21 shipped idles (fort_archer 21, regent
  21). foot_slide 30 passes all 13 shipped runs (max 29.5, samurai's own clip).
- **oathguard's 20 / 8 is a property of one stiff clip.** Applied to the other shipped runs it fails 12 of 13 on
  bbox_drift and 7 of 13 on foot_slide. Oathguard's run has the smallest bbox_drift of the 13 (8; next is
  companion_tank 9). Samurai's 30 / 16 fails 4 of 13 on bbox_drift. relay_sentinel's 15 / 10 is an idle limit and is
  not meant for runs.
- Pooled over the 13 shipped runs, the same method (observed max rounded up) gives foot_slide 30, bbox_drift 25. The 25
  comes from one clip (cleric east); the next highest is 19.
- Every shipped clip passes the limits calibrated on itself (by construction). None of the 3 calibration clips fails its
  own validator.

## RECOMMENDATIONS (proposal only, for the owner; options and risks in ADR-0008)

1. **Humanoid runs: keep the current semantics and per-character limits** (ADR-0008 option H0, "keep"). Against their
   own shipped clips, no robust variant passes any samurai or oathguard set (H2's samurai passes flip with the
   opening radius), and the failing frames trace to content, per direction:
   - the samurai NE is drawn as a side view;
   - the oathguard east row has 3–4× the bbox_drift of its shipped east row;
   - the katana tip appears and vanishes (12 of 18 samurai drift failures).

   These are content deviations, not metric artefacts. Don't adopt core-drift / foot-vs-body gates (H1/H2): they don't
   change the verdict robustly and they add a free radius.
2. **Fix the uncalibrated default with shipped evidence** (H4): calibrate the default on the 13 shipped run clips
   (30 / 25) instead of the 3-character max (30 / 16), which rejects 4 shipped runs. This changes no Phase 1 verdict.
   Prefer per-character calibration wherever the character has a shipped clip.
3. **Report the diagnostics, don't gate them.** The Phase 2 validator should put per-edge Δ, core Δ / protrusion,
   foot_body and the seam flag in each failure. Then an agent can tell "katana tip moved the right edge 28 px" from
   "body moved 25 px" and pick the right retry (weapon-retention seed vs re-generate).
4. **Floating subjects: stop gating foot_slide when `anchor_mode: bbox_bottom`** (F2). Add hover warnings: horizontal
   body drift above the shipped max, and bob below half the shipped minimum. Gate drift only once a second shipped
   floating clip exists to calibrate against.
5. Pooling the per-character limits (H3) is backed by the shipped-clip evidence but drops each character's own motion
   style. It's listed for the owner, not recommended.

## Script output

The verbatim `--tables` output of the command above (core radius 2) follows.

Game checkout 6f684b1, characters.json sha256 a30a1db6f619; core radius 2.

### Shipped run clips (max over N/NE/E/SE/S)

| clip | cell | foot_slide | bbox_drift | seam | core_drift | centroid_step | foot_body | planted_step |
|---|---|---|---|---|---|---|---|---|
| bellringer | 136 | 26.5 | 11 | 11 | 11 | 5.9 | 22 | 36 |
| cleric | 144 | 12.5 | 25 | 25 | 24 | 5.5 | 13.5 | 18 |
| companion_damage | 144 | 29 | 15 | 13 | 17 | 4.4 | 23.5 | 12 |
| companion_healer | 136 | 20 | 13 | 13 | 13 | 3 | 21.5 | 12.5 |
| companion_ranged | 144 | 17 | 19 | 19 | 20 | 9 | 22 | 28 |
| companion_tank | 168 | 16 | 9 | 9 | 10 | 3.9 | 16.5 | 16.5 |
| crossbowman | 168 | 26.5 | 16 | 16 | 18 | 3.4 | 29 | 25.5 |
| oathguard | 176 | 20 | 8 | 8 | 11 | 3.5 | 18.5 | 28.5 |
| pyromancer | 144 | 23 | 17 | 8 | 14 | 8 | 24 | 32.5 |
| road_warden | 136 | 21 | 19 | 19 | 16 | 3.1 | 24 | 34.5 |
| samurai | 184 | 29.5 | 16 | 16 | 20 | 6.6 | 29 | 31.5 |
| toll_sergeant | 192 | 28 | 10 | 7 | 11 | 6.7 | 23.5 | 8.5 |
| wizard | 152 | 20 | 11 | 7 | 10 | 5.4 | 21 | 31 |

### Shipped idle clips (max over N/NE/E/SE/S)

| clip | cell | foot_slide | bbox_drift | seam | core_drift | centroid_step | foot_body | planted_step |
|---|---|---|---|---|---|---|---|---|
| bellringer | 136 | 6 | 8 | 8 | 9 | 1.6 | 9.5 | 0.5 |
| buried_standard | 288 | 11.5 | 11 | 11 | 7 | 4.5 | 12 | 5 |
| cleric | 144 | 6 | 4 | 4 | 3 | 0.9 | 6 | 0 |
| companion_damage | 144 | 8.5 | 12 | 12 | 9 | 1.6 | 7.5 | 0.5 |
| companion_healer | 136 | 7.5 | 7 | 7 | 7 | 2.8 | 7.5 | 0.5 |
| companion_ranged | 144 | 7 | 3 | 3 | 4 | 1.1 | 7.5 | 0 |
| companion_tank | 168 | 13 | 6 | 6 | 17 | 2.7 | 14 | 0.5 |
| crossbowman | 168 | 9 | 9 | 9 | 8 | 1.4 | 11 | 0 |
| fort_archer | 168 | 14.5 | 21 | 21 | 2 | 1.2 | 8.5 | 0 |
| oathguard | 176 | 9 | 8 | 8 | 8 | 2.1 | 11.5 | 0 |
| pyromancer | 144 | 12 | 8 | 8 | 8 | 1.4 | 11.5 | 10 |
| regent | 184 | 8 | 21 | 21 | 12 | 5.2 | 11.5 | 0 |
| relay_sentinel | 136 | 14.5 | 10 | 10 | 12 | 6.7 | 13.5 | – |
| road_warden | 136 | 8 | 6 | 6 | 6 | 2.4 | 8 | 11 |
| samurai | 184 | 10 | 6 | 6 | 5 | 1.6 | 17.5 | 0 |
| shieldbearer | 152 | 14 | 9 | 9 | 8 | 4 | 9.5 | 18.5 |
| sir_bucket | 216 | 15 | 11 | 11 | 9 | 2.4 | 20 | 0.5 |
| squire | 120 | 4 | 10 | 10 | 11 | 3.8 | 5 | 0 |
| standard_bearer | 192 | 16.5 | 14 | 14 | 10 | 3.5 | 15.5 | 10.5 |
| toll_sergeant | 192 | 21 | 10 | 10 | 11 | 2.2 | 21 | 11.5 |
| wizard | 152 | 11.5 | 7 | 7 | 6 | 3.1 | 9.5 | 14.5 |

### Generated Track E sets (max over N/NE/E/SE/S; limits foot_slide / bbox_drift)

| set | limits | pass | cell | foot_slide | bbox_drift | seam | core_drift | centroid_step | foot_body | planted_step |
|---|---|---|---|---|---|---|---|---|---|---|
| oathguard 1 | 20 / 8 | no | 176 | 23 | 23 | 23 | 22 | 8.9 | 22 | 20.5 |
| oathguard 2 | 20 / 8 | no | 176 | 35 | 27 | 27 | 35 | 7.5 | 24 | 27 |
| oathguard 3 | 20 / 8 | no | 176 | 24 | 28 | 21 | 25 | 8.4 | 17 | 1.5 |
| oathguard 4 | 20 / 8 | no | 176 | 25.5 | 20 | 12 | 14 | 7.9 | 25.5 | 17 |
| relay_sentinel 1 | 15 / 10 | no | 136 | 21.5 | 6 | 3 | 10 | 4.5 | 21 | 20.5 |
| relay_sentinel 2 | 15 / 10 | yes | 136 | 6.5 | 2 | 1 | 12 | 3.3 | 6.5 | 5 |
| relay_sentinel 3 | 15 / 10 | no | 136 | 25.5 | 5 | 5 | 9 | 2.7 | 26.5 | 2.5 |
| relay_sentinel 4 | 15 / 10 | no | 136 | 21.5 | 4 | 4 | 5 | 3.7 | 21.5 | 8 |
| samurai 1 | 30 / 16 | no | 184 | 38 | 26 | 26 | 15 | 8 | 27 | 16 |
| samurai 2 | 30 / 16 | no | 184 | 32 | 20 | 20 | 16 | 7.6 | 22.5 | 20.5 |
| samurai 3 | 30 / 16 | no | 184 | 34.5 | 28 | 28 | 24 | 6.8 | 24.5 | 24.5 |
| samurai 4 | 30 / 16 | no | 184 | 32 | 27 | 27 | 26 | 6.3 | 23 | 12 |

### Per-direction, calibration characters: shipped vs generated (foot_slide / bbox_drift / core_drift / foot_body / planted_step)

| char | dir | shipped | seed 1 | seed 2 | seed 3 | seed 4 |
|---|---|---|---|---|---|---|
| samurai | north | 3.5 / 6 / 4 / 2.5 / 0 | 13.5 / 16 / 12 / 7.5 / – | 6 / 6 / 7 / 6.5 / 0.5 | 9.5 / 14 / 9 / 4 / – | 9.5 / 8 / 8 / 7.5 / 1 |
| samurai | north-east | 12.5 / 6 / 5 / 1 / 2.5 | 38 / 26 / 12 / 27 / 16 | 32 / 17 / 16 / 22.5 / 11 | 34.5 / 11 / 8 / 23.5 / 12 | 32 / 13 / 15 / 23 / 6.5 |
| samurai | east | 29.5 / 16 / 20 / 29 / 30.5 | 23.5 / 18 / 15 / 17.5 / 15.5 | 21 / 16 / 16 / 18.5 / 20.5 | 16.5 / 25 / 24 / 24.5 / 24.5 | 20 / 27 / 26 / 21 / 12 |
| samurai | south-east | 15 / 10 / 8 / 14 / 16.5 | 12 / 10 / 8 / 13 / 14.5 | 13 / 20 / 7 / 16.5 / 18.5 | 3.5 / 13 / 12 / 2.5 / – | 15.5 / 19 / 10 / 18 / 6 |
| samurai | south | 19 / 7 / 5 / 17.5 / 31.5 | 3 / 5 / 4 / 4 / 0.5 | 5 / 20 / 7 / 7 / – | 4.5 / 28 / 6 / 0.5 / – | 19 / 24 / 9 / 3 / – |
| oathguard | north | 9.5 / 4 / 6 / 12 / 15 | 5 / 8 / 7 / 4.5 / 0 | 8.5 / 8 / 8 / 11 / – | 7 / 9 / 9 / 6.5 / – | 8.5 / 5 / 5 / 8 / 0.5 |
| oathguard | north-east | 20 / 4 / 11 / 10.5 / 13 | 9.5 / 23 / 22 / 2 / – | 15 / 8 / 10 / 12 / 5.5 | 8 / 7 / 7 / 4 / 1.5 | 16 / 7 / 7 / 14 / 1.5 |
| oathguard | east | 12 / 7 / 7 / 11 / 10 | 23 / 21 / 11 / 22 / 20.5 | 35 / 27 / 35 / 24 / 27 | 24 / 28 / 25 / 17 / – | 25.5 / 20 / 14 / 25.5 / 17 |
| oathguard | south-east | 19.5 / 7 / 6 / 18.5 / 28.5 | 5.5 / 10 / 9 / 3.5 / – | 9.5 / 17 / 14 / 6 / – | 8.5 / 9 / 9 / 7 / – | 7 / 6 / 6 / 6 / 1 |
| oathguard | south | 10 / 8 / 8 / 11.5 / – | 5 / 4 / 4 / 8 / 2 | 9 / 4 / 5 / 9 / 3.5 | 8.5 / 3 / 3 / 9 / 1 | 9.5 / 9 / 10 / 9.5 / 1.5 |
| relay_sentinel | north | 2.5 / 10 / 8 / 3 / – | 3 / 0 / 1 / 3.5 / 1.5 | 5.5 / 1 / 8 / 5.5 / 0 | 4 / 1 / 9 / 3 / 1.5 | 2 / 0 / 0 / 1.5 / 1 |
| relay_sentinel | north-east | 4.5 / 3 / 6 / 4 / – | 11 / 1 / 2 / 11.5 / 2 | 3.5 / 1 / 7 / 4 / 2 | 5.5 / 1 / 1 / 4.5 / 2 | 2 / 1 / 1 / 3 / 1.5 |
| relay_sentinel | east | 14.5 / 6 / 12 / 13.5 / – | 21.5 / 6 / 8 / 21 / 20.5 | 6.5 / 0 / 1 / 6.5 / 1.5 | 25.5 / 5 / 7 / 26.5 / 1 | 21.5 / 4 / 5 / 21.5 / 8 |
| relay_sentinel | south-east | 1.5 / 4 / 8 / 1.5 / – | 6 / 1 / 10 / 6.5 / 7.5 | 4.5 / 2 / 12 / 4.5 / 5 | 10 / 4 / 3 / 9.5 / 1.5 | 2.5 / 0 / 0 / 3 / 4.5 |
| relay_sentinel | south | 2 / 3 / 3 / 1.5 / – | 5.5 / 2 / 2 / 7.5 / 1.5 | 3.5 / 0 / 6 / 4 / 2 | 3.5 / 4 / 9 / 4.5 / 2.5 | 2.5 / 2 / 2 / 3.5 / 1.5 |

### Failing frames of the bound reports, decomposed

| set | dir/frame | metric | value | limit | edge (Δ l,t,r,b) | core Δ on that edge | protrusion prev→cur | seam | foot_body | foot_run_w | planted_step |
|---|---|---|---|---|---|---|---|---|---|---|---|
| oathguard 1 | north-east/0 | bbox_drift | 23 | 8 | left (23,2,9,3) | 22 | 1→2 | yes | | | |
| oathguard 1 | east/1 | bbox_drift | 11 | 8 | right (5,1,11,2) | 11 | 1→1 |  | | | |
| oathguard 1 | east/3 | bbox_drift | 16 | 8 | right (8,3,16,0) | 11 | 5→10 |  | | | |
| oathguard 1 | east/3 | foot_slide | 23 | 20 | | | | | 22 | 4 | 20.5 |
| oathguard 1 | east/4 | bbox_drift | 21 | 8 | right (8,0,21,2) | 11 | 10→0 |  | | | |
| oathguard 1 | east/5 | bbox_drift | 9 | 8 | right (2,4,9,0) | 8 | 0→1 |  | | | |
| oathguard 1 | south-east/5 | bbox_drift | 10 | 8 | bottom (7,2,1,10) | 9 | 1→0 |  | | | |
| oathguard 2 | east/0 | bbox_drift | 27 | 8 | right (16,3,27,1) | 35 | 2→10 | yes | | | |
| oathguard 2 | east/1 | bbox_drift | 11 | 8 | right (9,1,11,0) | 12 | 10→9 |  | | | |
| oathguard 2 | east/1 | foot_slide | 23 | 20 | | | | | 14 | 2 | 17 |
| oathguard 2 | east/2 | bbox_drift | 15 | 8 | right (7,2,15,1) | 1 | 9→23 |  | | | |
| oathguard 2 | east/2 | foot_slide | 35 | 20 | | | | | 24 | 2 | 12 |
| oathguard 2 | east/3 | bbox_drift | 14 | 8 | left (14,2,10,1) | 13 | 2→1 |  | | | |
| oathguard 2 | east/4 | bbox_drift | 9 | 8 | right (8,0,9,0) | 13 | 25→21 |  | | | |
| oathguard 2 | east/4 | foot_slide | 25 | 20 | | | | | 17 | 2 | 17 |
| oathguard 2 | south-east/2 | bbox_drift | 17 | 8 | right (5,4,17,4) | 14 | 0→3 |  | | | |
| oathguard 2 | south-east/4 | bbox_drift | 12 | 8 | right (4,0,12,3) | 2 | 10→0 |  | | | |
| oathguard 3 | north/0 | bbox_drift | 9 | 8 | bottom (1,1,1,9) | 9 | 0→0 | yes | | | |
| oathguard 3 | east/0 | bbox_drift | 21 | 8 | right (11,4,21,2) | 8 | 14→1 | yes | | | |
| oathguard 3 | east/0 | foot_slide | 24 | 20 | | | | | 17 | 2 | – |
| oathguard 3 | east/1 | bbox_drift | 25 | 8 | right (4,1,25,2) | 22 | 1→4 |  | | | |
| oathguard 3 | east/2 | bbox_drift | 28 | 8 | right (12,3,28,0) | 25 | 4→1 |  | | | |
| oathguard 3 | east/3 | bbox_drift | 20 | 8 | right (2,4,20,2) | 18 | 1→3 |  | | | |
| oathguard 3 | east/4 | bbox_drift | 15 | 8 | left (15,1,11,2) | 14 | 0→1 |  | | | |
| oathguard 3 | east/5 | bbox_drift | 16 | 8 | left (16,3,15,0) | 15 | 1→0 |  | | | |
| oathguard 3 | south-east/1 | bbox_drift | 9 | 8 | right (1,3,9,7) | 9 | 1→1 |  | | | |
| oathguard 4 | east/0 | bbox_drift | 12 | 8 | left (12,3,10,3) | 12 | 1→1 | yes | | | |
| oathguard 4 | east/1 | bbox_drift | 20 | 8 | right (7,0,20,0) | 11 | 3→12 |  | | | |
| oathguard 4 | east/2 | foot_slide | 25.5 | 20 | | | | | 25.5 | 1 | 14 |
| oathguard 4 | east/3 | bbox_drift | 18 | 8 | right (10,2,18,1) | 14 | 5→1 |  | | | |
| oathguard 4 | east/4 | bbox_drift | 10 | 8 | right (6,0,10,3) | 10 | 1→1 |  | | | |
| oathguard 4 | south/0 | bbox_drift | 9 | 8 | top (2,9,5,0) | 10 | 1→2 | yes | | | |
| relay_sentinel 1 | east/1 | foot_slide | 18.5 | 15 | | | | | 16 | 1 | 13.5 |
| relay_sentinel 1 | east/2 | foot_slide | 21.5 | 15 | | | | | 21 | 1 | 3 |
| relay_sentinel 3 | east/0 | foot_slide | 24.5 | 15 | | | | | 26.5 | 3 | – |
| relay_sentinel 3 | east/1 | foot_slide | 25.5 | 15 | | | | | 26 | 5 | 1 |
| relay_sentinel 4 | east/1 | foot_slide | 16.5 | 15 | | | | | 16.5 | 1 | 6.5 |
| relay_sentinel 4 | east/2 | foot_slide | 21.5 | 15 | | | | | 21.5 | 1 | 5 |
| samurai 1 | north-east/0 | bbox_drift | 26 | 16 | right (9,0,26,1) | 12 | 33→19 | yes | | | |
| samurai 1 | north-east/2 | foot_slide | 38 | 30 | | | | | 27 | 2 | 8.5 |
| samurai 1 | east/6 | bbox_drift | 18 | 16 | right (16,2,18,0) | 9 | 1→28 |  | | | |
| samurai 2 | north-east/1 | bbox_drift | 17 | 16 | right (1,2,17,0) | 12 | 35→40 |  | | | |
| samurai 2 | north-east/2 | foot_slide | 32 | 30 | | | | | 22.5 | 4 | 11 |
| samurai 2 | south-east/3 | bbox_drift | 19 | 16 | left (19,1,6,2) | 7 | 1→13 |  | | | |
| samurai 2 | south-east/6 | bbox_drift | 20 | 16 | left (20,1,1,0) | 4 | 17→1 |  | | | |
| samurai 2 | south/0 | bbox_drift | 20 | 16 | left (20,2,2,2) | 5 | 26→11 | yes | | | |
| samurai 2 | south/4 | bbox_drift | 20 | 16 | left (20,5,5,2) | 6 | 0→14 |  | | | |
| samurai 3 | north-east/3 | foot_slide | 34.5 | 30 | | | | | 23.5 | 3 | 12 |
| samurai 3 | east/0 | bbox_drift | 22 | 16 | left (22,6,10,4) | 22 | 1→1 | yes | | | |
| samurai 3 | east/1 | bbox_drift | 20 | 16 | left (20,9,1,3) | 20 | 1→1 |  | | | |
| samurai 3 | east/3 | bbox_drift | 25 | 16 | left (25,2,7,2) | 24 | 0→1 |  | | | |
| samurai 3 | east/5 | bbox_drift | 23 | 16 | left (23,0,7,1) | 24 | 0→1 |  | | | |
| samurai 3 | east/6 | bbox_drift | 18 | 16 | right (5,2,18,0) | 0 | 7→25 |  | | | |
| samurai 3 | south/0 | bbox_drift | 28 | 16 | left (28,4,5,4) | 3 | 0→25 | yes | | | |
| samurai 4 | north-east/4 | foot_slide | 32 | 30 | | | | | 23 | 4 | 6.5 |
| samurai 4 | east/0 | bbox_drift | 27 | 16 | right (7,0,27,1) | 26 | 2→3 | yes | | | |
| samurai 4 | east/1 | bbox_drift | 19 | 16 | right (8,0,19,0) | 23 | 3→7 |  | | | |
| samurai 4 | south-east/6 | bbox_drift | 19 | 16 | right (10,3,19,3) | 5 | 1→15 |  | | | |
| samurai 4 | south/2 | bbox_drift | 23 | 16 | left (23,4,6,5) | 3 | 1→21 |  | | | |
| samurai 4 | south/6 | bbox_drift | 24 | 16 | left (24,2,4,2) | 4 | 21→1 |  | | | |

### Hover (relay_sentinel idle, bbox_bottom): drift and bob over the loop, per direction

| clip | dir | foot_slide | foot_run_w (contact) | cx_range | core_cx_range | centroid_x_range | centroid_y_range | bottom_range | centroid_step |
|---|---|---|---|---|---|---|---|---|---|
| shipped | north | 2.5 | [1] | 1 | 0 | 0.16 | 6.68 | 10 | 6.68 |
| shipped | north-east | 4.5 | [1] | 3 | 3 | 2.93 | 3.11 | 2 | 3.11 |
| shipped | east | 14.5 | [1] | 1.5 | 2 | 1.81 | 4.57 | 6 | 3.45 |
| shipped | south-east | 1.5 | [1] | 0.5 | 0 | 0.03 | 5.33 | 4 | 3.41 |
| shipped | south | 2 | [2] | 0 | 0 | 0.17 | 4.25 | 4 | 3.2 |
| seed 1 | north | 3 | [1, 1, 2, 2] | 0 | 0.5 | 0.08 | 0.18 | 0 | 0.18 |
| seed 1 | north-east | 11 | [1, 2, 2, 2] | 0.5 | 0 | 0.35 | 0.14 | 1 | 0.33 |
| seed 1 | east | 21.5 | [2, 1, 1, 2] | 6.5 | 6 | 6.87 | 0.69 | 1 | 4.55 |
| seed 1 | south-east | 6 | [2, 1, 1, 1] | 0 | 0 | 0.77 | 0.76 | 1 | 0.77 |
| seed 1 | south | 5.5 | [1, 3, 2] | 0.5 | 1 | 0.23 | 0.13 | 2 | 0.23 |
| seed 2 | north | 5.5 | [1, 1, 1, 1] | 0.5 | 0 | 0.31 | 0.39 | 0 | 0.37 |
| seed 2 | north-east | 3.5 | [1, 1, 1, 1] | 0 | 0 | 0.09 | 0.34 | 1 | 0.34 |
| seed 2 | east | 6.5 | [1, 1, 2, 2] | 0 | 0 | 0.19 | 0.42 | 0 | 0.36 |
| seed 2 | south-east | 4.5 | [1, 1] | 0.5 | 0.5 | 0.91 | 3.33 | 3 | 3.33 |
| seed 2 | south | 3.5 | [1, 2, 2, 1] | 0 | 0 | 0.27 | 0.6 | 0 | 0.55 |
| seed 3 | north | 4 | [1, 1, 2, 2] | 0 | 0 | 0.33 | 0.79 | 1 | 0.79 |
| seed 3 | north-east | 5.5 | [1, 2, 1, 1] | 0 | 0.5 | 0.39 | 0.3 | 1 | 0.39 |
| seed 3 | east | 25.5 | [3, 5] | 2.5 | 2 | 0.05 | 0.35 | 5 | 0.24 |
| seed 3 | south-east | 10 | [2, 1] | 1.5 | 2 | 2.57 | 3.53 | 4 | 2.69 |
| seed 3 | south | 3.5 | [2, 1] | 1 | 1 | 0.84 | 3.16 | 4 | 2.49 |
| seed 4 | north | 2 | [2, 2, 2, 2] | 0 | 0 | 0.06 | 0.19 | 0 | 0.19 |
| seed 4 | north-east | 2 | [1, 1, 1, 2] | 0.5 | 0 | 0.06 | 0.34 | 1 | 0.34 |
| seed 4 | east | 21.5 | [2, 1, 1, 1] | 3.5 | 3.5 | 4.08 | 3.07 | 1 | 3.66 |
| seed 4 | south-east | 2.5 | [2, 1, 2, 1] | 0 | 0 | 0.42 | 0.2 | 0 | 0.42 |
| seed 4 | south | 2.5 | [3, 3, 3, 4] | 1 | 0.5 | 0.19 | 0.27 | 0 | 0.27 |

### Planted-foot direction, NE/E/SE: steps moving > 1 px toward the facing side / all planted steps

| clip | forward / steps |
|---|---|
| shipped bellringer run | 4/12 |
| shipped cleric run | 2/12 |
| shipped companion_damage run | 1/2 |
| shipped companion_healer run | 2/9 |
| shipped companion_ranged run | 1/4 |
| shipped companion_tank run | 4/14 |
| shipped crossbowman run | 3/14 |
| shipped oathguard run | 1/11 |
| shipped pyromancer run | 2/9 |
| shipped road_warden run | 6/15 |
| shipped samurai run | 1/10 |
| shipped toll_sergeant run | 0/8 |
| shipped wizard run | 5/12 |
| E samurai 1 | 0/9 |
| E samurai 2 | 0/7 |
| E samurai 3 | 0/6 |
| E samurai 4 | 0/10 |
| E oathguard 1 | 0/2 |
| E oathguard 2 | 1/7 |
| E oathguard 3 | 0/3 |
| E oathguard 4 | 1/5 |

### Shipped clips failing the calibrated limits (current semantics; foot_slide > limit, bbox_drift > limit)

| limits (foot / drift) | run clips failing foot_slide | run clips failing bbox_drift | idle clips failing foot_slide | idle clips failing bbox_drift |
|---|---|---|---|---|
| samurai (30 / 16) | 0/13: – | 4/13: cleric, companion_ranged, pyromancer, road_warden | 0/21: – | 2/21: fort_archer, regent |
| oathguard (20 / 8) | 7/13: bellringer, companion_damage, crossbowman, pyromancer, road_warden, samurai, toll_sergeant | 12/13: bellringer, cleric, companion_damage, companion_healer, companion_ranged, companion_tank, crossbowman, pyromancer, road_warden, samurai, toll_sergeant, wizard | 1/21: toll_sergeant | 11/21: buried_standard, companion_damage, crossbowman, fort_archer, regent, relay_sentinel, shieldbearer, sir_bucket, squire, standard_bearer, toll_sergeant |
| relay_sentinel (15 / 10) | 12/13: bellringer, companion_damage, companion_healer, companion_ranged, companion_tank, crossbowman, oathguard, pyromancer, road_warden, samurai, toll_sergeant, wizard | 10/13: bellringer, cleric, companion_damage, companion_healer, companion_ranged, crossbowman, pyromancer, road_warden, samurai, wizard | 2/21: standard_bearer, toll_sergeant | 6/21: buried_standard, companion_damage, fort_archer, regent, sir_bucket, standard_bearer |
| all (30 / 16) | 0/13: – | 4/13: cleric, companion_ranged, pyromancer, road_warden | 0/21: – | 2/21: fort_archer, regent |

### Options: pass/fail per Phase 1 E set

pooled shipped run limits (13 clips): foot_slide 30, bbox_drift 25; own-clip limits: {"oathguard": {"core_drift_max": 11, "foot_body_max": 19, "core_cx_range": 6}, "relay_sentinel": {"core_drift_max": 12, "foot_body_max": 14, "core_cx_range": 3}, "samurai": {"core_drift_max": 20, "foot_body_max": 29, "core_cx_range": 6}}

| set | keep | H1 core drift | H2 core drift + foot vs body | H3 pooled run limits |
|---|---|---|---|---|
| oathguard 1 | FAIL | FAIL | FAIL | pass |
| oathguard 2 | FAIL | FAIL | FAIL | FAIL |
| oathguard 3 | FAIL | FAIL | FAIL | FAIL |
| oathguard 4 | FAIL | FAIL | FAIL | pass |
| samurai 1 | FAIL | FAIL | pass | FAIL |
| samurai 2 | FAIL | FAIL | pass | FAIL |
| samurai 3 | FAIL | FAIL | FAIL | FAIL |
| samurai 4 | FAIL | FAIL | FAIL | FAIL |
| **passing** | 0 | 0 | 2 | 2 |

| set | keep | F1 body x-drift | F2 no foot_slide |
|---|---|---|---|
| relay_sentinel 1 | FAIL | FAIL | pass |
| relay_sentinel 2 | pass | pass | pass |
| relay_sentinel 3 | FAIL | pass | pass |
| relay_sentinel 4 | FAIL | FAIL | pass |
| **passing** | 1 | 2 | 4 |


### Core-radius sensitivity: sets passing each option at opening radius r = 1 / 2 / 3

| option | r = 1 | r = 2 | r = 3 |
|---|---|---|---|
| keep | relay_sentinel 2 | relay_sentinel 2 | relay_sentinel 2 |
| H1 core drift | none | none | none |
| H2 core drift + foot vs body | none | samurai 1, samurai 2 | samurai 1, samurai 2, samurai 4 |
| H3 pooled run limits | oathguard 1, oathguard 4 | oathguard 1, oathguard 4 | oathguard 1, oathguard 4 |
| F1 body x-drift | relay_sentinel 2, relay_sentinel 3 | relay_sentinel 2, relay_sentinel 3 | relay_sentinel 2, relay_sentinel 3, relay_sentinel 4 |
| F2 no foot_slide | relay_sentinel 1, relay_sentinel 2, relay_sentinel 3, relay_sentinel 4 | relay_sentinel 1, relay_sentinel 2, relay_sentinel 3, relay_sentinel 4 | relay_sentinel 1, relay_sentinel 2, relay_sentinel 3, relay_sentinel 4 |
