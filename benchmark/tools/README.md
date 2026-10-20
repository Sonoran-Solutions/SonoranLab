# benchmark/tools

Disposable Phase 1 tooling (not the Phase 2 package). Outputs go under `~/sonoranlab-work/benchmark/`, never in this
public repo. The human usable-rubric and the frozen chain parameters live in `benchmark/characters/2026-10-18.yaml`.

```
UV="uv run --no-project --with pillow --with numpy --with pixeloe==1.0.0"
```

- `extract_refs.py`: pulls stills, 4x model inputs and ground-truth clips out of the private game checkout.
- `process.py run RAW OUT`: per-frame chain on `RAW/<direction>_<i>.png` (HD, RGBA or RGB on `#ff00ff`):
  alpha extract, PixelOE k-centroid ÷scale, palette enforce (off-palette counted before the snap), alpha clean
  (threshold 128, 1 px key fringe), per-clip registration (lowest row of the clip to cell-4, median bbox centre to
  cell/2), validate. Writes `OUT/processed/`, `sheet.png` (game layout, west mirrored from east), `report.json`
  (docs/08 shape) and `contact.png`. RAW is only read.

  ```
  $UV python -I benchmark/tools/process.py run RAW OUT --character samurai --clip run --cell 184 --frames 7 \
    --max-foot-slide 30 --max-bbox-drift 16 --loop-ratio-warn 1.29 \
    --stills ~/sonoranlab-work/benchmark/characters/2026-10-18/samurai
  ```
  `--config benchmark/characters/2026-10-18-processing-v1.yaml` (`version: processing-v1`) supplies the frozen
  defaults: downscaler, thickness, despill, key_tol, colour cap, validator warn limits, and per character the anchor
  mode and foot-slide / bbox-drift gates; any explicit flag overrides it, an unknown `version` is refused. Scale is
  per track and still a flag (C/E `--scale 3`, A/B `--scale 4`). Without `--config` the old flag defaults apply.
  `--anchor-mode bbox_bottom` for relay_sentinel. `--downscaler k_centroid,contrast,nearest` runs the same frames
  through each downscaler into `OUT/<name>/` and writes `OUT/compare.json` (DYL-77). `--despill N` removes magenta key spill within N HD px of the matte edge (Track C/E BiRefNet mattes; interior
  colours untouched). `--thickness` is PixelOE
  outline expansion (default 2, as the Phase 0 node). `test_nearest` is for tests only.
  `report.json` gets a `provenance` block: `config` ({path, sha256, version} or null), `inputs` (raw frame name ->
  sha256), `outputs` (processed frame name and `sheet.png` -> sha256), `process_seconds` (wall time of the run),
  `tool_sha256` (process.py) and, if `RAW`'s parent holds a `selection.json`, `selection_sha256` and `selection_set`.
  `params` lists every effective parameter, including `despill_px`, `max_colours` and `alpha_threshold`.
- `process.py calibrate INPUTS OUT.json`: the same validator on the shipped ground-truth clips (registration only,
  they are final pixels) -> observed maxima and proposed thresholds (the yaml `thresholds_addendum`).

  ```
  $UV python -I benchmark/tools/process.py calibrate ~/sonoranlab-work/benchmark/characters/2026-10-18 \
    ~/sonoranlab-work/benchmark/calibration-2026-10-18.json
  ```

- `validator_study.py GAME WORK OUT.json [--tables OUT.md]`: DYL-83 study of `foot_slide` / `bbox_drift` (read-only):
  every shipped run/idle clip in the game and the bound Track E sets, with per-edge, core-silhouette, foot-vs-body,
  planted-foot and hover metrics. Results: `benchmark/results/2026-10-19-phase2/validator-study.md`; proposal ADR-0008.

## Sets, scoring and review

A scored set lives in a directory SET (`<results>/E/samurai/2/set/`; for B `<results>/B/<char>/<seed>/<variant>/set/`)
holding `selection.json` (schema `sonoranlab.selection/1`, written by `assemble_set.py`: every attempt with
job, dir, part, retry, exit_code, wall_s, manifest_sha256 and `selected`, plus `selected`, `missing_parts`,
`raw_frames`) and, after `process.py run SET/raw SET/processed --config ...`, `SET/processed/report.json`.

- `score.py RESULTS_DATE_DIR OUT.csv [--review review.json]`: one row per ATTEMPTED set (failed-only sets included),
  from every `manifest.json`. `selection.json` says which attempts count; the report is used only if
  `provenance.selection_sha256` matches that file. No `selection.json` = "not assembled": no validator verdict, no
  processed hash. Columns: see `benchmark/README.md`. `gen_minutes` is the selected attempts' time,
  `attempt_minutes_total` all attempts (failed included); `usable` = human `yes` AND validator `yes` ("no" if either is
  no, blank if unknown); empty means not measured, never 0.
- `review_page.py RESULTS_DATE_DIR OUT_DIR`: local review page over the SET dirs. It shows each sheet's short sha256
  and exports it as `artifact_sha256` in `review.json`; `score.py` then marks `review_binding` bound (hash equals
  `processed_sha256`), stale (differs: the human columns are copied, noted as "review was of a different artifact",
  and the verdict does not count towards `usable`) or unbound (entry without hash).

Tests: `$UV --with pyyaml --with pytest python -m pytest -q tests/benchmark` (synthetic clips and JSON, no game pixels).
