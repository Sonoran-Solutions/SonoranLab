"""Immutable, hash-bound authority for a repair; masks are computed, never advisory."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np

from sonoranlab.artifacts import sha256_file
from sonoranlab.repair import Refused
from sonoranlab.repair.compare import expected_mapping, read_json
from sonoranlab.repair.confine import admit_repair
from sonoranlab.repair.runtime import no_symlinks

OPERATIONS = {"draw.pixels", "draw.grid", "draw.clear", "draw.blit", "cel.move"}


class Denied(Refused):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def require(condition: bool, code: str = "invalid_request") -> None:
    if not condition:
        raise Denied(code)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def source_revision(doc: dict) -> str:
    # The interchange revision predates this field: bind its canonical source hash map.
    return hashlib.sha256(json.dumps(doc["source"], sort_keys=True).encode()).hexdigest()


def safe_tree(root: Path) -> None:
    for p in (root, *root.rglob("*")):
        no_symlinks(p)
        s = p.stat()
        require(s.st_uid == os.getuid() and (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)), "unsafe_path")
        require(not stat.S_ISREG(s.st_mode) or s.st_nlink == 1, "unsafe_path")


def open_private(path: Path, flags: int, mode: int = 0o600) -> int:
    no_symlinks(path)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    s = os.fstat(fd)
    if s.st_uid != os.getuid() or not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        os.close(fd)
        raise Denied("unsafe_path")
    return fd


def persist(path: Path, value: dict) -> None:
    """Atomic state replacement; a crash leaves either complete version."""
    no_symlinks(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, "wb") as f:
        f.write(canonical(value) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lease(repair: Path) -> Iterator[None]:
    repair = admit_repair(repair)
    session = no_symlinks(repair / "session")
    session.mkdir(mode=0o700, exist_ok=True)
    require(session.stat().st_uid == os.getuid(), "unsafe_path")
    fd = open_private(session / "lease.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Denied("lease_held") from e
        yield
    finally:
        os.close(fd)


def target_key(target: dict) -> tuple:
    require(isinstance(target, dict))
    kind = target.get("kind")
    require(kind in ("anim", "still"))
    require(isinstance(target.get("direction"), str) and bool(re.fullmatch("[a-z0-9_-]+", target["direction"])))
    if kind == "anim":
        require(type(target.get("index")) is int and target["index"] >= 0)
    else:
        require("index" not in target)
    return kind, target["direction"], target.get("index", 0)


def frame_number(target: dict, mapping: dict) -> str:
    key = target_key(target)
    for n, entry in mapping.items():
        if key == (entry["kind"], entry["direction"], entry["index"]):
            return n
    raise Denied("target_not_authorized")


def mask_array(mask: dict, cell: list[int]) -> np.ndarray:
    require(isinstance(mask, dict) and not set(mask) - {"rects", "pixels"}, "invalid_mask")
    w, h = cell
    result = np.zeros((h, w), dtype=bool)
    for field, length in (("rects", 4), ("pixels", 2)):
        entries = mask.get(field, [])
        require(isinstance(entries, list), "invalid_mask")
        for item in entries:
            require(
                isinstance(item, list) and len(item) == length and all(type(v) is int for v in item), "invalid_mask"
            )
            x, y = item[:2]
            rw, rh = item[2:] if length == 4 else (1, 1)
            require(0 <= x < w and 0 <= y < h and rw > 0 and rh > 0 and x + rw <= w and y + rh <= h, "invalid_mask")
            result[y : y + rh, x : x + rw] = True
    require(bool(result.any()), "invalid_mask")
    return result


def validate(spec: dict, doc: dict, mapping: dict) -> dict[str, np.ndarray]:
    require(
        set(spec)
        == {
            "schema",
            "repair_id",
            "source_revision",
            "parent",
            "operations",
            "targets",
            "max_iterations",
            "intent",
            "authorized_by",
            "authorized_at",
        },
        "invalid_authorization",
    )
    require(
        spec["schema"] == "sonoranlab.repair-authorization/1" and spec["repair_id"] == doc["repair_id"],
        "invalid_authorization",
    )
    require(spec["source_revision"] == source_revision(doc), "stale_source_revision")
    require(
        spec["parent"]
        == {k + "_sha256": doc["parent_hashes"].get(k + ".json") for k in ("report", "manifest", "review")},
        "parent_hash_mismatch",
    )
    ops = spec["operations"]
    require(
        isinstance(ops, list)
        and bool(ops)
        and all(isinstance(o, str) for o in ops)
        and len(set(ops)) == len(ops)
        and set(ops) <= OPERATIONS,
        "invalid_authorization",
    )
    require(type(spec["max_iterations"]) is int and 1 <= spec["max_iterations"] <= 2, "invalid_authorization")
    for name in ("intent", "authorized_by", "authorized_at"):
        require(isinstance(spec[name], str) and bool(spec[name].strip()), "invalid_authorization")
    try:
        require(datetime.fromisoformat(spec["authorized_at"]).utcoffset() is not None, "invalid_authorization")
    except ValueError as e:
        raise Denied("invalid_authorization") from e
    require(isinstance(spec["targets"], list) and bool(spec["targets"]), "invalid_authorization")
    masks = {}
    for target in spec["targets"]:
        require(
            isinstance(target, dict)
            and set(target)
            == (
                {"kind", "direction", "mask", "derive_mirror"} | ({"index"} if target.get("kind") == "anim" else set())
            ),
            "invalid_authorization",
        )
        n = frame_number(target, mapping)
        require(n not in masks, "invalid_authorization")
        require(type(target["derive_mirror"]) is bool, "invalid_authorization")
        require(
            not target["derive_mirror"]
            or (target["kind"] == "anim" and target["direction"] in doc["directions"]["mirror"].values()),
            "invalid_mirror",
        )
        masks[n] = mask_array(target["mask"], doc["cell"])
    return masks


def authorize(repair: Path, spec: dict) -> dict:
    repair = admit_repair(repair)
    repair = no_symlinks(repair)
    with lease(repair):
        doc = read_json(repair / "repair.json")
        state = (
            read_json(repair / "state.json")
            if (repair / "state.json").exists()
            else {"state": doc["state"], "iterations": 0}
        )
        require(state["state"] == "prepared", "invalid_state")
        mapping = read_json(repair / "mapping.json")
        require(mapping == expected_mapping(doc), "mapping_mismatch")
        validate(spec, doc, mapping)
        path = repair / "authorization.json"
        fd = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "wb") as f:
            f.write(canonical(spec) + b"\n")
            f.flush()
            os.fchmod(f.fileno(), 0o444)
            os.fsync(f.fileno())
        state["authorization_sha256"] = sha256_file(path)
        persist(repair / "state.json", state)
    return {
        "ok": True,
        "authorization_sha256": state["authorization_sha256"],
        "source_revision": spec["source_revision"],
    }
