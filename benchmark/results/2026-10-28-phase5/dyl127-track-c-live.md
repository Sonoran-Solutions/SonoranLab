# DYL-127 — live Track C `pose_frame` over MCP (2026-10-28)

Owner window: the owner stopped the ComfyUI server inside the `strix-ai` container and kept it off for Phase 5
("It stopped the comfyui server that the container was using, I will keep it off for the remainder of phase 5 work
here"). No service was paused by SonoranLab; the guard was not bypassed; GTT credit stayed off.

## Setup

- Code: retained checkout `~/src/SonoranLab-p5live`. Attempt 1 at `ad92e9d` (main + DYL-125); attempt 2 at
  `242afa2` (integration: ADR-0011 + DYL-124/125/126/128 + the pose_frame control fix `28e3c6b`).
- ComfyUI 0.31.0 in `sonoranlab-comfy` (`env/comfy.sh up --offline`, `--network=none`, custom-node whitelist).
- Client: fresh stdio MCP client (`mcp` SDK, `sonoranlab mcp serve`), log `~/sonoranlab-work/phase5/dyl127/mcp.log`.
- Request: pyromancer, approved reference set `refs-bd2b1deda344` (Phase 4), direction east, template
  `run/east/pose_000`, seed 1. Shipped Qwen Edit 2511 adapter (not the Qwen 2.1 spike, GitHub #40).
- Guard dry run before submission: need 60 GiB + 8 GiB margin, available 78.4 GiB, nothing paused.

## Attempt 1 — FAILED before GPU (Phase 4 defect)

`job-20261028T054353Z-d7b078cd`: `generation_failed` in 8 s; worker log
`track C request: control_image is 544x544, expected a square multiple of cell 144`. The repository pose PNGs are
rendered at 544 px (the 136-cell gen canvas), so MCP `pose_frame` could only work for cell-136 characters. ComfyUI
queue and history were empty afterwards: no prompt was submitted. Fix `28e3c6b` renders the OpenPose control from
`template.yaml` joints for the character's cell (golden comparison against the 30 committed PNGs: 9–32 differing
pixels of 5.1k–6.6k lit per frame, IoU ≥ 0.9958, joint-centre colours identical; not byte-identical).

## Attempt 2 — execution PASS

`job-20261028T055858Z-b223fd5c`, submitted 05:58:59Z, `succeeded` by 06:02:07Z (about 3 min including model load).

| Check | Result |
|---|---|
| Admission at 60 + 8 GiB, lease | guard admitted (78 GiB available), no pause |
| Durable prompt id | `ac6cce84-1116-45ca-a826-5f02a5ae9f91` (`attempt-1/submission.json`) |
| Polling | `job_status` running → succeeded; no resubmission |
| Image ordering | `source/reference_1024.png` (still), `source/pose_1024.png` (control), outputs `image_rgb/raw_00001_.png`, `image_rgba/rgba_00001_.png` |
| Output | RGBA 432×432 (cell 144 × 3) |
| Model inventory | Qwen Edit 2511 FP8, Qwen VAE, Qwen 2.5-VL 7B FP8, Lightning 4-step LoRA and all four BiRefNet files with sha256; `model_provenance.birefnet.verified: true` (birefnet.py is the node's recorded load-time rewrite, B1a) |
| Queue drain / service state | queue empty after the job; `strix-ai` untouched; container left running for later Phase 5 work |

Memory, sampled every 2 s from `/proc/meminfo` and `mem_info_gtt_used` (129 samples, 05:58:52–06:03:09Z, whole host):
MemAvailable 80.5 → minimum 27.2 GiB (about 53 GiB drawn at peak); GTT peak 34.7 GiB. Per-attempt peak inside the
container: NOT_MEASURED.

## Review of the output (stated purpose: one posed frame)

- Facing (east), pose (run contact, legs and arms follow the skeleton), identity (hair, coat, orange sash) and the
  staff are retained — coordinator's visual inspection of `review_ref_pose_output.png`.
- **Technical suitability: not usable at sprite scale as-is.** The figure fills the 432 px canvas (about 2.5× the
  template's scale), so it does not keep the cell's 62 px/m registration; it would need re-registration before it
  could enter a sheet. No validator run (single frame, not a clip).
- Owner judgement: not requested (optional route, not used by any Phase 5 campaign).

## Disposition

The MCP lifecycle for Track C is live-verified (admission, durable prompt, polling, provenance, queue drain) at
`242afa2`. The route stays **disabled in `/new-class`** for Phase 5: the pilot clips are idle and run (Track E), and
the output's scale defect means a posed frame is not a drop-in sheet frame. No automatic C→E fallback exists.
