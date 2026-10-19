# ADR-0004 — Lead generation track

**Status:** proposed, 2026-10-18. Accepted once the owner's 1× review fills the human columns of
`benchmark/scoring.csv` and confirms the headline below.

## Context

See `docs/05-pipeline-tracks.md` and ADR-0007. ADR-0007 sets the target: the game-native **run**, 5 generated directions
with the west side mirrored, per-character cells at 62 px/m, and the shipped palette. The original hypothesis: Track E
(HD animate → pixelize) leads for locomotion, Track C (pose-conditioned) handles held poses and attacks, and Track A
generates static candidates.

Evidence: [`benchmark/results/2026-10-18-phase1/phase1-report.md`](../../benchmark/results/2026-10-18-phase1/phase1-report.md)
and [`benchmark/scoring.csv`](../../benchmark/scoring.csv). The raw data, manifests and the local review page are in
`~/sonoranlab-work/benchmark/` (private art, not committed).

## Decision

| animation class | lead | fallback | why |
|---|---|---|---|
| locomotion (run; later walk/idle) | **E**: Wan 2.2 I2V A14B Q5_K_M + Lightx2v from the approved per-direction still → BiRefNet → deterministic chain | **C** for side / three-quarter directions only | E is the only track that runs in all 5 directions, including front and back, with per-clip identity. It costs 13.8–15.9 min (median, 4 seeds) of generation per humanoid set. C costs 39–48 min, can't run N/S, and drifts identity under a helmet. |
| held poses / attack keyframes (weapon pose matters) | **C**: Qwen Edit 2511 with the Track D skeleton as image1 and the character as image2 (native keypoint input, no ControlNet), inputs on a 1024 canvas | E with a pose-specific prompt | C keeps the weapon present and placed in side views, where E's weapon retention is weakest. To verify when Phase 6 adds attack templates. |
| non-humanoid | **E** (hover idle) | none tested | E's relay_sentinel hover passed the validator (seed 2). C has no skeleton for it. |
| templates | **D**: Godot rig bake (CC0 rig) | none | Exact, reproducible poses and anchors; it feeds C. |

Headline, in minutes from "go" to a usable run cycle (generation + processing + human cleanup). Characters #1 and
#2 decide; #3 is recorded.

| track | samurai (#1) | oathguard (#2) | relay_sentinel (#3) |
|---|---|---|---|
| A | out: format can't ship (4-dir 32×48 grid), no-ship license | out | out |
| B | out: format can't ship (4-dir 32×32 grid), no-ship license | out | out |
| C | **out**: 48.0 min (median) of generation before cleanup; N/S need full redraws, so it exceeds the 60-minute rule | out: identity drift (a new face per frame) | n/a |
| E | 15.9 gen (median of 4 seeds, range 15.6–16.1) + ~1 processing + _cleanup: owner review_ | 13.8 + ~1 + _owner_ | 9.3 + ~1 + _owner_ |

_Agent estimate, before owner review, for E samurai: 15–30 min of cleanup (re-placing the katana in about 6–10 frames,
removing the green cast on metal, one NE facing fix by regenerating that direction). That puts the total at roughly
32–47 min, under 60. If the owner's review disagrees, the owner's numbers decide._

What was cut, and why:
- **A/B:** the format can't ship into this game; svntax licensing is experimental/no-ship. Kept as the speed floor.
- **ControlNet-Union for pose:** no effect on Qwen Edit 2511. Native keypoint input replaces it.
- **Track C seeds 3–4:** C can't meet the 60-minute rule for character 1 at any seed.
- **736² Wan:** 2.8× slower, with no gain at 1×.

## Consequences

- **Phase 3:** implements the E adapter first (`workflows/video/wan22_i2v_lightx2v.json`, `select_loop.py` gait-loop
  selection), then C for held poses (`workflows/pose/qwen_edit_pose_native.json`, `batch_frames.py`,
  `prep_inputs.py` 1024 canvas).
- **Phase 2:** the deterministic core must include what the benchmark proved necessary:
  - edge-band magenta despill;
  - a per-cell colour cap (≤ 64, merge least-used);
  - PixelOE k-centroid at thickness 0;
  - per-clip registration;
  - validator limits calibrated on shipped clips (docs/08's loop-closure and area-CV defaults don't fit this game).
- **E improvements to test first in Phase 3:**
  - neutral grey generation background: magenta induces a green cast on metal in Wan's raw output. A controlled test
    (2 clips, same seed) removed the cast on grey, so grey is the Phase 3 default;
  - weapon-retention mitigations: seed selection by accessory-presence scoring, or a C-posed first frame;
  - facing lock for NE.
- **Memory budgets on Strix Halo** (no other GPU tenant):
  - E draws about 20 GiB; a 40 GiB window is ample.
  - Batched Qwen draws up to about 61 GiB; budget 64.
  - Single Qwen draws about 50 GiB; budget 52, or 60 if a GPU tenant may be resident.
- **Licensing:** the lead and fallback are Apache-2.0/MIT end to end (docs/04). The svntax LoRAs stay out of the
  default stack.
