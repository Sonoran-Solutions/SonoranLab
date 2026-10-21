# Phase 4 gate (DYL-120): agent-driven local generation, recovery and protected approval

Date: 2026-10-21 (UTC). Orchestrator run `20261020T234527Z-9b3494`. Baseline `main` 0f54c33.
**Status: READY_FOR_SENIOR_REVIEW.** Nothing is merged. Phase 5 has not started.

Labels: **IMPLEMENTED** (code exists) · **TESTED WITH FAKES** (fake ComfyUI/Godot/prober/editor) ·
**VERIFIED LOCALLY** (real host: GPU, ComfyUI, Godot) · **OWNER-APPROVED** · **NOT_RUN** · **DEFERRED**.

## Trees

- Live gate run: pinned detached checkout `96724ed`. Its tool behaviour equals the final head for generation, jobs,
  guard and export.
- The live run found 7 usability defects (below). They were fixed afterwards in `16da981` and `e2f69b7`, with tests.
  The fixes change reporting and tool shapes only (artifact refs, review lookup, contact zoom, result size, stage). They
  do not touch generation, the guard, the validator or export admission.
- Final tested head: **`e2f69b7`**, tree `b17678f`. The DO-NOT-MERGE combined check (#49) has the same tree. The
  post-fix tools were re-checked against the live gate artifacts (section 9).

## 1. Fresh Claude Code agent session (transcript: `dyl120-agent-transcript.md`)

- **Client:** a fresh Claude Code session (Opus 5.5) started in the pinned checkout. It loaded the project-scoped
  `.mcp.json` server `sonoranlab` and used only MCP tools. The owner authorized a Claude agent as this test client.
- **Script:** `~/sonoranlab-work/phase4/gate/acceptance-prompt.md`.

| Scenario step | Result | Label |
|---|---|---|
| Discover capabilities (26 tools, editor available, ComfyUI `not_probed`) | OK | VERIFIED LOCALLY |
| Inspect the Twos Are Up profile (pyromancer cell 144; 8-row order; 5 generated plus 3 mirrored; run 6 f @ 10 fps) | OK | VERIFIED LOCALLY |
| Register pyromancer references (`refs-bd2b1deda344`) | OK; state `unapproved` | VERIFIED LOCALLY |
| Animate with unapproved references | Refused (`refused`, owner hint) | VERIFIED LOCALLY |
| Owner approval of the reference set | Recorded by the coordinator as the owner's delegate ("Pyromancer, I authorize you"). Copies verified byte-identical to the Phase 3 approved `reference-v2` | OWNER-APPROVED |
| Submit bounded Track E job (seed 1) → durable id `job-20261021T040422Z-1bafb051` | OK, about 7 s including client overhead | VERIFIED LOCALLY |
| Same idempotency key again | Same job id, no new work | VERIFIED LOCALLY |
| Interact while generation runs (8 reads) | All OK. Server-side latency under load: `job_status` 0.33–0.36 s, `list_jobs` 0.31 s, `capabilities` 0.007 s (coordinator SDK client, load average 21). The agent's own measurements were up to 6.2 s; these are client-side upper bounds | VERIFIED LOCALLY |
| Poll to completion; retrieve artifacts | `succeeded` | VERIFIED LOCALLY |
| Full validator result | **PASS**, 30 frames, 0 failures, 0 warnings, 0 off-palette, 0 semi-alpha | VERIFIED LOCALLY |
| Directional layout and stills | 5 generated directions; mirrored rows; all 8 stills in column 0; sheet 1008×1152 (7 × 144 by 8 × 144) | VERIFIED LOCALLY |
| Godot import/playback check (separate `godot_check` job) | **PASS**, Godot 4.7.2, 8 animations, 0 retries | VERIFIED LOCALLY |
| Inspect at 1× | Local review page (new export beside the Phase 3 approved export) | — |
| Owner 1× review | **PASS**, cleanup 0 min (owner: "pass at 1×, 0 cleanup, you may record it"). Recorded with `sonoranlab review record` as schema `review/2`, bound to the final job. The owner did not time the review; the interval is a timestamp upper bound (page served 04:22:21Z → reply) | OWNER-APPROVED |
| Production usability without changing files | `job_status`: `review: pass, production_usable: true`. Export hashes unchanged: png `532188c6…`, sidecar `f79bcaf2…`, tres `98bf0f45…` | VERIFIED LOCALLY |

Per-stage timing (`animate.json`):

| Stage | Seconds |
|---|---|
| prepare | 0.08 |
| generate | 683.2 |
| select_and_process | 61.4 |
| validate_export | 13.7 |
| **total** | **758.4** (12.6 min) |

- Queue wait was 0.5 s.
- Per-direction generation: north 179 s, north-east 129 s, east 129 s, south-east 122 s, south 120 s.
- All 5 directions passed on the first attempt.
- Margins against limits:
  - largest bbox drift: east 21 (limit 25);
  - largest foot slide: east 21.5 (limit 30).
- This is a single-run observation, not a guarantee across characters. Phase 3's single-run timing caveat still
  applies, as does the samurai zero-margin note.

Provenance recorded per attempt:
- workflow source sha256 `d3bad2cc…b78e`, plus per-attempt patched hashes;
- profile `9a3c6051…`;
- all 8 still hashes;
- models: Wan2.2 I2V A14B Q5_K_M high and low (`163e9e5a…`, `c5affcba…`), lightx2v 4-step LoRAs (`d176c808…`,
  `024f21de…`), umt5_xxl_fp8 (`c3355d30…`), wan_2.1_vae (`2fc39d31…`);
- **BiRefNet-general hash: `unknown`.** This is a pre-existing Phase 3 adapter limitation, not fixed here.

## 2. Resources (host sampler, 887 × 5 s, `~/sonoranlab-work/phase4/gate/resources.tsv`)

- Track E guard: 40 GiB plus 8 margin. It was admitted with **no service paused**; `llama-gemory` and host ComfyUI
  were inactive, and the owner's `strix-ai` container was untouched.
- Minimum MemAvailable: 43.9 GiB, starting from about 60. Peak GTT: 34.1 GiB, from a baseline of about 17.
- Per-attempt `memory` is null: the adapter records none, and it is never reported as 0.
- **ComfyUI CPU (DYL-112, whitelist):**
  - median **0.2%** with an empty queue, n = 757 samples across about 70 min;
  - median 213% / p90 1746% while a prompt runs.
  - The ~375% idle CPU originally reported is therefore resolved for idle. Spikes during load are inference.

## 3. Restart and recovery (disposable server, real job)

- A coordinator SDK client connected to its own server and read the running job (`running/generate 0/5`).
- That server process was then **SIGKILLed**. Only the client's own descendants were killed; the agent's server was
  untouched.
- A new server immediately saw the same job still `running`. The job's systemd unit stayed active, and it completed
  normally. There was no resubmission (5 submissions for 5 requests).
- Client disconnects (each SDK run ends its server) never affected the job.
- Crash at each boundary, lost replies and blocked reconciliation: TESTED WITH FAKES (jobs, regression and
  integration suites).

## 4. Prompt-scoped cancellation (disposable job)

- Disposable `animate` job `job-20261021T042044Z-d833363f` (pyromancer idle, seed 2). `cancel_job` was called while
  its own prompt `fa14ab2b…` was running in ComfyUI.
- `cancelling` at 04:20:54, then `cancelled` with outcome **confirmed** at 04:20:57.
- ComfyUI history shows `execution_interrupted` for exactly that prompt, and the queue was empty afterwards.
- The runwindow lease was removed and the job unit became inactive.
- No foreign prompt existed live. Foreign-prompt protection is TESTED WITH FAKES (manager, generation and integration
  tests).

## 5. Core without Aseprite / optional repair

- With the editor runtime absent, the 5 repair tools are not advertised, calling one returns `Unknown or disabled
  tool`, and the core tool list is identical: TESTED WITH FAKES (integration and stdio tests).
- On this host the runtime is present (`editor: available`), so the agent saw 26 tools.
- Live repair handoff: **NOT_RUN** in Phase 4. Phase 3.5's A1/A2/B1/B2/C1 trials remain the live editor evidence. The
  MCP lifecycle (prepare → diff → finalize → fresh review) is TESTED WITH FAKES.

## 6. Offline

- ComfyUI ran in the `--network=none` container through `podman exec`; the MCP server, workers, deterministic tools
  and Godot are local. There were no downloads.
- The stdio offline test (AF_INET/AF_INET6 connect blocked) passes for reads and jobs: TESTED WITH FAKES.
- The hosted agent (Claude Code) needs network for its model API. That is the orchestrator, not SonoranLab inference.

## 7. Track C

Live Track C `pose_frame` smoke: **NOT_RUN**. Its budget needs 68 GiB available (60 + 8) and the host had about
60 GiB. Reaching it would have required pausing an owner service, which was not authorized for this gate. Track C
was verified live in Phase 3 (1 frame at 48.5 GiB). The MCP `pose_frame` path, including its 60 GiB guard argv, is
TESTED WITH FAKES.

## 8. Defects found by the live run (all fixed after the gate, with regression tests)

| # | Finding | Fix |
|---|---|---|
| F1 | One imported job outside the default roots made `get_character` and `list_characters` fail entirely (caused by a coordinator import demo) | `16da981`: per-job isolation |
| F2 | `get_artifact` rejected native `job:path` refs | `e2f69b7` |
| F3 | `check_review` rejected native job ids | `e2f69b7` (resolves animate's final job) |
| F4 | Default contact zoom 8 always exceeded the size cap | `e2f69b7` (largest fitting zoom) |
| F5 | `job_result` was ~505k characters (1,819 refs) | `e2f69b7` (≤200 prioritised refs plus paging; compact status) |
| F6 | Stale top-level stage and timing lumped into generate | `e2f69b7` |
| F7 | BiRefNet model hash `unknown` | Not fixed: Phase 3 adapter limitation, recorded |

Also found in the dry run before the gate: `96724ed`, relative `../templates` profile paths were refused by the new
confinement.

## 9. Post-fix re-check against the live gate artifacts (head `e2f69b7`)

See `phase4-handoff.md` §9 for the call log: `check_review(job id)`, `get_artifact(native ref)`, compact `job_result`,
and default-zoom `contact_sheet` on the gate job.

## 10. Exit criteria

| Criterion | Status |
|---|---|
| MCP initializes and advertises truthful capabilities | VERIFIED LOCALLY (fresh agent) |
| CLI/MCP contract parity | TESTED WITH FAKES (parity tests per tool, stdio integration) |
| Jobs survive client/server failures without duplicate inference | VERIFIED LOCALLY (server SIGKILL, same key) plus TESTED WITH FAKES |
| Guarded scheduling and scoped cancellation | VERIFIED LOCALLY (guard admission, live cancel) plus TESTED WITH FAKES (overlap, foreign prompts) |
| Registry/artifacts keep hashes and approval state | VERIFIED LOCALLY (approval bound to hashes; unchanged export hashes) |
| Deterministic validation and Godot export authoritative | VERIFIED LOCALLY (validator plus Godot PASS); export re-validates pixels |
| A real agent completes generation-to-review | VERIFIED LOCALLY + OWNER-APPROVED (1× pass) |
| Core works with optional dependencies absent | TESTED WITH FAKES (editor absent); live: repair NOT_RUN |
| Host and CI tests recorded against exact revisions | See handoff §5 |
| Limitations documented | This file and the handoff |
