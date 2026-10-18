# Phase 0 readiness report — 2026-10-18 (revised after senior review)

Scope: DYL-64 … DYL-69 (Linear project "Sonoran Lab", milestone "Phase 0 — Environment").
Exit criterion (PLAN.md): one Qwen Edit 2511 image and one klein-4B image generated **offline**, with timing
recorded; the box can be rebooted and brought back to this state from written notes in under 30 minutes.

## Recommendation: **GO for Phase 1 after merge and owner authorization** (host reboot test waived by owner)

Senior review (2026-10-18) accepted the offline inference evidence and the reboot waiver, and asked for revisions
before merge. All P1 and P2 items are addressed below. There are no blocking items left, apart from review and merge.

Both models run reliably and reproducibly on the Strix Halo box. They run fully offline, every weight is pinned and
verified by hash, and the image's pip layer is now hash-locked. A locked rebuild reproduced every Phase 0 output
byte-for-byte. Heavy runs are now gated by a host-level memory preflight that refuses and touches nothing when a run
doesn't fit. That was observed live at 14:58, when the owner's host ComfyUI was using the GPU.

**Owner waiver, 2026-10-18:** "I authorize continuing without doing the reboot validation." Not verified: cold boot →
working ComfyUI from the notes. That covers kernel/GTT params, podman storage, the CIFS automount and user services
coming back. Container-level recovery is covered. Follow-up: run `env/recover.sh` after the next reboot that happens
anyway.

Before Phase 1 work starts:
1. Senior re-review, then merge the stacked PRs #1 → #2 → #3 → #4 → #5 in order. Nothing is merged and auto-merge is
   off.
2. Owner authorization to start Phase 1; move the Phase 1 tickets out of Backlog. DYL-70 needs owner-approved references.
3. Operating practice: heavy runs go through `env/runwindow.py`. Pausing `llama-gemory` needs the owner's explicit
   `--allow-pause`. Any other GPU-memory holder, such as the host ComfyUI, causes a refusal, never an interruption.

## Senior review items

| item | resolution | where |
|---|---|---|
| P1-1 memory/run-window preflight | `env/runwindow.py`: inventories GPU-memory holders per systemd unit, checks MemAvailable (need + 8 GiB margin) and PSI. Refuses (exit 3) without stopping or running anything; pauses only `--allow-pause` units, only if needed; restores original state on success, failure and signals; state file + `--restore` for SIGKILLed runs; single-run lock. 13 tests. Live: refused at 14:58 with host ComfyUI holding memory; paused and restored `llama-gemory` in an equivalence run. | #3 |
| P1-2 smoke failures return nonzero | `env/comfy_client.py`: status_str + completed + error-message validation, expected non-empty outputs, per-request HTTP timeouts, overall deadline with interrupt + dequeue; exit codes 2/3/4/5/6; `smoke.sh` propagates them. 24 tests (fake ComfyUI server, smoke.sh end to end). Live: missing-model workflow → exit 2. | #3 |
| P2-3 recover.sh preserves llama-gemory state | `recover.sh` no longer touches services: it runs `runwindow.py --restore` first and the Qwen step inside the run window. Steps log their rc and failures name the step (this also fixed a log-swallowing redirect bug). 2 tests. Live: `llama-gemory` active before and after both refused runs. | #5 |
| P2-4 `comfy.sh up` must not destroy an active container | `up`/`down` refuse (exit 5) on a running container without `--replace`, and on a busy or unreadable queue without `--force`. 9 tests. Live: second `up` → exit 5. | #3 |
| P2-5 reproducible dependency lock | `env/requirements.lock.txt` (64 packages, SHA-256, `--require-hashes --no-deps`) + `env/pip-freeze.p0.txt` (163) checked at build time, plus the ROCm-stack check. Locked image `a4b70c4d9727` reproduced Qwen, klein, node chain and pixel outputs byte-identically, offline. Ceiling: 5 sdists built with unpinned build backends (`ponytail:` note). | #3 |
| P2-6 follow-up ticket for the 179 queued jobs | Not created, at the owner's direction: the owner closed their ComfyUI after saving the jobs, and it was not part of the incident. | n/a |
| Register caveats resolved by smoke tests | K3 flux2-vae → approved; BiRefNet/RMBG compatibility → verified. Remaining licensing caveats kept. | #2 |
| Hardlink coupling documented | Q3/W4 hardlinks into `~/ComfyUI/models`: in-place edits would propagate, `--verify` detects it, decoupling command given. | #2 |
| Incident record | Corrected: the owner's host ComfyUI was closed by the owner, not killed by oomd. | #4, this report |

## Tickets

| ticket | result | PR |
|---|---|---|
| DYL-64 pin and launch toolbox | Done. kyuz0 image pinned by digest `384aa1fe…`, rootless podman on 127.0.0.1:8189, toolbox flags unchanged. Healthy in 11 s, gfx1151/ROCm 7.15 visible. | #1 |
| DYL-68 weights, hashes, licenses | Done. 28 files / 86.9 GB, each pinned to a revision, 28/28 SHA-256 verified offline. `docs/04` rewritten with a status per file. | #2 |
| DYL-67 node packs + ops tooling | Done. Node packs pinned in a derived, hash-locked image (`a4b70c4d9727`). All 17 classes load; operational tests pass. Run-window guard, fail-closed smoke client and replace guard added, with 46 regression tests. | #3 |
| DYL-65 Qwen Edit 2511 FP8 + Lightning | PASS. Reproducible (byte-identical across runs), no `--force-fp32` needed. | #4 |
| DYL-66 klein-base-4B | PASS. Reproducible, no `--force-fp32`; the Apache-tagged flux2-vae loads. | #4 |
| DYL-69 offline + recovery gate | Offline PASS. Container recovery PASS (193 s, pre-review script). Revised runbook proven fail-safe live. Host reboot **waived by owner**. | #5 |

## Tested workflows

Original runs used image `1e37559bc4f2`; offline re-runs on the locked image `a4b70c4d9727` gave identical outputs. Flags:
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

Waived (not blocking, by owner decision):
- **Host reboot recovery test**: not run, see the recommendation above.

Operating constraints (not blocking):
- **Memory coexistence.** The first Qwen load, made while ~30 GiB of GTT was already in use, exhausted RAM. systemd-oomd
  killed the Claude desktop app scope: the agent session and the container's conmon. The owner's host ComfyUI had already
  been closed by the owner and was not affected. Guardrails in place:
  - a container memory cap of 64 GiB, with no swap and oom-score-adj 1000
  - conmon in its own scope
  - **`env/runwindow.py`**, the host-level preflight
  
  Consequence: Qwen Edit (`--need-gib 52`) only runs when the box is otherwise quiet. With the host ComfyUI holding
  models, it is refused. Wan 2.2 14B (Track E) will be the largest load; measure its peak before seed sweeps. The
  pause estimate is optimistic for mmap'd models; the settle check is authoritative.
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
- Hardlinked weights (Q3, W4) share inodes with the owner's `~/ComfyUI/models`. This is documented in `docs/04`, and
  `fetch_models.sh --verify` detects changes.
- The svntax tracks (A/B) remain valid **experimental baselines** but are not production-eligible until their
  licensing and provenance gates clear.

Not claimed (per DYL-69): ControlNet-Union pose/depth conditioning and Wan 2.2 video have **not** been tested.
Their weights are present and verified, and Phase 1 Tracks C and E test them.

## Phase 1 prerequisites (for the owner)

- DYL-70 needs three owner-approved Twos Are Up references and the frozen palette and profile.
- Decide whether Track A/B (svntax) results may influence the lead-track choice despite the no-ship status.
- Heavy runs go through `env/runwindow.py`. Say which units may be paused (`--allow-pause llama-gemory`). The guard
  refuses rather than interrupting anything else.
