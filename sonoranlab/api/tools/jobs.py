"""Job tools share the CLI/MCP envelope; reads are strictly offline."""

from __future__ import annotations

from pathlib import Path

from sonoranlab.api.registry import Context, Tool, ToolError
from sonoranlab.jobs import manager, store
from sonoranlab.jobs.executors import executor


def list_jobs(params: dict, ctx: Context) -> dict:
    jobs = manager.reconcile_all()
    return {
        "jobs": [
            {
                key: job.get(key)
                for key in ("job_id", "kind", "project", "character", "anim", "state", "stage", "created_at", "error")
            }
            for job in jobs
            if ("project" not in params or job.get("project") == params["project"])
            and ("state" not in params or job["state"] == params["state"])
        ][: params.get("limit", 50)]
    }


def measured_progress(job: dict) -> dict:
    """Executor.stage must measure files only; it must not probe services or launch processes."""
    try:
        return executor(job["kind"]).stage(job)
    except (ToolError, OSError, ValueError):
        return {}


def job_status(params: dict, ctx: Context) -> dict:
    job = manager.get(params["job_id"])
    progress = {}
    technical = {"validator": None, "godot": "not_run"}
    human = {"review": "none", "production_usable": False}
    if not manager.corrupt(job):
        progress = measured_progress(job)
        if "human" not in progress:
            from sonoranlab.registry import _native_review

            review = _native_review(job, ctx)
            human = {"review": review["review_state"], "production_usable": review["production_usable"]}
    # An executor that knows where its final artifact lives (animate: sweep/final/job) reports measured verdicts.
    technical = progress.pop("technical", None) or technical
    human = progress.pop("human", human)
    stage = progress.get("stage")
    return {
        **{key: value for key, value in job.items() if key != "result"},
        "stage": stage if stage in store.STAGES else job["stage"],
        "result_summary": _result_summary(job, human),
        "progress": progress,
        "technical": technical,
        "human": human,
    }


def _result_summary(job: dict, human: dict) -> dict | None:
    result = job.get("result")
    if result is None:
        return None
    summary = {
        key: result[key]
        for key in (
            "ok",
            "exit",
            "error",
            "pass",
            "complete",
            "partial",
            "job",
            "artifact_id",
            "child_path",
            "report",
            "contact",
            "manifest",
            "repair_manifest",
            "export",
            "ref",
            "summary",
            "technical",
            "failures",
            "warnings",
            "errors",
        )
        if key in result
    }
    if "human" in result:
        summary["human"] = {key: human[key] for key in ("review", "production_usable")}
    if doc := result.get("animate"):
        summary["animate"] = {
            key: dict(doc[key]) if key == "verdict" and isinstance(doc[key], dict) else doc[key]
            for key in ("outcome", "exit", "error", "counts", "timing", "stills", "verdict", "export")
            if key in doc
        }
        summary["animate"]["generation"] = {
            "requests": [
                {key: r.get(key) for key in ("direction", "seed", "status", "exit", "gen_seconds", "error")}
                for r in (doc.get("generation") or {}).get("requests", [])
            ]
        }
        summary["attempts"] = [{"result": attempt["result"]} for attempt in result.get("attempts", [])]
    for verdict in (summary, (summary.get("animate") or {}).get("verdict") or {}):
        for key in ("failures", "warnings", "errors"):
            if isinstance(verdict.get(key), list):
                verdict[key + "_count"] = len(verdict[key])
                verdict[key + "_truncated"] = len(verdict[key]) > 20
                verdict[key] = verdict[key][:20]
    for key in ("godot_check", "raw_result"):
        if isinstance(result.get(key), dict):
            summary[key] = {
                k: result[key][k] for k in ("status", "pass", "available", "error", "timing") if k in result[key]
            }
            if "godot_attempts" in result[key]:
                summary[key]["godot_attempts"] = [
                    {k: v for k, v in attempt.items() if k not in {"stdout", "stderr", "argv"}}
                    for attempt in result[key]["godot_attempts"]
                ]

    root = str(store.output(job)) + "/"

    def refs(value):
        if isinstance(value, dict):
            return {key: refs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [refs(item) for item in value]
        if isinstance(value, str) and value.startswith(root):
            return f"{job['job_id']}:{value[len(root) :]}"
        return value

    return refs(summary)


def job_result(params: dict, ctx: Context) -> dict:
    job = manager.get(params["job_id"])
    if job["state"] not in store.TERMINAL:
        raise ToolError("not_ready", "Job result is not ready; poll job_status", field="job_id", retryable=True)
    root = store.output(job)
    paths = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
    ]

    def priority(relative):
        path = Path(relative)
        names = ("report.json", "contact.png", "manifest.json")
        rank = (
            names.index(path.name)
            if path.name in names
            else (
                3
                if "export" in path.parts
                else 4
                if path.name == "animate.json"
                else 5
                if path.name == "sweep.json"
                else 6
            )
        )
        return rank, relative

    paths.sort(key=priority)
    offset, limit = params.get("artifacts_offset", 0), params.get("artifacts_limit", 200)
    artifacts = [
        {
            "ref": f"{job['job_id']}:{relative}",
        }
        for relative in paths[offset : offset + limit]
    ]
    from sonoranlab.registry import _native_review

    review = _native_review(job, ctx) if not manager.corrupt(job) else {}
    human = {
        "review": review.get("review_state", "invalid"),
        "production_usable": review.get("production_usable", False),
    }
    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "result": _result_summary(job, human),
        "error": job["error"],
        "artifacts": artifacts,
        "artifact_count": len(paths),
        "artifacts_truncated": offset + len(artifacts) < len(paths),
    }


def cancel_job(params: dict, ctx: Context) -> dict:
    return manager.cancel_job(params["job_id"])


def retry_job(params: dict, ctx: Context) -> dict:
    return manager.retry_job(params["job_id"], params.get("idempotency_key"))


def schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


ID = {"type": "string", "pattern": f"^{store.JOB_ID}$"}
TOOLS = (
    Tool(
        "list_jobs",
        "List durable jobs; reconcile lost worker leases without contacting services.",
        schema(
            {
                "project": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$"},
                "state": {"enum": list(store.TRANSITIONS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
        True,
        "sync",
        list_jobs,
    ),
    Tool(
        "job_status",
        "Read execution state, measured progress, timing, and separate review status.",
        schema({"job_id": ID}, ("job_id",)),
        True,
        "sync",
        job_status,
    ),
    Tool(
        "job_result",
        "Read a completed job summary and a bounded page of prioritized artifact references.",
        schema(
            {
                "job_id": ID,
                "artifacts_offset": {"type": "integer", "minimum": 0, "default": 0},
                "artifacts_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
            ("job_id",),
        ),
        True,
        "sync",
        job_result,
    ),
    Tool(
        "cancel_job",
        "Request cancellation of this job only; preserve unconfirmed outcomes.",
        schema({"job_id": ID}, ("job_id",)),
        False,
        "sync",
        cancel_job,
    ),
    Tool(
        "retry_job",
        "Create a new linked attempt from a frozen failed/cancelled/resolved blocked request.",
        schema({"job_id": ID, "idempotency_key": {"type": "string", "minLength": 1}}, ("job_id",)),
        False,
        "job",
        retry_job,
    ),
)
