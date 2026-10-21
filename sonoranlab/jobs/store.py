"""Locked records, durable replacement, and the execution state machine (stdlib only)."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sonoranlab.api.paths import _confine, resolve_output, state_root
from sonoranlab.api.registry import ToolError

SCHEMA = "sonoranlab.job/1"
JOB_ID = "job-\\d{8}T\\d{6}Z-[0-9a-f]{8}"
ACTIVE = {"queued", "running", "cancelling"}
TERMINAL = {"cancelled", "succeeded", "failed"}
TRANSITIONS = {
    "queued": {"running", "cancelling", "cancelled", "failed"},
    "running": {"cancelling", "succeeded", "failed", "blocked"},
    "cancelling": {"cancelled", "blocked", "failed"},
    "blocked": {"cancelled"},
    "cancelled": set(),
    "succeeded": set(),
    "failed": set(),
}
STAGES = {"prepare", "generate", "select_and_process", "validate_export", "check"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load(path: Path):
    """Internal records must be regular files; never block on a FIFO or follow a replaced symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Record is not a regular file")
        return json.load(stream)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path: Path, value: dict, *, exclusive: bool = False) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def directory(relative: str, *, create: bool = False) -> Path:
    root = state_root()
    if create:
        try:
            _confine(root, (root,))
        except FileNotFoundError:
            missing, parent = [], root
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for path in reversed(missing):
                try:
                    path.mkdir()
                except FileExistsError:
                    if not path.is_dir() or path.is_symlink():
                        raise
                fsync_dir(path.parent)
    path = root
    for part in Path(relative).parts:
        if part in {"..", ".", "/"}:
            raise ToolError("path_refused", "Invalid state path")
        path = path / part
        if path.is_symlink():
            raise ToolError("path_refused", "State directory symlinks are refused")
        if create and (not path.exists()):
            try:
                resolve_output(path).mkdir()
                fsync_dir(path.parent)
            except FileExistsError:
                pass
    return _confine(path, (root,))


def job_dir(job_id: str) -> Path:
    if not isinstance(job_id, str) or re.fullmatch(JOB_ID, job_id) is None:
        raise ToolError("invalid_params", "Invalid job ID", field="job_id")
    return directory(f"jobs/{job_id}")


@contextmanager
def lock(path: Path, *, nonblocking: bool = False):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield fd
    finally:
        os.close(fd)


def live(path: Path) -> bool:
    try:
        with lock(path / "lease.lock", nonblocking=True):
            return False
    except BlockingIOError:
        return True


def output(job: dict) -> Path:
    return directory(job["output_dir"])


def nonnegative(value, *, nullable=False) -> bool:
    return (value is None and nullable) or (type(value) in (int, float) and math.isfinite(value) and value >= 0)


def valid_error(value: dict) -> bool:
    return (
        {"code", "stage", "message", "retryable"} <= value.keys()
        and all(isinstance(value[key], str) for key in ("code", "stage", "message"))
        and type(value["retryable"]) is bool
    )


def read(path: Path) -> dict:
    """Never repair or overwrite a corrupt record. It is an operator-visible tombstone."""
    try:
        if path.joinpath("job.json").is_symlink():
            raise ValueError("Record symlink")
        job = load(path / "job.json")
        required = {
            "job_id",
            "kind",
            "project",
            "character",
            "anim",
            "params",
            "input_hashes",
            "request_fingerprint",
            "idempotency_key_sha256",
            "parent_job_id",
            "attempt",
            "gpu",
            "track",
            "need_gib",
            "created_at",
            "state",
            "stage",
            "history",
            "worker",
            "output_dir",
            "prompts",
            "timing",
            "result",
            "error",
            "cancel",
        }
        if not (isinstance(job, dict) and required <= job.keys()):
            raise ValueError("Invalid job record")
        if not (job["schema"] == SCHEMA and job["job_id"] == path.name):
            raise ValueError("Invalid job record")
        if not (job["state"] in TRANSITIONS and job["stage"] in STAGES):
            raise ValueError("Invalid job record")
        if not all(isinstance(job[k], dict) for k in ("params", "input_hashes", "worker", "timing")):
            raise ValueError("Invalid job record")
        if not all(isinstance(job[k], list) for k in ("history", "prompts")):
            raise ValueError("Invalid job record")
        if not (type(job["gpu"]) is bool and type(job["attempt"]) is int and (job["attempt"] >= 1)):
            raise ValueError("Invalid job record")
        if not all(
            isinstance(job[k], str) and re.fullmatch("[a-z0-9][a-z0-9_-]*", job[k])
            for k in ("kind", "project", "character", "anim")
        ):
            raise ValueError("Invalid job record")
        if not job["output_dir"] == f"artifacts/{job['project']}/{job['character']}/{job['anim']}/{job['job_id']}":
            raise ValueError("Invalid job record")
        if not {"launcher", "unit", "pid", "boot_id", "started_at"} <= job["worker"].keys():
            raise ValueError("Invalid job record")
        if not {"queue_wait_s", "run_s", "per-stage"} <= job["timing"].keys():
            raise ValueError("Invalid job record")
        if not isinstance(job["timing"]["per-stage"], dict):
            raise ValueError("Invalid job record")
        if not (job["cancel"] is None or isinstance(job["cancel"], dict)):
            raise ValueError("Invalid job record")
        if not (job["error"] is None or isinstance(job["error"], dict)):
            raise ValueError("Invalid job record")
        if not (job["result"] is None or isinstance(job["result"], dict)):
            raise ValueError("Invalid job record")
        if not all(
            isinstance(p, dict) and {"prompt_id", "submission", "resolution"} <= p.keys() for p in job["prompts"]
        ):
            raise ValueError("Invalid job record")
        if not (job["history"] and all(isinstance(e, dict) and e["state"] in TRANSITIONS for e in job["history"])):
            raise ValueError("Invalid job record")
        if not job["history"][-1]["state"] == job["state"]:
            raise ValueError("Invalid job record")
        worker, timing = job["worker"], job["timing"]
        if worker["launcher"] not in {"systemd", "popen"} or not (
            worker["pid"] is None or (type(worker["pid"]) is int and worker["pid"] > 0)
        ):
            raise ValueError("Invalid worker identity")
        if any(
            worker[key] is not None and not isinstance(worker[key], str) for key in ("unit", "boot_id", "started_at")
        ):
            raise ValueError("Invalid worker metadata")
        if any(job[key] != job["params"].get(key) for key in ("project", "character", "anim")):
            raise ValueError("Record/request identity mismatch")
        if not isinstance(job["request_fingerprint"], str) or not re.fullmatch(
            "[a-f0-9]{64}", job["request_fingerprint"]
        ):
            raise ValueError("Invalid fingerprint")
        if job["idempotency_key_sha256"] is not None and not re.fullmatch(
            "[a-f0-9]{64}", job["idempotency_key_sha256"]
        ):
            raise ValueError("Invalid key hash")
        if job["parent_job_id"] is not None and not re.fullmatch(JOB_ID, job["parent_job_id"]):
            raise ValueError("Invalid parent")
        if not isinstance(job["created_at"], str) or datetime.fromisoformat(job["created_at"]).tzinfo is None:
            raise ValueError("Invalid creation time")
        if job["track"] not in {None, "E", "C"} or not nonnegative(job["need_gib"], nullable=True):
            raise ValueError("Invalid budget")
        if any(not nonnegative(timing[key], nullable=True) for key in ("queue_wait_s", "run_s")) or any(
            key not in STAGES or not nonnegative(value) for key, value in timing["per-stage"].items()
        ):
            raise ValueError("Invalid timing")
        if job["cancel"] is not None and not (
            {"requested_at", "outcome"} <= job["cancel"].keys()
            and isinstance(job["cancel"]["requested_at"], str)
            and job["cancel"]["outcome"] in {None, "confirmed", "unconfirmed"}
        ):
            raise ValueError("Invalid cancellation")
        if job["error"] is not None and not valid_error(job["error"]):
            raise ValueError("Invalid error")
        for prompt in job["prompts"]:
            if not (prompt["prompt_id"] is None or isinstance(prompt["prompt_id"], str)) or not (
                isinstance(prompt["submission"], str)
                and prompt["resolution"] in {"unresolved", "result", "confirmed_stopped"}
            ):
                raise ValueError("Invalid prompt")
        previous = None
        for event in job["history"]:
            if not {"at", "from", "state", "stage", "error"} <= event.keys() or not (
                event["from"] == previous and event["stage"] in STAGES and isinstance(event["at"], str)
            ):
                raise ValueError("Invalid history")
            if (previous is None and event["state"] != "queued") or (
                previous is not None and event["state"] not in TRANSITIONS[previous]
            ):
                raise ValueError("Illegal history transition")
            previous = event["state"]
        canonical(job)  # Refuse non-finite JSON numbers too.
        return job
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        return {
            "job_id": path.name,
            "state": "blocked",
            "stage": "prepare",
            "history": [],
            "prompts": [],
            "error": {
                "code": "corrupt_record",
                "stage": "prepare",
                "message": "Unreadable or partial job.json; preserve this directory for inspection",
                "retryable": False,
            },
            "result": None,
            "timing": {"queue_wait_s": None, "run_s": None, "per-stage": {}},
        }


def sync_events(path: Path, job: dict) -> None:
    """Recover a missing/torn journal tail from job.json, under job.lock.

    Append only: a torn line must be a prefix of the next durable history event.
    Complete that prefix without removing or rewriting any existing journal bytes.
    """
    events = path / "events.jsonl"
    fd = os.open(events, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Journal is not a regular file")
        previous = stream.read()
        complete = previous.rfind(b"\n") + 1
        lines = previous[:complete].splitlines()
        if len(lines) > len(job["history"]) or any(
            line != canonical(event) for line, event in zip(lines, job["history"], strict=False)
        ):
            raise OSError("Journal does not match record history; preserve for operator inspection")
        if complete == len(previous) and len(lines) == len(job["history"]):
            return
        pending = b"".join(canonical(event) + b"\n" for event in job["history"][len(lines) :])
        tail = previous[complete:]
        if not pending.startswith(tail):
            raise OSError("Torn journal does not match record history; preserve for operator inspection")
        stream.seek(0, os.SEEK_END)
        stream.write(pending[len(tail) :])
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path)


def save(path: Path, job: dict) -> None:
    """Caller holds job.lock. History is authoritative if a crash splits the writes."""
    atomic(path / "job.json", job)
    sync_events(path, job)


def transition(job: dict, state: str, *, error: dict | None = None) -> None:
    old = job["state"]
    if state not in TRANSITIONS[old]:
        raise ValueError(f"Illegal job transition: {old} -> {state}")
    job.update(state=state, error=error)
    job["history"].append({"at": now(), "from": old, "state": state, "stage": job["stage"], "error": error})
