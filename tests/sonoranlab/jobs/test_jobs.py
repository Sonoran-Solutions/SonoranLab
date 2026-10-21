"""Multiprocess lifecycle and adversarial recovery checks, using only filesystem-backed fakes."""

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sonoranlab.api import call
from sonoranlab.api.registry import ToolError
from sonoranlab.jobs import manager, store
from sonoranlab.jobs.worker import main as worker_main


def wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for file/lease/state handshake")


def state(job_id, expected):
    return wait(lambda: job if (job := manager.get(job_id))["state"] in expected else None)


def out(job_id):
    return store.output(manager.get(job_id))


def wait_path(path):
    wait(path.exists)
    return path


def submit_process(params, *, key=None, gate=None, ready=None):
    program = """
import json, sys, time
from pathlib import Path
from sonoranlab.jobs import submit
from sonoranlab.api.registry import ToolError
p=json.loads(sys.argv[1])
if p['ready']: Path(p['ready']).touch()
if p['gate']:
    deadline=time.monotonic()+15
    while not Path(p['gate']).exists():
        if time.monotonic()>deadline: raise TimeoutError('submission gate')
        time.sleep(.01)
try:
    print(json.dumps(submit(p['kind'], p['params'], p['key'])), flush=True)
except ToolError as e:
    print(json.dumps({'error':e.error}), flush=True)
"""
    args = {
        "kind": params.pop("kind", "slow"),
        "params": params,
        "key": key,
        "gate": str(gate) if gate else None,
        "ready": str(ready) if ready else None,
    }
    return subprocess.Popen(
        [sys.executable, "-c", program, json.dumps(args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def process_result(process):
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr
    return json.loads(stdout)


def events(root):
    path = root / "fake/events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def stop_worker(job_id):
    path = store.job_dir(job_id)
    job = store.read(path)
    os.kill(job["worker"]["pid"], signal.SIGKILL)
    wait(lambda: not store.live(path))


def test_transition_table():
    expected = {
        "queued": {"running", "cancelling", "cancelled", "failed"},
        "running": {"cancelling", "succeeded", "failed", "blocked"},
        "cancelling": {"cancelled", "blocked", "failed"},
        "blocked": {"cancelled"},
        "cancelled": set(),
        "succeeded": set(),
        "failed": set(),
    }
    assert expected == store.TRANSITIONS
    for before in expected:
        for after in expected:
            job = {"state": before, "stage": "prepare", "history": []}
            if after in expected[before]:
                store.transition(job, after)
                assert job["history"][0]["from"] == before
            else:
                with pytest.raises(ValueError, match="Illegal job transition"):
                    store.transition(job, after)


def test_validation_before_any_creation(jobs_env):
    for params in ({"invalid": True}, {"project": "../escape"}, {"track": "E"}):
        with pytest.raises((ValueError, ToolError)):
            manager.submit("slow", params)
        assert not jobs_env.exists()
    with pytest.raises(ToolError):
        manager.submit("missing", {})
    assert not jobs_env.exists()


def test_concurrent_idempotency_and_conflicts(jobs_env, tmp_path):
    gate, ready1, ready2 = [tmp_path / name for name in ("go", "one-ready", "two-ready")]
    one = submit_process({}, key="same", gate=gate, ready=ready1)
    two = submit_process({}, key="same", gate=gate, ready=ready2)
    wait_path(ready1)
    wait_path(ready2)
    gate.touch()
    first, second = process_result(one), process_result(two)
    assert first["job_id"] == second["job_id"]
    job_id = first["job_id"]
    wait_path(out(job_id) / "ready")
    assert len(list((jobs_env / "jobs").glob("job-*"))) == 1
    different = process_result(submit_process({"tag": "different"}, key="same"))
    assert different["error"]["code"] == "idempotency_conflict"
    duplicate = process_result(submit_process({}))
    assert duplicate["error"]["code"] == "conflict"
    assert job_id in duplicate["error"]["message"]
    # Raw caller keys never enter job records or frozen requests.
    assert "same" not in (store.job_dir(job_id) / "job.json").read_text()
    (out(job_id) / "release").touch()
    state(job_id, {"succeeded"})
    assert manager.submit("slow", {}, "same")["job_id"] == job_id
    history = manager.get(job_id)["history"]
    recorded = [json.loads(line) for line in (store.job_dir(job_id) / "events.jsonl").read_text().splitlines()]
    assert recorded == history


def test_no_key_concurrent_duplicate(jobs_env, tmp_path):
    gate = tmp_path / "go"
    ready1, ready2 = tmp_path / "r1", tmp_path / "r2"
    one = submit_process({}, gate=gate, ready=ready1)
    two = submit_process({}, gate=gate, ready=ready2)
    wait_path(ready1)
    wait_path(ready2)
    gate.touch()
    results = [process_result(one), process_result(two)]
    assert sum("job_id" in result for result in results) == 1
    assert next(result["error"]["code"] for result in results if "error" in result) == "conflict"


def test_gpu_schedule_across_submitters_and_heavy_cpu(jobs_env):
    first = process_result(submit_process({"kind": "fake-gpu", "tag": "one"}))["job_id"]
    wait_path(out(first) / "submitted")
    second = process_result(submit_process({"kind": "fake-gpu", "tag": "two"}))["job_id"]
    wait(lambda: manager.get(second)["worker"]["pid"] is not None)
    assert manager.get(second)["state"] == "queued"
    assert not (out(second) / "ready").exists()
    (out(first) / "release").touch()
    state(first, {"succeeded"})
    wait_path(out(second) / "submitted")
    (out(second) / "release").touch()
    state(second, {"succeeded"})
    sequence = [(e["event"], e["job_id"]) for e in events(jobs_env) if e["event"] in {"start", "end"}]
    assert sequence == [("start", first), ("end", first), ("start", second), ("end", second)]
    assert manager.get(second)["timing"]["queue_wait_s"] > 0


def test_status_result_and_cli_responsive_offline(jobs_env, monkeypatch):
    job_id = manager.submit("slow", {})["job_id"]
    wait_path(out(job_id) / "ready")

    def forbidden(*args, **kwargs):
        raise AssertionError("read launched a process or contacted a service")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "launch", forbidden)
        patch.setattr(manager, "prober", forbidden)
        started = time.monotonic()
        response = call("job_status", {"job_id": job_id})
        assert time.monotonic() - started < 1
        assert response["ok"] and response["result"]["progress"]["ready"]
        assert response["result"]["technical"]["validator"] is None
        assert response["result"]["human"] == {"review": "none", "production_usable": False}
        not_ready = call("job_result", {"job_id": job_id})
        assert not_ready["error"]["hint"] == "not_ready"
    cli = subprocess.run(
        [sys.executable, "-m", "sonoranlab.cli", "api", "job_status", "--params", "-"],
        input=json.dumps({"job_id": job_id}),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(cli.stdout)["result"]["state"] == "running"
    (out(job_id) / "release").touch()
    state(job_id, {"succeeded"})
    result = call("job_result", {"job_id": job_id})["result"]
    assert result["result"] == {"complete": True}
    assert result["artifacts"]
    assert all(item["ref"].startswith(job_id + ":") for item in result["artifacts"])
    assert manager.cancel_job(job_id)["state"] == "succeeded"
    with pytest.raises(ToolError, match="Only failed"):
        manager.retry_job(job_id)


@pytest.mark.parametrize("boundary", ["before_submit", "after_submission", "processing", "export"])
@pytest.mark.parametrize("crash", [False, True], ids=["sigkill", "exit"])
def test_crash_boundaries_never_replay(jobs_env, boundary, crash):
    job_id = manager.submit("fake-gpu", {"boundary": boundary, "crash": crash})["job_id"]
    root = out(job_id)
    wait_path(root / boundary)
    if not crash:
        stop_worker(job_id)
    else:
        wait(lambda: not store.live(store.job_dir(job_id)))
    recovered = manager.get(job_id)
    expected = "failed" if boundary == "before_submit" else "blocked"
    assert recovered["state"] == expected
    assert recovered["error"]["code"] == ("worker_lost" if expected == "failed" else "needs_reconciliation")
    if expected == "blocked":
        assert recovered["prompts"][0]["prompt_id"] in recovered["error"]["message"]
    count = sum(e["event"] == "submit" for e in events(jobs_env))
    assert count == (0 if expected == "failed" else 1)
    for _ in range(3):
        assert manager.get(job_id)["state"] == expected
        assert call("list_jobs", {})["result"]["jobs"][0]["state"] == expected
    assert sum(e["event"] == "submit" for e in events(jobs_env)) == count
    assert worker_main([job_id]) == 0
    assert sum(e["event"] == "submit" for e in events(jobs_env)) == count


def test_cancel_queued_without_worker_and_with_waiting_worker(jobs_env, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(manager, "launch", lambda job: None)
        job_id = manager.submit("slow", {})["job_id"]
    assert manager.cancel_job(job_id)["state"] == "cancelled"
    assert worker_main([job_id]) == 0
    first = manager.submit("fake-gpu", {"tag": "first"})["job_id"]
    wait_path(out(first) / "submitted")
    second = manager.submit("fake-gpu", {"tag": "second"})["job_id"]
    wait(lambda: manager.get(second)["worker"]["pid"] is not None)
    manager.cancel_job(second)
    assert state(second, {"cancelled"})["cancel"]["outcome"] == "confirmed"
    assert not (out(second) / "submitted").exists()
    assert manager.cancel_job(second)["state"] == "cancelled"


def test_cancel_running_confirmed_and_duplicate(jobs_env):
    job_id = manager.submit("fake-gpu", {})["job_id"]
    wait_path(out(job_id) / "submitted")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(manager.cancel_job, [job_id, job_id]))
    assert all(result["state"] in {"cancelling", "cancelled"} for result in results)
    assert state(job_id, {"cancelled"})["cancel"]["outcome"] == "confirmed"
    assert manager.get(job_id)["prompts"][0]["resolution"] == "confirmed_stopped"
    assert manager.cancel_job(job_id)["state"] == "cancelled"


@pytest.mark.parametrize("failure", ["still-running", "probe-error"])
def test_cancel_unconfirmed_scoped_then_resolve(jobs_env, failure):
    job_id = manager.submit("fake-gpu", {})["job_id"]
    wait_path(out(job_id) / "submitted")
    fake = jobs_env / "fake"
    (fake / failure).touch()
    foreign = "foreign-prompt"
    store.atomic(fake / f"{foreign}.json", {"state": "running"})
    manager.cancel_job(job_id)
    blocked = state(job_id, {"blocked"})
    assert blocked["error"]["code"] == "cancel_unconfirmed"
    wait(lambda: not store.live(store.job_dir(job_id)))
    with pytest.raises(ToolError, match="unresolved"):
        manager.retry_job(job_id)
    assert manager.cancel_job(job_id)["state"] == "blocked"
    assert manager.get(job_id)["error"]["code"] == "cancel_unconfirmed"
    assert json.loads((fake / f"{foreign}.json").read_text())["state"] == "running"
    assert all(e.get("prompt_id") != foreign for e in events(jobs_env) if e["event"] == "cancel")
    (fake / failure).unlink()
    assert manager.cancel_job(job_id)["state"] == "cancelled"
    assert manager.cancel_job(job_id)["state"] == "cancelled"


def test_blocked_cancel_probe_leaves_reads_responsive(jobs_env):
    job_id = manager.submit("fake-gpu", {"boundary": "after_submission", "crash": True})["job_id"]
    wait_path(out(job_id) / "after_submission")
    state(job_id, {"blocked"})
    fake = jobs_env / "fake"
    (fake / "probe-hold").touch()
    with ThreadPoolExecutor(1) as pool:
        cancellation = pool.submit(manager.cancel_job, job_id)
        wait_path(fake / "probe-ready")
        start = time.monotonic()
        assert call("job_status", {"job_id": job_id})["ok"]
        assert time.monotonic() - start < 1
        (fake / "probe-release").touch()
        assert cancellation.result(timeout=5)["state"] == "cancelled"


def test_retry_frozen_request_link_and_changed_input(jobs_env, tmp_path):
    input_path = tmp_path / "input"
    input_path.write_text("old")
    parent = manager.submit("slow", {"input": str(input_path)})["job_id"]
    wait_path(out(parent) / "ready")
    manager.cancel_job(parent)
    state(parent, {"cancelled"})
    child = manager.retry_job(parent, "retry-key")["job_id"]
    job = manager.get(child)
    assert job["parent_job_id"] == parent and job["attempt"] == 2
    assert job["params"] == manager.get(parent)["params"]
    assert job["output_dir"] != manager.get(parent)["output_dir"]
    assert manager.retry_job(parent, "retry-key")["job_id"] == child
    input_path.write_text("changed")
    with pytest.raises(ToolError, match="changed"):
        manager.retry_job(parent)


def test_blocked_retry_requires_stopped_prompts(jobs_env):
    parent = manager.submit("fake-gpu", {"boundary": "after_submission", "crash": True})["job_id"]
    wait_path(out(parent) / "after_submission")
    blocked = state(parent, {"blocked"})
    with pytest.raises(ToolError, match="unresolved"):
        manager.retry_job(parent)
    prompt_id = blocked["prompts"][0]["prompt_id"]
    store.atomic(jobs_env / "fake" / f"{prompt_id}.json", {"state": "done"})
    child = manager.retry_job(parent)["job_id"]
    assert manager.get(child)["parent_job_id"] == parent
    assert manager.get(parent)["prompts"][0]["resolution"] == "confirmed_stopped"


@pytest.mark.parametrize(
    "content",
    ["{", "{}", '{"schema":"sonoranlab.job/1"}', "null", "oversized_timing", "[" * 2000 + "]" * 2000],
    ids=["partial", "empty", "incomplete", "null", "oversized_timing", "deeply_nested"],
)
def test_corrupt_record_is_preserved_and_listing_continues(jobs_env, monkeypatch, content):
    with monkeypatch.context() as patch:
        patch.setattr(manager, "launch", lambda job: None)
        bad = manager.submit("slow", {"tag": "bad"})["job_id"]
    good = manager.submit("fast", {})["job_id"]
    state(good, {"succeeded"})
    record = store.job_dir(bad) / "job.json"
    if content == "oversized_timing":
        value = json.loads(record.read_text())
        value["timing"]["run_s"] = 10**1000
        content = json.dumps(value)
    record.write_text(content)
    assert manager.get(bad)["error"]["code"] == "corrupt_record"
    assert {job["job_id"] for job in manager.reconcile_all()} == {good, bad}
    assert call("job_status", {"job_id": bad})["result"]["state"] == "blocked"
    assert manager.cancel_job(bad)["state"] == "blocked"
    assert worker_main([bad]) == 0
    assert record.read_text() == content


def test_stale_worker_pid_lease_and_lease_exclusion(jobs_env):
    job_id = manager.submit("slow", {})["job_id"]
    wait_path(out(job_id) / "ready")
    assert (
        subprocess.run(
            [sys.executable, "-m", "sonoranlab.jobs.worker", job_id], capture_output=True, timeout=5
        ).returncode
        == 0
    )
    assert sum(e["event"] == "start" for e in events(jobs_env)) == 1
    stop_worker(job_id)
    # A PID's mere existence is not liveness; only the lease is authoritative.
    path = store.job_dir(job_id)
    with store.lock(path / "job.lock"):
        job = store.read(path)
        job["worker"]["pid"] = os.getpid()
        store.save(path, job)
    recovered = manager.get(job_id)
    assert recovered["state"] == "failed" and recovered["error"]["code"] == "worker_lost"
    assert manager.cancel_job(job_id)["state"] == "failed"
    child = manager.retry_job(job_id)["job_id"]
    assert manager.get(child)["parent_job_id"] == job_id


@pytest.mark.parametrize("exit_code,code", [(3, "budget_refused"), (4, "guard_busy")])
def test_guard_refusal_is_structured(jobs_env, exit_code, code):
    job_id = manager.submit("fast", {"guard_exit": exit_code})["job_id"]
    job = state(job_id, {"failed"})
    assert job["error"]["code"] == code and job["error"]["retryable"]
    assert not job["prompts"]


def test_term_propagates_to_children_and_log_is_bounded(jobs_env):
    job_id = manager.submit("fast", {"child": True, "more_children": True})["job_id"]
    wait_path(out(job_id) / "child-ready")
    manager.cancel_job(job_id)
    state(job_id, {"cancelled"})
    wait_path(out(job_id) / "child-stopped")
    assert not (out(job_id) / "extra-child").exists()
    noisy = manager.submit("fast", {"log_bytes": 3 * 1024 * 1024})["job_id"]
    state(noisy, {"succeeded"})
    wait(lambda: not store.live(store.job_dir(noisy)))
    assert (store.job_dir(noisy) / "worker.log").stat().st_size <= 1024 * 1024


def test_id_and_filter_validation(jobs_env):
    assert call("list_jobs", {"limit": 201})["error"]["code"] == "invalid_params"
    assert call("job_status", {"job_id": "../bad"})["error"]["code"] == "invalid_params"
    missing = "job-20260101T000000Z-12345678"
    assert call("job_status", {"job_id": missing})["error"]["code"] == "not_found"
    assert call("job_result", {"job_id": missing})["error"]["code"] == "not_found"
    assert call("list_jobs", {})["result"]["jobs"] == []
    assert not jobs_env.exists()
    first = manager.submit("fast", {"project": "one"})["job_id"]
    second = manager.submit("fast", {"project": "two"})["job_id"]
    state(first, {"succeeded"})
    state(second, {"succeeded"})
    assert len(call("list_jobs", {"limit": 1})["result"]["jobs"]) == 1
    assert call("list_jobs", {"state": "running"})["result"]["jobs"] == []
    assert call("list_jobs", {"project": "one"})["result"]["jobs"][0]["job_id"] == first


def test_journal_tail_recovers_without_replaying(jobs_env, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(manager, "launch", lambda job: None)
        job_id = manager.submit("slow", {})["job_id"]
    path = store.job_dir(job_id)
    original = (path / "events.jsonl").read_bytes()
    with store.lock(path / "job.lock"):
        job = store.read(path)
        store.transition(job, "cancelled")
        # Simulate SIGKILL between the durable record replacement and the event append.
        store.atomic(path / "job.json", job)
    (path / "events.jsonl").write_bytes(original + b'{"at":')
    assert manager.get(job_id)["state"] == "cancelled"
    committed = (path / "events.jsonl").read_bytes()
    assert committed.startswith(original)
    assert [json.loads(line) for line in committed.splitlines()] == job["history"]
    assert worker_main([job_id]) == 0


def test_game_state_root_refused_before_creation(jobs_env, monkeypatch, tmp_path):
    from sonoranlab.api import paths

    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.setattr(paths, "_game_roots", lambda: (game,))
    forbidden = game / "new-state"
    monkeypatch.setenv("SONORANLAB_STATE", str(forbidden))
    with pytest.raises(ToolError, match="protected"):
        manager.submit("slow", {})
    assert not forbidden.exists()


def test_heavy_cpu_uses_same_scheduler(jobs_env):
    first = manager.submit("fake-gpu", {})["job_id"]
    wait_path(out(first) / "submitted")
    second = process_result(submit_process({"kind": "heavy"}))["job_id"]
    wait(lambda: manager.get(second)["worker"]["pid"] is not None)
    assert manager.get(second)["state"] == "queued"
    assert not (out(second) / "ready").exists()
    (out(first) / "release").touch()
    state(first, {"succeeded"})
    wait_path(out(second) / "ready")
    assert manager.get(second)["state"] == "running"
    (out(second) / "release").touch()
    state(second, {"succeeded"})


def test_crash_between_persistence_and_post_is_blocked(jobs_env):
    job_id = manager.submit("fake-gpu", {"boundary": "persisted_before_post", "crash": True})["job_id"]
    wait_path(out(job_id) / "persisted_before_post")
    blocked = state(job_id, {"blocked"})
    assert blocked["prompts"][0]["prompt_id"]
    assert not any(e["event"] == "submit" for e in events(jobs_env))
    assert manager.cancel_job(job_id)["state"] == "cancelled"  # Fake queue confirms unknown; never posted.


def test_default_prober_confirmation_and_container_errors(jobs_env, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "exists" in argv:
            return SimpleNamespace(returncode=1)
        raise AssertionError(argv)

    monkeypatch.setattr(manager.subprocess, "run", run)
    probe = manager.ComfyProber()
    assert probe.status("owned") == "stopped"
    assert probe.cancel("owned") is True
    assert all("exists" in argv for argv in calls)

    def broken(argv, **kwargs):
        return SimpleNamespace(returncode=125)

    monkeypatch.setattr(manager.subprocess, "run", broken)
    with pytest.raises(OSError, match="could not be confirmed"):
        probe.status("owned")


def test_launchers_and_launch_failure(jobs_env, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("SONORANLAB_JOB_LAUNCHER")
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0)

    with monkeypatch.context() as patch:
        patch.setattr(manager.subprocess, "run", run)
        job_id = manager.submit("slow", {})["job_id"]
        record = manager.get(job_id)
        assert record["worker"]["launcher"] == "systemd"
        launch = commands[-1]
        assert launch[:2] == ["systemd-run", "--user"]
        assert f"--unit=sonoranlab-job-{job_id}" in launch
        assert "--collect" in launch and "--quiet" in launch
        assert launch[-4:] == [sys.executable, "-m", "sonoranlab.jobs.worker", job_id]
        manager._signal(record)
        assert commands[-1] == ["systemctl", "--user", "kill", "--signal=SIGTERM", f"sonoranlab-job-{job_id}"]
    assert manager.cancel_job(job_id)["state"] == "cancelled"
    monkeypatch.setenv("SONORANLAB_JOB_LAUNCHER", "popen")
    with monkeypatch.context() as patch:

        def failed(job):
            raise OSError("cannot start")

        patch.setattr(manager, "launch", failed)
        failed_id = manager.submit("fast", {})["job_id"]
    record = manager.get(failed_id)
    assert record["state"] == "failed" and record["error"]["code"] == "launcher_failed"
    assert record["error"]["retryable"]
    assert call("job_result", {"job_id": failed_id})["result"]["error"]["code"] == "launcher_failed"
    child = manager.retry_job(failed_id)["job_id"]
    state(child, {"succeeded"})


def test_nonregular_record_never_blocks_reads(jobs_env, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(manager, "launch", lambda job: None)
        job_id = manager.submit("slow", {})["job_id"]
    path = store.job_dir(job_id) / "job.json"
    path.unlink()
    os.mkfifo(path)
    start = time.monotonic()
    assert manager.get(job_id)["error"]["code"] == "corrupt_record"
    assert time.monotonic() - start < 1


def test_log_reader_survives_worker_sigkill(jobs_env, tmp_path):
    log_path = tmp_path / "worker.log"
    ready, release, done = (tmp_path / name for name in ("writer-ready", "writer-release", "writer-done"))
    writer = (
        "import os,time; from pathlib import Path; "
        f"ready=Path({str(ready)!r}); release=Path({str(release)!r}); ready.touch(); "
        "deadline=time.monotonic()+15\n"
        "while not release.exists():\n"
        "    if time.monotonic()>deadline: raise TimeoutError('writer gate')\n"
        "    time.sleep(.01)\n"
        f"os.write(2,b'x'*(3*1024*1024)); Path({str(done)!r}).touch()\n"
    )
    parent = (
        "import subprocess,sys,time; from pathlib import Path\n"
        "from sonoranlab.jobs.worker import bounded_log\n"
        f"with bounded_log(Path({str(log_path)!r})):\n"
        f"    subprocess.Popen([sys.executable,'-c',{writer!r}],start_new_session=True)\n"
        "    time.sleep(25)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", parent], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_path(ready)
        process.kill()
        process.wait(timeout=5)
        release.touch()
        wait_path(done)
        wait(lambda: log_path.stat().st_size > 0)
        assert log_path.stat().st_size <= 1024 * 1024
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_cancel_during_worker_bootstrap(jobs_env, monkeypatch, tmp_path):
    with monkeypatch.context() as patch:
        patch.setattr(manager, "launch", lambda job: None)
        job_id = manager.submit("slow", {})["job_id"]
    ready, release = tmp_path / "bootstrap-ready", tmp_path / "bootstrap-release"
    program = (
        "from pathlib import Path\nfrom contextlib import contextmanager\n"
        "from sonoranlab.jobs import worker\nfrom job_fakes import wait_file\n"
        "original=worker.bounded_log\n@contextmanager\ndef startup(path):\n"
        "    with original(path):\n"
        f"        Path({str(ready)!r}).touch()\n"
        f"        wait_file(Path({str(release)!r}))\n"
        "        yield\nworker.bounded_log=startup\n"
        f"raise SystemExit(worker.main([{job_id!r}]))\n"
    )
    process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_path(ready)
        assert manager.cancel_job(job_id)["state"] == "cancelled"
        release.touch()
        assert process.wait(timeout=10) == 0
        assert manager.get(job_id)["state"] == "cancelled"
        assert not events(jobs_env)
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_remembered_prompt_without_submission_never_authorizes_foreign_cancel(jobs_env):
    job_id = manager.submit("fake-gpu", {"boundary": "after_submission", "crash": True})["job_id"]
    wait_path(out(job_id) / "after_submission")
    state(job_id, {"blocked"})
    foreign = "foreign-prompt"
    store.atomic(jobs_env / "fake" / f"{foreign}.json", {"state": "running"})
    path = store.job_dir(job_id)
    with store.lock(path / "job.lock"):
        job = store.read(path)
        job["prompts"].append(
            {"prompt_id": foreign, "submission": "missing/submission.json", "resolution": "unresolved"}
        )
        store.save(path, job)
    assert manager.cancel_job(job_id)["state"] == "blocked"
    assert manager.get(job_id)["error"]["code"] == "cancel_unconfirmed"
    assert all(e.get("prompt_id") != foreign for e in events(jobs_env) if e["event"] == "cancel")
    assert json.loads((jobs_env / "fake" / f"{foreign}.json").read_text())["state"] == "running"
