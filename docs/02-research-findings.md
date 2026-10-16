# 02 — Research findings (as of 2026-10-16)

This is the evidence behind the plan. Three rounds of planning preceded this
repo (an initial plan, a Gemini critique, a Claude rebuttal, then a verified
pass with fresh sources). Several claims flipped between rounds. **This file
is the record of what was actually verified** so nobody re-litigates it from
memory. Dates matter; re-verify anything older than a few months before
changing a decision.

## Summary table

| Claim | Status | Consequence |
|---|---|---|
| Qwen-Image-Edit supports pose/depth/edge conditioning | **Verified** (2509 model card; InstantX ControlNet-Union; ComfyUI native support) | Track C is a real path, not speculative |
| Sprite Sage has local Qwen generation and is a candidate foundation | **False** (README lists OpenAI/Google providers only; Windows-only build; 29 commits) | Dropped. Godot-native bake replaces its .glb baker |
| Retro Diffusion is a local alternative worth comparing | **Verified**, static only (animation model is cloud-only) | It is the buy-vs-build baseline for static art |
| Gemini's post chain PixelOE → Median Fixer → Palette is correct | **False** (Median Fixer is itself a block-median *downscaler* that shrinks the image) | They are alternatives; chain is PixelOE → Palette → alpha → register |
| Grimoire is usable on AMD/Linux | **Unsupported** (ControlNet/IPAdapter SDXL-only; "other vendors not tested") | Reference-only; buy for workflow JSON if useful |
| Wan 2.2 should be VFX-only | **Partly false**: the pixel-animate *LoRA* is weak (226 clips); *base* Wan 2.2 I2V drives character motion in a working pipeline | Base model for Track E; LoRA for VFX only |
| Strix Halo runs these models | **Verified with caveats** (maintained toolbox; `--disable-mmap`; bf16 NaN issue on some paths; >40 GB system RAM for Qwen Edit bf16) | Use the toolbox, budget a weekend |
| klein-4B LoRA training is cheap | **Verified** (<24 GB, ~1 hour per BFL) | Project LoRA is a Phase 6 item; rent a GPU for it |

## Detail

### Pose conditioning on Qwen Image Edit

- The **Qwen-Image-Edit-2509** model card lists "Native Support for ControlNet:
  Including depth maps, edge maps, keypoint maps, and more." 2511 is the next
  monthly iteration in the same line.
  Source: https://huggingface.co/Qwen/Qwen-Image-Edit-2509/blob/main/README.md
- **InstantX/Qwen-Image-ControlNet-Union** is a true ControlNet (5 double
  blocks) supporting canny, soft edge, depth, pose. Trained from scratch, 50K
  steps, 10M general/human images at 1328². ComfyUI has native support.
  Recommended: DWPose for pose, Depth-Anything for depth, conditioning scale
  0.8–1.0. Source: https://huggingface.co/InstantX/Qwen-Image-ControlNet-Union
- There is also a DiffSynth "Union ControlNet LoRA" model-patch path for
  Qwen-Image (canny/depth/pose/lineart). Either works; pick by what the toolbox
  ships.
- **Open question the benchmark must answer:** the ControlNet was trained on
  photographic humans. Whether it obeys a stick figure at chibi / 32×48
  proportions upscaled to 768px is untested. That is Phase 1 Track C.

### A working reference pipeline: mor-o/comfyui-2d-character-pipeline (MIT)

Source: https://github.com/mor-o/comfyui-2d-character-pipeline

Turns one character image into a library of layered 2D sprite sheets. Not
pixel art, and that's the point: it proves the "HD motion first" architecture.

| Workflow | Model | Role |
|---|---|---|
| W1 | Qwen-Image-Edit 2509 + InstantX ControlNet-Union | pose a character from an OpenPose skeleton |
| W2 | WAN 2.2 I2V 14B (Q5_K_M GGUF) + Lightx2v 4-step distill | image-to-video motion |
| W3 | BiRefNet | background removal → RGBA horizontal strip |
| W4 | Wan 2.1 VACE 14B + CausVid LoRA + SAM3 mask | two-pass cosmetic inpaint, non-cosmetic pixels preserved bit-exact |
| W5 | SAM3 | per-frame segmentation → cosmetic-only sheet, pixel-aligned with base |

Other facts that matter here:
- Workflows are **API-format JSON**, designed to be driven by an agent (Claude
  Code / Cursor) via `artokun/comfyui-mcp`. Drivers are stdlib-only Python that
  POST to `/prompt` and poll `/history`. SonoranLab adopts this convention.
- Tuned for **24 GB VRAM** NVIDIA (peaks ~22.5 GB), ≥32 GB RAM, ~80 GB of
  models. On Strix Halo it fits in unified memory; speed is the question.
- Validated on idle, walking, in-air animations for one base character.

### Sprite Sage (keystoneintelligence/spritesage, GPLv3)

Source: https://github.com/keystoneintelligence/spritesage (read 2026-10-16)

- AI providers: "configured OpenAI or Google models" via API keys. **No local
  backend.** Earlier plans that said it had local Qwen integration were wrong.
- Build instructions are Windows (`venv\Scripts\...`); PyInstaller to `.exe`.
- The `.glb` baker is real but "targets a constrained animated GLB structure
  and is not a complete glTF runtime."
- Roadmap (animation templates, AI style pipeline, pixel editor) unshipped.
- 29 commits, 9 stars.

Decision: not a foundation. The baker is an orthographic camera and a frame
loop; Godot 4 does it natively with full glTF support.

### Retro Diffusion (Aseprite extension)

Sources: https://astropulse.itch.io/retrodiffusion and its comments page.

- One-time purchase; runs locally inside Aseprite with its own pixel-art model.
- "Linux support is not guaranteed," tested on stock Ubuntu, Mint, Fedora.
  For AMD: "You would need to use Linux with an AMD gpu."
- Animation: the model behind it "cannot be hosted locally due to both size
  constraints and licensing issues." Cloud only.

Decision: the honest baseline for local static pixel art. SonoranLab does not
need to beat it at static generation; it needs to do what it can't (local
animation, project memory, agent interface).

### PixelOE and Pixel Grid Helpers

- **PixelOE 1.0.0** released 2026-09-24 (KohakuBlueleaf). Contrast-aware
  outline expansion + contrast/k-centroid downscale; adds a Slang GPU backend;
  includes k-means quantization and ordered / error-diffusion dithering;
  ComfyUI node in-repo (clone into `custom_nodes`); `pip install pixeloe`.
  Sources: https://github.com/KohakuBlueleaf/PixelOE/releases/tag/v1.0.0 ,
  https://pypi.org/project/pixeloe/
- **Pixel Grid Helpers** (molbal): Quantize (Max Colors), Enforce Palette
  (nearest color in a HEX list), Merge Similar Colors, Analyze Palette, Palette
  to Image, **Median Fixer**. Median Fixer "applies a median blur by dividing
  the image into blocks and calculating the median color value for each
  block" and outputs the image "reduced in size in accordance with defined
  grid size." It is a downscaler, not a cleanup pass.
  Sources: https://civitai.com/articles/23699 ,
  https://comfyai.run/documentation/GridMedianFixer

Decision: PixelOE k-centroid is the downscaler. Enforce Palette is the palette
step (or PixelOE's own quantization; benchmark both). Median Fixer is an
alternative downscaler to A/B, never a step after PixelOE.

### Grimoire (BlackMagikStudios, Aseprite extension, $30 / $15 on sale)

Source: https://blackmagikstudios.itch.io/grimoire

- Connects local ComfyUI workflows to Aseprite; bundles LoRAs (walk cycles,
  Wang tilesets), neural pixelization, palette reduction, background removal.
- "Structure control with ControlNets or IPAdapters (*for sdxl)".
- "Metal GPU acceleration supported via ComfyUI… ComfyUI may support GPUs from
  other vendors, but these have not been tested and are not officially
  supported."

Decision: not a dependency. Worth $15 to read its walk-cycle and Wang tileset
graphs when tilesets come up.

### Strix Halo (gfx1151) + ComfyUI

- **kyuz0/amd-strix-halo-comfyui-toolboxes**: Fedora toolbox with ROCm 7 /
  TheRock nightlies for gfx1151. Ships workflows including Qwen Image 2512
  (FP8) + Lightning and **Qwen Image Edit 2511 (FP8) + Lightning (4/20
  steps)**, plus video models. `--disable-mmap` is "critical for Strix Halo."
  Benchmarks run with `--disable-mmap --gpu-only --disable-smart-memory
  --cache-none --bf16-vae`. Authors are "working directly with AMD to improve
  kernel stability and performance" (ROCm/TheRock#2591).
  Sources: https://github.com/kyuz0/amd-strix-halo-comfyui-toolboxes ,
  https://kyuz0.github.io/amd-strix-halo-comfyui-toolboxes/
- **qinzhen/comfyui-rocm724** (Docker, ROCm 7.2.4, targets AI MAX 395):
  "Qwen Image Edit 2511 … bf16 … Works, requires more than 40 GB system
  memory." Source: https://hub.docker.com/r/qinzhen/comfyui-rocm724
- Known failure: on gfx1151 some pixel-space sampling paths "emit NaN in bf16"
  → black image; `--force-fp32` fixes it (ComfyUI #14249). Source:
  https://localaimaster.com/blog/comfyui-amd-rocm-image-generation
- Earlier Phoronix testing: ROCm 6.4.1 and 7.0 work on Strix Halo though not
  always on the official support list; HIP compute efficiency lagged raw
  FLOPS; driver updates moved performance noticeably.

Decision: use the kyuz0 toolbox; do not hand-roll. Record toolbox image tag in
Phase 0 notes.

### FLUX.2 klein LoRA training

Source: https://huggingface.co/blog/black-forest-labs/flux-2-klein-lora (2026-06-04)

- klein-4B: ~13 GB bf16 weights; a LoRA run "lands under 24 GB, so a 4090 or
  an L4 is enough."
- "Applying the LoRA on the distilled model typically gives better results
  than the base model." This contradicts the svntax model cards, which say to
  use the base model. Test both in Phase 6.
- Caption guidance: don't name the style in captions; the style should bake
  into the weights.
- A community example exists: Limbicnation/pixel-art-lora (klein-4B, Apache
  2.0, 1,010-image dataset).

### Existing sprite-sheet LoRAs

- **svntax-dev/pixel_spritesheet_4walk_combat_32x48_v1** — Qwen-Image-Edit-
  2511; 6×4 grid (3 walk + 2 attack + 1 hurt per direction); generate at 768²
  from a reference image; downscale 4×.
  Mirror: https://civarchive.com/civision/models/441065/versions/71455
- **svntax-dev/pixel_spritesheet_4walk_small_lora_v1** — FLUX.2-klein-base-4B;
  4×4 grid; 512² → 128². Author documents clipped hair/helmets, weak non-
  humanoids, lost details on the back-facing row.
  https://huggingface.co/svntax-dev/pixel_spritesheet_4walk_small_lora_v1
- **Mystic07/flux-lora-spritesheet** — klein-base-**9B** (non-commercial base
  license), ~18.7k LPC-style sheets. Out of the default stack for licensing.
- **styly-agents/Wan2-2-pixel-animate** — Wan2.2-I2V-A14B LoRA, rank 256,
  ~2.3 GB, 226 training videos. VFX-only in this plan.
  https://huggingface.co/styly-agents/Wan2-2-pixel-animate

### Qwen-Image-Layered

- Decomposes an image into N RGBA layers (3–10), Apache 2.0, released
  2025-12-19. GGUF quants: unsloth/Qwen-Image-Layered-GGUF.
  https://huggingface.co/Qwen/Qwen-Image-Layered
- It is a Qwen-Image-class (~20B) model, not a lightweight matting tool, and
  layer semantics are not guaranteed to map to "body / weapon / helmet."

Decision: Phase 6 experiment for alpha and gear separation; BiRefNet + SAM3
(proven in mor-o) is the default.

### PixelLab (the reference product)

- Skeleton animation tool: estimate skeleton from a reference, insert a
  template, drag points, generate, rough manual fixes, regenerate with fixed
  frames as init images and inpainting to freeze parts. Canvas sizes 128/64/
  32/16. https://www.pixellab.ai/docs/tools/skeleton-animation
- Exposes a REST API, Python/JS SDKs, an MCP server, and Aseprite/Pixelorama
  plugins. No downloadable weights or self-host option found.
  https://www.pixellab.ai/llms-full.txt

Decision: copy the *shape* (tools + templates + init-image refinement loop),
not the models.

## Things nobody has verified yet (benchmark targets)

1. ControlNet-Union obedience at sprite proportions (Track C).
2. Wan 2.2 I2V coherence on flat-shaded 2D at 512–768px, and wall time on
   ROCm (Track E).
3. Whether PixelOE k-centroid at 4× from a model canvas beats Median Fixer or
   plain `contrast` mode on 1-px outlines.
4. Seconds-per-frame for every track on the Strix Halo box.
5. klein-4B LoRA: base vs distilled for sprite work.
