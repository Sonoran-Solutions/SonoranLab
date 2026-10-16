# ADR-0002 — ComfyUI is the inference host; SonoranLab lives outside it

**Status:** accepted (2026-10-16)

## Context

Every model in the plan has a ComfyUI path on Strix Halo via the kyuz0
toolbox. ComfyUI exposes `/prompt`, `/history`, `/interrupt`, and
memory-free endpoints. Writing our own inference loop would duplicate that
and lose every community node.

## Decision

- ComfyUI (inside the ROCm toolbox) is the only inference runtime.
- Workflows are committed as **API-format JSON** with a `params.yaml` sidecar
  naming patchable inputs. No UI graphs in the repo.
- The `sonoranlab` package is a separate Python project that talks to ComfyUI
  over HTTP. The deterministic core has no ComfyUI dependency.

## Consequences

- The deterministic core runs on any machine and is unit-testable.
- Generation adapters are small: patch params, POST, poll, download.
- We depend on the toolbox for environment stability; pin its tag.
- Interactive experimentation still happens in the ComfyUI UI; results are
  exported to API JSON before they become a track.
