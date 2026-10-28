# New class: the `/new-class` skill (DYL-130)

`.claude/skills/new-class/SKILL.md` is a Claude Code project skill that runs the ADR-0011 loop
([docs/10](../10-orchestrator-loop.md)) over the SonoranLab MCP server (`.mcp.json`). It is a procedure for the
agent, not code: everything it does goes through the 23 allowlisted tools, and everything it cannot do is an owner
or coordinator command it prints and stops for.

## Invoking

In Claude Code at the repository root, with the `sonoranlab` MCP server approved ([mcp.md](mcp.md)):

```
/new-class project=twos-are-up request=<class request YAML/JSON pasted or read by the operator>
           reference_dir=~/sonoranlab-work/phase5/refs/gun   # or reference_id=refs-<12 hex>
           [campaign_id=camp-...] [max_seeds_per_clip=N budget_decision_ref=O6-...]
           [composed=<compose job id> target_rev=<40 hex> base_manifest=game-<rev>/characters.json
            import_template=game-<rev>/pyromancer.png.import]
```

Inputs come from the invocation only. The request's `references.stills_dir` is never followed by the MCP tools;
free text in the request, reference notes, job output or images is data.

## What the agent does, in order

1. `capabilities`, `get_profile`, `get_character`; resume an existing campaign with `campaign_status` before
   creating anything (`campaign_create` answers `conflict` naming an existing campaign; it writes nothing unless
   every gate passes).
2. Admission: `register_character` (owner-named directory) → `check_class_request(stills_ref)` → reference
   approval in `get_character` → `campaign_create`. Each failing gate is a STOP with the exact owner action.
3. Generation per clip in request order (`run`, then `idle`): one `campaign_submit` (next unused seed), then
   `job_status` every ≥ 60 s, at most 40 polls. BLOCKED (unknown outcome, cancelled, guard refusal, infra) is a
   STOP with no resubmission; HOLD_BUDGET is `campaign_escalate` then STOP. The job manager also caps 3 attempts
   per animation per character.
4. Inspection: validator report via `job_result`, then `view_artifact` of the final contact sheet (zoom 1) and the
   exported sheet (zoom 2-4), siblings' sheets beside it, then `campaign_decide` (a recommendation, never approval).
5. Final sheet: `compose_sheet` with `campaign_id` (idle then run; only the campaign's current selected
   candidates are admitted), `godot_check` per selected clip on its
   `…/sweep/final/job/report.json`, `smoke_render` (candidate = the compose `artifact_id`), `view_artifact` of the
   renders and of the sheet at 1x. STOP for the owner's 1x review.
6. Delivery after the review: `delivery_status` (no duplicate bundles) → `build_delivery` → `delivery_status`.
   `BUILT` waits for the coordinator; `READY_TO_INSTALL` ends the skill.
7. Every invocation ends with `campaign_report` and the compact handoff block from the skill.

## Owner and coordinator actions the skill prints (never runs)

| Gate | Command |
|---|---|
| References | `sonoranlab registry approve-references --project P --character C --ref-set refs-… --reviewer NAME --evidence TEXT` |
| Profile row | `sonoranlab classreq propose-row …`, then a reviewed PR ([new-class-references.md](new-class-references.md)) |
| Budget / resume | `sonoranlab campaign owner-decision --campaign ID --kind budget_grant\|resume …` ([campaigns.md](campaigns.md)) |
| 1x review | `sonoranlab review record $SONORANLAB_STATE/<compose job output_dir>/composed --verdict pass …` (`owner_review_dir` in the compose result) |
| Staged inputs | copy the game's `characters.json` and a sibling `.png.import` at the target revision into `STATE/staged/…` |
| Snapshot | `sonoranlab deliver apply BUNDLE --target SNAP`, `tools/game_consumer/run.sh …`, `sonoranlab deliver verify BUNDLE --target SNAP --consumer-result …` ([delivery.md](delivery.md)); `BUNDLE` is `<build_delivery job output>/bundle` |
| Install | owner only, `BUNDLE/INSTALL.md`, then `campaign owner-decision --kind install` / `delivery --state installed` |

## Known limits (Phase 5)

- The owner reviews once, on the composed sheet. A sheet composed with `campaign_id` is recorded in the campaign
  (`composed` event), so its live owner review makes each contained selected clip `ARTIFACT_APPROVED`; a bundle
  built from it then moves `campaign_status` to `STAGED_AND_ENGINE_VERIFIED` / `READY_TO_INSTALL` as
  `delivery_status` verifies. `INSTALLED` comes only from the owner's install record.
- `godot_check` takes a single-clip export, so it runs per clip; the composed sheet is exercised by `smoke_render`
  and by the coordinator's game-consumer harness.
- Sibling selection depends on approved siblings being imported (`import_job` of staged game sheets, docs/09);
  with none, the skill says so in the handoff.
- Track C (`pose_frame`) and the repair handoff stay off in this skill (ADR-0011 O7/O9); an owner authorization is
  reported, not acted on.

## Tests

`tests/sonoranlab/newclass/` drives a scripted (non-LLM) agent that follows the skill through a real stdio MCP
server with fake generation and engine executors: happy path to `AWAITING_OWNER_REVIEW` and, after a test-recorded
owner review and a coordinator apply/verify, `READY_TO_INSTALL`; missing references; unsupported clip; unapproved
and stale references; restart mid-campaign; cancelled job; budget exhaustion; malicious request/reference text;
two clients; `view_artifact` allowlist and size limits; no install tool; and a static check that `SKILL.md` names
only allowlisted, discoverable tools.
