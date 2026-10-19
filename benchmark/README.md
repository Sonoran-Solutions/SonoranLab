# Phase 1 benchmark

Protocol: `docs/05-pipeline-tracks.md`. Decision goes in
`docs/decisions/ADR-0004-lead-track.md`.

## Storage (ADR-0007)

This repo is public and the Twos Are Up art is private. Raw/processed frames, contact sheets and reference images
go under `~/sonoranlab-work/benchmark/results/<date>/<track>/<char>/<seed>/`. Only `manifest.json`, `report.json`,
`notes.md`, `scoring.csv` and output hashes are committed. The frozen inputs are in
`benchmark/characters/2026-10-18.yaml`.

## Layout

```
benchmark/
  README.md
  scoring.csv                  one row per attempted (track, character, seed[, variant])
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
| character | 1 samurai (humanoid+weapon), 2 oathguard (humanoid+shield), 3 relay_sentinel (non-humanoid) |
| seed | |
| variant | B checkpoint (ck2750, ck3000); empty elsewhere |
| attempts | every generation attempt of the set, failed included |
| failed_attempts | attempts with a non-zero exit code |
| gen_minutes | wall time of the SELECTED attempts (selection.json); empty if none succeeded |
| attempt_minutes_total | wall time of ALL attempts, failed included |
| process_seconds | deterministic chain, from the report's provenance |
| cleanup_minutes | human time to make it usable (0 if none needed; empty = not measured) |
| human_usable | yes/no, the review's verdict at 1x in engine |
| validator_pass | yes/no from the report bound to selection.json; empty = not assembled/processed |
| usable | frozen rubric: yes only if human_usable and validator_pass are both yes; no if either is no; empty if unknown |
| frames_fixed | count of frames hand-edited or regenerated |
| back_row_accessories_kept | e.g. "plume:yes,shield:no" |
| foot_slide_max_px | from report.cycle |
| off_palette_before | sum over frames |
| peak_mem_gb | |
| processed_sha256 | sha256 of the processed sheet.png (provenance) |
| review_binding | bound / stale / unbound / empty: whether the review saw this processed_sha256 |
| notes | free text |

`headline_minutes = gen_minutes + process_seconds/60 + cleanup_minutes`; `attempt_minutes_total` is the honest cost of getting there.

## Rules

- Keep every output, including the ugly ones. The failure modes are the data.
- Same profile, same chain parameters for every row within a date. If you
  change a chain parameter, that's a new date folder.
- Fill `notes.md` while it's fresh: what you'd try next, what obviously
  broke.
