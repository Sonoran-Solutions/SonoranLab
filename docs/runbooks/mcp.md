# SonoranLab MCP operations

The local stdio server exposes the [tool surface](../06-tool-surface.md) to
Claude Code. Execution, technical validation and owner review are separate:
`succeeded` means execution finished, even when the validator failed. Only a
current, artifact-bound owner review can make a job `production_usable`.

## Install and register at project scope

From this checkout, use the already provisioned Python environment and cache:

```sh
uv sync --frozen --offline
uv run --frozen --offline sonoranlab api capabilities
claude mcp list
claude mcp get sonoranlab
```

The committed root `.mcp.json` registers one server, `sonoranlab`, using:

```sh
uv run --project . --frozen --offline sonoranlab mcp serve
```

Launch Claude Code from the repository root. Approve this **project** server
when Claude Code asks, then use `/mcp` to inspect its status/tools. Discovery
and `capabilities` must succeed before submitting work. No `claude mcp add`
command or global/user configuration edit is required. If it was previously
rejected, revisit the project's MCP approval choices in Claude Code.

Before enabling it, inspect `claude mcp list` / `claude mcp get sonoranlab` for
an existing registration from another scope or name. Resolve duplicates with
the owner; do not automatically remove or overwrite their configuration.
Do not edit `~/.claude.json`. Project settings do not authorize reference or
human-review approval: those remain owner CLI actions.

`SONORANLAB_STATE` defaults to `~/sonoranlab-work/mcp`;
`SONORANLAB_READ_ROOTS` defaults to `~/sonoranlab-work` (path-list separator
is `:` on Linux). Set these in the launching environment if necessary, then
restart the project server. Outputs stay in STATE; inputs must be confined
to READ roots or STATE. The profile's game checkout remains protected even
inside a read root. Only the profile loader may read its recorded palette.
Never set test hooks such as `SONORANLAB_JOB_EXECUTORS` in normal operation.

## Startup preflight

Initialize/list/read tools do not start or probe services. ComfyUI
`not_probed` means exactly that; editor `available` only means the runtime
directory exists, not that its sandbox is healthy. Godot's null path means
it is unavailable; this can never count as a technical PASS. Repair tools
are optional and require the editor capability and their own admission.

Before generation, inspect other GPU tenants and the ComfyUI queue/history.
Close conflicting workloads or obtain owner approval for specific pause
units. Do not interrupt unrelated prompts or lower the memory gates.

```sh
bash env/comfy.sh up --offline
python3 env/runwindow.py --need-gib 40 --dry-run
```

Track E requires at least 40 GiB, Track C at least 60 GiB, each plus the
existing guard's 8 GiB margin. A dry-run may refuse while a prompt is active
or its state is unknown. The guard is authoritative; do not add `--gtt-credit`
or bypass it. Pause units must be explicitly owner-authorized in
`SONORANLAB_ALLOW_PAUSE_UNITS` and requested through `allow_pause`.
See [durable jobs](jobs.md) for scheduler/lease details.

A hosted agent needs network access to its model provider and may disclose
requested images/contact sheets under the owner's provider approval. Initial
installation, model weights and editor/runtime provisioning may also require
network, as separate owner setup. Once cached/provisioned, the stdio server,
read tools, deterministic processing and local ComfyUI generation run offline.
`--offline` forbids downloads; a missing cache/model is a setup error, never a
reason to fall back to a cloud generator. The synthetic integration suite
uses no ComfyUI, engine, editor, container or GPU and denies IP connections.

## Inspect jobs and artifacts

Use MCP `list_jobs` (optional project/state, limit up to 200), `job_status`
and `job_result`. The equivalent JSON CLI is useful without an MCP client:

```sh
printf '%s\n' '{"limit":50}' | uv run --frozen --offline sonoranlab api list_jobs --params -
printf '%s\n' '{"job_id":"JOB_ID"}' | uv run --frozen --offline sonoranlab api job_status --params -
printf '%s\n' '{"job_id":"JOB_ID"}' | uv run --frozen --offline sonoranlab api job_result --params -
```

Replace `JOB_ID` with the returned ID. Poll status while queued/running;
`job_result` returns `not_ready` until terminal (blocked is not terminal).
Inspect execution state, technical.validator, technical.godot and human
fields separately. `get_artifact` returns bounded metadata using
`job_id:relative/path`; contact sheets use the dedicated tool. `check_review`
recomputes current imported-artifact review, including hash drift. Never
interpret a saved result's approval flag as fresh evidence.

`register_character` copies all eight correctly sized stills as **unapproved**.
Only the owner may inspect and approve them with `registry approve-references`.
Agents must not approve references or run `review record` for production art.
Changing stills, reports or exports invalidates their bindings; preserve the
old evidence and register/submit/review fresh artifacts explicitly.

## Cancel, recover and reconcile

Use `cancel_job({"job_id":"…"})`, then poll status until confirmed cancelled
or blocked. Cancellation records intent, signals only that worker, and
cancels only prompt IDs recorded by that job. Repeating cancellation is safe.
Never use a queue-wide clear or global interrupt to cancel a SonoranLab job.

Client disconnect and MCP server restart do not cancel detached workers.
Reconnect and inspect the original job ID. A live lease stays active. A
lost worker without unresolved submissions becomes `failed(worker_lost)`;
a lost worker with unresolved prompt IDs becomes
`blocked(needs_reconciliation)`. Neither outcome triggers a resubmission.

For an uncertain submission or lost reply:

1. Preserve `STATE/jobs/JOB_ID/` and its artifact directory. Inspect
   `job.json`, `request.json`, `events.jsonl`, `worker.log`, and every
   `submission.json` under the recorded `output_dir`.
2. Match each durable `prompt_id` to ComfyUI's queue and history in the
   configured container (`SONORANLAB_COMFY_CONTAINER`, default
   `sonoranlab-comfy`). A missing result is not proof the prompt was rejected.
   Read-only inspection, if that container is already running:

   ```sh
   podman exec sonoranlab-comfy curl -sf http://127.0.0.1:8188/queue
   podman exec sonoranlab-comfy curl -sf http://127.0.0.1:8188/history/PROMPT_ID
   ```

3. Reconcile via explicit `cancel_job` on the original job. If the service is
   unreachable or still reports it live, the job remains blocked
   (`cancel_unconfirmed`). Restore observability and repeat scoped cancel;
   do not edit the job record, delete submission evidence or invent a result.
4. Once all owned prompts are confirmed stopped, explicit
   `retry_job({"job_id":"…","idempotency_key":"new-attempt-key"})` creates a
   **new** linked job from unchanged frozen inputs. Changed inputs require a
   fresh submission. Successful jobs cannot retry. Inspect partial artifacts
   before spending another attempt; autonomy limits still apply.

Never resubmit an uncertain prompt, including with a new idempotency key.
Same key/same request reuses the original job; same key/different request is
an error. An unkeyed duplicate active request conflicts. A corrupt job stays
blocked: archive its evidence and investigate rather than resetting it.
If a run-window left approved services paused, verify the queue has drained
before the owner uses `python3 env/runwindow.py --restore`.

## Troubleshooting

Tool errors are envelopes with code/message/field/retryable/hint. Worker
failure codes live inside an otherwise successful status/result envelope.

| Code / symptom | Action |
|---|---|
| `invalid_params` | Inspect the advertised schema; remove extra fields and fix types/IDs. No arbitrary paths, prompts or graphs. |
| `not_found` | Check project/job/artifact ID and provisioned input existence. Do not reconstruct lost evidence. |
| `path_refused` | Use regular files inside READ/STATE; remove traversal/escaping symlinks; never target the game checkout. |
| `refused` | Follow the policy hint: unchanged owner-approved refs, passing bound report, valid review or remaining attempt budget. |
| `conflict` | Inspect the active duplicate, changed frozen input or existing output; preserve existing data. |
| `idempotency_conflict` | The key names different inputs; inspect the original job before choosing an explicit new request. |
| `not_ready` | Poll `job_status`; blocked jobs require reconciliation, not more polling alone. |
| `unavailable` | Provision the optional dependency or start local ComfyUI with `bash env/comfy.sh up --offline`; no cloud fallback. |
| `budget_refused` | Free memory or obtain specific owner-approved pauses; retain E/C minimums and margin. |
| `guard_busy` | Inspect the existing run-window/queue and wait for it to finish; never remove a live lock. |
| `worker_lost` / `launcher_failed` / `executor_error` | Inspect worker logs and frozen inputs; explicit retry only after stop confirmation. |
| `needs_reconciliation` / `cancel_unconfirmed` | Compare durable prompt IDs with queue/history, then repeat scoped `cancel_job`. |
| `corrupt_record` | Preserve record/journal/artifacts and investigate; never reset or auto-retry. |
| `internal` | Preserve stderr and job evidence; report the tool and exception type without private art. |
| Missing tools / duplicate server | Check project approval, scope, cwd, exact `.mcp.json` command and `capabilities`; restart after changing the environment. |

## Logs and safe shutdown

MCP stdout is protocol only. Server logs go to stderr, available in the
client's MCP diagnostics. Detached workers write
`STATE/jobs/JOB_ID/worker.log` (bounded to 1 MiB), while `events.jsonl` records
committed transitions and `request.json` preserves inputs. Systemd launchers
also expose the `sonoranlab-job-JOB_ID` user unit journal; Popen workers are
independent sessions. Do not redirect server logs onto stdout.

Before shutdown, confirm there are no queued/running/cancelling workers and
reconcile every blocked job's owned prompts. Verify ComfyUI's queue is idle,
then disconnect/disable the project MCP server and run:

```sh
bash env/comfy.sh down
```

Do not force container shutdown to bypass an active guard or unknown queue.

## Rollback and owner services

Disable the project server, ensure workers/prompts have stopped as above,
then remove only the `sonoranlab` entry from repository `.mcp.json` (delete
the file if it has no other entries). This does not remove state or cancel
workers. Resolve the actual STATE path, archive it with restricted permissions
outside that directory, verify the archive, then delete that specific STATE
directory if the owner wants a clean rollback. Keep approvals, frozen requests,
logs and artifacts in the archive; deletion loses idempotency history. Do not
delete a shared read root, game checkout or ComfyUI model cache.

The owner's user-scope **diivi aseprite-mcp** and **PixelLab MCP** servers are
separate owner configuration decisions. SonoranLab does not use, register,
modify or remove them. Its optional contained pixel repair broker is governed
by ADR-0009; it does not grant arbitrary editor operations or cloud generation.
