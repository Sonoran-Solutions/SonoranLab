# Phase 2 senior-review handoff (2026-10-19)

**Recommendation: GO for Phase 3, conditional.** The Phase 2 exit criterion is met on the Phase 1 data. Phase 3 can
start once the owner has (1) reviewed and merged the PR stack below and (2) watched the exported relay_sentinel
SpriteFrames play at 1× (a human check; the validator does not judge art). The open items below are Phase 3 work.
They are not Phase 2 defects.

## Exit criterion

> One CLI command takes an existing Phase 1 raw animation, processes it deterministically, validates it and exports a
> playable Godot SpriteFrames resource. Invalid assets produce actionable failures, never a misleading PASS. All
> inputs preserved; reproducible.

| requirement | evidence (`phase2-gate.md`) | status |
|---|---|---|
| one command | `sonoranlab process RAW --profile … --character relay_sentinel --anim idle --out JOB --godot-check` | met |
| deterministic | 4 reruns plus an orchestrator rerun on the committed tree give identical stage, report, sheet and .tres hashes | met |
| validates | ADR-0008 validator; the registered frames of all 12 Phase 1 sets are byte-identical to Phase 1's (processing-v1 parity) | met |
| playable SpriteFrames | Godot 4.7.2 import clean; the checker passes 8 animations (5 fps, loop, regions + pixel hashes); renders crisp at 1×/4× | met |
| invalid → actionable, no PASS | 8 humanoid sets exit 1, the report names frame/direction/value/limit plus diagnostics, and no `export/` dir is written | met |
| inputs preserved | raw files byte-copied read-only into `JOB/raw/`; all 340 Phase 1 raw hashes unchanged; `verify_raw` clean on every job | met |
| offline | identical outputs under `unshare -rn`; no ComfyUI, no GPU (CPU torch only inside PixelOE) | met |

## PRs (none merged; owner approval required)

| PR | ticket | content | base |
|---|---|---|---|
| #15 | Phase 1 cleanup | score.py counts only bound reviews (+ regression); Qwen budget docs 64/60/40/30 + 8 GiB margin | main |
| #17 | DYL-83 policy | validator study + ADR-0008 (**accepted by the owner** 2026-10-19: H0 + H4 + diagnostics, F2) | main |
| #16 | DYL-79 | package, uv.lock, strict profile schema, `profile check/show`, CI `package` job | main |
| #18 | DYL-80 | PixelOE wrapper (bit-identical to Phase 1, deterministic across thread counts) | #16 |
| #20 | DYL-81 | alpha / despill / fringe / palette snap / colour cap | #16 |
| #19 | DYL-82 | slice/pack (game layout, profile-only mirroring), per-clip foot/rig registration | #16 |
| #23 | DYL-83 | structured validator, ADR-0008 semantics, per-failure diagnostics | #16 |
| #21 | DYL-84 | contact sheets (runtime-border replica), immutable job dirs, manifests | #16 |
| #22 | DYL-85 | Godot SpriteFrames + game-layout PNG + characters.json fragment; headless checker | #16 |
| #24 | DYL-86 gate | `sonoranlab process`, gate evidence, cross-module review fixes; merges #18–#23 | #16 |

Suggested merge order: #15, #17 → #16 → #18, #20, #19, #23, #21, #22 into #16's branch → #24 → #16 to main. Or merge
#24 into #16's branch and #16 into main, which is the same tree. The review fixes (commit `cca758c`) live only in #24:
the individual ticket PRs do not carry them.

## Test evidence

- `uv run pytest tests/sonoranlab`: 401 passed locally (Godot and real-data tests ran). In CI the Godot/real-data
  tests skip with stated reasons. The pre-existing ops/benchmark suite: 134 passed.
- `ruff format --check` and `ruff check`: clean. CI is green on #15–#23. #24's run was in progress when this was
  written.
- An independent Codex review of the integrated tree found 8 issues. Fixed: 7 (slice path traversal, NaN/null
  thresholds, dropped character overrides, palette-PNG transparency, an unguarded standalone export, standalone
  validate without registration provenance, rig not flagged). The eighth, rig anchors having no validator rule, is
  now a profile error.

## Validation outcome on Phase 1 data (ADR-0008)

- **Humanoids (samurai 1–4, oathguard 1–4): all FAIL.** Their failures are unchanged from Phase 1 (bbox_drift,
  foot_slide). The study found content causes: NE drawn as a side view, oathguard's east swing, and the katana tip
  popping. The validator's diagnostics now name the moved edge and the core-silhouette change, so a weapon tip can
  be told apart from body motion. No threshold was loosened.
- **relay_sentinel 1–4: PASS with warnings.** F2 removed the tassel-tip "foot_slide". Hover warnings flag a low bob
  in every set and east x-drift in sets 1 and 4. Loop-closure-ratio warnings: 3–4 per set.

## Open items and known limitations

1. **ADR-0004 item 1 is still open:** no humanoid set passes. Phase 3's E adapter must produce one (weapon-retention
   seed selection, C-posed first frame, NE facing lock), or the owner records another decision. Cleanup time is
   still unmeasured (ADR-0004 item 4).
2. **The game does not consume SpriteFrames.** It uses `characters.json` + `PixelStandee`. The export writes a
   game-layout palette PNG and a `characters_json_entry` fragment next to the .tres, and never writes into the game.
   Dropping a sheet into the game is a manual, owner-approved step. `standing_pixels` and `height_m` are the game's to
   set.
3. **SpriteFrames loses features:** `loop_from` (channel) cannot be represented and is recorded in the sidecar only.
   The sticker border exists only in the game's shader, so a plain SpriteFrames shows no border; contact sheets render
   the border for review.
4. **The PNG omits the sRGB chunk** that the game's sheets carry. The pixels round-trip exactly, but an import of
   the two side by side in the game was not compared. Check this before the first sheet goes into the game.
5. **`rig` anchor mode** is refused end to end until Track D per-frame anchors and a rig validator rule exist (Phase 6).
6. **`--sheet` input** (a raw sheet in the game layout) is covered only by synthetic tests. All real Phase 1 E data is
   per-frame. PLAN.md's "768² sheet" wording dates from before ADR-0007.
7. **CI has no Godot.** Engine checks run locally (`tools/godot/spriteframes_check`). A Godot CI job would be a cheap
   follow-up.
8. **Calibration is thin for warnings:** loop-closure-ratio warnings fire on most generated loops, and shipped clips
   sit close to the limit. They are warnings only, but are worth recalibrating when more shipped clips are measured.

## Not done (out of scope)

Phase 3 adapters, the MCP server, any UI, model training, and GPU work. None was started.
