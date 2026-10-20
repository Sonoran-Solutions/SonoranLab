# SonoranLab — Implementation Plan

Phased, with an exit criterion per phase. A phase is not done until its exit
criterion is demonstrably met; do not start the next one early. Estimates
assume evenings/weekends on the Strix Halo box.

The plan deliberately front-loads the parts that could kill the project
(environment, model behavior) and back-loads the parts that are just
engineering (MCP server, orchestration). If Phase 1 says the models can't hold
a character across a walk cycle, the right move is to stop, not to build a
nicer wrapper around a broken core.

---

## Phase 0 — Environment (1 weekend)

Goal: a working, reproducible ComfyUI on the Strix Halo box that runs the
models this project depends on, offline.

Tasks
- [ ] Install the `kyuz0/amd-strix-halo-comfyui-toolboxes` image (ROCm 7 / TheRock nightlies, gfx1151). Do not hand-roll a ROCm env.
- [ ] Launch ComfyUI with the toolbox's recommended flags (`--disable-mmap` is required on gfx1151; the toolbox benchmarks use `--disable-mmap --gpu-only --disable-smart-memory --cache-none --bf16-vae`).
- [ ] Run the toolbox's stock **Qwen Image Edit 2511 (FP8) + Lightning** workflow once. Record seconds and peak memory in `benchmark/results/`.
- [ ] Run the stock **FLUX.2 klein-4B** workflow once. Record the same.
- [ ] Install custom nodes: `PixelOE` (ComfyUI node from the KohakuBlueleaf repo), `ComfyUI-PixelGridHelpers`, `comfyui_controlnet_aux`, `ComfyUI-GGUF`, `ComfyUI-RMBG` (BiRefNet + SAM3).
- [ ] Download: Qwen-Image-Edit-2511, InstantX Qwen-Image-ControlNet-Union, FLUX.2-klein-base-4B, svntax combat LoRA (32×48), svntax small LoRA (32×32), Wan 2.2 I2V 14B (Q5_K_M GGUF) + Lightx2v distill LoRA, BiRefNet, SAM3. Record every file's SHA and license in `docs/04-models-and-licensing.md`.
- [ ] Pull the network cable (or `unshare -n`) and re-run the Qwen Edit workflow. It must succeed.
- [ ] If any model emits black/NaN images: try `--force-fp32` for that workflow and note it. Known gfx1151 issue, not a model problem.

Exit criterion
- One Qwen Edit 2511 image and one klein-4B image generated **offline**, with timing recorded. The box can be rebooted and get back to this state from written notes in under 30 minutes.

---

## Phase 1 — Benchmark the model behavior (2–3 weekends)

Goal: find out which generation track can hold a character's identity and
accessories across a 4-direction walk cycle, and how long it takes. This phase
produces a decision, not code.

Full protocol: `docs/05-pipeline-tracks.md` and `benchmark/README.md`.

> **ADR-0007 (2026-10-18):** the game has no walk clip and no 4-direction or 32×48 sprites. Phase 1 measures a
> **run cycle in the game's format**: 5 generated directions with the west side mirrored, per-character cells at
> 62 px/m, and the shipped palette. Read "4-dir walk" below as that run cycle. References:
> `benchmark/characters/2026-10-18.yaml`.

Inputs: three approved reference characters from Twos Are Up:
1. a humanoid with a one-handed weapon (asymmetric),
2. a humanoid with a large silhouette-changing accessory (cloak / big hat / shield),
3. a non-humanoid (slime, quadruped, or floating thing).

Tracks (run every one, even the bad ones; the baseline numbers matter):
- **A — Grid LoRA baseline.** Qwen Edit 2511 + svntax combat LoRA, 768² → 4× down. One day. Keep every output.
- **B — Small-grid baseline.** klein-base-4B + svntax small LoRA (2750 and 3000-step weights), 512² → 128². Half a day.
- **C — Pose-conditioned frames.** Qwen Edit 2511 + InstantX ControlNet-Union (pose, then depth). Character ref + six hand-placed OpenPose frames. **This is the decisive test**: does the model obey a skeleton at sprite proportions?
- **D — 3D-baked rig, raw.** Godot orthographic bake of a rigged humanoid walk (Mixamo-style animation), 4 directions. No AI. Measures what a template alone gives you; the frames become C's conditioning and E's seed.
- **E — HD-animate-then-pixelize.** Clone of the mor-o W1→W2→W3 pattern: pose edit → Wan 2.2 I2V (base model, **not** the pixel-animate LoRA) → BiRefNet alpha → frames. Then PixelOE k-centroid 4× + palette lock, in code. **Expected winner.**

Post-processing applied identically to every track (see `docs/08-validation.md`):
PixelOE (k-centroid, 4×) per frame → Enforce Palette (project HEX list) → alpha threshold + fringe clean → foot-anchor registration → validator.

Metrics per track (sheet in `benchmark/scoring.csv`):
- minutes of generation to first candidate
- minutes of human cleanup to a usable 4-dir walk cycle (the headline number)
- % frames needing manual fixes
- back-row (facing-up) accessory survival, yes/no per accessory
- foot slide (px) across the cycle, from the validator
- off-palette pixel count before enforcement
- peak memory, wall time

Exit criterion
- A written decision in `docs/decisions/ADR-0004-lead-track.md` naming the lead track and the fallback, with the scoring sheet attached. Any track that can't produce a usable walk cycle for character #1 in under 60 minutes total is out.

---

## Phase 2 — Deterministic core (2 weekends)

Goal: the `sonoranlab` Python package: everything that happens after a model
emits pixels. No ComfyUI dependency in this phase; inputs are PNGs.

Package layout
```
sonoranlab/
  profile.py     load/validate profile.yaml (docs/07)
  pixelize.py    PixelOE wrapper: per-frame outline expand + k-centroid downscale
  palette.py     enforce palette, report off-palette pixels, optional dither
  alpha.py       threshold, fringe/halo removal, matte against bg color
  slice.py       sheet → frames (grid or explicit cell boxes), frames → sheet
  register.py    foot-anchor / rig-anchor alignment, canvas normalization
  validate.py    rules from docs/08, returns a structured report
  contact.py     zoomed contact sheets for agent/human review
  export/godot.py  SpriteFrames .tres + sheet PNG (docs/09)
  cli.py         `sonoranlab <verb>` for each of the above
```

Tasks
- [ ] Profile schema + loader, with the Twos Are Up profile as the first real instance.
- [ ] `pixelize` wrapping the `pixeloe` library directly (not via ComfyUI) so it runs anywhere, including on the 3080 box or a laptop.
- [ ] `palette`, `alpha`, `slice`, `register` with golden-image tests on synthetic 4×4 sheets.
- [ ] `validate` producing JSON: frame count, bbox per frame, foot-line drift, off-palette count, clipped-edge detection, alpha fringe count, pass/fail against profile thresholds.
- [ ] `contact` producing an 8× nearest-neighbor contact sheet with frame indices and a per-frame validator badge.
- [ ] Godot export, verified by opening the result in Godot 4 and playing the animation.
- [ ] CLI pipeline: `sonoranlab process raw_sheet.png --profile profiles/twos-are-up.yaml --anim walk_4dir --out artifacts/x/` runs the whole chain.

Exit criterion
- A raw 768² sheet from Phase 1 goes through the CLI and comes out as a validated Godot `SpriteFrames` that plays correctly in the engine, with a validation report, in one command.

---

## Phase 3 — Generation adapters (2 weekends)

Goal: SonoranLab can drive ComfyUI for the lead track and one fallback.

Tasks
- [ ] `comfy/client.py`: submit API-JSON to `/prompt`, poll `/history`, fetch outputs, interrupt, free memory. Retries and timeouts tuned for minutes-long jobs.
- [ ] `workflows/<track>/*.json` committed as API JSON with a sidecar `params.yaml` naming the patchable inputs (reference image path, control image path, seed, prompt, steps, resolution).
- [ ] `comfy/adapters/`: one adapter per track with a common interface `generate(request) -> RawResult`. Implement the lead track and Track A (cheap baseline) first.
- [ ] Pose template library: `templates/poses/<anim>/<dir>/<frame>.png` (OpenPose renders) produced from the Phase 1 Track D bake, plus the renderer script that made them.
- [ ] Seed sweep + candidate selection helpers: run N seeds, process all, rank by validator score, keep everything.
- [ ] `sonoranlab animate --profile ... --character <id> --anim walk_4dir --track <lead>` end to end.

Exit criterion
- One command produces a validated, exported 4-direction walk cycle for a new character reference with no manual steps. Time and memory recorded.

---

## Phase 3.5 — Aseprite targeted-repair pilot (optional)

Goal: find out whether a contained Aseprite editor can make small, verified pixel repairs to an approved job and hand
the result back through the existing validator, exporter and human review. Pilot only; design in
[`docs/decisions/ADR-0009-aseprite-repair-pilot.md`](docs/decisions/ADR-0009-aseprite-repair-pilot.md).

Tasks (Linear)
- [x] DYL-106: ADR-0009, pinned environment and containment smoke (`benchmark/results/2026-10-20-phase3.5/`).
- [x] DYL-107: immutable repair workspaces and a lossless Aseprite round trip (`sonoranlab repair prepare|export|diff`).
- [x] DYL-108: single-writer editor sessions and bounded, verified pixel-edit transactions (authorization, broker).
- [x] DYL-109: finalize repaired frames into a new validated child job with a fresh bound review; review-metadata hardening (core, kept whatever the disposition).
- [x] DYL-110: three measured trials (round trip, targeted repair, small animation repair), at most 2 repair iterations each.
- [x] DYL-111: gate: evidence package and owner disposition.

Exit criterion (met 2026-10-20: owner disposition **ADOPT, narrow and optional**; Phase 4 GO)
- An owner disposition (ADOPT, DEFER, REJECT or HOLD) recorded at DYL-111. Phase 4 waits for that disposition, not for
  an Aseprite success. The deterministic pipeline must work without Aseprite.

---

## Phase 4 — MCP server (1 weekend)

Goal: the tool surface in `docs/06-tool-surface.md`, so an agent can drive the
pipeline.

Tasks
- [ ] `sonoranlab/mcp/server.py` using the official `mcp` Python SDK. Each tool wraps a CLI verb.
- [ ] Job model: generation tools return a `job_id` immediately; `job_status` / `job_result` poll. Long jobs do not block the agent.
- [ ] Artifact store: `artifacts/<project>/<character>/<anim>/<job_id>/{raw,processed,report.json,contact.png}`.
- [ ] Project state: `list_characters`, `get_character` (approved refs, existing sheets, validator history).
- [ ] Register the server in Claude Code's MCP config; smoke test from a Claude Code session.
- [ ] Only if Phase 3.5 is adopted: wrap the scoped repair handoff (DYL-118). Otherwise skip.

Exit criterion
- From a fresh Claude Code session, the agent calls `generate_character` → `animate` → `validate` → `export_godot` and a walk cycle lands in a Godot project, with the agent only ever seeing tool results and contact sheets.

---

## Phase 5 — The "new class" orchestration (1 weekend)

Goal: the actual thing you wanted. Hand Opus reference images and a class
spec; get a class in Twos Are Up.

Tasks
- [ ] A Claude Code skill / command (`/new-class`) encoding the loop in `docs/10-orchestrator-loop.md`: profile → candidates → (human or agent) pick → animations in priority order → validate → export → in-engine smoke render.
- [ ] Retry budget and escalation rules: how many seeds before changing track, when to stop and ask.
- [ ] Godot headless smoke render: a scene that instantiates the new SpriteFrames next to an existing class and screenshots it, returned to the agent for a final style-drift check.
- [ ] Run it three times on three new classes. Log where it needed a human.

Exit criterion
- A new class with static art and a 4-dir walk cycle goes from references to the running game without opening Aseprite/Pixelorama. Documented time and token cost per class.

---

## Phase 6 — Depth (ongoing)

In rough priority order, each gated on the previous phases holding up:
- Attack / hurt / idle templates (depends on Phase 1 Track C results for weapon arcs).
- Layered cosmetics: body + gear sheets, pixel-aligned, the mor-o W4/W5 pattern (VACE inpaint + SAM3 segmentation). This is the real win for an RPG with equipment.
- Eight directions from the Track D bake.
- A project LoRA on approved Twos Are Up art (klein-4B, trainable on a rented 24 GB GPU in about an hour). Test on both base and distilled; the BFL guidance and the svntax guidance disagree.
- Qwen-Image-Layered as an alternative alpha/gear separator (GGUF quants exist; it's a 20B-class model, so it's an experiment, not a default).
- Review UI, only if the contact-sheet + Pixelorama loop proves too slow.

---

## What would make us stop

- Phase 1: no track produces a usable walk cycle for a simple humanoid in under 60 minutes of total effort, after seed sweeps. Then the honest answer is "keep the subscription for now, revisit when models improve," and the deterministic core from Phase 2 is still worth building for processing hosted outputs.
- Phase 0: the Strix Halo ROCm stack can't run Qwen Edit 2511 or klein-4B reliably. Fallback is the 3080 for static work only, and the project scope shrinks to "static class art + pixelize/validate/export."
