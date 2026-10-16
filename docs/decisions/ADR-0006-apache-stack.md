# ADR-0006 — Apache-2.0-only default model stack

**Status:** accepted (2026-10-16)

## Context

The outputs are shipped game assets. Qwen-Image-Edit-2511, InstantX
ControlNet-Union, FLUX.2-klein-base-4B, Wan 2.2, and Qwen-Image-Layered are
Apache-2.0. FLUX.2-klein-9B is under the FLUX Non-Commercial License; Qwen
Image 2.1 is under the Qwen Research License; some sprite LoRAs were trained
on CC-BY templates.

## Decision

Only models and adapters with Apache-2.0 / MIT (or equivalently permissive,
commercial-OK) licenses may be in the **default** stack. Anything else is an
*experiment* whose outputs do not ship until promoted with a recorded
licensing decision in `docs/04-models-and-licensing.md`.

## Consequences

- The 9B klein line and its LoRAs are out regardless of quality.
- Training-data provenance of community LoRAs is checked before their
  outputs ship; the safer path for production style is a project LoRA trained
  on our own approved art.
- Model selection stays swappable so a license change on one model does not
  take the pipeline down.
