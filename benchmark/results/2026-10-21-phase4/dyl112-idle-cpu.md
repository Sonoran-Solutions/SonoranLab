# DYL-112: idle CPU of the SonoranLab ComfyUI container (2026-10-20, host observation)

**Cause found:** the base image's `ComfyUI-AMDGPUMonitor` custom node (commit 0c584ed; not pinned or used by SonoranLab) starts a daemon thread when it is imported. Every 1 s that thread runs five `rocm-smi ... --json`
queries (`--showuse`, `--showtemp`, `--showproductname`, `--showmeminfo gtt`, `--showmeminfo vram`), and each query
starts a new Python interpreter. None of the 36 node types used by `workflows/**/*.json` come from it, so it is pure
overhead. Nothing in ComfyUI's queue, the relay or SonoranLab's polling caused the load: the queue was empty, and the
main ComfyUI process threads used about 0.2% CPU. The CPU went to those child processes.

**Fix:** `env/comfy.sh` now starts ComfyUI with `--disable-all-custom-nodes --whitelist-custom-nodes` listing the five nodes
the workflows need: the four `env/Containerfile` pins (ComfyUI-PixelGridHelpers, ComfyUI-RMBG, PixelOE,
comfyui_controlnet_aux) plus the base image's ComfyUI-GGUF (`UnetLoaderGGUF`). The image is not rebuilt and no node is upgraded. ComfyUI_essentials,
ComfyUI-MiniMax-H3-Turbo and websocket_image_save are also skipped; no SonoranLab workflow uses them.

| Measurement (container cgroup `cpu.stat` usage_usec, 60 s window, empty queue, offline `--network=none`) | CPU |
|---|---|
| Before (default flags, ~2 min after start) | 32.5 % of one core |
| After (node whitelist, ~1 min after start) | 0.0 % (0 usec over 60 s) |

- Before: the container held 1.85 GB of RAM, with no model loaded. After: 0.84 GB.
- Whitelist check: `/object_info` contains all 36 class types used by the committed workflows (0 missing).
- Host context: ComfyUI 0.31.0, torch 2.14.0a0+rocm7.15.0a20260721, image `localhost/sonoranlab-comfy:p0`. During
  the test the owner's `strix-ai` container was running and was not touched.
- **Not reproduced:** the original report of ~375%. Each `rocm-smi` call costs more while the GPU is busy, so a live
  window probably multiplied this load. That is an inference, not a measurement. A GPU-load comparison is left to the
  DYL-120 live run, which will record container CPU during generation.
- Safe stop: `env/comfy.sh down` checks that the queue is empty first. ComfyUI ignored SIGTERM as PID 1, and podman
  sent SIGKILL after 10 s. That is expected and harmless while the queue is empty.
- The container was absent before this test and was removed afterwards (state restored).
- Rollback: set `SONORANLAB_COMFY_FLAGS` to the previous five flags.
