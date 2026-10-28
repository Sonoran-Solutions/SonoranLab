# Class sheet compose and delivery (DYL-133)

From per-clip jobs to a bundle the owner can install, without SonoranLab ever writing the game checkout
([ADR-0011](../decisions/ADR-0011-phase5-new-class-contract.md): five separate verdicts, default stop at
READY_TO_INSTALL). Code: `sonoranlab/compose.py`, `sonoranlab/deliver.py`; harness: `tools/game_consumer/`.

## 1. Compose one class sheet

```sh
sonoranlab compose --profile profiles/twos-are-up.yaml --character gun \
  --clips idle=STATE/.../idle-job,run=STATE/.../run-job --stills REFS/gun --out WORK/gun-composed
```

- Each `--clips` job must be an intact, passing `process`/`animate` final job for that character and clip (the same
  integrity check `review` runs: report, manifest outputs, raw copies, profile hash), validated with this exact
  profile, whose export sheet is column 0 = stills then that one clip.
- Every cell is bound to the job's report digests; west rows must be exact mirrors; column 0 must be pixel-identical
  to `--stills` (transparent pixels compared as (0,0,0,0), as the PNG export stores them). Any mismatch: exit 2,
  nothing written.
- The composed sheet is re-validated per clip with the unchanged validator and profile limits. FAIL: exit 1, a job
  dir with `report.json`/`manifest.json` and no `export/`.
- PASS: `WORK/gun-composed/` holds `raw/` (byte copies of each input sheet/report/manifest and the stills),
  `report.json` (per-clip reports, input job ids and hashes), `manifest.json` and `export/gun/` (sheet, `.tres`,
  sidecar). Clip `start` columns follow the `--clips` order (idle 1, run 5 for the game).

The owner then reviews the composed sheet at 1x and records it:
`sonoranlab review record WORK/gun-composed --verdict pass --reviewer ... --started ... --ended ...`.

## 2. Build the bundle (staging only)

```sh
sonoranlab deliver build --profile profiles/twos-are-up.yaml --composed WORK/gun-composed \
  --request REQ/gun.yaml --target-rev <40-hex game commit> \
  --base-manifest STAGED/characters/characters.json \
  --import-template STAGED/characters/pyromancer.png.import --out WORK/gun-bundle
```

Refused (exit 2, nothing written) when: `review check` of the composed job is not clean (no owner review, a fail
verdict, any reviewed file changed); the job is not a `compose` job or used another profile; the clip table differs
from the profile or lacks idle/run; the request's clips are not all present; the id already exists in the base
manifest; the base manifest's rows/mirrors/density differ from the profile or it does not round-trip as the game's
indent-1 JSON; `round(height_m x density)` differs from the south still's opaque height (`standing_pixels`, the
relation every shipped entry satisfies); the `.import` template is not a lossless, no-mipmap texture import; the
target revision is not a full commit; `--out` exists or is inside a protected game checkout.

`BUNDLE/`:

| Path | What |
|---|---|
| `bundle.json` | `sonoranlab.delivery_bundle/1`: `state` (BUILT / READY_TO_INSTALL), `source` hashes (sheet, review record, validator report, request, profile, import template), `target` {game, revision, base_manifest_sha256}, `files` [{path, action add/modify, sha256_before, sha256_after}], `manifest_entry`, `known_missing_clips`, `playable_class`, `states` (four separate verdicts with evidence), `bundle_files` hashes |
| `files/<game path>` | the sheet, its `.png.import`, the patched `characters.json` |
| `characters.json.diff` | unified diff of the manifest patch (one entry appended, nothing else touched) |
| `checks/{before,after}.sha256` | `sha256sum -c` inputs used by INSTALL.md / ROLLBACK.md |
| `provenance.md` | composed and clip job ids, model inventory from the jobs' manifests, review, hashes |
| `INSTALL.md`, `ROLLBACK.md` | exact owner-run commands for the real checkout |

The `.import` is the sibling's with the source and imported paths re-pointed (Godot names the imported file
`<file>-<md5(res path)>.ctex`; the builder checks that rule on the template first) and a deterministic uid in Godot's
text form. If Godot still rewrites it on import, the consumer check reports `engine_rewrote` and keeps the
rewritten copy; nothing is hidden.

A Phase 5 sheet with idle + run only is reported as `known_missing_clips` cast and death, `playable_class: false`:
PixelStandee selects `cast` while casting or shooting (no fallback), and the game's `--pixel-check`
(`PixelGallery.Check`) requires a death clip, so it stops at the new entry's clip table.

## 3. Apply, check, roll back in a disposable snapshot

```sh
git -C ~/sonoranlab-work/phase5/game-snapshot rev-parse HEAD     # must equal the bundle's target revision
sonoranlab deliver apply  WORK/gun-bundle --target SNAP
GODOT=... tools/game_consumer/run.sh --snapshot SNAP --bundle WORK/gun-bundle --out WORK/gun-consumer \
  --test-class <game data-layout test class> --sibling pyromancer --sibling samurai
sonoranlab deliver verify WORK/gun-bundle --target SNAP --consumer-result WORK/gun-consumer/result.json
sonoranlab deliver rollback WORK/gun-bundle --target SNAP
```

`apply` refuses (exit 2, nothing changed): a target that is, or resolves (symlinks included) into, the profile's
`export.godot_project` or any repository profile's (the `api/paths.py` roots); not a git worktree root; HEAD not the
target revision; a dirty tree; a `sha256_before` mismatch or an existing added file (stale base); a path escape or a
symlinked path; bundle files or the owner review changed since build; an existing apply journal (already applied).
It backs up every modified file under `BUNDLE/.applied/<target-id>/`, journals, installs each file by tmp + rename,
and rolls back automatically if any step fails (the journal dir is kept as `<id>.failed-N`).

`rollback` restores the exact bytes from the backup (even if the engine changed a file after apply, which it
reports), checks every `sha256_before`, and requires a clean `git status`; the journal dir becomes
`<id>.rolledback-N`, so a later apply works again.

`verify` checks the applied files and `git status`, and with `--consumer-result` that the harness passed on exactly
these files at the target revision. Only then are STAGED_AND_ENGINE_VERIFIED and READY_TO_INSTALL set (state
READY_TO_INSTALL). INSTALLED is never set by any command: the owner installs with INSTALL.md and records it.

## 4. The game-consumer harness

`tools/game_consumer/run.sh` (read its header) runs, each step supervised (own process group, deadline, bounded
logs, group kill): the game's data-layout tests (`dotnet test SNAP/tests/TwosAreUp.Combat.Tests --filter
FullyQualifiedName~CLASS`, zero executed tests is a failure), a build of the game with the harness script added, a
headless import, and a render in a private headless compositor that calls `PixelCharacter.LoadAll()`,
`PixelGallery.Check()` and `PixelStandee` with the real shader for all 8 rows x (still + every clip frame) beside two
siblings, saving 1x and 4x PNGs under `OUT/render` (64 MiB budget). The harness holds no game source and no art; it is
copied into the snapshot only for the run and removed afterwards. It needs the .NET (mono) Godot binary and must be run
under the host's heavy-job lock.
