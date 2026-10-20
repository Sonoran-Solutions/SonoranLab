# DYL-106: Phase 3.5 environment and containment smoke (2026-10-20)

Text only (public repo). Evidence: `~/sonoranlab-work/phase3.5/smoke/20261020T182910Z/`. Design: ADR-0009 (proposed).
The smoke used a synthetic 8x6 sprite; no project art was involved.

## Pinned inventory (2026-10-20T18:18:53Z, start main 2fc9323)

| item | value |
|---|---|
| OS / kernel | Ubuntu 24.04.5 LTS, 7.0.0-38-generic |
| Aseprite | 1.3.18.6-x64 AppImage, sha256 `feb6330f3749115f28b7dfd7708be3a4b3afb54140129eeeb680b079a1ba3f3a` |
| bridge | with-pebbly/aseprite-ai-artist v0.8.2, `5894b12cead839789077b279369409f868451ffc` (MIT); upstream main is 8 commits ahead (2026-10-18), not used |
| node / npm | v26.8.1 / 11.19.0 |
| bubblewrap | 0.9.0 |
| Xephyr | 2:21.1.12-1ubuntu1.8 |
| Godot | 4.7.2.stable.official.ed1daf0bf |
| codex / claude | 0.163.0 / 2.1.289 |
| uv / Python | 0.12.19 / 3.13.15 |
| pre-existing `aseprite` MCP | diivi/aseprite-mcp @ `90d1696`, user scope (`~/.claude.json`, `~/.codex/config.toml`); exposes `run_lua_script`; not upstream, left untouched, excluded from the editor worker |
| owner `~/.config/aseprite` | no extensions dir; no Aseprite process running at inventory time |

## How the runtime was built

- Upstream: `git archive` of tag v0.8.2, then `npm ci --ignore-scripts` from `package-lock.json`, then `tsc`.
- Aseprite: AppImage `--appimage-extract`; files not modified.
- node: the host binary copied into the runtime, sha256 `19235a9b678f84729464c52623f92de130a165452747c6826d3fdc13df3abcc3`.
- Everything is under `~/sonoranlab-work/phase3.5/runtime/`. The upstream `install` command was not run.

## Live smoke results

Server `aseprite-ai-artist` 0.8.2 inside the sandbox with Xephyr and Aseprite.

| check | result |
|---|---|
| `preflight` | `mode: live`, `bridgeConnected` and `pluginConnected` true, Aseprite 1.3.18.6, extension 0.8.2, `ready: true`, `switched: false` |
| tool list | 18 tools: preflight, sprite_info, sprite_manage, look, read_pixels, draw, select, transform, layer, frame, tag, cel, palette, recolor, validate, reference, export, tileset. **No `run_lua`.** |
| `sprite_manage new` (8x6) | id 6 |
| `sprite_info` keys | activeFrame, activeLayer, colorMode, filename, frameCount, frames, height, id, layers, modified, name, palette, tags, width |
| `draw` one pixel | `pixelsChanged: 1`, bounds 1x1 at (1,2); a requested `#ff0000` was snapped to the palette's `#000000` (deltaE 117.33): upstream snaps colours, so the broker must reject non-palette colours rather than rely on it |
| `read_pixels` | 8x6 grid, one changed pixel at (1,2), colours `#00000000`, `#000000` |
| `export` to `/work/inside.png` | success; file exists in the workspace |
| `export` to a host home path | reported success, but the file did **not** appear on the host: it landed in the sandbox's ephemeral root and vanished with it |
| `preflight mode=headless` | accepted by upstream (`switched: true`); failed only because no Aseprite binary is reachable (`ready: false`). **The broker must refuse the `mode` argument.** |
| host connect to 127.0.0.1:9932 | refused (`Errno 111`): the bridge port exists only in the private netns |
| owner `~/.config/aseprite` afterwards | no extensions dir created |

## Script-security dialog

Aseprite blocks an extension's scripts behind a security dialog until trusted. The bridge extension therefore does not
run on first launch. Trust is seeded only in the **sandbox's private** `aseprite.ini` as
`[script_access] sha1(<script path>) = 31`; the owner's `~/.config/aseprite` is never edited.

## Rollback / uninstall

Nothing was installed globally. To remove everything:

```
rm -rf ~/sonoranlab-work/phase3.5/runtime ~/sonoranlab-work/phase3.5/smoke
```

No agent config, `~/.config/aseprite` or system package was changed. No `pkill -f 'opt/aai/dist/cli.js'` is needed: the
PID namespace dies with the session.
