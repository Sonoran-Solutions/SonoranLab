"""Pre-lease startup failures must not strand queued reservations."""

import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sonoranlab.jobs import manager, store, worker


def test_log_sink_failure_is_durable(jobs_env, monkeypatch):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("fast", {})["job_id"]
    path = store.job_dir(job_id)

    def fail(path):
        raise OSError("log sink initialization failed")

    monkeypatch.setattr(worker, "bounded_log", fail)
    assert worker.main([job_id]) == 1
    job = manager.get(job_id)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "worker_start_failed"
    assert job["worker"]["started_at"] is None
    assert not store.live(path)
    assert manager.retry_job(job_id)["state"] == "queued"


@pytest.mark.parametrize("launcher", ["popen", "systemd"])
@pytest.mark.parametrize("status", ["dead", "alive", "unknown", "grace"])
def test_prelease_worker_death_reconciled_after_grace(jobs_env, monkeypatch, launcher, status):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("fast", {})["job_id"]
    path = store.job_dir(job_id)
    with store.lock(path / "job.lock"):
        job = store.read(path)
        job["worker"].update(
            launcher=launcher,
            unit=f"sonoranlab-job-{job_id}",
            launch_pid=123456789,
            launch_attempted_at=(datetime.now(UTC) - timedelta(seconds=0 if status == "grace" else 60)).isoformat(),
        )
        store.save(path, job)
    if launcher == "systemd":

        def run(*args, **kwargs):
            if status == "unknown":
                raise subprocess.TimeoutExpired("systemctl", 5)
            return SimpleNamespace(returncode=0, stdout="active" if status == "alive" else "inactive")

        monkeypatch.setattr(manager.subprocess, "run", run)
    else:
        original = manager.Path.read_text

        def read_text(path, *args, **kwargs):
            if str(path) == "/proc/123456789/stat":
                if status == "unknown":
                    raise PermissionError("cannot inspect")
                if status == "alive":
                    return "123456789 (worker) S"
                raise FileNotFoundError(path)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(manager.Path, "read_text", read_text)
    job = manager.get(job_id)
    assert job["state"] == ("failed" if status == "dead" else "queued")
    if status == "dead":
        assert job["error"]["code"] == "worker_lost"
    assert job["worker"]["started_at"] is None
