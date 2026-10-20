"""Human production review, separate from the pipeline's technical verdict.

A review is bound (sha256) to the exact artifact the human looked at: report, manifest, exported files, contact sheet.
Changing any of them after approval makes the job not production usable (same rule as Phase 1 scoring).
"""

from __future__ import annotations

import json
import math
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal

from sonoranlab.artifacts import require_regular_file, verify_raw, write_json
from sonoranlab.artifacts import sha256_file as _sha256_file

SCHEMA = "sonoranlab.review/2"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def sha256_file(path: Path) -> str:
    """Hash only a regular file (no symlink, FIFO or device); review-local, the pipeline hasher stays permissive."""
    require_regular_file(path)
    return _sha256_file(path)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def _load(path: Path) -> object:
    try:
        require_regular_file(path)
        with path.open("rb") as f:
            data = f.read(_MAX_JSON_BYTES + 1)
        if len(data) > _MAX_JSON_BYTES:
            raise ValueError(f"JSON exceeds {_MAX_JSON_BYTES} bytes")
        return json.loads(data, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (OSError, ValueError, RecursionError) as e:
        raise ValueError(f"{path.name}: unreadable ({e})") from None


def _error(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def _metadata_errors(review: dict) -> list[dict[str, str]]:
    errors = []
    if review.get("schema") != SCHEMA:
        errors.append(_error("REVIEW_SCHEMA_INVALID", "schema", f"review.json must use schema {SCHEMA}"))
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not any(ch.isprintable() and not ch.isspace() for ch in reviewer):
        errors.append(_error("REVIEW_REVIEWER_INVALID", "reviewer", "reviewer must be a non-empty string"))
    verdict = review.get("verdict")
    if verdict not in ("pass", "fail"):
        errors.append(_error("REVIEW_VERDICT_INVALID", "verdict", "verdict must be pass|fail"))
    minutes = review.get("cleanup_minutes")
    try:
        valid_minutes = type(minutes) in (int, float) and minutes >= 0 and math.isfinite(float(minutes))
    except OverflowError:
        valid_minutes = False
    if (verdict == "pass" or minutes is not None) and not valid_minutes:
        errors.append(
            _error("REVIEW_CLEANUP_INVALID", "cleanup_minutes", "cleanup_minutes must be a finite, non-negative number")
        )
    times = {}
    for field in ("started_at", "ended_at"):
        try:
            value = review.get(field)
            if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
                raise ValueError
            timestamp = datetime.fromisoformat(value)
            if timestamp.utcoffset() is None:
                raise ValueError
            times[field] = timestamp
        except ValueError:
            errors.append(_error("REVIEW_TIMESTAMP_INVALID", field, f"{field} must be ISO-8601 with a timezone"))
    if len(times) == 2 and times["ended_at"] < times["started_at"]:
        errors.append(_error("REVIEW_INTERVAL_INVALID", "ended_at", "ended_at precedes started_at"))
    for field in ("notes", "carried_from"):
        if field in review and not isinstance(review[field], str):
            errors.append(_error("REVIEW_METADATA_INVALID", field, f"{field} must be a string"))
    if "supersedes" in review:
        old = review["supersedes"]
        if not isinstance(old, dict) or any(
            not isinstance(old.get(k), str) for k in ("path", "sha256", "verdict", "reviewer", "started_at", "ended_at")
        ):
            errors.append(
                _error(
                    "REVIEW_METADATA_INVALID", "supersedes", "supersedes must contain string path/hash/review metadata"
                )
            )
    return errors


def _report_passes(job_dir: Path) -> bool:
    try:
        report = _load(Path(job_dir) / "report.json")
    except ValueError:
        return False
    return isinstance(report, dict) and report.get("pass") is True


def _export_files(job: Path) -> dict[str, str]:
    """Hash every export file; refuse symlinks and special files, including the export root."""
    root = job / "export"
    pending = [root]
    files = {}
    while pending:
        p = pending.pop()
        mode = p.lstat().st_mode
        if stat.S_ISDIR(mode):
            pending.extend(p.iterdir())
        elif stat.S_ISREG(mode) and p != root:
            files[p.relative_to(job).as_posix()] = sha256_file(p)
        else:
            raise ValueError(f"{p}: export entry must be a regular file or directory")
    return files


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
    if not isinstance(manifest.get("inputs"), dict):
        return None, [*problems, "manifest.json has no inputs"]
    if "report.json" not in outputs:
        problems.append("manifest outputs lack report.json")
    listed_export = sorted(k for k in outputs if k.startswith("export/"))
    if not listed_export:
        problems.append("manifest outputs list no export/ files")
    export = _export_files(job)
    for rel in sorted(outputs):
        p = job / rel
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            problems.append(f"{rel}: output path must be relative to the job")
        elif p.is_symlink() or not p.is_file() or any(q.is_symlink() for q in p.parents if q.is_relative_to(job)):
            problems.append(f"{rel}: missing, not a regular file, or under a symlinked directory")
        elif sha256_file(p) != outputs[rel]:
            problems.append(f"{rel}: sha256 differs from manifest")
    if set(export) != set(listed_export):
        problems.append(f"export/ files differ from manifest: {sorted(set(export) ^ set(listed_export))}")
    profile = manifest.get("profile")
    if not isinstance(profile, dict) or not isinstance(profile.get("path"), str) or not profile["path"]:
        problems.append("manifest.json has no profile path")
    else:
        p = Path(profile["path"])
        if p.is_symlink() or not p.is_file():
            problems.append(f"profile {p}: missing or not a regular file")
        elif sha256_file(p) != profile.get("sha256"):
            problems.append(f"profile {p}: sha256 differs from manifest")
    try:
        problems += verify_raw(job, inputs=manifest["inputs"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(f"raw verification failed: {e}")
    contact = job / "contact.png"
    binds = {
        "report_sha256": sha256_file(job / "report.json"),
        "manifest_sha256": sha256_file(job / "manifest.json"),
        "export": export,
        "contact_sha256": sha256_file(contact) if contact.exists() or contact.is_symlink() else None,
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
    try:
        binds, problems = _provenance(job_dir)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as e:
        raise ValueError(f"{job_dir}: cannot bind a review: {e}") from None
    if problems:
        raise ValueError(f"{job_dir}: cannot bind a review: " + "; ".join(problems))
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
        if not isinstance(supersedes, (str, Path)):
            raise ValueError("supersedes must be a file path")
        supersedes = Path(supersedes)
        old = _load(supersedes) if supersedes.is_file() else None
        if not isinstance(old, dict):
            raise ValueError(f"{supersedes}: superseded review must be an existing JSON object file")
        doc["supersedes"] = {
            "path": str(supersedes),
            "sha256": sha256_file(supersedes),
            **{k: old.get(k) for k in ("verdict", "reviewer", "started_at", "ended_at")},
        }
    if errors := _metadata_errors(doc):
        raise ValueError("; ".join(f"{e['code']}: {e['message']}" for e in errors))
    try:
        return write_json(job_dir, "review.json", doc, max_bytes=_MAX_JSON_BYTES, strict=True)
    except FileExistsError as e:
        raise ValueError(f"{job_dir / 'review.json'} exists; reviews cannot be overwritten") from e


def review_errors(job_dir: Path, *, review_path: Path | None = None) -> list[dict[str, str]]:
    """Recheck metadata and all artifact bindings; empty means production usable.

    `review_path` allows read-only checking of archived reviews against the same job.
    """
    job = Path(job_dir)
    try:
        binds, problems = _provenance(job)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as e:
        binds, problems = None, [f"artifact verification failed: {e}"]
    errors = [_error("REVIEW_BINDING_INVALID", "binds", p) for p in problems]
    try:
        review = _load(review_path if review_path is not None else job / "review.json")
    except ValueError as e:
        return [*errors, _error("REVIEW_UNREADABLE", "review.json", f"no usable review: {e}")]
    if not isinstance(review, dict):
        return [*errors, _error("REVIEW_SCHEMA_INVALID", "schema", "review.json must be a schema 2 object")]
    errors += _metadata_errors(review)
    if review.get("verdict") == "fail":
        errors.append(_error("REVIEW_NOT_PASS", "verdict", "review verdict is not pass"))
    bound = review.get("binds")
    if not isinstance(bound, dict):
        return [*errors, _error("REVIEW_BINDING_INVALID", "binds", "review.json has no binds block")]
    if binds is None:
        return errors
    for key in ("report_sha256", "manifest_sha256", "contact_sha256"):
        if key not in bound or bound[key] != binds[key]:
            errors.append(_error("REVIEW_BINDING_INVALID", f"binds.{key}", f"{key} differs from the reviewed artifact"))
    reviewed = bound.get("export")
    if not isinstance(reviewed, dict) or not reviewed:
        return [*errors, _error("REVIEW_BINDING_INVALID", "binds.export", "review binds no export files")]
    for rel in sorted(set(reviewed) | set(binds["export"])):
        if rel not in binds["export"]:
            message = f"{rel}: reviewed file was removed"
        elif rel not in reviewed:
            message = f"{rel}: file added after review"
        elif reviewed[rel] != binds["export"][rel]:
            message = f"{rel}: changed after review"
        else:
            continue
        errors.append(_error("REVIEW_BINDING_INVALID", f"binds.export.{rel}", message))
    return errors


def review_problems(job_dir: Path) -> list[str]:
    """Human-readable reasons `job_dir` is not production usable (empty = usable)."""
    return [f"{e['code']}: {e['message']}" for e in review_errors(job_dir)]


def production_usable(job_dir: Path) -> bool:
    """True only when the job passes, the bound human review passes, and nothing it covers changed since."""
    return not review_errors(job_dir)
