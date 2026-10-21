"""Job tools share the CLI/MCP envelope; reads are strictly offline."""

from __future__ import annotations

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


def job_status(params: dict, ctx: Context) -> dict:
    job = manager.get(params["job_id"])
    progress = {}
    technical = {"validator": None, "godot": "not_run"}
    human = {"review": "none", "production_usable": False}
    if not manager.corrupt(job):
        # Executor.stage must measure files only; it must not probe services or launch processes.
        try:
            progress = executor(job["kind"]).stage(job)
        except (ToolError, OSError, ValueError):
            progress = {}
        root = store.output(job)
        from sonoranlab.registry import _review

        review = _review(root, ctx)
        human = {"review": review["review_state"], "production_usable": review["production_usable"]}
    # An executor that knows where its final artifact lives (animate: sweep/final/job) reports measured verdicts.
    technical = progress.pop("technical", technical)
    human = progress.pop("human", human)
    return {**job, "progress": progress, "technical": technical, "human": human}


def job_result(params: dict, ctx: Context) -> dict:
    job = manager.get(params["job_id"])
    if job["state"] not in store.TERMINAL:
        raise ToolError("not_ready", "Job result is not ready; poll job_status", field="job_id", retryable=True)
    root = store.output(job)
    artifacts = [
        {
            "ref": f"{job['job_id']}:{path.relative_to(root).as_posix()}",
            "path": path.relative_to(store.state_root()).as_posix(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
    ]
    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "result": job["result"],
        "error": job["error"],
        "artifacts": artifacts,
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
        "Read a completed job result and STATE-relative artifact references.",
        schema({"job_id": ID}, ("job_id",)),
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

# ponytail: startup reconciliation runs at first registry discovery; move to create_server when it enters scope.
manager.reconcile_all()
