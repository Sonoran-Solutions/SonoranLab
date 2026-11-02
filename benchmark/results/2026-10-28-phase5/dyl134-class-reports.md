# DYL-134 — three owner-selected new classes, case reports (2026-10-28 UTC)

Classes chosen by the owner (verbatim, AskUserQuestion): "This aligns with the twos are up F3 work that is about to
start, gun and thief will be built and engineer planning is being done in this phase. Have these three classes built
out visually as part of this phase to test SonoranLab's capabilities. No art has been made yet for any of these
classes so part of this phase will need to have codex create the reference images."

Visual assets only: no combat, balance or gameplay. Existing samurai, oathguard and pyromancer assets were regression
baselines (staged read-only copies of game `dd4b2c1`); none was modified. Every attempt, failure, review and
decision below is preserved in `STATE/campaigns/twos-are-up/<class>/<campaign>/events.jsonl` (hash-chained) and the
job store. Art stays private (`~/sonoranlab-work/phase5/`, `STATE`); only hashes are quoted here.

## Common route

| Stage | What ran | Evidence |
|---|---|---|
| References | Codex `gpt-6.1-sol` `image_gen` turnarounds (owner-directed, ADR-0011 §reference preparation): refgen 500 s, south-west fix 211 s; then deterministic `refprep` v1→v3 (per-source scale fix bd66fe2, whole-image segmentation 026b87e, thief `--despill-px 6`) | `~/sonoranlab-work/phase5/refprep/{gun-v3,thief-v3-dp6,engineer-v3}`; owner 1x/3x review sheet |
| Owner gates | references "Approve all three"; profile rows "Approve rows" (gun 160, thief 144, engineer 144, default limits); budget "≤3 seeds per clip" | run `owner-decisions.log`; profile commit ad9bd54 |
| Generation | Track E only (Qwen Edit 2511 FP8 + Lightning, BiRefNet provenance verified), via `campaign_submit` over real stdio MCP; guard lease admitted 40 GiB + 8 GiB margin, GTT credit off, ComfyUI in `strix-ai` stopped by the owner for Phase 5 | campaign events; job manifests (`model_provenance.birefnet.verified: true`) |
| Inspection | validator report first, then the coordinator / the fresh `/new-class` agent viewed contact sheets and composed sheets (images, not metadata); recommendations recorded separately from the technical verdict | `candidate_decision` reasons |
| Compose / review | `compose_sheet` (campaign-bound) → owner 1x review recorded by `sonoranlab review record` quoting the owner verbatim | `review.json` per composed job |
| Delivery | `build_delivery` → disposable snapshot clone of game `dd4b2c1` → `deliver apply` → `tools/game_consumer/run.sh` → `deliver verify` → `deliver rollback` | [dyl131-133-delivery.md](dyl131-133-delivery.md) |

Not measured (null, never 0): agent tokens and API cost for every stage (no cost source was recorded); per-attempt
memory inside the container; owner review duration (the recorded interval is a bound from "sheet presented" to "verdict
received"). Cleanup minutes are 0 because no pixel in any lineage was hand-edited (no repair or editor job exists);
that is an observation, not an estimate.

## Gun — `camp-20261028T064155Z-42d7df73` (rev 2) — READY_TO_INSTALL

| Clip | Seed | Rev | Job | Validator | Generation s | Outcome |
|---|---|---|---|---|---|---|
| run | 0 | 1 | `job-20261028T064221Z-c9d4a659` | PASS | 730 | rejected: rifle vanishes in E (mirrored W) and SE 1-3 |
| run | 1 | 1 | `job-20261028T065807Z-fe95414d` | FAIL foot_slide 34 px SE/4 | 711 | rejected |
| run | 2 | 1 | `job-20261028T071339Z-c47280b7` | PASS | 696 | escalated; later **selected by the owner** |
| run | 3 | 2 | `job-20261028T182241Z-c44e6a17` | FAIL foot_slide 37.5 px, bbox_drift 38 px | 1027 | rejected |
| run | 4 | 2 | `job-20261028T184607Z-178fd61c` | FAIL bbox_drift 29/33 px E | 927 | rejected (rifle also lost E frames 3-5) |
| run | 5 | 2 | `job-20261028T190439Z-373b9723` | FAIL bbox_drift 26 px S/2 (1 px over) | 730 | rejected; visually best (rifle held E/SE, pops N/S) |
| idle | 0 | 1 | `job-20261028T072810Z-e7d30ee5` | PASS (1 warning) | 694 | selected |

Totals (campaign_report): generation 5515.5 s, processing 389.2 s, validate/export 75.8 s, queue 6.8 s, wall
(union) 6025.5 s over 7 attempts; render null.

Owner interventions: "Grant 3 more seeds"; "(a) raise max_attempts_per_animation for gun's run to 6 (a profile
change and new campaign revision)" (implemented as an optional per-character row key, PR #64; campaign revision 2);
after the second escalation "Accept gun run seed 2 (job-20261028T071339Z-c47280b7) despite the rifle loss in E/W";
1x review "For my review I approve of the gun's sheet".

Final: composed `job-20261028T192433Z-79da77a1`, sheet sha256 `914dabb93b36fcc9…` (1760×1280); bundle
`job-20261028T210909Z-714085bf` READY_TO_INSTALL. Known defects accepted by the owner: rifle missing in most W/E run
frames; east idle loop seam 1.33 > 1.29 (warning). Smoke render: 9/10 checks pass; `nearest_filtering` FAIL by 1 —
two dark colours' blue channel 7→0 in the engine (tolerance 6), each colour still maps to exactly one rendered colour
(no blending). The threshold was not changed. Failure: rifle continuity across directions is the recurring defect
(4 of 6 run seeds broke the bbox/foot limits on the rifle side).

## Thief — `camp-20261028T074353Z-10ecc94b` (rev 2) — READY_TO_INSTALL

| Clip | Seed | Job | Validator | Generation s | Outcome |
|---|---|---|---|---|---|
| run | 0 | `job-20261028T074425Z-9fa9090d` | PASS | 568 | selected |
| idle | 0 | `job-20261028T171259Z-cea89f19` | PASS (3 loop warnings) | 585 | selected |

Totals: generation 1152.6 s, processing 75.5 s, validate/export 24.3 s, queue 1.1 s, wall 1263.5 s, 2 attempts.
Selections by the fresh `/new-class` agent session (DYL-130). Owner 1x review: "For my review I approve of the
theif's sheet" — recorded on the recompose `job-20261028T184451Z-b77a5363`, whose sheet is byte-identical
(sha256 `4d12372ac29c7074…`) to the one the owner viewed (needed after the gun profile change; see the profile-row
binding fix, PR #65). Bundle `job-20261028T193409Z-03a9f3c1` READY_TO_INSTALL. Smoke: all 10 checks pass (after the
runtime-dir fix; the first smoke attempt failed on the 108-byte socket path and was preserved). Caveats noted for the
owner: south idle head tilt pop; low-contrast dark leathers at 1x.

## Engineer — `camp-20261028T171325Z-8f7821e3` (rev 3) — READY_TO_INSTALL

| Clip | Seed | Job | Validator | Generation s | Outcome |
|---|---|---|---|---|---|
| run | 0 | `job-20261028T171349Z-2b667313` | PASS | 591 | selected (queue 586 s behind thief idle) |
| idle | 0 | `job-20261028T173703Z-fe246c88` | PASS | 588 | rejected: cannon invariant |
| idle | 1 | `job-20261028T174921Z-a77b3fbc` | PASS | 587 | rejected: cannon invariant |
| idle | 2 | `job-20261028T180159Z-6ffa35aa` | PASS | 657 | escalated; **selected by the owner** |

Totals: generation 2423.2 s, processing 153.8 s, validate/export 35.3 s, queue 587.3 s, wall 3221.4 s, 4 attempts.
Owner interventions: "Accept seed 2 (Recommended)" for idle (known defects: south row raises the cannon overhead,
SE/SW hands empty); 1x review "For my review I approve of the engineer's sheet"; at delivery the build refused 1.78 m
(110.36 px) against the approved south still's 108 px, and the owner chose "Set height_m to 1.74 (Recommended)"
(campaign revision 3; `height_m` made delivery-only for candidate currency, PR #65). Composed
`job-20261028T212732Z-a038e63c`, sheet sha256 `5946674118677c78…`; bundle `job-20261028T215606Z-51047b63`
READY_TO_INSTALL (1.74 m, 108 standing px). Smoke: all 10 checks pass.

## Comparison and the 60-minute rubric

| | Gun | Thief | Engineer |
|---|---|---|---|
| First-pass success (seed 0 of both clips usable) | no (run seed 0 lost the rifle) | **yes** | no (idle seed 0 lost the cannon) |
| Final success | owner-accepted with defects after escalation | yes | owner-accepted with defects after escalation |
| Attempts / validator failures | 7 / 4 | 2 / 0 | 4 / 0 |
| Machine wall time, generation through export (union) | 100.4 min | 21.1 min | 53.7 min |
| Owner interventions after references | 4 (grant, cap raise, seed choice, review) | 1 (review) | 3 (seed choice, review, height) |

Against the 60-minute rubric: thief and engineer fit inside 60 minutes of machine time; gun does not. The rubric can't
be fully scored for any class: reference preparation (Codex generation plus three refprep iterations, shared across
all three classes) and owner hands-on time were not measured per class. They are reported as unknown, not zero. One
success (thief) is not a claim of general reliability: accessory continuity across directions failed in 2 of 3 classes.

Fresh-session resume: the fresh `/new-class` Code session (DYL-130 skill, the owner's MCP approval) adopted the thief
campaign with run seed 0 already generated. It decided that job without resubmitting (the ledger shows one
`submitted` event for thief run), then ran thief idle and the engineer campaign. Its claims were checked against the
job store and the ledger.

## Budgets and stops

Every escalation stopped and reported (gun run twice, engineer idle once); no seed was spent without budget, no limit
or threshold was edited, and the Phase 1 waiver was not inherited. The one profile change mid-phase (gun's attempt
cap) was an owner decision applied between campaigns' GPU work and forced revisions of all three campaigns.
