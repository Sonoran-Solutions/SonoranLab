# Phase 5 senior-review handoff — New-class orchestration and game-ready asset delivery (DYL-123..135)

**Status: REVISED after senior review — READY_FOR_SENIOR_RE-REVIEW** (§0). Nothing is merged, auto-merge is off,
Phase 6 is not started, no artwork was approved on the owner's behalf (every approval below quotes the owner), and
the production game checkout was never written. Game installation: **HOLD**.

## 0. Revision after senior review (2026-10-29)

Senior verdict: REVISE before merge (P1 in #62/#63); Phase 5 engineering GO conditional; installation HOLD. The owner
(verbatim): "Make the required revisions from senior review, and record that I agree with the reviewer's answers to
the open questions for me".

**P1 — delivery bound to the campaign's current approved artwork** (PR #65, `53182b4`, `ba54300`, test import fix `4d8d34a`):
- `campaign.check_delivery()`: the composed job must be one of this campaign's owner-approved sheets for its
  current inputs (reference hashes, character row, request apart from the delivery-only `height_m`), and must
  contain exactly the current selection of every planned clip. `BuildDelivery._inputs` calls it at submission.
- At execution, `run()` holds the campaign's mutation lock from the check until the bundle is written, so no
  decision or revision can land in between. It builds from the same snapshot it validated against the frozen
  input hashes.
- Campaign readiness (STAGED/READY) counts only bundles of sheets that contain the current selections.
- Regressions, each shown to fail without its fix where applicable:
  - an older revision's sheet;
  - changed references;
  - an owner-approved sheet composed outside the campaign;
  - a revision between submission and execution;
  - the permitted height-only revision;
  - the lock blocking a revision during a build;
  - readiness ignoring a superseded selection's bundle.
- An independent Codex review of the first fix found the race, the double read and the readiness gap. All three
  are fixed in `ba54300`.
- The three existing bundles were built before this check. Re-checked read-only on the live checkout at `3a2eac4`,
  `check_delivery` accepts each bundle's sheet for its campaign (gun rev 2, thief rev 2, engineer rev 3), and all
  three campaigns still derive READY_TO_INSTALL under the stricter readiness rule. They are superseded anyway by the
  rebuild in O11. (A first attempt ran the check from the integration worktree's code; the profile then resolved to
  that worktree's path, so the clip jobs didn't count as current and all three were refused. Not a product finding.)

**Owner decisions recorded** (ADR-0011's decision table, PR #53 `af6d7f3`; run `owner-decisions.log`):
- ADR-0010 accepted, with an amendment recording what Phase 4 shipped. ADR-0011 accepted with amendments:
  - its scope is visual asset delivery only;
  - installation is a separate authorization;
  - the 60-minute criterion is not declared passed for any class;
  - the decision table now records the decisions actually made.
- DYL-132 retargeted to a disposable copy of an approved Phase 5 thief job.
- Installs on HOLD. They need a rebuild at the current game revision in a fresh private snapshot, a combined
  retest, and a separate authorization per bundle, in the order thief, engineer, gun.
- Missing cast/death: game-side fallbacks (with runtime tests) short term, dedicated clips long term.
- Content-hash provenance resolver: approved and prioritized.
- Gun's dark-colour shift: investigate before any waiver; the tolerance stays 6.

None of these is executed by this revision. They are follow-up work under the recorded decisions.

**Revised tested tree:** `p5/integration` @ `5631ec8` (code; plus this evidence). Full-suite result: see the
addendum at the end of §5.

## 1. Outcome

Three owner-selected new classes — **gun, thief, engineer** — went from concept only (no prior art) through
Codex-generated references, deterministic reference prep, owner-approved references and profile rows, campaign-driven
Track E generation over real stdio MCP, visual inspection, owner 1x reviews, delivery bundles and the game's own
consumer checks in a disposable snapshot. All three are **READY_TO_INSTALL** for game commit `dd4b2c1`. None is
INSTALLED; all are `playable_class: false` (idle + run only; the game needs `cast` and `death`). Case reports:
[dyl134-class-reports.md](dyl134-class-reports.md); delivery evidence: [dyl131-133-delivery.md](dyl131-133-delivery.md).

Separate states, never merged: technical PASS (validator + Godot) · agent recommendation (`campaign_decide`) · owner
approval (1x review of the composed sheet) · STAGED_AND_ENGINE_VERIFIED / READY_TO_INSTALL (bundle + consumer) ·
INSTALLED (owner only).

## 2. Ticket outcomes

| Ticket | Outcome |
|---|---|
| DYL-123 contract | ADR-0011 (status **proposed**), PLAN Phase 5, docs/10 loop — PR #53 |
| DYL-124 runner race | deterministic `dispatched` handshake; 30/30 stress loop; no sleeps/skips — PR #52 |
| DYL-125 BiRefNet provenance | hash-verified weights + load-time rewrite recorded; `model_provenance` per attempt; historical manifests untouched — PR #54 |
| DYL-126 history / approval | unified native + imported history; approval recomputed, never cached — PR #55 |
| DYL-127 Track C over MCP | pre-GPU defect found live and fixed (control at any cell); attempt 2 execution PASS; output ~2.5× scale, route stays disabled — PRs #56, #60 |
| DYL-128 class requests / refprep | schema, `check_class_request`, deterministic refprep, owner-approved rows — PRs #57, #60 |
| DYL-129 campaign ledger | revisions, hash-chained events, durable budgets, escalation, owner-only decisions — PRs #61, #63 |
| DYL-130 `/new-class` skill | skill over 23 MCP tools, `view_artifact` images, delivery tools; fresh agent ran thief/engineer — PR #62 |
| DYL-131 smoke + consumer | isolated side-by-side render; game-consumer harness — PRs #58, #59, #65 |
| DYL-132 optional repair | **STOPPED/NOT_RUN** at parent admission (no importable parent); unavailable-runtime discovery PASS — [dyl132-repair.md](dyl132-repair.md) |
| DYL-133 delivery | compose/build/apply/verify/rollback; three bundles READY_TO_INSTALL — PRs #59, #65 |
| DYL-134 three classes | gun, thief, engineer delivered to READY_TO_INSTALL with every attempt and failure preserved — PRs #64, #65 |
| DYL-135 this handoff | this file — combined-check PR (DO-NOT-MERGE) |

## 3. PRs, heads, bases, merge order

| Order | PR | Head branch @ sha | Base |
|---|---|---|---|
| 1 | #52 | p5/dyl-124-runner-race @ a2e9e64 | main (b42317f) |
| 2 | #53 | p5/dyl-123-contract @ af6d7f3 | main |
| 3 | #54 | p5/dyl-125-birefnet-provenance @ ad92e9d | main |
| 4 | #55 | p5/dyl-126-character-history @ e22db92 | main |
| 5 | #56 | p5/dyl-127-pose-control @ 28e3c6b | #54 |
| 6 | #57 | p5/dyl-128-class-requests @ 026b87e | #53 |
| 7 | #58 | p5/dyl-131-smoke-render @ 13c6465 | #53 |
| 8 | #59 | p5/dyl-133-delivery @ 9eba20b | #53 |
| 9 | #60 | p5/dyl-127-128-evidence-rows @ 457e3c6 | p5/base/242afa2 |
| 10 | #61 | p5/dyl-129-campaign-ledger @ d223472 | p5/base/242afa2 |
| 11 | #62 | p5/dyl-130-new-class-skill @ 71fd442 | p5/base/eec2d79 |
| 12 | #63 | p5/dyl-129b-composed-review @ e37856e | p5/base/dd73493 |
| 13 | #64 | p5/gun-attempt-limit @ 9198158 | p5/base/abb1c97 |
| 14 | #65 | p5/live-run-fixes @ 4d8d34a | p5/base/f550c4c |

`p5/base/<sha>` branches are snapshots of `p5/integration` at the commit each later branch was cut from, so each PR
shows only its own commits. The integration merges resolved conflicts in `cli.py`, `api/tools/__init__.py`, docs/06
and docs/09 (both sides kept; MODULES tuple combined). Recommended path: review the focused PRs, then merge the
integration tree (the combined-check PR's content) in one step, or merge in the order above re-resolving those
conflicts; either way the merged result must equal the tested tree in §4.

## 4. Exact tested tree

Code tree: `p5/integration` @ **782b3b5** (all PRs above integrated). The combined-check PR adds only this handoff and
three evidence notes under `benchmark/results/2026-10-28-phase5/` (inert: no code, test, fixture or config reads
them). Live runs used `~/src/SonoranLab-p5live` checked out at the integration head current at each step
(abb1c97 → f550c4c → f2efd72 → 55369d3 → 6cdb4ff → e56b961 → 692c88e → df64aa6; 782b3b5 adds only a test-isolation fix, see §5); each run names its head in the
evidence notes.

## 5. Test results

Local full suite on the exact code tree **782b3b5** (supervised, `fullsuite` attempt 2, 1140 s), the same commands as
hosted CI (`.github/workflows/ops-tests.yml`):

| Check | Result |
|---|---|
| `ruff format --check`, `ruff check` | clean |
| `pytest tests --ignore=tests/sonoranlab` | 147 passed |
| `pytest tests/sonoranlab` (one process) | **1973 passed, 1 skipped** |

The skip is `tests/sonoranlab/smoke/test_real_engine.py` (heavy real-Godot job, opt-in via `SONORANLAB_SMOKE_REAL=1`).
It is **not a pass**: real-engine evidence comes from the live smoke and consumer runs in §8 instead.

The first full run, on df64aa6, failed 5 smoke-job tests. When all directories run in one process, the newclass
fakes leaked into the smoke tests through the test-only executor override, which was cached globally. The fix
(`b41d487`, in PR #65) looks the override up per call and adds a regression test that fails on the old code.
Focused runs during the phase: profile 159, campaign 33, generation 59, review 203, deliver 29, newclass 20, repair
428, registry 65, smoke 45 (+1 skip), validate 94. Hosted CI: PRs #52–#55 green; #56–#65 tests job green, package
job reported on each PR; the combined-check PR's CI result is recorded on that PR.

**Addendum — revised tree `5631ec8` (2026-10-29, `fullsuite` attempt 4, 1148 s):** ruff clean; `tests` (ops) 147
passed; `tests/sonoranlab` one process **1981 passed, 1 skipped** (the same opt-in real-Godot test, not a pass).
Attempt 3 (on `3a2eac4`) failed one new test: it imported `approve` from the bare `conftest` inside the function,
which in a single-process run resolves to another directory's conftest. Fixed in `4d8d34a`; preserved.

## 6. Closure of the three Phase 4 findings

| Finding | Closure evidence |
|---|---|
| Runner race in `tests/ops/test_run_track.py` (DYL-124) | deterministic `dispatched` marker + one-dispatch assertion; delayed-staging and unsafe-reservation variants; 30/30 loop; hosted CI green (#52) |
| BiRefNet weights unverified (DYL-125) | four files hash-verified (B1/B1a/B1b/B1c), node commit pinned, cache keyed by path/size/mtime/inode; `model_provenance.birefnet.verified: true` on every Phase 5 attempt; old manifests not rewritten ([dyl125 note](dyl125-birefnet-provenance.md)) |
| Approval as a cached flag / split history (DYL-126) | merged history entries with technical and human states recomputed per read; paginated; 4 review findings fixed (#55) |

Open, related (not one of the three): Phase 4 gate job `job-20261021T040422Z-1bafb051` fails `check_review` with
`path_refused` even on main, because its manifests bind paths in a removed Phase 4 worktree. The same root cause
blocked DYL-132. Recommended follow-up: bind repository inputs by content hash (the Phase 5 profile-row binding
verifies a changed profile against git but cannot resolve a missing path).

## 7. Provenance

Models: Qwen Image Edit 2511 FP8 + Lightning 4-step LoRA, Qwen 2.5-VL 7B FP8, Qwen VAE, BiRefNet-general (all hashed in
every job manifest). Reference images: Codex `gpt-6.1-sol` `image_gen` (owner-directed; licence record in docs/04,
OpenAI Terms of Use as in the game's SOURCE.md precedent), then deterministic `refprep`. GitHub #40 (Qwen Image 2.1)
untouched: not awaited, not promoted, defaults unchanged. Astra was not used. Each composed job binds its clip jobs,
stills and report digests; each review binds the exact sheet bytes; each bundle binds sheet, review, report, request,
profile and import template hashes ([dyl131-133-delivery.md](dyl131-133-delivery.md)).

## 8. Engine consumer, delivery and rollback

Three bundles passed the game's own `dotnet test` (SamuraiPresentationTests 3/3), Debug build, headless import and
gallery capture (65) in a disposable clone at `dd4b2c1`; `--pixel-check` stops at the expected missing death clip.
Every apply was rolled back to a clean tree. Defects found and fixed on the way: consumer `df -P`, Godot uid alphabet
(engine rewrote gun's `.import`), smoke socket path length, engineer height mismatch (owner set 1.74 m), and a test-only executor override that leaked across test directories in a single-process run (5 smoke failures in the first full suite). Smoke renders:
thief and engineer 10/10; gun 9/10 (`nearest_filtering` fails by 1 on two dark colours, no blending; threshold
unchanged).

## 9. Optional capabilities

| Capability | Status |
|---|---|
| Track C `pose_frame` | live-verified execution at 242afa2; **disabled** in `/new-class` (scale defect; no fallback) |
| Aseprite repair | tools present when the runtime exists, absent when unavailable (PASS); live lifecycle NOT_RUN (DYL-132) |
| Qwen Image 2.1 (#40) | independent; not used |
| Fallback render in consumer | not run (`fallback_render_non_consumer`, never gating) |

## 10. Resources and supervision

GPU jobs serialized through the job manager's gpu.lock; guard lease 40 GiB + 8 GiB margin per Track E job, 60 + 8 for
Track C; GTT credit off; ComfyUI in `strix-ai` stopped by the owner, no service paused by SonoranLab. Godot work ran
under the host heavy-job lock, one at a time (it waited for another project's campaign twice). Memory sampling unit
`sonoranlab-p5-sampler3` (5 s, host-wide) — per-attempt memory NOT_MEASURED.

## 11. NEEDS_OWNER_INPUT (decided 2026-10-29: see §0)

1. Accept or amend **ADR-0010** and **ADR-0011** (both proposed).
2. **DYL-132**: authorize the bounded edit on a Phase 5 class job copy, regenerate a pyromancer parent, or defer.
3. **Installation**: per bundle and target revision. The bundles target `dd4b2c1`; the game is now at `fb712e7`, and
   the three bundles share one base manifest, so they must be rebuilt (stacked) for the actual target first.
4. **Playability**: `cast` and `death` clips (or game-side fallbacks) before any class is playable.
5. **Phase 4 approval-path finding**: approve the content-hash input binding follow-up.
6. Gun's smoke `nearest_filtering` finding: accept as an engine dark-channel effect or ask for investigation.

## 12. Recommendation

**HOLD for Phase 6 until items 1 and 3–4 are decided; GO on the Phase 5 code after senior review.** The pipeline did
what Phase 5 asked: three classes from nothing to engine-verified, install-ready bundles with every failure kept, and
each live defect fixed at its root with a regression test. It also showed the limits honestly: rifle and cannon
continuity across directions was the main failure (6 run seeds for gun, 3 idle seeds for engineer, both ending in
owner-accepted defects), and no class is playable without cast/death clips.
