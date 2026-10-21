# Durable jobs

Submit through `sonoranlab.jobs.submit(kind, params, idempotency_key=None)`.
Validation and input hashing happen before any record/output creation. The
submitter reserves exclusive directories, freezes the request, launches a detached
single-attempt worker, and returns `{job_id, state}`. Production operation tickets
register `sonoranlab.jobs.EXECUTORS`; this infrastructure registers no production
kinds. No dependencies or memory controller are added.

MCP and the JSON CLI expose the same tools/envelope:

```sh
sonoranlab api list_jobs --params -   # {"project":"twos-are-up","limit":50}
sonoranlab api job_status --params -  # {"job_id":"job-20261020T120000Z-01234567"}
sonoranlab api job_result --params -  # same job_id
sonoranlab api cancel_job --params -  # same job_id
sonoranlab api retry_job --params -   # job_id, optional idempotency_key
```

Listing filters by project/state, newest IDs first, with limit 1–200 (default 50).
Status returns measured file progress and timing, technical placeholders and human
review recomputed from disk via the existing review library. A terminal result
includes result/error and partial artifact references. Nonterminal/blocked results
return error code `not_ready` (retryable): poll `job_status`.

## Lifecycle

Only these transitions are legal (`sonoranlab/jobs/store.py:TRANSITIONS`):

| From | Allowed destinations | Meaning |
|---|---|---|
| queued | running, cancelling, cancelled, failed | Acquired scheduler; live cancel; pre-worker cancel; launch/init failure |
| running | succeeded, failed, cancelling, blocked | Completed pipeline; structured failure; cancel; lost worker with unresolved IDs |
| cancelling | cancelled, blocked, failed | Confirmed stopped; unconfirmed prompts; lost worker without unresolved prompts |
| blocked | cancelled | Explicit scoped cancel confirms every unresolved prompt stopped |
| cancelled | — | Terminal; cancel is an idempotent no-op |
| succeeded | — | Terminal; cancel is an idempotent no-op; retry refused |
| failed | — | Terminal; cancel is an idempotent no-op |

Retry creates a NEW job/output with `parent_job_id`, `attempt + 1` and the frozen
request. It accepts failed/cancelled/blocked parents, never rewinds their state.
Every parent requires confirmed stopped prompts; retry probes but does not
cancel them. Changed frozen input hashes refuse retry: explicitly submit a fresh
request. Pipeline completion, even with a technical FAIL, never implies approval.

## Persistence and launch

STATE is `$SONORANLAB_STATE`, default `~/sonoranlab-work/mcp`. API path helpers
remain the confinement authority; game checkouts are protected even inside STATE.

```text
jobs/<job_id>/job.json          schema sonoranlab.job/1
              request.json     frozen kind, normalized params, input hashes
              events.jsonl     one committed JSON line per transition
              job.lock         read/modify/write lock
              lease.lock       kernel lock held by the worker
              cancel.lock      serializes explicit cancellation/retry confirmation
              worker.log       stderr including children, truncated at 1 MiB
artifacts/<project>/<character>/<anim>/<job_id>/
idempotency/<sha256(key)>.json
jobs/.submit.lock              reservation transaction lock
STATE/gpu.lock                 shared GPU/heavy-CPU scheduling lock
```

Every record mutation holds job.lock and uses same-directory temp+fsync+replace
and directory fsync. `job.json.history` is authoritative if a crash splits record
replacement from journal append. Missing journal tails are reconstructed from
history. A torn last line is completed only if it is an exact prefix of the next
durable history event. No existing journal bytes are removed or rewritten; any
mismatching journal is preserved for operator inspection. Internal records refuse nonregular files and symlinks.

Idempotency claims use temp+fsync and exclusive os.link under the reservation
lock. Raw keys are never stored. Same key/same canonical request fingerprint
returns the original job, even terminal; changed params/inputs yield
idempotency_conflict. Every new reservation, keyed or not, conflicts with identical queued/running/cancelling work
conflicts and names the existing job. A crash mid-reservation can leave an
incomplete claim; it refuses replay and requires manual inspection. The job record,
frozen request, output directory and lease inode are durable before publishing the
claim. Claims naming a missing directory/record or non-object JSON refuse replay.

Default launch uses a working `systemd-run --user` to create a collected
`sonoranlab-job-<job_id>` service outside the MCP/desktop scope, explicitly carrying
STATE/runtime environment and working directory. Fallback/test launch uses
Popen(start_new_session=True), stdin/stdout disconnected and stderr to worker.log.
Force `SONORANLAB_JOB_LAUNCHER=systemd|popen`. A failed explicit systemd launch
records retryable launcher_failed and never launches a second worker. Popen
survives client/server exit but lacks the independent systemd cgroup's oomd
isolation. Inspect worker.log or `journalctl --user -u sonoranlab-job-<job_id>`.
A small independent stderr sink truncates at 1 MiB and survives worker SIGKILL
until child writers close, so a draining run-window never loses its pipe reader.
It inherits no worker lease/scheduler descriptors and ignores cancellation TERM.

A second worker cannot acquire lease.lock and exits quietly. A worker runs ONLY
a queued record, never a started attempt. GPU and heavy-CPU executors poll the
shared gpu.lock every second, noticing cancellation while waiting. This scheduler
does not replace admission: each actual generate still uses comfy.guard and
env/runwindow.py. Never wrap an entire multi-generation animate in one claimed
admission window. Track E needs ≥40 GiB; C ≥60 GiB, plus 8 GiB guard margin;
no gtt credit. Context.run maps guard exit 3/4 to retryable budget_refused/guard_busy.

Executors implement gpu, optional heavy_cpu, need_gib, validate, run and stage.
validate returns normalized JSON params plus input hashes without creating
anything. run returns JSON or raises JobFailure(code, stage, message, retryable).
Use ctx.check_cancelled() between operations, ctx.set_stage() for measured stages,
and join all children before returning. Pass `runner=ctx.run` to animate/sweep:
it accepts their subprocess.run arguments, waits for guard cleanup, and checks
cancellation before another generation can launch. stage reads files only, without network,
launches or expensive model imports.

## Recovery

Reconcile on initial registry discovery and every status/list read. This is local
disk/lock work only: never probe services, submit, launch, retry or infer success.
Registry discovery is the earliest startup hook inside this ticket's allowed
files; server construction itself does not load tool modules. Restarted clients
and servers see the same jobs and progress.

| Crash boundary | Read-time outcome |
|---|---|
| Started worker still queued, or before a submission file | failed(worker_lost) |
| After submission.json, before posting | blocked(needs_reconciliation), durable prompt ID |
| After posting, unresolved prompt during generation/processing/export | blocked(needs_reconciliation), prompt ID |
| Processing/export after all prompts have results or confirmed-stop resolutions | failed(worker_lost); never inferred success |
| Partial/corrupt/nonregular job.json | blocked(corrupt_record); preserved, other jobs still listed |

A PID is informational; a held lease establishes liveness. A free lease is a dead
worker even if the PID exists. Cancellation records intent, then signals only the
worker main process (`systemctl --kill-whom=main` or `os.kill(pid)`). Boot ID,
process start ticks and process-group identity protect Popen signalling against stale/reused PIDs.
The worker forwards TERM once to direct children; runwindow forwards to generate
without raising through subprocess.run and prematurely killing the guard's drain.

Active cancellation returns cancelling immediately. The worker confirms after
children stop. Both cancellation and normal executor completion cancel unresolved
prompts while retaining the worker lease and GPU scheduler lock, with job.lock free
during probes. An unconfirmed normal completion becomes blocked(needs_reconciliation),
retaining the executor result and any failure under error.detail.
Blocked cancellation calls client.cancel(prompt_id) only for its
own unresolved submission IDs: never a global interrupt or a foreign prompt.
A result file or persisted confirmed-stop resolution is resolved. Probes reporting
done/unknown, or a container confirmed absent/stopped, confirm stop. Running,
pending, any probe error or unreadable/missing submission leaves blocked(cancel_unconfirmed).
A remembered ID whose submission file disappeared remains unresolved; it cannot
authorize cancellation without the owning submission evidence.
Repeated cancellation is safe. Default probing is ComfyClient over
PodmanExecTransport to sonoranlab-comfy; only mutation/worker cancellation probes.

Restore container/API access, inspect this job's IDs, then explicitly cancel and
read status. Do not erase submission evidence or start replacement GPU work to
make blocked state disappear. Started queued workers with a free lease become
failed(worker_lost) and can be explicitly retried; they are never automatically restarted.

## Registry evidence

Imported artifacts must still match their import snapshot (excluding review.json,
which is checked by the review library). Changed or missing indexed files and added
export files produce invalid review with `import_drift`. An import without its final
report/manifest cannot become production usable by adding them later; import fresh evidence.

Reference-file verification is separate from approval-record verification. Corrupt
or invalid approval records do not prevent idempotent registration or a new owner
approval when the references still match. Approval records are ordered by their
timestamp filenames: the newest relevant record must be valid and bound to current
hashes. A newer invalid/unreadable record makes approval invalid even if older valid
records exist; a newer valid owner approval supersedes earlier invalid records.
Historical approval problems remain listed after supersession. Reference drift
always makes approval invalid and refuses owner approval until resolved.

## Retention and cleanup

No automatic expiry or deletion is performed. Keep records, requests, events and
idempotency claims for as long as keys may be reused. Deleting a claim permits
new work under that key; deleting artifacts removes recovery evidence. Back up
STATE as a unit. A retained claim naming a missing/archived job refuses replay.

For owner cleanup, verify terminal state, a free lease and confirmed stopped
prompts, then archive the record directory and exclusive artifacts together.
Never routinely clean queued/running/cancelling/blocked jobs or their claims.
Delete stale .tmp-* files only after confirming no writer holds the associated
record/reservation lock. Never unlink live lock files: a new inode defeats kernel
mutual exclusion. Corrupt records and mismatching journals require forensic
inspection, not automatic repair or retry.

## Test-only hooks

`SONORANLAB_JOB_EXECUTORS=<importable module>` adds its EXECUTORS mapping in the
submitter and worker; an optional Prober factory injects filesystem-backed
confirmation. Like SONORANLAB_PROC, this is trusted test environment configuration,
never a tool parameter or untrusted plugin surface. Tests force Popen and use file
and lease handshakes, fake prompt states and crash injection; no GPU/ComfyUI/podman.
