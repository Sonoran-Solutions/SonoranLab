# ADR-0010 — Phase 4 MCP contract

**Status:** proposed (2026-10-20, DYL-113)

## Context

Phase 3.5 was adopted narrowly and optionally (ADR-0009). The old Phase 4
plan and tool document still described Track A candidates and `walk_4dir`,
which cannot ship the game-native format (ADR-0007). ADR-0004 selects E for
locomotion under an explicit Phase 1 owner exception, not a demonstrated
60-minute end-to-end pass. ADR-0008 keeps technical validation separate from
human review. This decision records the coordinator's Phase 4 contract;
it does not redesign generation or relax a prior gate.

## Decision

One operations layer (`sonoranlab/api/`) serves CLI JSON and local stdio MCP
(`sonoranlab mcp serve`, server name `sonoranlab`, SDK `mcp==2.3.0`).
Tools carry name, version, description, exact JSON input schema,
read_only, sync/job kind, handler `(params, Context)` and an optional
capability gate. Area modules export tools through `api/tools/__init__.py`;
new tickets add their module there without editing existing tools.
Handlers reuse the existing library operations used by CLI verbs.

### Reconciled inventory and operation mapping

Supported = approved contract surface; pending = implemented by later
tickets. Only the first three tools are implemented by DYL-113. Optional
tools are advertised only when `editor=available` (runtime presence check,
not launch/probe). No stubs are advertised.

| Tool | Disposition | Read-only / kind | Existing operation / CLI equivalent |
|---|---|---|---|
| `capabilities` | Supported, implemented | yes / sync | Runtime path presence (same env/default as repair.runtime), shutil.which; `api capabilities` |
| `list_projects` | Supported, implemented | yes / sync | profiles/*.yaml + profile.load_profile(check_paths=False); `api list_projects` |
| `get_profile` | Supported, implemented | yes / sync | profile.load_profile + Profile.character; profile show/check semantics without external path checks; `api get_profile` |
| `list_characters` | Supported, implemented (DYL-114) | yes / sync | Profile.characters + new STATE registry records; no prior registry CLI, `api list_characters` |
| `get_character` | Supported, implemented (DYL-114) | yes / sync | Profile.character + registry/history; `profile show` plus new record lookup |
| `list_jobs` | Implemented (DYL-115) | yes / sync | New STATE job records over existing job artifacts; `api list_jobs` |
| `job_status` | Implemented (DYL-115) | yes / sync | New job model, existing animate summaries and technical reports; `api job_status` |
| `job_result` | Implemented (DYL-115) | yes / sync | Existing manifest/report/contact artifacts, new job record lookup; `api job_result` |
| `get_artifact` | Supported, implemented (DYL-114) | yes / sync | artifacts regular-file/hash checks + contact.contact_sheet; bounded metadata, size-capped contact images only |
| `check_review` | Supported, implemented (DYL-114) | yes / sync | review.review_errors / production_usable; `review check` |
| `import_job` | Supported, implemented (DYL-114) | no / sync | registry.import_job; explicit existing-job indexing, no job modification; `api import_job` |
| `register_character` | Supported, implemented (DYL-114) | no / sync | New registry copies/hashes using artifacts patterns; initially unapproved; `api register_character` |
| `validate` | Implemented (DYL-117) | no / sync | validate.read_frames/read_stills/validate; `validate`, new output directory |
| `contact_sheet` | Implemented (DYL-117) | no / sync | contact.contact_sheet; `contact`, new output directory |
| `compare` | Implemented (DYL-117) | no / sync | Existing validate + contact primitives, new side-by-side/report orchestration; new output directory |
| `animate` | Implemented (DYL-116) | no / job | animate.animate; `animate --track E`, existing guard and selection/processing chain |
| `pose_frame` | Implemented (DYL-116) | no / job | comfy.adapters.track_c.TrackC; `generate` Track C request, single pose / posed first frame only |
| `process` | Implemented (DYL-117) | no / job | pipeline.run; `process` |
| `export_godot` | Implemented (DYL-117) | no / job | export.godot.export_godot guarded export; `export godot`, STATE output only |
| `godot_check` | Implemented (DYL-117) | no / job | pipeline.check_in_godot / run_godot; existing --godot-check, isolated job output |
| `cancel_job` | Implemented (DYL-115) | no / sync | New job cancellation plus existing ComfyClient.cancel; preserve unknown outcomes |
| `retry_job` | Implemented (DYL-115) | no / job | New explicit linked job over existing animate/process operations |
| `prepare_repair` | Optional, implemented (DYL-118) | no / sync | repair.workspace.prepare; `repair prepare`, owner authorization/contained-worker handoff |
| `inspect_repair` | Optional, implemented | yes / sync | repair.workspace.inspect; `repair inspect`, plus read-only authorization/lease/journal summaries |
| `inspect_repair_diff` | Optional, implemented | yes / sync | repair.compare.diff; `repair diff`, capped coordinates/counts and authorization masks |
| `finalize_repair` | Optional, implemented | no / job | repair.finalize.finalize; `repair finalize`, heavy CPU lock, child requires fresh review |
| `abort_repair` | Optional, implemented | no / sync | admit_repair + repair.session.abort; exact current `repair abort` lease behavior |

| Excluded operation | Disposition / reason | Existing operation if any |
|---|---|---|
| Approve references / record review | Owner CLI only; no agent approval tool | reference owner workflow; `review record` / review.record_review |
| `generate_character` | Deferred; Track A is an experimental non-default baseline, not the production entrance | benchmark Track A workflow; no supported production candidate CLI |
| `regenerate_region` | Deferred; diffusion inpaint is outside the adopted pixel-repair scope | No supported operation |
| `smoke_render` beside | Deferred; isolated Godot check covers Phase 4 engine verification | pipeline.check_in_godot is not a sibling comparison render |
| `list_pose_templates` | Deferred; no template-management need in this contract | Profile animation template metadata |
| `bake_rig` | Deferred; Track D template production is outside this surface | Existing benchmark/template bake tooling |
| Automatic C→E fallback | Excluded; no implicit track changes or new budget commitments | Explicit single-pose C and locomotion E only |
| Arbitrary graphs, shell, Lua, profile overrides | Excluded; bypasses the validated operation/policy boundary | Owner development tools only |

### Registry schemas (DYL-114)

The six registry tools' final required inputs, result records, approval
states and exact schema discovery are specified in
[docs/06 registry tool schemas](../06-tool-surface.md#registry-tool-schemas-dyl-114).
All are sync: list_characters(project), get_character(project, character),
get_artifact(ref), check_review(job) are read-only; register_character(project,
character, reference_dir, notes?) and import_job(project, character, job_dir)
mutate only the STATE registry. Both front ends use the same Tool definitions.
Owner `registry approve-references` is absent from MCP and must never be run
by an agent; append-only schema `sonoranlab.ref-approval/1` binds the current
reference hashes. Neither a successful import nor technical PASS grants approval.

### Generation schemas and outcomes (DYL-116)

The narrow generation tools are implemented over the durable executor interface;
see [generation tool schemas](../06-tool-surface.md#generation-tools-dyl-116).
`animate(project, character, anim, ref_set_id, seeds, windows?, godot_check?,
need_gib?, allow_pause?, idempotency_key?)` admits only character animations whose
track_default is E, unchanged approved references, profile autonomy limits and
E >=40 GiB. Attempt admission is atomic under the manager's submission lock. `pose_frame(project, character, ref_set_id, direction, template, seed,
allow_pause?, idempotency_key?)` resolves repository-only anim/direction/pose_NNN
IDs and runs one C request at 60 GiB through the existing guarded generate CLI,
using the adapter's documented direction prompt. No batch animation, prompt
injection or automatic C->E fallback is exposed. Pause units are restricted to
the owner-set SONORANLAB_ALLOW_PAUSE_UNITS list.

Animate pass/validator_fail -> succeeded with separate technical pass/fail;
no_candidates/error -> failed; guard 3/4 -> failed, retryable budget_refused or
guard_busy. Missing-container errors are unavailable with the offline start hint.
Pose success returns RawResult plus artifacts; failure retains adapter errors.
Hash provenance comes from adapter result files, and unrecorded memory is null.
Human approval stays separate and generation progress recomputes current review
from the contained final pipeline job. job_status promotes them to its top-level technical/human fields.

### Deterministic operations (DYL-117)

The six deterministic tools are implemented; exact inputs/results are listed in
[docs/06](../06-tool-surface.md#deterministic-tools-dyl-117). They use repository
project profiles only. Process runs the existing pipeline under the heavy lock;
validate shares the CLI's final-resolution report builder and still/registration
checks. Contact and comparison write new size-capped images; comparison reuses
repair.compare's coordinate/palette rules and validate's bbox/foot helpers.

Export shares the CLI's complete bound-report admission and rechecks frozen
input hashes. Export/check accept artifact or imported-job refs only, and every
output belongs to a new STATE directory. No game checkout writes or review.json
writes are permitted. Isolated Godot checks preserve the signature-specific
singleton retry and all attempts; missing engine is unavailable, never pass.
Completed jobs and report.pass remain separate execution/technical verdicts.

### Optional repair acceptance (DYL-118)

ADR-0009's accepted narrow/optional scope exposes exactly the five repair tools
above. Their capability gate matches `capabilities.editor`'s runtime-directory
presence check; absence removes them from discovery and direct calls return
`invalid_params` / `Unknown or disabled tool`. Imports/discovery never launch
the editor, and the disabled server does not import repair session/runtime.

Prepare accepts registered/imported parent refs and a repository profile, creates
a fresh workspace under repair_root(), and returns hashes, informational staged
paths and owner/contained-worker next steps. All subsequent calls take a strict
directory ID and use admit_repair, never a caller path. Inspect/diff read existing
files only; diffs retain full counts and cap coordinate lists at 2000/frame.
Authorization records and masks remain authoritative. No authorization, session,
op/read, relay, editor export, Lua or upstream tool is added to MCP.

The durable finalize kind freezes and rechecks inputs, then invokes the existing
CLI finalizer under the heavy CPU lock, creating a fresh child at
`<REPAIR_ROOT>/<repair.name>-child-<job_id>`. It then indexes the child through
`registry.import_job`; the durable STATE output retains only an exclusively
written `finalize-result.json` with child path, import artifact ID, hashes and
verdicts. Default sibling roots work with the default READ root; a custom READ
configuration that refuses the child import fails the job with that error and
is never widened silently. A persistent per-workspace claim
prevents a second acceptance after execution starts; uncertain attempts require
a newly prepared repair. Parent/source/authorization/session checks, validator,
guarded export, Godot check and child review rules remain the library's.
The result supplies the child `artifact_id`, informational `child_path`, and
file refs using `<artifact_id>:<relative_path>` accepted by `get_artifact`.
The child is awaiting owner review, verified through `registry.check_review`
on its import ID; an ancestor approval is never carried forward. Exact
schemas/results are in
[docs/06](../06-tool-surface.md#optional-repair-handoff-dyl-118)
and the [repair runbook](../runbooks/repair.md).

### Envelope and transport

Schema identifier: `sonoranlab.api/1`.

```json
{"schema":"sonoranlab.api/1","tool":"<name>","ok":true,"result":{}}
```

```json
{"schema":"sonoranlab.api/1","tool":"<name>","ok":false,"error":{"code":"invalid_params","message":"...","field":null,"retryable":false,"hint":null}}
```

Codes: invalid_params, not_found, conflict, not_ready, idempotency_conflict,
path_refused, unavailable, refused, budget_refused, guard_busy, internal.
Unknown exceptions become internal with exception type only, never a
traceback on stdout. Schema validation uses jsonschema 4.26.0 (already in the lock as an
MCP dependency; pinned explicitly in pyproject so the API does not rely on a transitive).

`sonoranlab api <tool> [--params FILE|-]` emits exactly the MCP envelope.
Exit 0 = ok; 1 = tool failure/refused/validation FAIL; 2 = usage/schema error
(including unknown/disabled tools). `api --list` emits registry metadata.
MCP includes JSON text plus `structuredContent=envelope` and `isError` for
failures. Every tool's exact schema and readOnlyHint, destructiveHint,
idempotentHint are advertised. Blocking handlers run via anyio.to_thread;
logs go to stderr. Startup/initialize/list/read never probe services, launch
ComfyUI/Godot/Aseprite or import torch/pixeloe. Capabilities reports server
version, API schema, tool list (read_only/kind), ComfyUI configured container
and not_probed, Godot path/null, editor available/unavailable and offline
policy. Editor presence follows SONORANLAB_EDITOR_RUNTIME or the pinned
Phase 3.5 default; later repair admission must still validate containment.

Profiles summarize canvas, per-character cells, direction
order/generate/mirror_animations, animations, validator defaults, autonomy
and export target marked read-only/protected. Palette is path + sha256
only, never colours. No external palette/game file is read for discovery.

SDK 2.3 integration notes for later tickets: low-level `Server` takes
`on_list_tools` / `on_call_tool` constructor callbacks, each async with
`(ctx, params)` arguments; this is not the 1.x decorator API. Python model
fields are `input_schema`, `output_schema`, `structured_content`, `is_error`
and snake_case annotation hints; SDK JSON aliases produce `inputSchema`,
`structuredContent`, `isError`, etc. Keep validation in the shared API so
tool errors remain envelopes. The SDK stdio transport owns protocol streams
and diverts incidental fd-1 output to stderr; do not replace its framing.

### Paths, state and IDs

The normative layout, confinement, atomic-write rules and IDs are in
[docs/06](../06-tool-surface.md#state-paths-and-ids): STATE defaults to
~/sonoranlab-work/mcp; READ roots default to ~/sonoranlab-work; profiles
remain repository-owned/read-only. Inputs resolve strictly within READ or
STATE and must be regular files; core writes stay within STATE to new outputs.
Optional repair preparation/finalization uses fresh REPAIR_ROOT directories
through the existing repair admissions, then imports finalized children.
Reject traversal, symlink escapes, devices and existing output directories.
Game checkout reads/writes/output targets are refused even inside a root;
only reading the profile-recorded palette via the library is allowed.
Relative inputs use cwd, relative outputs STATE, existing parents resolve
strictly. Path helpers do not create directories. Record writers (later
tickets) use exclusive creates, same-directory temp+fsync+replace+directory
fsync and fcntl locks for read-modify-write.

Project = profile id; character = profile character key;
ref_set_id = refs- + first 12 sha256 hex over sorted (direction, file hash)
pairs; job_id = job- + UTC yyyymmddThhmmssZ + '-' + 8 random hex.
Artifacts use job_id:relative_path, import artifact_id:relative_path and STATE-relative references;
absolute paths appear only as informational metadata. Copies are immutable
0444 files with hashes. Unknown outcomes must be reconciled, not replayed.

### Status separation

- job.state: queued | running | cancelling | cancelled | succeeded | failed | blocked.
- job.stage: prepare | generate | select_and_process | validate_export | check (measured, informational).
- technical.validator: pass | fail | null; technical.godot: pass | fail | not_run | unavailable.
- human.review: none | pass | fail | invalid; human.production_usable: boolean.

Always recompute human state from disk via review.review_errors and
review.production_usable; never cache approval. succeeded means the pipeline
ran to completion, including a technical FAIL verdict, and implies no human
approval. blocked means outcome unknown or reconciliation required (such as
worker death after submission), never automatic retry/resubmission. Retry
is an explicit new job with parent_job_id.

### Durable job implementation (DYL-115)

`sonoranlab/jobs` owns frozen requests, exclusive outputs, locked atomic records,
append-only committed transition events, idempotency claims and worker leases.
Detached workers prefer an independent user systemd service; Popen sessions are
the fallback/test launcher. GPU and heavy-CPU kinds share STATE/gpu.lock, while
each generation retains the existing run-window guard. No production executor
is registered by this infrastructure ticket.

Reads and initial registry discovery reconcile dead leases without network or
replay: no unresolved prompts means failed(worker_lost); unresolved submission
IDs mean blocked(needs_reconciliation). Corrupt records remain blocked and are
never overwritten or rerun. Startup reconciliation occurs at registry discovery,
the available hook inside DYL-115's scoped files, rather than server construction.
Cancel records intent, signals only its worker, and confirms only prompt IDs
owned by its submission files. Errors/live prompts remain blocked. Retry creates
a new linked attempt, confirms blocked parents stopped, and revalidates frozen
input hashes. Successful jobs cannot retry.

`job_result` before a terminal outcome returns `not_ready` (added to the envelope's
error codes by DYL-115). Terminal results return `{job_id, state, result, error,
artifacts, artifact_count, artifacts_truncated}`: `result` is a compact executor
summary, while artifacts are `{ref}` objects (default at most 200). Optional
`artifacts_offset` (default 0) and `artifacts_limit` (1–500, default 200) select a
page ordered by report.json, contact.png, manifest.json, export/**, animate.json,
sweep.json, then other files, with relative-path tie breaks. `artifact_count` is
the total; `artifacts_truncated` means further refs remain after the selected page.
Status omits the full executor result and provides a compact
`result_summary`, null until a result exists. Summaries retain verdicts, counts,
timings, compact generation attempts and detail refs; full argument/hash inventories
remain on disk. Output paths in summaries use native artifact refs. Verdict
failure/warning/error lists retain at most 20 entries with `*_count` and
`*_truncated` fields. Default animate envelopes target less than 64 KiB;
explicit larger pages may exceed that.

`get_artifact` accepts native `job_id:relative/path` refs under the durable job's
output_dir, returning size, sha256, media type and PNG dimensions. Bare native IDs
return job_id, file_count, total `size` and media_type. Imported-id metadata is
unchanged; no bodies are returned and confinement remains enforced. `check_review`
accepts native IDs: animate uses generation status's contained final-job resolution,
process/export use their own output, and finalize_repair uses its imported child
and import snapshot. All review results recompute from current files.

When contact_sheet zoom is omitted, choose the largest value from 8 down to 1
that fits both the 16384 px edge and 64 MiB decoded caps, and return `zoom` alongside
`ref` and `dimensions`. Explicit zoom still refuses oversized images; even zoom 1
is refused if it cannot fit. Terminal job records persist the executor-measured
stage, and status promotes the measured stage to top-level `stage`. Animate's own
non-null stage timings populate `timing.per-stage` rather than charging processing
and export to generate; its `total` is excluded.

Record-level failure codes (worker_lost, corrupt_record,
needs_reconciliation, cancel_unconfirmed, launcher_failed, executor_error) live
inside successful status/result envelopes, not the API error enum. See the
[jobs runbook](../runbooks/jobs.md) for the exact transition table, crash outcomes,
manual retention and trusted test-only SONORANLAB_JOB_EXECUTORS hook.

### Superseded gate and defaults

The old PLAN exit sequence **generate_character → animate → validate →
export_godot**, `walk_4dir`, Track A is **superseded, NOT passed**. The Phase 4
exit sequence is approved-reference registration → animate (Track E, five
generated directions + mirrored animation rows + eight independent stills)
→ inspect/validate/export/check, followed by separately bound human review.
The live Phase 4 exit gate remains open until demonstrated end to end.

Track E leads locomotion; Track C is single pose / posed first frame only;
Track A is not default. No automatic C→E fallback. Optional Aseprite pixel
repair (ADR-0009) is not diffusion inpainting. GitHub issue #40 (Qwen Image
2.1 spike) changes no defaults. Guard/budgets reuse env/runwindow.py and
comfy/guard.py: E ≥40 GiB, C ≥60 GiB, plus 8 GiB guard margin; never lower
budgets, add --gtt-credit or implement a new memory controller.

## Consequences

CLI and MCP have one validation/error/result boundary. DYL-113 supplies
three read tools and transport/confinement tests; later tickets implement
registry records, jobs, deterministic operations and optional repair.
No pixel/validation logic is duplicated. The skeleton passes without GPU,
editor or engine launches; this does not establish the live Phase 4 gate.
