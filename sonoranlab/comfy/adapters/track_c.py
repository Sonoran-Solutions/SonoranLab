"""Track C adapter: Qwen Image Edit 2511 FP8 + Lightning 4-step with the pose as native keypoint input (image1),
the character still as image2 and NO ControlNet (an InstantX ControlNet-Union was ineffective in Phase 1, so it is
deliberately absent). One request = one held pose / posed first frame; the output feeds Track E as its start image.

`TrackC.generate(request)` stages both inputs on a 1024 canvas (Qwen's encoder rescales every input to ~1 MP; a
canvas-sized reference came out ~1.8x too large, benchmark/tools/prep_inputs.py), runs
`workflows/pose/qwen_edit_pose_native.json` through an injected ComfyUI client and copies both outputs into the
attempt dir. Layout of `request.out_dir`:

    source/ (original still + pose, reference_1024.png, pose_1024.png)  image_rgb/ (node 9)  image_rgba/ (node 92)
    request.json  run.json (client RunRecord)  result.json (RawResult)

The client is anything with `run(workflow, values, *, timeout_s, expect, prompt_id=None) -> RunRecord` (see the
`ClientLike` re-exported from `track_e`); inside the container the staged inputs are `sonoranlab/<attempt>/...` and
outputs land in `$WORK/output/p3/<attempt>/` (env/comfy.sh).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

from sonoranlab.comfy import provenance
from sonoranlab.comfy.adapters import track_e
from sonoranlab.comfy.adapters.base import DEFAULT_BACKGROUND, GenerateRequest, RawResult
from sonoranlab.comfy.adapters.track_e import (  # shared plumbing: never copy-paste these
    REPO,
    ClientLike,
    RunRecordLike,
    _sha,
    alpha_fringe_stats,
    default_work,
    load_workflow,
)
from sonoranlab.comfy.guard import TRACK_NEED_GIB
from sonoranlab.prep import gen_canvas, parse_hex, prepare_pose_1024, prepare_reference_1024


def make_client(base_url: str, output_root: Path, *, exec_container: str | None = None) -> ClientLike:
    """Per-module name so callers and tests patch Track C's client without touching Track E's."""
    return track_e.make_client(base_url, output_root, exec_container=exec_container)


DEFAULT_WORKFLOW = REPO / "workflows/pose/qwen_edit_pose_native.json"
GENERATED_DIRECTIONS = ("north", "north-east", "east", "south-east", "south")  # west is mirrored later (ADR-0007)
QWEN_NEED_GIB = TRACK_NEED_GIB["C"]  # run-window budget for one Qwen Edit prompt (sonoranlab.comfy.guard)
REFERENCE_CANVAS = 1024  # Qwen Edit's encoder resolution (~1 MP); anything smaller makes the subject grow
PREP_SCALE = 3  # x3 pixel scale of the profile's cell renders

# Phase 1 wording verbatim (C/samurai matrix), magenta -> grey; `{view}` per direction.
_VIEWS = {
    "north": "seen from directly behind (back view)",
    "north-east": "three-quarter back view, turned to the right",
    "east": "side view facing right",
    "south-east": "three-quarter front view, turned to the right",
    "south": "front view facing the viewer",
}
POSE_PROMPT = (
    "Picture 1 shows a pose. Redraw the character from Picture 2 exactly - same face, hair, clothes, colours and "
    "weapon, and the same size in the frame as in Picture 2 - in the pose of Picture 1, {view}, full body with the "
    "feet inside the frame, 2D game sprite, flat shading, plain flat grey background."
)
POSE_PROMPTS = {d: POSE_PROMPT.format(view=v) for d, v in _VIEWS.items()}

DEFAULTS: dict[str, Any] = {
    "steps": 4,
    "cfg": 1,
    "lightning_strength": 1.0,
    "negative_prompt": "",  # inert at cfg 1 but the graph encodes one; a params.yaml name so overridable
}
_OWNED = {"reference_image", "control_image", "prompt", "seed"}  # set from the request / staging, never settings
# workflow output node -> subdir of out_dir (SaveImage raw RGB, SaveImage BiRefNet RGBA)
_NODE_DIRS = {"9": "image_rgb", "92": "image_rgba"}
_SUBDIRS = ("source", *_NODE_DIRS.values())


class _Plan:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class TrackC:
    track = "C"

    def __init__(
        self,
        client: ClientLike,
        *,
        workflow_path: Path = DEFAULT_WORKFLOW,
        work_root: Path | None = None,
        timeout_s: float = 900,
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
            return ValueError(f"track C request: {msg}")

        if r.track != "C":
            raise bad(f"track is {r.track!r}, expected 'C'")
        if r.direction not in GENERATED_DIRECTIONS:
            raise bad(f"direction {r.direction!r} not in {GENERATED_DIRECTIONS} (west is mirrored, never generated)")
        if not r.character or not r.anim:
            raise bad("character and anim are required")
        if isinstance(r.seed, bool) or not isinstance(r.seed, int) or r.seed < 0:
            raise bad(f"seed must be a non-negative int, got {r.seed!r}")
        if r.control_image is None:
            raise bad("control_image (the Track D pose render) is required")
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
        if Path(r.source_image) == Path(r.control_image):
            raise bad("source_image and control_image must be different files")
        prompt = r.prompt.strip()
        if not prompt:
            if r.background != DEFAULT_BACKGROUND:
                raise bad("the default prompt asks for the grey background; pass a prompt for another background")
            prompt = POSE_PROMPTS[r.direction]
        cell = self._cell(Path(r.source_image), bad)
        pose_scale = self._pose_scale(Path(r.control_image), cell, bad)
        out = Path(r.out_dir).resolve()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", out.name).lstrip(".")[:60] or "attempt"
        attempt = f"{name}-{hashlib.sha256(str(out).encode()).hexdigest()[:10]}"
        staging = self.work_root / "input" / attempt
        if staging.exists():
            raise bad(f"staging dir {staging} exists; refusing to reuse it")
        values = {**DEFAULTS, **settings}
        return _Plan(
            wf_path=wf_path,
            values=values,
            prompt=prompt,
            cell=cell,
            canvas=gen_canvas(cell, PREP_SCALE),
            pose_scale=pose_scale,
            attempt=attempt,
            staging=staging,
        )

    @staticmethod
    def _cell(src: Path, bad: Any) -> int:
        """The character still is the cell-size RGBA still; its edge is the profile cell."""
        try:
            with Image.open(src) as im:
                size, has_alpha = im.size, "A" in im.getbands() or "transparency" in im.info
        except OSError as e:
            raise bad(f"source_image {src}: {e}") from e
        if size[0] != size[1]:
            raise bad(f"source_image is {size[0]}x{size[1]}, expected a square cell-size RGBA still")
        if not has_alpha:
            raise bad(f"source_image {src} has no alpha; Track C image2 is the RGBA still of the character")
        return size[0]

    @staticmethod
    def _pose_scale(pose: Path, cell: int, bad: Any) -> int:
        try:
            with Image.open(pose) as im:
                size = im.size
        except OSError as e:
            raise bad(f"control_image {pose}: {e}") from e
        if size[0] != size[1] or size[0] % cell:
            raise bad(f"control_image is {size[0]}x{size[1]}, expected a square multiple of cell {cell}")
        return size[0] // cell

    # -- run -------------------------------------------------------------------------------------------------------

    def generate(self, request: GenerateRequest) -> RawResult:
        plan = self._plan(request)
        out = Path(request.out_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            out.mkdir()
        except FileExistsError as e:
            raise ValueError(f"track C request: out_dir {out} exists") from e
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
            staged = self._stage(request, plan, out)
        except (OSError, ValueError) as e:
            fail("stage_failed", "stage", str(e))
            res.write()
            return res
        res.inputs = {"reference_image": staged["reference_sha256"], "control_image": staged["pose_sha256"]}
        res.checks["source"] = staged["checks"]

        values = {
            "width": plan.canvas,
            "height": plan.canvas,
            **plan.values,
            "reference_image": f"sonoranlab/{plan.attempt}/reference.png",
            "control_image": f"sonoranlab/{plan.attempt}/pose.png",
            "prompt": plan.prompt,
            "seed": request.seed,
            "output_prefix": f"p3/{plan.attempt}/raw",
            "output_prefix_rgba": f"p3/{plan.attempt}/rgba",
        }
        res.params = dict(values)
        try:
            workflow = self._wf(request, plan)
            sub = track_e.persist_submission(out, self.client, workflow, values, plan.wf_path)
            res.prompt_id = sub["prompt_id"]
            record = self.client.run(
                workflow, values, timeout_s=self.timeout_s, expect={"9": 1, "92": 1}, prompt_id=sub["prompt_id"]
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
        if record.status != "success":  # keep the client's structured error: it knows whether a retry helps
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
        if len(res.raw_frames) != 1 or len(res.frames) != 1:
            msg = f"expected 1 rgb + 1 rgba image, got {len(res.raw_frames)}/{len(res.frames)}"
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
        """Originals into out_dir/source, the 1024-canvas pair into out_dir/source and the ComfyUI input dir."""
        src, pose = Path(request.source_image), Path(request.control_image)
        shutil.copyfile(src, out / "source" / f"source_image{src.suffix}")
        shutil.copyfile(pose, out / "source" / f"control_image{pose.suffix}")
        ref = out / "source" / "reference_1024.png"
        pose_ref = out / "source" / "pose_1024.png"
        ref_info = prepare_reference_1024(
            src, plan.cell, ref, background=request.background, scale=PREP_SCALE, canvas=REFERENCE_CANVAS
        )
        pose_info = prepare_pose_1024(pose, plan.cell, pose_ref, scale=PREP_SCALE, canvas=REFERENCE_CANVAS)
        plan.staging.parent.mkdir(parents=True, exist_ok=True)
        plan.staging.mkdir()  # exclusive: an attempt id is staged once
        shutil.copyfile(ref, plan.staging / "reference.png")
        shutil.copyfile(pose_ref, plan.staging / "pose.png")
        checks = {
            "kinds": ["still_rgba", "pose_render"],
            "cell": plan.cell,
            "canvas": plan.canvas,
            "reference_canvas": ref_info["canvas"],
            "background": ref_info["background"],
            "source_sha256": _sha(src),
            "control_sha256": _sha(pose),
            "reference_sha256": ref_info["sha256"],
            "pose_sha256": pose_info["sha256"],
            "pose_source_scale": pose_info["source_scale"],
            "staged": [str(plan.staging / "reference.png"), str(plan.staging / "pose.png")],
        }
        return {
            "reference_sha256": ref_info["sha256"],
            "pose_sha256": pose_info["sha256"],
            "checks": checks,
        }

    def _collect(self, record: RunRecordLike, out: Path, res: RawResult) -> None:
        """Copy every output into its subdir, hash the copy (must match the recorded hash), keep partials on failure."""
        for o in sorted(record.outputs, key=lambda o: (str(o.node), Path(o.path).name)):
            sub = _NODE_DIRS.get(str(o.node), f"extra/{o.node}")
            dst = out / sub / Path(o.path).name
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(o.path, "rb") as s, open(dst, "xb") as d:
                shutil.copyfileobj(s, d)
            digest = _sha(dst)
            res.outputs[dst.relative_to(out).as_posix()] = digest
            if sub == "image_rgb":
                res.raw_frames.append(dst)
            elif sub == "image_rgba":
                res.frames.append(dst)
            if o.sha256 != digest:
                raise ValueError(f"{o.rel}: copy sha256 {digest[:12]} != recorded {o.sha256[:12]}")
