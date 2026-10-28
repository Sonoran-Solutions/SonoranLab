# New-class references (DYL-128, ADR-0011)

The entrance to a Phase 5 class is a class request (`sonoranlab.class_request/1`) plus eight independent,
final-resolution stills. This runbook covers the checks before any generation: intake, reference preparation,
the profile row and registration. Nothing here approves anything; approvals are owner-only.

## Class request

YAML or JSON, strict (unknown keys are errors), at most 256 KiB:

```yaml
schema: sonoranlab.class_request/1
project: twos-are-up
character: gun            # new stable id, [a-z0-9_]+
display_name: Gun
status: draft             # free text
height_m: 1.8             # game standing height; refprep target = round(height_m x density_px_per_m)
visual_role: ...
silhouette: ...
invariants: [rifle in the right hand]
palette_notes: ...        # optional
clips: [run, idle]        # profile animation names, priority order; Phase 5 supports track_default E only
source_design: ...        # optional, informational
gameplay: ...             # optional, informational
references:               # optional
  stills_dir: stills      # CLI only, relative to the request file; the MCP tool never follows it
  provenance: [generator, terms, prompt/output hashes]
  approved_by: null
owner_decisions: [O4 pending]
```

All text is untrusted data: at most 4096 characters per field (256 for display_name), 64 list items, no control
or bidi characters except newline and tab. It is never interpreted.

## Check

```sh
uv run sonoranlab classreq check REQUEST --profile profiles/twos-are-up.yaml \
    [--stills DIR] [--state STATE] [--out check.json] [--contact contact.png]
```

Pure read; `--out`/`--contact` must not exist. Exit 0 `ready`, 1 any other status, 2 usage/config. The MCP tool
`check_class_request` returns the same JSON (docs/06).

| Code | Blocks | Meaning |
|---|---|---|
| `request_invalid` | invalid | schema, type, bound or control-character problem |
| `project_mismatch` | invalid | request project is not the profile id |
| `id_collides_game_sheet` | invalid | id is a shipped game sheet (`references.style_refs`) |
| `id_near_collision` | invalid | id equals another row/sheet name except for `_` |
| `registry_collision` | invalid | STATE has registered references for the id but the profile has no row (retired or other character) |
| `unknown_clip` / `unsupported_clip` | invalid | clip not in the profile / not Track E |
| `missing_still`, `still_unreadable` | needs_reference_preparation | one per direction |
| `still_size` | needs_reference_preparation | no row: stills must share one square cell, multiple of 8, 120..288 |
| `still_canvas`, `still_empty`, `still_palette`, `still_fringe`, `still_semi_alpha`, `still_colours`, `still_touches_edge` | needs_reference_preparation | the validator's still gates (docs/08), unchanged thresholds |
| `still_foot_row` | needs_reference_preparation | lowest opaque row is not `cell - 4` |
| `baked_white_border` | needs_reference_preparation | opaque `#ffffff` on the silhouette's outer edge |
| `mirrored_independent_still` | needs_reference_preparation | a west still is the exact mirror of its east source |
| `needs_profile_row` | needs_profile_row | no `characters.<id>` row; `proposed_row` holds a snippet when the cell is known |

Concept art alone (no stills) yields `needs_reference_preparation` with all eight `missing_still` findings. Nothing
fabricates, mirrors or generates a missing view.

## Reference preparation (owner step, CLI only)

Owner-directed external HD turnaround art (ADR-0011 route b) becomes candidate stills with pixel math only:

```sh
uv run sonoranlab refprep --profile profiles/twos-are-up.yaml --request REQUEST \
    --turnaround turnaround.png --grid 4x2 [--order north,north-west,...] \
    [--view south-west=sw_regenerated.png ...] --out DIR [--cell N] [--bg-key '#ff00ff']
```

- Cells are split row-major in `--order` (default: profile `directions.order`). `--view DIRECTION=IMG` replaces
  one view with a separate HD image (a regenerated wrong-facing view); `--turnaround` may be omitted only when all
  eight are given as views. Overrides are recorded with their sha256.
- Matte: chroma key with the profile `key_tol` (tolerates near-#ff00ff noise) + despill; matte islands smaller
  than 0.5% of the largest component are dropped and listed. Opaque pixels still within 2 x key_tol of the key
  after matting are reported as a `residual_key` warning: inspect `DIR/crops/`.
- One common scale for the turnaround views (median of target height / view height); an `--view` override comes
  from another canvas, so its own standing height is normalised to the target (with only overrides, their median
  is the common scale). refprep.json records each view's source, scale and scale basis. Views more than 4% off the
  target height are warned. Lanczos to gen_scale x the target size, then the processing-v1 chain (PixelOE
  k-centroid, palette lock without dither, alpha threshold, fringe clean, colour cap), then foot registration on
  `cell - 4`, centred on `cell / 2`.
- Cell: `--cell`, else the profile row, else the smallest multiple of 8 that fits (>= 120, error above 288).
- A blank grid cell is an error naming the direction. Views are never mirrored or synthesised.
- `DIR` must not exist. Outputs: `still_<direction>.png`, `crops/<direction>_hd.png`, `contact.png`,
  `refprep.json` (input/request/profile hashes, all parameters, per-view measurements, dropped components,
  warnings, output hashes, the embedded `classreq check` result and elapsed preparation time).

The outputs are **candidates**: they carry no approval.

## Profile row

```sh
uv run sonoranlab classreq propose-row REQUEST --profile profiles/twos-are-up.yaml --cell 144 [--out row.yaml]
```

Prints a `characters.<id>` snippet (cell, `anchor_mode: foot`, the profile's `validator:` default limits copied
explicitly, a comment with the request sha256). It never edits the profile: the row goes through owner review and
a PR before any campaign uses it.

## Registration and approval; changed references

`register_character` copies the eight stills into immutable STATE references (initially unapproved); the owner
approves exact hashes with `sonoranlab registry approve-references`. A changed still set is a **new registration
with new reference hashes and a new `ref_set_id`**: it starts unapproved, and the earlier approval stays bound to
the earlier set. Approval is never transferred (tested in
`tests/sonoranlab/classreq/test_classreq_registry.py`). Per ADR-0011 a reference change is a new campaign revision.
