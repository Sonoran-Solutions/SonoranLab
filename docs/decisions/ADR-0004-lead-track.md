# ADR-0004 — Lead generation track

**Status:** owner-approved lead selection: **E** (2026-10-19). Phase 1 closes under an **explicit owner-approved
exception** to the 60-minute end-to-end criterion (see "Owner exception" below). The criterion was **waived, not
met**: cleanup was not timed, and only one Track E set (relay_sentinel seed 2) passes both the validator and the human
review. The validator failures are carried forward to Phase 2 as named open items.

Owner, on the 1× review: Tracks C and E look mostly similar except oathguard, which wasn't good in C; they prefer E,
mainly for its faster speed, and are close to approving the sets for use in the game.

Revised 2026-10-19 after a senior review. The earlier text called the lead "accepted" and E "well inside" the 60-minute
rule on generation time plus untimed cleanup. That overstated what the gate measured.

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

### What the gate did and did not establish

The frozen rubric (`benchmark/characters/2026-10-18.yaml`) defines a usable run cycle as validator pass **and**
human pass, and the headline as generation + processing + **stopwatch** cleanup minutes, with a 60-minute cut-off
for character #1. ADR-0007 kept both. These three acceptances are recorded separately:

| acceptance | E (lead) | C (fallback) | A / B |
|---|---|---|---|
| **Visual** (owner 1× review, human columns of scoring.csv) | 12/12 rated usable (samurai 4/4, oathguard 4/4, relay_sentinel 4/4) | samurai 2/2, oathguard 0/2 | 0/6, 0/12 |
| **Technical** (validator, calibrated on shipped clips) | 1/12 pass (relay_sentinel seed 2). Humanoids fail on bbox drift / foot offset from the weapon and shield swing | 0/4 pass | fail by format |
| **Total effort** (gen + processing + timed cleanup ≤ 60 min, char #1) | **not demonstrated**: cleanup untimed. Generation alone is 15.9 min (median, range 15.6–16.1), leaving ~43 min of headroom | out: 48.0 min of generation before cleanup | out |

Under the frozen definition, combined usable (validator and human) is E 1/12, C 0/4, A/B 0. The scorecard carries the
human verdict, the validator result and the combined value in separate columns.

Generation minutes per full set (median of the sets run; the time of selected successful attempts; failed and retried
attempts are listed separately in scoring.csv):

| track | samurai (#1) | oathguard (#2) | relay_sentinel (#3) |
|---|---|---|---|
| A | 2.4: out, format can't ship (4-dir 32×48 grid), no-ship license | 2.1: out | 2.1: out |
| B | 1.4: out, format can't ship (4-dir 32×32 grid), no-ship license | 1.3: out | 1.3: out |
| C | 48.0: out for locomotion (no front/back run) | 39.3: out (identity drift, a new face per frame) | n/a |
| E | 15.9 | 13.8 | 9.3 |

### Owner exception to the 60-minute criterion

On 2026-10-19 the owner chose, in the orchestrator session, to "record an explicit owner-approved exception to the
60-minute criterion in ADR-0004, with the validator failures carried forward to Phase 2", in preference to timing a
cleanup.

- **What is waived:** PLAN.md's and ADR-0007's requirement that the lead's best character-#1 run reaches a usable run
  cycle (validator **and** human pass) within 60 minutes of generation + processing + **timed** cleanup.
- **What it rests on:**
  - the owner's 1× visual review (E 12/12 human-usable);
  - their stated near-approval for in-game use;
  - E's measured generation time (15.9 min median for samurai, about 43 min under the cut-off before cleanup).
- **What it does not claim:** that E met the criterion. No cleanup time was measured. The scorecard keeps
  `cleanup_minutes` empty and `usable` (combined) at E 1/12.
- **Scope:** Phase 1's gate only. Phase 2/3 acceptance uses the rubric as written unless the owner records another
  exception.

### Validation failures carried forward to Phase 2

Validator results for every Track E set (bound reports in `set/processed/report.json`, processing-v1):

| set | result | failures |
|---|---|---|
| samurai 1 / 2 / 3 / 4 | FAIL | bbox_drift 2 / 5 / 6 / 5; foot_slide 1 each |
| oathguard 1 / 2 / 3 / 4 | FAIL | bbox_drift 6 / 7 / 8 / 5; foot_slide 1 / 3 / 1 / 1 |
| relay_sentinel 1 / 3 / 4 | FAIL | foot_slide 2 each (hover, bbox_bottom anchor) |
| relay_sentinel 2 | pass | none |

Open items for Phase 2 and Phase 3:
1. **Humanoid bbox drift and foot slide** come from the weapon and shield swing exceeding the shipped-clip limits
   (samurai 16 px / 30 px, oathguard 8 px / 20 px). Phase 2's validator (DYL-83) must keep these checks and report
   them per frame. Phase 3's E adapter must bring at least one humanoid set to a validator pass: weapon-retention
   seed selection, or a C-posed first frame. Alternatively, a recorded decision that these limits are wrong for
   weapon-bearing runs.
2. **relay_sentinel foot slide** on the east hover (`bbox_bottom` anchor). Check in Phase 2 whether that metric is
   meaningful for a floating subject.
3. **E failure modes seen in review:** weapon flicker/loss, NE facing drift, and the magenta-induced green cast (fixed
   on a grey background in a 2-clip test; grey is the Phase 3 default).
4. **Cleanup time** is still unmeasured. Time it on the first Phase 3 production run.

The white "papercraft" sticker border the owner noted as missing is **drawn at runtime by the game's
`PixelStandee.gdshader`**: a 1 px dark keyline plus a 1 px white border around the alpha mask. It is not baked into
shipped sheets (91% of the shipped samurai silhouette edge is dark outline). Generated frames rendered through that
shader logic get the identical border (orchestrator check, 2026-10-19). The profile keeps `baked_border: forbidden`
and the 2 px transparent margin the shader needs. Phase 2 contact sheets render through the same border logic, so
reviews look like the game.

What was cut, and why:
- **A/B:** the format can't ship into this game; svntax licensing is experimental/no-ship. Kept as the speed floor.
- **ControlNet-Union for pose:** no effect on Qwen Edit 2511. Native keypoint input replaces it.
- **Track C seeds 3–4:** C's generation time alone (48 min for samurai) plus front/back redraws can't meet the
  60-minute rule for character 1, whatever the seed.
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
- **Supported memory budgets** (`--need-gib`, host MemAvailable drawn; the guard adds an 8 GiB margin; see
  `env/runwindow.py`):
  - Wan 2.2, 33 frames: 40 (measured ≤ 27).
  - Batched Qwen Edit (`batch_frames.py`, 7 frames per prompt): **64** (measured 60.8).
  - Single Qwen Edit: 60 (default; covers a resident GPU tenant).
  - klein: 30.
- **Licensing:** the lead and fallback are Apache-2.0/MIT end to end (docs/04). The svntax LoRAs stay out of the
  default stack.
