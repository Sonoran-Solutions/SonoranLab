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

Recorded 2026-10-18 (DYL-67). Baked into `localhost/sonoranlab-comfy:p0` by [`env/Containerfile`](../../env/Containerfile)
(each commit verified with `git rev-parse` at build time; original image id `1e37559bc4f2`, locked rebuild `a4b70c4d9727`). A container restart or rebuild loads
the same commits. Full pip freeze of the image: `/opt/sonoranlab-pip-freeze.txt` inside it.

| node pack | commit | notes |
|---|---|---|
| PixelOE (ComfyUI node, KohakuBlueleaf/PixelOE) | `1d45ba0b5c51c3d998b19043a168366a8f170eaa` (tag v1.0.0 = PyPI 1.0.0) | full source incl. `src/pixeloe`, so the node never pip-installs at import. Classes `PixelOE`, `OutlineExpansion`, `PreResize`. |
| ComfyUI-PixelGridHelpers (molbal) | `005997b1ae8e666e5068866c4e97fd72a1b486b8` (v1.1.1) | no deps. `PixelGrid_ApplyPalette` (Enforce Palette), `GridMedianFixer`, etc. No LICENSE file upstream (pyproject says MIT). |
| comfyui_controlnet_aux (Fannovel16) | `a587a4c1594524c1d2de5cd1a86cd868ff448e1c` | `onnxruntime-gpu` (CUDA-only) dropped, CPU `onnxruntime` 1.31 instead → DWPose runs on CPU. Checkpoints pre-placed in `~/comfy-models/controlnet_aux_ckpts` (mounted read-only at `ckpts/`), so nothing downloads. Workflows must pin `pose_estimator=dw-ll_ucoco_384.onnx` (node default is a torchscript file we don't ship) and `ckpt_name=depth_anything_v2_vits.pth` (node default vitl is CC-BY-NC). |
| ComfyUI-GGUF | kyuz0/ComfyUI-GGUF-H3 `6d33de29cdc8bfa3f417dc0d96cbbc3b5412b8ed` | bundled in the toolbox image (fork of city96/ComfyUI-GGUF via molbal). `UnetLoaderGGUF`, `CLIPLoaderGGUF` present. |
| ComfyUI-RMBG (1038lab) | `229529e0ea63fb7085848225d74f91f6dc164956` (3.2.0) | GPL-3.0. Uses official ZhengPeng7 BiRefNet (MIT, fp16) pre-placed instead of its default `1038lab` mirror: loads and runs. RMBG rewrites `birefnet.py` on load, so `env/comfy.sh` gives it a working copy in `~/sonoranlab-work/RMBG`; the verified original keeps its hash. `SAM3Segment` loads; SAM3 itself is Phase 6 and also available as core `SAM3_Detect`. |

Pip constraints frozen from the base image: torch/torchvision/torchaudio (`2.14.0a0+rocm7.15.0a20260721`), the ROCm
wheels, numpy, opencv-python-headless. Transitive deps still pulled `opencv-python`/`opencv-contrib-python` 5.0.0
alongside headless; `cv2` imports and the nodes run (watch this if a node misbehaves on cv2 GUI symbols).

Startup evidence: all packs import with no `IMPORT FAILED` and no pip activity at startup; all 17 required classes are
present in `/object_info` (PixelOE, PixelGrid_ApplyPalette, GridMedianFixer, DWPreprocessor, OpenposePreprocessor,
DepthAnythingV2Preprocessor, BiRefNetRMBG, SAM3Segment, UnetLoaderGGUF, CLIPLoaderGGUF, SAM3_Detect,
ControlNetApplyAdvanced, SetUnionControlNetType, ReferenceLatent, Flux2Scheduler, EmptyFlux2LatentImage,
TextEncodeQwenImageEditPlus).

Operational tests (manifests in [`2026-10-18-phase0/`](2026-10-18-phase0/)):
- [`workflows/smoke/nodes_pixel.json`](../../workflows/smoke/nodes_pixel.json), 1.5 s: PixelOE k_centroid 4× (768→192) +
  Enforce Palette (16 HEX) → 10 colours, all in palette. Median Fixer grid 4 → **191×191** (not 192; matters for DYL-77).
- [`workflows/smoke/nodes_pose_depth_alpha.json`](../../workflows/smoke/nodes_pose_depth_alpha.json), 96 s total (mostly the klein
  t2i that makes the full-body test character): DWPose (ONNX, CPU) skeleton, Depth Anything V2 Small depth, BiRefNet-general
  RGBA with binary-capable alpha (min 0 / max 255). Contact sheet: `nodes_pose_depth_alpha_contact.jpg`. Non-blocking
  warning: depth model triggers a hipBLASLt `HIPBLAS_STATUS_NOT_SUPPORTED` fallback to hipBLAS on gfx1151.

### Operational safety and reproducibility (senior review, 2026-10-18)

- **Run-window guard** [`env/runwindow.py`](../../env/runwindow.py) wraps every heavy run, e.g.
  `python3 env/runwindow.py --need-gib 52 [--allow-pause llama-gemory] -- bash env/smoke.sh WF TAG`. In order, it:
  1. inventories GPU-memory holders (amdgpu fdinfo → systemd unit) and checks `MemAvailable` and memory PSI;
  2. **refuses with exit 3, stopping and running nothing** if `need + 8 GiB margin` doesn't fit;
  3. pauses only user units named with `--allow-pause`, and only if that is needed to fit;
  4. restores each paused unit to its original state on success, failure, or INT/TERM/HUP;
  5. persists paused units to `~/sonoranlab-work/runwindow.state.json`, so a SIGKILLed run is repaired by
     `--restore` (recover.sh runs it first);
  6. takes an exclusive lock, so two heavy runs can't overlap (exit 4). `--restore` takes the **same** lock: while a
     window is live it refuses (exit 4) and leaves the paused units stopped. Once the window's process is gone (the
     kernel drops the lock even on SIGKILL), restore proceeds.

  **Drain gate.** The heavy work is a ComfyUI prompt *inside the container*, and it outlives an interrupted
  host-side client (Ctrl-C, a closed terminal, a SIGKILLed window). So every restore path (normal exit, signals,
  `--restore`, and the automatic restore before a new run) first waits up to `--drain-s` (default 1800 s) for
  `sonoranlab-comfy` to report an empty queue. A busy or unreadable queue counts as busy. If the job is still running
  when the wait ends, or the operator presses Ctrl-C, the units **stay stopped**, the state file is kept, and a warning
  says to re-run `--restore`. Further signals are ignored while restoring. A unit is recorded as paused before
  `systemctl stop` runs, and the state file is fsynced.

  **Guarded by default:** `bash env/smoke.sh WF TAG` re-runs itself inside the window unless it is already in one, using
  `SMOKE_NEED_GIB` (default 52) and `SMOKE_ALLOW_PAUSE="llama-gemory"` (owner-approved units only). That makes it the
  single canonical command; only `SONORANLAB_UNGUARDED=1` (tests) skips the preflight. It counts as guarded only
  when its direct parent is the window (`SONORANLAB_RUNWINDOW == $PPID`), so a leaked variable doesn't bypass it.

  Budgets from the measurements above: Qwen Edit FP8 `--need-gib 52`, klein and the node chain `--need-gib 30`.
  The guard keeps a desktop-sized margin before anything runs. That is the protection the container memory cap
  alone can't give.
- **Smoke client** [`env/comfy_client.py`](../../env/comfy_client.py), used by `env/smoke.sh`:
  - validates `status_str == success` **and** `completed`, and the absence of execution_error/interrupted messages;
  - checks at least N output images exist and are non-empty;
  - applies per-request HTTP timeouts and an overall deadline. On timeout it sends `/interrupt` with **its own
    `prompt_id`**, which ComfyUI 0.31.0 honours only if that prompt is the running one, so another client's job is never
    stopped. It also deletes its own prompt from the queue;
  - exit codes: 0 success, 2 rejected, 3 execution_error, 4 timeout, 5 missing_outputs, 6 http_error / no response.
  
  `smoke.sh` exits with that code. Live check: a workflow naming a missing model → exit 2 (`rejected`).
- **`comfy.sh up` / `down`** refuse (exit 5) to destroy a running container unless `--replace`, and refuse even then
  while ComfyUI has a running or pending job, or can't report its queue, unless `--force`. The final check and the
  `podman rm` happen together under the run-window lock, so a guarded run can't start in between; `up`/`down` refuse
  while a window is live. Live check: a second `up` → exit 5.
- **Dependency lock.** [`env/requirements.lock.txt`](../../env/requirements.lock.txt) lists the image's 64-package pip
  layer with exact versions and SHA-256, installed with `--require-hashes --no-deps`. The build then fails if the
  environment differs from [`env/pip-freeze.p0.txt`](../../env/pip-freeze.p0.txt) (163 packages) or if the base's ROCm
  torch stack moved ([`env/check_lock.py`](../../env/check_lock.py)). Regenerate deliberately with
  [`env/lock_deps.sh`](../../env/lock_deps.sh).
  - Known ceiling: 5 sdists are hash-pinned but built with unpinned build backends.
  - The locked rebuild is image `a4b70c4d9727`, now tagged `:p0`. The pre-lock image `1e37559bc4f2` is kept as
    `:p0-resolved-20261018` for rollback.
  - Equivalence: Qwen Edit, klein, the node chain and the pixel test, run offline on the locked image, are all
    **byte-identical** to the pre-lock outputs. Manifests `*_offline_locked.json` are in
    [`2026-10-18-phase0/`](2026-10-18-phase0/).
- **Regression tests:**
  `uv run --no-project --with pytest pytest -q tests/ops` runs the fake-/proc run-window tests, the fake-ComfyUI
  client tests and the fake-podman guard tests. No GPU, network or real services are touched. The same suite runs on
  GitHub Actions (`.github/workflows/ops-tests.yml`) for every PR.

Unsupported / not exercised in Phase 0: onnxruntime GPU execution on ROCm (CPU only), SAM3 segmentation (Phase 6),
ControlNet-Union conditioning and Wan 2.2 video (Phase 1 Tracks C/E test them; weights are present and hash-verified).

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

Dated evidence (run manifests with workflow hash, image id, memory samples, output SHA-256, previews):
[`benchmark/results/2026-10-18-phase0/`](2026-10-18-phase0/). Full-size PNGs stay in `~/sonoranlab-work/output/p0/`
(their hashes are in the manifests). Runs go through `bash env/smoke.sh <workflow> <tag>`.

Conditions on 2026-10-18: image `localhost/sonoranlab-comfy:p0` (`1e37559bc4f2`), toolbox flags unchanged, container
capped at 64 GiB; `llama-gemory` paused for the run window, owner's host ComfyUI down, a model download writing to
NVMe at ~30 MB/s in parallel. "Repeat" is the second run in the same container; `--cache-none` reloads every model on
every run, so repeat ≈ steady state.

| workflow | resolution | steps | seconds (cold / repeat) | peak mem | notes |
|---|---|---|---|---|---|
| Qwen Image Edit 2511 FP8 + Lightning (stock) | input scaled to 1.6 MP → 1720×968 | 4 (euler/simple, CFG 1, shift 3.1) | 122.9 / 106.1 | GTT 39.8 GiB; container cgroup 51.2 GiB; host MemAvailable 73.4 → 36.7 GiB | [`workflows/static/qwen_edit_2511_fp8_lightning_4step.json`](../../workflows/static/qwen_edit_2511_fp8_lightning_4step.json): toolbox `Qwen-Image-Edit-2511-BF16-4-Step-LoRA.json` with one input changed (UNET → `qwen_image_edit_2511_fp8mixed`). Stock seed 224883477692338, stock prompt/input `ai-server.jpg`. No `--force-fp32` needed, no NaN. Both runs byte-identical (`fea3c149…`). |
| FLUX.2 klein-base-4B (stock template, base variant) | 1024×1024 | 20 (euler, CFG 5) | 116.3 / 116.2 | GTT 23.6 GiB; host MemAvailable 59.7 → 36.8 GiB | [`workflows/static/klein_base_4b_t2i.json`](../../workflows/static/klein_base_4b_t2i.json): API conversion of ComfyUI template `image_flux2_klein_text_to_image`, base 4B subgraph (the toolbox ships no klein workflow). Seed fixed to 42 (template randomizes). Undistilled base `flux-2-klein-base-4b.safetensors` + `qwen_3_4b` + Apache-tagged `flux2-vae` (K3) loads fine. No `--force-fp32`, no NaN. Both runs byte-identical (`1918e034…`). |

### Memory incident (2026-10-18 12:08) and the guardrails it produced

The first Qwen load started while other processes already held ~30 GiB of GTT (baseline sampled at 11:55; at least
`llama-gemory`'s model). It drove host MemAvailable to 1.2 GiB. systemd-oomd then killed the whole Claude desktop app
scope: the controlling agent session, its background jobs, and this container's `conmon`/port forwarder, which had
been launched from that scope. The container's ComfyUI survived.

*Correction (owner, 2026-10-18):* the owner's host ComfyUI was **not** part of this incident. The owner had closed it
after saving its queue. An earlier version of this note said otherwise.

Guardrails:
- `env/comfy.sh`: `--memory 64g --memory-swap 64g --oom-score-adj 1000`. GTT is charged to the container's memcg (Qwen
  peak 51.2 GiB), so an overshoot inside the container kills ComfyUI. On its own, this does **not** protect other
  apps from oomd when the host as a whole runs short.
- `env/comfy.sh`: `podman run` is wrapped in `systemd-run --user --scope`, so `conmon`/`rootlessport` live in their own scope.
- **`env/runwindow.py`** (see "Operational safety" above) is the host-level protection. It refuses heavy runs that
  don't fit with a desktop margin, pauses only owner-approved user units (e.g. `llama-gemory`), and restores them
  afterwards. Launch long runs with `systemd-run --user --wait` so they survive the controlling session.
- Rule of thumb: Qwen Edit FP8 needs ≈ 52 GiB free (`--need-gib 52`); klein ≈ 30.

The incident was not reproduced deliberately (no repeat OOM testing).

## Offline test

- Date: 2026-10-18 13:2x (DYL-69).
- Method: container network namespace with no interfaces, `bash env/comfy.sh up --offline` (`podman run --network=none`);
  the client runs inside the container via `podman exec` (`env/smoke.sh`). Not `unshare -n` on the host and not a cable
  pull: the repo lives on a Wi-Fi CIFS share and the box runs other services. No `HF_HUB_OFFLINE` or similar was set,
  so any hidden download would have failed loudly instead of being skipped.
- Isolation proof (inside the container): `/sys/class/net` = `lo` only; `curl https://huggingface.co` → `(6) Could not
  resolve host`; TCP connect 1.1.1.1:443 → `[Errno 101] Network is unreachable`; host `curl 127.0.0.1:8189` → no
  listener. ComfyUI itself started offline (no startup downloads).
- Result: **PASS**. Manifests in [`2026-10-18-phase0/`](2026-10-18-phase0/) (`network: none`):

| workflow | status | seconds | GTT peak | output vs online run |
|---|---|---|---|---|
| Qwen Image Edit 2511 FP8 + Lightning | success | 114.8 | 39.8 GiB | byte-identical (`fea3c149…`) |
| FLUX.2 klein-base-4B t2i | success | 86.2 | 23.7 GiB | byte-identical (`1918e034…`) |
| nodes: klein char → DWPose / Depth V2 Small / BiRefNet | success | 90.4 | 23.7 GiB | byte-identical (all 4 images) |

  No download, DNS or connection attempts in the ComfyUI log. (klein at 86 s vs 116 s online: the online runs had a
  ~30 MB/s model download writing to the same NVMe; treat 86 s as the clean number.)

## Restart / reboot recovery

Runbook = "Start / recover" above, scripted step for step in [`env/recover.sh`](../../env/recover.sh): image present
(else rebuild from `env/Containerfile`) → `fetch_models.sh --verify` → `comfy.sh up` → Qwen Edit smoke with
`llama-gemory` paused → compare the output SHA-256 with the reference `fea3c149…`. Run it from either checkout
(the NVMe clone does not depend on the NAS being up).

| date | scenario | runbook time | since reboot issued | result |
|---|---|---|---|---|
| 2026-10-18 13:28 | container teardown + recreate (host up) | 193 s | n/a | PASS (hash match) |
