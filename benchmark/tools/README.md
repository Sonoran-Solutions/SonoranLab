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
  `--anchor-mode bbox_bottom` for relay_sentinel. `--downscaler k_centroid,contrast,nearest` runs the same frames
  through each downscaler into `OUT/<name>/` and writes `OUT/compare.json` (DYL-77). `--thickness` is PixelOE
  outline expansion (default 2, as the Phase 0 node). `test_nearest` is for tests only.
- `process.py calibrate INPUTS OUT.json`: the same validator on the shipped ground-truth clips (registration only,
  they are final pixels) -> observed maxima and proposed thresholds (the yaml `thresholds_addendum`).

  ```
  $UV python -I benchmark/tools/process.py calibrate ~/sonoranlab-work/benchmark/characters/2026-10-18 \
    ~/sonoranlab-work/benchmark/calibration-2026-10-18.json
  ```

Tests: `$UV --with pytest python -m pytest -q tests/benchmark` (synthetic 16 px clip, no game pixels).
