# Phase 2 gate: one command, raw → validated → Godot (DYL-86), 2026-10-19

Evidence is text, numbers and hashes only. Job dirs, logs and screenshots stay private under
`~/sonoranlab-work/phase2/` (run id `20261019T162246`). Code: this branch's working tree on top of `37cbadc`
(DYL-79..85 merged); manifests record `git: 37cbadc…` because the DYL-86 changes were not committed yet.
Machine: Strix Halo box, CPU only (torch `2.14.1+cpu`, pixeloe 1.0.0, numpy 2.5.4, pillow 13.0.0, Python 3.13.15),
Godot 4.7.2.stable.official.ed1daf0bf. Every run went through `systemd-run --user --scope`, one at a time.

## 1. The one command (relay_sentinel, Phase 1 set E/relay_sentinel/2)

```
sonoranlab process ~/sonoranlab-work/benchmark/results/2026-10-18/E/relay_sentinel/2/set/raw \
  --profile profiles/twos-are-up.yaml --character relay_sentinel --anim idle \
  --stills ~/sonoranlab-work/benchmark/characters/2026-10-18/relay_sentinel \
  --out ~/sonoranlab-work/phase2/relay_sentinel-idle-20261019T162246 --godot-check
```

- Exit code **0**; wall 15.9 s (manifest `total_s` 14.1: ingest 0.1, per-frame chain 10.4 for 20 frames at scale 3,
  register + validate + contact 0.5, Godot check + export 2.9).
- Validator: `pass: true`, 0 failures, 7 warnings: `loop_closure_ratio` north 1.31, south 1.39, north-east 1.30
  (> 1.29) and `hover_bob` north 0.39, south 0.60, east 0.42, north-east 0.34 px (< 1.5). foot_slide is not computed
  for bbox_bottom (ADR-0008 F2).
- Godot check: import exit 0, `check_spriteframes.gd` exit 0, `pass: true`, 8 animations checked (`idle_<direction>`,
  including the 3 mirrored west rows), 5 fps, loop, 10 frame changes in 120 fixed steps each. The checked PNG/.tres
  hash the same as the exported ones.
- stdout ends with `godot: 4.7.2.stable.official.ed1daf0bf: import + SpriteFrames check passed (8 animations)`.

| output | sha256 |
|---|---|
| report.json | 125fac26d4f7d68c31059121ffb5d78b7cfbfdac217438ca64be5488005bc3b5 |
| contact.png | 9dff5a51ecf9454403a5346d5b25a1c97bd293b8fab0de9569b517b3efc5632c |
| export/relay_sentinel/relay_sentinel.png | 056784533978940029dde38911c9b7b0ab5c36accf0f1f3e244a5504fc5efa71 |
| export/relay_sentinel/relay_sentinel_frames.tres | 613de77e9776590abba41eb04cadcdeb544c2e1870cf158b9742bf6db786f92b |
| export/relay_sentinel/relay_sentinel.sonoranlab.json | c8bf27c77c210e32b53a81bbc662c22f84b2def78df6253db2a274e52e5e6613 |
| stage 01_alpha (20 files, sha256 of the sorted path+hash list) | d9544547d0158fcd3f9723af35b55535f29ad7d2797f2f968618d817c4a6b3cf |
| stage 02_pixelize | 8f6d444801027951a67e3be4026454c56f4150e1c830705555caf8bb6d720076 |
| stage 03_palette | a0da672ea9354c4c93111bec2d75d9d248b790b6e7fc6efa60324b9052f99e0c |
| stage 04_clean | da1adf80e736383101732aa4dc9522248c7725e0caeb16f3af36c49e3852b3f6 |
| stage 05_registered | cd23071670554ece9222bb3c895928e9fc66bb5d9305c8eb6b4eae6849af0880 |
| whole `manifest.outputs` map (105 files, sha256 of its sorted JSON) | 5df25eed562d64746c63ca14b5c19cba875d2b12b84dd1dae21ed13ca28f65cf |

Processing-v1 parity: the 20 `05_registered` PNGs are byte-identical to Phase 1's `set/processed/processed/*.png`.

## 2. All 12 Phase 1 track E sets, same command (`--stills` per character, `--godot-check`)

Job dirs `~/sonoranlab-work/phase2/sweep-20261019T162246/<character>-<anim>-<seed>`.

| set | exit | validator | failures by rule | warnings by rule | export | Phase 1 (process.py) |
|---|---|---|---|---|---|---|
| samurai/1 run | 1 | FAIL | bbox_drift 2, foot_slide 1 | opaque_area_cv 1 | none | FAIL, same 3 (rule, frame) |
| samurai/2 run | 1 | FAIL | bbox_drift 5, foot_slide 1 | loop_closure_ratio 1 | none | FAIL, same 6 |
| samurai/3 run | 1 | FAIL | bbox_drift 6, foot_slide 1 | opaque_area_cv 1 | none | FAIL, same 7 |
| samurai/4 run | 1 | FAIL | bbox_drift 5, foot_slide 1 | loop_closure_ratio 1, opaque_area_cv 1 | none | FAIL, same 6 |
| oathguard/1 run | 1 | FAIL | bbox_drift 6, foot_slide 1 | loop_closure_ratio 3 | none | FAIL, same 7 |
| oathguard/2 run | 1 | FAIL | bbox_drift 7, foot_slide 3 | – | none | FAIL, same 10 |
| oathguard/3 run | 1 | FAIL | bbox_drift 8, foot_slide 1 | loop_closure_ratio 2 | none | FAIL, same 9 |
| oathguard/4 run | 1 | FAIL | bbox_drift 5, foot_slide 1 | loop_closure_ratio 1 | none | FAIL, same 6 |
| relay_sentinel/1 idle | 0 | PASS | – | loop_closure_ratio 3, hover_bob 5, hover_x_drift 1 (east 6 px) | yes, Godot check pass | FAIL (foot_slide east/1, east/2) |
| relay_sentinel/2 idle | 0 | PASS | – | loop_closure_ratio 3, hover_bob 4 | yes, Godot check pass | PASS |
| relay_sentinel/3 idle | 0 | PASS | – | loop_closure_ratio 3, hover_bob 3 | yes, Godot check pass | FAIL (foot_slide east/0, east/1) |
| relay_sentinel/4 idle | 0 | PASS | – | loop_closure_ratio 4, hover_bob 4, hover_x_drift 1 (east 3.5 px) | yes, Godot check pass | FAIL (foot_slide east/1, east/2) |

- The 8 humanoid sets fail exactly as in Phase 1: the same (rule, direction/frame) failures as each set's Phase 1
  `report.json`, nothing added or removed. No export dir exists for any of them; stderr names each failing frame and
  the report/contact paths. Limits used: samurai 30/16, oathguard 20/8 (profile `characters:` rows).
- relay_sentinel 1, 3 and 4 now pass because ADR-0008 F2 drops foot_slide for anchor_mode bbox_bottom; their only
  Phase 1 failures were foot_slide on east frames. In their place the hover warnings fire: `hover_bob` (< 1.5 px
  centroid bob) in 3-5 of 5 directions in every set, `hover_x_drift` (> 3 px) on east in sets 1 (6 px) and 4 (3.5 px).
- Every one of the 12 sets' `05_registered` frames (35 samurai, 30 oathguard, 20 relay_sentinel per set) is
  byte-identical to Phase 1's processed frames: processing-v1 parity holds on all of them.
- Wall time per set: samurai 22.4-22.7 s, oathguard 18.7-20.1 s, relay_sentinel 15.7-15.9 s (incl. Godot check).

## 3. Determinism

`relay_sentinel/2` was processed four times into four new job dirs: the one command (§1), a rerun
(`…-20261019T162246-rerun`), the offline run (§4) and the sweep (§2). All four `manifest.json` `outputs` maps are
identical (105 entries: 5 stages x 20 frames, report.json, contact.png, the three export files; the aggregate hash in
§1), and every listed hash was recomputed from disk. `report.json` is byte-identical (it carries no timings or job
paths). The only manifest fields that differ between runs are `created_at` and `timings`. A rehearsal run of the same
command earlier the same day (`relay_sentinel-idle-20261019T162117`) produced the same export PNG and .tres hashes.

## 4. Offline

The §1 command was run again as `systemd-run --user --scope -q unshare -rn sonoranlab process … --godot-check`
(new user + network namespace with only a down loopback; a socket connect there fails with "Network is unreachable").
Exit 0, PASS, Godot check pass, wall 16.0 s, all output hashes identical to §1. Nothing in the chain uses the
network, ComfyUI or a GPU (torch is the CPU wheel, used only inside PixelOE).

## 5. Raw inputs never change

- sha256 of every Phase 1 raw file (340 files under `E/*/*/set/raw/`) before and after all runs: listing hash
  `a76199b92f3bc95a7d54eab148e5fb15e0cd99179f9b86d03a0df8b97a095b03` both times. The relay_sentinel stills (16 files):
  `4b7288af4d481b02326d688c0aed090cb6fac4dffd3e432a3f300496e7b66f2d` both times.
- `verify_raw` is empty for all 15 jobs (the pipeline runs it last and would exit 2 otherwise). Each job's `raw/` holds
  read-only byte copies (frames, stills, upstream `selection.json`) and the chain reads only those.
- The game checkout (`~/Projects/TwosAreUp`) was only read (the palette): no file in it is newer than the start of
  the runs (`find -newer` lists only the `.git` directory mtime, bumped by a `git status` check's transient lock).

## 6. Visual proof in Godot

`tools/godot/spriteframes_check/render.sh` on an imported copy of the §1 export, animations `idle_east`, `idle_west`
(mirrored), `idle_south`, `idle_north-west` (mirrored), 4 frames each; 16 PNGs in the job dir's `render/`. Each
capture is pixel-exact against the exported sheet cell at 1x and at 4x (nearest; no blended colours), the sprite
frame index matches the capture index, and live playback advances at 5 fps and loops (3,0,1,2 sequence). Looked at by
eye: crisp 1x and 4x standee, the west row is the east row mirrored, the hover pose reads, nothing cropped.
The first attempt showed the DYL-85 render script drew into a fixed 160x96 window (fine for its 16 px fixture, cropped
and overlapped for a 136 px cell); it now renders into a SubViewport sized from the frame (docs/09).

## 7. Re-run on the committed tree (orchestrator, after the review fixes)

After the independent review fixes (commit `cca758c`), the orchestrator re-ran §1's command and samurai/1 on the
committed tree (manifests record `git: cca758cd…`):
- relay_sentinel/2 `--godot-check`: exit 0, Godot check pass; `report.json` `125fac26…`, sheet `05678453…`,
  `.tres` `613de77e…`, identical to §1.
- samurai/1 run: exit 1, no `export/` dir; report, contact sheet and manifest written.
An earlier orchestrator re-run of §1 on the uncommitted tree (job `orch-verify-…`) also matched, and oathguard/2
exited 1 with no export and stderr naming each failing frame with its moved edge and core delta.
