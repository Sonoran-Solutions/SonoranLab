"""Durable submission, offline reconciliation, and prompt-scoped cancellation."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sonoranlab.api.paths import resolve_output, state_root
from sonoranlab.api.registry import ToolError
from sonoranlab.jobs import store
from sonoranlab.jobs.executors import JobFailure, executor


class Prober(Protocol):
    def status(self, prompt_id: str) -> str: ...
    def cancel(self, prompt_id: str) -> bool: ...


class ComfyProber:
    """Only explicit cancel/retry uses this transport. Failed container inspection is never absence."""

    def __init__(self):
        client = importlib.import_module("sonoranlab.comfy.client")

        self.container = "sonoranlab-comfy"
        self.podman = shlex.split(os.environ.get("SONORANLAB_PODMAN", "podman"))
        self.client = client.ComfyClient(
            "http://127.0.0.1:8188",
            transport=client.PodmanExecTransport(self.container, podman=self.podman),
            http_timeout=5,
            output_root=state_root() / "comfy-output",
        )

    def stopped(self) -> bool:
        proc = subprocess.run(
            [*self.podman, "container", "exists", self.container],
            capture_output=True,
            timeout=15,
        )
        if proc.returncode == 1:
            return True
        if proc.returncode != 0:
            raise OSError("Container existence could not be confirmed")
        proc = subprocess.run(
            [*self.podman, "inspect", "-f", "{{.State.Status}}", self.container],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            raise OSError("Container state could not be confirmed")
        return proc.stdout.strip() in {"exited", "stopped", "created", "configured"}

    def status(self, prompt_id: str) -> str:
        return "stopped" if self.stopped() else self.client.status(prompt_id)

    def cancel(self, prompt_id: str) -> bool:
        return self.stopped() or self.client.cancel(prompt_id)


def prober() -> Prober:
    if module := os.environ.get("SONORANLAB_JOB_EXECUTORS"):
        factory = getattr(importlib.import_module(module), "Prober", None)
        if factory:
            return factory()
    return ComfyProber()


def corrupt(job: dict) -> bool:
    return (job.get("error") or {}).get("code") == "corrupt_record"


def discover(job: dict) -> list[dict]:
    """Submission files, never the server queue, determine ownership. Unreadable files fail closed."""
    root = store.output(job)
    previous = {p["submission"]: p for p in job["prompts"]}
    prompts = []
    for path in sorted(root.rglob("submission.json")):
        relative = path.relative_to(root).as_posix()
        resolution, prompt_id = "unresolved", None
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Escaping submission")
            submission = store.load(path)
            prompt_id = submission["prompt_id"]
            if not isinstance(prompt_id, str) or not prompt_id or len(prompt_id) > 200:
                raise ValueError("Invalid prompt ID")
            result = path.parent / "result.json"
            if result.is_file() and not result.is_symlink():
                value = store.load(result)
                if isinstance(value, dict) and (value.get("error") or {}).get("code") not in {
                    "cancel_unconfirmed",
                    "submit_unknown",
                }:
                    resolution = "result"
            old = previous.get(relative, {})
            if old.get("prompt_id") == prompt_id and old.get("resolution") == "confirmed_stopped":
                resolution = "confirmed_stopped"
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        prompts.append({"prompt_id": prompt_id, "submission": relative, "resolution": resolution})
    # A missing submission must not erase a previously observed, unresolved prompt.
    seen = {p["submission"] for p in prompts}
    prompts.extend({**p, "missing_submission": True} for p in job["prompts"] if p["submission"] not in seen)
    return prompts


def unresolved(job: dict) -> list[dict]:
    return [p for p in job["prompts"] if p["resolution"] == "unresolved"]


def error(code: str, job: dict, message: str, retryable: bool = False) -> dict:
    return {"code": code, "stage": job["stage"], "message": message, "retryable": retryable}


def _reconcile(path: Path, job: dict) -> dict:
    if corrupt(job):
        return job
    if job["state"] in {"running", "cancelling"} and not store.live(path):
        try:
            job["prompts"] = discover(job)
            pending = unresolved(job)
        except (OSError, ToolError):
            # Missing/unreadable artifacts are not evidence that a submitted prompt stopped.
            pending = [{"prompt_id": None, "submission": "unreadable output_dir"}]
        if pending:
            message = "Unresolved prompts: " + ", ".join(p["prompt_id"] or p["submission"] for p in pending)
            store.transition(job, "blocked", error=error("needs_reconciliation", job, message))
        else:
            store.transition(job, "failed", error=error("worker_lost", job, "Worker lease is no longer held"))
        store.save(path, job)
    return job


def get(job_id: str) -> dict:
    path = store.job_dir(job_id)
    with store.lock(path / "job.lock"):
        job = _reconcile(path, store.read(path))
        if not corrupt(job):
            with suppress(OSError, ToolError):
                prompts = discover(job)
                if prompts != job["prompts"]:
                    job["prompts"] = prompts
                    store.save(path, job)
            store.sync_events(path, job)
        return job


def reconcile_all() -> list[dict]:
    try:
        root = store.directory("jobs")
    except FileNotFoundError:
        return []
    jobs = []
    for path in sorted(root.iterdir(), reverse=True):
        if re.fullmatch(store.JOB_ID, path.name) and path.is_dir() and not path.is_symlink():
            jobs.append(get(path.name))
    return jobs


def _launcher() -> str:
    choice = os.environ.get("SONORANLAB_JOB_LAUNCHER")
    if choice:
        if choice not in {"systemd", "popen"}:
            raise ToolError("invalid_params", "SONORANLAB_JOB_LAUNCHER must be systemd|popen")
        return choice
    try:
        proc = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--", "true"],
            capture_output=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return "systemd"
    except (OSError, subprocess.SubprocessError):
        pass
    return "popen"


# Keep Popen handles until reaped, without ever waiting for job completion at submission.
_CHILDREN: list[subprocess.Popen] = []


def launch(job: dict) -> None:
    argv = [sys.executable, "-m", "sonoranlab.jobs.worker", job["job_id"]]
    if job["worker"]["launcher"] == "systemd":
        # An ordinary service, outside the MCP/desktop app's scope. Explicitly carry state and test hooks.
        env = [
            f"--setenv={key}={value}"
            for key, value in os.environ.items()
            if key.startswith("SONORANLAB_") or key in {"PYTHONPATH", "PATH"}
        ]
        proc = subprocess.run(
            [
                "systemd-run",
                "--user",
                f"--unit={job['worker']['unit']}",
                "--collect",
                "--quiet",
                # Stopping the unit TERMs every process; runwindow must finish draining (--drain-s 1800) and restoring
                # paused services before systemd escalates to SIGKILL (default 90 s would strand paused units).
                "--property=TimeoutStopSec=1900",
                f"--working-directory={Path.cwd()}",
                *env,
                "--",
                *argv,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode:
            raise OSError("systemd-run could not launch job worker")
    else:
        _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]
        path = store.job_dir(job["job_id"])
        fd = os.open(path / "worker.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as log:
            _CHILDREN.append(
                subprocess.Popen(
                    argv,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                )
            )


def submit(
    kind: str,
    params: dict,
    idempotency_key: str | None = None,
    *,
    parent_job_id: str | None = None,
    attempt: int = 1,
    frozen_hashes: dict | None = None,
) -> dict:
    implementation = executor(kind)
    if not isinstance(params, dict):
        raise ToolError("invalid_params", "Job params must be an object")
    # Copy through canonical JSON so neither validation nor the caller can mutate the frozen request.
    supplied = json.loads(store.canonical(params))
    supplied.pop("idempotency_key", None)
    try:
        normalized, hashes = implementation.validate(supplied)
    except JobFailure as exc:
        raise ToolError("invalid_params", str(exc)) from exc
    normalized, hashes = json.loads(store.canonical(normalized)), json.loads(store.canonical(hashes))
    if not isinstance(normalized, dict) or not isinstance(hashes, dict):
        raise ValueError("Executor validation must return params and input hashes objects")
    normalized.pop("idempotency_key", None)
    if frozen_hashes is not None and (normalized != supplied or hashes != frozen_hashes):
        raise ToolError("conflict", "Frozen request inputs changed; submit a fresh request explicitly")
    for key in ("project", "character", "anim"):
        if not isinstance(normalized.get(key), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", normalized[key]):
            raise ToolError("invalid_params", f"Invalid {key}", field=key)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", kind):
        raise ToolError("invalid_params", "Invalid kind", field="kind")
    need = implementation.need_gib(normalized)
    if need is not None and (type(need) not in (int, float) or not math.isfinite(need) or need < 0):
        raise ToolError("invalid_params", "Invalid executor memory requirement")
    if normalized.get("track") is not None:
        minimum = importlib.import_module("sonoranlab.comfy.guard").min_need_gib(normalized["track"])
        if need is None or need < minimum:
            raise ToolError("invalid_params", "Executor memory requirement is below the track minimum")
    fingerprint = hashlib.sha256(
        store.canonical({"kind": kind, "params": normalized, "input_hashes": hashes})
    ).hexdigest()
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
        raise ToolError("invalid_params", "Idempotency key must be a nonempty string", field="idempotency_key")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key is not None else None
    launcher = _launcher()
    root = store.directory("jobs", create=True)
    # Serialize the small reservation transaction; never hold this lock while work executes.
    with store.lock(root / ".submit.lock"):
        if key_hash:
            claims = store.directory("idempotency", create=True)
            claim = claims / f"{key_hash}.json"
            if claim.is_symlink():
                raise ToolError("path_refused", "Idempotency claim symlink is refused")
            if claim.exists():
                try:
                    existing = store.load(claim)
                    if existing["request_fingerprint"] != fingerprint:
                        raise ToolError("idempotency_conflict", "Key already names a different request")
                    return summary(get(existing["job_id"]))
                except (ValueError, KeyError, FileNotFoundError) as exc:
                    raise ToolError(
                        "conflict", "Idempotency reservation is incomplete; inspect it, never replay"
                    ) from exc
        else:
            for old in reconcile_all():
                if old.get("request_fingerprint") == fingerprint and old["state"] in store.ACTIVE:
                    raise ToolError("conflict", f"Identical request already active: {old['job_id']}")
        job_id = f"job-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        if key_hash:
            store.atomic(claim, {"job_id": job_id, "request_fingerprint": fingerprint}, exclusive=True)
        path = resolve_output(root / job_id)
        path.mkdir()
        store.fsync_dir(root)
        output_parent = store.directory(
            f"artifacts/{normalized['project']}/{normalized['character']}/{normalized['anim']}",
            create=True,
        )
        out = resolve_output(output_parent / job_id)
        out.mkdir()
        store.fsync_dir(output_parent)
        created = store.now()
        job = {
            "schema": store.SCHEMA,
            "job_id": job_id,
            "kind": kind,
            **{key: normalized[key] for key in ("project", "character", "anim")},
            "params": normalized,
            "input_hashes": hashes,
            "request_fingerprint": fingerprint,
            "idempotency_key_sha256": key_hash,
            "parent_job_id": parent_job_id,
            "attempt": attempt,
            "gpu": bool(implementation.gpu),
            "track": normalized.get("track"),
            "need_gib": need,
            "created_at": created,
            "state": "queued",
            "stage": "prepare",
            "history": [{"at": created, "from": None, "state": "queued", "stage": "prepare", "error": None}],
            "worker": {
                "launcher": launcher,
                "unit": f"sonoranlab-job-{job_id}" if launcher == "systemd" else None,
                "pid": None,
                "boot_id": None,
                "started_at": None,
            },
            "output_dir": out.relative_to(state_root()).as_posix(),
            "prompts": [],
            "timing": {"queue_wait_s": None, "run_s": None, "per-stage": {}},
            "result": None,
            "error": None,
            "cancel": None,
        }
        with store.lock(path / "job.lock"):
            store.atomic(
                path / "request.json", {"kind": kind, "params": normalized, "input_hashes": hashes}, exclusive=True
            )
            # Create the lease inode before launching, but only the worker holds it.
            with store.lock(path / "lease.lock"):
                pass
            store.save(path, job)
        try:
            launch(job)
        except (OSError, subprocess.SubprocessError) as exc:
            with store.lock(path / "job.lock"):
                current = store.read(path)
                # A launcher timeout can mean the worker DID start; never overwrite it or launch again.
                if current["state"] == "queued" and not store.live(path):
                    store.transition(
                        current, "failed", error=error("launcher_failed", current, type(exc).__name__, True)
                    )
                    store.save(path, current)
        return summary(get(job_id))


def summary(job: dict) -> dict:
    return {"job_id": job["job_id"], "state": job["state"]}


def confirm(job: dict, probe: Prober, *, cancel: bool) -> bool:
    job["prompts"] = discover(job)
    for prompt in unresolved(job):
        prompt_id = prompt["prompt_id"]
        if not prompt_id or prompt.get("missing_submission"):
            continue  # Retained history prevents false resolution but cannot authorize a prompt cancellation.
        try:
            if cancel:
                probe.cancel(prompt_id)
            if probe.status(prompt_id) in {"done", "unknown", "stopped"}:
                prompt["resolution"] = "confirmed_stopped"
        except Exception:
            # Even cancel_unconfirmed is retained as unknown; a later explicit cancel may resolve it.
            continue
    return not unresolved(job)


def _signal(job: dict) -> bool:
    worker = job["worker"]
    if worker["launcher"] == "systemd":
        subprocess.run(
            ["systemctl", "--user", "kill", "--signal=SIGTERM", f"sonoranlab-job-{job['job_id']}"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    elif worker["pid"] is not None:
        # Lease + boot + process start time + process-group identity protect against stale/reused PIDs.
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        ticks = Path(f"/proc/{worker['pid']}/stat").read_text().rsplit(")", 1)[1].split()[19]
        if (
            worker["boot_id"] != boot_id
            or worker.get("start_ticks") != ticks
            or os.getpgid(worker["pid"]) != worker["pid"]
        ):
            raise OSError("Worker process identity no longer matches")
        os.killpg(worker["pid"], signal.SIGTERM)
    else:
        return False  # The worker will see the durable request before it can start.
    return True


def cancel_job(job_id: str, *, probe: Prober | None = None) -> dict:
    path = store.job_dir(job_id)
    # Serialize explicit cancel/probe operations, but never block status on network calls.
    with store.lock(path / "cancel.lock"):
        with store.lock(path / "job.lock"):
            job = _reconcile(path, store.read(path))
            if corrupt(job) or job["state"] in store.TERMINAL:
                return summary(job)
            live = store.live(path) and job["state"] != "blocked"
            if job["state"] == "queued" and not live:
                job["cancel"] = {"requested_at": store.now(), "outcome": "confirmed"}
                store.transition(job, "cancelled")
                store.save(path, job)
                return summary(job)
            if job["cancel"] is None:
                job["cancel"] = {"requested_at": store.now(), "outcome": None}
            if job["state"] in {"queued", "running"}:
                store.transition(job, "cancelling")
            store.save(path, job)
        if live:
            # Keep the record lock free even if systemctl is slow. The durable request also covers startup races.
            if not job["cancel"].get("signal_sent_at"):
                with suppress(OSError, subprocess.SubprocessError):
                    if _signal(job):
                        with store.lock(path / "job.lock"):
                            latest = store.read(path)
                            if not corrupt(latest) and latest["cancel"] is not None:
                                latest["cancel"]["signal_sent_at"] = store.now()
                                store.save(path, latest)
            return summary(job)
        # No worker can submit more work. Scope every operation to this job's durable submission IDs.
        try:
            confirmed = confirm(job, probe or prober(), cancel=True)
        except (OSError, ToolError):
            confirmed = False
        with store.lock(path / "job.lock"):
            if confirmed:
                job["cancel"]["outcome"] = "confirmed"
                store.transition(job, "cancelled")
            else:
                job["cancel"]["outcome"] = "unconfirmed"
                failure = error("cancel_unconfirmed", job, "Prompt stop could not be confirmed")
                if job["state"] != "blocked":
                    store.transition(job, "blocked", error=failure)
                else:
                    job["error"] = failure
            store.save(path, job)
        return summary(job)


def retry_job(job_id: str, idempotency_key: str | None = None, *, probe: Prober | None = None) -> dict:
    path = store.job_dir(job_id)
    # cancel.lock prevents retry confirmation racing an explicit cancel operation.
    with store.lock(path / "cancel.lock"):
        job = get(job_id)
        if corrupt(job) or job["state"] not in {"failed", "cancelled", "blocked"}:
            raise ToolError("conflict", "Only failed, cancelled or resolved blocked jobs can be retried")
        if job["state"] == "blocked":
            if store.live(path):
                raise ToolError("conflict", "Blocked job still has a live worker")
            if not confirm(job, probe or prober(), cancel=False):
                raise ToolError("conflict", "Parent still has unresolved prompts; cancel/reconcile it first")
            with store.lock(path / "job.lock"):
                store.save(path, job)
        try:
            if (path / "request.json").is_symlink():
                raise ValueError("Frozen request symlink")
            frozen = store.load(path / "request.json")
            if frozen != {"kind": job["kind"], "params": job["params"], "input_hashes": job["input_hashes"]}:
                raise ValueError("Frozen request mismatch")
        except (OSError, ValueError) as exc:
            raise ToolError("conflict", "Frozen request is missing or corrupt") from exc
        return submit(
            frozen["kind"],
            frozen["params"],
            idempotency_key,
            parent_job_id=job_id,
            attempt=job["attempt"] + 1,
            frozen_hashes=frozen["input_hashes"],
        )
