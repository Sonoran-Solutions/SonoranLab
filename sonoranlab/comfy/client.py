"""Local ComfyUI transport, prompt-scoped cleanup and reproducible run records."""

from __future__ import annotations

import base64
import csv
import hashlib
import http.client
import json
import math
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def graph_sha256(graph: dict) -> str:
    return hashlib.sha256(json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# (path, JSON body or None for GET, timeout_s) -> (HTTP status, body). Raises OSError when the server is unreachable.
Transport = Callable[[str, "bytes | None", float], "tuple[int, bytes]"]

# Runs inside the container (stdlib only): one HTTP request to its own ComfyUI, the result as one JSON line.
_RELAY = r"""
import base64, json, sys, urllib.error, urllib.request
q = json.load(sys.stdin)
data = None if q["body"] is None else base64.b64decode(q["body"])
req = urllib.request.Request(q["url"], data=data, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=q["timeout"]) as r:
        code, raw = r.status, r.read()
except urllib.error.HTTPError as e:
    code, raw = e.code, e.read()
except Exception as e:
    print(json.dumps({"error": f"{type(e).__name__}: {e}"})); sys.exit(0)
print(json.dumps({"status": code, "body": base64.b64encode(raw).decode()}))
"""


class PodmanExecTransport:
    """Reach a ComfyUI started with `env/comfy.sh up --offline` (`--network=none`): its API exists only inside the
    container, so each request is relayed by `podman exec -i <container> python -c <relay>` to 127.0.0.1:8188 there.
    Use with `ComfyClient("http://127.0.0.1:8188", transport=PodmanExecTransport(), ...)`."""

    def __init__(
        self,
        container: str = "sonoranlab-comfy",
        *,
        url: str = "http://127.0.0.1:8188",
        podman: list[str] | None = None,
    ) -> None:
        self.container, self.url = container, url.rstrip("/")
        self.podman = podman if podman is not None else ["podman"]

    def __call__(self, path: str, data: bytes | None, timeout: float) -> tuple[int, bytes]:
        query = {"url": self.url + path, "body": None if data is None else base64.b64encode(data).decode()}
        query["timeout"] = timeout
        argv = [*self.podman, "exec", "-i", self.container, "python", "-c", _RELAY]
        try:  # podman exec itself costs ~0.3 s; give it room beyond the HTTP timeout
            proc = subprocess.run(argv, input=json.dumps(query), capture_output=True, text=True, timeout=timeout + 15)
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"podman exec relay timed out after {timeout + 15:.0f}s") from exc
        try:
            reply = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise OSError(f"podman exec relay failed (rc {proc.returncode}): {proc.stderr.strip()[:500]}") from exc
        if "error" in reply:
            raise OSError(f"in-container request failed: {reply['error']}")
        return int(reply["status"]), base64.b64decode(reply["body"])


@dataclass(frozen=True)
class Workflow:
    source: Path
    graph: dict
    params: dict

    @classmethod
    def load(cls, path: Path) -> Workflow:
        path = Path(path)
        graph = json.loads(path.read_text())
        if not isinstance(graph, dict) or "nodes" in graph or "links" in graph:
            raise ValueError(f"{path}: export an API-format workflow, not a UI graph")
        params = yaml.safe_load(path.with_suffix(".params.yaml").read_text())
        if not isinstance(params, dict):
            raise ValueError(f"{path}: params sidecar must be a mapping")
        return cls(path, graph, params)

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def patch(self, values: dict[str, Any]) -> dict:
        graph = deepcopy(self.graph)
        for name, value in values.items():
            if name not in self.params:
                raise ValueError(f"Unknown param {name!r}; known: {sorted(self.params)}")
            targets = self.params[name]
            for target in targets if isinstance(targets, list) else [targets]:
                try:
                    node, key = target["node"], target["input"]
                    if key not in graph[node]["inputs"]:
                        raise KeyError(key)
                    graph[node]["inputs"][key] = deepcopy(value)
                except (KeyError, TypeError) as exc:
                    raise ValueError(f"Param {name!r}: missing node/input in graph: {target!r}") from exc
        return graph


class ComfyError(Exception):
    code: str
    stage: str
    message: str
    retryable: bool
    prompt_id: str | None
    detail: dict

    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        retryable: bool = False,
        prompt_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.retryable = retryable
        self.prompt_id = prompt_id
        self.detail = detail if detail is not None else {}

    def to_json(self) -> dict:
        return {key: getattr(self, key) for key in ("code", "stage", "message", "retryable", "prompt_id", "detail")}


@dataclass(frozen=True)
class OutputFile:
    node: str
    kind: str
    rel: str
    path: Path
    sha256: str
    bytes: int


@dataclass
class RunRecord:
    prompt_id: str
    client_id: str
    base_url: str
    workflow_source: str
    workflow_source_sha256: str
    workflow_patched_sha256: str
    params: dict
    models: dict[str, str]
    outputs: list[OutputFile]
    status: str
    submitted_at: str
    timing: dict[str, float]
    messages: list
    error: dict | None

    def to_json(self) -> dict:
        result = asdict(self)
        for output in result["outputs"]:
            output["path"] = str(output["path"])
        return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A local endpoint must not silently redirect a workflow off the machine.
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _duration(value: float, name: str, *, zero: bool = False) -> None:
    if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")


def _expect(expect: int | dict[str, int]) -> None:
    counts = expect.values() if isinstance(expect, dict) else [expect]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("expect must contain nonnegative integer counts")


class ComfyClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8189",
        *,
        output_root: Path,
        http_timeout: float = 15.0,
        poll_s: float = 1.0,
        allow_remote: bool = False,
        models_tsv: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transport: Transport | None = None,
    ) -> None:
        try:
            url = urllib.parse.urlsplit(base_url)
            _ = url.port  # Validate malformed ports before any request.
        except ValueError as exc:
            raise ComfyError("protocol", "config", f"Invalid ComfyUI base URL: {base_url}") from exc
        if url.hostname not in {"127.0.0.1", "::1", "localhost"} and not allow_remote:
            raise ComfyError(
                "remote_refused", "config", f"Refusing remote ComfyUI at {base_url}; opt in with allow_remote"
            )
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
        ):
            raise ComfyError("protocol", "config", f"Invalid ComfyUI base URL: {base_url}")
        _duration(http_timeout, "http_timeout")
        _duration(poll_s, "poll_s")
        self.base_url = base_url.rstrip("/")
        self.output_root = Path(output_root).expanduser().resolve()
        self.http_timeout = http_timeout
        self.poll_s = poll_s
        self.models_tsv = (
            Path(models_tsv) if models_tsv is not None else Path(__file__).resolve().parents[2] / "env/models.tsv"
        )
        self.sleep, self.clock = sleep, clock
        self.client_id = str(uuid.uuid4())
        self._attempted: set[str] = set()
        self._observed: dict[str, dict] = {}
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._transport: Transport = transport if transport is not None else self._urllib

    def _urllib(self, path: str, data: bytes | None, timeout: float) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with self._http.open(req, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                with exc:
                    return exc.code, exc.read()
            except (OSError, http.client.HTTPException):
                return exc.code, b""  # The HTTP status still establishes rejection/failure if its body was lost.

    def _request(
        self, path: str, *, stage: str, prompt_id: str, body: dict | None = None, deadline: float | None = None
    ) -> dict:
        timeout = self.http_timeout if deadline is None else min(self.http_timeout, deadline - self.clock())
        if timeout <= 0:
            raise ComfyError(
                "timeout",
                stage,
                "Request deadline exceeded; retry after prompt cleanup",
                retryable=True,
                prompt_id=prompt_id,
            )
        data = json.dumps(body).encode() if body is not None else None
        try:
            code, raw = self._transport(path, data, timeout)
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise ComfyError(
                "unreachable",
                stage,
                f"Server unreachable at {self.base_url}; is env/comfy.sh up running?",
                retryable=True,
                prompt_id=prompt_id,
                detail={"cause": str(exc)},
            ) from exc
        if code >= 400:
            try:
                detail = json.loads(raw)
            except (ValueError, UnicodeError):
                detail = {"body": raw.decode(errors="replace")[:2000]}
            detail = detail if isinstance(detail, dict) else {"body": detail}
            raise ComfyError(
                "rejected" if stage == "submit" and code == 400 else "http_error",
                stage,
                f"ComfyUI {path} returned HTTP {code}; inspect detail and server logs",
                retryable=code >= 500,
                prompt_id=prompt_id,
                detail={**detail, "http_status": code},
            )
        try:
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError("expected an object")
            return result
        except (ValueError, UnicodeError) as exc:
            raise ComfyError(
                "protocol", stage, f"Invalid JSON object from {path}; inspect ComfyUI server logs", prompt_id=prompt_id
            ) from exc

    @staticmethod
    def _id_path(prompt_id: str) -> str:
        return urllib.parse.quote(prompt_id, safe="")

    def _state(self, prompt_id: str, *, deadline: float | None = None) -> str:
        queue = self._request("/queue", stage="wait", prompt_id=prompt_id, deadline=deadline)
        for key, state in (("queue_running", "running"), ("queue_pending", "pending")):
            rows = queue.get(key)
            if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) < 2 for row in rows):
                raise ComfyError(
                    "protocol", "wait", "Malformed /queue; cannot confirm prompt state", prompt_id=prompt_id
                )
            if any(row[1] == prompt_id for row in rows):
                return state
        history = self._request(
            "/history/" + self._id_path(prompt_id), stage="wait", prompt_id=prompt_id, deadline=deadline
        )
        return "done" if prompt_id in history else "unknown"

    def _lookup(self, prompt_id: str, *, deadline: float | None = None) -> bool:
        try:
            job = self._request(
                "/api/jobs/" + self._id_path(prompt_id), stage="submit", prompt_id=prompt_id, deadline=deadline
            )
            if job:
                return True
        except ComfyError as exc:
            if exc.code == "timeout":
                raise
            # Older servers lack jobs; queue + history remain authoritative.
        return self._state(prompt_id, deadline=deadline) != "unknown"

    def submit(self, graph: dict, *, prompt_id: str | None = None) -> str:
        return self._submit(graph, prompt_id=prompt_id)

    def _submit(self, graph: dict, *, prompt_id: str | None, deadline: float | None = None) -> str:
        supplied = prompt_id is not None
        prompt_id = prompt_id if supplied else str(uuid.uuid4())
        try:
            valid = str(uuid.UUID(prompt_id)) == prompt_id
        except (ValueError, AttributeError, TypeError):
            valid = False
        if not valid:
            raise ComfyError(
                "rejected", "submit", "prompt_id must be a canonical lowercase hyphenated UUID", prompt_id=prompt_id
            )
        if prompt_id in self._attempted:
            raise ComfyError(
                "rejected", "submit", "prompt_id already attempted; use reattach, never resubmit", prompt_id=prompt_id
            )
        if supplied:
            try:
                found = self._lookup(prompt_id, deadline=deadline)
            except ComfyError as exc:
                if exc.code == "timeout":
                    raise
                raise ComfyError(
                    "submit_unknown",
                    "submit",
                    "Cannot check persisted ID; restore the server and reattach this ID",
                    prompt_id=prompt_id,
                    detail={"cause": exc.to_json()},
                ) from exc
            if found:
                raise ComfyError("rejected", "submit", "prompt_id already exists; use reattach", prompt_id=prompt_id)
        self._attempted.add(prompt_id)
        body = {"prompt": graph, "client_id": self.client_id, "prompt_id": prompt_id}
        for attempt in range(3):
            try:
                response = self._request("/prompt", stage="submit", prompt_id=prompt_id, body=body, deadline=deadline)
                if response.get("prompt_id") != prompt_id:
                    raise ComfyError("protocol", "submit", "Mismatched prompt_id from /prompt", prompt_id=prompt_id)
                self._observation(prompt_id)["seen"] = True
                return prompt_id
            except ComfyError as exc:
                if exc.code == "timeout" or (exc.code not in {"unreachable", "protocol"} and not exc.retryable):
                    raise
                for lookup_attempt in range(3):
                    try:
                        found = self._lookup(prompt_id)
                        break
                    except ComfyError as lookup_error:
                        if lookup_attempt == 2:
                            raise ComfyError(
                                "submit_unknown",
                                "submit",
                                f"Submission outcome unknown at {self.base_url}; "
                                "restore env/comfy.sh and reattach this ID; "
                                "do not submit a new ID",
                                prompt_id=prompt_id,
                                detail={"cause": lookup_error.to_json()},
                            ) from lookup_error
                        self.sleep(0.1 * 2**lookup_attempt)
                if found:
                    self._observation(prompt_id)["seen"] = True
                    return prompt_id
                if attempt == 2:
                    raise
                self.sleep(0.1 * 2**attempt)
        raise AssertionError("unreachable")

    def status(self, prompt_id: str) -> str:
        return self._state(prompt_id)

    def _observation(self, prompt_id: str) -> dict:
        return self._observed.setdefault(
            prompt_id, {"seen": False, "running_at": None, "poll_errors": 0, "messages": []}
        )

    def wait(self, prompt_id: str, *, timeout_s: float) -> dict:
        _duration(timeout_s, "timeout_s", zero=True)
        deadline = self.clock() + timeout_s
        observed = self._observation(prompt_id)
        missing = 0
        while self.clock() < deadline:
            try:
                history = self._request(
                    "/history/" + self._id_path(prompt_id),
                    stage="wait",
                    prompt_id=prompt_id,
                    deadline=deadline,
                )
                if self.clock() >= deadline:
                    break
                if prompt_id in history:
                    entry = history[prompt_id]
                    try:
                        status = entry["status"]
                        messages = status["messages"]
                        if not isinstance(messages, list):
                            raise TypeError
                        observed["messages"] = messages
                        for event, data in messages:
                            if event == "execution_start" and observed["running_at"] is None:
                                observed["running_at"] = self.clock()
                            if event in {"execution_error", "execution_interrupted"}:
                                raise ComfyError(
                                    "execution_error" if event == "execution_error" else "interrupted",
                                    "wait",
                                    f"ComfyUI {event}: {data.get('exception_message', 'inspect history messages')}",
                                    retryable=event == "execution_interrupted",
                                    prompt_id=prompt_id,
                                    detail=data,
                                )
                        if status["status_str"] == "success" and status["completed"] is True:
                            observed["finished_at"] = self.clock()
                            return entry
                        if status["status_str"] == "error":
                            raise ComfyError(
                                "execution_error",
                                "wait",
                                "ComfyUI execution failed; inspect history",
                                prompt_id=prompt_id,
                                detail={"status": status},
                            )
                    except (KeyError, TypeError, ValueError, AttributeError) as exc:
                        raise ComfyError(
                            "protocol", "wait", "Malformed history entry; inspect server logs", prompt_id=prompt_id
                        ) from exc
                    observed["seen"], missing = True, 0
                else:
                    state = self._state(prompt_id, deadline=deadline)
                    if state in {"pending", "running", "done"}:
                        observed["seen"], missing = True, 0
                        if state == "running" and observed["running_at"] is None:
                            observed["running_at"] = self.clock()
                    elif observed["seen"]:
                        missing += 1
                        if missing > 3:
                            raise ComfyError(
                                "interrupted",
                                "wait",
                                "Prompt disappeared; server restarted or queue was wiped; retry safely",
                                retryable=True,
                                prompt_id=prompt_id,
                                detail={"reason": "lost_from_queue"},
                            )
            except ComfyError as exc:
                if exc.code == "timeout":
                    break
                if exc.code != "unreachable" and not (exc.code == "http_error" and exc.retryable):
                    observed["finished_at"] = self.clock()
                    error = self._cleanup(prompt_id, exc)
                    if error is exc:
                        raise
                    raise error from exc
                observed["poll_errors"] += 1
                missing = 0  # An unreachable server is not evidence that the prompt vanished.
            self.sleep(min(self.poll_s, max(0, deadline - self.clock())))
        observed["finished_at"] = self.clock()
        error = ComfyError(
            "timeout",
            "wait",
            "Generation deadline exceeded; prompt cancellation confirmed; retry safely",
            retryable=True,
            prompt_id=prompt_id,
        )
        raise self._cleanup(prompt_id, error)

    def _cleanup(self, prompt_id: str, error: ComfyError) -> ComfyError:
        if error.code != "cancel_unconfirmed" and not self._observation(prompt_id).get("cancelled"):
            try:
                self.cancel(prompt_id)
            except ComfyError as cleanup_error:
                cleanup_error.detail["original_error"] = error.to_json()
                return cleanup_error
        return error

    def cancel(self, prompt_id: str, *, confirm_s: float = 30.0) -> bool:
        _duration(confirm_s, "confirm_s", zero=True)
        deadline = self.clock() + confirm_s
        cause: dict = {}
        try:
            self._request(
                "/api/jobs/" + self._id_path(prompt_id) + "/cancel",
                stage="cancel",
                prompt_id=prompt_id,
                body={},
                deadline=deadline,
            )
        except ComfyError as exc:
            cause = exc.to_json()
            if exc.detail.get("http_status") in {404, 405}:
                for path, body in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {"prompt_id": prompt_id})):
                    try:
                        self._request(
                            path,
                            stage="cancel",
                            prompt_id=prompt_id,
                            body=body,
                            deadline=deadline,
                        )
                    except ComfyError as fallback_error:
                        cause = fallback_error.to_json()
        while self.clock() < deadline:
            try:
                if self._state(prompt_id, deadline=deadline) in {"done", "unknown"}:
                    self._observation(prompt_id)["cancelled"] = True
                    return True
            except ComfyError as exc:
                cause = exc.to_json()
            self.sleep(min(self.poll_s, max(0, deadline - self.clock())))
        raise ComfyError(
            "cancel_unconfirmed",
            "cancel",
            f"Cancellation unconfirmed for {prompt_id}; GPU work may still be live; "
            "restore the server and cancel/reattach this ID before releasing the run window",
            prompt_id=prompt_id,
            detail={"cause": cause},
        )

    def collect(self, prompt_id: str, entry: dict, *, expect: int | dict[str, int] = 1) -> list[OutputFile]:
        _expect(expect)
        files = []
        try:
            for node, output in entry.get("outputs", {}).items():
                for kind, items in output.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict) or item.get("type") != "output":
                            continue
                        subfolder, filename = item.get("subfolder", ""), item["filename"]
                        if not isinstance(subfolder, str) or not isinstance(filename, str) or not filename:
                            raise ValueError("invalid subfolder/filename")
                        rel = Path(subfolder) / filename
                        if Path(subfolder).is_absolute() or Path(filename).is_absolute() or ".." in rel.parts:
                            raise ValueError(f"unsafe output path: {rel}")
                        path = (self.output_root / rel).resolve()
                        if not path.is_relative_to(self.output_root) or not path.is_file() or path.stat().st_size == 0:
                            raise ValueError(f"missing, empty, or escaping output: {rel}")
                        with path.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        files.append(OutputFile(str(node), kind, rel.as_posix(), path, digest, path.stat().st_size))
            counts = (
                {node: sum(file.node == node for file in files) for node in expect} if isinstance(expect, dict) else {}
            )
            insufficient = (
                len(files) < expect
                if isinstance(expect, int)
                else any(counts[n] < count for n, count in expect.items())
            )
            if insufficient:
                raise ValueError(f"expected at least {expect} output files; got {counts if counts else len(files)}")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ComfyError(
                "missing_outputs", "collect", f"{exc}; check output_root and workflow output nodes", prompt_id=prompt_id
            ) from exc
        return files

    def _models(self, graph: dict) -> dict[str, str]:
        rows = []
        if self.models_tsv.is_file():
            with self.models_tsv.open(newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
        names = {
            "unet_name",
            "ckpt_name",
            "lora_name",
            "clip_name",
            "clip_name1",
            "clip_name2",
            "vae_name",
            "model_name",
            "model",
        }
        result = {}
        for node in graph.values():
            for key, value in node.get("inputs", {}).items():
                if key in names and isinstance(value, str):
                    matches = [row for row in rows if value.endswith(Path(row["dest"]).name)]
                    if matches:
                        result.update({row["dest"]: row["sha256"] for row in matches})
                    else:
                        result[value] = "unknown"
        return result

    def run(
        self,
        workflow: Workflow,
        values: dict[str, Any],
        *,
        timeout_s: float,
        expect: int | dict[str, int] = 1,
        prompt_id: str | None = None,
    ) -> RunRecord:
        graph = workflow.patch(values)
        return self._run(
            workflow,
            graph,
            values,
            timeout_s,
            expect,
            prompt_id if prompt_id is not None else str(uuid.uuid4()),
            submit=True,
        )

    def reattach(
        self,
        prompt_id: str,
        *,
        timeout_s: float,
        workflow: Workflow,
        graph: dict,
        values: dict,
        expect: int | dict[str, int] = 1,
    ) -> RunRecord:
        return self._run(workflow, graph, values, timeout_s, expect, prompt_id, submit=False)

    def _run(
        self,
        workflow: Workflow,
        graph: dict,
        values: dict,
        timeout_s: float,
        expect: int | dict[str, int],
        prompt_id: str,
        *,
        submit: bool,
    ) -> RunRecord:
        _duration(timeout_s, "timeout_s", zero=True)
        _expect(expect)
        start = self.clock()
        self._observed.pop(prompt_id, None)
        record = RunRecord(
            prompt_id,
            self.client_id,
            self.base_url,
            str(workflow.source),
            "",
            graph_sha256(graph),
            deepcopy(values),
            {},
            [],
            "success",
            datetime.now(UTC).isoformat(),
            {},
            [],
            None,
        )
        submitted = not submit
        submit_s = 0.0
        try:
            try:
                record.workflow_source_sha256 = workflow.source_sha256
                record.models = self._models(graph)
            except (OSError, KeyError, TypeError, ValueError, csv.Error) as exc:
                raise ComfyError("protocol", "config", f"Cannot read workflow/model provenance: {exc}") from exc
            if submit:
                try:
                    self._submit(graph, prompt_id=prompt_id, deadline=start + timeout_s)
                    submitted = True
                except ComfyError as exc:
                    submitted = exc.code not in {"rejected"}
                    raise
                finally:
                    submit_s = self.clock() - start
            entry = self.wait(prompt_id, timeout_s=max(0, timeout_s - (self.clock() - start)))
            record.outputs = self.collect(prompt_id, entry, expect=expect)
        except (ComfyError, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                self._observation(prompt_id)["finished_at"] = self.clock()
                exc = ComfyError(
                    "interrupted",
                    "wait",
                    "Client interrupted; cancel this prompt before retrying",
                    retryable=True,
                    prompt_id=prompt_id,
                )
                submitted = submitted or prompt_id in self._attempted
            if submitted:
                exc = self._cleanup(prompt_id, exc)
            if exc.prompt_id is None:
                exc.prompt_id = prompt_id
            record.status, record.error = exc.code, exc.to_json()
        observed = self._observation(prompt_id)
        end = self.clock()
        running = observed["running_at"]
        finished = observed.get("finished_at", start + submit_s)
        record.messages = deepcopy(observed["messages"])
        record.timing = {
            "submit_s": submit_s,
            "queue_wait_s": max(0, (running if running is not None else finished) - start - submit_s),
            "exec_s": max(0, finished - running) if running is not None else 0.0,
            "wall_s": end - start,
            "poll_errors": float(observed["poll_errors"]),
        }
        return record
