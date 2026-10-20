# Phase 3.5 gate (DYL-111): senior-review package

## Revision 3 (2026-10-20, after the senior re-review) — supersedes the heads in revision 2

**Senior re-review of revision 2:** REVISE with one new P1. All earlier findings are confirmed fixed, and #36 is approved.

**The P1:** existing `--repair` paths bypassed destination confinement. `lease()` created `session/lease.lock` before
validating the directory, so `sonoranlab repair export <game checkout>` could write into the game. `authorize`, `abort`,
`session start` and `finalize` behaved the same, and a copied repair outside the root was accepted.

**Fix** (implemented by Gemini 3.8 Flash, high, via agy, as the owner requested; coordinator-reviewed):
- `confine.admit_repair` is one shared, read-only admission check that creates nothing. It refuses with
  `repair_not_admitted: ...` when the repair:
  - has a symlinked component;
  - lies outside `SONORANLAB_REPAIR_ROOT`;
  - is in the SonoranLab repo, a Git checkout, a bare repo or a `GIT_DIR`/`GIT_WORK_TREE` area;
  - is in the game's Godot project or palette checkout;
  - is nested inside another job or repair;
  - lacks a regular `.sonoranlab-staging` marker of kind `repair` whose id equals `repair.json`'s `repair_id`
    (schema `sonoranlab.repair/1`);
  - is not structurally a repair (`repair.json`, `mapping.json`, `source/`, `workspace/`).
- It runs first inside `lease()`, so every lease holder is covered: export, authorize, abort, session start/stop/checkpoint,
  broker and finalize. The CLI dispatcher also calls it explicitly for every mutating verb (export, authorize, abort,
  session start|stop|checkpoint, relay, finalize).
- **Regressions:** `tests/sonoranlab/repair/test_repair_admission.py` runs 6 mutating verbs against 9 invalid paths:
  - a fake game checkout inside the root;
  - a path outside the root;
  - a byte copy of a valid repair outside the root;
  - a missing staging marker, a mismatched id, and the wrong kind;
  - a symlinked component;
  - a path nested in a job;
  - a path inside the SonoranLab repo.

  Each case asserts a non-zero exit and a byte-for-byte unchanged target, with no `session/` or `lease.lock`. A positive case
  shows a genuine repair is still admitted.
- **Coordinator reproduction:** the senior's exact scenario, run against a *fake* game checkout with the default environment.
  All six verbs refused, the fake game was unchanged, and no child or mailbox was created. The real game checkout was never
  used.

**Heads (revision 3):** #36 `04cfda5` (base main, APPROVED by the senior) → #35 `54655e7` → #37 `c375081` → #38 `a67bdc7`.
#39 is the disposable combined check at `a67bdc7`, with its title and description updated. The split into the stack is
blob-identical to the tested tree `e304fe4`. Intermediate tips: #35 98 repair tests pass, #37 266.

**Evidence at `a67bdc7`:**
- Full suite: **1439 passed, 0 skipped**, ruff clean.
- The core imports no `sonoranlab.repair` module.
- All five recorded owner reviews still pass `review check`.
- Hosted CI: green on the #35, #36, #37, #38 and #39 heads. One #38 run hit a flaky pre-existing ops test (a benchmark
  runner workflow-file comparison in `tests/`, code untouched by this stack); it was green on re-run and in the sibling run.

**Recommendation:** unchanged. **ADOPT, narrow and optional,** then **GO for core Phase 4** with the editor optional. Owner
decisions still needed: the formal disposition, merge approval, the user-scope MCP configuration, and the mount-namespace
follow-up (not a merge blocker per the senior).

---


## Revision 2 (2026-10-20, after the senior review) — supersedes the status, heads and recommendation below

**Status: READY_FOR_SENIOR_REVIEW (revision 2).** Senior verdict was REVISE before merge, narrow ADOPT recommended.
Both P1 categories and the P2 are corrected. Owner visual approval is recorded. The formal disposition is still the owner's.

- **Owner 1× decision:** "I approve visually" (orchestrator session, 2026-10-20 ≈ 21:00Z). Accepted: A1, A2, B1, B2 iteration 2
  and C1. B2 iteration 1 stays a documented technical failure (67 > 64 colours).
- **Artifact-bound reviews recorded** on the five children (`~/sonoranlab-work/phase3.5/trials/runs/*-child*/review.json`):
  - verdict pass, reviewer "Dylan (owner)", `cleanup_minutes` 0 (no owner edits);
  - interval 2026-10-20T20:24:10Z → 21:03:13Z, a **timestamp upper bound** (review page generated → approval received), not a
    stopwatch time; the critic's cleanup estimate is not substituted;
  - every review binds `repair_manifest_sha256`; all five pass `review check`.

### Corrections

| finding | fix | PR |
|---|---|---|
| P1 staging paths: prepare/relay/finalize not fully confined | explicit roots `SONORANLAB_REPAIR_ROOT` (default `~/sonoranlab-work/repairs`) and `SONORANLAB_AGENT_ROOT` (`~/sonoranlab-work/agents`). `confine.check_destination` refuses, **before anything is created**: outside the root; symlinked components; existing paths; any Git checkout (`.git` dir with HEAD, `gitdir:` file, bare repos, `GIT_DIR`/`GIT_WORK_TREE`, `git rev-parse` cross-check); the SonoranLab repo; the game checkout and Godot project; the parent; the repair; any existing job/repair. Reservation walks no-follow dir fds, writes a staging marker first and verifies the fd. The relay does all mailbox I/O through fds, reuses only its own repair-bound mailbox, and re-verifies the mailbox and each queue before every claim/publish (`ok:false` on interference). | #35, #37, #38 |
| P1 repair lineage not bound to approval | the child `manifest.json` declares `{"repair": {"manifest": "repair-manifest.json", ...}}` with no sidecar hash (no circularity). `review record` binds `repair_manifest_sha256`. `review check` refuses a missing, symlinked, modified, replaced or added-after-review sidecar, and a declared child without a binding. | #36, #38 |
| P2 `binds.job` unchecked | `review check` requires `binds.job` == the job's resolved path. A copied or moved approved job loses approval; relocation = a new review with `--supersedes`. | #36 |

### Threat model for the path checks

- **Defended:** mistaken or hostile *paths* (including agent-supplied ones) and symlinks that exist at check time.
- **Not defended:** a **concurrent same-UID process** renaming or swapping ancestor directories *during* a command. Such a
  process can write into those locations directly anyway, and no userspace path discipline can stop it relocating an ancestor
  of the staging root. The final verification is best-effort detection, not a guarantee.
- The Astra re-audit reproduced such races (a transient swap restored before verification; moving a pinned ancestor). They are
  recorded here as accepted residuals, not hidden.
- Upgrade path: run prepare/finalize inside a mount namespace where only the staging root is writable and the game and
  repository are not mounted. This would also close those races, and is proposed as a follow-up.

### Review history of the corrections

- Codex sol implemented. The coordinator fixed:
  - the checkout detector being fooled by an empty sandbox `/tmp/.git` mount point;
  - relay reuse with worker files present;
  - an output race under the lease.
- Astra audit 1: **REJECT** (ancestor-swap at reservation, `GIT_DIR`/bare repos, marker-less failed prepare) → fd reservation,
  Git detection and marker-first fixes.
- Astra audit 2: **REJECT** (post-creation transient swap, relay queues, moved pinned ancestor). The queues were fixed; the two
  concurrent-race classes were scoped out as above.
- Astra audit 3 (narrow, at `63fd9df`): **REJECT** on one non-race gap (a Git repository inside a relay queue). It confirmed
  the queue identity fix, the threat-model wording, and the sidecar and `binds.job` controls. Fixed in `3172de5`; the auditor's
  own reproducers now refuse every variant (zero broker calls, nothing claimed or published). That last fix was verified by the
  coordinator with those reproducers, not by a further Astra pass.

### PRs and heads (revision 2)

| PR | ticket | head | base | CI |
|---|---|---|---|---|
| #36 | DYL-109 core (review hardening + job identity + sidecar binding) | `04cfda5` | main | green |
| #35 | DYL-107 (+ confinement) | `d9db1c2` | #36 | green |
| #37 | DYL-108 (+ relay confinement, queue identity/layout/Git checks) | `3172de5` | #35 | green |
| #38 | DYL-109 finalize (+ out confinement, sidecar declaration, race tests) | `a8a1832` | #37 | green |
| [#39](https://github.com/Sonoran-Solutions/SonoranLab/pull/39) | **DO NOT MERGE** published combined check (= #38 head) | `a8a1832` | main | green |
| #34 | docs (this file) | this branch | main | green |

- **Merge order:** #36 → #35 → #37 → #38; #34 is independent. Close #39 unmerged after.
- **Stack integrity:** each correction round was split into the stack by file, and the resulting top tree was checked to be
  blob-identical to the tested worker tree.
- **Intermediate tips stand alone:** #35 tip 98 repair tests pass; #37 tip 260.

### R2. Evidence at the published combined tree `a8a1832`

- Full suite at `a8a1832` (= #38 = #39 head, contains #36/#35/#37): **1375 passed, 0 skipped**, ruff format/check clean.
  The core imports no `sonoranlab.repair` module. Hosted CI is green on #35, #36, #37, #38 and #39 heads. An earlier #39 failure
  was a flaky pre-existing ops test (`test_concurrent_same_job_is_re…`, code untouched by this stack), green on re-run.
- **Trials:** they ran on `52c5174`. The corrections change only path confinement and approval binding, not the pixel,
  validation or export logic. The senior review accepted synthetic verification without re-running the trials. The full suite
  here includes the live sandboxed Aseprite GUI session and the Godot checks on the Strix Halo box.

### Recommendation (revision 2)

- **ADOPT, narrow and optional,** per the senior review:
  - the contained editor with the tested bounded `draw.pixels/grid/clear/blit` repair workflow only;
  - `cel.move`, transforms, rigging and animation creation outside the adopted capability;
  - SonoranLab authoritative for validation, export, lineage and approval.
- **Then GO for core Phase 4,** with the editor optional and not a dependency of the MCP server.
- **Owner decisions still needed:**
  1. Record the disposition.
  2. Merge approval.
  3. The user-scope diivi aseprite-mcp / PixelLab MCP for ordinary workers. The senior recommends removing them, or disabling them
     by default; this is the owner's configuration and was not changed.
  4. Whether to schedule the mount-namespace isolation follow-up.

---


**Status: READY_FOR_SENIOR_REVIEW.** The coordinator's disposition recommendation is **HOLD** until the owner's 1× reviews are
recorded. After that the recommendation becomes conditional: **ADOPT, narrow and optional**, if the owner passes C1 (or B2), and
**DEFER** otherwise. The owner decides. No PR is merged, auto-merge is off, and Phase 4 has not started.

## 1. PRs, heads and relationships

| PR | ticket | head | base | content | CI |
|---|---|---|---|---|---|
| [#34](https://github.com/Sonoran-Solutions/SonoranLab/pull/34) | DYL-106, DYL-110, DYL-111 | this branch | main | ADR-0009 (proposed), PLAN Phase 3.5, architecture, environment, trials and gate evidence (docs only) | green |
| [#35](https://github.com/Sonoran-Solutions/SonoranLab/pull/35) | DYL-107 | `3eb423e` | main | `sonoranlab repair prepare/export/diff/inspect/abort`, sandboxed batch interchange | green |
| [#36](https://github.com/Sonoran-Solutions/SonoranLab/pull/36) | DYL-109 (core) | `6dc6751` | main | review-metadata hardening; independent of the pilot and to be kept under any disposition | green |
| [#37](https://github.com/Sonoran-Solutions/SonoranLab/pull/37) | DYL-108 | `5a9acbd` | #35 | lease, sandboxed live session, bounded and verified ops, relay | green |
| [#38](https://github.com/Sonoran-Solutions/SonoranLab/pull/38) | DYL-109 | `d13327f` | #37 | finalize into a child job; a fresh review is required | green |

- **Merge order:** #36 can merge at any time. Then #35 → #37 → #38. #34 is docs and has no code dependency.
- **Integration check:** the local, unpushed tree `ce1db86` (tree `b816be38`) = #38 `d13327f` + #36 `6dc6751`. Its full-suite
  result is in section 2.
- **Trials:** they ran on the local tree `52c5174`. `git diff 52c5174 ce1db86 -- sonoranlab` is empty: the trials exercised exactly
  the code under review, and later commits only touched tests.
- **Starting main:** `2fc9323`.

## 2. Test evidence (not the same as skipped tests)

- **Hosted CI (GitHub, no Aseprite, Godot or Xephyr):** the package tests run against fakes; live tests skip there.
- **Local, Strix Halo box (Aseprite runtime, Xephyr, Godot 4.7.2 present):**
  - `tests/sonoranlab/repair`: 205 passed with 0 skipped at `d13327f`. That includes the live GUI broker session, the live batch
    round trip, Unix-socket CLI integration, the relay, and Godot finalize checks.
  - Full suite on the integration tree `ce1db86`: **1193 passed, 0 skipped**, ruff format/check clean.
- Editor absent (`SONORANLAB_EDITOR_RUNTIME=/nonexistent`): core suite (`tests/sonoranlab` minus `repair/`) 848 passed; importing
  the CLI, pipeline, validator, Godot exporter and review imports no `sonoranlab.repair` module; `repair prepare` returns
  `editor_unavailable` (exit 1).
- **Independent reviews:**
  - #36: Codex Astra adversarial REJECT (7 findings) → fixed → re-audit found 2 regressions → fixed → the auditor's probes rerun green.
  - #37: Claude Opus ACCEPT_WITH_NITS (no P1/P2) → all P3s and nits fixed.
  - #38: Claude Opus REJECT (2 P1 + 1 P2 gate bypasses) → fixed → the reviewer's probes no longer bypass.

## 3. Pinned versions and environment

See [`dyl106-environment.md`](dyl106-environment.md):
- Aseprite 1.3.18.6 (AppImage sha256 `feb6330f…`, extracted and unmodified).
- aseprite-ai-artist v0.8.2 `5894b12` (MIT), built offline from its lockfile.
- node v26.8.1 (sha256 `19235a9b…`), bubblewrap 0.9.0, Xephyr 21.1.12, Godot 4.7.2, codex-cli 0.163.0, Claude Code 2.1.289.

## 4. Installation and rollback

- **Nothing was installed globally.** No agent configuration, `~/.config/aseprite` file or system package was changed. The
  upstream `install` command was never run.
- **Runtime:** `~/sonoranlab-work/phase3.5/runtime/`.
- **Rollback:** delete that directory. The core never imports `sonoranlab/repair`, so the pipeline works without it, and every
  `sonoranlab repair` verb reports `editor_unavailable`.

## 5. Security model, containment evidence and residual risks

See ADR-0009. In short:
- **Bridge isolation:** the unauthenticated bridge exists only inside a private network namespace; host connects are refused.
- **Filesystem:** only the repair workspace and a private HOME are writable. A write to a host path stayed in the sandbox's
  throwaway root.
- **Lua and headless:** `run_lua` is absent, and no editor binary is reachable, so a headless switch cannot run.
- **Owner's editor:** the owner's Aseprite is a different process with a different HOME.
- **Edit verification:** every op is pre-validated, then verified pixel by pixel across all frames and metadata, and
  restored-or-TAINTED.
- **Unknown outcomes:** never resent, and disconnects fail closed.
- **Finalize:** it re-derives everything from the saved document and refuses any out-of-mask change, metadata change, profile
  change or unbound document.
- **Editor worker:** contained by Codex's OS sandbox plus explicit overrides (MCP=NONE). The relay holds the token.

**Residual risks:**
- Same-UID processes can modify files on disk. Hash checks detect this but do not prevent it.
- Codex sub-agent tools remain available to the editor worker, inside the same sandbox.
- The sandbox's private HOME persists between sessions of one repair.
- Xephyr is an X client of the owner's desktop.
- bwrap depends on Ubuntu's userns policy.
- The upstream extension is third-party code running inside the sandbox.
- The pre-existing user-scope diivi aseprite-mcp (arbitrary Lua) and PixelLab MCP remain available to agents that are not
  explicitly contained. That includes ordinary Codex/Claude code workers, and is an owner configuration matter, outside this pilot.

## 6–8. Round trip, repairs, diffs, time and cost, validation and Godot

See [`dyl110-trials.md`](dyl110-trials.md):
- A1 and A2 PASS (lossless).
- B1 PASS (exactly equal to ground truth).
- B2 iteration 1 FAIL (colour cap, kept).
- B2 iteration 2 PASS, with a partial artistic improvement.
- C1 PASS, improved, with 10–15 min of cleanup left.

All finalized children pass the unchanged validator and the real Godot import/SpriteFrames check. No unintended pixel changed in
any case.

## 9. Human visual review

**Outstanding.** No owner 1× review has been recorded yet; the read-only critic's verdicts are not a substitute.
- Review page: `~/sonoranlab-work/phase3.5/trials/owner-review.html` (local, private).
- Each child needs `sonoranlab review record <child> ...` with real start/end times and cleanup minutes.

## 10. Known limitations and deferred features

- Reads and blit sources are restricted to authorized targets. Neighbouring frames reach the worker only as snapshots.
- No undo tool upstream; the broker restores from snapshots instead.
- **Additive repairs are limited by the saturated 64-colour per-cell cap**, which is not relaxed.
- The colour-merge, layer, cel-move and transform workflows were not exercised. `cel.move` is implemented and tested against the
  fake only.
- Only one character and one animation were exercised.
- No measured manual-repair comparator, so no speedup claim.
- Iteration count = sessions; one "proposal" per session was used.

## 11. Documentation and capability matrix

ADR-0009 (status still proposed) holds the 18-tool capability matrix and the "Consequences after the trials" update. PLAN.md
Phase 3.5 is unchanged. Architecture section: `docs/01-architecture.md`.

## 12. Recommendation for Phase 4

- **Core Phase 4 (MCP server, DYL-112..117)** does not depend on this pilot and can proceed once the owner records a disposition.
  #36 should merge under any disposition.
- **Proposed disposition: HOLD**, because the owner's 1× review evidence is missing.
  - **After review, ADOPT (narrow):** if the owner passes C1 or B2 at 1× (with the measured cleanup). Keep the editor as an
    optional companion.
    - Supported ops: `draw.pixels/grid/clear`, with bounded blits on authorized frames.
    - The contained editor worker only.
    - Phase 4 wraps only the scoped repair handoff (DYL-118).
  - **After review, DEFER:** if the owner judges the real repairs not shippable, or the cleanup time makes the editor not worth
    keeping. Keep all code, leave the editor integration out of the MCP surface, and proceed with core Phase 4.
  - **Not REJECT:** no compatibility, safety or containment failure was observed.

**Owner decisions needed:**
1. Record the 1× reviews (B1, B2 iteration 2, C1; optionally A1/A2).
2. Choose the disposition.
3. Merge approval for #36 (core) and for the pilot stack.
4. Whether to contain or remove the user-scope diivi aseprite-mcp and PixelLab MCP for ordinary agent workers.
5. GO or HOLD for Phase 4.
