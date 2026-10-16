# 06 — Tool surface (MCP)

This is the only thing the orchestrator sees. Design rules:

- **Coarse, idempotent tools with structured results.** The agent is good at
  judging, retrying, and reading JSON. It is bad at assembling ComfyUI graphs
  and cannot see sub-pixel problems. So: no graph access, every result carries
  numbers, every visual review goes through an 8× contact sheet.
- **Long work returns a `job_id` immediately.** Generation on ROCm is
  minutes, not seconds. `job_status` / `job_result` poll. Nothing blocks.
- **Every tool is a CLI verb first.** `sonoranlab <verb> --json` produces the
  same payload the tool returns, so everything is testable without an agent.
- **Results reference artifact paths, never inline pixels.** The agent asks
  for a contact sheet when it wants to look.

Transport: official `mcp` Python SDK, stdio for Claude Code. Server name
`sonoranlab`.

## Project / profile

### `list_projects()`
→ `[{id, name, profile_path}]`

### `get_profile(project)`
→ the resolved profile (`docs/07`): canvas, palette, foot line, directions,
animations, approved reference list, export target.

### `list_characters(project)`
→ `[{id, name, status: "draft"|"approved", refs: [path], animations: {name: {status, job_id, report_summary}}}]`

### `get_character(project, character)`
→ full record including validator history and existing exported assets. The
agent uses this to avoid redoing work and to pick style-consistent siblings
for comparison.

### `register_character(project, name, refs: [path], notes?)`
→ `{character_id}`. Copies refs into the project; does not generate.

### `approve_candidate(project, character, candidate_path)`
→ marks a generated image as the canonical reference for later animation.
Humans call this via CLI; the agent may call it when `profile.autonomy.allow_auto_approve` is true.

## Generation (async)

### `generate_character(project, character, spec, n=8, track="A", seed?)`
Static candidates from refs + a text spec. Returns `{job_id}`.
Result: `{candidates: [{path, seed, validator: {...}}], contact: path}`.

### `pose_frames(project, character, animation, track="C", seed?, frames?: [int])`
Frames for an animation using the project's pose templates. `frames` lets the
agent regenerate specific indices. Returns `{job_id}`.

### `animate(project, character, animation, track="E", seed?, directions?: [str])`
Full animation; `directions` lets the agent redo one facing. Returns `{job_id}`.

### `regenerate_region(project, character, animation, frame, mask: path, prompt)`
Masked inpaint of one frame. Untouched pixels are preserved in code
(composited back from the original), not by prompt. Returns `{job_id}`.

### `job_status(job_id)`
→ `{state: "queued"|"running"|"done"|"failed", progress: 0..1, elapsed_s, message}`

### `job_result(job_id)`
→ track-specific result: `{raw: [path], processed: [path], report: {...}, contact: path, manifest: {...}}`

### `cancel_job(job_id)`

## Deterministic (sync, fast)

### `process(project, input: path|[path], animation, profile_overrides?)`
Runs the whole chain (`docs/05`) on raw frames or a raw sheet.
→ `{processed: [path], report: {...}, contact: path}`

### `validate(project, frames: [path], animation)`
Validator only (`docs/08`). → report JSON. Use when the agent edited frames
externally or wants to compare two candidates.

### `contact_sheet(frames: [path], zoom=8, labels?: [str])`
→ `{path}`. The agent's eyes.

### `compare(project, a: [path], b: [path], animation)`
→ side-by-side contact + both reports + a diff summary (which frames changed,
palette/bbox/foot deltas). Used for "did the retry help."

### `export_godot(project, character, animation, frames: [path], target?: path)`
→ `{sheet_png, spriteframes_tres, scene_tscn?, imported_at}`. See `docs/09`.

### `smoke_render(project, character, animation, beside?: character_id)`
Headless Godot render of the exported animation next to an existing class;
returns a screenshot path and the engine's import log. The last style-drift
check in the orchestrator loop.

## Templates

### `list_pose_templates(project)`
→ `[{animation, directions, frame_count, path}]`

### `bake_rig(project, rig: path, animation, directions, frame_count)`
Track D: Godot headless orthographic bake → frames + OpenPose renders into
`templates/poses/`. Returns `{job_id}`.

## Result conventions

Every generation result includes:

```json
{
  "job_id": "…",
  "track": "E",
  "manifest": {
    "profile_hash": "…", "workflow_hash": "…", "models": {"name": "sha"},
    "seed": 1234, "timings": {"generate_s": 212.4, "process_s": 3.1},
    "peak_mem_gb": 41.2
  },
  "report": { "pass": false, "frames": 32, "failures": ["foot_slide: frame 9 (3px)"], "...": "docs/08" },
  "contact": "artifacts/twos-are-up/necromancer/walk_4dir/job-…/contact.png"
}
```

`report.pass` is the gate. `report.failures` is written for an LLM to read
and act on (which frame, which rule, by how much).

## What is deliberately not a tool

- Anything that takes a ComfyUI graph as input.
- "Set palette" / "set canvas" at call time. Those live in the profile; the
  agent edits the profile file if it needs to and the change is reviewable.
- Free-form shell. The orchestrator has Claude Code for that; this server is
  the pipeline, not a terminal.
