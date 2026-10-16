# 04 — Models and licensing

Every model, adapter, node pack, and dataset that touches shipped assets is
listed here with its license and role. **Nothing enters the default stack
without a row.** Fill in SHA-256 of the downloaded file(s) during Phase 0.

Rule of thumb: the default stack is Apache-2.0 (or MIT) end to end so game
assets are unencumbered. Non-commercial or research-licensed models may be
used for *experiments* and must be marked as such; their outputs do not ship.

## Default stack

| Component | Version / file | License | Role | SHA-256 | Status |
|---|---|---|---|---|---|
| Qwen-Image-Edit-2511 (FP8 from toolbox; bf16 optional) | | Apache-2.0 | Track A/C base editor | | pending |
| InstantX/Qwen-Image-ControlNet-Union | | Apache-2.0 | Track C pose/depth conditioning | | pending |
| FLUX.2-klein-base-4B | | Apache-2.0 | Track B base; static candidates; project LoRA base | | pending |
| Wan 2.2 I2V 14B (Q5_K_M GGUF, high+low noise) | | Apache-2.0 (verify the specific GGUF repo's card) | Track E motion | | pending |
| Lightx2v 4-step distill LoRA for Wan 2.2 | | verify | Track E speed | | pending |
| BiRefNet | | MIT (verify variant) | alpha / background removal | | pending |
| SAM3 | | verify | segmentation for cosmetic layers (Phase 6) | | pending |
| PixelOE (`pixeloe` lib + ComfyUI node) | 1.0.x | verify in repo | pixelization | | pending |
| ComfyUI-PixelGridHelpers | | verify in repo | Enforce Palette (and Median Fixer for A/B) | | pending |
| comfyui_controlnet_aux | | Apache-2.0 | OpenPose/DWPose/depth preprocessors | | pending |
| ComfyUI-GGUF | | Apache-2.0 | GGUF loaders | | pending |
| ComfyUI-RMBG | | verify | BiRefNet/SAM3 nodes | | pending |

## Experiments (outputs do not ship until promoted)

| Component | License | Why it's here | Notes |
|---|---|---|---|
| svntax-dev/pixel_spritesheet_4walk_combat_32x48_v1 (Qwen Edit 2511 LoRA) | check model card | Track A baseline | Trained on a template sheet; verify training-data license before shipping outputs |
| svntax-dev/pixel_spritesheet_4walk_small_lora_v1 (klein-4B LoRA) | check model card | Track B baseline | Trained on edits of a CC-BY-4.0 template (author's note); attribution implications if outputs ship |
| Limbicnation/pixel-art-lora (klein-4B) | Apache-2.0 | static style reference | 1,010-image dataset |
| Qwen-Image-Layered (+ unsloth GGUF) | Apache-2.0 | RGBA layer decomposition experiment | ~20B class |
| styly-agents/Wan2-2-pixel-animate | check model card | VFX only | 226 training clips |
| nerijs/pixel-art-xl (SDXL LoRA) | check model card | static baseline on the 3080 | SDXL base is CreativeML OpenRAIL++ |

## Explicitly excluded

| Component | Reason |
|---|---|
| FLUX.2-klein-base-**9B** and LoRAs on it (e.g. Mystic07/flux-lora-spritesheet) | FLUX Non-Commercial License on the 9B base |
| Qwen Image 2.1 (and any model under the Qwen Research License) | research license; keep separate from this project's default stack |
| LPC (Liberated Pixel Cup) sheets as *training* or *pose* sources | CC-BY-SA / GPL share-alike terms; fine to study, risky to derive shipped assets from without a deliberate licensing decision |
| Any hosted API fallback | violates the offline requirement |

## How to add a model

1. Add a row here with license and role.
2. Add the download step (with SHA) to `benchmark/results/phase0-env.md` or
   the relevant phase notes.
3. If it changes a track, add an ADR under `docs/decisions/`.
