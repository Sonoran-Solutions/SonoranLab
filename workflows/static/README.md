# workflows/static

Phase 0 smoke baselines (DYL-65, DYL-66). API-format JSON; `params.yaml` names the patchable inputs.

| workflow | source | change from source | models (see `env/models.tsv`) |
|---|---|---|---|
| `qwen_edit_2511_fp8_lightning_4step.json` | kyuz0 toolbox `/opt/comfy-workflows/Qwen-Image-Edit-2511-BF16-4-Step-LoRA.json` (image `384aa1fe…`) | UNET `qwen_image_edit_2511_bf16` → `qwen_image_edit_2511_fp8mixed`; output prefix | Q1, Q2, Q3, Q4 |
| `klein_base_4b_t2i.json` | ComfyUI template `image_flux2_klein_text_to_image` (templates 0.11.37), "Flux.2 Klein 4B" base subgraph | flattened to API JSON; seed 42 instead of randomize; output prefix | K1, K2, K3 |

Flags: toolbox defaults (`--disable-mmap --gpu-only --disable-smart-memory --cache-none --bf16-vae`); neither needs
`--force-fp32` on gfx1151 as of 2026-10-18. Timings and memory: `benchmark/results/phase0-env.md`.
The Qwen input `ai-server.jpg` ships inside the toolbox image (`/opt/ComfyUI/input`).
