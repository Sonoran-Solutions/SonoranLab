# 01 — Architecture

## Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  Orchestrator (Claude Code / Opus)                                    │
│  "/new-class" skill: refs + spec in, class in Godot out               │
└───────────────┬──────────────────────────────────────────────────────┘
                │ MCP tool calls (docs/06)
┌───────────────▼──────────────────────────────────────────────────────┐
│  SonoranLab MCP server  (sonoranlab/mcp)                              │
│  thin wrappers over CLI verbs; job ids for long work; artifact store  │
└───────┬──────────────────────────────────────┬───────────────────────┘
        │                                      │
┌───────▼───────────────────────┐   ┌──────────▼────────────────────────┐
│  Generation adapters          │   │  Deterministic core               │
│  (sonoranlab/comfy)           │   │  (sonoranlab/*)                   │
│  Track A: grid LoRA           │   │  pixelize · palette · alpha       │
│  Track C: pose-conditioned    │   │  slice · register · validate      │
│  Track E: HD animate (i2v)    │   │  contact · export/godot           │
│  workflows/*.json (API form)  │   │  pure Pillow/NumPy/pixeloe        │
└───────┬───────────────────────┘   └───────────────────────────────────┘
        │ HTTP /prompt /history
┌───────▼───────────────────────────────────────────────────────────────┐
│  ComfyUI (ROCm toolbox on Strix Halo)                                  │
│  Qwen-Image-Edit-2511 · ControlNet-Union · klein-4B · Wan 2.2 I2V      │
│  BiRefNet · SAM3 · PixelOE node (optional, core wraps the lib directly)│
└────────────────────────────────────────────────────────────────────────┘
```

The boundary that matters is between the two middle boxes. **Everything to the
left is nondeterministic and slow; everything to the right is deterministic
and fast.** The right-hand box has no ComfyUI dependency and can run on a
laptop. The validator in the right-hand box is the only quality gate.

## Data flow for one animation

```
profile.yaml ──┐
character refs ─┼─► [generate_character] ─► candidates/*.png ─► contact.png ─► pick
                │
pose templates ─┼─► [animate: track C or E] ─► raw/frames/*.png   (HD, e.g. 512–768px)
                │
                └─► `sonoranlab process` (sonoranlab/pipeline.py), one cell at a time:
                    [alpha]     key/alpha extract (bg_key, key_tol); magenta despill (2 HD px)
                    [pixelize]  PixelOE k-centroid ÷N, N = raw edge / cell (outline thickness 0)
                    [alpha]     block-mean alpha ≥ threshold
                    [palette]   snap to the profile palette; count off-palette before snap
                    [alpha]     drop key-coloured fringe on the silhouette border
                    [palette]   cap colours per cell (least used merged)
                    [register]  one shift per direction: foot line / bbox bottom / rig anchor
                    [validate]  rules → report.json (pass/fail + numbers)
                    [contact]   8× sheet with badges for review
                    [export]    PASS only: game-layout sheet + SpriteFrames .tres → job/export/
```

Every run writes one job dir (`--out`; must be new or empty, never overwritten):

```
raw/            byte copies of the inputs (read-only); the chain reads only these
processed/      01_alpha 02_pixelize 03_palette 04_clean 05_registered (one PNG per frame)
report.json     validator output
contact.png     review image
manifest.json   profile + input + output hashes, every parameter, tool versions, timings,
                git commit, upstream selection.json / manifest.json (seed, set)
export/         only on a validator PASS (docs/09); copying into the game is a human step
```

`manifest.json` makes any artifact reproducible and lets the orchestrator
reason about what it already tried.

## Where "consistency" comes from

| Problem                          | Solved by                                   | Not solved by          |
|----------------------------------|---------------------------------------------|------------------------|
| Same character across frames     | Reference-conditioned edit / i2v temporal coherence (model) | prompting "keep it the same" |
| Correct pose per frame           | Pose template + ControlNet / rig bake (template) | text layout prompts    |
| Pixel grid alignment             | PixelOE k-centroid in code                  | nearest-neighbor downscale |
| Palette                          | Enforce Palette in code                     | prompt                 |
| Feet don't slide                 | Foot-anchor registration in code            | center-of-mass         |
| Clean alpha                      | BiRefNet + threshold + fringe clean in code | model "transparent bg" |
| Nothing clipped                  | Validator edge check + larger gen canvas    | hope                   |

## Tracks and when each is used

See `docs/05-pipeline-tracks.md`. Short version: Track E (HD animate, then
pixelize) is expected to lead for locomotion; Track C (pose-conditioned edits)
for specific held poses and attack keyframes; Track A as a cheap first-draft
generator for static candidates. The adapter interface is identical across
tracks so the orchestrator can switch tracks as a retry strategy.

## Why Python

PixelOE, ComfyUI, the MCP SDK, Pillow, NumPy, and every custom node are
Python. A Node/TypeScript implementation would spend most of its time shelling
out to Python. The orchestrator-facing surface is MCP, which is language-
agnostic, so nothing is lost on the agent side.

## Why not inside ComfyUI

The deterministic core could be a custom node pack. It isn't, because:
- it must run without a GPU box up (tests, laptop, CI);
- validators need to be unit-tested with synthetic inputs;
- the Godot exporter has nothing to do with inference.

ComfyUI custom nodes (PixelOE, PixelGridHelpers) are still installed for
interactive experimentation in Phase 1; the production chain calls the
libraries directly.

## ComfyUI client contract

`sonoranlab.comfy` is an isolated generation boundary: `Workflow.load` reads API
JSON plus its `.params.yaml` sidecar; `patch` copies named inputs. `ComfyClient`
submits, waits, cancels, collects local output files, or reattaches to a persisted
prompt UUID without resubmitting. It defaults to loopback only; remote hosts
require `allow_remote=True`. It never starts ComfyUI or replaces `env/runwindow.py`.

Persist a canonical UUID before submitting when crash recovery matters. Lost
submission responses trigger an ID lookup before any retry, using the same ID
(up to three POST attempts). `submit_unknown` means reattach that ID after
restoring connectivity; never retry under a fresh ID. Reusing an attempted or
existing ID is rejected. Cancellation uses `/api/jobs/{id}/cancel`, falling back
only on 404/405 to a scoped queue delete and `interrupt` with `prompt_id`.
Failures after submission cancel and confirm queue removal; `cancel_unconfirmed`
means GPU work may still be live and the run window must remain guarded.

`run` and `reattach` return JSON-serializable `RunRecord`s with structured errors,
verbatim history messages, parameters, source/patched graph hashes, model hashes
from `env/models.tsv` (unknown names marked `unknown`), and validated output
paths, sizes and hashes. Output paths must remain inside `output_root` and files
must be nonempty; expected counts can be total or per node. Timings use monotonic
seconds for submission, observed queue wait, execution and wall time;
`timing.poll_errors` counts transient polling failures. Reattachment timings
cover the current attachment, not the earlier process lifetime.

Transport is pluggable: HTTP to the loopback port by default, or `PodmanExecTransport` for a container started with
`env/comfy.sh up --offline` (`--network=none`), whose API exists only inside the container. Each request is then
relayed by `podman exec -i sonoranlab-comfy python -c <relay>` to `127.0.0.1:8188` there; outputs still arrive in the
host-mounted `$WORK/output`, so `output_root` is unchanged. This is the path the offline acceptance runs use.

## Optional editor companion (Phase 3.5)

Pilot, not part of the core: the deterministic pipeline imports no editor code and works without it (ADR-0009,
status proposed). SonoranLab stays the authority; Aseprite is reachable only through a broker.

```
 SonoranLab (host)                      bwrap sandbox (--unshare-all, private netns, lo only)
 +------------------------+  stdio MCP  +---------------------------------------------+
 | broker: allowlist,     |<----------->| upstream `serve` (no --allowLua, no headless)|
 | lease, mask/diff check,|             |     |  127.0.0.1 bridge (sandbox-only)       |
 | restore, finalize      |             |     v                                        |
 +-----------^------------+             | Aseprite + extension --- Xephyr display     |
             |                          |                                              |
  editor worker / critic                |  writable: workspace/ (+ private HOME, tmp)  |
  (broker CLI only, no                  +---------------------------------------------+
  upstream MCP)                           read-only: /usr, /etc, Aseprite, node, build
```

Repair job layout (`<repair>/`, reserved atomically by `repair prepare`):

| path | content |
|---|---|
| `repair.json` | ids, parent job and profile hashes, tool versions (created exclusively) |
| `source/` | byte-exact copies of the approved frames and stills, mode 0444, sha256 recorded |
| `mapping.json` | Aseprite frame number to (kind still\|anim, direction, index) |
| `workspace/` | `doc.aseprite` and `export/<frame>.png`; the only writable bind in the sandbox |
| `authorization.json` | allowed ops, per-frame masks, mirror derivations, max iterations; written before a session, hash-bound |
| `session/` | `lease.lock`, lease token, op log, private HOME |
| `export/` | finalize output used to build the child job |
| child job | normal `artifacts/` layout plus `repair-manifest.json`; needs its own review, the parent's approval does not transfer |
