# ADR-0009 — Aseprite targeted-repair pilot (optional editor companion)

**Status:** proposed (owner decision pending at DYL-111). Nothing here is adopted. Phase 3.5 builds and measures the
pilot; the owner records ADOPT, DEFER, REJECT or HOLD at DYL-111. Environment evidence:
[`benchmark/results/2026-10-20-phase3.5/dyl106-environment.md`](../../benchmark/results/2026-10-20-phase3.5/dyl106-environment.md).

## Context

The deterministic pipeline can leave a few wrong pixels in an otherwise good animation (a stray fringe, a clipped
weapon tip). Today the options are regenerating the whole clip or hand-editing in Aseprite/Pixelorama. A pixel editor
driven through an agent could repair a handful of pixels inside a declared region, but only if SonoranLab stays the
authority for what is accepted. The pilot asks one question: can a contained editor make small, verified repairs that
come back through the existing validator, exporter and human review unchanged?

SonoranLab remains the authority for source selection, copies, hashes, authorization, verification, validation, export
and review. The editor is a pixel tool reachable only through a SonoranLab broker. The core must work with no editor
installed.

## Decision (proposed)

### Pinned stack

| part | pin |
|---|---|
| editor | Aseprite 1.3.18.6, owner's licensed AppImage, extracted, unmodified (sha256 in the environment note) |
| bridge | with-pebbly/aseprite-ai-artist v0.8.2 = `5894b12cead839789077b279369409f868451ffc` (MIT), built offline from `package-lock.json` (`npm ci --ignore-scripts`, `tsc`); never `npx ...@latest`; upstream `install` never run |
| sandbox | bubblewrap 0.9.0 (unprivileged userns), nested Xephyr display |
| location | `~/sonoranlab-work/phase3.5/runtime/`, outside the repo; no agent config or `~/.config/aseprite` change |

### Containment is code and OS, not prompting

A prompt telling the editor worker to stay inside a region is not a sandbox: the worker can be wrong, confused or fed
hostile text from a file. Every limit below is enforced where the worker cannot talk its way past it.

1. **Sandbox per repair session** (`bwrap --unshare-all --die-with-parent --new-session --clearenv`): private network
   namespace (only `lo`; the bridge's 127.0.0.1 ports exist only inside it), minimal explicit env, `aseprite` not on
   `PATH`, no `ASEPRITE_PATH`, `ASEPRITE_AI_HEADLESS` or `ASEPRITE_AI_ALLOW_LUA`. Read-only: `/usr`, `/etc`, extracted
   Aseprite, upstream build, node. Writable: only `<repair>/workspace/`, a private HOME/`ASEPRITE_USER_FOLDER` under
   `<repair>/session/home`, and a tmpfs `/tmp`. Source copies, the parent job, the game checkout and the owner's
   `~/.config/aseprite` are not mounted. Killing the sandbox kills the bridge (PID namespace).
2. **Display:** Aseprite runs live on a nested Xephyr; only that socket is bound in. The owner's own Aseprite
   instance, documents and config are a different process and are never touched.
3. **Broker** (SonoranLab, outside the sandbox) holds the stdio MCP session to the in-sandbox `serve` (no `--allowLua`,
   no `--headless`). The editor worker never receives the upstream MCP server; it gets only the broker allowlist in the
   capability matrix.
4. Every call must name `sprite:"#<id>"`, `layer` and `frame`. A missing target, any `path`, `preflight mode`,
   `run_lua`, a `selectionOnly` that relies on a live selection, or any other family is refused before the editor sees
   it. Colours must be opaque `#rrggbb` members of the project palette, or transparent (erase).

### Capability matrix

Upstream registers 18 tools. Verified on the live server: 18 tools, no `run_lua` (started without `--allowLua`; env
lacks `ASEPRITE_AI_ALLOW_LUA`). Columns: exposed to the editor worker via the broker.

| upstream tool / op family | exposed | constraint | reason |
|---|---|---|---|
| `preflight` (no `mode`) | broker-only | broker calls it to confirm connection and versions | worker has no need |
| `preflight` with `mode` | no | refused | upstream accepted `mode=headless` (`switched: true`) in the smoke; only the missing binary stopped it |
| `sprite_info` | yes (read) | explicit sprite | metadata compare |
| `sprite_manage` `save` | broker-only | fixed path `workspace/doc.aseprite`, at `session checkpoint\|stop` | only the broker persists |
| `sprite_manage` new/open/close/other | no | refused | the broker opens the one document |
| `look` `ascii\|diff\|preview\|filmstrip\|onion` | yes (read) | explicit sprite/frame | images go to the agent's model provider |
| other `look` ops | no | refused | not needed |
| `read_pixels` | yes (read) | `composite:false`, explicit layer and frame | exact pixel grids |
| `draw` `pixels`, `grid` | yes (mutate) | frame and pixels inside the mask; palette colours or erase | the repair primitive |
| `draw` `clear` | yes (mutate) | region required, inside the mask | erase stray pixels |
| `draw` `blit` | yes (mutate) | source and destination inside allowed frames and mask | copy a fix from a neighbour frame |
| `draw` other kinds (lines, fills, shapes, text) | no | refused | unbounded footprint, antialias and snapping |
| `cel` `move` | yes (mutate) | allowed frames only, only if authorization allows `cel_move` | alignment repair |
| `cel` other ops | no | refused | |
| `select` | no | refused | live selection state is hidden input |
| `transform` | no | refused | resamples pixels |
| `layer`, `frame`, `tag` | no | refused | structure is fixed by the round-trip invariants |
| `palette`, `recolor` | no | refused | the palette is SonoranLab's |
| `validate` | no | refused | editor lint never replaces SonoranLab validation |
| `reference`, `tileset` | no | refused | out of scope (tilesets are out of v1) |
| `export` | no | refused | the broker/finalize export, never the worker |
| `run_lua` | absent | not registered | arbitrary code; see "Containment" and the smoke |

### Edit targets, mask and diff policy

`authorization.json` is written before a session by the orchestrator/owner (`repair authorize`), never by the editor
worker, and is immutable and hash-bound: repair id, parent hashes, source revision, allowed op types, per-frame masks
(rects and/or pixel lists in cell coordinates, keyed by kind, direction, index), declared mirror derivations, max
iterations (2). Before every mutating op the broker snapshots all frames of the art layer and the sprite metadata;
after it, it re-reads and computes the exact changed coordinate set. Accept only if every changed pixel is in an
allowed frame, inside that frame's mask, from an allowed op type, and metadata (size, colour mode, palette, layers,
frame count, durations, tags) is unchanged. Otherwise the broker restores the pre-op pixels, re-verifies equality
with the snapshot and logs the rejection. If the restore cannot be verified the session is `TAINTED`: all further ops
and finalize are refused.

### Single writer

`session start` takes an exclusive `fcntl` lock on `<repair>/session/lease.lock` for the broker's lifetime and writes a
random token (0600). Every mutation presents the token. A second `session start` on the same repair, or a missing or
stale token, is refused. One sandbox, one Aseprite, one bridge. Critics receive exported snapshots only.

### Unknown outcomes

A timeout, dropped reply or disconnect during a mutation records `outcome_unknown`; the broker never resends. On the
next request it re-reads state and classifies it as unchanged, applied-in-mask or violation (restore). A disconnect or
`not_connected` sets `DISCONNECTED` and refuses ops. No disk fallback, no headless fallback, no force-save. A new
explicit `session start` is required.

### Round-trip invariants

`repair prepare` copies inputs byte-exact into `source/` (0444, sha256 recorded) and builds `workspace/doc.aseprite`
with SonoranLab's own Lua run by Aseprite batch inside the same sandbox (a conversion step, not an editing fallback).
RGB mode, canvas = cell, one layer `art`, palette = transparent + project palette, 8 stills then each generated
direction's clip frames. Timing stays in the SonoranLab manifest; `1000/fps` must be an integer or prepare refuses.
Mirrored directions are not in the document and are re-derived by the exact flip at finalize. An unmodified round trip
must be pixel-identical for all frames and all 8 stills, with identical tags, durations, frame count, canvas and
mapping. Comparison is on decoded RGBA: alpha 0 pixels compare equal regardless of hidden RGB, alpha must be exactly
0 or 255, every opaque colour must be in the palette. Any difference means HOLD for real repairs.

### Reviewed-artifact lineage

The parent job is never modified. Finalize re-verifies parent/source/authorization hashes and log integrity, refuses
`TAINTED`, exports the saved document, diffs it against the immutable source, refuses any change outside the
authorization, assembles frames and stills, and runs the existing validator (unchanged thresholds, ADR-0008), pack,
Godot check and guarded exporter. It writes a **child job** in the normal layout plus `repair-manifest.json` (parent
ids/hashes, editor and extension versions, authorization, operations, before/after coordinate diff, validator and Godot
results, review status pending). No PixelOE, palette snap, registration or resize runs on the result. The child needs
a **new artifact-bound review** (`review record/check` unchanged). **The parent's approval is never inherited.**
Neither the editor's lint nor a critic's score replaces SonoranLab validation.

### Data disclosure

`look` images and pixel grids go to the agent's model provider, which the owner has already approved for agent work.
This is a disclosure to that provider and nothing else; no other party receives art, and there is no cloud fallback.

### Pre-existing `aseprite` MCP server

The owner's user scope already registers a different server (diivi/aseprite-mcp @ `90d1696`, in `~/.claude.json` and
`~/.codex/config.toml`) that exposes `run_lua_script`, i.e. arbitrary Lua. It is left untouched. It is excluded from the
editor worker's tool set (as are the upstream MCP and PixelLab). The worker's shell runs inside its agent's own OS
sandbox (writes confined to a scratch directory, no network; verified at DYL-110 before the first trial), and its only
path to an editor is the broker CLI.

## Adoption criteria (the owner decides; this ADR only states them)

| disposition | requires |
|---|---|
| ADOPT | required safety and interchange tests pass and useful repair capability is demonstrated. Kept as an optional companion with documented supported operations; Phase 4 may expose only the scoped repair handoff (DYL-118). |
| DEFER | promising but not reliable or useful enough. Preserve the work, disable the editor integration, core Phase 4 proceeds. |
| REJECT | unacceptable compatibility, safety or practical-value problems. Document it and preserve the pipeline. |
| HOLD | evidence incomplete or an unresolved safety issue. Name exactly what needs resolving or which owner decision is missing. |

Mandatory for any ADOPT: source immutability; all 8 stills preserved; profile and clip metadata unchanged; zero
unintended changed pixels; stale or malformed review records rejected. Trials are capped at 3 cases x at most 2 repair
iterations.

## Consequences

- Phase 4 waits for the DYL-111 disposition, not for an Aseprite success. The deterministic pipeline must work without
  Aseprite; the core imports no editor code, and `sonoranlab repair *` reports `editor_unavailable` when the runtime is
  absent.
- The pilot adds a CLI surface (`sonoranlab repair prepare|authorize|inspect|session|op|diff|finalize|abort`) and a new
  artifact type (the repair job). On REJECT or DEFER these stay out of the MCP surface.
- Rollback is deleting two directories under `~/sonoranlab-work/phase3.5/` (see the environment note).

## Residual risks

- Same-UID processes outside the sandbox can still read and modify `<repair>/workspace` and the source copies on disk.
  Hash checks detect this; they do not prevent it. An agent with a general shell can do anything the user can; only the
  editor worker's tool path is contained, so it runs with a restricted tool set.
- Xephyr is an X client of the owner's display (normal desktop trust).
- The upstream extension and its bundled skills are third-party code executing inside the sandbox.
- bwrap needs unprivileged user namespaces (Ubuntu's AppArmor policy currently permits them).
- Upstream accepts `preflight mode=headless`; safety depends on the broker refusing `mode` and on no binary being
  reachable in the sandbox.
- The sandbox's private HOME (`<repair>/session/home`) persists across sessions of one repair; code running inside the
  sandbox could plant an extension there. The worker has no code-execution path into the sandbox (no Lua, no upstream MCP).
- Codex's `collaboration.*` sub-agent tools remain in the contained editor worker (same OS sandbox, same overrides).

## Consequences after the trials (2026-10-20; still proposed)

Evidence: [`dyl110-trials.md`](../../benchmark/results/2026-10-20-phase3.5/dyl110-trials.md),
[`phase35-gate.md`](../../benchmark/results/2026-10-20-phase3.5/phase35-gate.md).

- The round trip is lossless on the approved pyromancer run. All 4 edited cases stayed inside their frozen masks with zero
  unintended pixels; an independent checker confirmed it.
- **Editor-worker containment requires explicit overrides.** `codex exec -c mcp_servers={}` does not remove MCP servers, and
  MCP servers run outside Codex's OS sandbox. The contained worker uses per-server/plugin/feature `enabled=false` /
  `--disable` overrides (MCP=NONE). Codex's sandbox blocks AF_UNIX, so the worker reaches the broker through
  `sonoranlab repair relay` (mailboxes in its own directory; the relay holds the token).
- **Additive repairs are colour-limited.** The pipeline saturates the per-cell 64-colour cap, so a repair may only reuse
  colours already in the frame. The validator correctly refused an otherwise better-looking edit that added 3 colours.
  Lifting this is an owner decision about the threshold or about authorizing out-of-mask colour merges; it is not part of
  this ADR.
- Owner 1× visual approval recorded 2026-10-20 (A1, A2, B1, B2 it.2, C1); artifact-bound reviews written with a
  timestamp-bounded interval (no stopwatch). Senior review: REVISE before merge, narrow ADOPT recommended; its P1/P2 findings
  (staging-path confinement, repair-manifest approval binding, `binds.job`) are corrected (gate document, revision 2).
- **Threat model of the path checks:** they defend against mistaken or hostile paths and symlinks present at check time; a
  concurrent same-UID process renaming ancestor directories during a command is out of scope (it can write there directly);
  final verification is best-effort detection. Upgrade: mount-namespace isolation for prepare/finalize.
- Proposed scope if adopted: optional contained editor; supported operations `draw.pixels`, `draw.grid`, `draw.clear`, bounded
  `draw.blit` on authorized frames; `cel.move`, transforms, rigging and animation creation are not adopted.
- Disposition: **pending the owner's formal ADOPT / DEFER / REJECT / HOLD** (status stays proposed until then).
