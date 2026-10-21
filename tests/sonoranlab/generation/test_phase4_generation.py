"""Generation must use the manager's reconciliation and replay boundary."""

import json
from pathlib import Path

import pytest
import test_generation as fixtures
from test_animate import Fake
from test_generation import execute, submit

from sonoranlab.api import call
from sonoranlab.jobs import manager, store

setup = fixtures.setup


def test_native_animate_review_follows_final_job_and_reads_current_evidence(setup, monkeypatch):
    from test_review import PASS

    from sonoranlab.review import record_review

    ctx, *_ = setup
    job = execute(submit(setup), Fake(), monkeypatch)
    identifier = job["job_id"]
    final = Path(job["result"]["animate"]["verdict"]["job"])
    assert call("check_review", {"job": identifier}, ctx=ctx)["result"]["review_state"] == "none"
    record_review(final, **PASS)
    for tool in ("check_review", "job_status", "job_result"):
        response = call(tool, {"job" if tool == "check_review" else "job_id": identifier}, ctx=ctx)
        assert response["ok"], response
        result = response["result"]
        human = (
            result if tool == "check_review" else result["human"] if tool == "job_status" else result["result"]["human"]
        )
        assert human["production_usable"]
    (final / "review.json").unlink()
    assert not call("check_review", {"job": identifier}, ctx=ctx)["result"]["production_usable"]
    doc_path = store.output(job) / "animate/animate.json"
    doc = store.load(doc_path)
    doc["verdict"]["job"] = str(final.parent.parent.parent.parent)
    store.atomic(doc_path, doc)
    assert call("check_review", {"job": identifier}, ctx=ctx)["error"]["code"] == "path_refused"


def test_animate_measured_stage_timings_and_compact_envelopes(setup, monkeypatch):
    ctx, *_ = setup
    job = execute(submit(setup), Fake(), monkeypatch)
    assert job["stage"] == "validate_export"
    assert job["timing"]["per-stage"] == {
        key: value for key, value in job["result"]["animate"]["timing"].items() if key in store.STAGES
    }
    params = {"job_id": job["job_id"]}
    for tool in ("job_status", "job_result"):
        envelope = call(tool, params, ctx=ctx)
        assert envelope["ok"], envelope
        assert len(json.dumps(envelope).encode()) < 64 * 1024
        result = envelope["result"]
        if tool == "job_status":
            assert result["stage"] == result["progress"]["stage"] == "validate_export"
            assert "result" not in result and "result_summary" in result
        else:
            assert len(result["artifacts"]) <= 200
            assert result["result"]["animate"]["verdict"]["job"].startswith(job["job_id"] + ":")


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
