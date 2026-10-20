"""Scriptable loopback ComfyUI, including ambiguous submissions and shared-queue cancellation."""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeComfy:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.history: dict[str, dict] = {}
        self.running: list[str] = []
        self.pending: list[str] = []
        self.log: list[tuple[str, str, Any]] = []
        self.faults: dict[tuple[str, str], deque] = defaultdict(deque)
        self.script: deque[str] = deque(["success"])
        self.outputs: dict = {}
        self.ignore_cancel = False
        self.jobs_supported = True
        self.cancel_supported = True
        self.cancel_status = 404
        self.accepted = 0
        self.foreign = str(uuid.uuid4())
        self.running.append(self.foreign)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.handle_request()

            def do_POST(self) -> None:
                self.handle_request()

            def handle_request(self) -> None:
                body = (
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if "Content-Length" in self.headers
                    else None
                )
                fake.log.append((self.command, self.path, body))
                faults = fake.faults[self.command, self.path]
                fault = faults.popleft() if faults else None
                delay = 0.0
                if isinstance(fault, tuple) and fault[0] == "slow":
                    delay, fault = fault[1], None
                if fault == "drop_before":
                    self.drop()
                    return
                if isinstance(fault, int):
                    self.respond(fault, {"error": {"type": "injected"}, "node_errors": {"7": "bad"}})
                    return
                status, result = fake.dispatch(self.command, self.path, body)
                if delay:
                    time.sleep(delay)
                if fault == "drop_after":
                    self.drop()
                    return
                if isinstance(fault, tuple) and fault[0] == "json":
                    result = fault[1]
                self.respond(status, result)

            def drop(self) -> None:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

            def respond(self, status: int, result: Any) -> None:
                data = json.dumps(result).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A slow response deliberately outlives the client's deadline.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.005), daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def inject(self, method: str, path: str, *actions: Any) -> None:
        self.faults[method, path].extend(actions)

    def add(self, prompt_id: str, *, running: bool = False) -> None:
        self.jobs[prompt_id] = {"id": prompt_id}
        (self.running if running else self.pending).append(prompt_id)

    def remove(self, prompt_id: str) -> None:
        self.pending[:] = [item for item in self.pending if item != prompt_id]
        self.running[:] = [item for item in self.running if item != prompt_id]

    def finish(self, prompt_id: str, event: str = "execution_success") -> None:
        self.remove(prompt_id)
        data = {"node_id": "7", "node_type": "Sampler", "exception_message": "out of memory", "traceback": ["trace"]}
        self.history[prompt_id] = {
            "prompt": [0, prompt_id, {}, {}, []],
            "outputs": self.outputs,
            "status": {
                "completed": True,
                "status_str": "success" if event == "execution_success" else "error",
                "messages": [["execution_start", {}], [event, data]],
            },
        }

    def cancel(self, prompt_id: str) -> None:
        if not self.ignore_cancel:
            self.remove(prompt_id)

    def dispatch(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        if method == "POST" and path == "/prompt":
            prompt_id = body["prompt_id"]
            if str(uuid.UUID(prompt_id)) != prompt_id:
                return 400, {"error": {"type": "invalid_prompt_id"}}
            self.accepted += 1
            self.add(prompt_id)
            return 200, {"prompt_id": prompt_id, "number": self.accepted, "node_errors": {}}
        if method == "GET" and path.startswith("/api/jobs/"):
            prompt_id = path.rsplit("/", 1)[1]
            if self.jobs_supported and prompt_id in self.jobs:
                return 200, self.jobs[prompt_id]
            return 404, {"error": "Job not found"}
        if method == "POST" and path.startswith("/api/jobs/") and path.endswith("/cancel"):
            if not self.cancel_supported:
                return self.cancel_status, {"error": "unsupported"}
            self.cancel(path.split("/")[-2])
            return 200, {}
        if path == "/queue":
            if method == "POST":
                for prompt_id in body["delete"]:
                    if prompt_id in self.pending:
                        self.cancel(prompt_id)
                return 200, {}
            return 200, {
                "queue_running": [[0, item, {}, {}, []] for item in self.running],
                "queue_pending": [[0, item, {}, {}, []] for item in self.pending],
            }
        if path == "/interrupt" and method == "POST":
            assert body.get("prompt_id"), "bare interrupt would cancel foreign work"
            if body["prompt_id"] in self.running:
                self.cancel(body["prompt_id"])
            return 200, {}
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[1]
            if prompt_id in self.jobs and prompt_id not in self.history and self.script:
                state = self.script.popleft()
                if state == "running":
                    self.remove(prompt_id)
                    self.running.append(prompt_id)
                elif state == "lost":
                    self.remove(prompt_id)
                    self.jobs.pop(prompt_id)
                elif state != "pending":
                    self.finish(
                        prompt_id,
                        {
                            "success": "execution_success",
                            "error": "execution_error",
                            "interrupted": "execution_interrupted",
                        }[state],
                    )
            return 200, {prompt_id: self.history[prompt_id]} if prompt_id in self.history else {}
        return 404, {"error": "not found"}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds
