# workflows/pose — Track C (pose-conditioned frame transfer)

| workflow | control | models (see `env/models.tsv`) |
|---|---|---|
| `qwen_edit_controlnet_union_pose.json` | OpenPose image pre-rendered from the Track D bake (no DWPose node) | Q1, Q2, Q3, Q4, Q5 |
| `qwen_edit_controlnet_union_depth.json` | `DepthAnythingV2Preprocessor` pinned to `depth_anything_v2_vits.pth`, run on a bake RGB render | Q1–Q5, P3 |

Graph: the static Qwen Edit 2511 FP8 + Lightning baseline (`workflows/static/`) plus
`ControlNetLoader` → `ControlNetApplyAdvanced` (strength 0.9, vae connected) between the reference-latent method
nodes and the KSampler. The latent is an empty canvas (`cell × 4`: 736 / 704 / 544). One frame per run.

## Does InstantX ControlNet-Union wire into Qwen Image Edit in this build?

**Structurally, yes.** It uses core nodes only, and the graph validates against this build's `/object_info`.
That does not prove output quality. Build: ComfyUI 0.31.0 @ `62b3c94b`, image `localhost/sonoranlab-comfy:p0`,
`object_info` saved to `~/sonoranlab-work/object_info-2026-10-18.json`.

Evidence, read in the container on 2026-10-18:
1. **Loader.** `ControlNetLoader` lists `Qwen-Image-InstantX-ControlNet-Union.safetensors`.
   `comfy/controlnet.py` `load_controlnet_state_dict` sends a state dict with `controlnet_blocks.0.weight` and
   `transformer_blocks.0.img_mlp.net.0.proj.weight` to `load_controlnet_qwen_instantx`. That builds
   `comfy.ldm.qwen_image.controlnet.QwenImageControlNetModel` with `latent_format = Wan21`, so
   `ControlNetApplyAdvanced` **must** get the VAE. Without it, `get_control` raises "This Controlnet needs a VAE".
2. **Injection point.** The residuals are consumed in the shared `QwenImageTransformer2DModel._forward`, which
   Qwen-Image and Qwen-Image-Edit both use: `hidden_states[:, :add.shape[1]] += add` after each double block.
   Edit reference latents are concatenated *after* the noise-latent tokens (`torch.cat([hidden_states, kontext])`),
   so the control residual lands only on the image being generated, never on the reference tokens.
   `txt_start` and the main-image positional ids are computed from `x` alone, the same way in both models.
3. **The files match that path** (safetensors headers, read without loading). Q5 has `controlnet_blocks.0.weight`
   and `transformer_blocks.0.img_mlp.net.0.proj.weight`, so it routes to the Qwen InstantX branch, not Flux or SD3.
   `controlnet_x_embedder.weight` is `[3072, 64]`: the plain union, not the 68-channel inpaint variant. It has
   5 transformer blocks, which the model repeats up to `main_model_double = 60`. Q1 has 60 transformer blocks and the
   `__index_timestep_zero__` marker of 2511.
4. **Stock precedent.** The ComfyUI template `image_qwen_image_instantx_controlnet` (templates 0.11.37) uses exactly
   `ControlNetLoader` + `ControlNetApplyAdvanced(vae)` with Qwen-**Image** + Lightning. mor-o W1 runs InstantX Union on
   Qwen-Image-**Edit 2509** (docs/02). Neither uses `SetUnionControlNetType`: the InstantX Qwen model has no
   control-type embedding (its `forward` drops extra kwargs), so the type is inferred from the image.

Not used, and why:
- **`QwenImageDiffsynthControlnet`** needs a `MODEL_PATCH` from `ModelPatchLoader`, the DiffSynth union patch.
  `ModelPatchLoader` lists no files in this build, and none is downloaded or licensed in docs/04.
- **Qwen "Fun" ControlNet** (`load_controlnet_qwen_fun`). This is a different checkpoint format; it is not our Q5 file.

**Still unknown; only a GPU run answers these:**
- InstantX was trained against Qwen-Image hidden states and text-only Qwen2.5-VL context. Edit 2511 adds vision tokens
  to the context and uses `index_timestep_zero` reference tokens. The wiring is valid, but how much the edit model's
  states drift from what the ControlNet expects is unmeasured.
- Strength 0.9 with Lightning 4-step, at sprite proportions (the Track C question in docs/02).

## Memory

Weights: Q1–Q4 are 31.0 GB, plus Q5 at 3.5 GB, so 34.6 GB (32.2 GiB). The Phase 0 Qwen Edit run without Q5 peaked at
39.8 GiB GTT and 51.2 GiB container cgroup. Expect roughly +3.5 GiB on both: about 43 GiB GTT and about 55 GiB cgroup,
close to the 64 GiB container cap. Start with `--need-gib 56` and record the measured peak in `benchmark/results/`.

## Mitigations not wired yet (docs/05)

- **Previous frame as a second reference.** Feed it to `image2` of both `TextEncodeQwenImageEditPlus` nodes. That is
  a new graph (`_prev` variant), not a patch, so add it only if per-frame drift shows up.
- **Same seed per frame.** The runner does this through the `seed` param.
