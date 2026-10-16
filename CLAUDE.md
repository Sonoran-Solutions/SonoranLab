# CLAUDE.md — conventions for agents working in SonoranLab

Read `README.md` and `PLAN.md` first. Read `docs/02-research-findings.md`
before proposing a different model, node pack, or tool; most "obvious" swaps
were already evaluated and several of them were wrong.

## Non-negotiables

1. **Pixel math lives in code, never in a prompt.** Downscale, palette lock,
   grid snap, alpha cleanup, frame slicing, foot anchoring, sheet layout. If a
   workflow "asks the model" to keep the palette or the canvas, that is a bug.
2. **Raw outputs are never overwritten.** Every generation writes to
   `artifacts/<job>/raw/` and every processed result to
   `artifacts/<job>/processed/`. Cleanup routines are allowed to be wrong; they
   are not allowed to be destructive.
3. **Foot anchor, not center of mass.** Frame registration aligns on the foot
   line / rig anchor from the project profile. A sword swing moves the center
   of mass; it does not move the feet.
4. **Per-frame processing by default.** Outline expansion and quantization run
   on sliced frames, not on the whole sheet, so a thick outline never bleeds
   across cell boundaries.
5. **Offline is an acceptance test.** After weights are downloaded, the full
   generate → validate → export path must pass with networking disabled. No
   silent cloud fallback, ever.
6. **ComfyUI workflows are API-format JSON** (what `/prompt` accepts), stored
   under `workflows/`, parameterized by patching named inputs. Do not commit UI
   graphs. Do not hand-build graphs at runtime.
7. **The orchestrator gets tools, not graphs.** The MCP surface in
   `docs/06-tool-surface.md` is the only thing an agent calls. Adding a tool
   means adding a validator and a structured result, not just a function.
8. **License gate.** No model, LoRA, or dataset enters the default stack unless
   its license is recorded in `docs/04-models-and-licensing.md` and is
   compatible with shipping commercial game assets.

## Language and tooling

- Python 3.11+, managed with `uv`. Pillow + NumPy for pixel work; `pixeloe`
  for pixelization; the official `mcp` Python SDK for the server.
- Tests: `pytest`. Pixel-processing functions get golden-image tests against
  tiny synthetic sheets in `tests/fixtures/`. A change that alters a golden
  image must say why in the commit.
- Formatting: `ruff format` + `ruff check`. Type hints on all public functions.
- CLI first, MCP second. Every tool in the MCP server is a thin wrapper over a
  CLI subcommand (`sonoranlab <verb>`), so everything is testable without an
  agent in the loop.

## Working style

- Prefer small, verifiable steps. The plan's phases have exit criteria; do not
  start Phase N+1 work while Phase N's exit criterion is unmet.
- When a benchmark result contradicts a doc, update the doc in the same change
  and add a line to `docs/decisions/`.
- Hardware numbers (seconds per frame, peak memory) go in
  `benchmark/results/` as dated entries, never hard-coded into docs.
- Do not install into the system Python on the Strix Halo box. The ROCm
  toolbox is its own environment; the SonoranLab package talks to ComfyUI over
  HTTP and does not need to live inside it.

## Things that are deliberately out of scope for v1

- A review/edit UI. Pixelorama or Aseprite handle manual touch-ups.
- Tilesets.
- Training new base models. (Training a project LoRA is in scope, later.)
- Running anything on the RTX 3080 that the Strix Halo box can't also run.
