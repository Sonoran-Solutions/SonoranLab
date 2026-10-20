# Phase 3 senior-review handoff (2026-10-20)

**Revision 2026-10-20 (after the senior review of #26–#32):** the three P1 findings and the P2 are fixed in the PRs
where they lived (#28, #29, #30, #31); see `phase3-gate.md` §6. Existing owner approvals are re-bound to their exact
artifacts. The corrected pyromancer export (stills in column 0) awaits one owner 1× confirmation. A hosted
combined-stack check runs on a disposable `[DO NOT MERGE]` integration PR.

**Recommendation: GO for Phase 4**, conditional on the owner reviewing and merging the PR stack below. All seven Phase 3
exit criteria are met on the evidence in `phase3-gate.md`. **Nothing is merged; no auto-merge is enabled.**

## Exit criteria

| # | criterion | evidence | status |
|---|---|---|---|
| 1 | Track E and the useful Track C fallback run through the common adapter interface | `generate(request) -> RawResult` (#28); Track E: 29 live clips, 29/29 first pass; Track C: 1 live posed frame (gate §5) | met |
| 2 | one command generates, processes, validates and exports a game-native run cycle for an approved character | `sonoranlab animate` on pyromancer: exit 0, 5 generated directions + mirrored west, 6 frames at 10 fps, cell 144, palette-locked, export only on PASS (gate §3). Revision: the sheet now carries all 8 approved stills in column 0, verified pixel-exact (gate §6) | met; corrected export awaits owner 1× confirmation |
| 3 | at least one humanoid full run set passes the existing validator | samurai all-grey set, limits 30 / 16 unchanged, 0 failures, owner 1× pass (gate §2); oathguard also passes 20 / 8 (technical only, §4) | met |
| 4 | raw outputs, failed seeds, provenance and timing preserved | immutable attempt dirs with request/run/result JSON; `sweep.json` keeps every rejected candidate with reasons; `animate.json` per-stage timing | met |
| 5 | Godot import and playback pass, with a 1× human review | Godot 4.7.2 import + SpriteFrames check pass on samurai and pyromancer (both exports); owner 1× pass on both reviewed artifacts, now hash-bound (`review.json` schema 2) | met |
| 6 | human cleanup measured; honest PASS/HOLD on the 60-minute criterion | pyromancer: 29.3 min effort, 40.0 min wall incl. an unrelated GPU wait; cleanup 0 by the owner's report; **the review stopwatch figure was never supplied** — the ≤ 0.72 min approval interval is used as an upper bound → **PASS, n = 1** | met (this run only) |
| 7 | offline, no interference with other services | all inference in the `--network=none` container; deterministic stage byte-identical under `unshare -rn`; runwindow paused nothing; owner services untouched. Revision: every public generation entry point (`generate`, `sweep run`, `animate`) now enters the guard with the track's minimum budget (E 40, C 60) | met |

## PRs (review in this order; each body has head SHA, scope, tests, limitations)

| PR | ticket | content | base |
|---|---|---|---|
| #26 | DYL-98 | ComfyUI client: prompt-scoped submit/wait/cancel, no blind resubmits, provenance, podman-exec offline transport | main |
| #27 | DYL-101 | bounded, recorded retry for Godot 4.7.2's `--import` singleton crash (independent) | main |
| #28 | DYL-99 | adapter contract + Track E (grey bg, BiRefNet, loop selector port, `sonoranlab generate`, TERM→scoped cancel) | #26 |
| #29 | DYL-100 | Track C adapter (Qwen Edit 2511 native keypoints, 1024 conditioning, no ControlNet) | #28 |
| #30 | DYL-102 | seed/loop-window sweep, per-direction ranking, full-set revalidation, review records | #29 |
| #31 | DYL-104 | `sonoranlab animate` | #30 |
| #32 | DYL-103/105 | pyromancer profile row (default limits), gate evidence, this handoff | #31 |

Merge order: #26 → #28 → #29 → #30 → #31 → #32, and #27 at any point. Integration check (orchestrator): #32's head merged
with #27 has no conflicts; full `pytest tests` 795 passed with real Godot; ruff clean. CI green on every PR (#28 needed
one rerun of `tests/ops/test_run_track.py::test_concurrent_same_job_is_rejected`, a pre-existing Phase 1 test that
flaked; untouched by this phase).

## Tickets

DYL-98, 99, 100, 101, 102, 104: In Review. DYL-103 (humanoid gate) and DYL-105 (timed gate): evidence complete, In
Review. Milestone "Phase 3 — Generation Adapters".

## Measurements (details in `phase3-gate.md`)

- Track E clip: 169–216 s wall at 560² / 33 frames (samurai, oathguard), ~149 s at 432² (pyromancer); peak memory drawn
  ≤ 23.9 GiB (budget 40). Track C frame: 165 s, peak 48.5 GiB (budget 60). No pause of any service was needed.
- Pyromancer timed run: prep 0.87, generation 24.88 (10/10 first pass), processing 1.94, validation/export 0.24,
  review ≤ 0.72, cleanup 0 min.
- Live cancellation: SIGTERM to the run window → scoped job cancel → queue empty in 5 s, GTT back to baseline.

## Assets (private; never in this repo or the game)

- Samurai approved set: `~/sonoranlab-work/phase3/2026-10-20/validate/samurai-grey-final-godot/` (export, report,
  contact, review.json).
- Pyromancer gate run: `~/sonoranlab-work/phase3/gate-105/job/` (animate.json, gen/attempts, sweep, final/job with
  export and review.json); timestamps `t0..t7`; memory `mem.tsv`.
- Oathguard technical pass: `~/sonoranlab-work/phase3/2026-10-20/validate/sweep-oathguard-grey-default-2/`.
- Nothing was written into the Twos Are Up checkout. Moving a sheet into the game stays a manual, owner-approved step.

## Decisions made in this phase

- Grey `#808080` generation background is the default (ADR-0004), verified: the samurai NE facing failure did not
  recur on grey at the default loop window.
- Per-direction candidate selection, then full-set revalidation by the unchanged validator, is the selection rule.
- The pyromancer profile row uses the default limits, as the owner approved; no own-clip calibration.
- No threshold, report or validation policy was changed.

## Senior-review findings and dispositions

| finding | disposition |
|---|---|
| P1 stills lost from the animate export (#31) | fixed; pyromancer re-exported with 8 stills (gate §6) |
| P1 review not bound to the artifact (#30) | fixed; samurai and pyromancer approvals re-bound |
| P1 generation entry points not uniformly guarded (#28–#30) | fixed; per-track minimum budgets enforced |
| P2 prompt UUID not durable before submit (#26 → adapters) | fixed in #28/#29 (`submission.json`, fsync) |

## Remaining limitations and risks

1. **Thin margins:** samurai east is exactly at 16 / 16 (a valid pass with zero margin; prefer headroom in later sweeps). The oathguard set is not 1×-reviewed or Godot-checked.
2. **One timed run.** Pyromancer is an easier humanoid (small wand) under looser default limits; the 60-minute result
   is n = 1. Cleanup 0 is the owner's report; the review time is an upper bound from timestamps.
3. **Track C** is proven for one frame only; automatic fallback is not integrated (animate refuses track C with a message).
7. The samurai export predates the stills fix (column 0 empty); it is gate evidence, not a game-ready sheet.
4. **Godot retry (#27) masks a Godot exit-time race**; unrecovered failures ≈ p³ per step. `--editor --quit` is the
   untested alternative.
5. **CPU during GPU windows:** some agent test runs overlapped GPU batches (light synthetic fixtures; no incident).
   Production `sweep rank` runs were kept outside GPU windows.
6. The sRGB-chunk and in-game side-by-side checks from the Phase 2 handoff are still open before any sheet enters the
   game.

## Not started (out of scope)

Phase 4 (MCP server), UI, model training, Phase 6 attack animations.
