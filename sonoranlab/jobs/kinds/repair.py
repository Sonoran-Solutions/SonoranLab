"""Saved-document acceptance under the durable worker's heavy CPU lock."""

from __future__ import annotations

import shutil
from pathlib import Path

from sonoranlab.api.registry import Context, ToolError
from sonoranlab.api.tools import repair as ops
from sonoranlab.artifacts import sha256_file
from sonoranlab.jobs import store
from sonoranlab.jobs.executors import JobFailure


class FinalizeRepair:
    gpu = False
    heavy_cpu = True

    def __init__(self, context: Context | None = None):
        self.context = context

    def need_gib(self, params: dict) -> None:
        return None

    def validate(self, params: dict) -> tuple[dict, dict]:
        from sonoranlab.api.tools.deterministic import profile_for
        from sonoranlab.repair.authorization import lease
        from sonoranlab.repair.finalize import _verify

        repair = ops.repair_path(params["repair_id"])
        claim = repair / "mcp-finalize.json"
        if claim.exists() or claim.is_symlink():
            raise ToolError("refused", "Repair already handed to finalize; prepare a new repair")
        with lease(repair):
            doc, profile, *_ = ops.invoke(_verify, repair)
            normalized = {
                "repair_id": repair.name,
                "project": profile.id,
                "character": doc["character"],
                "anim": doc["anim"],
            }
            if params != {"repair_id": repair.name} and params != normalized:
                raise ToolError("refused", "Frozen repair identity changed")
            registered = profile_for(normalized, self.context or Context())
            if registered.path.resolve() != profile.path.resolve():
                raise ToolError("refused", "Repair must use the repository project profile")
            files = [repair / name for name in ("repair.json", "mapping.json", "state.json", "authorization.json")]
            files += [p for folder in ("source", "workspace") for p in (repair / folder).rglob("*") if p.is_file()]
            files += [path for name in ("accepted.json", "ops.jsonl") if (path := repair / "session" / name).is_file()]
            files += [profile.path, profile.palette.source]
            files += [Path(doc["parent_job"]) / name for name in doc["parent_hashes"]]
            hashes = {str(path): sha256_file(path) for path in files}
        return normalized, hashes

    def run(self, job: dict, ctx) -> dict:
        from sonoranlab import registry
        from sonoranlab.api import paths
        from sonoranlab.repair.authorization import lease
        from sonoranlab.repair.confine import repair_root
        from sonoranlab.repair.finalize import finalize

        params, hashes = self.validate(job["params"])
        if hashes != job["input_hashes"]:
            raise ToolError("refused", "Frozen repair inputs changed before execution")
        repair = ops.repair_path(params["repair_id"])
        out = paths._confine(ctx.output_dir, (paths.state_root(),))
        child = ops.invoke(repair_root) / f"{repair.name}-child-{job['job_id']}"
        ctx.set_stage("validate_export")
        with lease(repair):
            # A durable single-attempt claim survives failure or worker death; never replay uncertain acceptance.
            store.atomic(repair / "mcp-finalize.json", {"job_id": job["job_id"]}, exclusive=True)
        result = ops.invoke(finalize, repair, out=child)
        context = self.context or Context()
        try:
            imported = registry.import_job(params["project"], params["character"], str(child), ctx=context)
            review = registry.check_review(imported["artifact_id"], ctx=context)
        except ToolError as exc:
            error = exc.error
            raise JobFailure(error["code"], "validate_export", error["message"], error["retryable"]) from exc
        manifest = store.load(child / "repair-manifest.json")
        gc = manifest.get("godot_check")
        missing_godot = (
            gc is None
            and any(error["code"] == "godot_or_export_failed" for error in result["errors"])
            and shutil.which("godot") is None
        )
        result = {
            **result,
            "job": job["job_id"],
            "artifact_id": imported["artifact_id"],
            "child_path": str(child),
            **{
                key: f"{imported['artifact_id']}:{Path(result[key]).relative_to(child).as_posix()}"
                for key in ("report", "contact", "manifest", "repair_manifest")
            },
            "export": {
                key: f"{imported['artifact_id']}:{Path(value).relative_to(child).as_posix()}"
                for key, value in (result["export"] or {}).items()
            }
            or None,
            "technical": {
                "validator": "pass" if manifest["validator"].get("pass") else "fail",
                "export": "pass" if result["export"] else "fail",
                "godot": "unavailable"
                if missing_godot
                else "not_run"
                if gc is None
                else "unavailable"
                if gc.get("available") is False
                else "pass"
                if gc.get("pass")
                else "fail",
            },
            "hashes": {**manifest["output_hashes"], "manifest.json": sha256_file(child / "manifest.json")},
            "human": {
                "review": review["review_state"],
                "production_usable": review["production_usable"],
                "status": "awaiting_owner_review",
                "problems": review["problems"],
            },
        }
        store.atomic(
            out / "finalize-result.json",
            {key: result[key] for key in ("child_path", "artifact_id", "hashes", "technical", "human")},
            exclusive=True,
        )
        return result

    def stage(self, job: dict) -> dict:
        out = store.output(job)
        return {"stage": job["stage"], "result_present": (out / "finalize-result.json").is_file()}


EXECUTORS = {"finalize_repair": FinalizeRepair()}
