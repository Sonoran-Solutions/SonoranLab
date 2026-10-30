# ADR-0008 — Validator semantics for weapon-bearing runs and floating subjects

**Status:** accepted (owner, 2026-10-19): **H0 + H4 + the per-failure diagnostics, and F2**, as recommended below. The
owner chose "Approve as recommended" in the Phase 2 orchestrator session. It takes effect from the Phase 2 profile and
validator (DYL-83); the frozen Phase 1 files and scoring are not changed retroactively.

## Context

ADR-0004 carries two validator questions into Phase 2:
1. All 8 Track E humanoid sets fail `bbox_drift` and/or `foot_slide`. Is that the generated motion, or the metric?
2. relay_sentinel fails `foot_slide` with a `bbox_bottom` anchor. Is that metric meaningful for a floating subject?

The limits are the observed max on one shipped clip per character (samurai 30 / 16, oathguard 20 / 8,
relay_sentinel 15 / 10; foot_slide / bbox_drift). The uncalibrated default `all` is 30 / 16.

Evidence: [`benchmark/results/2026-10-19-phase2/validator-study.md`](../../benchmark/results/2026-10-19-phase2/validator-study.md)
(script `benchmark/tools/validator_study.py`). It covers every failing frame of the 12 bound Phase 1 reports and every
shipped run (13) and idle (21) clip in the game. The facts this decision rests on:

- `foot_slide` is not a slide. It is the planted foot's offset from the bbox-centred clip anchor. Shipped planted feet
  move 8.5–36 px between contact frames by design. A thin accessory that pulls the bbox centre adds to the value:
  9–11 px of samurai's NE failures, 0–11 px of oathguard's.
- Samurai: 12 of 18 drift failures come from the thin katana tip appearing or vanishing. The other 6 are body/stride
  motion in the seed 3/4 east rows. All 4 foot failures are in NE, which is drawn as a side view (foot 22.5–27 px from
  the body, against 1 px in the shipped NE).
- Oathguard: the failures are concentrated in the east row (19 of 26 drift, 6 of 6 foot), and 22 of 26 are not thin
  parts. The generated east row exceeds the shipped oathguard east on every measure.
- On the shipped relay_sentinel, the "foot" is a 1–2 px tassel tip. The generated failures are all in the east row,
  where the trailing tassel tip lies 16–26.5 px from the body. 16 of 20 generated hover directions bob < 1 px vertically
  against 3.1–6.7 px shipped, and no current check sees that.
- The `all` default (30 / 16) fails 4 of 13 shipped runs and 2 of 21 shipped idles on bbox_drift. Oathguard's 20 / 8
  fails 12 of 13 shipped runs on bbox_drift and 7 of 13 on foot_slide.

## Options

Pass counts are for the Phase 1 Track E sets (humanoids 8, relay_sentinel 4) and are printed by the script ("Options"
table). Limits are always the shipped observed max rounded up (the DYL-71 method). "Core" = the silhouette after a
5×5 binary opening (parts thinner than 5 px removed).

### (a) Weapon-bearing humanoid runs

| option | what changes | Phase 1 sets passing | risk of letting bad animation through | |
|---|---|---|---|---|
| **H0 keep** | nothing: current foot_slide / bbox_drift and per-character limits | 0 / 8 | none added. Phase 3 must bring a set inside its own shipped clip's motion. | **recommended** |
| H1 core drift | bbox_drift measured on the core silhouette, own-clip limits (samurai 20, oathguard 11); foot_slide unchanged | 0 / 8 (same at r = 1, 3) | a flickering or vanishing thin weapon no longer trips drift (needs `accessory_presence`); adds a tunable radius | not recommended |
| H2 core drift + foot vs body | H1, and foot_slide measured from the core centre (own limits samurai 29, oathguard 19) | 2 / 8 (samurai 1, 2) at r = 2; 0 at r = 1; 3 at r = 3 | the verdict flips with the radius. The direction-pooled foot limit (set by the shipped side view) lets the NE side-view facing error through | not recommended |
| H3 pooled run limits | current metrics, limits pooled over all 13 shipped runs: 30 / 25 | 2 / 8 (oathguard 1, 4) | each character's own motion style is no longer enforced: oathguard may move 3× its shipped clip. The 25 comes from one clip (cleric east; next 19). | owner's call; not recommended |
| H4 default from the pool | only the uncalibrated `all` default changes: 30 / 16 → 30 / 25 (13 shipped runs). Calibrated characters keep their own limits | 0 / 8 (no Phase 1 set uses the default) | new characters get a looser drift gate, the same one shipped art needs | **recommended, with H0** |

Independent of the chosen option, **recommended:** the Phase 2 validator reports, per failing frame:
- the edge that moved and the 4 edge deltas;
- the core delta on that edge and the protrusion before → after;
- foot_body;
- the seam flag.

These are diagnostics, not gates. They let an agent tell a weapon tip from body motion when picking a retry.

### (b) Floating subjects (`anchor_mode: bbox_bottom`)

| option | what changes | Phase 1 sets passing | risk | |
|---|---|---|---|---|
| F0 keep | foot_slide on the bottom row stays a gate (limit 15) | 1 / 4 (seed 2) | gates the position of the lowest tassel tip, which is not a motion property. A longer trailing tassel fails while a hover that drifts sideways or doesn't bob passes. | not recommended |
| F1 body x-drift | for bbox_bottom, foot_slide is replaced by the range of the core centre x over the loop, limit = shipped max 3 px; bbox_drift unchanged | 2 / 4 (seeds 2, 3) at r = 2; 3 / 4 at r = 3 | calibrated on 1 clip × 4 frames × 5 directions; the 3 px limit is thin and radius-sensitive | gate later, once a second shipped floating clip exists |
| **F2 no foot_slide** | foot_slide is not computed/gated for bbox_bottom. bbox_drift and the lowest-row check stay gates. Two **warnings** are added: core centre x range > the shipped max (3 px), and centroid y range (bob) < half the shipped minimum (3.11 / 2 ≈ 1.5 px), per direction; the shipped clip triggers neither | 4 / 4 (all four then carry warnings: seeds 1 and 4 for drift, all four for low bob in at least one direction) | a floating subject that slides sideways ≤ 10 px per frame passes the gate; the warning surfaces it for the human review | **recommended** |

## Decision

Accepted by the owner on 2026-10-19: **H0 + H4 + the diagnostics, and F2**. Each part has its own justification:
- **H0.** Against their own shipped clips, the Phase 1 humanoid sets are out of spec for content reasons: NE facing
  drift, the oathguard east swing, and the katana tip popping in and out. That isn't a metric artefact, and no robust
  alternative metric changes it.
- **H4.** It changes no Phase 1 verdict. It is justified only by the default rejecting 4 shipped runs.
- **F2.** It replaces a measure of tassel length with warnings for the motion properties a hover actually has. It does
  let relay_sentinel seeds 1, 3 and 4 pass the gate; that is a consequence, not the reason.

## Consequences (if approved as recommended)

- DYL-83's `sonoranlab/validate.py` keeps processing-v1's foot_slide / bbox_drift and per-character limits. The
  profile's uncalibrated default becomes 30 / 25, cited to the validator study. bbox_bottom characters skip foot_slide
  and get the two hover warnings.
- ADR-0004 open item 1 stays open: Phase 3 still has to bring one humanoid set to a pass. The diagnostics point at
  weapon retention (samurai) and the east swing (oathguard). Open item 2 is closed by F2.
- A frozen-file change (`2026-10-18.yaml` addendum) is not made retroactively. The new semantics apply from the Phase 2
  profile on, and Phase 1 scoring is unchanged.

## Addendum 2026-10-29 — `fall` mode for death clips (owner: "New 'fall' rules (Recommended)", gun row only)

A death clip ends lying down: its top and sides move by most of the body height, and its feet leave the anchor. The
default gates would reject every real fall, and loosening the limits would weaken them for runs. Instead, a profile
animation may set `validator_mode: fall` (explicit; never inferred from `hold_last`; only on `loop: false` clips).
In fall mode, and only there:
- `foot_slide` is gated on frame 0 only (the approved standing pose); later frames are reported.
- `bbox_drift` is gated on the bottom edge and on the body core's centre x (same `max_bbox_drift_px`; 25 for gun);
  the top/left/right edges are reported as warnings, not gated.
- New gates per direction: `fall_final` (the last frame lies inside the cell padding and registration clipped none of
  its pixels), `fall_hold` (the held last frame is settled: `silhouette_delta(second-to-last, last)` — the share of
  the pair's opaque union whose opacity differs — is at most 0.02; amended 2026-10-29 by the owner, "Silhouette <= 2%",
  after live seeds showed the first rule (alpha or colour, 0.10) fails even approved idles, whose resting frames
  differ 24–38% in colour from re-quantisation but at most 1.1% in silhouette) and `static_clip` (not every frame equals frame
  0, mirroring the game's `--pixel-check` refusal of a repeated-still death row).
- `opaque_area_cv` stays a warning; the loop rules already do not run for loop=False. No limit is loosened and no
  other clip's rules change. `cast` uses the gun's existing gates unchanged.
- **Registration: fit, then anchor** (amended 2026-10-29 with the owner's "Widen the canvas for fall clips"). A fall
  clip is still registered on frame 0's anchor. If the union of all its frames' boxes would then leave the cell
  padding horizontally, and is narrow enough to fit, the whole clip moves by the minimum horizontal amount that keeps
  every frame inside (`registration.<dir>.fit.offset_px`); if no shift can fit it, it stays frame-0 anchored and
  `touches_edge`/`fall_final` fail it. A non-zero offset is reported as a `fall_start_offset` WARNING ("death start
  offset N px from the still's anchor") and as `fall.start_offset_px`, so the owner judges the jump at 1×. Frame 0's
  `foot_slide` gate is unchanged and is measured against the cell anchor, so an offset larger than
  `max_foot_slide_px` minus frame 0's own slide still fails. Loop clips and `cast` never fit.
