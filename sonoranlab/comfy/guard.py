"""Run-window admission, memory budgets and pending generation attempts (stdlib only).

Local safety against mistakes, not a security boundary against an adversarial same-user process.
"""

from __future__ import annotations

import fcntl
import hmac
import json
import math
import os
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# ADR-0004: Wan 2.2, 33 frames = 40; single Qwen Edit = 60. The guard adds its own 8 GiB margin.
TRACK_NEED_GIB = {"E": 40, "C": 60}


def min_need_gib(track: str) -> float:
    try:
        return TRACK_NEED_GIB[track]
    except KeyError as e:
        raise ValueError(f"unknown track {track!r}") from e


def admitted_window() -> dict | None:
    """Verify the direct parent's lease and live lock; stale or unreadable claims fail closed."""
    pid = os.getppid()
    nonce = os.environ.get("SONORANLAB_RUNWINDOW_NONCE")
    if os.environ.get("SONORANLAB_RUNWINDOW") != str(pid) or not nonce:
        return None
    work = Path(os.environ.get("SONORANLAB_WORK", Path.home() / "sonoranlab-work"))
    proc = Path(os.environ.get("SONORANLAB_PROC", "/proc"))
    try:
        with os.fdopen(os.open(work / "runwindow.lease.json", os.O_RDONLY | os.O_NONBLOCK)) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return None
            lease = json.load(f)
        if (
            lease["schema"] != "sonoranlab.runwindow-lease/1"
            or type(lease["pid"]) is not int
            or lease["pid"] != pid
            or not hmac.compare_digest(lease["nonce"], nonce)
            or not lease["boot_id"]
            or lease["boot_id"] != (proc / "sys/kernel/random/boot_id").read_text().strip()
            or any(
                type(lease[k]) not in (int, float) or not math.isfinite(lease[k]) or lease[k] < 0
                for k in ("need_gib", "margin_gib", "started")
            )
        ):
            return None
        with (work / "runwindow.lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return lease
            fcntl.flock(lock, fcntl.LOCK_UN)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        pass
    return None


def inside_guard(need_gib: float | None = None) -> bool:
    """True only for a verified window with enough budget for the request."""
    lease = admitted_window()
    return lease is not None and (need_gib is None or lease["need_gib"] >= need_gib)


def guarded_argv(
    argv: list[str], need_gib: float = TRACK_NEED_GIB["E"], allow_pause: tuple[str, ...] = ()
) -> list[str]:
    """Wrap a command in the repository's memory guard."""
    runwindow = REPO / "env/runwindow.py"
    if not runwindow.is_file():
        raise ValueError(f"run-window guard not found: {runwindow}; run from a SonoranLab repository checkout")
    # Never pass --gtt-credit; the documented budgets use host MemAvailable.
    pauses = [x for unit in allow_pause for x in ("--allow-pause", unit)]
    return ["python3", str(runwindow), "--need-gib", f"{need_gib:g}", *pauses, "--", *argv]


def pending_submissions(root: Path) -> list[dict]:
    """Return submission metadata plus out_dir for attempts without a result, for reattach/cancel recovery."""
    return [
        {**json.loads(p.read_text()), "out_dir": str(p.parent)}
        for p in sorted(root.rglob("submission.json"))
        if not (p.parent / "result.json").exists()
    ]
