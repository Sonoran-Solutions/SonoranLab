# 03 — Hardware and environment

## Boxes

| Box | Role | Notes |
|---|---|---|
| Strix Halo (Ryzen AI Max+ 395, Radeon 8060S, large unified memory, Ubuntu) | **All generation.** ComfyUI, every model, every track. | gfx1151. ROCm via the kyuz0 toolbox. |
| RTX 3080 | Optional. Static SDXL/klein work, PixelOE on GPU, dev/test of the deterministic core. | Not on the critical path. Cannot run Track E (24 GB class). |
| Anything else (laptop) | Deterministic core, tests, docs, MCP server dev. | No GPU needed. |

Do not buy hardware for this project until Phase 1 says a track works and is
merely too slow.

## Strix Halo setup (Phase 0)

1. Install `kyuz0/amd-strix-halo-comfyui-toolboxes`
   (https://github.com/kyuz0/amd-strix-halo-comfyui-toolboxes). It is a
   Fedora toolbox image with ROCm 7 / TheRock nightlies built for gfx1151, with
   PyTorch, ComfyUI, and helper scripts in `/opt`. Record the image tag you
   pulled in `benchmark/results/phase0-env.md`.
2. Create/enter the toolbox per its README (it needs
   `--security-opt seccomp=unconfined`).
3. Launch ComfyUI with the toolbox's flags. **`--disable-mmap` is required
   on gfx1151.** The toolbox's benchmarks also use
   `--gpu-only --disable-smart-memory --cache-none --bf16-vae`; start from
   those and change one at a time.
4. If an image comes out black and the console shows a NaN cast warning at
   save time, that is the known gfx1151 bf16 NaN issue on some sampling paths.
   Try `--force-fp32` for that workflow. It is slower; note which workflows
   need it.
5. Expect Qwen Image Edit 2511 in bf16 to need >40 GB of system memory
   (reported by a community Docker image targeting the same chip). FP8 from
   the toolbox is the default; bf16 only if quality demands it.
6. Expose ComfyUI on the LAN (`--listen 0.0.0.0`) behind a firewall rule so
   the MCP server and the orchestrator can run from another machine. Never
   expose it beyond the LAN.

## What "works" means on this box

ROCm on gfx1151 is functional but not as polished as CUDA. Plan for:
- occasional custom node that assumes CUDA (patch or skip);
- HIP compute efficiency below the raw TFLOPS number;
- driver/toolbox updates that change performance by double-digit percent;
- long first-run compile times.

Treat the environment as a dependency with a version. When the toolbox updates
and something regresses, pin the old tag and file it under
`benchmark/results/`.

## Memory budgeting

Unified memory means "fits" is rarely the issue; "fits and is fast" is. Rough
expectations to confirm in Phase 0:

| Model | Weights (approx) | Notes |
|---|---|---|
| Qwen-Image-Edit-2511 FP8 | ~20 GB-class model, FP8 ≈ half of bf16 | Lightning LoRA for 4/20 steps |
| Qwen ControlNet-Union | ~small relative to base | 5 double blocks |
| FLUX.2 klein-base-4B bf16 | ~13 GB | BFL figure |
| Wan 2.2 I2V 14B Q5_K_M GGUF | fits 24 GB VRAM boxes at ~22 GB peak in mor-o's tuning | Slowest thing in the plan |
| BiRefNet, SAM3 | small | |

Record actual peak memory per workflow in `benchmark/results/`; do not copy
these numbers into other docs.

## The 3080

- Use it for: SDXL / klein-4B static candidates, PixelOE GPU backend,
  running the deterministic core's tests fast.
- Do not use it for: Wan 2.2, Qwen Edit at full size, anything that would
  make it part of a "two-worker" scheme. That is a distributed-systems side
  quest and the project doesn't need it.

## Offline acceptance

Phase 0's exit criterion includes a run with networking disabled. Repeat that
test at the end of every phase that adds a model. Something like:

```
sudo unshare -n bash -c 'cd /path/to/ComfyUI && python main.py --disable-mmap ...'
```

(or pull the cable). The point is to catch a custom node that phones home for
weights, a tokenizer download, or a telemetry call before it bites during a
real job.

## LoRA training

klein-4B LoRA training needs ~24 GB and about an hour on a 4090-class GPU.
ROCm training on gfx1151 is possible but adds a second layer of jank on top
of inference jank. For the Phase 6 project LoRA: rent a 24 GB GPU for an hour.
It costs less than the electricity to debug it locally.
