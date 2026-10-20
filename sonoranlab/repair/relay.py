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
from sonoranlab.repair import Refused
from sonoranlab.repair.authorization import Denied, canonical, require
from sonoranlab.repair.compare import read_json
from sonoranlab.repair.confine import (
    _MARKERS,
    agent_root,
    create_destination,
    git_root,
    open_destination,
    verify_destination,
)
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


def _safe_mailbox(directory: int) -> None:
    """Apply safe_tree's ownership/type/link checks without traversing pathnames."""
    require(os.fstat(directory).st_uid == os.getuid(), "unsafe_path")
    for name in os.listdir(directory):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        require(info.st_uid == os.getuid(), "unsafe_path")
        require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), "unsafe_path")
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _safe_mailbox(child)
            finally:
                os.close(child)
        else:
            require(info.st_nlink == 1, "unsafe_path")


def _verify_queue(mailbox: int, name: str, queue: int) -> None:
    """Check the pinned queue's identity and reject job/repair layouts."""
    try:
        opened = os.fstat(queue)
        current = os.stat(name, dir_fd=mailbox, follow_symlinks=False)
        require(
            stat.S_ISDIR(current.st_mode)
            and stat.S_ISDIR(opened.st_mode)
            and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino),
            "race_detected",
        )
        entries = set(os.listdir(queue))
        require(not entries.intersection(_MARKERS), "unsafe_mailbox")
        require(
            not (
                {"raw", "processed"} <= entries
                and all(
                    stat.S_ISDIR(os.stat(child, dir_fd=queue, follow_symlinks=False).st_mode)
                    for child in ("raw", "processed")
                )
            ),
            "unsafe_mailbox",
        )
    except OSError as e:
        raise Denied("race_detected") from e


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
    root = agent_root()
    forbidden = (repair, parent, profile.export.godot_project, *((palette_checkout,) if palette_checkout else ()))
    if reuse:
        agent_dir, directory = open_destination(agent_dir, root=root, forbidden=forbidden)
    else:
        agent_dir, directory = create_destination(
            agent_dir,
            root=root,
            kind="mailbox",
            id=doc["repair_id"],
            forbidden=forbidden,
        )
    directories = [directory]

    def verify_queues() -> None:
        try:
            verify_destination(agent_dir, directory, forbidden=forbidden)
        except Refused as e:
            raise Denied("race_detected") from e
        for name, queue in zip(names, directories[1:], strict=False):
            _verify_queue(directory, name, queue)

    try:
        if reuse:
            # Task files may coexist with the queues, but the identity must be ours.
            try:
                fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE, "invalid_mailbox")
                    identity = json.loads(stream.read(MAX_FILE + 1))
                require(identity == {"repair_id": doc["repair_id"]}, "invalid_mailbox")
            except (OSError, ValueError) as e:
                raise Denied("invalid_mailbox") from e
        for name in names:
            if not reuse:
                os.mkdir(name, mode=0o700, dir_fd=directory)
            try:
                directories.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory))
            except OSError as e:
                raise Denied("invalid_mailbox") from e
            verify_queues()
        if reuse:
            _safe_mailbox(directory)
        if not reuse:
            write_new(directory, marker, canonical({"repair_id": doc["repair_id"]}) + b"\n")
        _, outbox, processing, inbox = directories
        while True:
            verify_queues()
            state = read_json(repair / "state.json")
            if state.get("session_state") == "STOPPED":
                return {"ok": True, "stopped": True}
            status = request(repair, {"action": "status"})
            verify_queues()
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
                    verify_queues()
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
                except (Denied, ValueError, TypeError, RecursionError) as e:
                    response = {"ok": False, "error": getattr(e, "code", "invalid_request")}
                else:
                    verify_queues()
                    try:
                        response = request(repair, payload)
                    except (Denied, ValueError, TypeError, RecursionError) as e:
                        response = {"ok": False, "error": getattr(e, "code", "invalid_request")}
                finally:
                    if fd is not None:
                        os.close(fd)
                try:
                    verify_queues()
                    response = image_result(response, inbox, name)
                    verify_queues()
                    write_new(inbox, name, canonical(response) + b"\n")
                except FileExistsError as e:
                    raise Denied("relay_output_exists") from e
                verify_queues()
                os.unlink(name, dir_fd=processing)
            time.sleep(0.1)
    except Denied as e:
        if e.code in ("race_detected", "unsafe_mailbox"):
            return {"ok": False, "error": e.code}
        raise
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Denied("unsafe_path") from e
        raise
    finally:
        for opened in directories:
            os.close(opened)
