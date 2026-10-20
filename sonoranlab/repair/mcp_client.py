"""A request is written once; a missing reply is never evidence it did not execute."""

from __future__ import annotations

import json
import os
import queue
import selectors
import subprocess
import threading
import time


class OutcomeUnknown(RuntimeError):
    pass


class Disconnected(RuntimeError):
    pass


class ToolError(RuntimeError):
    pass


class MCPClient:
    # ponytail: stdlib client instead of the mcp SDK; upgrade if Phase 4 adds the SDK.
    def __init__(self, process: subprocess.Popen, timeout: float = 22) -> None:
        self.process, self.timeout = process, timeout
        self.pending: dict[int, queue.Queue] = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.dead = False
        os.set_blocking(process.stdin.fileno(), False)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                reply = json.loads(line)
                with self.lock:
                    waiter = self.pending.get(reply.get("id"))
                    if waiter is not None:
                        waiter.put(reply)
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            with self.lock:
                self.dead = True
                for waiter in self.pending.values():
                    waiter.put(None)

    def rpc(self, method: str, params: dict, *, notify: bool = False, timeout: float | None = None) -> dict:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self.lock:
            if self.dead:
                raise Disconnected("upstream EOF")
            self.sequence += 1
            ident = self.sequence
            message = {"jsonrpc": "2.0", "method": method, "params": params}
            waiter: queue.Queue = queue.Queue()
            if not notify:
                message["id"] = ident
                self.pending[ident] = waiter
            try:
                remaining = memoryview((json.dumps(message) + "\n").encode())
                with selectors.DefaultSelector() as selector:
                    selector.register(self.process.stdin, selectors.EVENT_WRITE)
                    while remaining:
                        if time.monotonic() >= deadline or not selector.select(max(0, deadline - time.monotonic())):
                            # A partial JSON line cannot be safely followed by another request.
                            self.dead = True
                            raise OutcomeUnknown(f"write timed out for request {ident}")
                        try:
                            sent = os.write(self.process.stdin.fileno(), remaining[:65536])
                            remaining = remaining[sent:]
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.dead = True
                                raise OutcomeUnknown(f"write timed out for request {ident}") from None
            except OutcomeUnknown:
                self.pending.pop(ident, None)
                raise
            except (OSError, ValueError) as e:
                self.pending.pop(ident, None)
                self.dead = True
                raise Disconnected("upstream pipe closed") from e
        if notify:
            return {}
        try:
            reply = waiter.get(timeout=max(0, deadline - time.monotonic()))
            if reply is None:
                raise Disconnected("upstream EOF")
            if "error" in reply:
                raise ToolError(str(reply["error"]))
            return reply["result"]
        except queue.Empty as e:
            raise OutcomeUnknown(f"no reply for request {ident}") from e
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def initialize(self) -> None:
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "sonoranlab-repair", "version": "1"},
            },
            timeout=max(self.timeout, 10),
        )
        self.rpc("notifications/initialized", {}, notify=True)

    def call(self, name: str, arguments: dict, *, timeout: float | None = None) -> dict:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if result.get("isError"):
            error = json.dumps(result)
            if "not_connected" in error or "doNotFallBackToDisk" in error:
                raise Disconnected(error)
            if any(word in error.lower() for word in ("timeout", "timed out")):
                raise OutcomeUnknown(error)
            raise ToolError(error)
        structured = result.get("structuredContent", result)
        if isinstance(structured, dict) and any(block.get("type") == "image" for block in result.get("content", [])):
            return {**structured, "content": result["content"]}
        return structured

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                stream.close()
