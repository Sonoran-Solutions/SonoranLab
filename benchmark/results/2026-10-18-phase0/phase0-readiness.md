# Phase 0 readiness report — 2026-10-18

Scope: DYL-64 … DYL-69 (Linear project "Sonoran Lab", milestone "Phase 0 — Environment").
Exit criterion (PLAN.md): one Qwen Edit 2511 image and one klein-4B image generated **offline**, with timing
recorded; the box can be rebooted and brought back to this state from written notes in under 30 minutes.

## Recommendation: **GO for Phase 1, conditional**

Both models run reliably and reproducibly on the Strix Halo box. They also run fully offline, and every weight is
pinned and verified by hash. One exit-criterion item is still open: the **host reboot** recovery test. The scripted runbook already
passes a container-level restart in 193 s, against a 30-minute target. GO becomes unconditional when:

1. The owner reboots, runs `bash ~/src/SonoranLab-local/env/recover.sh`, and it prints `RECOVERY PASS` in under
   30 minutes. Add the row to `phase0-env.md` → "Restart / reboot recovery".
2. The owner reviews the stacked PRs (#1 → #2 → #3 → #4 → DYL-69 PR) and merges them in that order.
3. The owner accepts the operating constraint below: Qwen Edit needs ≈ 50 GiB free, so other model-holding services
   pause during heavy runs.

If the reboot test fails or takes longer than 30 minutes, the recommendation becomes **HOLD**. Then fix the
runbook and re-test. Phase 1 tickets stay in Backlog until the owner records GO on DYL-69.

## Tickets

| ticket | result | PR |
|---|---|---|
| DYL-64 pin and launch toolbox | Done. kyuz0 image pinned by digest `384aa1fe…`, rootless podman on 127.0.0.1:8189, toolbox flags unchanged. Healthy in 11 s, gfx1151/ROCm 7.15 visible. | #1 |
| DYL-68 weights, hashes, licenses | Done. 28 files / 86.9 GB, each pinned to a revision, 28/28 SHA-256 verified offline. `docs/04` rewritten with a status per file. | #2 |
| DYL-67 node packs | Done. PixelOE v1.0.0, PixelGridHelpers v1.1.1, controlnet_aux, RMBG and GGUF are pinned in a derived image. All 17 required node classes load, and the pixel and pose/depth/alpha operational tests pass. | #3 |
| DYL-65 Qwen Edit 2511 FP8 + Lightning | PASS. Reproducible (byte-identical across runs), no `--force-fp32` needed. | #4 |
| DYL-66 klein-base-4B | PASS. Reproducible, no `--force-fp32`; the Apache-tagged flux2-vae loads. | #4 |
| DYL-69 offline + recovery gate | Offline PASS. Container recovery PASS (193 s). **Host reboot pending (owner)**. | DYL-69 PR |

## Tested workflows

All runs use image `localhost/sonoranlab-comfy:p0` (`1e37559bc4f2`) with flags
`--disable-mmap --gpu-only --disable-smart-memory --cache-none --bf16-vae`. Each has a manifest in this folder.

| workflow | online | offline (`--network=none`) | output |
|---|---|---|---|
| Qwen Image Edit 2511 FP8 + Lightning, 4 steps, 1720×968 (stock toolbox workflow, UNET → fp8mixed) | 122.9 s cold / 106.1 s | 114.8 s | `fea3c149…`, identical in every run |
| FLUX.2 klein-base-4B t2i, 20 steps, CFG 5, 1024² (ComfyUI template base subgraph) | 116.3 / 116.2 s* | 86.2 s | `1918e034…`, identical in every run |
| klein character → DWPose (ONNX/CPU) + Depth V2 Small + BiRefNet-general | 96.5 s | 90.4 s | 4 images, identical online and offline |
| PixelOE k-centroid 4× → Enforce Palette; Median Fixer | 1.5 s | not run | all pixels in palette; Median Fixer gives 191 px, not 192 |

\* A ~30 MB/s model download was writing to the same NVMe during the online klein runs. 86 s is the clean figure.

## Performance and memory

| workload | GTT peak | container cgroup peak | host MemAvailable drop |
|---|---|---|---|
| Qwen Edit FP8 | 39.8 GiB | 51.2 GiB | ~37–49 GiB |
| klein-base-4B | 23.6 GiB | ≤ 51.2 GiB | ~23 GiB |

Every run reloads all models because of `--cache-none` (toolbox default), so load time is part of every number above.
Phase 1 can test removing `--cache-none` (one flag at a time) to speed up seed sweeps.

## Unresolved items and risks

Blocking GO:
- **Host reboot recovery test**: owner action, see above.

Operating constraints (not blocking):
- **Memory coexistence.** The first Qwen load ran next to the owner's ComfyUI and llama-gemory (~30 GiB GTT). It
  exhausted RAM, and systemd-oomd killed the whole Claude desktop app scope, including the owner's host ComfyUI. Three
  guardrails are now in place:
  - a container memory cap of 64 GiB, with no swap and oom-score-adj 1000
  - conmon in its own scope
  - heavy runs pause `llama-gemory` and run under `systemd-run --user`
  
  Wan 2.2 14B (Track E) will be the largest load. Phase 1 should check its peak before running seed sweeps.
- **The repo is on a Wi-Fi CIFS share.** It runs at ~11 MB/s and `git status` takes ~45 s. Weights are on local NVMe and
  git work happens in `~/src/SonoranLab-local`. Wired Ethernet or a local checkout would make this go away.
- **DWPose runs on CPU** (onnxruntime has no ROCm GPU wheel). This is fine for pose extraction, but slow at volume.
- Depth Anything triggers a non-blocking hipBLASLt → hipBLAS fallback warning on gfx1151.
- Two transitive opencv builds (5.0.0) are installed next to opencv-headless. `cv2` works today; watch it if a node breaks.

License items. These block **shipping**, not benchmarking. Each needs an owner decision or an ADR:
- svntax combat/small LoRAs (Tracks A/B): experimental, no-ship. The small LoRA's training template is CC-BY-4.0,
  which would carry an attribution obligation; the combat LoRA's card states no training data.
- SAM 3.1 (Phase 6): the Meta SAM License is custom, so it stays experimental until an ADR decides.
- ComfyUI-RMBG is GPL-3.0 tool code. That doesn't attach to generated assets, but the code must not be vendored. We
  use official MIT BiRefNet weights, not its unlicensed default mirror.
- ComfyUI-PixelGridHelpers has no LICENSE file; only its pyproject says MIT. Ask upstream, or vendor the palette
  step in Phase 2, which is code anyway.
- Depth Anything V2: only Small (vits) is allowed. The node defaults to the CC-BY-NC vitl, so workflows must pin vits.
- Not yet verified: whether the Apache-tagged flux2-vae copy is tensor-identical to the template's copy. It loads and
  produces correct images; the template's copy sits in a non-commercial-tagged repo and is excluded.

Not claimed (per DYL-69): ControlNet-Union pose/depth conditioning and Wan 2.2 video have **not** been tested.
Their weights are present and verified, and Phase 1 Tracks C and E test them.

## Phase 1 prerequisites (for the owner)

- DYL-70 needs three owner-approved Twos Are Up references and the frozen palette and profile.
- Decide whether Track A/B (svntax) results may influence the lead-track choice despite the no-ship status.
- Approve the run-window practice: pause llama-gemory and keep the host ComfyUI idle during benchmark runs.
