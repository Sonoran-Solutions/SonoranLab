# Phase 1 benchmark

Protocol: `docs/05-pipeline-tracks.md`. Decision goes in
`docs/decisions/ADR-0004-lead-track.md`.

## Layout

```
benchmark/
  README.md
  scoring.csv                  one row per (track, character, seed)
  characters/                  the three reference characters (refs + notes)
  results/
    phase0-env.md              toolbox tag, flags, model SHAs, first-run timings
    <YYYY-MM-DD>/<track>/<character>/<seed>/
      raw/  processed/  report.json  contact.png  manifest.json  notes.md
```

## Scoring columns (`scoring.csv`)

| column | meaning |
|---|---|
| date | |
| track | A, B, C, D, E |
| character | 1 (humanoid+weapon), 2 (humanoid+big accessory), 3 (non-humanoid) |
| seed | |
| gen_minutes | wall time from submit to raw frames |
| process_seconds | deterministic chain |
| cleanup_minutes | human time to make it usable (0 if none needed) |
| usable | yes/no — validator pass AND reads correctly at 1× in engine |
| frames_fixed | count of frames hand-edited or regenerated |
| back_row_accessories_kept | e.g. "plume:yes,shield:no" |
| foot_slide_max_px | from report.cycle |
| off_palette_before | sum over frames |
| peak_mem_gb | |
| notes | free text |

`headline_minutes = gen_minutes + process_seconds/60 + cleanup_minutes`.

## Rules

- Keep every output, including the ugly ones. The failure modes are the data.
- Same profile, same chain parameters for every row within a date. If you
  change a chain parameter, that's a new date folder.
- Fill `notes.md` while it's fresh: what you'd try next, what obviously
  broke.
