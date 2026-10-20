# Phase 3 gate evidence (2026-10-20)

Text and metrics only (public repo). Art, clips, reports and review material are in `~/sonoranlab-work/phase3/`.
Validator thresholds were not changed for any run below. Every GPU run used the offline container
(`env/comfy.sh up --offline`, `--network=none`), reached by `sonoranlab generate --exec sonoranlab-comfy`, one at a
time under `env/runwindow.py --need-gib 40` (no `--gtt-credit`, nothing paused).

## 1. Client, guard and cancellation (DYL-98 / DYL-99)

| check | result |
|---|---|
| first live Track E clip (samurai NE s1, grey) | 33/33 frames, exec 214.6 s, peak drawn 23.9 GiB (budget 40) |
| live cancel: SIGTERM to the run window mid-generation | client sent the prompt-scoped `/api/jobs/{id}/cancel` (server log "Cancelling running prompt <id>", "Processing interrupted"); queue empty and unit gone 5 s after the signal; GTT 13.8 → 0.8 GiB; result recorded `interrupted` with the prompt id |
| generations today | 29/29 first-pass ok (19 benchmark + 10 gate); per clip 169–216 s wall, peak drawn ≤ 23.9 GiB |

## 2. Humanoid validator blocker (DYL-103, ADR-0004 open item 1)

Evidence path: Phase 1 failures are **per direction** (each direction is its own clip). Samurai failed only NE in all 4
Phase 1 seeds (foot offset, the side-view facing drift); oathguard only E. A CPU-only NE loop-window sweep on the Phase 1
clips showed the facing drifts later in a clip (starts 1–5 pass 9/12; starts 9–13 fail 9/12). On the grey background the
NE drift went away for seed 1 at the selector's default window (foot 21.5/30, drift 11/16).

**Result on an existing approved reference (samurai, limits 30 / 16, unchanged):** an all-grey Phase 3 set chosen by
`sonoranlab sweep rank` (default loop windows) from 11 attempts passes the validator with no failures:

| direction | seed | loop start | worst gate ratio |
|---|---|---|---|
| north | 1 | 24 | 0.56 |
| north-east | 1 | 22 | 0.72 |
| east | 4 | 16 | 1.00 (bbox drift 16 / 16) |
| south-east | 2 | 16 | 0.81 |
| south | 1 | 19 | 0.19 |

Godot 4.7.2 import + SpriteFrames check: pass (8 animations). Owner 1× review: **pass** ("I approve the samurai grey run
set for use"), recorded in the job's `review.json`; no manual edits. Rejected candidates are kept in `sweep.json` with
reasons; the diagnostics attribute east s1/s3 and south-east s1/s3 to the thin katana edge (core ≤ limit) and east s2
to body drift.

Oathguard (limits 20 / 8): east on grey passes in seeds 3 and 4 (Phase 1 magenta: 0/4). North-east and south-east
seed results: see §4.

## 3. Timed production run on a new approved reference (DYL-105)

Reference: **pyromancer**, owner-approved for this use 2026-10-20 ("use pyromancer … under the default validator limits
(30/25), with its shipped stills approved for this use like the Phase 1 refs"). Column-0 stills from the shipped sheet
(game rev ee53160), cell 144, run 6 frames at 10 fps. Profile row = the `validator:` default row (30 / 25); no own-clip
calibration.

Command (repo `078321d`): `sonoranlab animate --profile profiles/twos-are-up.yaml --character pyromancer --reference REF
--anim run --track E --seeds 1,2 --out JOB --exec sonoranlab-comfy --need-gib 40 --godot-check` → exit 0.

| stage (measured) | minutes |
|---|---|
| reference + pose/profile preparation (operator) | 0.87 |
| model generation: 10 clips, 0 failed, 0 retries (GPU exec 24.54) | 24.88 |
| frame extraction, loop selection, per-direction processing + ranking | 1.94 |
| validation + export + Godot import/playback check | 0.24 |
| review material (GIFs, contact sheet) | 0.59 |
| human 1× review | ≤ 0.72 (see note) |
| human cleanup | 0 (owner: "no cleanup needed … cleanup 0 minutes, no manual fixes") |
| **total effort, prep start → owner approval** | **29.28** |
| GPU queue wait behind an unrelated benchmark batch (not effort) | 10.69 |
| **total wall clock including that wait** | **39.97** |

| metric | value |
|---|---|
| first-pass generation success | 10/10 |
| candidates passing their own direction | 10/10 (seed 1 alone would have formed a passing set) |
| final validator verdict | PASS, 0 failures, 0 warnings |
| Godot 4.7.2 import + playback | pass, 8 animations, run loops at 10 fps |
| owner 1× review | pass; `production_usable()` true for the final job |
| peak memory drawn / peak GTT | 18.8 / 17.3 GiB (start MemAvailable 74.2 GiB) |
| manual fixes | none |
| offline | inference in the `--network=none` container; the deterministic stage re-run under `unshare -rn` gives byte-identical export files and an identical report |
| other services | nothing paused; `strix-ai` container and user `ollama` untouched; `llama-gemory` left disabled as the owner set it |

Note on review time: the owner's stopwatch figure for the review was not filled in (sent as "N"). The table uses the
measured window from sending the material to the owner's reply, an upper bound.

**60-minute character-#1 criterion: PASS.** 29.3 minutes of effort, 40.0 minutes of wall clock even counting the
unrelated GPU wait; cleanup is the owner's own report of none. The Phase 1 waiver was not used.

## 4. Oathguard follow-up (second humanoid, technical only)

`sonoranlab sweep rank` over 14 grey oathguard attempts (limits 20 / 8, unchanged) finds a full set that passes the
validator with no failures: north s1 (0.75), north-east s2 (0.50), east s4 (0.75), south-east s4 (0.50), south s1
(0.50) (worst gate ratio in brackets). Rejections: east s1/s2 body drift (the exaggerated swing), north-east s1/s3/s4
foot offset (core), south-east s1/s2 thin shield-edge drift. Not yet reviewed at 1× and not Godot-checked; it is a
second technical pass, not part of the gate. `~/sonoranlab-work/phase3/2026-10-20/validate/sweep-oathguard-grey-default-2/`.

## 5. Track C live check (DYL-100)

One posed first frame through the same adapter contract: `sonoranlab generate --exec sonoranlab-comfy` with a Track C
request (samurai east still as image2, Track D bake skeleton `f7/g736/pose/east/000.png` as image1, 1024 conditioning,
560 output, grey, seed 1), guarded at `--need-gib 60`, nothing paused. Status ok: exec 164.8 s, peak drawn 48.5 GiB
(min MemAvailable 25.9 GiB), 1 RGB + 1 RGBA output, queue drained. Visual check (orchestrator): the run pose is
followed, katana present, identity held. `prep.start_image_from_rgba` turns it into a 560² grey Track E start image.
It was not needed for either humanoid pass, so no clip was generated from it.

## Limitations

- One new character, one run; the pass rate across characters is not established. Pyromancer is an easier humanoid
  than samurai (small wand, no long blade) and runs under the looser default limits the owner approved.
- Samurai's chosen east clip sits exactly at its 16 px drift limit.
- Track C has one live frame (§5), no animated use; held poses beyond that are Phase 6.
- Review time is an upper bound from timestamps, not a stopwatch reading.
