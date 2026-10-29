# DYL-131 / DYL-133 — smoke renders, delivery bundles and the game-consumer check (2026-10-28 UTC)

Code: live checkout `~/src/SonoranLab-p5live` at the integration heads named per run. Game access (owner, verbatim):
"Yes, read + snapshot". The production checkout `~/Projects/TwosAreUp` was only read (`git show`, `git clone`); it
was never written. Its HEAD at the end of the run is `fb712e7` (moved on by the owner's own work), not the bundles'
target `dd4b2c1`.

## Staged inputs (private, under STATE)

`STATE/staged/game-dd4b2c1/characters.json` sha256 `a30a1db6…` and `pyromancer.png.import` `3daa48bc…`, both from
`git show dd4b2c14381050ac069950e6b597f2ba16615069:assets/pixel/v0/characters/…`, equal to the earlier staged copy's
SHA256SUMS. Sibling sheets for smoke renders: pyromancer (Phase 3 export, `import-0482cf46…`) and thief.

## Smoke renders (isolated Godot 4.7.2 project, headless GNOME Shell + llvmpipe)

| Class | Job | Result |
|---|---|---|
| thief (1st) | `job-20261028T173115Z-bc71b6aa` | FAILED: compositor could not bind its Wayland socket — the runtime dir under the job dir made the path exceed sun_path's 108 bytes. Preserved. Fixed in `6ca2858` (short private runtime dir). |
| thief | `job-20261028T185028Z-8405d022` | 10/10 checks PASS, 50/50 renders |
| gun | `job-20261028T192649Z-e0a79a81` | 9/10; `nearest_filtering` FAIL: 51 px, blue 7→0 for two dark colours (tolerance 6), no colour split into several (no blending). Threshold unchanged. |
| engineer | `job-20261028T212948Z-cbb96713` | 10/10 PASS (max dark deviation 6) |

`agent_recommendation` and `owner_style_acceptance` stay empty in every smoke.json; the owner's style acceptance is
the 1x review of the composed sheet.

## Bundles

| Class | Bundle job | Files (sha256 after) | Manifest entry | State |
|---|---|---|---|---|
| gun (1st) | `job-20261028T193403Z-5218663d` | gun.png `914dabb9…`, .import `28802aa9…` (uid `bdoggxzlhctho`) | 1.80 m, 112 px, cell 160 | BUILT — consumer FAIL: Godot rewrote the .import uid (`z`→`0`). Preserved. |
| gun | `job-20261028T210909Z-714085bf` | gun.png `914dabb9…`, .import uid `bdoggx0lhctho`, characters.json `4555e1c7…` | 1.80 m, 112 px | **READY_TO_INSTALL** |
| thief | `job-20261028T193409Z-03a9f3c1` | thief.png `4d12372a…`, .import `6acf1332…`, characters.json `8772fa4d…` | 1.75 m, 108 px, cell 144 | **READY_TO_INSTALL** |
| engineer (1st) | `job-20261028T213344Z-44f7eb10` | — | — | refused at build: 1.78 m × 62 = 110.36 px vs south still 108 px |
| engineer | `job-20261028T215606Z-51047b63` | engineer.png `59466741…` | 1.74 m (owner), 108 px, cell 144 | **READY_TO_INSTALL** |

All three: `known_missing_clips` cast (PixelStandee selects it while casting or shooting, no fallback) and death
(`--pixel-check` requires it); `playable_class: false`. Each bundle appends one row to the same base manifest, so
installing more than one requires rebuilding the later ones on top of the earlier (stale-base refusal is by design).

## Game-consumer runs (snapshot = local clone of the game at `dd4b2c1`, origin remote removed)

Each run: `deliver apply` → `tools/game_consumer/run.sh --test-class SamuraiPresentationTests` on a private Xephyr
`:95` → `deliver verify --consumer-result` → `deliver rollback`, under the orchestrator's host heavy-job lock
(`ORCH_HEAVY=1`, 3600 s deadline, bounded output).

| Attempt | Outcome |
|---|---|
| gun 1 | refused: host lock held by another project's Godot campaign (waited 600 s); nothing applied |
| gun 2, thief 1 | harness exit 2: `df -P --output` rejected by GNU df, so "less than 20 GiB free" (151 GB free). Fixed `d4f41b8`. Apply and rollback clean. |
| gun 3, thief 2 | harness exit 2: `dotnet` not on the systemd unit's PATH (wrapper env, not product code). |
| gun 4 | consumer FAIL: engine rewrote gun.png.import (uid alphabet). Fixed `1ef6c2d` (Godot alphabet a–y, 0–8; the game's 119 uids contain no `z`/`9`); bundle rebuilt. |
| thief 3 | **PASS**: dotnet test 3/3, Debug build, import, pixel_check `known_missing_clip`, gallery 65 captures; verify → READY_TO_INSTALL; rollback clean |
| gun 5 | **PASS** (same steps, no engine rewrite); verify → READY_TO_INSTALL; rollback clean |
| engineer 1 | **PASS** (same steps); verify → READY_TO_INSTALL; rollback clean |

After every rollback `git status` of the snapshot was empty and HEAD `dd4b2c1`. The gallery uses PixelGallery's fixed
Layout: it proves the patched manifest keeps the real gallery working, not that the new ids are drawn there.
`fallback_render_non_consumer` was not run.

INSTALLED is never set by any command. Installation needs the owner's separate approval per bundle and target
revision; the target must first be rebuilt for the game's current HEAD.
