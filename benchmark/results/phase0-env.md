# Phase 0 — environment record

Fill this in as you go. It is the "get back to a working box in 30 minutes"
document.

## Toolbox

- Image: `docker.io/kyuz0/amd-strix-halo-comfyui:<TAG>`  ← record the tag
- Created with: `toolbox create ... --security-opt seccomp=unconfined ...`
- Kernel / ROCm reported by `rocminfo`:
- ComfyUI launch flags used:
  `--disable-mmap --gpu-only --disable-smart-memory --cache-none --bf16-vae --listen 0.0.0.0`
- Workflows needing `--force-fp32`:

## Custom nodes installed

| node pack | commit | notes |
|---|---|---|
| PixelOE (ComfyUI node) | | |
| ComfyUI-PixelGridHelpers | | |
| comfyui_controlnet_aux | | |
| ComfyUI-GGUF | | |
| ComfyUI-RMBG | | |

## Models downloaded

| file | SHA-256 | size | license row in docs/04 |
|---|---|---|---|

## First-run timings

| workflow | resolution | steps | seconds | peak mem | notes |
|---|---|---|---|---|---|
| Qwen Image Edit 2511 FP8 + Lightning (stock) | | | | | |
| FLUX.2 klein-4B (stock) | | | | | |

## Offline test

- Date:
- Method (unshare -n / cable):
- Result:
