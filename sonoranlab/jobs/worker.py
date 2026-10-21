"""Detached, single-attempt worker: lease -> scheduler lock -> executor -> durable outcome."""

from __future__ import annotations

import fcntl
import os
import select
import signal
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime
from pathlib import Path

from sonoranlab.jobs import store
from sonoranlab.jobs.executors import JobCancelled, JobFailure, executor
from sonoranlab.jobs.manager import confirm, corrupt, discover, error, prober, unresolved

LOG_LIMIT = 1024 * 1024


# A separate sink retains the pipe reader if the worker is SIGKILLed while a guard
# is draining. A thread would die with the worker and give its children EPIPE.
LOG_PUMP = """
import os, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'ab', buffering=0) as log:
    os.write(1, b'R')
    while chunk := os.read(0, 8192):
        if os.fstat(log.fileno()).st_size + len(chunk) > 1024 * 1024:
            os.ftruncate(log.fileno(), 0)
        log.write(chunk)
"""


@contextmanager
def bounded_log(path: Path):
    """Bound inherited child stderr without tying a guard's pipe lifetime to this worker."""
    sink = subprocess.Popen(
        [sys.executable, "-c", LOG_PUMP, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        if not select.select([sink.stdout], [], [], 10)[0] or sink.stdout.read(1) != b"R":
            sink.kill()
            sink.wait()
            raise OSError("Worker log sink did not start")
        sink.stdout.close()
        previous = os.dup(2)
        os.dup2(sink.stdin.fileno(), 2)
        sink.stdin.close()
        try:
            yield
        finally:
            sys.stderr.flush()
            os.dup2(previous, 2)
            os.close(previous)
    finally:
        if sink.stdin and not sink.stdin.closed:
            sink.stdin.close()
        if sink.stdout and not sink.stdout.closed:
            sink.stdout.close()
        # Guard children may still own the pipe after an unexpected executor error.
        # The sink exits on EOF, independently of the worker, rather than breaking their stderr.
        with suppress(subprocess.TimeoutExpired):
            sink.wait(timeout=2)


def terminate_children(pid: int) -> None:
    """Forward TERM even if sent only to the worker. Do not raise through subprocess.run's drain/unwind."""
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return
    for child in children:
        child_pid = int(child)
        with suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGTERM)


class Context:
    """Executors check cancellation between stages; guarded children receive TERM themselves."""

    def __init__(self, job: dict):
        self.job_id = job["job_id"]
        self.path = store.job_dir(self.job_id)
        self.output_dir = store.output(job)
        self.cancelled = False
        self.stage_started = time.monotonic()
        self.current_stage = job["stage"]
        self.stage_times = dict(job["timing"]["per-stage"])

    def term(self, _signum, _frame):
        if not self.cancelled:
            self.cancelled = True
            terminate_children(os.getpid())

    def check_cancelled(self):
        with store.lock(self.path / "job.lock"):
            job = store.read(self.path)
            if corrupt(job):
                raise JobFailure("corrupt_record", self.current_stage, "Job record is corrupt; preserve it")
        if self.cancelled or job["state"] in {"cancelling", "cancelled"}:
            self.cancelled = True
            raise JobCancelled

    def timings(self) -> dict:
        return {
            **self.stage_times,
            self.current_stage: self.stage_times.get(self.current_stage, 0) + time.monotonic() - self.stage_started,
        }

    def set_stage(self, stage: str) -> None:
        if stage not in store.STAGES:
            raise ValueError(f"Invalid stage: {stage}")
        self.check_cancelled()
        self.stage_times = self.timings()
        self.current_stage, self.stage_started = stage, time.monotonic()
        with store.lock(self.path / "job.lock"):
            job = store.read(self.path)
            if corrupt(job):
                raise JobFailure("corrupt_record", self.current_stage, "Job record is corrupt; preserve it")
            job["stage"] = stage
            job["timing"]["per-stage"] = self.stage_times
            store.save(self.path, job)

    def run(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Optional executor helper; it preserves the guard's refusal and busy exit codes."""
        self.check_cancelled()
        check = kwargs.pop("check", False)
        result = subprocess.run(argv, check=False, **kwargs)
        self.check_cancelled()
        if result.returncode in {3, 4}:
            raise JobFailure(
                "budget_refused" if result.returncode == 3 else "guard_busy",
                self.current_stage,
                f"Guard exited {result.returncode}; nothing submitted",
                True,
            )
        if check:
            result.check_returncode()
        return result


def _finish(path: Path, ctx: Context, result: dict | None, failure: dict | None, started: float | None) -> None:
    with store.lock(path / "job.lock"):
        job = store.read(path)
        if corrupt(job):
            return
        if job["timing"]["queue_wait_s"] is None:
            job["timing"]["queue_wait_s"] = max(
                0, (datetime.fromisoformat(store.now()) - datetime.fromisoformat(job["created_at"])).total_seconds()
            )
        job["timing"]["run_s"] = time.monotonic() - started if started is not None else None
        job["timing"]["per-stage"] = ctx.timings() if started is not None else {}
        job["prompts"] = discover(job)
        cancelling = ctx.cancelled or job["state"] == "cancelling"
        if cancelling:
            if job["state"] in {"queued", "running"}:
                store.transition(job, "cancelling")
            job["cancel"] = job["cancel"] or {"requested_at": store.now(), "outcome": None}
            store.save(path, job)
    # The lease and scheduler lock stay held, but job.lock is free during network cleanup.
    try:
        stopped = not unresolved(job) or confirm(job, prober(), cancel=True)
    except Exception:
        stopped = False
    measured = {}
    # Preserve the durable outcome even if partial executor metadata is unreadable.
    with suppress(Exception):
        measured = executor(job["kind"]).stage({**job, "stage": ctx.current_stage, "result": result})
    with store.lock(path / "job.lock"):
        current = store.read(path)
        if corrupt(current):
            return
        current["prompts"] = job["prompts"]
        current["timing"] = job["timing"]
        current["result"] = result
        stage = measured.get("stage")
        current["stage"] = stage if stage in store.STAGES else ctx.current_stage
        timing = measured.get("timing") or {}
        if measured_times := {k: v for k, v in timing.items() if k in store.STAGES and store.nonnegative(v)}:
            current["timing"]["per-stage"] = measured_times
        if cancelling or ctx.cancelled or current["state"] == "cancelling":
            if current["state"] in {"queued", "running"}:
                store.transition(current, "cancelling")
            current["cancel"] = current["cancel"] or {"requested_at": store.now(), "outcome": None}
            current["cancel"]["outcome"] = "confirmed" if stopped else "unconfirmed"
            state = "cancelled" if stopped else "blocked"
            outcome = None if stopped else error("cancel_unconfirmed", current, "Prompt stop could not be confirmed")
        else:
            state = ("failed" if failure else "succeeded") if stopped else "blocked"
            outcome = (
                failure if stopped else error("needs_reconciliation", current, "Prompt stop could not be confirmed")
            )
        if failure is not None and not stopped:
            outcome["detail"] = failure
        store.transition(current, state, error=outcome)
        store.save(path, current)


def _work(path: Path) -> int:
    with ExitStack() as leases:
        with store.lock(path / "job.lock"):
            # Publish lease ownership and initialized signal identity in one record-lock transaction.
            leases.enter_context(store.lock(path / "lease.lock", nonblocking=True))
            job = store.read(path)
            if corrupt(job) or job["state"] != "queued":
                return 0  # A started attempt is NEVER run again, even by an explicitly invoked worker.
            ctx = Context(job)
            old_signals = {sig: signal.signal(sig, ctx.term) for sig in (signal.SIGTERM, signal.SIGHUP)}
            job["worker"].update(
                pid=os.getpid(),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                started_at=store.now(),
                start_ticks=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
            )
            store.save(path, job)
        gpu_fd, started = None, None
        result, failure = None, None
        try:
            try:
                implementation = executor(job["kind"])
                ctx.check_cancelled()
                if job["gpu"] or getattr(implementation, "heavy_cpu", False):
                    gpu_fd = os.open(store.directory("") / "gpu.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    while True:
                        ctx.check_cancelled()
                        try:
                            fcntl.flock(gpu_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(1)
                ctx.check_cancelled()
                with store.lock(path / "job.lock"):
                    job = store.read(path)
                    if job["state"] != "queued":
                        ctx.cancelled = True
                        raise JobCancelled
                    queued = datetime.fromisoformat(job["created_at"])
                    job["timing"]["queue_wait_s"] = max(
                        0, (datetime.fromisoformat(store.now()) - queued).total_seconds()
                    )
                    store.transition(job, "running")
                    store.save(path, job)
                ctx.stage_started = started = time.monotonic()
                result = implementation.run(job, ctx)
                if not isinstance(result, dict):
                    raise TypeError("Executor result must be an object")
                store.canonical(result)
            except JobCancelled:
                ctx.cancelled = True
            except JobFailure as exc:
                failure = exc.error
            except BaseException as exc:
                traceback.print_exc()
                failure = error("executor_error", job, type(exc).__name__)
            _finish(path, ctx, result, failure, started)
        finally:
            if gpu_fd is not None:
                os.close(gpu_fd)
            for sig, handler in old_signals.items():
                signal.signal(sig, handler)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    path = store.job_dir(args[0])
    try:
        with bounded_log(path / "worker.log"):
            return _work(path)
    except BlockingIOError:
        return 0
    except Exception as exc:
        with store.lock(path / "job.lock"):
            job = store.read(path)
            if not corrupt(job) and job["state"] == "queued" and not store.live(path):
                store.transition(job, "failed", error=error("worker_start_failed", job, type(exc).__name__, True))
                store.save(path, job)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
