# ADR-0011 — Phase 5 new-class contract

**Status:** accepted 2026-10-29 with amendments (owner, after the Phase 5 senior review: "record that I agree with
the reviewer's answers to the open questions for me"). Proposed 2026-10-28 (DYL-123).

**Amendments at acceptance.** (1) Scope stays **visual asset delivery**: a delivered sheet is not a playable class,
and READY_TO_INSTALL is not an installation; installing any bundle remains a separate owner authorization per bundle
and target revision. (2) The 60-minute end-to-end criterion is **not declared passed for any class**: reference
preparation and owner hands-on time were not measured; thief and engineer met the machine-time component, gun did
not. (3) The owner-decision table below records the decisions actually given during Phase 5.

## Context

PLAN.md's Phase 5 and `docs/10-orchestrator-loop.md` were written before Phases 1-4. They assume
`generate_character` (Track A static candidates), `walk_4dir`, surgical `pose_frames` retries and an automatic
track switch. None of those is the shipped contract:

- ADR-0007: the game format is eight sheet rows, five generated directions with the west animation rows mirrored,
  eight independent stills in column 0, per-character square cells at 62 px/m, the shipped palette, no baked
  sticker border (`PixelStandee.gdshader` draws it).
- ADR-0004: Track E leads locomotion; Track A is not a production default; Track C is a held-pose fallback
  for single frames.
- ADR-0010 (Phase 4, **still marked proposed** although PRs #41-#51 were merged with the owner's "approved to merge"
  on 2026-10-21): `animate` (Track E) and `pose_frame` (Track C, one posed frame) are the only generation tools.
  `generate_character` and automatic C→E fallback were never implemented. This ADR does not accept ADR-0010 on the
  owner's behalf; it asks for that decision (§ Owner decisions).
- Phase 4 senior review left three P2 findings (DYL-124 test race, DYL-125 BiRefNet provenance, DYL-126
  character history); they close before the Phase 5 trials.

## Decision

### Scope

Phase 5 delivers **visual class assets and their game-compatible handoff**: a validated sheet, its manifest
fragment, provenance and a reversible delivery bundle. It does not implement abilities, balance, progression,
HUD, icons or playable-class registration in Twos Are Up. A sprite sheet is not a playable class, and a staged
engine test is not an installation.

### Entrance: class request + eight approved stills

The supported entrance is a **class request** (`sonoranlab.class_request/1`) plus **eight owner-approved,
final-resolution, independent directional stills** (`still_<direction>.png` for every direction in
`directions.order`, cell size, binary alpha, shipped palette, ≤ `max_colors_per_cell`, foot on `cell - 4`,
padding ≥ 2, no baked border). The class request carries: project, stable new character id and display name,
visual role, silhouette text, weapon/accessory invariants, palette notes, required clips in priority order, the
reference provenance and owner decision references. Gameplay fields are informational.

- Concept-only, partial or invalid input yields **`needs_reference_preparation`** with the exact missing or failing
  items and a review contact sheet. Nothing fabricates a missing view, mirrors an independent west still, or calls a
  model to fill the gap.
- **Reference preparation is a separate, owner-gated step outside the SonoranLab inference stack.** Its output is a
  candidate still set; the owner approves exact hashes with the owner-only `sonoranlab registry approve-references`.
  Preparation time is recorded separately from animation time.
- Supported preparation routes in Phase 5: (a) owner-supplied stills; (b) **owner-directed external image
  generation** (owner decision 2026-10-28: "part of this phase will need to have codex create the reference
  images") producing HD turnaround art that a deterministic converter (`sonoranlab refprep`, code-only pixel math:
  crop, matte, downscale, palette lock, alpha, foot registration, cell sizing) turns into candidate stills. The
  generator's terms and every prompt/output hash are recorded in the case provenance; the game already uses OpenAI
  images as design references (its `SOURCE.md`). This is **not** a SonoranLab static generator: it is not exposed as
  an MCP tool, not in the default model stack, and its outputs carry no approval until the owner gives one.
- A model-backed static-candidate or directional-reference generator inside SonoranLab requires its own reviewed
  scope and license decision (ADR-0006 gate). Track A is not revived; GitHub #40 (Qwen Image 2.1) stays an
  independent spike and changes no default.

### What `pose_frame` can and cannot do

`pose_frame` (Track C, 60 GiB + 8 GiB margin) produces **one posed frame** from an approved still and a pose template.
It cannot produce a still set, a clip, or a missing direction, and it is not invoked by `/new-class` unless DYL-127's
live MCP verification passed and the owner authorized it for that campaign. There is no automatic C→E fallback.

### Pilot clips (frozen)

`run` (6 frames, 10 fps) and `idle` (4 frames, 5 fps), both Track E, per character. The game's consumer
(`PixelStandee`) picks `idle` when standing and `run` when moving, so both are required for the sheet to display;
it picks `cast` while casting or shooting, so a Phase 5 sheet without `cast`/`death` is reported as a **visual
delivery with known missing clips** in the bundle, never as a playable class. Attack/cast/death/hurt, rigging,
training and UI are Phase 6 or later.

**Amendment 2026-10-29 (gun only).** Owner request: "prioritize producing the Gun's cast and death clips ... Twos are
up is ready to implement the Gun class and needs those animation". Owner choices: death rules "New 'fall' rules
(Recommended)"; scope "Gun row only (Recommended)"; budget "≤3 seeds per clip (Recommended)"; prompts "Proposed text
(Recommended)". The gun row (`characters.gun.animations`) makes `cast` (8 frames, 10 fps, loop false) and `death`
(6 frames, 8 fps, loop false) Track E with profile `prompt_motion` text, and `death` uses `validator_mode: fall`
(ADR-0008 fall addendum). A class request may list a clip whose *effective* track for that character is E; every
other character keeps the frozen pilot clips (`run`, `idle`), and Track E stays the only supported track. Non-loop
clips start at the approved still (frame 0) and are sampled to an end frame chosen in code (`loop.select_once`). A
campaign revision may add clips: the request's `clips` list is plan-only and the character row is bound per clip, so
the gun's approved run/idle selections stay current while cast/death get the owner's 3-seed budget. No hold field is
emitted (the game holds a non-loop clip's last frame); delivery needs every planned clip and the exact request.

Every exported sheet: eight rows in profile order; five generated directions; north-west/west/south-west animation
rows mirrored from north-east/east/south-east; column 0 = the eight approved stills, pixel-exact; unchanged palette,
alpha and validator limits.

### Profile rows

A new character needs a reviewed `characters:` row (cell, anchor_mode, gates). The row is proposed as a reviewable
patch (default limits 30/25, as the pyromancer's, unless the owner decides otherwise) and merged/approved **before**
a live campaign uses it. Profile and validator limits are never edited during a campaign; a profile change is a new
campaign revision.

### Campaign states and gates

```
NEEDS_OWNER_INPUT ─▶ NEEDS_REFERENCE_PREPARATION ─▶ REFERENCES_REGISTERED ─▶ AWAITING_REFERENCE_APPROVAL
   ─▶ AWAITING_PROFILE_ROW ─▶ READY ─▶ GENERATING ─┬─▶ BLOCKED (unknown outcome / guard / infra; reconcile, no retry)
                                                  ├─▶ HOLD_BUDGET (budget exhausted; escalate with best two)
                                                  └─▶ TECHNICAL_PASS ─▶ AWAITING_OWNER_REVIEW ─▶ ARTIFACT_APPROVED
   ─▶ STAGED_AND_ENGINE_VERIFIED ─▶ READY_TO_INSTALL ═▶ INSTALLED (separate owner authorization per bundle + target rev)
```

Five separate verdicts are never merged: reference approval (owner, hash-bound), technical validation (validator
and Godot check), artistic recommendation (agent, advisory), owner 1× review (artifact-bound `review record`), and
installation (owner authorization for one bundle and one target game revision). Default behaviour stops at
READY_TO_INSTALL.

### Budget and escalation

Start with one seed per required clip. The campaign budget is the profile's `autonomy` limits
(`max_seeds_per_attempt` × `max_attempts_per_animation`) or a stricter owner-approved budget, counted durably per
campaign across sessions, idempotency keys and candidate names. Reference changes create a new campaign revision
that keeps the consumed count visible. There is no surgical retry tool and no track switch: when the budget is
spent, or a failure is not fixable by re-selecting/re-processing existing outputs, escalate to the owner with the two
best candidates and their exact diagnostics. Guard refusal, infrastructure failure, cancellation and unknown prompt
outcomes are not artistic failures and never authorize a blind retry.

### Optional routes

Track C (DYL-127) and the Aseprite repair handoff (DYL-132) are disabled in `/new-class` until each has live MCP
evidence and the owner's authorization for the campaign. Aseprite stays limited to ADR-0009's bounded operations.

### Visual inspection

The agent must see pixels, not hashes. `/new-class` inspects contact sheets, side-by-side renders and stills through
a bounded, allowlisted, read-only MCP image view of artifacts already in STATE (no arbitrary path, no game
checkout).

### Trial selection

DYL-134 runs three owner-selected new classes. Owner decision 2026-10-28: **gun, thief and engineer** (Twos Are Up
F3 work), visual assets only; their mechanics designs are proposals in the game repository and are informational
here. Samurai, oathguard and pyromancer are regression baselines, not new classes.

## Consequences

- `/new-class` can be honest about what it cannot do: it stops at `needs_reference_preparation` instead of
  inventing art, and at READY_TO_INSTALL instead of writing the game.
- The external image generator sits before the owner's reference approval, so a bad reference costs owner review
  time, not GPU time. Its license position is a recorded owner decision, not a stack change.
- Without `cast`/`death`, the delivered sheets are not drop-in playable classes; that gap is a game task.

## Owner decisions

| # | Decision | State |
|---|---|---|
| O1 | Accept ADR-0010 | **accepted 2026-10-29** with the as-implemented amendment |
| O2 | Accept this ADR | **accepted 2026-10-29** with the amendments above |
| O3 | Trial classes gun, thief, engineer; Codex creates the references | given 2026-10-28 |
| O4 | Each class request and its eight-still reference set (hash-bound) | given 2026-10-28: "Approve all three" (gun-v3, thief-v3-dp6, engineer-v3); engineer `height_m` amended to 1.74 on 2026-10-28 ("Set height_m to 1.74") |
| O5 | Profile rows for gun/thief/engineer | given 2026-10-28: "Approve rows" (gun 160, thief 144, engineer 144, default limits); gun `max_attempts_per_animation: 6` on 2026-10-28 |
| O6 | Live generation budget per class | given 2026-10-28: "≤3 seeds per clip"; gun run +3 ("Grant 3 more seeds") |
| O7 | Track C live window (DYL-127) | given 2026-10-28: host ComfyUI kept off for Phase 5 |
| O8 | Game read + private staged copies + disposable snapshot (DYL-131/133) | given 2026-10-28 |
| O9 | One bounded live repair edit (DYL-132) | given 2026-10-28 for a pyromancer copy (refused at admission); retargeted 2026-10-29 to a disposable copy of an approved Phase 5 thief job |
| O10 | 1× review of each final sheet | given 2026-10-28 for thief, gun (run seed 2 accepted with E/W rifle loss) and engineer (idle seed 2 accepted with S/SE/SW cannon defects) |
| O11 | Installation of any bundle | **HOLD** (2026-10-29): rebuild against the current game revision in a fresh private snapshot, resolve the shared `characters.json` changes, retest the combined result, then authorize each final bundle separately (thief, engineer, gun last) |
| O12 | Missing `cast`/`death` | 2026-10-29: game-side explicit fallbacks short term (documented contract, runtime tests, gallery contract updated on purpose); dedicated clips long term; not "playable" until consumer and runtime tests pass |
| O13 | Historical artifact portability | 2026-10-29: content-hash provenance resolver approved and prioritized (no manifest rewrites, no inherited approvals, no external-directory allowlist) |
| O14 | Gun smoke `nearest_filtering` (blue 7→0, tolerance 6) | 2026-10-29: investigate with a controlled 1×/4×, with/without-shader comparison before any waiver; global tolerance stays 6; rifle continuity stays a separate known defect |
