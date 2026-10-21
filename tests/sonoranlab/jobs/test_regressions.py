"""Review regressions: prompt ownership, reservation ordering, and corrupt evidence."""

import hashlib
import json
import os
import signal
import sys

import pytest
from test_jobs import events, out, process_result, state, stop_worker, submit_process, wait, wait_path

from sonoranlab.api import call
from sonoranlab.api.registry import ToolError
from sonoranlab.jobs import manager, store, worker
from sonoranlab.jobs.executors import JobFailure


@pytest.mark.parametrize("unfinished", ["return", "failure", "raise", "cancel_unconfirmed", "submit_unknown"])
@pytest.mark.parametrize("stops", [True, False])
def test_finish_resolves_prompts_before_terminal_state(jobs_env, unfinished, stops):
    fake = jobs_env / "fake"
    fake.mkdir(parents=True)
    if not stops:
        (fake / "still-running").touch()
    job_id = manager.submit("fake-gpu", {"instant": True, "unfinished": unfinished})["job_id"]
    job = state(job_id, {"succeeded", "failed", "blocked"})
    wait(lambda: not store.live(store.job_dir(job_id)))
    failed = unfinished in {"failure", "raise"}
    assert job["state"] == (("failed" if failed else "succeeded") if stops else "blocked")
    assert any(e["event"] == "cancel" and e["prompt_id"] == job["prompts"][0]["prompt_id"] for e in events(jobs_env))
    if not stops:
        assert job["error"]["code"] == "needs_reconciliation"
        if failed:
            assert job["error"]["detail"]["code"] == ("fake_failure" if unfinished == "failure" else "executor_error")
        else:
            assert job["result"] == {"complete": True, "partial": True}


def test_finish_probe_keeps_gpu_and_lease_but_releases_record_lock(jobs_env):
    fake = jobs_env / "fake"
    fake.mkdir(parents=True)
    (fake / "probe-hold").touch()
    first = manager.submit("fake-gpu", {"instant": True, "unfinished": "return"})["job_id"]
    wait_path(fake / "probe-ready")
    assert store.live(store.job_dir(first))
    with store.lock(store.job_dir(first) / "job.lock", nonblocking=True):
        assert store.read(store.job_dir(first))["state"] == "running"
    with pytest.raises(BlockingIOError), store.lock(jobs_env / "gpu.lock", nonblocking=True):
        pass
    second = process_result(submit_process({"kind": "fake-gpu", "tag": "second"}))["job_id"]
    wait(lambda: manager.get(second)["worker"]["started_at"] is not None)
    assert manager.get(second)["state"] == "queued"
    assert not (out(second) / "submitted").exists()
    (fake / "probe-release").touch()
    state(first, {"succeeded"})
    wait_path(out(second) / "submitted")


@pytest.mark.parametrize("parent_state", ["failed", "cancelled", "blocked"])
def test_retry_probes_unresolved_prompts_for_every_parent(jobs_env, monkeypatch, parent_state):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("fake-gpu", {})["job_id"]
    path = store.job_dir(job_id)
    root = out(job_id)
    (root / "gen").mkdir()
    store.atomic(root / "gen/submission.json", {"prompt_id": "owned"})
    fake = jobs_env / "fake"
    fake.mkdir()
    store.atomic(fake / "owned.json", {"state": "running"})
    with store.lock(path / "job.lock"):
        job = store.read(path)
        store.transition(job, "running")
        if parent_state == "cancelled":
            store.transition(job, "cancelling")
        store.transition(job, parent_state)
        store.save(path, job)
    with pytest.raises(ToolError, match="unresolved"):
        manager.retry_job(job_id)
    assert len(list((jobs_env / "jobs").glob("job-*"))) == 1
    assert not events(jobs_env)  # Retry probes; it never cancels or submits.


def test_cancel_signals_main_and_forwards_once_to_direct_children(jobs_env, tmp_path):
    # The child models runwindow: it forwards once, then drains the grandchild's cleanup.
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import signal,time\nfrom pathlib import Path\n"
        f"root=Path({str(tmp_path)!r})\n"
        "def term(*_):\n"
        "    with (root/'grandchild-terms').open('a') as f: f.write('TERM\\n')\n"
        "    while not (root/'drain-release').exists(): time.sleep(.01)\n"
        "    raise SystemExit\n"
        "signal.signal(signal.SIGTERM,term)\n(root/'grandchild-ready').touch()\n"
        "while True: time.sleep(.01)\n"
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import signal,subprocess,sys,time\nfrom pathlib import Path\n"
        f"root=Path({str(tmp_path)!r})\n"
        f"p=subprocess.Popen([sys.executable,{str(grandchild)!r}])\n"
        "def term(*_):\n"
        "    with (root/'child-terms').open('a') as f: f.write('TERM\\n')\n"
        "    p.send_signal(signal.SIGTERM)\n"
        "signal.signal(signal.SIGTERM,term)\n"
        "while not (root/'grandchild-ready').exists(): time.sleep(.01)\n"
        "(root/'tree-ready').touch()\np.wait()\n"
    )
    job_id = manager.submit("fast", {"command": [sys.executable, str(child)]})["job_id"]
    wait_path(tmp_path / "tree-ready")
    try:
        manager.cancel_job(job_id)
        wait_path(tmp_path / "grandchild-terms")
        # Repeated TERM to the worker must not forward again while the child drains.
        os.kill(manager.get(job_id)["worker"]["pid"], signal.SIGTERM)
        manager.cancel_job(job_id)
        assert store.live(store.job_dir(job_id))
    finally:
        (tmp_path / "drain-release").touch()
    state(job_id, {"cancelled"})
    assert (tmp_path / "child-terms").read_text().splitlines() == ["TERM"]
    assert (tmp_path / "grandchild-terms").read_text().splitlines() == ["TERM"]


@pytest.mark.parametrize("first_key", [None, "one"])
def test_new_key_still_refuses_active_identical_request(jobs_env, first_key):
    first = process_result(submit_process({}, key=first_key))["job_id"]
    duplicate = process_result(submit_process({}, key="two"))
    assert duplicate["error"]["code"] == "conflict"
    assert first in duplicate["error"]["message"]
    if first_key:
        assert manager.submit("slow", {}, first_key)["job_id"] == first


@pytest.mark.parametrize("damage", ["claim_null", "claim_array", "no_dir", "no_record", "record_array"])
def test_incomplete_claim_never_replays(jobs_env, monkeypatch, damage):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("slow", {}, "key")["job_id"]
    path = store.job_dir(job_id)
    claim = jobs_env / "idempotency" / (hashlib.sha256(b"key").hexdigest() + ".json")
    if damage.startswith("claim_"):
        claim.write_text("null" if damage == "claim_null" else "[]")
    elif damage == "no_dir":
        path.rename(path.with_name("aside"))
    elif damage == "no_record":
        (path / "job.json").unlink()
    else:
        (path / "job.json").write_text("[]")
    with pytest.raises(ToolError, match="reservation is incomplete") as exc:
        manager.submit("slow", {}, "key")
    assert exc.value.error["code"] == "conflict"


def test_claim_published_only_after_durable_reservation(jobs_env, monkeypatch):
    original = store.atomic
    observed = []

    def atomic(path, value, **kwargs):
        if path.parent.name == "idempotency":
            job_path = store.job_dir(value["job_id"])
            assert not manager.corrupt(store.read(job_path))
            assert store.load(job_path / "request.json")["kind"] == "slow"
            assert (job_path / "lease.lock").is_file()
            observed.append(path)
        original(path, value, **kwargs)

    monkeypatch.setattr(store, "atomic", atomic)
    monkeypatch.setattr(manager, "launch", lambda job: None)
    manager.submit("slow", {}, "key")
    assert len(observed) == 1


def test_dead_started_queued_worker_is_failed_and_retryable(jobs_env):
    first = manager.submit("fake-gpu", {})["job_id"]
    wait_path(out(first) / "submitted")
    second = process_result(submit_process({"kind": "fake-gpu", "tag": "second"}))["job_id"]
    wait(lambda: manager.get(second)["worker"]["started_at"] is not None)
    assert manager.get(second)["state"] == "queued"
    assert not (out(second) / "submitted").exists()
    stop_worker(second)
    recovered = manager.get(second)
    assert recovered["state"] == "failed" and recovered["error"]["code"] == "worker_lost"
    assert not recovered["prompts"]
    retry = manager.retry_job(second)["job_id"]
    assert manager.get(retry)["parent_job_id"] == second


@pytest.mark.parametrize("prompt_id", [3, [], "", "x" * 201])
def test_discover_invalid_ids_cannot_corrupt_record(jobs_env, monkeypatch, prompt_id):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("slow", {})["job_id"]
    store.atomic(out(job_id) / "submission.json", {"prompt_id": prompt_id})
    job = manager.get(job_id)
    assert job["prompts"] == [{"prompt_id": None, "submission": "submission.json", "resolution": "unresolved"}]
    assert not manager.corrupt(store.read(store.job_dir(job_id)))


def test_discover_keeps_confirmed_stop_with_unreadable_result(jobs_env, monkeypatch):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("slow", {})["job_id"]
    root = out(job_id)
    store.atomic(root / "submission.json", {"prompt_id": "owned"})
    (root / "result.json").write_text("{")
    job = manager.get(job_id)
    job["prompts"][0]["resolution"] = "confirmed_stopped"
    assert manager.discover(job)[0]["resolution"] == "confirmed_stopped"


@pytest.mark.parametrize("operation", ["check_cancelled", "set_stage", "stage_race", "finish", "cancel", "retry"])
def test_corruption_during_mutation_is_never_overwritten(jobs_env, monkeypatch, operation):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("slow", {})["job_id"]
    path = store.job_dir(job_id)
    ctx = worker.Context(manager.get(job_id))
    if operation in {"cancel", "retry", "finish"}:
        store.atomic(out(job_id) / "submission.json", {"prompt_id": "owned"})
        with store.lock(path / "job.lock"):
            job = store.read(path)
            store.transition(job, "running")
            if operation != "finish":
                store.transition(job, "blocked")
            store.save(path, job)

        class CorruptingProbe:
            def cancel(self, prompt_id):
                return True

            def status(self, prompt_id):
                (path / "job.json").write_text("{broken")
                return "unknown"

        monkeypatch.setattr(worker, "prober", CorruptingProbe)
        if operation == "cancel":
            assert manager.cancel_job(job_id, probe=CorruptingProbe())["state"] == "blocked"
        elif operation == "retry":
            with pytest.raises(ToolError, match="corrupt"):
                manager.retry_job(job_id, probe=CorruptingProbe())
        else:
            worker._finish(path, ctx, {"done": True}, None, None)
    else:
        if operation == "stage_race":
            monkeypatch.setattr(ctx, "check_cancelled", lambda: (path / "job.json").write_text("{broken"))
        else:
            (path / "job.json").write_text("{broken")
        with pytest.raises(JobFailure):
            if operation in {"set_stage", "stage_race"}:
                ctx.set_stage("generate")
            else:
                ctx.check_cancelled()
    assert (path / "job.json").read_text() == "{broken"


def test_journal_mismatch_blocks_only_that_job_without_record_write(jobs_env, monkeypatch):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    bad = manager.submit("slow", {"tag": "bad"})["job_id"]
    good = manager.submit("slow", {"tag": "good"})["job_id"]
    path = store.job_dir(bad)
    record = (path / "job.json").read_bytes()
    (path / "events.jsonl").write_text('{"wrong":"event"}\n')
    assert manager.get(bad)["error"]["code"] == "corrupt_record"
    listed = call("list_jobs", {})["result"]["jobs"]
    assert {j["job_id"] for j in listed} == {bad, good}
    assert next(j for j in listed if j["job_id"] == good)["state"] == "queued"
    assert (path / "job.json").read_bytes() == record


@pytest.mark.parametrize("review", [{"verdict": "fail"}, None])
def test_job_status_fallback_reuses_registry_review_classification(jobs_env, monkeypatch, review):
    from sonoranlab import registry

    monkeypatch.setattr(manager, "launch", lambda job: None)
    job_id = manager.submit("slow", {})["job_id"]
    root = out(job_id)
    if review is None:
        (root / "review.json").symlink_to(root / "missing.json")
    else:
        store.atomic(root / "review.json", review)
    from sonoranlab.api.registry import Context

    expected = registry._review(root, Context())
    human = call("job_status", {"job_id": job_id})["result"]["human"]
    assert human == {"review": expected["review_state"], "production_usable": expected["production_usable"]}
    assert human["review"] == "invalid"


def test_result_summary_and_prioritized_artifact_pages_are_bounded(jobs_env, monkeypatch):
    monkeypatch.setattr(manager, "launch", lambda job: None)
    identifier = manager.submit("slow", {"instant": True})["job_id"]
    path = store.job_dir(identifier)
    worker._work(path)
    root = out(identifier)
    preferred = [
        "animate/sweep/final/job/report.json",
        "animate/sweep/final/job/contact.png",
        "animate/sweep/final/job/manifest.json",
        "animate/sweep/final/job/export/actor/actor.png",
        "animate/animate.json",
        "animate/sweep/sweep.json",
    ]
    for relative in [*preferred, *(f"animate/gen/attempt-1/frames/{i:04}.png" for i in range(1819))]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    with store.lock(path / "job.lock"):
        job = store.read(path)
        job["result"] = {
            "animate": {
                "outcome": "pass",
                "counts": {"planned": 5, "ok": 5},
                "verdict": {"pass": True, "job": str(root / "animate/sweep/final/job"), "failures": ["failure"] * 30},
                "generation": {"requests": [{"direction": "east", "seed": 1, "argv": ["x" * 100000]}]},
            },
            "attempts": [{"result": identifier + ":animate/gen/result.json", "outputs": {"pixels": "x" * 100000}}],
            "technical": {"validator": "pass", "godot": "not_run"},
            "warnings": ["warning"] * 25,
        }
        store.save(path, job)
    record = (path / "job.json").read_bytes()
    params = {"job_id": identifier}
    status = call("job_status", params)
    response = call("job_result", params)
    for envelope in (status, response):
        assert envelope["ok"], envelope
        assert len(json.dumps(envelope).encode()) < 64 * 1024
    assert "result" not in status["result"]
    assert "argv" not in status["result"]["result_summary"]["animate"]["generation"]["requests"][0]
    result = response["result"]
    assert result["result"]["warnings_count"] == 25 and len(result["result"]["warnings"]) == 20
    verdict = result["result"]["animate"]["verdict"]
    assert verdict["failures_count"] == 30 and verdict["failures_truncated"] and len(verdict["failures"]) == 20
    assert len(result["artifacts"]) == 200 and result["artifacts_truncated"]
    assert [item["ref"] for item in result["artifacts"][:6]] == [f"{identifier}:{rel}" for rel in preferred]
    refs = []
    for offset in range(0, result["artifact_count"], 500):
        page = call("job_result", params | {"artifacts_offset": offset, "artifacts_limit": 500})["result"]
        assert page["artifact_count"] == result["artifact_count"]
        assert len(page["artifacts"]) <= 500
        assert page["artifacts_truncated"] == (offset + len(page["artifacts"]) < result["artifact_count"])
        refs.extend(item["ref"] for item in page["artifacts"])
    assert len(refs) == len(set(refs)) == result["artifact_count"]
    assert result["artifacts"] == [{"ref": ref} for ref in refs[:200]]
    empty = call("job_result", params | {"artifacts_offset": len(refs)})["result"]
    assert not empty["artifacts"] and not empty["artifacts_truncated"]
    for bad in ({"artifacts_offset": -1}, {"artifacts_limit": 501}, {"artifacts_limit": 0}):
        assert call("job_result", params | bad)["error"]["code"] == "invalid_params"
    assert (path / "job.json").read_bytes() == record
