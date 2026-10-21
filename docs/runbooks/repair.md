# Optional repair handoff

ADR-0009 adopts small authorized pixel repairs as an optional companion.
SonoranLab MCP is the **acceptance authority**: prepare immutable inputs,
inspect saved results, finalize through validation/export/Godot, retain lineage.
The **contained editor worker** is the single writer: one broker lease, one
sandboxed editor session, only the owner's authorized frames, regions and ops.
The MCP server never proxies pixel edits or starts that session.

```text
registered/imported parent + repository profile
  -> prepare_repair -> immutable source copies + disposable workspace
  -> owner repair authorize -> frozen authorization
  -> contained worker -> broker -> private editor sandbox
  -> checkpoint + stop -> saved document / exported snapshots
  -> inspect_repair_diff -> finalize_repair job (heavy CPU lock)
  -> new validated/exported/checked child -> owner review record/check
```

## Opt in and paths

The presence-only capability check uses `SONORANLAB_EDITOR_RUNTIME`, default
`~/sonoranlab-work/phase3.5/runtime`. An existing directory advertises exactly
prepare_repair, inspect_repair, inspect_repair_diff, finalize_repair and
abort_repair. A fake/partial directory can advertise availability but cannot
pass converter admission. No install, download, health probe or GUI launch
happens during capabilities/discovery.

The CLI finalizer creates a fresh child at
`<REPAIR_ROOT>/<repair.name>-child-<job_id>`, then MCP imports it into the registry.
Durable jobs remain under SONORANLAB_STATE; their output holds only an exclusively
written `finalize-result.json` with the child path, import artifact ID, hashes and
verdicts. The sibling defaults (`~/sonoranlab-work/repairs` and
`~/sonoranlab-work/mcp`) work with the default READ root `~/sonoranlab-work`.
With custom READ roots, the child must be readable under READ/STATE or its import
fails the job with the registry error; the server never widens roots.
Prepare still writes directly under repair_root(), as the existing CLI does.
All input parents must be registered/imported jobs readable under READ/STATE. Game checkout writes,
symlinks, traversal and output reuse remain refused by the existing admissions.

## Owner and worker procedure

1. Import an existing passing exported parent if needed using `import_job`,
   then call `prepare_repair` with project, character, animation and parent ref.
   Keep its returned directory ID and identity hashes. Staged source paths are
   informational; callers cannot supply them to select another repair.
2. The **owner**, outside MCP, writes an authorization specification and runs
   `sonoranlab repair authorize <repair-dir> --spec <owner-auth.json>`.
   Bind the manifest's internal repair ID, source revision and parent hashes;
   choose exact frames/regions, at most two iterations and the adopted ops:
   draw.pixels, draw.grid, clear-in-region and restricted blit. Inspect reports
   exactly what the authorization records; it does not grant permissions.
   cel.move, transforms, rigging and animation creation were not adopted.
3. Follow the Phase 3.5 contained-worker launch procedure recorded in
   [DYL-110 setup/containment](../../benchmark/results/2026-10-20-phase3.5/dyl110-trials.md#setup)
   and [ADR-0009 containment](../decisions/ADR-0009-aseprite-repair-pilot.md#containment-is-code-and-os-not-prompting).
   Start the broker with `sonoranlab repair session start <repair-dir>`
   (optionally `--detach`) and the existing `repair relay` with a private
   mailbox under SONORANLAB_AGENT_ROOT. Give the worker only that mailbox.
   Its invocation must explicitly disable every MCP server/plugin/feature
   exposing external tools, leave network disabled, and confine writes to the
   mailbox. `mcp_servers={}` alone did **not** disable inherited servers in the
   pilot. Verify MCP=NONE before giving it repair work. The broker holds the
   token; the worker never gets the upstream editor MCP or arbitrary Lua.
4. Use one worker through that broker. The existing sandbox supplies a private
   HOME/network/PID namespace and nested Xephyr display; only workspace and
   private HOME are writable. Parent/source/game/config directories are not
   mounted. Finish with checkpoint and stop, for example
   `sonoranlab repair session stop <repair-dir> --checkpoint`.
5. Inspect identity/authorization/lease/journal and the last saved export's
   diff through MCP. Diff performs no new editor export. Lists are capped at
   2000 coordinates per frame with complete counts, including unauthorized
   changes and alpha/palette problems. Active sessions, stale hashes,
   tainted/aborted state, broken journals or missing checkpoints cannot finalize.
6. Call `finalize_repair` and poll `job_status`/`job_result`. The durable job
   freezes input hashes and runs the CLI's acceptance operation under the
   heavy CPU lock. It creates a fresh child under REPAIR_ROOT,
   rechecks the parent, source, authorization and saved pixels, then validates,
   exports and checks in Godot. Technical FAIL can be a completed job; inspect
   its technical verdicts and retained evidence. The result provides the child's
   registry `artifact_id`, informational `child_path`, and file refs of the form
   `<artifact_id>:<relative_path>` accepted by `get_artifact`. Human state comes
   from `registry.check_review` on that import ID. A persistent
   `<repair>/mcp-finalize.json` claim prevents replay once execution starts,
   even after worker death. Prepare a new repair for another acceptance.
7. The **owner** reviews the child and runs `sonoranlab review record
   <child-dir> --verdict pass --reviewer <owner> --cleanup-minutes <minutes>
   --started <ISO-time> --ended <ISO-time>`, then `sonoranlab review check
   <child-dir>`. Use actual review evidence/timing. The child's human state is
   awaiting owner review until a fresh artifact-bound record passes
   sonoranlab.review. Parent approval is never copied. An edited parent export
   invalidates its old review and is refused by acceptance hash checks.

Abort through `abort_repair` or `sonoranlab repair abort <repair-dir>`.
The same writer lease refuses abort while a broker owns the workspace; stop the
contained worker/session first. Aborting retains evidence and forbids finalize.
No authorize/session/op/read/relay/editor-export/Lua tools are exposed by MCP.

## Rollback and separate owner configuration

Remove the editor runtime directory (or set SONORANLAB_EDITOR_RUNTIME to a
missing directory). On the next discovery/call the five optional tools
disappear, capabilities.editor becomes `unavailable`, and direct names return
`invalid_params: Unknown or disabled tool`. Core tools and retained jobs remain
available; existing queued jobs retain their own execution state and can fail
converter admission. Stop active contained sessions separately before removing
their runtime.

The owner's user-scope **diivi aseprite-mcp and PixelLab servers are a separate
owner configuration decision**. This path neither configures nor uses them.
They must be excluded from the contained worker's tool set regardless of their
availability to ordinary owner agents.
