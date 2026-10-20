"""ComfyUI contract against an actual loopback HTTP server; no GPU or external network."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import pytest

from sonoranlab.comfy import ComfyClient, ComfyError, Workflow, graph_sha256
from sonoranlab.comfy.client import PodmanExecTransport

from .fake_comfy import Clock, FakeComfy


@pytest.fixture
def fake() -> Iterator[FakeComfy]:
    server = FakeComfy()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(fake: FakeComfy, tmp_path: Path, clock: Clock) -> ComfyClient:
    return ComfyClient(fake.url, output_root=tmp_path, poll_s=0.01, http_timeout=0.1, sleep=clock.sleep, clock=clock)


@pytest.fixture
def workflow(tmp_path: Path) -> Workflow:
    source = tmp_path / "workflow.json"
    source.write_text(
        json.dumps(
            {
                "7": {"class_type": "Sampler", "inputs": {"seed": 1, "unet_name": "toy.gguf"}},
                "8": {"class_type": "Sampler", "inputs": {"seed": 1, "vae_name": "mystery.pt"}},
            }
        )
    )
    source.with_suffix(".params.yaml").write_text('seed: [{node: "7", input: seed}, {node: "8", input: seed}]\n')
    return Workflow.load(source)


def output(filename: str = "image.png", subfolder: str = "", kind: str = "images", node: str = "9") -> dict:
    return {node: {kind: [{"filename": filename, "subfolder": subfolder, "type": "output"}]}}


def assert_safe(fake: FakeComfy, prompt_id: str) -> None:
    assert fake.foreign in fake.running
    for method, path, body in fake.log:
        if method == "POST" and path == "/interrupt":
            assert body == {"prompt_id": prompt_id}
        if method == "POST" and path == "/queue":
            assert body == {"delete": [prompt_id]}


def test_success_and_provenance(fake: FakeComfy, client: ComfyClient, workflow: Workflow, tmp_path: Path) -> None:
    (tmp_path / "frames").mkdir()
    data = b"a tiny generated frame"
    (tmp_path / "frames/image.png").write_bytes(data)
    fake.outputs = output(subfolder="frames")
    fake.outputs["9"]["gifs"] = [{"filename": "image.png", "subfolder": "frames", "type": "temp"}]
    tsv = tmp_path / "models.tsv"
    tsv.write_text(
        "id\trepo\trevision\tremote_path\tdest\tsize_bytes\tsha256\tstatus\n"
        "T\trepo\trev\tfile\tunet/toy.gguf\t123\tmodel-digest\tdefault\n"
    )
    client.models_tsv = tsv
    fake.script = deque(["pending", "running", "success"])
    record = client.run(workflow, {"seed": 42}, timeout_s=1)
    assert record.status == "success", record.error
    assert record.params == {"seed": 42}
    assert record.workflow_source_sha256 == hashlib.sha256(workflow.source.read_bytes()).hexdigest()
    assert record.workflow_patched_sha256 == graph_sha256(workflow.patch({"seed": 42}))
    assert record.models == {"unet/toy.gguf": "model-digest", "mystery.pt": "unknown"}
    assert record.outputs[0].sha256 == hashlib.sha256(data).hexdigest()
    assert record.outputs[0].bytes == len(data)
    assert record.outputs[0].rel == "frames/image.png"
    assert record.outputs[0].path == tmp_path / "frames/image.png"
    assert record.messages[-1][0] == "execution_success"
    assert record.timing["queue_wait_s"] > 0
    assert record.timing["exec_s"] > 0
    assert record.timing["wall_s"] >= record.timing["queue_wait_s"] + record.timing["exec_s"]
    json.dumps(record.to_json())
    post = next(body for method, path, body in fake.log if path == "/prompt")
    assert post["client_id"] == record.client_id
    assert post["prompt"]["7"]["inputs"]["seed"] == 42
    assert client.status(record.prompt_id) == "done"
    assert_safe(fake, record.prompt_id)


def test_rejected(fake: FakeComfy, client: ComfyClient, workflow: Workflow) -> None:
    fake.inject("POST", "/prompt", 400)
    record = client.run(workflow, {}, timeout_s=1)
    assert record.status == "rejected"
    assert record.error["stage"] == "submit"
    assert record.error["retryable"] is False
    assert record.error["detail"]["error"] == {"type": "injected"}
    assert record.error["detail"]["node_errors"] == {"7": "bad"}
    assert len([req for req in fake.log if req[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "prompt_id", ["bad", "0123456789abcdef0123456789abcdef", "A" * 8 + "-0000-0000-0000-000000000000"]
)
def test_invalid_uuid(client: ComfyClient, fake: FakeComfy, prompt_id: str) -> None:
    with pytest.raises(ComfyError) as error:
        client.submit({}, prompt_id=prompt_id)
    assert error.value.code == "rejected"
    assert not fake.log


@pytest.mark.parametrize("event,code", [("error", "execution_error"), ("interrupted", "interrupted")])
def test_execution_failure(fake: FakeComfy, client: ComfyClient, workflow: Workflow, event: str, code: str) -> None:
    fake.script = deque([event])
    record = client.run(workflow, {}, timeout_s=1)
    assert record.status == code
    assert record.error["prompt_id"] == record.prompt_id
    assert record.error["detail"]["node_id"] == "7"
    assert record.error["detail"]["node_type"] == "Sampler"
    assert record.error["detail"]["exception_message"] == "out of memory"
    assert record.messages[-1][0] == "execution_" + event
    assert record.prompt_id not in fake.pending + fake.running


@pytest.mark.parametrize("legacy", [False, True])
def test_timeout_scoped_cancel(fake: FakeComfy, client: ComfyClient, workflow: Workflow, legacy: bool) -> None:
    fake.script = deque(["running"])
    fake.cancel_supported = not legacy
    record = client.run(workflow, {}, timeout_s=0.04)
    assert record.status == "timeout", record.error
    assert record.error["retryable"] is True
    assert record.prompt_id not in fake.running + fake.pending
    assert_safe(fake, record.prompt_id)
    if legacy:
        assert any(path == "/interrupt" for _, path, _ in fake.log)


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("legacy_status", [None, 404, 405])
def test_cancel_pending_and_running(
    fake: FakeComfy,
    client: ComfyClient,
    running: bool,
    legacy_status: int | None,
) -> None:
    prompt_id = str(uuid.uuid4())
    fake.add(prompt_id, running=running)
    fake.script.clear()
    fake.cancel_supported = legacy_status is None
    fake.cancel_status = legacy_status or 404
    assert client.status(prompt_id) == ("running" if running else "pending")
    assert client.cancel(prompt_id, confirm_s=0.1)
    assert client.status(prompt_id) == "unknown"
    assert_safe(fake, prompt_id)
    assert any(path == f"/api/jobs/{prompt_id}/cancel" for _, path, _ in fake.log)


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("jobs_supported", [True, False])
def test_lost_submit_response(
    fake: FakeComfy,
    client: ComfyClient,
    clock: Clock,
    accepted: bool,
    jobs_supported: bool,
) -> None:
    fake.inject("POST", "/prompt", "drop_after" if accepted else "drop_before")
    fake.jobs_supported = jobs_supported
    fake.script.clear()
    prompt_id = client.submit({})
    posts = [body for method, path, body in fake.log if path == "/prompt"]
    assert len(posts) == (1 if accepted else 2)
    assert {body["prompt_id"] for body in posts} == {prompt_id}
    assert fake.accepted == 1
    assert len(fake.jobs) == 1
    if not accepted:
        assert clock.sleeps
    assert client.cancel(prompt_id)


def test_unreachable_submit_unknown(fake: FakeComfy, tmp_path: Path, clock: Clock) -> None:
    fake.close()
    client = ComfyClient(fake.url, output_root=tmp_path, sleep=clock.sleep, clock=clock, http_timeout=0.02)
    with pytest.raises(ComfyError) as error:
        client.submit({})
    assert error.value.code == "submit_unknown"
    assert error.value.retryable is False
    assert error.value.prompt_id
    assert "do not submit a new ID" in error.value.message


def test_500_lookup_before_retry(fake: FakeComfy, client: ComfyClient) -> None:
    fake.inject("POST", "/prompt", 500)
    prompt_id = client.submit({})
    paths = [path for _, path, _ in fake.log]
    first, second = paths.index("/prompt"), len(paths) - 1
    assert paths[second] == "/prompt"
    assert any(path.startswith("/api/jobs/") for path in paths[first + 1 : second])
    assert fake.accepted == 1
    assert client.cancel(prompt_id)


def test_persistent_500_is_http_error(fake: FakeComfy, client: ComfyClient) -> None:
    fake.inject("POST", "/prompt", 500, 500, 500)
    with pytest.raises(ComfyError) as error:
        client.submit({})
    assert error.value.code == "http_error"
    assert error.value.detail["http_status"] == 500
    posts = [body for _, path, body in fake.log if path == "/prompt"]
    assert len(posts) == 3
    assert len({body["prompt_id"] for body in posts}) == 1


def test_transient_poll_errors(fake: FakeComfy, client: ComfyClient, workflow: Workflow) -> None:
    prompt_id = str(uuid.uuid4())
    # Submission's duplicate check also reads history once.
    fake.inject("GET", f"/history/{prompt_id}", ("json", {}), "drop_before", 500)
    record = client.run(workflow, {}, prompt_id=prompt_id, timeout_s=1, expect=0)
    assert record.status == "success", record.error
    assert record.timing["poll_errors"] == 2


def test_lost_prompt(fake: FakeComfy, client: ComfyClient, workflow: Workflow) -> None:
    fake.script = deque(["pending", "lost"])
    record = client.run(workflow, {}, timeout_s=1)
    assert record.status == "interrupted"
    assert record.error["detail"] == {"reason": "lost_from_queue"}
    assert record.error["retryable"] is True


@pytest.mark.parametrize("bad", ["missing", "empty", "directory", "parent", "absolute", "symlink"])
def test_bad_outputs(fake: FakeComfy, client: ComfyClient, workflow: Workflow, tmp_path: Path, bad: str) -> None:
    filename, subfolder = "image.png", ""
    if bad == "empty":
        (tmp_path / filename).touch()
    elif bad == "directory":
        (tmp_path / filename).mkdir()
    elif bad == "parent":
        subfolder = "../" + tmp_path.name
        (tmp_path / filename).write_bytes(b"exists")
    elif bad == "absolute":
        filename = str(tmp_path / filename)
        Path(filename).write_bytes(b"exists")
    elif bad == "symlink":
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.write_bytes(b"outside")
        (tmp_path / filename).symlink_to(outside)
    fake.outputs = output(filename, subfolder)
    record = client.run(workflow, {}, timeout_s=1)
    assert record.status == "missing_outputs", record.error
    assert any(path.endswith("/cancel") for _, path, _ in fake.log)


def test_per_node_expect_and_other_output_kinds(client: ComfyClient, tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"gif")
    entry = {"outputs": output(kind="gifs") | output(node="10")}
    assert len(client.collect("id", entry, expect={"9": 1, "10": 1})) == 2
    with pytest.raises(ComfyError) as error:
        client.collect("id", entry, expect={"9": 2})
    assert error.value.code == "missing_outputs"
    with pytest.raises(ComfyError):
        client.collect("id", entry, expect={"absent": 1})


def test_reattach_new_client(fake: FakeComfy, client: ComfyClient, workflow: Workflow, clock: Clock) -> None:
    prompt_id = client.submit(workflow.graph)
    restarted = ComfyClient(fake.url, output_root=client.output_root, clock=clock, sleep=clock.sleep)
    record = restarted.reattach(prompt_id, workflow=workflow, graph=workflow.graph, values={}, timeout_s=1, expect=0)
    assert record.status == "success", record.error
    assert record.prompt_id == prompt_id
    assert len([path for _, path, _ in fake.log if path == "/prompt"]) == 1
    assert restarted.client_id != client.client_id


def test_no_duplicate_submit(fake: FakeComfy, client: ComfyClient) -> None:
    prompt_id = client.submit({})
    with pytest.raises(ComfyError, match="reattach"):
        client.submit({}, prompt_id=prompt_id)
    other = ComfyClient(fake.url, output_root=client.output_root)
    with pytest.raises(ComfyError, match="reattach"):
        other.submit({}, prompt_id=prompt_id)
    assert fake.accepted == 1


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.2:8189", "http://localhost.example.com"])
def test_remote_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(ComfyError) as error:
        ComfyClient(url, output_root=tmp_path)
    assert error.value.code == "remote_refused"
    assert error.value.stage == "config"
    ComfyClient(url, output_root=tmp_path, allow_remote=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_accepted(tmp_path: Path, host: str) -> None:
    ComfyClient(f"http://{host}:8189", output_root=tmp_path)


def test_workflow_validation(workflow: Workflow) -> None:
    patched = workflow.patch({"seed": 9})
    assert patched["7"]["inputs"]["seed"] == patched["8"]["inputs"]["seed"] == 9
    assert workflow.graph["7"]["inputs"]["seed"] == 1
    with pytest.raises(ValueError, match=r"known:.*seed"):
        workflow.patch({"typo": 1})
    for target in ({"node": "missing", "input": "seed"}, {"node": "7", "input": "missing"}):
        invalid = Workflow(workflow.source, workflow.graph, {"seed": target})
        with pytest.raises(ValueError, match="missing node/input"):
            invalid.patch({"seed": 2})
    for graph in ({"nodes": []}, {"links": []}, []):
        workflow.source.write_text(json.dumps(graph))
        with pytest.raises(ValueError, match="API-format"):
            Workflow.load(workflow.source)


def test_bad_params_fail_before_submit(client: ComfyClient, fake: FakeComfy, workflow: Workflow) -> None:
    with pytest.raises(ValueError):
        client.run(workflow, {"typo": 1}, timeout_s=1)
    assert not fake.log


def test_cancel_unconfirmed(fake: FakeComfy, client: ComfyClient, workflow: Workflow) -> None:
    client.poll_s = 1.0
    fake.ignore_cancel = True
    fake.script = deque(["running"])
    record = client.run(workflow, {}, timeout_s=0.02)
    assert record.status == "cancel_unconfirmed"
    assert record.error["stage"] == "cancel"
    assert record.error["retryable"] is False
    assert record.prompt_id in fake.running
    assert "GPU work may still be live" in record.error["message"]
    assert_safe(fake, record.prompt_id)


def test_slow_history_respects_deadline(fake: FakeComfy, tmp_path: Path, workflow: Workflow) -> None:
    # Real time is needed to prove urllib's socket timeout, rather than just the polling budget.
    client = ComfyClient(fake.url, output_root=tmp_path, poll_s=0.005, http_timeout=0.2)
    prompt_id = client.submit(workflow.graph)
    fake.script.clear()
    fake.inject("GET", f"/history/{prompt_id}", ("slow", 0.12))
    with pytest.raises(ComfyError) as error:
        client.wait(prompt_id, timeout_s=0.03)
    assert error.value.code == "timeout"
    assert prompt_id not in fake.pending + fake.running
    assert_safe(fake, prompt_id)


def test_unknown_lookup_never_blindly_reposts(fake: FakeComfy, client: ComfyClient) -> None:
    fake.inject("POST", "/prompt", "drop_after")
    # Neither the job endpoint nor queue/history can establish submission outcome.
    prompt_id = str(uuid.uuid4())
    fake.inject("GET", f"/api/jobs/{prompt_id}", 404, 500, 500, 500)
    fake.inject("GET", "/queue", ("json", {"queue_running": [], "queue_pending": []}), 500, 500, 500)
    with pytest.raises(ComfyError) as error:
        client.submit({}, prompt_id=prompt_id)
    assert error.value.code == "submit_unknown"
    assert error.value.retryable is False
    assert fake.accepted == 1
    assert len([path for _, path, _ in fake.log if path == "/prompt"]) == 1


def test_history_protocol_error_cleans_up(fake: FakeComfy, client: ComfyClient) -> None:
    prompt_id = client.submit({})
    fake.script.clear()
    fake.inject("GET", f"/history/{prompt_id}", ("json", {prompt_id: {"status": None}}))
    with pytest.raises(ComfyError) as error:
        client.wait(prompt_id, timeout_s=1)
    assert error.value.code == "protocol"
    assert prompt_id not in fake.running + fake.pending
    assert_safe(fake, prompt_id)


def test_cleanup_failure_overrides_original_error(fake: FakeComfy, client: ComfyClient, workflow: Workflow) -> None:
    client.poll_s = 1
    # A faulty server publishes history while still keeping the job in its queue.
    prompt_id = str(uuid.uuid4())
    fake.add(prompt_id, running=True)
    fake.script.clear()
    fake.ignore_cancel = True
    entry = {"outputs": output("absent.png"), "status": {"status_str": "success", "completed": True, "messages": []}}
    fake.history[prompt_id] = entry
    record = client.reattach(prompt_id, workflow=workflow, graph=workflow.graph, values={}, timeout_s=1)
    assert record.status == "cancel_unconfirmed"
    assert record.error["detail"]["original_error"]["code"] == "missing_outputs"
    assert prompt_id in fake.running


def test_cancel_500_does_not_use_legacy_fallback(fake: FakeComfy, client: ComfyClient) -> None:
    prompt_id = str(uuid.uuid4())
    fake.add(prompt_id, running=True)
    fake.inject("POST", f"/api/jobs/{prompt_id}/cancel", 500)
    with pytest.raises(ComfyError) as error:
        client.cancel(prompt_id, confirm_s=0.02)
    assert error.value.code == "cancel_unconfirmed"
    assert not any(path == "/interrupt" or (method == "POST" and path == "/queue") for method, path, _ in fake.log)
    assert_safe(fake, prompt_id)


def test_malformed_queue_is_not_cancel_confirmation(fake: FakeComfy, client: ComfyClient) -> None:
    prompt_id = str(uuid.uuid4())
    fake.add(prompt_id, running=True)
    fake.ignore_cancel = True
    fake.inject("GET", "/queue", ("json", {}), ("json", {}))
    with pytest.raises(ComfyError) as error:
        client.cancel(prompt_id, confirm_s=0.02)
    assert error.value.code == "cancel_unconfirmed"
    assert error.value.detail["cause"]["code"] == "protocol"


def test_zero_run_budget_does_not_submit(fake: FakeComfy, client: ComfyClient, workflow: Workflow) -> None:
    record = client.run(workflow, {}, timeout_s=0)
    assert record.status == "timeout"
    assert fake.accepted == 0
    assert not any(path == "/prompt" for _, path, _ in fake.log)


def test_invalid_run_uuid_is_recorded(client: ComfyClient, workflow: Workflow) -> None:
    record = client.run(workflow, {}, timeout_s=1, prompt_id="")
    assert record.status == "rejected"
    assert record.error["prompt_id"] == ""


def test_output_minimum_total_and_invalid_metadata(client: ComfyClient, tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"image")
    with pytest.raises(ComfyError) as error:
        client.collect("id", {"outputs": output()}, expect=2)
    assert error.value.code == "missing_outputs"
    for metadata in ({"type": "output"}, {"type": "output", "filename": 42}, {"type": "output", "filename": ""}):
        with pytest.raises(ComfyError) as error:
            client.collect("id", {"outputs": {"9": {"images": [metadata]}}})
        assert error.value.code == "missing_outputs"


def test_unknown_models_without_manifest(
    fake: FakeComfy, client: ComfyClient, workflow: Workflow, tmp_path: Path
) -> None:
    client.models_tsv = tmp_path / "no-model-manifest.tsv"
    record = client.run(workflow, {}, timeout_s=1, expect=0)
    assert record.status == "success"
    assert record.models == {"toy.gguf": "unknown", "mystery.pt": "unknown"}


def test_queue_loss_right_after_submission(fake: FakeComfy, client: ComfyClient, workflow: Workflow) -> None:
    fake.script = deque(["lost"])
    record = client.run(workflow, {}, timeout_s=1)
    assert record.status == "interrupted"
    assert record.error["detail"] == {"reason": "lost_from_queue"}


@pytest.mark.parametrize("url", ["http://127.0.0.1:bad", "http://127.0.0.1:99999", "http://[::1", "http://@localhost"])
def test_malformed_url_is_config_error(tmp_path: Path, url: str) -> None:
    with pytest.raises(ComfyError) as error:
        ComfyClient(url, output_root=tmp_path)
    assert error.value.code == "protocol"
    assert error.value.stage == "config"


def test_client_interruption_cancels_ours(
    fake: FakeComfy,
    client: ComfyClient,
    workflow: Workflow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = client._http.open
    interrupted = False

    def interrupt_once(request: object, timeout: float) -> object:
        nonlocal interrupted
        if fake.accepted and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original(request, timeout=timeout)

    monkeypatch.setattr(client._http, "open", interrupt_once)
    record = client.run(workflow, {}, timeout_s=1)
    assert record.status == "interrupted"
    assert record.prompt_id not in fake.running + fake.pending
    assert_safe(fake, record.prompt_id)


def test_execution_timing_excludes_collection(
    fake: FakeComfy,
    client: ComfyClient,
    workflow: Workflow,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect = client.collect

    def slow_collect(prompt_id: str, entry: dict, *, expect: int | dict[str, int]) -> list:
        clock.sleep(3)
        return collect(prompt_id, entry, expect=expect)

    monkeypatch.setattr(client, "collect", slow_collect)
    fake.script = deque(["pending", "running", "success"])
    record = client.run(workflow, {}, timeout_s=1, expect=0)
    assert record.status == "success"
    assert 0 < record.timing["exec_s"] < 1
    assert record.timing["wall_s"] >= 3


def test_podman_exec_transport_relays_through_container(
    fake: FakeComfy, tmp_path: Path, workflow: Workflow, clock: Clock
) -> None:
    # A stand-in `podman` that drops `exec -i <name>` and runs the relay with this interpreter, as the container would.
    podman = tmp_path / "podman"
    podman.write_text(f'#!/bin/sh\n[ "$1" = exec ] && [ "$2" = -i ] || exit 99\nshift 4\nexec {sys.executable} "$@"\n')
    podman.chmod(0o755)
    (tmp_path / "out.png").write_bytes(b"png")
    fake.outputs = output("out.png")
    transport = PodmanExecTransport("sonoranlab-comfy", url=fake.url, podman=[str(podman)])
    client = ComfyClient(
        "http://127.0.0.1:8188", output_root=tmp_path, poll_s=0.01, sleep=clock.sleep, clock=clock, transport=transport
    )
    record = client.run(workflow, {"seed": 5}, timeout_s=30)
    assert record.status == "success", record.error
    assert [o.rel for o in record.outputs] == ["out.png"]
    assert record.base_url == "http://127.0.0.1:8188"
    with pytest.raises(OSError, match="relay"):
        PodmanExecTransport(podman=["false"])("/queue", None, 1.0)


def test_transport_http_status_maps_to_structured_errors(tmp_path: Path) -> None:
    def transport(path: str, data: bytes | None, timeout: float) -> tuple[int, bytes]:
        return 400, b'{"error": {"type": "prompt_outputs_failed_validation"}, "node_errors": {"7": "x"}}'

    client = ComfyClient("http://127.0.0.1:8188", output_root=tmp_path, transport=transport)
    with pytest.raises(ComfyError) as err:
        client.submit({"7": {"class_type": "X", "inputs": {}}})
    assert err.value.code == "rejected" and err.value.detail["http_status"] == 400
