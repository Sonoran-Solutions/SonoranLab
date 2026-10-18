# Phase 0 — environment record

Fill this in as you go. It is the "get back to a working box in 30 minutes"
document.

## Toolbox

Recorded 2026-10-18 (DYL-64).

- Image: `docker.io/kyuz0/amd-strix-halo-comfyui@sha256:384aa1fecef6a841832e0d5552949977330308d8c25e212a94f5e8dfcc061cae`
  (the `latest` channel on 2026-08-19; image id `7785a9c7fd75`, 16.2 GB unpacked). Pinned by digest because the
  repo only publishes moving `latest`/`dev` channels plus build-stamp tags.
- Toolbox repo HEAD when pinned: `kyuz0/amd-strix-halo-comfyui-toolboxes@7e77f04c` (2026-09-02).
- Inside the image: Fedora 45 prerelease, Python 3.13.14, torch `2.14.0a0+rocm7.15.0a20260721`, HIP 7.15.0,
  ComfyUI 0.31.0 @ `62b3c94bd451` (2026-08-11), frontend 1.48.7, templates 0.11.37.
  Bundled nodes: ComfyUI-GGUF (kyuz0/ComfyUI-GGUF-H3 @ `6d33de29`), ComfyUI_essentials, ComfyUI-AMDGPUMonitor,
  ComfyUI-MiniMax-H3-Turbo.
- Host: ONEXPLAYER Super X, Ryzen AI Max+ 395 / Radeon 8060S, Ubuntu 24.04.5, kernel `7.0.0-38-generic`,
  cmdline `ttm.pages_limit=16777216 ttm.page_pool_size=16777216` (64 GiB GTT). Rootless podman 4.x + crun.
- `rocminfo` (in container): runtime 1.21, agents `gfx1151` (AMD Radeon 8060S Graphics) and the CPU.
  ComfyUI log: `AMD arch: gfx1151`, `ROCm version: (7, 15)`, `Total VRAM 65536 MB, total RAM 112302 MB`.
- Created with: not `toolbox create` (Toolbx isn't installed on this Ubuntu host). It runs as a plain podman container through
  [`env/comfy.sh`](../../env/comfy.sh): `--device /dev/kfd --device /dev/dri --group-add keep-groups
  --security-opt seccomp=unconfined`, models bind-mounted **read-only** from `~/comfy-models`, outputs to
  `~/sonoranlab-work/output`.
- ComfyUI launch flags used (toolbox `start_comfy_ui` alias / `benchmark_workflows.py`, unchanged):
  `--disable-mmap --gpu-only --disable-smart-memory --cache-none --bf16-vae`
  plus `--listen 0.0.0.0 --port 8188` *inside* the container, published only as `127.0.0.1:8189` on the host.
- Workflows needing `--force-fp32`: (see first-run timings)

### Coexistence with the owner's existing ComfyUI

The box already runs a hand-rolled host ComfyUI (`~/ComfyUI`, v0.41.2, torch 2.10 + ROCm 7.13, `0.0.0.0:8188`)
plus ollama/llama-server/open-webui. SonoranLab does not modify any of them. The two ComfyUIs share unified memory,
so **benchmarks are only valid while the host ComfyUI is idle**; the timing rows below say what else was loaded.
Identical weight files are hardlinked into `~/comfy-models` (same filesystem, no extra disk) after a SHA-256 match.

### Network policy

- Default: `127.0.0.1:8189` only (verified: `curl http://192.168.1.76:8189` fails, the loopback works).
- LAN exposure is deferred until the MCP server runs on another machine (Phase 4). When that happens, set `SONORANLAB_PORT` and
  bind to the LAN address, add a host firewall rule limited to `192.168.1.0/24`, and never forward it beyond the LAN.
- Offline mode: `bash env/comfy.sh up --offline` → `--network=none` (no interfaces except loopback). The API is
  then reachable only through `podman exec sonoranlab-comfy curl http://127.0.0.1:8188/...`.

### Start / recover (cold box)

The repo is on a CIFS share (`/mnt/serverdata`, Wi-Fi, ~11 MB/s). Weights must stay on local NVMe; the
share is only used for the repo itself.

```bash
podman image exists docker.io/kyuz0/amd-strix-halo-comfyui@sha256:384aa1fecef6a841832e0d5552949977330308d8c25e212a94f5e8dfcc061cae \
  || podman pull docker.io/kyuz0/amd-strix-halo-comfyui@sha256:384aa1fecef6a841832e0d5552949977330308d8c25e212a94f5e8dfcc061cae
cd /mnt/serverdata/Projects/SonoranLab
bash env/comfy.sh up          # ~11 s to a healthy /system_stats; prints it
curl -s http://127.0.0.1:8189/system_stats | head -c 300
```

The container does not start at boot, by design: it competes with the host ComfyUI for memory, so start it
when you need it. Nothing is downloaded at startup.

### Rollback

`bash env/comfy.sh down`, then optionally `podman rmi 7785a9c7fd75` and `rm -rf ~/sonoranlab-work ~/comfy-models`.
Deleting hardlinks in `~/comfy-models` never deletes the originals in `~/ComfyUI/models`. Nothing on the host
(packages, kernel args, the owner's ComfyUI) was changed.

## Custom nodes installed

| node pack | commit | notes |
|---|---|---|
| PixelOE (ComfyUI node) | | |
| ComfyUI-PixelGridHelpers | | |
| comfyui_controlnet_aux | | |
| ComfyUI-GGUF | | |
| ComfyUI-RMBG | | |

## Models downloaded

Recorded 2026-10-18 (DYL-68). Authoritative manifest: [`env/models.tsv`](../../env/models.tsv) (repo, pinned revision,
remote path, destination, size, SHA-256, status). Fetched with `bash env/fetch_models.sh` into `~/comfy-models`
(local NVMe, mounted read-only into the container); `bash env/fetch_models.sh --verify` re-hashes everything without
network (28/28 ok, 59 s, 2026-10-18). 86.9 GB listed, 81 GB on disk because two files are hardlinks.
License status per row lives in [`docs/04-models-and-licensing.md`](../../docs/04-models-and-licensing.md).

| id | file (under `~/comfy-models/`) | size | SHA-256 | status | notes |
|---|---|---|---|---|---|
| Q1 | `diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors` | 20.53 GB | `c9fdc158e46d3b61ef75f21ae866ca2fe808bf4a53643120d1c1e87c19280a4e` | default |  |
| Q2 | `text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | 9.38 GB | `cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4` | default |  |
| Q3 | `vae/qwen_image_vae.safetensors` | 0.25 GB | `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f` | default | hardlinked from ~/ComfyUI/models (hash match) |
| Q4 | `loras/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` | 0.85 GB | `22226e8d05d354bb356627d428809f5afd7819399b077238a2b70a82883a904f` | default |  |
| Q5 | `controlnet/Qwen-Image-InstantX-ControlNet-Union.safetensors` | 3.54 GB | `d51dca0073366a675108d5b83c3b7ef941cf2214c9a1c95c23f1e9a228ddbdb0` | default |  |
| K1 | `diffusion_models/flux-2-klein-base-4b.safetensors` | 7.75 GB | `9c5fed22b76baea749d88fc2abe3ad53245e7b21a0d353a762665eea00043b92` | default |  |
| K2 | `text_encoders/qwen_3_4b.safetensors` | 8.04 GB | `6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a` | default |  |
| K3 | `vae/flux2-vae.safetensors` | 0.34 GB | `868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3` | default |  |
| S1 | `loras/pixel_4walk_32x48_qwen_image_edit_2511_v1.safetensors` | 0.59 GB | `906e7b128071e38f05432bacbe30dc9be4d52d42b8aeb415a249b1754d27a8b3` | experimental-no-ship |  |
| S2 | `loras/pixel_4walk_32x48_qwen_image_edit_2511_v1_000001750.safetensors` | 0.59 GB | `565abf7be5387bfbaef604f141b9ee2ef4d0380c7a8ffca3c049734db92ee338` | experimental-no-ship |  |
| S3 | `loras/pixel_4walk_32x48_qwen_image_edit_2511_v1_000002000.safetensors` | 0.59 GB | `3377510f799e667242a5eddc8ff20b4f3fc5c3201ff8d4978f63586af8e57667` | experimental-no-ship |  |
| S4 | `loras/pixel_4walk_32x48_qwen_image_edit_2511_v1_000002500.safetensors` | 0.59 GB | `340042b2d86bdf2696daf311b4990c4a02403bec973857d7b497cebd08c9483f` | experimental-no-ship |  |
| S5 | `loras/pixel_4walk_small_flux2_klein_base_4b_v1.safetensors` | 0.09 GB | `a968c042c2443bf6bfed41ce833a0e06bf5bbb7961ac40b88fb7257a940ae54e` | experimental-no-ship |  |
| S6 | `loras/pixel_4walk_small_flux2_klein_base_4b_v1_000002750.safetensors` | 0.09 GB | `d7aaf9acb9c2d8b028110d8bbc5524f49103b92e99398b8c5905bbf8ddd21961` | experimental-no-ship |  |
| W1 | `unet/Wan2.2-I2V-A14B-HighNoise-Q5_K_M.gguf` | 10.79 GB | `163e9e5ae7ff83a4598d55242c767384be8909749dc07240d388a24838e8bac6` | default |  |
| W2 | `unet/Wan2.2-I2V-A14B-LowNoise-Q5_K_M.gguf` | 10.79 GB | `c5affcba15576959fa6d63c18261275b6407c3edd5ad3d8ab1c420e96f9d05d0` | default |  |
| W3 | `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors` | 6.74 GB | `c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68` | default |  |
| W4 | `vae/wan_2.1_vae.safetensors` | 0.25 GB | `2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b` | default | hardlinked from ~/ComfyUI/models (hash match) |
| W5 | `loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors` | 1.23 GB | `d176c808d6fc461999b68e321efcb7501b20b8c3797523ed0df14f7d1deff11e` | default |  |
| W6 | `loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors` | 1.23 GB | `024f21de095bc8fad9809ded3e9e49a2e170dcf27075da8145ba7d60d8aab7f9` | default |  |
| B1 | `RMBG/BiRefNet/BiRefNet-general.safetensors` | 0.44 GB | `9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154` | default |  |
| B1a | `RMBG/BiRefNet/birefnet.py` | 0.00 GB | `208771ae626f653d64128fbf2d6ac9f8e645c5cc5e286258a73ec3322bbfe5ef` | default |  |
| B1b | `RMBG/BiRefNet/BiRefNet_config.py` | 0.00 GB | `e7b8c2a74f6cea6a59553d517f71d47f2c1d90e670a13416af17c25fe2f3dc52` | default |  |
| B1c | `RMBG/BiRefNet/config.json` | 0.00 GB | `c97ea21569daf66b205491a4635147dd3bc42c7c168b89d7d75b53f67ef548ae` | default |  |
| P1 | `controlnet_aux_ckpts/yzd-v/DWPose/yolox_l.onnx` | 0.22 GB | `7860ae79de6c89a3c1eb72ae9a2756c0ccfbe04b7791bb5880afabd97855a411` | default |  |
| P2 | `controlnet_aux_ckpts/yzd-v/DWPose/dw-ll_ucoco_384.onnx` | 0.13 GB | `724f4ff2439ed61afb86fb8a1951ec39c6220682803b4a8bd4f598cd913b1843` | default |  |
| P3 | `controlnet_aux_ckpts/depth-anything/Depth-Anything-V2-Small/depth_anything_v2_vits.pth` | 0.10 GB | `715fade13be8f229f8a70cc02066f656f2423a59effd0579197bbf57860e1378` | default |  |
| M1 | `checkpoints/sam3.1_multiplex_fp16.safetensors` | 1.75 GB | `9ba99c92703c2e8b4f47de2d34a539bb8e18923049e238b780d70dbe6368eb03` | experimental-no-ship |  |

Not reused: the owner's `~/ComfyUI/models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` (8.66 GB) is not the
official Qwen2.5-VL file (9.38 GB); the audit found it byte-identical to the Qwen3-8B klein-9B encoder. Q2 was downloaded fresh.

## First-run timings

| workflow | resolution | steps | seconds | peak mem | notes |
|---|---|---|---|---|---|
| Qwen Image Edit 2511 FP8 + Lightning (stock) | | | | | |
| FLUX.2 klein-4B (stock) | | | | | |

## Offline test

- Date:
- Method (unshare -n / cable):
- Result:
