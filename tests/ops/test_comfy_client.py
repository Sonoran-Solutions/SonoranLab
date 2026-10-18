import importlib.util
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = ROOT / "env/comfy_client.py"
SPEC = importlib.util.spec_from_file_location("comfy_client", CLIENT_PATH)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)
WORKFLOW = {"1": {"class_type": "SaveImage", "inputs": {}}}


@pytest.fixture
def comfy():
    state = SimpleNamespace(
        entry={"status": {"status_str": "success", "completed": True, "messages": []},
               "outputs": {"1": {"images": [{"subfolder": "p0", "filename": "test.png"}]}}},
        prompt_status=200, prompt_body=b'{"prompt_id": "test-id"}',
        history_body=None, delays=[], history_calls=0, posts=[],
        executing=None, interrupted=[],
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Timed-out clients may disconnect before a delayed reply.

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.posts.append((self.path, body))
            if self.path == "/prompt":
                self.respond(state.prompt_status, state.prompt_body)
            elif self.path == "/interrupt":
                # Pinned ComfyUI 0.31.0 semantics: with prompt_id, interrupt only if that prompt is running;
                # without one, interrupt whatever is running.
                target = body.get("prompt_id")
                if state.executing and (not target or target == state.executing):
                    state.interrupted.append(state.executing)
                self.respond(200, b"{}")
            else:
                self.respond(200, b"{}")

        def do_GET(self):
            assert self.path == "/history/test-id"
            state.history_calls += 1
            if state.delays:
                time.sleep(state.delays.pop(0))
            body = state.history_body
            if body is None:
                body = json.dumps({} if state.entry is None else {"test-id": state.entry}).encode()
            self.respond(200, body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run(base, *args, workflow=WORKFLOW):
    stdout = io.StringIO()
    rc = client.main(["--base", base, "--timeout", "3", "--poll", "0.01", *args],
                     stdin=io.StringIO(json.dumps(workflow)), stdout=stdout)
    assert len(stdout.getvalue().splitlines()) == 1
    result = json.loads(stdout.getvalue())
    assert result["exit_code"] == rc
    assert isinstance(result["wall_s"], float)
    assert set(result) == {"status", "exit_code", "prompt_id", "wall_s", "images", "errors", "detail"}
    return rc, result


def test_success_with_output_file(comfy, tmp_path):
    (tmp_path / "p0").mkdir()
    (tmp_path / "p0/test.png").write_bytes(b"image")
    rc, result = run(comfy.base, "--output-dir", str(tmp_path))
    assert (rc, result["status"]) == (0, "success")
    assert result["images"] == ["p0/test.png"]
    path, body = comfy.posts[0]
    assert path == "/prompt" and body["prompt"] == WORKFLOW
    assert uuid.UUID(body["client_id"]).version == 4


def test_execution_error(comfy):
    message = ["execution_error", {"exception_message": "failed"}]
    comfy.entry["status"].update(status_str="error", messages=[message])
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (3, "execution_error")
    assert result["errors"] == [message]


def test_success_but_incomplete(comfy):
    comfy.entry["status"]["completed"] = False
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (3, "execution_error")


def test_rejected_prompt(comfy):
    comfy.prompt_status = 400
    comfy.prompt_body = b"workflow validation failed"
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (2, "rejected")
    assert "workflow validation failed" in result["detail"]


def test_zero_images(comfy):
    comfy.entry["outputs"] = {}
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (5, "missing_outputs")


def test_missing_output_file(comfy, tmp_path):
    rc, result = run(comfy.base, "--output-dir", str(tmp_path))
    assert (rc, result["status"]) == (5, "missing_outputs")


def test_history_never_appears(comfy):
    comfy.entry = None
    started = time.monotonic()
    rc, result = run(comfy.base, "--timeout", "1.5", "--poll", "0.1")
    assert (rc, result["status"]) == (4, "timeout")
    assert 1.4 <= time.monotonic() - started < 2.5
    assert ("/interrupt", {"prompt_id": "test-id"}) in comfy.posts
    assert ("/interrupt", {}) not in comfy.posts
    assert ("/queue", {"delete": ["test-id"]}) in comfy.posts


def test_timeout_does_not_interrupt_another_clients_prompt(comfy):
    comfy.entry = None              # our prompt never completes ...
    comfy.executing = "other-id"    # ... and by the deadline someone else's job is the one running
    rc, result = run(comfy.base, "--timeout", "0.8", "--poll", "0.1")
    assert (rc, result["status"]) == (4, "timeout")
    assert comfy.interrupted == []
    assert ("/interrupt", {"prompt_id": "test-id"}) in comfy.posts
    assert ("/queue", {"delete": ["test-id"]}) in comfy.posts


def test_timeout_interrupts_our_own_running_prompt(comfy):
    comfy.entry = None
    comfy.executing = "test-id"
    rc, _ = run(comfy.base, "--timeout", "0.8", "--poll", "0.1")
    assert rc == 4 and comfy.interrupted == ["test-id"]


def test_poll_http_timeout_is_retried(comfy):
    comfy.delays = [0.7]
    rc, result = run(comfy.base, "--http-timeout", "0.3")
    assert (rc, result["status"]) == (0, "success")
    assert comfy.history_calls >= 2


def test_nothing_listening():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        rc, result = run(f"http://127.0.0.1:{sock.getsockname()[1]}")
    assert (rc, result["status"]) == (6, "http_error")


def test_smoke_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "env/smoke.sh")], check=True)


def test_smoke_has_no_embedded_client():
    script = (ROOT / "env/smoke.sh").read_text()
    assert script.count("urllib.request.Request(") == 0
    assert "comfy_client.py" in script


@pytest.mark.parametrize("endpoint", ["prompt", "history"])
def test_malformed_response(comfy, endpoint):
    setattr(comfy, f"{endpoint}_body", b"not JSON")
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (6, "http_error")


@pytest.mark.parametrize("message_type", ["execution_error", "execution_interrupted"])
def test_error_message_overrides_success(comfy, message_type):
    comfy.entry["status"]["messages"] = [[message_type, {"reason": "failed"}]]
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (3, "execution_error")


def test_empty_output_file(comfy, tmp_path):
    (tmp_path / "p0").mkdir()
    (tmp_path / "p0/test.png").touch()
    rc, result = run(comfy.base, "--output-dir", str(tmp_path))
    assert (rc, result["status"]) == (5, "missing_outputs")


def test_output_cannot_escape_directory(comfy, tmp_path):
    (tmp_path / "outside.png").write_bytes(b"image")
    comfy.entry["outputs"]["1"]["images"] = [{"subfolder": "..", "filename": "outside.png"}]
    rc, result = run(comfy.base, "--output-dir", str(tmp_path / "output"))
    assert (rc, result["status"]) == (5, "missing_outputs")


def test_empty_subfolder_has_no_leading_slash(comfy):
    comfy.entry["outputs"]["1"]["images"][0]["subfolder"] = ""
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (0, "success")
    assert result["images"] == ["test.png"]


def test_rejection_detail_is_truncated(comfy):
    comfy.prompt_status = 500
    comfy.prompt_body = b"x" * 3000
    rc, result = run(comfy.base)
    assert (rc, result["status"]) == (2, "rejected")
    assert result["detail"] == "x" * 2000


def test_overall_timeout_caps_http_timeout(comfy):
    comfy.delays = [2]
    started = time.monotonic()
    rc, result = run(comfy.base, "--timeout", "0.4", "--http-timeout", "15")
    assert (rc, result["status"]) == (4, "timeout")
    assert time.monotonic() - started < 1


def test_runs_via_python_c_without_file(comfy):
    process = subprocess.run(
        [sys.executable, "-c", CLIENT_PATH.read_text(), "--base", comfy.base],
        input=json.dumps(WORKFLOW), capture_output=True, text=True, timeout=5,
    )
    assert process.returncode == 0
    assert json.loads(process.stdout)["status"] == "success"


FAKE_PODMAN_FOR_SMOKE = """#!/usr/bin/env bash
case "$1" in
  exec) cat > /dev/null; [ -n "$FAKE_OUT" ] && printf '%s\\n' "$FAKE_OUT"; exit "$FAKE_RC" ;;
  inspect) echo fake ;;
esac
"""


@pytest.mark.parametrize("fake_out,fake_rc,want_rc,want_status", [
    ('{"status": "execution_error", "exit_code": 3, "images": [], "errors": [["execution_error", {}]]}', 3, 3, "execution_error"),
    ("", 125, 6, "no-response"),
    ('{"status": "success", "exit_code": 0, "images": [], "errors": []}', 0, 0, "success"),
])
def test_smoke_sh_propagates_client_result(tmp_path, fake_out, fake_rc, want_rc, want_status):
    podman = tmp_path / "podman.sh"
    podman.write_text(FAKE_PODMAN_FOR_SMOKE)
    wf = tmp_path / "wf.json"
    wf.write_text("{}")
    env = {**os.environ, "SONORANLAB_PODMAN": f"bash {podman}", "SONORANLAB_WORK": str(tmp_path / "work"),
           "FAKE_OUT": fake_out, "FAKE_RC": str(fake_rc)}
    r = subprocess.run(["bash", str(ROOT / "env" / "smoke.sh"), str(wf), "t"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == want_rc, r.stderr
    manifest = json.loads((tmp_path / "work" / "runs" / "t.json").read_text())
    assert manifest["status"] == want_status
