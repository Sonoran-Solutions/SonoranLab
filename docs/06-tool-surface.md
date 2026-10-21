# 06 — Tool surface (MCP)

Phase 4 contract: [ADR-0010](decisions/ADR-0010-mcp-contract.md) (proposed).
One operations layer (`sonoranlab/api/`), two front ends:

```sh
sonoranlab api --list
sonoranlab api get_profile --params -  # stdin: {"project":"twos-are-up"}
sonoranlab mcp serve
```

The official SDK is pinned to `mcp==2.3.0`. Transport is local stdio only;
server name is `sonoranlab`. stdout contains only protocol (MCP) or JSON
(CLI); logging goes to stderr. Startup, initialize, list and read tools never
launch ComfyUI, Godot or Aseprite, or import torch/pixeloe. Capabilities report
ComfyUI `not_probed`, Godot's executable path or null, and editor runtime
presence only. An available editor runtime does not establish sandbox health.

## Tool inventory

Supported means part of the reconciled Phase 4 contract, not necessarily
implemented yet. **DYL-113 implements only `capabilities`, `list_projects`
and `get_profile`.** Other supported tools land in later tickets. There are
no placeholder tools in discovery. See ADR-0010 for each library/CLI mapping.

| Tool | Access / kind | Availability and purpose |
|---|---|---|
| `capabilities` | read-only / sync | Implemented: server version, API schema, tool metadata and capability flags |
| `list_projects` | read-only / sync | Implemented: `result.projects` = profiles/*.yaml as id, name, relative profile_path |
| `get_profile` | read-only / sync | Implemented: project id → resolved canvas, cells, directions, animations, limits, autonomy, protected export target; palette path + sha256 only |
| `list_characters` | read-only / sync | Supported, pending: registry summaries |
| `get_character` | read-only / sync | Supported, pending: references, history and artifacts |
| `list_jobs` | read-only / sync | Supported, pending: bounded job summaries |
| `job_status` | read-only / sync | Supported, pending: execution, technical and human status separately |
| `job_result` | read-only / sync | Supported, pending: result and artifact references |
| `get_artifact` | read-only / sync | Supported, pending: bounded metadata; images only as size-capped contact sheets |
| `check_review` | read-only / sync | Supported, pending: recompute artifact-bound human review from disk |
| `register_character` | mutating / sync | Supported, pending: copy references, initially unapproved |
| `validate` | mutating / sync | Supported, pending: validator report in a new output directory |
| `contact_sheet` | mutating / sync | Supported, pending: new review image output directory |
| `compare` | mutating / sync | Supported, pending: new side-by-side contact and report output directory |
| `animate` | mutating / job | Supported, pending: Track E locomotion |
| `pose_frame` | mutating / job | Supported, pending: Track C single pose / posed first frame only |
| `process` | mutating / job | Supported, pending: deterministic pixel chain |
| `export_godot` | mutating / job | Supported, pending: guarded export into STATE, never the game checkout |
| `godot_check` | mutating / job | Supported, pending: headless import/playback check in isolated output |
| `cancel_job` | mutating / sync | Supported, pending: request cancellation without claiming an unknown outcome succeeded |
| `retry_job` | mutating / job | Supported, pending: explicit new job linked by parent_job_id |
| `prepare_repair` | mutating / sync | **Optional**, pending: ADR-0009 scoped workspace handoff |
| `inspect_repair` | read-only / sync | **Optional**, pending: workspace metadata |
| `inspect_repair_diff` | read-only / sync | **Optional**, pending: bounded verified pixel diff |
| `finalize_repair` | mutating / job | **Optional**, pending: new validated child job; new review required |
| `abort_repair` | mutating / sync | **Optional**, pending: abort the repair workspace |

Optional repair tools register only when `editor=available`; the core works
without the editor. This is authorized pixel repair through the contained
broker, not diffusion inpainting. No arbitrary editor operations or Lua.

**Deferred / not tools:** approval of references and recording human review
are owner CLI operations. `generate_character` (Track A), `regenerate_region`
(diffusion inpaint), `smoke_render` beside another character,
`list_pose_templates`, `bake_rig`, automatic C→E fallback, arbitrary graphs,
shell, Lua and profile overrides are excluded. Reasons and existing
operations are recorded in ADR-0010. Track A is not the default; GitHub issue
#40 (Qwen Image 2.1 spike) changes no defaults.

## Envelope and CLI exits

Every tool returns an object in schema `sonoranlab.api/1`:

```json
{"schema":"sonoranlab.api/1","tool":"list_projects","ok":true,"result":{"projects":[]}}
```

```json
{"schema":"sonoranlab.api/1","tool":"get_profile","ok":false,"error":{"code":"not_found","message":"Unknown project","field":"project","retryable":false,"hint":null}}
```

MCP returns the same object as `structuredContent` and JSON text content,
with `isError` reflecting `ok`. CLI exit 0 means ok; 1 is a tool failure
(including refused / validation FAIL); 2 is usage or schema error. Unknown
or disabled tool names are `invalid_params` (exit 2). `api --list` prints
registry metadata as a JSON array. Params default to `{}` and must satisfy
the exact tool JSON schema; unknown properties are refused.

Error codes: `invalid_params`, `not_found`, `conflict`,
`idempotency_conflict`, `path_refused`, `unavailable`, `refused`,
`budget_refused`, `guard_busy`, `internal`. Expected errors include a field,
retryability and hint (nullable). Unknown exceptions return `internal` with
the exception type; no traceback in the result or stdout.

## State, paths and IDs

`STATE = $SONORANLAB_STATE`, default `~/sonoranlab-work/mcp`.
`READ = $SONORANLAB_READ_ROOTS` (os.pathsep list), default
`~/sonoranlab-work`; STATE is also readable. Project profiles are read-only
repository files. Caller input paths must exist, resolve strictly inside a
read root or STATE and be regular files. Reject `..`, symlink escapes,
devices and other non-regular files. Relative inputs use cwd; relative
outputs use STATE. Output parents must already exist; the leaf must be new
and created exclusively. Helpers perform no creation during discovery.
Game checkouts from profile `export.godot_project` are always protected,
even if inside STATE/READ; only the profile's recorded palette may be read
by the library's palette loader. Export never writes into the game.

```text
STATE/registry/<project>/<character>/character.json
                                    refs/<ref_set_id>/still_<dir>.png
STATE/jobs/<job_id>/job.json         (+ events.jsonl, request.json, lease.lock)
STATE/artifacts/<project>/<character>/<anim>/<job_id>/
STATE/idempotency/<sha256(key)>.json
```

Copied references are mode 0444 with sha256 recorded. Project = profile id;
character = profile character key. `ref_set_id = refs-` + first 12 hex of
sha256 over the sorted (direction, file sha256) list. Job IDs are
`job-<UTC yyyymmddThhmmssZ>-<8 random hex>`. Artifact refs are
`<job_id>:<relative path>`; outputs use IDs and STATE-relative refs, with
absolute paths only in informational fields. Writes use same-directory
temporary files + fsync + os.replace + directory fsync; exclusive creates
use O_EXCL/mkdir; record read-modify-write holds an fcntl lock.
These record writers are later-ticket work, not part of this skeleton.

## Execution, technical and human status

Never collapse these three meanings:

- `job.state`: queued, running, cancelling, cancelled, succeeded, failed, blocked.
- `job.stage`: prepare, generate, select_and_process, validate_export, check
  (informational, measured).
- `technical`: validator = pass/fail/null; godot = pass/fail/not_run/unavailable.
- `human`: review = none/pass/fail/invalid; production_usable = boolean.
  Always recompute from disk using `review_errors` / `production_usable`;
  never cache approval.

`succeeded` means the pipeline completed, including a reported technical
FAIL; it does not mean approved. `blocked` means unknown outcome or a need
for reconciliation (for example a dead worker after submitting a prompt).
Never auto-retry or resubmit a blocked job. An explicit retry creates a new
job with `parent_job_id`. Long work returns a job_id immediately.

## Reconciled acceptance sequence

Approved-reference registration → `animate` (Track E: five generated
directions, mirrored animation rows, eight independent stills) → inspect
via job/artifact/contact tools → validate → export → Godot check; human
review remains a separate owner action. Track C provides a single pose or
posed first frame, with no automatic C→E fallback. Track E leads locomotion.
The old `generate_character` → `animate` → `validate` → `export_godot`,
`walk_4dir`, Track A exit sequence is superseded, **not passed**.

Generation must reuse `env/runwindow.py` and `sonoranlab/comfy/guard.py`:
Track E needs at least 40 GiB, Track C at least 60 GiB, plus the guard's
8 GiB margin. No lower budgets, `--gtt-credit` or new memory controller.
