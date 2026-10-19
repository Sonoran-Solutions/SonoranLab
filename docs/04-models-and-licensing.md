# 04 — Models and licensing

Every model, adapter, node pack, and dataset that touches shipped assets is
listed here with its license and role. **Nothing enters the default stack
without a row.** SHA-256 values below were taken from the Hugging Face API at
the pinned revision; the authoritative manifest is `env/models.tsv` (one row
per downloaded file: repo, revision, remote path, destination, size, SHA-256).
Check what is on disk with `bash env/fetch_models.sh --verify`.

**Hardlink coupling.** Two files, Q3 `qwen_image_vae.safetensors` and W4 `wan_2.1_vae.safetensors`, are
hardlinks into the owner's `~/ComfyUI/models` (identical SHA-256 at link time). They take no extra disk space,
but both paths now name the same inode:
- If the owner deletes or replaces their copy (most tools write a new file), our link keeps the verified bytes.
- If something rewrites their copy *in place*, our copy changes too.

`bash env/fetch_models.sh --verify` catches that: any hash mismatch fails it, and a normal re-fetch restores the
file. To decouple, replace each link with a real copy: `cp --remove-destination <file> <file>.tmp && mv <file>.tmp <file>`.

Rule of thumb: the default stack is Apache-2.0 (or MIT) end to end so game
assets are unencumbered. Non-commercial or research-licensed models may be
used for *experiments* and must be marked as such; their outputs do not ship.

Status values: `approved (default)` means Apache-2.0/MIT end to end with no open
caveat. `default, caveat: ...` is in the default stack but has an open item
that is recorded here, not resolved. Provenance note: the cards for the
default models declare Apache-2.0/MIT for the weights, but none publishes a
dataset license inventory (Qwen-Image-Edit-2511, the Lightning/lightx2v LoRAs,
InstantX ControlNet-Union, FLUX.2-klein-base-4B, Wan 2.2, DWPose/YOLOX). ADR-0006
already requires a provenance check for community LoRAs; the same caveat
applies to these "official" bases.

## Default stack

### Models and weights

| Component | Repo @ revision | File | License | Role | SHA-256 | Status |
|---|---|---|---|---|---|---|
| Q1 Qwen-Image-Edit-2511 (FP8 mixed) | `Comfy-Org/Qwen-Image-Edit_ComfyUI` @ `f68ace85` | `qwen_image_edit_2511_fp8mixed.safetensors` | Apache-2.0 | Track A/C base editor | `c9fdc158e46d3b61ef75f21ae866ca2fe808bf4a53643120d1c1e87c19280a4e` | approved (default) |
| Q2 Qwen2.5-VL-7B text encoder (fp8 scaled) | `Comfy-Org/Qwen-Image_ComfyUI` @ `1f12b17b` | `qwen_2.5_vl_7b_fp8_scaled.safetensors` | Apache-2.0 | Text encoder for Qwen-Image-Edit | `cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4` | approved (default) |
| Q3 Qwen-Image VAE | `Comfy-Org/Qwen-Image_ComfyUI` @ `1f12b17b` | `qwen_image_vae.safetensors` | Apache-2.0 | VAE for Qwen-Image-Edit | `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f` | approved (default) |
| Q4 Qwen-Image-Edit-2511 Lightning 4-step LoRA (bf16) | `lightx2v/Qwen-Image-Edit-2511-Lightning` @ `107d238d` | `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` | Apache-2.0 | Track A/C speed | `22226e8d05d354bb356627d428809f5afd7819399b077238a2b70a82883a904f` | approved (default) |
| Q5 Qwen-Image ControlNet-Union (InstantX, ComfyUI repack) | `Comfy-Org/Qwen-Image-InstantX-ControlNets` @ `90cf72c3` | `Qwen-Image-InstantX-ControlNet-Union.safetensors` | Apache-2.0 | Track C pose/depth conditioning (byte-identical to InstantX/Qwen-Image-ControlNet-Union) | `d51dca0073366a675108d5b83c3b7ef941cf2214c9a1c95c23f1e9a228ddbdb0` | approved (default) |
| K1 FLUX.2-klein-base-4B (bf16, undistilled) | `Comfy-Org/vae-text-encorder-for-flux-klein-4b` @ `5f526678` | `flux-2-klein-base-4b.safetensors` | Apache-2.0 | Track B base; static candidates; project LoRA base | `9c5fed22b76baea749d88fc2abe3ad53245e7b21a0d353a762665eea00043b92` | approved (default) |
| K2 Qwen3-4B text encoder for FLUX.2 klein | `Comfy-Org/vae-text-encorder-for-flux-klein-4b` @ `5f526678` | `qwen_3_4b.safetensors` | Apache-2.0 | Track B text encoder (4B only; the 9B uses Qwen3-8B) | `6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a` | approved (default) |
| K3 flux2-vae (Apache-tagged copy from the klein-4b repo) | `Comfy-Org/vae-text-encorder-for-flux-klein-4b` @ `5f526678` | `flux2-vae.safetensors` | Apache-2.0 (repo card) | Track B VAE | `868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3` | approved (default). Loads and produces correct, reproducible images in the DYL-66 smoke runs (online and offline). The ComfyUI-template copy is non-commercial-tagged and excluded. |
| W1 Wan 2.2 I2V A14B HighNoise Q5_K_M GGUF | `QuantStack/Wan2.2-I2V-A14B-GGUF` @ `6c671745` | `Wan2.2-I2V-A14B-HighNoise-Q5_K_M.gguf` | Apache-2.0 (card says original licensing remains in effect) | Track E motion (high-noise expert) | `163e9e5ae7ff83a4598d55242c767384be8909749dc07240d388a24838e8bac6` | default, caveat: GGUF repo card defers to original Wan licensing (Apache-2.0) |
| W2 Wan 2.2 I2V A14B LowNoise Q5_K_M GGUF | `QuantStack/Wan2.2-I2V-A14B-GGUF` @ `6c671745` | `Wan2.2-I2V-A14B-LowNoise-Q5_K_M.gguf` | Apache-2.0 (card says original licensing remains in effect) | Track E motion (low-noise expert) | `c5affcba15576959fa6d63c18261275b6407c3edd5ad3d8ab1c420e96f9d05d0` | default, caveat: GGUF repo card defers to original Wan licensing (Apache-2.0) |
| W3 umt5-xxl text encoder (fp8 e4m3fn scaled) | `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` @ `ee6f4a40` | `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | Apache-2.0 | Track E text encoder | `c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68` | approved (default) |
| W4 Wan 2.1 VAE (used by Wan 2.2 A14B) | `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` @ `ee6f4a40` | `wan_2.1_vae.safetensors` | Apache-2.0 | Track E VAE (not wan2.2_vae, which is 5B only) | `2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b` | approved (default) |
| W5 Wan 2.2 I2V Lightning 4-step LoRA V1, high noise | `lightx2v/Wan2.2-Lightning` @ `18bccf88` | `wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors` | Apache-2.0 | Track E speed | `d176c808d6fc461999b68e321efcb7501b20b8c3797523ed0df14f7d1deff11e` | approved (default) |
| W6 Wan 2.2 I2V Lightning 4-step LoRA V1, low noise | `lightx2v/Wan2.2-Lightning` @ `18bccf88` | `wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors` | Apache-2.0 | Track E speed | `024f21de095bc8fad9809ded3e9e49a2e170dcf27075da8145ba7d60d8aab7f9` | approved (default) |
| B1 BiRefNet general weights (official, fp16; placed as BiRefNet-general.safetensors) | `ZhengPeng7/BiRefNet` @ `e2bf8e44` | `BiRefNet-general.safetensors` | MIT | Alpha / background removal | `9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154` | default, caveat: DIS5K training-data license not checked (provenance note). Works through ComfyUI-RMBG, verified in the DYL-67 operational test, online and offline. |
| B1a BiRefNet code: birefnet.py | `ZhengPeng7/BiRefNet` @ `e2bf8e44` | `birefnet.py` | MIT | Loaded by ComfyUI-RMBG via importlib. RMBG rewrites it in place at load, so `env/comfy.sh` gives RMBG a working copy in `~/sonoranlab-work/RMBG`; the verified original is untouched | `208771ae626f653d64128fbf2d6ac9f8e645c5cc5e286258a73ec3322bbfe5ef` | default, caveat: see B1 |
| B1b BiRefNet code: BiRefNet_config.py | `ZhengPeng7/BiRefNet` @ `e2bf8e44` | `BiRefNet_config.py` | MIT | Loaded alongside B1a | `e7b8c2a74f6cea6a59553d517f71d47f2c1d90e670a13416af17c25fe2f3dc52` | default, caveat: see B1 |
| B1c BiRefNet code: config.json | `ZhengPeng7/BiRefNet` @ `e2bf8e44` | `config.json` | MIT | Loaded alongside B1a | `c97ea21569daf66b205491a4635147dd3bc42c7c168b89d7d75b53f67ef548ae` | default, caveat: see B1 |
| P1 DWPose person detector yolox_l.onnx | `yzd-v/DWPose` @ `1a714410` | `yolox_l.onnx` | Apache-2.0 | OpenPose/DWPose preprocessing (pre-placed so controlnet_aux does not download) | `7860ae79de6c89a3c1eb72ae9a2756c0ccfbe04b7791bb5880afabd97855a411` | default, caveat: runs on CPU onnxruntime (see controlnet_aux row) |
| P2 DWPose whole-body pose dw-ll_ucoco_384.onnx | `yzd-v/DWPose` @ `1a714410` | `dw-ll_ucoco_384.onnx` | Apache-2.0 | Default estimator of DWPreprocessor | `724f4ff2439ed61afb86fb8a1951ec39c6220682803b4a8bd4f598cd913b1843` | default, caveat: runs on CPU onnxruntime (see controlnet_aux row) |
| P3 Depth Anything V2 Small (vits) | `depth-anything/Depth-Anything-V2-Small` @ `03876f86` | `depth_anything_v2_vits.pth` | Apache-2.0 | Depth preprocessing | `715fade13be8f229f8a70cc02066f656f2423a59effd0579197bbf57860e1378` | default, caveat: only vits is allowed; workflows must pin ckpt_name=depth_anything_v2_vits.pth |

Notes on the caveats above:

- **flux2-vae (K3).** We use the Apache-tagged copy from the klein-4b repo. The
  ComfyUI workflow templates (and the owner's existing local file) point at a
  copy in `Comfy-Org/flux2-dev`, whose repo tag is non-commercial; that copy is
  excluded. K3 uses original-style tensor key names. It loads in ComfyUI and gives
  correct, byte-reproducible klein images (DYL-66, 2026-10-18). Full-tensor equality
  with the other copy was not checked, and doesn't need to be, since we only ship K3.
- **Wan 2.2 GGUF (W1, W2).** The QuantStack card says all original licensing
  terms remain in effect; the base Wan-AI/Wan2.2-I2V-A14B is Apache-2.0.
- **BiRefNet (B1).** Official `ZhengPeng7/BiRefNet` weights are MIT. Training
  data is DIS-TR (DIS5K); the DIS5K dataset license was not checked, so treat
  this as a provenance note. ComfyUI-RMBG normally downloads from the
  unlicensed `1038lab/BiRefNet` mirror. We pre-place the official files instead,
  and RMBG loads them: the F16 weights are a drop-in for its F32 mirror. Verified in
  the DYL-67 operational test, online and offline.
- **Depth Anything V2 (P3).** `DepthAnythingV2Preprocessor` defaults to `vitl`,
  which is CC-BY-NC-4.0 and would be downloaded on first use. Only Small (vits)
  is allowed; every workflow must pin it.
- **DWPose runtime (P1, P2).** `comfyui_controlnet_aux` requires
  `onnxruntime-gpu`, which is CUDA-only. Our image replaces it with CPU
  `onnxruntime`, so DWPose runs on CPU.

### Node packs

Pinned in `env/Containerfile`. No SHA-256 column: the commit is the pin.

| Component | Repo @ commit | License | Role | Status |
|---|---|---|---|---|
| PixelOE (v1.0.0; equals PyPI `pixeloe 1.0.0`) | `KohakuBlueleaf/PixelOE` @ `1d45ba0b5c51c3d998b19043a168366a8f170eaa` | Apache-2.0 | Pixelization (`PixelOE`, `OutlineExpansion`, `PreResize` nodes) | approved (default) |
| ComfyUI-PixelGridHelpers (v1.1.1) | `molbal/ComfyUI-PixelGridHelpers` @ `005997b1ae8e666e5068866c4e97fd72a1b486b8` | MIT per pyproject; upstream LICENSE file missing | Enforce Palette (and Median Fixer for A/B) | default, caveat: MIT per pyproject only, no LICENSE file in the repo; request one upstream or vendor the palette-snapping |
| comfyui_controlnet_aux | `Fannovel16/comfyui_controlnet_aux` @ `a587a4c1594524c1d2de5cd1a86cd868ff448e1c` | Apache-2.0 | OpenPose/DWPose/depth preprocessors | default, caveat: `onnxruntime-gpu` (CUDA-only) replaced by CPU `onnxruntime` in our image; default depth checkpoint is CC-BY-NC, pin vits (P3) |
| ComfyUI-GGUF-H3 (toolbox bundled; fork of city96/ComfyUI-GGUF) | `kyuz0/ComfyUI-GGUF-H3` @ `6d33de29cdc8bfa3f417dc0d96cbbc3b5412b8ed` | Apache-2.0 | GGUF loaders (Wan 2.2 Q5_K_M) | approved (default) |
| ComfyUI-RMBG | `1038lab/ComfyUI-RMBG` @ `229529e0ea63fb7085848225d74f91f6dc164956` | GPL-3.0 | BiRefNet background-removal node | default, caveat: GPL-3.0 tool code (does not attach to generated assets; do not vendor or redistribute); downloads from unlicensed `1038lab` mirrors by default, so official ZhengPeng7 MIT weights are pre-placed instead (verified working with ComfyUI-RMBG online and offline, DYL-67) |

Other notes:

- PixelOE needs a full `git clone` (it keeps `src/pixeloe`); an install without
  `src/` would run `pip install` online at import.
- SAM3 does not need ComfyUI-RMBG: ComfyUI core ships `SAM3_Detect` (see M1
  under Experiments).
- The kyuz0 toolbox base image does not pin ComfyUI (it clones master at image
  build time). Record `git -C /opt/ComfyUI rev-parse HEAD` or the image digest
  for any run that matters.

## Experiments (outputs do not ship until promoted)

Downloaded for Phase 0 (hashes in `env/models.tsv`):

| Component | Repo @ revision | File | License | Why it's here | SHA-256 | Notes |
|---|---|---|---|---|---|---|
| S1 svntax combat LoRA (Qwen Edit 2511), final | `svntax-dev/pixel_spritesheet_4walk_combat_32x48_v1` @ `9e3fafee` | `pixel_4walk_32x48_qwen_image_edit_2511_v1.safetensors` | Apache-2.0 (card) | Track A baseline | `906e7b128071e38f05432bacbe30dc9be4d52d42b8aeb415a249b1754d27a8b3` | Card has no training-data statement (provenance unverified). The un-suffixed final export is step 3000 (safetensors metadata `training_info`, ai-toolkit 0.7.24; read 2026-10-18). |
| S2 svntax combat LoRA, checkpoint 1750 | `svntax-dev/pixel_spritesheet_4walk_combat_32x48_v1` @ `9e3fafee` | `pixel_4walk_32x48_qwen_image_edit_2511_v1_000001750.safetensors` | Apache-2.0 (card) | Track A baseline | `565abf7be5387bfbaef604f141b9ee2ef4d0380c7a8ffca3c049734db92ee338` | Same provenance caveat as S1. |
| S3 svntax combat LoRA, checkpoint 2000 | `svntax-dev/pixel_spritesheet_4walk_combat_32x48_v1` @ `9e3fafee` | `pixel_4walk_32x48_qwen_image_edit_2511_v1_000002000.safetensors` | Apache-2.0 (card) | Track A baseline | `3377510f799e667242a5eddc8ff20b4f3fc5c3201ff8d4978f63586af8e57667` | Same provenance caveat as S1. |
| S4 svntax combat LoRA, checkpoint 2500 | `svntax-dev/pixel_spritesheet_4walk_combat_32x48_v1` @ `9e3fafee` | `pixel_4walk_32x48_qwen_image_edit_2511_v1_000002500.safetensors` | Apache-2.0 (card) | Track A baseline | `340042b2d86bdf2696daf311b4990c4a02403bec973857d7b497cebd08c9483f` | Same provenance caveat as S1. Card showcase images appear to come from this checkpoint. |
| S5 svntax small LoRA (klein-4B), final (step 3000 per card and safetensors metadata) | `svntax-dev/pixel_spritesheet_4walk_small_lora_v1` @ `cb8bffe5` | `pixel_4walk_small_flux2_klein_base_4b_v1.safetensors` | Apache-2.0 (card) | Track B baseline | `a968c042c2443bf6bfed41ce833a0e06bf5bbb7961ac40b88fb7257a940ae54e` | Trained on spritesheets edited from a CC-BY-4.0 template (George Bailey, OpenGameArt): attribution obligation may attach if outputs ship. Use with the base model only, not the distilled one. S5 and S6 metadata name the LoRA `pixel_4walk_24x24_flux2_klein_4b_v1` (24×24, not 32×32): check the card's cell size before slicing. |
| S6 svntax small LoRA (klein-4B), checkpoint 2750 | `svntax-dev/pixel_spritesheet_4walk_small_lora_v1` @ `cb8bffe5` | `pixel_4walk_small_flux2_klein_base_4b_v1_000002750.safetensors` | Apache-2.0 (card) | Track B baseline | `d7aaf9acb9c2d8b028110d8bbc5524f49103b92e99398b8c5905bbf8ddd21961` | Same CC-BY-4.0 template caveat as S5. |
| M1 SAM 3.1 multiplex fp16 (Comfy-Org/sam3.1 ungated repack) | `Comfy-Org/sam3.1` @ `7bb83747` | `sam3.1_multiplex_fp16.safetensors` | Meta "SAM License" (custom, not Apache/MIT) | Segmentation for cosmetic layers (Phase 6), via core `SAM3_Detect` | `9ba99c92703c2e8b4f47de2d34a539bb8e18923049e238b780d70dbe6368eb03` | Commercial use is allowed but with restrictions: ITAR/military/weapons end-use bans, the license must ship with redistributed weights, Meta may terminate and amend terms, litigation termination clause. Upstream facebook/sam3.1 is gated; the Comfy-Org repack is not. Experimental until an ADR decides. |

Not downloaded (no manifest row):

| Component | License | Why it's here | Notes |
|---|---|---|---|
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
| `facebook/sam3` (gated original) and `1038lab/sam3` (third-party rehost) | Gated (manual approval) / unclear provenance of an unofficial conversion with no license text. Use `Comfy-Org/sam3.1` (M1) instead; its SHA-256 for the gated repo is not obtainable without an approved token |
| `1038lab/BiRefNet` mirror (including `BiRefNet_toonout`) | No model card or license. ComfyUI-RMBG downloads from it by default and its `birefnet.py` is executed code; use the official ZhengPeng7 files (B1, B1a-c). `BiRefNet_toonout` is also fine-tuned on a CC-BY-4.0 dataset |
| Depth-Anything-V2 Large, Base, Giant (e.g. `depth_anything_v2_vitl.pth`) | CC-BY-NC-4.0. `DepthAnythingV2Preprocessor` defaults to vitl, so workflows must pin Small (vits, P3) |
| `flux2-vae` copy in `Comfy-Org/flux2-dev` (the one the ComfyUI templates link) | Repo tag is non-commercial (FLUX.1-dev license). We use the Apache-tagged copy K3 instead |
| Local `~/ComfyUI/models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | Misnamed: byte-identical to the Qwen3-8B FLUX.2-klein-9B encoder (`qwen_3_8b_fp8mixed`), not Qwen2.5-VL, and the 9B line is excluded. Must not be reused; Q2 is downloaded fresh |

## Benchmark reference art (not a model; ADR-0007)

| Component | Source | License / terms | Allowed use | Notes |
|---|---|---|---|---|
| Twos Are Up character sheets (`samurai`, `oathguard`, `relay_sentinel`) and `palette.png` | private `Sonoran-Solutions/Twos-Are-Up` @ `ebf3272`, hashes in `benchmark/characters/2026-10-18.yaml` | Generated by PixelLab (owner's plan, PixelLab ToS) from the owner's OpenAI-generated references; generated, not CC0, no exclusive copyright claim | **Benchmark use only** (owner, 2026-10-18): model inputs, style refs, ground truth | Not LoRA training data until the PixelLab ToS is reviewed for that use. Never committed to this public repo |

## How to add a model

1. Add a row here with license and role.
2. Add a row to `env/models.tsv` (repo, pinned revision, path, size, SHA-256),
   and run `bash env/fetch_models.sh --verify`. Record any other download steps
   in `benchmark/results/phase0-env.md` or the relevant phase notes.
3. If it changes a track, add an ADR under `docs/decisions/`.
