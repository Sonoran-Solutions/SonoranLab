# DYL-132: optional live repair lifecycle (2026-10-28/29 UTC). **PASS** on a thief copy

## Attempt 1: pyromancer parents (2026-10-28): STOPPED at admission
The owner authorized "Yes, one bounded edit" on a Phase 4 pyromancer copy. Both candidate parents were refused by
`import_job`: their manifests bind paths in removed or out-of-root worktrees. No manifest was edited. Step 8 (with the
runtime unavailable, the tools are absent and `prepare_repair` returns `Unknown or disabled tool`) passed.

## Attempt 2: approved Phase 5 thief job (2026-10-29): PASS end to end
The owner agreed with the senior reviewer's answer: "Use a disposable copy of an approved Phase 5 thief job. One tiny
authorized edit is sufficient". The parent was a `cp -a` copy of thief run `job-20261028T074425Z-9fa9090d`.

| Step | Result |
|---|---|
| 1 copy; originals and copies hashed before, during and after | PASS (identical) |
| 2 capabilities (editor available), `import_job` (final job), `prepare_repair`, `inspect_repair` | PASS once the shared profile equivalence was extended to import, prepare and finalize (PR #67; one gun-row edit had made every older job unimportable) |
| 3 owner authorization: `draw.pixels` only, east run frame 2, a 16-px rect, 1 iteration | PASS |
| 4 one contained `codex exec -m gpt-6.1-sol` editor (DYL-110 overrides; MCP=NONE proven three ways; no Astra) | PASS on one recovery retry (the editor display died before the first op in try 1, with no pixel changed) |
| 5 checkpoint + `inspect_repair_diff` | PASS: exactly 6 px changed, all inside the rect; 0 unauthorized/alpha/palette problems |
| 6 `finalize_repair` under the host heavy lock | PASS: job `job-20261029T121253Z-8c11708d`; validator, export and Godot all pass |
| 7 verification | PASS: only `east_2` changed (the 6 px, mirrored once in the west export row); all 8 stills unchanged; lineage hashes match; child `import-8c775c1222e5c1820358ac1c` has review none and production_usable false, so nothing carried over |

The coordinator independently re-checked 30 registered frames: the only change is `east_2` at
(52,70) (53,70) (54,70) (51,71) (52,71) (54,71), and the child's review state is none.

Findings recorded for follow-up:
- `REPAIR_ROOT` plus `session/broker.sock` must fit a 107-byte socket path;
- `repair session start --detach` does not pass `SONORANLAB_*` to its unit, yet still prints ok;
- relay status polling journals about 8 requests/s (14 MB in 6 min).

The child awaits the owner's review. Private evidence: `~/sonoranlab-work/phase5/dyl132/thief/`.
