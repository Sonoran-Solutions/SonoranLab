"""Private nested display, sandbox lifetime, and authenticated local request transport."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TextIO

from sonoranlab.repair import EditorUnavailable
from sonoranlab.repair.authorization import Denied, canonical, lease, open_private, persist, require, safe_tree
from sonoranlab.repair.broker import Broker
from sonoranlab.repair.compare import read_json
from sonoranlab.repair.confine import admit_repair
from sonoranlab.repair.mcp_client import MCPClient
from sonoranlab.repair.operations import request_depth
from sonoranlab.repair.runtime import AsepriteConverter, bwrap_argv, locate, no_symlinks

MAX_REQUEST = 4 * 1024 * 1024


class Editor:
    def __init__(self, process: subprocess.Popen, xephyr: subprocess.Popen, log: TextIO) -> None:
        self.client, self.xephyr, self.log = MCPClient(process), xephyr, log

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.xephyr.terminate()
            try:
                self.xephyr.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.xephyr.kill()
                self.xephyr.wait()
            self.log.close()


def kill_with_parent(parent: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")
    if os.getppid() != parent:  # Parent may have died before prctl.
        os.kill(os.getpid(), signal.SIGKILL)


def socket_path(repair: Path) -> Path:
    path = no_symlinks(repair / "session/broker.sock")
    require(len(os.fsencode(path)) <= 107, "socket_path_too_long")
    return path


def launch_editor(repair: Path) -> Editor:
    runtime = locate()
    for tool in ("Xephyr", "xauth", "mcookie"):
        if not shutil.which(tool):
            raise EditorUnavailable(f"{tool} unavailable")
    session = repair / "session"
    safe_tree(session / "home")
    xauth = no_symlinks(session / "xauth")
    fd = open_private(xauth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.close(fd)
    ini = no_symlinks(session / "home/.config/aseprite/aseprite.ini")
    # Aseprite may key extension trust by the installed path rather than its mount source.
    key = hashlib.sha1(
        b"/home/sbx/.config/aseprite/extensions/aseprite-ai-artist/ai-artist.lua", usedforsecurity=False
    ).hexdigest()
    import configparser

    config = configparser.ConfigParser()
    config.read(ini)
    if not config.has_section("script_access"):
        config.add_section("script_access")
    config.set("script_access", key, "31")
    fd = open_private(ini, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w") as f:
        config.write(f)
    require(bool(os.environ.get("DISPLAY")), "editor_not_connected")
    xephyr = None
    log = os.fdopen(open_private(session / "editor.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND), "a")
    try:
        deadline = time.monotonic() + 10
        for number in range(50, 500):
            if time.monotonic() >= deadline:
                break
            x_socket = Path(f"/tmp/.X11-unix/X{number}")
            if x_socket.exists() or Path(f"/tmp/.X{number}-lock").exists():
                continue
            cookie = subprocess.check_output(["mcookie"], text=True, timeout=5).strip()
            subprocess.run(
                ["xauth", "-f", str(xauth), "add", f":{number}", ".", cookie],
                check=True,
                capture_output=True,
                timeout=5,
            )
            parent = os.getpid()
            xephyr = subprocess.Popen(
                [
                    "Xephyr",
                    f":{number}",
                    "-auth",
                    str(xauth),
                    "-nolisten",
                    "tcp",
                    "-screen",
                    "1400x900",
                    "-title",
                    f"SonoranLab repair: {read_json(repair / 'repair.json')['repair_id']}",
                ],
                preexec_fn=lambda parent=parent: kill_with_parent(parent),
                stdout=log,
                stderr=log,
            )
            for _ in range(100):
                if xephyr.poll() is not None:
                    break
                if x_socket.is_socket():
                    # The X server lock must belong to our process, including display-number races.
                    lock_pid = Path(f"/tmp/.X{number}-lock").read_text().strip()
                    if lock_pid == str(xephyr.pid):
                        process = subprocess.Popen(
                            bwrap_argv(runtime, repair / "workspace", session / "home", mode="gui", x_socket=x_socket),
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=log,
                            text=True,
                            bufsize=1,
                        )
                        return Editor(process, xephyr, log)
                    break
                time.sleep(0.05)
            if xephyr.poll() is None:
                xephyr.terminate()
            xephyr.wait(timeout=5)
        raise Denied("editor_not_connected", "no private Xephyr display available")
    except Exception:
        if xephyr and xephyr.poll() is None:
            xephyr.terminate()
            xephyr.wait(timeout=5)
        log.close()
        raise


def serve(
    repair: Path,
    *,
    launcher: Callable | None = None,
    converter: AsepriteConverter | None = None,
    ready_timeout: float = 90,
) -> dict:
    path = socket_path(repair)
    repair = admit_repair(repair)
    broker = Broker(repair, launcher=launcher, converter=converter, ready_timeout=ready_timeout)
    sock = None
    old_handlers = {}
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if path.exists():
            require(path.is_socket() and path.stat().st_uid == os.getuid(), "unsafe_path")
            path.unlink()  # Only the lease owner may replace a stale socket.
        mask = os.umask(0o177)
        try:
            sock.bind(str(path))
        finally:
            os.umask(mask)
        path.chmod(0o600)
        sock.listen(8)
        sock.settimeout(0.2)

        def stop_signal(signum, frame):
            broker.stopping = True

        import threading

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                old_handlers[signum] = signal.signal(signum, stop_signal)
        while not broker.stopping:
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(5)
                try:
                    with conn.makefile("rb") as stream:
                        line = stream.readline(MAX_REQUEST + 1)
                    require(len(line) <= MAX_REQUEST and line.endswith(b"\n"), "invalid_request")
                    payload = json.loads(line)
                    request_depth(payload)
                except (ValueError, Denied, OSError, RecursionError) as e:
                    response = {"ok": False, "error": getattr(e, "code", "invalid_request")}
                    broker.journal.append(action=None, error=response["error"])
                else:
                    # Journal/storage failures must terminate the writer, not be treated as bad input.
                    response = broker.handle(payload)
                # A disconnected caller retrieves the durable outcome with its op_id.
                with suppress(OSError):
                    conn.sendall(canonical(response) + b"\n")
        return {"ok": True, "state": broker.state["state"], "stopped": True}
    finally:
        try:
            broker.close()
        finally:
            if sock is not None:
                sock.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def request(repair: Path, payload: dict) -> dict:
    repair = no_symlinks(repair)
    payload = {**payload, "token": no_symlinks(repair / "session/token").read_text()}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(180)
        conn.connect(str(no_symlinks(repair / "session/broker.sock")))
        conn.sendall(canonical(payload) + b"\n")
        with conn.makefile("rb") as stream:
            line = stream.readline(64 * 1024 * 1024)
        require(bool(line), "broker_disconnected")
        return json.loads(line)


def detach(repair: Path) -> dict:
    repair = no_symlinks(repair)
    socket_path(repair)
    repair = admit_repair(repair)
    with lease(repair):
        pass  # Fail immediately for a second writer; the daemon acquires its own lifetime lease.
    ident = read_json(repair / "repair.json")["repair_id"]
    require(isinstance(ident, str) and ident.isalnum(), "invalid_repair_id")
    unit = "sonoranlab-repair-" + ident
    result = subprocess.run(
        [
            "systemd-run",
            "--user",
            "--unit",
            unit,
            "--collect",
            "--property=Type=exec",
            "--",
            sys.executable,
            "-m",
            "sonoranlab.cli",
            "repair",
            "session",
            "start",
            str(repair),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    require(result.returncode == 0, "session_start_failed")
    return {"ok": True, "unit": unit, "state": "starting"}


def abort(repair: Path) -> dict:
    """Abort with the same writer lease; retain authorization/checkpoint provenance."""
    repair = admit_repair(repair)
    repair = no_symlinks(repair)
    with lease(repair):
        doc = read_json(repair / "repair.json")
        state = read_json(repair / "state.json") if (repair / "state.json").exists() else {"state": doc["state"]}
        state["state"] = "aborted"
        persist(repair / "state.json", state)
    return {"ok": True, "repair": str(repair), "state": "aborted"}
