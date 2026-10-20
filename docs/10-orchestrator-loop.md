# 10 — The orchestrator loop ("make me a new class")

How an agent (Claude Code running Opus, or any MCP client) turns reference
images plus a class description into a class in the game. This becomes a
Claude Code skill (`/new-class`) in Phase 5. The loop is deliberately boring:
the agent's job is judgment and retries, not cleverness.

## Inputs

- `project` (e.g. `twos-are-up`)
- 1–4 reference images (concept art, a sketch, an existing sprite to riff on)
- a short spec: name, role, weapon/accessories, silhouette notes, palette
  notes ("uses the purple ramp, no red")
- which animations are required, in priority order (default:
  `walk_4dir`, `idle_4dir`, then `attack_4dir`, `hurt_4dir`)

## The loop

```
1. profile = get_profile(project)
   siblings = list_characters(project)                 # for style comparison and to avoid redoing work
   character = register_character(project, name, refs, notes)

2. STATIC CANDIDATES
   job = generate_character(project, character, spec, n=8, track="A")
   wait(job); res = job_result(job)
   look at res.contact                                  # 8× zoom, badges
   compare against 2 siblings' approved refs           # style drift, proportions, outline
   if autonomy.allow_auto_approve: approve_candidate(best)
   else: present contact + reasoning to the human; STOP until approved

3. FOR each animation in priority order:
     attempts = 0
     track = profile.animations[anim].track_default
     LOOP:
       job = animate(project, character, anim, track, seed=next_seed())
       wait(job); res = job_result(job)
       if res.report.pass and looks_right(res.contact): break
       read res.report.failures                         # "foot_slide: frame 9 (down/1) slid 3px"
       if failures are localized (≤ 2 frames, one direction):
           pose_frames(..., frames=[9]) or animate(..., directions=["down"])   # surgical retry
       else:
           attempts += 1
           if attempts < autonomy.max_attempts_per_animation: new seed, continue
           elif autonomy.escalate_after == "switch_track": track = fallback(track); attempts = 0
           else: escalate to human with the two best contacts and the reports; STOP

     export_godot(project, character, anim, res.processed)

4. smoke_render(project, character, "walk_4dir", beside=closest_sibling)
   look at the screenshot; if the new class reads wrong next to the sibling, say so and stop.

5. Report: per-animation job ids, seeds, attempts, total wall time, what needed a human.
```

## Rules the agent follows

- **Never approve its own static candidate unless the profile allows it.**
  The approved reference drives everything downstream; a bad one wastes an
  hour of GPU time.
- **Read the report before looking at the picture.** Numbers first. The
  contact sheet is for "does it read," not for finding a 1-px fringe.
- **Retry surgically before retrying globally.** One bad frame means
  `pose_frames(frames=[i])`, not a new seed for the whole cycle.
- **Switch tracks as a strategy, not as a reflex.** Track E drifting on an
  accessory → Track C with the same seed frame. Track C ignoring the pose →
  Track E from a Track D bake. Record why in the final report.
- **Budget.** `autonomy.max_seeds_per_attempt × max_attempts_per_animation`
  bounds GPU time per animation. When it runs out, stop and ask; do not keep
  rolling.
- **Don't touch the profile mid-run.** If the palette or canvas seems wrong,
  that's a finding for the human, not a parameter to tweak.
- **Everything is reproducible.** The final report lists job ids; anyone can
  re-run `process` on any raw set.

## What the agent sees per step

- JSON results with `pass`, `failures`, timings, paths.
- Contact sheets (8× nearest-neighbor, frame indices, validator badges).
- Smoke render screenshot.
- Nothing else. No ComfyUI UI, no graph, no model names unless it asks via
  `job_result.manifest`.

## Expected rhythm (Strix Halo)

This is a background job, not a chat. A walk cycle on Track E is minutes of
generation per direction plus seconds of processing, times seeds. Kick it
off, do something else, come back to a validated sheet in the Godot project
and a short report. If it needed a human, the report says exactly where.

## Why this beats the hosted loop

Not per-call quality (it won't, at first). Project memory: the loop compares
every new class against approved siblings, knows which seeds and tracks
worked for similar silhouettes, and can re-run any step from `manifest.json`.
The hosted service forgets everything between calls.

## Seed and loop-window sweeps

`sonoranlab sweep plan --character C --anim run --directions north east --seeds 1 2
--stills-dir STILLS --out PLAN` writes Track E request JSONs without generating.
`sweep run --requests PLAN/requests/*.json [--exec CONTAINER]` runs each request
serially through the run-window guard (40 GiB by default). Nonzero exits are
reported and retained; guard refusal/busy exits 3/4 stop the remaining requests.

`sweep rank --profile P --character C --anim A --attempts ATTEMPT [ATTEMPT ...]
--carrier RAW --out OUT [--windows steady]` scores each clip/window with the
other directions supplied by RAW (`<direction>_<i>.png`, HD). Default windows
use the loop selector's pick; steady also tries every distinct start from frame
6 with the same period and an available seam endpoint. Ranking is stable by
pass, core failures, total failures, worst gated value/limit, thin-part pixel
count CV, loop seam score, then generation seconds. Thin-part CV measures
pixels removed by a 5×5 opening of the selected HD alpha masks.

Every candidate job stays in `OUT/candidates/`; `OUT/sweep.json` records scores,
chosen/rejected reasons and each unqualified direction's best blocking reasons.
The best candidates are copied into `OUT/final/raw-set/` and the unchanged
pipeline validates the whole set in `OUT/final/job/`. Only that final verdict
sets `technical_pass`; ranking exits 0 for pass, 1 for validation failure, 2 for
bad input. Sweep outputs always have `production_usable: false`.

Human review is a separate, exclusive record:
`sonoranlab review record OUT/final/job --verdict pass --reviewer NAME
--cleanup-minutes 5 --started ISO --ended ISO [--notes TEXT]`.
A failed technical job cannot be reviewed. A passing review needs non-negative
cleanup time; `review.production_usable(job)` requires both technical and human
passes. No sweep score claims production quality, and no output is overwritten.

## animate

`sonoranlab animate --profile P --character C --reference STILLS [--anim run] [--seeds 1,2]
--out OUT [--exec sonoranlab-comfy] [--need-gib 40] [--windows default|steady] [--godot-check]`
is one command from approved stills to a validated export. `STILLS` needs a cell-size
RGBA `still_<d>.png` for every generated direction; Track E only (a Track C first frame
is refused). It copies the stills to `OUT/reference/`, plans and runs one guarded Track E
generation per direction and seed (`OUT/gen/`, failures kept and counted, never retried),
builds a carrier from each direction's first good clip (`OUT/carrier/`), runs the sweep
(`OUT/sweep/`) and lets the final `pipeline.run` export into `OUT/sweep/final/job/export/`
only on a validator PASS. Nothing is ever written under the profile's `export.godot_project`.

`OUT/animate.json` records inputs and hashes, every request's guard exit and generation
seconds, counts (`first_pass_ok_rate` = ok / attempts run), the chosen candidate per
direction, the final verdict, export paths and the Godot check. `timing` has `prepare`,
`generate`, `select_and_process`, `validate_export` and `total` in seconds
(`validate_export` is the final job's own total, taken out of `select_and_process`).
Peak memory is not sampled here; the orchestrator measures it externally.

Exit: 0 technical PASS exported, 1 validator FAIL / a direction with no good clip, 2 usage
or processing error (usage errors write nothing), 3 the run-window guard refused (3 or 4).
`production_usable` is always `false`: time cleanup with a stopwatch and record it with
`sonoranlab review record OUT/sweep/final/job ...`.
