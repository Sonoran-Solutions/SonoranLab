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
| `get_character` | read-only / sync | Implemented: references, current review and merged native + imported job history (DYL-126) |
| `list_jobs` | read-only / sync | Implemented: bounded/filterable durable summaries (limit ≤200), offline lease reconciliation |
| `job_status` | read-only / sync | Implemented: execution, measured file progress/timing, compact `result_summary` and separate technical/human status |
| `job_result` | read-only / sync | Implemented: terminal result summary/error and paged artifact references |
| `get_artifact` | read-only / sync | Implemented: bounded metadata only, no image pixels or text bodies |
| `check_review` | read-only / sync | Implemented: recompute artifact-bound human review from disk |
| `import_job` | mutating / sync | Implemented: index existing jobs and hashes without modifying them |
| `register_character` | mutating / sync | Implemented: immutable still copies, initially unapproved |
| `check_class_request` | read-only / sync | Implemented (DYL-128): class request + registered stills admission |
| `campaign_*` (7) | mixed / sync, `campaign_submit` job | Implemented (DYL-129): resumable class campaign ledger, see below |
| `validate` | mutating / sync | Implemented (DYL-117): validator report in a new output directory |
| `contact_sheet` | mutating / sync | Implemented (DYL-117): new review image output directory |
| `compare` | mutating / sync | Implemented (DYL-117): new side-by-side contact and report output directory |
| `animate` | mutating / job | Implemented (DYL-116): Track E profile animation |
| `pose_frame` | mutating / job | Implemented (DYL-116): Track C single pose / posed first frame only |
| `process` | mutating / job | Implemented (DYL-117): deterministic pixel chain |
| `export_godot` | mutating / job | Implemented (DYL-117): guarded export into STATE, never the game checkout |
| `godot_check` | mutating / job | Implemented (DYL-117): headless import/playback check in isolated output |
| `smoke_render` | mutating / job | Implemented (DYL-131): isolated side-by-side Godot render of a candidate beside up to 3 approved siblings |
| `view_artifact` | read-only / sync | Implemented (DYL-130): one allowlisted PNG in STATE as bounded MCP image content, nearest only |
| `compose_sheet` | mutating / job | Implemented (DYL-130): selected clip jobs + approved stills → one re-validated class sheet |
| `build_delivery` | mutating / job | Implemented (DYL-130): staged delivery bundle from an owner-reviewed composed sheet; no apply/install tool |
| `delivery_status` | read-only / sync | Implemented (DYL-130): bundle states, integrity rechecked |
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
| `list_characters` | `{project}` | `{characters: [{id, cell, anchor_mode, animations, registered, reference_sets: [{ref_set_id, approval}], jobs: [imported job_id], job_count}]}` |
| `get_character` | `{project, character, jobs_offset?: int>=0 (0), jobs_limit?: 1-200 (50)}` | `{id, cell, anchor_mode, animations, registered, reference_sets, jobs, job_count, jobs_offset, jobs_truncated, unattributed_invalid_jobs}` |
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
and `lineage: {parent_job, repair_children}`. Repair children are the
children linked by Phase 3.5 `repair.parent_job` / `upstream.parent_job`.
Retired profile rows remain readable using their registered spec snapshot.

**Merged history (DYL-126).** `get_character.jobs` lists every logical job of
the exact (project, character): durable native jobs (any kind, any execution
state) and registry imports. An import is the *same* job as a native one when
its resolved `job_dir` equals or lies inside the native job's validated output
root (`store.output`: no symlinks or escapes), or when it is the child a valid
`finalize_repair` job imported (`result.artifact_id`, itself a hash of the
exact path). Import-only entries merge only on the exact same resolved
directory, never by containment, and invalid native jobs claim nothing; names
are never compared. Each entry has `id` (native
job ID, else the import's `artifact_id`), `aliases` (the other accepted IDs),
`source` (`native`, `import`, `native+import`), `kind` (`import` for import-only),
`anim`, `created_at`, execution `state`/`stage`/`error` (null for import-only),
`attempt: {number, parent_job_id, retries}` from `retry_job`, separate
`technical` and `human` verdicts, `review_problems`, `imports` (per-import
summaries), `lineage: {parent_job, parent_id, repair_children, child_ids}`
(`parent_id`/`child_ids` are canonical IDs; `repair_children` keeps the DYL-114
child `job_id` form; children are those on the same page, while every child
always carries its own `parent_id`) and `status: ok|invalid` with `problems`. Import-backed
entries also keep the DYL-114 import fields (`artifact_id`, `job_dir`,
`review_job`, `hashes`, `current_hashes`, `provenance`, `imported_at`).
`human` is recomputed on every read through the `check_review` logic for every
bound review (native and each import); the most conservative state wins and
`production_usable` requires all of them. A corrupt record, journal mismatch,
malformed recorded verdict, import drift, out-of-root directory or any other
per-entry error makes only that entry `invalid`;
corrupt native records whose project/character cannot be read are counted in
`unattributed_invalid_jobs`. Entries are ordered by `created_at` then `id` and
paged with `jobs_offset`/`jobs_limit` (default 50, maximum 200); `job_count` is
the total and `jobs_truncated` says more pages follow. Ordering and dedup use
only record metadata and path resolution; only the returned page pays for
manifest/lineage reads, reviews and native `job_status`-style reconciliation (the same lease
check `list_jobs` performs); nothing is submitted, retried, launched or
approved. `list_characters` stays lightweight: imported IDs plus `job_count`.

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
source; pose_frame also binds the template PNG, template.yaml and the rendered control image. Explicit retries revalidate them.

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
and a control image rendered for the character's cell from the template's joints
(`sonoranlab/posetemplate.py`, cell x 4 px, written to the job's `control.png`; the shipped PNG is cell-136 only). The child runs `generate --request ... --exec ...`
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

## Class-request admission (DYL-128)

`check_class_request` (read-only / sync) is the MCP form of `sonoranlab classreq check` (ADR-0011 entrance).

| Input | Result |
|---|---|
| `{project, request: object (sonoranlab.class_request/1, parsed), stills_ref?: "refs-<12 hex>"}` | `{schema: sonoranlab.class_request_check/1, status, findings: [{code, blocks, detail, direction?}], request_sha256, character, cell, foot_y, profile_row, stills: {source, files: {direction: sha256\|null}, measurements}, registry, proposed_row}` |

`status` is `ready`, `needs_profile_row`, `needs_reference_preparation` or `invalid` (precedence
invalid > needs_reference_preparation > needs_profile_row > ready). Stills come only from a registered reference
set of the request's character (`stills_ref`); `references.stills_dir` inside the request is never followed by the
tool. No contact image is written (the CLI's `--contact` renders one). The request is untrusted data: unknown
keys, over-long text (4096 characters per field, 64 list items) and control/bidi characters make it `invalid`
with `request_invalid` findings; nothing in it is interpreted. `request_sha256` is the canonical-JSON hash, equal
for the CLI file and the MCP object. Finding codes and the CLI usage are in
[the new-class references runbook](runbooks/new-class-references.md). `refprep` and `classreq propose-row` are
CLI-only owner preparation steps, not tools.

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


## Class campaigns (DYL-129)

A resumable ledger over the durable job manager (ADR-0011 states, budgets and escalation); see
[the campaigns runbook](runbooks/campaigns.md). `campaign_submit` is a campaign's only generation path. Every
tool has a `sonoranlab campaign <verb>` CLI form printing the same envelope.

| Tool | Access / kind | Input | Result |
|---|---|---|---|
| `campaign_create` | mutating / sync | `{project, request: object, reference_id: refs-<12 hex>, revise?: campaign_id, max_seeds_per_clip?: 1-1000, budget_decision_ref?}` | `{created, campaign_id, revision, state, frozen_sha256, budget}`; not admitted: `{created: false, state, findings, request_sha256}` and nothing written |
| `campaign_status` | read-only / sync | `{campaign_id}` | `{state, gate, clips: {clip: {state, reason, budget: {base, granted, consumed, remaining, not_consumed}, attempt_limit, seeds_used, candidates, selected, attempts}}, next_actions, problems, ledger}` |
| `campaign_submit` | mutating / job | `{campaign_id, clip, seed?}` | `{job_id, job_state, seed, idempotency_key, budget}` |
| `campaign_decide` | mutating / sync | `{campaign_id, job_id, decision: selected\|rejected, reason}` | `{decision, clip_state}`; selected requires technical PASS; a recommendation, not approval |
| `campaign_escalate` | mutating / sync | `{campaign_id, clip, reason}` | best two candidates (pass, then fewest validator failures/warnings) with diagnostics |
| `campaign_report` | read-only / sync | `{campaign_id}` | per-clip attempts, outcome classes, timings, memory, explicit costs, interventions, null-aware totals |
| `campaign_note` | mutating / sync | `{campaign_id, text?, cost?: {source, units, amount}}` | appended event sequence numbers |

`campaign_id` is `camp-<UTC yyyymmddThhmmssZ>-<8 hex>`; records live under
`STATE/campaigns/<project>/<character>/<campaign_id>/` (`frozen-r<N>.json`, hash-chained `events.jsonl`,
`head.json`, `campaign.lock`). State is derived on every read from events, the job store (`job_status`
reconciliation) and registry approval/review checks; it is never stored. Gate states: `NEEDS_OWNER_INPUT`,
`NEEDS_REFERENCE_PREPARATION`, `AWAITING_REFERENCE_APPROVAL`, `AWAITING_PROFILE_ROW`, then `READY`. Clip states:
`BLOCKED` > `GENERATING` > `ARTIFACT_APPROVED` > `AWAITING_OWNER_REVIEW` > `TECHNICAL_PASS` > `HOLD_BUDGET` >
`READY`; the campaign takes the gate state, else BLOCKED/GENERATING if any attempt is, else the least advanced
planned clip; all clips approved plus an owner delivery record gives `STAGED_AND_ENGINE_VERIFIED`,
`READY_TO_INSTALL` or `INSTALLED`. Submission is refused while any attempt is in flight (`conflict`, GENERATING) or
blocked (`refused`, BLOCKED), when the clip's budget or the profile attempt limit is spent (`refused`, HOLD_BUDGET),
or when a selected candidate awaits owner review. A concurrent writer gets `conflict` with message `campaign_busy`.
Submission always uses Track E `animate` with the guard's default 40 GiB; no track switch, no memory change.
Plain `animate` and `retry_job` are `refused` for a clip owned by a campaign (animate admission check). A revision
carries each clip's cap; candidates, selections, reviews and delivery count only for the current frozen inputs.
Owner events count only when the owner CLI committed them in `head.json`.

Owner decisions (`budget_grant` adds seeds, `resume` clears a non-artistic block, `install`, `delivery`) are
**CLI only**: `sonoranlab campaign owner-decision`. There is no MCP tool for them.

## `/new-class` support tools (DYL-130)

The project skill `.claude/skills/new-class/SKILL.md` drives the ADR-0011 loop with an allowlist of 23 tools; see
[the new-class runbook](runbooks/new-class.md). These four tools close its gaps.

| Tool | Access / kind | Input | Result |
|---|---|---|---|
| `view_artifact` | read-only / sync | `{ref, zoom?: 1-8 (default 1), max_edge?: 1-2048 (default 2048)}` | structured `{ref, sha256, size, dimensions, zoom, downscale, mime_type, source: {sha256, size, dimensions}, delivery}` describing the delivered PNG, plus one MCP `ImageContent` block (base64 PNG) over MCP only |
| `compose_sheet` | mutating / job | `{project, character, clips: [{name, job}] (sheet order, 1-8), reference_id, idempotency_key?}` | `job_result.result`: `{pass, exit, failures, warnings, report, technical, artifact_id, export, summary: {sheet_sha256, dimensions, clips, owner_review_dir}}` |
| `build_delivery` | mutating / job | `{project, campaign_id, composed: compose job id, target_rev: 40 hex, base_manifest, import_template, idempotency_key?}` | `job_result.result`: `{ok, ref: <job>:bundle/bundle.json, summary: {state: BUILT, files, known_missing_clips, playable_class, next}}` |
| `delivery_status` | read-only / sync | `{bundle?: build job id, character?}` | `{bundles: [{bundle, character, state, states, target_revision, known_missing_clips, playable_class}], count}`; a tampered bundle is `state: INVALID` with its problem |

- `view_artifact` refs: `<job id>:<relative>` (native jobs, compose/delivery jobs, sync-tool outputs), imported
  artifact ids, `refs-<12 hex>:still_<direction>.png`. Refused: paths (`invalid_params`), traversal, any symlink,
  game-checkout targets (`path_refused`), non-PNG content (`refused`), files outside STATE unless they are
  hash-bound files of an imported artifact, sources over 16 MiB or 64 Mpx. `zoom > 1` past `max_edge` is refused;
  a zoom-1 source past `max_edge` is downscaled only by an exact integer factor (nearest), otherwise refused with a
  `contact_sheet` hint. The delivered PNG is at most 4 MiB. The CLI twin (`sonoranlab api view_artifact`) prints the
  structured part only and writes nothing; the image travels only as MCP content.
- `compose_sheet` wraps `sonoranlab compose` (docs/runbooks/delivery.md): each `job` is a succeeded native job id
  (animate: its `sweep/final/job`) or an import id; the reference set must be owner-approved. Output:
  `<job output>/composed/`, imported into the registry on PASS (`artifact_id`, usable as `smoke_render` candidate).
  The owner reviews that directory with `sonoranlab review record`.
- `build_delivery` wraps `sonoranlab deliver build`: the composed job must be production usable (`not_ready`
  otherwise, at submit), the request is the campaign's frozen admitted request, and `base_manifest` /
  `import_template` are paths relative to `STATE/staged/` (regular files, no symlink, never the game checkout).
  Output: `<job output>/{request.json,bundle/}`. It writes nothing outside STATE.
- There is no apply, verify, rollback or install tool: snapshot apply/verify is a coordinator/owner CLI action and
  installation is owner-only (ADR-0011). `INSTALLED` is never set by a tool.

## Registering in Claude Code

Use the repository's project-scoped `.mcp.json`; see the
[MCP operations runbook](runbooks/mcp.md) for approval, preflight and recovery.
