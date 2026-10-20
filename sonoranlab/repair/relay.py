"""File mailboxes for workers without socket access; credentials stay in the relay."""

from __future__ import annotations

import base64
import ctypes
import errno
import json
import os
import re
import secrets
import stat
import time
from contextlib import suppress
from pathlib import Path

from sonoranlab.repair.authorization import Denied, canonical, require
from sonoranlab.repair.compare import read_json
from sonoranlab.repair.operations import request_depth
from sonoranlab.repair.runtime import no_symlinks
from sonoranlab.repair.session import request

MAX_FILE = 1024 * 1024
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.json")


def rename_new(source: int, name: str, dest: int) -> None:
    # Linux renameat2 RENAME_NOREPLACE claims a file without clobbering an older request.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(source, os.fsencode(name), dest, os.fsencode(name), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def write_new(directory: int, name: str, data: bytes) -> None:
    temp = ".relay-" + secrets.token_hex(16)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.fsync(directory)
    finally:
        os.unlink(temp, dir_fd=directory)


def image_result(response: dict, inbox: int, name: str) -> dict:
    """MCP image content contains one PNG for each of the supported look operations."""
    result = response.get("result", {})
    content = result.get("content", []) if isinstance(result, dict) else []
    images = [block for block in content if block.get("type") == "image"]
    if not images:
        return response
    require(len(images) == 1 and images[0].get("mimeType") == "image/png", "invalid_image")
    data = base64.b64decode(images[0]["data"], validate=True)
    require(data.startswith(b"\x89PNG\r\n\x1a\n"), "invalid_image")
    png = name.removesuffix(".json") + ".png"
    write_new(inbox, png, data)
    return {
        **response,
        "result": {
            **result,
            "content": [
                {"type": "image", "mimeType": "image/png", "path": png} if block.get("type") == "image" else block
                for block in content
            ],
        },
    }


def relay(repair: Path, agent_dir: Path) -> dict:
    repair, agent_dir = no_symlinks(repair), no_symlinks(agent_dir)
    parent = no_symlinks(Path(read_json(repair / "repair.json")["parent_job"]))
    repo = Path(__file__).resolve().parents[2]
    for protected in (repair, parent, repo):
        require(not agent_dir.is_relative_to(protected) and not protected.is_relative_to(agent_dir), "unsafe_agent_dir")
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    directories = [os.open(agent_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for name in ("outbox", "processing", "inbox"):
            no_symlinks(agent_dir / name)
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=directories[0])
            directories.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directories[0]))
        _, outbox, processing, inbox = directories
        while True:
            state = read_json(repair / "state.json")
            if state.get("session_state") == "STOPPED":
                return {"ok": True, "stopped": True}
            status = request(repair, {"action": "status"})
            if status.get("session_state") == "STOPPED":
                return {"ok": True, "stopped": True}
            for name in sorted(os.listdir(outbox)):
                if not NAME.fullmatch(name):
                    continue
                info = os.stat(name, dir_fd=outbox, follow_symlinks=False)
                require(not stat.S_ISLNK(info.st_mode), "unsafe_path")
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                    continue
                # Check before dispatch as well as during atomic publication: retries must never overwrite.
                for output in (name, name.removesuffix(".json") + ".png"):
                    try:
                        os.stat(output, dir_fd=inbox, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise Denied("relay_output_exists")
                try:
                    rename_new(outbox, name, processing)
                except FileNotFoundError:
                    continue
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=processing)
                try:
                    info = os.fstat(fd)
                    require(
                        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_FILE,
                        "invalid_request",
                    )
                    with os.fdopen(fd, "rb") as stream:
                        fd = None
                        raw = stream.read(MAX_FILE + 1)
                    require(len(raw) <= MAX_FILE, "invalid_request")
                    payload = json.loads(raw)
                    request_depth(payload)
                    require(isinstance(payload, dict), "invalid_request")
                    payload.pop("token", None)
                    require(payload.get("action") in ("op", "read", "status"), "action_not_relayable")
                    response = request(repair, payload)
                except (Denied, ValueError, TypeError, RecursionError) as e:
                    response = {"ok": False, "error": getattr(e, "code", "invalid_request")}
                finally:
                    if fd is not None:
                        os.close(fd)
                try:
                    response = image_result(response, inbox, name)
                    write_new(inbox, name, canonical(response) + b"\n")
                except FileExistsError as e:
                    raise Denied("relay_output_exists") from e
                os.unlink(name, dir_fd=processing)
            time.sleep(0.1)
    except OSError as e:
        if read_json(repair / "state.json").get("session_state") == "STOPPED":
            return {"ok": True, "stopped": True}
        if e.errno == errno.ELOOP:
            raise Denied("unsafe_path") from e
        raise
    finally:
        for directory in directories:
            os.close(directory)
