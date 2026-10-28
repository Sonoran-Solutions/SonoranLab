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
implemented yet. **DYL-113 implements `capabilities`, `list_projects` and `get_profile`;
DYL-114 adds the six registry tools below.** DYL-115 adds durable job reads, cancellation and retry. Other supported tools land in later tickets. There are
no placeholder tools in discovery. See ADR-0010 for each library/CLI mapping.

| Tool | Access / kind | Availability and purpose |
|---|---|---|
| `capabilities` | read-only / sync | Implemented: server version, API schema, tool metadata and capability flags |
| `list_projects` | read-only / sync | Implemented: `result.projects` = profiles/*.yaml as id, name, relative profile_path |
| `get_profile` | read-only / sync | Implemented: project id → resolved canvas, cells, directions, animations, limits, autonomy, protected export target; palette path + sha256 only |
| `list_characters` | read-only / sync | Implemented: profile/registry union and verified approvals |
| `get_character` | read-only / sync | Implemented: references, current review and imported job lineage |
| `list_jobs` | read-only / sync | Implemented: bounded/filterable durable summaries (limit ≤200), offline lease reconciliation |
| `job_status` | read-only / sync | Implemented: execution, measured file progress/timing, compact `result_summary` and separate technical/human status |
| `job_result` | read-only / sync | Implemented: terminal result summary/error and paged artifact references |
| `get_artifact` | read-only / sync | Implemented: bounded metadata only, no image pixels or text bodies |
| `check_review` | read-only / sync | Implemented: recompute artifact-bound human review from disk |
| `import_job` | mutating / sync | Implemented: index existing jobs and hashes without modifying them |
| `register_character` | mutating / sync | Implemented: immutable still copies, initially unapproved |
| `validate` | mutating / sync | Implemented (DYL-117): validator report in a new output directory |
| `contact_sheet` | mutating / sync | Implemented (DYL-117): new review image output directory |
| `compare` | mutating / sync | Implemented (DYL-117): new side-by-side contact and report output directory |
| `animate` | mutating / job | Implemented (DYL-116): Track E profile animation |
| `pose_frame` | mutating / job | Implemented (DYL-116): Track C single pose / posed first frame only |
| `process` | mutating / job | Implemented (DYL-117): deterministic pixel chain |
| `export_godot` | mutating / job | Implemented (DYL-117): guarded export into STATE, never the game checkout |
| `godot_check` | mutating / job | Implemented (DYL-117): headless import/playback check in isolated output |
| `smoke_render` | mutating / job | Implemented (DYL-131): isolated side-by-side Godot render of a candidate beside up to 3 approved siblings |
| `cancel_job` | mutating / sync | Implemented: durable intent, worker TERM, prompt-scoped cancellation and stop confirmation |
| `retry_job` | mutating / job | Implemented: explicit new attempt from frozen request, linked by parent_job_id |
| `prepare_repair` | mutating / sync | **Optional**, implemented (DYL-118): ADR-0009 scoped workspace handoff |
| `inspect_repair` | read-only / sync | **Optional**, implemented: identity, recorded authorization, writer lease, journal summary |
| `inspect_repair_diff` | read-only / sync | **Optional**, implemented: bounded pixel diff and unauthorized changes |
| `finalize_repair` | mutating / job | **Optional**, implemented: heavy CPU acceptance into a new child; fresh review required |
| `abort_repair` | mutating / sync | **Optional**, implemented: abort under the existing writer lease |

Optional repair tools register only when `editor=available`; the core works
without the editor. This is authorized pixel repair through the contained
broker, not diffusion inpainting. No arbitrary editor operations or Lua.

## Optional repair handoff (DYL-118)

All five tools share the presence-only check used by `capabilities.editor`:
`SONORANLAB_EDITOR_RUNTIME`, default `~/sonoranlab-work/phase3.5/runtime`,
must be a directory. Missing runtime means `editor: "unavailable"`, no repair
tools in discovery, and `invalid_params: Unknown or disabled tool` for direct
calls. Core tool discovery remains unchanged. Presence does not prove runtime
health; prepare/finalize can still report converter unavailability. Startup,
discovery and disabled calls do not import repair session/runtime code.

Every schema rejects extra keys. `project`, `character`, `anim` use repository
profile IDs. `repair_id` is the directory basename matching
`^[a-z0-9][a-z0-9_-]{0,127}$`, never a caller path; every lookup goes through
`repair.confine.admit_repair` under `repair_root()`. Prepare creates a fresh
`repair-<32 hex>` directory there. Its basename differs from the manifest's
internal repair ID; the latter is returned in `identity.manifest_repair_id`.

| Tool | Required input | Result |
|---|---|---|
| `prepare_repair` | `{project, character, anim, parent_job}` | `repair_id`, state, identity hashes from the manifest, informational staged source paths, `next_steps` for owner authorization and the contained worker |
| `inspect_repair` | `{repair_id}` | Workspace state/identity, authorization as recorded plus hash binding, session lease/state, journal event counts/digests; no edit or session launch |
| `inspect_repair_diff` | `{repair_id}` | Existing `repair.compare.diff` report, coordinates capped at 2000 per frame/list with full `*_count`, palette/alpha problems and `unauthorized_changes` with counts |
| `finalize_repair` | `{repair_id}` | Immediate durable `job_id`/state, kind `finalize_repair`, sharing the heavy CPU lock; poll `job_result` for child import `artifact_id`, informational `child_path`, validator/export/Godot verdicts, manifest refs for hashes, and current human status |
| `abort_repair` | `{repair_id}` | Existing CLI abort result; active writer lease refuses abort; evidence retained |

`parent_job` accepts a durable job ID, imported artifact ID, or their confined
artifact refs. Prepare uses the repository profile and the CLI's immutable
workspace builder; it does not launch a GUI/editor session. The converter may
perform the existing contained batch conversion. Diff reads the latest batch
snapshot already produced by the worker; it never exports on behalf of MCP.

Finalize uses the same library as `sonoranlab repair finalize`, including
parent/source/authorization/checkpoint/journal checks and fresh child review.
Requests freeze input hashes and recheck them in the worker. A durable claim
refuses a second acceptance after execution starts, including uncertain outcomes;
prepare a new workspace instead of retrying that acceptance. Technical failures
retain a failed-verdict child even when job execution succeeds. Human state is
checked with `registry.check_review` on the child's import ID, never inherited
from an ancestor.

The unchanged CLI finalizer creates a fresh child at
`<REPAIR_ROOT>/<repair.name>-child-<job_id>`, which is then indexed by
`registry.import_job`. Child artifact refs use `<artifact_id>:<relative_path>`
accepted by `get_artifact`. The durable job output keeps only an exclusively
written `finalize-result.json` recording child path, import ID, hashes and verdicts.
The default sibling STATE/REPAIR_ROOT layout works with the default READ root.
Custom READ roots must admit the child; import refusal fails the job with the
registry error, without widening roots. See [the repair runbook](runbooks/repair.md).
No authorization, session, op/read, relay, editor export, Lua or upstream tool
is exposed by MCP. Editing stays with one contained worker through the broker.

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

Error codes: `invalid_params`, `not_found`, `conflict`, `not_ready`,
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
`<job_id>:<relative path>` or `<artifact_id>:<relative path>` for imports;
outputs use IDs and STATE-relative refs, with
absolute paths only in informational fields. Writes use same-directory
temporary files + fsync + os.replace + directory fsync; exclusive creates
use O_EXCL/mkdir; record read-modify-write holds an fcntl lock.
Registry record writers implement these rules.

## Registry tool schemas (DYL-114)

All six tools are version `1`, kind `sync`, and reject unknown input keys.
`project` and `character` are strings matching `^[a-z0-9][a-z0-9_-]*$`
(maximum 128 characters); paths and artifact refs are nonempty strings of at
most 4096 characters. Paths use the confinement rules above. Required inputs
are shown without `?`:

| Tool | Input object | Result object |
|---|---|---|
| `list_characters` | `{project}` | `{characters: [{id, cell, anchor_mode, animations, registered, reference_sets: [{ref_set_id, approval}], jobs: [job_id]}]}` |
| `get_character` | `{project, character}` | `{id, cell, anchor_mode, animations, registered, reference_sets, jobs}` |
| `register_character` | `{project, character, reference_dir, notes?: string}` | `{ref_set_id, approval, created: boolean}` |
| `import_job` | `{project, character, job_dir}` | `{artifact_id, job_id, created: boolean}` |
| `get_artifact` | `{ref}` | Native/import file ref: `{ref, size, sha256, media_type, dimensions?: [width,height]}`; bare native job ID: `{job_id, file_count, size, media_type}`; imported ID retains `{artifact_id, job_id, file_count, size, sha256, media_type}` |
| `check_review` | `{job}` (native job ID or imported ID) | `{production_usable: boolean, review_state: none\|pass\|fail\|invalid, problems: [{code,field,message}]}` |

`register_character` and `import_job` are mutating; the other four are
read-only. The exact JSON Schemas are advertised by `api --list` and MCP
`list_tools` from `api/tools/registry_tools.py`. `notes` is capped at 65536
characters. `job` accepts `job-YYYYMMDDThhmmssZ-xxxxxxxx` or an `import-` ID.
`ref` accepts a native job ID, an imported ID, or their file refs. Native jobs
need no explicit import. No discovery scan of arbitrary READ directories is performed.
Imported IDs are `import-` + 24 SHA256 hex of `project/character/absolute-job-path`.
Imports are idempotent while the recorded metadata/export hashes match;
changed evidence is a conflict. Imported-ID metadata describes the indexed
hash set, with total current file size. No text preview or pixel data is returned.

A full reference set contains `files` keyed by profile direction, each with
`sha256` and informational `source_path`, plus `registered_at`, `notes`,
`verification: {hashes, problems}`, and `approval: {state, evidence, problems}`.
Approval state is `approved`, `unapproved`, or `invalid`, recomputed from
current hashes. Evidence contains the STATE-relative approval record path,
its SHA256 and its record. Full jobs include import provenance and hashes,
`current_hashes`, separate `technical` and `human` states, review problems,
and `lineage: {parent_job, repair_children}`. Repair children are the imported
children linked by Phase 3.5 `repair.parent_job` / `upstream.parent_job`.
Retired profile rows remain readable using their registered spec snapshot.

Registration copies exactly the eight `still_<direction>.png` files,
checks alpha and cell size with animate's preflight, and never changes source
files. Reference files are 0444 and their exclusively created set directory
is 0555. The sorted direction/hash pairs use compact JSON encoding to derive
the content ID; collisions and existing unindexed set directories are refused.
Records use schema `sonoranlab.character/1`, one stable `character.lock`,
and atomic JSON replacement. Corrupt records fail with a named file, never reset.

Owner approval is **CLI only**:

```sh
sonoranlab registry approve-references --project P --character C --ref-set refs-xxxxxxxxxxxx --reviewer NAME --evidence TEXT
```

**An agent must never run this command.** It is not an MCP tool. It creates
`STATE/registry/P/C/approvals/<UTC timestamp>.json` using O_EXCL, with schema
`sonoranlab.ref-approval/1`, project, character, ref_set_id, direction/hash
bindings, reviewer, evidence and approved_at. Approval records are append-only;
registration never infers approval from prior-phase files or profile comments.

Durable job records and idempotency claims are implemented by `sonoranlab/jobs/`.
See the [jobs runbook](runbooks/jobs.md) for locking, crash recovery, launchers,
executor registration and retention. Production job kinds remain later-ticket work.

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

### Durable job behavior

Submission validates before creating records or output directories. Same
idempotency key/fingerprint returns the original job; changed inputs conflict.
A duplicate active fingerprint without a key conflicts. Detached single-attempt
workers share a GPU/heavy-CPU scheduler lock; actual generation still uses the
existing run-window guard per request. Status/list reconcile lease loss locally:
no unresolved prompt → failed(worker_lost), unresolved IDs → blocked with IDs.
No read launches, probes services, resubmits, or infers success.

Cancellation confirms only this job's durable submission IDs. Unknown outcomes
remain blocked(cancel_unconfirmed). Terminal cancellation is an idempotent no-op;
retry creates a new linked attempt and refuses unresolved blocked parents or
changed frozen inputs. `job_result` before a terminal outcome returns error code `not_ready`.
`job_result` returns `{job_id, state, result, error, artifacts, artifact_count,
artifacts_truncated}`. `result` is a compact executor summary; `artifacts` contains
`{ref}` objects, with no redundant filesystem path. The default page contains at
most 200 refs; optional `artifacts_offset` (default 0) and `artifacts_limit`
(1–500, default 200) page through a stable priority order: report.json, contact.png,
manifest.json, export/**, animate.json, sweep.json, then other files; ties use the
relative path. `artifact_count` counts all files and `artifacts_truncated` is true
when further refs remain after the selected page.
`job_status` omits the full executor `result` and returns the same compact
`result_summary` (null before a result exists). Summaries retain verdicts, counts,
timings, compact generation attempts and detail refs; full argv, model/input/output
hash inventories and detailed manifests remain on disk. Paths within the native
output become `job_id:relative/path` refs in summaries. Verdict failures, warnings
and errors are limited to 20 entries with `*_count` and `*_truncated` fields.
Default animate status/result envelopes target less than 64 KiB; explicit larger
artifact pages may exceed that.
Terminal records persist the executor's measured stage; `job_status.stage` follows
the measured progress stage when available. Animate's own non-null stage timings
populate `timing.per-stage`, excluding its `total` field.
Initial registry discovery performs startup reconciliation; server construction
itself does not import registry modules. See the [jobs runbook](runbooks/jobs.md).

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


## Generation tools (DYL-116)

Both tools submit GPU jobs immediately through the durable manager. Their schemas
reject extra fields, caller tracks, graphs, workflows, prompts, profile overrides
and output paths. Exact schemas are advertised by `api --list` and MCP discovery.

| Tool | Required parameters | Optional parameters and defaults |
|---|---|---|
| `animate` | project, character, anim, ref_set_id, seeds | windows=default (`default` or `steady`), godot_check=true, need_gib=40, allow_pause=[], idempotency_key |
| `pose_frame` | project, character, ref_set_id, direction, template, seed | allow_pause=[], idempotency_key |

Seeds are distinct integers in 0..2147483647; animate admits at most the profile's
max_seeds_per_attempt (4). Animation must belong to the character and default to
Track E. Animation attempts that submitted a prompt or are still active, including every
explicit linked retry, count against max_attempts_per_animation (3); the check runs
atomically under the manager's submission lock (executor `admit` hook). Same-key
replays return the existing job without consuming an attempt. Keys are nonempty, at most 200
characters. Pause units must be in the owner's `SONORANLAB_ALLOW_PAUSE_UNITS`
os.pathsep list; an unset/empty list authorizes none.

Reference approval is recomputed and all eight registered still hashes verified
at submission and again before generation. Unapproved/invalid references return
`refused` with `owner must run sonoranlab registry approve-references`; no job or
output directory is created. Frozen input hashes bind stills, profile and workflow
source; pose_frame also binds the template PNG. Explicit retries revalidate them.

`animate` calls the existing animate library, creates its required new `animate/`
subdirectory, and uses its guarded generation runner. Track E requires at least
40 GiB. The container is `SONORANLAB_COMFY_CONTAINER` or `sonoranlab-comfy`.
Pipeline PASS and validator FAIL both mean job succeeded; technical.validator
reports pass/fail separately. Only PASS includes export artifact references.
No candidates/processing errors fail the job; guard exit 3/4 becomes retryable
budget_refused/guard_busy. The result summary includes an animate.json ref, stage
timings, planned attempt seed/direction/exits, counts and verdict failures. Detailed
per-attempt model/workflow/input/output hashes and memory measurements remain in
the referenced result.json files. Five generated directions, mirrored
animation rows and eight independent stills retain animate's existing semantics.

`pose_frame` is **single pose / posed first frame; no automatic C->E fallback**.
Template IDs are `anim/direction/pose_NNN` (for example `run/east/pose_000`), with
no extension or caller paths. Only regular PNGs under repository templates/poses
are admitted, with matching generated direction and a profile animation. The
request uses Track C's documented POSE_PROMPTS, the registered direction still
and the template control image. The child runs `generate --request ... --exec ...`
through guarded_argv with **60 GiB**, retaining the runwindow lease and worker
TERM propagation. Result summaries include RawResult status/timing/error and paged artifact refs;
adapter failures retain their structured code/stage/message/retryable fields.

Absent-container errors become failed(unavailable), with hint
`start it: bash env/comfy.sh up --offline`; no service is started and no fallback
runs. Cancel uses DYL-115 prompt ownership and TERM handling.
The manager's cancel prober honours the same SONORANLAB_COMFY_CONTAINER.

Generation's stage() reads disk only: generation result count versus request
count, sweep presence for select_and_process, and export presence for
validate_export. For completed animations, `job_status.progress.technical` and
`job_status.progress.human` recompute technical status and review from the contained
final job using the registry check_review logic. `job_status` promotes these
measured verdicts to its top-level `technical`/`human` fields. Results initially report
production_usable=false and awaiting owner review; saved results never constitute
fresh review evidence.

`check_review` accepts native job IDs directly and always reads current evidence:
animate resolves the contained final job through the same verdict pointer as
generation status; process/export jobs use their own output; finalize_repair uses
the imported child and its import snapshot. `get_artifact` accepts native
`job_id:relative/path` refs rooted at the durable record's output_dir and bare IDs
for file count/total size. Imported IDs retain their existing behavior. Traversal,
escaping symlinks and special files remain refused; metadata never includes bodies.

## Deterministic tools (DYL-117)

All six take `project`, `character`, `anim` from the repository profile; profile
paths and output targets are not caller options. Inputs accept confined paths,
`job_id:relative/path` refs, or imported artifact IDs. Export/check `source` must
be a job/import/artifact ref, never a bare path. Every directory and descendant
read is confined; traversal, escaping symlinks, game checkout reads and special
files are refused. Only the profile loader reads its recorded palette.

| Tool | Additional required inputs | Optional inputs | Result |
|---|---|---|---|
| `process` | `raw`, `mode:"raw"` | `sheet`, `start_col`, `stills`, `anchors`, `godot_check`, `idempotency_key` | Immediate durable `job_id`; pipeline result contains report/contact/export refs and technical verdicts |
| `validate` | `frames_dir`, `final_resolution:true` | `stills`, `registration` | `report`, `ref` for the new report.json |
| `contact_sheet` | `frames_dir`, `report` | `zoom` (1–8; omitted selects largest fitting zoom) | PNG `ref`, `dimensions`, chosen `zoom` |
| `compare` | `before`, `after`, `final_resolution:true` | `zoom` (1–8, default 8) | Per-frame diffs, both reports, verdict change, three contact refs, compare.json `ref` |
| `export_godot` | `source` (job or report ref) | `idempotency_key` | Immediate durable `job_id`; result contains new export refs |
| `godot_check` | `source` (export ref) | `idempotency_key` | Immediate durable `job_id`; result contains engine verdict, every attempt, record `ref` |

Sync outputs are exclusively created at STATE/outputs/tool/op_id/; op_id uses
the job-id shape and refs use `op_id:relative/path`. Job outputs use the manager's
exclusive artifact directory. Inputs and prior jobs remain unchanged. Tools never
write review.json. A completed validation returns the report even on technical
FAIL; callers inspect report.pass independently of successful execution.

Validation calls the shared CLI report builder directly on final-resolution
pixels, with identical still/registration checks and no pixelize, palette or
registration pass. An inputs.json record binds its report hash and admitted
input locations for subsequent export. Process calls pipeline.run with the CLI
arguments and uses the DYL-115 heavy-CPU/GPU scheduler lock.

Contact rendering uses contact.contact_sheet. All contacts, including compare's
combined PNG, are refused before allocation above 16384 px per edge or 64 MiB
decoded size; encoded files also have a 64 MiB cap. Comparison requires matching
frame names and canvases. Coordinates are [x,y], ordered by row then column,
limited to 2000 per frame with total count and truncated flag; fully hidden RGB
is ignored by the existing repair comparator. Alpha and palette/off-palette
changes, bbox edges and lowest-row foot-anchor deltas use existing helpers.

Export admission shares the standalone CLI's profile hash, character, animation,
clip timing, mirror, still and decoded-pixel digest checks, and checks existing
manifest/import hashes. A bare pass:true cannot authorize export. Frozen jobs
recheck hashes before execution; the profile game checkout is always forbidden
as a destination. All eight supplied independent stills survive column zero.
Godot uses pipeline.check_in_godot in its disposable fixture copy and retains
run_godot's signature-specific singleton retries and every attempt. Missing
Godot succeeds with technical.godot=unavailable (no pass); import/content failure
is fail. Execution success never grants human approval.

## `smoke_render` (DYL-131)

Typed async job, same manager, heavy-CPU lock and technical/human split as `godot_check`; the CLI is
`sonoranlab smoke-render` (docs/09 "Smoke render"). Input `{project, character, candidate, siblings, idempotency_key?}`;
the schema is closed (no scene, script, shader, output or path field). `candidate` and every `siblings` entry must be a
job, import or artifact ref that resolves through the registry/STATE (`_reference`): a bare path, an unknown id or a
game-checkout path is refused. Each ref resolves to an export folder holding one `<id>.sonoranlab.json` sidecar and its
PNG (hash-checked); at most 3 distinct siblings, none equal to the candidate. Sibling game sheets are imported through
`import_job` after `python -m sonoranlab.smoke stage` (see docs/09). The owner's private `PixelStandee.gdshader`, if
present at `STATE/smoke/PixelStandee.gdshader` (regular file, never a symlink), is hashed into the frozen inputs and used;
otherwise the result says `border: not_rendered`. Execution `succeeded` with `technical.godot` = `pass`, `fail` or
`unavailable` (no Godot or compositor: never a pass); `agent_recommendation` and `owner_style_acceptance` are always
empty strings, the owner decides style. Output: `<job>/smoke/{smoke.json,render/*.png,project/,engine.log}`; `ref` names
`smoke/smoke.json`. The job is stored under the fixed clip slot `smoke` (`anim` is injected by validation).

## Registering in Claude Code

Use the repository's project-scoped `.mcp.json`; see the
[MCP operations runbook](runbooks/mcp.md) for approval, preflight and recovery.
