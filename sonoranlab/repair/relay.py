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
from pathlib import Path

from sonoranlab.profile import load_profile
from sonoranlab.repair.authorization import Denied, canonical, require, safe_tree
from sonoranlab.repair.compare import read_json
from sonoranlab.repair.confine import agent_root, check_destination, git_root
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
    repair = no_symlinks(repair)
    doc = read_json(repair / "repair.json")
    parent = no_symlinks(Path(doc["parent_job"]))
    profile = load_profile(no_symlinks(Path(doc["profile"]["path"])))
    palette_checkout = git_root(no_symlinks(profile.palette.source))
    agent_dir = Path(agent_dir).expanduser()
    names = ("outbox", "processing", "inbox")
    marker = ".sonoranlab-mailbox"
    reuse = agent_dir.exists() or agent_dir.is_symlink()
    # Confine first (pure checks, nothing created), then inspect an existing mailbox.
    agent_dir = check_destination(
        agent_dir,
        root=agent_root(),
        forbidden=(repair, parent, profile.export.godot_project, *((palette_checkout,) if palette_checkout else ())),
        must_not_exist=not reuse,
    )
    if reuse:
        safe_tree(agent_dir)
        # The worker keeps its task files here too; the queues and the marker must exist and be ours.
        require(agent_dir.is_dir() and all((agent_dir / name).is_dir() for name in names), "invalid_mailbox")
        info = (agent_dir / marker).lstat() if (agent_dir / marker).exists() else None
        require(info is not None and stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE, "invalid_mailbox")
        try:
            identity = read_json(agent_dir / marker)
        except ValueError as e:
            raise Denied("invalid_mailbox") from e
        require(identity == {"repair_id": doc["repair_id"]}, "invalid_mailbox")
    if not reuse:
        agent_dir.parent.mkdir(parents=True, exist_ok=True)
        agent_dir.mkdir(mode=0o700)
    directories = [os.open(agent_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for name in names:
            if not reuse:
                os.mkdir(name, mode=0o700, dir_fd=directories[0])
            directories.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directories[0]))
        if not reuse:
            write_new(directories[0], marker, canonical({"repair_id": doc["repair_id"]}) + b"\n")
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
