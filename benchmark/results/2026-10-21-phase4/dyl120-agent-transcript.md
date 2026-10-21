# DYL-120 acceptance-client report (fresh Claude Code session, sonoranlab MCP only)

Run date: 2026-10-21 (UTC). Client: Claude Code (Opus 5.5), driving the project-scoped `sonoranlab` stdio MCP server.
No repository files were modified, no owner-only commands were run, nothing was written to ~/Projects/TwosAreUp,
and no approval was claimed or recorded by this client. The reference-set approval was recorded out of band by the
owner's delegate; I could **not** observe it directly (see finding F1) and relied on `animate`'s own approval re-check.

## Summary table

| Step | What | Outcome |
|---|---|---|
| 1 | capabilities / list_projects / get_profile | OK. 26 tools; `editor: "available"`; ComfyUI `not_probed`; Godot at /home/dq/.local/bin/godot |
| 2 | list_characters | **FAILED**: `path_refused` "Path is outside the permitted roots" (F1) |
| 2 | register_character pyromancer (reference-v2, notes "DYL-120 gate") | OK. `ref_set_id` **refs-bd2b1deda344**, approval `unapproved`, evidence [], created=true |
| 3 | animate with unapproved refs | Correctly **refused**: `refused` "References must be approved and unchanged", field `ref_set_id`, hint "owner must run `sonoranlab registry approve-references`" |
| 3 | poll get_character for approval | **Could not verify**: get_character fails with `path_refused` (F1). Coordinator reported approval recorded (owner delegate); proceeded per coordinator instruction, relying on animate's re-check |
| 4 | animate (key `dyl120-gate-pyromancer-run-seed1`) | OK. **job-20261021T040422Z-1bafb051**, state queued; call ≤7.7 s |
| 4 | same request + same key | OK. **Same job_id** returned (state running); list_jobs shows exactly one job → no new work |
| 5 | in-flight polling | 4 job_status + 4 other read calls; all OK, sub-4 s; progress observed 0/5 → 2/5 (generate) |
| 6 | job_result | state **succeeded**; technical.validator **pass**; technical.godot **pass**; human review **none**, production_usable **false** ("awaiting owner review") |
| 6 | generation attempts | 5/5 ok, exit 0: north, north-east, east, south-east, south — all seed 1 |
| 6 | eight stills in export | Yes: animate.stills.directions = the full 8-direction `directions.order`, `exported_column0: true`; Godot checked 8 animations (run_<dir> for all 8) |
| 7 | godot_check on export | OK. Job **job-20261021T041832Z-6df29f96** succeeded; pass; 8 animations checked, 0 failures; 3 Godot attempts (version/import/check) all rc 0, no retries |
| 7 | contact_sheet zoom 8 | **Refused**: `refused` "Contact sheet exceeds 16384 px edge or 64 MiB decoded size" (F4). Retried at zoom 4 (deviation): OK, ref **job-20261021T041854Z-8a8286fe:contact.png**, 3470×3856 |
| 7 | check_review on the job | **FAILED**: `not_found` "Unknown imported artifact or job id" (F3). production_usable=false confirmed instead from job_result.human and job_status.human |

## Step details

### 1. Discovery
- Tools (26): capabilities, list_projects, get_profile, list_characters, get_character, register_character, import_job,
  get_artifact, check_review, list_jobs, job_status, job_result, cancel_job, retry_job, animate, pose_frame, validate,
  contact_sheet, compare, process, export_godot, godot_check, prepare_repair, inspect_repair, inspect_repair_diff,
  finalize_repair, abort_repair.
- Editor capability: `"available"`. ComfyUI container `sonoranlab-comfy`, status `not_probed`. Offline policy: local only, no cloud fallback.
- Project: `twos-are-up` (profiles/twos-are-up.yaml).
- Pyromancer cell: **144×144** (gen_scale 4, padding 2, rows_by_direction).
- directions.order: north, north-west, west, south-west, south, south-east, east, north-east.
- directions.generate: north, north-east, east, south-east, south.
- mirror_animations: north-west←north-east, west←east, south-west←south-east.
- run: 6 frames, 10 fps, loop true, loop_from null, hold_last false, track_default E, template `templates/poses/run` (absolute path inside the pinned checkout).
- Export target is marked `read_only: true, protected: true` (/home/dq/Projects/TwosAreUp).

### 2–3. Registration and approval gate
- ref_set_id `refs-bd2b1deda344`, approval `{"state":"unapproved","evidence":[],"problems":[]}`.
- Unapproved animate refused (exact error above). Approval gate works.
- get_character was called 2× (04:03:30, 04:05:22), both `path_refused`, so the approval transition was never observed through the API by this client.

### 4. Submission / idempotency
- First call 04:04:17.7 → returned by ≤04:04:25.4 (job created_at 04:04:23.46): `{"job_id":"job-20261021T040422Z-1bafb051","state":"queued"}`.
- Replay 04:04:25.4 → ≤04:04:31.98: `{"job_id":"job-20261021T040422Z-1bafb051","state":"running"}` — identical id.

### 5. In-flight interaction (no cancel issued)
| time (UTC) | call | state / stage | progress | latency |
|---|---|---|---|---|
| ~04:04:35 | job_status | running / generate | 0/5 (north-1 prompt unresolved) | <1 s |
| ~04:04:35 | list_jobs | running / generate | – | <1 s |
| ~04:06:40 | job_status | running / generate | 0/5 | <1 s |
| ~04:06:45 | capabilities | ok | – | <1 s |
| ~04:08:50 | list_jobs | running / generate | – | <1 s |
| 04:10:13.3 | job_status | running / generate | 2/5 (north-1, north-east-1 = result; east-1 unresolved) | ≤3.7 s |
| 04:10:17.0 | list_jobs | running / generate | – | ≤6.2 s (upper bound incl. client overhead) |
Server stayed responsive to reads throughout the GPU run. Polling paused 04:10–04:17 at the coordinator's request (it woke me at terminal).

### 6. Result
- state `succeeded`; job history: queued 04:04:23.46 → running 04:04:23.98 → succeeded 04:17:07.33. run_s 763.25, queue_wait 0.51 s.
- technical: validator **pass** (failures [], warnings []), godot **pass**.
- human: review `none`, production_usable **false**, message "awaiting owner review".
- Generation attempts (all `status: ok`, `exit: 0`, `error: null`):

| direction | seed | exit | gen_seconds |
|---|---|---|---|
| north | 1 | 0 | 179.19 |
| north-east | 1 | 0 | 128.71 |
| east | 1 | 0 | 128.51 |
| south-east | 1 | 0 | 122.10 |
| south | 1 | 0 | 119.67 |

  counts: planned 5, attempts 5, ok 5, failed 0, not_run 0, first_pass_ok_rate 1. Sweep: 5 candidates, qualified, technical_pass, every chosen candidate n_failures 0.
- Stage timings (animate.timing): prepare 0.083 s, generate 683.231 s, select_and_process 61.414 s, validate_export 13.664 s, total 758.404 s.
  (Job-level timing.per-stage instead reports prepare 3.27 s + generate 759.99 s — see F6.)
- Hashes present: workflow_source_sha256 `d3bad2cc…b78e` (same on all 5 attempts, matches job input_hashes.workflow), per-attempt
  workflow_patched_sha256, profile `9a3c6051…ec8e`, all 8 stills, attempt start_image and output frame hashes, and model hashes:
  Wan2.2-I2V-A14B High/LowNoise Q5_K_M gguf (`163e9e5a…`, `c5affcba…`), lightx2v 4-step LoRAs high/low (`d176c808…`, `024f21de…`),
  umt5_xxl_fp8 (`c3355d30…`), wan_2.1_vae (`2fc39d31…`). **BiRefNet-general hash = "unknown"** (F7).
- Export refs:
  - `job-20261021T040422Z-1bafb051:animate/sweep/final/job/export/pyromancer/pyromancer.png` (sha256 532188c6…074d)
  - `job-20261021T040422Z-1bafb051:animate/sweep/final/job/export/pyromancer/pyromancer.sonoranlab.json` (f79bcaf2…a8d6)
  - `job-20261021T040422Z-1bafb051:animate/sweep/final/job/export/pyromancer/pyromancer_frames.tres` (98bf0f45…b5c1)
  - report `…:animate/sweep/final/job/report.json`, in-job contact `…:animate/sweep/final/job/contact.png`
- Five generate directions confirmed from generation.requests and the 5 resolved prompts. Eight stills: animate.stills.directions lists all
  8 in directions.order order with exported_column0 true, and the Godot check iterated run_<dir> for all 8 directions → sheet rows = directions.order.
  I could not inspect the PNG pixels/sidecar body directly (get_artifact refused the refs, F2), so row order is confirmed from result metadata, not pixels.

### 7. Godot check, contact sheet, review
- godot_check source `job-20261021T040422Z-1bafb051:animate/sweep/final/job/export/pyromancer`, key `dyl120-gate-pyromancer-run-godot-check`
  → job-20261021T041832Z-6df29f96 (call ≤9.5 s), succeeded 04:18:42.68 (run_s 9.40). Godot 4.7.2.stable. pass true, animations_checked 8,
  failures []. Attempts: version rc0 0.027 s, import rc0 2.741 s, check rc0 0.18 s; no transient signatures/retries. Checked hashes match the export.
- contact_sheet: zoom 8 refused (verbatim above); zoom 4 succeeded → `job-20261021T041854Z-8a8286fe:contact.png` (3470×3856), frames
  `…:animate/sweep/final/job/processed/05_registered`, report `…:animate/sweep/final/job/report.json`.
- check_review `job-20261021T040422Z-1bafb051` → `not_found` (F3). From job_result/job_status: review `none`, **production_usable false, awaiting owner review**.

## Findings (things that failed or I could not verify)
- **F1 list_characters / get_character fail the whole call**: `{"code":"path_refused","message":"Path is outside the permitted roots","field":null,"retryable":false,"hint":null}`.
  Coordinator attributes it to an earlier demo-imported Phase 3 job whose profile is outside the default READ roots; one bad imported job
  should be reported as a per-job problem, not fail the tool. Consequence: the approval transition (step 3 polling) was not observable.
- **F2 get_artifact rejects refs that job_result returns**: `not_found` "Unknown imported artifact or job id" for
  `job-…:animate/sweep/final/job/export/pyromancer/pyromancer.png`, for `…sonoranlab.json`, and for the bare job id; the STATE path form gives
  `invalid_params` "Expected a job id or imported artifact id". Seems limited to imported artifacts only.
- **F3 check_review rejects a native job id**: `not_found` "Unknown imported artifact or job id" for job-20261021T040422Z-1bafb051, despite its schema accepting `job-…` ids.
- **F4 contact_sheet default zoom 8 always exceeds the cap for this sheet**: refused at zoom 8 (default); zoom 4 is 3470×3856, so zoom 8 ≈ 6940×7712 RGBA ≈ 214 MB > 64 MiB.
  The default is unusable for a 144 px, 8-row run sheet.
- **F5 Response size**: job_result is ~505 k chars (1,819 artifact refs) and terminal job_status ~51 k chars; both exceed the client's tool-output limit
  and had to be read from saved files with jq. An agent without file access could not consume them.
- **F6 Inconsistent terminal stage/timing**: terminal job_status reports `stage: "generate"` while `progress.stage` is `validate_export`; job-level per-stage
  timing lumps processing/validation/export into `generate` (759.99 s) vs animate.timing's breakdown. Also animate.json/verdict/export fields
  carry absolute host paths rather than STATE-relative refs.
- **F7** BiRefNet-general model hash recorded as `"unknown"`.
- Latencies are client-side upper bounds from shell timestamps between calls (resolution ~seconds), not server-measured.

## Full call log (UTC, 2026-10-21)
| # | time | tool | key params | result | wall time |
|---|---|---|---|---|---|
| 1 | 04:03:15 | capabilities | – | ok | <1 s (parallel batch) |
| 2 | 04:03:15 | list_projects | – | ok (twos-are-up) | <1 s |
| 3 | 04:03:15 | get_profile | twos-are-up | ok | <1 s |
| 4 | ~04:03:20 | list_characters | twos-are-up | ERROR path_refused | <1 s |
| 5 | 04:03:23.5 | register_character | pyromancer, reference-v2, notes "DYL-120 gate" | ok refs-bd2b1deda344 unapproved | ≤6.5 s |
| 6 | 04:03:30.1 | animate | refs-bd2b1deda344, run, seeds [1], no key | ERROR refused (unapproved refs) | ≤4.8 s |
| 7 | 04:03:30.1 | get_character | pyromancer | ERROR path_refused | ≤4.8 s |
| – | ~04:04:05 | (coordinator msg: approval recorded by owner delegate; submit animate) | | | |
| 8 | 04:04:17.7 | animate | key dyl120-gate-pyromancer-run-seed1 | ok job-20261021T040422Z-1bafb051 queued | ≤7.7 s |
| 9 | 04:04:25.4 | animate (replay) | same key | ok same job_id, running | ≤6.5 s |
| 10 | ~04:04:35 | job_status | main job | ok running/generate 0/5 | <1 s |
| 11 | ~04:04:35 | list_jobs | twos-are-up, limit 5 | ok (1 job) | <1 s |
| 12 | ~04:05:22 | get_character | pyromancer | ERROR path_refused | <1 s |
| 13 | ~04:06:40 | job_status | main job | ok running/generate 0/5 | <1 s |
| 14 | ~04:06:45 | capabilities | – | ok | <1 s |
| 15 | ~04:08:50 | list_jobs | twos-are-up | ok running/generate | <1 s |
| 16 | 04:10:13.3 | job_status | main job | ok running/generate 2/5 | ≤3.7 s |
| 17 | 04:10:17.0 | list_jobs | twos-are-up, limit 5 | ok running/generate | ≤6.2 s |
| – | ~04:17:30 | (coordinator msg: job terminal) | | | |
| 18 | 04:17:34.3 | job_result | main job | ok succeeded (505 k chars, read via file) | ≤6.6 s |
| 19 | ~04:18:00 | get_artifact | …/export/pyromancer/pyromancer.png | ERROR not_found | <1 s |
| 20 | ~04:18:00 | get_artifact | …/export/pyromancer/pyromancer.sonoranlab.json | ERROR not_found | <1 s |
| 21 | ~04:18:10 | get_artifact | STATE path form of png | ERROR invalid_params | <1 s |
| 22 | ~04:18:15 | get_artifact | job-20261021T040422Z-1bafb051 | ERROR not_found | <1 s |
| 23 | 04:18:25.0 | godot_check | source …/export/pyromancer, key dyl120-gate-pyromancer-run-godot-check | ok job-20261021T041832Z-6df29f96 queued | ≤9.5 s |
| 24 | ~04:18:40 | contact_sheet | frames 05_registered, report.json, zoom 8 | ERROR refused (size cap) | <2 s |
| 25 | ~04:18:50 | contact_sheet | same, zoom 4 | ok job-20261021T041854Z-8a8286fe:contact.png 3470×3856 | <5 s |
| 26 | ~04:19:00 | check_review | job-20261021T040422Z-1bafb051 | ERROR not_found | <1 s |
| 27 | ~04:19:00 | job_status | godot_check job | ok succeeded, godot pass | <1 s |
| 28 | ~04:19:03 | job_status | main job | ok succeeded (51 k chars, read via file) | <3 s |
