"""Human production review, separate from the pipeline's technical verdict.

A review is bound (sha256) to the exact artifact the human looked at: report, manifest, exported files, contact sheet.
Changing any of them after approval makes the job not production usable (same rule as Phase 1 scoring).
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Literal

from sonoranlab.artifacts import sha256_file, verify_raw, write_json

SCHEMA = "sonoranlab.review/2"


def _load(path: Path) -> object:
    if path.is_symlink():
        raise ValueError(f"{path.name} is a symlink")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"{path.name}: unreadable ({e})") from None


def _report_passes(job_dir: Path) -> bool:
    try:
        report = _load(Path(job_dir) / "report.json")
    except ValueError:
        return False
    return isinstance(report, dict) and report.get("pass") is True


def _export_files(job: Path) -> dict[str, str]:
    """{relpath: sha256} for every non-directory under export/; a symlink gets "" so it never matches a hash."""
    root = job / "export"
    files = [p for p in root.rglob("*") if not p.is_dir()] if root.is_dir() else []
    return {p.relative_to(job).as_posix(): "" if p.is_symlink() else sha256_file(p) for p in sorted(files)}


def _provenance(job: Path) -> tuple[dict | None, list[str]]:
    """Check report/manifest/outputs/raw against disk; returns (binds from the current disk state, problems)."""
    problems: list[str] = []
    if not _report_passes(job):
        return None, [f"{job}: review requires a passing report.json in a validated job dir"]
    try:
        manifest = _load(job / "manifest.json")
    except ValueError as e:
        return None, [f"missing provenance: {e}"]
    result = manifest.get("result") if isinstance(manifest, dict) else None
    outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
    if not isinstance(result, dict) or result.get("pass") is not True or result.get("exported") is not True:
        problems.append("manifest.json result is not a passing, exported job")
    if not isinstance(outputs, dict):
        return None, [*problems, "manifest.json has no outputs"]
    if "report.json" not in outputs:
        problems.append("manifest outputs lack report.json")
    listed_export = sorted(k for k in outputs if k.startswith("export/"))
    if not listed_export:
        problems.append("manifest outputs list no export/ files")
    export = _export_files(job)
    for rel in sorted(set(outputs) & ({"report.json", "contact.png"} | set(listed_export))):
        p = job / rel
        if p.is_symlink() or not p.is_file():
            problems.append(f"{rel}: missing or not a regular file")
        elif sha256_file(p) != outputs[rel]:
            problems.append(f"{rel}: sha256 differs from manifest")
    if set(export) != set(listed_export):
        problems.append(f"export/ files differ from manifest: {sorted(set(export) ^ set(listed_export))}")
    try:
        problems += verify_raw(job)
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(f"raw verification failed: {e}")
    contact = job / "contact.png"
    binds = {
        "report_sha256": sha256_file(job / "report.json"),
        "manifest_sha256": sha256_file(job / "manifest.json"),
        "export": export,
        "contact_sha256": sha256_file(contact) if contact.is_file() and not contact.is_symlink() else None,
        "job": str(job.resolve()),
    }
    return binds, problems


def record_review(
    job_dir: Path,
    *,
    verdict: Literal["pass", "fail"],
    reviewer: str,
    cleanup_minutes: float | None = None,
    started_at: str,
    ended_at: str,
    notes: str = "",
    supersedes: Path | None = None,
    carried_from: str | None = None,
) -> Path:
    """Exclusively record review of a technically passing job, bound to its current artifacts.

    `supersedes` names an earlier review file this one replaces (the caller moves it aside; nothing is deleted here).
    """
    job_dir = Path(job_dir)
    binds, problems = _provenance(job_dir)
    if problems:
        raise ValueError(f"{job_dir}: cannot bind a review: " + "; ".join(problems))
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
    doc: dict = {
        "schema": SCHEMA,
        "verdict": verdict,
        "reviewer": reviewer,
        "cleanup_minutes": cleanup_minutes,
        "started_at": started_at,
        "ended_at": ended_at,
        "notes": notes,
        "binds": binds,
    }
    if carried_from is not None:
        doc["carried_from"] = carried_from
    if supersedes is not None:
        supersedes = Path(supersedes)
        old = _load(supersedes) if supersedes.is_file() else None
        if not isinstance(old, dict):
            raise ValueError(f"{supersedes}: superseded review must be an existing JSON object file")
        doc["supersedes"] = {
            "path": str(supersedes),
            "sha256": sha256_file(supersedes),
            **{k: old.get(k) for k in ("verdict", "reviewer", "started_at", "ended_at")},
        }
    try:
        return write_json(job_dir, "review.json", doc)
    except FileExistsError as e:
        raise ValueError(f"{job_dir / 'review.json'} exists; reviews cannot be overwritten") from e


def review_problems(job_dir: Path) -> list[str]:
    """Why `job_dir` is not production usable (empty = usable)."""
    job = Path(job_dir)
    binds, problems = _provenance(job)
    try:
        review = _load(job / "review.json")
    except ValueError as e:
        return [*problems, f"no usable review: {e}"]
    if not isinstance(review, dict) or review.get("schema") != SCHEMA:
        return [*problems, f"review.json is not schema {SCHEMA} (legacy reviews are not bound to the artifact)"]
    if review.get("verdict") != "pass":
        problems.append("review verdict is not pass")
    bound = review.get("binds")
    if not isinstance(bound, dict) or binds is None:
        return [*problems, "review.json has no binds block"] if binds is not None else problems
    for key in ("report_sha256", "manifest_sha256", "contact_sha256"):
        if bound.get(key) != binds[key]:
            problems.append(f"{key} differs from the reviewed artifact")
    reviewed = bound.get("export")
    if not isinstance(reviewed, dict) or not reviewed:
        return [*problems, "review binds no export files"]
    for rel in sorted(set(reviewed) | set(binds["export"])):
        if rel not in binds["export"]:
            problems.append(f"{rel}: reviewed file was removed")
        elif rel not in reviewed:
            problems.append(f"{rel}: file added after review")
        elif reviewed[rel] != binds["export"][rel]:
            problems.append(f"{rel}: changed after review")
    return problems


def production_usable(job_dir: Path) -> bool:
    """True only when the job passes, the bound human review passes, and nothing it covers changed since."""
    return not review_problems(job_dir)
