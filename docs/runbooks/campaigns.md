# Class campaigns (DYL-129, ADR-0011)

A campaign is the durable record of one new class's generation: what was admitted, every seed spent, every job
outcome, the agent's recommendations and the owner's decisions. It is a ledger and a decision layer over the
Phase 4 job manager, not a scheduler or a queue. Generation still runs as ordinary durable `animate` jobs.

## Lifecycle

```sh
sonoranlab campaign create   --project P --request REQUEST.yaml --reference refs-xxxxxxxxxxxx [--revise ID]
sonoranlab campaign status   --campaign ID           # read-only; derived states + next allowed actions
sonoranlab campaign submit   --campaign ID --clip run [--seed N]
sonoranlab campaign decide   --campaign ID --job JOB --decision selected|rejected --reason TEXT
sonoranlab campaign escalate --campaign ID --clip run --reason TEXT
sonoranlab campaign report   --campaign ID
sonoranlab campaign note     --campaign ID [--text T] [--cost-source S --cost-units U --cost-amount N]
```

Each verb prints the same `sonoranlab.api/1` envelope as the MCP tool `campaign_<verb>` (exit 0 ok, 1 refused or
not created, 2 invalid parameters).

**Create** runs `check_class_request` against the registered reference set, recomputes reference approval
(approved hashes must equal the set's current hashes) and requires the character's profile row. If any gate is not
met, **nothing is written**: the result is `created: false` with the blocking state (`NEEDS_OWNER_INPUT` for an
invalid request, `NEEDS_REFERENCE_PREPARATION`, `AWAITING_REFERENCE_APPROVAL`, `AWAITING_PROFILE_ROW`) and the
findings. A character has at most one campaign; changed inputs are a new revision (`--revise ID`).

**Revisions.** A changed request, profile or reference set is revision N+1 (`frozen-r<N+1>.json`, linked to N).
Consumed budget, used seeds and every earlier job stay visible and keep counting. An unchanged revise writes
nothing. If the profile file or the character row changes under a live campaign, status reports
`NEEDS_OWNER_INPUT` (`revision_required`) and submission is refused until a revision is frozen.

## Budget

Per clip: profile `autonomy.max_seeds_per_attempt × max_attempts_per_animation` (twos-are-up: 4 × 3 = 12), or a
stricter owner-approved number given at create with its decision reference. Consumption counts submission
intents, not idempotency keys or candidate names, across all revisions. It is never reset; an owner `budget_grant`
adds seeds.

| Outcome class | Consumes | Blocks further submission |
|---|---|---|
| `pass`, `validator_fail`, `no_verdict`, `generation_failed` | yes | no |
| `guard_refusal` (guard exit 3/4), `infra` (unavailable, launcher/worker start, worker lost, model provenance, changed inputs, executor crash), `admission_refused` (manager refused the request) | no | yes, until owner `resume` |
| `cancelled` | only if a prompt was submitted | yes, until owner `resume` |
| `unknown` (intent with no job for its key) | yes | yes, until owner `resume` |
| `blocked` / `foreign` job (unresolved prompts, corrupt or mismatched record) | yes | yes; reconcile the job (`cancel_job`), no decision clears it |

Each submission is one Track E `animate` job with one seed: the next seed never used for that clip (failed and
refused seeds are never reused). The manager's own `max_attempts_per_animation` admission still applies; when it is
reached the clip is `HOLD_BUDGET` even if seeds remain. **Ceiling:** with one seed per job the profile's 3-attempt
limit is reached before the 12-seed budget; a `budget_grant` cannot lift a profile limit.

## Crash recovery

`submit` writes a `submitting` intent (key `campaign/<id>/r<rev>/<clip>/seed-<n>`) before calling the manager, then
`submitted` with the job ID. The next mutating call that finds an intent without a job looks the key up in the
manager's idempotency claims (then job records): found → the job is adopted; not found → `unknown`, BLOCKED, never
resubmitted. Status shows such an intent as `submitting` (GENERATING) and resolves nothing. Terminal jobs not yet
recorded are appended as `job_observed` by the next mutating call; derived state never waits for that.

## Owner decisions (CLI only, never MCP)

```sh
sonoranlab campaign owner-decision --campaign ID --kind budget_grant --clip run --seeds 2 --verbatim "TEXT"
sonoranlab campaign owner-decision --campaign ID --kind resume --ref JOB_OR_KEY --verbatim "TEXT"
sonoranlab campaign owner-decision --campaign ID --kind install --ref BUNDLE --verbatim "TEXT"
sonoranlab campaign owner-decision --campaign ID --kind delivery --state staged_and_engine_verified|ready_to_install|installed --ref BUNDLE --verbatim "TEXT"
```

**An agent must never run `owner-decision`.** Kinds `profile_row`, `reference_approval` and `note` record context;
`--activity preparation|cleanup|review --seconds N` records owner time on any decision. `installed` requires an
earlier `install` decision for the same bundle. Reference approval and 1× review stay the existing owner CLIs
(`registry approve-references`, `review record`).

## Files and locks

```text
STATE/campaigns/<project>/<character>/.create.lock     one campaign per character
                                     <campaign_id>/frozen-r<N>.json  immutable, 0444, sha256 in the revision event
                                                   events.jsonl     hash-chained, fsynced, append-only
                                                   head.json        last committed {seq, sha256}
                                                   campaign.lock    single writer (non-blocking flock)
                                                   lock.owner       holder identity ($SONORANLAB_CAMPAIGN_OWNER or pid@host)
                                                   events.torn-*    preserved torn tails
```

A second writer gets `conflict` with message `campaign_busy: ...` (retryable) naming the holder. A torn last line is
ignored on read and moved to `events.torn-*` before the next append. A broken chain, a changed frozen file, or a
head that disagrees with the lines (truncation, rewritten last line) makes status `BLOCKED` (`ledger_invalid`) and
every write refuses; preserve the directory for inspection.

## Report

`campaign_report` lists per clip: job IDs, seeds, revision, outcome class, verdict failures, and accounting from the
job record and animate.json: queue wait (submitted → started), generation (failed attempts included), processing,
validate/export, render (currently always null: not measured per attempt), run time, the guard's admitted memory
budget (`need_gib`) and memory sampling only where the generation result recorded it. Unknown values are null,
never 0; totals report `known`/`null` counts. Wall time is the union of attempt intervals, so overlaps count once.
Human time comes only from owner records, and agent tokens/API cost only from explicit `campaign_note` cost
records (source + units).
