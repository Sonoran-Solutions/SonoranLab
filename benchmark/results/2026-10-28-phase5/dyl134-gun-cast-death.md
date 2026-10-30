# DYL-134 addendum: gun `cast` and `death` (2026-10-29/30 UTC)

The owner asked for the work (verbatim): "Can you also prioritize producing the Gun's cast and death clips? Twos are up
is ready to implement the Gun class and needs those animation". The game plays `cast` while a standing gun shoots
(it falls back from cut → attack → cast), and its `--pixel-check` requires `death`.

Owner decisions are recorded in the run's owner-decisions log and in ADR-0011's amendments:
- **Validator:** new `fall` rules; cast keeps gun's existing gates.
- **Scope:** gun row only, ≤3 seeds per clip, the proposed motion prompts.
- **Held final frame:** judged by silhouette, ≤2%.
- **Death budget:** "Grant 3 more seeds", then "Fix death first, then ship all 4" (x2 start room, attempt cap 9, +3 seeds).
- **Smoke dark-colour finding:** a gun-only exception (O14).

All runs were Track E over the campaign ledger (`camp-20261028T064155Z-42d7df73`, revisions 3–4), inspected visually
before each decision. Nothing was deleted.

| Clip | Seed | Job | Result | Cause found → fix |
|---|---|---|---|---|
| cast | 0 | `job-20261029T033154Z-ff2ed84c` | FAIL: SE foot 32 px every frame, N top +26 | Median-bbox registration shifted SE 6 px (aimed rifle) → `e060cf2` non-loop clips anchor on frame 0 |
| cast | 1 | `job-20261029T035130Z-9ffc4ced` | FAIL: same SE 32 px | same; no supported reprocess for campaign jobs, so the last seed runs under the fix |
| cast | 2 | `job-20261029T041305Z-cee33676` | **PASS**, 0 warnings, selected | raise–aim–lower, rifle held in every frame |
| death | 0 | `job-20261029T043001Z-1c5b72b1` | FAIL: hold 0.58–0.77 (colour), SE/E clipped | the colour metric failed even approved idles (24–38% shimmer) → owner: silhouette ≤2% (`e895f80`) |
| death | 1 | `job-20261029T045029Z-99ed6ae2` | FAIL: hold, S clipped | the step-based settle ended at frame 25 while the body still slid → `5925bac` settle by silhouette vs the final frame |
| death | 2 | `job-20261029T121146Z-270e2166` | FAIL: hold 0.04–0.11 (silhouette) | same settle defect; budget 3/3 → escalated, owner +3 |
| death | 3 | `job-20261030T010513Z-ba1c0b3f` | FAIL: NE rifle clipped | hold passes everywhere after `5925bac` |
| death | 4 | `job-20261030T012240Z-03cc87ab` | FAIL: N/NE never settle within 33 frames | true model result |
| death | 5 | `job-20261030T014144Z-76a35db9` | FAIL: E clipped | the lying body left the generation frame itself (x=0 before registration) → escalated; owner "fix death first" → `c231b51`: still placed at ×2 on the same 480 px canvas (same Wan tokens/memory), fall clips fit inside the cell with the start offset reported |
| death | 6 | `job-20261030T022517Z-2ac57284` | **PASS**, selected | convincing collapse, held final pose; warning: SE starts +29 px from the still's anchor |

Totals for the whole gun campaign (all four clips, 17 attempts): generation 14 018.6 s, processing 1 024.8 s,
validate/export 188.9 s, wall (union) 15 334 s ≈ 4.3 h. For cast and death, Track E took 3 seeds and 7 seeds; the
first-pass result was a FAIL for both. Tokens/cost and owner hands-on time: not measured (null).

Final: compose `job-20261030T024132Z-cf95a191`, sheet `862ef0cefe0ade5d…` (4000×1280, 25 columns: idle 1–4,
run 5–10, cast 11–18, death 19–24). Owner 1x review: "Approve gun sheet". Smoke (beside thief,
`job-20261030T024609Z-13f42d11`): 9/10. `nearest_filtering` is the known dark-channel conversion on the same two
colours (owner gun-only exception, O14); the render budget refused two siblings, so one was used.

Delivery: bundle `job-20261030T024927Z-fbfbdd9f` at game `fb712e7` (the game's current HEAD), with no missing clips.
The game consumer in a fresh clone passed `dotnet test` 2/2, the build, the import, `--pixel-check` (exit 0, now
with a death clip) and the gallery (65). Verify gave READY_TO_INSTALL, then rollback left the clone clean. Not
installed.

Known defects the owner accepted at 1x:
- the run loses the rifle E/W;
- SE death start jump +29 px;
- the rifle is lost in some lying frames (N) and flies out in SE mid-fall;
- a cast speck in S frame 1 and a 1-px sight bump in SE/E.
