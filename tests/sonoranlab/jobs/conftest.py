import os
import signal
import time
from pathlib import Path

import pytest

from sonoranlab.jobs import manager, store


@pytest.fixture
def jobs_env(monkeypatch, tmp_path):
    fake_modules = Path(__file__).parent
    monkeypatch.syspath_prepend(str(fake_modules))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(fake_modules), str(Path.cwd()), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.setenv("SONORANLAB_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SONORANLAB_JOB_LAUNCHER", "popen")
    monkeypatch.setenv("SONORANLAB_JOB_EXECUTORS", "job_fakes")
    yield tmp_path / "state"
    # Always release file gates, then stop any remaining workers on assertion failures.
    root = tmp_path / "state"
    if root.exists():
        for path in root.glob("artifacts/*/*/*/*"):
            for name in ("release", "release-boundary"):
                (path / name).touch()
        (root / "fake" / "probe-release").touch() if (root / "fake").exists() else None
        for job in manager.reconcile_all():
            if job["state"] in store.ACTIVE:
                manager.cancel_job(job["job_id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(store.live(path) for path in root.glob("jobs/job-*")):
                break
            time.sleep(0.01)
        for path in root.glob("jobs/job-*"):
            if store.live(path):
                job = store.read(path)
                if not manager.corrupt(job) and (pid := job["worker"]["pid"]):
                    os.killpg(pid, signal.SIGKILL)
    for child in manager._CHILDREN:
        child.poll()
