"""Common generation-adapter contract (Phase 3, PLAN.md: `generate(request) -> RawResult`).

Plain data only: no HTTP and no ComfyUI imports, so sweeps, `animate` and tests can use the types with a fake adapter.
A request is one independent generation: one direction of one clip for one seed (ADR-0004: Track E generates each
direction as its own clip; ADR-0007: the west side is mirrored later by the deterministic pipeline, never generated).

A `RawResult` is written next to its raw outputs as `result.json`, and with the request it is enough to reproduce the
run: workflow source + patched hashes, every patched parameter, seed, model file hashes, input hashes.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

Track = Literal["E", "C"]
Status = Literal["ok", "failed"]

# Phase 1 found magenta induces a green cast on metal in Wan output; grey removed it (ADR-0004). Grey is the default.
DEFAULT_BACKGROUND = "#808080"


@dataclass(frozen=True)
class GenerateRequest:
    track: Track
    character: str
    anim: str
    direction: str  # a profile `directions.generate` name, e.g. "north-east"
    source_image: Path  # E: approved still or posed first frame; C: character reference (image2)
    prompt: str
    seed: int
    out_dir: Path  # this attempt's own directory; must not exist (raw outputs are never overwritten)
    settings: dict[str, Any] = field(default_factory=dict)  # workflow params by params.yaml name, recorded verbatim
    background: str = DEFAULT_BACKGROUND
    control_image: Path | None = None  # C: Track D OpenPose render (image1, native keypoints)
    workflow: str | None = None  # repo-relative workflow override; None = the adapter's default
    # E: integer scale of the still on the unchanged canvas (profile `start_scale`); None = the default x3 placement
    start_scale: int | None = None

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        for k in ("source_image", "out_dir", "control_image"):
            d[k] = None if d[k] is None else str(d[k])
        if d["start_scale"] is None:  # added later: requests without it keep their exact JSON
            del d["start_scale"]
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> GenerateRequest:
        d = dict(d)
        for k in ("source_image", "out_dir", "control_image"):
            d[k] = None if d.get(k) is None else Path(d[k])
        return cls(**d)


@dataclass
class RawResult:
    request: GenerateRequest
    status: Status
    frames: list[Path] = field(default_factory=list)  # RGBA frames in time order (E: the whole clip; C: the pose)
    raw_frames: list[Path] = field(default_factory=list)  # model RGB frames before alpha extraction
    prompt_id: str | None = None
    workflow_source: str | None = None
    workflow_source_sha256: str | None = None
    workflow_patched_sha256: str | None = None
    params: dict[str, Any] = field(default_factory=dict)  # every patched param, inputs included
    inputs: dict[str, str] = field(default_factory=dict)  # param name -> sha256 of the staged input file
    models: dict[str, str] = field(default_factory=dict)  # model file (models.tsv dest) -> sha256
    # verified file inventory per role, e.g. {"birefnet": {name, files, node, verified, reason}} (comfy.provenance)
    model_provenance: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)  # output path relative to out_dir -> sha256
    timing: dict[str, float] = field(default_factory=dict)  # seconds: queue_wait, exec, wall, ...
    error: dict[str, Any] | None = None  # structured client/adapter error: code, stage, message, retryable, ...
    checks: dict[str, Any] = field(default_factory=dict)  # adapter post-checks, e.g. alpha fringe counts per frame

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["request"] = self.request.to_json()
        d["frames"] = [str(p) for p in self.frames]
        d["raw_frames"] = [str(p) for p in self.raw_frames]
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RawResult:
        d = dict(d)
        d["request"] = GenerateRequest.from_json(d["request"])
        d["frames"] = [Path(p) for p in d.get("frames", [])]
        d["raw_frames"] = [Path(p) for p in d.get("raw_frames", [])]
        return cls(**d)

    def write(self) -> Path:
        """Exclusively create `<out_dir>/result.json` (an attempt records its result once)."""
        path = self.request.out_dir / "result.json"
        with open(path, "x") as f:
            f.write(json.dumps(self.to_json(), indent=2) + "\n")
        return path


class Adapter(Protocol):
    track: Track

    def generate(self, request: GenerateRequest) -> RawResult:
        """Run one request. Never raises for a generation failure: returns status "failed" with `error` set and every
        output that did arrive kept in `out_dir`. Raises only for a malformed request (before writing anything)."""
        ...
