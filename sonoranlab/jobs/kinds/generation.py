"""Durable wrappers around the existing E pipeline and guarded single-pose C CLI."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from sonoranlab import registry
from sonoranlab.api.registry import Context, ToolError
from sonoranlab.api.tools.generation import ANIMATE_SCHEMA, POSE_SCHEMA
from sonoranlab.artifacts import require_regular_file, sha256_file
from sonoranlab.jobs import manager, store
from sonoranlab.jobs.executors import JobFailure

START_HINT = "start it: bash env/comfy.sh up --offline"
guard = import_module("sonoranlab.comfy.guard")


def references(params, ctx):
    profile = registry._character(params["project"], params["character"], ctx)
    base = registry._base(params["project"], params["character"], ctx)
    record = registry._record(base, params["project"], params["character"])
    refs = next((r for r in record["reference_sets"] if r["ref_set_id"] == params["ref_set_id"]), None)
    if refs is None:
        raise ToolError("not_found", "Unknown registered reference set", field="ref_set_id")
    verified = registry._verify_refs(base, refs, ctx)
    if verified["approval"]["state"] != "approved" or set(refs["files"]) != set(profile.directions.order):
        raise ToolError(
            "refused",
            "References must be approved and unchanged",
            field="ref_set_id",
            hint="owner must run `sonoranlab registry approve-references`",
        )
    return profile, base / "refs" / params["ref_set_id"], verified["verification"]["hashes"]


def template_path(params, ctx):
    anim, direction, _ = params["template"].split("/")
    if direction != params["direction"]:
        raise ToolError("invalid_params", "Template direction must match direction", field="template")
    root = ctx.repo / "templates/poses"
    path = root / (params["template"] + ".png")
    try:
        require_regular_file(path, root=ctx.repo)
        if not root.resolve(strict=True).is_relative_to(ctx.repo.resolve(strict=True)) or not path.resolve(
            strict=True
        ).is_relative_to(root.resolve(strict=True)):
            raise ValueError("Template escapes repository poses")
        registry._protected(path, ctx)
    except (OSError, ValueError) as exc:
        raise ToolError("path_refused", str(exc), field="template") from exc
    return anim, path


def replay(kind, params):
    """Revalidate idempotent replays before admission; they consume no extra attempt."""
    key = params.get("idempotency_key")
    if key is None:
        return None
    try:
        claims = store.directory("idempotency")
    except FileNotFoundError:
        return None
    path = claims / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    if path.is_symlink():
        raise ToolError("path_refused", "Idempotency claim symlink is refused")
    if not path.exists():
        return None
    normalized, hashes = EXECUTORS[kind].validate(params, budget=False)
    fingerprint = hashlib.sha256(
        store.canonical({"kind": kind, "params": normalized, "input_hashes": hashes})
    ).hexdigest()
    try:
        claim = store.load(path)
        existing = claim["request_fingerprint"]
        job_id = claim["job_id"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError("conflict", "Idempotency reservation is incomplete; inspect it, never replay") from exc
    if existing != fingerprint:
        raise ToolError("idempotency_conflict", "Key already names a different request")
    return manager.summary(manager.get(job_id))


def artifact(job, path):
    root = store.output(job)
    require_regular_file(path, root=root)
    return f"{job['job_id']}:{path.relative_to(root).as_posix()}"


def artifacts(job, root):
    return [artifact(job, p) for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()]


def raw_results(root):
    return [(p, store.load(p)) for p in sorted(root.glob("gen/**/result.json"))]


def unavailable(value):
    message = str(value).lower()
    return any(
        token in message
        for token in (
            "podman exec relay failed",
            "no such container",
            "container is not running",
            "no such file or directory: 'podman'",
        )
    )


def failure(error, stage):
    error = error if isinstance(error, dict) else {"message": str(error)}
    missing = unavailable(error)
    exc = JobFailure(
        "unavailable" if missing else error.get("code", "generation_failed"),
        error.get("stage", stage),
        error.get("message", "Generation failed"),
        True if missing else error.get("retryable", False),
    )
    if missing:
        exc.error["hint"] = START_HINT
    raise exc


class Generation:
    gpu = True
    heavy_cpu = False

    def __init__(self, kind):
        self.kind = kind
        self.track = "E" if kind == "animate" else "C"

    def need_gib(self, params):
        return params.get("need_gib", guard.min_need_gib(self.track))

    def validate(self, params, *, budget=True):
        params = dict(params)
        # Frozen normalized requests also pass this boundary on explicit retry.
        track = params.pop("track", self.track)
        if track != self.track:
            raise ToolError("invalid_params", "Track cannot be overridden", field="track")
        if self.kind == "pose_frame":
            frozen_anim = params.pop("anim", None)
        schema = ANIMATE_SCHEMA if self.kind == "animate" else POSE_SCHEMA
        try:
            Draft202012Validator(schema).validate(params)
        except ValidationError as exc:
            raise ToolError("invalid_params", exc.message) from exc
        params.pop("idempotency_key", None)
        seeds = params["seeds"] if self.kind == "animate" else [params["seed"]]
        if any(type(seed) is not int for seed in seeds):
            raise ToolError(
                "invalid_params", "Seeds must be integers", field="seeds" if self.kind == "animate" else "seed"
            )
        ctx = Context()
        profile, _, hashes = references(params, ctx)
        allow = set(filter(None, os.environ.get("SONORANLAB_ALLOW_PAUSE_UNITS", "").split(os.pathsep)))
        if any(unit not in allow for unit in params.get("allow_pause", [])):
            raise ToolError("refused", "allow_pause unit is not in the owner's allowlist", field="allow_pause")
        params.setdefault("allow_pause", [])
        inputs = {"stills": hashes, "profile": sha256_file(profile.path)}
        if self.kind == "animate":
            spec = profile.character(params["character"])
            anim = spec.animations.get(params["anim"])
            if anim is None or anim.track_default != "E":
                raise ToolError(
                    "invalid_params", "anim must be this character's Track E profile animation", field="anim"
                )
            if len(params["seeds"]) > profile.autonomy.max_seeds_per_attempt:
                raise ToolError("refused", "Too many seeds for profile autonomy", field="seeds")
            params.setdefault("windows", "default")
            params.setdefault("godot_check", True)
            params.setdefault("need_gib", guard.min_need_gib("E"))
            workflow = ctx.repo / "workflows/video/wan22_i2v_lightx2v.json"
        else:
            if params["direction"] not in profile.directions.generate:
                raise ToolError("invalid_params", "direction must be in directions.generate", field="direction")
            anim, template = template_path(params, ctx)
            if anim not in profile.character(params["character"]).animations or frozen_anim not in (None, anim):
                raise ToolError("invalid_params", "Unknown template animation", field="template")
            adapter = import_module("sonoranlab.comfy.adapters.track_c")
            if not getattr(adapter, "POSE_PROMPTS", {}).get(params["direction"]):
                raise ToolError("refused", "Track C has no documented default prompt for this direction")
            params["anim"] = anim
            inputs["template"] = sha256_file(template)
            workflow = ctx.repo / "workflows/pose/qwen_edit_pose_native.json"
        inputs["workflow"] = sha256_file(workflow)
        params["track"] = self.track
        return params, inputs

    def admit(self, params, attempt, jobs):
        """Called by manager.submit under its submit lock. Attempts that ran (submitted a prompt) or are still active
        count toward profile autonomy.max_attempts_per_animation; launch failures and queued cancels do not."""
        if self.kind != "animate":
            return
        limit = registry._profile(params["project"], Context()).autonomy.max_attempts_per_animation
        used = [
            j["job_id"]
            for j in jobs
            if j.get("kind") == "animate"
            and all(j.get(k) == params[k] for k in ("project", "character", "anim"))
            and (j["state"] in store.ACTIVE or j.get("prompts"))
        ]
        if len(used) >= limit:
            raise ToolError(
                "refused",
                f"Animation attempt limit reached ({len(used)}/{limit}): {', '.join(sorted(used))}",
                field="anim",
                hint="profile autonomy.escalate_after: ask the owner before more attempts",
            )

    def stage(self, job):
        root = store.output(job)
        if self.kind == "pose_frame":
            return {
                "stage": "generate" if (root / "request.json").exists() else "prepare",
                "completed": int((root / "attempt-1/result.json").is_file()),
                "planned": 1,
            }
        root = root / "animate"
        planned = len(list((root / "gen/requests").glob("*.json")))
        stage = "generate" if (root / "gen").exists() else "prepare"
        if (root / "sweep").is_dir():
            stage = "select_and_process"
        if any((root / "sweep").glob("**/export")):
            stage = "validate_export"
        progress = {"stage": stage, "completed": len(list(root.glob("gen/**/result.json"))), "planned": planned}
        if (root / "animate.json").is_file():
            doc = store.load(root / "animate.json")
            progress.update(self.status(doc, root))
        return progress

    @staticmethod
    def status(doc, root):
        verdict = doc.get("verdict") or {}
        final = Path(verdict.get("job", root))
        if not final.resolve().is_relative_to(root.resolve()):
            raise ToolError("path_refused", "Final job escapes animate output")
        review = registry._review(final, Context())
        godot = doc.get("godot_check") or {}
        return {
            "technical": {
                "validator": None if "pass" not in verdict else "pass" if verdict["pass"] else "fail",
                "godot": "not_run"
                if not godot
                else "pass"
                if godot.get("pass")
                else "unavailable"
                if godot.get("available") is False
                else "fail",
            },
            "human": {
                "review": review["review_state"],
                "production_usable": review["production_usable"],
                "message": "awaiting owner review" if not review["production_usable"] else "owner reviewed",
            },
        }

    def run(self, job, ctx):
        # Approval and all frozen inputs are checked again after queueing, before any child starts.
        normalized, hashes = self.validate(job["params"], budget=False)
        if hashes != job["input_hashes"] or normalized != job["params"]:
            raise JobFailure("inputs_changed", "prepare", "Frozen generation inputs changed")
        api_ctx = Context()
        profile, refs, _ = references(normalized, api_ctx)
        ctx.check_cancelled()
        ctx.set_stage("generate")
        if self.kind == "pose_frame":
            return self.pose(job, ctx, refs, api_ctx)
        animate = import_module("sonoranlab.animate").animate

        def runner(argv, **kwargs):
            # sweep already wrapped this command; insert owner-authorized pause flags in that guard.
            index = argv.index("--")
            pauses = [x for unit in normalized["allow_pause"] for x in ("--allow-pause", unit)]
            try:
                return ctx.run([*argv[:index], *pauses, *argv[index:]], stdout=subprocess.DEVNULL, **kwargs)
            except JobFailure as exc:
                # Let animate record the guard exit and remaining requests in animate.json.
                if exc.error["code"] not in {"budget_refused", "guard_busy"}:
                    raise
                return subprocess.CompletedProcess(argv, 3 if exc.error["code"] == "budget_refused" else 4)

        root = ctx.output_dir / "animate"
        doc = animate(
            profile=profile.path,
            character=normalized["character"],
            reference_dir=refs,
            anim=normalized["anim"],
            track="E",
            seeds=normalized["seeds"],
            out=root,
            exec_container=os.environ.get("SONORANLAB_COMFY_CONTAINER", "sonoranlab-comfy"),
            need_gib=normalized["need_gib"],
            windows=normalized["windows"],
            godot_check=normalized["godot_check"],
            repo=api_ctx.repo,
            runner=runner,
        )
        ctx.check_cancelled()
        raw = raw_results(root)
        attempts = [
            {
                "result": artifact(job, p),
                **{
                    k: value.get(k)
                    for k in ("models", "workflow_source_sha256", "workflow_patched_sha256", "inputs", "outputs")
                },
                "memory": value.get("memory"),
            }
            for p, value in raw
        ]
        if doc["outcome"] not in {"pass", "validator_fail"}:
            if doc["outcome"] == "blocked":
                code = "guard_busy" if (doc.get("generation") or {}).get("stop_exit") == 4 else "budget_refused"
                raise JobFailure(code, "generate", str(doc.get("error")), True)
            cause = next((r.get("error") for _, r in raw if unavailable(r.get("error"))), None)
            failure(cause or {"code": doc["outcome"], "message": doc.get("error")}, self.stage(job)["stage"])
        return {
            "animate": doc,
            "summary": artifact(job, root / "animate.json"),
            "attempts": attempts,
            "export_artifacts": artifacts(job, root / "sweep/final/job/export") if doc["outcome"] == "pass" else [],
            **self.status(doc, root),
        }

    def pose(self, job, ctx, refs, api_ctx):
        GenerateRequest = import_module("sonoranlab.comfy.adapters.base").GenerateRequest

        params = job["params"]
        _, template = template_path(params, api_ctx)
        adapter = import_module("sonoranlab.comfy.adapters.track_c")
        request = GenerateRequest(
            track="C",
            character=params["character"],
            anim=params["anim"],
            direction=params["direction"],
            source_image=refs / f"still_{params['direction']}.png",
            control_image=template,
            prompt=adapter.POSE_PROMPTS[params["direction"]],
            seed=params["seed"],
            out_dir=ctx.output_dir / "attempt-1",
        )
        path = ctx.output_dir / "request.json"
        store.atomic(path, request.to_json(), exclusive=True)
        argv = guard.guarded_argv(
            [
                sys.executable,
                "-m",
                "sonoranlab.cli",
                "generate",
                "--request",
                str(path),
                "--exec",
                os.environ.get("SONORANLAB_COMFY_CONTAINER", "sonoranlab-comfy"),
            ],
            guard.min_need_gib("C"),
            tuple(params["allow_pause"]),
        )
        proc = ctx.run(argv, cwd=api_ctx.repo, stdout=subprocess.DEVNULL, check=False)
        result_path = request.out_dir / "result.json"
        if not result_path.is_file():
            failure(
                {
                    "code": "generation_failed",
                    "message": f"Generate child exited {proc.returncode} without result.json",
                },
                "generate",
            )
        raw = store.load(result_path)
        if raw["status"] != "ok" or proc.returncode:
            failure(raw.get("error"), "generate")
        return {"raw_result": raw, "artifacts": artifacts(job, request.out_dir), "memory": raw.get("memory")}


EXECUTORS = {kind: Generation(kind) for kind in ("animate", "pose_frame")}
