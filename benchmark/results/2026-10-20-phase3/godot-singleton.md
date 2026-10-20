# DYL-101: Godot 4.7.2 headless `Parameter "singleton" is null` (is_cmdline_mode)

Date 2026-10-19/20. Godot 4.7.2.stable.official.ed1daf0bf, Strix Halo box (32 threads, other jobs running: load
average 12-14 throughout). Nothing here touches the GPU, ComfyUI, containers or the game.

## Symptom

```
ERROR: Parameter "singleton" is null.
   at: is_cmdline_mode (editor/editor_node.cpp:6618)
```
then exit status -6 (SIGABRT). Seen once in `test_godot_imports_plays_and_matches` (the `--import` step); a rerun passed.

## Method

Repro script (scratchpad, not committed): builds the test's real fixture (`make_sheet` + `export` into a copy of
`tools/godot/spriteframes_check`) in a fresh temp dir per run, then runs the exact commands of the test:
`godot --headless --path P --import`, then `godot --headless --path P --fixed-fps 60 --script res://check_spriteframes.gd -- ...`.
Records returncode, signature match, seconds, stderr, stdout tail, and whether `.godot/imported/*.ctex` exists. Modes:
fresh (import + check), warm (import twice + check), import-only, `--editor --quit` (+ check), env stripping
(`DISPLAY`, `WAYLAND_DISPLAY`, `XDG_SESSION_TYPE` removed), CPU starvation (`taskset -c 0,1`), 1/2/3/4 concurrent instances.
Default env had `XDG_RUNTIME_DIR=/run/user/1000`, `WAYLAND_DISPLAY=wayland-0`, `DISPLAY=:0`; user data dir
`~/.local/share/godot` shared by every run (never isolated).

## Results

| variant | runs | `--import` crashes |
|---|---|---|
| fresh, sequential, normal load | 100 | 1 (run 35, 3.36 s vs ~2.4 s typical) |
| fresh, 2 concurrent, normal load | 100 | 0 |
| warm cache (second `--import` on an imported project) | 50 (100 imports) | 0 |
| import-only, 4 concurrent, normal load | 150 | 0 |
| import-only, no DISPLAY/WAYLAND_DISPLAY, sequential | 100 | 5 (all in the first 10 runs, i.e. a busy moment) |
| import-only, pinned to 2 CPUs, 3 concurrent | 100 | 29 |
| import-only, pinned to 2 CPUs, 3 concurrent (rerun) | 60 | 3 |
| `--editor --quit` (+ check), sequential | 50 | 0 |
| `--editor --quit` (+ check), pinned to 2 CPUs, 3 concurrent | 60 | 0 (and every following check passed, so the import happened) |

Every failure had the identical signature (rc -6, the two stderr lines, nothing else). The check step (`--script`)
never failed in any run. Unconstrained base rate for `--import`: about 1 in 300-400 runs on this box when it is
only moderately loaded; it climbs steeply with CPU contention (3-29% pinned to 2 cores, varying with what else ran).

## Where in startup

Not at startup before any work. In failing and passing runs the stdout is line-for-line identical (32 lines):
first scan, `reimport | toy.png`, `Finalizing Asset Import`, `Executing post-reimport operations`, `DONE`, then
`loading_editor_layout ... Loading docks ... DONE`. The imported `.ctex`/`.md5` exist in the failing runs too, so the
import itself finished. The abort comes after the last progress line, i.e. while the `--import` run is quitting and
the EditorNode is torn down: `EditorNode::is_cmdline_mode()` is called by something still alive while
`EditorNode::singleton` is already null. Longer wall time in failing runs (3.4-5.5 s vs 2.4-4.0 s) and the CPU-contention
dependence fit a shutdown-order race between the main thread and a background task or thread, not a content problem.
Not correlated with: concurrency as such (0/100 at 2 concurrent unconstrained), cache warmth (warm 0/100 imports),
display variables (the no-display failures cluster in time, not in the variable), or the user data dir.

I did not have the 4.7.2 source on the box, so the exact caller is not identified; this is the best evidence, not a
proven line-level root cause. It is an upstream Godot bug (the engine should not call `is_cmdline_mode` after freeing
EditorNode), reportable with the repro above.

## Fix

No invocation fix was adopted. `--editor --quit` avoided the crash in 110 runs (60 of them under the starvation that
broke `--import` 3-29% of the time), but it is a different exit path (same bug class could surface there), and I did not
verify that it surfaces import errors the way `--import` does. It is the first thing to try if the retry below proves
too leaky. The fix is therefore the bounded retry, in the Godot invocation helper only:

- `sonoranlab.pipeline.run_godot(godot, args, cwd=, step=, attempts=)` is used by `check_in_godot` (version, import,
  check) and by `tests/sonoranlab/test_export_godot.py`'s `godot()` helper.
- It re-runs only when `is_transient_singleton` holds: returncode -6 or 134, stderr contains both `Parameter "singleton" is null`
  and `is_cmdline_mode`, and no other ERROR-ish line (`ERROR|Parse Error|Failed|failed|Cannot|Invalid`) exists in stdout/stderr, and the
  checker JSON (if any) is not `"pass": false`. At most 2 retries, sleeping 1 s then 3 s.
- Every godot process is appended to `godot_check.godot_attempts` (`step`, `returncode`, `transient_signature`, `seconds`) in
  `manifest.json` and in the `run()` result. Only the final attempt of a step is judged, by the unchanged pass criteria
  (exit 0, no "ERROR" in the log, checker pass), so a retry cannot turn an import/validation/playback failure into a pass.
- Tests: `tests/sonoranlab/test_godot_retry.py` (fake `godot` on PATH; CPU only; no Godot needed in CI): transient twice then
  success (3 attempts recorded), exit 134, transient x3 -> failure with 3 attempts, six different failures (real import error,
  signature with exit 1, abort with other text, signature plus a content ERROR, plus SCRIPT ERROR, plus checker `pass:false`) ->
  1 attempt, and the same through `check_in_godot` including "recovered import, failing check still fails".

## After the fix (real Godot, through `run_godot`)

| variant | runs | godot processes | transient attempts | unrecovered failures |
|---|---|---|---|---|
| fresh import + check, sequential, normal load | 100 | 200 | 0 (the 1% did not occur) | 0 |
| fresh import + check, pinned to 2 CPUs, 3 concurrent | 100 | 203 | 3, all recovered on the first retry | 0 |

## Limits

- The retry hides, not removes, an upstream race: with a per-attempt rate p it leaves p^3 unrecovered (0.0001% at 1%; about
  2% at the worst starved rate measured, 29%). `godot_attempts` makes the rate visible in every manifest; a sustained rise means
  the box is starved or Godot changed.
- The signature match is deliberately narrow. A different Godot version with different wording, or the crash with extra
  ERROR lines, is not retried and fails as before.
- A retry re-runs on the same project dir (half-torn-down `.godot/` is reused; the import had already finished in all observed
  failures, so this was harmless in the data).
- Only 2 retries; a 3rd consecutive failure is reported as a failure with all 3 attempts listed.
