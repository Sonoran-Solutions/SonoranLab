# Phase 4 senior-review handoff — Local MCP server and durable jobs (DYL-112..120)

**READY_FOR_SENIOR_REVIEW.** Nothing is merged and auto-merge is off. Phase 5 has not started.
Recommendation: **GO for Phase 5** (reasons in §16), conditional on owner merge approval.

Labels: IMPLEMENTED · TESTED WITH FAKES · VERIFIED LOCALLY · OWNER-APPROVED · NOT_RUN · DEFERRED.

## 1. Delivered capabilities

Local stdio MCP server `sonoranlab` (`sonoranlab mcp serve`, official SDK `mcp==2.3.0`, project-scoped `.mcp.json`).
Every tool has a CLI twin (`sonoranlab api <tool>`) that returns the identical `sonoranlab.api/1` envelope.

- **Discovery/profile:** `capabilities`, `list_projects`, `get_profile`.
- **Registry (DYL-114):**
  - `list_characters`, `get_character`;
  - `register_character`: immutable hashed copies, unapproved;
  - `import_job`: validated indexing, no copy;
  - `get_artifact`: bounded metadata;
  - `check_review`: recomputed from disk.
  - Reference approval is **owner CLI only** (`sonoranlab registry approve-references`) and is bound to file hashes.
- **Durable jobs (DYL-115):** `list_jobs`, `job_status`, `job_result`, `cancel_job`, `retry_job`.
  - Idempotency keys; detached systemd-unit workers; a cross-process `gpu.lock`.
  - Reconciliation never resubmits; cancellation is prompt-scoped and confirmed.
- **Generation (DYL-116):**
  - `animate`: Track E, approved references only, E ≥ 40 GiB, profile autonomy limits;
  - `pose_frame`: Track C single pose at 60 GiB; no C→E fallback.
- **Deterministic (DYL-117):** `process`, `validate` (final resolution, no repixelize), `contact_sheet`, `compare`,
  `export_godot` (re-validates exact pixels), `godot_check`.
- **Optional repair (DYL-118, ADR-0009 narrow ADOPT):** `prepare_repair`, `inspect_repair`, `inspect_repair_diff`,
  `finalize_repair`, `abort_repair`. These are advertised only when the editor runtime exists.
- **Guard and ops (DYL-112):**
  - a nonce-bound runwindow lease with the admitted budget;
  - animate rejects < 40 GiB before writing;
  - the ComfyUI custom-node whitelist (idle CPU 32% → 0%).
- **Contract:** ADR-0010 (proposed) plus docs/06. The old generate_character / walk_4dir / Track A exit sequence is
  recorded as **superseded, not passed**.

## 2. Ticket status

| Ticket | Status | PR |
|---|---|---|
| DYL-112 guard/CPU prerequisites | IMPLEMENTED, TESTED, VERIFIED LOCALLY (lease admission and idle CPU under the live run) | #41 |
| DYL-113 contract + server | IMPLEMENTED, TESTED (real stdio client), VERIFIED LOCALLY (fresh agent) | #42 |
| DYL-114 registry | IMPLEMENTED, TESTED; VERIFIED LOCALLY (register, approve, import, check_review) | #43 (+#50) |
| DYL-115 durable jobs | IMPLEMENTED, TESTED WITH FAKES (fault matrix); VERIFIED LOCALLY (server SIGKILL, live cancel) | #44 (+#50) |
| DYL-116 generation tools | IMPLEMENTED, TESTED; Track E VERIFIED LOCALLY; Track C live NOT_RUN (memory) | #45 (+#50) |
| DYL-117 deterministic tools | IMPLEMENTED, TESTED; Godot check and contact VERIFIED LOCALLY | #46 (+#50) |
| DYL-118 optional repair | IMPLEMENTED, TESTED WITH FAKES; live handoff NOT_RUN (Phase 3.5 trials are the live evidence) | #47 |
| DYL-119 integration + runbooks | IMPLEMENTED, 13 stdio integration cases | #48 |
| DYL-120 gate | Executed; OWNER-APPROVED 1× pass; evidence in `phase4-gate.md` | this PR |

## 3. PRs, heads, bases, merge order

Merge in this order (stacked):

| Order | PR | Ticket | Head |
|---|---|---|---|
| 1 | #41 | DYL-112 | `201e6b1` |
| 2 | #42 | DYL-113 | `14b3140` |
| 3 | #43 | DYL-114 | `c710624` |
| 4 | #44 | DYL-115 | `18e9ed5` |
| 5 | #45 | DYL-116 | `23cfd41` |
| 6 | #46 | DYL-117 | `fa8ddc3` |
| 7 | #47 | DYL-118 | `6ca63b3` |
| 8 | #48 | DYL-119 | `4a8c5ae` |
| 9 | #50 | review and gate fixes (DYL-113..117) | `e2f69b7` |
| 10 | DYL-120 gate evidence (text only) | DYL-120 | this PR |

- #41's base is `main` @ `0f54c33`; each later PR is based on the one before it.
- **#49 is a DO-NOT-MERGE combined check.** Its head `f0208d0` is an empty marker commit; tree `b17678f` equals
  `e2f69b7`. Close it unmerged.
- After merging, verify that `main`'s tree equals `b17678f` plus this PR's text-only files.
- Stacked-merge note from Phase 3: `PUT /pulls/N/merge` needs the full 40-character sha.

## 4. Exact combined tested tree

`e2f69b7180bc670fb3f6f95f4c14a1b7f228e640`, tree `b17678fe5d2d425fe272e8cee2a5b577d809cd36`. The live gate ran on
`96724ed`; the later changes are reporting and tool-shape fixes, re-checked against the live artifacts (§9).

## 5. Test results

| Where | Revision | Result |
|---|---|---|
| Host, full `uv run pytest -q tests` | `e2f69b7` | **1856 passed, 0 failed, 0 skipped** (the host has Godot, editor runtime, private data) |
| Hosted CI `package` (`tests/sonoranlab`) | #49 `f0208d0` (= tree `b17678f`) | **1698 passed, 14 skipped, 0 failed** |
| Hosted CI `tests` (ops/benchmark) | same | **143 passed, 1 skipped, 0 failed** |
| Hosted CI on every stacked PR head (#41–#48, #50) | each head | package + tests pass |

Hosted CI skips (with reasons):
- private Phase 1 data/clips: 8;
- no Godot on the runner: 5, including the rig bake;
- no editor runtime: 2.

Earlier checkpoints:
- baseline `0f54c33`: `tests/sonoranlab` 1299 passed;
- `4a8c5ae`: 1817 passed;
- `96724ed`: 1838 passed.

## 6. Local Godot / ComfyUI / editor results (VERIFIED LOCALLY)

- ComfyUI 0.31.0 in the `--network=none` container, through `podman exec`.
- Track E: 5/5 generations, validator PASS, Godot 4.7.2 import and check PASS (8 animations, 0 retries).
- The separate `godot_check` MCP job also passed.
- Editor: runtime present; live repair handoff NOT_RUN in Phase 4.

## 7. CLI/MCP equivalence

- One registry and one envelope serve both front ends.
- Parity tests compare `sonoranlab api <tool>` stdout with MCP `structuredContent` for every tool family: registry,
  jobs, generation, deterministic and repair.
- `.mcp.json` launches are tested.

## 8. Durable-job and recovery evidence

- **Fakes (multiprocess):**
  - same-key races;
  - conflicting keys;
  - duplicates while active or while a prompt is unresolved;
  - kill -9 at each boundary;
  - lost reply leading to blocked;
  - zero resubmissions;
  - corrupt records and journal isolation;
  - pre-lease worker death;
  - retry linkage.
- **Live:** the server was SIGKILLed mid-session while a GPU job ran; a new server reattached by reading durable
  state, and the job finished. Same-key replay returned the same job.

## 9. Cancellation, concurrency, post-fix re-check

- **Live cancel:** the job's own running prompt was interrupted in about 3 s; ComfyUI recorded
  `execution_interrupted`; outcome `confirmed`.
- **Fakes:** foreign prompts untouched; unconfirmed cancels go to `blocked`; two servers never overlap GPU jobs.
- **Post-fix re-check on `e2f69b7` against the live gate job** (log: `~/sonoranlab-work/phase4/gate/demos/postfix.log`):

| Tool | Result |
|---|---|
| `check_review(job id)` | production_usable true |
| `get_artifact(native ref)` | sha `532188c6…`, 1008×1152 |
| `job_result` | 21 KB (was 505 KB), 200 of 1820 refs, paged |
| `job_status` | 6 KB, stage `validate_export` |
| `contact_sheet` | default zoom auto-selected 4 |
| `get_character` | ok: references approved; the out-of-root import is isolated as invalid |

## 10. Resources

- Live Track E run: guard 40 + 8 GiB, no pauses. Minimum MemAvailable 43.9 GiB; peak GTT 34.1 GiB.
- Total time 12.6 min (generate 683 s).
- ComfyUI idle CPU median 0.2% over about 70 min.
- Per-attempt memory: not recorded (null).

## 11. Provenance and approval

- Reference approval is an owner-delegated record bound to 8 hashes (copies identical to the Phase 3 approved set).
- Workflow, profile, still and model hashes are recorded per attempt; BiRefNet's hash is `unknown`.
- The owner's 1× review is recorded as `review/2` and bound to the final job's export, report and profile. No ancestor
  approval was carried over.
- Export files were unchanged after review.
- Earlier-phase approved export: imported and shows production_usable via MCP, with its files untouched.

## 12. Optional Aseprite status

ADR-0009 narrow ADOPT. The MCP lifecycle is implemented and TESTED WITH FAKES; it is gated on the runtime, and the
core is identical without it. Authorization and editing stay with the owner CLI and the single contained editor
worker. Live handoff: NOT_RUN in Phase 4.

The owner's user-scope diivi aseprite-mcp and PixelLab servers remain a separate owner decision; they are unchanged
and unused. The mount-namespace hardening remains a separate, gated follow-up and was **not** included.

## 13. Fresh-agent transcript

`dyl120-agent-transcript.md`: a fresh Claude Code session driving only MCP tools, with every call, latency and finding.

## 14. Runbooks

- `docs/runbooks/mcp.md`: install and approve, preflight, jobs, cancel, recovery, reconciliation, troubleshooting,
  shutdown, rollback.
- `docs/runbooks/jobs.md`: job lifecycle, crash points, retention.
- `docs/runbooks/repair.md`: optional editor flow.
- **Rollback:** remove the `.mcp.json` entry, archive and then delete `~/sonoranlab-work/mcp`, and restore the
  previous `SONORANLAB_COMFY_FLAGS` if needed.

## 15. Limitations and deferred items

- **DEFERRED (ADR-0010):** `generate_character` / Track A, `regenerate_region` (diffusion inpaint), `smoke_render`
  beside a sibling, template/bake tools, automatic C→E fallback.
- **NOT_RUN:**
  - live Track C `pose_frame`: needs 68 GiB available, the host had about 60, and no owner pause was authorized;
  - live repair handoff;
  - a live foreign-prompt cancel scenario.
- **Known gaps:**
  - BiRefNet hash `unknown`; per-attempt memory not measured;
  - `get_character.jobs` lists imported jobs only (native jobs are listed through `list_jobs`);
  - the gate job's own stage timing predates the F6 fix;
  - absolute host paths still appear inside `animate.json` (informational).
- **Review time:** the owner did not time it; the interval is a timestamp upper bound.
- **Path checks** carry the same same-UID TOCTOU caveat as Phase 3.5; mount isolation is a follow-up.
- **Single-run evidence:**
  - one character, one seed: an observation, not a cross-character guarantee;
  - east bbox drift 21 of 25;
  - Phase 3's single-run timing and samurai zero-margin caveats stand;
  - oathguard's status is unchanged.
- **Game side:** no installation into Twos Are Up. The game-side sRGB and side-by-side checks are still required
  before any separately approved install.

## 16. Recommendation: GO for Phase 5 (owner authorization required to start)

- A real agent drove generation through to an owner-approved, production-usable animation using only MCP tools.
- Safety held under a live crash, a cancel and the guard. Independent reviews (Gemini Argon: 9; Codex Astra: 6), the gate dry run (1) and the live
  fresh-agent gate (7) found 23 defects. 22 are fixed with regression tests; the BiRefNet hash is recorded.
- Phase 5 should begin with the §15 items it depends on: live Track C at an owner-approved memory window, and
  get_character listing native jobs.
