# Phase 3.5 trials (DYL-110), 2026-10-20

Text evidence only. Images, `.aseprite` documents, repair jobs and child jobs are private and live in
`~/sonoranlab-work/phase3.5/trials/` (`runs/`, `review/`, `agents/`, `results.json`, `owner-review.html`).

## Setup

- **Frozen before any edit:** `trials/trial-plan.json` (sha256 `5123884f…5a407d`, mode 0444) holds the cases, targets, masks,
  operations and the 2-iteration cap. `trials/b1/b1-ground-truth.json` (sha256 `e34e0e75…eacfde`) holds B1's hidden ground truth.
- **Code under test:** local trial tree `52c5174` = #38 head `c340d1f` + #36 head `6dc6751`. Full suite: 1193 passed. Later head
  changes are test-only (timeouts and fixtures); the final integration is in the gate document.
- **Editor:** Aseprite 1.3.18.6 (extracted, unmodified) plus aseprite-ai-artist v0.8.2 (`5894b12`), live GUI on a nested Xephyr
  inside bubblewrap (private netns), one session per repair, driven only through `sonoranlab repair`.
- **Editor worker:** Codex `gpt-6-astra`, effort high (owner-authorized), with per-invocation containment overrides:
  - MCP servers: none. No network. Writes only in its mailbox directory.
  - It reached the broker through `sonoranlab repair relay`, and the relay holds the lease token.
  - Every editor worker ran with these overrides. The non-editing code workers earlier in the run were Codex via the
    orchestrator wrapper with the user's default MCP set, and did not touch the editor.
- **Critic:** Claude Opus, read-only, reviewing before/after snapshot packs. Coordinator: Claude Opus 5.5.
- **Live writers:** one at a time across all cases.
- **Independent check:** `trials/verify.py` (does not import `sonoranlab.repair`) compares each child with its parent.
  - Decoded RGBA, with hidden RGB ignored at alpha 0.
  - Changed coordinates checked against the frozen masks.
  - Export sheet and `.tres` timing compared.
  - Godot result recorded.
  - sha256 of every parent file checked before and after.

## Results

The changed-pixel counts are for the target frames versus the parent. The export sheet count adds the exact mirror in the derived west row.

| case | what | iterations | changed px (target frames) | outside mask | unintended changes | validator | Godot | wall (s) | worker tokens | technical | critic (1×) | owner 1× |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A1 | synthetic asymmetric round trip, no edit | 0 | 0 (28/28 identical) | 0 | 0 | pass | pass | 26 | 0 | **PASS** | n/a | pending |
| A2 | pyromancer (approved) round trip, no edit | 0 | 0 (38/38 identical) | 0 | 0 | pass | pass | 53 | 0 | **PASS** | n/a | pending |
| B1 | synthetic: sword-outline gap, east f1 (blind) | 1 | 6 (sheet 12) | 0 | 0 | pass | pass | 122 | 30,789 | **PASS**, equals hidden ground truth exactly | IMPROVED, 0 min cleanup | pending |
| B2 it.1 | pyromancer: staff missing, east f4 | 1 | 92 | 0 | 0 | **fail**: `colours: frame 22 (east/4) uses 67 colours, limit 64` | not run | — | 49,661 | **FAIL** (kept) | best-looking, but invalid | — |
| B2 it.2 | same, colours restricted to the frame's own 64 | 2 | 88 vs parent (sheet 176) | 0 | 0 | pass | pass | 389 (both its.) | 54,808 | **PASS** | IMPROVED, partly: orb loses its orange (peach); ~10 min cleanup | pending |
| C1 | pyromancer: staff missing, east f4 + f0 (loop) | 1 | 97 + 94 (sheet 382) | 0 | 0 | pass | pass | 213 | 51,442 | **PASS** | IMPROVED: staff continuous; a little long and pale, f0≈f4; 10–15 min cleanup | pending |

Notes on the table:
- **Wall time** runs from setup start to finalized child. It includes editor start (about 6–8 s), the worker, checkpoint,
  finalize, the Godot check and verification.
- **Worker time** alone: B1 39 s; B2 1 m 46 s + 1 m 49 s; C1 2 m 16 s.
- **Cost:** the Codex subscription and the Claude subscription, with no per-token billing. Critic tokens are about 67.5k for B1/B2
  and 70.8k for C1. No OpenRouter spend.
- **Human time:** none so far. Owner review and cleanup times are pending and will be recorded with each child's `review.json`
  (stopwatch or timestamp bound).
- **No speedup claim:** there is no measured comparator for a manual repair, so none is made.

## Provenance

| case | repair id | authorization sha256 | target source → child frame sha256 | child manifest sha256 |
|---|---|---|---|---|
| A1 | 86c0ec21… | 94a4d1d58871… | — | 9fee98ec1fa4… |
| A2 | 71e70568… | ead7807a45d5… | — | 2628f78d4796… |
| B1 | 4389d576… | de06119c6dc5… | east_1 56d79bf38465 → f6fedbe97d16 | 584c9550e551… |
| B2 it.1 | 1c03a9cc… | 89724c3d27d8… | east_4 691898fe3724 → bc500479399a | a73fbaac870e… (pass: false, no export) |
| B2 it.2 | 1c03a9cc… | 89724c3d27d8… | east_4 691898fe3724 → e47a0d0aabf2 | defb5d41b4e8… |
| C1 | 9e51cddf… | b6c4e3d09192… | east_4 691898fe3724 → 82865cbcf58b; east_0 9a7acebed721 → 7eceb7ffb032 | fc34d34054bd… |

Integrity checks across all runs:
- **Parents:** the parent (gate-105/export-v2) was byte-identical before and after every run (sha256 of every file).
- **Approvals:** its owner approval still passes `review check`.
- **Children:** every child is correctly **not** production-usable until it gets its own review.
- **Edit outcomes:** every editor request was accepted as `applied_within_mask`. No mask violation, refusal or restore was
  triggered, and no session was TAINTED or DISCONNECTED.

## Findings

1. **Lossless interchange works** on the real asset. Pixels, alpha, all 8 stills, the frame mapping, tags, durations
   (100 ms = 1000/10 fps) and the export were all unchanged.
2. **The repair loop is fast and stays inside its scope.** Every edit, including the art-heavy ones, landed inside the frozen
   masks on the first try. An independent check that does not trust the broker confirmed it.
3. **Validation caught a real editor-introduced defect.** B2 iteration 1 added 3 colours to a frame already at the 64-colour cap.
   The unchanged validator refused it, and nothing was relaxed.
4. **Structural limit on additive repairs:** the deterministic pipeline saturates the per-cell cap. All 6 pyromancer east frames
   use exactly 64 colours, so a repair may only reuse colours already in that frame. Several staff oranges were unavailable in
   frames 0 and 4, and the orb colour suffered (B2 it.2, and partly C1). Any lift of this limit would be an owner decision about
   the threshold or about authorizing out-of-mask colour merges; this pilot did neither.
5. **The synthetic case is solved exactly.** The real cases are improvements that still need about 10–15 min of human cleanup
   (critic estimate). A technically passing result was not treated as an artistic success.
6. **Editor-worker containment (DYL-108 evidence):**
   - `codex exec -c mcp_servers={}` does **not** remove MCP servers. The worker still saw the user-scope diivi aseprite-mcp
     (`run_lua_script`), PixelLab, Blender, Godot and app connectors, all of which run outside Codex's OS sandbox.
   - The explicit per-server/plugin/feature overrides do produce MCP=NONE.
   - Codex's sandbox also blocks AF_UNIX, hence the file relay.
   - Evidence: `~/sonoranlab-work/phase3.5/evidence/editor-worker-containment/`.

## Not run, or outstanding

- **Owner 1× reviews** for all children, and owner cleanup/review times. Use `trials/owner-review.html` and the
  `sonoranlab review record` commands it lists.
- **Real-asset Trial B single-frame case with an independent defect:** B2 and C1 share the same real defect (the staff in east
  frame 4). Clean pixel-level defects (detached islands) were searched for in the approved frames and none were found.
