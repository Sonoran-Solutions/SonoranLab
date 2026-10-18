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
