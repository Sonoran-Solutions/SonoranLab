"""Human production review, separate from the pipeline's technical verdict."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Literal

from sonoranlab.artifacts import write_json


def _report_passes(job_dir: Path) -> bool:
    path = Path(job_dir) / "report.json"
    if path.is_symlink():
        return False
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(report, dict) and report.get("pass") is True


def record_review(
    job_dir: Path,
    *,
    verdict: Literal["pass", "fail"],
    reviewer: str,
    cleanup_minutes: float | None = None,
    started_at: str,
    ended_at: str,
    notes: str = "",
) -> Path:
    """Exclusively record review of a technically passing job; a passing review requires cleanup time."""
    job_dir = Path(job_dir)
    if not _report_passes(job_dir):
        raise ValueError(f"{job_dir}: review requires a passing report.json in a validated job dir")
    if verdict not in {"pass", "fail"} or not reviewer.strip():
        raise ValueError("verdict must be pass|fail and reviewer must be named")
    if (verdict == "pass" and cleanup_minutes is None) or (
        cleanup_minutes is not None and (not math.isfinite(cleanup_minutes) or cleanup_minutes < 0)
    ):
        raise ValueError("passing review requires finite, non-negative cleanup_minutes")
    try:
        if datetime.fromisoformat(ended_at) < datetime.fromisoformat(started_at):
            raise ValueError("ended_at precedes started_at")
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid ISO review interval: {e}") from e
    try:
        return write_json(
            job_dir,
            "review.json",
            {
                "verdict": verdict,
                "reviewer": reviewer,
                "cleanup_minutes": cleanup_minutes,
                "started_at": started_at,
                "ended_at": ended_at,
                "notes": notes,
            },
        )
    except FileExistsError as e:
        raise ValueError(f"{job_dir / 'review.json'} exists; reviews cannot be overwritten") from e


def production_usable(job_dir: Path) -> bool:
    """True only when report.json passes AND the recorded human review passes."""
    if not _report_passes(job_dir):
        return False
    try:
        review = json.loads((Path(job_dir) / "review.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(review, dict) and review.get("verdict") == "pass"
