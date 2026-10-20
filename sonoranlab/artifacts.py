"""Job directory, immutable raw copies and `manifest.json` (ADR-0007: raw inputs are never modified or overwritten).

A job dir is created once and only ever added to: reruns go to a new dir. Nothing here overwrites a file.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import numpy as np
from PIL import Image

SCHEMA = "sonoranlab.manifest/1"
_STAGE = re.compile(r"^[0-9]{2}_[a-z_]+$")
_REPO = Path(__file__).resolve().parent.parent


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _flat_name(name: str) -> str:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"{name!r}: expected a plain file name, no directories")
    return name


def create_job(out: Path) -> Path:
    """Make `out/raw` and `out/processed`; `out` must not exist or be an empty directory."""
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(
            f"{out} exists and is not an empty directory: reruns go to a new job dir; nothing is overwritten"
        )
    (out / "raw").mkdir(parents=True)
    (out / "processed").mkdir()
    return out


def ingest_raw(job: Path, sources: dict[str, Path] | list[Path]) -> dict[str, str]:
    """Byte-copy the inputs to `job/raw/<name>` (read-only, sha256-verified); once per job. Returns {name: sha256}."""
    job = Path(job)
    pairs = list(sources.items()) if isinstance(sources, dict) else [(Path(p).name, p) for p in sources]
    named = {_flat_name(n): Path(p) for n, p in pairs}
    if len(named) != len(pairs):
        raise ValueError("two inputs share a name")
    if any((job / "raw").iterdir()):
        raise FileExistsError(f"{job / 'raw'} already holds inputs: raw is ingested once per job")
    root = job.resolve()
    for p in named.values():
        if not p.is_file():
            raise FileNotFoundError(f"{p}: not a file")
        if p.resolve().is_relative_to(root):
            raise ValueError(f"{p} is inside the job dir {job}")
    hashes = {}
    for name, src in named.items():
        dst = job / "raw" / name
        shutil.copyfile(src, dst)
        hashes[name] = sha256_file(src)
        if sha256_file(dst) != hashes[name]:
            raise OSError(f"{dst}: copy does not match {src} (sha256)")
        dst.chmod(0o444)
    return hashes


def _save_png(path: Path, frame: np.ndarray) -> None:
    if frame.ndim != 3 or frame.shape[2] != 4 or frame.dtype != np.uint8:
        raise ValueError(f"{path.name}: expected an (H, W, 4) uint8 RGBA frame, got {frame.dtype} {frame.shape}")
    with open(path, "xb") as f:
        Image.fromarray(frame, "RGBA").save(f, "PNG")


def write_stage(job: Path, stage: str, frames: dict[str, list[np.ndarray]]) -> dict[str, str]:
    """Write `processed/<stage>/<direction>_<i>.png`; returns {path relative to job: sha256}; once per stage."""
    if not _STAGE.match(stage):
        raise ValueError(f"stage {stage!r} must match {_STAGE.pattern} (e.g. 02_pixelize)")
    d = Path(job) / "processed" / stage
    d.mkdir()  # FileExistsError if the stage was already written
    hashes = {}
    for direction, clip in frames.items():
        for i, frame in enumerate(clip):
            p = d / f"{_flat_name(direction)}_{i}.png"
            _save_png(p, frame)
            hashes[p.relative_to(job).as_posix()] = sha256_file(p)
    return hashes


def write_json(job: Path, name: str, obj: object) -> Path:
    """Write `job/<name>` (sorted keys, indent 1, trailing newline); refuses to overwrite."""
    p = Path(job) / _flat_name(name)
    with open(p, "x") as f:
        f.write(json.dumps(obj, sort_keys=True, indent=1) + "\n")
    return p


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _git_commit() -> str | None:
    if not (_REPO / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def build_manifest(
    *,
    job: Path,
    profile_path: Path,
    character: str,
    animation: str,
    inputs: dict[str, str],
    outputs: dict[str, str],
    params: dict,
    timings: dict,
    upstream: dict | None = None,
) -> dict:
    """The job's provenance record. `created_at` and `timings` are the only fields that differ between identical reruns.

    `job` is checked to exist but its path is deliberately not recorded (it differs on every rerun).
    """
    if not Path(job).is_dir():
        raise FileNotFoundError(f"{job}: not a job directory")
    profile_path = Path(profile_path)
    return {
        "schema": SCHEMA,
        "profile": {"path": profile_path.as_posix(), "sha256": sha256_file(profile_path)},
        "character": character,
        "animation": animation,
        "inputs": dict(sorted(inputs.items())),
        "outputs": dict(sorted(outputs.items())),
        "params": params,
        "tools": {
            "python": platform.python_version(),
            **{d: _version(d) for d in ("numpy", "pillow", "pyyaml", "pixeloe", "torch", "sonoranlab")},
        },
        "git": _git_commit(),
        "upstream": upstream,
        "timings": timings,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def verify_raw(job: Path) -> list[str]:
    """Recompute `raw/` hashes against `manifest.json` inputs; returns mismatch descriptions (empty = unchanged)."""
    job = Path(job)
    expected = json.loads((job / "manifest.json").read_text())["inputs"]
    raw = job / "raw"
    problems = []
    for name, want in sorted(expected.items()):
        p = raw / name
        if not p.is_file():
            problems.append(f"raw/{name}: missing")
        elif (got := sha256_file(p)) != want:
            problems.append(f"raw/{name}: sha256 {got} != manifest {want}")
    problems += [f"raw/{p.name}: not in manifest" for p in sorted(raw.iterdir()) if p.name not in expected]
    return problems
