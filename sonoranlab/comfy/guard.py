"""Run-window ownership, memory budgets and pending generation attempts (stdlib only)."""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# ADR-0004: Wan 2.2, 33 frames = 40; single Qwen Edit = 60. The guard adds its own 8 GiB margin.
TRACK_NEED_GIB = {"E": 40, "C": 60}


def min_need_gib(track: str) -> float:
    try:
        return TRACK_NEED_GIB[track]
    except KeyError as e:
        raise ValueError(f"unknown track {track!r}") from e


def inside_guard() -> bool:
    """Only our direct parent may claim the run window (env/smoke.sh)."""
    return os.environ.get("SONORANLAB_RUNWINDOW") == str(os.getppid())


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
