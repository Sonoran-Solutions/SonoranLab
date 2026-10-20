# Phase 3.5 gate (DYL-111): senior-review package

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
