"""Track E adapter: Wan 2.2 I2V A14B Q5_K_M + Lightx2v 4-step, then BiRefNet RGBA, one clip per direction (ADR-0004).

`TrackE.generate(request)` stages the start image, runs `workflows/video/wan22_i2v_lightx2v.json` through an injected
ComfyUI client, copies every output into the attempt dir and records alpha facts. Layout of `request.out_dir`:

    source/ (original + start_image.png)  clip_rgb/ (node 90)  clip_rgba/ (node 92)  preview/ (node 93, APNG)
    request.json  submission.json (durable prompt ID)  run.json (client RunRecord)  result.json (RawResult)

The client is anything with `run(workflow, values, *, timeout_s, expect, prompt_id=None) -> RunRecord` (see
`sonoranlab.comfy.client.ComfyClient`); it is injected so tests and sweeps need no server. Inside the container the
staged input is `sonoranlab/<attempt>/start_image.png` (host `$WORK/input` is mounted read-only there) and outputs land
in `$WORK/output/p3/<attempt>/` (env/comfy.sh).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import yaml
from PIL import Image

from sonoranlab.comfy import provenance
from sonoranlab.comfy.adapters.base import DEFAULT_BACKGROUND, GenerateRequest, RawResult
from sonoranlab.comfy.guard import REPO, TRACK_NEED_GIB
from sonoranlab.comfy.guard import guarded_argv as guarded_argv
from sonoranlab.prep import gen_canvas, parse_hex, prepare_start_image

DEFAULT_WORKFLOW = REPO / "workflows/video/wan22_i2v_lightx2v.json"
GENERATED_DIRECTIONS = ("north", "north-east", "east", "south-east", "south")  # west is mirrored later (ADR-0007)
WAN_NEED_GIB = TRACK_NEED_GIB["E"]
FRINGE_TOL = 24  # RGB distance to the background that counts as a grey fringe
MAX_CANVAS = 1024  # Wan A14B at 736^2 is already 2.8x slower than 560^2 (ADR-0004); refuse anything bigger

# Phase 1 prompts verbatim (E/samurai/2 manifests), magenta -> grey only. A request's own `prompt` overrides.
_VIEWS = {
    "north": "seen from directly behind (back view)",
    "north-east": "three-quarter back view, turned to the right",
    "east": "side view facing right",
    "south-east": "three-quarter front view, turned to the right",
    "south": "front view facing the viewer",
}
_TAIL = (
    "a smooth looping run cycle with arms and legs swinging, the character stays centred and keeps the same facing, "
    "holding the weapon the same way throughout, static camera, no zoom, flat solid grey background, "
    "2D game sprite animation."
)
RUN_PROMPTS = {d: f"This character running in place, {v}, {_TAIL}" for d, v in _VIEWS.items()}

DEFAULTS: dict[str, Any] = {"length": 33, "steps": 4, "split_step": 2, "cfg": 1, "lora_strength": 1.0, "fps": 16}
_OWNED = {"start_image", "prompt", "seed"}  # set from the request / staging, never from settings
# workflow output node -> subdir of out_dir (SaveImage raw, SaveImage RGBA, SaveAnimatedPNG preview)
_NODE_DIRS = {"90": "clip_rgb", "92": "clip_rgba", "93": "preview"}
_SUBDIRS = ("source", *_NODE_DIRS.values())


class RunRecordLike(Protocol):
    """What this adapter reads from `ComfyClient.run`'s RunRecord."""

    prompt_id: str
    workflow_source: str
    workflow_source_sha256: str
    workflow_patched_sha256: str
    params: dict[str, Any]
    models: dict[str, str]
    outputs: list[Any]  # OutputFile: node, kind, rel, path, sha256, bytes
    status: str
    timing: dict[str, float]
    error: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]: ...


class ClientLike(Protocol):
    def run(
        self, workflow: Any, values: dict[str, Any], *, timeout_s: float, expect: Any, prompt_id: str | None = None
    ) -> RunRecordLike: ...


def default_work() -> Path:
    return Path(os.environ.get("SONORANLAB_WORK", Path.home() / "sonoranlab-work"))


def load_workflow(path: Path) -> Any:
    from sonoranlab.comfy.client import Workflow  # lazy: the client is not needed to validate or test

    return Workflow.load(path)


def make_client(base_url: str, output_root: Path, *, exec_container: str | None = None) -> ClientLike:
    """HTTP to `base_url`, or with `exec_container` the in-container API of the `--network=none` (offline) container."""
    from sonoranlab.comfy.client import ComfyClient, PodmanExecTransport

    if exec_container:
        return ComfyClient(
            "http://127.0.0.1:8188", output_root=output_root, transport=PodmanExecTransport(exec_container)
        )
    return ComfyClient(base_url, output_root=output_root)


def _sha(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def alpha_fringe_stats(
    frames: list[Path], background: str, tol: float = FRINGE_TOL, threshold: int = 128
) -> dict[str, Any]:
    """Recorded facts per RGBA frame, not gates: semi-transparent pixels (0<a<255); the silhouette edge the pipeline
    keeps (a >= `threshold`, the profile's alpha.threshold, 4-adjacent to a < threshold); edge pixels within `tol` of
    the background colour ("grey fringe": background bleeding into what becomes the outline); empty frames.
    BiRefNet's alpha is soft, so the edge is taken at the threshold, not at a == 255 next to a == 0."""
    bg = np.array(parse_hex(background), np.float32)
    rows = []
    for f in frames:
        with Image.open(f) as im:
            px = np.asarray(im.convert("RGBA"))
        alpha = px[..., 3]
        clear = np.pad(alpha < threshold, 1)  # outside the frame is not "transparent"
        near_clear = clear[:-2, 1:-1] | clear[2:, 1:-1] | clear[1:-1, :-2] | clear[1:-1, 2:]
        edge = (alpha >= threshold) & near_clear
        close = np.linalg.norm(px[..., :3].astype(np.float32) - bg, axis=-1) <= tol
        rows.append(
            {
                "frame": f.name,
                "semi_transparent": int(((alpha > 0) & (alpha < 255)).sum()),
                "edge": int(edge.sum()),
                "grey_fringe": int((edge & close).sum()),
                "empty": not alpha.any(),
            }
        )
    keys = ("semi_transparent", "edge", "grey_fringe")
    return {
        "background": background,
        "tolerance": tol,
        "threshold": threshold,
        "frames": rows,
        "totals": {k: sum(r[k] for r in rows) for k in keys},
        "empty_frames": [r["frame"] for r in rows if r["empty"]],
    }


def persist_submission(out: Path, client: Any, workflow: Any, values: dict[str, Any], wf_path: Path) -> dict:
    """Mint the prompt UUID and fsync out/submission.json BEFORE the client posts it, so a SIGKILL after ComfyUI
    accepted the prompt still leaves the id needed to reattach or cancel that exact job (guard.pending_submissions)."""
    from sonoranlab.comfy.client import graph_sha256

    submission = {
        "schema": "sonoranlab.submission/1",
        "prompt_id": str(uuid.uuid4()),
        "client_id": getattr(client, "client_id", None),
        "base_url": getattr(client, "base_url", None),
        "workflow_source": str(getattr(workflow, "source", wf_path)),
        "workflow_patched_sha256": graph_sha256(workflow.patch(values)) if hasattr(workflow, "patch") else None,
        "state": "submitting",
        "written_at": datetime.now(UTC).isoformat(),
    }
    with open(out / "submission.json", "x") as f:
        f.write(json.dumps(submission, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return submission


class _Plan:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class TrackE:
    track = "E"

    def __init__(
        self,
        client: ClientLike,
        *,
        workflow_path: Path = DEFAULT_WORKFLOW,
        work_root: Path | None = None,
        timeout_s: float = 1800,
        workflow: Any = None,  # preloaded client Workflow; None = loaded lazily from the path
        models_tsv: Path | None = None,  # None = env/models.tsv
    ) -> None:
        self.client = client
        self.models_tsv = Path(models_tsv) if models_tsv is not None else provenance.MODELS_TSV
        self.workflow_path = Path(workflow_path)
        self.work_root = Path(work_root) if work_root is not None else default_work()
        self.timeout_s = timeout_s
        self._workflow = workflow

    # -- validation (writes nothing) ------------------------------------------------------------------------------

    def _plan(self, r: GenerateRequest) -> _Plan:
        def bad(msg: str) -> ValueError:
            return ValueError(f"track E request: {msg}")

        if r.track != "E":
            raise bad(f"track is {r.track!r}, expected 'E'")
        if r.direction not in GENERATED_DIRECTIONS:
            raise bad(f"direction {r.direction!r} not in {GENERATED_DIRECTIONS} (west is mirrored, never generated)")
        if not r.character or not r.anim:
            raise bad("character and anim are required")
        if isinstance(r.seed, bool) or not isinstance(r.seed, int) or r.seed < 0:
            raise bad(f"seed must be a non-negative int, got {r.seed!r}")
        if r.control_image is not None:
            raise bad("control_image is a Track C input")
        parse_hex(r.background)
        if Path(r.out_dir).exists():
            raise bad(f"out_dir {r.out_dir} exists; every attempt gets a new directory")
        wf_path = self.workflow_path
        if r.workflow is not None:
            rel = Path(r.workflow)
            if rel.is_absolute() or ".." in rel.parts:
                raise bad(f"workflow {r.workflow!r} must be repo-relative")
            wf_path = REPO / rel
        try:
            known = yaml.safe_load(wf_path.with_suffix(".params.yaml").read_text())
        except (OSError, yaml.YAMLError) as e:
            raise bad(f"workflow params for {wf_path}: {e}") from e
        settings = dict(r.settings)
        for k in settings:
            if k in _OWNED or k.startswith("output_prefix") or k not in known:
                raise bad(f"setting {k!r} is not overridable; allowed: {sorted(set(known) - _OWNED)}")
        values = {**DEFAULTS, **settings}
        if not isinstance(values["length"], int) or values["length"] < 5 or (values["length"] - 1) % 4:
            raise bad(f"length must be 4n+1 and >= 5, got {values['length']!r}")
        prompt = r.prompt.strip()
        if not prompt:
            if r.anim != "run" or r.background != DEFAULT_BACKGROUND:
                raise bad("the default prompt is for anim 'run' on the grey background; pass a prompt")
            prompt = RUN_PROMPTS[r.direction]
        src = Path(r.source_image)
        try:
            with Image.open(src) as im:
                size, fmt, mode = im.size, im.format, im.mode
                has_alpha = "A" in im.getbands() or "transparency" in im.info
        except OSError as e:
            raise bad(f"source_image {src}: {e}") from e
        if size[0] != size[1]:
            raise bad(f"source_image is {size[0]}x{size[1]}, expected square")
        if has_alpha:
            kind, cell, canvas = "still", size[0], gen_canvas(size[0])
        else:
            if fmt != "PNG" or mode != "RGB" or size[0] % 16:
                raise bad(f"source_image without alpha must be an RGB PNG with a multiple-of-16 edge ({fmt} {mode})")
            kind, cell, canvas = "canvas", None, size[0]
        if canvas > MAX_CANVAS:
            raise bad(f"canvas {canvas} exceeds {MAX_CANVAS}")
        out = Path(r.out_dir).resolve()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", out.name).lstrip(".")[:60] or "attempt"
        attempt = f"{name}-{hashlib.sha256(str(out).encode()).hexdigest()[:10]}"
        staging = self.work_root / "input" / attempt
        if staging.exists():
            raise bad(f"staging dir {staging} exists; refusing to reuse it")
        return _Plan(
            wf_path=wf_path,
            values=values,
            prompt=prompt,
            kind=kind,
            cell=cell,
            canvas=canvas,
            attempt=attempt,
            staging=staging,
        )

    # -- run -------------------------------------------------------------------------------------------------------

    def generate(self, request: GenerateRequest) -> RawResult:
        plan = self._plan(request)
        out = Path(request.out_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            out.mkdir()
        except FileExistsError as e:
            raise ValueError(f"track E request: out_dir {out} exists") from e
        for sub in _SUBDIRS:
            (out / sub).mkdir()
        with open(out / "request.json", "x") as f:
            f.write(json.dumps(request.to_json(), indent=2) + "\n")
        res = RawResult(request=request, status="failed")

        def fail(code: str, stage: str, message: str, retryable: bool = False) -> None:
            if res.error is None:  # the first error is the cause
                res.error = {"code": code, "stage": stage, "message": message, "retryable": retryable}

        # DYL-125: verify the BiRefNet files the container will load before anything is staged or submitted.
        res.model_provenance, refusal = provenance.admit(self.work_root, plan.wf_path, models_tsv=self.models_tsv)
        if refusal:
            fail("model_provenance", "admission", refusal)
            res.error["hint"] = provenance.HINT
            res.write()
            return res
        try:
            start = self._stage(request, plan, out)
        except (OSError, ValueError) as e:
            fail("stage_failed", "stage", str(e))
            res.write()
            return res
        res.inputs = {"start_image": start["sha256"]}
        res.checks["source"] = start

        values = {
            "width": plan.canvas,
            "height": plan.canvas,
            **plan.values,
            "start_image": f"sonoranlab/{plan.attempt}/start_image.png",
            "prompt": plan.prompt,
            "seed": request.seed,
            **{f"output_prefix_{k}": f"p3/{plan.attempt}/{k}" for k in ("raw", "rgba", "preview")},
        }
        length = values["length"]
        res.params = dict(values)
        try:
            workflow = self._wf(request, plan)
            sub = persist_submission(out, self.client, workflow, values, plan.wf_path)
            res.prompt_id, res.workflow_source = sub["prompt_id"], sub["workflow_source"]
            res.workflow_patched_sha256 = sub["workflow_patched_sha256"]
            prompt_id = sub["prompt_id"]
            record = self.client.run(
                workflow, values, timeout_s=self.timeout_s, expect={"90": length, "92": length}, prompt_id=prompt_id
            )
        except Exception as e:  # the contract: a failed generation is a result, not a raise
            fail("adapter_exception", "run", f"{type(e).__name__}: {e}")
            res.write()
            return res

        res.workflow_source = record.workflow_source
        res.workflow_source_sha256 = record.workflow_source_sha256
        res.workflow_patched_sha256 = record.workflow_patched_sha256
        res.params = dict(record.params or values)
        res.models = provenance.merge_models(record.models, res.model_provenance)
        res.timing = dict(record.timing)
        with open(out / "run.json", "x") as f:
            f.write(json.dumps(record.to_json(), indent=2) + "\n")
        if record.status != "success":
            res.error = record.error or {
                "code": record.status,
                "stage": "run",
                "message": f"run ended {record.status}",
                "retryable": False,
            }
        try:
            self._collect(record, out, res)
        except (OSError, ValueError) as e:
            fail("copy_failed", "collect", str(e))
        if len(res.frames) != length or len(res.raw_frames) != length:
            msg = f"expected {length} rgb/rgba frames, got {len(res.raw_frames)}/{len(res.frames)}"
            fail("missing_frames", "collect", msg, retryable=True)
        try:
            res.checks["alpha"] = alpha_fringe_stats(res.frames, request.background)
        except (OSError, ValueError) as e:
            res.checks["alpha"] = {"error": str(e)}
        res.status = "ok" if res.error is None else "failed"
        res.write()
        return res

    def _wf(self, request: GenerateRequest, plan: _Plan) -> Any:
        if request.workflow is not None:
            return load_workflow(plan.wf_path)
        if self._workflow is None:
            self._workflow = load_workflow(plan.wf_path)
        return self._workflow

    def _stage(self, request: GenerateRequest, plan: _Plan, out: Path) -> dict[str, Any]:
        """Original + start image into out_dir/source, start image into the ComfyUI-visible input dir."""
        src = Path(request.source_image)
        shutil.copyfile(src, out / "source" / f"source_image{src.suffix}")
        start = out / "source" / "start_image.png"
        if plan.kind == "still":
            prep = prepare_start_image(src, plan.cell, start, background=request.background)
        else:
            shutil.copyfile(src, start)
            prep = {"path": start, "sha256": _sha(start), "canvas": plan.canvas, "background": None}
        plan.staging.parent.mkdir(parents=True, exist_ok=True)
        plan.staging.mkdir()  # exclusive: an attempt id is staged once
        shutil.copyfile(start, plan.staging / "start_image.png")
        return {
            "kind": plan.kind,
            "cell": plan.cell,
            "canvas": prep["canvas"],
            "background": prep["background"],
            "sha256": prep["sha256"],
            "source_sha256": _sha(src),
            "staged": str(plan.staging / "start_image.png"),
        }

    def _collect(self, record: RunRecordLike, out: Path, res: RawResult) -> None:
        """Copy every output into its subdir (frames in counter order), hash the copy, keep partials on failure."""
        for o in sorted(record.outputs, key=lambda o: (str(o.node), Path(o.path).name)):
            sub = _NODE_DIRS.get(str(o.node), f"extra/{o.node}")
            dst = out / sub / Path(o.path).name
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(o.path, "rb") as s, open(dst, "xb") as d:
                shutil.copyfileobj(s, d)
            digest = _sha(dst)
            res.outputs[dst.relative_to(out).as_posix()] = digest
            (res.raw_frames if sub == "clip_rgb" else res.frames if sub == "clip_rgba" else []).append(dst)
            if o.sha256 != digest:
                raise ValueError(f"{o.rel}: copy sha256 {digest[:12]} != recorded {o.sha256[:12]}")
