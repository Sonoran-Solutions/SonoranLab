"""Generation must use the manager's reconciliation and replay boundary."""

import pytest
import test_generation as fixtures
from test_generation import submit

from sonoranlab.api import call
from sonoranlab.jobs import manager, store

setup = fixtures.setup


@pytest.mark.parametrize("state", ["blocked", "failed", "cancelled"])
def test_identical_pose_with_running_prompt_refused(setup, state):
    ctx, _, pose, _ = setup
    job = submit(setup, "pose_frame")
    path = store.job_dir(job["job_id"])
    root = store.output(job)
    store.atomic(root / "submission.json", {"prompt_id": "still-running"})
    with store.lock(path / "job.lock"):
        job = store.read(path)
        store.transition(job, "running")
        if state == "cancelled":
            store.transition(job, "cancelling")
        store.transition(job, state)
        store.save(path, job)
    response = call("pose_frame", pose, ctx=ctx)
    assert not response["ok"], response
    assert response["error"]["code"] == "conflict"
    assert job["job_id"] in response["error"]["message"]
    assert "cancel/reconcile" in response["error"]["message"]
    assert len(manager.reconcile_all()) == 1


@pytest.mark.parametrize("kind", ["animate", "pose_frame"])
def test_generation_replay_requires_retained_record(setup, kind):
    ctx, animate, pose, _ = setup
    params = (animate if kind == "animate" else pose) | {"idempotency_key": "retained"}
    first = call(kind, params, ctx=ctx)
    assert first["ok"], first
    path = store.job_dir(first["result"]["job_id"])
    (path / "job.json").unlink()
    response = call(kind, params, ctx=ctx)
    assert not response["ok"], response
    assert response["error"]["code"] == "conflict"
    assert "reservation is incomplete" in response["error"]["message"]
    assert not (path / "job.json").exists()
