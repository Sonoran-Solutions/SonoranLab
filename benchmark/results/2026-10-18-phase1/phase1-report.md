# Phase 1 benchmark report — 2026-10-18 (owner-reviewed 2026-10-19; lead E owner-approved; 60-minute criterion not yet demonstrated)

Scope: DYL-70 … DYL-78 and DYL-97 (Linear project "Sonoran Lab", milestone "Phase 1 — Five-track benchmark").
Decision record: [ADR-0004](../../../docs/decisions/ADR-0004-lead-track.md). Scoring sheet: [`benchmark/scoring.csv`](../../scoring.csv).
Frozen inputs and rubric: [`benchmark/characters/2026-10-18.yaml`](../../characters/2026-10-18.yaml) (ADR-0007).

This repo is public and the Twos Are Up art is private. No images are committed. Every raw output, processed sheet,
manifest and contact sheet is kept under `~/sonoranlab-work/benchmark/results/2026-10-18/` (failed and refused attempts
included). The owner reviews the sets on the local page `~/sonoranlab-work/benchmark/review-2026-10-18/index.html`.

## What was measured

Target (ADR-0007): the game's own format. That means a **run** loop (samurai 7 frames, oathguard 6 @ 10 fps;
relay_sentinel's 4-frame hover idle), generated for N/NE/E/SE/S with the west side mirrored. Cells are set per
character (184/176/136) at 62 px/m, colours are locked to the shipped 255-colour palette, and the foot line sits at
cell − 4.

References: samurai (one-handed katana), oathguard (kite shield and helmet), relay_sentinel (floating construct).
PixelLab-generated sheets, used for benchmarking only.

Chain (identical for every track, DYL-71):
1. Alpha: BiRefNet, or the chroma key.
2. Edge-band magenta despill (2 HD px).
3. PixelOE k-centroid at thickness 0. ÷3 for C/E (canvas round16(cell × 3)); ÷4 for the svntax grids.
4. Palette lock.
5. Colour cap ≤ 64 (merge least-used).
6. Binary alpha and fringe clean.
7. Per-clip foot registration.
8. Validator: thresholds calibrated on the shipped clips.

## Tracks as run

| track | configuration | sets |
|---|---|---|
| A | Qwen Edit 2511 FP8 + Lightning + svntax combat LoRA (ckpt 2500), card prompt verbatim, 768² 6×4 grid | 3 chars × 2 seeds |
| B | FLUX.2 klein-base-4B + svntax small LoRA, edit + reference arm, 512² 4×4 grid | 3 chars × 2 seeds × ckpt 3000/2750 |
| C | Qwen Edit 2511 FP8 + Lightning; **image1 = Track D OpenPose skeleton, image2 = character still** (native keypoint input, no ControlNet), batched 7 frames/prompt, inputs centred on 1024² | samurai, oathguard × seeds 1–2 (+1 direction of seed 3) |
| D | Godot 4.7 orthographic bake of CC0 Quaternius rig + `Jog_Fwd_Loop`, OpenPose from true bones | 1 bake, byte-reproducible |
| E | Wan 2.2 I2V A14B Q5_K_M GGUF + Lightx2v 4-step from the approved per-direction still, BiRefNet → RGBA, 33 frames, loop picked by gait period | 3 chars × seeds 1–4 |

## Results

_All seeds are in: A/B 2 seeds, C 2 seeds (+1 direction of seed 3), E 4 seeds. The owner's human columns are filled in at sign-off._

Generation minutes for one full set (all directions), median per track and character (complete sets only):

| track | samurai (#1) | oathguard (#2) | relay_sentinel (#3) | validator |
|---|---|---|---|---|
| A | 2.4 | 2.1 | 2.2 | fails by format (4-dir 32×48 grid: no NE/SE, wrong cell) |
| B | 1.4 | 1.3 | 1.3 | fails by format (4-dir 32×32 grid) |
| C | **48.0** [47.0–49.1] | 39.3 | n/a (no humanoid skeleton) | FAIL every set: 10–27 issues, mostly bbox drift from the weapon |
| E | **15.9** [15.6–16.1] | 13.8 [13.6–14.4] | 9.3 [8.9–9.7] | humanoids: 3–10 issues per set, all bbox drift or foot offsets caused by the weapon/shield swing; relay_sentinel passes seed 2, 2 issues otherwise |

Cost per job (Strix Halo, ROCm 7.15, toolbox flags `--disable-mmap --gpu-only --disable-smart-memory --cache-none`):

| track | wall per job | GTT added | host MemAvailable drawn (median / max) |
|---|---|---|---|
| A (one grid) | 133 s | 41.7 GiB | 46.9 / 57.6 GiB |
| B (one grid) | 79 s | 21.9 GiB | 22.4 / 29.3 GiB |
| C (7-frame batch, one direction) | 550 s | 42.8 GiB | 58.8 / 60.8 GiB |
| E (33-frame clip, one direction) | 163 s | 18.4 GiB | 18.7 / 19.6 GiB |

### Quality, by track (orchestrator visual review of processed sheets; owner review pending)

- **A, B (svntax, experimental / no-ship).**
  - Chibi 4-direction grids that can't ship into this game's format.
  - B loses every accessory: the katana, the sash, the shield and helmet. It turns the floating sentinel into a small humanoid.
  - A keeps the samurai's colour blocking but drops the katana, and its "attack" frames are empty-hand swings.
  - They are a speed floor only.
- **C (pose-conditioned Qwen Edit).**
  - The InstantX ControlNet-Union does **not** enforce pose on Qwen Edit 2511. That held at strength 0.9 and 1.0, with and without Lightning (20 steps).
  - Qwen Edit's native keypoint-image input **does** enforce pose for side and three-quarter views.
  - Inputs must be centred on 1024². The encoder rescales references to about 1 MP, so a 560 px reference came out about 1.8× too large.
  - It does not produce a front or back run: N and S frames return the reference standing pose.
  - NE is drawn as a side view.
  - Identity drifts badly when a helmet hides the face: oathguard gets a different face per frame, loses the shield in the E run, and one frame has a green tunic.
  - It keeps the samurai's katana in every frame, but the blade angle varies frame to frame.
  - The mannequin-repaint variant left a ghost in the frame.
- **D (rig bake).**
  - Exact, reproducible poses and anchors in about 15 s on CPU.
  - It's a template source (C's skeletons), not finished art.
- **E (Wan 2.2 HD motion → pixels).**
  - Runs in **all five directions, including real front and back runs**.
  - Identity is consistent per clip: oathguard keeps helmet, tabard and shield in most directions.
  - The sentinel's hover is clean.
  - Failure modes:
    - **weapon retention**: the samurai's katana is lost in seed 1's S row and flickers in E/SE; oathguard's shield drops out for 4 frames of seed 2's S row;
    - **facing drift** in NE toward a side view;
    - **a green/yellow/pink cast on metal**: confirmed in Wan's raw frames, so a generation artifact of the magenta background;
    - a slightly small figure in some N clips.
  - Loop picking found clean gait periods (14–17 frames) in E/SE. N/NE/S often hit the floor, meaning weak periodicity.

### Follow-up: neutral background for Track E (controlled, 2 clips)

Setup: same seed (2), settings and prompt as the sweep, but the start still sits on #808080 grey instead of #ff00ff and
the prompt says "flat solid grey background". Clips: oathguard east and samurai north-east, job ids `E-greybg/*`;
these are outside scoring.csv.

Result: the green/yellow cast on the steel armour and blades is **gone** in Wan's raw frames, and BiRefNet mattes the
grey cleanly. One side effect: the samurai NE grey clip sometimes shows a second katana. With n = 2 clips this confirms
the direction; it isn't a sweep. Recommendation: Phase 3 generates Track E on neutral grey.

## Owner review (2026-10-19, 1× on the local review page)

| track | human usable (owner) | validator pass | combined usable (frozen rubric) |
|---|---|---|---|
| E | **12/12**: samurai 4/4, oathguard 4/4, relay_sentinel 4/4 | 1/12 (relay_sentinel seed 2) | 1/12 |
| C | samurai 2/2, oathguard 0/2 | 0/4 | 0/4 |
| A / B | 0/6, 0/12 | fail by format | 0 |

- **Owner:** C and E look mostly similar except oathguard, which wasn't good in C. The owner prefers E, mainly for speed,
  and is close to approving the sets for use in the game. The main gap they named is the missing white papercraft
  outline.
- **That outline is drawn by the game's `PixelStandee.gdshader` at runtime** (1 px dark keyline + 1 px white border
  from the alpha mask), not baked into sheets. Generated frames rendered through that shader logic get the same border
  as shipped frames. The local preview is `~/sonoranlab-work/benchmark/review-2026-10-18/standee_preview.png`.
- **Cleanup minutes were not timed**, so the end-to-end headline (generation + processing + cleanup ≤ 60 min) is not
  demonstrated. E's measured part is generation (samurai 15.9 min median) plus processing; that is headroom, not
  compliance.
- **Decision (ADR-0004):** owner-approved lead selection E. The recommendation is to proceed to Phase 2 with the
  validation limitations carried forward. Open: a timed cleanup on one representative E samurai set, unless the owner
  records an explicit exception.

## Evidence regeneration (2026-10-19, after senior review; no new inference)

Every scored set was rebuilt from the existing raw outputs:
- `benchmark/tools/assemble_set.py` selected attempts by an explicit rule (per part, the successful attempt with the
  highest retry index) and wrote `set/selection.json`, listing every attempt with its manifest sha256.
- `process.py --config benchmark/characters/2026-10-18-processing-v1.yaml` reprocessed each set. Every
  `set/processed/report.json` records the config sha256, the effective parameters (including `despill_px` and
  `max_colours`), input and output hashes, and the selection it is bound to.

Results:
- **All 34 reviewed processed sheets came out byte-identical** to the sheets on the owner's review page. The selection
  rule reproduces the sweep, and the chain is deterministic.
- The owner's review is bound to those artifacts by sha256 (`review-bound.json`, built from the page's displayed files,
  all older than the review export): `review_binding = bound` for every reviewed set.
- `scoring.csv` (35 rows) keeps every attempted set, separates selected-attempt time (`gen_minutes`) from all-attempt
  time (`attempt_minutes_total`), and splits `human_usable`, `validator_pass` and the combined `usable`.
- The medians are unchanged.

## Safety and operations (DYL-97 + follow-ups, PRs #6 and #12)

- Queue lifecycle and fail-closed inspection, as specified. Verified live: an interrupted job's window held the lock
  while its orphaned prompt drained, and `comfy.sh up --replace` refused the busy container.
- Found and fixed during the run:
  1. **GTT accounting.** A paused unit's GTT drop did not show up in MemAvailable within the settle window. The first
     fix (an additive credit) was withdrawn as the default after review; see below.
  2. **Qwen budget with a resident GPU tenant.** Qwen drew 57.5 GiB with llama-gemory running.
  3. **Restart race.** The preflight ran during a just-restored unit's reload. MemAvailable fell to **2.9 GiB**, and the kernel OOM killer chose ComfyUI via `oom-score-adj 1000`; the desktop was untouched.
  4. **Correction:** the container cgroup cap does not bound GTT.
- **Batched Track C** drew up to 60.8 GiB against the 52 GiB it requested (safe only because about 80 GiB was free).
  The supported recipe is now `--need-gib 64` for batched Qwen (documented in `env/runwindow.py` and
  `workflows/README.md`).
- **GTT credit (revised after senior review):** an additive credit for GTT a paused unit released can double-count
  pages TTM already returned to MemAvailable. It is now **off by default** (`--gtt-credit` opt-in). When enabled it
  subtracts any MemAvailable rise since the pause. The container cgroup cap is not a backstop for GTT.
- **Owner actions:** closed the host ComfyUI (175-prompt queue saved first); llama-gemory stopped and disabled at boot.
- **Operating rules learned:**
  - no heavy CPU work during Qwen windows; processing runs in a 6 GiB-capped unit;
  - stop sweeps by TERM to the main process only;
  - sweep units use `TimeoutStopSec=1900`, so systemd can't kill a draining window.

## Deviations from the frozen protocol (all recorded here and in ADR-0004)

1. C/E use canvas round16(cell × 3) and ÷3, not × 4. A 736² × 33 Wan clip costs 546 s against 194 s at 544, with no visible gain after pixelization.
2. Track C ran 2 seeds, not 4. Its generation time alone is 47–49 min per samurai set, so with front/back redraws it can't meet the 60-minute rule for character 1, whatever the seed. The remaining GPU time went to Track E's 4 seeds.
3. A/B ran as the owner-approved reduced floor: 2 seeds.
4. PixelOE thickness 0 was chosen from DYL-77. Thickness 0 round-trips shipped art exactly; thickness 2 adds dark blobs and exceeds the colour cap.
5. Colour cap and despill were added to the chain during the benchmark. Every track was reprocessed with them.

## Failed approaches (kept as evidence)

- InstantX ControlNet-Union on Qwen Edit 2511 at 0.9 and 1.0, with and without Lightning: pose ignored.
- Track D mannequin as Qwen image1: ghosting.
- Canvas-sized Qwen references: subject about 1.4–1.8× too large.
- Mask-only gait-period scoring: locked onto the half-stride.
- 736² Wan: 2.8× slower, no quality gain at 1×.
- svntax A/B for this game: wrong format, no accessory retention.

## Licensing

- Default-stack components used by the lead: Qwen Edit 2511, Wan 2.2 I2V A14B GGUF, Lightx2v, BiRefNet (official MIT weights) and PixelOE. All Apache-2.0/MIT; the provenance notes are in docs/04.
- svntax LoRAs (A/B) stay experimental/no-ship.
- Track D's rig is CC0 (Quaternius).
- Reference art is PixelLab-generated and approved for benchmark use only.
