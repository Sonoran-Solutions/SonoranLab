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
