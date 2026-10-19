# ComfyUI workflows

API-format JSON only (the dict ComfyUI's `/prompt` accepts), one folder per
track. See ADR-0002.

```
workflows/<track>/
  <name>.json         API-format workflow
  <name>.params.yaml  maps logical inputs → {node_id, input_name} (a list = patch every entry)
  README.md           models required, flags (e.g. --force-fp32), notes from the benchmark
```

Example `params.yaml`:

```yaml
reference_image: {node: "12", input: "image"}
control_image:   {node: "27", input: "image"}
prompt:          {node: "6",  input: "text"}
seed:            {node: "3",  input: "seed"}
steps:           {node: "3",  input: "steps"}
width:           {node: "5",  input: "width"}
height:          {node: "5",  input: "height"}
output_prefix:   {node: "9",  input: "filename_prefix"}
```

To export from the ComfyUI UI: enable dev mode, "Save (API Format)". Strip
absolute paths before committing. Workflows are hashed into every job
manifest, so a change here is visible in every artifact it produced.

## Workflows

| track | workflow | what | models (`env/models.tsv`) |
|---|---|---|---|
| A | `grid/qwen_edit_svntax_combat.json` | Qwen Edit 2511 FP8 + Lightning + svntax combat LoRA, 768², one 6×4 sheet | Q1–Q4, S1 (or S2–S4) |
| B | `grid/klein_base_svntax_small.json` | klein-base-4B + svntax small LoRA, 512², text only | K1–K3, S5 or S6 |
| B | `grid/klein_base_svntax_small_ref.json` | same + the reference still via `ReferenceLatent` | K1–K3, S5 or S6 |
| C | `pose/qwen_edit_controlnet_union_pose.json` | Qwen Edit 2511 + InstantX ControlNet-Union, OpenPose control, 1 frame | Q1–Q5 |
| C | `pose/qwen_edit_controlnet_union_depth.json` | same, depth from Depth Anything V2 Small (vits) | Q1–Q5, P3 |
| E | `video/wan22_i2v_lightx2v.json` | Wan 2.2 I2V A14B Q5_K_M + Lightx2v 4-step, then BiRefNet → RGBA PNG frames | W1–W6, B1 |

UNVERIFIED items (svntax prompts and recipes) are listed at the top of each `params.yaml`. Track C wiring
evidence: `pose/README.md`.

Check a workflow without a GPU. This does not POST to `/prompt`; it only reads `/object_info` or a saved dump of it:

    python3 benchmark/tools/check_workflow.py --object-info ~/sonoranlab-work/object_info-2026-10-18.json workflows/*/*.json

It checks class names, required inputs, link arity and types, ranges, combo values (every model filename must be in
the server's list), and that every `params.yaml` entry names a literal input. Re-dump `object_info` after any image
or node-pack change.

## Job runner (`benchmark/tools/run_track.py`)

The runner turns one workflow + `params.yaml` into one job id `<date>/<track>/<char>/<seed>[/<part>]`. It runs
through `env/smoke.sh` (memory guard, queue drain, sampling), then keeps outputs, the patched workflow and a manifest
under `$WORK/benchmark/results/<job>/`.

Reservation semantics: a job id can be used exactly once. Before staging any input the runner atomically reserves
`$WORK/benchmark/results/<job>/` (`mkdir` without `exist_ok`, then an exclusive `reservation.json` holding job, pid,
host, creation time and argv), so two launches with the same id can never interleave their staged files. An
interrupted attempt keeps its directory and therefore stays reserved; retry with a new job id (the sweep scripts
already do `<id>-r1`, `-r2`, ...). A second launch exits 2 with
`job <id> is already reserved (in progress, interrupted, or done); use a new job id`. Job id components must match
`^[A-Za-z0-9._-]+$` and are never `.` or `..` (also exit 2).

## Estimated memory (from file sizes; measure before trusting)

Observed `--need-gib` values: batched Qwen jobs (`batch_frames.py`, 7 frames per prompt) request `--need-gib 64`
(measured 60.8 GiB draw); single Qwen 60, Wan 33 frames 40, klein 30.

Phase 0 measured, at 1720×968: Qwen Edit FP8 + Lightning, 31.0 GB of weights, peaked at 39.8 GiB GTT and 51.2 GiB
container cgroup. klein base, 16.1 GB of weights, peaked at 23.7 GiB GTT at 1024² (`benchmark/results/phase0-env.md`).
The rows below scale from those runs. Record real peaks in `benchmark/results/`.

| workflow | weights loaded | estimate | suggested `--need-gib` |
|---|---|---|---|
| A svntax combat | 31.6 GB (+S1 0.59) | ≈ 40 GiB GTT, ≈ 52 GiB cgroup (768² is smaller than the Phase 0 1.66 MP run) | 52 |
| B klein + small LoRA | 16.2 GB (+S5 0.09) | ≤ 24 GiB GTT at 512² | 30 |
| C ControlNet pose/depth | 34.6 GB (+Q5 3.54, +P3 0.1) | ≈ 43 GiB GTT, ≈ 55 GiB cgroup, close to the 64 GiB container cap | 56 |
| E Wan 2.2 + BiRefNet | 31.5 GB (W1+W2 21.6, W3 6.7, W5+W6 2.5, W4 0.25, B1 0.44) | unmeasured. Expect ≈ 35–45 GiB GTT if both experts stay resident; 17 × 544² latents are small | 52 |

## Input images

`env/comfy_client.py` and `env/smoke.sh` **do not upload anything**. They POST the workflow JSON as-is. Phase 0 only
used images that ship inside the toolbox image's `/opt/ComfyUI/input` (`ai-server.jpg`, `example.png`). Only
`$WORK/output` is mounted, so a runner has no path today that puts a reference still, control image or start frame
where `LoadImage` can see it.

`LoadImage` accepts any existing file below the input dir, subfolders included: its `VALIDATE_INPUTS` replaces the
combo-list check. New workflows therefore name their images `sonoranlab/<file>`.

Smallest proposed fix (not applied; `env/` is out of scope for this change): one extra read-only mount in
`env/comfy.sh up`:

    -v "$WORK/input:/opt/ComfyUI/input/sonoranlab:ro" \

with `mkdir -p "$WORK/input"` next to the existing `mkdir -p "$WORK/output"`. The image's own inputs stay where they
are, so the Phase 0 smokes are unchanged. It works with `--offline` too, because a mount needs no network. The runner
then:
1. reserves the job (see "Job runner" above) and copies each input to the nested path
   `$WORK/input/<date>/<track>/<char>/<seed>[/<part>]/<name>.png` (never into the repo; ADR-0007);
2. patches the `params.yaml` image input to `sonoranlab/<date>/<track>/<char>/<seed>[/<part>]/<name>.png`;
3. records each input's sha256 in the job manifest, next to the workflow hash.

Using the job id as a nested path (instead of flattening `/` to `_`) keeps staging collision-free: `a/b_c` and
`a_b/c` no longer land on the same directory.

Stopgap with no env change: `podman exec sonoranlab-comfy mkdir -p /opt/ComfyUI/input/sonoranlab`, then
`podman cp <file> sonoranlab-comfy:/opt/ComfyUI/input/sonoranlab/<name>.png`. It writes
into the container layer, so the file is gone when `comfy.sh up --replace` recreates the container.
`POST /upload/image` also works, but only `smoke.sh`'s in-container client can reach the API offline, and it would
need a multipart upload added.
